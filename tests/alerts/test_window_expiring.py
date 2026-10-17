"""窗口快到期的提醒（规格 `hub-alerts`「窗口快到期的提醒」）。

提醒是为了让使用者回一句话续期，所以只在"有人在用、提醒了有意义"的时候发。同时满足这些才发，
每个窗口最多一次，所有项目合成一条：

- 窗口还开着，剩余不超过 `alerts.window_warn.hours_before`（默认 3）小时；
- 现在在 `alerts.window_warn.between`（默认 07:00 到 22:30，显示时区）之内，并且今天是
  `alerts.window_warn.weekdays`（默认周一到周五，ISO 编号）之一；
- 这个窗口里有项目发过推送（回复不算）；
- 条件一时不满足（比如到点时是夜里），在窗口关闭之前每个检查周期再看一次，满足了就发。

使用者在提醒之后又说了话，新窗口重新开始，这条提醒不再有意义，也不补发。
"""

from __future__ import annotations

import pytest

from clawbot_hub import alerts
from clawbot_hub.config import Config
from clawbot_hub.quota import PUSH, REPLY
from clawbot_hub.upstream.base import Transient
from tests.alerts.conftest import go_to
from tests.support.hub import tweak

TITLE = "ClawBot 共用服务 · 窗口快到期"


def open_window(rig, when: str, *, pushed: bool = True, projects: tuple[str, ...] = ("alpha",)):
    """北京时间 `when` 使用者说了一句话（开窗口），项目在窗口里推送过（`pushed`）。"""
    go_to(rig.clock, when)
    rig.hub.bind()
    rig.hub.speak()
    for name in projects:
        rig.hub.add(name, guaranteed=3, cap=4)
        if pushed:
            rig.hub.fill(1, project=name, kind=PUSH)


def remind(rig, when: str) -> bool:
    go_to(rig.clock, when)
    return rig.alerts.check_window_expiring()


# ---------------------------------------------------------------- 到点就提醒


def test_a_weekday_afternoon_with_three_hours_left_sends_a_reminder(rig):
    open_window(rig, "2026-10-13 18:30")  # 周二；窗口到周三 18:30

    assert remind(rig, "2026-10-14 15:30")  # 周三 15:30，剩 3 小时

    assert rig.pending() == {
        alerts.WINDOW_EXPIRING: (
            "微信窗口还有约 3 小时到期（10月14日 18:30）：给 ClawBot 回一句话续期"
        )
    }
    rig.deliver()
    assert rig.bark.titles == [TITLE]


def test_not_a_minute_before_three_hours_are_left(rig):
    open_window(rig, "2026-10-13 18:30")

    assert not remind(rig, "2026-10-14 15:29")  # 还剩 3 小时 1 分钟
    assert rig.pending() == {}

    rig.clock.advance(60)

    assert rig.alerts.check_window_expiring()  # 剩 3 小时整


def test_less_than_three_hours_left_still_reminds(rig):
    open_window(rig, "2026-10-13 18:30")

    assert remind(rig, "2026-10-14 17:00")  # 只剩 1 小时 30 分钟，之前没有看过

    assert "约 2 小时" in rig.pending()[alerts.WINDOW_EXPIRING]


def test_the_hours_before_setting_is_used(make_rig):
    rig = make_rig(tweak(Config(), alerts={"window_warn": _warn(hours_before=5)}))
    open_window(rig, "2026-10-13 18:30")

    assert not remind(rig, "2026-10-14 13:00")  # 剩 5 小时 30 分钟
    assert remind(rig, "2026-10-14 13:30")


# ---------------------------------------------------------------- 夜里等天亮


def test_at_night_it_waits_for_the_morning(rig):
    open_window(rig, "2026-10-14 09:30")  # 周三 09:30 开窗口，到周四 09:30 到期

    assert not remind(rig, "2026-10-15 06:30")  # 剩 3 小时，天还没亮
    assert rig.pending() == {}

    assert remind(rig, "2026-10-15 07:00")  # 07:00 发，窗口还没关
    assert "10月15日 09:30" in rig.pending()[alerts.WINDOW_EXPIRING]


def test_a_window_that_closes_before_the_morning_gets_nothing(rig):
    open_window(rig, "2026-10-14 06:00")  # 到周四 06:00 到期

    for when in ("2026-10-15 03:00", "2026-10-15 05:59", "2026-10-15 06:00", "2026-10-15 07:00"):
        assert not remind(rig, when)

    assert rig.pending() == {}


