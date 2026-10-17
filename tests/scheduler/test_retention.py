"""数据留存（规格 `hub-deploy`「数据库和升级」）：发送记录、事件、窗口记录、用量记录一律保留
`events.keep_days`（默认 90）天；删窗口时发送记录里指向它的窗口编号置空；去重键从发送记录建立起占用
`send.dedupe_hours`（默认 24）小时，到点把去重摘要置空；过期窗口的对话凭证清掉。

两个节奏（都由定时任务的一轮带出）：

- **每天一次**（按显示时区的日期，服务启动后的第一轮也做一次）：删过了保留期的记录、释放到期的
  去重键、清掉窗口已经删了的"已告警窗口"标记；
- **每分钟一次**（和待取队列的清理在一起）：窗口过期了，它的对话凭证就清掉。

这个文件里的场景都是对着一份造好的数据库跑 `tick()`，看库里剩下什么。
"""

from __future__ import annotations

import itertools

import pytest

from clawbot_hub.config import Config
from clawbot_hub.events import add_event
from clawbot_hub.quota import NONE_WINDOW_KEY
from clawbot_hub.retention import Purged, clear_window_tokens
from tests.alerts.conftest import go_to
from tests.support.hub import tweak

HOUR = 3_600_000
DAY = 24 * HOUR


def make(make_scheduler, **sections):
    """备份定在 23:59，免得一进来就做一份备份、多出几条事件。"""
    return make_scheduler(tweak(Config(), backup={"at": "23:59"}, **sections))


_serial = itertools.count(1)


def add_window(s, age_ms: int, *, token: str | None = "ctx-x", voided: int = 0) -> int:
    started = s.clock.now_ms() - age_ms
    return s.execute(
        "INSERT INTO windows (started_ms, context_token, voided) VALUES (?, ?, ?)",
        started,
        token,
        voided,
    ).lastrowid


def add_outbox(
    s,
    project,
    age_ms: int,
    *,
    window: int | None = None,
    dedupe: str | None = None,
    status: str = "sent",
) -> str:
    outbox_id = f"o-{next(_serial):04d}"
    s.execute(
        "INSERT INTO outbox (id, project_id, kind, window_id, dedupe_hash, parts, status,"
        " created_ms) VALUES (?, ?, 'push', ?, ?, 1, ?, ?)",
        outbox_id,
        project.id,
        window,
        dedupe,
        status,
        s.clock.now_ms() - age_ms,
    )
    return outbox_id


def outbox_ids(s) -> list[str]:
    return [row["id"] for row in s.db.conn().execute("SELECT id FROM outbox ORDER BY id")]


def window_ids(s) -> list[int]:
    return [row["id"] for row in s.db.conn().execute("SELECT id FROM windows ORDER BY id")]


def window_tokens(s) -> list[str | None]:
    rows = s.db.conn().execute("SELECT context_token FROM windows ORDER BY id")
    return [row["context_token"] for row in rows]


def usage_windows(s) -> set[int]:
    return {row["window_id"] for row in s.db.conn().execute("SELECT window_id FROM usage")}


def lifecycle(s) -> list[str]:
    return [event["detail"] for event in s.events("lifecycle")]


def db_bytes(s) -> bytes:
    s.db.conn().execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    return b"".join(path.read_bytes() for path in sorted(s.paths.home.glob("hub.db*")))


# ---------------------------------------------------------------- 过了保留期的记录


def test_old_records_go_and_a_window_losing_its_row_leaves_the_outbox_pointing_nowhere(
    make_scheduler,
):
    """规格场景：91 天前的发送记录和窗口没了；89 天前的发送记录还在，它的窗口编号变成空。"""
    s = make(make_scheduler)
    alpha = s.hub.add("alpha", guaranteed=3, cap=4)
    old_window = add_window(s, 91 * DAY)
    recent_window = add_window(s, 89 * DAY)
    gone = add_outbox(s, alpha, 91 * DAY)
    kept = add_outbox(s, alpha, 89 * DAY, window=old_window)
    kept_too = add_outbox(s, alpha, 89 * DAY, window=recent_window)

    s.tick()

    assert outbox_ids(s) == sorted([kept, kept_too])
    assert s.hub.row(kept)["window_id"] is None
    assert s.hub.row(kept_too)["window_id"] == recent_window
    assert window_ids(s) == [recent_window]
    assert gone not in outbox_ids(s)


