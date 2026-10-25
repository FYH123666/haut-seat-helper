"""响应体安全脱敏与结构化诊断。

用于在业务错误（HTTP 200 但业务 code 表示失败，且 msg 缺失/为空）时，
保留「字段结构 + 经安全处理的响应内容」，同时确保绝不泄漏凭据、令牌、
会话标识或个人信息。

本模块只处理服务器返回的响应体；请求头（Cookie/Authorization 等）的处理
见 ``haut_seat.client.http.redact_headers`` 与 ``haut_seat.logging_conf``。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 敏感键名（归一化小写、`-`/空格→`_` 后精确匹配）。
# 覆盖凭据/会话与常见个人信息两类。
_SENSITIVE_KEYS = frozenset({
    # 凭据 / 会话
    "token", "accesstoken", "refreshtoken", "cookie", "authorization", "auth",
    "password", "passwd", "secret", "session", "sessionid", "sid", "jwt",
    "apikey", "api_key", "accesskey", "access_key", "sign", "signature",
    "ticket", "credential", "set_cookie",
    # 个人信息
    "username", "user_name", "realname", "real_name", "name", "phone",
    "mobile", "tel", "telephone", "email", "mail", "idcard", "id_card",
    "identity", "studentno", "student_no", "stuid", "stu_id", "account",
    "person", "idnumber", "id_number", "cardno", "card_no",
})

# 复合键中的敏感子串（例如 access_token / X-Auth-Token / userPhone）
_SENSITIVE_SUBSTR = re.compile(
    r"(token|cookie|secret|password|passwd|authorization|credential|session|"
    r"phone|mobile|email|idcard|id_card|id_number|realname|real_name|"
    r"username|user_name|student)",
    re.IGNORECASE,
)

DEFAULT_MAX_DEPTH = 8
DEFAULT_MAX_ITEMS = 100      # 列表/字典最多保留的条目数
DEFAULT_MAX_STR_LEN = 200    # 单个字符串值最大长度
DEFAULT_MAX_BODY_CHARS = 8000  # 序列化后的诊断文本总长度上限


def _normalize_key(key: Any) -> str:
    return str(key).strip().lower().replace("-", "_").replace(" ", "_")


def is_sensitive_key(key: Any) -> bool:
    n = _normalize_key(key)
    return n in _SENSITIVE_KEYS or bool(_SENSITIVE_SUBSTR.search(n))


def sanitize_json(
    value: Any,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_items: int = DEFAULT_MAX_ITEMS,
    max_str_len: int = DEFAULT_MAX_STR_LEN,
) -> Any:
    """递归脱敏并截断，返回可安全记录的 JSON 结构。

    - 敏感键的值替换为 ``"<redacted>"``；
    - 字符串截断到 ``max_str_len``；
    - 列表/字典截断到 ``max_items``；
    - 超过 ``max_depth`` 的部分折叠为占位符。
    """
    return _sanitize(
        value, depth=0, max_depth=max_depth,
        max_items=max_items, max_str_len=max_str_len,
    )


def _sanitize(value, *, depth, max_depth, max_items, max_str_len):
    if depth > max_depth:
        return f"…(深度超限 depth>{max_depth})"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= max_str_len:
            return value
        return value[:max_str_len] + f"…(len={len(value)})"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= max_items:
                out["…"] = f"省略 {len(value) - max_items} 字段"
                break
            ks = str(k)
            if is_sensitive_key(k):
                out[ks] = "<redacted>"
            else:
                out[ks] = _sanitize(
                    v, depth=depth + 1, max_depth=max_depth,
                    max_items=max_items, max_str_len=max_str_len,
                )
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for i, item in enumerate(value):
            if i >= max_items:
                out.append(f"…(省略 {len(value) - max_items} 项)")
                break
            out.append(_sanitize(
                item, depth=depth + 1, max_depth=max_depth,
                max_items=max_items, max_str_len=max_str_len,
            ))
        return out
    s = str(value)
    return s[:max_str_len]


def sanitize_to_text(value: Any, *, limit: int = DEFAULT_MAX_BODY_CHARS) -> str:
    """把响应体脱敏后序列化为受限长度的诊断文本。"""
    safe = sanitize_json(value)
    text = json.dumps(safe, ensure_ascii=False, default=str)
    if len(text) > limit:
        text = text[:limit] + f"…(诊断文本截断 len={len(text)})"
    return text
