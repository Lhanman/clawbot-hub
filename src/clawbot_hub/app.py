"""最小装配：`create_app(config, upstream, clock, …)` 造出能收请求的应用，`server_config` 给出
和它配套的 uvicorn 设置。测试和 `hubctl serve` 用同一个装法。

生命周期（启动自检的 8 步、后台线程、信号、`hubctl serve`）是阶段 D 的事，这里只管把部件接起来。
"""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI

from clawbot_hub.api import build_app
from clawbot_hub.clock import Clock
from clawbot_hub.config import Config
from clawbot_hub.db import Database
from clawbot_hub.paths import HubPaths
from clawbot_hub.services import Hooks, assemble
from clawbot_hub.upstream.base import Upstream


def create_app(
    config: Config,
    upstream: Upstream,
    clock: Clock,
    *,
    paths: HubPaths,
    db: Database,
    hooks: Hooks | None = None,
) -> FastAPI:
    """把各部件接上同一个数据库、时钟和上游，再装成应用。数据库要已经建好、升到最新（启动的第 4
    步）；登录状态要不要先比对一次（第 5 步）由调用者做。应用的部件在 `app.state.services`。"""
    return build_app(assemble(config, upstream, clock, paths=paths, db=db, hooks=hooks))


def server_config(app: FastAPI, config: Config, *, port: int | None = None) -> uvicorn.Config:
    """uvicorn 的设置：监听 `server.bind_host`，只认连接的对端地址（`proxy_headers` 关掉，
    不信 `X-Forwarded-For` 之类的头），不自己配日志、不开生命周期、不在响应里报服务器名。
    `port` 给 0 就让系统挑一个空闲端口（测试用）。"""
    return uvicorn.Config(
        app,
        host=config.server.bind_host,
        port=config.server.port if port is None else port,
        proxy_headers=False,
        server_header=False,
        access_log=False,
        log_config=None,
        lifespan="off",
    )
