"""告警（规格 `hub-alerts`）：服务自己出了问题要让使用者知道，只走 Bark，不走微信，不占 10 条额度。

分三层：

**文字**（纯函数）：十种告警的标题和正文。标题统一以"ClawBot 共用服务 ·"开头，正文一两句中文，
说明发生了什么、使用者要做什么。这些函数的参数里没有登录凭证、项目密钥和消息正文，所以告警里
不可能带上它们。

**判定和登记**（`Alerts` 的 `on_*` 回调和 `check_*` 检查）：什么时候该发、同一件事发过没有，
都记在告警状态表（`alert_state`）里，判定通过时去重状态和待发文字（`pending_text`）在**同一个
事务**里写；这一层不碰网络。每种告警用到哪几列：

- `login_expired`、`login_restored`：`episode` 是第几次失效；
- `window_expiring`：`episode` 是已经提醒过的窗口编号；`upstream_blocked` 同；
- `send_unconfirmed`：`counter` 是连续没有确认送达的请求数，`episode` 非空表示这一段已经告警过；
- `upstream_down`：`episode` 非空表示一次不通已经告警、还没告知恢复，里面是登记的时刻；
- `window_blocked`：这一行的 `episode` 是"已经处理到哪条被拒记录"（`创建时刻:rowid`），每个
  告警过的窗口另有一行 `window_blocked:<窗口编号或 none>` 当标记（`last_sent_ms` 记登记时刻）；
- `service_restarted`：合并时间看这一行的 `last_sent_ms` 和 `pending_since_ms`；
- `backup_failed`：`episode` 是告警过的那天（显示时区）。

**投递**（`deliver`，由独立的线程一轮一轮调用）：待发的告警一条条送出去。第一次送（`attempts`
是 0）立刻试，暂时不通就隔 1、2、4 秒…再试，最多 `alerts.bark_retries` 次重试；还不行留在待发队列
（这一行的 `pending_text` 就是队列，在数据库里，重启不丢），每 `alerts.queue_retry_minutes` 分钟
试一次，登记以后超过 `alerts.queue_give_up_minutes` 分钟放弃并写一条事件；Bark 明确拒绝（设备码
错了之类）不重试。Bark 回话之后才清待发文字、记发出的时刻，所以判定通过之后、Bark 回话之前被杀
也不会丢（至少一次）。"下一次什么时候试"只记在内存里，重启后第一轮就试。

投递线程在 Bark 的请求上卡住不影响收发：请求有超时（`alerts.bark_timeout_seconds`），而且根本
不占数据库的写锁。Bark 的设备码和请求的细节在 `bark.py`。

**规格没写、这里定下的小约定**（任务单 D2 的实现说明里也有）：

- "发过 `login_expired` 才发 `login_restored`"按"为这一次失效登记过"算，不要求已经送到；
- 窗口里"有项目推送过"按 `usage` 表算（含导入来的 `_import`），回复不算；
- `window_expiring` 的"小时"四舍五入、不写 0；`between` 的结束时刻按分钟算、包含在内，开始比
  结束晚就是跨过午夜；
- `upstream_down` 的分钟数写实际过去的整分钟；
- 没有设备码的告警和 Bark 暂时不通一样留在待发队列里按同样的节奏试、同样的时限放弃，只是事件
  写"没有配置设备码"，而且只在第一轮写一条；
- 一条告警还在待发队列里的时候又来了同一种的新告警（比如重启告警隔了合并时间），新的换掉旧的。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, tzinfo

from clawbot_hub.bark import (
    BarkClient,
    BarkKey,
    BarkResult,
    BarkSender,
    Delivered,
    Refused,
    Unreachable,
    lookup_bark_key,
)
from clawbot_hub.clock import Clock, ClockEvent
from clawbot_hub.config import Config
from clawbot_hub.db import Database
from clawbot_hub.events import add_event
from clawbot_hub.instance import RunInfo
from clawbot_hub.paths import HubPaths
from clawbot_hub.quota import (
    HOUR_MS,
    MINUTE_MS,
    WINDOW_MARKER_PREFIX,
    Hold,
    Quota,
    Window,
    window_ends_ms,
)
from clawbot_hub.sender import CONFIRMED, UNCONFIRMED
from clawbot_hub.services import Hooks
from clawbot_hub.textutil import mask
from clawbot_hub.timefmt import moment_text

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 十种告警

LOGIN_EXPIRED = "login_expired"
LOGIN_RESTORED = "login_restored"
WINDOW_EXPIRING = "window_expiring"
WINDOW_BLOCKED = "window_blocked"
SEND_UNCONFIRMED = "send_unconfirmed"
UPSTREAM_BLOCKED = "upstream_blocked"
UPSTREAM_DOWN = "upstream_down"
UPSTREAM_RESTORED = "upstream_restored"
SERVICE_RESTARTED = "service_restarted"
BACKUP_FAILED = "backup_failed"

TITLE_PREFIX = "ClawBot 共用服务 · "

_TITLES = {
    LOGIN_EXPIRED: "登录失效",
    LOGIN_RESTORED: "登录已恢复",
    WINDOW_EXPIRING: "窗口快到期",
    WINDOW_BLOCKED: "微信消息没发出去",
    SEND_UNCONFIRMED: "发送未确认",
    UPSTREAM_BLOCKED: "腾讯拒绝发送",
    UPSTREAM_DOWN: "连不上腾讯",
    UPSTREAM_RESTORED: "已连上腾讯",
    SERVICE_RESTARTED: "服务重启",
    BACKUP_FAILED: "备份失败",
}
KINDS = tuple(_TITLES)

# `hubctl alerts test` 发的那一条；不在十种之内，不占去重状态
TEST_TITLE = TITLE_PREFIX + "测试"
TEST_TEXT = "这是一条测试告警，收到说明 Bark 配置没问题"

# `window_blocked` 汇总的三种拒绝原因（`on_closed: skip` 的是 skipped，不在这里）
BLOCKED_REASONS = ("window_closed", "never_contacted", "quota_exhausted")

# 投递一轮出了意外的错（比如数据库暂时被锁）之后歇多久再来
ERROR_PAUSE_SECONDS = 30.0


def title(kind: str) -> str:
    """告警的标题；不认识的种类抛 `KeyError`。"""
    return TITLE_PREFIX + _TITLES[kind]


def login_expired_text() -> str:
    return "ClawBot 登录失效了：在电脑上运行 hubctl login 重新扫码"


def login_restored_text() -> str:
    return "ClawBot 登录已恢复"


def window_expiring_text(remaining_ms: int, ends_ms: int, tz: tzinfo) -> str:
    """小时数四舍五入（半小时进一），不足一小时也写 1，不写 0。"""
    hours = max(1, (remaining_ms + HOUR_MS // 2) // HOUR_MS)
    return f"微信窗口还有约 {hours} 小时到期（{moment_text(ends_ms, tz)}）：给 ClawBot 回一句话续期"


def window_blocked_text(projects: int) -> str:
    return (
        f"有 {projects} 个项目的微信消息没发出去（已经由它们自己走 Bark）：给 ClawBot 发一句话恢复"
    )


def send_unconfirmed_text(streak: int) -> str:
    return f"最近 {streak} 次发送腾讯都没有给消息编号，消息可能没到微信：看一眼手机"


def upstream_blocked_text(code: str, minutes: int) -> str:
    return f"腾讯拒绝了这个窗口里的发送（代码 {code}），已暂停 {minutes} 分钟；规则可能变了"


def upstream_down_text(minutes: int) -> str:
    return f"连不上腾讯的 ClawBot 服务已经 {minutes} 分钟"


def upstream_restored_text() -> str:
    return "已重新连上腾讯的 ClawBot 服务"


def service_restarted_text(last_beat_ms: int | None, interrupted: int, tz: tzinfo) -> str:
    """没有最后心跳就不写"上次运行到"，没有中断的发送就不写后半句。"""
    text = "共用服务重启了"
    if last_beat_ms is not None:
        text += f"（上次运行到 {datetime.fromtimestamp(last_beat_ms // 1000, tz):%H:%M}）"
    if interrupted > 0:
        text += f"；有 {interrupted} 条发送中断，可能少发或重发"
    return text


def backup_failed_text(reason: str) -> str:
    return f"备份失败，原因：{reason}"


# ---------------------------------------------------------------- Bark 最近一次的结论

OUTCOME_KEY = "bark_last_outcome"
OK = "ok"
REFUSED = "refused"


def last_bark_outcome(conn: sqlite3.Connection) -> str | None:
    """Bark 最近一次明确的回话：`ok`（收下了）、`refused`（拒绝了，多半是设备码不对），从来
    没有明确回过话是 `None`。暂时不通说明不了设备码对不对，不改变这个结论。`hubctl doctor` 用它。"""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (OUTCOME_KEY,)).fetchone()
    return None if row is None else row["value"]


def _set_outcome(conn: sqlite3.Connection, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (OUTCOME_KEY, value),
    )


# ---------------------------------------------------------------- 测试告警


@dataclass(frozen=True)
class BarkTestResult:
    """`hubctl alerts test` 的结果。`status`：`delivered`、`refused`、`unreachable`、`no_key`；
    `detail` 是一句话说明，不含设备码。"""

    status: str
    detail: str


def send_test_alert(
    db: Database,
    config: Config,
    clock: Clock,
    paths: HubPaths,
    *,
    client: BarkSender | None = None,
    env: Mapping[str, str] | None = None,
) -> BarkTestResult:
    """发一条测试告警：只请求一次，结果直接告诉调用者，写一条事件；不占去重状态，不进待发队列。"""
    lookup = lookup_bark_key(paths, env)
    if lookup.key is None:
        return BarkTestResult("no_key", lookup.problem or "没有配置设备码")
    if client is None:
        client = BarkClient(config.alerts.group, timeout_seconds=config.alerts.bark_timeout_seconds)
    try:
        result = client.send(lookup.key, TEST_TITLE, TEST_TEXT)
    except Exception as exc:  # noqa: BLE001 - 换了什么错都要告诉调用者，不能抛出去
        logger.warning("发测试告警时出了意外的错误：%s", type(exc).__name__)
        result = Unreachable(type(exc).__name__)
    with db.txn() as conn:
        now = clock.now_ms()
        if isinstance(result, Delivered):
            add_event(conn, now, "alert", "Bark 测试成功")
            _set_outcome(conn, OK)
            return BarkTestResult("delivered", "Bark 收下了测试告警")
        if isinstance(result, Refused):
            reason = mask(result.reason, [lookup.key.device])
            add_event(conn, now, "alert", f"Bark 测试被拒绝：{reason}", level="WARNING")
            _set_outcome(conn, REFUSED)
            return BarkTestResult("refused", reason)
        add_event(conn, now, "alert", f"Bark 测试没送到：{result.reason}", level="WARNING")
        return BarkTestResult("unreachable", result.reason)


# ---------------------------------------------------------------- 小工具


def _cursor(episode: str | None) -> tuple[int, int]:
    """`window_blocked` 那一行的 `episode`：`创建时刻:rowid`，读不出来就从头算。"""
    try:
        created, rowid = (episode or "").split(":")
        return int(created), int(rowid)
    except ValueError:
        return -1, -1


def _cursor_text(position: tuple[int, int]) -> str:
    return f"{position[0]}:{position[1]}"


def _window_key(window_id: int | None) -> str:
    """去重的单位：窗口编号，记录里没有窗口时是固定的 `none`。"""
    return "none" if window_id is None else str(window_id)


def _minute_of_day(text: str) -> int:
    hours, minutes = text.split(":")
    return int(hours) * 60 + int(minutes)


def _last_alert_ms(row: sqlite3.Row | None) -> int:
    """这一种告警上一次登记（还在排队）或发出的时刻，没有是 0。合并时间和最小间隔都从这里算。"""
    if row is None:
        return 0
    return max(row["last_sent_ms"] or 0, row["pending_since_ms"] or 0)


def _whole(text: str | None) -> int:
    try:
        return int(text or "")
    except ValueError:
        return 0


# ---------------------------------------------------------------- 告警


class Alerts:
    """判定、登记、投递。`client` 默认是真的 Bark 客户端，测试里换成替身；`env` 是读设备码用的
    环境变量（默认取进程的，**在这里读一次**，所以服务运行中改环境变量不生效，要重启）。"""

    def __init__(
        self,
        db: Database,
        config: Config,
        clock: Clock,
        paths: HubPaths,
        *,
        client: BarkSender | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._db = db
        self._config = config
        self._clock = clock
        self._paths = paths
        self._tz = config.display.tzinfo
        self._env = dict(os.environ if env is None else env)
        self._client: BarkSender = client or BarkClient(
            config.alerts.group, timeout_seconds=config.alerts.bark_timeout_seconds
        )
        self._quota = Quota(config, clock)
        self._started_ms = clock.now_ms()  # 刚启动还没有往返过的，"连不上"从这一刻算起
        self._next_try: dict[str, int] = {}  # 待发队列里每条下一次什么时候试（只在内存里）
        self._pass = threading.Lock()  # 一次只有一个投递的轮次
        self._wake = ClockEvent()  # 有新登记的告警，叫醒投递线程
        self._stop = ClockEvent()
        self._thread: threading.Thread | None = None

    def hooks(self) -> Hooks:
        """接到 `services.assemble` 上的四个回调。"""
        return Hooks(
            on_expired=self.on_expired,
            on_restored=self.on_restored,
            on_hold=self.on_hold,
            on_request=self.on_request,
        )

    # ------------------------------------------------ 写状态表的小动作

    @staticmethod
    def _row(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM alert_state WHERE key = ?", (key,)).fetchone()

    def _put(self, conn: sqlite3.Connection, key: str, episode: str | None, text: str) -> None:
        """登记一条待发的告警：去重状态（`episode`）和待发文字在调用者的同一个事务里写。
        `last_sent_ms` 和计数不动；换掉同一种还在排队的那一条时 `attempts` 归零，当新的送。"""
        conn.execute(
            "INSERT INTO alert_state (key, episode, pending_text, pending_since_ms, attempts)"
            " VALUES (?, ?, ?, ?, 0)"
            " ON CONFLICT(key) DO UPDATE SET episode = excluded.episode,"
            " pending_text = excluded.pending_text,"
            " pending_since_ms = excluded.pending_since_ms, attempts = 0",
            (key, episode, text, self._clock.now_ms()),
        )

    def _queued(self, key: str) -> None:
        """事务提交以后调：新登记的这一条马上可以送，叫醒投递线程。"""
        self._next_try.pop(key, None)
        self._wake.set()

    def _event(self, conn: sqlite3.Connection, detail: str, level: str = "INFO") -> None:
        add_event(conn, self._clock.now_ms(), "alert", detail, level=level)

    # ------------------------------------------------ 登录失效 / 恢复

    def on_expired(self, episode: int) -> None:
        """登录由正常变成失效。去重的单位是"一次失效"：同一次不再登记，哪怕重启后回调又来一次。"""
        with self._db.txn() as conn:
            row = self._row(conn, LOGIN_EXPIRED)
            if row is not None and row["episode"] == str(episode):
                return
            self._put(conn, LOGIN_EXPIRED, str(episode), login_expired_text())
        self._queued(LOGIN_EXPIRED)

    def on_restored(self, episode: int) -> None:
        """失效之后恢复。只在为这一次失效登记过失效告警之后发，而且每次失效只发一次。"""
        with self._db.txn() as conn:
            expired = self._row(conn, LOGIN_EXPIRED)
            if expired is None or expired["episode"] != str(episode):
                return
            restored = self._row(conn, LOGIN_RESTORED)
            if restored is not None and restored["episode"] == str(episode):
                return
            self._put(conn, LOGIN_RESTORED, str(episode), login_restored_text())
        self._queued(LOGIN_RESTORED)

    # ------------------------------------------------ 腾讯拒绝发送

    def on_hold(self, hold: Hold) -> None:
        """腾讯明确拒绝了推送、窗口进入暂停。一个窗口一次：同一个窗口里暂停之后再被拒绝不重复发。"""
        with self._db.txn() as conn:
            row = self._row(conn, UPSTREAM_BLOCKED)
            if row is not None and row["episode"] == str(hold.window_id):
                return
            text = upstream_blocked_text(hold.code, hold.minutes)
            self._put(conn, UPSTREAM_BLOCKED, str(hold.window_id), text)
        self._queued(UPSTREAM_BLOCKED)

    # ------------------------------------------------ 发送未确认

    def on_request(self, kind: str) -> None:
        """一个 `sendmessage` 请求结束了。按请求算连续没有确认送达的次数：确认送达归零，
        没确认加一，刚好够 `alerts.unconfirmed_streak` 次时登记一条，之后要先有一次确认再连续
        那么多次才会再发。别的结局（被拒、没发出去）服务根本不会叫这个回调。"""
        if kind == CONFIRMED:
            self._reset_streak()
        elif kind == UNCONFIRMED:
            self._count_unconfirmed()

    def _reset_streak(self) -> None:
        row = self._row(self._db.conn(), SEND_UNCONFIRMED)
        if row is None or (row["counter"] == 0 and row["episode"] is None):
            return  # 绝大多数请求都走这里，不必为它们抢写锁
        with self._db.txn() as conn:
            conn.execute(
                "UPDATE alert_state SET counter = 0, episode = NULL WHERE key = ?",
                (SEND_UNCONFIRMED,),
            )

    def _count_unconfirmed(self) -> None:
        streak = self._config.alerts.unconfirmed_streak
        registered = False
        with self._db.txn() as conn:
            row = self._row(conn, SEND_UNCONFIRMED)
            count = (0 if row is None else row["counter"]) + 1
            alerted = row is not None and row["episode"] is not None
            conn.execute(
                "INSERT INTO alert_state (key, counter) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET counter = excluded.counter",
                (SEND_UNCONFIRMED, count),
            )
            if count >= streak and not alerted:
                self._put(conn, SEND_UNCONFIRMED, "alerted", send_unconfirmed_text(streak))
                registered = True
        if registered:
            self._queued(SEND_UNCONFIRMED)

    # ------------------------------------------------ 窗口快到期

    def check_window_expiring(self) -> bool:
        """定时任务每一轮调一次；这一轮登记了提醒就返回真。条件见 `hub-alerts`「窗口快到期的
        提醒」：窗口还开着且剩余不超过 `hours_before`、现在在白天的范围和星期之内、这个窗口里有
        项目推送过、这个窗口还没提醒过。"""
        warn = self._config.alerts.window_warn
        now = self._clock.now_ms()
        conn = self._db.conn()
        window = self._quota.current_window(conn)
        ends = None if window is None else window_ends_ms(window, self._config.quota)
        self._void_stale_reminder(window, ends, now)
        if not warn.enabled or window is None or ends is None:
            return False
        remaining = ends - now
        if remaining <= 0 or remaining > warn.hours_before * HOUR_MS:
            return False
        if not self._daytime(now) or not self._pushed_in(conn, window.id):
            return False
        with self._db.txn() as write:
            row = self._row(write, WINDOW_EXPIRING)
            if row is not None and row["episode"] == str(window.id):
                return False
            text = window_expiring_text(remaining, ends, self._tz)
            self._put(write, WINDOW_EXPIRING, str(window.id), text)
        self._queued(WINDOW_EXPIRING)
        return True

    def _void_stale_reminder(self, window: Window | None, ends: int | None, now: int) -> None:
        """还没送出去的提醒，窗口已经换了（使用者又说了话）或者已经关了：不再有意义，作废，不补发。"""
        row = self._row(self._db.conn(), WINDOW_EXPIRING)
        if row is None or row["pending_text"] is None:
            return
        current = (
            window is not None
            and ends is not None
            and now < ends
            and row["episode"] == str(window.id)
        )
        if current:
            return
        with self._db.txn() as conn:
            cleared = conn.execute(
                "UPDATE alert_state SET pending_text = NULL, pending_since_ms = NULL, attempts = 0"
                " WHERE key = ? AND pending_text IS NOT NULL AND pending_since_ms IS ?",
                (WINDOW_EXPIRING, row["pending_since_ms"]),
            ).rowcount
            if cleared:
                self._event(conn, "窗口快到期的提醒作废了：窗口已经续期或关闭，不补发")
        self._next_try.pop(WINDOW_EXPIRING, None)

    def _daytime(self, now: int) -> bool:
        """现在（显示时区）在 `between` 之内、今天是 `weekdays` 之一。结束的那一分钟也算在内；
        开始比结束晚就是跨过午夜。"""
        warn = self._config.alerts.window_warn
        local = datetime.fromtimestamp(now // 1000, self._tz)
        if local.isoweekday() not in warn.weekdays:
            return False
        start, end = (_minute_of_day(part) for part in warn.between.split("-"))
        minute = local.hour * 60 + local.minute
        if start <= end:
            return start <= minute <= end
        return minute >= start or minute <= end

    @staticmethod
    def _pushed_in(conn: sqlite3.Connection, window_id: int) -> bool:
        row = conn.execute(
            "SELECT 1 FROM usage WHERE window_id = ? AND kind = 'push' AND n > 0 LIMIT 1",
            (window_id,),
        ).fetchone()
        return row is not None

    # ------------------------------------------------ 推送没发出去的汇总

    def check_window_blocked(self) -> bool:
        """定时任务每一轮调一次：把发送记录里被 `BLOCKED_REASONS` 拒绝的推送汇总成一条。

        素材就是发送记录，"处理到哪条"记在 `window_blocked` 那一行的 `episode` 里。去掉窗口已经
        告警过的（标记行）；剩下的距上一条不足 `min_interval_hours` 就先压住、留着累计；够了就
        发，N 是涉及的不同项目数，并把涉及的窗口记为已告警。已经被标记盖住的记录以后不会再被
        数到，所以使用者发话清掉 `none` 标记之后，旧的拒绝不会又冒出来。"""
        row = self._row(self._db.conn(), WINDOW_BLOCKED)
        if not self._rejections(self._db.conn(), _cursor(None if row is None else row["episode"])):
            return False  # 空闲的时候一次只读查询就够了，不抢写锁
        with self._db.txn() as conn:
            row = self._row(conn, WINDOW_BLOCKED)
            cursor = _cursor(None if row is None else row["episode"])
            rejections = self._rejections(conn, cursor)
            if not rejections:
                return False
            marked = {
                r["key"][len(WINDOW_MARKER_PREFIX) :]
                for r in conn.execute(
                    "SELECT key FROM alert_state WHERE substr(key, 1, ?) = ?",
                    (len(WINDOW_MARKER_PREFIX), WINDOW_MARKER_PREFIX),
                )
            }
            fresh = [r for r in rejections if _window_key(r["window_id"]) not in marked]
            if not fresh:
                self._move_cursor(
                    conn, cursor, (rejections[-1]["created_ms"], rejections[-1]["rid"])
                )
                return False
            now = self._clock.now_ms()
            interval = self._config.alerts.blocked.min_interval_hours * HOUR_MS
            last = _last_alert_ms(row)
            waiting = row is not None and row["pending_text"] is not None
            if waiting or (last and now - last < interval):
                first = fresh[0]
                self._move_cursor(conn, cursor, (first["created_ms"], first["rid"] - 1))
                return False
            for key in {_window_key(r["window_id"]) for r in fresh}:
                conn.execute(
                    "INSERT INTO alert_state (key, last_sent_ms) VALUES (?, ?)"
                    " ON CONFLICT(key) DO NOTHING",
                    (WINDOW_MARKER_PREFIX + key, now),
                )
            projects = len({r["project_id"] for r in fresh})
            newest = fresh[-1]
            self._put(
                conn,
                WINDOW_BLOCKED,
                _cursor_text((newest["created_ms"], newest["rid"])),
                window_blocked_text(projects),
            )
        self._queued(WINDOW_BLOCKED)
        return True

    @staticmethod
    def _rejections(conn: sqlite3.Connection, cursor: tuple[int, int]) -> list[sqlite3.Row]:
        marks = ", ".join("?" for _ in BLOCKED_REASONS)
        return conn.execute(
            "SELECT rowid AS rid, created_ms, window_id, project_id FROM outbox"
            f" WHERE kind = 'push' AND status = 'rejected' AND reason_code IN ({marks})"
            "   AND (created_ms > ? OR (created_ms = ? AND rowid > ?))"
            " ORDER BY created_ms, rowid",
            (*BLOCKED_REASONS, cursor[0], cursor[0], cursor[1]),
        ).fetchall()

    @staticmethod
    def _move_cursor(
        conn: sqlite3.Connection, current: tuple[int, int], position: tuple[int, int]
    ) -> None:
        if position == current:
            return
        conn.execute(
            "INSERT INTO alert_state (key, episode) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET episode = excluded.episode",
            (WINDOW_BLOCKED, _cursor_text(position)),
        )

    # ------------------------------------------------ 腾讯不通 / 恢复

    def check_upstream(self) -> bool:
        """定时任务每一轮调一次。

        **连不上**：登录状态是正常，最近一次出错是连接类的（比最近一次完成往返还新；收到完整
        响应的错误同时记了往返，所以不算），而且距离最近一次完成往返（刚启动还没有的，从启动时刻
        算）已经 `upstream_down_minutes` 分钟：登记 `upstream_down`，并记下"这一段不通已经告警"。
        **恢复**：已经告警的这一段不通，之后又有了新的完整往返：登记 `upstream_restored`。"""
        conn = self._db.conn()
        account = conn.execute(
            "SELECT login_state, last_upstream_ok_ms, last_poll_error_ms FROM account_state"
            " WHERE id = 1"
        ).fetchone()
        flag = self._row(conn, UPSTREAM_DOWN)
        ok = account["last_upstream_ok_ms"] or 0
        if flag is not None and flag["episode"] is not None:
            return self._recover(ok) if ok > _whole(flag["episode"]) else False
        error = account["last_poll_error_ms"]
        if account["login_state"] != "ok" or error is None or error <= ok:
            return False
        now = self._clock.now_ms()
        elapsed = now - max(ok, self._started_ms)
        if elapsed < self._config.alerts.upstream_down_minutes * MINUTE_MS:
            return False
        with self._db.txn() as write:
            flag = self._row(write, UPSTREAM_DOWN)
            if flag is not None and flag["episode"] is not None:
                return False
            self._put(write, UPSTREAM_DOWN, str(now), upstream_down_text(elapsed // MINUTE_MS))
        self._queued(UPSTREAM_DOWN)
        return True

    def _recover(self, ok_ms: int) -> bool:
        with self._db.txn() as conn:
            flag = self._row(conn, UPSTREAM_DOWN)
            if flag is None or flag["episode"] is None or ok_ms <= _whole(flag["episode"]):
                return False
            conn.execute("UPDATE alert_state SET episode = NULL WHERE key = ?", (UPSTREAM_DOWN,))
            self._put(conn, UPSTREAM_RESTORED, str(self._clock.now_ms()), upstream_restored_text())
        self._queued(UPSTREAM_RESTORED)
        return True

    # ------------------------------------------------ 服务重启 / 备份失败

    def service_restarted(self, previous: RunInfo | None, interrupted: int) -> bool:
        """服务启动时调：上一次不是正常退出，或者有中断的发送，就登记一条（只登记，由投递线程
        发）。`previous` 是启动前读到的 `run.json`，第一次启动是 `None`。
        `alerts.restart_merge_minutes` 分钟内最多一次：距离上一条的登记或发出不够，这一次并进去，
        不动那条的时间，也不重复登记。"""
        crashed = previous is not None and not previous.clean_exit
        if not crashed and interrupted <= 0:
            return False
        beat = None if previous is None else previous.last_heartbeat_ms
        with self._db.txn() as conn:
            row = self._row(conn, SERVICE_RESTARTED)
            now = self._clock.now_ms()
            last = _last_alert_ms(row)
            merge = self._config.alerts.restart_merge_minutes * MINUTE_MS
            if last and now - last < merge:
                return False
            text = service_restarted_text(beat, max(interrupted, 0), self._tz)
            self._put(conn, SERVICE_RESTARTED, None, text)
        self._queued(SERVICE_RESTARTED)
        return True

    def backup_failed(self, reason: str) -> bool:
        """每日备份重试之后仍然失败。一天一条（按显示时区的日期）。"""
        day = datetime.fromtimestamp(self._clock.now_ms() // 1000, self._tz).strftime("%Y-%m-%d")
        with self._db.txn() as conn:
            row = self._row(conn, BACKUP_FAILED)
            if row is not None and row["episode"] == day:
                return False
            self._put(conn, BACKUP_FAILED, day, backup_failed_text(reason))
        self._queued(BACKUP_FAILED)
        return True

    # ------------------------------------------------ 投递

    def deliver(self) -> float | None:
        """把到期的待发告警都送一遍（最早登记的先送），返回离下一次到期还有几秒，没有待发的返回
        `None`。每一轮每条最多处理一次：送的过程中被换掉的那条留到下一轮。"""
        with self._pass:
            for row in self._pending():
                if self._stop.is_set():
                    break
                self._deliver_one(row)
            return self._seconds_to_next()

    def _pending(self) -> list[sqlite3.Row]:
        return (
            self._db.conn()
            .execute(
                "SELECT key, pending_text, pending_since_ms, attempts FROM alert_state"
                " WHERE pending_text IS NOT NULL ORDER BY pending_since_ms, rowid"
            )
            .fetchall()
        )

    def _seconds_to_next(self) -> float | None:
        now = self._clock.now_ms()
        waits = []
        for row in self._pending():
            due = self._next_try.get(row["key"]) if row["attempts"] > 0 else None
            waits.append(0 if due is None else max(0, due - now))
        return min(waits) / 1000 if waits else None

    def _deliver_one(self, row: sqlite3.Row) -> None:
        key, text, attempts = row["key"], row["pending_text"], row["attempts"]
        since = row["pending_since_ms"]
        now = self._clock.now_ms()
        alerts = self._config.alerts
        if attempts > 0:  # 已经在待发队列里
            if now - since > alerts.queue_give_up_minutes * MINUTE_MS:
                self._give_up(key, since)
                return
            due = self._next_try.get(key)
            if due is not None and now < due:
                return
        lookup = lookup_bark_key(self._paths, self._env)
        if lookup.key is None:
            self._queue_again(key, since, attempts, 1, self._no_key_event(key, lookup.problem))
            return
        result: BarkResult = Unreachable("还没有请求")
        made = 0
        for index in range(1 + alerts.bark_retries if attempts == 0 else 1):
            if index:
                if self._stop.is_set():
                    break
                self._clock.sleep(2 ** (index - 1))  # 立即重试：1、2、4 秒……
            result = self._send(lookup.key, key, text)
            made += 1
            if not isinstance(result, Unreachable):
                break
        if isinstance(result, Delivered):
            self._finish(key, text, since, sent=True)
        elif isinstance(result, Refused):
            reason = mask(result.reason, [lookup.key.device])
            self._finish(key, text, since, sent=False, reason=reason)
        else:  # 原因只会是状态码或异常的类型名，不含设备码，可以写进事件
            event = f"告警暂时没发出去，留在待发队列：{_TITLES.get(key, key)}（{result.reason}）"
            self._queue_again(key, since, attempts, made, event)

    def _send(self, bark_key: BarkKey, key: str, text: str) -> BarkResult:
        try:
            return self._client.send(bark_key, TITLE_PREFIX + _TITLES.get(key, key), text)
        except Exception as exc:  # noqa: BLE001 - 客户端出了意外的错，当作暂时不通，不能让线程死掉
            logger.warning("发告警时出了意外的错误：%s", type(exc).__name__)
            return Unreachable(type(exc).__name__)

    def _no_key_event(self, key: str, problem: str | None) -> str:
        name = _TITLES.get(key, key)
        if problem:
            return f"告警未送出（设备码配置有问题）：{name}（{problem}）"
        return f"告警未送出（没有配置设备码）：{name}"

    def _clear_pending(self, conn: sqlite3.Connection, key: str, text: str, since: int) -> bool:
        """清掉我送的这一条：送的过程中被换成新的，就留着新的。"""
        return (
            conn.execute(
                "UPDATE alert_state SET pending_text = NULL, pending_since_ms = NULL, attempts = 0"
                " WHERE key = ? AND pending_text = ? AND pending_since_ms = ?",
                (key, text, since),
            ).rowcount
            > 0
        )

    def _finish(self, key: str, text: str, since: int, *, sent: bool, reason: str = "") -> None:
        """Bark 回了明确的话（收下了，或者拒绝了）：清待发文字，写事件和 Bark 最近的结论。
        `reason` 是 Bark 拒绝的原话，已经遮掉了设备码。"""
        name = _TITLES.get(key, key)
        with self._db.txn() as conn:
            if sent:
                conn.execute(
                    "UPDATE alert_state SET last_sent_ms = ? WHERE key = ?",
                    (self._clock.now_ms(), key),
                )
            self._clear_pending(conn, key, text, since)
            if sent:
                self._event(conn, f"告警已送出：{name}")
                _set_outcome(conn, OK)
            else:
                self._event(conn, f"告警被 Bark 拒绝：{name}（{reason}）", "WARNING")
                _set_outcome(conn, REFUSED)
        self._next_try.pop(key, None)

    def _queue_again(self, key: str, since: int, before: int, made: int, event: str) -> None:
        """这一轮没送成：留在待发队列，过 `queue_retry_minutes` 分钟再试。第一轮没送成时写事件。"""
        with self._db.txn() as conn:
            changed = conn.execute(
                "UPDATE alert_state SET attempts = attempts + ?"
                " WHERE key = ? AND pending_since_ms = ?",
                (made, key, since),
            ).rowcount
            if changed and before == 0:
                self._event(conn, event, "WARNING")
        if changed:
            retry = self._config.alerts.queue_retry_minutes * MINUTE_MS
            self._next_try[key] = self._clock.now_ms() + retry

    def _give_up(self, key: str, since: int) -> None:
        minutes = self._config.alerts.queue_give_up_minutes
        with self._db.txn() as conn:
            cleared = conn.execute(
                "UPDATE alert_state SET pending_text = NULL, pending_since_ms = NULL, attempts = 0"
                " WHERE key = ? AND pending_since_ms = ?",
                (key, since),
            ).rowcount
            if cleared:
                detail = f"告警放弃：{_TITLES.get(key, key)}（超过 {minutes} 分钟还没发出去）"
                self._event(conn, detail, "ERROR")
        self._next_try.pop(key, None)

    # ------------------------------------------------ 投递线程

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """起投递线程。启动时库里还留着的待发告警，线程的第一轮就送。"""
        if self._thread is not None:
            raise RuntimeError("告警投递线程已经启动过了")
        self._thread = threading.Thread(target=self._run, name="alerts", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """让线程结束并等它（真实时间最多 `timeout` 秒）。没启动过、重复停都没关系。"""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()  # 先清再送：送的时候新登记的会重新置位，下面的等待就不会错过
            try:
                due = self.deliver()
            except Exception as exc:  # noqa: BLE001 - 一轮出错（比如数据库暂时被锁）不能让线程死掉
                logger.error("告警投递出错：%s", type(exc).__name__)
                due = ERROR_PAUSE_SECONDS
            if self._stop.is_set():
                break
            self._wake.wait(self._clock, due)
