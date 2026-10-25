"""一键人工登录的启动器：**只登记 + 起进程**，绝不在请求线程里跑浏览器。

为什么不在这里直接调 :func:`haut_seat.services.auth_browser.capture_login`：

1. ``playwright.sync_api`` **不能**在已有 asyncio 事件循环的线程里运行，
   会直接抛 "It looks like you are using Playwright Sync API inside the asyncio loop"。
   Web 服务的请求线程正是这样一个线程。
2. 一个需要人工操作、可能等 5 分钟的交互式窗口，绝不能挂在 HTTP 请求上 ——
   它会把 uvicorn 的 worker 占住。
3. 子进程另有崩溃隔离：浏览器进程崩了不会带走 Web 服务；Ctrl+C 也不会传播过去。

**已知局限（必须如实告知用户，绝不假装成功）**：若 Web 服务以 Windows 服务 /
会话 0 运行，子进程没有桌面，``headless=False`` 会失败。子进程会把
``login_state="failed"`` 与原因写进元数据，页面据此明确提示改用命令行
``python -m haut_seat auth-login``。

本模块**绝不**调用任何预约接口，也绝不代填账号口令或识别验证码。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from haut_seat.web import repository as repo

#: 登录流程状态（与 ``auth_credential_state.login_state`` 同一套词汇）。
#: 一条完整的链：``idle``（未启动）→ ``launching``（起浏览器）→ ``awaiting_login``
#: （等待用户登录）→ ``verifying``（提取到令牌，正在用业务接口验证凭据）→
#: ``captured``（验证通过并落盘）/ ``failed``（失败，含超时/取消/浏览器关闭）。
#: 终态只有 ``captured`` / ``failed`` 两个，页面据此**必然**停止"加载中"。
LOGIN_IDLE = "idle"
LOGIN_LAUNCHING = "launching"
LOGIN_AWAITING = "awaiting_login"
LOGIN_VERIFYING = "verifying"
LOGIN_CAPTURED = "captured"
LOGIN_FAILED = "failed"

LOGIN_STATE_LABELS: dict[str, str] = {
    LOGIN_IDLE: "未在处理",
    LOGIN_LAUNCHING: "正在启动登录窗口",
    LOGIN_AWAITING: "等待你在浏览器中完成登录",
    LOGIN_VERIFYING: "已提取到令牌，正在用业务接口验证凭据",
    LOGIN_CAPTURED: "已获取并验证凭据",
    LOGIN_FAILED: "登录流程失败",
}

#: 这些状态表示"已经有一个采集在跑"，此时不该再开第二个窗口。
#: ``verifying`` 必须算在内：那时令牌已经出现，正在验证，绝不能另开一个采集
#: 去抢同一份全局快照。
ACTIVE_LOGIN_STATES = (LOGIN_LAUNCHING, LOGIN_AWAITING, LOGIN_VERIFYING)

#: 项目根目录：``services/`` → ``haut_seat/`` → 项目根。
#: 子进程必须以此处为 CWD，因为 ``.env`` 与 ``data/`` 都按 CWD 解析。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: 状态视为"还在进行中"的宽限：登录等待上限 + 一点余量，超过即认为上一轮已死。
_ACTIVE_GRACE_S = 60.0


def login_state_label(state: str) -> str:
    return LOGIN_STATE_LABELS.get(state, state or LOGIN_STATE_LABELS[LOGIN_IDLE])


def login_state_is_stale(settings, stamp: Any, *, now: Callable[[], datetime] | None = None) -> bool:
    """"进行中"的状态是否已经过期（按残留处理）。

    进程被强杀会留下 ``awaiting_login`` 这种永远不变的状态，不设宽限的话，
    用户从此再也点不动那个按钮。
    """
    if not stamp:
        return True  # 连时间都没有，无法证明它还在跑 → 按残留处理
    try:
        moment = datetime.strptime(str(stamp), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True
    age = ((now or datetime.now)() - moment).total_seconds()
    limit = float(settings.auth_login_timeout_s) + _ACTIVE_GRACE_S
    return age > limit


def active_login_state(
    settings, db, *, now: Callable[[], datetime] | None = None,
    logger: logging.Logger | None = None,
) -> str | None:
    """若已有采集在进行中，返回该状态；否则 ``None``。

    抽成模块级函数是为了让**远程浏览器管理器复用同一套单飞判断** —— 两处各写一遍
    "谁在采集"迟早会漂移，而漂移的后果是两个采集同时写同一份全局快照。
    """
    if db is None:
        return None
    state = repo.get_credential_state(db)
    if state.get("login_state") not in ACTIVE_LOGIN_STATES:
        return None
    if login_state_is_stale(settings, state.get("login_updated_at"), now=now):
        (logger or logging.getLogger("haut_seat.auth")).info(
            "上一次登录流程的状态已过期，按残留处理"
        )
        return None
    return str(state["login_state"])


def record_login_state(
    db, *, login_state: str, message: str = "", logger: logging.Logger | None = None
) -> None:
    """把登录流程状态写进元数据表；DB 出问题只记日志，**绝不让采集失败**。

    元数据是"给页面看的进度"，不是采集的目的 —— 采集的目的是那份快照。
    因此这里任何异常都不向上抛。
    """
    if db is None:
        return
    try:
        repo.set_credential_state(db, login_state=login_state, login_message=message)
    except Exception as exc:
        (logger or logging.getLogger("haut_seat.auth")).warning(
            "登录状态落库失败（不影响采集）: %s", type(exc).__name__
        )


def record_credential_source(
    db, *, source: str, snapshot_present: bool, captured_at: str | None,
    fingerprint: str = "", logger: logging.Logger | None = None,
) -> None:
    """把"当前会用哪份凭据"的元数据写进表里（只写标签/时间/指纹）。"""
    if db is None:
        return
    try:
        repo.set_credential_state(
            db,
            source=source,
            snapshot_present=snapshot_present,
            snapshot_captured_at=captured_at,
            fingerprint=fingerprint,
        )
    except Exception as exc:
        (logger or logging.getLogger("haut_seat.auth")).warning(
            "凭据元数据落库失败（不影响采集）: %s", type(exc).__name__
        )


def open_web_db(settings, *, logger: logging.Logger | None = None):
    """打开 Web 数据库（仅供记录元数据）。失败返回 None。

    ``init_schema()`` 是幂等的（只有 ``CREATE TABLE IF NOT EXISTS`` 与
    ``INSERT OR IGNORE``），因此对老库也安全 —— 新表会被就地补建。
    """
    log = logger or logging.getLogger("haut_seat.auth")
    try:
        from haut_seat.web.db import Database

        db = Database(settings.web_db)
        db.init_schema()
        return db
    except Exception as exc:
        log.warning("无法打开 Web 数据库以记录登录状态: %s", type(exc).__name__)
        return None


def _popen_detached(argv: list[str], cwd: str, log_path: Path) -> int:
    """以**脱离**方式启动子进程，返回其 PID。

    - ``stdin=DEVNULL``：子进程绝不能等着读输入。
    - stdout/stderr 落到 ``logs/auth-login.log``：保留 CLI 的提示与报错，便于排查。
    - ``DETACHED_PROCESS``：父进程（uvicorn）退出/重启不带走它，用户还能继续登录。
    - ``CREATE_NEW_PROCESS_GROUP``：Ctrl+C 不会串到浏览器那边。
    - **不用** ``shell=True``，也**不用** ``CREATE_NO_WINDOW`` —— 后者会把
      CLI 的提示一起藏掉，而这里恰恰要用户看得见提示。
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("a", encoding="utf-8")
    try:
        creationflags = 0
        if os.name == "nt":
            creationflags = (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            start_new_session=(os.name != "nt"),
            close_fds=True,
        )
    finally:
        # 句柄已交给子进程；父进程这边必须关掉，否则文件会一直被占着。
        handle.close()
    return int(proc.pid)


