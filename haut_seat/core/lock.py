"""进程级文件锁：防止定时任务重复触发/并发执行。"""

from __future__ import annotations

import os
import time
from pathlib import Path

DEFAULT_STALE_S = 300  # 锁文件超过该秒数视为残留（进程被强杀），可覆盖


class FileLock:
    """基于 O_CREAT|O_EXCL 的原子文件锁。用法：显式 acquire/release。"""

    def __init__(self, path: str | Path, stale_after_s: float = DEFAULT_STALE_S):
        self.path = Path(path)
        self.stale_after_s = stale_after_s
        self._acquired = False

    def acquire(self) -> bool:
        """尝试获取锁；返回 False 表示已有其它实例持有（未过期）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            self._acquired = True
            return True
        except (FileExistsError, PermissionError) as exc:
            # Windows 上并发创建/删除同一个锁文件时，``O_CREAT|O_EXCL`` 会返回
            # EACCES（``PermissionError``）而不是 EEXIST —— 那**不是**权限问题，
            # 而是「有人正在动这个路径」。把它当成「已被占用」处理，否则一个并发的
            # 探测会变成抛异常：手动执行接口会 500，运行线程会整轮放弃。
            # 文件确实不存在却报权限错误，才是真实的环境问题（目录不可写等），
            # 那种情况必须原样抛出，不能悄悄吞掉。
            if isinstance(exc, PermissionError) and not self.path.exists():
                raise
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError:
                age = 0.0
            if age > self.stale_after_s:
                # 残留锁（进程被强杀），清掉后重试一次
                try:
                    self.path.unlink(missing_ok=True)
                except OSError:
                    pass
                return self.acquire()
            return False

    def release(self) -> None:
        if self._acquired:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
            self._acquired = False
