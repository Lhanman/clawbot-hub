"""服务重启（`service_restarted`）和备份失败（`backup_failed`）两种告警。

**服务重启**：服务启动时发现上一次不是正常退出（崩溃、被杀、断电、关停时间用完强行退出），或者有
中断的发送，就登记一条；启动时只登记，由告警线程发出。正文里的"上次运行到 HH:MM"取 `run.json` 里
的最后心跳，没有就不写这半句。`alerts.restart_merge_minutes`（默认 10）分钟内最多一次（防止崩溃后
反复重启时刷屏），而且待发的那一条不会因为又重启了一次丢掉或重复。

**备份失败**：每日备份失败、10 分钟后重试仍失败才告警（重试在 `test_scheduler.py`），一天一条。
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from clawbot_hub import alerts
from clawbot_hub.bark import Delivered, Unreachable
from clawbot_hub.config import Config
from clawbot_hub.instance import RunInfo
from tests.alerts.conftest import go_to, ms_of
from tests.support.hub import tweak

RESTARTED_TITLE = "ClawBot 共用服务 · 服务重启"
BACKUP_TITLE = "ClawBot 共用服务 · 备份失败"
TZ = ZoneInfo("Asia/Shanghai")
MINUTE = 60


def crashed(beat: str = "2026-10-12 09:58") -> RunInfo:
    """上一次运行留下的 `run.json`：没有标"正常退出"。"""
    return RunInfo(
        pid=4242,
        last_heartbeat_ms=ms_of(beat),
        started_ms=ms_of("2026-10-11 20:00"),
        clean_exit=False,
    )


def stopped(beat: str = "2026-10-12 09:58") -> RunInfo:
    """上一次是正常退出的。"""
    return RunInfo(
        pid=4242,
        last_heartbeat_ms=ms_of(beat),
        started_ms=ms_of("2026-10-11 20:00"),
        clean_exit=True,
    )


# ---------------------------------------------------------------- 服务重启：什么时候登记


def test_an_unclean_exit_registers_one_alert_with_the_last_heartbeat(rig):
    assert rig.alerts.service_restarted(crashed("2026-10-12 09:58"), 0)

    assert rig.pending() == {
        alerts.SERVICE_RESTARTED: alerts.service_restarted_text(ms_of("2026-10-12 09:58"), 0, TZ)
    }
    assert rig.bark.calls == []  # 启动时只登记，发是告警线程的事
    rig.deliver()
    assert rig.bark.titles == [RESTARTED_TITLE]
    assert "上次运行到 09:58" in rig.bark.texts[0]


def test_a_normal_exit_is_not_an_alert(rig):
    assert not rig.alerts.service_restarted(stopped(), 0)

    assert rig.pending() == {}
    assert rig.states() == {}


def test_the_very_first_start_is_not_an_alert(rig):
    assert not rig.alerts.service_restarted(None, 0)

    assert rig.pending() == {}


def test_interrupted_sends_are_an_alert_even_after_a_normal_exit(rig):
    assert rig.alerts.service_restarted(stopped("2026-10-12 09:58"), 2)

    assert rig.pending() == {
        alerts.SERVICE_RESTARTED: alerts.service_restarted_text(ms_of("2026-10-12 09:58"), 2, TZ)
    }
    assert "2 条发送中断" in rig.pending()[alerts.SERVICE_RESTARTED]


def test_interrupted_sends_without_a_run_file_leave_out_the_last_run(rig):
    assert rig.alerts.service_restarted(None, 1)

    assert rig.pending() == {alerts.SERVICE_RESTARTED: alerts.service_restarted_text(None, 1, TZ)}
    assert "上次运行到" not in rig.pending()[alerts.SERVICE_RESTARTED]


def test_a_crash_with_interrupted_sends_says_both(rig):
    rig.alerts.service_restarted(crashed("2026-10-12 03:15"), 3)

    assert rig.pending() == {
        alerts.SERVICE_RESTARTED: (
            "共用服务重启了（上次运行到 03:15）；有 3 条发送中断，可能少发或重发"
        )
    }


def test_the_heartbeat_time_is_shown_in_the_display_time_zone(make_rig):
    rig = make_rig(tweak(Config(), display={"timezone": "Asia/Tokyo"}))

    rig.alerts.service_restarted(crashed("2026-10-12 09:58"), 0)  # 北京 09:58 = 东京 10:58

    assert "上次运行到 10:58" in rig.pending()[alerts.SERVICE_RESTARTED]


# ---------------------------------------------------------------- 服务重启：十分钟内最多一次


def test_a_crash_loop_makes_one_alert(rig):
    """5 分钟里被反复杀掉并重新拉起 4 次，只发一条。"""
    for _ in range(4):
        rig.restart()
        rig.alerts.service_restarted(crashed(), 0)
        rig.deliver()
        rig.clock.advance(75)

    assert rig.bark.titles == [RESTARTED_TITLE]


def test_a_crash_after_the_merge_window_alerts_again(rig):
    rig.alerts.service_restarted(crashed(), 0)
    rig.deliver()

    rig.clock.advance(10 * MINUTE - 0.001)
    rig.restart()
    assert not rig.alerts.service_restarted(crashed(), 0)  # 差 1 毫秒满 10 分钟：还在合并的时间里

    rig.clock.advance(0.001)
    rig.restart()
    assert rig.alerts.service_restarted(crashed(), 0)  # 整 10 分钟
    rig.deliver()

    assert rig.bark.titles == [RESTARTED_TITLE, RESTARTED_TITLE]


def test_the_merge_window_follows_the_configuration(make_rig):
    rig = make_rig(tweak(Config(), alerts={"restart_merge_minutes": 1}))
    rig.alerts.service_restarted(crashed(), 0)
    rig.deliver()

    rig.clock.advance(59)
    assert not rig.alerts.service_restarted(crashed(), 0)
    rig.clock.advance(1)
    assert rig.alerts.service_restarted(crashed(), 0)


def test_the_merged_restart_does_not_move_the_window(rig):
    """第二次重启被合并了，也不让第三次重启的"十分钟"从第二次起算。"""
    rig.alerts.service_restarted(crashed(), 0)
    rig.deliver()
    rig.clock.advance(6 * MINUTE)
    assert not rig.alerts.service_restarted(crashed(), 0)  # 合并

    rig.clock.advance(4 * MINUTE)  # 距第一条整 10 分钟，距第二次才 4 分钟

    assert rig.alerts.service_restarted(crashed(), 0)


def test_a_restart_alert_that_could_not_be_sent_is_not_lost_in_a_crash_loop(rig):
    """服务被杀后启动，登记了 `service_restarted` 但 Bark 不通；3 分钟后又被杀、再启动，这时
    Bark 恢复：待发的只有第一次登记的那一条，发出一次；第二次启动没有再登记一条。"""
    rig.bark.default = Unreachable("Bark 不通")
    rig.alerts.service_restarted(crashed(), 0)
    rig.deliver()
    first = rig.state(alerts.SERVICE_RESTARTED)["pending_since_ms"]
    rig.clock.advance(3 * MINUTE)

    rig.restart()
    registered_again = rig.alerts.service_restarted(crashed(), 0)
    rig.bark.default = Delivered()
    rig.bark.calls.clear()
    rig.deliver()

    assert not registered_again
    assert rig.state(alerts.SERVICE_RESTARTED)["pending_since_ms"] is None
    assert rig.state(alerts.SERVICE_RESTARTED)["last_sent_ms"] is not None
    assert first is not None
    assert rig.bark.titles == [RESTARTED_TITLE]


def test_an_undelivered_restart_alert_older_than_the_window_is_replaced(rig):
    rig.bark.default = Unreachable("Bark 不通")
    rig.alerts.service_restarted(crashed("2026-10-12 09:00"), 0)
    rig.deliver()
    rig.clock.advance(11 * MINUTE)

    assert rig.alerts.service_restarted(
        crashed("2026-10-12 10:08"), 0
    )  # 新的一次，不是同一次的重复
    rig.bark.default = Delivered()
    rig.bark.calls.clear()
    rig.deliver()

    assert rig.bark.titles == [RESTARTED_TITLE]  # 只发一条，用的是新的那条的内容
    assert "上次运行到 10:08" in rig.bark.texts[0]


# ---------------------------------------------------------------- 备份失败


def test_a_failed_backup_registers_one_alert_with_the_reason(rig):
    assert rig.alerts.backup_failed("磁盘满了")

    assert rig.pending() == {alerts.BACKUP_FAILED: alerts.backup_failed_text("磁盘满了")}
    rig.deliver()
    assert rig.bark.titles == [BACKUP_TITLE]
    assert "磁盘满了" in rig.bark.texts[0]


def test_only_one_backup_alert_a_day(rig):
    rig.alerts.backup_failed("磁盘满了")
    rig.deliver()
    rig.bark.calls.clear()

    rig.clock.advance(3600)
    assert not rig.alerts.backup_failed("磁盘满了")  # 当天晚些时候又失败
    rig.deliver()

    assert rig.bark.calls == []


def test_a_restart_does_not_bring_the_same_days_backup_alert_back(rig):
    rig.alerts.backup_failed("磁盘满了")
    rig.deliver()

    rig.restart()

    assert not rig.alerts.backup_failed("磁盘满了")


def test_the_next_day_alerts_again(rig):
    go_to(rig.clock, "2026-10-12 14:20")
    rig.alerts.backup_failed("磁盘满了")
    rig.deliver()

    go_to(rig.clock, "2026-10-13 14:20")
    assert rig.alerts.backup_failed("没有权限")
    rig.deliver()

    assert rig.bark.titles == [BACKUP_TITLE, BACKUP_TITLE]
    assert "没有权限" in rig.bark.texts[1]


@pytest.mark.parametrize(
    ("timezone", "first", "second"),
    [
        ("Asia/Shanghai", "2026-10-12 23:50", "2026-10-13 00:10"),
        ("Asia/Tokyo", "2026-10-12 22:50", "2026-10-12 23:10"),  # 东京 23:50 和次日 00:10
    ],
)
def test_the_day_is_the_day_in_the_display_time_zone(make_rig, timezone, first, second):
    rig = make_rig(tweak(Config(), display={"timezone": timezone}))
    go_to(rig.clock, first)
    rig.alerts.backup_failed("磁盘满了")
    rig.deliver()

    go_to(rig.clock, second)

    assert rig.alerts.backup_failed("磁盘满了")


def test_a_backup_alert_still_waiting_is_not_registered_twice(rig):
    rig.bark.default = Unreachable("Bark 不通")
    rig.alerts.backup_failed("磁盘满了")
    rig.deliver()
    first = rig.state(alerts.BACKUP_FAILED)["pending_since_ms"]
    rig.clock.advance(60)

    assert not rig.alerts.backup_failed("磁盘满了")

    assert rig.state(alerts.BACKUP_FAILED)["pending_since_ms"] == first
