"""额度测试共用的夹具和小函数（数据库层：窗口、预占、对账、暂停）。"""

from __future__ import annotations

import dataclasses
import threading

import pytest

from clawbot_hub.config import Config
from clawbot_hub.projects import Projects
from clawbot_hub.quota import Deny, Hold, PartResult, Quota, Request, Reservation
from tests.projects.conftest import events, register
from tests.support.threads import run_in_thread

__all__ = [
    "HOUR",
    "MINUTE",
    "NOT_SENT",
    "REJECTED",
    "SENT",
    "UNCONFIRMED",
    "events",
    "hold_of",
    "insert_outbox",
    "race",
    "register",
    "reserve",
    "settle",
    "speak",
    "usage",
    "window_row",
    "with_quota",
]

MINUTE = 60_000
HOUR = 60 * MINUTE

SENT = PartResult.SENT
UNCONFIRMED = PartResult.UNCONFIRMED
REJECTED = PartResult.REJECTED
NOT_SENT = PartResult.NOT_SENT


@pytest.fixture
def config() -> Config:
    """默认配置：窗口 24 小时、10 条，回复保留 2，可延后至少留 3，暂停 30 分钟，发送限时 30 秒。"""
    return Config()


@pytest.fixture
def quota(config, clock) -> Quota:
    return Quota(config, clock)


@pytest.fixture
def projects(database, config, clock) -> Projects:
    return Projects(database, config, clock)


def with_quota(config: Config, **kwargs) -> Config:
    """改几个额度配置项，别的不动。"""
    return dataclasses.replace(config, quota=dataclasses.replace(config.quota, **kwargs))


def speak(database, quota: Quota, started_ms: int, token: str = "tok") -> int | None:
    """使用者发来一条消息：在自己的事务里开窗口（旧消息不倒回时返回 `None`）。"""
    with database.txn() as conn:
        return quota.open_window(conn, started_ms, token)


def reserve(
    database, quota: Quota, project: str, n: int = 1, *, kind: str = "push", **kwargs
) -> Reservation | Deny:
    """在自己的事务里预占一次。"""
    outbox_id = kwargs.pop("outbox_id", None)
    with database.txn() as conn:
        return quota.reserve(conn, Request(project, n=n, kind=kind, **kwargs), outbox_id=outbox_id)


def usage(database, window_id: int | None = None) -> dict[tuple[str, str], int]:
    """某个窗口（默认最新的那个）里各项目已用的条数：{(项目名, 推送或回复): 条数}，0 条的不列。"""
    conn = database.conn()
    if window_id is None:
        row = conn.execute("SELECT MAX(id) AS id FROM windows").fetchone()
        window_id = row["id"]
    rows = conn.execute(
        "SELECT p.name, u.kind, u.n FROM usage u JOIN projects p ON p.id = u.project_id"
        " WHERE u.window_id = ? AND u.n > 0",
        (window_id,),
    ).fetchall()
    return {(row["name"], row["kind"]): row["n"] for row in rows}


def insert_outbox(
    conn, outbox_id: str, project: str, *, kind: str = "push", parts: int = 1, now: int = 0
) -> None:
    """建一条"发送中"的发送记录（真正的发送记录由 B1 的 sender 建，这里只要有这一行）。"""
    project_id = conn.execute("SELECT id FROM projects WHERE name = ?", (project,)).fetchone()["id"]
    conn.execute(
        "INSERT INTO outbox (id, project_id, kind, priority, parts, status, created_ms)"
        " VALUES (?, ?, ?, 'normal', ?, 'sending', ?)",
        (outbox_id, project_id, kind, parts, now),
    )


def settle(
    database, quota: Quota, reservation: Reservation, result: PartResult, **kwargs
) -> Hold | None:
    """对账一次，在自己的小事务里（发完一条就记一条）。返回这一次带来的暂停（没有就是 `None`）。"""
    with database.txn() as conn:
        return quota.settle(conn, reservation, result, **kwargs)


def window_row(database, window_id: int):
    return database.conn().execute("SELECT * FROM windows WHERE id = ?", (window_id,)).fetchone()


def hold_of(database, window_id: int) -> tuple[int | None, str | None]:
    """窗口的暂停：(截止时刻, 腾讯给的代码)。"""
    row = window_row(database, window_id)
    return row["blocked_until_ms"], row["blocked_code"]


def race(database, quota: Quota, names: list[str]) -> list:
    """几个推送请求同时到达（各用各的线程和事务），返回各自的结果。"""
    barrier = threading.Barrier(len(names))

    def go(name: str):
        barrier.wait()
        with database.txn() as conn:
            return quota.reserve(conn, Request(name))

    workers = [run_in_thread(go, name) for name in names]
    return [worker.result() for worker in workers]
