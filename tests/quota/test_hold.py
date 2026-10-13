"""腾讯拒绝以后先停一会儿（规格 `hub-quota`「腾讯拒绝以后先停一会儿」，任务 A7）。

暂停记在窗口那一行（`blocked_until_ms`、`blocked_code`）：推送被腾讯明确拒绝就停
`reject_hold_minutes` 分钟；停够以后下一条推送作为试探放行，同一个判定事务里把截止时刻临时设成
"现在 + `send.timeout_seconds`"，别的推送继续被拦；试探的结果在对账时处理。回复不受影响，新窗口
自然解除。
"""

from __future__ import annotations

import dataclasses

import pytest

from clawbot_hub.config import Config
from clawbot_hub.db import Database
from clawbot_hub.projects import Projects
from clawbot_hub.quota import Deny, Hold, Quota, Request, Reservation
from tests.quota.conftest import (
    MINUTE,
    NOT_SENT,
    REJECTED,
    SENT,
    UNCONFIRMED,
    events,
    hold_of,
    race,
    register,
    reserve,
    settle,
    speak,
    usage,
    with_quota,
)

HOLD = 30 * MINUTE  # 默认的暂停：30 分钟
TIMEOUT = 30_000  # 默认的发送总时限：30 秒，试探期间的临时截止


def two_projects(projects) -> None:
    register(projects, "alpha", guaranteed=6, cap=8)
    register(projects, "bravo", guaranteed=2, cap=4)


def paused(database, quota, projects, clock, *, code: str = "40001") -> tuple[int, Hold]:
    """使用者说了话，A 的一条推送被腾讯拒绝：窗口进入暂停。返回（窗口编号，暂停）。"""
    two_projects(projects)
    window = speak(database, quota, clock.now_ms(), "tok-1")
    rejected = reserve(database, quota, "alpha")
    hold = settle(database, quota, rejected, REJECTED, code=code)
    return window, hold


def hold_events(database) -> list[dict]:
    return [event for event in events(database) if event["kind"] == "hold"]


def pause_ends(clock) -> None:
    """让时间走到 30 分钟的暂停刚好过完。"""
    clock.advance(30 * 60)


# ---------------------------------------------------------------- 被拒之后暂停


@pytest.mark.parametrize("code", ["40001", "HTTP 403"])
def test_a_rejected_push_pauses_the_window_and_remembers_the_code(
    database, quota, projects, clock, code
):
    start = clock.now_ms()

    window, hold = paused(database, quota, projects, clock, code=code)

    assert hold == Hold(window_id=window, code=code, until_ms=start + HOLD, minutes=30)
    assert hold_of(database, window) == (start + HOLD, code)
    assert usage(database) == {}  # 被拒的那一条退回了


def test_pushes_are_refused_during_the_pause_and_cost_nothing(database, quota, projects, clock):
    window, hold = paused(database, quota, projects, clock)
    clock.advance(10 * 60)

    for project in ("alpha", "bravo"):
        denied = reserve(database, quota, project)
        assert denied == Deny("upstream_blocked", n=1, hold_code="40001", retry_in_ms=20 * MINUTE)
    assert reserve(database, quota, "alpha", 3).code == "upstream_blocked"
    assert reserve(database, quota, "alpha", priority="urgent").code == "upstream_blocked"
    assert reserve(database, quota, "alpha", priority="deferrable").code == "upstream_blocked"
    assert usage(database) == {}
    assert hold_of(database, window) == (hold.until_ms, "40001")  # 被拦的推送不改暂停


def test_the_code_is_stored_without_surrounding_blanks(database, quota, projects, clock):
    window, hold = paused(database, quota, projects, clock, code="  40001 ")

    assert hold.code == "40001"
    assert hold_of(database, window)[1] == "40001"


def test_the_pause_is_checked_before_the_quota_runs_out(database, quota, projects, clock):
    two_projects(projects)
    speak(database, quota, clock.now_ms())
    reserve(database, quota, "alpha", 5)
    last = reserve(database, quota, "alpha")
    settle(database, quota, last, REJECTED, code="40001")
    reserve(database, quota, "_hub", 5, kind="reply")  # 暂停期里回复照发：窗口用满了
    assert sum(usage(database).values()) == 10

    assert reserve(database, quota, "bravo").code == "upstream_blocked"  # 不是 quota_exhausted


