"""发送的各种结果：`sent` / `unconfirmed` / `partial` / `rejected` / `failed` 各一组，
确认档位、重试、拿不准不重发、明确拒绝不重试、-14 的几种情形、与腾讯完成往返的记录。

规格：`hub-send`「逐条发送，判断送达」「发消息接口」，`hub-login`「登录后服务自动换上新凭证」。
"""

from __future__ import annotations

import dataclasses
import itertools
import os
import re

import pytest

from clawbot_hub.config import Config
from clawbot_hub.loginstate import LOGIN_REASONS, write_scan_marker
from clawbot_hub.sender import Origin
from clawbot_hub.upstream.base import (
    Accepted,
    NoResponse,
    Rejected,
    SessionExpired,
    Transient,
)
from tests.sender.conftest import long_text, stamp
from tests.support.fake_upstream import Gate
from tests.support.hub import BOT_TOKEN, USER, Hub, tweak
from tests.support.threads import run_in_thread

HOUR = 3_600_000
STRICT = {"upstream": {"confirm": "strict"}}
SEND = "send_text"


def last_ok_ms(hub) -> int | None:
    row = hub.db.conn().execute("SELECT last_upstream_ok_ms FROM account_state").fetchone()
    return row["last_upstream_ok_ms"]


# ---------------------------------------------------------------- sent


def test_a_normal_message_is_sent(hub):
    a = hub.ready()
    started = hub.clock.now_ms()

    result = hub.sender.push(a, "盘点完成\n共 3 项")

    assert result.status == "sent"
    assert (result.reason_code, result.reason) == (None, None)
    assert (result.parts, result.parts_sent, result.duplicate) == (1, 1, False)
    assert re.fullmatch(r"o_[0-9a-f]{8}", result.id)
    assert hub.texts == ["盘点完成\n共 3 项"]
    assert hub.used() == {("alpha", "push"): 1}
    row = hub.row(result.id)
    assert (row["status"], row["kind"], row["priority"]) == ("sent", "push", "normal")
    assert (row["parts"], row["parts_sent"], row["parts_unconfirmed"]) == (1, 1, 0)
    assert (row["chars"], row["finished_ms"]) == (len("盘点完成\n共 3 项"), started)


def test_the_result_carries_the_callers_remaining_quota(hub):
    a = hub.ready()
    started = hub.clock.now_ms()

    first = hub.sender.push(a, "第一条")
    second = hub.sender.push(a, "第二条")

    # 只有 a 一个项目：L=10、R=2、保底 6、上限 8；第一条以后 min(9 − 2, 8 − 1, 5 + 2) = 7
    assert (first.quota.left, first.quota.limit) == (7, 10)
    assert first.quota.window_ends_ms == started + 24 * HOUR
    assert second.quota.left == 6


def test_a_long_message_that_went_out_whole_has_no_reason(hub):
    a = hub.ready()

    result = hub.sender.push(a, long_text(3))

    assert (result.status, result.reason_code, result.reason) == ("sent", None, None)
    assert (result.parts, result.parts_sent) == (3, 3)
    row = hub.row(result.id)
    assert (row["reason_code"], row["reason"]) == (None, None)
    assert hub.events("send") == []


def test_the_quota_view_of_a_refusal_in_a_closed_window_is_empty(hub):
    a = hub.ready()
    hub.clock.advance(25 * 3600)

    result = hub.sender.push(a, "你好")

    assert result.reason_code == "window_closed"
    assert (result.quota.left, result.quota.limit, result.quota.window_ends_ms) == (0, 10, None)


def test_the_quota_view_is_empty_while_the_login_is_not_right(hub):
    a = hub.ready()
    started = hub.clock.now_ms()
    hub.expire()

    result = hub.sender.push(a, "你好")

    assert result.reason_code == "login_expired"
    # 窗口还开着，只是现在发不出去：余量是 0，窗口结束的时刻照给
    assert (result.quota.left, result.quota.limit) == (0, 10)
    assert result.quota.window_ends_ms == started + 24 * HOUR


