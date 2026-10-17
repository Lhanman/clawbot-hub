"""装配和生命周期（任务 A7、D6，规格 `hub-deploy`「启动自检和顺序」「正常关闭」「谁能连」）。

`create_app` / `server_config`：把部件接成能收请求的应用、给出配套的 uvicorn 设置（组件测试
直接用）。

`Service` 是完整的服务，`hubctl serve` 和进程级测试用它。**启动**按这个顺序，任何一步失败就停下、
把已经拿到的东西还回去、抛 `StartupError`（带退出码）：

1. 读配置并校验（退出码 1）——在命令行层做，`Service.start` 拿到的已经是合格的配置；
2. `home`：建数据目录、装日志、把放宽了的权限改回（每处一条警告；改不回去就启动失败）；
3. `lock`：拿 `hub.lock`（拿不到：退出码 2）；
4. `database`：打开并按需升级数据库（版本比程序新或文件坏了：退出码 3），然后查配置警告、保底合计、
   前缀（只写事件，不拒绝启动）；
5. `account`：比对 `account.json` 和库里的凭证摘要（文件没有、读不出来照常启动）；
6. `restart`：看上一次的 `run.json`，把仍标着"发送中"的记录标成"中断"，上次没正常退出或有中断就
   登记 `service_restarted` 告警（只登记，由告警线程发，Bark 不通也不拖慢监听）；
7. `listen`：绑定端口、开始监听（失败：退出码 1）；
8. `threads`：写新的 `run.json`，`official` 档调 `notifystart`，起收消息、凭证监视、定时任务、
   服务回复的发送、告警五个线程，日志写"已启动"。

`on_step(名字)` 每一步**开始时**调一次（失败的那一步也算），测试和 `hubctl serve` 用它看顺序。

**关闭**（`stop()`，返回退出码）：新请求 503；等发送锁的发送马上 `failed` / `busy`；正在向腾讯
发的那一条、正在处理的请求和服务自己的回复发完，最多等 `server.shutdown_seconds`；挂着的取话
请求马上返回空；然后停 HTTP、停各线程，`official` 档调 `notifystop`，写"正常退出"标记，放锁，
退出码 0。到点还没发完就**强行退出**：退出码 1，不写"正常退出"、不调 `notifystop`、不收拾
（调用者马上 `os._exit`），那条记录下次启动时标成"中断"。`close()` 不管处在什么状态都把占着的
东西还掉（测试结束、强退之后在同一个进程里接着用时调），重复调没关系。

规格没写、这里定下的小约定（任务单 D6 的实现说明里也有）：

- 收消息线程可能正挂在一次长轮询里（最长三四十秒），退出不等它：`stop` 最多等
  `POLLER_GRACE_SECONDS`，等不到就放着不管（它是后台线程，进程退出时一并结束；它回来的那一批
  不处理）。这段时间不算在 `server.shutdown_seconds` 里；
- 权限放宽了又改不回去（不是自己的文件）：启动失败。凭证放在别人能读的地方，不如不起；
- 启动失败时不往 `hub.log` 里另写一行：原因已经打印在标准错误（launchd 收进 `launchd.log`）。
"""

from __future__ import annotations

import errno
import logging
import socket
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import datetime, tzinfo
from pathlib import Path
from typing import IO

import uvicorn
from fastapi import FastAPI

import clawbot_hub
from clawbot_hub.alerts import Alerts
from clawbot_hub.api import build_app
from clawbot_hub.bark import BarkSender
from clawbot_hub.clock import Clock
from clawbot_hub.config import Config
from clawbot_hub.db import Database, DatabaseProblem, is_busy, migrate
from clawbot_hub.events import add_event
from clawbot_hub.instance import (
    InstanceBusy,
    InstanceLock,
    RunInfo,
    RunRecorder,
    acquire_instance_lock,
    read_run_info,
)
from clawbot_hub.logging_setup import LogSetup, setup_logging
from clawbot_hub.paths import HubPaths, ensure_home, fix_permissions
from clawbot_hub.poller import Poller
from clawbot_hub.projects import Projects, guarantee_total
from clawbot_hub.replier import Replier
from clawbot_hub.scheduler import Scheduler
from clawbot_hub.services import Hooks, Services, assemble
from clawbot_hub.upstream.base import Upstream
from clawbot_hub.watcher import CredentialWatcher

