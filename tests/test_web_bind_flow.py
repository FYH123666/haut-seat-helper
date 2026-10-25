"""用户绑定自己的图书馆账号（``/booking/bind``）的端到端测试。

**全部离线**：``httpx.MockTransport`` 顶替传输层，于是 ``school_login`` 与
``io.auth_precheck`` **都是真货**，只有字节没有真的出网 —— 断言的是真实发出去的
请求体（账号 / 口令 / 验证码 / uuid），而不是测试自己拼出来的假客户端。

覆盖需求点名的几条：
- 一次提交只发**一个**登录请求：验证码失败**绝不自动重试**（这是红线，不是优化）；
- 学校口令出现在哪个请求体里、绝不出现在别处（响应体、日志、重定向、跳转地址）；
- 成功 / 失败 / 探测不确定三种结局如实分开，不粉饰；
- 绑定成功后该用户的「等待认证」计划被放回待执行；
- 失败过多时按用户限流，且提示里带着"必须由你本人重新看一张新验证码"。
"""

from __future__ import annotations

import base64
import logging
import re
from urllib.parse import unquote

import httpx
import pytest

from haut_seat.services import school_login, user_credentials as uc
from tests.user_end import ORIGIN, bind_credentials, login_user, page_csrf

#: 一张**通过形状校验**的极小 JPEG（base64）。它不是真验证码，只是让
#: ``_sanitize_captcha_image`` 的校验路径真的被走到：base64 字符集、解码成功、
#: 解码后长度 ≥64 且 ≤ 上限（这四条任何一条不过，站点返回的"图片"就会被拒绝）。
CAPTCHA_BYTES = b"\xff\xd8\xff\xe0" + b"\x11" * 200 + b"\xff\xd9"
CAPTCHA_B64 = base64.b64encode(CAPTCHA_BYTES).decode("ascii")
#: 页面上隐藏字段与 ``<img src>`` 用的是**同一条** data URI（见 ``_sanitize_captcha_image``
#: 的注释）：浏览器把页面原样回填，因此"带着同一张图重新提交"看的就是这个值。
CAPTCHA_DATA_URI = "data:image/jpeg;base64," + CAPTCHA_B64
CAPTCHA_UUID = "uuid-for-tests-0001"

SCHOOL_USER = "2023000000"
SCHOOL_PASSWORD = "school-password-sentinel-42"
TOKEN = "eyJhbGciOiJIUzUxMiJ9." + "BINDTESTSENTINEL" + ".BINDSIG"

_B64_RE = re.compile(r"^data:image/[a-z]+;base64,([A-Za-z0-9+/=]+)$")


class FakeSchool:
    """把学校侧的端点接在 MockTransport 上（验证码 / 登录 / 两个只读探测）。"""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.bodies: list[dict] = []
        self.login_response: object = {
            "code": 200, "msg": "操作成功", "token": TOKEN,
        }
        self.login_status = 200
        self.login_raises: Exception | None = None
        #: **凭据判据**：业务身份接口 ``GET /stage-api/getInfo`` 的响应体。
        #: 实测它能区分「凭据被接受」（code=200 + user/roles）与「未被接受」（code=401），
        #: 因此默认态就是"这份凭据可用"。
        self.identity_body: object = {
            "code": 200, "msg": "成功",
            "user": {"userId": 1, "userName": "测试用户"}, "roles": ["common"],
        }
        #: 第二意见：只读座位查询（``layout/query``）的响应体。它**不能**验证凭据
        #: （匿名同样放行），所以只有在身份接口没给出确定结论时才会被用到。
        self.probe_body: object = {"code": 200, "msg": "成功", "layout": [], "seatList": [{"id": 1}]}

    def set_auth_response(self, body: object) -> None:
        """让"凭据验证"的两个只读端点都返回这个响应体。

        构造"验证失败/无法确认"的场景必须**两个一起**改：身份接口一旦给出确定结论，
        第二个探测就不会被发出去，只改 ``probe_body`` 会得到与预期相反的结果。
        """
        self.identity_body = body
        self.probe_body = body

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        path = request.url.path
        if path == school_login.CAPTCHA_PATH:
            return httpx.Response(200, json={
                "code": 200, "msg": "操作成功", "uuid": CAPTCHA_UUID,
                "img": CAPTCHA_B64,
            })
        if path == school_login.LOGIN_PATH:
            import json

            self.bodies.append(json.loads(request.content.decode("utf-8")))
            if self.login_raises is not None:
                raise self.login_raises
            return httpx.Response(self.login_status, json=self.login_response)
        if path == "/stage-api/getInfo":
            return httpx.Response(200, json=self.identity_body)
        return httpx.Response(200, json=self.probe_body)

    def login_calls(self) -> int:
        return sum(1 for r in self.seen if r.url.path == school_login.LOGIN_PATH)

    def probe_calls(self) -> int:
        return sum(1 for r in self.seen if r.url.path not in
                   (school_login.LOGIN_PATH, school_login.CAPTCHA_PATH))


