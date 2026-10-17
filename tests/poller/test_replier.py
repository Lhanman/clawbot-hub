"""服务自己的回复（任务 C3，规格 `hub-inbox`「服务自己的指令和提示」「等确认」「项目没在线」）。

分两层：`Replier` 本身（有上限的内存队列、一个发送线程、尽力而为至多一次），和 poller 在什么
时候放进什么话（指令的回答、四种提示的限频、有几个项目在等确认）。文字本身在 `test_hubtext.py`。
"""

from __future__ import annotations

import inspect
import logging
import threading

import pytest

from clawbot_hub.config import Config
from clawbot_hub.quota import PUSH
from clawbot_hub.replier import JOIN_SECONDS, REPLY_QUEUE_MAX, Outgoing, Replier
from clawbot_hub.services import InFlight
from clawbot_hub.upstream.base import Rejected
from tests.poller.conftest import Rig
from tests.support.fake_upstream import Gate
from tests.support.hub import BOT_TOKEN
from tests.support.threads import run_in_thread, wait_for

MINUTE = 60_000


@pytest.fixture
def ready(rig: Rig):
    """绑好账号；ashare-quant（前缀"量化"，默认）和 x（前缀 x）两个项目。"""
    rig.bind()
    rig.quant = rig.add("ashare-quant", prefix="量化")
    rig.x = rig.add("xx", prefix="x")
    return rig


def await_confirmation(rig: Rig, project, clock) -> None:
    rig.heartbeat(project)
    with rig.db.txn() as conn:
        conn.execute(
            "UPDATE projects SET awaiting_until_ms = ? WHERE id = ?",
            (clock.now_ms() + 10 * MINUTE, project.id),
        )


def ask(rig: Rig, text: str, **kwargs) -> list[str]:
    """使用者发一句话，收一轮，把服务要回的话发完，返回发出去的文字。"""
    rig.say(text, **kwargs)
    rig.step()
    return rig.flush()


# ---------------------------------------------------------------- 服务 项目


def test_the_projects_command_lists_the_projects_that_can_receive(ready):
    ready.add("pusher", push_only=True)
    ready.add("off")
    ready.hub.projects.disable("off")
    ready.heartbeat(ready.quant)
    [text] = ask(ready, "服务 项目")
    assert text.splitlines()[0].startswith("【服务】")
    assert "量化（默认，在线）：不加前缀的话都给它，也可以写“量化 …”" in text
    assert "x（离线）：写“x …”交给它" in text
    assert "pusher" not in text and "off" not in text
    assert ready.rows() == []  # 没有交给任何项目


def test_a_project_with_a_hanging_request_counts_as_online_in_the_list(ready, clock):
    waiting = run_in_thread(ready.inbox.fetch, ready.x, wait=30, max_items=10)
    clock.wait_until_blocked()
    [text] = ask(ready, "服务 项目")
    assert "x（在线）" in text
    ready.inbox.wake(ready.x.id)
    clock.advance(30)
    waiting.result()


# ---------------------------------------------------------------- 服务 状态


def test_the_status_command_has_login_window_usage_and_projects_but_no_secrets(ready):
    ready.heartbeat(ready.quant)
    [text] = ask(ready, "服务 状态", context_token="ctx-secret-77")
    assert text.startswith("【服务】登录状态：正常")
    for expected in ("对话窗口：还剩 1 天，已用 0/10 条", "项目：量化 在线，x 离线"):
        assert expected in text
    for secret in (
        BOT_TOKEN,
        "ctx-secret-77",
        ready.hub.keys["xx"],
        ready.hub.keys["ashare-quant"],
    ):
        assert secret not in text


def test_every_message_opens_a_fresh_window_so_the_status_reply_starts_at_zero(ready):
    ask(ready, "服务 状态")
    [text] = ask(ready, "服务 状态")  # 上一条回复记在上一个窗口里
    assert "已用 0/10 条" in text


