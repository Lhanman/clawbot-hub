"""上游请求的结果分类（任务 A4）：对照 `hub-send`「逐条发送，判断送达」的分类表，
再加上收消息（`hub-inbox`）、扫码（`hub-login`）两处的分类。

全部用本机回环上的真 HTTP 假腾讯，超时、重置、半截响应都是真走一遍网络栈。重试策略不在这里
（在 B1 的 sender），这里只看单次请求归到哪一类、报错里有没有露出凭证。
"""

from __future__ import annotations

import socket
import time

import pytest
import requests

from clawbot_hub.config import UpstreamConfig
from clawbot_hub.upstream.base import (
    Accepted,
    IncomingMessage,
    NoResponse,
    PollFailed,
    Rejected,
    SessionExpired,
    Transient,
    Updates,
    UpstreamError,
)
from clawbot_hub.upstream.http import HttpUpstream
from tests.support.fake_ilink import (
    FakeIlink,
    Reply,
    close,
    empty,
    error,
    hang,
    html,
    http_status,
    ok,
    reset,
    session_expired,
    truncated,
)
from tests.support.fake_proxy import RefusingProxy
from tests.upstream.conftest import (
    CONTEXT,
    TOKEN,
    account_at,
    closed_port,
    poll,
    send,
    upstream_at,
)

# ---------------------------------------------------------------- 小工具


def sent_once(fake_ilink: FakeIlink) -> None:
    assert len(fake_ilink.requests_to("sendmessage")) == 1


# ---------------------------------------------------------------- 腾讯接受了


def test_a_plain_success_with_a_message_id_is_accepted(upstream, account, fake_ilink):
    fake_ilink.script("sendmessage", ok(message_id=123456))
    assert send(upstream, account) == Accepted(message_id="123456")
    sent_once(fake_ilink)


def test_the_fake_hands_out_message_ids_by_default(upstream, account):
    outcome = send(upstream, account)
    assert isinstance(outcome, Accepted) and outcome.message_id


def test_a_success_without_a_message_id_is_still_accepted(upstream, account, fake_ilink):
    fake_ilink.script("sendmessage", ok())
    assert send(upstream, account) == Accepted(message_id=None)


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"message_id": 7}, "7"),
        ({"msg_id": 8}, "8"),
        ({"message_id": "m-9"}, "m-9"),
        ({"message_id": "  m-9  "}, "m-9"),
        ({"message_id": 0, "msg_id": 10}, "10"),  # 0 不算有
        ({"message_id": "", "msg_id": 11}, "11"),  # 空串不算有
        ({"message_id": None, "msg_id": "m-12"}, "m-12"),
        ({"message_id": "   ", "msg_id": 13}, "13"),  # 只有空白不算有
        ({"message_id": 14, "msg_id": 15}, "14"),  # 都有时按配置里的先后
        ({"message_id": 1.5}, "1.5"),
        ({"message_id": 0.0}, None),
        ({"message_id": 0}, None),
        ({"message_id": ""}, None),
        ({"message_id": True, "msg_id": False}, None),  # 布尔不是编号
        ({"message_id": [1], "msg_id": {"a": 1}}, None),  # 不是标量不是编号
        ({"other_id": 16}, None),  # 不在配置的键里
    ],
)
def test_which_values_count_as_a_message_id(upstream, account, fake_ilink, fields, expected):
    fake_ilink.script("sendmessage", ok(**fields))
    assert send(upstream, account) == Accepted(message_id=expected)


def test_numbers_that_are_not_finite_are_not_message_ids(upstream, account, fake_ilink):
    fake_ilink.script(
        "sendmessage", Reply(body={"ret": 0, "message_id": float("nan"), "msg_id": float("inf")})
    )
    assert send(upstream, account) == Accepted(message_id=None)


def test_the_keys_that_carry_the_message_id_come_from_the_configuration(fake_ilink, account):
    custom = upstream_at(fake_ilink.base_url, message_id_keys=("id", "msg_id"))
    fake_ilink.script("sendmessage", ok(message_id=1, id="first", msg_id=2))
    fake_ilink.script("sendmessage", ok(message_id=1))
    assert send(custom, account) == Accepted(message_id="first")
    assert send(custom, account) == Accepted(message_id=None)  # message_id 不在配置的键里


def test_a_body_that_is_not_json_counts_as_no_business_code_and_no_message_id(
    upstream, account, fake_ilink
):
    fake_ilink.script("sendmessage", html(), empty(), Reply(body=[1, 2, 3]), Reply(body="null"))
    for _ in range(4):
        assert send(upstream, account) == Accepted(message_id=None)


