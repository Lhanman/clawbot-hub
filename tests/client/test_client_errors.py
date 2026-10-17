"""错误分得清、不偷偷重发（任务 D4，规格 `hub-client`「错误分得清」「不偷偷重发」）。

重点是 `HubUnavailable.possibly_sent`：连接建立之后才出的错（读超时、被重置、断开、500）不知道服务
有没有发出去，调用方不能当作没发；连不上、连接超时、502 / 503 / 504 一定没发。重试只在"确定没被
处理"时做。
"""

from __future__ import annotations

import copy
import errno
import http.client
import logging
import pickle
import socket

import clawbot_hub_client as hub_client
import pytest

from tests.client.conftest import KEY, FakeHub, error_body, result_body
from tests.support.fakehttp import Reply, close, hang, html, reset, truncated

pytestmark = pytest.mark.usefixtures("pauses")


def client_for(hub, **kwargs) -> hub_client.HubClient:
    return hub_client.HubClient(hub.base_url, KEY, **kwargs)


def err(status: int, code: str, message: str = "服务说的原因") -> Reply:
    return Reply(status=status, body=error_body(code, message))


@pytest.fixture
def stopped_hub_url() -> str:
    """一个没人听的地址：刚才有服务在那里，现在关了（连接会被拒绝）。"""
    with FakeHub() as fake:
        url = fake.base_url
    return url


# ---------------------------------------------------------------- 异常的家谱


def test_every_exception_of_the_client_is_a_hub_error():
    assert issubclass(hub_client.HubError, Exception)
    for kind in (hub_client.HubUnavailable, hub_client.HubAuthError, hub_client.HubRequestError):
        assert issubclass(kind, hub_client.HubError)


def test_the_three_kinds_do_not_inherit_from_each_other():
    kinds = (hub_client.HubUnavailable, hub_client.HubAuthError, hub_client.HubRequestError)
    for kind in kinds:
        assert [other for other in kinds if other is not kind and issubclass(kind, other)] == []


def test_the_exceptions_print_their_message_and_carry_the_documented_attributes():
    unavailable = hub_client.HubUnavailable("连不上", possibly_sent=True, http_status=500)
    auth = hub_client.HubAuthError("密钥不对", code="unauthorized", http_status=401)
    request = hub_client.HubRequestError(
        "太长", code="text_too_long", retry_after=12, http_status=400
    )

    assert str(unavailable) == "连不上" and unavailable.message == "连不上"
    assert (unavailable.possibly_sent, unavailable.http_status) == (True, 500)
    assert (str(auth), auth.code, auth.http_status) == ("密钥不对", "unauthorized", 401)
    assert (str(request), request.code, request.retry_after) == ("太长", "text_too_long", 12)
    assert hub_client.HubUnavailable("x").possibly_sent is False
    assert hub_client.HubAuthError("x").code is None
    assert hub_client.HubRequestError("x").retry_after is None


def test_the_exceptions_survive_pickling_and_copying():
    original = hub_client.HubRequestError(
        "太长", code="text_too_long", retry_after=3, http_status=400
    )

    for clone in (copy.copy(original), pickle.loads(pickle.dumps(original))):
        assert (clone.message, clone.code, clone.retry_after, clone.http_status) == (
            "太长",
            "text_too_long",
            3,
            400,
        )


# ---------------------------------------------------------------- 服务不可用：possibly_sent

# (名字, 怎么回, possibly_sent)
SERVER_FAILURES = [
    ("500 internal", err(500, "internal", "服务内部出错"), True),
    ("501", Reply(status=501, body=None), True),
    ("507", Reply(status=507, body=None), True),
    ("599", Reply(status=599, body=None), True),
    ("502", err(502, "bad_gateway"), False),
    ("502 网关页面", html(502), False),
    ("503 shutting_down", err(503, "shutting_down", "服务正在关闭，稍后再试"), False),
    ("503 空响应", Reply(status=503, body=None), False),
    ("504", Reply(status=504, body=None), False),
    ("读到一半断开", truncated(), True),
    ("不回话直接关", close(), True),
    ("连接被重置", reset(), True),
]


@pytest.mark.parametrize(
    ("name", "reply", "sent"), SERVER_FAILURES, ids=[c[0] for c in SERVER_FAILURES]
)
def test_a_failing_service_is_unavailable_and_says_whether_the_message_may_have_gone(
    hub, name, reply, sent
):
    hub.script("messages", reply)

    with pytest.raises(hub_client.HubUnavailable) as info:
        client_for(hub).send("早报")

    assert info.value.possibly_sent is sent
    assert KEY not in str(info.value)
    assert len(hub.requests_to("messages")) == 1  # 没给去重键，不重试


