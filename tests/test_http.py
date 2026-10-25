"""HTTP 客户端测试：脱敏、状态码/重定向/HTML/非JSON 错误分类、重试、会话头。"""

from __future__ import annotations

import logging

import httpx
import pytest

from haut_seat.client.http import MAX_RESPONSE_BODY_BYTES, SeatHttpClient, redact_headers
from haut_seat.exceptions import (
    AuthenticationError,
    HttpError,
    NonJsonResponseError,
    RedirectError,
)


def test_redact_headers():
    h = {"Authorization": "Bearer abc", "Cookie": "x=y", "X-Request-Id": "1"}
    r = redact_headers(h)
    assert r["Authorization"] == "<redacted>"
    assert r["Cookie"] == "<redacted>"
    assert r["X-Request-Id"] == "1"


def test_get_parses_json():
    def handler(request):
        return httpx.Response(200, json={"ok": True})

    c = SeatHttpClient("https://example.com", transport=httpx.MockTransport(handler), min_interval=0)
    assert c.get("/x") == {"ok": True}
    c.close()


def test_get_http_error_raises_and_no_retry():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500, text="err")

    c = SeatHttpClient("https://example.com", transport=httpx.MockTransport(handler), min_interval=0)
    with pytest.raises(HttpError):
        c.get("/x")
    assert len(calls) == 1  # 4xx/5xx 不重试
    c.close()


def test_get_403_is_authentication_error_and_not_retried():
    """HTTP 403 在客户端层被**无条件**归入「凭据被拒」：单次尝试、不重试。

    这条钉住的是**当前行为**，不是「403 就等于令牌过期」的证据 ——
    项目实测到的凭据失效形态是「HTTP 200 + 业务 code=401」（见 core/auth.py 顶部）。
    """
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(403, text="forbidden")

    c = SeatHttpClient("https://example.com", transport=httpx.MockTransport(handler), min_interval=0)
    with pytest.raises(AuthenticationError) as excinfo:
        c.get("/x")
    assert excinfo.value.status_code == 403
    assert len(calls) == 1  # 403 不做重试
    c.close()


def test_get_403_does_not_inspect_body_or_content_type():
    """403 的响应体与 content-type 一概不看 ⇒ 网关/WAF 的 403 与应用自己的 403
    在这里**无法区分**。排查「持续 403」时因此不能只看状态码下结论。"""
    seen = {}

    def handler(request):
        seen["type"] = "gateway-html-403"
        return httpx.Response(
            403,
            headers={"content-type": "text/html", "server": "openresty"},
            text="<html><body>403 Forbidden by gateway</body></html>",
        )

    c = SeatHttpClient("https://example.com", transport=httpx.MockTransport(handler), min_interval=0)
    # 若是「HTML 响应」分支先命中，就会抛 NonJsonResponseError；这里必须仍是 AuthenticationError。
    with pytest.raises(AuthenticationError):
        c.get("/x")
    assert seen["type"] == "gateway-html-403"
    c.close()


def test_get_403_log_leaks_nothing_and_gives_no_403_origin_clue(caplog):
    """403 的日志只有路径与状态码：既不含凭据，也不含能判断 403 来源的线索
    （server / content-type / 响应体都不在里面）。"""
    sentinel_body = "SENTINEL-BODY-MARKER"

    def handler(request):
        return httpx.Response(
            403, headers={"set-cookie": "SENTINEL-SET-COOKIE=1"}, text=sentinel_body
        )

    c = SeatHttpClient(
        "https://example.com",
        transport=httpx.MockTransport(handler),
        min_interval=0,
        cookie="Admin-Token=SENTINEL-COOKIE-MARKER",
        authorization="Bearer SENTINEL-TOKEN-MARKER",
    )
    with caplog.at_level(logging.WARNING):
        with pytest.raises(AuthenticationError):
            c.get("/x")
    c.close()

    text = caplog.text
    assert "/x" in text and "403" in text
    for marker in (
        "SENTINEL-COOKIE-MARKER",
        "SENTINEL-TOKEN-MARKER",
        "SENTINEL-SET-COOKIE",
        sentinel_body,
        "openresty",
    ):
        assert marker not in text


