"""单文件客户端的接口：构造、密钥、每个方法发什么收什么、数据类、读超时（任务 D4）。

规格 `hub-client`「接口」。错误分类和重试在 `test_client_errors.py`，取话循环在
`test_run_inbox.py`，和真服务一起跑的在 D5 的 `tests/contract/`。这里的服务是 `conftest.py`
里的假服务（真 HTTP，回复可编排）。
"""

from __future__ import annotations

import dataclasses
import inspect
import re

import clawbot_hub_client as hub_client
import pytest

import clawbot_hub
from tests.client.conftest import (
    KEY,
    OTHER_KEY,
    FakeHub,
    inbound,
    inbox_body,
    result_body,
    status_body,
)
from tests.support.fakehttp import Reply, default_after

pytestmark = pytest.mark.usefixtures("pauses")


# ---------------------------------------------------------------- 常量和签名


def test_the_api_version_is_the_string_the_service_reports():
    assert hub_client.API_VERSION == "1"
    assert hub_client.API_VERSION == clawbot_hub.API_VERSION


def test_the_client_version_moves_together_with_the_service():
    assert hub_client.CLIENT_VERSION == clawbot_hub.__version__
    assert re.fullmatch(r"\d+\.\d+\.\d+", hub_client.CLIENT_VERSION)


def parameters(function) -> list[tuple[str, str, object]]:
    return [
        (p.name, p.kind.name, p.default if p.default is not p.empty else "<必填>")
        for p in inspect.signature(function).parameters.values()
        if p.name != "self"
    ]


def test_the_constructor_has_the_parameters_of_the_spec():
    assert parameters(hub_client.HubClient) == [
        ("base_url", "POSITIONAL_OR_KEYWORD", "http://127.0.0.1:8790"),
        ("key", "POSITIONAL_OR_KEYWORD", None),
        ("key_env", "POSITIONAL_OR_KEYWORD", "CLAWBOT_HUB_KEY"),
        ("timeout", "POSITIONAL_OR_KEYWORD", 35.0),
        ("retries", "POSITIONAL_OR_KEYWORD", 1),
    ]


def test_the_methods_have_the_parameters_of_the_spec():
    client = hub_client.HubClient
    assert parameters(client.send) == [
        ("text", "POSITIONAL_OR_KEYWORD", "<必填>"),
        ("priority", "KEYWORD_ONLY", "normal"),
        ("dedupe_key", "KEYWORD_ONLY", None),
        ("on_closed", "KEYWORD_ONLY", "fail"),
        ("timeout", "KEYWORD_ONLY", None),
    ]
    assert parameters(client.reply) == [
        ("handle", "POSITIONAL_OR_KEYWORD", "<必填>"),
        ("text", "POSITIONAL_OR_KEYWORD", "<必填>"),
        ("awaiting_confirmation", "KEYWORD_ONLY", False),
        ("confirm_seconds", "KEYWORD_ONLY", None),
    ]
    assert parameters(client.inbox) == [
        ("wait", "POSITIONAL_OR_KEYWORD", 25),
        ("max", "POSITIONAL_OR_KEYWORD", 10),
    ]
    assert parameters(client.ack) == [("ids", "POSITIONAL_OR_KEYWORD", "<必填>")]
    assert parameters(client.status) == []
    assert parameters(client.health) == []


def test_run_inbox_has_the_parameters_of_the_spec():
    got = parameters(hub_client.HubClient.run_inbox)

    assert [name for name, _, _ in got] == [
        "handler",
        "stop",
        "wait",
        "max",
        "sleep",
        "heartbeat_seconds",
    ]
    assert got[0][1] == "POSITIONAL_OR_KEYWORD"
    assert all(kind == "KEYWORD_ONLY" for _, kind, _ in got[1:])
    defaults = {name: default for name, _, default in got}
    assert defaults["wait"] == 25 and defaults["max"] == 10
    assert defaults["heartbeat_seconds"] is None
    assert defaults["stop"]() is False
    assert defaults["sleep"] is hub_client.time.sleep


