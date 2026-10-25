"""只读预检结果复用窗口（``HAUT_AUTH_CHECK_TTL_S``）的测试。

全部离线：``httpx.Client`` 的传输层被换成 ``MockTransport``，因此断言的是
**真实构造出来的客户端真正发出去了几次请求**，不是测试自己数出来的。

三条必须守住的边界：
1. 默认 0 = 完全关闭，一行缓存都不读写（"既有行为逐字节不变"的第一道硬保证）；
2. 只复用 ``VALID`` —— ``invalid`` / ``indeterminate`` 一律重新真实探测；
3. 键里含凭据指纹，换凭据天然失效；人点出来的「重新检测」不走缓存。
"""

from __future__ import annotations

import httpx
import pytest

from haut_seat.core.auth import AuthStatus
from haut_seat.services import auth_store, io

OBSERVED_AUTH_FAILURE_BODY = {
    "code": 401,
    "msg": "请求访问：/spacelink/getInfo，认证失败，无法访问系统资源",
}
#: 业务身份接口（``getInfo``）的成功响应：只有在缓存窗口这套用例里，
#: ``auth_precheck`` 判 ``VALID`` 的形状才是"带着 user/roles"。
OBSERVED_SUCCESS_BODY = {
    "code": 200,
    "msg": "成功",
    "user": {"userId": 1, "userName": "测试用户"},
    "roles": ["common"],
}

SENTINEL = "SENTINELTOKEN-._0003-abcdefghijklmnop"
OTHER_SENTINEL = "OTHERSENTINEL-._0004-zyxwvutsrqponmlk"


class OfflineHttp:
    """记录每一次真实发出的请求；响应体可控。"""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self._mode: tuple[str, object, int] = ("json", OBSERVED_SUCCESS_BODY, 200)

    def respond(self, body: dict, status: int = 200) -> None:
        self._mode = ("json", body, status)

    def raise_on_request(self, exc: Exception) -> None:
        self._mode = ("raise", exc, 0)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        kind, payload, status = self._mode
        if kind == "raise":
            raise payload  # type: ignore[misc]
        return httpx.Response(status, json=payload)


@pytest.fixture(autouse=True)
def _empty_cache():
    """进程级缓存必须在每个用例之间清空，否则测试互相污染。"""
    io.clear_precheck_cache()
    yield
    io.clear_precheck_cache()


@pytest.fixture
def offline_http(monkeypatch):
    http = OfflineHttp()
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(http._handle)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", fake_client)
    return http


@pytest.fixture
def settings_factory(web_settings, tmp_path):
    """指向临时快照的配置工厂。TTL 由每个用例显式给出。"""

    def _make(*, ttl: float, token: str = SENTINEL, **overrides):
        base = {
            "auth_source": "browser",
            "auth_snapshot_file": str(tmp_path / "auth" / "credentials.json"),
            "auth_profile_dir": str(tmp_path / "auth" / "browser-profile"),
            "auth_check_ttl_s": ttl,
        }
        base.update(overrides)
        return web_settings.model_copy(update=base)

    return _make


def write_snapshot(settings, *, token: str, captured_at: str | None = None) -> None:
    from datetime import datetime

    auth_store.save_snapshot(
        settings,
        cookie=f"Admin-Token={token}",
        authorization=f"Bearer {token}",
        now=datetime.strptime(captured_at, "%Y-%m-%d %H:%M:%S") if captured_at else None,
    )


# ================================================================== 默认关闭


def test_ttl_zero_reads_and_writes_no_cache(settings_factory, offline_http):
    """默认配置下必须与引入本功能之前**逐字节一致**：每次都真实探测。"""
    settings = settings_factory(ttl=0.0)
    write_snapshot(settings, token=SENTINEL)

    assert io.auth_precheck(settings, 16) .is_valid
    assert io.auth_precheck(settings, 16).is_valid

    assert len(offline_http.seen) == 2, "TTL=0 时不得复用任何结论"
    assert io._PRECHECK_CACHE == {}, "TTL=0 时连一行缓存都不该写"


