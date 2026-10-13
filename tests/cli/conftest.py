"""命令行测试共用的夹具：`CliRunner` 调 `hubctl`，时钟、腾讯和"是不是终端"都换成假的。"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from clawbot_hub.cli import Env, app
from clawbot_hub.config import Config
from clawbot_hub.db import Database
from clawbot_hub.paths import HubPaths
from tests.support.fake_clock import FakeClock
from tests.support.fake_upstream import FakeUpstream


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


def json_of(result: Result):
    assert result.exit_code == 0, result.output
    return json.loads(result.output)
