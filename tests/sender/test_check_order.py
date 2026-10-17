"""发送前的检查顺序：第一个不通过的就是结果，后面的不再看；被拒的请求不碰腾讯、不占条数。
规格：`hub-send`「发送前的检查顺序」「拆条」最后几个场景。
"""

from __future__ import annotations

import re

import pytest

from clawbot_hub.config import Config
from clawbot_hub.sender import Origin
from clawbot_hub.upstream.base import Rejected
from tests.sender.conftest import long_text
from tests.support.hub import tweak
from tests.support.threads import run_in_thread

HUB = Origin("_hub", "服务")


def nothing_sent_or_spent(hub) -> None:
    assert hub.upstream.calls == []
    assert hub.used() == {}


# ---------------------------------------------------------------- 登录先于窗口


@pytest.mark.parametrize(
    ("damage", "code"),
    [("expire", "login_expired"), ("unbind", "login_missing"), ("corrupt", "login_broken")],
)
def test_login_is_checked_before_the_window_and_on_closed_does_not_matter(hub, damage, code):
    a = hub.ready()
    getattr(hub, damage)()
    hub.clock.advance(25 * 3600)  # 窗口也过期了

    result = hub.sender.push(a, "你好", on_closed="skip")

    assert (result.status, result.reason_code) == ("failed", code)
    nothing_sent_or_spent(hub)


def test_credentials_the_monitor_has_not_read_yet_count_as_missing(hub):
    hub.speak()
    a = hub.add("alpha", guaranteed=6, cap=8)
    with hub.db.txn() as conn:  # 库里记着登录正常，但监视器还没读过凭证文件
        conn.execute("UPDATE account_state SET login_state = 'ok' WHERE id = 1")
    assert hub.monitor.account() is None

    result = hub.sender.push(a, "你好")

    assert (result.status, result.reason_code) == ("failed", "login_missing")
    nothing_sent_or_spent(hub)


def test_login_is_checked_before_the_quota(hub):
    a = hub.ready()
    hub.fill(10)  # 额度也用完了
    hub.expire()

    result = hub.sender.push(a, "你好")

    assert (result.status, result.reason_code) == ("failed", "login_expired")
    assert hub.upstream.calls == []


def test_a_reply_meets_the_login_check_too(hub):
    hub.ready()
    hub.expire()

    result = hub.sender.reply(HUB, "你好", context_token="ctx-1")

    assert (result.status, result.reason_code) == ("failed", "login_expired")
    assert hub.upstream.calls == []


# ---------------------------------------------------------------- 窗口、暂停、额度


def test_a_user_who_never_spoke_has_no_window(hub):
    hub.bind()
    a = hub.add("alpha", guaranteed=6, cap=8)

    result = hub.sender.push(a, "你好")

    assert (result.status, result.reason_code) == ("rejected", "never_contacted")
    nothing_sent_or_spent(hub)


def test_a_closed_window_rejects_unless_the_project_chose_to_skip(hub):
    a = hub.ready()
    hub.clock.advance(25 * 3600)

    refused = hub.sender.push(a, "你好")
    skipped = hub.sender.push(a, "你好", on_closed="skip")

    assert (refused.status, refused.reason_code) == ("rejected", "window_closed")
    assert (skipped.status, skipped.reason_code) == ("skipped", "window_closed")
    nothing_sent_or_spent(hub)


def test_the_window_is_checked_before_the_pause(hub):
    a = hub.ready()
    hub.upstream.script("send_text", Rejected(200, 40001, "bad token"))
    hub.sender.push(a, "第一条")  # 腾讯拒绝，窗口进入暂停
    hub.clock.advance(25 * 3600)
    hub.upstream.clear_calls()

    result = hub.sender.push(a, "第二条")

    assert (result.status, result.reason_code) == ("rejected", "window_closed")
    assert hub.upstream.calls == []


def test_the_pause_is_checked_before_the_quota(hub):
    a = hub.ready()
    hub.upstream.script("send_text", Rejected(200, 40001, "bad token"))
    hub.sender.push(a, "第一条")  # 腾讯拒绝，窗口进入暂停
    hub.fill(10)  # 窗口里的条数同时用完了
    hub.upstream.clear_calls()

    result = hub.sender.push(a, "第二条")

    assert (result.status, result.reason_code) == ("rejected", "upstream_blocked")
    assert hub.upstream.calls == []


def test_the_quota_is_the_last_check(hub):
    a = hub.ready()
    hub.fill(10)

    result = hub.sender.push(a, "你好")

    assert (result.status, result.reason_code) == ("rejected", "quota_exhausted")
    assert hub.upstream.calls == []


# ---------------------------------------------------------------- 去重先于其余


def test_a_dedupe_hit_is_answered_before_the_login_window_and_quota_are_looked_at(make_hub):
    hub = make_hub(tweak(Config(), send={"dedupe_hours": 48}))  # 键比窗口活得久
    a = hub.ready()
    first = hub.sender.push(a, "你好", dedupe_key="brief")
    hub.fill(9)  # 额度用完
    hub.expire()
    hub.clock.advance(25 * 3600)  # 窗口过期
    hub.upstream.clear_calls()

    again = hub.sender.push(a, "你好", dedupe_key="brief", on_closed="skip")

    assert (again.status, again.reason_code, again.duplicate) == ("sent", None, True)
    assert again.id == first.id
    assert hub.upstream.calls == []


# ---------------------------------------------------------------- 条数太多先于登录