def test_the_documented_fixed_numbers():
    assert hub_client.DEFAULT_SEND_TIMEOUT_SECONDS == 30.0
    assert hub_client.SEND_TIMEOUT_MARGIN == 5.0
    assert hub_client.INBOX_TIMEOUT_MARGIN == 10.0
    assert hub_client.RETRY_INTERVAL_SECONDS == 1.0
    assert hub_client.ACK_BATCH_SIZE == 50
    assert hub_client.DEFAULT_INBOX_ONLINE_SECONDS == 90.0
    assert hub_client.BACKOFF_START_SECONDS == 5.0
    assert hub_client.BACKOFF_MAX_SECONDS == 60.0
    assert hub_client.RATE_LIMITED_SLEEP_SECONDS == 60.0
    assert hub_client.IDLE_SLEEP_SECONDS == 1.0


# ---------------------------------------------------------------- 构造


def test_a_client_built_with_no_arguments_uses_the_documented_defaults(monkeypatch):
    monkeypatch.delenv("CLAWBOT_HUB_KEY", raising=False)

    client = hub_client.HubClient()

    assert client.base_url == "http://127.0.0.1:8790"
    assert client.timeout == 35.0
    assert client.retries == 1
    assert client.key_env == "CLAWBOT_HUB_KEY"


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("http://127.0.0.1:8790/", "http://127.0.0.1:8790"),
        ("http://127.0.0.1:8790///", "http://127.0.0.1:8790"),
        ("http://hub.local", "http://hub.local"),
        ("https://hub.example:8443/prefix/", "https://hub.example:8443/prefix"),
        ("HTTP://Hub.Example:81", "HTTP://Hub.Example:81"),
        ("http://[::1]:8790", "http://[::1]:8790"),
    ],
)
def test_the_base_address_is_kept_without_its_trailing_slashes(given, kept):
    assert hub_client.HubClient(given, KEY).base_url == kept


@pytest.mark.parametrize(
    "address",
    [
        "",
        "127.0.0.1:8790",
        "localhost",
        "ftp://hub.example",
        "http://",
        "http:///v1",
        "http://[::1",
    ],
)
def test_an_address_that_is_not_http_or_https_with_a_host_is_refused_at_once(address):
    with pytest.raises(ValueError) as info:
        hub_client.HubClient(address, KEY)

    assert "base_url" in str(info.value)
    assert KEY not in str(info.value)


@pytest.mark.parametrize("retries", [-1, 1.5, True, "1", None])
def test_retries_must_be_a_whole_number_not_below_zero(retries):
    with pytest.raises(ValueError, match="retries"):
        hub_client.HubClient("http://127.0.0.1:1", KEY, retries=retries)


@pytest.mark.parametrize("timeout", [0, -1, "35", None, True, float("nan"), float("inf")])
def test_timeout_must_be_a_positive_number(timeout):
    with pytest.raises(ValueError, match="timeout"):
        hub_client.HubClient("http://127.0.0.1:1", KEY, timeout=timeout)


def test_zero_retries_and_small_timeouts_are_fine():
    client = hub_client.HubClient("http://127.0.0.1:1", KEY, timeout=0.05, retries=0)

    assert (client.timeout, client.retries) == (0.05, 0)


def test_the_text_form_of_the_client_never_shows_the_key():
    client = hub_client.HubClient("http://127.0.0.1:8790", KEY)

    assert KEY not in repr(client) and KEY not in str(client)
    assert "http://127.0.0.1:8790" in repr(client)


def test_a_client_can_be_built_before_anything_is_listening():
    """项目里常有在导入时就构造的全局客户端：构造不连接、不读密钥。"""
    client = hub_client.HubClient("http://127.0.0.1:9", None, key_env="NO_SUCH_VARIABLE_FOR_HUB")

    assert client.base_url == "http://127.0.0.1:9"


# ---------------------------------------------------------------- 密钥

CALLS = {
    "send": lambda c: c.send("你好"),
    "reply": lambda c: c.reply("h_1", "好的"),
    "inbox": lambda c: c.inbox(wait=0),
    "ack": lambda c: c.ack(["in_1"]),
    "status": lambda c: c.status(),
}


