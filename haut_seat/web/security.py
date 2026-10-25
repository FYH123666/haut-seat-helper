"""Web 安全基础设施：口令哈希、会话、CSRF、登录限流。

设计（对应「安全」要求）：
- 口令用标准库 ``hashlib.scrypt`` 加盐哈希，永不落明文；
- 会话令牌随机 32 字节，数据库只存 ``sha256`` 摘要 —— 数据库泄漏也无法直接冒用会话；
- CSRF 采用「同步器令牌」：令牌随会话存储，所有状态变更表单必须回传并做常量时间比对；
- 登录限流按「用户名 + IP」双维度计数，超限锁定一段时间；
- 会话 Cookie 由路由层设置 HttpOnly / SameSite=Lax / HTTPS 下 Secure。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

# scrypt 参数：n=2^14, r=8, p=1（约 16MB 内存，服务端单用户足够）
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32

SESSION_COOKIE = "hs_session"
SESSION_TTL_HOURS = 12
SESSION_IDLE_MINUTES = 120

LOGIN_MAX_FAILURES = 8
LOGIN_WINDOW_MINUTES = 15
LOGIN_LOCK_MINUTES = 15

MIN_PASSWORD_LENGTH = 10


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


# ------------------------------------------------------------------ 口令


def hash_password(password: str) -> str:
    """返回 ``scrypt$n$r$p$salt$hash`` 形式的字符串（可安全入库）。"""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"口令长度至少 {MIN_PASSWORD_LENGTH} 位")
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    """常量时间比对；存储格式异常一律返回 False（不抛异常、不泄漏细节）。"""
    if not password or not stored:
        return False
    try:
        scheme, n_s, r_s, p_s, salt_s, hash_s = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode("utf-8"), salt=_unb64(salt_s),
            n=int(n_s), r=int(r_s), p=int(p_s), dklen=len(_unb64(hash_s)),
        )
        return hmac.compare_digest(dk, _unb64(hash_s))
    except (ValueError, TypeError):
        return False


def validate_password_strength(password: str) -> list[str]:
    """返回问题列表；空列表表示通过。仅做最低限度要求，不强制复杂度。"""
    problems: list[str] = []
    if len(password or "") < MIN_PASSWORD_LENGTH:
        problems.append(f"口令长度至少 {MIN_PASSWORD_LENGTH} 位")
    if (password or "").strip() != password:
        problems.append("口令首尾不能有空白字符")
    return problems


# ------------------------------------------------------------------ 会话


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_matches(expected: str, provided: str) -> bool:
    if not expected or not provided:
        return False
    return hmac.compare_digest(str(expected), str(provided))


@dataclass(frozen=True)
class SessionRecord:
    token_hash: str
    admin_id: int
    csrf_token: str
    created_at: str
    expires_at: str
    last_seen_at: str
    username: str


def _parse(text: str) -> datetime:
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return datetime.fromtimestamp(0)


def create_session(db, admin_id: int, *, username: str = "", now: datetime | None = None) -> tuple[str, str]:
    """创建会话，返回 (明文令牌, CSRF 令牌)。明文令牌只出现在 Cookie 中。"""
    now = now or datetime.now()
    token = new_session_token()
    csrf = new_csrf_token()
    expires = now + timedelta(hours=SESSION_TTL_HOURS)
    fmt = "%Y-%m-%d %H:%M:%S"
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO session (token_hash, admin_id, csrf_token, created_at, expires_at, last_seen_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (token_digest(token), admin_id, csrf, now.strftime(fmt), expires.strftime(fmt), now.strftime(fmt)),
        )
    return token, csrf


def load_session(db, token: str, *, now: datetime | None = None) -> SessionRecord | None:
    """校验会话有效性（含过期与空闲超时）；无效返回 None。"""
    if not token or len(token) > 200:
        return None
    now = now or datetime.now()
    row = db.query_one(
        "SELECT s.token_hash, s.admin_id, s.csrf_token, s.created_at, s.expires_at,"
        "       s.last_seen_at, u.username"
        "  FROM session s JOIN admin_user u ON u.id = s.admin_id"
        " WHERE s.token_hash = ?",
        (token_digest(token),),
    )
    if row is None:
        return None
    if _parse(row["expires_at"]) <= now:
        return None
    if now - _parse(row["last_seen_at"]) > timedelta(minutes=SESSION_IDLE_MINUTES):
        return None
    with db.tx() as conn:
        conn.execute(
            "UPDATE session SET last_seen_at = ? WHERE token_hash = ?",
            (now.strftime("%Y-%m-%d %H:%M:%S"), row["token_hash"]),
        )
    return SessionRecord(
        token_hash=row["token_hash"],
        admin_id=row["admin_id"],
        csrf_token=row["csrf_token"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        last_seen_at=row["last_seen_at"],
        username=row["username"],
    )


def destroy_session(db, token: str) -> None:
    if not token:
        return
    with db.tx() as conn:
        conn.execute("DELETE FROM session WHERE token_hash = ?", (token_digest(token),))


def purge_expired_sessions(db, *, now: datetime | None = None) -> int:
    now = now or datetime.now()
    cutoff = (now - timedelta(minutes=SESSION_IDLE_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    with db.tx() as conn:
        cur = conn.execute(
            "DELETE FROM session WHERE expires_at <= ? OR last_seen_at <= ?",
            (now.strftime("%Y-%m-%d %H:%M:%S"), cutoff),
        )
        return cur.rowcount or 0


# ------------------------------------------------------------------ 登录限流


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def record_login_attempt(db, username: str, ip: str, ok: bool, *, now: datetime | None = None) -> None:
    now = now or datetime.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO login_attempt (username, ip, ok, at) VALUES (?, ?, ?, ?)",
            (username or "", ip or "", 1 if ok else 0, _fmt(now)),
        )


def login_locked_until(db, username: str, ip: str, *, now: datetime | None = None) -> datetime | None:
    """返回解锁时间；未锁定返回 None。最近一次成功登录会清零计数。"""
    now = now or datetime.now()
    window_start = _fmt(now - timedelta(minutes=LOGIN_WINDOW_MINUTES))
    rows = db.query_all(
        "SELECT ok, at FROM login_attempt"
        " WHERE (username = ? OR ip = ?) AND at >= ?"
        " ORDER BY at ASC",
        (username or "", ip or "", window_start),
    )
    if not rows:
        return None
    failures = 0
    for row in rows:
        if row["ok"]:
            failures = 0
        else:
            failures += 1
    if failures < LOGIN_MAX_FAILURES:
        return None
    last_failure = _parse(rows[-1]["at"])
    if rows[-1]["ok"]:
        return None
    unlock = last_failure + timedelta(minutes=LOGIN_LOCK_MINUTES)
    return unlock if unlock > now else None


def purge_old_login_attempts(db, *, now: datetime | None = None) -> int:
    now = now or datetime.now()
    cutoff = _fmt(now - timedelta(days=1))
    with db.tx() as conn:
        cur = conn.execute("DELETE FROM login_attempt WHERE at < ?", (cutoff,))
        return cur.rowcount or 0


# ------------------------------------------------------------------ 管理员


class AdminExistsError(RuntimeError):
    pass


def create_admin(db, username: str, password: str, *, now: datetime | None = None) -> int:
    """创建管理员。用户名唯一；口令经强度校验后 scrypt 哈希入库。"""
    username = (username or "").strip()
    if not username or len(username) > 64:
        raise ValueError("用户名不能为空且不超过 64 字符")
    problems = validate_password_strength(password)
    if problems:
        raise ValueError("；".join(problems))
    now = now or datetime.now()
    fmt = _fmt(now)
    with db.tx() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO admin_user (username, password_hash, created_at, updated_at)"
                " VALUES (?, ?, ?, ?)",
                (username, hash_password(password), fmt, fmt),
            )
        except sqlite3.IntegrityError as exc:
            raise AdminExistsError(f"管理员 {username!r} 已存在") from exc
        return int(cur.lastrowid)


def count_admins(db) -> int:
    row = db.query_one("SELECT COUNT(*) AS n FROM admin_user")
    return int(row["n"]) if row else 0


def find_admin(db, username: str) -> sqlite3.Row | None:
    return db.query_one("SELECT * FROM admin_user WHERE username = ?", ((username or "").strip(),))


def set_admin_password(db, admin_id: int, password: str, *, now: datetime | None = None) -> None:
    problems = validate_password_strength(password)
    if problems:
        raise ValueError("；".join(problems))
    now = now or datetime.now()
    with db.tx() as conn:
        conn.execute(
            "UPDATE admin_user SET password_hash = ?, updated_at = ? WHERE id = ?",
            (hash_password(password), _fmt(now), admin_id),
        )
        # 改密后使所有既有会话失效
        conn.execute("DELETE FROM session WHERE admin_id = ?", (admin_id,))


def touch_last_login(db, admin_id: int, *, now: datetime | None = None) -> None:
    now = now or datetime.now()
    with db.tx() as conn:
        conn.execute(
            "UPDATE admin_user SET last_login_at = ? WHERE id = ?", (_fmt(now), admin_id)
        )