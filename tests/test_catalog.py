"""座位目录：推断规则、持久化、级联与失败降级（全部离线）。

覆盖用户明确要求验证的几件事：
- 目录解析与区域/楼层推断；
- 级联（区域 → 座位）与区域更名；
- 座位下线不删除、重复座位如实保留、不完整数据跳过并计数；
- 更新失败时**目录一个字节都不改**、上次成功时间保持不变；
- 目录刷新路径**绝不触碰真实预约提交接口**。
"""

from __future__ import annotations

import inspect

import pytest

from haut_seat.core.catalog import (
    derive_area_label,
    derive_floor_label,
    normalize_seat_id,
)
from haut_seat.services.catalog_service import CatalogService
from haut_seat.web import repository as repo

# 真实观测到的座位名格式（README「已确认」一节 + 日志 id=2481）：
#   '十层东书库北区 01桌 E座'
AREAS = {
    "north": "十层东书库北区",
    "south": "五层东书库南区",
}


def seat(seat_id, name):
    return {"id": seat_id, "seatName": name}


def make_service(db, settings, *, by_region=None, error=None):
    """完全离线的目录服务：返回内容由测试直接指定。

    只提供 ``query_fn``——目录服务里根本不存在提交接口，结构上就不可能提交预约。
    """
    from haut_seat.models.seat import LayoutQueryResult

    def query_fn(settings_, regionid, start, end, page_num, page_size):
        if error is not None:
            raise error
        rows = (by_region or {}).get(int(regionid), [])
        return LayoutQueryResult.from_response({"layout": None, "seatList": list(rows)})

    return CatalogService(db, settings, query_fn=query_fn)


def names_of(db, regionid, *, active_only=True):
    return [
        s["seat_name"]
        for s in repo.list_catalog_seats(db, regionid=regionid, active_only=active_only)
    ]


# ============================================================ 推断规则（纯函数）


def test_area_label_comes_from_the_real_seat_name_format():
    assert derive_area_label("十层东书库北区 01桌 E座") == AREAS["north"]
    assert derive_area_label("五层东书库南区 01桌 A座") == AREAS["south"]


def test_area_label_needs_a_table_number_to_split_on():
    """没有「N桌」就没法切分——宁可返回空，也不猜。"""
    assert derive_area_label("A-001") == ""
    assert derive_area_label("十层东书库北区") == ""
    assert derive_area_label("") == ""
    assert derive_area_label(None) == ""


def test_floor_label_is_parsed_from_the_head_of_the_seat_name():
    assert derive_floor_label("十层东书库北区 01桌 E座") == "十层"
    assert derive_floor_label("10层东书库北区 01桌 E座") == "10层"


def test_floor_label_is_empty_when_not_present():
    assert derive_floor_label("东书库北区 01桌 E座") == ""
    assert derive_floor_label("A-001") == ""
    assert derive_floor_label(None) == ""


def test_floor_label_does_not_mistake_the_table_number_for_a_floor():
    """「01桌」里的数字不能被当成楼层——只从开头匹配。"""
    assert derive_floor_label("东书库北区 01桌 E座") == ""


@pytest.mark.parametrize("raw", [2481, "2481", " 2481 "])
def test_seat_id_is_normalised_so_one_seat_is_one_row(raw):
    assert normalize_seat_id(raw) == "2481"


@pytest.mark.parametrize("raw", [None, "", "   ", True, False])
def test_invalid_seat_ids_are_rejected_rather_than_guessed(raw):
    assert normalize_seat_id(raw) == ""


def test_cli_region_label_keeps_working_after_the_extraction():
    """``__main__._derive_region_label`` 改为委托后行为必须完全一致。"""
    from haut_seat.__main__ import _derive_region_label
    from haut_seat.models.seat import Seat

    seats = [Seat(seat(1, "五层东书库南区 01桌 A座"))]
    assert _derive_region_label(seats) == AREAS["south"]
    assert _derive_region_label([]) is None
    assert _derive_region_label([Seat(seat(1, "A-001"))]) is None


