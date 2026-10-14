"""数据库：连接的设置、事务、建库和迁移。

SQLite，WAL 模式，外键打开，每个连接 `secure_delete` 打开（删掉的内容不留在文件里），
`busy_timeout` 取配置 `db.busy_timeout_ms`。写事务一律 `BEGIN IMMEDIATE`：先拿写锁再读再写，
两个线程读到同一个旧值、各写各的覆盖掉对方的事不会发生。

迁移脚本在 `clawbot_hub/migrations/`，文件名 `NNNN_名字.sql`，只往前、不回退；
每个脚本在自己的事务里，版本号（`meta.schema_version`）和脚本在同一个事务里写；
升级之前先备份，名字 `pre-upgrade-v<旧>-v<新>-<时间>`。数据库比程序新、打不开、损坏、
不是本服务的库，一律拒绝启动（退出码 3），而且不改动那个文件。

规格：`hub-deploy`「数据库和升级」「启动自检和顺序」；设计第 4、15 节。
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import tzinfo
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

from clawbot_hub import backup
from clawbot_hub.clock import Clock
from clawbot_hub.config import DbConfig
from clawbot_hub.paths import FILE_MODE, HubPaths

DEFAULT_BUSY_TIMEOUT_MS = DbConfig().busy_timeout_ms

# 报告里最多摘抄几条 integrity_check 的结果
INTEGRITY_LINES_SHOWN = 3

_MIGRATION_FILE = re.compile(r"([0-9]{4})_([a-z0-9_]+)\.sql")
_VERSION_TEXT = re.compile(r"[1-9][0-9]*")


# ---------------------------------------------------------------- 错误


class DatabaseProblem(Exception):
    """数据库这边的问题，服务不该继续启动。命令行按 `exit_code` 退出。"""

    exit_code = 3


class DatabaseTooNew(DatabaseProblem):
    """数据库的版本比这个程序认识的新（可能是别的版本的程序升级过它）。"""

    def __init__(self, found: int, known: int) -> None:
        self.found = found
        self.known = known
        super().__init__(
            f"数据库版本比程序新：数据库是第 {found} 版，这个程序只认识到第 {known} 版。"
            "请换回匹配的程序版本，不要让旧程序改动这个数据库。"
        )


class DatabaseCorrupt(DatabaseProblem):
    """打不开、没通过完整性检查、版本号读不出来，或者根本不是本服务的库。"""


class MigrationError(DatabaseProblem):
    """某个迁移脚本执行失败（那个脚本的事务已经回滚，更早的脚本已经提交）。"""

    def __init__(self, version: int, name: str, reason: str) -> None:
        self.version = version
        self.name = name
        super().__init__(
            f"迁移 {version:04d}_{name} 执行失败，已回滚，数据库还是升级前的样子：{reason}"
        )


def is_busy(exc: sqlite3.Error) -> bool:
    """这个错误是不是"别人占着锁"（`database is locked` 一类），而不是库本身的问题。
    只看错误码（扩展错误码的低 8 位是主错误码），不看文字，免得迁移脚本里的表名碰巧带 locked。"""
    code = getattr(exc, "sqlite_errorcode", None)
    return code is not None and (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


# ---------------------------------------------------------------- 连接


def connect(
    path: str | os.PathLike[str],
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    read_only: bool = False,
) -> sqlite3.Connection:
    """打开一个连接并设好：自动提交（事务由 `Database.txn` 显式开）、按列名取值、WAL、
    外键、`secure_delete`、`busy_timeout`。文件不存在就先以 0600 建出来（SQLite 随后建的
    `-wal`、`-shm` 跟着它的权限）。可以在别的线程里用和关闭，但同一时刻只让一个线程用。

    `read_only=True`：只读着打开已有的文件，什么也不建、不改（连接关闭时也不会合并 WAL 日志），
    给只看不改的检查用（`hubctl doctor`）；文件不存在抛 `sqlite3.OperationalError`。"""
    path = Path(path)
    if read_only:
        conn = sqlite3.connect(
            f"{path.absolute().as_uri()}?mode=ro",
            uri=True,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        except BaseException:
            conn.close()
            raise
        return conn
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, FILE_MODE)  # 已经有的不动，也不截断
    os.close(fd)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA secure_delete = ON")
    except BaseException:
        conn.close()
        raise
    return conn


class Database:
    """一个数据库文件，每个线程各用各的连接（线程第一次要的时候才建）。

    `conn()` 取当前线程的连接；`txn()` 开一个写事务。服务关闭时调 `close()`，
    所有线程的连接一起关掉。`read_only=True` 时只读着开已有的文件（见 `connect`），写事务会失败。"""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        read_only: bool = False,
    ) -> None:
        self._path = Path(path)
        self._busy_timeout_ms = busy_timeout_ms
        self._read_only = read_only
        self._local = threading.local()
        self._lock = threading.Lock()
        self._open: list[tuple[threading.Thread, sqlite3.Connection]] = []
        self._closed = False

    def conn(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("数据库已经关闭")
        mine = getattr(self._local, "conn", None)
        if mine is not None:
            return mine
        with self._lock:
            if self._closed:
                raise RuntimeError("数据库已经关闭")
            self._drop_connections_of_finished_threads()
            fresh = connect(
                self._path, busy_timeout_ms=self._busy_timeout_ms, read_only=self._read_only
            )
            self._open.append((threading.current_thread(), fresh))
        self._local.conn = fresh
        return fresh

    def _drop_connections_of_finished_threads(self) -> None:
        """线程结束了它的连接就没人用了：关掉，免得不停地新起线程把连接越攒越多。"""
        alive = []
        for thread, conn in self._open:
            if thread.is_alive():
                alive.append((thread, conn))
            else:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
        self._open = alive

    @contextmanager
    def txn(self) -> Iterator[sqlite3.Connection]:
        """写事务：`BEGIN IMMEDIATE`，块正常结束就提交，出任何异常整个回滚并原样抛出。
        不能嵌套（嵌套是写错了，抛 `RuntimeError`，外层的事务不受影响）。
        拿不到写锁时等 `busy_timeout`，超时抛 `sqlite3.OperationalError`（database is locked）。"""
        conn = self.conn()
        if conn.in_transaction:
            raise RuntimeError("事务不能嵌套")
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            # SQLite 有时已经自己回滚了（比如磁盘满），那就不用再来一次
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        with self._lock:
            self._closed = True
            doomed, self._open = self._open, []
        for _, conn in doomed:
            try:
                conn.close()
            except sqlite3.Error:
                pass


# ---------------------------------------------------------------- 迁移脚本


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def load_migrations(folder: Traversable | None = None) -> list[Migration]:
    """迁移脚本（文件名 `NNNN_名字.sql`，别的文件不管），按编号排好。默认读程序自带的那些。"""
    if folder is None:
        folder = resources.files("clawbot_hub").joinpath("migrations")
    found = []
    for entry in folder.iterdir():
        match = _MIGRATION_FILE.fullmatch(entry.name)
        if match:
            found.append(Migration(int(match[1]), match[2], entry.read_text(encoding="utf-8")))
    found.sort(key=lambda migration: migration.version)
    return found


def validate_migrations(migrations: Sequence[Migration]) -> None:
    """编号必须是 1、2、3……连续、按顺序、不重复；写错了是程序的问题，抛 `ValueError`。"""
    if not migrations:
        raise ValueError("没有任何迁移脚本")
    for expected, migration in enumerate(migrations, start=1):
        if migration.version != expected:
            raise ValueError(
                f"迁移脚本的编号应该是 1、2、3……连续且按顺序，"
                f"第 {expected} 个却是 {migration.version}（{migration.name}）"
            )


# ---------------------------------------------------------------- 检查


def check_integrity(conn: sqlite3.Connection) -> None:
    """`PRAGMA integrity_check`：不是 ok、或者连检查都做不下去，抛 `DatabaseCorrupt`。
    别人占着锁、打不开文件之类不是库的问题，原样抛 `sqlite3.OperationalError`。"""
    try:
        rows = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
    except sqlite3.OperationalError:
        raise
    except sqlite3.DatabaseError as exc:
        raise DatabaseCorrupt(
            f"数据库文件已损坏，或者不是 SQLite 数据库（{exc}）。"
            "可以从 backups/ 里最近的备份恢复，恢复前先把这个文件挪走。"
        ) from exc
    if rows != ["ok"]:
        # 库坏得重时 SQLite 把几百条问题连成一个多行的结果，所以按行数摘抄
        lines = [line for row in rows for line in row.splitlines() if line.strip()]
        shown = "\n".join(f"  {line}" for line in lines[:INTEGRITY_LINES_SHOWN])
        more = len(lines) - INTEGRITY_LINES_SHOWN
        tail = f"\n  （还有 {more} 行）" if more > 0 else ""
        raise DatabaseCorrupt(
            f"数据库没通过完整性检查（PRAGMA integrity_check），文件已损坏：\n{shown}{tail}\n"
            "可以从 backups/ 里最近的备份恢复，恢复前先把这个文件挪走。"
        )


def read_version(conn: sqlite3.Connection) -> int:
    """库里记的版本号。一张表都没有是 0（新库）；有表但不是本服务的库、版本号缺了或不是正整数，
    抛 `DatabaseCorrupt`。"""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not tables:
        return 0
    if "meta" not in tables:
        raise DatabaseCorrupt("这个文件不是 ClawBot 共用服务的数据库（里面没有 meta 表），不能用。")
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        raise DatabaseCorrupt("数据库里没有版本号（meta.schema_version），不敢动它。")
    text = str(row[0])
    if not _VERSION_TEXT.fullmatch(text):
        raise DatabaseCorrupt(f"数据库里的版本号读不懂（{text!r}），不敢动它。")
    return int(text)


def latest_version() -> int:
    """这个程序认识的最新的数据库版本（自带迁移脚本里最大的编号）。"""
    return load_migrations()[-1].version


def stored_version(path: Path, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> int | None:
    """库文件里记的版本号：文件不存在是 `None`，一张表都没有是 0。只看一眼，不改动它，也不做
    完整性检查（`hubctl version` 用）；读不懂（不是数据库、不是本服务的库）抛 `DatabaseCorrupt`。"""
    if not path.exists():
        return None
    conn = sqlite3.connect(path, timeout=busy_timeout_ms / 1000, isolation_level=None)
    try:
        return read_version(conn)
    except sqlite3.DatabaseError as exc:
        if is_busy(exc):
            raise
        raise DatabaseCorrupt("这个文件不是 SQLite 数据库，读不懂。") from exc
    finally:
        conn.close()


# ---------------------------------------------------------------- 迁移


@dataclass(frozen=True)
class MigrationReport:
    from_version: int
    to_version: int
    applied: list[int]
    backup: Path | None


def _probe_existing(path: Path, busy_timeout_ms: int) -> int:
    """已有的数据库文件：只读着看一眼（完整性、版本），不改动它。返回版本号。"""
    probe = sqlite3.connect(path, timeout=busy_timeout_ms / 1000, isolation_level=None)
    try:
        check_integrity(probe)
        return read_version(probe)
    finally:
        probe.close()


def _apply(conn: sqlite3.Connection, migration: Migration) -> None:
    """一个脚本一个事务：脚本和版本号一起提交，中途出错整个回滚。"""
    script = (
        "BEGIN IMMEDIATE;\n"
        f"{migration.sql}\n"
        ";\n"
        "INSERT INTO meta (key, value) VALUES ('schema_version', "
        f"'{migration.version}') "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value;\n"
        "COMMIT;\n"
    )
    try:
        conn.executescript(script)
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if is_busy(exc):
            raise  # 别人占着库：不是脚本的错，让调用者稍后重试
        raise MigrationError(migration.version, migration.name, str(exc)) from exc


def _require_write_lock(conn: sqlite3.Connection) -> None:
    """备份之前先确认拿得到写锁：被别人占着就直接失败，不留下一份多余的备份（重试时同名会撞车）。"""
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("ROLLBACK")


def migrate(
    paths: HubPaths,
    *,
    clock: Clock,
    tz: tzinfo,
    migrations: Sequence[Migration] | None = None,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
) -> MigrationReport:
    """把数据库升到最新：没有就建；比程序新、坏了、不是本服务的库就拒绝（不改动文件）；
    有旧数据要升级时先备份。更早的脚本提交了、后面的出错，数据库停在出错之前的版本。

    不抛 `DatabaseProblem` 的情形：别人占着库（`sqlite3.OperationalError`，database is locked）。"""
    scripts = load_migrations() if migrations is None else list(migrations)
    validate_migrations(scripts)
    latest = scripts[-1].version

    current = 0
    if paths.db.exists():
        current = _probe_existing(paths.db, busy_timeout_ms)
        if current > latest:
            raise DatabaseTooNew(current, latest)
        if current == latest:
            return MigrationReport(current, current, [], None)

    pending = [script for script in scripts if script.version > current]
    conn = connect(paths.db, busy_timeout_ms=busy_timeout_ms)
    try:
        saved = None
        if current > 0:
            _require_write_lock(conn)
            stamp = backup.timestamp(clock.now_ms(), tz)
            name = backup.backup_name("pre-upgrade", stamp, old=current, new=latest)
            saved = backup.make_backup(
                paths, paths.backups, name, busy_timeout_ms=busy_timeout_ms
            ).directory
        for script in pending:
            _apply(conn, script)
    finally:
        conn.close()
    return MigrationReport(current, latest, [script.version for script in pending], saved)