def test_the_length_of_the_pause_comes_from_the_configuration(database, clock):
    config = with_quota(Config(), reject_hold_minutes=5)
    quota, projects = Quota(config, clock), Projects(database, config, clock)
    start = clock.now_ms()

    window, hold = paused(database, quota, projects, clock)

    assert (hold.minutes, hold.until_ms) == (5, start + 5 * MINUTE)
    assert hold_of(database, window)[0] == start + 5 * MINUTE
    assert reserve(database, quota, "alpha").retry_in_ms == 5 * MINUTE


def test_replies_still_go_through_during_the_pause(database, quota, projects, clock):
    window, hold = paused(database, quota, projects, clock)

    mine = reserve(database, quota, "alpha", 2, kind="reply")
    hub = reserve(database, quota, "_hub", kind="reply")

    assert isinstance(mine, Reservation) and isinstance(hub, Reservation)
    assert mine.probe is None and hub.probe is None
    assert usage(database) == {("alpha", "reply"): 2, ("_hub", "reply"): 1}
    assert hold_of(database, window) == (hold.until_ms, "40001")  # 回复不动暂停


def test_a_rejected_reply_pauses_nothing(database, quota, projects, clock):
    two_projects(projects)
    window = speak(database, quota, clock.now_ms())
    reply = reserve(database, quota, "alpha", kind="reply")

    assert settle(database, quota, reply, REJECTED) is None

    assert hold_of(database, window) == (None, None)
    assert isinstance(reserve(database, quota, "alpha"), Reservation)
    assert hold_events(database) == []


def test_a_second_rejection_that_was_already_in_flight_restarts_the_pause(
    database, quota, projects, clock
):
    two_projects(projects)
    window = speak(database, quota, clock.now_ms())
    first = reserve(database, quota, "alpha")
    second = reserve(database, quota, "bravo")
    settle(database, quota, first, REJECTED, code="40001")
    clock.advance(10)

    hold = settle(database, quota, second, REJECTED, code="40002")

    assert hold == Hold(window, "40002", clock.now_ms() + HOLD, 30)
    assert hold_of(database, window) == (clock.now_ms() + HOLD, "40002")  # 以最近这次为准
    assert usage(database) == {}
    assert "又" in hold_events(database)[-1]["detail"]  # 事件里写明是"又被拒绝"


def test_a_rejection_after_the_window_changed_pauses_nothing(database, quota, projects, clock):
    """被拒的那条发在旧窗口的凭证上，新窗口不受牵连。"""
    two_projects(projects)
    old = speak(database, quota, clock.now_ms(), "tok-1")
    reservation = reserve(database, quota, "alpha")
    clock.advance(1)
    new = speak(database, quota, clock.now_ms(), "tok-2")

    assert settle(database, quota, reservation, REJECTED, code="40001") is None

    assert hold_of(database, new) == (None, None)
    assert hold_of(database, old) == (None, None)
    assert isinstance(reserve(database, quota, "alpha"), Reservation)
    assert hold_events(database) == []


def test_the_pause_survives_a_restart(paths, database, quota, projects, clock, config):
    paused(database, quota, projects, clock)
    database.close()

    reopened = Database(paths.db)
    try:
        with reopened.txn() as conn:
            denied = Quota(config, clock).reserve(conn, Request("alpha"))
    finally:
        reopened.close()

    assert denied == Deny("upstream_blocked", n=1, hold_code="40001", retry_in_ms=HOLD)


# ---------------------------------------------------------------- 过了暂停期：试探


def test_the_first_push_after_the_pause_is_the_probe_and_shuts_the_door_behind_it(
    database, quota, projects, clock
):
    window, _ = paused(database, quota, projects, clock)
    pause_ends(clock)
    now = clock.now_ms()

    probe = reserve(database, quota, "alpha", 2)

    assert isinstance(probe, Reservation) and probe.probe is not None
    assert usage(database) == {("alpha", "push"): 2}  # 试探和别的推送一样先占条数
    assert hold_of(database, window) == (now + TIMEOUT, "40001")  # 临时截止：现在 + 发送总时限
    for project in ("alpha", "bravo"):  # 试探在飞，别的推送继续被拦
        assert reserve(database, quota, project) == Deny(
            "upstream_blocked", n=1, hold_code="40001", retry_in_ms=TIMEOUT
        )
    assert usage(database) == {("alpha", "push"): 2}


def test_the_pause_is_still_on_one_millisecond_before_it_ends(database, quota, projects, clock):
    _, hold = paused(database, quota, projects, clock)
    clock.advance(30 * 60 - 0.001)

    assert clock.now_ms() == hold.until_ms - 1
    assert reserve(database, quota, "alpha") == Deny(
        "upstream_blocked", n=1, hold_code="40001", retry_in_ms=1
    )


