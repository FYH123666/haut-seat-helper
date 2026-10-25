"""安全守卫：暂停开关、真实预约禁用保护。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class BookingDecision:
    allowed: bool
    reason: str


def is_paused(pause_file: str | Path) -> bool:
    """暂停旗标文件存在即视为已暂停。"""
    return Path(pause_file).exists()


def check_booking_allowed(
    enable_real_booking: bool, pause_file: str | Path
) -> BookingDecision:
    """真实预约是否放行：必须「开关开启 且 未暂停」。"""
    if not enable_real_booking:
        return BookingDecision(False, "真实预约开关未开启 (HAUT_ENABLE_REAL_BOOKING=false)")
    if is_paused(pause_file):
        return BookingDecision(False, f"暂停旗标存在: {pause_file}")
    return BookingDecision(True, "允许真实预约")
