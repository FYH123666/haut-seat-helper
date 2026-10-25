"""预约提交后的决策逻辑：成功停止、冲突切换、暂时重试、结果不明立即停止。"""

from __future__ import annotations

from haut_seat.core.booking import decide_submit_action
from haut_seat.models.booking import BookingOutcome, BookingResult


def _make_result(outcome, message="", booking_id=None):
    return BookingResult(outcome, message, booking_id)


def test_success_stops():
    r = _make_result(BookingOutcome.SUCCESS, booking_id=999)
    a = decide_submit_action(r, 1)
    assert a.stop is True
    assert a.switch_seat is False


def test_unknown_stops_no_retry():
    a = decide_submit_action(_make_result(BookingOutcome.UNKNOWN), 1)
    assert a.stop is True
    assert a.switch_seat is False


def test_auth_failed_stops():
    a = decide_submit_action(_make_result(BookingOutcome.AUTH_FAILED, "重定向"), 1)
    assert a.stop is True


def test_needs_human_stops():
    a = decide_submit_action(_make_result(BookingOutcome.NEEDS_HUMAN), 1)
    assert a.stop is True


def test_seat_conflict_switches():
    for kw in ("占用", "已预约", "已被预约", "不可预约", "冲突"):
        a = decide_submit_action(
            _make_result(BookingOutcome.FAILED, f"座位{kw}"),
            1,
        )
        assert a.switch_seat is True, kw
        assert a.stop is True


def test_transient_failure_retries():
    a = decide_submit_action(
        _make_result(BookingOutcome.FAILED, "服务器内部错误"),
        attempt=1,
        max_attempts=3,
    )
    assert a.stop is False
    assert a.retry_delay is not None
    assert a.retry_delay > 0


def test_max_attempts_exhausted_stops():
    a = decide_submit_action(
        _make_result(BookingOutcome.FAILED, "服务器错误"),
        attempt=3,
        max_attempts=3,
    )
    assert a.stop is True
    assert a.switch_seat is False


def test_backoff_scales():
    a1 = decide_submit_action(_make_result(BookingOutcome.FAILED, "err"), 1, 5)
    a2 = decide_submit_action(_make_result(BookingOutcome.FAILED, "err"), 3, 5)
    assert a2.retry_delay > a1.retry_delay  # 越往后等越久


def test_backoff_does_not_explode():
    # 高重试次数不应无限增长，cap 生效
    a = decide_submit_action(_make_result(BookingOutcome.FAILED, "err"), 10, 20)
    assert a.retry_delay is not None and a.retry_delay <= 120


def test_time_expired_stops_immediately():
    a = decide_submit_action(
        _make_result(BookingOutcome.FAILED, "预约开始时间必须大于当前时间"),
        1, 3,
    )
    assert a.stop is True
    assert a.switch_seat is False
    assert "过期" in a.message