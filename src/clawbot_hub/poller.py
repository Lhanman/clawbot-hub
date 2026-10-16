"""收消息循环（任务 C3，规格 `hub-inbox`「收消息」「只认使用者本人」「一句话交给谁」
「项目没在线」，`hub-login`「登录失效」「登录后服务自动换上新凭证」）。

一个后台线程不停地向腾讯长轮询 `getupdates`。每一轮做的事都在 `step()` 里，它返回"下一轮前等
几秒"（0 = 马上再收），所以退避、探测间隔这些时间上的规矩不用起线程就能测；线程（`start` /
`stop`）只是反复调 `step()`，并在等的时候听 `LoginMonitor.wake`——换上新凭证就马上醒。

**一批消息**按规格的顺序处理，缺一不可：

1. 按消息自己的时间排序（没有时间用收到的时间；时间相同的保持原来的先后）；
2. 在一个数据库事务里：丢掉别人发的和机器人自己发的（只计数，不看内容）、丢掉没有对话凭证的
   （不能回复，只在日志里记数量）、按去重键排除已经收过的、给每条开窗口、路由、建待取记录、
   写限频记录、算好服务要回的话；
3. 事务提交以后：叫醒等着的取话请求，把服务要回的话交给 `Replier`，最后才把新的进度写回凭证
   文件（写回是"只有凭证还是这一轮用的那个才改"，见 `AccountStore.save_progress`）。

所以服务在两步之间被杀，重启后腾讯会再给一遍，去重键保证不会收两次。

**出错**：连不上、HTTP 状态不对、业务码不对都不是"没有新消息"，等 `upstream.backoff_start` 秒起
每次翻倍、最多 `upstream.backoff_max` 秒再收，收到正常的响应（含读超时）以后重新计。回 -14 按
`LoginMonitor.session_expired` 的判定：凭证已经换了就马上用新凭证再收；确认失效以后每
`upstream.relogin_probe_seconds` 秒探测一次，探测得到正常响应就恢复。

规格没写、这里定下的小约定：

- 长轮询的读超时：腾讯建议的时长比 `upstream.poll_timeout` 长才用"建议值加 10 秒"，否则还是
  `poll_timeout`；腾讯没再给建议就沿用上一次的，出错不改它；
- 没有凭证（没绑定、文件读不出来）时不收，隔 5 秒再看；换上凭证由 `wake` 马上叫醒；
- 别人的消息、机器人的消息在去重之前就丢了（不为了去重去算陌生人那句话的摘要），所以崩溃后
  重新送来的同一条陌生人消息会被多计一次；
- 收到 -14 本身是一次完成的往返（记 `last_upstream_ok_ms`），但凭证已经换了的那种不记成收消息错误；
- 关闭（`stop`）以后才回来的那一批不处理、进度不写回，下次启动腾讯再给一遍。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from collections.abc import Sequence

from clawbot_hub import hubtext
from clawbot_hub.account import AccountFile, AccountStore
from clawbot_hub.clock import Clock
from clawbot_hub.config import Config
from clawbot_hub.db import Database
from clawbot_hub.hubtext import ProjectLine, StatusInfo
from clawbot_hub.inbox import Inbox, QueueFull
from clawbot_hub.loginstate import LoginMonitor, Verdict, read_login
from clawbot_hub.projects import Project, Projects
from clawbot_hub.quota import Quota, closed_reason, window_ends_ms
from clawbot_hub.replier import Outgoing, Replier
from clawbot_hub.router import HubReply, Reply, Snapshot, ToProject, route
from clawbot_hub.upstream.base import (
    IncomingMessage,
    PollFailed,
    SessionExpired,
    Updates,
    Upstream,
)

logger = logging.getLogger(__name__)

# 没有凭证时隔多久再看一次（固定常量；换上凭证由 `wake` 马上叫醒，这只是兜底）
IDLE_SECONDS = 5.0
# 腾讯建议的长轮询时长再加上这么多秒，才是我们这边的读超时（规格定的）
HINT_MARGIN_SECONDS = 10
# `stop` 最多等收消息线程多久（真实秒）：长轮询最长 `upstream.poll_timeout` 加十来秒
JOIN_SECONDS = 150.0

# 腾讯标明是机器人自己发的消息
BOT_MESSAGE_TYPE = 2
EXPIRED_ERROR = "登录失效（-14）"


def upstream_key(message: IncomingMessage) -> str:
    """去重键：腾讯给的 `message_id`；没有就用 `seq`；再没有就用"时间、对话凭证、文字"的摘要。
    前缀把三种来源分开，`id` 是 `7` 的和 `seq` 是 7 的不会撞。"""
    if message.message_id:
        return f"id:{message.message_id}"
    if message.seq is not None:
        return f"seq:{message.seq}"
    raw = json.dumps([message.at_ms, message.context_token, message.text], ensure_ascii=False)
    return "h:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Poller:
    def __init__(
        self,
        config: Config,
        clock: Clock,
        db: Database,
        upstream: Upstream,
        store: AccountStore,
        monitor: LoginMonitor,
        projects: Projects,
        quota: Quota,
        inbox: Inbox,
        replier: Replier,
    ) -> None:
        self._config = config
        self._clock = clock
        self._db = db
        self._upstream = upstream
        self._store = store
        self._monitor = monitor
        self._projects = projects
        self._quota = quota
        self._inbox = inbox
        self._replier = replier
        self._digest: str | None = None  # 正在用的凭证的摘要
        self._buf = ""  # 下一次收消息带的进度
        self._backoff_seconds = 0.0  # 当前的退避时长，0 = 没在退避
        self._hint_seconds: float | None = None  # 腾讯最近一次建议的长轮询时长
        self._thread: threading.Thread | None = None
        self._stopping = False

    # ------------------------------------------------ 一轮

    def step(self) -> float:
        """收一轮，返回下一轮前要等几秒（0 = 马上再收）。"""
        account = self._monitor.account()
        if account is None:
            account = self._monitor.sync().account
            if account is None:
                return IDLE_SECONDS
        if account.digest != self._digest:
            self._adopt(account)
        probing = self._monitor.login().state == "expired"

        outcome = self._upstream.get_updates(account.to_account(), self._buf, self._read_timeout())

        if self._stopping:
            return IDLE_SECONDS  # 关闭了：这一批不处理，进度没写回，下次启动腾讯再给一遍
        if isinstance(outcome, Updates):
            return self._on_updates(account, outcome, probing)
        if isinstance(outcome, SessionExpired):
            return self._on_session_expired(account)
        return self._on_failed(outcome, probing)

    def _adopt(self, account: AccountFile) -> None:
        """换用这份凭证：进度从它的文件里来，退避重新计。"""
        self._digest = account.digest
        self._buf = account.updates_buf
        self._backoff_seconds = 0.0

    def _read_timeout(self) -> float:
        configured = self._config.upstream.poll_timeout
        hint = self._hint_seconds
        if hint is not None and hint > configured:
            return hint + HINT_MARGIN_SECONDS
        return configured

    def _backoff(self) -> float:
        cfg = self._config.upstream
        grown = cfg.backoff_start if self._backoff_seconds == 0 else self._backoff_seconds * 2
        self._backoff_seconds = min(grown, cfg.backoff_max)
        return self._backoff_seconds

    @property
    def _probe_seconds(self) -> float:
        return self._config.upstream.relogin_probe_seconds

    # ------------------------------------------------ 收到了正常的响应

    def _on_updates(self, account: AccountFile, updates: Updates, probing: bool) -> float:
        if updates.longpolling_timeout_ms is not None:
            self._hint_seconds = updates.longpolling_timeout_ms / 1000
        if probing:
            self._monitor.probe_finished(updates)  # 恢复了就把状态变回正常
            if self._monitor.login().state == "expired":
                self._record_ok()
                return self._probe_seconds  # 没证明恢复（读超时）：下一次探测在一个间隔以后
        self._backoff_seconds = 0.0

        now = self._clock.now_ms()
        outgoing, woken = self._store_batch(account, updates.messages, now)

        # 事务提交以后才做下面这些
        for project_id in woken:
            self._inbox.wake(project_id)
        for reply in outgoing:
            self._replier.submit(reply)
        if updates.buf is not None:
            self._buf = updates.buf
            self._save_progress(account, updates.buf)
        return 0.0

    def _save_progress(self, account: AccountFile, buf: str) -> None:
        try:
            self._store.save_progress(account.bot_token, buf)
        except Exception as exc:  # noqa: BLE001 - 写不回去也不能丢掉这一批的后续处理
            logger.error("收消息的进度写不回凭证文件：%s", type(exc).__name__)

    def _record_ok(self) -> None:
        now = self._clock.now_ms()
        with self._db.txn() as conn:
            self._write_ok(conn, now)

    @staticmethod
    def _write_ok(conn: sqlite3.Connection, now: int) -> None:
        conn.execute(
            "UPDATE account_state SET last_poll_ok_ms = ?, last_upstream_ok_ms = ? WHERE id = 1",
            (now, now),
        )

    # ------------------------------------------------ 一批消息

    def _store_batch(
        self, account: AccountFile, messages: Sequence[IncomingMessage], now: int
    ) -> tuple[list[Outgoing], list[int]]:
        """一个事务：存下这一批，返回服务要回的话和要叫醒的项目。"""
        ordered = sorted(messages, key=lambda m: now if m.at_ms is None else m.at_ms)
        outgoing: list[Outgoing] = []
        woken: list[int] = []
        received: list[tuple[int, str | None]] = []  # (字数, 交给哪个项目；服务自己回复是 None)
        ignored = no_token = 0
        with self._db.txn() as conn:
            self._write_ok(conn, now)
            for message in ordered:
                if not _from_the_owner(message, account.user_id):
                    ignored += 1
                    continue
                if not message.context_token:
                    no_token += 1
                    continue
                key = upstream_key(message)
                new = conn.execute(
                    "INSERT OR IGNORE INTO inbound_seen (upstream_key, seen_ms) VALUES (?, ?)",
                    (key, now),
                )
                if new.rowcount == 0:
                    continue
                target = self._accept(conn, message, key, now, outgoing, woken)
                received.append((len(message.text), target))
            if ignored:
                conn.execute(
                    "UPDATE account_state SET ignored_count = ignored_count + ? WHERE id = 1",
                    (ignored,),
                )
        for chars, target in received:  # 只写字数和去向，不写内容
            if target is None:
                logger.info("收到一条消息，%d 个字，服务自己回复", chars)
            else:
                logger.info("收到一条消息，%d 个字，交给 %s", chars, target)
        if ignored:
            logger.info("丢弃了 %d 条不是使用者本人发的消息（陌生人或机器人）", ignored)
        if no_token:
            logger.info("%d 条消息没有对话凭证，不能回复，没有收", no_token)
        return outgoing, woken

    def _accept(
        self,
        conn: sqlite3.Connection,
        message: IncomingMessage,
        key: str,
        now: int,
        outgoing: list[Outgoing],
        woken: list[int],
    ) -> str | None:
        """使用者本人的一条新消息：开窗口、路由、入队或让服务回一句。返回入队交给的项目名，
        服务自己回复（含项目的队列满了没收下）返回 `None`。"""
        token = message.context_token or ""  # 调用之前已经排除了没有凭证的
        at_ms = now if message.at_ms is None else message.at_ms
        window_id = self._quota.open_window(conn, at_ms, token)

        projects = self._projects.list()
        online = self._inbox.online_names(projects)
        snapshot = Snapshot(
            tuple(projects),
            online,
            self._config.hub.label,
            self._config.routing.confirm_words,
        )
        decision = route(message.text, message.has_text, snapshot, now)
        self._inbox.clear_awaiting(conn, decision.clear_awaiting)
        target: str | None = None

        if isinstance(decision, ToProject):
            project = decision.project
            queued = self._inbox.enqueue(
                conn,
                project,
                text=decision.text,
                has_text=decision.has_text,
                at_ms=at_ms,
                received_ms=now,
                via=decision.via,
                context_token=token,
                window_id=window_id,
                upstream_key=key,
            )
            if isinstance(queued, QueueFull):
                text = self._limited(
                    conn, f"full:{project.name}", now, hubtext.full_text(project.display)
                )
            else:
                target = project.name
                woken.append(project.id)
                text = None
                if project.name not in online:
                    text = self._limited(
                        conn,
                        f"offline:{project.name}",
                        now,
                        hubtext.offline_text(project.display, self._config.inbox.retention_minutes),
                    )
        else:
            text = self._hub_text(conn, decision, now, projects, online)
        if text is not None:
            outgoing.append(Outgoing(token, text))
        return target

    def _hub_text(
        self,
        conn: sqlite3.Connection,
        decision: HubReply,
        now: int,
        projects: Sequence[Project],
        online: frozenset[str],
    ) -> str | None:
        """服务自己要回的话（被限频的那一次返回 `None`）。"""
        label = self._config.hub.label
        kind = decision.kind
        if kind is Reply.PROJECTS:
            return hubtext.projects_text(_lines(projects, online))
        if kind is Reply.STATUS:
            return hubtext.status_text(self._status_info(conn, projects, online))
        if kind is Reply.UNKNOWN_COMMAND:
            return hubtext.unknown_command_text(label)
        if kind is Reply.CONFIRM_AMBIGUOUS:
            displays = [project.display for project in decision.awaiting]
            return hubtext.ambiguous_text(displays, decision.word)
        if kind is Reply.NO_DEFAULT:
            return self._limited(conn, "nodefault", now, hubtext.no_default_text(label))
        assert decision.project is not None  # DISABLED 一定带着那个项目
        return self._limited(
            conn,
            f"disabled:{decision.project.name}",
            now,
            hubtext.disabled_text(decision.project.display),
        )

    def _limited(self, conn: sqlite3.Connection, key: str, now: int, text: str) -> str | None:
        """同一种提示对同一个目标在 `inbox.offline_notice_seconds` 秒内最多回一次：到点了返回
        文字并记下这一刻，限住的返回 `None`（不顺延上次的时间）。库里的时间比现在还晚（时钟被
        调慢过）算到点。"""
        row = conn.execute("SELECT last_ms FROM notice_state WHERE key = ?", (key,)).fetchone()
        period_ms = self._config.inbox.offline_notice_seconds * 1000
        if row is not None and 0 <= now - row["last_ms"] < period_ms:
            return None
        conn.execute(
            "INSERT INTO notice_state (key, last_ms) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET last_ms = excluded.last_ms",
            (key, now),
        )
        return text

    # ------------------------------------------------ 出错、登录失效

    def _on_failed(self, outcome: PollFailed, probing: bool) -> float:
        now = self._clock.now_ms()
        with self._db.txn() as conn:
            conn.execute(
                "UPDATE account_state SET last_poll_error = ?, last_poll_error_ms = ? WHERE id = 1",
                (outcome.reason, now),
            )
            if outcome.completed:
                conn.execute(
                    "UPDATE account_state SET last_upstream_ok_ms = ? WHERE id = 1", (now,)
                )
        return self._probe_seconds if probing else self._backoff()

    def _on_session_expired(self, account: AccountFile) -> float:
        verdict = self._monitor.session_expired(account.digest)
        now = self._clock.now_ms()
        with self._db.txn() as conn:
            conn.execute("UPDATE account_state SET last_upstream_ok_ms = ? WHERE id = 1", (now,))
            if verdict is not Verdict.CHANGED:
                conn.execute(
                    "UPDATE account_state SET last_poll_error = ?, last_poll_error_ms = ?"
                    " WHERE id = 1",
                    (EXPIRED_ERROR, now),
                )
        if verdict is Verdict.CHANGED:
            self._monitor.sync()  # 凭证已经换了：马上用新的再收
            return 0.0
        if verdict is Verdict.EXPIRED:
            return self._probe_seconds
        return self._backoff()  # 正在扫码、刚换过凭证：当作一次普通的错误

    # ------------------------------------------------ 状态

    def status_info(self) -> StatusInfo:
        """`服务 状态` 的数据：登录、窗口、各项目在不在线。"""
        projects = self._projects.list()
        online = self._inbox.online_names(projects)
        return self._status_info(self._db.conn(), projects, online)

    def _status_info(
        self, conn: sqlite3.Connection, projects: Sequence[Project], online: frozenset[str]
    ) -> StatusInfo:
        cfg = self._config.quota
        now = self._clock.now_ms()
        window = self._quota.current_window(conn)
        reason = closed_reason(window, now, cfg)
        lines = _lines(projects, online)
        login = read_login(conn).state
        if window is None or reason is not None:
            state = "none" if window is None else "closed"
            return StatusInfo(login, state, projects=lines)
        used = self._quota.read_state(conn).used
        return StatusInfo(
            login,
            "open",
            remaining_seconds=(window_ends_ms(window, cfg) - now) / 1000,
            used=used,
            limit=cfg.window_limit,
            projects=lines,
        )

    # ------------------------------------------------ 线程

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def stopping(self) -> bool:
        return self._stopping

    def start(self) -> None:
        if self.running:
            raise RuntimeError("收消息线程已经在运行")
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name="hub-poller", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = JOIN_SECONDS) -> None:
        """停下收消息线程：睡着的马上醒；正在长轮询的等它回来，回来的这一批不处理。
        没启动、重复停都没关系。"""
        self._stopping = True
        self._monitor.wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self) -> None:
        while True:
            # 先清、再看有没有叫停、再收：收的时候换了凭证或者 `stop` 来了，下面的等待马上就醒；
            # `stop` 先置标志、后叫醒，所以它在清之前到，标志拦得住，在清之后到，叫醒留得住
            self._monitor.wake.clear()
            if self._stopping:
                return
            try:
                delay = self.step()
            except Exception as exc:  # noqa: BLE001 - 收消息线程不能死
                logger.error("收消息出错：%s", type(exc).__name__)
                delay = self._backoff()
            self._monitor.wake.wait(self._clock, delay)  # 等 0 秒只是看一眼


def _from_the_owner(message: IncomingMessage, owner: str) -> bool:
    """使用者本人发的，并且不是腾讯标明的机器人自己发的。"""
    return message.from_user_id == owner and message.message_type != BOT_MESSAGE_TYPE


def _lines(projects: Sequence[Project], online: frozenset[str]) -> tuple[ProjectLine, ...]:
    """列给使用者看的项目：启用着、能收话的（只推送的、已停用的不列）。"""
    return tuple(
        ProjectLine(project.display, project.is_default, project.name in online)
        for project in projects
        if project.enabled and not project.push_only and not project.removed
    )
