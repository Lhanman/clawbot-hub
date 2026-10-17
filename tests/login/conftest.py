"""登录相关测试共用的小工具：造凭证文件内容、往库里放窗口和待取记录、读登录状态。"""

from __future__ import annotations

import subprocess
import sys

import pytest

from clawbot_hub.account import AccountFile, AccountStore
from clawbot_hub.loginstate import LoginMonitor, read_login

TOKEN = "tok-secret-111"


def account_file(**overrides) -> AccountFile:
    fields = {
        "bot_token": TOKEN,
        "user_id": "owner@im.wechat",
        "base_url": "https://ilink.example",
        "bot_id": "bot@im.bot",
        "bound_at": "2026-10-12T10:00:00+08:00",
        "updates_buf": "",
    }
    fields.update(overrides)
    return AccountFile(**fields)


@pytest.fixture
def store(paths) -> AccountStore:
    return AccountStore(paths)


def seed_conversation(db, *, windows: int = 2, handles: int = 2) -> None:
    """放 `windows` 个开着的窗口（各带对话凭证）、一个早已作废的窗口，
    和 `handles` 条带对话凭证的待取记录。"""
    with db.txn() as conn:
        for number in range(windows):
            conn.execute(
                "INSERT INTO windows (started_ms, context_token) VALUES (?, ?)",
                (1_000 + number, f"window-token-{number}"),
            )
        conn.execute(
            "INSERT INTO windows (started_ms, context_token, voided) VALUES (500, NULL, 1)"
        )
        project = conn.execute("SELECT id FROM projects WHERE name = '_hub'").fetchone()["id"]
        for number in range(handles):
            conn.execute(
                "INSERT INTO inbox (id, project_id, text, has_text, at_ms, received_ms, via, state,"
                " handle, context_token, handle_expires_ms) VALUES (?, ?, ?, 1, 1, 1, 'default',"
                " 'acked', ?, ?, 99)",
                (f"m{number}", project, f"正文{number}", f"rh_{number}", f"inbox-token-{number}"),
            )


def conversation_tokens(db) -> dict[str, list]:
    conn = db.conn()
    return {
        "windows": [
            (r["started_ms"], r["context_token"], r["voided"])
            for r in conn.execute("SELECT * FROM windows ORDER BY id")
        ],
        "inbox": [
            (
                r["id"],
                r["text"],
                r["state"],
                r["handle"],
                r["context_token"],
                r["handle_expires_ms"],
            )
            for r in conn.execute("SELECT * FROM inbox ORDER BY id")
        ],
    }


def login_row(db):
    return read_login(db.conn())


class Calls:
    """登录监视器的两个回调（失效、恢复）各被叫了几次、带着什么。"""

    def __init__(self) -> None:
        self.expired: list[int] = []
        self.restored: list[int] = []


@pytest.fixture
def calls() -> Calls:
    return Calls()


@pytest.fixture
def monitor(paths, database, store, clock, calls) -> LoginMonitor:
    return LoginMonitor(
        paths,
        database,
        store,
        clock,
        on_expired=calls.expired.append,
        on_restored=calls.restored.append,
    )


def events(db) -> list[dict]:
    return [dict(r) for r in db.conn().execute("SELECT * FROM events ORDER BY id")]


def dead_pid() -> int:
    """一个刚刚结束、已经被回收的子进程的号码（用来造"进程已经不在"的标记）。"""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid
