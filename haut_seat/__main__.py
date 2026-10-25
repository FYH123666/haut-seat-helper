"""命令行入口。

除 `book` 在 HAUT_ENABLE_REAL_BOOKING=true 且无暂停旗标时可能提交真实预约外，
其余命令（status / check-auth / auth-status / query / dry-run）均为只读或离线。
任何命令在认证失效时都会立即停止。

用法示例：
  # 状态检查（开关/暂停/可预约状态组合，含凭据来源与预检复用窗口）
  python -m haut_seat status

  # 认证预检（只读，验证当前凭据是否仍然有效；更新 .env 后用它验证）
  python -m haut_seat check-auth

  # 人工登录采集（打开浏览器由你本人登录，自动保存凭据快照，消除手抄 F12 的步骤）
  #   这是**人工**登录：不代填账号口令、不识别验证码
  python -m haut_seat auth-login

  # 只读查看凭据来源 / 快照时间 / 指纹 / 浏览器可用性（不发网络请求）
  python -m haut_seat auth-status

  # 离线解析脱敏样例
  python -m haut_seat query --from-file samples/layout_query_sample.json
  python -m haut_seat dry-run --from-file samples/layout_query_sample.json

  # 只读在线查询（需要网络可达；不会提交预约）
  python -m haut_seat query --regionid 16 --start "2026-10-16 08:00" --end "2026-10-16 12:00"

  # 预约任务（认证预检 → 等待触发 → 查询选座 → 提交）
  python -m haut_seat book
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime as _dt
from pathlib import Path

from haut_seat import __version__
from haut_seat.config import load_settings
from haut_seat.core.auth import RELOGIN_HINT, AuthStatus
from haut_seat.core.catalog import derive_area_label
from haut_seat.core.guard import check_booking_allowed
from haut_seat.core.lock import FileLock
from haut_seat.services.slot_lock import acquire_slot_lock
from haut_seat.core.priority import (
    SeatState,
    classify_seat,
    explain_seat_inclusion,
    seat_state_label,
    sort_by_priority,
    state_distribution,
)
from haut_seat.core.schedule import check_time_validity
from haut_seat.logging_conf import setup_logging
from haut_seat.models.seat import LayoutQueryResult
from haut_seat.services.booking_runner import (
    BookingDeps,
    BookingPlanConfig,
    run_booking,
)
from haut_seat.services.booking_runner import resolve_submit_time as _resolve_submit_time  # noqa: F401  （兼容测试导入）
from haut_seat.services.booking_runner import ts_parse as _ts_parse
from haut_seat.services.booking_runner import wait_until as _wait_until
# 真实 IO 适配移入服务层，由 CLI 与 Web 共用；这里的重新绑定保持
# 「调用时解析模块全局」的语义，既有测试对 __main__ 的打桩依然有效。
from haut_seat.services.auth_browser import (
    BrowserLoginError,
    BrowserNotInstalledError,
    INSTALL_HINT,
    LOGIN_PROMPT,
    browser_availability,
    capture_login,
)
from haut_seat.services.auth_login_service import (
    LOGIN_AWAITING,
    LOGIN_CAPTURED,
    LOGIN_FAILED,
    open_web_db,
    record_credential_source,
    record_login_state,
)
from haut_seat.services.auth_store import (
    Credentials,
    CredentialsUnavailableError,
    resolve_credentials,
    save_snapshot,
    snapshot_path,
    source_label,
)
from haut_seat.services.io import (
    auth_precheck as _auth_precheck,
    auth_stop_reason as _auth_stop_reason,
    build_http_client as _build_http_client,  # noqa: F401  （兼容既有引用）
    query_online as _query_online,
    secret_str as _secret_str,
    submit_online as _submit_online,
)

#: 登录采集锁的过期秒数。必须显式放大：默认 300s 挡不住一个人慢慢输验证码，
#: 会让"锁还活着却被当成残留"从而允许第二个窗口去抢同一个 profile 目录。
LOGIN_LOCK_STALE_S = 1800.0


def _load_json_file(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def _cli_reporter(logger):
    """CLI 运行事件上报：打印到 stdout，同时写入日志（已脱敏）。"""
    from haut_seat.services.state import RunPhase

    def _report(phase: RunPhase, level: str, message: str, category=None) -> None:
        print(message)
        log = getattr(logger, level.lower(), logger.info) if logger is not None else None
        if log is not None:
            suffix = f" [{category.value}]" if category is not None else ""
            log("[%s] %s%s", phase.value, message, suffix)

    return _report


def _load_result(args, settings) -> LayoutQueryResult:
    if getattr(args, "from_file", None):
        raw = json.loads(Path(args.from_file).read_text(encoding="utf-8"))
        return LayoutQueryResult.from_response(raw)

    if not args.start or not args.end:
        raise ValueError("在线查询必须提供 --start 与 --end 时间参数（如 'YYYY-MM-DD HH:MM:SS'）")

    regionid = args.regionid if args.regionid is not None else settings.regionid
    page_num = args.page_num if args.page_num is not None else 1
    page_size = args.page_size if args.page_size is not None else settings.page_size
    return _query_online(settings, regionid, args.start, args.end, page_num, page_size)


def _resolve_credentials_quietly(settings) -> tuple[Credentials | None, str]:
    """解析本次要用的凭据；给出原因但**不抛异常**（CLI 只读展示用）。

    只读文件（快照 JSON / ``.env``），**不发任何网络请求**。
    """
    try:
        return resolve_credentials(settings), ""
    except CredentialsUnavailableError as exc:
        return None, str(exc)
    except Exception as exc:  # 非预期异常：只报类型，不回显可能含值的原始消息
        return None, f"{type(exc).__name__}：凭据解析异常"


def cmd_status(args, settings, logger) -> int:
    decision = check_booking_allowed(settings.enable_real_booking, settings.pause_file)
    cfg = _load_json_file(settings.seats_config)
    confirmed = cfg.get("confirmed_bookable_states", [])
    creds, creds_reason = _resolve_credentials_quietly(settings)
    print(f"haut-seat-helper {__version__}")
    print(f"base_url: {settings.base_url}")
    print(f"真实预约开关: {settings.enable_real_booking}")
    print(
        f"暂停旗标: {'存在' if Path(settings.pause_file).exists() else '不存在'} "
        f"({settings.pause_file})"
    )
    print(f"预约放行: {decision.allowed} — {decision.reason}")
    print(f"已确认可预约状态组合数: {len(confirmed)} (为 0 表示状态语义未验证)")
    print(f"会话 Cookie: {'已配置(值不显示)' if _secret_str(settings.cookie) else '未配置'}")
    print(f"Authorization: {'已配置(值不显示)' if _secret_str(settings.authorization) else '未配置'}")
    # ---- 凭据来源与预检复用窗口：只印标签/时间/单向指纹与秒数，绝不印值 ----
    print(f"凭据来源配置: {settings.auth_source}"
          f"（本次实际使用：{source_label(creds.source) if creds else '不可用'}）")
    if creds is not None and creds.source == "snapshot":
        print(f"  快照采集时间: {creds.captured_at or '未知'}　指纹: {creds.fingerprint()}")
    if creds_reason:
        print(f"  凭据不可用原因: {creds_reason}")
    print(f"预检复用窗口: {settings.auth_check_ttl_s:g}s（0 = 每次预检都真实探测）")
    print(f"预约提交流程: 已实现（channel={settings.booking_channel}），成功判据 code=200+bookingid，ENABLE_REAL_BOOKING={settings.enable_real_booking}")
    return 0


def cmd_check_auth(args, settings, logger) -> int:
    """只读认证预检：确认当前凭据是否有效。绝不提交预约。

    预检**走凭据来源层**：``auto``/``browser`` 模式下真正被使用的那份可能是
    浏览器会话快照，而不是 ``.env``。因此这里必须如实印出来源，
    否则"Authorization: 未配置"会与"预检却通过了"自相矛盾。
    """
    regionid = args.regionid if args.regionid is not None else settings.regionid
    creds, creds_reason = _resolve_credentials_quietly(settings)
    print(f"haut-seat-helper {__version__}")
    print(f"base_url: {settings.base_url}")
    print(f"Authorization: {'已配置(值不显示)' if _secret_str(settings.authorization) else '未配置'}")
    print(f"Cookie: {'已配置(值不显示)' if _secret_str(settings.cookie) else '未配置'}")
    print(f"凭据来源配置: {settings.auth_source}"
          f"（本次实际使用：{source_label(creds.source) if creds else '不可用'}）")
    if creds is not None and creds.source == "snapshot":
        print(f"  快照采集时间: {creds.captured_at or '未知'}　指纹: {creds.fingerprint()}")
    if creds_reason:
        print(f"  凭据不可用原因: {creds_reason}")

    check = _auth_precheck(settings, regionid, args.start or None, args.end or None)
    print(f"登录预检: {check.status.value} — {check.message}")
    if check.http_status is not None:
        print(f"  HTTP 状态: {check.http_status}")
    if check.business_code is not None:
        print(f"  业务 code: {check.business_code}")
    if check.seat_count is not None:
        print(f"  座位数: {check.seat_count}")

    if check.status is AuthStatus.VALID:
        print("[只读预检通过：凭据有效，可以重新运行 book；本次未提交任何预约]")
        return 0
    if check.status is AuthStatus.INVALID:
        print(RELOGIN_HINT)
        return 3
    print("无法确认登录状态（不是认证失效判定：可能为网络/服务端/响应结构异常）。")
    print("建议：稍后重试；若持续出现，检查网络与服务器状态，必要时按上述步骤更新凭据。")
    return 1


def cmd_auth_status(args, settings, logger) -> int:
    """只读查看认证来源与浏览器可用性。**不发任何网络请求。**"""
    creds, creds_reason = _resolve_credentials_quietly(settings)
    available, unavailable_reason = browser_availability()
    path = snapshot_path(settings)
    print(f"haut-seat-helper {__version__}")
    print(f"base_url: {settings.base_url}")
    print(f"凭据来源配置: {settings.auth_source}")
    print(f".env 凭据: {'已配置(值不显示)' if (creds and creds.source == 'env') or _secret_str(settings.cookie) or _secret_str(settings.authorization) else '未配置'}")
    print(f"本次实际使用: {source_label(creds.source) if creds else '不可用'}")
    if creds is not None and creds.source == "snapshot":
        print(f"  快照采集时间: {creds.captured_at or '未知'}　指纹: {creds.fingerprint()}")
    if creds_reason:
        print(f"  凭据不可用原因: {creds_reason}")
    print(f"快照文件: {path}（{'存在' if path.exists() else '不存在'}）")
    print(f"浏览器自动化: {'可用' if available else '不可用'}")
    if not available:
        print(f"  原因: {unavailable_reason}")
    print(f"预检复用窗口: {settings.auth_check_ttl_s:g}s（0 = 每次预检都真实探测）")
    print("[只读查看：未发起任何网络请求，未提交任何预约]")
    return 0


def cmd_auth_login(args, settings, logger) -> int:
    """浏览器辅助的**人工**登录采集：打开可见浏览器 → 用户本人登录 → 落盘快照。

    明确不做的事：不代填账号口令、不识别或绕过验证码、不调用任何预约接口。
    返回码：0 采集成功（复检可通过）；1 采集/复检失败；2 依赖不可用；
    3 采集成功但只读复检判定凭据失效；4 已有一个采集在进行中。
    """
    available, unavailable_reason = browser_availability()
    if not available:
        print("浏览器自动化不可用，无法进行人工登录采集。", file=sys.stderr)
        print(f"原因：{unavailable_reason}", file=sys.stderr)
        print(f"安装后重试：{INSTALL_HINT}（不会下载任何浏览器，复用系统已装的 Edge）", file=sys.stderr)
        return 2

    timeout_s = args.timeout if args.timeout is not None else settings.auth_login_timeout_s
    lock = FileLock(Path(settings.auth_dir) / "login.lock", stale_after_s=LOGIN_LOCK_STALE_S)
    if not lock.acquire():
        print("已有一个登录采集在进行中（或上一轮留下的锁尚未过期），本次不再打开新窗口。",
              file=sys.stderr)
        print(f"如确认没有其它采集在跑，可删除锁文件后重试：{lock.path}", file=sys.stderr)
        return 4

    try:
        return _do_auth_login(args, settings, logger, timeout_s=timeout_s)
    finally:
        lock.release()


def _do_auth_login(args, settings, logger, *, timeout_s: float) -> int:
    """打开元数据库（尽力而为），跑完采集流程后必定关掉它。

    进度元数据只是"给页面看的"，DB 打不开也必须照常采集 ——
    采集的目的是那份快照，不是这条进度记录。
    """
    db = open_web_db(settings, logger=logger)
    try:
        return _capture_and_record(args, settings, logger, timeout_s=timeout_s, db=db)
    finally:
        if db is not None:
            db.close_current_thread()


def _capture_and_record(args, settings, logger, *, timeout_s: float, db) -> int:
    print(f"haut-seat-helper {__version__}")
    print(f"base_url: {settings.base_url}")
    print(f"浏览器通道: {settings.auth_browser_channel}（复用系统已装浏览器，不下载）")
    print(f"专用用户目录: {settings.auth_profile_dir}")
    print(f"等待上限: {timeout_s:g}s")
    print("说明：这是**人工登录**，不是自动登录。本项目不代填账号口令、不识别验证码。")
    print(LOGIN_PROMPT)

    record_login_state(
        db, login_state=LOGIN_AWAITING,
        message="等待你在浏览器窗口中完成登录（含图形验证码）…", logger=logger,
    )

    try:
        capture = capture_login(settings, timeout_s=timeout_s, on_prompt=lambda _text: None)
    except BrowserNotInstalledError as exc:
        print(f"浏览器自动化不可用：{exc}", file=sys.stderr)
        print(f"安装后重试：{INSTALL_HINT}", file=sys.stderr)
        record_login_state(db, login_state=LOGIN_FAILED,
                           message=f"浏览器自动化不可用：{exc}", logger=logger)
        return 2
    except BrowserLoginError as exc:
        print(f"采集失败：{exc}", file=sys.stderr)
        record_login_state(db, login_state=LOGIN_FAILED,
                           message=f"采集失败：{exc}", logger=logger)
        return 1

    # 先落盘：登录是人工成果，绝不能因为后面的复检失败而白费。
    try:
        path = save_snapshot(settings, cookie=capture.cookie,
                             authorization=capture.authorization)
    except Exception as exc:
        print(f"凭据已采集但写入快照失败：{type(exc).__name__}（值未显示）", file=sys.stderr)
        record_login_state(
            db, login_state=LOGIN_FAILED,
            message=f"凭据已采集但写入快照失败：{type(exc).__name__}", logger=logger,
        )
        return 1

    print(f"已保存凭据快照：{path}")
    print(f"  采集时间: {capture.captured_at}　指纹: {capture.fingerprint()}（只印指纹，不印值）")
    print(f"  当前 HAUT_AUTH_SOURCE={settings.auth_source}；"
          f"在 auto/browser 下会优先使用这份快照。")
    record_login_state(db, login_state=LOGIN_CAPTURED,
                       message="已获取凭据并保存快照。", logger=logger)
    record_credential_source(
        db, source="snapshot", snapshot_present=True,
        captured_at=capture.captured_at, fingerprint=capture.fingerprint(), logger=logger,
    )

    if args.no_verify:
        print("[已跳过只读复检；请自行运行 python -m haut_seat check-auth 验证]")
        return 0

    print("正在进行一次只读复检（不会提交任何预约）…")
    return _verify_captured(settings, capture, args)


def _verify_captured(settings, capture, args) -> int:
    """用刚采集到的凭据做一次只读预检，返回码沿用 check-auth 的 0/1/3。"""
    from pydantic import SecretStr

    regionid = args.regionid if args.regionid is not None else settings.regionid
    # 临时配置：强制 env 模式 + 刚采集的值，于是预检打的就是这份新凭据，
    # 而不是可能更旧的快照/其它来源。model_copy 不落盘、不污染进程配置。
    probe = settings.model_copy(
        update={
            "auth_source": "env",
            "cookie": SecretStr(capture.cookie),
            "authorization": SecretStr(capture.authorization),
        }
    )
    check = _auth_precheck(probe, regionid, args.start or None, args.end or None)
    print(f"登录预检: {check.status.value} — {check.message}")
    if check.http_status is not None:
        print(f"  HTTP 状态: {check.http_status}")
    if check.business_code is not None:
        print(f"  业务 code: {check.business_code}")

    if check.status is AuthStatus.VALID:
        print("[只读复检通过：快照凭据有效，本程序已可自动取用；本次未提交任何预约]")
        return 0
    if check.status is AuthStatus.INVALID:
        print("[只读复检判定该凭据已失效；快照已保存，但请在浏览器里确认登录是否真的完成]")
        print(RELOGIN_HINT)
        return 3
    print("无法确认登录状态（不是认证失效判定：可能为网络/服务端/响应结构异常）。")
    print("快照已保存；建议稍后再运行 python -m haut_seat check-auth 复核。")
    return 1


def cmd_query(args, settings, logger) -> int:
    result = _load_result(args, settings)
    print(f"顶层字段: {result.top_level_keys()}")
    print(f"layout 类型: {result.layout_type}")
    print(f"seatList 座位数: {result.seat_count}")
    for seat in result.seats:
        print(seat)
    print("[只读查询完成，未提交任何预约]")
    return 0


def _derive_region_label(seats) -> str | None:
    """由座位名推断区域名（如「五层东书库南区 01桌 A座」→「五层东书库南区」）。

    仅作展示参考，非权威字段；权威区域名应以接口 layout 字段为准。
    规则已抽到 ``haut_seat.core.catalog``，与 Web 座位目录共用同一套实现。
    """
    for s in seats:
        label = derive_area_label(s.seat_name)
        if label:
            return label
    return None


def cmd_dry_run(args, settings, logger) -> int:
    cfg = _load_json_file(settings.seats_config)
    confirmed = cfg.get("confirmed_bookable_states", [])
    pref_names = cfg.get("preferred_seat_names", [])
    pref_prefixes = cfg.get("preferred_seat_name_prefixes", [])
    regionid = args.regionid if args.regionid is not None else settings.regionid

    # 即时测试模式：未显式指定时间时，取当前 → 22:00，立即查询不等待
    is_instant = False
    if not getattr(args, "from_file", None):
        if not args.start or not args.end:
            now = _dt.now()
            if now.hour >= 22:
                print("当前已过闭馆时间 22:00，无法查询")
                return 2
            args.start = now.strftime("%Y-%m-%d %H:%M:%S")
            args.end = now.strftime("%Y-%m-%d") + " 22:00:00"
            is_instant = True

    result = _load_result(args, settings)

    # 1) 时间与模式
    print("== 时间与模式 ==")
    if getattr(args, "from_file", None):
        print("  离线模式（--from-file），无时间检查")
    elif is_instant:
        print(f"  即时测试模式（仅查询和选座，不提交预约）")
        print(f"  查询时段: {args.start} → {args.end}")
    else:
        print(f"  正式查询模式")
        print(f"  查询时段: {args.start} → {args.end}")
        warnings = check_time_validity(args.start, args.end)
        if warnings:
            for w in warnings:
                print(f"  [警告] {w}")
        else:
            print("  时间合法性: 未发现明显问题")

    # 2) 区域
    region_label = _derive_region_label(result.seats)
    line = f"== 区域 == regionid={regionid}"
    if region_label:
        line += f"（由座位名推断区域：{region_label}）"
    print(line)

    # 3) 状态组合分布（仅记录事实，不推断语义）
    print("== 状态组合分布（仅事实，不推断语义）==")
    for label, n in state_distribution(result.seats).items():
        print(f"  {label}: {n} 个")

    # 4) 全部座位（按名称筛选，含入选/排除原因）
    print("== 全部座位（名称筛选后）==")
    candidates: list[tuple] = []
    for seat in result.seats:
        included, reason = explain_seat_inclusion(seat, pref_names, pref_prefixes)
        if not included:
            print(f"  [排除] id={seat.id} name={seat.seat_name!r}（原因：{reason}）")
            continue
        candidates.append((seat, reason))
    ordered = sort_by_priority([s for s, _ in candidates], pref_names, pref_prefixes)
    reason_by_id = {s.id: r for s, r in candidates}
    for seat in ordered:
        state = classify_seat(seat, confirmed)
        print(
            f"  [{state.value}] id={seat.id} name={seat.seat_name!r} "
            f"{seat_state_label(seat)}（入选原因：{reason_by_id[seat.id]}）"
        )
    # 5) 实际可预约候选（仅已确认可预约 + 名称匹配）
    actual = [s for s in ordered if classify_seat(s, confirmed) is SeatState.CONFIRMED_BOOKABLE]
    print("== 实际可预约候选（仅已确认可预约）==")
    if not actual:
        if not confirmed:
            print("  无实际候选：confirmed_bookable_states 未配置，所有座位状态未知")
        else:
            print("  无实际候选：名称匹配的座位中无一命中已确认可预约状态组合")
    else:
        for i, seat in enumerate(actual):
            print(f"  #{i+1} id={seat.id} name={seat.seat_name!r} {seat_state_label(seat)}")
    print(f"展示座位: {len(ordered)} 个，实际可预约候选: {len(actual)} 个")
    print("[dry-run 完成，仅打印拟选座位，未提交任何预约]")
    return 0


def cmd_book(args, settings, logger) -> int:
    path = args.config or settings.booking_config
    if not Path(path).exists():
        print(f"配置文件 {path} 不存在；请复制 {path}.example 并编辑 regionid/start/end/preferred_seats")
        return 2

    cfg = _load_json_file(path)
    regionid = cfg.get("regionid", settings.regionid)
    start = str(cfg.get("start", ""))
    end = str(cfg.get("end", ""))
    preferred_seats = cfg.get("preferred_seats", [])

    # 如果 booking.json 未配置状态，从 seats.json 补充
    confirmed = cfg.get("confirmed_bookable_states", [])
    if not confirmed:
        seats_cfg = _load_json_file(settings.seats_config)
        confirmed = seats_cfg.get("confirmed_bookable_states", [])
    if not confirmed:
        print("⚠ confirmed_bookable_states 为空（booking.json 和 seats.json 均未配置），所有座位状态未知，不会成为实际预约候选")
    if confirmed and not cfg.get("confirmed_bookable_states"):
        cfg["confirmed_bookable_states"] = confirmed

    # --- 时间合法性快速检查 ---
    warnings = check_time_validity(start, end)
    if warnings:
        for w in warnings:
            print(f"[时间不合法] {w}")
        return 2

    start_ts = _ts_parse(start)
    end_ts = _ts_parse(end)
    if start_ts is None or end_ts is None:
        print(f"时间格式解析失败: start={start!r} end={end!r}")
        return 2

    # --- 防重复触发（与 Web 调度器共用**同一套按时段分片的**跨进程锁）---
    # 锁名由「区域 + 时段」派生：同一个时段仍然只允许一个进程提交，
    # 不同时段互不挡道（从前是一把全局锁，任何两个 book 都会互相挤掉）。
    lock = acquire_slot_lock(
        settings,
        regionid=regionid,
        start=start,
        end=end,
        wait_s=settings.book_lock_wait_s,
        stale_after_s=settings.book_lock_stale_s,
        logger=logger,
    )
    if lock is None:
        print(
            f"同一时段（区域 {regionid}，{start} → {end}）已有预约任务在运行"
            "（防重复提交），本次跳过"
        )
        return 0
    try:
        # 依赖在这里、以「调用时解析模块全局」的方式取出：
        # 既让 CLI 使用真实实现，又保持既有测试对 __main__ 命名空间的打桩有效。
        deps = BookingDeps(
            query_fn=_query_online,
            submit_fn=_submit_online,
            auth_check_fn=_auth_precheck,
            allowed_fn=lambda: check_booking_allowed(
                settings.enable_real_booking, settings.pause_file
            ),
            report=_cli_reporter(logger),
            wait_fn=_wait_until,
            auth_stop_reason_fn=_auth_stop_reason,
            time_mod=time,
        )
        base = BookingPlanConfig.from_mapping(cfg, fallback_regionid=settings.regionid)
        config = BookingPlanConfig(
            regionid=regionid,
            start=start,
            end=end,
            preferred_seats=tuple(base.preferred_seats),
            preferred_prefixes=tuple(base.preferred_prefixes),
            confirmed_bookable_states=tuple(confirmed),
            trigger_at=base.trigger_at,
            trigger_offset_s=base.trigger_offset_s,
            max_attempts_per_seat=base.max_attempts_per_seat,
        )
        report = run_booking(settings, config, deps, logger)
        return report.exit_code
    finally:
        lock.release()


def cmd_init_db(args, settings, logger) -> int:
    """初始化 Web 数据库（建表 + 单行配置）。幂等，不会删除任何既有数据。"""
    from haut_seat.web import security
    from haut_seat.web.db import Database

    db = Database(settings.web_db)
    db.init_schema()
    admins = security.count_admins(db)
    print(f"数据库已初始化: {settings.web_db}")
    print(f"管理员数量: {admins}")
    if admins == 0:
        print("尚未创建管理员，请执行：")
        print("  python -m haut_seat create-admin --username 你的用户名")
    return 0


def cmd_create_admin(args, settings, logger) -> int:
    """创建/重置 Web 管理员。口令只以交互方式输入，或由环境变量提供，绝不写入日志。"""
    import getpass

    from haut_seat.web import security
    from haut_seat.web.db import Database

    db = Database(settings.web_db)
    db.init_schema()

    username = (args.username or "").strip()
    if not username:
        print("必须提供 --username", file=sys.stderr)
        return 2

    password = args.password or ""
    if not password:
        try:
            p1 = getpass.getpass("请输入管理员口令（输入时不回显）: ")
            p2 = getpass.getpass("请再次输入确认: ")
        except (EOFError, KeyboardInterrupt):
            print("\n已取消", file=sys.stderr)
            return 2
        if p1 != p2:
            print("两次输入的口令不一致", file=sys.stderr)
            return 2
        password = p1

    row = security.find_admin(db, username)
    if row is not None:
        if not args.reset:
            print(f"管理员 {username!r} 已存在；如需重置口令，请加 --reset", file=sys.stderr)
            return 2
        try:
            security.set_admin_password(db, int(row["id"]), password)
        except ValueError as exc:
            print(f"重置失败: {exc}", file=sys.stderr)
            return 2
        print(f"管理员 {username!r} 的口令已重置，既有网页会话已全部失效。")
        return 0

    try:
        security.create_admin(db, username, password)
    except security.AdminExistsError as exc:
        print(f"创建失败: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"创建失败: {exc}", file=sys.stderr)
        return 2
    print(f"管理员 {username!r} 已创建。")
    print("口令仅以 scrypt 哈希入库；日志、页面与 API 响应都不会出现口令或凭据。")
    return 0


def cmd_web(args, settings, logger) -> int:
    """启动 Web 管理面板。

    **不会**因为启动服务而执行任何真实预约：新计划默认「已暂停」，
    全局暂停默认开启，且仍受 HAUT_ENABLE_REAL_BOOKING 与暂停旗标限制。
    """
    if args.init_db:
        cmd_init_db(args, settings, logger)
    host = args.host or settings.web_host
    port = args.port or settings.web_port
    try:
        import uvicorn
    except ImportError:
        print(
            "缺少 Web 依赖。请安装：pip install -e \".[web]\"",
            file=sys.stderr,
        )
        return 2

    from haut_seat.web.app import create_app

    app = create_app(settings, logger=logger)
    print(f"Web 管理面板: http://{host}:{port}/admin")
    print(f"数据库: {settings.web_db}")
    print(
        f"真实预约开关: {settings.enable_real_booking}"
        f"（{'允许提交' if settings.enable_real_booking else '禁止提交，只读查询可用'}）"
    )
    # 会话 Cookie 策略：这一项配错会造成「登录成功却一直回到登录页」且服务端毫无异常，
    # 因此和上面的开关一样在启动横幅里如实打印，便于对照日志排查。
    if settings.web_session_secure:
        print("会话 Cookie: Secure（浏览器只在 HTTPS 下才会回传；用 http:// 访问会一直回到登录页）")
    else:
        print("会话 Cookie: 不带 Secure（HTTP 与 HTTPS 均可；对外暴露时必须由反向代理做 HTTPS）")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"警告：正在监听 {host}（非回环地址）。请确认前面有反向代理 + HTTPS，"
            "或在可信内网中使用。",
            file=sys.stderr,
        )
    uvicorn.run(app, host=host, port=port, log_level=settings.log_level.lower())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="haut-seat", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("status", help="查看配置与安全开关状态")
    s.set_defaults(func=cmd_status)

    a = sub.add_parser("check-auth", help="只读认证预检：验证 .env 中的凭据是否仍然有效")
    a.add_argument("--regionid", type=int, default=None, help="区域 ID（默认取配置）")
    a.add_argument("--start", default="", help="可选：自定义预检开始时间（默认用安全窗口）")
    a.add_argument("--end", default="", help="可选：自定义预检结束时间")
    a.set_defaults(func=cmd_check_auth)

    al = sub.add_parser(
        "auth-login",
        help="浏览器辅助的人工登录：打开窗口由你本人登录，自动保存凭据快照",
    )
    al.add_argument("--timeout", type=float, default=None,
                    help="等待人工完成登录的秒数（默认取 HAUT_AUTH_LOGIN_TIMEOUT_S，300）")
    al.add_argument("--regionid", type=int, default=None, help="复检区域 ID（默认取配置）")
    al.add_argument("--start", default="", help="可选：复检用开始时间（默认用安全窗口）")
    al.add_argument("--end", default="", help="可选：复检用结束时间")
    al.add_argument("--no-verify", action="store_true",
                    help="只采集并保存快照，跳过采集后的只读复检")
    al.set_defaults(func=cmd_auth_login)

    ast = sub.add_parser("auth-status", help="只读查看凭据来源与浏览器可用性（不发网络请求）")
    ast.set_defaults(func=cmd_auth_status)

    q = sub.add_parser("query", help="只读查询座位")
    q.add_argument("--from-file", help="从脱敏 JSON 文件离线解析")
    q.add_argument("--regionid", type=int, default=None, help="区域 ID（默认取配置 HAUT_REGIONID，参考值 16）")
    q.add_argument("--start", default="", help="开始时间（格式待验证）")
    q.add_argument("--end", default="", help="结束时间（格式待验证）")
    q.add_argument("--page-num", type=int, default=None, help="页码（默认 1）")
    q.add_argument("--page-size", type=int, default=None, help="每页数量（默认取配置 HAUT_PAGE_SIZE，参考值 10）")
    q.set_defaults(func=cmd_query)

    d = sub.add_parser("dry-run", help="只读选座演练（不提交）")
    d.add_argument("--from-file", help="从脱敏 JSON 文件离线解析")
    d.add_argument("--regionid", type=int, default=None, help="区域 ID（默认取配置）")
    d.add_argument("--start", default="")
    d.add_argument("--end", default="")
    d.add_argument("--page-num", type=int, default=None)
    d.add_argument("--page-size", type=int, default=None)
    d.set_defaults(func=cmd_dry_run)

    b = sub.add_parser("book", help="预约任务：认证预检→等待触发→查询选座→提交")
    b.add_argument("--config", default=None, help="预约任务 JSON 路径（默认取 HAUT_BOOKING_CONFIG）")
    b.set_defaults(func=cmd_book)

    w = sub.add_parser("web", help="启动 Web 管理面板（默认只监听 127.0.0.1，不自动预约）")
    w.add_argument("--host", default=None, help="监听地址（默认取 HAUT_WEB_HOST，安全默认 127.0.0.1）")
    w.add_argument("--port", type=int, default=None, help="监听端口（默认取 HAUT_WEB_PORT）")
    w.add_argument("--init-db", action="store_true", help="启动前先初始化数据库（幂等）")
    w.set_defaults(func=cmd_web)

    i = sub.add_parser("init-db", help="初始化 Web 数据库（幂等，不会删除既有数据）")
    i.set_defaults(func=cmd_init_db)

    c = sub.add_parser("create-admin", help="创建（或用 --reset 重置）Web 管理员")
    c.add_argument("--username", required=True, help="管理员用户名")
    c.add_argument("--password", default=None,
                   help="不推荐：直接传口令会留在 shell 历史里；省略则交互式输入")
    c.add_argument("--reset", action="store_true", help="用户已存在时重置其口令")
    c.set_defaults(func=cmd_create_admin)

    return parser


def _force_utf8_stdio() -> None:
    """统一 stdout/stderr 为 UTF-8，保证中英文输出在 Windows/Linux 一致。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except Exception as exc:  # pydantic.ValidationError
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    logger = setup_logging(settings.log_level, settings.log_file)
    try:
        return args.func(args, settings, logger)
    except NotImplementedError as exc:
        logger.error("%s", exc)
        return 3
    except Exception as exc:
        logger.exception("执行失败: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
