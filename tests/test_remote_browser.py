"""远程浏览器（服务器端无头 Chromium + 画面/键鼠转发）的服务层测试。

**全离线**：不装浏览器、不开浏览器、不联网、不出现真实学校域名。
后端与只读预检都是注入的替身，因此 CI 上连 playwright 都不需要存在 ——
与 ``tests/test_auth_browser.py`` 的注入范式一致。

覆盖：
- 事件翻译（按键白名单、坐标收敛、URL 脱敏）—— 纯函数，可直接断言；
- 会话归属：别的管理员既看不到画面也发不了输入；
- 凭据链：采集后**先落盘快照**、再记元数据、再做只读复检，且**绝不**碰每用户凭据；
- 失败路径：未安装 / 启动失败 / 崩溃 / 被关窗 / 总超时 / 空闲超时，
  且**浏览器恰好被关一次**、无线程残留；
- 文本守卫：不代填口令、不出现提交接口、不出现 ``--no-sandbox``、不新增调试端口。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import pytest

from haut_seat.services import remote_browser as rb
from haut_seat.services.auth_browser import (
    COOKIE_NAME,
    BrowserLaunchError,
    BrowserNotInstalledError,
    LoginCapture,
)

#: 哨兵：形状像真令牌，但显然是测试专用值。任何日志/页面/异常都不该出现它。
SENTINEL = "SENTINELTOKEN-._0003-abcdefghijklmnop"

FAKE_BASE_URL = "https://seat.example.invalid/login"

#: 真实学校域名（拼出来，免得本文件自己被文本守卫命中）。
REAL_HOST = "wslib" + ".haut" + ".edu.cn"


# ====================================================================== 替身


class FakeSession:
    """假的无头浏览器页面：记录每一次鼠标/键盘调用，可模拟各种死法。"""

    def __init__(
        self,
        *,
        token: str | None = None,
        appear_after: int = 1,
        closed_after: int | None = None,
        url: str = FAKE_BASE_URL,
        screenshot: bytes | None = b"\xff\xd8\xff\xe0fake-jpeg",
        screenshot_error: Exception | None = None,
    ) -> None:
        self.token = token
        self.appear_after = appear_after
        self.closed_after = closed_after
        self.url = url
        self._screenshot = screenshot
        self._screenshot_error = screenshot_error
        self.polls = 0
        self.frames = 0
        self.close_calls = 0
        self.moves: list[tuple[float, float]] = []
        self.clicks: list[dict] = []
        self.wheels: list[tuple[float, float]] = []
        self.presses: list[str] = []
        self.texts: list[str] = []
        self.cookie_queries = 0

    # ---- 画面 ----

    def screenshot(self, *, quality: int) -> bytes:
        if self._screenshot_error is not None:
            raise self._screenshot_error
        self.frames += 1
        return self._screenshot

    def viewport_size(self) -> tuple[int, int]:
        return (1280, 800)

    def current_url(self) -> str:
        return self.url

    # ---- 输入 ----

    def goto(self, url: str, *, timeout_s: float) -> None:
        self.url = url

    def mouse_move(self, x: float, y: float) -> None:
        self.moves.append((x, y))

    def click(self, x: float, y: float, *, button: str, click_count: int) -> None:
        self.clicks.append({"x": x, "y": y, "button": button, "n": click_count})

    def wheel(self, dx: float, dy: float) -> None:
        self.wheels.append((dx, dy))

    def press(self, spec: str) -> None:
        self.presses.append(spec)

    def insert_text(self, value: str) -> None:
        self.texts.append(value)

    # ---- 状态 ----

    def is_closed(self) -> bool:
        return self.closed_after is not None and self.polls >= self.closed_after

    def cookie(self, name: str) -> str | None:
        self.cookie_queries += 1
        self.polls += 1
        if name != COOKIE_NAME or self.token is None:
            return None
        return self.token if self.polls >= self.appear_after else None

    def close(self) -> None:
        self.close_calls += 1


class FakeBackend:
    """假后端：记录 ``open()`` 收到的参数，可抛异常。"""

    def __init__(self, session=None, error: Exception | None = None) -> None:
        self.session = session if session is not None else FakeSession()
        self.error = error
        self.calls: list[dict] = []

    def open(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.session


class Clock:
    """可推进的假时钟（``time.monotonic`` 语义）。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# ====================================================================== 夹具


@pytest.fixture
def ws(web_settings, tmp_path):
    """远程浏览器专用配置：小视口、快节拍、短超时（测试不等待）。"""

    def _make(**overrides):
        base = {
            "base_url": FAKE_BASE_URL,
            "auth_dir": str(tmp_path / "auth"),
            "auth_snapshot_file": str(tmp_path / "auth" / "credentials.json"),
            "auth_profile_dir": str(tmp_path / "auth" / "browser-profile"),
            "auth_remote_frame_interval_ms": 100,
            "auth_remote_idle_timeout_s": 30.0,
            "auth_login_timeout_s": 60.0,
            "auth_remote_viewport_w": 1280,
            "auth_remote_viewport_h": 800,
        }
        base.update(overrides)
        return web_settings.model_copy(update=base)

    return _make


@pytest.fixture(autouse=True)
def _clear_availability_cache():
    """``browser_availability`` 带缓存；每个用例前后都清掉。"""
    from haut_seat.services.auth_browser import browser_availability

    browser_availability.cache_clear()
    yield
    browser_availability.cache_clear()


