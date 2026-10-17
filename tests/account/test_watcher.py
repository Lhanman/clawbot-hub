"""凭证监视线程：每 5 秒比对一次 `account.json` 和数据库，变了就换上新凭证并叫醒收消息线程。

规格 `hub-login`「还没绑定也能启动」「登录后服务自动换上新凭证」。比对本身（补做数据库事务、
记摘要、清失效状态、-14 的判定）在 `tests/login/` 里测；这里测线程的节奏。
"""

from __future__ import annotations

import re
import threading

import pytest

from clawbot_hub.account import AccountStore
from clawbot_hub.clock import RealClock
from clawbot_hub.loginstate import LoginMonitor, SyncResult
from clawbot_hub.watcher import WATCH_INTERVAL_SECONDS, CredentialWatcher
from tests.login.conftest import account_file, events, login_row, seed_conversation

REAL = RealClock()


@pytest.fixture
def store(paths) -> AccountStore:
    return AccountStore(paths)


@pytest.fixture
def monitor(paths, database, store, clock) -> LoginMonitor:
    return LoginMonitor(paths, database, store, clock)


class Ticks:
    """数监视线程一共比对了几次，让测试能确定地等"第 n 次比对做完了"。"""

    def __init__(self, monitor: LoginMonitor) -> None:
        self.count = 0
        self._condition = threading.Condition()
        real = monitor.sync

        def counted() -> SyncResult:
            try:
                return real()
            finally:
                with self._condition:
                    self.count += 1
                    self._condition.notify_all()

        monitor.sync = counted  # type: ignore[method-assign]

    def wait_for(self, count: int, timeout: float = 5.0) -> None:
        with self._condition:
            if not self._condition.wait_for(lambda: self.count >= count, timeout):
                raise AssertionError(
                    f"{timeout} 秒（真实时间）内只比对了 {self.count} 次，要 {count}"
                )


@pytest.fixture
def ticks(monitor) -> Ticks:
    return Ticks(monitor)


@pytest.fixture
def watcher(monitor, clock, ticks):
    running = CredentialWatcher(monitor, clock)
    yield running
    running.stop()


def test_the_interval_is_five_seconds():
    assert WATCH_INTERVAL_SECONDS == 5.0


# ------------------------------------------------------------------ 节奏


def test_between_two_comparisons_it_waits_exactly_five_seconds(watcher, ticks, clock):
    watcher.start()
    ticks.wait_for(1)
    clock.wait_until_blocked(1)
    assert clock.pending_deadlines() == [clock.now_ms() + 5_000]

    clock.advance(5)
    ticks.wait_for(2)
    clock.wait_until_blocked(1)

    assert clock.pending_deadlines() == [clock.now_ms() + 5_000]


def test_it_compares_right_away_and_then_every_five_seconds(watcher, ticks, clock):
    watcher.start()
    ticks.wait_for(1)
    clock.wait_until_blocked(1)

    clock.advance(4.9)
    clock.wait_until_blocked(1)
    assert ticks.count == 1

    clock.advance(0.1)
    ticks.wait_for(2)
    clock.wait_until_blocked(1)

    clock.advance(5)
    ticks.wait_for(3)


def test_new_credentials_are_picked_up_within_five_seconds(
    watcher, ticks, store, monitor, database, clock
):
    watcher.start()
    ticks.wait_for(1)
    clock.wait_until_blocked(1)
    seed_conversation(database)
    store.write(account_file())  # 另一个终端 `hubctl login` 成功了

    clock.advance(4.9)
    clock.wait_until_blocked(1)
    assert login_row(database).digest is None  # 还没到点

    clock.advance(0.1)
    assert monitor.wake.wait(REAL, 5)  # 收消息线程被叫醒
    assert login_row(database).state == "ok"
    assert login_row(database).digest == account_file().digest
    assert monitor.account() == account_file()
    assert (
        database.conn().execute("SELECT count(*) FROM windows WHERE voided = 0").fetchone()[0] == 0
    )


def test_nothing_is_bound_yet_so_it_keeps_looking_every_five_seconds(
    watcher, ticks, monitor, database, clock
):
    watcher.start()
    for round_number in range(1, 4):
        ticks.wait_for(round_number)
        clock.wait_until_blocked(1)
        assert login_row(database).state == "missing"
        assert not monitor.wake.is_set()
        clock.advance(WATCH_INTERVAL_SECONDS)
    ticks.wait_for(4)


