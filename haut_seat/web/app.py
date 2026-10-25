"""FastAPI 应用工厂：装配数据库、服务层、调度器与路由。

约束落实：
- 启动 Web 服务**不会**执行任何真实预约：调度器只在计划被显式「启用」且
  到达触发时刻后才会启动运行，且受全局暂停默认开启 + ``HAUT_ENABLE_REAL_BOOKING``
  两道闸门限制；
- 长任务不在请求线程中执行：所有执行入口都只是「登记 + 起后台线程」；
- 默认只监听 ``127.0.0.1``（见 ``config.web_host``）。
"""

from __future__ import annotations

import logging
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from haut_seat import __version__
from haut_seat.services.auth_login_service import LoginLauncher
from haut_seat.services.auth_service import AuthService
from haut_seat.services.catalog_service import CatalogService
from haut_seat.services.notification_service import NotificationService
from haut_seat.services.remote_browser import RemoteBrowserManager
from haut_seat.services.scheduler import Scheduler
from haut_seat.web import security
from haut_seat.web.db import Database
from haut_seat.web.deps import STATIC_DIR, render
from haut_seat.web.routes import router

# 安全响应头。
# 脚本一律只允许同源（'self'）——**故意不放行任何 CDN**：
# 面板里没有内联事件处理器，也不加载 Bootstrap 的 JS 包（会被这条策略拦掉），
# 导航折叠与提示关闭由本地 app.js 用原生 JS 实现。
# style-src 放行 jsdelivr 是因为基础样式表用的是 Bootstrap 的 CSS；
# 若把该 CSS 改为本地托管，应同时删掉这里的 CDN 来源。
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "script-src 'self'; "
        "img-src 'self' data:; "
        "form-action 'self'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'"
    ),
}


