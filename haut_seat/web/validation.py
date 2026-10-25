"""计划表单的服务端校验（纯函数，可离线测试）。

**服务端校验是唯一可信来源**：前端的 required/maxlength 只是便利，
任何字段都必须在这里重新校验，绝不信任浏览器提交的内容。

时间规则复用既有权威校验 ``core.schedule.check_time_validity``，
把 ``now`` 传成计划执行日，从而在「创建未来某天的计划」时也能
校验同日、顺序、开放窗口等规则，而不会误报「只能预约当天」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Mapping

from haut_seat.core.schedule import check_time_validity, effective_trigger_at, effective_window

MAX_NAME_LEN = 80
MAX_NOTE_LEN = 500
MAX_SEATS = 50
MAX_SEAT_NAME_LEN = 120
MAX_PREFIXES = 20
MAX_COMBOS = 20
MAX_ATTEMPTS = 10
REPEAT_RULES = ("once", "daily", "weekdays")
REPEAT_RULE_LABELS = {"once": "仅执行一次", "daily": "每天", "weekdays": "工作日（周一至周五）"}

_DATETIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")
_DATE_FORMAT = "%Y-%m-%d"


class ValidationError(ValueError):
    """携带逐字段错误信息。"""

    def __init__(self, errors: list[str]):
        super().__init__("；".join(errors))
        self.errors = errors


@dataclass
class PlanInput:
    name: str
    run_date: str
    repeat_rule: str
    trigger_at: str
    book_start: str
    book_end: str
    regionid: int
    preferred_seats: list[str] = field(default_factory=list)
    preferred_prefixes: list[str] = field(default_factory=list)
    confirmed_bookable_states: list[list] = field(default_factory=list)
    max_attempts_per_seat: int = 3
    note: str = ""

    def to_config_dict(self) -> dict:
        """转换为运行器接受的配置字典。"""
        return {
            "regionid": self.regionid,
            "start": self.book_start,
            "end": self.book_end,
            "preferred_seats": list(self.preferred_seats),
            "preferred_prefixes": list(self.preferred_prefixes),
            "confirmed_bookable_states": [list(c) for c in self.confirmed_bookable_states],
            "trigger_at": self.trigger_at,
            "max_attempts_per_seat": self.max_attempts_per_seat,
        }


def parse_lines(raw: Any, *, limit: int, max_len: int) -> list[str]:
    """把多行文本 / JSON 数组解析为字符串列表（去空行、去重、保序）。"""
    items: list[str] = []
    if raw is None or raw == "":
        return items
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        text = str(raw).strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    items = [str(x) for x in parsed]
                else:
                    items = [text]
            except json.JSONDecodeError:
                items = text.splitlines()
        else:
            items = text.splitlines()
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        s = str(item).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s[:max_len])
        if len(out) >= limit:
            break
    return out


def parse_combos(raw: Any) -> tuple[list[list], list[str]]:
    """解析 confirmed_bookable_states：仅接受三元素数组，其余忽略并给出提示。"""
    errors: list[str] = []
    if raw is None or raw == "":
        return [], errors
    data = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return [], errors
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return [], ["已确认可预约状态组合必须是 JSON 数组，例如 [[\"2\",1,false]]"]
    if not isinstance(data, list):
        return [], ["已确认可预约状态组合必须是 JSON 数组"]
    out: list[list] = []
    for item in data:
        if isinstance(item, (list, tuple)) and len(item) == 3:
            out.append(list(item))
        else:
            errors.append(f"忽略非法状态组合（需恰好 3 个元素，如 [\"2\",1,false]）：{item!r}")
        if len(out) >= MAX_COMBOS:
            break
    return out, errors


def _normalize_trigger(value: Any) -> str | None:
    text = str(value or "").strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).strftime("%H:%M:%S")
        except ValueError:
            continue
    return None


def _normalize_datetime(value: Any) -> str | None:
    text = str(value or "").strip().replace("T", " ")
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def parse_plan(data: dict, *, today: date | None = None) -> tuple[PlanInput, list[str]]:
    """校验并归一化计划表单。

    返回 ``(PlanInput, 非阻断性提示)``；存在阻断性错误时抛 ``ValidationError``
    （一次给出全部错误，避免用户逐个试错）。
    """
    today = today or date.today()
    errors: list[str] = []
    warnings: list[str] = []

    name = str(data.get("name") or "").strip()
    if not name:
        errors.append("计划名称不能为空")
    elif len(name) > MAX_NAME_LEN:
        errors.append(f"计划名称不能超过 {MAX_NAME_LEN} 字符")

    raw_date = str(data.get("run_date") or "").strip()
    run_date: date | None = None
    try:
        run_date = datetime.strptime(raw_date, _DATE_FORMAT).date()
    except ValueError:
        errors.append("执行日期格式必须为 YYYY-MM-DD")
    if run_date is not None and run_date < today:
        errors.append(f"执行日期不能早于今天（{today}）")

    repeat_rule = str(data.get("repeat_rule") or "once").strip()
    if repeat_rule not in REPEAT_RULES:
        errors.append("重复规则只能是「仅一次 / 每天 / 工作日」之一")

    trigger_at = _normalize_trigger(data.get("trigger_at"))
    if trigger_at is None:
        errors.append("触发时刻格式必须为 HH:MM 或 HH:MM:SS")

    book_start = _normalize_datetime(data.get("book_start"))
    book_end = _normalize_datetime(data.get("book_end"))
    if book_start is None:
        errors.append("预约开始时间格式必须为 YYYY-MM-DD HH:MM(:SS)")
    if book_end is None:
        errors.append("预约结束时间格式必须为 YYYY-MM-DD HH:MM(:SS)")

    regionid: int | None = None
    try:
        regionid = int(str(data.get("regionid") or "").strip())
        if regionid <= 0:
            errors.append("区域 ID 必须为正整数")
            regionid = None
    except (TypeError, ValueError):
        errors.append("区域 ID 必须为正整数")

    if book_start and book_end and run_date is not None:
        for w in check_time_validity(
            book_start, book_end, now=datetime.combine(run_date, dtime.min)
        ):
            errors.append(w)
        if book_start[:10] != run_date.strftime(_DATE_FORMAT):
            errors.append("预约时段必须落在执行日期当天")

    try:
        max_attempts = int(str(data.get("max_attempts_per_seat") or "3").strip())
    except (TypeError, ValueError):
        max_attempts = 0
    if not 1 <= max_attempts <= MAX_ATTEMPTS:
        errors.append(f"每个座位最大尝试次数必须在 1–{MAX_ATTEMPTS} 之间")
        max_attempts = 3

    seats = parse_lines(data.get("preferred_seats"), limit=MAX_SEATS, max_len=MAX_SEAT_NAME_LEN)
    prefixes = parse_lines(data.get("preferred_prefixes"), limit=MAX_PREFIXES, max_len=MAX_SEAT_NAME_LEN)
    combos, combo_errors = parse_combos(data.get("confirmed_bookable_states"))
    warnings.extend(combo_errors)
    note = str(data.get("note") or "").strip()[:MAX_NOTE_LEN]

    if errors:
        raise ValidationError(errors)

    return (
        PlanInput(
            name=name,
            run_date=run_date.strftime(_DATE_FORMAT),  # type: ignore[union-attr]
            repeat_rule=repeat_rule,
            trigger_at=trigger_at,  # type: ignore[arg-type]
            book_start=book_start,  # type: ignore[arg-type]
            book_end=book_end,  # type: ignore[arg-type]
            regionid=regionid,  # type: ignore[arg-type]
            preferred_seats=seats,
            preferred_prefixes=prefixes,
            confirmed_bookable_states=combos,
            max_attempts_per_seat=max_attempts,
            note=note,
        ),
        warnings,
    )


def plan_is_due(run_date: str, repeat_rule: str, *, today: date | None = None) -> bool:
    """该计划今天是否需要执行（不含触发时刻判断）。"""
    today = today or date.today()
    try:
        base = datetime.strptime(run_date, _DATE_FORMAT).date()
    except ValueError:
        return False
    if repeat_rule == "once":
        return today == base
    if repeat_rule == "daily":
        return today >= base
    if repeat_rule == "weekdays":
        return today >= base and today.weekday() < 5
    return False


# --------------------------------------------------- 简化表单：模式 + 时刻（纯函数）
#
# 界面只让用户回答四件事：多久一次 / 什么时候开始抢 / 座位用哪个时段 / 坐哪儿。
# 下面这些函数负责把这份「简化表单」翻译成 ``parse_plan`` 本来就认识的技术字段，
# **``parse_plan`` 的签名与规则一行不改** —— 服务端校验仍然只有一个权威入口。

PLAN_MODES = ("daily", "once")
PLAN_MODE_LABELS = {"daily": "每天预约", "once": "单次预约"}
_TRUE_VALUES = frozenset({"1", "true", "on", "yes", "是"})


def _is_true(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUE_VALUES


def plan_mode_to_repeat_rule(mode: str, *, weekdays_only: bool = False) -> str:
    """界面上的「模式」→ 数据库的 ``repeat_rule``。

    「每天预约」→ ``daily``；勾上「仅工作日」→ ``weekdays``。
    ``weekdays`` 不是第三种模式，而是「每天」的一个变体：``plan_is_due``
    对它的判断是「今天不早于起始日且是周一到周五」。
    """
    if mode == "once":
        return "once"
    return "weekdays" if weekdays_only else "daily"


def repeat_rule_to_plan_mode(repeat_rule: str) -> str:
    """``repeat_rule`` → 界面模式（编辑既有计划时用来预选，反向映射必须一致）。"""
    return "once" if repeat_rule == "once" else "daily"


def default_run_date(*, today: date) -> str:
    """新建单次预约的默认执行日期：**明天**。

    为什么不是今天：官方规则「读者只可预约当天座位」，而 ``run_date`` 同时是
    执行日和座位使用日。所以「约明天的座」= 明天由程序发起。今天作为默认值，
    在触发时刻已过时会做出一个永远不执行的计划；明天是唯一总能立刻生效的默认值。
    """
    return (today + timedelta(days=1)).strftime(_DATE_FORMAT)


def clock_of(value: str) -> str:
    """取 ``HH:MM``：``YYYY-MM-DD HH:MM(:SS)`` 与裸 ``HH:MM(:SS)`` 都接受。

    用途是预填 ``<input type="time">``：它只认 ``HH:MM``（默认 step=60 时
    带秒的 ``07:00:00`` 会被浏览器判为非法值，控件显示空白），因此
    ``trigger_at`` 这种存成 ``HH:MM:SS`` 的值也必须先削成 ``HH:MM``。
    认不出的形状返回空串，让用户重填，而不是猜一个时刻出来。
    """
    text = (value or "").strip()
    if len(text) >= 16 and text[10] == " ":
        text = text[11:]
    parts = text.split(":")
    if len(parts) < 2:
        return ""
    hh, mm = parts[0].strip(), parts[1].strip()
    if len(hh) == 2 and len(mm) == 2 and hh.isdigit() and mm.isdigit():
        return f"{hh}:{mm}"
    return ""


def compose_booking_time(day: str, clock: str) -> str | None:
    """把「日期 + HH:MM(:SS)」拼成 ``YYYY-MM-DD HH:MM:SS``；解析不了返回 ``None``。

    界面上时段只填时刻，日期只填一次：两处输入同一个事实是纯冗余，而
    ``parse_plan`` 本来就强制「时段日期 == 执行日期」（同日规则），
    所以拼接不会放松任何校验。
    """
    clock = (clock or "").strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            parsed = datetime.strptime(clock, fmt).time()
        except ValueError:
            continue
        try:
            base = datetime.strptime((day or "").strip(), _DATE_FORMAT).date()
        except ValueError:
            return None
        return datetime.combine(base, parsed).strftime("%Y-%m-%d %H:%M:%S")
    return None


def one_off_plan_is_dead(
    *, run_date: str, book_start: str, book_end: str, now: datetime
) -> str | None:
    """单次计划是否**建出来就不可能执行**：返回原因，正常返回 ``None``。

    唯一查明的必死情形：执行日就是今天，而当天时段已经整体过去
    （``now >= book_end``，正是 ``plan_window_state`` 判 ``expired`` 的同一个条件）。
    此后调度器每次 tick 都会判「窗口已过」直接返回，计划永远停在
    「待执行」+「已过预约窗口」两句互相矛盾的状态里。

    **触发时刻已过不算死**：窗口还没关时调度器下一 tick 就开始执行，
    这是「今天再约一次」的正常用法，拦掉它是错的。
    """
    try:
        day = datetime.strptime((run_date or "").strip(), _DATE_FORMAT).date()
    except ValueError:
        return None  # 日期格式错误由 parse_plan 报，不在这里重复
    if day != now.date():
        return None
    end = _parse_datetime(book_end)
    if end is None:
        return None
    if now >= end:
        return (
            f"当天预约时段（到 {clock_of(book_end)}）已经过去，这个计划建出来就不会执行。"
            f"请把日期改为明天，或把座位使用时段改到晚一点。"
        )
    return None


def plan_will_start_at_once(
    *, run_date: str, trigger_at: str, book_end: str, now: datetime
) -> str | None:
    """提醒用的非阻断提示：今天的触发时刻已过但窗口未关，启用后会立刻开始。

    只是提醒 —— 这是合法用法，不阻断。返回 ``None`` 表示不需要提醒。
    """
    try:
        day = datetime.strptime((run_date or "").strip(), _DATE_FORMAT).date()
    except ValueError:
        return None
    if day != now.date():
        return None
    trigger = _parse_clock(trigger_at, now.date())
    end = _parse_datetime(book_end)
    if trigger is None or end is None:
        return None
    if now >= trigger and now < end:
        return (
            f"今天的触发时刻 {clock_of(trigger_at)} 已过，但时段还没结束："
            "启用后调度器会在下一次检查时立刻开始执行。"
        )
    return None


def _parse_datetime(value: str) -> datetime | None:
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime((value or "").strip(), fmt)
        except ValueError:
            continue
    return None


def _parse_clock(value: str, day: date) -> datetime | None:
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.combine(day, datetime.strptime((value or "").strip(), fmt).time())
        except ValueError:
            continue
    return None


def expand_plan_form(form: Mapping[str, Any], *, today: date) -> dict:
    """把「简化表单」补全成 ``parse_plan`` 认识的技术字段。

    **兼容规则（重要）**：已给出的技术字段一律原样保留，只在缺失时才由简化字段
    推导。旧表单（直接提交 ``book_start`` / ``book_end`` / ``repeat_rule``）因此
    逐字节保持原行为 —— 既有测试与「自己拼参数」的用法都是靠这一条守住。

    简化字段：``mode``（daily|once）、``weekdays_only``、``run_date``（仅单次预约）、
    ``trigger_at``（``HH:MM``，与 ``parse_plan`` 同名）、``use_start`` / ``use_end``
    （``HH:MM``，与 ``run_date`` 拼成完整时间戳）。

    时刻填写非法时抛 ``ValidationError``，给出面向用户的说法，
    而不是让 ``parse_plan`` 回一句「期望 YYYY-MM-DD HH:MM:SS」。
    """
    out = dict(form)
    mode = str(out.get("mode") or "").strip()
    if mode in PLAN_MODES:
        out["repeat_rule"] = plan_mode_to_repeat_rule(
            mode, weekdays_only=_is_true(out.get("weekdays_only"))
        )

    if mode == "daily":
        # 每天预约没有日期字段：执行日期**一律**取今天，不采信表单里的日期。
        # 页面上那个日期控件只是被隐藏/禁用；关掉 JS 时它仍会跟着提交，
        # 采信它就会做出「模式说每天、执行日期却是明天」的半截状态。
        out["run_date"] = today.strftime(_DATE_FORMAT)

    run_date = str(out.get("run_date") or "").strip()
    if not run_date:
        # 单次预约没填日期（表单默认已给明天）；重复计划上面已经赋过值。
        run_date = today.strftime(_DATE_FORMAT)
        out["run_date"] = run_date

    errors: list[str] = []
    for field_name, clock_key, label in (
        ("book_start", "use_start", "座位使用开始时间"),
        ("book_end", "use_end", "座位使用结束时间"),
    ):
        if str(out.get(field_name) or "").strip():
            continue  # 技术字段已经给了，不覆盖
        clock = str(out.get(clock_key) or "").strip()
        if not clock:
            continue  # 缺失由 parse_plan 报格式错误
        composed = compose_booking_time(run_date, clock)
        if composed is None:
            errors.append(f"{label}必须是 HH:MM 或 HH:MM:SS")
        else:
            out[field_name] = composed
    if errors:
        raise ValidationError(errors)
    return out


# ------------------------------------------------ 主页「什么时候执行」的人话
#
# 主页不能把 trigger_at / book_start / book_end 三个裸时间戳摆给用户看，
# 但也不能自己发明一套说法：这里推算出的每一句话，用的都是调度器**真正**
# 会用的那几个判据（plan_is_due + effective_window），所以它描述的是
# 「程序接下来会怎么做」，而不是「图书馆大概什么时候开门」。


def next_execution_hint(plan: Mapping[str, Any], *, now: datetime) -> str:
    """下一次执行时刻的人话；完全不看计划状态（那只由 :func:`plan_when_label` 合起来讲）。

    只依据已确认的规则推算，因此有几种情形必须如实说「不会再执行」：
    今天的座位使用时间已经开始（调度器不会启动）、单次计划的时段已经过去、
    单次计划的执行日期已经过去、以及时间参数根本无法解析。
    绝不为了好看而含糊成「待执行」。
    """
    rule = str(plan.get("repeat_rule") or "")
    today = now.date()
    run_date = str(plan.get("run_date") or "")
    trigger_at = str(plan.get("trigger_at") or "")
    window_start, window_end = effective_window(
        str(plan.get("book_start") or ""), str(plan.get("book_end") or ""),
        repeat_rule=rule, today=today,
    )
    start = _parse_datetime(window_start)
    end = _parse_datetime(window_end)
    trigger_clock = clock_of(trigger_at)
    if start is None or end is None or not trigger_clock:
        return "无法推算（时间参数不合法）"
    # 触发时刻与窗口同一天：日期一直是向窗口开始日借的（与调度器同一个函数）。
    trigger_day = _parse_clock(trigger_at, start.date())
    due_today = plan_is_due(run_date, rule, today=today)
    if due_today and now < end:
        if now >= start:
            # 座位使用时间已经开始：调度器**不会**再启动（那样只会产生一条必然
            # CONFIG_ERROR 的运行），所以这里也绝不能说「下一次检查时开始」。
            return "不会再执行（今天的座位使用时间已经开始）"
        if trigger_day is not None and now >= trigger_day:
            return f"今天 {trigger_clock}（已到点，下一次检查时开始）"
        return f"今天 {trigger_clock}"
    if rule == "once":
        day = _date_of(run_date)
        if day is None:
            return "无法推算（执行日期不合法）"
        if due_today:
            return "不会再执行（今天的时段已经过去）"
        if day < today:
            return "不会再执行（执行日期已经过去）"
        return f"{_day_label(day, today=today)} {trigger_clock}"
    # 重复计划：窗口每天滚动，所以下一个「符合规则的日子」一定能执行。
    for offset in range(1, 9):  # 工作日规则最多跳过两天，8 天足够
        day = today + timedelta(days=offset)
        if plan_is_due(run_date, rule, today=day):
            return f"{_day_label(day, today=today)} {trigger_clock}"
    return f"{trigger_clock}（按规则的下一个执行日）"


def plan_when_label(plan: Mapping[str, Any], *, now: datetime) -> str:
    """主页「下次执行」一列：把**调度规则**与**当前状态**合成一句不矛盾的话。

    只有「待执行」的计划才会自动执行（``is_schedulable`` 只认 ``PENDING``），
    所以已暂停 / 已成功 / 结果未知的计划必须说清「时间点归时间点，程序现在不会动」，
    否则就是把 ``plan_window_state`` 那两句自相矛盾的话换了个位置继续骗人。
    """
    status = str(getattr(plan.get("status"), "value", plan.get("status")) or "")
    timing = next_execution_hint(plan, now=now)
    if status == "pending":
        return timing
    if status == "running":
        return "正在执行中"
    if status == "awaiting_auth":
        # 多用户之后这句话不能说成"所有计划"：一条计划进入等待认证，只因**它的归属
        # 用户**（或管理端账号本身）的凭据失效，别人的计划不受影响。
        return "登录已失效，这条计划已暂停提交（更新凭据后再启用）"
    if status == "paused":
        return f"已暂停；启用后 {timing}"
    if status == "succeeded":
        return "预约成功，不会再执行"
    if status == "stopped":
        return "已停止，不会再执行"
    if status == "unknown":
        return "提交结果未知，已停止自动执行（先核实，再在详情页恢复）"
    if status == "failed":
        if str(plan.get("repeat_rule") or "") == "once":
            return "已失败，单次预约不会自动重试"
        return f"上次失败，{timing}"
    return timing


def _date_of(value: str) -> date | None:
    try:
        return datetime.strptime((value or "").strip(), _DATE_FORMAT).date()
    except ValueError:
        return None


def _day_label(day: date, *, today: date) -> str:
    delta = (day - today).days
    if delta == 0:
        return "今天"
    if delta == 1:
        return "明天"
    if delta == 2:
        return "后天"
    return f"{day.month} 月 {day.day} 日"