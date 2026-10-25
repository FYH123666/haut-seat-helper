"""用户端账号体系：注册、登录、会话、限流、邀请码。

## 与管理员体系的关系：**两套完全独立的东西**

| | 管理端 | 用户端 |
|---|---|---|
| 账号表 | ``admin_user`` | ``app_user`` |
| 会话表 | ``session`` | ``user_session`` |
| Cookie | ``hs_session`` | ``hs_user`` |
| CSRF | 同步器令牌（随会话） | 双提交 Cookie（``haut_booking_csrf``） |
| 登录限流 | ``login_attempt`` | ``user_login_attempt`` |

刻意不复用同一张表：一旦共用，用户端令牌是否通过管理端校验就会取决于
"某个 admin_id 是否存在"这种隐式条件，越权往往就是这样长出来的。
两套的 Cookie 名与令牌空间也不相交。

## 用户名与口令

- **用户名统一按小写归一化后存储与查询**，因此 ``Alice`` 与 ``alice`` 是同一个账号。
  这不是洁癖：若按原样存储、按不区分大小写查询，`Alice` 与 `alice` 可以同时存在，
  而登录查询会随机命中其中一个 —— 那是一个真的会串账号的坑。
- 口令用与管理员同一套 ``scrypt`` 实现（``web.security``），只存哈希。
- **这里存的口令只是用户端账号的口令**，与「学校统一身份认证口令」无关；
  后者任何时候都不落盘（见 ``services.school_login``）。
"""

from __future__ import annotations

import logging
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from haut_seat.web import repository as repo
from haut_seat.web import security

logger = logging.getLogger("haut_seat.web.user_auth")

#: 用户端会话 Cookie。与管理端的 ``hs_session`` 名不同、表不同、令牌空间不同。
USER_SESSION_COOKIE = "hs_user"

USERNAME_MIN_LEN = 3
USERNAME_MAX_LEN = 32
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.\-]{2,31}$")

#: 邀请码长度（由管理端生成）。用 ``token_urlsafe`` 再削到易抄写的字符集。
INVITE_CODE_BYTES = 9          # 12 个 base64url 字符
INVITE_CODE_MIN_LEN = 6

_META_INVITE_CODE = "signup_invite_code"
_META_SIGNUP_ENABLED = "signup_enabled"

_FMT = "%Y-%m-%d %H:%M:%S"


class UserExistsError(RuntimeError):
    """用户名已被占用。"""


class UsernameError(ValueError):
    """用户名不合法（格式/长度）。消息面向用户，可直接展示。"""


@dataclass(frozen=True)
class UserSessionRecord:
    token_hash: str
    user_id: int
    csrf_token: str
    created_at: str
    expires_at: str
    last_seen_at: str
    username: str
    display_name: str
    status: str
    pending_captcha_uuid: str = ""
    pending_captcha_at: str | None = None

    @property
    def is_active(self) -> bool:
        return self.status == "active"


def _parse(text) -> datetime:
    try:
        return datetime.strptime(str(text), _FMT)
    except (ValueError, TypeError):
        return datetime.fromtimestamp(0)


def normalize_username(raw) -> str:
    """归一化用户名：去空白 + 转小写。存储与查询都走它，保证大小写不敏感唯一。"""
    return str(raw or "").strip().lower()


def validate_username(raw) -> str:
    """校验并返回归一化用户名；不合法抛 :class:`UsernameError`（消息可展示）。"""
    name = normalize_username(raw)
    if not name:
        raise UsernameError("用户名不能为空")
    if len(name) < USERNAME_MIN_LEN or len(name) > USERNAME_MAX_LEN:
        raise UsernameError(
            f"用户名长度必须在 {USERNAME_MIN_LEN}–{USERNAME_MAX_LEN} 个字符之间"
        )
    if not _USERNAME_RE.match(name):
        raise UsernameError("用户名只能用字母、数字、下划线、点或短横线，且以字母或数字开头")
    return name


# ------------------------------------------------------------------ 账号


def create_user(
    db,
    username: str,
    password: str,
    *,
    display_name: str = "",
    now: datetime | None = None,
) -> int:
    """创建用户端账号，返回 user_id。

    口令只存 ``scrypt`` 哈希；用户名已在库里（大小写不敏感）时抛
    :class:`UserExistsError` —— 由调用方转成面向用户的提示。
    """
    name = validate_username(username)
    problems = security.validate_password_strength(password)
    if problems:
        raise ValueError("；".join(problems))
    stamp = _fmt_now(now)
    with db.tx() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO app_user (username, password_hash, display_name, status,"
                " created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?)",
                (name, security.hash_password(password),
                 str(display_name or "").strip()[:40], stamp, stamp),
            )
        except sqlite3.IntegrityError as exc:
            raise UserExistsError(f"用户名 {name} 已被占用") from exc
        return int(cur.lastrowid)


