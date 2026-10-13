"""凭证监视线程：每 5 秒比对一次 `account.json` 和数据库里记的凭证摘要。

比对本身（换上新凭证、补做重新绑定的数据库事务、记摘要、清掉失效状态、没绑定时看文件有没有出现）
都在 `LoginMonitor.sync`；这里只管节奏：一启动就比对一次，之后每 5 秒一次，停服务时马上结束。
某一次比对出错（比如数据库暂时被锁）只记日志，下一轮接着来。

规格：`hub-login`「登录后服务自动换上新凭证」「还没绑定也能启动」。
"""

from __future__ import annotations

import logging
import threading

from clawbot_hub.clock import Clock, ClockEvent
from clawbot_hub.loginstate import LoginMonitor, SyncResult

logger = logging.getLogger(__name__)

WATCH_INTERVAL_SECONDS = 5.0


class CredentialWatcher:
    def __init__(
        self,
        monitor: LoginMonitor,
        clock: Clock,
        *,
        interval_seconds: float = WATCH_INTERVAL_SECONDS,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError(f"比对间隔要大于 0 秒：{interval_seconds}")
        self._monitor = monitor
        self._clock = clock
        self._interval = interval_seconds
        self._stop = ClockEvent()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def tick(self) -> SyncResult:
        """在调用者的线程里比对一次。"""
        return self._monitor.sync()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("凭证监视线程已经启动过了")
        self._thread = threading.Thread(target=self._run, name="credential-watcher", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """让线程结束并等它（真实时间最多 `timeout` 秒）。没启动过、重复停都没关系。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._monitor.sync()
            except Exception as exc:  # noqa: BLE001 - 一次出错不能让监视停下
                logger.error("凭证监视出错：%s", type(exc).__name__)
            self._stop.wait(self._clock, self._interval)  # 被 stop() 叫醒就回到循环条件，退出
