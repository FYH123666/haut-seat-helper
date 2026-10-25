"""远程浏览器在 Web 层的测试：路由、页面、权限与会话隔离。

**全离线**：管理器注入假后端（复用 ``tests/test_remote_browser.py`` 的替身）、
只读预检注入假实现，因此本模块不装浏览器、不开浏览器、不发任何网络请求，
也不出现真实学校域名。

任务点名的五条要求对应在：

1. 未登录不能用 —— ``test_every_remote_endpoint_refuses_an_unauthenticated_visitor``
   （并且**管理器一个方法都没被碰**，用记录型替身证明）；
2. 非管理员不能用 —— ``test_a_booking_user_session_cannot_reach_the_remote_endpoints``
   （本仓库"非管理员"的真实形态是用户端 ``/booking`` 会话）；
3. 管理员可以创建 / 查看 / 结束**自己的**会话 —— 属主那几条；
4. 不同用户之间的浏览器会话与凭据互不影响 —— 别人的会话拿 404 / ``ok=0``，
   且远程采集后 ``user_credential`` 表逐字节不变；
5. 凭据不泄露 —— 页面 / ``/state`` JSON / 303 的 Location / 日志 / 模板，全程用哨兵盯住。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import pytest

from haut_seat.services import auth_browser
from haut_seat.services import remote_browser as rb
from haut_seat.services.auth_browser import COOKIE_NAME, BrowserLaunchError
from haut_seat.services.auth_login_service import LOGIN_AWAITING

from tests.test_remote_browser import FakeBackend, FakeSession, sentinel_precheck
from tests.test_web_static import js_code_only
from tests.user_end import (
    FAKE_SCHOOL_AUTHORIZATION,
    FAKE_SCHOOL_COOKIE,
    bind_credentials,
    create_user,
    login_user,
)

#: 哨兵：形状像真令牌，但显然是测试专用值。
SENTINEL = "SENTINELTOKEN-._0002-abcdefghijklmnop"
GOOD_PASSWORD = "Correct-Horse-9"
ORIGIN = "http://testserver"

WEB_DIR = Path(__file__).resolve().parent.parent / "haut_seat" / "web"
TEMPLATE = WEB_DIR / "templates" / "remote_browser.html"
SCRIPT = WEB_DIR / "static" / "remote_browser.js"

_CSRF_RE = re.compile(r'name="_csrf"\s+value="([^"]+)"')

#: 需要"登录"的远程端点（未登录时都该被 ``require_admin`` 挡掉）。
ENDPOINTS = (
    "/admin/auth/browser",
    "/admin/auth/browser/frame",
    "/admin/auth/browser/state",
)

#: 未登录时**跳登录页**的那两个。``/state`` 走 ``optional_admin``，给的是 401 JSON
#: （303 会让 fetch 拿到登录页 HTML，把"会话过期"这个真实原因掩盖掉）。
REDIRECTING_ENDPOINTS = ENDPOINTS[:2]


# ====================================================================== 小工具


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def csrf_of(html: str) -> str:
    """取出页面里的 CSRF 令牌（``base.html`` 的退出表单保证每个管理页都有）。"""
    match = _CSRF_RE.search(html)
    assert match, "页面没有渲染 CSRF 隐藏字段"
    return match.group(1)


def wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.01):
    """等一个条件成立（轮询）。超时即失败，绝不静默放过。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("等待条件超时：远程浏览器会话没有进入预期状态")


