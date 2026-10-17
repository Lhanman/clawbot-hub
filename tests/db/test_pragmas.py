"""连接的设置和表结构的约束（任务 A3）。

连接：WAL、外键、`busy_timeout`、`secure_delete`；每个线程各用各的连接。
表结构：设计第 4 节那几张表的约束（去重键的部分唯一索引、窗口编号可空、封闭的取值集合……）。
"""

from __future__ import annotations

import os
import re
import sqlite3

import pytest

from clawbot_hub.db import Database, connect, migrate
from clawbot_hub.paths import HubPaths, ensure_home
from tests.support.threads import run_in_thread


def pragma(conn: sqlite3.Connection, name: str):
    return conn.execute(f"PRAGMA {name}").fetchone()[0]


# ---------------------------------------------------------------- 连接的设置


def test_every_connection_has_the_required_settings(paths):
    conn = connect(paths.db, busy_timeout_ms=1234)
    try:
        assert pragma(conn, "foreign_keys") == 1
        assert pragma(conn, "secure_delete") == 1
        assert pragma(conn, "busy_timeout") == 1234
        assert pragma(conn, "journal_mode") == "wal"
    finally:
        conn.close()


def test_the_default_busy_timeout_is_five_seconds(paths):
    conn = connect(paths.db)
    try:
        assert pragma(conn, "busy_timeout") == 5000
    finally:
        conn.close()


def test_rows_can_be_read_by_column_name(paths):
    conn = connect(paths.db)
    try:
        row = conn.execute("SELECT 1 AS one, 'x' AS two").fetchone()
        assert row["one"] == 1 and row["two"] == "x"
    finally:
        conn.close()


def test_secure_delete_is_switched_on_even_when_the_build_defaults_to_off(paths, monkeypatch):
    real_connect = sqlite3.connect

    def connect_like_a_build_that_defaults_to_off(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.execute("PRAGMA secure_delete = OFF")
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect_like_a_build_that_defaults_to_off)
    conn = connect(paths.db)
    try:
        assert pragma(conn, "secure_delete") == 1
    finally:
        conn.close()


def test_a_connection_that_cannot_be_set_up_is_closed_again(paths, monkeypatch):
    closed = []

    class Broken:
        row_factory = None

        def execute(self, sql, *args):
            raise sqlite3.OperationalError("disk I/O error")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: Broken())
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        connect(paths.db)
    assert closed == [True]


def test_creating_the_file_does_not_leave_a_file_descriptor_open(paths, monkeypatch):
    """先用 `os.open` 以 0600 建出文件，再交给 SQLite：自己开的那个描述符要还回去。"""
    opened, closed = [], []
    real_open, real_close = os.open, os.close

    def spy_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def spy_close(fd):
        closed.append(fd)
        return real_close(fd)

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "close", spy_close)

    connect(paths.db).close()

    assert opened and set(opened) <= set(closed)


def test_a_connection_can_be_used_and_closed_from_another_thread(paths):
    conn = connect(paths.db)
    try:
        assert run_in_thread(lambda: conn.execute("SELECT 1").fetchone()[0]).result() == 1
        run_in_thread(conn.close).result()
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")
    finally:
        conn.close()


SECRET = "独特的内容-UNIQUE-SECRET-8c1f6a0e" + "x" * 40


def _write_and_delete_a_secret(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, txt TEXT)")
    conn.execute("INSERT INTO t VALUES (1, ?)", (SECRET,))
    conn.execute("DELETE FROM t WHERE id = 1")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def test_a_deleted_secret_does_not_stay_in_the_file(paths):
    # 对照：不开 secure_delete 的普通连接，删掉的内容还留在文件里——说明下面的检查是有意义的
    plain = sqlite3.connect(paths.home / "control.db", isolation_level=None)
    plain.execute("PRAGMA journal_mode = WAL")
    plain.execute("PRAGMA secure_delete = OFF")
    _write_and_delete_a_secret(plain)
    plain.close()
    assert SECRET.encode() in (paths.home / "control.db").read_bytes()

    conn = connect(paths.db)
    _write_and_delete_a_secret(conn)
    conn.close()
    assert SECRET.encode() not in paths.db.read_bytes()


