"""上游请求的形式（任务 A4）：`legacy` 与 `official` 两档发出的请求，
对照 `hub-login`「上游请求的形式」。

用本机回环上的真 HTTP 假腾讯（`tests/support/fake_ilink.py`），请求头、请求体都是真走一遍网络栈后
假上游记下来的。随机值（`X-WECHAT-UIN`、`client_id`）只比格式。
"""

from __future__ import annotations

import base64
import logging
import re

import pytest

from clawbot_hub import __version__
from clawbot_hub.config import UpstreamConfig
from clawbot_hub.upstream.base import Accepted, Account, QrCode, QrStatus, UpstreamError
from clawbot_hub.upstream.http import HttpUpstream, _check_host
from tests.support.fake_ilink import FakeIlink, error, http_status, session_expired
from tests.upstream.conftest import CONTEXT, TOKEN, poll, send

LEGACY_HEADERS = {"content-type", "authorizationtype", "authorization", "x-wechat-uin"}
OFFICIAL_HEADERS = LEGACY_HEADERS | {"ilink-app-id", "ilink-app-clientversion"}
# E4（2026-10-18）在真账号上量过：只有带 /msg 的路径可用，不带的回 404
NOTIFY_PATHS = {
    "notifystart": ("/ilink/bot/msg/notifystart",),
    "notifystop": ("/ilink/bot/msg/notifystop",),
}


def official(fake_ilink) -> HttpUpstream:
    return HttpUpstream(UpstreamConfig(base_url=fake_ilink.base_url, profile="official"))


def header_names(request) -> set[str]:
    return {name.lower() for name in request.custom_header_names()}


# ---------------------------------------------------------------- legacy：收消息


def test_legacy_getupdates_has_exactly_the_four_custom_headers(upstream, account, fake_ilink):
    poll(upstream, account)
    (request,) = fake_ilink.requests_to("getupdates")
    assert len(request.custom_header_names()) == 4
    assert header_names(request) == LEGACY_HEADERS
    assert request.method == "POST" and request.path == "/ilink/bot/getupdates"


def test_legacy_header_values(upstream, account, fake_ilink):
    poll(upstream, account)
    (request,) = fake_ilink.requests_to("getupdates")
    assert request.header("Content-Type") == "application/json"
    assert request.header("AuthorizationType") == "ilink_bot_token"
    assert request.header("Authorization") == f"Bearer {TOKEN}"
    uin = request.header("X-WECHAT-UIN")
    assert re.fullmatch(r"[A-Za-z0-9+/]+=*", uin)
    number = base64.b64decode(uin).decode()
    assert number.isdigit() and 0 <= int(number) < 2**32 and str(int(number)) == number


def test_the_uin_is_different_on_every_request(upstream, account, fake_ilink):
    for _ in range(6):
        poll(upstream, account)
    uins = [r.header("X-WECHAT-UIN") for r in fake_ilink.requests_to("getupdates")]
    assert len(set(uins)) == 6


def test_legacy_getupdates_body_has_only_the_buf_and_base_info(upstream, account, fake_ilink):
    poll(upstream, account, "")
    poll(upstream, account, "buf-3")
    first, second = (r.json for r in fake_ilink.requests_to("getupdates"))
    assert set(first) == {"get_updates_buf", "base_info"}
    assert first["get_updates_buf"] == "" and second["get_updates_buf"] == "buf-3"
    assert first["base_info"] == {"channel_version": "1.0.2"}


# ---------------------------------------------------------------- legacy：发消息


def test_legacy_sendmessage_has_exactly_the_four_custom_headers(upstream, account, fake_ilink):
    send(upstream, account)
    (request,) = fake_ilink.requests_to("sendmessage")
    assert len(request.custom_header_names()) == 4
    assert header_names(request) == LEGACY_HEADERS
    assert request.header("Authorization") == f"Bearer {TOKEN}"
    assert request.method == "POST" and request.path == "/ilink/bot/sendmessage"


