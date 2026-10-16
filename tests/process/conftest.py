"""进程与部署测试共用的夹具（任务 D6）。

`World` 把"准备数据"和"起服务"接在一起：先用 `tests/support/hub.py` 的 `Hub` 往数据目录里放东西
（凭证、项目、窗口……），再 `start()` 起一个完整的服务——真的 uvicorn（随机端口）、真的后台线程，
时间用 `sleep` 立刻返回的假时钟，腾讯用进程内的假上游，Bark 用不联网的替身。所有起过的服务在测试
结束时一定关掉（`close()`），免得线程、端口、数据目录的锁漏到下一个测试。

`Slow`：卡住的 Bark 替身（Bark 不通时服务照样监听）。
`call`：用项目的密钥访问服务。

`Lab` 和 `Daemon` 是进程级测试用的：真的服务进程（`python -c` 里调 `hubctl`，真时钟、真信号、
真的退出码），腾讯是本机回环上的假服务（`FakeIlink`），Bark 是 `FakeBark`，数据目录在临时目录里。
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import suppress
from functools import cached_property
from pathlib import Path
from typing import Any

import pytest
import requests
import yaml

from clawbot_hub.app import Service
from clawbot_hub.bark import ENV_KEY, BarkKey, BarkResult
from clawbot_hub.clock import RealClock
from clawbot_hub.config import Config
from clawbot_hub.db import Database, migrate
from clawbot_hub.paths import HubPaths, ensure_home
from tests.alerts.conftest import DEVICE, StubBark
from tests.conftest import PROXY_VARS
from tests.support.fake_bark import FakeBark
from tests.support.fake_clock import FakeClock
from tests.support.fake_ilink import FakeIlink
from tests.support.fake_upstream import FakeUpstream
from tests.support.hub import USER, Hub

__all__ = [
    "DEVICE",
    "REPO_ROOT",
    "Daemon",
    "Lab",
    "Slow",
    "World",
    "auth",
    "call",
    "free_port",
]


class Slow(StubBark):
    """`send` 卡在门口，直到测试放行：模拟 Bark 服务器连得上却迟迟不回话。"""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock)
        self.entered = threading.Event()
        self._open = threading.Event()

    def send(self, key: BarkKey, title: str, text: str) -> BarkResult:
        self.entered.set()
        self._open.wait(10)  # 真实时间：测试忘了放行也不会挂住
        return super().send(key, title, text)

    def release(self) -> None:
        self._open.set()


def call(service: Service, method: str, path: str, *, key: str | None = None, **kw: Any):
    """访问服务；`key` 是项目的密钥。"""
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return requests.request(method, f"{service.url}{path}", headers=headers, timeout=10, **kw)


class World:
    """`hub`（和它用的 `db`）要用到才建：只测"全新启动"的测试一次也不碰它们，数据目录里就一个
    文件都没有。"""

    def __init__(self, paths, clock: FakeClock, upstream, config: Config) -> None:
        self.paths = paths
        self.clock = clock
        self.upstream = upstream
        upstream.user_id = USER  # 假上游里的"使用者"就是凭证文件里的 user_id
        self.config = config
        self.bark = StubBark(clock)
        self.started: list[Service] = []

    @cached_property
    def db(self) -> Database:
        migrate(self.paths, clock=self.clock, tz=self.config.display.tzinfo)
        return Database(self.paths.db)

    @cached_property
    def hub(self) -> Hub:
        return Hub(self.paths, self.db, self.clock, self.upstream, self.config)

    def start(self, config: Config | None = None, **overrides: Any) -> Service:
        """起一个服务，用的是同一个数据目录、时钟和假上游；`overrides` 换掉别的参数
        （`upstream=`、`bark=`、`environ=`、`on_step=`……）。"""
        arguments: dict[str, Any] = {
            "paths": self.paths,
            "upstream": self.upstream,
            "clock": self.clock,
            "environ": {ENV_KEY: DEVICE},
            "bark": self.bark,
            "port": 0,
        }
        arguments.update(overrides)
        service = Service.start(config or self.config, **arguments)
        self.started.append(service)
        return service

    def key(self, name: str = "alpha") -> str:
        return self.hub.keys[name]

    def leave_sending(self, project: str = "alpha", *, parts: int = 2) -> str:
        """像是服务在发送途中被杀了：库里留下一条"发送中"的记录，额度已经预占。"""
        from clawbot_hub.quota import Request
        from tests.quota.conftest import insert_outbox

        outbox_id = "o-left-behind"
        with self.db.txn() as conn:
            insert_outbox(conn, outbox_id, project, parts=parts, now=self.clock.now_ms())
            self.hub.quota.reserve(conn, Request(project, n=parts), outbox_id=outbox_id)
        return outbox_id

    def rows(self, sql: str, *args: Any) -> list[dict]:
        return [dict(row) for row in self.db.conn().execute(sql, args)]

    def events(self, kind: str = "lifecycle") -> list[dict]:
        return self.rows("SELECT * FROM events WHERE kind = ? ORDER BY id", kind)

    def run_json(self, **fields: Any) -> None:
        """放一份上一次运行留下的 `run.json`（默认是被杀的：没有正常退出标记）。"""
        record = {
            "pid": 4242,
            "started_ms": self.clock.now_ms() - 600_000,
            "version": "0.0.1",
            "listen": "127.0.0.1:8790",
            "profile": "legacy",
            "confirm": "observe",
            "last_heartbeat_ms": self.clock.now_ms() - 120_000,
            "clean_exit": False,
        }
        record.update(fields)
        fd = os.open(self.paths.run, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def close(self) -> None:
        for service in reversed(self.started):
            with suppress(Exception):
                service.close()
        if "db" in self.__dict__:
            self.db.close()


@pytest.fixture
def clock() -> FakeClock:
    """服务的后台线程和请求线程共用它；`sleep` 立刻返回（重试间隔不真等），`wait` 等 `advance`。"""
    return FakeClock(auto_sleep=True)


@pytest.fixture
def make_world(paths, clock, fake_upstream):
    worlds: list[World] = []

    def build(config: Config | None = None) -> World:
        world = World(paths, clock, fake_upstream, config or Config())
        worlds.append(world)
        return world

    yield build
    for world in worlds:
        world.close()


@pytest.fixture
def world(make_world) -> World:
    return make_world()


# ---------------------------------------------------------------- 进程级：真的服务进程

REPO_ROOT = Path(__file__).resolve().parents[2]
# 真实时间里最多等多久：进程起来、导入、监听，慢机器上也够；正常情况下一两秒
READY_SECONDS = 30.0
EXIT_SECONDS = 30.0

# 子进程里跑的东西：和 `uv run hubctl …` 一样调 `clawbot_hub.cli:app`，外加两个测试用的开关——
# 先把 umask 改成别的值（验证服务自己会收紧），或装上联网守卫（验证全程没碰外网）
LAUNCHER = """\
import os
mask = os.environ.pop("HUB_TEST_UMASK", "")
if mask:
    os.umask(int(mask, 8))