def test_the_status_info_before_any_message_and_after_the_window_closed(ready, clock):
    assert ready.poller.status_info().window == "none"
    ready.hub.speak()
    info = ready.poller.status_info()
    assert (info.window, info.remaining_seconds, info.used, info.limit) == ("open", 86_400, 0, 10)
    ready.hub.fill(3)
    clock.advance(3600)
    info = ready.poller.status_info()
    assert (info.remaining_seconds, info.used) == (23 * 3600, 3)
    clock.advance(23 * 3600)
    assert ready.poller.status_info().window == "closed"


def test_the_status_info_counts_pushes_and_replies_together(ready):
    ready.hub.speak()
    ready.hub.fill(2, project="ashare-quant", kind=PUSH)
    ready.hub.fill(3)  # 服务自己的回复
    assert ready.poller.status_info().used == 5


def test_the_status_info_follows_the_login_state(ready):
    assert ready.poller.status_info().login == "ok"
    ready.hub.expire()
    assert ready.poller.status_info().login == "expired"
    ready.hub.unbind()
    assert ready.poller.status_info().login == "missing"


def test_the_status_info_lists_only_projects_that_can_receive(ready):
    ready.add("pusher", push_only=True)
    ready.hub.projects.disable("xx")
    ready.heartbeat(ready.quant)
    lines = ready.poller.status_info().projects
    assert [(line.display, line.is_default, line.online) for line in lines] == [
        ("量化", True, True)
    ]


# ---------------------------------------------------------------- 别的字


@pytest.mark.parametrize(
    "text", ["服务", "服务 重启", "服务：", "服务 项目 状态", "服务 状态 详情"]
)
def test_anything_else_after_the_label_gets_the_help_line(ready, text):
    assert ask(ready, text) == ["【服务】我只认两个指令：服务 项目、服务 状态"]
    assert ready.rows() == []


def test_a_word_that_merely_starts_with_the_label_goes_to_the_default_project(ready):
    ready.heartbeat(ready.quant)
    assert ask(ready, "服务器状态") == []
    [row] = ready.rows()
    assert (row["project_id"], row["text"]) == (ready.quant.id, "服务器状态")


def test_the_label_changes_the_command_word_and_the_help_line(make_rig):
    rig = make_rig(hub={"label": "助手"})
    rig.bind()
    quant = rig.add("ashare-quant", prefix="量化")
    rig.heartbeat(quant)
    assert ask(rig, "助手 重启") == ["【助手】我只认两个指令：助手 项目、助手 状态"]
    [text] = ask(rig, "助手 状态")
    assert text.startswith("【助手】登录状态：正常")
    assert ask(rig, "服务 状态") == []  # 旧的指令词现在只是一句普通的话
    [row] = rig.rows()
    assert (row["project_id"], row["text"]) == (quant.id, "服务 状态")


def test_a_reply_goes_out_with_the_token_of_the_message_that_asked(ready):
    ask(ready, "服务 状态", context_token="ctx-asked")
    assert [m.context_token for m in ready.upstream.sent] == ["ctx-asked"]
    assert [m.to_user_id for m in ready.upstream.sent] == [ready.hub.monitor.account().user_id]


def test_service_replies_are_counted_as_replies_under_the_internal_name(ready):
    ask(ready, "服务 状态")
    assert ready.usage() == {("_hub", "reply"): 1}
    state = ready.hub.quota.read_state(ready.db.conn())
    assert state.replies_used == 1
    assert all(share.used == 0 for share in state.shares.values())  # 不占任何项目的保底和上限


def test_a_service_reply_still_goes_out_when_pushes_have_used_up_the_rest_of_the_window(rig):
    rig.bind()
    quant = rig.add("ashare-quant", prefix="量化", guaranteed=6, cap=8)
    rig.heartbeat(quant)
    rig.say("服务 状态")
    rig.step()  # 这句话开了新窗口，回复已在队里
    rig.hub.fill(8, project="ashare-quant", kind=PUSH)  # 推送把窗口里非保留的 8 条用光
    assert len(rig.flush()) == 1  # 回复留着自己的 2 条


# ---------------------------------------------------------------- 没在线


