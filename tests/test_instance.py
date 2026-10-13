"""单实例锁 `hub.lock` 和"服务在不在运行"的判断（规格 `hub-login`「单实例」、`hub-cli`）。"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys

import pytest

from clawbot_hub import instance
from clawbot_hub.instance import (
    HEARTBEAT_STALE_SECONDS,
    InstanceBusy,
    acquire_instance_lock,
    pid_alive,
    read_run_info,
    service_running,
)
from tests.support.fake_clock import FakeClock

# ---------------------------------------------------------------- 锁


def test_the_lock_can_be_taken_and_holds_our_process_number(paths):
    lock = acquire_instance_lock(paths)
    try:
        assert paths.lock.read_text().strip() == str(os.getpid())
    finally:
        lock.release()


def test_the_lock_file_is_private(paths):
    with acquire_instance_lock(paths):
        assert stat.S_IMODE(paths.lock.stat().st_mode) == 0o600


def test_a_second_attempt_is_refused_and_names_the_holder(paths):
    with acquire_instance_lock(paths):
        with pytest.raises(InstanceBusy) as error:
            acquire_instance_lock(paths)
    assert error.value.pid == os.getpid()
    assert str(error.value) == f"已经有一个在运行（进程号 {os.getpid()}）"


def test_a_refused_attempt_leaves_the_holders_number_alone(paths):
    with acquire_instance_lock(paths):
        with pytest.raises(InstanceBusy):
            acquire_instance_lock(paths)
        assert paths.lock.read_text().strip() == str(os.getpid())


def test_when_the_holder_left_no_readable_number_the_message_just_says_so(paths):
    with acquire_instance_lock(paths):
        paths.lock.write_text("not a number")
        with pytest.raises(InstanceBusy) as error:
            acquire_instance_lock(paths)
        assert error.value.pid is None
        assert str(error.value) == "已经有一个在运行"


def open_fds() -> int:
    return len(os.listdir("/dev/fd"))


def test_a_refused_attempt_does_not_leak_the_file_it_opened(paths):
    with acquire_instance_lock(paths):
        before = open_fds()
        for _ in range(5):
            with pytest.raises(InstanceBusy):
                acquire_instance_lock(paths)
        assert open_fds() == before


def test_a_holder_number_that_cannot_be_read_is_just_unknown(paths, monkeypatch):
    with acquire_instance_lock(paths):

        def unreadable(fd, length, offset):
            raise OSError("读不出来")

        monkeypatch.setattr(os, "pread", unreadable)
        with pytest.raises(InstanceBusy) as error:
            acquire_instance_lock(paths)
        assert error.value.pid is None


def test_releasing_lets_the_next_one_in(paths):
    first = acquire_instance_lock(paths)
    first.release()
    second = acquire_instance_lock(paths)
    second.release()


def test_releasing_twice_is_harmless(paths):
    lock = acquire_instance_lock(paths)
    lock.release()
    lock.release()
    acquire_instance_lock(paths).release()


def test_the_with_block_hands_back_the_lock_itself(paths):
    with acquire_instance_lock(paths) as lock:
        with pytest.raises(InstanceBusy):
            acquire_instance_lock(paths)
        lock.release()  # 手里的就是真的那把：放掉以后别人马上能拿
        acquire_instance_lock(paths).release()


def test_the_with_block_releases_the_lock_even_after_an_error(paths):
    with pytest.raises(RuntimeError):
        with acquire_instance_lock(paths):
            raise RuntimeError("出错了")
    acquire_instance_lock(paths).release()


def test_a_lock_file_left_behind_by_a_dead_process_is_no_obstacle(paths):
    paths.lock.write_text("999999\n")
    with acquire_instance_lock(paths):
        assert paths.lock.read_text().strip() == str(os.getpid())


def test_the_old_number_does_not_linger_when_the_new_one_is_shorter(paths):
    paths.lock.write_text("1234567890123\n")
    with acquire_instance_lock(paths):
        assert paths.lock.read_text() == f"{os.getpid()}\n"


def test_a_killed_holder_does_not_leave_a_zombie_lock(paths):
    code = (
        "import sys, time\n"
        "from clawbot_hub.instance import acquire_instance_lock\n"
        "from clawbot_hub.paths import HubPaths\n"
        "from pathlib import Path\n"
        "lock = acquire_instance_lock(HubPaths(Path(sys.argv[1])))\n"
        "print('locked', flush=True)\n"
        "time.sleep(60)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(paths.home)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(InstanceBusy) as error:
            acquire_instance_lock(paths)
        assert error.value.pid == child.pid
        child.kill()  # kill -9
        child.wait(10)
        acquire_instance_lock(paths).release()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(10)
        child.stdout.close()


# ---------------------------------------------------------------- 进程还在不在


def test_our_own_process_is_alive():
    assert pid_alive(os.getpid())


def test_a_process_that_has_exited_is_not_alive():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(10)
    assert not pid_alive(child.pid)


@pytest.mark.parametrize("pid", [0, -1, -12345])
def test_numbers_that_are_not_processes_are_never_alive(pid, monkeypatch):
    # 0 和负数送给 kill 会变成"整个进程组"，绝不能让它们走到 kill
    monkeypatch.setattr(instance.os, "kill", lambda *a: pytest.fail("不该调用 kill"))
    assert not pid_alive(pid)


def test_a_process_of_another_user_counts_as_alive(monkeypatch):
    def deny(pid, sig):
        raise PermissionError

    monkeypatch.setattr(instance.os, "kill", deny)
    assert pid_alive(4242)


def test_the_check_sends_no_signal_that_would_disturb_the_process(monkeypatch):
    seen = []
    monkeypatch.setattr(instance.os, "kill", lambda pid, sig: seen.append((pid, sig)))
    assert pid_alive(4242)
    assert seen == [(4242, 0)]


# ---------------------------------------------------------------- run.json


def write_run(paths, **overrides) -> dict:
    data = {
        "pid": os.getpid(),
        "started_ms": 1_000,
        "version": "0.1.0",
        "listen": "127.0.0.1:8790",
        "profile": "legacy",
        "confirm": "observe",
        "last_heartbeat_ms": 1_791_770_400_000,
        "clean_exit": False,
    }
    data.update(overrides)
    paths.run.write_text(json.dumps(data), encoding="utf-8")
    return data


def test_no_run_file_means_nothing_has_run(paths):
    assert read_run_info(paths) is None
    assert service_running(paths, FakeClock()) is None


def test_the_run_file_is_read_field_by_field(paths):
    write_run(paths, clean_exit=True)
    info = read_run_info(paths)
    assert (info.pid, info.started_ms, info.version) == (os.getpid(), 1_000, "0.1.0")
    assert (info.listen, info.profile, info.confirm) == ("127.0.0.1:8790", "legacy", "observe")
    assert (info.last_heartbeat_ms, info.clean_exit) == (1_791_770_400_000, True)


@pytest.mark.parametrize(
    "content",
    ["", "not json", "[]", '{"pid": "x"}', '{"pid": 5}', '{"pid": true, "last_heartbeat_ms": 1}'],
)
def test_a_run_file_that_makes_no_sense_is_the_same_as_none(paths, content):
    paths.run.write_text(content, encoding="utf-8")
    assert read_run_info(paths) is None
    assert service_running(paths, FakeClock()) is None


def test_only_the_pid_and_the_heartbeat_are_needed_to_make_sense_of_it(paths):
    paths.run.write_text(json.dumps({"pid": 7, "last_heartbeat_ms": 5}), encoding="utf-8")
    info = read_run_info(paths)
    assert (info.pid, info.last_heartbeat_ms) == (7, 5)
    assert (info.started_ms, info.version, info.listen, info.clean_exit) == (0, "", "", False)


@pytest.mark.parametrize("value", [1, "true", "yes", [True], {"a": 1}])
def test_only_a_real_true_counts_as_a_clean_exit(paths, value):
    write_run(paths, clean_exit=value)
    assert read_run_info(paths).clean_exit is False


@pytest.mark.parametrize("value", [5, None, ["x"], {"a": 1}, True])
def test_a_text_field_that_is_not_text_is_left_empty(paths, value):
    write_run(paths, version=value, listen=value, profile=value, confirm=value)
    info = read_run_info(paths)
    assert (info.version, info.listen, info.profile, info.confirm) == ("", "", "", "")


def test_a_live_process_with_a_fresh_heartbeat_is_a_running_service(paths):
    clock = FakeClock()
    write_run(paths, last_heartbeat_ms=clock.now_ms() - 20_000)
    assert service_running(paths, clock).pid == os.getpid()


def test_the_heartbeat_may_be_exactly_a_minute_old_but_not_older(paths):
    clock = FakeClock()
    write_run(paths, last_heartbeat_ms=clock.now_ms() - HEARTBEAT_STALE_SECONDS * 1000)
    assert service_running(paths, clock) is not None
    write_run(paths, last_heartbeat_ms=clock.now_ms() - HEARTBEAT_STALE_SECONDS * 1000 - 1)
    assert service_running(paths, clock) is None


def test_the_stale_limit_is_a_minute():
    assert HEARTBEAT_STALE_SECONDS == 60


def test_a_live_process_whose_heartbeat_is_five_minutes_old_is_not_running(paths):
    clock = FakeClock()
    write_run(paths, last_heartbeat_ms=clock.now_ms() - 300_000)
    assert service_running(paths, clock) is None


def test_a_fresh_heartbeat_of_a_process_that_is_gone_is_not_running(paths):
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(10)
    clock = FakeClock()
    write_run(paths, pid=child.pid, last_heartbeat_ms=clock.now_ms())
    assert service_running(paths, clock) is None


def test_a_service_that_has_said_it_exited_cleanly_is_not_running(paths):
    clock = FakeClock()
    write_run(paths, last_heartbeat_ms=clock.now_ms(), clean_exit=True)
    assert service_running(paths, clock) is None


def test_a_heartbeat_from_the_future_still_counts_as_fresh(paths):
    # 时钟被调慢过：不能因此把活着的服务当成没在运行
    clock = FakeClock()
    write_run(paths, last_heartbeat_ms=clock.now_ms() + 5_000)
    assert service_running(paths, clock) is not None


def test_the_check_reads_the_clock_it_was_given(paths):
    clock = FakeClock()
    write_run(paths, last_heartbeat_ms=clock.now_ms())
    assert service_running(paths, clock) is not None
    clock.advance(61)
    assert service_running(paths, clock) is None
