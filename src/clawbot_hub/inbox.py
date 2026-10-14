"""待取队列：入队、租约、取话、确认、回复句柄、等确认标记、清理（任务 C2，规格 `hub-inbox`）。

使用者发来的话经路由（`router.py`）交给某个项目以后，存成这个项目的一条待取记录；项目用
`GET /v1/inbox` 取（`fetch`）、处理完用 `POST /v1/inbox/ack` 确认（`ack`），回复时拿取话时给的
回复句柄（`resolve_reply`）。保证至少送到一次：取走没确认的话，租期过了再给一次（最多
`inbox.max_attempts` 次）；项目离线攒着的话保留 `inbox.retention_minutes` 分钟。

几个要点：

- 数据库是唯一的状态（重启不丢）；内存里只有"谁正挂着一个取话请求"和叫醒它们的条件变量。
  poller 在自己的事务里 `enqueue`，提交以后调 `wake`；挂着的请求每 5 秒（`SEGMENT_SECONDS`）
  自己再看一次库，所以 `hubctl` 这类另一个进程写的库（停用项目、移除项目）最迟 5 秒后生效。
- 正文只存在 `inbox.text`，确认、过期、作废时立即清掉。对话凭证（`context_token`）留到回复句柄
  失效：没租出去过的话在过期、作废时一起清（项目根本没拿到句柄）；租出去过的话项目可能还在处理，
  处理完要回复。
- "新的取话请求顶掉旧的"只针对会等的请求（`wait` 大于 0）；`max=0` 的心跳和不等的请求不动
  正在等的那个。
- 时间一律走注入的 `Clock`，等待走 `Clock.wait`，所以假时钟下不真等。
- 服务关闭（`shutdown`）时挂着的取话请求马上返回空列表，之后的取话也不再租出话。

规格没写、这里定下的小约定：队列满了丢最旧的"没租出去的"，被挤掉的话标 `dropped`；全是租出去的
时候新来的一句不建记录（只记事件）；一句话既租满了次数又过了保留期，算"作废"（带事件），比"过期"
更能说明是项目没确认；同一个请求里重复的 `id` 只算一个；已处理的记录从收到起算保留 7 天。
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field

from clawbot_hub.clock import Clock
from clawbot_hub.config import Config
from clawbot_hub.db import Database
from clawbot_hub.events import add_event
from clawbot_hub.projects import Project, ProjectDisabled, Projects, is_online

# 等待分成最长 5 秒一段，每醒来一次都再核对一次项目是否仍启用（固定常量）
SEGMENT_SECONDS = 5
# 已确认、过期、作废的记录和收消息的去重记录保留多久（固定常量）
RECORD_KEEP_DAYS = 7

OLD_MESSAGE = "这句话太旧，微信已不能回复"
HANDLE_CLEARED = "这句话已经不能回复了：重新登录过，或者它没被处理就过期了"

_MINUTE_MS = 60_000
_HOUR_MS = 60 * _MINUTE_MS
_DAY_MS = 24 * _HOUR_MS


# ---------------------------------------------------------------- 结果和错误


@dataclass(frozen=True)
class Leased:
    """取话返回的一条（接口里的 `items`，时间的显示格式由接口层定）。"""

    id: str
    text: str
    has_text: bool
    at_ms: int
    received_ms: int
    attempt: int
    via: str
    reply_handle: str


@dataclass(frozen=True)
class Enqueued:
    """话已入队。`dropped_oldest`：队列满了，为它腾地方丢掉的最旧一条的编号（没丢就是 `None`）。"""

    id: str
    dropped_oldest: str | None = None


@dataclass(frozen=True)
class QueueFull:
    """队列里全是租出去还没确认的话，没有可丢的：这一句没收（没有记录，只记了事件）。"""


@dataclass(frozen=True)
class Acked:
    acked: int
    unknown: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReplyTarget:
    """回复句柄指向的那句话：回复用它的对话凭证发。`replies` 是已经成功回复了几次。"""

    item_id: str
    context_token: str
    at_ms: int
    replies: int


@dataclass(frozen=True)
class Cleaned:
    """一轮清理做了什么：过期几条、作废几条、清了几个对话凭证、删了几条旧记录和去重记录。"""

    expired: int = 0
    dropped: int = 0
    tokens: int = 0
    deleted: int = 0
    seen: int = 0


class HandleError(Exception):
    """回复句柄用不了；信息是给项目看的一句中文。"""


class UnknownHandle(HandleError):
    """句柄不存在，或者不属于调用方——两种情况一模一样，不透露是哪一种（404）。"""

    def __init__(self) -> None:
        super().__init__("回复句柄不存在")


class HandleExpired(HandleError):
    """句柄过期，或者那句话的对话凭证已经清掉（410）；信息就是原因。"""


class ReplyLimit(HandleError):
    """同一个句柄成功回复的次数用完了（429）。429 要带 `Retry-After`，规格没说这里该写多少：
    `retry_after` 取句柄还有几秒失效（进一位，所以至少 1）——等多久这个句柄都回复不了，到时候只会
    变成 410。"""

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# ---------------------------------------------------------------- 队列


class Inbox:
    """所有方法自己开事务（`enqueue` 和 `clear_awaiting` 除外：它们在调用者的事务里做），可以被
    API 的几个线程和 poller 同时用。"""

    def __init__(self, db: Database, config: Config, clock: Clock, projects: Projects) -> None:
        self._db = db
        self._config = config
        self._clock = clock
        self._projects = projects
        self._lock = threading.Lock()
        self._conditions: dict[int, threading.Condition] = {}
        self._generation: dict[int, int] = {}
        self._slots: dict[int, object] = {}  # 项目号 → 此刻挂着等话的那个请求
        self._closed = False

    # ------------------------------------------------------------ 入队

    def enqueue(
        self,
        conn: sqlite3.Connection,
        project: Project,
        *,
        text: str,
        has_text: bool,
        at_ms: int,
        received_ms: int,
        via: str,
        context_token: str,
        window_id: int | None = None,
        upstream_key: str | None = None,
    ) -> Enqueued | QueueFull:
        """在调用者的事务里把一句话存成 `project` 的待取记录。提交以后调用者要调 `wake`。"""
        config = self._config
        now = self._clock.now_ms()
        dropped: str | None = None
        depth = conn.execute(
            "SELECT COUNT(*) FROM inbox WHERE project_id = ? AND state IN ('queued', 'leased')",
            (project.id,),
        ).fetchone()[0]
        if depth >= config.inbox.max_queued:
            oldest = conn.execute(
                "SELECT id FROM inbox WHERE project_id = ? AND state = 'queued'"
                " ORDER BY at_ms, rowid LIMIT 1",
                (project.id,),
            ).fetchone()
            if oldest is None:
                add_event(
                    conn,
                    now,
                    "queue",
                    f"项目 {project.name} 的队列里都是取走了还没确认的话，新来的一句没收",
                    level="WARNING",
                    project=project.name,
                )
                return QueueFull()
            conn.execute(
                "UPDATE inbox SET state = 'dropped', text = NULL, context_token = NULL"
                " WHERE id = ?",
                (oldest["id"],),
            )
            add_event(
                conn,
                now,
                "queue",
                f"项目 {project.name} 的队列满了（{config.inbox.max_queued} 条），丢掉最旧的一句"
                f"（{oldest['id']}）",
                level="WARNING",
                project=project.name,
            )
            dropped = oldest["id"]

        item_id = self._new_id(conn)
        handle = "rh_" + secrets.token_urlsafe(16)
        expires_ms = min(
            received_ms + config.reply.handle_minutes * _MINUTE_MS,
            at_ms + config.quota.window_hours * _HOUR_MS,
        )
        conn.execute(
            "INSERT INTO inbox (id, project_id, upstream_key, window_id, text, has_text, at_ms,"
            " received_ms, via, state, handle, context_token, handle_expires_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)",
            (
                item_id,
                project.id,
                upstream_key,
                window_id,
                text,
                int(has_text),
                at_ms,
                received_ms,
                via,
                handle,
                context_token,
                expires_ms,
            ),
        )
        # 这个项目收到了话：它之前在等的确认作废（交给别的项目的话不动它）
        conn.execute("UPDATE projects SET awaiting_until_ms = NULL WHERE id = ?", (project.id,))
        return Enqueued(item_id, dropped)

    def wake(self, project_id: int) -> None:
        """叫醒这个项目挂着的取话请求（入队的事务提交以后调）。"""
        condition = self._condition(project_id)
        with condition:
            self._generation[project_id] = self._generation.get(project_id, 0) + 1
            condition.notify_all()

    def _new_id(self, conn: sqlite3.Connection) -> str:
        while True:
            item_id = "m_" + secrets.token_hex(4)
            if conn.execute("SELECT 1 FROM inbox WHERE id = ?", (item_id,)).fetchone() is None:
                return item_id

    # ------------------------------------------------------------ 取话

    def fetch(self, project: Project, *, wait: int, max_items: int) -> list[Leased]:
        """取话：返回分给这个项目的话，每一条租出去 `inbox.lease_seconds` 秒。

        `wait` 秒内没有话就等（一有新话立刻返回；等待分成 5 秒一段，每段都再核对启用状态），
        `max_items` 为 0 只是心跳，立刻返回。项目被停用抛 `ProjectDisabled`（不交出任何话）。
        服务在关闭（`shutdown`）时挂着的请求马上返回空列表，不再租出话。
        调用者（接口层）负责把 `wait`、`max_items` 截到上限。"""
        if wait < 0 or max_items < 0:
            raise ValueError("wait 和 max_items 不能是负数")
        if max_items == 0:
            self._lease(project, 0, lease=False)
            return []
        token = self._take_slot(project.id) if wait > 0 else None
        deadline_ms = self._clock.now_ms() + wait * 1000
        try:
            while True:
                seen = self._seen(project.id)
                superseded = token is not None and self._slots.get(project.id) is not token
                closed = self._closed  # 一定要在 `seen` 之后读：`shutdown` 先置位、后叫醒
                items = self._lease(project, max_items, lease=not (superseded or closed))
                if items or superseded or closed:
                    return items
                left_ms = deadline_ms - self._clock.now_ms()
                if left_ms <= 0:
                    return []
                self._sleep(project.id, seen, min(SEGMENT_SECONDS, left_ms / 1000))
        finally:
            if token is not None:
                self._release_slot(project.id, token)

    def _lease(self, project: Project, max_items: int, *, lease: bool) -> list[Leased]:
        """一轮：核对启用状态、记下"刚取过话"，再（`lease` 为真时）租出话。"""
        config = self._config.inbox
        with self._db.txn() as conn:
            now = self._clock.now_ms()
            self._require_enabled(project)
            conn.execute("UPDATE projects SET last_inbox_ms = ? WHERE id = ?", (now, project.id))
            if not lease or max_items == 0:
                return []
            self._expire(conn, now, project.id)
            rows = conn.execute(
                "SELECT id, text, has_text, at_ms, received_ms, via, handle, attempts FROM inbox"
                " WHERE project_id = ? AND attempts < ?"
                " AND (state = 'queued' OR (state = 'leased' AND lease_until_ms <= ?))"
                " ORDER BY at_ms, rowid LIMIT ?",
                (project.id, config.max_attempts, now, max_items),
            ).fetchall()
            leased = []
            for row in rows:
                attempt = row["attempts"] + 1
                conn.execute(
                    "UPDATE inbox SET state = 'leased', lease_until_ms = ?, attempts = ?"
                    " WHERE id = ?",
                    (now + config.lease_seconds * 1000, attempt, row["id"]),
                )
                leased.append(
                    Leased(
                        id=row["id"],
                        text=row["text"] or "",
                        has_text=bool(row["has_text"]),
                        at_ms=row["at_ms"],
                        received_ms=row["received_ms"],
                        attempt=attempt,
                        via=row["via"],
                        reply_handle=row["handle"],
                    )
                )
            return leased

    def _require_enabled(self, project: Project) -> None:
        fresh = self._projects.get(project.name)
        if not fresh.enabled:  # 移除要先停用，所以已移除的也一定是停用的
            raise ProjectDisabled(fresh)

    def _expire(self, conn: sqlite3.Connection, now: int, project_id: int | None = None) -> int:
        """收到已经超过保留期、没人在处理的话标成"过期"，立即清正文；没租出去过的话连对话凭证
        一起清。租满了次数的话不在这里处理（由清理任务作废并记事件）。"""
        config = self._config.inbox
        sql = (
            "UPDATE inbox SET state = 'expired', text = NULL,"
            " context_token = CASE WHEN attempts = 0 THEN NULL ELSE context_token END"
            " WHERE state IN ('queued', 'leased') AND attempts < ?"
            " AND (state = 'queued' OR lease_until_ms <= ?) AND received_ms < ?"
        )
        args: list[int] = [config.max_attempts, now, now - config.retention_minutes * _MINUTE_MS]
        if project_id is not None:
            sql += " AND project_id = ?"
            args.append(project_id)
        return conn.execute(sql, args).rowcount

    # 挂着的请求：每个项目同一时间只留一个会等的

    def _condition(self, project_id: int) -> threading.Condition:
        with self._lock:
            return self._conditions.setdefault(project_id, threading.Condition())

    def _seen(self, project_id: int) -> int:
        with self._condition(project_id):
            return self._generation.get(project_id, 0)

    def _sleep(self, project_id: int, seen: int, seconds: float) -> None:
        """睡一段；`seen` 之后有人叫过（入队、顶掉）就不睡了，免得丢掉叫醒。"""
        condition = self._condition(project_id)
        with condition:
            if self._generation.get(project_id, 0) == seen:
                self._clock.wait(condition, seconds)

    def shutdown(self) -> None:
        """服务关闭：挂着的取话请求马上醒来、返回空列表，之后的取话也不再租出话（接口层在这之前
        就回 503，这里只是兜底）。"""
        self._closed = True
        with self._lock:
            project_ids = list(self._conditions)
        for project_id in project_ids:
            self.wake(project_id)

    def _take_slot(self, project_id: int) -> object:
        token = object()
        with self._lock:
            self._slots[project_id] = token
        self.wake(project_id)  # 旧的醒来，发现自己被顶掉了，返回空列表
        return token

    def _release_slot(self, project_id: int, token: object) -> None:
        with self._lock:
            if self._slots.get(project_id) is token:
                del self._slots[project_id]

    # ------------------------------------------------------------ 在线

    def online(self, project: Project) -> bool:
        """此刻有它的取话请求挂着，或者最近 `inbox.online_seconds` 秒内取过话 / 确认过 / 发过心跳。
        已停用（含已移除，移除要先停用）、只推送的项目永远不在线。`project` 是调用者手里的快照。"""
        if not project.enabled or project.push_only:
            return False
        with self._lock:
            if project.id in self._slots:
                return True
        return is_online(project, self._clock.now_ms(), self._config.inbox.online_seconds)

    def online_names(self, projects: Iterable[Project]) -> frozenset[str]:
        return frozenset(p.name for p in projects if self.online(p))

    # ------------------------------------------------------------ 确认

    def ack(self, project: Project, ids: Iterable[str]) -> Acked:
        """把租出去的话标成"已处理"并立即清正文；其他情形（已确认、已过期、已作废、别人的、
        不存在的）一律算 `unknown`，不报错，不说是哪一种。同一个 `id` 写了几次只算一次。"""
        acked = 0
        unknown: list[str] = []
        done: set[str] = set()
        with self._db.txn() as conn:
            now = self._clock.now_ms()
            conn.execute("UPDATE projects SET last_inbox_ms = ? WHERE id = ?", (now, project.id))
            for item_id in ids:
                if item_id in done:
                    continue
                done.add(item_id)
                changed = conn.execute(
                    "UPDATE inbox SET state = 'acked', text = NULL"
                    " WHERE id = ? AND project_id = ? AND state = 'leased'",
                    (item_id, project.id),
                ).rowcount
                if changed:
                    acked += 1
                else:
                    unknown.append(item_id)
        return Acked(acked, unknown)

    # ------------------------------------------------------------ 回复句柄

    def resolve_reply(self, project: Project, handle: str) -> ReplyTarget:
        """用句柄找到要回复的那句话。句柄不存在或属于别的项目抛 `UnknownHandle`；过期或对话凭证
        已清掉抛 `HandleExpired`（过期时顺手清掉凭证）；回复次数用完抛 `ReplyLimit`。"""
        config = self._config
        failure: HandleError | None = None
        target: ReplyTarget | None = None
        with self._db.txn() as conn:
            now = self._clock.now_ms()
            row = conn.execute("SELECT * FROM inbox WHERE handle = ?", (handle,)).fetchone()
            if row is None or row["project_id"] != project.id:
                failure = UnknownHandle()
            elif now >= row["handle_expires_ms"]:
                conn.execute("UPDATE inbox SET context_token = NULL WHERE id = ?", (row["id"],))
                if row["at_ms"] + config.quota.window_hours * _HOUR_MS <= now:
                    failure = HandleExpired(OLD_MESSAGE)
                else:
                    minutes = config.reply.handle_minutes
                    failure = HandleExpired(f"回复句柄已过期：话收到后 {minutes} 分钟内才能回复")
            elif row["context_token"] is None:
                failure = HandleExpired(HANDLE_CLEARED)
            elif row["replies"] >= config.reply.max_per_handle:
                left_ms = row["handle_expires_ms"] - now  # 过期的在上一个分支拦住了，所以大于 0
                failure = ReplyLimit(
                    f"这句话已经回复了 {row['replies']} 次，最多 {config.reply.max_per_handle} 次",
                    retry_after=-(-left_ms // 1000),
                )
            else:
                target = ReplyTarget(
                    item_id=row["id"],
                    context_token=row["context_token"],
                    at_ms=row["at_ms"],
                    replies=row["replies"],
                )
        if failure is not None:
            raise failure
        assert target is not None
        return target

    def count_reply(self, item_id: str) -> None:
        """一次回复成功（至少发出一条）以后记一次，用来限制同一个句柄的回复次数。"""
        with self._db.txn() as conn:
            conn.execute("UPDATE inbox SET replies = replies + 1 WHERE id = ?", (item_id,))

    # ------------------------------------------------------------ 等确认标记

    def mark_awaiting(self, project: Project, replied_id: str, until_ms: int) -> bool:
        """回复至少发出一条，并且带了 `awaiting_confirmation`：记下"等确认到 `until_ms`"。

        被回复的那句话之后，这个项目又收到过新的话就不记（"之后"按 `(at_ms, 入库先后)` 比较，
        与取话的排序一致）。返回有没有记。"""
        with self._db.txn() as conn:
            row = conn.execute(
                "SELECT at_ms, rowid AS seq FROM inbox WHERE id = ? AND project_id = ?",
                (replied_id, project.id),
            ).fetchone()
            if row is None:
                return False
            newer = conn.execute(
                "SELECT 1 FROM inbox WHERE project_id = ?"
                " AND (at_ms > ? OR (at_ms = ? AND rowid > ?)) LIMIT 1",
                (project.id, row["at_ms"], row["at_ms"], row["seq"]),
            ).fetchone()
            if newer is not None:
                return False
            conn.execute(
                "UPDATE projects SET awaiting_until_ms = ? WHERE id = ?", (until_ms, project.id)
            )
            return True

    def clear_awaiting(self, conn: sqlite3.Connection, names: Iterable[str]) -> None:
        """在调用者的事务里清掉这几个项目的等确认标记（路由发现它们已经不管用了）。"""
        names = list(names)
        if not names:
            return
        marks = ", ".join("?" for _ in names)
        conn.execute(f"UPDATE projects SET awaiting_until_ms = NULL WHERE name IN ({marks})", names)

    # ------------------------------------------------------------ 清理

    def cleanup(self) -> Cleaned:
        """每分钟一次（调度归 `scheduler`）：作废租满次数的话（记事件）、过期超过保留期的话、
        清失效句柄的对话凭证、删 7 天前的已处理记录和收消息的去重记录。"""
        config = self._config.inbox
        with self._db.txn() as conn:
            now = self._clock.now_ms()
            exhausted = conn.execute(
                "SELECT i.id, i.attempts, p.name FROM inbox i"
                " JOIN projects p ON p.id = i.project_id"
                " WHERE i.state = 'leased' AND i.lease_until_ms <= ? AND i.attempts >= ?",
                (now, config.max_attempts),
            ).fetchall()
            for row in exhausted:
                conn.execute(
                    "UPDATE inbox SET state = 'dropped', text = NULL WHERE id = ?", (row["id"],)
                )
                add_event(
                    conn,
                    now,
                    "queue",
                    f"一句话（{row['id']}）被取走 {row['attempts']} 次都没确认，作废了",
                    level="WARNING",
                    project=row["name"],
                )
            expired = self._expire(conn, now)
            tokens = conn.execute(
                "UPDATE inbox SET context_token = NULL"
                " WHERE context_token IS NOT NULL AND handle_expires_ms <= ?",
                (now,),
            ).rowcount
            keep_from = now - RECORD_KEEP_DAYS * _DAY_MS
            deleted = conn.execute(
                "DELETE FROM inbox WHERE state IN ('acked', 'expired', 'dropped')"
                " AND received_ms < ?",
                (keep_from,),
            ).rowcount
            seen = conn.execute("DELETE FROM inbound_seen WHERE seen_ms < ?", (keep_from,)).rowcount
        return Cleaned(
            expired=expired, dropped=len(exhausted), tokens=tokens, deleted=deleted, seen=seen
        )
