"""收消息循环（任务 C3，规格 `hub-inbox`「收消息」「只认使用者本人」「项目没在线」，
`hub-login`「登录失效」「登录后服务自动换上新凭证」）。

`Poller.step()` 收一轮并返回"下一轮前等几秒"（0 = 马上再收），所以退避、探测间隔这些时间上的
规矩不用起线程就能测；起线程的几个测试（`start()` / `stop()`）用假时钟的 `pending_deadlines()`
确定地检查它睡到了哪一刻。服务自己的回复内容和限频归 `test_replier.py`。
"""

from __future__ import annotations

import inspect
import logging
import os
import sqlite3
import threading

import pytest

from clawbot_hub.account import AccountFile
from clawbot_hub.config import _INT_RANGES
from clawbot_hub.loginstate import write_scan_marker
from clawbot_hub.poller import (
    HINT_MARGIN_SECONDS,
    IDLE_SECONDS,
    JOIN_SECONDS,
    Poller,
    upstream_key,
)
from clawbot_hub.upstream.base import IncomingMessage, PollFailed, SessionExpired, Updates
from tests.poller.conftest import texts
from tests.support.fake_upstream import Gate
from tests.support.hub import BOT_TOKEN, USER
from tests.support.threads import run_in_thread, wait_for


class Killed(BaseException):
    """模拟服务在两步之间被 `kill -9`：不是 `Exception`，任何 `except Exception` 都拦不住它。"""


def message(
    text: str = "持仓",
    *,
    token: str | None = "ctx-m",
    at_ms: int | None = None,
    message_id: str | None = "mid-1",
    seq: int | None = None,
    user: str = USER,
    kind: int | None = 1,
    has_text: bool = True,
) -> IncomingMessage:
    return IncomingMessage(
        from_user_id=user,
        message_type=kind,
        context_token=token,
        at_ms=at_ms,
        text=text,
        has_text=has_text,
        message_id=message_id,
        seq=seq,
    )


@pytest.fixture
def ready(rig):
    """绑好账号、登记默认项目 ashare-quant（前缀"量化"，在线）。"""
    rig.bind()
    rig.quant = rig.add("ashare-quant", prefix="量化")
    rig.heartbeat(rig.quant)  # 默认项目在线：没有"没在线"的提醒来干扰
    return rig


# ---------------------------------------------------------------- 收一句话


def test_a_message_becomes_a_queued_item_and_opens_a_window(ready, clock):
    sent = ready.say("持仓", context_token="ctx-1")
    assert ready.step() == 0
    [row] = ready.rows()
    assert (row["project_id"], row["text"], row["has_text"]) == (ready.quant.id, "持仓", 1)
    assert (row["via"], row["state"], row["context_token"]) == ("default", "queued", "ctx-1")
    assert row["upstream_key"] == f"id:{sent.message_id}"
    assert (row["at_ms"], row["received_ms"]) == (sent.at_ms, clock.now_ms())
    window = ready.window()
    assert (window.started_ms, window.context_token) == (sent.at_ms, "ctx-1")
    assert row["window_id"] == window.id
    assert ready.hub.store.read().updates_buf == "buf-1"


def test_the_received_time_is_when_the_service_got_it_not_when_it_was_said(ready, clock):
    long_ago = clock.now_ms() - 3 * 3_600_000
    ready.say("补收的话", at_ms=long_ago)
    ready.step()
    [row] = ready.rows()
    assert (row["at_ms"], row["received_ms"]) == (long_ago, clock.now_ms())


def test_a_message_without_text_goes_to_the_default_project_with_empty_text(ready):
    ready.say("", has_text=False)
    ready.step()
    [row] = ready.rows()
    assert (row["project_id"], row["text"], row["has_text"]) == (ready.quant.id, "", 0)


def test_a_poll_without_messages_still_saves_the_new_progress(ready):
    ready.upstream.script("get_updates", Updates(buf="b5"))
    assert ready.step() == 0
    assert ready.rows() == []
    assert ready.hub.store.read().updates_buf == "b5"


def test_a_poll_without_a_new_progress_keeps_the_old_one(ready):
    ready.upstream.script("get_updates", Updates(buf="b5"), Updates(buf=None))
    ready.step()
    ready.step()
    assert ready.hub.store.read().updates_buf == "b5"
    ready.step()
    assert ready.upstream.calls_to("get_updates")[-1].args["buf"] == "b5"


def test_every_poll_asks_with_the_progress_of_the_last_one(ready):
    ready.upstream.script("get_updates", Updates(buf="b1"), Updates(buf="b2"), Updates(buf="b3"))
    for _ in range(3):
        ready.step()
    assert [c.args["buf"] for c in ready.upstream.calls_to("get_updates")] == ["", "b1", "b2"]


def test_the_progress_is_written_back_only_after_the_batch_is_committed(ready, monkeypatch):
    ready.say("持仓")
    counted = []
    original = ready.hub.store.save_progress

    def spy(bot_token, updates_buf):
        other = sqlite3.connect(ready.hub.paths.db)  # 另一个连接只看得到已提交的
        counted.append(other.execute("SELECT COUNT(*) FROM inbox").fetchone()[0])
        other.close()
        return original(bot_token, updates_buf)

    monkeypatch.setattr(ready.hub.store, "save_progress", spy)
    ready.step()
    assert counted == [1]


def test_the_progress_is_written_back_for_the_token_this_round_used(ready, monkeypatch):
    calls = []
    original = ready.hub.store.save_progress
    monkeypatch.setattr(
        ready.hub.store,
        "save_progress",
        lambda bot_token, updates_buf: (
            calls.append((bot_token, updates_buf)) or original(bot_token, updates_buf)
        ),
    )
    ready.say("持仓")
    ready.step()
    assert calls == [(BOT_TOKEN, "buf-1")]


