"""SQLite 访问层测试。

覆盖此前只被间接使用的部分（``Database`` 的公共 API）：

- ``tx()`` 的提交 / 回滚语义，以及回滚后连接仍然可用；
- 每线程一个连接（调度器线程不能与 Web 请求线程共享 sqlite3 连接）；
- ``init_schema()`` 幂等：重复执行不会重置既有配置，也不会删数据；
- 外键开启与级联删除；
- 并发写入不会互相卡死（WAL + busy_timeout）。

这些都属于「异常处理漏洞」容易藏身的地方，因此单独成模块。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date
from pathlib import Path

import pytest

from haut_seat.web.db import SCHEMA_VERSION, Database


@pytest.fixture
def raw_db(tmp_path):
    """未经初始化的 Database，用于验证建表行为本身。"""
    db = Database(tmp_path / "raw.db")
    yield db
    db.close_current_thread()


# ------------------------------------------------------------------ 建表 / 幂等


def test_init_schema_creates_the_documented_tables(web_db):
    names = {
        r["name"]
        for r in web_db.query_all("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "admin_user", "login_attempt", "session", "plan", "run", "run_event",
        "auth_state", "auth_event", "notify_setting", "notify_event",
        "app_setting", "meta",
        "seat_region", "seat_catalog", "seat_favorite", "seat_pick", "catalog_state",
        "catalog_import_state",
    } <= names


#: 用户端（平台账号 + 每用户学校凭据 + 每用户座位草稿）引入的表。全部是**新表**。
USER_TABLES = (
    "app_user", "user_session", "user_login_attempt",
    "user_credential", "user_plan", "user_auth_event", "user_seat_pick",
)

USER_INDEXES = (
    "idx_user_session_expires", "idx_user_login_attempt_at",
    "idx_user_plan_user", "idx_user_auth_event_user",
)


def _index_names(db) -> set[str]:
    return {r["name"] for r in db.query_all("SELECT name FROM sqlite_master WHERE type = 'index'")}


def _table_columns(db, table: str) -> list[tuple]:
    return [tuple(r) for r in db.query_all(f"PRAGMA table_info({table})")]


def test_init_schema_creates_the_user_tables(web_db):
    """用户端的七张表与四个索引都要被建出来（它们是多用户体系的地基）。"""
    names = {
        r["name"]
        for r in web_db.query_all("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert set(USER_TABLES) <= names
    assert set(USER_INDEXES) <= _index_names(web_db)


def test_a_database_from_before_multiuser_only_gains_new_tables(tmp_path):
    """老库启动后**只多出几张新表**，既有表一列不动、既有行一个字节不改。

    这是多用户改造里唯一被允许的库结构变化。本仓库没有迁移机制
    （见 ``db.py`` 里的说明）：给既有表加一列，会让老库的 ``SELECT`` 直接抛
    ``no such column`` —— 而这台机器上跑着的 ``data/web.db`` 里就有真实的
    计划与运行记录，绝不能被清空或重建。

    因此这里刻意造出"引入用户端之前"的样子：把新表全部删掉、把老数据留下，
    再跑一次 ``init_schema()``，然后逐项比对。
    """
    from haut_seat.web import repository as repo
    from haut_seat.web.validation import parse_plan

    path = tmp_path / "before-multiuser.db"
    db = Database(path)
    try:
        db.init_schema()
        # 1. 放几条"历史数据"进去：一个管理员、一条计划、一条运行记录，
        #    以及一个被手动关掉的全局暂停（重启不得把它翻回默认值）。
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO admin_user (username, password_hash, created_at, updated_at)"
                " VALUES ('legacy-admin', 'hash-x', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
            conn.execute("UPDATE app_setting SET global_pause = 0 WHERE id = 1")
        form = {
            "name": "历史计划", "run_date": "2026-01-02", "repeat_rule": "once",
            "trigger_at": "07:00", "book_start": "2026-01-02 08:00:00",
            "book_end": "2026-01-02 12:00:00", "regionid": "16",
            "preferred_seats": "十层东书库北区 01桌 B座", "preferred_prefixes": "",
            "confirmed_bookable_states": '[["2", 1, false]]',
            "max_attempts_per_seat": "3", "note": "",
        }
        data, _ = parse_plan(form, today=date(2026, 1, 1))
        plan_id = repo.create_plan(db, data, enabled=True)
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO run (plan_id, started_at, finished_at, status, message)"
                " VALUES (?, '2026-01-02 07:00:00', '2026-01-02 07:00:09', 'succeeded', '历史')",
                (plan_id,),
            )

        # 2. 抹掉用户端那几张新表，还原成老库。
        with db.tx() as conn:
            for table in USER_TABLES:
                conn.execute(f"DROP TABLE {table}")
        assert not (set(USER_TABLES) & {
            r["name"] for r in db.query_all("SELECT name FROM sqlite_master WHERE type='table'")
        })

        # 3. 老库的既有结构：列清单是"改造前"的那一份。
        before_columns = {t: _table_columns(db, t) for t in ("plan", "run", "admin_user", "app_setting")}
        before_rows = {
            "plan": [tuple(r) for r in db.query_all("SELECT * FROM plan")],
            "run": [tuple(r) for r in db.query_all("SELECT * FROM run")],
            "admin_user": [tuple(r) for r in db.query_all("SELECT * FROM admin_user")],
        }

        # 4. 一次 ``init_schema()``（= 重启服务时发生的事）。
        db.init_schema()

        # 新表补建出来了，而且**真的能用**。
        assert set(USER_TABLES) <= {
            r["name"] for r in db.query_all("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert set(USER_INDEXES) <= _index_names(db)
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO app_user (username, password_hash, created_at, updated_at)"
                " VALUES ('newbie', 'hash-y', '2026-10-18 00:00:00', '2026-10-18 00:00:00')"
            )
            conn.execute(
                "INSERT INTO user_plan (plan_id, user_id, created_at)"
                " VALUES (?, (SELECT id FROM app_user WHERE username='newbie'),"
                " '2026-10-18 00:00:00')",
                (plan_id,),
            )
        assert repo.get_plan_owner(db, plan_id) is not None

        # 既有表**一列都没多、没少、没改名** —— 这才叫"只加新表"。
        for table, columns in before_columns.items():
            assert _table_columns(db, table) == columns, f"{table} 的列被改动了"
        # 既有行逐字节不变（含那条历史运行记录）。
        for table, rows in before_rows.items():
            assert [tuple(r) for r in db.query_all(f"SELECT * FROM {table}")] == rows, table
        assert db.query_one("SELECT global_pause FROM app_setting WHERE id = 1")["global_pause"] == 0
        # 老库上的查询照常工作：这正是"加列就会炸"的那条路径。
        assert repo.list_runs(db, limit=10)[0]["plan_id"] == plan_id
    finally:
        db.close_current_thread()

    # 5. 再打开一次（模拟第二次重启）：仍然是幂等的。
    again = Database(path)
    try:
        again.init_schema()
        assert again.query_one("SELECT COUNT(*) AS n FROM app_user")["n"] == 1
    finally:
        again.close_current_thread()


def test_init_schema_records_the_schema_version(raw_db):
    raw_db.init_schema()
    row = raw_db.query_one("SELECT value FROM meta WHERE key = 'schema_version'")
    assert row["value"] == str(SCHEMA_VERSION)


def test_init_schema_is_idempotent(raw_db):
    raw_db.init_schema()
    raw_db.init_schema()  # 第二次不能抛异常
    for table, pk in (("auth_state", 1), ("notify_setting", 1), ("app_setting", 1)):
        n = raw_db.query_one(f"SELECT COUNT(*) AS n FROM {table} WHERE id = ?", (pk,))["n"]
        assert n == 1, f"{table} 的单行配置被建成了多行"


def test_single_row_defaults_are_safe(web_db):
    """安全默认：认证状态未知、通知关闭、全局暂停**开启**。"""
    auth = web_db.query_one("SELECT * FROM auth_state WHERE id = 1")
    assert auth["status"] == "indeterminate"
    assert auth["recovery_state"] == "idle"

    notify = web_db.query_one("SELECT * FROM notify_setting WHERE id = 1")
    assert notify["enabled"] == 0, "通知默认必须是关闭的"

    app = web_db.query_one("SELECT * FROM app_setting WHERE id = 1")
    assert app["global_pause"] == 1, "全局暂停必须默认开启，否则启动即可能真实预约"


def test_global_pause_is_not_reset_by_a_second_init(web_db):
    """再次初始化（例如重启服务）不能把用户手动关闭的全局暂停又打开。"""
    with web_db.tx() as conn:
        conn.execute("UPDATE app_setting SET global_pause = 0 WHERE id = 1")
    web_db.init_schema()
    assert web_db.query_one("SELECT global_pause FROM app_setting WHERE id = 1")["global_pause"] == 0


def test_init_schema_keeps_existing_rows(web_db):
    with web_db.tx() as conn:
        conn.execute(
            "INSERT INTO admin_user (username, password_hash, created_at, updated_at)"
            " VALUES ('keepme', 'x', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        )
    web_db.init_schema()
    assert web_db.query_one("SELECT 1 AS x FROM admin_user WHERE username = 'keepme'") is not None


def test_init_schema_creates_the_parent_directory(tmp_path):
    """路径不存在时应自行创建，避免首次启动因目录缺失而失败。"""
    nested = tmp_path / "deep" / "deeper" / "web.db"
    db = Database(nested)
    try:
        db.init_schema()
        assert nested.exists()
    finally:
        db.close_current_thread()


# ------------------------------------------------------------------ 连接与线程


def test_connection_is_reused_within_a_thread(web_db):
    assert web_db.connect() is web_db.connect()


def test_each_thread_gets_its_own_connection(web_db):
    seen = {}

    def worker():
        seen["conn"] = web_db.connect()
        seen["thread"] = threading.get_ident()
        web_db.close_current_thread()

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert seen["conn"] is not web_db.connect(), "后台线程复用了 Web 线程的连接"
    assert seen["thread"] != threading.get_ident()


def test_writes_from_another_thread_are_visible(web_db):
    """调度器线程写入的数据，Web 线程必须能立刻看到。"""

    def worker():
        with web_db.tx() as conn:
            conn.execute(
                "INSERT INTO auth_event (at, status, message)"
                " VALUES ('2026-01-01 00:00:00', 'valid', '来自后台线程')"
            )
        web_db.close_current_thread()

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert web_db.query_one("SELECT message FROM auth_event")["message"] == "来自后台线程"


def test_close_current_thread_is_safe_when_never_connected(raw_db):
    raw_db.close_current_thread()  # 不应抛异常
    raw_db.close_current_thread()
    assert raw_db.query_one("SELECT 1 AS x")["x"] == 1


def test_connection_is_usable_again_after_closing(web_db):
    web_db.connect()
    web_db.close_current_thread()
    assert web_db.query_one("SELECT 1 AS x")["x"] == 1


def test_pragmas_are_applied_on_every_connection(web_db):
    for _ in range(2):
        conn = web_db.connect()
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 15000
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_rows_support_named_access(web_db):
    row = web_db.query_one("SELECT 1 AS answer")
    assert row["answer"] == 1
    assert isinstance(row, sqlite3.Row)


# ------------------------------------------------------------------ 事务


def test_tx_commits_on_success(web_db):
    with web_db.tx() as conn:
        conn.execute(
            "INSERT INTO auth_event (at, status, message) VALUES (?, ?, ?)",
            ("2026-01-01 00:00:00", "valid", "已提交"),
        )
    assert web_db.query_one("SELECT COUNT(*) AS n FROM auth_event")["n"] == 1


def test_tx_rolls_back_on_exception(web_db):
    with pytest.raises(RuntimeError):
        with web_db.tx() as conn:
            conn.execute(
                "INSERT INTO auth_event (at, status, message) VALUES (?, ?, ?)",
                ("2026-01-01 00:00:00", "valid", "应被回滚"),
            )
            raise RuntimeError("模拟中途失败")
    assert web_db.query_one("SELECT COUNT(*) AS n FROM auth_event")["n"] == 0


def test_tx_rolls_back_on_constraint_violation(web_db):
    """半途撞上唯一约束：既不能留下半条记录，也不能把连接弄坏。"""
    with web_db.tx() as conn:
        conn.execute(
            "INSERT INTO admin_user (username, password_hash, created_at, updated_at)"
            " VALUES ('dup', 'h', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        with web_db.tx() as conn:
            conn.execute(
                "INSERT INTO admin_user (username, password_hash, created_at, updated_at)"
                " VALUES ('dup', 'h', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
    assert web_db.query_one("SELECT COUNT(*) AS n FROM admin_user")["n"] == 1
    # 连接仍可用
    with web_db.tx() as conn:
        conn.execute(
            "INSERT INTO auth_event (at, status) VALUES ('2026-01-01 00:00:00', 'valid')"
        )
    assert web_db.query_one("SELECT COUNT(*) AS n FROM auth_event")["n"] == 1


def test_exception_type_propagates_unchanged(web_db):
    """tx 只负责事务；异常必须原样上抛，不能被吞掉或包装。"""
    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with web_db.tx():
            raise Boom("x")


def test_concurrent_writers_do_not_deadlock(web_db):
    """Web 请求线程与调度器线程同时写：靠 WAL + busy_timeout 不应互相卡死。"""
    errors: list[Exception] = []

    def worker(tag: str):
        try:
            for i in range(30):
                with web_db.tx() as conn:
                    conn.execute(
                        "INSERT INTO login_attempt (username, ip, ok, at)"
                        " VALUES (?, '', 1, ?)",
                        (f"{tag}-{i}", "2026-01-01 00:00:00"),
                    )
        except Exception as exc:  # noqa: BLE001 - 测试要看到失败原因
            errors.append(exc)
        finally:
            web_db.close_current_thread()

    threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "并发写入被卡住（可能死锁）"
    assert errors == []
    assert web_db.query_one("SELECT COUNT(*) AS n FROM login_attempt")["n"] == 90


# ------------------------------------------------------------------ 外键


def test_foreign_keys_are_enforced(web_db):
    with pytest.raises(sqlite3.IntegrityError):
        with web_db.tx() as conn:
            conn.execute(
                "INSERT INTO run (plan_id, started_at, status) VALUES (999, ?, 'running')",
                ("2026-01-01 00:00:00",),
            )


def test_deleting_a_plan_cascades_to_runs_and_events(web_db):
    with web_db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO plan (name, run_date, book_start, book_end, regionid,"
            " created_at, updated_at)"
            " VALUES ('p', '2026-01-01', '2026-01-01 08:00:00',"
            " '2026-01-01 12:00:00', 16, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        )
        plan_id = int(cur.lastrowid)
        cur = conn.execute(
            "INSERT INTO run (plan_id, started_at, status) VALUES (?, ?, 'running')",
            (plan_id, "2026-01-01 00:00:00"),
        )
        run_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO run_event (run_id, at, phase) VALUES (?, ?, 'start')",
            (run_id, "2026-01-01 00:00:00"),
        )

    with web_db.tx() as conn:
        conn.execute("DELETE FROM plan WHERE id = ?", (plan_id,))

    assert web_db.query_one("SELECT COUNT(*) AS n FROM run")["n"] == 0
    assert web_db.query_one("SELECT COUNT(*) AS n FROM run_event")["n"] == 0


def test_plan_defaults_are_paused_and_disarmed(web_db):
    """新建计划默认「已暂停 + 未武装」，这是「启动服务不会自动预约」的第一道闸。"""
    with web_db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO plan (name, run_date, book_start, book_end, regionid,"
            " created_at, updated_at)"
            " VALUES ('default', '2026-01-01', '2026-01-01 08:00:00',"
            " '2026-01-01 12:00:00', 16, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        )
        plan_id = int(cur.lastrowid)
    row = web_db.query_one("SELECT * FROM plan WHERE id = ?", (plan_id,))
    assert row["enabled"] == 0
    assert row["status"] == "paused"
    assert row["repeat_rule"] == "once"
    assert row["pause_requested"] == 0 and row["stop_requested"] == 0
    assert row["unknown_ack_at"] is None


def test_database_path_is_kept_as_given(tmp_path):
    path = tmp_path / "given.db"
    db = Database(str(path))
    try:
        assert Path(db.path) == path
    finally:
        db.close_current_thread()