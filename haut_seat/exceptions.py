"""领域异常：区分查询失败的具体原因，避免把错误响应误判为空结果。"""

from __future__ import annotations


class SeatBookError(Exception):
    """座位查询/预约领域异常基类。"""


class RedirectError(SeatBookError):
    """收到重定向（3xx），通常意味着未登录或需要统一身份认证。"""


class HttpError(SeatBookError):
    """HTTP 4xx/5xx 错误。"""

    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"HTTP 错误 {status_code}")


class AuthenticationError(HttpError):
    """明确的认证失效（HTTP 401/403）。

    与普通 ``HttpError`` 区分开：认证失效必须立即停止流程并提示更新凭据，
    而其它 4xx/5xx 只是单次失败，不应被当作登录问题。
    """

    def __init__(self, status_code: int = 401):
        super().__init__(status_code)


class NonJsonResponseError(SeatBookError):
    """响应不是有效 JSON（如 HTML 登录页）。"""


class BusinessError(SeatBookError):
    """服务器返回的业务错误（HTTP 200 但业务 code 表示失败）。

    ``body`` 为脱敏后的响应体诊断文本（不含凭据/令牌/个人信息），
    用于在 msg 缺失或为空时保留定位线索。
    """

    def __init__(self, code: Any, msg: Any = None, body: str | None = None):
        self.code = code
        self.msg = msg
        self.body = body
        detail = f"业务错误 code={code!r}"
        if msg:
            detail += f", msg={msg!r}"
        if body:
            detail += f", 响应(脱敏)={body}"
        super().__init__(detail)


class ResponseShapeError(SeatBookError, ValueError):
    """JSON 结构不符合预期（缺字段）。同时是 ValueError 以兼容旧测试。"""
