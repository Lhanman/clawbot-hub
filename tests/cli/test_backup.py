"""`hubctl backup`（任务 D3）：做一份能打开的备份，打印目录和各文件大小，提醒里面有登录凭证。

备份本身（在线备份接口、命名、清理旧的自动备份）在 `tests/test_backup.py` 测过；这里看命令：
默认放哪、`--to` 的规矩（父目录不改权限、云盘路径警告）、服务正忙时能做、出错怎么说。
规格 `hub-cli`「备份」。
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from clawbot_hub.config import Config
from clawbot_hub.events import add_event
from clawbot_hub.quota import Quota
from tests.cli.conftest import add, mode_of
from tests.quota.conftest import reserve, speak

STAMP = "20261012-100000"  # 假时钟的起点（北京时间 2026-10-12 10:00:00）
WARNING = "备份里有登录凭证，不要放进云盘或 git"


@pytest.fixture
def quota(clock) -> Quota:
    return Quota(Config(), clock)


@pytest.fixture
def full_home(run, database, hub_paths, quota, clock):
    """有两个项目、窗口里用了 3 条、四个文件齐全的数据目录。"""
    assert add(run, "alpha", guaranteed=2, cap=3).exit_code == 0
    assert add(run, "beta", prefix="乙", guaranteed=1, cap=2).exit_code == 0
    speak(database, quota, clock.now_ms())
    reserve(database, quota, "alpha", 2)
    reserve(database, quota, "beta", 1)
    hub_paths.account.write_text('{"bot_token": "TOKEN-IN-ACCOUNT"}', encoding="utf-8")
    hub_paths.config.write_text("send:\n  retries: 1\n", encoding="utf-8")
    hub_paths.bark.write_text('{"key": "KEY-IN-BARK"}', encoding="utf-8")
    return hub_paths


def listed(result) -> dict[str, int]:
    """输出里 `文件名：N 字节` 的行。"""
    found = {}
    for line in result.stdout.splitlines():
        name, sep, rest = line.strip().partition("：")
        if sep and rest.endswith(" 字节"):
            found[name] = int(rest.removesuffix(" 字节"))
    return found


# ---------------------------------------------------------------- 做出来的备份


def test_a_backup_opens_and_holds_the_projects_and_the_window_usage(run, full_home):
    result = run("backup")

    assert result.exit_code == 0, result.output
    folder = full_home.backups / STAMP
    copy = sqlite3.connect(folder / "hub.db")
    try:
        assert copy.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        names = {row[0] for row in copy.execute("SELECT name FROM projects")}
        assert {"alpha", "beta"} <= names
        assert copy.execute("SELECT SUM(n) FROM usage").fetchone()[0] == 3
    finally:
        copy.close()


def test_it_prints_the_folder_and_the_size_of_every_file_and_no_contents(run, full_home):
    result = run("backup")

    folder = full_home.backups / STAMP
    assert str(folder) in result.stdout
    sizes = listed(result)
    assert sizes == {
        name: (folder / name).stat().st_size
        for name in ("hub.db", "account.json", "config.yaml", "bark.json")
    }
    assert "TOKEN-IN-ACCOUNT" not in result.output and "KEY-IN-BARK" not in result.output


def test_files_that_do_not_exist_are_skipped(run, database, hub_paths):
    result = run("backup")  # 只有数据库（项目是命令登记的）

    assert result.exit_code == 0, result.output
    assert set(listed(result)) == {"hub.db"}


def test_the_output_warns_that_the_backup_holds_the_login_credentials(run, full_home):
    assert WARNING in run("backup").output


def test_backups_are_private_by_default(run, full_home):
    run("backup")

    folder = full_home.backups / STAMP
    assert mode_of(full_home.backups) == 0o700 and mode_of(folder) == 0o700
    for entry in folder.iterdir():
        assert mode_of(entry) == 0o600, entry.name


def test_a_manual_backup_leaves_an_event_with_the_name_only(run, full_home):
    run("backup")

    events = run("events", "--kind", "backup").stdout
    assert f"手动备份完成：{STAMP}" in events
    assert str(full_home.home) not in events


def test_a_manual_backup_does_not_touch_the_other_backups(run, full_home):
    full_home.backups.mkdir(mode=0o700)
    old = [f"auto-202610{day:02d}-040000" for day in range(1, 21)]  # 比保留的 14 份还多
    for name in [*old, "20261001-120000", "pre-upgrade-v0-v1-20261001-000000"]:
        (full_home.backups / name).mkdir()

    assert run("backup").exit_code == 0

    assert all((full_home.backups / name).is_dir() for name in old)
    assert (full_home.backups / "20261001-120000").is_dir()  # 清理旧备份是服务的事，命令不管


def test_backing_up_twice_in_the_same_second_does_not_overwrite(run, full_home):
    assert run("backup").exit_code == 0
    first = (full_home.backups / STAMP / "hub.db").read_bytes()

    again = run("backup")

    assert again.exit_code == 1
    assert "不覆盖" in again.output
    assert (full_home.backups / STAMP / "hub.db").read_bytes() == first


def test_without_a_database_there_is_nothing_to_back_up(run, hub_paths):
    result = run("backup")

    assert result.exit_code == 1
    assert "没有数据库可备份" in result.output
    assert not hub_paths.backups.exists()


def test_a_backup_works_while_the_service_keeps_writing(run, full_home, database, tmp_path):
    """服务在不停地写（收发都要写库）：备份照样做成，写的一方也没有出错。"""
    stop = threading.Event()
    problems: list[BaseException] = []
    written = [0]

    def keep_writing() -> None:
        while not stop.is_set():
            try:
                with database.txn() as conn:
                    add_event(conn, 1_000 + written[0], "lifecycle", f"忙着写 {written[0]}")
                written[0] += 1
                time.sleep(0.002)  # 一秒几百次，已经比真的服务忙得多
            except BaseException as exc:  # noqa: BLE001 - 收集起来让测试报告
                problems.append(exc)
                return

    writer = threading.Thread(target=keep_writing, daemon=True)
    writer.start()
    try:
        results = [run("backup", "--to", str(tmp_path / f"round-{n}")) for n in range(3)]
    finally:
        stop.set()
        writer.join(10)

    assert [r.exit_code for r in results] == [0, 0, 0], [r.output for r in results]
    assert problems == [] and written[0] > 0
    for n in range(3):
        copy = sqlite3.connect(tmp_path / f"round-{n}" / STAMP / "hub.db")
        try:
            assert copy.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        finally:
            copy.close()


def test_a_backup_that_was_made_is_not_called_failed_just_because_the_event_could_not_be_written(
    run, full_home, home
):
    home.joinpath("config.yaml").write_text("db:\n  busy_timeout_ms: 200\n", encoding="utf-8")
    holder = sqlite3.connect(full_home.db, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")  # 别的程序占着写锁；读和在线备份不受影响
    try:
        result = run("backup")
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert result.exit_code == 0, result.output
    assert (full_home.backups / STAMP / "hub.db").is_file()
    assert "没能写进事件记录" in result.output and WARNING in result.output
    assert "手动备份完成" not in run("events", "--kind", "backup").stdout


# ---------------------------------------------------------------- --to


def test_to_means_the_parent_folder_and_leaves_its_permissions_alone(run, full_home, tmp_path):
    parent = tmp_path / "Documents"
    parent.mkdir()
    parent.chmod(0o755)

    result = run("backup", "--to", str(parent))

    assert result.exit_code == 0, result.output
    folder = parent / STAMP
    assert (folder / "hub.db").is_file()
    assert mode_of(parent) == 0o755  # 已经有的目录不动它的权限
    assert mode_of(folder) == 0o700
    assert all(mode_of(entry) == 0o600 for entry in folder.iterdir())
    assert not full_home.backups.exists()  # 没有同时在数据目录里再放一份
    assert WARNING in result.output and str(folder) in result.stdout


def test_to_creates_a_missing_parent_as_private(run, full_home, tmp_path):
    parent = tmp_path / "somewhere" / "new"

    result = run("backup", "--to", str(parent))

    assert result.exit_code == 0, result.output
    assert mode_of(parent) == 0o700 and (parent / STAMP / "hub.db").is_file()


def test_to_understands_the_home_shortcut(run, full_home, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    result = run("backup", "--to", "~/saved")

    assert result.exit_code == 0, result.output
    assert (tmp_path / "saved" / STAMP / "hub.db").is_file()


def test_to_a_place_that_cannot_be_made_fails_in_one_line(run, full_home, tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("不是目录", encoding="utf-8")

    result = run("backup", "--to", str(blocker / "inside"))

    assert result.exit_code == 1
    assert "建不了存放备份的目录" in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize(
    "folder", ["Library/Mobile Documents/com~apple~CloudDocs/备份", "Library/CloudStorage/Dropbox"]
)
def test_a_cloud_folder_gets_a_loud_warning_but_the_backup_is_still_made(
    run, full_home, tmp_path, folder
):
    parent = tmp_path / folder

    result = run("backup", "--to", str(parent))

    assert result.exit_code == 0, result.output
    assert (parent / STAMP / "hub.db").is_file()
    assert "警告" in result.output and "云盘" in result.output and "同步" in result.output
    assert WARNING in result.output


def test_a_cloud_folder_is_recognised_through_a_symlink_too(run, full_home, tmp_path):
    real = tmp_path / "Library" / "Mobile Documents" / "x"
    real.mkdir(parents=True)
    link = tmp_path / "harmless-name"
    link.symlink_to(real)

    result = run("backup", "--to", str(link))

    assert result.exit_code == 0, result.output
    assert "警告" in result.output


def test_an_ordinary_folder_gets_no_cloud_warning(run, full_home, tmp_path):
    result = run("backup", "--to", str(tmp_path / "plain"))

    assert "云盘会" not in result.output and "警告" not in result.output


def test_the_default_place_never_gets_the_cloud_warning(run, full_home):
    assert "警告" not in run("backup").output
