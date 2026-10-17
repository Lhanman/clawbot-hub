"""给告警的回调 `on_request`：每个 `sendmessage` 请求结束后，按"有没有确认送达"通知一次。
规格：`hub-alerts` 的 `send_unconfirmed`（按请求算，不按消息算；拿到消息编号的归零；腾讯明确拒绝、
连不上、重试用完、服务自己拒绝的既不计数也不归零）。计数和告警本身归阶段 D2，这里只测发送层
给出的分类。
"""

from __future__ import annotations

import logging

import pytest

from clawbot_hub.config import Config
from clawbot_hub.sender import CONFIRMED, UNCONFIRMED, Origin
from clawbot_hub.upstream.base import Accepted, NoResponse, Rejected, SessionExpired, Transient
from tests.sender.conftest import long_text
from tests.support.hub import Hub, tweak

HUB = Origin("_hub", "服务")


@pytest.fixture
def kinds() -> list[str]:
    return []


@pytest.fixture
def hub(make_hub, kinds):
    return make_hub(on_request=kinds.append)


def with_confirm(make_hub, kinds, mode):
    return make_hub(tweak(Config(), upstream={"confirm": mode}), on_request=kinds.append)


def test_a_part_with_a_message_id_is_confirmed(hub, kinds):
    a = hub.ready()

    hub.sender.push(a, "你好")

    assert kinds == [CONFIRMED]


def test_every_part_is_one_request(hub, kinds):
    a = hub.ready()

    hub.sender.push(a, long_text(3))

    assert kinds == [CONFIRMED, CONFIRMED, CONFIRMED]


@pytest.mark.parametrize("mode", ["observe", "strict"])
def test_a_success_without_a_message_id_is_unconfirmed(make_hub, kinds, mode):
    hub = with_confirm(make_hub, kinds, mode)
    a = hub.ready()
    hub.upstream.set_default("send_text", Accepted(None))

    hub.sender.push(a, long_text(3))

    assert kinds == [UNCONFIRMED] * 3  # 一条拆三段都没编号算 3 次


def test_the_off_mode_does_not_look_at_message_ids(make_hub, kinds):
    hub = with_confirm(make_hub, kinds, "off")
    a = hub.ready()
    hub.upstream.script("send_text", Accepted(None), Accepted("m-1"))

    hub.sender.push(a, long_text(2))

    assert kinds == [CONFIRMED]  # 没编号的那次既不计数也不归零


def test_a_request_nobody_answered_is_unconfirmed(hub, kinds):
    a = hub.ready()
    hub.upstream.script("send_text", NoResponse("读超时", read_timeout=True))

    hub.sender.push(a, "你好")

    assert kinds == [UNCONFIRMED]


def test_an_unconfirmed_part_and_what_follows_it(make_hub, kinds):
    hub = with_confirm(make_hub, kinds, "strict")
    a = hub.ready()
    hub.upstream.script("send_text", Accepted(None), Accepted(None), Accepted("m-1"))

    hub.sender.push(a, long_text(3))  # 严格档：没编号也继续发后面的段

    assert kinds == [UNCONFIRMED, UNCONFIRMED, CONFIRMED]


@pytest.mark.parametrize(
    "outcomes",
    [
        [Rejected(200, 40001, "bad token")],
        [Rejected(403, None, "")],
        [SessionExpired()],
        [Transient("连不上")] * 3,
    ],
    ids=["rejected", "http-403", "login-expired", "unreachable"],
)
def test_requests_that_clearly_did_not_go_out_are_not_reported(hub, kinds, outcomes):
    a = hub.ready()
    hub.upstream.script("send_text", *outcomes)

    hub.sender.push(a, "你好")

    assert kinds == []


def test_a_retry_that_gets_through_is_one_request(hub, kinds):
    a = hub.ready()
    hub.upstream.script("send_text", Transient("连不上"), Transient("连不上"), Accepted("m-1"))

    hub.sender.push(a, "你好")

    assert kinds == [CONFIRMED]


def test_a_refusal_by_the_service_is_not_a_request(hub, kinds):
    a = hub.ready()
    hub.clock.advance(25 * 3600)

    hub.sender.push(a, "你好")

    assert kinds == []


def test_a_reply_is_reported_too(hub, kinds):
    hub.ready()

    hub.sender.reply(HUB, "收到", context_token="ctx-1")

    assert kinds == [CONFIRMED]


def test_the_part_is_already_booked_when_the_hook_runs(make_hub):
    seen = []

    def look(kind):
        row = hub.db.conn().execute("SELECT parts_sent, status FROM outbox").fetchone()
        seen.append((kind, row["parts_sent"], row["status"]))

    hub = make_hub(on_request=look)
    a = hub.ready()

    hub.sender.push(a, long_text(2))

    assert seen == [(CONFIRMED, 1, "sending"), (CONFIRMED, 2, "sending")]


def test_a_failing_hook_does_not_break_the_send(make_hub, caplog):
    def broken(kind):
        raise RuntimeError("计数坏了：不要出现在日志里")

    hub = make_hub(on_request=broken)
    a = hub.ready()
    caplog.set_level(logging.DEBUG)

    result = hub.sender.push(a, long_text(2))

    assert (result.status, result.parts_sent) == ("sent", 2)
    assert any("请求结果的回调出错" in record.getMessage() for record in caplog.records)
    assert all("不要出现在日志里" not in record.getMessage() for record in caplog.records)


def test_a_send_without_a_hook_works(paths, database, clock, fake_upstream):
    hub = Hub(paths, database, clock, fake_upstream, on_request=None)
    a = hub.ready()

    assert hub.sender.push(a, "你好").status == "sent"
