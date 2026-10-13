"""登录失效的状态机（函数层）：收到 -14 以后算不算失效、怎么恢复。

规格 `hub-login`「登录后服务自动换上新凭证」「登录失效」。探测间隔归收消息线程（C3），
各登录状态下发送得到什么结果归发送（B1），这里只管状态怎么变。
"""

from __future__ import annotations

import os
import threading

import pytest

from clawbot_hub.loginstate import (
    LOGIN_GRACE_SECONDS,
    LoginMonitor,
    Verdict,
    probe_proves_recovery,
    write_scan_marker,
)
from clawbot_hub.upstream.base import (
    IncomingMessage,
    PollFailed,
    SessionExpired,
    Updates,
)
from tests.login.conftest import (
    TOKEN,
    Calls,
    account_file,
    conversation_tokens,
    dead_pid,
    events,
    login_row,
    seed_conversation,
)
from tests.support.threads import run_in_thread


@pytest.fixture
def account(store, monitor, clock):
    """已经绑定并且比对过，时间走过了换凭证的 60 秒宽限。"""
    bound = account_file()
    store.write(bound)
    monitor.sync()
    clock.advance(LOGIN_GRACE_SECONDS + 1)
    return bound


def rebind(store, **overrides):
    other = account_file(**overrides)
    store.write(other)
    return other


# ------------------------------------------------------------------ 判定顺序


def test_the_grace_is_sixty_seconds():
    assert LOGIN_GRACE_SECONDS == 60


def test_a_plain_minus_14_expires_the_login(monitor, account, database, calls, clock):
    verdict = monitor.session_expired(account.digest)

    assert verdict is Verdict.EXPIRED
    row = login_row(database)
    assert (row.state, row.since_ms, row.episode) == ("expired", clock.now_ms(), 1)
    assert calls.expired == [1]
    assert calls.restored == []
    # 摘要和换凭证的时刻都没动
    assert row.digest == account.digest


def test_expiry_is_logged_without_any_credential(monitor, account, database):
    monitor.session_expired(account.digest)

    details = [(e["kind"], e["level"], e["detail"]) for e in events(database)]
    kind, level, detail = details[-1]
    assert (kind, level) == ("login", "WARNING")
    assert "-14" in detail
    assert TOKEN not in " ".join(d for _, _, d in details)


def test_the_expiry_does_not_touch_the_conversation(monitor, account, database):
    seed_conversation(database)
    before = conversation_tokens(database)

    monitor.session_expired(account.digest)

    assert conversation_tokens(database) == before


def test_a_second_minus_14_in_the_same_episode_changes_nothing(
    monitor, account, database, calls, clock
):
    monitor.session_expired(account.digest)
    first = login_row(database)
    clock.advance(600)

    verdict = monitor.session_expired(account.digest)

    assert verdict is Verdict.EXPIRED
    assert login_row(database) == first
    assert calls.expired == [1]
    assert len([e for e in events(database) if e["level"] == "WARNING"]) == 1


def test_progress_written_between_polls_does_not_make_it_a_new_credential(
    monitor, account, store, database
):
    """收消息进度每批都改，随后的 -14 立刻按失效处理，不会因为"文件刚被写过"而被当成换凭证。"""
    for number in range(3):
        assert store.save_progress(TOKEN, f"buf-{number}")

    verdict = monitor.session_expired(account.digest)

    assert verdict is Verdict.EXPIRED
    assert login_row(database).state == "expired"


def test_credentials_in_the_file_are_not_what_was_used(monitor, account, store, database, calls):
    rebind(store, bot_token="tok-new")
    before = login_row(database)

    verdict = monitor.session_expired(account.digest)

    assert verdict is Verdict.CHANGED
    assert login_row(database) == before  # 状态没动，监视线程随后换上新凭证
    assert calls.expired == []


def test_a_file_that_is_gone_counts_as_changed(monitor, account, paths, database, calls):
    paths.account.unlink()

    assert monitor.session_expired(account.digest) is Verdict.CHANGED
    assert login_row(database).state == "ok"
    assert calls.expired == []