def create_app(
    settings,
    *,
    logger: logging.Logger | None = None,
    db: Database | None = None,
    scheduler: Scheduler | None = None,
    start_scheduler: bool = True,
    **overrides,
) -> FastAPI:
    """构造应用。``start_scheduler=False`` 供测试使用（不自动起后台线程）。"""
    logger = logger or logging.getLogger("haut_seat.web")
    database = db or Database(settings.web_db)
    database.init_schema()

    notification = overrides.get("notification") or NotificationService(database, logger)
    auth_service = overrides.get("auth_service") or AuthService(database, settings, logger)
    # 座位目录服务：默认依赖只做**只读** layout/query（见 services/catalog_service）；
    # 测试通过 overrides 注入离线替身，避免打到学校服务器。
    catalog_service = overrides.get("catalog_service") or CatalogService(
        database, settings, logger
    )
    # 一键人工登录：默认只"登记 + 起脱离进程"，绝不在请求线程里跑浏览器。
    # 测试通过 overrides 注入替身，因此 CI 里绝不会真的 Popen。
    login_launcher = overrides.get("auth_login_launcher") or LoginLauncher(
        settings, database, logger
    )
    # 远程浏览器（服务器端无头 Chromium + 画面/键鼠转发）。构造期**完全惰性**：
    # 不 import playwright、不起线程、不开浏览器 —— 因此测试里（默认不注入替身）
    # 构造出真实管理器也绝不会去拉起浏览器。
    remote_browser = overrides.get("remote_browser") or RemoteBrowserManager(
        settings, database, logger
    )
    sched = scheduler or Scheduler(
        database, settings, logger,
        tick_s=settings.scheduler_tick_s,
        max_concurrent=settings.scheduler_max_concurrent,
        trigger_lead_s=settings.scheduler_trigger_lead_s,
        notification=notification,
        auth_service=auth_service,
        **{k: v for k, v in overrides.items() if k in {
            "query_fn", "submit_fn", "auth_check_fn", "clock", "time_mod", "wait_fn",
        }},
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db = database
        app.state.settings = settings
        app.state.scheduler = sched
        app.state.notification = notification
        app.state.auth_service = auth_service
        app.state.catalog_service = catalog_service
        app.state.auth_login_launcher = login_launcher
        app.state.remote_browser = remote_browser
        # 首次运行引导令牌（进程内、短时效），用于无会话时的 CSRF 防护
        app.state.setup_tokens = {}
        if start_scheduler and settings.web_enable_scheduler:
            sched.start()
        logger.info("Web 服务已就绪（%s:%s），调度器=%s",
                    settings.web_host, settings.web_port, sched.is_running())
        try:
            yield
        finally:
            if sched.is_running():
                sched.stop()
            # 远程浏览器会话必须收干净：关浏览器、停工作线程（有界，绝不死等）。
            remote_browser.shutdown()

    app = FastAPI(
        title="haut-seat-helper 管理面板",
        version=__version__,
        docs_url=None,        # 生产环境不暴露 Swagger/OpenAPI
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.setup_tokens = {}
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)

    # 让 lifespan 之外也能拿到（TestClient 未进入 lifespan 时的兜底）
    app.state.db = database
    app.state.settings = settings
    app.state.scheduler = sched
    app.state.notification = notification
    app.state.auth_service = auth_service
    app.state.catalog_service = catalog_service
    app.state.auth_login_launcher = login_launcher
    app.state.remote_browser = remote_browser

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        for key, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        if request.url.path.startswith("/admin"):
            # 管理页面绝不被缓存（避免共享设备上通过后退看到内容）
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(404)
    async def _not_found(request: Request, exc):
        if request.url.path.startswith("/admin"):
            return render(request, "error.html", title="页面不存在", status_code=404,
                          code=404, detail="请求的页面不存在。")
        return JSONResponse({"detail": "not found"}, status_code=404)

    @app.exception_handler(500)
    async def _server_error(request: Request, exc):  # pragma: no cover - 兜底
        logger.exception("Web 请求异常")
        return render(request, "error.html", title="服务异常", status_code=500,
                      code=500, detail="服务内部异常，请查看服务器日志。")

    return app


def new_setup_token(app: FastAPI, *, ttl_s: int = 900) -> str:
    """生成一次性引导令牌（用于「首次创建管理员」表单的 CSRF 防护）。"""
    token = secrets.token_urlsafe(24)
    now = time.time()
    store = getattr(app.state, "setup_tokens", None)
    if store is None:
        store = {}
        app.state.setup_tokens = store
    # 过期清理
    for key in [k for k, v in store.items() if v < now]:
        store.pop(key, None)
    store[token] = now + ttl_s
    return token


def consume_setup_token(app: FastAPI, token: str) -> bool:
    store = getattr(app.state, "setup_tokens", {}) or {}
    expires = store.pop(token, None)
    return expires is not None and expires >= time.time()


def session_cookie_kwargs(settings) -> dict:
    """会话 Cookie 属性：HttpOnly + SameSite=Lax（HTTPS 下再加 Secure）。"""
    return {
        "key": security.SESSION_COOKIE,
        "httponly": True,
        "samesite": "lax",
        "secure": bool(settings.web_session_secure),
        "max_age": security.SESSION_TTL_HOURS * 3600,
        "path": "/",
    }


def user_session_cookie_kwargs(settings) -> dict:
    """**用户端**会话 Cookie 属性（与管理员会话完全独立的一套 Cookie）。

    三处刻意的差异：

    - ``key`` 是 ``hs_user``，不是管理员的 ``hs_session``：两套身份在任何页面、
      任何接口上都不共用同一张票；
    - ``path`` 是 ``/booking``：管理端页面一次都收不到它，用户端令牌也就无从
      被管理端的代码路径误读；
    - ``max_age`` 用 ``HAUT_USER_SESSION_TTL_HOURS``（默认 72 小时，比管理员的
      12 小时长）：用户端是"手机上偶尔点一下"的场景，频繁被踢下线会直接导致
      预约任务在触发时刻因为"用户刚过期"而被拦住。

    HttpOnly + SameSite=Lax + （HTTPS 下）Secure 与管理员侧一致。
    """
    from haut_seat.web import user_auth

    return {
        "key": user_auth.USER_SESSION_COOKIE,
        "httponly": True,
        "samesite": "lax",
        "secure": bool(settings.web_session_secure),
        "max_age": int(getattr(settings, "user_session_ttl_hours", 72)) * 3600,
        "path": "/booking",
    }


__all__ = [
    "create_app", "new_setup_token", "consume_setup_token", "session_cookie_kwargs",
    "user_session_cookie_kwargs",
]