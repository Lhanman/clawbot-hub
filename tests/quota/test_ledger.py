"""窗口、预占和逐条对账（规格 `hub-quota`「窗口」「预占和对账」，任务 A7）。

这里的数据库是真的（临时目录里的 SQLite），时钟是假的；项目用 A6 的 `Projects` 登记。判定本身的
表格式测试在 `test_decide.py`，腾讯拒绝后的暂停在 `test_hold.py`。
"""

from __future__ import annotations

import pytest

from clawbot_hub.config import Config
from clawbot_hub.db import Database
from clawbot_hub.loginstate import reset_conversation
from clawbot_hub.projects import Projects
from clawbot_hub.quota import Deny, Quota, Request, Reservation, Share
from tests.quota.conftest import (
    HOUR,
    MINUTE,
    NOT_SENT,
    REJECTED,
    SENT,
    UNCONFIRMED,
    events,
    insert_outbox,
    race,
    register,
    reserve,
    settle,
    speak,
    usage,
    window_row,
    with_quota,
)


def two_projects(projects) -> None:
    register(projects, "alpha", guaranteed=6, cap=8)
    register(projects, "bravo", guaranteed=2, cap=4)


# ---------------------------------------------------------------- 开窗口


def test_a_message_opens_a_window_that_starts_at_its_own_time(database, quota, clock):
    started = clock.now_ms() - 5 * MINUTE

    window_id = speak(database, quota, started, "tok-1")

    with database.txn() as conn:
        window = quota.current_window(conn)
    assert window.id == window_id
    assert (window.started_ms, window.context_token) == (started, "tok-1")
    assert (window.blocked_until_ms, window.blocked_code) == (None, None)


def test_there_is_no_window_before_anyone_has_spoken(database, quota):
    with database.txn() as conn:
        assert quota.current_window(conn) is None
        assert quota.read_state(conn).window is None


def test_each_new_message_starts_over_with_nothing_used_and_drops_the_old_token(
    database, quota, projects, clock
):
    register(projects, "alpha", guaranteed=6, cap=8)
    first = speak(database, quota, clock.now_ms() - HOUR, "tok-1")
    reserve(database, quota, "alpha", 7)
    clock.advance(60)

    second = speak(database, quota, clock.now_ms(), "tok-2")

    assert second != first
    assert usage(database) == {}  # 新窗口清零
    assert usage(database, first) == {("alpha", "push"): 7}  # 旧窗口的账留着
    assert window_row(database, first)["context_token"] is None  # 被取代的窗口不再留着对话凭证
    assert window_row(database, second)["context_token"] == "tok-2"
    with database.txn() as conn:
        assert quota.read_state(conn).used == 0


def test_an_old_message_does_not_wind_the_window_back(database, quota, projects, clock):
    register(projects, "alpha", guaranteed=6, cap=8)
    ten = clock.now_ms()
    first = speak(database, quota, ten, "tok-10:00")
    reserve(database, quota, "alpha", 3)

    assert speak(database, quota, ten - 30 * MINUTE, "tok-09:30") is None  # 补收的旧消息

    with database.txn() as conn:
        window = quota.current_window(conn)
    assert (window.id, window.started_ms, window.context_token) == (first, ten, "tok-10:00")
    assert usage(database) == {("alpha", "push"): 3}  # 已用条数不变
    assert database.conn().execute("SELECT COUNT(*) FROM windows").fetchone()[0] == 1
    later = speak(database, quota, ten + 5 * MINUTE, "tok-10:05")
    assert later is not None and later != first
    assert usage(database) == {}


def test_a_message_at_exactly_the_start_of_the_window_opens_a_new_one(database, quota, clock):
    first = speak(database, quota, clock.now_ms(), "tok-1")

    second = speak(database, quota, clock.now_ms(), "tok-2")

    assert second is not None and second != first
    with database.txn() as conn:
        assert quota.current_window(conn).context_token == "tok-2"


