"""一次发送的总时限：等发送锁和向腾讯发请求共用一份 `send.timeout_seconds`；单次请求的超时取
`min(upstream.request_timeout, 剩下的预算)`；每次请求开始之前剩不到 1 秒就不开始。
规格：`hub-send`「逐条发送，判断送达」的「总时限」，设计第 6 节「时间预算」。
"""

from __future__ import annotations

import pytest

from clawbot_hub.config import Config
from clawbot_hub.sender import Origin
from clawbot_hub.upstream.base import Accepted, Transient
from tests.sender.conftest import long_text, takes
from tests.support.hub import tweak
from tests.support.threads import run_in_thread

HUB = Origin("_hub", "服务")


def elapsed_ms(hub, since_ms: int) -> int:
    return hub.clock.now_ms() - since_ms


# ---------------------------------------------------------------- 单次请求的超时


def test_a_request_waits_at_most_15_seconds_by_default(hub):
    a = hub.ready()
    hub.upstream.script("send_text", takes(hub.clock, 20))
    started = hub.clock.now_ms()

    result = hub.sender.push(a, "你好")

    assert (result.status, result.reason_code) == ("unconfirmed", "no_response")
    assert [m.timeout for m in hub.upstream.sent] == [15]  # 只请求了一次，没有重发
    assert elapsed_ms(hub, started) == 15_000


def test_a_reply_has_the_same_timeout(hub):
    hub.ready()
    hub.upstream.script("send_text", takes(hub.clock, 20))

    result = hub.sender.reply(HUB, "你好", context_token="ctx-1")

    assert (result.status, result.reason_code) == ("unconfirmed", "no_response")
    assert [m.timeout for m in hub.upstream.sent] == [15]


def test_the_request_timeout_is_configurable(make_hub):
    hub = make_hub(tweak(Config(), upstream={"request_timeout": 5}))
    a = hub.ready()

    hub.sender.push(a, "你好")

    assert [m.timeout for m in hub.upstream.sent] == [5]


def test_a_request_that_answers_in_time_is_not_cut_short(hub):
    a = hub.ready()
    hub.upstream.script("send_text", takes(hub.clock, 14.9))

    assert hub.sender.push(a, "你好").status == "sent"


# ---------------------------------------------------------------- 总时限


def test_the_budget_runs_out_before_the_next_part_can_start(make_hub):
    hub = make_hub(tweak(Config(), upstream={"request_timeout": 30}))
    a = hub.ready()
    hub.upstream.script("send_text", takes(hub.clock, 29.5))

    result = hub.sender.push(a, long_text(2))

    assert (result.status, result.reason_code) == ("partial", "send_timeout")
    assert (result.parts, result.parts_sent) == (2, 1)
    assert result.reason == "发送超过 30 秒还没发完，后面的 1 条没有发；发出了 1 条，共 2 条"
    assert len(hub.upstream.sent) == 1
    assert hub.used() == {("alpha", "push"): 1}  # 没发的那条退回了


def test_the_first_problem_is_the_reason_even_if_the_budget_runs_out_later(make_hub):
    cfg = tweak(Config(), upstream={"confirm": "strict", "request_timeout": 30})
    hub = make_hub(cfg)
    a = hub.ready()
    hub.upstream.script("send_text", takes(hub.clock, 29.5, Accepted(None)))

    result = hub.sender.push(a, long_text(2))

    assert (result.status, result.reason_code) == ("partial", "no_message_id")
    assert result.reason == "微信 ClawBot 没有返回消息编号，这条可能没有到；发出了 1 条，共 2 条"
    assert len(hub.upstream.sent) == 1


def test_the_budget_runs_out_while_waiting_for_the_answer(make_hub):
    hub = make_hub(tweak(Config(), upstream={"request_timeout": 25}))
    a = hub.ready()
    hub.upstream.script("send_text", takes(hub.clock, 20), takes(hub.clock, 20))

    result = hub.sender.push(a, long_text(3))

    # 第 1 条 20 秒时发出；第 2 条只剩 10 秒（min(25, 10)），到点读超时；第 3 条不发
    assert [m.timeout for m in hub.upstream.sent] == [25, 10]
    assert (result.status, result.reason_code) == ("partial", "no_response")
    assert (result.parts, result.parts_sent) == (3, 2)
    assert hub.used() == {("alpha", "push"): 2}
    assert hub.row(result.id)["parts_unconfirmed"] == 1


