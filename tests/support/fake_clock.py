"""假时钟：时间只在 `advance()` 时往前走，到期的 `sleep` 和 `wait` 在那时醒来，全程不真等。

两种用法：

- 默认（阻塞式）：`sleep(5)` 会一直睡到别的线程 `advance()` 过了 5 秒。适合后台线程
  （收消息循环、清理任务）：它们睡着，由测试决定什么时候让时间走。
- `auto_sleep=True`：`sleep(5)` 立刻返回，同时把假时间推进 5 秒。适合"一个线程从头跑到尾"的
  测试（发送时的重试间隔 1 秒、2 秒），免得测试得另起线程去推时间。

`wait(condition, timeout)` 总是阻塞式的：被 `notify` 叫醒返回真，假时间到了期限返回假。
和真正的 `Condition.wait` 一样，同一个 `Condition` 上有别人到期时，没到期的也可能被多叫醒一次，
所以调用者要在循环里重新检查条件。

锁的顺序：调用者的 `Condition` 锁 → 假时钟自己的锁；`advance()` 不会在拿着自己的锁时去拿别人的锁，
所以不会死锁。`advance()` 不要在拿着某个 `Condition`（用的是不可重入的普通锁）的情况下调用。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

# 2026-10-12 10:00:00 北京时间（周一，A 股交易日）
DEFAULT_START_MS = 1_791_770_400_000


@dataclass
class _Waiter:
    condition: threading.Condition
    deadline_ms: int | None  # None = 不限时
    expired: bool = False


def _to_ms(seconds: float) -> int:
    return round(seconds * 1000)


class FakeClock:
    def __init__(self, start_ms: int = DEFAULT_START_MS, *, auto_sleep: bool = False) -> None:
        self._now_ms = start_ms
        self._auto_sleep = auto_sleep
        # `_tick` 同时是：假时钟自己的锁、睡着的线程等待的条件、"有线程睡下了"的通知
        self._tick = threading.Condition()
        self._waiters: list[_Waiter] = []
        self._blocked = 0

    # ---------- Clock 接口 ----------

    def now_ms(self) -> int:
        with self._tick:
            return self._now_ms

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("sleep length must be non-negative")
        ms = _to_ms(seconds)
        if ms == 0:
            return
        if self._auto_sleep:
            self._move(ms)
            return
        with self._tick:
            deadline = self._now_ms + ms
            self._blocked += 1
            self._tick.notify_all()  # 让 wait_until_blocked 知道又多了一个
            try:
                while self._now_ms < deadline:
                    self._tick.wait()
            finally:
                self._blocked -= 1

    def wait(self, condition: threading.Condition, timeout: float | None = None) -> bool:
        waiter: _Waiter | None = None
        with self._tick:
            deadline = None if timeout is None else self._now_ms + max(0, _to_ms(timeout))
            if deadline is not None and deadline <= self._now_ms:
                return False  # 零和负数的期限：和 Condition.wait(0) 一样，不等
            waiter = _Waiter(condition, deadline)
            self._waiters.append(waiter)
            self._blocked += 1
            self._tick.notify_all()
        try:
            condition.wait()  # 真等，直到被 notify 或被 advance() 叫醒；没拿锁在这里抛 RuntimeError
        finally:
            with self._tick:
                self._waiters.remove(waiter)
                self._blocked -= 1
        return not waiter.expired

    # ---------- 测试用 ----------

    def advance(self, seconds: float) -> None:
        """让时间往前走 `seconds` 秒，到期的 `sleep` 和 `wait` 醒来。"""
        if seconds < 0:
            raise ValueError("时间只能往前走")
        self._move(_to_ms(seconds))

    @property
    def blocked_count(self) -> int:
        """现在有多少个线程睡在 `sleep` / `wait` 里。"""
        with self._tick:
            return self._blocked

    def wait_until_blocked(self, count: int = 1, timeout: float = 5.0) -> None:
        """等到至少有 `count` 个线程睡在 `sleep` / `wait` 里（`timeout` 是真实秒数）。

        测试要先等线程睡下，再 `advance()`：否则时间先走了，线程后来才登记，期限就从新时间算起。"""
        with self._tick:
            if not self._tick.wait_for(lambda: self._blocked >= count, timeout):
                raise AssertionError(
                    f"{timeout} 秒（真实时间）内没有线程睡下："
                    f"要 {count} 个，现在 {self._blocked} 个"
                )

    # ---------- 内部 ----------

    def _move(self, ms: int) -> None:
        with self._tick:
            self._now_ms += ms
            expired = [
                w
                for w in self._waiters
                if not w.expired and w.deadline_ms is not None and w.deadline_ms <= self._now_ms
            ]
            for waiter in expired:
                waiter.expired = True
            self._tick.notify_all()  # 睡在 sleep 里的线程自己看时间
        # 自己的锁放开以后再去叫醒别人的 Condition，避免和等待者的加锁顺序打架
        conditions = {id(w.condition): w.condition for w in expired}
        for condition in conditions.values():
            with condition:
                condition.notify_all()