if os.environ.pop("HUB_TEST_GUARD", ""):
    from tests.support import netguard
    netguard.block_network().__enter__()
from clawbot_hub.cli import app
app(prog_name="hubctl")
"""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def child_env(**extra: str) -> dict[str, str]:
    """子进程的环境变量：没有代理（回环请求不绕路）、没有测试进程的数据目录和设备码。"""
    env = {k: v for k, v in os.environ.items() if k not in PROXY_VARS}
    for name in ("CLAWBOT_HUB_HOME", ENV_KEY, "CLAWBOT_HUB_KEY"):
        env.pop(name, None)
    env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", NO_COLOR="1")
    env.update(extra)
    return env


class Daemon:
    """一个真的 `hubctl serve` 进程。输出（标准输出和标准错误）一行一行收进 `lines`。"""

    def __init__(self, args: list[str], *, port: int, env: dict[str, str]) -> None:
        self.port = port
        self.lines: list[str] = []
        self.proc = subprocess.Popen(
            [sys.executable, "-c", LAUNCHER, *args],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            start_new_session=True,  # 终端里的 Ctrl-C 只管 pytest，不顺带打到它
        )
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.append(line.rstrip("\n"))

    @property
    def pid(self) -> int:
        return self.proc.pid

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def output(self) -> str:
        return "\n".join(self.lines)

    @property
    def running(self) -> bool:
        return self.proc.poll() is None

    def wait_ready(self, timeout: float = READY_SECONDS) -> Daemon:
        """等服务完全起好：日志里有"已启动"那一行（它在写完 `run.json`、起完各线程以后才写），
        `/v1/health` 回 200。只看端口不够：端口先开，`run.json` 要稍后才有。进程先退出了就报错
        并带上它的输出。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.running:
                raise AssertionError(f"服务进程提前退出（{self.proc.returncode}）：\n{self.output}")
            if any("已启动" in line for line in list(self.lines)):
                with suppress(requests.RequestException):
                    if requests.get(f"{self.url}/v1/health", timeout=1).status_code == 200:
                        return self
            time.sleep(0.05)
        raise AssertionError(f"{timeout} 秒内没有起好：\n{self.output}")

    def send_signal(self, sig: int) -> None:
        self.proc.send_signal(sig)

    def wait(self, timeout: float = EXIT_SECONDS) -> int:
        """等进程退出，返回退出码（被信号杀死是负的信号编号）；输出收完才返回。"""
        try:
            code = self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            raise AssertionError(f"{timeout} 秒内没有退出：\n{self.output}") from None
        self._reader.join(5)
        return code

    def stop(self, sig: int = signal.SIGTERM, timeout: float = EXIT_SECONDS) -> int:
        self.send_signal(sig)
        return self.wait(timeout)

    def kill(self) -> int:
        self.proc.kill()
        return self.wait()