def test_the_temporary_deadline_follows_the_send_timeout(database, clock):
    default = Config()
    config = dataclasses.replace(
        default, send=dataclasses.replace(default.send, timeout_seconds=10)
    )
    quota, projects = Quota(config, clock), Projects(database, config, clock)
    window, _ = paused(database, quota, projects, clock)
    pause_ends(clock)

    reserve(database, quota, "alpha")

    assert hold_of(database, window)[0] == clock.now_ms() + 10_000


def test_a_probe_that_is_sent_lifts_the_pause(database, quota, projects, clock):
    window, _ = paused(database, quota, projects, clock)
    pause_ends(clock)
    probe = reserve(database, quota, "alpha")

    assert settle(database, quota, probe, SENT) is None

    assert hold_of(database, window) == (None, None)
    follow_up = reserve(database, quota, "alpha")
    assert isinstance(follow_up, Reservation) and follow_up.probe is None  # 之后的推送正常
    assert usage(database) == {("alpha", "push"): 2}


def test_a_probe_that_is_rejected_again_pauses_for_another_period(database, quota, projects, clock):
    window, _ = paused(database, quota, projects, clock)
    pause_ends(clock)
    probe = reserve(database, quota, "alpha")

    hold = settle(database, quota, probe, REJECTED, code="40002")

    assert hold == Hold(window, "40002", clock.now_ms() + HOLD, 30)
    assert hold_of(database, window) == (clock.now_ms() + HOLD, "40002")
    assert reserve(database, quota, "bravo") == Deny(
        "upstream_blocked", n=1, hold_code="40002", retry_in_ms=HOLD
    )
    assert usage(database) == {}


@pytest.mark.parametrize(
    ("result", "used_after"),
    [(UNCONFIRMED, {("alpha", "push"): 1}), (NOT_SENT, {})],
    ids=["unconfirmed", "unreachable"],
)
def test_a_probe_without_a_result_is_taken_back_and_the_next_push_probes_again(
    database, quota, projects, clock, result, used_after
):
    window, hold = paused(database, quota, projects, clock)
    pause_ends(clock)
    probe = reserve(database, quota, "alpha")

    assert settle(database, quota, probe, result) is None

    assert hold_of(database, window) == (hold.until_ms, "40001")  # 临时值撤回，回到原来的截止
    assert usage(database) == used_after
    again = reserve(database, quota, "bravo")
    assert isinstance(again, Reservation) and again.probe is not None
    assert hold_of(database, window) == (clock.now_ms() + TIMEOUT, "40001")


def test_a_probe_that_never_reports_back_runs_out_after_the_send_timeout(
    database, quota, projects, clock
):
    """试探的那条没有对账（服务在中途被杀）：临时截止到点自动过去，下一条推送再试探。"""
    paused(database, quota, projects, clock)
    pause_ends(clock)
    reserve(database, quota, "alpha")
    clock.advance(TIMEOUT / 1000 - 0.001)
    assert reserve(database, quota, "bravo").code == "upstream_blocked"

    clock.advance(0.001)

    again = reserve(database, quota, "bravo")
    assert isinstance(again, Reservation) and again.probe is not None


@pytest.mark.parametrize("late", [SENT, UNCONFIRMED, NOT_SENT])
def test_a_probe_that_ran_over_its_time_does_not_disturb_the_one_that_took_over(
    database, quota, projects, clock, late
):
    """第一个试探超过了发送总时限才有结果：临时截止早到点了，后来的推送已经成了新的试探。"""
    window, _ = paused(database, quota, projects, clock)
    pause_ends(clock)
    first = reserve(database, quota, "alpha")
    clock.advance(TIMEOUT / 1000)
    second = reserve(database, quota, "bravo")
    assert second.probe is not None
    temporary = hold_of(database, window)
    before = len(hold_events(database))

    settle(database, quota, first, late)  # 先前那条晚到的结果

    assert hold_of(database, window) == temporary  # 新试探的临时截止不动
    assert len(hold_events(database)) == before  # 什么都没改，也就没有事件
    settle(database, quota, second, SENT)
    assert hold_of(database, window) == (None, None)  # 新试探成功才解除