def test_a_message_for_an_offline_project_is_queued_and_the_user_is_told(ready):
    assert ask(ready, "x 状态") == [
        "【服务】「x」现在没在线。这句话我先留着：10 分钟内它回来就会处理，超过就作废"
    ]
    assert [row["text"] for row in ready.rows()] == ["状态"]


def test_the_notice_says_how_long_the_message_is_kept(make_rig):
    rig = make_rig(inbox={"retention_minutes": 3})
    rig.bind()
    rig.add("xx", prefix="x")
    assert "3 分钟内它回来" in ask(rig, "x 状态")[0]


def test_no_notice_for_a_project_that_is_online(ready):
    ready.heartbeat(ready.x)
    assert ask(ready, "x 状态") == []
    assert len(ready.rows()) == 1


def test_no_notice_while_a_project_works_on_a_slow_command_with_heartbeats(ready, clock):
    ready.heartbeat(ready.x)
    clock.advance(60)
    ready.heartbeat(ready.x)  # 每 30 秒一次心跳，这里省略中间的
    clock.advance(60)
    assert ask(ready, "x 又一句") == []


def test_the_default_project_gets_the_notice_too_when_it_is_offline(ready):
    assert ask(ready, "持仓") == [
        "【服务】「量化」现在没在线。这句话我先留着：10 分钟内它回来就会处理，超过就作废"
    ]


# ---------------------------------------------------------------- 提示的限频


def test_the_same_notice_for_the_same_project_is_sent_once_in_five_minutes(ready, clock):
    sent = []
    for text in ["x 一", "x 二", "x 三"]:
        sent += ask(ready, text)
        clock.advance(20)
    assert len(sent) == 1
    assert [row["text"] for row in ready.rows()] == ["一", "二", "三"]  # 话都在队列里


def test_the_limit_is_per_project(ready):
    ready.add("yy", prefix="y")
    sent = ask(ready, "x 一") + ask(ready, "y 一") + ask(ready, "x 二")
    assert len(sent) == 2
    assert "「x」" in sent[0] and "「y」" in sent[1]


def test_the_limit_ends_after_the_configured_seconds(ready, clock):
    assert len(ask(ready, "x 一")) == 1
    clock.advance(299.999)
    assert ask(ready, "x 二") == []
    clock.advance(0.001)
    assert len(ask(ready, "x 三")) == 1
    assert ask(ready, "x 四") == []  # 又限住了：上一条提醒的时间记成了这一刻


def test_the_limit_period_comes_from_the_config(make_rig, clock):
    rig = make_rig(inbox={"offline_notice_seconds": 60})
    rig.bind()
    rig.add("xx", prefix="x")
    assert len(ask(rig, "x 一")) == 1
    clock.advance(59)
    assert ask(rig, "x 二") == []
    clock.advance(1)
    assert len(ask(rig, "x 三")) == 1


def test_a_stored_time_later_than_now_does_not_silence_the_notice(ready, clock):
    with ready.db.txn() as conn:  # 时钟被调慢过：库里记着"将来"的一刻
        conn.execute(
            "INSERT INTO notice_state (key, last_ms) VALUES ('offline:xx', ?)",
            (clock.now_ms() + 3_600_000,),
        )
    assert len(ask(ready, "x 一")) == 1
    assert ready.notices() == {"offline:xx": clock.now_ms()}


def test_the_last_notice_time_is_kept_in_the_database_and_survives_a_restart(ready, clock):
    assert len(ask(ready, "x 一")) == 1
    assert ready.notices() == {"offline:xx": clock.now_ms()}
    clock.advance(30)
    ready.new_poller()
    assert ask(ready, "x 二") == []  # 重启不清零
    clock.advance(300)
    assert len(ask(ready, "x 三")) == 1


def test_a_limited_notice_leaves_the_stored_time_alone(ready, clock):
    ask(ready, "x 一")
    first = ready.notices()["offline:xx"]
    clock.advance(100)
    ask(ready, "x 二")
    assert ready.notices()["offline:xx"] == first  # 限住的那次不顺延


