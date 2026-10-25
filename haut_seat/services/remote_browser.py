"""公网面板里的**远程浏览器**：服务器端跑无头 Chromium，画面与键鼠走 HTTP 转发。

## 为什么需要它

``auth_browser.capture_login`` 硬编码 ``headless=False`` —— 那里是真需求：人工必须
**看到**登录页。可一旦部署到没有桌面的服务器（systemd 服务、会话 0、Linux 无 X），
"重新登录"这一步就断了，用户只能回去手抄 F12。本模块补上的正是那块画面：无头浏览器
照旧，画面用截图轮询送回管理员的浏览器，键鼠事件反向转发回去。

**人工环节一步都没省**：图形验证码仍由你本人识别，项目不代填账号口令、不识别验证码、
不绕过任何访问控制、也不新增远程调试端口。模块不做的事（有文本守卫测试钉住）：
不调用任何预约/提交接口、不出现 ``--no-sandbox``、不碰每用户凭据。

## 为什么是"截图轮询 + 输入转发"而不是别的

1. **iframe 嵌自己的流**会被本服务自己发的 ``X-Frame-Options: DENY`` 与
   CSP ``frame-ancestors 'none'`` 拦死（``web/app.py:39-53``）；
2. ``URL.createObjectURL`` 的画面被 CSP 拦死 —— ``img-src`` 里**没有** ``blob:``
   （``web/app.py:48``），所以画面走普通 ``<img src>``（``'self'``）；
3. 全仓库**没有**任何 WebSocket / SSE / ``StreamingResponse`` 基础设施，
   引入长连接等于新造一套机制，还要改 CSP。截图轮询零新依赖、零头改动。

## 线程模型（照抄 ``auth_login_service`` 已证明的结论）

``playwright.sync_api`` **不能**在活的 asyncio 事件循环线程里运行（会直接抛
"It looks like you are using Playwright Sync API inside the asyncio loop"）。因此：

* Playwright 的**全部**调用（``sync_playwright().start()`` 到 ``driver.stop()``）
  都发生在**一个专用工作线程**里，线程是 ``daemon=True``；
* 请求线程只做两件事：读内存里最新的一帧、往队列里塞事件（满即丢，绝不阻塞）；
* 停会话只"发信号 + 有界 join"，工作线程可能会卡在浏览器调用上，绝不让它拖住
  uvicorn 的优雅关闭（systemd ``TimeoutStopSec=30``）。

## 凭据

状态机是**显式**的：``idle``（未启动）→ ``launching``（起浏览器）→ ``awaiting_login``
（等待你登录）→ ``verifying``（已提取到令牌，正在用业务接口验证）→ ``captured``（成功）
/ ``failed``（失败，含 ``timed_out`` 超时、``cancelled`` 取消、``closed`` 浏览器被关）。
每一个终态都会**停止**页面的加载并给出可理解的结果（``reason_label``），不存在"一直转圈"。

采集成功后走的是**既有**那条链，但顺序是**先验证、后落盘**：``LoginCapture`` →
业务接口验证（``auth_precheck``，实测唯一能区分「被接受 / 未被接受」的只读端点
``GET /stage-api/getInfo``）→ 通过后才 ``save_snapshot`` → ``record_login_state`` /
``record_credential_source``。**「Cookie 非空」永远不等于登录成功**；未通过验证时
不写快照（免得把上一份能用的快照冲掉），原凭据保持不变。

写的是**全局快照** ``data/auth/credentials.json``，**绝不触碰** ``user_credential``
表里的每用户加密凭据 —— 有属主（``user_plan``）的计划永远用自己的凭据
（``scheduler._run_settings_for``），两者互不干扰。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

from haut_seat.services.auth_browser import (
    COOKIE_NAME,
    INSTALL_HINT,
    BrowserLaunchError,
    BrowserNotInstalledError,
    LoginCapture,
    browser_availability,
)
from haut_seat.services.auth_login_service import (
    ACTIVE_LOGIN_STATES,
    LOGIN_AWAITING,
    LOGIN_CAPTURED,
    LOGIN_FAILED,
    LOGIN_IDLE,
    LOGIN_LAUNCHING,
    LOGIN_VERIFYING,
    active_login_state,
    login_state_label,
    record_credential_source,
    record_login_state,
)
from haut_seat.services.auth_store import DEFAULT_SNAPSHOT_SOURCE, format_time, save_snapshot

# ---- 转发边界（都是硬上限，不是建议值）----

#: 单次请求最多转发多少条事件。再多就是异常或滥用，直接拒绝而不是排队。
MAX_EVENTS = 64
#: ``/input`` 请求体的字节上限。这份 body **逐字就是键盘记录**，绝不能无上限地读。
MAX_INPUT_BYTES = 8 * 1024
#: 单条文本（IME 组合结果）的长度上限。
MAX_TEXT = 256
#: 输入队列长度：满了就丢。宁可丢几个移动事件，也绝不让请求线程等。
INPUT_QUEUE_MAX = 256

#: 允许转发的具名键（**白名单**）。不接收任意 key spec —— 那等于把 Playwright 的
#: 按键语法暴露给请求体。
_NAMED_KEYS = frozenset({
    "Enter", "Tab", "Backspace", "Delete", "Escape", "Insert",
    "Home", "End", "PageUp", "PageDown",
    "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "F5",
})
_MODIFIERS = (("ctrl", "Control"), ("alt", "Alt"), ("shift", "Shift"), ("meta", "Meta"))
_MOUSE_BUTTONS = frozenset({"left", "right", "middle"})

#: 浏览器启动参数。``--disable-dev-shm-usage`` 是给 ``PrivateTmp=true`` /
#: ``PrivateDevices=true`` 的 systemd 单元兜底的（``/dev/shm`` 可能缺失或过小，
#: Chromium 的渲染进程会因此崩掉）。**绝不放 ``--no-sandbox``**：沙箱起不来就
#: 如实报错，那是部署问题，不是拿掉安全边界来解决的事。
_LAUNCH_ARGS = ("--no-first-run", "--no-default-browser-check", "--disable-dev-shm-usage")

#: 心跳间隔：刷新 ``login_updated_at``，否则会话超过 ``timeout+60s`` 会被
#: ``LoginLauncher`` 当成残留（于是单飞锁失效）。同时这也让 CLI 侧"看得见"
#: 远程会话正在进行中。
_HEARTBEAT_S = 30.0
#: 工作循环的唤醒粒度：输入事件会立刻 set 这个 Event，所以正常路径不靠它等。
_POLL_S = 0.05
#: 页面轮询用的状态保留时长：会话结束后把终态留一会儿，让页面读到结果。
_TERMINAL_KEEP_S = 600.0
#: 路由里 ``/stop`` 的最长等待：工作线程正常 0.1s 内就能收掉浏览器。
STOP_JOIN_S = 2.0
#: lifespan 收尾的最长等待（systemd ``TimeoutStopSec=30``，绝不死等）。
SHUTDOWN_JOIN_S = 5.0

#: 令牌出现后，用业务接口验证凭据的重试节奏与次数上限。
#: 出现的令牌有可能是「学校页面刚写入、服务端尚未生效」，也可能是页面在没真正
#: 登录成功时留下的东西 —— 所以**先验证再落盘**，并且有界重试（绝不无限等）。
_VERIFY_RETRY_S = 5.0
_VERIFY_MAX_ATTEMPTS = 6

_AWAITING_MESSAGE = "等待你在远程画面里完成登录（含图形验证码）…"
_VERIFYING_MESSAGE = "已从学校页面取到令牌，正在用业务接口验证它是否真的可用…"

#: 终态原因 → 页面上的中文标签（需求：终态必须清楚、可理解，不能只给一个代号）。
REASON_LABELS: dict[str, str] = {
    "timed_out": "超时",
    "cancelled": "已取消",
    "closed": "浏览器已关闭",
    "unverified": "凭据未被业务接口接受",
    "unavailable": "浏览器自动化不可用",
    "launch": "浏览器启动失败",
    "snapshot_write": "凭据写入失败",
    "error": "异常结束",
    "stopped": "已结束",
}


def reason_label(reason: str) -> str:
    """终态原因的中文标签；未知原因原样返回（绝不吞掉信息）。"""
    return REASON_LABELS.get(reason, reason or "")


# ====================================================================== 事件翻译


def key_spec(event: dict) -> tuple[str, str] | None:
    """把一个 keydown 事件翻译成 ``("press", spec)`` 或 ``("text", 字面)``。

    返回 ``None`` 表示这条事件不受支持（不认识、或不在白名单里），调用方丢弃它。
    **映射放在服务端 Python 而不是 JS 里**：可单测，而且只有白名单里的键能到达
    Playwright —— 请求体永远无法直接指定 key spec。

    无修饰键的单字符走 ``insert_text``：它按字面插入，中文与符号都正确，
    也不受键盘布局影响（``keyboard.type`` 是逐键映射，非 ASCII 会丢字符）。
    带修饰键的走 ``press("Control+a")`` 这种组合。已知限制（如实在页面上写明）：
    不支持按键长按重复、不支持鼠标拖拽选择文本。
    """
    key = event.get("k")
    if not isinstance(key, str) or not key:
        return None
    mods = [name for attr, name in _MODIFIERS if event.get(attr)]
    if len(key) == 1:
        if mods:
            return ("press", "+".join([*mods, key]))
        return ("text", key)
    if key in _NAMED_KEYS:
        return ("press", "+".join([*mods, key]))
    return None


def _num(value: Any, *, lo: float = 0.0, hi: float) -> float:
    """把坐标收敛到视口范围内：拒绝 NaN/inf/负数/越界，异常值一律夹到边界。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return lo
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        return lo
    return min(max(number, lo), hi)


