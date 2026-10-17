"""登录状态：状态行、重新绑定时的数据库事务、凭证文件和数据库的比对、收到 -14 以后的判定。

**状态**写在数据库的 `account_state` 一行里（重启后保持）：`ok` 正常、`expired` 登录失效（腾讯回过
-14）、`missing` 还没绑定（没有 `account.json`）、`broken` 凭证文件读不出来。

**重新绑定**（凭证摘要变了）时 MUST 在一个数据库事务里把所有窗口作废、把所有回复句柄的对话凭证
置空（`reset_conversation`，幂等）。`hubctl login` 写完文件就做一遍；服务里的 `LoginMonitor.sync`
（凭证监视线程每 5 秒一次、启动时一次）发现文件里的摘要和库里记的不一样时再做一遍——所以两步之间
`hubctl login` 被杀也没关系。摘要和这个事务记在同一个数据库事务里，不会出现"摘要记了、窗口没作废"。

**收到 -14**（`LoginMonitor.session_expired`）按下面的先后判定，前一条成立就不看后面的：

1. `CHANGED`：文件里现在的凭证已经不是这一轮收发所用的那个（或文件不见了、读不出来）；
2. `SCANNING`：`login.pending` 存在、里面的进程还活着、开始不到 20 分钟（正在扫码）；
3. `JUST_CHANGED`：服务在最近 60 秒内换上了新凭证（`creds_changed_ms`）——新绑定生效时腾讯会让旧
   凭证失效，不能因此报一条假的失效；
4. `EXPIRED`：都不是，登录状态置为失效，`expired_episode` 加一，叫 `on_expired`。

前三种都不改登录状态、不叫回调。

**恢复**只有两个条件：凭证摘要变了（`sync`），或者探测的 `getupdates` 得到 HTTP 200 且 `ret`、
`errcode` 为空或 0（`probe_finished`）。恢复时叫 `on_restored`；之前有没有发过失效告警，
由告警模块自己判断。

规格：`hub-login`「登录后服务自动换上新凭证」「登录失效」「扫码登录」「凭证文件」。
"""

from __future__ import annotations

import contextlib
import enum
import json
import logging
import os
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass

from clawbot_hub.account import BROKEN_TEXT, AccountFile, AccountFileError, AccountStore
from clawbot_hub.clock import Clock, ClockEvent
from clawbot_hub.db import Database
from clawbot_hub.events import add_event
from clawbot_hub.instance import pid_alive
from clawbot_hub.paths import FILE_MODE, HubPaths
from clawbot_hub.upstream.base import PollOutcome, Updates

logger = logging.getLogger(__name__)

STATES = ("ok", "expired", "missing", "broken")

# 扫码标记的有效期（`hub-login`「扫码登录」）和换凭证后的宽限（「登录后服务自动换上新凭证」）
SCAN_MARKER_MINUTES = 20
LOGIN_GRACE_SECONDS = 60


@dataclass(frozen=True)
class Reason:
    code: str
    text: str


# 登录状态不正常时，发送得到的原因码和原因（`hub-send` 的原因表）。`ok` 没有
LOGIN_REASONS = {
    "missing": Reason("login_missing", "还没绑定微信 ClawBot：运行 hubctl login"),
    "expired": Reason("login_expired", "ClawBot 登录失效了：在电脑上运行 hubctl login 重新扫码"),
    "broken": Reason("login_broken", BROKEN_TEXT),
}


# ---------------------------------------------------------------- 数据库里的状态


@dataclass(frozen=True)
class LoginRow:
    state: str
    since_ms: int | None  # 进入当前状态的时刻
    episode: int  # 第几次失效（每次由正常变成失效加一）
    digest: str | None  # 库里记的凭证摘要
    changed_ms: int | None  # 服务最近一次发现摘要变了的时刻


def read_login(conn: sqlite3.Connection) -> LoginRow:
    row = conn.execute(
        "SELECT login_state, login_since_ms, expired_episode, creds_digest, creds_changed_ms"
        " FROM account_state WHERE id = 1"
    ).fetchone()
    return LoginRow(
        state=row["login_state"],
        since_ms=row["login_since_ms"],
        episode=row["expired_episode"],
        digest=row["creds_digest"],
        changed_ms=row["creds_changed_ms"],
    )


def reset_conversation(conn: sqlite3.Connection) -> None:
    """重新绑定账号时的数据库事务（在调用者的事务里做，幂等）。

    窗口表里所有还没作废的行标上 `voided = 1`、清掉 `context_token`（行留着，发送记录还指着
    它们）；待取记录里所有的对话凭证置空（之后用旧句柄回复得到 410）。"""
    conn.execute("UPDATE windows SET voided = 1, context_token = NULL WHERE voided = 0")
    conn.execute("UPDATE inbox SET context_token = NULL WHERE context_token IS NOT NULL")


