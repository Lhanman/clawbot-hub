"""定时任务（`Scheduler`）测试共用的夹具：告警那一套（`AlertRig`：数据库、假上游、登录、项目、额度、
发送、告警）再接上待取队列、`run.json` 记录员和定时任务本身。

- `SchedulerRig.tick()`：让假时间走过 `seconds` 秒（默认 0），再跑一轮定时任务；
- `SchedulerRig.backups()`：`backups/` 下现有的目录名；
- `make_scheduler()`：默认把每日备份定在 23:59，免得和备份无关的测试在假时钟的 10:00 就做一次备份；
  要测默认的 04:00 就传 `Config()`。
"""

from __future__ import annotations

from typing import Any

import pytest

from clawbot_hub.config import Config
from clawbot_hub.inbox import Cleaned, Inbox
from clawbot_hub.instance import RunRecorder
from clawbot_hub.scheduler import Scheduler
from tests.alerts.conftest import AlertRig
from tests.support.fake_clock import FakeClock
from tests.support.hub import tweak

__all__ = ["SchedulerRig", "SpyInbox"]


@pytest.fixture
def clock() -> FakeClock:
    """`sleep` 立刻返回的假时钟（和 `tests/alerts/conftest.py` 里的一样）。"""
    return FakeClock(auto_sleep=True)


class SpyInbox:
    """`Inbox` 的替身：只数清理被调了几次。"""

    def __init__(self) -> None:
        self.cleanups = 0

    def cleanup(self) -> Cleaned:
        self.cleanups += 1
        return Cleaned()


class SchedulerRig(AlertRig):
    def __init__(
        self, paths, database, clock, upstream, *, config: Config | None = None, inbox=None, **hooks
    ) -> None:
        super().__init__(paths, database, clock, upstream, config=config)
        self.inbox = (
            inbox if inbox is not None else Inbox(database, self.config, clock, self.hub.projects)
        )
        self.recorder = RunRecorder(
            paths,
            clock,
            version="0.0.0-test",
            listen="127.0.0.1:8790",
            profile="legacy",
            confirm="observe",
        )
        self.recorder.start()
        self.hooks = hooks
        self.scheduler = self._scheduler()

    def _scheduler(self) -> Scheduler:
        return Scheduler(
            self.db,
            self.config,
            self.clock,
            self.paths,
            self.alerts,
            self.inbox,
            self.recorder,
            **self.hooks,
        )

    def restart(self, config: Config | None = None):
        """服务重启：新的告警对象、新的定时任务（内存状态没了）。"""
        super().restart(config)
        self.scheduler = self._scheduler()
        return self.scheduler

    def tick(self, seconds: float = 0) -> None:
        if seconds:
            self.clock.advance(seconds)
        self.scheduler.tick()

    def backups(self) -> list[str]:
        if not self.paths.backups.is_dir():
            return []
        return sorted(entry.name for entry in self.paths.backups.iterdir())


@pytest.fixture
def make_scheduler(paths, database, clock, fake_upstream):
    def build(config: Config | None = None, **kwargs: Any) -> SchedulerRig:
        if config is None:
            config = tweak(Config(), backup={"at": "23:59"})
        return SchedulerRig(paths, database, clock, fake_upstream, config=config, **kwargs)

    return build


@pytest.fixture
def sched(make_scheduler) -> SchedulerRig:
    return make_scheduler()
