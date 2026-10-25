"""响应体脱敏与结构诊断测试。"""

from __future__ import annotations

from haut_seat.sanitize import is_sensitive_key, sanitize_json, sanitize_to_text


def test_is_sensitive_key_credentials():
    for k in [
        "token", "accessToken", "Cookie", "authorization", "password",
        "sessionId", "set-cookie", "X-Auth-Token", "apiKey", "signature",
    ]:
        assert is_sensitive_key(k), k


def test_is_sensitive_key_personal():
    for k in ["username", "realName", "phone", "mobile", "idCard", "studentNo"]:
        assert is_sensitive_key(k), k


def test_is_sensitive_key_negative():
    for k in ["code", "msg", "seatName", "seatList", "layout", "id", "seatStatus", "isCan", "open"]:
        assert not is_sensitive_key(k), k


def test_sanitize_redacts_sensitive_values():
    v = {"code": 500, "msg": "", "token": "secret-token-abc", "seatName": "A-001"}
    s = sanitize_json(v)
    assert s["token"] == "<redacted>"
    assert s["seatName"] == "A-001"
    assert s["code"] == 500


def test_sanitize_recursive():
    v = {"data": {"rows": [{"username": "张三", "accessToken": "x"}]}}
    s = sanitize_json(v)
    assert s["data"]["rows"][0]["username"] == "<redacted>"
    assert s["data"]["rows"][0]["accessToken"] == "<redacted>"


def test_sanitize_truncates_long_string():
    v = {"msg": "a" * 1000}
    s = sanitize_json(v, max_str_len=10)
    assert s["msg"] == "a" * 10 + "…(len=1000)"


def test_sanitize_limits_list_items():
    v = {"list": list(range(1000))}
    s = sanitize_json(v, max_items=5)
    assert s["list"] == [0, 1, 2, 3, 4, "…(省略 995 项)"]


def test_sanitize_limits_depth():
    v = {"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}}
    s = sanitize_json(v, max_depth=3)
    assert "深度超限" in s["a"]["b"]["c"]["d"]


def test_sanitize_to_text_has_no_secret():
    text = sanitize_to_text({"token": "supersecret", "code": 500})
    assert "supersecret" not in text
    assert "<redacted>" in text
    assert "500" in text


def test_sanitize_to_text_is_bounded():
    # 大量字段（每个都被截断到 200 字符）叠加后，序列化文本仍需受 limit 限制
    v = {f"f{i}": "x" * 1000 for i in range(60)}
    text = sanitize_to_text(v, limit=1000)
    assert len(text) <= 1000 + 32  # 截断后缀少量余量
    assert "诊断文本截断" in text
