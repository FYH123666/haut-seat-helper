"""按时段（区域 + 使用时段）分片的**跨进程**文件锁。

## 为什么要把全局锁换成按时段分片

预约提交真正冲突的**共享资源**是「某个区域的某个时段」，而不是整个进程。原来的实现
让每个预约任务都去抢同一把全局文件锁（``HAUT_BOOK_LOCK_FILE``，默认 ``data/book.lock``），
后果是两个：

- 全进程同一时刻**只能有一个**预约任务在跑，``HAUT_SCHEDULER_MAX_CONCURRENT`` 形同虚设；
- 一个任务在窗口里等触发时刻（可能几十分钟）期间，**完全不相干的**其它任务也全部
  被挡在门外 —— 这是「不同预约任务互相拖慢」最主要的来源。

现在锁名由「区域 + 时段」派生：**同一个时段仍然只允许一个进程持有锁**（跨进程保护
一分未减），而不同时段的任务各拿各的锁，可以真正并行。

## 不可让步的几条约束

1. **跨进程能力必须保留**：用的还是 ``core.lock.FileLock``（``O_CREAT|O_EXCL`` 的原子
   文件创建），只是文件名不同 —— **不换成线程锁**。CLI 的 ``book`` 与 Web 调度器共用
   同一套命名，因此「cron 里的 CLI」和「Web 调度器」抢同一个时段时仍然会撞车。
2. **有界等待，绝不无限等**：``wait_s`` 到期即放弃（返回 ``None``），由调用方记日志
   并跳过本轮；下一轮调度会重新尝试，所以不会漏掉窗口。
3. **时段算不出锁名时回退全局锁**：宁可保守（退化成从前的串行行为），也绝不因为
   算不出锁名就无锁提交。
4. ``stale_after_s`` 必须**大于一次预约运行的最长合理时长**：运行器会在窗口内一直
   重试，锁被持有的时间远超 ``FileLock`` 默认的 300 秒。取小了会让第二个进程在第一个
   进程还在提交时把锁抢走（重复提交）；取大了只会让残留锁多留一会儿，而残留锁只影响
   同一个时段、且会被下一次调度重试自然绕过。
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

from haut_seat.core.lock import FileLock

#: 一次预约运行最长合理持锁时长（秒）。默认 1 小时：远大于任何一次窗口内重试的耗时，
#: 又不至于让被强杀的进程留下的残留锁挡住一整天（残留锁只影响同一个时段）。
DEFAULT_STALE_AFTER_S = 3600.0

#: 默认等待时长（秒）。**有界**：等不到就放弃本轮，由下一次调度重试。
DEFAULT_WAIT_S = 5.0

#: 轮询间隔（秒）。文件锁没有阻塞语义，只能探测。
_POLL_S = 0.2


def slot_key(regionid, start, end) -> str:
    """区域 + 时段 → 稳定的短锁名（sha1 前 12 位）。

    用摘要而不是拼接原文，是为了避开文件名里的 ``:`` ``/`` 等非法字符
    （时段形如 ``2026-10-18 08:00:00``）以及过长的文件名。
    """
    raw = f"{int(regionid)}|{start}|{end}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def slot_lock_path(settings, *, regionid, start, end) -> Path:
    """该时段应使用的锁文件路径。

    任何一项缺失/不可解析 ⇒ 返回**全局锁文件**（``book_lock_file`` 原值）：
    这时行为与从前逐字节相同（全进程串行），不会出现"算不出锁名就无锁提交"。
    """
    base = Path(str(getattr(settings, "book_lock_file", "data/book.lock")))
    if regionid is None or not start or not end:
        return base
    try:
        key = slot_key(regionid, start, end)
    except (TypeError, ValueError):
        return base
    return base.with_name(f"{base.stem}-{key}{base.suffix}")


def acquire_slot_lock(
    settings,
    *,
    regionid,
    start,
    end,
    wait_s: float = DEFAULT_WAIT_S,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
    logger: logging.Logger | None = None,
    plan_id: int | None = None,
    clock=time.monotonic,
    sleep=time.sleep,
) -> FileLock | None:
    """获取该时段的跨进程锁；在 ``wait_s`` 内拿不到则返回 ``None``。

    ``clock`` / ``sleep`` 可注入，测试里不必真的等待。
    """
    path = slot_lock_path(settings, regionid=regionid, start=start, end=end)
    deadline = clock() + max(0.0, float(wait_s))
    waited = False
    while True:
        lock = FileLock(path, stale_after_s=stale_after_s)
        if lock.acquire():
            if waited and logger is not None:
                logger.info(
                    "计划 #%s 已等到时段锁 %s（等待后成功，未超过 %.1fs）",
                    plan_id, path, wait_s,
                )
            return lock
        if clock() >= deadline:
            if logger is not None:
                logger.warning(
                    "计划 #%s 本轮放弃：时段锁 %s 被占用，等待 %.1fs 仍未释放"
                    "（同一时段的另一个预约任务正在运行；下一轮会重试）",
                    plan_id, path, wait_s,
                )
            return None
        if not waited:
            waited = True
            if logger is not None:
                logger.info(
                    "计划 #%s 正在等待时段锁 %s（最多 %.1fs）", plan_id, path, wait_s
                )
        sleep(_POLL_S)


def slot_lock_is_free(
    settings,
    *,
    regionid,
    start,
    end,
    stale_after_s: float = DEFAULT_STALE_AFTER_S,
    clock=time.time,
) -> bool:
    """**只读**探测：锁文件不存在（或已成残留）⇒ 该时段空闲。

    **刻意不做「创建后立即删除」的探测。** 那看起来更权威，实际会伤到别人：

    1. 探测会真的创建/删除那把**共享的**锁文件，于是同一轮 tick 里另一个任务的
       权威获取（``acquire_slot_lock``，等待上限可能是 0）会在探测留下的痕迹上
       失败，本轮直接放弃 —— 两个同时段的计划可能双双落空。锁是共享资源，探测
       不该写它；
    2. Windows 上并发创建/删除同一路径会让 ``O_CREAT|O_EXCL`` 返回 EACCES，
       探测于是抛 ``PermissionError`` 而不是返回「占用」。

    存在性判断同样是**提示性**的（探测与获取之间本来就有空隙，这里不假装它是
    原子的，文档末尾也一直这么写），但它是无副作用的，因此不会把别的任务搅乱。
    权威判定仍然只在 :func:`acquire_slot_lock` 里。
    """
    path = slot_lock_path(settings, regionid=regionid, start=start, end=end)
    try:
        age = clock() - path.stat().st_mtime
    except FileNotFoundError:
        return True
    except OSError:
        # 状态读不出来时保守地按「占用」处理：宁可本轮不启动，也不无锁提交。
        return False
    return age > stale_after_s