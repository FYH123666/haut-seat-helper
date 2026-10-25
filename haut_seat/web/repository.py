"""数据访问：计划、运行记录、运行事件、认证状态、通知设置、全局开关。

约定：
- 所有写操作走 ``db.tx()``；
- 写入 run_event / auth_event / notify_event 之前统一调用 ``redact_message``，
  保证「运行记录与日志」里不出现 Cookie / Authorization / JWT / 口令 / 验证码答案；
- 计划状态变更一律经由 ``services.state`` 的纯函数判定，本层只做持久化。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any

from haut_seat.logging_conf import redact_text
from haut_seat.services.state import (
    PLAN_STATUS_LABELS,
    ErrorCategory,
    PlanStatus,
    RunPhase,
    RunStatus,
    apply_pause,
    apply_resume,
    apply_stop,
)
from haut_seat.web.validation import PlanInput

_FMT = "%Y-%m-%d %H:%M:%S"

# 事件消息额外兜底：JWT 形状、长 base64/hex 串、Bearer 值、验证码答案键。
_JWT_LIKE = re.compile(r"\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}(?:\.[A-Za-z0-9_\-]+)?")
_LONG_BLOB = re.compile(r"\b[A-Za-z0-9_\-]{48,}\b")
_BEARER = re.compile(r"(?i)\bBearer\s+\S+")
_CODE_ANSWER = re.compile(r"(?i)\b(captcha|verifycode|checkcode|code_answer)\s*[:=]\s*\S+")
_MAX_MESSAGE_LEN = 500


def redact_message(text: Any) -> str:
    """把任意事件/错误文本脱敏并截断，供入库与页面展示。"""
    if text is None:
        return ""
    s = str(text)
    s = _BEARER.sub("Bearer <redacted>", s)
    s = _JWT_LIKE.sub("<redacted-jwt>", s)
    s = _CODE_ANSWER.sub(r"\1: <redacted>", s)
    s = redact_text(s)
    s = _LONG_BLOB.sub("<redacted>", s)
    s = s.replace("\x00", "")
    if len(s) > _MAX_MESSAGE_LEN:
        s = s[:_MAX_MESSAGE_LEN] + f"…(截断 len={len(s)})"
    return s


def now_text(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime(_FMT)


def _loads(text: Any, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except (json.JSONDecodeError, TypeError):
        return default


# ------------------------------------------------------------------ 全局设置


def get_app_setting(db) -> dict:
    row = db.query_one("SELECT * FROM app_setting WHERE id = 1")
    if row is None:
        return {"global_pause": 1, "scheduler_enabled": 1, "updated_at": ""}
    return {
        "global_pause": int(row["global_pause"]),
        "scheduler_enabled": int(row["scheduler_enabled"]),
        "updated_at": row["updated_at"],
    }


def set_app_setting(
    db,
    *,
    global_pause: bool | None = None,
    scheduler_enabled: bool | None = None,
    now: datetime | None = None,
) -> None:
    current = get_app_setting(db)
    gp = current["global_pause"] if global_pause is None else int(bool(global_pause))
    se = current["scheduler_enabled"] if scheduler_enabled is None else int(bool(scheduler_enabled))
    with db.tx() as conn:
        conn.execute(
            "UPDATE app_setting SET global_pause = ?, scheduler_enabled = ?, updated_at = ?"
            " WHERE id = 1",
            (gp, se, now_text(now)),
        )


# ------------------------------------------------------------------ 座位目录
#
# 目录只记录「服务器返回过这个座位」，**不代表它当前可预约**。
# 可预约与否仍由 confirmed_bookable_states + 运行时的实时查询决定。


def _like_pattern(text: str) -> str:
    """把用户输入转成 LIKE 模式串，转义 ``%`` / ``_`` / ``\\``。"""
    escaped = str(text).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def get_catalog_state(db) -> dict:
    row = db.query_one("SELECT * FROM catalog_state WHERE id = 1")
    if row is None:
        return {
            "last_ok_at": None, "last_attempt_at": None, "last_status": "never",
            "last_message": "", "last_region_count": 0, "last_seat_count": 0,
        }
    return {
        "last_ok_at": row["last_ok_at"],
        "last_attempt_at": row["last_attempt_at"],
        "last_status": row["last_status"],
        "last_message": row["last_message"],
        "last_region_count": int(row["last_region_count"]),
        "last_seat_count": int(row["last_seat_count"]),
    }


def set_catalog_state(
    db,
    *,
    status: str,
    message: str = "",
    region_count: int = 0,
    seat_count: int = 0,
    ok: bool = False,
    now: datetime | None = None,
) -> None:
    """记录一次目录更新的结果。

    ``ok=True`` 才推进 ``last_ok_at``（「上次成功时间」）；失败时该字段**保持不动**，
    界面因此能同时显示「上次成功」与「本次失败原因」。
    消息一律脱敏后入库。
    """
    ts = now_text(now)
    safe = redact_message(message)
    with db.tx() as conn:
        conn.execute(
            "UPDATE catalog_state SET last_attempt_at = ?, last_status = ?, last_message = ?,"
            " last_region_count = ?, last_seat_count = ?,"
            " last_ok_at = COALESCE(?, last_ok_at) WHERE id = 1",
            (ts, str(status), safe, int(region_count), int(seat_count), ts if ok else None),
        )


def get_catalog_import_state(db) -> dict:
    """离线导入状态（与 ``catalog_state`` 分开：导入不是在线刷新成功）。"""
    row = db.query_one("SELECT * FROM catalog_import_state WHERE id = 1")
    if row is None:
        return {
            "last_import_at": None, "last_attempt_at": None, "last_status": "never",
            "last_message": "", "last_regionid": 0, "last_seat_count": 0,
        }
    return {
        "last_import_at": row["last_import_at"],
        "last_attempt_at": row["last_attempt_at"],
        "last_status": row["last_status"],
        "last_message": row["last_message"],
        "last_regionid": int(row["last_regionid"]),
        "last_seat_count": int(row["last_seat_count"]),
    }


def set_catalog_import_state(
    db,
    *,
    status: str,
    message: str = "",
    regionid: int = 0,
    seat_count: int = 0,
    ok: bool = False,
    now: datetime | None = None,
) -> None:
    """记录一次离线导入的结果。

    与 ``set_catalog_state`` 同一套约定：``ok=True`` 才推进 ``last_import_at``，
    失败时该字段**保持不动**（导入失败不得抹掉上次成功导入的时间）。
    """
    ts = now_text(now)
    safe = redact_message(message)
    with db.tx() as conn:
        conn.execute(
            "UPDATE catalog_import_state SET last_attempt_at = ?, last_status = ?,"
            " last_message = ?, last_regionid = ?, last_seat_count = ?,"
            " last_import_at = COALESCE(?, last_import_at) WHERE id = 1",
            (ts, str(status), safe, int(regionid), int(seat_count), ts if ok else None),
        )


def list_catalog_regions(db) -> list[dict]:
    """区域清单（含座位数、已下线数、收藏数、同名座位数）。"""
    rows = db.query_all(
        "SELECT r.regionid, r.region_label, r.label_source, r.first_seen_at, r.last_seen_at,"
        " (SELECT COUNT(*) FROM seat_catalog c"
        "   WHERE c.regionid = r.regionid AND c.active = 1) AS seat_count,"
        " (SELECT COUNT(*) FROM seat_catalog c"
        "   WHERE c.regionid = r.regionid AND c.active = 0) AS retired_count,"
        " (SELECT COUNT(*) FROM seat_favorite f WHERE f.regionid = r.regionid)"
        "   AS favorite_count,"
        " (SELECT COUNT(*) FROM (SELECT seat_name FROM seat_catalog"
        "   WHERE regionid = r.regionid AND active = 1"
        "   GROUP BY seat_name HAVING COUNT(*) > 1)) AS duplicate_names"
        " FROM seat_region r ORDER BY r.regionid"
    )
    return [
        {
            "regionid": int(r["regionid"]),
            "region_label": r["region_label"],
            "label_source": r["label_source"],
            "first_seen_at": r["first_seen_at"],
            "last_seen_at": r["last_seen_at"],
            "seat_count": int(r["seat_count"]),
            "retired_count": int(r["retired_count"]),
            "favorite_count": int(r["favorite_count"]),
            "duplicate_names": int(r["duplicate_names"]),
        }
        for r in rows
    ]


def get_catalog_region(db, regionid: int) -> dict | None:
    for region in list_catalog_regions(db):
        if region["regionid"] == int(regionid):
            return region
    return None


def add_catalog_region(
    db, regionid: int, *, label: str = "", now: datetime | None = None
) -> bool:
    """登记一个区域；已存在则不动（返回 False）。

    ``label_source='manual'`` 仅用于人工命名；自动登记时留空、由刷新时推断填充。
    """
    ts = now_text(now)
    source = "manual" if str(label).strip() else "derived"
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO seat_region"
            " (regionid, region_label, label_source, first_seen_at, last_seen_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (int(regionid), str(label).strip()[:60], source, ts, ts),
        )
        return cur.rowcount > 0


def rename_catalog_region(
    db, regionid: int, label: str, *, now: datetime | None = None
) -> None:
    """人工命名区域。

    置 ``label_source='manual'`` 后，**后续刷新永不覆盖它** —— 这是「区域更名」
    的既定处理方式：推断名随数据走，人工名由人负责。
    """
    with db.tx() as conn:
        conn.execute(
            "UPDATE seat_region SET region_label = ?, label_source = 'manual',"
            " last_seen_at = ? WHERE regionid = ?",
            (str(label).strip()[:60], now_text(now), int(regionid)),
        )


def sync_region_catalog(
    db,
    regionid: int,
    seats: list[dict],
    *,
    derived_label: str = "",
    now: datetime | None = None,
) -> dict:
    """把某个区域的一次查询结果写入目录（**单个事务**）。

    ``seats`` 是已归一化的字典列表，每项含 ``seat_id`` 与 ``seat_name``
    （推断标签由本函数按需写入）。返回值用于汇总报告：
    ``{"added", "updated", "retired", "reactivated", "relabeled"}``。

    「座位下线」的语义：本次响应里没有的座位置 ``active = 0``，**绝不删除** ——
    收藏与草稿按名称引用它，删掉会让用户的选择凭空消失。

    调用方**必须**保证只在查询成功且座位数非零时才调用本函数。
    """
    ts = now_text(now)
    result = {"added": 0, "updated": 0, "retired": 0, "reactivated": 0, "relabeled": 0}
    incoming = {str(s["seat_id"]) for s in seats}

    with db.tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seat_region"
            " (regionid, region_label, label_source, first_seen_at, last_seen_at)"
            " VALUES (?, ?, 'derived', ?, ?)",
            (int(regionid), derived_label[:60], ts, ts),
        )
        # 人工命名优先：只有仍是 derived 的区域才接受推断名
        if derived_label:
            cur = conn.execute(
                "UPDATE seat_region SET region_label = ?, last_seen_at = ?"
                " WHERE regionid = ? AND label_source = 'derived' AND region_label <> ?",
                (derived_label[:60], ts, int(regionid), derived_label[:60]),
            )
            result["relabeled"] += cur.rowcount
        conn.execute(
            "UPDATE seat_region SET last_seen_at = ? WHERE regionid = ?",
            (ts, int(regionid)),
        )

        for seat in seats:
            seat_id = str(seat["seat_id"])
            existing = conn.execute(
                "SELECT active FROM seat_catalog WHERE regionid = ? AND seat_id = ?",
                (int(regionid), seat_id),
            ).fetchone()
            if existing is None:
                result["added"] += 1
            elif int(existing["active"]) == 0:
                result["reactivated"] += 1
            else:
                result["updated"] += 1
            conn.execute(
                "INSERT INTO seat_catalog (regionid, seat_id, seat_name, area_label,"
                " floor_label, first_seen_at, last_seen_at, active)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 1)"
                " ON CONFLICT(regionid, seat_id) DO UPDATE SET"
                " seat_name = excluded.seat_name, area_label = excluded.area_label,"
                " floor_label = excluded.floor_label, last_seen_at = excluded.last_seen_at,"
                " active = 1",
                (
                    int(regionid), seat_id, str(seat["seat_name"]),
                    str(seat.get("area_label", ""))[:60],
                    str(seat.get("floor_label", ""))[:60], ts, ts,
                ),
            )

        # 下线：本次响应里没出现的座位（同一个事务内，只影响本区域）
        placeholders = ",".join("?" for _ in incoming)
        sql = (
            "UPDATE seat_catalog SET active = 0 WHERE regionid = ? AND active = 1"
        )
        params: list = [int(regionid)]
        if incoming:
            sql += f" AND seat_id NOT IN ({placeholders})"
            params.extend(sorted(incoming))
        result["retired"] = conn.execute(sql, params).rowcount

    return result


def list_catalog_floors(db, regionid: int) -> list[dict]:
    """某个区域下由座位名推断出的楼层清单（用于筛选器）。"""
    rows = db.query_all(
        "SELECT floor_label, COUNT(*) AS n FROM seat_catalog"
        " WHERE regionid = ? AND active = 1 AND floor_label <> ''"
        " GROUP BY floor_label ORDER BY floor_label",
        (int(regionid),),
    )
    return [{"floor_label": r["floor_label"], "seat_count": int(r["n"])} for r in rows]


def list_catalog_seats(
    db,
    *,
    regionid: int | None = None,
    floor: str = "",
    q: str = "",
    active_only: bool = True,
    limit: int = 500,
) -> list[dict]:
    """按区域/楼层/关键词筛选座位。``q`` 同时匹配座位名、推断区域名与楼层。

    ``floor`` 与 ``q`` 都是**推断值上的筛选**，不构成任何可预约性判断。
    """
    where: list[str] = []
    params: list = []
    if active_only:
        where.append("active = 1")
    if regionid is not None:
        where.append("regionid = ?")
        params.append(int(regionid))
    if floor:
        where.append("floor_label = ?")
        params.append(str(floor))
    if q:
        where.append(
            "(seat_name LIKE ? ESCAPE '\\' OR area_label LIKE ? ESCAPE '\\'"
            " OR floor_label LIKE ? ESCAPE '\\')"
        )
        pattern = _like_pattern(q)
        params.extend([pattern, pattern, pattern])
    sql = "SELECT * FROM seat_catalog"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY regionid, seat_name, seat_id LIMIT ?"
    params.append(int(limit))
    return [
        {
            "regionid": int(r["regionid"]),
            "seat_id": r["seat_id"],
            "seat_name": r["seat_name"],
            "area_label": r["area_label"],
            "floor_label": r["floor_label"],
            "active": int(r["active"]),
            "first_seen_at": r["first_seen_at"],
            "last_seen_at": r["last_seen_at"],
        }
        for r in db.query_all(sql, params)
    ]


def count_catalog_seats(db, *, active_only: bool = True) -> int:
    sql = "SELECT COUNT(*) AS n FROM seat_catalog"
    if active_only:
        sql += " WHERE active = 1"
    row = db.query_one(sql)
    return int(row["n"]) if row else 0


def find_seats_by_names(db, names: list[str]) -> dict[str, list[dict]]:
    """按座位名查目录（名称 → 该名称对应的所有目录项）。

    一个名称可能对应多条记录（同名不同 ID）：**如实全部返回**，由调用方提示，
    不静默挑一条。查不到的键不存在于返回的字典里。
    """
    wanted = [str(n) for n in names if str(n).strip()]
    if not wanted:
        return {}
    out: dict[str, list[dict]] = {}
    for name in wanted:
        rows = db.query_all(
            "SELECT regionid, seat_id, seat_name, active FROM seat_catalog"
            " WHERE seat_name = ? ORDER BY regionid, seat_id",
            (name,),
        )
        if rows:
            out[name] = [
                {
                    "regionid": int(r["regionid"]),
                    "seat_id": r["seat_id"],
                    "seat_name": r["seat_name"],
                    "active": int(r["active"]),
                }
                for r in rows
            ]
    return out


# ---- 收藏 ----


def list_favorites(db, *, regionid: int | None = None) -> list[dict]:
    """收藏列表；``known`` 表示该座位当前是否仍在目录中（未下线）。"""
    where = "WHERE f.regionid = ?" if regionid is not None else ""
    params: list = [int(regionid)] if regionid is not None else []
    rows = db.query_all(
        "SELECT f.id, f.regionid, f.seat_name, f.note, f.created_at,"
        " (SELECT COUNT(*) FROM seat_catalog c WHERE c.regionid = f.regionid"
        "   AND c.seat_name = f.seat_name AND c.active = 1) AS known,"
        " (SELECT r.region_label FROM seat_region r WHERE r.regionid = f.regionid)"
        "   AS region_label"
        f" FROM seat_favorite f {where} ORDER BY f.id",
        params,
    )
    return [
        {
            "id": int(r["id"]),
            "regionid": int(r["regionid"]),
            "seat_name": r["seat_name"],
            "note": r["note"],
            "created_at": r["created_at"],
            "known": int(r["known"]) > 0,
            "region_label": r["region_label"] or "",
        }
        for r in rows
    ]


def add_favorite(
    db, regionid: int, seat_name: str, *, note: str = "", now: datetime | None = None
) -> bool:
    """加收藏（幂等：已存在返回 False，不报错）。"""
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO seat_favorite (regionid, seat_name, note, created_at)"
            " VALUES (?, ?, ?, ?)",
            (int(regionid), str(seat_name)[:120], str(note)[:200], now_text(now)),
        )
        return cur.rowcount > 0


def remove_favorite(db, regionid: int, seat_name: str) -> bool:
    with db.tx() as conn:
        cur = conn.execute(
            "DELETE FROM seat_favorite WHERE regionid = ? AND seat_name = ?",
            (int(regionid), str(seat_name)[:120]),
        )
        return cur.rowcount > 0


# ---- 待选草稿（目录 → 计划表单的传递媒介）----
#
# 草稿有**两种作用域**，SQL 除表名外完全一样：
#
# - 管理端：``seat_pick``，全局唯一一份（管理端本来就只有一个操作者）；
# - 用户端：``user_seat_pick``，**每人一份** —— 两个用户的选择绝不能互相看见，
#   更不能用 ``clear_picks`` 那种"清空全部"的语义去动别人的草稿。
#
# 两种作用域收在同一处实现（``user_id is None`` 即全局），公开函数各自只差一个
# 作用域参数：同一套顺序与去重规则，不会出现"用户端那条少去重一次"这类漂移。
# 表名只可能来自本模块的两个常量，永远不是外部输入。

_ADMIN_PICK_TABLE = "seat_pick"
_USER_PICK_TABLE = "user_seat_pick"


def _pick_table(user_id: int | None) -> str:
    """草稿表名：给了 ``user_id`` 就只能查/写用户端那张表。

    这个映射是**构造性**的：调用方无法用"带用户过滤的全局表"或"不带过滤的用户表"
    去查，因为表名根本不由调用方给。
    """
    return _USER_PICK_TABLE if user_id is not None else _ADMIN_PICK_TABLE


def _list_picks(db, user_id: int | None) -> list[dict]:
    """草稿内容，按选择顺序（即优先级顺序）；``known`` 同收藏。"""
    where = " WHERE p.user_id = ?" if user_id is not None else ""
    params: list = [int(user_id)] if user_id is not None else []
    rows = db.query_all(
        "SELECT p.id, p.regionid, p.seat_name, p.created_at,"
        " (SELECT COUNT(*) FROM seat_catalog c WHERE c.regionid = p.regionid"
        "   AND c.seat_name = p.seat_name AND c.active = 1) AS known,"
        " (SELECT r.region_label FROM seat_region r WHERE r.regionid = p.regionid)"
        "   AS region_label"
        f" FROM {_pick_table(user_id)} p{where} ORDER BY p.id",
        params,
    )
    return [
        {
            "id": int(r["id"]),
            "regionid": int(r["regionid"]),
            "seat_name": r["seat_name"],
            "created_at": r["created_at"],
            "known": int(r["known"]) > 0,
            "region_label": r["region_label"] or "",
        }
        for r in rows
    ]


def _replace_picks(
    db, user_id: int | None, items: list[tuple[int, str]], *, now: datetime | None = None
) -> int:
    """整体替换草稿（先清空再按给定顺序写入），返回写入条数。

    整体替换而非增量：勾选框的提交语义就是「这就是我最终的选择」，
    顺序即优先级，必须如实保留。``user_id`` 不为空时只动**这个人**的草稿。
    """
    table = _pick_table(user_id)
    ts = now_text(now)
    seen: set[tuple[int, str]] = set()
    with db.tx() as conn:
        if user_id is None:
            conn.execute(f"DELETE FROM {table}")
        else:
            conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (int(user_id),))
        for regionid, seat_name in items:
            key = (int(regionid), str(seat_name)[:120])
            if key in seen or not key[1]:
                continue
            seen.add(key)
            if user_id is None:
                conn.execute(
                    f"INSERT INTO {table} (regionid, seat_name, created_at) VALUES (?, ?, ?)",
                    (key[0], key[1], ts),
                )
            else:
                conn.execute(
                    f"INSERT INTO {table} (user_id, regionid, seat_name, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (int(user_id), key[0], key[1], ts),
                )
    return len(seen)


def _clear_picks(db, user_id: int | None) -> int:
    table = _pick_table(user_id)
    with db.tx() as conn:
        if user_id is None:
            return conn.execute(f"DELETE FROM {table}").rowcount
        return conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (int(user_id),)).rowcount


def list_picks(db) -> list[dict]:
    """管理端的全局草稿。"""
    return _list_picks(db, None)


def replace_picks(db, items: list[tuple[int, str]], *, now: datetime | None = None) -> int:
    return _replace_picks(db, None, items, now=now)


def clear_picks(db) -> int:
    return _clear_picks(db, None)


def list_user_picks(db, user_id: int) -> list[dict]:
    """某个用户**自己**那一份草稿（与他人、与管理端都互不可见）。"""
    return _list_picks(db, int(user_id))


def replace_user_picks(
    db, user_id: int, items: list[tuple[int, str]], *, now: datetime | None = None
) -> int:
    return _replace_picks(db, int(user_id), items, now=now)


def clear_user_picks(db, user_id: int) -> int:
    return _clear_picks(db, int(user_id))


def _move_pick(
    db, user_id: int | None, regionid: int, seat_name: str, direction: str,
    *, now: datetime | None = None,
) -> dict:
    """把草稿里的一个座位在优先级顺序里上移或下移**一位**。

    顺序即优先级（``replace_picks`` 按给定顺序写入），所以「调序」就是按交换后的
    顺序重写整个草稿——不新增列、不新增表、不动计划表。

    返回 ``{"status", "ok", "message"}``，其中 ``status`` 取值：

    - ``moved``：真的换了位置；
    - ``boundary``：已经在最前/最后，**什么都不改**（无害空操作，不算错误）；
    - ``missing``：草稿里没有这个座位（可能已被别的页面移出）；
    - ``bad_direction``：方向不是 up/down。
    """
    if direction not in ("up", "down"):
        return {"status": "bad_direction", "ok": False, "message": "移动方向只能是上移或下移。"}

    picks = _list_picks(db, user_id)
    key = (str(int(regionid)), str(seat_name))
    index = next(
        (i for i, p in enumerate(picks) if (str(p["regionid"]), p["seat_name"]) == key), None
    )
    if index is None:
        return {"status": "missing", "ok": False,
                "message": f"待选座位里已经没有「{seat_name}」了，可能已在别处被移出。"}

    label = "上移" if direction == "up" else "下移"
    target = index - 1 if direction == "up" else index + 1
    if target < 0 or target >= len(picks):
        edge = "最前面" if direction == "up" else "最后面"
        return {"status": "boundary", "ok": True,
                "message": f"「{seat_name}」已经在{edge}了，顺序没有变化。"}

    items = [(p["regionid"], p["seat_name"]) for p in picks]
    items[index], items[target] = items[target], items[index]
    _replace_picks(db, user_id, items, now=now)
    return {"status": "moved", "ok": True,
            "message": f"已{label}：「{seat_name}」（现在排第 {target + 1} 位）"}


def move_pick(
    db, regionid: int, seat_name: str, direction: str, *, now: datetime | None = None
) -> dict:
    return _move_pick(db, None, regionid, seat_name, direction, now=now)


def move_user_pick(
    db, user_id: int, regionid: int, seat_name: str, direction: str,
    *, now: datetime | None = None,
) -> dict:
    """用户端的调序：只作用于**该用户自己**的草稿。"""
    return _move_pick(db, int(user_id), regionid, seat_name, direction, now=now)


# ------------------------------------------------------------------ 计划


_PLAN_FIELDS = (
    "id, name, enabled, status, run_date, repeat_rule, trigger_at, book_start, book_end,"
    " regionid, preferred_seats, preferred_prefixes, confirmed_bookable_states,"
    " max_attempts_per_seat, note, pause_requested, stop_requested, unknown_ack_at,"
    " last_run_id, created_at, updated_at"
)


def _plan_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "enabled": int(row["enabled"]),
        "status": PlanStatus(row["status"]),
        "status_label": PLAN_STATUS_LABELS.get(PlanStatus(row["status"]), row["status"]),
        "run_date": row["run_date"],
        "repeat_rule": row["repeat_rule"],
        "trigger_at": row["trigger_at"],
        "book_start": row["book_start"],
        "book_end": row["book_end"],
        "regionid": row["regionid"],
        "preferred_seats": _loads(row["preferred_seats"], []),
        "preferred_prefixes": _loads(row["preferred_prefixes"], []),
        "confirmed_bookable_states": _loads(row["confirmed_bookable_states"], []),
        "max_attempts_per_seat": int(row["max_attempts_per_seat"]),
        "note": row["note"],
        "pause_requested": int(row["pause_requested"]),
        "stop_requested": int(row["stop_requested"]),
        "unknown_ack_at": row["unknown_ack_at"],
        "last_run_id": row["last_run_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_plans(db) -> list[dict]:
    rows = db.query_all(f"SELECT {_PLAN_FIELDS} FROM plan ORDER BY id DESC")
    return [_plan_to_dict(r) for r in rows]


def get_plan(db, plan_id: int) -> dict | None:
    row = db.query_one(f"SELECT {_PLAN_FIELDS} FROM plan WHERE id = ?", (plan_id,))
    return _plan_to_dict(row) if row else None


def create_plan(
    db, data: PlanInput, *, enabled: bool = False, now: datetime | None = None
) -> int:
    """新建计划。

    **默认不武装**（``enabled=False`` → 初始状态「已暂停」）：
    启动 Web 服务或刷新页面都不会因此产生真实预约，
    必须由使用者在界面上显式「启用」才会进入待执行。
    """
    ts = now_text(now)
    status = PlanStatus.PENDING if enabled else PlanStatus.PAUSED
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO plan (name, enabled, status, run_date, repeat_rule, trigger_at,"
            " book_start, book_end, regionid, preferred_seats, preferred_prefixes,"
            " confirmed_bookable_states, max_attempts_per_seat, note, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                data.name, int(bool(enabled)), status.value, data.run_date, data.repeat_rule,
                data.trigger_at, data.book_start, data.book_end, data.regionid,
                json.dumps(data.preferred_seats, ensure_ascii=False),
                json.dumps(data.preferred_prefixes, ensure_ascii=False),
                json.dumps(data.confirmed_bookable_states, ensure_ascii=False),
                data.max_attempts_per_seat, data.note, ts, ts,
            ),
        )
        return int(cur.lastrowid)


def update_plan(db, plan_id: int, data: PlanInput, *, now: datetime | None = None) -> None:
    with db.tx() as conn:
        conn.execute(
            "UPDATE plan SET name = ?, run_date = ?, repeat_rule = ?, trigger_at = ?,"
            " book_start = ?, book_end = ?, regionid = ?, preferred_seats = ?,"
            " preferred_prefixes = ?, confirmed_bookable_states = ?,"
            " max_attempts_per_seat = ?, note = ?, updated_at = ?"
            " WHERE id = ?",
            (
                data.name, data.run_date, data.repeat_rule, data.trigger_at, data.book_start,
                data.book_end, data.regionid,
                json.dumps(data.preferred_seats, ensure_ascii=False),
                json.dumps(data.preferred_prefixes, ensure_ascii=False),
                json.dumps(data.confirmed_bookable_states, ensure_ascii=False),
                data.max_attempts_per_seat, data.note, now_text(now), plan_id,
            ),
        )


def delete_plan(db, plan_id: int) -> None:
    with db.tx() as conn:
        conn.execute("DELETE FROM plan WHERE id = ?", (plan_id,))


# ------------------------------------------------------------------ 计划来源
#
# 需求是「管理端能一眼看出哪些计划是用户在用户端提交的」。现有 plan 表**不能**加列
# （本仓库无迁移机制，加列会让老库 SELECT 报 "no such column"），因此来源单独放一张
# 侧表 plan_origin，用 plan_id 主键 + 外键级联。**没有对应行的计划按 admin 解释** ——
# 老库里的计划全都是管理端建的，这正是它们的真实来源，不是猜测。

PLAN_ORIGIN_ADMIN = "admin"
PLAN_ORIGIN_USER = "user"
PLAN_ORIGIN_LABELS = {PLAN_ORIGIN_ADMIN: "管理端", PLAN_ORIGIN_USER: "用户端"}


def set_plan_origin(db, plan_id: int, origin: str, *, now: datetime | None = None) -> None:
    """写入/覆盖计划来源。只接受已知的两种来源，其余一律按管理端处理。"""
    value = origin if origin in PLAN_ORIGIN_LABELS else PLAN_ORIGIN_ADMIN
    with db.tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO plan_origin (plan_id, origin, created_at)"
            " VALUES (?, ?, ?)",
            (plan_id, value, now_text(now)),
        )


def get_plan_origin(db, plan_id: int) -> str:
    row = db.query_one("SELECT origin FROM plan_origin WHERE plan_id = ?", (plan_id,))
    if row is None or row["origin"] not in PLAN_ORIGIN_LABELS:
        return PLAN_ORIGIN_ADMIN
    return str(row["origin"])


def plan_origins(db) -> dict[int, str]:
    """``{plan_id: 来源}``，供列表页一次性取用（避免每行一次查询）。"""
    return {
        int(r["plan_id"]): (str(r["origin"]) if r["origin"] in PLAN_ORIGIN_LABELS
                            else PLAN_ORIGIN_ADMIN)
        for r in db.query_all("SELECT plan_id, origin FROM plan_origin")
    }


def origin_label(origin: str) -> str:
    return PLAN_ORIGIN_LABELS.get(origin, PLAN_ORIGIN_LABELS[PLAN_ORIGIN_ADMIN])


def _set_plan_fields(db, plan_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.tx() as conn:
        conn.execute(
            f"UPDATE plan SET {cols} WHERE id = ?", (*fields.values(), plan_id)
        )


def set_plan_status(db, plan_id: int, status: PlanStatus, *, now: datetime | None = None) -> None:
    _set_plan_fields(db, plan_id, status=status.value, updated_at=now_text(now))


def plan_has_active_run(db, plan_id: int) -> bool:
    row = db.query_one(
        "SELECT 1 AS x FROM run WHERE plan_id = ? AND status = ? LIMIT 1",
        (plan_id, RunStatus.RUNNING.value),
    )
    return row is not None


def _apply_transition(
    db, plan_id: int, result, *, defer_flag: str, now: datetime | None = None
) -> dict:
    """把 TransitionResult 落库。deferred 的请求写成暂停/停止标记。"""
    if not result.allowed:
        return {"ok": False, "message": result.reason}
    if result.deferred:
        # 立即登记标记、状态保持不变；运行器每轮读取该标记以停止「发起新提交」，
        # 待本次运行结束后再由运行器把状态落成 paused / stopped。
        _set_plan_fields(db, plan_id, **{defer_flag: 1, "updated_at": now_text(now)})
        return {"ok": True, "message": result.reason, "deferred": True}
    _set_plan_fields(
        db, plan_id, status=result.status.value, updated_at=now_text(now),
        pause_requested=0, stop_requested=0,
    )
    return {"ok": True, "message": result.reason, "deferred": False}


def pause_plan(db, plan_id: int, *, now: datetime | None = None) -> dict:
    plan = get_plan(db, plan_id)
    if plan is None:
        return {"ok": False, "message": "计划不存在"}
    result = apply_pause(plan["status"], has_active_run=plan_has_active_run(db, plan_id))
    return _apply_transition(db, plan_id, result, defer_flag="pause_requested", now=now)


def resume_plan(
    db, plan_id: int, *, acknowledge_unknown: bool = False, now: datetime | None = None
) -> dict:
    plan = get_plan(db, plan_id)
    if plan is None:
        return {"ok": False, "message": "计划不存在"}
    result = apply_resume(
        plan["status"],
        has_active_run=plan_has_active_run(db, plan_id),
        acknowledge_unknown=acknowledge_unknown,
    )
    if not result.allowed:
        return {"ok": False, "message": result.reason}
    _set_plan_fields(
        db, plan_id,
        status=result.status.value,
        enabled=1,
        pause_requested=0,
        stop_requested=0,
        unknown_ack_at=now_text(now) if plan["status"] is PlanStatus.UNKNOWN else plan["unknown_ack_at"],
        updated_at=now_text(now),
    )
    return {"ok": True, "message": result.reason, "deferred": False}


def stop_plan(db, plan_id: int, *, now: datetime | None = None) -> dict:
    plan = get_plan(db, plan_id)
    if plan is None:
        return {"ok": False, "message": "计划不存在"}
    result = apply_stop(plan["status"], has_active_run=plan_has_active_run(db, plan_id))
    out = _apply_transition(db, plan_id, result, defer_flag="stop_requested", now=now)
    if out.get("ok") and not out.get("deferred"):
        # 停止即不再武装；否则刷新页面后调度器仍会尝试拾取
        _set_plan_fields(db, plan_id, **{"enabled": 0})
    return out


# ------------------------------------------------------------------ 运行记录


def start_run(db, plan_id: int, *, trigger: str = "scheduler", now: datetime | None = None) -> int:
    """创建一条「运行中」记录并把计划置为运行中。"""
    ts = now_text(now)
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO run (plan_id, started_at, status, trigger) VALUES (?, ?, ?, ?)",
            (plan_id, ts, RunStatus.RUNNING.value, trigger),
        )
        run_id = int(cur.lastrowid)
        conn.execute(
            "UPDATE plan SET status = ?, last_run_id = ?, pause_requested = 0,"
            " stop_requested = 0, updated_at = ? WHERE id = ?",
            (PlanStatus.RUNNING.value, run_id, ts, plan_id),
        )
    return run_id


def finish_run(
    db,
    run_id: int,
    *,
    status: RunStatus,
    category: ErrorCategory,
    stop_reason: str,
    message: str,
    exit_code: int,
    booking_id: int | None = None,
    query_count: int = 0,
    submit_count: int = 0,
    seat_switches: int = 0,
    plan_status: PlanStatus | None = None,
    now: datetime | None = None,
) -> None:
    ts = now_text(now)
    with db.tx() as conn:
        conn.execute(
            "UPDATE run SET finished_at = ?, status = ?, category = ?, stop_reason = ?,"
            " message = ?, exit_code = ?, booking_id = ?, query_count = ?, submit_count = ?,"
            " seat_switches = ? WHERE id = ?",
            (
                ts, status.value, category.value, stop_reason, redact_message(message),
                exit_code, booking_id, query_count, submit_count, seat_switches, run_id,
            ),
        )
        if plan_status is not None:
            row = conn.execute("SELECT plan_id FROM run WHERE id = ?", (run_id,)).fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE plan SET status = ?, enabled = ?, updated_at = ? WHERE id = ?",
                    (
                        plan_status.value,
                        0 if plan_status is PlanStatus.STOPPED else 1,
                        ts,
                        row["plan_id"],
                    ),
                )


def get_run(db, run_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM run WHERE id = ?", (run_id,))
    return _run_to_dict(row) if row else None


def _run_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "plan_id": row["plan_id"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "status": RunStatus(row["status"]) if row["status"] in RunStatus._value2member_map_ else row["status"],
        "category": row["category"],
        "stop_reason": row["stop_reason"],
        "message": row["message"],
        "exit_code": row["exit_code"],
        "booking_id": row["booking_id"],
        "query_count": int(row["query_count"]),
        "submit_count": int(row["submit_count"]),
        "seat_switches": int(row["seat_switches"]),
        "trigger": row["trigger"],
    }


def list_runs(db, *, plan_id: int | None = None, limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit), 500))
    if plan_id is None:
        rows = db.query_all(
            "SELECT r.*, p.name AS plan_name FROM run r JOIN plan p ON p.id = r.plan_id"
            " ORDER BY r.id DESC LIMIT ?",
            (limit,),
        )
    else:
        rows = db.query_all(
            "SELECT r.*, p.name AS plan_name FROM run r JOIN plan p ON p.id = r.plan_id"
            " WHERE r.plan_id = ? ORDER BY r.id DESC LIMIT ?",
            (plan_id, limit),
        )
    out = []
    for row in rows:
        d = _run_to_dict(row)
        d["plan_name"] = row["plan_name"]
        out.append(d)
    return out


def list_running_runs(db) -> list[dict]:
    rows = db.query_all(
        "SELECT r.*, p.name AS plan_name FROM run r JOIN plan p ON p.id = r.plan_id"
        " WHERE r.status = ? ORDER BY r.id DESC",
        (RunStatus.RUNNING.value,),
    )
    out = []
    for row in rows:
        d = _run_to_dict(row)
        d["plan_name"] = row["plan_name"]
        out.append(d)
    return out


def last_run_for_plan(db, plan_id: int) -> dict | None:
    row = db.query_one(
        "SELECT * FROM run WHERE plan_id = ? ORDER BY id DESC LIMIT 1", (plan_id,)
    )
    return _run_to_dict(row) if row else None


def run_duration_text(run: dict | None) -> str:
    """运行的耗时（``开始 → 结束``）；未结束或时间无法解析时如实说明。

    供页面展示，不参与任何判定。两个时间戳同格式（``YYYY-MM-DD HH:MM:SS``），
    因此直接按字符串解析即可。
    """
    if not run:
        return "—"
    started = _parse_ts(run.get("started_at"))
    finished = _parse_ts(run.get("finished_at"))
    if started is None:
        return "—"
    if finished is None:
        return "进行中"
    seconds = (finished - started).total_seconds()
    if seconds < 0:
        return "—"
    if seconds < 60:
        return f"{int(seconds)} 秒"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes} 分 {sec} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


def _parse_ts(value) -> datetime | None:
    try:
        return datetime.strptime(str(value or "").strip(), _FMT)
    except ValueError:
        return None


# ------------------------------------------------------------------ 运行事件


def add_run_event(
    db,
    run_id: int,
    *,
    phase: RunPhase | str,
    level: str,
    message: str,
    category: ErrorCategory | None = None,
    now: datetime | None = None,
) -> None:
    phase_v = phase.value if isinstance(phase, RunPhase) else str(phase)
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO run_event (run_id, at, phase, level, message, category)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_id, now_text(now), phase_v, str(level or "info"),
                redact_message(message),
                category.value if isinstance(category, ErrorCategory) else (str(category) if category else None),
            ),
        )


def list_run_events(db, run_id: int, *, limit: int = 500) -> list[dict]:
    rows = db.query_all(
        "SELECT * FROM run_event WHERE run_id = ? ORDER BY id ASC LIMIT ?",
        (run_id, max(1, min(int(limit), 2000))),
    )
    return [
        {
            "id": r["id"],
            "at": r["at"],
            "phase": r["phase"],
            "level": r["level"],
            "message": r["message"],
            "category": r["category"],
        }
        for r in rows
    ]


def purge_run_events(db, *, keep_days: int = 30, now: datetime | None = None) -> int:
    from datetime import timedelta

    cutoff = ((now or datetime.now()) - timedelta(days=max(1, keep_days))).strftime(_FMT)
    with db.tx() as conn:
        cur = conn.execute(
            "DELETE FROM run_event WHERE run_id IN (SELECT id FROM run WHERE started_at < ?)",
            (cutoff,),
        )
        cur2 = conn.execute("DELETE FROM run WHERE started_at < ?", (cutoff,))
        return (cur.rowcount or 0) + (cur2.rowcount or 0)


# ------------------------------------------------------------------ 认证状态


def get_auth_state(db) -> dict:
    row = db.query_one("SELECT * FROM auth_state WHERE id = 1")
    if row is None:
        return {"status": "indeterminate", "recovery_state": "idle", "message": "", "checked_at": None, "updated_at": ""}
    return {
        "status": row["status"],
        "recovery_state": row["recovery_state"],
        "message": row["message"],
        "checked_at": row["checked_at"],
        "updated_at": row["updated_at"],
    }


def set_auth_state(
    db,
    *,
    status: str,
    message: str,
    recovery_state: str | None = None,
    source: str = "manual",
    run_id: int | None = None,
    now: datetime | None = None,
) -> None:
    ts = now_text(now)
    safe = redact_message(message)
    with db.tx() as conn:
        conn.execute(
            "UPDATE auth_state SET status = ?, message = ?, checked_at = ?, updated_at = ?,"
            " recovery_state = COALESCE(?, recovery_state) WHERE id = 1",
            (str(status), safe, ts, ts, recovery_state),
        )
        # 认证状态变化才记事件，避免轮询把表写爆
        last = conn.execute(
            "SELECT status FROM auth_event ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last is None or last["status"] != str(status):
            conn.execute(
                "INSERT INTO auth_event (at, status, message, source) VALUES (?, ?, ?, ?)",
                (ts, str(status), safe, source),
            )


# ---------------------------------------------------------- 凭据来源元数据
#
# 网页**只读**这张表：它存的是来源标签、时间、单向指纹与登录流程状态，
# 绝不存凭据值。页面因此在结构上不可能把 Cookie/Token 渲染到前端。


def get_credential_state(db) -> dict:
    """读取凭据来源元数据；缺行时给出与"未配置"一致的默认值。"""
    row = db.query_one("SELECT * FROM auth_credential_state WHERE id = 1")
    if row is None:
        return {
            "source": "none",
            "snapshot_present": 0,
            "snapshot_captured_at": None,
            "fingerprint": "",
            "browser_supported": 0,
            "browser_reason": "",
            "login_state": "idle",
            "login_message": "",
            "login_updated_at": None,
            "updated_at": "",
        }
    return {
        "source": row["source"],
        "snapshot_present": row["snapshot_present"],
        "snapshot_captured_at": row["snapshot_captured_at"],
        "fingerprint": row["fingerprint"],
        "browser_supported": row["browser_supported"],
        "browser_reason": row["browser_reason"],
        "login_state": row["login_state"],
        "login_message": row["login_message"],
        "login_updated_at": row["login_updated_at"],
        "updated_at": row["updated_at"],
    }


def set_credential_state(db, *, now: datetime | None = None, **fields) -> None:
    """部分更新凭据元数据（只写显式给出的字段）。

    所有文本都过 ``redact_message`` —— 即便某天有人误把一个令牌塞进
    ``login_message``，它也会在入库前被抹掉，并被截断到合理长度。
    """
    allowed = (
        "source", "snapshot_present", "snapshot_captured_at", "fingerprint",
        "browser_supported", "browser_reason", "login_state", "login_message",
    )
    unknown = set(fields) - set(allowed)
    if unknown:
        raise ValueError(f"未知的凭据元数据字段: {sorted(unknown)}")

    ts = now_text(now)
    assignments: list[str] = []
    params: list[Any] = []
    for key in allowed:
        if key not in fields:
            continue
        value = fields[key]
        if key in ("source", "fingerprint", "browser_reason", "login_state", "login_message"):
            value = redact_message(value)
        elif key == "snapshot_present" or key == "browser_supported":
            value = 1 if value else 0
        assignments.append(f"{key} = ?")
        params.append(value)

    stamp = "updated_at = ?"
    params.append(ts)
    if "login_state" in fields or "login_message" in fields:
        stamp += ", login_updated_at = ?"
        params.append(ts)

    with db.tx() as conn:
        conn.execute(
            f"UPDATE auth_credential_state SET {', '.join(assignments + [stamp])}"
            " WHERE id = 1",
            tuple(params),
        )


def list_auth_events(db, *, limit: int = 50) -> list[dict]:
    rows = db.query_all(
        "SELECT * FROM auth_event ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 500)),)
    )
    return [
        {"id": r["id"], "at": r["at"], "status": r["status"], "message": r["message"], "source": r["source"]}
        for r in rows
    ]


# ------------------------------------------------------------------ 通知设置


def get_notify_setting(db) -> dict:
    row = db.query_one("SELECT * FROM notify_setting WHERE id = 1")
    if row is None:
        return {}
    return {
        "enabled": int(row["enabled"]),
        "channel": row["channel"],
        "smtp_host": row["smtp_host"],
        "smtp_port": int(row["smtp_port"]),
        "smtp_user": row["smtp_user"],
        "smtp_password_env": row["smtp_password_env"],
        "mail_from": row["mail_from"],
        "mail_to": row["mail_to"],
        "use_tls": int(row["use_tls"]),
        "event_flags": _loads(row["event_flags"], {}),
        "updated_at": row["updated_at"],
    }


def set_notify_setting(db, data: dict, *, now: datetime | None = None) -> None:
    """保存通知设置。

    **不接受也不存储邮件口令**：只保存「读取口令的环境变量名」，
    真实口令留在服务端环境变量/受保护文件中（本轮未实现 SMTP 发送）。
    """
    current = get_notify_setting(db)
    flags = data.get("event_flags")
    if flags is None:
        flags = current.get("event_flags", {})
    try:
        port = int(str(data.get("smtp_port") or current.get("smtp_port") or 465).strip())
    except (TypeError, ValueError):
        port = 465
    if not 1 <= port <= 65535:
        port = 465
    with db.tx() as conn:
        conn.execute(
            "UPDATE notify_setting SET enabled = ?, channel = ?, smtp_host = ?, smtp_port = ?,"
            " smtp_user = ?, smtp_password_env = ?, mail_from = ?, mail_to = ?, use_tls = ?,"
            " event_flags = ?, updated_at = ? WHERE id = 1",
            (
                int(bool(data.get("enabled"))),
                str(data.get("channel") or "email")[:32],
                str(data.get("smtp_host") or "")[:200],
                port,
                str(data.get("smtp_user") or "")[:200],
                str(data.get("smtp_password_env") or "HAUT_SMTP_PASSWORD")[:64],
                str(data.get("mail_from") or "")[:200],
                str(data.get("mail_to") or "")[:500],
                int(bool(data.get("use_tls"))),
                json.dumps(flags, ensure_ascii=False),
                now_text(now),
            ),
        )


def list_notify_events(db, *, limit: int = 50) -> list[dict]:
    rows = db.query_all(
        "SELECT * FROM notify_event ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 500)),)
    )
    return [
        {
            "id": r["id"], "at": r["at"], "event": r["event"], "plan_id": r["plan_id"],
            "run_id": r["run_id"], "message": r["message"], "status": r["status"],
            "attempts": int(r["attempts"]), "last_error": r["last_error"],
        }
        for r in rows
    ]


# ------------------------------------------------------------------ 统计


def dashboard_stats(db, *, today: date | None = None) -> dict:
    today_s = (today or date.today()).strftime("%Y-%m-%d")
    def _count(sql: str, params=()) -> int:
        row = db.query_one(sql, params)
        return int(row["n"]) if row else 0

    return {
        "plans_total": _count("SELECT COUNT(*) AS n FROM plan"),
        "plans_pending": _count("SELECT COUNT(*) AS n FROM plan WHERE status = ?", (PlanStatus.PENDING.value,)),
        "plans_running": _count("SELECT COUNT(*) AS n FROM plan WHERE status = ?", (PlanStatus.RUNNING.value,)),
        "plans_paused": _count("SELECT COUNT(*) AS n FROM plan WHERE status = ?", (PlanStatus.PAUSED.value,)),
        "plans_awaiting_auth": _count("SELECT COUNT(*) AS n FROM plan WHERE status = ?", (PlanStatus.AWAITING_AUTH.value,)),
        "plans_succeeded": _count("SELECT COUNT(*) AS n FROM plan WHERE status = ?", (PlanStatus.SUCCEEDED.value,)),
        "plans_failed": _count("SELECT COUNT(*) AS n FROM plan WHERE status = ?", (PlanStatus.FAILED.value,)),
        "plans_unknown": _count("SELECT COUNT(*) AS n FROM plan WHERE status = ?", (PlanStatus.UNKNOWN.value,)),
        "plans_stopped": _count("SELECT COUNT(*) AS n FROM plan WHERE status = ?", (PlanStatus.STOPPED.value,)),
        "runs_today": _count("SELECT COUNT(*) AS n FROM run WHERE started_at >= ?", (today_s + " 00:00:00",)),
        "runs_unknown": _count("SELECT COUNT(*) AS n FROM run WHERE status = ?", (RunStatus.UNKNOWN.value,)),
        "runs_failed": _count("SELECT COUNT(*) AS n FROM run WHERE status = ?", (RunStatus.FAILED.value,)),
        "runs_running": _count("SELECT COUNT(*) AS n FROM run WHERE status = ?", (RunStatus.RUNNING.value,)),
        "today": today_s,
    }


# ------------------------------------------------------------------ meta 键值
#
# ``meta`` 表一直存在（schema_version 就写在里面），但在此之前从没有读写入口。
# 用户端的两个开关（注册开关、邀请码）需要一个**管理员可以随时改、又不必改 .env
# 再重启**的地方，这里就是它。值一律按文本存取，不做类型推断（读的人自己决定）。


def get_meta(db, key: str, default: str = "") -> str:
    row = db.query_one("SELECT value FROM meta WHERE key = ?", (str(key),))
    return default if row is None else str(row["value"])


def set_meta(db, key: str, value: str, *, now: datetime | None = None) -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(key), str(value)),
        )


# ------------------------------------------------------------------ 计划归属
#
# 归属关系放在侧表 ``user_plan`` 里，**不给 plan 加列**（本仓库没有迁移机制，
# 给既有表加列会让老库 SELECT 时抛 no such column）。因此：
# **没有对应行的计划 = 无主计划 = 管理端的计划**。这对老库是准确的，
# 不是猜测：那些计划全部建于引入用户端之前。


def set_plan_owner(db, plan_id: int, user_id: int, *, now: datetime | None = None) -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO user_plan (plan_id, user_id, created_at)"
            " VALUES (?, ?, ?)",
            (int(plan_id), int(user_id), now_text(now)),
        )


def get_plan_owner(db, plan_id: int) -> int | None:
    """计划的属主 user_id；无主（管理端/历史）计划返回 ``None``。"""
    row = db.query_one("SELECT user_id FROM user_plan WHERE plan_id = ?", (int(plan_id),))
    return int(row["user_id"]) if row else None


def plan_owners(db) -> dict[int, int]:
    """``{plan_id: user_id}``，供列表页一次性取用（避免每行一次查询）。"""
    return {
        int(r["plan_id"]): int(r["user_id"])
        for r in db.query_all("SELECT plan_id, user_id FROM user_plan")
    }


def list_plans_for_user(db, user_id: int) -> list[dict]:
    """只列出该用户自己的计划 —— 别人的计划在查询层面就取不出来。"""
    rows = db.query_all(
        f"SELECT {_PLAN_FIELDS} FROM plan WHERE id IN"
        " (SELECT plan_id FROM user_plan WHERE user_id = ?) ORDER BY id DESC",
        (int(user_id),),
    )
    return [_plan_to_dict(r) for r in rows]


# ------------------------------------------------------------------ 用户学校凭据（元数据）
#
# 两层接口是**刻意分开**的，只有这样才能给「凭据不进页面」一个结构性保证：
# ``get_user_credential`` 会返回密文（只有 services.user_credentials 用来解密，
# **绝不允许传进模板**），``get_user_credential_view`` 里根本不存在密文字段
# —— 页面拿到它以后想渲染密文都拿不到。


def get_user_credential(db, user_id: int) -> dict | None:
    """**含密文**的完整行。仅供 :mod:`haut_seat.services.user_credentials` 解密使用。

    调用方必须清楚：返回值里有 ``cookie_enc`` / ``authorization_enc``。
    任何把它交给模板、日志或 JSON 响应的写法都是错的。
    """
    row = db.query_one("SELECT * FROM user_credential WHERE user_id = ?", (int(user_id),))
    return dict(row) if row else None


def get_user_credential_view(db, user_id: int) -> dict:
    """页面/接口只读这一份：**没有任何密文字段**，只有标识与状态元数据。

    ``school_username`` 是学校统一身份认证账号（学号/工号）—— 它是**标识**，
    用来回答"这条计划用的是哪个学校账号"，不是口令，也不是平台用户名。
    """
    row = db.query_one(
        "SELECT school_username, fingerprint, status, message, captured_at, checked_at,"
        " updated_at FROM user_credential WHERE user_id = ?",
        (int(user_id),),
    )
    if row is None:
        return {
            "school_username": "", "fingerprint": "", "status": "none", "message": "",
            "captured_at": None, "checked_at": None, "updated_at": "",
        }
    return {
        "school_username": row["school_username"] or "",
        "fingerprint": row["fingerprint"] or "",
        "status": row["status"] or "none",
        "message": row["message"] or "",
        "captured_at": row["captured_at"],
        "checked_at": row["checked_at"],
        "updated_at": row["updated_at"],
    }


def set_user_credential(
    db,
    user_id: int,
    *,
    school_username: str,
    cookie_enc: str,
    authorization_enc: str,
    fingerprint: str,
    status: str = "valid",
    message: str = "",
    captured_at: str | None = None,
    now: datetime | None = None,
) -> None:
    """写入一份（已加密的）用户学校凭据，覆盖该用户原有的那一份。"""
    ts = now_text(now)
    with db.tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO user_credential (user_id, school_username, cookie_enc,"
            " authorization_enc, fingerprint, status, message, captured_at, checked_at,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(user_id), str(school_username or "")[:64], cookie_enc, authorization_enc,
                str(fingerprint or "")[:32], str(status or "none"), redact_message(message),
                captured_at or ts, ts, ts,
            ),
        )


def set_user_credential_status(
    db, user_id: int, *, status: str, message: str = "", now: datetime | None = None
) -> None:
    """只更新状态（不动密文）。凭据在调度器里被判失效时走这里，**不清空密文**
    —— 密文还在才能让用户看到"上次成功登录的时间"这类信息，也才不必重新登录。"""
    ts = now_text(now)
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO user_credential (user_id, status, message, checked_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET status = excluded.status,"
            " message = excluded.message, checked_at = excluded.checked_at,"
            " updated_at = excluded.updated_at",
            (int(user_id), str(status or "none"), redact_message(message), ts, ts),
        )


def clear_user_credential(db, user_id: int) -> bool:
    with db.tx() as conn:
        cur = conn.execute("DELETE FROM user_credential WHERE user_id = ?", (int(user_id),))
        return bool(cur.rowcount)


def user_credential_statuses(db) -> dict[int, str]:
    """``{user_id: 状态}``，调度器每个 tick 取一次，避免每计划一次查询。"""
    return {
        int(r["user_id"]): str(r["status"] or "none")
        for r in db.query_all("SELECT user_id, status FROM user_credential")
    }


def count_users_with_credentials(db) -> int:
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM user_credential WHERE status = 'valid'"
        " AND cookie_enc <> '' AND authorization_enc <> ''"
    )
    return int(row["n"]) if row else 0


# ------------------------------------------------------------------ 用户认证事件


def add_user_auth_event(
    db, user_id: int, *, status: str, message: str = "", source: str = "manual",
    now: datetime | None = None,
) -> None:
    """记一条该用户自己的认证事件（只属于他，别的用户在页面上看不到）。"""
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO user_auth_event (user_id, at, status, message, source)"
            " VALUES (?, ?, ?, ?, ?)",
            (int(user_id), now_text(now), str(status or ""), redact_message(message),
             str(source or "manual")),
        )


def list_user_auth_events(db, user_id: int, *, limit: int = 20) -> list[dict]:
    """该用户自己的认证事件（倒序）。

    默认**不列出限流计数用的 attempt 行**：那是内部计数，把它们混进事件流只会让
    用户看到一串没有含义的"尝试"。真正的结果（绑定成功 / 失效 / 解绑）都在。
    """
    rows = db.query_all(
        "SELECT id, at, status, message, source FROM user_auth_event"
        " WHERE user_id = ? AND source <> 'bind_attempt' ORDER BY id DESC LIMIT ?",
        (int(user_id), int(limit)),
    )
    return [dict(r) for r in rows]


def purge_old_user_auth_events(db, *, keep_days: int = 90, now: datetime | None = None) -> int:
    cutoff = now_text((now or datetime.now()) - timedelta(days=int(keep_days)))
    with db.tx() as conn:
        cur = conn.execute("DELETE FROM user_auth_event WHERE at < ?", (cutoff,))
        return cur.rowcount or 0