def test_the_latest_window_is_the_one_that_started_last(database, quota, clock):
    speak(database, quota, clock.now_ms() - 2 * HOUR, "tok-1")
    third = speak(database, quota, clock.now_ms(), "tok-3")
    speak(database, quota, clock.now_ms() - HOUR, "tok-2")  # 比当前的旧：不开

    with database.txn() as conn:
        assert quota.current_window(conn).id == third


def test_a_voided_window_counts_as_never_having_heard_from_the_user(
    database, quota, projects, clock
):
    """重新绑定账号时作废的窗口不算数：作废之后、使用者再发话之前，按"从来没收到过消息"处理。"""
    register(projects, "alpha", guaranteed=6, cap=8)
    speak(database, quota, clock.now_ms() - HOUR)
    reserve(database, quota, "alpha", 2)  # 作废的窗口里留着账，新的状态里不能出现
    with database.txn() as conn:
        reset_conversation(conn)
        assert quota.current_window(conn) is None
        state = quota.read_state(conn)

    assert (state.window, state.used, state.replies_used) == (None, 0, 0)
    assert state.shares["alpha"].used == 0
    assert reserve(database, quota, "alpha") == Deny("never_contacted", n=1)
    assert reserve(database, quota, "_hub", kind="reply") == Deny("never_contacted", n=1)
    # 再发话才恢复；这条消息比作废的窗口还早也照样开（没有"当前窗口"可以倒回）
    assert speak(database, quota, clock.now_ms() - 2 * HOUR, "tok-new") is not None
    assert isinstance(reserve(database, quota, "alpha"), Reservation)


def test_the_window_closes_when_the_configured_hours_have_passed(database, quota, projects, clock):
    register(projects, "alpha", guaranteed=6, cap=8)
    started = clock.now_ms()
    speak(database, quota, started)
    clock.advance(24 * 3600 - 1)
    assert isinstance(reserve(database, quota, "alpha"), Reservation)

    clock.advance(1)

    assert reserve(database, quota, "alpha") == Deny("window_closed", n=1, started_ms=started)
    assert reserve(database, quota, "_hub", kind="reply").code == "window_closed"


def test_the_window_length_and_size_come_from_the_configuration(database, clock):
    config = with_quota(Config(), window_hours=12, window_limit=5, reply_reserve=1)
    small = Quota(config, clock)
    projects = Projects(database, config, clock)
    register(projects, "alpha", guaranteed=3, cap=4)
    speak(database, small, clock.now_ms())

    assert [type(reserve(database, small, "alpha")).__name__ for _ in range(5)] == [
        "Reservation",
        "Reservation",
        "Reservation",
        "Reservation",
        "Deny",
    ]
    clock.advance(12 * 3600)
    assert reserve(database, small, "alpha").code == "window_closed"


# ---------------------------------------------------------------- 现状：谁算进去


def test_the_state_counts_every_project_and_the_internal_names(database, quota, projects, clock):
    register(projects, "alpha", guaranteed=4, cap=8)
    register(projects, "bravo", guaranteed=2, cap=4)
    window = speak(database, quota, clock.now_ms())
    reserve(database, quota, "alpha", 2)
    reserve(database, quota, "bravo")
    reserve(database, quota, "alpha", kind="reply")
    reserve(database, quota, "_hub", kind="reply")
    with database.txn() as conn:  # 导入时记在 `_import` 名下的推送
        conn.execute(
            "INSERT INTO usage (window_id, project_id, kind, n)"
            " SELECT ?, id, 'push', 1 FROM projects WHERE name = '_import'",
            (window,),
        )

    with database.txn() as conn:
        state = quota.read_state(conn)

    assert state.used == 6  # 2 + 1 + 1 + 1 + 1
    assert state.replies_used == 2  # 回复分开记；`_hub` 的回复同时算进回复已用
    assert state.shares == {"alpha": Share(4, 8, 2), "bravo": Share(2, 4, 1)}


