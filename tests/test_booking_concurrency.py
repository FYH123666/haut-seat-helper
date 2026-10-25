"""并发执行与跨层数据一致性测试。

**全部离线**：执行器是替身（查询返回固定座位、提交返回固定结果）、数据库是临时库、
只在「凭据隔离」两条用例里由 ``httpx.MockTransport`` 接管传输层 ——
不发起任何真实网络请求，更不发起任何真实预约提交。

覆盖需求第 5 节点名的并发条目：

- 两个**不冲突**的任务重叠执行，而不是被一把全局锁串起来；
- 同一时段（区域 + 起止）的冲突仍被串行化，而且冲突方**不丢**，前一个结束后照常执行；
- 一个任务卡在查询里 / 查询直接抛超时，都不会阻塞或污染其他任务；
- 同一计划不会被启动两次（并发的手动执行请求只有一个能过）；
- 并发的用户端请求不会破坏同一计划的运行记录；
- 不同任务不会错误共享 Cookie / Authorization / 会话（HTTP 客户端按凭据指纹分桶）；
- 并发池达上限时不产生无界线程、也不堆积待处理任务；
- 触发时刻不被其他任务明显延迟（**真实测量**从 tick 到开始运行的墙上时间）；
- 并发闸门没有放松 UNKNOWN / 今天已提交 / 重启恢复这三道保护。

关于「重叠」的判定方式：替身查询函数进入时给闸门 ``Gate`` 记一次在飞计数，
因此 ``gate.peak >= 2`` 就是**两个运行真的同时停在查询里**的直接证据 ——
如果任务被串行化，第二个线程根本进不到查询函数，peak 永远是 1。
"""

from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime, timedelta

import httpx
import pytest

from haut_seat.core.auth import AuthCheck, AuthStatus
from haut_seat.models.booking import BookingOutcome, BookingResult
from haut_seat.models.seat import LayoutQueryResult
from haut_seat.services import io
from haut_seat.services.auth_store import Credentials
from haut_seat.services.scheduler import Scheduler
from haut_seat.services.state import ErrorCategory, PlanStatus, RunStatus
from haut_seat.web import repository as repo
from haut_seat.web.validation import parse_plan

#: 固定「现在」为当天的 10:00：调度的「到点 / 过窗口」判断与真实运行时刻无关。
#: 预约窗口校验只允许 07:00–21:30，因此所有时段都落在这一天之内。
NOW = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
TODAY = NOW.strftime("%Y-%m-%d")

ORIGIN = "http://testserver"  # TestClient 的 Host，同源校验必须用它
COOKIE = "haut_booking_csrf"
_CSRF_RE = re.compile(r'name="_csrf" value="([^"]+)"')

CONFIRMED = '[["2", 1, false]]'


# ================================================================== 替身与工具


class FakeTime:
    """替身时钟：``sleep`` 只把时间往前拨，绝不真的等待。"""

    def __init__(self, now: float):
        self.now = now

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class StubAuthService:
    def __init__(self, blocked: bool = False):
        self.blocked = blocked

    def blocks_submissions(self) -> bool:
        return self.blocked

    def current(self):
        return None