def test_usage_goes_with_its_window(make_scheduler):
    s = make(make_scheduler)
    alpha = s.hub.add("alpha", guaranteed=3, cap=4)
    old_window = add_window(s, 91 * DAY)
    recent_window = add_window(s, 10 * DAY)
    for window in (old_window, recent_window):
        s.execute(
            "INSERT INTO usage (window_id, project_id, kind, n) VALUES (?, ?, 'push', 2)",
            window,
            alpha.id,
        )

    s.tick()

    assert usage_windows(s) == {recent_window}


def test_the_keep_period_is_exact(make_scheduler):
    """刚好 `keep_days` 天的还在，多一毫秒就没了。"""
    s = make(make_scheduler, events={"keep_days": 30})
    alpha = s.hub.add("alpha", guaranteed=3, cap=4)
    exactly = add_outbox(s, alpha, 30 * DAY)
    just_over = add_outbox(s, alpha, 30 * DAY + 1)
    window_exactly = add_window(s, 30 * DAY)
    window_over = add_window(s, 30 * DAY + 1)
    for window in (window_exactly, window_over):
        s.execute(
            "INSERT INTO usage (window_id, project_id, kind, n) VALUES (?, ?, 'push', 1)",
            window,
            alpha.id,
        )
    with s.db.txn() as conn:
        now = s.clock.now_ms()
        add_event(conn, now - 30 * DAY, "lifecycle", "刚好满期的事件")
        add_event(conn, now - 30 * DAY - 1, "lifecycle", "多了一毫秒的事件")

    s.tick()

    assert outbox_ids(s) == [exactly]
    assert window_ids(s) == [window_exactly]
    assert usage_windows(s) == {window_exactly}  # 窗口还在，它的用量也在
    assert just_over not in outbox_ids(s)
    details = [event["detail"] for event in s.events(kind=None)]
    assert "刚好满期的事件" in details
    assert "多了一毫秒的事件" not in details


def test_the_default_is_ninety_days(make_scheduler):
    s = make(make_scheduler)
    alpha = s.hub.add("alpha", guaranteed=3, cap=4)
    kept = add_outbox(s, alpha, 90 * DAY)
    add_outbox(s, alpha, 90 * DAY + 1)

    s.tick()

    assert outbox_ids(s) == [kept]


def test_events_follow_the_same_period(make_scheduler):
    s = make(make_scheduler)
    with s.db.txn() as conn:
        now = s.clock.now_ms()
        add_event(conn, now - 91 * DAY, "lifecycle", "九十一天前的事件")
        add_event(conn, now - 89 * DAY, "lifecycle", "八十九天前的事件")
        add_event(conn, now - 91 * DAY, "login", "很久以前的登录事件")

    s.tick()

    details = [event["detail"] for event in s.events(kind=None)]
    assert "八十九天前的事件" in details
    assert "九十一天前的事件" not in details
    assert "很久以前的登录事件" not in details


def test_recent_records_of_every_kind_stay(make_scheduler):
    s = make(make_scheduler)
    pusher = s.hub.ready()
    s.hub.sender.push(pusher, "窗口里有过一条推送")
    window = s.hub.quota.current_window(s.db.conn())
    sent = [row["id"] for row in s.hub.rows()]
    events = [event["id"] for event in s.events(kind=None)]

    s.tick()

    assert [row["id"] for row in s.hub.rows()] == sent
    assert s.hub.quota.current_window(s.db.conn()).id == window.id
    assert s.hub.used() == {("alpha", "push"): 1}
    assert [event["id"] for event in s.events(kind=None)][: len(events)] == events


def test_other_alert_state_is_left_alone(make_scheduler):
    s = make(make_scheduler)
    for key in ("login_expired", "send_unconfirmed", "window_blocked", "backup_failed"):
        s.execute("INSERT INTO alert_state (key, episode) VALUES (?, '1')", key)

    s.tick()

    assert set(s.states()) == {
        "login_expired",
        "send_unconfirmed",
        "window_blocked",
        "backup_failed",
    }


