"""命令行测试共用的夹具：`CliRunner` 调 `hubctl`，时钟、腾讯和"是不是终端"都换成假的。"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from clawbot_hub.bark import ENV_KEY
from clawbot_hub.cli import Env, app
from clawbot_hub.config import Config
from clawbot_hub.db import Database
from clawbot_hub.paths import HubPaths
from tests.support.fake_clock import FakeClock
from tests.support.fake_upstream import FakeUpstream

KEY = re.compile(r"chub_[A-Za-z0-9_-]{43}")
MINUTE = 60_000
HOUR = 60 * MINUTE


@pytest.fixture(autouse=True)
def _bark_key_is_configured(monkeypatch):
    """命令行测试默认已经配了告警设备码（`status` 在没配时多一条提示）；要测"没配"的测试
    自己 `monkeypatch.delenv(ENV_KEY)`。"""
    monkeypatch.setenv(ENV_KEY, "A-TEST-DEVICE-KEY")


@pytest.fixture
def clock() -> FakeClock:
    """会自己往前走的假时钟：扫码时 `sleep(1)` 立刻返回，同时假时间走一秒。"""
    return FakeClock(auto_sleep=True)


@pytest.fixture
def upstream(clock) -> FakeUpstream:
    return FakeUpstream(clock=clock)


@pytest.fixture
def terminal() -> dict[str, bool]:
    """假的"标准输入是不是终端"；默认不是（脚本里的样子），测试改 `terminal["on"]`。"""
    return {"on": False}


@pytest.fixture
def env(clock, upstream, terminal) -> Env:
    return Env(
        clock=clock,
        upstream_factory=lambda _config: upstream,
        is_terminal=lambda: terminal["on"],
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """还不存在的数据目录：由命令自己建。"""
    return tmp_path / "hub-home"


@pytest.fixture
def hub_paths(home: Path) -> HubPaths:
    return HubPaths(home)


Invoke = Callable[..., Result]


@pytest.fixture
def run(env, home) -> Invoke:
    """`run("project", "list", "--json")`：把 `--home` 补在最后，拿到 `CliRunner` 的结果。"""

    def invoke(*args: str, input: str | None = None, home_option: bool = True) -> Result:
        argv = [*args, "--home", str(home)] if home_option else list(args)
        return CliRunner().invoke(app, argv, obj=env, input=input)

    return invoke


@pytest.fixture
def database(hub_paths, run):
    """命令建好的数据库（用另一个连接去读，看命令到底改了什么）。先登记一个项目让库建出来。"""
    result = run(
        "project", "add", "seed", "--prefix", "种子", "--guaranteed", "1", "--cap", "2", "--yes"
    )
    assert result.exit_code == 0, result.output
    db = Database(hub_paths.db)
    yield db
    db.close()


def default_config() -> Config:
    return Config()


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def add(run, name: str, *extra: str, prefix: str | None = "量化", guaranteed=2, cap=4, input=None):
    """登记一个项目的命令行（没给前缀的项目要自己带 `--push-only`）。"""
    argv = ["project", "add", name]
    if prefix is not None:
        argv += ["--prefix", prefix]
    argv += ["--guaranteed", str(guaranteed), "--cap", str(cap), *extra]
    return run(*argv, input=input)


def db_text(hub_paths) -> bytes:
    """数据库文件和它的日志文件里的全部字节（找密钥用）。"""
    blobs = []
    for suffix in ("", "-wal", "-shm"):
        path = hub_paths.db.with_name(hub_paths.db.name + suffix)
        if path.exists():
            blobs.append(path.read_bytes())
    return b"\n".join(blobs)


def json_of(result: Result):
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def write_run_json(hub_paths, clock, **overrides) -> None:
    """一份"服务正在运行"的 `run.json`：进程号是测试进程自己，20 秒前刚有过心跳，一小时前启动。"""
    info = {
        "pid": os.getpid(),
        "last_heartbeat_ms": clock.now_ms() - 20_000,
        "started_ms": clock.now_ms() - HOUR - 5 * MINUTE,
        "version": "0.1.0",
        "listen": "127.0.0.1:8790",
        "profile": "legacy",
        "confirm": "observe",
    }
    info.update(overrides)
    hub_paths.home.mkdir(mode=0o700, exist_ok=True)
    hub_paths.run.write_text(json.dumps(info), encoding="utf-8")
    hub_paths.run.chmod(0o600)  # 真的 `run.json` 是 0600（`hubctl doctor` 会查）