class RecordingManager:
    """只记录调用的替身：用来证明"没登录时路由根本没碰管理器"。

    ``shutdown`` 存在但**不记录** —— ``create_app`` 的 lifespan 收尾会调用它，
    它不是任何一个端点触发的动作，记进来会污染"端点没碰管理器"这个断言。
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def shutdown(self, **kwargs) -> None:
        pass

    def _no(self, name):
        self.calls.append(name)

    def view_for(self, *a, **k):
        self._no("view_for")
        return None

    def idle_view(self, *a, **k):
        self._no("idle_view")
        return {}

    def state_view(self, *a, **k):
        self._no("state_view")
        return {}

    def frame_for(self, *a, **k):
        self._no("frame_for")
        return None

    def submit_input(self, *a, **k):
        self._no("submit_input")
        return {"ok": False, "owner": False, "queued": 0, "state": "idle"}

    def start(self, **k):
        self._no("start")
        return {"ok": False, "message": "替身：不启动"}

    def stop(self, **k):
        self._no("stop")
        return {"ok": False, "message": "替身：不停"}


class Panel:
    """一个管理端应用 + 它那台假浏览器，以及登录好的管理员客户端。

    测试里**不直接往库里塞会话**：走的是真实的 ``/admin/login`` 表单，
    否则"属主校验"会自动为真，那几条隔离用例就变成假绿。
    """

    def __init__(self, app, db, manager, backend) -> None:
        self.app = app
        self.db = db
        self.mgr = manager
        self.backend = backend          # 假后端：用例用它核对假页面上的调用
        self._clients: dict[str, object] = {}

    @property
    def client(self):
        """``panel`` 夹具已经建好的那个管理员客户端（"我自己"）。"""
        assert "tester" in self._clients, "请先用 panel.admin() 登录一个管理员"
        return self._clients["tester"]

    def admin(self, username: str = "tester"):
        from fastapi.testclient import TestClient

        from haut_seat.web import security

        security.create_admin(self.db, username, GOOD_PASSWORD)
        client = TestClient(self.app, follow_redirects=False)
        resp = client.post("/admin/login",
                           data={"username": username, "password": GOOD_PASSWORD})
        assert resp.status_code == 303, resp.text
        self._clients[username] = client
        return client

    def ids(self, username: str = "tester") -> tuple[int, str]:
        """会话归属的二元组：``(admin_id, 会话令牌的 sha256)``。"""
        from haut_seat.web import security

        row = security.find_admin(self.db, username)
        assert row is not None, f"库里没有管理员 {username}"
        token = self._clients[username].cookies.get(security.SESSION_COOKIE)
        assert token, "管理员会话 Cookie 不见了"
        return int(row["id"]), security.token_digest(token)

    def start(self, client, *, expect_ok: bool = True) -> str:
        """走真实的开始表单（带 CSRF），返回 Location。"""
        page = client.get("/admin/auth/browser")
        assert page.status_code == 200
        resp = client.post("/admin/auth/browser/start", data={"_csrf": csrf_of(page.text)})
        assert resp.status_code == 303, resp.text
        location = resp.headers["location"]
        assert location.startswith("/admin/auth/browser?"), location
        assert ("ok=1" in location) is expect_ok, location
        return location

    def stop(self, client) -> str:
        page = client.get("/admin/auth/browser")
        resp = client.post("/admin/auth/browser/stop", data={"_csrf": csrf_of(page.text)})
        assert resp.status_code == 303, resp.text
        return resp.headers["location"]

    def input(self, client, events, *, token=None, origin=ORIGIN):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-CSRF-Token"] = token
        if origin is not None:
            headers["Origin"] = origin
        return client.post("/admin/auth/browser/input", json={"events": events},
                           headers=headers)


# ====================================================================== 夹具


@pytest.fixture(autouse=True)
def _clear_availability_cache():
    """``browser_availability`` 带缓存；每个用例前后都清掉。"""
    auth_browser.browser_availability.cache_clear()
    yield
    auth_browser.browser_availability.cache_clear()


@pytest.fixture
def remote_settings(web_settings, tmp_path):
    """远程浏览器专用配置：凭据落在临时目录、快节拍、短超时。

    ``web_db`` 刻意**不覆盖**：``tests.conftest.web_db`` 是按 ``web_settings``
    建的，改了路径就会出现"路由写 A 库、用例查 B 库"的假绿。
    """

    def _make(**overrides):
        base = {
            "auth_source": "auto",
            "auth_dir": str(tmp_path / "auth"),
            "auth_snapshot_file": str(tmp_path / "auth" / "credentials.json"),
            "auth_profile_dir": str(tmp_path / "auth" / "browser-profile"),
            "auth_remote_frame_interval_ms": 100,
            "auth_remote_idle_timeout_s": 30.0,
            "auth_login_timeout_s": 60.0,
        }
        base.update(overrides)
        return web_settings.model_copy(update=base)

    return _make


@pytest.fixture
def browser_factory(remote_settings):
    """构造（真实的）管理器 + 假后端的工厂；结束时保证线程被收掉。

    ``precheck`` 默认注入假实现：真实预检会向 ``base_url`` 发一次 HTTP 请求，
    而本模块的底线是**零网络**。
    """
    created = []

    def _make(*, settings=None, db=None, session=None, error=None, precheck="default"):
        effective = settings or remote_settings()
        backend = FakeBackend(
            session if session is not None else FakeSession(token=None), error=error
        )
        mgr = rb.RemoteBrowserManager(
            effective, db, logging.getLogger("haut_seat.test.web.remote"),
            backend=backend,
            precheck=(sentinel_precheck() if precheck == "default" else precheck),
        )
        created.append(mgr)
        return mgr, backend

    yield _make
    for mgr in created:
        mgr.shutdown(timeout_s=2.0)


@pytest.fixture
def panel_factory(make_client, remote_settings, web_db, browser_factory):
    """造一个装了假浏览器的管理端应用（``Panel``）。"""
    created = []

    def _make(*, session=None, error=None, precheck="default", **overrides):
        settings = remote_settings(**overrides)
        app, _probe = make_client(settings=settings, db=web_db)
        mgr, backend = browser_factory(settings=settings, db=web_db, session=session,
                                       error=error, precheck=precheck)
        app.state.remote_browser = mgr
        panel = Panel(app, web_db, mgr, backend)
        created.append(panel)
        return panel

    yield _make
    for item in created:
        item.mgr.shutdown(timeout_s=2.0)


@pytest.fixture
def panel(panel_factory):
    """已登录管理员 + 假浏览器。"""
    item = panel_factory()
    item.admin()
    return item


# ====================================================================== 未登录


def test_every_remote_endpoint_refuses_an_unauthenticated_visitor(make_client):
    """未登录：GET 跳登录页（``/state`` 例外，见下一条）、POST 401，
    且**管理器一个方法都没被碰**。"""
    recorder = RecordingManager()
    _app, client = make_client(remote_browser=recorder)

    for path in REDIRECTING_ENDPOINTS:
        resp = client.get(path)
        assert resp.status_code == 303, path
        assert resp.headers["location"] == "/admin/login", path
    assert client.get("/admin/auth/browser/state").status_code == 401
    assert client.post("/admin/auth/browser/start").status_code == 401
    assert client.post("/admin/auth/browser/input", json={"events": []}).status_code == 401
    assert client.post("/admin/auth/browser/stop").status_code == 401

    assert recorder.calls == [], "未登录的请求绝不允许碰到远程浏览器管理器"


def test_the_state_endpoint_returns_json_401_not_a_redirect(make_client):
    """给机器读的端点用 401：303 跳转会让 fetch 拿到 HTML、把真正原因掩盖掉。"""
    recorder = RecordingManager()
    _app, client = make_client(remote_browser=recorder)
    resp = client.get("/admin/auth/browser/state")
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/json")
    payload = resp.json()
    assert payload["ok"] is False and payload["expired"] is True
    assert "重新登录" in payload["message"]
    assert recorder.calls == []


# ====================================================================== 非管理员


def test_a_booking_user_session_cannot_reach_the_remote_endpoints(
    make_client, web_settings, web_db
):
    """用户端会话（``/booking`` 的身份）够不到管理端端点，管理器同样一字节不被碰。"""
    recorder = RecordingManager()
    _app, client = make_client(settings=web_settings, db=web_db, remote_browser=recorder)
    create_user(web_db)
    login_user(client, web_db)

    for path in REDIRECTING_ENDPOINTS:
        assert client.get(path).status_code == 303, path
    assert client.get("/admin/auth/browser/state").status_code == 401
    assert client.post("/admin/auth/browser/start").status_code == 401
    assert client.post("/admin/auth/browser/input", json={"events": []}).status_code == 401
    assert client.post("/admin/auth/browser/stop").status_code == 401
    assert recorder.calls == [], "用户端身份绝不允许碰到远程浏览器管理器"


# ====================================================================== 属主


def test_the_owner_can_start_view_and_stop_their_own_session(panel):
    client = panel.client
    assert panel.start(client)
    wait_until(lambda: panel.mgr.has_session())

    page = client.get("/admin/auth/browser")
    assert page.status_code == 200
    assert "远程浏览器登录" in page.text
    assert "/admin/auth/browser/frame" in page.text, "页面必须包含画面元素"

    admin_id, token_hash = panel.ids()
    wait_until(lambda: panel.mgr.frame_for(admin_id, token_hash) is not None)
    frame = client.get("/admin/auth/browser/frame")
    assert frame.status_code == 200
    assert frame.headers["content-type"].startswith("image/jpeg")
    assert frame.content.startswith(b"\xff\xd8"), "返回的必须是真正的 JPEG 字节"

    state = client.get("/admin/auth/browser/state")
    assert state.status_code == 200
    payload = state.json()
    assert payload["ok"] is True and payload["expired"] is False
    assert payload["state"] in (rb.LOGIN_LAUNCHING, rb.LOGIN_AWAITING)
    assert payload["viewport"] == {"width": 1280, "height": 800}
    assert payload["secure"] is False, "TestClient 走的是明文 http"
    assert payload["allow_insecure"] is True
    assert payload["enabled"] is True

    assert "ok=1" in panel.stop(client)
    assert panel.mgr.has_session() is False
    assert panel.backend.session.close_calls == 1, "浏览器必须被关掉"


def test_the_frame_and_state_are_never_cached(panel):
    client = panel.client
    panel.start(client)
    assert client.get("/admin/auth/browser/frame").headers["cache-control"] == "no-store"
    assert client.get("/admin/auth/browser/state").headers["cache-control"] == "no-store"


def test_the_page_widget_points_at_the_real_routes(panel):
    """画面元素里的那三个 URL 必须真的存在 —— 改名时这里会先红。

    直接查 ``routes.router`` 而不是 ``app.routes``：这一版 FastAPI 把
    ``include_router`` 收成一个 ``_IncludedRouter`` 占位对象，``app.routes``
    里看不到各条真实路径（照它断言会得到"路由此不存在"的假结论）。
    """
    from haut_seat.web.routes import router as app_router

    client = panel.client
    panel.start(client)
    wait_until(lambda: panel.mgr.has_session())
    page = client.get("/admin/auth/browser")
    urls = re.findall(r'data-(?:frame|state|input)-url="([^"]+)"', page.text)
    assert len(urls) == 3, urls
    paths = {route.path for route in app_router.routes}
    for url in urls:
        assert url in paths, f"{url} 不是一个真实的路由"


def test_a_204_is_returned_while_there_is_no_frame_yet(panel_factory):
    """浏览器已起来但还没截到第一帧：204 空响应，``<img>`` 会安静地什么都不显示。

    这时状态必须仍是"等待登录"，**不能**把"没有画面"说成失败。
    """
    item = panel_factory(session=FakeSession(token=None, screenshot=None))
    client = item.admin()
    item.start(client)
    wait_until(lambda: item.mgr.has_session())

    frame = client.get("/admin/auth/browser/frame")
    assert frame.status_code == 204
    assert frame.content == b""
    assert client.get("/admin/auth/browser/state").json()["state"] == rb.LOGIN_AWAITING


# ====================================================================== 别的管理员


def test_a_foreign_admin_gets_404_on_frame_state_input_and_stop(panel_factory):
    """会话绑定到发起者：另一个管理员既看不到画面，也发不了输入。"""
    item = panel_factory()
    alice = item.admin("alice")
    bob = item.admin("bob")
    item.start(alice)
    wait_until(lambda: item.mgr.has_session())

    assert bob.get("/admin/auth/browser/frame").status_code == 404
    state = bob.get("/admin/auth/browser/state")
    assert state.status_code == 404
    assert state.json()["expired"] is True

    resp = item.input(bob, [{"t": "k", "k": "a"}],
                      token=csrf_of(bob.get("/admin/auth/browser").text))
    assert resp.status_code == 404, "别人的会话绝不允许接受输入"

    stopped = item.stop(bob)
    assert "ok=0" in stopped, stopped
    assert item.mgr.has_session() is True, "别人的会话绝不能被顺手停掉"

    # 页面按"没有你的会话"渲染：连"有人正在用"都不透露
    page = bob.get("/admin/auth/browser")
    assert 'action="/admin/auth/browser/start"' in page.text
    assert 'action="/admin/auth/browser/stop"' not in page.text

    # 反过来，属主自己看得见
    assert alice.get("/admin/auth/browser/frame").status_code in (200, 204)


def test_a_second_session_of_the_same_admin_cannot_take_over(panel_factory):
    """同一个人重新登录后，旧会话令牌不再能操作 —— 防的是"猜中/偷到 session id"。"""
    from fastapi.testclient import TestClient

    item = panel_factory()
    alice = item.admin("alice")
    item.start(alice)
    wait_until(lambda: item.mgr.has_session())

    again = TestClient(item.app, follow_redirects=False)
    assert again.post("/admin/login",
                      data={"username": "alice", "password": GOOD_PASSWORD}).status_code == 303
    assert again.get("/admin/auth/browser/frame").status_code == 404
    assert again.get("/admin/auth/browser/state").status_code == 404
    assert item.input(again, [{"t": "k", "k": "a"}],
                      token=csrf_of(again.get("/admin/auth/browser").text)).status_code == 404


# ====================================================================== CSRF / 同源 / 体积


def test_start_and_stop_require_csrf(panel):
    client = panel.client
    assert client.post("/admin/auth/browser/start").status_code == 400
    assert client.post("/admin/auth/browser/stop").status_code == 400
    assert panel.mgr.has_session() is False, "CSRF 不过就绝不允许启动"


def test_input_requires_the_csrf_header(panel):
    """``check_csrf`` 只读表单字段，JSON 端点必须自己比这个头。"""
    client = panel.client
    panel.start(client)
    resp = panel.input(client, [])
    assert resp.status_code == 400
    assert "CSRF" in resp.json()["detail"]


def test_input_requires_a_same_origin_request(panel):
    """管理端其它 POST 没有这道；远程端点值得多一层。"""
    client = panel.client
    panel.start(client)
    token = csrf_of(client.get("/admin/auth/browser").text)
    bad = panel.input(client, [], token=token, origin="http://evil.example.invalid")
    assert bad.status_code == 403
    # 连 Origin 都不带（非浏览器客户端）同样拒绝
    assert panel.input(client, [], token=token, origin=None).status_code == 403


def test_input_events_reach_the_remote_page(panel):
    client = panel.client
    panel.start(client)
    wait_until(lambda: panel.mgr.has_session())
    token = csrf_of(client.get("/admin/auth/browser").text)
    resp = panel.input(client, [{"t": "m", "x": 5, "y": 6}, {"t": "k", "k": "a"}], token=token)
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"ok": True, "queued": 2, "state": body["state"]}

    session = panel.backend.session
    wait_until(lambda: session.moves == [(5.0, 6.0)])
    wait_until(lambda: session.texts == ["a"])


def test_input_rejects_an_oversized_body(panel):
    client = panel.client
    panel.start(client)
    token = csrf_of(client.get("/admin/auth/browser").text)
    huge = json.dumps({"events": [{"t": "x", "s": "A" * (rb.MAX_INPUT_BYTES * 2)}]})
    resp = client.post("/admin/auth/browser/input", content=huge,
                       headers={"X-CSRF-Token": token, "Origin": ORIGIN,
                                "Content-Type": "application/json"})
    assert resp.status_code == 413


def test_input_rejects_too_many_events(panel):
    client = panel.client
    panel.start(client)
    token = csrf_of(client.get("/admin/auth/browser").text)
    resp = panel.input(client, [{"t": "m", "x": 1, "y": 1}] * (rb.MAX_EVENTS + 1), token=token)
    assert resp.status_code == 400
    assert str(rb.MAX_EVENTS) in resp.json()["detail"]


def test_input_rejects_a_malformed_body(panel):
    client = panel.client
    panel.start(client)
    token = csrf_of(client.get("/admin/auth/browser").text)
    headers = {"X-CSRF-Token": token, "Origin": ORIGIN, "Content-Type": "application/json"}
    assert client.post("/admin/auth/browser/input", content="{not json",
                       headers=headers).status_code == 400
    assert client.post("/admin/auth/browser/input", json={"events": "nope"},
                       headers=headers).status_code == 400
    assert client.post("/admin/auth/browser/input", json=[1, 2, 3],
                       headers=headers).status_code == 400


def test_input_without_a_session_is_404(panel):
    """没有会话时输入必须被拒，而不是静静丢进内存。"""
    client = panel.client
    token = csrf_of(client.get("/admin/auth/browser").text)
    assert panel.input(client, [{"t": "k", "k": "a"}], token=token).status_code == 404
    assert panel.mgr.has_session() is False


def test_the_input_body_never_reaches_the_logs(panel, caplog):
    """``/input`` 的请求体逐字就是学校口令的键盘记录。"""
    client = panel.client
    panel.start(client)
    token = csrf_of(client.get("/admin/auth/browser").text)
    with caplog.at_level(logging.DEBUG):
        resp = panel.input(client, [{"t": "x", "s": SENTINEL}], token=token)
        assert resp.status_code == 200
    assert SENTINEL not in caplog.text


# ====================================================================== 状态与页面


def test_the_page_shows_the_awaiting_login_state(panel):
    client = panel.client
    panel.start(client)
    wait_until(lambda: panel.mgr.state_view()["state"] == rb.LOGIN_AWAITING)
    page = client.get("/admin/auth/browser")
    assert "等待你在远程画面里完成登录" in page.text
    assert "图形验证码" in page.text
    assert 'action="/admin/auth/browser/stop"' in page.text


def test_the_page_shows_a_captured_session_with_the_precheck_result(panel_factory):
    item = panel_factory(session=FakeSession(token=SENTINEL),
                         precheck=sentinel_precheck("valid", "测试替身：凭据有效"))
    client = item.admin()
    item.start(client)
    wait_until(lambda: item.mgr.state_view()["captured"])

    page = client.get("/admin/auth/browser")
    assert page.status_code == 200
    assert "已获取凭据" in page.text
    assert "测试替身：凭据有效" in page.text
    assert item.mgr.state_view()["fingerprint"] in page.text
    assert "只读认证预检" in page.text


def test_the_page_shows_a_failed_session_with_an_honest_reason(panel_factory):
    item = panel_factory(error=BrowserLaunchError(
        "找不到浏览器可执行文件：PLAYWRIGHT_BROWSERS_PATH 指向项目下的 .cache/ms-playwright"))
    client = item.admin()
    item.start(client)
    wait_until(lambda: item.mgr.state_view()["state"] == rb.LOGIN_FAILED)

    page = client.get("/admin/auth/browser")
    assert "登录流程失败" in page.text
    assert "PLAYWRIGHT_BROWSERS_PATH" in page.text
    assert "launch" in page.text, "失败原因要如实写出来（这里是 launch）"


def test_the_page_warns_loudly_about_plain_http(panel):
    page = panel.client.get("/admin/auth/browser")
    assert "明文 HTTP" in page.text
    assert "ssh -L" in page.text
    assert "学校账号口令" in page.text


def test_the_start_button_is_hidden_when_plain_http_is_not_allowed(panel_factory):
    """服务端一定会拒绝的组合，就不该给一个按下去必然失败的按钮。"""
    item = panel_factory(auth_remote_allow_insecure=False)
    client = item.admin()
    page = client.get("/admin/auth/browser")
    assert 'action="/admin/auth/browser/start"' not in page.text
    assert "明文 HTTP 下已禁止启动" in page.text
    assert "只允许 HTTPS" in page.text

    # 页面不是唯一的一道：服务端确实会拒绝
    assert "ok=0" in item.start(client, expect_ok=False)


def test_the_page_explains_the_env_source_trap(panel_factory):
    """``HAUT_AUTH_SOURCE=env`` 时快照会被忽略：必须在动手**之前**说清楚。"""
    item = panel_factory(auth_source="env")
    page = item.admin().get("/admin/auth/browser")
    assert "HAUT_AUTH_SOURCE" in page.text
    assert "不会被使用" in page.text


def test_the_page_renders_the_configured_poll_interval(panel_factory):
    item = panel_factory(auth_remote_frame_interval_ms=250)
    client = item.admin()
    item.start(client)
    wait_until(lambda: item.mgr.has_session())
    assert 'data-poll-ms="250"' in client.get("/admin/auth/browser").text


def test_the_page_says_disabled_when_the_feature_is_off(panel_factory):
    item = panel_factory(auth_remote_enabled=False)
    client = item.admin()
    page = client.get("/admin/auth/browser")
    assert "已禁用" in page.text
    assert 'action="/admin/auth/browser/start"' not in page.text

    resp = client.post("/admin/auth/browser/start", data={"_csrf": csrf_of(page.text)})
    assert resp.status_code == 303
    assert "ok=0" in resp.headers["location"]
    assert item.mgr.has_session() is False


def test_the_page_says_unavailable_when_the_browser_is_missing(panel, monkeypatch):
    monkeypatch.setattr(auth_browser, "_probe_playwright",
                        lambda: (False, "未安装 playwright（测试强制的不可用）"))
    auth_browser.browser_availability.cache_clear()

    page = panel.client.get("/admin/auth/browser")
    assert page.status_code == 200
    assert "不可用" in page.text
    assert "未安装 playwright（测试强制的不可用）" in page.text
    assert 'action="/admin/auth/browser/start"' not in page.text
    assert "playwright install chromium" in page.text
    assert "PLAYWRIGHT_BROWSERS_PATH" in page.text


def test_the_page_keeps_working_when_the_manager_is_missing(make_client):
    """没装配管理器时页面也必须渲染（渲染期绝不能因为 None 而 500）。"""
    from fastapi.testclient import TestClient

    from haut_seat.web import security

    app, _probe = make_client()
    app.state.remote_browser = None
    security.create_admin(app.state.db, "tester", GOOD_PASSWORD)
    fresh = TestClient(app, follow_redirects=False)
    assert fresh.post("/admin/login",
                      data={"username": "tester", "password": GOOD_PASSWORD}).status_code == 303
    assert fresh.get("/admin/auth/browser").status_code == 200
    assert fresh.get("/admin/auth/browser/state").status_code == 404
    assert fresh.get("/admin/auth/browser/frame").status_code == 404


# ====================================================================== 单飞


def test_start_is_refused_while_another_login_is_in_flight(panel):
    """CLI 或上一次采集正在进行中：绝不开第二个窗口（两边会写同一份全局快照）。"""
    from haut_seat.services.auth_login_service import record_login_state

    record_login_state(panel.db, login_state=LOGIN_AWAITING, message="CLI 正在采集")
    location = panel.start(panel.client, expect_ok=False)
    assert panel.mgr.has_session() is False
    assert SENTINEL not in location


# ====================================================================== 凭据隔离与泄露


def test_capturing_through_the_panel_never_touches_per_user_credentials(panel_factory):
    """凭据归属：面板采集的是**全局快照**，用户端绑定的加密凭据一个字节都不动。

    这是任务点名要先查清的那件事：有属主的计划永远用该用户自己的凭据
    （``user_credentials.settings_for_user``），不存在"某个用户的 Cookie 被写进
    所有用户共用的凭据文件"这条路径。
    """
    from haut_seat.services import user_credentials as uc

    item = panel_factory(session=FakeSession(token=SENTINEL))
    user_id = create_user(item.db)
    bind_credentials(item.db, item.app.state.settings, user_id)
    before = [tuple(row) for row in item.db.query_all("SELECT * FROM user_credential")]
    assert before, "前提：用户端确实有一份加密凭据"

    client = item.admin()
    item.start(client)
    wait_until(lambda: item.mgr.state_view()["captured"])

    after = [tuple(row) for row in item.db.query_all("SELECT * FROM user_credential")]
    assert after == before, "远程浏览器绝不允许改动每用户凭据"

    # 有属主的计划依旧拿得到**用户自己的**凭据，而不是刚采集的全局快照
    per_user, creds = uc.settings_for_user(item.db, item.app.state.settings, user_id)
    assert creds.cookie == FAKE_SCHOOL_COOKIE
    assert creds.authorization == FAKE_SCHOOL_AUTHORIZATION
    assert per_user.cookie.get_secret_value() == FAKE_SCHOOL_COOKIE
    assert per_user.authorization.get_secret_value() == FAKE_SCHOOL_AUTHORIZATION


def test_the_captured_snapshot_lands_in_the_configured_file(panel_factory):
    """采集成功必须**真的落盘**到配置的快照文件（不是只在内存里说成功）。"""
    item = panel_factory(session=FakeSession(token=SENTINEL))
    client = item.admin()
    item.start(client)
    wait_until(lambda: item.mgr.state_view()["captured"])

    snapshot = Path(item.app.state.settings.auth_snapshot_file)
    assert snapshot.exists(), "采集成功却没有落盘"
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert payload["authorization"] == f"Bearer {SENTINEL}"


def test_the_sentinel_never_reaches_pages_state_json_or_logs(panel_factory, caplog):
    item = panel_factory(session=FakeSession(token=SENTINEL),
                         precheck=sentinel_precheck("valid", "测试替身：凭据有效"))
    client = item.admin()
    with caplog.at_level(logging.DEBUG):
        location = item.start(client)
        assert SENTINEL not in location
        wait_until(lambda: item.mgr.state_view()["captured"])
        for path in ENDPOINTS:
            resp = client.get(path)
            assert resp.status_code == 200, path
            assert SENTINEL.encode() not in resp.content, path
            assert b"Bearer " not in resp.content, path
            assert (COOKIE_NAME + "=").encode() not in resp.content, path
        state = client.get("/admin/auth/browser/state").json()
        assert SENTINEL not in json.dumps(state, ensure_ascii=False, default=str)

    assert SENTINEL not in caplog.text
    assert f"{COOKIE_NAME}={SENTINEL}" not in caplog.text
    assert "Bearer " not in caplog.text


def test_the_session_url_is_stripped_of_query_parameters(panel_factory):
    """登录跳转的 query 里可能带 token，而它会原样出现在页面与 JSON 里。"""
    session = FakeSession(token=None,
                          url="https://seat.example.invalid/login?token=SECRET#frag")
    item = panel_factory(session=session)
    client = item.admin()
    item.start(client)
    wait_until(lambda: item.mgr.state_view()["url"])

    assert "SECRET" not in client.get("/admin/auth/browser/state").text
    assert "SECRET" not in client.get("/admin/auth/browser").text
    assert item.mgr.state_view()["url"] == "https://seat.example.invalid/login"


# ====================================================================== 登出


def test_logout_stops_only_the_owners_session(panel_factory):
    item = panel_factory()
    alice = item.admin("alice")
    bob = item.admin("bob")
    item.start(alice)
    wait_until(lambda: item.mgr.has_session())
    assert item.backend.session.close_calls == 0

    # bob 退出登录：不是他的会话，一个字节都不该动
    bob_token = csrf_of(bob.get("/admin/auth/browser").text)
    assert bob.post("/admin/logout", data={"_csrf": bob_token}).status_code == 303
    assert item.mgr.has_session() is True, "退出登录只能收掉自己的会话"
    assert item.backend.session.close_calls == 0

    # alice 退出登录：她的会话必须被收掉，浏览器被关
    alice_token = csrf_of(alice.get("/admin/auth/browser").text)
    assert alice.post("/admin/logout", data={"_csrf": alice_token}).status_code == 303
    wait_until(lambda: item.mgr.has_session() is False)
    assert item.backend.session.close_calls == 1


def test_logout_works_even_if_the_remote_cleanup_fails(panel):
    """收尾失败绝不能让"退出登录"这件事失败。"""

    class ExplodingStop:
        def stop(self, **kwargs):
            raise RuntimeError("收尾失败")

    client = panel.client
    token = csrf_of(client.get("/admin/auth/browser").text)
    panel.app.state.remote_browser = ExplodingStop()
    resp = client.post("/admin/logout", data={"_csrf": token})
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/admin/login")


# ====================================================================== 认证状态页的入口


def test_the_auth_page_links_to_the_remote_browser(admin_client):
    _app, client = admin_client
    page = client.get("/admin/auth")
    assert page.status_code == 200
    assert 'href="/admin/auth/browser"' in page.text
    assert "服务器上没有桌面" in page.text


def test_the_auth_page_has_no_remote_link_when_the_browser_is_missing(admin_client, monkeypatch):
    monkeypatch.setattr(auth_browser, "_probe_playwright",
                        lambda: (False, "未安装 playwright（测试强制的不可用）"))
    auth_browser.browser_availability.cache_clear()
    _app, client = admin_client
    page = client.get("/admin/auth")
    assert "未安装 playwright（测试强制的不可用）" in page.text
    assert 'href="/admin/auth/browser"' not in page.text


# ====================================================================== 模板与脚本守卫


def test_the_template_has_no_credential_input_at_all():
    """学校的口令必须敲进**远端页面**，绝不能敲进我们自己的 DOM。

    敲进我们的 DOM 会进表单、浏览器自动填充与历史记录 —— 凭空多一处泄漏面。
    """
    text = read(TEMPLATE)
    for tag in re.findall(r"<input[^>]*>", text):
        assert 'type="hidden"' in tag, tag
    assert "<textarea" not in text
    assert "<select" not in text
    assert 'type="password"' not in text


def test_the_template_has_no_inline_handlers_or_external_assets():
    """CSP 是 ``script-src 'self'``（``test_web_static.py`` 逐字钉住），内联处理器不执行。"""
    text = read(TEMPLATE)
    assert not re.search(
        r"\son(click|change|submit|load|error|input|focus|blur|keydown|keyup|"
        r"mousemove|mousedown|mouseup|wheel|contextmenu|compositionend)\s*=",
        text, re.IGNORECASE,
    )
    assert not re.search(r"""(?:src|href)\s*=\s*["'](?:https?:)?//""", text)
    assert "javascript:" not in text.lower()


