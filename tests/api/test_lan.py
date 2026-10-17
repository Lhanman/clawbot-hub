"""来源地址怎么分类：回环（本机）、私有网段（局域网）、其他（公网和认不出来的）。规格：`hub-deploy`
「谁能连」（局域网开关打开后只认这些网段）、`hub-projects`「项目密钥」（认证失败的限流不管回环地址）。
"""

from __future__ import annotations

import pytest

from clawbot_hub.lan import is_loopback, is_private_source, source_key

LOOPBACK = [
    "127.0.0.1",
    "127.0.0.2",
    "127.255.255.254",
    "::1",
    "::ffff:127.0.0.1",  # 双栈监听时本机的 IPv4 连接会写成这样
]
PRIVATE = [
    "10.0.0.1",
    "10.255.255.255",
    "172.16.0.0",
    "172.16.0.1",
    "172.31.255.255",
    "192.168.0.1",
    "192.168.255.255",
    "169.254.0.1",
    "169.254.255.255",
    "fc00::1",
    "fd12:3456:789a::1",
    "fdff:ffff:ffff:ffff::1",
    "fe80::1",
    "febf::1",
    "fe80::1%en0",  # 带网卡名的链路本地地址
    "::ffff:192.168.1.20",
    "::ffff:10.1.2.3",
]
OUTSIDE = [
    "8.8.8.8",
    "203.0.113.5",
    "9.255.255.255",
    "11.0.0.0",
    "172.15.255.255",
    "172.32.0.0",
    "192.167.255.255",
    "192.169.0.0",
    "169.253.255.255",
    "169.255.0.0",
    "100.64.0.1",  # 运营商级 NAT 的地址不在名单里
    "0.0.0.0",
    "255.255.255.255",
    "2001:db8::1",
    "fbff::1",
    "fec0::1",
    "::",
    "::ffff:8.8.8.8",
    "::ffff:203.0.113.5",
]
JUNK = [
    "",
    " ",
    "testclient",
    "not-an-ip",
    "192.168.1",
    "192.168.1.20.5",
    "300.1.1.1",
    "::g",
    None,
    5,
]


@pytest.mark.parametrize("host", LOOPBACK)
def test_loopback_addresses(host):
    assert is_loopback(host)


@pytest.mark.parametrize("host", PRIVATE + OUTSIDE + JUNK)
def test_nothing_else_is_loopback(host):
    assert not is_loopback(host)


@pytest.mark.parametrize("host", LOOPBACK + PRIVATE)
def test_local_and_private_sources_are_allowed_in(host):
    assert is_private_source(host)


@pytest.mark.parametrize("host", OUTSIDE + JUNK)
def test_everything_else_is_kept_out(host):
    assert not is_private_source(host)


def test_the_throttle_key_is_the_same_for_every_way_of_writing_an_address():
    assert source_key("192.168.1.20") == source_key("::ffff:192.168.1.20")
    assert source_key("FE80::1") == source_key("fe80::0001")
    assert source_key("192.168.1.20") != source_key("192.168.1.21")


def test_an_address_that_cannot_be_read_still_gets_a_key():
    assert source_key(None) == source_key("")  # 认不出来的都算同一个来源
    assert source_key("testclient") == source_key(None)
    assert source_key(None) != source_key("192.168.1.20")
