"""「开放时刻之前」的行为：提前启动、开放前的「未开放」响应、窗口内补跑。

**全部离线**：所有请求都打进 ``httpx.MockTransport``，配合可推进的假时钟，
既不解析真实域名，也不发起任何真实预约提交（提交请求同样被替身拦下）。

本模块要钉住的三件事：
1. 开放前**只做只读查询，绝不提交** —— 提前启动或补跑都不可能把提交提前；
2. 开放前的「未开放」响应是**可恢复的暂时状态**：不结束这次运行、不计入
   重试次数，只按间隔重新查询，直到开放（成功）或窗口关闭（停止）；
3. 认证失效**不无限重试**、提交结果未知**不自动重复提交** —— 这两条即使把
   关键词配成能命中它们的文案，也必须照旧立即停止。

关于文案：``预约尚未开放`` 这类服务器消息**不是**实测得来的（日志与运行事件里
从未出现过）。所以测试里一律**显式配置** ``HAUT_NOT_OPEN_KEYWORDS``（等价于
操作者按实测文案填写），并单独断言"默认空 = 一个词都不猜"。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx

from haut_seat.client.http import SeatHttpClient
from haut_seat.client.seatbook import ADD_BOOKING_PATH, LAYOUT_QUERY_PATH, SeatBookClient
from haut_seat.config import Settings
from haut_seat.core.auth import RELOGIN_HINT, AuthCheck, AuthStatus
from haut_seat.core.booking import is_not_open_yet, parse_not_open_keywords
from haut_seat.core.guard import BookingDecision
from haut_seat.core.schedule import booking_opens_at
from haut_seat.services.booking_runner import (
    BookingDeps,
    BookingPlanConfig,
    run_booking,
    ts_parse,
)
from haut_seat.services.io import auth_stop_reason
from haut_seat.services.state import ErrorCategory, RunStatus, RunStop

START = "2030-01-01 08:00:00"
END = "2030-01-01 12:00:00"
OPEN = "2030-01-01 07:00:00"          # 官方开放时刻（窗口当天 07:00）
TRIGGER_EARLY = "06:55:00"            # 提前启动：触发时刻早于开放时刻
CONFIRMED = [["2", 1, False]]
# 关键词由操作者提供（这里只是测试值）；默认值是空字符串，见最后两条用例。
KEYWORDS = "尚未开放,未开放"

# 可预约座位（命中 CONFIRMED 里的状态组合）
BOOKABLE_SEAT = {"id": 7, "seatName": "A桌 1座", "seatStatus": "2", "isCan": 1, "open": False}
SEAT_LIST_BODY = {"layout": [], "seatList": [BOOKABLE_SEAT]}
# 服务器「还没开放」的响应：HTTP 200 + 非零业务 code（与已观察到的错误信封同形）
NOT_OPEN_BODY = {"code": 1111, "msg": "预约尚未开放"}
# 实测到的认证失效响应体（HTTP 200 + 业务 code 401）
AUTH_FAILED_BODY = {"code": 401, "msg": "请求访问：/spacelink/error，认证失败，无法访问系统资源"}


class FakeTime:
    """可推进的时钟：主循环的等待、冷却与退避都不需要真实 sleep。"""

    def __init__(self, now: float):
        self.now = now
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeLibrary:
    """假图书馆：按**当前假时钟**决定查询/提交返回什么，并逐次记录时刻。

    记录用的是同一个假时钟，因此"提交有没有发生在开放之前"是可断言的硬事实。
    遇到未预期的路径直接失败 —— 顺便证明测试期间没有任何别的接口被碰。
    """

    def __init__(self, clock: FakeTime, *, query, submit):
        self.clock = clock
        self._query = query          # (now, n) -> dict 响应体
        self._submit = submit        # (now, n) -> dict 响应体
        self.query_times: list[float] = []
        self.submit_times: list[float] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        now = self.clock.time()
        path = request.url.path
        if path == LAYOUT_QUERY_PATH:
            self.query_times.append(now)
            return httpx.Response(200, json=self._query(now, len(self.query_times)))
        if path == ADD_BOOKING_PATH:
            self.submit_times.append(now)
            return httpx.Response(200, json=self._submit(now, len(self.submit_times)))
        raise AssertionError(f"未预期的请求路径（测试禁止触碰其它接口）: {path}")


class Recorder:
    def __init__(self):
        self.events: list[tuple] = []

    def __call__(self, phase, level, message, category=None, **extra):
        self.events.append((phase, level, message, category))

    def messages(self) -> str:
        return "\n".join(e[2] for e in self.events)

    def categories(self) -> list:
        return [e[3] for e in self.events]

    def has_category(self, category) -> bool:
        return category in self.categories()


def always(body: dict):
    """不管什么时候、第几次请求，都返回同一个响应体。"""
    return lambda now, n: body


def make_deps(
    clock: FakeTime,
    library: FakeLibrary,
    *,
    should_continue=None,
    on_submit_started=None,
) -> tuple[BookingDeps, Recorder]:
    """把**真实的** SeatBookClient 接进主循环，仅把传输层换成 MockTransport。"""
    client = SeatHttpClient(
        "https://library.invalid",   # 保留域名，永不解析；请求全部被 MockTransport 拦下
        transport=httpx.MockTransport(library.handler),
        min_interval=0,
        max_retry=0,
    )
    book = SeatBookClient(client)
    reporter = Recorder()
    deps = BookingDeps(
        query_fn=lambda settings, regionid, start, end, page_num, page_size: book.query_layout(
            regionid=regionid, starttime=start, endtime=end,
            page_num=page_num, page_size=page_size,
        ),
        submit_fn=lambda settings, seat, start, end: book.submit_booking(
            seat, start, end, "1001"
        ),
        auth_check_fn=lambda settings, regionid: AuthCheck(AuthStatus.VALID, "凭据有效"),
        allowed_fn=lambda: BookingDecision(True, "测试放行"),
        report=reporter,
        wait_fn=lambda target: clock.sleep(max(0.0, target - clock.time())),
        # 用**真实**的认证失效判定，而不是替身：这样「认证失效立即停止」验的是生产判据
        auth_stop_reason_fn=auth_stop_reason,
        time_mod=clock,
        should_continue=should_continue,
        on_submit_started=on_submit_started,
    )
    return deps, reporter


def make_config(**overrides) -> BookingPlanConfig:
    """默认 ``max_attempts_per_seat=1``：一次普通失败就是「重试次数耗尽」，
    因此任何"没有提前结束"的断言都足够尖锐（旧行为下一次失败就烧掉整天）。"""
    data = {
        "regionid": 16,
        "start": START,
        "end": END,
        "preferred_seats": ["A桌 1座"],
        "confirmed_bookable_states": CONFIRMED,
        "trigger_at": TRIGGER_EARLY,
        "max_attempts_per_seat": 1,
    }
    data.update(overrides)
    return BookingPlanConfig.from_mapping(data)


def run(make_settings, clock, library, *, keywords=KEYWORDS, config=None,
        should_continue=None, on_submit_started=None):
    deps, reporter = make_deps(
        clock, library, should_continue=should_continue, on_submit_started=on_submit_started
    )
    settings = make_settings(not_open_keywords=keywords)
    report = run_booking(settings, config or make_config(), deps, None)
    return report, reporter


# ------------------------------------------------- 开放前「未开放」→ 开放后成功


def test_open_before_query_refusal_is_recoverable_and_succeeds_after_open(make_settings):
    """开放前查询被拒（服务器说未开放）不结束运行：开放后重新查询并成功。"""
    clock = FakeTime(ts_parse("2030-01-01 06:50:00"))   # 早于触发时刻，也早于开放
    library = FakeLibrary(
        clock,
        query=lambda now, n: NOT_OPEN_BODY if now < ts_parse(OPEN) else SEAT_LIST_BODY,
        submit=always({"code": 200, "bookingid": 555, "seatName": "A桌 1座"}),
    )
    report, reporter = run(make_settings, clock, library)

    assert report.status is RunStatus.SUCCEEDED
    assert report.stop is RunStop.SUCCESS
    assert report.booking_id == 555
    # 开放前反复重新查询（不是一次失败就放弃），开放后照常查询
    assert any(t < ts_parse(OPEN) for t in library.query_times)
    assert any(t >= ts_parse(OPEN) for t in library.query_times)
    assert report.query_count == len(library.query_times) >= 3
    # 提交只发生一次，且发生在开放之后
    assert report.submit_count == 1
    assert library.submit_times == [ts_parse(OPEN)]
    # 「未开放」被如实分类，而不是显示成网络异常
    assert reporter.has_category(ErrorCategory.NOT_OPEN)
    assert "尚未开放" in reporter.messages()


def test_query_refusal_alone_never_produces_a_submission(make_settings):
    """开放前查询一直被拒（全程无候选）→ 到点停止，绝不因为查询失败而提交。"""
    clock = FakeTime(ts_parse("2030-01-01 " + TRIGGER_EARLY))
    library = FakeLibrary(
        clock,
        query=lambda now, n: NOT_OPEN_BODY,        # 永远「未开放」
        submit=always({"code": 200, "bookingid": 1}),
    )
    # 窗口很短：END 压到开放后一分钟，用例不必空转一上午
    config = make_config(start="2030-01-01 07:00:00", end="2030-01-01 07:01:00")
    report, reporter = run(make_settings, clock, library, config=config)

    assert library.submit_times == [], "查询失败绝不能被当成『可以提交了』"
    assert report.status is RunStatus.NO_SEAT
    assert report.stop is RunStop.WINDOW_CLOSED
    assert report.submit_count == 0
    # 一直重查到窗口关闭为止（间隔有界、没有空转）
    assert len(library.query_times) >= 3
    assert max(clock.sleeps) <= 30.0
    assert reporter.has_category(ErrorCategory.NOT_OPEN)


# ------------------------------------------------- 开放前的硬闸门（不提前提交）


def test_no_submission_happens_before_the_official_open_moment(make_settings):
    """就算开放前查询就已经返回可预约座位，也绝不能提前提交。"""
    clock = FakeTime(ts_parse(OPEN) - 30)          # 06:59:30，触发时刻已过
    library = FakeLibrary(
        clock,
        query=always(SEAT_LIST_BODY),              # 座位看起来可以约 —— 但还没开放
        submit=always({"code": 200, "bookingid": 777}),
    )
    report, reporter = run(make_settings, clock, library)

    assert report.status is RunStatus.SUCCEEDED
    assert library.submit_times == [ts_parse(OPEN)], "提交只能发生在开放的那一刻或之后"
    assert report.submit_count == 1
    # 等待期间重新查询过（不是拿着 06:59:30 的旧候选直接提交）
    assert len(library.query_times) >= 2
    assert reporter.has_category(ErrorCategory.NOT_OPEN)
    assert "开放前不提交" in reporter.messages()


def test_pre_open_wait_still_honours_pause_and_submits_nothing(make_settings):
    """开放前等待期间收到暂停/停止：立即结束，且一次都不提交。"""
    clock = FakeTime(ts_parse(OPEN) - 60)
    library = FakeLibrary(
        clock, query=always(SEAT_LIST_BODY), submit=always({"code": 200, "bookingid": 1})
    )
    calls = {"n": 0}

    def should_continue():
        calls["n"] += 1
        if calls["n"] >= 2:
            return False, "收到暂停请求：不再发起新的提交"
        return True, ""

    report, _ = run(make_settings, clock, library, should_continue=should_continue)

    assert library.submit_times == []
    assert report.status is RunStatus.STOPPED
    assert report.stop is RunStop.STOPPED


# --------------------------------------- 服务器在提交时说「尚未开放」：可恢复


def test_submit_refused_as_not_open_recovers_and_does_not_burn_attempts(make_settings):
    """服务器（时钟比我们慢）在提交时说「尚未开放」：不算失败、不结束运行。"""
    clock = FakeTime(ts_parse(OPEN))               # 我们这边正好 07:00
    library = FakeLibrary(
        clock,
        query=always(SEAT_LIST_BODY),
        # 第 1 次拒绝（服务器还没到点），第 2 次成功
        submit=lambda now, n: NOT_OPEN_BODY if n == 1 else {"code": 200, "bookingid": 999},
    )
    report, reporter = run(make_settings, clock, library)

    assert report.status is RunStatus.SUCCEEDED, "一次『未开放』不能烧掉整天"
    assert report.booking_id == 999
    # 两次提交之间经过了一个退避间隔，且第二次提交前重新查询过
    assert library.submit_times == [ts_parse(OPEN), ts_parse(OPEN) + 30]
    assert len(library.query_times) >= 2
    assert reporter.has_category(ErrorCategory.NOT_OPEN)
    assert "不算作失败重试" in reporter.messages()


def test_without_configured_keywords_the_refusal_still_ends_the_run(make_settings):
    """默认不配置关键词：不猜文案，行为与从前一致（对照用例）。"""
    clock = FakeTime(ts_parse(OPEN))
    library = FakeLibrary(
        clock, query=always(SEAT_LIST_BODY), submit=always(NOT_OPEN_BODY)
    )
    report, reporter = run(make_settings, clock, library, keywords="")

    assert report.status is RunStatus.FAILED
    assert report.stop is RunStop.RETRIES_EXHAUSTED
    assert report.submit_count == 1
    assert not reporter.has_category(ErrorCategory.NOT_OPEN)


def test_not_open_refusal_keeps_polling_until_the_deadline_then_stops(make_settings):
    """一直「未开放」也要**等到窗口关闭**才停（间隔有界，不无限重试同一份候选）。"""
    clock = FakeTime(ts_parse("2030-01-01 06:59:00"))
    library = FakeLibrary(
        clock,
        query=always(SEAT_LIST_BODY),
        submit=always(NOT_OPEN_BODY),           # 永远说未开放
    )
    config = make_config(start="2030-01-01 07:00:00", end="2030-01-01 07:01:00")
    report, _ = run(make_settings, clock, library, config=config)

    assert report.stop is RunStop.RETRIES_EXHAUSTED
    assert report.status is RunStatus.FAILED
    assert library.submit_times == [ts_parse(OPEN), ts_parse(OPEN) + 30]
    assert clock.time() >= ts_parse("2030-01-01 07:01:00"), "必须在窗口关闭时才停"


# ------------------------------------------------- 认证失效：绝不无限重试


def test_query_auth_failure_stops_at_once_and_never_submits(make_settings):
    """查询被判定为认证失效 → 立即停止：不轮询、也不提交。"""
    clock = FakeTime(ts_parse(OPEN))
    library = FakeLibrary(
        clock, query=always(AUTH_FAILED_BODY), submit=always({"code": 200, "bookingid": 1})
    )
    report, reporter = run(make_settings, clock, library)

    assert report.status is RunStatus.AWAITING_AUTH
    assert report.stop is RunStop.AUTH_INVALID
    assert library.query_times == [ts_parse(OPEN)], "认证失效只探一次，不反复重试"
    assert library.submit_times == []
    assert reporter.has_category(ErrorCategory.AUTH_INVALID)
    assert RELOGIN_HINT.splitlines()[0] in reporter.messages()


def test_submit_auth_failure_is_stopped_even_if_keywords_cover_its_wording(make_settings):
    """就算关键词能命中认证失效的文案，也绝不能被当成「可恢复的未开放」。"""
    clock = FakeTime(ts_parse(OPEN))
    library = FakeLibrary(
        clock, query=always(SEAT_LIST_BODY), submit=always(AUTH_FAILED_BODY)
    )
    # 关键词故意配上「认证失败」：认证失效不得落入可恢复分支
    report, reporter = run(make_settings, clock, library, keywords="认证失败,未开放")

    assert report.status is RunStatus.AWAITING_AUTH
    assert report.stop is RunStop.AUTH_INVALID
    assert library.submit_times == [ts_parse(OPEN)], "认证失效绝不再提交"
    assert report.submit_count == 1
    assert not reporter.has_category(ErrorCategory.NOT_OPEN)


# ------------------------------------------------- 结果未知：绝不自动重复提交


def test_unknown_submit_result_is_never_automatically_repeated(make_settings):
    """提交结果未知 → 立即停止、只提交过一次，关键词命中也不改变这一点。"""
    clock = FakeTime(ts_parse(OPEN))
    library = FakeLibrary(
        clock,
        query=always(SEAT_LIST_BODY),
        submit=always({"code": 0, "msg": "无法确认结果"}),   # code=0 → UNKNOWN
    )
    report, _ = run(make_settings, clock, library, keywords="无法确认,未开放")

    assert report.status is RunStatus.UNKNOWN
    assert report.stop is RunStop.SUBMIT_UNKNOWN
    assert library.submit_times == [ts_parse(OPEN)], "结果未知绝不自动重发"
    assert report.submit_count == 1


def test_unknown_result_stops_before_the_persistence_hook_could_be_reused(make_settings):
    """未知结果与「提交前落盘」保护配合：落盘发生一次，且不会因为未知而重放。"""
    clock = FakeTime(ts_parse(OPEN))
    library = FakeLibrary(
        clock, query=always(SEAT_LIST_BODY), submit=always({"code": 0, "msg": "无法确认结果"})
    )
    marks: list[int] = []
    report, _ = run(make_settings, clock, library, on_submit_started=lambda: marks.append(1))

    assert marks == [1]
    assert report.status is RunStatus.UNKNOWN
    assert library.submit_times == [ts_parse(OPEN)]


# ------------------------------------------------- 普通失败/时间错误：照旧停止


def test_ordinary_failures_still_exhaust_attempts_and_stop(make_settings):
    """非「未开放」的确定性失败不走可恢复分支：重试次数耗尽后照旧停止。"""
    clock = FakeTime(ts_parse(OPEN))
    library = FakeLibrary(
        clock,
        query=always(SEAT_LIST_BODY),
        submit=always({"code": 1111, "msg": "系统繁忙，请稍后再试"}),
    )
    report, reporter = run(
        make_settings, clock, library, config=make_config(max_attempts_per_seat=2)
    )

    assert report.stop is RunStop.RETRIES_EXHAUSTED
    assert report.status is RunStatus.FAILED
    assert report.submit_count == 2 == len(library.submit_times)
    assert not reporter.has_category(ErrorCategory.NOT_OPEN)


def test_time_expired_submission_stops_without_retrying(make_settings):
    """服务器说开始时间已过期 → 确定性错误，一次就停（不重试、不切换座位）。"""
    clock = FakeTime(ts_parse(OPEN))
    library = FakeLibrary(
        clock,
        query=always(SEAT_LIST_BODY),
        submit=always({"code": 1111, "msg": "预约开始时间必须大于当前时间"}),
    )
    report, _ = run(make_settings, clock, library, config=make_config(max_attempts_per_seat=3))

    assert report.stop is RunStop.DETERMINISTIC_FAILURE
    assert library.submit_times == [ts_parse(OPEN)]


# ------------------------------------------------- 纯函数与默认值


def test_booking_opens_at_is_the_window_day_official_open():
    assert booking_opens_at("2030-01-01 08:00:00") == datetime(2030, 1, 1, 7, 0, 0)
    assert booking_opens_at("2030-01-01 07:00") == datetime(2030, 1, 1, 7, 0, 0)
    # 解析不出 → None：调用方沿用既有判据，不猜
    assert booking_opens_at("不是时间") is None
    assert booking_opens_at("") is None


def test_not_open_keywords_are_empty_by_default_so_nothing_is_guessed():
    assert Settings(_env_file=None).not_open_keywords == ""
    assert parse_not_open_keywords(None) == ()
    assert parse_not_open_keywords(" 未开放 , 尚未开放 ,,") == ("未开放", "尚未开放")
    # 默认（空关键词）时永远不匹配 —— 不得凭猜测把失败当成「稍后再试」
    assert is_not_open_yet("预约尚未开放", ()) is False
    assert is_not_open_yet("预约尚未开放", parse_not_open_keywords("尚未开放")) is True
    assert is_not_open_yet("", ("未开放",)) is False


def test_no_test_in_this_module_sends_real_requests():
    """守卫：本模块只使用 MockTransport，不构造任何真实网络客户端。"""
    text = Path(__file__).read_text(encoding="utf-8")
    assert "MockTransport" in text
    # 不允许出现真实基地址（避免误发真实请求）；按片段拼装，避免自匹配
    real_host = "wslib" + ".haut" + ".edu.cn"
    assert real_host not in text