def test_a_progress_that_cannot_be_written_back_is_kept_in_memory(ready, caplog):
    def failing(bot_token, updates_buf):
        raise OSError("磁盘满ZZ")

    ready.say("持仓")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ready.hub.store, "save_progress", failing)
        with caplog.at_level(logging.ERROR, logger="clawbot_hub.poller"):
            assert ready.step() == 0  # 这一批已经入库，照常往下走
        ready.step()
    assert texts_of(ready) == ["持仓"]
    assert ready.upstream.calls_to("get_updates")[-1].args["buf"] == "buf-1"  # 这个进程里不重收
    assert "OSError" in caplog.text and "磁盘满ZZ" not in caplog.text


# ---------------------------------------------------------------- 去重、两步之间被杀


def test_a_message_delivered_twice_in_one_batch_is_stored_once(ready):
    m = ready.say("持仓")
    ready.upstream.script("get_updates", Updates(messages=(m, m), buf="b1"))
    ready.step()
    assert len(ready.rows()) == 1 and len(ready.windows()) == 1


def test_a_message_delivered_again_in_a_later_poll_is_stored_once(ready):
    m = ready.say("持仓")
    ready.upstream.script("get_updates", Updates(messages=(m,), buf="b1"), Updates(messages=(m,)))
    ready.step()
    ready.step()
    assert len(ready.rows()) == 1 and len(ready.windows()) == 1


def test_a_kill_between_committing_and_writing_back_does_not_store_twice(ready):
    ready.say("持仓")

    def killed(bot_token, updates_buf):
        raise Killed

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ready.hub.store, "save_progress", killed)
        with pytest.raises(Killed):
            ready.step()
    assert len(ready.rows()) == 1
    assert ready.hub.store.read().updates_buf == ""  # 进度没写回，重启后腾讯会再给一遍

    ready.new_poller()
    ready.step()
    assert len(ready.rows()) == 1 and len(ready.windows()) == 1
    assert ready.hub.store.read().updates_buf == "buf-1"


def test_every_stored_message_leaves_a_seen_record_with_the_time_it_was_received(ready, clock):
    sent = ready.say("持仓")
    ready.step()
    [seen] = ready.db.conn().execute("SELECT * FROM inbound_seen").fetchall()
    assert (seen["upstream_key"], seen["seen_ms"]) == (f"id:{sent.message_id}", clock.now_ms())


def test_without_a_message_id_the_sequence_number_is_the_key(ready):
    first = message("甲", message_id=None, seq=7, at_ms=1_000)
    again = message("甲", message_id=None, seq=7, at_ms=1_000)
    other = message("乙", message_id=None, seq=8, at_ms=2_000)
    ready.upstream.script("get_updates", Updates(messages=(first, again, other), buf="b1"))
    ready.step()
    assert texts_of(ready) == ["甲", "乙"]


def test_without_an_id_or_sequence_the_time_token_and_text_are_the_key(ready):
    one = message("甲", message_id=None, seq=None, at_ms=1_000, token="ctx-a")
    same = message("甲", message_id=None, seq=None, at_ms=1_000, token="ctx-a")
    later = message("甲", message_id=None, seq=None, at_ms=2_000, token="ctx-a")
    other_token = message("甲", message_id=None, seq=None, at_ms=1_000, token="ctx-b")
    other_text = message("乙", message_id=None, seq=None, at_ms=1_000, token="ctx-a")
    batch = (one, same, later, other_token, other_text)
    ready.upstream.script("get_updates", Updates(messages=batch, buf="b1"))
    ready.step()
    # 只有 `same` 是重复的；其余按各自的时间排：one、other_token、other_text（都是 1000），later
    assert texts_of(ready) == ["甲", "甲", "乙", "甲"]


def test_an_empty_message_id_counts_as_no_id(ready):
    first = message("甲", message_id="", seq=7, at_ms=1_000)
    second = message("乙", message_id="", seq=7, at_ms=2_000)
    third = message("丙", message_id="", seq=8, at_ms=3_000)  # 序号不同：不是重复
    ready.upstream.script("get_updates", Updates(messages=(first, second, third), buf="b1"))
    ready.step()
    assert texts_of(ready) == ["甲", "丙"]


def test_the_keys_in_the_seen_table_have_their_fixed_formats(ready):
    # 去重键存在数据库里，服务升级以后还要认得：格式一变，重启后腾讯重发的同一批就会收两次
    by_id = message("一", message_id="m-1", seq=1, at_ms=1_000)
    by_seq = message("二", message_id=None, seq=7, at_ms=2_000)
    by_hash = message("三", message_id=None, seq=None, at_ms=3_000, token="ctx-h")
    ready.upstream.script("get_updates", Updates(messages=(by_id, by_seq, by_hash), buf="b1"))
    ready.step()
    keys = [row["upstream_key"] for row in ready.db.conn().execute("SELECT * FROM inbound_seen")]
    assert sorted(keys) == sorted(["id:m-1", "seq:7", upstream_key(by_hash)])
    digest = upstream_key(by_hash)
    assert digest.startswith("h:") and len(digest) == 2 + 64
    assert digest[2:] == digest[2:].lower() and int(digest[2:], 16) >= 0


def test_the_hash_key_changes_with_each_of_time_token_and_text():
    base = message("甲", message_id=None, seq=None, at_ms=1_000, token="ctx-a")
    keys = {
        upstream_key(base),
        upstream_key(message("乙", message_id=None, seq=None, at_ms=1_000, token="ctx-a")),
        upstream_key(message("甲", message_id=None, seq=None, at_ms=2_000, token="ctx-a")),
        upstream_key(message("甲", message_id=None, seq=None, at_ms=None, token="ctx-a")),
        upstream_key(message("甲", message_id=None, seq=None, at_ms=1_000, token="ctx-b")),
    }
    assert len(keys) == 5
    assert upstream_key(base) == upstream_key(
        message("甲", message_id=None, seq=None, at_ms=1_000, token="ctx-a", kind=2)
    )  # 类型、发信人不算在里面


def test_a_sequence_number_of_zero_is_still_a_sequence_number(ready):
    first = message("甲", message_id=None, seq=0, at_ms=1_000)
    second = message("乙", message_id=None, seq=0, at_ms=2_000)
    ready.upstream.script("get_updates", Updates(messages=(first, second), buf="b1"))
    ready.step()
    assert texts_of(ready) == ["甲"]