def test_different_kinds_of_notices_do_not_limit_each_other(ready):
    ready.add("yy", prefix="y")
    ready.hub.projects.disable("yy")
    sent = ask(ready, "x 一") + ask(ready, "y 一")
    assert len(sent) == 2 and "没在线" in sent[0] and "已停用" in sent[1]


# ---------------------------------------------------------------- 没有默认项目、已停用


def test_the_no_default_notice_uses_the_label_too(make_rig):
    rig = make_rig(hub={"label": "助手"})
    rig.bind()
    assert ask(rig, "持仓") == [
        "【助手】没有设默认项目，这句话没处理：请加项目前缀（发“助手 项目”看有哪些）"
    ]


def test_without_a_default_project_the_user_is_told_once(rig, clock):
    rig.bind()
    sent = ask(rig, "持仓") + ask(rig, "再来一句")
    assert sent == ["【服务】没有设默认项目，这句话没处理：请加项目前缀（发“服务 项目”看有哪些）"]
    assert rig.rows() == []
    assert rig.notices() == {"nodefault": clock.now_ms()}


def test_the_no_default_notice_comes_back_after_the_limit(rig, clock):
    rig.bind()
    assert len(ask(rig, "持仓")) == 1
    clock.advance(6 * 60)
    assert len(ask(rig, "持仓")) == 1


def test_a_picture_without_a_default_project_gets_the_same_notice(rig):
    rig.bind()
    assert len(ask(rig, "", has_text=False)) == 1


def test_a_disabled_default_project_gets_one_notice_per_five_minutes(ready, clock):
    ready.hub.projects.disable("ashare-quant")
    first = ask(ready, "持仓")
    assert first == ["【服务】项目「量化」已停用，这句话没处理"]
    clock.advance(60)
    assert ask(ready, "量化 状态") == []  # 同一个目标，落在限频里
    clock.advance(6 * 60)
    assert ask(ready, "持仓") == first
    assert ready.rows() == []
    assert set(ready.notices()) == {"disabled:ashare-quant"}


def test_a_disabled_project_addressed_by_its_prefix_gets_its_own_notice(ready):
    ready.hub.projects.disable("xx")
    assert ask(ready, "x 状态") == ["【服务】项目「x」已停用，这句话没处理"]
    assert set(ready.notices()) == {"disabled:xx"}
    ready.hub.projects.disable("ashare-quant")
    assert len(ask(ready, "持仓")) == 1  # 另一个目标，各限各的


# ---------------------------------------------------------------- 队列积压


def test_a_full_queue_of_leased_messages_turns_new_messages_away_with_a_notice(make_rig, clock):
    rig = make_rig(inbox={"max_queued": 2})
    rig.bind()
    rig.add("ashare-quant", prefix="量化")
    x = rig.add("xx", prefix="x")
    rig.heartbeat(x)
    ask(rig, "x 一")
    ask(rig, "x 二")
    assert len(rig.inbox.fetch(x, wait=0, max_items=10)) == 2  # 全取走了，都没确认
    assert ask(rig, "x 三") == ["【服务】「x」积压太多，这句话没收"]
    assert len(rig.rows()) == 2  # 第三句没有记录
    clock.advance(60)
    assert ask(rig, "x 四") == []  # 五分钟内不重复提醒
    assert len(rig.rows()) == 2
    assert len(rig.events("queue")) == 2  # 每次都记事件
    assert set(rig.notices()) == {"full:xx"}


def test_a_full_queue_that_can_drop_the_oldest_does_not_bother_the_user(make_rig):
    rig = make_rig(inbox={"max_queued": 2})
    rig.bind()
    rig.add("ashare-quant", prefix="量化")
    x = rig.add("xx", prefix="x")
    rig.heartbeat(x)
    for text in ["x 一", "x 二", "x 三"]:
        assert ask(rig, text) == []
    assert [row["state"] for row in rig.rows()] == ["dropped", "queued", "queued"]