def test_legacy_sendmessage_body_has_seven_msg_fields_and_a_bare_base_info(
    upstream, account, fake_ilink
):
    send(upstream, account, "第一行\n第二行 ✓", client_id="cid-77")
    (request,) = fake_ilink.requests_to("sendmessage")
    assert set(request.json) == {"msg", "base_info"}
    assert request.json["base_info"] == {"channel_version": "1.0.2"}
    assert request.json["msg"] == {
        "from_user_id": "",
        "to_user_id": account.user_id,
        "client_id": "cid-77",
        "message_type": 2,
        "message_state": 2,
        "context_token": CONTEXT,
        "item_list": [{"type": 1, "text_item": {"text": "第一行\n第二行 ✓"}}],
    }
    assert len(request.json["msg"]) == 7


def test_the_given_client_id_is_used_as_it_is_so_retries_can_reuse_it(
    upstream, account, fake_ilink
):
    send(upstream, account, client_id="same-id")
    send(upstream, account, client_id="same-id")
    assert [m.client_id for m in fake_ilink.sent] == ["same-id", "same-id"]


# ---------------------------------------------------------------- legacy：二维码


def test_legacy_qr_start_is_a_plain_get_without_custom_headers(upstream, fake_ilink):
    qr = upstream.qr_start()
    assert qr == QrCode(code="fake-qr-1", image_content=qr.image_content)
    assert qr.image_content.startswith("https://") and "fake-qr-1" in qr.image_content
    (request,) = fake_ilink.requests_to("get_bot_qrcode")
    assert request.method == "GET" and request.path == "/ilink/bot/get_bot_qrcode"
    assert request.query == {"bot_type": "3"}
    assert request.body == b""
    assert request.custom_header_names() == []


def test_legacy_qr_status_is_a_plain_get_with_the_code_and_optionally_the_verify_code(
    upstream, fake_ilink
):
    qr = upstream.qr_start()
    upstream.qr_status(qr.code, None, None)
    upstream.qr_status(qr.code, "123456", None)
    first, second = fake_ilink.requests_to("get_qrcode_status")
    assert first.method == "GET" and first.path == "/ilink/bot/get_qrcode_status"
    assert first.query == {"qrcode": qr.code}
    assert second.query == {"qrcode": qr.code, "verify_code": "123456"}
    assert first.custom_header_names() == [] and second.custom_header_names() == []


def test_a_confirmed_status_carries_the_new_account(upstream, fake_ilink):
    fake_ilink.qr_plan(["wait", "scaned", "confirmed"])
    qr = upstream.qr_start()
    states = [upstream.qr_status(qr.code, None, None) for _ in range(3)]
    assert [s.status for s in states] == ["wait", "scaned", "confirmed"]
    assert states[0].account is None and states[1].account is None
    account = states[2].account
    assert account == Account(
        bot_token="fake-token-1",
        base_url=fake_ilink.base_url,
        bot_id=fake_ilink.bot_id,
        user_id=fake_ilink.user_id,
    )
    assert "fake-token-1" not in repr(account)  # 打印出来也不露出登录凭证


def test_a_confirmed_status_without_a_base_url_leaves_it_empty(upstream, fake_ilink):
    fake_ilink.qr_plan([{"status": "confirmed", "baseurl": ""}])
    qr = upstream.qr_start()
    assert upstream.qr_status(qr.code, None, None).account.base_url == ""


@pytest.mark.parametrize(
    "status",
    ["expired", "need_verifycode", "verify_code_blocked", "binded_redirect", "something_new"],
)
def test_the_other_statuses_come_through_by_name(upstream, fake_ilink, status):
    fake_ilink.qr_plan([status])
    qr = upstream.qr_start()
    result = upstream.qr_status(qr.code, None, None)
    assert result == QrStatus(status=status)


def test_after_a_redirect_the_queries_go_to_the_redirect_host(upstream, fake_ilink):
    with FakeIlink() as other:
        fake_ilink.qr_plan(
            [{"status": "scaned_but_redirect", "redirect_host": f"127.0.0.1:{other.port}"}]
        )
        qr = upstream.qr_start()
        redirected = upstream.qr_status(qr.code, None, None)
        assert redirected.status == "scaned_but_redirect"
        assert redirected.redirect_host == f"127.0.0.1:{other.port}"

        upstream.qr_status(qr.code, None, redirected.redirect_host)
        assert len(other.requests_to("get_qrcode_status")) == 1
        assert other.requests_to("get_qrcode_status")[0].query == {"qrcode": qr.code}
        assert len(fake_ilink.requests_to("get_qrcode_status")) == 1  # 原来的地址没有再收到


