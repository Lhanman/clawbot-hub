"""只读着开数据库（任务 D3，`hubctl doctor` 用）：能读、不能写、什么也不建、不动文件。

检查工具"只看不改"的保证落在这里：连接是 `mode=ro`，所以连接关闭时也不会把 WAL 日志合并回
主文件（普通连接是最后一个关闭时会合并并删掉 `-wal`）。
"""

from __future__ import annotations

import shutil
import sqlite3

import pytest

from clawbot_hub.db import (
    Database,
    DatabaseCorrupt,
    check_integrity,
    connect,
    read_version,
    stored_version,
)
from tests.support.threads import run_in_thread
from tests.support.wal import leave_log_behind, wal_of


def test_a_read_only_connection_reads_what_is_there(database, paths):
    with database.txn() as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES ('probe', 'hello')")

    conn = connect(paths.db, read_only=True)
    try:
        assert conn.execute("SELECT value FROM meta WHERE key = 'probe'").fetchone()[0] == "hello"
        assert conn.execute("SELECT 1 AS one").fetchone()["one"] == 1  # 照样按列名取值
    finally:
        conn.close()


def test_a_read_only_connection_cannot_write(database, paths):
    conn = connect(paths.db, read_only=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO meta (key, value) VALUES ('x', 'y')")
    finally:
        conn.close()


def test_a_missing_file_is_not_created(paths):
    with pytest.raises(sqlite3.OperationalError):
        connect(paths.db, read_only=True).execute("SELECT 1 FROM sqlite_master")

    assert not paths.db.exists()


def test_the_busy_timeout_is_applied(database, paths):
    conn = connect(paths.db, busy_timeout_ms=1234, read_only=True)
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1234
    finally:
        conn.close()


def test_the_checks_work_on_a_read_only_connection(database, paths):
    conn = connect(paths.db, read_only=True)
    try:
        check_integrity(conn)
        assert read_version(conn) >= 1
    finally:
        conn.close()


def leftover_log(database, paths):
    """服务被杀掉以后的样子：库文件旁边有一份没合并的日志，手上没有任何连接。"""
    database.close()
    leave_log_behind(paths.db, "INSERT INTO meta (key, value) VALUES ('probe', 'hello')")
    return paths.db, wal_of(paths.db)


def test_reading_leaves_the_data_files_exactly_as_they_were(database, paths):
    """读完以后库文件和日志文件的字节一个不变（-shm 是读写双方互相登记读到哪儿的共享内存，读一下
    它就会动，不算数据）。"""
    db_file, log = leftover_log(database, paths)
    before = (db_file.read_bytes(), log.read_bytes())

    conn = connect(paths.db, read_only=True)
    assert conn.execute("SELECT value FROM meta WHERE key = 'probe'").fetchone()[0] == "hello"
    check_integrity(conn)
    conn.close()

    assert (db_file.read_bytes(), log.read_bytes()) == before


def test_an_ordinary_connection_would_have_merged_the_log_when_it_closed(database, paths, tmp_path):
    """对照：上一条测试要抓的就是这个动作——最后一个关闭的普通连接把日志合并回库文件、删掉日志。
    这条不成立，上一条就什么也证明不了。"""
    leftover_log(database, paths)
    copy = tmp_path / "copy"
    copy.mkdir()
    for name in (paths.db.name, paths.db.name + "-wal", paths.db.name + "-shm"):
        shutil.copy2(paths.home / name, copy / name)

    conn = connect(copy / paths.db.name)
    conn.execute("SELECT count(*) FROM meta").fetchone()
    conn.close()

    assert (
        not wal_of(copy / paths.db.name).exists()
        or wal_of(copy / paths.db.name).stat().st_size == 0
    )


def test_the_last_reader_closing_does_not_merge_the_log_into_the_file(database, paths):
    """只读连接是最后一个关闭的也不合并：日志还在、一个字节没变。"""
    leftover_log(database, paths)
    log = wal_of(paths.db)
    size = log.stat().st_size

    reader = connect(paths.db, read_only=True)
    reader.execute("SELECT count(*) FROM meta").fetchone()
    reader.close()

    assert log.exists() and log.stat().st_size == size


def test_the_database_class_hands_out_read_only_connections(database, paths):
    reader = Database(paths.db, read_only=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            with reader.txn() as conn:
                conn.execute("INSERT INTO meta (key, value) VALUES ('x', 'y')")
        assert reader.conn().execute("SELECT count(*) FROM meta").fetchone()[0] >= 1
    finally:
        reader.close()


def test_a_path_with_spaces_and_non_ascii_characters_still_opens(tmp_path):
    odd = tmp_path / "数据 目录"
    odd.mkdir()
    target = odd / "hub.db"
    with sqlite3.connect(target) as writer:
        writer.execute("CREATE TABLE t (x)")
        writer.execute("INSERT INTO t VALUES (7)")

    conn = connect(target, read_only=True)
    try:
        assert conn.execute("SELECT x FROM t").fetchone()[0] == 7
    finally:
        conn.close()


def test_the_file_is_opened_by_uri_with_mode_ro_and_never_by_a_plain_path(
    database, paths, monkeypatch
):
    """有的 SQLite 编译时默认认 URI，有的不认：要看传给 `sqlite3.connect` 的参数，不能只看结果。"""
    calls = []
    real_connect = sqlite3.connect

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)

    connect(paths.db, read_only=True).close()

    [(args, kwargs)] = calls
    assert args[0].startswith("file:///") and args[0].endswith("?mode=ro")
    assert kwargs["uri"] is True and kwargs["isolation_level"] is None


def test_a_read_only_connection_can_be_used_and_closed_from_another_thread(database, paths):
    """和普通连接一样：`Database.close()` 常在别的线程里关别的线程开的连接。"""
    conn = connect(paths.db, read_only=True)
    try:
        assert run_in_thread(lambda: conn.execute("SELECT 1").fetchone()[0]).result() == 1
        run_in_thread(conn.close).result()  # 在别的线程里关也不出错
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")  # 真的关掉了
    finally:
        conn.close()


def test_a_read_only_connection_that_cannot_be_set_up_is_closed_again(paths, monkeypatch):
    closed = []

    class Broken:
        row_factory = None

        def execute(self, sql, *args):
            raise sqlite3.OperationalError("disk I/O error")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: Broken())

    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        connect(paths.db, read_only=True)

    assert closed == [True]