@pytest.fixture
def fake_school(monkeypatch):
    """假学校端点。

    **在 TestClient 建好之后才安装**（见 ``bind_env``）：``starlette`` 的
    TestClient 本身就是 ``httpx.Client`` 的子类，先打补丁会让它继承了我们的替身函数，
    TestClient 直接建不起来。
    """
    return FakeSchool()


@pytest.fixture
def bind_env(make_client, web_settings, web_db, fake_school):
    """``(client, db, settings, school, user_id)``：已登录、未绑定的用户 + 假学校。

    传输层补丁**在 TestClient 建好之后**才装上（``starlette`` 的 TestClient 本身
    就是 ``httpx.Client`` 的子类，先打补丁会让它继承我们的替身函数，连客户端都建不起来），
    并在用例结束时**还原**，绝不把这层替身泄漏给后面的用例。
    """
    settings = web_settings.model_copy(update={"min_interval_s": 0.01})
    _app, client = make_client(settings=settings, db=web_db)

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake_school._handle)
        return real_client(*args, **kwargs)

    httpx.Client = fake_client  # type: ignore[assignment]
    try:
        from tests.user_end import create_user

        user_id = create_user(web_db)
        login_user(client, web_db)
        yield client, web_db, settings, fake_school, user_id
    finally:
        httpx.Client = real_client  # type: ignore[assignment]


def _bind(client, *, school_username=SCHOOL_USER, password=SCHOOL_PASSWORD,
          code="1234", image=""):
    token = page_csrf(client, "/booking/bind")
    return client.post(
        "/booking/bind",
        data={
            "_csrf": token, "school_username": school_username,
            "password": password, "code": code, "captcha_image": image,
        },
        headers={"Origin": ORIGIN},
    )


# ================================================================== 1. 取验证码


def test_bind_page_relays_the_school_captcha_as_a_data_uri(bind_env):
    """绑定页把学校返回的验证码图片**内联**渲染（CSP 只放行 ``data:``），且不转发 uuid。"""
    client, _db, settings, school, _uid = bind_env
    page = client.get("/booking/bind")
    assert page.status_code == 200
    match = re.search(r'<img src="(data:image/[^"]+)"', page.text)
    assert match, "页面上没有内联的验证码图片"
    payload = _B64_RE.match(match.group(1))
    assert payload and payload.group(1) == CAPTCHA_B64
    # uuid 是服务端会话里的东西，不该出现在页面里（浏览器不需要它，也不需要能改它）。
    assert CAPTCHA_UUID not in page.text
    assert school.seen[-1].url.path == school_login.CAPTCHA_PATH
    # 取验证码这一次**绝不能**顺带发登录请求。
    assert school.login_calls() == 0


def test_bind_page_says_so_when_captcha_is_unavailable(
    make_client, web_settings, web_db, monkeypatch
):
    """取不到验证码时**如实说明**，不画占位图，也不留一个用不了的 uuid。"""
    settings = web_settings.model_copy(update={"min_interval_s": 0.01})
    _app, client = make_client(settings=settings, db=web_db)
    from tests.user_end import create_user

    create_user(web_db)
    login_user(client, web_db)

    def boom(settings_, **kwargs):
        raise school_login.SchoolLoginError("取验证码失败：TimeoutException")

    monkeypatch.setattr(school_login, "fetch_captcha", boom)
    page = client.get("/booking/bind")
    assert page.status_code == 200
    assert "TimeoutException" in page.text
    assert "<img src=\"data:image" not in page.text
    # 表格仍然在（用户可以重试），但没有可提交的验证码。
    assert "再取一张验证码" in page.text


# ================================================================== 2. 单次提交