def test_ret_zero_and_errcode_zero_or_missing_are_success(upstream, account, fake_ilink):
    fake_ilink.script(
        "sendmessage",
        Reply(body={"ret": 0, "errcode": 0}),
        Reply(body={"errcode": 0}),
        Reply(body={"ret": None, "errcode": None}),
        Reply(body={}),
    )
    for _ in range(4):
        assert isinstance(send(upstream, account), Accepted)


# ---------------------------------------------------------------- 业务码：ret 先于 errcode


def test_a_business_error_is_a_rejection_with_the_code_and_the_message(
    upstream, account, fake_ilink
):
    fake_ilink.script("sendmessage", error(40001, "bad token"))
    outcome = send(upstream, account)
    assert outcome == Rejected(http_status=200, code=40001, errmsg="bad token")
    assert outcome.detail == "40001 bad token"
    sent_once(fake_ilink)


def test_errcode_is_used_when_there_is_no_ret(upstream, account, fake_ilink):
    fake_ilink.script("sendmessage", error(7, "nope", key="errcode"))
    assert send(upstream, account) == Rejected(http_status=200, code=7, errmsg="nope")


def test_ret_wins_over_errcode_even_when_errcode_is_the_expiry_code(upstream, account, fake_ilink):
    fake_ilink.script("sendmessage", Reply(body={"ret": 5, "errcode": -14, "errmsg": "x"}))
    assert send(upstream, account) == Rejected(http_status=200, code=5, errmsg="x")


def test_a_zero_ret_falls_through_to_errcode(upstream, account, fake_ilink):
    fake_ilink.script("sendmessage", Reply(body={"ret": 0, "errcode": 9}))
    assert send(upstream, account) == Rejected(http_status=200, code=9, errmsg="")


def test_a_rejection_without_a_message_has_just_the_code(upstream, account, fake_ilink):
    fake_ilink.script("sendmessage", Reply(body={"ret": 40001}))
    assert send(upstream, account).detail == "40001"


def test_a_message_that_is_not_text_is_turned_into_text(upstream, account, fake_ilink):
    fake_ilink.script("sendmessage", Reply(body={"ret": 40001, "errmsg": 123}))
    assert send(upstream, account) == Rejected(http_status=200, code=40001, errmsg="123")


@pytest.mark.parametrize("key", ["ret", "errcode"])
def test_minus_14_in_either_field_means_the_login_expired(upstream, account, fake_ilink, key):
    fake_ilink.script("sendmessage", session_expired(key=key))
    assert send(upstream, account) == SessionExpired()
    sent_once(fake_ilink)


def test_minus_14_in_errcode_wins_when_ret_is_zero(upstream, account, fake_ilink):
    fake_ilink.script("sendmessage", Reply(body={"ret": 0, "errcode": -14}))
    assert send(upstream, account) == SessionExpired()


# ---------------------------------------------------------------- HTTP 状态码


@pytest.mark.parametrize("status", [400, 401, 403, 404, 408, 410, 413, 418, 451])
def test_a_4xx_other_than_429_is_a_rejection_without_a_business_code(
    upstream, account, fake_ilink, status
):
    fake_ilink.script("sendmessage", http_status(status))
    outcome = send(upstream, account)
    assert outcome == Rejected(http_status=status, code=None, errmsg="")
    assert outcome.detail == f"HTTP {status}"
    sent_once(fake_ilink)


def test_a_4xx_with_a_business_error_reports_the_business_code(upstream, account, fake_ilink):
    fake_ilink.script("sendmessage", error(5, "no way", status=400))
    outcome = send(upstream, account)
    assert outcome == Rejected(http_status=400, code=5, errmsg="no way")
    assert outcome.detail == "5 no way"


def test_a_4xx_with_a_plain_message_keeps_it_after_the_status(upstream, account, fake_ilink):
    fake_ilink.script("sendmessage", Reply(status=403, body={"errmsg": "forbidden"}))
    assert send(upstream, account).detail == "HTTP 403 forbidden"


def test_a_non_json_4xx_page_is_just_the_status(upstream, account, fake_ilink):
    fake_ilink.script("sendmessage", html(status=403))
    assert send(upstream, account) == Rejected(http_status=403, code=None, errmsg="")


@pytest.mark.parametrize("status", [201, 202, 204, 206])
def test_a_success_status_other_than_200_is_not_accepted(upstream, account, fake_ilink, status):
    fake_ilink.script("sendmessage", Reply(status=status, body={"ret": 0}))
    outcome = send(upstream, account)
    assert outcome == Rejected(http_status=status, code=None, errmsg="")
    assert outcome.detail == f"HTTP {status}"
    sent_once(fake_ilink)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_a_rejection_and_is_not_followed(upstream, account, fake_ilink, status):
    with FakeIlink() as elsewhere:
        target = elsewhere.base_url + "/ilink/bot/sendmessage"
        fake_ilink.script("sendmessage", Reply(status=status, headers={"Location": target}))
        outcome = send(upstream, account)
        assert outcome == Rejected(http_status=status, code=None, errmsg="")
        assert elsewhere.requests == []  # 没有跟过去
    sent_once(fake_ilink)


