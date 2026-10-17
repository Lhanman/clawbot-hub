"""发送途中服务内部抛出意外（测试里注入）。规格：`hub-send`「错误格式和 HTTP 状态码」的内部出错、
「去重键」的两个内部出错场景。

规矩：碰腾讯之前出错——条数全部退回、记录标成失败、键释放；碰过腾讯以后出错——已经向腾讯发过请求
的几条（包括出错时正在发的那一条）保留，还没开始发的退回，记录标成"中断"并占着去重键。
意外原样抛给调用者（接口层回 500）。
"""

from __future__ import annotations

import itertools
import logging

import pytest

from clawbot_hub import sender as sender_module
from clawbot_hub.config import Config
from clawbot_hub.quota import INTERRUPTED_TEXT, Quota
from clawbot_hub.sender import Sender
from clawbot_hub.upstream.base import Rejected
from tests.sender.conftest import long_text
from tests.support.fake_upstream import DEFAULT
from tests.support.threads import run_in_thread

KEY = "brief"


class FailingQuota(Quota):
    """在指定的调用上抛出意外的额度记账（从 1 数起）：`settle_calls` 是要抛的第几次 `settle`
    （写 `"all"` 表示每次都抛），`reserve_calls` 是要抛的第几次 `reserve`——后者先记完账再抛，
    用来看事务会不会整个回滚。"""

    def __init__(self, config, clock, *, settle_calls=(), reserve_calls=()):
        super().__init__(config, clock)
        self.settle_calls = set(settle_calls)
        self.reserve_calls = set(reserve_calls)
        self.settles = 0
        self.reserves = 0

    def reserve(self, conn, request, *, outbox_id=None):
        self.reserves += 1
        result = super().reserve(conn, request, outbox_id=outbox_id)
        if self.reserves in self.reserve_calls:
            raise RuntimeError("预占出意外了")
        return result

    def settle(self, conn, reservation, result, **kwargs):
        self.settles += 1
        if self.settles in self.settle_calls or "all" in self.settle_calls:
            raise RuntimeError("对账出意外了")
        return super().settle(conn, reservation, result, **kwargs)


def client_ids(fail_on: int):
    """造 `client_id` 的函数：第 `fail_on` 次调用抛出意外。"""
    counter = itertools.count(1)

    def make() -> str:
        number = next(counter)
        if number == fail_on:
            raise RuntimeError("造不出编号")
        return f"cid-{number}"

    return make


# ---------------------------------------------------------------- 碰过腾讯以后出错


def test_an_error_while_the_second_part_is_out_keeps_both_and_gives_back_the_third(hub):
    a = hub.ready()
    hub.upstream.script("send_text", DEFAULT, RuntimeError("出意外了"))

    with pytest.raises(RuntimeError, match="出意外了"):
        hub.sender.push(a, long_text(3), dedupe_key=KEY)

    assert hub.used() == {
        ("alpha", "push"): 2
    }  # 第 1 条发了；第 2 条在飞，不知道到没到；第 3 条退回
    (row,) = hub.rows()
    assert (row["status"], row["reason_code"], row["reason"]) == (
        "interrupted",
        "interrupted",
        INTERRUPTED_TEXT,
    )
    assert (row["parts"], row["parts_sent"]) == (3, 1)  # 至少能说出已经确认的那一条
    assert row["finished_ms"] is not None
    (event,) = hub.events("send")
    assert event["level"] == "WARNING" and row["id"] in event["detail"]


def test_the_key_of_an_interrupted_send_stays_taken(hub):
    a = hub.ready()
    hub.upstream.script("send_text", DEFAULT, RuntimeError("出意外了"))
    with pytest.raises(RuntimeError):
        hub.sender.push(a, long_text(3), dedupe_key=KEY)
    calls = len(hub.upstream.calls)

    again = hub.sender.push(a, long_text(3), dedupe_key=KEY)

    assert (again.status, again.reason_code, again.duplicate) == (
        "unconfirmed",
        "interrupted",
        True,
    )
    assert again.reason == INTERRUPTED_TEXT
    assert len(hub.upstream.calls) == calls  # 没有新的请求
    assert hub.used() == {("alpha", "push"): 2}


