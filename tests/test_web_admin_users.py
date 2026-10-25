"""管理端的用户页：**看得到"谁在用哪个学校账号"，看不到凭据本身**。

管理端在多用户体系里的角色是"统一入口 + 运维视图"（见 ``routes.py`` 里
``/admin/users`` 那一段的说明）。这一页与这些列有一个共同的硬要求：
它能回答"这条计划执行时用的是谁的账号"，但**拿不到**任何凭据值 ——
不是"渲染时小心一点"，而是喂给模板的数据里根本不存在 Cookie / Authorization
字段（``repo.get_user_credential_view`` 的列里就没有密文）。

覆盖：
- 页面只显示元数据（用户名、学校账号标识、有效性、指纹前缀），
  明文凭据、密文、口令哈希一个都不出现；
- 五个写入口（停用/启用、重置口令、删除、轮换邀请码、开关注册）都走
  管理端会话 + CSRF，而且**状态真的变了**；
- 越权与非法输入：未登录 GET 跳登录页、未登录 POST 401、CSRF 不对 400 且不改状态、
  用户 id 不存在 404；
- 计划 / 运行四个列表与两个详情页都有「所属用户」列，无主计划如实留空；
- 删账号保留计划与运行记录，但把他的密文凭据一起带走（不留没人管的密文）。

全部离线：不碰学校接口，不启动调度器 tick，运行行是直接写库的**测试数据**。
"""

from __future__ import annotations

import re

import pytest

from haut_seat.services import user_credentials as uc
from haut_seat.services.auth_store import fingerprint
from haut_seat.services.state import PlanStatus
from haut_seat.web import repository as repo
from haut_seat.web import security, user_auth
from haut_seat.web.validation import parse_plan
from tests.user_end import (
    FAKE_SCHOOL_AUTHORIZATION,
    FAKE_SCHOOL_COOKIE,
    FAKE_SCHOOL_USERNAME,
    bind_credentials,
    create_user,
    login_user,
)

ADMIN_PASSWORD = "Correct-Horse-9"
USERS_PATH = "/admin/users"
#: 假凭据里的哨兵串。它出现在任何一个页面里都说明凭据漏了。
SENTINEL = "FAKETESTSENTINEL"
#: Cookie 的前缀形态本身也是敏感信息（它能让人认出"这就是那份令牌"）。
COOKIE_PREFIX = "Admin-Token="

_CSRF_RE = re.compile(r'name="_csrf" value="([^"]+)"')


# ---------------------------------------------------------------- 小工具


def page_csrf(client, path: str = USERS_PATH) -> str:
    """取一个管理端页面里的 CSRF 令牌。"""
    resp = client.get(path)
    assert resp.status_code == 200, (path, resp.status_code)
    match = _CSRF_RE.search(resp.text)
    assert match, f"{path} 页面里没有 CSRF 字段"
    return match.group(1)


def apost(client, path: str, data: dict | None = None, *, csrf_from: str = USERS_PATH):
    """带上当前管理端页面的 CSRF 令牌发 POST（管理端 POST 不要求 Origin）。"""
    payload = dict(data or {})
    payload.setdefault("_csrf", page_csrf(client, csrf_from))
    return client.post(path, data=payload)


def flash(resp) -> str:
    """从 303 的 Location 里取出提示文本（query string 是 URL 编码的）。"""
    from urllib.parse import parse_qs, unquote, urlparse

    query = parse_qs(urlparse(unquote(resp.headers["location"])).query)
    return (query.get("msg") or [""])[0]


def plan_form(*, name: str = "用户端建的", owner: int | None = None) -> tuple[dict, dict]:
    """一份必然通过服务端校验的计划表单 + 解析后的数据。"""
    from datetime import date, timedelta

    day = date.today() + timedelta(days=1)
    form = {
        "name": name,
        "run_date": day.strftime("%Y-%m-%d"),
        "repeat_rule": "once",
        "trigger_at": "07:00",
        "book_start": day.strftime("%Y-%m-%d") + " 08:00:00",
        "book_end": day.strftime("%Y-%m-%d") + " 12:00:00",
        "regionid": "16",
        "preferred_seats": "十层东书库北区 01桌 B座",
        "preferred_prefixes": "",
        "confirmed_bookable_states": '[["2", 1, false]]',
        "max_attempts_per_seat": "3",
        "note": "",
    }
    data, _ = parse_plan(form, today=date.today())
    return form, data