def test_successful_bind_saves_credentials_and_shows_metadata(bind_env):
    """登录成功 + 只读探测有效 ⇒ 存下凭据、状态「有效」、页面上只有标识与指纹。"""
    client, db, _settings, school, user_id = bind_env
    resp = _bind(client)
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"].startswith("/booking?")
    assert "绑定成功" in unquote(resp.headers["location"])
    # 重定向地址里绝不能带令牌。
    assert TOKEN not in resp.headers["location"]
    assert "Admin-Token" not in resp.headers["location"]

    assert school.login_calls() == 1, "一次提交只允许一个登录请求"
    body = school.bodies[0]
    assert body == {
        "username": SCHOOL_USER, "password": SCHOOL_PASSWORD,
        "code": "1234", "uuid": CAPTCHA_UUID,
    }

    view = uc.status_view(db, user_id)
    assert view["bound"] is True
    assert view["status"] == uc.CRED_STATUS_VALID
    assert view["school_username"] == SCHOOL_USER
    assert view["fingerprint"]

    page = client.get("/booking").text
    assert SCHOOL_USER in page
    assert "有效" in page
    # 凭据本体一个字节都不许进页面。
    assert TOKEN not in page
    assert SCHOOL_PASSWORD not in page
    assert "Admin-Token" not in page


def test_wrong_captcha_fails_once_and_never_retries(bind_env):
    """验证码错 ⇒ 只发一个请求、如实报错、**不自动重试**，并给一张新验证码。"""
    client, db, _settings, school, user_id = bind_env
    school.login_response = {"code": 500, "msg": "验证码错误"}
    resp = _bind(client)
    assert resp.status_code == 200, "失败必须原样回显，而不是 303 假装成功"
    assert "验证码错误" in resp.text
    assert school.login_calls() == 1, "验证码失败后程序自动重试了 —— 这是红线"
    assert uc.has_credentials(db, user_id) is False, "失败的绑定不得存下任何凭据"

    # 失败后页面上是一张**新**验证码（会话里那张已作废）。
    match = re.search(r'<img src="data:image/[^"]+"', resp.text)
    assert match, "失败后没有给出新的验证码"
    assert school.seen[-1].url.path == school_login.CAPTCHA_PATH


def test_bind_never_saves_credentials_when_the_probe_is_indeterminate(bind_env):
    """登录成功但只读探测无法确认 ⇒ 如实标「待确认」，绝不冒充「有效」。"""
    client, db, _settings, school, user_id = bind_env
    school.set_auth_response({"code": 500, "msg": "服务暂时不可用"})
    resp = _bind(client)
    assert resp.status_code == 303
    assert "未能确认可用" in unquote(resp.headers["location"])
    view = uc.status_view(db, user_id)
    assert view["status"] == uc.CRED_STATUS_INDETERMINATE
    # 凭据本身仍然存下来了（登录确实成功了），只是状态不粉饰。
    assert view["bound"] is True


def test_bind_marks_invalid_when_the_token_is_rejected(bind_env):
    """登录接口返回 token，但带着它做只读探测被判定认证失效 ⇒ 状态「已失效」。"""
    client, db, _settings, school, user_id = bind_env
    school.set_auth_response({"code": 401, "msg": "认证失败，无法访问系统资源"})
    resp = _bind(client)
    assert resp.status_code == 303
    view = uc.status_view(db, user_id)
    assert view["status"] == uc.CRED_STATUS_INVALID


def test_network_failure_does_not_touch_existing_credentials(bind_env):
    """出站异常（超时）：如实报错，**不改动**已经保存好的凭据。"""
    client, db, settings, school, user_id = bind_env
    bind_credentials(db, settings, user_id, school_username="old-account")

    school.login_raises = httpx.ConnectTimeout("boom")
    resp = _bind(client)
    assert resp.status_code == 200
    view = uc.status_view(db, user_id)
    assert view["school_username"] == "old-account", "出站失败不该把旧凭据洗掉"


