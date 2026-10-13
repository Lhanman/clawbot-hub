"""登录状态：数据库里的登录状态行、重新绑定时的数据库事务、凭证文件和数据库的比对（`sync`）。

规格 `hub-login`「登录后服务自动换上新凭证」「凭证文件」「还没绑定也能启动」，
`hub-deploy`「启动自检和顺序」第 5 步。
"""

from __future__ import annotations

import json
import threading

import pytest

import clawbot_hub.loginstate as loginstate
from clawbot_hub.account import BROKEN_TEXT, AccountFile, AccountStore
from clawbot_hub.loginstate import (
    LOGIN_REASONS,
    STATES,
    LoginMonitor,
    LoginRow,
    Reason,
    SyncResult,
    read_login,
    reset_conversation,
)
from tests.login.conftest import (
    TOKEN,
    account_file,
    conversation_tokens,
    events,
    login_row,
    seed_conversation,
)
from tests.support.threads import run_in_thread


def bind(store, **overrides) -> AccountFile:
    account = account_file(**overrides)
    store.write(account)
    return account


def windows_open(db) -> int:
    return db.conn().execute("SELECT count(*) FROM windows WHERE voided = 0").fetchone()[0]


# ------------------------------------------------------------------ 状态名和原因


def test_the_four_states():
    assert STATES == ("ok", "expired", "missing", "broken")


def test_reasons_are_the_texts_of_the_send_table():
    assert LOGIN_REASONS == {
        "missing": Reason("login_missing", "还没绑定微信 ClawBot：运行 hubctl login"),
        "expired": Reason(
            "login_expired", "ClawBot 登录失效了：在电脑上运行 hubctl login 重新扫码"
        ),
        "broken": Reason("login_broken", "account.json 读不出来：重新运行 hubctl login"),
    }
    assert LOGIN_REASONS["broken"].text == BROKEN_TEXT


# ------------------------------------------------------------------ 读状态行


def test_a_fresh_database_says_not_bound(database):
    assert read_login(database.conn()) == LoginRow(
        state="missing", since_ms=None, episode=0, digest=None, changed_ms=None
    )


def test_read_login_returns_every_column(database):
    with database.txn() as conn:
        conn.execute(
            "UPDATE account_state SET login_state = 'expired', login_since_ms = 11,"
            " expired_episode = 3, creds_digest = 'abc', creds_changed_ms = 22 WHERE id = 1"
        )

    assert read_login(database.conn()) == LoginRow(
        state="expired", since_ms=11, episode=3, digest="abc", changed_ms=22
    )


# ------------------------------------------------------------------ 重新绑定的数据库事务


def test_reset_voids_open_windows_and_clears_their_tokens(database):
    seed_conversation(database)

    with database.txn() as conn:
        reset_conversation(conn)

    state = conversation_tokens(database)
    assert state["windows"] == [(1_000, None, 1), (1_001, None, 1), (500, None, 1)]


def test_reset_clears_inbox_tokens_and_nothing_else(database):
    seed_conversation(database)
    before = conversation_tokens(database)["inbox"]

    with database.txn() as conn:
        reset_conversation(conn)

    after = conversation_tokens(database)["inbox"]
    assert [row[4] for row in before] == ["inbox-token-0", "inbox-token-1"]
    assert [row[4] for row in after] == [None, None]
    assert [row[:4] + row[5:] for row in after] == [row[:4] + row[5:] for row in before]


def test_reset_keeps_the_window_rows_and_usage(database):
    seed_conversation(database)
    with database.txn() as conn:
        conn.execute("INSERT INTO usage (window_id, project_id, kind, n) VALUES (1, 1, 'push', 4)")

    with database.txn() as conn:
        reset_conversation(conn)

    assert database.conn().execute("SELECT count(*) FROM windows").fetchone()[0] == 3
    assert database.conn().execute("SELECT n FROM usage").fetchone()[0] == 4


def test_reset_twice_gives_the_same_result(database):
    seed_conversation(database)
    with database.txn() as conn:
        reset_conversation(conn)
    once = conversation_tokens(database)

    with database.txn() as conn:
        reset_conversation(conn)

    assert conversation_tokens(database) == once


def test_reset_on_an_empty_database_is_fine(database):
    with database.txn() as conn:
        reset_conversation(conn)

    assert conversation_tokens(database) == {"windows": [], "inbox": []}


# ------------------------------------------------------------------ 没绑定、第一次比对


def test_no_credentials_file_means_not_bound(monitor, database):
    result = monitor.sync()

    assert result == SyncResult(state="missing", changed=False, recovered=False, account=None)
    assert login_row(database).state == "missing"
    assert events(database) == []
    assert monitor.account() is None
    assert not monitor.wake.is_set()