@pytest.mark.parametrize("name", CALLS)
def test_without_a_key_the_first_call_raises_and_nothing_is_sent(hub, monkeypatch, name):
    monkeypatch.delenv("CLAWBOT_HUB_KEY", raising=False)
    client = hub_client.HubClient(hub.base_url)  # 构造不抛

    with pytest.raises(hub_client.HubAuthError) as info:
        CALLS[name](client)

    assert hub.requests == []
    assert info.value.code is None and info.value.http_status is None
    assert "CLAWBOT_HUB_KEY" in str(info.value)  # 告诉调用方该设哪个环境变量


def test_the_error_names_the_environment_variable_that_was_asked_for(hub, monkeypatch):
    monkeypatch.delenv("MY_HUB_KEY", raising=False)
    client = hub_client.HubClient(hub.base_url, key_env="MY_HUB_KEY")

    with pytest.raises(hub_client.HubAuthError) as info:
        client.send("你好")

    assert "MY_HUB_KEY" in str(info.value) and "CLAWBOT_HUB_KEY" not in str(info.value)


def test_the_key_comes_from_the_default_environment_variable(hub, monkeypatch):
    monkeypatch.setenv("CLAWBOT_HUB_KEY", OTHER_KEY)

    hub_client.HubClient(hub.base_url).send("你好")

    assert hub.requests_to("messages")[0].header("Authorization") == f"Bearer {OTHER_KEY}"


def test_the_key_can_come_from_another_environment_variable(hub, monkeypatch):
    monkeypatch.setenv("MY_HUB_KEY", OTHER_KEY)
    monkeypatch.setenv("CLAWBOT_HUB_KEY", KEY)

    hub_client.HubClient(hub.base_url, key_env="MY_HUB_KEY").send("你好")

    assert hub.requests_to("messages")[0].header("Authorization") == f"Bearer {OTHER_KEY}"


def test_a_key_given_directly_wins_over_the_environment(hub, monkeypatch):
    monkeypatch.setenv("CLAWBOT_HUB_KEY", OTHER_KEY)

    hub_client.HubClient(hub.base_url, KEY).send("你好")

    assert hub.requests_to("messages")[0].header("Authorization") == f"Bearer {KEY}"


def test_the_environment_is_read_when_calling_not_when_building(hub, monkeypatch):
    monkeypatch.delenv("CLAWBOT_HUB_KEY", raising=False)
    client = hub_client.HubClient(hub.base_url)
    with pytest.raises(hub_client.HubAuthError):
        client.send("先试一次")

    monkeypatch.setenv("CLAWBOT_HUB_KEY", KEY)
    client.send("现在有了")

    assert [r.json["text"] for r in hub.requests_to("messages")] == ["现在有了"]


@pytest.mark.parametrize("blank", ["", "   ", "\n", "\t "])
def test_a_blank_key_counts_as_no_key(hub, monkeypatch, blank):
    monkeypatch.setenv("CLAWBOT_HUB_KEY", OTHER_KEY)  # 就算环境变量里有，直接给的空串也不回头去读它
    client = hub_client.HubClient(hub.base_url, blank)

    with pytest.raises(hub_client.HubAuthError):
        client.send("你好")

    assert hub.requests == []


def test_a_blank_environment_value_counts_as_no_key(hub, monkeypatch):
    monkeypatch.setenv("CLAWBOT_HUB_KEY", "  \n")

    with pytest.raises(hub_client.HubAuthError):
        hub_client.HubClient(hub.base_url).send("你好")

    assert hub.requests == []


def test_whitespace_around_a_key_is_dropped(hub):
    """密钥常从 `.env` 或文件里读出来，末尾带换行。"""
    hub_client.HubClient(hub.base_url, f"  {KEY}\n").send("你好")

    assert hub.requests_to("messages")[0].header("Authorization") == f"Bearer {KEY}"


@pytest.mark.parametrize(
    "bad",
    ["chub_two words", "chub_line\nbreak", "chub_tab\tinside", "密钥chub_abc", "chub_\x00nul"],
)
def test_a_key_that_cannot_be_written_in_a_header_is_refused_without_showing_it(hub, bad):
    client = hub_client.HubClient(hub.base_url, bad)

    with pytest.raises(hub_client.HubAuthError) as info:
        client.send("你好")

    assert hub.requests == []
    assert bad not in str(info.value) and bad.strip() not in str(info.value)
    assert repr(bad) not in str(info.value)


