"""腾讯明确拒绝一条推送以后，这个窗口先停一会儿，过后放一条试探。规格：`hub-quota`
「腾讯拒绝以后先停一会儿」；发送层要做的是：拒绝时让额度记下暂停、暂停期里的推送不碰腾讯、
试探的结果如实交给额度、把暂停告诉告警（`on_hold`）。
"""

from __future__ import annotations

import logging

import pytest

from clawbot_hub.config import Config
from clawbot_hub.upstream.base import Accepted, NoResponse, Rejected, Transient
from tests.sender.conftest import long_text
from tests.support.fake_upstream import Gate
from tests.support.hub import Hub, tweak
from tests.support.threads import run_in_thread

BAD_TOKEN = Rejected(200, 40001, "bad token")
BLOCKED = (
    "腾讯拒绝了这个窗口里的发送（40001），先不再试；{minutes} 分钟后再试，"
    "或者给 ClawBot 发一句话重新开窗口"
)


def paused(hub, project):
    """项目发一条被腾讯拒绝的推送，窗口进入暂停。"""
    hub.upstream.script("send_text", BAD_TOKEN)
    first = hub.sender.push(project, "第一条")
    assert first.reason_code == "upstream_rejected"
    hub.upstream.clear_calls()
    return first


# ---------------------------------------------------------------- 暂停


def test_a_refusal_pauses_the_window_for_the_following_pushes(hub):
    a = hub.ready()
    first = paused(hub, a)

    second = hub.sender.push(a, "第二条")

    assert first.status == "failed"
    assert (second.status, second.reason_code) == ("rejected", "upstream_blocked")
    assert second.reason == BLOCKED.format(minutes=30)
    assert hub.upstream.calls == []  # 没有碰腾讯
    assert hub.used() == {}  # 没有占条数
    assert second.quota.left == 0


def test_the_pause_is_told_to_the_alerts(hub):
    a = hub.ready()
    start = hub.clock.now_ms()

    paused(hub, a)

    (hold,) = hub.holds
    assert (hold.code, hold.minutes, hold.until_ms) == ("40001", 30, start + 30 * 60_000)
    (event,) = hub.events("hold")
    assert event["level"] == "WARNING"
    assert event["detail"] == "腾讯拒绝了这个窗口里的发送（40001），暂停 30 分钟"


def test_a_refusal_with_only_an_http_status_pauses_too(hub):
    a = hub.ready()
    hub.upstream.script("send_text", Rejected(403, None, ""))
    hub.sender.push(a, "第一条")

    second = hub.sender.push(a, "第二条")

    assert second.reason_code == "upstream_blocked"
    assert "（HTTP 403）" in second.reason


def test_a_refusal_with_an_empty_code_is_told_by_its_http_status(hub):
    a = hub.ready()
    hub.upstream.script("send_text", Rejected(403, "", ""))

    hub.sender.push(a, "你好")

    assert [hold.code for hold in hub.holds] == ["HTTP 403"]


def test_the_pause_applies_to_every_priority(hub):
    a = hub.ready()
    paused(hub, a)

    for priority in ("urgent", "normal", "deferrable"):
        result = hub.sender.push(a, "你好", priority=priority)
        assert (result.status, result.reason_code) == ("rejected", "upstream_blocked")


def test_the_pause_applies_to_every_project(hub):
    a = hub.ready()
    b = hub.add("beta", guaranteed=1, cap=2)
    paused(hub, a)

    assert hub.sender.push(b, "你好").reason_code == "upstream_blocked"


def test_a_refusal_of_a_later_part_pauses_and_gives_the_rest_back(hub):
    a = hub.ready()
    hub.upstream.script("send_text", Accepted("m-1"), BAD_TOKEN)

    result = hub.sender.push(a, long_text(3))

    assert (result.status, result.reason_code, result.parts_sent) == (
        "partial",
        "upstream_rejected",
        1,
    )
    assert hub.used() == {("alpha", "push"): 1}
    assert len(hub.holds) == 1
    assert hub.sender.push(a, "下一条").reason_code == "upstream_blocked"


def test_the_length_of_the_pause_is_configurable(make_hub):
    hub = make_hub(tweak(Config(), quota={"reject_hold_minutes": 5}))
    a = hub.ready()
    paused(hub, a)

    blocked = hub.sender.push(a, "第二条")
    hub.clock.advance(5 * 60)
    probe = hub.sender.push(a, "试探")

    assert blocked.reason == BLOCKED.format(minutes=5)
    assert probe.status == "sent"