def test_an_id_and_a_sequence_number_with_the_same_digits_are_different_keys(ready):
    by_id = message("按编号", message_id="7", seq=None, at_ms=1_000)
    by_seq = message("按序号", message_id=None, seq=7, at_ms=2_000)
    ready.upstream.script("get_updates", Updates(messages=(by_id, by_seq), buf="b1"))
    ready.step()
    assert texts_of(ready) == ["按编号", "按序号"]


def texts_of(rig) -> list[str]:
    return [row["text"] for row in rig.rows()]


# ---------------------------------------------------------------- 一批消息的先后


def test_a_batch_is_stored_in_the_order_of_the_messages_own_times(ready, clock):
    now = clock.now_ms()
    ready.say("C", at_ms=now - 1_000)  # 在批里最先，但时间最晚
    ready.say("A", at_ms=now - 3_000)
    ready.say("B", at_ms=now - 2_000)
    ready.step()
    assert texts_of(ready) == ["A", "B", "C"]
    assert texts(ready.inbox.fetch(ready.quant, wait=0, max_items=10)) == ["A", "B", "C"]


def test_messages_with_the_same_time_keep_their_order_in_the_batch(ready, clock):
    now = clock.now_ms()
    for text in ["丙", "甲", "乙"]:
        ready.say(text, at_ms=now - 1_000)
    ready.step()
    assert texts_of(ready) == ["丙", "甲", "乙"]


def test_a_message_without_a_time_counts_as_received_now(ready, clock):
    now = clock.now_ms()
    ready.upstream.script(
        "get_updates",
        Updates(
            messages=(message("没时间", at_ms=None, message_id="a"),
                      message("五秒前", at_ms=now - 5_000, message_id="b")),
            buf="b1",
        ),
    )  # fmt: skip
    ready.step()
    assert texts_of(ready) == ["五秒前", "没时间"]
    assert ready.rows()[1]["at_ms"] == now


def test_a_message_older_than_the_current_window_does_not_rewind_it(ready, clock):
    now = clock.now_ms()
    ready.say("新的", at_ms=now)
    ready.step()
    window = ready.window()
    ready.say("补收的旧话", at_ms=now - 3_600_000)
    ready.step()
    assert ready.window() == window  # 窗口没有倒回去，也没有多开
    assert len(ready.windows()) == 1
    old = ready.rows()[1]
    assert (old["text"], old["window_id"]) == ("补收的旧话", None)


def test_each_new_message_opens_a_newer_window_with_its_own_token(ready, clock):
    now = clock.now_ms()
    ready.say("一", at_ms=now - 2_000, context_token="ctx-1")
    ready.say("二", at_ms=now - 1_000, context_token="ctx-2")
    ready.step()
    window = ready.window()
    assert (window.started_ms, window.context_token) == (now - 1_000, "ctx-2")
    assert [w["context_token"] for w in ready.windows()] == [None, "ctx-2"]  # 被取代的不留凭证


# ---------------------------------------------------------------- 没有对话凭证、不是本人


def test_a_message_without_a_conversation_token_is_not_stored(ready, caplog):
    ready.say("机密正文ZZ", context_token=None)
    with caplog.at_level(logging.INFO, logger="clawbot_hub.poller"):
        ready.step()
    assert ready.rows() == [] and ready.windows() == []
    assert ready.replier.pending == 0
    assert "1 条消息没有对话凭证" in caplog.text and "机密正文ZZ" not in caplog.text
    assert ready.account_state()["ignored_count"] == 0  # 这不是"陌生人"


def test_a_message_with_an_empty_conversation_token_is_not_stored(ready):
    ready.say("持仓", context_token="")
    ready.step()
    assert ready.rows() == [] and ready.windows() == []


def test_messages_from_other_people_are_dropped_and_counted(ready, caplog):
    ready.say("陌生人的话ZZ", from_user_id="stranger@im.wechat")
    ready.say("我的话")
    ready.say("另一个陌生人", from_user_id="stranger-2@im.wechat")
    with caplog.at_level(logging.INFO, logger="clawbot_hub.poller"):
        ready.step()
    assert texts_of(ready) == ["我的话"]
    assert len(ready.windows()) == 1
    assert ready.account_state()["ignored_count"] == 2
    assert ready.replier.pending == 0
    assert "2 条" in caplog.text and "陌生人的话ZZ" not in caplog.text


def test_the_bots_own_messages_are_dropped_and_counted(ready):
    ready.upstream.push_bot_message("机器人自己说的")
    ready.say("我的话")
    ready.step()
    assert texts_of(ready) == ["我的话"]
    assert ready.account_state()["ignored_count"] == 1
    assert ready.replier.pending == 0


def test_a_message_from_the_owner_that_the_bot_itself_sent_is_dropped_and_counted(ready):
    ready.say("机器人说的", message_type=2)  # from_user_id 是使用者本人，但类型是机器人
    ready.say("我的话")
    ready.step()
    assert texts_of(ready) == ["我的话"]
    assert len(ready.windows()) == 1  # 只有"我的话"开了窗口
    assert ready.account_state()["ignored_count"] == 1
    assert ready.replier.pending == 0


@pytest.mark.parametrize("kind", [0, 1, 3, 10])
def test_only_type_2_is_the_bot_other_types_are_taken_as_normal(ready, kind):
    ready.say("持仓", message_type=kind)
    ready.step()
    assert texts_of(ready) == ["持仓"]
    assert ready.account_state()["ignored_count"] == 0


def test_a_strangers_message_that_is_also_from_a_bot_is_counted_once(ready):
    ready.say("x", from_user_id="stranger@im.wechat", message_type=2)
    ready.step()
    assert ready.account_state()["ignored_count"] == 1


def test_the_ignored_count_adds_up_over_several_polls(ready):
    for _ in range(3):
        ready.say("x", from_user_id="stranger@im.wechat")
        ready.step()
    assert ready.account_state()["ignored_count"] == 3