def test_the_backlog_notice_replaces_the_offline_notice(make_rig):
    rig = make_rig(inbox={"max_queued": 1})
    rig.bind()
    rig.add("ashare-quant", prefix="量化")
    x = rig.add("xx", prefix="x")
    rig.heartbeat(x)
    ask(rig, "x 一")
    rig.inbox.fetch(x, wait=0, max_items=1)
    with rig.db.txn() as conn:  # 让 x 看起来没在线
        conn.execute("UPDATE projects SET last_inbox_ms = NULL WHERE id = ?", (x.id,))
    assert ask(rig, "x 二") == ["【服务】「x」积压太多，这句话没收"]


# ---------------------------------------------------------------- 有几个项目在等确认


def test_several_waiting_projects_make_the_service_ask_for_a_prefix(ready, clock):
    await_confirmation(ready, ready.quant, clock)
    await_confirmation(ready, ready.x, clock)
    assert ask(ready, "对") == ["【服务】有几个项目在等你确认（量化、x），请加前缀，例如“量化 对”"]
    assert ready.rows() == []
    assert len(ask(ready, "对")) == 1  # 不限频
    assert ready.notices() == {}
    assert ask(ready, "x 对") == []
    [row] = ready.rows()
    assert (row["project_id"], row["text"], row["via"]) == (ready.x.id, "对", "prefix")


def test_the_ambiguity_reply_uses_the_word_the_user_sent(ready, clock):
    await_confirmation(ready, ready.quant, clock)
    await_confirmation(ready, ready.x, clock)
    [text] = ask(ready, "确认")
    assert text.endswith("例如“量化 确认”")


# ---------------------------------------------------------------- 一句话没有回复的时候


def test_an_ordinary_message_to_an_online_project_gets_no_reply(ready):
    ready.heartbeat(ready.quant)
    assert ask(ready, "持仓") == []
    assert ready.upstream.sent == []


# ---------------------------------------------------------------- Replier：队列、顺序、至多一次


def make_replier(rig: Rig) -> Replier:
    return Replier(rig.hub.sender, rig.config.hub.label)


def outgoing(n: int, token: str = "ctx") -> Outgoing:
    return Outgoing(context_token=f"{token}-{n}", text=f"第 {n} 条")


@pytest.fixture
def window(rig: Rig):
    rig.bind()
    rig.hub.speak(token="ctx-window")
    return rig


def test_replies_are_sent_in_the_order_they_were_queued(window):
    replier = make_replier(window)
    for n in range(3):
        replier.submit(outgoing(n))
    assert replier.pending == 3
    while replier.process_one():
        pass
    assert window.upstream.sent_texts() == ["【服务】第 0 条", "【服务】第 1 条", "【服务】第 2 条"]
    assert [m.context_token for m in window.upstream.sent] == ["ctx-0", "ctx-1", "ctx-2"]


def test_an_empty_queue_has_nothing_to_send(window):
    replier = make_replier(window)
    assert replier.process_one() is False
    assert replier.pending == 0


def test_the_queue_holds_twenty_and_drops_the_oldest(window, caplog):
    assert REPLY_QUEUE_MAX == 20
    replier = make_replier(window)
    with caplog.at_level(logging.WARNING, logger="clawbot_hub.replier"):
        dropped = [replier.submit(outgoing(n)) for n in range(23)]
    assert dropped == [0] * 20 + [1] * 3
    assert replier.pending == 20
    replier.process_one()
    assert window.upstream.sent_texts() == ["【服务】第 3 条"]  # 最旧的三条被挤掉了
    # 挤掉一次记一次警告；没挤掉就不记；日志里没有话的内容
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == ["服务回复的队列满了，丢掉最旧的 1 条"] * 3
    assert "第 " not in caplog.text


def test_nothing_is_logged_while_the_queue_has_room(window, caplog):
    replier = make_replier(window)

    with caplog.at_level(logging.DEBUG, logger="clawbot_hub.replier"):
        for n in range(REPLY_QUEUE_MAX):
            replier.submit(outgoing(n))

    assert caplog.records == []


