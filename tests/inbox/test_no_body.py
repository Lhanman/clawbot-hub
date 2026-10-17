"""收到的话的正文不落盘（任务 C5，规格 `hub-send`「消息正文不落盘」的收话一侧，`hub-inbox`
「确认」「清理」，`hub-deploy`「被清掉的内容不留在空闲页里」）。

每个测试让使用者说一句含独特字符串的话，让它走完自己的一种结局，然后做一次检查点，在数据库文件
（含 `-wal`、`-shm`）、日志、事件、每张表的每一格里都搜不到那个字符串。第一个测试是对照：话还在
队里的时候搜得到，说明这个搜法是有效的。

对话凭证（`ctx-…`）只看待取记录那一列：规格写的是确认了的话的凭证留到回复句柄失效才清，没租出去
过的话在过期、作废时一起清；窗口表里的那一份归 `hub-quota`（窗口过期、被新窗口取代时清）。
"""

from __future__ import annotations

import logging

import pytest

from clawbot_hub.inbox import QueueFull
from tests.poller.conftest import Rig

SECRET = "ZZ-UNIQUE-88"
TOKEN = "ctx-own"
MINUTE_SECONDS = 60


def everything_on_disk(rig: Rig) -> bytes:
    """数据库文件、预写日志、共享内存文件的全部内容（先做一次检查点，把日志并回主文件）。"""
    rig.db.conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return b"".join(path.read_bytes() for path in sorted(rig.hub.paths.home.glob("hub.db*")))


def every_cell(rig: Rig) -> list[str]:
    """所有表的所有格子，转成文字。"""
    conn = rig.db.conn()
    names = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    cells = []
    for name in names:
        for row in conn.execute(f'SELECT * FROM "{name["name"]}"'):  # noqa: S608 - 表名来自库自己
            cells.extend(str(value) for value in tuple(row))
    return cells


def assert_nowhere(rig: Rig, caplog, secret: str = SECRET) -> None:
    assert secret.encode() not in everything_on_disk(rig)
    assert all(secret not in cell for cell in every_cell(rig))
    assert all(secret not in record.getMessage() for record in caplog.records)
    for event in rig.events():
        assert secret not in event["detail"]


@pytest.fixture
def ready(rig: Rig, caplog):
    """绑好账号，默认项目 ashare-quant（前缀"量化"，在线）；日志开到最细。"""
    caplog.set_level(logging.DEBUG)
    rig.bind()
    rig.quant = rig.add("ashare-quant", prefix="量化")
    rig.heartbeat(rig.quant)
    return rig


def arrive(rig: Rig, text: str = f"持仓 {SECRET}", token: str = TOKEN):
    """使用者说一句话，收一轮，返回它在库里的那一行。"""
    rig.say(text, context_token=token)
    rig.step()
    return rig.rows()[-1]


def take(rig: Rig, project=None, *, n: int = 10) -> list:
    return rig.inbox.fetch(project or rig.quant, wait=0, max_items=n)


def only_row(rig: Rig):
    [row] = rig.rows()
    return row


# ---------------------------------------------------------------- 对照


def test_a_message_waiting_in_the_queue_is_on_disk_which_shows_the_search_can_find_it(ready):
    arrive(ready)

    assert SECRET.encode() in everything_on_disk(ready)
    assert any(SECRET in cell for cell in every_cell(ready))


def test_the_search_also_finds_a_message_that_was_taken_but_not_yet_acknowledged(ready):
    arrive(ready)
    [item] = take(ready)

    assert SECRET in item.text
    assert SECRET.encode() in everything_on_disk(ready)


# ---------------------------------------------------------------- 确认了的话


def test_an_acknowledged_message_leaves_no_trace_of_its_text(ready, caplog):
    arrive(ready)
    [item] = take(ready)
    assert SECRET in item.text  # 取话时确实交出去了

    ready.inbox.ack(ready.quant, [item.id])

    row = only_row(ready)
    assert (row["state"], row["text"], row["context_token"]) == ("acked", None, TOKEN)
    assert_nowhere(ready, caplog)


def test_the_conversation_token_of_an_acknowledged_message_stays_until_the_handle_expires(
    ready, clock
):
    arrive(ready)
    [item] = take(ready)
    ready.inbox.ack(ready.quant, [item.id])

    clock.advance(30 * MINUTE_SECONDS - 1)
    ready.inbox.cleanup()
    assert only_row(ready)["context_token"] == TOKEN  # 句柄还有 1 秒

    clock.advance(1)
    ready.inbox.cleanup()
    assert only_row(ready)["context_token"] is None