@pytest.mark.parametrize(
    "host",
    [
        "evil.example/path?x=",
        "user@evil.example",
        "evil.example:80:90",
        "a b",
        "",
        "ev il",
        "x/../y",
        ".evil.example",
        "evil.example.",
        "-evil.example",
        "evil.example:65536",
        "evil.example:123456",
        "evil.example:",
        ":80",
        "[::1]:80",
        "http://evil.example",
    ],
)
def test_a_redirect_host_that_is_not_a_plain_host_is_refused(upstream, fake_ilink, host):
    fake_ilink.qr_plan([{"status": "scaned_but_redirect", "redirect_host": host}])
    qr = upstream.qr_start()
    with pytest.raises(UpstreamError):
        upstream.qr_status(qr.code, None, None)


@pytest.mark.parametrize(
    "host",
    [
        "open.weixin.qq.com",
        "a",
        "A1.b2",
        "127.0.0.1:8080",
        "host-with-dash.example.com:443",
        "x:65535",
    ],
)
def test_plain_hosts_with_or_without_a_port_are_accepted(host):
    assert _check_host(host) == host


def test_a_redirect_without_a_host_is_refused(upstream, fake_ilink):
    fake_ilink.qr_plan([{"status": "scaned_but_redirect"}])
    qr = upstream.qr_start()
    with pytest.raises(UpstreamError):
        upstream.qr_status(qr.code, None, None)


def test_a_malformed_host_is_refused_even_when_the_caller_passes_it_in(upstream, fake_ilink):
    with pytest.raises(UpstreamError):
        upstream.qr_status("fake-qr-1", None, "evil.example/path")
    assert fake_ilink.requests == []


# ---------------------------------------------------------------- 地址


def test_the_account_address_wins_for_polling_and_sending(fake_ilink, account):
    with FakeIlink() as configured:
        upstream = HttpUpstream(UpstreamConfig(base_url=configured.base_url))
        poll(upstream, account)
        send(upstream, account)
        assert len(fake_ilink.requests_to("getupdates")) == 1
        assert len(fake_ilink.requests_to("sendmessage")) == 1
        assert configured.requests == []


def test_an_account_without_an_address_falls_back_to_the_configured_one(fake_ilink, account):
    bare = Account(account.bot_token, "", account.bot_id, account.user_id)
    upstream = HttpUpstream(UpstreamConfig(base_url=fake_ilink.base_url))
    poll(upstream, bare)
    send(upstream, bare)
    assert len(fake_ilink.requests) == 2


def test_the_qr_requests_use_the_configured_address_not_the_account_one(fake_ilink):
    with FakeIlink() as configured:
        upstream = HttpUpstream(UpstreamConfig(base_url=configured.base_url))
        upstream.qr_start()
        assert len(configured.requests_to("get_bot_qrcode")) == 1
        assert fake_ilink.requests == []


def test_a_trailing_slash_in_the_address_does_not_double_up(fake_ilink, account):
    upstream = HttpUpstream(UpstreamConfig(base_url=fake_ilink.base_url + "/"))
    bare = Account(account.bot_token, fake_ilink.base_url + "//", account.bot_id, account.user_id)
    poll(upstream, bare)
    upstream.qr_start()
    assert {r.path for r in fake_ilink.requests} == {
        "/ilink/bot/getupdates",
        "/ilink/bot/get_bot_qrcode",
    }


# ---------------------------------------------------------------- official


def test_official_adds_two_headers_and_a_richer_base_info(fake_ilink, account):
    upstream = official(fake_ilink)
    poll(upstream, account)
    send(upstream, account)
    for request in (
        fake_ilink.requests_to("getupdates")[0],
        fake_ilink.requests_to("sendmessage")[0],
    ):
        assert header_names(request) == OFFICIAL_HEADERS
        assert len(request.custom_header_names()) == 6
        assert request.header("iLink-App-Id") == "bot"
        assert request.header("iLink-App-ClientVersion") == "132104"
        assert request.header("Authorization") == f"Bearer {TOKEN}"
        assert request.json["base_info"] == {
            "channel_version": "2.4.8",
            "bot_agent": f"clawbot-hub/{__version__}",
        }


