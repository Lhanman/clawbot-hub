"""收话测试共用的夹具和小函数：数据库、假时钟、项目、待取队列。

`World` 把项目表和待取队列接在一起，再加几个随手用的小函数：

- `add()`：登记一个项目（没给的项用小值，见 `tests/projects/conftest.py`）；
- `say()`：使用者说了一句话，交给某个项目——在一个事务里入队，提交以后叫醒等着的取话请求，
  相当于 poller 做的事（poller 本身归 C3）；
- `row()`、`rows()`、`events()`：直接读库。
"""

from __future__ import annotations

import dataclasses
import itertools
import sqlite3

import pytest

from clawbot_hub.config import Config
from clawbot_hub.inbox import Inbox
from clawbot_hub.projects import Project, Projects
from tests.projects.conftest import register

__all__ = ["World"]


class World:
    def __init__(self, database, clock, config: Config | None = None) -> None:
        self.db = database
        self.clock = clock
        self.config = config or Config()
        self.projects = Projects(database, self.config, clock)
        self.inbox = Inbox(database, self.config, clock, self.projects)
        self._tokens = itertools.count(1)

    def add(self, name: str, **kwargs) -> Project:
        return register(self.projects, name, **kwargs).project

    def say(
        self,
        project: Project,
        text: str = "持仓",
        *,
        at_ms: int | None = None,
        received_ms: int | None = None,
        via: str = "default",
        has_text: bool = True,
        token: str | None = None,
        wake: bool = True,
    ):
        """使用者说了一句话，路由给 `project`。返回 `inbox.enqueue` 的结果。"""
        now = self.clock.now_ms()
        with self.db.txn() as conn:
            result = self.inbox.enqueue(
                conn,
                project,
                text=text,
                has_text=has_text,
                at_ms=now if at_ms is None else at_ms,
                received_ms=now if received_ms is None else received_ms,
                via=via,
                context_token=token or f"ctx-{next(self._tokens)}",
            )
        if wake:
            self.inbox.wake(project.id)
        return result

    def row(self, item_id: str) -> sqlite3.Row:
        return self.db.conn().execute("SELECT * FROM inbox WHERE id = ?", (item_id,)).fetchone()

    def rows(self, project: Project | None = None) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM inbox", ()
        if project is not None:
            sql, args = sql + " WHERE project_id = ?", (project.id,)
        return self.db.conn().execute(sql + " ORDER BY rowid", args).fetchall()

    def states(self, project: Project | None = None) -> list[str]:
        return [row["state"] for row in self.rows(project)]

    def events(self, kind: str = "queue") -> list[dict]:
        rows = self.db.conn().execute("SELECT * FROM events WHERE kind = ? ORDER BY id", (kind,))
        return [dict(row) for row in rows]

    def project(self, name: str) -> Project:
        return self.projects.get(name)

    def lease_all(self, project: Project, n: int = 50) -> list:
        return self.inbox.fetch(project, wait=0, max_items=n)


@pytest.fixture
def world(database, clock) -> World:
    return World(database, clock)


@pytest.fixture
def make_world(database, clock):
    """要改配置时：`make_world(inbox={"max_queued": 3})`。"""

    def build(**sections) -> World:
        config = Config()
        for name, values in sections.items():
            config = dataclasses.replace(
                config, **{name: dataclasses.replace(getattr(config, name), **values)}
            )
        return World(database, clock, config)

    return build