# ============================================================ 刷新与持久化


def test_refresh_stores_regions_seats_and_derived_labels(web_db, web_settings):
    service = make_service(web_db, web_settings, by_region={
        16: [seat(2481, "十层东书库北区 01桌 E座"), seat(2482, "十层东书库北区 01桌 F座")],
    })

    result = service.refresh([16])

    assert result["ok"] is True and result["status"] == "ok"
    region = repo.get_catalog_region(web_db, 16)
    assert region["region_label"] == AREAS["north"]
    assert region["label_source"] == "derived"
    assert region["seat_count"] == 2
    entries = repo.list_catalog_seats(web_db, regionid=16)
    assert entries[0]["seat_id"] == "2481"
    assert entries[0]["floor_label"] == "十层"
    assert entries[0]["area_label"] == AREAS["north"]
    state = repo.get_catalog_state(web_db)
    assert state["last_status"] == "ok" and state["last_ok_at"]


def test_the_first_refresh_falls_back_to_the_configured_region(web_db, web_settings):
    """目录为空时没有区域可选，只能用配置里的 regionid 起步。"""
    service = make_service(web_db, web_settings, by_region={
        int(web_settings.regionid): [seat(1, "十层东书库北区 01桌 A座")],
    })
    assert service.planned_regionids() == [int(web_settings.regionid)]
    assert service.refresh()["status"] == "ok"


def test_floors_are_listed_for_filtering(web_db, web_settings):
    service = make_service(web_db, web_settings, by_region={
        16: [seat(1, "十层东书库北区 01桌 A座"), seat(2, "五层东书库北区 01桌 A座")],
    })
    service.refresh([16])
    floors = {f["floor_label"]: f["seat_count"] for f in repo.list_catalog_floors(web_db, 16)}
    assert floors == {"十层": 1, "五层": 1}


def test_a_missing_seat_is_retired_not_deleted(web_db, web_settings):
    """座位下线必须是 active=0——收藏与草稿按名称引用了它，删掉会让选择凭空消失。"""
    first = make_service(web_db, web_settings, by_region={
        16: [seat(1, "十层东书库北区 01桌 A座"), seat(2, "十层东书库北区 01桌 B座")],
    })
    first.refresh([16])

    second = make_service(web_db, web_settings, by_region={
        16: [seat(1, "十层东书库北区 01桌 A座")],
    })
    assert second.refresh([16])["results"][0]["seat_count"] == 1

    assert names_of(web_db, 16) == ["十层东书库北区 01桌 A座"]
    assert names_of(web_db, 16, active_only=False) == [
        "十层东书库北区 01桌 A座", "十层东书库北区 01桌 B座",
    ]
    assert repo.get_catalog_region(web_db, 16)["retired_count"] == 1


def test_a_retired_seat_reappearing_comes_back_online(web_db, web_settings):
    make_service(web_db, web_settings, by_region={16: [seat(1, "十层东书库北区 01桌 A座")]}).refresh([16])
    make_service(web_db, web_settings, by_region={16: [seat(2, "十层东书库北区 01桌 B座")]}).refresh([16])
    result = make_service(
        web_db, web_settings, by_region={16: [seat(1, "十层东书库北区 01桌 A座")]}
    ).refresh([16])

    assert result["results"][0]["seat_count"] == 1
    assert names_of(web_db, 16) == ["十层东书库北区 01桌 A座"]
    assert repo.get_catalog_region(web_db, 16)["retired_count"] == 1


# ============================================================ 失败降级


