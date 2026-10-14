"""取话（任务 C2，规格 `hub-inbox`「取话」）：立刻返回、等待、排序、租约和重发、保留期、
分段等待时核对启用状态、新请求顶掉旧请求、心跳。"""

from __future__ import annotations

import pytest

from clawbot_hub.inbox import Leased
from clawbot_hub.projects import ProjectDisabled
from tests.support.threads import run_in_thread, wait_for

SECOND = 1000
MINUTE = 60 * SECOND


def texts(items) -> list[str]:
    return [item.text for item in items]


# ---------------------------------------------------------------- 立刻返回、等待


def test_a_waiting_message_is_returned_at_once(world, clock):
    x = world.add("xx")
    now = clock.now_ms()
    queued = world.say(x, "持仓", at_ms=now - 2_000, received_ms=now - 1_000, via="prefix")
    [item] = world.inbox.fetch(x, wait=30, max_items=10)
    row = world.row(queued.id)
    assert item == Leased(
        id=queued.id,
        text="持仓",
        has_text=True,
        at_ms=now - 2_000,
        received_ms=now - 1_000,
        attempt=1,
        via="prefix",
        reply_handle=row["handle"],
    )
    assert clock.now_ms() == now  # 不等


def test_a_message_without_text_comes_with_empty_text(world):
    x = world.add("xx")
    world.say(x, "", has_text=False)
    [item] = world.lease_all(x)
    assert (item.text, item.has_text) == ("", False)


def test_no_wait_and_nothing_queued_returns_an_empty_list_at_once(world, clock):
    x = world.add("xx")
    now = clock.now_ms()
    assert world.inbox.fetch(x, wait=0, max_items=10) == []
    assert clock.now_ms() == now


def test_waiting_ends_when_a_message_arrives(world, clock):
    x = world.add("xx")
    now = clock.now_ms()
    worker = run_in_thread(world.inbox.fetch, x, wait=30, max_items=10)
    clock.wait_until_blocked()
    assert worker.still_blocked()
    world.say(x, "状态")
    assert texts(worker.result()) == ["状态"]
    assert clock.now_ms() == now  # 一有新话立刻返回，时间没有走


def test_waiting_runs_out_and_returns_an_empty_list(world, clock):
    x = world.add("xx")
    worker = run_in_thread(world.inbox.fetch, x, wait=2, max_items=10)
    clock.wait_until_blocked()
    clock.advance(1.999)
    assert worker.still_blocked()
    clock.advance(0.001)
    assert worker.result() == []


def test_waiting_is_cut_into_segments_of_five_seconds(world, clock):
    x = world.add("xx")
    start = clock.now_ms()
    worker = run_in_thread(world.inbox.fetch, x, wait=12, max_items=10)
    clock.wait_until_blocked()
    assert clock.pending_deadlines() == [start + 5_000]
    clock.advance(5)
    wait_for(lambda: clock.pending_deadlines() == [start + 10_000])
    clock.advance(5)
    wait_for(lambda: clock.pending_deadlines() == [start + 12_000])  # 最后一段只剩 2 秒
    clock.advance(2)
    assert worker.result() == []


def test_a_message_that_is_not_announced_is_found_at_the_next_segment(world, clock):
    x = world.add("xx")
    worker = run_in_thread(world.inbox.fetch, x, wait=30, max_items=10)
    clock.wait_until_blocked()
    world.say(x, "悄悄入队", wake=False)  # 比如另一个进程写的库，没人来叫醒
    assert worker.still_blocked()
    clock.advance(5)
    assert texts(worker.result()) == ["悄悄入队"]


def test_a_message_for_another_project_does_not_wake_the_waiter(world, clock):
    x, y = world.add("xx"), world.add("yy")
    worker = run_in_thread(world.inbox.fetch, x, wait=30, max_items=10)
    clock.wait_until_blocked()
    world.say(y, "给 y 的")
    assert worker.still_blocked()
    world.say(x, "给 x 的")
    assert texts(worker.result()) == ["给 x 的"]


def test_the_result_is_only_this_projects_messages(world):
    x, y = world.add("xx"), world.add("yy")
    world.say(x, "给 x 的")
    world.say(y, "给 y 的")
    assert texts(world.lease_all(x)) == ["给 x 的"]
    assert texts(world.lease_all(y)) == ["给 y 的"]


