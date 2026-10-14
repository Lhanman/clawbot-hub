"""收消息循环和服务自己的回复的测试共用的夹具：一整套零件接在一起（`Rig`）。

`Rig` 在 `tests/support/hub.py` 的 `Hub`（数据库、假上游、登录监视器、项目、额度、发送）上，再接
待取队列、服务回复队列（`Replier`，不起线程，测试里用 `flush()` 让它把队里的回复发完）和
`Poller`（不起线程，测试里用 `step()` 收一轮）。

- `say()`：使用者给 ClawBot 发了一条消息（放进假上游的收件箱）；
- `step()`：收一轮，返回下一轮前要等几秒；
- `flush()`：把服务回复队列里的回复发完，返回这次发出去的文字；
- `new_poller()`：服务重启——同一个数据库、同一份凭证文件、新的内存状态；
- `rows()`、`window()`、`account_state()`：直接读库。
"""

from __future__ import annotations

import dataclasses
import sqlite3
from typing import Any

import pytest

from clawbot_hub.config import Config
from clawbot_hub.inbox import Inbox
from clawbot_hub.poller import Poller
from clawbot_hub.quota import Window
from clawbot_hub.replier import Replier
from clawbot_hub.upstream.base import IncomingMessage
from tests.support.hub import USER, Hub

__all__ = ["Rig", "texts"]


def texts(items) -> list[str]:
    return [item.text for item in items]


class Rig:
    def __init__(self, paths, database, clock, upstream, config: Config | None = None) -> None:
        self.config = config or Config()
        self.clock = clock
        self.db = database
        self.upstream = upstream
        upstream.user_id = USER  # 假上游里的"使用者"就是凭证文件里的 user_id
        self.hub = Hub(paths, database, clock, upstream, self.config)
        self.inbox = Inbox(database, self.config, clock, self.hub.projects)
        self.replier = Replier(self.hub.sender, self.config.hub.label)
        self.poller = self._poller(self.inbox, self.replier)

    def _poller(self, inbox: Inbox, replier: Replier) -> Poller:
        hub = self.hub
        return Poller(
            self.config,
            self.clock,
            self.db,
            self.upstream,
            hub.store,
            hub.monitor,
            hub.projects,
            hub.quota,
            inbox,
            replier,
        )

    # ------------------------------------------------ 准备

    def bind(self, **overrides: str):
        return self.hub.bind(**overrides)

    def add(self, name: str, **kwargs: Any):
        return self.hub.add(name, **kwargs)

    def heartbeat(self, project) -> None:
        """让这个项目在线（它取了一次话）。"""
        self.inbox.fetch(project, wait=0, max_items=0)

    def new_poller(self) -> Poller:
        """服务重启：新的待取队列对象、新的服务回复队列、新的 poller，数据库和凭证文件还是原来的；
        启动时先比对一次凭证（读进文件里的进度），再开始收。"""
        self.hub.monitor.sync()
        self.inbox = Inbox(self.db, self.config, self.clock, self.hub.projects)
        self.replier = Replier(self.hub.sender, self.config.hub.label)
        self.poller = self._poller(self.inbox, self.replier)
        return self.poller

    # ------------------------------------------------ 驱动

    def say(self, text: str = "持仓", **kwargs: Any) -> IncomingMessage:
        return self.upstream.push_user_message(text, **kwargs)

    def step(self) -> float:
        return self.poller.step()

    def flush(self) -> list[str]:
        """把服务回复队列里现有的回复发完（在测试自己的线程里），返回这次发给腾讯的文字。"""
        before = len(self.upstream.sent)
        while self.replier.process_one():
            pass
        return [message.text for message in self.upstream.sent[before:]]

    # ------------------------------------------------ 读库

    def rows(self) -> list[sqlite3.Row]:
        return self.db.conn().execute("SELECT * FROM inbox ORDER BY rowid").fetchall()

    def window(self) -> Window | None:
        return self.hub.quota.current_window(self.db.conn())

    def windows(self) -> list[sqlite3.Row]:
        return self.db.conn().execute("SELECT * FROM windows ORDER BY id").fetchall()

    def account_state(self) -> sqlite3.Row:
        return self.db.conn().execute("SELECT * FROM account_state WHERE id = 1").fetchone()

    def usage(self) -> dict[tuple[str, str], int]:
        rows = self.db.conn().execute(
            "SELECT p.name AS name, u.kind AS kind, u.n AS n FROM usage u"
            " JOIN projects p ON p.id = u.project_id"
        )
        return {(row["name"], row["kind"]): row["n"] for row in rows}

    def notices(self) -> dict[str, int]:
        rows = self.db.conn().execute("SELECT key, last_ms FROM notice_state")
        return {row["key"]: row["last_ms"] for row in rows}

    def events(self, kind: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM events", ()
        if kind is not None:
            sql, args = sql + " WHERE kind = ?", (kind,)
        return [dict(row) for row in self.db.conn().execute(sql + " ORDER BY id", args)]


@pytest.fixture
def make_rig(paths, database, clock, fake_upstream):
    """要改配置时：`make_rig(inbox={"offline_notice_seconds": 60})`。"""

    def build(**sections: dict[str, Any]) -> Rig:
        config = Config()
        for name, values in sections.items():
            config = dataclasses.replace(
                config, **{name: dataclasses.replace(getattr(config, name), **values)}
            )
        return Rig(paths, database, clock, fake_upstream, config)

    return build


@pytest.fixture
def rig(make_rig) -> Rig:
    return make_rig()
