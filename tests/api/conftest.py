"""接口测试共用的夹具：一整套部件（`tests/support/hub.py`）、接在它们上面的应用、能指定来源地址的
客户端。

客户端是 Starlette 的 `TestClient`（进程内，不走网络）：它的 `client=(地址, 端口)` 参数就是"注入来源
地址"，接口看到的连接对端就是这个地址。真走 HTTP 的验证在 `test_e2e_send.py`。
"""

from __future__ import annotations

import itertools
import threading
import time
import warnings
from contextlib import ExitStack

import pytest
import uvicorn

from clawbot_hub.api import build_app
from clawbot_hub.app import server_config
from clawbot_hub.config import Config
from clawbot_hub.services import Services
from tests.support.fake_clock import FakeClock
from tests.support.hub import Hub

with warnings.catch_warnings():
    # Starlette 的测试客户端提示"请改用 httpx2"；项目的开发依赖还是 httpx，两者用法一样
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient

LOCAL = "127.0.0.1"
LAN = "192.168.1.20"
OUTSIDE = "203.0.113.5"

__all__ = ["LAN", "LOCAL", "OUTSIDE", "Api", "bearer"]


def bearer(key: str | None) -> dict[str, str]:
    return {} if key is None else {"Authorization": f"Bearer {key}"}


class Api:
    """一整套部件 + 应用 + 客户端。`call` 系列用项目名取密钥，`source` 是来源地址。"""

    def __init__(self, hub: Hub, stack: ExitStack) -> None:
        self.hub = hub
        self.services = Services(
            config=hub.config,
            clock=hub.clock,
            paths=hub.paths,
            db=hub.db,
            store=hub.store,
            monitor=hub.monitor,
            projects=hub.projects,
            quota=hub.quota,
            sender=hub.sender,
        )
        self.app = build_app(self.services)
        self._stack = stack
        self._clients: dict[str, TestClient] = {}
        self._tokens = itertools.count(1)

    def client(self, source: str = LOCAL) -> TestClient:
        if source not in self._clients:
            client = TestClient(self.app, client=(source, 50000), raise_server_exceptions=False)
            self._stack.enter_context(client)  # 开着，请求之间不用反复起线程
            self._clients[source] = client
        return self._clients[source]

    def key(self, name: str = "alpha") -> str:
        return self.hub.keys[name]

    def request(self, method: str, path: str, *, key: str | None = None, source: str = LOCAL, **kw):
        headers = {**bearer(key), **kw.pop("headers", {})}
        return self.client(source).request(method, path, headers=headers, **kw)

    def get(self, path: str, *, as_: str | None = "alpha", source: str = LOCAL, **kw):
        """用项目 `as_` 的密钥（`None` 就不带）请求。"""
        key = None if as_ is None else self.key(as_)
        return self.request("GET", path, key=key, source=source, **kw)

    def post(self, path: str, *, as_: str | None = "alpha", source: str = LOCAL, **kw):
        key = None if as_ is None else self.key(as_)
        return self.request("POST", path, key=key, source=source, **kw)

    def send(self, text: str = "你好", *, as_: str = "alpha", source: str = LOCAL, **fields):
        """`POST /v1/messages`，正文是 JSON：`{"text": …, **fields}`。"""
        return self.post("/v1/messages", as_=as_, source=source, json={"text": text, **fields})

    # ------------------------------------------------ 收话、取话、确认、回复

    def say(
        self,
        text: str = "持仓",
        *,
        to: str = "alpha",
        at_ms: int | None = None,
        received_ms: int | None = None,
        via: str = "default",
        has_text: bool = True,
        token: str | None = None,
        wake: bool = True,
    ) -> str:
        """使用者说了一句话、路由给项目 `to`（相当于 poller 做的事：入队、提交、叫醒）。返回
        待取记录的编号。对话凭证默认依次是 `ctx-in-1`、`ctx-in-2`……（和窗口的 `ctx-<n>` 分开）。
        `wake=False`：只入队、不叫醒等着的取话请求。"""
        now = self.hub.clock.now_ms()
        project = self.hub.projects.get(to)
        with self.hub.db.txn() as conn:
            queued = self.services.inbox.enqueue(
                conn,
                project,
                text=text,
                has_text=has_text,
                at_ms=now if at_ms is None else at_ms,
                received_ms=now if received_ms is None else received_ms,
                via=via,
                context_token=token or f"ctx-in-{next(self._tokens)}",
            )
        if wake:
            self.services.inbox.wake(project.id)
        return queued.id

    def fetch(self, query: str = "wait=0", *, as_: str | None = "alpha", **kw):
        """`GET /v1/inbox?<query>`。"""
        return self.get(f"/v1/inbox?{query}", as_=as_, **kw)

    def ack(self, ids, *, as_: str | None = "alpha", **kw):
        """`POST /v1/inbox/ack`，正文是 `{"ids": ids}`。"""
        return self.post("/v1/inbox/ack", as_=as_, json={"ids": ids}, **kw)

    def reply(self, handle, text: str = "收到", *, as_: str | None = "alpha", **fields):
        """`POST /v1/replies`，正文是 JSON：`{"handle": …, "text": …, **fields}`。"""
        return self.post("/v1/replies", as_=as_, json={"handle": handle, "text": text, **fields})

    def hear(self, text: str = "持仓", *, to: str = "alpha", **say_fields) -> dict:
        """使用者说一句话，项目取到它：返回取话给的那一项（带 `reply_handle`）。"""
        item_id = self.say(text, to=to, **say_fields)
        response = self.fetch("wait=0", as_=to)
        assert response.status_code == 200, response.text
        [item] = [item for item in response.json()["items"] if item["id"] == item_id]
        return item


@pytest.fixture
def clock() -> FakeClock:
    """请求在别的线程里处理，发送要睡的重试间隔也用 `sleep` 立刻返回的假时钟。"""
    return FakeClock(auto_sleep=True)


@pytest.fixture
def make_api(paths, database, clock, fake_upstream):
    """造一套并接上应用；要改配置或换零件时传进来。项目要自己 `api.hub.ready()` / `add()`。"""
    with ExitStack() as stack:

        def build(config: Config | None = None, **sender_kwargs) -> Api:
            hub = Hub(paths, database, clock, fake_upstream, config, **sender_kwargs)
            return Api(hub, stack)

        yield build


@pytest.fixture
def api(make_api) -> Api:
    """默认配置，已绑定、使用者说过话、登记了项目 alpha（保底 6、上限 8，默认项目）。"""
    result = make_api()
    result.hub.ready()
    return result


@pytest.fixture
def serve():
    """`serve(api)`：在随机的本机端口上起一个真服务，返回它的地址和端口；测试结束时关掉。"""
    running: list[tuple[uvicorn.Server, threading.Thread]] = []

    def start(api) -> tuple[str, int]:
        settings = server_config(api.app, api.hub.config, port=0)
        server = uvicorn.Server(settings)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "服务没有起来"
            time.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        running.append((server, thread))
        return f"http://127.0.0.1:{port}", port

    yield start
    for server, thread in running:
        server.should_exit = True
        thread.join(10)
