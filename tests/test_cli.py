"""CLI 前置检查测试：在线查询必须提供开始/结束时间、即时测试模式、提交隔离。"""

from __future__ import annotations

import argparse

import pytest

from haut_seat.__main__ import _load_result, _resolve_submit_time, _wait_until, build_parser


def test_live_query_requires_start_and_end(make_settings):
    args = argparse.Namespace(
        from_file=None, regionid=1, start="", end="", page_num=1, page_size=100
    )
    with pytest.raises(ValueError):
        _load_result(args, make_settings())


def test_live_query_requires_both_times(make_settings):
    args = argparse.Namespace(
        from_file=None, regionid=1, start="2026-10-14 10:00:00", end="", page_num=1, page_size=100
    )
    with pytest.raises(ValueError):
        _load_result(args, make_settings())


def test_dry_run_subparser_func_is_not_book():
    p = build_parser()
    a = p.parse_args(["dry-run", "--from-file", "samples/layout_query_sample.json"])
    from haut_seat.__main__ import cmd_dry_run, cmd_book
    assert getattr(a, "func", None) is cmd_dry_run
    assert getattr(a, "func", None) is not cmd_book


def test_book_subparser_uses_submit():
    """验证 book 子命令绑定的是 cmd_book（包含 _submit_online 调用），dry-run 不是"""
    p = build_parser()
    a = p.parse_args(["book", "--config", "config/booking.json.example"])
    from haut_seat.__main__ import cmd_book
    assert getattr(a, "func", None) is cmd_book


def test_wait_until_past_returns_immediately():
    """_wait_until 对已过去的目标时间立即返回"""
    import time
    from haut_seat.__main__ import _wait_until
    t0 = time.time()
    _wait_until(t0 - 10)  # 10 秒前 → 立即返回
    assert time.time() - t0 < 1


def test_wait_until_future_waits():
    """_wait_until 对将来目标时间确实等待"""
    import time
    from haut_seat.__main__ import _wait_until
    t0 = time.time()
    target = t0 + 2
    _wait_until(target)
    assert time.time() >= target


def test_resolve_submit_time_no_adjustment():
    """start 远在将来 → 不调整"""
    from haut_seat.__main__ import _resolve_submit_time
    import time
    now = time.time()
    s, adj = _resolve_submit_time("X", now + 600, now + 7200)
    assert adj is False


def test_resolve_submit_time_adjustment():
    """start 过于接近 → 向后调整 5s"""
    import time
    from haut_seat.__main__ import _resolve_submit_time
    now_ts = time.time()
    start_ts = now_ts + 1        # 仅 1s 后，不够 buffer
    end_ts = now_ts + 7200
    s, adj = _resolve_submit_time("X", start_ts, end_ts)
    assert adj is True
    assert s is not None


def test_resolve_submit_time_over_limit():
    """start 已在过去 → 返回 None（应停止）"""
    import time
    from haut_seat.__main__ import _resolve_submit_time
    now_ts = time.time()
    start_ts = now_ts - 120      # 已过去 2 分钟
    end_ts = now_ts + 3600
    s, adj = _resolve_submit_time("X", start_ts, end_ts)
    assert s is None and adj is True