class Gate:
    """可放行的闸门，同时统计「同时在飞」的峰值。

    ``peak`` 是并发是否真的发生的证据；``entered`` 用来区分「没重叠」与「压根没跑」。
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0
        self.entered = 0

    def pass_through(self) -> bool:
        """在飞 +1 → 等待放行 → 在飞 -1。返回是否被放行（False = 等超时了）。"""
        with self._lock:
            self.entered += 1
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        ok = self._event.wait(timeout=10.0)
        with self._lock:
            self.in_flight -= 1
        return ok

    def release(self) -> None:
        self._event.set()


def seat(sid: int, name: str) -> dict:
    return {"id": sid, "seatName": name, "seatStatus": "2", "isCan": 1, "open": False}


def layout(*seats) -> LayoutQueryResult:
    return LayoutQueryResult.from_response({"layout": None, "seatList": list(seats)})


def always_one_seat() -> LayoutQueryResult:
    return layout(seat(1, "A桌 1座"))


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def wait_quiet(db, *, at_least: int = 1, timeout: float = 10.0) -> bool:
    """等到「至少出现过 ``at_least`` 条运行记录」且「当前没有运行中的记录」。

    不能只用「没有运行中的记录」当条件：运行线程还没起来、甚至运行记录还没创建时，
    它立刻为真，断言就会跑在调度前面 —— 这类竞态表现为随机失败。
    """
    def finished() -> bool:
        if repo.list_running_runs(db):
            return False
        return len(repo.list_runs(db, limit=500)) >= at_least

    return wait_for(finished, timeout=timeout)


def wait_done(db, *plan_ids: int, timeout: float = 10.0) -> bool:
    """等到这些计划都产生过运行记录、且当前没有任何「运行中」的记录。"""
    def finished() -> bool:
        if repo.list_running_runs(db):
            return False
        return all(repo.list_runs(db, plan_id=pid, limit=500) for pid in plan_ids)

    return wait_for(finished, timeout=timeout)


def add_plan(
    db,
    *,
    name: str,
    regionid: int = 16,
    start_min: int = 30,
    end_min: int = 180,
    trigger: str | None = None,
    enabled: bool = True,
) -> int:
    """建一条「触发时刻已过、座位时段尚未开始、窗口未关闭」的计划。

    时段由 ``start_min`` / ``end_min`` 相对 ``NOW`` 给出，因此**两条计划的时段
    是否冲突完全由参数决定** —— 需要不冲突就传不同的 ``start_min``（时段锁的
    粒度是区域 + 起止时刻）。
    """
    form = {
        "name": name,
        "run_date": TODAY,
        "repeat_rule": "once",
        "trigger_at": trigger or (NOW - timedelta(hours=1)).strftime("%H:%M:%S"),
        "book_start": (NOW + timedelta(minutes=start_min)).strftime("%Y-%m-%d %H:%M:%S"),
        "book_end": (NOW + timedelta(minutes=end_min)).strftime("%Y-%m-%d %H:%M:%S"),
        "regionid": str(regionid),
        "preferred_seats": "A桌 1座",
        "preferred_prefixes": "",
        "confirmed_bookable_states": CONFIRMED,
        "max_attempts_per_seat": "1",
        "note": "",
    }
    data, _warnings = parse_plan(form, today=NOW.date())
    plan_id = repo.create_plan(db, data, enabled=enabled)
    if enabled:
        repo.set_plan_status(db, plan_id, PlanStatus.PENDING)
    repo.set_app_setting(db, global_pause=False)
    return plan_id


def run_count(db, plan_id: int | None = None) -> int:
    return len(repo.list_runs(db, plan_id=plan_id, limit=500))


def seed_finished_run(
    db,
    plan_id: int,
    *,
    status: RunStatus,
    submit_count: int = 0,
    plan_status: PlanStatus | None = None,
) -> int:
    """补一条「今天已经跑过」的记录，用来验证各种保护在并发下依然生效。"""
    run_id = repo.start_run(db, plan_id, trigger="scheduler")
    if submit_count:
        with db.tx() as conn:
            conn.execute("UPDATE run SET submit_count = ? WHERE id = ?", (submit_count, run_id))
    repo.finish_run(
        db,
        run_id,
        status=status,
        category=ErrorCategory.SUBMIT_UNKNOWN
        if status is RunStatus.UNKNOWN
        else ErrorCategory.NONE,
        stop_reason="test",
        message="测试预置记录",
        exit_code=0,
        submit_count=submit_count,
        plan_status=plan_status,
    )
    return run_id


# ================================================================== 夹具


@pytest.fixture
def make_conc_scheduler(web_db, web_settings):
    """构造注入了替身的调度器（离线、不自启后台线程，由用例显式 ``_tick``）。"""
    made: list[Scheduler] = []

    def _make(
        *,
        query_fn=None,
        submit_fn=None,
        max_concurrent: int = 4,
        auth_blocked: bool = False,
        **kwargs,
    ):
        settings = web_settings.model_copy(update={"enable_real_booking": True})
        fake_time = FakeTime(NOW.timestamp())

        def _query(s, regionid, start, end, page_num, page_size):
            return always_one_seat()

        def _submit(s, target_seat, submit_start, end):
            return BookingResult(BookingOutcome.SUCCESS, "预约成功", booking_id=101)

        scheduler = Scheduler(
            web_db,
            settings,
            tick_s=0.05,
            max_concurrent=max_concurrent,
            query_fn=query_fn or _query,
            submit_fn=submit_fn or _submit,
            auth_check_fn=lambda s, r, start=None, end=None: AuthCheck(
                AuthStatus.VALID, "测试替身：凭据有效"
            ),
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
        # 先给在飞的任务一点收尾时间（闸门总会被放行，最多 Gate 自己的等待上限），
        # 免得把还在跑的线程留给后面的用例 —— 那会让别的用例变慢、变随机。
        wait_for(lambda: scheduler.active_plan_ids() == [], timeout=2.0)
        scheduler.stop(timeout=1.0)


# ================================================================== 1. 真正的并发


def test_different_slots_overlap_instead_of_serializing(web_db, make_conc_scheduler):
    """两个不冲突的任务**重叠**执行，而不是被一把全局锁串成一条线。

    这是本轮并发改造的核心断言：如果锁的粒度是全局的（或调度器在主循环里
    同步等待完整预约流程），第二个线程永远进不到查询函数，``gate.peak`` 就是 1。
    """
    first = add_plan(web_db, name="东区 10:30", regionid=16, start_min=30, end_min=120)
    second = add_plan(web_db, name="西区 11:00", regionid=17, start_min=60, end_min=180)

    gate = Gate()
    scheduler = make_conc_scheduler(
        query_fn=lambda *a, **k: (gate.pass_through(), always_one_seat())[1],
        max_concurrent=2,
    )

    # 两条计划的时段锁必须是**两把不同的锁**，否则「不冲突」这个前提就不成立。
    plans = [repo.get_plan(web_db, first), repo.get_plan(web_db, second)]
    assert scheduler._book_lock_path(plans[0]) != scheduler._book_lock_path(plans[1]), (
        "不同时段却算出同一把锁，并发不可能发生"
    )

    scheduler._tick()
    try:
        assert wait_for(lambda: gate.peak >= 2), (
            f"两个不冲突的计划没有同时执行（同时在飞峰值 {gate.peak}）："
            "预约任务被串行化了"
        )
    finally:
        gate.release()

    assert wait_done(web_db, first, second)
    assert run_count(web_db) == 2
    assert repo.get_plan(web_db, first)["status"] is PlanStatus.SUCCEEDED
    assert repo.get_plan(web_db, second)["status"] is PlanStatus.SUCCEEDED


def test_same_slot_conflicts_are_serialized_and_not_lost(web_db, make_conc_scheduler):
    """同时段冲突仍被串行化，但冲突方**不丢**：前一个结束后照常执行。

    区间锁不能因为「要并发」就取消 —— 同一时段向学校提交两次预约是真实风险。
    """
    first = add_plan(web_db, name="同一时段 A", regionid=16, start_min=30, end_min=180)
    second = add_plan(web_db, name="同一时段 B", regionid=16, start_min=30, end_min=180)

    gate = Gate()
    scheduler = make_conc_scheduler(
        query_fn=lambda *a, **k: (gate.pass_through(), always_one_seat())[1],
        max_concurrent=4,
    )
    plans = [repo.get_plan(web_db, first), repo.get_plan(web_db, second)]
    assert scheduler._book_lock_path(plans[0]) == scheduler._book_lock_path(plans[1])

    scheduler._tick()
    assert wait_for(lambda: gate.entered >= 1)
    # 并发上限是 4，但同一时段只允许一个：这一轮只创建一条运行记录 —— 另一个要么被
    # 只读探测挡下，要么在权威获取处放弃（下一轮 tick 会重试，不排队、不等待）。
    # 因此这里不断言「谁先跑」，只断言「没有两个一起跑」。
    assert wait_for(lambda: run_count(web_db) == 1)
    time.sleep(0.2)  # 给另一个计划一个「也一起启动」的机会
    assert run_count(web_db) == 1, "同一时段的两个计划同时启动了"

    gate.release()
    assert wait_quiet(web_db, at_least=1)
    assert gate.peak == 1, "同一时段的两个运行重叠了"

    # 前一个结束后，第二个在下一轮 tick 照常执行 —— 冲突只延后，不丢。
    for _ in range(200):
        if run_count(web_db) == 2 and not repo.list_running_runs(web_db):
            break
        scheduler._tick()
        time.sleep(0.02)

    assert run_count(web_db) == 2, "同一时段的第二个计划被丢掉了"
    assert gate.peak == 1
    assert repo.get_plan(web_db, first)["status"] is PlanStatus.SUCCEEDED
    assert repo.get_plan(web_db, second)["status"] is PlanStatus.SUCCEEDED


def test_a_hung_query_does_not_block_another_plan(web_db, make_conc_scheduler):
    """一个任务卡在查询里（模拟网络长时间无响应），另一个任务照常跑完。"""
    slow = add_plan(web_db, name="卡住的", regionid=16, start_min=30, end_min=120)
    fast = add_plan(web_db, name="正常的", regionid=17, start_min=60, end_min=180)

    stuck = Gate()

    def query_fn(s, regionid, start, end, page_num, page_size):
        if int(regionid) == 16:
            stuck.pass_through()
        return always_one_seat()

    scheduler = make_conc_scheduler(query_fn=query_fn, max_concurrent=2)
    scheduler._tick()
    try:
        # 先确认卡住的那个真的已经进到查询里（它一进查询就被闸门挡住），
        # 否则「另一个跑完了」可能只是因为它还没开始。
        assert wait_for(lambda: run_count(web_db, slow) == 1), "卡住的任务没有启动"
        assert wait_for(
            lambda: repo.get_plan(web_db, fast)["status"] is PlanStatus.SUCCEEDED
        ), "一个卡住的查询把另一个计划也拖住了"
        # 卡住的那个仍在运行中（没被超时误判成失败，也没被提前终结）
        assert repo.get_plan(web_db, slow)["status"] is PlanStatus.RUNNING
        assert slow in scheduler.active_plan_ids()
        assert run_count(web_db, slow) == 1
    finally:
        stuck.release()

    assert wait_done(web_db, slow)
    assert repo.get_plan(web_db, slow)["status"] is PlanStatus.SUCCEEDED


def test_a_query_timeout_only_fails_its_own_plan(web_db, make_conc_scheduler):
    """查询直接抛网络超时：只影响自己那条计划，另一条照常成功。

    被卡的那条计划窗口很短（10:30–10:33），因此重试循环会在替身时钟里很快
    走到「窗口已关闭」并结束 —— 测试因此是确定性的、秒级完成的。
    """
    broken = add_plan(web_db, name="超时的", regionid=16, start_min=30, end_min=33)
    healthy = add_plan(web_db, name="正常的", regionid=17, start_min=45, end_min=200)

    calls = {"broken": 0}

    def query_fn(s, regionid, start, end, page_num, page_size):
        if int(regionid) == 16:
            calls["broken"] += 1
            raise httpx.ReadTimeout("模拟读超时")
        return always_one_seat()

    scheduler = make_conc_scheduler(query_fn=query_fn, max_concurrent=2)
    scheduler._tick()

    assert wait_done(web_db, broken, healthy), (
        "抛超时的计划没有结束（重试循环可能没有边界）"
    )
    assert calls["broken"] > 0, "替身查询根本没被调用"

    bad = repo.list_runs(web_db, plan_id=broken)[0]
    assert bad["status"] is not RunStatus.RUNNING
    categories = {
        e["category"] for e in repo.list_run_events(web_db, bad["id"]) if e["category"]
    }
    assert ErrorCategory.NETWORK.value in categories, "网络失败没有被如实归到 NETWORK 分类"

    good = repo.list_runs(web_db, plan_id=healthy)[0]
    assert good["status"] is RunStatus.SUCCEEDED
    assert good["booking_id"] == 101


def test_the_same_plan_is_never_started_twice_by_concurrent_requests(
    web_db, make_conc_scheduler
):
    """并发的「立即执行」请求只有一个能过，绝不产生第二个运行实例。"""
    plan_id = add_plan(web_db, name="并发手动执行", regionid=16, start_min=30, end_min=120)

    gate = Gate()
    scheduler = make_conc_scheduler(
        query_fn=lambda *a, **k: (gate.pass_through(), always_one_seat())[1],
        max_concurrent=8,
    )

    results: list[dict] = []
    results_lock = threading.Lock()
    start_line = threading.Barrier(8)

    def call() -> None:
        start_line.wait()
        outcome = scheduler.request_run(plan_id)
        with results_lock:
            results.append(outcome)

    threads = [threading.Thread(target=call) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    gate.release()

    assert len(results) == 8
    accepted = [r for r in results if r["ok"]]
    assert len(accepted) == 1, f"并发请求接受了 {len(accepted)} 次启动：{results}"

    assert wait_done(web_db, plan_id)
    assert run_count(web_db, plan_id) == 1, "同一计划被启动了两次"
    plan = repo.get_plan(web_db, plan_id)
    only_run = repo.list_runs(web_db, plan_id=plan_id)[0]
    # 运行记录自洽：计划指回那条唯一的运行，运行也指向同一条计划。
    assert plan["last_run_id"] == only_run["id"]
    assert only_run["plan_id"] == plan_id


def test_the_pool_limit_never_spawns_unbounded_threads_or_a_queue(
    web_db, make_conc_scheduler
):
    """并发池满时：不产生无界线程，也不堆积待处理任务，且总量不丢。"""
    plan_ids = [
        add_plan(
            web_db,
            name=f"池测试 {i}",
            regionid=16 + i,
            start_min=30 + i * 10,
            end_min=200 + i * 10,
        )
        for i in range(5)
    ]

    gate = Gate()
    scheduler = make_conc_scheduler(
        query_fn=lambda *a, **k: (gate.pass_through(), always_one_seat())[1],
        max_concurrent=2,
    )

    try:
        scheduler._tick()
        assert wait_for(lambda: gate.peak >= 2)
        # 池已满，再 tick 几次：既不起新线程，也不攒「待处理队列」。
        for _ in range(4):
            scheduler._tick()
            time.sleep(0.05)
        assert len(scheduler.active_plan_ids()) == 2
        assert run_count(web_db) == 2, "并发上限 2，却启动了更多的运行"
        assert gate.peak <= 2, "同时在飞的查询超过了并发上限"
    finally:
        gate.release()

    # 放行后其余计划在后续 tick 里依次执行完 —— 被跳过的计划不丢。
    for _ in range(200):
        scheduler._tick()
        if run_count(web_db) == 5 and not repo.list_running_runs(web_db):
            break
        time.sleep(0.02)

    assert run_count(web_db) == 5
    assert not repo.list_running_runs(web_db)
    assert gate.peak <= 2, "全程同时在飞的查询都没有超过并发上限"
    for pid in plan_ids:
        assert repo.get_plan(web_db, pid)["status"] is PlanStatus.SUCCEEDED


def test_the_soonest_trigger_wins_the_last_concurrency_slot(web_db, make_conc_scheduler):
    """容量只剩一个时，**离触发时刻最近**的计划先拿到它。

    预约窗口以秒计：如果顺序按 id 倒序，一个几小时后才触发的计划会把槽位占掉，
    把马上要触发的计划挤到下一轮，触发就晚了。
    """
    late = add_plan(
        web_db, name="最早触发", regionid=16, start_min=30, end_min=180,
        trigger=(NOW - timedelta(hours=3)).strftime("%H:%M:%S"),
    )
    middle = add_plan(
        web_db, name="居中触发", regionid=17, start_min=60, end_min=200,
        trigger=(NOW - timedelta(hours=2)).strftime("%H:%M:%S"),
    )
    newest = add_plan(
        web_db, name="最后触发", regionid=18, start_min=90, end_min=220,
        trigger=(NOW - timedelta(hours=1)).strftime("%H:%M:%S"),
    )

    gate = Gate()
    scheduler = make_conc_scheduler(
        query_fn=lambda *a, **k: (gate.pass_through(), always_one_seat())[1],
        max_concurrent=1,
    )
    try:
        scheduler._tick()
        assert wait_for(lambda: run_count(web_db) == 1)
        started = [r["plan_id"] for r in repo.list_runs(web_db)]
        assert started == [late], (
            f"拿到唯一槽位的不是最早触发的计划（实际 {started}，"
            f"三条计划 {[late, middle, newest]}）"
        )
    finally:
        gate.release()
    assert wait_done(web_db, late)


def test_a_full_pool_does_not_delay_a_due_trigger(web_db, make_conc_scheduler):
    """池被一个长任务占着时，另一条到点的计划仍在同一轮 tick 内启动。

    需求要求「记录实际延迟并测试」，所以这里真的测量从 tick 到该计划的运行记录
    落库之间的墙上时间，而不是只断言「最终跑过」。
    """
    long_running = add_plan(web_db, name="长任务", regionid=16, start_min=30, end_min=120)
    due = add_plan(web_db, name="到点了", regionid=17, start_min=60, end_min=180)

    gate = Gate()

    def query_fn(s, regionid, start, end, page_num, page_size):
        if int(regionid) == 16:
            gate.pass_through()
        return always_one_seat()

    scheduler = make_conc_scheduler(query_fn=query_fn, max_concurrent=2)
    scheduler._tick()
    assert wait_for(lambda: run_count(web_db, long_running) == 1)

    try:
        started_at = time.monotonic()
        scheduler._tick()
        assert wait_for(lambda: run_count(web_db, due) == 1, timeout=2.0), (
            "到点的计划没有在下一轮 tick 立即启动 —— 被另一个长任务挡住了"
        )
        delay = time.monotonic() - started_at
        assert delay < 0.5, f"触发被其他任务延迟了 {delay:.3f}s（预算 0.5s）"

        run = repo.list_runs(web_db, plan_id=due)[0]
        # 触发时刻已过，因此这里能核对的是「tick 发现它到点后立刻落库」，
        # 而不是「等到某一秒才动手」：运行记录的起始时间就是刚刚。
        assert run["started_at"].startswith(TODAY)
    finally:
        gate.release()

    assert wait_done(web_db, long_running, due)
    assert repo.get_plan(web_db, due)["status"] is PlanStatus.SUCCEEDED


# ================================================================== 2. 保护没有被放松


def test_concurrency_does_not_weaken_unknown_protection(web_db, make_conc_scheduler):
    """结果未知的计划，**即使并发池空着**也不会被自动重放。"""
    healthy = add_plan(web_db, name="正常", regionid=17, start_min=60, end_min=180)
    unknown = add_plan(web_db, name="结果未知", regionid=16, start_min=30, end_min=120)
    seeded = seed_finished_run(
        web_db, unknown, status=RunStatus.UNKNOWN, submit_count=1,
        plan_status=PlanStatus.UNKNOWN,
    )

    scheduler = make_conc_scheduler(max_concurrent=4)
    scheduler._tick()
    assert wait_done(web_db, healthy)

    assert run_count(web_db, unknown) == 1, "结果未知的计划被自动重放了"
    assert repo.get_plan(web_db, unknown)["status"] is PlanStatus.UNKNOWN
    assert repo.get_plan(web_db, unknown)["unknown_ack_at"] is None
    assert repo.get_run(web_db, seeded)["submit_count"] == 1, "既有运行记录被改写"

    # 另一个计划照常执行：保护只作用于它自己那条计划。
    assert repo.get_plan(web_db, healthy)["status"] is PlanStatus.SUCCEEDED


def test_concurrency_does_not_weaken_submitted_today_protection(
    web_db, make_conc_scheduler
):
    """今天已经提交过的计划，即使池是空的也不会再来第二次。

    计划刻意留在「待执行」：这样它**通过了**其他所有闸门（已启用、到点、未过窗口、
    时段时间没到），唯一挡住它的就是「今天已经提交过」。并发容量空着也不会松动。
    """
    plan_id = add_plan(web_db, name="今天提交过", regionid=16, start_min=30, end_min=120)
    other = add_plan(web_db, name="另一条正常计划", regionid=17, start_min=60, end_min=180)
    seeded = seed_finished_run(
        web_db, plan_id, status=RunStatus.FAILED, submit_count=2,
        plan_status=PlanStatus.PENDING,
    )

    scheduler = make_conc_scheduler(max_concurrent=4)
    scheduler._tick()
    # 用另一条计划当「时钟」：它跑完了，说明这一轮 tick 真的发生了，
    # 「今天提交过」那条的静默因此是**被拦下**，而不是「还没来得及跑」。
    assert wait_done(web_db, other)

    assert run_count(web_db, plan_id) == 1, "今天已提交过的计划又启动了新运行"
    assert repo.get_plan(web_db, plan_id)["status"] is PlanStatus.PENDING
    assert repo.get_run(web_db, seeded)["submit_count"] == 2
    assert repo.get_run(web_db, seeded)["status"] is RunStatus.FAILED


def test_restart_recovery_still_holds_while_other_plans_run(web_db, make_conc_scheduler):
    """重启恢复不受并发影响：遗留的「运行中」判为结果未知且绝不自动重放。"""
    interrupted = add_plan(web_db, name="重启前遗留", regionid=16, start_min=30, end_min=120)
    healthy = add_plan(web_db, name="重启后正常", regionid=17, start_min=60, end_min=180)
    leftover = repo.start_run(web_db, interrupted, trigger="scheduler")
    with web_db.tx() as conn:
        conn.execute("UPDATE run SET submit_count = 1 WHERE id = ?", (leftover,))

    scheduler = make_conc_scheduler(max_concurrent=4)
    recovered = scheduler.recover_after_restart()
    assert [r["run_id"] for r in recovered] == [leftover]

    run = repo.get_run(web_db, leftover)
    assert run["status"] is RunStatus.INTERRUPTED
    assert run["category"] == ErrorCategory.SUBMIT_UNKNOWN.value
    assert run["status"] is not RunStatus.FAILED, "已经提交过的运行绝不能记成失败"

    scheduler._tick()
    assert wait_done(web_db, healthy)
    assert run_count(web_db, interrupted) == 1, "结果未知的遗留运行被自动重放了"
    assert repo.get_plan(web_db, healthy)["status"] is PlanStatus.SUCCEEDED


# --------------------------------------------------------- 2b. 锁自身的并发正确性
#
# 下面两条钉住并发改造中实际暴露出来的两个锁缺陷。它们都不是「测试才有的问题」：
# 手动执行接口（Web 请求线程）与调度器线程会同时走到这些代码路径上。


def test_slot_lock_probe_is_read_only_and_does_not_disturb_a_holder(
    web_settings, tmp_path
):
    """探测别的时段/自己时段的锁，**不得创建或删除**那把共享锁文件。

    早期实现用「创建后立即删除」来探测，于是同一轮 tick 里先探测的计划会把锁文件
    创建又删掉，后探测的计划于是看到「空闲」，两个同时段的计划双双启动或双双放弃；
    Windows 上并发创建/删除还会让 ``O_CREAT|O_EXCL`` 返回 EACCES。
    """
    from haut_seat.core.lock import FileLock
    from haut_seat.services import slot_lock

    settings = web_settings.model_copy(update={"book_lock_file": str(tmp_path / "book.lock")})
    start, end = "2026-10-18 10:30:00", "2026-10-18 13:00:00"
    path = slot_lock.slot_lock_path(settings, regionid=16, start=start, end=end)

    # 空闲：探测说空闲，且**没有**留下任何文件。
    assert slot_lock.slot_lock_is_free(settings, regionid=16, start=start, end=end) is True
    assert not path.exists(), "探测动作在空闲时留下了锁文件"

    # 别人持有：探测说占用，且不得动那把锁（持有者仍在正常持锁）。
    holder = FileLock(path)
    assert holder.acquire() is True
    try:
        assert slot_lock.slot_lock_is_free(settings, regionid=16, start=start, end=end) is False
        assert path.exists(), "探测动作把别人的锁删掉了"
        # 持有者仍然持锁：别人拿不到。
        assert FileLock(path).acquire() is False
        # 换一个时段仍然空闲，互不影响。
        assert slot_lock.slot_lock_is_free(
            settings, regionid=17, start="2026-10-18 14:00:00", end="2026-10-18 16:00:00"
        ) is True
    finally:
        holder.release()
    assert not path.exists()


def test_file_lock_treats_windows_access_denied_as_busy(tmp_path, monkeypatch):
    """Windows 上并发创建锁文件会返回 EACCES，必须当成「已被占用」，而不是抛异常。

    如果让它抛出去：手动执行接口会 500，运行线程会整轮异常退出 —— 都是把一个
    「有人在动这个文件」误报成真故障。文件不存在却报权限错误才是真问题，必须抛。
    """
    from haut_seat.core import lock as lock_mod

    path = tmp_path / "book.lock"
    path.write_text("", encoding="utf-8")

    class _DeniedOpen:
        """只让 ``os.open`` 失败，其余原样转发。

        不直接 patch 全局 ``os.open``：那会波及 pytest 自己的文件操作。
        """

        def __getattr__(self, name):
            return getattr(os, name)

        def open(self, *args, **kwargs):
            raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(lock_mod, "os", _DeniedOpen())
    assert lock_mod.FileLock(path, stale_after_s=3600.0).acquire() is False

    # 文件不存在时的权限错误必须原样抛出：那是环境问题，不能悄悄咽掉。
    missing = lock_mod.FileLock(tmp_path / "nope.lock", stale_after_s=3600.0)
    with pytest.raises(PermissionError):
        missing.acquire()


# ================================================================== 3. 连接与凭据隔离


class OfflineHttp:
    """把真实构造出来的 ``httpx.Client`` 的传输层换成 MockTransport 并记录请求。

    客户端本身（含它持有的 Cookie / Authorization）是**真货**，只有传输被拦下，
    因此断言的是「真实客户端真正会发出去的头」，而不是测试自己拼出来的假对象。
    """

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return httpx.Response(200, json={"code": 200, "msg": "成功", "seatList": []})


@pytest.fixture
def offline_http(monkeypatch):
    http = OfflineHttp()
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(http._handle)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", fake_client)
    return http


def _creds(tag: str, *, source: str = "env") -> Credentials:
    return Credentials(
        cookie=f"Admin-Token={tag}", authorization=f"Bearer {tag}", source=source
    )


def _http_settings(make_settings):
    """出站节流间隔必须为正（配置校验如此），取一个极小值即可，不真的拖慢测试。"""
    return make_settings(
        base_url="https://seat.example.invalid",
        min_interval_s=0.01,
        max_retry=0,
    )


def test_same_credentials_share_a_client_but_different_credentials_never_do(
    make_settings, offline_http
):
    """同一凭据复用同一个客户端（省掉重复的 DNS/TCP/TLS），换凭据则**必然**换客户端。"""
    settings = _http_settings(make_settings)

    with io.http_session(settings, credentials=_creds("alpha")) as first:
        with io.http_session(settings, credentials=_creds("alpha")) as again:
            assert first is again, "同一凭据没有复用客户端（连接复用失效）"
        with io.http_session(settings, credentials=_creds("beta")) as other:
            assert other is not first, "不同凭据共用了同一个客户端 —— 会串 Cookie"

    # 真实发出去的请求头：两条凭据各发各的，绝不互相污染。
    first.get("/stage-api/seat/list")
    other.get("/stage-api/seat/list")
    assert len(offline_http.seen) == 2
    assert offline_http.seen[0].headers["cookie"] == "Admin-Token=alpha"
    assert offline_http.seen[1].headers["cookie"] == "Admin-Token=beta"
    assert offline_http.seen[0].headers["authorization"] == "Bearer alpha"
    assert offline_http.seen[1].headers["authorization"] == "Bearer beta"


def test_concurrent_tasks_never_share_cookie_or_authorization(make_settings, offline_http):
    """并发跑多个任务时，每个任务的请求头只带自己的凭据。"""
    settings = _http_settings(make_settings)
    tags = [f"task{i}" for i in range(6)]
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker(tag: str) -> None:
        try:
            for _ in range(3):
                with io.http_session(settings, credentials=_creds(tag)) as client:
                    client.get(f"/stage-api/seat/list?who={tag}")
        except Exception as exc:  # pragma: no cover - 只在真的出错时上报
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in tags]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert not errors, f"并发借用客户端时出错：{errors}"
    assert len(offline_http.seen) == len(tags) * 3
    for request in offline_http.seen:
        who = request.url.params["who"]
        assert request.headers["cookie"] == f"Admin-Token={who}", (
            "请求带上了别的任务的 Cookie"
        )
        assert request.headers["authorization"] == f"Bearer {who}"


# ================================================================== 4. 用户端 × 并发 × 管理端


@pytest.fixture
def booking_web(make_client, web_settings, web_db):
    """用户端客户端 + 与 ``/admin`` 同一套离线替身的数据库。

    用户端现在要求登录，且计划执行时用的是**该用户自己绑定的**学校凭据 ——
    因此这里先建一个用户端账号、走真实登录表单登录，并按生产同一条路径
    存一份**假的**凭据（不经过学校登录接口：这里不发任何真实请求）。
    """
    from tests.user_end import bind_credentials, create_user, login_user

    _app, client = make_client(settings=web_settings, db=web_db)
    user_id = create_user(web_db)
    login_user(client, web_db)
    bind_credentials(web_db, web_settings, user_id)
    return client, web_db, web_settings


@pytest.fixture
def frozen_web_clock(monkeypatch):
    """把 Web 层的「现在」冻结在 ``NOW``。

    这样「今天 10:30 开始、13:00 结束」这种时段在任何真实时刻都是合法的**未来**
    时段（真实 21:00 之后当天已经没有合法时段可用了），用例结果与运行时刻无关。
    ``routes._now`` 本来就是为这个用途单独留出来的函数。
    """
    from haut_seat.web import routes

    monkeypatch.setattr(routes, "_now", lambda: NOW)
    return NOW


def _csrf(client) -> str:
    client.get("/booking")
    token = client.cookies.get(COOKIE)
    assert token, "用户端页面没有种下 CSRF Cookie"
    return token


def _bpost(client, path, data=None, *, token=None):
    body = dict(data or {})
    if token is not None:
        body["_csrf"] = token
    return client.post(path, data=body, headers={"Origin": ORIGIN})


def _user_form(**overrides) -> dict:
    """用户端表单：日期/时刻都相对 ``NOW``，因此执行日期就是今天、窗口还没开始。"""
    data = {
        "name": "并发用户端预约",
        "mode": "once",
        "run_date": TODAY,
        "trigger_at": (NOW - timedelta(hours=1)).strftime("%H:%M"),
        "use_start": (NOW + timedelta(minutes=30)).strftime("%H:%M"),
        "use_end": (NOW + timedelta(hours=3)).strftime("%H:%M"),
        "regionid": "16",
        "preferred_seats": "A桌 1座",
        "preferred_prefixes": "",
        "confirmed_bookable_states": CONFIRMED,
        "max_attempts_per_seat": "1",
        "note": "",
    }
    data.update(overrides)
    return data


def test_concurrent_user_requests_do_not_corrupt_run_records(
    booking_web, frozen_web_clock
):
    """并发的用户端写请求不会破坏同一计划的运行记录，也不会留下半截状态。"""
    client, db, _settings = booking_web
    token = _csrf(client)
    resp = _bpost(client, "/booking/new", _user_form(name="并发写"), token=token)
    assert resp.status_code == 303, resp.text
    plan_id = int(re.search(r"/booking/(\d+)", resp.headers["location"]).group(1))

    token = _csrf(client)
    assert _bpost(client, f"/booking/{plan_id}/enable", token=token).status_code == 303

    # 预置一条今天的运行记录（提交次数为 0 ⇒ 允许删除；运行记录本身不得被改写），
    # 计划留在「待执行」，这样 pause / delete 两条动作都有明确含义。
    seeded = seed_finished_run(
        db, plan_id, status=RunStatus.FAILED, submit_count=0,
        plan_status=PlanStatus.PENDING,
    )

    token = _csrf(client)
    responses: list[tuple[str, int]] = []
    lock = threading.Lock()

    def call(action: str) -> None:
        status = _bpost(client, f"/booking/{plan_id}/{action}", token=token).status_code
        with lock:
            responses.append((action, status))

    threads = [threading.Thread(target=call, args=(a,)) for a in ("pause", "delete")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert len(responses) == 2
    for action, status in responses:
        assert status in (303, 404), f"{action} 返回了意外状态 {status}"
    assert all(status != 500 for _a, status in responses)

    plan = repo.get_plan(db, plan_id)
    if plan is None:
        # 被删掉了：删除是最后生效的动作，运行记录要么一并消失、要么原样保留，
        # 无论哪种都不得被改写。
        run = repo.get_run(db, seeded)
        if run is not None:
            assert run["submit_count"] == 0
            assert run["status"] is RunStatus.FAILED
    else:
        assert plan["status"] in (PlanStatus.PAUSED, PlanStatus.PENDING)
        assert plan["last_run_id"] == seeded
        assert repo.get_run(db, seeded)["status"] is RunStatus.FAILED


def test_user_created_plan_runs_through_the_shared_scheduler(
    make_client, web_settings, web_db, make_conc_scheduler, frozen_web_clock
):
    """用户端建的计划进入**同一套**数据流：共享调度器执行，管理端看得到结果。"""
    from tests.user_end import bind_credentials, create_user, login_user

    scheduler = make_conc_scheduler(max_concurrent=2)
    _app, client = make_client(settings=web_settings, db=web_db, scheduler=scheduler)

    # 登录 + 绑定：计划执行时用的必须是**这个用户自己**的凭据，
    # 因此这里先按生产路径存一份（假值、真加密、不发任何真实请求）。
    user_id = create_user(web_db)
    login_user(client, web_db)
    bind_credentials(web_db, web_settings, user_id)

    token = _csrf(client)
    resp = _bpost(client, "/booking/new", _user_form(name="用户端到管理端"), token=token)
    assert resp.status_code == 303, resp.text
    plan_id = int(re.search(r"/booking/(\d+)", resp.headers["location"]).group(1))

    # 用户端建的计划默认「已暂停」，共享调度器不得动它。
    scheduler._tick()
    time.sleep(0.05)
    assert run_count(web_db, plan_id) == 0, "「已暂停」的用户端计划被调度器启动了"

    token = _csrf(client)
    assert _bpost(client, f"/booking/{plan_id}/enable", token=token).status_code == 303

    # 新库的 ``global_pause`` 默认是**开**的（安全默认：不配置就不自动提交），
    # 管理端要在「系统状态」页显式关掉。这里照做，否则任何计划都不会被启动。
    repo.set_app_setting(web_db, global_pause=False, scheduler_enabled=True)

    scheduler._tick()
    assert wait_for(lambda: run_count(web_db, plan_id) == 1), (
        "用户端启用后的计划没有被共享调度器拾取"
    )
    assert wait_done(web_db, plan_id)

    run = repo.list_runs(web_db, plan_id=plan_id)[0]
    assert run["status"] == RunStatus.SUCCEEDED.value
    assert run["booking_id"] == 101
    assert run["trigger"] == "scheduler"
    assert repo.get_plan_origin(web_db, plan_id) == repo.PLAN_ORIGIN_USER

    # 管理端看得到这条计划、这次运行和它的结果（同一个客户端、同一个库）。
    from haut_seat.web import security

    security.create_admin(web_db, "tester", "Correct-Horse-9")
    assert client.post(
        "/admin/login", data={"username": "tester", "password": "Correct-Horse-9"}
    ).status_code == 303

    plans_page = client.get("/admin/plans")
    assert "用户端到管理端" in plans_page.text
    assert "用户端" in plans_page.text

    detail = client.get(f"/admin/plans/{plan_id}")
    assert "用户端到管理端" in detail.text

    runs_page = client.get("/admin/runs")
    assert "用户端到管理端" in runs_page.text
    assert "101" in runs_page.text