def test_only_enabled_projects_are_in_the_shares_and_hold_a_guarantee(
    database, quota, projects, clock
):
    two_projects(projects)
    register(projects, "cron-job", push_only=True, guaranteed=0, cap=2)
    speak(database, quota, clock.now_ms())
    with database.txn() as conn:
        assert set(quota.read_state(conn).shares) == {"alpha", "bravo", "cron-job"}  # 只推送的也算

    assert reserve(database, quota, "alpha", 7).code == "reserved_for_others"  # B 的保底占着
    projects.disable("bravo")

    with database.txn() as conn:
        assert set(quota.read_state(conn).shares) == {"alpha", "cron-job"}
    assert isinstance(reserve(database, quota, "alpha", 7), Reservation)  # 保底放出来了


def test_a_removed_project_holds_nothing(database, quota, projects, clock):
    two_projects(projects)
    speak(database, quota, clock.now_ms())
    projects.disable("bravo")
    projects.remove("bravo", yes=True)

    with database.txn() as conn:
        assert set(quota.read_state(conn).shares) == {"alpha"}
    assert isinstance(reserve(database, quota, "alpha", 7), Reservation)


def test_usage_of_an_older_window_is_not_part_of_the_state(database, quota, projects, clock):
    register(projects, "alpha", guaranteed=6, cap=8)
    speak(database, quota, clock.now_ms() - 2 * HOUR)
    reserve(database, quota, "alpha", 7)
    reserve(database, quota, "_hub", kind="reply")
    speak(database, quota, clock.now_ms())

    with database.txn() as conn:
        state = quota.read_state(conn)

    assert (state.used, state.replies_used, state.shares["alpha"].used) == (0, 0, 0)


# ---------------------------------------------------------------- 预占


def test_reserving_books_every_part_at_once_and_remembers_the_window_and_the_token(
    database, quota, projects, clock
):
    two_projects(projects)
    window = speak(database, quota, clock.now_ms(), "tok-1")

    reservation = reserve(database, quota, "alpha", 3)

    assert isinstance(reservation, Reservation)
    assert (reservation.window_id, reservation.project, reservation.kind, reservation.n) == (
        window,
        "alpha",
        "push",
        3,
    )
    assert reservation.context_token == "tok-1"  # 推送用窗口的对话凭证
    assert reservation.probe is None
    assert usage(database) == {("alpha", "push"): 3}


def test_a_reply_is_booked_as_a_reply_and_brings_no_token_of_its_own(
    database, quota, projects, clock
):
    register(projects, "alpha", guaranteed=6, cap=8)
    window = speak(database, quota, clock.now_ms(), "tok-1")

    reservation = reserve(database, quota, "alpha", kind="reply")

    assert (reservation.window_id, reservation.kind) == (window, "reply")
    assert reservation.context_token is None  # 回复用被回复那句话的凭证，由调用者另给
    assert usage(database) == {("alpha", "reply"): 1}


def test_the_service_own_replies_are_booked_under_the_hub_name(database, quota, clock):
    speak(database, quota, clock.now_ms())

    assert isinstance(reserve(database, quota, "_hub", kind="reply"), Reservation)

    assert usage(database) == {("_hub", "reply"): 1}


def test_the_same_project_adds_up_over_several_reservations(database, quota, projects, clock):
    register(projects, "alpha", guaranteed=6, cap=8)
    speak(database, quota, clock.now_ms())

    reserve(database, quota, "alpha", 2)
    reserve(database, quota, "alpha", 3)

    assert usage(database) == {("alpha", "push"): 5}


def test_a_refusal_books_nothing(database, quota, projects, clock):
    register(projects, "alpha", guaranteed=6, cap=8)
    speak(database, quota, clock.now_ms())
    reserve(database, quota, "alpha", 8)

    denied = reserve(database, quota, "alpha")

    assert denied == Deny("project_cap", n=1, free=2, cap=8, used=8)
    assert usage(database) == {("alpha", "push"): 8}


