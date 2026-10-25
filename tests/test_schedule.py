"""预约时间合法性检查测试。"""

from __future__ import annotations

from datetime import date, datetime

from haut_seat.core.schedule import (
    check_time_validity,
    effective_trigger_at,
    effective_window,
)

NOW = datetime(2026, 10, 16, 13, 0, 0)


def test_valid_same_day_future():
    assert check_time_validity("2026-10-16 14:00:00", "2026-10-16 16:00:00", NOW) == []


def test_start_after_end():
    w = check_time_validity("2026-10-16 16:00:00", "2026-10-16 14:00:00", NOW)
    assert any("早于" in x for x in w)


def test_cross_day():
    w = check_time_validity("2026-10-16 14:00:00", "2026-10-17 16:00:00", NOW)
    assert any("同一天" in x for x in w)


def test_not_today():
    w = check_time_validity("2026-10-17 14:00:00", "2026-10-17 16:00:00", NOW)
    assert any("当天" in x for x in w)


def test_past_start():
    w = check_time_validity("2026-10-16 10:00:00", "2026-10-16 12:00:00", NOW)
    assert any("当前时间" in x for x in w)


def test_end_after_close():
    w = check_time_validity("2026-10-16 20:00:00", "2026-10-16 22:00:00", NOW)
    assert any("21:30" in x for x in w)


def test_unparseable():
    w = check_time_validity("not-a-time", "also-bad", NOW)
    assert any("无法解析" in x for x in w)


# ------------------------------------------------------- 重复计划的执行窗口滚动
#
# 这一组守护的是一个真实缺陷：``_rearm_repeating`` 只把状态改回「待执行」，
# 从不改写 ``book_start`` / ``book_end``。若窗口日期不滚动，重复计划在次日
# 一律被判成「窗口已过」，永远不再执行 —— 与界面上的「待执行」互相矛盾。


def test_effective_window_leaves_a_once_plan_untouched():
    """单次计划的窗口日期本就等于执行日期，必须逐字节原样返回。"""
    start = "2026-10-16 08:00:00"
    end = "2026-10-16 12:00:00"
    assert effective_window(start, end, repeat_rule="once", today=date(2026, 10, 20)) == (
        start,
        end,
    )


def test_effective_window_rolls_a_daily_plan_onto_today():
    assert effective_window(
        "2026-10-16 08:00:00",
        "2026-10-16 12:00:00",
        repeat_rule="daily",
        today=date(2026, 10, 20),
    ) == ("2026-10-20 08:00:00", "2026-10-20 12:00:00")


def test_effective_window_rolls_weekdays_too():
    """工作日规则不是 ``once``，同属重复计划，窗口照样要滚动。"""
    assert effective_window(
        "2026-10-16 08:00:00",
        "2026-10-16 12:00:00",
        repeat_rule="weekdays",
        today=date(2026, 10, 21),
    ) == ("2026-10-21 08:00:00", "2026-10-21 12:00:00")


def test_effective_window_keeps_the_time_of_day():
    """只换日期；时刻原样保留（含省略秒的写法）。"""
    assert effective_window(
        "2026-10-16 07:00",
        "2026-10-16 21:30",
        repeat_rule="daily",
        today=date(2026, 10, 20),
    ) == ("2026-10-20 07:00:00", "2026-10-20 21:30:00")


def test_effective_window_passes_through_unparseable_values():
    """解析不了就原样交回，由调用方按「时间参数不合法」处理（界面已有该提示）。"""
    assert effective_window(
        "不是时间", "2026-10-16 12:00:00", repeat_rule="daily", today=date(2026, 10, 20)
    ) == ("不是时间", "2026-10-16 12:00:00")


def test_effective_window_uses_the_given_day_verbatim():
    """窗口日期一律取 ``today``，不做「只许向前」之类的额外假设。"""
    assert effective_window(
        "2026-10-16 08:00:00",
        "2026-10-16 12:00:00",
        repeat_rule="daily",
        today=date(2026, 10, 15),
    ) == ("2026-10-15 08:00:00", "2026-10-15 12:00:00")


def test_effective_trigger_at_borrows_the_window_day():
    """触发时刻只有时刻，日期向**滚动之后**的窗口借 —— 否则会半截停在前一天。"""
    start, _ = effective_window(
        "2026-10-16 08:00:00",
        "2026-10-16 12:00:00",
        repeat_rule="daily",
        today=date(2026, 10, 20),
    )
    assert effective_trigger_at(start, "07:00") == "2026-10-20 07:00"


def test_effective_trigger_at_accepts_seconds_and_keeps_the_same_day():
    start, _ = effective_window(
        "2026-10-16 08:00:00",
        "2026-10-16 12:00:00",
        repeat_rule="daily",
        today=date(2026, 10, 20),
    )
    assert effective_trigger_at(start, "06:55:30") == "2026-10-20 06:55:30"