def test_local_field_check_does_not_call_the_school_at_all(bind_env):
    """缺账号 / 口令 / 验证码 ⇒ 本地就挡下，**一个请求都不发**。

    表单里带着同一张图片（真实浏览器就是这样回填 ``captcha_image`` 的），因此重新渲染
    时连验证码都不该重取 —— 用户看到的还是他刚在输的那张；本地校验失败更不该发登录请求。
    这里不经 ``_bind``（它每次都会 GET 页面、即重取一张验证码），而是**先取一次令牌**，
    再连续提交三次：这样"没有多打一个学校接口"才是可断言的。
    """
    from haut_seat.web import user_auth

    client, db, _settings, school, _uid = bind_env
    token = page_csrf(client, "/booking/bind")  # 先记一张有效验证码进会话
    token_hash = user_auth.load_user_session(
        db, client.cookies.get(user_auth.USER_SESSION_COOKIE)
    ).token_hash
    before = len(school.seen)
    assert before >= 1, "取验证码这一下必然要打一次接口"

    for overrides in ({"school_username": ""}, {"password": ""}, {"code": ""}):
        resp = client.post(
            "/booking/bind",
            data={
                "_csrf": token, "school_username": SCHOOL_USER,
                "password": SCHOOL_PASSWORD, "code": "1234",
                "captcha_image": CAPTCHA_DATA_URI,
                **overrides,
            },
            headers={"Origin": ORIGIN},
        )
        assert resp.status_code == 200, overrides
        assert "请填写学校账号、口令与验证码" in resp.text, overrides
        # 本地挡下时连图片都不换：会话里那张验证码**没被消费**，也**没被换掉**。
        session = user_auth.load_user_session(
            db, client.cookies.get(user_auth.USER_SESSION_COOKIE)
        )
        assert session.pending_captcha_uuid == CAPTCHA_UUID, overrides

    assert len(school.seen) == before, "本地校验失败不该打学校接口"
    assert school.login_calls() == 0
    assert token_hash


def test_expired_captcha_is_refused_and_replaced(bind_env, monkeypatch):
    """会话里那张验证码过期 ⇒ 拒绝提交，并自动取一张新的（不拿旧 uuid 去撞）。"""
    client, db, _settings, school, user_id = bind_env
    page_csrf(client, "/booking/bind")  # 取一张，记进会话
    from haut_seat.web import user_auth

    session_token = client.cookies.get(user_auth.USER_SESSION_COOKIE)
    monkeypatch.setattr(user_auth, "pending_captcha_fresh", lambda *a, **k: False)

    resp = _bind(client, image=CAPTCHA_B64)
    assert resp.status_code == 200
    assert "验证码已过期" in resp.text
    assert school.login_calls() == 0, "用一张已知过期的验证码去提交是没意义的"
    assert session_token


# ================================================================== 3. 失败限流


def test_repeated_failures_are_throttled_per_user(bind_env):
    """连续失败到阈值后按**用户**限流，且提示里写清"要你本人重新看新验证码"。"""
    client, _db, _settings, school, _uid = bind_env
    school.login_response = {"code": 500, "msg": "验证码错误"}

    allowed = 0
    for _ in range(uc.BIND_MAX_ATTEMPTS + 3):
        resp = _bind(client)
        if "绑定尝试过于频繁" in resp.text:
            break
        allowed += 1
    assert allowed == uc.BIND_MAX_ATTEMPTS, f"限流阈值不对：放过了 {allowed} 次"
    assert school.login_calls() == uc.BIND_MAX_ATTEMPTS
    assert "程序不会自动重试" in resp.text


def test_bind_attempts_do_not_pollute_the_auth_event_log(bind_env):
    """限流计数用的行**不**出现在用户的认证事件里（那是给用户看的历史）。"""
    from haut_seat.web import repository as repo

    client, db, _settings, school, user_id = bind_env
    school.login_response = {"code": 500, "msg": "验证码错误"}
    _bind(client)

    assert repo.list_user_auth_events(db, user_id) == []
    # 但计数确实记下了 —— 否则限流就是假的。直接查库确认那一行用的是 attempt 来源。
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM user_auth_event WHERE user_id = ? AND source = ?",
        (user_id, uc.BIND_ATTEMPT_SOURCE),
    )
    assert int(row["n"]) == 1, "限流计数没有被记下来，阈值永远到不了"


# ================================================================== 4. 解绑与复验


def test_unbind_removes_credentials_and_keeps_plans(bind_env):
    client, db, settings, _school, user_id = bind_env
    bind_credentials(db, settings, user_id)
    assert uc.has_credentials(db, user_id) is True

    token = page_csrf(client, "/booking/bind")
    resp = client.post("/booking/unbind", data={"_csrf": token}, headers={"Origin": ORIGIN})
    assert resp.status_code == 303
    assert uc.has_credentials(db, user_id) is False
    view = uc.status_view(db, user_id)
    assert view["bound"] is False


def test_unbind_does_not_delete_plans(bind_env):
    from haut_seat.web import repository as repo

    client, db, settings, _school, user_id = bind_env
    bind_credentials(db, settings, user_id)
    token = page_csrf(client, "/booking/new")
    import tests.test_web_booking as booking_tests

    form = booking_tests.plan_form(name="解绑前建的")
    form["_csrf"] = token
    assert client.post("/booking/new", data=form, headers={"Origin": ORIGIN}).status_code == 303

    token = page_csrf(client, "/booking/bind")
    client.post("/booking/unbind", data={"_csrf": token}, headers={"Origin": ORIGIN})
    assert len(repo.list_plans_for_user(db, user_id)) == 1, "解绑不该删掉他的计划"


