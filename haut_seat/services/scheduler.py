"""后台调度器：与浏览器生命周期解耦的预约任务执行器。

设计要点（对应需求「后台任务调度」）：
- **独立守护线程**轮询数据库，浏览器关掉、页面刷新都不影响运行；
- **同一计划不会并发执行**：进程内每计划一把锁 + 数据库层「是否有 running 记录」双重把关；
- **长任务绝不在 Web 请求线程里跑**：界面上的「立即执行」只是登记 + 起线程；
- **并发执行为「有界线程 + 时段锁」**：主循环只负责发现到点的计划并按触发时刻顺序
  把线程拉起来（``_spawn``），**绝不在主循环里同步等待任何一次网络预约流程**。
  线程数上限 = ``max_concurrent``，且**没有待处理队列** —— 本轮因为并发已满或时段锁
  被占而跳过的计划不会被囤起来，下一轮 tick 会重新评估（因此不存在无界堆积）。
  互斥的粒度是「区域 + 时段」（``services/slot_lock``）而不是整个进程：
  同一时段仍然只允许一个进程提交（跨进程保护不变），不同时段可以真正并行；
- **重启恢复**：启动时把遗留的 running 记录标成「已中断」；若该次运行已经发生过提交
  （``submit_count > 0``，提交前已落盘），则判定为**结果未知**并置计划为未知状态，
  **绝不自动重放**；
- **暂停 / 停止语义**：暂停=不再启动新运行；停止=不再启动新运行且不再发起新提交；
  正在飞行的 HTTP 请求不中断（中断只会制造未知结果）；
- **提前启动（可选，默认关闭）**：``trigger_lead_s`` > 0 时，计划会在
  ``trigger_ts - trigger_lead_s`` 就起线程，让 ``booking_runner`` 的**认证预检**在
  触发时刻之前完成；查询与提交仍然只能发生在触发时刻之后（由 ``booking_runner``
  的等待负责）。默认 ``0``：不配置时行为与从前逐字节一致；
- **认证闸门按计划判定（多用户）**：有主计划用**属主自己**的学校凭据状态决定是否
  可提交，无主（历史/管理端）计划沿用全局认证状态。凭据在运行前解析成一份
  settings 副本交给运行器 —— 运行器与并发模块因此一行都不用改。
  **取不到凭据时绝不用别人的顶上**，而是把该计划转入「等待认证」；
- 所有事件写 ``run_event``，写入前统一脱敏。
"""

from __future__ import annotations

import logging
import threading
import time as _stdlib_time
from datetime import date, datetime
from typing import Any, Callable

from haut_seat.models.booking import BookingResult
from haut_seat.services.booking_runner import (
    BookingDeps,
    BookingPlanConfig,
    RunReport,
    run_booking,
    ts_parse,
    wait_until,
)
from haut_seat.services.io import auth_stop_reason, query_online, submit_online
from haut_seat.services.state import (
    PLAN_STATUS_LABELS,
    ErrorCategory,
    PlanStatus,
    RunPhase,
    RunStatus,
    RunStop,
    plan_status_for,
)
from haut_seat.core.guard import check_booking_allowed
from haut_seat.core.schedule import effective_trigger_at, effective_window
from haut_seat.services.slot_lock import (
    DEFAULT_STALE_AFTER_S as DEFAULT_LOCK_STALE_S,
    DEFAULT_WAIT_S as DEFAULT_LOCK_WAIT_S,
    acquire_slot_lock,
    slot_lock_is_free,
    slot_lock_path,
)
from haut_seat.web import repository as repo
from haut_seat.web.validation import plan_is_due

DEFAULT_TICK_S = 5.0
DEFAULT_MAX_CONCURRENT = 3
_FMT = "%Y-%m-%d %H:%M:%S"


class ManualRunRefused(RuntimeError):
    pass


def plan_window_state(plan: dict, *, now: datetime | None = None) -> tuple[str, str]:
    """给界面用的窗口状态提示：返回 (代码, 中文说明)。仅展示，不改状态。"""
    now = now or datetime.now()
    today = now.date()
    if not plan_is_due(plan["run_date"], plan["repeat_rule"], today=today):
        # 只讲「今天不执行」和哪天执行：``repeat_rule`` 是内部代码（daily/weekdays），
        # 不印给用户看 —— 规则的中文说法由各页面自己按 repeat_labels 渲染。
        return "not_today", f"今天不执行（执行日期 {plan['run_date']}）"
    # 必须与调度器启动判断用同一个窗口：否则重复计划会显示「已过预约窗口」
    # 而调度器其实正准备执行（或反过来），又是一处自相矛盾。
    window_start, window_end = effective_window(
        plan["book_start"], plan["book_end"],
        repeat_rule=plan["repeat_rule"], today=today,
    )
    start_ts = ts_parse(window_start)
    end_ts = ts_parse(window_end)
    trigger_ts = ts_parse(effective_trigger_at(window_start, plan["trigger_at"]))
    if end_ts is None or start_ts is None:
        return "invalid", "时间参数不合法"
    now_ts = now.timestamp()
    if now_ts < (trigger_ts or 0):
        return "waiting", f"等待触发（{plan['trigger_at']} 开始抢约）"
    if now_ts >= end_ts:
        return "expired", "已过预约窗口，今天不会再执行"
    # 与 ``_maybe_start`` 的 ``now_ts >= start_ts`` 同源：座位使用时间已经开始，
    # 今天一样不会再执行（只是原因不同，所以文案单独说）。两处判据必须一致，
    # 否则又会出现「界面说已开放、调度器其实不会启动」的自相矛盾。
    if now_ts >= start_ts:
        return "expired", "座位使用时间已经开始，今天不会再执行"
    return "open", "预约窗口已开放"


