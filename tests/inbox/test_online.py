"""项目在线判断（任务 C2，规格 `hub-projects`「项目在线判断」）。

在线 = 此刻有它的取话请求挂着，或者最近 `inbox.online_seconds`（90）秒内有过取话 / 确认 /
`max=0` 的心跳。已停用、已移除、只推送的项目永远不在线。"""

from __future__ import annotations

import pytest

from clawbot_hub.inbox import Inbox
from clawbot_hub.projects import ProjectDisabled
from tests.support.threads import run_in_thread, wait_for


def online(world, name="xx") -> bool:
    return world.inbox.online(world.project(name))


def test_a_project_that_never_asked_is_offline(world):
    world.add("xx")
    assert not online(world)


def test_fetching_makes_a_project_online_for_ninety_seconds(world, clock):
    x = world.add("xx")
    world.inbox.fetch(x, wait=0, max_items=10)
    assert online(world)
    clock.advance(90)
    assert online(world)
    clock.advance(0.001)
    assert not online(world)


def test_the_online_period_comes_from_the_config(make_world, clock):
    world = make_world(inbox={"online_seconds": 30})
    x = world.add("xx")
    world.inbox.fetch(x, wait=0, max_items=10)
    clock.advance(30)
    assert online(world)
    clock.advance(0.001)
    assert not online(world)


def test_a_heartbeat_counts(world, clock):
    x = world.add("xx")
    world.inbox.fetch(x, wait=0, max_items=0)
    clock.advance(60)
    assert online(world)


def test_an_ack_counts(world, clock):
    x = world.add("xx")
    world.inbox.ack(x, [])
    clock.advance(60)
    assert online(world)


def test_sending_does_not_count(world, clock):
    # 发消息不影响在线：这里只证明"没取过话"就是离线，哪怕 `last_seen` 是新的
    x = world.add("xx")
    with world.db.txn() as conn:
        conn.execute("UPDATE projects SET last_seen_ms = ? WHERE id = ?", (clock.now_ms(), x.id))
    assert not online(world)


def test_every_fetch_leaves_the_time_it_arrived(world, clock):
    x = world.add("xx")
    clock.advance(7)
    world.inbox.fetch(x, wait=0, max_items=10)
    assert world.project("xx").last_inbox_ms == clock.now_ms()


def test_the_online_state_survives_a_restart(world, clock):
    x = world.add("xx")
    world.inbox.fetch(x, wait=0, max_items=10)
    clock.advance(30)
    restarted = Inbox(world.db, world.config, clock, world.projects)
    assert restarted.online(world.project("xx"))
    clock.advance(61)
    assert not restarted.online(world.project("xx"))


def test_a_clock_that_went_back_counts_as_just_now(world, clock):
    x = world.add("xx")
    with world.db.txn() as conn:
        conn.execute(
            "UPDATE projects SET last_inbox_ms = ? WHERE id = ?", (clock.now_ms() + 600_000, x.id)
        )
    assert online(world)


# ---------------------------------------------------------------- 永远不在线的


def test_disabled_projects_are_never_online(world):
    x = world.add("xx")
    world.inbox.fetch(x, wait=0, max_items=10)
    world.projects.disable("xx")
    assert not online(world)


def test_push_only_projects_are_never_online(world, clock):
    pusher = world.add("pusher", push_only=True)
    with world.db.txn() as conn:
        conn.execute(
            "UPDATE projects SET last_inbox_ms = ? WHERE id = ?", (clock.now_ms(), pusher.id)
        )
    assert not online(world, "pusher")


def test_a_disabled_project_is_offline_even_while_its_request_still_hangs(world, clock):
    x = world.add("xx")
    worker = run_in_thread(world.inbox.fetch, x, wait=60, max_items=10)
    clock.wait_until_blocked()
    assert online(world)
    world.projects.disable("xx")  # 挂着的请求最迟 5 秒后才发现，但这一刻起就不算在线
    assert not online(world)
    clock.advance(5)
    with pytest.raises(ProjectDisabled):
        worker.result()


def test_a_push_only_project_is_offline_even_with_a_hanging_request(world, clock):
    pusher = world.add("pusher", push_only=True)
    worker = run_in_thread(world.inbox.fetch, pusher, wait=60, max_items=10)
    clock.wait_until_blocked()  # 接口层会拒绝它，这里直接调是为了看"在线"的判断自己也不信
    assert not online(world, "pusher")
    world.inbox.wake(pusher.id)
    clock.advance(60)
    worker.result()


