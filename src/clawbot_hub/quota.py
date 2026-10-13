"""额度：窗口、谁能用几条、预占与逐条对账、腾讯拒绝后的暂停。

分三层，下面一层不依赖上面一层：

1. **纯函数**（不碰数据库）：`closed_reason` 窗口开着还是关着，`decide` 一次要发 n 条能不能发，
   `left` 现在最多还能拆成几条发。窗口、已用条数、各启用项目的保底和上限都在 `State` 里。
2. **`Quota`**（数据库层）：开窗口（`open_window`）、读现状（`read_state`）、预占（`reserve`）、
   逐条对账（`settle`）、重启时把"发送中"标成"中断"（`interrupt_unfinished`）。所有方法都在**调用者
   的事务**里做事，不自己开事务：发送要"插发送记录、判定、预占"在同一个 `BEGIN IMMEDIATE` 里提交。
3. **暂停**（腾讯明确拒绝推送以后停一会儿）：记在窗口那一行（`blocked_until_ms`、`blocked_code`），
   所以新窗口（使用者又说了话）自然没有暂停；过了截止时刻放行一条推送当试探，试探期间把截止临时
   设成"现在 + `send.timeout_seconds`"，别的推送继续被拦；试探的结果在对账时处理。

规格：`hub-quota`「窗口」「谁能用几条」「预占和对账」「腾讯拒绝以后先停一会儿」。

**规格没写、这里定下的约定**（任务单 A7 的实现说明里也有）：

- `Deny` 带着写原因文字要用的数字（还剩几条、上限、已用、窗口起点、暂停的代码和还要几毫秒）；
- `reserved_for_others` 里的"留给回复和其他项目的 N 条"取 free 减去这条推送最多能用的条数；
- 被拒的推送退回条数时不让已用掉到 0 以下（调用方多退了一次也不出错）；
- 推送被拒（`REJECTED`）必须给出腾讯的代码，回复被拒不用，也不引起暂停；
- 被拒的那条发在旧窗口、现在窗口已经换了（或被作废）：不记暂停，新窗口不受牵连；
- 暂停期里又有一条先前预占的推送被拒：以最近这次为准重新计时；
- 试探的结果只看第一段（`Probe.resolved` 防止后面几段再动暂停），且只改仍等于临时截止的那一行；
- 每次暂停的开始、试探、解除都写一条 `hold` 事件，没有正文和凭证。
"""

from __future__ import annotations

import enum
import sqlite3
from dataclasses import dataclass, field

from clawbot_hub.clock import Clock
from clawbot_hub.config import Config, QuotaConfig
from clawbot_hub.events import add_event

PUSH = "push"
REPLY = "reply"
KINDS = (PUSH, REPLY)
PRIORITIES = ("urgent", "normal", "deferrable")

HOUR_MS = 3_600_000
MINUTE_MS = 60_000

# 服务重启时把"发送中"的记录标成"中断"所用的原因（`hub-send`「原因代码和原文」）
INTERRUPTED_CODE = "interrupted"
INTERRUPTED_TEXT = "上次发送被中断，不知道发出了几条，不要重发"


# ---------------------------------------------------------------- 数据


@dataclass(frozen=True)
class Window:
    """使用者的一条消息开出来的窗口。`blocked_*` 是腾讯拒绝后的暂停（没有就是空）。"""

    id: int
    started_ms: int
    context_token: str | None = None
    blocked_until_ms: int | None = None
    blocked_code: str | None = None


@dataclass(frozen=True)
class Share:
    """一个启用的项目在窗口里的份额：保底、上限和已推送的条数（回复另算）。"""

    guaranteed: int
    cap: int
    used: int = 0


@dataclass(frozen=True)
class State:
    """判定要用的现状。`used` 是窗口里的总已用（所有项目的推送加所有回复，含内部名字的），
    `replies_used` 是其中回复的条数。没有窗口（从没收到过消息或被作废）时 `window` 是 `None`。"""

    window: Window | None
    shares: dict[str, Share]
    used: int = 0
    replies_used: int = 0