def test_a_failed_refresh_leaves_the_catalog_untouched(web_db, web_settings):
    """核心承诺：失败时一个字节都不写，上次成功时间保持不变。"""
    make_service(web_db, web_settings, by_region={
        16: [seat(1, "十层东书库北区 01桌 A座")],
    }).refresh([16])
    before_state = repo.get_catalog_state(web_db)
    before_seats = names_of(web_db, 16, active_only=False)

    failing = make_service(web_db, web_settings, error=RuntimeError("网络炸了"))
    result = failing.refresh([16])

    assert result["status"] == "failed" and result["ok"] is False
    assert names_of(web_db, 16, active_only=False) == before_seats
    after = repo.get_catalog_state(web_db)
    assert after["last_ok_at"] == before_state["last_ok_at"], "失败不得推进上次成功时间"
    assert after["last_attempt_at"], "失败必须记录本次尝试时间"
    assert "网络" not in after["last_message"] or "查询失败" in after["last_message"]


def test_an_exception_before_the_first_success_keeps_last_ok_empty(web_db, web_settings):
    make_service(web_db, web_settings, error=RuntimeError("boom")).refresh([16])
    state = repo.get_catalog_state(web_db)
    assert state["last_status"] == "failed"
    assert state["last_ok_at"] is None


def test_a_partial_refresh_only_touches_the_regions_that_succeeded(web_db, web_settings):
    good = make_service(web_db, web_settings, by_region={
        16: [seat(1, "十层东书库北区 01桌 A座")],
        35: [seat(9, "五层东书库南区 01桌 A座")],
    })
    good.refresh([16, 35])
    assert names_of(web_db, 35) == ["五层东书库南区 01桌 A座"]

    # 第二次只有 16 成功，且少了座位；35 没有返回 → 必须完全不动
    def flaky(settings_, regionid, start, end, page_num, page_size):
        from haut_seat.models.seat import LayoutQueryResult

        if int(regionid) == 35:
            raise RuntimeError("区域 35 挂了")
        return LayoutQueryResult.from_response(
            {"layout": None, "seatList": [seat(2, "十层东书库北区 01桌 B座")]}
        )

    result = CatalogService(web_db, web_settings, query_fn=flaky).refresh([16, 35])

    assert result["status"] == "partial"
    assert names_of(web_db, 16) == ["十层东书库北区 01桌 B座"]
    assert names_of(web_db, 35) == ["五层东书库南区 01桌 A座"], "失败区域必须保持 1 个座位且未下线"
    assert repo.get_catalog_region(web_db, 35)["seat_count"] == 1


def test_an_empty_response_is_suspicious_and_does_not_retire_everything(web_db, web_settings):
    """一次空响应若照写，会把整个区域的座位全部误判为「已下线」。"""
    make_service(web_db, web_settings, by_region={
        16: [seat(1, "十层东书库北区 01桌 A座"), seat(2, "十层东书库北区 01桌 B座")],
    }).refresh([16])

    result = make_service(web_db, web_settings, by_region={16: []}).refresh([16])

    assert result["status"] == "failed"
    assert len(names_of(web_db, 16)) == 2, "空响应绝不能触发全量下线"
    assert "空" in repo.get_catalog_state(web_db)["last_message"]


def test_an_empty_region_list_is_reported_instead_of_silently_doing_nothing(web_db, web_settings):
    service = make_service(web_db, web_settings)
    result = service.refresh([])
    assert result["ok"] is False
    assert "添加区域" in result["message"]


# ============================================================ 异常数据


def test_seats_without_an_id_or_a_name_are_skipped_and_counted(web_db, web_settings):
    service = make_service(web_db, web_settings, by_region={
        16: [
            seat(1, "十层东书库北区 01桌 A座"),
            {"id": None, "seatName": "十层东书库北区 01桌 B座"},
            {"id": 3, "seatName": "   "},
            {"seatName": "十层东书库北区 01桌 D座"},
        ],
    })

    result = service.refresh([16])

    assert repo.get_catalog_region(web_db, 16)["seat_count"] == 1
    assert "跳过 3 条数据不完整" in result["message"]


def test_a_duplicated_seat_id_in_one_response_is_counted_once(web_db, web_settings):
    service = make_service(web_db, web_settings, by_region={
        16: [seat(1, "十层东书库北区 01桌 A座"), seat(1, "十层东书库北区 01桌 A座")],
    })
    result = service.refresh([16])
    assert repo.get_catalog_region(web_db, 16)["seat_count"] == 1
    assert "跳过 1 条重复座位 ID" in result["message"]


