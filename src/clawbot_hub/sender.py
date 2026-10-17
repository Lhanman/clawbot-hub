"""发送：发消息（推送）和回复的状态机。

一次发送的流程（规格 `hub-send`，设计第 6 节）：

1. 校验字段（`SendInvalid`，接口回 400）；加标签、拆条（纯计算）；
2. **一个 `BEGIN IMMEDIATE` 事务**：先让到期的去重键失效 → 去重键命中就返回原来的结果 → 插发送记录
   （同时占着键）→ `too_long` → 登录状态 → 窗口 / 暂停 / 额度（`Quota.reserve`，通过就预占）。
   任何一步不通过都把这条记录改成 `rejected` / `failed` / `skipped` 并写原因（键随之释放），提交；
   通过的也提交，然后才去拿发送锁。所以被拒的请求不排队，也不会留下"发送中却没有预占"的记录；
3. 拿发送锁（同一时间只有一条消息在向腾讯发，几段连续、不同项目的消息不穿插），等锁和发请求共用
   一份时间预算 `send.timeout_seconds`；
4. 逐条发：每条向腾讯发一次 `sendmessage`（每次新连接是适配层的事），按结果判定，**发完一条就在
   自己的小事务里对账一条**（`Quota.settle`，连同发送记录里已发出的条数）；一条没发出去、或者拿不准
   到没到，后面的就不发了；
5. 汇总各条 → 整条的状态 → 写发送记录 → 返回。

**重试策略在这里，不在适配层**：只有"请求没发出去"（`Transient`）才重试，最多 `send.retries`
次，间隔 1 秒起每次翻倍，用同一个 `client_id`；"发出去了不知道到没到"（`NoResponse`）和明确拒绝
都不重试。

服务自己的回复（`_hub`）和项目的回复走 `reply`：用调用者给的对话凭证（被回复那句话的），条数记进
"回复"一类；没有 `on_closed`、不被腾讯拒绝后的暂停拦住、`too_long` 的上限是窗口总条数。

**规格没写、这里定下的约定**（任务单 B1 的实现说明里也有）：

- 发送记录在每一条对账的同一个事务里更新已发出的条数（`parts_sent`、`parts_unconfirmed`、
  `no_message_id`），所以服务被杀以后"中断"的记录至少能说出已经确认了几条；
- 与腾讯完成了一次往返（`Accepted`、`Rejected`、`SessionExpired`，即收到了完整的响应且不是
  429 / 5xx）就在对账的事务里写 `account_state.last_upstream_ok_ms`（`upstream_down` 告警的依据，
  "收和发不分"）；
- `no_message_id`（发送记录里"腾讯没给消息编号的请求数"）在 `observe` 和 `strict` 档都记，
  `off` 档不记；
- 发到一半内部出错（抛出意外）：已经向腾讯发过请求的几条（包括出错时正在发的那一条，不知道到没到）
  额度保留，还没开始发的退回；碰过腾讯的记录标成"中断"（占着去重键），没碰过的标成 `failed`
  （原因代码 `internal`，只写在记录里，接口给的是 500）、额度全部退回、键释放。出错以后原样把异常
  抛给调用者，由接口层回 500 并记日志；
- "重试 n 次后仍然失败"里的 n 是实际重试的次数（预算不够重试时可能是 0，不是配置的次数）；
- 重试之前先看剩下的预算够不够下一次开始（睡完 1 秒、2 秒以后还剩不到 1 秒的，不睡了直接用上一次的
  失败结束），效果与"睡完再检查"相同，只是不白睡；
- 推送发出前再核对一次项目还启用着（认人以后到这里之间被停用了就抛 `ProjectDisabled`，什么也不留）；
- 每条被拒绝 / 失败 / 未确认 / 部分 / 中断的发送写一条 `send` 事件，内容只有编号、状态、原因代码和
  原因文字，没有正文；
- 发送可以通过 `shutdown()` 让还在等发送锁的请求马上以 `busy` 结束（服务关闭时用）；
- 每个 `sendmessage` 请求结束后，按"有没有确认送达"通知 `on_request`（`hub-alerts` 的
  `send_unconfirmed` 按请求数连续计数，计数和告警归阶段 D）：腾讯给了消息编号是 `CONFIRMED`；
  腾讯回了成功但没有消息编号（`observe`、`strict` 档）或者发出后没有回话是 `UNCONFIRMED`；
  明确拒绝、连不上、登录失效、`off` 档下没有编号的请求都不通知（既不计数也不归零）。
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import math
import secrets
import sqlite3
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import tzinfo
from typing import Protocol

from clawbot_hub.clock import Clock
from clawbot_hub.config import Config
from clawbot_hub.db import Database
from clawbot_hub.events import add_event
from clawbot_hub.loginstate import LOGIN_REASONS, LoginMonitor, read_login
from clawbot_hub.projects import Project, ProjectDisabled
from clawbot_hub.quota import (
    INTERRUPTED_CODE,
    INTERRUPTED_TEXT,
    PRIORITIES,
    PUSH,
    REPLY,
    Deny,
    Hold,
    PartResult,
    Quota,
    Request,
    Reservation,
    closed_reason,
    window_ends_ms,
)
from clawbot_hub.quota import left as quota_left
from clawbot_hub.textutil import add_label, build_parts, is_encodable, mask
from clawbot_hub.timefmt import moment_text
from clawbot_hub.upstream.base import (
    Accepted,
    Account,
    NoResponse,
    Rejected,
    SessionExpired,
    Transient,
    Upstream,
)

logger = logging.getLogger(__name__)

ON_CLOSED = ("fail", "skip")
DEDUPE_KEY_MAX_CHARS = 128

# 向腾讯发每一次请求之前，剩下的预算不足这么久（毫秒）就不再开始（固定常量，见设计）
MIN_START_MS = 1_000
# 重试的间隔从这么多秒起，每次翻倍
RETRY_FIRST_SECONDS = 1

# 占着去重键的发送记录状态；其余（rejected、failed、skipped）一变成就释放
OCCUPYING = ("sending", "sent", "unconfirmed", "partial", "interrupted")
_OCCUPYING_SQL = ", ".join(f"'{status}'" for status in OCCUPYING)

# `on_request` 收到的两种分类
CONFIRMED = "confirmed"
UNCONFIRMED = "unconfirmed"

RECOVER = "给 ClawBot 发任意一句话就能恢复"
NEVER_TEXT = "还没收到你在微信里发的消息：给 ClawBot 发任意一句话就能开始接收"
NO_RESPONSE_TEXT = "微信 ClawBot 没有回话，这条可能已经到了，不要重发"
NO_MESSAGE_ID_TEXT = "微信 ClawBot 没有返回消息编号，这条可能没有到"
BUSY_TEXT = "服务正忙，这条没有发，稍后再试"
IN_FLIGHT_TEXT = "同一个去重键的上一次发送还没结束，先不要重发"
INTERNAL_TEXT = "服务内部出错，这条没有发出"


# ---------------------------------------------------------------- 数据


class SendInvalid(Exception):
    """字段校验没过（接口回 400）。`code` 是 `bad_request`、`empty_text` 或 `text_too_long`；
    `message` 是一句中文，不含请求里的内容。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Author(Protocol):
    """发消息的一方：项目（`projects.Project`）或服务自己。"""

    @property
    def name(self) -> str: ...

    @property
    def label(self) -> str | None: ...


