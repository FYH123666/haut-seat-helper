"""每个用户**自己那一份**学校凭据：加密保存、按用户取出、状态维护。

## 一句话说明它解决的是什么

引入本模块之前，「凭据」是一份全局的 ``.env`` / 快照，调度器每个计划都从同一处取用
⇒ 所有计划实际上都提交在**同一个学校账号**名下。现在每个用户的凭据按 ``user_id``
分开：密文分两列存（``cookie_enc`` / ``authorization_enc``），AAD 里绑定了
``user_id`` 与字段名，因此**一个用户的密文塞进另一个用户的行里必然解不开**
（认证标签会失败）。调度器据此为每个计划构造一份只属于该用户的 settings。

## 三条不可让步的边界

1. **明文只存在于内存**：``load_credentials`` 解出来就交给 HTTP 客户端，
   不缓存到模块级、不写日志、不进异常消息、不进模板上下文；
2. **不静默用别人的**：取不到、解不开、状态失效时**一律抛错**（``NoCredentialError``
   / ``CredentialDecryptError``），绝不回退到 ``.env`` 或快照 —— "回退到某个能用的
   账号"正是多账号场景里最危险的那种"看起来正常"；
3. **平台口令 ≠ 学校口令**：这里只管学校凭据；平台口令在 ``web.user_auth`` 里
   以各自的 scrypt 哈希保存，两者在任何代码路径上都不相交。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from haut_seat.services.credential_crypto import (
    CredentialCryptoError,
    decrypt,
    encrypt,
    master_key,
    user_aad,
)
from haut_seat.web import repository as repo

logger = logging.getLogger("haut_seat.user_credentials")

CRED_STATUS_NONE = "none"
CRED_STATUS_VALID = "valid"
CRED_STATUS_INVALID = "invalid"
CRED_STATUS_INDETERMINATE = "indeterminate"

CRED_STATUS_LABELS = {
    CRED_STATUS_NONE: "未绑定",
    CRED_STATUS_VALID: "有效",
    CRED_STATUS_INVALID: "已失效",
    CRED_STATUS_INDETERMINATE: "待确认",
}

#: 状态色的语义：只有 valid 是"可以提交"。
CRED_STATUS_TONES = {
    CRED_STATUS_NONE: "muted",
    CRED_STATUS_VALID: "ok",
    CRED_STATUS_INVALID: "bad",
    CRED_STATUS_INDETERMINATE: "warn",
}


class NoCredentialError(RuntimeError):
    """该用户还没有绑定学校凭据（或已被本人解绑）。消息不含任何凭据值。"""


class CredentialDecryptError(RuntimeError):
    """密文解不开：主密钥换了、数据库被改过、或这条密文不属于这个用户。"""


def cred_label(status: str) -> str:
    return CRED_STATUS_LABELS.get(str(status or ""), "未绑定")


def cred_tone(status: str) -> str:
    return CRED_STATUS_TONES.get(str(status or ""), "muted")


# ------------------------------------------------------------------ 加解密


def _blob_key(settings) -> bytes:
    return master_key(settings, logger=logger)


def encrypt_field(settings, user_id: int, field: str, value: str) -> str:
    return encrypt(
        _blob_key(settings), str(value), aad=user_aad(user_id, field)
    )


def decrypt_field(settings, user_id: int, field: str, token: str) -> str:
    try:
        return decrypt(_blob_key(settings), str(token), aad=user_aad(user_id, field))
    except CredentialCryptoError as exc:
        # 把细节留在这里（日志里也只有 user_id 与异常类型，没有值），
        # 对外统一成一句"解不开、请重新绑定"。
        logger.warning("用户 %s 的 %s 解密失败：%s", user_id, field, type(exc).__name__)
        raise CredentialDecryptError(
            "已保存的凭据无法解密（加密主密钥可能已更换）。请重新绑定图书馆账号。"
        ) from exc


# ------------------------------------------------------------------ 保存 / 读取


def save_credentials(
    db,
    settings,
    user_id: int,
    *,
    school_username: str,
    cookie: str,
    authorization: str,
    status: str = CRED_STATUS_VALID,
    message: str = "",
    source: str = "manual",
    now: datetime | None = None,
) -> None:
    """加密保存该用户的学校凭据，并记一条认证事件。

    ``cookie`` / ``authorization`` 是**明文入参、密文落库**：函数返回后调用方手里
    的那两个字符串仍是明文，请让它们随请求作用域一起消失（不要塞进 session、
    不要放进缓存、不要写进任何 logger）。
    """
    if not str(cookie or "") or not str(authorization or ""):
        raise ValueError("拒绝保存空的凭据（调用方应在登录失败时就不调用本函数）")
    from haut_seat.services.auth_store import fingerprint

    repo.set_user_credential(
        db,
        user_id,
        school_username=str(school_username or ""),
        cookie_enc=encrypt_field(settings, user_id, "cookie", cookie),
        authorization_enc=encrypt_field(settings, user_id, "authorization", authorization),
        fingerprint=fingerprint(authorization),
        status=status,
        message=message,
        captured_at=None,
        now=now,
    )
    repo.add_user_auth_event(
        db, user_id, status=status, message=message or "已绑定图书馆账号",
        source=source, now=now,
    )


def has_credentials(db, user_id: int) -> bool:
    row = repo.get_user_credential(db, user_id)
    return bool(row and row.get("cookie_enc") and row.get("authorization_enc"))


def load_credentials(db, settings, user_id: int):
    """解密并返回该用户的凭据（``auth_store.Credentials``）。

    任何"取不到"的情况都抛错，**绝不返回 None 让调用方自己去回退**：
    回退的下一步必然是"用别的账号提交"，那正是要防的事。
    """
    from haut_seat.services.auth_store import Credentials

    row = repo.get_user_credential(db, user_id)
    if row is None or not row.get("cookie_enc") or not row.get("authorization_enc"):
        raise NoCredentialError("尚未绑定图书馆账号")
    cookie = decrypt_field(settings, user_id, "cookie", row["cookie_enc"])
    authorization = decrypt_field(settings, user_id, "authorization", row["authorization_enc"])
    return Credentials(
        cookie=cookie,
        authorization=authorization,
        source="user",
        captured_at=row.get("captured_at"),
    )


def clear_credentials(db, settings, user_id: int, *, now: datetime | None = None) -> bool:
    """解绑：删除密文本身（不是只把状态标成失效）。"""
    removed = repo.clear_user_credential(db, user_id)
    if removed:
        repo.add_user_auth_event(
            db, user_id, status=CRED_STATUS_NONE, message="已解绑图书馆账号",
            source="manual", now=now,
        )
    return removed


def mark_status(
    db, user_id: int, status: str, message: str = "", *, source: str = "manual",
    now: datetime | None = None,
) -> None:
    """只改状态（保留密文），并记一条事件。"""
    repo.set_user_credential_status(db, user_id, status=status, message=message, now=now)
    repo.add_user_auth_event(
        db, user_id, status=status, message=message, source=source, now=now
    )


def credentials_status(db, user_id: int) -> str:
    view = repo.get_user_credential_view(db, user_id)
    if not has_credentials(db, user_id):
        return CRED_STATUS_NONE
    return str(view.get("status") or CRED_STATUS_NONE)


def status_view(db, user_id: int) -> dict:
    """给页面用的状态视图：**只有元数据**（``repo.get_user_credential_view``
    里根本不存在密文字段），加上展示用的标签与色调。"""
    view = repo.get_user_credential_view(db, user_id)
    if not has_credentials(db, user_id):
        view["status"] = CRED_STATUS_NONE
    view["status_label"] = cred_label(view.get("status"))
    view["status_tone"] = cred_tone(view.get("status"))
    view["bound"] = has_credentials(db, user_id)
    return view


# ------------------------------------------------------------------ 每用户 settings


def settings_with_credentials(settings, credentials):
    """把一份凭据装进 settings 的副本，交给预约运行器用。

    做法与 CLI 手工注入凭据时**逐字一致**（``__main__.py`` 里那处 ``model_copy``），
    因此预约核心与并发模块一行都不用改：它们照旧只认 ``settings``。

    ``auth_source`` 被固定成 ``"env"`` 是有意的：解析层在这种情况下只会读
    settings 自身的 ``cookie`` / ``authorization`` 字段（也就是我们刚塞进去的这两个），
    不会再去碰快照文件或 ``.env`` —— 于是"这份运行的凭据到底是谁的"不再有任何歧义。
    """
    from pydantic import SecretStr

    return settings.model_copy(
        update={
            "auth_source": "env",
            "cookie": SecretStr(credentials.cookie) if credentials.cookie else None,
            "authorization": (
                SecretStr(credentials.authorization) if credentials.authorization else None
            ),
        }
    )


def settings_for_user(db, settings, user_id: int):
    """按用户取出凭据并返回 ``(settings 副本, Credentials)``。

    这是调度器唯一的取用入口。它可能抛 :class:`NoCredentialError` /
    :class:`CredentialDecryptError` —— 调用方必须把这两种情况变成"该用户的任务
    暂停并提示重新登录"，而不是继续跑。
    """
    creds = load_credentials(db, settings, user_id)
    return settings_with_credentials(settings, creds), creds


def verify_credentials(settings, credentials, *, regionid=None):
    """用该用户的凭据做一次**只读**探测，确认它真的能用。

    返回 ``AuthCheck``（三态）。调用方必须如实区分：只有 ``is_valid`` 才是"能用"，
    ``indeterminate``（网络不通等）**不能**当成有效，也**不能**当成失效。

    探测本身走既有的预检实现，因此与预约路径用的是同一套判定，不存在
    "验证时是好的、跑起来不认"的两套标准。
    """
    from haut_seat.services.io import auth_precheck

    target = regionid if regionid is not None else getattr(settings, "regionid", 16)
    return auth_precheck(
        settings_with_credentials(settings, credentials), target, None, None
    )


def resume_owned_plans(db, user_id: int) -> int:
    """用户重新绑定成功后，把他那些「等待认证」的计划放回「待执行」。

    只动**他本人**的、状态恰好是 ``AWAITING_AUTH`` 且仍处于启用状态的计划。

    这不是"绕过闸门"：放回待执行之后，计划仍要逐一通过调度器原有的全部判据
    （今天是否该执行、窗口是否已过、今天是否已经提交过、时段锁是否空闲…），
    因此一个昨天就该跑的计划不会因为这次解冻在今天被补跑出一次意外提交。
    """
    from haut_seat.services.state import PlanStatus

    resumed = 0
    for plan in repo.list_plans_for_user(db, user_id):
        if plan["status"] is not PlanStatus.AWAITING_AUTH or not int(plan["enabled"]):
            continue
        repo.set_plan_status(db, plan["id"], PlanStatus.PENDING)
        resumed += 1
    if resumed:
        logger.info("用户 %s 重新绑定凭据后，%d 个计划已放回待执行", user_id, resumed)
    return resumed


# ------------------------------------------------------------------ 绑定尝试限流
#
# 绑定 = 拿着**用户本人输入的**口令与验证码去请求学校的登录接口。这条路径必须限流，
# 理由是它可能被当成"登录尝试的转发器"：不限次数的话，一个拿到平台账号的人可以借
# 本服务反复试学校口令（每次都要新验证码，但验证码可以由服务端代取），
# 那正是"不要对学校站点暴力尝试"要防的事。
#
# 限流按 user_id 计（不是按 IP）：同一个人换 IP 不该绕过去。计数落在
# ``user_auth_event`` 上（source='bind_attempt'），不新增表。

BIND_MAX_ATTEMPTS = 12
BIND_WINDOW_MINUTES = 10
BIND_LOCK_MINUTES = 10


def record_bind_attempt(db, user_id: int, *, now: datetime | None = None) -> None:
    """记一次**真实的**绑定尝试（本地字段没填全的不算，因为它没发出任何请求）。"""
    repo.add_user_auth_event(
        db, user_id, status="attempt", message="", source=BIND_ATTEMPT_SOURCE, now=now
    )


BIND_ATTEMPT_SOURCE = "bind_attempt"


def bind_locked_until(db, user_id: int, *, now: datetime | None = None) -> datetime | None:
    """是否已被限流；返回解锁时刻或 ``None``。"""
    moment = now or datetime.now()
    since = (moment - timedelta(minutes=BIND_WINDOW_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    row = db.query_one(
        "SELECT COUNT(*) AS n, MAX(at) AS last FROM user_auth_event"
        " WHERE user_id = ? AND source = ? AND at >= ?",
        (int(user_id), BIND_ATTEMPT_SOURCE, since),
    )
    count = int(row["n"]) if row and row["n"] else 0
    if count < BIND_MAX_ATTEMPTS:
        return None

    try:
        last = datetime.strptime(str(row["last"]), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return moment  # 时间读不出来时保守起见按"仍被限制"处理（只影响 10 分钟）
    unlock = last + timedelta(minutes=BIND_LOCK_MINUTES)
    return unlock if unlock > moment else None