def test_the_quota_view_follows_the_configured_window(make_hub):
    cfg = tweak(Config(), quota={"window_limit": 6, "window_hours": 12, "reply_reserve": 1})
    hub = make_hub(cfg)
    hub.bind()
    hub.speak()
    a = hub.add("alpha", guaranteed=4, cap=5)
    started = hub.clock.now_ms()

    result = hub.sender.push(a, "你好")

    # min(窗口剩 5 − 回复留 1, 上限 5 − 已用 1, 保底还剩 3 + 共享池 1)
    assert (result.quota.left, result.quota.limit) == (4, 6)
    assert result.quota.window_ends_ms == started + 12 * HOUR


def test_an_id_that_is_already_taken_is_not_used_again(make_hub):
    ids = iter(["o_same", "o_same", "o_same", "o_fresh"])
    hub = make_hub(new_id=lambda: next(ids))
    a = hub.ready()

    first = hub.sender.push(a, "一")
    second = hub.sender.push(a, "二")

    assert (first.id, second.id) == ("o_same", "o_fresh")
    assert [row["id"] for row in hub.rows()] == ["o_same", "o_fresh"]


def test_a_source_of_ids_that_only_repeats_is_an_internal_error(make_hub):
    hub = make_hub(new_id=lambda: "o_same")
    a = hub.ready()
    assert hub.sender.push(a, "一").id == "o_same"

    with pytest.raises(RuntimeError, match="发送编号"):
        hub.sender.push(a, "二")

    assert [row["id"] for row in hub.rows()] == ["o_same"]
    assert hub.used() == {("alpha", "push"): 1}


def test_the_sent_text_is_the_original_with_its_whitespace(hub):
    a = hub.ready()

    hub.sender.push(a, "  盘点完成\n共 3 项  ")

    assert hub.texts == ["  盘点完成\n共 3 项  "]


def test_the_recipient_and_credentials_come_from_the_service(hub):
    a = hub.ready()

    hub.sender.push(a, "你好")

    sent = hub.upstream.sent[0]
    assert sent.to_user_id == USER
    assert sent.context_token == "ctx-1"
    assert sent.acct.bot_token == BOT_TOKEN
    assert sent.acct.base_url == "https://ilink.example"
    assert re.fullmatch(r"[0-9a-f-]{36}", sent.client_id)


def test_the_record_keeps_no_text(hub):
    a = hub.ready()

    result = hub.sender.push(a, "独特的正文 ZZ-UNIQUE-77")

    row = dict(hub.row(result.id))
    assert "ZZ-UNIQUE-77" not in repr(row)
    assert set(row) == {
        "id", "project_id", "kind", "priority", "window_id", "dedupe_hash", "parts",
        "parts_sent", "parts_unconfirmed", "status", "reason_code", "reason", "chars",
        "no_message_id", "created_ms", "finished_ms",
    }  # fmt: skip


def test_the_record_remembers_which_window_it_was_charged_to(hub):
    a = hub.ready()
    window = hub.db.conn().execute("SELECT MAX(id) AS id FROM windows").fetchone()["id"]

    result = hub.sender.push(a, "你好")

    assert hub.row(result.id)["window_id"] == window


# ---------------------------------------------------------------- 确认档位


def test_strict_without_a_message_id_is_unconfirmed_and_costs_a_part(make_hub):
    hub = make_hub(tweak(Config(), **STRICT))
    a = hub.ready()
    hub.upstream.script(SEND, Accepted())

    result = hub.sender.push(a, "你好")

    assert result.status == "unconfirmed"
    assert result.reason_code == "no_message_id"
    assert result.reason == "微信 ClawBot 没有返回消息编号，这条可能没有到"
    assert (result.parts, result.parts_sent) == (1, 1)
    assert len(hub.upstream.sent) == 1  # 没有重发
    assert hub.used() == {("alpha", "push"): 1}
    row = hub.row(result.id)
    assert (row["status"], row["parts_unconfirmed"], row["no_message_id"]) == ("unconfirmed", 1, 1)