def test_a_push_from_a_project_that_is_not_enabled_is_a_programming_error(
    database, quota, projects, clock
):
    two_projects(projects)
    speak(database, quota, clock.now_ms())
    projects.disable("bravo")

    with pytest.raises(ValueError, match="bravo"):
        reserve(database, quota, "bravo")


@pytest.mark.parametrize("kind", ["push", "reply"])
def test_a_project_that_does_not_exist_is_a_programming_error(
    database, quota, projects, clock, kind
):
    register(projects, "alpha", guaranteed=6, cap=8)
    speak(database, quota, clock.now_ms())

    with pytest.raises(ValueError, match="ghost"):
        reserve(database, quota, "ghost", kind=kind)

    assert usage(database) == {}


def test_the_outbox_row_learns_the_window_it_was_booked_in(database, quota, projects, clock):
    register(projects, "alpha", guaranteed=6, cap=8)
    window = speak(database, quota, clock.now_ms())
    with database.txn() as conn:
        insert_outbox(conn, "o-1", "alpha", parts=2)
        quota.reserve(conn, Request("alpha", n=2), outbox_id="o-1")

    row = database.conn().execute("SELECT window_id FROM outbox WHERE id = 'o-1'").fetchone()
    assert row["window_id"] == window


def test_a_refused_request_is_linked_to_its_window_when_there_is_one(
    database, quota, projects, clock
):
    register(projects, "alpha", guaranteed=6, cap=8)

    def refuse(outbox_id: str, n: int):
        with database.txn() as conn:
            insert_outbox(conn, outbox_id, "alpha")
            result = quota.reserve(conn, Request("alpha", n=n), outbox_id=outbox_id)
        row = database.conn().execute("SELECT window_id FROM outbox WHERE id = ?", (outbox_id,))
        return result.code, row.fetchone()["window_id"]

    assert refuse("o-none", 1) == ("never_contacted", None)  # 没有窗口：记录里也没有
    window = speak(database, quota, clock.now_ms())
    assert refuse("o-big", 11) == ("quota_insufficient", window)
    clock.advance(25 * 3600)
    assert refuse("o-old", 1) == ("window_closed", window)  # 过期的窗口也是窗口


def test_an_error_before_the_commit_rolls_everything_back(database, quota, projects, clock):
    register(projects, "alpha", guaranteed=6, cap=8)
    speak(database, quota, clock.now_ms())

    with pytest.raises(RuntimeError, match="boom"):
        with database.txn() as conn:
            insert_outbox(conn, "o-1", "alpha", parts=3)
            quota.reserve(conn, Request("alpha", n=3), outbox_id="o-1")
            raise RuntimeError("boom")  # 判定、记账、建记录都做了，提交之前出错

    assert usage(database) == {}
    assert database.conn().execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    assert isinstance(reserve(database, quota, "alpha", 8), Reservation)  # 额度一条没被占


# ---------------------------------------------------------------- 逐条对账


def test_three_parts_and_the_second_one_fails(database, quota, projects, clock):
    """窗口已用先是 +3，结束后是 +1：第 1 条算用掉，第 2、3 条退回。"""
    register(projects, "alpha", guaranteed=6, cap=8)
    speak(database, quota, clock.now_ms())
    reservation = reserve(database, quota, "alpha", 3)
    assert usage(database) == {("alpha", "push"): 3}

    settle(database, quota, reservation, SENT)
    assert usage(database) == {("alpha", "push"): 3}  # 每条发完就记一条，没发完的先占着
    settle(database, quota, reservation, REJECTED, code="40001")
    assert usage(database) == {("alpha", "push"): 2}
    settle(database, quota, reservation, NOT_SENT)

    assert usage(database) == {("alpha", "push"): 1}


