"""座位解析测试：覆盖样例、字段缺失、空列表、异常响应。"""

from __future__ import annotations

import pytest

from haut_seat.exceptions import BusinessError, ResponseShapeError
from haut_seat.models.seat import LayoutQueryResult, Seat


def test_parse_sample(sample_layout_raw):
    r = LayoutQueryResult.from_response(sample_layout_raw)
    assert r.seat_count == 3
    assert isinstance(r.layout, list)
    assert r.seats[0].seat_name == "A-001"
    assert r.seats[0].id == 1001


def test_raw_preserved(sample_layout_raw):
    r = LayoutQueryResult.from_response(sample_layout_raw)
    assert r.raw is sample_layout_raw
    assert r.seats[0].raw["someUnknownField"] == "保留未知字段示例"


def test_missing_fields_default_none():
    r = LayoutQueryResult.from_response({"seatList": [{"id": 1}]})
    seat = r.seats[0]
    assert seat.id == 1
    assert seat.seat_name is None
    assert seat.seat_status is None
    assert seat.is_can is None
    assert seat.open is None


def test_empty_seatlist():
    r = LayoutQueryResult.from_response({"seatList": []})
    assert r.seat_count == 0


def test_missing_seatlist_raises():
    with pytest.raises(ResponseShapeError):
        LayoutQueryResult.from_response({"layout": []})


def test_non_dict_response_raises():
    with pytest.raises(ResponseShapeError):
        LayoutQueryResult.from_response([1, 2, 3])


def test_seatlist_not_list_raises():
    with pytest.raises(ResponseShapeError):
        LayoutQueryResult.from_response({"seatList": "oops"})


def test_business_error_surfaced_before_shape_check():
    # 业务错误（HTTP 200 + code/msg，无 seatList）应抛出 BusinessError，而非“缺少 seatList”
    with pytest.raises(BusinessError) as ei:
        LayoutQueryResult.from_response({"code": 401, "msg": "未登录"})
    assert ei.value.code == 401
    assert ei.value.msg == "未登录"


def test_business_error_code_1111_not_shape_error():
    # 观察到的“开始/结束时间”业务错误也应归为 BusinessError
    with pytest.raises(BusinessError) as ei:
        LayoutQueryResult.from_response(
            {"code": 1111, "msg": "预约开始时间必须大于结束时间"}
        )
    assert ei.value.code == 1111


def test_business_error_code_500_empty_msg_preserves_body():
    # code=500 且 msg 缺失：错误应保留 code 与脱敏后的响应结构，不丢失线索
    with pytest.raises(BusinessError) as ei:
        LayoutQueryResult.from_response({"code": 500})
    assert ei.value.code == 500
    assert ei.value.msg is None
    assert ei.value.body is not None
    assert "500" in str(ei.value)
    assert "code" in str(ei.value)


def test_business_error_body_redacts_sensitive():
    # 业务错误响应中即使混入敏感字段，也不得泄漏其值
    with pytest.raises(BusinessError) as ei:
        LayoutQueryResult.from_response({"code": 500, "token": "leak-me", "msg": ""})
    assert "leak-me" not in str(ei.value)
    assert "<redacted>" in str(ei.value)


def test_no_pagination_metadata_needed():
    # 无分页元数据时应正常解析，且不臆造 total 等字段
    r = LayoutQueryResult.from_response({"seatList": [{"id": 1}]})
    assert r.seat_count == 1
    assert "total" not in r.raw


def test_pagination_metadata_preserved_not_interpreted():
    # 分页相关字段仅保留在 raw，不做任何分页解释/猜测
    r = LayoutQueryResult.from_response({"seatList": [{"id": 1}], "total": 999})
    assert r.raw["total"] == 999
    assert r.seat_count == 1


def test_non_dict_seat_skipped():
    r = LayoutQueryResult.from_response({"seatList": [{"id": 1}, "bad", {"id": 2}]})
    assert r.seat_count == 2


def test_seat_repr_has_no_pii():
    seat = Seat({"id": 1, "seatName": "A-001", "username": "张三"})
    assert "张三" not in repr(seat)


def test_layout_type_dict():
    r = LayoutQueryResult.from_response({"layout": {"regionid": 16}, "seatList": []})
    assert r.layout_type == "dict(1 键)"


def test_layout_type_null():
    r = LayoutQueryResult.from_response({"layout": None, "seatList": []})
    assert r.layout_type == "null"


def test_top_level_keys():
    r = LayoutQueryResult.from_response({"layout": [], "seatList": [], "total": 999})
    assert "layout" in r.top_level_keys()
    assert "seatList" in r.top_level_keys()
    assert "total" in r.top_level_keys()
