"""回复句柄（任务 C2，规格 `hub-inbox`「回复句柄」、`hub-send`「回复」里的次数上限）。

句柄只对被路由给这个项目的那句话有效；别人的、不存在的都是同一个 `UnknownHandle`；有效期是
`min(收到后 30 分钟, 话自己的时间之后一个窗口)`；租出去过的话（不论确认、过期还是作废）在有效期内
仍可回复；重新登录以后一律失效。回复用的对话凭证从来不出现在句柄里。
"""

from __future__ import annotations

import pytest

from clawbot_hub.inbox import (
    HANDLE_CLEARED,
    OLD_MESSAGE,
    HandleExpired,
    ReplyLimit,
    ReplyTarget,
    UnknownHandle,
)
from clawbot_hub.loginstate import reset_conversation

MINUTE = 60_000
HOUR = 60 * MINUTE


def lease(world, project, text="持仓", **kwargs):
    """说一句话、取走，返回 (取话结果, 数据库里的行)。"""
    queued = world.say(project, text, **kwargs)
    [item] = world.lease_all(project)
    assert item.id == queued.id
    return item, world.row(item.id)


def test_a_valid_handle_resolves_to_the_message_and_its_own_token(world, clock):
    x = world.add("xx")
    now = clock.now_ms()
    item, _ = lease(world, x, at_ms=now - 3_000, token="ctx-own")
    target = world.inbox.resolve_reply(x, item.reply_handle)
    assert target == ReplyTarget(
        item_id=item.id, context_token="ctx-own", at_ms=now - 3_000, replies=0
    )


def test_each_message_keeps_its_own_token(world):
    x = world.add("xx")
    a = world.say(x, "A", token="ctx-A")
    b = world.say(x, "B", token="ctx-B")
    items = {item.id: item for item in world.lease_all(x)}
    assert world.inbox.resolve_reply(x, items[a.id].reply_handle).context_token == "ctx-A"
    assert world.inbox.resolve_reply(x, items[b.id].reply_handle).context_token == "ctx-B"


def test_the_same_handle_works_after_the_message_is_acked(world, clock):
    x = world.add("xx")
    item, _ = lease(world, x, token="ctx-own")
    world.inbox.ack(x, [item.id])
    clock.advance(5 * 60)
    target = world.inbox.resolve_reply(x, item.reply_handle)
    assert target.context_token == "ctx-own"
    assert world.row(item.id)["text"] is None


def test_another_projects_handle_and_a_made_up_one_look_the_same(world):
    x, y = world.add("xx"), world.add("yy")
    item, _ = lease(world, x)
    errors = []
    for handle in (item.reply_handle, "rh_" + "A" * 22, "", "垃圾"):
        with pytest.raises(UnknownHandle) as caught:
            world.inbox.resolve_reply(y, handle)
        errors.append((type(caught.value), caught.value.args))
    assert len(set(errors)) == 1


def test_a_made_up_handle_is_unknown_for_everyone(world):
    x = world.add("xx")
    with pytest.raises(UnknownHandle):
        world.inbox.resolve_reply(x, "rh_" + "A" * 22)


def test_a_removed_project_loses_its_handles(world):
    x = world.add("xx")
    item, _ = lease(world, x)
    world.projects.disable("xx")
    world.projects.remove("xx", yes=True)
    with pytest.raises(UnknownHandle):
        world.inbox.resolve_reply(x, item.reply_handle)


# ---------------------------------------------------------------- 有效期


def test_the_handle_works_until_thirty_minutes_after_receipt(world, clock):
    x = world.add("xx")
    item, _ = lease(world, x)
    clock.advance(30 * 60 - 0.001)
    assert world.inbox.resolve_reply(x, item.reply_handle).item_id == item.id
    clock.advance(0.001)
    with pytest.raises(HandleExpired) as caught:
        world.inbox.resolve_reply(x, item.reply_handle)
    assert "30 分钟" in str(caught.value)
    assert OLD_MESSAGE not in str(caught.value)


def test_an_expired_lookup_clears_the_conversation_token(world, clock):
    x = world.add("xx")
    item, _ = lease(world, x, token="ctx-own")
    clock.advance(31 * 60)
    with pytest.raises(HandleExpired):
        world.inbox.resolve_reply(x, item.reply_handle)
    assert world.row(item.id)["context_token"] is None


