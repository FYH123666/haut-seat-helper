"""Web 相关 CLI 命令测试：``init-db`` / ``create-admin`` / ``web``。

这三个命令是文档里面向生产的使用方式（尤其 ``create-admin``，在
``HAUT_WEB_ALLOW_FIRST_RUN_SETUP=false`` 时是**唯一**能建管理员的途径），
此前完全没有测试。

约定：
- 只调用 ``cmd_*`` 函数本身，注入指向 ``tmp_path`` 的配置，绝不碰 ``data/web.db``；
- 绝不真的启动 uvicorn（用替身模块记录调用参数）；
- 口令不得出现在 stdout / stderr / 数据库明文里。
"""

from __future__ import annotations

import argparse
import logging
import sys
import types

import pytest

from haut_seat.__main__ import (
    build_parser,
    cmd_create_admin,
    cmd_init_db,
    cmd_web,
)
from haut_seat.web import security
from haut_seat.web.db import Database

GOOD_PASSWORD = "Correct-Horse-9"


@pytest.fixture
def logger():
    return logging.getLogger("test.cli")


@pytest.fixture
def settings(tmp_path, make_settings):
    return make_settings(
        web_db=str(tmp_path / "web.db"),
        web_allow_first_run_setup=True,
        enable_real_booking=False,
        pause_file=str(tmp_path / "pause.flag"),
        book_lock_file=str(tmp_path / "book.lock"),
    )


def open_db(settings):
    db = Database(settings.web_db)
    db.init_schema()
    return db


# ------------------------------------------------------------------ 参数解析


def test_new_subcommands_are_wired_to_their_functions():
    p = build_parser()
    assert p.parse_args(["init-db", ]).func is cmd_init_db
    assert p.parse_args(["create-admin", "--username", "u"]).func is cmd_create_admin
    assert p.parse_args(["web"]).func is cmd_web


def test_web_subcommand_defaults_are_safe():
    a = build_parser().parse_args(["web"])
    assert a.host is None, "默认必须交给配置决定（配置默认 127.0.0.1）"
    assert a.port is None
    assert a.init_db is False


def test_web_subcommand_accepts_host_port_and_init_db():
    a = build_parser().parse_args(["web", "--host", "0.0.0.0", "--port", "9000", "--init-db"])
    assert (a.host, a.port, a.init_db) == ("0.0.0.0", 9000, True)


def test_create_admin_requires_a_username():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["create-admin"])


def test_create_admin_reset_flag_defaults_off():
    a = build_parser().parse_args(["create-admin", "--username", "u"])
    assert a.reset is False
    assert a.password is None, "默认必须走交互式输入，避免口令留在 shell 历史里"


def test_default_web_host_is_loopback(settings):
    assert settings.web_host in ("127.0.0.1", "localhost", "::1")


# ------------------------------------------------------------------ init-db


def test_init_db_creates_the_schema_and_reports_the_admin_count(
    settings, logger, capsys
):
    assert cmd_init_db(argparse.Namespace(), settings, logger) == 0
    out = capsys.readouterr().out
    assert "数据库已初始化" in out
    assert "管理员数量: 0" in out
    assert "create-admin" in out, "没有管理员时应提示下一步怎么做"
    assert open_db(settings).query_one("SELECT 1 AS x FROM app_setting") is not None


def test_init_db_is_idempotent_and_keeps_existing_admins(settings, logger, capsys):
    db = open_db(settings)
    security.create_admin(db, "tester", GOOD_PASSWORD)
    db.close_current_thread()

    assert cmd_init_db(argparse.Namespace(), settings, logger) == 0
    assert "管理员数量: 1" in capsys.readouterr().out

    db = open_db(settings)
    try:
        row = security.find_admin(db, "tester")
        assert row is not None, "init-db 删掉了既有管理员"
        assert security.verify_password(GOOD_PASSWORD, row["password_hash"])
    finally:
        db.close_current_thread()


