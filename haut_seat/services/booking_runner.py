"""预约主循环的服务化封装：CLI 与 Web 后台调度器共用同一份实现。

抽取原则（对应「不要复制或重写一套预约实现」的约束）：
- 主循环的控制流、判据、重试/切换策略与 ``__main__.cmd_book`` **完全一致**；
- 网络、时间、锁、放行检查、事件上报全部通过 ``BookingDeps`` 注入，
  这样 CLI 用真实实现、Web 用带事件记录的实现、测试用替身，而逻辑只有一份；
- 本模块**不**自行获取文件锁、**不**读写数据库、**不**打印到 stdout，
  这些由调用方（``cmd_book`` / 调度器）负责。

安全语义（保持并强化）：
- 认证失效（valid/invalid/indeterminate 三态中的 invalid）→ 立即停止，
  既不轮询座位也不提交预约；
- 提交结果未知 → 立刻停止，绝不重试（``decide_submit_action`` 已保证）；
- 开放时刻（窗口当天 07:00，官方规则）**之前只做只读查询、绝不提交**：
  提前启动（``scheduler_trigger_lead_s``）或窗口内补跑都不可能把提交提前；
- 服务器**明确**说「尚未开放」时按可恢复的暂时状态处理：不计入重试次数、
  不结束这次运行，退回只读查询按间隔重来，直到窗口关闭为止；
- ``should_continue`` 返回 False（计划被暂停/停止）时，只停止「发起新的提交」，
  已在飞行的请求会走到应答，避免制造新的未知结果。
"""

from __future__ import annotations

import time as _stdlib_time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Protocol

from haut_seat.core.auth import RELOGIN_HINT
from haut_seat.core.booking import (
    decide_submit_action,
    is_not_open_yet,
    parse_not_open_keywords,
)
from haut_seat.core.guard import BookingDecision
from haut_seat.core.priority import (
    classify_seat,
    pick_bookable_candidates,
    seat_state_label,
)
from haut_seat.core.schedule import booking_opens_at
from haut_seat.models.booking import BookingOutcome
from haut_seat.services.state import (
    ErrorCategory,
    RunPhase,
    RunStatus,
    RunStop,
    error_category_for,
    exit_code_for,
    run_status_for,
)

COOLDOWN_S = 120
DEFAULT_TRIGGER_AT = "07:00:00"
QUERY_RETRY_DELAY_S = 30.0
NO_CANDIDATE_DELAY_S = 30.0
# 「还没到开放时间」时的重新查询间隔（秒）。与「无候选座位」同一个量级：
# 开放前只是白等，但等太久会错过开放后的第一秒，所以间隔要短于一次退避的上限。
NOT_OPEN_DELAY_S = 30.0


class Reporter(Protocol):
    def __call__(
        self,
        phase: RunPhase,
        level: str,
        message: str,
        category: ErrorCategory | None = None,
        **extra: Any,
    ) -> None: ...


@dataclass(frozen=True)
class BookingPlanConfig:
    """一次预约运行所需的计划参数（来自 CLI JSON 或数据库行）。"""

    regionid: Any
    start: str
    end: str
    preferred_seats: tuple[str, ...] = ()
    preferred_prefixes: tuple[str, ...] = ()
    confirmed_bookable_states: tuple[Any, ...] = ()
    trigger_at: str = ""
    trigger_offset_s: Any = None
    max_attempts_per_seat: int | None = None

    @classmethod
    def from_mapping(cls, cfg: dict, *, fallback_regionid: Any = None) -> "BookingPlanConfig":
        return cls(
            regionid=cfg.get("regionid", fallback_regionid),
            start=str(cfg.get("start", "")),
            end=str(cfg.get("end", "")),
            preferred_seats=tuple(cfg.get("preferred_seats") or ()),
            preferred_prefixes=tuple(cfg.get("preferred_prefixes") or ()),
            confirmed_bookable_states=tuple(cfg.get("confirmed_bookable_states") or ()),
            trigger_at=str(cfg.get("trigger_at", "") or ""),
            trigger_offset_s=cfg.get("trigger_offset_s"),
            max_attempts_per_seat=cfg.get("max_attempts_per_seat"),
        )


