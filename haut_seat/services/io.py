"""真实 IO 适配：HTTP 客户端构造、只读查询、认证预检、真实提交。

从 ``haut_seat.__main__`` 原样搬出，CLI 与 Web 服务层共用；
``__main__`` 仍以 ``_query_online`` / ``_auth_precheck`` / ``_submit_online``
等旧名字导入它们，因此既有代码与既有测试的打桩点完全不变。

安全约定：
- 认证值只从 ``auth_store.resolve_credentials`` 取出后交给 HTTP 客户端，
  绝不出现在日志、返回值或事件消息中；
- ``submit_online`` 单次调用、不自动重试（重试策略在运行器里）。

本模块是**唯一的凭据注入点**：所有出站请求的 Cookie / Authorization 都在
``build_http_client`` 里由凭据来源层解析而来，因此把"凭据从哪来"这件事收在一处，
不用改任何预约业务代码。

## 连接复用（``http_session``）

原来 ``query_online`` / ``submit_online`` / ``auth_precheck`` 都是"新建客户端 →
发一次请求 → 立刻关闭"，于是每一次请求都要重做 DNS / TCP / TLS，而预约窗口里
查询是高频动作。现在它们都经由 :func:`http_session` 借出一个**按
``(base_url, authorization 摘要, cookie 摘要)`` 缓存**的客户端，连接池因此能跨请求
复用（keep-alive）。

三条安全边界：

1. **缓存键含凭据摘要** ⇒ 换了凭据就是换一个客户端，绝不让不同账号/不同凭据
   共用同一个客户端的 Cookie 或 Authorization（authorization 与 cookie **都**在键里：
   多用户之后只按其中之一分桶会让两份不同的凭据共用同一个客户端）；
2. 缓存**有界**（LRU，条数由 ``HAUT_HTTP_CLIENT_CACHE_MAX`` 决定，默认 8），
   被挤出的客户端会被真正 ``close()`` —— 但**只在没有借用者时**：仍被借出
   （有线程正在用它发请求）的客户端会先转进「待关闭」列表，等最后一个借用者
   归还时才关。否则一次挤出会让正在飞行的请求抛出
   ``Cannot send a request, as the client has been closed``，
   把一个假故障记成真网络错误，并发任务之间互相拖挂；
3. 复用连接**不改变任何提交语义**：``submit_online`` 仍然单次尝试、绝不自动重试，
   「结果未知」的保护一分未减（复用只影响连接，不影响重试策略与判定）。
"""

from __future__ import annotations

import atexit
import hashlib
import logging
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from typing import Any

from haut_seat.core.auth import AuthCheck, AuthStatus, is_auth_business_code
from haut_seat.models.booking import BookingResult
from haut_seat.models.seat import LayoutQueryResult
from haut_seat.services.auth_store import (
    Credentials,
    CredentialsUnavailableError,
    resolve_credentials,
)

#: 预检结果缓存：键 → (放入时刻, 结论)。
#:
#: **进程级、不落库**：重启即空，绝不把"上次有效"当成跨进程的事实。
#: 只放 ``VALID`` 结论（见 ``auth_precheck``）—— 安全闸门不得因缓存变松。
_PRECHECK_CACHE: dict[tuple, tuple[float, AuthCheck]] = {}
_PRECHECK_LOCK = threading.Lock()

#: 可复用 HTTP 客户端缓存：键 → 客户端。LRU，有界。
_CLIENT_CACHE: "OrderedDict[tuple, Any]" = OrderedDict()
_CLIENT_CACHE_LOCK = threading.Lock()
#: 缓存条数的**兜底**默认值。实际上限取自 ``settings.http_client_cache_max``
#: （见 ``_client_cache_max``）；这个常量只在拿不到配置时使用，保持历史值不变。
_CLIENT_CACHE_MAX = 3

