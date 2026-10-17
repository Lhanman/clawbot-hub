"""告警测试共用的夹具：一整套零件（`Hub`）接上告警（`Alerts`），Bark 用不联网的替身。

- `StubBark`：实现 `BarkClient.send` 的替身，记下每一次发送（设备码、标题、正文、假时钟的时刻），
  结果按剧本给（`script`），没排的就是送到了。规则测试用它，不起 HTTP 服务；真的 Bark 请求
  格式和各种故障在 `test_bark.py`、`test_delivery.py` 里对着假 Bark 服务测；
- `AlertRig`：数据库、假上游、登录监视器、项目、额度、发送（`Hub`），再接告警——登录失效 / 恢复、
  腾讯拒绝后的暂停、每个 `sendmessage` 请求的分类，都经 `Alerts` 的回调登记；
- `go_to()`：把假时钟拨到北京时间的某一刻（告警按显示时区判断几点、星期几）；
- 这个目录下的假时钟是 `sleep` 立刻返回的那种（立即重试要睡 1 秒、2 秒、4 秒，测试里不真等），
  所以 `deliver()` 会让假时间走过这些秒数。
"""

from __future__ import annotations

import sqlite3
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from clawbot_hub.alerts import Alerts
from clawbot_hub.bark import ENV_KEY, BarkKey, BarkResult, Delivered
from clawbot_hub.config import Config
from clawbot_hub.inbox import Inbox
from clawbot_hub.poller import Poller
from clawbot_hub.replier import Replier
from tests.support.fake_clock import FakeClock
from tests.support.hub import Hub

__all__ = [
    "DEVICE",
    "AlertRig",
    "Sent",
    "StubBark",
    "fail_connection",
    "go_to",
    "make_poller",
    "ms_of",
    "round_trip",
]

DEVICE = "TEST-DEVICE-KEY"
BEIJING = ZoneInfo("Asia/Shanghai")


def ms_of(text: str) -> int:
    """`2026-10-14 15:30`（北京时间）→ 毫秒。"""
    when = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=BEIJING)
    return int(when.timestamp()) * 1000


def go_to(clock: FakeClock, text: str) -> None:
    """把假时钟拨到北京时间的这一刻（只能往前拨）。"""
    target = ms_of(text)
    now = clock.now_ms()
    assert target >= now, f"{text} 比现在早"
    clock.advance((target - now) / 1000)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(auto_sleep=True)


@dataclass(frozen=True)
class Sent:
    key: BarkKey
    title: str
    text: str
    at_ms: int


class StubBark:
    """`BarkClient` 的替身：`send(key, title, text)` 记一笔，结果按剧本（剧本里放异常就抛出来）。"""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.calls: list[Sent] = []
        self._script: deque[BarkResult | BaseException] = deque()
        self.default: BarkResult = Delivered()

    def script(self, *results: BarkResult | BaseException) -> None:
        self._script.extend(results)

    def send(self, key: BarkKey, title: str, text: str) -> BarkResult:
        self.calls.append(Sent(key, title, text, self._clock.now_ms()))
        result = self._script.popleft() if self._script else self.default
        if isinstance(result, BaseException):
            raise result
        return result

    @property
    def titles(self) -> list[str]:
        return [call.title for call in self.calls]

    @property
    def texts(self) -> list[str]:
        return [call.text for call in self.calls]