def test_verify_updates_the_status_honestly(bind_env):
    """「重新验证」只做只读探测：有效 → 有效；被拒 → 已失效。"""
    client, db, settings, school, user_id = bind_env
    bind_credentials(db, settings, user_id, status=uc.CRED_STATUS_INVALID)

    resp = client.post("/booking/verify",
                       data={"_csrf": page_csrf(client, "/booking")},
                       headers={"Origin": ORIGIN})
    assert resp.status_code == 303
    assert uc.credentials_status(db, user_id) == uc.CRED_STATUS_VALID
    assert school.probe_calls() >= 1, "重新验证必须真的探测一次，而不是改个字段"

    school.set_auth_response({"code": 401, "msg": "认证失败"})
    client.post("/booking/verify",
                data={"_csrf": page_csrf(client, "/booking")}, headers={"Origin": ORIGIN})
    assert uc.credentials_status(db, user_id) == uc.CRED_STATUS_INVALID


def test_verify_without_credentials_redirects_with_a_clear_message(bind_env):
    client, _db, _settings, _school, _uid = bind_env
    resp = client.post("/booking/verify",
                       data={"_csrf": page_csrf(client, "/booking")},
                       headers={"Origin": ORIGIN})
    assert resp.status_code == 303
    assert "尚未绑定" in unquote(resp.headers["location"])


# ================================================================== 5. 成功后的恢复


def test_successful_rebind_puts_owned_awaiting_plans_back_to_pending(bind_env):
    """重新绑定成功后，**该用户**处于「等待认证」的计划被放回待执行（且只动他的）。

    计划必须**处于启用状态**才算数：``resume_owned_plans`` 只认"启用 + 等待认证"的计划，
    一个被用户自己暂停的计划不该因为重新绑定就被偷偷武装起来。所以这里先走真实的
    ``/booking/{id}/enable`` 把它启用，再置成「等待认证」。
    """
    from haut_seat.services.state import PlanStatus
    from haut_seat.web import repository as repo
    from tests.test_web_booking import bpost, plan_form

    client, db, settings, _school, user_id = bind_env

    form = plan_form(name="等待认证的")
    form["_csrf"] = page_csrf(client, "/booking/new")
    resp = client.post("/booking/new", data=form, headers={"Origin": ORIGIN})
    plan_id = int(re.search(r"/booking/(\d+)", resp.headers["location"]).group(1))

    # 暂停的计划不该被"重新绑定"悄悄武装：先记下这条，稍后另行断言。
    paused = plan_form(name="用户自己暂停的")
    paused["_csrf"] = page_csrf(client, "/booking/new")
    paused_id = int(re.search(
        r"/booking/(\d+)",
        client.post("/booking/new", data=paused, headers={"Origin": ORIGIN}).headers["location"],
    ).group(1))

    token = page_csrf(client, f"/booking/{plan_id}")
    assert bpost(client, f"/booking/{plan_id}/enable", token=token).status_code == 303
    repo.set_plan_status(db, plan_id, PlanStatus.AWAITING_AUTH)
    repo.set_plan_status(db, paused_id, PlanStatus.AWAITING_AUTH)

    assert _bind(client).status_code == 303
    assert repo.get_plan(db, plan_id)["status"] is PlanStatus.PENDING, (
        "重新绑定成功后，该用户处于「等待认证」的计划应被放回待执行"
    )
    assert repo.get_plan(db, paused_id)["status"] is PlanStatus.AWAITING_AUTH, (
        "用户自己暂停的计划不该因为一次重新绑定就被放回待执行"
    )


# ================================================================== 6. 口令不出现在任何地方


def test_school_password_never_reaches_logs_or_pages(bind_env, caplog):
    """学校口令只在那一个请求体里：日志、页面、重定向都不出现它。"""
    client, _db, _settings, school, _uid = bind_env
    with caplog.at_level(logging.DEBUG, logger="haut_seat"):
        resp = _bind(client)
    assert SCHOOL_PASSWORD in school.bodies[0]["password"]
    assert SCHOOL_PASSWORD not in caplog.text
    assert SCHOOL_PASSWORD not in resp.text
    assert SCHOOL_PASSWORD not in resp.headers.get("location", "")
    # 成功页与绑定页也不含它。
    for path in ("/booking", "/booking/bind"):
        assert SCHOOL_PASSWORD not in client.get(path).text