class LoginLauncher:
    """一键人工登录：单飞检查 → 落状态 → 起脱离进程。"""

    def __init__(
        self,
        settings,
        db=None,
        logger: logging.Logger | None = None,
        *,
        spawner: Callable[[list[str], str, Path], int] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.logger = logger or logging.getLogger("haut_seat.auth")
        self._spawner = spawner or _popen_detached
        self._now = now or datetime.now

    # ---- 路径 ----

    @property
    def log_path(self) -> Path:
        """子进程日志。与 ``HAUT_LOG_FILE`` 同目录（默认即 ``logs/auth-login.log``）。"""
        return Path(self.settings.log_file).parent / "auth-login.log"

    @property
    def project_root(self) -> Path:
        return _PROJECT_ROOT

    # ---- 单飞 ----

    def active_login_state(self) -> str | None:
        """若已有采集在进行中，返回该状态；否则 None。

        实现已抽到模块级 :func:`active_login_state`（远程浏览器管理器复用同一套
        判断）；这里保留同名方法，调用点与从前逐字一致。
        """
        return active_login_state(self.settings, self.db, now=self._now, logger=self.logger)

    def _is_stale(self, stamp: Any) -> bool:
        return login_state_is_stale(self.settings, stamp, now=self._now)

    # ---- 启动 ----

    def command(self, *, timeout_s: float | None = None) -> list[str]:
        """子进程命令行。用当前解释器，保证用的是同一个 venv。"""
        limit = float(self.settings.auth_login_timeout_s if timeout_s is None else timeout_s)
        return [sys.executable, "-m", "haut_seat", "auth-login", "--timeout", f"{limit:g}"]

    def spawn(self, settings=None, *, timeout_s: float | None = None) -> dict:
        """启动一次采集。返回 ``{"ok": bool, "message": str}``，**不抛异常**。"""
        active = self.active_login_state()
        if active is not None:
            return {
                "ok": False,
                "message": f"已有一个登录窗口在进行中（{login_state_label(active)}）；"
                           "请先在浏览器里完成登录，或等它超时后再试。",
            }

        argv = self.command(timeout_s=timeout_s)
        record_login_state(
            self.db, login_state=LOGIN_LAUNCHING,
            message="正在启动登录窗口…", logger=self.logger,
        )
        try:
            pid = self._spawner(argv, str(self.project_root), self.log_path)
        except Exception as exc:
            self.logger.warning("启动登录子进程失败: %s", type(exc).__name__)
            record_login_state(
                self.db, login_state=LOGIN_FAILED,
                message=f"无法启动登录子进程（{type(exc).__name__}）。"
                        "请改用命令行：python -m haut_seat auth-login",
                logger=self.logger,
            )
            return {
                "ok": False,
                "message": "无法启动登录子进程；请改用命令行 python -m haut_seat auth-login。",
            }

        self.logger.info("已启动人工登录子进程（pid=%s）", pid)
        return {
            "ok": True,
            "message": "已启动登录窗口：请在桌面上弹出的浏览器里完成登录"
                       "（含图形验证码）。完成后本页状态会自动更新。",
        }