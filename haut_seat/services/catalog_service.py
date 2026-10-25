"""座位目录服务：只读拉取 + 落库 + 失败降级。

设计要点（与 ``AuthService`` 同形，便于测试注入与阅读）：
- ``query_fn`` 可注入；默认延迟导入 ``services.io.query_online``（**只读**）。
- **绝不** import 或调用 ``submit_online`` —— 刷新目录在任何情况下都不会产生
  真实预约或任何业务副作用。
- **先采集、后写库**：所有网络请求在事务外完成，避免持写锁做 I/O（会阻塞调度器
  与看板）；采集完成后只对**成功**的区域写库。
- 失败时一个字节都不写目录，「上次成功时间」保持不变。
- 「最近一次在线成功更新」（``catalog_state``）与「最近一次本地导入」
  （``catalog_import_state``）**分开记录、分别展示**：本地导入绝不写前者，
  不会把自己伪装成一次在线刷新成功。
- 目录只表示「服务器返回过这个座位」，**不代表它当前可预约**。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from haut_seat.core.auth import canonical_probe_window
from haut_seat.core.catalog import (
    derive_area_label,
    derive_floor_label,
    normalize_seat_id,
)
from haut_seat.web import repository as repo

CATALOG_STATUS_LABELS: dict[str, str] = {
    "never": "尚未在线更新过",
    "ok": "更新成功",
    "partial": "部分区域更新失败",
    "failed": "更新失败",
}

IMPORT_STATUS_LABELS: dict[str, str] = {
    "never": "尚未导入过",
    "ok": "导入成功",
    "failed": "导入失败",
}

_SEAT_NAME_MAX = 120
_REGION_LABEL_MAX = 60

# 业务错误 code 里实测出现过的是 200/401/403/404/500/1111 这类短标识。只放行
# 这个形状，是为了在保留诊断价值的同时，不把响应里的任意内容带进页面。
_SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,32}$")


def safe_business_code(code: Any) -> str:
    """把业务错误的 ``code`` 渲染成**可安全展示**的短文本；不可展示时返回 ``""``。

    ``code`` 来自服务器返回的 JSON，形状不受控（可能是对象、超长串或含敏感
    内容的文本）。这里只放行 int（排除 bool —— 它是 int 子类）与极短标识串，
    其余一律降级为「不显示」。
    """
    if isinstance(code, bool):  # bool 是 int 的子类，需先排除
        return ""
    if isinstance(code, int):
        return str(code)
    if isinstance(code, str):
        text = code.strip()
        if _SAFE_CODE_RE.match(text):
            return text
    return ""


class CatalogService:
    """座位目录的只读刷新与持久化入口。"""

    def __init__(
        self,
        db,
        settings,
        logger: logging.Logger | None = None,
        *,
        query_fn: Callable[..., Any] | None = None,
    ):
        self.db = db
        self.settings = settings
        self.logger = logger or logging.getLogger("haut_seat.catalog")
        self._query_fn = query_fn

    # ---- 只读查询 ----

    def _query(self, regionid: int):
        """执行一次只读座位查询。

        时间窗口用 ``core.auth.canonical_probe_window()``（当天 08:00→22:00），
        与认证预检同一套做法：目的是让业务时间规则（预约窗口限制）不干扰
        「这个区域有哪些座位」这个事实本身。
        """
        fn = self._query_fn
        if fn is None:
            from haut_seat.services.io import query_online as fn  # noqa: PLW2901
        start, end = canonical_probe_window()
        return fn(
            self.settings,
            int(regionid),
            start,
            end,
            1,  # pageNum：真实观察显示 pageSize 被忽略（请求 10 返回 70），故不需翻页
            self.settings.page_size,
        )

    @staticmethod
    def _normalize(result) -> tuple[list[dict], dict]:
        """把查询结果归一化为目录行，并统计被跳过的异常数据。

        跳过项**如实计数上报**而不是静默丢弃：座位名或 ID 缺失意味着接口数据
        不完整，用户需要知道目录没覆盖全。
        """
        seats: list[dict] = []
        seen: set[str] = set()
        stats = {"skipped_incomplete": 0, "skipped_duplicate_id": 0}
        for seat in getattr(result, "seats", []):
            seat_id = normalize_seat_id(seat.id)
            raw_name = seat.seat_name
            name = "" if raw_name is None else str(raw_name).strip()
            if not seat_id or not name:
                stats["skipped_incomplete"] += 1
                continue
            if seat_id in seen:
                stats["skipped_duplicate_id"] += 1
                continue
            seen.add(seat_id)
            seats.append({
                "seat_id": seat_id,
                "seat_name": name[:_SEAT_NAME_MAX],
                "area_label": derive_area_label(name),
                "floor_label": derive_floor_label(name),
            })
        return seats, stats

    def _region_label_of(self, seats: list[dict]) -> str:
        """区域名：取本区域座位推断出的第一个非空区域名。"""
        for seat in seats:
            if seat["area_label"]:
                return seat["area_label"]
        return ""

    # ---- 刷新 ----

    def planned_regionids(self) -> list[int]:
        """要刷新的区域：目录已知的区域；一个都没有时用配置里的默认区域。

        不存在「区域列表」接口，所以区域只能来自人工登记（面板「添加区域」或
        离线导入），配置里的 ``regionid`` 仅作为首次的起点。
        """
        known = [r["regionid"] for r in repo.list_catalog_regions(self.db)]
        if known:
            return known
        default = getattr(self.settings, "regionid", None)
        return [int(default)] if default else []

    def refresh(self, regionids: list[int] | None = None, *, source: str = "manual") -> dict:
        """刷新目录。**只读查询，绝不提交预约。**

        返回 ``{"ok", "status", "message", "results": [...]}``。

        ``regionids=None`` 表示「按目录/配置自动决定」；显式传入空列表表示
        「没有要刷新的区域」，两者语义不同，不互相顶替。
        """
        candidates = self.planned_regionids() if regionids is None else regionids
        targets = [int(r) for r in candidates]
        if not targets:
            message = "没有可刷新的区域：请先「添加区域」或从 JSON 导入。"
            repo.set_catalog_state(self.db, status="failed", message=message)
            return {"ok": False, "status": "failed", "message": message, "results": []}

        # ---- 采集阶段：网络 I/O 一律在事务之外 ----
        collected: list[tuple[int, list[dict]]] = []
        failed: list[tuple[int, str]] = []
        totals = {"skipped_incomplete": 0, "skipped_duplicate_id": 0, "empty_regions": 0}

        for regionid in targets:
            try:
                result = self._query(regionid)
            except Exception as exc:  # 网络/结构/业务/认证异常
                reason = self._describe_failure(exc)
                self.logger.warning("目录刷新失败 regionid=%s: %s", regionid, reason)
                failed.append((regionid, reason))
                continue
            seats, stats = self._normalize(result)
            for key in stats:
                totals[key] += stats[key]
            if not seats:
                # 空响应视为可疑：若照写会把该区域全部座位误判为「已下线」
                totals["empty_regions"] += 1
                failed.append(
                    (regionid, "接口返回 0 个座位（按可疑处理，未改动该区域目录）")
                )
                continue
            collected.append((regionid, seats))

        # ---- 写库阶段：只写成功的区域 ----
        report = {"added": 0, "updated": 0, "retired": 0, "reactivated": 0, "relabeled": 0}
        seat_total = 0
        for regionid, seats in collected:
            outcome = repo.sync_region_catalog(
                self.db, regionid, seats, derived_label=self._region_label_of(seats),
            )
            for key in report:
                report[key] += outcome[key]
            seat_total += len(seats)

        status = self._status_for(succeeded=len(collected), failed=len(failed))
        message = self._build_message(status, collected, failed, totals, report)
        repo.set_catalog_state(
            self.db,
            status=status,
            message=message,
            region_count=len(collected),
            seat_count=seat_total,
            ok=(status == "ok"),
        )
        self.logger.info(
            "目录刷新完成：status=%s 成功区域=%s 失败区域=%s 座位=%s",
            status, len(collected), len(failed), seat_total,
        )
        return {
            "ok": status == "ok",
            "status": status,
            "message": message,
            "results": [
                {"regionid": r, "ok": True, "seat_count": len(s)}
                for r, s in collected
            ] + [{"regionid": r, "ok": False, "reason": m} for r, m in failed],
        }

    @staticmethod
    def _describe_failure(exc: Exception) -> str:
        """把异常转成给人看的中文原因（不泄漏 URL、响应体或凭据）。"""
        from haut_seat.exceptions import BusinessError
        from haut_seat.services.io import auth_stop_reason

        auth_reason = auth_stop_reason(exc)
        if auth_reason:
            return f"{auth_reason} —— 请到「认证状态」页更新凭据后重试（目录未被改动）"
        if isinstance(exc, BusinessError):
            # 认证类 code（401/403）已被上面的 auth_stop_reason 拦走；能到这里的是
            # 其它业务错误。保留 code 才能区分「时间窗口不合法(1111)」之类的具体原因。
            # msg / body 一律不带：实测 msg 里含服务端路径，且它来自服务器文本。
            code = safe_business_code(exc.code)
            if code:
                return f"服务器返回业务错误 code={code}（不是座位列表），该区域目录未被改动"
            return "服务器返回业务错误（未提供可展示的 code），该区域目录未被改动"
        name = type(exc).__name__
        if name == "ResponseShapeError":
            return "响应结构不符合预期（可能接口有变动），该区域目录未被改动"
        if name == "NonJsonResponseError":
            return "服务器返回了非 JSON 响应（通常是登录页），该区域目录未被改动"
        return f"查询失败（{name}），该区域目录未被改动"

    @staticmethod
    def _status_for(*, succeeded: int, failed: int) -> str:
        if succeeded and failed:
            return "partial"
        if succeeded:
            return "ok"
        return "failed"

    @staticmethod
    def _build_message(
        status: str, collected: list, failed: list, totals: dict, report: dict
    ) -> str:
        parts: list[str] = []
        if status == "ok":
            parts.append(f"更新成功：{len(collected)} 个区域")
        elif status == "partial":
            parts.append(
                f"部分成功：{len(collected)} 个区域已更新，{len(failed)} 个失败"
                "（失败区域的目录原样保留）"
            )
        else:
            parts.append("更新失败：没有区域更新成功，目录保持原样")
        if collected:
            parts.append(
                f"新增 {report['added']}，更新 {report['updated']}，"
                f"恢复上线 {report['reactivated']}，标记下线 {report['retired']}"
            )
        if report["relabeled"]:
            parts.append(f"{report['relabeled']} 个区域的推断名称发生变化")
        if totals["skipped_incomplete"]:
            parts.append(f"跳过 {totals['skipped_incomplete']} 条数据不完整的座位")
        if totals["skipped_duplicate_id"]:
            parts.append(f"跳过 {totals['skipped_duplicate_id']} 条重复座位 ID")
        if totals["empty_regions"]:
            parts.append(f"{totals['empty_regions']} 个区域返回空列表")
        if failed:
            parts.append("失败原因：" + "；".join(f"区域 {r}：{m}" for r, m in failed[:3]))
        return "。".join(parts)

    # ---- 离线导入 ----

    def import_json(self, text: str, regionid: int, *, source: str = "import") -> dict:
        """从粘贴的 ``layout/query`` 响应 JSON 导入一个区域的座位。

        与线上刷新走**同一个** ``LayoutQueryResult.from_response`` 解析入口，
        因此两条路径的解析行为完全一致。**只读本地文本，不访问网络。**

        注意：响应里没有区域 ID（它是请求参数），所以必须由表单提供 ``regionid``。

        每一次尝试（成功或失败）都记进 ``catalog_import_state``——页面据此区分
        「最近一次在线更新」与「最近一次本地导入」。失败的几个分支都在任何写库
        之前提前返回，因此旧目录与上次成功时间一个字节都不动，也**从不**写
        ``catalog_state``（导入不是在线刷新成功）。
        """
        from haut_seat.exceptions import BusinessError, ResponseShapeError
        from haut_seat.models.seat import LayoutQueryResult

        raw = str(text or "").strip()
        if not raw:
            return self._import_failed("请粘贴查询响应 JSON。")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            return self._import_failed(f"JSON 解析失败（第 {exc.lineno} 行）：{exc.msg}")
        try:
            result = LayoutQueryResult.from_response(data)
        except BusinessError as exc:
            # 只放行形状受控的 code；异常携带的 msg/body（服务器文本，实测含服务端
            # 路径）一律不进页面。
            code = safe_business_code(exc.code)
            detail = f"code={code}" if code else "未提供可展示的 code"
            return self._import_failed(f"响应是业务错误信封（{detail}），不是座位列表。")
        except ResponseShapeError as exc:
            return self._import_failed(f"响应结构不符合预期：{exc}")

        seats, stats = self._normalize(result)
        if not seats:
            return self._import_failed(
                "这份响应里没有可用的座位（座位名或 ID 缺失），已忽略。"
            )
        repo.add_catalog_region(self.db, int(regionid))
        outcome = repo.sync_region_catalog(
            self.db, int(regionid), seats, derived_label=self._region_label_of(seats),
        )
        message = (
            f"已导入区域 {regionid}：{len(seats)} 个座位"
            f"（新增 {outcome['added']}，更新 {outcome['updated']}，"
            f"下线 {outcome['retired']}）"
        )
        if stats["skipped_incomplete"]:
            message += f"，跳过 {stats['skipped_incomplete']} 条不完整数据"
        if stats["skipped_duplicate_id"]:
            message += f"，跳过 {stats['skipped_duplicate_id']} 条重复 ID"
        repo.set_catalog_import_state(
            self.db, status="ok", message=message,
            regionid=int(regionid), seat_count=len(seats), ok=True,
        )
        self.logger.info("目录离线导入 regionid=%s 座位=%s", regionid, len(seats))
        return {"ok": True, "message": message, "seat_count": len(seats)}

    def _import_failed(self, message: str) -> dict:
        """记录一次失败的导入：只推进「尝试」，不动上次成功导入时间与目录。"""
        repo.set_catalog_import_state(self.db, status="failed", message=message, ok=False)
        return {"ok": False, "message": message}

    # ---- 供路由使用的只读视图 ----

    def state(self) -> dict:
        """在线刷新状态 + 本地导入状态（两者独立，页面分别展示）。"""
        state = repo.get_catalog_state(self.db)
        state["status_label"] = CATALOG_STATUS_LABELS.get(
            state["last_status"], state["last_status"]
        )
        imported = repo.get_catalog_import_state(self.db)
        imported["status_label"] = IMPORT_STATUS_LABELS.get(
            imported["last_status"], imported["last_status"]
        )
        state["import"] = imported
        return state

    def add_region(self, regionid: int) -> dict:
        if repo.add_catalog_region(self.db, int(regionid)):
            return {"ok": True, "message": f"已添加区域 {regionid}，请点击「更新目录」拉取座位。"}
        return {"ok": True, "message": f"区域 {regionid} 已在目录中（未重复添加）。"}

    def rename_region(self, regionid: int, label: str) -> dict:
        text = str(label or "").strip()
        if not text:
            return {"ok": False, "message": "区域名称不能为空。"}
        if repo.get_catalog_region(self.db, int(regionid)) is None:
            return {"ok": False, "message": f"区域 {regionid} 不在目录中。"}
        repo.rename_catalog_region(self.db, int(regionid), text[:_REGION_LABEL_MAX])
        return {
            "ok": True,
            "message": f"区域 {regionid} 已命名为「{text}」。人工命名不会被后续更新覆盖。",
        }


__all__ = ["CatalogService", "CATALOG_STATUS_LABELS", "IMPORT_STATUS_LABELS", "safe_business_code"]