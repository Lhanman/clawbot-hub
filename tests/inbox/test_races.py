"""取话的两个竞态（任务 C2，规格 `hub-inbox`「取话」「新请求顶掉旧请求」）。

一个请求"查库发现没话 → 睡下"之间，另一个线程可能入队叫醒它，或者来了新请求顶掉它。这两个空档
用假时钟的一个小钩子造出来：指定的线程第 N 次问时间时（取话请求正好在这些位置问时间：算期限、
租话一轮、查剩余时间），先插一段代码再回答。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from tests.inbox.conftest import World
from tests.support.threads import run_in_thread


@dataclass
class _Armed:
    skip: int
    hook: Callable[[], None]


class HookedClock:
    """包一层假时钟：给某个线程装一个钩子，它第 `skip + 1` 次问时间时跑一次（只跑一次）。"""

    def __init__(self, inner) -> None:
        self._inner = inner
        self._armed: dict[int, _Armed] = {}

    def arm_this_thread(self, hook: Callable[[], None], *, skip: int = 0) -> None:
        self._armed[threading.get_ident()] = _Armed(skip, hook)

    def now_ms(self) -> int:
        armed = self._armed.get(threading.get_ident())
        if armed is not None:
            if armed.skip == 0:
                del self._armed[threading.get_ident()]
                armed.hook()
            else:
                armed.skip -= 1
        return self._inner.now_ms()

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)

    def wait(self, condition: threading.Condition, timeout: float | None = None) -> bool:
        return self._inner.wait(condition, timeout)


@pytest.fixture
def hooked(clock) -> HookedClock:
    return HookedClock(clock)


@pytest.fixture
def raced(database, hooked) -> World:
    return World(database, hooked)


def texts(items) -> list[str]:
    return [item.text for item in items]


def test_a_message_arriving_between_the_check_and_the_sleep_is_not_missed(raced, hooked, clock):
    x = raced.add("xx")

    def request():
        # 取话请求问时间的顺序：算期限（1）、租话一轮里（2）、租完以后查剩余时间（3）。
        # 第 3 次正好是"这一轮没有话"和"睡下"之间
        hooked.arm_this_thread(lambda: raced.say(x, "夹在空档里的话"), skip=2)
        return raced.inbox.fetch(x, wait=30, max_items=10)

    started = clock.now_ms()
    worker = run_in_thread(request)
    assert texts(worker.result()) == ["夹在空档里的话"]  # 不用等下一个 5 秒
    assert clock.now_ms() == started


def test_a_superseded_request_does_not_take_the_messages_meant_for_the_new_one(
    raced, hooked, clock
):
    x = raced.add("xx")
    old = run_in_thread(raced.inbox.fetch, x, wait=30, max_items=10)
    clock.wait_until_blocked()
    raced.say(x, "还没人叫醒谁的话", wake=False)  # 入了队，但旧请求还睡着

    def new_request():
        # 新请求占了位（旧请求这时被叫醒、发现自己被顶掉了）以后第一次问时间，先等旧请求走完
        hooked.arm_this_thread(old.join, skip=0)
        return raced.inbox.fetch(x, wait=30, max_items=10)

    new = run_in_thread(new_request)
    assert texts(new.result()) == ["还没人叫醒谁的话"]
    assert old.result() == []
