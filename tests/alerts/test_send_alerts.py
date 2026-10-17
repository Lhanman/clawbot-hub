"""发送这一侧的两种告警：发送未确认（`send_unconfirmed`）和腾讯拒绝发送（`upstream_blocked`）。

**发送未确认**按一次 `sendmessage` 请求算，不按消息算：腾讯回了成功但没有消息编号（`observe`
档也算）或者请求发出后读超时没有回话，都是"没有确认送达"；拿到消息编号的请求把连续次数归零；
腾讯明确拒绝的、连不上的、重试用完的，以及服务自己拒绝（根本没有请求）的，既不计数也不归零。
连续次数存在告警状态表 `send_unconfirmed` 那一行的计数列里，重启后保留；刚好连续 N 次时发一条，
之后要先有一次确认送达，再连续 N 次才会再发。

**腾讯拒绝发送**一个窗口一次：同一个窗口里暂停之后再被拒绝不重复发。
"""

from __future__ import annotations

import dataclasses
import sqlite3

import pytest

from clawbot_hub import alerts
from clawbot_hub.config import Config
from clawbot_hub.sender import CONFIRMED
from clawbot_hub.upstream.base import Accepted, NoResponse, Rejected, Transient
from tests.sender.conftest import long_text
from tests.support.hub import tweak

UNCONFIRMED_TITLE = "ClawBot 共用服务 · 发送未确认"
BLOCKED_TITLE = "ClawBot 共用服务 · 腾讯拒绝发送"


def counter(rig) -> int:
    row = rig.state(alerts.SEND_UNCONFIRMED)
    return 0 if row is None else row["counter"]


@pytest.fixture
def pusher(rig):
    return rig.hub.ready()


def push_without_id(rig, pusher, times: int = 1):
    for _ in range(times):
        rig.hub.upstream.script("send_text", Accepted(None))
        rig.hub.sender.push(pusher, "你好")


# ---------------------------------------------------------------- 发送未确认


def test_the_third_unconfirmed_request_in_a_row_sends_one_alert(rig, pusher):
    push_without_id(rig, pusher, 2)
    assert rig.pending() == {}
    assert counter(rig) == 2

    push_without_id(rig, pusher)

    assert rig.pending() == {alerts.SEND_UNCONFIRMED: alerts.send_unconfirmed_text(3)}
    rig.deliver()
    assert rig.bark.titles == [UNCONFIRMED_TITLE]
    assert "最近 3 次" in rig.bark.texts[0]


def test_a_confirmed_request_resets_the_count_and_a_new_streak_alerts_again(rig, pusher):
    push_without_id(rig, pusher, 3)
    rig.deliver()
    assert counter(rig) == 3

    rig.hub.sender.push(pusher, "有编号的")  # 第 4 次有消息编号
    assert counter(rig) == 0

    push_without_id(rig, pusher, 2)
    assert rig.pending() == {}  # 还差一次
    push_without_id(rig, pusher)
    rig.deliver()

    assert rig.bark.titles == [UNCONFIRMED_TITLE, UNCONFIRMED_TITLE]