def plan_trigger_ts(plan: dict, *, today: date) -> float | None:
    """该计划**今天**的触发时刻（epoch 秒）；时间参数不可解析时返回 ``None``。

    与 ``_maybe_start``、``plan_window_state``、``_run_once`` 用同一个
    ``effective_window`` + ``effective_trigger_at``，因此这里排出来的顺序
    就是调度器真正的启动顺序，不是另算一套。
    """
    try:
        window_start, _ = effective_window(
            plan["book_start"], plan["book_end"],
            repeat_rule=plan["repeat_rule"], today=today,
        )
    except Exception:
        return None
    return ts_parse(effective_trigger_at(window_start, plan["trigger_at"]))


# ------------------------------------------------------ 「同一天能不能再来一次」
#
# 下面两条判据被三处共用（自动启动 ``_maybe_start``、重复计划重排 ``_rearm_repeating``、
# 手动「立即执行」``request_run``），必须同源 —— 否则同一份状态会被三条路径解释成
# 三个不同的答案。判「当天」用的都是 ``run.started_at`` 的日期部分，与
# ``_rearm_repeating`` 的「今天已经跑过」检查、以及 ``effective_window`` 的窗口滚动
# 用的是同一个日期口径。
#
# ``submit_count`` 由调度器在**真正发出提交请求之前**落盘（见 ``_bump_submit_count``），
# 所以 ``submit_count > 0`` 的含义是「可能已经在服务器端留下了一条预约」。**不能**因为
# 这次运行记成了失败就假设服务器端一定没有记录 —— 因此一律按「今天不能再提交」保守处理。


def _started_today(run: dict, today: date) -> bool:
    """这条运行是不是「今天（或更晚）」起始的。与既有重排判据同款比较方式。"""
    return (run.get("started_at") or "")[:10] >= today.strftime("%Y-%m-%d")


def ran_irreversibly_today(run: dict | None, today: date) -> bool:
    """今天的这条运行是否已经产生了**不可回退**的结果：成功过，或提交过。

    返回 True 时，同一天不得再发起第二次提交（自动与手动都一样）。
    """
    if not run or not _started_today(run, today):
        return False
    return (
        run.get("status") is RunStatus.SUCCEEDED or int(run.get("submit_count") or 0) > 0
    )


def safe_to_run_again_after_interruption(run: dict | None, today: date) -> bool:
    """今天的这条运行是不是「进程中断、且从未提交过」——可以安全地再来一次。

    只有 ``recover_after_restart`` 会产出 ``INTERRUPTED``，也就是说：进程在提交之前
    就死了（``submit_count`` 停在 0，服务器端不可能留下任何东西）。提前启动会把
    「尚未提交」的窗口拉长，这类中断因此更需要被识别出来 —— 否则一次重启就会让
    重复计划白扔一整天。**提交过的一律返回 False**，绝不重放。
    """
    if not run or not _started_today(run, today):
        return False
    return run.get("status") is RunStatus.INTERRUPTED and int(run.get("submit_count") or 0) == 0


