"""HTTP 客户端封装：超时、有界重试、最小请求间隔、敏感头脱敏、错误分类。

重试策略：仅对网络瞬断类错误（连接失败/超时）做有限次指数退避重试；
HTTP 状态码错误（4xx/5xx）、重定向、非 JSON 响应均不做盲重试，直接抛出，
避免高频请求与对错误响应的误判。任何异常都失败关闭，不会被当作空结果。

**最小请求间隔是进程级、按站点共享的**（``_SharedThrottle``）。原来它是每个客户端
实例各记一份「上次请求时刻」，于是一旦允许并发执行，出站速率就会随并发数线性放大
（3 个任务 = 3 倍频率），这与「不要盲目提高请求频率」直接冲突。现在同一站点上
不管有多少个客户端在并发，任意两次出站请求之间仍然至少隔 ``min_interval`` 秒。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

import httpx

from haut_seat.exceptions import (
    AuthenticationError,
    HttpError,
    NonJsonResponseError,
    RedirectError,
)

TRANSIENT_EXCEPTIONS = (httpx.TransportError, httpx.TimeoutException)

_SENSITIVE_HEADERS = {"authorization", "cookie", "token", "set-cookie"}

# 响应体长度上限：超过即拒绝解析与记录，避免超大内容进入内存/日志
MAX_RESPONSE_BODY_BYTES = 5 * 1024 * 1024


class _SharedThrottle:
    """按站点（base_url）共享的最小请求间隔。

    做法是**预留时间片**而不是「睡醒再看」：在锁内把"下一次允许发出的时刻"算出来
    并记下，然后在锁外睡到那个时刻。因此 N 个并发调用会被排成间隔严格为
    ``min_interval`` 的一列，既不会同时冲出去，也不会因为互相谦让而多睡。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_at: dict[str, float] = {}

    def reserve(self, key: str, interval: float) -> float:
        """登记本次请求并返回还需等待的秒数（调用方在锁外 sleep）。"""
        if interval <= 0:
            return 0.0
        now = time.monotonic()
        with self._lock:
            start = max(now, self._next_at.get(key, 0.0))
            self._next_at[key] = start + interval
        return max(0.0, start - now)

    def reset(self) -> None:
        with self._lock:
            self._next_at.clear()


_SHARED_THROTTLE = _SharedThrottle()


def reset_shared_throttle() -> None:
    """清空共享限速状态（测试与"换了站点/凭据"之后手工复位用）。"""
    _SHARED_THROTTLE.reset()


def redact_headers(headers: dict | None) -> dict:
    """将敏感请求头的值替换为 <redacted>，用于日志。"""
    if not headers:
        return {}
    return {
        k: ("<redacted>" if k.lower() in _SENSITIVE_HEADERS else v)
        for k, v in headers.items()
    }


class SeatHttpClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        max_retry: int = 2,
        min_interval: float = 1.0,
        backoff_base: float = 1.0,
        cookie: str | None = None,
        authorization: str | None = None,
        accept: str | None = None,
        accept_language: str | None = None,
        referer: str | None = None,
        user_agent: str = "haut-seat-helper/0.1 (personal read-only)",
        logger: logging.Logger | None = None,
        transport: httpx.BaseTransport | None = None,
        max_connections: int = 8,
        max_keepalive_connections: int = 4,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retry = max(0, max_retry)
        self.min_interval = max(0.0, min_interval)
        self.backoff_base = backoff_base
        # 会话凭据（来自用户本地 .env，绝不写入日志）
        self.cookie = cookie
        self.authorization = authorization
        self.logger = logger or logging.getLogger("haut_seat.http")
        default_headers = {"User-Agent": user_agent}
        if accept:
            default_headers["Accept"] = accept
        if accept_language:
            default_headers["Accept-Language"] = accept_language
        if referer:
            default_headers["Referer"] = referer
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            transport=transport,
            headers=default_headers,
            # 连接池**有界**：并发数再怎么涨，出站连接数也不会无限增长。
            limits=httpx.Limits(
                max_connections=max(1, int(max_connections)),
                max_keepalive_connections=max(0, int(max_keepalive_connections)),
            ),
        )

    def _throttle(self) -> None:
        """进程级共享限速：同一站点上任意两次出站请求至少隔 ``min_interval`` 秒。

        注意这是**出站速率的全局上限**，不是"每个任务各自限速" —— 正因为如此，
        增加并发不会提高对学校站点的请求频率。
        """
        wait = _SHARED_THROTTLE.reserve(self.base_url, self.min_interval)
        if wait > 0:
            time.sleep(wait)

    def _build_headers(self, headers: dict | None) -> dict:
        merged = dict(headers or {})
        lower = {k.lower() for k in merged}
        if self.cookie and "cookie" not in lower:
            merged["Cookie"] = self.cookie
        if self.authorization and "authorization" not in lower:
            merged["Authorization"] = self.authorization
        return merged

    def _parse_response(self, path: str, resp: httpx.Response) -> Any:
        status = resp.status_code
        if 300 <= status < 400:
            # 不跟随重定向：重定向通常指向统一身份认证/登录页
            raise RedirectError(f"收到 {status} 重定向（可能未登录），未跟随跳转")
        if status in (401, 403):
            # 与普通 4xx/5xx 区分：这表示凭据被拒，必须停止流程。
            # 措辞只复述服务器状态：403 按站点自身错误表是「当前操作没有权限」，
            # **不据此断言令牌过期**（需求要求保留 HTTP 状态与安全诊断信息）。
            self.logger.warning("%s 返回 HTTP %d（凭据被拒，未断言是否过期）", path, status)
            raise AuthenticationError(status)
        if status >= 400:
            self.logger.warning("GET %s 返回错误状态码 %d", path, status)
            raise HttpError(status)
        content_type = resp.headers.get("content-type", "")
        if "html" in content_type.lower():
            raise NonJsonResponseError(
                f"收到 HTML 响应（content-type={content_type!r}），可能是登录页"
            )
        body = self._read_bounded(resp)
        try:
            return json.loads(body)
        except ValueError as exc:
            raise NonJsonResponseError(f"响应不是有效 JSON: {exc}") from exc

    @staticmethod
    def _read_bounded(resp: httpx.Response, max_bytes: int = MAX_RESPONSE_BODY_BYTES) -> bytes:
        """有界读取响应体；超过 max_bytes 即拒绝，避免超大内容进入内存/日志。"""
        data = bytearray()
        for chunk in resp.iter_bytes(65536):
            data += chunk
            if len(data) > max_bytes:
                raise NonJsonResponseError(
                    f"响应体超过 {max_bytes} 字节，已拒绝解析与记录"
                )
        return bytes(data)

    def get(
        self,
        path: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        retry: bool = True,
    ) -> Any:
        """发送 GET 请求。

        仅对「幂等、只读、无副作用」的请求启用 retry（默认 True）。
        有副作用的请求（如预约提交）必须显式 retry=False，避免重复提交。
        """
        attempts = (self.max_retry + 1) if retry else 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            self._throttle()
            try:
                # 注意：不记录请求头（含凭据），只记录路径与查询参数
                self.logger.debug("GET %s params=%s", path, params)
                resp = self._client.get(
                    path, params=params, headers=self._build_headers(headers)
                )
                self.logger.info("GET %s -> %d", path, resp.status_code)
                return self._parse_response(path, resp)
            except TRANSIENT_EXCEPTIONS as exc:
                last_exc = exc
                self.logger.warning(
                    "GET %s 网络异常(第 %d/%d 次): %s",
                    path,
                    attempt + 1,
                    attempts,
                    exc,
                )
                if not retry or attempt >= attempts - 1:
                    break
                time.sleep(self.backoff_base * (2 ** attempt))
        raise last_exc or httpx.TransportError("request failed")

    def post(
        self,
        path: str,
        *,
        params: dict | None = None,
        json: Any = None,
        data: Any = None,
        headers: dict | None = None,
    ) -> Any:
        """发送 POST 请求（有副作用：单次尝试，绝不自动重试）。"""
        self._throttle()
        resp = self._client.post(
            path,
            params=params,
            json=json,
            data=data,
            headers=self._build_headers(headers),
        )
        self.logger.info("POST %s -> %d", path, resp.status_code)
        return self._parse_response(path, resp)

    def close(self) -> None:
        self._client.close()
