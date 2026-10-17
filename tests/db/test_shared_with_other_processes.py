"""别的进程开库、关库，不能让这个进程里还开着的连接出事（任务 E1，核对安装说明时发现的）。

`hubctl status`、`project list` 这类命令和常驻的服务同时用一个库：服务里每个线程各开一个连接，
线程是后来才有的；命令那边每次开了又关。SQLite 靠进程在库文件上持有的锁知道"我是不是最后一个
关库的"，最后一个关库的会把 `-wal` 和 `-shm` 合并删掉。POSIX 的规矩是：**同一个进程里对这个文件
做任何一次普通的打开再关闭（`os.open` 加 `os.close`），这个进程在它上面的所有锁就一起放掉了**，
包括 SQLite 的连接持有的。锁没了，别的进程就以为自己是最后一个，把日志文件从还在用它的连接脚下
删掉，之后读写报 `disk I/O error`、`database disk image is malformed`。

所以建连接时不能碰已经有的库文件（只在文件还不存在时先以 0600 建出来）。这些测试用真的另一个
进程来开库、关库——同一个进程里的两个连接互相看得见锁，测不出这个毛病。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from clawbot_hub.db import Database, connect

OTHER_PROCESS = """\
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute("SELECT count(*) FROM thing").fetchall()
conn.close()
"""


def other_process_opens_and_closes(path: Path) -> None:
    """像 `hubctl status` 那样：开一个普通连接，读一下，关掉。它是库的最后一个使用者的话，
    关的时候会合并并删掉日志文件。"""
    subprocess.run([sys.executable, "-c", OTHER_PROCESS, str(path)], check=True, timeout=60)


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT count(*) FROM thing").fetchone()[0]


@pytest.fixture
def first(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "hub.db")
    conn.execute("CREATE TABLE thing (n INTEGER)")
    conn.execute("INSERT INTO thing VALUES (1)")
    yield conn
    conn.close()


def wal_files(tmp_path: Path) -> tuple[bool, bool]:
    return (tmp_path / "hub.db-wal").exists(), (tmp_path / "hub.db-shm").exists()


def test_another_process_closing_its_connection_leaves_a_live_connection_alone(tmp_path, first):
    other_process_opens_and_closes(tmp_path / "hub.db")

    assert wal_files(tmp_path) == (True, True)
    first.execute("INSERT INTO thing VALUES (2)")
    assert count(first) == 2


def test_making_more_connections_here_does_not_loosen_the_first_ones_locks(tmp_path, first):
    """服务里每个线程第一次要库的时候开一个新连接。"""
    second = connect(tmp_path / "hub.db")
    third = connect(tmp_path / "hub.db")
    try:
        other_process_opens_and_closes(tmp_path / "hub.db")

        assert wal_files(tmp_path) == (True, True)
        first.execute("INSERT INTO thing VALUES (2)")
        assert count(second) == 2
        assert count(third) == 2
        assert count(connect(tmp_path / "hub.db")) == 2  # 之后开的连接看到的也是同一份
    finally:
        second.close()
        third.close()


def test_a_connection_made_by_a_late_thread_does_not_loosen_the_others_locks(tmp_path):
    """和上一条一样，走的是服务用的 `Database`：每个线程各一个连接，线程是后来才来的。"""
    db = Database(tmp_path / "hub.db")
    try:
        with db.txn() as conn:
            conn.execute("CREATE TABLE thing (n INTEGER)")
            conn.execute("INSERT INTO thing VALUES (1)")
        late: list[int] = []
        worker = threading.Thread(target=lambda: late.append(count(db.conn())))
        worker.start()
        worker.join(30)
        assert late == [1]

        other_process_opens_and_closes(tmp_path / "hub.db")

        assert wal_files(tmp_path) == (True, True)
        with db.txn() as conn:
            conn.execute("INSERT INTO thing VALUES (2)")
        assert count(db.conn()) == 2
    finally:
        db.close()


def test_connecting_does_not_open_a_database_file_that_is_already_there(tmp_path, monkeypatch):
    """出问题的根子：对已经有的库文件做一次 `os.open` 加 `os.close`。这里直接盯着它。"""
    path = tmp_path / "hub.db"
    connect(path).close()
    opened: list[str] = []
    real_open = os.open

    def watching(target, *args, **kwargs):
        if os.fspath(target) == os.fspath(path):
            opened.append(os.fspath(target))
        return real_open(target, *args, **kwargs)

    monkeypatch.setattr(os, "open", watching)

    connect(path).close()
    Database(path).conn().close()

    assert opened == []


def test_a_missing_database_file_is_made_with_owner_only_permissions(tmp_path):
    path = tmp_path / "hub.db"
    old_mask = os.umask(0o022)  # 默认的 umask 会让新文件人人可读
    try:
        conn = connect(path)
    finally:
        os.umask(old_mask)
    try:
        conn.execute("CREATE TABLE thing (n INTEGER)")
        for name in ("hub.db", "hub.db-wal", "hub.db-shm"):
            assert (tmp_path / name).stat().st_mode & 0o777 == 0o600, name
    finally:
        conn.close()


def test_an_existing_database_file_keeps_its_content_and_mode(tmp_path):
    path = tmp_path / "hub.db"
    conn = connect(path)
    conn.execute("CREATE TABLE thing (n INTEGER)")
    conn.execute("INSERT INTO thing VALUES (1)")
    conn.close()
    path.chmod(0o640)
    size = path.stat().st_size

    again = connect(path)
    try:
        assert count(again) == 1
    finally:
        again.close()

    assert path.stat().st_size >= size
    assert path.stat().st_mode & 0o777 == 0o640  # 建连接不改已有文件的权限（放宽的由启动自检修回）
