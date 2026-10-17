"""建库和迁移（任务 A3）：版本表、只往前、比程序新则拒绝、中途出错回滚、升级前备份、预置的内部项目。

规格：`hub-deploy`「数据库和升级」「启动自检和顺序」第 4 步；设计第 4、15 节。
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from pathlib import Path

import pytest

from clawbot_hub.db import (
    Database,
    DatabaseCorrupt,
    DatabaseProblem,
    DatabaseTooNew,
    Migration,
    MigrationError,
    check_integrity,
    connect,
    is_busy,
    load_migrations,
    migrate,
    read_version,
    validate_migrations,
)
from tests.db.damage import fill_with_events, remove_side_files, scramble_pages
from tests.support.threads import run_in_thread

V1 = load_migrations()[0]
V2 = Migration(
    2, "add_note", "ALTER TABLE projects ADD COLUMN note TEXT;\nCREATE TABLE extra (id INTEGER);"
)
V3 = Migration(3, "more", "CREATE TABLE more (id INTEGER);")
BROKEN_V2 = Migration(
    2, "broken", "CREATE TABLE half_done (id INTEGER);\nINSERT INTO no_such_table VALUES (1);"
)

STAMP = "20261012-100000"  # 假时钟的起点：北京时间 2026-10-12 10:00


def run(paths, clock, tz, migrations=None):
    return migrate(paths, clock=clock, tz=tz, migrations=migrations)


def tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def old_style_database_with_data(paths, clock, tz) -> None:
    """一个只有版本 1 的数据库，里面有项目、窗口、发送记录。"""
    run(paths, clock, tz, [V1])
    conn = connect(paths.db)
    try:
        conn.execute(
            "INSERT INTO projects (name, key_hash, guaranteed, cap, created_ms) "
            "VALUES ('quant', 'h', 2, 5, 1)"
        )
        conn.execute("INSERT INTO windows (started_ms) VALUES (1000)")
        conn.execute(
            "INSERT INTO outbox (id, project_id, kind, parts, status, created_ms) "
            "SELECT 'm1', id, 'push', 1, 'sent', 1 FROM projects WHERE name = 'quant'"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------- 迁移脚本本身


def test_the_shipped_migrations_are_numbered_from_one_without_gaps():
    migrations = load_migrations()
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))
    assert migrations[0].name == "init"
    assert all(m.sql.strip() for m in migrations)


def test_the_loader_reads_numbered_sql_files_in_order_and_ignores_the_rest(tmp_path):
    folder = tmp_path / "migrations"
    folder.mkdir()
    for number in (5, 2, 8, 1, 7, 3, 6, 4):  # 故意不按顺序建
        (folder / f"{number:04d}_step{number}.sql").write_text(
            f"SELECT {number};  -- 中文注释", encoding="utf-8"
        )
    for ignored in (
        "README.md",
        "notes.sql",
        "0009-bad-name.sql",
        "0010_Upper.sql",
        "11_short.sql",
        "0012_x.sql.bak",
    ):
        (folder / ignored).write_text("SELECT 99;", encoding="utf-8")

    loaded = load_migrations(folder)

    assert [(m.version, m.name) for m in loaded] == [(n, f"step{n}") for n in range(1, 9)]
    assert loaded[0].sql == "SELECT 1;  -- 中文注释"
    validate_migrations(loaded)


def test_migrate_refuses_a_broken_list_before_touching_anything(paths, clock, display_tz):
    with pytest.raises(ValueError):
        run(paths, clock, display_tz, [V2])
    assert not paths.db.exists()


def test_validate_rejects_gaps_duplicates_and_disorder():
    validate_migrations([V1, V2, V3])
    for bad in ([], [V2], [V1, V3], [V1, V1], [V2, V1], [Migration(0, "zero", "SELECT 1;")]):
        with pytest.raises(ValueError):
            validate_migrations(bad)


# ---------------------------------------------------------------- 新建


def test_a_new_database_is_created_at_the_latest_version(paths, clock, display_tz):
    assert not paths.db.exists()
    report = run(paths, clock, display_tz)
    latest = load_migrations()[-1].version
    assert (report.from_version, report.to_version) == (0, latest)
    assert report.applied == list(range(1, latest + 1))
    assert report.backup is None  # 新库没有旧数据，不用备份
    conn = connect(paths.db)
    try:
        assert read_version(conn) == latest
        assert conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[
            0
        ] == str(latest)
    finally:
        conn.close()
    assert not paths.backups.exists()


def test_migrating_again_changes_nothing(paths, clock, display_tz):
    run(paths, clock, display_tz)
    report = run(paths, clock, display_tz)
    assert report.applied == []
    assert report.backup is None
    assert report.from_version == report.to_version == load_migrations()[-1].version
    assert not paths.backups.exists()


def test_an_empty_file_counts_as_a_new_database(paths, clock, display_tz):
    fd = os.open(paths.db, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    assert paths.db.stat().st_size == 0
    report = run(paths, clock, display_tz)
    assert report.from_version == 0 and report.backup is None
    assert paths.db.stat().st_size > 0


def test_the_schema_has_the_designed_tables_and_indexes(database):
    conn = database.conn()
    assert tables(conn) >= {
        "meta",
        "account_state",
        "projects",
        "windows",
        "usage",
        "outbox",
        "inbox",
        "inbound_seen",
        "notice_state",
        "alert_state",
        "events",
    }
    indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"one_default", "outbox_dedupe", "inbox_fetch"} <= indexes


def test_no_table_holds_the_login_token_and_send_records_hold_no_conversation_token(database):
    conn = database.conn()
    schema = " ".join(r[0] or "" for r in conn.execute("SELECT sql FROM sqlite_master"))
    assert "bot_token" not in schema
    outbox_columns = {r["name"] for r in conn.execute("PRAGMA table_info(outbox)")}
    assert "context_token" not in outbox_columns
    assert "text" not in outbox_columns  # 发送的正文不落盘
    for table in ("windows", "inbox"):
        columns = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert "context_token" in columns


def test_the_two_internal_projects_are_preset(database):
    rows = {
        r["name"]: r for r in database.conn().execute("SELECT * FROM projects WHERE internal = 1")
    }
    assert set(rows) == {"_hub", "_import"}
    for name, row in rows.items():
        assert (row["enabled"], row["push_only"], row["guaranteed"], row["cap"]) == (0, 1, 0, 0)
        assert row["is_default"] == 0 and row["prefix"] is None and row["removed_ms"] is None
        assert row["key_hash"] == f"internal:{name}"
        assert not re.fullmatch(r"[0-9a-f]{64}", row["key_hash"])  # 不可能认成真的密钥摘要
    assert (
        database.conn().execute("SELECT COUNT(*) FROM projects WHERE internal = 0").fetchone()[0]
        == 0
    )


def test_the_account_row_starts_as_not_logged_in(database):
    row = database.conn().execute("SELECT * FROM account_state").fetchall()
    assert len(row) == 1
    assert row[0]["id"] == 1 and row[0]["login_state"] == "missing"
    assert row[0]["ignored_count"] == 0 and row[0]["expired_episode"] == 0


def test_the_instance_id_is_random_and_survives_upgrades(paths, clock, display_tz):
    run(paths, clock, display_tz, [V1])
    conn = connect(paths.db)
    first = conn.execute("SELECT value FROM meta WHERE key = 'instance_id'").fetchone()[0]
    conn.close()
    assert re.fullmatch(r"[0-9a-f]{32}", first)
    run(paths, clock, display_tz, [V1, V2])
    conn = connect(paths.db)
    assert conn.execute("SELECT value FROM meta WHERE key = 'instance_id'").fetchone()[0] == first
    conn.close()


# ---------------------------------------------------------------- 升级


def test_an_upgrade_makes_a_backup_first_and_keeps_the_data(paths, clock, display_tz):
    old_style_database_with_data(paths, clock, display_tz)
    report = run(paths, clock, display_tz, [V1, V2])

    assert (report.from_version, report.to_version, report.applied) == (1, 2, [2])
    assert report.backup == paths.backups / f"pre-upgrade-v1-v2-{STAMP}"
    conn = connect(paths.db)
    try:
        assert read_version(conn) == 2
        assert "extra" in tables(conn)
        assert conn.execute("SELECT note FROM projects WHERE name = 'quant'").fetchone()[0] is None
        assert conn.execute("SELECT COUNT(*) FROM windows").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM outbox WHERE id = 'm1'").fetchone()[0] == "sent"
    finally:
        conn.close()


def test_the_pre_upgrade_backup_is_the_old_version(paths, clock, display_tz):
    old_style_database_with_data(paths, clock, display_tz)
    report = run(paths, clock, display_tz, [V1, V2])
    copy = sqlite3.connect(f"file:{report.backup / 'hub.db'}?mode=ro", uri=True)
    try:
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert read_version(copy) == 1
        assert "extra" not in tables(copy)
        assert copy.execute("SELECT COUNT(*) FROM projects WHERE name = 'quant'").fetchone()[0] == 1
    finally:
        copy.close()


def test_several_pending_migrations_share_one_backup_named_first_to_last(paths, clock, display_tz):
    old_style_database_with_data(paths, clock, display_tz)
    report = run(paths, clock, display_tz, [V1, V2, V3])
    assert report.applied == [2, 3]
    assert report.backup == paths.backups / f"pre-upgrade-v1-v3-{STAMP}"
    assert sorted(p.name for p in paths.backups.iterdir()) == [f"pre-upgrade-v1-v3-{STAMP}"]
    conn = connect(paths.db)
    try:
        assert read_version(conn) == 3 and {"extra", "more"} <= tables(conn)
    finally:
        conn.close()


def test_a_failed_migration_leaves_the_database_as_it_was(paths, clock, display_tz):
    old_style_database_with_data(paths, clock, display_tz)
    with pytest.raises(MigrationError) as info:
        run(paths, clock, display_tz, [V1, BROKEN_V2])
    assert info.value.version == 2
    assert isinstance(info.value, DatabaseProblem) and info.value.exit_code == 3
    assert "no_such_table" in str(info.value)

    conn = connect(paths.db)
    try:
        assert read_version(conn) == 1
        assert "half_done" not in tables(conn)  # 半截建出来的表也回滚了
        assert conn.execute("SELECT COUNT(*) FROM projects WHERE name = 'quant'").fetchone()[0] == 1
        assert not conn.in_transaction
    finally:
        conn.close()
    assert (paths.backups / f"pre-upgrade-v1-v2-{STAMP}" / "hub.db").exists()  # 备份还在


def test_each_script_is_its_own_transaction(paths, clock, display_tz):
    old_style_database_with_data(paths, clock, display_tz)
    with pytest.raises(MigrationError) as info:
        run(
            paths,
            clock,
            display_tz,
            [V1, V2, Migration(3, "broken", "CREATE TABLE x (id); BOGUS;")],
        )
    assert info.value.version == 3
    conn = connect(paths.db)
    try:
        assert read_version(conn) == 2  # 第 2 个已经提交，第 3 个回滚
        assert "extra" in tables(conn) and "x" not in tables(conn)
    finally:
        conn.close()


def test_a_failed_upgrade_can_be_retried_after_the_script_is_fixed(paths, clock, display_tz):
    old_style_database_with_data(paths, clock, display_tz)
    with pytest.raises(MigrationError):
        run(paths, clock, display_tz, [V1, BROKEN_V2])
    clock.advance(60)
    report = run(paths, clock, display_tz, [V1, V2])
    assert report.applied == [2]
    assert report.backup is not None and report.backup.name == "pre-upgrade-v1-v2-20261012-100100"


def test_a_script_does_not_have_to_end_with_a_semicolon(paths, clock, display_tz):
    old_style_database_with_data(paths, clock, display_tz)
    bare = Migration(2, "bare", "CREATE TABLE bare (id INTEGER)")
    assert run(paths, clock, display_tz, [V1, bare]).applied == [2]
    conn = connect(paths.db)
    try:
        assert read_version(conn) == 2 and "bare" in tables(conn)
    finally:
        conn.close()


def test_a_script_may_end_with_a_comment_line(paths, clock, display_tz):
    old_style_database_with_data(paths, clock, display_tz)
    commented = Migration(2, "commented", "CREATE TABLE commented (id INTEGER);\n-- 到此为止")
    assert run(paths, clock, display_tz, [V1, commented]).applied == [2]
    conn = connect(paths.db)
    try:
        assert read_version(conn) == 2 and "commented" in tables(conn)
    finally:
        conn.close()


def test_a_broken_script_that_merely_mentions_locking_is_still_a_migration_error(
    paths, clock, display_tz
):
    old_style_database_with_data(paths, clock, display_tz)
    sneaky = Migration(2, "sneaky", "INSERT INTO table_that_is_locked_and_busy VALUES (1);")
    with pytest.raises(MigrationError, match="table_that_is_locked_and_busy"):
        run(paths, clock, display_tz, [V1, sneaky])


# ---------------------------------------------------------------- 拒绝启动的情形


def raw_database(path: Path, version: str | None, *, wal: bool = False) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(f"PRAGMA journal_mode = {'WAL' if wal else 'DELETE'}")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    if version is not None:
        conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (version,))
    conn.close()


def test_a_database_newer_than_the_program_is_refused_and_left_untouched(paths, clock, display_tz):
    raw_database(paths.db, "99")
    before = sha(paths.db)
    with pytest.raises(DatabaseTooNew) as info:
        run(paths, clock, display_tz)
    assert "数据库版本比程序新" in str(info.value) and "99" in str(info.value)
    assert info.value.exit_code == 3 and isinstance(info.value, DatabaseProblem)
    assert sha(paths.db) == before
    assert not paths.backups.exists()
    assert sorted(p.name for p in paths.home.iterdir()) == ["hub.db"]  # 连 -wal、-shm 都没留下


def test_the_version_that_is_one_ahead_is_refused_too(paths, clock, display_tz):
    run(paths, clock, display_tz, [V1, V2])
    with pytest.raises(DatabaseTooNew):
        run(paths, clock, display_tz, [V1])


def test_a_file_that_is_not_a_database_is_refused(paths, clock, display_tz):
    paths.db.write_bytes(os.urandom(5000))
    before = sha(paths.db)
    with pytest.raises(DatabaseCorrupt) as info:
        run(paths, clock, display_tz)
    assert info.value.exit_code == 3 and isinstance(info.value, DatabaseProblem)
    assert sha(paths.db) == before
    assert not paths.backups.exists()


def test_a_database_with_damaged_pages_is_refused(paths, clock, display_tz):
    run(paths, clock, display_tz)
    conn = connect(paths.db)
    fill_with_events(conn)
    conn.close()
    scramble_pages(paths.db)
    remove_side_files(paths.db)

    with pytest.raises(DatabaseCorrupt) as info:
        run(paths, clock, display_tz)
    assert "integrity_check" in str(info.value) or "损坏" in str(info.value)
    assert len(str(info.value).splitlines()) <= 8  # 报告只摘要，不把几百行检查结果倒出来


class IntegrityRows:
    """只会回答 `PRAGMA integrity_check` 的假连接。"""

    def __init__(self, *rows: str) -> None:
        self.rows = rows

    def execute(self, sql: str):
        assert sql == "PRAGMA integrity_check"
        return [(row,) for row in self.rows]


def test_a_healthy_integrity_check_passes():
    check_integrity(IntegrityRows("ok"))


def test_a_long_report_is_cut_to_the_first_lines_and_says_how_many_are_left():
    report = "*** in database main ***\n" + "\n".join(
        f"Tree {n} page {n}: bad" for n in range(5, 15)
    )
    with pytest.raises(DatabaseCorrupt) as info:
        check_integrity(IntegrityRows(report))
    text = str(info.value)
    assert "PRAGMA integrity_check" in text
    assert "*** in database main ***" in text
    assert "Tree 5 page 5: bad" in text and "Tree 6 page 6: bad" in text
    assert "Tree 7 page 7" not in text  # 只摘抄前 3 行
    assert "还有 8 行" in text
    assert len(text.splitlines()) <= 8


def test_several_short_rows_are_all_shown_when_there_are_few():
    with pytest.raises(DatabaseCorrupt) as info:
        check_integrity(IntegrityRows("row one is bad", "", "row two is bad"))
    text = str(info.value)
    assert "row one is bad" in text and "row two is bad" in text
    assert "还有" not in text


def test_a_database_that_was_opened_but_never_filled_counts_as_new(paths, clock, display_tz):
    connect(
        paths.db
    ).close()  # 上一次启动在建表之前被打断：文件有内容（WAL 的文件头），但一张表都没有
    assert paths.db.stat().st_size > 0
    report = run(paths, clock, display_tz)
    assert report.from_version == 0 and report.backup is None
    assert report.to_version == load_migrations()[-1].version


def test_a_database_of_something_else_is_refused(paths, clock, display_tz):
    conn = sqlite3.connect(paths.db)
    conn.execute("CREATE TABLE groceries (item TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(DatabaseCorrupt) as info:
        run(paths, clock, display_tz)
    assert "不是" in str(info.value)


@pytest.mark.parametrize("version", ["two", None, "", "1.5", "-1", "0"])
def test_a_missing_or_unreadable_version_is_refused(paths, clock, display_tz, version):
    raw_database(paths.db, version)
    with pytest.raises(DatabaseCorrupt):
        run(paths, clock, display_tz)


def test_the_integrity_check_reports_a_locked_file_as_locked_not_as_corrupt(paths):
    raw_database(paths.db, "1")
    holder = sqlite3.connect(paths.db, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")  # 回滚日志模式下，读的人也进不来
    probe = sqlite3.connect(paths.db, timeout=0.05, isolation_level=None)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked") as info:
            check_integrity(probe)
        assert not isinstance(info.value, DatabaseProblem)
    finally:
        probe.close()
        holder.execute("ROLLBACK")
        holder.close()


def test_a_lock_conflict_is_told_apart_from_other_errors(paths, database):
    other = Database(paths.db, busy_timeout_ms=50)
    try:
        with database.txn():
            with pytest.raises(sqlite3.OperationalError) as locked:
                with other.txn():
                    pass
        with pytest.raises(sqlite3.OperationalError) as missing:
            database.conn().execute("SELECT * FROM table_that_is_locked_and_busy")
    finally:
        other.close()
    assert is_busy(locked.value)
    assert not is_busy(missing.value)
    assert not is_busy(sqlite3.Error("database is locked"))  # 没有错误码，只靠文字不算


def test_the_table_lock_error_counts_as_busy_too():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (x)")
    conn.executemany("INSERT INTO t VALUES (?)", [(1,), (2,)])
    pending = conn.execute("SELECT x FROM t")
    pending.fetchone()
    with pytest.raises(sqlite3.OperationalError) as info:
        conn.execute("DROP TABLE t")  # 同一个连接里还有读到一半的查询：database table is locked
    conn.close()
    assert info.value.sqlite_errorname == "SQLITE_LOCKED"
    assert is_busy(info.value)


def test_extended_busy_codes_count_as_busy_too(paths, database):
    reader = database.conn()
    reader.execute("BEGIN")  # 先读一下，拿到一个旧快照
    reader.execute("SELECT COUNT(*) FROM projects").fetchall()
    writer = Database(paths.db)
    try:
        with writer.txn() as other:
            other.execute("UPDATE account_state SET ignored_count = 1")
        with pytest.raises(sqlite3.OperationalError) as info:
            reader.execute("UPDATE account_state SET ignored_count = 2")  # 旧快照上不能升级成写
    finally:
        writer.close()
        reader.execute("ROLLBACK")
    assert info.value.sqlite_errorname == "SQLITE_BUSY_SNAPSHOT"
    assert is_busy(info.value)


def test_a_locked_new_database_is_reported_as_locked_too(paths, clock, display_tz):
    connect(paths.db).close()  # 空的 WAL 库，还没有表
    holder = Database(paths.db)
    try:
        with holder.txn():
            attempt = run_in_thread(
                lambda: migrate(paths, clock=clock, tz=display_tz, busy_timeout_ms=100)
            )
            with pytest.raises(sqlite3.OperationalError, match="locked") as info:
                attempt.result()
            assert not isinstance(info.value, DatabaseProblem)
    finally:
        holder.close()
    assert run(paths, clock, display_tz).to_version == load_migrations()[-1].version


def test_the_first_look_waits_for_a_busy_file_instead_of_giving_up(paths, clock, display_tz):
    raw_database(paths.db, "1")  # 回滚日志模式：有人在写，读的人也要等
    holder = sqlite3.connect(paths.db, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    attempt = run_in_thread(
        lambda: migrate(paths, clock=clock, tz=display_tz, migrations=[V1], busy_timeout_ms=3000)
    )
    try:
        assert attempt.still_blocked(0.3)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    report = attempt.result()
    assert report.from_version == report.to_version == 1


def test_a_failed_script_leaves_the_connection_outside_any_transaction(paths):
    from clawbot_hub import db

    conn = connect(paths.db)
    try:
        db._apply(conn, V1)
        with pytest.raises(MigrationError):
            db._apply(conn, BROKEN_V2)
        assert not conn.in_transaction
        assert "half_done" not in tables(conn)
        assert read_version(conn) == 1
    finally:
        conn.close()


def test_checking_for_the_write_lock_leaves_no_transaction_open(paths):
    from clawbot_hub import db

    conn = connect(paths.db)
    try:
        db._require_write_lock(conn)
        assert not conn.in_transaction
        holder = Database(paths.db, busy_timeout_ms=50)
        try:
            with holder.txn():
                other = connect(paths.db, busy_timeout_ms=50)
                try:
                    with pytest.raises(sqlite3.OperationalError, match="locked"):
                        db._require_write_lock(other)
                    assert not other.in_transaction
                finally:
                    other.close()
        finally:
            holder.close()
    finally:
        conn.close()


@pytest.fixture
def opened(monkeypatch):
    """记下每个打开过的 SQLite 连接，用来检查有没有忘了关的。"""
    real = sqlite3.connect
    seen = []

    class Tracked(sqlite3.Connection):
        pass

    def tracking_connect(*args, **kwargs):
        conn = real(*args, factory=Tracked, **kwargs)
        seen.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    return seen


def still_open(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return False
    return True


@pytest.mark.parametrize(
    "case", ["new", "up to date", "upgrade", "broken script", "too new", "damaged", "locked"]
)
def test_no_connection_is_left_open_however_the_migration_ends(
    paths, clock, display_tz, opened, case
):
    def attempt(migrations=None):
        return run(paths, clock, display_tz, migrations)

    if case == "new":
        attempt()
    elif case == "up to date":
        attempt()
        attempt()
    elif case == "upgrade":
        old_style_database_with_data(paths, clock, display_tz)
        attempt([V1, V2])
    elif case == "broken script":
        old_style_database_with_data(paths, clock, display_tz)
        with pytest.raises(MigrationError):
            attempt([V1, BROKEN_V2])
    elif case == "too new":
        attempt([V1, V2])
        with pytest.raises(DatabaseTooNew):
            attempt([V1])
    elif case == "damaged":
        paths.db.write_bytes(os.urandom(5000))
        with pytest.raises(DatabaseCorrupt):
            attempt()
    else:
        old_style_database_with_data(paths, clock, display_tz)
        holder = Database(paths.db)
        try:
            with holder.txn():
                with pytest.raises(sqlite3.OperationalError):
                    migrate(
                        paths, clock=clock, tz=display_tz, migrations=[V1, V2], busy_timeout_ms=50
                    )
        finally:
            holder.close()

    assert opened
    assert [conn for conn in opened if still_open(conn)] == []


def test_a_locked_database_is_reported_as_locked_not_as_corrupt(paths, clock, display_tz):
    old_style_database_with_data(paths, clock, display_tz)
    holder = Database(paths.db)
    try:
        with holder.txn():  # 别的进程占着写锁
            attempt = run_in_thread(
                lambda: migrate(
                    paths, clock=clock, tz=display_tz, migrations=[V1, V2], busy_timeout_ms=100
                )
            )
            with pytest.raises(sqlite3.OperationalError, match="locked") as info:
                attempt.result()
            assert not isinstance(info.value, DatabaseProblem)
    finally:
        holder.close()
    conn = connect(paths.db)
    try:
        assert read_version(conn) == 1
    finally:
        conn.close()
    assert run(paths, clock, display_tz, [V1, V2]).applied == [2]  # 锁放开以后可以再升