def test_a_confirmed_request_with_nothing_to_reset_does_not_take_the_write_lock(rig, pusher):
    """绝大多数请求是确认送达、也没有什么要归零的：这条路只读，不抢写锁，免得每个请求都在
    别人的写事务后面排队（别的连接占着写锁时它也不会被卡住）。"""
    push_without_id(rig, pusher)
    rig.hub.sender.push(pusher, "有编号的")  # 归零：计数那一行还在，只是什么都没有了
    assert counter(rig) == 0
    holder = sqlite3.connect(rig.paths.db, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        rig.alerts.on_request(CONFIRMED)
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert counter(rig) == 0


def test_the_streak_alerts_once_however_long_it_goes_on(rig, pusher):
    push_without_id(rig, pusher, 3)
    rig.deliver()
    rig.bark.calls.clear()

    push_without_id(rig, pusher, 4)  # 连续第 4、5、6、7 次
    rig.deliver()

    assert rig.bark.calls == []
    assert counter(rig) == 7


def test_the_streak_alert_waiting_to_go_out_is_not_rewritten_by_the_next_failures(rig, pusher):
    push_without_id(rig, pusher, 3)
    first = rig.state(alerts.SEND_UNCONFIRMED)["pending_since_ms"]
    rig.clock.advance(120)

    push_without_id(rig, pusher, 2)

    assert rig.state(alerts.SEND_UNCONFIRMED)["pending_since_ms"] == first


def test_one_message_in_three_parts_counts_three(rig, pusher):
    rig.hub.upstream.set_default("send_text", Accepted(None))

    rig.hub.sender.push(pusher, long_text(3))  # 一条拆成三段，三个请求都没编号

    assert counter(rig) == 3
    assert rig.pending() == {alerts.SEND_UNCONFIRMED: alerts.send_unconfirmed_text(3)}


def test_a_request_nobody_answered_counts_as_unconfirmed(rig, pusher):
    for _ in range(3):
        rig.hub.upstream.script("send_text", NoResponse("读超时", read_timeout=True))
        rig.hub.sender.push(pusher, "你好")

    assert alerts.SEND_UNCONFIRMED in rig.pending()


def test_requests_rejected_by_tencent_neither_count_nor_reset(rig, pusher):
    push_without_id(rig, pusher, 2)

    rig.hub.upstream.script("send_text", Rejected(200, 40001, "bad token"))
    failed = rig.hub.sender.push(pusher, "被腾讯拒绝")  # failed，请求发出去了但腾讯明确拒绝
    assert failed.status == "failed"
    assert counter(rig) == 2

    rig.clock.advance(31 * 60)  # 腾讯拒绝以后暂停 30 分钟，过了才会再发请求
    push_without_id(rig, pusher)  # 再有 1 次没有编号

    assert counter(rig) == 3
    assert alerts.SEND_UNCONFIRMED in rig.pending()


def test_requests_that_never_got_out_neither_count_nor_reset(rig, pusher):
    push_without_id(rig, pusher, 2)

    rig.hub.upstream.script(
        "send_text", Transient("连不上"), Transient("连不上"), Transient("连不上")
    )
    unreachable = rig.hub.sender.push(pusher, "连不上")
    assert unreachable.status == "failed"

    assert counter(rig) == 2


def test_refusals_by_the_service_itself_neither_count_nor_reset(make_rig):
    rig = make_rig(tweak(Config(), send={"max_chars": 30000}))  # 放宽字数，才会走到"条数太多"
    pusher = rig.hub.ready()
    push_without_id(rig, pusher, 2)

    refused = rig.hub.sender.push(pusher, long_text(9))  # 超过一个窗口能发的条数：没有请求
    assert refused.status == "rejected"
    assert counter(rig) == 2

    push_without_id(rig, pusher)

    assert counter(rig) == 3
    assert alerts.SEND_UNCONFIRMED in rig.pending()


def test_the_count_survives_a_restart(rig, pusher):
    push_without_id(rig, pusher, 2)

    rig.restart()
    push_without_id(rig, pusher)

    assert counter(rig) == 3
    assert alerts.SEND_UNCONFIRMED in rig.pending()


def test_the_configured_streak_is_used(make_rig):
    rig = make_rig(tweak(Config(), alerts={"unconfirmed_streak": 2}))
    pusher = rig.hub.ready()

    push_without_id(rig, pusher, 2)

    assert rig.pending() == {alerts.SEND_UNCONFIRMED: alerts.send_unconfirmed_text(2)}


def test_a_streak_longer_than_the_new_setting_still_alerts_once(make_rig):
    """配置改小以后（5 改成 3），已经连续 4 次的这一段：下一次没编号时发，而且只发一次。"""
    rig = make_rig(tweak(Config(), alerts={"unconfirmed_streak": 5}))
    pusher = rig.hub.ready()
    push_without_id(rig, pusher, 4)
    assert rig.pending() == {}

    rig.restart(tweak(Config(), alerts={"unconfirmed_streak": 3}))
    push_without_id(rig, pusher)

    assert rig.pending() == {alerts.SEND_UNCONFIRMED: alerts.send_unconfirmed_text(3)}


def test_the_off_mode_does_not_look_at_message_ids(make_rig):
    rig = make_rig(tweak(Config(), upstream={"confirm": "off"}))
    pusher = rig.hub.ready()

    push_without_id(rig, pusher, 4)

    assert counter(rig) == 0
    assert rig.pending() == {}


# ---------------------------------------------------------------- 腾讯拒绝发送


def test_a_refusal_by_tencent_alerts_with_the_code_and_the_pause(rig, pusher):
    rig.hub.upstream.script("send_text", Rejected(200, 5, "no way"))

    rig.hub.sender.push(pusher, "你好")

    assert rig.pending() == {alerts.UPSTREAM_BLOCKED: alerts.upstream_blocked_text("5", 30)}
    rig.deliver()
    assert rig.bark.titles == [BLOCKED_TITLE]
    assert "代码 5" in rig.bark.texts[0]
    assert "30 分钟" in rig.bark.texts[0]


def test_the_pause_minutes_follow_the_configuration(make_rig):
    rig = make_rig(tweak(Config(), quota={"reject_hold_minutes": 45}))
    pusher = rig.hub.ready()
    rig.hub.upstream.script("send_text", Rejected(200, 5, ""))

    rig.hub.sender.push(pusher, "你好")

    assert "45 分钟" in rig.pending()[alerts.UPSTREAM_BLOCKED]


def test_being_refused_again_in_the_same_window_does_not_alert_again(rig, pusher):
    rig.hub.upstream.script("send_text", Rejected(200, 5, ""))
    rig.hub.sender.push(pusher, "第一条")
    rig.deliver()
    rig.bark.calls.clear()

    rig.clock.advance(31 * 60)  # 暂停期过了，下一条推送作为试探，又被拒绝，再停一次
    rig.hub.upstream.script("send_text", Rejected(200, 5, ""))
    probe = rig.hub.sender.push(pusher, "试探")
    rig.deliver()

    assert probe.status == "failed"
    window = rig.hub.quota.current_window(rig.db.conn())
    assert window.blocked_until_ms > rig.clock.now_ms()  # 暂停又开始了
    assert rig.bark.calls == []  # 但同一个窗口不再告警


def test_a_new_window_can_alert_again(rig, pusher):
    rig.hub.upstream.script("send_text", Rejected(200, 5, ""))
    rig.hub.sender.push(pusher, "第一条")
    rig.deliver()
    rig.bark.calls.clear()

    rig.hub.speak()  # 使用者又说了话：新窗口，暂停解除
    rig.hub.upstream.script("send_text", Rejected(200, 6, ""))
    rig.hub.sender.push(pusher, "新窗口里又被拒绝")
    rig.deliver()

    assert rig.bark.titles == [BLOCKED_TITLE]
    assert "代码 6" in rig.bark.texts[0]


def test_the_hold_hook_registers_by_window(rig):
    rig.alerts.on_hold(dataclasses.replace(hold(7), minutes=10))
    rig.alerts.on_hold(hold(7))  # 同一个窗口

    assert rig.state(alerts.UPSTREAM_BLOCKED)["episode"] == "7"
    assert rig.pending() == {alerts.UPSTREAM_BLOCKED: alerts.upstream_blocked_text("5", 10)}


def hold(window_id: int):
    from clawbot_hub.quota import Hold

    return Hold(window_id=window_id, code="5", until_ms=0, minutes=30)