def test_a_file_that_is_broken_counts_as_changed(monitor, account, paths, database, calls):
    paths.account.write_text("oops")

    assert monitor.session_expired(account.digest) is Verdict.CHANGED
    assert login_row(database).state == "ok"
    assert calls.expired == []


def test_a_caller_that_used_other_credentials_gets_changed(monitor, account, database, calls):
    """调用者手里的凭证和文件里的不是一个：不管文件有没有"刚换"，都是换了。"""
    verdict = monitor.session_expired("0" * 64)

    assert verdict is Verdict.CHANGED
    assert calls.expired == []


# ------------------------------------------------------------------ 正在扫码


def test_minus_14_while_scanning_is_ignored(monitor, account, paths, database, calls, clock):
    write_scan_marker(paths, pid=os.getpid(), started_ms=clock.now_ms())
    before = login_row(database)

    verdict = monitor.session_expired(account.digest)

    assert verdict is Verdict.SCANNING
    assert login_row(database) == before
    assert calls.expired == []
    assert [e for e in events(database) if e["level"] == "WARNING"] == []


def test_a_marker_of_a_dead_login_does_not_hide_a_real_expiry(
    monitor, account, paths, calls, clock
):
    write_scan_marker(paths, pid=dead_pid(), started_ms=clock.now_ms())

    assert monitor.session_expired(account.digest) is Verdict.EXPIRED
    assert calls.expired == [1]


def test_a_marker_older_than_twenty_minutes_does_not_hide_a_real_expiry(
    monitor, account, paths, calls, clock
):
    write_scan_marker(paths, pid=os.getpid(), started_ms=clock.now_ms() - 20 * 60_000)

    assert monitor.session_expired(account.digest) is Verdict.EXPIRED


def test_scanning_then_binding_leaves_no_alerts_at_all(
    monitor, account, store, paths, database, calls, clock
):
    write_scan_marker(paths, pid=os.getpid(), started_ms=clock.now_ms())
    assert monitor.session_expired(account.digest) is Verdict.SCANNING

    rebind(store, bot_token="tok-new")
    result = monitor.sync()

    assert result.changed and not result.recovered
    assert login_row(database).state == "ok"
    assert calls.expired == [] and calls.restored == []


# ------------------------------------------------------------------ 刚换完凭证


def fresh_binding(store, monitor, clock, **overrides):
    """换一次凭证并比对，返回新账号；这时 `creds_changed_ms` 就是现在。"""
    other = rebind(store, **overrides)
    monitor.sync()
    return other


@pytest.mark.parametrize("seconds", [0, 1, 30, 59, 60])
def test_a_minus_14_within_sixty_seconds_of_a_change_is_ignored(
    monitor, account, store, database, calls, clock, seconds
):
    other = fresh_binding(store, monitor, clock, bot_token="tok-new")
    clock.advance(seconds)
    before = login_row(database)

    verdict = monitor.session_expired(other.digest)

    assert verdict is Verdict.JUST_CHANGED
    assert login_row(database) == before
    assert calls.expired == []


@pytest.mark.parametrize("extra_ms", [1, 1_000, 3_600_000])
def test_a_minus_14_after_the_grace_is_a_real_expiry(
    monitor, account, store, database, calls, clock, extra_ms
):
    other = fresh_binding(store, monitor, clock, bot_token="tok-new")
    clock.advance(LOGIN_GRACE_SECONDS)
    clock.advance(extra_ms / 1000)

    verdict = monitor.session_expired(other.digest)

    assert verdict is Verdict.EXPIRED
    assert calls.expired == [1]


def test_a_change_time_in_the_future_gives_no_grace(monitor, account, database, calls, clock):
    """时钟被拨回去过：宁可多报一次，也不能让真正的失效被压住。"""
    with database.txn() as conn:
        conn.execute(
            "UPDATE account_state SET creds_changed_ms = ? WHERE id = 1", (clock.now_ms() + 5_000,)
        )

    assert monitor.session_expired(account.digest) is Verdict.EXPIRED
    assert calls.expired == [1]


