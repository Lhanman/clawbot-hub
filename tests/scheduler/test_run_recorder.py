"""`run.json` 的写法：服务每次启动写一份新的，定时任务每个周期（30 秒）更新一次心跳，正常退出时
标上"正常退出"。`hubctl` 用它判断服务在不在运行，`service_restarted` 告警用它的最后心跳时间。

字段：`pid`、`started_ms`、`version`、`listen`、`profile`、`confirm`、`last_heartbeat_ms`、
`clean_exit`。一律先写临时文件再改名（读的一方永远读到完整的一份），文件创建时就是 0600。
"""

from __future__ import annotations

import json
import os
import stat
import threading

from clawbot_hub.instance import RunInfo, RunRecorder, read_run_info, service_running
from tests.support.fake_clock import FakeClock


def recorder(paths, clock: FakeClock, **fields) -> RunRecorder:
    defaults = {
        "version": "1.2.3",
        "listen": "127.0.0.1:8790",
        "profile": "legacy",
        "confirm": "observe",
    }
    return RunRecorder(paths, clock, **{**defaults, **fields})


def test_starting_writes_every_field(paths):
    clock = FakeClock()
    started = clock.now_ms()

    recorder(paths, clock).start()

    assert json.loads(paths.run.read_text(encoding="utf-8")) == {
        "pid": os.getpid(),
        "started_ms": started,
        "version": "1.2.3",
        "listen": "127.0.0.1:8790",
        "profile": "legacy",
        "confirm": "observe",
        "last_heartbeat_ms": started,
        "clean_exit": False,
    }


def test_what_was_written_can_be_read_back(paths):
    clock = FakeClock()
    recorder(paths, clock, profile="official", confirm="strict").start()

    info = read_run_info(paths)

    assert info == RunInfo(
        pid=os.getpid(),
        last_heartbeat_ms=clock.now_ms(),
        started_ms=clock.now_ms(),
        version="1.2.3",
        listen="127.0.0.1:8790",
        profile="official",
        confirm="strict",
        clean_exit=False,
    )


def test_a_beat_moves_only_the_heartbeat(paths):
    clock = FakeClock()
    run = recorder(paths, clock)
    run.start()
    started = clock.now_ms()

    clock.advance(30)
    run.beat()

    info = read_run_info(paths)
    assert info.last_heartbeat_ms == started + 30_000
    assert info.started_ms == started
    assert (info.version, info.listen, info.profile, info.confirm) == (
        "1.2.3",
        "127.0.0.1:8790",
        "legacy",
        "observe",
    )
    assert not info.clean_exit


def test_finishing_marks_the_exit_as_normal(paths):
    clock = FakeClock()
    run = recorder(paths, clock)
    run.start()
    assert service_running(paths, clock) is not None

    clock.advance(5)
    run.finish()

    info = read_run_info(paths)
    assert info.clean_exit
    assert info.last_heartbeat_ms == clock.now_ms()
    assert service_running(paths, clock) is None


def test_a_late_beat_cannot_undo_the_normal_exit(paths):
    """停服务时定时任务线程可能比主线程慢一步：收尾以后的心跳不能把标记改回去。"""
    clock = FakeClock()
    run = recorder(paths, clock)
    run.start()
    run.finish()

    clock.advance(30)
    run.beat()

    assert read_run_info(paths).clean_exit


def test_beating_before_starting_does_nothing(paths):
    run = recorder(paths, FakeClock())

    run.beat()
    run.finish()

    assert not paths.run.exists()


def test_finishing_twice_leaves_the_first_record_alone(paths):
    clock = FakeClock()
    run = recorder(paths, clock)
    run.start()
    run.finish()
    first = paths.run.read_bytes()

    clock.advance(10)
    run.finish()

    assert paths.run.read_bytes() == first


def test_a_new_start_after_finishing_beats_again(paths):
    clock = FakeClock()
    run = recorder(paths, clock)
    run.start()
    run.finish()
    clock.advance(5)
    run.start()

    clock.advance(30)
    run.beat()

    info = read_run_info(paths)
    assert info.last_heartbeat_ms == clock.now_ms()
    assert not info.clean_exit


def test_only_starting_and_finishing_sync_to_disk(paths, monkeypatch):
    """心跳每 30 秒一次，丢了无害，不落盘；启动和正常退出的记录落盘（文件一次、目录一次）。"""
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real(fd))[1])
    run = recorder(paths, FakeClock())

    run.start()
    assert len(synced) == 2
    synced.clear()

    run.beat()
    assert synced == []

    run.finish()
    assert len(synced) == 2


def test_a_new_start_replaces_the_previous_runs_file(paths):
    clock = FakeClock()
    first = recorder(paths, clock)
    first.start()
    first.finish()
    clock.advance(3600)

    recorder(paths, clock, version="2.0.0").start()

    info = read_run_info(paths)
    assert not info.clean_exit
    assert info.version == "2.0.0"
    assert info.started_ms == clock.now_ms()


def test_the_service_counts_as_running_only_while_beats_keep_coming(paths):
    clock = FakeClock()
    run = recorder(paths, clock)
    run.start()

    clock.advance(59)
    assert service_running(paths, clock) is not None
    clock.advance(30)
    run.beat()
    clock.advance(59)
    assert service_running(paths, clock) is not None
    clock.advance(2)

    assert service_running(paths, clock) is None


def test_the_file_is_private_from_the_moment_it_exists(paths):
    previous = os.umask(0)  # 就算进程的 umask 是 0，也不能先宽后紧
    try:
        run = recorder(paths, FakeClock())
        run.start()
        run.beat()
        run.finish()
    finally:
        os.umask(previous)

    assert stat.S_IMODE(os.stat(paths.run).st_mode) == 0o600


def test_no_temporary_file_is_left_behind(paths):
    run = recorder(paths, FakeClock())

    run.start()
    run.beat()
    run.finish()

    assert sorted(entry.name for entry in paths.home.iterdir() if "run" in entry.name) == [
        "run.json"
    ]


def test_a_reader_never_sees_a_half_written_file(paths):
    clock = FakeClock()
    run = recorder(paths, clock)
    run.start()
    stop = threading.Event()
    unreadable = []

    def read_forever() -> None:
        while not stop.is_set():
            if read_run_info(paths) is None:
                unreadable.append(1)

    reader = threading.Thread(target=read_forever, daemon=True)
    reader.start()
    try:
        for _ in range(300):
            clock.advance(30)
            run.beat()
    finally:
        stop.set()
        reader.join(5)

    assert unreadable == []