def test_each_thread_gets_its_own_connection_with_the_same_settings(database):
    mine = database.conn()
    assert database.conn() is mine  # 同一个线程里一直是同一个

    def look():
        conn = database.conn()
        return conn, pragma(conn, "foreign_keys"), pragma(conn, "secure_delete")

    other, foreign_keys, secure_delete = run_in_thread(look).result()
    assert other is not mine
    assert foreign_keys == 1 and secure_delete == 1


# ---------------------------------------------------------------- 表结构的约束


def add_project(conn: sqlite3.Connection, name: str = "quant", **extra) -> int:
    row = {"name": name, "key_hash": f"hash-{name}", "guaranteed": 2, "cap": 5, "created_ms": 1}
    row.update(extra)
    columns = ", ".join(row)
    marks = ", ".join("?" * len(row))
    cursor = conn.execute(f"INSERT INTO projects ({columns}) VALUES ({marks})", tuple(row.values()))
    return cursor.lastrowid


def add_outbox(conn: sqlite3.Connection, project_id: int, outbox_id: str, **extra) -> None:
    row = {
        "id": outbox_id,
        "project_id": project_id,
        "kind": "push",
        "parts": 1,
        "status": "sending",
        "created_ms": 1,
    }
    row.update(extra)
    columns = ", ".join(row)
    marks = ", ".join("?" * len(row))
    conn.execute(f"INSERT INTO outbox ({columns}) VALUES ({marks})", tuple(row.values()))


@pytest.mark.parametrize("status", ["sending", "sent", "unconfirmed", "partial", "interrupted"])
def test_these_states_hold_the_dedupe_key(database, status):
    conn = database.conn()
    project = add_project(conn)
    add_outbox(conn, project, "m1", dedupe_hash="h", status=status)
    with pytest.raises(sqlite3.IntegrityError):
        add_outbox(conn, project, "m2", dedupe_hash="h", status="sending")


@pytest.mark.parametrize("status", ["rejected", "failed", "skipped"])
def test_these_states_do_not_hold_the_dedupe_key(database, status):
    conn = database.conn()
    project = add_project(conn)
    add_outbox(conn, project, "m1", dedupe_hash="h", status=status)
    add_outbox(conn, project, "m2", dedupe_hash="h", status="sending")
    add_outbox(conn, project, "m3", dedupe_hash="h", status=status)


def test_changing_the_state_releases_the_key_by_itself(database):
    conn = database.conn()
    project = add_project(conn)
    add_outbox(conn, project, "m1", dedupe_hash="h", status="sending")
    conn.execute("UPDATE outbox SET status = 'rejected' WHERE id = 'm1'")
    add_outbox(conn, project, "m2", dedupe_hash="h", status="sending")


def test_the_dedupe_key_is_per_project_and_a_missing_key_never_conflicts(database):
    conn = database.conn()
    one = add_project(conn, "one")
    two = add_project(conn, "two")
    add_outbox(conn, one, "m1", dedupe_hash="h")
    add_outbox(conn, two, "m2", dedupe_hash="h")
    add_outbox(conn, one, "m3")
    add_outbox(conn, one, "m4")


def test_the_window_of_a_send_record_may_be_empty(database):
    conn = database.conn()
    project = add_project(conn)
    add_outbox(conn, project, "m1", window_id=None)
    assert conn.execute("SELECT window_id FROM outbox WHERE id = 'm1'").fetchone()[0] is None


def test_deleting_a_window_empties_the_window_of_its_send_records(database):
    conn = database.conn()
    project = add_project(conn)
    window = conn.execute("INSERT INTO windows (started_ms) VALUES (1)").lastrowid
    add_outbox(conn, project, "m1", window_id=window)
    conn.execute("DELETE FROM windows WHERE id = ?", (window,))
    assert conn.execute("SELECT window_id FROM outbox WHERE id = 'm1'").fetchone()[0] is None