@pytest.mark.parametrize("status", [401, 403, 400, 201, 302])
def test_minus_14_is_read_from_the_body_of_any_status_except_429_and_5xx(
    upstream, account, fake_ilink, status
):
    fake_ilink.script("sendmessage", session_expired(status=status))
    assert send(upstream, account) == SessionExpired()
    sent_once(fake_ilink)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 599])
def test_a_complete_429_or_5xx_response_is_transient(upstream, account, fake_ilink, status):
    fake_ilink.script("sendmessage", http_status(status))
    outcome = send(upstream, account)
    assert isinstance(outcome, Transient)
    assert str(status) in outcome.reason
    sent_once(fake_ilink)


@pytest.mark.parametrize("status", [429, 502, 503])
def test_the_body_of_a_429_or_5xx_is_not_looked_at(upstream, account, fake_ilink, status):
    fake_ilink.script("sendmessage", session_expired(status=status))
    fake_ilink.script("sendmessage", error(40001, "bad token", status=status))
    assert isinstance(send(upstream, account), Transient)  # 带 -14 也只是可以重试
    assert isinstance(send(upstream, account), Transient)  # 带业务错误也一样


def test_a_5xx_reason_does_not_repeat_the_body(upstream, account, fake_ilink):
    fake_ilink.script("sendmessage", Reply(status=502, body="<html>secret page text</html>"))
    assert "secret page text" not in send(upstream, account).reason


# ---------------------------------------------------------------- 连接层：没发出去


def test_a_refused_connection_is_transient_and_nothing_was_sent(fake_ilink):
    dead = f"http://127.0.0.1:{closed_port()}"
    outcome = send(upstream_at(dead), account_at(dead))
    assert isinstance(outcome, Transient)
    assert fake_ilink.requests == []


def test_the_timeouts_that_reach_requests(monkeypatch):
    seen = {}

    def record(self, method, url, **kwargs):
        seen.update(kwargs)
        raise requests.exceptions.ConnectionError("stop here")

    monkeypatch.setattr(requests.Session, "request", record)
    client = HttpUpstream(UpstreamConfig(base_url="http://x.invalid", request_timeout=7))
    local = account_at("http://x.invalid")

    poll(client, local, timeout=45.0)  # 长轮询：连接最多等 request_timeout，读等轮询时长
    assert seen["timeout"] == (7, 45.0)
    poll(client, local, timeout=3.0)  # 轮询时长更短就都按它
    assert seen["timeout"] == (3.0, 3.0)
    send(client, local, timeout=12.5)  # 发送：连接和读都是调用者给的剩余预算
    assert seen["timeout"] == (12.5, 12.5)


def test_a_failure_before_sending_is_not_taken_for_an_unknown_result_after_an_earlier_send(
    upstream, account, fake_ilink
):
    assert isinstance(send(upstream, account), Accepted)  # 这个线程刚刚发出过一个请求
    dead = f"http://127.0.0.1:{closed_port()}"
    assert isinstance(send(upstream_at(dead), account_at(dead)), Transient)


def test_a_name_that_cannot_be_resolved_is_transient(monkeypatch):
    def no_such_host(*args, **kwargs):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr("urllib3.util.connection.create_connection", no_such_host)
    base = "http://upstream.invalid"
    assert isinstance(send(upstream_at(base), account_at(base)), Transient)