def test_seats_sharing_a_name_are_both_kept_and_flagged(web_db, web_settings):
    """同名座位不能静默合并：按名称匹配的计划会同时命中，用户必须知道。"""
    service = make_service(web_db, web_settings, by_region={
        16: [seat(1, "十层东书库北区 01桌 A座"), seat(2, "十层东书库北区 01桌 A座")],
    })
    service.refresh([16])

    region = repo.get_catalog_region(web_db, 16)
    assert region["seat_count"] == 2
    assert region["duplicate_names"] == 1

    found = repo.find_seats_by_names(web_db, ["十层东书库北区 01桌 A座"])
    assert [s["seat_id"] for s in found["十层东书库北区 01桌 A座"]] == ["1", "2"]
    assert repo.find_seats_by_names(web_db, ["不存在的座位"]) == {}


# ============================================================ 区域更名


def test_a_derived_region_name_follows_the_data(web_db, web_settings):
    make_service(web_db, web_settings, by_region={16: [seat(1, "十层东书库北区 01桌 A座")]}).refresh([16])
    assert repo.get_catalog_region(web_db, 16)["region_label"] == AREAS["north"]

    result = make_service(
        web_db, web_settings, by_region={16: [seat(1, "五层东书库南区 01桌 A座")]}
    ).refresh([16])

    assert repo.get_catalog_region(web_db, 16)["region_label"] == AREAS["south"]
    assert "推断名称发生变化" in result["message"]


def test_a_manual_region_name_is_never_overwritten_by_a_refresh(web_db, web_settings):
    repo.add_catalog_region(web_db, 16)
    repo.rename_catalog_region(web_db, 16, "我最常去的南区")
    assert repo.get_catalog_region(web_db, 16)["label_source"] == "manual"

    make_service(web_db, web_settings, by_region={
        16: [seat(1, "十层东书库北区 01桌 A座")],
    }).refresh([16])

    region = repo.get_catalog_region(web_db, 16)
    assert region["region_label"] == "我最常去的南区"
    assert region["label_source"] == "manual"


def test_renaming_needs_an_existing_region(web_db, web_settings):
    service = make_service(web_db, web_settings)
    assert service.rename_region(999, "随便")["ok"] is False
    assert service.rename_region(999, "  ")["ok"] is False


def test_adding_a_region_twice_is_idempotent(web_db, web_settings):
    service = make_service(web_db, web_settings)
    assert "已添加" in service.add_region(16)["message"]
    assert "已在目录中" in service.add_region(16)["message"]
    assert len(repo.list_catalog_regions(web_db)) == 1


# ============================================================ 离线导入


def test_import_json_uses_the_same_parser_as_the_online_path(web_db, web_settings):
    service = make_service(web_db, web_settings)
    payload = (
        '{"layout": null, "seatList": ['
        '{"id": 2481, "seatName": "十层东书库北区 01桌 E座"}]}'
    )

    result = service.import_json(payload, 16)

    assert result["ok"] is True and result["seat_count"] == 1
    entry = repo.list_catalog_seats(web_db, regionid=16)[0]
    assert entry["seat_id"] == "2481"
    assert entry["floor_label"] == "十层"


def test_import_json_rejects_junk_with_a_readable_reason(web_db, web_settings):
    service = make_service(web_db, web_settings)

    assert service.import_json("", 16)["ok"] is False
    assert "JSON 解析失败" in service.import_json("{not json", 16)["message"]
    # 业务错误信封：含 code 且没有 seatList
    assert "业务错误信封" in service.import_json('{"code": 401, "msg": "未登录"}', 16)["message"]
    # 结构不对：有 seatList 但不是数组
    assert "结构不符合预期" in service.import_json('{"seatList": {}}', 16)["message"]
    # 结构对但没有可用座位
    assert service.import_json('{"seatList": []}', 16)["ok"] is False


