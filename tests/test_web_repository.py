"""数据访问层测试：计划生命周期、运行记录、脱敏、认证状态、通知设置。

全部离线（临时 SQLite），不触发任何真实预约。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from haut_seat.services.state import ErrorCategory, PlanStatus, RunPhase, RunStatus
from haut_seat.web import repository as repo
from haut_seat.web.validation import parse_plan

NOW = datetime(2026, 10, 16, 9, 0, 0)


def make_plan(db, *, enabled=False, name="测试计划", **overrides):
    from datetime import date

    form = {
        "name": name,
        "run_date": "2026-10-20",
        "repeat_rule": "once",
        "trigger_at": "07:00:00",
        "book_start": "2026-10-20 08:00:00",
        "book_end": "2026-10-20 12:00:00",
        "regionid": "16",
        "preferred_seats": "A桌 1座",
        "preferred_prefixes": "",
        "confirmed_bookable_states": '[["2", 1, false]]',
        "max_attempts_per_seat": "3",
        "note": "",
    }
    form.update(overrides)
    data, _ = parse_plan(form, today=date(2026, 10, 16))
    return repo.create_plan(db, data, enabled=enabled, now=NOW)


# ------------------------------------------------------------------ 脱敏


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Bearer abcdefghijklmnop",
        "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signaturepart",
        '{"Admin-Token": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc"}',
        "captcha: 8f3a2b",
        "cookie: " + "A" * 80,
    ],
)
def test_redact_message_removes_credential_shapes(text):
    out = repo.redact_message(text)
    assert "Bearer " not in out or "<redacted>" in out
    assert "eyJhbGciOiJIUzI1NiJ9" not in out
    assert "8f3a2b" not in out
    assert "A" * 80 not in out


def test_redact_message_keeps_ordinary_chinese_text():
    assert repo.redact_message("候选座位: 3 个") == "候选座位: 3 个"


def test_redact_message_truncates_long_text():
    out = repo.redact_message("字" * 2000)
    assert len(out) < 700
    assert "截断" in out


def test_redact_message_handles_none():
    assert repo.redact_message(None) == ""


# ------------------------------------------------------------------ 计划生命周期


def test_new_plan_defaults_to_paused_and_unarmed(web_db):
    """新建计划绝不自动武装：否则「打开服务」就等于「自动预约」。"""
    plan_id = make_plan(web_db)
    plan = repo.get_plan(web_db, plan_id)
    assert plan["status"] is PlanStatus.PAUSED
    assert plan["enabled"] == 0


def test_plan_roundtrip_preserves_fields(web_db):
    plan_id = make_plan(web_db)
    plan = repo.get_plan(web_db, plan_id)
    assert plan["name"] == "测试计划"
    assert plan["preferred_seats"] == ["A桌 1座"]
    assert plan["confirmed_bookable_states"] == [["2", 1, False]]
    assert plan["regionid"] == 16


def test_resume_from_paused_enables_and_goes_pending(web_db):
    plan_id = make_plan(web_db)
    result = repo.resume_plan(web_db, plan_id, now=NOW)
    assert result["ok"]
    plan = repo.get_plan(web_db, plan_id)
    assert plan["status"] is PlanStatus.PENDING
    assert plan["enabled"] == 1


def test_pause_and_stop_from_pending(web_db):
    plan_id = make_plan(web_db)
    repo.resume_plan(web_db, plan_id, now=NOW)

    assert repo.pause_plan(web_db, plan_id, now=NOW)["ok"]
    assert repo.get_plan(web_db, plan_id)["status"] is PlanStatus.PAUSED

    assert repo.stop_plan(web_db, plan_id, now=NOW)["ok"]
    stopped = repo.get_plan(web_db, plan_id)
    assert stopped["status"] is PlanStatus.STOPPED
    assert stopped["enabled"] == 0, "停止后必须解除武装，否则刷新后又被调度器拾取"


def test_pause_while_running_is_deferred_and_flags_only(web_db):
    plan_id = make_plan(web_db)
    repo.resume_plan(web_db, plan_id, now=NOW)
    repo.start_run(web_db, plan_id, trigger="scheduler", now=NOW)

    result = repo.pause_plan(web_db, plan_id, now=NOW)
    assert result["ok"] and result["deferred"]
    plan = repo.get_plan(web_db, plan_id)
    assert plan["status"] is PlanStatus.RUNNING, "运行中不能被瞬间改状态"
    assert plan["pause_requested"] == 1


def test_stop_while_running_is_deferred(web_db):
    plan_id = make_plan(web_db)
    repo.resume_plan(web_db, plan_id, now=NOW)
    repo.start_run(web_db, plan_id, trigger="scheduler", now=NOW)

    result = repo.stop_plan(web_db, plan_id, now=NOW)
    assert result["ok"] and result["deferred"]
    plan = repo.get_plan(web_db, plan_id)
    assert plan["stop_requested"] == 1
    assert plan["status"] is PlanStatus.RUNNING


def test_unknown_plan_cannot_be_resumed_without_acknowledgement(web_db):
    plan_id = make_plan(web_db)
    repo.resume_plan(web_db, plan_id, now=NOW)
    run_id = repo.start_run(web_db, plan_id, trigger="scheduler", now=NOW)
    repo.finish_run(
        web_db, run_id, status=RunStatus.UNKNOWN, category=ErrorCategory.SUBMIT_UNKNOWN,
        stop_reason="submit_unknown", message="", exit_code=0,
        plan_status=PlanStatus.UNKNOWN, now=NOW,
    )
    assert repo.get_plan(web_db, plan_id)["status"] is PlanStatus.UNKNOWN

    refused = repo.resume_plan(web_db, plan_id, acknowledge_unknown=False, now=NOW)
    assert not refused["ok"]
    assert repo.get_plan(web_db, plan_id)["status"] is PlanStatus.UNKNOWN

    accepted = repo.resume_plan(web_db, plan_id, acknowledge_unknown=True, now=NOW)
    assert accepted["ok"]
    plan = repo.get_plan(web_db, plan_id)
    assert plan["status"] is PlanStatus.PENDING
    assert plan["unknown_ack_at"], "恢复时必须记录人工核实时间"


def test_pause_non_existent_plan_reports_failure(web_db):
    assert not repo.pause_plan(web_db, 999, now=NOW)["ok"]
    assert not repo.resume_plan(web_db, 999, now=NOW)["ok"]
    assert not repo.stop_plan(web_db, 999, now=NOW)["ok"]


def test_update_and_delete_plan(web_db):
    from datetime import date

    plan_id = make_plan(web_db)
    data, _ = parse_plan(
        {
            "name": "改名", "run_date": "2026-10-21", "repeat_rule": "daily",
            "trigger_at": "06:30", "book_start": "2026-10-21 09:00:00",
            "book_end": "2026-10-21 11:00:00", "regionid": "17",
            "preferred_seats": "", "preferred_prefixes": "十层",
            "confirmed_bookable_states": "", "max_attempts_per_seat": "5", "note": "n",
        },
        today=date(2026, 10, 16),
    )
    repo.update_plan(web_db, plan_id, data, now=NOW)
    plan = repo.get_plan(web_db, plan_id)
    assert plan["name"] == "改名"
    assert plan["repeat_rule"] == "daily"
    assert plan["regionid"] == 17

    repo.delete_plan(web_db, plan_id)
    assert repo.get_plan(web_db, plan_id) is None


def test_plan_has_active_run(web_db):
    plan_id = make_plan(web_db)
    assert not repo.plan_has_active_run(web_db, plan_id)
    run_id = repo.start_run(web_db, plan_id, now=NOW)
    assert repo.plan_has_active_run(web_db, plan_id)
    repo.finish_run(
        web_db, run_id, status=RunStatus.FAILED, category=ErrorCategory.NO_SEAT,
        stop_reason="window_closed", message="", exit_code=0, now=NOW,
    )
    assert not repo.plan_has_active_run(web_db, plan_id)


def test_deleting_plan_cascades_runs(web_db):
    plan_id = make_plan(web_db)
    run_id = repo.start_run(web_db, plan_id, now=NOW)
    repo.add_run_event(web_db, run_id, phase=RunPhase.START, level="info", message="x", now=NOW)
    repo.delete_plan(web_db, plan_id)
    assert repo.get_run(web_db, run_id) is None
    assert repo.list_run_events(web_db, run_id) == []


# ------------------------------------------------------------------ 运行记录


def test_start_run_marks_plan_running(web_db):
    plan_id = make_plan(web_db)
    repo.resume_plan(web_db, plan_id, now=NOW)
    run_id = repo.start_run(web_db, plan_id, trigger="manual", now=NOW)
    plan = repo.get_plan(web_db, plan_id)
    assert plan["status"] is PlanStatus.RUNNING
    assert plan["last_run_id"] == run_id
    run = repo.get_run(web_db, run_id)
    assert run["trigger"] == "manual"
    assert run["status"] is RunStatus.RUNNING


def test_finish_run_records_all_counters_and_redacts_message(web_db):
    plan_id = make_plan(web_db)
    run_id = repo.start_run(web_db, plan_id, now=NOW)
    repo.finish_run(
        web_db, run_id, status=RunStatus.SUCCEEDED, category=ErrorCategory.NONE,
        stop_reason="success", message="预约成功 bookingid=42 Bearer abcdefghijkl",
        exit_code=0, booking_id=42, query_count=3, submit_count=1, seat_switches=2,
        plan_status=PlanStatus.SUCCEEDED, now=NOW,
    )
    run = repo.get_run(web_db, run_id)
    assert run["status"] is RunStatus.SUCCEEDED
    assert run["booking_id"] == 42
    assert run["query_count"] == 3 and run["submit_count"] == 1
    assert run["seat_switches"] == 2
    assert "abcdefghijkl" not in run["message"]
    assert repo.get_plan(web_db, plan_id)["status"] is PlanStatus.SUCCEEDED


def test_unknown_run_stays_unknown_and_is_listed_separately(web_db):
    plan_id = make_plan(web_db)
    run_id = repo.start_run(web_db, plan_id, now=NOW)
    repo.finish_run(
        web_db, run_id, status=RunStatus.UNKNOWN, category=ErrorCategory.SUBMIT_UNKNOWN,
        stop_reason="submit_unknown", message="提交后未收到应答", exit_code=0,
        plan_status=PlanStatus.UNKNOWN, now=NOW,
    )
    run = repo.get_run(web_db, run_id)
    assert run["status"] is RunStatus.UNKNOWN
    assert run["status"] is not RunStatus.FAILED
    assert run["category"] == ErrorCategory.SUBMIT_UNKNOWN.value

    stats = repo.dashboard_stats(web_db, today=NOW.date())
    assert stats["runs_unknown"] == 1
    assert stats["runs_failed"] == 0
    assert stats["plans_unknown"] == 1
    assert stats["plans_failed"] == 0


def test_list_runs_joins_plan_name_and_orders_desc(web_db):
    plan_id = make_plan(web_db, name="计划甲")
    for i in range(3):
        run_id = repo.start_run(web_db, plan_id, now=NOW + timedelta(minutes=i))
        repo.finish_run(
            web_db, run_id, status=RunStatus.FAILED, category=ErrorCategory.NO_SEAT,
            stop_reason="window_closed", message="", exit_code=0, now=NOW,
        )
    runs = repo.list_runs(web_db, plan_id=plan_id)
    assert [r["plan_name"] for r in runs] == ["计划甲"] * 3
    assert runs[0]["id"] > runs[-1]["id"]


def test_list_running_runs_and_last_run_for_plan(web_db):
    plan_id = make_plan(web_db)
    run_id = repo.start_run(web_db, plan_id, now=NOW)
    running = repo.list_running_runs(web_db)
    assert [r["id"] for r in running] == [run_id]
    assert repo.last_run_for_plan(web_db, plan_id)["id"] == run_id


def test_run_events_are_redacted_and_ordered(web_db):
    plan_id = make_plan(web_db)
    run_id = repo.start_run(web_db, plan_id, now=NOW)
    repo.add_run_event(
        web_db, run_id, phase=RunPhase.AUTH_CHECK, level="error",
        message="precheck failed: Authorization: Bearer supersecrettokenvalue",
        category=ErrorCategory.AUTH_INVALID, now=NOW,
    )
    repo.add_run_event(web_db, run_id, phase=RunPhase.END, level="info", message="结束", now=NOW)
    events = repo.list_run_events(web_db, run_id)
    assert [e["phase"] for e in events] == ["auth_check", "end"]
    assert "supersecrettokenvalue" not in events[0]["message"]
    assert events[0]["level"] == "error"
    assert events[0]["category"] == ErrorCategory.AUTH_INVALID.value


def test_purge_run_events_removes_only_old_rows(web_db):
    plan_id = make_plan(web_db)
    old_run = repo.start_run(web_db, plan_id, now=NOW - timedelta(days=40))
    new_run = repo.start_run(web_db, plan_id, now=NOW)
    repo.add_run_event(web_db, old_run, phase=RunPhase.START, level="info",
                       message="旧", now=NOW - timedelta(days=40))
    repo.add_run_event(web_db, new_run, phase=RunPhase.START, level="info",
                       message="新", now=NOW)
    repo.purge_run_events(web_db, keep_days=30, now=NOW)
    assert repo.get_run(web_db, old_run) is None
    assert repo.get_run(web_db, new_run) is not None
    assert repo.list_run_events(web_db, old_run) == []
    assert len(repo.list_run_events(web_db, new_run)) == 1


# ------------------------------------------------------------------ 认证状态


def test_auth_state_defaults_to_indeterminate(web_db):
    state = repo.get_auth_state(web_db)
    assert state["status"] == "indeterminate"
    assert state["recovery_state"] == "idle"


def test_auth_event_is_written_only_on_status_change(web_db):
    for _ in range(5):
        repo.set_auth_state(web_db, status="valid", message="ok", now=NOW)
    assert len(repo.list_auth_events(web_db)) == 1
    repo.set_auth_state(web_db, status="invalid", message="失效", now=NOW)
    assert len(repo.list_auth_events(web_db)) == 2


def test_auth_state_message_is_redacted(web_db):
    repo.set_auth_state(
        web_db, status="invalid",
        message="Authorization: Bearer supersecrettokenvalue 已失效", now=NOW,
    )
    assert "supersecrettokenvalue" not in repo.get_auth_state(web_db)["message"]


# ------------------------------------------------------------------ 通知设置


def test_notify_setting_stores_env_var_name_never_a_password(web_db):
    repo.set_notify_setting(
        web_db,
        {
            "enabled": True, "channel": "email", "smtp_host": "smtp.example.com",
            "smtp_port": "465", "smtp_user": "me@example.com",
            "smtp_password_env": "HAUT_SMTP_PASSWORD", "mail_from": "me@example.com",
            "mail_to": "me@example.com", "use_tls": True,
            "event_flags": {"booking_success": True},
            # 就算表单里混进口令字段，也不能被持久化
            "smtp_password": "SuperSecretMailPassword",
        },
        now=NOW,
    )
    setting = repo.get_notify_setting(web_db)
    assert setting["smtp_password_env"] == "HAUT_SMTP_PASSWORD"
    assert setting["event_flags"] == {"booking_success": True}
    assert "SuperSecretMailPassword" not in str(setting)


def test_notify_setting_port_falls_back_when_invalid(web_db):
    repo.set_notify_setting(web_db, {"smtp_port": "not-a-port"}, now=NOW)
    assert repo.get_notify_setting(web_db)["smtp_port"] == 465
    repo.set_notify_setting(web_db, {"smtp_port": "99999"}, now=NOW)
    assert repo.get_notify_setting(web_db)["smtp_port"] == 465


# ------------------------------------------------------------------ 全局设置


def test_global_pause_defaults_to_on(web_db):
    """默认必须是「暂停」：安装完就自动开始抢座是不可接受的。"""
    setting = repo.get_app_setting(web_db)
    assert setting["global_pause"] == 1
    assert setting["scheduler_enabled"] == 1


def test_set_app_setting_toggles_independently(web_db):
    repo.set_app_setting(web_db, global_pause=False, now=NOW)
    assert repo.get_app_setting(web_db)["global_pause"] == 0
    assert repo.get_app_setting(web_db)["scheduler_enabled"] == 1
    repo.set_app_setting(web_db, scheduler_enabled=False, now=NOW)
    setting = repo.get_app_setting(web_db)
    assert setting["global_pause"] == 0 and setting["scheduler_enabled"] == 0


# ------------------------------------------------------------------ 统计


def test_dashboard_stats_counts_each_plan_status(web_db):
    paused = make_plan(web_db, name="暂停的")
    pending = make_plan(web_db, name="待执行的")
    repo.resume_plan(web_db, pending, now=NOW)
    stats = repo.dashboard_stats(web_db, today=NOW.date())
    assert stats["plans_total"] == 2
    assert stats["plans_paused"] == 1
    assert stats["plans_pending"] == 1
    assert repo.get_plan(web_db, paused)["status"] is PlanStatus.PAUSED