def test_the_owner_is_the_user_id_in_the_credentials_file(ready):
    ready.hub.bind(user_id="someone-else@im.wechat")
    ready.upstream.script("get_updates", Updates(messages=(message("她的话"),), buf="b1"))
    ready.step()
    assert ready.rows() == []
    assert ready.account_state()["ignored_count"] == 1


def test_a_message_without_a_type_counts_as_a_normal_one(ready):
    ready.upstream.script("get_updates", Updates(messages=(message("持仓", kind=None),), buf="b1"))
    ready.step()
    assert texts_of(ready) == ["持仓"]


# ---------------------------------------------------------------- 腾讯报错、退避


def sleeping_until(clock, ms: int) -> None:
    """等收消息线程睡到假时间的 `ms` 那一刻（真实时间里最多等几秒）。

    不用 `wait_until_blocked()`：`advance()` 以后，刚被叫醒的线程在真正跑起来之前还算"睡着"。"""
    wait_for(lambda: clock.pending_deadlines() == [ms])


def failing(*reasons: tuple[str, bool]):
    return [PollFailed(reason, completed) for reason, completed in reasons]


def test_errors_back_off_5_10_20_and_start_over_after_a_good_poll(ready):
    ready.upstream.script(
        "get_updates",
        *failing(("ret 500", True), ("HTTP 502", False)),
        PollFailed("HTTP 502", False),
        Updates(buf="b1"),
        PollFailed("连不上", False),
    )
    assert [ready.step() for _ in range(5)] == [5, 10, 20, 0, 5]


def test_the_wait_stops_growing_at_the_maximum(ready):
    ready.upstream.script("get_updates", *failing(*[("HTTP 502", False)] * 8))
    assert [ready.step() for _ in range(8)] == [5, 10, 20, 40, 60, 60, 60, 60]


def test_the_backoff_comes_from_the_config(make_rig):
    rig = make_rig(upstream={"backoff_start": 2, "backoff_max": 9})
    rig.bind()
    rig.upstream.script("get_updates", *failing(*[("HTTP 502", False)] * 5))
    assert [rig.step() for _ in range(5)] == [2, 4, 8, 9, 9]


def test_http_429_and_403_are_errors_too_and_nothing_reaches_a_project(ready):
    ready.say("持仓")
    ready.upstream.script(
        "get_updates", PollFailed("HTTP 429", False), PollFailed("HTTP 403", True), Updates(buf="b")
    )
    assert [ready.step() for _ in range(3)] == [5, 10, 0]
    assert ready.rows() == []  # 脚本把真正的消息挡在外面，三轮里一条也没收到


def test_a_failed_poll_is_recorded_with_its_reason(ready, clock):
    ready.upstream.script("get_updates", PollFailed("ret 500", True))
    ready.step()
    state = ready.account_state()
    assert (state["last_poll_error"], state["last_poll_error_ms"]) == ("ret 500", clock.now_ms())
    assert state["last_poll_ok_ms"] is None


def test_only_a_completed_round_trip_counts_for_upstream_down(ready, clock):
    ready.upstream.script("get_updates", PollFailed("连不上", False))
    ready.step()
    assert ready.account_state()["last_upstream_ok_ms"] is None
    clock.advance(5)
    ready.upstream.script("get_updates", PollFailed("HTTP 403", True))
    ready.step()
    assert ready.account_state()["last_upstream_ok_ms"] == clock.now_ms()


@pytest.mark.parametrize("timed_out", [False, True])
def test_a_normal_poll_is_a_completed_round_trip(ready, clock, timed_out):
    ready.upstream.script("get_updates", Updates(buf="b1", timed_out=timed_out))
    ready.step()
    state = ready.account_state()
    assert state["last_poll_ok_ms"] == state["last_upstream_ok_ms"] == clock.now_ms()


def test_the_last_error_stays_visible_after_the_service_recovers(ready, clock):
    ready.upstream.script("get_updates", PollFailed("HTTP 502", False), Updates(buf="b1"))
    ready.step()
    error_at = clock.now_ms()
    clock.advance(5)
    ready.step()
    state = ready.account_state()
    assert (state["last_poll_error"], state["last_poll_error_ms"]) == ("HTTP 502", error_at)
    assert state["last_poll_ok_ms"] == clock.now_ms()


def test_credentials_never_show_up_in_the_recorded_error(ready):
    ready.upstream.script("get_updates", PollFailed("HTTP 502", False))
    ready.step()
    state = dict(ready.account_state())
    assert BOT_TOKEN not in repr(state)


# ---------------------------------------------------------------- 长轮询的读超时


def test_the_read_timeout_starts_at_the_configured_poll_timeout(ready):
    ready.step()
    assert ready.upstream.calls_to("get_updates")[0].args["timeout"] == 45


def test_the_configured_poll_timeout_is_used(make_rig):
    rig = make_rig(upstream={"poll_timeout": 30})
    rig.bind()
    rig.step()
    assert rig.upstream.calls_to("get_updates")[0].args["timeout"] == 30


@pytest.mark.parametrize(
    ("hint_ms", "timeout"),
    [(60_000, 70), (46_000, 56), (45_000, 45), (40_000, 45), (20_000, 45)],
)
def test_only_a_suggestion_longer_than_the_configured_timeout_counts_plus_ten_seconds(
    ready, hint_ms, timeout
):
    ready.upstream.script("get_updates", Updates(longpolling_timeout_ms=hint_ms), Updates())
    ready.step()
    ready.step()
    timeouts = [c.args["timeout"] for c in ready.upstream.calls_to("get_updates")]
    assert timeouts == [45, timeout]


def test_a_response_without_a_suggestion_keeps_the_last_one(ready):
    ready.upstream.script(
        "get_updates", Updates(longpolling_timeout_ms=60_000), Updates(), Updates(), Updates()
    )
    for _ in range(4):
        ready.step()
    timeouts = [c.args["timeout"] for c in ready.upstream.calls_to("get_updates")]
    assert timeouts == [45, 70, 70, 70]


