"""清理（任务 C2，规格 `hub-inbox`「清理」）：每分钟一次（调度归 D2），这里测清理本身。"""

from __future__ import annotations

from clawbot_hub.inbox import Cleaned

MINUTE = 60_000
DAY = 24 * 60 * MINUTE


def test_queued_messages_past_retention_expire_with_text_and_token_cleared(world, clock):
    x = world.add("xx")
    queued = world.say(x, "持仓", token="ctx-own")
    clock.advance(10 * 60)
    assert world.inbox.cleanup() == Cleaned()
    assert world.row(queued.id)["state"] == "queued"
    clock.advance(0.001)
    result = world.inbox.cleanup()
    row = world.row(queued.id)
    assert (row["state"], row["text"], row["context_token"]) == ("expired", None, None)
    assert result == Cleaned(expired=1)


def test_leased_messages_whose_lease_ended_expire_but_keep_the_token(world, clock):
    x = world.add("xx")
    queued = world.say(x, "持仓", token="ctx-own")
    clock.advance(9 * 60)
    world.lease_all(x)
    clock.advance(2 * 60)
    world.inbox.cleanup()
    row = world.row(queued.id)
    assert (row["state"], row["text"], row["context_token"]) == ("expired", None, "ctx-own")


def test_messages_inside_their_lease_are_left_alone(world, clock):
    x = world.add("xx")
    queued = world.say(x, "持仓")
    clock.advance(9.5 * 60)
    world.lease_all(x)
    clock.advance(40)  # 已超过保留期，租期还没结束
    assert world.inbox.cleanup() == Cleaned()
    assert world.row(queued.id)["state"] == "leased"


def test_a_lease_that_ends_exactly_now_no_longer_protects_the_message(world, clock):
    x = world.add("xx")
    queued = world.say(x, "持仓")
    clock.advance(9.5 * 60)
    world.lease_all(x)  # 租到第 10 分 30 秒
    clock.advance(59.999)  # 已过保留期，租期还差 1 毫秒
    assert world.inbox.cleanup() == Cleaned()
    assert world.row(queued.id)["state"] == "leased"
    clock.advance(0.001)
    assert world.inbox.cleanup() == Cleaned(expired=1)


def test_exhausted_messages_are_dropped_with_an_event(make_world, clock):
    world = make_world(inbox={"max_attempts": 2})
    x = world.add("xx")
    queued = world.say(x, "ZZ-UNIQUE-TEXT", token="ctx-own")
    world.lease_all(x)
    clock.advance(60)
    world.lease_all(x)
    assert world.inbox.cleanup() == Cleaned()  # 第 2 次租期还没结束
    clock.advance(60)
    result = world.inbox.cleanup()
    row = world.row(queued.id)
    # 作废：正文清掉，对话凭证照租出去过的规矩留到句柄失效
    assert (row["state"], row["text"], row["context_token"]) == ("dropped", None, "ctx-own")
    assert result == Cleaned(dropped=1)
    [event] = world.events("queue")
    assert (event["level"], event["project"]) == ("WARNING", "xx")
    assert queued.id in event["detail"] and "被取走 2 次" in event["detail"]
    assert "ZZ-UNIQUE-TEXT" not in event["detail"] and "ctx-own" not in event["detail"]


def test_exhausted_beats_expired_when_both_apply(make_world, clock):
    world = make_world(inbox={"max_attempts": 1})
    x = world.add("xx")
    queued = world.say(x, "持仓")
    world.lease_all(x)
    clock.advance(11 * 60)  # 租期结束，也过了保留期
    result = world.inbox.cleanup()
    assert world.row(queued.id)["state"] == "dropped"
    assert result == Cleaned(dropped=1)
    assert len(world.events("queue")) == 1


def test_conversation_tokens_are_cleared_when_the_handle_expires(world, clock):
    x = world.add("xx")
    queued = world.say(x, "持仓", token="ctx-own")
    [item] = world.lease_all(x)
    world.inbox.ack(x, [item.id])
    clock.advance(30 * 60 - 0.001)
    world.inbox.cleanup()
    assert world.row(queued.id)["context_token"] == "ctx-own"
    clock.advance(0.001)
    result = world.inbox.cleanup()
    assert world.row(queued.id)["context_token"] is None
    assert result == Cleaned(tokens=1)


