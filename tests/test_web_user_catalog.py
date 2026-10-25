"""用户端座位选择（``/booking/catalog``）—— 挑座位、以及**按人隔离**。

用户端的抱怨是「没有座位选择，只能手动输入 ID 和座位信息」。这里补上的能力，
与管理端的座位目录是同一批只读数据，但有三条硬边界：

1. **草稿按人隔离**：每人一份（``user_seat_pick``），别人的选择既看不到也动不了，
   管理端那份全局草稿（``seat_pick``）与用户端互不影响 —— 这是本模块的重点；
2. **目录只是填表助手**：勾选只把座位名填进计划表单，提交时 ``parse_plan`` 仍然
   只读 textarea，计划里的座位永远等于表单里的文本；
3. **能力更小**：刷新目录 / 添加区域 / 离线导入 / 区域改名 / 收藏都是管理端能力，
   用户端没有对应路由，页面上也不出现任何管理端地址。

全部离线：目录直接写进库，不发任何网络请求，也没有可调用的提交接口。
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from haut_seat.web import repository as repo
from tests.user_end import (
    USER_PASSWORD,
    create_user,
    login_user,
    page_csrf,
)

ORIGIN = "http://testserver"

#: 观测到的真实座位名格式（区域名/楼层都靠它推断）。
NORTH = "十层东书库北区"
SEAT_A = f"{NORTH} 01桌 E座"
SEAT_B = f"{NORTH} 01桌 F座"
SEAT_C = f"{NORTH} 02桌 A座"
SOUTH_SEAT = "五层东书库南区 01桌 A座"


# ------------------------------------------------------------------ 助手


def bpost(client, path, data=None, *, token=None, origin=ORIGIN):
    """用户端 POST：默认带上正确的同源 Origin 与双提交令牌。"""
    headers = {"Origin": origin} if origin else {}
    body = dict(data or {})
    if token is not None:
        body["_csrf"] = token
    return client.post(path, data=body, headers=headers)


def csrf(client, path: str = "/booking/catalog") -> str:
    return page_csrf(client, path)


def seed_catalog(db, regionid: int = 16, label: str = "", seats=None):
    """把目录直接写进库（路由层测试用，省掉网络替身往返）。"""
    from haut_seat.core.catalog import derive_area_label, derive_floor_label

    rows = seats or [
        {"seat_id": "2481", "seat_name": SEAT_A},
        {"seat_id": "2482", "seat_name": SEAT_B},
        {"seat_id": "2483", "seat_name": SEAT_C},
    ]
    for row in rows:
        row.setdefault("area_label", derive_area_label(row["seat_name"]))
        row.setdefault("floor_label", derive_floor_label(row["seat_name"]))
    repo.add_catalog_region(db, regionid)
    repo.sync_region_catalog(db, regionid, rows)
    if label:
        repo.rename_catalog_region(db, regionid, label)


def textarea_value(html: str, field: str) -> str:
    match = re.search(rf'<textarea[^>]*name="{field}"[^>]*>(.*?)</textarea>', html, re.DOTALL)
    return match.group(1) if match else ""


def selected_region(html: str) -> str:
    """计划表单里当前生效的区域 = 下拉框中选中的那一项。"""
    match = re.search(r'<option value="([^"]*)"\s+selected', html)
    return match.group(1) if match else ""


def plan_form(**overrides) -> dict:
    """一份必然通过服务端校验的用户端表单（简化的用户端表单）。"""
    from datetime import timedelta

    day = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
    data = {
        "name": "用户端预约",
        "mode": "once",
        "run_date": day,
        "trigger_at": "07:00",
        "use_start": "08:00",
        "use_end": "12:00",
        "regionid": "16",
        "regionid_manual": "",
        "preferred_seats": "",
        "preferred_prefixes": "",
        "confirmed_bookable_states": '[["2", 1, false]]',
        "max_attempts_per_seat": "3",
        "note": "",
    }
    data.update(overrides)
    return data


@pytest.fixture
def two_users(make_client, web_settings, web_db):
    """``(alice_client, alice_id, bob_client, bob_id, db)`` —— 同一库上的两个用户。

    两个客户端各自身份独立（会话在库里），用来验证"一个人的选择不会出现在
    另一个人的页面里"。
    """
    app_a, alice = make_client(settings=web_settings, db=web_db)
    app_b, bob = make_client(settings=web_settings, db=web_db)
    alice_id = create_user(web_db, "alice")
    bob_id = create_user(web_db, "bob", USER_PASSWORD)
    login_user(alice, web_db, "alice")
    login_user(bob, web_db, "bob")
    assert app_a is not None and app_b is not None
    return alice, alice_id, bob, bob_id, web_db


# ============================================================ 1. 表单能挑座位了


def test_the_user_form_offers_the_seat_catalog(booking_user):
    """计划表单不再只让人手打：有「从座位目录挑选」入口与按名字选的区域。"""
    client, db, _settings, _uid = booking_user
    seed_catalog(db, 16, label="十层东书库北区")

    page = client.get("/booking/new").text
    assert '/booking/catalog?return=plan_new' in page
    assert 'name="regionid"' in page
    assert "十层东书库北区" in page, "目录里的区域名没有出现在区域下拉框里"


def test_the_form_still_works_without_any_catalog(booking_user):
    """目录为空（还没拉取过）时，表单必须照旧可用 —— 不能变成"没有可选项的空下拉"。"""
    client, db, settings, _uid = booking_user
    page = client.get("/booking/new").text
    assert f'value="{settings.regionid}"' in page, "默认区域没有带上"
    assert "座位目录里还没有任何区域" in page
    # 空目录下手工建计划仍然走得通（区域 ID 手工填）。
    token = csrf(client, "/booking/new")
    resp = bpost(client, "/booking/new", plan_form(
        regionid="", regionid_manual="16", preferred_seats=SEAT_A,
    ), token=token)
    assert resp.status_code == 303, resp.text
    plan_id = int(re.search(r"/booking/(\d+)", resp.headers["location"]).group(1))
    assert repo.get_plan(db, plan_id)["preferred_seats"] == [SEAT_A]


def test_region_dropdown_and_manual_id_must_agree(booking_user):
    """两处区域都填且不一致 ⇒ 报错，绝不静默挑一个（与管理端同一规则）。"""
    client, db, _settings, _uid = booking_user
    seed_catalog(db, 16, label="十层东书库北区")
    token = csrf(client, "/booking/new")
    resp = bpost(client, "/booking/new",
                 plan_form(regionid="16", regionid_manual="35"), token=token)
    assert resp.status_code == 200
    assert "不一致" in resp.text


# ============================================================ 2. 挑完自动填表


def test_picking_seats_fills_the_form_and_binds_the_region(booking_user):
    client, db, _settings, _uid = booking_user
    seed_catalog(db, 16, label="十层东书库北区")
    token = page_csrf(client, "/booking/catalog?region=16")

    resp = bpost(client, "/booking/catalog/picks",
                 {"region": "16", "seat": [f"16|{SEAT_A}", f"16|{SEAT_B}"]}, token=token)
    assert resp.status_code == 303, resp.text

    page = client.get("/booking/new").text
    assert textarea_value(page, "preferred_seats").splitlines() == [SEAT_A, SEAT_B]
    assert selected_region(page) == "16", "草稿来自单一区域时应自动绑定该区域"


def test_picks_from_several_regions_do_not_rewrite_the_region(booking_user):
    """跨区域挑选时不改写区域 ID —— 计划只有一个区域，猜一个等于骗人。"""
    client, db, _settings, _uid = booking_user
    seed_catalog(db, 16, label="十层东书库北区")
    seed_catalog(db, 35, label="五层东书库南区", seats=[
        {"seat_id": "9", "seat_name": SOUTH_SEAT},
    ])
    token = csrf(client)
    bpost(client, "/booking/catalog/picks", {"region": "16", "seat": [f"16|{SEAT_A}"]}, token=token)
    bpost(client, "/booking/catalog/picks", {"region": "35", "seat": [f"35|{SOUTH_SEAT}"]}, token=token)

    page = client.get("/booking/new").text
    assert textarea_value(page, "preferred_seats").splitlines() == [SEAT_A, SOUTH_SEAT]
    assert selected_region(page) == "16", "多区域时区域应保持原值（默认 16），不得被改写"
    assert "来自多个区域" in page


def test_a_filtered_view_only_replaces_the_seats_it_shows(booking_user):
    """带筛选时，页面上没出现的座位不能被当成「取消勾选」。"""
    client, db, _settings, _uid = booking_user
    seed_catalog(db, 16, seats=[
        {"seat_id": "1", "seat_name": f"{NORTH} 01桌 A座"},
        {"seat_id": "2", "seat_name": "五层东书库北区 01桌 B座"},
    ])
    token = csrf(client)
    bpost(client, "/booking/catalog/picks",
          {"region": "16", "seat": ["16|五层东书库北区 01桌 B座"]}, token=token)
    bpost(client, "/booking/catalog/picks", {"region": "16", "floor": "十层"}, token=token)

    picks = repo.list_user_picks(db, _uid)
    assert [p["seat_name"] for p in picks] == ["五层东书库北区 01桌 B座"]


def test_the_catalog_link_carries_the_plan_when_editing(booking_user):
    """编辑时会带着计划走：回来还是这条计划，而不是又新建一条。"""
    client, db, _settings, _uid = booking_user
    seed_catalog(db, 16, label="十层东书库北区")
    token = csrf(client, "/booking/new")
    created = bpost(client, "/booking/new", plan_form(preferred_seats=SEAT_A), token=token)
    plan_id = int(re.search(r"/booking/(\d+)", created.headers["location"]).group(1))

    page = client.get(f"/booking/{plan_id}/edit").text
    assert f"/booking/catalog?return=plan_edit&amp;plan_id={plan_id}" in page

    # 计划原有座位在前，草稿按顺序追加在后，且回来时回到这条计划。
    back = bpost(client, "/booking/catalog/picks", {
        "region": "16", "seat": [f"16|{SEAT_B}"],
        "return": "plan_edit", "plan_id": str(plan_id),
    }, token=page_csrf(client, f"/booking/catalog?region=16&return=plan_edit&plan_id={plan_id}"))
    assert back.headers["location"].startswith(f"/booking/{plan_id}/edit"), back.headers["location"]

    edit_page = client.get(f"/booking/{plan_id}/edit").text
    assert textarea_value(edit_page, "preferred_seats").splitlines() == [SEAT_A, SEAT_B]


# ============================================================ 3. 按人隔离（核心）


def test_one_users_picks_never_appear_in_another_users_form(two_users):
    alice, alice_id, bob, bob_id, db = two_users
    seed_catalog(db, 16, label="十层东书库北区")

    token = csrf(alice)
    bpost(alice, "/booking/catalog/picks", {"region": "16", "seat": [f"16|{SEAT_A}"]}, token=token)

    assert [p["seat_name"] for p in repo.list_user_picks(db, alice_id)] == [SEAT_A]
    assert repo.list_user_picks(db, bob_id) == []

    bob_form = bob.get("/booking/new").text
    assert "01桌 E座" not in textarea_value(bob_form, "preferred_seats"), (
        "别人的待选座位出现在了我的表单里"
    )
    assert "待选座位（共" not in bob.get("/booking/catalog").text
    # 而本人那边照旧看得到
    assert textarea_value(alice.get("/booking/new").text, "preferred_seats") == SEAT_A


def test_the_admin_draft_and_the_user_drafts_never_mix(admin_client, booking_user):
    """两个方向都要挡：管理端的全局草稿不进来，用户的操作也不动它。"""
    app, admin = admin_client
    client, db, _settings, uid = booking_user
    seed_catalog(db, 16, label="十层东书库北区")

    repo.replace_picks(db, [(16, SEAT_A)])          # 管理端攒了一份草稿
    repo.replace_user_picks(db, uid, [(16, SEAT_B)])  # 用户端也攒了一份

    assert "01桌 E座" not in textarea_value(client.get("/booking/new").text, "preferred_seats"), (
        "管理端的草稿串进了用户端表单"
    )
    assert "01桌 F座" not in textarea_value(admin.get("/admin/plans/new").text, "preferred_seats"), (
        "用户端的草稿串进了管理端表单"
    )

    # 用户在目录页的操作（应用/移出/清空）不得改动管理端那份草稿。
    token = csrf(client)
    bpost(client, "/booking/catalog/picks", {"region": "16", "seat": [f"16|{SEAT_C}"]}, token=token)
    bpost(client, "/booking/catalog/picks/clear", {}, token=token)
    assert [p["seat_name"] for p in repo.list_picks(db)] == [SEAT_A], (
        "全局草稿（seat_pick）被用户端的清空动作改动了"
    )
    assert repo.list_user_picks(db, uid) == []


def test_a_user_cannot_move_or_remove_another_users_pick(two_users):
    alice, alice_id, bob, bob_id, db = two_users
    repo.replace_user_picks(db, alice_id, [(16, SEAT_A), (16, SEAT_B), (16, SEAT_C)])

    token = csrf(bob)
    moved = bpost(bob, "/booking/catalog/picks/move", {
        "regionid": "16", "seat_name": SEAT_B, "direction": "up",
    }, token=token)
    assert moved.status_code == 303
    removed = bpost(bob, "/booking/catalog/picks/remove", {
        "regionid": "16", "seat_name": SEAT_A,
    }, token=token)
    assert removed.status_code == 303
    bpost(bob, "/booking/catalog/picks/clear", {}, token=token)

    assert [p["seat_name"] for p in repo.list_user_picks(db, alice_id)] == [SEAT_A, SEAT_B, SEAT_C], (
        "别人动到了我的待选座位"
    )


def test_saving_a_plan_clears_only_the_owners_draft(two_users):
    alice, alice_id, bob, bob_id, db = two_users
    seed_catalog(db, 16, label="十层东书库北区")
    repo.replace_user_picks(db, alice_id, [(16, SEAT_A)])
    repo.replace_user_picks(db, bob_id, [(16, SEAT_B)])

    created = bpost(alice, "/booking/new", plan_form(preferred_seats=SEAT_A), token=csrf(alice, "/booking/new"))
    assert created.status_code == 303, created.text

    assert repo.list_user_picks(db, alice_id) == [], "自己的草稿应在保存后清空"
    assert [p["seat_name"] for p in repo.list_user_picks(db, bob_id)] == [SEAT_B], (
        "清空自己的草稿时动到了别人的"
    )


def test_a_foreign_plan_id_is_not_used_as_a_return_target(two_users):
    """``return=plan_edit&plan_id=别人的`` 不得把页面指向别人的计划。

    那条 ``plan_id`` 来自查询串（客户端可以随便写）。认出「不是本人的」之后
    就当作没带：既不指过去，也不透露"这个编号存在但不是你的"。
    """
    alice, alice_id, bob, bob_id, db = two_users
    seed_catalog(db, 16, label="十层东书库北区")
    created = bpost(alice, "/booking/new", plan_form(), token=csrf(alice, "/booking/new"))
    plan_id = int(re.search(r"/booking/(\d+)", created.headers["location"]).group(1))

    page = bob.get(f"/booking/catalog?return=plan_edit&plan_id={plan_id}")
    assert page.status_code == 200
    assert f"/booking/{plan_id}/edit" not in page.text, "页面把别人的计划当成了返回目标"
    assert "创建计划" in page.text

    resp = bpost(bob, "/booking/catalog/picks", {
        "region": "16", "seat": [f"16|{SEAT_A}"],
        "return": "plan_edit", "plan_id": str(plan_id),
    }, token=csrf(bob, "/booking/catalog?return=plan_edit"))
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/booking/catalog"), resp.headers["location"]


def test_the_draft_goes_away_with_the_account(admin_client, booking_user):
    """删账号时草稿随账号级联消失 —— 不留一个"无主"的草稿行。"""
    _app, admin = admin_client
    client, db, _settings, uid = booking_user
    repo.replace_user_picks(db, uid, [(16, SEAT_A)])

    token = page_csrf(admin, "/admin/users")
    resp = admin.post("/admin/users/%d/delete" % uid, data={"_csrf": token})
    assert resp.status_code == 303, resp.text
    assert db.query_all("SELECT * FROM user_seat_pick") == []


# ============================================================ 4. 只是填表助手


def test_the_picks_never_become_a_second_source_of_truth(booking_user):
    """草稿里勾了座位，但提交的表单里没写 ⇒ 计划里就没有。

    这一条钉住「目录只是填表助手」：``parse_plan`` 只读 textarea，
    因此"我勾了什么"和"计划里是什么"之间不存在隐式通道。
    """
    client, db, _settings, uid = booking_user
    seed_catalog(db, 16, label="十层东书库北区")
    repo.replace_user_picks(db, uid, [(16, SEAT_A), (16, SEAT_B)])

    token = csrf(client, "/booking/new")
    resp = bpost(client, "/booking/new", plan_form(preferred_seats=SEAT_C), token=token)
    assert resp.status_code == 303, resp.text
    plan_id = int(re.search(r"/booking/(\d+)", resp.headers["location"]).group(1))
    assert repo.get_plan(db, plan_id)["preferred_seats"] == [SEAT_C]


def test_the_user_catalog_cannot_refresh_or_import(two_users):
    """用户端没有目录的写能力：刷新/加区域/导入/改名/收藏都不存在。"""
    from haut_seat.web.routes import router

    alice, _aid, _bob, _bid, _db = two_users
    paths = {
        (route.path, method)
        for route in router.routes
        for method in getattr(route, "methods", set()) or set()
        if route.path.startswith("/booking/catalog")
    }
    assert paths == {
        ("/booking/catalog", "GET"),
        ("/booking/catalog/picks", "POST"),
        ("/booking/catalog/picks/remove", "POST"),
        ("/booking/catalog/picks/move", "POST"),
        ("/booking/catalog/picks/clear", "POST"),
    }, "用户端的目录路由只有这几个"

    token = csrf(alice)
    for path in ("/booking/catalog/refresh", "/booking/catalog/regions",
                 "/booking/catalog/import", "/booking/catalog/favorites"):
        assert bpost(alice, path, {}, token=token).status_code == 404, path


def test_the_user_catalog_page_leaks_no_admin_route(booking_user):
    """页面上不出现任何管理端地址，也不出现收藏/运维状态这些管理端信息。"""
    client, db, _settings, _uid = booking_user
    seed_catalog(db, 16, label="十层东书库北区")
    page = client.get("/booking/catalog?region=16").text
    assert "/admin" not in page
    for word in ("更新目录", "添加区域", "离线导入", "加入收藏", "取消收藏", "在线更新"):
        assert word not in page, f"用户端目录页出现了管理端能力：{word}"


def test_the_user_catalog_searchs_across_regions(booking_user):
    """跨区域搜索与楼层筛选与管理端共用同一套语义（同一个视图装配器）。"""
    client, db, _settings, _uid = booking_user
    seed_catalog(db, 16, label="十层东书库北区")
    seed_catalog(db, 35, label="五层东书库南区", seats=[
        {"seat_id": "9", "seat_name": SOUTH_SEAT},
    ])
    page = client.get("/booking/catalog?q=南区").text
    assert SOUTH_SEAT in page
    assert SEAT_A not in page


# ============================================================ 5. 写入口的守卫


def test_the_new_writes_require_csrf_and_same_origin(booking_user):
    client, db, _settings, _uid = booking_user
    seed_catalog(db, 16, label="十层东书库北区")

    assert bpost(client, "/booking/catalog/picks", {"region": "16"}).status_code == 400
    assert bpost(client, "/booking/catalog/picks", {"region": "16"},
                 token=csrf(client), origin="http://evil.invalid").status_code == 403
    assert repo.list_user_picks(db, _uid) == []


def test_a_manifestly_bad_move_is_refused_without_changing_anything(booking_user):
    client, db, _settings, uid = booking_user
    repo.replace_user_picks(db, uid, [(16, SEAT_A)])
    token = csrf(client)

    edge = bpost(client, "/booking/catalog/picks/move", {
        "regionid": "16", "seat_name": SEAT_A, "direction": "up",
    }, token=token)
    assert edge.status_code == 303
    assert repo.list_user_picks(db, uid)[0]["seat_name"] == SEAT_A

    missing = bpost(client, "/booking/catalog/picks/move", {
        "regionid": "16", "seat_name": "并不存在的座位", "direction": "up",
    }, token=token)
    assert missing.status_code == 303
    assert [p["seat_name"] for p in repo.list_user_picks(db, uid)] == [SEAT_A]