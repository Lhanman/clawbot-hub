"""内存里的滑动窗口计数：认证失败的限流和项目的请求限速共用。规格：`hub-projects`「项目密钥」
「请求限速和大小」。限流和限速都是重启清零的，所以这里只看计数本身：满了几秒后再来、边界、
互不影响、不往里面记被拒绝的、不无限涨。接口层怎么用它见 `test_auth_limits.py`。
"""

from __future__ import annotations

import threading

import pytest

from clawbot_hub.limits import SlidingCount
from tests.support.fake_clock import FakeClock


class ManualClock:
    """能往回拨的时钟（系统时间被调慢的情形）；只有计数要用到的 `now_ms`。"""

    def __init__(self, now_ms: int = 1_000_000) -> None:
        self.now = now_ms

    def now_ms(self) -> int:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def count(clock) -> SlidingCount:
    return SlidingCount(clock, limit=3)


def test_hits_are_let_through_until_the_limit(count):
    assert [count.hit("a") for _ in range(3)] == [None, None, None]
    assert count.hit("a") is not None


def test_a_hit_that_is_turned_away_is_not_counted(count, clock):
    for _ in range(3):
        count.hit("a")
    clock.advance(10)

    first = count.hit("a")
    clock.advance(10)
    second = count.hit("a")

    assert (first, second) == (50, 40)  # 恢复时间只看当初那三次，被拒绝的不往后推


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, 60), (0.001, 60), (1, 59), (30, 30), (30.001, 30), (59, 1), (59.5, 1), (59.999, 1)],
)
def test_the_wait_is_the_seconds_left_rounded_up(count, clock, seconds, expected):
    for _ in range(3):
        count.hit("a")
    clock.advance(seconds)

    assert count.hit("a") == expected


def test_an_entry_is_forgotten_after_exactly_a_minute(count, clock):
    for _ in range(3):
        count.hit("a")
    clock.advance(59.999)
    assert count.hit("a") == 1

    clock.advance(0.001)

    assert count.hit("a") is None


def test_the_window_slides(count, clock):
    count.hit("a")
    clock.advance(30)
    count.hit("a")
    count.hit("a")
    clock.advance(29)
    assert count.hit("a") == 1  # 第一次再过 1 秒才满 1 分钟

    clock.advance(1)

    assert count.hit("a") is None  # 只剩后两次，又能记一次
    assert count.hit("a") == 30


def test_each_key_has_its_own_count(count):
    for _ in range(3):
        count.hit("a")

    assert count.hit("b") is None
    assert count.hit("a") is not None


def test_looking_does_not_count(count):
    for _ in range(10):
        assert count.retry_after("a") is None

    assert [count.hit("a") for _ in range(3)] == [None, None, None]


def test_looking_says_how_long_to_wait_without_counting(count, clock):
    for _ in range(3):
        count.hit("a")
    clock.advance(20)

    assert count.retry_after("a") == 40
    assert count.retry_after("a") == 40
    assert count.retry_after("b") is None


def test_adding_counts_without_asking(count, clock):
    for _ in range(3):
        count.add("a")

    assert count.retry_after("a") == 60
    clock.advance(60)
    assert count.retry_after("a") is None


def test_the_limit_and_the_window_are_configurable(clock):
    short = SlidingCount(clock, limit=1, window_ms=5_000)
    short.hit("a")

    assert short.hit("a") == 5
    clock.advance(5)
    assert short.hit("a") is None


def test_a_limit_of_zero_is_a_programming_error(clock):
    with pytest.raises(ValueError, match="至少"):
        SlidingCount(clock, limit=0)


def test_a_clock_set_back_does_not_lock_a_key_for_hours():
    clock = ManualClock()
    count = SlidingCount(clock, limit=2)
    count.hit("a")
    count.hit("a")

    clock.now -= 3_600_000  # 系统时间被调慢了一小时

    wait = count.retry_after("a")
    assert wait is not None and wait <= 60  # 记录当作刚刚发生的，最多再等 1 分钟
    clock.now += 60_000
    assert count.retry_after("a") is None


def test_keys_that_were_never_seen_again_do_not_pile_up(clock):
    count = SlidingCount(clock, limit=3)
    for number in range(5000):
        count.add(f"10.0.{number // 250}.{number % 250}")
    assert count.tracked() == 5000

    clock.advance(61)
    count.hit("fresh")  # 记新的东西时顺手清掉早就过期的

    assert count.tracked() == 1


def test_a_key_with_nothing_left_is_dropped(count, clock):
    count.hit("a")
    clock.advance(61)

    assert count.retry_after("a") is None
    assert count.tracked() == 0


def test_many_threads_cannot_get_past_the_limit(clock):
    count = SlidingCount(clock, limit=10)
    start = threading.Barrier(40)
    results = []

    def go():
        start.wait()
        results.append(count.hit("a"))

    threads = [threading.Thread(target=go) for _ in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert results.count(None) == 10
    assert len(results) == 40
