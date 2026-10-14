"""备份的核心（任务 A3）：能打开的一致性副本、三种命名、只清理 `auto-` 的旧份、服务运行时也能备份。

`hubctl backup` 命令、`--to` 的提示和云盘路径警告留到 D3，按时间点触发留到 D2。
规格：`hub-cli`「备份」，`hub-deploy`「数据库和升级」。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from clawbot_hub import backup
from clawbot_hub.backup import (
    BackupError,
    auto_backup_names,
    backup_name,
    latest_auto_backup,
    make_backup,
    prune_auto_backups,
    timestamp,
)
from tests.db.damage import fill_with_events, scramble_pages
from tests.support.fake_clock import DEFAULT_START_MS
from tests.support.threads import run_in_thread


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def add_two_projects_and_three_uses(database) -> None:
    with database.txn() as conn:
        for name in ("one", "two"):
            conn.execute(
                "INSERT INTO projects (name, key_hash, guaranteed, cap, created_ms) "
                "VALUES (?, ?, 1, 3, 1)",
                (name, f"hash-{name}"),
            )
        window = conn.execute("INSERT INTO windows (started_ms) VALUES (1)").lastrowid
        one = conn.execute("SELECT id FROM projects WHERE name = 'one'").fetchone()[0]
        conn.execute(
            "INSERT INTO usage (window_id, project_id, kind, n) VALUES (?, ?, 'push', 3)",
            (window, one),
        )


# ---------------------------------------------------------------- 命名


def test_the_timestamp_uses_the_given_time_zone():
    assert timestamp(DEFAULT_START_MS, ZoneInfo("Asia/Shanghai")) == "20261012-100000"
    assert timestamp(DEFAULT_START_MS, ZoneInfo("Asia/Tokyo")) == "20261012-110000"
    assert timestamp(DEFAULT_START_MS, ZoneInfo("UTC")) == "20261012-020000"


def test_the_three_kinds_have_three_names():
    stamp = "20261012-040000"
    assert backup_name("auto", stamp) == "auto-20261012-040000"
    assert backup_name("manual", stamp) == "20261012-040000"
    assert backup_name("pre-upgrade", stamp, old=1, new=2) == "pre-upgrade-v1-v2-20261012-040000"


def test_naming_mistakes_are_programming_errors():
    with pytest.raises(ValueError):
        backup_name("sometimes", "20261012-040000")
    with pytest.raises(ValueError):
        backup_name("pre-upgrade", "20261012-040000")  # 缺新旧版本号
    with pytest.raises(ValueError):
        backup_name("auto", "tomorrow")


@pytest.mark.parametrize(
    "stamp",
    ["20261012-040000x", "x20261012-040000", "20261012-04000", "2026-10-12", "20261012040000", ""],
)
def test_a_time_with_anything_extra_or_missing_is_refused(stamp):
    for kind in ("auto", "manual", "pre-upgrade"):
        with pytest.raises(ValueError):
            backup_name(kind, stamp, old=1, new=2)


# ---------------------------------------------------------------- 做备份


def test_the_backup_opens_and_has_the_data(paths, database):
    add_two_projects_and_three_uses(database)
    result = make_backup(paths, paths.backups, "20261012-100000")

    assert result.directory == paths.backups / "20261012-100000"
    copy = sqlite3.connect(f"file:{result.directory / 'hub.db'}?mode=ro", uri=True)
    try:
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        names = [
            r[0] for r in copy.execute("SELECT name FROM projects WHERE internal = 0 ORDER BY name")
        ]
        assert names == ["one", "two"]
        assert copy.execute("SELECT SUM(n) FROM usage").fetchone()[0] == 3
    finally:
        copy.close()
    assert result.files["hub.db"] == (result.directory / "hub.db").stat().st_size > 0


def test_the_copy_is_one_self_contained_file(paths, database):
    result = make_backup(paths, paths.backups, "20261012-100000")
    assert sorted(p.name for p in result.directory.iterdir()) == ["hub.db"]


def test_the_other_files_are_copied_when_they_exist(paths, database):
    paths.account.write_text('{"bot_token": "tok"}')
    paths.config.write_text("server: { port: 9000 }\n")
    result = make_backup(paths, paths.backups, "20261012-100000")

    assert (result.directory / "account.json").read_text() == '{"bot_token": "tok"}'
    assert (result.directory / "config.yaml").read_text() == "server: { port: 9000 }\n"
    assert not (result.directory / "bark.json").exists()  # 没有的不凭空造
    assert set(result.files) == {"hub.db", "account.json", "config.yaml"}
    assert result.files["account.json"] == len(b'{"bot_token": "tok"}')


def test_copies_are_private_even_when_the_originals_are_not(paths, database):
    for original in (paths.account, paths.config, paths.bark):
        original.write_text("x")
        original.chmod(0o644)
    result = make_backup(paths, paths.backups, "20261012-100000")

    assert mode(result.directory) == 0o700
    for name in ("hub.db", "account.json", "config.yaml", "bark.json"):
        assert mode(result.directory / name) == 0o600


def test_a_missing_parent_is_created_private_and_an_existing_one_is_left_alone(
    paths, database, tmp_path
):
    fresh = tmp_path / "somewhere" / "backups"
    make_backup(paths, fresh, "20261012-100000")
    assert mode(fresh) == 0o700

    existing = tmp_path / "existing"
    existing.mkdir()
    existing.chmod(0o755)
    result = make_backup(paths, existing, "20261012-100000")
    assert mode(existing) == 0o755  # 已经有的父目录不改它的权限
    assert mode(result.directory) == 0o700


def test_an_existing_backup_is_never_overwritten(paths, database):
    first = make_backup(paths, paths.backups, "20261012-100000")
    marker = first.directory / "marker.txt"
    marker.write_text("keep me")
    with pytest.raises(BackupError, match="已经有"):
        make_backup(paths, paths.backups, "20261012-100000")
    assert marker.read_text() == "keep me"


def test_a_parent_that_is_a_file_is_reported_as_such(paths, database, tmp_path):
    in_the_way = tmp_path / "in-the-way"
    in_the_way.write_text("a file")
    with pytest.raises(BackupError, match="建不了存放备份的目录"):
        make_backup(paths, in_the_way, "20261012-100000")
    with pytest.raises(BackupError, match="建不了"):
        make_backup(paths, in_the_way / "deeper", "20261012-100000")
    assert in_the_way.read_text() == "a file"


def test_a_backup_directory_that_cannot_be_created_is_reported_as_such(paths, database):
    with pytest.raises(BackupError, match="建不了备份目录"):
        make_backup(paths, paths.backups, "x" * 300)  # 名字超过文件系统允许的长度
    assert list(paths.backups.iterdir()) == []


def test_without_a_database_there_is_nothing_to_back_up(paths):
    with pytest.raises(BackupError):
        make_backup(paths, paths.backups, "20261012-100000")
    assert not (paths.backups / "20261012-100000").exists()
    assert not paths.db.exists()  # 也不会顺手建一个空库


@pytest.mark.parametrize("breaker", ["_copy_private", "_verify"])
def test_a_backup_that_fails_leaves_no_partial_directory(paths, database, monkeypatch, breaker):
    paths.account.write_text("x")

    def explode(*args, **kwargs):
        raise OSError("磁盘满了")

    monkeypatch.setattr(backup, breaker, explode)
    with pytest.raises(BackupError, match="磁盘满了"):
        make_backup(paths, paths.backups, "20261012-100000")
    assert not (paths.backups / "20261012-100000").exists()
    assert list(paths.backups.iterdir()) == []  # 父目录本身可以留着，里面什么都没有


def test_any_other_kind_of_error_also_leaves_no_partial_directory_and_is_not_disguised(
    paths, database, monkeypatch
):
    paths.account.write_text("x")

    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(backup, "_copy_private", explode)
    with pytest.raises(RuntimeError, match="boom"):
        make_backup(paths, paths.backups, "20261012-100000")
    assert list(paths.backups.iterdir()) == []


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        ("not a database", r"^备份没做成：.*not a database"),  # 打不开：别的错，包一层
        ("damaged pages", r"^备份出来的数据库没通过完整性检查"),  # 我们自己的判断，原样报出
    ],
)
def test_a_copy_that_fails_the_integrity_check_is_not_kept(
    paths, database, monkeypatch, damage, message
):
    fill_with_events(database.conn())

    def broken_copy(source_file, destination, source, busy_timeout_ms):
        if damage == "not a database":
            destination.write_bytes(os.urandom(5000))
        else:
            shutil.copyfile(source_file, destination)
            scramble_pages(destination)

    monkeypatch.setattr(backup, "_copy_database", broken_copy)
    with pytest.raises(BackupError, match=message):
        make_backup(paths, paths.backups, "20261012-100000")
    assert list(paths.backups.iterdir()) == []


def test_a_backup_made_while_the_service_keeps_writing_is_consistent(paths, database):
    stop = threading.Event()
    written = []

    def keep_writing():
        n = 0
        while not stop.is_set():
            with database.txn() as conn:
                conn.execute(
                    "INSERT INTO events (ts_ms, level, kind, detail) VALUES (?, 'info', 'send', ?)",
                    (n, "z" * 200),
                )
            n += 1
            written.append(n)

    writer = run_in_thread(keep_writing)
    try:
        for _ in range(5000):
            if len(written) >= 20:
                break
            time.sleep(0.001)
        before = len(written)
        assert before >= 20
        result = make_backup(paths, paths.backups, "20261012-100000")
        after = len(written)
    finally:
        stop.set()
    writer.result()  # 写的那一边一次错都没出过（比如"database is locked"）

    copy = sqlite3.connect(f"file:{result.directory / 'hub.db'}?mode=ro", uri=True)
    try:
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        rows = copy.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert before <= rows <= after + 1  # 是备份开始到结束之间某个时刻的完整快照
    finally:
        copy.close()


def test_a_backup_can_use_the_connection_the_service_already_has(paths, database):
    add_two_projects_and_three_uses(database)
    result = make_backup(paths, paths.backups, "20261012-100000", source=database.conn())
    copy = sqlite3.connect(f"file:{result.directory / 'hub.db'}?mode=ro", uri=True)
    try:
        assert copy.execute("SELECT COUNT(*) FROM projects WHERE internal = 0").fetchone()[0] == 2
    finally:
        copy.close()


def test_the_connection_that_is_given_is_the_one_that_gets_copied(paths, database, tmp_path):
    other = sqlite3.connect(tmp_path / "other.db")
    other.execute("CREATE TABLE marker (note TEXT)")
    other.execute("INSERT INTO marker VALUES ('from the other connection')")
    other.commit()
    try:
        result = make_backup(paths, paths.backups, "20261012-100000", source=other)
    finally:
        other.close()
    copy = sqlite3.connect(f"file:{result.directory / 'hub.db'}?mode=ro", uri=True)
    try:
        note = copy.execute("SELECT note FROM marker").fetchone()[0]
        assert note == "from the other connection"
    finally:
        copy.close()


def hold_the_database_exclusively(path: Path) -> sqlite3.Connection:
    """另开一个连接独占整个库（WAL 模式下读也被挡住）。要先把别的连接都关掉。"""
    blocker = sqlite3.connect(path, isolation_level=None)
    blocker.execute("PRAGMA locking_mode = EXCLUSIVE")
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute("INSERT INTO events (ts_ms, level, kind) VALUES (1, 'info', 'send')")
    return blocker


def test_a_database_that_stays_locked_is_given_up_on_after_the_busy_timeout(paths, database):
    database.close()
    blocker = hold_the_database_exclusively(paths.db)
    try:
        started = time.monotonic()
        with pytest.raises(BackupError, match="一直被占着"):
            make_backup(paths, paths.backups, "20261012-100000", busy_timeout_ms=300)
        waited = time.monotonic() - started
    finally:
        blocker.close()
    assert 0.25 <= waited < 3  # 等满了设定的时间才放弃，也没有一直等下去
    assert list(paths.backups.iterdir()) == []


def test_a_connection_in_the_middle_of_its_own_write_does_not_hang_the_backup(paths, database):
    conn = database.conn()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO events (ts_ms, level, kind) VALUES (1, 'info', 'send')")
    try:
        started = time.monotonic()
        with pytest.raises(BackupError, match="一直被占着"):
            make_backup(paths, paths.backups, "20261012-100000", source=conn, busy_timeout_ms=300)
        waited = time.monotonic() - started
    finally:
        conn.execute("ROLLBACK")
    assert waited < 3


def test_the_guard_gives_up_only_when_the_source_stays_busy_long_enough():
    now = {"t": 100.0}
    guard = backup._give_up_when_stuck(5000, now=lambda: now["t"])
    busy, locked = sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED

    guard(busy, 0, 0)  # 刚开始，还不放弃
    now["t"] = 104.9
    guard(busy, 0, 0)
    guard(locked, 0, 0)
    now["t"] = 105.0
    with pytest.raises(BackupError, match=r"5\.0 秒.*5000"):
        guard(busy, 0, 0)
    with pytest.raises(BackupError):
        guard(locked, 0, 0)

    now["t"] = 9999.0  # 备份本身没被占着时，花多久都不打断
    guard(sqlite3.SQLITE_OK, 10, 100)
    guard(sqlite3.SQLITE_DONE, 0, 100)


# ---------------------------------------------------------------- 清理旧的自动备份


def make_fake_backup_dirs(parent: Path, names: list[str]) -> None:
    for name in names:
        (parent / name).mkdir(parents=True)
        (parent / name / "hub.db").write_text("x")


def auto_names(count: int) -> list[str]:
    return [f"auto-202610{day:02d}-040000" for day in range(1, count + 1)]


def test_only_the_newest_auto_backups_are_kept(tmp_path):
    parent = tmp_path / "backups"
    manual = ["20261001-101010", "20260901-090909"]
    upgrade = ["pre-upgrade-v1-v2-20260801-000000"]
    make_fake_backup_dirs(parent, auto_names(15) + manual + upgrade)

    removed = prune_auto_backups(parent, 14)

    assert removed == ["auto-20261001-040000"]  # 最早的一份
    remaining = sorted(p.name for p in parent.iterdir())
    assert len([n for n in remaining if n.startswith("auto-")]) == 14
    assert set(manual + upgrade) <= set(remaining)  # 手动的、升级前的都不动


def test_nothing_is_removed_while_there_are_not_more_than_keep(tmp_path):
    parent = tmp_path / "backups"
    make_fake_backup_dirs(parent, auto_names(3))
    assert prune_auto_backups(parent, 3) == []
    assert len(list(parent.iterdir())) == 3


def test_a_missing_directory_is_nothing_to_do(tmp_path):
    assert prune_auto_backups(tmp_path / "nope", 14) == []


def test_look_alikes_are_left_alone(tmp_path):
    parent = tmp_path / "backups"
    make_fake_backup_dirs(
        parent, auto_names(3) + ["auto-latest", "auto-2026", "auto-20261012-100000.old"]
    )
    (parent / "auto-20250101-000000").write_text("a file, not a backup directory")

    removed = prune_auto_backups(parent, 1)

    assert removed == ["auto-20261001-040000", "auto-20261002-040000"]
    assert (parent / "auto-latest").is_dir()
    assert (parent / "auto-2026").is_dir()
    assert (parent / "auto-20261012-100000.old").is_dir()
    assert (parent / "auto-20250101-000000").is_file()


def test_symbolic_links_with_a_backup_name_are_neither_counted_nor_removed(tmp_path):
    parent = tmp_path / "backups"
    make_fake_backup_dirs(parent, auto_names(3))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "precious.txt").write_text("not a backup")
    (parent / "auto-20250101-000000").symlink_to(elsewhere, target_is_directory=True)

    removed = prune_auto_backups(parent, 2)

    assert removed == ["auto-20261001-040000"]
    assert (parent / "auto-20250101-000000").is_symlink()
    assert (elsewhere / "precious.txt").read_text() == "not a backup"


def test_keep_must_be_at_least_one(tmp_path):
    with pytest.raises(ValueError):
        prune_auto_backups(tmp_path, 0)


def test_auto_backup_names_are_the_real_ones_oldest_first(tmp_path):
    parent = tmp_path / "backups"
    make_fake_backup_dirs(
        parent,
        ["auto-20261003-040000", "auto-20261001-040000", "20261002-040000", "auto-latest"]
        + ["pre-upgrade-v1-v2-20261001-000000"],
    )
    (parent / "auto-20261004-040000").write_text("a file")
    (parent / "auto-20261005-040000").symlink_to(parent / "auto-20261001-040000")

    assert auto_backup_names(parent) == ["auto-20261001-040000", "auto-20261003-040000"]


def test_auto_backup_names_of_a_missing_directory_is_empty(tmp_path):
    assert auto_backup_names(tmp_path / "nope") == []


def test_the_latest_auto_backup_is_the_one_with_the_newest_name_and_its_time_is_in_that_zone(
    tmp_path,
):
    parent = tmp_path / "backups"
    make_fake_backup_dirs(parent, auto_names(3) + ["20261009-101010"])
    shanghai = ZoneInfo("Asia/Shanghai")

    name, when = latest_auto_backup(parent, shanghai)

    assert name == "auto-20261003-040000"
    assert when.tzinfo is shanghai
    assert (when.year, when.month, when.day, when.hour, when.minute, when.second) == (
        2026, 10, 3, 4, 0, 0,
    )  # fmt: skip
    assert when.isoformat() == "2026-10-03T04:00:00+08:00"


def test_the_latest_auto_backup_skips_names_with_dates_that_do_not_exist(tmp_path):
    parent = tmp_path / "backups"
    make_fake_backup_dirs(parent, ["auto-20261002-040000", "auto-20261340-040000"])

    name, _ = latest_auto_backup(parent, ZoneInfo("Asia/Shanghai"))

    assert name == "auto-20261002-040000"


def test_no_auto_backup_at_all_is_none(tmp_path):
    parent = tmp_path / "backups"
    make_fake_backup_dirs(parent, ["20261002-040000"])

    assert latest_auto_backup(parent, ZoneInfo("Asia/Shanghai")) is None
    assert latest_auto_backup(tmp_path / "nope", ZoneInfo("Asia/Shanghai")) is None


def test_the_newest_is_decided_by_the_name_not_by_file_dates(tmp_path):
    parent = tmp_path / "backups"
    make_fake_backup_dirs(parent, auto_names(3))
    os.utime(parent / "auto-20261001-040000", (9_999_999_999, 9_999_999_999))  # 文件日期最新
    assert prune_auto_backups(parent, 2) == ["auto-20261001-040000"]


def test_the_core_also_backs_up_into_another_parent_directory(tmp_path, paths, database):
    # `hubctl backup --to <目录>`：父目录由调用者给，同一个函数
    result = make_backup(paths, tmp_path / "elsewhere", backup_name("manual", "20261012-100000"))
    assert result.directory == tmp_path / "elsewhere" / "20261012-100000"
    assert (result.directory / "hub.db").exists()
