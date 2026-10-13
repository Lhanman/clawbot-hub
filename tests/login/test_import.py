"""导入 ashare-quant 的 `clawbot.json`（`hubctl import-credentials` 的流程，函数层）。

规格 `hub-login`「导入已有的凭证」。进程级的"导入期间服务起不来（退出码 2）"留到 D6 的
`test_lifecycle`；这里测函数层：导入持有锁期间，启动用的"拿锁"函数返回被占用。
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime
from pathlib import Path

import pytest

from clawbot_hub.account import AccountStore
from clawbot_hub.backup import timestamp
from clawbot_hub.instance import InstanceBusy, acquire_instance_lock
from clawbot_hub.login import ImportReport, LoginError, import_credentials, mask_user
from clawbot_hub.loginstate import LoginMonitor, read_login
from clawbot_hub.upstream.base import PollFailed, SessionExpired, Updates
from tests.login.conftest import (
    account_file,
    conversation_tokens,
    dead_pid,
    events,
    seed_conversation,
)
from tests.support.fake_upstream import GET_UPDATES, FakeUpstream
from tests.support.threads import run_in_thread

HOUR_MS = 3_600_000


@pytest.fixture
def store(paths) -> AccountStore:
    return AccountStore(paths)


@pytest.fixture
def source(tmp_path, clock) -> Path:
    return write_source(tmp_path / "clawbot.json", clock)


def write_source(path: Path, clock, **overrides) -> Path:
    """一份 ashare-quant 的 `clawbot.json`（3 小时前使用者说过话，窗口里用掉 4 条）。"""
    data = {
        "bot_token": "tok-import-9",
        "base_url": "https://ilink.example",
        "bot_id": "bot123456@im.bot",
        "user_id": "owner@im.wechat",
        "bound_at": "2026-10-01T09:00:00+08:00",
        "updates_buf": "progress-xyz",
        "context_token": "ctx-import-1",
        "context_at_ms": clock.now_ms() - 3 * HOUR_MS,
        "sent": 4,
    }
    data.update(overrides)
    data = {key: value for key, value in data.items() if value is not ...}
    path.write_text(json.dumps(data, ensure_ascii=False))
    return path


@pytest.fixture
def run(paths, store, clock, display_tz):
    def go(path, **extra) -> ImportReport:
        return import_credentials(
            path, paths=paths, store=store, clock=clock, tz=display_tz, **extra
        )

    return go


def window_rows(db):
    return [dict(r) for r in db.conn().execute("SELECT * FROM windows ORDER BY id")]


def usage_rows(db):
    return [
        (r["name"], r["kind"], r["n"], r["started_ms"])
        for r in db.conn().execute(
            "SELECT p.name, u.kind, u.n, w.started_ms FROM usage u"
            " JOIN projects p ON p.id = u.project_id JOIN windows w ON w.id = u.window_id"
            " ORDER BY u.window_id, p.name, u.kind"
        )
    ]


def old_account(store, **overrides):
    store.write(account_file(**overrides))


# ------------------------------------------------------------------ 迁移时接得上


def test_the_window_and_the_used_count_carry_over(run, source, database, clock):
    run(source)

    started = clock.now_ms() - 3 * HOUR_MS
    (window,) = window_rows(database)
    assert (window["started_ms"], window["context_token"], window["voided"]) == (
        started,
        "ctx-import-1",
        0,
    )
    # 已用的 4 条记在内部项目 `_import` 名下，类别是推送
    assert usage_rows(database) == [("_import", "push", 4, started)]


def test_the_credentials_file_holds_the_imported_values(run, source, store, paths):
    run(source)

    saved = store.read()
    assert saved.bot_token == "tok-import-9"
    assert (saved.user_id, saved.bot_id) == ("owner@im.wechat", "bot123456@im.bot")
    assert saved.base_url == "https://ilink.example"
    assert saved.bound_at == "2026-10-01T09:00:00+08:00"
    assert saved.updates_buf == "progress-xyz"
    assert stat.S_IMODE(paths.account.stat().st_mode) == 0o600


def test_the_digest_is_recorded_and_the_login_is_ok(run, source, store, database, clock):
    run(source)

    row = read_login(database.conn())
    assert row.digest == store.read().digest
    assert (row.state, row.since_ms) == ("ok", clock.now_ms())
    assert row.changed_ms is None  # 导入不是"刚换了凭证"：真的失效了要马上报


def test_the_import_is_logged_without_credentials(run, source, database):
    run(source)

    (event,) = events(database)
    assert (event["kind"], event["level"]) == ("login", "INFO")
    assert "导入" in event["detail"]
    dumped = json.dumps(events(database), ensure_ascii=False)
    for secret in ("tok-import-9", "ctx-import-1", "progress-xyz", "owner@im.wechat"):
        assert secret not in dumped


def test_the_database_is_created_if_it_is_not_there_yet(run, source, paths):
    assert not paths.db.exists()

    run(source)

    assert paths.db.exists()


def test_a_source_without_a_window_imports_only_the_credentials(
    run, tmp_path, clock, store, database
):
    path = write_source(tmp_path / "no-window.json", clock, context_token=None, context_at_ms=None)

    report = run(path)

    assert store.read() is not None
    assert window_rows(database) == []
    assert usage_rows(database) == []
    assert report.window_left_ms is None and report.used is None


@pytest.mark.parametrize("missing", ["context_token", "context_at_ms"])
def test_half_a_window_is_no_window(run, tmp_path, clock, database, missing):
    path = write_source(tmp_path / "half.json", clock, **{missing: ...})

    run(path)

    assert window_rows(database) == []


def test_a_window_without_a_used_count_starts_at_zero(run, tmp_path, clock, database):
    path = write_source(tmp_path / "unused.json", clock, sent=...)

    report = run(path)

    assert [row[2] for row in usage_rows(database)] == [0]
    assert report.used == 0


def test_an_old_window_is_imported_too_and_reported_as_over(run, tmp_path, clock, database):
    path = write_source(tmp_path / "old.json", clock, context_at_ms=clock.now_ms() - 30 * HOUR_MS)

    report = run(path)

    (window,) = window_rows(database)
    assert window["started_ms"] == clock.now_ms() - 30 * HOUR_MS
    assert report.window_left_ms == -6 * HOUR_MS
    assert any("过期" in line for line in report.lines())


def test_a_missing_bound_time_becomes_the_import_time(run, tmp_path, clock, store, display_tz):
    path = write_source(tmp_path / "nobound.json", clock, bound_at=...)

    run(path)

    expected = datetime.fromtimestamp(clock.now_ms() / 1000, display_tz).isoformat(
        timespec="seconds"
    )
    assert store.read().bound_at == expected


def test_unknown_keys_in_the_source_are_ignored(run, tmp_path, clock, store):
    path = write_source(tmp_path / "extra.json", clock, something_new=[1, 2, 3])

    run(path)

    assert store.read().bot_token == "tok-import-9"


# ------------------------------------------------------------------ 输出只有摘要


def test_the_summary_has_no_credentials(run, source):
    report = run(source)

    text = "\n".join(report.lines())
    for secret in (
        "tok-import-9",
        "ctx-import-1",
        "progress-xyz",
        "owner@im.wechat",
        "bot123456@im.bot",
        "ilink.example",
    ):
        assert secret not in text
    assert "机器人：…" in text


def test_the_summary_says_how_long_the_window_has_left_and_how_much_is_used(run, source):
    report = run(source)

    assert report.window_left_ms == 21 * HOUR_MS
    assert report.used == 4
    text = "\n".join(report.lines())
    assert "21 小时 0 分钟" in text
    assert "4 条" in text
    assert "2026-10-01T09:00:00+08:00" in text


def test_the_user_id_is_masked(run, source):
    report = run(source)

    assert report.user_masked != "owner@im.wechat"
    assert report.user_masked.startswith("ow")
    assert "***" in report.user_masked
    assert "wechat" not in report.user_masked


def test_the_bot_id_shows_only_its_last_four_characters(run, source):
    report = run(source)

    assert report.bot_id_tail == ".bot"


@pytest.mark.parametrize(
    ("user_id", "masked"),
    [
        ("", "***"),
        ("a", "***"),
        ("abcde", "***"),
        ("abcdef", "ab***"),
        ("owner@im.wechat", "ow***"),
    ],
)
def test_masking_user_ids(user_id, masked):
    assert mask_user(user_id) == masked


def test_the_window_length_comes_from_the_caller(run, source):
    report = run(source, window_hours=48)

    assert report.window_left_ms == 45 * HOUR_MS


def test_the_hours_and_minutes_are_not_mixed_up(run, tmp_path, clock):
    path = write_source(tmp_path / "half.json", clock, context_at_ms=clock.now_ms() - 150 * 60_000)

    report = run(path)

    assert "还剩 21 小时 30 分钟" in "\n".join(report.lines())


def test_a_window_that_ends_right_now_is_over(run, tmp_path, clock):
    path = write_source(tmp_path / "edge.json", clock, context_at_ms=clock.now_ms() - 24 * HOUR_MS)

    report = run(path)

    assert report.window_left_ms == 0
    assert any("已经过期" in line for line in report.lines())


def test_no_window_is_said_in_the_summary(run, tmp_path, clock):
    path = write_source(tmp_path / "none.json", clock, context_token=None)

    report = run(path)

    assert any("对话窗口：没有" in line for line in report.lines())


def test_a_source_without_a_bot_id_says_so(run, tmp_path, clock):
    path = write_source(tmp_path / "nobot.json", clock, bot_id="")

    report = run(path)

    assert "机器人：（没有编号）" in report.lines()


def test_short_ids_are_masked_completely(run, tmp_path, clock):
    path = write_source(tmp_path / "short.json", clock, user_id="u", bot_id="b")

    report = run(path)

    assert report.user_masked == "***"
    assert report.bot_id_tail == "b"


# ------------------------------------------------------------------ 拒绝的情形


def test_an_existing_credentials_file_is_not_overwritten_without_replace(
    run, source, store, paths, database
):
    old_account(store, bot_token="tok-old")
    original = paths.account.read_bytes()

    with pytest.raises(LoginError, match="--replace"):
        run(source)

    assert paths.account.read_bytes() == original
    assert window_rows(database) == []
    assert not list(paths.home.glob("account.json.bak-*"))


def test_a_broken_existing_file_also_needs_replace(run, source, paths):
    paths.account.write_text("oops")

    with pytest.raises(LoginError, match="--replace"):
        run(source)

    assert paths.account.read_text() == "oops"


def test_a_running_service_blocks_the_import(run, source, paths, clock):
    paths.run.write_text(
        json.dumps({"pid": os.getpid(), "last_heartbeat_ms": clock.now_ms(), "clean_exit": False})
    )

    with pytest.raises(LoginError, match="先停服务"):
        run(source)

    assert not paths.account.exists()
    assert not paths.db.exists()
    acquire_instance_lock(paths).release()  # 没有留下占着的锁


@pytest.mark.parametrize(
    "make_state",
    [
        lambda clock: {"pid": dead_pid(), "last_heartbeat_ms": clock.now_ms()},
        lambda clock: {"pid": os.getpid(), "last_heartbeat_ms": clock.now_ms() - 3_600_000},
        lambda clock: {
            "pid": os.getpid(),
            "last_heartbeat_ms": clock.now_ms(),
            "clean_exit": True,
        },
    ],
    ids=["进程不在", "心跳停了", "正常退出"],
)
def test_a_service_that_is_gone_does_not_block_the_import(run, source, paths, clock, make_state):
    paths.run.write_text(json.dumps(make_state(clock)))

    run(source)

    assert paths.account.exists()


def test_a_lock_held_by_somebody_else_blocks_the_import(run, source, paths, database):
    held = acquire_instance_lock(paths)
    try:
        with pytest.raises(LoginError, match="服务刚好在启动"):
            run(source)
    finally:
        held.release()

    assert not paths.account.exists()
    assert window_rows(database) == []


def test_the_lock_is_free_again_after_a_success_and_after_a_failure(run, source, tmp_path, paths):
    run(source)
    acquire_instance_lock(paths).release()

    with pytest.raises(LoginError):
        run(source)  # 已经有凭证，拒绝
    acquire_instance_lock(paths).release()


def test_the_lock_is_held_for_the_whole_import(run, source, paths, clock):
    """导入没做完服务起不来：探测期间（导入的最后一步）锁仍然在导入手里。"""
    upstream = FakeUpstream(clock=clock)
    gate = upstream.hold(GET_UPDATES)
    worker = run_in_thread(run, source, probe=upstream)
    assert gate.entered.wait(5)

    with pytest.raises(InstanceBusy):
        acquire_instance_lock(paths)

    gate.release()
    worker.result()
    acquire_instance_lock(paths).release()


# ------------------------------------------------------------------ 校验导入文件


@pytest.mark.parametrize("missing", [["bot_token"], ["user_id"], ["bot_token", "user_id"]])
def test_missing_required_fields_are_named(run, tmp_path, clock, paths, missing):
    path = write_source(
        tmp_path / "bad.json", clock, context_token="ctx-SECRET", **dict.fromkeys(missing, ...)
    )

    with pytest.raises(LoginError) as raised:
        run(path)

    message = str(raised.value)
    assert all(name in message for name in missing)
    assert "ctx-SECRET" not in message and "progress-xyz" not in message
    assert not paths.account.exists()
    assert not paths.db.exists()


@pytest.mark.parametrize("empty", ["bot_token", "user_id"])
def test_empty_required_fields_count_as_missing(run, tmp_path, clock, paths, empty):
    path = write_source(tmp_path / "empty.json", clock, **{empty: ""})

    with pytest.raises(LoginError, match=empty):
        run(path)

    assert not paths.account.exists()


@pytest.mark.parametrize(
    "content",
    ["", "not json {tok-SECRET", "[]", '"text"', "123", "null"],
)
def test_a_source_that_is_not_an_object_is_refused_without_quoting_it(
    run, tmp_path, paths, content
):
    path = tmp_path / "junk.json"
    path.write_text(content)

    with pytest.raises(LoginError) as raised:
        run(path)

    assert "tok-SECRET" not in str(raised.value)
    assert "读不出来" in str(raised.value)
    assert not paths.account.exists()


def test_a_source_that_does_not_exist(run, tmp_path):
    with pytest.raises(LoginError, match="读不出来"):
        run(tmp_path / "nope.json")


def test_a_directory_is_not_a_source(run, tmp_path):
    with pytest.raises(LoginError, match="读不出来"):
        run(tmp_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sent", "4"),
        ("sent", -1),
        ("sent", True),
        ("sent", 1.5),
        ("context_at_ms", "x"),
        ("context_at_ms", 0),
        ("context_at_ms", -5),
        ("context_at_ms", True),
        ("context_token", 5),
        ("bot_id", 7),
        ("base_url", ["x"]),
        ("updates_buf", 1),
        ("bound_at", 20261001),
    ],
)
def test_fields_of_the_wrong_kind_are_named(run, tmp_path, clock, paths, field, value):
    path = write_source(tmp_path / "wrong.json", clock, **{field: value})

    with pytest.raises(LoginError, match=field):
        run(path)

    assert not paths.account.exists()
    assert not paths.db.exists()


def test_null_optional_fields_are_fine(run, tmp_path, clock, store):
    path = write_source(
        tmp_path / "nulls.json",
        clock,
        base_url=None,
        bot_id=None,
        bound_at=None,
        updates_buf=None,
        context_token=None,
        context_at_ms=None,
        sent=None,
    )

    run(path)

    saved = store.read()
    assert (saved.base_url, saved.bot_id, saved.updates_buf) == ("", "", "")


# ------------------------------------------------------------------ --replace


def test_replace_backs_up_the_old_file_and_voids_the_old_conversation(
    run, source, store, paths, database, clock, display_tz
):
    old_account(store, bot_token="tok-old")
    seed_conversation(database)
    original = paths.account.read_bytes()

    report = run(source, replace=True)

    backups = sorted(paths.home.glob("account.json.bak-*"))
    assert len(backups) == 1
    stamp = backups[0].name.removeprefix("account.json.bak-")
    assert stamp == timestamp(clock.now_ms(), display_tz)
    assert backups[0].read_bytes() == original
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert report.backup == backups[0]
    assert f"旧的 account.json 已备份为 {backups[0].name}" in report.lines()
    assert store.read().bot_token == "tok-import-9"
    state = conversation_tokens(database)
    seeded = [row for row in state["windows"] if row[1] != "ctx-import-1"]
    assert all(row[1] is None and row[2] == 1 for row in seeded)
    assert all(row[4] is None for row in state["inbox"])
    assert [row[1] for row in state["windows"] if row[2] == 0] == ["ctx-import-1"]


def test_a_broken_old_file_is_backed_up_too(run, source, paths):
    paths.account.write_text("oops, 这不是 JSON")

    run(source, replace=True)

    (backup,) = paths.home.glob("account.json.bak-*")
    assert backup.read_text() == "oops, 这不是 JSON"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_backups_in_the_same_second_get_a_numbered_suffix_from_2(
    run, source, store, paths, clock, display_tz
):
    stamp = timestamp(clock.now_ms(), display_tz)
    old_account(store, bot_token="tok-old")
    for _ in range(3):
        run(source, replace=True)

    names = sorted(p.name for p in paths.home.glob("account.json.bak-*"))
    assert names == [
        f"account.json.bak-{stamp}",
        f"account.json.bak-{stamp}-2",
        f"account.json.bak-{stamp}-3",
    ]


def test_replace_without_an_old_file_just_imports(run, source, paths):
    report = run(source, replace=True)

    assert paths.account.exists()
    assert report.backup is None
    assert not list(paths.home.glob("account.json.bak-*"))


def test_another_account_without_a_file_still_voids_what_was_left(
    run, source, store, database, paths, clock
):
    """库里还留着另一个账号的窗口（`account.json` 被删过）：导入的是另一个账号，旧的不能留。"""
    old_account(store, bot_token="tok-old")
    LoginMonitor(paths, database, store, clock).sync()
    seed_conversation(database)
    paths.account.unlink()

    run(source)

    old = [row for row in conversation_tokens(database)["windows"] if row[1] != "ctx-import-1"]
    assert old and all(row[1] is None and row[2] == 1 for row in old)


# ------------------------------------------------------------------ 先数据库、后文件，可重复


def test_importing_the_same_file_again_gives_the_same_result(run, source, database):
    run(source)
    first = (window_rows(database), usage_rows(database))

    run(source, replace=True)

    assert (window_rows(database), usage_rows(database)) == first


def test_the_used_count_is_set_not_added(run, source, tmp_path, clock, database):
    run(source)
    again = write_source(tmp_path / "again.json", clock, sent=6)

    run(again, replace=True)

    (window,) = window_rows(database)
    assert usage_rows(database) == [("_import", "push", 6, window["started_ms"])]


def test_killed_between_the_database_and_the_file(run, source, store, paths, database, monkeypatch):
    class Killed(BaseException):
        pass

    def killed(_account):
        raise Killed

    monkeypatch.setattr(store, "write", killed)
    with pytest.raises(Killed):
        run(source)

    # 数据库已经提交，文件还没写
    assert not paths.account.exists()
    assert len(window_rows(database)) == 1
    acquire_instance_lock(paths).release()  # 锁也放了
    monkeypatch.undo()

    run(source)

    (window,) = window_rows(database)
    assert window["voided"] == 0
    assert usage_rows(database) == [("_import", "push", 4, window["started_ms"])]
    assert store.read().bot_token == "tok-import-9"


def test_killed_before_the_file_with_replace_then_run_again(
    run, source, store, paths, database, monkeypatch
):
    old_account(store, bot_token="tok-old")
    seed_conversation(database)

    class Killed(BaseException):
        pass

    real_write = store.write

    def killed(_account):
        raise Killed

    monkeypatch.setattr(store, "write", killed)
    with pytest.raises(Killed):
        run(source, replace=True)
    monkeypatch.setattr(store, "write", real_write)

    run(source, replace=True)

    imported = [row for row in conversation_tokens(database)["windows"] if row[1] == "ctx-import-1"]
    assert len(imported) == 1 and imported[0][2] == 0
    assert [u[2] for u in usage_rows(database)] == [4]
    assert store.read().bot_token == "tok-import-9"


# ------------------------------------------------------------------ 导入以后启动


def test_starting_after_the_import_does_not_void_the_window(
    run, source, store, paths, database, clock
):
    run(source)

    result = LoginMonitor(paths, database, store, clock).sync()

    assert not result.changed
    (window,) = window_rows(database)
    assert (window["voided"], window["context_token"]) == (0, "ctx-import-1")
    assert usage_rows(database)[0][2] == 4


def test_importing_over_an_expired_login_clears_it(run, source, store, paths, database, clock):
    old_account(store, bot_token="tok-old")
    LoginMonitor(paths, database, store, clock).sync()
    with database.txn() as conn:
        conn.execute(
            "UPDATE account_state SET login_state = 'expired', expired_episode = 2 WHERE id = 1"
        )

    run(source, replace=True)

    row = read_login(database.conn())
    assert (row.state, row.episode) == ("ok", 2)


# ------------------------------------------------------------------ --probe


@pytest.fixture
def upstream(clock) -> FakeUpstream:
    return FakeUpstream(clock=clock)


def test_a_probe_reports_valid_credentials_without_eating_messages(
    run, source, upstream, store, database
):
    upstream.push_user_message("你好，这条不能被吃掉")

    report = run(source, probe=upstream, probe_timeout=12.5)

    assert report.probe.status == "valid"
    assert any("凭证有效" in line for line in report.lines())
    (call,) = upstream.calls_to(GET_UPDATES)
    assert call.args["acct"].bot_token == "tok-import-9"
    assert call.args["buf"] == "progress-xyz"
    assert call.args["timeout"] == 12.5
    assert store.read().updates_buf == "progress-xyz"  # 腾讯回的新进度没有保存
    assert database.conn().execute("SELECT count(*) FROM inbox").fetchone()[0] == 0
    assert database.conn().execute("SELECT count(*) FROM inbound_seen").fetchone()[0] == 0
    assert len(window_rows(database)) == 1  # 也没有为这条消息新开窗口


def test_without_probe_the_network_is_not_touched(run, source, upstream):
    run(source)

    assert upstream.calls == []


def test_a_probe_that_gets_minus_14_says_the_credentials_are_dead(run, source, upstream, store):
    upstream.script(GET_UPDATES, SessionExpired())

    report = run(source, probe=upstream)

    assert report.probe.status == "expired"
    assert store.read() is not None  # 导入照做，只是告诉你
    assert any("-14" in line or "失效" in line for line in report.lines())


@pytest.mark.parametrize(
    "outcome",
    [PollFailed("HTTP 502", completed=False), PollFailed("500 server error", completed=True)],
    ids=["502", "ret 500"],
)
def test_a_probe_that_fails_for_another_reason_is_inconclusive(run, source, upstream, outcome):
    upstream.script(GET_UPDATES, outcome)

    report = run(source, probe=upstream)

    assert report.probe.status == "unknown"
    assert outcome.reason in report.probe.reason
    assert any("没能确认" in line and outcome.reason in line for line in report.lines())


def test_a_probe_that_times_out_is_inconclusive(run, source, upstream):
    upstream.script(GET_UPDATES, Updates(timed_out=True))

    report = run(source, probe=upstream)

    assert report.probe.status == "unknown"


def test_the_probe_line_has_no_credentials(run, source, upstream):
    upstream.push_user_message("secret-body-text")

    report = run(source, probe=upstream)

    text = "\n".join(report.lines())
    assert "secret-body-text" not in text
    assert "buf-" not in text
    assert "tok-import-9" not in text


def test_no_probe_means_no_probe_result(run, source):
    assert run(source).probe is None
