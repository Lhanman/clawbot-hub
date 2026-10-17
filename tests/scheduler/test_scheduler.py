"""定时任务：每 30 秒一轮，一轮里是一组互不牵连的小检查——心跳（更新 `run.json`）、清理（每分钟）、
留存（每天，见 `test_retention.py`）、窗口快到期、窗口被拒的汇总、腾讯不通的判断（这三个的规则在
`tests/alerts/`，这里只确认每一轮都会查）、每天 `backup.at` 的自动备份。

每个检查各自出错不影响别的检查，也不让线程死掉。
"""

from __future__ import annotations

import os
import sqlite3
import stat
import time

import pytest

from clawbot_hub import alerts, scheduler
from clawbot_hub.backup import BackupError, make_backup
from clawbot_hub.bark import write_bark_key
from clawbot_hub.config import Config
from clawbot_hub.instance import read_run_info, service_running
from tests.alerts.conftest import fail_connection, go_to, round_trip
from tests.scheduler.conftest import SpyInbox
from tests.support.hub import tweak
from tests.support.threads import wait_for

MINUTE = 60
BACKUP_TIME = "2026-10-13 04:00"


def beat_of(s) -> int:
    return read_run_info(s.paths).last_heartbeat_ms


def backup_events(s) -> list[dict]:
    return s.events("backup")


# ---------------------------------------------------------------- 心跳


def test_every_round_updates_the_heartbeat(sched):
    sched.tick()
    assert beat_of(sched) == sched.clock.now_ms()

    sched.tick(30)
    assert beat_of(sched) == sched.clock.now_ms()


def test_the_service_counts_as_running_while_rounds_keep_coming(sched):
    for _ in range(10):
        sched.tick(30)
        assert service_running(sched.paths, sched.clock) is not None

    sched.clock.advance(61)  # 一轮都没有了

    assert service_running(sched.paths, sched.clock) is None


# ---------------------------------------------------------------- 清理：每分钟一次


def test_cleanup_runs_once_a_minute_not_every_round(make_scheduler):
    spy = SpyInbox()
    s = make_scheduler(inbox=spy)

    for _ in range(5):  # 0、30、60、90、120 秒
        s.scheduler.tick()
        s.clock.advance(30)

    assert spy.cleanups == 3  # 第 0、60、120 秒


def test_clearing_window_tokens_runs_once_a_minute_not_every_round(make_scheduler, monkeypatch):
    calls = []
    monkeypatch.setattr(
        scheduler.retention, "clear_window_tokens", lambda *args: calls.append(args) or 0
    )
    s = make_scheduler()

    for _ in range(5):  # 0、30、60、90、120 秒
        s.scheduler.tick()
        s.clock.advance(30)

    assert len(calls) == 3  # 第 0、60、120 秒


def test_a_round_a_little_early_does_not_skip_the_minute(make_scheduler):
    spy = SpyInbox()
    s = make_scheduler(inbox=spy)
    s.scheduler.tick()

    s.clock.advance(59.9)
    s.scheduler.tick()
    assert spy.cleanups == 1

    s.clock.advance(0.2)  # 跨过整分钟
    s.scheduler.tick()
    assert spy.cleanups == 2


def test_cleanup_really_cleans_the_queue(sched):
    """一句话在队列里放了 11 分钟没人取，一轮下来它是"过期"。"""
    project = sched.hub.ready()
    with sched.db.txn() as conn:
        sched.inbox.enqueue(
            conn,
            project,
            text="持仓",
            has_text=True,
            at_ms=sched.clock.now_ms(),
            received_ms=sched.clock.now_ms(),
            via="default",
            context_token="ctx-1",
        )
    sched.clock.advance(11 * MINUTE)

    sched.tick()

    states = [row["state"] for row in sched.db.conn().execute("SELECT state FROM inbox")]
    assert states == ["expired"]


# ---------------------------------------------------------------- 一轮里查的告警


