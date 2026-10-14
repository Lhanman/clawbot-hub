"""内存里的滑动窗口计数：认证失败的限流（按来源地址）和项目的请求限速（按项目）共用。

计数只在内存里，服务重启就清零（规格 `hub-deploy`「重启后的状态」：限速计数和失败计数清零，
可接受）。
一个键在窗口（默认 1 分钟）里的记录满了 `limit` 条，就要等最早的那条满一个窗口才能再记；
"还要等几秒"向上取整，所以至少是 1。被拒绝的不往里记，所以被拒绝的请求不会把恢复时间往后推。
满一个窗口的记录就忘掉（刚好满 60 秒那一刻已经忘了）。

系统时间被调慢时，比现在还晚的记录当作刚刚发生的，最多再等一个窗口，不会把一个键锁上几个小时。
没有再出现的键过一个窗口后，在下一次记数时顺手清掉，不会越攒越多。线程安全。
"""

from __future__ import annotations

import threading
from collections import deque

from clawbot_hub.clock import Clock
from clawbot_hub.config import Config

WINDOW_MS = 60_000
# 同一个局域网来源 1 分钟内认证失败这么多次，之后的请求在认证之前就被拒绝（固定值）
FAILURE_LIMIT = 10


class SlidingCount:
    def __init__(self, clock: Clock, *, limit: int, window_ms: int = WINDOW_MS) -> None:
        if limit < 1:
            raise ValueError(f"限额至少是 1：{limit!r}")
        self._clock = clock
        self._limit = limit
        self._window_ms = window_ms
        self._hits: dict[str, deque[int]] = {}
        self._lock = threading.Lock()
        self._swept_ms = clock.now_ms()

    def hit(self, key: str) -> int | None:
        """查一下、没满就记一次（一步做完，几个线程同时来也不会一起越过限额）。满了返回还要等几秒，
        这一次不记。"""
        with self._lock:
            now = self._clock.now_ms()
            self._sweep(now)
            wait = self._wait(key, now)
            if wait is None:
                self._hits.setdefault(key, deque()).append(now)
            return wait

    def retry_after(self, key: str) -> int | None:
        """满了就返回还要等几秒，没满返回 `None`。只看，不记。"""
        with self._lock:
            return self._wait(key, self._clock.now_ms())

    def add(self, key: str) -> None:
        """不管满没满，记一次。"""
        with self._lock:
            now = self._clock.now_ms()
            self._sweep(now)
            self._hits.setdefault(key, deque()).append(now)

    def tracked(self) -> int:
        """现在存着多少个键（测试和排错用）。"""
        with self._lock:
            return len(self._hits)

    # ------------------------------------------------------------ 内部（调用时已拿着锁）

    def _wait(self, key: str, now: int) -> int | None:
        hits = self._live(key, now)
        if hits is None or len(hits) < self._limit:
            return None
        left_ms = hits[0] + self._window_ms - now  # 最早那条还差多久满一个窗口，一定大于 0
        return -(-left_ms // 1000)

    def _live(self, key: str, now: int) -> deque[int] | None:
        """这个键还有效的记录（忘掉满一个窗口的）；一条都不剩就把键也删了。"""
        hits = self._hits.get(key)
        if hits is None:
            return None
        if hits[-1] > now:  # 时间被调慢过：未来的记录当作刚刚
            hits = deque(min(moment, now) for moment in hits)
            self._hits[key] = hits
        while hits and now - hits[0] >= self._window_ms:
            hits.popleft()
        if not hits:
            del self._hits[key]
            return None
        return hits

    def _sweep(self, now: int) -> None:
        """每过一个窗口，把所有早就过期的键清掉一遍。"""
        if now < self._swept_ms:
            self._swept_ms = now
        if now - self._swept_ms < self._window_ms:
            return
        for key in list(self._hits):
            self._live(key, now)
        self._swept_ms = now


class Limits:
    """接口用的两套计数：`failures` 按来源地址记认证失败（`hub-projects`「项目密钥」），
    `requests` 按项目记发消息和回复的请求（「请求限速和大小」，上限是 `send.rate_per_minute`）。"""

    def __init__(self, config: Config, clock: Clock) -> None:
        self.failures = SlidingCount(clock, limit=FAILURE_LIMIT)
        self.requests = SlidingCount(clock, limit=config.send.rate_per_minute)
