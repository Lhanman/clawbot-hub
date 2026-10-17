"""组件测试共用的"一整套"：数据库、假上游、登录监视器、项目、额度、发送，外加几个随手用的小函数。

`Hub` 只是把已经各自测过的零件接在一起，方便发送、接口这些要用到全套东西的测试：

- `bind()`：写一份凭证文件并让监视器读进去（登录状态变成正常）；默认把"刚换过凭证"的宽限
  清掉，免得每个测试都得先等 60 秒假时间才能走到"登录真的失效了"那条路；
- `speak()`：使用者说了一句话（开窗口，对话凭证 `ctx-<n>`）；
- `add()`：登记一个项目（没给的项用小值，见 `tests/projects/conftest.py`），密钥记在 `keys[名字]`；
- `ready()`：上面三件事一起做，返回项目；
- `expire()`、`unbind()`、`corrupt()`：把登录状态弄成失效、没绑定、凭证文件读不出来；
- `fill()`：直接往窗口里记几条用量（不碰假上游），造"额度快用完"的场面；
- `rows()`、`used()`、`events()`：读发送记录、窗口用量、事件。
"""

from __future__ import annotations

import dataclasses
import itertools
import sqlite3
from collections.abc import Callable
from typing import Any

from clawbot_hub.account import AccountFile, AccountStore
from clawbot_hub.config import Config
from clawbot_hub.loginstate import LoginMonitor, Verdict
from clawbot_hub.projects import Project, Projects
from clawbot_hub.quota import PUSH, REPLY, Deny, Quota, Request
from clawbot_hub.sender import Sender
from tests.projects.conftest import register

USER = "owner@im.wechat"
BOT_TOKEN = "tok-bot-secret-1"

__all__ = ["BOT_TOKEN", "USER", "Hub", "tweak"]


def tweak(config: Config, **sections: dict[str, Any]) -> Config:
    """改几个配置项：`tweak(config, send={"retries": 0}, upstream={"confirm": "strict"})`。"""
    changed = {
        name: dataclasses.replace(getattr(config, name), **values)
        for name, values in sections.items()
    }
    return dataclasses.replace(config, **changed)


