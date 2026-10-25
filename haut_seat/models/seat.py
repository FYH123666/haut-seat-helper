"""座位与查询结果的数据模型。

重要：seatStatus / isCan / open 的语义尚未验证，本模块只做
「原样保留 + 可访问属性」，不推断其含义，更不据此判定可预约。
"""

from __future__ import annotations

import json
from typing import Any

from haut_seat.exceptions import BusinessError, ResponseShapeError
from haut_seat.sanitize import sanitize_to_text


class Seat:
    """单个座位。raw 保留接口返回的完整原始对象。"""

    def __init__(self, raw: dict[str, Any]):
        self.raw: dict[str, Any] = dict(raw)

    @property
    def id(self) -> Any:
        return self.raw.get("id")

    @property
    def seat_name(self) -> Any:
        return self.raw.get("seatName")

    @property
    def seat_status(self) -> Any:
        return self.raw.get("seatStatus")

    @property
    def is_can(self) -> Any:
        return self.raw.get("isCan")

    @property
    def open(self) -> Any:
        return self.raw.get("open")

    def state_signature(self) -> str:
        """三个状态字段的组合签名，用于与「已验证可预约」集合精确比对。

        取值类型来自 JSON 原始类型（数字/字符串/布尔），不做类型转换，
        因此比对是精确的。
        """
        return json.dumps(
            [self.seat_status, self.is_can, self.open],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def as_dict(self) -> dict[str, Any]:
        return dict(self.raw)

    def __repr__(self) -> str:
        # 仅输出座位标识与状态字段，不输出任何个人信息
        return (
            f"Seat(id={self.id!r}, seatName={self.seat_name!r}, "
            f"seatStatus={self.seat_status!r}, isCan={self.is_can!r}, "
            f"open={self.open!r})"
        )


class LayoutQueryResult:
    """layout/query 的解析结果。保留 layout 与完整原始响应。"""

    def __init__(self, layout: Any, seats: list[Seat], raw: dict[str, Any]):
        self.layout: Any = layout
        self.seats: list[Seat] = seats
        self.raw: dict[str, Any] = raw

    @classmethod
    def from_response(cls, data: Any) -> "LayoutQueryResult":
        if not isinstance(data, dict):
            raise ResponseShapeError(f"查询响应不是 JSON 对象: {type(data)!r}")
        # 业务错误识别先于结构校验：含 code 且无 seatList 视为业务错误信封
        if "code" in data and "seatList" not in data:
            raise BusinessError(
                code=data.get("code"),
                msg=data.get("msg"),
                body=sanitize_to_text(data),
            )
        layout = data.get("layout")
        seat_list = data.get("seatList")
        if seat_list is None:
            raise ResponseShapeError("查询响应缺少 seatList 字段")
        if not isinstance(seat_list, list):
            raise ResponseShapeError("seatList 字段不是数组")
        # 跳过非对象元素，避免解析异常
        seats = [Seat(item) for item in seat_list if isinstance(item, dict)]
        return cls(layout=layout, seats=seats, raw=data)

    @property
    def seat_count(self) -> int:
        return len(self.seats)

    @property
    def layout_type(self) -> str:
        """layout 字段的实际类型摘要（不输出内容，仅类型与规模）。"""
        if self.layout is None:
            return "null"
        if isinstance(self.layout, list):
            return f"list({len(self.layout)})"
        if isinstance(self.layout, dict):
            return f"dict({len(self.layout)} 键)"
        return type(self.layout).__name__

    def top_level_keys(self) -> list[str]:
        """顶层字段名（仅名称，不含值），用于诊断响应结构。"""
        if not isinstance(self.raw, dict):
            return []
        return [str(k) for k in self.raw.keys()]
