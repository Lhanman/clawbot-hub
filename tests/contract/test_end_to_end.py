"""合约测试：发消息这一侧（任务 D5，规格 `hub-client`「合约测试」「接口」「错误分得清」）。

真 HTTP 服务加单文件客户端：发消息的每种结果、去重、停用和换钥、限速、读超时、`retries`、错误映射。
取话、回复和确认在 `test_inbox_flow.py`。服务里的时间是假的（测试 `advance`），客户端的读超时和
停顿是真的。
"""

from __future__ import annotations

import clawbot_hub_client as hub_client
import pytest

from clawbot_hub.config import Config
from clawbot_hub.upstream.base import NoResponse, Rejected
from tests.contract import covers
from tests.support.fake_upstream import DEFAULT
from tests.support.hub import tweak
from tests.support.threads import wait_for

DAY = 24 * 3600


def outcome(result: hub_client.SendResult) -> tuple[str, bool, bool, bool]:
    return result.status, result.ok, result.needs_fallback, result.maybe_delivered


# ---------------------------------------------------------------- 探活和状态


@covers("GET /v1/health")
def test_health_needs_no_key(rig, monkeypatch):
    monkeypatch.delenv("CLAWBOT_HUB_KEY", raising=False)

    body = hub_client.HubClient(rig.url).health()

    assert body["ok"] is True and body["api"] == hub_client.API_VERSION
    assert body["version"] == hub_client.CLIENT_VERSION


@covers("GET /v1/status")
def test_status_tells_a_project_its_own_quota_and_nothing_about_the_others(rig):
    body = rig.client().status()

    assert body["service"]["api"] == "1"
    assert body["service"]["send_timeout_seconds"] == 30
    assert body["service"]["inbox_online_seconds"] == 90
    assert body["login"] == {"state": "ok"}
    assert body["window"]["open"] is True
    assert body["quota"]["limit"] == 10
    assert (body["quota"]["project"]["guaranteed"], body["quota"]["project"]["cap"]) == (6, 8)
    assert "beta" not in repr(body) and "贝塔" not in repr(body)


@covers("GET /v1/status")
def test_a_status_call_teaches_the_client_the_deadline_the_service_reported(make_rig):
    rig = make_rig(tweak(Config(), send={"timeout_seconds": 60}))
    client = rig.client()
    assert client._reported_send_deadline is None

    client.status()

    assert client._reported_send_deadline == 60.0


# ---------------------------------------------------------------- 发消息的每种结果


@covers("POST /v1/messages")
def test_a_send_goes_through_and_the_upstream_gets_the_text_unchanged(rig):
    result = rig.client().send("盘点完成\n共 12 只")

    assert outcome(result) == ("sent", True, False, False)
    assert (result.parts, result.parts_sent, result.duplicate) == (1, 1, False)
    assert result.quota["limit"] == 10
    assert rig.hub.texts == ["盘点完成\n共 12 只"]


@covers("POST /v1/messages")
def test_two_projects_send_through_the_same_login_and_the_second_one_is_labelled(rig):
    rig.client("alpha").send("甲的消息")
    rig.client("beta").send("乙的消息")

    assert rig.hub.texts == ["甲的消息", "【贝塔】乙的消息"]


@covers("POST /v1/messages")
def test_a_closed_window_is_a_rejected_result_and_not_an_exception(rig):
    rig.clock.advance(DAY + 3600)

    result = rig.client().send("盘前简报")

    assert outcome(result) == ("rejected", False, True, False)
    assert result.reason_code == "window_closed" and "对话窗口过期了" in result.reason
    assert result.parts_sent == 0 and rig.hub.texts == []


@covers("POST /v1/messages")
def test_the_user_speaking_again_opens_the_window_again(rig):
    rig.clock.advance(DAY + 3600)
    assert rig.client().send("没人听").status == "rejected"

    rig.say("在吗")
    # alpha 没人在线收这句话，服务自己回使用者一句“现在没在线”；它发出去，说明窗口已经重新开了
    wait_for(lambda: len(rig.hub.texts) == 1)
    assert "现在没在线" in rig.hub.texts[0]
    assert rig.client().status()["window"]["open"] is True

    assert rig.client().send("有人听").status == "sent"
    assert rig.hub.texts[1:] == ["有人听"]


@covers("POST /v1/messages")
def test_a_closed_window_with_on_closed_skip_is_fine(rig):
    rig.clock.advance(DAY + 3600)

    result = rig.client().send("可有可无的消息", on_closed="skip")

    assert outcome(result) == ("skipped", True, False, False)
    assert result.reason_code == "window_closed" and result.parts_sent == 0
    assert rig.hub.texts == []


