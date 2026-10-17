"""等确认标记（任务 C2，规格 `hub-inbox`「等确认」、`hub-send`「回复」）。

写入：回复结果至少发出一条时，由回复接口（C4）调 `mark_awaiting`——被回复的那句话之后，这个项目
没有再收到过新的话才写（"之后"按 `(at_ms, 入库先后)` 比较）。清除：收到任何话（入队时，见
`test_enqueue.py`）、停用（见 `tests/projects`）、以及路由发现标记已不管用时（`clear_awaiting`）。
标记是否生效、要不要清，由路由当场判断（`tests/router`）。"""

from __future__ import annotations


def marker(world, name="xx"):
    return world.project(name).awaiting_until_ms


def reply_to(world, project, item, until_ms):
    return world.inbox.mark_awaiting(project, item.id, until_ms)


def test_marker_is_written_when_nothing_newer_arrived(world, clock):
    x = world.add("xx")
    world.say(x, "问")
    [item] = world.lease_all(x)
    until = clock.now_ms() + 600_000
    assert reply_to(world, x, item, until) is True
    assert marker(world) == until


def test_a_newer_message_stops_the_marker_from_being_written(world, clock):
    x = world.add("xx")
    now = clock.now_ms()
    world.say(x, "问", at_ms=now)
    world.say(x, "回复期间又来的新话", at_ms=now + 1_000)
    [item, _] = world.lease_all(x)
    assert reply_to(world, x, item, now + 600_000) is False
    assert marker(world) is None


def test_after_means_message_time_first(world, clock):
    x = world.add("xx")
    now = clock.now_ms()
    world.say(x, "问", at_ms=now)
    world.say(x, "补收的更早的话", at_ms=now - 1_000)  # 入库在后，但话是更早说的
    item = next(i for i in world.lease_all(x) if i.text == "问")
    assert reply_to(world, x, item, now + 600_000) is True


def test_with_the_same_message_time_arrival_order_decides(world, clock):
    x = world.add("xx")
    now = clock.now_ms()
    earlier = world.say(x, "先入库", at_ms=now)
    world.say(x, "后入库", at_ms=now)
    items = {i.id: i for i in world.lease_all(x)}
    # 回复先入库的那句：同一时间、后入库的在它"之后"
    assert reply_to(world, x, items[earlier.id], now + 600_000) is False
    assert marker(world) is None


def test_a_message_with_the_same_time_that_arrived_earlier_is_not_newer(world, clock):
    x = world.add("xx")
    now = clock.now_ms()
    world.say(x, "先入库", at_ms=now)
    asked = world.say(x, "后入库", at_ms=now)
    items = {i.id: i for i in world.lease_all(x)}
    assert reply_to(world, x, items[asked.id], now + 600_000) is True


def test_an_unknown_item_gets_no_marker(world, clock):
    x = world.add("xx")
    assert world.inbox.mark_awaiting(x, "m_nosuchid", clock.now_ms() + 600_000) is False
    assert marker(world) is None


def test_an_item_of_another_project_gets_no_marker(world, clock):
    x, y = world.add("xx"), world.add("yy")
    world.say(y, "问")
    [item] = world.lease_all(y)
    assert world.inbox.mark_awaiting(x, item.id, clock.now_ms() + 600_000) is False
    assert marker(world, "xx") is None and marker(world, "yy") is None


def test_newer_messages_of_other_projects_do_not_matter(world, clock):
    x, y = world.add("xx"), world.add("yy")
    now = clock.now_ms()
    world.say(x, "问", at_ms=now)
    world.say(y, "别的项目的新话", at_ms=now + 5_000)
    [item] = world.lease_all(x)
    assert reply_to(world, x, item, now + 600_000) is True


def test_a_newer_message_counts_in_any_state(world, clock):
    x = world.add("xx")
    now = clock.now_ms()
    world.say(x, "问", at_ms=now)
    world.say(x, "新话", at_ms=now + 1_000)
    asked, newer = world.lease_all(x)
    world.inbox.ack(x, [newer.id])  # 项目已经处理完、确认了
    assert reply_to(world, x, asked, now + 600_000) is False


def test_the_latest_marker_replaces_the_earlier_one(world, clock):
    x = world.add("xx")
    now = clock.now_ms()
    world.say(x, "问", at_ms=now)
    [item] = world.lease_all(x)
    reply_to(world, x, item, now + 600_000)
    reply_to(world, x, item, now + 60_000)
    assert marker(world) == now + 60_000


def test_a_refused_marker_leaves_an_existing_one_alone(world, clock):
    x = world.add("xx")
    now = clock.now_ms()
    world.say(x, "问", at_ms=now)
    [item] = world.lease_all(x)
    reply_to(world, x, item, now + 600_000)
    with world.db.txn() as conn:  # 之后来了新话（直接写库，不经入队，免得入队顺手清掉标记）
        conn.execute(
            "INSERT INTO inbox (id, project_id, text, has_text, at_ms, received_ms, via, state)"
            " VALUES ('m_newer001', ?, '新话', 1, ?, ?, 'default', 'queued')",
            (x.id, now + 1_000, now + 1_000),
        )
    assert reply_to(world, x, item, now + 300_000) is False
    assert marker(world) == now + 600_000


def test_only_the_replying_project_gets_the_marker(world, clock):
    x = world.add("xx")
    world.add("yy")
    world.say(x, "问")
    [item] = world.lease_all(x)
    reply_to(world, x, item, clock.now_ms() + 600_000)
    assert marker(world, "yy") is None


# ---------------------------------------------------------------- 清除


def test_clearing_removes_only_the_named_markers(world, clock):
    for name in ("xx", "yy", "zz"):
        world.add(name)
    until = clock.now_ms() + 600_000
    with world.db.txn() as conn:
        conn.execute("UPDATE projects SET awaiting_until_ms = ?", (until,))
    with world.db.txn() as conn:
        world.inbox.clear_awaiting(conn, ["xx", "zz"])
    assert (marker(world, "xx"), marker(world, "yy"), marker(world, "zz")) == (None, until, None)


def test_clearing_takes_any_iterable_of_names(world, clock):
    for name in ("xx", "yy"):
        world.add(name)
    until = clock.now_ms() + 600_000
    with world.db.txn() as conn:
        conn.execute("UPDATE projects SET awaiting_until_ms = ?", (until,))
    with world.db.txn() as conn:
        world.inbox.clear_awaiting(conn, (name for name in ("xx", "yy")))
    assert (marker(world, "xx"), marker(world, "yy")) == (None, None)


def test_clearing_unknown_names_or_nothing_is_fine(world):
    world.add("xx")
    with world.db.txn() as conn:
        world.inbox.clear_awaiting(conn, [])
        world.inbox.clear_awaiting(conn, ["nobody"])


def test_disabling_a_project_clears_its_marker(world, clock):
    x = world.add("xx")
    world.say(x, "问")
    [item] = world.lease_all(x)
    reply_to(world, x, item, clock.now_ms() + 600_000)
    world.projects.disable("xx")
    assert marker(world) is None


def test_the_marker_survives_a_restart(world, clock):
    from clawbot_hub.inbox import Inbox

    x = world.add("xx")
    world.say(x, "问")
    [item] = world.lease_all(x)
    until = clock.now_ms() + 600_000
    reply_to(world, x, item, until)
    Inbox(world.db, world.config, clock, world.projects)  # 重启：标记在库里，不在内存里
    assert marker(world) == until