def test_a_round_summarises_the_pushes_that_were_blocked(sched):
    """窗口已过期，项目 A 和项目 B 的推送都被拒绝；30 秒的定时任务跑一轮，汇总成一条。"""
    sched.hub.bind()
    sched.hub.speak()
    first = sched.hub.add("alpha", guaranteed=3, cap=4)
    second = sched.hub.add("beta", guaranteed=3, cap=4)
    sched.clock.advance(25 * 3600)
    sched.hub.sender.push(first, "你好")
    sched.hub.sender.push(second, "你好")

    sched.tick()

    assert sched.pending() == {alerts.WINDOW_BLOCKED: alerts.window_blocked_text(2)}


def test_a_round_reminds_about_a_window_that_is_about_to_close(sched):
    pusher = sched.hub.ready()
    sched.hub.sender.push(pusher, "窗口里有过推送")
    go_to(sched.clock, "2026-10-13 08:00")  # 周二早上，窗口 10:00 到期，剩 2 小时

    sched.tick()

    assert alerts.WINDOW_EXPIRING in sched.pending()


def test_a_round_notices_that_tencent_is_unreachable(sched):
    sched.hub.bind()
    round_trip(sched)

    for _ in range(20):  # 10 分钟里每 30 秒一轮，一直连不上
        sched.clock.advance(30)
        fail_connection(sched)
        sched.scheduler.tick()

    assert sched.pending() == {alerts.UPSTREAM_DOWN: alerts.upstream_down_text(10)}


# ---------------------------------------------------------------- 各查各的


@pytest.mark.parametrize(
    "broken",
    ["heartbeat", "cleanup", "tokens", "retention", "expiring", "blocked", "upstream", "backup"],
)
def test_one_step_failing_does_not_stop_the_others(make_scheduler, caplog, monkeypatch, broken):
    ran: set[str] = set()
    inbox = SpyInbox()

    def backup(paths, parent, name, **kwargs):
        ran.add("backup")
        if broken == "backup":
            raise RuntimeError("boom 不该出现在日志里")
        return make_backup(paths, parent, name, **kwargs)

    s = make_scheduler(Config(), inbox=inbox, make_backup=backup)
    go_to(s.clock, BACKUP_TIME)

    def step(name, action):
        def run(*args, **kwargs):
            ran.add(name)
            if broken == name:
                raise RuntimeError("boom 不该出现在日志里")
            return action(*args, **kwargs)

        return run

    s.recorder.beat = step("heartbeat", s.recorder.beat)
    inbox.cleanup = step("cleanup", inbox.cleanup)
    s.alerts.check_window_expiring = step("expiring", s.alerts.check_window_expiring)
    s.alerts.check_window_blocked = step("blocked", s.alerts.check_window_blocked)
    s.alerts.check_upstream = step("upstream", s.alerts.check_upstream)
    for name, function in (("tokens", "clear_window_tokens"), ("retention", "purge_old")):
        monkeypatch.setattr(
            scheduler.retention, function, step(name, getattr(scheduler.retention, function))
        )

    s.tick()  # 不抛出来

    assert ran == {
        "heartbeat",
        "cleanup",
        "tokens",
        "retention",
        "expiring",
        "blocked",
        "upstream",
        "backup",
    }
    if broken != "backup":
        assert "RuntimeError" in caplog.text
        assert "boom" not in caplog.text


# ---------------------------------------------------------------- 每日备份


def test_the_daily_backup_waits_for_the_configured_time(make_scheduler):
    s = make_scheduler(Config())  # 默认 04:00
    go_to(s.clock, "2026-10-13 03:59")
    s.tick()
    assert s.backups() == []

    go_to(s.clock, BACKUP_TIME)
    s.tick()

    assert s.backups() == ["auto-20261013-040000"]
    [event] = backup_events(s)
    assert event["detail"] == "每日备份完成：auto-20261013-040000"
    assert event["level"] == "INFO"


def test_the_backup_opens_and_carries_the_credentials_and_settings(make_scheduler):
    s = make_scheduler(Config())
    s.hub.ready()
    write_bark_key(s.paths, "A-DEVICE-KEY")
    s.paths.config.write_text("server:\n  port: 8791\n", encoding="utf-8")
    go_to(s.clock, BACKUP_TIME)

    s.tick()

    directory = s.paths.backups / "auto-20261013-040000"
    assert sorted(entry.name for entry in directory.iterdir()) == [
        "account.json",
        "bark.json",
        "config.yaml",
        "hub.db",
    ]
    assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
    for entry in directory.iterdir():
        assert stat.S_IMODE(os.stat(entry).st_mode) == 0o600
    copy = sqlite3.connect(directory / "hub.db")
    try:
        assert [row[0] for row in copy.execute("PRAGMA integrity_check")] == ["ok"]
        assert ("alpha",) in copy.execute("SELECT name FROM projects").fetchall()
    finally:
        copy.close()


