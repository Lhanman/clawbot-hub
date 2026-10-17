"""定时任务：一个线程，每 30 秒一轮，一轮里是一组互不牵连的小检查。

每一轮按这个顺序做（每一步各自出错只记日志、不影响别的步，也不让线程死掉；日志里只写错误的
类型名，不写错误信息）：

1. **心跳**：更新 `run.json` 里的最后心跳（`hubctl` 靠它判断服务在不在运行，下次启动靠它写
   "上次运行到"）；
2. **清理**（每分钟一次）：待取队列的清理（`Inbox.cleanup`）、过期窗口的对话凭证清掉
   （`retention.clear_window_tokens`）；
3. **留存**（每天一次，按显示时区的日期；服务一启动的第一轮也做）：删过了保留期的记录
   （`retention.purge_old`），做失败的下一轮再来；
4. **三个告警检查**：窗口快到期、推送没发出去的汇总、腾讯不通。规则在 `alerts.py`，这里只管每一轮
   都查；
5. **每日备份**：显示时区到了 `backup.at` 就做一份自动备份（名字 `auto-<时间>`），只留最近
   `backup.keep` 份；失败了 10 分钟后重试一次，仍然失败才告警（一天一条），当天不再试。

**每日备份的约定**（规格没写细的地方）：

- "今天有没有做过"看备份目录里有没有 `auto-<今天的日期>-` 开头的目录，所以服务在 `backup.at`
  之后才启动，会马上补做今天的那一份；手动备份和升级前的备份不算；
- 同一天里是否已经失败过一次只记在内存里：重启以后当天会再试一次；
- 备份里出了意外的错误（不是 `BackupError`）也算失败，告警和事件里只写错误的类型名。

线程的节奏：一启动就跑一轮，之后每 30 秒一轮（等待走 `ClockEvent`，所以假时钟 `advance` 就能叫醒，
停服务时 `stop()` 马上叫醒）。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from clawbot_hub import backup, retention
from clawbot_hub.alerts import Alerts
from clawbot_hub.backup import BackupError, backup_name, prune_auto_backups, timestamp
from clawbot_hub.clock import Clock, ClockEvent
from clawbot_hub.config import Config
from clawbot_hub.db import Database
from clawbot_hub.events import add_event
from clawbot_hub.instance import RunRecorder
from clawbot_hub.paths import HubPaths
from clawbot_hub.quota import MINUTE_MS

logger = logging.getLogger(__name__)

# 定时任务的周期，也是心跳的周期（`run.json` 超过 60 秒没更新就当服务没在运行）
INTERVAL_SECONDS = 30.0

# 每日备份失败以后隔多久重试一次
BACKUP_RETRY_MS = 10 * MINUTE_MS


class _Cleanable(Protocol):
    def cleanup(self) -> object: ...


class Scheduler:
    def __init__(
        self,
        db: Database,
        config: Config,
        clock: Clock,
        paths: HubPaths,
        alerts: Alerts,
        inbox: _Cleanable,
        recorder: RunRecorder,
        *,
        make_backup: Callable[..., object] = backup.make_backup,
    ) -> None:
        self._db = db
        self._config = config
        self._clock = clock
        self._paths = paths
        self._alerts = alerts
        self._inbox = inbox
        self._recorder = recorder
        self._make_backup = make_backup
        self._tz = config.display.tzinfo
        hours, minutes = config.backup.at.split(":")
        self._backup_at = (int(hours), int(minutes))
        self._minute: dict[str, int] = {}  # 每分钟一次的步骤，上一次是哪一分钟
        self._retention_day: str | None = None  # 每日留存上一次做在哪一天
        self._backup_day: str | None = None  # 下面两项说的是哪一天
        self._backup_failed_ms: int | None = None  # 当天第一次失败的时刻
        self._backup_settled = False  # 当天的备份已经做成了，或者重试也失败了，不再试
        self._stop = ClockEvent()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------ 一轮

    def tick(self) -> None:
        """在调用者的线程里跑一轮。不会抛出异常。"""
        # 方法在这里现取（不在构造时取好），所以换掉 `recorder.beat` 之类的替身马上生效
        self._guard("heartbeat", self._recorder.beat)
        self._guard("cleanup", self._cleanup)
        self._guard("tokens", self._tokens)
        self._guard("retention", self._retention)
        self._guard("expiring", self._alerts.check_window_expiring)
        self._guard("blocked", self._alerts.check_window_blocked)
        self._guard("upstream", self._alerts.check_upstream)
        self._guard("backup", self._backup)

    @staticmethod
    def _guard(step: str, action: Callable[[], object]) -> None:
        try:
            action()
        except Exception as exc:  # noqa: BLE001 - 一步出错不能连累别的步，更不能让线程死掉
            logger.error("定时任务的“%s”这一步出错：%s", step, type(exc).__name__)

    def _minute_due(self, step: str) -> bool:
        """每分钟只做一次：这一分钟（按绝对时间分桶）这一步做过了就是否。"""
        bucket = self._clock.now_ms() // MINUTE_MS
        if self._minute.get(step) == bucket:
            return False
        self._minute[step] = bucket
        return True

    def _cleanup(self) -> None:
        if self._minute_due("cleanup"):
            self._inbox.cleanup()

    def _tokens(self) -> None:
        if self._minute_due("tokens"):
            retention.clear_window_tokens(self._db, self._config, self._clock)

    def _today(self, fmt: str = "%Y-%m-%d") -> str:
        return datetime.fromtimestamp(self._clock.now_ms() // 1000, self._tz).strftime(fmt)

    def _retention(self) -> None:
        day = self._today()
        if day == self._retention_day:
            return
        retention.purge_old(self._db, self._config, self._clock)
        self._retention_day = day  # 做成了才记，做失败的下一轮再来

    # ------------------------------------------------ 每日备份

    def _backup(self) -> None:
        now = self._clock.now_ms()
        local = datetime.fromtimestamp(now // 1000, self._tz)
        today = local.strftime("%Y%m%d")
        if today != self._backup_day:  # 新的一天，重新开始
            self._backup_day = today
            self._backup_failed_ms = None
            self._backup_settled = False
        if self._backup_settled or (local.hour, local.minute) < self._backup_at:
            return
        if self._backup_failed_ms is not None and now - self._backup_failed_ms < BACKUP_RETRY_MS:
            return
        if self._has_backup_today(today):
            self._backup_settled = True
            return
        name = backup_name("auto", timestamp(now, self._tz))
        try:
            self._make_backup(
                self._paths,
                self._paths.backups,
                name,
                source=self._db.conn(),
                busy_timeout_ms=self._config.db.busy_timeout_ms,
            )
        except BackupError as exc:
            self._backup_failed(now, str(exc))
        except Exception as exc:  # noqa: BLE001 - 备份出了意外的错误也算失败，信息里只写类型名
            logger.error("每日备份出了意外的错误：%s", type(exc).__name__)
            self._backup_failed(now, f"意外的错误（{type(exc).__name__}）")
        else:
            self._backup_settled = True
            self._backup_done(now, name)

    def _has_backup_today(self, today: str) -> bool:
        prefix = f"auto-{today}-"
        try:
            return any(
                entry.name.startswith(prefix) and entry.is_dir()
                for entry in self._paths.backups.iterdir()
            )
        except OSError:  # 备份目录还没有，或者根本不是目录
            return False

    def _backup_done(self, now: int, name: str) -> None:
        with self._db.txn() as conn:
            add_event(conn, now, "backup", f"每日备份完成：{name}")
        keep = self._config.backup.keep
        try:
            removed = prune_auto_backups(self._paths.backups, keep)
        except Exception as exc:  # noqa: BLE001 - 清理旧备份失败不影响这份备份已经做成
            logger.error("清理旧的自动备份出错：%s", type(exc).__name__)
            return
        if removed:
            with self._db.txn() as conn:
                detail = f"清理了 {len(removed)} 份旧的自动备份（最多留 {keep} 份）"
                add_event(conn, self._clock.now_ms(), "backup", detail)

    def _backup_failed(self, now: int, reason: str) -> None:
        retried = self._backup_failed_ms is not None
        with self._db.txn() as conn:
            if retried:
                detail = f"每日备份重试仍然失败，今天不再试：{reason}"
                add_event(conn, now, "backup", detail, level="ERROR")
            else:
                detail = f"每日备份失败，10 分钟后重试一次：{reason}"
                add_event(conn, now, "backup", detail, level="WARNING")
        if retried:
            self._backup_settled = True
            self._alerts.backup_failed(reason)
        else:
            self._backup_failed_ms = now

    # ------------------------------------------------ 线程

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("定时任务线程已经启动过了")
        self._thread = threading.Thread(target=self._run, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """让线程结束并等它（真实时间最多 `timeout` 秒）。没启动过、重复停都没关系。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            started = self._clock.now_ms()
            self.tick()
            spent = (self._clock.now_ms() - started) / 1000  # 一轮花了多久，从周期里扣掉
            self._stop.wait(self._clock, max(INTERVAL_SECONDS - spent, 0.0))
