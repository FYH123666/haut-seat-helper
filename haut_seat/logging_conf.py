"""日志配置：统一格式 + 敏感信息脱敏。"""

from __future__ import annotations

import logging
import re
from pathlib import Path

# 敏感键名（用于防御性脱敏）
_SECRET_KEY = r"(authorization|cookie|token|password|passwd|secret|set-cookie)"

# 冒号形态：header 或 dict 风格（"Key: value" / "'key': 'value'"，值可取到行尾/逗号）
_RE_COLON = re.compile(r"(?i)\b" + _SECRET_KEY + r"['\"]?\s*:\s*[^\r\n,]*")
# 等号形态：query 参数（"key=value"，值取到空白/&/;）
_RE_EQUALS = re.compile(r"(?i)\b" + _SECRET_KEY + r"\s*=\s*[^\s&,;]*")

#: 第三方网络栈的 logger。它们**没有**装脱敏 formatter（脱敏只装在 ``haut_seat`` 上），
#: 因此必须用"级别下限"来掐掉最危险的那一类输出。
#:
#: 实测依据（本机 loopback 服务器，零外网请求）：把 ``HAUT_LOG_LEVEL`` 设成 DEBUG 时，
#: ``httpcore`` 会把**完整的响应头列表逐字打印**：
#:
#:     httpcore.http11|DEBUG|receive_response_headers.complete return_value=
#:         (b'HTTP/1.0', 200, b'OK', [(b'Connection', b'close'), ...])
#:
#: 请求头的**值**不会打印（好），但响应头列表会 ⇒ 学校若以 ``Set-Cookie`` 下发新令牌，
#: DEBUG 级别下它会原样落进 ``logs/haut_seat.log``。而 ``redact_text`` **挡不住**这种
#: ``(b'Set-Cookie', b'...')`` 的元组形态（它认的是 ``key: value`` 与 ``key=value``），
#: 所以这里不假装"脱敏过了"，而是从源头把记录本身掐掉。
#:
#: 级别取 **INFO**（而不是 WARNING）是有意的：``httpx`` 在 INFO 上只打
#: ``HTTP Request: GET <url> "HTTP/1.0 200 OK"`` —— 方法、URL、状态码，没有头。
#: 既保住排查网络问题需要的那一行摘要，又不留逐字头转储。
_THIRD_PARTY_NETWORK_LOGGERS = (
    "httpx",
    "httpcore",
    "urllib3",
    "h11",
    "h2",
    "socksio",
)

#: 第三方网络 logger 的级别下限。
THIRD_PARTY_NETWORK_FLOOR = logging.INFO


def redact_text(text: str) -> str:
    """将文本中敏感键的值替换为 <redacted>。

    仅作防御性兜底，不构成绝对保证；首要防线仍是「不记录敏感信息」。
    """
    text = _RE_COLON.sub(r"\1: <redacted>", text)
    text = _RE_EQUALS.sub(r"\1=<redacted>", text)
    return text


class RedactingFormatter(logging.Formatter):
    """将日志中可能出现的敏感键值对替换为 <redacted>。"""

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


def harden_third_party_loggers() -> None:
    """把第三方网络栈的 logger 抬到至少 INFO 级，掐掉请求/响应头的逐字转储。

    幂等、可重复调用；理由与实测依据见 ``_THIRD_PARTY_NETWORK_LOGGERS`` 上方的注释。
    用户显式把级别调得更高（如 WARNING）不受影响 —— 只在"低于下限"时抬起来。
    """
    for name in _THIRD_PARTY_NETWORK_LOGGERS:
        logger = logging.getLogger(name)
        current = logger.level
        if current == logging.NOTSET or current < THIRD_PARTY_NETWORK_FLOOR:
            logger.setLevel(THIRD_PARTY_NETWORK_FLOOR)


def setup_logging(level: str = "INFO", log_file: str | None = None) -> logging.Logger:
    """初始化并返回 haut_seat 日志器。重复调用不会叠加 handler。

    同时把第三方网络 logger 抬到 ``THIRD_PARTY_NETWORK_FLOOR``：即使
    ``HAUT_LOG_LEVEL=DEBUG``，``httpcore`` 也不会把响应头列表（可能含
    ``Set-Cookie``）逐字写进日志文件。
    """
    harden_third_party_loggers()

    logger = logging.getLogger("haut_seat")
    logger.setLevel(level.upper())
    logger.propagate = False

    if logger.handlers:
        return logger

    fmt = RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    return logger
