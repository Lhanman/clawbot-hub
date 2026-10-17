"""联网守卫（任务 0.2）：单元测试里连非回环地址一律抛"测试里不允许联网"，回环放行。

守卫由 `tests/conftest.py` 的自动夹具给每个测试装上，所以这里的测试不用自己装；
`live` 标记的真机测试不装守卫（那是它们存在的意义）。
"""

from __future__ import annotations

import os
import socket
import tempfile
import urllib.request
from pathlib import Path

import pytest
import requests

from tests.support import netguard
from tests.support.netguard import NetworkBlocked

REPO_ROOT = Path(__file__).resolve().parent.parent

# 文档示例地址（TEST-NET-3、2001:db8::/32），本来就不该被路由，但守卫要在连之前就拦下
PUBLIC_V4 = "203.0.113.10"
PUBLIC_V6 = "2001:db8::10"


def _listening_socket(family: int = socket.AF_INET, host: str = "127.0.0.1") -> socket.socket:
    server = socket.socket(family, socket.SOCK_STREAM)
    server.bind((host, 0))
    server.listen(1)
    return server


# ---------- 非回环：拦 ----------


def test_connect_to_public_ipv4_is_blocked():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(NetworkBlocked, match="测试里不允许联网"):
            sock.connect((PUBLIC_V4, 443))


def test_connect_to_public_ipv6_is_blocked():
    # 守卫在碰到套接字之前就判断地址，所以这里用 AF_INET 的套接字也行：
    # 有的机器（包括云里的容器）根本建不出 IPv6 套接字，但这条判断照样要测
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(NetworkBlocked, match="测试里不允许联网"):
            sock.connect((PUBLIC_V6, 443, 0, 0))


def test_connect_ex_is_blocked_too():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(NetworkBlocked):
            sock.connect_ex((PUBLIC_V4, 443))


def test_udp_sendto_to_public_address_is_blocked():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        with pytest.raises(NetworkBlocked):
            sock.sendto(b"x", (PUBLIC_V4, 53))


def test_wildcard_and_private_lan_addresses_are_not_loopback():
    # 0.0.0.0 和局域网地址都不算回环：测试只该跟本机的假服务说话
    for host in ("0.0.0.0", "192.168.1.20", "10.0.0.5"):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            with pytest.raises(NetworkBlocked):
                sock.connect((host, 80))


def test_message_names_the_address_but_blocks_before_any_dns():
    # 按名字连：必须在解析域名之前就拦下（异常是守卫的，不是 gaierror）
    with pytest.raises(NetworkBlocked, match="ilinkai.weixin.qq.com"):
        socket.create_connection(("ilinkai.weixin.qq.com", 443), timeout=1)


def test_getaddrinfo_of_public_name_is_blocked():
    with pytest.raises(NetworkBlocked, match="测试里不允许联网"):
        socket.getaddrinfo("example.com", 443)


def test_requests_to_public_host_raises_the_guard_error_not_connection_error():
    # 守卫的异常不是 OSError：requests 不会把"误联网"包成 ConnectionError 悄悄吞掉
    with pytest.raises(NetworkBlocked) as info:
        requests.get("http://example.com/", timeout=1)
    assert not isinstance(info.value, requests.ConnectionError)


def test_the_guard_error_is_not_an_oserror():
    assert not issubclass(NetworkBlocked, OSError)
    assert issubclass(NetworkBlocked, RuntimeError)


# ---------- 回环：放行 ----------


def test_loopback_ipv4_tcp_roundtrip_is_allowed():
    with _listening_socket() as server:
        port = server.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            conn, _ = server.accept()
            with conn:
                client.sendall(b"ping")
                assert conn.recv(4) == b"ping"


def test_localhost_name_is_allowed():
    with _listening_socket() as server:
        port = server.getsockname()[1]
        with socket.create_connection(("localhost", port), timeout=2):
            pass


def test_loopback_ipv6_is_allowed():
    try:
        server = _listening_socket(socket.AF_INET6, "::1")
    except OSError:
        pytest.skip("本机没有 IPv6 回环")
    with server:
        port = server.getsockname()[1]
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(("::1", port, 0, 0))


