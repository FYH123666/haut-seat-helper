"""Web 面板的 HTTP 层测试：登录、CSRF、计划增删改、运行记录、安全响应头。

全部离线：夹具注入的调度器/认证服务都是替身，查询返回「没有可预约座位」，
提交替身一旦被调用就抛错——因此这里不可能发出真实预约提交请求。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import pytest

from haut_seat.services.notification_service import NotifyEvent
from haut_seat.services.state import ErrorCategory, PlanStatus, RunPhase, RunStatus

GOOD_PASSWORD = "Correct-Horse-9"

_CSRF_RE = re.compile(r'name="_csrf" value="([^"]+)"')
_SETUP_RE = re.compile(r'name="_setup_token" value="([^"]+)"')


def tomorrow(days: int = 1) -> date:
    return date.today() + timedelta(days=days)


def plan_form(**overrides) -> dict:
    """一份必然通过服务端校验的表单（执行日期取明天，与运行时刻无关）。"""
    day = tomorrow()
    data = {
        "name": "工作日早八",
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
    data.update(overrides)
    return data


def wait_for(predicate, timeout: float = 5.0) -> bool:
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if predicate():
            return True
        _time.sleep(0.02)
    return False


def csrf_of(html: str) -> str:
    match = _CSRF_RE.search(html)
    assert match, "页面没有渲染 CSRF 隐藏字段"
    return match.group(1)


def make_admin(app, username="tester", password=GOOD_PASSWORD):
    from haut_seat.web import security

    return security.create_admin(app.state.db, username, password)


def login(client, username="tester", password=GOOD_PASSWORD):
    return client.post("/admin/login", data={"username": username, "password": password})


def create_plan(app, client, **overrides) -> int:
    """用真实表单流程创建计划，返回计划 ID（顺带验证创建路径可用）。"""
    page = client.get("/admin/plans/new")
    assert page.status_code == 200
    form = plan_form(**overrides)
    form["_csrf"] = csrf_of(page.text)
    resp = client.post("/admin/plans/new", data=form)
    assert resp.status_code == 303, resp.text
    location = resp.headers["location"]
    match = re.search(r"/admin/plans/(\d+)", location)
    assert match, location
    return int(match.group(1))


# ================================================================== 基本入口


def test_root_redirects_to_admin(make_client):
    _app, client = make_client()
    resp = client.get("/")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin"


def test_healthz_is_bare_and_needs_no_login(make_client):
    _app, client = make_client()
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_static_assets_are_served(make_client):
    _app, client = make_client()
    assert client.get("/static/app.css").status_code == 200


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_api_docs_are_not_exposed(make_client, path):
    """生产环境不暴露调试接口文档。"""
    _app, client = make_client()
    assert client.get(path).status_code == 404


# ================================================================== 访问控制


@pytest.mark.parametrize(
    "path",
    [
        "/admin",
        "/admin/plans",
        "/admin/plans/new",
        "/admin/runs",
        "/admin/auth",
        "/admin/notifications",
        "/admin/system",
    ],
)
def test_anonymous_get_redirects_to_login(make_client, path):
    _app, client = make_client()
    resp = client.get(path)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"


def test_anonymous_post_is_rejected_without_touching_state(make_client, web_db):
    from haut_seat.web import repository as repo

    _app, client = make_client()
    resp = client.post("/admin/system/toggle", data={"global_pause": "1"})
    assert resp.status_code == 401
    assert repo.get_app_setting(web_db)["global_pause"] == 1  # 默认值，未被改动


def test_logged_out_session_cannot_reach_admin(admin_client):
    app, client = admin_client
    page = client.get("/admin")
    assert page.status_code == 200
    token = csrf_of(page.text)
    resp = client.post("/admin/logout", data={"_csrf": token})
    assert resp.status_code == 303
    after = client.get("/admin")
    assert after.status_code == 303
    assert after.headers["location"] == "/admin/login"


def test_unknown_admin_page_returns_real_404(admin_client):
    _app, client = admin_client
    resp = client.get("/admin/does-not-exist")
    assert resp.status_code == 404
    assert "Traceback" not in resp.text


def test_bad_plan_id_type_is_rejected_not_crashed(admin_client):
    _app, client = admin_client
    assert client.get("/admin/plans/not-a-number").status_code == 422


def test_legacy_plan_runs_route_redirects(admin_client):
    _app, client = admin_client
    resp = client.get("/admin/plans/1/runs")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/runs?plan_id=1"


# ================================================================== 登录


def test_login_page_redirects_to_setup_when_no_admin(make_client):
    _app, client = make_client()
    resp = client.get("/admin/login")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/setup"


def test_wrong_password_does_not_start_a_session(make_client):
    from haut_seat.web import security

    app, client = make_client()
    make_admin(app)
    resp = login(client, password="wrong-password-1")
    assert resp.status_code == 303
    assert "ok=0" in resp.headers["location"]
    assert security.SESSION_COOKIE not in resp.cookies
    assert client.get("/admin").status_code == 303


def test_successful_login_sets_hardened_cookie(make_client):
    from haut_seat.web import security

    app, client = make_client()
    make_admin(app)
    resp = login(client)
    assert resp.status_code == 303
    raw = resp.headers.get("set-cookie", "")
    assert security.SESSION_COOKIE in raw
    assert "HttpOnly" in raw
    assert "SameSite=lax" in raw
    assert "Path=/" in raw
    assert client.get("/admin").status_code == 200


def test_unknown_user_and_wrong_password_give_the_same_message(make_client):
    """不能通过错误文案区分「用户不存在」和「口令错误」。"""
    app, client = make_client()
    make_admin(app)
    a = login(client, username="nobody")
    b = login(client, password="wrong-password-1")
    assert a.headers["location"] == b.headers["location"]


def test_login_is_rate_limited_over_http(make_client):
    from haut_seat.web import security

    app, client = make_client()
    make_admin(app)
    for _ in range(security.LOGIN_MAX_FAILURES):
        login(client, password="wrong-password-1")
    # 锁定期内即使口令正确也拒绝
    resp = login(client)
    assert resp.status_code == 303
    assert "ok=0" in resp.headers["location"]
    assert client.get("/admin").status_code == 303


def _recorded_login_ips(db) -> list[str]:
    return [row["ip"] for row in db.query_all("SELECT ip FROM login_attempt ORDER BY id")]


def test_forwarded_header_is_ignored_unless_the_proxy_is_trusted(make_client, web_db):
    """默认（不信任代理）时限流键来自连接本身，客户端提交的 X-Forwarded-For 无效。"""
    app, client = make_client()
    make_admin(app)
    client.post(
        "/admin/login",
        data={"username": "tester", "password": "wrong-password-1"},
        headers={"X-Forwarded-For": "9.9.9.9"},
    )
    login(client, password="wrong-password-1")
    ips = _recorded_login_ips(web_db)
    assert len(ips) == 2
    assert "9.9.9.9" not in ips
    assert len(set(ips)) == 1, ips


def test_trusted_proxy_uses_the_entry_the_proxy_appended(make_client, web_settings, web_db):
    """信任代理时只认代理追加的最右一项，客户端伪造的最左项必须被忽略。

    nginx 的 ``$proxy_add_x_forwarded_for`` 会把客户端自带的 XFF 原样接在前面、
    再把真实来源追加到最后，所以「最右项」才是可信来源。
    """
    settings = web_settings.model_copy(update={"web_trust_proxy": True})
    app, client = make_client(settings=settings)
    make_admin(app)
    client.post(
        "/admin/login",
        data={"username": "tester", "password": "wrong-password-1"},
        headers={"X-Forwarded-For": "9.9.9.9, 203.0.113.7"},
    )
    assert _recorded_login_ips(web_db) == ["203.0.113.7"]


def test_client_ip_header_parsing_edge_cases():
    """纯函数层面的取值规则：最右项、含空白、空值回退到连接地址。"""
    from types import SimpleNamespace

    from haut_seat.web.deps import client_ip

    def fake(forwarded: str, host: str = "127.0.0.1"):
        return SimpleNamespace(
            headers={"x-forwarded-for": forwarded} if forwarded is not None else {},
            client=SimpleNamespace(host=host),
        )

    assert client_ip(fake("9.9.9.9, 203.0.113.7"), trust_proxy=True) == "203.0.113.7"
    assert client_ip(fake(" 203.0.113.7 ,"), trust_proxy=True) == "203.0.113.7"
    assert client_ip(fake(""), trust_proxy=True) == "127.0.0.1"
    assert client_ip(fake(""), trust_proxy=False) == "127.0.0.1"
    assert client_ip(fake("9.9.9.9"), trust_proxy=False) == "127.0.0.1"


def test_login_page_hides_form_when_already_logged_in(admin_client):
    _app, client = admin_client
    resp = client.get("/admin/login")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin"


def test_login_rejects_credentials_hidden_in_query_string(make_client):
    """口令只能走 POST 表单，不能出现在 URL 里。"""
    app, client = make_client()
    make_admin(app)
    resp = client.get(f"/admin/login?password={GOOD_PASSWORD}")
    assert resp.status_code == 200
    assert GOOD_PASSWORD not in resp.text


# ================================================================== 首次初始化


def test_first_run_setup_creates_the_admin(make_client):
    from haut_seat.web import security

    app, client = make_client()
    page = client.get("/admin/setup")
    assert page.status_code == 200
    token = _SETUP_RE.search(page.text).group(1)

    resp = client.post(
        "/admin/setup",
        data={
            "_setup_token": token,
            "username": "owner",
            "password": GOOD_PASSWORD,
            "password_confirm": GOOD_PASSWORD,
        },
    )
    assert resp.status_code == 303
    assert security.count_admins(app.state.db) == 1
    assert client.get("/admin/setup").status_code == 303  # 已初始化后不再开放


def test_setup_token_cannot_be_reused(make_client):
    from haut_seat.web import security

    app, client = make_client()
    page = client.get("/admin/setup")
    token = _SETUP_RE.search(page.text).group(1)
    payload = {
        "_setup_token": token,
        "username": "owner",
        "password": GOOD_PASSWORD,
        "password_confirm": GOOD_PASSWORD,
    }
    assert client.post("/admin/setup", data=payload).status_code == 303
    # 第二次提交：初始化入口已关闭（令牌、口令都不再被接受）
    payload["username"] = "second"
    resp = client.post("/admin/setup", data=payload)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"
    assert security.count_admins(app.state.db) == 1


def test_setup_requires_a_valid_token(make_client):
    from haut_seat.web import security

    app, client = make_client()
    resp = client.post(
        "/admin/setup",
        data={
            "_setup_token": "forged",
            "username": "owner",
            "password": GOOD_PASSWORD,
            "password_confirm": GOOD_PASSWORD,
        },
    )
    assert "ok=0" in resp.headers["location"]
    assert security.count_admins(app.state.db) == 0


def test_setup_rejects_mismatched_confirmation(make_client):
    from haut_seat.web import security

    app, client = make_client()
    page = client.get("/admin/setup")
    token = _SETUP_RE.search(page.text).group(1)
    resp = client.post(
        "/admin/setup",
        data={
            "_setup_token": token,
            "username": "owner",
            "password": GOOD_PASSWORD,
            "password_confirm": "something-else-1",
        },
    )
    assert "ok=0" in resp.headers["location"]
    assert security.count_admins(app.state.db) == 0


def test_setup_can_be_disabled_for_server_deployments(make_client, web_settings):
    """服务器上应当用 CLI 建号，把网页初始化关掉。"""
    app, client = make_client(
        settings=web_settings.model_copy(update={"web_allow_first_run_setup": False})
    )
    resp = client.get("/admin/setup")
    assert resp.status_code == 403
    assert "create-admin" in resp.text
    assert client.post(
        "/admin/setup",
        data={"username": "owner", "password": GOOD_PASSWORD, "password_confirm": GOOD_PASSWORD},
    ).status_code == 403


# ================================================================== CSRF


def test_state_changing_post_without_csrf_is_rejected(admin_client):
    _app, client = admin_client
    pid = create_plan(_app, client)
    resp = client.post(f"/admin/plans/{pid}/pause")
    assert resp.status_code == 400
    assert client.get(f"/admin/plans/{pid}").text  # 页面仍可访问，未被改状态


def test_state_changing_post_with_wrong_csrf_is_rejected(admin_client):
    app, client = admin_client
    pid = create_plan(app, client)
    resp = client.post(f"/admin/plans/{pid}/pause", data={"_csrf": "forged-token-value"})
    assert resp.status_code == 400


def test_csrf_token_from_another_session_is_rejected(admin_client, make_client):
    app, client = admin_client
    pid = create_plan(app, client)

    # 第二个会话（同一数据库文件，另一个 app 实例）
    _other_app, other = make_client()
    assert login(other).status_code == 303
    other_token = csrf_of(other.get("/admin/plans/new").text)
    assert other_token != csrf_of(client.get("/admin/plans/new").text)

    resp = client.post(f"/admin/plans/{pid}/pause", data={"_csrf": other_token})
    assert resp.status_code == 400


def test_csrf_is_required_on_system_toggles(admin_client):
    _app, client = admin_client
    resp = client.post("/admin/system/toggle", data={"global_pause": "0"})
    assert resp.status_code == 400


# ================================================================== 计划管理


def test_created_plan_is_paused_and_unarmed(admin_client, web_db):
    """新建计划绝不自动武装：否则「打开服务」就等于「自动预约」。"""
    from haut_seat.web import repository as repo

    app, client = admin_client
    pid = create_plan(app, client)
    plan = repo.get_plan(web_db, pid)
    assert plan["status"] is PlanStatus.PAUSED
    assert plan["enabled"] == 0
    page = client.get(f"/admin/plans/{pid}").text
    assert "已暂停" in page or "暂停" in page


def test_plan_form_reports_all_errors_without_saving(admin_client, web_db):
    from haut_seat.web import repository as repo

    app, client = admin_client
    page = client.get("/admin/plans/new")
    form = plan_form(name="", regionid="abc", trigger_at="99:99")
    form["_csrf"] = csrf_of(page.text)
    resp = client.post("/admin/plans/new", data=form)
    assert resp.status_code == 200
    assert "计划名称不能为空" in resp.text
    assert "区域 ID 必须为正整数" in resp.text
    assert repo.list_plans(web_db) == []


def test_cross_day_window_is_rejected_server_side(admin_client, web_db):
    """客户端给什么不重要：跨天的时段必须被服务端挡下。"""
    from haut_seat.web import repository as repo

    app, client = admin_client
    day = tomorrow(2)
    page = client.get("/admin/plans/new")
    form = plan_form(
        run_date=day.strftime("%Y-%m-%d"),
        book_start=day.strftime("%Y-%m-%d") + " 22:00:00",
        book_end=day.strftime("%Y-%m-%d") + " 23:30:00",
    )
    form["_csrf"] = csrf_of(page.text)
    resp = client.post("/admin/plans/new", data=form)
    assert resp.status_code == 200
    assert "07:00" in resp.text or "21:30" in resp.text
    assert repo.list_plans(web_db) == []


def test_past_date_is_rejected(admin_client):
    app, client = admin_client
    page = client.get("/admin/plans/new")
    form = plan_form(run_date=(date.today() - timedelta(days=1)).strftime("%Y-%m-%d"))
    form["_csrf"] = csrf_of(page.text)
    resp = client.post("/admin/plans/new", data=form)
    assert resp.status_code == 200
    assert "不能早于今天" in resp.text


def test_plan_edit_updates_fields(admin_client, web_db):
    from haut_seat.web import repository as repo

    app, client = admin_client
    pid = create_plan(app, client)

    page = client.get(f"/admin/plans/{pid}/edit")
    assert page.status_code == 200
    form = plan_form(name="改名后的计划", max_attempts_per_seat="5")
    form["_csrf"] = csrf_of(page.text)
    assert client.post(f"/admin/plans/{pid}/edit", data=form).status_code == 303

    plan = repo.get_plan(web_db, pid)
    assert plan["name"] == "改名后的计划"
    assert plan["max_attempts_per_seat"] == 5
    assert plan["status"] is PlanStatus.PAUSED, "编辑不应改变武装状态"


def test_plan_actions_require_csrf_and_follow_the_state_machine(admin_client, web_db):
    from haut_seat.web import repository as repo

    app, client = admin_client
    pid = create_plan(app, client)
    page = client.get(f"/admin/plans/{pid}")
    token = csrf_of(page.text)

    assert client.post(f"/admin/plans/{pid}/resume", data={"_csrf": token}).status_code == 303
    assert repo.get_plan(web_db, pid)["status"] is PlanStatus.PENDING

    assert client.post(f"/admin/plans/{pid}/pause", data={"_csrf": token}).status_code == 303
    assert repo.get_plan(web_db, pid)["status"] is PlanStatus.PAUSED

    assert client.post(f"/admin/plans/{pid}/resume", data={"_csrf": token}).status_code == 303
    assert client.post(f"/admin/plans/{pid}/stop", data={"_csrf": token}).status_code == 303
    assert repo.get_plan(web_db, pid)["status"] is PlanStatus.STOPPED


def test_deleting_a_plan_removes_its_runs(admin_client, web_db):
    from haut_seat.web import repository as repo

    app, client = admin_client
    pid = create_plan(app, client)
    run_id = repo.start_run(web_db, pid, trigger="manual")
    repo.finish_run(
        web_db, run_id, status=RunStatus.FAILED, category=ErrorCategory.NO_SEAT,
        stop_reason="window_closed", message="", exit_code=0,
    )
    page = client.get(f"/admin/plans/{pid}")
    resp = client.post(f"/admin/plans/{pid}/delete", data={"_csrf": csrf_of(page.text)})
    assert resp.status_code == 303
    assert repo.get_plan(web_db, pid) is None
    assert repo.get_run(web_db, run_id) is None


def test_cannot_delete_a_running_plan(admin_client, web_db):
    from haut_seat.web import repository as repo

    app, client = admin_client
    pid = create_plan(app, client)
    repo.start_run(web_db, pid, trigger="manual")
    page = client.get(f"/admin/plans/{pid}")
    resp = client.post(f"/admin/plans/{pid}/delete", data={"_csrf": csrf_of(page.text)})
    assert repo.get_plan(web_db, pid) is not None
    assert "ok=0" in resp.headers["location"]


def test_unknown_plan_cannot_be_resumed_without_human_acknowledgement(admin_client, web_db):
    """结果未知的计划必须人工核实后才能恢复，网页上不能一键放行。"""
    from haut_seat.web import repository as repo

    app, client = admin_client
    pid = create_plan(app, client)
    run_id = repo.start_run(web_db, pid, trigger="manual")
    repo.finish_run(
        web_db, run_id, status=RunStatus.UNKNOWN, category=ErrorCategory.SUBMIT_UNKNOWN,
        stop_reason="submit_unknown", message="提交后未收到应答", exit_code=0,
        plan_status=PlanStatus.UNKNOWN,
    )

    page = client.get(f"/admin/plans/{pid}")
    assert "结果未知" in page.text
    assert "不是" in page.text  # 明确说明「不是普通失败」
    token = csrf_of(page.text)

    refused = client.post(f"/admin/plans/{pid}/resume", data={"_csrf": token})
    assert "ok=0" in refused.headers["location"]
    assert repo.get_plan(web_db, pid)["status"] is PlanStatus.UNKNOWN

    accepted = client.post(
        f"/admin/plans/{pid}/resume",
        data={"_csrf": token, "acknowledge_unknown": "1"},
    )
    assert accepted.status_code == 303
    assert repo.get_plan(web_db, pid)["status"] is PlanStatus.PENDING


def test_unknown_plan_is_not_listed_as_failed_on_dashboard(admin_client, web_db):
    from haut_seat.web import repository as repo

    app, client = admin_client
    pid = create_plan(app, client)
    run_id = repo.start_run(web_db, pid, trigger="manual")
    repo.finish_run(
        web_db, run_id, status=RunStatus.UNKNOWN, category=ErrorCategory.SUBMIT_UNKNOWN,
        stop_reason="submit_unknown", message="", exit_code=0, plan_status=PlanStatus.UNKNOWN,
    )
    text = client.get("/admin").text
    assert "结果未知" in text
    stats = repo.dashboard_stats(web_db)
    assert stats["runs_unknown"] == 1
    assert stats["runs_failed"] == 0


# ================================================================== 运行记录


def _seed_run(db, plan_id, **overrides) -> int:
    from haut_seat.web import repository as repo

    run_id = repo.start_run(db, plan_id, trigger=overrides.pop("trigger", "manual"))
    defaults = {
        "status": RunStatus.FAILED,
        "category": ErrorCategory.NO_SEAT,
        "stop_reason": "window_closed",
        "message": "窗口内没有可预约座位",
        "exit_code": 0,
    }
    defaults.update(overrides)
    repo.finish_run(db, run_id, **defaults)
    return run_id


def test_run_detail_shows_phases_and_category(admin_client, web_db):
    from haut_seat.web import repository as repo

    app, client = admin_client
    pid = create_plan(app, client)
    run_id = _seed_run(web_db, pid)
    repo.add_run_event(web_db, run_id, phase=RunPhase.QUERY, level="info",
                       message="候选座位 3 个")
    repo.add_run_event(web_db, run_id, phase=RunPhase.END, level="info",
                       message="本轮结束")
    page = client.get(f"/admin/runs/{run_id}")
    assert page.status_code == 200
    assert "候选座位" in page.text
    assert "本轮结束" in page.text


def test_run_detail_explains_unknown_and_never_replays(admin_client, web_db):
    from haut_seat.web import repository as repo

    app, client = admin_client
    pid = create_plan(app, client)
    run_id = _seed_run(
        web_db, pid, status=RunStatus.UNKNOWN, category=ErrorCategory.SUBMIT_UNKNOWN,
        stop_reason="submit_unknown", message="提交后未收到应答",
    )
    page = client.get(f"/admin/runs/{run_id}")
    assert page.status_code == 200
    assert "结果未知" in page.text
    assert "不会" in page.text  # 明确说明「不会自动重放」
    assert repo.get_run(web_db, run_id)["status"] is RunStatus.UNKNOWN


def test_interrupted_run_is_explained_too(admin_client, web_db):
    from haut_seat.web import repository as repo

    app, client = admin_client
    pid = create_plan(app, client)
    run_id = _seed_run(
        web_db, pid, status=RunStatus.INTERRUPTED, category=ErrorCategory.SUBMIT_UNKNOWN,
        stop_reason="interrupted", message="服务重启时该运行仍在进行",
    )
    page = client.get(f"/admin/runs/{run_id}")
    assert page.status_code == 200
    assert "结果未知" in page.text


def test_runs_can_be_filtered_by_plan(admin_client, web_db):
    app, client = admin_client
    pid_a = create_plan(app, client, name="甲")
    pid_b = create_plan(app, client, name="乙")
    _seed_run(web_db, pid_a)

    page = client.get(f"/admin/runs?plan_id={pid_a}")
    assert page.status_code == 200
    assert "#1" in page.text or "甲" in page.text
    filtered = client.get(f"/admin/runs?plan_id={pid_b}")
    assert "没有" in filtered.text or "暂无" in filtered.text


def test_garbage_plan_filter_does_not_crash(admin_client):
    _app, client = admin_client
    assert client.get("/admin/runs?plan_id=abc").status_code == 200


def test_unknown_run_id_returns_404(admin_client):
    _app, client = admin_client
    assert client.get("/admin/runs/999999").status_code == 404


def test_logs_are_redacted_in_the_html(admin_client, web_db):
    """凭证绝不能出现在页面里（日志落库前已脱敏）。"""
    from haut_seat.web import repository as repo

    app, client = admin_client
    pid = create_plan(app, client)
    run_id = repo.start_run(web_db, pid, trigger="manual")
    repo.add_run_event(
        web_db, run_id, phase=RunPhase.AUTH_CHECK, level="error",
        message="precheck failed: Authorization: Bearer supersecrettokenvalue",
        category=ErrorCategory.AUTH_INVALID,
    )
    repo.add_run_event(
        web_db, run_id, phase=RunPhase.QUERY, level="info",
        message="cookie: " + "S" * 80,
    )
    text = client.get(f"/admin/runs/{run_id}").text
    assert "supersecrettokenvalue" not in text
    assert "S" * 80 not in text
    assert "<redacted>" in text or "已脱敏" in text or "***" in text


def test_session_token_never_appears_in_html(admin_client):
    app, client = admin_client
    cookie = client.cookies.get("hs_session")
    assert cookie
    for path in ("/admin", "/admin/plans", "/admin/system", "/admin/auth"):
        assert cookie not in client.get(path).text


# ================================================================== 手动执行
#
# 「立即执行」要先过一串前置判断（今天是否执行 / 窗口是否已过 / 触发时刻 /
# 认证 / 全局暂停 / 真实提交开关），这些都依赖「现在」。
# 因此这里把调度器时钟固定为「今天 10:00」，计划窗口取 10:30–10:31，
# 测试结论就与机器真实运行时刻无关。


class FrozenClock:
    """可推进的假时钟：让主循环的等待不需要真实 sleep。"""

    def __init__(self, now: float):
        self.now = now

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def runnable_setup(
    make_client,
    web_settings,
    offline,
    *,
    enable_real_booking=True,
    auth_status="valid",
    query_fn=None,
):
    """搭一个「现在可以立即执行」的环境，返回 (app, client, db, plan_id)。"""
    from haut_seat.web.db import Database

    fixed = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    settings = web_settings.model_copy(
        update={"enable_real_booking": enable_real_booking}
    )
    db = Database(settings.web_db)
    db.init_schema()
    auth = offline.auth_service(db, settings, status=auth_status)
    app, client = make_client(
        settings=settings,
        db=db,
        scheduler=offline.scheduler(
            db,
            settings,
            clock=lambda: fixed,
            time_mod=FrozenClock(fixed.timestamp()),
            auth_service=auth,
            **({"query_fn": query_fn} if query_fn else {}),
        ),
        auth_service=auth,
    )
    make_admin(app)
    assert login(client).status_code == 303

    day = fixed.strftime("%Y-%m-%d")
    pid = create_plan(
        app, client,
        run_date=day,
        trigger_at="09:00:00",
        book_start=day + " 10:30:00",
        book_end=day + " 10:31:00",
    )
    return app, client, db, pid


def post_run(client, pid):
    page = client.get(f"/admin/plans/{pid}")
    return client.post(f"/admin/plans/{pid}/run", data={"_csrf": csrf_of(page.text)})


def test_manual_run_refuses_while_global_pause_is_on(make_client, web_settings, offline):
    from urllib.parse import unquote

    from haut_seat.web import repository as repo

    app, client, db, pid = runnable_setup(make_client, web_settings, offline)
    resp = post_run(client, pid)
    assert resp.status_code == 303
    assert "ok=0" in resp.headers["location"]
    assert "全局暂停" in unquote(resp.headers["location"])
    assert repo.list_runs(db) == [], "全局暂停时不得启动任何运行"


def test_manual_run_refuses_when_real_booking_is_disabled(
    make_client, web_settings, offline
):
    from urllib.parse import unquote

    from haut_seat.web import repository as repo

    app, client, db, pid = runnable_setup(
        make_client, web_settings, offline, enable_real_booking=False
    )
    repo.set_app_setting(db, global_pause=False)
    resp = post_run(client, pid)
    assert "ok=0" in resp.headers["location"]
    assert "未放行" in unquote(resp.headers["location"])
    assert "HAUT_ENABLE_REAL_BOOKING" in unquote(resp.headers["location"])
    assert repo.list_runs(db) == []


def test_manual_run_refuses_when_auth_is_invalid(make_client, web_settings, offline):
    """认证失效时不得继续轮询座位，也不得提交。"""
    from urllib.parse import unquote

    from haut_seat.web import repository as repo

    app, client, db, pid = runnable_setup(
        make_client, web_settings, offline, auth_status="invalid"
    )
    repo.set_app_setting(db, global_pause=False)
    # 界面上的「检测登录状态」等价于一次预检：结果会落库，调度器据此拦截
    repo.set_auth_state(db, status="invalid", message="凭据已失效，请重新登录")
    resp = post_run(client, pid)
    assert "ok=0" in resp.headers["location"]
    assert "认证" in unquote(resp.headers["location"])
    assert repo.list_runs(db) == [], "认证失效时不得轮询座位或提交"


def test_manual_run_executes_in_background_and_records_the_run(
    make_client, web_settings, offline
):
    """放行后的「立即执行」必须走后台线程，并留下运行记录。

    查询替身返回空布局 → 无候选座位 → 全程不会调用提交替身。
    """
    import time as _time

    from haut_seat.web import repository as repo

    app, client, db, pid = runnable_setup(make_client, web_settings, offline)
    repo.set_app_setting(db, global_pause=False)
    repo.resume_plan(db, pid)

    resp = post_run(client, pid)
    assert resp.status_code == 303
    assert "ok=0" not in resp.headers["location"], resp.headers["location"]

    deadline = _time.monotonic() + 10.0
    while _time.monotonic() < deadline:
        runs = repo.list_runs(db, plan_id=pid)
        if runs and runs[0]["status"] is not RunStatus.RUNNING:
            break
        _time.sleep(0.02)

    runs = repo.list_runs(db, plan_id=pid)
    assert len(runs) == 1, "「立即执行」没有留下运行记录"
    assert runs[0]["trigger"] == "manual"
    assert runs[0]["submit_count"] == 0, "测试环境绝不能调用真实提交接口"
    assert runs[0]["status"] is not RunStatus.SUCCEEDED
    # 后台线程确实跑过：留下了阶段事件
    phases = {e["phase"] for e in repo.list_run_events(db, runs[0]["id"])}
    assert {"start", "query", "end"} <= phases


def test_manual_run_refuses_when_the_plan_is_not_due_today(make_client, web_settings, offline):
    """计划不是今天执行 → 按钮拒绝，且不留下运行记录。"""
    from urllib.parse import unquote

    from haut_seat.web import repository as repo

    app, client, db, pid = runnable_setup(make_client, web_settings, offline)
    repo.set_app_setting(db, global_pause=False)
    repo.resume_plan(db, pid)
    # 把计划日期改成明天（直接改库，避免走校验）
    with db.tx() as conn:
        conn.execute("UPDATE plan SET run_date = ? WHERE id = ?", (str(tomorrow()), pid))

    resp = post_run(client, pid)
    assert "ok=0" in resp.headers["location"]
    assert "不执行" in unquote(resp.headers["location"])
    assert repo.list_runs(db, plan_id=pid) == []


def test_repeated_manual_run_clicks_do_not_start_two_runs(
    make_client, web_settings, offline
):
    """连点「立即执行」不能产生同一计划的并发运行。"""
    import threading

    from haut_seat.models.seat import LayoutQueryResult
    from haut_seat.web import repository as repo

    gate = threading.Event()

    def blocking_query(*args, **kwargs):
        gate.wait(timeout=5.0)  # 让第一次运行「停在查询阶段」，模拟长时间运行
        return LayoutQueryResult.from_response({"layout": None, "seatList": []})

    app, client, db, pid = runnable_setup(
        make_client, web_settings, offline, query_fn=blocking_query
    )
    repo.set_app_setting(db, global_pause=False)
    repo.resume_plan(db, pid)

    try:
        first = post_run(client, pid)
        assert "ok=0" not in first.headers["location"], first.headers["location"]
        assert wait_for(lambda: len(repo.list_runs(db, plan_id=pid)) == 1), "运行未创建"

        second = post_run(client, pid)
        assert "ok=0" in second.headers["location"], "第二次点击应当被拒绝"
    finally:
        gate.set()
    assert wait_for(lambda: not repo.list_running_runs(db))
    assert len(repo.list_runs(db, plan_id=pid)) == 1, "并发保护失效：出现了两次运行"


# ================================================================== 认证状态页


def test_auth_page_shows_three_state_framework(admin_client):
    _app, client = admin_client
    page = client.get("/admin/auth")
    assert page.status_code == 200
    assert "无法确认" in page.text or "未确认" in page.text
    assert "自动续期" in page.text
    assert "不支持" in page.text or "未实现" in page.text


def test_manual_auth_check_updates_state(admin_client, web_db):
    from haut_seat.web import repository as repo

    app, client = admin_client
    token = csrf_of(client.get("/admin/auth").text)
    resp = client.post("/admin/auth/check", data={"_csrf": token})
    assert resp.status_code == 303
    assert repo.get_auth_state(web_db)["status"] == "valid"


def test_recovery_flow_requires_human_and_shows_steps(admin_client, web_db):
    from haut_seat.web import repository as repo

    app, client = admin_client
    token = csrf_of(client.get("/admin/auth").text)
    resp = client.post("/admin/auth/recover", data={"_csrf": token})
    assert resp.status_code == 303
    state = repo.get_auth_state(web_db)
    assert state["recovery_state"] == "awaiting_human"

    page = client.get("/admin/auth")
    assert "等待人工完成登录" in page.text
    assert "F12" in page.text
    assert "HAUT_AUTHORIZATION" in page.text


def test_recovery_verification_failure_is_reported_and_keeps_blocking(
    admin_client, web_db, web_settings, offline
):
    """恢复校验失败 → 状态为「恢复失败」，仍然拦截提交。"""
    from haut_seat.services.auth_service import RecoveryState
    from haut_seat.web import repository as repo

    app, client = admin_client
    # 换成「预检必失败」的认证服务
    app.state.auth_service = offline.auth_service(
        app.state.db, web_settings, status="invalid"
    )
    token = csrf_of(client.get("/admin/auth").text)
    client.post("/admin/auth/recover", data={"_csrf": token})
    resp = client.post("/admin/auth/verify", data={"_csrf": token})
    assert "ok=0" in resp.headers["location"]
    state = repo.get_auth_state(web_db)
    assert state["status"] == "invalid"
    assert state["recovery_state"] == RecoveryState.FAILED.value
    assert app.state.auth_service.blocks_submissions()


def test_recovery_verification_success_clears_the_block(admin_client, web_db):
    from haut_seat.web import repository as repo

    app, client = admin_client
    token = csrf_of(client.get("/admin/auth").text)
    client.post("/admin/auth/recover", data={"_csrf": token})
    resp = client.post("/admin/auth/verify", data={"_csrf": token})
    assert "ok=1" in resp.headers["location"]
    assert repo.get_auth_state(web_db)["recovery_state"] == "recovered"
    assert not app.state.auth_service.blocks_submissions()


def test_auth_events_are_listed(admin_client, web_db):
    from haut_seat.web import repository as repo

    _app, client = admin_client
    repo.set_auth_state(web_db, status="invalid", message="凭据已失效，请重新登录")
    page = client.get("/admin/auth")
    assert "凭据已失效" in page.text


# ================================================================== 通知设置


def test_notifications_page_says_email_is_not_implemented(admin_client):
    _app, client = admin_client
    page = client.get("/admin/notifications")
    assert page.status_code == 200
    assert "尚未实现" in page.text
    assert "预留" in page.text


def test_notification_settings_save_but_never_store_a_password(admin_client, web_db):
    from haut_seat.web import repository as repo

    app, client = admin_client
    token = csrf_of(client.get("/admin/notifications").text)
    data = {
        "_csrf": token,
        "enabled": "1",
        "channel": "email",
        "smtp_host": "smtp.example.com",
        "smtp_port": "465",
        "smtp_user": "me@example.com",
        "smtp_password_env": "HAUT_SMTP_PASSWORD",
        "mail_from": "me@example.com",
        "mail_to": "me@example.com",
        "use_tls": "1",
        "flag_booking_success": "1",
        # 伪造一个口令字段：绝不能被持久化
        "smtp_password": "SuperSecretMailPassword",
    }
    resp = client.post("/admin/notifications", data=data)
    assert resp.status_code == 303

    setting = repo.get_notify_setting(web_db)
    assert setting["smtp_host"] == "smtp.example.com"
    assert setting["smtp_password_env"] == "HAUT_SMTP_PASSWORD"
    assert setting["event_flags"][NotifyEvent.BOOKING_SUCCESS.value] is True
    assert "SuperSecretMailPassword" not in str(setting)


def test_notification_save_requires_csrf(admin_client):
    _app, client = admin_client
    assert client.post("/admin/notifications", data={"enabled": "1"}).status_code == 400


def test_notification_flags_default_to_off_for_no_seat(admin_client, web_db):
    from haut_seat.web import repository as repo

    app, client = admin_client
    repo.set_notify_setting(app.state.db, {"event_flags": {}})
    page = client.get("/admin/notifications")
    assert page.status_code == 200
    # 界面上仍要能看到全部预留事件
    for event in NotifyEvent:
        assert f"flag_{event.value}" in page.text


# ================================================================== 系统状态


def test_system_page_shows_scheduler_and_settings(admin_client):
    _app, client = admin_client
    page = client.get("/admin/system")
    assert page.status_code == 200
    assert "全局暂停" in page.text
    assert "调度" in page.text


def test_global_pause_toggle_changes_state(admin_client, web_db):
    from haut_seat.web import repository as repo

    app, client = admin_client
    token = csrf_of(client.get("/admin/system").text)
    assert repo.get_app_setting(web_db)["global_pause"] == 1

    resp = client.post("/admin/system/toggle", data={"_csrf": token, "global_pause": "0"})
    assert resp.status_code == 303
    assert repo.get_app_setting(web_db)["global_pause"] == 0

    resp = client.post("/admin/system/toggle", data={"_csrf": token, "global_pause": "1"})
    assert repo.get_app_setting(web_db)["global_pause"] == 1


def test_scheduler_switch_toggles_independently(admin_client, web_db):
    from haut_seat.web import repository as repo

    app, client = admin_client
    token = csrf_of(client.get("/admin/system").text)
    client.post("/admin/system/toggle", data={"_csrf": token, "scheduler_enabled": "0"})
    setting = repo.get_app_setting(web_db)
    assert setting["scheduler_enabled"] == 0
    assert setting["global_pause"] == 1, "两个开关必须互相独立"


def test_maintenance_only_clears_housekeeping_records(admin_client, web_db):
    from datetime import datetime as dt

    from haut_seat.web import repository as repo

    app, client = admin_client
    pid = create_plan(app, client)
    run_id = _seed_run(web_db, pid)
    token = csrf_of(client.get("/admin/system").text)
    resp = client.post("/admin/system/maintenance", data={"_csrf": token})
    assert resp.status_code == 303
    # 近期记录必须保留
    assert repo.get_run(web_db, run_id) is not None
    assert repo.get_plan(web_db, pid) is not None


def test_system_page_never_prints_the_session_token(admin_client):
    app, client = admin_client
    cookie = client.cookies.get("hs_session")
    assert cookie and cookie not in client.get("/admin/system").text


# ================================================================== 响应头与页面基线


def test_security_headers_are_present(admin_client):
    _app, client = admin_client
    resp = client.get("/admin")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]
    assert "default-src 'self'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["Referrer-Policy"] == "same-origin"


def test_admin_pages_are_not_cacheable(admin_client):
    _app, client = admin_client
    assert client.get("/admin").headers["Cache-Control"] == "no-store"


def test_security_headers_apply_to_login_too(make_client):
    _app, client = make_client()
    resp = client.get("/admin/login")
    assert resp.headers["X-Frame-Options"] == "DENY"


def test_pages_are_mobile_ready(admin_client):
    _app, client = admin_client
    page = client.get("/admin").text
    assert 'name="viewport"' in page
    assert "width=device-width" in page


def test_pages_do_not_use_localstorage_for_credentials(admin_client):
    _app, client = admin_client
    page = client.get("/admin").text
    assert "localStorage" not in page
    assert "sessionStorage" not in page


def test_scripts_are_served_locally_and_referenced_by_the_page(admin_client):
    _app, client = admin_client
    page = client.get("/admin").text
    assert "/static/app.js" in page


def test_plan_form_never_echoes_a_cookie_or_token(admin_client):
    app, client = admin_client
    cookie = client.cookies.get("hs_session")
    page = client.get("/admin/plans/new").text
    assert cookie not in page
    assert "Authorization" not in page


def test_session_lock_is_not_rendered_as_a_form_value(admin_client, web_db):
    """CSRF 令牌只出现在表单隐藏字段里，且与 Cookie 值不同。"""
    app, client = admin_client
    cookie = client.cookies.get("hs_session")
    page = client.get("/admin/plans/new").text
    token = csrf_of(page)
    assert token != cookie
    assert f'name="_csrf" value="{token}"' in page


def test_admin_shell_marks_real_booking_switch_state(make_client):
    """真实提交开关关闭时，页面必须显式提醒（避免误以为会自动抢座）。"""
    app, client = make_client()
    make_admin(app)
    assert login(client).status_code == 303
    page = client.get("/admin").text
    assert "HAUT_ENABLE_REAL_BOOKING" in page
    assert "false" in page


def test_flash_message_does_not_break_out_of_html(admin_client):
    """提示语来自查询串，必须被转义。"""
    _app, client = admin_client
    resp = client.get('/admin?msg=<script>alert(1)</script>&ok=0')
    assert resp.status_code == 200
    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


def test_plan_name_is_escaped_on_the_detail_page(admin_client):
    app, client = admin_client
    pid = create_plan(app, client, name='<img src=x onerror=alert(1)>')
    page = client.get(f"/admin/plans/{pid}").text
    assert "<img src=x onerror=alert(1)>" not in page
    assert "&lt;img" in page


def test_dashboard_lists_recent_runs_without_credentials(admin_client, web_db):
    app, client = admin_client
    pid = create_plan(app, client, name="看板上的计划")
    _seed_run(web_db, pid)
    page = client.get("/admin").text
    assert "看板上的计划" in page
    assert "没有可预约座位" in client.get("/admin/runs").text


def test_notify_history_shows_skipped_status(admin_client, web_db):
    from haut_seat.services.notification_service import NotificationService

    app, client = admin_client
    service = NotificationService(app.state.db)
    service.notify_booking_success(plan_id=1, run_id=1, detail="预约成功")
    page = client.get("/admin/notifications").text
    assert "skipped" in page or "未发送" in page or "已跳过" in page


def test_all_admin_pages_render_for_a_logged_in_admin(admin_client):
    _app, client = admin_client
    for path in (
        "/admin",
        "/admin/advanced",
        "/admin/plans",
        "/admin/plans/new",
        "/admin/runs",
        "/admin/auth",
        "/admin/notifications",
        "/admin/system",
    ):
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} -> {resp.status_code}"


def test_never_trusts_client_side_regionid(admin_client, web_db):
    """区域 ID 必须被服务端重新校验，不能只靠前端下拉框。"""
    from haut_seat.web import repository as repo

    app, client = admin_client
    page = client.get("/admin/plans/new")
    form = plan_form(regionid="0")
    form["_csrf"] = csrf_of(page.text)
    assert client.post("/admin/plans/new", data=form).status_code == 200
    assert repo.list_plans(web_db) == []


def test_duplicate_plan_names_are_allowed_but_ids_are_distinct(admin_client, web_db):
    from haut_seat.web import repository as repo

    app, client = admin_client
    a = create_plan(app, client, name="同名")
    b = create_plan(app, client, name="同名")
    assert a != b
    assert len(repo.list_plans(web_db)) == 2


# ------------------------------------------------ 简化表单：模式 + 三个时刻（路由层）
#
# 用户只需回答四件事：多久一次 / 什么时候开始抢 / 座位用哪个时段 / 坐哪儿。
# 这一组守住「表单提交 → 落库字段」这条链，并确认界面不再要求手写技术字符串。


def simple_plan_form(**overrides) -> dict:
    """新表单的样子：mode + 三个时刻，没有任何技术字段。"""
    data = {
        "name": "早八",
        "mode": "once",
        "run_date": tomorrow().strftime("%Y-%m-%d"),
        "trigger_at": "06:55",
        "use_start": "08:00",
        "use_end": "12:00",
        "regionid": "16",
        "preferred_seats": "十层东书库北区 01桌 B座",
        "preferred_prefixes": "",
        "confirmed_bookable_states": '[["2", 1, false]]',
        "max_attempts_per_seat": "3",
        "note": "",
    }
    data.update(overrides)
    return data


def post_simple(client, **overrides) -> tuple[int, str]:
    """从真实页面取 CSRF 后提交简化表单，返回 (状态码, 响应文本)。"""
    page = client.get("/admin/plans/new")
    form = simple_plan_form(**overrides)
    form["_csrf"] = csrf_of(page.text)
    resp = client.post("/admin/plans/new", data=form)
    return resp.status_code, resp.text


def test_the_form_offers_time_pickers_instead_of_raw_timestamps(admin_client):
    """三个时刻必须是时刻控件，用户不再需要手写 YYYY-MM-DD HH:MM:SS。"""
    _app, client = admin_client
    page = client.get("/admin/plans/new").text
    for field in ("trigger_at", "use_start", "use_end"):
        assert f'name="{field}" type="time"' in page, field
    for technical in ("book_start", "book_end"):
        assert f'name="{technical}"' not in page, technical
    # 三个时间的含义必须分开写清（系统开始执行 / 座位使用开始 / 座位使用结束）
    assert "系统开始执行预约操作的时间" in page
    assert "座位使用开始时间" in page
    assert "座位使用结束时间" in page


def test_the_single_booking_date_defaults_to_tomorrow(admin_client):
    _app, client = admin_client
    page = client.get("/admin/plans/new").text
    assert 'name="run_date" type="date"' in page
    assert f'value="{tomorrow().strftime("%Y-%m-%d")}"' in page


def test_the_advanced_fields_are_collapsed_but_present(admin_client):
    """专业字段保留原值，只是默认折叠 —— 不隐藏、不静默改写。"""
    _app, client = admin_client
    page = client.get("/admin/plans/new").text
    assert "<details" in page
    for field in ("preferred_prefixes", "confirmed_bookable_states",
                  "max_attempts_per_seat", "note", "regionid_manual"):
        assert f'name="{field}"' in page, field
    # 留空状态组合 = 零候选 = 永远约不到，所以必须预填一个非空默认值
    assert '[["2", 1, false]]' in page


def test_the_weekdays_checkbox_is_offered_for_daily_plans(admin_client):
    _app, client = admin_client
    page = client.get("/admin/plans/new").text
    assert 'name="weekdays_only"' in page
    assert "仅工作日" in page
    assert "每天预约" in page and "单次预约" in page


def test_a_single_plan_is_stored_with_a_composed_timestamp(admin_client, web_db):
    from haut_seat.web import repository as repo

    _app, client = admin_client
    day = tomorrow().strftime("%Y-%m-%d")
    status, _text = post_simple(client, mode="once", run_date=day)
    assert status == 303
    plan = repo.list_plans(web_db)[0]
    assert plan["repeat_rule"] == "once"
    assert plan["run_date"] == day
    assert plan["trigger_at"] == "06:55:00"
    assert plan["book_start"] == f"{day} 08:00:00"
    assert plan["book_end"] == f"{day} 12:00:00"


def test_a_daily_plan_needs_no_date_and_takes_today(admin_client, web_db):
    from haut_seat.web import repository as repo

    _app, client = admin_client
    status, _text = post_simple(client, mode="daily", run_date="")
    assert status == 303
    plan = repo.list_plans(web_db)[0]
    today = date.today().strftime("%Y-%m-%d")
    assert plan["repeat_rule"] == "daily"
    assert plan["run_date"] == today
    assert plan["book_start"] == f"{today} 08:00:00"


def test_the_weekdays_only_checkbox_lands_on_the_weekdays_rule(admin_client, web_db):
    from haut_seat.web import repository as repo

    _app, client = admin_client
    status, _text = post_simple(client, mode="daily", run_date="", weekdays_only="1")
    assert status == 303
    assert repo.list_plans(web_db)[0]["repeat_rule"] == "weekdays"


def test_a_single_plan_whose_window_already_ended_is_refused(admin_client, web_db, monkeypatch):
    """今天 + 时段已过 ⇒ 建出来就不会执行，必须拦下并指向明天。"""
    from haut_seat.web import repository as repo
    from haut_seat.web import routes as web_routes

    _app, client = admin_client
    fixed = datetime.now().replace(hour=15, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(web_routes, "_now", lambda: fixed)
    today = date.today().strftime("%Y-%m-%d")

    status, text = post_simple(client, mode="once", run_date=today,
                               trigger_at="07:00", use_start="08:00", use_end="12:00")
    assert status == 200
    assert "已经过去" in text and "明天" in text
    assert repo.list_plans(web_db) == []


def test_a_single_plan_for_todays_remaining_window_is_accepted(admin_client, web_db, monkeypatch):
    """触发时刻已过但窗口未关是合法用法，只给提示、不阻断。"""
    from haut_seat.web import repository as repo
    from haut_seat.web import routes as web_routes

    _app, client = admin_client
    fixed = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(web_routes, "_now", lambda: fixed)
    today = date.today().strftime("%Y-%m-%d")

    status, _text = post_simple(client, mode="once", run_date=today,
                                trigger_at="06:55", use_start="11:00", use_end="12:00")
    assert status == 303
    plan = repo.list_plans(web_db)[0]
    assert plan["book_start"] == f"{today} 11:00:00"
    assert plan["book_end"] == f"{today} 12:00:00"


def test_a_daily_plan_is_accepted_even_when_todays_window_has_passed(
    admin_client, web_db, monkeypatch
):
    """重复计划的窗口每天滚动，今天过了明天还会跑，不能套用必死判据。"""
    from haut_seat.web import repository as repo
    from haut_seat.web import routes as web_routes

    _app, client = admin_client
    fixed = datetime.now().replace(hour=15, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(web_routes, "_now", lambda: fixed)

    status, _text = post_simple(client, mode="daily", run_date="",
                                trigger_at="07:00", use_start="08:00", use_end="12:00")
    assert status == 303
    assert repo.list_plans(web_db)[0]["repeat_rule"] == "daily"


def test_the_region_can_be_given_by_hand_for_a_region_not_in_the_catalog(admin_client, web_db):
    """目录里还没有的区域：手工填 ID 必须能用，否则第一次登记就没路可走。"""
    from haut_seat.web import repository as repo

    _app, client = admin_client
    status, _text = post_simple(client, regionid="", regionid_manual="38")
    assert status == 303
    assert repo.list_plans(web_db)[0]["regionid"] == 38


def test_two_disagreeing_regions_are_refused_instead_of_silently_picked(admin_client, web_db):
    from haut_seat.web import repository as repo

    _app, client = admin_client
    status, text = post_simple(client, regionid="16", regionid_manual="38")
    assert status == 200
    assert "不一致" in text
    assert repo.list_plans(web_db) == []


def test_editing_a_weekdays_plan_preselects_the_right_mode(admin_client, web_db):
    """编辑既有计划时模式与时刻必须回填：反向映射不一致会悄悄改掉计划。"""
    app, client = admin_client
    pid = create_plan(app, client, name="工作日")
    with web_db.tx() as conn:
        conn.execute("UPDATE plan SET repeat_rule = 'weekdays' WHERE id = ?", (pid,))

    page = client.get(f"/admin/plans/{pid}/edit").text
    assert 'value="daily" data-mode-radio' in page
    assert 'name="weekdays_only" id="weekdays_only"' in page
    assert "checked" in page
    # 时刻控件也要回填成本次计划的时刻（默认是 07:00 / 08:00 / 12:00）
    assert 'value="07:00"' in page
    assert 'value="08:00"' in page
    assert 'value="12:00"' in page


def test_editing_keeps_the_plan_mode_and_times(admin_client, web_db):
    from haut_seat.web import repository as repo

    app, client = admin_client
    pid = create_plan(app, client, name="改前")
    page = client.get(f"/admin/plans/{pid}/edit")
    form = simple_plan_form(name="改后", mode="daily", run_date="", weekdays_only="1",
                            trigger_at="07:30", use_start="09:00", use_end="11:00")
    form["_csrf"] = csrf_of(page.text)
    assert client.post(f"/admin/plans/{pid}/edit", data=form).status_code == 303

    plan = repo.get_plan(web_db, pid)
    today = date.today().strftime("%Y-%m-%d")
    assert plan["name"] == "改后"
    assert plan["repeat_rule"] == "weekdays"
    assert plan["trigger_at"] == "07:30:00"
    assert plan["book_start"] == f"{today} 09:00:00"
    assert plan["book_end"] == f"{today} 11:00:00"


# --------------------------------------------------- 主页（我的预约）+ 高级页 + 导航
#
# 主页不再是「管理后台概览」，而是普通用户打开就想看到的那三件事：
# 我有哪些预约、下次什么时候跑、上一次结果如何。运维界面收进「高级」页，
# 但**一个都没有删除**，仍然逐个可直达。


def test_the_home_page_renders_with_no_data_at_all(admin_client):
    """四个区块各自的空态都必须能渲染 —— 新装的面板就是这种状态。"""
    _app, client = admin_client
    resp = client.get("/admin")
    assert resp.status_code == 200
    page = resp.text
    assert "我的预约" in page
    assert "还没有任何预约" in page
    assert "新建第一个预约" in page
    assert "还没有执行过" in page
    # 全局暂停默认是「开」（安全默认），主页必须说清计划不会动手
    assert "全局暂停当前是「开」" in page


def test_the_home_page_says_when_the_scheduler_is_off(admin_client, web_db):
    """调度总开关关掉时，等「待执行」就是在等一个不会发生的事：必须在主页说明。"""
    from haut_seat.web import repository as repo

    _app, client = admin_client
    repo.set_app_setting(web_db, scheduler_enabled=False)
    page = client.get("/admin").text
    assert "后台调度总开关当前是「关」" in page


def test_the_next_execution_is_written_in_plain_words(admin_client, web_db, monkeypatch):
    """每天预约的「下次执行」必须是「明天 07:00」这种人话，不是裸时间戳。"""
    from haut_seat.web import repository as repo
    from haut_seat.web import routes as web_routes

    _app, client = admin_client
    status, _text = post_simple(client, mode="daily", run_date="",
                                trigger_at="07:00", use_start="08:00", use_end="12:00")
    assert status == 303
    pid = repo.list_plans(web_db)[0]["id"]

    # 固定到当天 15:00：今天的 08:00–12:00 已经过去 ⇒ 只可能说明天
    monkeypatch.setattr(
        web_routes, "_now",
        lambda: datetime.now().replace(hour=15, minute=0, second=0, microsecond=0),
    )
    page = client.get("/admin").text
    # 新建的预约默认是「已暂停」：时间点归时间点，程序现在不会动，两件事都要说
    assert "已暂停；启用后 明天 07:00" in page
    assert "08:00–12:00" in page
    assert "1 个指定座位" in page


def test_enabling_a_booking_switches_the_same_sentence_to_a_promise(
    admin_client, web_db, monkeypatch
):
    """启用后同一句「明天 07:00」不再带「已暂停」前缀 —— 状态与时间必须同源。"""
    from haut_seat.web import repository as repo
    from haut_seat.web import routes as web_routes

    _app, client = admin_client
    assert post_simple(client, mode="daily", run_date="",
                       trigger_at="07:00", use_start="08:00", use_end="12:00")[0] == 303
    pid = repo.list_plans(web_db)[0]["id"]
    monkeypatch.setattr(
        web_routes, "_now",
        lambda: datetime.now().replace(hour=15, minute=0, second=0, microsecond=0),
    )
    page = client.get("/admin")
    resp = client.post(f"/admin/plans/{pid}/resume", data={"_csrf": csrf_of(page.text)})
    assert resp.status_code == 303

    after = client.get("/admin").text
    assert "明天 07:00" in after
    assert "已暂停；启用后" not in after


def test_the_home_page_shows_the_last_execution_result(admin_client, web_db):
    """「最后一次执行结果」是主页的第二块内容：结果、错误分类、说明与详情入口。"""
    app, client = admin_client
    pid = create_plan(app, client, name="主页上的计划")
    run_id = _seed_run(web_db, pid)

    page = client.get("/admin").text
    assert "最后一次执行结果" in page
    assert "主页上的计划" in page
    assert "无可预约座位" in page              # 错误分类徽标
    assert "没有可预约座位" in page            # 运行记录里的说明
    assert f'href="/admin/runs/{run_id}"' in page


def test_the_home_page_has_only_three_top_level_links(admin_client):
    """顶级导航只剩三项：日常操作之外的门，全部收进「高级」。"""
    _app, client = admin_client
    page = client.get("/admin").text
    nav = page.split('id="navmain"', 1)[1].split("</ul>", 1)[0]
    assert nav.count('class="nav-link') == 3, nav
    for href in ("/admin", "/admin/catalog", "/admin/advanced"):
        assert f'href="{href}"' in nav, href
    for href in ("/admin/plans", "/admin/runs", "/admin/auth",
                 "/admin/notifications", "/admin/system"):
        assert f'href="{href}"' not in nav, href


def test_the_advanced_page_reaches_every_ops_page(admin_client):
    """收进高级页的每个界面都必须还在、可直达 —— 收敛导航不等于砍功能。"""
    _app, client = admin_client
    resp = client.get("/admin/advanced")
    assert resp.status_code == 200
    page = resp.text
    for href in ("/admin/plans", "/admin/runs", "/admin/auth",
                 "/admin/notifications", "/admin/system"):
        assert f'href="{href}"' in page, href
        assert client.get(href).status_code == 200, href
    # 把话说清楚：这些是运维界面，且它们都动不了真实预约开关
    assert "工程" in page
    assert ".env" in page


def test_no_admin_page_offers_the_real_booking_switch(admin_client):
    """真实预约开关只能改服务器上的 .env：任何页面都不能给出「打开它」的入口。"""
    _app, client = admin_client
    for path in ("/admin", "/admin/advanced", "/admin/system", "/admin/catalog"):
        page = client.get(path).text
        assert 'name="enable_real_booking"' not in page, path
        assert 'name="HAUT_ENABLE_REAL_BOOKING"' not in page, path


def test_the_detail_page_leads_with_plain_language(admin_client, web_db, monkeypatch):
    """主页「详情」的落点：先说什么时候执行，原始字段折叠在后面但一个不少。"""
    import html as html_module

    from haut_seat.web import repository as repo
    from haut_seat.web import routes as web_routes

    _app, client = admin_client
    assert post_simple(client, mode="daily", run_date="",
                       trigger_at="07:00", use_start="08:00", use_end="12:00")[0] == 303
    pid = repo.list_plans(web_db)[0]["id"]
    monkeypatch.setattr(
        web_routes, "_now",
        lambda: datetime.now().replace(hour=15, minute=0, second=0, microsecond=0),
    )

    page = client.get(f"/admin/plans/{pid}").text
    assert "这个预约什么时候执行" in page
    assert "已暂停；启用后 明天 07:00" in page
    assert "08:00–12:00" in page
    # 原始参数仍然如实展示（默认折叠），一条都没有被隐藏或改写
    assert "<details" in page
    for label in ("触发时刻", "预约时段", "可预约状态组合", "单座位尝试次数"):
        assert label in page, label
    assert "07:00:00" in page                      # trigger_at 原值
    assert "08:00:00" in page and "12:00:00" in page
    # 状态组合用 JSON 的写法，不是 Python 的 repr
    unescaped = html_module.unescape(page)
    assert '[["2", 1, false]]' in unescaped
    assert "[['2', 1, False]]" not in unescaped
    # 内部代码（daily / weekdays / once）不再印给用户看
    assert "规则 weekdays" not in page and "规则 daily" not in page


def test_a_run_result_is_written_in_chinese_not_as_an_enum_repr(admin_client, web_db):
    """``RunStatus`` 是 ``str`` 混入的枚举，``status is string`` 对它同样为真。

    模板一旦按「是字符串就原样打印」处理，页面上就会出现 ``RunStatus.UNKNOWN``
    这种内部名字 —— 普通用户看不懂，中文标签反而丢了。
    """
    app, client = admin_client
    pid = create_plan(app, client, name="结果徽标")
    run_id = _seed_run(
        web_db, pid, status=RunStatus.UNKNOWN, category=ErrorCategory.SUBMIT_UNKNOWN
    )
    for path in ("/admin", "/admin/runs", f"/admin/runs/{run_id}", f"/admin/plans/{pid}"):
        page = client.get(path).text
        assert "RunStatus." not in page, path
        assert "PlanStatus." not in page, path
        assert "提交结果未知" in page, path
    # 旧库里可能存着认不出的状态字符串：那种情况仍要原样显示，不能报错
    with web_db.tx() as conn:
        conn.execute("UPDATE run SET status = 'legacy_oddity' WHERE id = ?", (run_id,))
    page = client.get("/admin/runs").text
    assert "legacy_oddity" in page