#: 每个客户端当前**借出**的次数（也就是有几个线程正拿它发请求）。
#:
#: 这是必须的：LRU 挤出时如果直接 ``close()``，被挤出的那个可能仍有别的线程正在
#: 使用它，下一次请求会抛 ``RuntimeError: Cannot send a request, as the client has
#: been closed`` —— 那不是网络故障，却会被记成网络故障，几个并发的预约任务因此
#: 互相拖挂。所以「有人在用」的客户端只从缓存里摘掉（进 ``_RETIRED``），
#: 等最后一个借用者归还时才真正关闭。
_CLIENT_INUSE: dict[tuple, int] = {}

#: 已被挤出缓存、但还有借用者的客户端。归还到 0 时关闭。
_CLIENT_RETIRED: list[Any] = []


def clear_precheck_cache() -> None:
    """清空预检缓存（测试与"我已更新凭据"之后手工刷新用）。"""
    with _PRECHECK_LOCK:
        _PRECHECK_CACHE.clear()


def _close_quietly(client: Any) -> None:
    try:
        client.close()
    except Exception:  # 关闭失败不能影响调用方
        pass


def close_shared_http_clients() -> None:
    """关闭并清空可复用客户端缓存（测试隔离、凭据变更后手工复位用）。

    仍被借出的客户端**不会**在这里被强行关闭（那会让正在飞行的请求报出假的网络
    错误），而是转进 ``_RETIRED``，由最后一个借用者归还时关闭。
    """
    with _CLIENT_CACHE_LOCK:
        to_close: list[Any] = list(_CLIENT_RETIRED)
        _CLIENT_RETIRED.clear()
        for key, client in list(_CLIENT_CACHE.items()):
            if _CLIENT_INUSE.get(key, 0) > 0:
                _CLIENT_RETIRED.append(client)
            else:
                to_close.append(client)
        _CLIENT_CACHE.clear()
    for client in to_close:
        _close_quietly(client)


atexit.register(close_shared_http_clients)


def _client_cache_key(settings, creds: Credentials) -> tuple:
    """缓存键。**含凭据指纹**是关键：换凭据即换客户端，不同账号绝不共享。

    多用户之后，键里同时包含 **authorization 与 cookie 两份**摘要。
    原来只有 authorization 的指纹，够用是因为当时凭据只有一个来源、两份值总是一起换；
    现在每个用户各有一份凭据，而 Cookie（``Admin-Token=…``）才是站点真正鉴权的东西
    —— 只按 authorization 分桶的话，两份"authorization 相同而 cookie 不同"的凭据
    会共用同一个客户端，也就共用了同一个连接池里的会话状态。摘要前缀不可逆，
    不含明文。
    """
    return (
        str(settings.base_url),
        creds.fingerprint(),
        _digest(creds.cookie),
        float(getattr(settings, "min_interval_s", 0.0) or 0.0),
        float(getattr(settings, "request_timeout_s", 0.0) or 0.0),
        int(getattr(settings, "max_retry", 0) or 0),
    )


def _digest(value: str | None) -> str:
    """单向摘要前缀（与凭据指纹同一规格）。空值返回空串。"""
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _client_cache_max(settings) -> int:
    """缓存条数上限（LRU）。取自配置，缺省仍是历史值 3。"""
    try:
        value = int(getattr(settings, "http_client_cache_max", _CLIENT_CACHE_MAX) or 0)
    except (TypeError, ValueError):
        return _CLIENT_CACHE_MAX
    return value if value > 0 else _CLIENT_CACHE_MAX