def test_a_shorter_suggestion_does_not_shorten_the_timeout_below_the_configured_one(ready):
    ready.upstream.script(
        "get_updates",
        Updates(longpolling_timeout_ms=60_000),
        Updates(longpolling_timeout_ms=20_000),
        Updates(),
    )
    for _ in range(3):
        ready.step()
    timeouts = [c.args["timeout"] for c in ready.upstream.calls_to("get_updates")]
    assert timeouts == [45, 70, 45]


def test_an_error_does_not_change_the_timeout(ready):
    ready.upstream.script(
        "get_updates",
        Updates(longpolling_timeout_ms=60_000),
        PollFailed("HTTP 502", False),
        Updates(),
    )
    for _ in range(3):
        ready.step()
    timeouts = [c.args["timeout"] for c in ready.upstream.calls_to("get_updates")]
    assert timeouts == [45, 70, 70]


# ---------------------------------------------------------------- 没有凭证


def test_without_credentials_nothing_is_polled(rig):
    assert rig.step() == 5
    assert rig.upstream.calls_to("get_updates") == []


def test_a_broken_credentials_file_is_waited_out_too(rig):
    rig.bind()
    rig.hub.corrupt()
    assert rig.step() == 5
    assert rig.upstream.calls_to("get_updates") == []


def test_credentials_written_behind_the_monitors_back_are_picked_up_by_the_round_itself(rig):
    rig.hub.store.write(AccountFile(bot_token=BOT_TOKEN, user_id=USER))  # 没有经过 monitor.sync()
    assert rig.step() == 0
    assert len(rig.upstream.calls_to("get_updates")) == 1


def test_credentials_that_appear_later_are_picked_up(rig):
    assert rig.step() == 5
    rig.bind()
    assert rig.step() == 0
    assert len(rig.upstream.calls_to("get_updates")) == 1


def test_the_round_after_a_new_login_starts_from_the_new_progress(ready):
    ready.upstream.script("get_updates", Updates(buf="b1"))
    ready.step()
    assert ready.hub.store.read().updates_buf == "b1"
    ready.bind(bot_token="tok-new", updates_buf="")  # hubctl login：新凭证，进度清空
    ready.step()
    last = ready.upstream.calls_to("get_updates")[-1].args
    assert (last["acct"].bot_token, last["buf"]) == ("tok-new", "")


def test_credentials_that_come_with_their_own_progress_continue_from_it(ready):
    ready.bind(bot_token="tok-imported", updates_buf="buf-7")  # 从别处导入的凭证带着进度
    ready.step()
    first = ready.upstream.calls_to("get_updates")[0].args
    assert (first["acct"].bot_token, first["buf"]) == ("tok-imported", "buf-7")


def test_credentials_with_progress_are_used_from_the_first_poll_after_a_start(rig):
    rig.bind(updates_buf="buf-3")
    rig.step()
    assert rig.upstream.calls_to("get_updates")[0].args["buf"] == "buf-3"


def test_a_new_login_ends_the_backoff(ready):
    ready.upstream.script("get_updates", *failing(("HTTP 502", False), ("HTTP 502", False)))
    assert [ready.step() for _ in range(2)] == [5, 10]
    ready.bind(bot_token="tok-new")
    ready.upstream.script("get_updates", PollFailed("HTTP 502", False))
    assert ready.step() == 5  # 从头计


def test_a_login_during_a_poll_keeps_the_new_progress_file(ready):
    def relogin_during_poll(call):
        ready.bind(bot_token="tok-new")  # 这一轮长轮询还在路上，hubctl login 写好了新凭证
        return Updates(messages=(message("路上收到的话"),), buf="b9")

    ready.upstream.script("get_updates", relogin_during_poll)
    ready.step()
    account = ready.hub.store.read()
    assert (account.bot_token, account.updates_buf) == ("tok-new", "")  # 旧凭证的进度不写回
    assert texts_of(ready) == ["路上收到的话"]  # 话照收
    ready.step()
    assert ready.upstream.calls_to("get_updates")[-1].args["buf"] == ""


# ---------------------------------------------------------------- 登录失效（-14）


def test_a_minus_14_marks_the_login_expired_and_probes_every_600_seconds(ready):
    ready.upstream.script("get_updates", SessionExpired())
    assert ready.step() == 600
    assert ready.hub.monitor.login().state == "expired"
    assert ready.hub.expired == [1]
    state = ready.account_state()
    assert state["last_poll_error"] == "登录失效（-14）"
    assert state["last_upstream_ok_ms"] is not None  # -14 也是一次完成的往返


def test_the_probe_interval_comes_from_the_config(make_rig):
    rig = make_rig(upstream={"relogin_probe_seconds": 120})
    rig.bind()
    rig.upstream.script("get_updates", SessionExpired(), PollFailed("HTTP 502", False))
    assert [rig.step(), rig.step()] == [120, 120]


def test_a_probe_that_works_restores_the_login_and_the_messages_are_stored(ready):
    ready.upstream.script("get_updates", SessionExpired())
    ready.step()
    m = ready.say("探测时来的话", at_ms=None)
    ready.upstream.script("get_updates", Updates(messages=(m,), buf="b2"))
    assert ready.step() == 0
    assert ready.hub.monitor.login().state == "ok"
    assert ready.hub.restored == [1]
    assert texts_of(ready) == ["探测时来的话"]
    assert ready.hub.store.read().updates_buf == "b2"


@pytest.mark.parametrize(
    "outcome",
    [
        PollFailed("HTTP 502", False),
        PollFailed("连不上", False),
        PollFailed("ret 500", True),
        Updates(timed_out=True),
        SessionExpired(),
    ],
    ids=["502", "no connection", "ret 500", "read timeout", "minus 14 again"],
)
def test_a_probe_that_does_not_prove_recovery_leaves_the_login_expired(ready, outcome):
    ready.upstream.script("get_updates", SessionExpired(), outcome, outcome)
    assert ready.step() == 600
    assert ready.step() == 600
    assert ready.hub.monitor.login().state == "expired"
    assert ready.hub.restored == []
    assert ready.step() == 600  # 间隔不变
    assert ready.hub.expired == [1]  # 没有第二次"失效"


