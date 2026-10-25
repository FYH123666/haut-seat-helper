"""测试夹具：隔离环境变量、提供脱敏样例。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from haut_seat.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = PROJECT_ROOT / "samples"

# 测试期的凭据隔离目录：**故意不创建**。
# 指向一个进程唯一的、不存在的路径，这样任何测试都不可能意外读到开发机上
# 真实存在的 data/auth/credentials.json，从而做到可复现。
_AUTH_ISOLATION_DIR = Path(tempfile.gettempdir()) / f"haut-tests-auth-{os.getpid()}"


@pytest.fixture(autouse=True)
def _clean_haut_env(monkeypatch):
    """清除进程中的 HAUT_* 环境变量，保证测试确定性。

    随后**强制** ``HAUT_AUTH_SOURCE=env``：产品默认是 ``auto``，但测试若继承
    ``auto``，就可能去读开发机上真实的凭据快照。强制 ``env`` 之后，
    所有既有测试走的凭据路径与引入凭据来源层之前**逐字节相同**。
    需要测 ``auto`` / ``browser`` 的用例在自己的作用域里覆盖这两个变量。
    """
    for k in list(os.environ):
        if k.upper().startswith("HAUT_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HAUT_AUTH_SOURCE", "env")
    monkeypatch.setenv("HAUT_AUTH_DIR", str(_AUTH_ISOLATION_DIR))
    monkeypatch.setenv("HAUT_AUTH_SNAPSHOT_FILE", str(_AUTH_ISOLATION_DIR / "credentials.json"))
    monkeypatch.setenv("HAUT_AUTH_PROFILE_DIR", str(_AUTH_ISOLATION_DIR / "browser-profile"))
    # 每用户学校凭据的**加密主密钥**同样必须隔离：默认路径是相对 CWD 的
    # ``data/auth/master.key``，测试若沿用就会读写开发机上真实的那一个
    # （既污染生产密钥文件，也让测试互相依赖）。这里指到一个进程唯一的临时路径，
    # 并清掉进程级的主密钥缓存 —— 缓存键含路径，不清会留下跨用例的幽灵密钥。
    monkeypatch.setenv("HAUT_AUTH_MASTER_KEY_FILE", str(_AUTH_ISOLATION_DIR / "master.key"))
    from haut_seat.services.credential_crypto import reset_master_key_cache

    reset_master_key_cache()
    # 进程级的 HTTP 客户端缓存与出站节流器也必须逐测试清空：它们是**跨测试**共享的
    # 状态，留着会让上一条用例的 MockTransport / 节流时间戳泄漏到下一条，
    # 出现「单跑通过、全跑失败」这种最难查的失败。与上面的环境变量隔离同一目的。
    _reset_shared_http_state()


def _reset_shared_http_state() -> None:
    """清空共享 HTTP 客户端缓存与进程级节流器。

    导入失败必须吵闹地失败：名字写错 / 模块被删这类问题如果被吞掉，
    隔离就悄悄失效，退化成「单跑通过、全跑失败」的幽灵故障。
    真正关闭客户端时抛的异常（例如连接已关闭）才允许吞掉。
    """
    from haut_seat.client.http import reset_shared_throttle
    from haut_seat.services.io import close_shared_http_clients

    reset_shared_throttle()
    try:
        close_shared_http_clients()
    except Exception:  # pragma: no cover - 清理失败不该让测试失败
        pass


@pytest.fixture
def sample_layout_raw() -> dict:
    path = SAMPLE_DIR / "layout_query_sample.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def make_settings():
    """构造不读取 .env、不读取环境变量的 Settings。"""

    def _make(**overrides):
        return Settings(_env_file=None, **overrides)

    return _make


# ------------------------------------------------------------------ Web / 调度器夹具
#
# 全部离线：不发任何真实网络请求，也不调用真实预约提交接口。


@pytest.fixture
def web_settings(tmp_path, make_settings):
    """指向临时数据库的 Web 配置（调度器默认不自动启动）。

    ``book_lock_wait_s=0``：测试里**绝不**为时段锁真的等待。默认值是 5 秒
    （给真实运行留出「同址另一个任务正好在收尾」的余量），但在测试中等待只会
    让用例白白变慢，还会把「锁被占用」这条路径的断言变成时序赌博。
    需要验证「有界等待」的用例在自己的作用域里显式传一个更长的值。
    """
    return make_settings(
        web_db=str(tmp_path / "web.db"),
        web_allow_first_run_setup=True,
        enable_real_booking=False,
        pause_file=str(tmp_path / "pause.flag"),
        book_lock_file=str(tmp_path / "book.lock"),
        book_lock_wait_s=0.0,
    )


@pytest.fixture
def web_db(web_settings):
    from haut_seat.web.db import Database

    db = Database(web_settings.web_db)
    db.init_schema()
    yield db
    db.close_current_thread()


def offline_scheduler(db, settings, **kwargs):
    """不接触网络的调度器：查询返回空布局，提交一旦被调用即大声失败。

    Web 层的测试只关心「接口接对了、状态流转正确」，
    因此查询替身永远返回「没有可预约座位」，提交替身直接抛错——
    这样任何一次意外的真实提交都会立刻暴露，而不是被静默吞掉。
    额外的 ``kwargs``（如 clock / time_mod）透传给 ``Scheduler``。
    """
    from haut_seat.core.auth import AuthCheck, AuthStatus
    from haut_seat.models.seat import LayoutQueryResult
    from haut_seat.services.scheduler import Scheduler

    def query_fn(settings_, regionid, start, end, page_num, page_size):
        return LayoutQueryResult.from_response({"layout": None, "seatList": []})

    def submit_fn(settings_, target_seat, submit_start, end):
        raise RuntimeError("测试禁止调用真实预约提交接口")

    params = {
        "tick_s": 1.0,
        "max_concurrent": 1,
        "query_fn": query_fn,
        "submit_fn": submit_fn,
        "auth_check_fn": lambda s, r, start=None, end=None: AuthCheck(
            AuthStatus.VALID, "测试替身：凭据有效"
        ),
    }
    params.update(kwargs)
    return Scheduler(db, settings, **params)


def offline_auth_service(db, settings, status="valid"):
    """不接触网络的认证服务：预检结果由测试直接指定。"""
    from haut_seat.core.auth import AuthCheck, AuthStatus
    from haut_seat.services.auth_service import AuthService

    chosen = AuthStatus(status)
    return AuthService(
        db, settings, check_fn=lambda *a, **k: AuthCheck(chosen, f"测试替身：{status}")
    )


# 座位名用**真实观测到的格式**（「十层东书库北区 01桌 E座」），
# 这样区域名/楼层的推断逻辑才被真正走到；samples/ 里那份 A-001 占位符做不到。
CATALOG_TEST_SEATS = (
    {"id": 2481, "seatName": "十层东书库北区 01桌 E座"},
    {"id": 2482, "seatName": "十层东书库北区 01桌 F座"},
    {"id": 2483, "seatName": "十层东书库北区 02桌 A座"},
)


def offline_catalog_service(db, settings, *, seats=None, error=None):
    """不接触网络的座位目录服务：只读查询返回固定的少量座位。

    目录刷新在任何情况下都不得触达真实提交接口 —— 本替身只提供 ``query_fn``，
    ``submit_fn`` 根本不存在于该服务中。
    """
    from haut_seat.models.seat import LayoutQueryResult
    from haut_seat.services.catalog_service import CatalogService

    rows = CATALOG_TEST_SEATS if seats is None else tuple(seats)

    def query_fn(settings_, regionid, start, end, page_num, page_size):
        if error is not None:
            raise error
        return LayoutQueryResult.from_response(
            {"layout": None, "seatList": [dict(r) for r in rows]}
        )

    return CatalogService(db, settings, query_fn=query_fn)


@pytest.fixture
def make_client(web_settings):
    """返回 (app, TestClient) 工厂；默认不启动后台调度器，且全部依赖离线。

    ``create_app`` 若使用默认依赖会对学校服务器发真实只读预检/座位查询，
    因此这里**总是**注入替身；需要真实行为的用例请显式传入自己的替身。
    """
    from haut_seat.web.app import create_app
    from haut_seat.web.db import Database

    created = []

    def _make(
        *,
        start_scheduler=False,
        scheduler=None,
        auth_service=None,
        catalog_service=None,
        settings=None,
        db=None,
        **overrides,
    ):
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        effective = settings or web_settings
        database = db or Database(effective.web_db)
        database.init_schema()
        app = create_app(
            effective,
            db=database,
            start_scheduler=start_scheduler,
            scheduler=scheduler or offline_scheduler(database, effective),
            auth_service=auth_service or offline_auth_service(database, effective),
            catalog_service=catalog_service or offline_catalog_service(database, effective),
            **overrides,
        )
        created.append((app, database))
        return app, TestClient(app, follow_redirects=False)

    yield _make
    for _app, db in created:
        db.close_current_thread()


@pytest.fixture
def offline():
    """把离线替身构造器暴露给用例（避免用例去 import conftest）。"""
    from types import SimpleNamespace

    return SimpleNamespace(
        scheduler=offline_scheduler,
        auth_service=offline_auth_service,
        catalog_service=offline_catalog_service,
    )


@pytest.fixture
def admin_client(make_client):
    """已登录的管理员客户端（口令走真实 scrypt 校验，但不做任何网络访问）。"""
    from haut_seat.web import security

    app, client = make_client()
    security.create_admin(app.state.db, "tester", "Correct-Horse-9")
    resp = client.post(
        "/admin/login", data={"username": "tester", "password": "Correct-Horse-9"}
    )
    assert resp.status_code == 303, resp.text
    return app, client


# ------------------------------------------------------------------ 用户端账号夹具
#
# 用户端现在**有自己的账号体系**：页面与写入口都要求先登录，且每个用户只看得到
# 自己的计划。登录链路本身（注册/登录/退出、CSRF、Cookie 作用域）的助手放在
# ``tests/user_end.py``，用例直接从那里面取 —— 不在 conftest 里重复一份。


@pytest.fixture
def booking_user(make_client, web_settings, web_db):
    """``(client, db, settings, user_id)``：一个已登录的用户端客户端。

    页面、写入口与凭据绑定都挂在这个身份上；管理端不在这个夹具里。
    """
    from tests.user_end import create_user, login_user

    _app, client = make_client(settings=web_settings, db=web_db)
    user_id = create_user(web_db)
    login_user(client, web_db)
    return client, web_db, web_settings, user_id