def test_a_reply_after_the_acknowledgement_still_finds_its_token_and_the_text_stays_gone(
    ready, caplog
):
    arrive(ready)
    [item] = take(ready)
    ready.inbox.ack(ready.quant, [item.id])

    target = ready.inbox.resolve_reply(ready.quant, item.reply_handle)

    assert target.context_token == TOKEN
    assert_nowhere(ready, caplog)


# ---------------------------------------------------------------- 放到过期的话


def test_a_message_nobody_took_expires_without_text_or_token(ready, clock, caplog):
    arrive(ready)
    clock.advance(11 * MINUTE_SECONDS)

    ready.inbox.cleanup()

    row = only_row(ready)
    assert (row["state"], row["text"], row["context_token"]) == ("expired", None, None)
    assert_nowhere(ready, caplog)


def test_a_message_that_expires_when_the_user_asks_the_queue_does_not_leave_its_text(
    ready, clock, caplog
):
    """没等清理任务，取话的时候就顺手标成过期的话，也一样清掉。"""
    arrive(ready)
    clock.advance(11 * MINUTE_SECONDS)

    assert take(ready) == []

    row = only_row(ready)
    assert (row["state"], row["text"], row["context_token"]) == ("expired", None, None)
    assert_nowhere(ready, caplog)


def test_a_message_that_was_taken_and_then_expired_loses_its_text_and_keeps_the_token(
    ready, clock, caplog
):
    arrive(ready)
    [item] = take(ready)
    assert SECRET in item.text
    clock.advance(11 * MINUTE_SECONDS)  # 租期早结束了，也超过了保留期

    ready.inbox.cleanup()

    row = only_row(ready)
    assert (row["state"], row["text"], row["context_token"]) == ("expired", None, TOKEN)
    assert_nowhere(ready, caplog)


def test_the_token_of_a_message_that_was_taken_and_then_expired_goes_with_its_handle(ready, clock):
    arrive(ready)
    take(ready)
    clock.advance(11 * MINUTE_SECONDS)
    ready.inbox.cleanup()
    assert only_row(ready)["context_token"] == TOKEN  # 句柄 30 分钟，还有 19 分钟

    clock.advance(19 * MINUTE_SECONDS)
    ready.inbox.cleanup()

    assert only_row(ready)["context_token"] is None


# ---------------------------------------------------------------- 租出 5 次作废的话


def exhaust(rig: Rig, clock, times: int = 5) -> None:
    """一句话被租出 `times` 次、每次都不确认，最后一次的租期也到了。"""
    for _ in range(times):
        assert len(take(rig)) == 1
        clock.advance(rig.config.inbox.lease_seconds)


def test_a_message_dropped_after_five_leases_leaves_no_trace_of_its_text(ready, clock, caplog):
    arrive(ready)
    exhaust(ready, clock)

    result = ready.inbox.cleanup()

    row = only_row(ready)
    assert result.dropped == 1
    assert (row["state"], row["attempts"], row["text"]) == ("dropped", 5, None)
    assert row["context_token"] == TOKEN  # 租出去过的话，凭证留到句柄失效
    assert len(ready.events("queue")) == 1  # 作废记了一条事件，里面没有正文
    assert_nowhere(ready, caplog)


def test_the_token_of_a_dropped_message_goes_when_its_handle_expires(ready, clock):
    arrive(ready)
    exhaust(ready, clock)
    ready.inbox.cleanup()
    assert only_row(ready)["context_token"] == TOKEN

    clock.advance(30 * MINUTE_SECONDS)
    ready.inbox.cleanup()

    assert only_row(ready)["context_token"] is None


# ---------------------------------------------------------------- 队列满了


def test_the_oldest_message_pushed_out_of_a_full_queue_leaves_no_trace(make_rig, caplog):
    rig = make_rig(inbox={"max_queued": 3})
    caplog.set_level(logging.DEBUG)
    rig.bind()
    quant = rig.add("ashare-quant", prefix="量化")
    rig.heartbeat(quant)
    rig.say(f"最早的 {SECRET}", context_token=TOKEN)
    for n in range(3):
        rig.say(f"后来的 {n}", context_token=f"ctx-later-{n}")

    rig.step()

    first, *rest = rig.rows()
    assert (first["state"], first["text"], first["context_token"]) == ("dropped", None, None)
    assert [row["state"] for row in rest] == ["queued"] * 3
    assert len(rig.events("queue")) == 1
    assert_nowhere(rig, caplog)