def test_a_read_timeout_may_have_sent(hub, quick):
    hub.script("messages", hang(5))

    with pytest.raises(hub_client.HubUnavailable) as info:
        client_for(hub, timeout=0.3).send("早报")

    assert info.value.possibly_sent is True
    assert info.value.http_status is None


def test_the_message_of_a_503_is_the_one_the_service_gave(hub):
    hub.script("messages", err(503, "shutting_down", "服务正在关闭，稍后再试"))

    with pytest.raises(hub_client.HubUnavailable) as info:
        client_for(hub).send("早报")

    assert "服务正在关闭，稍后再试" in str(info.value)
    assert info.value.http_status == 503


def test_a_service_that_is_not_running_is_unavailable_and_nothing_was_sent(stopped_hub_url):
    with pytest.raises(hub_client.HubUnavailable) as info:
        hub_client.HubClient(stopped_hub_url, KEY).send("早报")

    assert info.value.possibly_sent is False
    assert info.value.http_status is None
    assert KEY not in str(info.value)


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("timed out"),
        TimeoutError("timed out"),
        socket.gaierror(-2, "Name or service not known"),
        OSError(errno.ENETUNREACH, "Network is unreachable"),
        OSError(errno.EHOSTUNREACH, "No route to host"),
        ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"),
    ],
    ids=lambda failure: type(failure).__name__ + str(getattr(failure, "errno", "")),
)
def test_any_failure_while_connecting_means_nothing_was_sent(hub, monkeypatch, failure):
    def refuse(self):
        raise failure

    monkeypatch.setattr(http.client.HTTPConnection, "connect", refuse)

    with pytest.raises(hub_client.HubUnavailable) as info:
        client_for(hub).send("早报")

    assert info.value.possibly_sent is False
    assert hub.requests == []


def test_a_failed_tls_handshake_means_nothing_was_sent(hub):
    """地址写成 https，服务其实是明文 http：握手就失败，请求还没发出去。"""
    client = hub_client.HubClient(hub.base_url.replace("http://", "https://"), KEY)

    with pytest.raises(hub_client.HubUnavailable) as info:
        client.send("早报")

    assert info.value.possibly_sent is False
    assert hub.requests == []


def test_a_failure_after_the_connection_is_made_may_have_sent(hub, monkeypatch):
    """连上以后，发请求或读回话时出的任何网络错误都当作"可能已经到了"。"""

    def broken(self, *args, **kwargs):
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")

    monkeypatch.setattr(http.client.HTTPConnection, "request", broken)

    with pytest.raises(hub_client.HubUnavailable) as info:
        client_for(hub).send("早报")

    assert info.value.possibly_sent is True


@pytest.mark.parametrize("status", [301, 302, 307, 204, 304])
def test_a_status_nobody_expected_is_not_followed_and_may_have_sent(hub, status):
    hub.script(
        "messages",
        Reply(status=status, body=None, headers={"Location": hub.base_url + "/elsewhere"}),
    )

    with pytest.raises(hub_client.HubUnavailable) as info:
        client_for(hub).send("早报")

    assert info.value.possibly_sent is True and info.value.http_status == status
    assert [r.path for r in hub.requests] == ["/v1/messages"]  # 不去 Location


def test_a_200_that_is_not_json_may_have_sent(hub):
    hub.script("messages", html(200))

    with pytest.raises(hub_client.HubUnavailable) as info:
        client_for(hub).send("早报")

    assert info.value.possibly_sent is True and info.value.http_status == 200


@pytest.mark.parametrize(
    "raw",
    [b"\xff\xfe not utf-8", b"{", b"", b"{'single': 'quotes'}"],
)
def test_a_200_with_a_body_that_cannot_be_read_may_have_sent(hub, raw):
    hub.script("messages", Reply(body=raw, content_type="application/json"))

    with pytest.raises(hub_client.HubUnavailable) as info:
        client_for(hub).send("早报")

    assert info.value.possibly_sent is True


# ---------------------------------------------------------------- 认证和请求错误