def test_a_probe_that_gets_an_ordinary_answer_is_a_completed_round_trip(ready, clock):
    ready.upstream.script("get_updates", SessionExpired(), Updates(timed_out=True))
    ready.step()
    expired_at = clock.now_ms()
    assert ready.account_state()["last_poll_ok_ms"] is None  # -14 不算"收消息成功"
    clock.advance(600)
    assert ready.step() == 600
    state = ready.account_state()
    assert state["last_poll_ok_ms"] == state["last_upstream_ok_ms"] == clock.now_ms()
    assert state["last_upstream_ok_ms"] != expired_at


def test_nothing_is_sent_while_the_login_is_expired_but_polls_continue_as_probes(ready):
    ready.upstream.script("get_updates", SessionExpired())
    ready.step()
    ready.step()
    assert len(ready.upstream.calls_to("get_updates")) == 2
    assert ready.upstream.calls_to("send_text") == []


def test_the_expired_state_survives_a_restart(ready):
    ready.upstream.script("get_updates", SessionExpired(), PollFailed("HTTP 502", False))
    ready.step()
    ready.new_poller()
    assert ready.step() == 600  # 重启后的第一轮就是探测，不是正常收消息
    assert ready.hub.expired == [1]


def test_a_minus_14_after_the_credentials_changed_is_not_an_expiry(ready):
    def relogin_during_poll(call):
        ready.bind(bot_token="tok-new")
        return SessionExpired()  # 旧凭证被新登录顶掉了

    ready.upstream.script("get_updates", relogin_during_poll)
    assert ready.step() == 0  # 马上用新凭证再收
    assert ready.hub.monitor.login().state == "ok"
    assert ready.hub.expired == []
    state = ready.account_state()
    assert state["last_poll_error"] is None  # 旧凭证被顶掉不算收消息的错误
    assert state["last_upstream_ok_ms"] is not None  # 但这是一次完成的往返
    ready.step()
    assert ready.upstream.calls_to("get_updates")[-1].args["acct"].bot_token == "tok-new"


def test_a_minus_14_for_replaced_credentials_loads_the_new_ones_at_once(ready):
    def replaced_behind_the_monitors_back(call):
        ready.hub.store.write(AccountFile(bot_token="tok-new", user_id=USER))  # 监视线程还没发现
        return SessionExpired()

    ready.upstream.script("get_updates", replaced_behind_the_monitors_back)
    assert ready.step() == 0
    ready.step()
    assert ready.upstream.calls_to("get_updates")[-1].args["acct"].bot_token == "tok-new"


def test_a_minus_14_right_after_a_new_login_is_not_an_expiry(ready, clock):
    ready.bind(aged=False, bot_token="tok-new")  # 刚换上新凭证，60 秒宽限还在
    ready.upstream.script("get_updates", SessionExpired(), SessionExpired())
    assert ready.step() == 5  # 按错误退避，不进探测
    assert ready.hub.monitor.login().state == "ok"
    assert ready.hub.expired == []
    clock.advance(61)
    assert ready.step() == 600  # 宽限过了，同样的 -14 才算失效
    assert ready.hub.expired == [1]


def test_a_minus_14_during_a_scan_is_not_an_expiry(ready, clock):
    write_scan_marker(ready.hub.paths, pid=os.getpid(), started_ms=clock.now_ms())
    ready.upstream.script("get_updates", SessionExpired())
    assert ready.step() == 5
    assert ready.hub.monitor.login().state == "ok"
    assert ready.hub.expired == []


# ---------------------------------------------------------------- 线程：睡眠、叫醒、停止


def test_the_loop_polls_with_the_new_credentials_at_once_when_woken_from_a_probe_sleep(
    ready, clock
):
    gate = Gate()
    ready.upstream.script("get_updates", SessionExpired(), gate)
    ready.poller.start()
    try:
        sleeping_until(clock, clock.now_ms() + 600_000)
        ready.bind(bot_token="tok-new")  # 另一个终端 hubctl login；5 秒内监视器换上新凭证并叫醒
        assert gate.entered.wait(5)
        assert ready.upstream.calls_to("get_updates")[-1].args["acct"].bot_token == "tok-new"
        assert ready.hub.monitor.login().state == "ok"
    finally:
        gate.release()
        ready.poller.stop(5)


def test_the_loop_sleeps_the_backoff_and_polls_again(ready, clock):
    gate = Gate()
    ready.upstream.script("get_updates", PollFailed("HTTP 502", False), gate)
    ready.poller.start()
    try:
        sleeping_until(clock, clock.now_ms() + 5_000)
        assert not gate.entered.is_set()
        clock.advance(5)
        assert gate.entered.wait(5)
    finally:
        gate.release()
        ready.poller.stop(5)


def test_an_unexpected_error_backs_off_and_the_loop_keeps_going(ready, clock, caplog):
    gate = Gate()
    ready.upstream.script("get_updates", RuntimeError("内部细节ZZ"), RuntimeError("again"), gate)
    with caplog.at_level(logging.ERROR, logger="clawbot_hub.poller"):
        ready.poller.start()
        try:
            sleeping_until(clock, clock.now_ms() + 5_000)
            clock.advance(5)
            sleeping_until(clock, clock.now_ms() + 10_000)  # 连续出错照样翻倍
            clock.advance(10)
            assert gate.entered.wait(5)
        finally:
            gate.release()
            ready.poller.stop(5)
    assert "RuntimeError" in caplog.text and "内部细节ZZ" not in caplog.text


def test_stop_ends_a_sleeping_loop_at_once_without_one_more_poll(ready, clock):
    ready.upstream.script("get_updates", PollFailed("HTTP 502", False))
    ready.poller.start()
    sleeping_until(clock, clock.now_ms() + 5_000)
    assert ready.poller.running
    ready.poller.stop(5)
    assert not ready.poller.running
    assert len(ready.upstream.calls_to("get_updates")) == 1  # 醒来发现叫停，不再去长轮询


