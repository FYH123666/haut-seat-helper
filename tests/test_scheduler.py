"""后台调度器测试（全部离线：替身查询/提交，不发起任何真实预约）。

覆盖需求里点名要测的几条：
- 后台任务与浏览器生命周期解耦（关掉页面照样跑完）；
- 同一计划不会并发执行；
- 重启恢复：已经提交过的遗留运行判为**结果未知**且绝不自动重放；
- 认证失效时不再发起点任何提交；
- 通知抛异常不影响预约任务；
- 全局暂停 / 重复计划重排 / 手动执行的各项前置拒绝。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, time as dtime, timedelta

import pytest

from haut_seat.core.auth import AuthCheck, AuthStatus
from haut_seat.models.booking import BookingOutcome, BookingResult
from haut_seat.models.seat import LayoutQueryResult
from haut_seat.services.booking_runner import ts_parse
from haut_seat.services.scheduler import Scheduler, plan_window_state
from haut_seat.services.state import ErrorCategory, PlanStatus, RunStatus
from haut_seat.web import repository as repo
from haut_seat.web.validation import parse_plan

CONFIRMED = [["2", 1, False]]

# 固定「现在」为当天的 10:00：让「触发时刻已过、预约开始时间在未来、窗口未关闭」
# 这三个条件与机器上真实的运行时刻无关（预约窗口校验只允许 07:00–21:30）。
NOW = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
TODAY = NOW.strftime("%Y-%m-%d")


# ------------------------------------------------------------------ 替身


class FakeTime:
    def __init__(self, now: float):
        self.now = now

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:  # 不真的等待
        self.now += seconds


class StubAuthService:
    def __init__(self, blocked: bool = False):
        self.blocked = blocked

    def blocks_submissions(self) -> bool:
        return self.blocked

    def current(self):
        return None


class RecordingNotifier:
    """记录通知调用；可选地让每个方法都抛异常，验证通知不会拖垮任务。"""

    def __init__(self, explode: bool = False):
        self.explode = explode
        self.calls: list[tuple[str, dict]] = []

    def _record(self, name, kwargs):
        self.calls.append((name, kwargs))
        if self.explode:
            raise RuntimeError("通知服务故障")

    def __getattr__(self, name):
        if name.startswith("notify_"):
            def _call(**kwargs):
                self._record(name, kwargs)
                return {"status": "skipped"}
            return _call
        raise AttributeError(name)

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


def seat(sid: int, name: str) -> dict:
    return {"id": sid, "seatName": name, "seatStatus": "2", "isCan": 1, "open": False}


def layout(*seats) -> LayoutQueryResult:
    return LayoutQueryResult.from_response({"layout": None, "seatList": list(seats)})


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ------------------------------------------------------------------ 夹具


@pytest.fixture
def live_plan(web_db):
    """一条「窗口已开放」的计划：触发时刻已过、预约开始时间尚未来到。"""
    form = {
        "name": "调度测试",
        "run_date": TODAY,
        "repeat_rule": "once",
        "trigger_at": (NOW - timedelta(hours=1)).strftime("%H:%M:%S"),
        "book_start": (NOW + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
        "book_end": (NOW + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S"),
        "regionid": "16",
        "preferred_seats": "A桌 1座",
        "preferred_prefixes": "",
        "confirmed_bookable_states": '[["2", 1, false]]',
        "max_attempts_per_seat": "1",
        "note": "",
    }
    data, _ = parse_plan(form, today=NOW.date())
    plan_id = repo.create_plan(web_db, data, enabled=True)
    repo.set_plan_status(web_db, plan_id, PlanStatus.PENDING)
    repo.set_app_setting(web_db, global_pause=False)
    return plan_id


@pytest.fixture
def make_scheduler(web_db, web_settings):
    """构造注入替身的调度器；默认不启动后台线程（由用例显式 tick）。

    时钟固定为 ``NOW``，因此「是否到点/是否过窗口」的判断与真实运行时刻无关。
    """
    made = []

    def _make(
        *,
        submit_results=None,
        submit_error=None,
        auth_blocked=False,
        notifier=None,
        query_delay=None,
        enable_real_booking=True,
        **kwargs,
    ):
        settings = web_settings.model_copy(
            update={"enable_real_booking": enable_real_booking}
        )
        fake_time = FakeTime(NOW.timestamp())
        results = iter(submit_results or [
            BookingResult(BookingOutcome.SUCCESS, "预约成功", booking_id=101)
        ])
        if query_delay is not None:
            gate = query_delay  # 由用例决定何时放行，用来模拟「长时间运行中」
        else:
            gate = threading.Event()
            gate.set()  # 默认不阻塞

        def query_fn(settings_, regionid, start, end, page_num, page_size):
            gate.wait(timeout=5.0)
            return layout(seat(1, "A桌 1座"))

        def submit_fn(settings_, target_seat, submit_start, end):
            if submit_error is not None:
                raise submit_error
            try:
                return next(results)
            except StopIteration:  # pragma: no cover
                return BookingResult(BookingOutcome.FAILED, "无更多替身返回值")

        scheduler = Scheduler(
            web_db,
            settings,
            tick_s=0.05,
            max_concurrent=2,
            query_fn=query_fn,
            submit_fn=submit_fn,
            auth_check_fn=lambda s, r, start=None, end=None: AuthCheck(AuthStatus.VALID, "ok"),
            notification=notifier,
            auth_service=StubAuthService(blocked=auth_blocked),
            clock=lambda: NOW,
            time_mod=fake_time,
            wait_fn=lambda target_ts: None,
            **kwargs,
        )
        made.append(scheduler)
        return scheduler

    yield _make
    for scheduler in made:
        scheduler.stop(timeout=1.0)


def run_count(db) -> int:
    return len(repo.list_runs(db))


# ------------------------------------------------------------------ 正常调度


def test_scheduler_runs_plan_in_background_thread(web_db, live_plan, make_scheduler):
    """后台任务与浏览器生命周期解耦：没有任何 HTTP 请求参与也会跑完。"""
    scheduler = make_scheduler()
    scheduler._tick()

    assert wait_for(lambda: not repo.list_running_runs(web_db)), "运行没有结束"
    assert wait_for(lambda: scheduler.active_plan_ids() == [])
    runs = repo.list_runs(web_db, plan_id=live_plan)
    assert len(runs) == 1
    assert runs[0]["status"] == RunStatus.SUCCEEDED.value
    assert runs[0]["trigger"] == "scheduler"
    assert runs[0]["booking_id"] == 101
    assert runs[0]["submit_count"] == 1
    assert repo.get_plan(web_db, live_plan)["status"] is PlanStatus.SUCCEEDED

    # 运行事件被记录，且阶段齐全
    events = repo.list_run_events(web_db, runs[0]["id"])
    phases = {e["phase"] for e in events}
    assert {"start", "auth_check", "query", "submit", "end"} <= phases


def test_run_events_never_contain_credentials(web_db, live_plan, make_scheduler):
    scheduler = make_scheduler(
        submit_results=[BookingResult(BookingOutcome.SUCCESS, "预约成功", booking_id=1)]
    )
    scheduler._tick()
    assert wait_for(lambda: not repo.list_running_runs(web_db))
    blob = " ".join(
        e["message"] for run in repo.list_runs(web_db) for e in repo.list_run_events(web_db, run["id"])
    )
    assert "Bearer " not in blob
    assert "Authorization" not in blob


def test_global_pause_blocks_new_runs(web_db, live_plan, make_scheduler):
    """默认全局暂停必须生效：不启动任何新运行。"""
    repo.set_app_setting(web_db, global_pause=True)
    scheduler = make_scheduler()
    for _ in range(3):
        scheduler._tick()
    time.sleep(0.1)
    assert run_count(web_db) == 0
    assert repo.get_plan(web_db, live_plan)["status"] is PlanStatus.PENDING


def test_scheduler_enabled_flag_stops_scheduling(web_db, live_plan, make_scheduler):
    repo.set_app_setting(web_db, scheduler_enabled=False)
    scheduler = make_scheduler()
    scheduler._tick()
    time.sleep(0.1)
    assert run_count(web_db) == 0


def test_disabled_booking_switch_blocks_run(web_db, live_plan, make_scheduler):
    """HAUT_ENABLE_REAL_BOOKING=false 时不得启动真实运行。"""
    scheduler = make_scheduler(enable_real_booking=False)
    scheduler._tick()
    time.sleep(0.1)
    assert run_count(web_db) == 0


def test_plan_not_due_today_is_skipped(web_db, live_plan, make_scheduler):
    future = (NOW + timedelta(days=3)).strftime("%Y-%m-%d")
    with web_db.tx() as conn:
        conn.execute("UPDATE plan SET run_date = ? WHERE id = ?", (future, live_plan))
    scheduler = make_scheduler()
    scheduler._tick()
    time.sleep(0.1)
    assert run_count(web_db) == 0


def test_before_trigger_nothing_starts(web_db, live_plan, make_scheduler):
    later = (NOW + timedelta(hours=1)).strftime("%H:%M:%S")
    with web_db.tx() as conn:
        conn.execute("UPDATE plan SET trigger_at = ? WHERE id = ?", (later, live_plan))
    scheduler = make_scheduler()
    scheduler._tick()
    time.sleep(0.1)
    assert run_count(web_db) == 0


def test_expired_window_is_not_started(web_db, live_plan, make_scheduler):
    past = (NOW - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    with web_db.tx() as conn:
        conn.execute(
            "UPDATE plan SET book_start = ?, book_end = ? WHERE id = ?",
            ((NOW - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"), past, live_plan),
        )
    scheduler = make_scheduler()
    scheduler._tick()
    time.sleep(0.1)
    assert run_count(web_db) == 0


# ------------------------------------------------------------------ 并发保护


def test_same_plan_is_never_executed_twice_concurrently(web_db, live_plan, make_scheduler):
    """同一计划不能并发执行：第二次 spawn 必须被拒绝。"""
    gate = threading.Event()
    scheduler = make_scheduler(query_delay=gate)

    assert scheduler._spawn(live_plan, trigger="scheduler") is True
    assert scheduler._spawn(live_plan, trigger="scheduler") is False
    assert wait_for(lambda: scheduler.active_plan_ids() == [live_plan])

    gate.set()
    # 运行记录是在后台线程里创建的，先等它出现再判断「只跑了一次」
    assert wait_for(lambda: run_count(web_db) == 1), "运行记录尚未创建"
    assert wait_for(lambda: not repo.list_running_runs(web_db))
    assert run_count(web_db) == 1, "并发保护失效会导致重复运行"


def test_tick_does_not_start_a_second_run_while_one_is_running(
    web_db, live_plan, make_scheduler
):
    gate = threading.Event()
    scheduler = make_scheduler(query_delay=gate)
    scheduler._tick()
    assert wait_for(lambda: run_count(web_db) == 1), "首个运行尚未创建"
    for _ in range(5):
        scheduler._tick()
    assert run_count(web_db) == 1
    gate.set()
    assert wait_for(lambda: not repo.list_running_runs(web_db))
    assert run_count(web_db) == 1


def test_max_concurrent_limit_is_respected(web_db, live_plan, make_scheduler):
    """并发上限生效时，超出的计划本轮跳过而不是排队硬上。

    第二个计划刻意用**另一个时段**：时段锁与并发上限是两道独立的闸门，如果两者
    用同一个时段，挡住它的可能是时段锁而不是 ``max_concurrent``，这条用例就废了。
    """
    second = repo.get_plan(web_db, live_plan)
    data, _ = parse_plan(
        {
            "name": "第二条", "run_date": second["run_date"], "repeat_rule": "once",
            "trigger_at": second["trigger_at"],
            "book_start": (NOW + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "book_end": (NOW + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S"),
            "regionid": "16",
            "preferred_seats": "A桌 1座", "preferred_prefixes": "",
            "confirmed_bookable_states": '[["2", 1, false]]',
            "max_attempts_per_seat": "1", "note": "",
        },
        today=NOW.date(),
    )
    other = repo.create_plan(web_db, data, enabled=True)
    repo.set_plan_status(web_db, other, PlanStatus.PENDING)

    gate = threading.Event()
    scheduler = make_scheduler(query_delay=gate)
    scheduler.max_concurrent = 1
    scheduler._tick()
    assert wait_for(lambda: run_count(web_db) == 1), "首个运行尚未创建"
    assert wait_for(lambda: len(scheduler.active_plan_ids()) == 1)
    # 前置事实：这一条确实被时段锁放行了（否则挡住它的是锁，不是并发上限）。
    assert scheduler._slot_lock_free(repo.get_plan(web_db, other)), (
        "两个计划的时段撞在一起了，这条用例就测不到并发上限"
    )
    scheduler._tick()
    assert run_count(web_db) == 1, "并发上限 1 时不得启动第二个运行"
    gate.set()
    assert wait_for(lambda: not repo.list_running_runs(web_db))


# ------------------------------------------------------------------ 重启恢复


def _start_and_leave_running(web_db, plan_id, *, submit_count: int) -> int:
    run_id = repo.start_run(web_db, plan_id, trigger="scheduler")
    if submit_count:
        with web_db.tx() as conn:
            conn.execute(
                "UPDATE run SET submit_count = ? WHERE id = ?", (submit_count, run_id)
            )
    return run_id


def test_restart_after_submit_marks_unknown_and_never_replays(
    web_db, live_plan, make_scheduler
):
    """已经提交过的遗留运行 → 结果未知；既不能记成失败，也绝不自动重放。"""
    run_id = _start_and_leave_running(web_db, live_plan, submit_count=1)
    notifier = RecordingNotifier()

    scheduler = make_scheduler(notifier=notifier)
    recovered = scheduler.recover_after_restart()

    assert [r["run_id"] for r in recovered] == [run_id]
    run = repo.get_run(web_db, run_id)
    assert run["status"] is RunStatus.INTERRUPTED
    assert run["category"] == ErrorCategory.SUBMIT_UNKNOWN.value
    assert run["status"] is not RunStatus.FAILED

    plan = repo.get_plan(web_db, live_plan)
    assert plan["status"] is PlanStatus.UNKNOWN
    assert plan["unknown_ack_at"] is None, "恢复后必须等待人工核实"
    assert "notify_submit_unknown" in notifier.names()

    # 再次 tick：状态不是「待执行」，不会被自动重跑
    for _ in range(3):
        scheduler._tick()
    time.sleep(0.1)
    assert run_count(web_db) == 1, "未知结果绝不能被自动重放"


def test_restart_without_submit_is_marked_interrupted_failed(web_db, live_plan, make_scheduler):
    """尚未提交过的遗留运行 → 中断（可安全重跑），不进入未知状态。"""
    run_id = _start_and_leave_running(web_db, live_plan, submit_count=0)
    notifier = RecordingNotifier()
    scheduler = make_scheduler(notifier=notifier)

    scheduler.recover_after_restart()
    run = repo.get_run(web_db, run_id)
    assert run["status"] is RunStatus.INTERRUPTED
    assert run["category"] == ErrorCategory.INTERNAL_ERROR.value
    assert repo.get_plan(web_db, live_plan)["status"] is PlanStatus.FAILED
    assert "notify_submit_unknown" not in notifier.names()


def test_restart_recovery_keeps_the_persisted_submit_marker(web_db, live_plan, make_scheduler):
    """恢复必须原样保留「提交前已落盘」的计数。

    ``finish_run`` 对这一列是**整列覆盖**（形参默认值 0），恢复时漏传就等于把
    「可能已经在服务器端留下了一条预约」的证据抹掉 —— 后面的
    ``ran_irreversibly_today`` 会因此放行同一天的第二次提交。
    """
    run_id = _start_and_leave_running(web_db, live_plan, submit_count=2)
    assert repo.get_run(web_db, run_id)["submit_count"] == 2  # 前置条件

    scheduler = make_scheduler()
    scheduler.recover_after_restart()

    run = repo.get_run(web_db, run_id)
    assert run["submit_count"] == 2, "恢复不得把提交标记清零"
    # 其余语义保持既有约定不变：已提交 ⇒ 运行记中断、分类结果未知、计划等人工核实
    assert run["status"] is RunStatus.INTERRUPTED
    assert run["category"] == ErrorCategory.SUBMIT_UNKNOWN.value
    assert run["status"] is not RunStatus.FAILED
    plan = repo.get_plan(web_db, live_plan)
    assert plan["status"] is PlanStatus.UNKNOWN
    assert plan["unknown_ack_at"] is None


def test_an_acknowledged_unknown_from_a_restart_is_not_replayed_the_same_day(
    web_db, live_plan, make_scheduler
):
    """完整危险链路：已提交 → 中断 → 恢复 → 人工核实 → 同一天不得再提交一次。

    核实只是「我看到了这个未知结果」，**不是**「允许今天再提交一次」。
    """
    run_id = _start_and_leave_running(web_db, live_plan, submit_count=1)

    scheduler = make_scheduler()
    assert [r["submitted"] for r in scheduler.recover_after_restart()] == [True]

    # 走现有人工核实流程（与网页「已核实并恢复」同一个业务函数），不直接改库
    resumed = repo.resume_plan(web_db, live_plan, acknowledge_unknown=True)
    assert resumed["ok"], resumed
    plan = repo.get_plan(web_db, live_plan)
    assert plan["status"] is PlanStatus.PENDING and int(plan["enabled"]) == 1
    assert plan["unknown_ack_at"], "核实时间必须落库"

    # 同一天再次触发自动调度：不得产生任何查询、提交或新的运行记录
    queries = {"n": 0}
    original = scheduler._query_fn

    def counting_query(*args, **kwargs):
        queries["n"] += 1
        return original(*args, **kwargs)

    scheduler._query_fn = counting_query
    for _ in range(3):
        scheduler._tick()
    time.sleep(0.1)

    assert run_count(web_db) == 1, "核实过的未知结果不得在同一天被再次提交"
    assert queries["n"] == 0, "连座位查询都不该发生"
    assert scheduler.active_plan_ids() == []
    assert repo.list_running_runs(web_db) == []
    assert repo.get_run(web_db, run_id)["submit_count"] == 1


def test_recover_after_restart_survives_a_broken_database(web_db, live_plan, make_scheduler):
    class BrokenDb:
        def query_all(self, *a, **k):
            raise RuntimeError("数据库不可用")

    scheduler = make_scheduler()
    scheduler.db = BrokenDb()
    assert scheduler.recover_after_restart() == []  # 不抛异常


def test_unknown_plan_is_not_started_without_acknowledgement(
    web_db, live_plan, make_scheduler
):
    old_run = repo.start_run(web_db, live_plan, trigger="scheduler")
    repo.finish_run(
        web_db, old_run, status=RunStatus.UNKNOWN, category=ErrorCategory.SUBMIT_UNKNOWN,
        stop_reason="submit_unknown", message="结果未知", exit_code=0,
        plan_status=PlanStatus.UNKNOWN,
    )
    # 人为把它改回「待执行」但没有核实时间，模拟误操作
    with web_db.tx() as conn:
        conn.execute(
            "UPDATE plan SET status = 'pending', unknown_ack_at = NULL WHERE id = ?",
            (live_plan,),
        )
    scheduler = make_scheduler()
    scheduler._tick()
    time.sleep(0.1)
    assert run_count(web_db) == 1, "未核实的未知结果不得被重新执行"


# ------------------------------------------------------------------ 认证


def test_auth_failure_stops_all_new_submissions(web_db, live_plan, make_scheduler):
    """认证失效：不启动运行、不轮询座位、不提交，计划标为等待认证。"""
    queries = {"n": 0}
    scheduler = make_scheduler(auth_blocked=True)
    original = scheduler._query_fn

    def counting_query(*args, **kwargs):
        queries["n"] += 1
        return original(*args, **kwargs)

    scheduler._query_fn = counting_query
    notifier = RecordingNotifier()
    scheduler.notification = notifier

    scheduler._tick()
    time.sleep(0.1)
    assert run_count(web_db) == 0
    assert queries["n"] == 0
    assert repo.get_plan(web_db, live_plan)["status"] is PlanStatus.AWAITING_AUTH
    assert "notify_auth_failure" in notifier.names()


def test_auth_failure_during_run_marks_auth_state_invalid(web_db, live_plan, make_scheduler):
    notifier = RecordingNotifier()
    scheduler = make_scheduler(
        submit_results=[BookingResult(BookingOutcome.AUTH_FAILED, "登录已失效")],
        notifier=notifier,
    )
    scheduler._tick()
    assert wait_for(lambda: repo.list_runs(web_db)), "运行记录尚未创建"
    assert wait_for(lambda: not repo.list_running_runs(web_db))
    run = repo.list_runs(web_db)[0]
    assert run["status"] == RunStatus.AWAITING_AUTH.value
    assert run["category"] == ErrorCategory.AUTH_INVALID.value
    assert repo.get_auth_state(web_db)["status"] == "invalid"
    assert "notify_auth_failure" in notifier.names()


# ------------------------------------------------------------------ 通知容错


@pytest.mark.parametrize("explode", [False, True])
def test_notification_failure_never_breaks_the_task(
    web_db, live_plan, make_scheduler, explode
):
    notifier = RecordingNotifier(explode=explode)
    scheduler = make_scheduler(notifier=notifier)
    scheduler._tick()
    assert wait_for(lambda: repo.list_runs(web_db)), "运行记录尚未创建"
    assert wait_for(lambda: not repo.list_running_runs(web_db))
    run = repo.list_runs(web_db)[0]
    assert run["status"] == RunStatus.SUCCEEDED.value
    assert "notify_booking_success" in notifier.names()


def test_tick_survives_a_notification_that_explodes(web_db, live_plan, make_scheduler):
    """通知故障绝不能杀死调度线程。"""
    notifier = RecordingNotifier(explode=True)
    scheduler = make_scheduler(notifier=notifier)
    scheduler._tick()
    assert wait_for(lambda: not repo.list_running_runs(web_db))
    scheduler._tick()
    assert scheduler.last_error == ""


# ------------------------------------------------------------------ 手动执行


def test_request_run_starts_a_background_thread(web_db, live_plan, make_scheduler):
    scheduler = make_scheduler()
    result = scheduler.request_run(live_plan)
    assert result["ok"], result
    assert wait_for(lambda: repo.list_runs(web_db)), "运行记录尚未创建"
    assert wait_for(lambda: not repo.list_running_runs(web_db))
    assert repo.list_runs(web_db)[0]["trigger"] == "manual"


def test_request_run_refuses_when_global_pause_is_on(web_db, live_plan, make_scheduler):
    repo.set_app_setting(web_db, global_pause=True)
    result = make_scheduler().request_run(live_plan)
    assert not result["ok"]
    assert "全局暂停" in result["message"]
    assert run_count(web_db) == 0


def test_request_run_refuses_unknown_plan(web_db, live_plan, make_scheduler):
    repo.set_plan_status(web_db, live_plan, PlanStatus.UNKNOWN)
    result = make_scheduler().request_run(live_plan)
    assert not result["ok"]
    assert "核实" in result["message"] or "未知" in result["message"]
    assert run_count(web_db) == 0


def test_request_run_refuses_before_trigger(web_db, live_plan, make_scheduler):
    later = (NOW + timedelta(hours=1)).strftime("%H:%M:%S")
    with web_db.tx() as conn:
        conn.execute("UPDATE plan SET trigger_at = ? WHERE id = ?", (later, live_plan))
    result = make_scheduler().request_run(live_plan)
    assert not result["ok"]
    assert "触发时刻" in result["message"]


def test_request_run_refuses_after_window(web_db, live_plan, make_scheduler):
    past = (NOW - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    with web_db.tx() as conn:
        conn.execute("UPDATE plan SET book_end = ? WHERE id = ?", (past, live_plan))
    result = make_scheduler().request_run(live_plan)
    assert not result["ok"]
    assert "窗口" in result["message"]


def test_request_run_refuses_when_auth_is_invalid(web_db, live_plan, make_scheduler):
    result = make_scheduler(auth_blocked=True).request_run(live_plan)
    assert not result["ok"]
    assert "认证" in result["message"]
    assert run_count(web_db) == 0


def test_request_run_refuses_when_booking_is_disabled(web_db, live_plan, make_scheduler):
    result = make_scheduler(enable_real_booking=False).request_run(live_plan)
    assert not result["ok"]
    assert run_count(web_db) == 0


def test_request_run_refuses_missing_plan(web_db, live_plan, make_scheduler):
    result = make_scheduler().request_run(99999)
    assert not result["ok"]


def test_request_run_honours_the_cross_process_lock(web_db, live_plan, make_scheduler):
    """CLI 与 Web 共用**同一把按时段命名的文件锁**：另一个进程在跑同一时段时，手动执行必须被拒绝。

    锁的粒度从「全局一把」改成了「按时段（区域 + 开始 + 结束）一把」——
    但**跨进程**这条性质没有变：仍然是 ``core.lock.FileLock``（``O_CREAT|O_EXCL``），
    仍然由 CLI 的 ``book`` 子命令与 Web 调度器共用同一个路径。
    """
    from haut_seat.core.lock import FileLock

    scheduler = make_scheduler()
    # 锁的是**这条计划所在时段**的锁文件：不同时段的预约本来就不该互相阻塞，
    # 只有真正共享同一个座位时段的任务才需要互斥。
    slot_path = scheduler._book_lock_path(repo.get_plan(web_db, live_plan))
    external = FileLock(slot_path)
    assert external.acquire()
    try:
        result = scheduler.request_run(live_plan)
        assert not result["ok"]
        assert "正在运行" in result["message"]
        assert slot_path in result["message"], "拒绝时要说清是哪把锁挡住的"
    finally:
        external.release()


def test_execute_skips_when_the_cross_process_lock_is_held(
    web_db, live_plan, make_scheduler
):
    """另一个进程正持有同一时段的锁时，``_execute`` 必须**跳过**（不等待、更不提交）。

    ``book_lock_wait_s`` 是 0（见 ``web_settings``），因此这里是「试一次、拿不到就
    放弃」。真实运行时默认会先等最多 5 秒，超时同样放弃 —— 见
    ``test_slot_lock_wait_is_bounded_and_gives_up``。
    """
    from haut_seat.core.lock import FileLock

    scheduler = make_scheduler()
    slot_path = scheduler._book_lock_path(repo.get_plan(web_db, live_plan))
    external = FileLock(slot_path)
    assert external.acquire()
    try:
        assert scheduler._execute(live_plan, trigger="scheduler") is None
        assert run_count(web_db) == 0, "被锁占用时不得创建运行记录，更不得提交"
    finally:
        external.release()


def test_other_slots_are_not_blocked_by_a_held_slot_lock(web_db, live_plan, make_scheduler):
    """**不同时段**的锁互不影响：持有时段 A 的锁，时段 B 的任务照常执行。

    这是本次并发改动的核心诉求（「不同预约任务不应无故争抢一个全局锁」），
    因此必须有测试钉住：否则很容易在未来的重构里又退回「一把全局锁」。
    """
    from haut_seat.core.lock import FileLock
    from haut_seat.services.slot_lock import slot_lock_path

    scheduler = make_scheduler()
    plan = repo.get_plan(web_db, live_plan)
    other_path = str(slot_lock_path(
        scheduler.settings, regionid=plan["regionid"],
        start="2099-01-01 08:00:00", end="2099-01-01 12:00:00",
    ))
    assert other_path != scheduler._book_lock_path(plan)
    external = FileLock(other_path)
    assert external.acquire()
    try:
        # 别的时段的锁被占着，本计划仍然能跑完。
        assert scheduler._execute(live_plan, trigger="scheduler") is not None
        assert run_count(web_db) == 1
    finally:
        external.release()


def test_slot_lock_wait_is_bounded_and_gives_up(web_settings, caplog):
    """锁等待**必须有明确超时**，不能无限等待；超时要落一条日志。

    用一个走得很慢的假时钟验证「等满就放弃」，因此用例本身不真的等待。
    """
    import logging

    from haut_seat.core.lock import FileLock
    from haut_seat.services.slot_lock import acquire_slot_lock, slot_lock_path

    path = slot_lock_path(
        web_settings, regionid=16, start="2099-01-01 08:00:00", end="2099-01-01 12:00:00"
    )
    holder = FileLock(str(path))
    assert holder.acquire()

    ticks = {"n": 0}

    def fake_clock() -> float:
        ticks["n"] += 1
        # 每次看表都往前跳一大步 ⇒ 下一次循环必然已经超时。
        return 1000.0 * ticks["n"]

    logger = logging.getLogger("haut_seat.test.slot_lock")
    try:
        with caplog.at_level(logging.WARNING, logger=logger.name):
            lock = acquire_slot_lock(
                web_settings, regionid=16, start="2099-01-01 08:00:00",
                end="2099-01-01 12:00:00", wait_s=5.0, logger=logger,
                plan_id=42, clock=fake_clock, sleep=lambda s: None,
            )
        assert lock is None, "超时必须放弃，绝不无限等待"
        assert any("时段锁" in r.message for r in caplog.records), \
            "等待超时必须留下日志，否则并发问题无法排查"
    finally:
        holder.release()


# ------------------------------------------------------------------ 重复计划


def test_repeating_plan_is_rearmed_for_a_new_day(web_db, live_plan, make_scheduler):
    old_run = repo.start_run(
        web_db, live_plan, trigger="scheduler", now=NOW - timedelta(days=2)
    )
    repo.finish_run(
        web_db, old_run, status=RunStatus.SUCCEEDED, category=ErrorCategory.NONE,
        stop_reason="success", message="", exit_code=0, plan_status=PlanStatus.SUCCEEDED,
        now=NOW - timedelta(days=2),
    )
    with web_db.tx() as conn:
        conn.execute(
            "UPDATE plan SET repeat_rule = 'daily', status = 'succeeded', enabled = 1,"
            " run_date = ?, updated_at = ? WHERE id = ?",
            ((NOW - timedelta(days=2)).strftime("%Y-%m-%d"),
             (NOW - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"), live_plan),
        )

    scheduler = make_scheduler()
    scheduler._rearm_repeating(NOW.date())
    assert repo.get_plan(web_db, live_plan)["status"] is PlanStatus.PENDING


def test_unknown_plan_is_never_rearmed(web_db, live_plan, make_scheduler):
    old_run = repo.start_run(web_db, live_plan, trigger="scheduler")
    repo.finish_run(
        web_db, old_run, status=RunStatus.UNKNOWN, category=ErrorCategory.SUBMIT_UNKNOWN,
        stop_reason="submit_unknown", message="", exit_code=0,
        plan_status=PlanStatus.UNKNOWN, now=NOW - timedelta(days=2),
    )
    with web_db.tx() as conn:
        conn.execute(
            "UPDATE plan SET repeat_rule = 'daily', run_date = ? WHERE id = ?",
            ((NOW - timedelta(days=2)).strftime("%Y-%m-%d"), live_plan),
        )
    scheduler = make_scheduler()
    scheduler._rearm_repeating(NOW.date())
    assert repo.get_plan(web_db, live_plan)["status"] is PlanStatus.UNKNOWN


# ------------------------------------------------------------------ 其它


def test_pause_and_stop_requests_prevent_new_submissions(web_db, live_plan, make_scheduler):
    """运行中登记暂停/停止后，运行器不再发起新的提交。"""
    scheduler = make_scheduler(
        submit_results=[BookingResult(BookingOutcome.FAILED, "座位被占")],
    )
    repo.resume_plan(web_db, live_plan)
    with web_db.tx() as conn:
        conn.execute("UPDATE plan SET pause_requested = 1 WHERE id = ?", (live_plan,))
    ok, reason = scheduler._should_continue(live_plan)
    assert not ok and "暂停" in reason

    with web_db.tx() as conn:
        conn.execute("UPDATE plan SET pause_requested = 0, stop_requested = 1 WHERE id = ?", (live_plan,))
    ok, reason = scheduler._should_continue(live_plan)
    assert not ok and "停止" in reason


def test_should_continue_follows_global_pause(web_db, live_plan, make_scheduler):
    scheduler = make_scheduler()
    assert scheduler._should_continue(live_plan)[0] is True
    repo.set_app_setting(web_db, global_pause=True)
    ok, reason = scheduler._should_continue(live_plan)
    assert not ok and "全局暂停" in reason


def test_scheduler_status_snapshot(web_db, live_plan, make_scheduler):
    scheduler = make_scheduler()
    status = scheduler.status()
    assert set(status) >= {
        "running", "tick_s", "max_concurrent", "active_plans", "last_tick_at",
        "tick_count", "last_error",
    }
    assert status["running"] is False
    scheduler._tick()
    assert scheduler.status()["tick_count"] == 1


def test_plan_window_state_messages(web_db, live_plan):
    plan = repo.get_plan(web_db, live_plan)
    code, text = plan_window_state(plan)
    assert code in {"open", "waiting", "expired", "not_today", "invalid"}
    assert text

    plan["run_date"] = "2099-01-01"
    plan["repeat_rule"] = "once"
    assert plan_window_state(plan)[0] == "not_today"

    plan["book_start"] = "不是时间"
    plan["run_date"] = NOW.strftime("%Y-%m-%d")
    assert plan_window_state(plan)[0] == "invalid"


def test_start_and_stop_are_idempotent(web_db, make_scheduler):
    scheduler = make_scheduler()
    scheduler.start()
    scheduler.start()
    assert scheduler.is_running()
    scheduler.stop()
    scheduler.stop()
    assert not scheduler.is_running()


# ------------------------------------------------- 重复计划的窗口必须滚动到当天
#
# 这一组守护一个真实缺陷：``_rearm_repeating`` 只把状态改回「待执行」，
# 从不改写 ``book_start`` / ``book_end``（全仓库只有 create_plan / update_plan
# 写这两列）。窗口日期若不滚动，daily 计划在次日一律被 ``now >= end_ts``
# 判成「窗口已过」，永远不再执行 —— 而界面此刻正显示「待执行」，
# 同页两句互相矛盾。原有测试只断言状态翻转，从未验证「次日真的会跑」。


YESTERDAY = NOW - timedelta(days=1)


def _repeating_plan(web_db, *, created_on) -> int:
    """按「创建当天」提交一条每天预约，返回计划 id。

    刻意让 ``book_start`` / ``book_end`` 落在**创建当天** —— 这正是真实库里
    重复计划存储的样子（``parse_plan`` 强制时段日期 == 执行日期，且
    ``start_run`` 之外没有任何地方改写这两列）。
    """
    day = created_on.strftime("%Y-%m-%d")
    form = {
        "name": "每天预约",
        "run_date": day,
        "repeat_rule": "daily",
        "trigger_at": (NOW - timedelta(hours=1)).strftime("%H:%M:%S"),
        "book_start": f"{day} {(NOW + timedelta(minutes=30)).strftime('%H:%M:%S')}",
        "book_end": f"{day} {(NOW + timedelta(hours=3)).strftime('%H:%M:%S')}",
        "regionid": "16",
        "preferred_seats": "A桌 1座",
        "preferred_prefixes": "",
        "confirmed_bookable_states": '[["2", 1, false]]',
        "max_attempts_per_seat": "1",
        "note": "",
    }
    data, _ = parse_plan(form, today=created_on.date())
    plan_id = repo.create_plan(web_db, data, enabled=True)
    repo.set_app_setting(web_db, global_pause=False)
    return plan_id


def _mark_succeeded(db, plan_id: int, *, at: datetime) -> int:
    """把计划做成「当天已成功执行完」的重复计划（次日应被重新排入待执行）。"""
    run_id = repo.start_run(db, plan_id, trigger="scheduler", now=at)
    repo.finish_run(
        db, run_id, status=RunStatus.SUCCEEDED, category=ErrorCategory.NONE,
        stop_reason="success", message="预约成功", exit_code=0,
        booking_id=101, submit_count=1, plan_status=PlanStatus.SUCCEEDED, now=at,
    )
    return run_id


def test_a_daily_plan_really_runs_again_the_next_day(web_db, make_scheduler):
    """缺失的守护：昨天成功的 daily 计划，今天 tick 后必须产生**第 2 条** run。"""
    plan_id = _repeating_plan(web_db, created_on=YESTERDAY)
    _mark_succeeded(web_db, plan_id, at=YESTERDAY)

    plan = repo.get_plan(web_db, plan_id)
    # 前置确认：库里存的窗口确实停在昨天，否则这条测试就没有触到缺陷
    assert plan["book_start"][:10] == YESTERDAY.strftime("%Y-%m-%d")
    assert plan["status"] is PlanStatus.SUCCEEDED

    scheduler = make_scheduler()
    scheduler._tick()

    # 等到「第 2 条 run 已经出现**并且**跑完」。只等条数会在机器负载高时读到一条
    # 仍在运行的记录（线程插入了 run 行但还没 finalize），断言随之随机失败 ——
    # 要断言的是最终状态，等待条件就必须等到最终状态。
    assert wait_for(
        lambda: run_count(web_db) == 2 and not repo.list_running_runs(web_db)
    ), "重复计划次日没有再执行（窗口日期没有滚动）"
    newest = repo.list_runs(web_db, plan_id=plan_id)[0]
    assert newest["started_at"][:10] == TODAY
    assert newest["status"] is RunStatus.SUCCEEDED


def test_a_daily_plan_is_not_expired_on_the_next_day(web_db):
    """消除「待执行 + 已过预约窗口」并存的自相矛盾。"""
    plan_id = _repeating_plan(web_db, created_on=YESTERDAY)
    _mark_succeeded(web_db, plan_id, at=YESTERDAY)
    plan = repo.get_plan(web_db, plan_id)

    # 存储窗口的结束时刻早已过去：滚动之前它必然被判成 expired
    assert ts_parse(plan["book_end"]) < NOW.timestamp()

    code, text = plan_window_state(plan, now=NOW)
    assert code == "open", text


def test_the_window_state_rolls_the_trigger_day_too(web_db):
    """触发时刻的日期同样取自滚动后的窗口，不会出现「窗口滚了、触发没滚」。"""
    plan_id = _repeating_plan(web_db, created_on=YESTERDAY)
    later = (NOW + timedelta(hours=2)).strftime("%H:%M:%S")
    with web_db.tx() as conn:
        conn.execute("UPDATE plan SET trigger_at = ? WHERE id = ?", (later, plan_id))

    code, text = plan_window_state(repo.get_plan(web_db, plan_id), now=NOW)
    assert code == "waiting", text
    assert "已过" not in text


def test_the_submitted_window_uses_the_rolled_date(web_db, web_settings):
    """提交给图书馆的 starttime / endtime 必须是**当天**的窗口日期。

    ``make_scheduler`` 的 query_fn / submit_fn 是固定替身、无法注入观察者，
    因此这里手工构造调度器，用两个替身把真正发出去的参数记下来。
    """
    plan_id = _repeating_plan(web_db, created_on=YESTERDAY)
    seen: dict[str, str] = {}

    def query_fn(settings_, regionid, start, end, page_num, page_size):
        seen.setdefault("query_start", start)
        seen.setdefault("query_end", end)
        return layout(seat(1, "A桌 1座"))

    def submit_fn(settings_, target_seat, submit_start, end):
        seen.setdefault("submit_start", submit_start)
        seen.setdefault("submit_end", end)
        return BookingResult(BookingOutcome.SUCCESS, "预约成功", booking_id=7)

    settings = web_settings.model_copy(update={"enable_real_booking": True})
    scheduler = Scheduler(
        web_db,
        settings,
        query_fn=query_fn,
        submit_fn=submit_fn,
        auth_check_fn=lambda s, r, start=None, end=None: AuthCheck(AuthStatus.VALID, "ok"),
        clock=lambda: NOW,
        time_mod=FakeTime(NOW.timestamp()),
        wait_fn=lambda target_ts: None,
    )
    try:
        scheduler._tick()
        assert wait_for(lambda: "submit_start" in seen), "预期发生一次提交"
    finally:
        scheduler.stop(timeout=1.0)

    # 库里存的仍是建计划那天的日期，而发出去的必须是今天
    assert repo.get_plan(web_db, plan_id)["book_start"][:10] == YESTERDAY.strftime("%Y-%m-%d")
    assert seen["query_start"][:10] == TODAY
    assert seen["query_end"][:10] == TODAY
    assert seen["submit_start"][:10] == TODAY
    assert seen["submit_end"][:10] == TODAY


# ------------------------------------------------- 提前启动 / 已过使用开始时间 / 同日防重复
#
# 三条修复共同守一个不变量：**同一天最多提交一次**，且只在真正还有机会的时候启动。
#   M1 提前启动：线程可以早 ``trigger_lead_s`` 秒起来跑认证预检，但查询与提交仍必须
#      发生在触发时刻之后 —— 默认 0，不配置时行为与从前一致。
#   M2 使用开始时间已到：不再启动（那种运行必然被服务器拒绝，只会留下一条没意义的
#      失败记录）。「触发时刻已过」与「座位使用开始时间已过」必须分开处理。
#   M3 同日防重复：今天已经成功过、或已经提交过的计划，自动与手动都不得再来一次。
#
# 全部离线：替身查询/提交，不发起任何真实预约。


class MutableClock:
    """可推进的时钟，并记录每个动作发生在哪一刻（同时充当 ``time_mod``）。

    ``booking_runner`` 的 ``_now()`` 就是 ``time_mod.time()``，所以把同一个对象同时
    交给 ``clock`` 与 ``time_mod``，替身看到的「现在」与调度器完全一致。
    """

    def __init__(self, now: datetime):
        self.now = now
        self.events: list[tuple[str, datetime]] = []
        self.wait_targets: list[datetime] = []  # 每次「等到某个时刻」的目标时刻

    def set(self, now: datetime) -> None:
        self.now = now

    def mark(self, name: str) -> None:
        self.events.append((name, self.now))

    def at(self, name: str) -> datetime:
        for event, when in self.events:
            if event == name:
                return when
        raise AssertionError(f"没有记录到动作 {name!r}（实际记录 {[e[0] for e in self.events]}）")

    # --- time_mod 接口 ---
    def time(self) -> float:
        return self.now.timestamp()

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _seat_window_plan(web_db, *, trigger_at: datetime, book_start: datetime, book_end: datetime) -> int:
    """建一条「单次」计划，时段与触发时刻都由用例精确指定。"""
    form = {
        "name": "提前启动测试",
        "run_date": book_start.strftime("%Y-%m-%d"),
        "repeat_rule": "once",
        "trigger_at": trigger_at.strftime("%H:%M:%S"),
        "book_start": book_start.strftime("%Y-%m-%d %H:%M:%S"),
        "book_end": book_end.strftime("%Y-%m-%d %H:%M:%S"),
        "regionid": "16",
        "preferred_seats": "A桌 1座",
        "preferred_prefixes": "",
        "confirmed_bookable_states": '[["2", 1, false]]',
        "max_attempts_per_seat": "1",
        "note": "",
    }
    data, errors = parse_plan(form, today=NOW.date())
    assert not errors, errors
    plan_id = repo.create_plan(web_db, data, enabled=True)
    repo.set_plan_status(web_db, plan_id, PlanStatus.PENDING)
    repo.set_app_setting(web_db, global_pause=False)
    return plan_id


def _lead_scheduler(web_db, web_settings, clock: MutableClock, *, trigger_lead_s: float):
    """手工构造的调度器：可变时钟 + 逐动作记录的替身（提前量无法从夹具注入）。"""
    settings = web_settings.model_copy(update={"enable_real_booking": True})
    results = iter([BookingResult(BookingOutcome.SUCCESS, "预约成功", booking_id=101)])

    def auth_check_fn(settings_, regionid, start=None, end=None):
        clock.mark("auth")
        return AuthCheck(AuthStatus.VALID, "ok")

    def query_fn(settings_, regionid, start, end, page_num, page_size):
        clock.mark("query")
        return layout(seat(1, "A桌 1座"))

    def submit_fn(settings_, target_seat, submit_start, end):
        clock.mark("submit")
        try:
            return next(results)
        except StopIteration:  # pragma: no cover
            return BookingResult(BookingOutcome.FAILED, "无更多替身返回值")

    def wait_fn(target_ts: float) -> None:
        clock.mark("wait")
        clock.wait_targets.append(datetime.fromtimestamp(target_ts))
        clock.set(datetime.fromtimestamp(target_ts))  # 模拟真实等待：睡到触发时刻

    return Scheduler(
        web_db,
        settings,
        tick_s=0.05,
        query_fn=query_fn,
        submit_fn=submit_fn,
        auth_check_fn=auth_check_fn,
        auth_service=StubAuthService(),
        clock=lambda: clock.now,
        time_mod=clock,
        wait_fn=wait_fn,
        trigger_lead_s=trigger_lead_s,
    )


def test_trigger_lead_does_the_auth_precheck_before_the_trigger(web_db, web_settings):
    """提前启动：预检发生在触发时刻**之前**，查询/提交仍只在触发时刻之后。"""
    trigger = NOW + timedelta(minutes=10)
    _seat_window_plan(
        web_db, trigger_at=trigger,
        book_start=NOW + timedelta(minutes=30), book_end=NOW + timedelta(hours=3),
    )
    clock = MutableClock(trigger - timedelta(minutes=1))  # 提前量 120s，此刻已可启动
    scheduler = _lead_scheduler(web_db, web_settings, clock, trigger_lead_s=120.0)
    try:
        scheduler._tick()
        assert wait_for(lambda: repo.list_runs(web_db) and not repo.list_running_runs(web_db)), (
            "运行没有跑完"
        )
    finally:
        scheduler.stop(timeout=1.0)

    assert repo.list_runs(web_db)[0]["status"] is RunStatus.SUCCEEDED
    assert clock.at("auth") < trigger, "认证预检必须赶在触发时刻之前完成"
    assert clock.wait_targets == [trigger], "必须等到触发时刻，不得提前查询"
    assert clock.at("query") >= trigger, "触发时刻之前不得查询座位"
    assert clock.at("submit") >= trigger, "触发时刻之前不得提交预约"


def test_trigger_lead_waits_until_the_lead_window_opens(web_db, web_settings):
    """提前量只是把启动门槛前移，不能提前得更早。"""
    trigger = NOW + timedelta(minutes=10)
    _seat_window_plan(
        web_db, trigger_at=trigger,
        book_start=NOW + timedelta(minutes=30), book_end=NOW + timedelta(hours=3),
    )
    clock = MutableClock(trigger - timedelta(seconds=150))  # 比「触发 - 120s」早 30 秒
    scheduler = _lead_scheduler(web_db, web_settings, clock, trigger_lead_s=120.0)
    try:
        scheduler._tick()
        time.sleep(0.1)
        assert run_count(web_db) == 0, "还没到提前量的边界就启动了"

        clock.set(trigger - timedelta(seconds=120))
        scheduler._tick()
        assert wait_for(lambda: repo.list_runs(web_db)), "到了提前量的边界仍不启动"
        assert wait_for(lambda: not repo.list_running_runs(web_db))
    finally:
        scheduler.stop(timeout=1.0)

    assert clock.at("auth") == trigger - timedelta(seconds=120)
    assert clock.at("query") >= trigger


def test_default_trigger_lead_is_zero_and_changes_nothing(web_db, web_settings):
    """不配置提前量（默认 0）时，触发时刻之前一个字节都不动。"""
    trigger = NOW + timedelta(minutes=10)
    _seat_window_plan(
        web_db, trigger_at=trigger,
        book_start=NOW + timedelta(minutes=30), book_end=NOW + timedelta(hours=3),
    )
    clock = MutableClock(trigger - timedelta(minutes=1))
    scheduler = _lead_scheduler(web_db, web_settings, clock, trigger_lead_s=0.0)
    try:
        assert scheduler.trigger_lead_s == 0.0
        scheduler._tick()
        time.sleep(0.1)
        assert run_count(web_db) == 0, "默认配置下触发时刻之前不得启动"

        clock.set(trigger)
        scheduler._tick()
        assert wait_for(lambda: repo.list_runs(web_db)), "到点后应当启动"
        assert wait_for(lambda: not repo.list_running_runs(web_db))
    finally:
        scheduler.stop(timeout=1.0)


def test_trigger_lead_never_submits_before_the_official_open_moment(web_db, web_settings):
    """提前启动 + 触发时刻早于 07:00：等到 07:00 才查询并提交，绝不提前。

    这是「调度器提前把任务拉起来」这条真实路径的端到端版本（真锁、真调度、
    真运行器，只有查询/提交是替身）：认证预检可以、也应当赶在触发时刻之前做完；
    查询不早于触发时刻；而**提交**必须等到官方开放时刻（07:00）—— 即使计划的
    触发时刻本身设在 06:55，也不允许把提交提前到开放之前。
    """
    day = NOW.date()
    trigger = datetime.combine(day, dtime(6, 55))
    open_moment = datetime.combine(day, dtime(7, 0))
    _seat_window_plan(
        web_db, trigger_at=trigger,
        book_start=datetime.combine(day, dtime(8, 0)),
        book_end=datetime.combine(day, dtime(12, 0)),
    )
    clock = MutableClock(trigger - timedelta(minutes=1))  # 提前量 120s，此刻已可启动
    scheduler = _lead_scheduler(web_db, web_settings, clock, trigger_lead_s=120.0)
    try:
        scheduler._tick()
        assert wait_for(lambda: repo.list_runs(web_db) and not repo.list_running_runs(web_db)), (
            "运行没有跑完"
        )
    finally:
        scheduler.stop(timeout=1.0)

    assert repo.list_runs(web_db)[0]["status"] is RunStatus.SUCCEEDED
    assert clock.at("auth") < trigger, "认证预检必须赶在触发时刻之前完成"
    assert clock.at("query") >= trigger, "触发时刻之前不得查询座位"
    assert clock.at("submit") >= open_moment, "开放时刻（07:00）之前绝不能提交预约"
    # 等待期间确实重新查询过：不是拿着开放前那一份候选直接提交
    queries = [when for name, when in clock.events if name == "query"]
    assert len(queries) >= 2 and any(when >= open_moment for when in queries)


@pytest.mark.parametrize(
    "start_offset, end_offset, should_start, why",
    [
        (timedelta(minutes=30), timedelta(hours=3), True, "触发已过但使用开始时间未到 → 允许补跑"),
        (timedelta(0), timedelta(hours=2), False, "使用开始时间恰好到达 → 不启动"),
        (-timedelta(minutes=1), timedelta(hours=2), False, "使用开始时间已过 → 不启动"),
        (-timedelta(hours=2), timedelta(hours=1), False, "使用开始时间已过很久 → 不启动"),
        (-timedelta(hours=2), -timedelta(minutes=1), False, "窗口已关闭 → 不启动"),
    ],
)
def test_a_plan_whose_seat_time_already_started_is_not_started(
    web_db, live_plan, make_scheduler, start_offset, end_offset, should_start, why
):
    """M2：座位使用时间已到 ⇒ 不启动，且**不留下**没意义的失败运行记录。"""
    plan_id = live_plan
    with web_db.tx() as conn:
        conn.execute(
            "UPDATE plan SET book_start = ?, book_end = ? WHERE id = ?",
            (
                (NOW + start_offset).strftime("%Y-%m-%d %H:%M:%S"),
                (NOW + end_offset).strftime("%Y-%m-%d %H:%M:%S"),
                plan_id,
            ),
        )
    scheduler = make_scheduler()
    scheduler._tick()

    if should_start:
        assert wait_for(lambda: run_count(web_db) == 1), why
        assert wait_for(lambda: not repo.list_running_runs(web_db))
        assert repo.list_runs(web_db)[0]["status"] is RunStatus.SUCCEEDED
    else:
        time.sleep(0.1)
        assert run_count(web_db) == 0, why
        assert repo.get_plan(web_db, plan_id)["status"] is PlanStatus.PENDING, (
            "不启动时计划状态必须保持原样"
        )


def test_a_daily_plan_still_runs_after_the_seat_window_rolls(web_db, make_scheduler):
    """滚动之后使用开始时间仍在未来 ⇒ 照常执行（M2 不得误伤重复计划）。"""
    plan_id = _repeating_plan(web_db, created_on=YESTERDAY)
    _mark_succeeded(web_db, plan_id, at=YESTERDAY)

    scheduler = make_scheduler()
    scheduler._tick()
    assert wait_for(lambda: run_count(web_db) == 2), "重复计划被 M2 误伤"
    # 必须等第二个运行真正跑完再断言终态：run_count 只数行数，
    # 行一插入就为真，此刻状态完全可能还是 running（与上面那条参数化用例同样的等待）。
    assert wait_for(lambda: not repo.list_running_runs(web_db)), "第二个运行没有跑完"
    assert repo.list_runs(web_db, plan_id=plan_id)[0]["status"] is RunStatus.SUCCEEDED


def test_a_second_run_is_not_started_after_todays_success(web_db, live_plan, make_scheduler):
    """M3：今天已经成功过，即使状态被人为改回「待执行」也不得再来一次。"""
    scheduler = make_scheduler()
    scheduler._tick()
    assert wait_for(lambda: repo.list_runs(web_db)), "运行记录尚未创建"
    assert wait_for(lambda: not repo.list_running_runs(web_db))
    assert run_count(web_db) == 1
    assert repo.get_plan(web_db, live_plan)["status"] is PlanStatus.SUCCEEDED

    repo.set_plan_status(web_db, live_plan, PlanStatus.PENDING)  # 模拟误操作 / 恢复
    scheduler._tick()
    time.sleep(0.1)
    assert run_count(web_db) == 1, "同一天不得重复提交"


def test_manual_run_is_refused_after_todays_success(web_db, live_plan, make_scheduler):
    scheduler = make_scheduler()
    scheduler._tick()
    assert wait_for(lambda: repo.list_runs(web_db)), "运行记录尚未创建"
    assert wait_for(lambda: not repo.list_running_runs(web_db))

    result = scheduler.request_run(live_plan)
    assert not result["ok"], result
    assert "今天已经执行过" in result["message"]
    assert run_count(web_db) == 1


def test_manual_run_is_refused_after_an_acknowledged_unknown_submit(web_db, live_plan, make_scheduler):
    """结果未知但已人工核实：仍然不许重放（今天已经提交过），自动路径同样拦。"""
    run_id = _start_and_leave_running(web_db, live_plan, submit_count=1)
    repo.finish_run(
        web_db, run_id, status=RunStatus.UNKNOWN, category=ErrorCategory.SUBMIT_UNKNOWN,
        stop_reason="submit_unknown", message="结果未知", exit_code=0,
        submit_count=1, plan_status=PlanStatus.UNKNOWN,
    )
    resumed = repo.resume_plan(web_db, live_plan, acknowledge_unknown=True)
    assert resumed["ok"], resumed
    assert repo.get_plan(web_db, live_plan)["status"] is PlanStatus.PENDING

    scheduler = make_scheduler()
    result = scheduler.request_run(live_plan)
    assert not result["ok"], result
    assert "今天已经执行过" in result["message"]
    assert run_count(web_db) == 1

    scheduler._tick()
    time.sleep(0.1)
    assert run_count(web_db) == 1, "已经提交过的运行不得自动重放"


def test_manual_run_is_still_allowed_when_there_is_no_run_today(web_db, live_plan, make_scheduler):
    """今天还没有跑过（昨天跑过）⇒ 手动执行必须仍然可用，防护不得过度拦截。"""
    _mark_succeeded(web_db, live_plan, at=YESTERDAY)

    scheduler = make_scheduler()
    result = scheduler.request_run(live_plan)
    assert result["ok"], result
    assert wait_for(lambda: run_count(web_db) == 2), "手动执行没有产生新的运行"
    assert wait_for(lambda: not repo.list_running_runs(web_db))
    assert repo.list_runs(web_db, plan_id=live_plan)[0]["trigger"] == "manual"


def test_a_daily_plan_runs_again_the_next_day_even_after_todays_success(web_db, web_settings):
    """M3 的防护按「当天」判定：今天成功过，明天照样要跑。"""
    plan_id = _repeating_plan(web_db, created_on=NOW)
    _mark_succeeded(web_db, plan_id, at=NOW)  # 今天已经成功并提交
    tomorrow = NOW + timedelta(days=1)
    seen: dict[str, str] = {}

    settings = web_settings.model_copy(update={"enable_real_booking": True})

    def query_fn(settings_, regionid, start, end, page_num, page_size):
        seen.setdefault("query_start", start)
        return layout(seat(1, "A桌 1座"))

    def submit_fn(settings_, target_seat, submit_start, end):
        seen.setdefault("submit_start", submit_start)
        return BookingResult(BookingOutcome.SUCCESS, "预约成功", booking_id=7)

    scheduler = Scheduler(
        web_db,
        settings,
        query_fn=query_fn,
        submit_fn=submit_fn,
        auth_check_fn=lambda s, r, start=None, end=None: AuthCheck(AuthStatus.VALID, "ok"),
        clock=lambda: tomorrow,
        time_mod=FakeTime(tomorrow.timestamp()),
        wait_fn=lambda target_ts: None,
    )
    try:
        scheduler._tick()
        assert wait_for(lambda: run_count(web_db) == 2), "次日没有再执行"
        assert wait_for(lambda: not repo.list_running_runs(web_db))
    finally:
        scheduler.stop(timeout=1.0)

    assert repo.list_runs(web_db, plan_id=plan_id)[0]["status"] is RunStatus.SUCCEEDED
    # 真正发出去的时段必须是「明天」的日期（既有窗口滚动语义没有被 M3 破坏）
    assert seen["submit_start"][:10] == tomorrow.strftime("%Y-%m-%d")


def test_a_repeating_plan_interrupted_before_submit_can_still_run_today(web_db, make_scheduler):
    """M1 的安全恢复：提交前被打断的重复计划当天重新排入待执行，不白扔一天。"""
    plan_id = _repeating_plan(web_db, created_on=NOW)
    _start_and_leave_running(web_db, plan_id, submit_count=0)

    scheduler = make_scheduler()
    recovered = scheduler.recover_after_restart()
    assert [r["submitted"] for r in recovered] == [False]
    assert repo.get_plan(web_db, plan_id)["status"] is PlanStatus.FAILED

    scheduler._tick()
    assert wait_for(lambda: run_count(web_db) == 2), "提交前中断的重复计划今天没有再跑"
    assert wait_for(lambda: not repo.list_running_runs(web_db))
    assert repo.list_runs(web_db, plan_id=plan_id)[0]["status"] is RunStatus.SUCCEEDED


def test_a_repeating_plan_interrupted_after_submit_is_never_rearmed_today(web_db, make_scheduler):
    """已经提交过就绝不重放 —— 宁可白扔一天，也不能重复预约。"""
    plan_id = _repeating_plan(web_db, created_on=NOW)
    _start_and_leave_running(web_db, plan_id, submit_count=1)

    scheduler = make_scheduler()
    recovered = scheduler.recover_after_restart()
    assert [r["submitted"] for r in recovered] == [True]
    assert repo.get_plan(web_db, plan_id)["status"] is PlanStatus.UNKNOWN

    for _ in range(2):
        scheduler._tick()
    time.sleep(0.1)
    assert run_count(web_db) == 1, "已经提交过的运行绝不能被重放"


def test_the_window_badge_agrees_with_the_scheduler_when_the_seat_time_started(
    web_db, live_plan, make_scheduler
):
    """界面提示与调度器判定必须同源：座位使用时间已到，两边都不能说「已开放」。"""
    with web_db.tx() as conn:
        conn.execute(
            "UPDATE plan SET book_start = ?, book_end = ? WHERE id = ?",
            (
                (NOW - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),
                (NOW + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
                live_plan,
            ),
        )
    code, text = plan_window_state(repo.get_plan(web_db, live_plan), now=NOW)
    assert code == "expired", text
    assert "座位使用时间" in text, text

    scheduler = make_scheduler()
    scheduler._tick()
    time.sleep(0.1)
    assert run_count(web_db) == 0, "界面说不会再执行，调度器就必须同样不启动"