def get_user(db, user_id: int) -> dict | None:
    row = db.query_one(
        "SELECT id, username, display_name, status, created_at, updated_at, last_login_at"
        " FROM app_user WHERE id = ?",
        (int(user_id),),
    )
    return _user_to_dict(row) if row else None


def find_user(db, username: str) -> dict | None:
    """按（归一化后的）用户名查账号；**绝不返回口令哈希**。"""
    row = db.query_one(
        "SELECT id, username, display_name, status, created_at, updated_at, last_login_at"
        " FROM app_user WHERE username = ?",
        (normalize_username(username),),
    )
    return _user_to_dict(row) if row else None


def find_user_with_hash(db, username: str):
    """登录校验专用：额外返回 ``password_hash``（内部使用，绝不进模板上下文）。"""
    return db.query_one(
        "SELECT id, username, display_name, status, password_hash FROM app_user"
        " WHERE username = ?",
        (normalize_username(username),),
    )


def list_users(db) -> list[dict]:
    """全部用户端账号（含计划数与凭据状态，供管理端一页看全）。"""
    rows = db.query_all(
        "SELECT u.id, u.username, u.display_name, u.status, u.created_at, u.updated_at,"
        " u.last_login_at,"
        " (SELECT COUNT(*) FROM user_plan up WHERE up.user_id = u.id) AS plan_count,"
        " (SELECT c.school_username FROM user_credential c WHERE c.user_id = u.id)"
        "   AS school_username,"
        " (SELECT c.status FROM user_credential c WHERE c.user_id = u.id) AS cred_status,"
        " (SELECT c.fingerprint FROM user_credential c WHERE c.user_id = u.id)"
        "   AS cred_fingerprint,"
        " (SELECT c.captured_at FROM user_credential c WHERE c.user_id = u.id)"
        "   AS cred_captured_at"
        " FROM app_user u ORDER BY u.id"
    )
    out = []
    for row in rows:
        item = _user_to_dict(row)
        item.update(
            {
                "plan_count": int(row["plan_count"]),
                # 学校账号是**标识**（学号/工号），不是口令；管理端只展示它，
                # 用来回答"这个人的计划用的是哪个学校账号"。
                "school_username": row["school_username"] or "",
                "cred_status": row["cred_status"] or "none",
                "cred_fingerprint": row["cred_fingerprint"] or "",
                "cred_captured_at": row["cred_captured_at"],
            }
        )
        out.append(item)
    return out


def count_users(db) -> int:
    row = db.query_one("SELECT COUNT(*) AS n FROM app_user")
    return int(row["n"]) if row else 0


def set_user_status(db, user_id: int, status: str, *, now: datetime | None = None) -> None:
    """启用/停用账号。停用时**同时删掉它的全部会话**，立即生效。"""
    value = "active" if str(status) == "active" else "disabled"
    with db.tx() as conn:
        conn.execute(
            "UPDATE app_user SET status = ?, updated_at = ? WHERE id = ?",
            (value, _fmt_now(now), int(user_id)),
        )
        if value != "active":
            conn.execute("DELETE FROM user_session WHERE user_id = ?", (int(user_id),))


def set_user_password(
    db, user_id: int, password: str, *, now: datetime | None = None
) -> None:
    problems = security.validate_password_strength(password)
    if problems:
        raise ValueError("；".join(problems))
    with db.tx() as conn:
        conn.execute(
            "UPDATE app_user SET password_hash = ?, updated_at = ? WHERE id = ?",
            (security.hash_password(password), _fmt_now(now), int(user_id)),
        )
        # 改密后让该用户所有会话失效（与管理员侧同一约定）
        conn.execute("DELETE FROM user_session WHERE user_id = ?", (int(user_id),))


def delete_user(db, user_id: int) -> None:
    """删除账号。**计划与运行记录都不删**：归属行随账号级联消失，
    计划随即按「无主/管理端」解释（与 plan_origin 同一套约定），
    因此"今天已经提交过"的证据一条都不会丢。"""
    with db.tx() as conn:
        conn.execute("DELETE FROM app_user WHERE id = ?", (int(user_id),))