@dataclass(frozen=True)
class Request:
    """一次要发 n 条。`project` 是项目名（服务自己的回复用 `_hub`）；`priority` 只对推送有意义。"""

    project: str
    n: int = 1
    kind: str = PUSH
    priority: str = "normal"

    def __post_init__(self) -> None:
        if isinstance(self.n, bool) or not isinstance(self.n, int) or self.n < 1:
            raise ValueError(f"条数要是从 1 起的整数：{self.n!r}")
        if self.kind not in KINDS:
            raise ValueError(f"种类不认识：{self.kind!r}（可用：{'、'.join(KINDS)}）")
        if self.priority not in PRIORITIES:
            raise ValueError(f"优先级不认识：{self.priority!r}（可用：{'、'.join(PRIORITIES)}）")


@dataclass(frozen=True)
class Allow:
    """放行。`probe` 为真表示这是腾讯拒绝后的试探推送。"""

    n: int
    probe: bool = False


@dataclass(frozen=True)
class Deny:
    """不放行。`code` 是 `hub-send` 原因表里的原因代码；别的字段是写原因文字要用的数字，
    哪个原因带哪些字段见 `decide`，用不到的是 `None`。"""

    code: str
    n: int
    free: int | None = None  # 窗口里还剩几条
    reserved: int | None = None  # 剩下的条数里，留给回复和其他项目的有几条
    cap: int | None = None  # 项目的上限
    used: int | None = None  # 项目已推送的条数
    started_ms: int | None = None  # 过期窗口的起点（使用者最后一次发消息的时间）
    hold_code: str | None = None  # 暂停：腾讯给的代码
    retry_in_ms: int | None = None  # 暂停：还要多久


class PartResult(enum.Enum):
    """一条（一段）消息的结局，对账用。"""

    SENT = "sent"  # 腾讯确认发出
    UNCONFIRMED = "unconfirmed"  # 发出了，但拿不准有没有送到（算已用）
    REJECTED = "rejected"  # 腾讯明确拒绝
    NOT_SENT = "not_sent"  # 没发出去（连不上、没来得及发、前一条拿不准所以没发）


@dataclass
class Probe:
    """试探的状态，跟着预占走。只有对账时改，所以不是冻结的。"""

    previous_until_ms: int  # 暂停原来的截止（已经过了）；撤回临时值时回到它
    temp_until_ms: int  # 试探期间的临时截止
    resolved: bool = False  # 第一段的结果已经处理过了


@dataclass(frozen=True)
class Reservation:
    """放行以后的预占。

    `context_token`：推送用窗口的；回复是 `None`（用被回复那句话的，由调用者另给）。"""

    window_id: int
    project: str
    kind: str
    n: int
    context_token: str | None
    probe: Probe | None = field(default=None)


@dataclass(frozen=True)
class Hold:
    """对账带来的暂停：哪个窗口、腾讯给的代码、停到什么时候、停多少分钟。"""

    window_id: int
    code: str
    until_ms: int
    minutes: int


# ---------------------------------------------------------------- 纯函数：窗口、判定、还能发几条


def window_ends_ms(window: Window, cfg: QuotaConfig) -> int:
    return window.started_ms + cfg.window_hours * HOUR_MS


def closed_reason(window: Window | None, now_ms: int, cfg: QuotaConfig) -> str | None:
    """窗口关着的原因：从没收到过消息 `never_contacted`，已过期 `window_closed`，开着是 `None`。"""
    if window is None:
        return "never_contacted"
    if now_ms >= window_ends_ms(window, cfg):
        return "window_closed"
    return None


