"""预约提交结果模型。

仅在取得真实网页请求证据、实现 addbooking 后用于返回分类。
在证据齐全前，本模块不包含任何参数/成功判据的猜测。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class BookingOutcome(str, Enum):
    SUCCESS = "success"          # 明确预约成功
    FAILED = "failed"            # 明确失败（服务端给出失败原因）
    UNKNOWN = "unknown"          # 结果不明确，无法确认是否成功
    AUTH_FAILED = "auth_failed"  # 认证失效（重定向/登录页/401/403）
    NEEDS_HUMAN = "needs_human"  # 需要人工验证（如验证码）或人工介入


@dataclass(frozen=True)
class BookingResult:
    outcome: BookingOutcome
    message: str = ""            # 脱敏后的说明，不含个人信息/凭据
    booking_id: int | None = None  # 提交成功后返回的 bookingid（仅 SUCCESS 时有值）
