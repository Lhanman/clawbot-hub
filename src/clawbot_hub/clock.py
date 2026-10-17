"""时钟：取当前时间、睡觉、带超时地等一个条件，三件事都走这里。

所有要看时间的组件（额度窗口、租约、重试间隔、告警去重……）都从构造函数收一个 `Clock`，
不直接调 `time.time()` / `time.sleep()` / `Condition.wait(timeout)`。这样测试里换成假时钟
（`tests/support/fake_clock.py`），时间只在 `advance()` 时往前走，
既不真等，也不会因为机器慢而偶尔失败。
"""

from __future__ import annotations

import threading
import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """时钟接口。时间一律是毫秒级的 Unix 时间戳（整数），时长一律是秒（可以带小数）。"""

    def now_ms(self) -> int:
        """现在的 Unix 时间，毫秒。"""
        ...

    def sleep(self, seconds: float) -> None:
        """睡 `seconds` 秒；负数和 `time.sleep` 一样抛 `ValueError`。"""
        ...

    def wait(self, condition: threading.Condition, timeout: float | None = None) -> bool:
        """在 `condition` 上等，用法和 `Condition.wait` 一样：调用者先拿着它的锁，
        等的时候锁放开，返回时锁又在调用者手里。被 `notify` 叫醒返回真，到了 `timeout` 返回假；
        `timeout` 为 `None` 就一直等到被叫醒。

        和 `Condition.wait` 一样可能多醒一次，所以调用者要在循环里重新检查自己的条件。"""
        ...


class RealClock:
    """真时钟：墙上时间、真睡、真等。"""

    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def wait(self, condition: threading.Condition, timeout: float | None = None) -> bool:
        return condition.wait(timeout)


class ClockEvent:
    """一个事件：`set()` 置位并叫醒所有等待的线程；`wait(clock, timeout)` 等到置位或到期。

    等待走 `Clock.wait`，所以假时钟下 `advance()` 能让到期的等待醒来（`threading.Event` 的
    `wait(timeout)` 用真实时间，假时钟管不到）。和 `threading.Event` 一样，置位以后一直有效，
    直到 `clear()`，所以"先置位、后来才等"不会丢；`wait` 不清除标志。"""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._flag = False

    def set(self) -> None:
        with self._condition:
            self._flag = True
            self._condition.notify_all()

    def clear(self) -> None:
        with self._condition:
            self._flag = False

    def is_set(self) -> bool:
        with self._condition:
            return self._flag

    def wait(self, clock: Clock, timeout: float | None = None) -> bool:
        """等到置位返回真，到了 `timeout` 秒（按 `clock` 算）还没置位返回假；`None` 一直等。
        零和负数的 `timeout` 只看一眼。"""
        deadline_ms = None if timeout is None else clock.now_ms() + round(timeout * 1000)
        with self._condition:
            while not self._flag:
                remaining = None
                if deadline_ms is not None:
                    left_ms = deadline_ms - clock.now_ms()
                    if left_ms <= 0:
                        return False
                    remaining = left_ms / 1000
                clock.wait(self._condition, remaining)  # 可能多醒一次，回到循环重新看
            return True
