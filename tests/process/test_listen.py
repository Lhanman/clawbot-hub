"""绑定端口失败时的提示（任务 D6，规格 `hub-deploy`「谁能连」「启动自检和顺序」第 7 步）。

端口被别的程序占着，在真系统上测过（`test_startup.py`、`test_lifecycle.py`）；别的失败——没有权限、
这台机器没有这个地址、其他——在测试机上造不出来，所以这里用一个假的套接字代替 `app.py` 里的
`socket` 模块（只换 `app.py` 里的名字，不碰真的 `socket`，别的测试和线程不受影响）。
"""

from __future__ import annotations

import asyncio
import errno
import os
import socket
import types
from pathlib import Path

import pytest
import uvicorn

import clawbot_hub.app as app_module
from clawbot_hub.app import EXIT_FAILED, Service, StartupError
from clawbot_hub.config import Config
from clawbot_hub.paths import HubPaths
from tests.support.fake_clock import FakeClock
from tests.support.fake_upstream import FakeUpstream


class FakeSocket:
    """`bind` 按剧本失败；记下它是怎么造的、绑到哪里、有没有关。"""

    made: list[FakeSocket] = []
    failure: BaseException | None = None

    def __init__(self, family: int, kind: int) -> None:
        self.family = family
        self.kind = kind
        self.options: list[tuple[int, int, int]] = []
        self.address: tuple[str, int] | None = None
        self.backlog: int | None = None
        self.closed = False
        self.made.append(self)

    def setsockopt(self, level: int, name: int, value: int) -> None:
        self.options.append((level, name, value))

    def bind(self, address: tuple[str, int]) -> None:
        if self.failure is not None:
            raise self.failure
        self.address = address

    def listen(self, backlog: int) -> None:
        self.backlog = backlog

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_socket(monkeypatch) -> type[FakeSocket]:
    # 每个测试一份新的：记录和剧本不串
    fake = type("FakeSocketForOneTest", (FakeSocket,), {"made": [], "failure": None})
    stand_in = types.SimpleNamespace(
        socket=fake,
        AF_INET=socket.AF_INET,
        AF_INET6=socket.AF_INET6,
        SOCK_STREAM=socket.SOCK_STREAM,
        SOL_SOCKET=socket.SOL_SOCKET,
        SO_REUSEADDR=socket.SO_REUSEADDR,
    )
    monkeypatch.setattr(app_module, "socket", stand_in)
    return fake


# ---------------------------------------------------------------- 绑不上

CASES = [
    pytest.param(
        OSError(errno.EADDRINUSE, os.strerror(errno.EADDRINUSE)),
        "127.0.0.1",
        8790,
        ["端口 8790 已被占用（127.0.0.1）", "server.port"],
        id="address-in-use",
    ),
    pytest.param(
        OSError(errno.EACCES, os.strerror(errno.EACCES)),
        "127.0.0.1",
        80,
        ["没有权限监听 127.0.0.1:80", "1024 以下", "server.port"],
        id="no-permission",
    ),
    pytest.param(
        OSError(errno.EADDRNOTAVAIL, os.strerror(errno.EADDRNOTAVAIL)),
        "192.0.2.7",
        8790,
        ["这台机器上没有地址 192.0.2.7", "server.host"],
        id="no-such-address",
    ),
    pytest.param(
        OSError(errno.ENETDOWN, os.strerror(errno.ENETDOWN)),
        "::1",
        8790,
        ["监听 ::1:8790 失败：", os.strerror(errno.ENETDOWN)],
        id="anything-else",
    ),
    pytest.param(
        OSError(),
        "127.0.0.1",
        8790,
        ["监听 127.0.0.1:8790 失败：OSError"],
        id="no-explanation-at-all",
    ),
]


