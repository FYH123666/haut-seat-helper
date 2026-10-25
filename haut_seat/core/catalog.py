"""座位名解析：区域名与楼层的**推断**逻辑（纯函数，无 I/O）。

为什么单独一个模块：``__main__._derive_region_label`` 原本是 CLI 私有的，
Web 层的座位目录要用同一套规则，因此把规则集中到这里，
CLI 改为委托调用，保证「命令行看到的区域名」与「面板里看到的区域名」永远一致。

**这些值全部是推断，不是权威字段。**
真实观测到的座位名形如 ``十层东书库北区 01桌 E座``：
- 区域名 = 「桌」之前的整段（``十层东书库北区``）；
- 楼层   = 开头的「N层」（``十层``）。

接口返回的数据里**没有**楼层与楼栋字段，所以：
- 区域名只能由座位名推断（权威区域名应以接口 ``layout`` 字段为准，但其真实
  结构尚未核实，故不读取）；
- 楼层只用于**筛选**，界面上必须标注「由座位名推断，非权威」；
- 楼栋**没有数据来源，不做**。

推断失败一律返回空串，绝不猜测（例如座位名是 ``A-001`` 这种占位符时，
既没有「桌」也可能没有「层」，两个函数都会返回空）。
"""

from __future__ import annotations

import re
from typing import Any

# 区域名：「桌」之前的整段。与 __main__ 中原先的实现逐字一致。
_AREA_RE = re.compile(r"^(.*?)\s*\d+桌")

# 楼层：座位名开头的「N层」。中文数字与阿拉伯数字都支持（「十层」「10层」）。
# 只在**开头**匹配，避免把「东书库北区 01桌」里的数字误当楼层。
_FLOOR_RE = re.compile(r"^\s*([0-9一二三四五六七八九十百]+层)")

# 单个标签的长度上限：防止异常长的座位名把标签撑爆（座位名本身也可能不规范）。
_MAX_LABEL_LEN = 40


def _as_text(seat_name: Any) -> str:
    """座位名归一化为字符串；``None`` 与空值一律得到空串。"""
    if seat_name is None:
        return ""
    return str(seat_name).strip()


def derive_area_label(seat_name: Any) -> str:
    """由座位名推断区域名：``十层东书库北区 01桌 E座`` → ``十层东书库北区``。

    推断不出来时返回空串（**不是** None，便于直接入库为 TEXT NOT NULL）。
    """
    text = _as_text(seat_name)
    if not text:
        return ""
    match = _AREA_RE.match(text)
    if not match:
        return ""
    return match.group(1).strip()[:_MAX_LABEL_LEN]


def derive_floor_label(seat_name: Any) -> str:
    """由座位名推断楼层：``十层东书库北区 01桌 E座`` → ``十层``。

    只作筛选维度，**非权威字段**；推断不出来时返回空串。
    """
    text = _as_text(seat_name)
    if not text:
        return ""
    match = _FLOOR_RE.match(text)
    if not match:
        return ""
    return match.group(1)[:_MAX_LABEL_LEN]


def normalize_seat_id(raw_id: Any) -> str:
    """座位 ID 归一化为字符串。

    接口返回的 ``id`` 是数字（真实日志中有 ``id=2481``），但类型未经严格核实
    （``models/seat.py`` 里 ``Seat.id`` 的类型是 ``Any``）。统一转成字符串入库，
    避免同一座位因 ``2481`` / ``"2481"`` 两种形态被存成两行。

    ``None``、空串与纯空白视为**无效 ID** → 返回空串，调用方据此跳过该座位。
    """
    if raw_id is None:
        return ""
    if isinstance(raw_id, bool):
        # bool 是 int 的子类，但座位 ID 不会是布尔值 —— 视为无效，不猜
        return ""
    text = str(raw_id).strip()
    return text


__all__ = [
    "derive_area_label",
    "derive_floor_label",
    "normalize_seat_id",
]