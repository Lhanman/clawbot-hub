"""回复的发送层：直接给对话凭证（不经句柄）。规格：`hub-send`「回复」，`hub-quota` 的回复规则。
（句柄、有效期、次数上限、等确认的标记归阶段 C。）
"""

from __future__ import annotations

import pytest

from clawbot_hub.config import Config
from clawbot_hub.sender import Origin, SendInvalid
from clawbot_hub.upstream.base import Accepted, Rejected, Transient
from tests.sender.conftest import long_text
from tests.support.hub import tweak

HUB = Origin("_hub", "服务")


# ---------------------------------------------------------------- 凭证


def test_a_reply_uses_the_credential_it_was_given_not_the_latest_one(hub):
    hub.ready()  # 第一句话，凭证 ctx-1
    hub.speak()  # 第二句话，凭证 ctx-2：最近的窗口用它

    result = hub.sender.reply(HUB, long_text(3), context_token="ctx-1")

    assert (result.status, result.parts_sent) == ("sent", 3)
    assert {m.context_token for m in hub.upstream.sent} == {"ctx-1"}


def test_a_push_uses_the_latest_credential(hub):
    a = hub.ready()
    hub.speak()

    hub.sender.push(a, "你好")

    assert [m.context_token for m in hub.upstream.sent] == ["ctx-2"]


def test_a_reply_goes_to_the_owner_with_the_stored_credentials(hub):
    hub.ready()

    hub.sender.reply(HUB, "你好", context_token="ctx-1")

    (sent,) = hub.upstream.sent
    assert sent.to_user_id == "owner@im.wechat"
    assert sent.acct.bot_token == "tok-bot-secret-1"


def test_a_reply_whose_credential_is_old_is_still_sent_while_the_window_is_open(hub):
    hub.ready()
    hub.clock.advance(3600)
    hub.speak()
    hub.clock.advance(23 * 3600)  # 第一个窗口早已过期，最近的窗口还开着

    result = hub.sender.reply(HUB, "你好", context_token="ctx-1")

    assert result.status == "sent"


# ---------------------------------------------------------------- 记账


def test_the_parts_of_a_reply_are_counted_as_replies(hub):
    hub.ready()

    result = hub.sender.reply(HUB, long_text(3), context_token="ctx-1")

    assert result.parts == 3
    assert hub.used() == {("_hub", "reply"): 3}
    assert hub.row(result.id)["kind"] == "reply"
    assert hub.row(result.id)["priority"] is None
    assert hub.row(result.id)["dedupe_hash"] is None


def test_a_project_reply_is_counted_under_the_project(hub):
    hub.ready()
    b = hub.add("beta", guaranteed=1, cap=2)

    hub.sender.reply(b, "收到", context_token="ctx-1")

    assert hub.used() == {("beta", "reply"): 1}
    assert hub.texts == ["【beta】收到"]


def test_a_reply_is_not_held_back_by_caps_or_the_reserve(hub):
    a = hub.ready()
    hub.fill(8, project="alpha", kind="push")  # 推送已经用到 L − R

    first = hub.sender.reply(HUB, "一", context_token="ctx-1")
    second = hub.sender.reply(HUB, "二", context_token="ctx-1")
    third = hub.sender.reply(HUB, "三", context_token="ctx-1")
    push = hub.sender.push(a, "四")

    assert (first.status, second.status) == ("sent", "sent")
    assert (third.status, third.reason_code) == ("rejected", "quota_exhausted")
    assert (push.status, push.reason_code) == ("rejected", "quota_exhausted")


def test_a_reply_that_does_not_fit_the_window_is_rejected_as_a_whole(hub):
    hub.ready()
    hub.fill(8)

    result = hub.sender.reply(HUB, long_text(3), context_token="ctx-1")

    assert (result.status, result.reason_code) == ("rejected", "quota_insufficient")
    assert result.reason == (
        "这 24 小时的 10 条只剩 2 条，不够发这 3 条；给 ClawBot 发任意一句话就能恢复"
    )
    assert hub.upstream.calls == []


def test_a_reply_that_fails_gives_its_parts_back(hub):
    hub.ready()
    hub.upstream.script("send_text", Accepted("m-1"))
    hub.upstream.set_default("send_text", Transient("连不上"))  # 第一条之后一直连不上

    result = hub.sender.reply(HUB, long_text(3), context_token="ctx-1")

    assert (result.status, result.reason_code) == ("partial", "upstream_unreachable")
    assert (result.parts, result.parts_sent) == (3, 1)
    assert hub.used() == {("_hub", "reply"): 1}


# ---------------------------------------------------------------- 窗口、暂停、拒绝


def test_a_reply_has_no_on_closed_so_a_closed_window_is_a_rejection(hub):
    hub.ready()
    hub.clock.advance(25 * 3600)

    result = hub.sender.reply(HUB, "你好", context_token="ctx-1")

    assert (result.status, result.reason_code) == ("rejected", "window_closed")
    assert hub.upstream.calls == []


def test_a_reply_before_anyone_spoke_is_rejected(hub):
    hub.bind()

    result = hub.sender.reply(HUB, "你好", context_token="ctx-1")

    assert (result.status, result.reason_code) == ("rejected", "never_contacted")


def test_the_pause_after_a_refusal_does_not_stop_replies(hub):
    a = hub.ready()
    hub.upstream.script("send_text", Rejected(200, 40001, "bad token"))
    hub.sender.push(a, "第一条")
    assert hub.sender.push(a, "第二条").reason_code == "upstream_blocked"

    reply = hub.sender.reply(HUB, "你好", context_token="ctx-1")

    assert reply.status == "sent"


def test_a_refused_reply_does_not_start_a_pause(hub):
    a = hub.ready()
    hub.upstream.script("send_text", Rejected(200, 40001, "bad token"))

    reply = hub.sender.reply(HUB, "你好", context_token="ctx-1")
    push = hub.sender.push(a, "推送")

    assert (reply.status, reply.reason_code) == ("failed", "upstream_rejected")
    assert push.status == "sent"
    assert hub.holds == []
    assert hub.events("hold") == []


# ---------------------------------------------------------------- 校验和作者


def test_a_reply_is_checked_before_anything_is_spent(hub):
    hub.ready()

    with pytest.raises(SendInvalid):
        hub.sender.reply(HUB, "   ", context_token="ctx-1")

    assert hub.rows() == [] and hub.upstream.calls == []


def test_the_service_label_comes_from_the_configuration(make_hub):
    hub = make_hub(tweak(Config(), hub={"label": "中枢"}))
    hub.ready()

    hub.sender.reply(Origin("_hub", "中枢"), "收到", context_token="ctx-1")

    assert hub.texts == ["【中枢】收到"]


def test_a_reply_from_a_project_that_does_not_exist_is_a_programming_error(hub):
    hub.ready()

    with pytest.raises(ValueError, match="不存在"):
        hub.sender.reply(Origin("ghost", None), "你好", context_token="ctx-1")


def test_a_reply_can_be_sent_by_a_project_object(hub):
    hub.ready()
    b = hub.add("beta", guaranteed=1, cap=2)

    result = hub.sender.reply(b, "收到", context_token="ctx-1")

    assert result.status == "sent"
