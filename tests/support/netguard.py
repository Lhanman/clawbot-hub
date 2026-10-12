"""联网守卫：测试里只放行本机回环（和 Unix 套接字），其余一律抛 `NetworkBlocked`。

抛的不是 `OSError`：requests / urllib3 会把 `OSError` 包成 `ConnectionError`，
测试里的 `pytest.raises(ConnectionError)` 就会把"误联网"当成"预期中的连接失败"悄悄放过去。
所以守卫的异常继承 `RuntimeError`，一路穿出来，测试直接红。

拦两层：解析域名（`getaddrinfo` 等，免得在没有 DNS 的机器上看到一个莫名其妙的解析失败）
和连接 / 发包（`connect`、`connect_ex`、`sendto`）。监听、绑定不拦，假服务要用。
"""

from __future__ import annotations

import contextlib
import functools
import ipaddress
import os
import socket
from collections.abc import Callable, Iterator
from typing import Any

# 装在被包装函数上的记号，`is_active()` 靠它判断守卫是否在位
_MARK = "_clawbot_network_guard"


class NetworkBlocked(RuntimeError):
    """测试里试图连非回环地址。"""


def is_loopback_host(host: str | bytes | None) -> bool:
    """是不是本机回环：127.0.0.0/8、::1、::ffff:127.x.x.x 和名字 localhost。"""
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not host:
        return False
    host = host.strip()
    if host.lower() == "localhost":
        return True
    if "%" in host:  # 带接口名的链路本地地址（fe80::1%lo）不算
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _describe(address: Any) -> str:
    if isinstance(address, tuple) and len(address) >= 2:
        return f"{address[0]}:{address[1]}"
    return str(address)


def _check_address(action: str, address: Any) -> None:
    """`connect` 类调用的地址：Unix 套接字的路径放行，IP 地址必须是回环。"""
    if isinstance(address, (str, bytes, os.PathLike)):
        return  # AF_UNIX 的地址是路径
    host = address[0] if isinstance(address, tuple) and address else None
    if not is_loopback_host(host):
        raise NetworkBlocked(f"测试里不允许联网：{action} {_describe(address)}")


def _check_name(action: str, host: Any) -> None:
    """解析域名：不给名字（被动监听）或本机回环放行。"""
    if host is None or host == "" or host == b"":
        return
    if not is_loopback_host(host):
        raise NetworkBlocked(f"测试里不允许联网：{action} {host!r}")


def _guard_connect_like(action: str, original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def guarded(self: socket.socket, address: Any, *args: Any, **kwargs: Any) -> Any:
        _check_address(action, address)
        return original(self, address, *args, **kwargs)

    setattr(guarded, _MARK, True)
    return guarded


def _guard_sendto(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def guarded(self: socket.socket, data: Any, *args: Any, **kwargs: Any) -> Any:
        # sendto(data, address) 或 sendto(data, flags, address)：地址总是最后一个位置参数
        address = args[-1] if args else kwargs.get("address")
        _check_address("sendto", address)
        return original(self, data, *args, **kwargs)

    setattr(guarded, _MARK, True)
    return guarded


def _guard_resolver(action: str, original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def guarded(host: Any, *args: Any, **kwargs: Any) -> Any:
        _check_name(action, host)
        return original(host, *args, **kwargs)

    setattr(guarded, _MARK, True)
    return guarded


# (所有者, 属性名, 怎么包)
_TARGETS: list[tuple[Any, str, Callable[[str, Callable[..., Any]], Callable[..., Any]]]] = [
    (socket.socket, "connect", lambda _n, f: _guard_connect_like("connect", f)),
    (socket.socket, "connect_ex", lambda _n, f: _guard_connect_like("connect_ex", f)),
    (socket.socket, "sendto", lambda _n, f: _guard_sendto(f)),
    (socket, "getaddrinfo", lambda _n, f: _guard_resolver("getaddrinfo", f)),
    (socket, "gethostbyname", lambda _n, f: _guard_resolver("gethostbyname", f)),
    (socket, "gethostbyname_ex", lambda _n, f: _guard_resolver("gethostbyname_ex", f)),
]


def is_active() -> bool:
    """守卫现在是否在位。"""
    return bool(getattr(socket.socket.connect, _MARK, False))


@contextlib.contextmanager
def block_network() -> Iterator[None]:
    """进入时装上守卫，退出时还原成进入前的样子（可以嵌套）。"""
    saved: list[tuple[Any, str, bool, Any]] = []
    try:
        for owner, name, wrap in _TARGETS:
            had_own = name in vars(owner)
            original = getattr(owner, name)
            saved.append((owner, name, had_own, original))
            setattr(owner, name, wrap(name, original))
        yield
    finally:
        for owner, name, had_own, original in reversed(saved):
            if had_own:
                setattr(owner, name, original)
            else:
                delattr(owner, name)  # 原本是从父类继承来的，删掉自己加的就还原了