@covers("POST /v1/messages")
def test_the_same_dedupe_key_is_sent_once(rig):
    client = rig.client()

    first = client.send("今天的简报", dedupe_key="brief-2026-10-12")
    again = client.send("今天的简报", dedupe_key="brief-2026-10-12")

    assert (first.status, first.duplicate) == ("sent", False)
    assert (again.status, again.duplicate, again.id) == ("sent", True, first.id)
    assert rig.hub.texts == ["今天的简报"]
    assert rig.client("beta").send("别人的", dedupe_key="brief-2026-10-12").duplicate is False


@covers("POST /v1/messages")
def test_an_upstream_that_cannot_be_reached_is_a_failed_result(rig):
    rig.upstream.set_mode("disconnect")

    result = rig.client().send("发不出去")

    assert outcome(result) == ("failed", False, True, False)
    assert result.reason_code == "upstream_unreachable" and result.parts_sent == 0


@covers("POST /v1/messages")
def test_an_upstream_that_never_answers_is_an_unconfirmed_result_that_may_have_arrived(rig):
    rig.upstream.script("send_text", NoResponse("回话超时", read_timeout=True))

    result = rig.client().send("可能到了")

    assert outcome(result) == ("unconfirmed", False, True, True)
    assert result.reason_code == "no_response" and "可能已经到了" in result.reason


@covers("POST /v1/messages")
def test_a_long_text_that_only_got_through_in_part_is_a_partial_result(rig):
    rig.upstream.script("send_text", lambda call: DEFAULT, Rejected(200, 12345, "拒绝"))
    text = "很长的一段话。" * 400  # 比一条放得下的多，要拆成几条

    result = rig.client().send(text)

    assert outcome(result) == ("partial", False, True, True)
    assert result.parts >= 2 and result.parts_sent == 1
    assert result.reason_code == "upstream_rejected"


# ---------------------------------------------------------------- 不偷偷重发，`retries`


class ServiceComesBack:
    """客户端每停顿一次，就让“正在关闭”的服务恢复：第一次请求 503，重试时服务已经回来了。"""

    def __init__(self, rig) -> None:
        self.rig = rig
        self.pauses: list[float] = []
        rig.api.services.shutting_down.set()

    def __call__(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.rig.api.services.shutting_down.clear()


@covers("POST /v1/messages")
def test_a_send_with_a_dedupe_key_is_retried_when_the_service_was_shutting_down(rig, monkeypatch):
    comes_back = ServiceComesBack(rig)
    monkeypatch.setattr(hub_client, "_sleep", comes_back)

    result = rig.client(retries=1).send("简报", dedupe_key="brief-1")

    assert result.status == "sent" and comes_back.pauses == [1.0]
    assert rig.hub.texts == ["简报"]


@covers("POST /v1/messages")
def test_a_send_without_a_dedupe_key_is_never_retried(rig, monkeypatch):
    comes_back = ServiceComesBack(rig)
    monkeypatch.setattr(hub_client, "_sleep", comes_back)

    with pytest.raises(hub_client.HubUnavailable) as info:
        rig.client(retries=3).send("没有去重键")

    assert info.value.possibly_sent is False and info.value.http_status == 503
    assert comes_back.pauses == [] and rig.hub.texts == []


@covers("POST /v1/messages")
def test_retries_zero_means_the_first_failure_is_the_last(rig, monkeypatch):
    comes_back = ServiceComesBack(rig)
    monkeypatch.setattr(hub_client, "_sleep", comes_back)

    with pytest.raises(hub_client.HubUnavailable):
        rig.client(retries=0).send("不重试", dedupe_key="k")

    assert comes_back.pauses == [] and rig.hub.texts == []


@covers("POST /v1/messages")
def test_the_service_being_down_is_an_unavailable_service_that_sent_nothing(rig):
    client = hub_client.HubClient("http://127.0.0.1:1", rig.key(), retries=0)

    with pytest.raises(hub_client.HubUnavailable) as info:
        client.send("没人接")

    assert info.value.possibly_sent is False and info.value.http_status is None


# ---------------------------------------------------------------- 读超时


@covers("POST /v1/messages")
def test_a_read_timeout_on_a_send_means_it_may_still_have_been_sent(rig, monkeypatch):
    """服务正在等腾讯，客户端先读超时了：抛 `HubUnavailable`、`possibly_sent` 为真；之后服务把这条
    发完，消息确实到了。这就是“不要当作没发”的原因。"""
    monkeypatch.setattr(hub_client, "DEFAULT_SEND_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(hub_client, "SEND_TIMEOUT_MARGIN", 0.0)
    gate = rig.upstream.hold("send_text")

    try:
        with pytest.raises(hub_client.HubUnavailable) as info:
            rig.client(timeout=0.3, retries=0).send("慢消息", dedupe_key="slow-1")
        assert info.value.possibly_sent is True and info.value.http_status is None
    finally:
        gate.release()

    wait_for(lambda: rig.hub.texts == ["慢消息"])
    assert rig.client().send("慢消息", dedupe_key="slow-1").duplicate is True


# ---------------------------------------------------------------- 停用、换钥


@covers("POST /v1/messages", "GET /v1/status", "GET /v1/inbox")
def test_a_disabled_project_gets_an_auth_error_with_a_code_until_it_is_enabled_again(rig):
    client = rig.client()
    rig.hub.projects.disable("alpha")

    for call in (lambda: client.send("x"), client.status, lambda: client.inbox(wait=0)):
        with pytest.raises(hub_client.HubAuthError) as info:
            call()
        assert (info.value.code, info.value.http_status) == ("project_disabled", 403)
    assert client.health()["ok"] is True  # 探活不看密钥，也不看项目

    rig.hub.projects.enable("alpha")
    assert client.send("好了").status == "sent"


@covers("POST /v1/messages", "GET /v1/inbox")
def test_after_a_key_is_rotated_the_old_key_is_refused_and_the_new_one_works(rig):
    old = rig.client()
    assert old.send("换钥前").status == "sent"

    new_key = rig.hub.projects.rotate("alpha", yes=True).key

    with pytest.raises(hub_client.HubAuthError) as info:
        old.send("旧钥匙")
    assert (info.value.code, info.value.http_status) == ("unauthorized", 401)
    with pytest.raises(hub_client.HubAuthError):
        old.inbox(wait=0)
    assert hub_client.HubClient(rig.url, new_key).send("新钥匙").status == "sent"
    assert rig.hub.texts == ["换钥前", "新钥匙"]


@covers("POST /v1/messages")
def test_a_key_the_service_does_not_know_is_unauthorized(rig):
    with pytest.raises(hub_client.HubAuthError) as info:
        hub_client.HubClient(rig.url, "chub_" + "x" * 43).send("冒充")

    assert info.value.code == "unauthorized" and rig.hub.texts == []


# ---------------------------------------------------------------- 限速


@covers("POST /v1/messages")
def test_too_many_requests_in_a_minute_are_told_to_wait_and_may_try_again_later(rig):
    client = rig.client()
    for _ in range(30):  # 每个项目每分钟 30 次，不管结果是发出去还是被拒绝
        client.send("刷屏", on_closed="skip")

    with pytest.raises(hub_client.HubRequestError) as info:
        client.send("第 31 次")

    assert (info.value.code, info.value.http_status) == ("rate_limited", 429)
    assert 1 <= info.value.retry_after <= 60

    rig.clock.advance(61)
    assert client.send("过了一分钟").status in ("sent", "rejected")


# ---------------------------------------------------------------- 错误映射


@covers("POST /v1/messages")
@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"text": "   \n "}, "empty_text"),
        ({"text": "字" * 8001}, "text_too_long"),
        ({"text": "好", "priority": "high"}, "bad_request"),
        ({"text": "好", "on_closed": "ignore"}, "bad_request"),
        ({"text": "好", "dedupe_key": "k" * 129}, "bad_request"),
    ],
)
def test_a_request_the_service_cannot_accept_is_a_request_error_with_its_code(rig, kwargs, code):
    with pytest.raises(hub_client.HubRequestError) as info:
        rig.client().send(**kwargs)

    assert (info.value.code, info.value.http_status) == (code, 400)
    assert rig.hub.texts == []


