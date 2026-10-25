"""应用配置：从环境变量 / .env 读取并校验。

认证信息（Cookie/Token/用户名/密码）使用 SecretStr 占位，绝不写入日志、代码或版本库。

凭据来源由 ``auth_source`` 决定：``env`` 只用 .env（与从前逐字节一致）、
``browser`` 只用浏览器会话快照、``auto`` 取较新的一方。快照由 ``auth-login``
子命令在**人工完成登录（含图形验证码）**后采集，本项目不做口令自动登录、
不识别验证码，也绝不把凭据值写进日志/页面/异常。

**多用户（用户端）的凭据是另一条独立的路**：每个平台用户在「绑定图书馆账号」时
用**自己的**学校统一身份认证账号完成一次官方登录，服务端把返回的令牌经
``services.credential_crypto`` 加密后**按 user_id 分开存**；学校口令只在那一次请求
的内存里存在，绝不落盘、绝不入日志。``.env`` 里的 ``HAUT_COOKIE`` /
``HAUT_AUTHORIZATION`` 与用户端是两套互不影响的来源（前者服务管理端自己的计划）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HAUT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # ``auth_cred_key`` 用了 ``validation_alias``（见下）。pydantic 一旦看到别名，
        # 就不再按字段名填充，于是 ``Settings(auth_cred_key=...)`` 会**静默变成 None**。
        # 打开这个开关，按字段名传参（测试、``dataclasses.replace``）才继续有效。
        populate_by_name=True,
    )

    # 服务端
    base_url: str = "https://wslib.haut.edu.cn"

    # HTTP
    request_timeout_s: float = 10.0
    max_retry: int = 2
    min_interval_s: float = 1.0

    # 安全开关
    enable_real_booking: bool = False
    pause_file: str = "data/pause.flag"

    # 跨进程互斥锁的**基名**：CLI 的 book 与 Web 后台调度共用同一套锁名，
    # 避免「cron 里的 CLI」和「Web 调度器」同时提交同一时段的预约。
    # 实际锁文件按「区域 + 时段」派生：data/book.lock → data/book-<摘要>.lock
    # （见 services/slot_lock）。因此同一个时段仍然只允许一个进程提交，
    # 而**不同时段可以真正并行**；时段解析不出时回退到这个基名本身（全进程串行）。
    book_lock_file: str = "data/book.lock"
    # 时段锁的等待上限（秒）：等不到就放弃本轮，由下一次调度重试。**绝不无限等待**。
    book_lock_wait_s: float = 5.0
    # 残留锁的判定时长（秒）：必须大于一次预约运行的最长合理持锁时间
    # （运行器会在整个窗口内重试），否则第二个进程会在第一个进程还在提交时把锁抢走。
    book_lock_stale_s: float = 3600.0

    # 座位配置
    seats_config: str = "config/seats.json"

    # 预约任务
    booking_config: str = "config/booking.json"
    booking_channel: str = "1001"  # addbooking 的 channel 参数；已观察到的示例值，未确认适用所有场景
    # 提交被服务器**明确拒绝为「尚未开放」**时的消息关键词（``HAUT_NOT_OPEN_KEYWORDS``，
    # 逗号分隔）。命中时该次提交**不算进 max_attempts、也不结束这次运行**，而是退回
    # 只读查询、按间隔重试，直到开放或窗口关闭为止。
    #
    # **默认空 = 该机制关闭，一个词都不内置。** 我们至今没有观察到服务器「未开放」
    # 响应的真实文案（日志与运行事件里都没有出现过），凭空猜词会把一次**永久性失败**
    # 误判成「稍后再试」。首次遇到时的真实文案会原样出现在运行事件里
    # （形如「重试次数耗尽(3次)：<服务器 msg>」），把其中稳定出现的片段填进来即可，
    # 例如 HAUT_NOT_OPEN_KEYWORDS=尚未开放,未开放。
    #
    # 注意：可能的「开放前不得提交」还有一层与文案无关的硬保护 —— 窗口当天 07:00
    # 之前运行器根本不提交（见 core.schedule.booking_opens_at）。
    not_open_keywords: str = ""

    # 日志
    log_level: str = "INFO"
    log_file: str = "logs/haut_seat.log"

    # 查询默认参数（以浏览器正常查询为参考，非敏感；可被 CLI 参数覆盖）
    regionid: int = 16
    page_size: int = 10

    # 非敏感请求头（凭据类请求头仍由 cookie / authorization 单独处理，绝不在此配置）
    http_accept: str = "application/json, text/plain, */*"
    http_accept_language: str = "zh-CN,zh;q=0.9"
    http_referer: str = ""            # 默认不发送 Referer（浏览器页面地址未知，留空即不发送）
    http_user_agent: str = "haut-seat-helper/0.1 (personal read-only)"
    # 连接池上限。进程内按「base_url + 凭据指纹」复用同一个 httpx.Client，
    # 因此这里的上限是**每个客户端**的并发连接数 —— 有界，不会因为计划多而膨胀。
    http_max_connections: int = 8
    http_max_keepalive_connections: int = 4
    # 可复用 HTTP 客户端的**缓存条数**上限（LRU）。多用户场景下每个用户一份凭据
    # 就是一份客户端，默认值必须大于「同时在跑的用户数」，否则每个 tick 都在
    # 反复建/拆连接（那正是连接复用要消除的成本）。仍然是**有界**的。
    http_client_cache_max: int = 8

    # ---- Web 管理面板（可选功能；默认只监听回环地址，必须由反向代理/TLS 对外暴露）----
    web_host: str = "127.0.0.1"        # 安全默认：不要直接绑定 0.0.0.0 裸奔到公网
    web_port: int = 8080
    web_db: str = "data/web.db"
    web_session_secure: bool = False   # 通过 HTTPS 访问时置 true（Cookie 加 Secure）
    web_trust_proxy: bool = False      # 反向代理后面再开启（读取 X-Forwarded-*）
    web_allow_first_run_setup: bool = True  # 无管理员时可网页创建首个管理员（见 README 安全说明）
    web_enable_scheduler: bool = True
    # 用户端（/booking）：极简用户端，**每个用户各自的账号 + 各自的学校凭据**。
    # 用户端账号是平台自己的账号（用户名 + 平台口令），与学校统一身份认证账号
    # 是两回事：学校账号只在「绑定图书馆账号」时使用一次，学校口令任何时候都不落盘。
    web_enable_user_end: bool = True
    # 自助注册（用户名 + 平台口令 + 邀请码）。``web_signup_enabled`` 是总开关；
    # 邀请码由管理端生成/查看（存 meta），也可以在 .env 里预设一个：
    # ``web_signup_invite_code`` 非空时作为**默认**值，管理端页面里改过之后以数据库为准。
    # 两者都没有时注册不可用 —— 页面如实说明「管理员尚未生成邀请码」，绝不悄悄放行。
    web_signup_enabled: bool = True
    web_signup_invite_code: str = ""
    user_session_ttl_hours: int = 72          # 用户端会话绝对有效期
    user_session_idle_minutes: int = 240      # 用户端会话空闲超时
    # 绑定图书馆账号时取到的验证码标识（uuid）的有效期：超过就要求重新取一张
    # （学校那侧本来也有自己的过期时间，我们只是不假装它永远有效）。
    user_captcha_ttl_s: float = 300.0
    scheduler_tick_s: float = 5.0
    scheduler_max_concurrent: int = 3
    scheduler_run_log_keep_days: int = 30
    # 提前启动调度线程的秒数（HAUT_SCHEDULER_TRIGGER_LEAD_S）：>0 时线程会在触发
    # 时刻之前就起来，只为让登录预检提前完成；查询与提交仍只能发生在触发时刻之后
    # （由 booking_runner 的等待负责）。默认 0 = 关闭，行为与从前逐字节一致。
    # 提前量期间会一直占着**该时段**的锁（跨进程互斥，见 services/slot_lock），
    # 因此上限 600 秒：占的是自己的时段，不再牵连其它时段的任务。
    scheduler_trigger_lead_s: float = 0.0

    # ---- 凭据来源（浏览器会话快照 / .env 手动配置）----
    # auto（默认）：快照与 .env 里**较新**的一方胜出（用 .env 的 mtime 作真实信号）
    # browser      ：只用快照；快照缺失或不可用时**明确报错并阻断提交**，绝不回退 .env
    # env          ：只用 .env，与从前逐字节一致（不读快照文件）
    auth_source: Literal["auto", "browser", "env"] = "auto"
    auth_dir: str = "data/auth"
    auth_snapshot_file: str = "data/auth/credentials.json"
    auth_profile_dir: str = "data/auth/browser-profile"
    # 复用系统已装的 Edge（channel=msedge），**绝不执行 playwright install 下载浏览器**
    auth_browser_channel: str = "msedge"
    # 等待人工完成登录（含图形验证码）的上限
    auth_login_timeout_s: float = 300.0
    # 只读预检结果的复用窗口：0（默认）= 完全关闭，行为与从前逐字节一致。
    # >0 时，同一凭据（按指纹比对）在窗口内重复预检会直接复用上次的 valid 结论，
    # 避免触发时刻前后对同一凭据反复做昂贵的探测；只复用 valid，
    # invalid / indeterminate 一律重新探测（安全闸门不得因缓存变松）。
    auth_check_ttl_s: float = 0.0

    # ---- 远程浏览器（在面板里直接操作服务器上的浏览器）----
    # 解决的是"服务器没有桌面"这个死结：``auth-login`` 用的是 ``headless=False``
    # （那里是真需求 —— 人工必须**看到**登录页），部署到无桌面服务器后必然失败。
    # 这里用无头 Chromium + 画面/键鼠转发补上画面，人工环节照旧（验证码仍由你本人
    # 识别，程序不代填任何值、不识别验证码、不绕过访问控制）。
    auth_remote_enabled: bool = True
    # 空 = 用 playwright 自带的 Chromium。**不重载** auth_browser_channel：
    # 那个默认 "msedge" 是给桌面 CLI 用的，Linux 服务器上没有 Edge。
    auth_remote_channel: str = ""
    auth_remote_viewport_w: int = 1280
    auth_remote_viewport_h: int = 800
    # 画面刷新间隔。每帧实测约 50ms（1280x800 / JPEG q70 ≈ 10.9KB），
    # 600ms 约合单核 8%；调小更跟手、更费 CPU。
    auth_remote_frame_interval_ms: int = 600
    auth_remote_jpeg_quality: int = 70
    # 多久没有键鼠输入就自动关掉浏览器（释放单飞锁，不留孤儿进程）
    auth_remote_idle_timeout_s: float = 900.0
    # 明文 HTTP 下是否允许启动远程会话。默认允许（"先用起来"），但页面会给出
    # 醒目的明文告警：链路上会有管理会话 Cookie、登录页画面与学校口令的按键。
    # 设为 false 即"仅 HTTPS"（配合反代 TLS 或 ssh -L 隧道）。
    auth_remote_allow_insecure: bool = True

    # 每个用户的学校凭据在库里**加密**保存（services.credential_crypto）。主密钥
    # 只在这两处：``HAUT_AUTH_CRED_KEY``（环境变量）或下面这个文件（不存在时自动生成
    # 32 字节随机数并尽力收紧权限）。密钥**不入库**：只有 data/web.db 落到别人手里
    # 时，密文仍然解不开。
    #
    # 环境变量名为什么写成别名：字段叫 ``auth_cred_key``，按本文件的命名推导出来是
    # ``HAUT_AUTH_CRED_KEY``（与邻居 ``auth_master_key_file`` → ``HAUT_AUTH_MASTER_KEY_FILE``
    # 同一族，也就是文档里写的那个名字）；``HAUT_CRED_KEY`` 是早期文档用过的简写，
    # 一并接受 —— 两个名字都认得，写哪个都不会被**静默忽略**（那正是要防的事：
    # 操作者以为配了主密钥，其实悄悄回退到文件里那把）。两个都设时**以
    # ``HAUT_AUTH_CRED_KEY`` 为准**（别名列表按序取第一个命中的）。
    auth_cred_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("HAUT_AUTH_CRED_KEY", "HAUT_CRED_KEY"),
    )
    auth_master_key_file: str = "data/auth/master.key"

    # 邮件通知（本轮仅预留；口令只从环境变量读取，不入库、不入日志）
    smtp_password: SecretStr | None = None   # 预留：默认由 HAUT_SMTP_PASSWORD 提供

    # 会话凭据（复用用户正常登录后的会话；来自 .env，绝不写入日志/代码）
    cookie: SecretStr | None = None          # 作为 Cookie 请求头发送
    authorization: SecretStr | None = None   # 作为 Authorization 请求头发送
    # 以下为预留占位，暂未使用
    token: SecretStr | None = None
    username: SecretStr | None = None
    password: SecretStr | None = None

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, v: str) -> str:
        v = (v or "").strip().rstrip("/")
        if not v.startswith(("http://", "https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        return v

    @field_validator("request_timeout_s", "min_interval_s", "scheduler_tick_s")
    @classmethod
    def _check_positive_float(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("必须为正数")
        return v

    @field_validator("scheduler_trigger_lead_s")
    @classmethod
    def _check_trigger_lead_s(cls, v: float) -> float:
        # 0 是合法的（=关闭）。上限存在的理由不是精度，而是锁：提前启动期间
        # 调度器一直持有**该时段**的锁（见 services/slot_lock），填一个误输入的
        # 大数字会让同一时段的 CLI `book` 在触发时刻之前长时间拿不到锁。
        if v < 0:
            raise ValueError("不能为负数")
        if v > 600:
            raise ValueError("最多 600 秒（提前启动期间会一直占着该时段的预约文件锁）")
        return v

    @field_validator("book_lock_wait_s")
    @classmethod
    def _check_book_lock_wait_s(cls, v: float) -> float:
        # 0 是合法的（=只探测一次，不等）。负数没有意义，直接拒绝。
        if v < 0:
            raise ValueError("不能为负数（0 表示不等待，只探测一次）")
        return v

    @field_validator("book_lock_stale_s")
    @classmethod
    def _check_book_lock_stale_s(cls, v: float) -> float:
        # 必须大于一次预约运行的最长持锁时间，否则第二个进程会把锁抢走（重复提交）。
        # 下限 60 秒：默认运行间隔与窗口粒度都以分钟计，低于这个数没有任何合理场景。
        if v < 60:
            raise ValueError("至少 60 秒（必须大于一次预约运行的最长持锁时间）")
        return v

    @field_validator("auth_login_timeout_s")
    @classmethod
    def _check_auth_login_timeout_s(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("必须为正数")
        return v

    @field_validator("auth_check_ttl_s")
    @classmethod
    def _check_auth_check_ttl_s(cls, v: float) -> float:
        # 0 是合法的（=关闭复用）。负数没有意义，直接拒绝而不是静默当 0。
        if v < 0:
            raise ValueError("不能为负数（0 表示关闭预检结果复用）")
        return v

    @field_validator("auth_remote_viewport_w", "auth_remote_viewport_h")
    @classmethod
    def _check_remote_viewport(cls, v: int) -> int:
        # 视口尺寸就是坐标系的定义域：键鼠事件的坐标按它换算，截图也是这个尺寸。
        # 上下限不是精度问题，而是"填错了要当场报错"，别等到页面上一片空白。
        if v < 320 or v > 3840:
            raise ValueError("必须在 320..3840 之间（远程画面的视口尺寸）")
        return v

    @field_validator("auth_remote_frame_interval_ms")
    @classmethod
    def _check_remote_frame_interval(cls, v: int) -> int:
        # 下限 100ms：每帧约 50ms，比这更密只会把 CPU 烧在截图上；
        # 上限 10s：再慢就不叫"远程操作"了，页面根本没法用。
        if v < 100 or v > 10000:
            raise ValueError("必须在 100..10000 毫秒之间（远程画面的刷新间隔）")
        return v

    @field_validator("auth_remote_jpeg_quality")
    @classmethod
    def _check_remote_jpeg_quality(cls, v: int) -> int:
        if v < 10 or v > 95:
            raise ValueError("必须在 10..95 之间（JPEG 质量）")
        return v

    @field_validator("auth_remote_idle_timeout_s")
    @classmethod
    def _check_remote_idle_timeout(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("必须为正数（0 会让会话永远不自动结束）")
        return v

    @field_validator("regionid", "page_size", "web_port", "scheduler_max_concurrent",
                     "scheduler_run_log_keep_days", "http_max_connections")
    @classmethod
    def _check_positive_int(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("必须为正整数")
        return v

    @field_validator("http_client_cache_max", "user_session_ttl_hours",
                     "user_session_idle_minutes")
    @classmethod
    def _check_positive_int_strict(cls, v: int) -> int:
        # 这三项取 0 都没有合理语义（0 条缓存 = 复用彻底失效；0 分钟会话 = 谁都用不了），
        # 因此直接拒绝，而不是静默当成"关闭某项功能"。
        if v <= 0:
            raise ValueError("必须为正整数")
        return v

    @field_validator("user_captcha_ttl_s")
    @classmethod
    def _check_captcha_ttl(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("必须为正数（验证码标识的有效期）")
        return v

    @field_validator("http_max_keepalive_connections")
    @classmethod
    def _check_keepalive_int(cls, v: int) -> int:
        # 0 是合法的：表示完全不保持长连接（等价于回到"每次请求新建连接"的旧行为）。
        if v < 0:
            raise ValueError("不能为负数（0 表示不保持长连接）")
        return v

    @field_validator("max_retry")
    @classmethod
    def _check_max_retry(cls, v: int) -> int:
        if v < 0:
            raise ValueError("max_retry 不能为负数")
        return v

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, v: str) -> str:
        v = (v or "INFO").upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"非法日志级别: {v}")
        return v


def load_settings(**overrides) -> Settings:
    """加载并校验配置。校验失败抛出 pydantic.ValidationError。"""
    return Settings(**overrides)