def test_a_connect_timeout_is_transient(monkeypatch):
    def too_slow(*args, **kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr("urllib3.util.connection.create_connection", too_slow)
    base = "http://upstream.invalid"
    assert isinstance(send(upstream_at(base), account_at(base)), Transient)


def test_a_failed_tls_handshake_is_transient(fake_ilink):
    # 拿一个只会说明文 HTTP 的服务当 https 去连：握手失败，请求还没发出去
    base = f"https://127.0.0.1:{fake_ilink.port}"
    assert isinstance(send(upstream_at(base), account_at(base)), Transient)
    assert fake_ilink.requests == []


def test_a_proxy_that_refuses_connections_is_transient(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{closed_port()}")
    base = "http://upstream.example"
    assert isinstance(send(upstream_at(base), account_at(base)), Transient)


def test_a_proxy_tunnel_that_cannot_be_set_up_is_transient_not_unknown(monkeypatch):
    with RefusingProxy() as proxy:
        monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{proxy.port}")
        base = "https://upstream.example"
        outcome = send(upstream_at(base), account_at(base))
        assert isinstance(outcome, Transient), outcome
        assert proxy.connects == 1


# ---------------------------------------------------------------- 连接层：发出去了，不知道结果


def test_a_read_timeout_is_no_response_and_says_it_was_a_read_timeout(
    upstream, account, fake_ilink
):
    fake_ilink.script("sendmessage", hang())
    started = time.monotonic()
    outcome = send(upstream, account, timeout=0.3)
    assert isinstance(outcome, NoResponse) and outcome.read_timeout is True
    assert 0.25 <= time.monotonic() - started < 5
    sent_once(fake_ilink)


@pytest.mark.parametrize("make", [reset, close, truncated], ids=["reset", "closed", "half a body"])
def test_a_connection_that_dies_after_the_request_is_no_response(
    upstream, account, fake_ilink, make
):
    fake_ilink.script("sendmessage", make())
    outcome = send(upstream, account)
    assert isinstance(outcome, NoResponse) and outcome.read_timeout is False
    sent_once(fake_ilink)


def test_a_reply_that_is_slow_but_within_the_timeout_is_accepted(upstream, account, fake_ilink):
    fake_ilink.script("sendmessage", ok(message_id=5).after(0.2))
    assert send(upstream, account, timeout=3.0) == Accepted(message_id="5")


def test_the_timeout_that_is_given_is_the_one_that_applies(upstream, account, fake_ilink):
    fake_ilink.script("sendmessage", hang(), hang())
    started = time.monotonic()
    send(upstream, account, timeout=0.2)
    short = time.monotonic() - started
    started = time.monotonic()
    send(upstream, account, timeout=0.9)
    long = time.monotonic() - started
    assert 0.15 <= short < 0.7
    assert 0.85 <= long < 5


# ---------------------------------------------------------------- 每次用新连接


def test_a_server_that_quietly_closes_idle_connections_never_looks_like_no_response(account):
    with FakeIlink(close_after_response=True) as flaky:
        local = account_at(flaky.base_url)
        upstream = upstream_at(flaky.base_url)
        outcomes = [send(upstream, local, client_id=f"cid-{i}") for i in range(4)]
        assert all(isinstance(o, Accepted) for o in outcomes), outcomes
        requests = flaky.requests_to("sendmessage")
        assert len(requests) == 4
        assert len({r.connection_id for r in requests}) == 4  # 四条各用各的连接


def test_polling_and_sending_do_not_share_a_connection(upstream, account, fake_ilink):
    poll(upstream, account)
    send(upstream, account)
    connections = {r.connection_id for r in fake_ilink.requests}
    assert len(connections) == 2


# ---------------------------------------------------------------- 报错里不露出凭证


def test_the_login_token_in_a_business_error_is_masked(upstream, account, fake_ilink):
    fake_ilink.script("sendmessage", error(40001, f"bad token {TOKEN} and ctx {CONTEXT}"))
    outcome = send(upstream, account)
    assert outcome.errmsg == "bad token *** and ctx ***"
    assert TOKEN not in repr(outcome) and CONTEXT not in repr(outcome)
    assert TOKEN not in outcome.detail


def test_a_project_key_in_an_error_is_masked_too(upstream, account, fake_ilink):
    key = "chub_" + "A" * 43
    fake_ilink.script("sendmessage", error(1, f"key {key}"))
    assert send(upstream, account).errmsg == "key ***"


def test_secrets_in_the_reasons_of_connection_failures_are_masked():
    # 把凭证放进地址里，连接错误的描述就会带着它（requests 的报错里有完整路径）
    dead = f"http://127.0.0.1:{closed_port()}/{TOKEN}"
    outcome = send(upstream_at(dead), account_at(dead), context_token=CONTEXT)
    assert isinstance(outcome, Transient)
    assert TOKEN not in outcome.reason and "***" in outcome.reason


def break_requests_with(monkeypatch, exc_type, text):
    def failing(self, *args, **kwargs):
        raise exc_type(text)

    monkeypatch.setattr(requests.Session, "request", failing)


def test_secrets_in_the_reasons_of_every_kind_of_failure_are_masked(monkeypatch):
    text = f"boom {TOKEN} and {CONTEXT} and buf-secret-77"
    local = account_at("http://upstream.invalid")
    client = upstream_at("http://upstream.invalid")

    break_requests_with(monkeypatch, requests.exceptions.ConnectionError, text)
    transient = send(client, local)
    failed_poll = poll(client, local, "buf-secret-77")
    break_requests_with(monkeypatch, requests.exceptions.ReadTimeout, text)
    unknown = send(client, local)

    assert isinstance(transient, Transient) and isinstance(unknown, NoResponse)
    assert isinstance(failed_poll, PollFailed)
    # 发送要遮登录凭证和对话凭证，收消息要遮登录凭证和收消息进度
    for reason in (transient.reason, unknown.reason):
        assert "boom *** and *** and buf-secret-77" in reason
        assert TOKEN not in reason and CONTEXT not in reason
    assert "boom *** and " in failed_poll.reason and failed_poll.reason.endswith(" and ***")
    assert TOKEN not in failed_poll.reason and "buf-secret-77" not in failed_poll.reason


def test_the_qr_code_and_the_verify_code_are_masked_in_qr_errors():
    dead = f"http://127.0.0.1:{closed_port()}"
    client = upstream_at(dead)
    with pytest.raises(UpstreamError) as caught:
        client.qr_status("qr-secret-code-1", "654321", None)
    assert "qr-secret-code-1" not in str(caught.value) and "654321" not in str(caught.value)
    assert "***" in str(caught.value)


# ---------------------------------------------------------------- ~/.netrc 不抢走 Authorization


def test_a_netrc_entry_does_not_replace_the_bearer_header(tmp_path, monkeypatch, fake_ilink):
    home = tmp_path / "netrc-home"
    home.mkdir()
    (home / ".netrc").write_text("default login someone password hunter2\n")
    (home / ".netrc").chmod(0o600)
    monkeypatch.setenv("HOME", str(home))
    local = account_at(fake_ilink.base_url)
    send(upstream_at(fake_ilink.base_url), local)
    poll(upstream_at(fake_ilink.base_url), local)
    for request in fake_ilink.requests:
        assert request.header("Authorization") == f"Bearer {TOKEN}"


# ---------------------------------------------------------------- 收消息


def text_item(text: str) -> dict:
    return {"type": 1, "text_item": {"text": text}}


def test_new_messages_are_parsed_and_the_next_position_is_returned(upstream, account, fake_ilink):
    fake_ilink.push_user_message("持仓", context_token="c-1", create_time_ms=1_700_000_000_111)
    fake_ilink.push_user_message("状态", context_token="c-2", create_time_ms=1_700_000_000_222)

    result = poll(upstream, account, "")

    assert isinstance(result, Updates) and result.timed_out is False
    assert result.buf == "buf-2"
    assert [(m.text, m.context_token, m.at_ms) for m in result.messages] == [
        ("持仓", "c-1", 1_700_000_000_111),
        ("状态", "c-2", 1_700_000_000_222),
    ]
    assert all(m.from_user_id == fake_ilink.user_id and m.has_text for m in result.messages)
    assert all(m.message_type == 1 for m in result.messages)
    assert [m.seq for m in result.messages] == [1, 2]
    assert all(m.message_id and m.message_id.isdigit() for m in result.messages)


def test_polling_from_the_returned_position_gives_nothing_new(upstream, account, fake_ilink):
    fake_ilink.push_user_message("一句")
    first = poll(upstream, account, "")
    second = poll(upstream, account, first.buf)
    assert second == Updates(messages=(), buf="buf-1", longpolling_timeout_ms=None, timed_out=False)


def test_a_poll_sends_the_position_it_was_given(upstream, account, fake_ilink):
    poll(upstream, account, "buf-41")
    assert fake_ilink.requests_to("getupdates")[0].json["get_updates_buf"] == "buf-41"


def one_message(**fields) -> dict:
    message = {
        "seq": 5,
        "message_id": 4242,
        "from_user_id": "someone@im.wechat",
        "message_type": 1,
        "context_token": "ctx-x",
        "create_time_ms": 1_700_000_000_000,
        "item_list": [text_item("你好")],
    }
    message.update(fields)
    return message


def poll_scripted(upstream, account, fake_ilink, message) -> IncomingMessage:
    fake_ilink.script("getupdates", ok(msgs=[message], get_updates_buf="b"))
    (parsed,) = poll(upstream, account).messages
    return parsed


def test_every_field_of_a_message_is_passed_through(upstream, account, fake_ilink):
    parsed = poll_scripted(upstream, account, fake_ilink, one_message())
    assert parsed == IncomingMessage(
        from_user_id="someone@im.wechat",
        message_type=1,
        context_token="ctx-x",
        at_ms=1_700_000_000_000,
        text="你好",
        has_text=True,
        message_id="4242",
        seq=5,
    )


def test_all_text_items_are_joined_in_order_and_other_items_are_skipped(
    upstream, account, fake_ilink
):
    items = [text_item("第一"), {"type": 2, "image_item": {"url": "x"}}, text_item("第二")]
    parsed = poll_scripted(upstream, account, fake_ilink, one_message(item_list=items))
    assert parsed.text == "第一第二" and parsed.has_text is True


def test_a_message_without_text_items_has_no_text(upstream, account, fake_ilink):
    items = [{"type": 2, "image_item": {}}, {"type": 3}]
    parsed = poll_scripted(upstream, account, fake_ilink, one_message(item_list=items))
    assert parsed.text == "" and parsed.has_text is False


@pytest.mark.parametrize("items", [None, [], "nope", {"type": 1}, [None, 5, "x"]])
def test_a_missing_or_odd_item_list_is_no_text(upstream, account, fake_ilink, items):
    parsed = poll_scripted(upstream, account, fake_ilink, one_message(item_list=items))
    assert parsed.text == "" and parsed.has_text is False


def test_an_empty_text_item_is_still_text(upstream, account, fake_ilink):
    # 使用者发了一句只有前缀的话：交给项目的文字是空的，但这是一条文字消息
    for item in (text_item(""), {"type": 1, "text_item": None}, {"type": 1}):
        parsed = poll_scripted(upstream, account, fake_ilink, one_message(item_list=[item]))
        assert parsed.text == "" and parsed.has_text is True


@pytest.mark.parametrize("value", [None, 0, -5, "1700000000000", True, [], "x"])
def test_a_time_that_is_not_a_positive_number_is_left_out(upstream, account, fake_ilink, value):
    assert (
        poll_scripted(upstream, account, fake_ilink, one_message(create_time_ms=value)).at_ms
        is None
    )


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_a_time_that_is_not_finite_is_left_out(upstream, account, fake_ilink, value):
    message = one_message(create_time_ms=value, message_id=value)
    parsed = poll_scripted(upstream, account, fake_ilink, message)
    assert parsed.at_ms is None and parsed.message_id is None


def test_a_time_is_kept_as_a_whole_number_of_milliseconds(upstream, account, fake_ilink):
    parsed = poll_scripted(upstream, account, fake_ilink, one_message(create_time_ms=1.7e12))
    assert parsed.at_ms == 1_700_000_000_000 and isinstance(parsed.at_ms, int)


@pytest.mark.parametrize("value", [None, "", 0, 5])
def test_a_context_token_that_is_empty_or_not_text_is_left_out(
    upstream, account, fake_ilink, value
):
    assert (
        poll_scripted(upstream, account, fake_ilink, one_message(context_token=value)).context_token
        is None
    )


def test_a_message_without_a_context_token_key_is_still_returned(upstream, account, fake_ilink):
    message = one_message()
    del message["context_token"]
    parsed = poll_scripted(upstream, account, fake_ilink, message)
    assert parsed.context_token is None  # 收不收由 poller 决定


@pytest.mark.parametrize(
    ("value", "expected"),
    [(7, "7"), ("m-1", "m-1"), (0, None), ("", None), (None, None), (True, None), ([1], None)],
)
def test_the_message_id_is_text_when_there_is_one(upstream, account, fake_ilink, value, expected):
    assert (
        poll_scripted(upstream, account, fake_ilink, one_message(message_id=value)).message_id
        == expected
    )


@pytest.mark.parametrize("value", [None, "5", 5.5, True, [5]])
def test_a_sequence_number_that_is_not_a_whole_number_is_left_out(
    upstream, account, fake_ilink, value
):
    assert poll_scripted(upstream, account, fake_ilink, one_message(seq=value)).seq is None


def test_the_sender_and_the_type_are_passed_through_whatever_they_are(
    upstream, account, fake_ilink
):
    parsed = poll_scripted(
        upstream, account, fake_ilink, one_message(from_user_id="bot@im.bot", message_type=2)
    )
    assert parsed.from_user_id == "bot@im.bot" and parsed.message_type == 2  # 丢不丢由 poller 决定
    missing = one_message(message_type="x")
    del missing["from_user_id"]
    parsed = poll_scripted(upstream, account, fake_ilink, missing)
    assert parsed.from_user_id == "" and parsed.message_type is None


def test_entries_that_are_not_objects_are_skipped(upstream, account, fake_ilink):
    fake_ilink.script(
        "getupdates", ok(msgs=[None, 5, "x", one_message(seq=1), [1]], get_updates_buf="b")
    )
    result = poll(upstream, account)
    assert [m.seq for m in result.messages] == [1]


@pytest.mark.parametrize("msgs", [None, "nope", {"a": 1}, 5])
def test_a_message_list_that_is_not_a_list_is_empty(upstream, account, fake_ilink, msgs):
    fake_ilink.script("getupdates", ok(msgs=msgs, get_updates_buf="b9"))
    assert poll(upstream, account) == Updates(messages=(), buf="b9")


@pytest.mark.parametrize(
    ("buf", "expected"), [("b-1", "b-1"), ("", None), (None, None), (5, None), (["x"], None)]
)
def test_the_new_position_is_text_when_there_is_one(upstream, account, fake_ilink, buf, expected):
    fake_ilink.script("getupdates", ok(msgs=[], get_updates_buf=buf))
    assert poll(upstream, account).buf == expected


@pytest.mark.parametrize(
    ("hint", "expected"),
    [(60000, 60000), (1, 1), (0, None), (-5, None), ("60000", None), (True, None), (None, None)],
)
def test_the_suggested_long_poll_time_is_kept_only_when_it_is_a_positive_number(
    upstream, account, fake_ilink, hint, expected
):
    fake_ilink.script("getupdates", ok(msgs=[], get_updates_buf="b", longpolling_timeout_ms=hint))
    assert poll(upstream, account).longpolling_timeout_ms == expected


def test_the_fake_can_suggest_a_long_poll_time(account):
    with FakeIlink(longpoll_hint_ms=60000) as fake:
        result = poll(upstream_at(fake.base_url), account_at(fake.base_url))
        assert result.longpolling_timeout_ms == 60000


def test_a_long_poll_that_the_client_gives_up_on_is_no_news(upstream, account, fake_ilink):
    fake_ilink.script("getupdates", hang())
    started = time.monotonic()
    result = poll(upstream, account, "buf-3", timeout=0.3)
    assert result == Updates(messages=(), buf=None, longpolling_timeout_ms=None, timed_out=True)
    assert 0.25 <= time.monotonic() - started < 5


@pytest.mark.parametrize("make", [html, empty], ids=["page", "empty"])
def test_a_200_that_is_not_json_is_no_news_not_an_error(upstream, account, fake_ilink, make):
    fake_ilink.script("getupdates", make())
    assert poll(upstream, account) == Updates(messages=(), buf=None)


def test_a_200_that_is_json_but_not_an_object_is_no_news(upstream, account, fake_ilink):
    fake_ilink.script("getupdates", Reply(body=[1, 2]), Reply(body="null"))
    assert poll(upstream, account) == Updates(messages=(), buf=None)
    assert poll(upstream, account) == Updates(messages=(), buf=None)


# ---------------------------------------------------------------- 收消息：出错


def test_minus_14_on_a_poll_is_session_expired(upstream, account, fake_ilink):
    fake_ilink.script("getupdates", session_expired(), session_expired(key="errcode"))
    assert poll(upstream, account) == SessionExpired()
    assert poll(upstream, account) == SessionExpired()


@pytest.mark.parametrize("status", [400, 401, 403])
def test_minus_14_in_a_4xx_on_a_poll_is_session_expired(upstream, account, fake_ilink, status):
    fake_ilink.script("getupdates", session_expired(status=status))
    assert poll(upstream, account) == SessionExpired()


def test_ret_wins_over_errcode_on_a_poll_too(upstream, account, fake_ilink):
    fake_ilink.script("getupdates", Reply(body={"ret": 5, "errcode": -14, "errmsg": "x"}))
    assert poll(upstream, account) == PollFailed(reason="5 x", completed=True)


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_429_and_5xx_on_a_poll_are_errors_that_are_not_a_round_trip(
    upstream, account, fake_ilink, status
):
    fake_ilink.script("getupdates", http_status(status), session_expired(status=status))
    for _ in range(2):  # 带 -14 的也只是出错，不看响应体
        result = poll(upstream, account)
        assert isinstance(result, PollFailed) and result.completed is False
        assert str(status) in result.reason


@pytest.mark.parametrize("status", [400, 403, 404, 410])
def test_other_4xx_on_a_poll_are_errors_that_did_complete_a_round_trip(
    upstream, account, fake_ilink, status
):
    fake_ilink.script("getupdates", http_status(status))
    assert poll(upstream, account) == PollFailed(reason=f"HTTP {status}", completed=True)


@pytest.mark.parametrize("status", [201, 204, 302])
def test_a_status_other_than_200_on_a_poll_is_an_error_even_with_a_good_body(
    upstream, account, fake_ilink, status
):
    fake_ilink.script("getupdates", Reply(status=status, body={"ret": 0, "msgs": [one_message()]}))
    assert poll(upstream, account) == PollFailed(reason=f"HTTP {status}", completed=True)


def test_a_business_error_on_a_poll_is_an_error_that_did_complete(upstream, account, fake_ilink):
    fake_ilink.script("getupdates", error(500, "oops"))
    assert poll(upstream, account) == PollFailed(reason="500 oops", completed=True)


def test_an_error_body_wins_over_messages_in_the_same_body(upstream, account, fake_ilink):
    fake_ilink.script("getupdates", Reply(body={"ret": 7, "msgs": [one_message()]}))
    assert poll(upstream, account) == PollFailed(reason="7", completed=True)


def test_connection_failures_on_a_poll_are_errors_that_are_not_a_round_trip(fake_ilink):
    dead = f"http://127.0.0.1:{closed_port()}"
    result = poll(upstream_at(dead), account_at(dead))
    assert isinstance(result, PollFailed) and result.completed is False


def test_a_tls_failure_on_a_poll_is_an_error_that_is_not_a_round_trip(fake_ilink):
    base = f"https://127.0.0.1:{fake_ilink.port}"
    result = poll(upstream_at(base), account_at(base))
    assert isinstance(result, PollFailed) and result.completed is False


@pytest.mark.parametrize("make", [reset, close, truncated], ids=["reset", "closed", "half a body"])
def test_a_connection_that_dies_during_a_poll_is_an_error_not_no_news(
    upstream, account, fake_ilink, make
):
    fake_ilink.script("getupdates", make())
    result = poll(upstream, account)
    assert isinstance(result, PollFailed) and result.completed is False


def test_secrets_are_masked_in_poll_errors(upstream, account, fake_ilink):
    fake_ilink.script("getupdates", error(500, f"echo {TOKEN} and buf-secret-77"))
    result = poll(upstream, account, "buf-secret-77")
    assert result.reason == "500 echo *** and ***"
    dead = f"http://127.0.0.1:{closed_port()}/{TOKEN}"
    result = poll(upstream_at(dead), account_at(dead))
    assert TOKEN not in result.reason and "***" in result.reason


# ---------------------------------------------------------------- 扫码：出错一律是 UpstreamError


def test_the_qr_calls_fail_with_an_upstream_error_when_nothing_can_be_reached():
    dead = f"http://127.0.0.1:{closed_port()}"
    client = upstream_at(dead)
    with pytest.raises(UpstreamError):
        client.qr_start()
    with pytest.raises(UpstreamError):
        client.qr_status("any-code", None, None)


@pytest.mark.parametrize("status", [400, 404, 500, 502])
def test_a_status_other_than_200_on_a_qr_call_is_an_upstream_error(upstream, fake_ilink, status):
    fake_ilink.script("get_bot_qrcode", http_status(status))
    fake_ilink.script("get_qrcode_status", http_status(status))
    with pytest.raises(UpstreamError, match=f"HTTP {status}"):
        upstream.qr_start()
    with pytest.raises(UpstreamError, match=f"HTTP {status}"):
        upstream.qr_status("code", None, None)


def test_a_business_error_on_a_qr_call_is_reported_with_its_code(upstream, fake_ilink):
    fake_ilink.script("get_bot_qrcode", error(5, "no way"))
    fake_ilink.script("get_qrcode_status", error(6, "still no"))
    with pytest.raises(UpstreamError, match="5 no way"):
        upstream.qr_start()
    with pytest.raises(UpstreamError, match="6 still no"):
        upstream.qr_status("code", None, None)


@pytest.mark.parametrize("reply", [html(), empty(), Reply(body=[1]), Reply(body={})])
def test_a_qr_answer_that_is_not_what_it_should_be_is_an_upstream_error(
    upstream, fake_ilink, reply
):
    fake_ilink.script("get_bot_qrcode", reply)
    fake_ilink.script("get_qrcode_status", reply)
    with pytest.raises(UpstreamError):
        upstream.qr_start()
    with pytest.raises(UpstreamError):
        upstream.qr_status("code", None, None)


@pytest.mark.parametrize(
    "body",
    [{"qrcode": "c"}, {"qrcode_img_content": "u"}, {"qrcode": "", "qrcode_img_content": "u"}],
)
def test_a_qr_start_answer_needs_both_the_code_and_the_image_content(upstream, fake_ilink, body):
    fake_ilink.script("get_bot_qrcode", Reply(body=body))
    with pytest.raises(UpstreamError):
        upstream.qr_start()


def test_a_confirmed_status_without_a_login_token_is_an_upstream_error(upstream, fake_ilink):
    fake_ilink.script("get_qrcode_status", Reply(body={"status": "confirmed", "ilink_bot_id": "b"}))
    with pytest.raises(UpstreamError):
        upstream.qr_status("code", None, None)
    fake_ilink.script("get_qrcode_status", Reply(body={"status": "confirmed", "bot_token": ""}))
    with pytest.raises(UpstreamError):
        upstream.qr_status("code", None, None)


@pytest.mark.parametrize("body", [{"status": ""}, {"status": None}, {"status": 5}, {"x": 1}])
def test_a_status_answer_without_a_status_is_an_upstream_error(upstream, fake_ilink, body):
    fake_ilink.script("get_qrcode_status", Reply(body=body))
    with pytest.raises(UpstreamError):
        upstream.qr_status("code", None, None)
