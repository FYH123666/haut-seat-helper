"""优先级与状态分类测试：未知状态不自动认定可预约。"""

from __future__ import annotations

from haut_seat.core.priority import (
    SeatState,
    classify_seat,
    explain_seat_inclusion,
    filter_by_exact_names,
    filter_by_name_prefixes,
    filter_confirmed,
    is_confirmed_bookable,
    pick_bookable_candidates,
    pick_candidates,
    seat_state_label,
    sort_by_priority,
    state_distribution,
)
from haut_seat.models.seat import Seat


def _seat(name, status=0, is_can=0, open=0):
    return Seat(
        {"id": 1, "seatName": name, "seatStatus": status, "isCan": is_can, "open": open}
    )


def test_classify_unknown_by_default():
    s = _seat("A-001", "X", "Y", "Z")
    assert classify_seat(s, None) is SeatState.UNKNOWN
    assert classify_seat(s, []) is SeatState.UNKNOWN
    assert is_confirmed_bookable(s, None) is False


def test_classify_confirmed():
    s = _seat("A-001", 1, 1, 1)
    assert classify_seat(s, [[1, 1, 1]]) is SeatState.CONFIRMED_BOOKABLE


def test_classify_signature_type_matters():
    s = _seat("A-001", 1, 1, 1)
    # 字符串 "1" 不等于数字 1，不能命中
    assert classify_seat(s, [["1", "1", "1"]]) is SeatState.UNKNOWN


def test_filter_exact_names():
    seats = [_seat("A-001"), _seat("A-002"), _seat("B-001")]
    out = filter_by_exact_names(seats, ["A-001"])
    assert [s.seat_name for s in out] == ["A-001"]


def test_filter_prefixes():
    seats = [_seat("A-001"), _seat("A-002"), _seat("B-001")]
    out = filter_by_name_prefixes(seats, ["A-"])
    assert [s.seat_name for s in out] == ["A-001", "A-002"]


def test_filter_empty_returns_all():
    seats = [_seat("A-001"), _seat("B-001")]
    assert len(filter_by_exact_names(seats, None)) == 2
    assert len(filter_by_name_prefixes(seats, [])) == 2


def test_sort_priority_order():
    seats = [_seat("C-001"), _seat("A-002"), _seat("A-001")]
    ordered = sort_by_priority(
        seats, preferred_names=["A-001"], preferred_prefixes=["A-"]
    )
    names = [s.seat_name for s in ordered]
    assert names == ["A-001", "A-002", "C-001"]


def test_sort_stable_by_name_without_preference():
    seats = [_seat("B-001"), _seat("A-001")]
    ordered = sort_by_priority(seats)
    assert [s.seat_name for s in ordered] == ["A-001", "B-001"]


def test_classify_ignores_non_list_entries():
    s = _seat("A-001", 1, 1, 1)
    # 说明性字符串 / 错误配置不应被当作有效组合，也不应导致误判
    assert classify_seat(s, ["说明", "not-a-combo", 42]) is SeatState.UNKNOWN


def test_classify_mixed_entries_still_matches_valid_combo():
    s = _seat("A-001", 1, 1, 1)
    assert (
        classify_seat(s, ["说明", [1, 1, 1], "noise"])
        is SeatState.CONFIRMED_BOOKABLE
    )


def test_classify_wrong_length_ignored():
    s = _seat("A-001", 1, 1, 1)
    assert classify_seat(s, [[1, 1]]) is SeatState.UNKNOWN


def test_explain_inclusion_default():
    ok, reason = explain_seat_inclusion(_seat("A-001"), [], [])
    assert ok is True
    assert "默认" in reason


def test_explain_inclusion_exact_name():
    ok, reason = explain_seat_inclusion(_seat("A-001"), ["A-001"], [])
    assert ok is True
    assert "精确" in reason


def test_explain_exclusion_exact_name():
    ok, reason = explain_seat_inclusion(_seat("B-001"), ["A-001"], [])
    assert ok is False
    assert "精确" in reason