def test_the_file_appearing_later_starts_everything_without_a_restart(
    watcher, ticks, store, monitor, database, clock
):
    watcher.start()
    ticks.wait_for(1)
    clock.wait_until_blocked(1)
    clock.advance(5)
    ticks.wait_for(2)
    clock.wait_until_blocked(1)
    store.write(account_file())

    clock.advance(5)

    assert monitor.wake.wait(REAL, 5)
    assert login_row(database).state == "ok"


def test_a_change_of_updates_buf_alone_does_not_wake_anybody(watcher, ticks, store, monitor, clock):
    store.write(account_file())
    watcher.start()
    ticks.wait_for(1)
    clock.wait_until_blocked(1)
    monitor.wake.clear()
    store.save_progress(account_file().bot_token, "new-progress")

    clock.advance(5)
    ticks.wait_for(2)

    assert not monitor.wake.is_set()


def test_a_custom_interval(monitor, clock, ticks):
    quick = CredentialWatcher(monitor, clock, interval_seconds=1.0)
    try:
        quick.start()
        ticks.wait_for(1)
        clock.wait_until_blocked(1)
        assert clock.pending_deadlines() == [clock.now_ms() + 1_000]

        clock.advance(1)

        ticks.wait_for(2)
    finally:
        quick.stop()


@pytest.mark.parametrize("seconds", [0, -1, -0.5])
def test_an_interval_that_would_spin_is_refused(monitor, clock, seconds):
    with pytest.raises(ValueError, match=rf"间隔要大于 0 秒：{re.escape(str(seconds))}"):
        CredentialWatcher(monitor, clock, interval_seconds=seconds)


# ------------------------------------------------------------------ 出错


def test_one_failed_comparison_does_not_end_the_watching(monitor, clock, store, database, caplog):
    failures = {"left": 1}
    real = monitor.sync
    done = threading.Event()

    def flaky() -> SyncResult:
        if failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("database is locked，里面有 tok-secret-111")
        try:
            return real()
        finally:
            done.set()

    monitor.sync = flaky  # type: ignore[method-assign]
    watcher = CredentialWatcher(monitor, clock)
    try:
        with caplog.at_level("ERROR"):
            watcher.start()
            clock.wait_until_blocked(1)
            store.write(account_file())
            clock.advance(5)
            assert done.wait(5)
    finally:
        watcher.stop()

    assert login_row(database).state == "ok"
    assert "RuntimeError" in caplog.text
    assert "tok-secret-111" not in caplog.text


# ------------------------------------------------------------------ 起停


def test_stop_ends_the_thread_without_the_clock_moving(watcher, ticks, clock):
    watcher.start()
    ticks.wait_for(1)
    clock.wait_until_blocked(1)

    watcher.stop()

    assert not watcher.running
    assert clock.blocked_count == 0


def test_no_comparison_happens_after_stop(watcher, ticks, clock):
    watcher.start()
    ticks.wait_for(1)
    clock.wait_until_blocked(1)
    watcher.stop()

    clock.advance(60)

    assert ticks.count == 1


def test_a_watcher_that_was_stopped_never_compares_even_if_started_later(watcher, ticks):
    watcher.stop()
    watcher.start()
    watcher.stop()  # 等线程退出

    assert not watcher.running
    assert ticks.count == 0


def test_stop_without_start_and_twice_is_fine(watcher):
    watcher.stop()
    watcher.stop()


def test_starting_twice_is_a_mistake(watcher, ticks):
    watcher.start()
    ticks.wait_for(1)

    with pytest.raises(RuntimeError, match="已经"):
        watcher.start()


def test_the_watcher_thread_is_a_daemon(watcher, ticks):
    watcher.start()
    ticks.wait_for(1)

    (thread,) = [t for t in threading.enumerate() if t.name == "credential-watcher"]
    assert thread.daemon


def test_tick_does_one_comparison_in_the_callers_thread(watcher, store, database, monitor):
    store.write(account_file())

    result = watcher.tick()

    assert result.changed
    assert login_row(database).state == "ok"
    assert monitor.wake.is_set()
    assert [e["kind"] for e in events(database)] == ["login"]
