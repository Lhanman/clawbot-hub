"""去重键：同一个项目、同一个键在 `send.dedupe_hours` 小时内只发一次。
规格：`hub-send`「去重键」。
"""

from __future__ import annotations

import re

import pytest

from clawbot_hub.config import Config
from clawbot_hub.sender import dedupe_digest
from clawbot_hub.upstream.base import NoResponse, Rejected, Transient
from tests.sender.conftest import long_text
from tests.support.fake_upstream import DEFAULT, Gate
from tests.support.hub import tweak
from tests.support.threads import run_in_thread

KEY = "brief-2026-10-12"


def stuck_first_send(hub, project, **kwargs):
    """第一次发送卡在腾讯那里（假上游的门口）：返回 (门, 第一次发送的线程)。"""
    gate = hub.upstream.hold("send_text")
    first = run_in_thread(hub.sender.push, project, "你好", dedupe_key=KEY, **kwargs)
    assert gate.entered.wait(5)
    return gate, first


# ---------------------------------------------------------------- 命中


def test_a_rerun_returns_the_first_result_and_sends_nothing(hub):
    a = hub.ready()
    first = hub.sender.push(a, "简报", dedupe_key=KEY)

    again = hub.sender.push(a, "简报", dedupe_key=KEY)

    assert (first.status, first.duplicate) == ("sent", False)
    assert (again.status, again.duplicate) == ("sent", True)
    assert (again.id, again.parts, again.parts_sent) == (first.id, 1, 1)
    assert (again.reason_code, again.reason) == (None, None)
    assert len(hub.upstream.sent) == 1
    assert hub.used() == {("alpha", "push"): 1}  # 额度只扣了一次
    assert len(hub.rows()) == 1  # 没有新的发送记录
    assert hub.events("send") == []


def test_the_key_decides_not_the_text(hub):
    a = hub.ready()
    hub.sender.push(a, "上午的简报", dedupe_key=KEY)

    again = hub.sender.push(a, "完全不同的另一段话", dedupe_key=KEY)

    assert again.duplicate
    assert hub.texts == ["上午的简报"]


def test_a_message_without_a_key_is_never_a_duplicate(hub):
    a = hub.ready()

    hub.sender.push(a, "一")
    hub.sender.push(a, "一")

    assert hub.texts == ["一", "一"]


def test_different_keys_are_independent(hub):
    a = hub.ready()

    first = hub.sender.push(a, "一", dedupe_key="k1")
    second = hub.sender.push(a, "二", dedupe_key="k2")

    assert (first.duplicate, second.duplicate) == (False, False)
    assert hub.texts == ["一", "二"]


def test_the_key_is_case_sensitive(hub):
    a = hub.ready()

    hub.sender.push(a, "一", dedupe_key="Daily")
    other = hub.sender.push(a, "二", dedupe_key="daily")

    assert other.duplicate is False
    assert hub.texts == ["一", "二"]


def test_two_projects_may_use_the_same_key(hub):
    a = hub.ready()
    b = hub.add("beta", guaranteed=1, cap=2)

    first = hub.sender.push(a, "A 的日报", dedupe_key="daily")
    second = hub.sender.push(b, "B 的日报", dedupe_key="daily")
    again = hub.sender.push(b, "B 的日报", dedupe_key="daily")

    assert (first.duplicate, second.duplicate, again.duplicate) == (False, False, True)
    assert hub.texts == ["A 的日报", "【beta】B 的日报"]


def test_a_replay_is_for_a_long_message_too(hub):
    a = hub.ready()
    first = hub.sender.push(a, long_text(3), dedupe_key=KEY)

    again = hub.sender.push(a, long_text(3), dedupe_key=KEY)

    assert (again.status, again.parts, again.parts_sent) == ("sent", 3, 3)
    assert again.id == first.id
    assert len(hub.upstream.sent) == 3


# ---------------------------------------------------------------- 占着键和释放键