def test_the_first_binding_has_the_grace_too(store, monitor, clock, database, calls):
    first = account_file()
    store.write(first)
    monitor.sync()
    clock.advance(30)

    assert monitor.session_expired(first.digest) is Verdict.JUST_CHANGED
    assert calls.expired == []


# ------------------------------------------------------------------ 谁压过谁


def test_changed_beats_scanning_and_just_changed(monitor, account, store, paths, clock):
    other = fresh_binding(store, monitor, clock, bot_token="tok-new")  # 刚换
    write_scan_marker(paths, pid=os.getpid(), started_ms=clock.now_ms())  # 正在扫
    rebind(store, bot_token="tok-newer")  # 文件又换了，监视线程还没比对

    assert monitor.session_expired(other.digest) is Verdict.CHANGED


def test_scanning_beats_just_changed(monitor, account, store, paths, clock):
    other = fresh_binding(store, monitor, clock, bot_token="tok-new")
    write_scan_marker(paths, pid=os.getpid(), started_ms=clock.now_ms())

    assert monitor.session_expired(other.digest) is Verdict.SCANNING


# ------------------------------------------------------------------ 恢复：摘要变了


def test_new_credentials_end_the_episode(monitor, account, store, database, calls, clock):
    monitor.session_expired(account.digest)
    clock.advance(100)
    rebind(store, bot_token="tok-new")

    result = monitor.sync()

    assert result.recovered and result.changed
    assert login_row(database).state == "ok"
    assert calls.restored == [1]


def test_a_second_episode_has_its_own_number(monitor, account, store, database, calls, clock):
    monitor.session_expired(account.digest)
    other = rebind(store, bot_token="tok-new")
    monitor.sync()
    clock.advance(LOGIN_GRACE_SECONDS + 1)

    monitor.session_expired(other.digest)

    assert login_row(database).episode == 2
    assert calls.expired == [1, 2]
    assert calls.restored == [1]


def test_expiry_survives_a_restart_without_a_second_alert(
    paths, database, store, clock, calls, monitor, account
):
    monitor.session_expired(account.digest)
    again_calls = Calls()
    again = LoginMonitor(
        paths,
        database,
        store,
        clock,
        on_expired=again_calls.expired.append,
        on_restored=again_calls.restored.append,
    )

    result = again.sync()

    assert result.state == "expired" and not result.changed
    assert login_row(database).state == "expired"
    assert again_calls.expired == [] and again_calls.restored == []


def test_the_login_that_happened_while_stopped_ends_the_episode_at_startup(
    paths, database, store, clock, calls, monitor, account
):
    monitor.session_expired(account.digest)
    rebind(store, bot_token="tok-new")  # 服务停着时扫了码
    again_calls = Calls()
    again = LoginMonitor(
        paths,
        database,
        store,
        clock,
        on_expired=again_calls.expired.append,
        on_restored=again_calls.restored.append,
    )

    again.sync()

    assert login_row(database).state == "ok"
    assert again_calls.restored == [1]


# ------------------------------------------------------------------ 恢复：探测


def good_poll() -> Updates:
    return Updates(messages=(), buf="b", longpolling_timeout_ms=None, timed_out=False)


def a_message() -> IncomingMessage:
    return IncomingMessage(
        from_user_id="u",
        message_type=1,
        context_token="c",
        at_ms=1,
        text="hi",
        has_text=True,
        message_id="1",
        seq=1,
    )


@pytest.mark.parametrize(
    ("outcome", "proves"),
    [
        (good_poll(), True),
        (Updates(messages=(a_message(),), buf="b"), True),
        (Updates(), True),
        (Updates(timed_out=True), False),
        (SessionExpired(), False),
        (PollFailed("HTTP 502", completed=False), False),
        (PollFailed("连不上", completed=False), False),
        (PollFailed("500 server error", completed=True), False),
        (PollFailed("HTTP 403", completed=True), False),
    ],
    ids=[
        "正常空",
        "带消息",
        "默认",
        "读超时不算",
        "仍是 -14",
        "HTTP 502",
        "连不上",
        "ret 500",
        "HTTP 403",
    ],
)
def test_which_poll_results_prove_the_login_is_back(outcome, proves):
    assert probe_proves_recovery(outcome) is proves


