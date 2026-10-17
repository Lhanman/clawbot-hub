"""正在处理的事情的个数（`services.InFlight`，任务 D6）。

守门人放进来的每个请求、服务自己的回复，处理期间都记在这里；服务关闭时靠它知道“还有没有事情没做完”，
等到个数归零或者 `server.shutdown_seconds` 用完。等的是注入的时钟（测试里是假时钟），不是真实时间。
"""

from __future__ import annotations

from clawbot_hub.services import InFlight
from tests.support.fake_clock import FakeClock
from tests.support.threads import run_in_thread, wait_for


def test_the_count_follows_enter_and_leave():
    in_flight = InFlight()
    assert in_flight.count == 0

    in_flight.enter()
    in_flight.enter()
    assert in_flight.count == 2

    in_flight.leave()
    assert in_flight.count == 1
    in_flight.leave()
    assert in_flight.count == 0


def test_waiting_with_nothing_in_flight_returns_true_at_once():
    clock = FakeClock()

    assert InFlight().wait_idle(clock, clock.now_ms() + 5_000) is True


def test_waiting_ends_when_the_last_one_leaves():
    clock = FakeClock()
    in_flight = InFlight()
    in_flight.enter()
    in_flight.enter()
    waiting = run_in_thread(in_flight.wait_idle, clock, clock.now_ms() + 60_000)
    wait_for(lambda: clock.blocked_count == 1)

    in_flight.leave()
    assert waiting.still_blocked()  # 还剩一件

    in_flight.leave()
    assert waiting.result() is True


def test_waiting_gives_up_when_the_clock_reaches_the_deadline():
    clock = FakeClock()
    in_flight = InFlight()
    in_flight.enter()
    waiting = run_in_thread(in_flight.wait_idle, clock, clock.now_ms() + 5_000)
    wait_for(lambda: clock.blocked_count == 1)

    clock.advance(4.999)
    assert waiting.still_blocked()
    clock.advance(0.001)

    assert waiting.result() is False
    assert in_flight.count == 1  # 没做完的还记着


def test_a_deadline_that_has_already_been_reached_is_not_waited_for():
    clock = FakeClock()
    in_flight = InFlight()
    in_flight.enter()

    now = run_in_thread(in_flight.wait_idle, clock, clock.now_ms())
    past = run_in_thread(in_flight.wait_idle, clock, clock.now_ms() - 1)

    assert now.result() is False  # 正好到点：不睡
    assert past.result() is False


def test_a_deadline_one_millisecond_away_is_still_waited_for():
    clock = FakeClock()
    in_flight = InFlight()
    in_flight.enter()
    waiting = run_in_thread(in_flight.wait_idle, clock, clock.now_ms() + 1)
    wait_for(lambda: clock.blocked_count == 1)

    assert waiting.still_blocked()
    clock.advance(0.001)

    assert waiting.result() is False


def test_the_deadline_is_not_what_ends_the_wait_when_the_work_finishes_first():
    clock = FakeClock()
    in_flight = InFlight()
    in_flight.enter()
    waiting = run_in_thread(in_flight.wait_idle, clock, clock.now_ms() + 35_000)
    wait_for(lambda: clock.blocked_count == 1)

    in_flight.leave()  # 假时间一点也没走

    assert waiting.result() is True