def make_owned_plan(db, owner: int | None = None, *, name: str = "计划") -> int:
    """建一条计划；``owner`` 非空时挂上归属（与用户端建计划同一条仓储路径）。"""
    _form, data = plan_form(name=name)
    plan_id = repo.create_plan(db, data, enabled=True)
    repo.set_plan_status(db, plan_id, PlanStatus.PENDING)
    if owner is not None:
        repo.set_plan_owner(db, plan_id, owner)
    return plan_id


def add_run_row(db, plan_id: int) -> int:
    """直接写一条**测试用**的运行记录（页面渲染只关心它属于哪条计划）。

    不启动调度器、不发任何请求：这里测的是"运行记录能不能追到归属用户"，
    造一条真的运行会让用例依赖线程与时钟，与这条断言无关。
    """
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO run (plan_id, started_at, finished_at, status, message)"
            " VALUES (?, ?, ?, ?, ?)",
            (plan_id, "2026-10-18 08:00:00", "2026-10-18 08:00:05", "succeeded", "测试预置"),
        )
        return int(cur.lastrowid)


# ---------------------------------------------------------------- 夹具


@pytest.fixture
def admin(make_client, web_settings, web_db):
    """已登录的**管理端**客户端（与 ``web_db`` 是同一个库文件）。"""
    _app, client = make_client(settings=web_settings, db=web_db)
    security.create_admin(web_db, "tester", ADMIN_PASSWORD)
    resp = client.post(
        "/admin/login", data={"username": "tester", "password": ADMIN_PASSWORD}
    )
    assert resp.status_code == 303, resp.text
    return client


@pytest.fixture
def bound_user(web_db, web_settings):
    """一个已绑定（**假**）学校凭据的用户端账号，返回 user_id。"""
    user_id = create_user(web_db, "alice")
    bind_credentials(web_db, web_settings, user_id)
    return user_id


@pytest.fixture
def user_client(make_client, web_settings, web_db):
    """另外开一个用户端客户端（用来验证"停用/改密后会话立即作废"）。"""
    _app, client = make_client(settings=web_settings, db=web_db)
    return client


# ================================================================== 1. 页面只显示元数据


def test_users_page_requires_an_admin_session(make_client, web_settings, web_db):
    """未登录：GET 跳登录页，POST 直接 401（不泄露任何东西）。"""
    _app, client = make_client(settings=web_settings, db=web_db)
    page = client.get(USERS_PATH)
    assert page.status_code == 303
    assert page.headers["location"] == "/admin/login"

    resp = client.post(f"{USERS_PATH}/1/status", data={"_csrf": "x", "status": "disabled"})
    assert resp.status_code == 401


def test_users_page_shows_metadata_but_never_credential_values(admin, bound_user, web_db):
    """页面上有学校账号标识、有效性、指纹前缀 —— 没有明文、没有密文、没有口令哈希。"""
    text = admin.get(USERS_PATH).text
    assert "alice" in text
    # 学校账号是**标识**（学号/工号），管理员正需要它来回答"用的是哪个账号"。
    assert FAKE_SCHOOL_USERNAME in text
    assert uc.CRED_STATUS_LABELS[uc.CRED_STATUS_VALID] in text
    # 指纹是单向摘要前缀，可以展示；它必须与库里那一份一致。
    assert fingerprint(FAKE_SCHOOL_AUTHORIZATION) in text

    # 凭据值（明文与它的可辨认前缀）、密文、口令哈希，一个都不能出现。
    assert SENTINEL not in text
    assert COOKIE_PREFIX not in text
    assert "Bearer " not in text
    assert "hsc1$" not in text
    raw = repo.get_user_credential(web_db, bound_user)
    assert raw["cookie_enc"] and raw["cookie_enc"] not in text
    assert raw["authorization_enc"] not in text
    stored_hash = web_db.query_one(
        "SELECT password_hash FROM app_user WHERE id = ?", (bound_user,)
    )["password_hash"]
    assert stored_hash and stored_hash not in text


