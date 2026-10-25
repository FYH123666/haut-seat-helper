"""文件锁测试：并发互斥与残留锁清理。"""

from __future__ import annotations

import os
import time

from haut_seat.core.lock import DEFAULT_STALE_S, FileLock


def test_lock_acquire_release(tmp_path):
    path = tmp_path / "book.lock"
    lock = FileLock(path)
    assert lock.acquire() is True
    assert path.exists()
    lock.release()
    assert not path.exists()


def test_lock_blocks_second_acquisition(tmp_path):
    path = tmp_path / "book.lock"
    l1 = FileLock(path)
    assert l1.acquire() is True
    l2 = FileLock(path)
    assert l2.acquire() is False
    l1.release()


def test_lock_stale_is_retaken(tmp_path):
    path = tmp_path / "book.lock"
    path.write_text("x")
    old = time.time() - DEFAULT_STALE_S - 10
    os.utime(path, (old, old))
    lock = FileLock(path)
    assert lock.acquire() is True
    lock.release()
    assert not path.exists()
