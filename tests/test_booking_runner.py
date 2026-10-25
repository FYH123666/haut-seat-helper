"""预约主循环（services.booking_runner）的离线测试。

全部使用替身：**不发起任何真实网络请求，也不调用真实预约提交接口**。
重点验证安全语义：
- 认证失效 → 立即停止（既不轮询座位也不提交）；
- 提交结果未知 → 停止且绝不重试；
- 提交前必须先落盘「即将提交」标记（供重启时判定未知）；
- 被暂停/停止时不再发起新提交。
"""

from __future__ import annotations

import pytest

from haut_seat.core.auth import AuthCheck, AuthStatus
from haut_seat.core.guard import BookingDecision
from haut_seat.models.booking import BookingOutcome, BookingResult
from haut_seat.models.seat import LayoutQueryResult
from haut_seat.services.booking_runner import (
    BookingDeps,
    BookingPlanConfig,
    RunReport,
    resolve_submit_time,
    run_booking,
    ts_parse,
)
from haut_seat.services.state import ErrorCategory, RunStatus, RunStop

START = "2030-01-01 08:00:00"
END = "2030-01-01 12:00:00"
CONFIRMED = [["2", 1, False]]


class FakeTime:
    """可推进的时钟：让主循环的等待与冷却不需要真实 sleep。"""

    def __init__(self, now: float):
        self.now = now
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def seat(sid: int, name: str, *, status="2", is_can=1, open_=False) -> dict:
    return {"id": sid, "seatName": name, "seatStatus": status, "isCan": is_can, "open": open_}


def layout(*seats) -> LayoutQueryResult:
    return LayoutQueryResult.from_response({"layout": None, "seatList": list(seats)})


class Recorder:
    def __init__(self):
        self.events: list[tuple] = []

    def __call__(self, phase, level, message, category=None, **extra):
        self.events.append((phase, level, message, category))

    def phases(self) -> list[str]:
        return [e[0].value for e in self.events]

    def messages(self) -> str:
        return "\n".join(e[2] for e in self.events)

    def categories(self) -> list:
        return [e[3] for e in self.events]


def make_deps(
    *,
    seats=(),
    submit_results=(),
    query_error: Exception | None = None,
    submit_error: Exception | None = None,
    auth_check: AuthCheck | None = None,
    allowed: bool = True,
    should_continue=None,
    auth_stop_reason=None,
    time_mod: FakeTime | None = None,
    on_submit_started=None,
):
    calls = {"query": 0, "submit": 0}
    submit_iter = iter(submit_results)
    reporter = Recorder()
    clock = time_mod or FakeTime(ts_parse(START) - 3600)

    def query_fn(settings, regionid, start, end, page_num, page_size):
        calls["query"] += 1
        if query_error is not None:
            raise query_error
        return layout(*seats)

    def submit_fn(settings, target, submit_start, end):
        calls["submit"] += 1
        if submit_error is not None:
            raise submit_error
        try:
            return next(submit_iter)
        except StopIteration:  # pragma: no cover - 用例应当给足返回值
            return BookingResult(BookingOutcome.FAILED, "无更多替身返回值")

    deps = BookingDeps(
        query_fn=query_fn,
        submit_fn=submit_fn,
        auth_check_fn=lambda settings, regionid: auth_check
        or AuthCheck(AuthStatus.VALID, "凭据有效"),
        allowed_fn=lambda: BookingDecision(allowed, "测试放行" if allowed else "被拦截"),
        report=reporter,
        wait_fn=lambda target: clock.sleep(max(0.0, target - clock.time())),
        auth_stop_reason_fn=auth_stop_reason,
        time_mod=clock,
        should_continue=should_continue,
        on_submit_started=on_submit_started,
    )
    return deps, calls, reporter, clock