def test_a_probe_that_went_out_does_not_undo_a_newer_pause(database, quota, projects, clock):
    two_projects(projects)
    window = speak(database, quota, clock.now_ms(), "tok-1")
    early = reserve(database, quota, "alpha")  # 在飞
    first = reserve(database, quota, "alpha")
    settle(database, quota, first, REJECTED, code="40001")
    pause_ends(clock)
    probe = reserve(database, quota, "alpha")
    newer = settle(database, quota, early, REJECTED, code="40007")  # 先前预占的这一条这时才被拒绝
    before = len(hold_events(database))

    settle(database, quota, probe, SENT)

    assert hold_of(database, window) == (newer.until_ms, "40007")
    assert len(hold_events(database)) == before  # 没解除，就没有"解除"事件
    assert reserve(database, quota, "bravo").hold_code == "40007"


def test_a_probe_must_pass_the_quota_checks_like_any_other_push(database, quota, projects, clock):
    window, hold = paused(database, quota, projects, clock)
    pause_ends(clock)
    reserve(database, quota, "_hub", 10, kind="reply")  # 窗口用满

    denied = reserve(database, quota, "alpha")

    assert denied == Deny("quota_exhausted", n=1, free=0)
    assert hold_of(database, window) == (hold.until_ms, "40001")  # 没放行就没有临时值


def test_a_reply_is_never_a_probe(database, quota, projects, clock):
    window, hold = paused(database, quota, projects, clock)
    pause_ends(clock)

    reply = reserve(database, quota, "alpha", kind="reply")

    assert isinstance(reply, Reservation) and reply.probe is None
    assert hold_of(database, window) == (hold.until_ms, "40001")


def test_only_one_of_several_pushes_arriving_together_becomes_the_probe(
    database, quota, projects, clock
):
    paused(database, quota, projects, clock)
    pause_ends(clock)

    results = race(database, quota, ["alpha", "bravo"] * 4)

    booked = [result for result in results if isinstance(result, Reservation)]
    refused = [result for result in results if isinstance(result, Deny)]
    assert len(booked) == 1 and booked[0].probe is not None
    assert len(refused) == 7 and {result.code for result in refused} == {"upstream_blocked"}
    assert sum(usage(database).values()) == 1  # 假上游只会收到这一条


def test_the_second_part_of_a_probe_leaves_the_pause_alone(database, quota, projects, clock):
    """试探只由第一段的结果决定；第一段拿不准、后面几段没发，不会再动暂停——
    即使这时别的推送已经成了新的试探（时钟没走，两次的临时截止恰好相等）。"""
    window, hold = paused(database, quota, projects, clock)
    pause_ends(clock)
    first = reserve(database, quota, "alpha", 3)
    settle(database, quota, first, UNCONFIRMED)
    second = reserve(database, quota, "bravo")
    assert second.probe is not None
    assert first.probe.temp_until_ms == second.probe.temp_until_ms != hold.until_ms

    settle(database, quota, first, NOT_SENT, count=2)

    assert hold_of(database, window) == (second.probe.temp_until_ms, "40001")  # 新试探的临时值还在
    assert usage(database) == {("alpha", "push"): 1, ("bravo", "push"): 1}


def test_after_a_probe_went_out_the_other_parts_follow_the_normal_rules(
    database, quota, projects, clock
):
    window, _ = paused(database, quota, projects, clock)
    pause_ends(clock)
    probe = reserve(database, quota, "alpha", 3)
    settle(database, quota, probe, SENT)  # 第一段发出去了：暂停解除
    assert hold_of(database, window) == (None, None)

    hold = settle(database, quota, probe, REJECTED, code="40003")  # 第二段又被拒绝：新的暂停

    assert hold == Hold(window, "40003", clock.now_ms() + HOLD, 30)
    settle(database, quota, probe, NOT_SENT)
    assert hold_of(database, window) == (clock.now_ms() + HOLD, "40003")
    assert usage(database) == {("alpha", "push"): 1}


def test_a_probe_result_after_the_window_changed_leaves_the_new_window_alone(
    database, quota, projects, clock
):
    paused(database, quota, projects, clock)
    pause_ends(clock)
    probe = reserve(database, quota, "alpha")
    new = speak(database, quota, clock.now_ms(), "tok-2")  # 试探在飞时使用者说了话

    assert settle(database, quota, probe, REJECTED, code="40002") is None

    assert hold_of(database, new) == (None, None)
    assert isinstance(reserve(database, quota, "bravo"), Reservation)


# ---------------------------------------------------------------- 新窗口解除


def test_a_new_message_lifts_the_pause_and_pushes_go_out_again(database, quota, projects, clock):
    paused(database, quota, projects, clock)
    clock.advance(60)

    new = speak(database, quota, clock.now_ms(), "tok-2")

    assert hold_of(database, new) == (None, None)
    pushed = reserve(database, quota, "alpha")
    assert isinstance(pushed, Reservation) and pushed.probe is None
    assert pushed.context_token == "tok-2"


