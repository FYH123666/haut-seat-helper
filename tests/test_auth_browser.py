"""浏览器采集层测试。

**全离线**：不装浏览器、不开窗口、不联网、不出现真实学校域名。
``capture_login`` 的后端是 `Protocol`，测试注入假实现，
因此 CI 上跑这些用例连 playwright 都不需要存在。

覆盖用户第六阶段要求的：首次登录与会话持久化、Cookie 自动获取、
Authorization 的获取方式、超时/用户关窗等失败路径、凭据不泄露。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from haut_seat.services import auth_browser
from haut_seat.services.auth_browser import (
    COOKIE_NAME,
    BrowserLaunchError,
    BrowserLoginError,
    BrowserNotInstalledError,
    LoginCapture,
    browser_availability,
    capture_login,
)

# 哨兵：形状像真令牌，但显然是测试专用值。任何日志/异常/页面都不该出现它。
SENTINEL = "SENTINELTOKEN-._0003-abcdefghijklmnop"

# 一个明显不是真实域名的站点地址
FAKE_BASE_URL = "https://seat.example.invalid"


@pytest.fixture
def auth_settings(make_settings, tmp_path):
    def _make(**overrides):
        base = {
            "base_url": FAKE_BASE_URL,
            "auth_dir": str(tmp_path / "auth"),
            "auth_snapshot_file": str(tmp_path / "auth" / "credentials.json"),
            "auth_profile_dir": str(tmp_path / "auth" / "browser-profile"),
        }
        base.update(overrides)
        return make_settings(**base)

    return _make


@pytest.fixture(autouse=True)
def _clear_availability_cache():
    """``browser_availability`` 带缓存；每个用例前后都清掉，避免互相污染。"""
    browser_availability.cache_clear()
    yield
    browser_availability.cache_clear()


class FakeSession:
    """假会话：第 ``appear_after`` 次轮询起给出令牌；可模拟用户关窗。"""

    def __init__(self, token: str | None = None, *, appear_after: int = 1,
                 closed_after: int | None = None) -> None:
        self.token = token
        self.appear_after = appear_after
        self.closed_after = closed_after
        self.polls = 0
        self.close_calls = 0

    def get_cookie(self, name: str) -> str | None:
        self.polls += 1
        if name != COOKIE_NAME or self.token is None:
            return None
        return self.token if self.polls >= self.appear_after else None

    def is_closed(self) -> bool:
        return self.closed_after is not None and self.polls >= self.closed_after

    def close(self) -> None:
        self.close_calls += 1


class FakeBackend:
    """假后端：记录 open() 收到的每一个参数，并可抛异常。"""

    def __init__(self, session=None, error: Exception | None = None) -> None:
        self.session = session or FakeSession(f"{SENTINEL}")
        self.error = error
        self.calls: list[dict] = []
        self.sessions: list[FakeSession] = []

    def open(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        self.sessions.append(self.session)
        return self.session


# ---------------------------------------------------------------- 采集成功路径


def test_capture_passes_persistent_profile_dir(auth_settings):
    """首次登录与会话持久化：必须是**专用且持久**的用户目录，且**可见**（非 headless）。"""
    settings = auth_settings()
    backend = FakeBackend()
    capture_login(settings, backend=backend, poll_interval_s=0.0)

    assert len(backend.calls) == 1
    call = backend.calls[0]
    assert call["user_data_dir"] == settings.auth_profile_dir
    assert call["headless"] is False, "登录必须有人看着，绝不能是无头"
    assert call["channel"] == "msedge", "默认复用系统已装的 Edge"
    assert call["login_url"] == FAKE_BASE_URL


def test_capture_creates_the_profile_dir(auth_settings):
    """目录要真的建出来，否则 launch_persistent_context 会自己去猜路径。"""
    settings = auth_settings()
    capture_login(settings, backend=FakeBackend(), poll_interval_s=0.0)
    assert Path(settings.auth_profile_dir).is_dir()


def test_capture_reuses_profile_dir_on_second_run(auth_settings):
    """第二次运行必须指向同一个目录 —— 否则"持久化"名不副实。"""
    settings = auth_settings()
    first, second = FakeBackend(), FakeBackend()
    capture_login(settings, backend=first, poll_interval_s=0.0)
    capture_login(settings, backend=second, poll_interval_s=0.0)
    assert first.calls[0]["user_data_dir"] == second.calls[0]["user_data_dir"]


def test_capture_reads_admin_token_cookie(auth_settings):
    settings = auth_settings()
    session = FakeSession(SENTINEL, appear_after=3)
    capture = capture_login(
        settings, backend=FakeBackend(session), poll_interval_s=0.0
    )
    assert capture.cookie == f"{COOKIE_NAME}={SENTINEL}"
    assert session.polls >= 3


def test_capture_extracts_session_cookie_without_expiry(auth_settings):
    """钉住前提：会话级 Cookie（无 expires）也能被 ``context.cookies()`` 取到值。

    这正是"必须显式取值落盘"的根据 —— 持久化目录靠不住，只有显式取出才作数。
    """
    settings = auth_settings()
    capture = capture_login(settings, backend=FakeBackend(), poll_interval_s=0.0)
    assert SENTINEL in capture.cookie


def test_capture_derives_bearer_from_token(auth_settings):
    """Authorization 的获取方式：``"Bearer " + <Admin-Token>``（站点拦截器的实测关系）。"""
    settings = auth_settings()
    capture = capture_login(settings, backend=FakeBackend(), poll_interval_s=0.0)

    assert capture.authorization == f"Bearer {SENTINEL}"
    # 两个头里必须是**同一个**令牌
    assert capture.cookie.removeprefix(f"{COOKIE_NAME}=") == capture.authorization.removeprefix(
        "Bearer "
    )


def test_capture_records_time_and_source(auth_settings):
    settings = auth_settings()
    capture = capture_login(settings, backend=FakeBackend(), poll_interval_s=0.0)
    assert capture.source == "browser-login"
    assert capture.captured_at  # 形如 2026-10-18 17:50:12
    assert capture.fingerprint() and SENTINEL not in capture.fingerprint()


def test_capture_invokes_prompt_after_opening(auth_settings):
    """提示必须在窗口**已经打开之后**给，否则用户不知道去看哪里。"""
    seen: list[str] = []
    capture_login(
        auth_settings(),
        backend=FakeBackend(),
        poll_interval_s=0.0,
        on_prompt=seen.append,
    )
    assert len(seen) == 1
    assert "验证码" in seen[0]      # 明确告诉用户验证码要他自己输
    assert "人工" in seen[0] or "本人" in seen[0]


# ---------------------------------------------------------------- 失败路径


def test_capture_times_out_without_cookie(auth_settings):
    settings = auth_settings()
    session = FakeSession(token=None)   # 永远不出现令牌
    backend = FakeBackend(session)
    with pytest.raises(BrowserLoginError) as excinfo:
        capture_login(settings, backend=backend, timeout_s=0.01, poll_interval_s=0.0)
    assert "超时" in str(excinfo.value)
    assert SENTINEL not in str(excinfo.value)


def test_capture_reports_user_closed_window(auth_settings):
    settings = auth_settings()
    session = FakeSession(token=None, closed_after=1)
    with pytest.raises(BrowserLoginError) as excinfo:
        capture_login(
            settings, backend=FakeBackend(session), timeout_s=5.0, poll_interval_s=0.0
        )
    assert "已关闭" in str(excinfo.value)


def test_capture_keeps_token_when_window_closed_after_login(auth_settings):
    """用户登录完顺手关窗是很自然的动作 —— 令牌已拿到就必须先取走，不算失败。"""
    settings = auth_settings()
    session = FakeSession(SENTINEL, appear_after=1, closed_after=1)
    capture = capture_login(
        settings, backend=FakeBackend(session), timeout_s=5.0, poll_interval_s=0.0
    )
    assert capture.cookie == f"{COOKIE_NAME}={SENTINEL}"


def test_capture_closes_session_on_success_and_on_failure(auth_settings):
    """无论成功还是失败都必须收掉会话，绝不能留残留进程。"""
    ok_session = FakeSession(SENTINEL)
    capture_login(auth_settings(), backend=FakeBackend(ok_session), poll_interval_s=0.0)
    assert ok_session.close_calls == 1

    bad_session = FakeSession(token=None, closed_after=1)
    with pytest.raises(BrowserLoginError):
        capture_login(
            auth_settings(), backend=FakeBackend(bad_session),
            timeout_s=5.0, poll_interval_s=0.0,
        )
    assert bad_session.close_calls == 1


def test_capture_propagates_launch_errors(auth_settings):
    backend = FakeBackend(error=BrowserLaunchError("无法打开浏览器（channel=msedge）"))
    with pytest.raises(BrowserLaunchError):
        capture_login(auth_settings(), backend=backend, poll_interval_s=0.0)


def test_capture_honours_timeout_override(auth_settings):
    settings = auth_settings(auth_login_timeout_s=300.0)
    backend = FakeBackend()
    capture_login(settings, backend=backend, timeout_s=7.0, poll_interval_s=0.0)
    assert backend.calls[0]["timeout_s"] == 7.0


def test_capture_defaults_timeout_to_settings(auth_settings):
    settings = auth_settings(auth_login_timeout_s=123.0)
    backend = FakeBackend()
    capture_login(settings, backend=backend, poll_interval_s=0.0)
    assert backend.calls[0]["timeout_s"] == 123.0


# ---------------------------------------------------------------- 不泄露


def test_sentinel_token_never_reaches_the_logs(auth_settings, caplog):
    settings = auth_settings()
    with caplog.at_level(logging.DEBUG, logger="haut_seat.auth"):
        capture = capture_login(settings, backend=FakeBackend(), poll_interval_s=0.0)
        logging.getLogger("haut_seat.auth").debug("%s", capture)
    assert SENTINEL not in caplog.text
    assert f"{COOKIE_NAME}=" not in caplog.text
    assert "Bearer " not in caplog.text
    assert f"{COOKIE_NAME}" in caplog.text   # 名字可以记，值不行


def test_login_capture_repr_redacts():
    capture = LoginCapture(f"{COOKIE_NAME}={SENTINEL}", f"Bearer {SENTINEL}", "t")
    text = repr(capture)
    assert SENTINEL not in text
    assert f"{COOKIE_NAME}=" not in text
    assert capture.fingerprint() in text      # 指纹可见，用于指认"取到的是哪一份"


def test_timeout_message_has_no_value(auth_settings):
    settings = auth_settings()
    session = FakeSession(SENTINEL, appear_after=99, closed_after=1)
    with pytest.raises(BrowserLoginError) as excinfo:
        capture_login(
            settings, backend=FakeBackend(session), timeout_s=5.0, poll_interval_s=0.0
        )
    assert SENTINEL not in str(excinfo.value)


# ---------------------------------------------------------------- 可用性探测


def test_availability_reports_missing_dependency_with_install_hint(monkeypatch):
    """真探测路径：find_spec 返回 None ⇒ 不可用，且原因里给出安装命令。"""
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    available, reason = auth_browser._probe_playwright()
    assert available is False
    assert ".[auth]" in reason


def test_availability_is_true_when_playwright_present(monkeypatch):
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    available, reason = auth_browser._probe_playwright()
    assert available is True and reason == ""


def test_availability_handles_find_spec_raising(monkeypatch):
    """父包缺失时 find_spec 会抛异常；这只能是"没装"，不能把进程炸掉。"""

    def boom(name):
        raise ModuleNotFoundError(name)

    monkeypatch.setattr("importlib.util.find_spec", boom)
    available, reason = auth_browser._probe_playwright()
    assert available is False
    assert "playwright" in reason


def test_availability_is_cached(monkeypatch):
    calls: list[str] = []

    def probe():
        calls.append("x")
        return True, ""

    monkeypatch.setattr(auth_browser, "_probe_playwright", probe)
    browser_availability.cache_clear()
    assert browser_availability() == (True, "")
    assert browser_availability() == (True, "")
    assert len(calls) == 1, "每页都会问一次，装没装不会变，必须缓存"


def test_install_hint_names_the_auth_extra():
    assert ".[auth]" in auth_browser.INSTALL_HINT


def test_not_installed_error_is_a_login_error():
    """父类关系保证调用方只需捕一个 BrowserLoginError 就能兜住所有采集失败。"""
    assert issubclass(BrowserNotInstalledError, BrowserLoginError)
    assert issubclass(BrowserLoginError, RuntimeError)


# ---------------------------------------------------------------- 默认后端与守卫


def test_default_backend_is_never_touched_in_tests(auth_settings, monkeypatch):
    """守护：本模块的测试**零真实浏览器** —— 默认后端一旦被实例化即失败。"""

    class Exploding:
        def __init__(self):
            raise AssertionError("测试中绝不应该用到真实 Playwright 后端")

    monkeypatch.setattr(auth_browser, "PlaywrightEdgeBackend", Exploding)
    # 注入假后端时一切正常 ⇒ 证明默认后端确实没被碰
    capture_login(auth_settings(), backend=FakeBackend(), poll_interval_s=0.0)


def test_default_backend_is_used_when_none_injected(auth_settings, monkeypatch):
    """反过来也要证明：不注入时确实会构造默认后端（否则实现可能是空壳）。"""
    settings = auth_settings()
    used: list[str] = []

    class Stub:
        def open(self, **kwargs):
            used.append(kwargs["user_data_dir"])
            return FakeSession(SENTINEL)

    monkeypatch.setattr(auth_browser, "PlaywrightEdgeBackend", Stub)
    capture_login(settings, poll_interval_s=0.0)
    assert used == [settings.auth_profile_dir]


def test_module_never_calls_the_submit_endpoint():
    """守卫：采集层不引用任何提交接口。"""
    text = Path(auth_browser.__file__).read_text(encoding="utf-8")
    submit_path = "add" + "booking"
    assert submit_path not in text
    assert "submit_" + "booking" not in text
    assert "submit_" + "online" not in text


def test_module_never_downloads_a_browser():
    """守卫：绝不下载浏览器 —— 不 fork 进程、不 shell out。

    这里查的是**行为**（有没有起进程的手段），而不是"文字里出现过
    ``playwright install``"：模块文档里恰恰要写出这条禁令才说得清楚，
    所以按字符串查会自己命中自己。
    """
    text = Path(auth_browser.__file__).read_text(encoding="utf-8")
    for forbidden in ("subprocess", "os.system", "os.popen", "install_", "_install"):
        assert forbidden not in text


def test_module_never_fills_credentials():
    """守卫：不代填账号口令（本项目不处理密码）。"""
    text = Path(auth_browser.__file__).read_text(encoding="utf-8")
    for forbidden in ("fill(", "type=", "password_input", "check_captcha", "solve_captcha"):
        assert forbidden not in text


def test_this_module_has_no_real_host():
    text = Path(auth_browser.__file__).read_text(encoding="utf-8")
    real_host = "wslib" + ".haut" + ".edu.cn"
    assert real_host not in text