def test_one_backup_a_day_however_many_rounds_or_restarts(make_scheduler):
    s = make_scheduler(Config())
    go_to(s.clock, BACKUP_TIME)
    s.tick()

    for stamp in ("2026-10-13 04:00", "2026-10-13 12:00", "2026-10-13 23:59"):
        go_to(s.clock, stamp)
        s.tick()
    s.restart()
    s.tick()

    assert s.backups() == ["auto-20261013-040000"]


def test_the_next_day_makes_another_one(make_scheduler):
    s = make_scheduler(Config())
    go_to(s.clock, BACKUP_TIME)
    s.tick()

    go_to(s.clock, "2026-10-14 04:00")
    s.tick()

    assert s.backups() == ["auto-20261013-040000", "auto-20261014-040000"]


def test_a_service_started_after_the_time_makes_todays_backup_straight_away(make_scheduler):
    s = make_scheduler(Config())
    go_to(s.clock, "2026-10-13 09:30")

    s.tick()

    assert s.backups() == ["auto-20261013-093000"]


def test_the_time_follows_the_display_time_zone(make_scheduler):
    s = make_scheduler(tweak(Config(), display={"timezone": "Asia/Tokyo"}))
    go_to(s.clock, "2026-10-13 02:59")  # 北京 02:59 = 东京 03:59
    s.tick()
    assert s.backups() == []

    go_to(s.clock, "2026-10-13 03:00")  # 东京 04:00
    s.tick()

    assert s.backups() == ["auto-20261013-040000"]


def test_the_configured_time_is_used(make_scheduler):
    s = make_scheduler(tweak(Config(), backup={"at": "13:30"}))
    go_to(s.clock, "2026-10-12 13:29")
    s.tick()
    assert s.backups() == []

    go_to(s.clock, "2026-10-12 13:30")
    s.tick()

    assert len(s.backups()) == 1


def test_only_the_newest_automatic_backups_are_kept_and_other_kinds_stay(make_scheduler):
    s = make_scheduler(Config())
    s.paths.backups.mkdir(mode=0o700)
    # 14 份，最新的是 10 月 12 日（今天 10 月 13 日这份还没有）
    autos = ["auto-20260929-040000", "auto-20260930-040000"]
    autos += [f"auto-202610{day:02d}-040000" for day in range(1, 13)]
    others = ["20261001-120000", "20261002-120000", "pre-upgrade-v1-v2-20261003-120000"]
    for name in autos + others:
        (s.paths.backups / name).mkdir()
    go_to(s.clock, BACKUP_TIME)

    s.tick()

    kept = [name for name in s.backups() if name.startswith("auto-")]
    assert kept == autos[1:] + ["auto-20261013-040000"]
    assert all(name in s.backups() for name in others)
    assert [e["detail"] for e in backup_events(s)] == [
        "每日备份完成：auto-20261013-040000",
        "清理了 1 份旧的自动备份（最多留 14 份）",
    ]


def test_the_number_kept_follows_the_configuration(make_scheduler):
    s = make_scheduler(tweak(Config(), backup={"keep": 2}))
    s.paths.backups.mkdir(mode=0o700)
    for name in ("auto-20261001-040000", "auto-20261002-040000"):
        (s.paths.backups / name).mkdir()
    go_to(s.clock, BACKUP_TIME)

    s.tick()

    assert s.backups() == ["auto-20261002-040000", "auto-20261013-040000"]
    assert backup_events(s)[-1]["detail"] == "清理了 1 份旧的自动备份（最多留 2 份）"


# ---------------------------------------------------------------- 备份失败：10 分钟后重试一次


class Flaky:
    """`make_backup` 的替身：按顺序给结果，异常就抛，`None` 就真的做一份。"""

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[int] = []
        self.kwargs: list[dict] = []

    def __call__(self, paths, parent, name, **kwargs):
        self.calls.append(0)
        self.kwargs.append(kwargs)
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, BaseException):
            raise outcome
        return make_backup(paths, parent, name, **kwargs)


