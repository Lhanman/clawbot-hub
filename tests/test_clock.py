"""时钟（任务 0.3）：`Clock` 接口、真时钟、假时钟。

假时钟让其他组件的测试不用真等：时间只在 `advance()` 时往前走，
到期的 `sleep` 和 `wait` 在那时醒来；`notify` 能让 `wait` 提前返回真。
"""

from __future__ import annotations

import inspect
import threading
import time

import pytest

from clawbot_hub.clock import Clock, RealClock
from tests.support.fake_clock import DEFAULT_START_MS, FakeClock
from tests.support.threads import run_in_thread


def _wait_call(clock: FakeClock, cond: threading.Condition, timeout: float | None):
    """在当前线程里按调用约定用 `clock.wait`：先拿锁。"""
    with cond:
        return clock.wait(cond, timeout)


# ---------- 假时钟：时间 ----------


def test_now_ms_starts_at_the_given_value_and_does_not_move_by_itself():
    clock = FakeClock(start_ms=1_000)
    first = clock.now_ms()
    time.sleep(0.01)  # 真实时间过去了，假时间不动
    assert first == 1_000
    assert clock.now_ms() == 1_000


def test_default_start_is_a_fixed_recent_instant():
    assert FakeClock().now_ms() == DEFAULT_START_MS
    assert DEFAULT_START_MS > 1_700_000_000_000  # 2023 年以后，毫秒


def test_advance_moves_time_in_seconds():
    clock = FakeClock(start_ms=0)
    clock.advance(1.5)
    assert clock.now_ms() == 1_500
    clock.advance(0.001)
    assert clock.now_ms() == 1_501
    clock.advance(0)
    assert clock.now_ms() == 1_501


def test_advance_rejects_going_back():
    clock = FakeClock(start_ms=0)
    with pytest.raises(ValueError):
        clock.advance(-1)


# ---------- 假时钟：sleep ----------


def test_sleep_blocks_until_advance_reaches_the_deadline_without_really_waiting():
    clock = FakeClock(start_ms=0)
    started = time.monotonic()
    worker = run_in_thread(clock.sleep, 5)
    clock.wait_until_blocked()
    clock.advance(4.9)
    assert worker.still_blocked()  # 差 0.1 秒到期
    clock.advance(0.1)
    worker.result()
    assert time.monotonic() - started < 2  # 5 秒是假时间，真实几乎没花时间
    assert clock.now_ms() == 5_000


def test_sleepers_wake_in_deadline_order_as_time_advances():
    clock = FakeClock(start_ms=0)
    short = run_in_thread(clock.sleep, 1)
    long = run_in_thread(clock.sleep, 3)
    clock.wait_until_blocked(2)
    clock.advance(1)
    short.result()
    assert long.still_blocked()
    clock.advance(2)
    long.result()


def test_sleep_zero_returns_at_once_and_negative_is_an_error_like_time_sleep():
    clock = FakeClock(start_ms=0)
    clock.sleep(0)
    assert clock.now_ms() == 0
    with pytest.raises(ValueError):
        clock.sleep(-1)


def test_auto_sleep_moves_time_instead_of_blocking():
    clock = FakeClock(start_ms=0, auto_sleep=True)
    clock.sleep(1)
    clock.sleep(2)
    assert clock.now_ms() == 3_000
    assert clock.blocked_count == 0


def test_auto_sleep_also_wakes_waiters_whose_deadline_passed():
    clock = FakeClock(start_ms=0, auto_sleep=True)
    cond = threading.Condition()
    waiter = run_in_thread(_wait_call, clock, cond, 2)
    clock.wait_until_blocked()
    clock.sleep(3)  # 别的线程在"睡"，时间随之走过了 2 秒
    assert waiter.result() is False


# ---------- 假时钟：wait ----------


def test_wait_returns_false_only_once_advance_passes_the_deadline():
    clock = FakeClock(start_ms=0)
    cond = threading.Condition()
    waiter = run_in_thread(_wait_call, clock, cond, 10)
    clock.wait_until_blocked()
    clock.advance(9.9)
    assert waiter.still_blocked()
    clock.advance(0.1)
    assert waiter.result() is False