def test_the_template_asks_before_launching_a_browser():
    """两个动作（开始 / 结束）都必须先问一句：起浏览器是不可忽视的副作用。"""
    assert read(TEMPLATE).count("data-confirm=") == 2


def test_the_script_is_actually_served(panel):
    """模板里的 ``<script src>`` 必须真的取得到 —— 改名/挪目录会让页面静默失去键鼠转发。"""
    resp = panel.client.get("/static/remote_browser.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    assert "remote-browser-root" in resp.text


def test_the_script_never_stores_credentials():
    """只看可执行代码：注释里提到 localStorage 不算数（复用既有的注释剥离器）。"""
    code = js_code_only(read(SCRIPT))
    for forbidden in ("localStorage", "sessionStorage", "document.cookie", "innerHTML"):
        assert forbidden not in code, forbidden


def test_the_script_talks_to_exactly_the_three_endpoints():
    code = js_code_only(read(SCRIPT))
    for attr in ("data-frame-url", "data-state-url", "data-input-url"):
        assert attr in read(SCRIPT), attr
    assert "X-CSRF-Token" in code, "JSON 端点没有表单字段，令牌只能走请求头"


def test_the_script_does_not_define_the_key_mapping():
    """按键 → 远端按键的映射必须留在服务端（可单测，且只有白名单能到达 Playwright）。"""
    code = js_code_only(read(SCRIPT))
    for key in ("Enter", "Tab", "Escape", "Backspace", "Delete", "ArrowLeft", "PageDown"):
        assert key not in code, f"{key} 的映射不该出现在前端"


def test_neither_file_mentions_a_real_school_host():
    real_host = "wslib" + ".haut" + ".edu.cn"
    assert real_host not in read(TEMPLATE)
    assert real_host not in read(SCRIPT)


# ------------------------------------------------- 前后端契约（这一节专治"静默失效"）
#
# 这一节是**补上的**：状态卡片原本把「已获取凭据 / 只读认证预检 / 结束原因」包在
# ``{% if %}`` 里，而脚本用 ``getElementById`` 找它们 —— 元素在"当时没有"的那一刻
# 就不在 DOM 里，于是**采集成功之后页面上也永远不会出现那几行**，只剩一个看起来
# 还在转圈的旧画面。症状是"明明好了却像卡住"，而任何一侧单独看都完全正常。
# 契约必须由测试钉住，而不是靠下一次有人想起来。


def _js_element_ids() -> set[str]:
    """脚本里 ``getElementById('X')`` 找的全部 id。"""
    return set(re.findall(r"getElementById\(\s*['\"]([^'\"]+)['\"]\s*\)", read(SCRIPT)))


def _js_state_fields() -> set[str]:
    """脚本从状态 JSON 里读的全部字段（``data.<name>``）。

    ``data.`` 后面跟着的属性名就是前后端契约：服务端少给一个，页面就少一块信息，
    而且**不会报错**（JS 里 undefined 只会渲染成空），所以只能靠这条测试挡住。
    """
    code = js_code_only(read(SCRIPT))
    return {m for m in re.findall(r"\bdata\.([A-Za-z_][A-Za-z0-9_]*)", code)}


def test_every_element_the_script_looks_up_is_rendered_by_the_template():
    """脚本要找的每个 id 都必须真的渲染在页面上（且不依赖任何条件分支）。"""
    template = read(TEMPLATE)
    ids = _js_element_ids()
    assert ids, "脚本一个元素都不找？解析出来的 id 集合不该是空的"
    missing = sorted(i for i in ids if f'id="{i}"' not in template)
    assert not missing, f"脚本会去找这些元素，但模板里没有：{missing}"
    # 根节点不存在时脚本自行退出，所以它可以在"没有会话"的页面上缺席 ——
    # 但它必须**无条件**渲染：一旦包进 {% if %}，暂时没有会话就会永久失去键鼠转发。
    for element_id in ("remote-browser-root", "rb-captured-line",
                       "rb-precheck-line", "rb-reason-line"):
        assert f'id="{element_id}"' in template


def test_every_state_field_the_script_reads_exists_in_the_state_view(panel_factory):
    """``state_view()`` 必须给出脚本读的每一个字段 —— 空会话、进行中、终态都要给。

    比"少给一个字段页面就静默少一块信息"更糟的是少给 ``terminal`` 或 ``reason``：
    那时页面会一直轮询一个已经结束的会话，正是"永远加载中"。
    JS 里读到 ``undefined`` **不会报错**，只会渲染成空 —— 所以只能靠这条测试挡住。
    """
    fields = _js_state_fields()
    assert fields, "脚本一个状态字段都不读？解析出来的集合不该是空的"

    item = panel_factory(session=FakeSession(token=SENTINEL),
                         precheck=sentinel_precheck("valid", "测试替身：凭据有效"))
    client = item.admin()

    idle = item.mgr.idle_view()
    missing = sorted(f for f in fields if f not in idle)
    assert not missing, f"脚本会读这些字段，但空会话的视图里没有：{missing}"

    item.start(client)
    wait_until(lambda: item.mgr.state_view()["state"] == rb.LOGIN_AWAITING)
    active = item.mgr.state_view()
    missing = sorted(f for f in fields if f not in active)
    assert not missing, f"进行中的视图缺字段：{missing}"

    wait_until(lambda: item.mgr.state_view()["captured"])
    final = item.mgr.state_view()
    missing = sorted(f for f in fields if f not in final)
    assert not missing, f"终态视图缺字段：{missing}"
    assert final["terminal"] is True, "采集成功是终态：页面据此必须停止轮询"
    assert final["reason"] == "" or isinstance(final["reason"], str)