def test_import_json_accepts_the_sanitised_sample_for_the_parse_path_only(
    web_db, web_settings, sample_layout_raw
):
    """脱敏样例只用来验证解析路径；它的占位座位名推不出区域名，这是预期的。"""
    import json

    result = make_service(web_db, web_settings).import_json(
        json.dumps(sample_layout_raw, ensure_ascii=False), 16
    )

    assert result["ok"] is True and result["seat_count"] == 3
    assert repo.get_catalog_region(web_db, 16)["region_label"] == "", "占位名推不出区域名"


# ============================================================ 收藏与待选草稿


def test_favorites_are_idempotent_and_track_whether_the_seat_still_exists(web_db, web_settings):
    make_service(web_db, web_settings, by_region={
        16: [seat(1, "十层东书库北区 01桌 A座")],
    }).refresh([16])

    assert repo.add_favorite(web_db, 16, "十层东书库北区 01桌 A座") is True
    assert repo.add_favorite(web_db, 16, "十层东书库北区 01桌 A座") is False, "重复收藏不应报错"
    assert repo.add_favorite(web_db, 16, "本来就不存在的座位") is True

    favorites = {f["seat_name"]: f for f in repo.list_favorites(web_db)}
    assert favorites["十层东书库北区 01桌 A座"]["known"] is True
    assert favorites["本来就不存在的座位"]["known"] is False

    assert repo.remove_favorite(web_db, 16, "十层东书库北区 01桌 A座") is True
    assert repo.remove_favorite(web_db, 16, "十层东书库北区 01桌 A座") is False


def test_picks_keep_the_order_they_were_chosen_in(web_db):
    repo.replace_picks(web_db, [(16, "B座"), (16, "A座"), (35, "C座"), (16, "A座")])

    picks = repo.list_picks(web_db)
    assert [p["seat_name"] for p in picks] == ["B座", "A座", "C座"], "顺序即优先级，且去重"
    assert repo.clear_picks(web_db) == 3
    assert repo.list_picks(web_db) == []


# ============================================================ 安全边界


def test_the_catalog_service_cannot_submit_a_booking():
    """结构上的保证：目录服务里根本没有真实提交接口。

    用 AST 检查而不是 grep 源码——注释与文档字符串里提到 ``submit_online``
    是解释「本模块不做什么」，不算违规；真正的判据是**没有任何引用**。
    """
    import ast

    from haut_seat.services import catalog_service as module

    tree = ast.parse(inspect.getsource(module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imported.update(alias.name for alias in node.names)
    assert not [n for n in imported if "submit" in n.lower()], imported

    # 提交接口的路径字符串也不该出现在**可执行代码**里（连拼错的尝试都不给）。
    # 文档字符串被排除在外：它们解释「本模块不做什么」，属于必要的说明。
    docstrings = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef))
    }
    literals = {
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and n.value not in docstrings
    }
    offenders = [
        s for s in literals
        if "submit" in s.lower() or "addbooking" in s.lower()
    ]
    assert not offenders, offenders
    for name in dir(CatalogService):
        assert "submit" not in name.lower()


def test_refreshing_never_writes_to_the_plan_table(web_db, web_settings):
    """目录与计划彻底解耦：刷新不得改动任何计划行，也不得新增计划。"""
    from haut_seat.web.repository import PlanInput, create_plan

    plan_id = create_plan(web_db, PlanInput(
        name="既有计划", run_date="2026-10-17", repeat_rule="once", trigger_at="07:00:00",
        book_start="2026-10-17 08:00:00", book_end="2026-10-17 12:00:00", regionid=16,
        preferred_seats=["十层东书库北区 01桌 E座"], preferred_prefixes=[],
        confirmed_bookable_states=[["2", 1, False]], max_attempts_per_seat=3, note="",
    ))
    before = repo.get_plan(web_db, plan_id)

    make_service(web_db, web_settings, by_region={
        16: [seat(2481, "十层东书库北区 01桌 E座"), seat(2482, "十层东书库北区 02桌 B座")],
    }).refresh([16])

    after = repo.get_plan(web_db, plan_id)
    assert before == after, "刷新目录不得改动既有计划"
    assert len(repo.list_plans(web_db)) == 1


