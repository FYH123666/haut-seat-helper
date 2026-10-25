"""日志脱敏与第三方网络 logger 加固测试。

两件事分开测，因为它们是两种不同的机制：

- ``haut_seat`` 自己的日志走 ``RedactingFormatter``（**改内容**）；
- 第三方网络栈（httpx / httpcore / …）**没有**脱敏 formatter，所以用
  **级别下限**掐掉最危险的那类输出（**不改内容，直接不产生记录**）。

依据是实测结论（见 logs 与 ``logging_conf`` 的注释）：``httpcore`` 在 DEBUG 级别会
把完整的响应头列表逐字打印，而 ``redact_text`` 挡不住 ``(b'Set-Cookie', b'...')``
这种元组形态 —— 所以不假装"脱敏过了"，而是从源头掐掉。
"""

from __future__ import annotations

import logging

import pytest

from haut_seat.logging_conf import (
    THIRD_PARTY_NETWORK_FLOOR,
    RedactingFormatter,
    harden_third_party_loggers,
    redact_text,
    setup_logging,
)

# 哨兵：形状像真令牌，但显然是测试专用值
SENTINEL = "SENTINELTOKEN-._0005-abcdefghijklmnop"


def test_redact_header_style():
    assert redact_text("Authorization: Bearer abc123") == "Authorization: <redacted>"


def test_redact_full_cookie():
    # 完整 Cookie 头可能含多组 key=value，应整体脱敏
    assert redact_text("Cookie: a=b; c=d") == "Cookie: <redacted>"


def test_redact_query_param():
    assert redact_text("token=abc123") == "token=<redacted>"


def test_redact_dict_repr():
    assert "abc123" not in redact_text("{'token': 'abc123'}")


def test_redact_password():
    assert "hunter2" not in redact_text("password=hunter2")


def test_redact_leaves_normal_text_untouched():
    assert redact_text("GET /stage-api/... -> 200") == "GET /stage-api/... -> 200"


def test_formatter_redacts_message():
    rec = logging.LogRecord("x", logging.INFO, "p", 1, "token=secret123", None, None)
    out = RedactingFormatter("%(message)s").format(rec)
    assert "secret123" not in out


# ================================================================== 第三方网络 logger


class RecordingHandler(logging.Handler):
    """只把记录收进列表，不做任何格式化。"""

    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - 一行
        self.records.append(record)


def capture_records(logger_name: str):
    """临时挂一个收集器；返回 (handler, 清理函数)。"""
    handler = RecordingHandler()
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    return handler, lambda: logger.removeHandler(handler)


@pytest.fixture(autouse=True)
def _restore_logger_levels():
    """加固会改全局 logger 级别 —— 用例结束后恢复，避免污染其它测试。"""
    names = ("httpx", "httpcore", "httpcore.http11", "urllib3", "h11", "h2", "socksio")
    saved = {name: logging.getLogger(name).level for name in names}
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


def test_httpcore_debug_header_dump_is_suppressed():
    """核心断言：即使在 DEBUG 全局级别下，httpcore 的响应头转储也不会产生记录。

    ``httpcore`` 在 DEBUG 上会打 ``receive_response_headers.complete return_value=``
    那一行；那里会带上完整的响应头列表。学校若以 ``Set-Cookie`` 下发令牌，
    它就落进日志了 —— 必须掐掉。
    """
    setup_logging(level="DEBUG")
    handler, detach = capture_records("httpcore.http11")
    try:
        logger = logging.getLogger("httpcore.http11")
        logger.debug(
            "receive_response_headers.complete return_value=(b'HTTP/1.0', 200, b'OK',"
            " [(b'Connection', b'close'), (b'Set-Cookie', b'Admin-Token=%s')])",
            SENTINEL,
        )
        assert handler.records == [], "DEBUG 级别的响应头转储必须被彻底掐掉"
    finally:
        detach()


def test_httpx_info_summary_still_comes_through():
    """正对照：级别是抬到 INFO，不是全关 —— 排查网络问题要的那行摘要还在。"""
    setup_logging(level="DEBUG")
    handler, detach = capture_records("httpx")
    try:
        logging.getLogger("httpx").info(
            'HTTP Request: GET http://127.0.0.1:1/probe "HTTP/1.0 200 OK"'
        )
        assert len(handler.records) == 1, "INFO 的请求摘要必须保留"
        assert "HTTP Request" in handler.records[0].getMessage()
    finally:
        detach()


def test_the_floor_is_applied_to_every_network_logger():
    for name in ("httpx", "httpcore", "urllib3", "h11", "h2", "socksio"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    harden_third_party_loggers()
    for name in ("httpx", "httpcore", "urllib3", "h11", "h2", "socksio"):
        assert logging.getLogger(name).getEffectiveLevel() >= THIRD_PARTY_NETWORK_FLOOR


def test_hardening_never_lowers_a_stricter_level():
    """用户把某个 logger 调得更严（如 WARNING）时，不得给他放松回去。"""
    logging.getLogger("urllib3").setLevel(logging.ERROR)
    harden_third_party_loggers()
    assert logging.getLogger("urllib3").getEffectiveLevel() == logging.ERROR


def test_hardening_is_idempotent():
    harden_third_party_loggers()
    first = {n: logging.getLogger(n).level for n in ("httpx", "httpcore")}
    harden_third_party_loggers()
    assert {n: logging.getLogger(n).level for n in ("httpx", "httpcore")} == first


def test_setup_logging_alone_is_enough(tmp_path):
    """加固必须挂在 ``setup_logging`` 里 —— 只装脱敏 formatter 是不够的。"""
    setup_logging(level="DEBUG", log_file=str(tmp_path / "haut_seat.log"))
    assert logging.getLogger("httpcore").getEffectiveLevel() >= THIRD_PARTY_NETWORK_FLOOR


def test_every_handler_on_the_haut_seat_logger_redacts(tmp_path):
    """``haut_seat`` 这一侧靠的是**接线**：每个 handler 都挂 RedactingFormatter。

    这里断言接线本身而不是某条输出 —— ``setup_logging`` 幂等、handler 又持有
    创建时的 stream 对象，所以"打到当前 capsys 里"是不可靠的断言方式。
    接线断言才是稳定的，且恰好是与第三方 logger 形成对照的那一点：
    **它们一个脱敏 formatter 都没有**，所以才用级别下限兜底。
    """
    logger = setup_logging(level="DEBUG", log_file=str(tmp_path / "haut_seat.log"))
    assert logger.propagate is False, "不得冒泡到 root（root 上没有脱敏）"

    own = [
        h for h in logger.handlers
        # pytest 的 logging 插件会往"不冒泡"的 logger 上挂自己的捕获 handler，
        # 那是测试环境的东西，与生产接线无关，不参与这条断言。
        if not type(h).__module__.startswith("_pytest")
    ]
    assert own, "setup_logging 至少要挂一个自己的 handler"
    for handler in own:
        assert isinstance(handler.formatter, RedactingFormatter), (
            f"{handler} 没有挂脱敏 formatter"
        )


def test_third_party_loggers_have_no_redacting_formatter():
    """这是"为什么必须靠级别下限"的直接证据，不是猜测。"""
    setup_logging(level="DEBUG")
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        assert all(
            not isinstance(h.formatter, RedactingFormatter) for h in logger.handlers
        ), f"{name} 竟然挂了脱敏 formatter —— 那段注释就该改写了"