# ---------------------------------------------------------------- 请求的样子


def test_a_post_carries_the_key_and_says_what_it_sends(hub, client):
    client.send("盘点完成")

    [request] = hub.requests
    assert (request.method, request.path) == ("POST", "/v1/messages")
    assert request.header("Authorization") == f"Bearer {KEY}"
    assert request.header("Content-Type") == "application/json; charset=utf-8"
    assert request.header("Accept") == "application/json"
    assert request.header("User-Agent") == f"clawbot-hub-client/{hub_client.CLIENT_VERSION}"
    assert request.header("Connection") == "close"
    assert sorted(request.custom_header_names()) == ["Authorization", "Content-Type"]


def test_a_get_has_no_body_and_no_content_type(hub, client):
    client.inbox(wait=0)

    [request] = hub.requests
    assert (request.method, request.body) == ("GET", b"")
    assert request.custom_header_names() == ["Authorization"]


def test_every_call_opens_its_own_connection(hub, client):
    """不留长连接：隔了很久再用时不会撞上服务端已经关掉的空闲连接。"""
    client.status()
    client.status()
    client.send("你好")

    assert len({r.connection_id for r in hub.requests}) == 3


def test_a_base_address_with_a_path_prefix_is_put_in_front_of_every_path():
    class Prefixed(FakeHub):
        def route(self, path):
            return super().route(path.removeprefix("/hub")) if path.startswith("/hub/") else None

    with Prefixed() as fake:
        client = hub_client.HubClient(fake.base_url + "/hub/", KEY)
        client.send("你好")
        client.inbox(wait=0)
        client.ack(["in_1"])
        client.status()
        client.health()

        assert [r.path for r in fake.requests] == [
            "/hub/v1/messages",
            "/hub/v1/inbox",
            "/hub/v1/inbox/ack",
            "/hub/v1/status",
            "/hub/v1/health",
        ]


def test_the_body_is_utf_8_json_with_the_text_unchanged(hub, client):
    text = '标题\n正文 ✓ 🚀 “引号” \\ " \t 末行'

    client.send(text)

    [request] = hub.requests
    assert request.json["text"] == text
    assert request.body.decode("utf-8")  # 整个请求体是合法的 UTF-8


def test_text_with_a_lone_surrogate_still_goes_out_as_valid_json(hub, client):
    """这种文字服务会回 400；客户端自己不能先抛 `UnicodeEncodeError`，免得错误种类乱掉。"""
    text = "文件名 \udcff 坏了"
    hub.script(
        "messages", Reply(status=400, body={"error": {"code": "bad_request", "message": "x"}})
    )

    with pytest.raises(hub_client.HubRequestError) as info:
        client_for(hub).send(text)

    assert info.value.code == "bad_request"
    assert hub.requests_to("messages")[0].json["text"] == text


def client_for(hub, **kwargs) -> hub_client.HubClient:
    return hub_client.HubClient(hub.base_url, KEY, **kwargs)


# ---------------------------------------------------------------- send


def test_send_posts_the_text_with_the_documented_defaults(hub, client):
    result = client.send("盘点完成")

    [request] = hub.requests
    assert request.json == {"text": "盘点完成", "priority": "normal", "on_closed": "fail"}
    assert isinstance(result, hub_client.SendResult)
    assert result.status == "sent" and result.ok and not result.needs_fallback
    assert not result.maybe_delivered


def test_send_passes_priority_dedupe_key_and_on_closed_through(hub, client):
    client.send("早报", priority="urgent", dedupe_key="brief-2026-10-12", on_closed="skip")

    assert hub.requests[0].json == {
        "text": "早报",
        "priority": "urgent",
        "dedupe_key": "brief-2026-10-12",
        "on_closed": "skip",
    }


def test_send_leaves_out_the_dedupe_key_when_there_is_none(hub, client):
    client.send("早报", dedupe_key=None)

    assert "dedupe_key" not in hub.requests[0].json


