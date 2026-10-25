"""座位目录的 Web 路由：级联选择、自动绑定 ID、搜索、收藏、降级与安全边界。

全部离线（``admin_client`` 注入的目录替身只提供只读查询，不存在提交接口）。
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from haut_seat.core.catalog import derive_area_label, derive_floor_label
from haut_seat.web import repository as repo
from haut_seat.web import security

GOOD_PASSWORD = "Correct-Horse-9"

# 真实观测到的座位名格式；替身返回的也正是这几条
NORTH = "十层东书库北区"
SEAT_A = f"{NORTH} 01桌 E座"
SEAT_B = f"{NORTH} 01桌 F座"
SEAT_C = f"{NORTH} 02桌 A座"


def csrf_from(html: str) -> str:
    marker = 'name="_csrf" value="'
    start = html.index(marker) + len(marker)
    return html[start:html.index('"', start)]


def token_of(client) -> str:
    return client.get("/admin/catalog").text


def post(client, path: str, data: dict):
    """带上当前页面的 CSRF 令牌发 POST。"""
    payload = dict(data)
    payload.setdefault("_csrf", csrf_from(token_of(client)))
    return client.post(path, data=payload)


def textarea_value(html: str, field: str) -> str:
    match = re.search(
        rf'<textarea[^>]*name="{field}"[^>]*>(.*?)</textarea>', html, re.DOTALL
    )
    return match.group(1) if match else ""


def listed_seat_ids(html: str) -> set[str]:
    """页面上**座位列表里**实际渲染出来的座位 ID。

    必须只看复选框，不能用「座位名是否出现在页面里」当判据——导入表单的
    placeholder 里就含一个座位名，会让断言假通过。

    复选框 id 形如 ``seat_<区域ID>_<座位ID>``：座位 ID 是否全局唯一未核实，
    加上区域前缀才能保证跨区域列表里 DOM id 不重复。
    """
    return set(re.findall(r'id="seat_\d+_([^"]+)"', html))


def flash_message(resp) -> str:
    """从 303 的 Location 里取出提示文本（query string 是 URL 编码的）。"""
    from urllib.parse import parse_qs, unquote, urlparse

    query = parse_qs(urlparse(unquote(resp.headers["location"])).query)
    return (query.get("msg") or [""])[0]


def location_query(resp) -> dict:
    """303 Location 的查询串（parse_qs 后的多值字典）。"""
    from urllib.parse import parse_qs, unquote, urlparse

    return parse_qs(urlparse(unquote(resp.headers["location"])).query)


def assert_single_query_string(resp, *, region: str | None = None):
    """Location 只能有一个 ``?``；``region`` 应当是纯数字，不被后面的参数污染。"""
    location = resp.headers["location"]
    assert location.count("?") <= 1, f"出现了两个问号：{location}"
    query = location_query(resp)
    assert query.get("msg"), f"提示被吞掉了：{location}"
    if region is not None:
        assert query.get("region") == [region], f"region 被污染：{location}"
    return query


def region_input_value(html: str) -> str:
    """计划表单里「当前生效的区域」= 区域下拉框中选中的那一项。

    区域控件从裸数字输入框改成了「按名字选」的下拉框（目录里还没有的区域仍可
    在高级设置里手工填 ID），所以要认的是 ``selected`` 的 option。断言强度不变：
    仍然要求点选之后该区域真的处于选中状态。
    """
    match = re.search(r'<option value="([^"]*)"\s+selected', html)
    return match.group(1) if match else ""


def seed_catalog(app, regionid=16, seats=None):
    """直接把目录写进库（路由层测试用，省掉网络替身往返）。"""
    rows = seats or [
        {"seat_id": "2481", "seat_name": SEAT_A},
        {"seat_id": "2482", "seat_name": SEAT_B},
        {"seat_id": "2483", "seat_name": SEAT_C},
    ]
    for row in rows:
        row.setdefault("area_label", derive_area_label(row["seat_name"]))
        row.setdefault("floor_label", derive_floor_label(row["seat_name"]))
    repo.add_catalog_region(app.state.db, regionid)
    repo.sync_region_catalog(app.state.db, regionid, rows)


# ============================================================ 访问控制


def test_the_catalog_page_loads_for_an_admin(admin_client):
    _app, client = admin_client
    resp = client.get("/admin/catalog")
    assert resp.status_code == 200
    assert "座位目录" in resp.text


def test_the_page_states_that_the_catalog_is_not_a_bookability_answer(admin_client):
    """目录存在 ≠ 可预约。这句话必须出现在页面上，而不是只写在文档里。"""
    _app, client = admin_client
    assert "目录 ≠ 可预约" in client.get("/admin/catalog").text


def test_anonymous_visitors_are_sent_to_login_and_cannot_post(make_client):
    app, client = make_client()
    security.create_admin(app.state.db, "tester", GOOD_PASSWORD)

    assert client.get("/admin/catalog").status_code == 303
    assert client.post("/admin/catalog/refresh", data={}).status_code == 401
    assert client.post("/admin/catalog/picks", data={}).status_code == 401


def test_a_post_without_a_csrf_token_is_refused(admin_client):
    _app, client = admin_client
    resp = client.post("/admin/catalog/refresh", data={})
    assert resp.status_code == 400
    assert "CSRF" in resp.text


# ============================================================ 添加区域 / 更新目录


def test_adding_a_region_then_refreshing_fills_the_catalog(admin_client):
    app, client = admin_client

    added = post(client, "/admin/catalog/regions", {"regionid": "16"})
    assert added.status_code == 303
    assert repo.get_catalog_region(app.state.db, 16) is not None

    refreshed = post(client, "/admin/catalog/refresh", {})
    assert refreshed.status_code == 303
    assert "ok=1" in refreshed.headers["location"] or "ok" not in refreshed.headers["location"]

    entries = repo.list_catalog_seats(app.state.db, regionid=16)
    assert {e["seat_name"] for e in entries} == {SEAT_A, SEAT_B, SEAT_C}
    assert repo.get_catalog_region(app.state.db, 16)["region_label"] == NORTH


def test_a_bogus_region_id_is_rejected(admin_client):
    _app, client = admin_client
    resp = post(client, "/admin/catalog/regions", {"regionid": "0"})
    assert resp.status_code == 303
    assert "正整数" in flash_message(resp)


def test_an_unknown_region_filter_does_not_invent_a_name(admin_client):
    _app, client = admin_client
    resp = client.get("/admin/catalog", params={"region": "999999"})
    assert resp.status_code == 200
    assert "999999" not in resp.text or "挑选座位" not in resp.text


# ============================================================ 级联与搜索


def test_seats_are_listed_for_the_selected_region(admin_client):
    app, client = admin_client
    seed_catalog(app)

    resp = client.get("/admin/catalog", params={"region": "16"})

    assert resp.status_code == 200
    assert listed_seat_ids(resp.text) == {"2481", "2482", "2483"}
    assert NORTH in resp.text
    assert "由座位名推断，非权威" in resp.text


def test_the_floor_filter_narrows_the_seat_list(admin_client):
    app, client = admin_client
    seed_catalog(app, seats=[
        {"seat_id": "1", "seat_name": f"{NORTH} 01桌 A座"},
        {"seat_id": "2", "seat_name": "五层东书库北区 01桌 A座"},
    ])

    both = client.get("/admin/catalog", params={"region": "16"})
    assert listed_seat_ids(both.text) == {"1", "2"}

    only_five = client.get("/admin/catalog", params={"region": "16", "floor": "五层"})
    assert listed_seat_ids(only_five.text) == {"2"}, "楼层筛选必须只剩五层的座位"


def test_search_matches_the_seat_name(admin_client):
    app, client = admin_client
    seed_catalog(app, seats=[
        {"seat_id": "1", "seat_name": f"{NORTH} 01桌 A座"},
        {"seat_id": "2", "seat_name": f"{NORTH} 02桌 B座"},
    ])

    resp = client.get("/admin/catalog", params={"region": "16", "q": "02桌"})

    assert listed_seat_ids(resp.text) == {"2"}, "搜索必须只留下命中的座位"


def test_search_treats_sql_wildcards_as_literal_text(admin_client):
    """``%`` 不能变成通配符——否则搜一个百分号就把整个目录列出来。"""
    app, client = admin_client
    seed_catalog(app)

    resp = client.get("/admin/catalog", params={"region": "16", "q": "%"})

    assert resp.status_code == 200
    assert listed_seat_ids(resp.text) == set()


def test_search_reaches_across_regions_when_no_region_is_selected(admin_client):
    app, client = admin_client
    seed_catalog(app, regionid=16, seats=[{"seat_id": "1", "seat_name": f"{NORTH} 01桌 A座"}])
    seed_catalog(app, regionid=35, seats=[
        {"seat_id": "2", "seat_name": "五层东书库南区 02桌 B座"},
    ])

    resp = client.get("/admin/catalog", params={"q": "02桌"})

    assert resp.status_code == 200
    assert listed_seat_ids(resp.text) == {"2"}


def test_a_cross_region_result_says_which_region_each_seat_belongs_to(admin_client):
    """跨区域列表必须标出归属：否则勾出来的座位无从核对是哪个区域的。"""
    app, client = admin_client
    seed_catalog(app, regionid=16, seats=[{"seat_id": "1", "seat_name": f"{NORTH} 01桌 A座"}])
    seed_catalog(app, regionid=35, seats=[
        {"seat_id": "2", "seat_name": "五层东书库南区 02桌 B座"},
    ])

    resp = client.get("/admin/catalog", params={"q": "02桌"})

    assert "跨区域搜索结果" in resp.text
    assert "五层东书库南区" in resp.text  # 区域名一并显示，便于核对


def test_picking_from_a_cross_region_search_binds_each_seats_own_region(admin_client):
    """跨区域勾选时，区域 ID 仍然来自座位自身的区域，不是「当前区域」。"""
    app, client = admin_client
    seed_catalog(app, regionid=35, seats=[
        {"seat_id": "2", "seat_name": "五层东书库南区 02桌 B座"},
    ])

    applied = post(client, "/admin/catalog/picks", {
        "q": "02桌",
        "seat": ["35|五层东书库南区 02桌 B座"],
    })
    assert applied.status_code == 303

    assert [p["regionid"] for p in repo.list_picks(app.state.db)] == [35]

    form = client.get("/admin/plans/new")
    assert region_input_value(form.text) == "35"


def test_an_empty_catalog_does_not_render_an_empty_seat_table(admin_client):
    """目录为空时列表不渲染，也不该把「没有区域」当成一次搜索。"""
    _app, client = admin_client
    resp = client.get("/admin/catalog")
    assert resp.status_code == 200
    assert listed_seat_ids(resp.text) == set()


# ============================================================ 自动绑定 ID 与座位名


def test_picking_seats_binds_regionid_and_seat_names_into_the_plan_form(admin_client):
    """本功能的核心：点选之后，真实区域 ID 与座位名自动出现在计划表单里。"""
    app, client = admin_client
    seed_catalog(app)

    applied = post(client, "/admin/catalog/picks", {
        "region": "16",
        "seat": [f"16|{SEAT_A}", f"16|{SEAT_C}"],
    })
    assert applied.status_code == 303

    form = client.get("/admin/plans/new")
    assert form.status_code == 200
    assert textarea_value(form.text, "preferred_seats").splitlines() == [SEAT_A, SEAT_C]
    assert region_input_value(form.text) == "16"


def test_picks_from_another_region_are_kept_when_the_view_changes(admin_client):
    """零 JS 下靠「每次提交只改本页范围」来攒选择，换区域不能把之前的清掉。"""
    app, client = admin_client
    seed_catalog(app)
    seed_catalog(app, regionid=35, seats=[
        {"seat_id": "9", "seat_name": "五层东书库南区 01桌 A座"},
    ])

    post(client, "/admin/catalog/picks", {"region": "16", "seat": [f"16|{SEAT_A}"]})
    post(client, "/admin/catalog/picks", {
        "region": "35", "seat": ["35|五层东书库南区 01桌 A座"],
    })

    picks = [(p["regionid"], p["seat_name"]) for p in repo.list_picks(app.state.db)]
    assert picks == [(16, SEAT_A), (35, "五层东书库南区 01桌 A座")]


def test_unchecking_a_seat_removes_it_from_the_picks(admin_client):
    app, client = admin_client
    seed_catalog(app)

    post(client, "/admin/catalog/picks", {"region": "16", "seat": [f"16|{SEAT_A}", f"16|{SEAT_B}"]})
    post(client, "/admin/catalog/picks", {"region": "16", "seat": [f"16|{SEAT_B}"]})

    assert [p["seat_name"] for p in repo.list_picks(app.state.db)] == [SEAT_B]


def test_a_filtered_view_only_replaces_the_seats_it_shows(admin_client):
    """带筛选时，页面上没出现的座位不能被当成「取消勾选」。"""
    app, client = admin_client
    seed_catalog(app, seats=[
        {"seat_id": "1", "seat_name": f"{NORTH} 01桌 A座"},
        {"seat_id": "2", "seat_name": "五层东书库北区 01桌 B座"},
    ])

    post(client, "/admin/catalog/picks", {"region": "16", "seat": ["16|五层东书库北区 01桌 B座"]})
    # 只看十层，且什么都不勾
    post(client, "/admin/catalog/picks", {"region": "16", "floor": "十层"})

    assert [p["seat_name"] for p in repo.list_picks(app.state.db)] == ["五层东书库北区 01桌 B座"]


def test_saving_a_plan_clears_the_draft(admin_client):
    app, client = admin_client
    seed_catalog(app)
    post(client, "/admin/catalog/picks", {"region": "16", "seat": [f"16|{SEAT_A}"]})

    today = date.today().isoformat()
    created = post(client, "/admin/plans/new", {
        "name": "用目录建的",
        "run_date": today,
        "repeat_rule": "once",
        "trigger_at": "07:00:00",
        "regionid": "16",
        "book_start": f"{today} 08:00:00",
        "book_end": f"{today} 12:00:00",
        "preferred_seats": SEAT_A,
        "preferred_prefixes": "",
        "confirmed_bookable_states": '[["2", 1, false]]',
        "max_attempts_per_seat": "3",
        "note": "",
    })

    assert created.status_code == 303, created.text
    assert repo.list_picks(app.state.db) == [], "计划保存后草稿必须清空，免得串到下一个计划"

    plans = repo.list_plans(app.state.db)
    assert len(plans) == 1
    assert plans[0]["preferred_seats"] == [SEAT_A]
    assert plans[0]["regionid"] == 16


def test_the_manual_path_still_works_without_touching_the_catalog(admin_client):
    """不用目录、直接手填座位名——既有工作流必须完全不受影响。"""
    app, client = admin_client

    today = date.today().isoformat()
    created = post(client, "/admin/plans/new", {
        "name": "手填的",
        "run_date": today,
        "repeat_rule": "once",
        "trigger_at": "07:00:00",
        "regionid": "38",
        "book_start": f"{today} 08:00:00",
        "book_end": f"{today} 12:00:00",
        "preferred_seats": "十层东书库北区 09桌 Z座\n十层东书库北区 08桌 Y座",
        "preferred_prefixes": "",
        "confirmed_bookable_states": '[["2", 1, false]]',
        "max_attempts_per_seat": "3",
        "note": "手填",
    })

    assert created.status_code == 303
    plan = repo.list_plans(app.state.db)[0]
    assert plan["preferred_seats"] == ["十层东书库北区 09桌 Z座", "十层东书库北区 08桌 Y座"]
    assert plan["regionid"] == 38, "手工输入的区域 ID 不能被目录改写"


def test_a_form_validation_failure_does_not_override_what_the_user_typed(admin_client):
    """校验失败重渲染时，用户提交的内容优先——不能被草稿悄悄改掉。"""
    app, client = admin_client
    seed_catalog(app)
    post(client, "/admin/catalog/picks", {"region": "16", "seat": [f"16|{SEAT_C}"]})

    today = date.today().isoformat()
    resp = post(client, "/admin/plans/new", {
        "name": "",  # 故意留空 → 校验失败
        "run_date": today,
        "repeat_rule": "once",
        "trigger_at": "07:00:00",
        "regionid": "16",
        "book_start": f"{today} 08:00:00",
        "book_end": f"{today} 12:00:00",
        "preferred_seats": SEAT_A,
        "preferred_prefixes": "",
        "confirmed_bookable_states": '[["2", 1, false]]',
        "max_attempts_per_seat": "3",
        "note": "",
    })

    assert resp.status_code == 200 and "请修正以下问题" in resp.text
    assert textarea_value(resp.text, "preferred_seats") == SEAT_A


# ============================================================ 收藏


def test_favorites_can_be_added_and_removed(admin_client):
    app, client = admin_client
    seed_catalog(app)

    added = post(client, "/admin/catalog/favorites", {"regionid": "16", "seat_name": SEAT_A})
    assert added.status_code == 303
    assert [f["seat_name"] for f in repo.list_favorites(app.state.db)] == [SEAT_A]

    # 重复收藏不报错，也不产生第二行
    post(client, "/admin/catalog/favorites", {"regionid": "16", "seat_name": SEAT_A})
    assert len(repo.list_favorites(app.state.db)) == 1

    removed = post(client, "/admin/catalog/favorites/delete",
                   {"regionid": "16", "seat_name": SEAT_A})
    assert removed.status_code == 303
    assert repo.list_favorites(app.state.db) == []


def test_a_favorite_survives_the_seat_going_offline_and_is_flagged(admin_client):
    app, client = admin_client
    seed_catalog(app)
    post(client, "/admin/catalog/favorites", {"regionid": "16", "seat_name": SEAT_A})

    repo.sync_region_catalog(app.state.db, 16, [
        {"seat_id": "2482", "seat_name": SEAT_B, "area_label": NORTH, "floor_label": "十层"},
    ])

    favorite = repo.list_favorites(app.state.db)[0]
    assert favorite["seat_name"] == SEAT_A and favorite["known"] is False
    assert "已不在目录中" in client.get("/admin/catalog", params={"region": "16"}).text


# ============================================================ 降级与离线导入


def test_a_failed_refresh_keeps_the_page_usable_and_shows_both_times(make_client, offline):
    """刷新失败后：旧目录仍在，页面同时显示上次成功时间与本次失败原因。"""
    app, client = make_client()
    security.create_admin(app.state.db, "tester", GOOD_PASSWORD)
    client.post("/admin/login", data={"username": "tester", "password": GOOD_PASSWORD})
    seed_catalog(app)
    repo.set_catalog_state(app.state.db, status="ok", message="更新成功", ok=True,
                           region_count=1, seat_count=3)

    failing = offline.catalog_service(app.state.db, app.state.settings,
                                     error=RuntimeError("离线测试：网络不可达"))
    app.state.catalog_service = failing
    resp = post(client, "/admin/catalog/refresh", {})

    assert resp.status_code == 303
    assert "ok=0" in resp.headers["location"]

    page = client.get("/admin/catalog", params={"region": "16"})
    assert page.status_code == 200
    assert SEAT_A in page.text, "失败不得清掉已有目录"
    assert "更新失败" in page.text
    assert repo.get_catalog_state(app.state.db)["last_ok_at"], "上次成功时间必须保留"


def test_offline_import_fills_the_catalog_without_any_network(admin_client):
    app, client = admin_client

    resp = post(client, "/admin/catalog/import", {
        "regionid": "16",
        "payload": '{"layout": null, "seatList": ['
                   '{"id": 2481, "seatName": "十层东书库北区 01桌 E座"}]}',
    })

    assert resp.status_code == 303
    assert [s["seat_name"] for s in repo.list_catalog_seats(app.state.db, regionid=16)] == [SEAT_A]


def test_offline_import_rejects_a_missing_region_id(admin_client):
    _app, client = admin_client
    resp = post(client, "/admin/catalog/import", {"regionid": "", "payload": "{}"})
    assert resp.status_code == 303
    assert "区域 ID" in flash_message(resp)


# ============================================================ 安全


@pytest.mark.parametrize("path", ["/admin/catalog", "/admin/plans/new"])
def test_the_session_token_never_leaks_into_the_catalog_pages(admin_client, path):
    app, client = admin_client
    seed_catalog(app)
    post(client, "/admin/catalog/picks", {"region": "16", "seat": [f"16|{SEAT_A}"]})

    token = client.cookies.get(security.SESSION_COOKIE)
    assert token

    resp = client.get(path)
    assert resp.status_code == 200
    for marker in (token, "HAUT_COOKIE", "HAUT_AUTHORIZATION", "scrypt$", "token_hash"):
        assert marker not in resp.text, marker


def test_the_catalog_never_stores_credentials(admin_client):
    """目录表里只有座位名与 ID，任何位置都不该出现凭据。"""
    app, client = admin_client
    seed_catalog(app)
    post(client, "/admin/catalog/refresh", {})

    rows = app.state.db.query_all("SELECT seat_id, seat_name FROM seat_catalog")
    blob = " ".join(f"{r['seat_id']} {r['seat_name']}" for r in rows)
    for marker in ("Authorization", "Bearer", "token", "cookie", "scrypt$"):
        assert marker.lower() not in blob.lower()


# ===================================== 验收回归：303 查询参数拼接（2026-10-18）


def test_redirect_appends_to_an_existing_query_string():
    """path 已带查询串时必须用 & 续接，否则 msg 会被当成 region 的值。"""
    from haut_seat.web.deps import redirect

    resp = redirect("/admin/catalog?region=16", msg="已添加区域 16。", ok=True)

    query = assert_single_query_string(resp, region="16")
    assert query["ok"] == ["1"]
    assert "已添加区域" in query["msg"][0]


def test_redirect_to_a_queryless_path_is_unchanged():
    """不带查询串的路径语义不变：仍是 ``path ? msg & ok``。"""
    from haut_seat.web.deps import redirect

    resp = redirect("/admin/catalog", msg="更新成功。", ok=False)

    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/admin/catalog?")
    query = location_query(resp)
    assert query["msg"] == ["更新成功。"] and query["ok"] == ["0"]


def test_redirect_without_a_message_adds_no_separator_at_all():
    """空参数集合不得留下多余的 ? 或 &。"""
    from haut_seat.web.deps import redirect

    for path in ("/admin/catalog", "/admin/catalog?region=16"):
        resp = redirect(path)
        assert resp.headers["location"] == path


def test_adding_a_region_keeps_the_flash_and_the_region(admin_client):
    """「添加并开始挑选」必须真的落到该区域：region 纯数字、提示还在。"""
    _app, client = admin_client

    resp = post(client, "/admin/catalog/regions", {"regionid": "16"})

    assert resp.status_code == 303
    assert_single_query_string(resp, region="16")


def test_renaming_a_region_keeps_the_flash_and_the_region(admin_client):
    app, client = admin_client
    seed_catalog(app)

    resp = post(client, "/admin/catalog/regions/16/rename", {"region_label": "十层北"})

    assert resp.status_code == 303
    assert "十层北" in flash_message(resp)
    assert_single_query_string(resp, region="16")


def test_offline_import_keeps_the_flash_and_the_region(admin_client):
    app, client = admin_client

    resp = post(client, "/admin/catalog/import", {
        "regionid": "16",
        "payload": '{"layout": null, "seatList": ['
                   '{"id": 2481, "seatName": "十层东书库北区 01桌 E座"}]}',
    })

    assert resp.status_code == 303
    query = assert_single_query_string(resp, region="16")
    assert query["ok"] == ["1"]

    # 落地页真的列出该区域的座位（region 解析不出数字时会退回未选区域视图）
    landed = client.get(resp.headers["location"])
    assert landed.status_code == 200
    assert listed_seat_ids(landed.text) == {"2481"}


def test_applying_picks_keeps_the_flash_and_the_region(admin_client):
    """``_catalog_return_url`` 也返回带 ?region= 的路径，同样不能拼出两个问号。"""
    app, client = admin_client
    seed_catalog(app)

    resp = post(client, "/admin/catalog/picks", {"region": "16", "seat": [f"16|{SEAT_A}"]})

    assert resp.status_code == 303
    query = assert_single_query_string(resp, region="16")
    assert "已应用选择" in query["msg"][0]


def test_clearing_picks_keeps_the_flash_and_the_region(admin_client):
    app, client = admin_client
    seed_catalog(app)
    post(client, "/admin/catalog/picks", {"region": "16", "seat": [f"16|{SEAT_A}"]})

    resp = post(client, "/admin/catalog/picks/clear", {"region": "16"})

    assert resp.status_code == 303
    query = assert_single_query_string(resp, region="16")
    assert "已清空待选座位" in query["msg"][0]


def test_a_favorite_redirect_keeps_the_flash_and_the_region(admin_client):
    app, client = admin_client
    seed_catalog(app)

    resp = post(client, "/admin/catalog/favorites", {"regionid": "16", "seat_name": SEAT_A})

    assert resp.status_code == 303
    query = assert_single_query_string(resp, region="16")
    assert "已收藏" in query["msg"][0]


# ============================= 验收回归：跨区域时区域 ID 提示不得自相矛盾


def test_apply_picks_reports_a_single_bound_region(web_db):
    """单区域：确实改写了 regionid，就要如实报出来。"""
    from haut_seat.web.routes import _apply_picks

    repo.replace_picks(web_db, [(16, SEAT_A)])

    values, info = _apply_picks(web_db, {"regionid": 1, "preferred_seats": ""})

    assert info["bound_regionid"] == 16
    assert values["regionid"] == 16


def test_apply_picks_reports_no_bound_region_for_multiple_regions(web_db):
    """多区域：并未改写 regionid，bound_regionid 必须是 None。"""
    from haut_seat.web.routes import _apply_picks

    repo.replace_picks(web_db, [(16, SEAT_A), (35, "五层东书库南区 01桌 A座")])

    values, info = _apply_picks(web_db, {"regionid": 1, "preferred_seats": ""})

    assert info["bound_regionid"] is None
    assert values["regionid"] == 1, "多区域不得改写计划里的区域 ID"


def test_a_single_region_pick_says_which_region_was_bound(admin_client):
    app, client = admin_client
    seed_catalog(app)
    post(client, "/admin/catalog/picks", {"region": "16", "seat": [f"16|{SEAT_A}"]})

    form = client.get("/admin/plans/new")

    assert form.status_code == 200
    assert "已绑定为" in form.text and "16" in form.text
    assert region_input_value(form.text) == "16"
    assert "请自行确认" not in form.text, "单区域不该出现「请自行确认区域」的说法"
    assert "未改写区域 ID" not in form.text


def test_a_multi_region_pick_does_not_claim_the_region_was_bound(admin_client):
    """同一页不能既说「已自动绑定」又说「没有自动改写」。"""
    app, client = admin_client
    seed_catalog(app)
    seed_catalog(app, regionid=35, seats=[
        {"seat_id": "9", "seat_name": "五层东书库南区 01桌 A座"},
    ])
    # 先记下未挑选时表单里的区域 ID，稍后比对它有没有被悄悄改写
    # （配置默认值恰好也是 16，所以不能直接拿 16/35 去比）
    before = region_input_value(client.get("/admin/plans/new").text)

    post(client, "/admin/catalog/picks", {"region": "16", "seat": [f"16|{SEAT_A}"]})
    post(client, "/admin/catalog/picks", {
        "region": "35", "seat": ["35|五层东书库南区 01桌 A座"],
    })

    form = client.get("/admin/plans/new")

    assert form.status_code == 200
    assert "请自行确认" in form.text
    assert "未改写区域 ID" in form.text
    assert "已绑定为" not in form.text, "并未改写 regionid 就不能声称已绑定"
    assert region_input_value(form.text) == before, "多区域挑选不得改写计划里的区域 ID"


def test_the_plan_form_shown_after_a_validation_error_is_also_consistent(admin_client):
    """校验失败回显时走的是另一个 pick_info 字面量，同样不能自相矛盾。"""
    app, client = admin_client
    seed_catalog(app)
    seed_catalog(app, regionid=35, seats=[
        {"seat_id": "9", "seat_name": "五层东书库南区 01桌 A座"},
    ])
    post(client, "/admin/catalog/picks", {"region": "16", "seat": [f"16|{SEAT_A}"]})
    post(client, "/admin/catalog/picks", {
        "region": "35", "seat": ["35|五层东书库南区 01桌 A座"],
    })

    resp = post(client, "/admin/plans/new", {"name": "", "regionid": "0"})

    assert resp.status_code == 200
    assert "请修正以下问题" in resp.text
    assert "已绑定为" not in resp.text


# ================================ 验收回归：本地导入与在线更新是两个来源


def test_offline_import_records_its_own_time_and_does_not_look_empty(admin_client):
    """导入成功后页面不得仍像「没有任何目录数据」。"""
    app, client = admin_client

    post(client, "/admin/catalog/import", {
        "regionid": "16",
        "payload": '{"layout": null, "seatList": ['
                   '{"id": 2481, "seatName": "十层东书库北区 01桌 E座"}]}',
    })

    state = repo.get_catalog_import_state(app.state.db)
    assert state["last_status"] == "ok" and state["last_import_at"]
    assert state["last_regionid"] == 16 and state["last_seat_count"] == 1

    page = client.get("/admin/catalog")
    assert page.status_code == 200
    assert state["last_import_at"] in page.text
    assert "本地导入" in page.text
    assert "目录数据来自" in page.text, "只有导入过时必须说清数据来自哪"


def test_an_import_never_claims_to_be_an_online_refresh(admin_client):
    app, client = admin_client

    post(client, "/admin/catalog/import", {
        "regionid": "16",
        "payload": '{"layout": null, "seatList": ['
                   '{"id": 2481, "seatName": "十层东书库北区 01桌 E座"}]}',
    })

    online = repo.get_catalog_state(app.state.db)
    assert online["last_ok_at"] is None, "导入不是在线刷新成功"
    assert online["last_status"] == "never"


def test_a_failed_import_never_touches_the_online_success_time(admin_client):
    app, client = admin_client
    seed_catalog(app)
    repo.set_catalog_state(app.state.db, status="ok", message="更新成功", ok=True,
                           region_count=1, seat_count=3)
    online_before = repo.get_catalog_state(app.state.db)
    seats_before = repo.list_catalog_seats(app.state.db, regionid=16)

    resp = post(client, "/admin/catalog/import", {"regionid": "16", "payload": "{坏掉的 JSON"})

    assert resp.status_code == 303
    assert "ok=0" in resp.headers["location"]
    assert repo.get_catalog_state(app.state.db)["last_ok_at"] == online_before["last_ok_at"]
    assert repo.get_catalog_import_state(app.state.db)["last_import_at"] is None
    assert repo.list_catalog_seats(app.state.db, regionid=16) == seats_before


# ==================================== 阶段 3：按名字选区域、按优先级排座位


def test_the_region_picker_offers_catalog_names_and_says_where_each_name_came_from(admin_client):
    """区域下拉必须显示目录里的名字，并说清这个名字是人工起的还是推断的。"""
    app, client = admin_client
    seed_catalog(app)                      # 16：由座位名推断
    seed_catalog(app, regionid=35, seats=[
        {"seat_id": "9", "seat_name": "五层东书库南区 01桌 A座"},
    ])
    repo.rename_catalog_region(app.state.db, 35, "五层南区（靠窗）")

    html = client.get("/admin/catalog").text

    assert 'name="region"' in html, "目录页必须能按名字选区域，而不是只能点区域 ID"
    assert "十层东书库北区" in html and "五层南区（靠窗）" in html
    assert "人工命名" in html
    assert "由座位名推断，非权威" in html


def test_the_unknown_region_id_is_not_offered_as_a_name(admin_client):
    """没登记过的区域不能出现在下拉里——否则等于伪造了一个名字。"""
    app, client = admin_client
    seed_catalog(app)

    options = re.findall(r'<option value="(\d+)"', client.get("/admin/catalog").text)

    assert options == ["16"]


def test_picking_by_name_lands_on_that_region(admin_client):
    """下拉选中的区域就是列表要渲染的区域（名称与座位都对得上）。"""
    app, client = admin_client
    seed_catalog(app)

    picked = client.get("/admin/catalog", params={"region": "16"})

    assert picked.status_code == 200
    assert listed_seat_ids(picked.text) == {"2481", "2482", "2483"}
    assert "十层东书库北区" in picked.text


def test_the_floor_filter_admits_it_is_not_authoritative(admin_client):
    app, client = admin_client
    seed_catalog(app)

    html = client.get("/admin/catalog", params={"region": "16"}).text

    assert "楼层（由座位名推断，非权威）" in html


def _pick_three_seats(app, client):
    post(client, "/admin/catalog/picks", {
        "region": "16", "seat": [f"16|{SEAT_A}", f"16|{SEAT_B}", f"16|{SEAT_C}"],
    })
    assert [p["seat_name"] for p in repo.list_picks(app.state.db)] == [SEAT_A, SEAT_B, SEAT_C]


def _preferred_seats_line(client) -> list[str]:
    return textarea_value(client.get("/admin/plans/new").text, "preferred_seats").splitlines()


def test_moving_a_pick_up_changes_the_priority_order_everywhere(admin_client):
    """顺序即优先级：上移必须同时改草稿顺序与计划表单里的尝试顺序。"""
    app, client = admin_client
    seed_catalog(app)
    _pick_three_seats(app, client)

    resp = post(client, "/admin/catalog/picks/move",
                {"region": "16", "regionid": "16", "seat_name": SEAT_C, "direction": "up"})

    assert resp.status_code == 303
    assert "上移" in flash_message(resp)
    assert [p["seat_name"] for p in repo.list_picks(app.state.db)] == [SEAT_A, SEAT_C, SEAT_B]
    assert _preferred_seats_line(client) == [SEAT_A, SEAT_C, SEAT_B]


def test_moving_a_pick_down_changes_the_priority_order_everywhere(admin_client):
    app, client = admin_client
    seed_catalog(app)
    _pick_three_seats(app, client)

    resp = post(client, "/admin/catalog/picks/move",
                {"region": "16", "regionid": "16", "seat_name": SEAT_A, "direction": "down"})

    assert resp.status_code == 303
    assert [p["seat_name"] for p in repo.list_picks(app.state.db)] == [SEAT_B, SEAT_A, SEAT_C]
    assert _preferred_seats_line(client) == [SEAT_B, SEAT_A, SEAT_C]


def test_a_pick_moved_two_steps_up_really_ends_up_first(admin_client):
    """连点两次上移 = 往前两位；不能只动一次或动成别的座位。"""
    app, client = admin_client
    seed_catalog(app)
    _pick_three_seats(app, client)

    for _ in range(2):
        post(client, "/admin/catalog/picks/move",
             {"region": "16", "regionid": "16", "seat_name": SEAT_C, "direction": "up"})

    assert [p["seat_name"] for p in repo.list_picks(app.state.db)] == [SEAT_C, SEAT_A, SEAT_B]


def test_moving_off_the_edge_is_a_harmless_no_op(admin_client):
    """越界移动不得报错、不得丢座位、不得改变顺序。"""
    app, client = admin_client
    seed_catalog(app)
    _pick_three_seats(app, client)

    too_high = post(client, "/admin/catalog/picks/move",
                    {"region": "16", "regionid": "16", "seat_name": SEAT_A, "direction": "up"})
    too_low = post(client, "/admin/catalog/picks/move",
                   {"region": "16", "regionid": "16", "seat_name": SEAT_C, "direction": "down"})

    for resp in (too_high, too_low):
        assert resp.status_code == 303
        assert "没有变化" in flash_message(resp)
        assert "ok=0" not in resp.headers["location"], "越界不是错误，不该报红"
    assert [p["seat_name"] for p in repo.list_picks(app.state.db)] == [SEAT_A, SEAT_B, SEAT_C]


def test_moving_an_unknown_seat_and_a_bad_direction_change_nothing(admin_client):
    app, client = admin_client
    seed_catalog(app)
    _pick_three_seats(app, client)

    missing = post(client, "/admin/catalog/picks/move",
                   {"region": "16", "regionid": "16", "seat_name": "不存在的座位", "direction": "up"})
    bad = post(client, "/admin/catalog/picks/move",
               {"region": "16", "regionid": "16", "seat_name": SEAT_A, "direction": "sideways"})

    assert "已经没有" in flash_message(missing)
    assert "上移或下移" in flash_message(bad)
    assert [p["seat_name"] for p in repo.list_picks(app.state.db)] == [SEAT_A, SEAT_B, SEAT_C]


def test_a_move_returns_to_the_plan_form_being_edited(admin_client):
    """从编辑计划页进来的调序必须回到那一页，而不是把人丢回目录页。"""
    app, client = admin_client
    seed_catalog(app)
    _pick_three_seats(app, client)

    resp = client.post("/admin/catalog/picks/move", data={
        "_csrf": csrf_from(client.get("/admin/catalog").text),
        "region": "16", "regionid": "16", "seat_name": SEAT_C, "direction": "up",
        "return": "plan_edit", "plan_id": "7",
    })

    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/admin/plans/7/edit")


def _make_plan(app, *, regionid=16) -> int:
    """直接落一条计划（这里要验的是「计划表单 ↔ 目录」这条链接，不是创建流程）。"""
    from haut_seat.web.validation import parse_plan

    data, _ = parse_plan({
        "name": "带计划的链接", "run_date": "2026-01-02", "repeat_rule": "once",
        "trigger_at": "07:00", "book_start": "2026-01-02 08:00:00",
        "book_end": "2026-01-02 12:00:00", "regionid": str(regionid),
        "preferred_seats": SEAT_A, "preferred_prefixes": "",
        "confirmed_bookable_states": '[["2", 1, false]]',
        "max_attempts_per_seat": "3", "note": "",
    }, today=date(2026, 1, 1))
    return repo.create_plan(app.state.db, data, enabled=True)


def catalog_link(html: str) -> str:
    """计划表单里「从座位目录挑选」那条链接，**按浏览器的方式解码后**返回。

    必须解码：页面里是 ``&amp;``，浏览器会把它还原成 ``&``，因此真正发出去的
    查询串以解码后的为准。断言原样字符串会把"转义了两次"当成通过。
    """
    from html import unescape

    match = re.search(
        r'<a class="btn btn-sm btn-primary" href="([^"]*)">从座位目录挑选</a>', html
    )
    assert match, "计划表单里没有「从座位目录挑选」入口"
    return unescape(match.group(1))


def test_the_plan_form_link_keeps_the_plan_on_its_way_to_the_catalog(admin_client):
    """从编辑页去目录时必须**带上计划编号**，否则回来时计划就丢了。

    曾经的写法在 ``{% set %}`` 的字符串里自己写了 ``&amp;``，Jinja 又转义一次，
    渲染成 ``&amp;amp;``：浏览器把它读成 ``return=plan_edit`` 加一个叫
    ``amp;plan_id`` 的杂参数 —— 计划编号静默消失（既有的管理端用例只测了 POST
    回去的那一步，所以这条一直没被发现）。
    """
    from urllib.parse import parse_qs, urlparse

    app, client = admin_client
    seed_catalog(app)
    plan_id = _make_plan(app)

    html = client.get(f"/admin/plans/{plan_id}/edit").text
    assert "&amp;amp;" not in html, "链接被转义了两次，参数名会变成 amp;plan_id"

    query = parse_qs(urlparse(catalog_link(html)).query)
    assert query.get("return") == ["plan_edit"]
    assert query.get("plan_id") == [str(plan_id)], "计划编号在跳转途中丢了"


def test_following_that_link_offers_to_return_to_the_plan_being_edited(admin_client):
    """真的照着那条链接走一遍：目录页要认得这个计划，按钮也回到它。"""
    app, client = admin_client
    seed_catalog(app)
    plan_id = _make_plan(app)
    post(client, "/admin/catalog/picks", {"region": "16", "seat": [f"16|{SEAT_A}"]})

    page = client.get(catalog_link(client.get(f"/admin/plans/{plan_id}/edit").text))

    assert page.status_code == 200
    assert "修改计划" in page.text and "创建计划" not in page.text
    assert f'href="/admin/plans/{plan_id}/edit"' in page.text


def test_the_catalog_action_row_never_renders_an_empty_plan_id(admin_client):
    """``return=plan_edit`` 但没带（或带错）编号时，不许拼出 ``/admin/plans/None/edit``。"""
    app, client = admin_client
    seed_catalog(app)
    post(client, "/admin/catalog/picks", {"region": "16", "seat": [f"16|{SEAT_A}"]})

    page = client.get("/admin/catalog", params={"region": "16", "return": "plan_edit"})

    assert page.status_code == 200
    assert "/admin/plans/None/edit" not in page.text
    assert "创建计划" in page.text


def test_the_move_buttons_are_disabled_at_both_ends(admin_client):
    """上移/下移按钮渲染出来，且首尾两端各自禁用，省掉一次必然空操作的提交。"""
    app, client = admin_client
    seed_catalog(app)
    _pick_three_seats(app, client)

    html = client.get("/admin/catalog", params={"region": "16"}).text

    assert html.count('name="direction" value="up"') == 3
    assert html.count('name="direction" value="down"') == 3
    assert len(re.findall(r"<button[^>]*\bdisabled\b", html)) == 2, \
        "只有第一个座位的「上移」和最后一个座位的「下移」该被禁用"


def test_removing_a_pick_still_works_after_the_ordering_buttons_appear(admin_client):
    """新增排序按钮不能碰坏既有的「移出」。"""
    app, client = admin_client
    seed_catalog(app)
    _pick_three_seats(app, client)

    resp = post(client, "/admin/catalog/picks/remove",
                {"region": "16", "regionid": "16", "seat_name": SEAT_B})

    assert resp.status_code == 303
    assert [p["seat_name"] for p in repo.list_picks(app.state.db)] == [SEAT_A, SEAT_C]


def test_reordering_across_regions_keeps_every_pick_and_its_own_region(admin_client):
    """跨区域草稿调序：整组重写不得丢掉任何一条，也不得改写区域。"""
    app, client = admin_client
    seed_catalog(app)
    seed_catalog(app, regionid=35, seats=[
        {"seat_id": "9", "seat_name": "五层东书库南区 01桌 A座"},
    ])
    post(client, "/admin/catalog/picks", {"region": "16", "seat": [f"16|{SEAT_A}"]})
    post(client, "/admin/catalog/picks", {
        "region": "35", "seat": ["35|五层东书库南区 01桌 A座"],
    })

    post(client, "/admin/catalog/picks/move", {
        "region": "35", "regionid": "35", "seat_name": "五层东书库南区 01桌 A座",
        "direction": "up",
    })

    picks = [(p["regionid"], p["seat_name"]) for p in repo.list_picks(app.state.db)]
    assert picks == [(35, "五层东书库南区 01桌 A座"), (16, SEAT_A)]


def test_a_multi_region_draft_is_still_not_rewritten_by_reordering(admin_client):
    """任务一轮修的互斥提示：调序只是换顺序，仍不得改写计划里的 regionid。"""
    app, client = admin_client
    seed_catalog(app)
    seed_catalog(app, regionid=35, seats=[
        {"seat_id": "9", "seat_name": "五层东书库南区 01桌 A座"},
    ])
    before = region_input_value(client.get("/admin/plans/new").text)
    post(client, "/admin/catalog/picks", {"region": "16", "seat": [f"16|{SEAT_A}"]})
    post(client, "/admin/catalog/picks", {
        "region": "35", "seat": ["35|五层东书库南区 01桌 A座"],
    })

    post(client, "/admin/catalog/picks/move", {
        "region": "35", "regionid": "35", "seat_name": "五层东书库南区 01桌 A座",
        "direction": "up",
    })

    form = client.get("/admin/plans/new")
    assert region_input_value(form.text) == before, "调序不得改写计划里的区域 ID"
    assert "未改写区域 ID" in form.text and "已绑定为" not in form.text


def test_browsing_and_ordering_produce_no_library_request(admin_client):
    """浏览、挑选、调序全程只读本地库：查询替身一次都不许被调用。

    （刷新目录是另一个显式按钮，仍然会真的去查——这里不碰它。）
    """
    from haut_seat.services.catalog_service import CatalogService

    app, client = admin_client
    seed_catalog(app)

    calls: list = []

    def spy_query(*args, **kwargs):
        calls.append(args)
        raise AssertionError("浏览/挑选/调序不允许发起任何图书馆查询")

    app.state.catalog_service = CatalogService(app.state.db, app.state.settings, query_fn=spy_query)

    assert client.get("/admin/catalog").status_code == 200
    assert client.get("/admin/catalog", params={"region": "16", "floor": "十层"}).status_code == 200
    post(client, "/admin/catalog/picks", {
        "region": "16", "seat": [f"16|{SEAT_A}", f"16|{SEAT_C}"],
    })
    post(client, "/admin/catalog/picks/move",
         {"region": "16", "regionid": "16", "seat_name": SEAT_C, "direction": "up"})

    assert calls == [], "页面上的浏览与调序不得触发任何查询"


def test_anonymous_visitors_cannot_reorder_the_draft(make_client):
    app, client = make_client()
    security.create_admin(app.state.db, "tester", GOOD_PASSWORD)

    assert client.post("/admin/catalog/picks/move", data={}).status_code == 401


def test_a_move_without_a_csrf_token_is_refused(admin_client):
    app, client = admin_client
    seed_catalog(app)
    _pick_three_seats(app, client)

    resp = client.post("/admin/catalog/picks/move", data={
        "region": "16", "regionid": "16", "seat_name": SEAT_C, "direction": "up",
    })

    assert resp.status_code == 400
    assert "CSRF" in resp.text
    assert [p["seat_name"] for p in repo.list_picks(app.state.db)] == [SEAT_A, SEAT_B, SEAT_C]