logger = logging.getLogger(__name__)

# `hubctl serve` 的退出码（规格 `hub-deploy`「启动自检和顺序」「正常关闭」）
EXIT_OK = 0
EXIT_FAILED = 1  # 配置错、端口被占、关闭时间用完被强退
EXIT_BUSY = 2  # 已经有一个在运行
EXIT_DATABASE = 3  # 数据库比程序新、文件坏了、升级失败

# 关闭时最多等收消息线程多久（真实秒），见模块说明
POLLER_GRACE_SECONDS = 2.0
# 停服务自己的发送线程、定时任务等后台线程时最多各等多久（真实秒）；它们都在等事件，醒得很快
WORKER_JOIN_SECONDS = 10.0
# HTTP 服务起来、正常停下各最多等多久（真实秒）
SERVER_START_SECONDS = 15.0
SERVER_STOP_SECONDS = 10.0
# 不是正常关闭时（强退之后、测试结束），停各线程各最多等多久（真实秒）：卡住的不再等
CLOSE_JOIN_SECONDS = 1.0


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


# ---------------------------------------------------------------- 启动


class StartupError(Exception):
    """启动失败：一句话原因（给人看，不含凭证和正文）和退出码。"""

    def __init__(self, message: str, exit_code: int = EXIT_FAILED) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _bind(host: str, port: int) -> socket.socket:
    """绑定并监听。`SO_REUSEADDR` 让服务刚停下立刻再起不被残留的连接挡住；别的程序正监听着
    同一个端口时照样绑不上。"""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(2048)
    except OSError as exc:
        sock.close()
        raise StartupError(_bind_problem(host, port, exc)) from None
    return sock


def _bind_problem(host: str, port: int, exc: OSError) -> str:
    if exc.errno == errno.EADDRINUSE:
        return (
            f"端口 {port} 已被占用（{host}）：是不是已经有一个服务在运行，或者别的程序占着它？"
            "换一个 server.port，或者停掉占着它的程序"
        )
    if exc.errno == errno.EACCES:
        return f"没有权限监听 {host}:{port}（1024 以下的端口要管理员权限）：换一个 server.port"
    if exc.errno == errno.EADDRNOTAVAIL:
        return f"这台机器上没有地址 {host}，没法监听：检查 server.host"
    return f"监听 {host}:{port} 失败：{exc.strerror or type(exc).__name__}"


class _Server(uvicorn.Server):
    """起好以后置 `ready`，启动线程据此知道可以连了（`started` 是个普通属性，没法等）。"""

    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.ready = threading.Event()

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        if self.started:
            self.ready.set()