def test_the_reply_being_sent_is_counted_as_in_flight_until_it_is_done(window):
    in_flight = InFlight()
    replier = Replier(window.hub.sender, window.config.hub.label, in_flight=in_flight)
    gate = window.upstream.hold("send_text")
    replier.submit(outgoing(1))

    sending = run_in_thread(replier.process_one)
    assert gate.entered.wait(5)

    assert in_flight.count == 1
    assert replier.pending == 0  # 已经出了队，但还在数着
    gate.release()
    assert sending.result() is True
    assert in_flight.count == 0


def test_a_reply_that_fails_is_not_left_counted_as_in_flight(window):
    in_flight = InFlight()
    replier = Replier(window.hub.sender, window.config.hub.label, in_flight=in_flight)
    window.upstream.script("send_text", RuntimeError("boom"))
    replier.submit(outgoing(1))

    assert replier.process_one() is True

    assert in_flight.count == 0


def test_an_empty_queue_counts_nothing_as_in_flight(window):
    in_flight = InFlight()
    replier = Replier(window.hub.sender, window.config.hub.label, in_flight=in_flight)

    assert replier.process_one() is False

    assert in_flight.count == 0


def test_a_reply_is_tried_once_and_never_again(window):
    window.upstream.script("send_text", Rejected(400, 40001, "bad"))
    replier = make_replier(window)
    replier.submit(outgoing(1))
    assert replier.process_one() is True
    assert replier.pending == 0
    assert replier.process_one() is False
    assert len(window.upstream.calls_to("send_text")) == 1


def test_an_error_while_sending_is_swallowed_and_the_next_reply_still_goes(window, caplog):
    window.upstream.script("send_text", RuntimeError("内部细节ZZ"))
    replier = make_replier(window)
    replier.submit(outgoing(1))
    replier.submit(outgoing(2))
    with caplog.at_level(logging.ERROR, logger="clawbot_hub.replier"):
        assert replier.process_one() and replier.process_one()
    assert window.upstream.sent_texts()[-1] == "【服务】第 2 条"
    assert "RuntimeError" in caplog.text
    assert "内部细节ZZ" not in caplog.text and "第 1 条" not in caplog.text


def test_a_reply_whose_window_is_long_gone_is_dropped_quietly(ready, clock):
    old = clock.now_ms() - 30 * 3_600_000
    ready.say("服务 状态", at_ms=old)  # 服务停了一天多，补收到的旧消息
    ready.step()
    assert ready.replier.pending == 1
    assert ready.flush() == []  # 窗口早关了，发不出去就算了
    assert ready.replier.pending == 0
    assert ready.flush() == []  # 不重试
    assert ready.hub.holds == []  # 不告警（窗口关了不是腾讯拒绝）


def test_a_reply_while_the_login_is_expired_is_dropped_quietly(ready):
    ready.say("服务 状态")
    ready.step()
    ready.hub.expire()  # 放进队列以后、发出之前，登录失效了
    assert ready.flush() == []
    assert ready.upstream.calls_to("send_text") == []
    assert ready.replier.pending == 0


def test_what_is_queued_when_the_service_dies_is_not_sent_after_the_restart(ready):
    ready.say("服务 状态")
    ready.step()
    assert ready.replier.pending == 1
    ready.new_poller()  # 放进队列之后、发出之前被杀
    assert ready.replier.pending == 0
    assert ready.flush() == []
    ready.step()  # 腾讯不再给这句话
    assert ready.flush() == []


def test_the_message_itself_is_not_lost_when_its_reply_is_dropped(ready):
    ready.say("x 状态")
    ready.step()
    ready.new_poller()  # 提醒丢了
    assert [row["text"] for row in ready.rows()] == ["状态"]  # 话已经入队


# ---------------------------------------------------------------- 线程


def test_the_thread_sends_what_arrives_and_stop_ends_it(window):
    replier = make_replier(window)
    replier.start()
    try:
        replier.submit(outgoing(1))
        wait_for(lambda: len(window.upstream.sent) == 1)
        replier.submit(outgoing(2))
        wait_for(lambda: len(window.upstream.sent) == 2)
    finally:
        replier.stop(5)
    assert not replier.running