# ---------------------------------------------------------------- 已告警窗口的标记跟着窗口走


def test_alerted_window_markers_go_with_their_windows(make_scheduler):
    s = make(make_scheduler)
    old_window = add_window(s, 91 * DAY)
    live_window = add_window(s, 1 * DAY)
    keys = [
        f"window_blocked:{old_window}",
        f"window_blocked:{live_window}",
        NONE_WINDOW_KEY,
        "window_blocked",
        "login_expired",
    ]
    for key in keys:
        s.execute("INSERT INTO alert_state (key, episode) VALUES (?, '1')", key)

    s.tick()

    assert set(s.states()) == {
        f"window_blocked:{live_window}",
        NONE_WINDOW_KEY,
        "window_blocked",
        "login_expired",
    }


def test_a_marker_for_a_window_that_no_longer_exists_goes_too(make_scheduler):
    s = make(make_scheduler)
    s.execute("INSERT INTO alert_state (key, episode) VALUES ('window_blocked:9999', '1')")

    s.tick()

    assert s.state("window_blocked:9999") is None


# ---------------------------------------------------------------- 去重键到期释放


def test_dedupe_digests_are_released_after_the_configured_hours(make_scheduler):
    s = make(make_scheduler)
    alpha = s.hub.add("alpha", guaranteed=3, cap=4)
    stale = add_outbox(s, alpha, 25 * HOUR, dedupe="digest-stale")
    edge = add_outbox(s, alpha, 24 * HOUR, dedupe="digest-edge")
    fresh = add_outbox(s, alpha, 23 * HOUR, dedupe="digest-fresh")

    s.tick()

    assert s.hub.row(stale)["dedupe_hash"] is None  # 记录本身还在
    assert s.hub.row(edge)["dedupe_hash"] is None  # 刚好 24 小时：到点了
    assert s.hub.row(fresh)["dedupe_hash"] == "digest-fresh"


def test_the_dedupe_hours_setting_is_used(make_scheduler):
    s = make(make_scheduler, send={"dedupe_hours": 2})
    alpha = s.hub.add("alpha", guaranteed=3, cap=4)
    old = add_outbox(s, alpha, 3 * HOUR, dedupe="digest-old")
    new = add_outbox(s, alpha, 1 * HOUR, dedupe="digest-new")

    s.tick()

    assert s.hub.row(old)["dedupe_hash"] is None
    assert s.hub.row(new)["dedupe_hash"] == "digest-new"


def test_a_released_key_can_be_used_again(make_scheduler):
    """规格场景：25 小时前建立的去重键，项目用同一个键再发，当作新的发送。"""
    s = make(make_scheduler)
    pusher = s.hub.ready()
    first = s.hub.sender.push(pusher, "同一条", dedupe_key="k1")
    assert s.hub.row(first.id)["dedupe_hash"] is not None
    s.clock.advance(25 * 3600)
    s.hub.speak()

    s.tick()
    assert s.hub.row(first.id)["dedupe_hash"] is None  # 发送记录还在，去重摘要清空了
    second = s.hub.sender.push(pusher, "同一条", dedupe_key="k1")

    assert not first.duplicate
    assert not second.duplicate
    assert second.id != first.id


# ---------------------------------------------------------------- 过期窗口的对话凭证


@pytest.mark.parametrize(
    ("age_ms", "cleared"),
    [
        (23 * HOUR + 59 * 60_000 + 59_000, False),
        (24 * HOUR - 1, False),
        (24 * HOUR, True),
        (25 * HOUR, True),
        (40 * DAY, True),
    ],
)
def test_a_window_loses_its_token_when_it_expires(make_scheduler, age_ms, cleared):
    s = make(make_scheduler)
    add_window(s, age_ms, token="ctx-old")

    s.tick()

    assert window_tokens(s) == [None if cleared else "ctx-old"]


def test_the_expiry_follows_the_window_hours_setting(make_scheduler):
    s = make(make_scheduler, quota={"window_hours": 12})
    add_window(s, 13 * HOUR, token="ctx-old")
    add_window(s, 11 * HOUR, token="ctx-new")

    s.tick()

    assert window_tokens(s) == [None, "ctx-new"]