def sanitize_url(url: str) -> str:
    """只保留 ``scheme://host/path``：**query 与 fragment 一律丢掉**。

    登录跳转的 query 里可能带 token（``?redirect=…&token=…``），而这个字符串会
    原样出现在页面与 JSON 里。路径已经足够回答"现在走到哪一步了"。
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _cookie_belongs_to(cookie: dict, host: str) -> bool:
    """Cookie 的域是否属于本站（回退查找时的防串站检查）。"""
    if not host:
        return False
    domain = str(cookie.get("domain") or "").lstrip(".").lower()
    if not domain:
        return False
    host = host.lower()
    return host == domain or host.endswith("." + domain)


# ====================================================================== 会话接口


class InteractiveSession(Protocol):
    """一个可交互的无头浏览器页面。测试注入假实现 ⇒ CI 永不启动真浏览器。"""

    def goto(self, url: str, *, timeout_s: float) -> None: ...
    def screenshot(self, *, quality: int) -> bytes: ...
    def mouse_move(self, x: float, y: float) -> None: ...
    def click(self, x: float, y: float, *, button: str, click_count: int) -> None: ...
    def wheel(self, dx: float, dy: float) -> None: ...
    def press(self, spec: str) -> None: ...
    def insert_text(self, value: str) -> None: ...
    def current_url(self) -> str: ...
    def viewport_size(self) -> tuple[int, int]: ...
    def is_closed(self) -> bool: ...
    def cookie(self, name: str) -> str | None: ...
    def close(self) -> None: ...


class InteractiveBackend(Protocol):
    def open(
        self, *, channel: str, headless: bool, viewport: tuple[int, int],
        login_url: str, timeout_s: float,
    ) -> InteractiveSession: ...


# ================================================================== Playwright 实现


class PlaywrightInteractiveSession:
    """``InteractiveSession`` 的真实实现：薄薄一层，包住 page/context/browser/driver。

    用 ``launch()`` + ``new_context()``（**临时** profile），**不用**持久化
    ``user_data_dir``，三条理由：① ``Admin-Token`` 是 session cookie
    （``expires=-1``），持久化 profile 根本留不住它；② 与桌面 CLI 的
    ``data/auth/browser-profile`` 共享目录会撞 Chromium 的 profile 锁
    （同一个目录只能被一个实例占用）；③ 磁盘上不留任何 cookie。
    """

    def __init__(self, *, browser, context, page, driver, login_url: str) -> None:
        self._browser = browser
        self._context = context
        self._page = page
        self._driver = driver
        self._login_url = login_url

    # ---- 画面 ----

    def screenshot(self, *, quality: int) -> bytes:
        return self._page.screenshot(type="jpeg", quality=int(quality))

    def viewport_size(self) -> tuple[int, int]:
        size = self._page.viewport_size or {}
        return int(size.get("width") or 0), int(size.get("height") or 0)

    def current_url(self) -> str:
        try:
            return str(self._page.url or "")
        except Exception:
            return ""

    # ---- 输入 ----

    def goto(self, url: str, *, timeout_s: float) -> None:
        # 复用 auth_browser 的导航超时策略（上限 30s / 下限 5s），不另发明一套。
        from haut_seat.services.auth_browser import _NAV_TIMEOUT_CAP_S, _NAV_TIMEOUT_FLOOR_S

        limit = min(_NAV_TIMEOUT_CAP_S, max(_NAV_TIMEOUT_FLOOR_S, float(timeout_s)))
        self._page.goto(url, wait_until="domcontentloaded", timeout=int(limit * 1000))

    def mouse_move(self, x: float, y: float) -> None:
        self._page.mouse.move(x, y)

    def click(self, x: float, y: float, *, button: str, click_count: int) -> None:
        self._page.mouse.click(x, y, button=button, click_count=max(1, int(click_count)))

    def wheel(self, dx: float, dy: float) -> None:
        self._page.mouse.wheel(dx, dy)

    def press(self, spec: str) -> None:
        self._page.keyboard.press(spec)

    def insert_text(self, value: str) -> None:
        self._page.keyboard.insert_text(value)

    # ---- 状态 ----

    def is_closed(self) -> bool:
        try:
            if self._page.is_closed():
                return True
        except Exception:
            return True
        try:
            return not self._context.pages
        except Exception:
            return True

    def cookie(self, name: str) -> str | None:
        """取指定 Cookie 的值。

        先用 ``cookies(login_url)``（与 ``auth_browser`` 一致，按站点过滤）；
        取不到时回退到全量 Cookie，但**必须**确认域属于本站 —— 绝不接受别处的
        同名 Cookie（那会变成"把别人的令牌当自己的"）。
        """
        cookies: list = []
        try:
            cookies = list(self._context.cookies(self._login_url))
        except Exception:
            cookies = []
        if not any(c.get("name") == name for c in cookies):
            try:
                host = urlsplit(self._login_url).hostname or ""
                cookies = [
                    c for c in self._context.cookies() if _cookie_belongs_to(c, host)
                ]
            except Exception:
                return None
        for c in cookies:
            if c.get("name") == name:
                value = c.get("value")
                return value or None
        return None

    def close(self) -> None:
        for step in (self._context.close, self._browser.close, self._driver.stop):
            try:
                step()
            except Exception:
                pass


def _launch_hint(channel: str, exc: Exception) -> str:
    """把 Playwright 的启动异常翻译成**可执行**的提示（不泄露任何凭据）。"""
    if "Executable doesn't exist" in str(exc):
        # 实测踩过：playwright 版本与已下载的浏览器 revision 必须匹配。
        return (
            "找不到浏览器可执行文件：服务器上的 playwright 版本与已下载的 Chromium "
            "不匹配。请在服务器上执行 `python -m playwright install chromium`，"
            "并确认 PLAYWRIGHT_BROWSERS_PATH 指向项目下的 .cache/ms-playwright。"
            f"（{type(exc).__name__}）"
        )
    return (
        f"无法启动无头浏览器（channel={channel or 'bundled-chromium'}）："
        f"{type(exc).__name__}。常见原因：Chromium 未安装、systemd 限制了命名空间"
        "（RestrictNamespaces）、或 /dev/shm 太小。详见服务器日志里的浏览器错误。"
    )


class PlaywrightHeadlessBackend:
    """默认后端：playwright **自带**的 Chromium，无头运行。

    **不**复用 ``auth_browser_channel``（默认 ``msedge`` 是给桌面 CLI 用的，
    Linux 服务器上没有 Edge）。``channel=""`` 即用捆绑 Chromium。
    """

    def open(
        self, *, channel: str, headless: bool, viewport: tuple[int, int],
        login_url: str, timeout_s: float,
    ) -> InteractiveSession:
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

        browser = None
        try:
            kwargs: dict = {"headless": headless, "args": list(_LAUNCH_ARGS)}
            if channel:
                kwargs["channel"] = channel
            browser = driver.chromium.launch(**kwargs)
            context = browser.new_context(
                viewport={"width": int(viewport[0]), "height": int(viewport[1])}
            )
            page = context.new_page()
        except Exception as exc:
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            try:
                driver.stop()
            except Exception:
                pass
            raise BrowserLaunchError(_launch_hint(channel, exc)) from exc

        session = PlaywrightInteractiveSession(
            browser=browser, context=context, page=page, driver=driver, login_url=login_url
        )
        try:
            session.goto(login_url, timeout_s=timeout_s)
        except Exception as exc:
            session.close()  # 打开失败的窗口必须收掉，否则残留进程
            raise BrowserLaunchError(
                f"浏览器已打开但无法加载登录页：{type(exc).__name__}。"
                "请确认服务器能访问学校站点。"
            ) from exc
        return session


# ====================================================================== 会话状态


@dataclass
class _Session:
    """一次远程浏览器会话。**只存内存**，唯一的持久化是既有的登录状态元数据。"""

    admin_id: Any
    token_hash: str
    viewport: tuple[int, int]
    thread: threading.Thread | None = None
    state: str = LOGIN_LAUNCHING
    reason: str = ""
    message: str = ""
    precheck_status: str = ""
    precheck_message: str = ""
    url: str = ""
    captured_at: str = ""
    fingerprint: str = ""
    # 「提取凭据 → 验证凭据」这一步的状态。``verify_fingerprint`` 存的是令牌的
    # sha256 前几位（**不是令牌本身**）：令牌换了就重新计次、重新验证。
    verify_fingerprint: str = ""
    verify_attempts: int = 0
    next_verify_at: float = 0.0
    # 这两个时间戳由 ``start()`` 用管理器自己的时钟（可注入）赋值，**不用**
    # ``default_factory=time.monotonic``：会话的超时判定读的是 ``self._now()``，
    # 两者若来自不同的时钟源，注入假时钟的测试里"总时长"会永远算不出正数。
    started_at: float = 0.0
    last_input_at: float = 0.0
    finished_at: float | None = None
    frame: bytes | None = None
    frame_seq: int = 0
    frame_lock: threading.Lock = field(default_factory=threading.Lock)
    inputs: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=INPUT_QUEUE_MAX))
    wake: threading.Event = field(default_factory=threading.Event)
    stop_event: threading.Event = field(default_factory=threading.Event)

    def put_frame(self, data: bytes) -> None:
        with self.frame_lock:
            self.frame = data
            self.frame_seq += 1

    def get_frame(self) -> bytes | None:
        with self.frame_lock:
            return self.frame


# ====================================================================== 管理器


class RemoteBrowserManager:
    """全局**单个**远程浏览器会话：谁先开谁持有，别人既看不到也操作不了。

    ``__init__`` 刻意**完全惰性**：不 import playwright、不起线程、不开浏览器。
    这不是洁癖 —— ``create_app`` 在每个 web 测试里都会构造真实管理器
    （``tests/conftest.py`` 默认不注入替身），构造期一旦有副作用，整个测试套件
    就会去拉起真浏览器。
    """

    def __init__(
        self,
        settings,
        db=None,
        logger: logging.Logger | None = None,
        *,
        backend: InteractiveBackend | None = None,
        precheck: Callable[[LoginCapture], tuple[str, str]] | None = None,
        now: Callable[[], float] | None = None,
        wall: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.logger = logger or logging.getLogger("haut_seat.auth")
        self._backend = backend if backend is not None else PlaywrightHeadlessBackend()
        self._precheck = precheck or self._readonly_precheck
        self._now = now or time.monotonic
        self._wall = wall or datetime.now
        self._lock = threading.RLock()
        self._session: _Session | None = None

    # ------------------------------------------------------------ 只读视图

    def active(self) -> bool:
        """是否有会话正在进行（终态但保留结果的不算）。"""
        with self._lock:
            sess = self._session
        return sess is not None and sess.state in ACTIVE_LOGIN_STATES

    def has_session(self) -> bool:
        with self._lock:
            return self._session is not None

    def view_for(self, admin_id: Any, token_hash: str) -> dict | None:
        """属于调用者（或当前没有会话）时返回状态视图。

        会话属于**别的**管理员时返回 ``None`` —— 路由按 404 处理，不泄露任何细节
        （连"有人正在用"都不告诉他）。
        """
        with self._lock:
            sess = self._session
            if sess is not None and not self._is_owner(sess, admin_id, token_hash):
                return None
        return self.state_view()

    def state_view(self) -> dict:
        """给页面用的状态快照。**绝不含任何凭据值**（只有指纹与时间）。"""
        with self._lock:
            sess = self._session
        if sess is None:
            return self.idle_view()
        now = self._now()
        base = self.idle_view()
        base.update({
            "active": sess.state in ACTIVE_LOGIN_STATES,
            "state": sess.state,
            "label": login_state_label(sess.state),
            "message": sess.message,
            "reason": sess.reason,
            "url": sess.url,
            "viewport": {"width": sess.viewport[0], "height": sess.viewport[1]},
            "frame_seq": sess.frame_seq,
            "precheck_status": sess.precheck_status,
            "precheck_message": sess.precheck_message,
            "captured_at": sess.captured_at,
            "fingerprint": sess.fingerprint,
            "captured": sess.state == LOGIN_CAPTURED,
            "verifying": sess.state == LOGIN_VERIFYING,
            "verify_attempts": sess.verify_attempts,
            # 终态标记：页面据此**必然**停止刷新，并显示上面的 message/reason_label。
            "terminal": sess.state in (LOGIN_CAPTURED, LOGIN_FAILED),
            "reason_label": reason_label(sess.reason),
            "age_s": round(max(0.0, now - sess.started_at), 1),
            "idle_left_s": round(
                max(0.0, float(self.settings.auth_remote_idle_timeout_s)
                    - (now - sess.last_input_at)), 1),
        })
        return base

    def idle_view(self) -> dict:
        """"当前没有你的会话"的形状。**不读任何会话** —— 别人的会话绝不经过这里。

        页面在"有会话但不属于你"时用它渲染：连"谁在用它"都不透露。
        """
        return {
            "active": False,
            "state": LOGIN_IDLE,
            "label": login_state_label(LOGIN_IDLE),
            "message": "",
            "reason": "",
            "url": "",
            "viewport": {"width": int(self.settings.auth_remote_viewport_w),
                         "height": int(self.settings.auth_remote_viewport_h)},
            "frame_seq": 0,
            "precheck_status": "",
            "precheck_message": "",
            "captured_at": "",
            "fingerprint": "",
            "captured": False,
            "verifying": False,
            "verify_attempts": 0,
            "terminal": False,
            "reason_label": "",
            "age_s": 0.0,
            "idle_timeout_s": float(self.settings.auth_remote_idle_timeout_s),
            "total_timeout_s": float(self.settings.auth_login_timeout_s),
            "idle_left_s": float(self.settings.auth_remote_idle_timeout_s),
            "enabled": bool(self.settings.auth_remote_enabled),
            "channel": self.settings.auth_remote_channel or "bundled-chromium",
            "installed": bool(browser_availability()[0]),
        }

    def frame_for(self, admin_id: Any, token_hash: str) -> bytes | None:
        with self._lock:
            sess = self._session
            if sess is None or not self._is_owner(sess, admin_id, token_hash):
                return None
        return sess.get_frame()

    @staticmethod
    def _is_owner(sess: _Session, admin_id: Any, token_hash: str) -> bool:
        return sess.admin_id == admin_id and sess.token_hash == token_hash

    # ------------------------------------------------------------ 生命周期

    def start(self, *, admin_id: Any, token_hash: str, secure: bool = True) -> dict:
        """开始一次远程会话。返回 ``{"ok": bool, "message": str}``，**不抛异常**。

        "检查并占用"在**同一把锁**内完成：两个并发 ``/start`` 不可能都开成功。
        """
        if not self.settings.auth_remote_enabled:
            return {"ok": False, "message": "远程浏览器已禁用（HAUT_AUTH_REMOTE_ENABLED=false）。"}
        if not secure and not self.settings.auth_remote_allow_insecure:
            return {
                "ok": False,
                "message": "明文 HTTP 下不允许启动远程浏览器（会把学校口令暴露在链路上）。"
                           "请改用 https，或先用 ssh -L 8080:127.0.0.1:8080 隧道；"
                           "确要在明文下使用，设 HAUT_AUTH_REMOTE_ALLOW_INSECURE=true。",
            }
        available, reason = browser_availability()
        if not available:
            return {"ok": False, "message": f"浏览器自动化不可用：{reason}"}

        with self._lock:
            self._gc_locked()
            busy = self._busy_locked()
            if busy:
                return {"ok": False, "message": busy}
            # 单飞：CLI 或上一次采集在进行中就不开第二个窗口（同一套状态机）。
            other = active_login_state(self.settings, self.db, now=self._wall, logger=self.logger)
            if other is not None:
                return {
                    "ok": False,
                    "message": f"已有一个登录流程在进行中（{login_state_label(other)}）；"
                               "请先完成它或等它超时。",
                }
            sess = _Session(
                admin_id=admin_id, token_hash=token_hash,
                viewport=(int(self.settings.auth_remote_viewport_w),
                          int(self.settings.auth_remote_viewport_h)),
            )
            # 用管理器自己的时钟起算（测试注入假时钟时这一点是必须的）。
            sess.started_at = sess.last_input_at = self._now()
            self._session = sess
            sess.thread = threading.Thread(
                target=self._run, args=(sess,), name="haut-remote-browser", daemon=True
            )
            thread = sess.thread

        record_login_state(
            self.db, login_state=LOGIN_LAUNCHING,
            message="正在启动服务器端浏览器…", logger=self.logger,
        )
        if not secure:
            # 只记"发生了这件事"，绝不记任何值。
            self.logger.warning("远程浏览器在明文 HTTP 上启动；链路可被旁路读取（见页面告警）")
        thread.start()
        return {
            "ok": True,
            "message": "已开始启动服务器端浏览器；画面出现后，请在其中完成登录"
                       "（含图形验证码，由你本人识别）。",
        }

    def submit_input(self, events: list[dict], *, admin_id: Any, token_hash: str) -> dict:
        """把键鼠事件塞进队列。**绝不记录事件内容**（那就是键盘记录）。

        队列满 → 丢弃剩余事件并立刻返回，绝不在请求线程里等待工作线程。
        """
        with self._lock:
            sess = self._session
            if sess is None or not self._is_owner(sess, admin_id, token_hash):
                return {"ok": False, "owner": False, "queued": 0, "state": LOGIN_IDLE}
            state = sess.state
        queued = 0
        for event in events[:MAX_EVENTS]:
            try:
                sess.inputs.put_nowait(event)
                queued += 1
            except queue.Full:
                break
        if queued:
            sess.last_input_at = self._now()
            sess.wake.set()
        return {"ok": True, "owner": True, "queued": queued, "state": state}

    def stop(
        self, *, admin_id: Any = None, token_hash: str | None = None,
        reason: str = "", join_timeout_s: float = STOP_JOIN_S,
    ) -> dict:
        """结束会话。给了 ``token_hash`` 就只有属主能停。

        只"发信号 + 有界 join"：工作线程可能正卡在浏览器调用上，绝不让它把请求
        线程（或 uvicorn 的关闭流程）无限拖住。
        """
        with self._lock:
            sess = self._session
            if sess is None:
                return {"ok": False, "message": "当前没有远程浏览器会话。"}
            if token_hash is not None and not self._is_owner(sess, admin_id, token_hash):
                return {"ok": False, "message": "该远程浏览器会话不属于当前登录的管理员。"}
            was_active = sess.state in ACTIVE_LOGIN_STATES
            if reason and not sess.reason:
                # 原因码与原因文字分开：页面用原因码给出「已取消」这类明确标签，
                # 文字保留调用方（路由）写的上下文。
                sess.reason = "cancelled"
                sess.message = f"{reason}；浏览器正在关闭。"
            sess.stop_event.set()
            sess.wake.set()
            thread = sess.thread

        if thread is not None and thread.is_alive():
            thread.join(join_timeout_s)

        still_alive = thread is not None and thread.is_alive()
        if was_active and still_alive:
            # 工作线程没能在时限内收尾（卡在浏览器调用上）：这里补一条如实的落库，
            # 免得 DB 里永远停在"等待登录"（那会让单飞锁看起来一直被占着）。
            # 正常路径下终态由工作线程自己写，这里不重复写。
            with self._lock:
                sess.state = LOGIN_FAILED
                sess.finished_at = self._now()
            record_login_state(
                self.db, login_state=LOGIN_FAILED,
                message=f"远程浏览器会话已结束（{reason or '手动停止'}）。", logger=self.logger,
            )

        with self._lock:
            if self._session is sess and not still_alive:
                # 主动停止就是主动放弃所有权（终态也一并收走）；
                # 自然结束的会话会保留一小会儿，让页面读到最终结果。
                self._session = None
        return {"ok": True, "message": "已结束远程浏览器会话。"}

    def shutdown(self, *, timeout_s: float = SHUTDOWN_JOIN_S) -> None:
        """进程收尾：关浏览器、停线程。**有界**，绝不阻塞优雅关闭。"""
        with self._lock:
            sess = self._session
        if sess is None:
            return
        sess.stop_event.set()
        sess.wake.set()
        thread = sess.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout_s)
            if thread.is_alive():
                # 绝不死等：systemd TimeoutStopSec=30，等不下去就如实记一笔。
                # 线程是 daemon，随进程退出；KillMode=control-group 会连浏览器一起收。
                self.logger.warning(
                    "远程浏览器工作线程未能在 %.0fs 内退出，随进程结束", timeout_s
                )

    def _gc_locked(self) -> None:
        """丢掉留太久的终态会话（页面早就不看它了）。"""
        sess = self._session
        if sess is None or sess.state in ACTIVE_LOGIN_STATES:
            return
        if sess.thread is not None and sess.thread.is_alive():
            return
        if sess.finished_at is not None and self._now() - sess.finished_at < _TERMINAL_KEEP_S:
            return
        self._session = None

    def _busy_locked(self) -> str | None:
        """不能新开会话的原因；``None`` 表示可以开。"""
        sess = self._session
        if sess is None:
            return None
        if sess.state in ACTIVE_LOGIN_STATES:
            return "已有一个远程浏览器会话在进行中；请先结束它再开新的。"
        if sess.thread is not None and sess.thread.is_alive():
            return "上一个远程浏览器会话正在关闭；请稍候再试。"
        return None

    # ------------------------------------------------------------ 工作线程

    def _run(self, sess: _Session) -> None:
        """工作线程主体：**这里（且只有这里）能碰 Playwright。**"""
        browser: InteractiveSession | None = None
        try:
            browser = self._backend.open(
                channel=self.settings.auth_remote_channel,
                headless=True,
                viewport=sess.viewport,
                login_url=self.settings.base_url,
                timeout_s=self.settings.auth_login_timeout_s,
            )
            sess.state = LOGIN_AWAITING
            sess.message = _AWAITING_MESSAGE
            record_login_state(
                self.db, login_state=LOGIN_AWAITING, message=_AWAITING_MESSAGE, logger=self.logger,
            )
            capture = self._pump(sess, browser)
            # 无需再做什么：``_pump`` 内部已完成「取到令牌 → 业务接口验证 → 通过才落盘」
            # （``_verify_and_commit`` / ``_commit_captured``）。返回非 None 只表示
            # **已经验证通过并保存**，绝不只是"看到了 Cookie"。
            _ = capture
        except BrowserNotInstalledError as exc:
            self._fail(sess, "unavailable", f"浏览器自动化不可用：{exc}")
        except BrowserLaunchError as exc:
            self._fail(sess, "launch", str(exc))
        except Exception as exc:
            # 绝不让异常文本带着可能的值进页面：只报类型。
            self.logger.warning("远程浏览器会话异常结束：%s", type(exc).__name__)
            self._fail(sess, "error", f"远程浏览器异常结束（{type(exc).__name__}）。请重新开始。")
        finally:
            if browser is not None:
                browser.close()
            # 队列里可能还留着没转发的按键 —— 会话结束就清干净，不留残留。
            try:
                while True:
                    sess.inputs.get_nowait()
            except queue.Empty:
                pass
            with self._lock:
                if sess.finished_at is None:
                    sess.finished_at = self._now()
            if self.db is not None:
                # 每线程一个连接（web/db.py:407-433）：不收掉就会一直挂着一个
                # WAL 连接，直到进程结束。
                try:
                    self.db.close_current_thread()
                except Exception:
                    pass

    def _pump(self, sess: _Session, browser: InteractiveSession) -> LoginCapture | None:
        """主循环：转发输入 → 按节拍截图 → 心跳 → 找 Admin-Token。

        返回 ``LoginCapture`` 表示登录成功；返回 ``None`` 表示已结束（``sess`` 里
        带着如实的 ``reason``/``message``）。
        """
        interval_s = max(0.05, int(self.settings.auth_remote_frame_interval_ms) / 1000.0)
        quality = int(self.settings.auth_remote_jpeg_quality)
        total_limit = float(self.settings.auth_login_timeout_s)
        idle_limit = float(self.settings.auth_remote_idle_timeout_s)
        next_frame_at = 0.0        # 0 ⇒ 第一圈立刻截一帧
        next_hb_at = self._now() + _HEARTBEAT_S
        sess.url = sanitize_url(browser.current_url())

        while not sess.stop_event.is_set():
            now = self._now()

            if now - sess.started_at > total_limit:
                return self._end(
                    sess, "timed_out",
                    f"等待登录超过 {total_limit:g}s 仍未完成，已自动关闭浏览器。"
                    "请重新开始（图形验证码需要你本人识别）。" + self._verify_note(sess),
                )
            if now - sess.last_input_at > idle_limit:
                return self._end(
                    sess, "timed_out",
                    f"超过 {idle_limit:g}s 没有任何操作，已自动关闭浏览器并释放会话。"
                    + self._verify_note(sess),
                )

            # 1) 输入优先：画面要反映刚发生的事。
            drained: list[dict] = []
            while True:
                try:
                    drained.append(sess.inputs.get_nowait())
                except queue.Empty:
                    break
            if drained:
                reason = self._apply(browser, drained, sess.viewport)
                if reason == "closed":
                    return self._end(
                        sess, "closed", "浏览器已被关闭，且未获取到 Admin-Token（登录可能没有完成）。"
                    )
                # 输入之后**不额外**截图：画面刷新节拍就是 interval_s，
                # 更密只会把 CPU 烧在截图上而页面根本来不及显示（每帧约 50ms）。

            # 2) 画面：严格按节拍，不做"字节变化去重"（截图本身才是成本，去重只省拷贝）。
            if now >= next_frame_at:
                try:
                    data = browser.screenshot(quality=quality)
                except Exception as exc:
                    if browser.is_closed():
                        return self._end(
                            sess, "closed",
                            "浏览器已被关闭，且未获取到 Admin-Token（登录可能没有完成）。",
                        )
                    self.logger.warning("截图失败：%s", type(exc).__name__)
                    data = None
                if data:
                    sess.put_frame(data)
                next_frame_at = now + interval_s

            # 3) 心跳：刷新 login_updated_at，否则会话超过 timeout+60s 会被当成残留。
            if now >= next_hb_at:
                record_login_state(
                    self.db, login_state=LOGIN_AWAITING,
                    message=sess.message or _AWAITING_MESSAGE, logger=self.logger,
                )
                next_hb_at = now + _HEARTBEAT_S

            # 4) 令牌出现了吗？
            #
            #    **绝不**把「Cookie 非空」当成登录成功（那正是本功能一开始的错）：
            #    令牌出现只说明可以进入"验证凭据"这一步，必须再用**业务接口**
            #    （GET /stage-api/getInfo，实测能区分「被接受」与「未被接受」）
            #    确认它真的可用，通过之后才写快照。
            token = browser.cookie(COOKIE_NAME)
            if token:
                capture = LoginCapture(
                    cookie=f"{COOKIE_NAME}={token}",
                    authorization=f"Bearer {token}",
                    captured_at=format_time(self._wall()),
                    source=DEFAULT_SNAPSHOT_SOURCE,
                )
                done = self._verify_and_commit(sess, capture)
                if done is not None:
                    return done
                if sess.state not in ACTIVE_LOGIN_STATES:
                    # 验证环节已经给出终态（未通过 / 写入失败）：会话到此为止，
                    # 页面会读到明确的 failed + reason_label，不会一直"加载中"。
                    return None

            sess.url = sanitize_url(browser.current_url())
            if browser.is_closed():
                return self._end(
                    sess, "closed", "浏览器已被关闭，且未获取到 Admin-Token（登录可能没有完成）。"
                )

            sess.wake.wait(_POLL_S)
            sess.wake.clear()

        # 循环只可能因为 stop_event（``stop()`` / ``shutdown()``）而正常退出。
        if sess.stop_event.is_set():
            return self._end(
                sess, "cancelled",
                sess.message or "远程浏览器会话已取消（浏览器已关闭）。",
            )
        return self._end(sess, "stopped", "远程浏览器会话已结束。")

    def _apply(self, browser: InteractiveSession, events: list[dict], viewport) -> str | None:
        """转发一批事件。单条失败不杀会话（页面跳转时很常见），只记类型。"""
        width = float(viewport[0]) - 1.0
        height = float(viewport[1]) - 1.0
        for event in events:
            try:
                kind = event.get("t")
                if kind == "m":
                    browser.mouse_move(_num(event.get("x"), hi=width),
                                       _num(event.get("y"), hi=height))
                elif kind == "c":
                    button = event.get("b")
                    browser.click(
                        _num(event.get("x"), hi=width), _num(event.get("y"), hi=height),
                        button=(button if button in _MOUSE_BUTTONS else "left"),
                        click_count=int(_num(event.get("n", 1), hi=3) or 1),
                    )
                elif kind == "w":
                    browser.wheel(float(_num(event.get("dx", 0), lo=-4000.0, hi=4000.0)),
                                  float(_num(event.get("dy", 0), lo=-4000.0, hi=4000.0)))
                elif kind == "k":
                    spec = key_spec(event)
                    if spec is None:
                        continue
                    mode, value = spec
                    if mode == "press":
                        browser.press(value)
                    else:
                        browser.insert_text(value)
                elif kind == "x":
                    text = event.get("s")
                    if isinstance(text, str) and text:
                        browser.insert_text(text[:MAX_TEXT])
            except Exception:
                if browser.is_closed():
                    return "closed"
                continue
        return None

    def _end(self, sess: _Session, reason: str, message: str) -> None:
        """给出终态。已存在的原因码优先（例如用户在面板上点了「结束会话」= cancelled）。"""
        sess.reason = sess.reason or reason
        sess.message = message
        sess.state = LOGIN_FAILED
        sess.finished_at = self._now()
        record_login_state(
            self.db, login_state=LOGIN_FAILED, message=message, logger=self.logger,
        )
        return None

    def _fail(self, sess: _Session, reason: str, message: str) -> None:
        sess.reason = sess.reason or reason
        sess.message = message
        sess.state = LOGIN_FAILED
        sess.finished_at = self._now()
        record_login_state(
            self.db, login_state=LOGIN_FAILED, message=message, logger=self.logger,
        )

    # ------------------------------------------------------------ 凭据链
    #
    # 顺序是**先验证、后落盘**（与 CLI 的 auth-login 同一条链，但把验证提到写盘之前）：
    # 只有业务接口确认这份令牌可用，才允许它覆盖快照。理由很直接 —— 写盘在验证之前的话，
    # 一次"看起来登录成功、其实令牌没用"的采集会把上一份**能用**的快照冲掉。

    def _verify_and_commit(self, sess: _Session, capture: LoginCapture) -> LoginCapture | None:
        """令牌出现后的处理：进"验证凭据"状态 → 用业务接口验证 → 通过才落盘。

        返回 ``LoginCapture`` 表示验证通过并已保存；返回 ``None`` 表示"继续等"或
        "已在验证环节结束"（后者靠 ``sess.state`` 判定，调用方据此退出循环）。
        """
        fingerprint = capture.fingerprint()
        if fingerprint != sess.verify_fingerprint:
            # 令牌换了（用户重新登录了一次）：重新计次、立刻验证。
            sess.verify_fingerprint = fingerprint
            sess.verify_attempts = 0
            sess.next_verify_at = 0.0
            sess.precheck_status = ""
            sess.precheck_message = ""

        if sess.state != LOGIN_VERIFYING:
            sess.state = LOGIN_VERIFYING
            sess.message = _VERIFYING_MESSAGE
            record_login_state(
                self.db, login_state=LOGIN_VERIFYING, message=_VERIFYING_MESSAGE,
                logger=self.logger,
            )

        now = self._now()
        if now < sess.next_verify_at:
            return None  # 还没到下一次验证的节拍：继续转发画面，不重复打接口
        sess.next_verify_at = now + _VERIFY_RETRY_S

        try:
            status, message = self._precheck(capture)
        except Exception as exc:
            # 验证器自己崩了也不能把会话判成"凭据有效"，更不能把异常文本（可能含值）
            # 带进页面：只报类型，按"无法确认"处理。
            status, message = "unknown", f"凭据验证未能完成：{type(exc).__name__}"
        # 只记结论，绝不记任何凭据值。
        self.logger.info("远程浏览器凭据验证：%s", status)

        if status == "valid":
            return self._commit_captured(sess, capture, verify_message=message)

        sess.verify_attempts += 1
        sess.precheck_status = status
        sess.precheck_message = message
        if sess.verify_attempts >= _VERIFY_MAX_ATTEMPTS:
            return self._end(
                sess, "unverified",
                f"已从学校页面取到令牌，但业务接口仍未接受它（验证 {sess.verify_attempts} 次）："
                f"{message}。**没有写入任何快照**，原有凭据保持不变。"
                "常见原因：登录其实没完成（验证码/口令错误）、令牌属于别的子系统，"
                "或服务器出口被站点拒绝。请重新在画面里登录，或改用命令行 "
                "python -m haut_seat auth-login 观察真实登录过程。",
            )

        sess.state = LOGIN_AWAITING  # 回到"等待你登录"，用户可以继续在画面里操作
        sess.message = (
            f"已取到令牌，但业务接口尚未接受它（第 {sess.verify_attempts}/"
            f"{_VERIFY_MAX_ATTEMPTS} 次验证）：{message}。未写入快照，继续等待登录完成。"
        )
        record_login_state(
            self.db, login_state=LOGIN_AWAITING, message=sess.message, logger=self.logger,
        )
        return None

    def _verify_note(self, sess: _Session) -> str:
        """超时/结束时补一句"最后一次验证说了什么"，否则用户只看到"超时"无从下手。"""
        if not sess.precheck_message:
            return ""
        return (
            f"（最后一次验证结论：{sess.precheck_status or '未知'} —— {sess.precheck_message}）"
        )

    def _commit_captured(
        self, sess: _Session, capture: LoginCapture, *, verify_message: str = ""
    ) -> LoginCapture | None:
        """验证通过后的落盘链：save_snapshot → 记状态 → 记来源。写盘失败返回 ``None``。"""
        try:
            save_snapshot(
                self.settings, cookie=capture.cookie, authorization=capture.authorization,
                logger=self.logger,
            )
        except Exception as exc:
            # 登录是人工成果，写盘失败必须如实说出来（但绝不显示任何值）。
            self._fail(sess, "snapshot_write",
                       f"凭据已通过业务接口验证但写入快照失败：{type(exc).__name__}（值未显示）")
            return None

        sess.captured_at = capture.captured_at
        sess.fingerprint = capture.fingerprint()
        sess.state = LOGIN_CAPTURED
        sess.message = "凭据已通过业务接口验证并保存为快照。"
        sess.precheck_status = "valid"
        sess.precheck_message = verify_message or "业务接口返回正常数据，凭据可用"
        record_login_state(
            self.db, login_state=LOGIN_CAPTURED, message=sess.message, logger=self.logger,
        )
        record_credential_source(
            self.db, source="snapshot", snapshot_present=True,
            captured_at=capture.captured_at, fingerprint=sess.fingerprint, logger=self.logger,
        )
        sess.finished_at = self._now()
        return capture

    def _readonly_precheck(self, capture: LoginCapture) -> tuple[str, str]:
        """只读预检：临时 Settings 强制 env 模式 + 刚采到的值。

        与 ``__main__._verify_captured`` 同一范式（``model_copy`` 不落盘、不污染
        进程配置），于是预检打的就是这份**新**凭据，而不是可能更旧的快照。
        这是一次**真实但只读**的请求 —— 绝不提交任何预约。
        """
        try:
            from pydantic import SecretStr

            from haut_seat.services.io import auth_precheck

            probe = self.settings.model_copy(update={
                "auth_source": "env",
                "cookie": SecretStr(capture.cookie),
                "authorization": SecretStr(capture.authorization),
            })
            check = auth_precheck(probe, self.settings.regionid)
            return check.status.value, check.message
        except Exception as exc:
            return "unknown", f"只读复检未能完成：{type(exc).__name__}"