# ---------------------------------------------------------------- 排序、条数


def test_messages_come_in_message_time_order_then_arrival_order(world, clock):
    x = world.add("xx")
    now = clock.now_ms()
    world.say(x, "第三", at_ms=now + 3_000)
    world.say(x, "第一", at_ms=now + 1_000)
    world.say(x, "第二", at_ms=now + 2_000)
    world.say(x, "第一之二", at_ms=now + 1_000)
    assert texts(world.lease_all(x)) == ["第一", "第一之二", "第二", "第三"]


def test_max_items_limits_the_batch_and_the_rest_stay_queued(world, clock):
    x = world.add("xx")
    now = clock.now_ms()
    for n in range(3):
        world.say(x, f"第{n}句", at_ms=now + n)
    assert texts(world.inbox.fetch(x, wait=0, max_items=2)) == ["第0句", "第1句"]
    assert world.states() == ["leased", "leased", "queued"]
    assert texts(world.inbox.fetch(x, wait=0, max_items=2)) == ["第2句"]


def test_max_zero_is_a_heartbeat_that_leases_nothing_and_ignores_wait(world, clock):
    x = world.add("xx")
    world.say(x, "持仓")
    now = clock.now_ms()
    assert world.inbox.fetch(x, wait=30, max_items=0) == []
    assert clock.now_ms() == now
    assert world.states() == ["queued"]


# ---------------------------------------------------------------- 租约


def test_a_leased_message_is_marked_with_its_lease(world, clock):
    x = world.add("xx")
    queued = world.say(x, "持仓")
    now = clock.now_ms()
    world.lease_all(x)
    row = world.row(queued.id)
    assert (row["state"], row["attempts"], row["lease_until_ms"]) == ("leased", 1, now + MINUTE)


def test_a_leased_message_is_not_given_again_during_the_lease(world, clock):
    x = world.add("xx")
    world.say(x, "持仓")
    world.lease_all(x)
    assert world.lease_all(x) == []
    clock.advance(59.999)
    assert world.lease_all(x) == []


def test_after_the_lease_the_same_message_comes_again_with_the_same_handle(world, clock):
    x = world.add("xx")
    queued = world.say(x, "持仓")
    [first] = world.lease_all(x)
    clock.advance(60)
    [again] = world.lease_all(x)
    assert (again.id, again.reply_handle, again.attempt) == (first.id, first.reply_handle, 2)
    assert again.id == queued.id and first.attempt == 1
    assert world.row(queued.id)["lease_until_ms"] == clock.now_ms() + MINUTE


def test_the_lease_length_comes_from_the_config(make_world, clock):
    world = make_world(inbox={"lease_seconds": 10})
    x = world.add("xx")
    world.say(x, "持仓")
    world.lease_all(x)
    clock.advance(9.999)
    assert world.lease_all(x) == []
    clock.advance(0.001)
    assert texts(world.lease_all(x)) == ["持仓"]


def test_a_message_is_leased_at_most_max_attempts_times(make_world, clock):
    world = make_world(inbox={"max_attempts": 3})
    x = world.add("xx")
    queued = world.say(x, "持仓")
    attempts = []
    for _ in range(3):
        [item] = world.lease_all(x)
        attempts.append(item.attempt)
        clock.advance(60)
    assert attempts == [1, 2, 3]
    assert world.lease_all(x) == []
    row = world.row(queued.id)
    # 不再给；作废是清理任务的事
    assert (row["state"], row["attempts"]) == ("leased", 3)


def test_a_batch_mixes_new_and_released_messages_in_order(world, clock):
    x = world.add("xx")
    now = clock.now_ms()
    world.say(x, "旧话", at_ms=now)
    world.lease_all(x)
    clock.advance(60)
    world.say(x, "新话", at_ms=now + 1)
    items = world.lease_all(x)
    assert [(i.text, i.attempt) for i in items] == [("旧话", 2), ("新话", 1)]


# ---------------------------------------------------------------- 保留期


