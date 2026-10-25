"""凭据来源接到真实 IO 收口点的测试。

全部使用 ``httpx.MockTransport``，**不发送任何真实请求，更不发送任何预约请求**。

做法：把 ``httpx.Client`` 换成注入了 MockTransport 的版本，于是
``io.build_http_client`` 与 ``SeatHttpClient`` **都是真货**，唯独传输层被拦下 ——
这样断言的是真实发出去的请求头，而不是测试自己拼出来的假客户端。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from haut_seat.core.auth import AUTH_403_NOTE, AuthStatus
from haut_seat.services import auth_store, io
from haut_seat.services.auth_store import CredentialsUnavailableError
from haut_seat.services.auth_service import AuthService

OBSERVED_AUTH_FAILURE_BODY = {
    "code": 401,
    "msg": "请求访问：/spacelink/error，认证失败，无法访问系统资源",
}
#: 只读座位查询的响应（``layout/query``）。**它不是凭据判据**：实测匿名请求同样
#: 拿得到座位列表，因此它的存在只说明「这次查询被受理了」。
OBSERVED_SEATS_BODY = {"code": 200, "msg": "成功", "layout": [], "seatList": [{"id": 1}]}
#: 业务身份接口（``getInfo``）的成功响应 —— 这是**唯一**能让预检判 ``VALID`` 的形状
#: （站点自己的路由守卫登录后读的就是 ``user`` / ``roles``）。
OBSERVED_IDENTITY_BODY = {
    "code": 200,
    "msg": "成功",
    "user": {"userId": 1, "userName": "测试用户"},
    "roles": ["common"],
}

# 哨兵：形状像真令牌，但显然是测试专用值
SENTINEL = "SENTINELTOKEN-._0001-abcdefghijklmnop"
ENV_SENTINEL = "ENVSENTINEL-._0002-zyxwvutsrqponmlk"


class OfflineHttp:
    """把真实构造出来的 ``httpx.Client`` 的传输层换成 MockTransport，并记录请求。

    ``seen`` 由本对象持有：测试只要拿 ``offline_http.seen`` 就能看到
    **真实构造出来的那个客户端真正发出去的东西**。
    """

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self._mode: tuple[str, object, int] = ("json", OBSERVED_IDENTITY_BODY, 200)

    def respond(self, body: dict, status: int = 200) -> None:
        """让后续请求固定得到这个响应体。"""
        self._mode = ("json", body, status)

    def raise_on_request(self, exc: Exception) -> None:
        """让后续请求在传输层直接抛异常（模拟网络故障）。"""
        self._mode = ("raise", exc, 0)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        kind, payload, status = self._mode
        if kind == "raise":
            raise payload  # type: ignore[misc]
        return httpx.Response(status, json=payload)


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
def auth_settings(make_settings, tmp_path):
    def _make(**overrides):
        base = {
            "auth_snapshot_file": str(tmp_path / "auth" / "credentials.json"),
            "auth_profile_dir": str(tmp_path / "auth" / "browser-profile"),
        }
        base.update(overrides)
        return make_settings(**base)

    return _make


def write_snapshot(settings) -> None:
    auth_store.save_snapshot(
        settings,
        cookie=f"Admin-Token={SENTINEL}",
        authorization=f"Bearer {SENTINEL}",
    )


# ---------------------------------------------------------------- 收口点：注入


def test_build_http_client_default_uses_env_credentials(auth_settings, monkeypatch):
    """既有单参调用点必须仍然走 .env —— 这是"既有部署零变化"的直接证明。"""
    monkeypatch.setattr(auth_store, "env_file_mtime", lambda settings: None)
    settings = auth_settings(cookie=f"Admin-Token={ENV_SENTINEL}",
                             authorization=f"Bearer {ENV_SENTINEL}")
    client = io.build_http_client(settings)
    try:
        assert client.cookie == f"Admin-Token={ENV_SENTINEL}"
        assert client.authorization == f"Bearer {ENV_SENTINEL}"
    finally:
        client.close()


def test_build_http_client_accepts_injected_credentials(auth_settings):
    settings = auth_settings()
    creds = auth_store.Credentials("C", "A", "snapshot", "2026-10-18 12:00:00")
    client = io.build_http_client(settings, credentials=creds)
    try:
        assert client.cookie == "C"
        assert client.authorization == "A"
    finally:
        client.close()


def test_build_http_client_propagates_missing_snapshot_in_browser_mode(auth_settings):
    settings = auth_settings(auth_source="browser")
    with pytest.raises(CredentialsUnavailableError):
        io.build_http_client(settings)


# ---------------------------------------------------------------- 发出去的请求头


def test_precheck_sends_the_snapshot_credentials(auth_settings, offline_http):
    """browser 模式下，真正发出去的请求头必须就是快照里的那对值。

    并且**只打一个请求**：凭据验证走业务身份接口（``getInfo``），它一旦给出
    「被接受」就不再补发座位查询。
    """
    settings = auth_settings(auth_source="browser")
    write_snapshot(settings)
    offline_http.respond(OBSERVED_IDENTITY_BODY)

    check = io.auth_precheck(settings, regionid=16)

    assert check.status is AuthStatus.VALID
    assert len(offline_http.seen) == 1
    assert offline_http.seen[0].url.path == "/stage-api/getInfo"
    assert offline_http.seen[0].headers["Authorization"] == f"Bearer {SENTINEL}"
    assert offline_http.seen[0].headers["Cookie"] == f"Admin-Token={SENTINEL}"


def test_precheck_sends_the_env_credentials_in_env_mode(auth_settings, offline_http):
    settings = auth_settings(auth_source="env", cookie=f"Admin-Token={ENV_SENTINEL}",
                             authorization=f"Bearer {ENV_SENTINEL}")
    offline_http.respond(OBSERVED_IDENTITY_BODY)

    io.auth_precheck(settings, regionid=16)

    assert offline_http.seen[0].headers["Authorization"] == f"Bearer {ENV_SENTINEL}"
    assert offline_http.seen[0].headers["Cookie"] == f"Admin-Token={ENV_SENTINEL}"


def test_precheck_requests_never_touch_addbooking(auth_settings, offline_http):
    """守卫：预检只走只读探测（身份接口 / 座位查询），任何情况下都不碰提交接口。"""
    settings = auth_settings(auth_source="browser")
    write_snapshot(settings)
    offline_http.respond(OBSERVED_IDENTITY_BODY)

    io.auth_precheck(settings, regionid=16)

    assert offline_http.seen, "应当发出一次只读探测"
    for request in offline_http.seen:
        assert "addbooking" not in str(request.url)


def test_precheck_query_also_uses_the_snapshot(auth_settings, offline_http):
    """query_online 零改动，但自动跟着走凭据来源层。"""
    settings = auth_settings(auth_source="browser")
    write_snapshot(settings)
    offline_http.respond(OBSERVED_SEATS_BODY)

    io.query_online(settings, 16, "2026-10-18 08:00:00", "2026-10-18 22:00:00", 1, 10)

    assert offline_http.seen[0].headers["Authorization"] == f"Bearer {SENTINEL}"


# ---------------------------------------------------------------- 三态判定


def test_precheck_valid_on_success_body(auth_settings, offline_http):
    """**只有**身份接口带着 user/roles 时才算验证通过（需求里的"pass业务接口验证"）。"""
    settings = auth_settings(auth_source="browser")
    write_snapshot(settings)
    offline_http.respond(OBSERVED_IDENTITY_BODY)
    check = io.auth_precheck(settings, regionid=16)
    assert check.status is AuthStatus.VALID
    assert "凭据已被业务接口接受" in check.message
    assert "user(" in check.message and "roles(" in check.message
    # 只报字段名与条数，绝不回显任何字段值（可能含姓名/学号）
    assert "测试用户" not in check.message
    assert SENTINEL not in check.message


def test_precheck_seat_list_alone_is_not_a_credential_verdict(auth_settings, offline_http):
    """座位列表**不构成**"凭据可用"的证据（实测匿名请求同样拿得到座位）。"""
    settings = auth_settings(auth_source="browser")
    write_snapshot(settings)
    offline_http.respond(OBSERVED_SEATS_BODY)

    check = io.auth_precheck(settings, regionid=16)

    assert check.status is AuthStatus.INDETERMINATE
    assert check.seat_count == 1
    assert check.is_valid is False


def test_precheck_invalid_on_business_401(auth_settings, offline_http):
    """过期/失效的典型形态：HTTP 200 + 业务 code=401。"""
    settings = auth_settings(auth_source="browser")
    write_snapshot(settings)
    offline_http.respond(OBSERVED_AUTH_FAILURE_BODY)
    check = io.auth_precheck(settings, regionid=16)
    assert check.status is AuthStatus.INVALID
    assert check.business_code == 401


def test_precheck_invalid_on_http_401(auth_settings, offline_http):
    settings = auth_settings(auth_source="browser")
    write_snapshot(settings)
    offline_http.respond({"msg": "unauthorized"}, status=401)
    assert io.auth_precheck(settings, regionid=16).status is AuthStatus.INVALID


def test_precheck_on_http_403_pins_behaviour_for_the_403_symptom(auth_settings, offline_http):
    """HTTP 403（本轮要排查的现象）在**完整的**预检路径上的行为：

    * 结论：``INVALID``（保守停止，不放任盲提交），HTTP 状态**原样保留**；
    * 措辞里**不出现**「令牌过期 / 认证已失效」这类断言 —— 403 也可能是网关或
      权限问题，站点自己的前端错误表把 403 记为「当前操作没有权限」；
    * 只发一次请求（403 不重试，也不再补发座位查询）。

    ``INVALID`` 在这里的含义是「停止流程」，不是「判定令牌死了」：要区分这两者，
    得看 403 到底是怎么来的（响应体给一段网关 HTML 也一样得出这个结论）。
    """
    settings = auth_settings(auth_source="browser")
    write_snapshot(settings)
    offline_http.respond({"msg": "网关拒绝"}, status=403)

    check = io.auth_precheck(settings, regionid=16)

    assert check.status is AuthStatus.INVALID
    assert check.http_status == 403
    assert check.business_code is None      # 403 的响应体根本没被解析
    assert "403" in check.message
    assert AUTH_403_NOTE in check.message
    # 措辞必须**明说不断言过期**，而且不能出现任何断言式的失效结论
    assert "不据此断言令牌过期" in check.message
    for claim in ("认证已失效", "登录已失效"):
        assert claim not in check.message
    assert len(offline_http.seen) == 1      # 单次；403 不做重试，也不再补发座位查询


def test_precheck_http_403_never_writes_the_precheck_cache(auth_settings, offline_http):
    """403 不会被记进「复用窗口」缓存：TTL>0 时下一次预检仍会真的去探测。"""
    settings = auth_settings(auth_source="browser", auth_check_ttl_s=600.0)
    write_snapshot(settings)
    offline_http.respond({"msg": "网关拒绝"}, status=403)

    assert io.auth_precheck(settings, regionid=16).status is AuthStatus.INVALID
    assert io.auth_precheck(settings, regionid=16).status is AuthStatus.INVALID
    assert len(offline_http.seen) == 2      # 没有被缓存挡住（失效/不确定一律重新探测）


def test_precheck_indeterminate_on_network_error(auth_settings, offline_http):
    """网络异常绝不能误报成"登录失效"。"""
    settings = auth_settings(auth_source="browser")
    write_snapshot(settings)
    offline_http.raise_on_request(httpx.ConnectTimeout("模拟超时"))

    check = io.auth_precheck(settings, regionid=16)
    assert check.status is AuthStatus.INDETERMINATE


def test_precheck_indeterminate_when_no_authorization_configured(auth_settings, offline_http):
    """既有语义原样保留：没配 Authorization 时，匿名也能查成功 ⇒ 不能声称登录有效。"""
    settings = auth_settings(auth_source="env")  # 既没有 .env 凭据，也没有快照
    offline_http.respond(OBSERVED_SEATS_BODY)

    check = io.auth_precheck(settings, regionid=16)

    assert check.status is AuthStatus.INDETERMINATE
    assert len(offline_http.seen) == 1  # 仍然发了探测请求（旧行为不变）
    assert "Authorization" not in offline_http.seen[0].headers


# ---------------------------------------------------------------- browser 模式硬阻断


def test_browser_mode_missing_snapshot_is_invalid_and_makes_zero_http_calls(
    auth_settings, offline_http
):
    """关键行为：确定没有凭据时**一次请求都不发**，直接判失效以阻断提交。"""
    settings = auth_settings(auth_source="browser",
                             cookie=f"Admin-Token={ENV_SENTINEL}",
                             authorization=f"Bearer {ENV_SENTINEL}")
    offline_http.respond(OBSERVED_IDENTITY_BODY)

    check = io.auth_precheck(settings, regionid=16)

    assert check.status is AuthStatus.INVALID
    assert offline_http.seen == [], "没有可用凭据时不应该发出任何请求"


def test_browser_mode_missing_snapshot_message_leaks_nothing(auth_settings, offline_http):
    settings = auth_settings(auth_source="browser", cookie=f"Admin-Token={ENV_SENTINEL}",
                             authorization=f"Bearer {ENV_SENTINEL}")
    offline_http.respond(OBSERVED_IDENTITY_BODY)

    message = io.auth_precheck(settings, regionid=16).message

    assert "browser" in message          # 说明原因
    assert "auth-login" in message       # 给出补救方式
    assert ENV_SENTINEL not in message   # 绝不回显 .env 里的旧凭据
    assert "Bearer " not in message


def test_auto_mode_without_any_credential_keeps_legacy_behaviour(auth_settings, offline_http):
    """auto 且两边都没有凭据时仍按旧语义走（照旧发一次匿名探测）。"""
    settings = auth_settings(auth_source="auto")
    offline_http.respond(OBSERVED_SEATS_BODY)
    check = io.auth_precheck(settings, regionid=16)
    assert check.status is AuthStatus.INDETERMINATE
    assert len(offline_http.seen) == 1


# ---------------------------------------------------------------- AuthService


def test_auth_service_reports_browser_missing_snapshot_as_blocking(auth_settings, web_db):
    settings = auth_settings(auth_source="browser")
    service = AuthService(web_db, settings)
    view = service.current()

    assert view.blocks_submissions is True
    assert service.credentials_unavailable() is True
    assert view.credential_source == "none"
    assert view.credential_source_label == "不可用"
    assert view.snapshot_present is False
    assert "auth-login" in view.credential_blocked_reason


def test_auth_service_reports_snapshot_source_without_leaking(auth_settings, web_db):
    settings = auth_settings(auth_source="browser")
    write_snapshot(settings)
    view = AuthService(web_db, settings).current()

    assert view.credential_source == "snapshot"
    assert view.credential_source_label == "浏览器会话快照"
    assert view.snapshot_present is True
    assert view.snapshot_captured_at
    assert view.snapshot_fingerprint == auth_store.fingerprint(f"Bearer {SENTINEL}")
    # 指纹可见、值不可见
    assert SENTINEL not in view.snapshot_fingerprint
    assert SENTINEL not in str(view)
    assert f"Bearer {SENTINEL}" not in str(view)


def test_auth_service_reports_env_source(auth_settings, web_db, monkeypatch):
    monkeypatch.setattr(auth_store, "env_file_mtime", lambda settings: None)
    settings = auth_settings(auth_source="auto",
                             cookie=f"Admin-Token={ENV_SENTINEL}",
                             authorization=f"Bearer {ENV_SENTINEL}")
    view = AuthService(web_db, settings).current()

    assert view.credential_source == "env"
    assert view.credential_source_label == ".env 手动配置"
    assert view.env_configured is True
    assert view.blocks_submissions is False
    assert view.credential_blocked_reason == ""


def test_auth_service_reports_none_source_when_nothing_configured(auth_settings, web_db):
    view = AuthService(web_db, auth_settings(auth_source="env")).current()
    assert view.credential_source == "none"
    assert view.credential_source_label == "未配置"
    assert view.env_configured is False
    # 旧行为：没配凭据是"不确定"，不是"失效"，因此不阻断
    assert view.blocks_submissions is False


def test_auth_service_env_mode_ignores_snapshot_presence(auth_settings, web_db, monkeypatch):
    """env 模式下即使存在快照也不该被采用（页面显示的就是真正会用的那份）。"""
    monkeypatch.setattr(auth_store, "env_file_mtime", lambda settings: None)
    settings = auth_settings(auth_source="env", cookie="ENVCOOKIE", authorization="ENVAUTH")
    write_snapshot(settings)
    view = AuthService(web_db, settings).current()
    assert view.credential_source == "env"
    assert view.snapshot_present is False


def test_check_now_records_invalid_for_browser_mode_without_snapshot(
    auth_settings, web_db, offline_http
):
    """端到端（离线）：无快照 + browser 模式 → 落库 invalid → 阻断一切提交。"""
    settings = auth_settings(auth_source="browser")
    offline_http.respond(OBSERVED_IDENTITY_BODY)
    service = AuthService(web_db, settings)

    check = service.check_now(source="manual")

    assert check.status is AuthStatus.INVALID
    assert offline_http.seen == []
    assert service.blocks_submissions() is True
    assert AuthService(web_db, settings).current().status == AuthStatus.INVALID.value


def test_check_now_uses_snapshot_and_records_valid(auth_settings, web_db, offline_http):
    settings = auth_settings(auth_source="browser")
    write_snapshot(settings)
    offline_http.respond(OBSERVED_IDENTITY_BODY)
    service = AuthService(web_db, settings)

    check = service.check_now(source="manual")

    assert check.status is AuthStatus.VALID
    assert offline_http.seen[0].headers["Authorization"] == f"Bearer {SENTINEL}"
    assert service.blocks_submissions() is False


def test_auth_service_accepts_injected_credential_provider(web_db, make_settings):
    from haut_seat.services.auth_store import Credentials

    settings = make_settings(auth_source="browser")
    service = AuthService(
        web_db, settings, credential_provider=lambda s: Credentials("c", "a", "snapshot")
    )
    view = service.current()
    assert view.credential_source == "snapshot"
    assert view.blocks_submissions is False


def test_credential_provider_failure_does_not_block(web_db, make_settings):
    """凭据解析本身炸了不该悄悄变成"阻断"（保持既有结论：不确定不阻断）。"""
    settings = make_settings(auth_source="auto")

    def boom(_settings):
        raise RuntimeError("boom")

    service = AuthService(web_db, settings, credential_provider=boom)
    assert service.credentials_unavailable() is False
    assert service.current().blocks_submissions is False
    assert "RuntimeError" in service.credentials_unavailable_reason()


# ---------------------------------------------------------------- 守卫


def test_this_module_only_uses_mock_transport():
    text = Path(__file__).read_text(encoding="utf-8")
    assert "MockTransport" in text
    real_host = "wslib" + ".haut" + ".edu.cn"
    assert real_host not in text
    submit_path = "add" + "booking"
    # 唯一允许提到该路径的地方是"断言请求不含它"的那条守卫用例
    assert text.count(f'"{submit_path}"') == 1