class Scheduler:
    """计划调度器。线程安全，可安全地由 Web 请求线程调用其公开方法。"""

    def __init__(
        self,
        db,
        settings,
        logger: logging.Logger | None = None,
        *,
        tick_s: float = DEFAULT_TICK_S,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        query_fn: Callable[..., Any] = query_online,
        submit_fn: Callable[..., BookingResult] = submit_online,
        auth_check_fn: Callable[..., Any] | None = None,
        notification=None,
        auth_service=None,
        clock: Callable[[], datetime] = datetime.now,
        time_mod: Any = _stdlib_time,
        wait_fn: Callable[[float], None] | None = wait_until,
        trigger_lead_s: float = 0.0,
        auto_start: bool = False,
    ):
        self.db = db
        self.settings = settings
        self.logger = logger or logging.getLogger("haut_seat.scheduler")
        self.tick_s = max(0.5, float(tick_s))
        # 提前启动量：>0 时线程在触发时刻之前就起来，只为让认证预检提前完成；
        # 查询与提交仍由 booking_runner 的等待挡在触发时刻之后。默认 0 = 行为不变。
        self.trigger_lead_s = max(0.0, float(trigger_lead_s))
        self.max_concurrent = max(1, int(max_concurrent))
        # 时段锁的等待上限与残留判定。两个都是**实例属性**，测试可以直接改小，
        # 不必真的等待或依赖设置项。
        self.book_lock_wait_s = max(
            0.0, float(getattr(settings, "book_lock_wait_s", DEFAULT_LOCK_WAIT_S) or 0.0)
        )
        self.book_lock_stale_s = max(
            1.0,
            float(getattr(settings, "book_lock_stale_s", DEFAULT_LOCK_STALE_S)
                  or DEFAULT_LOCK_STALE_S),
        )
        self._query_fn = query_fn
        self._submit_fn = submit_fn
        self._auth_check_fn = auth_check_fn
        self.notification = notification
        self.auth_service = auth_service
        self._clock = clock
        self._time = time_mod
        self._wait_fn = wait_fn

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._mutex = threading.Lock()
        self._active: dict[int, threading.Thread] = {}
        self._locks: dict[int, threading.Lock] = {}
        self.last_tick_at: str | None = None
        self.tick_count = 0
        self.last_error: str = ""
        if auto_start:
            self.start()

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.recover_after_restart()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="haut-seat-scheduler", daemon=True
        )
        self._thread.start()
        self.logger.info("调度器已启动（tick=%.1fs，最大并发=%d）", self.tick_s, self.max_concurrent)

    def stop(self, timeout: float = 5.0) -> None:
        """停止调度线程。**不**中断正在运行的预约（避免制造未知结果）。"""
        self._stop_event.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
        self._thread = None
        self.logger.info("调度器已停止")

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def active_plan_ids(self) -> list[int]:
        with self._mutex:
            return sorted(self._active.keys())

    def status(self) -> dict:
        return {
            "running": self.is_running(),
            "tick_s": self.tick_s,
            "max_concurrent": self.max_concurrent,
            "active_plans": self.active_plan_ids(),
            "last_tick_at": self.last_tick_at,
            "tick_count": self.tick_count,
            "last_error": self.last_error,
        }

    # ------------------------------------------------------------------ 重启恢复

    def recover_after_restart(self) -> list[dict]:
        """把服务重启前遗留的「运行中」记录判为中断。

        关键安全性：只要该次运行**已经发生过提交**（提交前已把它写入
        ``submit_count``），就必须判为「结果未知」，并且计划进入未知状态、
        需要人工核实后才可恢复 —— 绝不自动重放。
        """
        recovered: list[dict] = []
        try:
            stale_runs = repo.list_running_runs(self.db)
        except Exception as exc:
            self.logger.error("重启恢复读取运行记录失败: %s", type(exc).__name__)
            return recovered
        for run in stale_runs:
            submitted = int(run.get("submit_count") or 0) > 0
            message = (
                "服务重启时该次运行仍在进行，且已发生过提交，**结果未知**："
                "请在图书馆网站核对是否已有预约记录，再决定是否恢复计划。"
                if submitted
                else "服务重启时该次运行仍在进行（尚未提交过预约），已标记为中断。"
            )
            repo.finish_run(
                self.db, run["id"],
                status=RunStatus.INTERRUPTED,
                category=ErrorCategory.SUBMIT_UNKNOWN if submitted else ErrorCategory.INTERNAL_ERROR,
                stop_reason=RunStop.INTERNAL_ERROR.value,
                message=message,
                exit_code=1,
                # 原样写回提交前落盘的计数：``finish_run`` 的该参数默认值是 0，且更新语句
                # 会把这一列整列覆盖 —— 不传就等于把「可能已经提交过」的证据抹掉，
                # 而这条证据是 ``ran_irreversibly_today`` 判「今天不能再提交」的唯一依据。
                submit_count=int(run.get("submit_count") or 0),
                plan_status=PlanStatus.UNKNOWN if submitted else PlanStatus.FAILED,
            )
            repo.add_run_event(
                self.db, run["id"], phase=RunPhase.EXCEPTION,
                level="warning", message=message,
                category=ErrorCategory.SUBMIT_UNKNOWN if submitted else ErrorCategory.INTERNAL_ERROR,
            )
            recovered.append({"run_id": run["id"], "plan_id": run["plan_id"], "submitted": submitted})
            if submitted and self.notification is not None:
                self.notification.notify_submit_unknown(
                    plan_id=run["plan_id"], run_id=run["id"], detail=message
                )
            self.logger.warning("重启恢复：运行 #%s（计划 #%s）已标记为中断，已提交=%s",
                                run["id"], run["plan_id"], submitted)
        return recovered

    # ------------------------------------------------------------------ 主循环

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
                self.last_error = ""
            except Exception as exc:  # 单次 tick 异常不能让调度线程死掉
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.logger.exception("调度器 tick 异常")
                if self.notification is not None:
                    self.notification.notify_service_exception(detail=self.last_error)
            self._stop_event.wait(self.tick_s)

    def _tick(self) -> None:
        now = self._clock()
        self.last_tick_at = now.strftime(_FMT)
        self.tick_count += 1
        self._reap_finished()

        setting = repo.get_app_setting(self.db)
        if not int(setting.get("scheduler_enabled", 1)):
            return  # 调度总开关关闭：只做恢复与回收，不启动新运行
        self._rearm_repeating(now.date())
        if int(setting.get("global_pause", 0)):
            return  # 全局暂停：不启动任何新运行（正在飞行的不受影响）

        # 认证闸门**按计划**判定，不再是"一个全局布尔盖住所有计划"。
        #
        # 引入用户端之前，凭据只有一份（.env / 快照），所以"全局是否失效"就等于
        # "所有计划是否失效"。现在每个用户各有一份凭据，再用一个全局布尔就会
        # 出现两种错：某个用户的凭据过期却把**所有人**的计划都停掉；
        # 或者某人的凭据失效了却因为管理员那份还有效而**继续用错的账号提交**。
        #
        # 因此：有主的计划看它属主那份凭据的状态；无主（历史/管理端）计划
        # 保持与从前逐字节一致的全局判定。
        global_blocked = bool(self.auth_service and self.auth_service.blocks_submissions())
        owners = repo.plan_owners(self.db)
        credential_statuses = repo.user_credential_statuses(self.db)
        for plan in self._armed_pending_plans(now):
            try:
                blocked, reason = self._plan_auth_block(
                    plan, owners=owners, statuses=credential_statuses,
                    global_blocked=global_blocked,
                )
                self._maybe_start(plan, now, auth_blocked=blocked, auth_reason=reason)
            except Exception as exc:
                self.logger.exception("计划 #%s 调度异常", plan.get("id"))
                if self.notification is not None:
                    self.notification.notify_task_exception(
                        plan_id=plan.get("id"), detail=f"{type(exc).__name__}: {exc}"
                    )

    def _plan_auth_block(
        self, plan: dict, *, owners: dict, statuses: dict, global_blocked: bool
    ) -> tuple[bool, str]:
        """这个计划此刻是否因认证问题不能提交，以及为什么。

        **只有"确定失效"才拦**：``none``（未绑定）与 ``invalid``（已失效）拦，
        ``indeterminate``（探测不出结论，比如网络不通）不拦 —— 与
        ``AuthService.blocks_submissions`` 只认 ``invalid`` 是同一条原则：
        不确定绝不冒充成"确定失效"，否则一次网络抖动就能把计划永久停掉。
        """
        from haut_seat.services import user_credentials as uc

        owner_id = owners.get(int(plan["id"]))
        if owner_id is None:
            if global_blocked:
                return True, (
                    "登录凭据已失效，已暂停该计划的新提交。"
                    "请在正常浏览器登录后按「认证状态」页的步骤更新凭据。"
                )
            return False, ""
        status = statuses.get(int(owner_id), uc.CRED_STATUS_NONE)
        if status == uc.CRED_STATUS_VALID:
            return False, ""
        if status == uc.CRED_STATUS_INVALID:
            return True, (
                f"计划所属用户（#{owner_id}）绑定的图书馆账号登录已失效，"
                "已暂停该计划的新提交，等待其本人重新登录绑定。"
            )
        if status == uc.CRED_STATUS_NONE:
            return True, (
                f"计划所属用户（#{owner_id}）尚未绑定图书馆账号，"
                "无法提交预约。请该用户在用户端完成「绑定图书馆账号」。"
            )
        return False, ""  # indeterminate：不拦（见上面的说明）

    def _armed_pending_plans(self, now: datetime | None = None) -> list[dict]:
        """今天该执行的「待执行」计划，**按触发时刻从近到远**排序。

        排序是并发闸门公平性的关键：``max_concurrent`` 满了之后本轮没被拾取的
        计划要等下一轮，如果顺序是按 id 倒序（历史上就是如此），一个刚建出来的、
        几小时后才触发的计划会先把并发槽位占掉，把 30 秒后就要触发的老计划挤出去 ——
        而预约窗口是以秒计的。按「距触发时刻最近者优先」排，本就该最先动的计划
        先拿到容量；触发时刻相同的按 id 升序（先建的先来，保持稳定）。
        """
        today = (now or self._clock()).date()
        plans = [
            p for p in repo.list_plans(self.db)
            if p["status"] is PlanStatus.PENDING and int(p["enabled"])
        ]

        def sort_key(plan: dict):
            ts = plan_trigger_ts(plan, today=today)
            # 时间参数解析不出（或解析为过去）的计划排在最后：它们本来就通不过
            # ``_maybe_start`` 的检查，不该占着前排位置。
            return (0, ts, plan["id"]) if ts is not None else (1, 0.0, plan["id"])

        return sorted(plans, key=sort_key)

    def _rearm_repeating(self, today: date) -> None:
        """重复计划（每天 / 工作日）在次日重新排入待执行。

        只对「预约成功 / 预约失败」重新武装；**结果未知的计划绝不自动重排**。
        """
        today_s = today.strftime("%Y-%m-%d")
        for plan in repo.list_plans(self.db):
            if plan["repeat_rule"] == "once":
                continue
            if plan["status"] not in (PlanStatus.SUCCEEDED, PlanStatus.FAILED):
                continue
            if not plan_is_due(plan["run_date"], plan["repeat_rule"], today=today):
                continue
            last = repo.last_run_for_plan(self.db, plan["id"])
            if last and (last["started_at"] or "")[:10] >= today_s:
                # 今天已经跑过 —— 唯一的例外是「提交之前被重启打断」的那一次：
                # 那种情况下服务器端不可能留下预约（submit_count 仍为 0），
                # 今天还有机会，不该白扔一天。提交过的一律不重排。
                if not safe_to_run_again_after_interruption(last, today):
                    continue
                self.logger.info(
                    "重复计划 #%s 今天有一次「提交前被中断」的运行 #%s，重新排入待执行",
                    plan["id"], last.get("id"),
                )
            repo.set_plan_status(self.db, plan["id"], PlanStatus.PENDING)
            self.logger.info("重复计划 #%s 已重新排入待执行", plan["id"])

    def _maybe_start(
        self, plan: dict, now: datetime, *, auth_blocked: bool, auth_reason: str = ""
    ) -> None:
        plan_id = plan["id"]
        if not plan_is_due(plan["run_date"], plan["repeat_rule"], today=now.date()):
            return
        if int(plan["pause_requested"]) or int(plan["stop_requested"]):
            return
        if plan_id in self.active_plan_ids():
            return
        if repo.plan_has_active_run(self.db, plan_id):
            return
        # 结果未知且未人工确认 → 绝不自动重放
        last = repo.last_run_for_plan(self.db, plan_id)
        if last and last["status"] is RunStatus.UNKNOWN and not plan.get("unknown_ack_at"):
            return
        # 今天已经成功过、或已经提交过（哪怕记成失败）→ 不再自动来第二次
        if ran_irreversibly_today(last, now.date()):
            return

        # 重复计划的窗口要滚动到当天（``once`` 原样返回），且触发时刻取同一个
        # 窗口日期 —— 四处（此处、提交参数、手动执行、界面提示）必须同源。
        window_start, window_end = effective_window(
            plan["book_start"], plan["book_end"],
            repeat_rule=plan["repeat_rule"], today=now.date(),
        )
        trigger_ts = ts_parse(effective_trigger_at(window_start, plan["trigger_at"]))
        end_ts = ts_parse(window_end)
        start_ts = ts_parse(window_start)
        if trigger_ts is None or end_ts is None or start_ts is None:
            self.logger.warning("计划 #%s 时间参数不合法，跳过", plan_id)
            return
        now_ts = now.timestamp()
        # 提前量只放宽**启动**门槛：线程可以早 ``trigger_lead_s`` 秒起来跑认证预检，
        # 但查询与提交仍被 booking_runner 的等待挡在触发时刻之后。
        if now_ts < trigger_ts - self.trigger_lead_s:
            return  # 还没到（含提前量的）启动时刻
        if now_ts >= end_ts:
            return  # 窗口已过；状态保持不变，界面会提示「已过预约窗口」
        # 「触发时刻已过」与「座位使用开始时间已过」是两回事：前者可以补跑，后者不行。
        # 座位使用时段已经开始，服务器必然拒绝（booking_runner 会判 CONFIG_ERROR），
        # 这里直接不启动，免得留下一条注定失败的运行记录。
        if now_ts >= start_ts:
            self.logger.info(
                "计划 #%s 的座位使用开始时间（%s）已到，今天不再启动", plan_id, window_start
            )
            return

        if auth_blocked:
            # 认证失效：不再启动新运行，并把计划显式标成「等待认证」以便一眼看到
            repo.set_plan_status(self.db, plan_id, PlanStatus.AWAITING_AUTH)
            detail = auth_reason or (
                "登录凭据已失效，已暂停该计划的新提交。"
                "请在正常浏览器登录后按「认证状态」页的步骤更新凭据。"
            )
            self.logger.warning("认证不可用（%s），计划 #%s 转入等待认证", detail, plan_id)
            if self.notification is not None:
                self.notification.notify_auth_failure(plan_id=plan_id, detail=detail)
            return

        # 全局真实预约开关 / 暂停旗标（与 CLI 共用同一判据）
        decision = check_booking_allowed(
            self.settings.enable_real_booking, self.settings.pause_file
        )
        if not decision.allowed:
            self.logger.info("计划 #%s 未放行：%s", plan_id, decision.reason)
            return

        # 该时段已被占用（本进程的另一个计划，或 CLI 进程）⇒ 本轮不起线程。
        # 起线程去锁上等只会白占一个并发槽位：下一轮 tick 会重新评估，
        # 因此这里不排队、不等待、也不改变计划状态。
        if not self._slot_lock_free(plan):
            self.logger.info(
                "计划 #%s 本轮跳过：时段锁 %s 已被占用（同时段的另一个预约任务在运行）",
                plan_id, self._book_lock_path(plan),
            )
            return

        self._spawn(plan_id, trigger="scheduler")

    # ------------------------------------------------------------------ 执行

    def _spawn(self, plan_id: int, *, trigger: str) -> bool:
        """登记并起一个运行线程。**只做登记**，绝不在调用线程里跑预约流程。

        两道上限合起来保证「不产生无界线程、也不产生无界积压」：

        1. ``len(self._active) >= self.max_concurrent`` ⇒ 直接不启动。跳过的计划
           **不入队**，只是本轮不动它，下一轮 tick 重新评估 —— 因此不存在
           "越攒越多"的待处理队列；
        2. 每计划一把进程内锁（``blocking=False``）+ 数据库里的 ``running`` 记录，
           保证同一个计划永远只有一个执行实例。

        线程是 ``daemon=True``：预约运行是长时间的网络等待，非守护线程会让
        Ctrl+C / 解释器退出挂在一次正在飞行的提交上（那正是最不该被打断的东西）。
        """
        with self._mutex:
            if plan_id in self._active:
                return False
            if len(self._active) >= self.max_concurrent:
                self.logger.info("并发已达上限（%d），计划 #%s 本轮跳过", self.max_concurrent, plan_id)
                return False
            lock = self._locks.setdefault(plan_id, threading.Lock())
            if not lock.acquire(blocking=False):
                return False
            thread = threading.Thread(
                target=self._worker, args=(plan_id, trigger, lock),
                name=f"haut-seat-run-{plan_id}", daemon=True,
            )
            self._active[plan_id] = thread
        thread.start()
        return True

    def _reap_finished(self) -> None:
        with self._mutex:
            dead = [pid for pid, t in self._active.items() if not t.is_alive()]
            for pid in dead:
                self._active.pop(pid, None)

    def _worker(self, plan_id: int, trigger: str, lock: threading.Lock) -> None:
        try:
            self._execute(plan_id, trigger=trigger)
        except Exception as exc:
            self.logger.exception("计划 #%s 执行线程异常", plan_id)
            if self.notification is not None:
                self.notification.notify_task_exception(
                    plan_id=plan_id, detail=f"{type(exc).__name__}: {exc}"
                )
        finally:
            lock.release()
            with self._mutex:
                self._active.pop(plan_id, None)

    def _plan_slot(self, plan: dict, *, now: datetime | None = None):
        """计划**今天**的（区域, 时段起, 时段止）；算不出锁名时退回全局锁。

        ``effective_window`` 让重复计划的窗口滚到今天 —— 与提交参数、
        启动判据、界面提示四处同源，绝不另算一套。
        """
        try:
            window_start, window_end = effective_window(
                plan["book_start"], plan["book_end"],
                repeat_rule=plan["repeat_rule"],
                today=(now or self._clock()).date(),
            )
        except Exception:  # 时间字段异常：退化成全局锁，宁可串行也不无锁提交
            self.logger.warning("计划 #%s 的时段无法解析，改用全局锁", plan.get("id"))
            return None, None, None
        return plan.get("regionid"), window_start, window_end

    def _execute(self, plan_id: int, *, trigger: str) -> RunReport | None:
        plan = repo.get_plan(self.db, plan_id)
        if plan is None:
            return None
        # 先取该计划该用的凭据：取不到就**根本不开这次运行**。
        # 放在拿时段锁之前是有意的 —— 连凭据都没有的时候去占住一个时段锁，
        # 只会挡住同时段其它真的能跑的（别人的）任务。
        run_settings, block_reason = self._run_settings_for(plan)
        if run_settings is None:
            self._mark_awaiting_auth(plan_id, block_reason)
            return None
        # 跨进程互斥，粒度是「区域 + 时段」：与 CLI 的 `book` 命令共用同一套锁名，
        # 避免「cron 的 CLI」与「Web 调度器」同时向**同一时段**提交预约。
        # 不同时段各拿各的锁，因此互不拖慢 —— 这是并发能真正生效的前提。
        regionid, start, end = self._plan_slot(plan)
        book_lock = acquire_slot_lock(
            self.settings,
            regionid=regionid, start=start, end=end,
            wait_s=self.book_lock_wait_s,
            stale_after_s=self.book_lock_stale_s,
            logger=self.logger,
            plan_id=plan_id,
        )
        if book_lock is None:
            return None
        try:
            return self._run_once(plan_id, plan, trigger=trigger, run_settings=run_settings)
        finally:
            book_lock.release()

    def _run_settings_for(self, plan: dict):
        """这次运行该用哪一份 settings（含凭据）；取不到返回 ``(None, 原因)``。

        - **无主计划**（历史计划与管理端建的计划）⇒ 原样返回 ``self.settings``，
          与从前逐字节一致；
        - **有主计划** ⇒ 解出该用户自己的凭据，装进一份 settings 副本。

        失败时**绝不回退**到 ``self.settings``：那等于拿别人的账号去提交，
        正是这套多账号机制要根除的东西。宁可这次不开运行，并让计划转入
        「等待认证」，由属主本人重新登录。
        """
        from haut_seat.services import user_credentials as uc

        owner_id = repo.get_plan_owner(self.db, plan["id"])
        if owner_id is None:
            return self.settings, ""
        try:
            run_settings, _creds = uc.settings_for_user(self.db, self.settings, owner_id)
        except uc.NoCredentialError:
            return None, (
                f"计划所属用户（#{owner_id}）尚未绑定图书馆账号，无法提交预约。"
                "请该用户在用户端完成「绑定图书馆账号」。"
            )
        except uc.CredentialDecryptError:
            return None, (
                f"计划所属用户（#{owner_id}）已保存的凭据无法解密"
                "（加密主密钥可能已更换）。请该用户重新绑定图书馆账号。"
            )
        except Exception as exc:  # 主密钥不可用等：同样不拿别人的凭据顶上
            self.logger.error(
                "读取用户 #%s 的凭据失败：%s", owner_id, type(exc).__name__
            )
            return None, (
                f"计划所属用户（#{owner_id}）的凭据暂时无法读取"
                f"（{type(exc).__name__}）。请联系管理员检查凭据加密配置。"
            )
        return run_settings, ""

    def _mark_awaiting_auth(self, plan_id: int, reason: str) -> None:
        """凭据不可用：把计划标成「等待认证」并如实通知（不创建假的运行记录）。"""
        repo.set_plan_status(self.db, plan_id, PlanStatus.AWAITING_AUTH)
        self.logger.warning("计划 #%s 转入等待认证：%s", plan_id, reason)
        if self.notification is not None:
            self.notification.notify_auth_failure(plan_id=plan_id, detail=reason)

    def _book_lock_path(self, plan: dict | None = None) -> str:
        """该计划今天应使用的锁文件路径；``plan`` 为空时返回全局锁路径。"""
        regionid, start, end = (None, None, None)
        if plan is not None:
            regionid, start, end = self._plan_slot(plan)
        return str(slot_lock_path(self.settings, regionid=regionid, start=start, end=end))

    def _slot_lock_free(self, plan: dict) -> bool:
        """探测该计划的时段锁是否空闲（探测后立即释放，不持有）。"""
        regionid, start, end = self._plan_slot(plan)
        return slot_lock_is_free(
            self.settings, regionid=regionid, start=start, end=end,
            stale_after_s=self.book_lock_stale_s,
        )

    def _slot_lock_busy(self, plan: dict) -> bool:
        """只做一次「探测后立即释放」，用于给手动执行一个友好的前置提示。"""
        return not self._slot_lock_free(plan)

    def _run_once(
        self, plan_id: int, plan: dict, *, trigger: str, run_settings=None
    ) -> RunReport | None:
        run_id = repo.start_run(self.db, plan_id, trigger=trigger)
        reporter = self._make_reporter(run_id)
        reporter(RunPhase.START, "info", f"运行 #{run_id} 开始（触发方式：{trigger}）")

        # 提交给图书馆的 starttime/endtime 必须用**当天**的窗口：重复计划若沿用
        # 建计划那天的日期，服务器会以「必须是同一天 / 只能预约当天」拒绝。
        window_start, window_end = effective_window(
            plan["book_start"], plan["book_end"],
            repeat_rule=plan["repeat_rule"], today=self._clock().date(),
        )
        config = BookingPlanConfig.from_mapping(
            {
                "regionid": plan["regionid"],
                "start": window_start,
                "end": window_end,
                "preferred_seats": plan["preferred_seats"],
                "preferred_prefixes": plan["preferred_prefixes"],
                "confirmed_bookable_states": plan["confirmed_bookable_states"],
                "trigger_at": plan["trigger_at"],
                "max_attempts_per_seat": plan["max_attempts_per_seat"],
            },
            fallback_regionid=self.settings.regionid,
        )
        deps = BookingDeps(
            query_fn=self._query_fn,
            submit_fn=self._submit_fn,
            auth_check_fn=self._auth_check_fn or _default_auth_check,
            allowed_fn=lambda: check_booking_allowed(
                self.settings.enable_real_booking, self.settings.pause_file
            ),
            report=reporter,
            wait_fn=self._wait_fn,
            auth_stop_reason_fn=auth_stop_reason,
            time_mod=self._time,
            should_continue=lambda: self._should_continue(plan_id),
            on_submit_started=lambda: self._bump_submit_count(run_id),
        )
        report = run_booking(
            run_settings if run_settings is not None else self.settings,
            config, deps, self.logger,
        )
        self._finalize(plan_id, run_id, report)
        return report

    def _make_reporter(self, run_id: int):
        def _report(phase, level, message, category=None) -> None:
            try:
                repo.add_run_event(
                    self.db, run_id, phase=phase, level=level,
                    message=message, category=category,
                )
            except Exception:
                self.logger.warning("运行事件写入失败（已忽略）", exc_info=True)

        return _report

    def _bump_submit_count(self, run_id: int) -> None:
        """在真正提交之前落盘：崩溃/重启后据此判定「结果未知」。"""
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE run SET submit_count = submit_count + 1 WHERE id = ?", (run_id,)
            )

    def _should_continue(self, plan_id: int) -> tuple[bool, str]:
        """每次循环、每次提交前调用：判断是否还应发起新的提交。"""
        plan = repo.get_plan(self.db, plan_id)
        if plan is None:
            return False, "计划已被删除，停止运行"
        if int(plan["stop_requested"]):
            return False, "收到停止请求：不再发起新的提交"
        if int(plan["pause_requested"]):
            return False, "收到暂停请求：不再发起新的提交"
        if plan["status"] in (PlanStatus.PAUSED, PlanStatus.STOPPED, PlanStatus.SUCCEEDED):
            return False, f"计划状态已变为「{PLAN_STATUS_LABELS[plan['status']]}」，停止运行"
        setting = repo.get_app_setting(self.db)
        if int(setting.get("global_pause", 0)):
            return False, "全局暂停已开启：不再发起新的提交"
        if self._auth_now_blocked(plan):
            return False, "认证已失效：停止新的提交，等待重新登录绑定"
        return True, ""

    def _auth_now_blocked(self, plan: dict) -> bool:
        """运行中复查用的认证闸门：有主计划看属主，无主计划看全局。

        与 ``_plan_auth_block`` 同一套判据（只认"确定失效"），但每次调用都重新读
        数据库：运行可能在几分钟的窗口里跨越"用户恰好在这个时间重新登录/解绑"，
        因此不能沿用 tick 开始时算出的那份快照。
        """
        from haut_seat.services import user_credentials as uc

        owner_id = repo.get_plan_owner(self.db, int(plan["id"]))
        if owner_id is None:
            return bool(self.auth_service and self.auth_service.blocks_submissions())
        status = uc.credentials_status(self.db, owner_id)
        return status in (uc.CRED_STATUS_INVALID, uc.CRED_STATUS_NONE)

    def _finalize(self, plan_id: int, run_id: int, report: RunReport) -> None:
        plan_status = self._plan_status_after(plan_id, report)
        repo.finish_run(
            self.db, run_id,
            status=report.status,
            category=report.category,
            stop_reason=report.stop.value,
            message=report.message,
            exit_code=report.exit_code,
            booking_id=report.booking_id,
            query_count=report.query_count,
            submit_count=report.submit_count,
            seat_switches=report.seat_switches,
            plan_status=plan_status,
        )
        repo.add_run_event(
            self.db, run_id, phase=RunPhase.END,
            level="info" if report.status in (RunStatus.SUCCEEDED, RunStatus.AWAITING_AUTH) else "warning",
            message=f"运行结束：{report.message}",
            category=report.category,
        )
        self._after_run(plan_id, run_id, report)

    def _plan_status_after(self, plan_id: int, report: RunReport) -> PlanStatus:
        if report.stop is RunStop.STOPPED:
            plan = repo.get_plan(self.db, plan_id) or {}
            if int(plan.get("stop_requested") or 0):
                return PlanStatus.STOPPED
            if int(plan.get("pause_requested") or 0):
                return PlanStatus.PAUSED
            current = plan.get("status")
            if current in (PlanStatus.PAUSED, PlanStatus.STOPPED):
                return current
            return PlanStatus.STOPPED
        if report.status is RunStatus.UNKNOWN:
            return PlanStatus.UNKNOWN  # 未知独立成态，绝不写成「失败」
        return plan_status_for(report.status)

    def _after_run(self, plan_id: int, run_id: int, report: RunReport) -> None:
        notify = self.notification
        try:
            if report.stop is RunStop.AUTH_INVALID:
                # 认证失效要记到**该计划真正用的那份凭据**上：
                # 有主计划记到属主名下（他自己的凭据确实失效了），
                # 无主计划才记全局状态。
                #
                # 这一条是隔离的关键：如果把某个用户的失效写进全局 auth_state，
                # 就会连带停掉管理端与其它用户的计划 —— 一个人的账号过期让所有人
                # 停工；反过来只记全局不记用户，那位用户的计划会一直"看起来正常"，
                # 下一次触发又白跑一遍。两种都是错的，所以两条路径分开走。
                owner_id = repo.get_plan_owner(self.db, plan_id)
                if owner_id is not None:
                    from haut_seat.services import user_credentials as uc

                    uc.mark_status(
                        self.db, owner_id, uc.CRED_STATUS_INVALID,
                        f"运行 #{run_id} 检测到登录失效：{report.message}",
                        source="scheduler",
                    )
                    self.logger.warning(
                        "用户 #%s 的学校凭据已被判定失效（计划 #%s，运行 #%s）",
                        owner_id, plan_id, run_id,
                    )
                else:
                    repo.set_auth_state(
                        self.db, status="invalid",
                        message=f"运行 #{run_id} 检测到认证失效：{report.message}",
                        source="scheduler",
                    )
                if notify is not None:
                    notify.notify_auth_failure(
                        plan_id=plan_id, run_id=run_id, detail=report.message
                    )
                return
            if report.status is RunStatus.SUCCEEDED and notify is not None:
                notify.notify_booking_success(
                    plan_id=plan_id, run_id=run_id, detail=report.message
                )
            elif report.status is RunStatus.UNKNOWN and notify is not None:
                notify.notify_submit_unknown(
                    plan_id=plan_id, run_id=run_id, detail=report.message
                )
            elif report.status is RunStatus.NO_SEAT and notify is not None:
                notify.notify_no_seat(plan_id=plan_id, run_id=run_id, detail=report.message)
            elif report.stop in (RunStop.SUBMIT_EXCEPTION, RunStop.INTERNAL_ERROR) and notify is not None:
                notify.notify_task_exception(
                    plan_id=plan_id, run_id=run_id, detail=report.message
                )
        except Exception:
            # 通知/状态记录的任何问题都不能影响预约任务本身
            self.logger.warning("运行后处理异常（已忽略）", exc_info=True)

    # ------------------------------------------------------------------ 手动执行

    def request_run(self, plan_id: int) -> dict:
        """「立即执行」（供 Web 请求线程调用）。

        只做「登记 + 起线程」，长任务绝不在这里执行；仍在提交中的计划不会被重入。
        """
        plan = repo.get_plan(self.db, plan_id)
        if plan is None:
            return {"ok": False, "message": "计划不存在"}
        if plan_id in self.active_plan_ids() or repo.plan_has_active_run(self.db, plan_id):
            return {"ok": False, "message": "该计划正在运行中，不会重复启动"}
        if self._slot_lock_busy(plan):
            return {
                "ok": False,
                "message": f"同一时段（区域 {plan['regionid']}，"
                           f"{plan['book_start']} → {plan['book_end']}）的另一个预约任务"
                           f"正在运行（{self._book_lock_path(plan)} 被占用），请稍后重试",
            }
        if plan["status"] is PlanStatus.UNKNOWN:
            return {
                "ok": False,
                "message": "上一次提交的结果未知：请先在图书馆网站核实是否已有预约记录，"
                           "确认后在计划页勾选「已核实」再恢复执行。",
            }
        now = self._clock()
        # 同一天不重复提交：今天已经成功过、或已经提交过（哪怕这次记成了失败，
        # 也不能假设服务器端没有留下记录），就拒绝这次「立即执行」。
        # 这层判断在启动互斥锁之外，但不会与并发调用打架：真正起线程的 ``_spawn``
        # 会在自己的 mutex 里再查一次 ``_active``，两个并发请求只有一个能过。
        last = repo.last_run_for_plan(self.db, plan_id)
        if ran_irreversibly_today(last, now.date()):
            if last.get("status") is RunStatus.SUCCEEDED:
                reason = "今天的预约已经成功"
            else:
                reason = (
                    f"今天已经发起过提交（运行 #{last.get('id')}，"
                    f"提交 {int(last.get('submit_count') or 0)} 次）"
                )
            return {"ok": False, "message": f"该计划今天已经执行过：{reason}。同一天不会重复提交。"}
        if not plan_is_due(plan["run_date"], plan["repeat_rule"], today=now.date()):
            return {"ok": False, "message": f"该计划今天不执行（{plan['run_date']} / {plan['repeat_rule']}）"}
        window_start, window_end = effective_window(
            plan["book_start"], plan["book_end"],
            repeat_rule=plan["repeat_rule"], today=now.date(),
        )
        end_ts = ts_parse(window_end)
        trigger_ts = ts_parse(effective_trigger_at(window_start, plan["trigger_at"]))
        if end_ts is None or trigger_ts is None:
            return {"ok": False, "message": "计划的时间参数不合法，请先编辑修正"}
        now_ts = now.timestamp()
        if now_ts >= end_ts:
            return {"ok": False, "message": "已过预约窗口，无法立即执行"}
        if now_ts < trigger_ts:
            return {
                "ok": False,
                "message": f"触发时刻（{plan['trigger_at']}）尚未到达，调度器会在到点时自动执行，无需手动启动",
            }
        if self._auth_now_blocked(plan):
            if repo.get_plan_owner(self.db, plan_id) is not None:
                return {
                    "ok": False,
                    "message": "该计划所属用户尚未绑定图书馆账号，或其登录已失效："
                               "请该用户在用户端完成「绑定图书馆账号」后再执行",
                }
            return {"ok": False, "message": "认证已失效：请先在「认证状态」页按步骤更新凭据"}
        setting = repo.get_app_setting(self.db)
        if int(setting.get("global_pause", 0)):
            return {"ok": False, "message": "全局暂停已开启：请先在系统页关闭全局暂停"}
        decision = check_booking_allowed(
            self.settings.enable_real_booking, self.settings.pause_file
        )
        if not decision.allowed:
            return {"ok": False, "message": f"未放行：{decision.reason}"}
        if not self._spawn(plan_id, trigger="manual"):
            return {"ok": False, "message": "并发已满或该计划已在运行，请稍后重试"}
        return {"ok": True, "message": "已开始执行（后台线程运行，可关闭页面）"}


def _default_auth_check(settings, regionid, start=None, end=None):
    from haut_seat.services.io import auth_precheck

    return auth_precheck(settings, regionid, start, end)