def test_an_error_between_two_parts_keeps_what_went_out_and_gives_back_the_rest(make_hub):
    hub = make_hub(new_client_id=client_ids(fail_on=2))
    a = hub.ready()

    with pytest.raises(RuntimeError, match="造不出编号"):
        hub.sender.push(a, long_text(3), dedupe_key=KEY)

    assert hub.used() == {("alpha", "push"): 1}
    (row,) = hub.rows()
    assert (row["status"], row["parts_sent"]) == ("interrupted", 1)
    assert len(hub.upstream.sent) == 1


def test_an_error_while_booking_a_part_that_went_out_keeps_that_part(make_hub, clock):
    quota = FailingQuota(Config(), clock, settle_calls={2})
    hub = make_hub(quota=quota)
    a = hub.ready()

    with pytest.raises(RuntimeError, match="对账出意外了"):
        hub.sender.push(a, long_text(3))

    # 第 2 条腾讯已经收下了，只是没记上账：它算在已用里，不会被退回
    assert hub.used() == {("alpha", "push"): 2}
    (row,) = hub.rows()
    assert (row["status"], row["parts_sent"]) == ("interrupted", 1)
    assert len(hub.upstream.sent) == 2


def test_an_error_after_everything_was_sent_still_keeps_the_usage(hub, monkeypatch):
    a = hub.ready()

    def broken(*args, **kwargs):
        raise RuntimeError("收尾出意外了")

    monkeypatch.setattr(Sender, "_view", broken)

    with pytest.raises(RuntimeError, match="收尾出意外了"):
        hub.sender.push(a, long_text(2), dedupe_key=KEY)

    assert hub.used() == {("alpha", "push"): 2}
    (row,) = hub.rows()
    assert (row["status"], row["parts_sent"]) == ("interrupted", 2)


def test_when_even_the_clean_up_fails_the_record_is_left_for_the_next_start(
    make_hub, clock, caplog
):
    quota = FailingQuota(Config(), clock, settle_calls={"all"})
    hub = make_hub(quota=quota)
    a = hub.ready()
    caplog.set_level(logging.DEBUG)

    with pytest.raises(RuntimeError, match="对账出意外了"):  # 抛出去的是最初那个意外
        hub.sender.push(a, long_text(3), dedupe_key="不要出现在日志里的键")

    assert any("整理账目也失败了" in record.getMessage() for record in caplog.records)
    assert all("对账出意外了" not in record.getMessage() for record in caplog.records)
    assert all("不要出现在日志里" not in record.getMessage() for record in caplog.records)
    (row,) = hub.rows()
    assert row["status"] == "sending"  # 没来得及写成终态
    assert hub.used() == {("alpha", "push"): 3}
    with hub.db.txn() as conn:  # 下一次启动
        assert hub.quota.interrupt_unfinished(conn) == 1
    assert hub.row(row["id"])["status"] == "interrupted"
    assert hub.used() == {("alpha", "push"): 3}  # 不知道发出去几条，预占不退