@dataclass(frozen=True)
class Origin:
    """服务自己（`_hub`）的回复用它当作者；测试里也可以随手造一个。"""

    name: str
    label: str | None = None


@dataclass(frozen=True)
class QuotaView:
    """发完以后调用方自己能用的余量：`left` 是一条 `normal` 的推送现在最多能拆成几条发出去，
    `limit` 是窗口总条数，`window_ends_ms` 在窗口没开时是 `None`。"""

    left: int
    limit: int
    window_ends_ms: int | None


@dataclass(frozen=True)
class SendResult:
    id: str
    status: str  # sent / unconfirmed / partial / rejected / failed / skipped
    reason_code: str | None
    reason: str | None
    parts: int
    parts_sent: int
    duplicate: bool
    quota: QuotaView


@dataclass
class _Part:
    """一条（一段）发出去以后的结局。`status`：`sent`、`unconfirmed`（发出了但拿不准）、`failed`
    （没发出去）；`stop` 为真表示后面的段不再发。"""

    status: str
    code: str | None = None
    reason: str | None = None
    stop: bool = False


@dataclass
class _Run:
    """一次发送的现场（只有发送它的那个线程碰）。"""

    id: str
    kind: str
    name: str
    pieces: list[str]
    deadline_ms: int
    reservation: Reservation
    account: Account
    digest: str
    context_token: str
    to_mask: tuple[str, ...]
    results: list[_Part] = field(default_factory=list)
    settled: int = 0  # 已经对过账的条数（含退回的）
    in_flight: bool = False  # 有一条的请求发出去了、还没对账
    touched: bool = False  # 至少调过一次腾讯
    sent: int = 0
    unconfirmed: int = 0
    no_id: int = 0
    timed_out_from: int | None = None  # 预算用完时第一条没来得及发的是第几条（从 0 起）