def test_an_old_message_does_not_lift_the_pause(database, quota, projects, clock):
    window, hold = paused(database, quota, projects, clock)

    assert speak(database, quota, clock.now_ms() - HOLD, "tok-old") is None  # 补收的旧消息

    assert hold_of(database, window) == (hold.until_ms, "40001")
    assert reserve(database, quota, "alpha").code == "upstream_blocked"


def test_a_pause_in_the_new_window_is_the_new_windows_own(database, quota, projects, clock):
    paused(database, quota, projects, clock)
    clock.advance(60)
    second = speak(database, quota, clock.now_ms(), "tok-2")
    pushed = reserve(database, quota, "alpha")

    hold = settle(database, quota, pushed, REJECTED, code="40009")

    assert hold.window_id == second
    assert hold_of(database, second) == (clock.now_ms() + HOLD, "40009")
    assert reserve(database, quota, "bravo").hold_code == "40009"


# ---------------------------------------------------------------- 还能发几条


def test_left_is_zero_during_the_pause_and_back_once_it_is_lifted(database, quota, projects, clock):
    paused(database, quota, projects, clock)
    with database.txn() as conn:
        assert (quota.left(conn, "alpha"), quota.left(conn, "bravo")) == (0, 0)
    pause_ends(clock)  # 暂停期过了：试探能放行，所以有余量（B 的保底留着，A 只能用到 6 条）
    with database.txn() as conn:
        assert quota.left(conn, "alpha") == 6
    probe = reserve(database, quota, "alpha")
    with database.txn() as conn:
        assert quota.left(conn, "alpha") == 0  # 试探在飞，临时的暂停还在
    settle(database, quota, probe, SENT)

    with database.txn() as conn:
        assert quota.left(conn, "alpha") == 5
        assert quota.left(conn, "alpha", login_ok=False) == 0


# ---------------------------------------------------------------- 事件记录


def test_the_pause_leaves_events_that_say_what_happened(database, quota, projects, clock):
    paused(database, quota, projects, clock, code="40001")
    pause_ends(clock)
    unclear = reserve(database, quota, "alpha")
    settle(database, quota, unclear, UNCONFIRMED)  # 第一次试探没有结果
    rejected = reserve(database, quota, "bravo")
    settle(database, quota, rejected, REJECTED, code="40002")  # 第二次试探被拒绝
    pause_ends(clock)
    ok = reserve(database, quota, "alpha")
    settle(database, quota, ok, SENT)  # 第三次试探成功

    story = [(e["level"], e["project"], e["detail"]) for e in hold_events(database)]

    assert [(level, project) for level, project, _ in story] == [
        ("WARNING", "alpha"),  # 暂停开始
        ("INFO", "alpha"),  # 第一次试探放行
        ("INFO", "alpha"),  # 没有结果，撤回
        ("INFO", "bravo"),  # 第二次试探放行
        ("WARNING", "bravo"),  # 又被拒绝
        ("INFO", "alpha"),  # 第三次试探放行
        ("INFO", "alpha"),  # 发出去了，解除
    ]
    details = [detail for _, _, detail in story]
    assert "40001" in details[0] and "30" in details[0] and "又" not in details[0]
    assert "试探" in details[1]
    assert "没有结果" in details[2]
    assert "试探" in details[3]
    assert "试探" in details[4] and "40002" in details[4] and "30" in details[4]
    assert "试探" in details[5]
    assert "解除" in details[6]


def test_a_pause_that_a_new_message_lifts_leaves_an_event_too(database, quota, projects, clock):
    paused(database, quota, projects, clock)
    clock.advance(60)
    before = len(hold_events(database))

    speak(database, quota, clock.now_ms(), "tok-2")

    (added,) = hold_events(database)[before:]
    assert (added["level"], added["project"]) == ("INFO", None)
    assert "新" in added["detail"] and "解除" in added["detail"]


def test_replacing_a_window_that_was_never_paused_leaves_no_hold_event(
    database, quota, projects, clock
):
    two_projects(projects)
    speak(database, quota, clock.now_ms(), "tok-1")
    clock.advance(60)

    speak(database, quota, clock.now_ms(), "tok-2")

    assert hold_events(database) == []


def test_pause_events_never_carry_the_conversation_token(database, quota, projects, clock):
    paused(database, quota, projects, clock, code="HTTP 403")
    pause_ends(clock)
    settle(database, quota, reserve(database, quota, "alpha"), SENT)

    assert hold_events(database)
    for event in events(database):
        assert "tok-1" not in (event["detail"] or "")