def test_the_result_has_every_field_of_the_reply(hub, client):
    body = result_body(
        "partial",
        reason_code="part_failed",
        reason="第 2 段没发出去",
        parts=3,
        parts_sent=1,
        duplicate=True,
        id="o_abc",
        quota={"left": 3, "limit": 10, "window_ends_at": None},
    )
    hub.script("messages", Reply(body=body))

    result = client.send("很长的文字")

    assert result == hub_client.SendResult(
        status="partial",
        reason_code="part_failed",
        reason="第 2 段没发出去",
        parts=3,
        parts_sent=1,
        duplicate=True,
        id="o_abc",
        quota={"left": 3, "limit": 10, "window_ends_at": None},
    )


def test_the_result_fields_are_exactly_the_fields_the_service_returns():
    assert {f.name for f in dataclasses.fields(hub_client.SendResult)} == set(result_body())


def test_a_rejected_send_is_a_result_and_not_an_exception(hub, client):
    hub.script(
        "messages",
        Reply(
            body=result_body(
                "rejected",
                reason_code="window_closed",
                reason="对话窗口已过期：给 ClawBot 发一句话",
                parts_sent=0,
                id=None,
            )
        ),
    )

    result = client.send("早报")

    assert result.status == "rejected" and result.reason_code == "window_closed"
    assert result.reason == "对话窗口已过期：给 ClawBot 发一句话"
    assert result.needs_fallback and not result.ok and not result.maybe_delivered


def test_a_skipped_send_counts_as_fine(hub, client):
    hub.script(
        "messages",
        Reply(body=result_body("skipped", reason_code="window_closed", reason="窗口过期，已跳过")),
    )

    result = client.send("可有可无的提醒", on_closed="skip")

    assert result.status == "skipped" and result.ok and not result.needs_fallback


STATUS_TABLE = [
    # 服务给的 status, ok, needs_fallback, maybe_delivered
    ("sent", True, False, False),
    ("skipped", True, False, False),
    ("rejected", False, True, False),
    ("failed", False, True, False),
    ("unconfirmed", False, True, True),
    ("partial", False, True, True),
    ("queued", False, True, False),  # 客户端不认识的状态：走兜底，也不说"可能已经到了"
    ("", False, True, False),
    ("SENT", False, True, False),  # 大小写不同也是不认识
]


@pytest.mark.parametrize(("status", "ok", "fallback", "maybe"), STATUS_TABLE)
def test_what_each_status_means_for_the_caller(hub, client, status, ok, fallback, maybe):
    hub.script("messages", Reply(body=result_body(status)))

    result = client.send("你好")

    assert (result.ok, result.needs_fallback, result.maybe_delivered) == (ok, fallback, maybe)
    assert result.status == status


def test_a_result_without_a_status_is_not_trusted(hub, client):
    body = result_body()
    del body["status"]
    hub.script("messages", Reply(body=body))

    result = client.send("你好")

    assert result.needs_fallback and not result.ok


def test_fields_the_client_does_not_know_are_ignored(hub, client):
    body = result_body()
    body["future_field"] = {"anything": [1, 2]}
    body["quota"]["extra"] = True
    hub.script("messages", Reply(body=body))

    result = client.send("你好")

    assert result.ok and result.quota["extra"] is True  # quota 原样给，里面多的东西也不丢


def test_a_result_with_fewer_fields_than_usual_still_works(hub, client):
    hub.script("messages", Reply(body={"status": "sent"}))

    result = client.send("你好")

    assert result.ok and result.parts == 0 and result.quota == {} and result.id is None


@pytest.mark.parametrize("raw", [b"[]", b'"sent"', b"5", b"null", b"true"])
def test_a_reply_that_is_not_a_json_object_is_an_unavailable_service(hub, client, raw):
    hub.script("messages", Reply(body=raw, content_type="application/json"))

    with pytest.raises(hub_client.HubUnavailable) as info:
        client.send("你好")

    assert info.value.possibly_sent is True  # 服务回了 200，请求已经被处理过


# ---------------------------------------------------------------- reply


def test_reply_posts_the_handle_and_text(hub, client):
    result = client.reply("h_in_1", "收到")

    [request] = hub.requests
    assert (request.method, request.path) == ("POST", "/v1/replies")
    assert request.json == {"handle": "h_in_1", "text": "收到", "awaiting_confirmation": False}
    assert isinstance(result, hub_client.SendResult) and result.ok


