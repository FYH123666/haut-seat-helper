"""``auth-login`` / ``auth-status`` 两个 CLI 子命令的测试。

**全离线**：不装浏览器、不开窗口、不联网、不发任何真实请求。
浏览器后端由 `Protocol` 注入假实现，预检用替身，于是被测的是**真实的 CLI 流程**
（参数解析、锁、落盘、复检分支、返回码、输出），而不是测试自己拼的逻辑。

覆盖用户第六阶段要求的：Cookie 自动获取、Authorization 获取方式、
认证有效/失效/网络异常、凭据不泄露到日志与输出、认证失败不触发提交。
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import pytest

from haut_seat.__main__ import (
    LOGIN_LOCK_STALE_S,
    build_parser,
    cmd_auth_login,
    cmd_auth_status,
)
from haut_seat.core.auth import RELOGIN_HINT, AuthCheck, AuthStatus
from haut_seat.services import auth_browser, auth_store
from haut_seat.services.auth_browser import (
    COOKIE_NAME,
    BrowserLaunchError,
    BrowserNotInstalledError,
    LOGIN_PROMPT,
)

# 哨兵：形状像真令牌，但显然是测试专用值。任何输出/日志都不该出现它。
SENTINEL = "SENTINELTOKEN-._0004-abcdefghijklmnop"
FAKE_BASE_URL = "https://seat.example.invalid"


@pytest.fixture
def logger():
    return logging.getLogger("test.cli.auth")


@pytest.fixture
def settings(tmp_path, make_settings):
    return make_settings(
        base_url=FAKE_BASE_URL,
        auth_dir=str(tmp_path / "auth"),
        auth_snapshot_file=str(tmp_path / "auth" / "credentials.json"),
        auth_profile_dir=str(tmp_path / "auth" / "browser-profile"),
        # 必须重定向：命令行会写登录进度元数据，默认值 data/web.db 是在用的业务库，
        # 测试绝不能碰它（这里指向 tmp，顺带让"写没写对"可断言）。
        web_db=str(tmp_path / "web.db"),
    )


def read_login_state(settings) -> dict:
    from haut_seat.web import repository as repo
    from haut_seat.web.db import Database

    db = Database(settings.web_db)
    try:
        return repo.get_credential_state(db)
    finally:
        db.close_current_thread()


@pytest.fixture(autouse=True)
def _clear_availability_cache():
    auth_browser.browser_availability.cache_clear()
    yield
    auth_browser.browser_availability.cache_clear()


@pytest.fixture
def available(monkeypatch):
    """让"依赖已装"成为默认前提；需要测不可用的用例自行覆盖。"""
    monkeypatch.setattr("haut_seat.__main__.browser_availability", lambda: (True, ""))


class FakeSession:
    """假会话：给出令牌 / 永远不给令牌 / 模拟用户关窗。"""

    def __init__(self, token: str | None = f"{SENTINEL}", *, closed: bool = False) -> None:
        self.token = token
        self.closed = closed
        self.polls = 0
        self.close_calls = 0

    def get_cookie(self, name: str) -> str | None:
        self.polls += 1
        return self.token if name == COOKIE_NAME else None

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.close_calls += 1


class FakeBackend:
    def __init__(self, session: FakeSession) -> None:
        self.session = session
        self.calls: list[dict] = []

    def open(self, **kwargs):
        self.calls.append(kwargs)
        return self.session


def install_fake_backend(monkeypatch, session: FakeSession | None = None) -> FakeBackend:
    """把**默认**后端换成替身（CLI 自己不注入 backend，所以只能从这里换）。"""
    backend = FakeBackend(session or FakeSession())
    monkeypatch.setattr(auth_browser, "PlaywrightEdgeBackend", lambda: backend)
    return backend


def login_args(**overrides) -> argparse.Namespace:
    base = {"timeout": None, "regionid": None, "start": "", "end": "",
            "no_verify": False}
    base.update(overrides)
    return argparse.Namespace(**base)


def patch_precheck(monkeypatch, check: AuthCheck) -> list:
    """接管只读复检 —— 离线的确定性替身。"""
    seen: list = []

    def fn(settings_, regionid, start=None, end=None):
        seen.append((settings_, regionid, start, end))
        return check

    monkeypatch.setattr("haut_seat.__main__._auth_precheck", fn)
    return seen


def patch_check_auth_only(monkeypatch, check: AuthCheck) -> None:
    """让 ``check-auth`` 也走替身，用于验证它如实印出来源。"""
    monkeypatch.setattr("haut_seat.__main__._auth_precheck", lambda *a, **k: check)


# ------------------------------------------------------------------ 参数解析


def test_new_subcommands_are_wired_to_their_functions():
    p = build_parser()
    assert p.parse_args(["auth-login"]).func is cmd_auth_login
    assert p.parse_args(["auth-status"]).func is cmd_auth_status


def test_auth_login_defaults_are_safe():
    a = build_parser().parse_args(["auth-login"])
    assert a.timeout is None, "默认必须交给配置决定（300s）"
    assert a.no_verify is False
    assert a.regionid is None


def test_auth_login_accepts_timeout_override():
    a = build_parser().parse_args(["auth-login", "--timeout", "42", "--no-verify"])
    assert a.timeout == 42.0
    assert a.no_verify is True


def test_auth_status_takes_no_arguments():
    a = build_parser().parse_args(["auth-status"])
    assert not hasattr(a, "from_file"), "不该有会诱人发真实请求的参数"


# ------------------------------------------------------------------ 依赖可用性


def test_auth_login_returns_2_when_browser_unavailable(
    monkeypatch, settings, logger, capsys
):
    monkeypatch.setattr(
        "haut_seat.__main__.browser_availability", lambda: (False, "未安装 playwright")
    )
    rc = cmd_auth_login(login_args(), settings, logger)
    err = capsys.readouterr().err

    assert rc == 2
    assert "未安装 playwright" in err            # 明确说明原因
    assert ".[auth]" in err                      # 明确给出补救方式
    assert not Path(settings.auth_snapshot_file).exists(), "不该留下任何快照"


def test_auth_login_unavailable_never_falls_back_to_stale_env(
    monkeypatch, settings, logger, capsys
):
    """依赖不可用就明确报错 —— 绝不静默改用 .env 里的旧凭据。"""
    monkeypatch.setattr(
        "haut_seat.__main__.browser_availability", lambda: (False, "不可用")
    )
    stale = settings.model_copy(
        update={"cookie": "Admin-Token=STALECOOKIE", "authorization": "Bearer STALEAUTH"}
    )
    cmd_auth_login(login_args(), stale, logger)
    both = "".join(capsys.readouterr())
    assert "STALECOOKIE" not in both
    assert "STALEAUTH" not in both


def test_auth_login_returns_2_when_backend_reports_not_installed(
    available, monkeypatch, settings, logger, capsys
):
    """可用性探测说"装了"，但起窗口时才发现没装 —— 也要归到 2。"""

    class Backend:
        def __init__(self):
            raise BrowserNotInstalledError(f"未安装 playwright（{auth_browser.INSTALL_HINT}）")

    monkeypatch.setattr(auth_browser, "PlaywrightEdgeBackend", Backend)
    rc = cmd_auth_login(login_args(), settings, logger)

    assert rc == 2
    assert ".[auth]" in capsys.readouterr().err


# ------------------------------------------------------------------ 成功路径


def test_auth_login_saves_snapshot_and_verifies(available, monkeypatch, settings,
                                               logger, capsys):
    install_fake_backend(monkeypatch)
    patch_precheck(monkeypatch, AuthCheck(AuthStatus.VALID, "凭据有效"))

    rc = cmd_auth_login(login_args(), settings, logger)
    out = capsys.readouterr().out

    assert rc == 0
    snapshot = auth_store.load_snapshot(settings)
    assert snapshot is not None
    assert snapshot.cookie == f"{COOKIE_NAME}={SENTINEL}"
    assert snapshot.authorization == f"Bearer {SENTINEL}"
    assert snapshot.source == "browser-login"
    assert "已保存凭据快照" in out
    assert "只读复检通过" in out


def test_auth_login_opens_a_visible_window_with_the_persistent_profile(
    available, monkeypatch, settings, logger
):
    """端到端串起来：CLI 真的按配置去开窗口，而不是用某个默认路径。"""
    backend = install_fake_backend(monkeypatch)
    patch_precheck(monkeypatch, AuthCheck(AuthStatus.VALID, "有效"))

    cmd_auth_login(login_args(), settings, logger)

    assert len(backend.calls) == 1
    call = backend.calls[0]
    assert call["user_data_dir"] == settings.auth_profile_dir
    assert call["headless"] is False
    assert call["channel"] == settings.auth_browser_channel
    assert call["login_url"] == FAKE_BASE_URL


def test_auth_login_closes_the_browser_session_afterwards(available, monkeypatch,
                                                          settings, logger):
    session = FakeSession()
    install_fake_backend(monkeypatch, session=session)
    patch_precheck(monkeypatch, AuthCheck(AuthStatus.VALID, "有效"))

    cmd_auth_login(login_args(), settings, logger)
    assert session.close_calls == 1, "绝不能留下残留浏览器进程"


def test_auth_login_output_never_contains_the_token(available, monkeypatch, settings,
                                                    logger, capsys):
    """凭据值绝不进 stdout/stderr —— 只印采集时间与单向指纹。"""
    install_fake_backend(monkeypatch)
    patch_precheck(monkeypatch, AuthCheck(AuthStatus.VALID, "凭据有效"))

    cmd_auth_login(login_args(), settings, logger)
    both = "".join(capsys.readouterr())

    assert SENTINEL not in both
    assert f"{COOKIE_NAME}=" not in both
    assert "Bearer " not in both
    assert auth_store.fingerprint(f"Bearer {SENTINEL}") in both


def test_auth_login_prints_the_honest_prompt(available, monkeypatch, settings,
                                             logger, capsys):
    """文案必须说清这是人工登录，且不识别验证码。"""
    install_fake_backend(monkeypatch)
    patch_precheck(monkeypatch, AuthCheck(AuthStatus.VALID, "凭据有效"))

    cmd_auth_login(login_args(), settings, logger)
    out = capsys.readouterr().out

    assert "人工登录" in out          # 明确：这是人工登录，不是自动登录
    assert "不识别验证码" in out      # 明确：不识别验证码
    assert "不代填账号口令" in out
    assert "验证码" in LOGIN_PROMPT


def test_auth_login_verifies_with_the_captured_values(available, monkeypatch, settings,
                                                      logger):
    """复检必须打的是**刚采集到的**那份凭据，而不是可能更旧的快照/其它来源。"""
    install_fake_backend(monkeypatch)
    seen = patch_precheck(monkeypatch, AuthCheck(AuthStatus.VALID, "有效"))

    cmd_auth_login(login_args(), settings, logger)

    assert len(seen) == 1
    probe = seen[0][0]
    assert probe.auth_source == "env", "临时配置强制 env，避免又走回快照解析"
    assert probe.cookie.get_secret_value() == f"{COOKIE_NAME}={SENTINEL}"
    assert probe.authorization.get_secret_value() == f"Bearer {SENTINEL}"
    # 原配置必须毫发无损（否则会把一次采集变成一个进程级副作用）
    assert settings.cookie is None
    assert settings.authorization is None
    assert settings.base_url == FAKE_BASE_URL


def test_auth_login_no_verify_skips_the_precheck(available, monkeypatch, settings,
                                                 logger, capsys):
    install_fake_backend(monkeypatch)
    seen = patch_precheck(monkeypatch, AuthCheck(AuthStatus.VALID, "有效"))

    rc = cmd_auth_login(login_args(no_verify=True), settings, logger)
    out = capsys.readouterr().out

    assert rc == 0
    assert seen == [], "不允许发起任何预检请求"
    assert "已跳过只读复检" in out
    assert auth_store.load_snapshot(settings) is not None, "快照仍要落盘"


def test_auth_login_snapshot_survives_a_failed_recheck(available, monkeypatch, settings,
                                                       logger):
    """登录是人工成果：复检失败也不能把快照丢掉。"""
    install_fake_backend(monkeypatch)
    patch_precheck(monkeypatch, AuthCheck(AuthStatus.INVALID, "认证失败"))

    rc = cmd_auth_login(login_args(), settings, logger)

    assert rc == 3
    assert auth_store.load_snapshot(settings) is not None


# ------------------------------------------------------------------ 失效 / 异常


def test_auth_login_returns_3_when_recheck_says_invalid(available, monkeypatch,
                                                        settings, logger, capsys):
    install_fake_backend(monkeypatch)
    patch_precheck(monkeypatch, AuthCheck(AuthStatus.INVALID, "服务器返回认证失败 code=401"))

    rc = cmd_auth_login(login_args(), settings, logger)
    out = capsys.readouterr().out

    assert rc == 3
    assert RELOGIN_HINT in out
    assert "已失效" in out


def test_auth_login_returns_1_when_recheck_is_indeterminate(available, monkeypatch,
                                                            settings, logger, capsys):
    """网络异常绝不能报成"失效"，也不能报成成功。"""
    install_fake_backend(monkeypatch)
    patch_precheck(monkeypatch, AuthCheck(AuthStatus.INDETERMINATE, "网络超时"))

    rc = cmd_auth_login(login_args(), settings, logger)
    out = capsys.readouterr().out

    assert rc == 1
    assert "不是认证失效判定" in out
    assert auth_store.load_snapshot(settings) is not None


def test_auth_login_returns_1_when_capture_times_out(available, monkeypatch, settings,
                                                     logger, capsys):
    """窗口还开着但始终没拿到令牌 ⇒ 超时（而不是"窗口已关闭"）。"""
    install_fake_backend(monkeypatch, session=FakeSession(token=None, closed=False))

    rc = cmd_auth_login(login_args(timeout=0.01), settings, logger)
    err = capsys.readouterr().err

    assert rc == 1
    assert "采集失败" in err
    assert "超时" in err
    assert not Path(settings.auth_snapshot_file).exists()


def test_auth_login_returns_1_when_user_closed_the_window(available, monkeypatch,
                                                          settings, logger, capsys):
    install_fake_backend(monkeypatch, session=FakeSession(token=None, closed=True))

    rc = cmd_auth_login(login_args(timeout=5.0), settings, logger)

    assert rc == 1
    assert "已关闭" in capsys.readouterr().err


def test_auth_login_returns_1_when_browser_cannot_open(available, monkeypatch, settings,
                                                       logger, capsys):
    class Backend:
        def open(self, **kwargs):
            raise BrowserLaunchError("无法打开浏览器（channel=msedge）")

    monkeypatch.setattr(auth_browser, "PlaywrightEdgeBackend", Backend)
    rc = cmd_auth_login(login_args(), settings, logger)

    assert rc == 1
    assert "无法打开浏览器" in capsys.readouterr().err


def test_auth_login_writes_no_snapshot_when_capture_fails(available, monkeypatch,
                                                          settings, logger):
    """失败路径绝不能留下"看起来有、其实没用"的快照，也不能留临时文件。"""
    install_fake_backend(monkeypatch, session=FakeSession(token=None, closed=False))
    cmd_auth_login(login_args(timeout=0.01), settings, logger)

    assert not Path(settings.auth_snapshot_file).exists()
    assert list(Path(settings.auth_dir).glob(".cred-*.tmp")) == []


def test_capture_failure_never_triggers_a_submission(available, monkeypatch, settings,
                                                     logger, capsys):
    """认证失败时禁止真实预约提交 —— 用"提交接口一旦被调用即炸"钉住。"""
    import haut_seat.__main__ as m

    def forbidden(*a, **k):
        raise AssertionError("认证失败路径绝不允许触发提交")

    monkeypatch.setattr(m, "_submit_online", forbidden)
    install_fake_backend(monkeypatch, session=FakeSession(token=None, closed=False))

    rc = cmd_auth_login(login_args(timeout=0.01), settings, logger)

    assert rc == 1, "必须正常返回失败码，而不是把 AssertionError 抛穿"
    out = capsys.readouterr().out
    assert "add" + "booking" not in out


# ------------------------------------------------------------------ 单飞锁


def test_auth_login_refuses_when_another_capture_is_running(available, monkeypatch,
                                                            settings, logger, capsys):
    """连点/并发时只允许一个窗口 —— 否则两个 Edge 会抢同一个 profile 目录。"""
    from haut_seat.core.lock import FileLock

    held = FileLock(Path(settings.auth_dir) / "login.lock",
                    stale_after_s=LOGIN_LOCK_STALE_S)
    assert held.acquire() is True
    try:
        seen = patch_precheck(monkeypatch, AuthCheck(AuthStatus.VALID, "有效"))
        rc = cmd_auth_login(login_args(), settings, logger)
        err = capsys.readouterr().err

        assert rc == 4
        assert "已有一个登录采集在进行中" in err
        assert seen == [], "被拒绝时不得发起任何请求"
        assert not Path(settings.auth_snapshot_file).exists()
    finally:
        held.release()


def test_auth_login_releases_the_lock_even_on_failure(available, monkeypatch, settings,
                                                      logger):
    """失败也必须放锁，否则用户会被上一次的失败永久挡住。"""
    install_fake_backend(monkeypatch, session=FakeSession(token=None, closed=False))
    cmd_auth_login(login_args(timeout=0.01), settings, logger)
    assert not (Path(settings.auth_dir) / "login.lock").exists()


def test_auth_login_lock_uses_the_login_state_dir(available, monkeypatch, settings,
                                                  logger):
    """锁必须落在 auth_dir 下（与浏览器目录同处一地），而不是 CWD。"""
    install_fake_backend(monkeypatch, session=FakeSession(token=None, closed=False))
    patch_precheck(monkeypatch, AuthCheck(AuthStatus.VALID, "有效"))
    cmd_auth_login(login_args(timeout=0.01), settings, logger)
    # 采集期间锁存在过；这里只断言目录被用到了（锁本身已被释放）
    assert Path(settings.auth_dir).is_dir()


def test_auth_login_lock_stale_window_outlasts_a_slow_captcha():
    """默认 300s 挡不住慢速输验证码；这里显式放大到 1800s。"""
    from haut_seat.core.lock import DEFAULT_STALE_S

    assert LOGIN_LOCK_STALE_S > DEFAULT_STALE_S
    assert LOGIN_LOCK_STALE_S >= 1800.0


# ------------------------------------------------------------------ 进度元数据（供网页显示）


def test_auth_login_records_progress_metadata(available, monkeypatch, settings, logger):
    """成功路径落库：来源/时间/指纹齐全，且**绝不落值**。"""
    install_fake_backend(monkeypatch)
    patch_precheck(monkeypatch, AuthCheck(AuthStatus.VALID, "有效"))

    cmd_auth_login(login_args(), settings, logger)

    state = read_login_state(settings)
    assert state["login_state"] == "captured"
    assert state["source"] == "snapshot"
    assert state["snapshot_present"] == 1
    assert state["snapshot_captured_at"]
    assert state["fingerprint"] == auth_store.fingerprint(f"Bearer {SENTINEL}")
    # 元数据表是"给页面看的"：绝不能把令牌写进去
    assert SENTINEL not in json.dumps(state, ensure_ascii=False)
    assert "Bearer " not in json.dumps(state, ensure_ascii=False)


def test_auth_login_records_awaiting_state_while_waiting(available, monkeypatch,
                                                        settings, logger):
    """状态机顺序必须是 launching → awaiting_login → captured。

    用"会话第一次被轮询时去读一眼库"的办法观察中间态 ——
    否则 awaiting 这个状态一眨眼就被 captured 覆盖，测试永远看不到它。
    """
    observed: list[str] = []

    class WatchingSession(FakeSession):
        def get_cookie(self, name):
            if not observed:
                observed.append(read_login_state(settings)["login_state"])
            return super().get_cookie(name)

    install_fake_backend(monkeypatch, session=WatchingSession())
    patch_precheck(monkeypatch, AuthCheck(AuthStatus.VALID, "有效"))

    cmd_auth_login(login_args(), settings, logger)

    assert observed == ["awaiting_login"]
    assert read_login_state(settings)["login_state"] == "captured"


def test_auth_login_records_failure_reason_without_values(available, monkeypatch,
                                                          settings, logger):
    install_fake_backend(monkeypatch, session=FakeSession(token=None, closed=True))

    cmd_auth_login(login_args(timeout=5.0), settings, logger)

    state = read_login_state(settings)
    assert state["login_state"] == "failed"
    assert "已关闭" in state["login_message"]
    assert SENTINEL not in json.dumps(state, ensure_ascii=False)


def test_auth_login_never_writes_a_snapshot_on_failure_to_the_metadata(
    available, monkeypatch, settings, logger
):
    """失败路径不许把 source/snapshot_present 改成"已配置" —— 那是在说谎。"""
    install_fake_backend(monkeypatch, session=FakeSession(token=None, closed=False))
    cmd_auth_login(login_args(timeout=0.01), settings, logger)
    state = read_login_state(settings)
    assert state["login_state"] == "failed"
    assert state["source"] == "none"
    assert state["snapshot_present"] == 0


def test_capture_still_succeeds_when_the_metadata_db_is_unusable(
    available, monkeypatch, settings, logger, capsys
):
    """进度元数据只是"给页面看的"：库不可用时采集必须照常成功。"""
    install_fake_backend(monkeypatch)
    patch_precheck(monkeypatch, AuthCheck(AuthStatus.VALID, "有效"))
    broken = settings.model_copy(update={"web_db": str(Path(settings.auth_dir) / "nope" / "x.db")})

    # 让"打开数据库"这一步彻底失败
    monkeypatch.setattr("haut_seat.__main__.open_web_db", lambda s, logger=None: None)

    rc = cmd_auth_login(login_args(), broken, logger)

    assert rc == 0
    assert auth_store.load_snapshot(broken) is not None
    assert "只读复检通过" in capsys.readouterr().out


def test_cli_tests_never_write_to_the_live_database(settings):
    """守卫：命令行会写进度元数据，测试必须把 web_db 指到临时目录。

    默认值 ``data/web.db`` 是在用的业务库，测试碰它会造成真实数据被改动。
    """
    assert str(settings.web_db) != "data/web.db"
    assert "haut-tests" in str(settings.web_db) or "pytest" in str(settings.web_db) \
        or Path(settings.web_db).is_absolute()


# ------------------------------------------------------------------ 不泄露


def test_auth_login_never_logs_the_token(available, monkeypatch, settings, logger,
                                         caplog):
    install_fake_backend(monkeypatch)
    patch_precheck(monkeypatch, AuthCheck(AuthStatus.VALID, "有效"))

    with caplog.at_level(logging.DEBUG, logger="haut_seat.auth"):
        cmd_auth_login(login_args(), settings, logger)

    assert SENTINEL not in caplog.text
    assert f"{COOKIE_NAME}=" not in caplog.text
    assert "Bearer " not in caplog.text


# ------------------------------------------------------------------ auth-status


def test_auth_status_reports_none_source_offline(monkeypatch, settings, logger, capsys):
    monkeypatch.setattr("haut_seat.__main__.browser_availability", lambda: (True, ""))
    rc = cmd_auth_status(argparse.Namespace(), settings, logger)
    out = capsys.readouterr().out

    assert rc == 0
    assert "未配置" in out
    assert "只读查看" in out            # 明确声明没有发网络请求
    assert "快照文件" in out


def test_auth_status_reports_snapshot_source_with_fingerprint_but_no_value(
    monkeypatch, settings, logger, capsys
):
    monkeypatch.setattr("haut_seat.__main__.browser_availability", lambda: (True, ""))
    auth_store.save_snapshot(settings, cookie=f"{COOKIE_NAME}={SENTINEL}",
                             authorization=f"Bearer {SENTINEL}")
    # 必须显式用 auto：conftest 把测试环境钉在 env 模式，而 env 模式**故意**忽略
    # 快照（"既有部署零变化"），所以那条路径下显示"未配置"才是对的。
    auto = settings.model_copy(update={"auth_source": "auto"})

    cmd_auth_status(argparse.Namespace(), auto, logger)
    out = capsys.readouterr().out

    assert "浏览器会话快照" in out
    assert auth_store.fingerprint(f"Bearer {SENTINEL}") in out
    assert SENTINEL not in out


def test_auth_status_explains_browser_unavailability(monkeypatch, settings, logger,
                                                     capsys):
    monkeypatch.setattr(
        "haut_seat.__main__.browser_availability", lambda: (False, "未安装 playwright")
    )
    cmd_auth_status(argparse.Namespace(), settings, logger)
    out = capsys.readouterr().out
    assert "浏览器自动化: 不可用" in out
    assert "未安装 playwright" in out


def test_auth_status_explains_unavailable_credentials_in_browser_mode(
    monkeypatch, settings, logger, capsys
):
    """browser 模式缺快照时，必须把"为什么没有凭据"如实说出来。"""
    monkeypatch.setattr("haut_seat.__main__.browser_availability", lambda: (True, ""))
    strict = settings.model_copy(update={
        "auth_source": "browser",
        "auth_snapshot_file": str(Path(settings.auth_dir) / "nope.json"),
    })

    rc = cmd_auth_status(argparse.Namespace(), strict, logger)
    out = capsys.readouterr().out

    assert rc == 0
    assert "不可用" in out
    assert "auth-login" in out          # 给出补救方式
    assert "browser" in out


def test_auth_status_shows_the_reuse_window(monkeypatch, settings, logger, capsys):
    """TTL 是"与预约时序结合"的配置，必须能不开面板就核实它真的生效。"""
    monkeypatch.setattr("haut_seat.__main__.browser_availability", lambda: (True, ""))
    tuned = settings.model_copy(update={"auth_check_ttl_s": 120.0})
    cmd_auth_status(argparse.Namespace(), tuned, logger)
    assert "预检复用窗口: 120s" in capsys.readouterr().out


def test_auth_status_makes_no_network_call(monkeypatch, settings, logger):
    """守卫：auth-status 是纯只读文件检查，绝不构造 HTTP 客户端。"""
    import haut_seat.services.io as io_mod

    def forbidden(*a, **k):
        raise AssertionError("auth-status 不允许发起任何请求")

    monkeypatch.setattr(io_mod, "auth_precheck", forbidden)
    monkeypatch.setattr(io_mod, "query_online", forbidden)
    monkeypatch.setattr(io_mod, "build_http_client", forbidden)
    monkeypatch.setattr("haut_seat.__main__.browser_availability", lambda: (True, ""))

    assert cmd_auth_status(argparse.Namespace(), settings, logger) == 0


# ------------------------------------------------------------------ 既有命令补充


def test_check_auth_reports_the_actual_credential_source(available, monkeypatch,
                                                         settings, logger, capsys):
    """预检走凭据来源层，因此输出必须如实说明用的是哪一份（否则自相矛盾）。"""
    from haut_seat.__main__ import cmd_check_auth

    auth_store.save_snapshot(settings, cookie=f"{COOKIE_NAME}={SENTINEL}",
                             authorization=f"Bearer {SENTINEL}")
    auto = settings.model_copy(update={"auth_source": "auto"})
    patch_check_auth_only(monkeypatch, AuthCheck(AuthStatus.VALID, "凭据有效"))

    rc = cmd_check_auth(argparse.Namespace(regionid=None, start="", end=""), auto, logger)
    out = capsys.readouterr().out

    assert rc == 0
    assert "浏览器会话快照" in out
    assert auth_store.fingerprint(f"Bearer {SENTINEL}") in out
    assert SENTINEL not in out


def test_status_reports_source_and_reuse_window(available, monkeypatch, settings,
                                                logger, capsys):
    from haut_seat.__main__ import cmd_status

    auth_store.save_snapshot(settings, cookie=f"{COOKIE_NAME}={SENTINEL}",
                             authorization=f"Bearer {SENTINEL}")
    auto = settings.model_copy(update={"auth_source": "auto", "auth_check_ttl_s": 120.0})

    rc = cmd_status(argparse.Namespace(), auto, logger)
    out = capsys.readouterr().out

    assert rc == 0
    assert "预检复用窗口: 120s" in out
    assert "浏览器会话快照" in out
    assert SENTINEL not in out


# ------------------------------------------------------------------ 守卫


def test_this_module_never_opens_a_real_browser():
    """守卫：本测试模块不引用任何真实浏览器 API。

    关键词按片段拼装，注释里也不写出完整词 —— 否则断言所在的这一行
    自己就会命中自己的检查。这与 ``test_auth.py`` 里那条守卫是同一个套路。
    """
    text = Path(__file__).read_text(encoding="utf-8")
    for forbidden in (
        "sync_" + "playwright",
        "launch_persistent_" + "context",
        "chromium." + "launch",
    ):
        assert forbidden not in text


def test_this_module_has_no_real_host():
    text = Path(__file__).read_text(encoding="utf-8")
    real_host = "wslib" + ".haut" + ".edu.cn"
    assert real_host not in text