def test_foreign_keys_are_enforced(database):
    conn = database.conn()
    with pytest.raises(sqlite3.IntegrityError):
        add_outbox(conn, 999, "m1")
    project = add_project(conn)
    with pytest.raises(sqlite3.IntegrityError):
        add_outbox(conn, project, "m2", window_id=999)


def test_only_one_project_can_be_the_default(database):
    conn = database.conn()
    add_project(conn, "one", is_default=1)
    add_project(conn, "two", is_default=0)
    add_project(conn, "three", is_default=0)
    with pytest.raises(sqlite3.IntegrityError):
        add_project(conn, "four", is_default=1)


def test_names_prefixes_and_key_hashes_are_unique(database):
    conn = database.conn()
    add_project(conn, "one", prefix="量化", prefix_key="量化", key_hash="k1")
    with pytest.raises(sqlite3.IntegrityError):
        add_project(conn, "one", key_hash="k2")
    with pytest.raises(sqlite3.IntegrityError):
        add_project(conn, "two", prefix="x", prefix_key="量化", key_hash="k3")
    with pytest.raises(sqlite3.IntegrityError):
        add_project(conn, "three", key_hash="k1")
    # 只推送的项目没有前缀：空值不算重复
    add_project(conn, "four", key_hash="k4")
    add_project(conn, "five", key_hash="k5")


def test_usage_kind_is_push_or_reply(database):
    conn = database.conn()
    project = add_project(conn)
    window = conn.execute("INSERT INTO windows (started_ms) VALUES (1)").lastrowid
    conn.execute(
        "INSERT INTO usage (window_id, project_id, kind) VALUES (?, ?, 'push')", (window, project)
    )
    conn.execute(
        "INSERT INTO usage (window_id, project_id, kind) VALUES (?, ?, 'reply')", (window, project)
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO usage (window_id, project_id, kind) VALUES (?, ?, 'other')",
            (window, project),
        )
    with pytest.raises(sqlite3.IntegrityError):  # 同一窗口、项目、类别只有一行
        conn.execute(
            "INSERT INTO usage (window_id, project_id, kind) VALUES (?, ?, 'push')",
            (window, project),
        )


@pytest.mark.parametrize(
    "kind", ["login", "alert", "send", "hold", "project", "queue", "backup", "lifecycle"]
)
def test_event_kinds_are_a_closed_set(database, kind):
    conn = database.conn()
    conn.execute("INSERT INTO events (ts_ms, level, kind) VALUES (1, 'info', ?)", (kind,))


def test_an_unknown_event_kind_is_refused(database):
    with pytest.raises(sqlite3.IntegrityError):
        database.conn().execute(
            "INSERT INTO events (ts_ms, level, kind) VALUES (1, 'info', 'other')"
        )


@pytest.mark.parametrize(
    "status",
    ["sending", "sent", "unconfirmed", "partial", "rejected", "failed", "skipped", "interrupted"],
)
def test_send_record_states_are_a_closed_set(database, status):
    conn = database.conn()
    project = add_project(conn)
    add_outbox(conn, project, "m1", status=status)
    with pytest.raises(sqlite3.IntegrityError):
        add_outbox(conn, project, "m2", status="queued")
    with pytest.raises(sqlite3.IntegrityError):
        add_outbox(conn, project, "m3", kind="other")


def test_inbox_states_and_routes_are_closed_sets(database):
    conn = database.conn()
    project = add_project(conn)

    def add(inbox_id: str, state: str, via: str) -> None:
        conn.execute(
            "INSERT INTO inbox (id, project_id, has_text, at_ms, received_ms, via, state) "
            "VALUES (?, ?, 1, 1, 1, ?, ?)",
            (inbox_id, project, via, state),
        )

    for index, state in enumerate(["queued", "leased", "acked", "expired", "dropped"]):
        add(f"i{index}", state, "prefix")
    for index, via in enumerate(["prefix", "confirm", "default"]):
        add(f"v{index}", "queued", via)
    with pytest.raises(sqlite3.IntegrityError):
        add("bad1", "waiting", "prefix")
    with pytest.raises(sqlite3.IntegrityError):
        add("bad2", "queued", "other")


