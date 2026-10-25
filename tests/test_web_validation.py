"""计划表单的服务端校验测试（纯函数，离线）。

服务端校验是唯一可信来源：这里逐项验证「客户端能提交什么」都不影响结论。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from haut_seat.services.state import PlanStatus
from haut_seat.web.validation import (
    MAX_ATTEMPTS,
    MAX_SEATS,
    REPEAT_RULES,
    ValidationError,
    clock_of,
    compose_booking_time,
    default_run_date,
    expand_plan_form,
    next_execution_hint,
    one_off_plan_is_dead,
    parse_combos,
    parse_lines,
    parse_plan,
    plan_is_due,
    plan_mode_to_repeat_rule,
    plan_when_label,
    plan_will_start_at_once,
    repeat_rule_to_plan_mode,
)

TODAY = date(2026, 10, 16)


def _form(**overrides) -> dict:
    data = {
        "name": "工作日早八",
        "run_date": "2026-10-20",
        "repeat_rule": "once",
        "trigger_at": "07:00",
        "book_start": "2026-10-20 08:00:00",
        "book_end": "2026-10-20 12:00:00",
        "regionid": "16",
        "preferred_seats": "十层东书库北区 01桌 B座\n十层东书库北区 02桌 A座",
        "preferred_prefixes": "",
        "confirmed_bookable_states": '[["2", 1, false]]',
        "max_attempts_per_seat": "3",
        "note": "备注",
    }
    data.update(overrides)
    return data


# ------------------------------------------------------------------ 正常路径


def test_valid_form_is_normalized():
    plan, warnings = parse_plan(_form(), today=TODAY)
    assert plan.name == "工作日早八"
    assert plan.run_date == "2026-10-20"
    assert plan.trigger_at == "07:00:00"          # HH:MM 归一为 HH:MM:SS
    assert plan.book_end == "2026-10-20 12:00:00"
    assert plan.regionid == 16
    assert plan.preferred_seats == [
        "十层东书库北区 01桌 B座",
        "十层东书库北区 02桌 A座",
    ]
    assert plan.confirmed_bookable_states == [["2", 1, False]]
    assert warnings == []


def test_iso_datetime_with_T_is_accepted():
    plan, _ = parse_plan(_form(book_start="2026-10-20T08:00"), today=TODAY)
    assert plan.book_start == "2026-10-20 08:00:00"


def test_to_config_dict_matches_runner_expectations():
    plan, _ = parse_plan(_form(), today=TODAY)
    cfg = plan.to_config_dict()
    assert cfg["regionid"] == 16
    assert cfg["start"] == plan.book_start
    assert cfg["end"] == plan.book_end
    assert cfg["trigger_at"] == "07:00:00"
    assert cfg["preferred_seats"] == plan.preferred_seats
    assert cfg["confirmed_bookable_states"] == plan.confirmed_bookable_states


# ------------------------------------------------------------------ 阻断性错误


def test_empty_name_is_rejected():
    with pytest.raises(ValidationError) as exc:
        parse_plan(_form(name="   "), today=TODAY)
    assert any("名称" in e for e in exc.value.errors)


def test_overlong_name_is_rejected():
    with pytest.raises(ValidationError):
        parse_plan(_form(name="x" * 81), today=TODAY)


def test_bad_date_format_is_rejected():
    with pytest.raises(ValidationError) as exc:
        parse_plan(_form(run_date="2026/10/20"), today=TODAY)
    assert any("YYYY-MM-DD" in e for e in exc.value.errors)


def test_past_date_is_rejected():
    with pytest.raises(ValidationError) as exc:
        parse_plan(_form(run_date="2026-10-15"), today=TODAY)
    assert any("不能早于今天" in e for e in exc.value.errors)


def test_booking_window_must_fall_on_the_run_date():
    with pytest.raises(ValidationError) as exc:
        parse_plan(_form(book_start="2026-10-21 08:00:00", book_end="2026-10-21 12:00:00"),
                   today=TODAY)
    assert any("执行日期当天" in e for e in exc.value.errors)


def test_end_before_start_is_rejected():
    with pytest.raises(ValidationError):
        parse_plan(_form(book_start="2026-10-20 12:00:00", book_end="2026-10-20 08:00:00"),
                   today=TODAY)


def test_window_outside_opening_hours_is_rejected():
    """图书馆开放窗口 07:00–21:30 由既有权威校验把关，必须被复用。"""
    with pytest.raises(ValidationError):
        parse_plan(_form(book_start="2026-10-20 05:00:00", book_end="2026-10-20 06:00:00"),
                   today=TODAY)


def test_bad_trigger_is_rejected():
    with pytest.raises(ValidationError) as exc:
        parse_plan(_form(trigger_at="25:99"), today=TODAY)
    assert any("触发时刻" in e for e in exc.value.errors)


def test_bad_region_is_rejected():
    for bad in ("", "abc", "0", "-3"):
        with pytest.raises(ValidationError) as exc:
            parse_plan(_form(regionid=bad), today=TODAY)
        assert any("区域" in e for e in exc.value.errors)


def test_bad_repeat_rule_is_rejected():
    with pytest.raises(ValidationError) as exc:
        parse_plan(_form(repeat_rule="hourly"), today=TODAY)
    assert any("重复规则" in e for e in exc.value.errors)


@pytest.mark.parametrize("value", ["0", "11", "-1", "abc"])
def test_attempts_out_of_range_is_rejected(value):
    with pytest.raises(ValidationError) as exc:
        parse_plan(_form(max_attempts_per_seat=value), today=TODAY)
    assert any("尝试次数" in e for e in exc.value.errors)


def test_attempts_upper_bound_is_enforced():
    plan, _ = parse_plan(_form(max_attempts_per_seat=str(MAX_ATTEMPTS)), today=TODAY)
    assert plan.max_attempts_per_seat == MAX_ATTEMPTS


def test_all_errors_are_reported_at_once():
    """一次给出全部错误，避免用户逐项试错。"""
    with pytest.raises(ValidationError) as exc:
        parse_plan(_form(name="", regionid="x", trigger_at="zz"), today=TODAY)
    assert len(exc.value.errors) >= 3


# ------------------------------------------------------------------ 列表与组合解析


def test_parse_lines_dedupes_and_keeps_order():
    assert parse_lines("b\na\nb\n\n  \n", limit=10, max_len=50) == ["b", "a"]


def test_parse_lines_accepts_json_array():
    assert parse_lines('["甲", "乙"]', limit=10, max_len=50) == ["甲", "乙"]


def test_parse_lines_truncates_at_limit():
    raw = "\n".join(f"seat{i}" for i in range(MAX_SEATS + 5))
    assert len(parse_lines(raw, limit=MAX_SEATS, max_len=50)) == MAX_SEATS


def test_parse_lines_handles_none():
    assert parse_lines(None, limit=5, max_len=5) == []


def test_parse_combos_accepts_three_element_arrays():
    combos, errors = parse_combos('[["2", 1, false], ["1", 0, true]]')
    assert combos == [["2", 1, False], ["1", 0, True]]
    assert errors == []


@pytest.mark.parametrize("raw", ['"not json"', "[[1,2]]", "{}"])
def test_parse_combos_reports_invalid_entries(raw):
    combos, errors = parse_combos(raw)
    assert combos == []
    assert errors, f"{raw!r} 应当给出提示"


def test_parse_combos_keeps_valid_entries_and_reports_the_rest():
    """坏条目只影响自己：合法组合仍被保留，坏组合单独给出提示。"""
    combos, errors = parse_combos('[[1, 2, 3], [4]]')
    assert combos == [[1, 2, 3]]
    assert errors


def test_parse_combos_empty_is_not_an_error_but_yields_no_candidates():
    combos, errors = parse_combos("")
    assert combos == [] and errors == []


def test_invalid_combos_do_not_block_saving_but_warn():
    plan, warnings = parse_plan(_form(confirmed_bookable_states='[["2",1,false], "bad"]'),
                                today=TODAY)
    assert plan.confirmed_bookable_states == [["2", 1, False]]
    assert warnings


# ------------------------------------------------------------------ 执行日期规则


def test_plan_is_due_once():
    assert plan_is_due("2026-10-16", "once", today=TODAY)
    assert not plan_is_due("2026-10-17", "once", today=TODAY)


def test_plan_is_due_daily():
    assert plan_is_due("2026-10-16", "daily", today=TODAY)
    assert plan_is_due("2026-10-01", "daily", today=TODAY)
    assert not plan_is_due("2026-10-17", "daily", today=TODAY)


def test_plan_is_due_weekdays():
    friday = date(2026, 10, 16)
    saturday = date(2026, 10, 17)
    assert plan_is_due("2026-10-01", "weekdays", today=friday)
    assert not plan_is_due("2026-10-01", "weekdays", today=saturday)


def test_plan_is_due_rejects_garbage():
    assert not plan_is_due("not-a-date", "once", today=TODAY)
    assert not plan_is_due("2026-10-16", "hourly", today=TODAY)


def test_repeat_rules_are_the_three_supported_ones():
    assert set(REPEAT_RULES) == {"once", "daily", "weekdays"}


# ------------------------------------------------- 简化表单：模式 + 三个时刻
#
# 界面只让用户回答「多久一次 / 什么时候抢 / 座位用哪个时段 / 坐哪儿」。
# 这一组守住「简化字段 → parse_plan 认识的技术字段」的翻译：
# parse_plan 本身一行不改，因此下面每条都以 parse_plan 的结果为终点。


def _simple_form(**overrides) -> dict:
    """简化表单：只有模式与三个时刻，没有任何技术字段。"""
    data = {
        "name": "早八",
        "mode": "once",
        "run_date": "2026-10-20",
        "trigger_at": "06:55",
        "use_start": "08:00",
        "use_end": "12:00",
        "regionid": "16",
        "preferred_seats": "十层东书库北区 01桌 B座",
        "confirmed_bookable_states": '[["2", 1, false]]',
    }
    data.update(overrides)
    return data


def test_a_single_plan_is_composed_and_passes_the_technical_validation():
    plan, warnings = parse_plan(expand_plan_form(_simple_form(), today=TODAY), today=TODAY)
    assert plan.repeat_rule == "once"
    assert plan.run_date == "2026-10-20"
    assert plan.trigger_at == "06:55:00"
    assert plan.book_start == "2026-10-20 08:00:00"
    assert plan.book_end == "2026-10-20 12:00:00"
    assert warnings == []


def test_a_daily_plan_needs_no_date_field_at_all():
    """每天预约的表单里没有日期控件：执行日期取今天，时段日期随之同日。"""
    form = _simple_form(mode="daily")
    form.pop("run_date")
    plan, _ = parse_plan(expand_plan_form(form, today=TODAY), today=TODAY)
    assert plan.repeat_rule == "daily"
    assert plan.run_date == "2026-10-16"
    assert plan.book_start == "2026-10-16 08:00:00"
    assert plan.book_end == "2026-10-16 12:00:00"


@pytest.mark.parametrize(
    "flag,expected",
    [("1", "weekdays"), ("on", "weekdays"), ("true", "weekdays"),
     ("", "daily"), ("0", "daily"), ("no", "daily")],
)
def test_weekdays_only_switches_the_rule(flag, expected):
    form = _simple_form(mode="daily", weekdays_only=flag)
    form.pop("run_date")
    plan, _ = parse_plan(expand_plan_form(form, today=TODAY), today=TODAY)
    assert plan.repeat_rule == expected


def test_mode_and_repeat_rule_round_trip():
    """模式 → 规则 → 模式必须先明：编辑既有计划时靠它预选，映射不一致会串模式。"""
    for rule in REPEAT_RULES:
        mode = repeat_rule_to_plan_mode(rule)
        assert plan_mode_to_repeat_rule(mode, weekdays_only=(rule == "weekdays")) == rule
    assert plan_mode_to_repeat_rule("daily") == "daily"
    assert plan_mode_to_repeat_rule("once") == "once"


def test_the_default_date_for_a_single_booking_is_tomorrow():
    assert default_run_date(today=TODAY) == "2026-10-17"


def test_an_hh_mm_clock_is_composed_into_a_full_timestamp():
    assert compose_booking_time("2026-10-20", "08:00") == "2026-10-20 08:00:00"
    assert compose_booking_time("2026-10-20", "08:00:30") == "2026-10-20 08:00:30"
    assert compose_booking_time("2026-10-20", "八点") is None
    assert compose_booking_time("不是日期", "08:00") is None


def test_an_illegal_clock_is_reported_in_user_terms():
    with pytest.raises(ValidationError) as exc:
        expand_plan_form(_simple_form(use_start="8时"), today=TODAY)
    assert any("座位使用开始时间" in e for e in exc.value.errors)


def test_clock_of_feeds_a_time_input_from_either_stored_shape():
    assert clock_of("2026-10-20 08:00:00") == "08:00"
    assert clock_of("2026-10-20 08:00") == "08:00"
    assert clock_of("07:00:00") == "07:00"     # trigger_at 存成 HH:MM:SS
    assert clock_of("07:00") == "07:00"
    assert clock_of("八点") == ""
    assert clock_of("") == ""


def test_a_technical_form_is_passed_through_untouched():
    """旧表单（自己拼 book_start / book_end / repeat_rule）必须逐字节不变。"""
    original = _form()
    expanded = expand_plan_form(original, today=TODAY)
    for key, value in original.items():
        assert expanded[key] == value


def test_a_daily_plan_created_today_keeps_todays_date():
    """重复计划的执行日期就是今天；窗口今天过了明天照样滚动，不影响建计划。"""
    form = _simple_form(mode="daily")
    form.pop("run_date")
    data = expand_plan_form(form, today=TODAY)
    assert data["run_date"] == TODAY.strftime("%Y-%m-%d")


def test_a_daily_plan_ignores_a_leftover_date_from_the_form():
    """关掉 JS 时那个隐藏的日期控件仍会提交；模式是每天，就必须以今天为准。"""
    form = _simple_form(mode="daily", run_date="2026-10-20")
    data = expand_plan_form(form, today=TODAY)
    assert data["run_date"] == TODAY.strftime("%Y-%m-%d")
    assert data["book_start"] == f"{TODAY} 08:00:00"


# --------------------------------------------------- 「建出来就不会执行」的拦截


def test_a_single_plan_whose_window_already_ended_is_blocked():
    reason = one_off_plan_is_dead(
        run_date="2026-10-16",
        book_start="2026-10-16 08:00:00",
        book_end="2026-10-16 12:00:00",
        now=datetime(2026, 10, 16, 15, 0),
    )
    assert reason and "明天" in reason


def test_a_single_plan_for_todays_remaining_window_is_allowed():
    """今天稍晚的时段完全合法 —— 不能因为「触发时刻已过」就拦掉。"""
    assert one_off_plan_is_dead(
        run_date="2026-10-16",
        book_start="2026-10-16 08:00:00",
        book_end="2026-10-16 12:00:00",
        now=datetime(2026, 10, 16, 10, 0),
    ) is None


def test_tomorrows_single_plan_is_never_dead():
    assert one_off_plan_is_dead(
        run_date="2026-10-17",
        book_start="2026-10-17 08:00:00",
        book_end="2026-10-17 12:00:00",
        now=datetime(2026, 10, 16, 23, 30),
    ) is None


def test_unparseable_times_are_left_to_the_technical_validation():
    """不在这一层重复报格式错，免得同一件事出现两种说法。"""
    assert one_off_plan_is_dead(
        run_date="2026-10-16", book_start="x", book_end="y",
        now=datetime(2026, 10, 16, 10, 0),
    ) is None


def test_the_dead_rule_must_not_be_applied_to_repeating_plans():
    """同一条判据对重复计划会误伤：窗口每天滚动，今天过了明天还会跑。

    这里固定住「判据本身会命中」这一事实，说明调用方**必须按模式区分** ——
    路由只在 ``repeat_rule == "once"`` 时调用它。
    """
    assert one_off_plan_is_dead(
        run_date="2026-10-16",
        book_start="2026-10-16 08:00:00",
        book_end="2026-10-16 12:00:00",
        now=datetime(2026, 10, 16, 23, 0),
    ) is not None


def test_a_past_trigger_with_an_open_window_is_only_a_reminder():
    now = datetime(2026, 10, 16, 10, 0)
    hint = plan_will_start_at_once(
        run_date="2026-10-16", trigger_at="06:55",
        book_end="2026-10-16 12:00:00", now=now,
    )
    assert hint and "立刻开始执行" in hint
    # 触发时刻还没到 → 不需要提醒
    assert plan_will_start_at_once(
        run_date="2026-10-16", trigger_at="11:00",
        book_end="2026-10-16 12:00:00", now=now,
    ) is None
    # 执行日期不是今天 → 不需要提醒
    assert plan_will_start_at_once(
        run_date="2026-10-17", trigger_at="06:55",
        book_end="2026-10-17 12:00:00", now=now,
    ) is None


# ------------------------------------------------ 主页「什么时候执行」的人话
#
# 主页不把 trigger_at / book_start / book_end 三个裸时间戳摆给用户看，
# 但推算出的每一句话都必须来自调度器真正会用的判据（plan_is_due +
# effective_window），所以下面全部用固定时刻锁住确切文案。


FRIDAY = date(2026, 10, 16)


def _plan(**overrides) -> dict:
    """一条计划的样子（与 ``repo.list_plans`` 的字段同名）。"""
    data = {
        "id": 1,
        "name": "早八",
        "run_date": FRIDAY.strftime("%Y-%m-%d"),
        "repeat_rule": "daily",
        "trigger_at": "07:00:00",
        "book_start": f"{FRIDAY} 08:00:00",
        "book_end": f"{FRIDAY} 12:00:00",
        "status": PlanStatus.PENDING,
    }
    data.update(overrides)
    return data


def _at(hour: int, minute: int = 0, *, day: date = FRIDAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute)


def test_a_daily_plan_before_the_trigger_names_today():
    assert next_execution_hint(_plan(), now=_at(6)) == "今天 07:00"


def test_a_daily_plan_after_the_trigger_says_it_starts_on_the_next_check():
    """触发已到、座位使用时间还没到 → 调度器下一次检查就会启动。"""
    assert next_execution_hint(_plan(), now=_at(7, 30)) == "今天 07:00（已到点，下一次检查时开始）"


def test_a_plan_whose_seat_time_already_started_says_it_will_not_run_today():
    """座位使用时间已经开始 → 调度器**不会**启动，界面也就不许说「下一次检查时开始」。

    判据必须与 ``_maybe_start`` 的 ``now >= window_start`` 一致：07:00 触发、
    08:00 开始使用 —— 08:00 整（恰好到达）与 10:00（已过）都没有今天的机会了。
    """
    at_start = next_execution_hint(_plan(), now=_at(8))
    assert at_start == "不会再执行（今天的座位使用时间已经开始）"
    assert next_execution_hint(_plan(), now=_at(10)) == at_start
    # 单次计划同理：今天就是它唯一的机会
    assert next_execution_hint(_plan(repeat_rule="once"), now=_at(10)) == at_start


def test_a_daily_plan_after_the_window_names_tomorrow():
    """窗口每天滚动：今天过了不是「永不再执行」，而是明天同一时刻。"""
    assert next_execution_hint(_plan(), now=_at(15)) == "明天 07:00"


def test_a_daily_plan_created_days_ago_still_rolls_to_today():
    """计划里存的日期是**建计划那天**，推算必须按今天重算，不能被它带着走。"""
    stale = _plan(book_start="2026-10-01 08:00:00", book_end="2026-10-01 12:00:00")
    assert next_execution_hint(stale, now=_at(6, day=date(2026, 10, 20))) == "今天 07:00"
    assert next_execution_hint(stale, now=_at(15, day=date(2026, 10, 20))) == "明天 07:00"


def test_a_weekdays_plan_skips_the_weekend():
    plan = _plan(repeat_rule="weekdays")
    # 周六上午看：下一个工作日是周一（+2 天）
    assert next_execution_hint(plan, now=_at(9, day=date(2026, 10, 17))) == "后天 07:00"
    # 周日看仍然是周一（+1 天）
    assert next_execution_hint(plan, now=_at(9, day=date(2026, 10, 18))) == "明天 07:00"
    # 周五还没到点 → 就是今天
    assert next_execution_hint(plan, now=_at(6)) == "今天 07:00"
    # 周五已经过了窗口 → 下周一（+3 天，超出「明天/后天」的说法）
    assert next_execution_hint(plan, now=_at(15)) == "10 月 19 日 07:00"


def test_a_single_plan_names_its_own_date():
    tomorrow = date(2026, 10, 17)
    plan = _plan(
        repeat_rule="once", run_date=tomorrow.strftime("%Y-%m-%d"),
        book_start=f"{tomorrow} 08:00:00", book_end=f"{tomorrow} 12:00:00",
    )
    assert next_execution_hint(plan, now=_at(15)) == "明天 07:00"
    far = date(2026, 10, 20)
    plan["run_date"] = far.strftime("%Y-%m-%d")
    plan["book_start"] = f"{far} 08:00:00"
    plan["book_end"] = f"{far} 12:00:00"
    assert next_execution_hint(plan, now=_at(15)) == "10 月 20 日 07:00"


def test_a_single_plan_whose_window_has_passed_says_it_will_not_run():
    """这一天永远不会执行，含糊成「待执行」就是在骗人。"""
    plan = _plan(repeat_rule="once")
    assert next_execution_hint(plan, now=_at(15)) == "不会再执行（今天的时段已经过去）"


def test_a_single_plan_with_a_past_date_says_it_will_not_run():
    plan = _plan(repeat_rule="once", run_date="2026-10-01",
                 book_start="2026-10-01 08:00:00", book_end="2026-10-01 12:00:00")
    assert next_execution_hint(plan, now=_at(6)) == "不会再执行（执行日期已经过去）"


def test_unparseable_times_are_admitted_instead_of_guessed():
    broken = _plan(book_start="昨天", book_end="今天")
    assert next_execution_hint(broken, now=_at(6)) == "无法推算（时间参数不合法）"
    assert next_execution_hint(_plan(trigger_at="八点"), now=_at(6)) == "无法推算（时间参数不合法）"


def test_the_label_never_lets_a_stalled_plan_look_scheduled():
    """状态与时间必须合起来说：只有「待执行」才会被调度器拾取。"""
    now = _at(6)
    assert plan_when_label(_plan(), now=now) == "今天 07:00"
    assert plan_when_label(_plan(status=PlanStatus.PAUSED), now=now) == "已暂停；启用后 今天 07:00"
    assert plan_when_label(_plan(status="paused"), now=now) == "已暂停；启用后 今天 07:00"
    assert plan_when_label(_plan(status=PlanStatus.RUNNING), now=now) == "正在执行中"
    assert "登录已失效" in plan_when_label(_plan(status=PlanStatus.AWAITING_AUTH), now=now)
    assert plan_when_label(_plan(status=PlanStatus.SUCCEEDED), now=now) == "预约成功，不会再执行"
    assert plan_when_label(_plan(status=PlanStatus.STOPPED), now=now) == "已停止，不会再执行"
    assert "提交结果未知" in plan_when_label(_plan(status=PlanStatus.UNKNOWN), now=now)


def test_a_failed_once_plan_is_not_advertised_as_retrying():
    """单次预约失败后不会重排（_rearm_repeating 直接跳过 once），不许说成还会跑。"""
    now = _at(6)
    once = _plan(repeat_rule="once", status=PlanStatus.FAILED)
    assert plan_when_label(once, now=now) == "已失败，单次预约不会自动重试"
    daily = _plan(status=PlanStatus.FAILED)
    assert plan_when_label(daily, now=now) == "上次失败，今天 07:00"