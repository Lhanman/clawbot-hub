"""项目登记、密钥的测试共用的夹具和小函数。"""

from __future__ import annotations

import dataclasses

import pytest

from clawbot_hub.config import Config, HubConfig
from clawbot_hub.projects import Projects, Registration


@pytest.fixture
def config() -> Config:
    """默认配置：窗口 10 条、回复保留 2、服务自己的指令词"服务"、确认词 对 / 是 / 确认。"""
    return Config()


@pytest.fixture
def projects(database, config, clock) -> Projects:
    return Projects(database, config, clock)


def with_settings(
    config: Config,
    *,
    window_limit: int | None = None,
    reply_reserve: int | None = None,
    hub_label: str | None = None,
    confirm_words: tuple[str, ...] | None = None,
) -> Config:
    """改几个和项目有关的配置项，别的不动。"""
    quota = config.quota
    if window_limit is not None:
        quota = dataclasses.replace(quota, window_limit=window_limit)
    if reply_reserve is not None:
        quota = dataclasses.replace(quota, reply_reserve=reply_reserve)
    routing = config.routing
    if confirm_words is not None:
        routing = dataclasses.replace(routing, confirm_words=confirm_words)
    hub = HubConfig(label=hub_label) if hub_label is not None else config.hub
    return dataclasses.replace(config, quota=quota, routing=routing, hub=hub)


def register(projects: Projects, name: str, **kwargs) -> Registration:
    """登记一个项目；没给的项用小值：前缀取名字、保底 1、上限 2（只推送的项目没有前缀）。"""
    if not kwargs.get("push_only"):
        kwargs.setdefault("prefix", name)
    kwargs.setdefault("guaranteed", 1)
    kwargs.setdefault("cap", 2)
    return projects.add(name, **kwargs)


def events(database) -> list[dict]:
    rows = database.conn().execute("SELECT * FROM events ORDER BY id").fetchall()
    return [dict(row) for row in rows]