def touch_user_login(db, user_id: int, *, now: datetime | None = None) -> None:
    with db.tx() as conn:
        conn.execute(
            "UPDATE app_user SET last_login_at = ? WHERE id = ?",
            (_fmt_now(now), int(user_id)),
        )


def _user_to_dict(row) -> dict:
    return {
        "id": int(row["id"]),
        "username": row["username"],
        "display_name": row["display_name"] or "",
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_login_at": row["last_login_at"],
    }


# ------------------------------------------------------------------ 会话


def create_user_session(
    db, user_id: int, *, now: datetime | None = None, ttl_hours: int = None,
    idle_minutes: int = None,
) -> tuple[str, str]:
    """创建用户端会话，返回 ``(明文令牌, CSRF 令牌)``。明文令牌只出现在 Cookie 里。"""
    moment = now or datetime.now()
    token = security.new_session_token()
    csrf = security.new_csrf_token()
    ttl = timedelta(hours=int(ttl_hours or security.SESSION_TTL_HOURS))
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO user_session (token_hash, user_id, csrf_token, created_at,"
            " expires_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                security.token_digest(token), int(user_id), csrf,
                moment.strftime(_FMT), (moment + ttl).strftime(_FMT),
                moment.strftime(_FMT),
            ),
        )
    return token, csrf


def load_user_session(
    db, token: str, *, now: datetime | None = None, idle_minutes: int | None = None
) -> UserSessionRecord | None:
    """校验用户端会话；无效、过期、空闲超时或账号被停用一律返回 ``None``。"""
    if not token or len(token) > 200:
        return None
    moment = now or datetime.now()
    row = db.query_one(
        "SELECT s.token_hash, s.user_id, s.csrf_token, s.created_at, s.expires_at,"
        " s.last_seen_at, s.pending_captcha_uuid, s.pending_captcha_at,"
        " u.username, u.display_name, u.status"
        " FROM user_session s JOIN app_user u ON u.id = s.user_id"
        " WHERE s.token_hash = ?",
        (security.token_digest(token),),
    )
    if row is None:
        return None
    # 账号被停用 ⇒ 会话立即作废（不等到过期）。这是"停用"该有的即时性。
    if str(row["status"]) != "active":
        destroy_user_session(db, token)
        return None
    if _parse(row["expires_at"]) <= moment:
        return None
    idle = timedelta(minutes=int(
        idle_minutes if idle_minutes is not None else security.SESSION_IDLE_MINUTES
    ))
    if moment - _parse(row["last_seen_at"]) > idle:
        return None
    with db.tx() as conn:
        conn.execute(
            "UPDATE user_session SET last_seen_at = ? WHERE token_hash = ?",
            (moment.strftime(_FMT), row["token_hash"]),
        )
    return UserSessionRecord(
        token_hash=row["token_hash"],
        user_id=int(row["user_id"]),
        csrf_token=row["csrf_token"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        last_seen_at=row["last_seen_at"],
        username=row["username"],
        display_name=row["display_name"] or "",
        status=row["status"],
        pending_captcha_uuid=row["pending_captcha_uuid"] or "",
        pending_captcha_at=row["pending_captcha_at"],
    )


def destroy_user_session(db, token: str) -> None:
    if not token:
        return
    with db.tx() as conn:
        conn.execute(
            "DELETE FROM user_session WHERE token_hash = ?",
            (security.token_digest(token),),
        )


def purge_expired_user_sessions(db, *, now: datetime | None = None, idle_minutes=None) -> int:
    moment = now or datetime.now()
    idle = int(idle_minutes if idle_minutes is not None else security.SESSION_IDLE_MINUTES)
    cutoff = (moment - timedelta(minutes=idle)).strftime(_FMT)
    with db.tx() as conn:
        cur = conn.execute(
            "DELETE FROM user_session WHERE expires_at <= ? OR last_seen_at <= ?",
            (moment.strftime(_FMT), cutoff),
        )
        return cur.rowcount or 0


# ---- 会话里挂着的「本次绑定用的验证码标识」----


def set_pending_captcha(
    db, token_hash: str, uuid: str, *, now: datetime | None = None
) -> None:
    with db.tx() as conn:
        conn.execute(
            "UPDATE user_session SET pending_captcha_uuid = ?, pending_captcha_at = ?"
            " WHERE token_hash = ?",
            (str(uuid or "")[:64], _fmt_now(now), str(token_hash)),
        )


def clear_pending_captcha(db, token_hash: str) -> None:
    with db.tx() as conn:
        conn.execute(
            "UPDATE user_session SET pending_captcha_uuid = '', pending_captcha_at = NULL"
            " WHERE token_hash = ?",
            (str(token_hash),),
        )


def pending_captcha_fresh(
    session: UserSessionRecord, *, ttl_s: float, now: datetime | None = None
) -> bool:
    """会话里那张验证码是否还在有效期内（不能假装学校那侧的验证码永远有效）。"""
    if not session.pending_captcha_uuid or not session.pending_captcha_at:
        return False
    moment = now or datetime.now()
    return (moment - _parse(session.pending_captcha_at)).total_seconds() < float(ttl_s)


# ------------------------------------------------------------------ 登录限流


def record_user_login_attempt(
    db, username: str, ip: str, ok: bool, *, now: datetime | None = None
) -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO user_login_attempt (username, ip, ok, at) VALUES (?, ?, ?, ?)",
            (normalize_username(username), str(ip or ""), 1 if ok else 0, _fmt_now(now)),
        )