def test_wait_returns_true_when_notified_before_the_deadline_and_time_stays_put():
    clock = FakeClock(start_ms=0)
    cond = threading.Condition()
    waiter = run_in_thread(_wait_call, clock, cond, 10)
    clock.wait_until_blocked()
    with cond:
        cond.notify_all()
    assert waiter.result() is True
    assert clock.now_ms() == 0
    assert clock.blocked_count == 0


def test_wait_without_timeout_ignores_advance_and_waits_for_notify():
    clock = FakeClock(start_ms=0)
    cond = threading.Condition()
    waiter = run_in_thread(_wait_call, clock, cond, None)
    clock.wait_until_blocked()
    clock.advance(10_000)
    assert waiter.still_blocked()
    with cond:
        cond.notify()
    assert waiter.result() is True


def test_wait_with_zero_or_negative_timeout_returns_false_at_once():
    clock = FakeClock(start_ms=0)
    cond = threading.Condition()
    assert _wait_call(clock, cond, 0) is False
    assert _wait_call(clock, cond, -3) is False
    assert clock.now_ms() == 0
    assert clock.blocked_count == 0


def test_wait_deadline_counts_from_the_fake_now_at_the_time_of_the_call():
    clock = FakeClock(start_ms=0)
    clock.advance(3)
    cond = threading.Condition()
    waiter = run_in_thread(_wait_call, clock, cond, 5)  # 到期点是 8 秒
    clock.wait_until_blocked()
    clock.advance(4)
    assert waiter.still_blocked()
    clock.advance(1)
    assert waiter.result() is False


def test_wait_wakes_only_the_waiters_whose_deadline_passed():
    clock = FakeClock(start_ms=0)
    cond_a, cond_b = threading.Condition(), threading.Condition()
    a = run_in_thread(_wait_call, clock, cond_a, 5)
    b = run_in_thread(_wait_call, clock, cond_b, 10)
    clock.wait_until_blocked(2)
    clock.advance(5)
    assert a.result() is False
    assert b.still_blocked()
    clock.advance(5)
    assert b.result() is False


def test_waiters_sharing_one_condition_each_get_their_own_deadline():
    clock = FakeClock(start_ms=0)
    cond = threading.Condition()
    finished: list[int] = []

    def wait_loop(timeout: float, tag: int) -> None:
        # 和真实的调用方一样：被叫醒后重新检查"到期了没有"，没到期就接着等
        # （同一个 Condition 上的别人到期时，这里也会被多叫醒一次，所以必须这样写）
        deadline = clock.now_ms() + int(timeout * 1000)
        with cond:
            while clock.now_ms() < deadline:
                clock.wait(cond, (deadline - clock.now_ms()) / 1000)
        finished.append(tag)

    first = run_in_thread(wait_loop, 5, 1)
    second = run_in_thread(wait_loop, 10, 2)
    clock.wait_until_blocked(2)
    clock.advance(5)
    first.result()
    assert second.still_blocked()
    assert finished == [1]
    clock.advance(5)
    second.result()


def test_wait_requires_the_caller_to_hold_the_lock_and_leaves_nothing_registered():
    clock = FakeClock(start_ms=0)
    cond = threading.Condition()
    with pytest.raises(RuntimeError):
        clock.wait(cond, 5)  # 没拿锁：和 Condition.wait 一样报错
    assert clock.blocked_count == 0
    clock.advance(10)  # 没有残留的登记，不会去叫醒谁


def test_wait_hands_the_lock_back_to_the_caller_when_it_returns():
    clock = FakeClock(start_ms=0)
    cond = threading.Condition()
    seen: list[bool] = []

    def waiter() -> None:
        with cond:
            clock.wait(cond, 1)
            seen.append(cond._is_owned())  # 返回时锁又在调用者手里

    worker = run_in_thread(waiter)
    clock.wait_until_blocked()
    clock.advance(1)
    worker.result()
    assert seen == [True]