def test_too_long_is_decided_before_the_login_check(make_hub):
    hub = make_hub(tweak(Config(), send={"max_chars": 30000}))
    a = hub.ready()
    hub.expire()

    result = hub.sender.push(a, "a" * 25000)

    assert (result.status, result.reason_code) == ("rejected", "too_long")
    match = re.fullmatch(r"这条要拆成 (\d+) 条，超过这个窗口最多能发的 8 条", result.reason)
    assert match and int(match[1]) > 10
    assert result.parts == int(match[1])
    nothing_sent_or_spent(hub)


def test_too_long_is_decided_before_the_window(make_hub):
    hub = make_hub(tweak(Config(), send={"max_chars": 30000}))
    a = hub.ready()
    hub.clock.advance(25 * 3600)

    result = hub.sender.push(a, "a" * 25000, on_closed="skip")

    assert (result.status, result.reason_code) == ("rejected", "too_long")
    nothing_sent_or_spent(hub)


def test_a_push_can_be_split_into_at_most_the_window_minus_the_reply_reserve(make_hub):
    hub = make_hub(tweak(Config(), send={"max_chars": 30000}))
    a = hub.ready()

    eight = hub.sender.push(a, long_text(8))
    nine = hub.sender.push(a, long_text(9))

    assert (eight.status, eight.parts) == ("sent", 8)
    assert (nine.status, nine.reason_code, nine.parts) == ("rejected", "too_long", 9)
    assert nine.reason == "这条要拆成 9 条，超过这个窗口最多能发的 8 条"


def test_a_reply_can_use_the_reserved_parts_too(make_hub):
    hub = make_hub(tweak(Config(), send={"max_chars": 30000}))
    hub.ready()

    nine = hub.sender.reply(HUB, long_text(9), context_token="ctx-1")

    assert (nine.status, nine.parts, nine.parts_sent) == ("sent", 9, 9)
    assert len(hub.upstream.sent) == 9
    assert hub.used() == {("_hub", "reply"): 9}


def test_a_reply_of_more_than_the_whole_window_is_too_long(make_hub):
    hub = make_hub(tweak(Config(), send={"max_chars": 30000}))
    hub.ready()

    eleven = hub.sender.reply(HUB, long_text(11), context_token="ctx-1")

    assert (eleven.status, eleven.reason_code) == ("rejected", "too_long")
    assert eleven.reason == "这条要拆成 11 条，超过这个窗口最多能发的 10 条"
    nothing_sent_or_spent(hub)


def test_the_most_parts_follow_the_configured_window(make_hub):
    cfg = tweak(Config(), send={"max_chars": 30000}, quota={"window_limit": 6, "reply_reserve": 1})
    hub = make_hub(cfg)
    hub.bind()
    hub.speak()
    a = hub.add("alpha", guaranteed=4, cap=5)

    push = hub.sender.push(a, long_text(6, config=cfg))
    reply = hub.sender.reply(HUB, long_text(6, config=cfg), context_token="ctx-1")

    assert (push.status, push.reason_code) == ("rejected", "too_long")
    assert push.reason == "这条要拆成 6 条，超过这个窗口最多能发的 5 条"
    assert (reply.status, reply.parts_sent) == ("sent", 6)


# ---------------------------------------------------------------- 被拒的请求留下什么


def test_a_refusal_leaves_a_finished_record_and_one_event_but_no_text(hub):
    a = hub.ready()
    hub.clock.advance(25 * 3600)

    result = hub.sender.push(a, "不要出现在记录里的正文")

    row = hub.row(result.id)
    assert (row["status"], row["reason_code"]) == ("rejected", "window_closed")
    assert row["reason"] == result.reason
    assert (row["parts"], row["parts_sent"], row["chars"]) == (1, 0, 11)
    assert row["finished_ms"] == hub.clock.now_ms()
    assert row["window_id"] is not None  # 过期的窗口也是窗口
    (event,) = hub.events("send")
    assert (event["level"], event["project"]) == ("INFO", "alpha")
    assert result.id in event["detail"] and "窗口" in event["detail"]
    assert "不要出现在记录里的正文" not in event["detail"]


def test_a_refusal_before_anyone_spoke_has_no_window_to_point_at(hub):
    hub.bind()
    a = hub.add("alpha", guaranteed=6, cap=8)

    result = hub.sender.push(a, "你好")

    assert hub.row(result.id)["window_id"] is None


def test_a_failure_is_a_warning_and_a_skip_leaves_no_event(hub):
    a = hub.ready()
    hub.expire()
    failed = hub.sender.push(a, "你好")
    hub.clock.advance(25 * 3600)
    hub.unbind()

    assert hub.row(failed.id)["status"] == "failed"
    (event,) = hub.events("send")
    assert event["level"] == "WARNING"


# ---------------------------------------------------------------- 不排队


def test_a_refused_request_does_not_wait_behind_the_one_that_is_sending(make_hub):
    hub = make_hub(tweak(Config(), send={"max_chars": 20000}))
    a = hub.ready()
    gate = hub.upstream.hold("send_text")
    sending = run_in_thread(hub.sender.push, a, "在发的这条")
    assert gate.entered.wait(5)

    refused = hub.sender.push(a, long_text(9))  # 立刻拒绝，不用等发送锁

    assert (refused.status, refused.reason_code) == ("rejected", "too_long")
    assert len(hub.upstream.calls) == 1  # 只有那条在发的
    gate.release()
    assert sending.result().status == "sent"