def test_online_names_lists_the_projects_that_are_online(world, clock):
    x, y = world.add("xx"), world.add("yy")
    world.add("zz")
    world.inbox.fetch(x, wait=0, max_items=0)
    clock.advance(100)
    world.inbox.fetch(y, wait=0, max_items=0)
    world.projects.disable("zz")
    names = world.inbox.online_names(world.projects.list())
    assert names == frozenset({"yy"})


# ---------------------------------------------------------------- 挂着的请求


def test_a_hanging_request_counts_even_when_it_arrived_long_ago(make_world, clock):
    world = make_world(inbox={"online_seconds": 1, "max_wait": 120})
    x = world.add("xx")
    worker = run_in_thread(world.inbox.fetch, x, wait=60, max_items=10)
    clock.wait_until_blocked()
    clock.advance(3)  # 还没到下一段，库里的"最近取话"已经是 3 秒前，超过了 1 秒
    assert world.project("xx").last_inbox_ms == clock.now_ms() - 3_000
    assert online(world)
    world.say(x, "来了")
    worker.result()
    assert online(world)  # 请求刚返回，库里的时间是刚刚
    clock.advance(1.001)
    assert not online(world)


def test_a_long_wait_keeps_refreshing_the_time_in_the_database(make_world, clock):
    world = make_world(inbox={"online_seconds": 90, "max_wait": 120})
    x = world.add("xx")
    start = clock.now_ms()
    worker = run_in_thread(world.inbox.fetch, x, wait=120, max_items=10)
    clock.wait_until_blocked()
    clock.advance(100)  # 另一个进程（hubctl）只看库：挂了 100 秒也要是在线
    wait_for(lambda: world.project("xx").last_inbox_ms == start + 100_000)
    assert online(world)
    clock.advance(20)
    assert worker.result() == []
    assert world.project("xx").last_inbox_ms == start + 120_000
    clock.advance(90)
    assert online(world)
    clock.advance(0.001)
    assert not online(world)


def test_the_request_stops_counting_when_it_returns(make_world, clock):
    world = make_world(inbox={"online_seconds": 1})
    x = world.add("xx")
    worker = run_in_thread(world.inbox.fetch, x, wait=10, max_items=10)
    clock.wait_until_blocked()
    world.say(x, "来了")
    worker.result()
    clock.advance(2)
    assert not online(world)


def test_a_superseded_request_hands_over_the_online_state(make_world, clock):
    world = make_world(inbox={"online_seconds": 1})
    x = world.add("xx")
    first = run_in_thread(world.inbox.fetch, x, wait=60, max_items=10)
    clock.wait_until_blocked()
    second = run_in_thread(world.inbox.fetch, x, wait=60, max_items=10)
    first.result()
    clock.advance(3)
    assert online(world)  # 新的请求还挂着
    world.say(x, "来了")
    second.result()


def test_a_request_that_ended_with_a_refusal_leaves_no_online_state(make_world, clock):
    world = make_world(inbox={"online_seconds": 1})
    x = world.add("xx")
    worker = run_in_thread(world.inbox.fetch, x, wait=60, max_items=10)
    clock.wait_until_blocked()
    world.projects.disable("xx")
    clock.advance(5)
    try:
        worker.result()
    except Exception:  # 停用了：以 ProjectDisabled 结束
        pass
    world.projects.enable("xx")
    clock.advance(2)
    assert not online(world)


# ---------------------------------------------------------------- 处理慢指令


def test_a_project_busy_with_a_slow_command_stays_online_by_heartbeats(world, clock):
    x = world.add("xx")
    world.say(x, "诊断")
    world.lease_all(x)
    for _ in range(4):  # 处理了 120 秒，每 30 秒一次心跳
        clock.advance(30)
        world.inbox.fetch(x, wait=0, max_items=0)
        assert online(world)
    world.say(x, "另一句")
    assert online(world)


def test_without_heartbeats_the_same_project_would_be_offline(world, clock):
    x = world.add("xx")
    world.say(x, "诊断")
    world.lease_all(x)
    clock.advance(120)
    assert not online(world)
