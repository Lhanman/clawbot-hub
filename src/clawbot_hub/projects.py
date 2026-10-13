"""项目：登记、修改、停用、启用、移除，以及项目密钥的生成、认人和换钥。

管理命令（`hubctl project …`）和服务里的每个请求都走这里，直接读写数据库，**不缓存**：改动对下一个
请求立即生效。凡是"先检查、再写"的操作，检查和写入都在同一个写事务（`BEGIN IMMEDIATE`）里，所以
两条命令同时登记也不会一起超过保底总数。

需要使用者确认的操作（换默认项目、改前缀、移除、换钥）在没带 `yes` 时抛 `NeedsConfirmation`，
信息就是要给他看的那句话；交互、`--yes` 和退出码是命令行外壳的事（A8）。别的拒绝抛 `ProjectError`，
信息是一句中文，说清楚错在哪。一次只报第一处错。

密钥是 `chub_` 加 43 个 URL 安全的 base64 字符（`secrets.token_urlsafe(32)`），数据库里只存它的
SHA-256 摘要；认人先按摘要查，再用恒定时间比较。密钥只在登记和换钥的返回值里出现一次，不进事件
记录，也不进日志。

规格：`hub-projects`。
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import re
import secrets
import sqlite3
from dataclasses import dataclass

from clawbot_hub.clock import Clock
from clawbot_hub.config import Config
from clawbot_hub.db import Database
from clawbot_hub.events import add_event
from clawbot_hub.textutil import is_word, normalize

NAME_RULE = re.compile(r"[a-z][a-z0-9-]{1,31}")
KEY_PREFIX = "chub_"
KEY_RULE = re.compile(r"chub_[A-Za-z0-9_-]{43}")
LABEL_MAX_CHARS = 16
GRACE_MAX_MINUTES = 60

# 移除项目后，它的 key_hash 换成这个：不是 64 位十六进制，永远不会等于任何密钥的摘要
_REMOVED_KEY_HASH = "removed:{id}"


# ---------------------------------------------------------------- 结果和错误


class ProjectError(Exception):
    """登记、修改被拒绝。信息是一句中文，说清楚错在哪、怎么办。"""


class NeedsConfirmation(ProjectError):
    """这个操作要使用者确认才能做；信息就是要给他看的那句话。带上 `yes=True` 再来一次。"""


class ProjectNotFound(ProjectError):
    """没有这个项目（服务内部的 `_hub`、`_import` 也算没有）。"""


class Unauthorized(Exception):
    """没带密钥、格式不对、认不出来、项目已移除：一律同一个样子，不透露是哪一种（401）。"""

    def __init__(self) -> None:
        super().__init__("unauthorized")


class ProjectDisabled(Exception):
    """密钥对，但项目已停用（403）。"""

    def __init__(self, project: Project) -> None:
        super().__init__("project_disabled")
        self.project = project


@dataclass(frozen=True)
class Project:
    id: int
    name: str
    prefix: str | None  # 登记时写的样子；只推送的项目、已移除的项目没有
    guaranteed: int
    cap: int
    is_default: bool
    label: str | None  # 回复和推送第一行前面的 `【标签】`；没有就是 None
    enabled: bool
    push_only: bool
    created_ms: int
    removed_ms: int | None = None
    last_seen_ms: int | None = None
    last_inbox_ms: int | None = None
    awaiting_until_ms: int | None = None

    @property
    def removed(self) -> bool:
        return self.removed_ms is not None

    @property
    def display(self) -> str:
        """服务对使用者提到这个项目时的称呼：前缀，没有前缀用名字。"""
        return self.prefix or self.name


@dataclass(frozen=True)
class Registration:
    """登记的结果。`key` 只有这一次拿得到；`notes` 是要多打印的提醒；
    `replaced_default` 是被换掉的原默认项目的名字（没换就是 `None`）。"""

    project: Project
    key: str
    replaced_default: str | None
    notes: tuple[str, ...]


@dataclass(frozen=True)
class Rotation:
    """换钥的结果。`grace_until_ms` 是旧密钥用到哪一刻（没有宽限期就是 `None`）。"""

    project: Project
    key: str
    grace_until_ms: int | None


@dataclass(frozen=True)
class PrefixConflict:
    """一个已登记项目的前缀，和配置里的某个词（`hub.label` 或 `routing.confirm_words`）撞了。"""

    project: str
    prefix: str
    setting: str
    word: str


# ---------------------------------------------------------------- 小函数


def new_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def key_digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _whole(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProjectError(f"{what}要是 0 以上的整数")
    return value


def _project(row: sqlite3.Row) -> Project:
    return Project(
        id=row["id"],
        name=row["name"],
        prefix=row["prefix"],
        guaranteed=row["guaranteed"],
        cap=row["cap"],
        is_default=bool(row["is_default"]),
        label=row["label"],
        enabled=bool(row["enabled"]),
        push_only=bool(row["push_only"]),
        created_ms=row["created_ms"],
        removed_ms=row["removed_ms"],
        last_seen_ms=row["last_seen_ms"],
        last_inbox_ms=row["last_inbox_ms"],
        awaiting_until_ms=row["awaiting_until_ms"],
    )


def is_online(project: Project, now_ms: int, online_seconds: int) -> bool:
    """项目现在在不在线：启用着、能收话、最近 `online_seconds` 秒内取过话或确认过
    （`last_inbox_ms`）。此刻挂着的取话请求那一刻也已经更新了 `last_inbox_ms`，所以读库就够了。
    已停用、已移除和只推送的项目永远不在线；库里的时间比现在还晚（时钟被调慢过）当作刚刚。"""
    if not project.enabled or project.push_only or project.removed:
        return False
    last = project.last_inbox_ms
    return last is not None and now_ms - last <= online_seconds * 1000


def _prefix_reminder(prefix: str) -> str:
    return (
        f"提醒：服务分不清默认项目自己的指令里有没有和前缀「{prefix}」同名的词。例如前缀取"
        "「状态」，默认项目整句「状态」就会被交给这个项目；请自己确认。"
    )


class Projects:
    """项目的增删改查和认人。每个方法自己开事务，可以被几个线程同时用。"""

    def __init__(self, db: Database, config: Config, clock: Clock) -> None:
        self._db = db
        self._config = config
        self._clock = clock

    # ------------------------------------------------------------ 查

    def list(self, *, include_removed: bool = False) -> list[Project]:
        """按登记先后列出项目；内部的 `_hub`、`_import` 不列；已移除的默认不列。"""
        return [_project(row) for row in self._rows(self._db.conn(), include_removed)]

    def get(self, name: str) -> Project:
        """按名字取一个项目（含已移除的）；没有就抛 `ProjectNotFound`。"""
        return _project(self._row(self._db.conn(), name))

    def default(self) -> Project | None:
        row = (
            self._db.conn()
            .execute("SELECT * FROM projects WHERE internal = 0 AND is_default = 1")
            .fetchone()
        )
        return None if row is None else _project(row)

    def prefix_conflicts(self) -> list[PrefixConflict]:
        """已登记项目的前缀里，和 `hub.label`、`routing.confirm_words` 相同的。

        比较不分大小写和全半角。服务启动时据此记警告事件，`hubctl doctor` 把它列为错误
        （见规格「配置与已有前缀冲突」）。"""
        label = self._config.hub.label
        words = {normalize(word): word for word in self._config.routing.confirm_words}
        found: list[PrefixConflict] = []
        for row in self._rows(self._db.conn(), include_removed=False):
            if row["prefix"] is None:
                continue
            key = normalize(row["prefix"])
            if key == normalize(label):
                found.append(PrefixConflict(row["name"], row["prefix"], "hub.label", label))
            if key in words:
                found.append(
                    PrefixConflict(row["name"], row["prefix"], "routing.confirm_words", words[key])
                )
        return found

    # ------------------------------------------------------------ 登记

    def add(
        self,
        name: str,
        *,
        prefix: str | None = None,
        guaranteed: int,
        cap: int,
        make_default: bool = False,
        yes: bool = False,
        label: str | None = None,
        no_label: bool = False,
        push_only: bool = False,
    ) -> Registration:
        """登记一个项目，返回它和一把新密钥。检查依次是：名字、前缀和只推送的关系、前缀、
        保底和上限、标签、默认项目；换掉已有的默认项目要 `yes`。"""
        now = self._clock.now_ms()
        with self._db.txn() as conn:
            self._check_name(conn, name)
            if push_only and prefix is not None:
                raise ProjectError("只推送的项目没有前缀")
            if push_only and make_default:
                raise ProjectError("只推送的项目不能当默认项目")
            if not push_only and prefix is None:
                raise ProjectError("需要收话的项目必须有前缀")
            prefix_key = None if prefix is None else self._check_prefix(conn, prefix)
            self._check_numbers(guaranteed, cap)
            self._check_total(conn, guaranteed, exclude_id=None)

            current = conn.execute("SELECT id, name FROM projects WHERE is_default = 1").fetchone()
            becomes_default = False if push_only else (make_default or current is None)
            replaced = current if becomes_default and current is not None else None
            settled = self._settle_label(name, prefix, label, no_label, push_only, becomes_default)
            if replaced is not None and not yes:
                raise NeedsConfirmation(
                    f"要把默认项目换成 {name}，原来的默认项目是 {replaced['name']}"
                )

            if replaced is not None:  # 同一时间只能有一个：先放下旧的，再写新的
                conn.execute("UPDATE projects SET is_default = 0 WHERE id = ?", (replaced["id"],))
            key = new_key()
            cursor = conn.execute(
                "INSERT INTO projects (name, prefix, prefix_key, key_hash, guaranteed, cap,"
                " is_default, label, enabled, push_only, internal, created_ms)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 0, ?)",
                (
                    name,
                    prefix,
                    prefix_key,
                    key_digest(key),
                    guaranteed,
                    cap,
                    int(becomes_default),
                    settled,
                    int(push_only),
                    now,
                ),
            )
            detail = f"登记项目 {name}（保底 {guaranteed}、上限 {cap}）"
            detail += "，只推送" if push_only else ""
            detail += "，成为默认项目" if becomes_default else ""
            detail += f"，原来的默认项目是 {replaced['name']}" if replaced is not None else ""
            add_event(conn, now, "project", detail, project=name)
            project = _project(
                conn.execute("SELECT * FROM projects WHERE id = ?", (cursor.lastrowid,)).fetchone()
            )
        notes = () if prefix is None else (_prefix_reminder(prefix),)
        return Registration(
            project=project,
            key=key,
            replaced_default=None if replaced is None else replaced["name"],
            notes=notes,
        )

    # ------------------------------------------------------------ 修改

    def set(
        self,
        name: str,
        *,
        prefix: str | None = None,
        guaranteed: int | None = None,
        cap: int | None = None,
        label: str | None = None,
        no_label: bool = False,
        make_default: bool = False,
        yes: bool = False,
    ) -> Project:
        """改一个项目的前缀、保底、上限、标签、是不是默认项目；没给的不动，什么也没给就什么也不做。
        改完同样要通过登记时的校验；改前缀、换掉已有的默认项目要 `yes`。"""
        now = self._clock.now_ms()
        with self._db.txn() as conn:
            row = self._live_row(conn, name)
            changes: dict[str, object] = {}
            said: list[str] = []
            asks: list[str] = []

            if prefix is not None:
                if row["push_only"]:
                    raise ProjectError("只推送的项目没有前缀")
                if normalize(prefix) != row["prefix_key"]:
                    changes["prefix_key"] = self._check_prefix(conn, prefix)
                    changes["prefix"] = prefix
                    said.append(f"前缀 {row['prefix']} → {prefix}")
                    asks.append(f"要把项目 {name} 的前缀从「{row['prefix']}」改成「{prefix}」")

            new_guaranteed = row["guaranteed"] if guaranteed is None else guaranteed
            new_cap = row["cap"] if cap is None else cap
            if guaranteed is not None or cap is not None:
                self._check_numbers(new_guaranteed, new_cap)
                if row["enabled"]:
                    self._check_total(conn, new_guaranteed, exclude_id=row["id"])
                if new_guaranteed != row["guaranteed"]:
                    changes["guaranteed"] = new_guaranteed
                    said.append(f"保底 {row['guaranteed']} → {new_guaranteed}")
                if new_cap != row["cap"]:
                    changes["cap"] = new_cap
                    said.append(f"上限 {row['cap']} → {new_cap}")

            if label is not None or no_label:
                new_label = self._explicit_label(label, no_label)
                if new_label != row["label"]:
                    changes["label"] = new_label
                    said.append("标签 " + ("去掉" if new_label is None else f"改成 {new_label}"))

            replaced = None
            if make_default and not row["is_default"]:
                if row["push_only"]:
                    raise ProjectError("只推送的项目不能当默认项目")
                replaced = conn.execute(
                    "SELECT id, name FROM projects WHERE is_default = 1"
                ).fetchone()
                changes["is_default"] = 1
                said.append("成为默认项目")
                if replaced is not None:
                    asks.append(f"要把默认项目换成 {name}，原来的默认项目是 {replaced['name']}")

            if asks and not yes:
                raise NeedsConfirmation("；".join(asks))
            if not changes:
                return _project(row)
            if replaced is not None:
                conn.execute("UPDATE projects SET is_default = 0 WHERE id = ?", (replaced["id"],))
            columns = ", ".join(f"{column} = ?" for column in changes)
            conn.execute(
                f"UPDATE projects SET {columns} WHERE id = ?", (*changes.values(), row["id"])
            )
            add_event(conn, now, "project", f"改了项目 {name}：" + "，".join(said), project=name)
            return _project(
                conn.execute("SELECT * FROM projects WHERE id = ?", (row["id"],)).fetchone()
            )

    # ------------------------------------------------------------ 停用、启用、移除

    def disable(self, name: str) -> Project:
        """停用：对下一个请求立即生效，等确认的标记清掉。已经是停用的就什么也不做。"""
        now = self._clock.now_ms()
        with self._db.txn() as conn:
            row = self._live_row(conn, name)
            if row["enabled"]:
                conn.execute(
                    "UPDATE projects SET enabled = 0, awaiting_until_ms = NULL WHERE id = ?",
                    (row["id"],),
                )
                add_event(conn, now, "project", f"停用了项目 {name}", project=name)
            return _project(
                conn.execute("SELECT * FROM projects WHERE id = ?", (row["id"],)).fetchone()
            )

    def enable(self, name: str) -> Project:
        """启用：重新校验保底合计，超了就拒绝并说明超了多少。已经是启用的就什么也不做。"""
        now = self._clock.now_ms()
        with self._db.txn() as conn:
            row = self._live_row(conn, name)
            if not row["enabled"]:
                self._check_total(conn, row["guaranteed"], exclude_id=None)
                conn.execute("UPDATE projects SET enabled = 1 WHERE id = ?", (row["id"],))
                add_event(conn, now, "project", f"启用了项目 {name}", project=name)
            return _project(
                conn.execute("SELECT * FROM projects WHERE id = ?", (row["id"],)).fetchone()
            )

    def remove(self, name: str, *, yes: bool = False) -> Project:
        """移除：先停用；要确认。它还没取走的话作废（清掉正文和对话凭证），前缀和默认项目的身份
        放掉，密钥作废；记录保留，名字不能再被新项目用。"""
        now = self._clock.now_ms()
        with self._db.txn() as conn:
            row = self._live_row(conn, name)
            if row["enabled"]:
                raise ProjectError(f"项目 {name} 还在启用，要先停用：hubctl project disable {name}")
            if row["is_default"]:
                other = conn.execute(
                    "SELECT name FROM projects WHERE internal = 0 AND removed_ms IS NULL"
                    " AND push_only = 0 AND id != ? ORDER BY id LIMIT 1",
                    (row["id"],),
                ).fetchone()
                if other is not None:
                    raise ProjectError(
                        f"{name} 是默认项目，别的项目也能收话：先用 "
                        f"hubctl project set {other['name']} --default 换掉默认项目，再移除"
                    )
            if not yes:
                raise NeedsConfirmation(f"要移除项目 {name}：它没取走的话会作废，名字也不能再用")
            conn.execute(
                "UPDATE inbox SET state = 'dropped', text = NULL, context_token = NULL,"
                " handle = NULL WHERE project_id = ? AND state IN ('queued', 'leased')",
                (row["id"],),
            )
            conn.execute(
                "UPDATE projects SET removed_ms = ?, is_default = 0, prefix = NULL,"
                " prefix_key = NULL, key_hash = ?, prev_key_hash = NULL, prev_key_until_ms = NULL"
                " WHERE id = ?",
                (now, _REMOVED_KEY_HASH.format(id=row["id"]), row["id"]),
            )
            add_event(conn, now, "project", f"移除了项目 {name}", project=name)
            return _project(
                conn.execute("SELECT * FROM projects WHERE id = ?", (row["id"],)).fetchone()
            )

    # ------------------------------------------------------------ 密钥

    def rotate(self, name: str, *, grace_minutes: int = 0, yes: bool = False) -> Rotation:
        """换一把新密钥。旧密钥立刻失效；`grace_minutes`（0 到 60）让它再用若干分钟。
        宽限期内又换一次，更早的旧密钥立刻失效，只保留上一把。"""
        if (
            isinstance(grace_minutes, bool)
            or not isinstance(grace_minutes, int)
            or not 0 <= grace_minutes <= GRACE_MAX_MINUTES
        ):
            raise ProjectError(f"宽限期要在 0 到 {GRACE_MAX_MINUTES} 分钟之间")
        now = self._clock.now_ms()
        with self._db.txn() as conn:
            row = self._live_row(conn, name)
            if not yes:
                after = (
                    "旧密钥立刻失效" if grace_minutes == 0 else f"旧密钥再用 {grace_minutes} 分钟"
                )
                raise NeedsConfirmation(f"要给项目 {name} 换密钥，{after}")
            key = new_key()
            until = now + grace_minutes * 60_000 if grace_minutes else None
            conn.execute(
                "UPDATE projects SET key_hash = ?, prev_key_hash = ?, prev_key_until_ms = ?"
                " WHERE id = ?",
                (key_digest(key), row["key_hash"] if grace_minutes else None, until, row["id"]),
            )
            detail = f"给项目 {name} 换了密钥"
            detail += f"，旧密钥再用 {grace_minutes} 分钟" if grace_minutes else "，旧密钥立刻失效"
            add_event(conn, now, "project", detail, project=name)
            project = _project(
                conn.execute("SELECT * FROM projects WHERE id = ?", (row["id"],)).fetchone()
            )
        return Rotation(project=project, key=key, grace_until_ms=until)

    def authenticate(self, key: object) -> Project:
        """按密钥认项目：先按摘要查，再用恒定时间比较。认不出来抛 `Unauthorized`，已停用抛
        `ProjectDisabled`；认出来就记下"最后出现"的时间。"""
        if not isinstance(key, str) or KEY_RULE.fullmatch(key) is None:
            raise Unauthorized
        digest = key_digest(key)
        now = self._clock.now_ms()
        row = (
            self._db.conn()
            .execute(
                "SELECT * FROM projects WHERE internal = 0 AND removed_ms IS NULL"
                " AND (key_hash = ? OR (prev_key_hash = ? AND prev_key_until_ms > ?))",
                (digest, digest, now),
            )
            .fetchone()
        )
        if row is None or not self._matches(row, digest):
            raise Unauthorized
        project = _project(row)
        if not project.enabled:
            raise ProjectDisabled(project)
        with self._db.txn() as conn:
            conn.execute("UPDATE projects SET last_seen_ms = ? WHERE id = ?", (now, project.id))
        return dataclasses.replace(project, last_seen_ms=now)

    @staticmethod
    def _matches(row: sqlite3.Row, digest: str) -> bool:
        """恒定时间比较：当前这把，或者宽限期内的上一把（有没有过期，查询里已经卡过了）。"""
        if hmac.compare_digest(row["key_hash"], digest):
            return True
        previous = row["prev_key_hash"]
        return previous is not None and hmac.compare_digest(previous, digest)

    # ------------------------------------------------------------ 内部：取行

    @staticmethod
    def _rows(conn: sqlite3.Connection, include_removed: bool) -> list[sqlite3.Row]:
        where = "internal = 0" + ("" if include_removed else " AND removed_ms IS NULL")
        return conn.execute(f"SELECT * FROM projects WHERE {where} ORDER BY id").fetchall()

    @staticmethod
    def _row(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM projects WHERE name = ? AND internal = 0", (name,)
        ).fetchone()
        if row is None:
            raise ProjectNotFound(f"没有叫 {name} 的项目")
        return row

    def _live_row(self, conn: sqlite3.Connection, name: str) -> sqlite3.Row:
        """取一个没移除的项目；移除了的不能再改。"""
        row = self._row(conn, name)
        if row["removed_ms"] is not None:
            raise ProjectError(f"项目 {name} 已移除，不能再改")
        return row

    # ------------------------------------------------------------ 内部：检查

    @staticmethod
    def _check_name(conn: sqlite3.Connection, name: object) -> None:
        if not isinstance(name, str) or NAME_RULE.fullmatch(name) is None:
            raise ProjectError(
                "名字要以小写字母开头，2 到 32 个字符，只含小写字母、数字、减号"
                "（_hub、_import 是服务内部保留的名字）"
            )
        row = conn.execute("SELECT removed_ms FROM projects WHERE name = ?", (name,)).fetchone()
        if row is not None:
            if row["removed_ms"] is not None:
                raise ProjectError(f"名字 {name} 属于一个已移除的项目，不能再用")
            raise ProjectError(f"名字 {name} 已经有项目在用")

    def _check_prefix(self, conn: sqlite3.Connection, prefix: object) -> str:
        """前缀合规就返回比较用的形式（`prefix_key`），不合规抛 `ProjectError`。"""
        if not isinstance(prefix, str) or not is_word(prefix):
            raise ProjectError("前缀要 1 到 8 个字符，只含字母、数字、汉字（不含空格和标点）")
        key = normalize(prefix)
        if key == normalize(self._config.hub.label):
            raise ProjectError(f"前缀「{prefix}」是服务自己的指令词，不能当前缀")
        if any(key == normalize(word) for word in self._config.routing.confirm_words):
            raise ProjectError(f"前缀「{prefix}」是确认词，不能当前缀")
        taken = conn.execute(
            "SELECT name, enabled FROM projects WHERE prefix_key = ?", (key,)
        ).fetchone()
        if taken is not None:
            note = "" if taken["enabled"] else "（它已停用，仍占着前缀）"
            raise ProjectError(f"前缀「{prefix}」已经被项目 {taken['name']} 占着{note}")
        return key

    def _check_numbers(self, guaranteed: object, cap: object) -> None:
        guaranteed = _whole(guaranteed, "保底")
        cap = _whole(cap, "上限")
        window, reserve = self._config.quota.window_limit, self._config.quota.reply_reserve
        if cap > window - reserve:
            raise ProjectError(
                f"上限最多 {window - reserve}（窗口总条数 {window} 减回复保留 {reserve}）"
            )
        if guaranteed > cap:
            raise ProjectError("保底不能大于上限")

    def _check_total(
        self, conn: sqlite3.Connection, guaranteed: int, *, exclude_id: int | None
    ) -> None:
        """启用的项目的保底之和，加上这个项目的 `guaranteed` 和回复保留，不能超过窗口总条数。"""
        others = conn.execute(
            "SELECT name, guaranteed FROM projects WHERE internal = 0 AND enabled = 1"
            " AND id IS NOT ? ORDER BY id",
            (exclude_id,),
        ).fetchall()
        total = sum(row["guaranteed"] for row in others) + guaranteed
        window, reserve = self._config.quota.window_limit, self._config.quota.reply_reserve
        if total + reserve > window:
            listing = "、".join(f"{row['name']} {row['guaranteed']}" for row in others)
            raise ProjectError(
                f"保底合计 {total} 加上回复保留 {reserve} 超过窗口总条数 {window}，"
                f"超了 {total + reserve - window} 条。别的项目的保底：{listing or '（没有）'}"
            )

    def _check_label(self, label: object) -> str:
        if (
            not isinstance(label, str)
            or not 1 <= len(label) <= LABEL_MAX_CHARS
            or any(char in label for char in "\r\n【】")
        ):
            raise ProjectError(f"标签要 1 到 {LABEL_MAX_CHARS} 个字符，不含换行和【】")
        if normalize(label) == normalize(self._config.hub.label):
            raise ProjectError(f"标签不能是服务自己的指令词「{self._config.hub.label}」")
        return label

    def _explicit_label(self, label: str | None, no_label: bool) -> str | None:
        if label is not None and no_label:
            raise ProjectError("标签：--label 和 --no-label 不能同时给")
        return None if label is None else self._check_label(label)

    def _settle_label(
        self,
        name: str,
        prefix: str | None,
        label: str | None,
        no_label: bool,
        push_only: bool,
        becomes_default: bool,
    ) -> str | None:
        """登记时把标签落成具体的值：自己给的用自己的；`--no-label` 没有；都没给时默认项目没有，
        别的能收话的项目用前缀，只推送的项目用名字（最多取前 16 个字符）。"""
        if label is not None or no_label:
            return self._explicit_label(label, no_label)
        if becomes_default:
            return None
        derived = name[:LABEL_MAX_CHARS] if push_only else prefix
        assert derived is not None  # 能收话的项目一定有前缀
        if normalize(derived) == normalize(self._config.hub.label):
            raise ProjectError(
                f"按规则得出的标签「{derived}」和服务自己的指令词相同：用 --label 另取一个，"
                "或者用 --no-label 不要标签"
            )
        return derived