@dataclass
class BookingDeps:
    """注入的运行依赖。CLI 注入真实实现，Web 注入带事件记录的实现。"""

    query_fn: Callable[..., Any]
    submit_fn: Callable[..., Any]
    auth_check_fn: Callable[..., Any]
    allowed_fn: Callable[[], BookingDecision]
    report: Reporter
    wait_fn: Callable[[float], None] | None = None
    auth_stop_reason_fn: Callable[[Exception], str | None] | None = None
    time_mod: Any = field(default=_stdlib_time)
    # 返回 (是否继续, 原因)。None 表示不检查（CLI 行为不变）。
    should_continue: Callable[[], tuple[bool, str]] | None = None
    # 在**真正调用提交接口之前**立即回调一次。
    # 用途：让 Web 端把「已发生一次提交」持久化，这样进程被杀后重启
    # 也能判定该次运行「结果未知」，而不是误当成「从未提交、可重放」。
    on_submit_started: Callable[[], None] | None = None


@dataclass
class RunReport:
    """一次运行的完整结果（供 CLI 退出码与 Web 记录使用）。"""

    stop: RunStop
    status: RunStatus
    category: ErrorCategory
    message: str
    exit_code: int
    booking_id: int | None = None
    query_count: int = 0
    submit_count: int = 0
    seat_switches: int = 0

    @property
    def is_unknown(self) -> bool:
        """结果未知：界面必须与「失败」区分，且不得自动重放。"""
        return self.status is RunStatus.UNKNOWN


def ts_parse(s: str) -> float | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s.strip(), fmt).timestamp()
        except ValueError:
            continue
    return None


def resolve_submit_time(
    start_str: str,
    start_ts: float,
    end_ts: float,
    buffer_s: int = 5,
    *,
    time_mod: Any = None,
) -> tuple[str | None, bool]:
    """返回 (有效开始时间, 是否调整过)。

    buffer_s = 服务器要求 '开始>当前' 的安全余量（含网络延迟）。
    如果配置的开始时间过于接近当前时间，自动向后推到 now + buffer_s。
    调整超过 60s 或超过结束时间 → 返回 (None, True)，调用方应停止。
    """
    time_mod = time_mod or _stdlib_time
    now_ts = time_mod.time()
    if start_ts > now_ts + buffer_s:
        return start_str, False
    adj_ts = now_ts + buffer_s
    if adj_ts - start_ts > 60 or adj_ts >= end_ts:
        return None, True  # 调整幅度过大或超过结束时间
    adj_str = datetime.fromtimestamp(adj_ts).strftime("%Y-%m-%d %H:%M:%S")
    return adj_str, True


def wait_until(target_ts: float, check_s: float = 15.0, *, time_mod: Any = None) -> None:
    """等待到目标时刻，期间定期提示剩余时间。"""
    time_mod = time_mod or _stdlib_time
    while True:
        now = time_mod.time()
        if now >= target_ts:
            return
        sleep_s = min(target_ts - now, check_s)
        time_mod.sleep(max(sleep_s, 1.0))
        remaining = target_ts - time_mod.time()
        if remaining > 0:
            print(f"等待预约窗口开放，约 {remaining:.0f}s 后开始")


def resolve_trigger_ts(config: BookingPlanConfig, start_ts: float) -> tuple[float | None, str, bool]:
    """计算触发时刻。返回 (trigger_ts, trigger_at, 是否使用了废弃的 trigger_offset_s)。"""
    trigger_at = config.trigger_at
    deprecated = False
    if not trigger_at:
        if config.trigger_offset_s is not None:
            deprecated = True
            trigger_dt = datetime.fromtimestamp(start_ts) - timedelta(
                seconds=config.trigger_offset_s
            )
            trigger_at = trigger_dt.strftime("%H:%M:%S")
        else:
            trigger_at = DEFAULT_TRIGGER_AT
    day = datetime.fromtimestamp(start_ts)
    return ts_parse(f"{day:%Y-%m-%d} {trigger_at}"), trigger_at, deprecated