def test_loopback_udp_is_allowed():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(2)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(b"x", receiver.getsockname())
        assert receiver.recv(1) == b"x"


def test_getaddrinfo_of_loopback_names_is_allowed():
    assert socket.getaddrinfo("127.0.0.1", 80)
    assert socket.getaddrinfo("localhost", 80)
    assert socket.getaddrinfo(None, 80, flags=socket.AI_PASSIVE)


def test_unix_sockets_are_allowed():
    # AF_UNIX 的路径在 macOS 上最长 104 字节，pytest 的临时目录在 Mac 上有一百多字节：
    # 用 /tmp 下的短目录
    with tempfile.TemporaryDirectory(prefix="hub", dir="/tmp") as folder:
        path = os.path.join(folder, "s.sock")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(path)
            server.listen(1)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(path)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("127.255.255.254", True),
        ("::1", True),
        ("::ffff:127.0.0.1", True),
        ("localhost", True),
        ("LOCALHOST", True),
        ("0.0.0.0", False),
        ("::", False),
        ("192.168.0.1", False),
        ("10.1.2.3", False),
        ("169.254.1.1", False),
        ("8.8.8.8", False),
        ("2001:db8::1", False),
        ("fe80::1%lo", False),
        ("example.com", False),
        ("localhost.evil.example", False),
        ("", False),
    ],
)
def test_is_loopback_host_table(host, expected):
    assert netguard.is_loopback_host(host) is expected


# ---------- 守卫本身 ----------


def test_guard_is_on_in_every_test_by_default():
    assert netguard.is_active()


def test_block_network_restores_what_it_found_even_when_nested():
    outer = socket.socket.connect  # 自动夹具装的那份
    with netguard.block_network():
        assert socket.socket.connect is not outer  # 内层又套了一层
        with pytest.raises(NetworkBlocked):
            socket.create_connection((PUBLIC_V4, 80), timeout=1)
    assert socket.socket.connect is outer
    assert netguard.is_active()


def test_proxy_environment_is_cleared_so_loopback_calls_never_go_through_a_proxy():
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        assert name not in os.environ
    # getproxies() 还会列出 NO_PROXY、GLOBAL_AGENT_HTTPS_PROXY 之类，不影响 requests；
    # 真正会让请求绕道的只有 http / https / all 三项
    assert not {"http", "https", "all"} & set(urllib.request.getproxies())
    # 回环地址明说不走代理（`[::1]` 也是）：环境变量里没有代理项时 Python 会去读系统设置，
    # Mac 上开着代理软件，系统设置里的例外名单通常只有 127.0.0.1 和 localhost
    for host, url in (
        ("127.0.0.1", "http://127.0.0.1:80/"),
        ("localhost", "http://localhost:80/"),
        ("::1", "http://[::1]:80/"),
    ):
        assert urllib.request.proxy_bypass(host), host
        assert requests.utils.get_environ_proxies(url) == {}, url


def test_hub_home_and_home_point_into_a_temp_dir_never_the_real_one():
    home = Path(os.environ["HOME"]).resolve()
    hub_home = Path(os.environ["CLAWBOT_HUB_HOME"]).resolve()
    assert Path.home().resolve() == home
    assert hub_home.is_relative_to(home)
    # 夹具给的是一个空目录，测试里不会碰到真实的数据目录
    assert not hub_home.exists() or not any(hub_home.iterdir())


# ---------- live 标记的测试不装守卫（子进程里验证，免得受外层守卫影响）----------


def test_live_marked_tests_run_without_the_guard(pytester, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    pytester.makeini("[pytest]\nmarkers =\n    live: real-network tests\n")
    pytester.makeconftest((Path(__file__).with_name("conftest.py")).read_text(encoding="utf-8"))
    pytester.makepyfile(
        """
        import pytest
        from tests.support import netguard

        @pytest.mark.live
        def test_live_has_no_guard():
            assert not netguard.is_active()

        def test_normal_has_guard():
            assert netguard.is_active()
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=2)