def test_an_unconfirmed_part_counts_as_used(database, quota, projects, clock):
    register(projects, "alpha", guaranteed=6, cap=8)
    speak(database, quota, clock.now_ms())
    reservation = reserve(database, quota, "alpha", 2)

    settle(database, quota, reservation, UNCONFIRMED)
    settle(database, quota, reservation, NOT_SENT)  # 拿不准的这一条之后不再发

    assert usage(database) == {("alpha", "push"): 1}


def test_parts_that_never_got_started_are_given_back_together(database, quota, projects, clock):
    register(projects, "alpha", guaranteed=6, cap=8)
    speak(database, quota, clock.now_ms())
    reservation = reserve(database, quota, "alpha", 5)
    settle(database, quota, reservation, SENT)

    settle(database, quota, reservation, NOT_SENT, count=4)  # 剩下 4 条没来得及发

    assert usage(database) == {("alpha", "push"): 1}


def test_a_reply_is_settled_like_a_push(database, quota, projects, clock):
    register(projects, "alpha", guaranteed=6, cap=8)
    speak(database, quota, clock.now_ms())
    reservation = reserve(database, quota, "alpha", 3, kind="reply")

    settle(database, quota, reservation, SENT)
    settle(database, quota, reservation, REJECTED)  # 回复被拒不用给代码，也不引起暂停
    settle(database, quota, reservation, NOT_SENT)

    assert usage(database) == {("alpha", "reply"): 1}


def test_giving_back_never_takes_the_count_below_zero(database, quota, projects, clock):
    """调用方多退了一次（不该发生）：已用停在 0，不让整个发送因此出错。"""
    register(projects, "alpha", guaranteed=6, cap=8)
    speak(database, quota, clock.now_ms())
    reservation = reserve(database, quota, "alpha", 1)
    settle(database, quota, reservation, NOT_SENT)

    settle(database, quota, reservation, NOT_SENT)
    settle(database, quota, reservation, NOT_SENT, count=5)

    assert usage(database) == {}
    assert isinstance(reserve(database, quota, "alpha", 8), Reservation)


@pytest.mark.parametrize("count", [0, -1, 1.5, True, "1"])
def test_settling_a_number_of_messages_that_makes_no_sense_is_refused(
    database, quota, projects, clock, count
):
    register(projects, "alpha", guaranteed=6, cap=8)
    speak(database, quota, clock.now_ms())
    reservation = reserve(database, quota, "alpha", 2)

    with pytest.raises(ValueError, match="条数"):
        settle(database, quota, reservation, NOT_SENT, count=count)

    assert usage(database) == {("alpha", "push"): 2}


@pytest.mark.parametrize("code", [None, "", "   ", 40001])
def test_a_rejection_without_a_usable_code_is_refused(database, quota, projects, clock, code):
    register(projects, "alpha", guaranteed=6, cap=8)
    speak(database, quota, clock.now_ms())
    reservation = reserve(database, quota, "alpha")

    with pytest.raises(ValueError, match="代码"):
        settle(database, quota, reservation, REJECTED, code=code)

    assert usage(database) == {("alpha", "push"): 1}


def test_a_rejected_push_needs_the_code_that_tencent_gave(database, quota, projects, clock):
    register(projects, "alpha", guaranteed=6, cap=8)
    speak(database, quota, clock.now_ms())
    reservation = reserve(database, quota, "alpha", 2)

    with pytest.raises(ValueError, match="代码"):
        settle(database, quota, reservation, REJECTED)

    assert usage(database) == {("alpha", "push"): 2}  # 出错的对账没有留下半截


# ---------------------------------------------------------------- 发送途中换窗口