AUTH_CASES = [
    (err(401, "unauthorized", "没带密钥，或者密钥不对"), 401, "unauthorized"),
    (err(403, "project_disabled", "这个项目已停用"), 403, "project_disabled"),
    (err(403, "push_only", "这个项目是只推送的，不能取话"), 403, "push_only"),
    (Reply(status=403, body=None), 403, None),  # 局域网规则拒绝的 403 没有正文
    (Reply(status=401, body="<html>nope</html>"), 401, None),
    (Reply(status=403, body={"error": "不是约定的形状"}), 403, None),
    (Reply(status=403, body={"error": {"code": 7, "message": 8}}), 403, None),
]


@pytest.mark.parametrize(("reply", "http_status", "code"), AUTH_CASES)
def test_401_and_403_are_auth_errors_with_the_code_when_there_is_one(hub, reply, http_status, code):
    hub.script("messages", reply)

    with pytest.raises(hub_client.HubAuthError) as info:
        client_for(hub).send("早报")

    assert (info.value.http_status, info.value.code) == (http_status, code)
    assert str(info.value)  # 总有一句话说是怎么回事
    assert KEY not in str(info.value)
    assert len(hub.requests_to("messages")) == 1


def test_an_auth_error_carries_the_chinese_reason_of_the_service(hub):
    hub.script("messages", err(403, "project_disabled", "这个项目已停用：请联系运维启用后再试"))

    with pytest.raises(hub_client.HubAuthError) as info:
        client_for(hub).send("早报")

    assert str(info.value) == "这个项目已停用：请联系运维启用后再试"


REQUEST_CASES = [
    (err(400, "bad_request", "priority 的值不对"), 400, "bad_request", None),
    (err(400, "text_too_long", "文字太长：最多 8000 个字"), 400, "text_too_long", None),
    (err(400, "empty_text", "文字不能是空的"), 400, "empty_text", None),
    (err(404, "unknown_handle", "没有这个回复句柄"), 404, "unknown_handle", None),
    (err(410, "handle_expired", "回复句柄已经失效"), 410, "handle_expired", None),
    (err(413, "too_large", "请求太大"), 413, "too_large", None),
    (err(404, "not_found", "没有这个接口"), 404, "not_found", None),
    (err(405, "method_not_allowed", "不支持"), 405, "method_not_allowed", None),
    (
        Reply(
            status=429, body=error_body("rate_limited", "请求太频繁"), headers={"Retry-After": "12"}
        ),
        429,
        "rate_limited",
        12,
    ),
    (
        Reply(
            status=429,
            body=error_body("reply_limit", "这个句柄回得太多了"),
            headers={"Retry-After": "600"},
        ),
        429,
        "reply_limit",
        600,
    ),
    (
        Reply(
            status=429,
            body=error_body("too_many_failures", "先停一会儿"),
            headers={"Retry-After": "40"},
        ),
        429,
        "too_many_failures",
        40,
    ),
    (Reply(status=429, body=error_body("rate_limited", "请求太频繁")), 429, "rate_limited", None),
    (Reply(status=400, body=None), 400, None, None),
    (Reply(status=404, body="<html>Not Found</html>"), 404, None, None),
]


@pytest.mark.parametrize(("reply", "http_status", "code", "retry_after"), REQUEST_CASES)
def test_other_4xx_are_request_errors_with_code_message_and_retry_after(
    hub, reply, http_status, code, retry_after
):
    hub.script("messages", reply)

    with pytest.raises(hub_client.HubRequestError) as info:
        client_for(hub).send("早报", dedupe_key="k")  # 有去重键也不重试 4xx

    assert (info.value.http_status, info.value.code, info.value.retry_after) == (
        http_status,
        code,
        retry_after,
    )
    assert str(info.value)
    assert len(hub.requests_to("messages")) == 1


def test_the_message_of_a_request_error_is_the_one_the_service_gave(hub):
    hub.script("messages", err(400, "text_too_long", "文字太长：最多 8000 个字"))

    with pytest.raises(hub_client.HubRequestError) as info:
        client_for(hub).send("很长")

    assert info.value.message == "文字太长：最多 8000 个字"


@pytest.mark.parametrize(
    "header", ["soon", "-5", "1.5", "", " ", "Wed, 21 Oct 2026 07:28:00 GMT", "1e3", "0x10"]
)
def test_a_retry_after_that_is_not_a_whole_number_of_seconds_is_ignored(hub, header):
    hub.script(
        "messages",
        Reply(status=429, body=error_body("rate_limited", "x"), headers={"Retry-After": header}),
    )

    with pytest.raises(hub_client.HubRequestError) as info:
        client_for(hub).send("早报")

    assert info.value.retry_after is None and info.value.code == "rate_limited"