@pytest.mark.parametrize(
    ("seconds_left", "minutes"),
    [
        (0.001, 1),
        (59, 1),
        (60, 1),
        (61, 2),  # 向下取整是 1，四舍五入也是 1
        (90, 2),  # 向下取整是 1
        (120, 2),
        (121, 3),
        (29 * 60, 29),
        (30 * 60 - 0.001, 30),
    ],
)
def test_the_minutes_left_are_always_rounded_up(hub, seconds_left, minutes):
    a = hub.ready()
    paused(hub, a)
    hub.clock.advance(30 * 60 - seconds_left)

    result = hub.sender.push(a, "你好")

    assert result.reason == BLOCKED.format(minutes=minutes)


# ---------------------------------------------------------------- 试探


def test_after_the_pause_one_push_is_let_through_as_a_probe(hub):
    a = hub.ready()
    paused(hub, a)
    hub.clock.advance(30 * 60 - 0.001)
    assert hub.sender.push(a, "还早").reason_code == "upstream_blocked"
    hub.clock.advance(0.001)

    probe = hub.sender.push(a, "试探")
    after = hub.sender.push(a, "之后")

    assert probe.status == "sent"
    assert after.status == "sent"
    assert [e["detail"] for e in hub.events("hold")][-1] == "试探的推送发出去了，解除暂停"


def test_only_one_push_is_let_through_while_the_probe_is_out(hub):
    a = hub.ready()
    b = hub.add("beta", guaranteed=1, cap=2)
    paused(hub, a)
    hub.clock.advance(31 * 60)
    gate = Gate()
    hub.upstream.set_default("send_text", gate)
    probe = run_in_thread(hub.sender.push, a, "试探")
    assert gate.entered.wait(5)

    other = hub.sender.push(b, "同时到的另一个项目")

    assert (other.status, other.reason_code) == ("rejected", "upstream_blocked")
    assert len(hub.upstream.calls) == 1  # 只有试探那一条碰了腾讯
    gate.release()
    assert probe.result().status == "sent"
    assert hub.sender.push(b, "之后").status == "sent"


def test_a_probe_the_upstream_refuses_again_starts_another_pause(hub):
    a = hub.ready()
    paused(hub, a)
    hub.clock.advance(31 * 60)
    hub.upstream.script("send_text", BAD_TOKEN)

    probe = hub.sender.push(a, "试探")
    after = hub.sender.push(a, "之后")

    assert (probe.status, probe.reason_code) == ("failed", "upstream_rejected")
    assert after.reason == BLOCKED.format(minutes=30)
    assert len(hub.holds) == 2


@pytest.mark.parametrize(
    "outcome",
    [Transient("连不上"), NoResponse("读超时", read_timeout=True)],
    ids=["unreachable", "unconfirmed"],
)
def test_a_probe_without_a_clear_answer_leaves_the_next_push_as_the_probe(hub, outcome):
    a = hub.ready()
    paused(hub, a)
    hub.clock.advance(31 * 60)
    hub.upstream.set_default("send_text", outcome)
    probe = hub.sender.push(a, "试探")
    hub.upstream.set_default("send_text", None)

    next_probe = hub.sender.push(a, "再试")

    assert probe.status in ("failed", "unconfirmed")
    assert next_probe.status == "sent"
    assert len(hub.holds) == 1  # 没有新的暂停


def test_the_user_speaking_again_lifts_the_pause(hub):
    a = hub.ready()
    paused(hub, a)
    assert hub.sender.push(a, "还在暂停").reason_code == "upstream_blocked"

    hub.speak()
    result = hub.sender.push(a, "新窗口")

    assert result.status == "sent"
    assert hub.events("hold")[-1]["detail"] == "使用者发来新消息，开了新窗口，暂停解除"


# ---------------------------------------------------------------- 告警的回调


def test_a_failing_alert_callback_does_not_break_the_send(make_hub, caplog):
    def broken(hold):
        raise RuntimeError("告警坏了：不要出现在日志里")

    hub = make_hub(on_hold=broken)
    a = hub.ready()
    hub.upstream.script("send_text", BAD_TOKEN)
    caplog.set_level(logging.DEBUG)

    result = hub.sender.push(a, "你好")

    assert (result.status, result.reason_code) == ("failed", "upstream_rejected")
    assert any("暂停的回调出错" in record.getMessage() for record in caplog.records)
    assert all("不要出现在日志里" not in record.getMessage() for record in caplog.records)
    assert hub.sender.push(a, "下一条").reason_code == "upstream_blocked"  # 暂停照样记下了


def test_a_send_without_an_alert_callback_still_pauses(paths, database, clock, fake_upstream):
    hub = Hub(paths, database, clock, fake_upstream, on_hold=None)
    a = hub.ready()
    hub.upstream.script("send_text", BAD_TOKEN)

    hub.sender.push(a, "你好")

    assert hub.sender.push(a, "下一条").reason_code == "upstream_blocked"