def test_the_first_comparison_counts_as_a_change(monitor, store, database, clock):
    account = bind(store)

    result = monitor.sync()

    assert result == SyncResult(state="ok", changed=True, recovered=False, account=account)
    assert login_row(database) == LoginRow(
        state="ok",
        since_ms=clock.now_ms(),
        episode=0,
        digest=account.digest,
        changed_ms=clock.now_ms(),
    )
    assert monitor.account() == account
    assert monitor.wake.is_set()


def test_the_first_comparison_voids_a_leftover_conversation(monitor, store, database):
    seed_conversation(database)
    bind(store)

    monitor.sync()

    state = conversation_tokens(database)
    assert all(row[1] is None and row[2] == 1 for row in state["windows"])
    assert all(row[4] is None for row in state["inbox"])
    assert [row[1] for row in state["inbox"]] == ["正文0", "正文1"]  # 记录本身还在


def test_a_change_is_logged_without_any_credential(monitor, store, database):
    bind(store)

    monitor.sync()

    (event,) = events(database)
    assert (event["kind"], event["level"]) == ("login", "INFO")
    assert "凭证" in event["detail"]
    dumped = json.dumps(events(database), ensure_ascii=False)
    for secret in (TOKEN, "owner@im.wechat", "bot@im.bot", "ilink.example"):
        assert secret not in dumped


# ------------------------------------------------------------------ 没变就什么都不做


def test_the_same_credentials_change_nothing(monitor, store, database, clock):
    account = bind(store)
    monitor.sync()
    first = login_row(database)
    monitor.wake.clear()
    seed_conversation(database)  # 使用者之后又发了话
    clock.advance(3_600)

    result = monitor.sync()

    assert result == SyncResult(state="ok", changed=False, recovered=False, account=account)
    assert login_row(database) == first
    assert windows_open(database) == 2
    assert [row[1] for row in conversation_tokens(database)["windows"]][:2] == [
        "window-token-0",
        "window-token-1",
    ]
    assert not monitor.wake.is_set()
    assert len(events(database)) == 1


@pytest.mark.parametrize(
    "progress",
    [{"updates_buf": "新的进度"}, {"bound_at": "2027-01-01T00:00:00+08:00"}],
    ids=["收消息进度", "绑定时间"],
)
def test_progress_and_bound_time_are_not_a_change(monitor, store, database, progress):
    bind(store)
    monitor.sync()
    monitor.wake.clear()
    seed_conversation(database)
    bind(store, **progress)

    result = monitor.sync()

    assert not result.changed
    assert windows_open(database) == 2
    assert not monitor.wake.is_set()
    assert monitor.account().updates_buf == progress.get("updates_buf", "")


@pytest.mark.parametrize(
    "different",
    [
        {"bot_token": "tok-other"},
        {"base_url": "https://other.example"},
        {"bot_id": "other@im.bot"},
        {"user_id": "someone@im.wechat"},
    ],
    ids=["令牌", "接口地址", "机器人号", "用户号"],
)
def test_each_of_the_four_credentials_counts_as_a_change(
    monitor, store, database, clock, different
):
    bind(store)
    monitor.sync()
    monitor.wake.clear()
    seed_conversation(database)
    clock.advance(500)
    other = bind(store, **different)

    result = monitor.sync()

    assert result.changed and not result.recovered and result.state == "ok"
    assert result.account == other
    row = login_row(database)
    assert (row.digest, row.changed_ms, row.since_ms) == (
        other.digest,
        clock.now_ms(),
        clock.now_ms(),
    )
    assert windows_open(database) == 0
    assert all(r[4] is None for r in conversation_tokens(database)["inbox"])
    assert monitor.account() == other
    assert monitor.wake.is_set()


def test_a_change_made_while_the_service_was_stopped_applies_at_the_first_sync(
    paths, database, store, clock
):
    """服务停着时 `hubctl login` 写了新凭证：启动时的比对补做数据库事务。"""
    first = LoginMonitor(paths, database, store, clock)
    bind(store)
    first.sync()
    seed_conversation(database)
    bind(store, bot_token="tok-new")  # 服务停着，扫了码

    second = LoginMonitor(paths, database, store, clock)
    result = second.sync()

    assert result.changed
    assert windows_open(database) == 0


# ------------------------------------------------------------------ 失效和恢复


def mark_expired(db, *, episode: int = 1) -> None:
    with db.txn() as conn:
        conn.execute(
            "UPDATE account_state SET login_state = 'expired', login_since_ms = 5,"
            " expired_episode = ? WHERE id = 1",
            (episode,),
        )