def test_a_good_probe_ends_the_episode_without_touching_the_credentials(
    monitor, account, database, calls, clock
):
    monitor.session_expired(account.digest)
    seed_conversation(database)
    before = conversation_tokens(database)
    digest_row = login_row(database)
    clock.advance(600)

    recovered = monitor.probe_finished(good_poll())

    assert recovered is True
    row = login_row(database)
    assert (row.state, row.since_ms, row.episode) == ("ok", clock.now_ms(), 1)
    assert (row.digest, row.changed_ms) == (digest_row.digest, digest_row.changed_ms)
    assert conversation_tokens(database) == before
    assert calls.restored == [1]
    assert any("恢复" in e["detail"] for e in events(database))


@pytest.mark.parametrize(
    "outcome",
    [
        Updates(timed_out=True),
        SessionExpired(),
        PollFailed("HTTP 502", completed=False),
        PollFailed("500 server error", completed=True),
    ],
    ids=["读超时", "仍是 -14", "连不上或 502", "ret 500"],
)
def test_other_probe_results_keep_the_login_expired(monitor, account, database, calls, outcome):
    monitor.session_expired(account.digest)
    before = login_row(database)

    recovered = monitor.probe_finished(outcome)

    assert recovered is False
    assert login_row(database) == before
    assert calls.restored == []
    assert not any("恢复" in e["detail"] for e in events(database))


def test_a_good_poll_while_the_login_is_fine_is_nothing_to_report(
    monitor, account, database, calls
):
    before = login_row(database)
    count = len(events(database))

    assert monitor.probe_finished(good_poll()) is False

    assert login_row(database) == before
    assert calls.restored == []
    assert len(events(database)) == count


def test_a_probe_recovery_leaves_no_grace_for_the_next_minus_14(
    monitor, account, database, calls, clock
):
    """探测恢复没有换凭证，所以紧接着的 -14 不享受 60 秒宽限。"""
    monitor.session_expired(account.digest)
    clock.advance(600)
    monitor.probe_finished(good_poll())

    verdict = monitor.session_expired(account.digest)

    assert verdict is Verdict.EXPIRED
    assert login_row(database).episode == 2
    assert calls.expired == [1, 2]


# ------------------------------------------------------------------ 回调出错、并发


def test_a_failing_expiry_callback_does_not_undo_the_state(paths, database, store, clock, account):
    def boom(_episode: int) -> None:
        raise RuntimeError("告警线程出错")

    bad = LoginMonitor(paths, database, store, clock, on_expired=boom)

    assert bad.session_expired(account.digest) is Verdict.EXPIRED
    assert login_row(database).state == "expired"


def test_without_callbacks_nothing_is_logged_as_an_error(
    paths, database, store, clock, account, caplog
):
    quiet = LoginMonitor(paths, database, store, clock)

    with caplog.at_level("ERROR"):
        quiet.session_expired(account.digest)
        store.write(account_file(bot_token="tok-new"))
        quiet.sync()

    assert caplog.records == []


def test_a_failing_callback_is_logged_without_its_message(
    paths, database, store, clock, account, caplog
):
    def boom(_episode: int) -> None:
        raise RuntimeError("里面有 tok-secret-111")

    bad = LoginMonitor(paths, database, store, clock, on_expired=boom)

    with caplog.at_level("ERROR"):
        bad.session_expired(account.digest)

    assert [r.levelname for r in caplog.records] == ["ERROR"]
    assert "RuntimeError" in caplog.text
    assert TOKEN not in caplog.text


def test_concurrent_minus_14s_raise_one_alert(paths, database, store, clock, account, calls):
    monitors = [
        LoginMonitor(paths, database, store, clock, on_expired=calls.expired.append)
        for _ in range(8)
    ]
    start = threading.Barrier(len(monitors))

    def run(m: LoginMonitor) -> Verdict:
        start.wait(5)
        return m.session_expired(account.digest)

    verdicts = [w.result() for w in [run_in_thread(run, m) for m in monitors]]

    assert verdicts == [Verdict.EXPIRED] * 8
    assert calls.expired == [1]
    assert login_row(database).episode == 1
