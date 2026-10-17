"""`hubctl serve`（任务 D6，规格 `hub-cli`「命令一览」、`hub-deploy`「启动自检和顺序」）。

命令行这一层只管：读配置（错误一次列全，什么都不碰）、设 umask、起服务、等停止请求、停服务、
把失败变成退出码（1 / 2 / 3）和强退。服务本身的启动、关闭在 `tests/process/`；真的信号和
`kill -9` 在 `tests/process/test_lifecycle.py`。

这里用 `CliRunner` 在测试进程里跑：停止请求由测试给的替身发出（`Env.stop_requests`），强退由
`Env.hard_exit` 记下来，umask 由 `Env.umask` 记下来，免得改掉测试进程自己的。
"""

from __future__ import annotations

import dataclasses
import os
import signal
import socket
import threading
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import requests
from typer.testing import CliRunner

from clawbot_hub.app import Service
from clawbot_hub.cli import StopRequests, app
from clawbot_hub.db import Database, migrate
from clawbot_hub.instance import acquire_instance_lock, read_run_info
from clawbot_hub.paths import HubPaths

STEPS = ["config", "home", "lock", "database", "account", "restart", "listen", "threads"]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Stopper:
    """`StopRequests` 的替身：`wait()` 里做一件事（服务这时正在运行），然后就算收到了停止请求。"""

    def __init__(self, during=None) -> None:
        self.during = during
        self.installed = False
        self.restored = False

    def install(self) -> None:
        self.installed = True

    def wait(self) -> None:
        if self.during is not None:
            self.during()

    def restore(self) -> None:
        self.restored = True


class Exited(Exception):
    """替身 `hard_exit` 抛的：带着退出码。"""

    def __init__(self, code: int) -> None:
        self.code = code


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "serve.yaml"
    path.write_text(f"server:\n  port: {free_port()}\n", encoding="utf-8")
    return path


@pytest.fixture
def serve(env, home, config_file):
    """`serve(stopper, **env_changes)`：带着替身跑一次 `hubctl serve`，返回 `CliRunner` 的结果。"""
    calls: dict = {"steps": [], "umask": [], "exits": []}

    def hard_exit(code: int):
        calls["exits"].append(code)
        raise Exited(code)

    def run(stopper=None, *args: str, **changes):
        changed = dataclasses.replace(
            env,
            stop_requests=lambda: stopper or Stopper(),
            hard_exit=hard_exit,
            umask=calls["umask"].append,
            on_startup_step=calls["steps"].append,
            **changes,
        )
        argv = ["serve", "--home", str(home), "--config", str(config_file), *args]
        return CliRunner().invoke(app, argv, obj=changed)

    run.calls = calls
    return run


def health(home: Path) -> int:
    listen = read_run_info(HubPaths(home)).listen
    return requests.get(f"http://{listen}/v1/health", timeout=10).status_code


# ---------------------------------------------------------------- 正常的一次


def test_serve_runs_until_it_is_asked_to_stop_and_then_exits_0(serve, home):
    seen: dict = {}
    stopper = Stopper(during=lambda: seen.update(status=health(home)))

    result = serve(stopper)

    assert result.exit_code == 0, result.output
    assert seen["status"] == 200  # 等停止请求的时候，服务在运行
    assert (stopper.installed, stopper.restored) == (True, True)
    run = read_run_info(HubPaths(home))
    assert run is not None and run.clean_exit is True
    assert serve.calls["exits"] == []  # 平常的关闭不走强退


def test_the_steps_start_with_reading_the_config_and_follow_the_spec(serve):
    result = serve()

    assert result.exit_code == 0, result.output
    assert serve.calls["steps"] == STEPS


def test_the_umask_is_restricted_to_077(serve):
    result = serve()

    assert result.exit_code == 0, result.output
    assert serve.calls["umask"] == [0o077]


def test_the_umask_is_set_before_the_first_step_that_makes_files(env, home, config_file):
    order: list[str] = []
    changed = dataclasses.replace(
        env,
        stop_requests=lambda: Stopper(),
        hard_exit=lambda code: None,
        umask=lambda mask: order.append(f"umask {mask:o}"),
        on_startup_step=order.append,
    )

    CliRunner().invoke(
        app, ["serve", "--home", str(home), "--config", str(config_file)], obj=changed
    )

    assert order[:3] == ["config", "umask 77", "home"]


def test_serve_prints_the_started_line_on_the_error_stream(serve):
    result = serve()

    assert "已启动" in result.output


# ---------------------------------------------------------------- 失败和退出码


def test_config_errors_are_listed_at_once_and_nothing_is_touched(serve, home, config_file):
    config_file.write_text(
        "quota: {window_limit: -1}\nserver: {port: 70000}\nupstream: {profile: x}\n",
        encoding="utf-8",
    )
    made: list[object] = []

    result = serve(None, upstream_factory=lambda config: made.append(config))

    assert result.exit_code == 1
    assert "配置有 3 处错误" in result.output
    for key in ("quota.window_limit", "server.port", "upstream.profile"):
        assert key in result.output
    assert not home.exists()  # 数据目录都没建，更没有 hub.db 和日志
    assert made == []  # 连腾讯的连接都没造