def test_users_page_marks_users_without_credentials(admin, web_db):
    """未绑定的用户如实显示「未绑定」，不猜、不留空。"""
    create_user(web_db, "bob")
    text = admin.get(USERS_PATH).text
    assert "bob" in text
    assert uc.CRED_STATUS_LABELS[uc.CRED_STATUS_NONE] in text


def test_users_page_counts_each_users_own_plans(admin, web_db, bound_user):
    """计划数按归属统计：别人的计划不会算到他头上。"""
    other = create_user(web_db, "bob")
    make_owned_plan(web_db, bound_user, name="alice 的")
    make_owned_plan(web_db, bound_user, name="alice 的第二个")
    make_owned_plan(web_db, other, name="bob 的")
    make_owned_plan(web_db, None, name="管理端的")

    rows = {u["username"]: u for u in user_auth.list_users(web_db)}
    assert rows["alice"]["plan_count"] == 2
    assert rows["bob"]["plan_count"] == 1
    # 无主的计划不属于任何用户，因此谁的计划数里都不该多出这一条。
    assert sum(u["plan_count"] for u in rows.values()) == 3
    assert "用户列表（2 人）" in admin.get(USERS_PATH).text, "用户总数应当如实显示"


def test_empty_state_is_honest(admin):
    """一个人都没注册时页面直说，而不是显示一张空表让人猜。"""
    text = admin.get(USERS_PATH).text
    assert "还没有任何用户端账号" in text


# ================================================================== 2. 注册入口的两个动作


def test_rotating_the_invite_code_replaces_it(admin, web_db, web_settings):
    old = user_auth.rotate_invite_code(web_db)
    resp = apost(admin, f"{USERS_PATH}/invite")
    assert resp.status_code == 303
    new = user_auth.effective_invite_code(web_db, web_settings)
    assert new and new != old
    assert new in flash(resp), "提示里要给出新码，否则管理员得再去页面上找一次"
    assert new in admin.get(USERS_PATH).text


def test_signup_toggle_flips_the_db_switch_both_ways(admin, web_db, web_settings):
    user_auth.set_signup_enabled(web_db, False)
    on = apost(admin, f"{USERS_PATH}/signup", {"signup_enabled": "1"})
    assert on.status_code == 303
    assert user_auth.signup_enabled(web_db, web_settings) is True
    assert "已开启" in flash(on)

    off = apost(admin, f"{USERS_PATH}/signup", {"signup_enabled": "0"})
    assert off.status_code == 303
    assert user_auth.signup_enabled(web_db, web_settings) is False
    assert "已关闭" in flash(off)
    # 关掉的只是注册入口，已登录用户不受影响 —— 这句要写进提示里。
    assert "已登录用户不受影响" in flash(off)


# ================================================================== 3. 账号动作


def test_disabling_a_user_kills_sessions_and_can_be_undone(
    admin, user_client, web_db, bound_user
):
    login_user(user_client, web_db, "alice")
    assert user_client.cookies.get(user_auth.USER_SESSION_COOKIE)

    resp = apost(admin, f"{USERS_PATH}/{bound_user}/status", {"status": "disabled"})
    assert resp.status_code == 303
    message = flash(resp)
    assert "已停用用户 alice" in message
    assert "会话已作废" in message and "计划不会被执行" in message
    assert user_auth.get_user(web_db, bound_user)["status"] == "disabled"
    assert web_db.query_one(
        "SELECT COUNT(*) AS n FROM user_session WHERE user_id = ?", (bound_user,)
    )["n"] == 0
    assert user_client.get("/booking").status_code == 303, "停用必须立即生效"
    # 凭据**保留**：停用是"先别跑"，不是"替你解绑"。
    assert repo.get_user_credential(web_db, bound_user) is not None

    back = apost(admin, f"{USERS_PATH}/{bound_user}/status", {"status": "active"})
    assert back.status_code == 303
    assert "已启用用户 alice" in flash(back)
    assert user_auth.get_user(web_db, bound_user)["status"] == "active"