@contextmanager
def http_session(settings, *, credentials: Credentials | None = None):
    """借出一个可复用的 HTTP 客户端；退出时**不关闭**（连接留给下一次请求）。

    调用方不需要（也不应该）自己 ``close()``：客户端的生命周期由缓存管理，
    缓存在被挤出或显式 :func:`close_shared_http_clients` 时才真正关闭。

    借出期间会记一次引用计数，因此**被 LRU 挤出时不会在别的线程脚下把连接关掉**
    （见 :data:`_CLIENT_INUSE`）—— 并发任务之间不会因此互相报出假网络错误。
    """
    creds = credentials if credentials is not None else resolve_credentials(settings)
    key = _client_cache_key(settings, creds)
    client, evicted = _borrow_client(settings, creds, key)
    for old in evicted:
        _close_quietly(old)
    try:
        yield client
    finally:
        _return_client(key, client)


def _borrow_client(settings, creds: Credentials, key: tuple):
    """取客户端并记一次借用；返回 ``(客户端, 需要关闭的已挤出客户端)``。"""
    with _CLIENT_CACHE_LOCK:
        cached = _CLIENT_CACHE.get(key)
        if cached is not None:
            _CLIENT_CACHE.move_to_end(key)
            _CLIENT_INUSE[key] = _CLIENT_INUSE.get(key, 0) + 1
            return cached, []

    # 构造放在锁外：httpx.Client 的构造不便宜，不能让它把整个缓存锁住。
    built = build_http_client(settings, credentials=creds)
    evicted: list[Any] = []
    with _CLIENT_CACHE_LOCK:
        existing = _CLIENT_CACHE.get(key)
        if existing is not None:
            # 并发构造：只保留先放进去的那个。自己这个从没借出过，直接关掉不泄漏。
            _close_quietly(built)
            client = existing
            _CLIENT_CACHE.move_to_end(key)
        else:
            client = built
            _CLIENT_CACHE[key] = built
            limit = _client_cache_max(settings)
            while len(_CLIENT_CACHE) > limit:
                old_key, old = _CLIENT_CACHE.popitem(last=False)
                if _CLIENT_INUSE.get(old_key, 0) > 0:
                    _CLIENT_RETIRED.append(old)  # 还有人在用：等它归还时再关
                else:
                    evicted.append(old)
        _CLIENT_INUSE[key] = _CLIENT_INUSE.get(key, 0) + 1
    return client, evicted


def _return_client(key: tuple, client: Any) -> None:
    """归还一次借用；引用计数归零（且已被挤出缓存 / 缓存被清空）时才真正关闭。"""
    close_now = False
    with _CLIENT_CACHE_LOCK:
        left = _CLIENT_INUSE.get(key, 0) - 1
        if left > 0:
            _CLIENT_INUSE[key] = left
        else:
            _CLIENT_INUSE.pop(key, None)
            if _CLIENT_CACHE.get(key) is not client:
                # 已经不在缓存里了：要么被挤出，要么缓存被整体清空。此时无人再用。
                if client in _CLIENT_RETIRED:
                    _CLIENT_RETIRED.remove(client)
                close_now = True
    if close_now:
        _close_quietly(client)


def _precheck_cache_key(settings, creds: Credentials, regionid, start, end) -> tuple:
    """缓存键。

    **含凭据指纹**是关键：换一份快照（或改了 ``.env``）就是一把新钥匙，
    旧结论自然失效，不需要额外写"凭据变更时清缓存"的钩子。
    """
    return (
        str(settings.base_url),
        str(regionid),
        str(start or ""),
        str(end or ""),
        creds.fingerprint(),
    )


def secret_str(value: Any) -> str | None:
    """安全读取 SecretStr，返回字符串或 None。"""
    return value.get_secret_value() if value is not None else None


def build_http_client(settings, *, credentials: Credentials | None = None):
    """构造 HTTP 客户端。

    ``credentials`` 为空时自行按 ``HAUT_AUTH_SOURCE`` 解析，因此既有的
    ``build_http_client(settings)`` 单参调用点（以及针对它的测试打桩）全部不变。
    """
    from haut_seat.client.http import SeatHttpClient

    creds = credentials if credentials is not None else resolve_credentials(settings)
    return SeatHttpClient(
        settings.base_url,
        timeout=settings.request_timeout_s,
        max_retry=settings.max_retry,
        min_interval=settings.min_interval_s,
        cookie=creds.cookie,
        authorization=creds.authorization,
        accept=settings.http_accept or None,
        accept_language=settings.http_accept_language or None,
        referer=settings.http_referer or None,
        user_agent=settings.http_user_agent,
        max_connections=int(getattr(settings, "http_max_connections", 8)),
        max_keepalive_connections=int(
            getattr(settings, "http_max_keepalive_connections", 4)
        ),
    )