def test_the_configured_budget_is_what_the_reason_says(make_hub):
    cfg = tweak(Config(), send={"timeout_seconds": 20}, upstream={"request_timeout": 20})
    hub = make_hub(cfg)
    a = hub.ready()
    hub.upstream.script("send_text", takes(hub.clock, 19.5))

    result = hub.sender.push(a, long_text(3))

    assert result.reason_code == "send_timeout"
    assert result.reason == "发送超过 20 秒还没发完，后面的 2 条没有发；发出了 1 条，共 3 条"
    assert hub.used() == {("alpha", "push"): 1}


@pytest.mark.parametrize(
    ("first_part_takes", "second_part_starts"), [(29.0, True), (29.001, False)]
)
def test_a_part_starts_with_one_second_left_but_not_with_less(
    make_hub, first_part_takes, second_part_starts
):
    hub = make_hub(tweak(Config(), upstream={"request_timeout": 30}))
    a = hub.ready()
    hub.upstream.script("send_text", takes(hub.clock, first_part_takes))

    result = hub.sender.push(a, long_text(2))

    assert (len(hub.upstream.sent) == 2) is second_part_starts
    if second_part_starts:
        assert result.status == "sent"
        assert hub.upstream.sent[1].timeout == 1.0
    else:
        assert (result.status, result.reason_code) == ("partial", "send_timeout")


def test_nothing_is_sent_when_less_than_a_second_is_left_for_the_first_part(make_hub):
    hub = make_hub()
    a = hub.ready()
    gate = hub.upstream.hold("send_text")
    sending = run_in_thread(hub.sender.push, a, "在发的")
    assert gate.entered.wait(5)
    waiting = run_in_thread(hub.sender.push, hub.add("beta"), "等着的")
    hub.clock.wait_until_blocked(1)
    hub.clock.advance(29.5)  # 等锁用掉了 29.5 秒，只剩 0.5 秒

    gate.release()

    assert sending.result().status == "sent"
    late = waiting.result()
    assert (late.status, late.reason_code) == ("failed", "send_timeout")
    assert late.reason == "发送超过 30 秒还没发完，后面的 1 条没有发"
    assert len(hub.upstream.sent) == 1  # 等着的那条没碰腾讯
    assert hub.used() == {("alpha", "push"): 1}


def test_the_time_spent_waiting_for_the_lock_comes_out_of_the_budget(make_hub):
    hub = make_hub()
    a = hub.ready()
    gate = hub.upstream.hold("send_text")
    sending = run_in_thread(hub.sender.push, a, "在发的")
    assert gate.entered.wait(5)
    waiting = run_in_thread(hub.sender.push, hub.add("beta"), "等着的")
    hub.clock.wait_until_blocked(1)
    hub.clock.advance(29)  # 等锁用掉了 29 秒，刚好剩 1 秒

    gate.release()

    assert sending.result().status == "sent"
    assert waiting.result().status == "sent"
    assert hub.upstream.sent[1].timeout == 1.0  # min(15, 剩下的 1 秒)


# ---------------------------------------------------------------- 重试和预算


def refuse_after(clock, seconds: float):
    """假上游的剧本函数：花 `seconds` 秒（假时钟）才发现连不上。"""

    def refused(call):
        clock.sleep(seconds)
        return Transient("连接被拒绝")

    return refused


def test_a_retry_the_budget_cannot_cover_is_not_made(make_hub):
    hub = make_hub(tweak(Config(), send={"timeout_seconds": 3}))
    a = hub.ready()
    hub.upstream.script("send_text", refuse_after(hub.clock, 2.5))

    result = hub.sender.push(a, "你好")

    # 第一次用掉 2.5 秒，剩 0.5 秒，重试之前的 1 秒都等不起；原因沿用上一次的失败，不是 send_timeout
    assert (result.status, result.reason_code) == ("failed", "upstream_unreachable")
    assert "重试 0 次后仍然失败" in result.reason
    assert len(hub.upstream.sent) == 1
    assert hub.used() == {}


