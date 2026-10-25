"""用户端（``/booking``）测试。

全部离线：夹具注入的调度器 / 认证服务 / 目录服务都是替身，查询返回空布局，
提交替身一旦被调用就抛错 —— 这里不可能发出任何真实预约提交请求。

用户端**有自己的账号体系**：所有页面与写入口都要求先登录，且每个用户只看得到
自己的计划。夹具因此先建一个用户端账号并走**真实登录表单**登录；
"未登录会怎样"另有专门用例（见 ``test_booking_pages_require_login``）。

覆盖需求点名的几条：
- 页面可访问、路径不与既有路由冲突；
- 合法创建进入**同一套**后端数据流（同一张 plan 表 + 同一套校验 + 同一套状态机），
  因此 ``/admin`` 立刻能看到；
- 非法日期 / 时段 / 区域 / 座位参数被**服务端**拒绝（不是只靠前端）；
- 编辑、删除规则服从任务状态限制（运行中 / 结果未知 / 今天已提交过都拦得住）；
- 用户端碰不到认证凭据与全局管理配置；
- 页面与响应里不出现任何凭据。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from urllib.parse import unquote

import pytest

from haut_seat.services.state import ErrorCategory, PlanStatus, RunStatus
from tests.user_end import USERNAME, USER_PASSWORD, create_user, login_user, page_csrf

#: TestClient 的 Host 是 ``testserver``，同源校验因此必须用这个 Origin。
ORIGIN = "http://testserver"

_CSRF_RE = re.compile(r'name="_csrf" value="([^"]+)"')
COOKIE = "haut_booking_csrf"


def tomorrow(days: int = 1) -> date:
    return date.today() + timedelta(days=days)


def plan_form(**overrides) -> dict:
    """一份必然通过服务端校验的用户端表单（执行日期取明天，与运行时刻无关）。"""
    day = tomorrow()
    data = {
        "name": "用户端预约",
        "mode": "once",
        "run_date": day.strftime("%Y-%m-%d"),
        "trigger_at": "07:00",
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


def seed_csrf(client) -> str:
    """先 GET 一次页面拿到双提交令牌（同时也验证了 GET 会种 Cookie）。"""
    client.get("/booking")
    token = client.cookies.get(COOKIE)
    assert token, "用户端页面没有种下 CSRF Cookie"
    return token


def bpost(client, path, data=None, *, token=None, origin=ORIGIN):
    """用户端 POST：默认带上正确的同源 Origin 与双提交令牌。"""
    headers = {"Origin": origin} if origin else {}
    body = dict(data or {})
    if token is not None:
        body["_csrf"] = token
    return client.post(path, data=body, headers=headers)


def make_plan(client, db, **overrides) -> int:
    """走真实表单流程创建一个用户端计划，返回计划 ID。"""
    from haut_seat.web import repository as repo

    token = seed_csrf(client)
    resp = bpost(client, "/booking/new", plan_form(**overrides), token=token)
    assert resp.status_code == 303, resp.text
    match = re.search(r"/booking/(\d+)", resp.headers["location"])
    assert match, resp.headers["location"]
    pid = int(match.group(1))
    assert repo.get_plan(db, pid) is not None, "创建路径没有真的落库"
    return pid


@pytest.fixture
def booking(make_client, web_settings, web_db):
    """``(client, db, settings)``：一个**已登录**的用户端客户端。

    账号直接建在库里，但登录走真实表单（见 ``tests/user_end.login_user``）——
    于是每个用例都顺带证明了"登录态确实能让页面与写入口工作"。
    """
    _app, client = make_client(settings=web_settings, db=web_db)
    create_user(web_db)
    login_user(client, web_db)
    return client, web_db, web_settings


# ================================================================== 0. 必须先登录


def test_booking_pages_require_login(make_client, web_settings, web_db):
    """未登录：页面一律 303 到登录页，写入口一律 401 —— 没有"匿名可用"的余地。"""
    _app, anon = make_client(settings=web_settings, db=web_db)
    for path in ("/booking", "/booking/new", "/booking/bind", "/booking/1",
                 "/booking/catalog"):
        resp = anon.get(path)
        assert resp.status_code == 303, path
        assert resp.headers["location"] == "/booking/login", path

    # 表单 POST 不跟着跳转（303 会让"提交成功"和"没登录"长得一样），直接 401。
    # 这里带上**真**令牌与同源 Origin，确保拦住它的确实是"没登录"这一条。
    token = page_csrf(anon, "/booking/login")
    for path in ("/booking/new", "/booking/bind", "/booking/catalog/picks",
                 "/booking/catalog/picks/clear"):
        resp = bpost(anon, path, {"_csrf": token}, token=None)
        assert resp.status_code == 401, path
    assert anon.get("/booking/999999").headers["location"] == "/booking/login"


def test_login_page_does_not_leak_any_plan(booking, make_client, web_settings, web_db):
    """登录页只是表单：既不夹带既有计划的名字，也不夹带计划 ID。

    用**另一个**客户端去取（``booking`` 那个已经登录了，再取登录页会直接跳走），
    这样看到的正是"还没登录的陌生人"看到的东西。
    """
    from haut_seat.web import repository as repo

    client, db, _settings = booking
    pid = make_plan(client, db, name="不该出现在登录页的计划")

    _app, stranger = make_client(settings=web_settings, db=web_db)
    body = stranger.get("/booking/login").text
    assert "不该出现在登录页的计划" not in body
    assert f"/booking/{pid}" not in body
    assert repo.get_plan(db, pid) is not None


# ================================================================== 1. 页面可访问


def test_booking_page_is_accessible(booking):
    client, _db, _settings = booking
    resp = client.get("/booking")
    assert resp.status_code == 200
    assert "我的预约" in resp.text
    # 用户端里**没有**管理端的任何入口。
    assert "/admin" not in resp.text


def test_booking_page_seeds_a_scoped_csrf_cookie(make_client, web_settings, web_db):
    """任何用户端页面都会种下双提交令牌，且作用域限定在 ``/booking``。

    用干净客户端取**登录页**：那是唯一匿名可达的用户端页面，也是"还在登录前"
    就已经需要 CSRF 保护的那个页面。
    """
    _app, anon = make_client(settings=web_settings, db=web_db)
    resp = anon.get("/booking/login")
    assert resp.status_code == 200
    raw = resp.headers.get("set-cookie", "")
    assert COOKIE in raw
    # 作用域限定在 /booking：管理端页面一次都拿不到这个 Cookie。
    assert "Path=/booking" in raw
    # HttpOnly：脚本读不到它，也就没法把令牌交给别的源。
    assert "httponly" in raw.lower()


def test_booking_new_form_renders(booking):
    client, _db, settings = booking
    resp = client.get("/booking/new")
    assert resp.status_code == 200
    assert f'value="{settings.regionid}"' in resp.text, "区域默认值没有带上"


def test_booking_root_does_not_shadow_existing_routes(booking):
    """用户端**不能**顶掉既有路由：``/`` 仍然 303 到 ``/admin``，``/healthz`` 仍是 200。"""
    client, _db, _settings = booking
    root = client.get("/")
    assert root.status_code == 303
    assert root.headers["location"] == "/admin"
    assert client.get("/healthz").json() == {"status": "ok"}


def test_booking_can_be_switched_off(make_client, web_settings, web_db):
    """``HAUT_WEB_ENABLE_USER_END=false`` 时整个用户端 404（最小必要的开关）。

    关掉之后**连登录页也不存在**：不是"没登录所以跳转"，而是这里没有这个功能。
    """
    off = web_settings.model_copy(update={"web_enable_user_end": False})
    _app, client = make_client(settings=off, db=web_db)
    for path in ("/booking", "/booking/new", "/booking/login", "/booking/register",
                 "/booking/catalog"):
        assert client.get(path).status_code == 404, path
    assert bpost(client, "/booking/new", plan_form()).status_code == 404
    assert bpost(client, "/booking/catalog/picks").status_code == 404


# ================================================================== 2. 创建


def test_valid_plan_creation_enters_the_same_data_flow(booking):
    """合法创建：进同一张 plan 表、标为「用户端」、且默认「已暂停」。"""
    from haut_seat.web import repository as repo

    client, db, _settings = booking
    pid = make_plan(client, db)
    plan = repo.get_plan(db, pid)
    assert plan["name"] == "用户端预约"
    assert plan["status"] is PlanStatus.PAUSED, "新建必须是不武装状态"
    assert plan["enabled"] == 0
    assert plan["regionid"] == 16
    assert plan["preferred_seats"] == ["十层东书库北区 01桌 B座"]
    assert repo.get_plan_origin(db, pid) == repo.PLAN_ORIGIN_USER


def test_user_plan_is_visible_in_admin_with_its_source(
    booking, make_client, web_settings, web_db
):
    """用户端建的计划必须在既有管理端里看得见，并且**标注来源为「用户端」**。"""
    client, db, _settings = booking
    pid = make_plan(client, db, name="管理端可见的用户预约")

    _app, admin = make_client(settings=web_settings, db=web_db)
    from haut_seat.web import security

    security.create_admin(db, "tester", "Correct-Horse-9")
    assert admin.post(
        "/admin/login", data={"username": "tester", "password": "Correct-Horse-9"}
    ).status_code == 303

    # 主页、计划列表、计划详情三处都能看到它，且都标明来源。
    for path in ("/admin", "/admin/plans", f"/admin/plans/{pid}"):
        page = admin.get(path)
        assert page.status_code == 200, path
        assert "管理端可见的用户预约" in page.text, f"{path} 看不到用户端建的计划"

    assert "用户端" in admin.get("/admin/plans").text
    detail = admin.get(f"/admin/plans/{pid}").text
    assert "用户端" in detail
    # 管理端要能看清参数与状态（需求点名的字段）。
    assert "16" in detail and "08:00" in detail and "12:00" in detail
    assert "已暂停" in detail


def test_admin_created_plan_is_labelled_admin(booking, make_client, web_settings, web_db):
    """来源判定不能只说一半：管理端建的（老库里没有来源行的）必须显示「管理端」。"""
    client, db, _settings = booking
    make_plan(client, db, name="用户端的一条")

    _app, admin = make_client(settings=web_settings, db=web_db)
    from haut_seat.web import security

    security.create_admin(db, "tester", "Correct-Horse-9")
    admin.post("/admin/login", data={"username": "tester", "password": "Correct-Horse-9"})
    page = admin.get("/admin/plans/new")
    form = plan_form(name="管理端的一条")
    form.pop("mode")          # 用管理端的原始技术字段表单
    form.pop("use_start")
    form.pop("use_end")
    day = tomorrow().strftime("%Y-%m-%d")
    form["repeat_rule"] = "once"
    form["book_start"] = day + " 08:00:00"
    form["book_end"] = day + " 12:00:00"
    form["_csrf"] = _CSRF_RE.search(page.text).group(1)
    assert admin.post("/admin/plans/new", data=form).status_code == 303

    listing = admin.get("/admin/plans").text
    assert "管理端" in listing
    assert "用户端" in listing


# ================================================================== 3. 服务端校验


def test_invalid_date_is_rejected_server_side(booking):
    from haut_seat.web import repository as repo

    client, db, _settings = booking
    token = seed_csrf(client)
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    resp = bpost(client, "/booking/new", plan_form(run_date=yesterday), token=token)
    assert resp.status_code == 200, "校验失败必须原样回显表单，而不是 303 假装成功"
    assert "不能早于今天" in resp.text
    assert repo.list_plans(db) == [], "被拒绝的表单绝不能落库"


def test_invalid_time_range_is_rejected_server_side(booking):
    from haut_seat.web import repository as repo

    client, db, _settings = booking
    token = seed_csrf(client)
    # 开始晚于结束
    resp = bpost(client, "/booking/new",
                 plan_form(use_start="15:00", use_end="09:00"), token=token)
    assert resp.status_code == 200
    assert "请修正以下问题" in resp.text
    assert repo.list_plans(db) == []


def test_out_of_window_time_is_rejected_server_side(booking):
    """07:00–21:30 之外的使用时段由服务端挡下（前端控件管不住直接构造的请求）。"""
    from haut_seat.web import repository as repo

    client, db, _settings = booking
    token = seed_csrf(client)
    resp = bpost(client, "/booking/new",
                 plan_form(use_start="05:00", use_end="06:00"), token=token)
    assert resp.status_code == 200
    assert repo.list_plans(db) == []


def test_invalid_region_is_rejected_server_side(booking):
    from haut_seat.web import repository as repo

    client, db, _settings = booking
    token = seed_csrf(client)
    resp = bpost(client, "/booking/new", plan_form(regionid="abc"), token=token)
    assert resp.status_code == 200
    assert "区域 ID 必须为正整数" in resp.text

    # 0 与负数同样拒绝 —— 区域 ID 填错的后果是整天盯着另一个区域抢座位。
    for bad in ("0", "-3"):
        resp = bpost(client, "/booking/new", plan_form(regionid=bad), token=token)
        assert resp.status_code == 200, bad
    assert repo.list_plans(db) == []


def test_blank_name_is_rejected_server_side(booking):
    from haut_seat.web import repository as repo

    client, db, _settings = booking
    token = seed_csrf(client)
    resp = bpost(client, "/booking/new", plan_form(name="   "), token=token)
    assert resp.status_code == 200
    assert "计划名称不能为空" in resp.text
    assert repo.list_plans(db) == []


def test_seat_parameters_are_bounded_server_side(booking):
    """座位参数由服务端设上限（前端 maxlength 只是便利，挡不住直接构造的请求）。"""
    from haut_seat.web import repository as repo
    from haut_seat.web.validation import MAX_ATTEMPTS, MAX_SEATS

    client, db, _settings = booking
    token = seed_csrf(client)

    resp = bpost(client, "/booking/new",
                 plan_form(max_attempts_per_seat=str(MAX_ATTEMPTS + 1)), token=token)
    assert resp.status_code == 200
    assert "最大尝试次数" in resp.text

    pid = make_plan(client, db, preferred_seats="\n".join(
        f"A桌 {i}座" for i in range(MAX_SEATS + 30)
    ))
    assert len(repo.get_plan(db, pid)["preferred_seats"]) == MAX_SEATS


# ================================================================== 4. 提交守卫


def test_post_without_same_origin_is_refused(booking):
    """缺少 Origin/Referer 的 POST 一律 403：跨站表单提交带的一定是对方的 Origin。"""
    client, db, _settings = booking
    token = seed_csrf(client)
    resp = bpost(client, "/booking/new", plan_form(), token=token, origin=None)
    assert resp.status_code == 403

    resp = bpost(client, "/booking/new", plan_form(), token=token,
                 origin="http://evil.example")
    assert resp.status_code == 403
    from haut_seat.web import repository as repo

    assert repo.list_plans(db) == []


def test_post_without_csrf_token_is_refused(booking):
    client, db, _settings = booking
    seed_csrf(client)
    resp = bpost(client, "/booking/new", plan_form(), token=None)
    assert resp.status_code == 400

    resp = bpost(client, "/booking/new", plan_form(), token="not-the-cookie-value")
    assert resp.status_code == 400
    from haut_seat.web import repository as repo

    assert repo.list_plans(db) == []


def test_admin_session_token_does_not_work_on_the_user_end(
    booking, make_client, web_settings, web_db
):
    """两个令牌互相独立：管理员的会话令牌**不能**用来提交用户端表单。"""
    from haut_seat.web import repository as repo
    from haut_seat.web import security

    client, db, _settings = booking
    client.get("/booking")  # 种下用户端自己的令牌
    security.create_admin(db, "tester", "Correct-Horse-9")

    _app, admin = make_client(settings=web_settings, db=web_db)
    # 登录页本身没有 CSRF 字段（登录前还没有会话），因此这里只提交口令。
    assert admin.post(
        "/admin/login", data={"username": "tester", "password": "Correct-Horse-9"}
    ).status_code == 303
    admin_token = admin.cookies.get("hs_session")
    assert admin_token, "管理员登录没有种下会话 Cookie"

    resp = bpost(client, "/booking/new", plan_form(), token=admin_token)
    assert resp.status_code == 400, "管理端令牌不得通过用户端校验"
    assert repo.list_plans(db) == []


# ================================================================== 5. 编辑 / 删除规则


def test_edit_updates_the_same_plan(booking):
    from haut_seat.web import repository as repo

    client, db, _settings = booking
    pid = make_plan(client, db)
    token = seed_csrf(client)
    resp = bpost(client, f"/booking/{pid}/edit",
                 plan_form(name="改过的名字", preferred_seats=""), token=token)
    assert resp.status_code == 303
    plan = repo.get_plan(db, pid)
    assert plan["name"] == "改过的名字"
    assert plan["preferred_seats"] == []
    # 编辑**不改变执行状态**：仍然是暂停。
    assert plan["status"] is PlanStatus.PAUSED
    # 来源也不因为编辑而丢失。
    assert repo.get_plan_origin(db, pid) == repo.PLAN_ORIGIN_USER


def test_edit_is_refused_while_the_plan_is_running(booking):
    from haut_seat.web import repository as repo

    client, db, _settings = booking
    pid = make_plan(client, db)
    repo.start_run(db, pid)  # 把计划置为「运行中」并留下一条 running 记录

    page = client.get(f"/booking/{pid}/edit")
    assert page.status_code == 303, "运行中的计划不能进编辑页"
    assert "正在执行中" in unquote(page.headers["location"])

    token = seed_csrf(client)
    resp = bpost(client, f"/booking/{pid}/edit", plan_form(name="偷偷改掉"), token=token)
    assert resp.status_code == 303
    assert "正在执行中" in unquote(resp.headers["location"])
    assert repo.get_plan(db, pid)["name"] == "用户端预约", "运行中不得被改写"


def test_edit_and_delete_are_refused_for_unknown_result(booking):
    """结果未知是重复预约保护的核心证据：用户端不能改也不能删。"""
    from haut_seat.web import repository as repo

    client, db, _settings = booking
    pid = make_plan(client, db)
    run_id = repo.start_run(db, pid)
    repo.finish_run(
        db, run_id, status=RunStatus.UNKNOWN, category=ErrorCategory.SUBMIT_UNKNOWN,
        stop_reason="unknown", message="提交后无法确认", exit_code=1,
        submit_count=1, plan_status=PlanStatus.UNKNOWN,
    )

    page = client.get(f"/booking/{pid}/edit")
    assert page.status_code == 303
    assert "结果未知" in unquote(page.headers["location"])

    token = seed_csrf(client)
    resp = bpost(client, f"/booking/{pid}/delete", token=token)
    assert resp.status_code == 303
    assert "结果未知" in unquote(resp.headers["location"])
    assert repo.get_plan(db, pid) is not None, "结果未知的计划不得被删除"
    assert len(repo.list_runs(db, plan_id=pid)) == 1, "运行记录必须留着（它是证据）"


def test_delete_is_refused_after_a_submission_today(booking):
    """今天已经发起过提交（哪怕失败了）就不能删：删计划会级联删掉那条「今天不得再提交」的依据。"""
    from haut_seat.web import repository as repo

    client, db, _settings = booking
    pid = make_plan(client, db)
    run_id = repo.start_run(db, pid)
    repo.finish_run(
        db, run_id, status=RunStatus.FAILED, category=ErrorCategory.BUSINESS_ERROR,
        stop_reason="failed", message="服务器返回失败", exit_code=1,
        submit_count=1, plan_status=PlanStatus.FAILED,
    )

    token = seed_csrf(client)
    resp = bpost(client, f"/booking/{pid}/delete", token=token)
    assert resp.status_code == 303
    assert "不能在这里删除" in unquote(resp.headers["location"])
    assert repo.get_plan(db, pid) is not None
    assert len(repo.list_runs(db, plan_id=pid)) == 1


def test_delete_is_allowed_when_nothing_was_ever_submitted(booking):
    from haut_seat.web import repository as repo

    client, db, _settings = booking
    pid = make_plan(client, db)
    token = seed_csrf(client)
    resp = bpost(client, f"/booking/{pid}/delete", token=token)
    assert resp.status_code == 303
    assert repo.get_plan(db, pid) is None
    assert repo.list_plans(db) == []


def test_unknown_plan_cannot_be_resumed_without_acknowledgement(booking):
    """结果未知的恢复必须带「已核实」勾选 —— 由既有状态机唯一判定。"""
    from haut_seat.web import repository as repo

    client, db, _settings = booking
    pid = make_plan(client, db)
    run_id = repo.start_run(db, pid)
    repo.finish_run(
        db, run_id, status=RunStatus.UNKNOWN, category=ErrorCategory.SUBMIT_UNKNOWN,
        stop_reason="unknown", message="提交后无法确认", exit_code=1,
        submit_count=1, plan_status=PlanStatus.UNKNOWN,
    )

    token = seed_csrf(client)
    resp = bpost(client, f"/booking/{pid}/enable", token=token)
    assert resp.status_code == 303
    assert "结果未知" in unquote(resp.headers["location"])
    assert repo.get_plan(db, pid)["status"] is PlanStatus.UNKNOWN, "没核实就不能恢复"

    resp = bpost(client, f"/booking/{pid}/enable",
                 {"acknowledge_unknown": "1"}, token=token)
    assert resp.status_code == 303
    assert repo.get_plan(db, pid)["status"] is PlanStatus.PENDING


def test_pause_then_enable_round_trip(booking):
    from haut_seat.web import repository as repo

    client, db, _settings = booking
    pid = make_plan(client, db)
    token = seed_csrf(client)
    assert bpost(client, f"/booking/{pid}/enable", token=token).status_code == 303
    assert repo.get_plan(db, pid)["status"] is PlanStatus.PENDING
    assert bpost(client, f"/booking/{pid}/pause", token=token).status_code == 303
    assert repo.get_plan(db, pid)["status"] is PlanStatus.PAUSED


# ================================================================== 6. 用户端碰不到的东西


def test_user_end_has_no_write_route_beyond_its_own_plans(booking):
    """用户端的写入口**就是这几个**，且没有一个能碰别人的计划或全局状态。

    认证凭据、全局暂停、调度总开关、真实预约开关、"立即执行"这些管理端能力
    在用户端根本没有对应路由 —— 不是"隐藏了按钮"，而是路由不存在。
    新增的 ``login/register/logout/bind/unbind/verify`` 全部只作用于
    **当前登录用户自己**的账号与会话。
    """
    from haut_seat.web.routes import router

    writes = {
        (route.path, method)
        for route in router.routes
        for method in getattr(route, "methods", set()) or set()
        if route.path.startswith("/booking") and method != "GET"
    }
    assert writes == {
        # 账号本身
        ("/booking/login", "POST"),
        ("/booking/register", "POST"),
        ("/booking/logout", "POST"),
        # 学校凭据（只写当前用户自己的那一份）
        ("/booking/bind", "POST"),
        ("/booking/unbind", "POST"),
        ("/booking/verify", "POST"),
        # 自己的计划
        ("/booking/new", "POST"),
        ("/booking/{plan_id}/edit", "POST"),
        ("/booking/{plan_id}/pause", "POST"),
        ("/booking/{plan_id}/enable", "POST"),
        ("/booking/{plan_id}/delete", "POST"),
        # 座位目录里**自己**的待选草稿（user_seat_pick，每人一份）。
        # 这四个只写草稿表，而草稿只是"填表助手"：它不改变任何计划的参数，
        # 也不会被调度器读到（调度器只读 plan 表）。刷目录 / 加区域 / 导入 /
        # 收藏这些目录的写能力**不在**这张表里 —— 用户端没有对应路由。
        ("/booking/catalog/picks", "POST"),
        ("/booking/catalog/picks/remove", "POST"),
        ("/booking/catalog/picks/move", "POST"),
        ("/booking/catalog/picks/clear", "POST"),
    }


def test_user_end_cannot_touch_auth_or_global_settings(booking):
    """用户端请求既读不到认证页，也改不动全局开关（都必须先登录）。"""
    from haut_seat.web import repository as repo

    client, db, _settings = booking
    token = seed_csrf(client)
    before = repo.get_app_setting(db)

    # 管理端的认证页 / 系统页：未登录一律 303 到登录页。
    assert client.get("/admin/auth").status_code == 303
    assert client.get("/admin/system").status_code == 303

    # 用户端的令牌 + 用户端的 Origin 也换不来管理端的状态变更。
    for path in ("/admin/system/toggle", "/admin/auth/reload", "/admin/auth/login-browser"):
        resp = bpost(client, path, {"_csrf": token}, token=token)
        assert resp.status_code in (303, 401), (path, resp.status_code)

    after = repo.get_app_setting(db)
    assert (after["global_pause"], after["scheduler_enabled"]) == (
        before["global_pause"], before["scheduler_enabled"]
    ), "用户端不得改动全局暂停 / 调度总开关"


def test_user_end_cannot_bypass_the_real_booking_switch(booking):
    """用户端只能"建计划"：真正的提交仍受既有闸门（真实预约开关 + 全局暂停）约束。"""
    from haut_seat.web import repository as repo

    client, db, settings = booking
    assert settings.enable_real_booking is False
    pid = make_plan(client, db)
    token = seed_csrf(client)
    bpost(client, f"/booking/{pid}/enable", token=token)
    # 即使已启用，只要闸门没开就不可能产生提交 —— 用户端没有任何捷径。
    assert repo.list_runs(db, plan_id=pid) == []
    assert repo.get_app_setting(db)["global_pause"] == 1


def test_user_end_cannot_rewrite_run_records(booking):
    """用户端没有改运行记录的入口：删除 / 编辑 / 暂停 / 启用都动不了已有记录。"""
    from haut_seat.web import repository as repo

    client, db, _settings = booking
    pid = make_plan(client, db)
    run_id = repo.start_run(db, pid)
    repo.finish_run(
        db, run_id, status=RunStatus.SUCCEEDED, category=ErrorCategory.NONE,
        stop_reason="success", message="预约成功", exit_code=0, booking_id=4242,
        submit_count=1, plan_status=PlanStatus.SUCCEEDED,
    )
    snapshot = repo.get_run(db, run_id)

    token = seed_csrf(client)
    # 所有能想到的"改记录"尝试：删除计划、编辑计划、暂停/启用。
    bpost(client, f"/booking/{pid}/delete", token=token)
    bpost(client, f"/booking/{pid}/edit", plan_form(name="改名"), token=token)
    bpost(client, f"/booking/{pid}/pause", token=token)
    bpost(client, f"/booking/{pid}/enable", token=token)

    after = repo.get_run(db, run_id)
    assert after == snapshot, "运行记录（含预约编号）不得被用户端任意改写"


# ================================================================== 7. 凭据不泄露


#: 哨兵串：形状与真实令牌一致，但绝不会被真实用到。
SENTINEL = "eyJhbGciOiJIUzUxMiJ9." + "SENTINELPAYLOAD" + ".SENTINELSIGNATURE"


@pytest.mark.parametrize("path", ["/booking", "/booking/new", "/booking/1"])
def test_credentials_never_appear_on_user_pages(booking, path, monkeypatch):
    """页面、响应体与重定向地址里都不允许出现 Cookie / Authorization / 令牌。

    这里的 ``HAUT_COOKIE`` / ``HAUT_AUTHORIZATION`` 用哨兵值（**不是**真实凭据），
    断言的是"渲染层根本不碰它们"。
    """
    client, db, _settings = booking
    monkeypatch.setenv("HAUT_COOKIE", f"Admin-Token={SENTINEL}")
    monkeypatch.setenv("HAUT_AUTHORIZATION", f"Bearer {SENTINEL}")

    make_plan(client, db, name="哨兵测试")
    resp = client.get(path)
    assert resp.status_code in (200, 404)
    if resp.status_code == 200:
        body = resp.text
        assert SENTINEL not in body
        assert "Admin-Token" not in body
        assert "Bearer " not in body


def test_redirect_messages_never_carry_credentials(booking, monkeypatch):
    client, db, _settings = booking
    monkeypatch.setenv("HAUT_COOKIE", f"Admin-Token={SENTINEL}")
    token = seed_csrf(client)
    resp = bpost(client, "/booking/new", plan_form(), token=token)
    assert resp.status_code == 303
    assert SENTINEL not in resp.headers["location"]
    assert "Admin-Token" not in resp.headers["location"]


def test_booking_pages_do_not_reference_the_credentials_module(booking):
    """结构性保证：用户端模板里根本不出现凭据相关的名字。

    比"页面上没出现"更强的一条：模板文件本身不引用凭据，
    因此不存在"某条分支没走到、凭据侥幸没被渲染出来"的可能。
    ``_csrf`` 不在此列 —— 它是用户端自己的会话级令牌，与凭据无关。
    """
    from haut_seat.web.deps import TEMPLATES_DIR

    forbidden = ("Admin-Token", "Authorization", "Bearer", "credentials.json",
                 "HAUT_COOKIE", "HAUT_AUTHORIZATION")
    for name in ("booking_base.html", "booking.html", "booking_detail.html",
                 "booking_form.html", "booking_catalog.html"):
        text = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
        for word in forbidden:
            assert word not in text, f"{name} 不该提到 {word}"


# ================================================================== 8. 自己的页面，如实说明


def test_pages_state_the_per_user_scope_honestly(booking):
    """页面必须如实写明"这是你自己的页面、用的是你自己的账号"，而不是含糊过去。"""
    client, _db, _settings = booking
    for path in ("/booking", "/booking/new"):
        page = client.get(path)
        assert page.status_code == 200, path
        assert "这是你自己的页面" in page.text, path
        assert USERNAME in page.text, path
        assert "你本人绑定的图书馆账号" in page.text, path
        # 没绑定时要**明说**未绑定，不能让用户以为"没绑定也能约"。
        assert "尚未绑定" in page.text, path


def test_unbound_user_is_told_their_plans_will_not_run(booking):
    """未绑定凭据 = 计划不会被执行。这一点必须在页面上说清，且给出绑定入口。"""
    client, db, _settings = booking
    make_plan(client, db)
    listing = client.get("/booking").text
    assert "未绑定" in listing
    assert "你的预约不会被执行" in listing
    assert "/booking/bind" in listing


def test_plan_states_are_explained_in_plain_words(booking):
    """成功 / 失败 / 等待执行 / 执行中 / 结果未知 五种说法都要在页面上有解释。"""
    from haut_seat.web import repository as repo

    client, db, _settings = booking
    pid = make_plan(client, db)
    page = client.get(f"/booking/{pid}")
    assert page.status_code == 200
    assert "已暂停" in page.text, "新建的计划是暂停态，页面上要说清"

    # 列表页的状态说明必须把几种状态讲成人话，尤其是最容易被误读的两种。
    listing = client.get("/booking").text
    assert "结果未知" in listing
    assert "已暂停" in listing
    assert "失败" in listing

    # 有成功记录时，预约编号与耗时都必须显示出来。
    run_id = repo.start_run(db, pid)
    repo.finish_run(
        db, run_id, status=RunStatus.SUCCEEDED, category=ErrorCategory.NONE,
        stop_reason="success", message="预约成功", exit_code=0, booking_id=987654,
        submit_count=1, plan_status=PlanStatus.SUCCEEDED,
    )
    detail = client.get(f"/booking/{pid}")
    assert "987654" in detail.text, "成功时必须给出预约编号"
    assert "耗时" in detail.text
    assert "预约成功" in detail.text


def test_failure_reason_is_shown_to_the_user(booking):
    from haut_seat.web import repository as repo

    client, db, _settings = booking
    pid = make_plan(client, db)
    run_id = repo.start_run(db, pid)
    repo.finish_run(
        db, run_id, status=RunStatus.FAILED, category=ErrorCategory.BUSINESS_ERROR,
        stop_reason="failed", message="目标座位已被他人预约", exit_code=1,
        submit_count=1, plan_status=PlanStatus.FAILED,
    )
    detail = client.get(f"/booking/{pid}").text
    assert "目标座位已被他人预约" in detail, "失败原因必须让用户看得到"
    assert "失败" in detail


def test_unknown_run_tells_the_user_it_may_have_been_submitted(booking):
    """结果未知的详情页必须说清"可能已经提交过"，而不是含糊成失败。"""
    from haut_seat.web import repository as repo

    client, db, _settings = booking
    pid = make_plan(client, db)
    run_id = repo.start_run(db, pid)
    repo.finish_run(
        db, run_id, status=RunStatus.UNKNOWN, category=ErrorCategory.SUBMIT_UNKNOWN,
        stop_reason="unknown", message="提交后无法确认", exit_code=1,
        submit_count=2, plan_status=PlanStatus.UNKNOWN,
    )
    detail = client.get(f"/booking/{pid}").text
    assert "可能已经留下了预约记录" in detail
    assert "不是" in detail and "失败" in detail
    assert "核实" in detail


def test_run_history_is_bounded_on_the_detail_page(booking):
    from haut_seat.web import repository as repo

    client, db, _settings = booking
    pid = make_plan(client, db)
    for _ in range(14):
        run_id = repo.start_run(db, pid)
        repo.finish_run(
            db, run_id, status=RunStatus.FAILED, category=ErrorCategory.BUSINESS_ERROR,
            stop_reason="failed", message="失败", exit_code=1, submit_count=1,
            plan_status=PlanStatus.FAILED,
        )
    detail = client.get(f"/booking/{pid}").text
    # 最多 10 条：一个页面不该被无限增长的历史撑爆（库里有 14 条）。
    assert detail.count("<td>#") == 10, "详情页的运行历史条数没有上限"


def test_unknown_plan_id_returns_404(booking):
    client, _db, _settings = booking
    assert client.get("/booking/999999").status_code == 404