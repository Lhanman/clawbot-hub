"""服务里各个部件的集合，接口层（`api.py`）从这里拿东西。

`Services` 只是把已经造好的部件放在一起；`assemble` 是正式的装法：登录监视器、项目、额度、发送
接上同一个数据库、同一个时钟、同一个上游。测试和 `hubctl serve` 用同一个装法（`app.create_app`），
测试想换零件就自己造一个 `Services`。

`Hooks` 是给告警留的口子（阶段 D 的告警把它们接上）：登录失效 / 恢复、腾讯拒绝后的暂停、每个
`sendmessage` 请求有没有确认送达。回调出错只记日志，不影响收发。

`inbox` 是收话（poller）和取话 / 确认 / 回复（接口）共用的那一个待取队列：挂着的取话请求要靠它被
新入队的话叫醒，所以两边必须是同一个对象。

关闭：`begin_shutdown()` 以后新请求一律 503（`api.py` 的守门人看 `shutting_down`），正在等发送锁的
发送马上以 `failed` / `busy` 结束，正在发的那一条发完，挂着的取话请求马上返回空列表
（`hub-deploy`「正常关闭」）。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from clawbot_hub.account import AccountStore
from clawbot_hub.clock import Clock
from clawbot_hub.config import Config
from clawbot_hub.db import Database
from clawbot_hub.inbox import Inbox
from clawbot_hub.limits import Limits
from clawbot_hub.loginstate import LoginMonitor
from clawbot_hub.paths import HubPaths
from clawbot_hub.projects import Projects
from clawbot_hub.quota import Hold, Quota
from clawbot_hub.sender import Sender
from clawbot_hub.upstream.base import Upstream


@dataclass(frozen=True)
class Hooks:
    on_expired: Callable[[int], None] | None = None
    on_restored: Callable[[int], None] | None = None
    on_hold: Callable[[Hold], None] | None = None
    on_request: Callable[[str], None] | None = None


@dataclass
class Services:
    config: Config
    clock: Clock
    paths: HubPaths
    db: Database
    store: AccountStore
    monitor: LoginMonitor
    projects: Projects
    quota: Quota
    sender: Sender
    limits: Limits | None = None
    inbox: Inbox | None = None
    shutting_down: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        if self.limits is None:
            self.limits = Limits(self.config, self.clock)
        if self.inbox is None:
            self.inbox = Inbox(self.db, self.config, self.clock, self.projects)

    def begin_shutdown(self) -> None:
        """开始关闭：新请求 503；等发送锁的发送马上结束，正在发的那一条不打断；挂着的取话请求
        马上返回空列表。"""
        self.shutting_down.set()
        self.sender.shutdown()
        self.inbox.shutdown()


def assemble(
    config: Config,
    upstream: Upstream,
    clock: Clock,
    *,
    paths: HubPaths,
    db: Database,
    hooks: Hooks | None = None,
) -> Services:
    hooks = hooks or Hooks()
    store = AccountStore(paths)
    monitor = LoginMonitor(
        paths, db, store, clock, on_expired=hooks.on_expired, on_restored=hooks.on_restored
    )
    quota = Quota(config, clock)
    sender = Sender(
        db,
        config,
        clock,
        upstream,
        monitor,
        quota=quota,
        on_hold=hooks.on_hold,
        on_request=hooks.on_request,
    )
    projects = Projects(db, config, clock)
    return Services(
        config=config,
        clock=clock,
        paths=paths,
        db=db,
        store=store,
        monitor=monitor,
        projects=projects,
        quota=quota,
        sender=sender,
    )
