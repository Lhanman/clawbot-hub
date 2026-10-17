"""扫码标记 `login.pending`：`hubctl login` 扫码期间放着，里面是进程号和开始时间。

规格 `hub-login`「扫码登录」：凡是用到这个标记的地方都要核对"那个进程号还活着、开始不到 20 分钟"。
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from clawbot_hub.loginstate import (
    SCAN_MARKER_MINUTES,
    ScanMarker,
    read_scan_marker,
    remove_scan_marker,
    scan_in_progress,
    write_scan_marker,
)
from tests.login.conftest import dead_pid

MINUTE_MS = 60_000


def test_the_rule_is_twenty_minutes():
    assert SCAN_MARKER_MINUTES == 20


# ------------------------------------------------------------------ 写


def test_the_marker_holds_pid_and_start_time(paths):
    write_scan_marker(paths, pid=4321, started_ms=1_700_000_000_000)

    assert json.loads(paths.login_pending.read_text()) == {
        "pid": 4321,
        "started_ms": 1_700_000_000_000,
    }
    assert paths.login_pending.read_text().endswith("\n")


def test_the_marker_is_private(paths):
    write_scan_marker(paths, pid=1, started_ms=1)

    assert stat.S_IMODE(paths.login_pending.stat().st_mode) == 0o600


def test_overwriting_an_old_marker_makes_it_private_again(paths):
    paths.login_pending.write_text("old")
    paths.login_pending.chmod(0o644)

    write_scan_marker(paths, pid=2, started_ms=3)

    assert stat.S_IMODE(paths.login_pending.stat().st_mode) == 0o600
    assert read_scan_marker(paths) == ScanMarker(pid=2, started_ms=3)


def test_overwriting_a_longer_marker_leaves_no_tail(paths):
    write_scan_marker(paths, pid=123456789, started_ms=1_700_000_000_000_000)

    write_scan_marker(paths, pid=7, started_ms=8)

    assert read_scan_marker(paths) == ScanMarker(pid=7, started_ms=8)
    assert paths.login_pending.read_text() == '{"pid": 7, "started_ms": 8}\n'


# ------------------------------------------------------------------ 读


def test_read_returns_what_was_written(paths):
    write_scan_marker(paths, pid=77, started_ms=88)

    assert read_scan_marker(paths) == ScanMarker(pid=77, started_ms=88)


def test_no_marker_reads_as_none(paths):
    assert read_scan_marker(paths) is None


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json",
        "[]",
        '"text"',
        "{}",
        '{"pid": 5}',
        '{"started_ms": 5}',
        '{"pid": "5", "started_ms": 5}',
        '{"pid": 5, "started_ms": "5"}',
        '{"pid": 5.5, "started_ms": 5}',
        '{"pid": 5, "started_ms": 5.5}',
        '{"pid": true, "started_ms": 5}',
        '{"pid": 5, "started_ms": true}',
        '{"pid": 0, "started_ms": 5}',
        '{"pid": -3, "started_ms": 5}',
        '{"pid": 5, "started_ms": -1}',
        "\xff\xfe",
    ],
)
def test_a_marker_that_does_not_make_sense_reads_as_none(paths, content):
    paths.login_pending.write_bytes(content.encode("latin-1"))

    assert read_scan_marker(paths) is None


def test_a_directory_in_the_place_of_the_marker_reads_as_none(paths):
    paths.login_pending.mkdir()

    assert read_scan_marker(paths) is None


# ------------------------------------------------------------------ 删


def test_remove_deletes_our_own_marker(paths):
    write_scan_marker(paths, pid=os.getpid(), started_ms=1)

    remove_scan_marker(paths, pid=os.getpid())

    assert not paths.login_pending.exists()


def test_remove_leaves_another_logins_marker(paths):
    write_scan_marker(paths, pid=os.getpid() + 1, started_ms=1)

    remove_scan_marker(paths, pid=os.getpid())

    assert read_scan_marker(paths) == ScanMarker(pid=os.getpid() + 1, started_ms=1)


def test_remove_without_a_marker_is_fine(paths):
    remove_scan_marker(paths, pid=os.getpid())


def test_remove_cleans_up_a_damaged_marker(paths):
    paths.login_pending.write_text("garbage")

    remove_scan_marker(paths, pid=os.getpid())

    assert not paths.login_pending.exists()


# ------------------------------------------------------------------ 还在扫码吗


def test_a_fresh_marker_of_a_living_process_means_scanning(paths, clock):
    write_scan_marker(paths, pid=os.getpid(), started_ms=clock.now_ms())

    assert scan_in_progress(paths, clock)


def test_no_marker_means_not_scanning(paths, clock):
    assert not scan_in_progress(paths, clock)


def test_a_damaged_marker_means_not_scanning(paths, clock):
    paths.login_pending.write_text("???")

    assert not scan_in_progress(paths, clock)


def test_a_marker_of_a_dead_process_means_not_scanning(paths, clock):
    """`hubctl login` 当年被 `kill -9`，标记残留。"""
    write_scan_marker(paths, pid=dead_pid(), started_ms=clock.now_ms())

    assert not scan_in_progress(paths, clock)


def test_a_process_of_another_user_counts_as_alive(paths, clock):
    write_scan_marker(paths, pid=1, started_ms=clock.now_ms())

    assert scan_in_progress(paths, clock)


@pytest.mark.parametrize(
    ("age_ms", "scanning"),
    [
        (0, True),
        (10 * MINUTE_MS, True),
        (20 * MINUTE_MS - 1, True),
        (20 * MINUTE_MS, False),
        (20 * MINUTE_MS + 1, False),
        (3 * 60 * MINUTE_MS, False),
    ],
    ids=["刚开始", "十分钟", "差一毫秒二十分钟", "整二十分钟", "刚过二十分钟", "三小时"],
)
def test_only_the_first_twenty_minutes_count(paths, clock, age_ms, scanning):
    write_scan_marker(paths, pid=os.getpid(), started_ms=clock.now_ms() - age_ms)

    assert scan_in_progress(paths, clock) is scanning


def test_a_start_time_in_the_future_is_not_trusted(paths, clock):
    """乱写成很远以后的开始时间，不能让告警永远被压住。"""
    write_scan_marker(paths, pid=os.getpid(), started_ms=clock.now_ms() + 1)

    assert not scan_in_progress(paths, clock)


def test_time_moving_on_ends_the_scan_window(paths, clock):
    write_scan_marker(paths, pid=os.getpid(), started_ms=clock.now_ms())
    clock.advance(19 * 60 + 59)
    assert scan_in_progress(paths, clock)

    clock.advance(1)

    assert not scan_in_progress(paths, clock)