def test_strict_with_a_message_id_is_sent(make_hub):
    hub = make_hub(tweak(Config(), **STRICT))
    a = hub.ready()

    result = hub.sender.push(a, "你好")

    assert result.status == "sent"
    assert hub.row(result.id)["no_message_id"] == 0


def test_observe_without_a_message_id_is_sent_and_noted(hub):
    a = hub.ready()
    hub.upstream.script(SEND, Accepted())

    result = hub.sender.push(a, "你好")

    assert result.status == "sent"
    assert result.reason_code is None
    assert hub.row(result.id)["no_message_id"] == 1
    assert hub.row(result.id)["parts_unconfirmed"] == 0


def test_off_without_a_message_id_is_sent_and_not_noted(make_hub):
    hub = make_hub(tweak(Config(), upstream={"confirm": "off"}))
    a = hub.ready()
    hub.upstream.script(SEND, Accepted())

    result = hub.sender.push(a, "你好")

    assert result.status == "sent"
    assert hub.row(result.id)["no_message_id"] == 0


def test_every_request_without_an_id_is_counted_not_every_message(hub):
    a = hub.ready()
    hub.upstream.script(SEND, Accepted(), Accepted("m2"), Accepted())

    result = hub.sender.push(a, long_text(3))

    assert result.status == "sent"
    assert hub.row(result.id)["no_message_id"] == 2


def test_strict_keeps_sending_the_next_parts_after_a_missing_id(make_hub):
    hub = make_hub(tweak(Config(), **STRICT))
    a = hub.ready()
    hub.upstream.script(SEND, Accepted("m1"), Accepted(), Accepted("m3"))

    result = hub.sender.push(a, long_text(3))

    assert result.status == "unconfirmed"
    assert result.reason_code == "no_message_id"
    assert result.reason.endswith("；发出了 3 条，共 3 条")
    assert (result.parts, result.parts_sent) == (3, 3)
    assert len(hub.upstream.sent) == 3
    assert hub.used() == {("alpha", "push"): 3}
    row = hub.row(result.id)
    assert (row["parts_sent"], row["parts_unconfirmed"], row["no_message_id"]) == (3, 1, 1)


def seen_while_the_last_part_is_out(hub, project, mode_outcomes):
    """前两条按 `mode_outcomes` 回，第三条卡在腾讯那里：这时候读发送记录，再放行。
    返回 (卡着时读到的记录, 最后的结果)。"""
    gate = Gate()
    hub.upstream.script(SEND, *mode_outcomes, gate)
    sending = run_in_thread(hub.sender.push, project, long_text(3))
    assert gate.entered.wait(5)
    (row,) = hub.rows()
    seen = dict(row)
    gate.release()
    return seen, sending.result()


def test_the_record_counts_the_parts_as_they_are_booked_in_strict_mode(make_hub):
    hub = make_hub(tweak(Config(), **STRICT))
    a = hub.ready()

    seen, result = seen_while_the_last_part_is_out(hub, a, [Accepted("m1"), Accepted()])

    assert seen["status"] == "sending"
    assert (seen["parts_sent"], seen["parts_unconfirmed"], seen["no_message_id"]) == (2, 1, 1)
    assert (result.status, result.parts_sent) == ("unconfirmed", 3)
    row = hub.row(result.id)
    assert (row["parts_sent"], row["parts_unconfirmed"], row["no_message_id"]) == (3, 1, 1)


def test_the_record_counts_the_parts_as_they_are_booked_in_observe_mode(hub):
    a = hub.ready()

    seen, result = seen_while_the_last_part_is_out(hub, a, [Accepted(), Accepted("m2")])

    assert (seen["parts_sent"], seen["parts_unconfirmed"], seen["no_message_id"]) == (2, 0, 1)
    assert result.status == "sent"
    row = hub.row(result.id)
    assert (row["parts_sent"], row["parts_unconfirmed"], row["no_message_id"]) == (3, 0, 1)