SPEC_CONFIG_CASES = [
    # 数值越界：各写明允许的范围
    (
        "routing: {confirm_seconds: 3600}\n"
        "send: {timeout_seconds: 1, max_chars: 70000, retries: 9}\n",
        4,
        ("routing.confirm_seconds", "send.timeout_seconds", "send.max_chars", "send.retries"),
    ),
    # 服务指令词和确认词的格式
    (
        "hub: {label: ''}\nrouting: {confirm_words: ['对', '好,的']}\n",
        2,
        ("hub.label", "routing.confirm_words"),
    ),
    # 一条里放不下正文
    ("send: {part_chars: 150}\n", 1, ("send.part_chars",)),
    # 局域网开关和地址要一致
    ("server: {lan: true, host: 192.168.1.5}\n", 1, ("server.host",)),
]


@pytest.mark.parametrize(("text", "count", "keys"), SPEC_CONFIG_CASES)
def test_the_config_scenarios_of_the_spec_stop_serve_before_anything_is_touched(
    serve, home, config_file, text, count, keys
):
    config_file.write_text(text, encoding="utf-8")
    made: list[object] = []

    result = serve(None, upstream_factory=lambda config: made.append(config))

    assert result.exit_code == 1
    assert f"配置有 {count} 处错误" in result.output
    for key in keys:
        assert key in result.output
    assert not home.exists()
    assert made == []
    assert serve.calls["steps"] == ["config"]  # 只进了第一步"读配置"，没有第二步


def test_a_config_file_that_does_not_exist_is_an_error(env, home, tmp_path):
    result = CliRunner().invoke(
        app, ["serve", "--home", str(home), "--config", str(tmp_path / "nope.yaml")], obj=env
    )

    assert result.exit_code == 1
    assert "配置文件不存在" in result.output
    assert not home.exists()


def test_a_running_instance_makes_serve_exit_with_2(serve, home):
    HubPaths(home).home.mkdir(mode=0o700, parents=True)
    held = acquire_instance_lock(HubPaths(home))

    try:
        result = serve()
    finally:
        held.release()

    assert result.exit_code == 2
    assert f"已经有一个在运行（进程号 {os.getpid()}）" in result.output
    assert not HubPaths(home).db.exists()  # 没有碰数据库


def test_a_database_newer_than_the_program_makes_serve_exit_with_3(serve, home, clock):
    paths = HubPaths(home)
    paths.home.mkdir(mode=0o700, parents=True)
    migrate(paths, clock=clock, tz=ZoneInfo("Asia/Shanghai"))
    db = Database(paths.db)
    with db.txn() as conn:
        conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    db.close()

    result = serve()

    assert result.exit_code == 3
    assert "数据库版本比程序新，请升级程序，不要回退" in result.output


def test_a_taken_port_makes_serve_exit_with_1(serve, config_file):
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        config_file.write_text(f"server:\n  port: {port}\n", encoding="utf-8")

        result = serve()

    assert result.exit_code == 1
    assert f"端口 {port} 已被占用" in result.output


def test_a_forced_exit_ends_the_process_with_1(serve, monkeypatch):
    real_stop = Service.stop

    def stop_and_report_forced(self) -> int:
        real_stop(self)  # 真的收拾干净（测试进程还要用），但报告的是"强退"
        return 1

    monkeypatch.setattr(Service, "stop", stop_and_report_forced)

    result = serve()

    assert isinstance(result.exception, Exited)
    assert serve.calls["exits"] == [1]


def test_the_stop_request_handlers_are_given_back_even_when_startup_fails(serve, config_file):
    stopper = Stopper()
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        config_file.write_text(f"server:\n  port: {blocker.getsockname()[1]}\n", encoding="utf-8")

        serve(stopper)

    assert (stopper.installed, stopper.restored) == (True, True)


# ---------------------------------------------------------------- 真的停止请求（信号）


@pytest.mark.skipif(
    threading.current_thread() is not threading.main_thread(),
    reason="信号处理函数只能在主线程里装",
)
def test_a_signal_sets_the_stop_request_and_the_old_handlers_come_back():
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    stop = StopRequests()

    stop.install()
    try:
        assert signal.getsignal(signal.SIGTERM) != before[signal.SIGTERM]
        os.kill(os.getpid(), signal.SIGTERM)
        stop.wait()  # 已经收到了：马上返回
    finally:
        stop.restore()

    assert {sig: signal.getsignal(sig) for sig in before} == before


def test_a_stop_request_made_before_waiting_is_not_lost():
    stop = StopRequests()

    stop.request()

    done = threading.Event()
    waiter = threading.Thread(target=lambda: (stop.wait(), done.set()), daemon=True)
    waiter.start()
    assert done.wait(5)
