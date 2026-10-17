"""把数据库弄坏的辅助函数：先灌进足够多的行，再往中间的页里写随机字节。"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

PAGE_SIZE = 4096


def fill_with_events(conn: sqlite3.Connection, count: int = 3000) -> None:
    """往 events 里灌 `count` 行，让库有几十页，并把 WAL 并回主文件。"""
    conn.execute("BEGIN")
    for i in range(count):
        conn.execute(
            "INSERT INTO events (ts_ms, level, kind, detail) VALUES (?, 'info', 'send', ?)",
            (i, "z" * 60),
        )
    conn.execute("COMMIT")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def scramble_pages(path: Path, first_page: int = 5, step: int = 3) -> None:
    """从第 `first_page` 页起每隔 `step` 页，在页里写 100 个随机字节（第 1 页不动，库还打得开）。"""
    pages = path.stat().st_size // PAGE_SIZE
    with open(path, "r+b") as f:
        for page in range(first_page, pages, step):
            f.seek(PAGE_SIZE * page + 50)
            f.write(os.urandom(100))


def remove_side_files(path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