def query_online(settings, regionid, start, end, page_num, page_size) -> LayoutQueryResult:
    """执行一次只读在线查询。"""
    from haut_seat.client.seatbook import SeatBookClient

    with http_session(settings) as client:
        return SeatBookClient(client).query_layout(
            regionid=regionid,
            starttime=start,
            endtime=end,
            page_num=page_num,
            page_size=page_size,
        )


def auth_precheck(settings, regionid, start=None, end=None) -> AuthCheck:
    """正式预约流程开始前的登录预检（只读，绝不提交预约）。

    **判据顺序（2026-10-23 实测更正）**：

    1. 先用业务身份接口 ``GET /stage-api/getInfo`` 验证凭据 —— 站点自己的路由守卫在
       登录后调用的就是它，实测能区分「凭据被接受」（code=200 + user/roles）与
       「未被接受」（code=401）。**只有它返回成功时才会显示「凭据已被业务接口接受」**，
       而且这条路径只打**一个**请求（交互式验证要求它是快的；随后每次预约运行本来
       还会做真正的座位查询）。
    2. 只有身份接口**没给出确定结论**时（网络/服务端/结构异常），才补一次只读座位
       查询作为第二意见。它自己**不能**验证凭据（匿名同样放行、闭馆后恒返回业务
       code=1111），只在「服务器拒绝了 Authorization 头」时把结论收紧为 INVALID，
       其余情况不改变结论。身份接口一旦判出 VALID/INVALID 就不再发它 —— 结论不会变，
       白白多打一个请求。

    ``HAUT_AUTH_CHECK_TTL_S``（默认 0 = 关闭）> 0 时，同一份凭据在同一窗口内
    的重复预检会直接复用上次的 ``VALID`` 结论，避免"多个计划 / 预检 + 运行器"
    对同一凭据反复做昂贵探测。三条边界：

    1. **只复用 VALID**。``invalid`` / ``indeterminate`` 一律重新真实探测 ——
       缓存绝不能让安全闸门变松，也绝不能让一次网络抖动被记住一分钟。
    2. **默认 0 时一行缓存都不会读写**，行为与引入本功能之前逐字节一致。
    3. 键里含凭据指纹，换一份凭据天然失效；也可以显式调
       :func:`clear_precheck_cache`。
    """
    from haut_seat.client.seatbook import SeatBookClient

    try:
        creds = resolve_credentials(settings)
    except CredentialsUnavailableError as exc:
        return AuthCheck(AuthStatus.INVALID, str(exc))

    ttl = max(0.0, float(getattr(settings, "auth_check_ttl_s", 0.0) or 0.0))
    key = _precheck_cache_key(settings, creds, regionid, start, end) if ttl > 0 else None
    if key is not None:
        cached = _precheck_cache_lookup(key, ttl)
        if cached is not None:
            logging.getLogger("haut_seat.auth").debug(
                "复用 %gs 内的预检结论（凭据指纹 %s，未发起网络请求）",
                ttl, creds.fingerprint(),
            )
            return cached

    expect = bool(creds.authorization)
    with http_session(settings, credentials=creds) as client:
        book = SeatBookClient(client)
        identity = book.check_identity(expect_authenticated=expect)
        if identity.is_valid or identity.is_invalid:
            # 身份接口已经给出**确定**结论（实测：它只区分"被接受"与"未被接受"），
            # 就不再补发座位查询：那个接口无法验证凭据（匿名同样放行），
            # 既改不动这个结论（见 _combine_precheck 的第一条规则），
            # 又会让"明确失败"这条路径平白多打一个请求。
            check = identity
        else:
            seats = book.check_session(
                regionid, start, end, expect_authenticated=expect
            )
            check = _combine_precheck(identity, seats)

    if key is not None and check.is_valid:
        _precheck_cache_store(key, check)
    return check


