"""时间和时长的中文写法，状态、项目列表、原因文字共用。

时刻按显示时区写成 `M月D日 HH:MM`（规格 `hub-send`「原因代码和原文」），秒是直接截掉不是四舍五入；
给脚本看的 JSON 用带时区偏移的 ISO 8601，精确到秒；清单里的列用 `年-月-日 时:分:秒`。
时长只写最大的两个单位。
"""

from __future__ import annotations

from datetime import datetime, tzinfo


def _at(ms: int, tz: tzinfo) -> datetime:
    return datetime.fromtimestamp(ms // 1000, tz)


def moment_text(ms: int, tz: tzinfo) -> str:
    """`10月2日 09:05`：月和日不补零，时分补零。"""
    when = _at(ms, tz)
    return f"{when.month}月{when.day}日 {when:%H:%M}"


def clock_text(ms: int, tz: tzinfo) -> str:
    """`2026-10-12 18:30:15`：清单（事件记录）里一列一列对着看用，按显示时区。"""
    return f"{_at(ms, tz):%Y-%m-%d %H:%M:%S}"


def iso_text(ms: int, tz: tzinfo) -> str:
    """`2026-10-12T18:30:15+08:00`。"""
    return _at(ms, tz).isoformat(timespec="seconds")


def span_text(seconds: float) -> str:
    """`1 天 3 小时`、`21 小时 5 分钟`、`40 分钟`、`不到 1 分钟`：最大的两个单位，零的小单位不写；
    不到一分钟（含负数，也就是已经到了）都写"不到 1 分钟"。"""
    total = int(seconds)
    if total < 60:
        return "不到 1 分钟"
    hours, minutes = divmod(total // 60, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days} 天" + (f" {hours} 小时" if hours else "")
    if hours:
        return f"{hours} 小时" + (f" {minutes} 分钟" if minutes else "")
    return f"{minutes} 分钟"