def test_the_end_of_the_daytime_range_is_inclusive(rig):
    open_window(rig, "2026-10-14 01:30")  # 到周四 01:30 到期

    assert remind(rig, "2026-10-14 22:30")  # 剩 3 小时，22:30 还在 07:00 到 22:30 之内


def test_after_the_daytime_range_nothing_is_sent(rig):
    open_window(rig, "2026-10-14 01:00")  # 到周四 01:00 到期

    assert not remind(rig, "2026-10-14 22:31")  # 剩不到 3 小时，但已经过了 22:30
    assert not remind(rig, "2026-10-14 23:00")
    assert not remind(rig, "2026-10-15 07:00")  # 天亮的时候窗口早关了
    assert rig.pending() == {}


def test_the_daytime_range_follows_the_display_time_zone(make_rig):
    rig = make_rig(tweak(Config(), display={"timezone": "Asia/Tokyo"}))
    open_window(rig, "2026-10-14 06:30")  # 北京时间 06:30，东京时间 07:30；到周四 06:30（北京）

    # 北京时间周四 03:30 = 东京 04:30，天还没亮；北京 06:00 = 东京 07:00 才算白天
    assert not remind(rig, "2026-10-15 03:30")
    assert remind(rig, "2026-10-15 06:00")


@pytest.mark.parametrize(
    ("started", "now", "reminded"),
    [
        ("2026-10-14 02:00", "2026-10-14 23:00", True),  # 晚上 23:00，在 22:00 到 06:00 之内
        ("2026-10-14 04:00", "2026-10-15 01:00", True),  # 过了午夜，凌晨 01:00 也在之内
        ("2026-10-14 09:30", "2026-10-15 06:30", False),  # 06:30 过了范围的尽头
        ("2026-10-14 15:00", "2026-10-15 12:00", False),  # 中午不在之内
    ],
)
def test_a_range_across_midnight_works(make_rig, started, now, reminded):
    warn = _warn(between="22:00-06:00", weekdays=(1, 2, 3, 4, 5, 6, 7))
    rig = make_rig(tweak(Config(), alerts={"window_warn": warn}))
    open_window(rig, started)

    assert remind(rig, now) is reminded


# ---------------------------------------------------------------- 星期


@pytest.mark.parametrize(
    ("started", "now", "weekday"),
    [
        ("2026-10-16 12:00", "2026-10-17 09:00", "周六"),
        ("2026-10-17 12:00", "2026-10-18 09:00", "周日"),
    ],
)
def test_weekends_get_no_reminder(rig, started, now, weekday):
    open_window(rig, started)

    assert not remind(rig, now), weekday
    assert rig.pending() == {}


def test_the_same_window_on_a_monday_gets_a_reminder(rig):
    open_window(rig, "2026-10-18 12:00")  # 周日 12:00 开，周一 12:00 到期

    assert remind(rig, "2026-10-19 09:00")  # 周一（ISO 编号 1）剩 3 小时


def test_the_weekdays_setting_is_used(make_rig):
    rig = make_rig(tweak(Config(), alerts={"window_warn": _warn(weekdays=(6, 7))}))
    open_window(rig, "2026-10-16 12:00")  # 到周六 12:00 到期

    assert remind(rig, "2026-10-17 09:00")  # 周六在 weekdays 里了


def test_sunday_is_iso_seven(make_rig):
    rig = make_rig(tweak(Config(), alerts={"window_warn": _warn(weekdays=(7,))}))
    open_window(rig, "2026-10-17 12:00")  # 到周日 12:00 到期

    assert remind(rig, "2026-10-18 09:00")


# ---------------------------------------------------------------- 有没有人在用


def test_a_window_with_only_replies_gets_no_reminder(rig):
    open_window(rig, "2026-10-13 18:30", pushed=False)
    rig.hub.fill(2, kind=REPLY)  # 使用者问、项目答，没有任何推送

    assert not remind(rig, "2026-10-14 15:30")
    assert rig.pending() == {}


def test_a_window_nobody_used_gets_no_reminder(rig):
    open_window(rig, "2026-10-13 18:30", pushed=False)

    assert not remind(rig, "2026-10-14 15:30")


def test_a_push_that_never_got_out_does_not_count_as_pushing(rig):
    """推送预占过又退回了（一条也没发出去）：用量表里有这一行，但数是 0，窗口里等于没人推送过。"""
    open_window(rig, "2026-10-13 18:30", pushed=False)
    rig.hub.upstream.script(
        "send_text", Transient("连不上"), Transient("连不上"), Transient("连不上")
    )
    assert rig.hub.sender.push(rig.hub.project("alpha"), "连不上").status == "failed"
    row = rig.db.conn().execute("SELECT n FROM usage WHERE kind = 'push'").fetchone()
    assert row is not None and row["n"] == 0  # 这一行确实在

    assert not remind(rig, "2026-10-14 15:30")
    assert alerts.WINDOW_EXPIRING not in rig.pending()


