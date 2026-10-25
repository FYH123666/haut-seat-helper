"""座位查询客户端测试：参数构造与 addbooking 禁用保护。"""

from __future__ import annotations

import httpx
import pytest

from haut_seat.client.http import SeatHttpClient
from haut_seat.client.seatbook import SeatBookClient
from haut_seat.models.booking import BookingOutcome, BookingResult
from haut_seat.models.seat import Seat


def test_query_layout_builds_params():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        return httpx.Response(
            200, json={"layout": [], "seatList": [{"id": 1, "seatName": "A-001"}]}
        )

    c = SeatHttpClient("https://example.com", transport=httpx.MockTransport(handler), min_interval=0)
    sb = SeatBookClient(c)
    r = sb.query_layout(
        regionid=1,
        starttime="2026-10-16 08:00",
        endtime="2026-10-16 12:00",
        page_num=1,
        page_size=50,
    )
    assert r.seat_count == 1
    assert "regionid=1" in captured["url"]
    assert "pageNum=1" in captured["url"]
    assert "pageSize=50" in captured["url"]
    assert "starttime=2026-10-16" in captured["url"]
    assert "endtime=2026-10-16" in captured["url"]
    assert "/seatbook/layout/query" in captured["url"]
    assert "addbooking" not in captured["url"]
    c.close()


def test_query_layout_time_params_passed_verbatim():
    captured = {}

    def handler(request):
        captured["starttime"] = request.url.params.get("starttime")
        captured["endtime"] = request.url.params.get("endtime")
        return httpx.Response(200, json={"layout": [], "seatList": []})

    c = SeatHttpClient("https://example.com", transport=httpx.MockTransport(handler), min_interval=0)
    sb = SeatBookClient(c)
    sb.query_layout(
        regionid=1,
        starttime="2026-10-14 10:00:00",
        endtime="2026-10-14 10:30:00",
    )
    # 值原样透传：不反转、不重排、不重格式化
    assert captured["starttime"] == "2026-10-14 10:00:00"
    assert captured["endtime"] == "2026-10-14 10:30:00"
    c.close()


def test_query_layout_sends_session_cookie():
    captured = {}

    def handler(request):
        captured["cookie"] = request.headers.get("cookie")
        return httpx.Response(200, json={"layout": [], "seatList": []})

    c = SeatHttpClient(
        "https://example.com",
        transport=httpx.MockTransport(handler),
        min_interval=0,
        cookie="SESSIONID=abc",
    )
    sb = SeatBookClient(c)
    sb.query_layout(regionid=1, starttime="s", endtime="e")
    assert captured["cookie"] == "SESSIONID=abc"
    c.close()


def test_addbooking_not_implemented_and_no_request():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={})

    c = SeatHttpClient(
        "https://example.com", transport=httpx.MockTransport(handler), min_interval=0
    )
    sb = SeatBookClient(c)
    with pytest.raises(NotImplementedError):
        sb.addbooking(seatid=1, starttime="x", endtime="y")
    assert calls == []  # 未发送任何 HTTP 请求
    c.close()


def test_submit_booking_builds_params():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"code": 0})

    c = SeatHttpClient("https://example.com", transport=httpx.MockTransport(handler), min_interval=0)
    sb = SeatBookClient(c)
    seat = Seat({"id": 99, "seatName": "A-001"})
    r = sb.submit_booking(seat, "2026-10-16 08:00:00", "2026-10-16 12:00:00", "1001")
    assert r.outcome is BookingOutcome.UNKNOWN  # code=0 → 无法确认
    assert "seatid=99" in captured["url"]
    assert "channel=1001" in captured["url"]
    assert "starttime=2026-10-16" in captured["url"]
    assert "endtime=2026-10-16" in captured["url"]
    assert "/seatbook/user/addbooking" in captured["url"]
    c.close()


def test_submit_booking_no_retry_on_transient():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("boom")

    c = SeatHttpClient("https://example.com", transport=httpx.MockTransport(handler), min_interval=0, max_retry=5)
    sb = SeatBookClient(c)
    r = sb.submit_booking(Seat({"id": 1, "seatName": "X"}), "s", "e", "1001")
    assert len(calls) == 1                     # 底层仅 1 次尝试
    assert r.outcome is BookingOutcome.FAILED  # 网络错误→可重试的 FAILED
    assert "可重试" in r.message
    c.close()


def test_submit_booking_timeout_unknown():
    def handler(request):
        raise httpx.TimeoutException("timeout")

    c = SeatHttpClient("https://example.com", transport=httpx.MockTransport(handler), min_interval=0)
    sb = SeatBookClient(c)
    r = sb.submit_booking(Seat({"id": 1, "seatName": "X"}), "s", "e", "1001")
    assert r.outcome is BookingOutcome.UNKNOWN  # 超时不可确认是否已提交
    c.close()


