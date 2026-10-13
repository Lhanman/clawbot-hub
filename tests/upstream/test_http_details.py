"""请求的几处细节（任务 A4）：地址怎么拼、各个请求用多久的超时、业务码里的空串、会话收尾、
扫码被拒时原因里的码要遮掉。分类表本身在 `test_http_outcomes.py`。"""

from __future__ import annotations

import pytest
import requests

from clawbot_hub.config import UpstreamConfig
from clawbot_hub.upstream.base import (
    Accepted,
    Account,
    Rejected,
    SessionExpired,
    Updates,
    UpstreamError,
)
from clawbot_hub.upstream.http import HttpUpstream
from tests.support.fake_ilink import Reply, error, reset
from tests.upstream.conftest import TOKEN, account_at, closed_port, poll, send, upstream_at

# ---------------------------------------------------------------- 业务码里的空串


def test_an_empty_string_ret_is_no_business_code_so_errcode_is_looked_at(
    upstream, account, fake_ilink
):
    fake_ilink.script(
        "sendmessage",
        Reply(body={"ret": "", "errcode": -14}),
        Reply(body={"ret": "", "errcode": 7}),
        Reply(body={"ret": "", "errcode": ""}),
        Reply(body={"ret": "", "errcode": 0}),
        Reply(body={"ret": "", "errcode": None}),
    )

    assert send(upstream, account) == SessionExpired()
    rejected = send(upstream, account)
    assert isinstance(rejected, Rejected) and rejected.code == 7
    for _ in range(3):
        assert send(upstream, account) == Accepted(message_id=None)


def test_an_empty_string_ret_on_a_poll_is_no_business_code_either(upstream, account, fake_ilink):
    fake_ilink.script(
        "getupdates",
        Reply(body={"ret": "", "errcode": -14}),
        Reply(body={"ret": "", "msgs": []}),
    )

    assert poll(upstream, account) == SessionExpired()
    assert isinstance(poll(upstream, account), Updates)


# ---------------------------------------------------------------- 地址的拼法


def test_a_trailing_slash_on_the_address_of_the_account_makes_no_double_slash(fake_ilink):
    slashed = fake_ilink.base_url + "/"
    client = upstream_at(fake_ilink.base_url)
    local = Account(bot_token=TOKEN, base_url=slashed, bot_id="b", user_id="u")

    assert isinstance(send(client, local), Accepted)
    assert isinstance(poll(client, local), Updates)
    assert [r.target for r in fake_ilink.requests] == [
        "/ilink/bot/sendmessage",
        "/ilink/bot/getupdates",
    ]


def test_a_trailing_slash_on_the_configured_address_makes_no_double_slash(fake_ilink):
    slashed = fake_ilink.base_url + "/"
    client = upstream_at(slashed)
    local = Account(bot_token=TOKEN, base_url="", bot_id="b", user_id="u")  # 账号没写：用配置的

    assert isinstance(send(client, local), Accepted)
    assert isinstance(poll(client, local), Updates)
    qr = client.qr_start()
    assert client.qr_status(qr.code, None, None).status  # 路径不对的话这里会是 UpstreamError
    assert [r.target.split("?")[0] for r in fake_ilink.requests] == [
        "/ilink/bot/sendmessage",
        "/ilink/bot/getupdates",
        "/ilink/bot/get_bot_qrcode",
        "/ilink/bot/get_qrcode_status",
    ]


# ---------------------------------------------------------------- 每个请求的超时


def record_timeouts(monkeypatch) -> dict[str, tuple[float, float]]:
    """把每次交给 `requests` 的超时按地址最后一段记下来，请求本身不发。"""
    seen: dict[str, tuple[float, float]] = {}

    def record(self, method, url, **kwargs):
        seen[url.split("?")[0].rsplit("/", 1)[-1]] = kwargs["timeout"]
        raise requests.exceptions.ConnectionError("stop here")

    monkeypatch.setattr(requests.Session, "request", record)
    return seen


def test_the_qr_calls_and_the_notifications_use_their_own_timeouts(monkeypatch):
    seen = record_timeouts(monkeypatch)
    config = UpstreamConfig(base_url="http://x.invalid", profile="official", request_timeout=7)
    client = HttpUpstream(config, qr_start_timeout=11.0, qr_status_timeout=33.0)
    local = account_at("http://x.invalid")

    with pytest.raises(UpstreamError):
        client.qr_start()
    with pytest.raises(UpstreamError):
        client.qr_status("code", None, None)
    client.notify_start(local)
    client.notify_stop(local)

    assert seen == {
        "get_bot_qrcode": (11.0, 11.0),
        "get_qrcode_status": (33.0, 33.0),
        "notifystart": (7, 7),
        "notifystop": (7, 7),
    }


def test_the_qr_calls_have_default_timeouts_of_20_and_40_seconds(monkeypatch):
    seen = record_timeouts(monkeypatch)
    client = upstream_at("http://x.invalid")

    with pytest.raises(UpstreamError):
        client.qr_start()
    with pytest.raises(UpstreamError):
        client.qr_status("code", None, None)

    assert seen == {"get_bot_qrcode": (20.0, 20.0), "get_qrcode_status": (40.0, 40.0)}


# ---------------------------------------------------------------- 会话收尾


def test_every_request_closes_the_session_it_opened(monkeypatch, upstream, account, fake_ilink):
    closed: list[requests.Session] = []
    real_close = requests.Session.close

    def counting(self):
        closed.append(self)
        real_close(self)

    monkeypatch.setattr(requests.Session, "close", counting)
    dead = f"http://127.0.0.1:{closed_port()}"

    send(upstream, account)  # 成功
    send(upstream_at(dead), account_at(dead))  # 没发出去
    fake_ilink.script("sendmessage", reset())
    send(upstream, account)  # 发出去了，不知道结果

    assert len(closed) == 3
    assert len({id(session) for session in closed}) == 3  # 每次一个新会话


# ---------------------------------------------------------------- 扫码被拒时原因里的码


def test_the_qr_code_and_the_verify_code_are_masked_in_what_the_server_says(upstream, fake_ilink):
    fake_ilink.script("get_qrcode_status", error(9, "bad qr-secret-code-1 and 654321"))

    with pytest.raises(UpstreamError) as caught:
        upstream.qr_status("qr-secret-code-1", "654321", None)

    assert str(caught.value) == "9 bad *** and ***"
