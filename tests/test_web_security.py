"""Web 安全基础设施测试：口令哈希、会话、CSRF、登录限流、管理员管理。

全部离线，不访问网络，也不产生任何预约行为。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from haut_seat.web import security as sec

GOOD_PASSWORD = "Correct-Horse-9"


# ------------------------------------------------------------------ 口令


def test_hash_is_not_plaintext_and_has_expected_shape():
    stored = sec.hash_password(GOOD_PASSWORD)
    assert GOOD_PASSWORD not in stored
    scheme, n, r, p, salt, digest = stored.split("$")
    assert scheme == "scrypt"
    assert int(n) == sec.SCRYPT_N and int(r) == sec.SCRYPT_R and int(p) == sec.SCRYPT_P
    assert salt and digest


def test_same_password_hashes_differently():
    """必须加盐：相同口令两次哈希结果不同。"""
    assert sec.hash_password(GOOD_PASSWORD) != sec.hash_password(GOOD_PASSWORD)


def test_hash_rejects_short_password():
    with pytest.raises(ValueError):
        sec.hash_password("short")


def test_verify_password_accepts_correct_and_rejects_wrong():
    stored = sec.hash_password(GOOD_PASSWORD)
    assert sec.verify_password(GOOD_PASSWORD, stored)
    assert not sec.verify_password(GOOD_PASSWORD + "x", stored)
    assert not sec.verify_password("", stored)


@pytest.mark.parametrize(
    "stored", ["", "garbage", "scrypt$bad", "bcrypt$1$2$3$4$5", "scrypt$1$1$1$!!$!!"]
)
def test_verify_password_never_raises_on_corrupt_storage(stored):
    assert sec.verify_password(GOOD_PASSWORD, stored) is False


def test_password_strength_reports_problems():
    assert sec.validate_password_strength(GOOD_PASSWORD) == []
    assert sec.validate_password_strength("short")
    assert sec.validate_password_strength(" " + GOOD_PASSWORD)


# ------------------------------------------------------------------ CSRF


def test_csrf_matches_requires_both_sides():
    token = sec.new_csrf_token()
    assert sec.csrf_matches(token, token)
    assert not sec.csrf_matches(token, token + "x")
    assert not sec.csrf_matches(token, "")
    assert not sec.csrf_matches("", token)


def test_csrf_tokens_are_unique_and_long():
    tokens = {sec.new_csrf_token() for _ in range(20)}
    assert len(tokens) == 20
    assert all(len(t) >= 32 for t in tokens)


# ------------------------------------------------------------------ 会话


def test_session_token_is_not_stored_in_plaintext(web_db):
    token, csrf = sec.create_session(web_db, _admin_id(web_db))
    rows = web_db.query_all("SELECT token_hash FROM session")
    assert len(rows) == 1
    assert rows[0]["token_hash"] != token
    assert rows[0]["token_hash"] == sec.token_digest(token)
    assert csrf != token


def test_session_roundtrip_and_destroy(web_db):
    admin_id = _admin_id(web_db)
    token, csrf = sec.create_session(web_db, admin_id)
    record = sec.load_session(web_db, token)
    assert record is not None
    assert record.admin_id == admin_id
    assert record.username == "tester"
    assert record.csrf_token == csrf

    sec.destroy_session(web_db, token)
    assert sec.load_session(web_db, token) is None


def test_unknown_and_empty_tokens_are_rejected(web_db):
    _admin_id(web_db)
    assert sec.load_session(web_db, "") is None
    assert sec.load_session(web_db, "not-a-real-token") is None
    assert sec.load_session(web_db, "x" * 500) is None


def test_expired_session_is_rejected(web_db):
    admin_id = _admin_id(web_db)
    now = datetime(2026, 1, 1, 8, 0, 0)
    token, _ = sec.create_session(web_db, admin_id, now=now)
    later = now + timedelta(hours=sec.SESSION_TTL_HOURS + 1)
    assert sec.load_session(web_db, token, now=later) is None


def test_idle_session_is_rejected(web_db):
    admin_id = _admin_id(web_db)
    now = datetime(2026, 1, 1, 8, 0, 0)
    token, _ = sec.create_session(web_db, admin_id, now=now)
    later = now + timedelta(minutes=sec.SESSION_IDLE_MINUTES + 1)
    assert sec.load_session(web_db, token, now=later) is None


def test_purge_expired_sessions_removes_only_stale_rows(web_db):
    admin_id = _admin_id(web_db)
    now = datetime(2026, 1, 1, 8, 0, 0)
    sec.create_session(web_db, admin_id, now=now)
    fresh, _ = sec.create_session(web_db, admin_id, now=now)
    removed = sec.purge_expired_sessions(
        web_db, now=now + timedelta(minutes=sec.SESSION_IDLE_MINUTES + 1)
    )
    assert removed == 2
    assert sec.load_session(
        web_db, fresh, now=now + timedelta(minutes=sec.SESSION_IDLE_MINUTES + 1)
    ) is None


def test_password_change_invalidates_existing_sessions(web_db):
    admin_id = _admin_id(web_db)
    token, _ = sec.create_session(web_db, admin_id)
    sec.set_admin_password(web_db, admin_id, "Another-Password-1")
    assert sec.load_session(web_db, token) is None


# ------------------------------------------------------------------ 登录限流


def test_login_lock_after_repeated_failures():
    from haut_seat.web.db import Database

    db = Database(":memory:")
    db.init_schema()
    now = datetime(2026, 1, 1, 8, 0, 0)
    for i in range(sec.LOGIN_MAX_FAILURES):
        assert sec.login_locked_until(db, "tester", "10.0.0.1", now=now) is None
        sec.record_login_attempt(db, "tester", "10.0.0.1", False, now=now)
    locked = sec.login_locked_until(db, "tester", "10.0.0.1", now=now)
    assert locked is not None
    assert locked > now
    db.close_current_thread()


def test_login_lock_expires():
    from haut_seat.web.db import Database

    db = Database(":memory:")
    db.init_schema()
    now = datetime(2026, 1, 1, 8, 0, 0)
    for _ in range(sec.LOGIN_MAX_FAILURES):
        sec.record_login_attempt(db, "tester", "10.0.0.1", False, now=now)
    after = now + timedelta(minutes=sec.LOGIN_LOCK_MINUTES + 1)
    assert sec.login_locked_until(db, "tester", "10.0.0.1", now=after) is None
    db.close_current_thread()


def test_successful_login_resets_failure_counter():
    from haut_seat.web.db import Database

    db = Database(":memory:")
    db.init_schema()
    now = datetime(2026, 1, 1, 8, 0, 0)
    for _ in range(sec.LOGIN_MAX_FAILURES - 1):
        sec.record_login_attempt(db, "tester", "10.0.0.1", False, now=now)
    sec.record_login_attempt(db, "tester", "10.0.0.1", True, now=now)
    assert sec.login_locked_until(db, "tester", "10.0.0.1", now=now) is None
    db.close_current_thread()


def test_purge_old_login_attempts():
    from haut_seat.web.db import Database

    db = Database(":memory:")
    db.init_schema()
    now = datetime(2026, 1, 1, 8, 0, 0)
    sec.record_login_attempt(db, "tester", "10.0.0.1", False, now=now)
    removed = sec.purge_old_login_attempts(db, now=now + timedelta(days=2))
    assert removed == 1
    db.close_current_thread()


# ------------------------------------------------------------------ 管理员


def test_create_admin_and_duplicate_rejected(web_db):
    admin_id = sec.create_admin(web_db, "tester", GOOD_PASSWORD)
    assert admin_id > 0
    assert sec.count_admins(web_db) == 1
    with pytest.raises(sec.AdminExistsError):
        sec.create_admin(web_db, "tester", GOOD_PASSWORD)


def test_created_admin_password_is_hashed(web_db):
    sec.create_admin(web_db, "tester", GOOD_PASSWORD)
    row = sec.find_admin(web_db, "tester")
    assert row is not None
    assert GOOD_PASSWORD not in row["password_hash"]
    assert sec.verify_password(GOOD_PASSWORD, row["password_hash"])


@pytest.mark.parametrize("username", ["", "   ", "x" * 65])
def test_create_admin_validates_username(web_db, username):
    with pytest.raises(ValueError):
        sec.create_admin(web_db, username, GOOD_PASSWORD)


def test_create_admin_rejects_weak_password(web_db):
    with pytest.raises(ValueError):
        sec.create_admin(web_db, "tester", "short")


def test_username_is_trimmed(web_db):
    sec.create_admin(web_db, "  tester  ", GOOD_PASSWORD)
    assert sec.find_admin(web_db, "tester") is not None


def _admin_id(db) -> int:
    return sec.create_admin(db, "tester", GOOD_PASSWORD)