def test_outgoing_request_shape_has_no_referer_or_origin_or_content_type():
    """钉住实际发出的请求形态（本轮排查的对象）：

    GET、无 ``content-type``；有 UA / Accept / Accept-Language；
    **没有** ``Referer`` / ``Origin`` / ``Sec-Fetch-*``（本项目从不发送这几个头）。
    """
    captured = {}

    def handler(request):
        captured["request"] = request
        return httpx.Response(200, json={"ok": True})

    c = SeatHttpClient(
        "https://example.com",
        transport=httpx.MockTransport(handler),
        min_interval=0,
        cookie="Admin-Token=SYNTHETIC",
        authorization="Bearer SYNTHETIC",
        accept="application/json, text/plain, */*",
        accept_language="zh-CN,zh;q=0.9",
        referer=None,
    )
    c.get("/stage-api/api/seatbook/layout/query", params={"starttime": "2026-01-01 08:00:00"})
    c.close()

    req = captured["request"]
    lower = {k.lower() for k in req.headers}
    assert req.method == "GET"
    assert "content-type" not in lower
    assert "referer" not in lower
    assert "origin" not in lower
    assert not [k for k in lower if k.startswith("sec-fetch-")]
    assert req.headers["user-agent"] == "haut-seat-helper/0.1 (personal read-only)"
    # 时间窗口里的空格由 httpx 编码（不会原样出现在请求行里）
    assert " " not in str(req.url)


def test_no_duplicate_cookie_or_authorization_header():
    """同名头只发一次：调用方自己传了同名头时以调用方的为准，**不是追加**。"""
    captured = {}

    def handler(request):
        captured["request"] = request
        return httpx.Response(200, json={"ok": True})

    c = SeatHttpClient(
        "https://example.com",
        transport=httpx.MockTransport(handler),
        min_interval=0,
        cookie="Admin-Token=SESSION",
        authorization="Bearer SESSION",
    )
    c.get(
        "/x",
        headers={"Authorization": "Bearer PER-CALL", "Cookie": "Admin-Token=PER-CALL"},
    )
    c.close()

    headers = captured["request"].headers
    auth_values = [v for k, v in headers.items() if k.lower() == "authorization"]
    cookie_values = [v for k, v in headers.items() if k.lower() == "cookie"]
    assert auth_values == ["Bearer PER-CALL"]
    assert cookie_values == ["Admin-Token=PER-CALL"]


def test_get_redirect_raises():
    def handler(request):
        return httpx.Response(302, headers={"location": "https://auth.example.com/login"})

    c = SeatHttpClient("https://example.com", transport=httpx.MockTransport(handler), min_interval=0)
    with pytest.raises(RedirectError):
        c.get("/x")
    c.close()


def test_get_html_response_raises():
    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text="<html>login</html>"
        )

    c = SeatHttpClient("https://example.com", transport=httpx.MockTransport(handler), min_interval=0)
    with pytest.raises(NonJsonResponseError):
        c.get("/x")
    c.close()


def test_get_non_json_response_raises():
    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "application/json"}, text="not-json{{{"
        )

    c = SeatHttpClient("https://example.com", transport=httpx.MockTransport(handler), min_interval=0)
    with pytest.raises(NonJsonResponseError):
        c.get("/x")
    c.close()


def test_get_transient_retries_then_raises():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("boom")

    c = SeatHttpClient(
        "https://example.com",
        transport=httpx.MockTransport(handler),
        min_interval=0,
        max_retry=2,
        backoff_base=0,
    )
    with pytest.raises(httpx.ConnectError):
        c.get("/x")
    assert len(calls) == 3
    c.close()


def test_get_no_retry_when_disabled():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("boom")

    c = SeatHttpClient(
        "https://example.com",
        transport=httpx.MockTransport(handler),
        min_interval=0,
        max_retry=5,
        backoff_base=0,
    )
    with pytest.raises(httpx.ConnectError):
        c.get("/x", retry=False)
    assert len(calls) == 1
    c.close()