def _clock_text(ms: int, tz: tzinfo) -> str:
    return datetime.fromtimestamp(ms // 1000, tz).strftime("%H:%M")


class Service:
    """一个跑着的服务。用 `Service.start(...)` 起；`stop()` 正常关闭并返回退出码；`close()` 把占着
    的东西全还掉。线程、端口、锁、数据库连接、日志处理器都归它管。

    起好以后可以看：`url`（本机访问用的地址）、`listening`（监听的主机和端口）、`services`（各部件）、
    `db`、`previous`（启动前读到的 `run.json`，第一次启动是 `None`）、`interrupted`（启动时标成
    "中断"的发送记录数）、`forced`（关闭时间用完被强退）。"""

    def __init__(self, config: Config, paths: HubPaths, clock: Clock, upstream: Upstream) -> None:
        self.config = config
        self.paths = paths
        self.clock = clock
        self.upstream = upstream
        self.listening: tuple[str, int] = (config.server.bind_host, 0)
        self.previous: RunInfo | None = None
        self.interrupted = 0
        self.forced = False
        self.db: Database = None  # type: ignore[assignment]  # 第 4 步以后有
        self.services: Services = None  # type: ignore[assignment]
        self.alerts: Alerts | None = None
        self.recorder: RunRecorder | None = None
        self.scheduler: Scheduler | None = None
        self.watcher: CredentialWatcher | None = None
        self.replier: Replier | None = None
        self.poller: Poller | None = None
        self._log: LogSetup | None = None
        self._lock: InstanceLock | None = None
        self._sock: socket.socket | None = None
        self._server: _Server | None = None
        self._server_thread: threading.Thread | None = None
        self._stop_lock = threading.Lock()
        self._code: int | None = None
        self._close_lock = threading.Lock()
        self._closed = False
        self._workers_stopped = False

    # ---------------------------------------------------------------- 启动

    @classmethod
    def start(
        cls,
        config: Config,
        *,
        paths: HubPaths,
        upstream: Upstream,
        clock: Clock,
        environ: Mapping[str, str] | None = None,
        bark: BarkSender | None = None,
        port: int | None = None,
        on_step: Callable[[str], None] | None = None,
        stream: IO[str] | None = None,
    ) -> Service:
        """起服务，起好了才返回。`environ` 是读 Bark 设备码用的环境变量（不给就是进程的）；`bark`
        换掉真的 Bark 客户端（测试）；`port` 不给用配置里的，给 0 让系统挑；`stream` 是日志同时写到
        的流（默认标准错误）。失败抛 `StartupError`，前面拿到的东西都已还回去。"""
        service = cls(config, paths, clock, upstream)
        try:
            service._boot(
                environ=environ, bark=bark, port=port, on_step=on_step or _no_step, stream=stream
            )
        except BaseException:
            service.close()
            raise
        return service

    @property
    def url(self) -> str:
        """本机访问用的地址（监听所有网卡时也是 `127.0.0.1`）。"""
        host = self.listening[0]
        host = "127.0.0.1" if host == "0.0.0.0" else host
        host = f"[{host}]" if ":" in host else host
        return f"http://{host}:{self.listening[1]}"

    def _boot(
        self,
        *,
        environ: Mapping[str, str] | None,
        bark: BarkSender | None,
        port: int | None,
        on_step: Callable[[str], None],
        stream: IO[str] | None,
    ) -> None:
        config, paths, clock = self.config, self.paths, self.clock
        tz = config.display.tzinfo

        on_step("home")
        try:
            ensure_home(paths)
        except NotADirectoryError as exc:
            raise StartupError(str(exc)) from None
        except OSError as exc:
            raise StartupError(f"数据目录建不出来：{paths.home}（{exc.strerror}）") from None
        self._log = setup_logging(paths, config.log, tz=tz, environ=environ, stream=stream)
        self._repair_permissions()

        on_step("lock")
        try:
            self._lock = acquire_instance_lock(paths)
        except InstanceBusy as exc:
            raise StartupError(str(exc), EXIT_BUSY) from None

        on_step("database")
        self._open_database()
        alerts = self.alerts = Alerts(self.db, config, clock, paths, client=bark, env=environ)
        app = create_app(
            config, self.upstream, clock, paths=paths, db=self.db, hooks=alerts.hooks()
        )
        self.services = services = app.state.services
        self._warn_about_settings()

        on_step("account")
        services.monitor.sync()

        on_step("restart")
        self._deal_with_the_previous_run()

        on_step("listen")
        self._listen(app, config.server.port if port is None else port)

        on_step("threads")
        self._start_threads()

    def _repair_permissions(self) -> None:
        try:
            fixed = fix_permissions(self.paths)
        except OSError as exc:
            name = getattr(exc, "filename", None) or self.paths.home
            raise StartupError(
                f"{name} 的权限比规定的宽，又改不回去（{exc.strerror}）：凭证和数据不该让别人读到，"
                "请用它的主人身份运行，或者自己 chmod 700 / 600"
            ) from None
        for path in fixed:
            logger.warning(
                "%s 的权限比规定的宽，已改回（目录 0700、文件 0600）", _relative(path, self.paths)
            )

    def _open_database(self) -> None:
        busy_ms = self.config.db.busy_timeout_ms
        try:
            migrate(
                self.paths, clock=self.clock, tz=self.config.display.tzinfo, busy_timeout_ms=busy_ms
            )
        except DatabaseProblem as exc:
            raise StartupError(str(exc), exc.exit_code) from None
        except sqlite3.OperationalError as exc:
            if is_busy(exc):
                raise StartupError("数据库正忙：别的程序占着它，稍后再试") from None
            raise
        self.db = Database(self.paths.db, busy_timeout_ms=busy_ms)

    def _warn_about_settings(self) -> None:
        """配置警告、保底合计超了、前缀和指令词撞了：只写事件和日志，不拒绝启动。"""
        config = self.config
        found = list(config.warnings())
        projects = Projects(self.db, config, self.clock)
        total = guarantee_total(projects.list(), config)
        if total.excess:
            found.append(
                f"保底合计 {total.total} 加上回复保留 {total.reserve} 超过窗口总条数 {total.limit}"
                f"（{total.listing}）：服务照常运行，只是登记和启用项目会被拒绝"
            )
        for item in projects.prefix_conflicts():
            found.append(
                f"项目 {item.project} 的前缀「{item.prefix}」和 {item.setting} 里的"
                f"「{item.word}」相同"
            )
        if not found:
            return
        with self.db.txn() as conn:
            for text in found:
                add_event(conn, self.clock.now_ms(), "lifecycle", text, level="WARNING")
        for text in found:
            logger.warning("%s", text)

    def _deal_with_the_previous_run(self) -> None:
        self.previous = previous = read_run_info(self.paths)
        with self.db.txn() as conn:
            interrupted = self.services.quota.interrupt_unfinished(conn)
            if previous is not None and not previous.clean_exit:
                beat = _clock_text(previous.last_heartbeat_ms, self.config.display.tzinfo)
                detail = f"上一次运行没有正常退出（最后心跳 {beat}）"
                if interrupted:
                    detail += f"，{interrupted} 条仍在发送的记录已标成中断"
                add_event(conn, self.clock.now_ms(), "lifecycle", detail, level="WARNING")
            elif interrupted:
                detail = f"上一次运行留下 {interrupted} 条仍在发送的记录，已标成中断"
                add_event(conn, self.clock.now_ms(), "lifecycle", detail, level="WARNING")
        self.interrupted = interrupted
        assert self.alerts is not None
        self.alerts.service_restarted(previous, interrupted)

    def _listen(self, app: FastAPI, port: int) -> None:
        host = self.config.server.bind_host
        self._sock = sock = _bind(host, port)
        self.listening = (host, sock.getsockname()[1])
        server = self._server = _Server(server_config(app, self.config, port=self.listening[1]))
        thread = self._server_thread = threading.Thread(
            target=server.run, kwargs={"sockets": [sock]}, name="hub-http", daemon=True
        )
        thread.start()
        started = time.monotonic()  # 起一个线程和事件循环用的是真实时间，和注入的时钟无关
        while not server.ready.wait(0.05):
            if not thread.is_alive():
                raise StartupError("HTTP 服务没有起来")
            if time.monotonic() - started > SERVER_START_SECONDS:
                raise StartupError(f"HTTP 服务 {SERVER_START_SECONDS:.0f} 秒内没有起来")

    def _start_threads(self) -> None:
        config, services, clock = self.config, self.services, self.clock
        assert self.alerts is not None
        host, port = self.listening
        shown = f"[{host}]" if ":" in host else host
        listen = f"{shown}:{port}"
        self.recorder = recorder = RunRecorder(
            self.paths,
            clock,
            version=clawbot_hub.__version__,
            listen=listen,
            profile=config.upstream.profile,
            confirm=config.upstream.confirm,
        )
        recorder.start()
        self._notify("start")
        self.replier = Replier(services.sender, config.hub.label, in_flight=services.in_flight)
        self.poller = Poller(
            config,
            clock,
            self.db,
            self.upstream,
            services.store,
            services.monitor,
            services.projects,
            services.quota,
            services.inbox,
            self.replier,
        )
        self.watcher = CredentialWatcher(services.monitor, clock)
        self.scheduler = Scheduler(
            self.db, config, clock, self.paths, self.alerts, services.inbox, recorder
        )
        self.alerts.start()
        self.replier.start()
        self.poller.start()
        self.watcher.start()
        self.scheduler.start()
        count = len(services.projects.list())
        logger.info(
            "已启动：版本 %s，监听 %s，上游档位 %s，项目 %d 个",
            clawbot_hub.__version__,
            listen,
            config.upstream.profile,
            count,
        )
        with self.db.txn() as conn:
            detail = (
                f"服务启动：版本 {clawbot_hub.__version__}，监听 {listen}，"
                f"上游档位 {config.upstream.profile}，送达确认 {config.upstream.confirm}"
            )
            add_event(conn, clock.now_ms(), "lifecycle", detail)

    def _notify(self, which: str) -> None:
        """`official` 档在启动、退出时告诉腾讯一声（`notifystart` / `notifystop`）。没有凭证就不调；
        出错只记一条警告，不影响启动和退出。"""
        if self.config.upstream.profile != "official":
            return
        account = self.services.monitor.account()
        if account is None:
            return
        call = self.upstream.notify_start if which == "start" else self.upstream.notify_stop
        try:
            call(account.to_account())
        except Exception as exc:  # noqa: BLE001 - 通知不成不能连累启动和退出
            logger.warning("notify%s 没有成功（%s），照常继续", which, type(exc).__name__)

    # ---------------------------------------------------------------- 关闭

    def stop(self) -> int:
        """正常关闭，返回退出码：0 是正常退出，1 是关闭时间用完被强退（这时什么都没收拾，调用者要
        马上结束进程；同一个进程里接着用就再调 `close()`）。重复调返回第一次的结果。"""
        with self._stop_lock:
            if self._code is None:
                self._code = self._shutdown()
            return self._code

    def _shutdown(self) -> int:
        config, clock, services = self.config, self.clock, self.services
        logger.info("收到停止请求，开始关闭")
        services.begin_shutdown()
        deadline = clock.now_ms() + config.server.shutdown_seconds * 1000
        if not services.in_flight.wait_idle(clock, deadline):
            self.forced = True
            logger.error(
                "关闭时间用完（%d 秒），还有 %d 件事没做完：强行退出，没发完的发送下次启动时"
                "标成中断",
                config.server.shutdown_seconds,
                services.in_flight.count,
            )
            return EXIT_FAILED
        self._stop_workers(graceful=True)
        self._notify("stop")
        assert self.recorder is not None
        self.recorder.finish()
        with self.db.txn() as conn:
            add_event(conn, clock.now_ms(), "lifecycle", "服务正常退出")
        logger.info("正常退出")
        self.close()
        return EXIT_OK

    def _stop_workers(self, *, graceful: bool) -> None:
        """停 HTTP 和各线程（只做一次）。收消息线程只等一小会儿，见模块说明。"""
        with self._close_lock:
            if self._workers_stopped:
                return
            self._workers_stopped = True
        self._stop_server(graceful=graceful)
        if self.poller is not None:
            self.poller.stop(POLLER_GRACE_SECONDS)
        if self.replier is not None:
            self.replier.stop(WORKER_JOIN_SECONDS if graceful else CLOSE_JOIN_SECONDS)
        for worker in (self.watcher, self.scheduler, self.alerts):
            if worker is not None:
                worker.stop(WORKER_JOIN_SECONDS if graceful else CLOSE_JOIN_SECONDS)

    def _stop_server(self, *, graceful: bool) -> None:
        """正常关闭时请求早已处理完，让 uvicorn 自己把连接收尾；不是正常关闭（强退之后、测试结束）
        可能还有卡住的请求，不等连接收尾，也不等卡住的请求（线程要等它们回来才结束，放着不管）。"""
        server, thread = self._server, self._server_thread
        if server is None or thread is None:
            return
        server.should_exit = True
        if not graceful:
            server.force_exit = True
        thread.join(SERVER_STOP_SECONDS if graceful else CLOSE_JOIN_SECONDS)
        if graceful and thread.is_alive():
            server.force_exit = True
            thread.join(CLOSE_JOIN_SECONDS)

    def close(self) -> None:
        """不管处在什么状态都把占着的东西还掉：停线程和 HTTP、放端口和锁、关数据库和日志。
        不写"正常退出"标记。重复调没关系。"""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        if self.services is not None:
            self.services.begin_shutdown()
        self._stop_workers(graceful=False)
        if self._sock is not None:
            with suppress(OSError):
                self._sock.close()
        if self.db is not None:
            self.db.close()
        if self._lock is not None:
            self._lock.release()
        if self._log is not None:
            self._log.close()


def _no_step(name: str) -> None:
    return None


def _relative(path: Path, paths: HubPaths) -> str:
    try:
        return str(path.relative_to(paths.home))
    except ValueError:
        return str(path)