def test_ttl_zero_does_not_reuse_an_entry_left_by_another_settings(
    settings_factory, offline_http
):
    """即使缓存里恰好有别的条目，TTL=0 也绝不读它。"""
    warm = settings_factory(ttl=600.0)
    write_snapshot(warm, token=SENTINEL)
    assert io.auth_precheck(warm, 16).is_valid
    assert len(offline_http.seen) == 1

    cold = settings_factory(ttl=0.0)
    assert io.auth_precheck(cold, 16).is_valid
    assert len(offline_http.seen) == 2, "TTL=0 必须重新真实探测"


# ================================================================== 窗口内复用


def test_valid_result_is_reused_within_the_window(settings_factory, offline_http):
    settings = settings_factory(ttl=120.0)
    write_snapshot(settings, token=SENTINEL)

    first = io.auth_precheck(settings, 16, "2026-10-19 10:30:00", "2026-10-19 10:31:00")
    second = io.auth_precheck(settings, 16, "2026-10-19 10:30:00", "2026-10-19 10:31:00")

    assert first.is_valid and second.is_valid
    assert second is first, "窗口内应直接返回同一个结论对象"
    assert len(offline_http.seen) == 1, "窗口内不该再发第二次请求"


def test_a_different_window_is_a_different_key(settings_factory, offline_http):
    """不同时段/区域的预检必须各自探测：结论是按窗口问出来的。"""
    settings = settings_factory(ttl=120.0)
    write_snapshot(settings, token=SENTINEL)

    io.auth_precheck(settings, 16, "2026-10-19 10:30:00", "2026-10-19 10:31:00")
    io.auth_precheck(settings, 17, "2026-10-19 10:30:00", "2026-10-19 10:31:00")
    io.auth_precheck(settings, 16, "2026-10-19 15:00:00", "2026-10-19 15:01:00")
    io.auth_precheck(settings, 16, "2026-10-19 10:30:00", "2026-10-19 10:32:00")

    assert len(offline_http.seen) == 4


def test_expired_entry_is_not_reused(settings_factory, offline_http):
    """窗口过期必须重新探测 —— 否则"上次有效"会被当成永远有效。"""
    settings = settings_factory(ttl=120.0)
    write_snapshot(settings, token=SENTINEL)

    io.auth_precheck(settings, 16)
    assert len(offline_http.seen) == 1

    # 把那条缓存的时间戳挪到窗口之外（不改系统时钟，只改这一条记录）
    key = next(iter(io._PRECHECK_CACHE))
    stored_at, check = io._PRECHECK_CACHE[key]
    io._PRECHECK_CACHE[key] = (stored_at - 1000.0, check)

    io.auth_precheck(settings, 16)
    assert len(offline_http.seen) == 2, "过期条目必须被丢弃并重新探测"
    assert len(io._PRECHECK_CACHE) == 1, "过期条目应被顺手清掉，不能无限堆积"


# ================================================================== 只复用 VALID


def test_invalid_result_is_never_cached(settings_factory, offline_http):
    """安全闸门不得因缓存变松：失效结论每次都必须重新探测。"""
    settings = settings_factory(ttl=600.0)
    write_snapshot(settings, token=SENTINEL)
    offline_http.respond(OBSERVED_AUTH_FAILURE_BODY)

    assert io.auth_precheck(settings, 16).status is AuthStatus.INVALID
    assert io.auth_precheck(settings, 16).status is AuthStatus.INVALID

    assert len(offline_http.seen) == 2, "invalid 绝不能被缓存"
    assert io._PRECHECK_CACHE == {}


def test_indeterminate_result_is_never_cached(settings_factory, offline_http):
    """一次网络抖动不该被记住整个窗口 —— 否则窗口内再也发现不了真实状态。"""
    settings = settings_factory(ttl=600.0)
    write_snapshot(settings, token=SENTINEL)
    offline_http.raise_on_request(httpx.ConnectError("测试注入的连接失败"))

    assert io.auth_precheck(settings, 16).status is AuthStatus.INDETERMINATE
    assert io._PRECHECK_CACHE == {}

    # 网络恢复后必须立刻能得到真实结论
    offline_http.respond(OBSERVED_SUCCESS_BODY)
    assert io.auth_precheck(settings, 16).is_valid