def test_reply_can_wait_for_the_users_confirmation(hub, client):
    client.reply("h_in_1", "确认转账？回“对”", awaiting_confirmation=True, confirm_seconds=300)

    assert hub.requests[0].json == {
        "handle": "h_in_1",
        "text": "确认转账？回“对”",
        "awaiting_confirmation": True,
        "confirm_seconds": 300,
    }


def test_reply_leaves_out_confirm_seconds_when_not_given(hub, client):
    client.reply("h_in_1", "好", awaiting_confirmation=True)

    assert "confirm_seconds" not in hub.requests[0].json


def test_a_reply_result_reads_like_a_send_result(hub, client):
    hub.script("replies", Reply(body=result_body("unconfirmed", reason="发出去了但没收到回执")))

    result = client.reply("h_in_1", "好")

    assert result.status == "unconfirmed" and result.maybe_delivered and result.needs_fallback


# ---------------------------------------------------------------- inbox


def test_inbox_asks_for_25_seconds_and_10_items_by_default(hub, client):
    assert client.inbox() == []

    [request] = hub.requests
    assert (request.method, request.path) == ("GET", "/v1/inbox")
    assert request.target == "/v1/inbox?wait=25&max=10"


def test_inbox_passes_wait_and_max_through(hub, client):
    client.inbox(wait=0, max=50)
    client.inbox(3, 1)

    assert [r.target for r in hub.requests] == [
        "/v1/inbox?wait=0&max=50",
        "/v1/inbox?wait=3&max=1",
    ]


def test_inbox_returns_the_items_as_dataclasses(hub, client):
    hub.script(
        "inbox",
        Reply(
            body=inbox_body(
                inbound("in_1", "你好"),
                inbound("in_2", "", has_text=False, attempt=2, via="confirm"),
            )
        ),
    )

    first, second = client.inbox()

    assert first == hub_client.Inbound(
        id="in_1",
        text="你好",
        has_text=True,
        at="2026-10-12T10:00:00+08:00",
        at_ms=1_791_770_400_000,
        received_at="2026-10-12T10:00:01+08:00",
        attempt=1,
        via="route",
        reply_handle="h_in_1",
    )
    assert (second.id, second.has_text, second.attempt, second.via) == ("in_2", False, 2, "confirm")


def test_the_inbound_fields_are_exactly_the_fields_the_service_returns():
    assert {f.name for f in dataclasses.fields(hub_client.Inbound)} == set(inbound())


def test_unknown_fields_in_the_inbox_reply_are_ignored(hub, client):
    body = inbox_body(inbound("in_1", future="x"))
    body["future_top_level"] = [1]
    hub.script("inbox", Reply(body=body))

    [item] = client.inbox()

    assert item.id == "in_1"


@pytest.mark.parametrize(
    "body",
    [
        {"items": "x"},
        {"items": [1]},
        {"items": [{"text": "没有编号"}]},
        {"items": None},
        {},
        [],
    ],
)
def test_an_inbox_reply_that_makes_no_sense_is_an_unavailable_service(hub, client, body):
    hub.script("inbox", Reply(body=body))

    with pytest.raises(hub_client.HubUnavailable):
        client_for(hub, retries=0).inbox(wait=0)


def test_an_inbound_item_keeps_the_exact_text(hub, client):
    text = "  前后有空格\n和换行 😀  "
    hub.script("inbox", Reply(body=inbox_body(inbound("in_1", text))))

    [item] = client.inbox()

    assert item.text == text


# ---------------------------------------------------------------- ack


def test_ack_posts_the_ids_and_returns_how_many_were_acknowledged(hub, client):
    assert client.ack(["in_1", "in_2"]) == 2

    [request] = hub.requests
    assert (request.method, request.path) == ("POST", "/v1/inbox/ack")
    assert request.json == {"ids": ["in_1", "in_2"]}


def test_ack_returns_the_service_count_not_the_number_sent(hub, client):
    hub.script("ack", Reply(body={"acked": 1, "unknown": ["in_2"]}))

    assert client.ack(["in_1", "in_2"]) == 1