def test_the_plan_table_columns_are_unchanged(web_db):
    """防手滑：本仓库没有迁移机制，给 plan 加列会让所有老库在 SELECT 时崩掉。"""
    columns = {
        r["name"] for r in web_db.query_all("PRAGMA table_info(plan)")
    }
    assert columns == {
        "id", "name", "enabled", "status", "run_date", "repeat_rule", "trigger_at",
        "book_start", "book_end", "regionid", "preferred_seats", "preferred_prefixes",
        "confirmed_bookable_states", "max_attempts_per_seat", "note", "pause_requested",
        "stop_requested", "unknown_ack_at", "last_run_id", "created_at", "updated_at",
    }


# =============== 验收回归：本地导入与在线更新分成两个时间（2026-10-18）

_OK_PAYLOAD = (
    '{"layout": null, "seatList": ['
    '{"id": 2481, "seatName": "十层东书库北区 01桌 E座"}]}'
)


def test_import_json_records_the_import_time_without_pretending_to_be_a_refresh(
    web_db, web_settings
):
    """导入成功要留下自己的时间，且**不能**写在线刷新状态。"""
    service = make_service(web_db, web_settings)

    result = service.import_json(_OK_PAYLOAD, 16)

    assert result["ok"] is True
    imported = repo.get_catalog_import_state(web_db)
    assert imported["last_status"] == "ok" and imported["last_import_at"]
    assert imported["last_regionid"] == 16 and imported["last_seat_count"] == 1
    online = repo.get_catalog_state(web_db)
    assert online["last_status"] == "never" and online["last_ok_at"] is None, (
        "本地导入绝不能被写成在线刷新成功"
    )


def test_an_online_refresh_does_not_touch_the_import_state(web_db, web_settings):
    """反过来也一样：在线刷新只动 catalog_state。"""
    make_service(web_db, web_settings, by_region={
        16: [seat(2481, "十层东书库北区 01桌 E座")],
    }).refresh([16])

    imported = repo.get_catalog_import_state(web_db)
    assert imported["last_status"] == "never" and imported["last_import_at"] is None


def test_a_failed_import_does_not_advance_the_import_time(web_db, web_settings):
    service = make_service(web_db, web_settings)
    service.import_json(_OK_PAYLOAD, 16)
    before = repo.get_catalog_import_state(web_db)

    result = service.import_json("{坏掉的", 16)

    assert result["ok"] is False
    after = repo.get_catalog_import_state(web_db)
    assert after["last_import_at"] == before["last_import_at"], "失败不得推进上次成功导入时间"
    assert after["last_attempt_at"] and after["last_status"] == "failed"


def test_a_failed_first_import_leaves_the_import_time_empty(web_db, web_settings):
    service = make_service(web_db, web_settings)

    service.import_json('{"seatList": []}', 16)

    state = repo.get_catalog_import_state(web_db)
    assert state["last_import_at"] is None
    assert state["last_attempt_at"] and state["last_status"] == "failed"


def test_every_import_failure_path_records_a_failed_attempt(web_db, web_settings):
    """五种失败分支都要记账，不能只覆盖解析失败那一条。"""
    service = make_service(web_db, web_settings)
    service.import_json(_OK_PAYLOAD, 16)
    success_time = repo.get_catalog_import_state(web_db)["last_import_at"]

    for payload in ("", "{not json", '{"code": 401, "msg": "未登录"}',
                    '{"seatList": {}}', '{"seatList": []}'):
        assert service.import_json(payload, 16)["ok"] is False, payload
        state = repo.get_catalog_import_state(web_db)
        assert state["last_status"] == "failed", payload
        assert state["last_import_at"] == success_time, payload