def user_login_locked_until(
    db, username: str, ip: str, *, now: datetime | None = None
) -> datetime | None:
    """按「用户名 + IP」双维度限流；最近一次成功登录会清零计数（与管理员侧同一策略）。"""
    moment = now or datetime.now()
    window_start = _fmt(moment - timedelta(minutes=security.LOGIN_WINDOW_MINUTES))
    rows = db.query_all(
        "SELECT ok, at FROM user_login_attempt"
        " WHERE (username = ? OR ip = ?) AND at >= ? ORDER BY at ASC",
        (normalize_username(username), str(ip or ""), window_start),
    )
    if not rows:
        return None
    failures = 0
    for row in rows:
        failures = 0 if row["ok"] else failures + 1
    if failures < security.LOGIN_MAX_FAILURES or rows[-1]["ok"]:
        return None
    unlock = _parse(rows[-1]["at"]) + timedelta(minutes=security.LOGIN_LOCK_MINUTES)
    return unlock if unlock > moment else None


def purge_old_user_login_attempts(db, *, now: datetime | None = None) -> int:
    moment = now or datetime.now()
    cutoff = _fmt(moment - timedelta(days=1))
    with db.tx() as conn:
        cur = conn.execute("DELETE FROM user_login_attempt WHERE at < ?", (cutoff,))
        return cur.rowcount or 0


# ------------------------------------------------------------------ 邀请码 / 注册开关


def new_invite_code() -> str:
    """生成一个便于手抄的邀请码（base64url 字符集，无易混的大小写歧义处理）。"""
    return secrets.token_urlsafe(INVITE_CODE_BYTES)


def effective_invite_code(db, settings) -> str:
    """当前生效的邀请码。

    以**数据库里的值**为准（管理员可以在页面上随时换一个），
    数据库里没有时才用 ``HAUT_WEB_SIGNUP_INVITE_CODE`` 作为默认值。
    两者都没有 ⇒ 返回空串 ⇒ 注册不可用（页面如实说明，不悄悄放行）。
    """
    stored = str(repo.get_meta(db, _META_INVITE_CODE, "") or "").strip()
    if stored:
        return stored
    return str(getattr(settings, "web_signup_invite_code", "") or "").strip()


def signup_enabled(db, settings) -> bool:
    """注册是否开放：``HAUT_WEB_SIGNUP_ENABLED`` 与数据库开关**同时**为真才开放。

    环境变量是部署侧的"急停"，数据库开关是管理端的日常开关，两者都不能单独绕过对方。
    """
    if not bool(getattr(settings, "web_signup_enabled", True)):
        return False
    return str(repo.get_meta(db, _META_SIGNUP_ENABLED, "1")) == "1"


def set_signup_enabled(db, enabled: bool, *, now: datetime | None = None) -> None:
    repo.set_meta(db, _META_SIGNUP_ENABLED, "1" if enabled else "0", now=now)


def rotate_invite_code(db, *, now: datetime | None = None) -> str:
    """生成并保存一个新的邀请码（旧的立即失效）。"""
    code = new_invite_code()
    repo.set_meta(db, _META_INVITE_CODE, code, now=now)
    return code


def invite_code_matches(expected: str, provided) -> bool:
    """常量时间比对邀请码；任何一侧为空都不算匹配。"""
    return security.csrf_matches(str(expected or ""), str(provided or "").strip())


def _fmt(moment: datetime) -> str:
    return moment.strftime(_FMT)


def _fmt_now(now: datetime | None) -> str:
    return _fmt(now or datetime.now())