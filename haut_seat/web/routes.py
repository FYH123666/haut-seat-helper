"""Web 路由：登录、看板、计划、运行记录、认证状态、通知设置、系统状态。

所有状态变更都是 POST，且必须通过「已登录会话 + CSRF 令牌」两道校验。
所有异常路径都返回可读的中文提示，不泄漏凭据、路径或内部堆栈。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from haut_seat.core.auth import AuthStatus
from haut_seat.services.auth_browser import browser_availability
from haut_seat.services.auth_login_service import record_credential_source
from haut_seat.services.auth_store import (
    CredentialsUnavailableError,
    resolve_credentials,
)
from haut_seat.services.catalog_service import CATALOG_STATUS_LABELS
from haut_seat.services.notification_service import (
    DEFAULT_EVENT_FLAGS,
    NOTIFY_EVENT_LABELS,
    NotifyEvent,
)
from haut_seat.services.remote_browser import MAX_EVENTS, MAX_INPUT_BYTES
from haut_seat.services.scheduler import plan_window_state, ran_irreversibly_today
from haut_seat.services.school_login import SchoolLoginError
from haut_seat.services.state import PlanStatus, RunStatus
from haut_seat.services import user_credentials as uc
from haut_seat.web import repository as repo
from haut_seat.web import security
from haut_seat.web import user_auth
from haut_seat.web.deps import (
    _same_origin,
    booking_csrf_token,
    check_booking_submit,
    check_csrf,
    client_ip,
    get_ctx,
    optional_admin,
    redirect,
    render,
    require_admin,
    set_booking_csrf_cookie,
)
from haut_seat.web.validation import (
    PLAN_MODES,
    PLAN_MODE_LABELS,
    REPEAT_RULES,
    ValidationError,
    clock_of,
    default_run_date,
    expand_plan_form,
    one_off_plan_is_dead,
    parse_plan,
    plan_when_label,
    plan_will_start_at_once,
    repeat_rule_to_plan_mode,
)

logger = logging.getLogger("haut_seat.web.routes")
router = APIRouter()

_DEFAULT_DAY = default_run_date(today=datetime.now().date())

_PLACEHOLDER_EXAMPLE = {
    # 新建单次预约的默认执行日期是**明天**（理由见 validation.default_run_date）；
    # 下面三个时间戳与它同一天，避免默认值自己就互相矛盾。
    "run_date": _DEFAULT_DAY,
    "trigger_at": "07:00:00",
    "book_start": f"{_DEFAULT_DAY} 08:00:00",
    "book_end": f"{_DEFAULT_DAY} 12:00:00",
}

# 目录页单次最多渲染多少个座位复选框：一个区域实测约 70 个，
# 留足余量又能防止异常数据把页面撑到不可用。
CATALOG_SEAT_LIMIT = 300


# ================================================================== 登录 / 首次引导


@router.get("/")
def index(request: Request):
    return RedirectResponse("/admin", status_code=303)


@router.get("/healthz")
def healthz():
    """存活探针：不返回任何业务或凭据信息。"""
    return {"status": "ok"}


@router.get("/admin/login")
def login_form(request: Request):
    ctx = get_ctx(request)
    if security.count_admins(ctx.db) == 0:
        return RedirectResponse("/admin/setup", status_code=303)
    if optional_admin(request) is not None:
        return RedirectResponse("/admin", status_code=303)
    return render(request, "login.html", title="登录")


@router.post("/admin/login")
def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
):
    ctx = get_ctx(request)
    if security.count_admins(ctx.db) == 0:
        return RedirectResponse("/admin/setup", status_code=303)

    ip = client_ip(request, trust_proxy=bool(ctx.settings.web_trust_proxy))
    locked = security.login_locked_until(ctx.db, username, ip)
    if locked is not None:
        return redirect(
            "/admin/login",
            msg=f"登录尝试过于频繁，请在 {locked:%H:%M:%S} 后重试。", ok=False,
        )

    row = security.find_admin(ctx.db, username)
    ok = bool(row) and security.verify_password(password, row["password_hash"])
    security.record_login_attempt(ctx.db, username, ip, ok)
    if not ok:
        # 统一的失败提示，避免暴露用户名是否存在
        return redirect("/admin/login", msg="用户名或口令不正确。", ok=False)

    token, _csrf = security.create_session(ctx.db, int(row["id"]))
    security.touch_last_login(ctx.db, int(row["id"]))
    logger.info("管理员 %s 登录成功（来源 %s）", row["username"], ip or "unknown")
    response = redirect("/admin", msg="登录成功。")
    from haut_seat.web.app import session_cookie_kwargs

    response.set_cookie(value=token, **session_cookie_kwargs(ctx.settings))
    return response


@router.post("/admin/logout")
async def logout(request: Request):
    session = require_admin(request)
    form = dict(await request.form())
    check_csrf(form, session)
    ctx = get_ctx(request)
    # 退出登录 = 主动放弃这次远程浏览器会话的所有权：**属主匹配才停**，
    # 别的管理员的会话一个字节都不动。join 给得很短（0.5s）—— 工作线程可能
    # 正卡在浏览器调用上，绝不能让"退出登录"这个动作被它拖住。
    if ctx.remote_browser is not None:
        try:
            ctx.remote_browser.stop(
                admin_id=session.admin_id,
                token_hash=session.token_hash,
                reason="管理员退出登录",
                join_timeout_s=0.5,
            )
        except Exception as exc:  # 兜底：退出登录本身绝不能因为收尾失败而失败
            logger.warning("退出登录时结束远程浏览器会话失败: %s", type(exc).__name__)
    security.destroy_session(ctx.db, request.cookies.get(security.SESSION_COOKIE, ""))
    response = redirect("/admin/login", msg="已退出登录。")
    from haut_seat.web.app import session_cookie_kwargs

    response.delete_cookie(session_cookie_kwargs(ctx.settings)["key"], path="/")
    return response


@router.get("/admin/setup")
def setup_form(request: Request):
    ctx = get_ctx(request)
    if security.count_admins(ctx.db) > 0:
        return RedirectResponse("/admin/login", status_code=303)
    if not ctx.settings.web_allow_first_run_setup:
        return render(
            request, "error.html", title="需要先在服务器上创建管理员", status_code=403,
            code=403,
            detail=(
                "未检测到管理员账号，且当前配置禁止网页创建。"
                "请在服务器上执行：python -m haut_seat create-admin --username 你的用户名"
            ),
        )
    from haut_seat.web.app import new_setup_token

    return render(request, "setup.html", title="创建初始管理员",
                  setup_token=new_setup_token(request.app))


@router.post("/admin/setup")
async def setup_submit(request: Request):
    ctx = get_ctx(request)
    if security.count_admins(ctx.db) > 0:
        return RedirectResponse("/admin/login", status_code=303)
    if not ctx.settings.web_allow_first_run_setup:
        raise HTTPException(status_code=403, detail="当前配置禁止网页创建管理员")
    form = dict(await request.form())
    from haut_seat.web.app import consume_setup_token

    if not consume_setup_token(request.app, str(form.get("_setup_token") or "")):
        return redirect("/admin/setup", msg="表单已过期，请重新提交。", ok=False)

    username = str(form.get("username") or "").strip()
    password = str(form.get("password") or "")
    confirm = str(form.get("password_confirm") or "")
    if password != confirm:
        return redirect("/admin/setup", msg="两次输入的口令不一致。", ok=False)
    try:
        security.create_admin(ctx.db, username, password)
    except Exception as exc:
        return redirect("/admin/setup", msg=f"创建失败：{exc}", ok=False)
    logger.info("已创建初始管理员 %s", username)
    return redirect("/admin/login", msg="管理员已创建，请登录。")


# ================================================================== 看板


@router.get("/admin")
def dashboard(request: Request):
    session = require_admin(request)
    ctx = get_ctx(request)
    stats = repo.dashboard_stats(ctx.db)
    running = repo.list_running_runs(ctx.db)
    recent = repo.list_runs(ctx.db, limit=15)
    plans = repo.list_plans(ctx.db)
    # 与表单校验用同一个「现在」：界面上的「下次执行」必须由同一批函数推出来，
    # 单独留一个可注入的时钟，测试才能固定时刻验证这些文案。
    now = _now()
    return render(
        request, "dashboard.html", session, title="我的预约",
        stats=stats, running=running, recent=recent,
        plans=plans,
        # 来源（管理端 / 用户端）：用户端提交的计划同样出现在这里，
        # 因此主页也要能一眼区分「谁建的」——管理端是统一数据入口。
        origins=repo.plan_origins(ctx.db),
        origin_labels=repo.PLAN_ORIGIN_LABELS,
        # 以及「这条计划执行时用谁的账号」——多用户之后，这一列与来源同等重要。
        plan_owners=_plan_owner_view(ctx),
        run_duration_text=repo.run_duration_text,
        when_labels={p["id"]: plan_when_label(p, now=now) for p in plans},
        # 时段只显示时刻：重复计划窗口里的日期只是「建计划那天」，显示日期会误导。
        seat_windows={p["id"]: (clock_of(p["book_start"]), clock_of(p["book_end"])) for p in plans},
        app_setting=repo.get_app_setting(ctx.db),
        scheduler_status=ctx.scheduler.status() if ctx.scheduler else None,
    )


@router.get("/admin/advanced")
def advanced_page(request: Request):
    """高级页：工程/运维界面的唯一入口（纯链接页，不读任何新数据）。

    这些页面一个都没有被删除，也都能直接访问 —— 只是不再和「预约座位」
    平铺在同一级导航里。
    """
    session = require_admin(request)
    ctx = get_ctx(request)
    return render(
        request, "advanced.html", session, title="高级",
        app_setting=repo.get_app_setting(ctx.db),
        scheduler_status=ctx.scheduler.status() if ctx.scheduler else None,
        stats=repo.dashboard_stats(ctx.db),
    )


# ================================================================== 计划


def _plan_owner_view(ctx) -> dict[int, dict]:
    """``{plan_id: {"username":…, "user_id":…, "cred_status":…, "cred_label":…, "cred_tone":…}}``。

    一次查询取全（``plan_owners`` + 用户名表 + 凭据状态表），列表页因此不会
    每行查一次库。**只有标识与状态**：这里没有任何凭据值，模板也就无从渲染。
    """
    owners = repo.plan_owners(ctx.db)
    if not owners:
        return {}
    names = {int(u["id"]): (u["username"], u["display_name"]) for u in user_auth.list_users(ctx.db)}
    statuses = repo.user_credential_statuses(ctx.db)
    view: dict[int, dict] = {}
    for plan_id, user_id in owners.items():
        username, display = names.get(int(user_id), (f"#{user_id}", ""))
        status = statuses.get(int(user_id), uc.CRED_STATUS_NONE)
        view[int(plan_id)] = {
            "user_id": int(user_id),
            "username": username,
            "display_name": display,
            "cred_status": status,
            "cred_label": uc.cred_label(status),
            "cred_tone": uc.cred_tone(status),
        }
    return view


@router.get("/admin/plans")
def plans_list(request: Request):
    session = require_admin(request)
    ctx = get_ctx(request)
    plans = repo.list_plans(ctx.db)
    now = datetime.now()
    return render(
        request, "plans.html", session, title="预约计划",
        plans=plans,
        # 来源（管理端 / 用户端）：{plan_id: 来源代码} + 代码→标签表。没有对应行的老计划
        # 按管理端解释，这正是它们的真实来源，不是猜测（见 repository.PLAN_ORIGIN_LABELS）。
        origins=repo.plan_origins(ctx.db),
        origin_labels=repo.PLAN_ORIGIN_LABELS,
        # 所属用户（多用户）：执行这条计划时用的是谁的学校凭据。
        plan_owners=_plan_owner_view(ctx),
        window_states={p["id"]: plan_window_state(p, now=now) for p in plans},
        app_setting=repo.get_app_setting(ctx.db),
    )


def _form_values(fallback: dict | None = None) -> dict:
    values = dict(_PLACEHOLDER_EXAMPLE)
    values.setdefault("repeat_rule", "once")
    values.setdefault("regionid", "")
    values.setdefault("max_attempts_per_seat", 3)
    values.setdefault("preferred_seats", "")
    values.setdefault("preferred_prefixes", "")
    values.setdefault("confirmed_bookable_states", '[["2", 1, false]]')
    values.setdefault("note", "")
    values.setdefault("name", "")
    # 简化表单的取值。它们与上面的技术字段**同源**（都由同一批默认值/已存计划推出），
    # 因此界面不会出现「模式说一套、时间戳说另一套」。
    values.setdefault("mode", repeat_rule_to_plan_mode(values["repeat_rule"]))
    values.setdefault("weekdays_only", "")
    values.setdefault("trigger_clock", clock_of(values["trigger_at"]))
    values.setdefault("use_start", clock_of(values["book_start"]))
    values.setdefault("use_end", clock_of(values["book_end"]))
    values.setdefault("regionid_manual", "")
    if fallback:
        values.update({k: v for k, v in fallback.items() if v is not None})
    # 回显失败的提交时，时刻控件也要跟着用户填的值走（trigger_at 可能是 HH:MM:SS）
    values["trigger_clock"] = clock_of(values.get("trigger_at"))
    return values


def _region_labels(db) -> dict:
    """``{区域ID字符串: 区域记录}``，供模板把裸数字显示成可读名称。"""
    return {str(r["regionid"]): r for r in repo.list_catalog_regions(db)}


def _apply_picks(db, values: dict, *, existing_seats=None, user_id=None) -> tuple[dict, dict]:
    """把座位目录的待选草稿并入计划表单的**初始取值**（仅影响渲染）。

    合并规则：计划原有座位在前（保持既有优先级），草稿按选择顺序追加在后，去重。
    草稿恰好来自一个区域时，``regionid`` 也被自动带入 —— 这就是「自动绑定真实 ID」。

    ``user_id`` 给了就只用**这个人自己**的草稿（用户端）；不给就用管理端那份全局草稿。
    两条路径共用同一段合并逻辑，因此两端的填表行为不会各自漂移。

    提交时 ``parse_plan`` 仍然只读 textarea，所以目录只是「填表助手」，
    不是新的数据来源，也没有改变任何既有语义。
    """
    picks = repo.list_picks(db) if user_id is None else repo.list_user_picks(db, int(user_id))
    regionids = sorted({p["regionid"] for p in picks})
    # 「区域 ID 已自动绑定」只在草稿恰好来自**一个**区域时才成立。这里把判断
    # 结果记进 pick_info，并与下面真正赋值的那一句共用同一个值：模板据此显示
    # 提示，就不会出现「已自动绑定」和「没有自动改写」同页并存的自相矛盾。
    bound_regionid = regionids[0] if len(regionids) == 1 else None
    info = {
        "count": len(picks),
        "regionids": regionids,
        "missing": [p["seat_name"] for p in picks if not p["known"]],
        "bound_regionid": bound_regionid,
    }
    if not picks:
        return values, info
    merged = list(existing_seats or [])
    for pick in picks:
        if pick["seat_name"] not in merged:
            merged.append(pick["seat_name"])
    values["preferred_seats"] = "\n".join(merged)
    if bound_regionid is not None:
        values["regionid"] = bound_regionid
    return values, info


def _plan_form_ctx(db, values: dict, *, existing_seats=None) -> dict:
    """计划表单模板的额外上下文（目录区域名 + 待选草稿提示）。"""
    values, pick_info = _apply_picks(db, values, existing_seats=existing_seats)
    return {
        "values": values,
        "pick_info": pick_info,
        "region_labels": _region_labels(db),
        "repeat_rules": REPEAT_RULES,
        "plan_modes": PLAN_MODES,
        "mode_labels": PLAN_MODE_LABELS,
    }


def _resolve_regionid(form: dict) -> None:
    """把「下拉选中的区域」与「手工填写的区域 ID」归并成唯一的 ``regionid``。

    两种方式都填且不一致时**直接报错**，绝不静默挑一个：区域 ID 是唯一无法从
    接口查回来的参数（接口没有区域列表，也不返回区域字段），猜错的后果是整天
    盯着另一个区域抢座位。
    """
    selected = str(form.get("regionid") or "").strip()
    manual = str(form.get("regionid_manual") or "").strip()
    if not manual:
        return
    if selected and selected != manual:
        raise ValidationError(
            [f"区域填写了两处且不一致（下拉框 {selected}，手工填写 {manual}）：请只保留一处。"]
        )
    form["regionid"] = manual


def _now() -> datetime:
    """当前时间。单独留一个函数，方便测试注入固定时刻。"""
    return datetime.now()


def _validated_plan_form(form: dict, *, now: datetime | None = None) -> tuple[Any, list[str]]:
    """提交路径的唯一入口：简化表单 → 技术字段 → ``parse_plan`` → 必死拦截。

    放在一处，是因为创建与编辑必须走完全相同的规则；分散实现迟早会出现
    「新建拦住了、编辑没拦住」这类不一致。
    """
    now = now or _now()
    _resolve_regionid(form)
    data, warnings = parse_plan(expand_plan_form(form, today=now.date()), today=now.date())
    # 「必死」拦截只作用于简化表单（带 mode 的那种）。它不是 ``parse_plan`` 的规则，
    # 而是给普通用户的一道保护：按模式 + 日期 + 时刻算出来「这个计划永远不会执行」。
    # 手工拼技术字段的提交一律照旧 —— 与 expand_plan_form 同一条兼容约定：
    # 技术字段那条路逐字节保持原行为。
    simple_form = str(form.get("mode") or "").strip() in PLAN_MODES
    if simple_form and data.repeat_rule == "once":
        dead = one_off_plan_is_dead(
            run_date=data.run_date, book_start=data.book_start, book_end=data.book_end, now=now
        )
        if dead:
            raise ValidationError([dead])
    hint = plan_will_start_at_once(
        run_date=data.run_date, trigger_at=data.trigger_at, book_end=data.book_end, now=now
    )
    if hint:
        warnings = [*warnings, hint]
    return data, warnings


def _plan_form_error(request, session, ctx, *, plan_id: int | None, title: str,
                     form: dict, errors: list[str]):
    """校验失败时原样回显表单（200），与成功路径分开以免误报「已保存」。"""
    return render(
        request, "plan_form.html", session, title=title,
        plan=repo.get_plan(ctx.db, plan_id) if plan_id else None,
        errors=errors, warnings=[],
        values=_form_values(form),
        pick_info={"count": 0, "regionids": [], "missing": [], "bound_regionid": None},
        region_labels=_region_labels(ctx.db),
        repeat_rules=REPEAT_RULES,
        plan_modes=PLAN_MODES,
        mode_labels=PLAN_MODE_LABELS,
    )


@router.get("/admin/plans/new")
def plan_new_form(request: Request):
    session = require_admin(request)
    ctx = get_ctx(request)
    defaults = _form_values()
    defaults["regionid"] = ctx.settings.regionid
    extra = _plan_form_ctx(ctx.db, defaults)
    return render(request, "plan_form.html", session, title="新建预约",
                  plan=None, errors=[], warnings=[], **extra)


@router.post("/admin/plans/new")
async def plan_new_submit(request: Request):
    session = require_admin(request)
    form = dict(await request.form())
    check_csrf(form, session)
    ctx = get_ctx(request)
    try:
        data, warnings = _validated_plan_form(form)
    except ValidationError as exc:
        return _plan_form_error(request, session, ctx, plan_id=None, title="新建预约",
                               form=form, errors=exc.errors)
    # 新建的计划一律「不武装」：必须由使用者显式启用，避免启动服务即产生真实预约
    plan_id = repo.create_plan(ctx.db, data, enabled=False)
    # 草稿已经变成计划内容，清掉以免串到下一个计划
    repo.clear_picks(ctx.db)
    msg = "预约已保存，当前为「已暂停」。确认参数无误后点击「启用」才会开始执行。"
    if warnings:
        msg += " 提示：" + "；".join(warnings)
    return redirect(f"/admin/plans/{plan_id}", msg=msg)


def _plan_values(plan: dict) -> dict:
    return {
        "name": plan["name"],
        "run_date": plan["run_date"],
        "repeat_rule": plan["repeat_rule"],
        "trigger_at": plan["trigger_at"],
        "book_start": plan["book_start"],
        "book_end": plan["book_end"],
        "regionid": plan["regionid"],
        "preferred_seats": "\n".join(plan["preferred_seats"]),
        "preferred_prefixes": "\n".join(plan["preferred_prefixes"]),
        "confirmed_bookable_states": _json_combos(plan["confirmed_bookable_states"]),
        "max_attempts_per_seat": plan["max_attempts_per_seat"],
        "note": plan["note"],
        # 简化表单的反向映射：模式与三个时刻都由已存的技术字段推出，不另存一份。
        "mode": repeat_rule_to_plan_mode(plan["repeat_rule"]),
        "weekdays_only": "1" if plan["repeat_rule"] == "weekdays" else "",
        "trigger_clock": clock_of(plan["trigger_at"]),
        "use_start": clock_of(plan["book_start"]),
        "use_end": clock_of(plan["book_end"]),
        "regionid_manual": "",
    }


def _json_combos(combos) -> str:
    import json

    return json.dumps(combos, ensure_ascii=False)


@router.get("/admin/plans/{plan_id}")
def plan_detail(request: Request, plan_id: int):
    session = require_admin(request)
    ctx = get_ctx(request)
    plan = repo.get_plan(ctx.db, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="计划不存在")
    now = _now()
    return render(
        request, "plan_detail.html", session, title=f"预约 #{plan_id}",
        plan=plan,
        # 与主页同一套说法：详情页不能把 trigger_at / book_start 这些原始字段当门面。
        when_label=plan_when_label(plan, now=now),
        seat_window=(clock_of(plan["book_start"]), clock_of(plan["book_end"])),
        # 原始技术字段仍然如实展示（折叠），JSON 用 JSON 的写法而不是 Python repr。
        combos_text=_json_combos(plan["confirmed_bookable_states"]),
        # 来源：管理端自己建的，还是用户端提交上来的。管理端是统一数据入口，
        # 因此这里必须能区分 —— 否则「这条是谁建的」要靠猜。
        origin=repo.origin_label(repo.get_plan_origin(ctx.db, plan_id)),
        # 所属用户：启用这条计划后提交用的是**这个人**的学校凭据。
        plan_owner=_plan_owner_view(ctx).get(int(plan_id)),
        runs=repo.list_runs(ctx.db, plan_id=plan_id, limit=30),
        last_run=repo.last_run_for_plan(ctx.db, plan_id),
        run_duration_text=repo.run_duration_text,
    )


@router.get("/admin/plans/{plan_id}/edit")
def plan_edit_form(request: Request, plan_id: int):
    session = require_admin(request)
    ctx = get_ctx(request)
    plan = repo.get_plan(ctx.db, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="计划不存在")
    if plan["status"] is PlanStatus.RUNNING:
        return redirect(f"/admin/plans/{plan_id}", msg="计划正在运行中，请先暂停或停止后再编辑。", ok=False)
    extra = _plan_form_ctx(ctx.db, _plan_values(plan), existing_seats=plan["preferred_seats"])
    return render(request, "plan_form.html", session, title=f"编辑预约 #{plan_id}",
                  plan=plan, errors=[], warnings=[], **extra)


@router.post("/admin/plans/{plan_id}/edit")
async def plan_edit_submit(request: Request, plan_id: int):
    session = require_admin(request)
    form = dict(await request.form())
    check_csrf(form, session)
    ctx = get_ctx(request)
    plan = repo.get_plan(ctx.db, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="计划不存在")
    if plan["status"] is PlanStatus.RUNNING:
        return redirect(f"/admin/plans/{plan_id}", msg="计划正在运行中，不能编辑。", ok=False)
    try:
        data, warnings = _validated_plan_form(form)
    except ValidationError as exc:
        return _plan_form_error(request, session, ctx, plan_id=plan_id,
                               title=f"编辑预约 #{plan_id}", form=form, errors=exc.errors)
    repo.update_plan(ctx.db, plan_id, data)
    # 草稿已经并入计划内容，清掉以免串到下一个计划
    repo.clear_picks(ctx.db)
    msg = "预约已保存（状态未改变）。"
    if warnings:
        msg += " 提示：" + "；".join(warnings)
    return redirect(f"/admin/plans/{plan_id}", msg=msg)


@router.post("/admin/plans/{plan_id}/pause")
async def plan_pause(request: Request, plan_id: int):
    return await _plan_action(request, plan_id, "pause")


@router.post("/admin/plans/{plan_id}/stop")
async def plan_stop(request: Request, plan_id: int):
    return await _plan_action(request, plan_id, "stop")


@router.post("/admin/plans/{plan_id}/resume")
async def plan_resume(request: Request, plan_id: int):
    return await _plan_action(request, plan_id, "resume")


@router.post("/admin/plans/{plan_id}/run")
async def plan_run(request: Request, plan_id: int):
    return await _plan_action(request, plan_id, "run")


@router.post("/admin/plans/{plan_id}/delete")
async def plan_delete(request: Request, plan_id: int):
    return await _plan_action(request, plan_id, "delete")


async def _plan_action(request: Request, plan_id: int, action: str):
    session = require_admin(request)
    form = dict(await request.form())
    check_csrf(form, session)
    ctx = get_ctx(request)
    plan = repo.get_plan(ctx.db, plan_id)
    if plan is None:
        return redirect("/admin/plans", msg="计划不存在。", ok=False)

    if action == "pause":
        result = repo.pause_plan(ctx.db, plan_id)
    elif action == "stop":
        result = repo.stop_plan(ctx.db, plan_id)
    elif action == "resume":
        ack = str(form.get("acknowledge_unknown") or "") in ("1", "on", "true")
        if plan["status"] is PlanStatus.UNKNOWN and not ack:
            return redirect(f"/admin/plans/{plan_id}",
                            msg="结果未知的计划必须勾选「我已核实图书馆预约记录」后才能恢复。",
                            ok=False)
        result = repo.resume_plan(ctx.db, plan_id, acknowledge_unknown=ack)
    elif action == "run":
        if ctx.scheduler is None:
            return redirect(f"/admin/plans/{plan_id}", msg="调度器未启用，无法立即执行。", ok=False)
        result = ctx.scheduler.request_run(plan_id)
    elif action == "delete":
        if repo.plan_has_active_run(ctx.db, plan_id):
            return redirect(f"/admin/plans/{plan_id}", msg="计划正在运行中，不能删除。请先停止。", ok=False)
        repo.delete_plan(ctx.db, plan_id)
        return redirect("/admin/plans", msg=f"计划 #{plan_id} 已删除。")
    else:  # pragma: no cover - 路由已限定
        raise HTTPException(status_code=400, detail="未知操作")

    logger.info("计划 #%s 执行操作 %s：%s", plan_id, action, result.get("message"))
    target = "/admin/plans" if action in ("pause", "stop", "resume") and result.get("ok") and str(form.get("back") or "") == "list" else f"/admin/plans/{plan_id}"
    return redirect(target, msg=result.get("message", ""), ok=bool(result.get("ok")))


# ================================================================== 座位目录
#
# 目录只记录「服务器返回过这个座位」，**不代表它当前可预约**。
# 它唯一的作用是帮用户把区域 ID 与座位名填进计划表单——提交时的唯一事实来源
# 仍然是计划表单里的 textarea（parse_plan 只读它，plan 表一列未改）。


def _int_param(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _seat_key(regionid: int, seat_name: str) -> str:
    """复选框的值：``区域ID|座位名``。跨区域搜索时也不会混淆。"""
    return f"{regionid}|{seat_name}"


def _catalog_view(ctx, request: Request, *, user_id: int | None = None) -> dict:
    """汇总目录页要展示的全部只读数据。

    管理端与用户端**共用**这一个装配器（搜索、楼层、跨区域、截断等语义因此完全
    一致），区别只有两处，都由 ``user_id`` 决定：

    - 待选草稿：``user_id`` 给了就读**该用户自己**那一份（``user_seat_pick``），
      否则读管理端那份全局草稿（``seat_pick``）；
    - 收藏：只有管理端视图才装配（``seat_favorite`` 是全局的，没有按人区分），
      用户端一律给空列表 —— 别人的书签不该出现在你的页面上下文里。
    """
    regionid = _int_param(request.query_params.get("region"))
    floor = str(request.query_params.get("floor") or "").strip()
    q = str(request.query_params.get("q") or "").strip()
    only_fav = request.query_params.get("fav") == "1" and user_id is None

    regions = repo.list_catalog_regions(ctx.db)
    region_map = {r["regionid"]: r for r in regions}
    if regionid is not None and regionid not in region_map:
        regionid = None  # 目录里没有这个区域：当作「未选择」处理，避免伪造名称

    # 只有「选中了区域」或「有关键词/只看收藏」时才取座位列表：
    # 否则会把整个目录拉出来渲染，既慢又没用。
    scoped = regionid is not None or bool(q) or only_fav
    seats = repo.list_catalog_seats(
        ctx.db, regionid=regionid, floor=floor, q=q, limit=CATALOG_SEAT_LIMIT + 1,
    ) if scoped else []
    truncated = len(seats) > CATALOG_SEAT_LIMIT
    seats = seats[:CATALOG_SEAT_LIMIT]

    favorites = repo.list_favorites(ctx.db, regionid=regionid) if user_id is None else []
    if only_fav:
        fav_names = {(f["regionid"], f["seat_name"]) for f in favorites}
        seats = [s for s in seats if (s["regionid"], s["seat_name"]) in fav_names]

    picks = (
        repo.list_picks(ctx.db) if user_id is None
        else repo.list_user_picks(ctx.db, int(user_id))
    )

    return {
        "regionid": regionid,
        "region_label": region_map[regionid]["region_label"] if regionid else "",
        "region_label_source": region_map[regionid]["label_source"] if regionid else "",
        "floor": floor,
        "q": q,
        "only_fav": only_fav,
        "scoped": scoped,
        # 跨区域搜索时列表要标出每个座位属于哪个区域
        "cross_region": regionid is None and bool(seats),
        "regions": regions,
        "seats": seats,
        "truncated": truncated,
        "seat_limit": CATALOG_SEAT_LIMIT,
        "floors": repo.list_catalog_floors(ctx.db, regionid) if regionid else [],
        "favorites": favorites,
        "favorite_keys": {(f["regionid"], f["seat_name"]) for f in favorites},
        "picks": picks,
        "picked": {(p["regionid"], p["seat_name"]) for p in picks},
    }


@router.get("/admin/catalog")
def catalog_page(request: Request):
    session = require_admin(request)
    ctx = get_ctx(request)
    view = _catalog_view(ctx, request)
    return render(
        request, "catalog.html", session, title="座位目录",
        view=view,
        catalog_state=ctx.catalog_service.state() if ctx.catalog_service else None,
        status_labels=CATALOG_STATUS_LABELS,
        return_to=str(request.query_params.get("return") or ""),
        plan_id=_int_param(request.query_params.get("plan_id")),
        total_seats=repo.count_catalog_seats(ctx.db),
        seat_key=_seat_key,
    )


@router.post("/admin/catalog/refresh")
async def catalog_refresh(request: Request):
    session = require_admin(request)
    form = await request.form()
    check_csrf(form, session)
    ctx = get_ctx(request)
    if ctx.catalog_service is None:
        return redirect("/admin/catalog", msg="座位目录服务未启用。", ok=False)
    result = ctx.catalog_service.refresh(source="manual")
    logger.info("座位目录刷新：status=%s", result.get("status"))
    return redirect("/admin/catalog", msg=result["message"], ok=bool(result["ok"]))


@router.post("/admin/catalog/regions")
async def catalog_add_region(request: Request):
    session = require_admin(request)
    form = await request.form()
    check_csrf(form, session)
    ctx = get_ctx(request)
    regionid = _int_param(form.get("regionid"))
    if regionid is None or regionid < 1:
        return redirect("/admin/catalog", msg="区域 ID 必须是正整数。", ok=False)
    if ctx.catalog_service is None:
        return redirect("/admin/catalog", msg="座位目录服务未启用。", ok=False)
    result = ctx.catalog_service.add_region(regionid)
    return redirect(f"/admin/catalog?region={regionid}", msg=result["message"], ok=result["ok"])


@router.post("/admin/catalog/regions/{regionid}/rename")
async def catalog_rename_region(request: Request, regionid: int):
    session = require_admin(request)
    form = await request.form()
    check_csrf(form, session)
    ctx = get_ctx(request)
    if ctx.catalog_service is None:
        return redirect("/admin/catalog", msg="座位目录服务未启用。", ok=False)
    result = ctx.catalog_service.rename_region(regionid, str(form.get("region_label") or ""))
    return redirect(f"/admin/catalog?region={regionid}", msg=result["message"], ok=result["ok"])


@router.post("/admin/catalog/import")
async def catalog_import(request: Request):
    """离线导入：把一份 layout/query 响应 JSON 粘贴进来解析入库（不访问网络）。"""
    session = require_admin(request)
    form = await request.form()
    check_csrf(form, session)
    ctx = get_ctx(request)
    if ctx.catalog_service is None:
        return redirect("/admin/catalog", msg="座位目录服务未启用。", ok=False)
    regionid = _int_param(form.get("regionid"))
    if regionid is None or regionid < 1:
        return redirect("/admin/catalog", msg="导入前必须填写区域 ID（正整数）。", ok=False)
    result = ctx.catalog_service.import_json(str(form.get("payload") or ""), regionid)
    return redirect(f"/admin/catalog?region={regionid}",
                    msg=result["message"], ok=bool(result["ok"]))


@router.post("/admin/catalog/picks")
async def catalog_apply_picks(request: Request):
    """把当前视图里勾选的座位写成待选草稿。

    语义：**当前视图范围内**的勾选状态整体生效（勾上=加入，取消=移出），
    视图之外的草稿原样保留 —— 因此可以换区域、换楼层慢慢攒选择。
    """
    session = require_admin(request)
    form = await request.form()
    check_csrf(form, session)
    ctx = get_ctx(request)
    regionid = _int_param(form.get("region"))
    seats = repo.list_catalog_seats(
        ctx.db,
        regionid=regionid,
        floor=str(form.get("floor") or "").strip(),
        q=str(form.get("q") or "").strip(),
        limit=CATALOG_SEAT_LIMIT,
    )
    scoped = {(s["regionid"], s["seat_name"]) for s in seats}
    checked = set(form.getlist("seat"))
    # 视图外的草稿保持原顺序保留
    kept = [
        (p["regionid"], p["seat_name"]) for p in repo.list_picks(ctx.db)
        if (p["regionid"], p["seat_name"]) not in scoped
    ]
    # 视图内的按**渲染顺序**加入，与页面上看到的顺序一致（即优先级顺序）
    chosen = [
        (s["regionid"], s["seat_name"]) for s in seats
        if _seat_key(s["regionid"], s["seat_name"]) in checked
    ]
    count = repo.replace_picks(ctx.db, kept + chosen)
    return redirect(
        _catalog_return_url(form, regionid),
        msg=f"已应用选择：当前共 {count} 个座位。可继续换区域/楼层挑选，或回到计划页确认。",
    )


@router.post("/admin/catalog/picks/remove")
async def catalog_remove_pick(request: Request):
    session = require_admin(request)
    form = await request.form()
    check_csrf(form, session)
    ctx = get_ctx(request)
    target = (str(form.get("regionid") or ""), str(form.get("seat_name") or ""))
    kept = [
        (p["regionid"], p["seat_name"]) for p in repo.list_picks(ctx.db)
        if (str(p["regionid"]), p["seat_name"]) != target
    ]
    repo.replace_picks(ctx.db, kept)
    return redirect(_catalog_return_url(form, _int_param(form.get("region"))),
                    msg=f"已移出选择：{target[1]}")


@router.post("/admin/catalog/picks/move")
async def catalog_move_pick(request: Request):
    """候选座位上移/下移一位 —— 顺序即计划里的优先级顺序。

    越界（已在最前/最后）是**无害空操作**：如实回一句提示，不改任何数据。
    """
    session = require_admin(request)
    form = await request.form()
    check_csrf(form, session)
    ctx = get_ctx(request)
    regionid = _int_param(form.get("regionid"))
    seat_name = str(form.get("seat_name") or "").strip()
    if regionid is None or not seat_name:
        return redirect("/admin/catalog", msg="移动候选座位需要区域与座位名。", ok=False)
    result = repo.move_pick(ctx.db, regionid, seat_name, str(form.get("direction") or ""))
    return redirect(
        _catalog_return_url(form, _int_param(form.get("region"))),
        msg=result["message"], ok=bool(result["ok"]),
    )


@router.post("/admin/catalog/picks/clear")
async def catalog_clear_picks(request: Request):
    session = require_admin(request)
    form = await request.form()
    check_csrf(form, session)
    ctx = get_ctx(request)
    count = repo.clear_picks(ctx.db)
    return redirect(_catalog_return_url(form, _int_param(form.get("region"))),
                    msg=f"已清空待选座位（{count} 个）。")


@router.post("/admin/catalog/favorites")
async def catalog_add_favorite(request: Request):
    session = require_admin(request)
    form = await request.form()
    check_csrf(form, session)
    ctx = get_ctx(request)
    regionid = _int_param(form.get("regionid"))
    seat_name = str(form.get("seat_name") or "").strip()
    if regionid is None or not seat_name:
        return redirect("/admin/catalog", msg="收藏需要区域与座位名。", ok=False)
    created = repo.add_favorite(ctx.db, regionid, seat_name)
    msg = f"已收藏：{seat_name}" if created else f"已经在收藏里了：{seat_name}"
    return redirect(_catalog_return_url(form, regionid), msg=msg)


@router.post("/admin/catalog/favorites/delete")
async def catalog_remove_favorite(request: Request):
    session = require_admin(request)
    form = await request.form()
    check_csrf(form, session)
    ctx = get_ctx(request)
    regionid = _int_param(form.get("regionid"))
    seat_name = str(form.get("seat_name") or "").strip()
    if regionid is None or not seat_name:
        return redirect("/admin/catalog", msg="取消收藏需要区域与座位名。", ok=False)
    repo.remove_favorite(ctx.db, regionid, seat_name)
    return redirect(_catalog_return_url(form, regionid), msg=f"已取消收藏：{seat_name}")


def _catalog_return_url(form, regionid) -> str:
    """操作后回到「计划表单」还是「目录页」。"""
    ret = str(form.get("return") or "")
    if ret == "plan_new":
        return "/admin/plans/new"
    if ret == "plan_edit":
        plan_id = _int_param(form.get("plan_id"))
        if plan_id is not None:
            return f"/admin/plans/{plan_id}/edit"
    return f"/admin/catalog?region={regionid}" if regionid else "/admin/catalog"


# ================================================================== 运行记录


@router.get("/admin/runs")
def runs_list(request: Request):
    session = require_admin(request)
    ctx = get_ctx(request)
    plan_id = request.query_params.get("plan_id")
    try:
        plan_id_int = int(plan_id) if plan_id else None
    except ValueError:
        plan_id_int = None
    return render(
        request, "runs.html", session, title="运行记录",
        runs=repo.list_runs(ctx.db, plan_id=plan_id_int, limit=100),
        plan_id=plan_id_int,
        # 每条运行所属计划的来源：管理端 / 用户端（用户端提交的计划也会在这里出现）。
        origins=repo.plan_origins(ctx.db),
        origin_labels=repo.PLAN_ORIGIN_LABELS,
        # 以及「这次运行用的是谁的账号」——运行记录必须能一路追到归属用户。
        plan_owners=_plan_owner_view(ctx),
        run_duration_text=repo.run_duration_text,
    )


@router.get("/admin/runs/{run_id}")
def run_detail(request: Request, run_id: int):
    session = require_admin(request)
    ctx = get_ctx(request)
    run = repo.get_run(ctx.db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    plan = repo.get_plan(ctx.db, run["plan_id"])
    return render(request, "run_detail.html", session, title=f"运行 #{run_id}",
                  run=run, plan=plan, events=repo.list_run_events(ctx.db, run_id),
                  origin=repo.origin_label(repo.get_plan_origin(ctx.db, run["plan_id"])),
                  plan_owner=_plan_owner_view(ctx).get(int(run["plan_id"])),
                  run_duration_text=repo.run_duration_text)


# ================================================================== 认证状态


@router.get("/admin/auth")
def auth_page(request: Request):
    session = require_admin(request)
    ctx = get_ctx(request)
    steps = ctx.auth_service.recovery_steps() if ctx.auth_service else []
    view = ctx.auth_service.current() if ctx.auth_service else None
    return render(request, "auth.html", session, title="认证状态",
                  view=view,
                  events=repo.list_auth_events(ctx.db, limit=30),
                  steps=steps,
                  relogin_hint=(ctx.auth_service.relogin_hint() if ctx.auth_service else ""),
                  # 等待期间才自动刷新：日常浏览不该被强制重载。
                  # 用 <meta http-equiv="refresh"> 而非 JS —— 不影响 CSP 的 script-src 'self'。
                  #
                  # **必须问 active_login_state()，不能直接看 view.login_state**：进程被强杀
                  # （或工作线程崩了）会在元数据里留下一条永远不变的 ``awaiting_login``，
                  # 直接看它，这个页面就会每 5 秒自刷一次、**永远不停** —— 与
                  #「绝不无限加载/无限刷新」相冲突，而且用户看到的永远是"正在登录"。
                  # active_login_state() 带残留宽限（login_state_is_stale），过期即视为
                  # 没有会话在跑，页面于是停止自刷并显示真实状态。
                  waiting=bool(view and request.query_params.get("wait")
                               and _live_login_state(ctx) in ("launching", "awaiting_login")))


def _live_login_state(ctx) -> str:
    """当前是否真有登录流程在跑（已排除残留状态）；没有则空串。"""
    from haut_seat.services.auth_login_service import active_login_state

    return str(active_login_state(ctx.settings, ctx.db) or "")


@router.post("/admin/auth/check")
async def auth_check(request: Request):
    session = require_admin(request)
    form = dict(await request.form())
    check_csrf(form, session)
    ctx = get_ctx(request)
    if ctx.auth_service is None:
        return redirect("/admin/auth", msg="认证服务未启用。", ok=False)
    check = ctx.auth_service.check_now(source="manual")
    ok = check.status is AuthStatus.VALID
    return redirect("/admin/auth", msg=f"预检结果：{check.status.value} — {check.message}", ok=ok)


@router.post("/admin/auth/login-browser")
async def auth_login_browser(request: Request):
    """一键**人工**登录：只登记并起脱离进程，绝不在请求线程里跑浏览器。

    授权链与其它 POST 逐字一致：``require_admin`` + ``check_csrf`` + 303 PRG。
    """
    session = require_admin(request)
    form = dict(await request.form())
    check_csrf(form, session)
    ctx = get_ctx(request)
    launcher = ctx.auth_login_launcher
    if launcher is None:
        return redirect("/admin/auth", msg="登录启动器不可用；请改用命令行 python -m haut_seat auth-login。",
                        ok=False)
    try:
        result = launcher.spawn()
    except Exception as exc:
        # 绝不让启动器的异常把令牌之类的东西带进跳转 URL：只报异常类型
        return redirect("/admin/auth", msg=f"启动登录窗口失败（{type(exc).__name__}）。"
                                           "请改用命令行 python -m haut_seat auth-login。", ok=False)
    return redirect("/admin/auth?wait=1", msg=str(result.get("message") or ""),
                    ok=bool(result.get("ok")))


@router.post("/admin/auth/reload")
async def auth_reload(request: Request):
    """只读"重新读取凭据来源"：**不发任何网络请求**。

    用途：你刚在终端里跑完 ``auth-login`` 生成了新快照，希望面板立刻按新来源显示，
    但不想顺带做一次昂贵的在线预检。需要真正确认登录是否有效时，请点"重新检测登录状态"。
    """
    session = require_admin(request)
    form = dict(await request.form())
    check_csrf(form, session)
    ctx = get_ctx(request)

    creds, reason = _resolve_credentials_for_display(ctx.settings)
    record_credential_source(
        ctx.db,
        source=(creds.source if creds is not None else "none"),
        snapshot_present=bool(creds is not None and creds.source == "snapshot"),
        captured_at=(creds.captured_at if creds is not None else None),
        fingerprint=(creds.fingerprint() if creds is not None else ""),
    )
    if creds is None:
        return redirect("/admin/auth", msg=f"凭据不可用：{reason}", ok=False)
    label = creds.source_label
    extra = f"（指纹 {creds.fingerprint()}）" if creds.fingerprint() else ""
    return redirect("/admin/auth", msg=f"已重新读取凭据来源：{label}{extra}。"
                                       "本次未发起任何网络请求。", ok=True)


def _resolve_credentials_for_display(settings):
    """解析凭据供展示；失败时返回 (None, 原因)。只读文件，不发网络请求。"""
    try:
        return resolve_credentials(settings), ""
    except CredentialsUnavailableError as exc:
        return None, str(exc)
    except Exception as exc:
        return None, f"{type(exc).__name__}"


@router.post("/admin/auth/recover")
async def auth_recover(request: Request):
    session = require_admin(request)
    form = dict(await request.form())
    check_csrf(form, session)
    ctx = get_ctx(request)
    if ctx.auth_service is None:
        return redirect("/admin/auth", msg="认证服务未启用。", ok=False)
    result = ctx.auth_service.begin_recovery()
    return redirect("/admin/auth", msg=result["message"], ok=True)


@router.post("/admin/auth/verify")
async def auth_verify(request: Request):
    session = require_admin(request)
    form = dict(await request.form())
    check_csrf(form, session)
    ctx = get_ctx(request)
    if ctx.auth_service is None:
        return redirect("/admin/auth", msg="认证服务未启用。", ok=False)
    result = ctx.auth_service.verify_recovery(source="manual")
    return redirect("/admin/auth", msg=result["message"], ok=bool(result["ok"]))


# ============================================================== 远程浏览器登录
#
# 服务器上没有桌面，``headless=False`` 的登录窗口在部署环境里必然起不来
# （见 ``services/auth_login_service.py`` 的模块说明）。这一节让管理员**在面板里
# 直接操作服务器上的无头 Chromium**：画面以 JPEG 帧轮询取回，键鼠事件转发过去。
#
# 为什么是"截图轮询"而不是 iframe / 视频流（三个都已核实的硬约束）：
# 1. 本应用给自己发的响应头里有 ``X-Frame-Options: DENY`` 与
#    ``frame-ancestors 'none'``（``web/app.py``）⇒ 用 iframe 嵌自己的页面
#    会被自己的头拦死；
# 2. CSP 的 ``img-src`` 只有 ``'self' data:``，**没有** ``blob:``
#    ⇒ ``URL.createObjectURL`` 的视频/画面会被 CSP 拦掉；
# 3. 全仓库没有任何 WebSocket / SSE / StreamingResponse 基础设施。
# 而 ``<img>`` + fetch 轮询**零新依赖、零 CSP 改动、零响应头改动**。
#
# 本节的授权链在既有"已登录会话 + CSRF"之上再叠同源校验（管理端其它 POST 没有
# 这道，远程端点值得多一层）。**绝不**记录 ``/input`` 的请求体：那逐字就是
# 学校口令的键盘记录，脱敏函数兜不住。


def _remote_browser_view(ctx, session):
    """页面视角的会话视图。

    - 没有会话 ⇒ ``idle_view()``（"没有你的会话"）；
    - 会话属于**别人** ⇒ 同样按"没有你的会话"渲染：连"有人正在用"都不透露
      （``view_for`` 返回 ``None``）。JSON 端点才用 404 区分这两种情形。
    """
    mgr = ctx.remote_browser
    if mgr is None:
        return None
    view = mgr.view_for(session.admin_id, session.token_hash)
    return mgr.idle_view() if view is None else view


@router.get("/admin/auth/browser")
def remote_browser_page(request: Request):
    session = require_admin(request)
    ctx = get_ctx(request)
    available, reason = browser_availability()
    return render(
        request, "remote_browser.html", session, title="远程浏览器登录",
        view=_remote_browser_view(ctx, session),
        available=available,
        unavailable_reason=reason,
        enabled=bool(ctx.settings.auth_remote_enabled),
        # 明文链路的告警由这两个值驱动：https 之外还允许启动时才红字提示。
        secure=(request.url.scheme == "https"),
        allow_insecure=bool(ctx.settings.auth_remote_allow_insecure),
        auth_source=str(getattr(ctx.settings, "auth_source", "") or ""),
        # 画面刷新间隔交给页面：服务端每 600ms 才产出一帧，页面刷得更快没有意义。
        poll_ms=int(ctx.settings.auth_remote_frame_interval_ms),
    )


@router.get("/admin/auth/browser/frame")
def remote_browser_frame(request: Request):
    """最新一帧画面。纯 ``<img>`` 的 ``src``，因此**不发 ETag**。"""
    session = require_admin(request)
    ctx = get_ctx(request)
    mgr = ctx.remote_browser
    if mgr is None or mgr.view_for(session.admin_id, session.token_hash) is None:
        # 会话属于别人：404，不泄露任何细节、更不返回一个字节的画面。
        raise HTTPException(status_code=404, detail="没有正在进行的远程浏览器会话")
    frame = mgr.frame_for(session.admin_id, session.token_hash)
    if frame is None:
        # 是自己的会话但画面还没出来（浏览器正在启动）：204 空响应。
        # ``<img>`` 对 204 的处理就是"什么都不显示"，正好是想要的效果。
        return Response(status_code=204)
    return Response(content=frame, media_type="image/jpeg")


@router.get("/admin/auth/browser/state")
def remote_browser_state(request: Request):
    """状态 JSON。给**机器**读的端点：未登录用 401，不用 303 跳转。

    303 会让 ``fetch`` 拿到登录页的 HTML（解析失败），页面只能显示"网络错误"，
    而真正的原因（会话过期）就被掩盖了 —— 这里要如实告诉用户。
    """
    session = optional_admin(request)
    if session is None:
        return JSONResponse(
            {"ok": False, "expired": True,
             "message": "管理员登录会话已失效，请重新登录后再看这次远程浏览器会话。"},
            status_code=401,
        )
    ctx = get_ctx(request)
    mgr = ctx.remote_browser
    if mgr is None or mgr.view_for(session.admin_id, session.token_hash) is None:
        return JSONResponse(
            {"ok": False, "expired": True,
             "message": "这次远程浏览器会话不属于当前登录的管理员（可能已结束或已重新开始）。"},
            status_code=404,
        )
    payload = {
        "ok": True,
        "expired": False,
        "secure": (request.url.scheme == "https"),
        "allow_insecure": bool(ctx.settings.auth_remote_allow_insecure),
        "enabled": bool(ctx.settings.auth_remote_enabled),
        "auth_source": str(getattr(ctx.settings, "auth_source", "") or ""),
    }
    payload.update(mgr.state_view())
    return JSONResponse(payload)


@router.post("/admin/auth/browser/start")
async def remote_browser_start(request: Request):
    session = require_admin(request)
    form = dict(await request.form())
    check_csrf(form, session)
    ctx = get_ctx(request)
    mgr = ctx.remote_browser
    if mgr is None:
        return redirect("/admin/auth/browser", msg="远程浏览器不可用（服务未装配）。", ok=False)
    try:
        result = mgr.start(
            admin_id=session.admin_id,
            token_hash=session.token_hash,
            secure=(request.url.scheme == "https"),
        )
    except Exception as exc:
        # 只报异常类型：启动路径会碰设置与环境，异常文本里可能带路径。
        return redirect("/admin/auth/browser",
                        msg=f"启动远程浏览器失败（{type(exc).__name__}）。", ok=False)
    return redirect("/admin/auth/browser", msg=str(result.get("message") or ""),
                    ok=bool(result.get("ok")))


@router.post("/admin/auth/browser/input")
async def remote_browser_input(request: Request):
    """转发一批键鼠事件（JSON）。

    授权链：``require_admin`` → ``X-CSRF-Token`` 头 → 同源 → 体积上限 → 属主。
    ``check_csrf`` 只读表单字段，而这里是 JSON 体，所以令牌走请求头、
    用同一个 ``security.csrf_matches`` 比对 —— 不是另造一套校验。
    """
    session = require_admin(request)
    ctx = get_ctx(request)
    mgr = ctx.remote_browser
    if not security.csrf_matches(session.csrf_token, request.headers.get("x-csrf-token", "")):
        raise HTTPException(status_code=400,
                            detail="CSRF 校验失败：请刷新页面后重试（令牌与当前会话不匹配）。")
    if not _same_origin(request):
        raise HTTPException(status_code=403,
                            detail="来源校验失败：请从本站页面提交（跨站提交或缺少来源信息都会被拒绝）。")
    # 体积：先看声明长度（挡掉大 body 被完整读进来），再看真实字节数
    # ——声明过的长度是可以撒谎的，`Content-Length` 从来不是可信输入。
    declared = (request.headers.get("content-length") or "").strip()
    if declared.isdigit() and int(declared) > MAX_INPUT_BYTES:
        raise HTTPException(status_code=413, detail="请求体过大。")
    raw = await request.body()
    if len(raw) > MAX_INPUT_BYTES:
        raise HTTPException(status_code=413, detail="请求体过大。")
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON。") from None
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        raise HTTPException(status_code=400, detail="events 必须是数组。")
    if len(events) > MAX_EVENTS:
        raise HTTPException(status_code=400, detail=f"单次事件数上限为 {MAX_EVENTS}。")
    clean = [e for e in events if isinstance(e, dict)]
    if mgr is None:
        raise HTTPException(status_code=404, detail="没有正在进行的远程浏览器会话")
    result = mgr.submit_input(clean, admin_id=session.admin_id, token_hash=session.token_hash)
    if not result.get("owner"):
        raise HTTPException(status_code=404, detail="没有正在进行的远程浏览器会话")
    return JSONResponse({"ok": True, "queued": int(result.get("queued") or 0),
                         "state": str(result.get("state") or "")})


@router.post("/admin/auth/browser/stop")
async def remote_browser_stop(request: Request):
    session = require_admin(request)
    form = dict(await request.form())
    check_csrf(form, session)
    ctx = get_ctx(request)
    mgr = ctx.remote_browser
    if mgr is None:
        return redirect("/admin/auth/browser", msg="远程浏览器不可用（服务未装配）。", ok=False)
    result = mgr.stop(admin_id=session.admin_id, token_hash=session.token_hash,
                      reason="管理员在面板上手动结束")
    return redirect("/admin/auth/browser", msg=str(result.get("message") or ""),
                    ok=bool(result.get("ok")))


# ================================================================== 通知设置


@router.get("/admin/notifications")
def notifications_page(request: Request):
    session = require_admin(request)
    ctx = get_ctx(request)
    setting = repo.get_notify_setting(ctx.db)
    flags = {**DEFAULT_EVENT_FLAGS, **(setting.get("event_flags") or {})}
    return render(request, "notifications.html", session, title="通知设置",
                  setting=setting, flags=flags,
                  event_labels=NOTIFY_EVENT_LABELS, events=list(NotifyEvent),
                  history=repo.list_notify_events(ctx.db, limit=30),
                  smtp_implemented=False)


@router.post("/admin/notifications")
async def notifications_save(request: Request):
    session = require_admin(request)
    form = dict(await request.form())
    check_csrf(form, session)
    ctx = get_ctx(request)
    flags = {e.value: (str(form.get(f"flag_{e.value}") or "") in ("1", "on", "true"))
             for e in NotifyEvent}
    repo.set_notify_setting(ctx.db, {
        "enabled": form.get("enabled"),
        "channel": "email",
        "smtp_host": form.get("smtp_host"),
        "smtp_port": form.get("smtp_port"),
        "smtp_user": form.get("smtp_user"),
        "smtp_password_env": form.get("smtp_password_env") or "HAUT_SMTP_PASSWORD",
        "mail_from": form.get("mail_from"),
        "mail_to": form.get("mail_to"),
        "use_tls": form.get("use_tls"),
        "event_flags": flags,
    })
    # 明确告知：设置已保存，但发送尚未实现，避免误以为会收到邮件
    return redirect("/admin/notifications",
                    msg="设置已保存。注意：邮件发送尚未实现，当前不会真正发出邮件。", ok=True)


# ================================================================== 系统状态


@router.get("/admin/system")
def system_page(request: Request):
    session = require_admin(request)
    ctx = get_ctx(request)
    return render(
        request, "system.html", session, title="系统状态",
        app_setting=repo.get_app_setting(ctx.db),
        scheduler_status=ctx.scheduler.status() if ctx.scheduler else None,
        stats=repo.dashboard_stats(ctx.db),
        admin_count=security.count_admins(ctx.db),
        db_path=str(ctx.settings.web_db),
    )


@router.post("/admin/system/toggle")
async def system_toggle(request: Request):
    session = require_admin(request)
    form = dict(await request.form())
    check_csrf(form, session)
    ctx = get_ctx(request)
    current = repo.get_app_setting(ctx.db)
    kwargs = {}
    if "global_pause" in form:
        kwargs["global_pause"] = str(form.get("global_pause") or "") in ("1", "on", "true")
    if "scheduler_enabled" in form:
        kwargs["scheduler_enabled"] = str(form.get("scheduler_enabled") or "") in ("1", "on", "true")
    if not kwargs:
        kwargs = {"global_pause": not current["global_pause"]}
    repo.set_app_setting(ctx.db, **kwargs)
    new = repo.get_app_setting(ctx.db)
    logger.info("全局开关变更：global_pause=%s scheduler_enabled=%s",
                new["global_pause"], new["scheduler_enabled"])
    return redirect("/admin/system",
                    msg=f"已保存：全局暂停={'开' if new['global_pause'] else '关'}，"
                        f"调度器={'开' if new['scheduler_enabled'] else '关'}")


@router.post("/admin/system/maintenance")
async def system_maintenance(request: Request):
    """清理：过期会话、旧登录记录、超出保留期的运行日志。"""
    session = require_admin(request)
    form = dict(await request.form())
    check_csrf(form, session)
    ctx = get_ctx(request)
    sessions = security.purge_expired_sessions(ctx.db)
    attempts = security.purge_old_login_attempts(ctx.db)
    events = repo.purge_run_events(
        ctx.db, keep_days=int(ctx.settings.scheduler_run_log_keep_days)
    )
    return redirect("/admin/system",
                    msg=f"已清理：会话 {sessions} 条、登录记录 {attempts} 条、运行日志 {events} 条。")


# ================================================================== 管理端：用户与邀请码


def _users_context(ctx) -> dict:
    """用户页需要的一切（**全都是元数据**，没有任何凭据值）。

    复用 ``user_auth.list_users`` 已经做好的子查询：计划数、学校账号标识、
    凭据状态与指纹。指纹是单向摘要前缀，可以安全展示 —— 它让管理员能确认
    "这条计划用的确实是那个账号"，但不能反推出令牌。
    """
    return {
        "users": user_auth.list_users(ctx.db),
        "invite_code": user_auth.effective_invite_code(ctx.db, ctx.settings),
        "signup_enabled": user_auth.signup_enabled(ctx.db, ctx.settings),
        "cred_labels": uc.CRED_STATUS_LABELS,
        "cred_tones": uc.CRED_STATUS_TONES,
    }


@router.get("/admin/users")
def users_page(request: Request):
    session = require_admin(request)
    ctx = get_ctx(request)
    return render(
        request, "admin_users.html", session, title="用户",
        **_users_context(ctx),
    )


def _user_row_or_404(ctx, user_id: int) -> dict:
    row = user_auth.get_user(ctx.db, int(user_id))
    if row is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return row


@router.post("/admin/users/{user_id}/status")
async def users_set_status(request: Request, user_id: int):
    session = require_admin(request)
    form = dict(await request.form())
    check_csrf(form, session)
    ctx = get_ctx(request)
    row = _user_row_or_404(ctx, user_id)
    enable = str(form.get("status") or "") == "active"
    user_auth.set_user_status(ctx.db, int(user_id), "active" if enable else "disabled")
    # 停用会**同时删掉该用户的所有会话**（在 set_user_status 里），因此这里能把
    # "停用后他还留在已登录页面里"这种最容易被忽略的口子一并堵上。
    logger.info("管理端%s用户 %s（#%s）", "启用" if enable else "停用", row["username"], user_id)
    return redirect(
        "/admin/users",
        msg=f"已{'启用' if enable else '停用'}用户 {row['username']}"
            + ("" if enable else "（其全部会话已作废、计划不会被执行）"),
        ok=True,
    )


@router.post("/admin/users/{user_id}/password")
async def users_reset_password(request: Request, user_id: int):
    session = require_admin(request)
    form = dict(await request.form())
    check_csrf(form, session)
    ctx = get_ctx(request)
    row = _user_row_or_404(ctx, user_id)
    password = str(form.get("password") or "")
    try:
        user_auth.set_user_password(ctx.db, int(user_id), password)
    except ValueError as exc:
        return redirect("/admin/users", msg=f"口令不符合要求：{exc}", ok=False)
    # 口令**从不回显、从不记录**：日志里只有用户名。
    logger.info("管理端重置了用户 %s（#%s）的口令", row["username"], user_id)
    return redirect(
        "/admin/users",
        msg=f"已重置 {row['username']} 的口令（其原有会话全部作废，请把新口令交给本人）。",
        ok=True,
    )


@router.post("/admin/users/{user_id}/delete")
async def users_delete(request: Request, user_id: int):
    """删除用户账号：**保留**他的计划与运行记录，只解除归属。

    这是刻意的选择 —— 运行记录是"今天是否已经提交过"的证据，
    删用户顺手删掉它们会打开重复预约的口子（与用户端删除计划的既有保护同源）。
    """
    session = require_admin(request)
    form = dict(await request.form())
    check_csrf(form, session)
    ctx = get_ctx(request)
    row = _user_row_or_404(ctx, user_id)
    # 计划数用**归属查询**现算，不读 ``row["plan_count"]``：那个字段只存在于
    # ``user_auth.list_users`` 的行里（它是给列表页用的子查询），而这里的 ``row``
    # 来自 ``get_user``，没有这一列 —— 读它必然 KeyError ⇒ 500。
    # 用 ``list_plans_for_user`` 的条数还顺带保证了"计划数"与"归属"永远同源。
    plans = len(repo.list_plans_for_user(ctx.db, int(user_id)))
    user_auth.delete_user(ctx.db, int(user_id))
    logger.info("管理端删除了用户 %s（#%s），保留计划 %d 个", row["username"], user_id, plans)
    return redirect(
        "/admin/users",
        msg=f"已删除用户 {row['username']}（其 {plans} 个计划与全部运行记录保留，"
            "但不再有归属用户）",
        ok=True,
    )


@router.post("/admin/users/invite")
async def users_rotate_invite(request: Request):
    session = require_admin(request)
    form = dict(await request.form())
    check_csrf(form, session)
    ctx = get_ctx(request)
    code = user_auth.rotate_invite_code(ctx.db)
    # 邀请码不是凭据（它只决定"能不能注册"，且可以随时换），因此可以在页面上显示；
    # 但一样不写进日志。
    logger.info("管理端轮换了注册邀请码")
    return redirect(
        "/admin/users",
        msg=f"已生成新的邀请码：{code}（旧邀请码立即失效，请把新码发给要注册的人）",
        ok=True,
    )


@router.post("/admin/users/signup")
async def users_toggle_signup(request: Request):
    session = require_admin(request)
    form = dict(await request.form())
    check_csrf(form, session)
    ctx = get_ctx(request)
    enabled = str(form.get("signup_enabled") or "") in ("1", "on", "true")
    user_auth.set_signup_enabled(ctx.db, enabled)
    logger.info("管理端%s了自助注册", "开启" if enabled else "关闭")
    return redirect(
        "/admin/users",
        msg=f"自助注册已{'开启' if enabled else '关闭'}"
            + ("" if enabled else "（已登录用户不受影响）"),
        ok=True,
    )


# ================================================================== 用户端（/booking）
#
# 每个用户**自己的账号 + 自己的学校凭据**的极简用户端：
# 自助注册（邀请码）/ 登录 → 绑定自己的图书馆账号 → 建自己的预约计划 → 看自己的执行状态。
#
# 它仍然不是第二套数据模型，也不是第二套执行链路：
# - 数据写进**同一张 plan 表**（因此 /admin 立刻能看到），来源多一行 plan_origin，
#   归属多一行 user_plan（侧表，见 db.py 里为什么不给 plan 加列）；
# - 校验走**同一个** ``parse_plan`` / ``expand_plan_form``（服务端唯一权威入口）；
# - 状态变更走**同一批** ``repository`` 动作（pause/resume/delete）与状态机；
# - 执行仍然由**同一个**调度器负责，用户端连"立即执行"都不提供。
#
# ## 身份与隔离（本轮的核心变化）
#
# 从前这里写着"没有用户体系、共享使用模式"，现在不再是了：用户端有平台账号
# （``app_user`` + ``user_session``，与管理员那套**完全分开**的表/Cookie/令牌）。
# 具体边界：
#
# 1. **所有 /booking 路由都要求已登录**（``_require_user``），未登录 303 去登录页；
# 2. **越权一律 404**：别人的计划不是"403 不能操作"，而是"看起来根本不存在"
#    （``_owned_plan_or_404``）—— 403 会泄漏"这个 id 确实存在"；
# 3. 无主的计划（历史计划、管理端建的计划）**不属于任何用户**，因此用户在
#    /booking 里既看不到也改不了它们；
# 4. 用户端**碰不到**的东西仍然是硬边界：凭据明文、全局暂停/调度总开关、
#    暂停旗标文件、真实预约总开关、已提交/结果未知记录的删除、"立即执行"。
#
# ## 凭据
#
# 学校口令只在"绑定图书馆账号"那一次请求的内存里存在，绝不落盘、绝不入日志；
# 拿到的是**用户自己**的 Cookie/Authorization，加密后按 user_id 分开存。
# 页面上任何地方都不渲染凭据，连密文都拿不到（``repo.get_user_credential_view``
# 里根本没有密文字段）。


def _booking_ctx(request: Request):
    """用户端统一入口：开关关闭则直接 404（不暴露"这里本来有个页面"）。"""
    ctx = get_ctx(request)
    if not bool(getattr(ctx.settings, "web_enable_user_end", True)):
        raise HTTPException(status_code=404, detail="用户端未启用")
    return ctx


def _current_user(request: Request, ctx):
    """当前登录的用户端会话；未登录/已过期/账号被停用一律返回 ``None``。"""
    token = request.cookies.get(user_auth.USER_SESSION_COOKIE, "")
    if not token:
        return None
    # TTL 在**签发时**已写进 user_session.expires_at，这里只补查**空闲超时**；
    # 两个时限都按各自的默认常量兜底，不依赖配置项一定存在。
    return user_auth.load_user_session(
        ctx.db, token,
        idle_minutes=int(
            getattr(ctx.settings, "user_session_idle_minutes", security.SESSION_IDLE_MINUTES)
        ),
    )


def _require_user(request: Request, ctx):
    """要求已登录：GET 跳登录页，其它方法 401（与管理员侧同一套语义）。"""
    session = _current_user(request, ctx)
    if session is None:
        if request.method in ("GET", "HEAD"):
            raise HTTPException(
                status_code=303, headers={"Location": "/booking/login"}, detail="需要登录"
            )
        raise HTTPException(status_code=401, detail="需要登录")
    return session


def _set_user_cookie(response, request: Request, token: str, settings):
    from haut_seat.web.app import user_session_cookie_kwargs

    kwargs = user_session_cookie_kwargs(settings)
    if request.cookies.get(kwargs["key"]) != token:
        response.set_cookie(value=token, **kwargs)
    return response


def _clear_user_cookie(response, ctx):
    from haut_seat.web.app import user_session_cookie_kwargs

    response.delete_cookie(
        user_session_cookie_kwargs(ctx.settings)["key"], path="/booking"
    )
    return response


def _booking_render(request: Request, name: str, *, status_code: int = 200, user=None, **extra):
    """渲染用户端页面，并把 CSRF 令牌种进 Cookie（模板同时回填进表单）。

    ``csrf_token`` 显式覆盖 ``base_context`` 里那个「管理员会话令牌」：用户端的表单
    用的是双提交 Cookie 令牌，与管理端会话令牌彼此独立、互不通用。

    用户端会话**不**作为 ``render`` 的 ``session`` 参数传进去 —— 那个参数会填充
    ``admin`` 与管理员 CSRF 令牌，两者都属于另一套身份；当前用户通过 ``user``
    显式注入。
    """
    ctx = _booking_ctx(request)
    token = booking_csrf_token(request)
    extra.setdefault("user", user)
    response = render(request, name, None, status_code=status_code,
                      csrf_token=token, **extra)
    return set_booking_csrf_cookie(response, request, token, ctx.settings)


def _owned_plan_or_404(ctx, user, plan_id: int) -> dict:
    """取**该用户自己的**计划；不存在、或属于别人，一律 404。

    这里刻意不区分"不存在"与"不是你的"：返回 403 等于告诉探测者
    "这个编号确实有一个计划"，那是一处不必要的存在性泄漏。
    """
    plan = repo.get_plan(ctx.db, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="计划不存在")
    if repo.get_plan_owner(ctx.db, plan_id) != int(user.user_id):
        raise HTTPException(status_code=404, detail="计划不存在")
    return plan


def _credential_context(ctx, user) -> dict:
    """用户端页面共用的凭据状态（**只有元数据**：学校账号标识 + 状态 + 指纹）。"""
    from haut_seat.services import user_credentials as uc

    return uc.status_view(ctx.db, int(user.user_id))


def _user_can_modify(plan: dict, *, has_active_run: bool) -> tuple[bool, str]:
    """用户端「能不能改 / 能不能删」的统一判据（两处必须同源）。

    允许的边界是需求说的「尚未开始、尚未提交」；两条硬拦截：

    1. **正在运行**（或有 ``running`` 运行记录）——改参数会让正在飞的这次提交
       与记录对不上；
    2. **结果未知**——这是重复预约保护的核心证据。运行记录虽然删不掉，
       但允许改参数会让使用者以为"这条已经处理掉了"，而实际服务器端可能已经
       留下了预约。必须先核实、先恢复，再谈修改。

    另外 ``ran_irreversibly_today``（今天成功过或提交过）时禁止**删除**：
    删计划会级联删掉运行记录，而那条记录正是"今天不得再提交"的唯一依据 ——
    允许删除等于开出一条绕过重复预约保护的路。
    """
    if has_active_run or plan["status"] is PlanStatus.RUNNING:
        return False, "计划正在执行中，不能修改或删除。请先暂停，等本轮结束后再操作。"
    if plan["status"] is PlanStatus.UNKNOWN:
        return False, (
            "上一次提交的结果未知：请先登录图书馆网站核实该时段是否已有预约记录，"
            "再在详情页勾选「已核实」恢复；在此之前不能修改或删除这条计划。"
        )
    return True, ""


def _user_delete_blocked(ctx, plan: dict) -> str:
    """删除特有的额外拦截：今天已经成功过或提交过 ⇒ 拒绝。"""
    last = repo.last_run_for_plan(ctx.db, plan["id"])
    if ran_irreversibly_today(last, _now().date()):
        if last.get("status") is RunStatus.SUCCEEDED:
            why = "今天的预约已经成功"
        else:
            why = (f"今天已经发起过提交（运行 #{last.get('id')}，"
                   f"提交 {int(last.get('submit_count') or 0)} 次）")
        return (
            f"该计划{why}。删除会一并删掉这条运行记录，而它正是「今天不再提交」的依据，"
            "因此不能在这里删除。"
        )
    return ""


@router.get("/booking")
def booking_list(request: Request):
    ctx = _booking_ctx(request)
    user = _require_user(request, ctx)
    # **只列自己的计划**：归属在 SQL 里过滤（repo.list_plans_for_user），
    # 不是取全部再在模板里 if —— 后者意味着别人的计划数据已经进了这个进程的
    # 模板上下文，一次写错的模板就能把它渲染出去。
    plans = repo.list_plans_for_user(ctx.db, int(user.user_id))
    now = _now()
    origins = repo.plan_origins(ctx.db)
    last_runs = {p["id"]: repo.last_run_for_plan(ctx.db, p["id"]) for p in plans}
    return _booking_render(
        request, "booking.html",
        title="预约用户端",
        user=user,
        plan_count=len(plans),
        cred=_credential_context(ctx, user),
        plans=plans,
        when_labels={p["id"]: plan_when_label(p, now=now) for p in plans},
        window_states={p["id"]: plan_window_state(p, now=now) for p in plans},
        seat_windows={p["id"]: (clock_of(p["book_start"]), clock_of(p["book_end"]))
                      for p in plans},
        last_runs=last_runs,
        origins=origins,
        origin_labels=repo.PLAN_ORIGIN_LABELS,
        # 用户端只能改「不在运行、且不是结果未知」的计划 —— 与动作处理里同一判据。
        editable={p["id"]: _user_can_modify(
            p, has_active_run=repo.plan_has_active_run(ctx.db, p["id"])
        )[0] for p in plans},
        run_duration_text=repo.run_duration_text,
    )


# ---------------------------------------------------------------- 注册 / 登录 / 退出


@router.get("/booking/login")
def booking_login_form(request: Request):
    ctx = _booking_ctx(request)
    if _current_user(request, ctx) is not None:
        return redirect("/booking")
    return _booking_render(
        request, "booking_login.html", title="登录",
        signup_open=user_auth.signup_enabled(ctx.db, ctx.settings),
        values={}, error="",
    )


@router.post("/booking/login")
async def booking_login_submit(request: Request):
    ctx = _booking_ctx(request)
    form = dict(await request.form())
    check_booking_submit(request, form)
    username = str(form.get("username") or "").strip()
    password = str(form.get("password") or "")
    ip = client_ip(request, trust_proxy=bool(ctx.settings.web_trust_proxy))

    def _fail(message: str):
        return _booking_render(
            request, "booking_login.html", title="登录", status_code=200,
            signup_open=user_auth.signup_enabled(ctx.db, ctx.settings),
            values={"username": username}, error=message,
        )

    locked = user_auth.user_login_locked_until(ctx.db, username, ip)
    if locked is not None:
        return _fail(f"登录尝试过于频繁，请在 {locked:%H:%M:%S} 后重试。")

    row = user_auth.find_user_with_hash(ctx.db, username)
    ok = bool(row) and row["status"] == "active" and security.verify_password(
        password, row["password_hash"]
    )
    user_auth.record_user_login_attempt(ctx.db, username, ip, ok)
    if not ok:
        # 统一提示：不透露"用户名是否存在"或"账号是否被停用"。
        return _fail("用户名或口令不正确。")

    token, _csrf = user_auth.create_user_session(
        ctx.db, int(row["id"]),
        ttl_hours=int(getattr(ctx.settings, "user_session_ttl_hours", 72)),
    )
    user_auth.touch_user_login(ctx.db, int(row["id"]))
    logger.info("用户 %s 登录成功（来源 %s）", row["username"], ip or "unknown")
    response = redirect("/booking", msg="登录成功。")
    return _set_user_cookie(response, request, token, ctx.settings)


@router.get("/booking/register")
def booking_register_form(request: Request):
    ctx = _booking_ctx(request)
    open_ = user_auth.signup_enabled(ctx.db, ctx.settings)
    return _booking_render(
        request, "booking_register.html", title="注册",
        signup_open=open_,
        # 页面上**不显示**邀请码本身，只说明"需要管理员给的邀请码"：
        # 把码印在注册页上等于没有邀请码。
        values={}, error="",
        status_code=200 if open_ else 403,
    )


@router.post("/booking/register")
async def booking_register_submit(request: Request):
    ctx = _booking_ctx(request)
    form = dict(await request.form())
    check_booking_submit(request, form)
    username = str(form.get("username") or "").strip()
    password = str(form.get("password") or "")
    password2 = str(form.get("password2") or "")
    invite = str(form.get("invite_code") or "")

    def _fail(message: str, *, status_code: int = 200):
        return _booking_render(
            request, "booking_register.html", title="注册", status_code=status_code,
            signup_open=user_auth.signup_enabled(ctx.db, ctx.settings),
            values={"username": username, "display_name": str(form.get("display_name") or "")},
            error=message,
        )

    if not user_auth.signup_enabled(ctx.db, ctx.settings):
        return _fail("管理员尚未开放注册（或未生成邀请码），请联系管理员。", status_code=403)
    if password != password2:
        return _fail("两次输入的口令不一致。")
    if not user_auth.invite_code_matches(
        user_auth.effective_invite_code(ctx.db, ctx.settings), invite
    ):
        return _fail("邀请码不正确。")
    try:
        user_id = user_auth.create_user(
            ctx.db, username, password,
            display_name=str(form.get("display_name") or ""),
        )
    except user_auth.UserExistsError:
        return _fail("该用户名已被占用，请换一个。")
    except user_auth.UsernameError as exc:
        return _fail(str(exc))
    except ValueError as exc:  # 口令强度
        return _fail(str(exc))

    # 注册成功**不自动登录**：让用户亲手走一次登录，口令输错时也不会以为注册失败了。
    logger.info("用户端新注册账号 #%s（%s）", user_id, username)
    return redirect("/booking/login", msg="注册成功，请登录。")


@router.post("/booking/logout")
async def booking_logout(request: Request):
    ctx = _booking_ctx(request)
    form = dict(await request.form())
    check_booking_submit(request, form)
    user_auth.destroy_user_session(ctx.db, request.cookies.get(user_auth.USER_SESSION_COOKIE, ""))
    response = redirect("/booking/login", msg="已退出登录。")
    return _clear_user_cookie(response, ctx)


# ---------------------------------------------------------------- 绑定图书馆账号


@router.get("/booking/bind")
def booking_bind_form(request: Request, error: str = ""):
    ctx = _booking_ctx(request)
    user = _require_user(request, ctx)
    # 每次打开这个页面都取一张**新的**验证码：上一次那张无论用没用过，
    # 都不该被反复提交（学校那侧也是一次性的）。
    return _booking_bind_render(request, ctx, user, error=error)


def _booking_bind_render(
    request, ctx, user, *, error: str = "", captcha_image: str = "", values: dict | None = None
):
    """渲染绑定页；**总是**把会话里那张验证码换成新的一张（除非调用方指定了图片）。

    拿不到验证码时**如实说明**并渲染一个没有图片的表单 —— 绝不画一张占位图片
    假装有验证码。
    """
    from haut_seat.services import user_credentials as uc

    captcha_error = ""
    if not captcha_image:
        try:
            from haut_seat.services.school_login import fetch_captcha

            challenge = fetch_captcha(ctx.settings)
            captcha_image = challenge.image_data_uri
            user_auth.set_pending_captcha(ctx.db, user.token_hash, challenge.uuid)
        except SchoolLoginError as exc:
            # 取不到就如实说，并把会话里那张作废（否则用户对着一个不存在的 uuid 提交）。
            captcha_error = str(exc)
            user_auth.clear_pending_captcha(ctx.db, user.token_hash)
    return _booking_render(
        request, "booking_bind.html", title="绑定图书馆账号",
        user=user, cred=uc.status_view(ctx.db, int(user.user_id)),
        captcha_image=captcha_image, captcha_error=captcha_error,
        error=error, values=values or {},
    )


@router.post("/booking/bind")
async def booking_bind_submit(request: Request):
    ctx = _booking_ctx(request)
    form = dict(await request.form())
    check_booking_submit(request, form)
    user = _require_user(request, ctx)
    from haut_seat.services import school_login
    from haut_seat.services import user_credentials as uc

    school_username = str(form.get("school_username") or "").strip()
    password = str(form.get("password") or "")
    answer = str(form.get("code") or "").strip()
    # 图片只是给下一次渲染用的（本地校验失败时要让用户看到同一张图），
    # 服务端不依赖它：真正有效的是会话里记录的 uuid。客户端改这张图无害。
    keep_image = str(form.get("captcha_image") or "")
    values = {"school_username": school_username}

    if not school_username or not password or not answer:
        return _booking_bind_render(
            request, ctx, user,
            error="请填写学校账号、口令与验证码。",
            captcha_image=keep_image, values=values,
        )

    locked = uc.bind_locked_until(ctx.db, int(user.user_id))
    if locked is not None:
        return _booking_bind_render(
            request, ctx, user,
            error=f"绑定尝试过于频繁（{locked:%H:%M:%S} 后重试）。"
                  "每次失败都必须由你本人重新看一张新验证码，程序不会自动重试。",
            captcha_image=keep_image, values=values,
        )

    uuid = user.pending_captcha_uuid
    if not user_auth.pending_captcha_fresh(
        user, ttl_s=float(getattr(ctx.settings, "user_captcha_ttl_s", 300.0))
    ):
        return _booking_bind_render(
            request, ctx, user,
            error="页面上的验证码已过期，已为你取一张新的，请重新输入。",
            values=values,
        )

    uc.record_bind_attempt(ctx.db, int(user.user_id))
    try:
        outcome = school_login.submit_login(
            ctx.settings, username=school_username, password=password,
            code=answer, uuid=uuid,
        )
    except SchoolLoginError as exc:
        # 出站异常（网络/协议）：**不改动**已保存的凭据，只如实报错。
        user_auth.clear_pending_captcha(ctx.db, user.token_hash)
        return _booking_bind_render(
            request, ctx, user, error=str(exc), values=values
        )
    finally:
        password = ""  # 明文口令到此为止：不留下任何一个还能读到的引用

    # 这张验证码已经被消费（无论成功失败），一律作废。
    user_auth.clear_pending_captcha(ctx.db, user.token_hash)

    if not outcome.ok:
        # 失败：取一张**新**验证码让用户重新读 —— 完全不自动重试。
        # 页面上显示的是学校自己给的原因（已脱敏、截断）。
        logger.info("用户 #%s 绑定失败：%s", user.user_id, outcome.message)
        return _booking_bind_render(
            request, ctx, user, error=f"绑定失败：{outcome.message}", values=values
        )

    # 登录成功 ≠ 可用：再用**只读**预检确认这份凭据真的能取到数据（第二项的第 6 步）。
    # 结论如实分成三态，不做任何粉饰。
    from haut_seat.services.auth_store import Credentials

    creds = Credentials(cookie=outcome.cookie, authorization=outcome.authorization,
                        source="user")
    check = uc.verify_credentials(ctx.settings, creds)
    status = uc.CRED_STATUS_VALID if check.is_valid else (
        uc.CRED_STATUS_INVALID if check.is_invalid else uc.CRED_STATUS_INDETERMINATE
    )
    uc.save_credentials(
        ctx.db, ctx.settings, int(user.user_id),
        school_username=school_username, cookie=outcome.cookie,
        authorization=outcome.authorization, status=status,
        message="绑定成功，只读探测确认可用" if status == uc.CRED_STATUS_VALID
                else f"绑定已保存，但只读探测结论为：{check.message}",
        source="bind",
    )
    if status == uc.CRED_STATUS_VALID:
        resumed = uc.resume_owned_plans(ctx.db, int(user.user_id))
        note = f"其中 {resumed} 个「等待认证」的计划已放回待执行。" if resumed else ""
        logger.info("用户 #%s 绑定成功（指纹 %s），恢复计划 %d 个",
                    user.user_id, outcome.token_fingerprint, resumed)
        return redirect("/booking", msg=f"图书馆账号绑定成功，凭据已验证可用。{note}")
    logger.warning("用户 #%s 绑定后探测结论为 %s", user.user_id, status)
    return redirect(
        "/booking",
        msg=f"凭据已保存，但只读探测未能确认可用（{uc.cred_label(status)}）。"
            "如果马上要用，请点「重新验证」或重新绑定。",
        ok=False,
    )


@router.post("/booking/unbind")
async def booking_unbind(request: Request):
    ctx = _booking_ctx(request)
    form = dict(await request.form())
    check_booking_submit(request, form)
    user = _require_user(request, ctx)
    from haut_seat.services import user_credentials as uc

    removed = uc.clear_credentials(ctx.db, ctx.settings, int(user.user_id))
    if removed:
        logger.info("用户 #%s 解绑了学校凭据", user.user_id)
        return redirect("/booking", msg="已解绑图书馆账号（密文已删除）。", ok=True)
    return redirect("/booking", msg="当前没有已绑定的图书馆账号。", ok=False)


@router.post("/booking/verify")
async def booking_verify(request: Request):
    """重新验证已保存的凭据是否仍然可用（只读探测，绝不提交预约）。"""
    ctx = _booking_ctx(request)
    form = dict(await request.form())
    check_booking_submit(request, form)
    user = _require_user(request, ctx)
    from haut_seat.services import user_credentials as uc

    try:
        creds = uc.load_credentials(ctx.db, ctx.settings, int(user.user_id))
    except uc.NoCredentialError:
        return redirect("/booking/bind", msg="尚未绑定图书馆账号，请先绑定。", ok=False)
    except uc.CredentialDecryptError as exc:
        return redirect("/booking/bind", msg=str(exc), ok=False)

    check = uc.verify_credentials(ctx.settings, creds)
    status = uc.CRED_STATUS_VALID if check.is_valid else (
        uc.CRED_STATUS_INVALID if check.is_invalid else uc.CRED_STATUS_INDETERMINATE
    )
    uc.mark_status(ctx.db, int(user.user_id), status, check.message, source="manual")
    resumed = 0
    if status == uc.CRED_STATUS_VALID:
        resumed = uc.resume_owned_plans(ctx.db, int(user.user_id))
    logger.info("用户 #%s 重新验证凭据：%s", user.user_id, status)
    if status == uc.CRED_STATUS_VALID:
        note = f"，{resumed} 个「等待认证」的计划已放回待执行" if resumed else ""
        return redirect("/booking", msg=f"凭据有效（只读探测通过）{note}。")
    if status == uc.CRED_STATUS_INVALID:
        return redirect(
            "/booking", ok=False,
            msg="探测结果显示这份凭据已被服务器拒绝（登录已失效），请重新绑定图书馆账号。",
        )
    return redirect(
        "/booking", ok=False,
        msg=f"无法确认凭据是否有效：{check.message}。这**不代表**凭据已失效，"
            "可能是网络或服务器问题，稍后再试即可。",
    )


def _booking_form_ctx(ctx, user, values: dict, *, existing_seats=None) -> dict:
    """用户端计划表单的额外上下文：目录区域名 + **该用户自己**的待选草稿。

    与管理端的 ``_plan_form_ctx`` 是同一套合并逻辑（``_apply_picks``），
    区别只有一个：草稿按 ``user_id`` 取，别人的选择永远不会出现在你的表单里。
    """
    values, pick_info = _apply_picks(
        ctx.db, values, existing_seats=existing_seats, user_id=int(user.user_id)
    )
    return {
        "values": values,
        "pick_info": pick_info,
        "region_labels": _region_labels(ctx.db),
        "plan_modes": PLAN_MODES,
        "mode_labels": PLAN_MODE_LABELS,
    }


@router.get("/booking/new")
def booking_new_form(request: Request):
    ctx = _booking_ctx(request)
    user = _require_user(request, ctx)
    defaults = _form_values()
    defaults["regionid"] = ctx.settings.regionid
    extra = _booking_form_ctx(ctx, user, defaults)
    return _booking_render(
        request, "booking_form.html",
        title="新建预约", plan=None, errors=[], warnings=[],
        action="/booking/new",
        user=user, cred=_credential_context(ctx, user), **extra,
    )


def _booking_form_error(request, ctx, *, plan_id, title, action, form, errors, user=None):
    """校验失败时原样回显表单（200），与成功路径分开以免误报「已保存」。

    与管理端同一条约定：回显时**不再**并入目录草稿 —— 这时用户填的内容才是唯一
    依据，草稿若再叠上去会让"我提交的值"和"页面显示的值"不一致。
    """
    return _booking_render(
        request, "booking_form.html", title=title,
        plan=repo.get_plan(ctx.db, plan_id) if plan_id else None,
        errors=errors, warnings=[], values=_form_values(form), action=action,
        plan_modes=PLAN_MODES, mode_labels=PLAN_MODE_LABELS,
        pick_info={"count": 0, "regionids": [], "missing": [], "bound_regionid": None},
        region_labels=_region_labels(ctx.db),
        user=user, cred=_credential_context(ctx, user) if user else None,
    )


@router.post("/booking/new")
async def booking_new_submit(request: Request):
    ctx = _booking_ctx(request)
    form = dict(await request.form())
    check_booking_submit(request, form)
    user = _require_user(request, ctx)
    try:
        data, warnings = _validated_plan_form(form)
    except ValidationError as exc:
        return _booking_form_error(request, ctx, plan_id=None, title="新建预约",
                                   action="/booking/new", form=form,
                                   errors=exc.errors, user=user)
    # 与管理端同一条规则：新建的计划一律「不武装」，必须显式启用才会被调度器拾取。
    plan_id = repo.create_plan(ctx.db, data, enabled=False)
    repo.set_plan_origin(ctx.db, plan_id, repo.PLAN_ORIGIN_USER)
    # 归属与来源是两件事：来源（plan_origin）说"谁建的"，归属（user_plan）说
    # "用谁的学校凭据执行"。用户端建的计划两者都指向本人。
    repo.set_plan_owner(ctx.db, plan_id, int(user.user_id))
    # 草稿已经变成计划内容，清掉以免串到下一个计划。**只清自己的那一份**
    # （管理端清的是它自己那份全局草稿；两者互不影响）。
    cleared = repo.clear_user_picks(ctx.db, int(user.user_id))
    logger.info("用户端新建计划 #%s（用户 #%s）", plan_id, user.user_id)
    msg = "预约已保存，当前为「已暂停」。确认参数无误后点「启用」才会开始执行。"
    if cleared:
        msg += f" 座位目录里那 {cleared} 个待选座位已清空（它们已经写进这条预约了）。"
    if warnings:
        msg += " 提示：" + "；".join(warnings)
    return redirect(f"/booking/{plan_id}", msg=msg)


def _booking_values(plan: dict) -> dict:
    """编辑表单的初始值：与用户端新建表单同一批字段（不含管理端专属的技术字段）。"""
    return {
        "name": plan["name"],
        "run_date": plan["run_date"],
        "repeat_rule": plan["repeat_rule"],
        "trigger_at": plan["trigger_at"],
        "book_start": plan["book_start"],
        "book_end": plan["book_end"],
        "regionid": plan["regionid"],
        "preferred_seats": "\n".join(plan["preferred_seats"]),
        "preferred_prefixes": "\n".join(plan["preferred_prefixes"]),
        "confirmed_bookable_states": _json_combos(plan["confirmed_bookable_states"]),
        "max_attempts_per_seat": plan["max_attempts_per_seat"],
        "note": plan["note"],
        "mode": repeat_rule_to_plan_mode(plan["repeat_rule"]),
        "weekdays_only": "1" if plan["repeat_rule"] == "weekdays" else "",
        "trigger_clock": clock_of(plan["trigger_at"]),
        "use_start": clock_of(plan["book_start"]),
        "use_end": clock_of(plan["book_end"]),
        "regionid_manual": "",
    }


# ================================================================== 用户端座位目录
#
# 与管理端同一套「目录只是填表助手」的语义，但**能力严格更小**：
#
# - 只读浏览 + 挑选。刷新目录、添加区域、离线导入、区域改名、收藏全部是管理端能力，
#   用户端一条都没有 —— 用户不该能触发对外请求，也不该能给目录里塞进区域；
# - 草稿按人隔离（``user_seat_pick``），别人的选择既看不到也动不了；
# - 页面上不渲染目录的运维状态（最近刷新时间/失败原因），那是管理端的运维信息。
#
# 这些路由必须声明在 ``/booking/{plan_id}`` **之前**：否则 ``/booking/catalog``
# 会先被那条通配路由吃掉（FastAPI 按声明顺序匹配）。


def _user_catalog_return_url(ctx, user, form, regionid) -> str:
    """用户端目录操作后回到「计划表单」还是「目录页」。

    ``return=plan_edit`` 只有当那个计划**确实是本人的**才回到它，否则一律回目录页：
    不报 404、也不透露"这个编号是否存在于别人名下"（与 ``_owned_plan_or_404``
    同一条「不泄漏存在性」的约定）。
    """
    ret = str(form.get("return") or "")
    plan_id = _int_param(form.get("plan_id"))
    if ret == "plan_new":
        return "/booking/new"
    if ret == "plan_edit" and plan_id is not None:
        if repo.get_plan_owner(ctx.db, plan_id) == int(user.user_id):
            return f"/booking/{plan_id}/edit"
    return f"/booking/catalog?region={regionid}" if regionid else "/booking/catalog"


@router.get("/booking/catalog")
def booking_catalog_page(request: Request):
    ctx = _booking_ctx(request)
    user = _require_user(request, ctx)
    view = _catalog_view(ctx, request, user_id=int(user.user_id))
    # ``return=plan_edit&plan_id=N`` 里的 N 来自查询串，是客户端可以随便写的输入：
    # 只有确实是本人的计划才认它（页面上的按钮与隐藏字段都据此决定）。
    # 不是本人的就当作没带 —— 不指过去、也不说"这个编号存在但不是你的"。
    plan_id = _int_param(request.query_params.get("plan_id"))
    if plan_id is not None and repo.get_plan_owner(ctx.db, plan_id) != int(user.user_id):
        plan_id = None
    return_to = str(request.query_params.get("return") or "")
    if return_to == "plan_edit" and plan_id is None:
        # 编号丢了（或被抹掉）就不能说"回到那个计划"：隐藏字段会被带进
        # 每个 POST，最后把用户送去一个不存在的地址。当作没带 return。
        return_to = ""
    return _booking_render(
        request, "booking_catalog.html",
        title="座位目录（挑座位）",
        view=view,
        return_to=return_to,
        plan_id=plan_id,
        seat_key=_seat_key,
        user=user, cred=_credential_context(ctx, user),
    )


def _user_catalog_seats(ctx, form) -> tuple[list, set]:
    """当前视图范围内的座位与其 key 集合（勾选语义与解勾语义都只作用于它）。"""
    seats = repo.list_catalog_seats(
        ctx.db,
        regionid=_int_param(form.get("region")),
        floor=str(form.get("floor") or "").strip(),
        q=str(form.get("q") or "").strip(),
        limit=CATALOG_SEAT_LIMIT,
    )
    return seats, {(s["regionid"], s["seat_name"]) for s in seats}


@router.post("/booking/catalog/picks")
async def booking_catalog_apply_picks(request: Request):
    """把当前视图里勾选的座位写进**你自己的**待选草稿。

    语义与管理端一致：视图内整体生效（勾上=加入，取消=移出），视图外的草稿原样
    保留 —— 于是可以换区域、换楼层慢慢攒选择。
    """
    ctx = _booking_ctx(request)
    form = await request.form()
    check_booking_submit(request, form)
    user = _require_user(request, ctx)
    uid = int(user.user_id)
    regionid = _int_param(form.get("region"))
    seats, scoped = _user_catalog_seats(ctx, form)
    checked = set(form.getlist("seat"))
    kept = [
        (p["regionid"], p["seat_name"]) for p in repo.list_user_picks(ctx.db, uid)
        if (p["regionid"], p["seat_name"]) not in scoped
    ]
    # 视图内的按**渲染顺序**加入，与页面上看到的顺序一致（即优先级顺序）
    chosen = [
        (s["regionid"], s["seat_name"]) for s in seats
        if _seat_key(s["regionid"], s["seat_name"]) in checked
    ]
    count = repo.replace_user_picks(ctx.db, uid, kept + chosen)
    return redirect(
        _user_catalog_return_url(ctx, user, form, regionid),
        msg=f"已应用选择：当前共 {count} 个座位。可以继续换区域/楼层挑选，或回到计划页确认。",
    )


@router.post("/booking/catalog/picks/remove")
async def booking_catalog_remove_pick(request: Request):
    ctx = _booking_ctx(request)
    form = await request.form()
    check_booking_submit(request, form)
    user = _require_user(request, ctx)
    uid = int(user.user_id)
    target = (str(form.get("regionid") or ""), str(form.get("seat_name") or ""))
    kept = [
        (p["regionid"], p["seat_name"]) for p in repo.list_user_picks(ctx.db, uid)
        if (str(p["regionid"]), p["seat_name"]) != target
    ]
    repo.replace_user_picks(ctx.db, uid, kept)
    return redirect(_user_catalog_return_url(ctx, user, form, _int_param(form.get("region"))),
                    msg=f"已移出选择：{target[1]}")


@router.post("/booking/catalog/picks/move")
async def booking_catalog_move_pick(request: Request):
    """候选座位上移/下移一位 —— 顺序即计划里的优先级顺序。"""
    ctx = _booking_ctx(request)
    form = await request.form()
    check_booking_submit(request, form)
    user = _require_user(request, ctx)
    regionid = _int_param(form.get("regionid"))
    seat_name = str(form.get("seat_name") or "").strip()
    if regionid is None or not seat_name:
        return redirect("/booking/catalog", msg="移动候选座位需要区域与座位名。", ok=False)
    result = repo.move_user_pick(
        ctx.db, int(user.user_id), regionid, seat_name, str(form.get("direction") or "")
    )
    return redirect(
        _user_catalog_return_url(ctx, user, form, _int_param(form.get("region"))),
        msg=result["message"], ok=bool(result["ok"]),
    )


@router.post("/booking/catalog/picks/clear")
async def booking_catalog_clear_picks(request: Request):
    ctx = _booking_ctx(request)
    form = await request.form()
    check_booking_submit(request, form)
    user = _require_user(request, ctx)
    count = repo.clear_user_picks(ctx.db, int(user.user_id))
    return redirect(_user_catalog_return_url(ctx, user, form, _int_param(form.get("region"))),
                    msg=f"已清空待选座位（{count} 个）。")


@router.get("/booking/{plan_id}")
def booking_detail(request: Request, plan_id: int):
    ctx = _booking_ctx(request)
    user = _require_user(request, ctx)
    plan = _owned_plan_or_404(ctx, user, plan_id)
    now = _now()
    ok, reason = _user_can_modify(
        plan, has_active_run=repo.plan_has_active_run(ctx.db, plan_id)
    )
    return _booking_render(
        request, "booking_detail.html",
        title=f"预约 #{plan_id}",
        plan=plan,
        user=user,
        cred=_credential_context(ctx, user),
        origin=repo.origin_label(repo.get_plan_origin(ctx.db, plan_id)),
        when_label=plan_when_label(plan, now=now),
        window_state=plan_window_state(plan, now=now),
        seat_window=(clock_of(plan["book_start"]), clock_of(plan["book_end"])),
        last_run=repo.last_run_for_plan(ctx.db, plan_id),
        runs=repo.list_runs(ctx.db, plan_id=plan_id, limit=10),
        run_duration_text=repo.run_duration_text,
        can_modify=ok,
        modify_blocked_reason=reason,
        delete_blocked_reason=_user_delete_blocked(ctx, plan),
    )


@router.get("/booking/{plan_id}/edit")
def booking_edit_form(request: Request, plan_id: int):
    ctx = _booking_ctx(request)
    user = _require_user(request, ctx)
    plan = _owned_plan_or_404(ctx, user, plan_id)
    ok, reason = _user_can_modify(
        plan, has_active_run=repo.plan_has_active_run(ctx.db, plan_id)
    )
    if not ok:
        return redirect(f"/booking/{plan_id}", msg=reason, ok=False)
    extra = _booking_form_ctx(
        ctx, user, _booking_values(plan), existing_seats=plan["preferred_seats"]
    )
    return _booking_render(
        request, "booking_form.html",
        title=f"修改预约 #{plan_id}", plan=plan, errors=[], warnings=[],
        action=f"/booking/{plan_id}/edit",
        user=user, cred=_credential_context(ctx, user), **extra,
    )


@router.post("/booking/{plan_id}/edit")
async def booking_edit_submit(request: Request, plan_id: int):
    ctx = _booking_ctx(request)
    form = dict(await request.form())
    check_booking_submit(request, form)
    user = _require_user(request, ctx)
    plan = _owned_plan_or_404(ctx, user, plan_id)
    ok, reason = _user_can_modify(
        plan, has_active_run=repo.plan_has_active_run(ctx.db, plan_id)
    )
    if not ok:
        return redirect(f"/booking/{plan_id}", msg=reason, ok=False)
    try:
        data, warnings = _validated_plan_form(form)
    except ValidationError as exc:
        return _booking_form_error(request, ctx, plan_id=plan_id,
                                   title=f"修改预约 #{plan_id}",
                                   action=f"/booking/{plan_id}/edit",
                                   form=form, errors=exc.errors, user=user)
    repo.update_plan(ctx.db, plan_id, data)
    # 与管理端同一条规则：草稿已经并入计划内容，清自己的那份以免串到下一个计划。
    repo.clear_user_picks(ctx.db, int(user.user_id))
    logger.info("用户端修改计划 #%s（用户 #%s）", plan_id, user.user_id)
    msg = "预约已保存（执行状态未改变）。"
    if warnings:
        msg += " 提示：" + "；".join(warnings)
    return redirect(f"/booking/{plan_id}", msg=msg)


async def _booking_action(request: Request, plan_id: int, action: str):
    ctx = _booking_ctx(request)
    form = dict(await request.form())
    check_booking_submit(request, form)
    user = _require_user(request, ctx)
    plan = _owned_plan_or_404(ctx, user, plan_id)

    if action == "delete":
        ok, reason = _user_can_modify(
            plan, has_active_run=repo.plan_has_active_run(ctx.db, plan_id)
        )
        if not ok:
            return redirect(f"/booking/{plan_id}", msg=reason, ok=False)
        blocked = _user_delete_blocked(ctx, plan)
        if blocked:
            return redirect(f"/booking/{plan_id}", msg=blocked, ok=False)
        repo.delete_plan(ctx.db, plan_id)
        logger.info("用户端删除计划 #%s（用户 #%s）", plan_id, user.user_id)
        return redirect("/booking", msg=f"预约 #{plan_id} 已删除。")

    if action == "pause":
        result = repo.pause_plan(ctx.db, plan_id)
    elif action == "enable":
        # UNKNOWN 必须显式确认才能恢复 —— 这条规则由状态机（apply_resume）**唯一**持有，
        # 用户端只负责把「已核实」这个勾选原样传下去，绝不自己复制一份判断，
        # 否则两处判据迟早会分叉，而分叉的一定是这道保护。
        ack = str(form.get("acknowledge_unknown") or "").strip().lower() in (
            "1", "on", "true", "yes"
        )
        result = repo.resume_plan(ctx.db, plan_id, acknowledge_unknown=ack)
    else:  # pragma: no cover - 路由已限定
        raise HTTPException(status_code=400, detail="未知操作")

    logger.info("用户端计划 #%s 执行操作 %s：%s", plan_id, action, result.get("message"))
    return redirect(f"/booking/{plan_id}", msg=result.get("message", ""),
                    ok=bool(result.get("ok")))


@router.post("/booking/{plan_id}/pause")
async def booking_pause(request: Request, plan_id: int):
    return await _booking_action(request, plan_id, "pause")


@router.post("/booking/{plan_id}/enable")
async def booking_enable(request: Request, plan_id: int):
    return await _booking_action(request, plan_id, "enable")


@router.post("/booking/{plan_id}/delete")
async def booking_delete(request: Request, plan_id: int):
    return await _booking_action(request, plan_id, "delete")


# ================================================================== 错误页


@router.get("/admin/plans/{plan_id}/runs", include_in_schema=False)
def plan_runs_legacy(plan_id: int):
    return RedirectResponse(f"/admin/runs?plan_id={plan_id}", status_code=303)