@pytest.mark.parametrize(
    ("age_ms", "delivered"),
    [(10 * MINUTE - 1, True), (10 * MINUTE, True), (10 * MINUTE + 1, False)],
)
def test_a_message_is_kept_for_ten_minutes_after_receipt(world, clock, age_ms, delivered):
    x = world.add("xx")
    received = clock.now_ms()
    queued = world.say(x, "持仓", received_ms=received)
    clock.advance(age_ms / SECOND)
    items = world.lease_all(x)
    assert bool(items) is delivered
    if not delivered:
        row = world.row(queued.id)
        assert (row["state"], row["text"], row["context_token"]) == ("expired", None, None)


def test_the_retention_comes_from_the_config(make_world, clock):
    world = make_world(inbox={"retention_minutes": 1})
    x = world.add("xx")
    world.say(x, "持仓")
    clock.advance(61)
    assert world.lease_all(x) == []


def test_retention_counts_from_receipt_not_from_the_message_time(world, clock):
    x = world.add("xx")
    now = clock.now_ms()
    # 服务停了一阵，补收到 8 分钟前说的话：保留期从收到起算，所以还能交出去
    world.say(x, "补收", at_ms=now - 8 * MINUTE, received_ms=now)
    clock.advance(9 * 60)
    assert texts(world.lease_all(x)) == ["补收"]


def test_a_leased_message_past_retention_expires_when_its_lease_ends(world, clock):
    x = world.add("xx")
    queued = world.say(x, "持仓", token="ctx-keep")
    clock.advance(9 * 60)
    world.lease_all(x)  # 第 9 分钟取走，没确认
    clock.advance(90)  # 第 10 分半：租期结束，收到已超过 10 分钟
    assert world.lease_all(x) == []
    row = world.row(queued.id)
    # 租出去过的话：正文清掉，对话凭证留到句柄失效
    assert (row["state"], row["text"], row["context_token"]) == ("expired", None, "ctx-keep")


def test_a_message_inside_its_lease_is_not_touched_by_retention(world, clock):
    x = world.add("xx")
    queued = world.say(x, "持仓")
    clock.advance(9.5 * 60)
    world.lease_all(x)
    clock.advance(40)  # 第 10 分 10 秒：已超过保留期，但租期（到 10 分 30 秒）还没结束
    assert world.lease_all(x) == []
    row = world.row(queued.id)
    assert (row["state"], row["text"]) == ("leased", "持仓")
    assert world.inbox.ack(x, [queued.id]).acked == 1


def test_an_exhausted_message_is_left_for_the_cleanup_even_past_retention(make_world, clock):
    world = make_world(inbox={"max_attempts": 1})
    x = world.add("xx")
    queued = world.say(x, "持仓")
    world.lease_all(x)
    clock.advance(11 * 60)
    assert world.lease_all(x) == []
    assert world.row(queued.id)["state"] == "leased"  # 作废并记事件，是清理任务的事


# ---------------------------------------------------------------- 停用


def test_a_disabled_project_gets_nothing_and_its_messages_stay_queued(world):
    x = world.add("xx")
    world.say(x, "持仓")
    world.projects.disable("xx")
    with pytest.raises(ProjectDisabled):
        world.inbox.fetch(x, wait=0, max_items=10)
    with pytest.raises(ProjectDisabled):
        world.inbox.fetch(x, wait=0, max_items=0)
    row = world.rows(x)[0]
    assert (row["state"], row["attempts"], row["text"]) == ("queued", 0, "持仓")


def test_messages_queued_during_a_pause_are_collected_after_enabling(world, clock):
    x = world.add("xx")
    world.say(x, "持仓")
    world.projects.disable("xx")
    clock.advance(5 * 60)
    world.projects.enable("xx")
    assert texts(world.lease_all(x)) == ["持仓"]


def test_messages_older_than_retention_expire_instead_of_being_collected(world, clock):
    x = world.add("xx")
    queued = world.say(x, "持仓")
    world.projects.disable("xx")
    clock.advance(11 * 60)
    world.projects.enable("xx")
    assert world.lease_all(x) == []
    assert world.row(queued.id)["state"] == "expired"


def test_disabling_ends_a_waiting_request_within_five_seconds(world, clock):
    x = world.add("xx")
    worker = run_in_thread(world.inbox.fetch, x, wait=30, max_items=10)
    clock.wait_until_blocked()
    world.projects.disable("xx")  # 另一个进程改的库，叫不醒等待
    assert worker.still_blocked()
    clock.advance(5)
    with pytest.raises(ProjectDisabled):
        worker.result()


