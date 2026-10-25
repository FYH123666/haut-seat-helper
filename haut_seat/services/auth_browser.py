"""浏览器辅助的人工登录采集层（本项目**唯一**知道 Playwright 的模块）。

它只做一件事：打开一个**可见的、持久化的**浏览器窗口，请用户本人完成一次正常登录，
然后从该会话中取出 cookie ``Admin-Token``，连同推导出的 ``Authorization`` 一起交给
:mod:`haut_seat.services.auth_store` 落盘成受限快照。

三条不可逾越的边界（写死在代码里，也写在 README 里）：

- **不代填账号口令**：本模块从不接触用户名/密码输入框；``config`` 里那两个
  ``token``/``username``/``password`` 占位字段至今无人读取；
- **不识别、不绕过验证码**：登录页有图形验证码（``GET /stage-api/captchaImage``
  返回真实图片），因此"全自动登录"在本项目里既不合规也不可行，人工登录是唯一路径；
- **不用无头模式去"探"接口**：``headless=False`` 是硬编码 —— 登录必须有人看着。

``Authorization`` 的来源是**已证实的推导关系**，不是猜测：站点自己的请求拦截器执行
``e.headers["Authorization"] = "Bearer " + getToken()``，而 ``getToken()`` 读的正是
cookie ``Admin-Token``；用户 ``.env`` 里两值的 sha256 完全一致（见
``docs/auth-recon.md``）。因此这里直接按该关系推导，不再去"观察实时请求头" ——
那只会增加时序复杂度，却不增加任何确定性。

**为什么必须显式取值，而不能指望持久化目录**：``Admin-Token`` 是会话级 Cookie
（无 ``expires``/``max-age``），不会写进持久化 profile 的 cookie 库。
``user_data_dir`` 的作用是"把自动化与日常浏览器隔离开、并保留站点在其它
Cookie/本地存储里留下的东西"，**不是**凭据来源，也**不能**减少本项目的登录次数 ——
令牌既然是会话级的，每次采集都得有人重新登一次。这一点如实写在文档里。

本模块**绝不**执行 ``playwright install``、绝不下载任何浏览器：
默认复用系统已装的 Edge（``channel="msedge"``）。
"""

from __future__ import annotations

import importlib.util
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Callable, Protocol

from haut_seat.services.auth_store import (
    DEFAULT_SNAPSHOT_SOURCE,
    fingerprint,
    format_time,
)

#: 承载会话令牌的 Cookie 名（实测：站点 ``getToken()`` 读的就是它）
COOKIE_NAME = "Admin-Token"

#: 依赖未安装时的补救命令（对外文案统一用这一份）
INSTALL_HINT = 'pip install -e ".[auth]"'

#: 打开窗口后给用户的提示。落到 CLI 与网页按钮上，措辞必须诚实：
#: 这是**人工**登录，不是自动登录。
LOGIN_PROMPT = (
    "已打开浏览器窗口，请在其中正常登录图书馆网站。\n"
    "  * 如出现图形验证码，请由你本人完成识别 —— 本项目不代填账号口令、不识别验证码。\n"
    "  * 登录成功后窗口会自动关闭；若中途手动关闭窗口，本次采集即告失败。"
)

#: 导航到登录页的超时封顶（秒）。登录等待可以是 5 分钟，
#: 但"打开登录页"这一步绝不该等那么久。
_NAV_TIMEOUT_CAP_S = 30.0
_NAV_TIMEOUT_FLOOR_S = 5.0


class BrowserLoginError(RuntimeError):
    """采集过程中的失败。消息只描述状态，绝不含任何凭据值。"""


class BrowserNotInstalledError(BrowserLoginError):
    """Playwright 未安装（``[auth]`` extra 未装）。"""


class BrowserLaunchError(BrowserLoginError):
    """浏览器起不来：未装 Edge/Chromium、无桌面会话、profile 被占用等。"""


class BrowserSession(Protocol):
    """已打开的浏览器会话（测试用假实现替换，CI 无需任何浏览器）。"""

    def get_cookie(self, name: str) -> str | None:
        """返回该名字的 Cookie 值；不存在返回 None。"""
        ...

    def is_closed(self) -> bool:
        """用户是否已经把窗口关掉了。"""
        ...

    def close(self) -> None:
        """关闭会话并释放驱动进程；重复调用必须安全。"""
        ...