def test_get_attaches_session_headers():
    captured = {}

    def handler(request):
        captured["cookie"] = request.headers.get("cookie")
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"ok": True})

    c = SeatHttpClient(
        "https://example.com",
        transport=httpx.MockTransport(handler),
        min_interval=0,
        cookie="SESSIONID=abc123",
        authorization="Bearer xyz789",
    )
    assert c.get("/x") == {"ok": True}
    assert captured["cookie"] == "SESSIONID=abc123"
    assert captured["authorization"] == "Bearer xyz789"
    c.close()


def test_get_attaches_non_sensitive_headers():
    captured = {}

    def handler(request):
        captured["accept"] = request.headers.get("accept")
        captured["accept-language"] = request.headers.get("accept-language")
        captured["referer"] = request.headers.get("referer")
        captured["user-agent"] = request.headers.get("user-agent")
        return httpx.Response(200, json={"ok": True})

    c = SeatHttpClient(
        "https://example.com",
        transport=httpx.MockTransport(handler),
        min_interval=0,
        accept="application/json",
        accept_language="zh-CN,zh;q=0.9",
        referer="https://wslib.haut.edu.cn/seat",
        user_agent="test-ua/1.0",
    )
    assert c.get("/x") == {"ok": True}
    assert captured["accept"] == "application/json"
    assert captured["accept-language"] == "zh-CN,zh;q=0.9"
    assert captured["referer"] == "https://wslib.haut.edu.cn/seat"
    assert captured["user-agent"] == "test-ua/1.0"
    c.close()


def test_get_omits_empty_optional_headers():
    captured = {}

    def handler(request):
        captured["referer"] = request.headers.get("referer")
        return httpx.Response(200, json={"ok": True})

    c = SeatHttpClient(
        "https://example.com",
        transport=httpx.MockTransport(handler),
        min_interval=0,
        accept=None,
        accept_language=None,
        referer=None,
    )
    c.get("/x")
    assert captured["referer"] is None
    c.close()


def test_session_credentials_not_logged(caplog):
    def handler(request):
        return httpx.Response(200, json={"ok": True})

    c = SeatHttpClient(
        "https://example.com",
        transport=httpx.MockTransport(handler),
        min_interval=0,
        cookie="SESSIONID=supersecretcookie",
        authorization="Bearer supersecrettoken",
        logger=logging.getLogger("haut_seat.http"),
    )
    with caplog.at_level(logging.DEBUG):
        c.get("/x")
    c.close()

    joined = "\n".join(caplog.messages)
    assert "supersecretcookie" not in joined
    assert "supersecrettoken" not in joined


def test_get_oversized_response_raises():
    # 超长响应体应被拒绝，避免进入内存/日志
    big = b'{"a": "' + b"x" * (MAX_RESPONSE_BODY_BYTES + 100) + b'"}'

    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "application/json"}, content=big
        )

    c = SeatHttpClient("https://example.com", transport=httpx.MockTransport(handler), min_interval=0)
    with pytest.raises(NonJsonResponseError):
        c.get("/x")
    c.close()


def test_post_sends_json_and_parses():
    captured = {}

    def handler(request):
        captured["method"] = request.method
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"code": 0})

    c = SeatHttpClient("https://example.com", transport=httpx.MockTransport(handler), min_interval=0)
    r = c.post("/submit", json={"seatId": 1})
    assert r == {"code": 0}
    assert captured["method"] == "POST"
    assert "seatId" in captured["body"]
    c.close()


def test_post_no_retry_on_transient():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("boom")

    c = SeatHttpClient(
        "https://example.com", transport=httpx.MockTransport(handler), min_interval=0, max_retry=5
    )
    with pytest.raises(httpx.ConnectError):
        c.post("/submit")
    assert len(calls) == 1  # 副作用请求绝不自动重试
    c.close()