def test_the_window_changes_while_the_second_part_is_in_flight(database, quota, projects, clock):
    """第 2 条在飞时使用者说了话：新窗口的已用是 2（第 2、3 条）；对话凭证全程是预占时的那一个。"""
    register(projects, "alpha", guaranteed=6, cap=8)
    first = speak(database, quota, clock.now_ms(), "tok-1")
    reservation = reserve(database, quota, "alpha", 3)
    settle(database, quota, reservation, SENT)
    clock.advance(1)
    second = speak(database, quota, clock.now_ms(), "tok-2")  # 第 2 条还没回话

    settle(database, quota, reservation, SENT)
    settle(database, quota, reservation, SENT)

    assert usage(database, second) == {("alpha", "push"): 2}
    assert usage(database, first) == {("alpha", "push"): 3}  # 旧窗口的数不退
    assert reservation.context_token == "tok-1"


def test_the_window_changes_while_the_third_part_is_in_flight(database, quota, projects, clock):
    register(projects, "alpha", guaranteed=6, cap=8)
    first = speak(database, quota, clock.now_ms(), "tok-1")
    reservation = reserve(database, quota, "alpha", 3)
    settle(database, quota, reservation, SENT)
    settle(database, quota, reservation, SENT)
    clock.advance(1)
    second = speak(database, quota, clock.now_ms(), "tok-2")  # 第 3 条还没回话

    settle(database, quota, reservation, SENT)

    assert usage(database, second) == {("alpha", "push"): 1}
    assert usage(database, first) == {("alpha", "push"): 3}


def test_parts_that_did_not_go_out_after_the_window_changed_are_not_given_back(
    database, quota, projects, clock
):
    register(projects, "alpha", guaranteed=6, cap=8)
    first = speak(database, quota, clock.now_ms(), "tok-1")
    reservation = reserve(database, quota, "alpha", 3)
    settle(database, quota, reservation, SENT)  # 第 1 条发在换窗口之前，留在旧窗口
    clock.advance(1)
    second = speak(database, quota, clock.now_ms(), "tok-2")

    settle(database, quota, reservation, REJECTED, code="40001")
    settle(database, quota, reservation, NOT_SENT)

    assert usage(database, second) == {}  # 旧窗口的数字不再重要，新窗口不动
    assert usage(database, first) == {("alpha", "push"): 3}


def test_an_unconfirmed_part_sent_after_the_window_changed_counts_in_the_new_window(
    database, quota, projects, clock
):
    register(projects, "alpha", guaranteed=6, cap=8)
    speak(database, quota, clock.now_ms(), "tok-1")
    reservation = reserve(database, quota, "alpha", 1)
    clock.advance(1)
    second = speak(database, quota, clock.now_ms(), "tok-2")

    settle(database, quota, reservation, UNCONFIRMED)

    assert usage(database, second) == {("alpha", "push"): 1}


def test_several_parts_settled_together_after_the_window_changed_are_booked_in_full(
    database, quota, projects, clock
):
    register(projects, "alpha", guaranteed=6, cap=8)
    speak(database, quota, clock.now_ms(), "tok-1")
    reservation = reserve(database, quota, "alpha", 4)
    clock.advance(1)
    second = speak(database, quota, clock.now_ms(), "tok-2")

    settle(database, quota, reservation, SENT, count=3)

    assert usage(database, second) == {("alpha", "push"): 3}


def test_a_reply_changes_the_window_it_is_booked_in_the_same_way(database, quota, projects, clock):
    register(projects, "alpha", guaranteed=6, cap=8)
    speak(database, quota, clock.now_ms(), "tok-1")
    reservation = reserve(database, quota, "alpha", 2, kind="reply")
    clock.advance(1)
    second = speak(database, quota, clock.now_ms(), "tok-2")

    settle(database, quota, reservation, SENT)

    assert usage(database, second) == {("alpha", "reply"): 1}


def test_a_reply_is_booked_in_the_latest_window_whichever_message_it_answers(
    database, quota, projects, clock
):
    """使用者先后发了 A、B 两句话（B 开了新窗口），项目拿 A 的句柄回复：记在 B 的窗口里。"""
    register(projects, "alpha", guaranteed=6, cap=8)
    speak(database, quota, clock.now_ms(), "tok-a")
    clock.advance(60)
    window_b = speak(database, quota, clock.now_ms(), "tok-b")

    reservation = reserve(database, quota, "alpha", kind="reply")

    assert reservation.window_id == window_b
    assert usage(database, window_b) == {("alpha", "reply"): 1}