def test_official_keeps_the_same_body_shapes(fake_ilink, account):
    upstream = official(fake_ilink)
    poll(upstream, account, "buf-1")
    send(upstream, account, client_id="cid-9")
    polled = fake_ilink.requests_to("getupdates")[0].json
    sent = fake_ilink.requests_to("sendmessage")[0].json
    assert set(polled) == {"get_updates_buf", "base_info"}
    assert set(sent) == {"msg", "base_info"} and len(sent["msg"]) == 7


def test_official_qr_start_is_a_post_with_an_empty_local_token_list(fake_ilink):
    upstream = official(fake_ilink)
    upstream.qr_start()
    (request,) = fake_ilink.requests_to("get_bot_qrcode")
    assert request.method == "POST"
    assert request.json == {"local_token_list": []}
    assert request.query == {"bot_type": "3"}
    assert request.header("iLink-App-Id") == "bot"
    assert request.header("iLink-App-ClientVersion") == "132104"
    assert request.header("Authorization") is None


def test_official_qr_status_stays_a_get_with_the_extra_headers(fake_ilink):
    upstream = official(fake_ilink)
    qr = upstream.qr_start()
    upstream.qr_status(qr.code, None, None)
    (request,) = fake_ilink.requests_to("get_qrcode_status")
    assert request.method == "GET" and request.query == {"qrcode": qr.code}
    assert request.header("iLink-App-Id") == "bot"


def test_official_calls_notifystart_and_notifystop(fake_ilink, account):
    upstream = official(fake_ilink)
    upstream.notify_start(account)
    upstream.notify_stop(account)
    (start,) = fake_ilink.requests_to("notifystart")
    (stop,) = fake_ilink.requests_to("notifystop")
    assert start.method == "POST" and start.path in NOTIFY_PATHS["notifystart"]
    assert stop.method == "POST" and stop.path in NOTIFY_PATHS["notifystop"]
    for request in (start, stop):
        assert request.header("Authorization") == f"Bearer {TOKEN}"
        assert request.header("iLink-App-Id") == "bot"
        assert request.json == {
            "base_info": {"channel_version": "2.4.8", "bot_agent": f"clawbot-hub/{__version__}"}
        }


def test_legacy_never_calls_notifystart_or_notifystop(upstream, account, fake_ilink):
    upstream.notify_start(account)
    upstream.notify_stop(account)
    assert fake_ilink.requests == []


@pytest.mark.parametrize(
    "reply",
    [error(500, "boom"), session_expired(), http_status(404), http_status(503)],
    ids=["business error", "expired", "404", "503"],
)
def test_a_failing_notifystart_or_notifystop_is_only_a_warning(fake_ilink, account, caplog, reply):
    fake_ilink.script("notifystart", reply)
    fake_ilink.script("notifystop", reply)
    upstream = official(fake_ilink)
    with caplog.at_level(logging.WARNING, logger="clawbot_hub"):
        upstream.notify_start(account)
        upstream.notify_stop(account)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2  # 开始、结束各一条
    assert all(TOKEN not in r.getMessage() for r in warnings)


def test_a_notify_that_works_logs_no_warning(fake_ilink, account, caplog):
    upstream = official(fake_ilink)
    with caplog.at_level(logging.WARNING, logger="clawbot_hub"):
        upstream.notify_start(account)
        upstream.notify_stop(account)
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_a_notify_that_cannot_connect_is_only_a_warning(account, caplog):
    dead = Account(TOKEN, "http://127.0.0.1:1", "b", "u")
    upstream = HttpUpstream(UpstreamConfig(base_url="http://127.0.0.1:1", profile="official"))
    with caplog.at_level(logging.WARNING, logger="clawbot_hub"):
        upstream.notify_start(dead)
        upstream.notify_stop(dead)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 2
    assert all(TOKEN not in r.getMessage() for r in warnings)


def test_an_unknown_profile_is_refused():
    with pytest.raises(ValueError, match="profile"):
        HttpUpstream(UpstreamConfig(profile="nope"))


def test_a_confirmed_send_through_the_official_profile_is_accepted(fake_ilink, account):
    assert isinstance(send(official(fake_ilink), account), Accepted)
