"""等发送锁：等到预算用完（或服务在关闭）就以 `failed` / `busy` 结束，预占的条数全部退回，没碰腾讯。
规格：`hub-send`「总时限」的「等发送锁等到预算用完」，`hub-deploy` 的关闭。
"""

from __future__ import annotations

from clawbot_hub.sender import Origin
from clawbot_hub.upstream.base import Rejected
from tests.sender.conftest import long_text
from tests.support.threads import run_in_thread

HUB = Origin("_hub", "服务")
BUSY = "服务正忙，这条没有发，稍后再试"


def holder(hub, project):
    """项目发一条消息，卡在腾讯那里，占着发送锁：返回 (门, 线程)。"""
    gate = hub.upstream.hold("send_text")
    sending = run_in_thread(hub.sender.push, project, "在发的")
    assert gate.entered.wait(5)
    return gate, sending


def test_a_request_that_waits_until_its_budget_is_gone_is_busy(hub):
    a = hub.ready()
    b = hub.add("beta", guaranteed=1, cap=2)
    gate, sending = holder(hub, a)
    waiting = run_in_thread(hub.sender.push, b, "等着的", dedupe_key="k")
    hub.clock.wait_until_blocked(1)

    hub.clock.advance(30)

    result = waiting.result()
    assert (result.status, result.reason_code, result.reason) == ("failed", "busy", BUSY)
    assert (result.parts, result.parts_sent) == (1, 0)
    assert len(hub.upstream.calls) == 1  # 只有占着锁的那条碰过腾讯
    assert hub.used() == {("alpha", "push"): 1}  # 预占的条数退回了
    row = hub.row(result.id)
    assert (row["status"], row["parts_sent"]) == ("failed", 0)
    assert result.quota.left > 0
    gate.release()
    assert sending.result().status == "sent"


def test_a_busy_request_of_several_parts_gives_all_of_them_back(hub):
    a = hub.ready(guaranteed=4, cap=8)
    b = hub.add("beta", guaranteed=3, cap=4)
    gate, sending = holder(hub, a)
    waiting = run_in_thread(hub.sender.push, b, long_text(3))
    hub.clock.wait_until_blocked(1)

    hub.clock.advance(30)

    result = waiting.result()
    assert (result.status, result.reason_code) == ("failed", "busy")
    assert (result.parts, result.parts_sent) == (3, 0)
    assert hub.used() == {("alpha", "push"): 1}  # beta 预占的 3 条一条不剩地退回
    assert result.quota.left == 4  # 都退回了：beta 又能用满自己的上限
    gate.release()
    sending.result()


def test_a_busy_request_frees_its_dedupe_key(hub):
    a = hub.ready()
    b = hub.add("beta", guaranteed=1, cap=2)
    gate, sending = holder(hub, a)
    waiting = run_in_thread(hub.sender.push, b, "等着的", dedupe_key="k")
    hub.clock.wait_until_blocked(1)
    hub.clock.advance(30)
    assert waiting.result().reason_code == "busy"
    gate.release()
    sending.result()

    again = hub.sender.push(b, "再来一次", dedupe_key="k")

    assert (again.status, again.duplicate) == ("sent", False)


def test_a_busy_request_leaves_a_warning_event(hub):
    a = hub.ready()
    b = hub.add("beta", guaranteed=1, cap=2)
    gate, sending = holder(hub, a)
    waiting = run_in_thread(hub.sender.push, b, "等着的")
    hub.clock.wait_until_blocked(1)
    hub.clock.advance(30)
    result = waiting.result()
    gate.release()
    sending.result()

    (event,) = hub.events("send")
    assert event["level"] == "WARNING" and event["project"] == "beta"
    assert result.id in event["detail"] and "busy" in event["detail"]


def test_it_is_busy_exactly_when_the_budget_is_gone_not_a_moment_before(hub):
    a = hub.ready()
    b = hub.add("beta", guaranteed=1, cap=2)
    start = hub.clock.now_ms()
    gate, sending = holder(hub, a)
    waiting = run_in_thread(hub.sender.push, b, "等着的")
    hub.clock.wait_until_blocked(1)
    assert hub.clock.pending_deadlines() == [start + 30_000]

    hub.clock.advance(29.999)
    assert waiting.still_blocked()
    hub.clock.advance(0.001)

    assert waiting.result().reason_code == "busy"
    gate.release()
    sending.result()


def test_a_reply_can_be_busy_too(hub):
    a = hub.ready()
    gate, sending = holder(hub, a)
    waiting = run_in_thread(hub.sender.reply, HUB, "等着的", context_token="ctx-1")
    hub.clock.wait_until_blocked(1)

    hub.clock.advance(30)

    result = waiting.result()
    assert (result.status, result.reason_code) == ("failed", "busy")
    assert hub.used() == {("alpha", "push"): 1}
    gate.release()
    sending.result()


# ---------------------------------------------------------------- 服务关闭


def test_shutting_down_ends_the_waiting_requests_at_once(hub):
    a = hub.ready()
    b = hub.add("beta", guaranteed=1, cap=2)
    gate, sending = holder(hub, a)
    waiting = run_in_thread(hub.sender.push, b, "等着的")
    hub.clock.wait_until_blocked(1)

    hub.sender.shutdown()  # 没有让时间往前走

    result = waiting.result()
    assert (result.status, result.reason_code) == ("failed", "busy")
    assert hub.used() == {("alpha", "push"): 1}
    gate.release()
    assert sending.result().status == "sent"  # 正在发的那条照常发完
    assert len(hub.upstream.calls) == 1


def test_a_request_that_arrives_after_shutting_down_is_busy_right_away(hub):
    a = hub.ready()
    hub.sender.shutdown()

    result = hub.sender.push(a, "你好")

    assert (result.status, result.reason_code) == ("failed", "busy")
    assert hub.upstream.calls == []
    assert hub.used() == {}


# ---------------------------------------------------------------- 试探


def test_a_probe_that_never_got_the_lock_leaves_the_next_push_as_the_probe(hub):
    a = hub.ready()
    hub.upstream.script("send_text", Rejected(200, 40001, "bad token"))
    hub.sender.push(a, "第一条")  # 被腾讯拒绝，窗口进入暂停
    hub.clock.advance(31 * 60)  # 暂停期过了：下一条推送是试探
    gate = hub.upstream.hold("send_text")
    replying = run_in_thread(hub.sender.reply, HUB, "回复占着锁", context_token="ctx-1")
    assert gate.entered.wait(5)
    probe = run_in_thread(hub.sender.push, a, "试探")
    hub.clock.wait_until_blocked(1)
    hub.clock.advance(30)
    assert probe.result().reason_code == "busy"
    gate.release()
    replying.result()

    again = hub.sender.push(a, "再试")

    assert again.status == "sent"  # 又是试探，这次发出去了
    assert hub.sender.push(a, "第三条").status == "sent"  # 暂停解除了