@pytest.mark.parametrize(("failure", "host", "port", "parts"), CASES)
def test_each_way_the_bind_can_fail_gets_its_own_one_line_reason(
    fake_socket, failure, host, port, parts
):
    fake_socket.failure = failure

    with pytest.raises(StartupError) as caught:
        app_module._bind(host, port)

    message = str(caught.value)
    assert "\n" not in message
    for part in parts:
        assert part in message
    assert caught.value.exit_code == EXIT_FAILED
    [made] = fake_socket.made
    assert made.closed is True  # 绑不上也不留着半截套接字


def test_only_the_matching_reason_is_given(fake_socket):
    """每种失败只说自己那一句，不会因为别的分支的条件写宽了而串味。"""
    texts = {}
    for case in CASES:
        failure, host, port, _parts = case.values
        fake_socket.failure = failure
        with pytest.raises(StartupError) as caught:
            app_module._bind(host, port)
        texts[case.id] = str(caught.value)

    assert "已被占用" in texts["address-in-use"]
    assert sum("已被占用" in text for text in texts.values()) == 1
    assert sum("没有权限" in text for text in texts.values()) == 1
    assert sum("没有地址" in text for text in texts.values()) == 1


# ---------------------------------------------------------------- 绑得上

BINDABLE = [
    ("127.0.0.1", socket.AF_INET),
    ("0.0.0.0", socket.AF_INET),
    ("::1", socket.AF_INET6),
    ("::", socket.AF_INET6),
]


@pytest.mark.parametrize(("host", "family"), BINDABLE)
def test_the_address_family_follows_the_host(fake_socket, host, family):
    sock = app_module._bind(host, 8790)

    assert sock.family == family
    assert sock.kind == socket.SOCK_STREAM
    assert sock.address == (host, 8790)
    assert sock.backlog is not None and sock.backlog > 0  # 绑完就开始监听
    assert sock.closed is False


def test_the_address_may_be_reused_so_a_quick_restart_is_not_blocked(fake_socket):
    sock = app_module._bind("127.0.0.1", 8790)

    assert (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) in sock.options


# ---------------------------------------------------------------- 错误本身


def test_a_startup_error_reads_as_its_message_alone():
    error = StartupError("已经有一个在运行（进程号 7）", 2)

    assert str(error) == "已经有一个在运行（进程号 7）"
    assert error.exit_code == 2


def test_a_startup_error_exits_with_1_unless_told_otherwise():
    assert StartupError("x").exit_code == EXIT_FAILED == 1


# ---------------------------------------------------------------- 等到真的起来


def make_server() -> app_module._Server:
    config = uvicorn.Config(app=lambda scope, receive, send: None, log_config=None, lifespan="off")
    return app_module._Server(config)


def test_the_server_is_not_called_ready_before_uvicorn_really_started(monkeypatch):
    async def stays_down(self, sockets=None) -> None:
        return None  # `started` 仍是假

    monkeypatch.setattr(uvicorn.Server, "startup", stays_down)
    server = make_server()

    asyncio.run(server.startup())

    assert server.started is False
    assert not server.ready.is_set()


def test_the_server_is_ready_as_soon_as_uvicorn_started(monkeypatch):
    async def comes_up(self, sockets=None) -> None:
        self.started = True

    monkeypatch.setattr(uvicorn.Server, "startup", comes_up)
    server = make_server()

    asyncio.run(server.startup())

    assert server.ready.is_set()


# ---------------------------------------------------------------- 本机访问的地址

URLS = [
    ("127.0.0.1", "http://127.0.0.1:8790"),
    ("127.0.0.2", "http://127.0.0.2:8790"),
    ("0.0.0.0", "http://127.0.0.1:8790"),  # 监听所有网卡时，本机用回环地址连
    ("::1", "http://[::1]:8790"),  # IPv6 的地址要加方括号
]


@pytest.mark.parametrize(("host", "url"), URLS)
def test_the_local_url_is_one_a_client_on_this_machine_can_use(host, url):
    clock = FakeClock()
    service = Service(Config(), HubPaths(Path("/nowhere")), clock, FakeUpstream(clock=clock))
    service.listening = (host, 8790)

    assert service.url == url
