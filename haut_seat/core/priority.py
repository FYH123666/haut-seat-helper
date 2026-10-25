"""座位过滤与优先级排序（纯函数，无 IO、无网络）。

原则：在 seatStatus/isCan/open 语义未经验证前，任何座位都不会被
「自动认定为可预约」。只有命中配置中明确给出的 confirmed_bookable
组合签名时，才标记为 CONFIRMED_BOOKABLE。
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Iterable

from haut_seat.models.seat import Seat


class SeatState(str, Enum):
    UNKNOWN = "unknown"                     # 状态语义未验证，不可自动认定
    CONFIRMED_BOOKABLE = "confirmed_bookable"  # 已人工验证为可预约


def filter_by_exact_names(seats: Iterable[Seat], names: list | None) -> list[Seat]:
    """按座位名称精确匹配过滤。names 为空/None 时返回全部。"""
    names = [str(n) for n in (names or []) if n]
    if not names:
        return list(seats)
    name_set = set(names)
    return [s for s in seats if s.seat_name is not None and str(s.seat_name) in name_set]


def filter_by_name_prefixes(seats: Iterable[Seat], prefixes: list | None) -> list[Seat]:
    """按座位名称前缀过滤。prefixes 为空/None 时返回全部。"""
    prefixes = [str(p) for p in (prefixes or []) if p]
    if not prefixes:
        return list(seats)
    out = []
    for s in seats:
        if s.seat_name is None:
            continue
        if any(str(s.seat_name).startswith(p) for p in prefixes):
            out.append(s)
    return out


def _normalize_signature(value: Any) -> str | None:
    """把配置中的「已验证可预约」组合归一为签名；非三元素序列返回 None。"""
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    return json.dumps(list(value), ensure_ascii=False, separators=(",", ":"))


def classify_seat(seat: Seat, confirmed_bookable: list | None) -> SeatState:
    """依据「已验证可预约」集合分类座位。默认未知，绝不猜测。

    仅接受三元素序列（[seatStatus, isCan, open]）作为有效组合，
    其它条目（如说明性字符串、错误配置）一律忽略，避免误判。
    """
    if confirmed_bookable:
        seat_sig = seat.state_signature()
        for combo in confirmed_bookable:
            sig = _normalize_signature(combo)
            if sig is not None and seat_sig == sig:
                return SeatState.CONFIRMED_BOOKABLE
    return SeatState.UNKNOWN


def is_confirmed_bookable(seat: Seat, confirmed_bookable: list | None) -> bool:
    return classify_seat(seat, confirmed_bookable) is SeatState.CONFIRMED_BOOKABLE


def sort_by_priority(
    seats: Iterable[Seat],
    preferred_names: list | None = None,
    preferred_prefixes: list | None = None,
) -> list[Seat]:
    """按优先级排序：精确名（按列表顺序）> 前缀 > 其它；同级按名称稳定排序。"""
    pref_names = [str(n) for n in (preferred_names or []) if n]
    name_rank = {n: i for i, n in enumerate(pref_names)}
    pref_prefixes = [str(p) for p in (preferred_prefixes or []) if p]

    def key(seat: Seat) -> tuple:
        name = str(seat.seat_name) if seat.seat_name is not None else ""
        if name in name_rank:
            return (0, name_rank[name], name)
        if any(name.startswith(p) for p in pref_prefixes):
            return (1, 0, name)
        return (2, 0, name)

    return sorted(seats, key=key)


def pick_candidates(
    seats: Iterable[Seat],
    preferred_names: list | None = None,
    preferred_prefixes: list | None = None,
) -> list[Seat]:
    """返回按优先级排序的候选座位：仅保留命中精确名或前缀的座位。

    未配置任何偏好时返回全部（按名称排序）。首元素即首选目标，其余为备选。
    """
    names = {str(n) for n in (preferred_names or []) if n}
    prefixes = [str(p) for p in (preferred_prefixes or []) if p]
    if not names and not prefixes:
        return sort_by_priority(seats, preferred_names, preferred_prefixes)
    matched = []
    for s in seats:
        name = str(s.seat_name) if s.seat_name is not None else ""
        if (names and name in names) or (prefixes and any(name.startswith(p) for p in prefixes)):
            matched.append(s)
    return sort_by_priority(matched, preferred_names, preferred_prefixes)


def filter_seats(
    seats: Iterable[Seat],
    preferred_names: list | None = None,
    preferred_prefixes: list | None = None,
) -> tuple[list[Seat], str]:
    """按名称过滤，返回 (结果, 采用的过滤说明)。"""
    if preferred_names:
        out = filter_by_exact_names(seats, preferred_names)
        return out, f"精确名称匹配 {len(preferred_names)} 个"
    if preferred_prefixes:
        out = filter_by_name_prefixes(seats, preferred_prefixes)
        return out, f"名称前缀匹配 {len(preferred_prefixes)} 个"
    return list(seats), "未配置过滤，取全部"


def explain_seat_inclusion(
    seat: Seat,
    preferred_names: list | None = None,
    preferred_prefixes: list | None = None,
) -> tuple[bool, str]:
    """返回 (是否入选候选, 原因)，与 filter_seats 采用相同的过滤规则。"""
    name = str(seat.seat_name) if seat.seat_name is not None else ""
    if preferred_names:
        names = {str(n) for n in preferred_names if n}
        if names and name in names:
            return True, "命中精确名称"
        return False, "未命中精确名称"
    if preferred_prefixes:
        prefixes = [str(p) for p in preferred_prefixes if p]
        if prefixes and any(name.startswith(p) for p in prefixes):
            return True, "命中名称前缀"
        return False, "未命中名称前缀"
    return True, "未配置过滤，默认入选"


def seat_state_label(seat: Seat) -> str:
    """座位的三字段原始取值摘要（仅展示，不推断语义）。"""
    return (
        f"seatStatus={seat.seat_status!r}, isCan={seat.is_can!r}, open={seat.open!r}"
    )


def state_distribution(seats: Iterable[Seat]) -> dict[str, int]:
    """按 (seatStatus, isCan, open) 组合统计数量，按数量降序返回。"""
    counts: dict[str, int] = {}
    for s in seats:
        label = seat_state_label(s)
        counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def filter_confirmed(
    seats: Iterable[Seat], confirmed_bookable: list | None
) -> list[Seat]:
    """仅保留已确认可预约的座位。未配置时全部为 unknown → 返回空列表。"""
    return [s for s in seats if classify_seat(s, confirmed_bookable) is SeatState.CONFIRMED_BOOKABLE]


def pick_bookable_candidates(
    seats: Iterable[Seat],
    preferred_names: list | None = None,
    preferred_prefixes: list | None = None,
    confirmed_bookable: list | None = None,
) -> list[Seat]:
    """按优先级返回「名称匹配 且 已确认可预约」的候选座位。

    双重把关：名称筛选 + 状态筛选。confirmed_bookable 为空时无候选。
    """
    name_matched = pick_candidates(seats, preferred_names, preferred_prefixes)
    return filter_confirmed(name_matched, confirmed_bookable)