def test_the_record_counts_a_part_nobody_answered_as_sent_but_unconfirmed(hub):
    a = hub.ready()
    hub.upstream.script(SEND, Accepted("m1"), NoResponse("读超时", read_timeout=True))

    result = hub.sender.push(a, long_text(3))

    assert (result.status, result.parts_sent) == ("partial", 2)
    row = hub.row(result.id)
    assert (row["parts_sent"], row["parts_unconfirmed"], row["no_message_id"]) == (2, 1, 0)


# ---------------------------------------------------------------- 腾讯明确拒绝


def test_a_clear_rejection_is_failed_and_the_part_is_given_back(hub):
    a = hub.ready()
    hub.upstream.script(SEND, Rejected(200, 40001, "bad token"))

    result = hub.sender.push(a, "你好")

    assert result.status == "failed"
    assert result.reason_code == "upstream_rejected"
    assert result.reason == "微信 ClawBot 拒绝：40001 bad token"
    assert (result.parts, result.parts_sent) == (1, 0)
    assert len(hub.upstream.sent) == 1  # 不重试
    assert hub.used() == {}


def test_a_rejection_with_only_an_http_status(hub):
    a = hub.ready()
    hub.upstream.script(SEND, Rejected(403, None))

    result = hub.sender.push(a, "你好")

    assert (result.status, result.reason_code) == ("failed", "upstream_rejected")
    assert result.reason == "微信 ClawBot 拒绝：HTTP 403"
    assert len(hub.upstream.sent) == 1


def test_secrets_in_the_upstreams_words_never_reach_the_project(hub):
    a = hub.ready()
    leak = f"bad {BOT_TOKEN} and ctx-1 and chub_{'A' * 43}"
    hub.upstream.script(SEND, Rejected(200, 40001, leak))

    result = hub.sender.push(a, "你好")

    assert BOT_TOKEN not in result.reason and "ctx-1" not in result.reason
    assert "chub_" not in result.reason
    assert result.reason == "微信 ClawBot 拒绝：40001 bad *** and *** and ***"
    assert BOT_TOKEN not in repr(dict(hub.row(result.id)))
    assert BOT_TOKEN not in repr(hub.events())


# ---------------------------------------------------------------- 重试


def test_a_server_error_is_retried_with_the_same_client_id(hub, clock):
    a = hub.ready()
    times: list[int] = []
    busy = Transient("HTTP 503")
    hub.upstream.script(SEND, stamp(clock, times, busy), stamp(clock, times, busy))
    hub.upstream.script(SEND, stamp(clock, times))

    result = hub.sender.push(a, "你好")

    assert result.status == "sent"
    sent = hub.upstream.sent
    assert len(sent) == 3
    assert len({message.client_id for message in sent}) == 1
    assert [times[1] - times[0], times[2] - times[1]] == [1000, 2000]
    assert hub.used() == {("alpha", "push"): 1}


def test_retries_used_up_means_the_part_was_not_sent(hub):
    a = hub.ready()
    hub.upstream.set_default(SEND, Transient("连不上"))

    result = hub.sender.push(a, "你好")

    assert result.status == "failed"
    assert result.reason_code == "upstream_unreachable"
    assert result.reason == "微信 ClawBot 发送失败，重试 2 次后仍然失败：连不上"
    assert (result.parts, result.parts_sent) == (1, 0)
    assert len(hub.upstream.sent) == 3
    assert len({message.client_id for message in hub.upstream.sent}) == 1
    assert hub.used() == {}