def test_old_messages_lose_their_token_even_before_anyone_collects_them(world, clock):
    x = world.add("xx")
    now = clock.now_ms()
    # 补收到 30 小时前的话：句柄从一开始就是失效的，凭证不必留
    queued = world.say(x, "旧", at_ms=now - 30 * 60 * MINUTE, token="ctx-old")
    world.inbox.cleanup()
    row = world.row(queued.id)
    assert (row["state"], row["context_token"]) == ("queued", None)


def test_finished_records_are_deleted_after_seven_days(world, clock):
    x = world.add("xx")
    acked = world.say(x, "确认过的").id
    expired = world.say(x, "过期的").id
    [item] = world.inbox.fetch(x, wait=0, max_items=1)
    world.inbox.ack(x, [item.id])
    clock.advance(11 * 60)
    world.inbox.cleanup()
    assert world.row(expired)["state"] == "expired"
    clock.advance(7 * 24 * 3600 - 11 * 60 - 0.001)  # 收到后 7 天差一点
    assert world.inbox.cleanup().deleted == 0
    assert len(world.rows()) == 2
    clock.advance(0.001)  # 恰好 7 天：还留着，过了才删
    assert world.inbox.cleanup().deleted == 0
    clock.advance(0.001)
    result = world.inbox.cleanup()
    assert world.rows() == [] and result == Cleaned(deleted=2)
    assert acked != expired


def test_no_finished_record_keeps_any_text(world, clock):
    x = world.add("xx")
    for n in range(4):
        world.say(x, f"ZZ-{n}")
    items = world.inbox.fetch(x, wait=0, max_items=2)
    world.inbox.ack(x, [items[0].id])
    clock.advance(11 * 60)
    world.inbox.cleanup()
    leftovers = (
        world.db.conn()
        .execute("SELECT state, text FROM inbox WHERE state IN ('acked', 'expired', 'dropped')")
        .fetchall()
    )
    assert leftovers and all(row["text"] is None for row in leftovers)


def test_dropped_records_are_deleted_too(make_world, clock):
    world = make_world(inbox={"max_queued": 1})
    x = world.add("xx")
    world.say(x, "挤掉")
    world.say(x, "留下")
    assert "dropped" in world.states()
    clock.advance(8 * 24 * 3600)
    world.inbox.cleanup()
    assert world.rows() == []


def test_a_very_old_waiting_message_is_expired_and_deleted_in_one_run(world, clock):
    x = world.add("xx")
    world.say(x, "持仓")
    clock.advance(8 * 24 * 3600)
    result = world.inbox.cleanup()
    assert world.rows() == [] and result == Cleaned(expired=1, deleted=1)


def test_the_receipt_records_are_kept_for_seven_days(world, clock):
    now = clock.now_ms()
    with world.db.txn() as conn:
        conn.executemany(
            "INSERT INTO inbound_seen (upstream_key, seen_ms) VALUES (?, ?)",
            [("old", now - 7 * DAY - 1), ("edge", now - 7 * DAY), ("fresh", now - DAY)],
        )
    result = world.inbox.cleanup()
    kept = [r["upstream_key"] for r in world.db.conn().execute("SELECT * FROM inbound_seen")]
    assert sorted(kept) == ["edge", "fresh"]
    assert result == Cleaned(seen=1)


def test_cleanup_twice_changes_nothing_more(world, clock):
    x = world.add("xx")
    world.say(x, "持仓")
    clock.advance(11 * 60)
    assert world.inbox.cleanup() == Cleaned(expired=1)
    before = [dict(r) for r in world.rows()]
    assert world.inbox.cleanup() == Cleaned()
    assert [dict(r) for r in world.rows()] == before


def test_cleanup_does_not_touch_queued_messages_within_retention(world, clock):
    x, y = world.add("xx"), world.add("yy")
    world.say(x, "给 x")
    world.say(y, "给 y")
    clock.advance(5 * 60)
    assert world.inbox.cleanup() == Cleaned()
    assert world.states() == ["queued", "queued"]


def test_unfinished_messages_are_never_deleted_by_the_seven_day_rule(make_world, clock):
    world = make_world(inbox={"retention_minutes": 20_000})  # 保留期比 7 天还长（约 14 天）
    x = world.add("xx")
    world.say(x, "取走了没确认")
    world.say(x, "还在排队")
    clock.advance(8 * 24 * 3600)
    [leased] = world.inbox.fetch(x, wait=0, max_items=1)
    assert leased.text == "取走了没确认"
    result = world.inbox.cleanup()
    assert result.deleted == 0
    assert world.states() == ["leased", "queued"]