# ---------- 假时钟：辅助 ----------


def test_blocked_count_counts_sleepers_and_waiters_and_drops_when_they_return():
    clock = FakeClock(start_ms=0)
    cond = threading.Condition()
    a = run_in_thread(clock.sleep, 1)
    b = run_in_thread(_wait_call, clock, cond, 2)
    clock.wait_until_blocked(2)
    assert clock.blocked_count == 2
    clock.advance(2)
    a.result()
    b.result()
    assert clock.blocked_count == 0


def test_pending_deadlines_lists_what_each_sleeper_and_waiter_is_waiting_for():
    clock = FakeClock(start_ms=1_000)
    cond_a, cond_b = threading.Condition(), threading.Condition()
    sleeper = run_in_thread(clock.sleep, 3)  # 4_000
    timed = run_in_thread(_wait_call, clock, cond_a, 2)  # 3_000
    endless = run_in_thread(_wait_call, clock, cond_b, None)  # 不限时的不列
    clock.wait_until_blocked(3)

    assert clock.pending_deadlines() == [3_000, 4_000]

    clock.advance(2)
    timed.result()
    assert clock.pending_deadlines() == [4_000]

    clock.advance(1)
    sleeper.result()
    assert clock.pending_deadlines() == []
    with cond_b:
        cond_b.notify_all()
    assert endless.result() is True


def test_a_deadline_that_has_just_passed_is_no_longer_pending():
    clock = FakeClock(start_ms=0)
    worker = run_in_thread(clock.sleep, 5)
    clock.wait_until_blocked()
    assert clock.pending_deadlines() == [5_000]

    clock.advance(5)

    assert clock.pending_deadlines() == []  # 线程可能还没跑起来，期限已经算过了
    worker.result()


def test_wait_until_blocked_gives_up_in_real_time_instead_of_hanging():
    clock = FakeClock(start_ms=0)
    started = time.monotonic()
    with pytest.raises(AssertionError, match="没有线程"):
        clock.wait_until_blocked(1, timeout=0.1)
    assert time.monotonic() - started < 2


def test_many_threads_with_different_deadlines_all_come_back():
    clock = FakeClock(start_ms=0)
    workers = [run_in_thread(clock.sleep, n) for n in range(1, 21)]
    clock.wait_until_blocked(20)
    for _ in range(20):
        clock.advance(1)
    for worker in workers:
        worker.result()
    assert clock.now_ms() == 20_000


# ---------- 真时钟 ----------


def test_real_clock_now_ms_follows_wall_time():
    clock = RealClock()
    assert abs(clock.now_ms() - int(time.time() * 1000)) < 1_000
    assert isinstance(clock.now_ms(), int)


def test_real_clock_sleep_really_sleeps():
    clock = RealClock()
    started = time.monotonic()
    clock.sleep(0.03)
    assert time.monotonic() - started >= 0.025


def test_real_clock_wait_times_out_with_false_and_notify_gives_true():
    clock = RealClock()
    cond = threading.Condition()
    with cond:
        assert clock.wait(cond, 0.02) is False

    def notify_soon() -> None:
        time.sleep(0.02)
        with cond:
            cond.notify_all()

    notifier = run_in_thread(notify_soon)
    with cond:
        assert clock.wait(cond, 5) is True
    notifier.result()


# ---------- 接口形状 ----------


@pytest.mark.parametrize("name", ["now_ms", "sleep", "wait"])
def test_real_and_fake_clock_have_the_same_method_signatures(name):
    real = inspect.signature(getattr(RealClock, name))
    fake = inspect.signature(getattr(FakeClock, name))
    assert list(real.parameters) == list(fake.parameters)
    for key in real.parameters:
        assert real.parameters[key].kind == fake.parameters[key].kind
        assert real.parameters[key].default == fake.parameters[key].default


def test_both_clocks_satisfy_the_clock_protocol():
    assert isinstance(RealClock(), Clock)
    assert isinstance(FakeClock(), Clock)
