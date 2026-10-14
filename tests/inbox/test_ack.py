"""确认（任务 C2，规格 `hub-inbox`「确认」）：标成已处理、立即清正文、幂等、`unknown` 不说原因。"""

from __future__ import annotations

from clawbot_hub.inbox import Acked

MINUTE = 60_000


def test_ack_marks_the_message_done_and_clears_its_text(world):
    x = world.add("xx")
    queued = world.say(x, "持仓", token="ctx-keep")
    [item] = world.lease_all(x)
    assert world.inbox.ack(x, [item.id]) == Acked(acked=1, unknown=[])
    row = world.row(queued.id)
    # 正文立即清掉；对话凭证留到回复句柄失效
    assert (row["state"], row["text"], row["context_token"]) == ("acked", None, "ctx-keep")


def test_an_acked_message_is_not_given_again(world, clock):
    x = world.add("xx")
    world.say(x, "持仓")
    [item] = world.lease_all(x)
    world.inbox.ack(x, [item.id])
    clock.advance(120)
    assert world.lease_all(x) == []


def test_ack_after_the_lease_ended_still_counts(world, clock):
    x = world.add("xx")
    world.say(x, "持仓")
    [item] = world.lease_all(x)
    clock.advance(70)  # 处理了 70 秒，租期已过但没有再取过话
    assert world.inbox.ack(x, [item.id]) == Acked(acked=1, unknown=[])


def test_acking_twice_is_harmless(world):
    x = world.add("xx")
    world.say(x, "持仓")
    [item] = world.lease_all(x)
    world.inbox.ack(x, [item.id])
    before = dict(world.row(item.id))
    assert world.inbox.ack(x, [item.id]) == Acked(acked=0, unknown=[item.id])
    assert dict(world.row(item.id)) == before


def test_another_projects_message_is_unknown_and_untouched(world):
    x, y = world.add("xx"), world.add("yy")
    world.say(x, "给 x 的")
    [item] = world.lease_all(x)
    assert world.inbox.ack(y, [item.id]) == Acked(acked=0, unknown=[item.id])
    row = world.row(item.id)
    assert (row["state"], row["text"]) == ("leased", "给 x 的")


def test_a_made_up_id_is_unknown(world):
    x = world.add("xx")
    assert world.inbox.ack(x, ["m_00000000"]) == Acked(acked=0, unknown=["m_00000000"])


def test_a_message_that_was_never_handed_out_is_unknown(world):
    x = world.add("xx")
    queued = world.say(x, "持仓")
    assert world.inbox.ack(x, [queued.id]) == Acked(acked=0, unknown=[queued.id])
    assert world.row(queued.id)["state"] == "queued"


def test_expired_and_dropped_messages_are_unknown_and_unchanged(make_world, clock):
    world = make_world(inbox={"max_queued": 2})
    x = world.add("xx")
    dropped = world.say(x, "会被挤掉").id
    expired = world.say(x, "会过期").id
    world.say(x, "挤掉最旧的一句")
    assert world.row(dropped)["state"] == "dropped"
    clock.advance(11 * 60)
    world.inbox.cleanup()
    assert world.row(expired)["state"] == "expired"
    before = [dict(world.row(i)) for i in (dropped, expired)]
    assert world.inbox.ack(x, [dropped, expired]) == Acked(acked=0, unknown=[dropped, expired])
    assert [dict(world.row(i)) for i in (dropped, expired)] == before


def test_ack_mixes_known_and_unknown_ids_in_order(world):
    x = world.add("xx")
    world.say(x, "一")
    world.say(x, "二")
    first, second = world.lease_all(x)
    result = world.inbox.ack(x, ["m_nope0001", first.id, "m_nope0002", second.id])
    assert result == Acked(acked=2, unknown=["m_nope0001", "m_nope0002"])


def test_an_id_listed_twice_counts_once(world):
    x = world.add("xx")
    world.say(x, "持仓")
    [item] = world.lease_all(x)
    assert world.inbox.ack(x, [item.id, item.id]) == Acked(acked=1, unknown=[])


def test_an_empty_list_is_fine(world):
    x = world.add("xx")
    assert world.inbox.ack(x, []) == Acked(acked=0, unknown=[])


def test_ack_does_not_touch_other_leased_messages(world):
    x = world.add("xx")
    world.say(x, "一")
    world.say(x, "二")
    first, second = world.lease_all(x)
    world.inbox.ack(x, [first.id])
    assert world.row(second.id)["state"] == "leased"
    assert world.row(second.id)["text"] == "二"


def test_ack_counts_as_a_sign_of_life(world, clock):
    x = world.add("xx")
    assert world.project("xx").last_inbox_ms is None
    world.inbox.ack(x, [])
    assert world.project("xx").last_inbox_ms == clock.now_ms()