def test_the_handle_lifetime_comes_from_the_config(make_world, clock):
    world = make_world(reply={"handle_minutes": 2})
    x = world.add("xx")
    item, _ = lease(world, x)
    clock.advance(119.999)
    world.inbox.resolve_reply(x, item.reply_handle)
    clock.advance(0.001)
    with pytest.raises(HandleExpired) as caught:
        world.inbox.resolve_reply(x, item.reply_handle)
    assert "2 分钟" in str(caught.value)


def test_a_message_older_than_a_window_cannot_be_answered(world, clock):
    x = world.add("xx")
    now = clock.now_ms()
    # 服务停了一天半，补收到 30 小时前的话：照样交给项目，但回复不了
    item, _ = lease(world, x, at_ms=now - 30 * HOUR)
    with pytest.raises(HandleExpired) as caught:
        world.inbox.resolve_reply(x, item.reply_handle)
    assert str(caught.value) == OLD_MESSAGE == "这句话太旧，微信已不能回复"


def test_the_window_after_the_message_can_end_before_the_thirty_minutes(world, clock):
    x = world.add("xx")
    now = clock.now_ms()
    item, _ = lease(world, x, at_ms=now - 23 * HOUR - 50 * MINUTE)  # 窗口只剩 10 分钟
    clock.advance(9 * 60 + 59.999)
    world.inbox.resolve_reply(x, item.reply_handle)
    clock.advance(0.001)
    with pytest.raises(HandleExpired) as caught:
        world.inbox.resolve_reply(x, item.reply_handle)
    assert str(caught.value) == OLD_MESSAGE


def test_window_hours_come_from_the_config(make_world, clock):
    world = make_world(quota={"window_hours": 2})
    x = world.add("xx")
    item, _ = lease(world, x, at_ms=clock.now_ms() - 2 * HOUR)
    with pytest.raises(HandleExpired) as caught:
        world.inbox.resolve_reply(x, item.reply_handle)
    assert str(caught.value) == OLD_MESSAGE


# ---------------------------------------------------------------- 租出去过 / 没租出去过


def test_a_message_leased_before_it_expired_can_still_be_answered(world, clock):
    x = world.add("xx")
    queued = world.say(x, "持仓", token="ctx-own")
    clock.advance(9 * 60)
    [item] = world.lease_all(x)  # 第 9 分钟取走，没确认
    clock.advance(3 * 60)  # 第 12 分钟：租期结束、收到已超过保留期
    assert world.lease_all(x) == []
    world.inbox.cleanup()
    assert world.row(queued.id)["state"] == "expired"
    target = world.inbox.resolve_reply(x, item.reply_handle)
    assert target.context_token == "ctx-own"
    clock.advance(19 * 60)  # 收到后 31 分钟：句柄失效
    with pytest.raises(HandleExpired):
        world.inbox.resolve_reply(x, item.reply_handle)
    world.inbox.cleanup()
    assert world.row(queued.id)["context_token"] is None


def test_a_message_dropped_after_too_many_leases_can_still_be_answered(make_world, clock):
    world = make_world(inbox={"max_attempts": 1})
    x = world.add("xx")
    queued = world.say(x, "持仓", token="ctx-own")
    [item] = world.lease_all(x)
    clock.advance(61)
    world.inbox.cleanup()
    assert world.row(queued.id)["state"] == "dropped"
    assert world.inbox.resolve_reply(x, item.reply_handle).context_token == "ctx-own"


def test_a_message_that_was_never_handed_out_cannot_be_answered(world, clock):
    x = world.add("xx")
    queued = world.say(x, "持仓", token="ctx-own")
    handle = world.row(queued.id)["handle"]
    clock.advance(11 * 60)
    world.inbox.cleanup()  # 没人取，过期了：凭证随之清掉
    assert world.row(queued.id)["context_token"] is None
    with pytest.raises(HandleExpired) as caught:
        world.inbox.resolve_reply(x, handle)
    assert str(caught.value) == HANDLE_CLEARED