def test_the_window_is_voided_while_a_part_is_in_flight(database, quota, projects, clock):
    register(projects, "alpha", guaranteed=6, cap=8)
    first = speak(database, quota, clock.now_ms(), "tok-1")
    reservation = reserve(database, quota, "alpha", 3)
    with database.txn() as conn:
        reset_conversation(conn)  # 重新绑定了账号

    settle(database, quota, reservation, SENT)  # 没有"当前窗口"可记：不出错，也不乱记
    settle(database, quota, reservation, NOT_SENT, count=2)

    assert usage(database, first) == {("alpha", "push"): 3}
    assert database.conn().execute("SELECT COUNT(*) FROM windows").fetchone()[0] == 1


# ---------------------------------------------------------------- 同时来的请求


def last_slot_world(database, quota, projects, clock, *, replies_used: int):
    """A（保底 4、上限 8）、B（保底 2、上限 4）：A 已推 5、B 已推 2，回复已用 `replies_used` 条。"""
    register(projects, "alpha", guaranteed=4, cap=8)
    register(projects, "bravo", guaranteed=2, cap=4)
    speak(database, quota, clock.now_ms())
    reserve(database, quota, "alpha", 5)
    reserve(database, quota, "bravo", 2)
    if replies_used:
        reserve(database, quota, "_hub", replies_used, kind="reply")


def test_two_projects_race_for_the_last_slot_when_the_reserve_is_used_up(
    database, quota, projects, clock
):
    last_slot_world(
        database, quota, projects, clock, replies_used=2
    )  # 已用 9 条，剩 1 条（在共享池里）

    results = race(database, quota, ["alpha", "bravo"])

    assert sorted(type(result).__name__ for result in results) == ["Deny", "Reservation"]
    (denied,) = [result for result in results if isinstance(result, Deny)]
    assert denied.code == "quota_exhausted"
    assert sum(usage(database).values()) == 10  # 不超过总条数


def test_two_projects_race_when_the_reserve_is_still_there(database, quota, projects, clock):
    last_slot_world(database, quota, projects, clock, replies_used=0)  # 已用 7 条

    results = race(database, quota, ["alpha", "bravo"])

    assert sorted(type(result).__name__ for result in results) == ["Deny", "Reservation"]
    (denied,) = [result for result in results if isinstance(result, Deny)]
    assert denied.code == "reserved_for_others"  # 放行之后只剩 2 条，都是留给回复的
    assert sum(usage(database).values()) == 8


def test_the_same_two_requests_one_after_the_other_give_the_same_answer(
    database, quota, projects, clock
):
    last_slot_world(database, quota, projects, clock, replies_used=2)

    first = reserve(database, quota, "alpha")
    second = reserve(database, quota, "bravo")

    assert isinstance(first, Reservation)
    assert second.code == "quota_exhausted"


def test_many_threads_never_book_more_than_the_window_holds(database, quota, projects, clock):
    register(projects, "alpha", guaranteed=6, cap=8)
    register(projects, "bravo", guaranteed=2, cap=4)
    speak(database, quota, clock.now_ms())

    results = race(database, quota, ["alpha"] * 6 + ["bravo"] * 6)

    booked = [result for result in results if isinstance(result, Reservation)]
    assert len(booked) == 8  # L − R：回复保留的 2 条推送动不了
    assert sum(usage(database).values()) == 8
    assert sum(1 for r in booked if r.project == "alpha") == 6  # 各自的保底一个都不少
    assert sum(1 for r in booked if r.project == "bravo") == 2


# ---------------------------------------------------------------- 重启