def _room(state: State, share: Share, cfg: QuotaConfig) -> tuple[int, int, int, int]:
    """(free, reserve_left, own_left, pool)：窗口还剩几条、回复保留还剩几条、这个项目的保底还剩
    几条、共享池（没被保底和回复保留占着的条数，可以是负数）。"""
    free = cfg.window_limit - state.used
    reserve_left = max(cfg.reply_reserve - state.replies_used, 0)
    guaranteed_left = sum(max(s.guaranteed - s.used, 0) for s in state.shares.values())
    own_left = max(share.guaranteed - share.used, 0)
    return free, reserve_left, own_left, free - reserve_left - guaranteed_left


def decide(state: State, request: Request, cfg: QuotaConfig, now_ms: int) -> Allow | Deny:
    """一次要发 `request.n` 条，按规格的顺序判定，第一个不满足的就是原因：

    窗口 → （推送）腾讯拒绝后的暂停 → 总额 → （回复到这里放行）→ 可延后 → 上限 → 保底和共享池。

    推送的项目不在 `state.shares`（没启用或不存在）是调用者的错，抛 `ValueError`。"""
    n = request.n
    share = None
    if request.kind == PUSH:
        share = state.shares.get(request.project)
        if share is None:
            raise ValueError(f"项目 {request.project} 没有启用，不能推送")

    window = state.window
    reason = closed_reason(window, now_ms, cfg)
    if reason is not None:
        started = window.started_ms if window is not None else None  # 过期的窗口才有起点
        return Deny(reason, n, started_ms=started)

    probe = False
    if request.kind == PUSH and window.blocked_until_ms is not None:
        if now_ms < window.blocked_until_ms:
            return Deny(
                "upstream_blocked",
                n,
                hold_code=window.blocked_code,
                retry_in_ms=window.blocked_until_ms - now_ms,
            )
        probe = True  # 暂停期过了：这一条要是放行就是试探

    free = cfg.window_limit - state.used
    if free <= 0:
        return Deny("quota_exhausted", n, free=0)
    if n > free:
        return Deny("quota_insufficient", n, free=free)
    if request.kind == REPLY:
        return Allow(n)  # 回复不受保留、保底、上限限制

    if request.priority == "deferrable" and free - n < cfg.deferrable_keep:
        return Deny("deferrable_denied", n, free=free)
    if request.priority != "urgent" and share.used + n > share.cap:
        return Deny("project_cap", n, free=free, cap=share.cap, used=share.used)

    free, reserve_left, own_left, pool = _room(state, share, cfg)
    usable = min(free - reserve_left, own_left + max(pool, 0))
    if n > free - reserve_left or max(n - own_left, 0) > max(pool, 0):
        return Deny("reserved_for_others", n, free=free, reserved=free - max(usable, 0))
    return Allow(n, probe=probe)


def left(
    state: State, project: str, cfg: QuotaConfig, now_ms: int, *, login_ok: bool = True
) -> int:
    """一条 `normal` 的推送现在最多能拆成几条发出去：
    max(0, min(free − reserve_left, 上限 − 已用, own_left + max(pool, 0)))。
    登录状态不正常、窗口关着、处于暂停期内（试探已经能放行时不算）、项目没启用时是 0。"""
    if not login_ok or closed_reason(state.window, now_ms, cfg) is not None:
        return 0
    held_until = state.window.blocked_until_ms
    if held_until is not None and now_ms < held_until:
        return 0
    share = state.shares.get(project)
    if share is None:
        return 0
    free, reserve_left, own_left, pool = _room(state, share, cfg)
    return max(0, min(free - reserve_left, share.cap - share.used, own_left + max(pool, 0)))


# ---------------------------------------------------------------- 数据库层


