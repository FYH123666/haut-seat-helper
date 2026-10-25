"""认证状态服务：三态判定 + 恢复流程 + 凭据来源展示。

本轮实现：
- 复用既有的只读预检 ``services.io.auth_precheck`` 刷新认证状态；
- 记录认证状态与事件，供「认证状态」页展示；
- 提供恢复流程的状态机与人工步骤文案；
- 展示**凭据来源**（.env 手动配置 / 浏览器会话快照 / 未配置），并在
  ``HAUT_AUTH_SOURCE=browser`` 却没有可用快照时**硬阻断**一切新提交。

本轮**不实现**（明确标注为未实现，界面上如实显示）：
- 不调用学校登录接口、不猜测参数、不代填账号口令、不识别或绕过验证码；
- 不做自动续期（既有实测结论：令牌无 exp、无 refresh 接口，故无可靠续期机制）。

凭据来源由 ``services.auth_store.resolve_credentials`` 决定，本服务只是它的
**消费者**：注入 ``credential_provider`` 后，判定与展示都基于"真正会被用到的那份凭据"，
而不是"文件存在与否"。这里出现的任何文案都只含来源标签、时间与单向指纹，绝不含凭据值。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from haut_seat.core.auth import RELOGIN_HINT, AuthCheck, AuthStatus
from haut_seat.services.auth_login_service import login_state_label
from haut_seat.services.auth_store import (
    Credentials,
    CredentialsUnavailableError,
    resolve_credentials,
)
from haut_seat.web import repository as repo


class RecoveryState(str, Enum):
    """恢复流程状态机。"""

    IDLE = "idle"                      # 未在处理
    AWAITING_HUMAN = "awaiting_human"  # 已提示人工处理（正常浏览器登录 + 更新凭据）
    VERIFYING = "verifying"            # 正在重新预检
    RECOVERED = "recovered"            # 恢复成功（预检 valid）
    FAILED = "failed"                  # 恢复失败（预检仍 invalid）


RECOVERY_STATE_LABELS: dict[RecoveryState, str] = {
    RecoveryState.IDLE: "未在处理",
    RecoveryState.AWAITING_HUMAN: "等待人工完成登录",
    RecoveryState.VERIFYING: "正在验证新凭据",
    RecoveryState.RECOVERED: "恢复成功",
    RecoveryState.FAILED: "恢复失败",
}

AUTH_STATUS_LABELS: dict[str, str] = {
    AuthStatus.VALID.value: "登录有效",
    AuthStatus.INVALID.value: "登录已失效",
    AuthStatus.INDETERMINATE.value: "无法确认（非失效判定）",
}

# 人工恢复步骤：只描述「在正常浏览器里由本人完成」，不含任何绕过手段。
RECOVERY_STEPS: tuple[str, ...] = (
    "1. 在浏览器中打开图书馆网站 https://wslib.haut.edu.cn/ 并正常登录"
    "（如出现验证码，由你本人完成识别）。",
    "2. 按 F12 打开开发者工具 → Network，任选一个 /stage-api/ 开头的请求。",
    "3. 复制该请求的 Authorization 请求头完整值，以及 Cookie 中的 Admin-Token= 那一段。",
    "4. 在服务器上编辑 .env，更新 HAUT_AUTHORIZATION 与 HAUT_COOKIE。",
    "5. 保存后重启本服务（systemctl restart haut-seat-web），回到本页点击「重新检测登录状态」。",
    "6. 检测为「登录有效」后，到计划页恢复需要重跑的计划。",
)


@dataclass(frozen=True)
class AuthStatusView:
    status: str
    status_label: str
    recovery_state: str
    recovery_label: str
    message: str
    checked_at: str | None
    seat_count: int | None = None
    http_status: int | None = None
    business_code: Any = None
    blocks_submissions: bool = False
    auto_renew_supported: bool = False
    auto_login_supported: bool = False
    # ---- 凭据来源（只含标签/时间/单向指纹，绝不含值）----
    credential_source: str = "none"                 # snapshot | env | none
    credential_source_label: str = "未配置"
    snapshot_present: bool = False
    snapshot_captured_at: str | None = None
    snapshot_fingerprint: str = ""
    env_configured: bool = False
    credential_blocked_reason: str = ""
    # ---- 浏览器辅助的人工登录（第五阶段接入）----
    browser_automation_supported: bool = False
    browser_unavailable_reason: str = ""
    login_state: str = "idle"
    login_state_label: str = "未在处理"
    login_message: str = ""


class AuthService:
    """认证状态与恢复流程的服务层入口。"""

    def __init__(
        self,
        db,
        settings,
        logger: logging.Logger | None = None,
        *,
        check_fn: Callable[..., AuthCheck] | None = None,
        credential_provider: Callable[..., Credentials] | None = None,
    ):
        self.db = db
        self.settings = settings
        self.logger = logger or logging.getLogger("haut_seat.auth")
        self._check_fn = check_fn  # 测试注入；默认延迟导入真实只读预检
        self._credential_provider = credential_provider or resolve_credentials

    # ---- 凭据来源 ----

    def _resolve(self) -> Credentials:
        """按配置解析本次要用的凭据。``browser`` 模式缺快照时抛异常。"""
        return self._credential_provider(self.settings)

    def _resolve_quietly(self) -> tuple[Credentials | None, Exception | None]:
        """一次解析，同时给出"凭据"与"不可用原因"。不抛异常。

        调度器每个 tick 每个计划都会问一次，所以只解析一次、不重复读文件。
        """
        try:
            return self._resolve(), None
        except CredentialsUnavailableError as exc:
            return None, exc
        except Exception as exc:
            self.logger.warning("凭据解析异常: %s", type(exc).__name__)
            return None, exc

    def resolved_credentials(self) -> Credentials | None:
        """解析凭据；不可用时返回 None（供展示层使用，不抛异常）。"""
        return self._resolve_quietly()[0]

    def credentials_unavailable_reason(self) -> str:
        """凭据来源层的明确原因；凭据可用时返回空串。

        ``CredentialsUnavailableError`` 的文案是本模块精写的（含补救命令），可原样展示；
        其它异常**只展示类型名**，绝不回显任意异常的原始消息 ——
        那是一条来源不可控的字符串，正是最可能夹带凭据值的地方。
        """
        _, exc = self._resolve_quietly()
        if exc is None:
            return ""
        if isinstance(exc, CredentialsUnavailableError):
            return str(exc)
        return f"{type(exc).__name__}：凭据解析异常（非凭据不可用，不阻断）"

    # ---- 只读预检 ----

    def _precheck(self, *, start: str | None = None, end: str | None = None) -> AuthCheck:
        fn = self._check_fn
        if fn is None:
            from haut_seat.services.io import auth_precheck as fn  # noqa: PLW2901
        return fn(self.settings, self.settings.regionid, start, end)

    def check_now(self, *, source: str = "manual") -> AuthCheck:
        """执行一次只读认证预检并落库。**绝不提交预约**。

        这里先清掉预检结果缓存：``check_now`` 只由人点出来（面板的「重新检测登录状态」、
        「我已更新凭据，重新验证」），人对"重新检测"的期待就是**真的去探测一次**。
        ``HAUT_AUTH_CHECK_TTL_S`` 的复用只服务于调度器与运行器那些无人值守的重复预检。
        """
        try:
            from haut_seat.services.io import clear_precheck_cache

            clear_precheck_cache()
        except Exception as exc:  # 缓存清理失败绝不能连累预检本身
            self.logger.warning("清理预检缓存失败: %s", type(exc).__name__)
        try:
            check = self._precheck()
        except Exception as exc:  # 预检本身异常 → 不确定，绝不误判为失效
            self.logger.warning("认证预检异常: %s", type(exc).__name__)
            check = AuthCheck(AuthStatus.INDETERMINATE, f"预检异常（非认证失效）：{type(exc).__name__}")
        recovery = None
        if check.is_valid:
            current = repo.get_auth_state(self.db).get("recovery_state")
            if current in (RecoveryState.VERIFYING.value, RecoveryState.AWAITING_HUMAN.value):
                recovery = RecoveryState.RECOVERED.value
        repo.set_auth_state(
            self.db, status=check.status.value, message=check.message,
            recovery_state=recovery, source=source,
        )
        return check

    def current(self) -> AuthStatusView:
        state = repo.get_auth_state(self.db)
        status = state.get("status") or AuthStatus.INDETERMINATE.value
        recovery = state.get("recovery_state") or RecoveryState.IDLE.value
        creds, blocked_exc = self._resolve_quietly()
        login = repo.get_credential_state(self.db)
        available, unavailable_reason = self._browser_availability()
        login_state = str(login.get("login_state") or "idle")
        return AuthStatusView(
            status=status,
            status_label=AUTH_STATUS_LABELS.get(status, status),
            recovery_state=recovery,
            recovery_label=RECOVERY_STATE_LABELS.get(
                RecoveryState(recovery) if recovery in RecoveryState._value2member_map_ else RecoveryState.IDLE,
                recovery,
            ),
            message=state.get("message") or "",
            checked_at=state.get("checked_at"),
            # 只有明确确认「失效」才阻断新提交；不确定绝不阻断（也绝不误放开）
            blocks_submissions=self.blocks_submissions(),
            auto_renew_supported=False,
            auto_login_supported=False,
            credential_source=(creds.source if creds is not None else "none"),
            credential_source_label=(
                creds.source_label if creds is not None else "不可用"
            ),
            snapshot_present=bool(creds is not None and creds.source == "snapshot"),
            snapshot_captured_at=(creds.captured_at if creds is not None else None),
            snapshot_fingerprint=(creds.fingerprint() if creds is not None else ""),
            env_configured=self._env_configured(),
            credential_blocked_reason=(
                str(blocked_exc) if isinstance(blocked_exc, CredentialsUnavailableError) else ""
            ),
            # ---- 浏览器辅助的人工登录 ----
            # 含义是"提供一键**人工**登录"，不是"自动登录" —— 因此
            # browser_automation_supported 这个诚实的名字替换了 auto_login_supported。
            browser_automation_supported=available,
            browser_unavailable_reason=unavailable_reason,
            login_state=login_state,
            login_state_label=login_state_label(login_state),
            login_message=str(login.get("login_message") or ""),
        )

    def _browser_availability(self) -> tuple[bool, str]:
        """浏览器可用性。延迟导入：没装 playwright 时 Web 面板照常工作。"""
        try:
            from haut_seat.services.auth_browser import browser_availability

            return browser_availability()
        except Exception as exc:
            self.logger.warning("浏览器可用性探测失败: %s", type(exc).__name__)
            return False, f"无法判断浏览器可用性（{type(exc).__name__}）"

    def _env_configured(self) -> bool:
        """``.env`` 里是否配了凭据（只看有没有，绝不取值展示）。"""
        from haut_seat.services.io import secret_str

        return bool(
            secret_str(getattr(self.settings, "cookie", None))
            or secret_str(getattr(self.settings, "authorization", None))
        )

    def credentials_unavailable(self) -> bool:
        """凭据来源层是否**确定**没有可用凭据（仅 browser 模式缺/坏快照时为真）。

        与"认证失效"区分开：一个是"根本没有凭据可用"，一个是"有凭据但服务器不认"。
        两者都必须在提交前拦住，但它们给用户的补救文案不同。
        """
        _, exc = self._resolve_quietly()
        return isinstance(exc, CredentialsUnavailableError)

    def blocks_submissions(self) -> bool:
        """认证失效时暂停一切新提交（等待人工恢复）。"""
        state = repo.get_auth_state(self.db)
        if state.get("status") == AuthStatus.INVALID.value:
            return True
        if state.get("recovery_state") in (
            RecoveryState.AWAITING_HUMAN.value, RecoveryState.FAILED.value,
        ):
            return True
        # browser 模式下确定没有可用凭据 → 同样在提交前拦住。
        # 这不是新增闸门，而是把运行器稍后必然会得出的同一个结论提前到
        # 调度器这一层，避免白跑一次注定失败的任务。其它异常一概不改变既有结论。
        return self.credentials_unavailable()

    # ---- 恢复流程（本轮只做状态流转与提示，不自动登录）----

    def begin_recovery(self) -> dict:
        """进入「等待人工完成登录」：仅记录状态并给出步骤，不做任何自动登录尝试。"""
        repo.set_auth_state(
            self.db,
            status=repo.get_auth_state(self.db).get("status") or AuthStatus.INDETERMINATE.value,
            message="已进入人工恢复流程：请在正常浏览器登录后更新凭据，再点击「重新检测登录状态」。",
            recovery_state=RecoveryState.AWAITING_HUMAN.value,
            source="manual",
        )
        return {"ok": True, "message": "已进入人工恢复流程", "steps": list(RECOVERY_STEPS)}

    def verify_recovery(self, *, source: str = "manual") -> dict:
        """人工更新凭据后重新预检，据此判定恢复成功/失败。"""
        repo.set_auth_state(
            self.db,
            status=repo.get_auth_state(self.db).get("status") or AuthStatus.INDETERMINATE.value,
            message="正在验证新凭据…",
            recovery_state=RecoveryState.VERIFYING.value,
            source=source,
        )
        check = self.check_now(source=source)
        if check.is_valid:
            repo.set_auth_state(
                self.db, status=check.status.value, message=check.message,
                recovery_state=RecoveryState.RECOVERED.value, source=source,
            )
            return {"ok": True, "status": check.status.value, "message": "恢复成功：凭据有效"}
        if check.is_invalid:
            repo.set_auth_state(
                self.db, status=check.status.value, message=check.message,
                recovery_state=RecoveryState.FAILED.value, source=source,
            )
            return {
                "ok": False, "status": check.status.value,
                "message": "恢复失败：凭据仍然无效，请重新按步骤更新",
                "steps": list(RECOVERY_STEPS),
            }
        # 不确定既不算成功也不算失败：保持等待人工状态，绝不因此重开提交
        repo.set_auth_state(
            self.db, status=check.status.value, message=check.message,
            recovery_state=RecoveryState.AWAITING_HUMAN.value, source=source,
        )
        return {
            "ok": False, "status": check.status.value,
            "message": f"仍无法确认登录状态（{check.message}）——这不是失效判定，"
            "恢复流程保持等待；请检查网络后重试。",
        }

    @staticmethod
    def relogin_hint() -> str:
        return RELOGIN_HINT

    @staticmethod
    def recovery_steps() -> list[str]:
        return list(RECOVERY_STEPS)