def test_ack_with_nothing_to_acknowledge_sends_nothing(hub, client):
    assert client.ack([]) == 0
    assert client.ack(()) == 0
    assert client.ack(iter([])) == 0

    assert hub.requests == []


def test_ack_takes_any_iterable_of_ids(hub, client):
    assert client.ack(f"in_{n}" for n in range(3)) == 3
    assert client.ack(("a", "b")) == 2

    assert [r.json["ids"] for r in hub.requests] == [["in_0", "in_1", "in_2"], ["a", "b"]]


def test_ack_splits_120_ids_into_50_50_20(hub, client):
    ids = [f"in_{n}" for n in range(120)]

    assert client.ack(ids) == 120

    sent = [r.json["ids"] for r in hub.requests_to("ack")]
    assert [len(batch) for batch in sent] == [50, 50, 20]
    assert [i for batch in sent for i in batch] == ids  # 顺序不变，一个不少


@pytest.mark.parametrize(
    ("count", "sizes"),
    [(1, [1]), (49, [49]), (50, [50]), (51, [50, 1]), (100, [50, 50]), (101, [50, 50, 1])],
)
def test_ack_batches_at_the_boundaries(hub, client, count, sizes):
    client.ack([f"in_{n}" for n in range(count)])

    assert [len(r.json["ids"]) for r in hub.requests_to("ack")] == sizes


def test_ack_adds_up_what_each_batch_reports(hub, client):
    hub.script(
        "ack",
        Reply(body={"acked": 50, "unknown": []}),
        Reply(body={"acked": 47, "unknown": ["x", "y", "z"]}),
        Reply(body={"acked": 0, "unknown": []}),
    )

    assert client.ack([f"in_{n}" for n in range(120)]) == 97


def test_ack_stops_at_the_first_failing_batch(hub):
    hub.script("ack", Reply(body={"acked": 50, "unknown": []}), Reply(status=400, body=None))
    client = client_for(hub)

    with pytest.raises(hub_client.HubRequestError):
        client.ack([f"in_{n}" for n in range(120)])

    assert len(hub.requests_to("ack")) == 2  # 第三批没有发


def test_ack_treats_a_missing_count_as_a_broken_reply(hub):
    hub.script("ack", Reply(body={"unknown": []}))

    with pytest.raises(hub_client.HubUnavailable):
        client_for(hub, retries=0).ack(["in_1"])


# ---------------------------------------------------------------- status 和 health


def test_status_returns_the_json_as_it_came(hub, client):
    body = status_body()
    body["future"] = {"x": 1}
    hub.script("status", Reply(body=body))

    assert client.status() == body

    [request] = hub.requests
    assert (request.method, request.path) == ("GET", "/v1/status")
    assert request.header("Authorization") == f"Bearer {KEY}"


def test_health_returns_the_json_and_needs_no_key(hub, monkeypatch):
    monkeypatch.delenv("CLAWBOT_HUB_KEY", raising=False)

    body = hub_client.HubClient(hub.base_url).health()

    assert body == {"ok": True, "version": "0.1.0", "api": "1"}
    [request] = hub.requests
    assert (request.method, request.path) == ("GET", "/v1/health")
    assert request.header("Authorization") is None  # 探活用不着密钥，也就不发


def test_health_does_not_send_a_key_even_when_there_is_one(hub, client):
    client.health()

    assert hub.requests[0].header("Authorization") is None


@pytest.mark.parametrize("raw", [b"[]", b'"ok"', b"1"])
def test_status_and_health_replies_must_be_json_objects(hub, raw):
    hub.script("status", Reply(body=raw, content_type="application/json"))
    hub.script("health", Reply(body=raw, content_type="application/json"))
    client = client_for(hub, retries=0)

    with pytest.raises(hub_client.HubUnavailable):
        client.status()
    with pytest.raises(hub_client.HubUnavailable):
        client.health()


# ---------------------------------------------------------------- 读超时的算法


def test_the_send_read_timeout_is_the_larger_of_timeout_and_the_service_deadline_plus_5(hub):
    client = client_for(hub)  # timeout 35

    assert client._send_read_timeout(None) == 35.0  # max(35, 默认 30 + 5)
    assert client._send_read_timeout(100) == 100  # 想等得更久可以
    assert client._send_read_timeout(1) == 35.0  # 想等得更短没用：服务可能真要那么久


