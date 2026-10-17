"""关闭（任务 C4，规格 `hub-deploy`「正常关闭」）：挂着的取话请求马上返回空列表，之后的取话不再
租出话。接口层的 503 在 `tests/api/test_inbox.py`。"""

from __future__ import annotations

from tests.support.threads import run_in_thread


def test_shutdown_lets_a_waiting_fetch_go_with_an_empty_list_at_once(world, clock):
    x = world.add("xx")
    now = clock.now_ms()
    worker = run_in_thread(world.inbox.fetch, x, wait=30, max_items=10)
    clock.wait_until_blocked()

    world.inbox.shutdown()

    assert worker.result() == []
    assert clock.now_ms() == now  # 不用等时间走


def test_a_fetch_let_go_by_the_shutdown_leases_nothing_even_when_a_message_is_there(world, clock):
    x = world.add("xx")
    worker = run_in_thread(world.inbox.fetch, x, wait=30, max_items=10)
    clock.wait_until_blocked()
    world.say(x, "悄悄入队", wake=False)

    world.inbox.shutdown()

    assert worker.result() == []
    assert world.states() == ["queued"]
    assert world.row(world.rows()[0]["id"])["attempts"] == 0


def test_every_waiting_project_is_let_go(world, clock):
    x, y = world.add("xx"), world.add("yy")
    first = run_in_thread(world.inbox.fetch, x, wait=30, max_items=10)
    second = run_in_thread(world.inbox.fetch, y, wait=30, max_items=10)
    clock.wait_until_blocked(2)

    world.inbox.shutdown()

    assert (first.result(), second.result()) == ([], [])


def test_a_fetch_after_the_shutdown_returns_nothing_at_once_and_leases_nothing(world, clock):
    x = world.add("xx")
    world.say(x, "话")
    world.inbox.shutdown()
    now = clock.now_ms()

    assert world.inbox.fetch(x, wait=0, max_items=10) == []
    assert world.inbox.fetch(x, wait=30, max_items=10) == []

    assert clock.now_ms() == now
    assert world.states() == ["queued"]


def test_shutdown_twice_and_with_nobody_waiting_is_fine(world):
    world.inbox.shutdown()
    world.inbox.shutdown()


def test_a_shutdown_that_lands_right_after_the_state_was_read_does_not_leave_the_fetch_asleep(
    world, monkeypatch
):
    x = world.add("xx")
    inbox = world.inbox
    real_seen = inbox._seen

    def seen_just_after_the_shutdown(project_id):
        inbox.shutdown()
        return real_seen(project_id)

    monkeypatch.setattr(inbox, "_seen", seen_just_after_the_shutdown)

    worker = run_in_thread(inbox.fetch, x, wait=30, max_items=10)

    assert worker.result() == []  # 关闭的标志在"看到的叫醒次数"之后读，所以一定看得见


def test_a_shutdown_that_lands_while_the_fetch_looks_for_messages_does_not_leave_it_asleep(
    world, monkeypatch
):
    x = world.add("xx")
    inbox = world.inbox
    real_lease = inbox._lease

    def lease_then_shutdown(*args, **kwargs):
        leased = real_lease(*args, **kwargs)
        inbox.shutdown()  # 查完没有话、还没睡下，关闭来了
        return leased

    monkeypatch.setattr(inbox, "_lease", lease_then_shutdown)

    worker = run_in_thread(inbox.fetch, x, wait=30, max_items=10)

    assert worker.result() == []
