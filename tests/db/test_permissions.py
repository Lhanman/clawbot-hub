"""权限（任务 A3）：新建的文件 0600、目录 0700，放宽了的启动时修回。

规格：`hub-deploy`「数据目录和权限」。启动流程本身（写警告、改回）留到 D6，这里测核心函数。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from clawbot_hub.backup import make_backup
from clawbot_hub.db import connect, migrate
from clawbot_hub.paths import HubPaths, WidePermission, fix_permissions, wide_permissions


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def loose_umask():
    """把进程的 umask 放到最松，证明文件的权限是创建时就指定好的，不是碰巧被 umask 收紧。"""
    old = os.umask(0)
    yield
    os.umask(old)


def test_a_new_database_and_its_side_files_are_private(paths, clock, display_tz, loose_umask):
    migrate(paths, clock=clock, tz=display_tz)
    conn = connect(paths.db)
    try:
        conn.execute("INSERT INTO events (ts_ms, level, kind) VALUES (1, 'info', 'login')")
        side_files = [Path(str(paths.db) + s) for s in ("-wal", "-shm")]
        assert any(p.exists() for p in side_files)  # 连接开着，WAL 文件在
        for path in [paths.db, *[p for p in side_files if p.exists()]]:
            assert mode(path) == 0o600, path
    finally:
        conn.close()


def test_a_new_backup_is_private(paths, database, loose_umask):
    paths.account.write_text("x")
    result = make_backup(paths, paths.backups, "20261012-100000")
    assert mode(paths.backups) == 0o700
    assert mode(result.directory) == 0o700
    assert mode(result.directory / "hub.db") == 0o600
    assert mode(result.directory / "account.json") == 0o600


def make_loose_house(paths: HubPaths) -> list[Path]:
    """数据目录里每一处都放宽到 0644 / 0755，返回应该被修回的路径。"""
    files = [
        paths.account,
        paths.account_lock,
        paths.db,
        Path(str(paths.db) + "-wal"),
        Path(str(paths.db) + "-shm"),
        paths.config,
        paths.bark,
        paths.lock,
        paths.run,
        paths.login_png,
        paths.login_pending,
        paths.launchd_log,
        paths.log_file,
        paths.backups / "auto-20261012-040000" / "hub.db",
    ]
    dirs = [paths.home, paths.logs, paths.backups, paths.backups / "auto-20261012-040000"]
    for directory in dirs[1:]:
        directory.mkdir(parents=True, exist_ok=True)
    for file in files:
        file.write_text("x")
        file.chmod(0o644)
    for directory in dirs:
        directory.chmod(0o755)
    return [*files, *dirs]


def test_everything_that_was_loosened_is_tightened_again(paths):
    loose = make_loose_house(paths)
    fixed = fix_permissions(paths)

    assert sorted(fixed) == sorted(loose)
    for path in loose:
        assert mode(path) == (0o700 if path.is_dir() else 0o600), path


def test_fixing_twice_changes_nothing_the_second_time(paths):
    make_loose_house(paths)
    fix_permissions(paths)
    assert fix_permissions(paths) == []


def test_files_that_are_not_there_are_ignored(paths):
    paths.home.chmod(0o700)
    assert fix_permissions(paths) == []


def test_stricter_modes_are_left_alone(paths):
    paths.account.write_text("x")
    paths.account.chmod(0o400)
    paths.config.write_text("x")
    paths.config.chmod(0o600)
    assert fix_permissions(paths) == []
    assert mode(paths.account) == 0o400


def test_only_the_group_and_other_bits_count_as_too_wide(paths):
    paths.account.write_text("x")
    for wide in (0o640, 0o604, 0o660, 0o606, 0o666, 0o644, 0o755):
        paths.account.chmod(wide)
        assert fix_permissions(paths) == [paths.account], oct(wide)
        assert mode(paths.account) == 0o600


def test_files_in_logs_and_backups_are_covered_however_deep(paths):
    deep = paths.backups / "pre-upgrade-v1-v2-20261012-100000" / "nested"
    deep.mkdir(parents=True)
    (deep / "file.bin").write_text("x")
    (deep / "file.bin").chmod(0o644)
    deep.chmod(0o755)
    (paths.logs).mkdir()
    (paths.logs / "hub.log.1").write_text("x")
    (paths.logs / "hub.log.1").chmod(0o666)

    fixed = fix_permissions(paths)

    assert deep in fixed and deep / "file.bin" in fixed and paths.logs / "hub.log.1" in fixed
    assert mode(deep) == 0o700 and mode(deep / "file.bin") == 0o600
    assert mode(paths.logs / "hub.log.1") == 0o600


def test_symbolic_links_are_not_followed(paths, tmp_path):
    target = tmp_path / "elsewhere.txt"
    target.write_text("not ours")
    target.chmod(0o644)
    linked_dir = tmp_path / "linked-dir"
    linked_dir.mkdir()
    linked_dir.chmod(0o755)
    paths.account.symlink_to(target)
    paths.backups.mkdir(mode=0o700)
    (paths.backups / "link").symlink_to(linked_dir)

    assert fix_permissions(paths) == []
    assert mode(target) == 0o644
    assert mode(linked_dir) == 0o755


def test_a_logs_or_backups_directory_that_is_a_link_is_not_entered(paths, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "file.txt").write_text("not ours")
    (elsewhere / "file.txt").chmod(0o644)
    paths.logs.symlink_to(elsewhere, target_is_directory=True)
    paths.backups.symlink_to(elsewhere, target_is_directory=True)

    assert fix_permissions(paths) == []
    assert mode(elsewhere / "file.txt") == 0o644


def test_a_file_where_a_directory_belongs_is_left_alone(paths):
    paths.logs.write_text("a file, not a directory")
    paths.logs.chmod(0o644)
    paths.backups.write_text("a file, not a directory")
    paths.backups.chmod(0o644)

    assert fix_permissions(paths) == []
    assert mode(paths.logs) == 0o644 and mode(paths.backups) == 0o644


def test_the_data_directory_itself_is_covered(paths):
    paths.home.chmod(0o755)
    assert fix_permissions(paths) == [paths.home]
    assert mode(paths.home) == 0o700


# ---------------------------------------------------------------- 只看不改（doctor 用）


def test_the_scan_lists_what_fixing_would_change_and_changes_nothing(paths):
    loose = make_loose_house(paths)

    found = wide_permissions(paths)

    assert sorted(item.path for item in found) == sorted(loose)
    for item in found:
        assert mode(item.path) in (0o644, 0o755)  # 一个字节都没改
        assert item.wanted == (0o700 if item.path.is_dir() else 0o600)
        assert item.mode == mode(item.path)


def test_the_scan_says_what_the_mode_is_and_what_it_should_be(paths):
    paths.account.write_text("x")
    paths.account.chmod(0o640)

    assert wide_permissions(paths) == [WidePermission(paths.account, 0o640, 0o600)]


def test_the_scan_finds_nothing_in_a_tidy_house(paths):
    paths.account.write_text("x")
    paths.account.chmod(0o600)

    assert wide_permissions(paths) == []