def make_config(**overrides) -> BookingPlanConfig:
    data = {
        "regionid": 16,
        "start": START,
        "end": END,
        "preferred_seats": ["A桌 1座"],
        "confirmed_bookable_states": CONFIRMED,
        "trigger_at": "07:00:00",
    }
    data.update(overrides)
    return BookingPlanConfig.from_mapping(data)


def run(settings, deps, config=None) -> RunReport:
    return run_booking(settings, config or make_config(), deps, None)


# ------------------------------------------------------------------ 正常路径


def test_successful_booking(make_settings):
    deps, calls, reporter, _ = make_deps(
        seats=[seat(1, "A桌 1座")],
        submit_results=[BookingResult(BookingOutcome.SUCCESS, "预约成功", booking_id=42)],
    )
    report = run(make_settings(), deps)
    assert report.status is RunStatus.SUCCEEDED
    assert report.stop is RunStop.SUCCESS
    assert report.exit_code == 0
    assert report.booking_id == 42
    assert report.submit_count == 1
    assert calls == {"query": 1, "submit": 1}
    assert "query" in reporter.phases() and "submit" in reporter.phases()


def test_success_is_reported_before_any_retry(make_settings):
    """已经拿到 bookingid 就绝不能再提交第二次。"""
    deps, calls, _, _ = make_deps(
        seats=[seat(1, "A桌 1座")],
        submit_results=[
            BookingResult(BookingOutcome.SUCCESS, "预约成功", booking_id=7),
            BookingResult(BookingOutcome.SUCCESS, "不该被调用", booking_id=8),
        ],
    )
    report = run(make_settings(), deps)
    assert report.booking_id == 7
    assert calls["submit"] == 1


def test_no_bookable_seat_reports_no_seat_not_failure(make_settings):
    """窗口内始终没有可预约座位 → NO_SEAT，而不是「预约失败」的业务错误。"""
    deps, calls, _, clock = make_deps(seats=[seat(1, "其它座位", status="1")])
    report = run(make_settings(), deps)
    assert report.status is RunStatus.NO_SEAT
    assert report.category is ErrorCategory.NO_SEAT
    assert report.submit_count == 0
    assert calls["submit"] == 0


# ------------------------------------------------------------------ 认证语义


def test_invalid_auth_stops_before_querying_or_submitting(make_settings):
    deps, calls, reporter, _ = make_deps(
        seats=[seat(1, "A桌 1座")],
        auth_check=AuthCheck(AuthStatus.INVALID, "认证失败，无法访问系统资源"),
    )
    report = run(make_settings(), deps)
    assert report.status is RunStatus.AWAITING_AUTH
    assert report.category is ErrorCategory.AUTH_INVALID
    assert calls == {"query": 0, "submit": 0}, "认证失效时不得轮询座位或提交"
    assert "重新" in reporter.messages() or "登录" in reporter.messages()


def test_indeterminate_auth_continues_but_warns(make_settings):
    """无法确认（网络/服务端异常）不是失效判定，不能被误当成失效。"""
    deps, calls, reporter, _ = make_deps(
        seats=[seat(1, "A桌 1座")],
        submit_results=[BookingResult(BookingOutcome.SUCCESS, "预约成功", booking_id=1)],
        auth_check=AuthCheck(AuthStatus.INDETERMINATE, "网络异常，无法确认"),
    )
    report = run(make_settings(), deps)
    assert report.status is RunStatus.SUCCEEDED
    assert ErrorCategory.AUTH_INDETERMINATE in reporter.categories()
    assert calls["submit"] == 1


def test_query_auth_failure_stops_immediately(make_settings):
    """查询阶段识别出认证失效：立即停止，不再继续轮询，更不提交。"""
    deps, calls, _, _ = make_deps(
        query_error=RuntimeError("认证失败"),
        auth_stop_reason=lambda exc: "认证失败，无法访问系统资源",
    )
    report = run(make_settings(), deps)
    assert report.status is RunStatus.AWAITING_AUTH
    assert calls["query"] == 1
    assert calls["submit"] == 0


