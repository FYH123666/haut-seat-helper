"""凭据来源解析与受限快照存储。

本模块回答一个问题：**这次请求该用哪一对 Cookie / Authorization？**

三种来源，由 ``HAUT_AUTH_SOURCE`` 决定：

- ``env``     只用 ``.env`` 的手工凭据 —— 与引入本模块之前逐字节一致，
              连快照文件都不会去读（保证既有部署与既有测试零变化）。
- ``browser`` 只用浏览器会话快照。快照缺失/损坏时**明确报错**
              （``CredentialsUnavailableError``），**绝不静默回退到可能已过期的 .env**。
- ``auto``    快照与 ``.env`` 里**较新**的一方胜出。用 ``.env`` 文件的 mtime
              作为"用户刚手工粘贴了新凭据"的真实信号，而不是发明一个凭据寿命上限：
              ``Admin-Token`` 的 JWT 载荷只有 ``login_user_key``（无 ``exp``/``iat``），
              服务端会话时长未知，客户端任何"年龄判定"都是猜测；凭据是否还有效，
              由只读预检真实发现（HTTP 401 / 业务 code），不靠本地时钟猜。

安全约定（本模块是凭据的唯一落盘点）：
- 快照文件权限尽力收紧到 0600（**Windows 上 POSIX 权限位不生效**，如实见
  ``save_snapshot`` 的说明），目录被 ``.gitignore`` 排除；
- ``Snapshot`` / ``Credentials`` 都自定义 ``__repr__``，即使被误写进日志也只留
  来源、时间与单向指纹；
- 所有异常消息只描述状态，不含任何凭据值。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from haut_seat.exceptions import SeatBookError

SNAPSHOT_VERSION = 1
DEFAULT_SNAPSHOT_SOURCE = "browser-login"
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

#: 来源标识 → 面向人的文案（页面与 CLI 共用，绝不含值）
SOURCE_LABELS = {
    "snapshot": "浏览器会话快照",
    "env": ".env 手动配置",
    "none": "未配置",
    # 用户端：由 `services.user_credentials` 构造的"某位用户自己的学校凭据"。
    # 解析层（resolve_credentials）**永远不会**产出这个来源 —— 它只出现在
    # 按用户构造的 settings 副本里，加进标签表只是为了让任何意外展示都不至于
    # 显示成一个光秃秃的英文标识。
    "user": "用户本人绑定的学校账号",
}


def source_label(source: str) -> str:
    """把来源标识翻成人话；未知值原样返回，不抛异常。"""
    return SOURCE_LABELS.get(source, source or SOURCE_LABELS["none"])


def format_time(moment: datetime) -> str:
    """快照与采集结果共用的时间格式。

    单独暴露出来是为了让"采集时刻"与"落盘时刻"长得一模一样 ——
    两处各写一遍 strftime 格式迟早会漂移，而 ``captured_at`` 是
    ``auto`` 模式比较新旧、以及页面展示采集时间的依据。
    """
    return moment.strftime(_TIME_FORMAT)


def fingerprint(value: str | None) -> str:
    """凭据的单向指纹（sha256 前 12 位）。

    用于在页面/日志里指认"当前用的是哪一份凭据"，以及让预检结果缓存以
    "凭据是否变更"为键失效。高熵随机串的前缀不可逆，可以安全展示；
    空值返回空串，避免把"没有凭据"伪装成一个指纹。
    """
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


class CredentialsUnavailableError(SeatBookError):
    """``browser`` 模式下快照缺失或不可用。

    消息只描述状态与补救方式，绝不含任何凭据值。
    """


@dataclass(frozen=True)
class Snapshot:
    """浏览器会话快照。字段刻意最小：不存账号、口令、验证码或 ``exp``（不存在）。"""

    cookie: str
    authorization: str
    captured_at: str
    source: str = DEFAULT_SNAPSHOT_SOURCE
    base_url: str = ""
    version: int = SNAPSHOT_VERSION

    def __repr__(self) -> str:  # pragma: no cover - 简洁性由测试钉住
        # frozen dataclass 的默认 repr 会把令牌原样打进日志，必须覆盖。
        return (
            f"Snapshot(source={self.source!r}, captured_at={self.captured_at!r}, "
            f"base_url={self.base_url!r}, version={self.version}, "
            f"fingerprint={fingerprint(self.authorization)!r}, "
            f"cookie=<redacted>, authorization=<redacted>)"
        )

    @property
    def captured_ts(self) -> float | None:
        """``captured_at`` 对应的 epoch 秒；无法解析返回 None。"""
        try:
            return datetime.strptime(self.captured_at, _TIME_FORMAT).timestamp()
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True)
class Credentials:
    """一次请求实际要用的凭据对。"""

    cookie: str | None
    authorization: str | None
    source: str  # snapshot | env | none
    captured_at: str | None = None

    @property
    def has_authorization(self) -> bool:
        return bool(self.authorization)

    @property
    def source_label(self) -> str:
        return source_label(self.source)

    def fingerprint(self) -> str:
        return fingerprint(self.authorization)

    def __repr__(self) -> str:  # pragma: no cover - 简洁性由测试钉住
        return (
            f"Credentials(source={self.source!r}, captured_at={self.captured_at!r}, "
            f"fingerprint={self.fingerprint()!r}, "
            f"cookie=<redacted>, authorization=<redacted>)"
        )


def snapshot_path(settings) -> Path:
    """快照文件路径（取自配置，支持任意相对/绝对路径）。"""
    return Path(settings.auth_snapshot_file)


def env_file_path(settings) -> Path | None:
    """``.env`` 的路径。

    与 pydantic-settings 的行为一致：相对路径按**进程 CWD** 解析。
    取不到可用的路径字符串时返回 None（``auto`` 模式随后退化为"快照优先"）。
    """
    raw = settings.model_config.get("env_file") if settings.model_config else None
    if isinstance(raw, (str, Path)) and str(raw):
        return Path(str(raw))
    return None


def env_file_mtime(settings) -> float | None:
    """``.env`` 的最后修改时间；读不到返回 None。

    这是 ``auto`` 模式判断"手工更新是否更新鲜"的唯一依据。单独成函数是为了
    让测试可以精确控制这个真实世界的量（而不是依赖运行测试的机器上恰好有一份 .env）。
    """
    path = env_file_path(settings)
    if path is None:
        return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def load_snapshot(settings, *, logger: logging.Logger | None = None) -> Snapshot | None:
    """读取快照；任何问题都返回 None 且只记录路径与异常类型，绝不抛出。

    返回 None 的具体情形：文件不存在、无权读取（Windows 上可能被另一进程占用）、
    JSON 损坏（半写/断电）、``version`` 不受支持、字段缺失或类型/取值非法、
    以及 ``base_url`` 与当前配置不一致（防止把 A 环境的会话发到 B 环境）。
    """
    log = logger or logging.getLogger("haut_seat.auth")
    path = snapshot_path(settings)

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("凭据快照不可读：%s（%s）", path, type(exc).__name__)
        return None

    try:
        payload: Any = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("凭据快照解析失败：%s（%s）", path, type(exc).__name__)
        return None

    if not isinstance(payload, dict):
        log.warning("凭据快照结构非对象：%s", path)
        return None

    version = payload.get("version")
    if version != SNAPSHOT_VERSION:
        log.warning("凭据快照版本不受支持：%s（version=%r）", path, version)
        return None

    cookie = payload.get("cookie")
    authorization = payload.get("authorization")
    if not _is_nonempty_str(cookie) or not _is_nonempty_str(authorization):
        log.warning("凭据快照缺少可用字段：%s", path)
        return None

    base_url = payload.get("base_url") or ""
    if base_url and str(base_url).rstrip("/") != str(settings.base_url).rstrip("/"):
        log.warning("凭据快照的 base_url 与当前配置不一致，拒绝使用：%s", path)
        return None

    captured_at = payload.get("captured_at")
    return Snapshot(
        cookie=cookie,
        authorization=authorization,
        captured_at=captured_at if _is_nonempty_str(captured_at) else "",
        source=str(payload.get("source") or DEFAULT_SNAPSHOT_SOURCE),
        base_url=str(base_url),
        version=SNAPSHOT_VERSION,
    )


def save_snapshot(
    settings,
    *,
    cookie: str,
    authorization: str,
    source: str = DEFAULT_SNAPSHOT_SOURCE,
    now: datetime | None = None,
    logger: logging.Logger | None = None,
) -> str:
    """原子写入快照，返回文件路径。日志只记路径与采集时间，绝不记值。

    权限说明（**必须如实对待**）：``os.chmod(..., 0o600)`` 在 POSIX 上确实产生
    "仅属主可读写"；在 Windows 上 POSIX 权限位不生效，``chmod`` 只映射到只读位，
    真正的访问控制继承自父目录 ACL。因此 Windows 上的实际防护是三条：
    文件位于用户 profile 的 ``data/`` 内、``data/`` 被 ``.gitignore`` 排除、
    以及本模块从不打印任何值。需要更强隔离时可在部署文档给出的 icacls 命令，
    本函数不自动执行它。
    """
    log = logger or logging.getLogger("haut_seat.auth")
    if not _is_nonempty_str(cookie) or not _is_nonempty_str(authorization):
        raise ValueError("拒绝写入空凭据快照")

    moment = now or datetime.now()
    payload = {
        "version": SNAPSHOT_VERSION,
        "source": source,
        "base_url": str(settings.base_url),
        "captured_at": format_time(moment),
        "cookie": cookie,
        "authorization": authorization,
    }

    path = snapshot_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    _harden_dir(path.parent)

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".cred-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        # 先收紧权限再替换，避免"新文件短暂对所有人可读"的窗口。
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    log.info("凭据快照已更新：%s（captured_at=%s）", path, payload["captured_at"])
    return str(path)


def resolve_credentials(
    settings, *, logger: logging.Logger | None = None
) -> Credentials:
    """按 ``HAUT_AUTH_SOURCE`` 决定本次要用的凭据。

    ``browser`` 模式下不返回"没有凭据"的空对象，而是抛
    :class:`CredentialsUnavailableError` —— 调用方据此**阻断提交**并提示重新登录，
    而不是拿着空凭据继续（那会把"确定无凭据"降级成"结果未知"）。
    """
    mode = str(getattr(settings, "auth_source", "auto") or "auto").lower()

    if mode == "env":
        return _from_env(settings)

    snapshot = load_snapshot(settings, logger=logger)

    if mode == "browser":
        if snapshot is None:
            raise CredentialsUnavailableError(
                "HAUT_AUTH_SOURCE=browser 但没有可用的浏览器会话快照；"
                "请先运行 python -m haut_seat auth-login 完成一次人工登录，"
                "或改用 HAUT_AUTH_SOURCE=env。为避免使用过期凭据，本次不会回退到 .env。"
            )
        return _from_snapshot(snapshot)

    # auto：较新的一方胜出
    env_creds = _from_env(settings)
    if snapshot is None:
        return env_creds
    if env_creds.source == "none":
        return _from_snapshot(snapshot)
    return _from_snapshot(snapshot) if _snapshot_is_newer(snapshot, settings) else env_creds


# ---------------------------------------------------------------- 内部工具


def _from_env(settings) -> Credentials:
    from haut_seat.services.io import secret_str

    cookie = secret_str(getattr(settings, "cookie", None))
    authorization = secret_str(getattr(settings, "authorization", None))
    if not cookie and not authorization:
        return Credentials(None, None, "none")
    return Credentials(cookie, authorization, "env")


def _from_snapshot(snapshot: Snapshot) -> Credentials:
    return Credentials(
        cookie=snapshot.cookie,
        authorization=snapshot.authorization,
        source="snapshot",
        captured_at=snapshot.captured_at or None,
    )


def _snapshot_is_newer(snapshot: Snapshot, settings) -> bool:
    """快照是否比 ``.env`` 更新鲜。

    任一时间不可得（``.env`` 不存在/mtime 读不到、``captured_at`` 无法解析）时
    返回 True = 快照优先：这正是引入本功能的意图，且即便选错，
    随后的只读预检也会把失效凭据如实判出来，不会被静默放过。
    """
    env_mtime = env_file_mtime(settings)
    captured_ts = snapshot.captured_ts
    if env_mtime is None or captured_ts is None:
        return True
    return captured_ts >= env_mtime


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _harden_dir(path: Path) -> None:
    """尽力把目录收紧到 0700；失败不影响功能（Windows 上本就基本无效）。"""
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass