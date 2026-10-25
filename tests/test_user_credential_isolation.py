"""多账号凭证隔离：调度器为**每个计划**取用**它属主自己**的那一份凭据。

这是整个多用户改造里最要紧的一组断言 —— 前面几个模块证明"凭据按用户分开存、
分开加密"，这里证明"跑起来的时候真的各用各的"。

**全部离线**：查询/提交/预检都是替身，判据是"替身看到的 settings 里装的是谁的
Cookie 与 Authorization"，不发起任何真实请求、不做任何真实预约。

覆盖需求点名的几条：
- 用户 A 的运行用 A 的 Cookie，用户 B 的用 B 的（同一次 tick 里并发也一样）；
- 某个用户的凭据失效 ⇒ **只**停他自己的计划，别人的照跑；
- 取不到 / 解不开 ⇒ 不开这次运行，**绝不回退**到全局凭据或别人的凭据；
- 无主（历史 / 管理端）计划仍按从前的全局判定；
- 运行中判定的失效只写到**属主**名下，不污染全局 auth_state。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from haut_seat.core.auth import AuthCheck, AuthStatus
from haut_seat.models.booking import BookingOutcome, BookingResult
from haut_seat.models.seat import LayoutQueryResult
from haut_seat.services import user_credentials as uc
from haut_seat.services.io import secret_str
from haut_seat.services.scheduler import Scheduler
from haut_seat.services.state import PlanStatus, RunStatus
from haut_seat.web import repository as repo
from haut_seat.web import user_auth
from haut_seat.web.validation import parse_plan
from tests.user_end import bind_credentials, create_user, find_user_id

NOW = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
TODAY = NOW.strftime("%Y-%m-%d")

#: 全局（``.env`` / 快照）那份凭据。它只该被**无主**计划用到；
#: 任何一个有主计划的请求里出现它，都说明发生了"拿别人的账号提交"。
GLOBAL_COOKIE = "Admin-Token=GLOBAL-SENTINEL-DO-NOT-USE"
GLOBAL_AUTHORIZATION = "Bearer GLOBAL-SENTINEL-DO-NOT-USE"


def creds_in(settings) -> tuple[str, str]:
    """这次调用拿到的是哪一份凭据（页面与日志从不需要它，测试才需要）。"""
    return (secret_str(settings.cookie) or "", secret_str(settings.authorization) or "")


def user_cookie(user_id: int) -> str:
    return f"Admin-Token=USER-{user_id}-COOKIE"


def user_authz(user_id: int) -> str:
    return f"Bearer USER-{user_id}-TOKEN"


class FakeSchool:
    """替身学校：记录**每次**查询/提交/预检时看到的凭据与时段。"""

    def __init__(self, *, seat_available: bool = True, auth: AuthStatus = AuthStatus.VALID):
        self.seat_available = seat_available
        self.auth = auth
        #: ``(window_start, cookie, authorization)``
        self.queries: list[tuple[str, str, str]] = []
        self.submits: list[tuple[str, str, str]] = []
        self.checks: list[tuple[str, str]] = []

    def query(self, settings, regionid, start, end, page_num, page_size):
        self.queries.append((str(start), *creds_in(settings)))
        rows = [{"id": 1, "seatName": "A桌 1座", "seatStatus": "2", "isCan": 1, "open": False}]
        return LayoutQueryResult.from_response(
            {"layout": None, "seatList": rows if self.seat_available else []}
        )

    def submit(self, settings, target_seat, submit_start, end):
        self.submits.append((str(submit_start), *creds_in(settings)))
        return BookingResult(BookingOutcome.SUCCESS, "预约成功", booking_id=1)

    def auth_check(self, settings, regionid, start=None, end=None):
        self.checks.append(creds_in(settings))
        return AuthCheck(self.auth, f"替身：{self.auth.value}")

    def cookies_used(self) -> set[str]:
        return {c for _start, c, _authz in self.queries + self.submits}


class StubAuthService:
    """只回答"全局那份凭据是否已被判定失效"。"""

    def __init__(self, blocked: bool = False):
        self.blocked = blocked

    def blocks_submissions(self) -> bool:
        return self.blocked

    def current(self):
        return None


class RecordingNotifier:
    """记录通知调用：调度器"如实告诉操作者"这条路径也要能被断言到。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, name):
        if name.startswith("notify_"):
            def _call(**kwargs):
                self.calls.append((name, kwargs))
                return {"status": "skipped"}

            return _call
        raise AttributeError(name)

    def details(self, name: str) -> list[str]:
        return [c[1].get("detail", "") for c in self.calls if c[0] == name]


class FakeTime:
    """把时钟钉在 ``NOW``。

    必须同时给调度器 **和** 运行器（``time_mod``）：运行器判断"预约开始时间是否
    已过"用的是 ``time_mod.time()``。只钉住 ``clock`` 的话，它会拿真实的墙上时间
    （比如晚上 23 点）去比上午 10 点的窗口，于是每次运行都以 CONFIG_ERROR 收场 ——
    用例看到的将是"一个请求都没发"，却以为是隔离出了问题。
    """

    def __init__(self, now: float):
        self.now = now

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:  # 不真的等待
        self.now += seconds


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def settle(db, scheduler) -> None:
    """等这次 tick 启动的运行真的结束。

    **先等线程**：``_active`` 在 ``thread.start()`` **之前**就登记了，因此它不可能
    出现在"已启动但还没落库"的窗口里。只等数据库里的运行行会与线程抢跑 ——
    ``_tick`` 返回时线程可能还没开始写 ``run`` 行，"没有正在运行的行"立刻为真，
    用例便带着空结果往下走了。这种假绿最难查，所以两道都等。
    """
    assert wait_for(lambda: scheduler.active_plan_ids() == []), "运行没有结束"
    assert wait_for(lambda: not repo.list_running_runs(db)), "运行记录没有收尾"


# ------------------------------------------------------------------ 夹具


@pytest.fixture
def school():
    return FakeSchool()


@pytest.fixture
def settings_with_global_credentials(web_settings):
    """**同时**把全局那份凭据装上：这样一来"用了别人的"就一定会被断言抓到。

    ``model_copy`` 不做校验，因此这里必须给 ``SecretStr`` 而不是裸 ``str``
    （``Settings.cookie`` 的类型就是 ``SecretStr``，传字符串会在取用时炸掉）。
    """
    from pydantic import SecretStr

    return web_settings.model_copy(
        update={
            "enable_real_booking": True,
            "cookie": SecretStr(GLOBAL_COOKIE),
            "authorization": SecretStr(GLOBAL_AUTHORIZATION),
        }
    )


@pytest.fixture
def bind(web_db, settings_with_global_credentials):
    """把一份按用户区分的**假**凭据存好（走生产同一条加密保存路径）。"""

    def _bind(user_id, *, cookie=None, authorization=None,
              status: str = uc.CRED_STATUS_VALID):
        bind_credentials(
            web_db, settings_with_global_credentials, user_id,
            cookie=cookie if cookie is not None else user_cookie(user_id),
            authorization=(
                authorization if authorization is not None else user_authz(user_id)
            ),
            status=status,
        )

    return _bind


@pytest.fixture
def bind_both(bind):
    def _bind_both(*user_ids, status: str = uc.CRED_STATUS_VALID):
        for user_id in user_ids:
            bind(user_id, status=status)

    return _bind_both