class Quota:
    """窗口和额度的账。方法都在调用者的事务里做事（`Database.txn()`），自己不开事务。"""

    def __init__(self, config: Config, clock: Clock) -> None:
        self._cfg = config.quota
        self._timeout_ms = config.send.timeout_seconds * 1000
        self._clock = clock

    # ---------- 窗口

    def current_window(self, conn: sqlite3.Connection) -> Window | None:
        """当前窗口：没作废的里面起点最晚的（起点相同取后开的）。重新绑定账号作废的不算。"""
        row = conn.execute(
            "SELECT id, started_ms, context_token, blocked_until_ms, blocked_code"
            " FROM windows WHERE voided = 0 ORDER BY started_ms DESC, id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return Window(
            id=row["id"],
            started_ms=row["started_ms"],
            context_token=row["context_token"],
            blocked_until_ms=row["blocked_until_ms"],
            blocked_code=row["blocked_code"],
        )

    def open_window(
        self, conn: sqlite3.Connection, started_ms: int, context_token: str | None
    ) -> int | None:
        """使用者的一条消息到达：开新窗口，返回它的编号。时间早于当前窗口起点的消息（补收的旧消息）
        不倒回窗口，返回 `None`；起点相同的照样开新窗口。被取代的窗口不再留着对话凭证。
        当前窗口还挂着暂停的话，新窗口就是它的解除，写一条 `hold` 事件。"""
        current = self.current_window(conn)
        if current is not None and started_ms < current.started_ms:
            return None
        cursor = conn.execute(
            "INSERT INTO windows (started_ms, context_token) VALUES (?, ?)",
            (started_ms, context_token),
        )
        window_id = cursor.lastrowid
        conn.execute(
            "UPDATE windows SET context_token = NULL WHERE id != ? AND context_token IS NOT NULL",
            (window_id,),
        )
        if current is not None and current.blocked_until_ms is not None:
            add_event(conn, self._clock.now_ms(), "hold", "使用者发来新消息，开了新窗口，暂停解除")
        return window_id

    def read_state(self, conn: sqlite3.Connection) -> State:
        """当前窗口的现状：总已用、回复已用，以及每个启用项目（含只推送的）的保底、上限、已推送。
        停用和移除的项目不在里面，不占保底；`_hub`、`_import` 只算进总已用。"""
        window = self.current_window(conn)
        window_id = window.id if window is not None else None
        totals = conn.execute(
            "SELECT COALESCE(SUM(n), 0) AS used,"
            " COALESCE(SUM(CASE WHEN kind = 'reply' THEN n ELSE 0 END), 0) AS replies"
            " FROM usage WHERE window_id = ?",
            (window_id,),
        ).fetchone()
        rows = conn.execute(
            "SELECT p.name, p.guaranteed, p.cap, COALESCE(u.n, 0) AS used"
            " FROM projects p LEFT JOIN usage u"
            "   ON u.project_id = p.id AND u.kind = 'push' AND u.window_id = ?"
            " WHERE p.enabled = 1",  # 内部名字的 enabled 恒为 0（迁移里写死）；移除要先停用
            (window_id,),
        ).fetchall()
        shares = {r["name"]: Share(r["guaranteed"], r["cap"], r["used"]) for r in rows}
        return State(window, shares, used=totals["used"], replies_used=totals["replies"])

    def left(self, conn: sqlite3.Connection, project: str, *, login_ok: bool = True) -> int:
        """`left` 的数据库版：读现状、按现在的时间算。"""
        return left(
            self.read_state(conn), project, self._cfg, self._clock.now_ms(), login_ok=login_ok
        )

    # ---------- 预占

    def reserve(
        self, conn: sqlite3.Connection, request: Request, *, outbox_id: str | None = None
    ) -> Reservation | Deny:
        """判定，通过就立刻记进窗口的已用（预占）。`outbox_id` 给了的话把发送记录的窗口编号填上
        （拒绝时窗口还在的也填，过期的窗口也是窗口；从没收到过消息的没有窗口，留空）。

        放行的是试探推送时，同一步里把暂停的截止临时设成"现在 + 发送总时限"，同时到达的
        别的推送就被拦在门外。调用者要在**同一个事务**里建发送记录、调这个、提交。"""
        now = self._clock.now_ms()
        state = self.read_state(conn)
        project_id = self._project_id(conn, request.project)
        decision = decide(state, request, self._cfg, now)
        window = state.window
        if isinstance(decision, Deny):
            if window is not None:
                self._link(conn, outbox_id, window.id)
            return decision

        self._add(conn, window.id, project_id, request.kind, request.n)
        probe = None
        if decision.probe:
            temp = now + self._timeout_ms
            conn.execute("UPDATE windows SET blocked_until_ms = ? WHERE id = ?", (temp, window.id))
            probe = Probe(previous_until_ms=window.blocked_until_ms, temp_until_ms=temp)
            add_event(conn, now, "hold", "暂停期已过，放行一条推送试探", project=request.project)
        self._link(conn, outbox_id, window.id)
        token = window.context_token if request.kind == PUSH else None
        return Reservation(window.id, request.project, request.kind, request.n, token, probe)

    # ---------- 对账

    def settle(
        self,
        conn: sqlite3.Connection,
        reservation: Reservation,
        result: PartResult,
        *,
        code: str | None = None,
        count: int = 1,
    ) -> Hold | None:
        """一条（或 `count` 条）的结局，发完一条就记一条，不等整条消息结束：

        - 发出去了（`SENT`、`UNCONFIRMED`）：当前窗口已经不是预占时的那个（发送期间使用者说了话），
          在**当前窗口**的已用里再加；旧窗口的数不退。窗口没换就保持已用。当前没有窗口（被作废）不记。
        - 没发出去（`REJECTED`、`NOT_SENT`）：窗口还是预占时的就从它退回；换了就不用退。
        - 推送被腾讯明确拒绝（`REJECTED`，要给 `code`）：这个窗口进入暂停，返回 `Hold`；
          窗口已经换了就不暂停，返回 `None`。
        - 试探推送（`reservation.probe`）第一段的结果另有处理：发出去了就解除暂停；又被拒绝再停
          一个周期；拿不准或没发出去就撤回临时值，下一条推送仍当试探。"""
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError(f"条数要是从 1 起的整数：{count!r}")
        pushed = reservation.kind == PUSH
        if (
            result is PartResult.REJECTED
            and pushed
            and not (isinstance(code, str) and code.strip())
        ):
            raise ValueError("推送被腾讯拒绝要给出它的代码（业务码或 HTTP 状态）")

        now = self._clock.now_ms()
        window = self.current_window(conn)
        same = window is not None and window.id == reservation.window_id
        project_id = self._project_id(conn, reservation.project)
        probe = reservation.probe if reservation.probe and not reservation.probe.resolved else None
        hold = None

        if result in (PartResult.SENT, PartResult.UNCONFIRMED):
            if window is not None and not same:
                self._add(conn, window.id, project_id, reservation.kind, count)
            if probe is not None:
                if result is PartResult.SENT:
                    self._lift(conn, now, reservation, probe)
                else:
                    self._revert(conn, now, reservation, probe)
        else:
            if same:
                self._give_back(conn, reservation.window_id, project_id, reservation.kind, count)
            if result is PartResult.REJECTED and pushed:
                if same:
                    hold = self._pause(conn, now, reservation, window, code.strip(), probe)
            elif probe is not None:
                self._revert(conn, now, reservation, probe)
        if probe is not None:
            probe.resolved = True
        return hold

    def interrupt_unfinished(self, conn: sqlite3.Connection) -> int:
        """服务启动时：仍标着"发送中"的发送记录标成"中断"，每条写一条 `send` 事件。
        预占不退（不知道发出去了几条）。返回标了几条。"""
        now = self._clock.now_ms()
        rows = conn.execute(
            "SELECT o.id AS id, o.parts AS parts, p.name AS project FROM outbox o"
            " JOIN projects p ON p.id = o.project_id WHERE o.status = 'sending'"
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE outbox SET status = 'interrupted', reason_code = ?, reason = ?,"
                " finished_ms = ? WHERE id = ?",
                (INTERRUPTED_CODE, INTERRUPTED_TEXT, now, row["id"]),
            )
            add_event(
                conn,
                now,
                "send",
                f"发送 {row['id']} 被中断：服务停下时它还在发送，不知道发出了几条，"
                f"预占的 {row['parts']} 条额度保留",
                level="WARNING",
                project=row["project"],
            )
        return len(rows)

    # ---------- 暂停

    def _pause(
        self,
        conn: sqlite3.Connection,
        now: int,
        reservation: Reservation,
        window: Window,
        code: str,
        probe: Probe | None,
    ) -> Hold:
        minutes = self._cfg.reject_hold_minutes
        until = now + minutes * MINUTE_MS
        conn.execute(
            "UPDATE windows SET blocked_until_ms = ?, blocked_code = ? WHERE id = ?",
            (until, code, window.id),
        )
        if probe is not None:
            text = f"试探又被腾讯拒绝（{code}），再暂停 {minutes} 分钟"
        elif window.blocked_until_ms is not None:
            text = f"又被腾讯拒绝（{code}），暂停重新计 {minutes} 分钟"
        else:
            text = f"腾讯拒绝了这个窗口里的发送（{code}），暂停 {minutes} 分钟"
        add_event(conn, now, "hold", text, level="WARNING", project=reservation.project)
        return Hold(window.id, code, until, minutes)

    def _lift(self, conn: sqlite3.Connection, now: int, reservation: Reservation, probe: Probe):
        """试探的推送发出去了：解除暂停（临时截止没被别的改过才动）。"""
        cursor = conn.execute(
            "UPDATE windows SET blocked_until_ms = NULL, blocked_code = NULL"
            " WHERE id = ? AND blocked_until_ms = ?",
            (reservation.window_id, probe.temp_until_ms),
        )
        if cursor.rowcount:
            add_event(
                conn, now, "hold", "试探的推送发出去了，解除暂停", project=reservation.project
            )

    def _revert(self, conn: sqlite3.Connection, now: int, reservation: Reservation, probe: Probe):
        """试探没有结果（拿不准、连不上、没来得及发）：撤回临时的截止，回到原来已经过去的那个。"""
        cursor = conn.execute(
            "UPDATE windows SET blocked_until_ms = ? WHERE id = ? AND blocked_until_ms = ?",
            (probe.previous_until_ms, reservation.window_id, probe.temp_until_ms),
        )
        if cursor.rowcount:
            add_event(
                conn,
                now,
                "hold",
                "试探没有结果，撤回临时的暂停，下一条推送继续试探",
                project=reservation.project,
            )

    # ---------- 记账的小函数

    @staticmethod
    def _project_id(conn: sqlite3.Connection, name: str) -> int:
        row = conn.execute("SELECT id FROM projects WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise ValueError(f"项目 {name} 不存在")
        return row["id"]

    @staticmethod
    def _add(conn: sqlite3.Connection, window_id: int, project_id: int, kind: str, n: int) -> None:
        conn.execute(
            "INSERT INTO usage (window_id, project_id, kind, n) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (window_id, project_id, kind) DO UPDATE SET n = n + excluded.n",
            (window_id, project_id, kind, n),
        )

    @staticmethod
    def _give_back(
        conn: sqlite3.Connection, window_id: int, project_id: int, kind: str, n: int
    ) -> None:
        conn.execute(
            "UPDATE usage SET n = MAX(n - ?, 0)"
            " WHERE window_id = ? AND project_id = ? AND kind = ?",
            (n, window_id, project_id, kind),
        )

    @staticmethod
    def _link(conn: sqlite3.Connection, outbox_id: str | None, window_id: int) -> None:
        """发送记录里记下预占的窗口；没给记录编号（`WHERE id = NULL` 一行也不命中）就什么都不改。"""
        conn.execute("UPDATE outbox SET window_id = ? WHERE id = ?", (window_id, outbox_id))
