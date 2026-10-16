"""`hubctl serve` 的停止请求和强行退出（任务 D6，规格 `hub-deploy`「正常关闭」）。

`StopRequests` 管 `SIGTERM` / `SIGINT`：处理函数里只改一个标记（不拿锁，免得赶上主线程正拿着同一把锁
而死锁），等的一方每隔一小会儿自己看一眼；别的线程可以用 `request()` 请求停止，那个会马上叫醒。
`_hard_exit` 是关闭时间用完以后的强退：先把日志和输出刷出去，再不跑任何清理地结束进程。

装上真的信号处理函数、对着自己发信号的测试只能在主线程里做；`hubctl serve` 对真信号的反应（退出码、
`run.json`、端口和锁）在 `tests/process/test_lifecycle.py` 里用真的子进程测。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any

import pytest

import clawbot_hub.cli as cli_module
from clawbot_hub.cli import StopRequests

main_thread_only = pytest.mark.skipif(
    threading.current_thread() is not threading.main_thread(),
    reason="信号处理函数只能在主线程里装",
)


def after(seconds: float, action) -> threading.Timer:
    timer = threading.Timer(seconds, action)
    timer.daemon = True
    timer.start()
    return timer


# ---------------------------------------------------------------- 等和叫醒


def test_the_poll_interval_is_a_fifth_of_a_second():
    assert StopRequests.POLL_SECONDS == 0.2
    assert StopRequests.SIGNALS == (signal.SIGTERM, signal.SIGINT)


def test_a_request_from_another_thread_wakes_the_waiter_at_once():
    stop = StopRequests()
    stop.POLL_SECONDS = 2.0  # 等的一方要隔两秒才会自己看一眼：返回得快就只能是被叫醒的
    after(0.05, stop.request)
    started = time.monotonic()

    stop.wait()

    assert time.monotonic() - started < 1.0


def test_waiting_sleeps_instead_of_spinning():
    stop = StopRequests()
    after(0.5, stop.request)
    cpu_before = time.process_time()

    stop.wait()

    assert time.process_time() - cpu_before < 0.25  # 空转的话 0.5 秒就是 0.5 秒的 CPU


def test_a_stop_request_made_before_waiting_returns_at_once():
    stop = StopRequests()
    stop.request()
    started = time.monotonic()

    stop.wait()

    assert time.monotonic() - started < 0.15


@main_thread_only
def test_a_signal_is_noticed_within_about_one_poll():
    stop = StopRequests()
    stop.install()
    try:
        after(0.05, lambda: os.kill(os.getpid(), signal.SIGTERM))
        started = time.monotonic()

        stop.wait()

        elapsed = time.monotonic() - started
    finally:
        stop.restore()
    assert elapsed < 0.9  # 每 0.2 秒看一眼标记；这里留足余量，免得机器忙时误报


# ---------------------------------------------------------------- 装上和还回去


class FakeSignals:
    """代替 `cli.py` 里的 `signal` 模块：不碰真的信号，记下装了什么；`previous` 是原来装着的。"""

    def __init__(self, previous: dict[int, Any]) -> None:
        self.previous = previous
        self.calls: list[tuple[int, Any]] = []
        self.SIGTERM = signal.SIGTERM
        self.SIGINT = signal.SIGINT
        self.SIG_DFL = signal.SIG_DFL

    def signal(self, number: int, handler: Any) -> Any:
        self.calls.append((number, handler))
        return self.previous.get(number)


def previous_handler(number, frame):  # 原来装着的处理函数
    pass


def test_the_old_handlers_are_put_back_and_only_once(monkeypatch):
    fake = FakeSignals(
        {signal.SIGTERM: previous_handler, signal.SIGINT: signal.default_int_handler}
    )
    monkeypatch.setattr(cli_module, "signal", fake)
    stop = StopRequests()

    stop.install()
    assert [number for number, _ in fake.calls] == [signal.SIGTERM, signal.SIGINT]
    assert all(handler == stop._on_signal for _, handler in fake.calls)

    stop.restore()
    assert fake.calls[2:] == [
        (signal.SIGTERM, previous_handler),
        (signal.SIGINT, signal.default_int_handler),
    ]

    stop.restore()  # 还回去以后再还不动任何东西（别人后来装上的不该被盖掉）
    assert len(fake.calls) == 4


def test_a_handler_that_python_did_not_install_is_replaced_by_the_default(monkeypatch):
    """`signal.signal` 返回 `None` 表示原来的处理函数不是 Python 装的：还回去用系统默认的。"""
    fake = FakeSignals({signal.SIGTERM: None, signal.SIGINT: None})
    monkeypatch.setattr(cli_module, "signal", fake)
    stop = StopRequests()
    stop.install()

    stop.restore()

    assert fake.calls[2:] == [(signal.SIGTERM, signal.SIG_DFL), (signal.SIGINT, signal.SIG_DFL)]


def test_the_handler_only_sets_a_flag():
    stop = StopRequests()
    waiter_done = threading.Event()
    waiter = threading.Thread(target=lambda: (stop.wait(), waiter_done.set()), daemon=True)
    waiter.start()
    assert not waiter_done.wait(0.1)  # 还没有请求：一直等

    stop._on_signal(signal.SIGTERM, None)  # 就是处理函数做的那一件事

    assert waiter_done.wait(2)  # 等的一方每 0.2 秒看一眼，看到了就走


# ---------------------------------------------------------------- 强行退出


def test_a_hard_exit_flushes_what_was_written_and_skips_everything_else():
    code = (
        "import atexit, sys\n"
        "atexit.register(lambda: print('ATEXIT-RAN'))\n"
        "sys.stdout.write('partial-out')\n"  # 没换行：管道里是整块缓冲，不刷就丢
        "sys.stderr.write('partial-err')\n"
        "from clawbot_hub.cli import _hard_exit\n"
        "_hard_exit(7)\n"
    )
    env = {key: value for key, value in os.environ.items() if key != "PYTHONUNBUFFERED"}

    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60
    )

    assert result.returncode == 7
    assert result.stdout == "partial-out"  # 刷出来了，登记的退出清理没跑
    assert "partial-err" in result.stderr


def test_the_default_hard_exit_is_the_real_one():
    from clawbot_hub.cli import Env

    assert Env().hard_exit is cli_module._hard_exit
    assert Env().stop_requests is StopRequests
