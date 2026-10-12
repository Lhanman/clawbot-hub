"""测试里在线程中跑一段代码：记下返回值或异常，等待一律带真实时间的超时，不会把测试挂住。"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

# 真实时间里最多等多久（秒）：正常情况下毫秒级，超了就是真出了问题
REAL_TIMEOUT = 5.0


class Worker:
    """`run_in_thread` 返回的句柄。"""

    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        self._value: Any = None
        self._error: BaseException | None = None
        self._done = threading.Event()

        def run() -> None:
            try:
                self._value = fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 - 原样交还给测试
                self._error = exc
            finally:
                self._done.set()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    @property
    def done(self) -> bool:
        return self._done.is_set()

    def still_blocked(self, real_seconds: float = 0.05) -> bool:
        """给它一小段真实时间，看它是不是仍然没返回（用来断言"还没醒"）。"""
        return not self._done.wait(real_seconds)

    def result(self, timeout: float = REAL_TIMEOUT) -> Any:
        """等线程结束并返回它的值；线程里抛了异常就在这里重新抛出。"""
        if not self._done.wait(timeout):
            raise AssertionError(f"线程在 {timeout} 秒（真实时间）内没有结束")
        if self._error is not None:
            raise self._error
        return self._value


def run_in_thread(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Worker:
    return Worker(fn, *args, **kwargs)
