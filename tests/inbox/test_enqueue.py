"""入队（任务 C2，规格 `hub-inbox`「取话」里队列上限的部分）：poller 在自己的事务里调
`Inbox.enqueue`，把路由好的一句话存成待取记录。"""

from __future__ import annotations

import re

import pytest

from clawbot_hub.inbox import Enqueued, QueueFull

MINUTE = 60_000
HOUR = 60 * MINUTE


def test_enqueue_stores_a_queued_record(world, clock):
    x = world.add("xx")
    now = clock.now_ms()
    result = world.say(
        x, "状态", at_ms=now - 5_000, received_ms=now - 1_000, via="prefix", token="ctx-9"
    )
    assert isinstance(result, Enqueued)
    row = world.row(result.id)
    assert dict(row) | {"handle": None, "handle_expires_ms": None} == {
        "id": result.id,
        "project_id": x.id,
        "upstream_key": None,
        "window_id": None,
        "text": "状态",
        "has_text": 1,
        "at_ms": now - 5_000,
        "received_ms": now - 1_000,
        "via": "prefix",
        "state": "queued",
        "lease_until_ms": None,
        "attempts": 0,
        "handle": None,
        "context_token": "ctx-9",
        "handle_expires_ms": None,
        "replies": 0,
    }


def test_window_and_upstream_key_are_kept(world, database, clock):
    x = world.add("xx")
    with database.txn() as conn:
        result = world.inbox.enqueue(
            conn,
            x,
            text="状态",
            has_text=True,
            at_ms=clock.now_ms(),
            received_ms=clock.now_ms(),
            via="default",
            context_token="ctx-1",
            window_id=7,
            upstream_key="msg-123",
        )
    row = world.row(result.id)
    assert (row["window_id"], row["upstream_key"]) == (7, "msg-123")


def test_a_message_without_text_is_stored_with_empty_text(world):
    x = world.add("xx")
    row = world.row(world.say(x, "", has_text=False).id)
    assert (row["text"], row["has_text"], row["state"]) == ("", 0, "queued")


def test_ids_and_handles_have_the_documented_shapes_and_are_unique(world):
    x = world.add("xx")
    rows = [world.row(world.say(x, f"第{n}句").id) for n in range(60)]
    assert all(re.fullmatch(r"m_[0-9a-f]{8}", row["id"]) for row in rows)
    assert all(re.fullmatch(r"rh_[A-Za-z0-9_-]{22}", row["handle"]) for row in rows)
    assert len({row["id"] for row in rows}) == len({row["handle"] for row in rows}) == 60


def test_handles_do_not_contain_the_conversation_token(world):
    x = world.add("xx")
    row = world.row(world.say(x, "状态", token="ctx-SECRET-TOKEN").id)
    assert "SECRET" not in row["handle"] and row["handle"] not in row["context_token"]


def test_handle_lasts_30_minutes_from_receipt(world, clock):
    x = world.add("xx")
    now = clock.now_ms()
    row = world.row(world.say(x, "状态", at_ms=now, received_ms=now + 4_000).id)
    assert row["handle_expires_ms"] == now + 4_000 + 30 * MINUTE


def test_handle_never_outlives_one_window_after_the_message_itself(world, clock):
    x = world.add("xx")
    now = clock.now_ms()
    # 补收的旧消息：话是 30 小时前说的，窗口（24 小时）早就过了，句柄一开始就是失效的
    old = world.row(world.say(x, "旧", at_ms=now - 30 * HOUR, received_ms=now).id)
    assert old["handle_expires_ms"] == now - 30 * HOUR + 24 * HOUR
    # 话是 23 小时 50 分钟前说的：窗口只剩 10 分钟，比 30 分钟短
    late = world.row(world.say(x, "晚", at_ms=now - 23 * HOUR - 50 * MINUTE, received_ms=now).id)
    assert late["handle_expires_ms"] == now + 10 * MINUTE


def test_handle_minutes_and_window_hours_come_from_the_config(make_world, clock):
    world = make_world(reply={"handle_minutes": 5}, quota={"window_hours": 2})
    x = world.add("xx")
    now = clock.now_ms()
    short = world.row(world.say(x, "短", at_ms=now, received_ms=now).id)
    assert short["handle_expires_ms"] == now + 5 * MINUTE
    old = world.row(world.say(x, "旧", at_ms=now - HOUR - 58 * MINUTE, received_ms=now).id)
    assert old["handle_expires_ms"] == now + 2 * MINUTE


# ---------------------------------------------------------------- 队列上限


def small_queue(make_world, size=3):
    world = make_world(inbox={"max_queued": size})
    return world, world.add("xx")


def test_full_queue_drops_the_oldest_waiting_message(make_world, clock):
    world, x = small_queue(make_world)
    now = clock.now_ms()
    first, second, third = (world.say(x, f"ZZ-{n}", at_ms=now + n).id for n in range(3))
    result = world.say(x, "ZZ-3", at_ms=now + 3)
    assert isinstance(result, Enqueued) and result.dropped_oldest == first
    dropped = world.row(first)
    assert (dropped["state"], dropped["text"], dropped["context_token"]) == ("dropped", None, None)
    assert [world.row(i)["state"] for i in (second, third, result.id)] == ["queued"] * 3


def test_the_oldest_is_by_message_time_then_arrival_order(make_world, clock):
    world, x = small_queue(make_world)
    now = clock.now_ms()
    late = world.say(x, "晚", at_ms=now + 30).id
    early = world.say(x, "早", at_ms=now + 10).id
    middle = world.say(x, "中", at_ms=now + 20).id
    assert world.say(x, "新", at_ms=now + 40).dropped_oldest == early
    assert world.say(x, "新2", at_ms=now + 50).dropped_oldest == middle
    assert world.row(late)["state"] == "queued"