def test_submit_auth_failure_stops_without_retry(make_settings):
    deps, calls, _, _ = make_deps(
        seats=[seat(1, "A桌 1座")],
        submit_results=[BookingResult(BookingOutcome.AUTH_FAILED, "登录已失效")],
    )
    report = run(make_settings(), deps)
    assert report.status is RunStatus.AWAITING_AUTH
    assert report.category is ErrorCategory.AUTH_INVALID
    assert calls["submit"] == 1, "认证失效时绝不能重试提交"


# ------------------------------------------------------------------ 结果未知


def test_unknown_submit_result_stops_and_is_not_replayed(make_settings):
    deps, calls, _, _ = make_deps(
        seats=[seat(1, "A桌 1座")],
        submit_results=[
            BookingResult(BookingOutcome.UNKNOWN, "提交后未收到应答"),
            BookingResult(BookingOutcome.SUCCESS, "不该被调用", booking_id=99),
        ],
    )
    report = run(make_settings(), deps)
    assert report.status is RunStatus.UNKNOWN
    assert report.is_unknown
    assert report.status is not RunStatus.FAILED
    assert calls["submit"] == 1


def test_submit_exception_is_unknown_and_stops(make_settings):
    deps, calls, reporter, _ = make_deps(
        seats=[seat(1, "A桌 1座")],
        submit_error=TimeoutError("read timeout"),
    )
    report = run(make_settings(), deps)
    assert report.status is RunStatus.UNKNOWN
    assert report.category is ErrorCategory.SUBMIT_EXCEPTION
    assert report.is_unknown
    assert calls["submit"] == 1
    assert ErrorCategory.SUBMIT_EXCEPTION in reporter.categories()


def test_submission_intent_is_persisted_before_the_http_call(make_settings):
    """提交前必须先回调落盘：否则崩溃重启后会误判为「从未提交、可以重放」。"""
    order: list[str] = []

    def hook():
        order.append("persisted")

    deps, _, _, _ = make_deps(
        seats=[seat(1, "A桌 1座")],
        submit_results=[BookingResult(BookingOutcome.SUCCESS, "预约成功", booking_id=1)],
        on_submit_started=hook,
    )
    original = deps.submit_fn

    def spy(settings, target, submit_start, end):
        order.append("submitted")
        return original(settings, target, submit_start, end)

    deps.submit_fn = spy
    run(make_settings(), deps)
    assert order == ["persisted", "submitted"]


def test_missing_persistence_hook_does_not_block_submission(make_settings):
    """落盘标记失败不能阻止提交本身（否则会因记录问题错过预约）。"""
    def broken_hook():
        raise RuntimeError("数据库写入失败")

    deps, calls, _, _ = make_deps(
        seats=[seat(1, "A桌 1座")],
        submit_results=[BookingResult(BookingOutcome.SUCCESS, "预约成功", booking_id=1)],
        on_submit_started=broken_hook,
    )
    report = run(make_settings(), deps)
    assert report.status is RunStatus.SUCCEEDED
    assert calls["submit"] == 1


# ------------------------------------------------------------------ 暂停 / 停止 / 拦截


def test_should_continue_false_stops_before_any_query(make_settings):
    deps, calls, _, _ = make_deps(
        seats=[seat(1, "A桌 1座")],
        should_continue=lambda: (False, "收到停止请求：不再发起新的提交"),
    )
    report = run(make_settings(), deps)
    assert report.status is RunStatus.STOPPED
    assert calls == {"query": 0, "submit": 0}


def test_stop_request_right_before_submit_prevents_the_submit(make_settings):
    """查询已经做完，但提交前收到暂停 → 不得再发起提交。"""
    state = {"n": 0}

    def should_continue():
        state["n"] += 1
        # 第 1 次是循环开头（放行），第 2 次是提交前的检查
        return (state["n"] < 2, "收到暂停请求：不再发起新的提交")

    deps, calls, _, _ = make_deps(
        seats=[seat(1, "A桌 1座")],
        submit_results=[BookingResult(BookingOutcome.SUCCESS, "不该被调用", booking_id=1)],
        should_continue=should_continue,
    )
    report = run(make_settings(), deps)
    assert report.status is RunStatus.STOPPED
    assert calls["query"] == 1
    assert calls["submit"] == 0, "停止后绝不能发起新的提交"


