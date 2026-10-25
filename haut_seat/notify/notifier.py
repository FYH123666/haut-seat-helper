"""通知模块。第一阶段仅记录日志，不接入任何通知平台。"""

from __future__ import annotations

import logging


class Notifier:
    """预留通知接口。当前实现仅写入日志，后续可扩展 Server酱/飞书/邮件。"""

    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def send(self, level: str, message: str) -> None:
        """发送通知（当前仅写日志）。"""
        log = getattr(self.logger, level.lower(), self.logger.info)
        log(message)