def test_a_restart_loses_no_bookings(paths, database, quota, projects, clock, config):
    register(projects, "alpha", guaranteed=6, cap=8)
    started = clock.now_ms() - HOUR
    speak(database, quota, started, "tok-1")
    reserve(database, quota, "alpha", 3)
    reserve(database, quota, "_hub", kind="reply")
    database.close()

    reopened = Database(paths.db)
    try:
        with reopened.txn() as conn:
            window = Quota(config, clock).current_window(conn)
            state = Quota(config, clock).read_state(conn)
    finally:
        reopened.close()

    assert (window.started_ms, window.context_token) == (started, "tok-1")
    assert (state.used, state.replies_used) == (4, 1)
    assert state.shares["alpha"].used == 3


def test_a_send_that_was_still_going_when_the_service_died_becomes_interrupted(
    database, quota, projects, clock
):
    register(projects, "alpha", guaranteed=6, cap=8)
    speak(database, quota, clock.now_ms())
    with database.txn() as conn:
        insert_outbox(conn, "o-1", "alpha", parts=3, now=clock.now_ms())
        quota.reserve(conn, Request("alpha", n=3), outbox_id="o-1")
        insert_outbox(conn, "o-2", "alpha", now=clock.now_ms())
        conn.execute("UPDATE outbox SET status = 'sent' WHERE id = 'o-2'")
        insert_outbox(conn, "o-3", "alpha", now=clock.now_ms())
        conn.execute("UPDATE outbox SET status = 'rejected' WHERE id = 'o-3'")
    clock.advance(90)  # 服务被 kill -9，过了一会儿又起来

    with database.txn() as conn:
        count = quota.interrupt_unfinished(conn)

    assert count == 1
    rows = {
        row["id"]: row
        for row in database.conn().execute("SELECT * FROM outbox ORDER BY id").fetchall()
    }
    assert rows["o-1"]["status"] == "interrupted"
    assert rows["o-1"]["reason_code"] == "interrupted"
    assert "不要重发" in rows["o-1"]["reason"]
    assert rows["o-1"]["finished_ms"] == clock.now_ms()
    assert (rows["o-2"]["status"], rows["o-3"]["status"]) == ("sent", "rejected")
    assert usage(database) == {("alpha", "push"): 3}  # 预占保留不退：不知道发出去了几条


def test_an_interrupted_send_leaves_one_event_without_any_text(database, quota, projects, clock):
    register(projects, "alpha", guaranteed=6, cap=8)
    speak(database, quota, clock.now_ms())
    with database.txn() as conn:
        insert_outbox(conn, "o-1", "alpha", parts=3, now=clock.now_ms())
        insert_outbox(conn, "o-2", "alpha", parts=1, now=clock.now_ms())
    before = len(events(database))

    with database.txn() as conn:
        quota.interrupt_unfinished(conn)

    added = events(database)[before:]
    assert len(added) == 2  # 每条中断的发送各一条
    assert {(e["kind"], e["level"], e["project"]) for e in added} == {("send", "WARNING", "alpha")}
    details = [e["detail"] for e in added]
    assert any(
        "o-1" in detail and "3 条" in detail for detail in details
    )  # 各自写明是哪一条、预占几条
    assert any("o-2" in detail and "1 条" in detail for detail in details)


def test_marking_interrupted_sends_twice_changes_nothing_the_second_time(
    database, quota, projects, clock
):
    register(projects, "alpha", guaranteed=6, cap=8)
    with database.txn() as conn:
        insert_outbox(conn, "o-1", "alpha", now=clock.now_ms())
    with database.txn() as conn:
        assert quota.interrupt_unfinished(conn) == 1
    before = len(events(database))

    with database.txn() as conn:
        assert quota.interrupt_unfinished(conn) == 0

    assert len(events(database)) == before


def test_there_is_nothing_to_interrupt_in_a_fresh_database(database, quota):
    with database.txn() as conn:
        assert quota.interrupt_unfinished(conn) == 0
