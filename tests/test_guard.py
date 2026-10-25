"""安全守卫测试：暂停开关与真实预约禁用保护。"""

from __future__ import annotations

from haut_seat.core.guard import check_booking_allowed, is_paused


def test_disabled_when_flag_off(tmp_path):
    d = check_booking_allowed(False, tmp_path / "pause.flag")
    assert d.allowed is False
    assert "未开启" in d.reason


def test_disabled_when_paused(tmp_path):
    f = tmp_path / "pause.flag"
    f.write_text("", encoding="utf-8")
    d = check_booking_allowed(True, f)
    assert d.allowed is False
    assert "暂停" in d.reason


def test_allowed_when_flag_on_and_not_paused(tmp_path):
    d = check_booking_allowed(True, tmp_path / "pause.flag")
    assert d.allowed is True


def test_is_paused(tmp_path):
    f = tmp_path / "pause.flag"
    assert is_paused(f) is False
    f.write_text("", encoding="utf-8")
    assert is_paused(f) is True