@pytest.fixture
def make_plan(web_db):
    """按给定时段建一条**启用 + 待执行**的计划；``owner`` 非空时挂上归属。

    每条计划的预约开始时间都不同 ⇒ 时段锁名不同 ⇒ 同一次 tick 里可以真的并发跑，
    不必为了避开锁竞争而串行化 —— 并发下的隔离正是要验的东西。
    """

    def _make(*, name: str, start_offset_minutes: int, owner: int | None = None) -> int:
        form = {
            "name": name,
            "run_date": TODAY,
            "repeat_rule": "once",
            "trigger_at": (NOW - timedelta(hours=1)).strftime("%H:%M:%S"),
            "book_start": (NOW + timedelta(minutes=start_offset_minutes)).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
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
        repo.set_plan_status(web_db, plan_id, PlanStatus.PENDING, now=NOW)
        if owner is not None:
            repo.set_plan_owner(web_db, plan_id, owner)
        return plan_id

    return _make


@pytest.fixture
def make_scheduler(web_db, settings_with_global_credentials, school):
    made = []

    def _make(*, auth_blocked: bool = False, max_concurrent: int = 4, notifier=None):
        scheduler = Scheduler(
            web_db,
            settings_with_global_credentials,
            tick_s=0.05,
            max_concurrent=max_concurrent,
            query_fn=school.query,
            submit_fn=school.submit,
            auth_check_fn=school.auth_check,
            auth_service=StubAuthService(blocked=auth_blocked),
            notification=notifier,
            clock=lambda: NOW,
            time_mod=FakeTime(NOW.timestamp()),
            wait_fn=lambda target_ts: None,
        )
        made.append(scheduler)
        return scheduler

    repo.set_app_setting(web_db, global_pause=False)
    yield _make
    for scheduler in made:
        scheduler.stop(timeout=1.0)


def window_of(db, plan_id: int) -> str:
    """计划今天真正会被提交的那个时段（前 16 位足以区分，秒位可能被归一化）。"""
    return str(repo.get_plan(db, plan_id)["book_start"])[:16]


def _capture_reason(db, sink: list[str]):
    """包一层 ``_mark_awaiting_auth``：照常落库，另外把原因记下来供断言。"""

    def _wrapper(plan_id: int, reason: str) -> None:
        sink.append(reason)
        repo.set_plan_status(db, plan_id, PlanStatus.AWAITING_AUTH)

    return _wrapper


# ================================================================== 1. 各用各的


def test_each_run_uses_its_own_owners_credentials(
    web_db, school, make_plan, make_scheduler, bind_both
):
    """同一次 tick 里两条计划并发跑：各自的查询与提交都用**自己属主**的凭据。

    这是"多账号"的**定义级**断言：只要有一个请求带着别人的 Cookie 出去，它就失败。
    """
    alice = create_user(web_db, "alice")
    bob = create_user(web_db, "bob")
    bind_both(alice, bob)

    a_plan = make_plan(name="alice 的", start_offset_minutes=10, owner=alice)
    b_plan = make_plan(name="bob 的", start_offset_minutes=20, owner=bob)
    owner_by_window = {
        window_of(web_db, a_plan): alice,
        window_of(web_db, b_plan): bob,
    }
    assert len(owner_by_window) == 2, "两条计划的时段必须不同，否则时段锁会互相挡"

    scheduler = make_scheduler()
    scheduler._tick()
    settle(web_db, scheduler)

    assert len(school.submits) == 2, school.submits
    for start, cookie, authz in school.queries + school.submits:
        owner = owner_by_window[start[:16]]
        assert cookie == user_cookie(owner), (start, cookie)
        assert authz == user_authz(owner), (start, authz)

    assert GLOBAL_COOKIE not in school.cookies_used(), "有主计划绝不能用全局那份凭据"
    assert repo.get_plan(web_db, a_plan)["status"] is PlanStatus.SUCCEEDED
    assert repo.get_plan(web_db, b_plan)["status"] is PlanStatus.SUCCEEDED


def test_ownerless_plan_uses_the_global_credentials(
    web_db, school, make_plan, make_scheduler
):
    """无主计划（历史 / 管理端建的）仍用全局那份 —— 与从前逐字节一致。"""
    plan_id = make_plan(name="管理端建的", start_offset_minutes=10, owner=None)
    scheduler = make_scheduler()
    scheduler._tick()
    settle(web_db, scheduler)

    assert [c for _s, c, _a in school.submits] == [GLOBAL_COOKIE]
    assert repo.get_plan(web_db, plan_id)["status"] is PlanStatus.SUCCEEDED


def test_credentials_are_re_read_for_every_run(
    web_db, school, make_plan, make_scheduler, bind
):
    """每次运行都重新解密取用：用户重新绑定后的新凭据立刻生效，不沿用旧的。"""
    alice = create_user(web_db, "alice")
    bind(alice)
    first_plan = make_plan(name="alice 第一次", start_offset_minutes=10, owner=alice)

    scheduler = make_scheduler()
    scheduler._tick()
    settle(web_db, scheduler)
    assert school.submits[0][1] == user_cookie(alice)

    # 重新绑定成另一份凭据，再建一条新计划（第一条今天已经跑过，不会重跑）。
    rebound = "Admin-Token=USER-ALICE-REBOUND"
    bind(alice, cookie=rebound, authorization="Bearer USER-ALICE-REBOUND")
    second_plan = make_plan(name="alice 重绑后", start_offset_minutes=20, owner=alice)

    scheduler._tick()
    settle(web_db, scheduler)

    assert window_of(web_db, first_plan) != window_of(web_db, second_plan)
    assert school.submits[-1][1] == rebound, "重绑之后必须用新凭据，不能沿用旧的"


# ================================================================== 2. 失效只停自己


def test_one_users_expiry_pauses_only_their_plans(
    web_db, school, make_plan, make_scheduler, bind_both
):
    """alice 的凭据失效 ⇒ 只停 alice 的计划；bob 的照跑，无主计划照跑。"""
    alice = create_user(web_db, "alice")
    bob = create_user(web_db, "bob")
    bind_both(alice, bob)
    uc.mark_status(web_db, alice, uc.CRED_STATUS_INVALID, "凭据过期", source="test")

    a_plan = make_plan(name="alice 的", start_offset_minutes=10, owner=alice)
    b_plan = make_plan(name="bob 的", start_offset_minutes=20, owner=bob)
    legacy = make_plan(name="历史计划", start_offset_minutes=30, owner=None)

    scheduler = make_scheduler()
    scheduler._tick()
    settle(web_db, scheduler)

    assert repo.get_plan(web_db, a_plan)["status"] is PlanStatus.AWAITING_AUTH
    assert repo.get_plan(web_db, b_plan)["status"] is PlanStatus.SUCCEEDED
    assert repo.get_plan(web_db, legacy)["status"] is PlanStatus.SUCCEEDED
    assert user_cookie(alice) not in school.cookies_used(), (
        "凭据已判失效，不该再用它去查任何东西"
    )
    # 只开了两条运行（bob 的 + 无主的），alice 那条根本没开。
    assert len(repo.list_runs(web_db)) == 2
    assert {r["plan_id"] for r in repo.list_runs(web_db)} == {b_plan, legacy}


def test_unbound_user_gets_a_clear_reason(web_db, school, make_plan, make_scheduler):
    """还没绑定的用户 ⇒ 计划转入等待认证，而且**连这次运行都不开**。"""
    carol = create_user(web_db, "carol")
    plan_id = make_plan(name="carol 的", start_offset_minutes=10, owner=carol)

    scheduler = make_scheduler()
    scheduler._tick()
    settle(web_db, scheduler)

    assert repo.get_plan(web_db, plan_id)["status"] is PlanStatus.AWAITING_AUTH
    assert repo.list_runs(web_db) == [], "连凭据都没有时不该开这次运行"
    assert school.queries == [] and school.submits == []


def test_indeterminate_credential_status_does_not_block(
    web_db, school, make_plan, make_scheduler, bind
):
    """``indeterminate``（探测不出结论）**不拦** —— 一次网络抖动不该停掉计划。"""
    dave = create_user(web_db, "dave")
    bind(dave, status=uc.CRED_STATUS_INDETERMINATE)
    plan_id = make_plan(name="dave 的", start_offset_minutes=10, owner=dave)

    scheduler = make_scheduler()
    scheduler._tick()
    settle(web_db, scheduler)

    assert repo.get_plan(web_db, plan_id)["status"] is PlanStatus.SUCCEEDED
    assert [c for _s, c, _a in school.submits] == [user_cookie(dave)]


# ================================================================== 3. 绝不回退


def test_credentials_that_cannot_be_decrypted_park_the_plan(
    web_db, school, make_plan, make_scheduler, bind
):
    """密文解不开（主密钥换了 / 库被改过）⇒ 不开运行、转等待认证，**不拿别人的顶上**。"""
    erin = create_user(web_db, "erin")
    bind(erin)
    with web_db.tx() as conn:
        conn.execute(
            "UPDATE user_credential SET cookie_enc = ? WHERE user_id = ?",
            ("hsc1$AAAA$BBBB$CCCC$DDDD", erin),
        )
    plan_id = make_plan(name="erin 的", start_offset_minutes=10, owner=erin)
    reasons: list[str] = []

    scheduler = make_scheduler()
    scheduler._mark_awaiting_auth = _capture_reason(web_db, reasons)
    scheduler._tick()
    settle(web_db, scheduler)

    assert repo.get_plan(web_db, plan_id)["status"] is PlanStatus.AWAITING_AUTH
    assert repo.list_runs(web_db) == []
    assert reasons and "无法解密" in reasons[0]
    assert school.cookies_used() == set(), "解不开的凭据不该被送出去，也不该换成别人的"


def test_no_credentials_means_no_run_and_no_other_account_used(
    web_db, school, make_plan, make_scheduler
):
    """取不到凭据就直接不跑：连这次运行都不开，也**绝不**改用全局那份。

    未绑定的用户是在**调度那一刻**就被拦下的（``_plan_auth_block``），因此断言的
    是"计划转入等待认证 + 操作者收到如实的原因"，而不是某条 run 记录 —— 这种情形
    本就不该留下一条注定跑不成的 run 行。
    """
    frank = create_user(web_db, "frank")
    plan_id = make_plan(name="frank 的", start_offset_minutes=10, owner=frank)
    notifier = RecordingNotifier()

    scheduler = make_scheduler(notifier=notifier)
    scheduler._tick()
    settle(web_db, scheduler)

    details = notifier.details("notify_auth_failure")
    assert details, "认证不可用时必须如实通知，而不是悄悄跳过"
    assert f"#{frank}" in details[0] and "尚未绑定" in details[0]
    assert repo.get_plan(web_db, plan_id)["status"] is PlanStatus.AWAITING_AUTH
    assert repo.list_runs(web_db) == [], "连凭据都没有时不该开这次运行"
    assert GLOBAL_COOKIE not in school.cookies_used()
    assert school.queries == [] and school.submits == []


def test_ownerless_plan_is_blocked_by_the_global_gate(
    web_db, school, make_plan, make_scheduler
):
    """全局认证失效时，**无主**计划仍按老规矩被拦（这条语义没被改动）。"""
    plan_id = make_plan(name="历史计划", start_offset_minutes=10, owner=None)
    notifier = RecordingNotifier()

    scheduler = make_scheduler(auth_blocked=True, notifier=notifier)
    scheduler._tick()
    settle(web_db, scheduler)

    assert repo.get_plan(web_db, plan_id)["status"] is PlanStatus.AWAITING_AUTH
    assert repo.list_runs(web_db) == []
    assert notifier.details("notify_auth_failure"), "无主计划被全局闸门拦下时也要通知"


def test_global_gate_does_not_block_owned_plans(
    web_db, school, make_plan, make_scheduler, bind
):
    """反过来同样成立：全局那份失效**不该**波及用户自己的计划。

    这正是"一个人账号过期让所有人停工"要防的那种错。
    """
    gina = create_user(web_db, "gina")
    bind(gina)
    plan_id = make_plan(name="gina 的", start_offset_minutes=10, owner=gina)

    scheduler = make_scheduler(auth_blocked=True)
    scheduler._tick()
    settle(web_db, scheduler)

    assert repo.get_plan(web_db, plan_id)["status"] is PlanStatus.SUCCEEDED
    assert [c for _s, c, _a in school.submits] == [user_cookie(gina)]


# ================================================================== 4. 运行中的失效归谁


def test_auth_invalid_during_a_run_is_recorded_against_the_owner_only(
    web_db, school, make_plan, make_scheduler, bind
):
    """运行中检测到登录失效：写进**属主**名下，全局 auth_state 一个字节都不动。"""
    holly = create_user(web_db, "holly")
    bind(holly)
    school.auth = AuthStatus.INVALID
    plan_id = make_plan(name="holly 的", start_offset_minutes=10, owner=holly)

    before = repo.get_auth_state(web_db)
    scheduler = make_scheduler()
    scheduler._tick()
    settle(web_db, scheduler)

    assert repo.get_plan(web_db, plan_id)["status"] is PlanStatus.AWAITING_AUTH
    runs = repo.list_runs(web_db)
    assert len(runs) == 1 and runs[0]["status"] is RunStatus.AWAITING_AUTH
    assert uc.credentials_status(web_db, holly) == uc.CRED_STATUS_INVALID
    events = repo.list_user_auth_events(web_db, holly)
    assert events and events[0]["source"] == "scheduler"
    assert "登录失效" in events[0]["message"]
    assert repo.get_auth_state(web_db) == before, "全局认证状态不该被某个用户带崩"
    assert school.submits == [], "预检判失效后一次提交都不该发"


def test_auth_invalid_does_not_touch_other_users_status(
    web_db, school, make_plan, make_scheduler, bind_both
):
    """只有失效那个用户的计划被停，别人的计划继续跑完并以**自己的**凭据提交。"""
    holly = create_user(web_db, "holly")
    ivan = create_user(web_db, "ivan")
    bind_both(holly, ivan)

    def selective(settings, regionid, start=None, end=None):
        # 只让 holly 的那份预检判失效：按凭据区分，证明调度器真的分开取了。
        if creds_in(settings)[0] == user_cookie(holly):
            return AuthCheck(AuthStatus.INVALID, "holly 的凭据过期")
        return AuthCheck(AuthStatus.VALID, "ok")

    scheduler = make_scheduler()
    scheduler._auth_check_fn = selective

    holly_plan = make_plan(name="holly 的", start_offset_minutes=10, owner=holly)
    ivan_plan = make_plan(name="ivan 的", start_offset_minutes=20, owner=ivan)
    scheduler._tick()
    settle(web_db, scheduler)

    assert uc.credentials_status(web_db, holly) == uc.CRED_STATUS_INVALID
    assert uc.credentials_status(web_db, ivan) == uc.CRED_STATUS_VALID
    assert repo.get_plan(web_db, holly_plan)["status"] is PlanStatus.AWAITING_AUTH
    assert repo.get_plan(web_db, ivan_plan)["status"] is PlanStatus.SUCCEEDED
    assert [c for _s, c, _a in school.submits] == [user_cookie(ivan)]


# ================================================================== 5. 运行期复查与善后


def test_should_continue_rechecks_the_owners_credentials_live(
    web_db, make_plan, make_scheduler, bind, settings_with_global_credentials
):
    """运行中途属主被标失效 / 解绑 ⇒ ``_should_continue`` 立刻叫停新的提交。

    读的是**数据库当下的值**，不是 tick 开始时的快照 —— 运行可能跨越几分钟，
    用户完全可能在这期间被判定失效或自己解绑。
    """
    alice = create_user(web_db, "alice")
    bind(alice)
    plan_id = make_plan(name="alice 的", start_offset_minutes=10, owner=alice)
    scheduler = make_scheduler()

    assert scheduler._should_continue(plan_id) == (True, "")

    uc.mark_status(web_db, alice, uc.CRED_STATUS_INVALID, "过期", source="test")
    ok, reason = scheduler._should_continue(plan_id)
    assert ok is False and "认证已失效" in reason

    uc.clear_credentials(web_db, settings_with_global_credentials, alice)
    ok, reason = scheduler._should_continue(plan_id)
    assert ok is False and "认证已失效" in reason


def test_deleting_an_account_keeps_plans_and_runs(
    web_db, school, make_plan, make_scheduler, bind
):
    """删账号**不删**计划与运行记录：归属行消失后计划按无主解释。

    这是有意的（见 ``user_auth.delete_user`` 的说明）："今天已经提交过"的证据
    必须留着，否则删一个人就能把当天的防重复判据一起抹掉 —— 而防重复正是
    "不要在同一天向学校重复提交"的最后一道。
    """
    kate = create_user(web_db, "kate")
    bind(kate)
    done_plan = make_plan(name="kate 已完成的", start_offset_minutes=10, owner=kate)

    scheduler = make_scheduler()
    scheduler._tick()
    settle(web_db, scheduler)
    runs_before = repo.list_runs(web_db)
    assert [r["plan_id"] for r in runs_before] == [done_plan]
    assert school.submits[-1][1] == user_cookie(kate)

    # 删账号之前再给他建一条还没跑的计划，然后删掉账号。
    pending_plan = make_plan(name="kate 待执行的", start_offset_minutes=20, owner=kate)
    user_auth.delete_user(web_db, kate)

    assert repo.get_plan_owner(web_db, done_plan) is None
    assert repo.get_plan_owner(web_db, pending_plan) is None
    assert repo.get_plan(web_db, done_plan) is not None, "计划不能被连带删掉"
    assert repo.list_runs(web_db) == runs_before, "运行记录一条都不能少"

    # 无主解释的下一层含义：他剩下的计划改用全局那份凭据，
    # 而不是"没有凭据所以永远跑不起来"。
    scheduler._tick()
    settle(web_db, scheduler)
    assert len(repo.list_runs(web_db)) == 2, "只多了待执行那一条"
    assert school.submits[-1][1] == GLOBAL_COOKIE
    assert repo.get_plan(web_db, pending_plan)["status"] is PlanStatus.SUCCEEDED


def test_resume_after_rebinding_only_touches_that_users_plans(web_db, make_plan):
    """重新绑定后的解冻只动他本人的、且是"启用 + 等待认证"的计划。"""
    alice = create_user(web_db, "alice")
    bob = create_user(web_db, "bob")
    a_plan = make_plan(name="alice 的", start_offset_minutes=10, owner=alice)
    b_plan = make_plan(name="bob 的", start_offset_minutes=20, owner=bob)
    repo.set_plan_status(web_db, a_plan, PlanStatus.AWAITING_AUTH)
    repo.set_plan_status(web_db, b_plan, PlanStatus.AWAITING_AUTH)

    assert uc.resume_owned_plans(web_db, find_user_id(web_db, "alice")) == 1
    assert repo.get_plan(web_db, a_plan)["status"] is PlanStatus.PENDING
    assert repo.get_plan(web_db, b_plan)["status"] is PlanStatus.AWAITING_AUTH