@pytest.mark.parametrize(("first_attempt_takes", "retried"), [(28.0, True), (28.001, False)])
def test_a_retry_needs_two_seconds_in_hand_for_the_first_pause(
    make_hub, first_attempt_takes, retried
):
    """停 1 秒以后还要剩 1 秒才能再试：重试之前剩下的预算 ≥ 2 秒才重试。"""
    hub = make_hub()
    a = hub.ready()
    hub.upstream.script("send_text", refuse_after(hub.clock, first_attempt_takes))

    result = hub.sender.push(a, "你好")

    assert (len(hub.upstream.sent) == 2) is retried
    assert result.status == "sent" if retried else result.reason_code == "upstream_unreachable"
    if retried:
        assert hub.upstream.sent[1].timeout == 1.0
        assert hub.upstream.sent[0].client_id == hub.upstream.sent[1].client_id


def test_the_pauses_between_retries_come_out_of_the_budget(make_hub):
    hub = make_hub(tweak(Config(), send={"retries": 5, "timeout_seconds": 10}))
    a = hub.ready()
    hub.upstream.set_default("send_text", Transient("连接被拒绝"))
    started = hub.clock.now_ms()

    result = hub.sender.push(a, "你好")

    # 1 + 2 + 4 秒的停顿之后剩 3 秒，再停 8 秒就不够了：一共 4 次尝试
    assert len(hub.upstream.sent) == 4
    assert "重试 3 次后仍然失败" in result.reason
    assert elapsed_ms(hub, started) == 7_000


def test_a_later_part_can_run_out_of_budget_while_retrying(make_hub):
    hub = make_hub(tweak(Config(), send={"timeout_seconds": 10}))
    a = hub.ready()
    hub.upstream.script("send_text", takes(hub.clock, 6), refuse_after(hub.clock, 2.5))

    result = hub.sender.push(a, long_text(3))

    # 第 2 条第一次试用掉 2.5 秒，剩 1.5 秒，停 1 秒以后只剩 0.5 秒：不再试
    assert [m.index for m in hub.upstream.sent] == [1, 2]
    assert (result.status, result.reason_code) == ("partial", "upstream_unreachable")
    assert "重试 0 次后仍然失败" in result.reason
    assert (result.parts, result.parts_sent) == (3, 1)
    assert hub.used() == {("alpha", "push"): 1}


# ---------------------------------------------------------------- 几段连续发出


def test_the_parts_of_one_message_are_not_interleaved_with_another_projects(make_hub):
    hub = make_hub()
    a = hub.ready()
    b = hub.add("beta", guaranteed=2, cap=3)
    gate = hub.upstream.hold("send_text")
    first = run_in_thread(hub.sender.push, a, long_text(2))
    assert gate.entered.wait(5)
    second = run_in_thread(hub.sender.push, b, "短消息")
    hub.clock.wait_until_blocked(1)  # 第二个在等发送锁
    assert gate.entries == 1  # 它还没碰到腾讯

    gate.release()

    assert first.result().status == "sent"
    assert second.result().status == "sent"
    texts = hub.texts
    assert texts[0].startswith("盘点\n")  # alpha 是默认项目，不带标签
    assert texts[1].startswith("盘点（第 2/2 条）\n")
    assert texts[2] == "【beta】短消息"


def test_the_order_in_which_requests_arrive_is_kept_when_nobody_waits(make_hub):
    hub = make_hub()
    a = hub.ready()
    b = hub.add("beta", guaranteed=2, cap=3)

    hub.sender.push(a, "一")
    hub.sender.push(b, "二")
    hub.sender.reply(HUB, "三", context_token="ctx-1")

    assert hub.texts == ["一", "【beta】二", "【服务】三"]
