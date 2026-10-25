"""状态机（services.state）的纯函数测试。

重点验证需求里的硬约束：
- 「提交结果未知」是独立状态，绝不显示为普通失败、绝不被自动重放；
- 「暂停」与「停止」语义不同；
- 非法状态迁移一律拒绝。
"""

from __future__ import annotations

import pytest

from haut_seat.services.state import (
    ERROR_CATEGORY_LABELS,
    PLAN_STATUS_LABELS,
    PLAN_STATUS_TONE,
    RUN_STATUS_LABELS,
    RUN_STATUS_TONE,
    ErrorCategory,
    PlanStatus,
    RunStatus,
    RunStop,
    apply_pause,
    apply_resume,
    apply_stop,
    assert_transition,
    blocks_new_submissions,
    can_transition,
    error_category_for,
    exit_code_for,
    is_schedulable,
    plan_status_for,
    requires_unknown_acknowledgement,
    run_status_for,
)

ALL_RUN_STOPS = list(RunStop)


def test_every_plan_status_has_label_and_tone():
    for status in PlanStatus:
        assert status in PLAN_STATUS_LABELS
        assert status in PLAN_STATUS_TONE


def test_every_run_status_has_label_and_tone():
    for status in RunStatus:
        assert status in RUN_STATUS_LABELS
        assert status in RUN_STATUS_TONE


def test_plan_status_labels_are_distinct_for_unknown_and_failed():
    """未知必须与失败在文案上明确区分，不能混为一谈。"""
    unknown = PLAN_STATUS_LABELS[PlanStatus.UNKNOWN]
    failed = PLAN_STATUS_LABELS[PlanStatus.FAILED]
    assert unknown != failed
    assert "未知" in unknown
    # 未知的用色也必须与失败不同，避免用户扫一眼就当成失败
    assert PLAN_STATUS_TONE[PlanStatus.UNKNOWN] != PLAN_STATUS_TONE[PlanStatus.FAILED]
    assert RUN_STATUS_TONE[RunStatus.UNKNOWN] != RUN_STATUS_TONE[RunStatus.FAILED]


def test_every_run_stop_maps_to_a_known_status():
    for stop in ALL_RUN_STOPS:
        assert run_status_for(stop) in RunStatus
        assert error_category_for(stop) in ErrorCategory
        assert isinstance(exit_code_for(stop), int)


@pytest.mark.parametrize("stop", [RunStop.SUBMIT_UNKNOWN, RunStop.SUBMIT_EXCEPTION])
def test_submit_unknown_is_never_classified_as_failure(stop):
    """提交结果未知/提交异常 → 未知状态，而不是失败。"""
    assert run_status_for(stop) is RunStatus.UNKNOWN
    assert run_status_for(stop) is not RunStatus.FAILED
    assert plan_status_for(RunStatus.UNKNOWN) is PlanStatus.UNKNOWN
    assert plan_status_for(RunStatus.UNKNOWN) is not PlanStatus.FAILED


def test_auth_invalid_expects_human_action_not_plain_failure():
    assert run_status_for(RunStop.AUTH_INVALID) is RunStatus.AWAITING_AUTH
    assert plan_status_for(RunStatus.AWAITING_AUTH) is PlanStatus.AWAITING_AUTH
    assert exit_code_for(RunStop.AUTH_INVALID) == 3


def test_window_closed_without_any_submit_is_no_seat():
    assert run_status_for(RunStop.WINDOW_CLOSED) is RunStatus.NO_SEAT
    assert error_category_for(RunStop.WINDOW_CLOSED) is ErrorCategory.NO_SEAT


def test_policy_block_does_not_change_plan_status():
    """被安全开关拦截不算一次失败，也不能把计划状态改坏。"""
    assert run_status_for(RunStop.BLOCKED) is RunStatus.STOPPED
    assert plan_status_for(RunStatus.STOPPED) is PlanStatus.STOPPED