@dataclass(frozen=True)
class _Duplicate:
    """去重键被占着：占着它的那条发送记录的编号。"""

    id: str


# ---------------------------------------------------------------- 校验


def check_text(text: object, max_chars: int) -> str:
    if not isinstance(text, str):
        raise SendInvalid("bad_request", "text 要是字符串")
    if not is_encodable(text):
        raise SendInvalid("bad_request", "text 里有不能编码成 UTF-8 的字符")
    if not text.strip():
        raise SendInvalid("empty_text", "文字不能是空的")
    if len(text) > max_chars:
        raise SendInvalid("text_too_long", f"文字太长：最多 {max_chars} 个字")
    return text


def check_push(
    text: object, priority: object, dedupe_key: object, on_closed: object, max_chars: int
) -> None:
    check_text(text, max_chars)
    if priority not in PRIORITIES:
        raise SendInvalid("bad_request", f"priority 的值不对，可选：{'、'.join(PRIORITIES)}")
    if dedupe_key is not None and not (
        isinstance(dedupe_key, str)
        and 1 <= len(dedupe_key) <= DEDUPE_KEY_MAX_CHARS
        and is_encodable(dedupe_key)
    ):
        raise SendInvalid(
            "bad_request", f"dedupe_key 要是 1 到 {DEDUPE_KEY_MAX_CHARS} 个字符的字符串"
        )
    if on_closed not in ON_CLOSED:
        raise SendInvalid("bad_request", f"on_closed 的值不对，可选：{'、'.join(ON_CLOSED)}")


def dedupe_digest(project: str, key: str) -> str:
    """去重键在库里的样子：加项目名当盐的 SHA-256，库里没有原文。"""
    return hashlib.sha256(f"{project}\x00{key}".encode()).hexdigest()


# ---------------------------------------------------------------- 发送锁


class _SendLock:
    """一把能按时钟限时等待的锁（`threading.Lock` 的限时等待走真实时间，假时钟管不到）。
    `close()` 以后，还在等的和之后新来的都马上放弃（锁空着也一样）。"""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._held = False
        self._closed = False

    def acquire(self, clock: Clock, deadline_ms: int) -> bool:
        with self._cond:
            if self._closed:
                return False
            while self._held:
                left_ms = deadline_ms - clock.now_ms()
                if left_ms <= 0 or self._closed:
                    return False
                clock.wait(self._cond, left_ms / 1000)
            self._held = True
            return True

    def release(self) -> None:
        with self._cond:
            self._held = False
            self._cond.notify()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()


# ---------------------------------------------------------------- 发送


