"""预约自动重试、座位切换与退避策略。

所有函数为纯逻辑，不含 IO/网络/时间依赖，可离线测试。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from haut_seat.models.booking import BookingOutcome, BookingResult

# 座位冲突关键词（来自服务器错误消息的已观察/可预见的特征；未穷举）。
# 后续拿到 addbooking 的实际座位冲突响应后可补充。
_SEAT_CONFLICT_KW = ("占用", "已预约", "已被", "冲突", "不可预约", "seat is occupied")

# 确定性参数错误（重试不会改变结果，立即停止）
_TIME_EXPIRED_KW = ("预约开始时间必须大于当前时间", "大于当前时间")

DEFAULT_BASE_INTERVAL_S = 5.0
DEFAULT_MAX_INTERVAL_S = 60.0
DEFAULT_MAX_ATTEMPTS_PER_SEAT = 3


def _backoff(
    failure_count: int,
    base: float = DEFAULT_BASE_INTERVAL_S,
    cap: float = DEFAULT_MAX_INTERVAL_S,
) -> float:
    return min(base * (2 ** failure_count), cap)


def _is_seat_conflict(message: str) -> bool:
    return any(kw in message for kw in _SEAT_CONFLICT_KW)


def _is_time_expired(message: str) -> bool:
    """服务器明确返回‘开始时间须大于当前时间’→ 确定性错误，停止不重试。"""
    return any(kw in message for kw in _TIME_EXPIRED_KW)


def parse_not_open_keywords(text: str | None) -> tuple[str, ...]:
    """把 ``HAUT_NOT_OPEN_KEYWORDS``（逗号分隔）解析成关键词元组。

    **默认空 = 该机制关闭。** 我们至今**没有观察到**服务器「未开放」响应的真实
    文案，凭空猜词会把一次**永久性失败**误判成「稍后再试」，等于拿一整天去赌一个
    猜测。因此识别词只能由操作者按实测文案显式配置，代码里一个都不内置。
    """
    return tuple(k.strip() for k in (text or "").split(",") if k.strip())


def is_not_open_yet(message: str, keywords: Iterable[str] = ()) -> bool:
    """这条失败消息是否明确表示「还没到开放时间」（可稍后重试，不算失败）。

    ``keywords`` 为空（默认）时**永远返回 False** —— 不匹配任何猜测词。
    只用于「服务器明确拒绝」的失败：``UNKNOWN`` / 认证失效等结果绝不经此判断，
    它们的处理分别由 ``decide_submit_action`` 与调用方负责。
    """
    text = message or ""
    return any(kw in text for kw in keywords if kw)


@dataclass(frozen=True)
class SubmitAction:
    stop: bool            # True = 停止整个任务
    switch_seat: bool     # True = 当前座位不可用，切换备选
    retry_delay: float | None  # None = 不重试；数值 = 等待该秒数后重试同一座位
    message: str


def decide_submit_action(
    result: BookingResult,
    attempt: int,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS_PER_SEAT,
) -> SubmitAction:
    """给定提交结果和当前座位已尝试次数（1-based），返回唯一后续动作。

    规则（基于已有证据与需求约束）：
    - SUCCESS → 停止。
    - UNKNOWN / AUTH_FAILED / NEEDS_HUMAN → 立即停止（绝不再次提交）。
    - FAILED + 座位冲突标志 → 切换备选。
    - FAILED + 其他 → 未达上限时退避重试同座位；达上限后停止。
    """
    out = result.outcome

    if out is BookingOutcome.SUCCESS:
        return SubmitAction(True, False, None, f"预约成功 bookingid={result.booking_id}")

    if out is BookingOutcome.UNKNOWN:
        return SubmitAction(True, False, None, f"结果不明确，停止：{result.message}")

    if out in (BookingOutcome.AUTH_FAILED, BookingOutcome.NEEDS_HUMAN):
        return SubmitAction(True, False, None, result.message)

    # FAILED
    if _is_time_expired(result.message):
        return SubmitAction(True, False, None, f"预约时间已过期，停止：{result.message}")
    if _is_seat_conflict(result.message):
        return SubmitAction(True, True, None, f"座位冲突，切换备选：{result.message}")

    if attempt < max_attempts:
        delay = _backoff(attempt - 1)
        return SubmitAction(False, False, delay,
                            f"暂时失败，{delay:.0f}s 后重试 (#{attempt})：{result.message}")

    return SubmitAction(True, False, None,
                        f"重试次数耗尽({max_attempts}次)：" + result.message)