def test_reset_password_refuses_weak_then_invalidates_sessions(
    admin, make_client, web_settings, web_db, bound_user
):
    """口令太弱 ⇒ 拒绝且**原口令仍然有效**；重置成功 ⇒ 他所有会话立即作废。

    每条"能不能登录"的验证都用一个**干净的客户端**：已登录的客户端再打开
    ``/booking/login`` 会被直接跳回 ``/booking``（这是既有行为），拿它来验证
    口令必然测不出东西。
    """
    from tests.user_end import USER_PASSWORD, ORIGIN

    _app, logged_in = make_client(settings=web_settings, db=web_db)
    login_user(logged_in, web_db, "alice")

    weak = apost(admin, f"{USERS_PATH}/{bound_user}/password", {"password": "short"})
    assert weak.status_code == 303
    assert "口令不符合要求" in flash(weak)
    # 被拒绝时口令没变。
    _app2, probe = make_client(settings=web_settings, db=web_db)
    token = page_csrf(probe, "/booking/login")
    still_ok = probe.post(
        "/booking/login",
        data={"_csrf": token, "username": "alice", "password": USER_PASSWORD},
        headers={"Origin": ORIGIN},
    )
    assert still_ok.status_code == 303, "被拒绝的重置不该改动口令"

    fresh = "Brand-New-Pass-9"
    ok = apost(admin, f"{USERS_PATH}/{bound_user}/password", {"password": fresh})
    assert ok.status_code == 303
    assert "已重置 alice 的口令" in flash(ok)
    assert web_db.query_one(
        "SELECT COUNT(*) AS n FROM user_session WHERE user_id = ?", (bound_user,)
    )["n"] == 0, "重置口令后原有会话必须全部作废"
    assert logged_in.get("/booking").status_code == 303
    # 提示里要交代"新口令得你自己交给本人"，但口令本身绝不回显。
    assert "交给本人" in flash(ok)
    assert fresh not in flash(ok)

    # 旧口令不再可用、新口令可用 —— 各用一个干净客户端验。
    _app3, old_probe = make_client(settings=web_settings, db=web_db)
    token = page_csrf(old_probe, "/booking/login")
    rejected = old_probe.post(
        "/booking/login",
        data={"_csrf": token, "username": "alice", "password": USER_PASSWORD},
        headers={"Origin": ORIGIN},
    )
    assert rejected.status_code == 200 and "用户名或口令不正确" in rejected.text

    _app4, new_probe = make_client(settings=web_settings, db=web_db)
    token = page_csrf(new_probe, "/booking/login")
    accepted = new_probe.post(
        "/booking/login",
        data={"_csrf": token, "username": "alice", "password": fresh},
        headers={"Origin": ORIGIN},
    )
    assert accepted.status_code == 303


def test_deleting_a_user_keeps_plans_and_runs_but_drops_the_ciphertext(
    admin, web_db, bound_user
):
    """删账号：计划与运行记录留下（防重复的证据），他那一份密文凭据一起带走。"""
    plan_id = make_owned_plan(web_db, bound_user)
    run_id = add_run_row(web_db, plan_id)

    resp = apost(admin, f"{USERS_PATH}/{bound_user}/delete")
    assert resp.status_code == 303
    assert "已删除用户 alice" in flash(resp)
    assert "保留" in flash(resp)

    assert user_auth.get_user(web_db, bound_user) is None
    assert repo.get_plan(web_db, plan_id) is not None, "计划不能被连带删掉"
    assert repo.get_run(web_db, run_id) is not None, "运行记录一条都不能少"
    assert repo.get_plan_owner(web_db, plan_id) is None, "归属行随账号消失"
    # 不留一份没人管的密文：账号没了，他的凭据也不该继续躺在库里。
    assert repo.get_user_credential(web_db, bound_user) is None

    # 归属消失之后，页面按"无主"如实显示，而不是继续挂着一个不存在的用户名。
    text = admin.get("/admin/plans").text
    assert "—（管理端账号）" in text
    assert "alice" not in text


def test_unknown_user_id_is_404(admin):
    """对一个不存在的用户做任何动作都是 404，不是"悄悄什么也没发生"。"""
    for action, payload in (
        ("status", {"status": "disabled"}),
        ("password", {"password": "Whatever-Pass-1"}),
        ("delete", {}),
    ):
        resp = apost(admin, f"{USERS_PATH}/9999/{action}", payload)
        assert resp.status_code == 404, (action, resp.status_code)


# ================================================================== 4. CSRF