@pytest.mark.parametrize(
    ("text", "script", "status", "code", "parts_sent"),
    [
        ("你好", [], "sent", None, 1),
        ("你好", [NoResponse("读超时", read_timeout=True)], "unconfirmed", "no_response", 1),
        (
            long_text(2),
            [DEFAULT, Rejected(200, 40001, "bad token")],
            "partial",
            "upstream_rejected",
            1,
        ),
        (long_text(3), [DEFAULT, NoResponse("连接被重置")], "partial", "no_response", 2),
    ],
    ids=["sent", "unconfirmed", "partial-rejected", "partial-unconfirmed"],
)
def test_these_results_keep_the_key(hub, text, script, status, code, parts_sent):
    a = hub.ready()
    hub.upstream.script("send_text", *script)
    first = hub.sender.push(a, text, dedupe_key=KEY)
    calls = len(hub.upstream.calls)

    again = hub.sender.push(a, text, dedupe_key=KEY)

    assert (first.status, first.reason_code, first.parts_sent) == (status, code, parts_sent)
    assert (again.status, again.reason_code, again.parts_sent) == (status, code, parts_sent)
    assert (again.reason, again.parts, again.id) == (first.reason, first.parts, first.id)
    assert again.duplicate
    assert len(hub.upstream.calls) == calls  # 没有碰腾讯
    assert len(hub.rows()) == 1


def release_by_window(hub, a):
    first = hub.sender.push(a, "你好", dedupe_key=KEY)
    hub.speak()
    return first


def release_by_skip(hub, a):
    hub.clock.advance(25 * 3600)
    first = hub.sender.push(a, "你好", dedupe_key=KEY, on_closed="skip")
    hub.speak()
    return first


def release_by_login(hub, a):
    hub.expire()
    first = hub.sender.push(a, "你好", dedupe_key=KEY)
    hub.bind(bot_token="tok-bot-secret-2")  # 重新扫码绑定
    hub.speak()
    return first


def release_by_unreachable(hub, a):
    hub.upstream.script("send_text", Transient("连不上"), Transient("连不上"), Transient("连不上"))
    return hub.sender.push(a, "你好", dedupe_key=KEY)


@pytest.mark.parametrize(
    ("make_first", "status", "code"),
    [
        (release_by_skip, "skipped", "window_closed"),
        (release_by_login, "failed", "login_expired"),
        (release_by_unreachable, "failed", "upstream_unreachable"),
    ],
    ids=["skipped", "login-failed", "unreachable"],
)
def test_nothing_delivered_means_the_key_is_free_again(hub, make_first, status, code):
    a = hub.ready()

    first = make_first(hub, a)
    second = hub.sender.push(a, "你好", dedupe_key=KEY)

    assert (first.status, first.reason_code) == (status, code)
    assert (second.status, second.duplicate) == ("sent", False)
    assert second.id != first.id
    assert len(hub.rows()) == 2


def test_a_rejected_first_try_can_be_redone_after_the_user_speaks(hub):
    hub.bind()
    a = hub.add("alpha", guaranteed=6, cap=8)  # 还没人说过话
    first = hub.sender.push(a, "你好", dedupe_key=KEY)
    hub.speak()

    second = hub.sender.push(a, "你好", dedupe_key=KEY)

    assert (first.status, first.reason_code) == ("rejected", "never_contacted")
    assert (second.status, second.duplicate) == ("sent", False)
    assert hub.texts == ["你好"]


def test_a_too_long_rejection_frees_the_key(make_hub):
    hub = make_hub(tweak(Config(), send={"max_chars": 20000}))
    a = hub.ready()
    first = hub.sender.push(a, long_text(9), dedupe_key=KEY)

    second = hub.sender.push(a, "短一点", dedupe_key=KEY)

    assert first.reason_code == "too_long"
    assert (second.status, second.duplicate) == ("sent", False)


# ---------------------------------------------------------------- 到期