@covers("POST /v1/messages")
def test_a_body_over_64_kib_is_a_request_error_413(rig):
    with pytest.raises(hub_client.HubRequestError) as info:
        rig.client().send("字" * 30000)  # 3 个字节一个字，90000 字节

    assert (info.value.code, info.value.http_status) == ("too_large", 413)


@covers("POST /v1/messages")
def test_an_internal_error_may_have_sent_something(rig, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("测试里注入的意外")

    monkeypatch.setattr(rig.hub.sender, "push", boom)

    with pytest.raises(hub_client.HubUnavailable) as info:
        rig.client(retries=0).send("会出错")

    assert (info.value.possibly_sent, info.value.http_status) == (True, 500)
    assert "测试里注入的意外" not in info.value.message


@covers("POST /v1/messages", "GET /v1/health")
def test_a_service_that_is_shutting_down_is_unavailable_and_nothing_was_sent(rig):
    rig.api.services.shutting_down.set()

    with pytest.raises(hub_client.HubUnavailable) as info:
        rig.client(retries=0).send("关门了")
    with pytest.raises(hub_client.HubUnavailable) as health:
        rig.client(retries=0).health()

    assert (info.value.possibly_sent, info.value.http_status) == (False, 503)
    assert health.value.http_status == 503
    assert rig.hub.texts == []


@covers("GET /v1/inbox")
def test_a_push_only_project_cannot_fetch_and_is_told_so(rig):
    rig.hub.add("pusher", push_only=True)
    client = hub_client.HubClient(rig.url, rig.hub.keys["pusher"])

    with pytest.raises(hub_client.HubAuthError) as info:
        client.inbox(wait=0)

    assert (info.value.code, info.value.http_status) == ("push_only", 403)
    assert client.send("只推送也能发").status == "sent"