class BrowserBackend(Protocol):
    """打开一个可见的持久化浏览器窗口并导航到登录页。"""

    def open(
        self,
        *,
        user_data_dir: str,
        channel: str,
        headless: bool,
        login_url: str,
        timeout_s: float,
    ) -> BrowserSession: ...


@dataclass(frozen=True)
class LoginCapture:
    """一次成功的人工登录采集结果（**含凭据值**，因此强制脱敏 repr）。"""

    cookie: str
    authorization: str
    captured_at: str
    source: str = DEFAULT_SNAPSHOT_SOURCE

    def fingerprint(self) -> str:
        return fingerprint(self.authorization)

    def __repr__(self) -> str:  # pragma: no cover - 简洁性由测试钉住
        return (
            f"LoginCapture(source={self.source!r}, captured_at={self.captured_at!r}, "
            f"fingerprint={self.fingerprint()!r}, "
            f"cookie=<redacted>, authorization=<redacted>)"
        )


def _probe_playwright() -> tuple[bool, str]:
    """真实探测：只问"装没装"，**不 import** playwright。

    ``find_spec`` 不会加载模块，因此在没装的时候既不会抛异常，
    也不会因为 import 失败而掩盖原因。单独成函数是为了让测试可以替换它。
    """
    try:
        spec = importlib.util.find_spec("playwright")
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        return False, f"未安装 playwright（{INSTALL_HINT}）；安装后仅需人工登录一步，不会下载浏览器。"
    return True, ""


@lru_cache(maxsize=1)
def browser_availability() -> tuple[bool, str]:
    """浏览器自动化是否可用 + 不可用的原因（空串表示可用）。

    结果缓存：网页每个请求都会问一次，而"装没装"不会在进程运行期间改变。
    测试改环境后需调用 ``browser_availability.cache_clear()``。
    """
    return _probe_playwright()


class PlaywrightSession:
    """``BrowserSession`` 的真实实现：薄薄一层，包住 persistent context 与驱动。"""

    def __init__(self, context, driver) -> None:
        self._context = context
        self._driver = driver

    def get_cookie(self, name: str) -> str | None:
        try:
            cookies = self._context.cookies()
        except Exception:  # 驱动已退出 → 视为没有 Cookie，由上层给出"窗口已关闭"
            return None
        for item in cookies or ():
            if item.get("name") == name and item.get("value"):
                return str(item["value"])
        return None

    def is_closed(self) -> bool:
        """所有页面都没了 = 用户关掉了窗口。

        取不到状态时**按已关闭处理**：让上层的报错是"窗口已关闭"这一确定结论，
        而不是把一个说不清的状态拖到超时。
        """
        try:
            return not self._context.pages
        except Exception:
            return True

    def close(self) -> None:
        for closer in (self._context.close, self._driver.stop):
            try:
                closer()
            except Exception:
                pass