def test_a_failed_import_leaves_the_catalog_untouched(web_db, web_settings):
    service = make_service(web_db, web_settings)
    service.import_json(_OK_PAYLOAD, 16)
    before = names_of(web_db, 16, active_only=False)

    service.import_json('{"seatList": []}', 16)

    assert names_of(web_db, 16, active_only=False) == before, "失败的导入不得破坏旧目录"


def test_set_catalog_import_state_keeps_the_time_when_not_ok(web_db):
    """直连 repo 的回归：ok=False 不得清掉上次成功导入时间。"""
    repo.set_catalog_import_state(web_db, status="ok", message="导入成功",
                                  regionid=16, seat_count=1, ok=True)
    first = repo.get_catalog_import_state(web_db)["last_import_at"]
    assert first

    repo.set_catalog_import_state(web_db, status="failed", message="坏掉了", ok=False)

    state = repo.get_catalog_import_state(web_db)
    assert state["last_import_at"] == first and state["last_status"] == "failed"


def test_get_catalog_import_state_defaults_when_the_row_is_missing(web_db):
    """老库/未初始化库读不到行时给安全默认值，而不是抛异常。"""
    with web_db.tx() as conn:
        conn.execute("DELETE FROM catalog_import_state WHERE id = 1")

    assert repo.get_catalog_import_state(web_db) == {
        "last_import_at": None, "last_attempt_at": None, "last_status": "never",
        "last_message": "", "last_regionid": 0, "last_seat_count": 0,
    }


def test_the_import_state_message_is_redacted(web_db):
    token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abcdefghijklmnop.qrstuvwx"

    repo.set_catalog_import_state(
        web_db, status="failed",
        message=f"失败 HTTP 500 Authorization: Bearer {token}", ok=False,
    )

    message = repo.get_catalog_import_state(web_db)["last_message"]
    assert token not in message
    assert "<redacted>" in message