def test_a_message_turned_away_by_a_queue_full_of_leased_messages_is_never_stored(make_rig, caplog):
    rig = make_rig(inbox={"max_queued": 2})
    caplog.set_level(logging.DEBUG)
    rig.bind()
    quant = rig.add("ashare-quant", prefix="量化")
    rig.heartbeat(quant)
    rig.say("一", context_token="ctx-1")
    rig.say("二", context_token="ctx-2")
    rig.step()
    assert len(take(rig, quant)) == 2  # 都租出去了，没有可丢的

    rig.say(f"被挡回去的 {SECRET}", context_token=TOKEN)
    rig.step()

    assert [row["text"] for row in rig.rows()] == ["一", "二"]
    assert len(rig.events("queue")) == 1
    sent = rig.flush()
    assert len(sent) == 1 and SECRET not in sent[0]  # 使用者收到的是"积压太多"，不含他那句话
    assert_nowhere(rig, caplog)


def test_enqueue_itself_stores_nothing_of_a_message_it_turns_away(world):
    """不经过 poller，直接看 `enqueue`：队列满了挡回去的话返回 `QueueFull`，库里没有这句话。"""
    project = world.add("xx")
    for n in range(world.config.inbox.max_queued):
        world.say(project, f"话 {n}")
    world.lease_all(project)

    result = world.say(project, f"新的 {SECRET}")

    assert isinstance(result, QueueFull)
    assert all(SECRET not in str(tuple(row)) for row in world.rows())


# ---------------------------------------------------------------- 移除项目


def test_removing_a_project_voids_its_waiting_messages_without_leaving_text_or_token(ready, caplog):
    beta = ready.add("beta", prefix="贝塔")
    ready.heartbeat(beta)
    arrive(ready, f"贝塔 {SECRET}")
    [leased_for_beta] = take(ready, beta)
    assert SECRET in leased_for_beta.text
    arrive(ready, f"贝塔 又一句 {SECRET}", token="ctx-second")  # 没租出去的
    ready.hub.projects.disable("beta")

    ready.hub.projects.remove("beta", yes=True)

    states = [(row["state"], row["text"], row["context_token"]) for row in ready.rows()]
    assert states == [("dropped", None, None), ("dropped", None, None)]
    assert_nowhere(ready, caplog)


# ---------------------------------------------------------------- 服务自己处理的话、没地方可去的话


def test_the_service_commands_leave_no_trace_of_their_text(ready, caplog):
    for text in ("服务 项目", "服务 状态", f"服务 {SECRET}", f"服务 状态 {SECRET}"):
        ready.say(text)
    ready.step()
    sent = ready.flush()

    assert len(sent) == 4 and all(SECRET not in text for text in sent)
    assert ready.rows() == []
    assert_nowhere(ready, caplog)


def test_a_message_with_nowhere_to_go_leaves_no_trace_of_its_text(rig, caplog):
    """没有设默认项目，又没带前缀的话。"""
    caplog.set_level(logging.DEBUG)
    rig.bind()
    rig.say(f"持仓 {SECRET}")
    rig.step()
    sent = rig.flush()

    assert len(sent) == 1 and SECRET not in sent[0]
    assert rig.rows() == []
    assert_nowhere(rig, caplog)


def test_a_message_for_a_disabled_project_leaves_no_trace_of_its_text(ready, caplog):
    ready.hub.projects.disable("ashare-quant")
    ready.say(f"持仓 {SECRET}")
    ready.step()
    sent = ready.flush()

    assert len(sent) == 1 and SECRET not in sent[0]
    assert ready.rows() == []
    assert_nowhere(ready, caplog)


# ---------------------------------------------------------------- 没收的话


def test_a_strangers_message_is_counted_and_not_kept(ready, caplog):
    ready.say(f"陌生人说的 {SECRET}", from_user_id="someone-else")
    ready.step()

    assert ready.rows() == []
    assert ready.account_state()["ignored_count"] == 1
    assert_nowhere(ready, caplog)


def test_a_message_the_bot_itself_sent_is_dropped_without_a_trace(ready, caplog):
    ready.upstream.push_bot_message(f"机器人自己发的 {SECRET}")
    ready.step()

    assert ready.rows() == []
    assert_nowhere(ready, caplog)


def test_a_message_marked_as_the_bots_even_under_the_users_id_is_dropped_without_a_trace(
    ready, caplog
):
    ready.say(f"标明是机器人发的 {SECRET}", message_type=2)
    ready.step()

    assert ready.rows() == []
    assert_nowhere(ready, caplog)


def test_a_message_without_a_conversation_token_is_not_kept(ready, caplog):
    ready.say(f"没有凭证 {SECRET}", context_token=None)
    ready.step()

    assert ready.rows() == []
    assert_nowhere(ready, caplog)