def _combine_precheck(identity: AuthCheck, seats: AuthCheck) -> AuthCheck:
    """合并「身份接口」与「座位查询」两个只读结论（纯函数）。

    只在身份接口**没有**给出确定结论（``VALID``/``INVALID``）时才会被调用。规则：

    * 任一为 ``INVALID`` ⇒ ``INVALID``（两处都只有实测过的「凭据未被接受」才会这样）；
    * 身份接口 ``VALID`` ⇒ ``VALID``（保持不变；座位查询只是附带佐证）；
    * 其余 ⇒ ``INDETERMINATE``，消息里同时保留两条原始说明与状态码（便于排查，
      不泄露任何凭据值）。
    """
    if identity.is_invalid or seats.is_invalid:
        first = identity if identity.is_invalid else seats
        other = seats if identity.is_invalid else identity
        message = first.message
        if other.status is not AuthStatus.INDETERMINATE:
            message = f"{message}；另一只读探测：{other.message}"
        return AuthCheck(
            AuthStatus.INVALID,
            message,
            http_status=first.http_status if first.http_status is not None else other.http_status,
            business_code=first.business_code,
            seat_count=seats.seat_count,
        )

    if identity.is_valid:
        message = identity.message
        if seats.seat_count is not None:
            message = f"{message}；同一凭据下的只读座位查询返回 {seats.seat_count} 个座位"
        return AuthCheck(
            AuthStatus.VALID,
            message,
            http_status=identity.http_status,
            business_code=identity.business_code,
            seat_count=seats.seat_count,
        )

    return AuthCheck(
        AuthStatus.INDETERMINATE,
        f"{identity.message}；座位查询也无法确认：{seats.message}",
        http_status=(
            identity.http_status if identity.http_status is not None else seats.http_status
        ),
        business_code=identity.business_code,
        seat_count=seats.seat_count,
    )


def _precheck_cache_lookup(key: tuple, ttl: float) -> AuthCheck | None:
    with _PRECHECK_LOCK:
        entry = _PRECHECK_CACHE.get(key)
    if entry is None:
        return None
    stored_at, check = entry
    if (time.monotonic() - stored_at) >= ttl:
        with _PRECHECK_LOCK:
            _PRECHECK_CACHE.pop(key, None)
        return None
    return check


def _precheck_cache_store(key: tuple, check: AuthCheck) -> None:
    with _PRECHECK_LOCK:
        _PRECHECK_CACHE[key] = (time.monotonic(), check)


def auth_stop_reason(exc: Exception) -> str | None:
    """把查询/提交过程中的异常映射为「认证失效」说明；非认证问题返回 None。"""
    from haut_seat.exceptions import AuthenticationError, BusinessError

    if isinstance(exc, AuthenticationError):
        return f"服务器拒绝访问（HTTP {exc.status_code}）"
    if isinstance(exc, BusinessError) and is_auth_business_code(exc.code):
        return f"服务器返回认证失败 code={exc.code}"
    return None


def submit_online(settings, seat, start, end) -> BookingResult:
    """执行一次真实预约提交（单次、不自动重试）。

    复用连接**不改变**这里的语义：``submit_booking`` 客户端侧仍然是单次尝试，
    绝无自动重发；传输层异常照旧由运行器判为「结果未知」而不是重试。
    """
    from haut_seat.client.seatbook import SeatBookClient

    channel = settings.booking_channel
    with http_session(settings) as client:
        return SeatBookClient(client).submit_booking(seat, start, end, channel)