class Sender:
    """发消息和回复。可以被几个线程同时用：额度判定在数据库事务里，向腾讯发请求有一把发送锁。

    `on_hold(hold)`：腾讯明确拒绝了一条推送、窗口进入暂停时调用（给告警用，出错只记日志）。
    `on_request(kind)`：一个 `sendmessage` 请求结束后调用，`kind` 是 `CONFIRMED` 或 `UNCONFIRMED`
    （见模块说明；出错只记日志）。
    `new_id`、`new_client_id`：造发送记录编号和 `client_id` 的函数，测试里换成确定的。"""

    def __init__(
        self,
        db: Database,
        config: Config,
        clock: Clock,
        upstream: Upstream,
        monitor: LoginMonitor,
        *,
        quota: Quota | None = None,
        on_hold: Callable[[Hold], None] | None = None,
        on_request: Callable[[str], None] | None = None,
        new_id: Callable[[], str] | None = None,
        new_client_id: Callable[[], str] | None = None,
    ) -> None:
        self._db = db
        self._cfg = config
        self._clock = clock
        self._upstream = upstream
        self._monitor = monitor
        self._quota = quota or Quota(config, clock)
        self._on_hold = on_hold
        self._on_request = on_request
        self._new_id = new_id or (lambda: "o_" + secrets.token_hex(4))
        self._new_client_id = new_client_id or (lambda: str(uuid.uuid4()))
        self._tz: tzinfo = config.display.tzinfo
        self._timeout_ms = config.send.timeout_seconds * 1000
        self._lock = _SendLock()
        self._finished = threading.Condition()  # 有一次发送收尾了：等同一个去重键的请求在这里等

    # ------------------------------------------------ 对外

    def push(
        self,
        project: Author,
        text: str,
        *,
        priority: str = "normal",
        dedupe_key: str | None = None,
        on_closed: str = "fail",
    ) -> SendResult:
        """项目推送一条消息。字段不对抛 `SendInvalid`，项目已被停用抛 `ProjectDisabled`，
        服务内部出错原样抛出（见模块说明）。"""
        started_ms = self._clock.now_ms()
        check_push(text, priority, dedupe_key, on_closed, self._cfg.send.max_chars)
        return self._send(
            PUSH,
            project,
            text,
            started_ms=started_ms,
            priority=priority,
            dedupe_key=dedupe_key,
            on_closed=on_closed,
            context_token=None,
        )

    def reply(self, author: Author, text: str, *, context_token: str) -> SendResult:
        """回复使用者的一句话：用 `context_token`（被回复那句话的）发。"""
        started_ms = self._clock.now_ms()
        check_text(text, self._cfg.send.max_chars)
        if not context_token:
            raise ValueError("回复要带对话凭证")
        return self._send(
            REPLY,
            author,
            text,
            started_ms=started_ms,
            priority=None,
            dedupe_key=None,
            on_closed="fail",
            context_token=context_token,
        )

    def shutdown(self) -> None:
        """服务关闭：还在等发送锁的请求马上以 `failed` / `busy` 结束，之后新来的也一样（接口层
        在这之前就回 503 `shutting_down`，这里只是兜底）；正在发的那条照常发完。"""
        self._lock.close()

    # ------------------------------------------------ 主流程

    def _send(
        self,
        kind: str,
        author: Author,
        text: str,
        *,
        started_ms: int,
        priority: str | None,
        dedupe_key: str | None,
        on_closed: str,
        context_token: str | None,
    ) -> SendResult:
        quota_cfg = self._cfg.quota
        deadline_ms = started_ms + self._timeout_ms
        labelled = add_label(text, author.label or "")
        pieces = build_parts(labelled, self._cfg.send.part_chars, quota_cfg.window_limit)
        digest = None if dedupe_key is None else dedupe_digest(author.name, dedupe_key)

        admitted = self._admit(
            kind,
            author,
            pieces,
            chars=len(text),
            priority=priority,
            digest=digest,
            on_closed=on_closed,
            context_token=context_token,
            deadline_ms=deadline_ms,
        )
        if isinstance(admitted, SendResult):
            return admitted
        if isinstance(admitted, _Duplicate):
            return self._replay(author.name, admitted.id, deadline_ms)

        run = admitted
        try:
            if not self._lock.acquire(self._clock, deadline_ms):
                return self._busy(run)
            try:
                self._transmit(run)
            finally:
                self._lock.release()
            return self._finalize(run)
        except Exception:
            self._abort(run)
            raise
        finally:
            with self._finished:
                self._finished.notify_all()

    # ------------------------------------------------ 第一步：一个事务里判定

    def _admit(
        self,
        kind: str,
        author: Author,
        pieces: list[str],
        *,
        chars: int,
        priority: str | None,
        digest: str | None,
        on_closed: str,
        context_token: str | None,
        deadline_ms: int,
    ) -> SendResult | _Duplicate | _Run:
        quota_cfg = self._cfg.quota
        name, parts = author.name, len(pieces)
        cap = quota_cfg.window_limit - (quota_cfg.reply_reserve if kind == PUSH else 0)
        with self._db.txn() as conn:
            now = self._clock.now_ms()
            project = conn.execute(
                "SELECT id, enabled FROM projects WHERE name = ?", (name,)
            ).fetchone()
            if project is None:
                raise ValueError(f"项目 {name} 不存在")
            if kind == PUSH and not project["enabled"]:
                if isinstance(author, Project):
                    raise ProjectDisabled(dataclasses.replace(author, enabled=False))
                raise ValueError(f"项目 {name} 没有启用")
            self._expire_keys(conn, now)
            if digest is not None:
                taken = conn.execute(
                    "SELECT id FROM outbox WHERE project_id = ? AND dedupe_hash = ?"
                    f" AND status IN ({_OCCUPYING_SQL})",
                    (project["id"], digest),
                ).fetchone()
                if taken is not None:
                    return _Duplicate(taken["id"])

            outbox_id = self._fresh_id(conn)
            conn.execute(
                "INSERT INTO outbox (id, project_id, kind, priority, dedupe_hash, parts, status,"
                " chars, created_ms) VALUES (?, ?, ?, ?, ?, ?, 'sending', ?, ?)",
                (outbox_id, project["id"], kind, priority, digest, parts, chars, now),
            )

            def refuse(status: str, code: str, reason: str) -> SendResult:
                return self._refuse(conn, outbox_id, name, parts, status, code, reason, now)

            if parts > cap:
                return refuse(
                    "rejected",
                    "too_long",
                    f"这条要拆成 {parts} 条，超过这个窗口最多能发的 {cap} 条",
                )
            login = read_login(conn).state
            account = self._monitor.account()
            if login != "ok" or account is None:
                reason = LOGIN_REASONS[login if login != "ok" else "missing"]
                return refuse("failed", reason.code, reason.text)

            request = Request(name, n=parts, kind=kind, priority=priority or "normal")
            reservation = self._quota.reserve(conn, request, outbox_id=outbox_id)
            if isinstance(reservation, Deny):
                closed = reservation.code in ("never_contacted", "window_closed")
                status = (
                    "skipped" if kind == PUSH and on_closed == "skip" and closed else "rejected"
                )
                return refuse(status, reservation.code, self._deny_text(reservation, name))

            token = reservation.context_token if kind == PUSH else context_token
            if not token:
                raise RuntimeError("窗口里没有对话凭证，发不了")
            return _Run(
                id=outbox_id,
                kind=kind,
                name=name,
                pieces=pieces,
                deadline_ms=deadline_ms,
                reservation=reservation,
                account=account.to_account(),
                digest=account.digest,
                context_token=token,
                to_mask=(account.bot_token, token),
            )

    def _expire_keys(self, conn: sqlite3.Connection, now: int) -> None:
        """到期的去重键失效（发送记录本身留着）：现在 ≥ 建立时刻 + `send.dedupe_hours`。"""
        hours_ms = self._cfg.send.dedupe_hours * 3_600_000
        conn.execute(
            "UPDATE outbox SET dedupe_hash = NULL"
            " WHERE dedupe_hash IS NOT NULL AND created_ms <= ?",
            (now - hours_ms,),
        )

    def _fresh_id(self, conn: sqlite3.Connection) -> str:
        for _ in range(100):
            candidate = self._new_id()
            if conn.execute("SELECT 1 FROM outbox WHERE id = ?", (candidate,)).fetchone() is None:
                return candidate
        raise RuntimeError("造不出没用过的发送编号")

    def _refuse(
        self,
        conn: sqlite3.Connection,
        outbox_id: str,
        name: str,
        parts: int,
        status: str,
        code: str,
        reason: str,
        now: int,
    ) -> SendResult:
        """判定没过：这条记录一次写成终态（键随之释放），写事件，返回结果。"""
        self._close_row(conn, outbox_id, name, status, code, reason, now)
        return SendResult(
            id=outbox_id,
            status=status,
            reason_code=code,
            reason=reason,
            parts=parts,
            parts_sent=0,
            duplicate=False,
            quota=self._view(conn, name),
        )

    def _deny_text(self, deny: Deny, project: str) -> str:
        """额度判定没过的原因文字（`hub-send` 的原因表；前四条与 ashare-quant 逐字相同）。"""
        cfg = self._cfg.quota
        code = deny.code
        if code == "never_contacted":
            return NEVER_TEXT
        if code == "window_closed":
            when = moment_text(deny.started_ms, self._tz)
            return f"对话窗口过期了（你最后一次发消息是 {when}），{RECOVER}"
        if code == "quota_exhausted":
            return f"这 {cfg.window_hours} 小时的 {cfg.window_limit} 条已经用完，{RECOVER}"
        if code == "quota_insufficient":
            return (
                f"这 {cfg.window_hours} 小时的 {cfg.window_limit} 条只剩 {deny.free} 条，"
                f"不够发这 {deny.n} 条；{RECOVER}"
            )
        if code == "project_cap":
            return (
                f"{project} 这 {cfg.window_hours} 小时最多发 {deny.cap} 条，"
                f"已经发了 {deny.used} 条，不够发这 {deny.n} 条"
            )
        if code == "reserved_for_others":
            return f"剩下的 {deny.reserved} 条是留给回复和其他项目的，这 {deny.n} 条先不发"
        if code == "deferrable_denied":
            return f"额度比较紧，可延后的消息先不发（还剩 {deny.free} 条）"
        if code == "upstream_blocked":
            minutes = math.ceil(deny.retry_in_ms / 60_000)  # 暂停没结束，所以至少是 1
            return (
                f"腾讯拒绝了这个窗口里的发送（{deny.hold_code}），先不再试；"
                f"{minutes} 分钟后再试，或者给 ClawBot 发一句话重新开窗口"
            )
        raise ValueError(f"额度判定的原因代码不认识：{code!r}")

    # ------------------------------------------------ 去重键命中

    def _replay(self, name: str, outbox_id: str, deadline_ms: int) -> SendResult:
        """同一个去重键的上一次发送：收尾了就返回它的结果，没收尾就等，最长等到自己的预算用完。"""
        row = self._await(outbox_id, deadline_ms)
        with self._db.txn() as conn:
            view = self._view(conn, name)
        if row["status"] == "sending":
            return SendResult(
                id=row["id"],
                status="unconfirmed",
                reason_code="duplicate_in_flight",
                reason=IN_FLIGHT_TEXT,
                parts=row["parts"],
                parts_sent=row["parts_sent"],
                duplicate=True,
                quota=view,
            )
        status = "unconfirmed" if row["status"] == "interrupted" else row["status"]
        return SendResult(
            id=row["id"],
            status=status,
            reason_code=row["reason_code"],
            reason=row["reason"],
            parts=row["parts"],
            parts_sent=row["parts_sent"],
            duplicate=True,
            quota=view,
        )

    def _await(self, outbox_id: str, deadline_ms: int) -> sqlite3.Row:
        """等一条发送记录离开"发送中"，到 `deadline_ms` 还没离开就把当时的样子返回。"""
        with self._finished:
            while True:
                row = self._read_row(outbox_id)
                if row["status"] != "sending":
                    return row
                left_ms = deadline_ms - self._clock.now_ms()
                if left_ms <= 0:
                    return row
                self._clock.wait(self._finished, left_ms / 1000)

    def _read_row(self, outbox_id: str) -> sqlite3.Row:
        return self._db.conn().execute("SELECT * FROM outbox WHERE id = ?", (outbox_id,)).fetchone()

    # ------------------------------------------------ 第二步：逐条发

    def _left_ms(self, run: _Run) -> int:
        return run.deadline_ms - self._clock.now_ms()

    def _transmit(self, run: _Run) -> None:
        count = len(run.pieces)
        for index, piece in enumerate(run.pieces):
            if self._left_ms(run) < MIN_START_MS:
                run.timed_out_from = index
                self._give_back(run, count - index)
                return
            part = self._send_part(run, piece)
            run.results.append(part)
            if part.stop:
                if index + 1 < count:
                    self._give_back(run, count - index - 1)
                return

    def _send_part(self, run: _Run, piece: str) -> _Part:
        """一条：发、必要时重试、对账。返回这一条的结局。"""
        client_id = self._new_client_id()
        retries = 0
        while True:
            timeout = min(self._cfg.upstream.request_timeout, self._left_ms(run) / 1000)
            run.touched = run.in_flight = True
            outcome = self._upstream.send_text(
                run.account, run.account.user_id, run.context_token, piece, client_id, timeout
            )
            if not isinstance(outcome, Transient):
                break
            delay = RETRY_FIRST_SECONDS * 2**retries
            if retries < self._cfg.send.retries and self._left_ms(run) - delay * 1000 >= (
                MIN_START_MS
            ):
                logger.warning("发送 %s 第 %d 次没发出去，%d 秒后重试", run.id, retries + 1, delay)
                self._clock.sleep(delay)
                retries += 1
                continue
            reason = mask(
                f"微信 ClawBot 发送失败，重试 {retries} 次后仍然失败：{outcome.reason}",
                run.to_mask,
            )
            self._record(run, PartResult.NOT_SENT, "failed")
            return _Part("failed", "upstream_unreachable", reason, stop=True)

        if isinstance(outcome, Accepted):
            return self._accepted(run, outcome)
        if isinstance(outcome, NoResponse):
            self._record(run, PartResult.UNCONFIRMED, "unconfirmed")
            self._requested(UNCONFIRMED)
            return _Part("unconfirmed", "no_response", NO_RESPONSE_TEXT, stop=True)
        if isinstance(outcome, Rejected):
            has_code = outcome.code not in (None, "")
            code = str(outcome.code) if has_code else f"HTTP {outcome.http_status}"
            self._record(run, PartResult.REJECTED, "failed", code=code, completed=True)
            reason = mask(f"微信 ClawBot 拒绝：{outcome.detail}", run.to_mask)
            return _Part("failed", "upstream_rejected", reason, stop=True)
        if isinstance(outcome, SessionExpired):
            self._record(run, PartResult.NOT_SENT, "failed", completed=True)
            self._monitor.session_expired(run.digest)  # 该不该报警、改不改状态由监视器按规矩判
            expired = LOGIN_REASONS["expired"]
            return _Part("failed", expired.code, expired.text, stop=True)
        raise TypeError(f"上游给了不认识的发送结果：{type(outcome).__name__}")

    def _accepted(self, run: _Run, outcome: Accepted) -> _Part:
        """腾讯接受了：有消息编号就是 `sent`；没有的话看 `upstream.confirm`（`strict` 是未确认，
        其余是 `sent`，`observe` 和 `strict` 记一笔"没有编号"）。两种都继续发后面的段。"""
        confirm = self._cfg.upstream.confirm
        missing = outcome.message_id is None
        if missing and confirm == "strict":
            self._record(
                run, PartResult.UNCONFIRMED, "unconfirmed", completed=True, missing_id=True
            )
            self._requested(UNCONFIRMED)
            return _Part("unconfirmed", "no_message_id", NO_MESSAGE_ID_TEXT)
        self._record(
            run,
            PartResult.SENT,
            "sent",
            completed=True,
            missing_id=missing and confirm == "observe",
        )
        if not missing:
            self._requested(CONFIRMED)
        elif confirm == "observe":
            self._requested(UNCONFIRMED)
        return _Part("sent")

    def _record(
        self,
        run: _Run,
        result: PartResult,
        status: str,
        *,
        code: str | None = None,
        completed: bool = False,
        missing_id: bool = False,
    ) -> None:
        """这一条的账：额度对账和发送记录里的进度在同一个事务里。"""
        sent = run.sent + (status == "sent")
        unconfirmed = run.unconfirmed + (status == "unconfirmed")
        no_id = run.no_id + missing_id
        with self._db.txn() as conn:
            hold = self._quota.settle(conn, run.reservation, result, code=code)
            conn.execute(
                "UPDATE outbox SET parts_sent = ?, parts_unconfirmed = ?, no_message_id = ?"
                " WHERE id = ?",
                (sent + unconfirmed, unconfirmed, no_id, run.id),
            )
            if completed:
                conn.execute(
                    "UPDATE account_state SET last_upstream_ok_ms = ? WHERE id = 1",
                    (self._clock.now_ms(),),
                )
        run.sent, run.unconfirmed, run.no_id = sent, unconfirmed, no_id
        run.settled += 1
        run.in_flight = False
        if hold is not None:
            self._announce(hold)

    def _give_back(self, run: _Run, count: int) -> None:
        """没发的 `count` 条：额度退回（"没发"不是腾讯的拒绝，不会带来暂停）。"""
        with self._db.txn() as conn:
            self._quota.settle(conn, run.reservation, PartResult.NOT_SENT, count=count)
        run.settled += count

    def _requested(self, kind: str) -> None:
        if self._on_request is None:
            return
        try:
            self._on_request(kind)
        except Exception as exc:  # noqa: BLE001 - 告警出错不能连累发送
            logger.error("请求结果的回调出错：%s", type(exc).__name__)

    def _announce(self, hold: Hold) -> None:
        if self._on_hold is None:
            return
        try:
            self._on_hold(hold)
        except Exception as exc:  # noqa: BLE001 - 告警出错不能连累发送
            logger.error("暂停的回调出错：%s", type(exc).__name__)

    # ------------------------------------------------ 收尾

    def _finalize(self, run: _Run) -> SendResult:
        count = len(run.pieces)
        delivered = run.sent + run.unconfirmed
        problem = next((p for p in run.results if p.status != "sent"), None)
        reason_code = reason = None
        if problem is None and run.timed_out_from is not None:
            unsent = count - run.timed_out_from
            reason_code = "send_timeout"
            reason = (
                f"发送超过 {self._cfg.send.timeout_seconds} 秒还没发完，后面的 {unsent} 条没有发"
            )
        elif problem is not None:
            reason_code, reason = problem.code, problem.reason

        if reason_code is None:
            status = "sent"
        elif delivered == 0:
            status = "failed"
        elif len(run.results) < count or any(p.status == "failed" for p in run.results):
            status = "partial"
        else:
            status = "unconfirmed"
        if status != "sent" and count > 1:
            reason = f"{reason}；发出了 {delivered} 条，共 {count} 条"

        now = self._clock.now_ms()
        with self._db.txn() as conn:
            self._close_row(conn, run.id, run.name, status, reason_code, reason, now)
            view = self._view(conn, run.name)
        return SendResult(
            id=run.id,
            status=status,
            reason_code=reason_code,
            reason=reason,
            parts=count,
            parts_sent=delivered,
            duplicate=False,
            quota=view,
        )

    def _busy(self, run: _Run) -> SendResult:
        """等发送锁等到预算用完（或服务在关闭）：预占的条数全部退回，没有碰腾讯。"""
        count = len(run.pieces)
        now = self._clock.now_ms()
        with self._db.txn() as conn:
            self._quota.settle(conn, run.reservation, PartResult.NOT_SENT, count=count)
            self._close_row(conn, run.id, run.name, "failed", "busy", BUSY_TEXT, now)
            view = self._view(conn, run.name)
        return SendResult(
            id=run.id,
            status="failed",
            reason_code="busy",
            reason=BUSY_TEXT,
            parts=count,
            parts_sent=0,
            duplicate=False,
            quota=view,
        )

    def _abort(self, run: _Run) -> None:
        """发送途中抛出了意外：整理账目（见模块说明），整理不了就留给重启时的"中断"处理。"""
        unstarted = len(run.pieces) - run.settled - (1 if run.in_flight else 0)
        now = self._clock.now_ms()
        try:
            with self._db.txn() as conn:
                if unstarted > 0:
                    self._quota.settle(conn, run.reservation, PartResult.NOT_SENT, count=unstarted)
                if run.touched:
                    status, code, reason = "interrupted", INTERRUPTED_CODE, INTERRUPTED_TEXT
                else:
                    status, code, reason = "failed", "internal", INTERNAL_TEXT
                self._close_row(conn, run.id, run.name, status, code, reason, now)
        except Exception as exc:  # noqa: BLE001 - 已经在处理一个错误，这里只留一条日志
            logger.error("发送 %s 出错以后整理账目也失败了：%s", run.id, type(exc).__name__)

    # ------------------------------------------------ 记录和余量

    def _close_row(
        self,
        conn: sqlite3.Connection,
        outbox_id: str,
        name: str,
        status: str,
        code: str | None,
        reason: str | None,
        now: int,
    ) -> None:
        """把发送记录写成终态，并留一条事件（被拒绝 / 失败 / 未确认 / 部分 / 中断的）。
        已发出的条数不在这里写：每条对账时就写进记录了（`_record`）。"""
        conn.execute(
            "UPDATE outbox SET status = ?, reason_code = ?, reason = ?, finished_ms = ?"
            " WHERE id = ?",
            (status, code, reason, now, outbox_id),
        )
        if status in ("sent", "skipped"):
            return
        level = "INFO" if status == "rejected" else "WARNING"
        if code == "internal":
            level = "ERROR"
        detail = f"发送 {outbox_id} {status}（{code}）：{reason}"
        add_event(conn, now, "send", detail, level=level, project=name)

    def _view(self, conn: sqlite3.Connection, name: str) -> QuotaView:
        """发完以后调用方自己的余量（登录不正常、窗口没开、暂停期里 `left` 是 0）。"""
        cfg = self._cfg.quota
        now = self._clock.now_ms()
        state = self._quota.read_state(conn)
        login_ok = read_login(conn).state == "ok"
        window = state.window
        ends = None
        if window is not None and closed_reason(window, now, cfg) is None:
            ends = window_ends_ms(window, cfg)
        return QuotaView(
            left=quota_left(state, name, cfg, now, login_ok=login_ok),
            limit=cfg.window_limit,
            window_ends_ms=ends,
        )
