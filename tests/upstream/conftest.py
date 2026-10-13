"""上游适配层测试共用的夹具和小函数：真 HTTP 的假腾讯（`fake_ilink`）加上被测的 `HttpUpstream`。"""

from __future__ import annotations

import socket

import pytest

from clawbot_hub.config import UpstreamConfig
from clawbot_hub.upstream.base import Account
from clawbot_hub.upstream.http import HttpUpstream

TOKEN = "tok-secret-123"
CONTEXT = "ctx-secret-456"


@pytest.fixture
def account(fake_ilink) -> Account:
    return Account(
        bot_token=TOKEN,
        base_url=fake_ilink.base_url,
        bot_id=fake_ilink.bot_id,
        user_id=fake_ilink.user_id,
    )


@pytest.fixture
def upstream(fake_ilink) -> HttpUpstream:
    return HttpUpstream(UpstreamConfig(base_url=fake_ilink.base_url))


def closed_port() -> int:
    """一个现在没人听的本机端口（连上去会被拒绝）。"""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def account_at(base_url: str, token: str = TOKEN) -> Account:
    return Account(bot_token=token, base_url=base_url, bot_id="b", user_id="u")


def upstream_at(base_url: str, **kwargs) -> HttpUpstream:
    return HttpUpstream(UpstreamConfig(base_url=base_url), **kwargs)


def send(
    upstream: HttpUpstream,
    account: Account,
    text: str = "你好",
    *,
    client_id: str = "cid-1",
    context_token: str = CONTEXT,
    timeout: float = 5.0,
):
    return upstream.send_text(account, account.user_id, context_token, text, client_id, timeout)


def poll(upstream: HttpUpstream, account: Account, buf: str = "", *, timeout: float = 5.0):
    return upstream.get_updates(account, buf, timeout)