def test_expired_stays_expired_while_the_credentials_are_the_same(monitor, store, database, calls):
    bind(store)
    monitor.sync()
    mark_expired(database)
    monitor.wake.clear()

    result = monitor.sync()

    assert result.state == "expired" and not result.changed and not result.recovered
    assert login_row(database).state == "expired"
    assert calls.restored == []
    assert not monitor.wake.is_set()


def test_new_credentials_clear_the_expired_state(monitor, store, database, calls, clock):
    bind(store)
    monitor.sync()
    mark_expired(database, episode=2)
    monitor.wake.clear()
    clock.advance(1_000)
    other = bind(store, bot_token="tok-new")

    result = monitor.sync()

    assert result == SyncResult(state="ok", changed=True, recovered=True, account=other)
    row = login_row(database)
    assert (row.state, row.since_ms, row.episode) == ("ok", clock.now_ms(), 2)
    assert calls.restored == [2]
    assert monitor.wake.is_set()
    details = [e["detail"] for e in events(database)]
    assert any("恢复" in d for d in details)


def test_a_change_that_is_not_a_recovery_does_not_call_restored(monitor, store, calls):
    bind(store)
    monitor.sync()
    bind(store, bot_token="tok-new")

    monitor.sync()

    assert calls.restored == []


def test_a_failing_callback_does_not_undo_the_state(paths, database, store, clock):
    def boom(_episode: int) -> None:
        raise RuntimeError("告警线程出错")

    bad = LoginMonitor(paths, database, store, clock, on_restored=boom)
    bind(store)
    bad.sync()
    mark_expired(database)
    bind(store, bot_token="tok-new")

    result = bad.sync()

    assert result.recovered
    assert login_row(database).state == "ok"


# ------------------------------------------------------------------ 文件读不出来、不见了


def test_a_broken_file_makes_the_state_broken(monitor, store, database, paths):
    bind(store)
    monitor.sync()
    seed_conversation(database)
    first = login_row(database)
    paths.account.write_text("{not json at all, 秘密内容 tok-secret-111")

    result = monitor.sync()

    assert result == SyncResult(state="broken", changed=False, recovered=False, account=None)
    row = login_row(database)
    assert row.state == "broken"
    assert row.digest == first.digest  # 摘要留着：同一份文件修好以后不算换了凭证
    assert monitor.account() is None
    assert windows_open(database) == 2
    broken = [e for e in events(database) if e["level"] == "WARNING"]
    assert len(broken) == 1 and broken[0]["kind"] == "login"
    assert "account.json" in broken[0]["detail"]
    dumped = json.dumps(events(database), ensure_ascii=False)
    assert "秘密内容" not in dumped and TOKEN not in dumped


def test_a_broken_file_is_reported_once(monitor, store, database, paths, clock):
    bind(store)
    monitor.sync()
    paths.account.write_text("[]")
    monitor.sync()
    since = login_row(database).since_ms
    clock.advance(30)

    monitor.sync()
    monitor.sync()

    assert login_row(database).since_ms == since
    assert len([e for e in events(database) if e["level"] == "WARNING"]) == 1


def test_a_broken_file_on_a_fresh_database(monitor, paths, database):
    paths.account.write_text("oops")

    result = monitor.sync()

    assert result.state == "broken"
    assert login_row(database).digest is None


def test_repairing_the_file_with_the_same_credentials_is_not_a_change(
    monitor, store, database, paths
):
    account = bind(store)
    monitor.sync()
    paths.account.write_text("oops")
    monitor.sync()
    monitor.wake.clear()
    seed_conversation(database)
    store.write(account)  # 修好了，凭证没变

    result = monitor.sync()

    assert result == SyncResult(state="ok", changed=False, recovered=False, account=account)
    assert login_row(database).state == "ok"
    assert windows_open(database) == 2
    assert monitor.wake.is_set()  # 收消息线程可以重新开始了
    last = events(database)[-1]
    assert (last["kind"], last["level"]) == ("login", "INFO")
    assert "account.json" in last["detail"]


def test_repairing_the_file_with_other_credentials_is_a_change(monitor, store, database, paths):
    bind(store)
    monitor.sync()
    paths.account.write_text("oops")
    monitor.sync()
    seed_conversation(database)
    bind(store, bot_token="tok-new")

    result = monitor.sync()

    assert result.changed
    assert windows_open(database) == 0