def test_init_db_does_not_reopen_the_global_pause(settings, logger):
    """重启服务时跑 init-db 不能把用户手动关闭的全局暂停又打开。"""
    db = open_db(settings)
    with db.tx() as conn:
        conn.execute("UPDATE app_setting SET global_pause = 0 WHERE id = 1")
    db.close_current_thread()

    assert cmd_init_db(argparse.Namespace(), settings, logger) == 0
    db = open_db(settings)
    try:
        assert db.query_one("SELECT global_pause FROM app_setting WHERE id = 1")["global_pause"] == 0
    finally:
        db.close_current_thread()


def test_init_db_creates_no_plan_or_run(settings, logger):
    cmd_init_db(argparse.Namespace(), settings, logger)
    db = open_db(settings)
    try:
        assert db.query_one("SELECT COUNT(*) AS n FROM plan")["n"] == 0
        assert db.query_one("SELECT COUNT(*) AS n FROM run")["n"] == 0
    finally:
        db.close_current_thread()


# ------------------------------------------------------------------ create-admin


def _create(args_kwargs, settings, logger):
    fields = {"password": None, "reset": False}
    fields.update(args_kwargs)
    return cmd_create_admin(argparse.Namespace(**fields), settings, logger)


def test_create_admin_rejects_a_short_password(settings, logger, capsys):
    code = _create({"username": "tester", "password": "short"}, settings, logger)
    assert code == 2
    err = capsys.readouterr().err
    assert "创建失败" in err
    assert "short" not in err, "错误信息里不应回显口令"
    assert security.count_admins(open_db(settings)) == 0


def test_create_admin_rejects_a_whitespace_padded_password(settings, logger, capsys):
    code = _create({"username": "tester", "password": " password-1234 "}, settings, logger)
    assert code == 2
    assert "空白" in capsys.readouterr().err
    assert security.count_admins(open_db(settings)) == 0


def test_create_admin_rejects_a_blank_username(settings, logger, capsys):
    code = _create({"username": "   ", "password": GOOD_PASSWORD}, settings, logger)
    assert code == 2
    assert "--username" in capsys.readouterr().err


def test_create_admin_stores_only_a_hash(settings, logger, capsys):
    assert _create({"username": "tester", "password": GOOD_PASSWORD}, settings, logger) == 0
    out = capsys.readouterr().out
    assert "已创建" in out
    assert GOOD_PASSWORD not in out, "口令绝不能出现在终端输出里"

    db = open_db(settings)
    try:
        row = security.find_admin(db, "tester")
        assert row is not None
        assert GOOD_PASSWORD not in row["password_hash"]
        assert row["password_hash"].startswith("scrypt$")
        assert security.verify_password(GOOD_PASSWORD, row["password_hash"])
    finally:
        db.close_current_thread()


def test_create_admin_never_writes_the_plaintext_password_into_the_database(
    settings, logger
):
    assert _create({"username": "tester", "password": GOOD_PASSWORD}, settings, logger) == 0
    db = open_db(settings)
    try:
        for table in ("admin_user", "session", "login_attempt", "auth_event"):
            assert GOOD_PASSWORD not in str(db.query_all(f"SELECT * FROM {table}")), table
    finally:
        db.close_current_thread()


def test_create_admin_does_not_reuse_a_duplicate_username(settings, logger, capsys):
    assert _create({"username": "tester", "password": GOOD_PASSWORD}, settings, logger) == 0
    capsys.readouterr()

    code = _create({"username": "tester", "password": "Another-Password-1"}, settings, logger)
    assert code == 2
    assert "--reset" in capsys.readouterr().err

    db = open_db(settings)
    try:
        assert security.count_admins(db) == 1
        row = security.find_admin(db, "tester")
        assert security.verify_password(GOOD_PASSWORD, row["password_hash"]), "口令被意外改掉了"
    finally:
        db.close_current_thread()