def test_parts_already_given_back_are_not_given_back_again_when_the_wrap_up_fails(hub, monkeypatch):
    a = hub.ready()
    assert hub.sender.push(a, "先发一条").status == "sent"  # 这个项目在窗口里已经用了 1 条
    hub.upstream.script("send_text", DEFAULT, Rejected(200, 40001, "bad token"))
    real = sender_module.add_event
    calls = []

    def once_broken(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("收尾写事件出意外了")
        return real(*args, **kwargs)

    monkeypatch.setattr(sender_module, "add_event", once_broken)

    # 3 条：第 1 条发出去，第 2 条被拒绝，第 3 条没发所以退回；然后收尾出了意外
    with pytest.raises(RuntimeError, match="收尾写事件出意外了"):
        hub.sender.push(a, long_text(3))

    assert hub.used() == {("alpha", "push"): 2}  # 先前的 1 条加这次发出去的 1 条，没有多退
    first, second = hub.rows()
    assert (first["status"], second["status"]) == ("sent", "interrupted")


def test_a_restart_after_an_error_finds_nothing_left_to_fix(hub):
    a = hub.ready()
    hub.upstream.script("send_text", DEFAULT, RuntimeError("出意外了"))
    with pytest.raises(RuntimeError):
        hub.sender.push(a, long_text(3))

    with hub.db.txn() as conn:
        assert hub.quota.interrupt_unfinished(conn) == 0

    assert hub.used() == {("alpha", "push"): 2}


def test_the_send_lock_is_free_again_after_an_error(hub):
    a = hub.ready()
    b = hub.add("beta", guaranteed=1, cap=2)
    hub.upstream.script("send_text", RuntimeError("出意外了"))
    with pytest.raises(RuntimeError):
        hub.sender.push(a, "你好")

    result = hub.sender.push(b, "别的项目")

    assert result.status == "sent"


def test_a_request_waiting_on_the_same_key_is_told_about_the_interruption(hub):
    a = hub.ready()
    gate = hub.upstream.hold("send_text")

    def explode(call):
        gate(call)  # 等测试放行
        raise RuntimeError("出意外了")

    hub.upstream.script("send_text", explode)
    first = run_in_thread(hub.sender.push, a, "你好", dedupe_key=KEY)
    assert gate.entered.wait(5)
    second = run_in_thread(hub.sender.push, a, "你好", dedupe_key=KEY)
    hub.clock.wait_until_blocked(1)

    gate.release()

    with pytest.raises(RuntimeError):
        first.result()
    result = second.result()
    assert (result.status, result.reason_code, result.duplicate) == (
        "unconfirmed",
        "interrupted",
        True,
    )


# ---------------------------------------------------------------- 碰腾讯之前出错


def test_an_error_before_the_first_request_gives_everything_back_and_frees_the_key(make_hub):
    hub = make_hub(new_client_id=client_ids(fail_on=1))
    a = hub.ready()

    with pytest.raises(RuntimeError, match="造不出编号"):
        hub.sender.push(a, long_text(3), dedupe_key=KEY)

    assert hub.used() == {}
    assert hub.upstream.calls == []
    (row,) = hub.rows()
    assert (row["status"], row["reason_code"]) == ("failed", "internal")
    assert row["reason"] == "服务内部出错，这条没有发出"
    assert row["parts_sent"] == 0
    (event,) = hub.events("send")
    assert event["level"] == "ERROR"
    again = hub.sender.push(a, "你好", dedupe_key=KEY)  # 键已经释放：当成新的发送
    assert (again.status, again.duplicate) == ("sent", False)


def test_an_error_while_booking_the_reservation_leaves_nothing_behind(make_hub, clock):
    quota = FailingQuota(Config(), clock, reserve_calls={1})
    hub = make_hub(quota=quota)
    a = hub.ready()

    with pytest.raises(RuntimeError, match="预占出意外了"):
        hub.sender.push(a, "你好", dedupe_key=KEY)

    assert hub.rows() == []  # 判定、记账、建记录是一个事务：一起回滚
    assert hub.used() == {}
    assert hub.events("send") == []
    assert hub.upstream.calls == []
    again = hub.sender.push(a, "你好", dedupe_key=KEY)
    assert (again.status, again.duplicate) == ("sent", False)


def test_an_error_while_booking_the_reservation_does_not_start_a_probe(make_hub, clock):
    quota = FailingQuota(Config(), clock, reserve_calls={2})
    hub = make_hub(quota=quota)
    a = hub.ready()
    hub.upstream.script("send_text", Rejected(200, 40001, "bad token"))
    hub.sender.push(a, "第一条")  # 进入暂停
    hub.clock.advance(31 * 60)  # 暂停期过了：下一条是试探

    with pytest.raises(RuntimeError):
        hub.sender.push(a, "试探")

    # 预占回滚了，临时的暂停截止也一起回滚：下一条仍然是试探
    assert hub.sender.push(a, "再试").status == "sent"


def test_a_window_without_a_credential_cannot_be_used_and_leaves_nothing_behind(hub):
    hub.bind()
    hub.speak(token="")  # 不该出现的数据：窗口里没有对话凭证
    a = hub.add("alpha", guaranteed=6, cap=8)

    with pytest.raises(RuntimeError, match="对话凭证"):
        hub.sender.push(a, "你好", dedupe_key=KEY)

    assert hub.rows() == []
    assert hub.used() == {}
    assert hub.upstream.calls == []
