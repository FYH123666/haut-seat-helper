"""预约计划 / 运行记录的状态机（纯函数，无 IO、无网络、无时间依赖）。

设计要点（与项目既有安全约束一致）：
- **提交结果未知**（``UNKNOWN``）是与「预约失败」并列的独立状态，
  绝不能在界面或统计中显示为普通失败，也不能被自动重放。
- 「暂停」与「停止」语义不同：
  * `pause` = 不再启动**新的**运行，但已进入提交的调用不被中断（避免打断提交产生未知结果）；
  * `stop`  = 同样不启动新运行，且不再发起**新的提交**；当前正在飞行的 HTTP 提交
    （无法安全取消）仍会走到应答，由运行器据实分类。
- 从 `UNKNOWN` 恢复必须显式确认（``acknowledge_unknown``），否则拒绝，
  防止重启后盲目重复提交。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PlanStatus(str, Enum):
    """计划状态（8 态，界面与调度器共用）。"""

    PENDING = "pending"              # 待执行
    RUNNING = "running"              # 运行中
    PAUSED = "paused"                # 已暂停
    AWAITING_AUTH = "awaiting_auth"  # 等待认证
    SUCCEEDED = "succeeded"          # 预约成功
    FAILED = "failed"                # 预约失败
    UNKNOWN = "unknown"              # 提交结果未知
    STOPPED = "stopped"              # 已停止


class RunStatus(str, Enum):
    """单次运行的结果状态。"""

    RUNNING = "running"              # 运行中
    SUCCEEDED = "succeeded"          # 预约成功
    FAILED = "failed"                # 预约失败
    UNKNOWN = "unknown"              # 提交结果未知（不可自动重放）
    AWAITING_AUTH = "awaiting_auth"  # 等待认证/人工处理
    NO_SEAT = "no_seat"              # 窗口内始终无可预约座位
    STOPPED = "stopped"              # 被停止/暂停
    INTERRUPTED = "interrupted"      # 服务重启时仍在运行（结果未知，需人工确认）


class RunPhase(str, Enum):
    """运行事件所处阶段。"""

    START = "start"
    AUTH_CHECK = "auth_check"
    WAIT = "wait"
    QUERY = "query"
    SELECT = "select"
    SUBMIT = "submit"
    END = "end"
    EXCEPTION = "exception"


class ErrorCategory(str, Enum):
    """错误分类（用于界面展示与统计，不替代原始判定）。"""

    NONE = "none"
    AUTH_INVALID = "auth_invalid"
    AUTH_INDETERMINATE = "auth_indeterminate"
    NEEDS_HUMAN = "needs_human"
    NETWORK = "network"
    SERVER_ERROR = "server_error"
    BUSINESS_ERROR = "business_error"
    TIME_INVALID = "time_invalid"
    TIME_ADJUST_FAILED = "time_adjust_failed"
    # 「还没到开放时间」：可恢复的暂时状态（开放前不提交、只按间隔重查）。
    # 它与 NETWORK / BUSINESS_ERROR 分开，是为了让界面能一眼区分
    # 「到点前白等一会」和「真的出错了」——前者不是故障。
    NOT_OPEN = "not_open"
    NO_SEAT = "no_seat"
    SUBMIT_UNKNOWN = "submit_unknown"
    SUBMIT_EXCEPTION = "submit_exception"
    RETRIES_EXHAUSTED = "retries_exhausted"
    POLICY_BLOCKED = "policy_blocked"
    CONFIG_ERROR = "config_error"
    INTERNAL_ERROR = "internal_error"


class RunStop(str, Enum):
    """运行结束原因（由运行器给出，映射到 RunStatus / 退出码）。"""

    SUCCESS = "success"
    WINDOW_CLOSED = "window_closed"
    AUTH_INVALID = "auth_invalid"
    NEEDS_HUMAN = "needs_human"
    TIME_ADJUST_FAILED = "time_adjust_failed"
    SUBMIT_UNKNOWN = "submit_unknown"
    SUBMIT_EXCEPTION = "submit_exception"
    RETRIES_EXHAUSTED = "retries_exhausted"
    DETERMINISTIC_FAILURE = "deterministic_failure"
    BLOCKED = "blocked"
    CONFIG_ERROR = "config_error"
    STOPPED = "stopped"
    INTERNAL_ERROR = "internal_error"


PLAN_STATUS_LABELS: dict[PlanStatus, str] = {
    PlanStatus.PENDING: "待执行",
    PlanStatus.RUNNING: "运行中",
    PlanStatus.PAUSED: "已暂停",
    PlanStatus.AWAITING_AUTH: "等待认证",
    PlanStatus.SUCCEEDED: "预约成功",
    PlanStatus.FAILED: "预约失败",
    PlanStatus.UNKNOWN: "提交结果未知",
    PlanStatus.STOPPED: "已停止",
}

RUN_STATUS_LABELS: dict[RunStatus, str] = {
    RunStatus.RUNNING: "运行中",
    RunStatus.SUCCEEDED: "预约成功",
    RunStatus.FAILED: "预约失败",
    RunStatus.UNKNOWN: "提交结果未知",
    RunStatus.AWAITING_AUTH: "等待认证",
    RunStatus.NO_SEAT: "无可预约座位",
    RunStatus.STOPPED: "已停止",
    RunStatus.INTERRUPTED: "已中断（结果未知）",
}

ERROR_CATEGORY_LABELS: dict[ErrorCategory, str] = {
    ErrorCategory.NONE: "无错误",
    ErrorCategory.AUTH_INVALID: "认证失效",
    ErrorCategory.AUTH_INDETERMINATE: "登录状态无法确认",
    ErrorCategory.NEEDS_HUMAN: "需人工处理（如验证码）",
    ErrorCategory.NETWORK: "网络异常",
    ErrorCategory.SERVER_ERROR: "服务端错误",
    ErrorCategory.BUSINESS_ERROR: "业务错误",
    ErrorCategory.TIME_INVALID: "时间参数不合法",
    ErrorCategory.TIME_ADJUST_FAILED: "开始时间无法满足服务器要求",
    ErrorCategory.NOT_OPEN: "尚未开放（等待开放后重试）",
    ErrorCategory.NO_SEAT: "无可预约座位",
    ErrorCategory.SUBMIT_UNKNOWN: "提交结果未知",
    ErrorCategory.SUBMIT_EXCEPTION: "提交过程异常",
    ErrorCategory.RETRIES_EXHAUSTED: "重试次数耗尽",
    ErrorCategory.POLICY_BLOCKED: "被安全开关拦截",
    ErrorCategory.CONFIG_ERROR: "配置错误",
    ErrorCategory.INTERNAL_ERROR: "内部错误",
}

# 界面用色：未知/中断类必须与「失败」明显区分，不显示为普通失败。
PLAN_STATUS_TONE: dict[PlanStatus, str] = {
    PlanStatus.PENDING: "secondary",
    PlanStatus.RUNNING: "info",
    PlanStatus.PAUSED: "warning",
    PlanStatus.AWAITING_AUTH: "warning",
    PlanStatus.SUCCEEDED: "success",
    PlanStatus.FAILED: "danger",
    PlanStatus.UNKNOWN: "dark",      # 未知 ≠ 失败
    PlanStatus.STOPPED: "secondary",
}

RUN_STATUS_TONE: dict[RunStatus, str] = {
    RunStatus.RUNNING: "info",
    RunStatus.SUCCEEDED: "success",
    RunStatus.FAILED: "danger",
    RunStatus.UNKNOWN: "dark",
    RunStatus.AWAITING_AUTH: "warning",
    RunStatus.NO_SEAT: "secondary",
    RunStatus.STOPPED: "secondary",
    RunStatus.INTERRUPTED: "dark",
}

# 允许的状态迁移（计划生命周期）。任何未列出的迁移一律拒绝。
_ALLOWED_TRANSITIONS: dict[PlanStatus, frozenset[PlanStatus]] = {
    PlanStatus.PENDING: frozenset({
        PlanStatus.RUNNING, PlanStatus.PAUSED, PlanStatus.STOPPED,
    }),
    PlanStatus.RUNNING: frozenset({
        PlanStatus.PENDING,      # 重复计划：本次结束、下次待执行
        PlanStatus.PAUSED,
        PlanStatus.STOPPED,
        PlanStatus.AWAITING_AUTH,
        PlanStatus.SUCCEEDED,
        PlanStatus.FAILED,
        PlanStatus.UNKNOWN,
    }),
    PlanStatus.PAUSED: frozenset({PlanStatus.PENDING, PlanStatus.STOPPED}),
    PlanStatus.AWAITING_AUTH: frozenset({
        PlanStatus.PENDING, PlanStatus.PAUSED, PlanStatus.STOPPED,
    }),
    PlanStatus.SUCCEEDED: frozenset({PlanStatus.PENDING, PlanStatus.STOPPED}),
    PlanStatus.FAILED: frozenset({PlanStatus.PENDING, PlanStatus.STOPPED}),
    PlanStatus.UNKNOWN: frozenset({PlanStatus.PENDING, PlanStatus.STOPPED}),
    PlanStatus.STOPPED: frozenset({PlanStatus.PENDING}),
}

# 运行结束时，计划应进入的状态（由运行结果决定）。
_STOP_TO_STATUS: dict[RunStop, RunStatus] = {}
_STOP_TO_CATEGORY: dict[RunStop, ErrorCategory] = {}
_STOP_TO_EXIT: dict[RunStop, int] = {}
_PLAN_FOR_RUN_STATUS: dict[RunStatus, PlanStatus] = {}


def _register(
    stop: RunStop,
    status: RunStatus,
    category: ErrorCategory,
    exit_code: int,
    plan: PlanStatus | None,
) -> None:
    _STOP_TO_STATUS[stop] = status
    _STOP_TO_CATEGORY[stop] = category
    _STOP_TO_EXIT[stop] = exit_code
    if plan is not None:
        _PLAN_FOR_RUN_STATUS[status] = plan


# 「提交结果未知」始终保持独立状态，绝不当成失败。
_register(RunStop.SUCCESS, RunStatus.SUCCEEDED, ErrorCategory.NONE, 0, PlanStatus.SUCCEEDED)
_register(RunStop.WINDOW_CLOSED, RunStatus.NO_SEAT, ErrorCategory.NO_SEAT, 0, PlanStatus.FAILED)
_register(RunStop.AUTH_INVALID, RunStatus.AWAITING_AUTH, ErrorCategory.AUTH_INVALID, 3, PlanStatus.AWAITING_AUTH)
_register(RunStop.NEEDS_HUMAN, RunStatus.AWAITING_AUTH, ErrorCategory.NEEDS_HUMAN, 1, PlanStatus.AWAITING_AUTH)
_register(RunStop.TIME_ADJUST_FAILED, RunStatus.FAILED, ErrorCategory.TIME_ADJUST_FAILED, 0, PlanStatus.FAILED)
_register(RunStop.SUBMIT_UNKNOWN, RunStatus.UNKNOWN, ErrorCategory.SUBMIT_UNKNOWN, 0, PlanStatus.UNKNOWN)
_register(RunStop.SUBMIT_EXCEPTION, RunStatus.UNKNOWN, ErrorCategory.SUBMIT_EXCEPTION, 1, PlanStatus.UNKNOWN)
_register(RunStop.RETRIES_EXHAUSTED, RunStatus.FAILED, ErrorCategory.RETRIES_EXHAUSTED, 0, PlanStatus.FAILED)
_register(RunStop.DETERMINISTIC_FAILURE, RunStatus.FAILED, ErrorCategory.BUSINESS_ERROR, 0, PlanStatus.FAILED)
_register(RunStop.BLOCKED, RunStatus.STOPPED, ErrorCategory.POLICY_BLOCKED, 0, None)
_register(RunStop.CONFIG_ERROR, RunStatus.FAILED, ErrorCategory.CONFIG_ERROR, 2, PlanStatus.FAILED)
_register(RunStop.STOPPED, RunStatus.STOPPED, ErrorCategory.NONE, 0, PlanStatus.STOPPED)
_register(RunStop.INTERNAL_ERROR, RunStatus.FAILED, ErrorCategory.INTERNAL_ERROR, 1, PlanStatus.FAILED)


def run_status_for(stop: RunStop) -> RunStatus:
    return _STOP_TO_STATUS[stop]


def error_category_for(stop: RunStop) -> ErrorCategory:
    return _STOP_TO_CATEGORY[stop]


def exit_code_for(stop: RunStop) -> int:
    return _STOP_TO_EXIT[stop]


def plan_status_for(run_status: RunStatus) -> PlanStatus:
    """运行结束后的计划状态；BLOCKED 等不改变计划状态的返回 PENDING。"""
    return _PLAN_FOR_RUN_STATUS.get(run_status, PlanStatus.PENDING)


def can_transition(src: PlanStatus, dst: PlanStatus) -> bool:
    return dst in _ALLOWED_TRANSITIONS.get(src, frozenset())


def assert_transition(src: PlanStatus, dst: PlanStatus) -> None:
    if not can_transition(src, dst):
        raise ValueError(f"非法状态迁移: {src.value} → {dst.value}")


# ------------------------------------------------------------------ 用户动作


@dataclass(frozen=True)
class TransitionResult:
    """用户动作的结果。

    ``deferred=True`` 表示动作已登记，将在当前运行结束后生效
    （正在提交中的调用不会被中途打断）。
    """

    allowed: bool
    reason: str
    status: PlanStatus | None = None
    deferred: bool = False


def apply_pause(status: PlanStatus, *, has_active_run: bool) -> TransitionResult:
    """暂停：不再启动新运行。"""
    if has_active_run or status is PlanStatus.RUNNING:
        return TransitionResult(
            True, "已登记暂停：当前运行结束后不再启动新运行（不中断正在提交的请求）",
            status=PlanStatus.RUNNING, deferred=True,
        )
    if status is PlanStatus.PAUSED:
        return TransitionResult(True, "计划已是暂停状态", status=PlanStatus.PAUSED)
    if can_transition(status, PlanStatus.PAUSED):
        return TransitionResult(True, "已暂停", status=PlanStatus.PAUSED)
    return TransitionResult(False, f"当前状态（{PLAN_STATUS_LABELS[status]}）不可暂停")


def apply_resume(
    status: PlanStatus, *, has_active_run: bool, acknowledge_unknown: bool = False
) -> TransitionResult:
    """恢复：重新排入待执行。UNKNOWN 必须显式确认。"""
    if has_active_run or status is PlanStatus.RUNNING:
        return TransitionResult(False, "计划正在运行中，无需恢复")
    if status is PlanStatus.PENDING:
        return TransitionResult(True, "计划已是待执行状态", status=PlanStatus.PENDING)
    if status is PlanStatus.UNKNOWN and not acknowledge_unknown:
        return TransitionResult(
            False,
            "上一次提交的结果未知，不能直接恢复。请先在图书馆网站确认该时段是否已有预约记录，"
            "确认后再勾选「已核实」恢复执行。",
        )
    if can_transition(status, PlanStatus.PENDING):
        return TransitionResult(True, "已恢复为待执行", status=PlanStatus.PENDING)
    return TransitionResult(False, f"当前状态（{PLAN_STATUS_LABELS[status]}）不可恢复")


def apply_stop(status: PlanStatus, *, has_active_run: bool) -> TransitionResult:
    """停止：不再启动新运行，并不再发起新的提交。"""
    if has_active_run or status is PlanStatus.RUNNING:
        return TransitionResult(
            True, "已登记停止：不再发起新的提交，当前运行结束后停止（不会中断已在飞行的请求）",
            status=PlanStatus.RUNNING, deferred=True,
        )
    if status is PlanStatus.STOPPED:
        return TransitionResult(True, "计划已是停止状态", status=PlanStatus.STOPPED)
    if can_transition(status, PlanStatus.STOPPED):
        return TransitionResult(True, "已停止", status=PlanStatus.STOPPED)
    return TransitionResult(False, f"当前状态（{PLAN_STATUS_LABELS[status]}）不可停止")


def requires_unknown_acknowledgement(status: PlanStatus) -> bool:
    return status is PlanStatus.UNKNOWN


def is_schedulable(status: PlanStatus) -> bool:
    """只有「待执行」才会被调度器拾取。"""
    return status is PlanStatus.PENDING


def blocks_new_submissions(status: PlanStatus) -> bool:
    """这些状态下必须停止一切新提交（等待认证 / 未知 / 暂停 / 停止）。"""
    return status in (
        PlanStatus.AWAITING_AUTH,
        PlanStatus.UNKNOWN,
        PlanStatus.PAUSED,
        PlanStatus.STOPPED,
        PlanStatus.SUCCEEDED,
    )