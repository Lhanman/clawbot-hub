"""事件记录：把值得事后查的事写进 `events` 表（时间、级别、种类、项目、一句话，**没有正文**）。

种类是封闭的集合（与数据库里的 CHECK 一致），级别用日志的三个名字。事件写在调用者自己的
事务里，所以"状态变了"和"留下一条记录"要么都成、要么都不成。

规格：`hub-cli`「事件记录」。
"""

from __future__ import annotations

import sqlite3

KINDS = ("login", "alert", "send", "hold", "project", "queue", "backup", "lifecycle")
LEVELS = ("INFO", "WARNING", "ERROR")


def add_event(
    conn: sqlite3.Connection,
    ts_ms: int,
    kind: str,
    detail: str,
    *,
    level: str = "INFO",
    project: str | None = None,
) -> None:
    """在 `conn` 当前的事务里写一条事件。`detail` 里不要放登录凭证、密钥和消息正文。"""
    if kind not in KINDS:
        raise ValueError(f"事件种类不认识：{kind!r}（可用：{'、'.join(KINDS)}）")
    if level not in LEVELS:
        raise ValueError(f"事件级别不认识：{level!r}（可用：{'、'.join(LEVELS)}）")
    if not detail.strip():
        raise ValueError("事件内容不能为空")
    conn.execute(
        "INSERT INTO events (ts_ms, level, kind, project, detail) VALUES (?, ?, ?, ?, ?)",
        (ts_ms, level, kind, project, detail),
    )