def test_policy_block_prevents_submission(make_settings):
    deps, calls, _, _ = make_deps(
        seats=[seat(1, "A桌 1座")],
        allowed=False,
        submit_results=[BookingResult(BookingOutcome.SUCCESS, "不该被调用", booking_id=1)],
    )
    report = run(make_settings(), deps)
    assert report.stop is RunStop.BLOCKED
    assert report.category is ErrorCategory.POLICY_BLOCKED
    assert calls["submit"] == 0


# ------------------------------------------------------------------ 配置与兜底


def test_bad_time_format_is_config_error(make_settings):
    deps, calls, _, _ = make_deps()
    report = run(make_settings(), deps, make_config(start="明天"))
    assert report.stop is RunStop.CONFIG_ERROR
    assert report.exit_code == 2
    assert calls == {"query": 0, "submit": 0}


def test_bad_trigger_is_config_error(make_settings):
    deps, _, _, _ = make_deps()
    report = run(make_settings(), deps, make_config(trigger_at="25:00"))
    assert report.stop is RunStop.CONFIG_ERROR


def test_exception_inside_reporting_does_not_break_the_run(make_settings):
    """事件上报失败绝不能影响预约流程本身。"""
    deps, calls, _, _ = make_deps(
        seats=[seat(1, "A桌 1座")],
        submit_results=[BookingResult(BookingOutcome.SUCCESS, "预约成功", booking_id=3)],
    )

    def exploding_report(*args, **kwargs):
        raise RuntimeError("上报失败")

    deps.report = exploding_report
    report = run(make_settings(), deps)
    assert report.status is RunStatus.SUCCEEDED
    assert calls["submit"] == 1


def test_retries_exhausted_then_switches_seat(make_settings):
    """首选座位多次失败后切换备选，最终成功。"""
    deps, calls, _, _ = make_deps(
        seats=[seat(1, "A桌 1座"), seat(2, "A桌 2座")],
        submit_results=[
            BookingResult(BookingOutcome.FAILED, "该座位已被占用"),
            BookingResult(BookingOutcome.SUCCESS, "预约成功", booking_id=11),
        ],
    )
    config = make_config(preferred_seats=["A桌 1座", "A桌 2座"], max_attempts_per_seat=1)
    report = run(make_settings(), deps, config)
    assert report.status is RunStatus.SUCCEEDED
    assert report.seat_switches == 1
    assert calls["submit"] == 2


# ------------------------------------------------------------------ 时间工具


def test_resolve_submit_time_keeps_far_future_start():
    start_ts = ts_parse(START)
    fake = FakeTime(start_ts - 3600)
    value, adjusted = resolve_submit_time(START, start_ts, ts_parse(END), time_mod=fake)
    assert value == START and not adjusted


def test_resolve_submit_time_adjusts_small_gap():
    start_ts = ts_parse(START)
    fake = FakeTime(start_ts - 2)
    value, adjusted = resolve_submit_time(START, start_ts, ts_parse(END), time_mod=fake)
    assert adjusted
    assert value is not None and value > START


def test_resolve_submit_time_gives_up_on_huge_gap():
    start_ts = ts_parse(START)
    fake = FakeTime(start_ts + 600)
    value, adjusted = resolve_submit_time(START, start_ts, ts_parse(END), time_mod=fake)
    assert value is None and adjusted


@pytest.mark.parametrize(
    "text,expected_ok",
    [(START, True), ("2030-01-01 08:00", True), ("2030/01/01 08:00", False), ("", False)],
)
def test_ts_parse(text, expected_ok):
    assert (ts_parse(text) is not None) is expected_ok