def test_create_admin_reset_changes_the_password_and_kills_sessions(settings, logger, capsys):
    assert _create({"username": "tester", "password": GOOD_PASSWORD}, settings, logger) == 0
    db = open_db(settings)
    admin = security.find_admin(db, "tester")
    token, _csrf = security.create_session(db, int(admin["id"]), username="tester")
    assert security.load_session(db, token) is not None
    db.close_current_thread()
    capsys.readouterr()

    new_password = "Another-Password-1"
    code = _create(
        {"username": "tester", "password": new_password, "reset": True}, settings, logger
    )
    assert code == 0
    assert "已重置" in capsys.readouterr().out

    db = open_db(settings)
    try:
        row = security.find_admin(db, "tester")
        assert security.verify_password(new_password, row["password_hash"])
        assert not security.verify_password(GOOD_PASSWORD, row["password_hash"])
        assert security.load_session(db, token) is None, "改密后既有会话必须失效"
    finally:
        db.close_current_thread()


def test_create_admin_reset_rejects_a_weak_password_and_keeps_the_old_one(
    settings, logger, capsys
):
    assert _create({"username": "tester", "password": GOOD_PASSWORD}, settings, logger) == 0
    capsys.readouterr()

    code = _create({"username": "tester", "password": "tiny", "reset": True}, settings, logger)
    assert code == 2
    assert "重置失败" in capsys.readouterr().err

    db = open_db(settings)
    try:
        row = security.find_admin(db, "tester")
        assert security.verify_password(GOOD_PASSWORD, row["password_hash"])
    finally:
        db.close_current_thread()


def test_create_admin_without_a_password_flag_asks_interactively(
    settings, logger, monkeypatch
):
    import getpass

    answers = iter([GOOD_PASSWORD, GOOD_PASSWORD])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))
    assert _create({"username": "tester"}, settings, logger) == 0

    db = open_db(settings)
    try:
        assert security.verify_password(GOOD_PASSWORD, security.find_admin(db, "tester")["password_hash"])
    finally:
        db.close_current_thread()


def test_create_admin_rejects_mismatched_confirmation(settings, logger, monkeypatch, capsys):
    import getpass

    answers = iter([GOOD_PASSWORD, GOOD_PASSWORD + "x"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))
    assert _create({"username": "tester"}, settings, logger) == 2
    assert "不一致" in capsys.readouterr().err
    assert security.count_admins(open_db(settings)) == 0