def test_the_send_read_timeout_follows_the_deadline_the_service_reported(hub):
    hub.script("status", Reply(body=status_body(send_timeout=60)))
    client = client_for(hub)

    client.status()

    assert client._send_read_timeout(None) == 65.0
    assert client._send_read_timeout(100) == 100


def test_a_shorter_deadline_than_the_default_lowers_the_floor_of_the_timeout(hub):
    hub.script("status", Reply(body=status_body(send_timeout=2)))
    client = client_for(hub, timeout=10)
    assert client._send_read_timeout(None) == 35.0  # 还没问过服务：按默认的 30 秒算，30 + 5

    client.status()

    assert client._send_read_timeout(None) == 10.0  # max(10, 2 + 5)


@pytest.mark.parametrize("reported", [None, "30", 0, -5, True, [], {}])
def test_a_deadline_that_makes_no_sense_is_not_remembered(hub, reported):
    body = status_body()
    body["service"]["send_timeout_seconds"] = reported
    hub.script("status", Reply(body=body))
    client = client_for(hub, timeout=1)

    client.status()

    assert client._send_read_timeout(None) == 35.0  # max(1, 默认 30 + 5)


def test_a_status_reply_without_the_service_section_changes_nothing(hub):
    hub.script("status", Reply(body={"login": {"state": "ok"}}))
    client = client_for(hub, timeout=1)

    client.status()

    assert client._send_read_timeout(None) == 35.0


def test_the_inbox_read_timeout_is_the_larger_of_timeout_and_wait_plus_10(hub):
    client = client_for(hub)  # timeout 35

    assert client._inbox_read_timeout(0) == 35.0
    assert client._inbox_read_timeout(25) == 35.0
    assert client._inbox_read_timeout(26) == 36.0
    assert client._inbox_read_timeout(55) == 65.0
    assert client_for(hub, timeout=100)._inbox_read_timeout(55) == 100


def test_a_long_inbox_wait_does_not_hit_the_read_timeout(hub, quick):
    """规格里"等 55 秒的取话不会读超时"的缩小版：timeout 0.3 秒，wait 1 秒，服务 0.6 秒才回。"""
    hub.script("inbox", default_after(0.6))
    client = client_for(hub, timeout=0.3, retries=0)

    assert client.inbox(wait=1) == []


def test_without_the_wait_the_same_slow_reply_does_time_out(hub, quick):
    hub.script("inbox", default_after(0.6))
    client = client_for(hub, timeout=0.3, retries=0)

    with pytest.raises(hub_client.HubUnavailable):
        client.inbox(wait=0)


def test_a_send_waits_as_long_as_the_service_said_it_may_take(hub, quick):
    """规格里"总时限 60 秒，第 50 秒才回话"的缩小版：总时限 1 秒，服务 0.6 秒才回，timeout 0.3。"""
    hub.script("status", Reply(body=status_body(send_timeout=1)))
    hub.script("messages", default_after(0.6))
    client = client_for(hub, timeout=0.3)
    client.status()

    assert client.send("慢但是成功").status == "sent"


def test_a_send_that_was_never_told_the_deadline_uses_the_default_one(hub, monkeypatch):
    monkeypatch.setattr(hub_client, "DEFAULT_SEND_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(hub_client, "SEND_TIMEOUT_MARGIN", 0.0)
    hub.script("messages", default_after(0.6))
    client = client_for(hub, timeout=0.3)

    assert client.send("慢但是成功").status == "sent"


def test_a_reply_waits_like_a_send(hub, quick):
    hub.script("status", Reply(body=status_body(send_timeout=1)))
    hub.script("replies", default_after(0.6))
    client = client_for(hub, timeout=0.3)
    client.status()

    assert client.reply("h_1", "好").status == "sent"


def test_a_per_call_timeout_can_make_a_send_wait_longer(hub, quick):
    hub.script("messages", default_after(0.6))
    client = client_for(hub, timeout=0.3)

    assert client.send("慢", timeout=1.5).status == "sent"
