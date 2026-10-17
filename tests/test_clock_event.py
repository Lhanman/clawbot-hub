"""`ClockEvent`：等待走 `Clock`，所以假时钟下 `advance()` 能让到期的等待醒来。

收消息线程睡在 600 秒的探测间隔里，凭证监视线程一发现新凭证就要叫醒它；
不用 `threading.Event`，因为它的 `wait(timeout)` 用的是真实时间，假时钟管不到。
"""

from __future__ import annotations

import threading
import time

from clawbot_hub.clock import ClockEvent, RealClock
from tests.support.fake_clock import FakeClock
from tests.support.threads import run_in_thread


def test_a_new_event_is_not_set():
    assert not ClockEvent().is_set()


def test_set_and_clear():
    event = ClockEvent()
    event.set()
    assert event.is_set()
    event.clear()
    assert not event.is_set()


def test_setting_twice_is_harmless():
    event = ClockEvent()
    event.set()
    event.set()
    assert event.is_set()


def test_waiting_on_an_event_that_is_already_set_returns_at_once_without_time_passing():
    clock = FakeClock()
    event = ClockEvent()
    event.set()
    started = clock.now_ms()
    assert event.wait(clock, 600) is True
    assert clock.now_ms() == started


def test_waiting_does_not_clear_the_event():
    clock = FakeClock()
    event = ClockEvent()
    event.set()
    event.wait(clock, 1)
    assert event.is_set() and event.wait(clock, 1) is True


def test_a_wait_ends_false_when_the_fake_clock_reaches_the_timeout():
    clock = FakeClock()
    event = ClockEvent()
    worker = run_in_thread(event.wait, clock, 600)
    clock.wait_until_blocked(1)
    clock.advance(599)
    assert worker.still_blocked(0.05)
    clock.advance(1)
    assert worker.result(timeout=2) is False


def test_setting_wakes_a_waiter_before_its_timeout():
    clock = FakeClock()
    event = ClockEvent()
    worker = run_in_thread(event.wait, clock, 600)
    clock.wait_until_blocked(1)
    event.set()
    assert worker.result(timeout=2) is True
    assert clock.now_ms() == FakeClock().now_ms()  # 假时间一秒都没走


def test_setting_wakes_every_waiter():
    clock = FakeClock()
    event = ClockEvent()
    workers = [run_in_thread(event.wait, clock, 600) for _ in range(3)]
    clock.wait_until_blocked(3)
    event.set()
    assert [w.result(timeout=2) for w in workers] == [True, True, True]


def test_a_set_that_comes_before_the_wait_is_not_lost():
    clock = FakeClock()
    event = ClockEvent()
    event.set()
    worker = run_in_thread(event.wait, clock, 600)
    assert worker.result(timeout=2) is True


def test_without_a_timeout_the_wait_goes_on_until_the_event_is_set():
    clock = FakeClock()
    event = ClockEvent()
    worker = run_in_thread(event.wait, clock)
    clock.wait_until_blocked(1)
    clock.advance(10_000)
    assert worker.still_blocked(0.05)
    event.set()
    assert worker.result(timeout=2) is True


def test_an_extra_wake_up_does_not_end_the_wait_early():
    clock = FakeClock()
    event = ClockEvent()
    worker = run_in_thread(event.wait, clock, 600)
    clock.wait_until_blocked(1)
    clock.advance(100)  # 假时钟的 advance 可能多叫醒一次
    assert worker.still_blocked(0.05)
    event.set()
    assert worker.result(timeout=2) is True


def test_a_zero_or_negative_timeout_only_looks():
    clock = FakeClock()
    event = ClockEvent()
    assert event.wait(clock, 0) is False
    assert event.wait(clock, -5) is False
    event.set()
    assert event.wait(clock, 0) is True


def test_the_event_can_be_used_again_after_clearing():
    clock = FakeClock()
    event = ClockEvent()
    event.set()
    assert event.wait(clock, 1) is True
    event.clear()
    worker = run_in_thread(event.wait, clock, 600)
    clock.wait_until_blocked(1)
    assert worker.still_blocked(0.05)
    event.set()
    assert worker.result(timeout=2) is True


def test_with_the_real_clock_a_short_wait_times_out_and_a_set_wakes_it():
    event = ClockEvent()
    started = time.monotonic()
    assert event.wait(RealClock(), 0.05) is False
    assert 0.04 <= time.monotonic() - started < 2
    timer = threading.Timer(0.05, event.set)
    timer.start()
    try:
        assert event.wait(RealClock(), 5) is True
    finally:
        timer.cancel()
