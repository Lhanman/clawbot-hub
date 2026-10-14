"""来源地址：本机（回环）、局域网（私有网段）、其他。

接口只看 TCP 连接的对端地址，不看任何请求头（`X-Forwarded-For` 之类谁都能写）。两处用到：

- 局域网监听打开（`server.lan`）时，只接受本机和私有网段的来源，其他来源 403
  （`hub-deploy`「谁能连」）；
- 认证失败的限流只算局域网来源，本机的进程不计也不受限（`hub-projects`「项目密钥」）。

IPv4 映射的 IPv6 地址（`::ffff:192.168.1.20`，双栈监听时 IPv4 连接会这样写）按它里面的 IPv4 地址算。
认不出来的来源（不是地址）一律按"其他"算：不放行，限流时算同一个来源。
"""

from __future__ import annotations

import ipaddress

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

LOOPBACK = tuple(ipaddress.ip_network(net) for net in ("127.0.0.0/8", "::1/128"))
PRIVATE = tuple(
    ipaddress.ip_network(net)
    for net in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "fc00::/7",
        "fe80::/10",
    )
)

UNKNOWN_SOURCE = "unknown"


def parse(host: object) -> IPAddress | None:
    """把对端地址读成 IP 地址；不是地址返回 `None`。IPv4 映射的 IPv6 地址换成 IPv4，
    链路本地地址后面的网卡名（`fe80::1%en0`）去掉。"""
    if not isinstance(host, str):
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped
        return ipaddress.IPv6Address(int(address))
    return address


def _within(address: IPAddress | None, networks: tuple) -> bool:
    return address is not None and any(address in net for net in networks)


def is_loopback(host: object) -> bool:
    """127.0.0.0/8 或 `::1`。"""
    return _within(parse(host), LOOPBACK)


def is_private_source(host: object) -> bool:
    """本机或私有网段（10.0.0.0/8、172.16.0.0/12、192.168.0.0/16、169.254.0.0/16、127.0.0.0/8、
    `::1`、fc00::/7、fe80::/10）。局域网监听打开时，只有这样的来源能连。"""
    address = parse(host)
    return _within(address, LOOPBACK) or _within(address, PRIVATE)


def source_key(host: object) -> str:
    """限流时用来认"同一个来源"的写法：同一个地址的各种写法得到同一个键。"""
    address = parse(host)
    return UNKNOWN_SOURCE if address is None else str(address)