def test_handles_are_unique(database):
    conn = database.conn()
    project = add_project(conn)
    sql = (
        "INSERT INTO inbox (id, project_id, has_text, at_ms, received_ms, via, state, handle) "
        "VALUES (?, ?, 1, 1, 1, 'prefix', 'queued', ?)"
    )
    conn.execute(sql, ("i1", project, "rh_x"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, ("i2", project, "rh_x"))
    conn.execute(sql, ("i3", project, None))
    conn.execute(sql, ("i4", project, None))


def test_account_state_is_a_single_row_with_known_login_states(database):
    conn = database.conn()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO account_state (id, login_state) VALUES (2, 'ok')")
    for state in ("ok", "expired", "missing", "broken"):
        conn.execute("UPDATE account_state SET login_state = ? WHERE id = 1", (state,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE account_state SET login_state = 'sulking' WHERE id = 1")


def test_closed_flags_only_take_zero_or_one(database):
    conn = database.conn()
    for flag, bad in (("enabled", 2), ("push_only", -1), ("is_default", 2), ("internal", 2)):
        with pytest.raises(sqlite3.IntegrityError):
            add_project(conn, "flag", **{flag: bad})
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO windows (started_ms, voided) VALUES (1, 2)")
    project = add_project(conn, "inbox")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO inbox (id, project_id, has_text, at_ms, received_ms, via, state) "
            "VALUES ('i1', ?, 2, 1, 1, 'prefix', 'queued')",
            (project,),
        )


def test_a_new_project_is_enabled_and_has_no_other_flags_by_default(database):
    conn = database.conn()
    add_project(conn, "plain")
    row = conn.execute(
        "SELECT enabled, push_only, is_default, internal, removed_ms "
        "FROM projects WHERE name = 'plain'"
    ).fetchone()
    assert tuple(row) == (1, 0, 0, 0, None)


def test_a_new_window_is_not_voided_and_a_new_usage_row_starts_at_zero(database):
    conn = database.conn()
    window = conn.execute("INSERT INTO windows (started_ms) VALUES (1)").lastrowid
    project = add_project(conn)
    conn.execute(
        "INSERT INTO usage (window_id, project_id, kind) VALUES (?, ?, 'push')", (window, project)
    )
    assert conn.execute("SELECT voided FROM windows WHERE id = ?", (window,)).fetchone()[0] == 0
    assert conn.execute("SELECT n FROM usage").fetchone()[0] == 0


def test_every_new_database_gets_its_own_instance_id(paths, clock, display_tz, tmp_path):
    migrate(paths, clock=clock, tz=display_tz)
    other = HubPaths(tmp_path / "other-home")
    ensure_home(other)
    migrate(other, clock=clock, tz=display_tz)
    ids = []
    for hub_paths in (paths, other):
        conn = connect(hub_paths.db)
        try:
            ids.append(
                conn.execute("SELECT value FROM meta WHERE key = 'instance_id'").fetchone()[0]
            )
        finally:
            conn.close()
    assert all(re.fullmatch(r"[0-9a-f]{32}", value) for value in ids)
    assert ids[0] != ids[1]


def test_an_inbox_row_needs_a_real_project(database):
    conn = database.conn()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO inbox (id, project_id, has_text, at_ms, received_ms, via, state) "
            "VALUES ('i1', 999, 1, 1, 1, 'prefix', 'queued')"
        )


def test_quotas_cannot_be_negative(database):
    conn = database.conn()
    with pytest.raises(sqlite3.IntegrityError):
        add_project(conn, "neg", guaranteed=-1)
    with pytest.raises(sqlite3.IntegrityError):
        add_project(conn, "neg", cap=-1)
    window = conn.execute("INSERT INTO windows (started_ms) VALUES (1)").lastrowid
    project = add_project(conn, "ok")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO usage (window_id, project_id, kind, n) VALUES (?, ?, 'push', -1)",
            (window, project),
        )


def test_a_database_object_hands_out_working_connections(paths, clock, display_tz):
    migrate(paths, clock=clock, tz=display_tz)
    db = Database(paths.db, busy_timeout_ms=777)
    try:
        assert pragma(db.conn(), "busy_timeout") == 777
    finally:
        db.close()
