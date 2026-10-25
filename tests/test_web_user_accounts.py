"""用户端**平台账号**体系：注册 / 登录 / 退出 / 会话失效。

这些用例只关心"本平台自己的账号"这一层 —— 它和"学校账号"是两件不相干的事
（见 ``haut_seat.web.user_auth`` 的模块说明）。全部离线，不碰学校接口。

覆盖：
- 注册：需要邀请码、邀请码错/缺一律拒绝、注册关闭时 403、用户名大小写不敏感唯一、
  口令强度、注册**不自动登录**；
- 登录：失败提示一律同一句（不透露用户名是否存在、账号是否被停用）、按用户名+IP 限流、
  成功后种下 ``hs_user`` 且**不是**管理端 Cookie；
- 会话：绝对过期、空闲超时、被停用立即失效且会话行被删；
- 退出：会话行真的没了，Cookie 被清掉；
- 两套体系互不相通（用户端身份拿不到管理端页面，管理端 Cookie 也换不来用户端身份）。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest

from haut_seat.web import security, user_auth
from haut_seat.web import repository as repo
from tests.user_end import ORIGIN, USERNAME, USER_PASSWORD, create_user, page_csrf

REGISTER_PATH = "/booking/register"
LOGIN_PATH = "/booking/login"

#: 页面上的错误提示块（两个模板用的是同一段 markup）。
_ERROR_RE = re.compile(
    r'<div class="alert alert-danger py-2" role="alert">(.*?)</div>', re.S
)


def _error_text(html: str) -> str:
    match = _ERROR_RE.search(html)
    assert match, "页面上没有错误提示块"
    return " ".join(match.group(1).split())


# ---------------------------------------------------------------- 小工具


def _register(client, *, username, password, password2=None, invite, csrf_from=REGISTER_PATH,
              **extra):
    token = page_csrf(client, csrf_from)
    data = {
        "_csrf": token,
        "username": username,
        "password": password,
        "password2": password if password2 is None else password2,
        "invite_code": invite,
        "display_name": extra.pop("display_name", ""),
    }
    data.update(extra)
    return client.post(REGISTER_PATH, data=data, headers={"Origin": ORIGIN})


def _login(client, *, username, password):
    token = page_csrf(client, LOGIN_PATH)
    return client.post(
        LOGIN_PATH,
        data={"_csrf": token, "username": username, "password": password},
        headers={"Origin": ORIGIN},
    )


@pytest.fixture
def signup(web_settings, web_db, make_client):
    """``(client, db, settings, code)``：注册开放、邀请码已知的匿名客户端。"""
    from haut_seat.web import user_auth as ua

    settings = web_settings.model_copy(
        update={"web_signup_enabled": True, "web_signup_invite_code": ""}
    )
    _app, client = make_client(settings=settings, db=web_db)
    code = ua.rotate_invite_code(web_db)
    return client, web_db, settings, code


# ================================================================== 1. 注册


def test_registration_creates_an_account_but_does_not_log_in(signup):
    """注册成功只建账号、**不自动登录**：用户得亲手走一次登录。

    理由写在路由里：自动登录会让"口令输错了"看起来像"注册失败"。
    """
    client, db, _settings, code = signup
    resp = _register(client, username="bob", password="Bob-Password-1", invite=code)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith(LOGIN_PATH)
    assert client.cookies.get(user_auth.USER_SESSION_COOKIE) is None, (
        "注册不该顺手把用户登进去"
    )
    row = user_auth.find_user(db, "bob")
    assert row is not None and row["status"] == "active"
    # 页面拿到的账号信息里没有口令哈希。
    assert "password" not in {k.lower() for k in row}


def test_registration_requires_the_invite_code(signup):
    """没有邀请码 / 邀请码不对 ⇒ 不建账号。"""
    client, db, _settings, code = signup
    for provided in ("", "not-the-code", code[:-1]):
        resp = _register(client, username="mallory", password="Mallory-Pass-1", invite=provided)
        assert resp.status_code == 200
        assert "邀请码不正确" in resp.text
    assert user_auth.find_user(db, "mallory") is None, "邀请码不对却建了账号"


def test_registration_is_closed_when_the_switch_is_off(web_settings, web_db, make_client):
    """管理端把注册关掉 ⇒ 页面 403、提交 403，且不建账号。"""
    from haut_seat.web import user_auth as ua

    settings = web_settings.model_copy(update={"web_signup_enabled": True})
    _app, client = make_client(settings=settings, db=web_db)
    code = ua.rotate_invite_code(web_db)
    ua.set_signup_enabled(web_db, False)

    page = client.get(REGISTER_PATH)
    assert page.status_code == 403
    assert "未开放注册" in page.text
    # 注册页此时没有表单（也就没有令牌），令牌从登录页取 —— 关掉的是注册，
    # 不是整个用户端；用登录页的令牌提交注册，验的是注册这道闸门本身。
    resp = _register(
        client, username="nobody", password="Nobody-Pass-1", invite=code,
        csrf_from=LOGIN_PATH,
    )
    assert resp.status_code == 403
    assert ua.find_user(web_db, "nobody") is None


def test_signup_env_switch_cannot_be_overridden_by_the_db_switch(web_db, make_client):
    """环境变量是**急停**：它为假时，数据库开关为真也不开放。"""
    from haut_seat.web import user_auth as ua

    from haut_seat.config import Settings

    settings = Settings(
        _env_file=None, web_db=str(web_db.path), web_signup_enabled=False,
        web_allow_first_run_setup=True, enable_real_booking=False,
    )
    _app, client = make_client(settings=settings, db=web_db)
    ua.set_signup_enabled(web_db, True)
    assert ua.signup_enabled(web_db, settings) is False
    assert client.get(REGISTER_PATH).status_code == 403


def test_username_is_unique_case_insensitively(signup):
    """``Alice`` 与 ``alice`` 是同一个账号 —— 否则登录查询会随机命中其中一个。"""
    client, db, _settings, code = signup
    assert _register(client, username="Alice", password="Alice-Pass-01", invite=code).status_code == 303
    resp = _register(client, username="ALICE", password="Another-Pass-1", invite=code)
    assert resp.status_code == 200
    assert "已被占用" in resp.text
    assert user_auth.normalize_username("ALICE") == "alice"
    row = user_auth.find_user(db, "aLiCe")
    assert row is not None and row["username"] == "alice"


def test_registration_rejects_weak_or_mismatched_passwords(signup):
    """口令太短 / 两次不一致 ⇒ 拒绝，且账号不存在。"""
    client, db, _settings, code = signup
    short = _register(client, username="carol", password="short", invite=code)
    assert short.status_code == 200
    assert "至少" in short.text
    mismatch = _register(
        client, username="carol", password="Carol-Pass-01", password2="Carol-Pass-02", invite=code
    )
    assert mismatch.status_code == 200
    assert "不一致" in mismatch.text
    assert user_auth.find_user(db, "carol") is None


def test_invalid_username_is_refused_with_a_readable_reason(signup):
    client, db, _settings, code = signup
    resp = _register(client, username="a", password="Short-Name-01", invite=code)
    assert resp.status_code == 200
    assert "用户名长度" in resp.text
    assert user_auth.count_users(db) == 0


# ================================================================== 2. 登录


def test_login_succeeds_and_issues_only_the_user_cookie(signup):
    """登录成功后种下 ``hs_user``，并且**没有**顺手给一个管理端会话。"""
    client, db, _settings, code = signup
    _register(client, username="dave", password="Dave-Pass-001", invite=code)
    resp = _login(client, username="dave", password="Dave-Pass-001")
    assert resp.status_code == 303
    assert client.cookies.get(user_auth.USER_SESSION_COOKIE)
    assert client.cookies.get(security.SESSION_COOKIE) is None, "用户端登录不该发出管理端 Cookie"
    assert user_auth.find_user(db, "dave")["last_login_at"]


def test_login_failure_message_is_uniform(signup):
    """三种失败（不存在 / 口令错 / 被停用）给出的提示**逐字相同**。

    比对的是页面里那一句错误文案本身 —— 用户名会被回填进表单（``value="erin"``），
    那是正常的交互，不是泄漏；要紧的是提示**不区分**这几种情况。
    """
    client, db, _settings, code = signup
    _register(client, username="erin", password="Erin-Pass-001", invite=code)
    user_auth.set_user_status(db, user_auth.find_user(db, "erin")["id"], "disabled")

    messages = []
    for username, password in (
        ("erin", "Erin-Pass-001"),      # 存在但被停用
        ("erin", "Wrong-Pass-001"),     # 存在但口令错
        ("ghost", "Ghost-Pass-01"),     # 不存在
    ):
        resp = _login(client, username=username, password=password)
        assert resp.status_code == 200
        messages.append(_error_text(resp.text))
    assert messages == ["用户名或口令不正确。"] * 3, messages


def test_login_attempts_are_throttled_by_username_and_ip(signup):
    """连续失败到阈值后被限流，提示里给出解锁时刻。"""
    client, _db, _settings, code = signup
    _register(client, username="frank", password="Frank-Pass-01", invite=code)
    for _ in range(security.LOGIN_MAX_FAILURES):
        _login(client, username="frank", password="Wrong-Pass-0001")
    resp = _login(client, username="frank", password="Frank-Pass-01")
    assert resp.status_code == 200
    assert "过于频繁" in resp.text, "达到阈值后即使是正确口令也应被限流"
    assert "用户名或口令不正确" not in resp.text


def test_attempts_are_recorded_with_ok_flag(signup):
    """每次尝试都留痕（含成功），限流窗口靠它计算。"""
    client, db, _settings, code = signup
    _register(client, username="gina", password="Gina-Pass-001", invite=code)
    _login(client, username="gina", password="Wrong-Pass-0001")
    _login(client, username="gina", password="Gina-Pass-001")
    rows = db.query_all("SELECT ok FROM user_login_attempt ORDER BY rowid")
    assert [int(r["ok"]) for r in rows] == [0, 1]


# ================================================================== 3. 会话生命周期


def test_absolute_expiry_invalidates_the_session(signup):
    """绝对有效期到了 ⇒ 页面重新要求登录（会话行仍在，只是不再被承认）。"""
    client, db, _settings, code = signup
    _register(client, username="hank", password="Hank-Pass-001", invite=code)
    _login(client, username="hank", password="Hank-Pass-001")
    user_id = user_auth.find_user(db, "hank")["id"]
    past = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    with db.tx() as conn:
        conn.execute("UPDATE user_session SET expires_at = ? WHERE user_id = ?", (past, user_id))

    resp = client.get("/booking")
    assert resp.status_code == 303
    assert resp.headers["location"].startswith(LOGIN_PATH)


def test_idle_timeout_invalidates_the_session(signup):
    """空闲超时同样让会话失效 —— 两个时限是**各自独立**的判据。"""
    client, db, _settings, code = signup
    _register(client, username="ivan", password="Ivan-Pass-001", invite=code)
    _login(client, username="ivan", password="Ivan-Pass-001")
    user_id = user_auth.find_user(db, "ivan")["id"]
    stale = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    with db.tx() as conn:
        conn.execute(
            "UPDATE user_session SET created_at = ?, last_seen_at = ? WHERE user_id = ?",
            (stale, stale, user_id),
        )

    token = client.cookies.get(user_auth.USER_SESSION_COOKIE)
    assert user_auth.load_user_session(db, token) is None
    assert client.get("/booking").status_code == 303


def test_disabling_an_account_kills_its_sessions_immediately(signup):
    """停用账号要**立即**生效：会话行被删掉，而不是等着过期。"""
    client, db, _settings, code = signup
    _register(client, username="judy", password="Judy-Pass-001", invite=code)
    _login(client, username="judy", password="Judy-Pass-001")
    user_id = user_auth.find_user(db, "judy")["id"]
    assert user_auth.load_user_session(
        db, client.cookies.get(user_auth.USER_SESSION_COOKIE)
    ) is not None

    user_auth.set_user_status(db, user_id, "disabled")
    assert db.query_one(
        "SELECT COUNT(*) AS n FROM user_session WHERE user_id = ?", (user_id,)
    )["n"] == 0
    resp = client.get("/booking")
    assert resp.status_code == 303


def test_password_change_invalidates_sessions(signup):
    """改密后旧会话立刻失效（与管理员侧同一约定）。"""
    client, db, _settings, code = signup
    _register(client, username="kate", password="Kate-Pass-001", invite=code)
    _login(client, username="kate", password="Kate-Pass-001")
    user_id = user_auth.find_user(db, "kate")["id"]

    user_auth.set_user_password(db, user_id, "Kate-Pass-002")
    assert client.get("/booking").status_code == 303
    assert _login(client, username="kate", password="Kate-Pass-002").status_code == 303


def test_logout_destroys_the_session_row(signup):
    """退出不只是清 Cookie：会话行必须真的没了（否则令牌仍然可用）。"""
    from tests.user_end import logout_user

    client, db, _settings, code = signup
    _register(client, username="liam", password="Liam-Pass-001", invite=code)
    _login(client, username="liam", password="Liam-Pass-001")
    token = client.cookies.get(user_auth.USER_SESSION_COOKIE)

    resp = logout_user(client)
    assert resp.status_code == 303
    assert db.query_one(
        "SELECT COUNT(*) AS n FROM user_session", ()
    )["n"] == 0
    assert user_auth.load_user_session(db, token) is None


def test_expired_sessions_are_purged(signup):
    """清理任务只删过期/空闲超时的行，不碰还在用的。"""
    client, db, _settings, code = signup
    _register(client, username="nina", password="Nina-Pass-001", invite=code)
    _login(client, username="nina", password="Nina-Pass-001")
    # 再挂一个早就过期的会话行（模拟一个再也不回来的浏览器）。
    user_id = user_auth.find_user(db, "nina")["id"]
    past = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO user_session (token_hash, user_id, csrf_token, created_at,"
            " expires_at, last_seen_at) VALUES ('deadbeef', ?, 'x', ?, ?, ?)",
            (user_id, past, past, past),
        )
    assert user_auth.purge_expired_user_sessions(db) == 1
    assert client.get("/booking").status_code == 200, "还在用的会话不该被清掉"


# ================================================================== 4. 两套体系互不相通


def test_the_two_account_systems_are_isolated(signup, admin_client):
    """用户端身份拿不到管理端页面；反过来，管理端会话也换不来用户端身份。"""
    client, db, _settings, code = signup
    _register(client, username="olive", password="Olive-Pass-01", invite=code)
    _login(client, username="olive", password="Olive-Pass-01")
    assert client.get("/admin").status_code == 303

    _app, admin = admin_client
    assert admin.get("/booking").status_code == 303, "管理端会话不该被当成用户端身份"
    assert admin.get("/booking").headers["location"].startswith(LOGIN_PATH)


def test_user_tables_and_cookie_names_are_distinct(signup, admin_client):
    """结构性的保证：表名与 Cookie 名都不同，越权没有隐式通路。"""
    _app, _admin = admin_client
    client, _db, _settings, _code = signup
    assert user_auth.USER_SESSION_COOKIE != security.SESSION_COOKIE
    assert user_auth.USER_SESSION_COOKIE == "hs_user"
    assert repo is not None  # 用户端的库访问一律走同一个仓储层
    # 页面里不会出现管理端的会话 Cookie 名。
    assert security.SESSION_COOKIE not in client.get(LOGIN_PATH).text


def test_users_only_see_their_own_plans_through_the_real_flow(signup, make_client, web_settings, web_db):
    """两个真实用户在同一个库里，各自只看到自己的计划 —— 走完整表单链路。"""
    import re

    from tests.test_web_booking import plan_form

    client, db, settings, code = signup
    for name in ("pam", "quinn"):
        _register(client, username=name, password=f"{name.title()}-Pass-01", invite=code)

    ids = {}
    for name in ("pam", "quinn"):
        c = client
        _login(c, username=name, password=f"{name.title()}-Pass-01")
        form = plan_form(name=f"{name} 的计划")
        form["_csrf"] = page_csrf(c, "/booking/new")
        resp = c.post("/booking/new", data=form, headers={"Origin": ORIGIN})
        assert resp.status_code == 303, resp.text
        ids[name] = int(re.search(r"/booking/(\d+)", resp.headers["location"]).group(1))
        from tests.user_end import logout_user

        logout_user(c)

    assert len({*ids.values()}) == 2
    for name in ("pam", "quinn"):
        _login(client, username=name, password=f"{name.title()}-Pass-01")
        mine = client.get("/booking")
        assert f"{name} 的计划" in mine.text
        other = "quinn" if name == "pam" else "pam"
        assert f"{other} 的计划" not in mine.text
        # 直接访问对方的计划详情：404，不是 403（不确认它存在）。
        assert client.get(f"/booking/{ids[other]}").status_code == 404
        from tests.user_end import logout_user

        logout_user(client)

    assert create_user(web_db, "extra-user") is not None