class Lab:
    """进程级测试的现场：数据目录、一份配置、假腾讯、假 Bark，能起真的服务进程。

    假腾讯和假 Bark 在测试进程里（回环上的真 HTTP 服务，真时钟）；服务进程通过配置里的
    `upstream.base_url` 和环境变量里的 Bark 地址找到它们。"""

    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "hub-home"
        self.paths = HubPaths(self.home)
        self.config_file = tmp_path / "hub.yaml"
        self.port = free_port()
        self.ilink = FakeIlink(longpoll_seconds=0.3)
        self.bark = FakeBark()
        self.ilink.start()
        self.bark.start()
        self.sections: dict[str, dict[str, Any]] = {}
        self.daemons: list[Daemon] = []
        self.configure()

    # ------------------------------------------------ 准备

    def configure(self, **sections: dict[str, Any]) -> None:
        """写配置文件：端口和假腾讯的地址是固定带上的，`sections` 在上面再加（按节合并）。"""
        base = {"server": {"port": self.port}, "upstream": {"base_url": self.ilink.base_url}}
        for name, values in sections.items():
            self.sections.setdefault(name, {}).update(values)
        merged = {name: dict(values) for name, values in base.items()}
        for name, values in self.sections.items():
            merged.setdefault(name, {}).update(values)
        self.config_file.write_text(yaml.safe_dump(merged, allow_unicode=True), encoding="utf-8")

    def seed(self, *names: str, speak: bool = True) -> dict[str, str]:
        """往数据目录放东西：建库、一份指向假腾讯的凭证、（使用者说过一句话）、登记项目。
        返回 `{项目名: 密钥}`。"""
        ensure_home(self.paths)
        clock = RealClock()
        config = Config()
        migrate(self.paths, clock=clock, tz=config.display.tzinfo)
        db = Database(self.paths.db)
        try:
            hub = Hub(self.paths, db, clock, FakeUpstream(clock=clock), config)
            hub.bind(base_url=self.ilink.base_url, user_id=self.ilink.user_id)
            if speak:
                hub.speak()
            for name in names:
                hub.add(name, guaranteed=6, cap=8)
            return dict(hub.keys)
        finally:
            db.close()

    # ------------------------------------------------ 起进程

    @property
    def bark_key(self) -> str:
        return f"{self.bark.base_url}/{DEVICE}"

    def env(self, **extra: str) -> dict[str, str]:
        return child_env(**{ENV_KEY: self.bark_key, **extra})

    def spawn(self, *, umask: int | None = None, guard: bool = False, **extra: str) -> Daemon:
        """起 `hubctl serve`，不等它开始监听。"""
        env = self.env(**extra)
        if umask is not None:
            env["HUB_TEST_UMASK"] = f"{umask:o}"
        if guard:
            env["HUB_TEST_GUARD"] = "1"
        args = ["serve", "--home", str(self.home), "--config", str(self.config_file)]
        daemon = Daemon(args, port=self.port, env=env)
        self.daemons.append(daemon)
        return daemon

    def start(self, **kwargs: Any) -> Daemon:
        """起 `hubctl serve` 并等它开始监听。"""
        return self.spawn(**kwargs).wait_ready()

    def background(self, *args: str, guard: bool = True) -> Daemon:
        """跑一条别的 `hubctl` 命令，不等它结束（`Daemon` 就是个把输出收下来的子进程）。"""
        env = self.env(**({"HUB_TEST_GUARD": "1"} if guard else {}))
        process = Daemon(list(args), port=self.port, env=env)
        self.daemons.append(process)
        return process

    def hubctl(
        self, *args: str, guard: bool = True, timeout: float = 60.0, **extra: str
    ) -> subprocess.CompletedProcess[str]:
        """跑一条别的 `hubctl` 命令（不带 `--home`、`--config` 的由调用者自己写），等它结束。"""
        env = self.env(**extra)
        if guard:
            env["HUB_TEST_GUARD"] = "1"
        return subprocess.run(
            [sys.executable, "-c", LAUNCHER, *args],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )

    # ------------------------------------------------ 看

    def rows(self, sql: str, *args: Any) -> list[dict]:
        """用只读连接查数据库（服务在写也不碍事：WAL）。"""
        conn = sqlite3.connect(f"file:{self.paths.db}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in conn.execute(sql, args)]
        finally:
            conn.close()

    def log_text(self) -> str:
        try:
            return self.paths.log_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def close(self) -> None:
        for daemon in self.daemons:
            if daemon.running:
                daemon.proc.kill()
            with suppress(Exception):
                daemon.proc.wait(10)
        self.ilink.stop()
        self.bark.stop()


@pytest.fixture
def lab(tmp_path: Path):
    laboratory = Lab(tmp_path)
    yield laboratory
    laboratory.close()