@pytest.mark.parametrize(("header", "seconds"), [("0", 0), ("12", 12), (" 7 ", 7), ("0012", 12)])
def test_a_retry_after_in_seconds_is_read_as_a_number(hub, header, seconds):
    hub.script(
        "messages",
        Reply(status=429, body=error_body("rate_limited", "x"), headers={"Retry-After": header}),
    )

    with pytest.raises(hub_client.HubRequestError) as info:
        client_for(hub).send("早报")

    assert info.value.retry_after == seconds


def test_a_business_rejection_and_an_http_error_are_two_different_things(hub):
    """HTTP 400 `text_too_long` 是错误（抛异常，`code`）；业务结果 `rejected` 带的 `reason_code`
    是结果里的字段（不抛异常），两套代码不混用。"""
    hub.script("messages", err(400, "text_too_long", "文字太长"))
    hub.script(
        "messages", Reply(body=result_body("rejected", reason_code="text_too_long", reason="x"))
    )
    client = client_for(hub)

    with pytest.raises(hub_client.HubRequestError) as info:
        client.send("第一次")
    result = client.send("第二次")

    assert info.value.code == "text_too_long"
    assert result.status == "rejected" and result.reason_code == "text_too_long"
    assert not hasattr(result, "code") and not hasattr(info.value, "reason_code")


BUSINESS_RESULTS = ["rejected", "failed", "unconfirmed", "partial", "skipped", "sent"]


@pytest.mark.parametrize("status", BUSINESS_RESULTS)
@pytest.mark.parametrize("method", ["send", "reply"])
def test_business_results_never_raise(hub, status, method):
    hub.script("messages", Reply(body=result_body(status, reason_code="x", reason="原因")))
    hub.script("replies", Reply(body=result_body(status, reason_code="x", reason="原因")))
    client = client_for(hub)

    result = client.send("你好") if method == "send" else client.reply("h_1", "你好")

    assert result.status == status and result.reason == "原因"


# ---------------------------------------------------------------- 每个方法的错误一样分

METHODS = {
    "send": ("messages", lambda c: c.send("你好")),
    "reply": ("replies", lambda c: c.reply("h_1", "好")),
    "inbox": ("inbox", lambda c: c.inbox(wait=0)),
    "ack": ("ack", lambda c: c.ack(["in_1"])),
    "status": ("status", lambda c: c.status()),
}


@pytest.mark.parametrize("name", METHODS)
def test_every_method_maps_the_errors_the_same_way(hub, name):
    endpoint, call = METHODS[name]
    client = client_for(hub, retries=0)

    hub.script(endpoint, err(503, "shutting_down"))
    with pytest.raises(hub_client.HubUnavailable):
        call(client)
    hub.script(endpoint, err(401, "unauthorized"))
    with pytest.raises(hub_client.HubAuthError):
        call(client)
    hub.script(endpoint, err(400, "bad_request"))
    with pytest.raises(hub_client.HubRequestError):
        call(client)


def test_health_maps_the_errors_the_same_way(hub):
    client = client_for(hub, retries=0)

    hub.script("health", err(503, "shutting_down"))
    with pytest.raises(hub_client.HubUnavailable):
        client.health()
    hub.script("health", err(404, "not_found"))
    with pytest.raises(hub_client.HubRequestError):
        client.health()


# ---------------------------------------------------------------- 不偷偷重发


def test_without_a_dedupe_key_a_503_is_not_retried(hub, pauses):
    hub.script("messages", err(503, "shutting_down"), Reply(body=result_body()))

    with pytest.raises(hub_client.HubUnavailable):
        client_for(hub).send("早报")

    assert len(hub.requests_to("messages")) == 1
    assert pauses == []


def test_with_a_dedupe_key_a_503_is_retried_once_after_a_second(hub, pauses):
    hub.script("messages", err(503, "shutting_down"), Reply(body=result_body()))

    result = client_for(hub).send("早报", dedupe_key="k")

    assert result.status == "sent"
    first, second = hub.requests_to("messages")
    assert first.json == second.json
    assert first.json["dedupe_key"] == "k" and second.json["dedupe_key"] == "k"
    assert pauses == [1.0]


def test_a_read_timeout_with_a_dedupe_key_is_not_retried(hub, quick, pauses):
    hub.script("messages", hang(5))

    with pytest.raises(hub_client.HubUnavailable) as info:
        client_for(hub, timeout=0.3).send("早报", dedupe_key="k")

    assert info.value.possibly_sent is True
    assert len(hub.requests_to("messages")) == 1
    assert pauses == []