def test_the_loop_polls_again_at_once_after_a_poll_that_got_a_normal_response(ready):
    gate = Gate()
    ready.upstream.script("get_updates", Updates(buf="b1"), Updates(buf="b2"), gate)
    ready.poller.start()
    try:
        assert gate.entered.wait(5)  # 假时钟一直没动，第三次轮询还是来了：中间没有等
        assert [c.args["buf"] for c in ready.upstream.calls_to("get_updates")] == ["", "b1", "b2"]
    finally:
        gate.release()
        ready.poller.stop(5)


def test_stop_raises_its_flag_before_it_wakes_the_loop(ready, monkeypatch):
    """线程醒来先看标志：标志在叫醒之后才置的话，醒来的线程可能没看到，又去长轮询了。"""
    seen = []
    wake = ready.hub.monitor.wake
    original = wake.set

    def set_and_note() -> None:
        seen.append(ready.poller.stopping)
        original()

    monkeypatch.setattr(wake, "set", set_and_note)
    ready.poller.stop(5)
    assert seen == [True]


def test_stop_gives_up_waiting_for_a_long_poll_after_its_time_limit(ready):
    gate = Gate()
    ready.upstream.script("get_updates", gate)
    ready.poller.start()
    try:
        assert gate.entered.wait(5)
        run_in_thread(ready.poller.stop, 0.05).result()  # 限时 0.05 秒：长轮询没回来也返回
        assert ready.poller.running  # 线程还在等那个长轮询
    finally:
        gate.release()
        ready.poller.stop(5)
    assert not ready.poller.running


def test_a_poll_that_comes_back_after_stop_is_dropped_and_asks_for_a_pause(ready):
    ready.upstream.script("get_updates", Updates(messages=(message("路上的话"),), buf="b1"))
    ready.poller.stop(5)  # 没起线程：只是叫停
    assert ready.step() == IDLE_SECONDS
    assert ready.rows() == []
    assert ready.hub.store.read().updates_buf == ""


def test_the_pause_after_stop_does_not_depend_on_how_far_the_backoff_has_grown(ready):
    ready.upstream.script("get_updates", *failing(("HTTP 502", False), ("HTTP 502", False)))
    assert [ready.step(), ready.step()] == [5, 10]
    ready.poller.stop(5)
    assert ready.step() == IDLE_SECONDS  # 不是下一档 20 秒


def test_the_receiving_thread_is_a_daemon_so_a_stuck_poll_cannot_hold_the_process(ready):
    ready.upstream.set_default("get_updates", PollFailed("HTTP 502", False))
    ready.poller.start()
    try:
        threads = [t for t in threading.enumerate() if t.name == "hub-poller"]
        assert threads and all(t.daemon for t in threads)
    finally:
        ready.poller.stop(5)


def test_stop_by_default_waits_longer_than_the_longest_long_poll_can_take(ready):
    default = inspect.signature(Poller.stop).parameters["timeout"].default
    longest = _INT_RANGES["upstream.poll_timeout"][1] + HINT_MARGIN_SECONDS
    assert default == JOIN_SECONDS and JOIN_SECONDS >= longest


def test_stop_without_start_and_twice_is_fine(ready):
    ready.poller.stop(5)
    ready.poller.stop(5)


def test_start_twice_is_a_mistake(ready):
    ready.upstream.set_default("get_updates", PollFailed("HTTP 502", False))
    ready.poller.start()
    try:
        with pytest.raises(RuntimeError):
            ready.poller.start()
    finally:
        ready.poller.stop(5)


def test_a_batch_that_arrives_after_stop_is_left_for_the_next_start(ready):
    gate = Gate()
    ready.upstream.script("get_updates", gate)
    ready.say("路上的话")
    ready.poller.start()
    assert gate.entered.wait(5)
    stopper = run_in_thread(ready.poller.stop)
    assert stopper.still_blocked()  # 长轮询还没回来
    gate.release()
    stopper.result()
    assert ready.rows() == []  # 关闭了就不再处理；进度没写回，下次启动腾讯再给
    assert ready.hub.store.read().updates_buf == ""


def test_the_loop_picks_up_credentials_that_appear_while_it_waits(rig, clock):
    gate = Gate()
    rig.upstream.script("get_updates", gate)
    rig.poller.start()
    try:
        sleeping_until(clock, clock.now_ms() + 5_000)
        rig.bind()
        assert gate.entered.wait(5)
    finally:
        gate.release()
        rig.poller.stop(5)


def test_a_stopped_poller_can_be_started_again(ready, clock):
    ready.upstream.set_default("get_updates", PollFailed("HTTP 502", False))
    ready.poller.start()
    sleeping_until(clock, clock.now_ms() + 5_000)
    ready.poller.stop(5)
    assert not ready.poller.running and ready.poller.stopping
    ready.poller.start()
    try:
        assert ready.poller.running and not ready.poller.stopping
    finally:
        ready.poller.stop(5)


# ---------------------------------------------------------------- 日志里没有秘密


def test_neither_credentials_nor_message_text_reach_the_log(ready, caplog):
    with caplog.at_level(logging.DEBUG):
        ready.say("机密正文QQ", context_token="ctx-secret-log")
        ready.say("陌生话QQ", from_user_id="stranger@im.wechat")
        ready.say("没凭证QQ", context_token=None)
        ready.say("服务 状态", context_token="ctx-secret-log-2")
        ready.step()
        ready.flush()
        ready.upstream.script(
            "get_updates",
            PollFailed("HTTP 502", False),
            RuntimeError("机密正文QQ"),
            SessionExpired(),
        )
        for _ in range(3):
            try:
                ready.step()
            except RuntimeError:
                pass
    for secret in (BOT_TOKEN, "ctx-secret-log", "机密正文QQ", "陌生话QQ", "没凭证QQ"):
        assert secret not in caplog.text


# ---------------------------------------------------------------- 交给谁（路由的接线）