@pytest.mark.parametrize(("retries", "attempts"), [(0, 1), (1, 2), (3, 4)])
def test_the_number_of_retries_is_configurable(make_hub, retries, attempts):
    hub = make_hub(tweak(Config(), send={"retries": retries}))
    a = hub.ready()
    hub.upstream.set_default(SEND, Transient("连不上"))

    result = hub.sender.push(a, "你好")

    assert len(hub.upstream.sent) == attempts
    assert f"重试 {retries} 次后仍然失败" in result.reason


def test_the_waits_between_retries_double(make_hub, clock):
    hub = make_hub(tweak(Config(), send={"retries": 4, "timeout_seconds": 100}))
    a = hub.ready()
    times: list[int] = []
    hub.upstream.set_default(SEND, stamp(clock, times, Transient("连不上")))

    hub.sender.push(a, "你好")

    assert [later - earlier for earlier, later in itertools.pairwise(times)] == [
        1000,
        2000,
        4000,
        8000,
    ]


def test_the_transient_reason_is_masked_too(hub):
    a = hub.ready()
    hub.upstream.set_default(SEND, Transient(f"连不上 https://x/?t={BOT_TOKEN}"))

    result = hub.sender.push(a, "你好")

    assert BOT_TOKEN not in result.reason
    assert result.reason.endswith("连不上 https://x/?t=***")


@pytest.mark.parametrize(
    "outcome",
    [Rejected(200, 40001, "x"), NoResponse("读超时", read_timeout=True), SessionExpired()],
    ids=["clear rejection", "no response", "session expired"],
)
def test_only_a_request_that_was_not_sent_is_retried(hub, outcome):
    a = hub.ready()
    hub.upstream.set_default(SEND, outcome)

    hub.sender.push(a, "你好")

    assert len(hub.upstream.sent) == 1


# ---------------------------------------------------------------- 发出去了不知道到没到


def test_no_response_is_unconfirmed_and_never_resent(hub):
    a = hub.ready()
    hub.upstream.script(SEND, NoResponse("读超时", read_timeout=True))

    result = hub.sender.push(a, "你好")

    assert result.status == "unconfirmed"
    assert result.reason_code == "no_response"
    assert result.reason == "微信 ClawBot 没有回话，这条可能已经到了，不要重发"
    assert (result.parts, result.parts_sent) == (1, 1)
    assert len(hub.upstream.sent) == 1
    assert hub.used() == {("alpha", "push"): 1}  # 可能已经到了：条数算已用


def test_a_connection_reset_is_the_same(hub):
    a = hub.ready()
    hub.upstream.script(SEND, NoResponse("连接被重置"))

    result = hub.sender.push(a, "你好")

    assert (result.status, result.reason_code) == ("unconfirmed", "no_response")


# ---------------------------------------------------------------- 拆成几条


def test_a_failure_in_the_middle_leaves_a_partial_result(hub):
    a = hub.ready()
    hub.upstream.script(SEND, Accepted("m1"), Rejected(200, 40001, "bad token"))

    result = hub.sender.push(a, long_text(3))

    assert result.status == "partial"
    assert (result.parts, result.parts_sent) == (3, 1)
    assert result.reason_code == "upstream_rejected"
    assert result.reason == "微信 ClawBot 拒绝：40001 bad token；发出了 1 条，共 3 条"
    assert len(hub.upstream.sent) == 2  # 第 3 条没有发
    assert hub.used() == {("alpha", "push"): 1}  # 第 2、3 条退回
    row = hub.row(result.id)
    assert (row["status"], row["parts_sent"]) == ("partial", 1)


def test_after_an_unconfirmed_part_the_rest_is_not_sent(hub):
    a = hub.ready()
    hub.upstream.script(SEND, Accepted("m1"), NoResponse("读超时", read_timeout=True))

    result = hub.sender.push(a, long_text(3))

    assert result.status == "partial"
    assert (result.parts, result.parts_sent) == (3, 2)
    assert result.reason_code == "no_response"
    assert result.reason.endswith("；发出了 2 条，共 3 条")
    assert len(hub.upstream.sent) == 2
    assert hub.used() == {("alpha", "push"): 2}  # 第 1、2 条保持已用，第 3 条退回
    assert hub.row(result.id)["parts_unconfirmed"] == 1