# 有去重键时哪些失败会重试：(怎么回, 第一次失败后是否再发一次)
RETRY_WITH_KEY = [
    ("502", err(502, "bad_gateway"), True),
    ("503", err(503, "shutting_down"), True),
    ("504", Reply(status=504, body=None), True),
    ("500", err(500, "internal"), False),
    ("501", Reply(status=501, body=None), False),
    ("重置", reset(), False),
    ("直接关", close(), False),
    ("读到一半断开", truncated(), False),
    ("200 但看不懂", html(200), False),
    ("401", err(401, "unauthorized"), False),
    ("403", err(403, "project_disabled"), False),
    ("400", err(400, "bad_request"), False),
    (
        "429",
        Reply(status=429, body=error_body("rate_limited", "x"), headers={"Retry-After": "1"}),
        False,
    ),
    ("302", Reply(status=302, body=None), False),
]


@pytest.mark.parametrize(
    ("name", "reply", "again"), RETRY_WITH_KEY, ids=[c[0] for c in RETRY_WITH_KEY]
)
def test_only_failures_that_surely_did_not_reach_the_service_are_retried(hub, name, reply, again):
    hub.script("messages", reply)  # 第一次；第二次走默认（sent）
    client = client_for(hub)

    try:
        client.send("早报", dedupe_key="k")
    except hub_client.HubError:
        pass

    assert len(hub.requests_to("messages")) == (2 if again else 1)


def test_connection_refused_with_a_dedupe_key_is_retried(stopped_hub_url, pauses):
    client = hub_client.HubClient(stopped_hub_url, KEY, retries=2)

    with pytest.raises(hub_client.HubUnavailable) as info:
        client.send("早报", dedupe_key="k")

    assert info.value.possibly_sent is False
    assert pauses == [1.0, 1.0]  # 三次尝试，两次停顿


def test_a_connect_timeout_with_a_dedupe_key_is_retried(hub, monkeypatch, pauses):
    attempts = []

    def slow(self):
        attempts.append(1)
        raise TimeoutError("timed out")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", slow)

    with pytest.raises(hub_client.HubUnavailable):
        client_for(hub).send("早报", dedupe_key="k")

    assert len(attempts) == 2 and pauses == [1.0]


def test_retries_zero_never_resends(hub, pauses):
    hub.script("messages", err(503, "shutting_down"), Reply(body=result_body()))

    with pytest.raises(hub_client.HubUnavailable):
        client_for(hub, retries=0).send("早报", dedupe_key="k")

    assert len(hub.requests_to("messages")) == 1 and pauses == []


def test_retries_two_resends_twice(hub, pauses):
    hub.script("messages", err(503, "x"), err(503, "x"), Reply(body=result_body()))

    result = client_for(hub, retries=2).send("早报", dedupe_key="k")

    assert result.ok and len(hub.requests_to("messages")) == 3
    assert pauses == [1.0, 1.0]


def test_the_last_failure_is_raised_after_the_retries_run_out(hub, pauses):
    hub.script(
        "messages", err(503, "x", "第一次"), err(503, "x", "第二次"), err(503, "x", "第三次")
    )

    with pytest.raises(hub_client.HubUnavailable) as info:
        client_for(hub, retries=2).send("早报", dedupe_key="k")

    assert "第三次" in str(info.value)
    assert len(hub.requests_to("messages")) == 3 and pauses == [1.0, 1.0]  # 最后一次失败后不再停顿


def test_a_retry_that_hits_a_different_failure_stops_there(hub, pauses):
    """第一次 503（没发出），第二次读到一半断开（可能发了）：到此为止，不再试第三次。"""
    hub.script("messages", err(503, "x"), truncated(), Reply(body=result_body()))

    with pytest.raises(hub_client.HubUnavailable) as info:
        client_for(hub, retries=5).send("早报", dedupe_key="k")

    assert info.value.possibly_sent is True
    assert len(hub.requests_to("messages")) == 2 and pauses == [1.0]


def test_a_reply_is_never_retried(hub, pauses):
    hub.script("replies", err(503, "x"), Reply(body=result_body()))

    with pytest.raises(hub_client.HubUnavailable):
        client_for(hub, retries=3).reply("h_1", "好")

    assert len(hub.requests_to("replies")) == 1 and pauses == []


def test_a_send_that_succeeds_is_sent_once(hub, pauses):
    client_for(hub, retries=3).send("早报", dedupe_key="k")

    assert len(hub.requests_to("messages")) == 1 and pauses == []