class Hub:
    def __init__(
        self,
        paths,
        database,
        clock,
        upstream,
        config: Config | None = None,
        *,
        on_expired: Callable[[int], None] | None = None,
        on_restored: Callable[[int], None] | None = None,
        **sender_kwargs: Any,
    ) -> None:
        """`on_expired`、`on_restored` 是登录监视器的回调（告警测试接进来用）；`hub.expired`、
        `hub.restored` 两个列表照旧记着每一次回调。"""
        self.paths = paths
        self.db = database
        self.clock = clock
        self.upstream = upstream
        self.config = config or Config()
        self.store = AccountStore(paths)
        self.expired: list[int] = []
        self.restored: list[int] = []
        self.holds: list = []
        self.keys: dict[str, str] = {}

        def expired_hook(episode: int) -> None:
            self.expired.append(episode)
            if on_expired is not None:
                on_expired(episode)

        def restored_hook(episode: int) -> None:
            self.restored.append(episode)
            if on_restored is not None:
                on_restored(episode)

        self.monitor = LoginMonitor(
            paths,
            database,
            self.store,
            clock,
            on_expired=expired_hook,
            on_restored=restored_hook,
        )
        self.projects = Projects(database, self.config, clock)
        self.quota = Quota(self.config, clock)
        sender_kwargs.setdefault("on_hold", self.holds.append)
        self.sender = Sender(database, self.config, clock, upstream, self.monitor, **sender_kwargs)
        self._speeches = itertools.count(1)

    # ------------------------------------------------ 准备

    def bind(self, *, aged: bool = True, **overrides: str) -> AccountFile:
        fields = {
            "bot_token": BOT_TOKEN,
            "user_id": USER,
            "base_url": "https://ilink.example",
            "bot_id": "bot@im.bot",
            "bound_at": "2026-10-12T10:00:00+08:00",
            "updates_buf": "",
        }
        fields.update(overrides)
        account = AccountFile(**fields)
        self.store.write(account)
        self.monitor.sync()
        if aged:
            with self.db.txn() as conn:
                conn.execute("UPDATE account_state SET creds_changed_ms = NULL WHERE id = 1")
        return account

    def speak(self, at_ms: int | None = None, token: str | None = None) -> int | None:
        """使用者说了一句话。`at_ms` 默认是现在，对话凭证默认依次是 `ctx-1`、`ctx-2`……"""
        if token is None:
            token = f"ctx-{next(self._speeches)}"
        with self.db.txn() as conn:
            return self.quota.open_window(
                conn, self.clock.now_ms() if at_ms is None else at_ms, token
            )

    def add(self, name: str, **kwargs: Any) -> Project:
        registration = register(self.projects, name, **kwargs)
        self.keys[name] = registration.key
        return registration.project

    def ready(self, name: str = "alpha", **kwargs: Any) -> Project:
        """绑定、使用者说一句话、登记项目（保底 6、上限 8），一步到位。"""
        kwargs.setdefault("guaranteed", 6)
        kwargs.setdefault("cap", 8)
        self.bind()
        self.speak()
        return self.add(name, **kwargs)

    def project(self, name: str) -> Project:
        return self.projects.get(name)

    def expire(self) -> None:
        """腾讯回过 -14：登录状态变成失效（凭证没换，换凭证的宽限也已经清掉）。"""
        verdict = self.monitor.session_expired(self.monitor.account().digest)
        assert verdict is Verdict.EXPIRED, verdict

    def unbind(self) -> None:
        """凭证文件不见了（还没绑定）。"""
        self.paths.account.unlink()
        self.monitor.sync()

    def corrupt(self) -> None:
        """凭证文件读不出来。"""
        self.paths.account.write_text("这不是 JSON", encoding="utf-8")
        self.monitor.sync()

    def fill(self, n: int, *, project: str = "_hub", kind: str = REPLY) -> None:
        """窗口里直接记上 n 条用量（走额度的预占，不碰假上游）。默认记成服务自己的回复。"""
        assert kind in (PUSH, REPLY)
        with self.db.txn() as conn:
            reservation = self.quota.reserve(conn, Request(project, n=n, kind=kind))
            assert not isinstance(reservation, Deny), reservation

    # ------------------------------------------------ 看

    def rows(self) -> list[sqlite3.Row]:
        return self.db.conn().execute("SELECT * FROM outbox ORDER BY created_ms, rowid").fetchall()

    def row(self, outbox_id: str) -> sqlite3.Row:
        return self.db.conn().execute("SELECT * FROM outbox WHERE id = ?", (outbox_id,)).fetchone()

    def used(self, window_id: int | None = None) -> dict[tuple[str, str], int]:
        """某个窗口（默认最新的）里各项目已用的条数：{(项目名, 推送或回复): 条数}，0 条的不列。"""
        conn = self.db.conn()
        if window_id is None:
            window_id = conn.execute("SELECT MAX(id) AS id FROM windows").fetchone()["id"]
        rows = conn.execute(
            "SELECT p.name, u.kind, u.n FROM usage u JOIN projects p ON p.id = u.project_id"
            " WHERE u.window_id = ? AND u.n > 0",
            (window_id,),
        ).fetchall()
        return {(r["name"], r["kind"]): r["n"] for r in rows}

    def events(self, kind: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM events", ()
        if kind is not None:
            sql, args = sql + " WHERE kind = ?", (kind,)
        return [dict(r) for r in self.db.conn().execute(sql + " ORDER BY id", args)]

    @property
    def texts(self) -> list[str]:
        """假上游收到的每一条的文字（含重试）。"""
        return self.upstream.sent_texts()
