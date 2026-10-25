"""认证元数据表、凭据来源展示与网页登录入口的测试。

全部离线：

- 需要发请求的地方一律用 ``httpx.MockTransport``（见模块末尾的守卫）；
- **不开浏览器**：登录按钮的测试注入假 launcher 或假 spawner，
  并且把 ``_popen_detached`` 换成"一旦被调用即失败"，证明路由绝不会真的起子进程；
- 不调用任何预约提交接口（``submit_fn`` 一被碰就抛错）。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from haut_seat.services import auth_store
from haut_seat.web import repository as repo
from haut_seat.web.validation import PlanInput

# 哨兵串：形状像真令牌，但显然是测试专用值。页面 / 跳转 / 日志里都不该出现它。
SENTINEL = "SENTINELTOKEN-._0002-abcdefghijklmnop"
GOOD_PASSWORD = "Correct-Horse-9"

_CSRF_RE = re.compile(r'name="_csrf"\s+value="([^"]+)"')


def csrf_of(html: str) -> str:
    match = _CSRF_RE.search(html)
    assert match, "页面没有渲染 CSRF 隐藏字段"
    return match.group(1)


def make_admin(app, username="tester", password=GOOD_PASSWORD):
    from haut_seat.web import security

    return security.create_admin(app.state.db, username, password)


def login(client, username="tester", password=GOOD_PASSWORD):
    return client.post("/admin/login", data={"username": username, "password": password})


def auth_settings(web_settings, tmp_path, **overrides):
    """把快照/配置目录指向临时目录的配置工厂（默认 browser 模式）。"""

    def _make(**extra):
        base = {
            "auth_source": "browser",
            "auth_dir": str(tmp_path / "auth"),
            "auth_snapshot_file": str(tmp_path / "auth" / "credentials.json"),
            "auth_profile_dir": str(tmp_path / "auth" / "browser-profile"),
        }
        base.update(overrides)
        base.update(extra)
        return web_settings.model_copy(update=base)

    return _make


def write_snapshot(settings, *, token: str = SENTINEL) -> None:
    auth_store.save_snapshot(
        settings,
        cookie=f"Admin-Token={token}",
        authorization=f"Bearer {token}",
    )


class FakeLauncher:
    """假 launcher：只记录调用，**绝不碰 subprocess**。"""

    def __init__(self, *, ok=True, message="已启动登录窗口"):
        self.calls = 0
        self._ok = ok
        self._message = message

    def spawn(self, settings=None, *, timeout_s=None):
        self.calls += 1
        return {"ok": self._ok, "message": self._message}


class NoHttpClient:
    """任何构造 HTTP 客户端的行为都立刻大声失败，并记下次数。"""

    def __init__(self) -> None:
        self.attempts = 0

    def install(self, monkeypatch) -> None:
        recorder = self

        def forbidden(*args, **kwargs):
            recorder.attempts += 1
            raise AssertionError("这一步绝不允许发起任何 HTTP 请求")

        monkeypatch.setattr(httpx, "Client", forbidden)


def admin_env(
    make_client,
    settings,
    db=None,
    auth_service=None,
    *,
    catalog_service=None,
    **overrides,
):
    """登录好的管理员客户端（离线依赖），返回 (app, client, db, settings)。"""
    from haut_seat.web.db import Database

    database = db or Database(settings.web_db)
    database.init_schema()
    kwargs = dict(overrides)
    if catalog_service is not None:
        kwargs["catalog_service"] = catalog_service
    app, client = make_client(settings=settings, db=database, **kwargs)
    make_admin(app)
    assert login(client).status_code == 303
    return app, client, database, settings


# ================================================================== 老库兼容
#
# 本仓库**没有迁移机制**（web/db.py 的注释已明确）：给既有表加列会让老库
# 的 SELECT 抛 "no such column"。所以本轮只加新表，这里用一个"老结构"库实测。


def test_legacy_database_gets_the_new_table_without_a_migration(tmp_path):
    """用一个"上一版程序建出来的库"实测就地补建。

    不手搓一个残缺的 plan 表来冒充老库 —— 那会撞上本项目**本来就不支持**的
    "给既有表加列"（`CREATE INDEX ... ON plan(status)` 直接报 no such column）。
    真实的老库是"当前 schema 少一张新表"，所以这里就照这个口径构造。
    """
    from haut_seat.web import db as db_module
    from haut_seat.web.db import Database

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    for stmt in db_module._SCHEMA_STATEMENTS:
        if "auth_credential_state" in stmt:
            continue  # 老版本没有这张表
        conn.execute(stmt)
    conn.execute(
        "INSERT INTO plan (name, enabled, status, run_date, repeat_rule, trigger_at,"
        " book_start, book_end, regionid, created_at, updated_at)"
        " VALUES ('老计划', 1, 'pending', '2026-10-18', 'once', '09:00:00',"
        " '2026-10-18 10:30:00', '2026-10-18 10:31:00', 16, '2026-10-18 08:00:00',"
        " '2026-10-18 08:00:00')"
    )
    conn.commit()
    conn.close()

    db = Database(path)
    try:
        db.init_schema()

        names = {
            row["name"]
            for row in db.query_all("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "auth_credential_state" in names, "老库启动时应就地补建新表"

        # 既有数据一个字节都没动
        row = db.query_one("SELECT name, status FROM plan WHERE regionid = 16")
        assert row is not None and row["name"] == "老计划"
        assert row["status"] == "pending"

        # 新表真的能读：不报 no such table / no such column，且带上默认行
        rows = db.query_all("SELECT id, source, login_state FROM auth_credential_state")
        assert len(rows) == 1
        assert rows[0]["id"] == 1
        assert rows[0]["source"] == "none"
        assert rows[0]["login_state"] == "idle"

        # 补建之后还必须能写（页面上的各种动作都要写这张表）
        repo.set_credential_state(db, login_state="captured", login_message="已获取凭据")
        assert repo.get_credential_state(db)["login_state"] == "captured"

        # 再启动一次也不能出错、不能多出一行
        db.init_schema()
        assert len(db.query_all("SELECT id FROM auth_credential_state")) == 1
    finally:
        db.close_current_thread()


def test_init_schema_is_idempotent_and_never_duplicates_the_row(tmp_path):
    from haut_seat.web.db import Database

    db = Database(tmp_path / "web.db")
    try:
        db.init_schema()
        db.init_schema()
        rows = db.query_all("SELECT id FROM auth_credential_state")
        assert [r["id"] for r in rows] == [1]
    finally:
        db.close_current_thread()


def test_credential_state_roundtrip_survives_a_reopen(web_settings):
    """写入的元数据必须落盘：进程重启后页面读到的还是同一条。"""
    from haut_seat.web.db import Database

    db = Database(web_settings.web_db)
    db.init_schema()
    repo.set_credential_state(
        db,
        source="snapshot",
        snapshot_present=True,
        snapshot_captured_at="2026-10-18 17:50:12",
        fingerprint="a1b2c3d4e5f6",
    )
    db.close_current_thread()

    reopened = Database(web_settings.web_db)
    try:
        state = repo.get_credential_state(reopened)
        assert state["source"] == "snapshot"
        assert state["snapshot_present"] == 1
        assert state["snapshot_captured_at"] == "2026-10-18 17:50:12"
        assert state["fingerprint"] == "a1b2c3d4e5f6"
    finally:
        reopened.close_current_thread()


def test_credential_state_rejects_unknown_fields(web_db):
    with pytest.raises(ValueError):
        repo.set_credential_state(web_db, cookie="Admin-Token=x")  # type: ignore[arg-type]


def test_login_message_is_redacted_before_it_is_stored(web_db):
    """纵深防御：即便有人误把令牌塞进这条消息，入库前也会被抹掉。"""
    repo.set_credential_state(
        web_db, login_state="failed", login_message=f"登录失败：Authorization: Bearer {SENTINEL}"
    )
    stored = repo.get_credential_state(web_db)["login_message"]
    assert SENTINEL not in stored
    assert "<redacted>" in stored


# ================================================================== 展示
#
# 说明：`credential_source` 走 resolve_credentials（页面显示的是"这一页刷新时
# 真正会被用到的来源"，而不是"文件存在与否"），快照时间与指纹也同源。


def test_auth_page_shows_the_credential_source_card(make_client, web_settings, tmp_path):
    settings = auth_settings(web_settings, tmp_path)(auth_source="browser")
    write_snapshot(settings)
    _app, client, _db, _settings = admin_env(make_client, settings)

    page = client.get("/admin/auth")
    assert page.status_code == 200
    assert "凭据来源" in page.text
    assert "浏览器会话快照" in page.text
    assert "快照采集" in page.text or "已采集" in page.text
    # 指纹是单向摘要前缀 —— 只出现指纹，绝不出现值本身
    expected_fp = auth_store.fingerprint(f"Bearer {SENTINEL}")
    assert expected_fp in page.text
    assert SENTINEL not in page.text
    # 注意：页面上确实会出现字面量 "Admin-Token=" —— 那是**手工恢复步骤**里的
    # 说明文字（"复制 Cookie 中的 Admin-Token= 那一段"）与 .env 占位符示例，
    # 不是任何真实值。所以这里只能断言哨兵串本身不在，不能断言键名不在。
    assert "Admin-Token=&lt;新值&gt;" in page.text, "占位符本身必须是占位符"


def test_auth_page_shows_the_snapshot_after_a_restart(make_client, web_settings, tmp_path):
    """重启后仍能恢复：快照在磁盘上，页面按它显示来源（不依赖任何内存状态）。"""
    from haut_seat.web.db import Database

    settings = auth_settings(web_settings, tmp_path)(auth_source="browser")
    write_snapshot(settings)

    first_app, first_client, first_db, _ = admin_env(make_client, settings)
    assert first_client.get("/admin/auth").status_code == 200
    first_db.close_current_thread()

    # 完全重新构造一遍（新 Database、新 app，同一个配置与同一个库文件），
    # 模拟进程重启：不共享任何上一进程的对象。
    second_db = Database(settings.web_db)
    second_db.init_schema()
    app, client = make_client(settings=settings, db=second_db)
    assert login(client).status_code == 303, "管理员与快照都应还在"

    page = client.get("/admin/auth")
    assert "浏览器会话快照" in page.text
    assert SENTINEL not in page.text


def test_auth_page_reports_an_unavailable_browser_with_a_fallback(
    make_client, web_settings, tmp_path, monkeypatch
):
    """浏览器自动化不可用时必须说清原因与替代做法，绝不静默。"""
    from haut_seat.services import auth_browser

    monkeypatch.setattr(
        auth_browser,
        "browser_availability",
        lambda: (False, "未安装 playwright（测试强制的不可用）"),
    )
    settings = auth_settings(web_settings, tmp_path)(auth_source="env")
    _app, client, _db, _settings = admin_env(make_client, settings)

    page = client.get("/admin/auth")
    assert page.status_code == 200
    assert "不可用" in page.text
    assert "未安装 playwright（测试强制的不可用）" in page.text
    assert "pip install -e" in page.text  # 给出补救命令
    assert "auth-login" in page.text      # 给出命令行回退
    # 不可用时不得渲染那个按钮对应的表单
    assert 'action="/admin/auth/login-browser"' not in page.text


def test_auth_page_offers_the_button_when_the_browser_is_available(
    make_client, web_settings, tmp_path, monkeypatch
):
    from haut_seat.services import auth_browser

    monkeypatch.setattr(auth_browser, "browser_availability", lambda: (True, ""))
    settings = auth_settings(web_settings, tmp_path)(auth_source="env")
    _app, client, _db, _settings = admin_env(make_client, settings)

    page = client.get("/admin/auth")
    assert 'action="/admin/auth/login-browser"' in page.text
    assert "人工" in page.text
    assert "不识别" in page.text or "不代填" in page.text


def test_system_page_carries_a_compact_card_not_a_second_copy(
    make_client, web_settings, tmp_path
):
    settings = auth_settings(web_settings, tmp_path)(auth_source="browser")
    write_snapshot(settings)
    _app, client, _db, _settings = admin_env(make_client, settings)

    page = client.get("/admin/system")
    assert page.status_code == 200
    assert "前往认证状态页" in page.text
    assert "浏览器会话快照" in page.text
    # 紧凑卡片：整页复制过来的那些内容不该出现在系统状态页
    assert "一键人工登录（消除手抄 F12 的步骤）" not in page.text
    assert "为什么不能自动续期" not in page.text
    assert SENTINEL not in page.text


def test_system_page_has_the_login_button_only_when_available(
    make_client, web_settings, tmp_path, monkeypatch
):
    from haut_seat.services import auth_browser

    settings = auth_settings(web_settings, tmp_path)(auth_source="env")

    monkeypatch.setattr(auth_browser, "browser_availability", lambda: (False, "不可用（测试）"))
    _app, client, _db, _settings = admin_env(make_client, settings)
    assert 'action="/admin/auth/login-browser"' not in client.get("/admin/system").text

    monkeypatch.setattr(auth_browser, "browser_availability", lambda: (True, ""))
    assert 'action="/admin/auth/login-browser"' in client.get("/admin/system").text


def test_wait_flag_only_refreshes_while_a_login_is_in_flight(
    make_client, web_settings, tmp_path
):
    """`?wait=1` 的自动刷新必须只在真的等待登录时出现，日常浏览不被强制重载。"""
    from haut_seat.services import auth_login_service as als

    settings = auth_settings(web_settings, tmp_path)(auth_source="env")
    app, client, db, _settings = admin_env(make_client, settings)

    assert 'http-equiv="refresh"' not in client.get("/admin/auth").text
    assert 'http-equiv="refresh"' not in client.get("/admin/auth?wait=1").text

    als.record_login_state(db, login_state=als.LOGIN_AWAITING, message="等待登录")
    assert 'http-equiv="refresh"' in client.get("/admin/auth?wait=1").text
    assert 'http-equiv="refresh"' not in client.get("/admin/auth").text, "不带 wait 时不得刷新"


def test_wait_flag_stops_refreshing_on_a_leftover_state(
    make_client, web_settings, tmp_path
):
    """**被强杀留下的** ``awaiting_login`` 绝不能让页面无限自刷。

    进程被 kill 之后，元数据表里那条 ``awaiting_login`` 再也不会被改写。如果
    ``?wait=1`` 只看它，页面就会每 5 秒重载一次、永远不停，用户永远看不到结论
    —— 这正是「绝不无限刷新/无限加载」要挡掉的那类症状。
    宽限期（登录等待上限 + 余量）一过就必须停止自刷。
    """
    from datetime import timedelta

    from haut_seat.services import auth_login_service as als

    settings = auth_settings(web_settings, tmp_path)(auth_source="env")
    app, client, db, _settings = admin_env(make_client, settings)

    stale_at = datetime.now() - timedelta(
        seconds=float(settings.auth_login_timeout_s) + 600.0
    )
    repo.set_credential_state(
        db, login_state=als.LOGIN_AWAITING, login_message="上一次被强杀留下的",
        now=stale_at,
    )

    assert 'http-equiv="refresh"' not in client.get("/admin/auth?wait=1").text, (
        "残留状态必须按「没有会话在跑」处理，否则页面会无限自刷"
    )

    # 真的重新开始一次登录：又该刷新了（宽限只针对残留，不是把功能关掉）
    als.record_login_state(db, login_state=als.LOGIN_AWAITING, message="真的在等待")
    assert 'http-equiv="refresh"' in client.get("/admin/auth?wait=1").text


# ================================================================== 登录按钮
#
# 授权链与其它 POST 逐字一致：require_admin + check_csrf + 303 PRG。


def test_login_button_requires_a_session(make_client, web_settings, tmp_path):
    settings = auth_settings(web_settings, tmp_path)(auth_source="env")
    launcher = FakeLauncher()
    _app, client = make_client(settings=settings, auth_login_launcher=launcher)

    resp = client.post("/admin/auth/login-browser", data={"_csrf": "whatever"})
    assert resp.status_code == 401
    assert launcher.calls == 0


def test_login_button_requires_csrf(admin_client):
    app, client = admin_client
    launcher = FakeLauncher()
    app.state.auth_login_launcher = launcher

    resp = client.post("/admin/auth/login-browser", data={"_csrf": "wrong"})
    assert resp.status_code == 400
    assert launcher.calls == 0


def test_login_button_uses_the_real_launcher_and_never_popens(
    make_client, web_settings, tmp_path, monkeypatch
):
    """真 launcher + 假 spawner：既证明路由不走 subprocess，又证明单飞真的生效。"""
    from haut_seat.services import auth_login_service as als
    from haut_seat.web.db import Database

    settings = auth_settings(web_settings, tmp_path)(auth_source="env")
    db = Database(settings.web_db)
    db.init_schema()

    spawned: list[tuple[list[str], str, Path]] = []

    def fake_spawner(argv, cwd, log_path):
        spawned.append((argv, cwd, log_path))
        return 4242

    def forbidden(*args, **kwargs):
        raise AssertionError("登录按钮的路由绝不允许真的 Popen")

    monkeypatch.setattr(als, "_popen_detached", forbidden)

    launcher = als.LoginLauncher(settings, db, spawner=fake_spawner)
    app, client = make_client(settings=settings, db=db, auth_login_launcher=launcher)
    make_admin(app)
    assert login(client).status_code == 303

    token = csrf_of(client.get("/admin/auth").text)
    resp = client.post("/admin/auth/login-browser", data={"_csrf": token})

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/admin/auth?wait=1")
    assert "ok=1" in location
    assert SENTINEL not in location

    assert len(spawned) == 1, "应恰好起一个子进程"
    argv, cwd, log_path = spawned[0]
    assert "-m" in argv and "auth-login" in argv
    assert cwd == str(launcher.project_root), "子进程必须项目根为 CWD（.env / data 都按 CWD 解析）"
    assert str(log_path).endswith("auth-login.log")

    state = repo.get_credential_state(db)
    assert state["login_state"] == als.LOGIN_LAUNCHING

    # 单飞：还没结束之前再点一次，不得再起一个窗口抢同一个 profile
    again = client.post("/admin/auth/login-browser", data={"_csrf": token})
    assert again.status_code == 303
    assert "ok=0" in again.headers["location"]
    assert len(spawned) == 1


def test_login_button_reports_a_launcher_failure_without_leaking(
    make_client, web_settings, tmp_path
):
    """启动器抛异常时只报类型名，绝不把异常消息带进跳转 URL。"""
    settings = auth_settings(web_settings, tmp_path)(auth_source="env")

    class ExplodingLauncher:
        def spawn(self, settings=None, *, timeout_s=None):
            raise RuntimeError(f"内部细节 {SENTINEL}")

    app, client = make_client(settings=settings, auth_login_launcher=ExplodingLauncher())
    make_admin(app)
    assert login(client).status_code == 303

    token = csrf_of(client.get("/admin/auth").text)
    resp = client.post("/admin/auth/login-browser", data={"_csrf": token})
    assert resp.status_code == 303
    assert "ok=0" in resp.headers["location"]
    assert "RuntimeError" in resp.headers["location"]
    assert SENTINEL not in resp.headers["location"]


def test_login_button_says_so_when_there_is_no_launcher(
    make_client, web_settings, tmp_path
):
    settings = auth_settings(web_settings, tmp_path)(auth_source="env")
    app, client = make_client(settings=settings)
    app.state.auth_login_launcher = None
    make_admin(app)
    assert login(client).status_code == 303

    token = csrf_of(client.get("/admin/auth").text)
    resp = client.post("/admin/auth/login-browser", data={"_csrf": token})
    assert resp.status_code == 303
    assert "ok=0" in resp.headers["location"]
    assert "auth-login" in resp.headers["location"] or "命令行" in resp.headers["location"]


def test_reload_button_re_reads_the_source_without_any_network_call(
    make_client, web_settings, tmp_path, monkeypatch
):
    """「重新读取凭据来源」连网络请求都不发 —— 这是它存在的全部理由。"""
    settings = auth_settings(web_settings, tmp_path)(auth_source="browser")
    write_snapshot(settings)
    recorder = NoHttpClient()
    recorder.install(monkeypatch)

    _app, client, db, _settings = admin_env(make_client, settings)
    token = csrf_of(client.get("/admin/auth").text)
    resp = client.post("/admin/auth/reload", data={"_csrf": token})

    assert resp.status_code == 303
    assert "ok=1" in resp.headers["location"]
    assert recorder.attempts == 0
    state = repo.get_credential_state(db)
    assert state["source"] == "snapshot"
    assert state["fingerprint"] == auth_store.fingerprint(f"Bearer {SENTINEL}")


def test_reload_button_reports_an_unavailable_source_honestly(
    make_client, web_settings, tmp_path
):
    settings = auth_settings(web_settings, tmp_path)(auth_source="browser")  # 故意没有快照
    _app, client, _db, _settings = admin_env(make_client, settings)

    token = csrf_of(client.get("/admin/auth").text)
    resp = client.post("/admin/auth/reload", data={"_csrf": token})
    assert resp.status_code == 303
    assert "ok=0" in resp.headers["location"]


# ================================================================== 不泄露
#
# 哨兵串（含 JWT 形状）走完"写快照 → 解析 → 预检 → 渲染页面 → 跳转"全流程后，
# 任何面向人的输出里都不该出现它。


def capture_haut_logs():
    """把 ``haut_seat`` logger 的输出抓下来。

    ``logging_conf`` 给该 logger 设了 ``propagate=False``，所以 caplog 抓不到，
    必须自己挂一个 handler。
    """
    import contextlib
    import io as _io

    @contextlib.contextmanager
    def _capture():
        stream = _io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("haut_seat")
        logger.addHandler(handler)
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            yield stream
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)

    return _capture()


def test_sentinel_never_reaches_the_pages_or_the_logs(
    make_client, web_settings, tmp_path, monkeypatch
):
    settings = auth_settings(web_settings, tmp_path)(auth_source="browser")
    recorder = NoHttpClient()
    recorder.install(monkeypatch)  # 「重新读取来源」这一路不发请求

    with capture_haut_logs() as stream:
        write_snapshot(settings)  # 写快照的过程本身也不该记值
        _app, client, _db, _settings = admin_env(make_client, settings)

        auth_page = None
        for path in ("/admin/auth", "/admin/system", "/admin/advanced", "/admin"):
            page = client.get(path)
            assert page.status_code == 200, path
            assert SENTINEL not in page.text, path
            if path == "/admin/auth":
                auth_page = page
        # 正对照：卡片真的渲染了（否则"页面里没有哨兵"可能只是因为页面是空的）。
        # 说明：页面上的 "Admin-Token=" 只可能是**手工恢复步骤**里的键名与
        # `<新值>` 占位符，不是值，所以断言的是哨兵串本身不在页面里。
        assert auth_page is not None
        assert auth_store.fingerprint(f"Bearer {SENTINEL}") in auth_page.text

        token = csrf_of(auth_page.text)
        resp = client.post("/admin/auth/reload", data={"_csrf": token})
        assert SENTINEL not in resp.headers["location"]

    logged = stream.getvalue()
    assert SENTINEL not in logged
    assert f"Admin-Token={SENTINEL}" not in logged


def test_credentials_in_flight_never_land_in_the_logs(
    make_settings, tmp_path, monkeypatch
):
    """凭据真的发出去了，但一行日志里都没有 —— 这是"从不记录请求头"的直接证据。

    只看"日志里没有哨兵"是不够的：如果请求根本没带凭据，那个断言什么都证明不了。
    所以这里同时断言传输层**确实**收到了带哨兵的 Authorization 头。
    """
    from haut_seat.core.auth import AuthStatus
    from haut_seat.services import io

    settings = make_settings(
        auth_source="browser",
        auth_snapshot_file=str(tmp_path / "auth" / "credentials.json"),
        auth_profile_dir=str(tmp_path / "auth" / "browser-profile"),
    )
    write_snapshot(settings)

    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({k.lower(): v for k, v in request.headers.items()})
        raise httpx.ConnectError("连接被拒绝（测试注入的传输层故障）")

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", fake_client)

    with capture_haut_logs() as stream:
        check = io.auth_precheck(settings, 16, None, None)

    assert seen, "这个测试的前提是请求真的发出去过"
    assert any(SENTINEL in str(h.get("authorization", "")) for h in seen), (
        "凭据必须真的在请求头里 —— 否则「日志里没有」什么都证明不了"
    )

    assert check.status is AuthStatus.INDETERMINATE, "网络异常必须判为不确定，绝不误判失效"
    assert SENTINEL not in check.message

    logged = stream.getvalue()
    assert SENTINEL not in logged
    # 请求头（含凭据）从不进日志：Cookie 与 Authorization 都只有方法、路径与状态码
    assert "Bearer " not in logged
    assert "Admin-Token=" not in logged
    assert "authorization" not in logged.lower()

    assert SENTINEL not in stream.getvalue()


def test_credentials_repr_never_contains_the_value(tmp_path, make_settings):
    """即使有人 ``logger.debug("%s", creds)``，也只会看到来源与指纹。"""
    settings = make_settings(
        auth_source="browser",
        auth_snapshot_file=str(tmp_path / "auth" / "credentials.json"),
        auth_profile_dir=str(tmp_path / "auth" / "browser-profile"),
    )
    write_snapshot(settings)
    creds = auth_store.resolve_credentials(settings)
    assert SENTINEL not in repr(creds)
    assert "<redacted>" in repr(creds)

    snapshot = auth_store.load_snapshot(settings)
    assert snapshot is not None
    assert SENTINEL not in repr(snapshot)


# ================================================================== 阻断
#
# 这是本设计里唯一偏离"不确定绝不阻断"惯例的地方：browser 模式下没有快照
# 不是"不确定"，而是"确定无法提交"。因此必须在**提交前**拦住，
# 复用既有的 auth_blocked 分支，不新增闸门。


def _browser_env_without_snapshot(make_client, web_settings, tmp_path, monkeypatch):
    """浏览器模式 + 无快照 + **真实**预检 + 真实调度器的完整环境。"""
    from haut_seat.services.auth_service import AuthService
    from haut_seat.services.scheduler import Scheduler
    from haut_seat.web.db import Database

    fixed = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    settings = web_settings.model_copy(
        update={
            "auth_source": "browser",
            "auth_dir": str(tmp_path / "auth"),
            "auth_snapshot_file": str(tmp_path / "auth" / "credentials.json"),
            "auth_profile_dir": str(tmp_path / "auth" / "browser-profile"),
            "enable_real_booking": True,  # 让"真开关放行"不再是干扰项
        }
    )
    db = Database(settings.web_db)
    db.init_schema()

    queries = {"n": 0}
    submits = {"n": 0}

    def query_fn(*args, **kwargs):
        queries["n"] += 1
        from haut_seat.models.seat import LayoutQueryResult

        return LayoutQueryResult.from_response({"layout": None, "seatList": []})

    def submit_fn(*args, **kwargs):
        submits["n"] += 1
        raise RuntimeError("测试禁止调用真实预约提交接口")

    def forbidden_clock():
        return fixed

    auth = AuthService(db, settings)  # 真实 check_fn：走 io.auth_precheck

    scheduler = Scheduler(
        db,
        settings,
        tick_s=1.0,
        max_concurrent=1,
        query_fn=query_fn,
        submit_fn=submit_fn,
        clock=forbidden_clock,
        auth_service=auth,
    )

    day = fixed.strftime("%Y-%m-%d")
    plan_id = repo.create_plan(
        db,
        PlanInput(
            name="测试计划",
            run_date=day,
            repeat_rule="once",
            trigger_at="09:00:00",
            book_start=f"{day} 10:30:00",
            book_end=f"{day} 10:31:00",
            regionid=16,
        ),
        enabled=True,
    )
    repo.set_app_setting(db, global_pause=False, scheduler_enabled=True)

    app, client = make_client(settings=settings, db=db, scheduler=scheduler, auth_service=auth)
    make_admin(app)
    assert login(client).status_code == 303
    return app, client, db, settings, scheduler, plan_id, queries, submits


def test_invalid_credentials_block_and_no_run(
    make_client, web_settings, tmp_path, monkeypatch
):
    """browser 模式无快照 ⇒ 判定失效 ⇒ 零 HTTP 调用、零运行、零提交。"""
    recorder = NoHttpClient()
    recorder.install(monkeypatch)

    (
        _app, client, db, _settings, scheduler, plan_id, queries, submits
    ) = _browser_env_without_snapshot(make_client, web_settings, tmp_path, monkeypatch)

    # 一次真实的只读预检（面板上的「重新检测登录状态」等价物）
    check = scheduler.auth_service.check_now(source="manual")
    assert check.status.value == "invalid"
    assert "auth-login" in check.message, "必须给出补救命令"
    assert recorder.attempts == 0, "没有凭据时绝不允许发起任何 HTTP 请求"
    assert repo.get_auth_state(db)["status"] == "invalid"
    assert scheduler.auth_service.blocks_submissions() is True

    scheduler._tick()

    assert queries["n"] == 0, "认证失效时不得轮询座位"
    assert submits["n"] == 0, "认证失效时不得提交"
    assert repo.list_runs(db) == [], "认证失效时不得创建运行记录"
    assert repo.get_plan(db, plan_id)["status"].value == "awaiting_auth"


def test_browser_mode_missing_snapshot_blocks_submissions_on_every_page(
    make_client, web_settings, tmp_path, monkeypatch
):
    recorder = NoHttpClient()
    recorder.install(monkeypatch)

    (
        _app, client, db, _settings, scheduler, _plan_id, _queries, _submits
    ) = _browser_env_without_snapshot(make_client, web_settings, tmp_path, monkeypatch)

    assert scheduler.auth_service.blocks_submissions() is True

    page = client.get("/admin/auth")
    assert page.status_code == 200
    assert "auth-login" in page.text or "HAUT_AUTH_SOURCE" in page.text

    # 全局失效横幅出现在管理端任意页面上。
    # 措辞必须与调度器的真实语义一致：全局凭据只管**无归属**的计划，
    # 用户端计划按各自用户绑定的账号状态单独判定 —— 所以这里不能说"所有计划"。
    home = client.get("/admin")
    assert home.status_code == 200
    assert "管理端自建计划" in home.text
    assert "新提交已停止" in home.text


def test_env_mode_without_credentials_still_does_not_block(
    make_client, web_settings, tmp_path, monkeypatch
):
    """旧语义必须原样保留：env/auto 模式没有凭据是"不确定"，绝不阻断提交。"""
    from haut_seat.services.auth_service import AuthService
    from haut_seat.web.db import Database

    settings = web_settings.model_copy(
        update={
            "auth_source": "env",
            "auth_dir": str(tmp_path / "auth"),
            "auth_snapshot_file": str(tmp_path / "auth" / "credentials.json"),
            "auth_profile_dir": str(tmp_path / "auth" / "browser-profile"),
        }
    )
    db = Database(settings.web_db)
    db.init_schema()
    auth = AuthService(db, settings)

    assert auth.credentials_unavailable() is False
    assert auth.blocks_submissions() is False

    check = auth.check_now(source="manual")
    assert check.status.value == "indeterminate"


# ================================================================== 守卫


def test_this_module_never_sends_real_requests():
    """守卫：需要发请求的地方只用 MockTransport，且不出现真实学校域名。"""
    text = Path(__file__).read_text(encoding="utf-8")
    assert "MockTransport" in text
    real_host = "wslib" + ".haut" + ".edu.cn"
    assert real_host not in text


def test_this_module_never_opens_a_browser():
    """守卫：本模块不 import 浏览器驱动，也绝不执行任何"下载浏览器"的命令。

    针串按片段拼装、docstring 里也不写全字面量：否则断言会把自己匹配上
    （这个坑在本轮已经踩过一次）。
    """
    text = Path(__file__).read_text(encoding="utf-8")
    for needle in (
        "playwright" + "." + "sync_api",
        "playwright" + " install",
        "subprocess" + ".Popen",
        "os" + ".system",
    ):
        assert needle not in text, f"本模块不该出现 {needle!r}"