def run_rounds(s, flaky: Flaky, *, minutes: int, every: int = 30) -> list[int]:
    """每 `every` 秒一轮，走 `minutes` 分钟；返回每次备份尝试发生在第几秒。"""
    started = s.clock.now_ms()
    attempts: list[int] = []
    seen = len(flaky.calls)
    for _ in range(minutes * MINUTE // every):
        s.tick(every)
        if len(flaky.calls) > seen:
            attempts += [(s.clock.now_ms() - started) // 1000] * (len(flaky.calls) - seen)
            seen = len(flaky.calls)
    return attempts


def test_a_failed_backup_is_tried_once_more_ten_minutes_later(make_scheduler):
    flaky = Flaky(BackupError("磁盘满了"), BackupError("磁盘满了"))
    s = make_scheduler(Config(), make_backup=flaky)
    go_to(s.clock, BACKUP_TIME)
    s.tick()  # 04:00:00 第一次

    attempts = run_rounds(s, flaky, minutes=30)

    assert len(flaky.calls) == 2
    assert attempts == [600]  # 04:10:00 重试一次，之后当天不再试


def test_the_retry_is_not_early(make_scheduler):
    flaky = Flaky(BackupError("磁盘满了"), None)
    s = make_scheduler(Config(), make_backup=flaky)
    go_to(s.clock, BACKUP_TIME)
    s.tick()

    s.tick(9 * MINUTE + 59.999)
    assert len(flaky.calls) == 1

    s.tick(0.001)  # 刚好满 10 分钟
    assert len(flaky.calls) == 2


def test_the_first_failure_leaves_an_event_and_no_alert(make_scheduler):
    flaky = Flaky(BackupError("磁盘满了"))
    s = make_scheduler(Config(), make_backup=flaky)
    go_to(s.clock, BACKUP_TIME)

    s.tick()

    [event] = backup_events(s)
    assert event["level"] == "WARNING"
    assert "磁盘满了" in event["detail"]
    assert s.pending() == {}


def test_a_retry_that_works_means_no_alert_and_two_events(make_scheduler):
    flaky = Flaky(BackupError("磁盘满了"), None)
    s = make_scheduler(Config(), make_backup=flaky)
    go_to(s.clock, BACKUP_TIME)
    s.tick()

    s.tick(10 * MINUTE)

    assert s.pending() == {}
    assert len(s.backups()) == 1
    [failed, done] = backup_events(s)
    assert "磁盘满了" in failed["detail"]
    assert done["detail"].startswith("每日备份完成：auto-20261013-04")


def test_a_retry_that_fails_too_makes_one_alert_with_the_reason(make_scheduler):
    flaky = Flaky(BackupError("磁盘满了"), BackupError("磁盘满了"))
    s = make_scheduler(Config(), make_backup=flaky)
    go_to(s.clock, BACKUP_TIME)
    s.tick()

    s.tick(10 * MINUTE)

    assert s.pending() == {alerts.BACKUP_FAILED: alerts.backup_failed_text("磁盘满了")}
    assert [e["level"] for e in backup_events(s)] == ["WARNING", "ERROR"]
    s.deliver()
    assert s.bark.titles == ["ClawBot 共用服务 · 备份失败"]


def test_no_more_attempts_and_no_second_alert_the_same_day(make_scheduler):
    flaky = Flaky(BackupError("磁盘满了"), BackupError("磁盘满了"), BackupError("磁盘满了"))
    s = make_scheduler(Config(), make_backup=flaky)
    go_to(s.clock, BACKUP_TIME)
    s.tick()
    s.tick(10 * MINUTE)
    s.deliver()
    s.bark.calls.clear()

    for stamp in ("2026-10-13 06:00", "2026-10-13 12:00", "2026-10-13 23:59"):
        go_to(s.clock, stamp)
        s.tick()
    s.deliver()

    assert len(flaky.calls) == 2
    assert s.bark.calls == []


def test_the_next_day_starts_afresh_after_a_failed_day(make_scheduler):
    flaky = Flaky(BackupError("磁盘满了"), BackupError("磁盘满了"), None)
    s = make_scheduler(Config(), make_backup=flaky)
    go_to(s.clock, BACKUP_TIME)
    s.tick()
    s.tick(10 * MINUTE)

    go_to(s.clock, "2026-10-14 04:00")
    s.tick()

    assert len(flaky.calls) == 3
    assert s.backups() == ["auto-20261014-040000"]


def test_a_real_failure_is_reported_with_its_reason(make_scheduler):
    s = make_scheduler(Config())
    s.paths.backups.write_text("这里本该是个目录", encoding="utf-8")
    go_to(s.clock, BACKUP_TIME)

    s.tick()
    s.tick(10 * MINUTE)

    assert alerts.BACKUP_FAILED in s.pending()
    assert "备份失败，原因：" in s.pending()[alerts.BACKUP_FAILED]


def test_an_unexpected_error_in_the_backup_is_a_failure_and_keeps_its_details_out(
    make_scheduler, caplog
):
    flaky = Flaky(RuntimeError("secret detail"), RuntimeError("secret detail"))
    s = make_scheduler(Config(), make_backup=flaky)
    go_to(s.clock, BACKUP_TIME)
    s.tick()

    s.tick(10 * MINUTE)

    text = s.pending()[alerts.BACKUP_FAILED]
    assert "RuntimeError" in text
    assert "secret detail" not in text
    assert all("secret detail" not in e["detail"] for e in s.events(kind=None))
    assert "每日备份出了意外的错误：RuntimeError" in caplog.text
    assert "secret detail" not in caplog.text


def test_yesterdays_backup_does_not_count_as_todays(make_scheduler):
    s = make_scheduler(Config())
    s.paths.backups.mkdir(mode=0o700)
    (s.paths.backups / "auto-20261012-040000").mkdir()
    go_to(s.clock, BACKUP_TIME)

    s.tick()

    assert s.backups() == ["auto-20261012-040000", "auto-20261013-040000"]


def test_a_manual_backup_made_today_does_not_replace_the_daily_one(make_scheduler):
    s = make_scheduler(Config())
    s.paths.backups.mkdir(mode=0o700)
    (s.paths.backups / "20261013-030000").mkdir()  # 手动备份没有 auto- 前缀
    go_to(s.clock, BACKUP_TIME)

    s.tick()

    assert "auto-20261013-040000" in s.backups()


def test_a_stray_file_named_like_todays_backup_is_not_a_backup(make_scheduler):
    s = make_scheduler(Config())
    s.paths.backups.mkdir(mode=0o700)
    (s.paths.backups / "auto-20261013-030000").write_text("不是目录", encoding="utf-8")
    go_to(s.clock, BACKUP_TIME)

    s.tick()

    assert "auto-20261013-040000" in s.backups()


def test_a_backup_that_worked_is_not_repeated_whatever_the_directory_looks_like(make_scheduler):
    """ "今天做成了"是定时任务自己记着的，不靠回头看目录：假的备份什么都没建，也只做一次。"""
    made = []
    s = make_scheduler(Config(), make_backup=lambda *args, **kwargs: made.append(args))
    go_to(s.clock, BACKUP_TIME)

    s.tick()
    s.tick(30)
    s.tick(MINUTE)

    assert len(made) == 1


def test_a_failed_day_does_not_turn_the_next_days_first_failure_into_a_retry(make_scheduler):
    flaky = Flaky(*[BackupError("磁盘满了")] * 4)
    s = make_scheduler(Config(), make_backup=flaky)
    go_to(s.clock, BACKUP_TIME)
    s.tick()
    s.tick(10 * MINUTE)  # 13 日：两次都失败，告警一条

    go_to(s.clock, "2026-10-14 04:00")
    s.tick()  # 14 日的第一次失败：只是警告，还有一次重试

    assert [e["level"] for e in backup_events(s)] == ["WARNING", "ERROR", "WARNING"]
    s.tick(10 * MINUTE)
    assert len(flaky.calls) == 4
    assert [e["level"] for e in backup_events(s)] == ["WARNING", "ERROR", "WARNING", "ERROR"]


def test_the_backup_is_made_from_the_services_own_connection_with_the_configured_wait(
    make_scheduler,
):
    flaky = Flaky(None)
    s = make_scheduler(tweak(Config(), db={"busy_timeout_ms": 1234}), make_backup=flaky)
    go_to(s.clock, BACKUP_TIME)

    s.tick()

    [kwargs] = flaky.kwargs
    assert kwargs["busy_timeout_ms"] == 1234
    assert kwargs["source"] is s.db.conn()


def test_failing_to_tidy_old_backups_does_not_undo_the_new_one(make_scheduler, monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError("boom 不该出现在日志里")

    monkeypatch.setattr(scheduler, "prune_auto_backups", broken)
    s = make_scheduler(Config())
    go_to(s.clock, BACKUP_TIME)

    s.tick()

    assert s.backups() == ["auto-20261013-040000"]
    assert [e["detail"] for e in backup_events(s)] == ["每日备份完成：auto-20261013-040000"]
    assert s.pending() == {}
    assert "清理旧的自动备份出错：RuntimeError" in caplog.text
    assert "boom" not in caplog.text


# ---------------------------------------------------------------- 线程


@pytest.fixture
def running(sched):
    sched.scheduler.start()
    yield sched
    sched.scheduler.stop()


def test_the_thread_runs_a_round_at_once_and_then_every_thirty_seconds(running):
    start = running.clock.now_ms()
    wait_for(lambda: read_run_info(running.paths).last_heartbeat_ms == start)
    running.clock.wait_until_blocked()

    running.clock.advance(29)
    time.sleep(0.05)  # 给线程一点真实时间：它不该醒
    assert beat_of(running) == start

    running.clock.advance(1)
    wait_for(lambda: beat_of(running) == start + 30_000)


def test_the_time_a_round_takes_is_taken_off_the_wait(sched):
    """第一轮花了 10 秒（假时间），下一轮仍在这一轮开始后的 30 秒，不是 40 秒，也不是 35 秒。"""
    original = sched.recorder.beat
    slow = []

    def beat():
        original()
        if not slow:
            slow.append(1)
            sched.clock.advance(10)

    sched.recorder.beat = beat
    start = sched.clock.now_ms()
    sched.scheduler.start()
    try:
        wait_for(lambda: slow)
        sched.clock.wait_until_blocked()

        assert sched.clock.pending_deadlines() == [start + 30_000]
    finally:
        sched.scheduler.stop()


def test_a_round_that_took_longer_than_the_period_is_followed_by_the_next_at_once(sched):
    beats = []
    original = sched.recorder.beat

    def beat():
        beats.append(1)
        original()
        if len(beats) == 1:
            sched.clock.advance(45)

    sched.recorder.beat = beat
    sched.scheduler.start()
    try:
        wait_for(lambda: len(beats) >= 2)  # 没有人推时间，第二轮自己就来了
    finally:
        sched.scheduler.stop()


def test_a_round_that_blows_up_does_not_kill_the_thread(sched):
    beats = []
    original = sched.recorder.beat

    def flaky_beat():
        beats.append(1)
        if len(beats) == 1:
            raise RuntimeError("boom")
        original()

    sched.recorder.beat = flaky_beat
    sched.scheduler.start()
    try:
        wait_for(lambda: len(beats) == 1)
        sched.clock.wait_until_blocked()
        sched.clock.advance(30)
        wait_for(lambda: len(beats) == 2 and beat_of(sched) == sched.clock.now_ms())
    finally:
        sched.scheduler.stop()


def test_stopping_ends_the_thread_and_is_harmless_to_repeat(sched):
    sched.scheduler.start()
    assert sched.scheduler.running

    sched.scheduler.stop()
    sched.scheduler.stop()

    assert not sched.scheduler.running


def test_stopping_a_scheduler_that_never_started_is_fine(sched):
    sched.scheduler.stop()

    assert not sched.scheduler.running


def test_the_thread_cannot_be_started_twice(running):
    with pytest.raises(RuntimeError):
        running.scheduler.start()


def test_after_stopping_no_more_rounds_run(sched):
    sched.scheduler.start()
    wait_for(lambda: beat_of(sched) == sched.clock.now_ms())
    sched.scheduler.stop()
    last = beat_of(sched)

    sched.clock.advance(120)
    time.sleep(0.05)

    assert beat_of(sched) == last