class PlaywrightEdgeBackend:
    """默认后端：复用系统已装的 Edge（``channel="msedge"``），不下载浏览器。"""

    def open(
        self,
        *,
        user_data_dir: str,
        channel: str,
        headless: bool,
        login_url: str,
        timeout_s: float,
    ) -> BrowserSession:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # 依赖没装
            raise BrowserNotInstalledError(f"{INSTALL_HINT}（{type(exc).__name__}）") from exc

        try:
            driver = sync_playwright().start()
        except Exception as exc:
            raise BrowserLaunchError(
                f"无法启动 playwright 驱动：{type(exc).__name__}。"
                "本机缺少运行库或驱动被安全软件拦截时可出现此情况。"
            ) from exc

        try:
            context = driver.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                channel=channel,
                headless=headless,
                viewport=None,
                args=["--no-first-run", "--no-default-browser-check"],
            )
        except Exception as exc:
            self._stop_quietly(driver)
            raise BrowserLaunchError(
                f"无法打开浏览器（channel={channel}）：{type(exc).__name__}。"
                "常见原因：未安装该浏览器、当前会话没有桌面（服务/无桌面环境）、"
                "或该用户目录已被另一个浏览器实例占用。"
            ) from exc

        session = PlaywrightSession(context, driver)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            nav_timeout_ms = int(
                min(_NAV_TIMEOUT_CAP_S, max(_NAV_TIMEOUT_FLOOR_S, timeout_s)) * 1000
            )
            page.goto(login_url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
        except Exception as exc:
            session.close()  # 打开失败的窗口必须收掉，否则残留进程
            raise BrowserLaunchError(
                f"浏览器已打开但无法加载登录页：{type(exc).__name__}。"
                "请确认网络可达且站点地址正确。"
            ) from exc
        return session

    @staticmethod
    def _stop_quietly(driver) -> None:
        try:
            driver.stop()
        except Exception:
            pass


def capture_login(
    settings,
    *,
    backend: BrowserBackend | None = None,
    logger: logging.Logger | None = None,
    on_prompt: Callable[[str], None] | None = None,
    timeout_s: float | None = None,
    poll_interval_s: float = 1.0,
    now: datetime | None = None,
) -> LoginCapture:
    """打开浏览器请用户人工登录，成功后返回采集到的凭据。

    **只由人工显式触发**（CLI ``auth-login`` 或网页按钮），预约路径永不调用它。
    无论成功、超时还是异常，``finally`` 里都会关掉会话，不留残留进程。

    ``timeout_s`` 未给出时取 ``settings.auth_login_timeout_s``（默认 300 秒，
    留足人输验证码的时间）。
    """
    log = logger or logging.getLogger("haut_seat.auth")
    limit = float(settings.auth_login_timeout_s if timeout_s is None else timeout_s)
    profile_dir = Path(settings.auth_profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    chosen = backend or PlaywrightEdgeBackend()
    session = chosen.open(
        user_data_dir=str(profile_dir),
        channel=settings.auth_browser_channel,
        headless=False,  # 硬编码：登录必须有人看着，绝不静默改成无头
        login_url=settings.base_url,
        timeout_s=limit,
    )
    log.info(
        "已打开登录浏览器（user_data_dir=%s channel=%s，等待上限 %ss）",
        profile_dir,
        settings.auth_browser_channel,
        f"{limit:.0f}",
    )
    if on_prompt is not None:
        on_prompt(LOGIN_PROMPT)

    try:
        return _poll_for_token(
            session, timeout_s=limit, poll_interval_s=poll_interval_s, logger=log, now=now
        )
    finally:
        session.close()


def _poll_for_token(
    session: BrowserSession,
    *,
    timeout_s: float,
    poll_interval_s: float,
    logger: logging.Logger,
    now: datetime | None = None,
) -> LoginCapture:
    """轮询会话 Cookie 直到拿到 ``Admin-Token``、窗口被关掉或超时。

    顺序刻意是"先看 Cookie，再看关没关，最后看超时"：用户登录完立刻关窗口是
    很自然的动作，此时令牌已经拿到，必须先取走而不是判成失败。
    """
    deadline = time.monotonic() + timeout_s
    while True:
        token = session.get_cookie(COOKIE_NAME)
        if token:
            # 只记长度与采集时间，绝不记值
            logger.info(
                "已从浏览器会话取得 %s（长度 %d，值不入日志）", COOKIE_NAME, len(token)
            )
            return LoginCapture(
                cookie=f"{COOKIE_NAME}={token}",
                authorization=f"Bearer {token}",
                captured_at=format_time(now or datetime.now()),
            )
        if session.is_closed():
            raise BrowserLoginError(
                f"浏览器窗口已关闭，但未取得 {COOKIE_NAME}（登录未完成？）。"
                "请重新运行 python -m haut_seat auth-login 并完成登录后再关闭窗口。"
            )
        if time.monotonic() >= deadline:
            raise BrowserLoginError(
                f"等待登录超时（{timeout_s:.0f}s），未取得 {COOKIE_NAME}。"
                "请确认已在弹出的浏览器窗口中完成登录"
                "（含图形验证码）；可用 --timeout 放宽等待时间。"
            )
        time.sleep(max(0.0, poll_interval_s))