def test_submit_booking_connect_error_retriable():
    def handler(request):
        raise httpx.ConnectError("refused")

    c = SeatHttpClient("https://example.com", transport=httpx.MockTransport(handler), min_interval=0)
    sb = SeatBookClient(c)
    r = sb.submit_booking(Seat({"id": 1, "seatName": "X"}), "s", "e", "1001")
    assert r.outcome is BookingOutcome.FAILED  # 连接失败 → 请求未到达 → 可重试
    c.close()


def test_submit_booking_redirect_auth_failed():
    def handler(request):
        return httpx.Response(302, headers={"location": "https://auth.example.com/login"})

    c = SeatHttpClient("https://example.com", transport=httpx.MockTransport(handler), min_interval=0)
    sb = SeatBookClient(c)
    r = sb.submit_booking(Seat({"id": 1, "seatName": "X"}), "s", "e", "1001")
    assert r.outcome is BookingOutcome.AUTH_FAILED
    c.close()


def test_submit_booking_http_error_failed():
    def handler(request):
        return httpx.Response(500)

    c = SeatHttpClient("https://example.com", transport=httpx.MockTransport(handler), min_interval=0)
    sb = SeatBookClient(c)
    r = sb.submit_booking(Seat({"id": 1, "seatName": "X"}), "s", "e", "1001")
    assert r.outcome is BookingOutcome.FAILED
    c.close()


def test_submit_booking_business_code_failed():
    def handler(request):
        return httpx.Response(200, json={"code": 1111, "msg": "只能预约1天之内的时间(含当天)"})

    c = SeatHttpClient("https://example.com", transport=httpx.MockTransport(handler), min_interval=0)
    sb = SeatBookClient(c)
    r = sb.submit_booking(Seat({"id": 1, "seatName": "X"}), "s", "e", "1001")
    assert r.outcome is BookingOutcome.FAILED
    assert "预约" in r.message
    c.close()


def test_submit_booking_code_zero_unknown():
    def handler(request):
        return httpx.Response(200, json={"code": 0})

    c = SeatHttpClient("https://example.com", transport=httpx.MockTransport(handler), min_interval=0)
    sb = SeatBookClient(c)
    r = sb.submit_booking(Seat({"id": 1, "seatName": "X"}), "s", "e", "1001")
    assert r.outcome is BookingOutcome.UNKNOWN
    c.close()


def test_submit_booking_no_code_unknown():
    def handler(request):
        return httpx.Response(200, json={"status": "ok"})

    c = SeatHttpClient("https://example.com", transport=httpx.MockTransport(handler), min_interval=0)
    sb = SeatBookClient(c)
    r = sb.submit_booking(Seat({"id": 1, "seatName": "X"}), "s", "e", "1001")
    assert r.outcome is BookingOutcome.UNKNOWN
    c.close()


def test_submit_booking_success():
    def handler(request):
        return httpx.Response(200, json={
            "code": 200, "msg": "成功",
            "seatName": "九层西书库北区 03桌 B座",
            "bookingid": 9066132,
        })

    c = SeatHttpClient("https://example.com", transport=httpx.MockTransport(handler), min_interval=0)
    sb = SeatBookClient(c)
    r = sb.submit_booking(Seat({"id": 1, "seatName": "X"}), "s", "e", "1001")
    assert r.outcome is BookingOutcome.SUCCESS
    assert r.booking_id == 9066132
    assert "9066132" in r.message
    c.close()


def test_submit_booking_code_200_no_bookingid():
    def handler(request):
        return httpx.Response(200, json={"code": 200, "msg": "成功"})

    c = SeatHttpClient("https://example.com", transport=httpx.MockTransport(handler), min_interval=0)
    sb = SeatBookClient(c)
    r = sb.submit_booking(Seat({"id": 1, "seatName": "X"}), "s", "e", "1001")
    assert r.outcome is BookingOutcome.UNKNOWN
    assert "bookingid" in r.message
    c.close()


def test_submit_booking_success_excludes_pii():
    def handler(request):
        return httpx.Response(200, json={
            "code": 200, "msg": "成功",
            "seatName": "A-001", "bookingid": 1,
            "readerno": "20200001", "realname": "张三",
        })

    c = SeatHttpClient("https://example.com", transport=httpx.MockTransport(handler), min_interval=0)
    sb = SeatBookClient(c)
    r = sb.submit_booking(Seat({"id": 1, "seatName": "X"}), "s", "e", "1001")
    assert r.outcome is BookingOutcome.SUCCESS
    assert "20200001" not in r.message
    assert "张三" not in r.message
    c.close()


def test_query_booking_result_not_implemented_and_no_request():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={})

    c = SeatHttpClient(
        "https://example.com", transport=httpx.MockTransport(handler), min_interval=0
    )
    sb = SeatBookClient(c)
    with pytest.raises(NotImplementedError):
        sb.query_booking_result()
    assert calls == []  # 未发送任何 HTTP 请求
    c.close()