def test_explain_inclusion_prefix():
    ok, reason = explain_seat_inclusion(_seat("五层东书库南区 01桌 A座"), [], ["五层东书库南区"])
    assert ok is True
    assert "前缀" in reason


def test_explain_exclusion_prefix():
    ok, reason = explain_seat_inclusion(_seat("四层自习区 01桌 A座"), [], ["五层东书库南区"])
    assert ok is False
    assert "前缀" in reason


def test_seat_state_label():
    s = _seat("A-001", "1", 0, False)
    label = seat_state_label(s)
    assert "seatStatus='1'" in label
    assert "isCan=0" in label
    assert "open=False" in label


def test_state_distribution():
    seats = [_seat("A", "1", 0, False), _seat("B", "1", 0, False), _seat("C", "2", 1, False)]
    d = state_distribution(seats)
    assert len(d) == 2
    assert list(d.values())[0] == 2


def test_sort_respects_preferred_order():
    seats = [_seat("B-001"), _seat("A-001"), _seat("A-002")]
    ordered = sort_by_priority(seats, preferred_names=["A-002", "A-001"])
    assert [s.seat_name for s in ordered] == ["A-002", "A-001", "B-001"]


def test_pick_candidates_prefers_exact_then_prefix():
    seats = [_seat("A-001"), _seat("A-002"), _seat("B-001")]
    out = pick_candidates(seats, ["A-002"], ["A-"])
    assert [s.seat_name for s in out] == ["A-002", "A-001"]


def test_pick_candidates_no_preference_returns_all():
    seats = [_seat("B-001"), _seat("A-001")]
    out = pick_candidates(seats)
    assert [s.seat_name for s in out] == ["A-001", "B-001"]


def test_filter_confirmed_passes_only_bookable():
    s1 = _seat("B座", "2", 1, False)   # 已确认可预约
    s2 = _seat("A座", "1", 0, False)   # 未知/不可预约
    out = filter_confirmed([s1, s2], [["2", 1, False]])
    assert len(out) == 1
    assert out[0].seat_name == "B座"


def test_filter_confirmed_empty_config_all_unknown():
    s = _seat("X", "2", 1, False)
    out = filter_confirmed([s], [])
    assert out == []


def test_filter_confirmed_no_config_all_unknown():
    s = _seat("X", "2", 1, False)
    out = filter_confirmed([s], None)
    assert out == []


def test_pick_bookable_candidates_combines_name_and_state():
    s1 = _seat("X", "2", 1, False)  # bookable
    s2 = _seat("Y", "1", 0, False)  # not bookable
    out = pick_bookable_candidates([s1, s2], None, None, [["2", 1, False]])
    assert len(out) == 1
    assert out[0].seat_name == "X"


def test_pick_bookable_candidates_preferred_order():
    s1 = _seat("A-002", "2", 1, False)
    s2 = _seat("A-001", "2", 1, False)
    out = pick_bookable_candidates([s2, s1], ["A-001"], ["A-"], [["2", 1, False]])
    assert [s.seat_name for s in out] == ["A-001", "A-002"]


def test_filter_confirmed_missing_field_not_bookable():
    s = Seat({"id": 1, "seatName": "X", "isCan": 1, "open": False})  # 缺 seatStatus
    assert filter_confirmed([s], [["2", 1, False]]) == []


def test_filter_confirmed_all_non_bookable_returns_empty():
    seats = [_seat("A", "1", 0, False), _seat("B", "1", 0, False)]
    out = filter_confirmed(seats, [["2", 1, False]])
    assert out == []


def test_filter_confirmed_empty_list_returns_empty():
    assert filter_confirmed([], [["2", 1, False]]) == []


def test_pick_bookable_candidates_empty_confirmed_states():
    s = _seat("X", "2", 1, False)
    out = pick_bookable_candidates([s], None, None, [])
    assert out == []  # 未配置状态 → 全 unknown → 无候选


def test_pick_bookable_candidates_all_unknown():
    seats = [_seat("A", "1", 0, False), _seat("B", "2", 1, True)]
    out = pick_bookable_candidates(seats, None, None, [["2", 1, False]])
    assert out == []  # 全部不匹配已确认组合