def test_the_key_expires_after_the_configured_hours(make_hub):
    hub = make_hub(tweak(Config(), send={"dedupe_hours": 1}))
    a = hub.ready()
    first = hub.sender.push(a, "一", dedupe_key=KEY)

    hub.clock.advance(3599.999)
    still = hub.sender.push(a, "二", dedupe_key=KEY)
    hub.clock.advance(0.001)
    later = hub.sender.push(a, "三", dedupe_key=KEY)

    assert still.duplicate and still.id == first.id
    assert (later.status, later.duplicate) == ("sent", False)
    assert hub.texts == ["一", "三"]
    assert hub.row(first.id)["dedupe_hash"] is None  # 记录留着，键失效了


def test_the_default_is_24_hours(make_hub):
    hub = make_hub(tweak(Config(), quota={"window_hours": 72}))
    a = hub.ready()
    hub.sender.push(a, "一", dedupe_key=KEY)

    hub.clock.advance(24 * 3600 - 1)
    assert hub.sender.push(a, "二", dedupe_key=KEY).duplicate
    hub.clock.advance(1)
    assert not hub.sender.push(a, "三", dedupe_key=KEY).duplicate


def test_the_key_counts_from_when_the_record_was_made(make_hub):
    hub = make_hub(tweak(Config(), send={"dedupe_hours": 1}))
    a = hub.ready()
    gate, first = stuck_first_send(hub, a)
    hub.clock.advance(20)  # 发送在腾讯那里卡了 20 秒
    gate.release()
    first.result()

    hub.clock.advance(3600 - 20 - 0.001)
    assert hub.sender.push(a, "二", dedupe_key=KEY).duplicate
    hub.clock.advance(0.001)
    assert not hub.sender.push(a, "三", dedupe_key=KEY).duplicate


# ---------------------------------------------------------------- 同时来两次


def test_the_second_request_waits_for_the_first_and_gets_its_result(hub):
    a = hub.ready()
    gate, first = stuck_first_send(hub, a)
    second = run_in_thread(hub.sender.push, a, "你好", dedupe_key=KEY)
    hub.clock.wait_until_blocked(1)

    gate.release()

    one, two = first.result(), second.result()
    assert (one.status, one.duplicate) == ("sent", False)
    assert (two.status, two.duplicate) == ("sent", True)
    assert two.id == one.id
    assert len(hub.upstream.sent) == 1
    assert hub.used() == {("alpha", "push"): 1}


def test_another_send_finishing_does_not_end_the_wait(hub):
    a = hub.ready()
    b = hub.add("beta", guaranteed=1, cap=2)
    gate, first = stuck_first_send(hub, a)
    other = run_in_thread(hub.sender.push, b, "别的项目的")  # 也等着发送锁，比第二次早到 10 秒
    hub.clock.wait_until_blocked(1)
    hub.clock.advance(10)
    second = run_in_thread(hub.sender.push, a, "你好", dedupe_key=KEY)
    hub.clock.wait_until_blocked(2)

    hub.clock.advance(20)  # 别的项目那条等够 30 秒收了尾，同样会叫醒等着的人

    assert other.result().reason_code == "busy"
    assert second.still_blocked(0.3)  # 被叫醒了，一看它等的那条还没完，接着等
    gate.release()
    result = second.result()
    assert (result.status, result.duplicate) == ("sent", True)
    assert result.id == first.result().id


def test_the_second_request_gets_a_failure_too_and_does_not_resend(hub):
    a = hub.ready()
    gate = Gate(Transient("连不上"))
    hub.upstream.set_default("send_text", gate)
    first = run_in_thread(hub.sender.push, a, "你好", dedupe_key=KEY)
    assert gate.entered.wait(5)
    second = run_in_thread(hub.sender.push, a, "你好", dedupe_key=KEY)
    hub.clock.wait_until_blocked(1)

    gate.release()

    one, two = first.result(), second.result()
    assert (one.status, one.reason_code, one.duplicate) == ("failed", "upstream_unreachable", False)
    assert (two.status, two.reason_code, two.duplicate) == ("failed", "upstream_unreachable", True)
    assert two.id == one.id
    sent_before = len(hub.upstream.sent)
    hub.upstream.set_default("send_text", None)
    third = hub.sender.push(a, "你好", dedupe_key=KEY)  # 键已经释放：当成新的发送
    assert (third.status, third.duplicate) == ("sent", False)
    assert len(hub.upstream.sent) == sent_before + 1


