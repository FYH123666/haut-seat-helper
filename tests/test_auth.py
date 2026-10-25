"""认证预检测试：判定三态、误判防护、失效后禁止提交。

全部使用 httpx.MockTransport 模拟响应，**不发送任何真实请求，
更不发送任何真实预约请求（addbooking）**。

判定依据来自受控实测（见 haut_seat/core/auth.py 模块文档，2026-10-23 更新）：

* ``/stage-api/api/seatbook/layout/query``：携带已失效凭据与**完全不携带**凭据时
  响应逐字相同（都能拿到座位列表），时间窗口还会命中 code=1111 —— 因此它
  **不能**用来验证凭据，``classify_body`` 永不返回 VALID。
* ``/stage-api/getInfo``：站点自己登录后调用的业务身份接口，实测无凭据与失效凭据
  都返回业务 code=401 —— 它才是凭据验证入口（``classify_identity``）。
* 站点自身错误表：``401:"认证失败，无法访问系统资源"``、``403:"当前操作没有权限"``
  ⇒ 403 **不得**被表述成「令牌过期」。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx
import pytest

from haut_seat.client.http import SeatHttpClient
from haut_seat.client.seatbook import ADD_BOOKING_PATH, LAYOUT_QUERY_PATH, SeatBookClient
from haut_seat.core.auth import (
    AUTH_403_NOTE,
    AuthStatus,
    canonical_probe_window,
    classify_body,
    classify_exception,
    classify_identity,
)
from haut_seat.exceptions import AuthenticationError, HttpError, NonJsonResponseError, RedirectError
from haut_seat.models.booking import BookingOutcome, BookingResult
from haut_seat.models.seat import Seat

# 实测到的认证失效响应体（HTTP 200 + 业务 code 401），已去掉无关字符
OBSERVED_AUTH_FAILURE_BODY = {
    "code": 401,
    "msg": "请求访问：/spacelink/error，认证失败，无法访问系统资源",
}
OBSERVED_SUCCESS_BODY = {"code": 200, "msg": "成功", "layout": [], "seatList": [{"id": 1}]}
# 实测到的身份接口未认证响应体（路径与匿名请求一致）
OBSERVED_IDENTITY_401_BODY = {
    "code": 401,
    "msg": "请求访问：/spacelink/getInfo，认证失败，无法访问系统资源",
}
# 站点自身路由守卫在登录成功后读到的形状（字段名取自站点 JS；值只是测试替身）
IDENTITY_SUCCESS_BODY = {
    "code": 200,
    "msg": "成功",
    "user": {"userId": 1, "userName": "张三", "studentNo": "20230001"},
    "roles": ["common"],
    "permissions": ["*:*:*"],
}


def _client(handler, **kw):
    return SeatHttpClient(
        "https://example.com",
        transport=httpx.MockTransport(handler),
        min_interval=0,
        **kw,
    )


# ---------------------------------------------------------------- 纯判定函数


def test_classify_body_with_seats_is_never_a_credential_verdict():
    """座位列表**不是**凭据有效的证明（实测：匿名请求同样拿得到座位）。"""
    check = classify_body(OBSERVED_SUCCESS_BODY)
    assert check.status is AuthStatus.INDETERMINATE
    assert check.seat_count == 1
    assert "无法据此验证凭据" in check.message


def test_classify_identity_valid_on_real_business_data():
    """凭据验证的唯一成功依据：业务身份接口返回 code=200 且带 user/roles。"""
    check = classify_identity(IDENTITY_SUCCESS_BODY)
    assert check.status is AuthStatus.VALID
    assert "user" in check.message and "roles" in check.message
    # 消息里只有字段名与条数，绝不出现字段值（那是个人信息）
    assert "张三" not in check.message
    assert "20230001" not in check.message


def test_classify_identity_200_without_identity_data_is_not_success():
    check = classify_identity({"code": 200, "msg": "成功"})
    assert check.status is AuthStatus.INDETERMINATE
    assert "缺 user/roles" in check.message


def test_classify_identity_observed_401_body_is_not_accepted():
    check = classify_identity(OBSERVED_IDENTITY_401_BODY)
    assert check.status is AuthStatus.INVALID
    assert check.business_code == 401
    assert check.http_status == 200, "实测认证失败在 HTTP 层仍是 200"
    assert "认证失败" in check.message


def test_classify_identity_403_does_not_claim_expiry():
    check = classify_identity({"code": 403, "msg": "当前操作没有权限"})
    assert check.status is AuthStatus.INVALID
    assert AUTH_403_NOTE in check.message
    assert "未据此推断令牌是否过期" in check.message


def test_classify_body_is_still_the_old_contract_for_the_seat_query():
    """仅座位查询这一路：业务 code=401 仍然表示「服务器拒绝了这个请求头」。"""
    check = classify_body(OBSERVED_AUTH_FAILURE_BODY)
    assert check.status is AuthStatus.INVALID
    assert check.business_code == 401


def test_classify_observed_auth_failure_code_401():
    """实测信号：HTTP 200 但业务 code=401 → 必须判定为认证失效。"""
    check = classify_body(OBSERVED_AUTH_FAILURE_BODY)
    assert check.status is AuthStatus.INVALID
    assert check.business_code == 401


def test_classify_auth_failure_by_message_only():
    """code 缺失但 msg 命中认证失败文案且无 seatList → 仍判失效。"""
    check = classify_body({"msg": "认证失败，无法访问系统资源"})
    assert check.status is AuthStatus.INVALID


def test_classify_code_403_is_invalid():
    assert classify_body({"code": 403, "msg": "无权限"}).status is AuthStatus.INVALID


def test_classify_string_code_401_is_invalid():
    assert classify_body({"code": "401"}).status is AuthStatus.INVALID


def test_classify_time_rule_1111_is_not_auth_failure():
    """时间规则错误绝不能被误判为认证失效。"""
    check = classify_body({"code": 1111, "msg": "只能预约1天之内的时间(含当天)", "layout": []})
    assert check.status is AuthStatus.INDETERMINATE


def test_classify_same_day_404_is_not_auth_failure():
    check = classify_body({"code": 404, "msg": "开始时间与结束时间必须是同一天"})
    assert check.status is AuthStatus.INDETERMINATE


def test_classify_server_error_500_is_not_auth_failure():
    """实测：服务端 code=500（msg 为 null）——不是认证问题。"""
    check = classify_body({"code": 500, "msg": None})
    assert check.status is AuthStatus.INDETERMINATE
    assert "服务端" in check.message


def test_classify_anonymous_success_is_indeterminate():
    """无 Authorization 时只读接口也返回成功 → 不能据此声称登录有效。"""
    check = classify_body(OBSERVED_SUCCESS_BODY, expect_authenticated=False)
    assert check.status is AuthStatus.INDETERMINATE
    assert "HAUT_AUTHORIZATION" in check.message


def test_classify_non_dict_body_indeterminate():
    assert classify_body([1, 2, 3]).status is AuthStatus.INDETERMINATE


def test_classify_exception_http_401_403_invalid():
    for status in (401, 403):
        check = classify_exception(AuthenticationError(status))
        assert check.status is AuthStatus.INVALID
        assert check.http_status == status


def test_http_403_is_reported_as_not_accepted_without_claiming_expiry():
    """钉住 HTTP 403 的**当前措辞与判定**。

    站点自己的前端错误表把 403 记为「当前操作没有权限」，所以：

    * 判定方向保守（``INVALID`` ⇒ 阻断提交），这是刻意保留的；
    * 但措辞**必须**如实：不得断言令牌过期，也不得把它等同于「认证失败」；
      需要按响应内容/来源区分 403 的真实出处（网关、权限、还是真的未认证）。
    """
    check = classify_exception(AuthenticationError(403))
    assert check.status is AuthStatus.INVALID
    assert check.http_status == 403
    assert "403" in check.message
    assert AUTH_403_NOTE in check.message
    assert "不据此断言令牌过期" in check.message
    assert "认证已失效" not in check.message
    # 判定为未接受 ⇒ 会阻断提交（保守方向），这是刻意保留的行为
    assert check.is_invalid is True


def test_http_401_keeps_the_status_without_claiming_expiry():
    check = classify_exception(AuthenticationError(401))
    assert check.status is AuthStatus.INVALID
    assert check.http_status == 401
    assert "未据此推断令牌是否过期" in check.message


def test_classify_exception_redirect_is_invalid():
    assert classify_exception(RedirectError("302")).status is AuthStatus.INVALID


def test_classify_exception_http_500_indeterminate():
    """服务器异常 ≠ 认证失效。"""
    check = classify_exception(HttpError(500))
    assert check.status is AuthStatus.INDETERMINATE


def test_classify_exception_http_404_indeterminate():
    assert classify_exception(HttpError(404)).status is AuthStatus.INDETERMINATE


def test_classify_exception_network_errors_indeterminate():
    """网络故障 ≠ 认证失效。"""
    assert classify_exception(httpx.ConnectError("refused")).status is AuthStatus.INDETERMINATE
    assert classify_exception(httpx.ReadTimeout("timeout")).status is AuthStatus.INDETERMINATE


def test_classify_exception_malformed_json_indeterminate():
    check = classify_exception(NonJsonResponseError("响应不是有效 JSON: ..."))
    assert check.status is AuthStatus.INDETERMINATE


def test_classify_exception_html_is_indeterminate_not_invalid():
    """HTML 可能是登录页，也可能是网关错误页 → 不武断判失效。"""
    check = classify_exception(NonJsonResponseError("收到 HTML 响应（content-type='text/html'），可能是登录页"))
    assert check.status is AuthStatus.INDETERMINATE


def test_classify_exception_unknown_indeterminate():
    assert classify_exception(RuntimeError("boom")).status is AuthStatus.INDETERMINATE


# ---------------------------------------------------------------- 预检编排


def test_canonical_probe_window_is_same_day_and_ordered():
    start, end = canonical_probe_window()
    assert start[:10] == end[:10]
    assert start < end


def test_check_session_hits_readonly_endpoint_only():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=OBSERVED_SUCCESS_BODY)

    c = _client(handler)
    sb = SeatBookClient(c)
    check = sb.check_session(regionid=16)

    # 座位查询只回答"这次查询被受理了吗"，不回答"凭据可用吗"
    assert check.status is AuthStatus.INDETERMINATE
    assert check.seat_count == 1
    assert len(calls) == 1
    assert LAYOUT_QUERY_PATH in calls[0]
    assert ADD_BOOKING_PATH not in calls[0]
    assert "addbooking" not in calls[0]
    c.close()


def test_check_identity_hits_only_the_business_identity_endpoint():
    """凭据验证只打业务身份接口：一个请求、只读、绝不碰提交接口。"""
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=IDENTITY_SUCCESS_BODY)

    c = _client(handler)
    check = SeatBookClient(c).check_identity()

    assert check.status is AuthStatus.VALID
    assert len(calls) == 1
    assert "/stage-api/getInfo" in calls[0]
    assert "addbooking" not in calls[0]
    c.close()


def test_check_identity_on_observed_401_body():
    def handler(request):
        return httpx.Response(200, json=OBSERVED_IDENTITY_401_BODY)

    c = _client(handler)
    check = SeatBookClient(c).check_identity()
    assert check.status is AuthStatus.INVALID
    c.close()


def test_check_identity_without_authorization_sends_nothing():
    """只有 Cookie 时不发请求，也绝不因此判成功（实测服务端只认 Authorization）。"""
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=IDENTITY_SUCCESS_BODY)

    c = _client(handler)
    check = SeatBookClient(c).check_identity(expect_authenticated=False)
    assert check.status is AuthStatus.INDETERMINATE
    assert calls == []
    c.close()


def test_check_session_invalid_on_observed_401_body():
    def handler(request):
        return httpx.Response(200, json=OBSERVED_AUTH_FAILURE_BODY)

    c = _client(handler)
    check = SeatBookClient(c).check_session(regionid=16)
    assert check.status is AuthStatus.INVALID
    c.close()


def test_check_session_indeterminate_on_server_error():
    def handler(request):
        return httpx.Response(200, json={"code": 500, "msg": None})

    c = _client(handler)
    check = SeatBookClient(c).check_session(regionid=16)
    assert check.status is AuthStatus.INDETERMINATE
    c.close()


def test_check_session_indeterminate_on_network_error():
    def handler(request):
        raise httpx.ConnectError("refused")

    c = _client(handler, max_retry=0)
    check = SeatBookClient(c).check_session(regionid=16)
    assert check.status is AuthStatus.INDETERMINATE
    c.close()


def test_check_session_does_not_leak_credentials(caplog):
    """预检日志与结果中不得出现凭据值。"""
    import logging

    def handler(request):
        return httpx.Response(200, json=OBSERVED_SUCCESS_BODY)

    c = _client(
        handler,
        cookie="Admin-Token=SECRETCOOKIE",
        authorization="Bearer SECRETTOKEN",
    )
    with caplog.at_level(logging.DEBUG):
        check = SeatBookClient(c).check_session(regionid=16)
    c.close()

    joined = "\n".join(caplog.messages) + check.message
    assert "SECRETCOOKIE" not in joined
    assert "SECRETTOKEN" not in joined


# ---------------------------------------------------------------- 失效后禁止提交


def test_submit_booking_observed_auth_failure_is_auth_failed_no_retry():
    """提交返回 HTTP 200 + code=401 时，必须判 AUTH_FAILED，且只请求一次。"""
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=OBSERVED_AUTH_FAILURE_BODY)

    c = _client(handler, max_retry=5)
    sb = SeatBookClient(c)
    r = sb.submit_booking(Seat({"id": 1, "seatName": "X"}), "s", "e", "1001")
    assert r.outcome is BookingOutcome.AUTH_FAILED
    assert len(calls) == 1
    c.close()


def test_submit_booking_http_403_is_auth_failed():
    def handler(request):
        return httpx.Response(403)

    c = _client(handler)
    r = SeatBookClient(c).submit_booking(Seat({"id": 1, "seatName": "X"}), "s", "e", "1001")
    assert r.outcome is BookingOutcome.AUTH_FAILED
    c.close()


def test_submit_booking_http_500_still_plain_failure():
    """服务端 5xx 不能被升级为认证失效判定。"""
    def handler(request):
        return httpx.Response(500)

    c = _client(handler)
    r = SeatBookClient(c).submit_booking(Seat({"id": 1, "seatName": "X"}), "s", "e", "1001")
    assert r.outcome is BookingOutcome.FAILED
    c.close()


def test_submit_booking_auth_failure_never_reported_as_success():
    """认证失效响应绝不能误报为预约成功。"""
    def handler(request):
        return httpx.Response(200, json={**OBSERVED_AUTH_FAILURE_BODY, "bookingid": 999})

    c = _client(handler)
    r = SeatBookClient(c).submit_booking(Seat({"id": 1, "seatName": "X"}), "s", "e", "1001")
    assert r.outcome is BookingOutcome.AUTH_FAILED
    assert r.booking_id is None
    c.close()


# ---------------------------------------------------------------- cmd_book 集成


@pytest.fixture
def booking_cfg(tmp_path):
    """当天、未来时段的有效配置（时刻相对当前时间，避免 22:00 后无可用窗口）。

    窗口较短（5 分钟），配合下面的假时钟让刷新循环必然自行结束，不会挂住测试。
    """
    from datetime import datetime, timedelta

    start = datetime.now() + timedelta(seconds=60)
    end = start + timedelta(minutes=4)
    cfg = {
        "regionid": 16,
        "start": start.strftime("%Y-%m-%d %H:%M:%S"),
        "end": end.strftime("%Y-%m-%d %H:%M:%S"),
        "preferred_seats": ["A-001"],
        "preferred_prefixes": [],
        "confirmed_bookable_states": [["2", 1, False]],
        "trigger_at": "07:00:00",
    }
    p = tmp_path / "booking.json"
    p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return p


class _FakeClock:
    """可控时钟：sleep 只推进时间，不真正等待，保证循环测试确定且不挂住。"""

    def __init__(self, start: float):
        self.t = start

    def time(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(0.0, float(seconds))


@pytest.fixture
def book_env(monkeypatch, tmp_path):
    """隔离文件锁、暂停旗标、真实等待与时间规则校验（时间规则非本测试主题）。"""
    import time as _time

    import haut_seat.__main__ as m

    clock = _FakeClock(_time.time())
    monkeypatch.setattr(m, "FileLock", lambda *a, **k: _FakeLock())
    monkeypatch.setattr(m, "_wait_until", lambda *a, **k: None)
    # 只替换 __main__ 命名空间里的 time 引用，不污染标准库 time 模块
    monkeypatch.setattr(m, "time", _FakeTimeModule(clock))
    monkeypatch.setattr(m, "check_time_validity", lambda *a, **k: [])
    return m


class _FakeTimeModule:
    """__main__ 中仅用到 time.time() 与 time.sleep()，这里做最小替身。"""

    def __init__(self, clock: "_FakeClock"):
        self._clock = clock

    def time(self) -> float:
        return self._clock.time()

    def sleep(self, seconds: float) -> None:
        self._clock.sleep(seconds)


class _FakeLock:
    def acquire(self):
        return True

    def release(self):
        pass


def _book_args(cfg_path):
    return argparse.Namespace(config=str(cfg_path))


def _logger():
    import logging

    return logging.getLogger("test")


def test_cmd_book_stops_on_invalid_auth_without_submitting(
    book_env, booking_cfg, make_settings, monkeypatch, capsys
):
    """认证失效 → 立即停止：既不轮询座位，也不提交预约。"""
    m = book_env
    submitted, queried = [], []
    monkeypatch.setattr(m, "_auth_precheck", lambda *a, **k: _invalid_check())
    monkeypatch.setattr(m, "_submit_online", lambda *a, **k: submitted.append(a))
    monkeypatch.setattr(m, "_query_online", lambda *a, **k: queried.append(a))

    settings = make_settings(
        enable_real_booking=True, pause_file=str(booking_cfg.parent / "nope.flag")
    )
    rc = m.cmd_book(_book_args(booking_cfg), settings, _logger())
    out = capsys.readouterr().out

    assert rc == 3
    assert submitted == []   # 未提交任何预约
    assert queried == []     # 未轮询座位
    assert "HAUT_AUTHORIZATION" in out  # 输出中文更新指引
    assert "check-auth" in out


def test_cmd_book_continues_on_indeterminate_auth(
    book_env, booking_cfg, make_settings, monkeypatch, capsys
):
    """无法确认登录状态（网络/服务端异常）不得被当成失效：应继续执行。"""
    m = book_env
    submitted = []
    monkeypatch.setattr(m, "_auth_precheck", lambda *a, **k: _indeterminate_check())
    monkeypatch.setattr(m, "_query_online", lambda *a, **k: _empty_result())
    monkeypatch.setattr(m, "_submit_online", lambda *a, **k: submitted.append(a))

    settings = make_settings(
        enable_real_booking=True, pause_file=str(booking_cfg.parent / "nope.flag")
    )
    rc = m.cmd_book(_book_args(booking_cfg), settings, _logger())
    out = capsys.readouterr().out

    assert rc != 3
    assert "无法确认登录状态" in out
    assert submitted == []  # 无候选座位，绝不提交


def test_cmd_book_stops_when_refresh_query_reports_auth_failure(
    book_env, booking_cfg, make_settings, monkeypatch, capsys
):
    """等待结束后，刷新查询返回认证失效 → 停止，不提交预约。"""
    m = book_env
    submitted = []
    monkeypatch.setattr(m, "_auth_precheck", lambda *a, **k: _valid_check())

    def _raise(*a, **k):
        raise AuthenticationError(401)

    monkeypatch.setattr(m, "_query_online", _raise)
    monkeypatch.setattr(m, "_submit_online", lambda *a, **k: submitted.append(a))

    settings = make_settings(
        enable_real_booking=True, pause_file=str(booking_cfg.parent / "nope.flag")
    )
    rc = m.cmd_book(_book_args(booking_cfg), settings, _logger())
    out = capsys.readouterr().out

    assert rc == 3
    assert submitted == []
    assert "HAUT_AUTHORIZATION" in out  # 中文更新指引已输出


def test_cmd_book_stops_on_auth_failed_submit_without_retrying(
    book_env, booking_cfg, make_settings, monkeypatch, capsys
):
    """提交返回认证失效 → 停止且只提交一次（绝不重试提交）。"""
    from haut_seat.core.priority import SeatState  # noqa: F401  (仅用于可读性)

    m = book_env
    submits = []

    class _OneSeatResult:
        def __init__(self):
            self.seats = [Seat({"id": 7, "seatName": "A-001", "seatStatus": "2", "isCan": 1, "open": False})]

    monkeypatch.setattr(m, "_auth_precheck", lambda *a, **k: _valid_check())
    monkeypatch.setattr(m, "_query_online", lambda *a, **k: _OneSeatResult())
    monkeypatch.setattr(
        m,
        "_submit_online",
        lambda *a, **k: (
            submits.append(a)
            or BookingResult(BookingOutcome.AUTH_FAILED, "服务器返回认证失败 code=401")
        ),
    )

    settings = make_settings(
        enable_real_booking=True, pause_file=str(booking_cfg.parent / "nope.flag")
    )
    rc = m.cmd_book(_book_args(booking_cfg), settings, _logger())
    out = capsys.readouterr().out

    assert rc == 3, out
    assert len(submits) == 1  # 认证失效后绝不重试提交
    assert "HAUT_AUTHORIZATION" in out


def _invalid_check():
    from haut_seat.core.auth import AuthCheck

    return AuthCheck(AuthStatus.INVALID, "服务器返回认证失败 code=401")


def _indeterminate_check():
    from haut_seat.core.auth import AuthCheck

    return AuthCheck(AuthStatus.INDETERMINATE, "网络异常（非认证失效）")


def _valid_check():
    from haut_seat.core.auth import AuthCheck

    return AuthCheck(AuthStatus.VALID, "登录有效", seat_count=70)


def _empty_result():
    from haut_seat.models.seat import LayoutQueryResult

    return LayoutQueryResult(layout=[], seats=[], raw={"code": 200, "seatList": []})


# ---------------------------------------------------------------- CLI check-auth


def test_check_auth_cli_valid(monkeypatch, make_settings, capsys):
    import haut_seat.__main__ as m

    monkeypatch.setattr(m, "_auth_precheck", lambda *a, **k: _valid_check())
    args = argparse.Namespace(regionid=None, start="", end="")
    rc = m.cmd_check_auth(args, make_settings(), __import__("logging").getLogger("test"))
    out = capsys.readouterr().out

    assert rc == 0
    assert "登录有效" in out
    assert "未提交任何预约" in out


def test_check_auth_cli_invalid_prints_relogin_steps(monkeypatch, make_settings, capsys):
    import haut_seat.__main__ as m

    monkeypatch.setattr(m, "_auth_precheck", lambda *a, **k: _invalid_check())
    args = argparse.Namespace(regionid=None, start="", end="")
    rc = m.cmd_check_auth(args, make_settings(), __import__("logging").getLogger("test"))
    out = capsys.readouterr().out

    assert rc == 3
    assert "HAUT_AUTHORIZATION" in out
    assert "check-auth" in out


def test_check_auth_cli_403_output_pins_the_only_distinguishing_line(
    monkeypatch, make_settings, capsys
):
    """HTTP 403 时 CLI 到底打印什么 —— 钉住那两行可用来分辨的信号。

    排查「持续 403」时唯一能区分两种情况的，就是这两行：
    ``HTTP 状态: 403``（HTTP 层 403，客户端**不看响应体**）
    与 ``业务 code: 403``（HTTP 200，应用在 JSON 里说自己拒绝）。

    结论都是 ``invalid``（保守停止流程），但**措辞不得断言令牌过期**：
    站点自己的错误表把 403 记为「当前操作没有权限」，403 也可能是网关/WAF。
    因此这里同时钉住"必须原样保留 HTTP 状态"与"必须明说不断言过期"。
    """
    import argparse
    import logging

    import haut_seat.__main__ as m

    check_403 = classify_exception(AuthenticationError(403))
    monkeypatch.setattr(m, "_auth_precheck", lambda *a, **k: check_403)
    args = argparse.Namespace(regionid=None, start="", end="")

    rc = m.cmd_check_auth(args, make_settings(), logging.getLogger("test"))
    out = capsys.readouterr().out

    assert rc == 3
    assert "登录预检: invalid" in out
    assert "HTTP 状态: 403" in out          # ← HTTP 层 403 的判据
    assert AUTH_403_NOTE in out             # ← 站点原文的 403 语义
    assert "不据此断言令牌过期" in out       # ← 不得把 403 讲成"令牌死了"
    assert "HAUT_AUTHORIZATION" in out      # 仍会引导你去重新登录
    assert "SYNTHETIC" not in out           # 仍然不含任何凭据值


def test_check_auth_cli_403_business_code_is_a_different_signal(
    monkeypatch, make_settings, capsys
):
    """HTTP 200 + 业务 code=403（应用层拒绝）与 HTTP 层 403 的输出**不同**：
    此时 ``HTTP 状态`` 是 200，识别的关键是 ``业务 code``。"""
    import argparse
    import logging

    import haut_seat.__main__ as m

    check_business_403 = classify_body({"code": 403, "msg": "无权限"})
    monkeypatch.setattr(m, "_auth_precheck", lambda *a, **k: check_business_403)
    args = argparse.Namespace(regionid=None, start="", end="")

    rc = m.cmd_check_auth(args, make_settings(), logging.getLogger("test"))
    out = capsys.readouterr().out

    assert rc == 3
    assert "业务 code: 403" in out
    assert "HTTP 状态: 403" not in out      # 这一路不会出现 HTTP 层 403


def test_check_auth_cli_indeterminate_returns_1(monkeypatch, make_settings, capsys):
    import haut_seat.__main__ as m

    monkeypatch.setattr(m, "_auth_precheck", lambda *a, **k: _indeterminate_check())
    args = argparse.Namespace(regionid=None, start="", end="")
    rc = m.cmd_check_auth(args, make_settings(), __import__("logging").getLogger("test"))
    out = capsys.readouterr().out

    assert rc == 1
    assert "不是认证失效判定" in out


def test_check_auth_cli_never_prints_credential_values(make_settings, capsys):
    """凭据只显示配置状态，绝不显示值。"""
    import haut_seat.__main__ as m

    orig = m._auth_precheck
    m._auth_precheck = lambda *a, **k: _valid_check()  # type: ignore[attr-defined]
    try:
        args = argparse.Namespace(regionid=None, start="", end="")
        settings = make_settings(
            cookie="Admin-Token=SECRETCOOKIE", authorization="Bearer SECRETTOKEN"
        )
        m.cmd_check_auth(args, settings, __import__("logging").getLogger("test"))
    finally:
        m._auth_precheck = orig  # type: ignore[assignment]
    out = capsys.readouterr().out
    assert "SECRETCOOKIE" not in out
    assert "SECRETTOKEN" not in out
    assert "已配置(值不显示)" in out


def test_check_auth_subparser_registered():
    from haut_seat.__main__ import build_parser, cmd_check_auth

    a = build_parser().parse_args(["check-auth"])
    assert getattr(a, "func", None) is cmd_check_auth


def test_no_test_in_this_module_sends_real_requests():
    """守卫：本测试模块只使用 MockTransport，不构造真实网络客户端。"""
    text = Path(__file__).read_text(encoding="utf-8")
    assert "MockTransport" in text
    # 不允许出现真实基地址（避免误发真实请求）；此处按片段拼装，避免自匹配
    real_host = "wslib" + ".haut" + ".edu.cn"
    assert real_host not in text