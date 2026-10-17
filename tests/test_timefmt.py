"""时间的中文写法（`hub-send`「原因代码和原文」：`M月D日 HH:MM`，按显示时区）和时长的写法。"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from clawbot_hub.timefmt import iso_text, moment_text, span_text

SHANGHAI = ZoneInfo("Asia/Shanghai")


def ms_of(year, month, day, hour, minute, second=0, tz=SHANGHAI) -> int:
    return int(datetime(year, month, day, hour, minute, second, tzinfo=tz).timestamp() * 1000)


def test_a_moment_is_month_day_and_clock_time_without_leading_zeros_on_the_date():
    assert moment_text(ms_of(2026, 10, 2, 9, 5), SHANGHAI) == "10月2日 09:05"
    assert moment_text(ms_of(2026, 1, 12, 18, 30), SHANGHAI) == "1月12日 18:30"


def test_a_moment_follows_the_display_time_zone():
    ms = ms_of(2026, 10, 12, 18, 30)

    assert moment_text(ms, SHANGHAI) == "10月12日 18:30"
    assert moment_text(ms, UTC) == "10月12日 10:30"
    assert moment_text(ms, ZoneInfo("Asia/Tokyo")) == "10月12日 19:30"


def test_a_moment_crossing_midnight_changes_the_date_with_the_zone():
    ms = ms_of(2026, 10, 12, 23, 59)

    assert moment_text(ms, SHANGHAI) == "10月12日 23:59"
    assert moment_text(ms, ZoneInfo("Asia/Tokyo")) == "10月13日 00:59"


def test_seconds_are_cut_off_not_rounded():
    assert moment_text(ms_of(2026, 10, 12, 18, 30, 59), SHANGHAI) == "10月12日 18:30"


def test_an_iso_text_has_the_offset_and_whole_seconds():
    ms = ms_of(2026, 10, 12, 18, 30, 15) + 987

    assert iso_text(ms, SHANGHAI) == "2026-10-12T18:30:15+08:00"
    assert iso_text(ms, UTC) == "2026-10-12T10:30:15+00:00"


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "不到 1 分钟"),
        (59, "不到 1 分钟"),
        (60, "1 分钟"),
        (59 * 60 + 59, "59 分钟"),
        (3600, "1 小时"),
        (3600 + 60, "1 小时 1 分钟"),
        (21 * 3600 + 5 * 60, "21 小时 5 分钟"),
        (23 * 3600 + 59 * 60 + 59, "23 小时 59 分钟"),
        (24 * 3600, "1 天"),
        (24 * 3600 + 3600, "1 天 1 小时"),
        (3 * 86400 + 4 * 3600 + 59 * 60, "3 天 4 小时"),
    ],
)
def test_a_span_is_written_with_its_two_biggest_units(seconds, text):
    assert span_text(seconds) == text


def test_a_negative_span_reads_as_nothing_left():
    assert span_text(-5) == "不到 1 分钟"


def test_fractions_of_a_second_are_cut_off_not_rounded():
    """和 `moment_text` 一样，秒以下直接截掉：59.9 秒还是"不到 1 分钟"，119.9 秒是 1 分钟。"""
    assert span_text(59.9) == "不到 1 分钟"
    assert span_text(119.9) == "1 分钟"
    assert span_text(-0.5) == "不到 1 分钟"