def parked(replier: Replier) -> bool:
    """发送线程已经睡进 `Condition.wait`（`_waiters` 是 CPython 的实现细节，这里只是为了在测试里
    确定地等到"线程闲着"，免得 `stop` 抢在线程睡下之前把标志置好，叫醒与否就看不出区别了）。"""
    return len(replier._cond._waiters) == 1


def test_stop_wakes_a_thread_that_has_nothing_to_send(window):
    replier = make_replier(window)
    replier.start()
    wait_for(lambda: parked(replier))

    stopper = run_in_thread(replier.stop)  # 默认最多等 60 秒：叫不醒的话它会一直卡在这里

    stopper.result()
    assert not replier.running


def test_stop_does_not_send_the_rest_of_the_queue(window):
    gate = Gate()
    window.upstream.script("send_text", gate)
    replier = make_replier(window)
    replier.start()
    replier.submit(outgoing(1))
    assert gate.entered.wait(5)
    replier.submit(outgoing(2))
    replier.submit(outgoing(3))
    stopper = run_in_thread(replier.stop)
    assert stopper.still_blocked()  # 正在发的那一条让它发完
    gate.release()
    stopper.result()
    assert window.upstream.sent_texts() == ["【服务】第 1 条"]
    assert replier.pending == 0


def test_the_sending_thread_is_a_daemon_so_a_stuck_send_cannot_hold_the_process(window):
    replier = make_replier(window)
    replier.start()
    try:
        threads = [t for t in threading.enumerate() if t.name == "hub-replier"]
        assert threads and all(t.daemon for t in threads)
    finally:
        replier.stop(5)


def test_stop_by_default_waits_longer_than_one_send_can_take(window):
    default = inspect.signature(Replier.stop).parameters["timeout"].default
    assert default == JOIN_SECONDS and JOIN_SECONDS > Config().send.timeout_seconds


def test_stop_without_start_and_twice_is_fine(window):
    replier = make_replier(window)
    replier.stop(5)
    replier.stop(5)


def test_a_stopped_replier_can_be_started_again(window):
    replier = make_replier(window)
    replier.start()
    replier.stop(5)
    replier.start()
    try:
        replier.submit(outgoing(1))
        wait_for(lambda: len(window.upstream.sent) == 1)
    finally:
        replier.stop(5)


def test_start_twice_is_a_mistake(window):
    replier = make_replier(window)
    replier.start()
    try:
        with pytest.raises(RuntimeError):
            replier.start()
    finally:
        replier.stop(5)


def test_the_poller_keeps_receiving_while_the_send_lock_is_held(ready, clock):
    ready.hub.speak()
    gate = Gate()
    ready.upstream.script("send_text", gate)
    push = run_in_thread(ready.hub.sender.push, ready.quant, "占着发送锁的推送")
    assert gate.entered.wait(5)  # 推送卡在向腾讯发请求，发送锁被占着

    ready.replier.start()
    try:
        ready.say("服务 状态")
        ready.say("持仓")
        assert ready.step() == 0  # 马上返回，不等发送锁
        assert [row["text"] for row in ready.rows()] == ["持仓"]  # 话照常入库
        assert ready.hub.store.read().updates_buf == "buf-2"  # 进度照常写回
        clock.wait_until_blocked()  # 服务的回复在等发送锁
        assert len(ready.upstream.sent) == 1  # 只有那条推送发出去了
        ready.say("再来一句")
        assert ready.step() == 0  # 下一轮长轮询照常开始
        assert len(ready.rows()) == 2

        gate.release()
        push.result()
        # 锁一放，队里的两条回复接着发；轮到测试看的时候可能已经发完两条了，所以是“至少”
        wait_for(lambda: len(ready.upstream.sent) >= 2)
        assert ready.upstream.sent_texts()[1].startswith("【服务】登录状态")
    finally:
        gate.release()
        ready.replier.stop(5)
