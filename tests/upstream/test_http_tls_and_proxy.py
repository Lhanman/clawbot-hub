"""加密连接和代理上的"发出去了没有"（任务 A4）。

明文 HTTP 上的分类在 `test_http_outcomes.py`。腾讯的接口是 https，经代理时还要先发一次 `CONNECT`
建隧道（那一次不是我们的请求，不能记成"已经发了"）：这几条路径用的连接类各不相同，要各测一遍。
任何一处没把"请求开始写出去"记下来，发出去之后才出的错就会被当成"没发出去"，被 sender 重发。

https 的假腾讯用本机 `openssl` 现生成的自签证书（`tests/support/tls.py`），客户端通过
`REQUESTS_CA_BUNDLE` 信任它，和生产里换 CA 包的办法一样。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clawbot_hub.upstream.base import Accepted, NoResponse, PollFailed, Transient, Updates
from tests.support import tls
from tests.support.fake_ilink import FakeIlink, close, hang, reset, truncated
from tests.support.fake_proxy import SwallowingProxy, TunnelProxy
from tests.upstream.conftest import account_at, poll, send, upstream_at

DIES = pytest.mark.parametrize(
    "make", [reset, close, truncated], ids=["reset", "closed", "half a body"]
)


@pytest.fixture(scope="session")
def certificate(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    if not tls.openssl_available():
        pytest.skip("本机没有 openssl 命令，生成不了测试证书")
    return tls.make_certificate(tmp_path_factory.mktemp("tls"))


@pytest.fixture
def secure_ilink(certificate, monkeypatch):
    """一个说 https 的假腾讯；客户端信任它的自签证书。"""
    cert, key = certificate
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(cert))
    with FakeIlink(ssl_context=tls.server_context(cert, key)) as fake:
        yield fake


def sent_once(fake: FakeIlink) -> None:
    assert len(fake.requests_to("sendmessage")) == 1


# ---------------------------------------------------------------- 加密连接


def test_the_fake_really_speaks_https(secure_ilink):
    assert secure_ilink.base_url.startswith("https://")


def test_sending_and_polling_work_over_tls(secure_ilink):
    base = secure_ilink.base_url
    client, local = upstream_at(base), account_at(base)

    assert isinstance(send(client, local), Accepted)
    assert isinstance(poll(client, local), Updates)
    assert [r.endpoint for r in secure_ilink.requests] == ["sendmessage", "getupdates"]


@DIES
def test_a_connection_that_dies_after_the_request_over_tls_is_no_response(secure_ilink, make):
    base = secure_ilink.base_url
    secure_ilink.script("sendmessage", make())

    outcome = send(upstream_at(base), account_at(base))

    assert isinstance(outcome, NoResponse) and outcome.read_timeout is False
    sent_once(secure_ilink)


def test_a_read_timeout_over_tls_is_no_response(secure_ilink):
    base = secure_ilink.base_url
    secure_ilink.script("sendmessage", hang())

    outcome = send(upstream_at(base), account_at(base), timeout=0.4)

    assert isinstance(outcome, NoResponse) and outcome.read_timeout is True
    sent_once(secure_ilink)


def test_a_connection_that_dies_during_a_poll_over_tls_is_a_failed_poll(secure_ilink):
    base = secure_ilink.base_url
    secure_ilink.script("getupdates", reset())

    result = poll(upstream_at(base), account_at(base))

    assert isinstance(result, PollFailed) and result.completed is False


def test_a_certificate_nobody_trusts_is_transient_and_nothing_was_sent(certificate, monkeypatch):
    cert, key = certificate
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
    with FakeIlink(ssl_context=tls.server_context(cert, key)) as fake:
        outcome = send(upstream_at(fake.base_url), account_at(fake.base_url))

        assert isinstance(outcome, Transient)
        assert fake.requests == []


# ---------------------------------------------------------------- 经 CONNECT 代理的 https


def test_a_request_through_a_connect_proxy_reaches_the_server(secure_ilink, monkeypatch):
    with TunnelProxy(secure_ilink.port) as proxy:
        monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{proxy.port}")
        base = f"https://{tls.HOSTNAME}"

        outcome = send(upstream_at(base), account_at(base))

    assert isinstance(outcome, Accepted)
    assert proxy.connects == 1
    assert proxy.request_lines[0].startswith(f"CONNECT {tls.HOSTNAME}:443 ")
    sent_once(secure_ilink)


@DIES
def test_a_connection_that_dies_after_the_request_through_a_tunnel_is_no_response(
    secure_ilink, monkeypatch, make
):
    """隧道建好以后发的请求要记成"已经发了"：隧道里出的错是"不知道结果"，不是"没发出去"。"""
    secure_ilink.script("sendmessage", make())
    with TunnelProxy(secure_ilink.port) as proxy:
        monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{proxy.port}")
        base = f"https://{tls.HOSTNAME}"

        outcome = send(upstream_at(base), account_at(base))

    assert isinstance(outcome, NoResponse) and outcome.read_timeout is False
    assert proxy.connects == 1
    sent_once(secure_ilink)


def test_a_connection_that_dies_during_a_poll_through_a_tunnel_is_a_failed_poll(
    secure_ilink, monkeypatch
):
    secure_ilink.script("getupdates", reset())
    with TunnelProxy(secure_ilink.port) as proxy:
        monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{proxy.port}")
        base = f"https://{tls.HOSTNAME}"

        result = poll(upstream_at(base), account_at(base))

    assert isinstance(result, PollFailed) and result.completed is False
    assert len(secure_ilink.requests_to("getupdates")) == 1


# ---------------------------------------------------------------- 经代理的明文 http


def test_a_plain_request_through_a_proxy_that_swallows_it_is_no_response(monkeypatch):
    with SwallowingProxy() as proxy:
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.port}")
        base = "http://upstream.example"

        outcome = send(upstream_at(base), account_at(base))

    assert isinstance(outcome, NoResponse) and outcome.read_timeout is False
    assert proxy.request_lines == ["POST http://upstream.example/ilink/bot/sendmessage HTTP/1.1"]


# ---------------------------------------------------------------- SOCKS 代理不支持


@pytest.mark.parametrize("scheme", ["socks5", "socks5h", "socks4", "SOCKS5"])
def test_a_socks_proxy_is_refused_as_not_sent(monkeypatch, scheme):
    """SOCKS 的连接类不是我们的，判断不了"发出去了没有"：不发，按"没发出去"报，原因写清楚。"""
    with SwallowingProxy() as proxy:  # 万一真去连了代理，它的记录里就会有东西
        monkeypatch.setenv("ALL_PROXY", f"{scheme}://127.0.0.1:{proxy.port}")
        base = "http://upstream.example"

        outcome = send(upstream_at(base), account_at(base))

    assert isinstance(outcome, Transient)
    assert outcome.reason == "InvalidProxyURL: 不支持 SOCKS 代理"
    assert proxy.request_lines == []