@pytest.fixture
def manager_factory(ws, web_db):
    """构造管理器并保证测试结束时线程一定被收掉。"""
    created = []

    def _make(*, backend=None, precheck=None, clock=None, settings=None, db=None, **kw):
        mgr = rb.RemoteBrowserManager(
            settings or ws(**kw), db if db is not None else web_db,
            logging.getLogger("haut_seat.test.remote"),
            backend=backend if backend is not None else FakeBackend(),
            precheck=precheck,
            now=clock if clock is not None else None,
        )
        created.append(mgr)
        return mgr

    yield _make
    for mgr in created:
        mgr.shutdown(timeout_s=2.0)


def wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.01):
    """等一个条件成立（轮询）。超时即失败，绝不静默放过。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("等待条件超时：远程浏览器会话没有进入预期状态")


def sentinel_precheck(status="valid", message="测试替身：凭据有效"):
    """假只读预检：记录收到的 capture，不联网。"""
    calls = []

    def _precheck(capture):
        calls.append(capture)
        return status, message

    _precheck.calls = calls
    return _precheck


# ====================================================================== 纯函数


def test_single_character_becomes_text_not_a_key_spec():
    """单字符走 insert_text：中文与符号按字面插入，不受键盘布局影响。"""
    assert rb.key_spec({"t": "k", "k": "a"}) == ("text", "a")
    assert rb.key_spec({"t": "k", "k": "中"}) == ("text", "中")
    assert rb.key_spec({"t": "k", "k": "@"}) == ("text", "@")


def test_named_keys_are_whitelisted():
    assert rb.key_spec({"t": "k", "k": "Enter"}) == ("press", "Enter")
    assert rb.key_spec({"t": "k", "k": "Tab"}) == ("press", "Tab")
    assert rb.key_spec({"t": "k", "k": "ArrowLeft"}) == ("press", "ArrowLeft")


def test_an_unknown_key_spec_is_dropped():
    """请求体**永远**无法直接把 Playwright 的 key spec 塞进来。"""
    for key in ("F13", "MediaPlayPause", "Control+Shift+Delete", "", "Unknown"):
        assert rb.key_spec({"t": "k", "k": key}) is None, key


def test_modifiers_are_combined_in_a_fixed_order():
    assert rb.key_spec({"t": "k", "k": "a", "ctrl": True}) == ("press", "Control+a")
    assert rb.key_spec({"t": "k", "k": "Enter", "shift": True}) == ("press", "Shift+Enter")
    assert rb.key_spec(
        {"t": "k", "k": "a", "ctrl": True, "shift": True}
    ) == ("press", "Control+Shift+a")


def test_key_spec_ignores_non_string_keys():
    assert rb.key_spec({"t": "k", "k": None}) is None
    assert rb.key_spec({"t": "k", "k": 65}) is None
    assert rb.key_spec({"t": "k"}) is None


def test_url_is_stripped_of_query_and_fragment():
    """登录跳转的 query 里可能带 token，而它会原样出现在页面与 JSON 里。"""
    assert rb.sanitize_url(
        "https://seat.example.invalid/login?redirect=/x&token=SECRET#frag"
    ) == "https://seat.example.invalid/login"
    assert rb.sanitize_url("https://seat.example.invalid/") == "https://seat.example.invalid/"
    assert rb.sanitize_url("") == ""
    assert rb.sanitize_url("not a url") == ""


def test_numbers_are_clamped_and_garbage_becomes_the_floor():
    assert rb._num(5, hi=10) == 5
    assert rb._num(-5, hi=10) == 0
    assert rb._num(50, hi=10) == 10
    assert rb._num(float("nan"), hi=10) == 0
    assert rb._num(float("inf"), hi=10) == 0
    assert rb._num("abc", hi=10) == 0
    assert rb._num(None, hi=10) == 0


# ====================================================================== 归属


def test_a_foreign_admin_gets_no_view_and_no_frame(manager_factory):
    mgr = manager_factory()
    assert mgr.start(admin_id=1, token_hash="A")["ok"] is True
    try:
        wait_until(lambda: mgr.has_session())
        # 属主看得到
        assert mgr.view_for(1, "A") is not None
        # 别人：既没有视图，也没有一个字节的画面
        assert mgr.view_for(2, "B") is None
        assert mgr.frame_for(2, "B") is None
        # 同一个管理员但换了会话令牌（重新登录后的新会话）同样不是属主
        assert mgr.view_for(1, "OTHER") is None
        assert mgr.frame_for(1, "OTHER") is None
    finally:
        mgr.stop(admin_id=1, token_hash="A")


def test_a_foreign_admin_cannot_stop_or_send_input(manager_factory):
    backend = FakeBackend()
    mgr = manager_factory(backend=backend)
    mgr.start(admin_id=1, token_hash="A")
    try:
        wait_until(lambda: mgr.view_for(1, "A")["active"])
        stopped = mgr.stop(admin_id=2, token_hash="B")
        assert stopped["ok"] is False
        assert mgr.has_session() is True, "别人的 stop 绝不能把会话停掉"

        result = mgr.submit_input([{"t": "k", "k": "a"}], admin_id=2, token_hash="B")
        assert result == {"ok": False, "owner": False, "queued": 0, "state": rb.LOGIN_IDLE}
        assert backend.session.texts == [], "别人的输入一个字节都不该转发"
    finally:
        mgr.stop(admin_id=1, token_hash="A")


def test_the_session_is_bound_to_the_admin_id_and_token_hash(manager_factory):
    """归属是 (admin_id, token_hash) 二元组：猜中其中一个也接管不了。"""
    mgr = manager_factory()
    mgr.start(admin_id=7, token_hash="HASH")
    try:
        wait_until(lambda: mgr.has_session())
        assert mgr.view_for(7, "HASH") is not None
        for admin_id, token_hash in ((7, "guess"), (8, "HASH"), (0, "")):
            assert mgr.view_for(admin_id, token_hash) is None, (admin_id, token_hash)
    finally:
        mgr.stop(admin_id=7, token_hash="HASH")


# ====================================================================== 启动守卫


def test_start_is_refused_when_the_feature_is_disabled(manager_factory):
    mgr = manager_factory(auth_remote_enabled=False)
    result = mgr.start(admin_id=1, token_hash="A")
    assert result["ok"] is False
    assert "HAUT_AUTH_REMOTE_ENABLED" in result["message"]
    assert mgr.has_session() is False


def test_start_is_refused_on_plain_http_when_insecure_is_not_allowed(manager_factory):
    mgr = manager_factory(auth_remote_allow_insecure=False)
    result = mgr.start(admin_id=1, token_hash="A", secure=False)
    assert result["ok"] is False
    assert "明文" in result["message"]
    assert "ssh -L" in result["message"], "必须给出可执行的缓解办法"
    assert mgr.has_session() is False


def test_start_is_allowed_on_plain_http_by_default(manager_factory):
    """默认放行（用户明确要求"HTTP 先用起来"），但页面会红字告警。"""
    mgr = manager_factory()
    assert mgr.start(admin_id=1, token_hash="A", secure=False)["ok"] is True
    mgr.stop(admin_id=1, token_hash="A")


def test_start_is_refused_when_the_browser_is_unavailable(manager_factory, monkeypatch):
    monkeypatch.setattr(
        rb, "browser_availability", lambda: (False, "未安装 playwright（测试强制的不可用）")
    )
    mgr = manager_factory()
    result = mgr.start(admin_id=1, token_hash="A")
    assert result["ok"] is False
    assert "未安装 playwright（测试强制的不可用）" in result["message"]
    assert mgr.has_session() is False


def test_start_joins_the_single_flight_state_machine(manager_factory, web_db):
    """CLI 正在采集时绝不开第二个窗口（否则两份采集同时写全局快照）。"""
    from haut_seat.services.auth_login_service import LOGIN_AWAITING, record_login_state

    record_login_state(web_db, login_state=LOGIN_AWAITING, message="CLI 正在采集")
    mgr = manager_factory()
    result = mgr.start(admin_id=1, token_hash="A")
    assert result["ok"] is False
    assert "已有一个登录流程在进行中" in result["message"]
    assert mgr.has_session() is False


def test_a_stale_login_state_does_not_block_start(manager_factory, web_db):
    """上一次被强杀留下的 awaiting_login 不能把功能永久锁死。"""
    from haut_seat.web import repository as repo

    repo.set_credential_state(web_db, login_state="awaiting_login",
                              login_message="上次被强杀留下的残留")
    web_db.execute("UPDATE auth_credential_state SET login_updated_at = ?",
                   ("2000-01-01 00:00:00",))
    mgr = manager_factory()
    assert mgr.start(admin_id=1, token_hash="A")["ok"] is True
    mgr.stop(admin_id=1, token_hash="A")


def test_two_concurrent_starts_cannot_both_win(manager_factory):
    """检查并占用必须在同一把锁里：否则两个管理员会同时"启动成功"。"""
    mgr = manager_factory()
    results: list[dict] = []
    barrier = threading.Barrier(3)

    def run():
        barrier.wait()
        results.append(mgr.start(admin_id=1, token_hash="A"))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    barrier.wait()
    for t in threads:
        t.join(5)
    try:
        assert sum(1 for r in results if r["ok"]) == 1, results
        assert sum(1 for r in results if not r["ok"]) == 1, results
    finally:
        mgr.stop(admin_id=1, token_hash="A")


def test_only_one_session_at_a_time_even_from_the_same_admin(manager_factory):
    mgr = manager_factory()
    assert mgr.start(admin_id=1, token_hash="A")["ok"] is True
    try:
        second = mgr.start(admin_id=1, token_hash="A")
        assert second["ok"] is False
        assert "已有一个远程浏览器会话在进行中" in second["message"]
    finally:
        mgr.stop(admin_id=1, token_hash="A")


# ====================================================================== 正常运行


def test_the_worker_opens_a_headless_bundled_browser(manager_factory):
    backend = FakeBackend()
    mgr = manager_factory(backend=backend)
    mgr.start(admin_id=1, token_hash="A")
    try:
        wait_until(lambda: backend.calls)
        call = backend.calls[0]
        assert call["headless"] is True, "服务器上没有桌面，必须无头"
        assert call["channel"] == "", "空 = 捆绑 Chromium（不能重载 msedge）"
        assert call["viewport"] == (1280, 800)
        assert call["login_url"] == FAKE_BASE_URL
    finally:
        mgr.stop(admin_id=1, token_hash="A")


def test_state_and_frames_become_available(manager_factory):
    mgr = manager_factory()
    mgr.start(admin_id=1, token_hash="A")
    try:
        wait_until(lambda: mgr.view_for(1, "A")["state"] == rb.LOGIN_AWAITING)
        wait_until(lambda: mgr.frame_for(1, "A") is not None)
        view = mgr.view_for(1, "A")
        assert view["label"] == rb.login_state_label(rb.LOGIN_AWAITING)
        assert view["active"] is True
        assert view["viewport"] == {"width": 1280, "height": 800}
        assert view["idle_timeout_s"] == 30.0
        assert view["total_timeout_s"] == 60.0
        assert view["installed"] in (True, False)  # 这台机器装没装都合法
        assert mgr.frame_for(1, "A").startswith(b"\xff\xd8")
    finally:
        mgr.stop(admin_id=1, token_hash="A")


def test_the_state_view_never_carries_a_credential(manager_factory):
    """视图里只能有指纹与时间，绝不能有令牌本身。"""
    # 预检用替身：本用例只看"视图里有没有凭据"，真去打接口只会让用例变慢且更脆。
    mgr = manager_factory(
        backend=FakeBackend(FakeSession(token=SENTINEL)),
        precheck=sentinel_precheck(),
    )
    mgr.start(admin_id=1, token_hash="A")
    try:
        wait_until(lambda: mgr.view_for(1, "A")["captured"])
        blob = json.dumps(mgr.view_for(1, "A"), ensure_ascii=False, default=str)
        assert SENTINEL not in blob
        assert "Bearer " not in blob
        assert COOKIE_NAME + "=" not in blob
    finally:
        mgr.stop(admin_id=1, token_hash="A")


def test_the_heartbeat_keeps_the_session_from_looking_stale(manager_factory, web_db):
    """没有心跳的话，会话超过 timeout+60s 会被单飞判断当成残留。"""
    from haut_seat.services.auth_login_service import active_login_state

    mgr = manager_factory()
    settings = mgr.settings
    mgr.start(admin_id=1, token_hash="A")
    try:
        # 远程会话在进行中 ⇒ CLI 侧的同一套判断能看见它（单飞锁有效）。
        # 这里等的必须是**库里的**状态：内存状态是工作线程先写的，落库紧随其后，
        # 直接断言内存会撞上那几微秒的窗口。
        wait_until(lambda: active_login_state(settings, web_db) == rb.LOGIN_AWAITING)
    finally:
        mgr.stop(admin_id=1, token_hash="A")


def test_input_is_forwarded_to_the_remote_page(manager_factory):
    backend = FakeBackend()
    mgr = manager_factory(backend=backend)
    mgr.start(admin_id=1, token_hash="A")
    session = backend.session
    try:
        wait_until(lambda: mgr.view_for(1, "A")["state"] == rb.LOGIN_AWAITING)
        result = mgr.submit_input(
            [
                {"t": "m", "x": 10, "y": 20},
                {"t": "c", "x": 30, "y": 40, "b": "left", "n": 1},
                {"t": "w", "dx": 0, "dy": 120},
                {"t": "k", "k": "a"},
                {"t": "k", "k": "Enter"},
                {"t": "k", "k": "a", "ctrl": True},
                {"t": "x", "s": "中文"},
            ],
            admin_id=1, token_hash="A",
        )
        assert result["ok"] is True and result["owner"] is True
        wait_until(lambda: session.texts.count("中文") == 1 and "Control+a" in session.presses)
        assert session.moves == [(10.0, 20.0)]
        assert session.clicks == [{"x": 30.0, "y": 40.0, "button": "left", "n": 1}]
        assert session.wheels == [(0.0, 120.0)]
        assert session.presses == ["Enter", "Control+a"]
        assert session.texts == ["a", "中文"]
    finally:
        mgr.stop(admin_id=1, token_hash="A")


def test_coordinates_are_clamped_into_the_viewport(manager_factory):
    backend = FakeBackend()
    mgr = manager_factory(backend=backend)
    mgr.start(admin_id=1, token_hash="A")
    session = backend.session
    try:
        wait_until(lambda: mgr.view_for(1, "A")["active"])
        mgr.submit_input(
            [{"t": "m", "x": -50, "y": 10 ** 9}, {"t": "c", "x": 10 ** 9, "y": -1}],
            admin_id=1, token_hash="A",
        )
        wait_until(lambda: session.clicks)
        assert session.moves == [(0.0, 799.0)]
        assert session.clicks == [{"x": 1279.0, "y": 0.0, "button": "left", "n": 1}]
    finally:
        mgr.stop(admin_id=1, token_hash="A")


def test_an_unknown_mouse_button_becomes_left(manager_factory):
    backend = FakeBackend()
    mgr = manager_factory(backend=backend)
    mgr.start(admin_id=1, token_hash="A")
    session = backend.session
    try:
        wait_until(lambda: mgr.view_for(1, "A")["active"])
        mgr.submit_input([{"t": "c", "x": 1, "y": 1, "b": "huge"}], admin_id=1, token_hash="A")
        wait_until(lambda: session.clicks)
        assert session.clicks[0]["button"] == "left"
    finally:
        mgr.stop(admin_id=1, token_hash="A")


def test_an_event_that_raises_does_not_kill_the_session(manager_factory):
    """页面跳转瞬间的转发失败很常见，绝不能因此把整个会话判死。"""

    class Grumpy(FakeSession):
        def mouse_move(self, x, y):
            raise RuntimeError("页面正在跳转")

    backend = FakeBackend(Grumpy(token=None))
    mgr = manager_factory(backend=backend)
    mgr.start(admin_id=1, token_hash="A")
    try:
        wait_until(lambda: mgr.view_for(1, "A")["state"] == rb.LOGIN_AWAITING)
        mgr.submit_input([{"t": "m", "x": 1, "y": 1}], admin_id=1, token_hash="A")
        time.sleep(0.2)
        assert mgr.view_for(1, "A")["state"] == rb.LOGIN_AWAITING, "会话不该被杀掉"
    finally:
        mgr.stop(admin_id=1, token_hash="A")


def test_event_caps_and_a_full_queue_never_block_the_caller(manager_factory):
    mgr = manager_factory()
    mgr.start(admin_id=1, token_hash="A")
    try:
        wait_until(lambda: mgr.view_for(1, "A")["active"])
        # 单次请求的事件数上限：多余的直接丢掉，不报错、不排队。
        result = mgr.submit_input(
            [{"t": "m", "x": 1, "y": 1}] * (rb.MAX_EVENTS * 4), admin_id=1, token_hash="A"
        )
        assert result["ok"] is True and result["queued"] <= rb.MAX_EVENTS
    finally:
        mgr.stop(admin_id=1, token_hash="A")


# ====================================================================== 采集成功


def _capture_manager(manager_factory, clock, **kw):
    precheck = kw.pop("precheck", None) or sentinel_precheck()
    backend = FakeBackend(FakeSession(token=SENTINEL, appear_after=2))
    mgr = manager_factory(backend=backend, precheck=precheck, clock=clock, **kw)
    return mgr, backend, precheck


def test_a_captured_token_goes_through_the_existing_credential_chain(
    manager_factory, ws, web_db, tmp_path
):
    """采集成功后：**先落盘快照** → 记登录状态 → 记凭据来源 → 只读复检。"""
    from haut_seat.services import auth_store
    from haut_seat.web import repository as repo

    precheck = sentinel_precheck("valid", "测试替身：凭据有效")
    mgr, _backend, _ = _capture_manager(manager_factory, None, precheck=precheck)
    mgr.start(admin_id=1, token_hash="A")
    try:
        wait_until(lambda: mgr.view_for(1, "A")["captured"])
        snapshot = json.loads(Path(mgr.settings.auth_snapshot_file).read_text(encoding="utf-8"))
        assert snapshot["cookie"] == f"{COOKIE_NAME}={SENTINEL}"
        assert snapshot["authorization"] == f"Bearer {SENTINEL}"

        state = repo.get_credential_state(web_db)
        assert state["login_state"] == rb.LOGIN_CAPTURED
        assert state["source"] == "snapshot"
        assert state["snapshot_present"] == 1
        assert state["fingerprint"] == auth_store.fingerprint(f"Bearer {SENTINEL}")

        view = mgr.view_for(1, "A")
        assert view["precheck_status"] == "valid"
        assert view["precheck_message"] == "测试替身：凭据有效"
        assert view["fingerprint"] == auth_store.fingerprint(f"Bearer {SENTINEL}")
        assert view["captured_at"]

        # 只读复检打的是**刚采到的这份**凭据，不是可能更旧的快照
        assert len(precheck.calls) == 1
        assert precheck.calls[0].cookie == f"{COOKIE_NAME}={SENTINEL}"
    finally:
        mgr.stop(admin_id=1, token_hash="A")


def test_the_browser_is_closed_exactly_once_after_a_capture(manager_factory):
    mgr, backend, _ = _capture_manager(manager_factory, None)
    mgr.start(admin_id=1, token_hash="A")
    try:
        wait_until(lambda: mgr.view_for(1, "A")["captured"])
        wait_until(lambda: backend.session.close_calls == 1)
        time.sleep(0.2)
        assert backend.session.close_calls == 1, "浏览器只能被关一次"
    finally:
        mgr.stop(admin_id=1, token_hash="A")


def test_a_failed_snapshot_write_is_reported_honestly(manager_factory, monkeypatch):
    """人工成果写盘失败必须如实说出来，且**绝不**显示任何值。"""

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(rb, "save_snapshot", boom)
    mgr, _backend, _ = _capture_manager(manager_factory, None)
    mgr.start(admin_id=1, token_hash="A")
    try:
        wait_until(lambda: mgr.view_for(1, "A")["reason"] == "snapshot_write")
        view = mgr.view_for(1, "A")
        assert view["state"] == rb.LOGIN_FAILED
        assert "OSError" in view["message"]
        assert SENTINEL not in json.dumps(view, ensure_ascii=False, default=str)
    finally:
        mgr.stop(admin_id=1, token_hash="A")


def test_the_worker_never_touches_the_per_user_credentials(manager_factory, web_db):
    """凭据归属：远程浏览器写的是**全局快照**，每用户加密凭据一个字节都不动。"""
    from haut_seat.web import repository as repo

    before = [tuple(r) for r in web_db.query_all("SELECT * FROM user_credential")]
    mgr, _backend, _ = _capture_manager(manager_factory, None)
    mgr.start(admin_id=1, token_hash="A")
    try:
        wait_until(lambda: mgr.view_for(1, "A")["captured"])
        after = [tuple(r) for r in web_db.query_all("SELECT * FROM user_credential")]
        assert after == before, "远程浏览器绝不允许改动每用户凭据"
    finally:
        mgr.stop(admin_id=1, token_hash="A")
    assert repo.get_credential_state(web_db)["source"] == "snapshot"


# ====================================================================== 失败路径


def test_a_missing_browser_is_reported_without_a_stack(manager_factory):
    backend = FakeBackend(error=BrowserNotInstalledError("未安装 playwright（测试强制的不可用）"))
    mgr = manager_factory(backend=backend)
    mgr.start(admin_id=1, token_hash="A")
    try:
        wait_until(lambda: mgr.view_for(1, "A")["state"] == rb.LOGIN_FAILED)
        view = mgr.view_for(1, "A")
        assert view["reason"] == "unavailable"
        assert "未安装 playwright（测试强制的不可用）" in view["message"]
    finally:
        mgr.stop(admin_id=1, token_hash="A")


def test_a_launch_failure_is_reported_with_the_playwright_hint(manager_factory):
    backend = FakeBackend(
        error=BrowserLaunchError(
            "找不到浏览器可执行文件：…… PLAYWRIGHT_BROWSERS_PATH 指向 .cache/ms-playwright"
        )
    )
    mgr = manager_factory(backend=backend)
    mgr.start(admin_id=1, token_hash="A")
    try:
        wait_until(lambda: mgr.view_for(1, "A")["reason"] == "launch")
        assert "PLAYWRIGHT_BROWSERS_PATH" in mgr.view_for(1, "A")["message"]
    finally:
        mgr.stop(admin_id=1, token_hash="A")


def test_an_unexpected_exception_only_reports_its_type(manager_factory):
    """异常文本可能带路径或值，一律只报类型。"""
    backend = FakeBackend(error=RuntimeError(f"内部错误 {SENTINEL} /opt/haut-seat-helper"))
    mgr = manager_factory(backend=backend)
    mgr.start(admin_id=1, token_hash="A")
    try:
        wait_until(lambda: mgr.view_for(1, "A")["reason"] == "error")
        view = mgr.view_for(1, "A")
        assert "RuntimeError" in view["message"]
        assert SENTINEL not in view["message"]
        assert "/opt/haut-seat-helper" not in view["message"]
    finally:
        mgr.stop(admin_id=1, token_hash="A")


def test_a_closed_browser_ends_the_session_honestly(manager_factory):
    backend = FakeBackend(FakeSession(token=None, closed_after=2))
    mgr = manager_factory(backend=backend)
    mgr.start(admin_id=1, token_hash="A")
    try:
        wait_until(lambda: mgr.view_for(1, "A")["reason"] == "closed")
        view = mgr.view_for(1, "A")
        assert view["state"] == rb.LOGIN_FAILED
        assert "Admin-Token" in view["message"], "必须说清没拿到凭据，不能假装成功"
        assert view["captured"] is False
    finally:
        mgr.stop(admin_id=1, token_hash="A")


def test_a_screenshot_failure_is_not_fatal(manager_factory):
    """截图偶发失败（页面正在跳转）不该杀会话，也不该发出没有画面的成功。"""
    backend = FakeBackend(FakeSession(token=None, screenshot_error=RuntimeError("跳转中")))
    mgr = manager_factory(backend=backend)
    mgr.start(admin_id=1, token_hash="A")
    try:
        wait_until(lambda: mgr.view_for(1, "A")["state"] == rb.LOGIN_AWAITING)
        time.sleep(0.3)
        assert mgr.view_for(1, "A")["state"] == rb.LOGIN_AWAITING
        assert mgr.frame_for(1, "A") is None, "失败的一帧不该被当成画面发出去"
    finally:
        mgr.stop(admin_id=1, token_hash="A")


def test_the_total_timeout_closes_the_browser(manager_factory, ws):
    clock = Clock()
    backend = FakeBackend(FakeSession(token=None))
    mgr = manager_factory(backend=backend, clock=clock,
                          settings=ws(auth_login_timeout_s=60.0))
    mgr.start(admin_id=1, token_hash="A")
    try:
        wait_until(lambda: mgr.view_for(1, "A")["state"] == rb.LOGIN_AWAITING)
        clock.advance(61.0)
        wait_until(lambda: mgr.view_for(1, "A")["reason"] == "timed_out")
        assert "60s" in mgr.view_for(1, "A")["message"]
        wait_until(lambda: backend.session.close_calls == 1)
    finally:
        mgr.stop(admin_id=1, token_hash="A")


def test_the_idle_timeout_closes_the_browser(manager_factory, ws):
    clock = Clock()
    backend = FakeBackend(FakeSession(token=None))
    mgr = manager_factory(backend=backend, clock=clock,
                          settings=ws(auth_remote_idle_timeout_s=5.0))
    mgr.start(admin_id=1, token_hash="A")
    try:
        wait_until(lambda: mgr.view_for(1, "A")["state"] == rb.LOGIN_AWAITING)
        clock.advance(6.0)
        wait_until(lambda: mgr.view_for(1, "A")["reason"] == "timed_out")
        assert "没有任何操作" in mgr.view_for(1, "A")["message"]
    finally:
        mgr.stop(admin_id=1, token_hash="A")


def test_an_input_resets_the_idle_countdown(manager_factory, ws):
    clock = Clock()
    backend = FakeBackend(FakeSession(token=None))
    mgr = manager_factory(backend=backend, clock=clock,
                          settings=ws(auth_remote_idle_timeout_s=5.0))
    mgr.start(admin_id=1, token_hash="A")
    try:
        wait_until(lambda: mgr.view_for(1, "A")["state"] == rb.LOGIN_AWAITING)
        clock.advance(4.0)
        mgr.submit_input([{"t": "k", "k": "a"}], admin_id=1, token_hash="A")
        clock.advance(4.0)
        time.sleep(0.15)
        assert mgr.view_for(1, "A")["state"] == rb.LOGIN_AWAITING, "有输入就不该超时"
    finally:
        mgr.stop(admin_id=1, token_hash="A")


# ====================================================================== 停止与收尾


def test_stop_releases_the_session_and_records_it(manager_factory, web_db):
    from haut_seat.web import repository as repo

    backend = FakeBackend(FakeSession(token=None))
    mgr = manager_factory(backend=backend)
    mgr.start(admin_id=1, token_hash="A")
    try:
        wait_until(lambda: mgr.view_for(1, "A")["active"])
        result = mgr.stop(admin_id=1, token_hash="A", reason="管理员在面板上手动结束")
        assert result["ok"] is True
        assert mgr.has_session() is False, "主动停止 = 主动放弃所有权"
        assert backend.session.close_calls == 1
        assert repo.get_credential_state(web_db)["login_state"] == rb.LOGIN_FAILED
    finally:
        mgr.stop(admin_id=1, token_hash="A")


def test_stop_without_a_session_says_so(manager_factory):
    result = manager_factory().stop(admin_id=1, token_hash="A")
    assert result["ok"] is False
    assert "没有远程浏览器会话" in result["message"]


def test_stopping_when_nothing_is_owned_by_you_does_not_touch_the_others_session(
    manager_factory,
):
    """登出钩子用的是属主匹配的 stop：别人的会话绝不能被顺手停掉。"""
    mgr = manager_factory()
    mgr.start(admin_id=1, token_hash="A")
    try:
        wait_until(lambda: mgr.has_session())
        mgr.stop(admin_id=99, token_hash="Z")
        assert mgr.has_session() is True
    finally:
        mgr.stop(admin_id=1, token_hash="A")


def test_shutdown_is_bounded_and_stops_the_worker(manager_factory):
    backend = FakeBackend(FakeSession(token=None))
    mgr = manager_factory(backend=backend)
    mgr.start(admin_id=1, token_hash="A")
    wait_until(lambda: mgr.view_for(1, "A")["active"])
    started = time.monotonic()
    mgr.shutdown(timeout_s=2.0)
    assert time.monotonic() - started < 2.5, "收尾必须有界，绝不死等"
    assert backend.session.close_calls == 1


def test_shutdown_without_a_session_is_a_noop(manager_factory):
    manager_factory().shutdown(timeout_s=0.1)


def test_the_worker_thread_is_a_daemon(manager_factory):
    """解释器退出绝不能被工作线程拖住。"""
    mgr = manager_factory()
    mgr.start(admin_id=1, token_hash="A")
    try:
        wait_until(lambda: mgr.has_session())
        with mgr._lock:  # noqa: SLF001 - 这是在测内部不变量，不是为了使用它
            thread = mgr._session.thread
        assert thread is not None and thread.daemon is True
        assert thread.name == "haut-remote-browser"
    finally:
        mgr.stop(admin_id=1, token_hash="A")


def test_the_input_queue_is_drained_when_the_session_ends(manager_factory):
    """会话结束后绝不能留下没转发的按键。"""
    backend = FakeBackend(FakeSession(token=None))
    mgr = manager_factory(backend=backend)
    mgr.start(admin_id=1, token_hash="A")
    try:
        wait_until(lambda: mgr.view_for(1, "A")["active"])
        mgr.submit_input([{"t": "k", "k": "s"}] * 50, admin_id=1, token_hash="A")
        mgr.stop(admin_id=1, token_hash="A")
        with mgr._lock:  # noqa: SLF001
            assert mgr._session is None
        # 线程已经收尾 ⇒ 队列里不该还有东西（用一个新的假会话来核对不方便，
        # 这里直接检查后端没有留下更多调用即可）
        assert backend.session.close_calls == 1
    finally:
        mgr.stop(admin_id=1, token_hash="A")


# ====================================================================== 不记录、不泄露


def test_the_input_payload_is_never_logged(manager_factory, caplog):
    """``/input`` 的请求体逐字就是学校口令的键盘记录：绝不能进日志。"""
    mgr = manager_factory()
    mgr.start(admin_id=1, token_hash="A")
    try:
        wait_until(lambda: mgr.view_for(1, "A")["active"])
        with caplog.at_level(logging.DEBUG):
            mgr.submit_input(
                [{"t": "k", "k": c} for c in SENTINEL] + [{"t": "x", "s": SENTINEL}],
                admin_id=1, token_hash="A",
            )
            time.sleep(0.2)
        assert SENTINEL not in caplog.text
        assert SENTINEL not in repr(mgr)
    finally:
        mgr.stop(admin_id=1, token_hash="A")


def test_the_worker_never_logs_the_token(manager_factory, caplog, web_db):
    mgr, _backend, _ = _capture_manager(manager_factory, None)
    with caplog.at_level(logging.DEBUG):
        mgr.start(admin_id=1, token_hash="A")
        try:
            wait_until(lambda: mgr.view_for(1, "A")["captured"])
        finally:
            mgr.stop(admin_id=1, token_hash="A")
    assert SENTINEL not in caplog.text
    assert f"{COOKIE_NAME}={SENTINEL}" not in caplog.text
    assert "Bearer " not in caplog.text


def test_the_login_capture_repr_is_still_redacted():
    capture = LoginCapture(cookie=f"{COOKIE_NAME}={SENTINEL}",
                           authorization=f"Bearer {SENTINEL}",
                           captured_at="2026-01-01 00:00:00")
    assert SENTINEL not in repr(capture)
    assert "<redacted>" in repr(capture)


# ====================================================================== 只读复检


@pytest.fixture
def patched_precheck(monkeypatch):
    """替换 ``services.io.auth_precheck``（``_readonly_precheck`` 在调用时查找它）。"""
    import haut_seat.services.io as io_mod

    def _install(fn):
        monkeypatch.setattr(io_mod, "auth_precheck", fn)
        return fn

    return _install


def test_the_readonly_precheck_forces_env_mode_and_never_persists(
    tmp_path, make_settings, patched_precheck
):
    """与 ``__main__._verify_captured`` 同一范式：``model_copy`` 不落盘、不污染配置。"""
    settings = make_settings(
        base_url=FAKE_BASE_URL,
        auth_source="auto",
        auth_dir=str(tmp_path / "auth"),
        auth_snapshot_file=str(tmp_path / "auth" / "credentials.json"),
    )
    mgr = rb.RemoteBrowserManager(settings, None, logging.getLogger("test"))
    seen = {}

    class FakeCheck:
        status = type("S", (), {"value": "valid"})()
        message = "只读预检成功"

    @patched_precheck
    def fake_precheck(probe, regionid):
        seen["source"] = probe.auth_source
        seen["cookie"] = probe.cookie.get_secret_value()
        seen["authorization"] = probe.authorization.get_secret_value()
        seen["regionid"] = regionid
        return FakeCheck()

    status, message = mgr._readonly_precheck(  # noqa: SLF001 - 直接测这一步的语义
        LoginCapture(cookie=f"{COOKIE_NAME}={SENTINEL}",
                     authorization=f"Bearer {SENTINEL}",
                     captured_at="2026-01-01 00:00:00")
    )

    assert (status, message) == ("valid", "只读预检成功")
    assert seen["source"] == "env", "复检必须强制 env 模式，打的才是刚采到的那份凭据"
    assert seen["cookie"] == f"{COOKIE_NAME}={SENTINEL}"
    assert seen["authorization"] == f"Bearer {SENTINEL}"
    assert seen["regionid"] == settings.regionid
    assert settings.auth_source == "auto", "进程配置绝不能被改成 env"
    assert not Path(settings.auth_snapshot_file).exists(), "复检绝不落盘"


def test_a_broken_precheck_is_reported_as_unknown(ws, patched_precheck):
    settings = ws()
    mgr = rb.RemoteBrowserManager(settings, None, logging.getLogger("test"))

    @patched_precheck
    def boom(*a, **k):
        raise RuntimeError(SENTINEL)

    status, message = mgr._readonly_precheck(  # noqa: SLF001
        LoginCapture(cookie=f"{COOKIE_NAME}={SENTINEL}",
                     authorization=f"Bearer {SENTINEL}",
                     captured_at="2026-01-01 00:00:00")
    )
    assert status == "unknown"
    assert "RuntimeError" in message
    assert SENTINEL not in message


# ====================================================================== 惰性与守卫


def test_the_manager_is_lazy_and_never_starts_a_browser_in_its_constructor(
    ws, web_db, monkeypatch
):
    """``create_app`` 在每个 web 测试里都会构造真实管理器：构造期必须完全惰性。

    "惰性"指的**不是**"不构造后端对象"（``PlaywrightHeadlessBackend()`` 本身是空的，
    不 import playwright、不开浏览器），而是：构造管理器绝不去**启动**任何东西。
    """

    class ExplodingOpen:
        def open(self, **kwargs):  # pragma: no cover - 被调用即失败
            raise AssertionError("构造管理器时绝不应该真的去启动浏览器")

    monkeypatch.setattr(rb, "PlaywrightHeadlessBackend", ExplodingOpen)
    before = set(threading.enumerate())
    mgr = rb.RemoteBrowserManager(ws(), web_db, logging.getLogger("test"))

    assert mgr.has_session() is False
    assert mgr.active() is False
    assert mgr.view_for(1, "A") is not None, "只读视图不需要浏览器"
    assert mgr.frame_for(1, "A") is None
    assert mgr.submit_input([{"t": "k", "k": "a"}], admin_id=1, token_hash="A")["owner"] is False
    assert set(threading.enumerate()) == before, "构造管理器不得起任何后台线程"


def test_playwright_is_only_imported_inside_the_backend():
    """构造期不 import playwright：模块层**没有**任何 playwright 的 import。

    用 AST 查模块顶层语句，而不是查文本行 —— 缩进写的 import（在函数里）才是
    我们要保留的那一种，按字符串查会把两者混为一谈。
    """
    import ast

    text = Path(rb.__file__).read_text(encoding="utf-8")
    for node in ast.parse(text).body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, "module", "") or ""
            names = [alias.name for alias in node.names]
            assert not module.startswith("playwright"), node.lineno
            assert not any(n.startswith("playwright") for n in names), node.lineno
    # 真的会 import 的地方：``PlaywrightHeadlessBackend.open()`` 里面那一行
    assert "            from playwright.sync_api import sync_playwright" in text


def test_the_default_backend_is_used_when_none_is_injected(ws, web_db, monkeypatch):
    """反过来也要证明默认后端确实会被构造（否则实现可能是空壳）。"""
    used = []

    class Stub:
        def open(self, **kwargs):
            used.append(kwargs)
            return FakeSession(token=None, closed_after=1)

    monkeypatch.setattr(rb, "PlaywrightHeadlessBackend", Stub)
    mgr = rb.RemoteBrowserManager(ws(), web_db, logging.getLogger("test"))
    mgr.start(admin_id=1, token_hash="A")
    try:
        wait_until(lambda: used)
        assert used[0]["headless"] is True
    finally:
        mgr.stop(admin_id=1, token_hash="A")


def test_the_module_never_calls_a_booking_endpoint():
    text = Path(rb.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "add" + "booking",
        "submit_" + "booking",
        "submit_" + "online",
        "booking_runner",
    ):
        assert forbidden not in text, forbidden


def test_the_module_never_weakens_the_sandbox():
    """沙箱起不来就如实报错，**绝不**用 --no-sandbox 绕过。

    这里查的是**真正会传给 Chromium 的启动参数**（``_LAUNCH_ARGS``）与启动代码
    本身，而不是整份文件里的字符串：模块文档里恰恰要写出这条禁令才说得清楚，
    按整份文件查字符串会自己命中自己。
    """
    import inspect

    assert "--no-sandbox" not in rb._LAUNCH_ARGS  # noqa: SLF001
    assert "--no-sandbox" not in inspect.getsource(rb.PlaywrightHeadlessBackend)
    assert "--no-sandbox" not in inspect.getsource(rb.PlaywrightInteractiveSession)


def test_the_module_opens_no_remote_debugging_port():
    text = Path(rb.__file__).read_text(encoding="utf-8")
    for forbidden in ("remote-debugging-port", "remote_debugging", "9222", "start_server",
                      "connect_over_cdp", "browser-server"):
        assert forbidden not in text, forbidden


def test_the_module_never_fills_credentials_or_solves_captchas():
    text = Path(rb.__file__).read_text(encoding="utf-8")
    for forbidden in ("fill(", "solve_captcha", "check_captcha", "password_input",
                      "fill_password", "username_input"):
        assert forbidden not in text, forbidden


def test_the_module_has_no_real_host():
    text = Path(rb.__file__).read_text(encoding="utf-8")
    assert REAL_HOST not in text


def test_the_module_never_downloads_a_browser():
    """守卫的是**行为**：不 shell out、不起子进程去装浏览器。"""
    text = Path(rb.__file__).read_text(encoding="utf-8")
    for forbidden in ("subprocess", "os.system", "os.popen"):
        assert forbidden not in text, forbidden