class _Runner:
    def __init__(
        self,
        settings: Any,
        config: BookingPlanConfig,
        deps: BookingDeps,
        logger: Any = None,
    ):
        self.settings = settings
        self.config = config
        self.deps = deps
        self.logger = logger
        self.regionid = config.regionid
        self.start = config.start
        self.end = config.end
        self.max_attempts = int(
            config.max_attempts_per_seat
            or getattr(settings, "max_attempts_per_seat", 3)
            or 3
        )
        self.exhausted_ids: dict[Any, float] = {}
        # 「尚未开放」的提交失败关键词：默认空 = 不启用（不得凭猜测匹配，
        # 见 config.not_open_keywords 与 core.booking.is_not_open_yet）。
        self.not_open_keywords = parse_not_open_keywords(
            getattr(settings, "not_open_keywords", "")
        )
        self._result: RunReport | None = None
        self.query_count: int = 0
        self.submit_count: int = 0
        self.seat_switches: int = 0

    # ---- 小工具 ----

    def _now(self) -> float:
        return self.deps.time_mod.time()

    def _sleep(self, seconds: float) -> None:
        self.deps.time_mod.sleep(seconds)

    def _report(
        self,
        phase: RunPhase,
        level: str,
        message: str,
        category: ErrorCategory | None = None,
    ) -> None:
        try:
            self.deps.report(phase, level, message, category)
        except Exception:
            # 事件上报失败绝不能影响预约流程本身
            if self.logger is not None:
                self.logger.warning("运行事件上报失败（已忽略）")

    def _finish(self, stop: RunStop, message: str, **kw: Any) -> RunReport:
        report = RunReport(
            stop=stop,
            status=run_status_for(stop),
            category=error_category_for(stop),
            message=message,
            exit_code=exit_code_for(stop),
            query_count=self.query_count,
            submit_count=self.submit_count,
            seat_switches=self.seat_switches,
            **kw,
        )
        self._result = report
        return report

    def _auth_stop_reason(self, exc: Exception) -> str | None:
        fn = self.deps.auth_stop_reason_fn
        if fn is None:
            return None
        return fn(exc)

    def _continue(self) -> tuple[bool, str]:
        if self.deps.should_continue is None:
            return True, ""
        return self.deps.should_continue()

    def _mark_submit_started(self) -> None:
        self.submit_count += 1
        hook = self.deps.on_submit_started
        if hook is None:
            return
        try:
            hook()
        except Exception:
            # 落盘失败不能阻止提交本身（否则会因记录问题错过预约）；
            # 但要在日志里留下痕迹。
            if self.logger is not None:
                self.logger.error("提交前落盘标记失败，结果未知时的重启判定可能不准")

    # ---- 主流程 ----

    def run(self) -> RunReport:
        self.query_count = 0
        self.submit_count = 0
        self.seat_switches = 0

        # --- 时间解析 ---
        start_ts = ts_parse(self.start)
        end_ts = ts_parse(self.end)
        if start_ts is None or end_ts is None:
            msg = f"时间格式解析失败: start={self.start!r} end={self.end!r}"
            self._report(RunPhase.START, "error", msg, ErrorCategory.CONFIG_ERROR)
            return self._finish(RunStop.CONFIG_ERROR, msg)

        trigger_ts, trigger_at, deprecated = resolve_trigger_ts(self.config, start_ts)
        if trigger_ts is None:
            msg = f"trigger_at 格式错误: {trigger_at!r}"
            self._report(RunPhase.START, "error", msg, ErrorCategory.CONFIG_ERROR)
            return self._finish(RunStop.CONFIG_ERROR, msg)
        if deprecated:
            self._report(
                RunPhase.START, "warning",
                "trigger_offset_s 已废弃，请改用 trigger_at",
            )

        # 窗口当天最早可提交的时刻（官方规则 07:00，见 core.schedule.booking_opens_at）。
        # 解析不出（时间字段异常）时为 None：此时沿用既有行为，由 resolve_submit_time 兜底。
        opens_at = booking_opens_at(self.start)
        opens_ts = opens_at.timestamp() if opens_at is not None else None

        self._report(
            RunPhase.START, "info",
            f"== 预约任务 == 目标时段: {self.start} → {self.end}  regionid={self.regionid}\n"
            f"目标座位数: {len(self.config.preferred_seats) or '全部(按前缀)'}",
        )

        # --- 放行检查（仅上报，与既有 CLI 行为一致）---
        # 真正的硬闸门在「每次提交之前」（见下方 allowed_fn 调用）；
        # 「要不要开始这次运行」的前置判断由调用方（调度器）负责。
        decision = self.deps.allowed_fn()
        self._report(RunPhase.START, "info", f"预约放行: {decision.allowed} — {decision.reason}")

        # --- 认证预检（只读，绝不提交预约）---
        # 早期预检：在可能的长时间等待之前就先暴露「凭据已失效」，避免白等一场。
        check = self.deps.auth_check_fn(self.settings, self.regionid)
        if check.is_invalid:
            self._report(
                RunPhase.AUTH_CHECK, "error",
                f"登录预检: invalid — {check.message}\n{RELOGIN_HINT}",
                ErrorCategory.AUTH_INVALID,
            )
            return self._finish(RunStop.AUTH_INVALID, check.message)
        if check.is_indeterminate:
            self._report(
                RunPhase.AUTH_CHECK, "warning",
                f"登录预检: 无法确认登录状态（{check.message}）\n"
                "  [警告] 这不是认证失效判定（网络/服务端异常同样无法确认）；将继续，"
                "但进入提交窗口前会再次校验。",
                ErrorCategory.AUTH_INDETERMINATE,
            )
        else:
            self._report(RunPhase.AUTH_CHECK, "info", f"登录预检: {check.message}")

        # --- 触发调度 ---
        self._report(
            RunPhase.WAIT, "info",
            f"触发时刻: {trigger_at}，预约时段: {self.start} → {self.end}",
        )
        if opens_ts is not None:
            self._report(
                RunPhase.WAIT, "info",
                f"开放时刻: {opens_at:%Y-%m-%d %H:%M:%S}（官方规则；在此之前只做只读查询，"
                "绝不提交）",
            )
        now_ts = self._now()
        if now_ts >= start_ts:
            msg = f"预约开始时间 {self.start} 已过，停止"
            self._report(RunPhase.WAIT, "warning", msg, ErrorCategory.TIME_INVALID)
            return self._finish(RunStop.CONFIG_ERROR, msg)
        if now_ts >= trigger_ts:
            self._report(RunPhase.WAIT, "info", "触发时刻已过，立即进入提交窗口")
        else:
            wait_fn = self.deps.wait_fn
            if wait_fn is not None:
                wait_fn(trigger_ts)

        # --- 主循环 ---
        candidates: list = []
        seat_idx: int = 0

        while True:
            keep_going, reason = self._continue()
            if not keep_going:
                return self._finish(RunStop.STOPPED, reason or "已停止")

            # 窗口已关闭
            if self._now() >= end_ts:
                if self.submit_count == 0:
                    return self._finish(RunStop.WINDOW_CLOSED, "预约窗口已关闭，全程无可预约座位")
                return self._finish(RunStop.RETRIES_EXHAUSTED, "预约窗口已关闭，仍未预约成功")

            # 刷新候选队列（首次或已耗尽）
            if seat_idx >= len(candidates):
                self._report(RunPhase.QUERY, "info", "刷新座位状态…")
                try:
                    result = self.deps.query_fn(
                        self.settings, self.regionid, self.start, self.end,
                        1, self.settings.page_size,
                    )
                except Exception as e:
                    self.query_count += 1
                    auth_reason = self._auth_stop_reason(e)
                    if auth_reason:
                        # 认证失效：立即停止，不再轮询座位，更不会提交预约
                        self._report(
                            RunPhase.QUERY, "error",
                            f"登录预检: invalid — {auth_reason}\n{RELOGIN_HINT}",
                            ErrorCategory.AUTH_INVALID,
                        )
                        return self._finish(RunStop.AUTH_INVALID, auth_reason)
                    if is_not_open_yet(str(e), self.not_open_keywords):
                        # 服务器明确说「还没开放」（识别词由操作者按**实测文案**配置，
                        # 默认空 = 不启用）：这是可恢复的暂时状态，不是故障。
                        # 处理方式与下一条完全相同（隔 30 秒重查，直到窗口关闭），
                        # 只是分类如实标成 NOT_OPEN，免得界面把它显示成网络异常。
                        self._report(
                            RunPhase.QUERY, "info",
                            f"尚未开放（服务器返回：{e}），"
                            f"{NO_CANDIDATE_DELAY_S:.0f}s 后重新查询",
                            ErrorCategory.NOT_OPEN,
                        )
                        self._sleep(NO_CANDIDATE_DELAY_S)
                        continue
                    self._report(
                        RunPhase.QUERY, "warning",
                        f"查询失败: {e}，{NO_CANDIDATE_DELAY_S:.0f}s 后重试",
                        ErrorCategory.NETWORK,
                    )
                    self._sleep(NO_CANDIDATE_DELAY_S)
                    continue
                self.query_count += 1

                candidates = pick_bookable_candidates(
                    result.seats,
                    list(self.config.preferred_seats),
                    list(self.config.preferred_prefixes),
                    list(self.config.confirmed_bookable_states),
                )
                # 排除冷却期内已确认不可用的座位
                _now = self._now()
                before = len(candidates)
                candidates = [
                    s for s in candidates
                    if _now - self.exhausted_ids.get(s.id, 0) >= COOLDOWN_S
                ]
                if len(candidates) < before:
                    self._report(
                        RunPhase.SELECT, "info",
                        f"  （{before - len(candidates)} 个座位处于冷却期，暂时排除）",
                    )
                seat_idx = 0
                self._report(RunPhase.SELECT, "info", f"候选座位: {len(candidates)} 个")
                for i, s in enumerate(candidates):
                    st = classify_seat(s, list(self.config.confirmed_bookable_states))
                    self._report(
                        RunPhase.SELECT, "info",
                        f"  #{i+1} [{st.value}] id={s.id} name={s.seat_name!r} "
                        f"{seat_state_label(s)}",
                    )
                if not candidates:
                    self._report(
                        RunPhase.SELECT, "info",
                        f"无候选座位，{NO_CANDIDATE_DELAY_S:.0f}s 后刷新",
                        ErrorCategory.NO_SEAT,
                    )
                    self._sleep(NO_CANDIDATE_DELAY_S)
                    continue

            # ---- 开放时刻之前的硬闸门：**绝不提交** ----
            # 官方规则是 07:00 开放，在此之前提交必然被服务器拒绝。提前启动
            # （scheduler_trigger_lead_s）或窗口内补跑都不允许把提交提前到开放
            # 之前；这里只是等，并强制下一轮重新查询，直到开放或窗口关闭。
            #
            # 这段等待的**唯一出路**是窗口关闭（``end_ts``）或计划被暂停/停止
            # （循环开头的 ``_continue``）：开放前的任何查询失败、任何异常响应
            # 都不会结束这次运行，所以 07:00 一到仍会照常查询并提交。
            if opens_ts is not None and self._now() < opens_ts:
                now_pre = self._now()
                # 睡到「开放」或「下一个查询间隔」中较早的那个：既能按间隔重查，
                # 也保证开放那一刻立刻醒来（不会因为睡满间隔而错过开放的第一秒）。
                wait_s = max(0.5, min(NOT_OPEN_DELAY_S, opens_ts - now_pre))
                self._report(
                    RunPhase.SUBMIT, "info",
                    f"尚未开放（现在 {datetime.fromtimestamp(now_pre):%H:%M:%S}，"
                    f"{opens_at:%H:%M:%S} 开放），{wait_s:.0f}s 后重新查询；开放前不提交",
                    ErrorCategory.NOT_OPEN,
                )
                self._sleep(wait_s)
                candidates = []   # 作废这一轮候选：下一步必须重新查询，而不是直接提交
                seat_idx = 0
                continue

            target = candidates[seat_idx]
            self._report(
                RunPhase.SUBMIT, "info",
                f"尝试首选 #{seat_idx+1}: id={target.id} name={target.seat_name!r}",
            )

            # 真实预约放行检查（每次提交前重新判断，防止运行中被暂停）
            decision = self.deps.allowed_fn()
            if not decision.allowed:
                return self._finish(RunStop.BLOCKED, decision.reason)

            # 计划层面的停止/暂停请求（Web）：不发起新的提交
            keep_going, reason = self._continue()
            if not keep_going:
                return self._finish(RunStop.STOPPED, reason or "已停止")

            # 预约开始时间必须严格晚于当前时间（自动调整 buffer_s 秒；调整超限则停止）
            submit_start, was_adjusted = resolve_submit_time(
                self.start, start_ts, end_ts, time_mod=self.deps.time_mod
            )
            if submit_start is None:
                msg = f"预约开始时间 {self.start} 无法满足服务器'大于当前时间'要求，停止"
                self._report(RunPhase.SUBMIT, "warning", msg, ErrorCategory.TIME_ADJUST_FAILED)
                return self._finish(RunStop.TIME_ADJUST_FAILED, msg)
            if was_adjusted:
                self._report(
                    RunPhase.SUBMIT, "info",
                    f"  预约开始时间调整为 {submit_start}（原 {self.start}，"
                    "需晚于服务器当前时间）",
                )

            switched = False
            not_open = False
            for attempt in range(1, self.max_attempts + 1):
                # 先把「即将提交」落盘，再发请求：崩溃/重启后据此判定结果未知
                self._mark_submit_started()
                try:
                    submit_result = self.deps.submit_fn(
                        self.settings, target, submit_start, self.end
                    )
                except Exception as e:
                    # 提交本身抛异常 = 视为 UNKNOWN → 停止（不冒险）
                    self._report(
                        RunPhase.SUBMIT, "error",
                        f"提交异常: {e}",
                        ErrorCategory.SUBMIT_EXCEPTION,
                    )
                    return self._finish(RunStop.SUBMIT_EXCEPTION, f"提交异常: {e}")

                action = decide_submit_action(submit_result, attempt, self.max_attempts)
                self._report(
                    RunPhase.SUBMIT, "info", f"  [{attempt}/{self.max_attempts}] {action.message}"
                )

                if submit_result.outcome is BookingOutcome.AUTH_FAILED:
                    # 认证失效：绝不重试提交，立即停止并给出更新凭据指引
                    self._report(
                        RunPhase.SUBMIT, "error",
                        f"{submit_result.message}\n{RELOGIN_HINT}",
                        ErrorCategory.AUTH_INVALID,
                    )
                    return self._finish(RunStop.AUTH_INVALID, submit_result.message)

                # ---- 服务器明确说「尚未开放」：可恢复的暂时状态，不是失败 ----
                # 判据是**服务器自己说的话**（关键词由操作者按实测文案配置，默认空 =
                # 不启用），而不是我们对时刻的推断：服务器说未开放，就是它认为此刻
                # 还没开放 —— 这比本地时钟更可信，也覆盖「我们以为开放了、服务器时钟
                # 还差几秒」的边界情况（那正是「07:00 一到反而整天约不到」的成因）。
                #
                # 因此这一支**不计入 max_attempts、也不结束这次运行**：退回外层只读
                # 查询、按间隔重来，直到窗口关闭（``end_ts``）为止。它绝不「重试这次
                # 提交」—— 下一次提交必须由外层重新查询之后决定（座位/参数都可能变了）。
                #
                # 结果未知（UNKNOWN）与认证失效**绝不经此分支**：前者在 ``action.stop``
                # 里立即停止，后者在上面已经停止；两者都不在这里出现。
                if (
                    submit_result.outcome is BookingOutcome.FAILED
                    and is_not_open_yet(submit_result.message, self.not_open_keywords)
                ):
                    self._report(
                        RunPhase.SUBMIT, "warning",
                        f"尚未开放（服务器返回：{submit_result.message}），"
                        f"{NOT_OPEN_DELAY_S:.0f}s 后重新查询；不算作失败重试",
                        ErrorCategory.NOT_OPEN,
                    )
                    self._sleep(NOT_OPEN_DELAY_S)
                    not_open = True
                    break

                if action.stop:
                    if action.switch_seat:
                        self.exhausted_ids[target.id] = self._now()
                        switched = True
                        self.seat_switches += 1
                        seat_idx += 1
                        break
                    # 成功 / 不可恢复错误 / 结果未知 / 次数耗尽 → 退出
                    return self._finish_from_action(
                        submit_result, attempt, action.message
                    )

                # 暂退重试
                self._sleep(action.retry_delay)

            if not_open:
                # 服务器说还没开放：强制重新查询后再决定是否再次提交，
                # 绝不拿着同一份候选直接重发（座位状态可能已经变了）。
                candidates = []
                seat_idx = 0
                continue

            if not switched:
                return self._finish(
                    RunStop.RETRIES_EXHAUSTED, f"重试次数耗尽({self.max_attempts}次)，停止"
                )

    def _finish_from_action(self, submit_result, attempt: int, message: str) -> RunReport:
        """把「停止」类提交结果映射为运行结论（结果未知绝不记为失败）。"""
        out = submit_result.outcome
        if out is BookingOutcome.SUCCESS:
            return self._finish(
                RunStop.SUCCESS, message, booking_id=submit_result.booking_id
            )
        if out is BookingOutcome.UNKNOWN:
            return self._finish(RunStop.SUBMIT_UNKNOWN, message)
        if out is BookingOutcome.NEEDS_HUMAN:
            return self._finish(RunStop.NEEDS_HUMAN, message)
        # FAILED：重试耗尽 vs 确定性错误
        if attempt >= self.max_attempts:
            return self._finish(RunStop.RETRIES_EXHAUSTED, message)
        return self._finish(RunStop.DETERMINISTIC_FAILURE, message)


def run_booking(
    settings: Any,
    config: BookingPlanConfig,
    deps: BookingDeps,
    logger: Any = None,
) -> RunReport:
    """执行一次预约运行。**不**获取文件锁，**不**访问数据库。

    调用方（``cmd_book`` 或调度器）负责：加锁、写运行记录、更新计划状态。
    """
    runner = _Runner(settings, config, deps, logger)
    try:
        return runner.run()
    except Exception as exc:  # 兜底：绝不让异常逃逸成「看起来像成功」
        if logger is not None:
            logger.exception("预约运行内部异常: %s", exc)
        try:
            deps.report(
                RunPhase.EXCEPTION, "error",
                f"运行内部异常: {exc}", ErrorCategory.INTERNAL_ERROR,
            )
        except Exception:
            pass
        return RunReport(
            stop=RunStop.INTERNAL_ERROR,
            status=RunStatus.FAILED,
            category=ErrorCategory.INTERNAL_ERROR,
            message=f"运行内部异常: {exc}",
            exit_code=1,
        )