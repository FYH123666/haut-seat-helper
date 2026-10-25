"""通知服务测试（接口 + 设置模型，SMTP 尚未实现）。

要点：
- 未实现 SMTP 时只能是 ``skipped``，**绝不能出现 ``sent``**（不能假装发过邮件）；
- 去重、重试、异常兜底都覆盖；
- 通知记录里不得出现口令、Authorization、完整 Cookie 等凭证。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from haut_seat.services.notification_service import (
    DEFAULT_DEDUP_WINDOW_MINUTES,
    DEFAULT_EVENT_FLAGS,
    SMTP_NOT_IMPLEMENTED_REASON,
    NotificationService,
    NotifyEvent,
    build_request,
)
from haut_seat.web import repository as repo

NOW = datetime(2026, 10, 16, 9, 0, 0)


@pytest.fixture
def service(web_db):
    return NotificationService(web_db, clock=lambda: NOW)


def enable(db, **flags):
    repo.set_notify_setting(
        db,
        {
            "enabled": True,
            "channel": "email",
            "smtp_host": "smtp.example.com",
            "smtp_port": "465",
            "smtp_user": "me@example.com",
            "smtp_password_env": "HAUT_SMTP_PASSWORD",
            "mail_from": "me@example.com",
            "mail_to": "me@example.com",
            "use_tls": True,
            "event_flags": flags,
        },
        now=NOW,
    )


def rows(db):
    return repo.list_notify_events(db, limit=100)


# ------------------------------------------------------------------ 未启用 / 未选定


def test_disabled_notifications_are_skipped_not_sent(service, web_db):
    result = service.notify_booking_success(plan_id=1, run_id=1, detail="预约成功")
    assert result["status"] == "skipped"
    assert rows(web_db)[0]["status"] == "skipped"
    assert "通知功能未启用" in rows(web_db)[0]["last_error"]


def test_event_flag_off_is_skipped(service, web_db):
    enable(web_db, booking_success=False)
    result = service.notify_booking_success(plan_id=1, run_id=1)
    assert result["status"] == "skipped"
    assert "未在设置中启用" in rows(web_db)[0]["last_error"]


def test_event_flag_on_still_reports_smtp_not_implemented(service, web_db):
    """开关打开也不等于「已发出邮件」：SMTP 未实现，只能是 skipped。"""
    enable(web_db, booking_success=True)
    result = service.notify_booking_success(plan_id=1, run_id=1, detail="预约成功")
    assert result["status"] == "skipped"
    assert result["status"] != "sent"
    assert "未实现" in rows(web_db)[0]["last_error"]
    assert rows(web_db)[0]["attempts"] == 1


def test_default_flags_match_the_documented_set():
    assert set(DEFAULT_EVENT_FLAGS) == {e.value for e in NotifyEvent}
    assert DEFAULT_EVENT_FLAGS[NotifyEvent.BOOKING_SUCCESS.value] is True
    assert DEFAULT_EVENT_FLAGS[NotifyEvent.NO_SEAT.value] is False


# ------------------------------------------------------------------ 真正发送（替身）


def test_successful_delivery_is_recorded_as_sent(service, web_db):
    enable(web_db, booking_success=True)
    service._deliver = lambda request, setting: None  # 替身：假装 SMTP 已实现
    result = service.notify_booking_success(plan_id=1, run_id=1, detail="预约成功")
    assert result["status"] == "sent"
    row = rows(web_db)[0]
    assert row["status"] == "sent"
    assert row["attempts"] == 1
    assert row["event"] == NotifyEvent.BOOKING_SUCCESS.value


def test_failure_is_retried_then_recorded_as_failed(service, web_db):
    enable(web_db, booking_success=True)
    calls = {"n": 0}

    def boom(request, setting):
        calls["n"] += 1
        raise OSError("connection refused")

    service._deliver = boom
    result = service.notify_booking_success(plan_id=1, run_id=1)
    assert result["status"] == "failed"
    assert calls["n"] == 3, "必须按约定的次数重试"
    row = rows(web_db)[0]
    assert row["status"] == "failed"
    assert row["attempts"] == 3
    assert "OSError" in row["last_error"]


def test_failure_message_is_redacted(service, web_db):
    enable(web_db, booking_success=True)

    def boom(request, setting):
        raise OSError("Authorization: Bearer supersecrettokenvalue")

    service._deliver = boom
    service.notify_booking_success(plan_id=1, run_id=1)
    assert "supersecrettokenvalue" not in str(rows(web_db)[0])


def test_second_failure_mode_is_not_retried_forever(service, web_db):
    """重试次数有上界：不能因为发信失败而无限循环。"""
    enable(web_db, booking_success=True)
    service._deliver = lambda request, setting: (_ for _ in ()).throw(TimeoutError("t"))
    service.notify_booking_success(plan_id=1, run_id=1)
    assert rows(web_db)[0]["attempts"] <= 5


def test_not_implemented_error_is_not_retried(service, web_db):
    """未实现是「已知状态」，重试没有意义，只能记一次 skipped。"""
    enable(web_db, booking_success=True)
    calls = {"n": 0}
    original = service._deliver

    def counting(request, setting):
        calls["n"] += 1
        return original(request, setting)

    service._deliver = counting
    service.notify_booking_success(plan_id=1, run_id=1)
    assert calls["n"] == 1
    assert rows(web_db)[0]["status"] == "skipped"


def test_smtp_reason_constant_is_used(service, web_db):
    enable(web_db, booking_success=True)
    service.notify_booking_success(plan_id=1, run_id=1)
    assert SMTP_NOT_IMPLEMENTED_REASON in rows(web_db)[0]["last_error"]


# ------------------------------------------------------------------ 去重


def test_duplicate_event_within_window_is_not_sent_twice(service, web_db):
    enable(web_db, booking_success=True)
    service._deliver = lambda request, setting: None
    assert service.notify_booking_success(plan_id=1, run_id=7)["status"] == "sent"
    second = service.notify_booking_success(plan_id=1, run_id=7)
    assert second["status"] == "skipped"
    assert "重复事件" in second["error"]


def test_dedup_does_not_swallow_a_different_run(service, web_db):
    enable(web_db, booking_success=True)
    service._deliver = lambda request, setting: None
    service.notify_booking_success(plan_id=1, run_id=1)
    assert service.notify_booking_success(plan_id=1, run_id=2)["status"] == "sent"


def test_dedup_window_expires(web_db):
    enable(web_db, booking_success=True)
    delivered = []
    old = NotificationService(web_db, clock=lambda: NOW)
    old._deliver = lambda request, setting: delivered.append(request.dedup_key)
    assert old.notify_booking_success(plan_id=1, run_id=9)["status"] == "sent"

    later = NOW + timedelta(minutes=DEFAULT_DEDUP_WINDOW_MINUTES + 1)
    new = NotificationService(web_db, clock=lambda: later)
    new._deliver = lambda request, setting: delivered.append(request.dedup_key)
    assert new.notify_booking_success(plan_id=1, run_id=9)["status"] == "sent"
    assert len(delivered) == 2


def test_skipped_events_do_not_block_a_later_real_send(service, web_db):
    """未实现导致的 skipped 不应被当成「已发过」而永久压制后续通知。"""
    enable(web_db, booking_success=True)
    service.notify_booking_success(plan_id=1, run_id=3)  # skipped
    service._deliver = lambda request, setting: None
    assert service.notify_booking_success(plan_id=1, run_id=3)["status"] == "sent"


# ------------------------------------------------------------------ 事件与内容


def test_each_helper_maps_to_its_own_event(service, web_db):
    enable(web_db)
    service.notify_booking_success(plan_id=1, run_id=1)
    service.notify_auth_failure(plan_id=1)
    service.notify_auth_recovery(detail="已恢复")
    service.notify_no_seat(plan_id=1, run_id=2)
    service.notify_task_exception(plan_id=1, run_id=2)
    service.notify_submit_unknown(plan_id=1, run_id=3)
    service.notify_service_exception(detail="tick 异常")
    assert {r["event"] for r in rows(web_db)} == {e.value for e in NotifyEvent}


def test_auth_failure_notification_dedups_per_plan(service, web_db):
    enable(web_db)
    service.notify_auth_failure(plan_id=5)
    service.notify_auth_failure(plan_id=5)
    service.notify_auth_failure(plan_id=6)
    assert len(rows(web_db)) == 3
    keys = [r["dedup_key"] for r in web_db.query_all("SELECT dedup_key FROM notify_event")]
    assert keys[0] == keys[1] != keys[2]


def test_body_contains_no_credentials(service, web_db):
    enable(web_db, booking_success=True)
    request = build_request(
        NotifyEvent.BOOKING_SUCCESS,
        plan_id=1,
        run_id=1,
        detail="Authorization: Bearer supersecrettokenvalue",
    )
    service.notify(request)
    assert "supersecrettokenvalue" not in request.body
    assert "supersecrettokenvalue" not in str(rows(web_db))


def test_subject_is_stable_and_has_no_secrets(service):
    request = build_request(NotifyEvent.SUBMIT_UNKNOWN, plan_id=3, run_id=4, detail="x")
    assert request.subject == "[图书馆座位助手] 提交结果未知"
    assert request.dedup_key == "submit_unknown:3:4"


def test_notification_record_never_stores_the_smtp_password(service, web_db):
    repo.set_notify_setting(
        web_db,
        {"enabled": True, "smtp_password_env": "HAUT_SMTP_PASSWORD",
         "smtp_password": "SuperSecretMailPassword"},
        now=NOW,
    )
    service.notify_booking_success(plan_id=1, run_id=1)
    assert "SuperSecretMailPassword" not in str(rows(web_db))
    assert "SuperSecretMailPassword" not in str(repo.get_notify_setting(web_db))


# ------------------------------------------------------------------ 异常兜底


def test_notify_never_raises_when_the_database_is_broken(service):
    class BrokenDb:
        def query_one(self, *a, **k):
            raise RuntimeError("数据库不可用")

        def query_all(self, *a, **k):
            raise RuntimeError("数据库不可用")

    service.db = BrokenDb()
    result = service.notify_booking_success(plan_id=1, run_id=1)
    assert result["status"] == "skipped"
    assert "异常" in result["error"]


def test_record_write_failure_is_swallowed(service, web_db):
    """记录写不进去也不能把异常抛给预约主流程。"""
    enable(web_db, booking_success=True)
    service._deliver = lambda request, setting: None

    class HalfBrokenDb:
        def __init__(self, real):
            self._real = real

        def query_one(self, *a, **k):
            return self._real.query_one(*a, **k)

        def tx(self):
            raise RuntimeError("磁盘满")

    service.db = HalfBrokenDb(web_db)
    result = service.notify_booking_success(plan_id=1, run_id=1)
    assert result["status"] == "skipped"
    assert "写入失败" in result["error"] or "记录" in result["error"]


def test_broken_logger_does_not_break_notification(service, web_db):
    """日志系统本身出问题也不能把异常抛回预约主流程。"""
    enable(web_db, booking_success=True)

    class BrokenLogger:
        def warning(self, *a, **k):
            raise RuntimeError("日志系统坏了")

    service.logger = BrokenLogger()
    service._deliver = lambda request, setting: (_ for _ in ()).throw(OSError("x"))
    result = service.notify_booking_success(plan_id=1, run_id=1)
    assert result["status"] == "skipped"
    assert "异常" in result["error"]