"""通知服务：**接口与设置模型预留**，本轮不实现 SMTP 发送。

明确边界（避免把「预留」当成「可用」）：
- 本模块不导入 ``smtplib``，不会发出任何邮件；
- ``_deliver`` 一律抛 ``NotImplementedError``，调用方把它记成
  ``status='skipped'`` 并写明原因，界面上如实显示「未实现」；
- 邮件口令**不入库**：设置里只保存「读取口令的环境变量名」；
- 通知失败**绝不影响**预约任务：所有异常都在本模块内部吞掉并记录。

已预留的事件：预约成功、认证失效、认证恢复、无可预约座位、
任务异常、提交结果未知、服务异常。预留能力：事件去重、发送重试、安全日志。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from haut_seat.web import repository as repo


class NotifyEvent(str, Enum):
    BOOKING_SUCCESS = "booking_success"        # 预约成功
    AUTH_FAILURE = "auth_failure"              # 认证失效
    AUTH_RECOVERY = "auth_recovery"            # 认证恢复
    NO_SEAT = "no_seat"                        # 无可预约座位
    TASK_EXCEPTION = "task_exception"          # 任务异常
    SUBMIT_UNKNOWN = "submit_unknown"          # 提交结果未知
    SERVICE_EXCEPTION = "service_exception"    # 服务异常


NOTIFY_EVENT_LABELS: dict[NotifyEvent, str] = {
    NotifyEvent.BOOKING_SUCCESS: "预约成功",
    NotifyEvent.AUTH_FAILURE: "认证失效",
    NotifyEvent.AUTH_RECOVERY: "认证恢复",
    NotifyEvent.NO_SEAT: "无可预约座位",
    NotifyEvent.TASK_EXCEPTION: "任务异常",
    NotifyEvent.SUBMIT_UNKNOWN: "提交结果未知",
    NotifyEvent.SERVICE_EXCEPTION: "服务异常",
}

DEFAULT_DEDUP_WINDOW_MINUTES = 30
DEFAULT_MAX_ATTEMPTS = 3

# 默认开启的事件（其余需在设置页勾选）
DEFAULT_EVENT_FLAGS: dict[str, bool] = {
    NotifyEvent.BOOKING_SUCCESS.value: True,
    NotifyEvent.AUTH_FAILURE.value: True,
    NotifyEvent.AUTH_RECOVERY.value: True,
    NotifyEvent.NO_SEAT.value: False,
    NotifyEvent.TASK_EXCEPTION.value: True,
    NotifyEvent.SUBMIT_UNKNOWN.value: True,
    NotifyEvent.SERVICE_EXCEPTION.value: True,
}

SMTP_NOT_IMPLEMENTED_REASON = "SMTP 发送尚未实现（本轮仅预留接口与设置模型）"


@dataclass(frozen=True)
class NotifyRequest:
    event: NotifyEvent
    subject: str
    body: str
    plan_id: int | None = None
    run_id: int | None = None
    dedup_key: str = ""


def build_request(
    event: NotifyEvent,
    *,
    plan_id: int | None = None,
    run_id: int | None = None,
    detail: str = "",
    dedup_key: str = "",
) -> NotifyRequest:
    """构造通知请求。正文只使用脱敏后的说明，不含凭据与个人信息。"""
    label = NOTIFY_EVENT_LABELS.get(event, event.value)
    subject = f"[图书馆座位助手] {label}"
    body = f"{label}\n{repo.redact_message(detail)}".strip()
    return NotifyRequest(
        event=event,
        subject=subject,
        body=body,
        plan_id=plan_id,
        run_id=run_id,
        dedup_key=dedup_key or f"{event.value}:{plan_id or '-'}:{run_id or '-'}",
    )


def _safe_warning(logger, message: str, *args) -> None:
    """连日志本身出问题也不能把异常抛出去。

    ``notify`` 的契约是「绝不抛异常」，而兜底分支里写日志同样是可能失败的
    （磁盘满、日志系统被替换等），因此这里再包一层。
    """
    try:
        logger.warning(message, *args)
    except Exception:  # pragma: no cover - 仅日志系统自身失效时触发
        pass


class NotificationService:
    """通知调度器。所有公开方法都不会抛异常。"""

    def __init__(self, db, logger: logging.Logger | None = None, *, clock=None):
        self.db = db
        self.logger = logger or logging.getLogger("haut_seat.notify")
        self._clock = clock or datetime.now

    # ---- 对外接口 ----

    def notify(self, request: NotifyRequest) -> dict:
        """登记并尝试发送一条通知。返回结果字典（绝不抛异常）。"""
        try:
            setting = repo.get_notify_setting(self.db)
            if not setting or not setting.get("enabled"):
                return self._record(request, status="skipped", error="通知功能未启用")
            flags = setting.get("event_flags") or {}
            enabled_flags = {**DEFAULT_EVENT_FLAGS, **(flags if isinstance(flags, dict) else {})}
            if not enabled_flags.get(request.event.value, False):
                return self._record(request, status="skipped", error="该事件未在设置中启用")
            if self._is_duplicate(request):
                return self._record(request, status="skipped", error="重复事件（去重窗口内已通知）")
            return self._send_with_retry(request, setting)
        except Exception as exc:  # 通知永远不能影响预约任务
            _safe_warning(self.logger, "通知流程异常（已忽略）: %s", type(exc).__name__)
            return {"status": "skipped", "error": f"通知流程异常: {type(exc).__name__}"}

    def _is_duplicate(self, request: NotifyRequest) -> bool:
        if not request.dedup_key:
            return False
        since = (self._clock() - timedelta(minutes=DEFAULT_DEDUP_WINDOW_MINUTES)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        row = self.db.query_one(
            "SELECT 1 AS x FROM notify_event WHERE dedup_key = ? AND at >= ? AND status = ? LIMIT 1",
            (request.dedup_key, since, "sent"),
        )
        return row is not None

    def _send_with_retry(self, request: NotifyRequest, setting: dict) -> dict:
        last_error = ""
        for attempt in range(1, DEFAULT_MAX_ATTEMPTS + 1):
            try:
                self._deliver(request, setting)
                return self._record(request, status="sent", error="", attempts=attempt)
            except NotImplementedError as exc:
                # 预留接口：如实记录「未实现」，绝不假装已发送
                last_error = str(exc) or SMTP_NOT_IMPLEMENTED_REASON
                return self._record(request, status="skipped", error=last_error, attempts=attempt)
            except Exception as exc:
                last_error = repo.redact_message(f"{type(exc).__name__}: {exc}")
                if self.logger:
                    self.logger.warning("通知发送失败（第 %d 次）: %s", attempt, last_error)
        return self._record(request, status="failed", error=last_error,
                           attempts=DEFAULT_MAX_ATTEMPTS)

    def _deliver(self, request: NotifyRequest, setting: dict) -> None:
        """真正发送通知。

        **本轮未实现**：SMTP 接入属于后续阶段，且需要人工验证连通性。
        真实实现应从 ``setting['smtp_password_env']`` 指定的环境变量读取口令，
        绝不把口令写入数据库、日志或页面。
        """
        raise NotImplementedError(SMTP_NOT_IMPLEMENTED_REASON)

    def _record(
        self, request: NotifyRequest, *, status: str, error: str = "", attempts: int = 0
    ) -> dict:
        try:
            now = self._clock().strftime("%Y-%m-%d %H:%M:%S")
            with self.db.tx() as conn:
                conn.execute(
                    "INSERT INTO notify_event (at, event, plan_id, run_id, dedup_key,"
                    " message, status, attempts, last_error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        now, request.event.value, request.plan_id, request.run_id,
                        request.dedup_key, repo.redact_message(request.body),
                        status, attempts, repo.redact_message(error),
                    ),
                )
            return {"status": status, "error": error, "event": request.event.value}
        except Exception as exc:
            self.logger.warning("通知记录写入失败（已忽略）: %s", type(exc).__name__)
            return {"status": "skipped", "error": "通知记录写入失败", "event": request.event.value}

    # ---- 便捷方法（供调度器调用）----

    def notify_booking_success(self, *, plan_id: int, run_id: int, detail: str = "") -> dict:
        return self.notify(build_request(
            NotifyEvent.BOOKING_SUCCESS, plan_id=plan_id, run_id=run_id, detail=detail,
            dedup_key=f"booking_success:{run_id}",
        ))

    def notify_auth_failure(self, *, plan_id: int | None = None, run_id: int | None = None, detail: str = "") -> dict:
        return self.notify(build_request(
            NotifyEvent.AUTH_FAILURE, plan_id=plan_id, run_id=run_id, detail=detail,
            dedup_key=f"auth_failure:{plan_id or '-'}",
        ))

    def notify_auth_recovery(self, *, detail: str = "") -> dict:
        return self.notify(build_request(
            NotifyEvent.AUTH_RECOVERY, detail=detail, dedup_key="auth_recovery",
        ))

    def notify_no_seat(self, *, plan_id: int, run_id: int, detail: str = "") -> dict:
        return self.notify(build_request(
            NotifyEvent.NO_SEAT, plan_id=plan_id, run_id=run_id, detail=detail,
            dedup_key=f"no_seat:{plan_id}:{run_id}",
        ))

    def notify_task_exception(self, *, plan_id: int | None = None, run_id: int | None = None, detail: str = "") -> dict:
        return self.notify(build_request(
            NotifyEvent.TASK_EXCEPTION, plan_id=plan_id, run_id=run_id, detail=detail,
            dedup_key=f"task_exception:{run_id or plan_id or '-'}",
        ))

    def notify_submit_unknown(self, *, plan_id: int, run_id: int, detail: str = "") -> dict:
        return self.notify(build_request(
            NotifyEvent.SUBMIT_UNKNOWN, plan_id=plan_id, run_id=run_id, detail=detail,
            dedup_key=f"submit_unknown:{run_id}",
        ))

    def notify_service_exception(self, *, detail: str = "") -> dict:
        return self.notify(build_request(
            NotifyEvent.SERVICE_EXCEPTION, detail=detail, dedup_key="service_exception",
        ))