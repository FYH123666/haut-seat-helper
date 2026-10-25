"""端到端登录流程测试：追踪「登录成功后又被送回登录页」这条链路。

为什么单独一个文件：``test_web_app.py`` 关心的是「每个路由返回什么状态码」，
用 ``follow_redirects=False`` 逐个断言。本文件关心的是**浏览器视角**——
Cookie 会不会被保存、下一次请求会不会带上，因此：

- ``base_url`` 的协议决定了 ``Secure`` 规则是否生效（httpx 的 ``Cookies``
  内部就是标准库 ``http.cookiejar``，与浏览器同一套判定）；
- 跳转由 ``follow()`` 手动跟随，这样可以同时拿到「中间那次 303 上的
  ``Set-Cookie``」和「最终落在哪个页面」。

覆盖的链路：
    登录 POST → 口令校验 → 写入 session 行 → Set-Cookie → jar 是否保存
    → 后续请求是否带上 Cookie → load_session 校验 → 受保护页面

全部离线：调度器、查询、提交都是替身，不会发出任何真实预约请求。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from haut_seat.web import security

GOOD_PASSWORD = "Correct-Horse-9"
USERNAME = "tester"

# 页面标记：导航栏在任何页面都存在，不能用它判断登录状态；
# 只有登录后的页面才会渲染带 CSRF 的「退出」表单（base.html 的 {% if admin %}）。
LOGIN_PAGE_MARK = "登录管理面板"
LOGGED_IN_MARK = 'action="/admin/logout"'

REDIRECTS = (301, 302, 303, 307, 308)


# ------------------------------------------------------------------ 测试辅助


def make_admin(app, username=USERNAME, password=GOOD_PASSWORD):
    return security.create_admin(app.state.db, username, password)


def browser(app, *, scheme="http", follow_redirects=False, raise_server_exceptions=True):
    """带真实 cookie jar 的客户端；``scheme`` 决定 Secure Cookie 能否发出。"""
    return TestClient(
        app,
        base_url=f"{scheme}://panel.test",
        follow_redirects=follow_redirects,
        raise_server_exceptions=raise_server_exceptions,
    )


def follow(client, resp, *, limit=5):
    """手动跟随跳转，模拟浏览器（不经 follow_redirects，便于逐跳取证）。"""
    hops = 0
    while resp.status_code in REDIRECTS and hops < limit:
        location = resp.headers.get("location", "")
        if not location:
            break
        resp = client.get(location)
        hops += 1
    return resp


def login(client, *, username=USERNAME, password=GOOD_PASSWORD):
    """提交登录表单（不自动跟随，返回那次 POST 的响应）。"""
    return client.post("/admin/login", data={"username": username, "password": password})


def login_and_land(client, **kwargs):
    """像浏览器一样登录，返回最终落在的页面响应。"""
    return follow(client, login(client, **kwargs))


def session_rows(db) -> list:
    return db.query_all("SELECT * FROM session ORDER BY rowid")


# ====================================================================== 正常路径


def test_login_writes_a_session_row_and_a_hardened_cookie(make_client, web_db):
    """口令校验通过后：服务端确实写了 session 行，Cookie 只带摘要。"""
    app, _ = make_client()
    make_admin(app)

    client = browser(app)
    post = login(client)

    assert post.status_code == 303
    assert post.headers["location"].startswith("/admin")

    rows = session_rows(web_db)
    assert len(rows) == 1, "登录成功必须写入恰好一条会话"

    cookie = client.cookies.get(security.SESSION_COOKIE)
    assert cookie, "浏览器的 cookie jar 里应当保存了会话 Cookie"

    token_hash = rows[0]["token_hash"]
    assert token_hash == security.token_digest(cookie), "库里存的应当是令牌的 sha256 摘要"
    assert token_hash != cookie, "明文令牌绝不入库"
    assert rows[0]["expires_at"] > rows[0]["created_at"]

    assert LOGGED_IN_MARK in follow(client, post).text


def test_the_cookie_is_honoured_on_every_later_request(make_client):
    """登录后连续访问受保护页面都不应再被弹回登录页。"""
    app, _ = make_client()
    make_admin(app)

    client = browser(app)
    assert follow(client, login(client)).url.path == "/admin"

    for path in ("/admin", "/admin/plans", "/admin/runs", "/admin/system",
                 "/admin/auth", "/admin/notifications"):
        page = client.get(path)
        assert page.status_code == 200, path
        assert LOGGED_IN_MARK in page.text, path


def test_wrong_password_starts_no_session(make_client, web_db):
    app, _ = make_client()
    make_admin(app)

    client = browser(app)
    landed = follow(client, login(client, password="definitely-wrong-1"))

    assert landed.url.path == "/admin/login"
    assert LOGIN_PAGE_MARK in landed.text
    assert security.SESSION_COOKIE not in client.cookies
    assert session_rows(web_db) == []


# ============================================ 登录循环：Secure Cookie 走 HTTP


def test_secure_cookie_over_http_reproduces_the_login_loop(make_client, web_settings):
    """复现线上症状：服务端一切正常，浏览器却永远停在登录页。

    证据链（每一步都有断言）：
      1. 服务端确实写了 session 行 —— 不是「会话没建起来」；
      2. 那次 303 上确实带了 ``Set-Cookie`` 且带 ``Secure`` —— 不是「没下发」；
      3. 浏览器照常把 Cookie 存进 jar，但因为当下是 http，**不会回传**；
      4. 于是 /admin 收到的是没有 Cookie 的请求 → 又跳回 /admin/login。
    """
    settings = web_settings.model_copy(update={"web_session_secure": True})
    app, _ = make_client(settings=settings)
    make_admin(app)

    client = browser(app, scheme="http")
    post = login(client)

    # (2) 下发了带 Secure 的 Cookie
    raw_cookie = post.headers.get("set-cookie", "")
    assert "Secure" in raw_cookie, raw_cookie

    # (1) 会话确实建立了
    assert len(session_rows(app.state.db)) == 1

    # (3) 浏览器把 Cookie 存下来了
    assert security.SESSION_COOKIE in client.cookies

    # (4) 但访问受保护页面时被弹回登录页 —— 循环发生
    landed = follow(client, post)
    assert landed.url.path == "/admin/login"
    assert LOGIN_PAGE_MARK in landed.text
    assert LOGGED_IN_MARK not in landed.text

    # 反证：把同一个令牌手工放进另一个请求头里，后台立刻可进 ——
    # 说明令牌和会话都是好的，差别只在「浏览器没有回传它」。
    same_token = client.cookies.get(security.SESSION_COOKIE)
    manual = TestClient(app, base_url="http://panel.test", follow_redirects=False)
    manual.cookies.set(security.SESSION_COOKIE, same_token)
    assert manual.get("/admin").status_code == 200


def test_the_same_config_works_once_the_scheme_is_https(make_client, web_settings):
    """同一份配置换成 https 访问就正常 —— 问题在协议，不在会话逻辑。"""
    settings = web_settings.model_copy(update={"web_session_secure": True})
    app, _ = make_client(settings=settings)
    make_admin(app)

    client = browser(app, scheme="https")
    landed = follow(client, login(client))

    assert landed.url.path == "/admin"
    assert LOGGED_IN_MARK in landed.text


def test_default_config_needs_no_https(make_client):
    """默认 SESSION_SECURE=false：http 下也正常，Cookie 上没有 Secure。"""
    app, _ = make_client()
    make_admin(app)

    client = browser(app, scheme="http")
    post = login(client)
    assert "Secure" not in post.headers.get("set-cookie", "")
    assert follow(client, post).url.path == "/admin"


# ============================================================ 会话有效期与失效


def test_expired_session_is_rejected_even_with_a_valid_cookie(make_client, web_db):
    app, _ = make_client()
    make_admin(app)

    client = browser(app)
    assert follow(client, login(client)).url.path == "/admin"

    past = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    with web_db.tx() as conn:
        conn.execute("UPDATE session SET expires_at = ?", (past,))

    landed = follow(client, client.get("/admin"))
    assert landed.url.path == "/admin/login"


def test_idle_timeout_ends_the_session(make_client, web_db):
    """空闲窗口由服务端判定，与 Cookie 的 Max-Age 无关。"""
    app, _ = make_client()
    make_admin(app)

    client = browser(app)
    assert follow(client, login(client)).url.path == "/admin"

    stale = (datetime.now() - timedelta(minutes=security.SESSION_IDLE_MINUTES + 1))
    with web_db.tx() as conn:
        conn.execute("UPDATE session SET last_seen_at = ?", (stale.strftime("%Y-%m-%d %H:%M:%S"),))

    assert client.get("/admin").headers.get("location") == "/admin/login"


def test_activity_extends_the_idle_window(make_client, web_db):
    """每次成功读取会话都会刷新 last_seen_at，活跃用户不会被踢。"""
    app, _ = make_client()
    make_admin(app)

    client = browser(app)
    follow(client, login(client))

    almost = (datetime.now() - timedelta(minutes=security.SESSION_IDLE_MINUTES - 5))
    with web_db.tx() as conn:
        conn.execute("UPDATE session SET last_seen_at = ?", (almost.strftime("%Y-%m-%d %H:%M:%S"),))

    assert client.get("/admin").status_code == 200
    refreshed = session_rows(web_db)[0]["last_seen_at"]
    assert refreshed > almost.strftime("%Y-%m-%d %H:%M:%S")


def test_logout_revokes_the_row_and_clears_the_cookie(make_client, web_db):
    app, _ = make_client()
    make_admin(app)

    client = browser(app)
    follow(client, login(client))
    page = client.get("/admin/system")
    assert page.status_code == 200

    out = client.post("/admin/logout", data={"_csrf": csrf_from(page.text)})
    assert follow(client, out).url.path == "/admin/login"

    assert session_rows(web_db) == []
    assert security.SESSION_COOKIE not in client.cookies


def csrf_from(html: str) -> str:
    """从页面里取出 CSRF 令牌（登录后的每个表单都带）。"""
    marker = 'name="_csrf" value="'
    start = html.index(marker) + len(marker)
    return html[start:html.index('"', start)]


# ============================================== 会话查不到时的其他可能：库


def test_a_second_database_sees_no_sessions(make_client, tmp_path, make_settings):
    """不同 web.db 之间不共享会话。

    线上对应风险：``HAUT_WEB_DB`` 默认是相对路径（``data/web.db``），
    相对**进程工作目录**解析。若 systemd 的 WorkingDirectory 与建库/排查时
    所在的目录不一致，就会出现「另一个库」：服务端一切正常、登录记录也在，
    但会话永远查不到 —— 症状与登录循环一模一样。
    """
    from haut_seat.web.db import Database

    other_path = tmp_path / "elsewhere" / "web.db"
    other_db = Database(str(other_path))
    other_db.init_schema()
    try:
        app, _ = make_client()
        make_admin(app)

        client = browser(app)
        assert follow(client, login(client)).url.path == "/admin"
        token = client.cookies.get(security.SESSION_COOKIE)

        other_settings = make_settings(
            web_db=str(other_path),
            enable_real_booking=False,
            pause_file=str(tmp_path / "pause.flag"),
            book_lock_file=str(tmp_path / "book.lock"),
        )
        make_admin_from(other_db)          # 另一个库也有管理员，否则会跳 setup
        other_app, _ = make_client(settings=other_settings, db=other_db)

        other_client = browser(other_app)
        other_client.cookies.set(security.SESSION_COOKIE, token)
        assert other_client.get("/admin").headers.get("location") == "/admin/login"
    finally:
        other_db.close_current_thread()


def make_admin_from(db, username=USERNAME, password=GOOD_PASSWORD):
    return security.create_admin(db, username, password)


def test_relative_database_path_follows_the_working_directory(tmp_path, monkeypatch,
                                                              make_settings):
    """相对路径按 **进程工作目录** 解析 —— 这是上面那条风险的直接证据。"""
    from haut_seat.web.db import Database

    (tmp_path / "sub").mkdir()
    settings = make_settings(web_db="data/web.db")

    # 在 tmp_path 下建库
    monkeypatch.chdir(tmp_path)
    db_a = Database(settings.web_db)
    db_a.init_schema()
    try:
        assert (tmp_path / "data" / "web.db").exists()
        # 同一个相对路径，换个工作目录指向的就是另一个文件
        monkeypatch.chdir(tmp_path / "sub")
        db_b = Database(settings.web_db)
        db_b.init_schema()
        try:
            assert (tmp_path / "sub" / "data" / "web.db").exists()
            assert (tmp_path / "sub" / "data" / "web.db") != (tmp_path / "data" / "web.db")
            # 两个库互不相干：A 里的管理员在 B 里看不到
            security.create_admin(db_a, USERNAME, GOOD_PASSWORD)
            assert security.count_admins(db_a) == 1
            assert security.count_admins(db_b) == 0
        finally:
            db_b.close_current_thread()
    finally:
        db_a.close_current_thread()


def test_a_bogus_cookie_is_rejected_without_a_server_error(make_client):
    app, _ = make_client()
    make_admin(app)

    client = browser(app)
    client.cookies.set(security.SESSION_COOKIE, "not-a-real-token")
    assert client.get("/admin").headers.get("location") == "/admin/login"


# ================================================================ 异常与边界


def test_missing_database_directory_is_created(make_settings, tmp_path):
    """数据库目录不存在时自动创建，不需要手工 mkdir。"""
    from haut_seat.web.db import Database

    nested = tmp_path / "a" / "b" / "web.db"
    db = Database(str(nested))
    db.init_schema()
    try:
        assert nested.exists()
        assert security.count_admins(db) == 0
    finally:
        db.close_current_thread()


def test_broken_database_degrades_to_an_error_page(make_client, web_db):
    """数据库异常不得把堆栈、路径或 SQL 细节吐给浏览器。"""
    app, _ = make_client()
    make_admin(app)

    client = browser(app, raise_server_exceptions=False)
    follow(client, login(client))

    with web_db.tx() as conn:
        conn.execute("ALTER TABLE session RENAME TO session_broken")

    resp = client.get("/admin")

    assert resp.status_code == 500
    assert "Traceback" not in resp.text
    assert "session_broken" not in resp.text
    assert "OperationalError" not in resp.text
    assert "sqlite" not in resp.text.lower()


def test_anonymous_post_to_a_protected_route_is_401(make_client):
    """未登录的 POST 直接 401，GET 才跳登录页（不暴露行为差异）。"""
    app, _ = make_client()
    make_admin(app)

    assert browser(app).post("/admin/system/toggle", data={}).status_code == 401


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_session_token_never_leaks_into_the_page(make_client, scheme):
    app, _ = make_client()
    make_admin(app)

    client = browser(app, scheme=scheme)
    follow(client, login(client))
    token = client.cookies.get(security.SESSION_COOKIE)
    assert token, "该协议下 Cookie 应当可用"
    for path in ("/admin", "/admin/system", "/admin/plans"):
        assert token not in client.get(path).text


def test_the_login_fixtures_never_enable_real_booking(make_client, web_settings):
    """端到端夹具必须始终关闭真实预约（仓库根的 .env 可能与测试同目录）。"""
    app, _ = make_client()
    assert app.state.settings.enable_real_booking is False
    assert security.count_admins(app.state.db) == 0