def test_a_file_that_disappears_makes_the_state_missing(monitor, store, database, paths):
    account = bind(store)
    monitor.sync()
    seed_conversation(database)
    paths.account.unlink()

    result = monitor.sync()

    assert result == SyncResult(state="missing", changed=False, recovered=False, account=None)
    row = login_row(database)
    assert (row.state, row.digest) == ("missing", account.digest)
    assert monitor.account() is None
    assert windows_open(database) == 2
    assert len([e for e in events(database) if e["level"] == "WARNING"]) == 1

    monitor.sync()
    assert len([e for e in events(database) if e["level"] == "WARNING"]) == 1


def test_the_same_file_coming_back_is_not_a_change(monitor, store, database, paths):
    account = bind(store)
    monitor.sync()
    paths.account.unlink()
    monitor.sync()
    seed_conversation(database)
    store.write(account)

    result = monitor.sync()

    assert result == SyncResult(state="ok", changed=False, recovered=False, account=account)
    assert windows_open(database) == 2


def test_another_account_after_the_file_was_gone_is_a_change(monitor, store, database, paths):
    bind(store)
    monitor.sync()
    paths.account.unlink()
    monitor.sync()
    seed_conversation(database)
    bind(store, user_id="someone@im.wechat")

    result = monitor.sync()

    assert result.changed and windows_open(database) == 0


# ------------------------------------------------------------------ 重启之后、重复、并发


def test_a_new_monitor_over_the_same_files_finds_nothing_to_do(
    paths, database, store, clock, monitor
):
    account = bind(store)
    monitor.sync()
    seed_conversation(database)
    again = LoginMonitor(paths, database, store, clock)

    result = again.sync()

    assert result == SyncResult(state="ok", changed=False, recovered=False, account=account)
    assert windows_open(database) == 2
    assert again.account() == account


def test_the_state_survives_reopening_the_database(paths, database, store, clock):
    from clawbot_hub.db import Database

    account = bind(store)
    LoginMonitor(paths, database, store, clock).sync()
    database.close()

    reopened = Database(paths.db)
    try:
        assert read_login(reopened.conn()).digest == account.digest
        assert LoginMonitor(paths, reopened, store, clock).sync().changed is False
    finally:
        reopened.close()


def test_replaying_the_transaction_changes_nothing(monitor, store, database):
    """补做的事务幂等：比对做完以后再做一遍（`hubctl login` 也做过一遍），结果一样。"""
    bind(store)
    seed_conversation(database)
    monitor.sync()
    once = conversation_tokens(database)

    with database.txn() as conn:
        reset_conversation(conn)

    assert conversation_tokens(database) == once


def test_the_digest_is_recorded_together_with_the_reset(monitor, store, database, monkeypatch):
    """事务里任何一步失败，摘要也不记：下一次比对会再做一遍，不会出现"摘要记了、窗口没作废"。"""
    seed_conversation(database)
    bind(store)

    def boom(*_args, **_kwargs):
        raise RuntimeError("写事件失败")

    monkeypatch.setattr(loginstate, "add_event", boom)
    with pytest.raises(RuntimeError):
        monitor.sync()

    assert login_row(database).digest is None
    assert windows_open(database) == 2
    monkeypatch.undo()

    result = monitor.sync()

    assert result.changed
    assert windows_open(database) == 0


def test_overlapping_syncs_never_leave_an_older_file_in_charge(paths, database, store, clock):
    """A 读到旧文件后卡住，这时文件换了，B 来比对：B 要等 A 做完，最后留下的是新文件。"""
    first = bind(store)
    reading = threading.Event()
    go_on = threading.Event()
    calls = []

    class SlowStore(AccountStore):
        def read(self):
            result = super().read()
            calls.append(result)
            if len(calls) == 1:
                reading.set()
                assert go_on.wait(5)
            return result

    monitor = LoginMonitor(paths, database, SlowStore(paths), clock)
    a = run_in_thread(monitor.sync)
    assert reading.wait(5)
    second = bind(store, bot_token="tok-new")  # 另一个终端 `hubctl login` 写了新的
    b = run_in_thread(monitor.sync)
    assert b.still_blocked(0.2)  # B 在等 A

    go_on.set()
    a.result()
    b.result()

    assert calls == [first, second]
    assert monitor.account() == second
    assert login_row(database).digest == second.digest


def test_concurrent_syncs_see_exactly_one_change(paths, database, store, clock, calls):
    bind(store)
    monitors = [
        LoginMonitor(paths, database, store, clock, on_restored=calls.restored.append)
        for _ in range(8)
    ]
    start = threading.Barrier(len(monitors))

    def run(m: LoginMonitor) -> bool:
        start.wait(5)
        return m.sync().changed

    workers = [run_in_thread(run, m) for m in monitors]
    results = [w.result() for w in workers]

    assert results.count(True) == 1
    assert len([e for e in events(database) if "凭证" in e["detail"]]) == 1