class AlertRig:
    def __init__(
        self,
        paths,
        database,
        clock: FakeClock,
        upstream,
        *,
        config: Config | None = None,
        env_key: str | None = DEVICE,
    ) -> None:
        self.paths = paths
        self.db = database
        self.clock = clock
        self.config = config or Config()
        self.env_key = env_key
        self.bark = StubBark(clock)
        self.alerts = self._alerts()
        self.hub = Hub(
            paths,
            database,
            clock,
            upstream,
            self.config,
            # 重启以后换了新的告警对象，回调要跟着换，所以每次现查
            on_expired=lambda episode: self.alerts.on_expired(episode),
            on_restored=lambda episode: self.alerts.on_restored(episode),
            on_hold=lambda hold: self.alerts.on_hold(hold),
            on_request=lambda kind: self.alerts.on_request(kind),
        )

    def _alerts(self) -> Alerts:
        return Alerts(
            self.db,
            self.config,
            self.clock,
            self.paths,
            client=self.bark,
            env={ENV_KEY: self.env_key} if self.env_key else {},
        )

    def restart(self, config: Config | None = None) -> Alerts:
        """服务重启：新的告警对象（内存状态没了），数据库还是原来的；可以同时换一份配置。"""
        if config is not None:
            self.config = config
        self.alerts = self._alerts()
        return self.alerts

    # ------------------------------------------------ 读库

    def state(self, key: str) -> dict[str, Any] | None:
        row = self.db.conn().execute("SELECT * FROM alert_state WHERE key = ?", (key,)).fetchone()
        return dict(row) if row is not None else None

    def states(self) -> dict[str, dict[str, Any]]:
        rows = self.db.conn().execute("SELECT * FROM alert_state ORDER BY key").fetchall()
        return {row["key"]: dict(row) for row in rows}

    def pending(self) -> dict[str, str]:
        """还没送出去的告警：{键: 正文}。"""
        rows = self.db.conn().execute(
            "SELECT key, pending_text FROM alert_state WHERE pending_text IS NOT NULL"
        )
        return {row["key"]: row["pending_text"] for row in rows}

    def events(self, kind: str | None = "alert") -> list[dict]:
        sql, args = "SELECT * FROM events", ()
        if kind is not None:
            sql, args = sql + " WHERE kind = ?", (kind,)
        return [dict(row) for row in self.db.conn().execute(sql + " ORDER BY id", args)]

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        with self.db.txn() as conn:
            return conn.execute(sql, args)

    # ------------------------------------------------ 动作

    def deliver(self) -> float | None:
        """把到期的待发告警发一遍（在测试自己的线程里）。"""
        return self.alerts.deliver()


# ---------------------------------------------------------------- 在库里造出 poller 留下的痕迹


def fail_connection(rig: AlertRig, reason: str = "连不上") -> None:
    """收消息出了一次连接类的错（没有完整的响应）：poller 只写错误，不写往返。"""
    rig.execute(
        "UPDATE account_state SET last_poll_error = ?, last_poll_error_ms = ? WHERE id = 1",
        reason,
        rig.clock.now_ms(),
    )


def round_trip(rig: AlertRig) -> None:
    """和腾讯完成了一次往返。"""
    now = rig.clock.now_ms()
    rig.execute(
        "UPDATE account_state SET last_poll_ok_ms = ?, last_upstream_ok_ms = ? WHERE id = 1",
        now,
        now,
    )


def make_poller(rig: AlertRig) -> Poller:
    """一个真的收消息循环（不起线程，测试里自己 `step()`），对着这套零件里的假上游。"""
    hub = rig.hub
    hub.upstream.longpoll_seconds = 0  # 没有新消息就马上回，不真等
    inbox = Inbox(rig.db, rig.config, rig.clock, hub.projects)
    replier = Replier(hub.sender, rig.config.hub.label)
    return Poller(
        rig.config,
        rig.clock,
        rig.db,
        hub.upstream,
        hub.store,
        hub.monitor,
        hub.projects,
        hub.quota,
        inbox,
        replier,
    )


@pytest.fixture
def make_rig(paths, database, clock, fake_upstream):
    """要改配置时：`make_rig(Config(...))`；不想配设备码：`make_rig(env_key=None)`。"""

    def build(config: Config | None = None, **kwargs: Any) -> AlertRig:
        return AlertRig(paths, database, clock, fake_upstream, config=config, **kwargs)

    return build


@pytest.fixture
def rig(make_rig) -> AlertRig:
    return make_rig()
