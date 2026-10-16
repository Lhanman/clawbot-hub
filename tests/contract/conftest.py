"""合约测试共用的装置（任务 D5）。

项目这一侧能用到的全部行为，用**真的 HTTP 服务**（本机回环上的随机端口）加**单文件客户端**跑一遍：
假上游代替腾讯，收消息线程和服务回复线程接在同一个待取队列上（`hubctl serve` 的接线是 D6 的事，
这里手工接，和 `tests/api/test_e2e_inbox.py` 一样）。时间是假时钟：服务里的等待、租期、保留期都由
测试 `advance()`，客户端自己的读超时、退避和心跳仍然是真的。

两个项目：alpha 是默认项目（保底 6、上限 8），beta 的前缀是“贝塔”（保底 1、上限 2）。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import clawbot_hub_client as hub_client
import pytest

from clawbot_hub.config import Config
from clawbot_hub.poller import Poller
from clawbot_hub.replier import Replier
from clawbot_hub.upstream.base import IncomingMessage
from tests.api.conftest import Api, clock, make_api, serve  # noqa: F401 - 夹具，按名字被用到
from tests.support.hub import USER
from tests.support.threads import run_in_thread, wait_for

# 项目的处理函数：拿到一句话和它自己的客户端（回复要用）
Handler = Callable[[hub_client.Inbound, hub_client.HubClient], object]


class Consumer:
    """项目这一侧的取话循环：单文件客户端的 `run_inbox` 跑在一个线程里，处理函数默认只记下每一项。

    服务里的等待由假时钟管，所以长轮询要等测试 `advance()`；`stop()` 在循环还挂在长轮询上时把假时间
    往前推一截让它返回。睡觉（退避、空转保护）不真睡，只记在 `pauses` 里。"""

    def __init__(
        self,
        rig: Rig,
        name: str,
        handler: Handler | None,
        *,
        wait: int = 25,
        heartbeat_seconds: float = 3600.0,
        on_pause: Callable[[list[float]], None] | None = None,
    ) -> None:
        self.rig = rig
        self.name = name
        self.client = rig.client(name)
        self.seen: list[hub_client.Inbound] = []
        self.pauses: list[float] = []
        self._handler = handler
        self._on_pause = on_pause
        self._wait = wait
        self._stop = threading.Event()
        self._worker = run_in_thread(
            self.client.run_inbox,
            self._handle,
            stop=self._stop.is_set,
            wait=wait,
            sleep=self._pause,
            heartbeat_seconds=heartbeat_seconds,
        )
        # 服务看到它在取话了，再往下做
        wait_for(lambda: rig.hub.projects.get(name).last_inbox_ms is not None)

    def _handle(self, item: hub_client.Inbound) -> None:
        self.seen.append(item)
        if self._handler is not None:
            self._handler(item, self.client)

    def _pause(self, seconds: float) -> None:
        self.pauses.append(seconds)
        if self._on_pause is not None:
            self._on_pause(self.pauses)
        time.sleep(min(seconds, 0.005))

    @property
    def texts(self) -> list[str]:
        return [item.text for item in self.seen]

    def failure(self, timeout: float = 5.0) -> BaseException | None:
        """等循环结束，返回它抛出的异常（正常结束是 `None`）。"""
        try:
            self._worker.result(timeout)
        except Exception as exc:  # noqa: BLE001 - 原样交给测试看
            return exc
        return None

    def stop(self, *, raise_errors: bool = True) -> None:
        """让循环停下来并等它退出；循环里抛过的异常在这里重新抛出。"""
        self._stop.set()
        deadline = time.monotonic() + 10
        while self._worker.still_blocked(0.05) and time.monotonic() < deadline:
            self.rig.clock.advance(self._wait + 5)
        try:
            self._worker.result()
        except Exception:
            if raise_errors:
                raise


@dataclass
class Rig:
    api: Api
    url: str
    port: int
    consumers: list[Consumer] = field(default_factory=list)

    @property
    def hub(self):
        return self.api.hub

    @property
    def upstream(self):
        return self.api.hub.upstream

    @property
    def clock(self):
        return self.api.hub.clock

    def key(self, name: str = "alpha") -> str:
        return self.api.key(name)

    def client(self, name: str = "alpha", **kwargs) -> hub_client.HubClient:
        """项目 `name` 的单文件客户端，对着这个真服务。"""
        return hub_client.HubClient(self.url, self.key(name), **kwargs)

    def say(self, text: str, token: str | None = None) -> IncomingMessage:
        """使用者在微信里说一句话：进假上游的收件箱，收消息线程取走、路由、入队。
        不给 `token` 就让假上游自己编一个对话凭证（没有凭证的消息服务不会收）。"""
        if token is None:
            return self.upstream.push_user_message(text)
        return self.upstream.push_user_message(text, context_token=token)

    def flush(self) -> IncomingMessage:
        """等到使用者之前说的话都处理完、服务该回的话都发出去了：再问一句服务的指令，它的回复排在
        前面那些回复后面（收消息线程一句一句处理，服务回复线程按先后发），等到它发出来就行。
        返回这句指令（窗口里记的对话凭证现在是它的）。"""
        before = len(self.hub.texts)
        said = self.say("服务 项目")
        wait_for(lambda: len(self.hub.texts) > before)
        return said

    def present(self, name: str = "alpha") -> None:
        """让服务知道项目 `name` 在线（发一次心跳）。"""
        self.client(name).inbox(wait=0, max=0)

    def take(self, name: str = "alpha", count: int = 1) -> list[hub_client.Inbound]:
        """项目 `name` 取话（`wait=0` 轮询），直到取满 `count` 句：用来等使用者的话到。"""
        client = self.client(name)
        got: list[hub_client.Inbound] = []

        def enough() -> bool:
            got.extend(client.inbox(wait=0, max=50))
            return len(got) >= count

        wait_for(enough)
        return got

    def consume(self, name: str = "alpha", handler: Handler | None = None, **kwargs) -> Consumer:
        """起项目 `name` 的取话循环（`run_inbox`），测试结束时自动停。"""
        consumer = Consumer(self, name, handler, **kwargs)
        self.consumers.append(consumer)
        return consumer


@pytest.fixture
def make_rig(make_api, serve):  # noqa: F811
    """`make_rig(config=None)`：造一套真服务；要改配置时传进来。"""
    started: list[tuple[Poller, Replier]] = []
    rigs: list[Rig] = []

    def build(config: Config | None = None) -> Rig:
        api = make_api(config)
        hub = api.hub
        hub.ready("alpha")
        hub.add("beta", prefix="贝塔", guaranteed=1, cap=2)
        hub.upstream.user_id = USER  # 假上游里的使用者就是凭证文件里的 user_id
        url, port = serve(api)
        replier = Replier(hub.sender, hub.config.hub.label)
        poller = Poller(
            hub.config,
            hub.clock,
            hub.db,
            hub.upstream,
            hub.store,
            hub.monitor,
            hub.projects,
            hub.quota,
            api.services.inbox,
            replier,
        )
        replier.start()
        poller.start()
        started.append((poller, replier))
        rig = Rig(api, url, port)
        rigs.append(rig)
        return rig

    yield build
    for rig in rigs:
        for consumer in rig.consumers:
            consumer.stop(raise_errors=False)
    for poller, replier in started:
        poller.stop(10)
        replier.stop(10)


@pytest.fixture
def rig(make_rig) -> Rig:
    return make_rig()