def test_a_project_disabled_at_the_last_moment_still_gets_the_refusal(world, clock):
    x = world.add("xx")
    worker = run_in_thread(world.inbox.fetch, x, wait=3, max_items=10)
    clock.wait_until_blocked()
    world.projects.disable("xx")
    clock.advance(3)  # 等待到点，返回之前再核对一次
    with pytest.raises(ProjectDisabled):
        worker.result()


def test_a_disabled_project_is_not_given_a_message_that_arrives_meanwhile(world, clock):
    x = world.add("xx")
    worker = run_in_thread(world.inbox.fetch, x, wait=30, max_items=10)
    clock.wait_until_blocked()
    world.projects.disable("xx")
    world.say(x, "来晚了")  # 唤醒以后先核对启用状态，再租
    with pytest.raises(ProjectDisabled):
        worker.result()
    assert world.states() == ["queued"]


# ---------------------------------------------------------------- 新请求顶掉旧请求、心跳


def test_a_new_request_ends_the_old_one_with_an_empty_list(world, clock):
    x = world.add("xx")
    now = clock.now_ms()
    first = run_in_thread(world.inbox.fetch, x, wait=30, max_items=10)
    clock.wait_until_blocked()
    second = run_in_thread(world.inbox.fetch, x, wait=30, max_items=10)
    assert first.result() == []
    assert clock.now_ms() == now
    assert second.still_blocked()
    world.say(x, "状态")
    assert texts(second.result()) == ["状态"]


def test_a_request_for_another_project_does_not_end_the_old_one(world, clock):
    x, y = world.add("xx"), world.add("yy")
    first = run_in_thread(world.inbox.fetch, x, wait=30, max_items=10)
    clock.wait_until_blocked()
    second = run_in_thread(world.inbox.fetch, y, wait=30, max_items=10)
    clock.wait_until_blocked(2)
    assert first.still_blocked() and second.still_blocked()
    world.say(x, "给 x")
    world.say(y, "给 y")
    assert texts(first.result()) == ["给 x"] and texts(second.result()) == ["给 y"]


def test_a_heartbeat_does_not_end_a_waiting_request(world, clock):
    x = world.add("xx")
    waiting = run_in_thread(world.inbox.fetch, x, wait=30, max_items=10)
    clock.wait_until_blocked()
    assert world.inbox.fetch(x, wait=30, max_items=0) == []
    assert waiting.still_blocked()
    world.say(x, "状态")
    assert texts(waiting.result()) == ["状态"]


def test_a_request_that_does_not_wait_leaves_the_waiting_one_alone(world, clock):
    x = world.add("xx")
    waiting = run_in_thread(world.inbox.fetch, x, wait=30, max_items=10)
    clock.wait_until_blocked()
    assert world.inbox.fetch(x, wait=0, max_items=10) == []
    assert waiting.still_blocked()
    world.say(x, "状态")
    assert texts(waiting.result()) == ["状态"]


# ---------------------------------------------------------------- 参数和库里的空值


@pytest.mark.parametrize("wait, max_items", [(-1, 10), (0, -1), (-1, -1), (30, -5)])
def test_negative_arguments_are_refused(world, wait, max_items):
    x = world.add("xx")
    world.say(x, "状态")
    with pytest.raises(ValueError):
        world.inbox.fetch(x, wait=wait, max_items=max_items)
    assert world.states() == ["queued"]  # 什么也没租出去


def test_a_message_stored_without_text_comes_with_empty_text(world, clock):
    x = world.add("xx")
    now = clock.now_ms()
    with world.db.txn() as conn:
        conn.execute(
            "INSERT INTO inbox (id, project_id, text, has_text, at_ms, received_ms, via, state)"
            " VALUES ('m_notext01', ?, NULL, 0, ?, ?, 'default', 'queued')",
            (x.id, now, now),
        )
    [item] = world.lease_all(x)
    assert (item.text, item.has_text) == ("", False)


def test_fetching_expires_only_the_fetching_projects_old_messages(world, clock):
    x, y = world.add("xx"), world.add("yy")
    old_x = world.say(x, "x 的旧话")
    clock.advance(11 * 60)
    assert world.lease_all(y) == []
    assert world.row(old_x.id)["state"] == "queued"  # y 取话不动 x 的话
    assert world.lease_all(x) == []
    assert world.row(old_x.id)["state"] == "expired"
