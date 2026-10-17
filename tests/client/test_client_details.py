"""单文件客户端的小处（任务 D4，改坏检查补出来的）。

故意改坏客户端的逻辑，看测试会不会红：固定的数有没有真被用上、异常和数据类的形状、字段类型不对
时怎么处理、错误正文不是约定形状时的样子、日志里怎么描述一次失败。大块的行为在
`test_client.py`、`test_client_errors.py`、`test_run_inbox.py`。
"""

from __future__ import annotations

import dataclasses
import http.client
import logging
import socket
import threading
import time

import clawbot_hub_client as hub_client
import pytest

from tests.client.conftest import KEY, error_body, inbound, inbox_body, result_body
from tests.support.fakehttp import Reply, hang, reset

JSON = "application/json"


def client_for(hub, **kwargs) -> hub_client.HubClient:
    return hub_client.HubClient(hub.base_url, KEY, **kwargs)


# ---------------------------------------------------------------- 固定的数真被用上


def test_connecting_is_given_5_seconds(monkeypatch, hub, client):
    given = []
    real = http.client.HTTPConnection

    class Spy(real):
        def __init__(self, *args, **kwargs):
            given.append(kwargs.get("timeout"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(http.client, "HTTPConnection", Spy)

    client.health()

    assert given == [5.0] and hub_client.CONNECT_TIMEOUT_SECONDS == 5.0


def test_the_heartbeat_thread_gets_2_seconds_to_wrap_up_after_each_item(monkeypatch, hub, client):
    joined = []
    real_join = threading.Thread.join

    def spy(self, timeout=None):
        if self.name == "clawbot-hub-heartbeat":
            joined.append(timeout)
        return real_join(self, timeout)

    monkeypatch.setattr(threading.Thread, "join", spy)
    hub.script("inbox", Reply(body=inbox_body(inbound("in_1"))))

    client.run_inbox(
        lambda item: None,
        stop=lambda: len(hub.requests_to("ack")) >= 1,
        sleep=lambda seconds: None,
        heartbeat_seconds=3600,
    )

    assert joined == [2.0] and hub_client.HEARTBEAT_JOIN_SECONDS == 2.0


def test_the_pause_between_retries_really_sleeps(monkeypatch):
    slept = []
    monkeypatch.setattr(hub_client.time, "sleep", slept.append)

    hub_client._sleep(1.5)

    assert slept == [1.5]


@pytest.mark.parametrize(
    ("endpoint", "call"),
    [
        ("health", lambda c: c.health()),
        ("status", lambda c: c.status()),
        ("ack", lambda c: c.ack(["in_1"])),
        ("inbox", lambda c: c._beat()),
    ],
    ids=["health", "status", "ack", "heartbeat"],
)
def test_a_call_without_a_longer_read_timeout_of_its_own_waits_as_long_as_timeout(
    hub, endpoint, call
):
    hub.script(endpoint, hang(30))
    started = time.monotonic()

    with pytest.raises(hub_client.HubUnavailable) as info:
        call(client_for(hub, timeout=0.3, retries=0))

    assert time.monotonic() - started < 5
    assert info.value.possibly_sent is True


class ClosingSpy(http.client.HTTPConnection):
    """记下被建出来的连接，和每个连接关了几次。"""

    made: list[ClosingSpy] = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.closed_times = 0
        type(self).made.append(self)

    def close(self):
        self.closed_times += 1
        super().close()


@pytest.fixture
def spy(monkeypatch):
    ClosingSpy.made = []
    monkeypatch.setattr(http.client, "HTTPConnection", ClosingSpy)
    return ClosingSpy.made


def test_a_connection_is_closed_after_a_normal_call(hub, client, spy):
    client.health()

    assert [c.closed_times >= 1 for c in spy] == [True]


def test_a_connection_is_closed_after_a_read_timeout(hub, spy):
    hub.script("health", hang(30))

    with pytest.raises(hub_client.HubUnavailable):
        client_for(hub, timeout=0.2, retries=0).health()

    assert [c.closed_times >= 1 for c in spy] == [True]


def test_a_connection_is_closed_after_the_service_reset_it(hub, spy):
    hub.script("health", reset())

    with pytest.raises(hub_client.HubUnavailable):
        client_for(hub, retries=0).health()

    assert [c.closed_times >= 1 for c in spy] == [True]


def test_a_connection_that_could_not_be_made_is_closed_too(spy):
    with pytest.raises(hub_client.HubUnavailable):
        hub_client.HubClient(f"http://127.0.0.1:{free_port()}", KEY, retries=0).health()

    assert [c.closed_times >= 1 for c in spy] == [True]


def free_port() -> int:
    """一个现在没人监听的本机端口（绑定、取号、关掉）。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.parametrize(
    ("host", "written"),
    [("127.0.0.1", "127.0.0.1"), ("localhost", "localhost"), ("[::1]", "[::1]")],
)
def test_a_connect_failure_names_the_host_and_port_it_tried(host, written):
    port = free_port()

    with pytest.raises(hub_client.HubUnavailable) as info:
        hub_client.HubClient(f"http://{host}:{port}", KEY, retries=0).health()

    assert f"{written}:{port}" in info.value.message
    assert info.value.possibly_sent is False


def test_a_broken_connection_names_the_host_and_port_it_was_talking_to(hub):
    hub.script("health", reset())

    with pytest.raises(hub_client.HubUnavailable) as info:
        client_for(hub, retries=0).health()

    assert f"127.0.0.1:{hub.port}" in info.value.message


# ---------------------------------------------------------------- 异常和数据类的形状


@pytest.mark.parametrize(
    "cls",
    [hub_client.HubError, hub_client.HubUnavailable, hub_client.HubAuthError]
    + [hub_client.HubRequestError],
)
def test_the_text_of_an_exception_is_its_message(cls):
    exc = cls("出错了")

    assert str(exc) == "出错了" and exc.message == "出错了" and exc.args == ("出错了",)


def test_the_result_and_the_item_cannot_be_changed_afterwards():
    result = hub_client.SendResult(status="sent")
    item = hub_client.Inbound.from_json(inbound())

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.status = "failed"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        item.text = "改了"  # type: ignore[misc]


def test_a_result_made_by_hand_starts_empty():
    result = hub_client.SendResult(status="sent")

    assert dataclasses.asdict(result) == {
        "status": "sent",
        "reason_code": None,
        "reason": None,
        "parts": 0,
        "parts_sent": 0,
        "duplicate": False,
        "id": None,
        "quota": {},
    }
    assert result.quota is not hub_client.SendResult(status="sent").quota  # 各用各的字典


WRONG_TYPES = [5, 1.5, True, None, ["x"], {"a": 1}]


@pytest.mark.parametrize("value", WRONG_TYPES)
def test_a_text_field_of_the_wrong_type_is_left_out_of_the_result(value):
    result = hub_client.SendResult.from_json(
        result_body("sent", reason_code=value, reason=value, id=value)
    )

    assert (result.reason_code, result.reason, result.id) == (None, None, None)


@pytest.mark.parametrize("value", WRONG_TYPES)
def test_a_text_field_of_the_wrong_type_is_empty_in_an_inbound_item(value):
    item = hub_client.Inbound.from_json(
        inbound("in_1", text=value, at=value, received_at=value, via=value, reply_handle=value)
    )

    assert (item.text, item.at, item.received_at, item.via, item.reply_handle) == ("",) * 5


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, True),
        (0.5, True),
        (1e-9, True),
        (0, False),
        (-1, False),
        (float("inf"), False),
        (float("nan"), False),
        (True, False),
        ("5", False),
        (None, False),
    ],
)
def test_a_number_counts_when_it_is_positive_finite_and_not_a_boolean(value, expected):
    assert hub_client._positive_finite(value) is expected


def test_the_default_stop_never_stops():
    assert hub_client._never_stop() is False


def test_a_pause_for_rate_limiting_follows_the_service_but_never_spins(monkeypatch):
    """`Retry-After` 说几秒就睡几秒；没说睡 60 秒；说 0 也睡一小会儿。两个固定的数换成别的值，
    才分得出"睡的是服务说的数"还是"睡的是保底的数"。"""
    monkeypatch.setattr(hub_client, "IDLE_SLEEP_SECONDS", 0.25)
    monkeypatch.setattr(hub_client, "RATE_LIMITED_SLEEP_SECONDS", 7.5)
    slept: list[float] = []

    for after in (None, 0, 1, 2, 90):
        error = hub_client.HubRequestError("x", http_status=429, retry_after=after)
        hub_client._rate_limit_pause(error, slept.append)

    assert slept == [7.5, 0.25, 1.0, 2.0, 90.0]


def test_a_request_error_that_is_not_rate_limiting_is_not_slept_off():
    slept: list[float] = []
    error = hub_client.HubRequestError("x", http_status=400, retry_after=5)

    with pytest.raises(hub_client.HubRequestError) as info:
        hub_client._rate_limit_pause(error, slept.append)

    assert info.value is error and slept == []


def test_the_heartbeat_context_runs_a_daemon_thread_only_while_inside(client):
    beat = hub_client._Heartbeat(client, 3600)

    with beat as entered:
        assert entered is beat
        [thread] = [t for t in threading.enumerate() if t.name == "clawbot-hub-heartbeat"]
        assert thread.daemon and thread.is_alive()

    assert not thread.is_alive()


@pytest.mark.parametrize(
    "address", [None, 5, 1.5, b"http://127.0.0.1:8790", ["http://127.0.0.1:8790"]]
)
def test_an_address_that_is_not_text_is_refused_at_once(address):
    with pytest.raises(ValueError, match="base_url"):
        hub_client.HubClient(address, KEY)


@pytest.mark.parametrize(
    "address",
    [
        "http://user@127.0.0.1:8790",
        "http://:s3cr3t@127.0.0.1:8790",
        "http://user:s3cr3t@127.0.0.1:8790",
    ],
)
def test_an_address_with_a_user_name_or_a_password_is_refused_without_repeating_it(address):
    with pytest.raises(ValueError) as info:
        hub_client.HubClient(address, KEY)

    assert "s3cr3t" not in str(info.value) and "user" not in str(info.value)


@pytest.mark.parametrize(
    ("address", "parts"),
    [
        ("http://hub.local", ("http", "hub.local", 80, "")),
        ("https://hub.example", ("https", "hub.example", 443, "")),
        ("HTTPS://Hub.Example:8443/prefix/", ("https", "hub.example", 8443, "/prefix")),
        ("http://hub.local:81///", ("http", "hub.local", 81, "")),
        ("http://[::1]:9000/a/b", ("http", "::1", 9000, "/a/b")),
    ],
)
def test_an_address_is_taken_apart_into_scheme_host_port_and_prefix(address, parts):
    assert hub_client._parse_base_url(address) == parts


def test_a_handler_that_returns_counts_as_handled_and_one_that_raises_does_not(client):
    item = hub_client.Inbound.from_json(inbound("in_1"))

    assert client._handle(lambda _: None, item, 3600) is True

    def failing(_):
        raise RuntimeError("处理不了")

    assert client._handle(failing, item, 3600) is False


# ---------------------------------------------------------------- 请求体


def test_the_body_goes_out_as_raw_utf_8_and_not_as_escapes(hub, client):
    client.send("你好 ✓")

    [request] = hub.requests
    assert "你好 ✓".encode() in request.body
    assert b"\\u" not in request.body


# ---------------------------------------------------------------- 错误正文不是约定的形状


STATUS_MESSAGES = [
    (400, hub_client.HubRequestError, "共用服务不接受这个请求（HTTP 400）"),
    (404, hub_client.HubRequestError, "共用服务不接受这个请求（HTTP 404）"),
    (
        401,
        hub_client.HubAuthError,
        "共用服务拒绝了这次请求（HTTP 401）：检查密钥和这台机器能不能连它",
    ),
    (
        403,
        hub_client.HubAuthError,
        "共用服务拒绝了这次请求（HTTP 403）：检查密钥和这台机器能不能连它",
    ),
    (500, hub_client.HubUnavailable, "共用服务出错了（HTTP 500）"),
    (503, hub_client.HubUnavailable, "共用服务出错了（HTTP 503）"),
]

OTHER_SHAPES = [
    b"[1, 2]",
    b'"x"',
    b"5",
    b"null",
    b"true",
    b'{"error": "x"}',
    b'{"error": ["x"]}',
    b'{"error": null}',
    b'{"error": 5}',
    b'{"nope": 1}',
    b'{"error": {}}',
    b"not json",
    b"",
    b"\xff\xfe",
]


@pytest.mark.parametrize(("status", "kind", "plain"), STATUS_MESSAGES)
@pytest.mark.parametrize("raw", OTHER_SHAPES)
def test_an_error_body_of_another_shape_gives_the_plain_message_and_no_code(
    hub, status, kind, plain, raw
):
    hub.script("messages", Reply(status=status, body=raw, content_type=JSON))

    with pytest.raises(kind) as info:
        client_for(hub, retries=0).send("x")

    assert info.value.message == plain and info.value.http_status == status
    if kind is not hub_client.HubUnavailable:
        assert info.value.code is None


@pytest.mark.parametrize(
    "error",
    [
        {"code": 5, "message": 7},
        {"code": "", "message": ""},
        {"code": ["a"], "message": ["b"]},
        {"code": None, "message": None},
        {"code": True, "message": True},
    ],
)
def test_a_code_or_message_that_is_not_text_is_not_used(hub, error):
    hub.script("messages", Reply(status=400, body={"error": error}))

    with pytest.raises(hub_client.HubRequestError) as info:
        client_for(hub).send("x")

    assert info.value.code is None
    assert info.value.message == "共用服务不接受这个请求（HTTP 400）"


def test_the_codes_and_messages_of_the_service_are_used_when_they_are_text(hub):
    hub.script("messages", Reply(status=400, body=error_body("bad_request", "服务说的原因")))

    with pytest.raises(hub_client.HubRequestError) as info:
        client_for(hub).send("x")

    assert (info.value.code, info.value.message) == ("bad_request", "服务说的原因")


# ---------------------------------------------------------------- 回了 200 但看不懂：说明里写什么


def test_an_unparsable_reply_says_nothing_about_a_reason_it_does_not_have(hub):
    hub.script("messages", Reply(body="<html>网关</html>"))

    with pytest.raises(hub_client.HubUnavailable) as info:
        client_for(hub).send("x")

    assert info.value.message == "服务回了 200，但返回的内容看不懂"
    assert info.value.possibly_sent is True and info.value.http_status == 200


@pytest.mark.parametrize(
    ("call", "endpoint", "body", "message"),
    [
        (lambda c: c.send("x"), "messages", [1], "发送结果的内容看不懂（不是 JSON 对象）"),
        (
            lambda c: c.reply("h", "x"),
            "replies",
            [1],
            "回复结果的内容看不懂（不是 JSON 对象）",
        ),
        (lambda c: c.status(), "status", [1], "状态的内容看不懂（不是 JSON 对象）"),
        (lambda c: c.health(), "health", [1], "探活的内容看不懂（不是 JSON 对象）"),
        (lambda c: c.inbox(wait=0), "inbox", {}, "取话的内容看不懂（没有 items 列表）"),
        (
            lambda c: c.inbox(wait=0),
            "inbox",
            {"items": [{}]},
            "取话的内容看不懂（取话返回的一项缺少 id）",
        ),
        (lambda c: c.ack(["a"]), "ack", {"acked": "1"}, "确认的内容看不懂（没有 acked 数）"),
        (lambda c: c.ack(["a"]), "ack", {"acked": True}, "确认的内容看不懂（没有 acked 数）"),
    ],
)
def test_an_unreadable_reply_names_what_could_not_be_read_and_why(
    hub, call, endpoint, body, message
):
    hub.script(endpoint, Reply(body=body))

    with pytest.raises(hub_client.HubUnavailable) as info:
        call(client_for(hub, retries=0))

    assert info.value.message == f"服务回了 200，但{message}"


# ---------------------------------------------------------------- 日志里怎么描述一次失败


def test_a_failure_is_described_by_its_type_and_the_http_status_only():
    brief = hub_client._brief

    assert brief(hub_client.HubUnavailable("原文不该出现", http_status=503)) == (
        "HubUnavailable，HTTP 503"
    )
    assert brief(hub_client.HubUnavailable("原文不该出现")) == "HubUnavailable"
    assert brief(ValueError("原文不该出现")) == "ValueError"


def test_a_retry_after_a_503_says_so_in_the_log(hub, caplog, pauses):
    caplog.set_level(logging.WARNING, logger="clawbot_hub_client")
    hub.script("health", Reply(status=503, body=error_body("shutting_down")))

    client_for(hub).health()

    [record] = [r for r in caplog.records if r.name == "clawbot_hub_client"]
    assert "HubUnavailable，HTTP 503" in record.getMessage()
    assert pauses == [1.0]


def test_a_retry_after_a_failure_without_a_status_leaves_the_status_out_of_the_log(
    hub, caplog, pauses
):
    caplog.set_level(logging.WARNING, logger="clawbot_hub_client")
    hub.script("health", Reply(action="reset"))

    client_for(hub).health()

    [record] = [r for r in caplog.records if r.name == "clawbot_hub_client"]
    assert "HubUnavailable" in record.getMessage() and "HTTP" not in record.getMessage()