def test_a_prefixed_message_goes_to_that_project(ready):
    x = ready.add("xx", prefix="x")
    ready.say("x 状态")
    ready.step()
    [row] = ready.rows()
    assert (row["project_id"], row["text"], row["via"]) == (x.id, "状态", "prefix")


def wait_for_confirmation(rig, project, clock, minutes: int = 10) -> None:
    """让 `project` 在线并且在等确认。"""
    rig.heartbeat(project)
    with rig.db.txn() as conn:
        conn.execute(
            "UPDATE projects SET awaiting_until_ms = ? WHERE id = ?",
            (clock.now_ms() + minutes * 60_000, project.id),
        )


def test_a_confirmation_goes_to_the_project_that_waits_and_clears_its_marker(ready, clock):
    x = ready.add("xx", prefix="x")
    wait_for_confirmation(ready, x, clock)
    ready.say("对")
    ready.step()
    [row] = ready.rows()
    assert (row["project_id"], row["text"], row["via"]) == (x.id, "对", "confirm")
    assert ready.hub.project("xx").awaiting_until_ms is None


def test_stale_markers_found_by_the_router_are_cleared(ready, clock):
    y = ready.add("yy", prefix="y")
    ready.heartbeat(y)  # 在线：只有"到期了"这一条能让标记失效
    with ready.db.txn() as conn:
        conn.execute(
            "UPDATE projects SET awaiting_until_ms = ? WHERE id = ?", (clock.now_ms() - 1, y.id)
        )
    ready.say("对")
    ready.step()
    [row] = ready.rows()
    assert (row["project_id"], row["via"]) == (ready.quant.id, "default")
    assert ready.hub.project("yy").awaiting_until_ms is None


def test_a_restart_does_not_clear_the_waiting_marker(ready, clock):
    x = ready.add("xx", prefix="x")
    wait_for_confirmation(ready, x, clock)
    ready.new_poller()
    ready.heartbeat(x)  # 服务重启后 x 重新取话
    ready.say("对")
    ready.step()
    [row] = ready.rows()
    assert (row["project_id"], row["via"]) == (x.id, "confirm")


def test_a_project_that_waits_but_stopped_asking_for_messages_loses_its_marker(ready, clock):
    x = ready.add("xx", prefix="x")
    wait_for_confirmation(ready, x, clock)
    clock.advance(91)  # 90 秒没取话：不在线
    ready.say("对")
    ready.step()
    [row] = ready.rows()
    assert (row["project_id"], row["via"]) == (ready.quant.id, "default")
    assert ready.hub.project("xx").awaiting_until_ms is None


def test_a_project_that_waits_but_was_disabled_loses_its_marker(ready, clock):
    x = ready.add("xx", prefix="x")
    wait_for_confirmation(ready, x, clock)
    ready.hub.projects.disable("xx")
    ready.say("对")
    ready.step()
    [row] = ready.rows()
    assert (row["project_id"], row["via"]) == (ready.quant.id, "default")
    assert ready.hub.project("xx").awaiting_until_ms is None


def test_what_one_message_of_a_batch_changed_is_seen_by_the_next_one(ready, clock):
    x = ready.add("xx", prefix="x")
    wait_for_confirmation(ready, x, clock)
    now = clock.now_ms()
    ready.say("x 状态", at_ms=now - 2_000)  # 交给 x，x 之前在等的确认作废
    ready.say("对", at_ms=now - 1_000)  # 这时已经没有项目在等
    ready.step()
    assert [(row["project_id"], row["via"]) for row in ready.rows()] == [
        (x.id, "prefix"),
        (ready.quant.id, "default"),
    ]


def test_a_message_to_a_project_clears_that_projects_own_marker_only(ready, clock):
    ready.add("xx", prefix="x")
    with ready.db.txn() as conn:
        conn.execute("UPDATE projects SET awaiting_until_ms = ?", (clock.now_ms() + 60_000,))
    ready.say("持仓")  # 交给默认项目 ashare-quant
    ready.step()
    assert ready.hub.project("ashare-quant").awaiting_until_ms is None
    assert ready.hub.project("xx").awaiting_until_ms is not None


def test_a_hub_command_is_not_stored_and_does_not_touch_markers(ready, clock):
    x = ready.add("xx", prefix="x")
    with ready.db.txn() as conn:
        conn.execute(
            "UPDATE projects SET awaiting_until_ms = ? WHERE id = ?",
            (clock.now_ms() + 60_000, x.id),
        )
    ready.say("服务 状态")
    ready.step()
    assert ready.rows() == []
    assert ready.hub.project("xx").awaiting_until_ms is not None
    assert len(ready.windows()) == 1  # 这句话本身开了窗口，服务才有额度回复


def test_the_replies_to_one_batch_go_out_in_the_order_of_the_messages(ready, clock):
    now = clock.now_ms()
    ready.say("服务 状态", at_ms=now - 1_000)  # 在批里最先，但时间在后
    ready.say("服务 项目", at_ms=now - 2_000)
    ready.step()
    first, second = ready.flush()
    assert first.startswith("【服务】现在能找的项目") and second.startswith("【服务】登录状态")


def test_a_message_the_service_answers_itself_still_opens_a_window(ready):
    ready.hub.projects.disable("ashare-quant")
    ready.say("持仓")
    ready.step()
    assert ready.rows() == [] and len(ready.windows()) == 1


def test_waiting_fetch_requests_are_woken_after_the_commit(ready, clock):
    waiting = run_in_thread(ready.inbox.fetch, ready.quant, wait=30, max_items=10)
    clock.wait_until_blocked()
    started = clock.now_ms()
    ready.say("持仓")
    ready.step()
    assert texts(waiting.result()) == ["持仓"]
    assert clock.now_ms() == started  # 假时钟没走：是被叫醒的，不是等到了下一个 5 秒


def test_the_window_token_is_stored_with_the_item_for_replies(ready):
    ready.say("持仓", context_token="ctx-reply-me")
    ready.step()
    [item] = ready.inbox.fetch(ready.quant, wait=0, max_items=10)
    target = ready.inbox.resolve_reply(ready.quant, item.reply_handle)
    assert target.context_token == "ctx-reply-me"