def test_a_window_without_a_token_or_already_voided_is_fine(make_scheduler):
    s = make(make_scheduler)
    add_window(s, 30 * HOUR, token=None)
    add_window(s, 30 * HOUR, token=None, voided=1)

    s.tick()

    assert window_tokens(s) == [None, None]
    assert len(window_ids(s)) == 2


def test_clearing_tokens_counts_only_the_tokens_it_cleared(make_scheduler):
    s = make(make_scheduler)
    add_window(s, 30 * HOUR, token="ctx-1")
    add_window(s, 30 * HOUR, token=None)  # 过期了，但本来就没有凭证
    add_window(s, 1 * HOUR, token="ctx-2")  # 还没过期

    assert clear_window_tokens(s.db, s.config, s.clock) == 1
    assert clear_window_tokens(s.db, s.config, s.clock) == 0
    assert window_tokens(s) == [None, None, "ctx-2"]


def test_tokens_are_cleared_in_the_minute_round_not_only_the_daily_one(make_scheduler):
    s = make(make_scheduler)
    s.tick()  # 今天的每日留存已经做过了
    add_window(s, 25 * HOUR, token="ctx-old")

    s.tick(60)

    assert window_tokens(s) == [None]


def enqueue(s, project, token: str) -> str:
    now = s.clock.now_ms()
    with s.db.txn() as conn:
        queued = s.inbox.enqueue(
            conn,
            project,
            text="持仓",
            has_text=True,
            at_ms=now,
            received_ms=now,
            via="default",
            context_token=token,
        )
    return queued.id


def test_conversation_tokens_do_not_outlive_their_windows(make_scheduler):
    """规格场景：使用者先后发了两句话（两个窗口，对话凭证 ctx-1、ctx-2），项目都取走并确认了；
    25 小时没有新话，清理跑了一轮。数据库文件（做一次检查点后）里搜不到 ctx-1 和 ctx-2。"""
    s = make(make_scheduler)
    project = s.hub.ready()  # 使用者说了第一句话：ctx-1
    enqueue(s, project, "ctx-1")
    s.clock.advance(60)
    s.hub.speak()  # 第二句：ctx-2，ctx-1 所在的窗口被取代
    enqueue(s, project, "ctx-2")
    leased = s.inbox.fetch(project, wait=0, max_items=10)
    assert len(leased) == 2
    s.inbox.ack(project, [item.id for item in leased])

    assert window_tokens(s) == [None, "ctx-2"]  # 第二句话到达时，第一个窗口的凭证已经清掉
    assert b"ctx-2" in db_bytes(s)

    s.clock.advance(25 * 3600)
    s.tick()

    assert window_tokens(s) == [None, None]
    tokens = s.db.conn().execute("SELECT context_token FROM inbox").fetchall()
    assert [row["context_token"] for row in tokens] == [None, None]
    data = db_bytes(s)
    assert b"ctx-1" not in data
    assert b"ctx-2" not in data


# ---------------------------------------------------------------- 每天一次

NEW_DAY = "2026-10-13 00:05"


def test_the_pass_runs_on_the_first_round(make_scheduler):
    s = make(make_scheduler)
    alpha = s.hub.add("alpha", guaranteed=3, cap=4)
    add_outbox(s, alpha, 91 * DAY)

    s.tick()

    assert outbox_ids(s) == []


def test_the_pass_runs_once_a_day_not_every_round(make_scheduler):
    s = make(make_scheduler)
    alpha = s.hub.add("alpha", guaranteed=3, cap=4)
    s.tick()
    later = add_outbox(s, alpha, 91 * DAY)

    for _ in range(5):
        s.tick(30)

    assert outbox_ids(s) == [later]  # 同一天，不再做


def test_the_next_day_runs_it_again(make_scheduler):
    s = make(make_scheduler)
    alpha = s.hub.add("alpha", guaranteed=3, cap=4)
    s.tick()
    later = add_outbox(s, alpha, 91 * DAY)

    go_to(s.clock, NEW_DAY)
    s.tick()

    assert later not in outbox_ids(s)