def test_a_dropped_message_that_was_never_handed_out_cannot_be_answered(make_world):
    world = make_world(inbox={"max_queued": 1})
    x = world.add("xx")
    oldest = world.say(x, "最旧的", token="ctx-own")
    handle = world.row(oldest.id)["handle"]
    world.say(x, "新的")
    assert world.row(oldest.id)["state"] == "dropped"
    with pytest.raises(HandleExpired):
        world.inbox.resolve_reply(x, handle)


def test_after_a_new_login_every_old_handle_is_expired(world):
    x = world.add("xx")
    item, _ = lease(world, x)
    with world.db.txn() as conn:
        reset_conversation(conn)
    with pytest.raises(HandleExpired) as caught:
        world.inbox.resolve_reply(x, item.reply_handle)
    assert str(caught.value) == HANDLE_CLEARED
    # 没有凭证了，句柄也就没有用了；重复换登录不影响
    with world.db.txn() as conn:
        reset_conversation(conn)
    with pytest.raises(HandleExpired):
        world.inbox.resolve_reply(x, item.reply_handle)


# ---------------------------------------------------------------- 回复次数


def test_replies_are_counted_per_handle(world):
    x = world.add("xx")
    a, _ = lease(world, x, text="A")
    b, _ = lease(world, x, text="B")
    world.inbox.count_reply(a.id)
    world.inbox.count_reply(a.id)
    world.inbox.count_reply(b.id)
    assert world.inbox.resolve_reply(x, a.reply_handle).replies == 2
    assert world.inbox.resolve_reply(x, b.reply_handle).replies == 1


def test_a_handle_stops_after_max_per_handle_replies(world):
    x = world.add("xx")
    item, _ = lease(world, x)
    for _ in range(5):
        world.inbox.resolve_reply(x, item.reply_handle)
        world.inbox.count_reply(item.id)
    with pytest.raises(ReplyLimit):
        world.inbox.resolve_reply(x, item.reply_handle)


def test_the_reply_limit_comes_from_the_config(make_world):
    world = make_world(reply={"max_per_handle": 2})
    x = world.add("xx")
    item, _ = lease(world, x)
    world.inbox.count_reply(item.id)
    world.inbox.resolve_reply(x, item.reply_handle)
    world.inbox.count_reply(item.id)
    with pytest.raises(ReplyLimit):
        world.inbox.resolve_reply(x, item.reply_handle)


def test_the_reply_limit_message_says_how_many_were_used_and_the_most_allowed(make_world):
    world = make_world(reply={"max_per_handle": 3})
    x = world.add("xx")
    item, _ = lease(world, x)
    with world.db.txn() as conn:
        conn.execute("UPDATE inbox SET replies = 5 WHERE id = ?", (item.id,))
    with pytest.raises(ReplyLimit) as caught:
        world.inbox.resolve_reply(x, item.reply_handle)
    assert str(caught.value) == "这句话已经回复了 5 次，最多 3 次"


def test_the_reply_limit_says_how_many_seconds_the_handle_has_left(world, clock):
    x = world.add("xx")
    item, _ = lease(world, x)
    with world.db.txn() as conn:
        conn.execute("UPDATE inbox SET replies = 5 WHERE id = ?", (item.id,))

    def retry_after() -> int:
        with pytest.raises(ReplyLimit) as caught:
            world.inbox.resolve_reply(x, item.reply_handle)
        return caught.value.retry_after

    assert retry_after() == 1800  # 句柄有效 30 分钟
    clock.advance(100)
    assert retry_after() == 1700
    clock.advance(1698.9)  # 还剩 1.1 秒：进一位
    assert retry_after() == 2
    clock.advance(0.1)  # 还剩整整 1 秒
    assert retry_after() == 1
    clock.advance(0.999)  # 还剩 1 毫秒：仍然是 1，不是 0
    assert retry_after() == 1


def test_resolving_alone_does_not_use_up_a_reply(world):
    x = world.add("xx")
    item, _ = lease(world, x)
    for _ in range(10):
        world.inbox.resolve_reply(x, item.reply_handle)
    assert world.row(item.id)["replies"] == 0


def test_an_expired_handle_is_reported_before_the_reply_limit(world, clock):
    x = world.add("xx")
    item, _ = lease(world, x)
    for _ in range(5):
        world.inbox.count_reply(item.id)
    clock.advance(31 * 60)
    with pytest.raises(HandleExpired):
        world.inbox.resolve_reply(x, item.reply_handle)
