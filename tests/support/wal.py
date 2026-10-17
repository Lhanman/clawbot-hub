"""造一份"写的一方被杀掉、日志没来得及合并"的数据库：库文件旁边有 `-wal`，没有任何进程开着它。

只读检查（`hubctl doctor`）的"只看不改"要在这种库上才测得出来：有别的连接开着的时候，哪个连接关闭
都不会去合并日志，用普通连接读也看不出差别。写的一方在另一个进程里（`os._exit`，不关连接、不合并），
所以测试进程自己手上没有这个库的连接。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_WRITER = """
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("PRAGMA journal_mode = WAL")
conn.execute("PRAGMA wal_autocheckpoint = 0")
conn.execute(sys.argv[2])
os._exit(0)  # 不 close、不合并日志，就像被 kill -9 了一样
"""


def wal_of(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + "-wal")


def leave_log_behind(db_path: Path, sql: str) -> None:
    """另起一个进程执行 `sql`（提交了），然后不关连接直接结束。调用之前，测试自己的连接要先关掉。"""
    subprocess.run([sys.executable, "-I", "-c", _WRITER, str(db_path), sql], check=True, timeout=30)
    assert wal_of(db_path).stat().st_size > 0, "没造出来：日志是空的"
