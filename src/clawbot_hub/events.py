"""事件记录：把值得事后查的事写进 `events` 表（时间、级别、种类、项目、一句话，**没有正文**）。

种类是封闭的集合（与数据库里的 CHECK 一致），级别用日志的三个名字。事件写在调用者自己的
事务里，所以"状态变了"和"留下一条记录"要么都成、要么都不成。

规格：`hub-cli`「事件记录」。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import tzinfo

from clawbot_hub.timefmt import clock_text, iso_text

KINDS = ("login", "alert", "send", "hold", "project", "queue", "backup", "lifecycle")
LEVELS = ("INFO", "WARNING", "ERROR")
DEFAULT_LIMIT = 50  # `hubctl events` 不带 `--limit` 时列几条（规格定死的数）


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


@dataclass(frozen=True)
class EventRow:
    """库里的一条事件，给 `hubctl events` 列出来。"""

    id: int
    ts_ms: int
    level: str
    kind: str
    project: str | None
    detail: str

    def line(self, tz: tzinfo) -> str:
        """`时间  级别  种类  项目  一句话`，列之间两个空格，没有项目写 `-`。"""
        columns = (clock_text(self.ts_ms, tz), self.level, self.kind, self.project or "-")
        return "  ".join((*columns, self.detail))

    def to_json(self, tz: tzinfo) -> dict[str, object]:
        return {
            "id": self.id,
            "time": iso_text(self.ts_ms, tz),
            "level": self.level,
            "kind": self.kind,
            "project": self.project,
            "detail": self.detail,
        }


def recent_events(
    conn: sqlite3.Connection, *, limit: int = DEFAULT_LIMIT, kind: str | None = None
) -> list[EventRow]:
    """最近的 `limit` 条（可以只挑一个种类），旧的在前、新的在后；同一时刻的按写入先后。"""
    sql, args = "SELECT id, ts_ms, level, kind, project, detail FROM events", []
    if kind is not None:
        sql, args = sql + " WHERE kind = ?", [kind]
    rows = conn.execute(sql + " ORDER BY ts_ms DESC, id DESC LIMIT ?", [*args, limit]).fetchall()
    return [
        EventRow(r["id"], r["ts_ms"], r["level"], r["kind"], r["project"], r["detail"] or "")
        for r in reversed(rows)
    ]