def test_a_stuck_first_send_makes_the_second_answer_in_flight_after_its_own_budget(hub):
    a = hub.ready()
    gate, first = stuck_first_send(hub, a)
    hub.clock.advance(10)
    start = hub.clock.now_ms()
    second = run_in_thread(hub.sender.push, a, "你好", dedupe_key=KEY)
    hub.clock.wait_until_blocked(1)

    assert hub.clock.pending_deadlines() == [start + 30_000]  # 从它自己收到请求起算
    hub.clock.advance(30)

    result = second.result()
    assert (result.status, result.reason_code, result.duplicate) == (
        "unconfirmed",
        "duplicate_in_flight",
        True,
    )
    assert result.reason == "同一个去重键的上一次发送还没结束，先不要重发"
    assert (result.parts, result.parts_sent) == (1, 0)
    assert len(hub.upstream.calls) == 1
    assert hub.used() == {("alpha", "push"): 1}
    gate.release()
    assert first.result().status == "sent"
    assert hub.sender.push(a, "你好", dedupe_key=KEY).status == "sent"  # 现在能看到真结果了


# ---------------------------------------------------------------- 中断


def test_a_send_killed_midway_keeps_its_key_after_the_restart(hub):
    a = hub.ready()
    with hub.db.txn() as conn:
        conn.execute(
            "INSERT INTO outbox (id, project_id, kind, priority, dedupe_hash, parts, status,"
            " chars, created_ms) VALUES ('o_dead', (SELECT id FROM projects WHERE name = 'alpha'),"
            " 'push', 'normal', ?, 3, 'sending', 20, ?)",
            (dedupe_digest("alpha", KEY), hub.clock.now_ms()),
        )
    with hub.db.txn() as conn:  # 服务重启
        assert hub.quota.interrupt_unfinished(conn) == 1

    result = hub.sender.push(a, "你好", dedupe_key=KEY)

    assert (result.status, result.reason_code, result.duplicate) == (
        "unconfirmed",
        "interrupted",
        True,
    )
    assert result.reason == "上次发送被中断，不知道发出了几条，不要重发"
    assert result.id == "o_dead"
    assert hub.upstream.calls == []


# ---------------------------------------------------------------- 重放带现在的余量


def test_a_replay_carries_the_quota_as_it_is_now(hub):
    a = hub.ready()
    b = hub.add("beta", guaranteed=1, cap=3)
    first = hub.sender.push(a, "一", dedupe_key=KEY)
    hub.sender.push(b, "别的项目发了几条")
    hub.sender.push(b, "再发一条")

    again = hub.sender.push(a, "一", dedupe_key=KEY)

    with hub.db.txn() as conn:
        now = hub.quota.left(conn, "alpha")
    assert again.quota.left == now
    assert again.quota.left != first.quota.left
    assert again.quota.limit == 10
    assert again.status == first.status == "sent"


# ---------------------------------------------------------------- 库里只有摘要


def test_only_a_digest_of_the_key_is_stored(hub):
    a = hub.ready()
    b = hub.add("beta", guaranteed=1, cap=2)
    first = hub.sender.push(a, "一", dedupe_key=KEY)
    second = hub.sender.push(b, "二", dedupe_key=KEY)

    stored = {hub.row(first.id)["dedupe_hash"], hub.row(second.id)["dedupe_hash"]}

    assert len(stored) == 2  # 加项目名当盐：同一个键在两个项目里摘要不同
    assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in stored)
    hub.db.conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    files = [path for path in hub.paths.home.glob("hub.db*") if path.is_file()]
    assert files
    assert all(KEY.encode() not in path.read_bytes() for path in files)