def test_a_part_whose_retries_ran_out_stops_the_rest(hub):
    a = hub.ready()
    hub.upstream.script(SEND, Accepted("m1"), Transient("连不上"), Transient("连不上"))
    hub.upstream.script(SEND, Transient("连不上"))

    result = hub.sender.push(a, long_text(3))

    assert result.status == "partial"
    assert (result.parts, result.parts_sent) == (3, 1)
    assert result.reason_code == "upstream_unreachable"
    assert result.reason.endswith("；发出了 1 条，共 3 条")
    assert len(hub.upstream.sent) == 4  # 第 1 条 1 次，第 2 条 3 次，第 3 条没有试
    assert hub.used() == {("alpha", "push"): 1}


def test_the_first_part_failing_is_a_failure_not_a_partial(hub):
    a = hub.ready()
    hub.upstream.script(SEND, Rejected(200, 40001, "bad token"))

    result = hub.sender.push(a, long_text(3))

    assert result.status == "failed"
    assert (result.parts, result.parts_sent) == (3, 0)
    assert result.reason == "微信 ClawBot 拒绝：40001 bad token；发出了 0 条，共 3 条"
    assert len(hub.upstream.sent) == 1
    assert hub.used() == {}


def test_every_part_has_its_own_client_id_and_the_same_credentials(hub):
    a = hub.ready()

    hub.sender.push(a, long_text(3))

    sent = hub.upstream.sent
    assert len({message.client_id for message in sent}) == 3
    assert {message.context_token for message in sent} == {"ctx-1"}
    assert {message.to_user_id for message in sent} == {USER}


def test_a_retried_part_keeps_its_id_and_the_next_part_gets_a_new_one(hub):
    a = hub.ready()
    hub.upstream.script(SEND, Transient("HTTP 503"), Accepted("m1"), Accepted("m2"))

    result = hub.sender.push(a, long_text(2))

    assert result.status == "sent"
    first, again, second = hub.upstream.sent
    assert first.client_id == again.client_id != second.client_id


def test_the_client_ids_come_from_the_injected_source(make_hub):
    ids = iter(["c-1", "c-2"])
    hub = make_hub(new_client_id=lambda: next(ids))
    a = hub.ready()

    hub.sender.push(a, long_text(2))

    assert [message.client_id for message in hub.upstream.sent] == ["c-1", "c-2"]


# ---------------------------------------------------------------- -14


def test_a_session_expiry_while_sending_marks_the_login_expired(hub):
    a = hub.ready()
    hub.upstream.script(SEND, Accepted("m1"), SessionExpired())

    result = hub.sender.push(a, long_text(3))

    expired = LOGIN_REASONS["expired"]
    assert result.status == "partial"
    assert (result.parts, result.parts_sent) == (3, 1)
    assert result.reason_code == "login_expired"
    assert result.reason == f"{expired.text}；发出了 1 条，共 3 条"
    assert len(hub.upstream.sent) == 2  # 不重试，后面的不发
    assert hub.used() == {("alpha", "push"): 1}
    assert hub.monitor.login().state == "expired"
    assert hub.expired == [1]


def test_after_the_expiry_the_next_message_does_not_touch_the_upstream(hub):
    a = hub.ready()
    hub.upstream.script(SEND, SessionExpired())
    first = hub.sender.push(a, "你好")
    calls = len(hub.upstream.calls)

    again = hub.sender.push(a, "再来一条")

    assert first.status == again.status == "failed"
    assert first.reason_code == again.reason_code == "login_expired"
    assert again.reason == LOGIN_REASONS["expired"].text
    assert len(hub.upstream.calls) == calls
    assert hub.expired == [1]  # 同一次失效只报一次