def test_create_admin_cancelled_at_the_prompt_is_not_an_error(settings, logger, monkeypatch, capsys):
    import getpass

    def abort(prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr(getpass, "getpass", abort)
    assert _create({"username": "tester"}, settings, logger) == 2
    assert "已取消" in capsys.readouterr().err
    assert security.count_admins(open_db(settings)) == 0


def test_create_admin_works_without_a_terminal_by_using_the_flag(settings, logger, monkeypatch):
    """非交互环境（如 systemd 前置脚本）必须能用 --password 建管理员。"""
    import getpass

    def no_tty(prompt=""):
        raise EOFError

    monkeypatch.setattr(getpass, "getpass", no_tty)
    assert _create({"username": "tester", "password": GOOD_PASSWORD}, settings, logger) == 0
    assert security.count_admins(open_db(settings)) == 1


# ------------------------------------------------------------------ web


class FakeUvicorn(types.ModuleType):
    """替身 uvicorn：只记录调用参数，绝不真的监听端口。"""

    def __init__(self):
        super().__init__("uvicorn")
        self.calls: list[tuple[tuple, dict]] = []

    def run(self, app, *args, **kwargs):
        self.calls.append(((app, *args), kwargs))


@pytest.fixture
def fake_uvicorn(monkeypatch):
    fake = FakeUvicorn()
    monkeypatch.setitem(sys.modules, "uvicorn", fake)
    return fake


def web_args(**overrides):
    base = {"host": None, "port": None, "init_db": False}
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def web_settings(tmp_path, make_settings):
    """不自动起调度器线程、不真实预约的 Web 配置。"""
    return make_settings(
        web_db=str(tmp_path / "web.db"),
        web_host="127.0.0.1",
        web_port=8080,
        web_allow_first_run_setup=True,
        web_enable_scheduler=False,
        enable_real_booking=False,
        pause_file=str(tmp_path / "pause.flag"),
        book_lock_file=str(tmp_path / "book.lock"),
    )


def test_web_command_runs_uvicorn_without_debug_or_reload(web_settings, logger, fake_uvicorn, capsys):
    assert cmd_web(web_args(), web_settings, logger) == 0
    assert len(fake_uvicorn.calls) == 1
    _args, kwargs = fake_uvicorn.calls[0]
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 8080
    assert kwargs.get("reload") is not True, "生产环境不能开自动重载"
    assert kwargs.get("debug") is not True
    out = capsys.readouterr().out
    assert "http://127.0.0.1:8080/admin" in out
    assert web_settings.web_db in out


def test_web_command_honours_host_and_port_overrides(web_settings, logger, fake_uvicorn):
    assert cmd_web(web_args(host="127.0.0.1", port=9001), web_settings, logger) == 0
    _args, kwargs = fake_uvicorn.calls[0]
    assert (kwargs["host"], kwargs["port"]) == ("127.0.0.1", 9001)


def test_web_command_warns_when_binding_a_non_loopback_address(web_settings, logger, fake_uvicorn, capsys):
    cmd_web(web_args(host="0.0.0.0"), web_settings, logger)
    assert "警告" in capsys.readouterr().err


def test_web_command_does_not_warn_for_a_loopback_address(web_settings, logger, fake_uvicorn, capsys):
    cmd_web(web_args(host="127.0.0.1"), web_settings, logger)
    assert "警告" not in capsys.readouterr().err


def test_web_command_initialises_the_database_with_init_db(web_settings, logger, fake_uvicorn, capsys):
    assert cmd_web(web_args(init_db=True), web_settings, logger) == 0
    assert "数据库已初始化" in capsys.readouterr().out
    assert open_db(web_settings).query_one("SELECT 1 AS x FROM app_setting") is not None


def test_web_command_initialises_the_schema_even_without_the_flag(web_settings, logger, fake_uvicorn):
    """即便忘了 --init-db 也不能启动到一个没有表的库上。"""
    assert cmd_web(web_args(), web_settings, logger) == 0
    assert open_db(web_settings).query_one("SELECT 1 AS x FROM meta") is not None


def test_web_command_reports_a_missing_web_dependency(settings, logger, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "uvicorn", None)  # 让 import uvicorn 抛 ImportError
    assert cmd_web(web_args(), settings, logger) == 2
    err = capsys.readouterr().err
    assert "pip install" in err


def test_web_command_never_records_a_run(web_settings, logger, fake_uvicorn):
    """硬约束：启动 Web 服务本身绝不产生任何预约运行。"""
    cmd_web(web_args(), web_settings, logger)
    app = fake_uvicorn.calls[0][0][0]
    assert app.state.scheduler.is_running() is False

    db = open_db(web_settings)
    try:
        assert db.query_one("SELECT COUNT(*) AS n FROM run")["n"] == 0
        assert db.query_one("SELECT COUNT(*) AS n FROM plan")["n"] == 0
    finally:
        db.close_current_thread()


def test_scheduler_thread_does_nothing_on_an_empty_database(tmp_path, make_settings, logger, fake_uvicorn):
    """把 lifespan 真的跑起来：调度器启动后，空库上也不会有任何预约动静。

    这是「启动服务不会自动执行真实预约」这条约束最直接的证据：
    这里用的是 ``create_app`` 的真实依赖（真实 Scheduler），
    因此只要有可执行计划，就会立刻对学校服务器发起查询。
    """
    settings = make_settings(
        web_db=str(tmp_path / "empty.db"),
        web_enable_scheduler=True,
        scheduler_tick_s=0.05,
        enable_real_booking=False,
        pause_file=str(tmp_path / "pause.flag"),
        book_lock_file=str(tmp_path / "book.lock"),
    )
    assert cmd_web(web_args(), settings, logger) == 0
    app = fake_uvicorn.calls[0][0][0]

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    with TestClient(app):
        assert app.state.scheduler.is_running() is True, "调度器未按配置启动"
        for _ in range(10):
            with app.state.db.tx() as conn:
                conn.execute("SELECT 1")
            if app.state.db.query_one("SELECT COUNT(*) AS n FROM run")["n"]:
                break

    db = open_db(settings)
    try:
        assert db.query_one("SELECT COUNT(*) AS n FROM run")["n"] == 0
        assert db.query_one("SELECT COUNT(*) AS n FROM plan")["n"] == 0
    finally:
        db.close_current_thread()


def test_web_command_leaves_the_global_pause_on(web_settings, logger, fake_uvicorn):
    """新库的全局暂停默认开启：即使有计划进来，也必须人工放行。"""
    cmd_web(web_args(), web_settings, logger)
    db = open_db(web_settings)
    try:
        assert db.query_one("SELECT global_pause FROM app_setting WHERE id = 1")["global_pause"] == 1
    finally:
        db.close_current_thread()


def test_web_command_respects_the_real_booking_switch(web_settings, logger, fake_uvicorn, capsys):
    cmd_web(web_args(), web_settings, logger)
    out = capsys.readouterr().out
    assert "真实预约开关: False" in out
    assert "禁止提交" in out
    app = fake_uvicorn.calls[0][0][0]
    assert app.state.settings.enable_real_booking is False


def test_web_command_wires_the_trigger_lead_from_settings(
    web_settings, make_settings, tmp_path, logger, fake_uvicorn
):
    """HAUT_SCHEDULER_TRIGGER_LEAD_S → Settings → ``create_app`` 真正构造的 Scheduler。

    这条守护的是「配置接对了没有」：``Scheduler`` 自己的默认值是 0（关闭），
    但如果 ``create_app`` 忘了把设置传进去，改 ``.env`` 将毫无效果。
    这里走的是真实依赖（``create_app`` 自己建 Scheduler），只注入替身 uvicorn。
    """
    cmd_web(web_args(), web_settings, logger)
    assert fake_uvicorn.calls[0][0][0].state.scheduler.trigger_lead_s == 0.0

    led = make_settings(
        web_db=str(tmp_path / "led.db"),
        web_enable_scheduler=False,
        enable_real_booking=False,
        pause_file=str(tmp_path / "pause.flag"),
        book_lock_file=str(tmp_path / "book.lock"),
        scheduler_trigger_lead_s=45,
    )
    cmd_web(web_args(), led, logger)
    assert fake_uvicorn.calls[1][0][0].state.scheduler.trigger_lead_s == 45.0


def test_web_command_reports_the_session_cookie_policy(web_settings, logger, fake_uvicorn, capsys):
    """启动横幅必须说明会话 Cookie 有没有 Secure。

    这一项配错（开着 Secure 却用 http:// 访问）会造成「登录成功却一直回到登录页」，
    而且服务端没有任何异常 —— 横幅里写明，日志就能直接自证。
    """
    cmd_web(web_args(), web_settings, logger)
    out = capsys.readouterr().out
    assert "会话 Cookie: 不带 Secure" in out
    assert f"数据库: {web_settings.web_db}" in out


def test_web_command_warns_about_secure_cookies_over_http(make_settings, tmp_path,
                                                          logger, fake_uvicorn, capsys):
    """开启 Secure 时，横幅要写明「http:// 访问会一直回到登录页」。"""
    settings = make_settings(
        web_db=str(tmp_path / "web.db"),
        web_session_secure=True,
        web_enable_scheduler=False,
        enable_real_booking=False,
        pause_file=str(tmp_path / "pause.flag"),
        book_lock_file=str(tmp_path / "book.lock"),
    )
    cmd_web(web_args(), settings, logger)
    out = capsys.readouterr().out
    assert "会话 Cookie: Secure" in out
    assert "HTTPS" in out