def test_a_restart_runs_it_again_on_its_first_round(make_scheduler):
    s = make(make_scheduler)
    alpha = s.hub.add("alpha", guaranteed=3, cap=4)
    s.tick()
    later = add_outbox(s, alpha, 91 * DAY)

    s.restart()
    s.tick()

    assert later not in outbox_ids(s)


def test_the_day_is_the_day_in_the_display_time_zone(make_scheduler):
    s = make(make_scheduler, display={"timezone": "Asia/Tokyo"})
    alpha = s.hub.add("alpha", guaranteed=3, cap=4)
    go_to(s.clock, "2026-10-12 22:50")  # 东京 23:50
    s.tick()
    later = add_outbox(s, alpha, 91 * DAY)

    go_to(s.clock, "2026-10-12 23:05")  # 东京 00:05，已经是第二天（北京还是同一天）
    s.tick()

    assert later not in outbox_ids(s)


def test_a_failed_pass_is_tried_again_on_the_next_round(make_scheduler, monkeypatch):
    from clawbot_hub import scheduler

    s = make(make_scheduler)
    alpha = s.hub.add("alpha", guaranteed=3, cap=4)
    old = add_outbox(s, alpha, 91 * DAY)
    real = scheduler.retention.purge_old
    attempts = []

    def flaky(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("boom")
        return real(*args, **kwargs)

    monkeypatch.setattr(scheduler.retention, "purge_old", flaky)

    s.tick()
    assert outbox_ids(s) == [old]

    s.tick(30)

    assert outbox_ids(s) == []
    assert len(attempts) == 2


# ---------------------------------------------------------------- 事件


def test_a_pass_that_deleted_something_leaves_one_event_with_the_counts(make_scheduler):
    s = make(make_scheduler)
    alpha = s.hub.add("alpha", guaranteed=3, cap=4)
    window = add_window(s, 91 * DAY)
    add_outbox(s, alpha, 91 * DAY, window=window)
    add_outbox(s, alpha, 92 * DAY)
    with s.db.txn() as conn:
        add_event(conn, s.clock.now_ms() - 95 * DAY, "lifecycle", "很旧的事件")

    s.tick()

    [event] = [d for d in lifecycle(s) if d.startswith("留存清理：")]
    assert "2 条发送记录" in event
    assert "1 个窗口" in event
    assert "1 条事件" in event


def test_the_event_names_the_configured_period_and_only_what_was_deleted(make_scheduler):
    s = make(make_scheduler, events={"keep_days": 30})
    with s.db.txn() as conn:
        add_event(conn, s.clock.now_ms() - 31 * DAY, "lifecycle", "很旧的事件")

    s.tick()

    [event] = [d for d in lifecycle(s) if d.startswith("留存清理：")]
    assert event == "留存清理：删了 1 条事件（超过 30 天）"


def test_a_pass_with_nothing_to_delete_leaves_no_event(make_scheduler):
    s = make(make_scheduler)
    s.hub.add("alpha", guaranteed=3, cap=4)

    s.tick()

    assert [d for d in lifecycle(s) if d.startswith("留存清理")] == []


def test_only_deleted_records_count_not_released_keys_or_cleared_markers():
    purged = Purged(outbox=1, events=2, windows=3, usage=4, keys=50, markers=600)

    assert purged.deleted == 1 + 2 + 3 + 4
    assert Purged(keys=5, markers=6).deleted == 0


def test_clearing_markers_alone_is_not_worth_an_event(make_scheduler):
    s = make(make_scheduler)
    s.execute("INSERT INTO alert_state (key, episode) VALUES ('window_blocked:9999', '1')")

    s.tick()

    assert s.state("window_blocked:9999") is None
    assert [d for d in lifecycle(s) if d.startswith("留存清理")] == []


def test_releasing_keys_alone_is_not_worth_an_event(make_scheduler):
    s = make(make_scheduler)
    alpha = s.hub.add("alpha", guaranteed=3, cap=4)
    add_outbox(s, alpha, 25 * HOUR, dedupe="digest")

    s.tick()

    assert [d for d in lifecycle(s) if d.startswith("留存清理")] == []
