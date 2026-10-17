"""数据留存（规格 `hub-deploy`「数据库和升级」）：两件事，节奏不同，都由定时任务带出。

**每天一次**（`purge_old`）：
- 发送记录、事件、窗口记录、用量记录一律保留 `events.keep_days`（默认 90）天，更早的删掉；删窗口时
  发送记录里指向它的窗口编号置空（外键 `ON DELETE SET NULL`，所以数据库的外键必须是打开的）；
  窗口被删以后，告警状态表里"这个窗口已经告警过"的标记（`window_blocked:<窗口编号>`）也跟着删，
  `window_blocked:none` 不动（它由使用者发话清掉，见 `quota.open_window`）；
- 去重键从发送记录建立起占 `send.dedupe_hours`（默认 24）小时，到点把去重摘要置空，记录本身还在；
- 做过删除就写一条 `lifecycle` 事件，记各删了多少；只释放了去重键不写事件。

**每分钟一次**（`clear_window_tokens`，和待取队列的清理一起）：窗口过期了（起点加
`quota.window_hours` 小时已经到了），它的对话凭证就清掉：过期的窗口发不了任何消息，留着凭证没有
用，只多一份该清的秘密。窗口被新窗口取代、被作废（重新绑定账号）时的清法在 `quota.open_window`
和 `loginstate`；待取记录里的对话凭证在回复句柄失效时清，是 `Inbox.cleanup` 的事。

时间一律从注入的时钟来；删除在一个事务里做完，写事件也在同一个事务里。
"""

from __future__ import annotations

from dataclasses import dataclass

from clawbot_hub.clock import Clock
from clawbot_hub.config import Config
from clawbot_hub.db import Database
from clawbot_hub.events import add_event
from clawbot_hub.quota import HOUR_MS, NONE_WINDOW_KEY, WINDOW_MARKER_PREFIX

DAY_MS = 24 * HOUR_MS


@dataclass(frozen=True)
class Purged:
    """一次每日留存做了什么。"""

    outbox: int = 0
    events: int = 0
    windows: int = 0
    usage: int = 0
    keys: int = 0
    markers: int = 0

    @property
    def deleted(self) -> int:
        """删掉的记录数（释放去重键和清标记不算）。"""
        return self.outbox + self.events + self.windows + self.usage


def purge_old(db: Database, config: Config, clock: Clock) -> Purged:
    """每日留存：删过了保留期的记录，释放到期的去重键，清掉窗口已删的"已告警"标记。"""
    now = clock.now_ms()
    cutoff = now - config.events.keep_days * DAY_MS
    key_cutoff = now - config.send.dedupe_hours * HOUR_MS
    with db.txn() as conn:
        # 先删用量（它按窗口编号找窗口，没有外键），再删窗口，最后删发送记录
        usage = conn.execute(
            "DELETE FROM usage WHERE window_id IN (SELECT id FROM windows WHERE started_ms < ?)",
            (cutoff,),
        ).rowcount
        windows = conn.execute("DELETE FROM windows WHERE started_ms < ?", (cutoff,)).rowcount
        outbox = conn.execute("DELETE FROM outbox WHERE created_ms < ?", (cutoff,)).rowcount
        events = conn.execute("DELETE FROM events WHERE ts_ms < ?", (cutoff,)).rowcount
        keys = conn.execute(
            "UPDATE outbox SET dedupe_hash = NULL"
            " WHERE dedupe_hash IS NOT NULL AND created_ms <= ?",
            (key_cutoff,),
        ).rowcount
        markers = conn.execute(
            "DELETE FROM alert_state WHERE substr(key, 1, ?) = ? AND key != ?"
            " AND CAST(substr(key, ?) AS INTEGER) NOT IN (SELECT id FROM windows)",
            (
                len(WINDOW_MARKER_PREFIX),
                WINDOW_MARKER_PREFIX,
                NONE_WINDOW_KEY,
                len(WINDOW_MARKER_PREFIX) + 1,
            ),
        ).rowcount
        result = Purged(outbox, events, windows, usage, keys, markers)
        if result.deleted:
            add_event(conn, now, "lifecycle", _summary(result, config.events.keep_days))
    return result


def _summary(purged: Purged, keep_days: int) -> str:
    parts = [
        (purged.outbox, "条发送记录"),
        (purged.windows, "个窗口"),
        (purged.usage, "条用量记录"),
        (purged.events, "条事件"),
    ]
    shown = "、".join(f"{count} {unit}" for count, unit in parts if count)
    return f"留存清理：删了 {shown}（超过 {keep_days} 天）"


def clear_window_tokens(db: Database, config: Config, clock: Clock) -> int:
    """过期窗口的对话凭证清掉，返回清了几个。"""
    now = clock.now_ms()
    expired_before = now - config.quota.window_hours * HOUR_MS
    with db.txn() as conn:
        return conn.execute(
            "UPDATE windows SET context_token = NULL"
            " WHERE context_token IS NOT NULL AND started_ms <= ?",
            (expired_before,),
        ).rowcount