@pytest.mark.parametrize(
    "src,dst",
    [
        (PlanStatus.PENDING, PlanStatus.RUNNING),
        (PlanStatus.PENDING, PlanStatus.PAUSED),
        (PlanStatus.PENDING, PlanStatus.STOPPED),
        (PlanStatus.RUNNING, PlanStatus.UNKNOWN),
        (PlanStatus.RUNNING, PlanStatus.SUCCEEDED),
        (PlanStatus.UNKNOWN, PlanStatus.PENDING),
        (PlanStatus.STOPPED, PlanStatus.PENDING),
    ],
)
def test_allowed_transitions(src, dst):
    assert can_transition(src, dst)
    assert_transition(src, dst)


@pytest.mark.parametrize(
    "src,dst",
    [
        (PlanStatus.STOPPED, PlanStatus.RUNNING),
        (PlanStatus.PAUSED, PlanStatus.RUNNING),
        (PlanStatus.SUCCEEDED, PlanStatus.RUNNING),
        (PlanStatus.FAILED, PlanStatus.RUNNING),
        (PlanStatus.UNKNOWN, PlanStatus.RUNNING),
        (PlanStatus.AWAITING_AUTH, PlanStatus.RUNNING),
    ],
)
def test_forbidden_transitions(src, dst):
    assert not can_transition(src, dst)
    with pytest.raises(ValueError):
        assert_transition(src, dst)


def test_pause_while_running_is_deferred_not_interrupting():
    result = apply_pause(PlanStatus.RUNNING, has_active_run=True)
    assert result.allowed
    assert result.deferred, "运行中暂停必须延后生效，不能中断正在飞行的提交"
    assert result.status is PlanStatus.RUNNING


def test_stop_while_running_is_deferred_not_interrupting():
    result = apply_stop(PlanStatus.RUNNING, has_active_run=True)
    assert result.allowed
    assert result.deferred
    assert "不会中断" in result.reason


def test_pause_from_pending_applies_immediately():
    result = apply_pause(PlanStatus.PENDING, has_active_run=False)
    assert result.allowed and not result.deferred
    assert result.status is PlanStatus.PAUSED


def test_stop_from_pending_applies_immediately():
    result = apply_stop(PlanStatus.PENDING, has_active_run=False)
    assert result.allowed and not result.deferred
    assert result.status is PlanStatus.STOPPED


def test_resume_from_unknown_requires_explicit_acknowledgement():
    refused = apply_resume(PlanStatus.UNKNOWN, has_active_run=False)
    assert not refused.allowed
    assert "未知" in refused.reason

    accepted = apply_resume(
        PlanStatus.UNKNOWN, has_active_run=False, acknowledge_unknown=True
    )
    assert accepted.allowed
    assert accepted.status is PlanStatus.PENDING


def test_resume_while_running_is_refused():
    result = apply_resume(PlanStatus.RUNNING, has_active_run=True)
    assert not result.allowed


def test_resume_from_stopped_returns_to_pending():
    result = apply_resume(PlanStatus.STOPPED, has_active_run=False)
    assert result.allowed
    assert result.status is PlanStatus.PENDING


def test_only_pending_is_schedulable():
    for status in PlanStatus:
        assert is_schedulable(status) is (status is PlanStatus.PENDING)


def test_blocks_new_submissions_covers_all_hold_states():
    for status in (
        PlanStatus.AWAITING_AUTH,
        PlanStatus.UNKNOWN,
        PlanStatus.PAUSED,
        PlanStatus.STOPPED,
        PlanStatus.SUCCEEDED,
    ):
        assert blocks_new_submissions(status), status
    assert not blocks_new_submissions(PlanStatus.PENDING)
    assert not blocks_new_submissions(PlanStatus.RUNNING)


def test_requires_unknown_acknowledgement_only_for_unknown():
    assert requires_unknown_acknowledgement(PlanStatus.UNKNOWN)
    for status in PlanStatus:
        if status is not PlanStatus.UNKNOWN:
            assert not requires_unknown_acknowledgement(status)


def test_unknown_category_and_label_are_distinguishable():
    label = ERROR_CATEGORY_LABELS[ErrorCategory.SUBMIT_UNKNOWN]
    assert "未知" in label
    assert label != ERROR_CATEGORY_LABELS[ErrorCategory.BUSINESS_ERROR]