# ================================================================== 凭据变更即失效


def test_changing_the_snapshot_invalidates_the_cache(settings_factory, offline_http):
    """键里含凭据指纹 ⇒ 换一份凭据就是一把新钥匙，不需要额外的清缓存钩子。"""
    settings = settings_factory(ttl=600.0)
    write_snapshot(settings, token=SENTINEL)
    io.auth_precheck(settings, 16)
    assert len(offline_http.seen) == 1

    write_snapshot(settings, token=OTHER_SENTINEL, captured_at="2026-10-18 23:59:59")
    io.auth_precheck(settings, 16)
    assert len(offline_http.seen) == 2, "换了凭据必须重新预检"


def test_switching_to_env_credentials_invalidates_the_cache(
    settings_factory, offline_http, make_settings
):
    """同一个进程里从快照切回 .env：指纹不同，缓存自然失效。"""
    from pydantic import SecretStr

    snapshot_settings = settings_factory(ttl=600.0)
    write_snapshot(snapshot_settings, token=SENTINEL)
    io.auth_precheck(snapshot_settings, 16)
    assert len(offline_http.seen) == 1

    # model_copy 不做校验，所以 SecretStr 要自己包 —— 否则 _from_env 读不到值
    env_settings = make_settings(
        auth_source="env",
        auth_check_ttl_s=600.0,
        auth_snapshot_file=snapshot_settings.auth_snapshot_file,
        auth_profile_dir=snapshot_settings.auth_profile_dir,
        cookie=SecretStr(f"Admin-Token={OTHER_SENTINEL}"),
        authorization=SecretStr(f"Bearer {OTHER_SENTINEL}"),
    )
    io.auth_precheck(env_settings, 16)
    assert len(offline_http.seen) == 2


# ================================================================== 人点出来的检查


def test_manual_check_bypasses_the_cache(settings_factory, offline_http, web_db):
    """面板上的「重新检测登录状态」必须真的探测一次，不能拿缓存搪塞人。"""
    from haut_seat.services.auth_service import AuthService

    settings = settings_factory(ttl=600.0)
    web_db_ = web_db  # 用临时库
    write_snapshot(settings, token=SENTINEL)

    assert io.auth_precheck(settings, settings.regionid).is_valid
    assert len(offline_http.seen) == 1

    service = AuthService(web_db_, settings)
    check = service.check_now(source="manual")
    assert check.is_valid
    assert len(offline_http.seen) == 2, "人工重新检测必须绕过复用窗口"


def test_cache_is_per_process_and_never_written_to_the_database(
    settings_factory, offline_http, web_db
):
    """缓存是进程内字典，绝不落库 —— 重启后不会继承"上次有效"。"""
    settings = settings_factory(ttl=600.0)
    write_snapshot(settings, token=SENTINEL)
    io.auth_precheck(settings, 16)

    tables = {
        row["name"]
        for row in web_db.query_all("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "auth_credential_state" in tables
    # 库里没有任何"预检结论"表；缓存只在内存中
    assert not any("precheck" in t or "cache" in t for t in tables)
    assert io._PRECHECK_CACHE, "缓存确实存在于内存里"


def test_clear_precheck_cache_forces_a_real_probe(settings_factory, offline_http):
    settings = settings_factory(ttl=600.0)
    write_snapshot(settings, token=SENTINEL)
    io.auth_precheck(settings, 16)
    io.clear_precheck_cache()
    io.auth_precheck(settings, 16)
    assert len(offline_http.seen) == 2


# ================================================================== 守卫


def test_this_module_never_sends_real_requests():
    from pathlib import Path

    text = Path(__file__).read_text(encoding="utf-8")
    assert "MockTransport" in text
    real_host = "wslib" + ".haut" + ".edu.cn"
    assert real_host not in text