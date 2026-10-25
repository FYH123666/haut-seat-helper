"""预约时间合法性检查 + 重复计划的执行窗口滚动（纯函数，仅告警，不自动改写时间）。

只依据「已确认规则」与已观察到的服务器行为给出提示，不臆测边界：
- 图书馆开放时间 07:30–22:00；预约时间 07:00–21:30（官方规则，已确认）。
- 读者只可预约当天座位（官方规则，已确认）。
- 服务器曾返回 code=404「开始时间与结束时间必须是同一天」、
  code=1111「预约开始时间必须大于结束时间」/「只能预约1天之内的时间(含当天)」。

未确认的边界（自然日边界、跨零点、是否 24 小时滚动）不在此判断。

``effective_window`` 单独放在这里，是为了让「窗口日期怎么算」只有一处实现：
调度器里有四个地方要用同一个窗口（启动判断、提交参数、手动执行、界面提示），
分散计算会让它们互相不一致 —— 与 ``core/catalog.py`` 集中推断规则同一套理由。
"""

from __future__ import annotations

from datetime import date, datetime

_BOOKING_OPEN = "07:00"
_BOOKING_CLOSE = "21:30"

_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


def _parse(s: str) -> datetime | None:
    s = (s or "").strip()
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def check_time_validity(start: str, end: str, now: datetime | None = None) -> list[str]:
    """返回时间合法性问题列表；空列表表示未发现明显问题。"""
    now = now or datetime.now()
    warnings: list[str] = []
    st = _parse(start)
    en = _parse(end)
    if st is None or en is None:
        warnings.append(
            f"时间格式无法解析（start={start!r}, end={end!r}），期望 'YYYY-MM-DD HH:MM:SS'"
        )
        return warnings
    if st >= en:
        warnings.append("开始时间必须早于结束时间（服务器曾返回 code=1111）")
    if st.date() != en.date():
        warnings.append("开始时间与结束时间必须是同一天（服务器曾返回 code=404）")
    if st.date() != now.date():
        warnings.append(f"只能预约当天（今天是 {now.date()}），查询日期为 {st.date()}")
    if st.strftime("%H:%M") < _BOOKING_OPEN or en.strftime("%H:%M") > _BOOKING_CLOSE:
        warnings.append(f"预约时间窗口为 {_BOOKING_OPEN}–{_BOOKING_CLOSE}")
    if st <= now:
        warnings.append("开始时间不得早于当前时间")
    return warnings


def effective_window(
    book_start: str, book_end: str, *, repeat_rule: str, today: date
) -> tuple[str, str]:
    """重复计划（每天 / 工作日）**当天实际**的执行与使用窗口。

    ``plan.book_start`` / ``book_end`` 里带的日期是**建计划那天**的日期，而
    ``_rearm_repeating`` 只把状态改回「待执行」，从不改写这两列。若沿用原日期，
    重复计划在次日就会被 ``now >= end_ts`` 判成「窗口已过」而永远不再执行 ——
    界面同时显示「待执行」和「已过预约窗口」两句互相矛盾的话。

    因此这里把窗口的**日期部分**滚动到 ``today``，时刻部分原样保留。

    ``once`` 计划原样返回：它的 ``run_date`` 与窗口日期本就由 ``parse_plan``
    强制相等，滚动等于不变，行为逐字节保持。

    任一时间无法解析时也原样返回，交给调用方按「时间参数不合法」处理。
    """
    if repeat_rule == "once":
        return book_start, book_end
    st = _parse(book_start)
    en = _parse(book_end)
    if st is None or en is None:
        return book_start, book_end
    day = today.strftime("%Y-%m-%d")
    return f"{day} {st:%H:%M:%S}", f"{day} {en:%H:%M:%S}"


def booking_opens_at(window_start: str) -> datetime | None:
    """``window_start`` **当天**预约系统开放的时刻（07:00）；解析不出返回 ``None``。

    来源是模块开头那条已确认的官方规则（``_BOOKING_OPEN``）：07:00 之前提交
    必然被服务器拒绝。它的用途因此只有一个 —— 给 ``booking_runner`` 一个
    「现在还不能提交」的判据，使提前启动或窗口内补跑**不可能**把提交提前到
    开放之前。

    它**不是**「开放后就能约到」的证明：过了 07:00 能不能约到，仍然只由实时
    查询结果决定。也**不**用来判断「未开放」这类服务器消息（消息文案属于未
    观察到的信息，见 ``config.not_open_keywords``）。
    """
    st = _parse(window_start)
    if st is None:
        return None
    hh, mm = _BOOKING_OPEN.split(":")
    return st.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)


def effective_trigger_at(window_start: str, trigger_at: str) -> str:
    """触发时刻的完整时间戳：**窗口开始日** + 裸时刻 ``trigger_at``。

    ``trigger_at`` 只有时刻、没有日期，它的日期一直是向窗口开始日借的。这里把
    这个耦合写明，并规定四个调用点都取**滚动之后**的窗口 —— 否则会出现「窗口
    滚到了明天、触发时刻还停在昨天」这种半截状态。
    """
    return f"{window_start[:10]} {trigger_at}"