def test_the_database_class_closes_read_only_connections_of_every_thread(database, paths):
    reading = Database(paths.db, read_only=True)
    mine = reading.conn()
    other = run_in_thread(reading.conn).result()

    reading.close()

    for conn in (mine, other):
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")


# ------------------------------------------------------------ stored_version（`hubctl version` 用）


def test_the_stored_version_of_a_missing_file_is_none_and_nothing_is_created(paths):
    assert stored_version(paths.db) is None
    assert not paths.db.exists()


def test_the_stored_version_of_an_empty_database_file_is_zero(paths):
    paths.db.write_bytes(b"")

    assert stored_version(paths.db) == 0


def test_the_stored_version_is_what_the_file_says(database, paths):
    with database.txn() as conn:
        conn.execute("UPDATE meta SET value = '7' WHERE key = 'schema_version'")

    assert stored_version(paths.db) == 7


def test_looking_at_the_stored_version_does_not_change_the_files(database, paths):
    database.close()
    leave_log_behind(paths.db, "INSERT INTO meta (key, value) VALUES ('probe', 'x')")
    before = (paths.db.read_bytes(), wal_of(paths.db).read_bytes())

    assert stored_version(paths.db) >= 1

    assert (paths.db.read_bytes(), wal_of(paths.db).read_bytes()) == before


def test_a_file_that_is_not_a_database_is_reported_as_such(paths):
    paths.db.write_bytes(b"this is not a database at all" * 20)

    with pytest.raises(DatabaseCorrupt, match="不是 SQLite 数据库"):
        stored_version(paths.db)


def test_a_database_that_is_not_ours_is_reported_as_such(paths):
    other = sqlite3.connect(paths.db)
    other.execute("CREATE TABLE something_else (x)")
    other.commit()
    other.close()

    with pytest.raises(DatabaseCorrupt, match="meta 表"):
        stored_version(paths.db)


@pytest.mark.parametrize(
    ("stored", "message"),
    [(None, "没有版本号"), ("abc", "读不懂"), ("0", "读不懂"), ("-1", "读不懂"), ("1.5", "读不懂")],
)
def test_a_version_that_is_missing_or_makes_no_sense_is_reported(database, paths, stored, message):
    database.close()
    raw = sqlite3.connect(paths.db, isolation_level=None)
    if stored is None:
        raw.execute("DELETE FROM meta WHERE key = 'schema_version'")
    else:
        raw.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (stored,))
    raw.close()

    with pytest.raises(DatabaseCorrupt, match=message):
        stored_version(paths.db)


def test_a_database_somebody_else_is_holding_is_busy_not_corrupt(paths):
    """别人占着锁读不了：和"不是数据库"是两回事，原样抛出原来的错（`hubctl version` 要分开说）。"""
    holder = sqlite3.connect(paths.db, isolation_level=None)
    holder.execute("PRAGMA journal_mode = DELETE")  # 回滚日志模式下，独占锁连读也挡
    holder.execute("CREATE TABLE meta (key TEXT, value TEXT)")
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked") as info:
            stored_version(paths.db, busy_timeout_ms=50)
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert not isinstance(info.value, DatabaseCorrupt)


def test_the_connection_of_the_stored_version_is_closed_again(database, paths, monkeypatch):
    closed = []
    real_connect = sqlite3.connect

    class Spy(sqlite3.Connection):
        def close(self):
            closed.append(True)
            super().close()

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: real_connect(*a, factory=Spy, **k))

    stored_version(paths.db)

    assert closed == [True]
