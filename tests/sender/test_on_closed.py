"""`on_closed`：对话窗口没开时，`fail`（默认）是 `rejected`，`skip` 是 `skipped`。规格：`hub-send`
「发消息接口」「检查顺序」「原因代码和原文」。`skipped` 不扣条数、没有发送失败的记录；"不进告警汇总"
归阶段 D 的告警。
"""

from __future__ import annotations

import pytest

from clawbot_hub.config import Config
from clawbot_hub.sender import Origin
from clawbot_hub.upstream.base import Rejected
from tests.sender.conftest import long_text
from tests.support.hub import tweak


def window_never_opened(hub):
    hub.bind()
    return hub.add("alpha", guaranteed=6, cap=8), "never_contacted"


def window_expired(hub):
    a = hub.ready()
    hub.clock.advance(25 * 3600)
    return a, "window_closed"


CLOSED = pytest.mark.parametrize("closed", [window_never_opened, window_expired])


@CLOSED
def test_the_default_is_to_reject(hub, closed):
    a, code = closed(hub)

    result = hub.sender.push(a, "你好")

    assert (result.status, result.reason_code) == ("rejected", code)


@CLOSED
def test_skip_turns_the_rejection_into_a_skip(hub, closed):
    a, code = closed(hub)

    result = hub.sender.push(a, "你好", on_closed="skip")

    assert (result.status, result.reason_code) == ("skipped", code)
    assert result.parts_sent == 0
    assert hub.upstream.calls == []


@CLOSED
def test_fail_is_the_same_as_the_default(hub, closed):
    a, code = closed(hub)

    result = hub.sender.push(a, "你好", on_closed="fail")

    assert (result.status, result.reason_code) == ("rejected", code)


@CLOSED
def test_a_skip_takes_no_quota_and_leaves_no_failure_event(hub, closed):
    a, code = closed(hub)

    result = hub.sender.push(a, "你好", on_closed="skip")

    assert hub.used() == {}
    assert hub.events("send") == []
    row = hub.row(result.id)
    assert (row["status"], row["reason_code"], row["parts_sent"]) == ("skipped", code, 0)


def test_a_skip_gives_the_same_reason_text_as_a_rejection(hub):
    a, _ = window_expired(hub)

    refused = hub.sender.push(a, "你好")
    skipped = hub.sender.push(a, "你好", on_closed="skip")

    assert skipped.reason == refused.reason


def test_a_rejection_is_an_info_event_but_a_skip_is_none(hub):
    a, _ = window_expired(hub)

    hub.sender.push(a, "你好", on_closed="skip")
    assert hub.events("send") == []
    hub.sender.push(a, "你好")

    (event,) = hub.events("send")
    assert event["level"] == "INFO"


def test_on_closed_does_nothing_when_the_window_is_open(hub):
    a = hub.ready()

    result = hub.sender.push(a, "你好", on_closed="skip")

    assert result.status == "sent"


# ---------------------------------------------------------------- 只管"窗口没开"


def test_skip_does_not_soften_a_quota_rejection(hub):
    a = hub.ready()
    hub.fill(10)

    result = hub.sender.push(a, "你好", on_closed="skip")

    assert (result.status, result.reason_code) == ("rejected", "quota_exhausted")


def test_skip_does_not_soften_a_cap_rejection(hub):
    a = hub.ready(guaranteed=1, cap=1)
    hub.sender.push(a, "一")

    result = hub.sender.push(a, "二", on_closed="skip")

    assert (result.status, result.reason_code) == ("rejected", "project_cap")


def test_skip_does_not_soften_too_long(make_hub):
    hub = make_hub(tweak(Config(), send={"max_chars": 20000}))
    a = hub.ready()

    result = hub.sender.push(a, long_text(9), on_closed="skip")

    assert (result.status, result.reason_code) == ("rejected", "too_long")


def test_skip_does_not_soften_the_pause(hub):
    a = hub.ready()
    hub.upstream.script("send_text", Rejected(200, 40001, "bad token"))
    hub.sender.push(a, "第一条")

    result = hub.sender.push(a, "第二条", on_closed="skip")

    assert (result.status, result.reason_code) == ("rejected", "upstream_blocked")


def test_skip_does_not_soften_a_login_failure(hub):
    a = hub.ready()
    hub.expire()
    hub.clock.advance(25 * 3600)

    result = hub.sender.push(a, "你好", on_closed="skip")

    assert (result.status, result.reason_code) == ("failed", "login_expired")


# ---------------------------------------------------------------- 回复没有 on_closed


def test_a_reply_has_no_such_choice(hub):
    hub.ready()

    with pytest.raises(TypeError):
        hub.sender.reply(Origin("_hub", "服务"), "你好", context_token="ctx-1", on_closed="skip")