def test_only_pushes_in_this_window_count(rig):
    open_window(rig, "2026-10-13 10:00")  # 上一个窗口里推送过
    go_to(rig.clock, "2026-10-13 18:30")
    rig.hub.speak()  # 使用者又说了话：新窗口，里面还没有推送

    assert not remind(rig, "2026-10-14 15:30")


def test_two_projects_get_one_reminder(rig):
    open_window(rig, "2026-10-13 18:30", projects=("alpha", "beta"))

    assert remind(rig, "2026-10-14 15:30")
    assert not rig.alerts.check_window_expiring()
    rig.deliver()

    assert len(rig.bark.calls) == 1


# ---------------------------------------------------------------- 每个窗口一次


def test_each_window_gets_one_reminder(rig):
    open_window(rig, "2026-10-13 18:30")
    remind(rig, "2026-10-14 15:30")
    rig.deliver()

    for when in ("2026-10-14 15:31", "2026-10-14 16:00", "2026-10-14 18:00"):
        assert not remind(rig, when)

    assert len(rig.bark.calls) == 1


def test_a_restart_does_not_remind_again(rig):
    open_window(rig, "2026-10-13 18:30")
    remind(rig, "2026-10-14 15:30")
    rig.deliver()

    rig.restart()

    assert not rig.alerts.check_window_expiring()
    rig.deliver()
    assert len(rig.bark.calls) == 1


def test_the_next_window_gets_its_own_reminder(rig):
    open_window(rig, "2026-10-13 18:30")
    remind(rig, "2026-10-14 15:30")
    rig.deliver()

    go_to(rig.clock, "2026-10-14 16:00")
    rig.hub.speak()  # 续期：新窗口，到周四 16:00
    rig.hub.fill(1, project="alpha", kind=PUSH)

    assert remind(rig, "2026-10-15 13:00")  # 新窗口剩 3 小时
    rig.deliver()
    assert len(rig.bark.calls) == 2


# ---------------------------------------------------------------- 开关和已经关了的窗口


def test_the_reminder_can_be_turned_off(make_rig):
    rig = make_rig(tweak(Config(), alerts={"window_warn": _warn(enabled=False)}))
    open_window(rig, "2026-10-13 18:30")

    assert not remind(rig, "2026-10-14 15:30")
    assert rig.pending() == {}


def test_a_window_that_is_already_closed_gets_nothing(rig):
    open_window(rig, "2026-10-13 18:30")

    assert not remind(rig, "2026-10-14 18:30")
    assert not remind(rig, "2026-10-14 19:00")


def test_without_any_window_there_is_nothing_to_remind_about(rig):
    rig.hub.bind()

    assert not rig.alerts.check_window_expiring()


# ---------------------------------------------------------------- 使用者又说了话


def test_speaking_again_makes_a_waiting_reminder_moot(rig):
    open_window(rig, "2026-10-13 18:30")
    remind(rig, "2026-10-14 15:30")
    assert alerts.WINDOW_EXPIRING in rig.pending()  # Bark 不通，还在待发队列里

    go_to(rig.clock, "2026-10-14 15:45")
    rig.hub.speak()  # 使用者回了一句话：新窗口
    rig.alerts.check_window_expiring()
    rig.deliver()

    assert rig.pending() == {}
    assert rig.bark.calls == []  # 不补发
    assert any("作废" in event["detail"] for event in rig.events())


def test_a_reminder_for_a_window_that_has_closed_is_not_sent_late(rig):
    open_window(rig, "2026-10-13 18:30")
    remind(rig, "2026-10-14 15:30")

    go_to(rig.clock, "2026-10-14 19:00")
    rig.alerts.check_window_expiring()
    rig.deliver()

    assert rig.bark.calls == []
    assert rig.pending() == {}


def test_a_waiting_reminder_for_the_current_window_stays(rig):
    open_window(rig, "2026-10-13 18:30")
    remind(rig, "2026-10-14 15:30")

    rig.clock.advance(600)
    rig.alerts.check_window_expiring()

    assert alerts.WINDOW_EXPIRING in rig.pending()


def _warn(**changes):
    import dataclasses

    from clawbot_hub.config import WindowWarnConfig

    return dataclasses.replace(WindowWarnConfig(), **changes)
