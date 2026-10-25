"""SQLite 访问层：连接管理、建表、轻量迁移。

不使用 ORM（依赖越少越可靠）。要点：
- **每线程一个连接**：调度器在后台线程里跑，不能共享 sqlite3 连接；
- WAL 模式 + ``busy_timeout``，让 Web 请求线程与调度器线程并发读写不互相阻塞；
- 外键打开；写操作统一走 ``Database.tx()`` 事务上下文。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1

# 建表语句。所有时间字段统一存本地时间字符串（YYYY-MM-DD HH:MM:SS），
# 便于人工查看与备份；排序与比较依赖同格式字符串的字典序。
_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS admin_user (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_login_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS login_attempt (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        ip TEXT NOT NULL DEFAULT '',
        ok INTEGER NOT NULL DEFAULT 0,
        at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_login_attempt_at ON login_attempt(at)",
    """
    CREATE TABLE IF NOT EXISTS session (
        token_hash TEXT PRIMARY KEY,
        admin_id INTEGER NOT NULL,
        csrf_token TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        FOREIGN KEY (admin_id) REFERENCES admin_user(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_session_expires ON session(expires_at)",
    """
    CREATE TABLE IF NOT EXISTS plan (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'paused',
        run_date TEXT NOT NULL,
        repeat_rule TEXT NOT NULL DEFAULT 'once',
        trigger_at TEXT NOT NULL DEFAULT '07:00:00',
        book_start TEXT NOT NULL,
        book_end TEXT NOT NULL,
        regionid INTEGER NOT NULL,
        preferred_seats TEXT NOT NULL DEFAULT '[]',
        preferred_prefixes TEXT NOT NULL DEFAULT '[]',
        confirmed_bookable_states TEXT NOT NULL DEFAULT '[]',
        max_attempts_per_seat INTEGER NOT NULL DEFAULT 3,
        note TEXT NOT NULL DEFAULT '',
        pause_requested INTEGER NOT NULL DEFAULT 0,
        stop_requested INTEGER NOT NULL DEFAULT 0,
        unknown_ack_at TEXT,
        last_run_id INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_plan_status ON plan(status, enabled)",
    # 计划的**来源标识**（管理端 / 用户端）。刻意做成**独立的新表**而不是给 plan
    # 加一列：本仓库没有迁移机制，给既有表加列会让老库在 SELECT 时抛
    # "no such column: origin"。新表对老库零风险（CREATE TABLE IF NOT EXISTS），
    # 而没有对应行的老计划一律按 'admin' 解释（它们本来就都是管理端建的）。
    # ON DELETE CASCADE：删计划时来源行一起走，不留孤儿。
    """
    CREATE TABLE IF NOT EXISTS plan_origin (
        plan_id INTEGER PRIMARY KEY,
        origin TEXT NOT NULL DEFAULT 'admin',
        created_at TEXT NOT NULL,
        FOREIGN KEY (plan_id) REFERENCES plan(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id INTEGER NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL,
        category TEXT,
        stop_reason TEXT,
        message TEXT NOT NULL DEFAULT '',
        exit_code INTEGER,
        booking_id INTEGER,
        query_count INTEGER NOT NULL DEFAULT 0,
        submit_count INTEGER NOT NULL DEFAULT 0,
        seat_switches INTEGER NOT NULL DEFAULT 0,
        trigger TEXT NOT NULL DEFAULT 'scheduler',
        FOREIGN KEY (plan_id) REFERENCES plan(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_run_plan ON run(plan_id, started_at)",
    """
    CREATE TABLE IF NOT EXISTS run_event (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,
        at TEXT NOT NULL,
        phase TEXT NOT NULL,
        level TEXT NOT NULL DEFAULT 'info',
        message TEXT NOT NULL DEFAULT '',
        category TEXT,
        FOREIGN KEY (run_id) REFERENCES run(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_run_event_run ON run_event(run_id, id)",
    """
    CREATE TABLE IF NOT EXISTS auth_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        status TEXT NOT NULL DEFAULT 'indeterminate',
        recovery_state TEXT NOT NULL DEFAULT 'idle',
        message TEXT NOT NULL DEFAULT '',
        checked_at TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS auth_event (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        status TEXT NOT NULL,
        message TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'manual'
    )
    """,
    # 凭据来源的**只读元数据**：网页只读这张表，因此页面在结构上
    # 不可能打开快照文件、也就不可能把 Cookie/Token 渲染到前端。
    # fingerprint 是 sha256(令牌)[:12]（单向摘要前缀），不是明文。
    """
    CREATE TABLE IF NOT EXISTS auth_credential_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        source TEXT NOT NULL DEFAULT 'none',
        snapshot_present INTEGER NOT NULL DEFAULT 0,
        snapshot_captured_at TEXT,
        fingerprint TEXT NOT NULL DEFAULT '',
        browser_supported INTEGER NOT NULL DEFAULT 0,
        browser_reason TEXT NOT NULL DEFAULT '',
        login_state TEXT NOT NULL DEFAULT 'idle',
        login_message TEXT NOT NULL DEFAULT '',
        login_updated_at TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notify_setting (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        enabled INTEGER NOT NULL DEFAULT 0,
        channel TEXT NOT NULL DEFAULT 'email',
        smtp_host TEXT NOT NULL DEFAULT '',
        smtp_port INTEGER NOT NULL DEFAULT 465,
        smtp_user TEXT NOT NULL DEFAULT '',
        smtp_password_env TEXT NOT NULL DEFAULT 'HAUT_SMTP_PASSWORD',
        mail_from TEXT NOT NULL DEFAULT '',
        mail_to TEXT NOT NULL DEFAULT '',
        use_tls INTEGER NOT NULL DEFAULT 1,
        event_flags TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notify_event (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        event TEXT NOT NULL,
        plan_id INTEGER,
        run_id INTEGER,
        dedup_key TEXT NOT NULL DEFAULT '',
        message TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_notify_event_at ON notify_event(at)",
    "CREATE INDEX IF NOT EXISTS idx_notify_event_dedup ON notify_event(dedup_key, at)",
    """
    CREATE TABLE IF NOT EXISTS app_setting (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        global_pause INTEGER NOT NULL DEFAULT 1,
        scheduler_enabled INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # ---------------- 座位目录 ----------------
    # 只加**新表**：本仓库没有迁移机制（SCHEMA_VERSION 写入 meta 但从未被读取），
    # 而 init_schema 在老库上会让整条 CREATE TABLE 被跳过；给既有表加列会让
    # 老库在 SELECT 时抛 "no such column"。因此 plan 表一列都不动。
    """
    CREATE TABLE IF NOT EXISTS seat_region (
        regionid INTEGER PRIMARY KEY,
        region_label TEXT NOT NULL DEFAULT '',
        label_source TEXT NOT NULL DEFAULT 'derived',
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS seat_catalog (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        regionid INTEGER NOT NULL,
        seat_id TEXT NOT NULL,
        seat_name TEXT NOT NULL,
        area_label TEXT NOT NULL DEFAULT '',
        floor_label TEXT NOT NULL DEFAULT '',
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        UNIQUE (regionid, seat_id),
        FOREIGN KEY (regionid) REFERENCES seat_region(regionid) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_seat_catalog_region ON seat_catalog(regionid, active)",
    "CREATE INDEX IF NOT EXISTS idx_seat_catalog_name ON seat_catalog(seat_name)",
    """
    CREATE TABLE IF NOT EXISTS seat_favorite (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        regionid INTEGER NOT NULL,
        seat_name TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        UNIQUE (regionid, seat_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS seat_pick (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        regionid INTEGER NOT NULL,
        seat_name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (regionid, seat_name)
    )
    """,
    # 用户端自己的「待选座位」草稿。**另起一张表**而不是给 ``seat_pick`` 加一列：
    #
    # 1. 本仓库没有迁移机制，给既有表加列会让老库 ``SELECT`` 抛 "no such column"
    #    （与上面 ``plan_origin``、下面 ``user_plan`` 同一条理由）；
    # 2. ``seat_pick`` 是**全局唯一一份**草稿（管理端在用），用户端若复用它：
    #    两个用户的选择会互相串、管理端的草稿会被用户的操作清掉
    #    —— ``replace_picks``/``clear_picks`` 的语义就是"整体替换/清空全部"。
    #    草稿属于"某个人的临时选择"，必须按人隔离，就像计划与凭据一样。
    """
    CREATE TABLE IF NOT EXISTS user_seat_pick (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        regionid INTEGER NOT NULL,
        seat_name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (user_id, regionid, seat_name),
        FOREIGN KEY (user_id) REFERENCES app_user(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS catalog_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        last_ok_at TEXT,
        last_attempt_at TEXT,
        last_status TEXT NOT NULL DEFAULT 'never',
        last_message TEXT NOT NULL DEFAULT '',
        last_region_count INTEGER NOT NULL DEFAULT 0,
        last_seat_count INTEGER NOT NULL DEFAULT 0
    )
    """,
    # 离线导入是**另一条**来源：它绝不能写 catalog_state（那会让「在线刷新成功」
    # 变成假的）。两个时间各自独立记录，页面分别展示。
    """
    CREATE TABLE IF NOT EXISTS catalog_import_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        last_import_at TEXT,
        last_attempt_at TEXT,
        last_status TEXT NOT NULL DEFAULT 'never',
        last_message TEXT NOT NULL DEFAULT '',
        last_regionid INTEGER NOT NULL DEFAULT 0,
        last_seat_count INTEGER NOT NULL DEFAULT 0
    )
    """,
    # ---------------- 平台用户（用户端自己的账号体系）----------------
    #
    # 与 ``admin_user`` **完全分开**的两套身份：管理员的会话/口令/限流都在各自
    # 的表里，用户端登录一次都碰不到管理端的 ``session`` 表（反之亦然）。
    # 这里存的**只是用户端账号**（用户名 + 平台口令哈希），
    # 与「学校统一身份认证账号/口令」不是一回事：学校口令**任何时候都不落盘**
    # （见 services.school_login 与 services.user_credentials）。
    """
    CREATE TABLE IF NOT EXISTS app_user (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_login_at TEXT
    )
    """,
    # 用户端会话。与 ``session`` 同构但**不能**共用一张表：
    # 共用会让「用户端令牌能否通过管理端校验」取决于一个 admin_id 是否恰好存在，
    # 那种隐式耦合迟早变成越权。这里用独立的表和独立的 Cookie 名。
    #
    # ``pending_captcha_*`` 保存**尚未提交的**绑定登录的验证码标识（uuid）与取得时刻。
    # 放在服务端会话行里而不是隐藏表单字段：绑定登录必须用"服务端这一次真的取过"
    # 的那张验证码，而隐藏字段是客户端可以任意改写的输入。
    # uuid 本身不是凭证（它只是配验证码图片的编号），仍与本次会话绑定以保持唯一。
    """
    CREATE TABLE IF NOT EXISTS user_session (
        token_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        csrf_token TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        pending_captcha_uuid TEXT NOT NULL DEFAULT '',
        pending_captcha_at TEXT,
        FOREIGN KEY (user_id) REFERENCES app_user(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_user_session_expires ON user_session(expires_at)",
    """
    CREATE TABLE IF NOT EXISTS user_login_attempt (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        ip TEXT NOT NULL DEFAULT '',
        ok INTEGER NOT NULL DEFAULT 0,
        at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_user_login_attempt_at ON user_login_attempt(at)",
    # 每个用户**自己那一份**学校凭据。两条不可让步的边界：
    #
    # 1. ``cookie_enc`` / ``authorization_enc`` 是经认证加密后的密文
    #    （services.credential_crypto，encrypt-then-MAC），**这里没有任何明文字段**；
    #    密钥来自 HAUT_AUTH_CRED_KEY（别名 HAUT_CRED_KEY）或 data/auth/master.key，不在库里。
    # 2. ``school_username`` 是**学校统一身份认证账号**（学号/工号这类标识），
    #    用于让用户和管理员看清「这条计划用的是哪个学校账号」。它**不是**平台用户名，
    #    也**不是**口令 —— 口令只在登录那一次经过内存，任何路径都不落盘。
    #
    # ``plan`` / ``run`` 表一列都不动（本仓库没有迁移机制），归属关系放在侧表
    # ``user_plan`` 里；`user_credential` 用 user_id 主键，天然一对一。
    """
    CREATE TABLE IF NOT EXISTS user_credential (
        user_id INTEGER PRIMARY KEY,
        school_username TEXT NOT NULL DEFAULT '',
        cookie_enc TEXT NOT NULL DEFAULT '',
        authorization_enc TEXT NOT NULL DEFAULT '',
        fingerprint TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'none',
        message TEXT NOT NULL DEFAULT '',
        captured_at TEXT,
        checked_at TEXT,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES app_user(id) ON DELETE CASCADE
    )
    """,
    # 计划归属。**没有对应行的计划一律按「无主/管理端」解释** ——
    # 老库里的计划全部建于引入用户端之前，这正是它们的真实归属，不是猜测。
    # 与 plan_origin 同一套做法（只加新表，绝不给 plan 加列）。
    """
    CREATE TABLE IF NOT EXISTS user_plan (
        plan_id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (plan_id) REFERENCES plan(id) ON DELETE CASCADE,
        FOREIGN KEY (user_id) REFERENCES app_user(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_user_plan_user ON user_plan(user_id)",
    """
    CREATE TABLE IF NOT EXISTS user_auth_event (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        at TEXT NOT NULL,
        status TEXT NOT NULL,
        message TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'manual',
        FOREIGN KEY (user_id) REFERENCES app_user(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_user_auth_event_user ON user_auth_event(user_id, id)",
)


class Database:
    """线程安全的 SQLite 访问对象（每线程一个连接）。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._local = threading.local()
        self._all_conns: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    # ---- 连接 ----

    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(self.path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        self._local.conn = conn
        with self._lock:
            self._all_conns.append(conn)
        return conn

    def close_current_thread(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        try:
            conn.close()
        except sqlite3.Error:
            pass
        self._local.conn = None
        with self._lock:
            if conn in self._all_conns:
                self._all_conns.remove(conn)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """写事务：异常自动回滚。"""
        conn = self.connect()
        try:
            with conn:
                yield conn
        except Exception:
            raise

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        return self.connect().execute(sql, params)

    def query_one(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        return self.connect().execute(sql, params).fetchone()

    def query_all(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        return list(self.connect().execute(sql, params).fetchall())

    # ---- 建表 ----

    def init_schema(self) -> None:
        now = _now_text()
        with self.tx() as conn:
            for stmt in _SCHEMA_STATEMENTS:
                conn.execute(stmt)
            # 单行配置初始化（幂等）
            conn.execute(
                "INSERT OR IGNORE INTO auth_state (id, status, recovery_state, updated_at)"
                " VALUES (1, 'indeterminate', 'idle', ?)",
                (now,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO notify_setting (id, updated_at) VALUES (1, ?)",
                (now,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO app_setting (id, global_pause, scheduler_enabled, updated_at)"
                " VALUES (1, 1, 1, ?)",
                (now,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO catalog_state (id, last_status, last_message)"
                " VALUES (1, 'never', '尚未更新过目录')",
            )
            conn.execute(
                "INSERT OR IGNORE INTO catalog_import_state (id, last_status, last_message)"
                " VALUES (1, 'never', '尚未导入过目录')",
            )
            conn.execute(
                "INSERT OR IGNORE INTO auth_credential_state (id, updated_at) VALUES (1, ?)",
                (now,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )


def _now_text() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")