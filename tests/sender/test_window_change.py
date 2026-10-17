"""发送途中使用者说了话（换了窗口）：逐条记账。规格：`hub-quota`「预占和对账」。

规矩：预占时定下窗口 W 和对话凭证，整条消息的几段都用这一个凭证；发完一条记一条——发出去的那一条
如果当前窗口已经不是 W，就在**当前窗口**的已用里再加一，W 里的数不退；没发出去的，窗口还是 W 才退。
回复的条数一律记进最新的窗口。
"""

from __future__ import annotations

from clawbot_hub.sender import Origin
from clawbot_hub.upstream.base import Rejected
from tests.sender.conftest import long_text
from tests.support.fake_upstream import DEFAULT

HUB = Origin("_hub", "服务")


def user_speaks(hub):
    """假上游的剧本函数：这一条在飞的时候，使用者说了一句话（开了新窗口），然后照常回话。"""

    def script(call):
        hub.speak()
        return DEFAULT

    return script


def user_speaks_then(hub, outcome):
    def script(call):
        hub.speak()
        return outcome

    return script


def test_a_new_window_while_the_second_part_is_in_flight(hub):
    a = hub.ready()
    hub.upstream.script("send_text", DEFAULT, user_speaks(hub))

    result = hub.sender.push(a, long_text(3))

    assert (result.status, result.parts_sent) == ("sent", 3)
    assert hub.used(window_id=1) == {("alpha", "push"): 3}  # 旧窗口的数不退
    assert hub.used(window_id=2) == {("alpha", "push"): 2}  # 第 2、3 条记进新窗口
    assert {m.context_token for m in hub.upstream.sent} == {"ctx-1"}  # 凭证全程是预占时的那个
    assert hub.row(result.id)["window_id"] == 1


def test_a_new_window_while_the_third_part_is_in_flight(hub):
    a = hub.ready()
    hub.upstream.script("send_text", DEFAULT, DEFAULT, user_speaks(hub))

    hub.sender.push(a, long_text(3))

    assert hub.used(window_id=1) == {("alpha", "push"): 3}
    assert hub.used(window_id=2) == {("alpha", "push"): 1}


def test_a_new_window_while_the_first_part_is_in_flight(hub):
    a = hub.ready()
    hub.upstream.script("send_text", user_speaks(hub))

    hub.sender.push(a, long_text(3))

    assert hub.used(window_id=1) == {("alpha", "push"): 3}
    assert hub.used(window_id=2) == {("alpha", "push"): 3}
    assert {m.context_token for m in hub.upstream.sent} == {"ctx-1"}


def test_a_new_window_after_the_whole_message_changes_nothing(hub):
    a = hub.ready()

    hub.sender.push(a, long_text(3))
    hub.speak()

    assert hub.used(window_id=1) == {("alpha", "push"): 3}
    assert hub.used(window_id=2) == {}


def test_parts_that_fail_after_a_window_change_are_not_taken_from_the_old_window(hub):
    a = hub.ready()
    hub.upstream.script("send_text", DEFAULT, user_speaks_then(hub, Rejected(200, 40001, "bad")))

    result = hub.sender.push(a, long_text(3))

    assert (result.status, result.reason_code, result.parts_sent) == (
        "partial",
        "upstream_rejected",
        1,
    )
    assert hub.used(window_id=1) == {("alpha", "push"): 3}  # 旧窗口的数字不再重要，不退
    assert hub.used(window_id=2) == {}  # 没发出去的不记
    assert hub.holds == []  # 窗口已经换了，不为旧窗口暂停


def test_a_refusal_in_the_new_window_does_not_pause_it(hub):
    a = hub.ready()
    hub.upstream.script("send_text", user_speaks_then(hub, Rejected(200, 40001, "bad")))

    hub.sender.push(a, "你好")
    later = hub.sender.push(a, "又一条")

    assert later.status == "sent"
    assert hub.holds == [] and hub.events("hold") == []


def test_a_reply_is_recorded_in_the_latest_window(hub):
    hub.ready()
    hub.speak()

    result = hub.sender.reply(HUB, "收到", context_token="ctx-1")

    assert hub.used(window_id=1) == {}
    assert hub.used(window_id=2) == {("_hub", "reply"): 1}
    assert hub.row(result.id)["window_id"] == 2
    assert [m.context_token for m in hub.upstream.sent] == ["ctx-1"]


def test_a_reply_that_sees_a_new_window_mid_send_follows_the_same_rules(hub):
    hub.ready()
    hub.upstream.script("send_text", DEFAULT, user_speaks(hub))

    hub.sender.reply(HUB, long_text(3), context_token="ctx-1")

    assert hub.used(window_id=1) == {("_hub", "reply"): 3}
    assert hub.used(window_id=2) == {("_hub", "reply"): 2}


def test_a_rebound_account_while_sending_drops_the_accounting_quietly(hub):
    a = hub.ready()

    def rebind(call):
        hub.bind(bot_token="tok-bot-secret-2")  # 重新扫码：窗口全部作废
        return DEFAULT

    hub.upstream.script("send_text", DEFAULT, rebind)

    result = hub.sender.push(a, long_text(3))

    assert result.parts_sent == 3
    assert hub.used(window_id=1) == {("alpha", "push"): 3}  # 作废的窗口，数不动