def test_a_session_expiry_just_after_a_rebind_is_not_an_expiry(hub):
    hub.bind(aged=False)
    hub.speak()
    a = hub.add("alpha", guaranteed=6, cap=8)
    hub.upstream.script(SEND, SessionExpired())

    result = hub.sender.push(a, "你好")

    assert (result.status, result.reason_code) == ("failed", "login_expired")
    assert len(hub.upstream.sent) == 1  # 不重试
    assert hub.monitor.login().state == "ok"
    assert hub.expired == []
    assert hub.used() == {}


def test_a_session_expiry_during_a_scan_is_not_an_expiry(hub):
    a = hub.ready()
    write_scan_marker(hub.paths, pid=os.getpid(), started_ms=hub.clock.now_ms())
    hub.upstream.script(SEND, SessionExpired())

    result = hub.sender.push(a, "你好")

    assert (result.status, result.reason_code) == ("failed", "login_expired")
    assert hub.monitor.login().state == "ok"
    assert hub.expired == []


def test_a_session_expiry_after_the_credentials_file_changed_is_not_an_expiry(hub):
    a = hub.ready()
    old = hub.store.read()
    hub.store.write(dataclasses.replace(old, bot_token="tok-new-2"))  # 监视器下一轮才会换上
    hub.upstream.script(SEND, SessionExpired())

    result = hub.sender.push(a, "你好")

    assert (result.status, result.reason_code) == ("failed", "login_expired")
    assert hub.monitor.login().state == "ok"
    assert hub.expired == []


def test_a_session_expiry_in_a_reply_is_the_same(hub):
    hub.ready()
    hub.upstream.script(SEND, SessionExpired())

    result = hub.sender.reply(Origin("_hub", "服务"), "收到", context_token="ctx-9")

    assert (result.status, result.reason_code) == ("failed", "login_expired")
    assert hub.monitor.login().state == "expired"


# ---------------------------------------------------------------- 与腾讯完成往返


def test_a_completed_round_trip_is_noted(hub, clock):
    a = hub.ready()
    assert last_ok_ms(hub) is None

    hub.sender.push(a, "你好")

    assert last_ok_ms(hub) == clock.now_ms()


@pytest.mark.parametrize(
    "outcome",
    [Rejected(403, None), SessionExpired(), Accepted()],
    ids=["rejected", "session expired", "accepted without id"],
)
def test_a_complete_answer_counts_even_when_it_says_no(hub, outcome):
    a = hub.ready()
    hub.upstream.script(SEND, outcome)

    hub.sender.push(a, "你好")

    assert last_ok_ms(hub) == hub.clock.now_ms()


def test_a_strict_answer_without_an_id_is_a_round_trip_too(make_hub):
    hub = make_hub(tweak(Config(), **STRICT))
    a = hub.ready()
    hub.upstream.script(SEND, Accepted())

    result = hub.sender.push(a, "你好")

    assert result.status == "unconfirmed"
    assert last_ok_ms(hub) == hub.clock.now_ms()


@pytest.mark.parametrize(
    "outcome",
    [Transient("HTTP 503"), NoResponse("读超时", read_timeout=True)],
    ids=["transient", "no response"],
)
def test_no_complete_answer_is_not_a_round_trip(hub, outcome):
    a = hub.ready()
    hub.upstream.set_default(SEND, outcome)

    hub.sender.push(a, "你好")

    assert last_ok_ms(hub) is None


def test_a_rejected_by_rule_request_is_not_a_round_trip(hub):
    a = hub.ready()
    hub.clock.advance(25 * 3600)

    result = hub.sender.push(a, "你好")

    assert result.status == "rejected"
    assert last_ok_ms(hub) is None


def test_an_unexpected_kind_of_result_is_an_internal_error(paths, database, clock):
    class Odd:
        def send_text(self, *args, **kwargs):
            return "不是结果"

    odd = Hub(paths, database, clock, Odd())
    a = odd.ready()

    with pytest.raises(TypeError, match="不认识的发送结果"):
        odd.sender.push(a, "你好")