def test_admin_writes_reject_a_bad_csrf_token(admin, web_db, bound_user):
    """令牌不对 ⇒ 400，并且**状态一点没变**（校验必须在动作之前）。"""
    resp = apost(
        admin, f"{USERS_PATH}/{bound_user}/status", {"status": "disabled", "_csrf": "wrong"}
    )
    assert resp.status_code == 400
    assert "CSRF" in resp.text
    assert user_auth.get_user(web_db, bound_user)["status"] == "active"

    # 缺字段与空串同样拒绝（这里不能用 ``apost``：它会补一个**合法**令牌）。
    for payload in ({}, {"_csrf": ""}):
        missing = admin.post(f"{USERS_PATH}/{bound_user}/delete", data=payload)
        assert missing.status_code == 400, payload
    assert user_auth.get_user(web_db, bound_user) is not None


# ================================================================== 5. 计划的「所属用户」


def test_plan_lists_and_details_name_the_owner(admin, web_db, bound_user):
    """看板 / 计划列表 / 计划详情 / 运行列表 / 运行详情都能追到归属用户。"""
    owned = make_owned_plan(web_db, bound_user, name="alice 的计划")
    legacy = make_owned_plan(web_db, None, name="历史计划")
    run_id = add_run_row(web_db, owned)
    # 无主计划也造一条运行记录：这样「所属用户」这一列在**有**归属与**无**归属
    # 两种情形下都被真正渲染到，"如实留空"才有东西可断言。
    legacy_run = add_run_row(web_db, legacy)

    for path in ("/admin", "/admin/plans", "/admin/runs"):
        text = admin.get(path).text
        assert "alice" in text, f"{path} 少了所属用户"
        assert "—（管理端账号）" in text, f"{path} 应当如实标出无主计划"
        assert SENTINEL not in text and COOKIE_PREFIX not in text, path

    detail = admin.get(f"/admin/plans/{owned}").text
    assert "所属用户：alice" in detail
    assert SENTINEL not in detail and COOKIE_PREFIX not in detail

    legacy_detail = admin.get(f"/admin/plans/{legacy}").text
    # 无主计划**不编**一个归属用户，但也不含糊：如实写出"这用的是管理端账号"。
    assert "所属用户：—（管理端账号）" in legacy_detail
    assert SENTINEL not in legacy_detail and COOKIE_PREFIX not in legacy_detail

    run_page = admin.get(f"/admin/runs/{run_id}").text
    assert "alice" in run_page
    assert "他本人绑定的图书馆账号" in run_page
    assert SENTINEL not in run_page and COOKIE_PREFIX not in run_page

    legacy_run_page = admin.get(f"/admin/runs/{legacy_run}").text
    assert "—（管理端账号）" in legacy_run_page
    assert SENTINEL not in legacy_run_page and COOKIE_PREFIX not in legacy_run_page


def test_owner_column_shows_the_credential_state_without_the_value(admin, web_db, bound_user):
    """失效状态要跟着归属一起显示（管理员才知道"他得重新登录"），但值永远没有。"""
    make_owned_plan(web_db, bound_user, name="alice 的计划")
    uc.mark_status(web_db, bound_user, uc.CRED_STATUS_INVALID, "凭据已过期", source="test")

    text = admin.get("/admin/plans").text
    assert "alice" in text
    assert uc.CRED_STATUS_LABELS[uc.CRED_STATUS_INVALID] in text
    assert SENTINEL not in text


def test_owner_view_carries_only_identity_and_state(admin, web_db, bound_user):
    """结构性保证：喂给模板的那个字典里**没有**任何凭据字段。

    这条比"页面上没出现哨兵"更强 —— 它钉住的是数据形状：即使将来模板改了，
    也没有可渲染的凭据字段可用。
    """
    from types import SimpleNamespace

    from haut_seat.web.routes import _plan_owner_view

    plan_id = make_owned_plan(web_db, bound_user)
    view = _plan_owner_view(SimpleNamespace(db=web_db))[plan_id]
    assert view["username"] == "alice" and view["user_id"] == bound_user
    assert view["cred_status"] == uc.CRED_STATUS_VALID
    joined = " ".join(str(v) for v in view.values())
    assert SENTINEL not in joined and COOKIE_PREFIX not in joined
    assert set(view) == {
        "user_id", "username", "display_name", "cred_status", "cred_label", "cred_tone",
    }
    assert not any("enc" in key or "token" in key or "cookie" in key for key in view)