# ---------------------------------------------------------------- 扫码标记 login.pending


@dataclass(frozen=True)
class ScanMarker:
    pid: int
    started_ms: int


def _whole_number(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def write_scan_marker(paths: HubPaths, *, pid: int, started_ms: int) -> None:
    """扫码开始时放标记：`{"pid": …, "started_ms": …}`，0600。"""
    text = json.dumps({"pid": pid, "started_ms": started_ms}) + "\n"
    fd = os.open(paths.login_pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)  # 旧文件留下的更宽的权限也收回来
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


def read_scan_marker(paths: HubPaths) -> ScanMarker | None:
    """没有、读不出来、内容不对（进程号不是正整数、开始时间不是非负整数）都是 `None`。"""
    try:
        data = json.loads(paths.login_pending.read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    pid, started = data.get("pid"), data.get("started_ms")
    if not (_whole_number(pid) and pid > 0 and _whole_number(started) and started >= 0):
        return None
    return ScanMarker(pid=pid, started_ms=started)


def remove_scan_marker(paths: HubPaths, *, pid: int) -> None:
    """扫码结束时删标记。标记是别的 `hubctl login` 的（进程号不同）就不动它。
    删不掉也不报错：残留的标记靠进程号和 20 分钟的核对失效，不碍事。"""
    marker = read_scan_marker(paths)
    if marker is not None and marker.pid != pid:
        return
    with contextlib.suppress(OSError):
        paths.login_pending.unlink()


def scan_in_progress(paths: HubPaths, clock: Clock) -> bool:
    """正在扫码吗：标记在、里面的进程还活着、开始不到 20 分钟。

    开始时间在"将来"的标记不信（时钟被拨回去或写坏了），免得它把告警永远压住。"""
    marker = read_scan_marker(paths)
    if marker is None:
        return False
    elapsed_ms = clock.now_ms() - marker.started_ms
    return 0 <= elapsed_ms < SCAN_MARKER_MINUTES * 60_000 and pid_alive(marker.pid)


# ---------------------------------------------------------------- 监视器


class Verdict(enum.Enum):
    """收到 -14 以后的判定，见模块说明。只有 `EXPIRED` 改了登录状态。"""

    CHANGED = "changed"
    SCANNING = "scanning"
    JUST_CHANGED = "just_changed"
    EXPIRED = "expired"


@dataclass(frozen=True)
class SyncResult:
    state: str  # 比对之后的登录状态
    changed: bool  # 凭证摘要变了（第一次比对也算）：事务已补做、新摘要已记下
    recovered: bool  # 这一次比对把"登录失效"清掉了
    account: AccountFile | None  # 文件里现在的凭证；没有或读不出来是 `None`


def probe_proves_recovery(outcome: PollOutcome) -> bool:
    """登录失效期间探测的一次 `getupdates`：HTTP 200 且 `ret`、`errcode` 为空或 0 才算恢复。
    读超时（`timed_out`）没有收到响应，不算；-14、连不上、5xx、`ret: 500` 都不算。"""
    return isinstance(outcome, Updates) and not outcome.timed_out


Callback = Callable[[int], None]


class LoginMonitor:
    """服务里所有需要"现在登录怎么样"的地方共用的一个对象（收消息线程、发送、凭证监视线程）。

    - `account()`：最近一次比对读到的凭证文件内容（没有或读不出来是 `None`）；
    - `login()`：数据库里的登录状态行；
    - `sync()`：比对凭证文件和数据库，见模块说明；监视线程每 5 秒调一次，启动时调一次，
      收消息线程遇到"文件里的凭证变了"时也调一次再重试；
    - `session_expired(used_digest)`：某次收发遇到 -14，`used_digest` 是这一次所用凭证的摘要；
    - `probe_finished(outcome)`：登录失效期间探测的结果，恢复了返回真；
    - `wake`：换上新凭证（或文件重新读得出来）时置位，收消息线程醒来后自己 `clear()`。

    `on_expired(episode)`、`on_restored(episode)` 在状态改完、锁放开以后调（给告警模块用）；
    回调出错只记日志，不影响登录状态。"""

    def __init__(
        self,
        paths: HubPaths,
        db: Database,
        store: AccountStore,
        clock: Clock,
        *,
        on_expired: Callback | None = None,
        on_restored: Callback | None = None,
    ) -> None:
        self._paths = paths
        self._db = db
        self._store = store
        self._clock = clock
        self._on_expired = on_expired
        self._on_restored = on_restored
        self._lock = threading.Lock()
        self._account: AccountFile | None = None
        self.wake = ClockEvent()

    def account(self) -> AccountFile | None:
        return self._account

    def login(self) -> LoginRow:
        return read_login(self._db.conn())

    # ------------------------------------------------ 比对

    def sync(self) -> SyncResult:
        # 加锁只为一件事：两个线程同时比对时，`account()` 不会退回到较旧的那次读到的内容。
        # 状态本身靠数据库事务里的"读-比-写"保证只变一次
        with self._lock:
            result, restored_episode = self._sync()
        if restored_episode is not None:
            self._notify(self._on_restored, restored_episode)
        return result

    def _sync(self) -> tuple[SyncResult, int | None]:
        try:
            account = self._store.read()
            unreadable = False
        except AccountFileError:
            account, unreadable = None, True
        now = self._clock.now_ms()
        if account is None:
            state = "broken" if unreadable else "missing"
            detail = (
                "account.json 读不出来，登录状态改为 broken"
                if unreadable
                else "account.json 不见了，登录状态改为 missing"
            )
            with self._db.txn() as conn:
                if read_login(conn).state != state:
                    self._set_state(conn, state, now)
                    add_event(conn, now, "login", detail, level="WARNING")
            self._account = None
            return SyncResult(state, False, False, None), None

        digest = account.digest
        changed = recovered = back = False
        with self._db.txn() as conn:
            row = read_login(conn)
            state = row.state
            if row.digest != digest:
                changed, recovered, state = True, row.state == "expired", "ok"
                reset_conversation(conn)
                conn.execute(
                    "UPDATE account_state SET creds_digest = ?, creds_changed_ms = ?,"
                    " login_state = 'ok', login_since_ms = ? WHERE id = 1",
                    (digest, now, now),
                )
                add_event(conn, now, "login", "登录凭证已更换：对话窗口作废，换用新凭证")
                if recovered:
                    add_event(conn, now, "login", "登录已恢复（换了新凭证）")
            elif row.state in ("missing", "broken"):
                back, state = True, "ok"
                self._set_state(conn, "ok", now)
                add_event(conn, now, "login", "account.json 又读得出来了，登录状态恢复为 ok")
        self._account = account
        if changed or back:
            self.wake.set()
        return SyncResult(state, changed, recovered, account), (row.episode if recovered else None)

    @staticmethod
    def _set_state(conn: sqlite3.Connection, state: str, now: int) -> None:
        conn.execute(
            "UPDATE account_state SET login_state = ?, login_since_ms = ? WHERE id = 1",
            (state, now),
        )

    # ------------------------------------------------ 收到 -14

    def session_expired(self, used_digest: str) -> Verdict:
        verdict, episode = self._session_expired(used_digest)
        if episode is not None:
            self._notify(self._on_expired, episode)
        return verdict

    def _session_expired(self, used_digest: str) -> tuple[Verdict, int | None]:
        try:
            current = self._store.read()
        except AccountFileError:
            current = None
        if current is None or current.digest != used_digest:
            return Verdict.CHANGED, None
        if scan_in_progress(self._paths, self._clock):
            return Verdict.SCANNING, None
        now = self._clock.now_ms()
        with self._db.txn() as conn:
            row = read_login(conn)
            if row.changed_ms is not None and 0 <= now - row.changed_ms <= (
                LOGIN_GRACE_SECONDS * 1000
            ):
                return Verdict.JUST_CHANGED, None
            if row.state == "expired":
                return Verdict.EXPIRED, None
            episode = row.episode + 1
            conn.execute(
                "UPDATE account_state SET login_state = 'expired', login_since_ms = ?,"
                " expired_episode = ? WHERE id = 1",
                (now, episode),
            )
            add_event(conn, now, "login", "登录失效（腾讯回 -14）", level="WARNING")
        return Verdict.EXPIRED, episode

    # ------------------------------------------------ 探测

    def probe_finished(self, outcome: PollOutcome) -> bool:
        """登录失效期间探测的 `getupdates` 有结果了。恢复了（状态变回 `ok`）返回真。
        恢复不是换凭证：不动摘要、不作废窗口，也不给下一次 -14 留宽限。"""
        if not probe_proves_recovery(outcome):
            return False
        now = self._clock.now_ms()
        with self._db.txn() as conn:
            row = read_login(conn)
            if row.state != "expired":
                return False
            self._set_state(conn, "ok", now)
            add_event(conn, now, "login", "登录已恢复（探测通过）")
        self._notify(self._on_restored, row.episode)
        return True

    # ------------------------------------------------ 内部

    @staticmethod
    def _notify(callback: Callback | None, episode: int) -> None:
        if callback is None:
            return
        try:
            callback(episode)
        except Exception as exc:  # noqa: BLE001 - 告警出错不能连累登录状态
            logger.error("登录状态的回调出错：%s", type(exc).__name__)
