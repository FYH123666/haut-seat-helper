"""Web 层的共享依赖：模板、数据库、会话、CSRF、消息提示。

安全要点：
- 所有页面模板共享一份上下文，只注入**非敏感**信息；
  Cookie / Authorization / JWT / 口令绝不进入模板上下文或响应体；
- 状态变更（POST）一律要求有效会话 + CSRF 令牌；
- 未登录的 GET 跳转登录页，未登录的 POST 直接 401（不泄漏任何行为差异）；
- **用户端（``/booking``）现在也有自己的账号体系**：``app_user`` + ``user_session``
  与管理员那套是**完全分开**的表、Cookie（``hs_user``）与令牌空间，
  页面与写入口都要求先登录，每个用户只看得到、也只改得动自己的计划。
  在这个真实身份之上，用户端**仍然**保留双提交 Cookie + 同源校验
  （见 ``check_booking_submit``）作为**第二层**：会话 Cookie 是 HttpOnly 的，
  双提交令牌则要求请求方同时持有 Cookie 与表单字段 —— 跨站表单两样都拿不到。
  它是纵深防御，不是身份认证的替代品。
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from haut_seat import __version__
from haut_seat.services.state import (
    ERROR_CATEGORY_LABELS,
    PLAN_STATUS_LABELS,
    PLAN_STATUS_TONE,
    RUN_STATUS_LABELS,
    RUN_STATUS_TONE,
    PlanStatus,
    RunStatus,
)
from haut_seat.web import security
from haut_seat.web.validation import REPEAT_RULE_LABELS

logger = logging.getLogger("haut_seat.web.deps")

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

#: 用户端的双提交 CSRF Cookie 名。与管理员会话 Cookie 完全无关。
BOOKING_CSRF_COOKIE = "haut_booking_csrf"

#: Cookie 作用域：只在 ``/booking`` 下发送，管理员页面一次都拿不到它。
BOOKING_CSRF_PATH = "/booking"


@dataclass
class AppContext:
    db: Any
    settings: Any
    scheduler: Any
    notification: Any
    auth_service: Any
    catalog_service: Any
    auth_login_launcher: Any = None
    remote_browser: Any = None


def get_ctx(request: Request) -> AppContext:
    state = request.app.state
    return AppContext(
        db=state.db,
        settings=state.settings,
        scheduler=getattr(state, "scheduler", None),
        notification=getattr(state, "notification", None),
        auth_service=getattr(state, "auth_service", None),
        catalog_service=getattr(state, "catalog_service", None),
        auth_login_launcher=getattr(state, "auth_login_launcher", None),
        remote_browser=getattr(state, "remote_browser", None),
    )


def redirect(path: str, *, msg: str = "", ok: bool = True) -> RedirectResponse:
    """303 跳转 + 一次性提示（用 query string 传递，不落库、不含敏感信息）。

    ``path`` 可能自带查询串（例如座位目录的 ``?region=16``）：这时必须用 ``&``
    续接，否则会拼出两个 ``?``——后面整条查询串会被当成前一个参数的值，
    提示丢失、``region`` 也解析不出数字。空参数集直接回原 path，不留多余分隔符。
    """
    from urllib.parse import urlencode

    params: dict[str, str] = {}
    if msg:
        params["msg"] = msg[:400]
        params["ok"] = "1" if ok else "0"
    if not params:
        return RedirectResponse(path, status_code=303)
    query = urlencode(params)
    if "?" not in path:
        url = f"{path}?{query}"
    elif path.endswith("?") or path.endswith("&"):
        url = f"{path}{query}"
    else:
        url = f"{path}&{query}"
    return RedirectResponse(url, status_code=303)


def _session_token(request: Request) -> str:
    return request.cookies.get(security.SESSION_COOKIE, "")


def optional_admin(request: Request):
    """返回当前登录会话；未登录返回 None。"""
    ctx = get_ctx(request)
    return security.load_session(ctx.db, _session_token(request))


def require_admin(request: Request):
    """要求已登录；未登录时 GET 跳登录页、其它方法返回 401。"""
    session = optional_admin(request)
    if session is None:
        if request.method in ("GET", "HEAD"):
            raise HTTPException(
                status_code=303, headers={"Location": "/admin/login"}, detail="需要登录"
            )
        raise HTTPException(status_code=401, detail="需要登录")
    return session


def check_csrf(form: dict, session) -> None:
    """同步器令牌校验；失败即拒绝，不做任何状态变更。"""
    provided = ""
    try:
        provided = str(form.get("_csrf") or "")
    except Exception:
        provided = ""
    if not security.csrf_matches(session.csrf_token, provided):
        raise HTTPException(
            status_code=400,
            detail="CSRF 校验失败：请返回上一页刷新后重试（令牌与当前会话不匹配）。",
        )


# ------------------------------------------------------ 用户端的浏览器侧限制
#
# 用户端**现在有自己的账号体系**（``app_user`` + ``user_session``，见
# ``haut_seat.web.user_auth``）：身份与"谁的计划"由它决定，这一节的东西**不是**
# 身份认证，而是在真实会话之上再叠一层浏览器侧防线（纵深防御）：
#
# 1. **同源校验**：POST 必须带 Origin（或 Referer），且主机与请求 Host 一致。
#    这是浏览器侧最直接的一道：跨站表单提交一定带的是对方的 Origin；
# 2. **双提交 Cookie CSRF**：服务端种一个随机值到 ``path=/booking`` 的 Cookie
#    并在表单里回填同一个值，提交时两者必须一致。Cookie 是 ``SameSite=Lax``，
#    因此跨站 POST 根本不会带上它 —— 校验必然失败。
#    注意它与身份会话是**两个**令牌：``hs_user`` 是 HttpOnly 的会话 Cookie，
#    谁都不该能读出它的值来当 CSRF 令牌使用。
#
# 这两道曾经是用户端**唯一**的访问限制，那时它确实是"共享使用模式"；
# 现在身份由会话决定，它们退回成第二层。**只监听回环地址**（``HAUT_WEB_HOST``）
# 仍然是"不要把面板暴露到局域网"的第一道部署侧防线（多人使用时按需求改为
# 公网 HTTPS + 反向代理，见 README）。


def booking_csrf_token(request: Request) -> str:
    """取（或生成）用户端的 CSRF 令牌。生成的值由调用方写回 Cookie。"""
    return request.cookies.get(BOOKING_CSRF_COOKIE) or secrets.token_urlsafe(32)


def set_booking_csrf_cookie(response, request: Request, token: str, settings):
    """把令牌种进 Cookie（已经是这个值就什么都不做，避免每次都写响应头）。"""
    if request.cookies.get(BOOKING_CSRF_COOKIE) == token:
        return response
    response.set_cookie(
        key=BOOKING_CSRF_COOKIE,
        value=token,
        path=BOOKING_CSRF_PATH,
        httponly=True,          # 页面脚本不需要读它：模板由服务端回填
        samesite="lax",         # 跨站 POST 不携带 ⇒ 双提交校验必然失败
        secure=bool(getattr(settings, "web_session_secure", False)),
    )
    return response


def _same_origin(request: Request) -> bool:
    """POST 的来源主机是否就是本站主机。

    优先用 ``Origin``；没有则退回 ``Referer`` 的主机部分。两者都没有 ⇒ **拒绝**：
    现代浏览器对 POST 一定会带 ``Origin``，缺失意味着请求不是从本站页面发出的
    （或来自不按规范行事的客户端）。
    """
    host = (request.headers.get("host") or "").strip().lower()
    if not host:
        return False
    source = (request.headers.get("origin") or "").strip()
    if not source:
        source = (request.headers.get("referer") or "").strip()
    if not source:
        return False
    try:
        parts = urlsplit(source)
    except ValueError:
        return False
    return (parts.netloc or "").strip().lower() == host


def check_booking_submit(request: Request, form) -> None:
    """用户端 POST 的统一守卫：同源 + 双提交 CSRF。任何一条不过即拒绝。"""
    if not _same_origin(request):
        logger.warning(
            "用户端 POST 被拒绝：来源与本站不一致或缺少 Origin/Referer（%s）",
            request.url.path,
        )
        raise HTTPException(
            status_code=403,
            detail="来源校验失败：请从本站页面提交（跨站提交或缺少来源信息都会被拒绝）。",
        )
    provided = ""
    try:
        provided = str(form.get("_csrf") or "")
    except Exception:
        provided = ""
    if not security.csrf_matches(request.cookies.get(BOOKING_CSRF_COOKIE, ""), provided):
        raise HTTPException(
            status_code=400,
            detail="表单校验失败：请返回上一页刷新后重试（令牌与 Cookie 不匹配）。",
        )


def client_ip(request: Request, *, trust_proxy: bool = False) -> str:
    """登录限流用的客户端 IP。

    ``trust_proxy=False``（默认）：用 ``request.client.host``。uvicorn 默认只信任
    本机来源（``forwarded_allow_ips=127.0.0.1,::1``），并按「从右往左第一个不受
    信任的地址」解析 ``X-Forwarded-For``；因此在同机 nginx 之后运行时，这里拿到的
    已经是代理追加的真实来源地址，不需要再自己解析请求头。

    ``trust_proxy=True``：取 ``X-Forwarded-For`` 的**最右侧**一项。该项由紧邻的
    可信代理追加（nginx 的 ``$proxy_add_x_forwarded_for`` 会追加 ``$remote_addr``，
    覆盖式写法则整条就是 ``$remote_addr``），而它左侧的各项是客户端可以自行伪造的。
    只信任真实代理追加的那一项；取最左项等于把限流键交给攻击者
    —— 每个请求换一个伪造值就能绕过按 IP 的锁定。
    """
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-1][:64]
    return (request.client.host if request.client else "") or ""


def base_context(request: Request, session=None, *, title: str = "") -> dict:
    """所有页面共享的模板上下文（仅非敏感信息）。"""
    ctx = get_ctx(request)
    msg = request.query_params.get("msg", "")
    ok = request.query_params.get("ok", "1") == "1"
    state = ctx.auth_service.current() if ctx.auth_service is not None else None
    return {
        "version": __version__,
        "title": title,
        "admin": session.username if session else "",
        "csrf_token": session.csrf_token if session else "",
        "flash": msg,
        "flash_ok": ok,
        "auth_view": state,
        "plan_labels": PLAN_STATUS_LABELS,
        "plan_tones": PLAN_STATUS_TONE,
        "run_labels": RUN_STATUS_LABELS,
        "run_tones": RUN_STATUS_TONE,
        "error_labels": ERROR_CATEGORY_LABELS,
        "repeat_labels": REPEAT_RULE_LABELS,
        "PlanStatus": PlanStatus,
        "RunStatus": RunStatus,
        "base_url": ctx.settings.base_url,
        "real_booking_enabled": bool(ctx.settings.enable_real_booking),
    }


def render(request: Request, name: str, session=None, *, status_code: int = 200, **extra):
    context = base_context(request, session)
    context.update(extra)
    return templates.TemplateResponse(request, name, context, status_code=status_code)