def test_an_old_database_gains_the_import_state_table_without_losing_data(tmp_path):
    """老库兼容：只新增表；既有目录与「上次在线成功时间」一个字节都不动。"""
    import sqlite3

    from haut_seat.web.db import Database

    path = tmp_path / "legacy.db"
    # 刻意只建「新增这张表之前」就存在的几张表，模拟已上线运行的老库
    legacy = sqlite3.connect(path)
    legacy.executescript(
        "CREATE TABLE catalog_state (id INTEGER PRIMARY KEY CHECK (id = 1),"
        " last_ok_at TEXT, last_attempt_at TEXT,"
        " last_status TEXT NOT NULL DEFAULT 'never',"
        " last_message TEXT NOT NULL DEFAULT '',"
        " last_region_count INTEGER NOT NULL DEFAULT 0,"
        " last_seat_count INTEGER NOT NULL DEFAULT 0);"
        "CREATE TABLE seat_region (regionid INTEGER PRIMARY KEY,"
        " region_label TEXT NOT NULL DEFAULT '',"
        " label_source TEXT NOT NULL DEFAULT 'derived',"
        " first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL);"
        "CREATE TABLE seat_catalog (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " regionid INTEGER NOT NULL, seat_id TEXT NOT NULL, seat_name TEXT NOT NULL,"
        " area_label TEXT NOT NULL DEFAULT '', floor_label TEXT NOT NULL DEFAULT '',"
        " first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,"
        " active INTEGER NOT NULL DEFAULT 1, UNIQUE (regionid, seat_id));"
        "INSERT INTO catalog_state (id, last_ok_at, last_attempt_at, last_status,"
        " last_message, last_region_count, last_seat_count)"
        " VALUES (1, '2026-10-01 07:00:00', '2026-10-01 07:00:00', 'ok', '更新成功', 1, 1);"
        "INSERT INTO seat_region (regionid, region_label, label_source,"
        " first_seen_at, last_seen_at)"
        " VALUES (16, '十层东书库北区', 'derived',"
        " '2026-10-01 07:00:00', '2026-10-01 07:00:00');"
        "INSERT INTO seat_catalog (regionid, seat_id, seat_name, area_label,"
        " floor_label, first_seen_at, last_seen_at, active)"
        " VALUES (16, '2481', '十层东书库北区 01桌 E座', '十层东书库北区', '十层',"
        " '2026-10-01 07:00:00', '2026-10-01 07:00:00', 1);"
    )
    legacy.commit()
    legacy.close()

    db = Database(path)
    try:
        db.init_schema()  # 老库上必须能顺利建出新表且不抛异常

        names = {
            r["name"] for r in
            db.query_all("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "catalog_import_state" in names
        assert repo.get_catalog_state(db)["last_ok_at"] == "2026-10-01 07:00:00", (
            "老库的上次在线成功时间不得被动过"
        )
        assert names_of(db, 16, active_only=False) == ["十层东书库北区 01桌 E座"]
        state = repo.get_catalog_import_state(db)
        assert state["last_status"] == "never" and state["last_import_at"] is None
    finally:
        db.close_current_thread()


# ================== 验收回归：保留非敏感业务 code，且不泄漏其它内容


def test_describe_failure_keeps_a_non_auth_business_code():
    """非认证的业务错误要留下 code（实测 1111/404/500），但不带服务器 msg。"""
    from haut_seat.exceptions import BusinessError

    reason = CatalogService._describe_failure(BusinessError(1111, msg="只能预约1天之内的时间"))

    assert "code=1111" in reason
    assert "只能预约1天之内的时间" not in reason, "不得输出服务器 msg / 异常原文"
    assert "该区域目录未被改动" in reason


def test_describe_failure_without_a_usable_code_still_degrades_safely():
    from haut_seat.exceptions import BusinessError

    reason = CatalogService._describe_failure(BusinessError({"token": "secret-token"}))

    assert "业务错误" in reason and "该区域目录未被改动" in reason
    assert "secret-token" not in reason and "{" not in reason


def test_auth_business_codes_still_take_the_auth_branch():
    """401/403 仍由认证分支处理，不落入新的业务错误分支。"""
    from haut_seat.exceptions import BusinessError

    reason = CatalogService._describe_failure(BusinessError(401, msg="认证失败"))

    assert "认证" in reason and "更新凭据" in reason


def test_describe_failure_for_an_unknown_exception_is_unchanged():
    from haut_seat.exceptions import ResponseShapeError

    assert "查询失败（RuntimeError）" in CatalogService._describe_failure(RuntimeError("网络炸了"))
    assert "结构" in CatalogService._describe_failure(ResponseShapeError("缺少 seatList"))


@pytest.mark.parametrize("code,expected", [
    (1111, "1111"),
    (500, "500"),
    (0, "0"),
    ("401", "401"),
    ("ok_1.2:x", "ok_1.2:x"),
    ("  403  ", "403"),
    (True, ""),
    (False, ""),
    (None, ""),
    ({"token": "x"}, ""),
    (["a"], ""),
    ("a" * 40, ""),
    ("有中文", ""),
    ("", ""),
])
def test_safe_business_code_only_allows_short_identifiers(code, expected):
    from haut_seat.services.catalog_service import safe_business_code

    assert safe_business_code(code) == expected


def test_import_envelope_keeps_the_marker_and_a_safe_code(web_db, web_settings):
    service = make_service(web_db, web_settings)

    message = service.import_json('{"code": 401, "msg": "未登录"}', 16)["message"]

    assert "业务错误信封" in message and "code=401" in message
    assert "未登录" not in message, "不得输出服务器给的 msg"


def test_import_envelope_does_not_echo_an_unusable_code(web_db, web_settings):
    """code 形状异常时只降级，不把对象内容渲染进页面。"""
    service = make_service(web_db, web_settings)

    message = service.import_json(
        '{"code": {"token": "secret-token"}, "msg": "x"}', 16
    )["message"]

    assert "业务错误信封" in message
    assert "secret-token" not in message and "{" not in message