def test_messages_with_the_same_time_are_dropped_in_arrival_order(make_world, clock):
    world, x = small_queue(make_world)
    now = clock.now_ms()
    first, second, third = (world.say(x, f"第{n}句", at_ms=now).id for n in range(3))
    assert world.say(x, "新", at_ms=now).dropped_oldest == first
    assert world.say(x, "新2", at_ms=now).dropped_oldest == second
    assert world.row(third)["state"] == "queued"


def test_a_drop_is_recorded_as_an_event_without_text(make_world):
    world, x = small_queue(make_world)
    for n in range(3):
        world.say(x, f"ZZ-UNIQUE-{n}")
    world.say(x, "ZZ-UNIQUE-3")
    [event] = world.events("queue")
    assert (event["level"], event["project"]) == ("WARNING", "xx")
    assert "ZZ-UNIQUE" not in event["detail"] and "ctx-" not in event["detail"]


def test_a_queue_with_room_drops_nothing(make_world):
    world, x = small_queue(make_world)
    results = [world.say(x, f"第{n}句") for n in range(3)]
    assert all(r.dropped_oldest is None for r in results)
    assert world.events("queue") == []


def test_leased_messages_count_toward_the_limit_but_are_not_dropped(make_world, clock):
    world, x = small_queue(make_world)
    now = clock.now_ms()
    ids = [world.say(x, f"第{n}句", at_ms=now + n).id for n in range(3)]
    [leased] = world.inbox.fetch(x, wait=0, max_items=1)
    assert leased.id == ids[0]
    result = world.say(x, "新", at_ms=now + 9)
    # 租出去的那句在租期里，丢的是最旧的"没租出去的"
    assert result.dropped_oldest == ids[1]
    assert world.row(ids[0])["state"] == "leased"


def test_queue_full_of_leased_messages_refuses_the_new_one(make_world, clock):
    world, x = small_queue(make_world)
    ids = [world.say(x, f"第{n}句").id for n in range(3)]
    assert len(world.inbox.fetch(x, wait=0, max_items=3)) == 3
    before = [dict(r) for r in world.rows()]
    result = world.say(x, "ZZ-NEW")
    assert isinstance(result, QueueFull)
    assert [dict(r) for r in world.rows()] == before  # 没有新记录，租出去的也没动
    assert [world.row(i)["state"] for i in ids] == ["leased"] * 3
    [event] = world.events("queue")
    assert (event["level"], event["project"]) == ("WARNING", "xx")
    assert "ZZ-NEW" not in event["detail"]


def test_leased_messages_whose_lease_ended_still_block_the_queue(make_world, clock):
    world, x = small_queue(make_world)
    for n in range(3):
        world.say(x, f"第{n}句")
    world.inbox.fetch(x, wait=0, max_items=3)
    clock.advance(120)  # 租期过了，但还没被重新租出去：仍是项目手里的话
    assert isinstance(world.say(x, "新"), QueueFull)


def test_each_project_has_its_own_queue(make_world):
    world, x = small_queue(make_world)
    y = world.add("yy")
    for n in range(3):
        world.say(x, f"第{n}句")
    assert world.say(y, "别的项目").dropped_oldest is None
    assert len(world.rows(y)) == 1


def test_acked_and_expired_messages_do_not_count(make_world, clock):
    world, x = small_queue(make_world)
    for n in range(3):
        world.say(x, f"第{n}句")
    items = world.inbox.fetch(x, wait=0, max_items=3)
    world.inbox.ack(x, [item.id for item in items])
    result = world.say(x, "新")
    assert isinstance(result, Enqueued) and result.dropped_oldest is None


# ---------------------------------------------------------------- 等确认标记


def set_marker(world, project, until_ms):
    with world.db.txn() as conn:
        conn.execute(
            "UPDATE projects SET awaiting_until_ms = ? WHERE id = ?", (until_ms, project.id)
        )


def marker(world, project):
    row = (
        world.db.conn()
        .execute("SELECT awaiting_until_ms FROM projects WHERE id = ?", (project.id,))
        .fetchone()
    )
    return row["awaiting_until_ms"]


def test_a_message_clears_the_awaiting_marker_of_its_project_only(world, clock):
    x, y = world.add("xx"), world.add("yy")
    until = clock.now_ms() + 60_000
    set_marker(world, x, until)
    set_marker(world, y, until)
    world.say(x, "对", via="confirm")
    assert marker(world, x) is None
    assert marker(world, y) == until


def test_a_refused_message_does_not_clear_the_marker(make_world, clock):
    world, x = small_queue(make_world)
    for n in range(3):
        world.say(x, f"第{n}句")
    world.inbox.fetch(x, wait=0, max_items=3)
    until = clock.now_ms() + 60_000
    set_marker(world, x, until)
    assert isinstance(world.say(x, "对"), QueueFull)
    assert marker(world, x) == until


# ---------------------------------------------------------------- 移除项目


def test_removing_a_project_drops_what_it_never_collected(world):
    x = world.add("xx")
    [first, second] = [world.say(x, f"ZZ-{n}").id for n in range(2)]
    world.inbox.fetch(x, wait=0, max_items=1)
    world.projects.disable("xx")
    world.projects.remove("xx", yes=True)
    for item_id in (first, second):
        row = world.row(item_id)
        assert (row["state"], row["text"], row["context_token"]) == ("dropped", None, None)


@pytest.mark.parametrize("n", [0, 1])
def test_dropped_oldest_is_none_when_nothing_was_dropped(world, n):
    x = world.add("xx")
    for i in range(n):
        world.say(x, f"第{i}句")
    assert world.say(x, "新").dropped_oldest is None