# inbox / ack / status / health 重试到底：任何 HubUnavailable 都算
IDEMPOTENT = {
    "inbox": ("inbox", lambda c: c.inbox(wait=0)),
    "ack": ("ack", lambda c: c.ack(["in_1"])),
    "status": ("status", lambda c: c.status()),
    "health": ("health", lambda c: c.health()),
}

ANY_UNAVAILABLE = [
    err(503, "shutting_down"),
    err(500, "internal"),
    Reply(status=502, body=None),
    reset(),
    close(),
    truncated(),
    html(200),
]


@pytest.mark.parametrize("failure", ANY_UNAVAILABLE, ids=lambda r: f"{r.action}-{r.status}")
@pytest.mark.parametrize("name", IDEMPOTENT)
def test_calls_that_are_safe_to_repeat_retry_after_any_unavailable(hub, pauses, name, failure):
    endpoint, call = IDEMPOTENT[name]
    hub.script(endpoint, failure)

    call(client_for(hub))  # 第二次默认回复，成功

    assert len(hub.requests_to(endpoint)) == 2
    assert pauses == [1.0]


@pytest.mark.parametrize("name", IDEMPOTENT)
def test_calls_that_are_safe_to_repeat_give_up_after_the_configured_retries(hub, pauses, name):
    endpoint, call = IDEMPOTENT[name]
    hub.script(endpoint, *[err(503, "x")] * 4)

    with pytest.raises(hub_client.HubUnavailable):
        call(client_for(hub, retries=2))

    assert len(hub.requests_to(endpoint)) == 3 and pauses == [1.0, 1.0]


@pytest.mark.parametrize("name", IDEMPOTENT)
def test_calls_that_are_safe_to_repeat_do_not_retry_with_zero_retries(hub, pauses, name):
    endpoint, call = IDEMPOTENT[name]
    hub.script(endpoint, err(503, "x"))

    with pytest.raises(hub_client.HubUnavailable):
        call(client_for(hub, retries=0))

    assert len(hub.requests_to(endpoint)) == 1 and pauses == []


@pytest.mark.parametrize("name", ["inbox", "ack", "status"])
@pytest.mark.parametrize(
    "reply", [err(401, "unauthorized"), err(403, "project_disabled"), err(400, "bad_request")]
)
def test_calls_that_are_safe_to_repeat_still_do_not_retry_auth_and_request_errors(
    hub, pauses, name, reply
):
    endpoint, call = IDEMPOTENT[name]
    hub.script(endpoint, reply)

    with pytest.raises(hub_client.HubError):
        call(client_for(hub, retries=3))

    assert len(hub.requests_to(endpoint)) == 1 and pauses == []


def test_a_retried_ack_with_a_failing_second_batch_resends_only_what_failed(hub, pauses):
    """分批确认时，某一批失败了只重试那一批，前面确认过的不重发。"""
    hub.script("ack", Reply(body={"acked": 50, "unknown": []}), err(503, "x"))
    ids = [f"in_{n}" for n in range(60)]

    assert client_for(hub).ack(ids) == 60

    sent = [r.json["ids"] for r in hub.requests_to("ack")]
    assert sent == [ids[:50], ids[50:], ids[50:]]


def test_the_default_number_of_retries_is_one(hub, pauses):
    hub.script("status", err(503, "x"), err(503, "x"), err(503, "x"))

    with pytest.raises(hub_client.HubUnavailable):
        hub_client.HubClient(hub.base_url, KEY).status()

    assert len(hub.requests_to("status")) == 2


# ---------------------------------------------------------------- 日志里不留密钥和正文


def test_a_retry_is_logged_without_the_key_or_the_text(hub, caplog):
    secret_text = "只有我知道的一句话-77b1"
    hub.script("messages", err(503, "shutting_down"))
    caplog.set_level(logging.DEBUG, logger="clawbot_hub_client")

    client_for(hub).send(secret_text, dedupe_key="k-secret-dedupe")

    warnings = [r for r in caplog.records if r.name == "clawbot_hub_client" and r.levelno >= 30]
    assert [r.levelno for r in warnings] == [logging.WARNING]
    assert KEY not in caplog.text and secret_text not in caplog.text
    assert "k-secret-dedupe" not in caplog.text


def test_the_client_logger_has_a_null_handler_so_the_library_never_prints_by_itself():
    handlers = logging.getLogger("clawbot_hub_client").handlers

    assert any(isinstance(handler, logging.NullHandler) for handler in handlers)
