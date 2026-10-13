"""额度判定（规格 `hub-quota`「窗口」「谁能用几条」，任务 A7）：纯函数，表格式测试。

`decide(state, request, cfg, now_ms)` 不碰数据库：窗口、已用条数、各启用项目的保底和上限都在
`State` 里。判定的顺序按规格：窗口 → （推送）暂停 → 总额 → （回复到此放行）→ 可延后 → 上限 →
保底和共享池。
"""

from __future__ import annotations

import dataclasses

import pytest

from clawbot_hub.config import QuotaConfig
from clawbot_hub.quota import (
    Allow,
    Deny,
    Request,
    Share,
    State,
    Window,
    closed_reason,
    decide,
    left,
    window_ends_ms,
)

MINUTE = 60_000
HOUR = 60 * MINUTE
NOW = 1_800_000_000_000
CFG = QuotaConfig()  # 窗口 24 小时、10 条，回复保留 2，可延后至少留 3，暂停 30 分钟


def open_window(**kwargs) -> Window:
    return Window(id=1, started_ms=kwargs.pop("started_ms", NOW - HOUR), **kwargs)


def world(
    shares: dict[str, Share],
    *,
    replies: int = 0,
    others: int = 0,
    window: Window | None = None,
) -> State:
    """窗口里的现状。总已用 = 各项目的推送 + 回复 + `others`（内部名下的用量，只算进总已用）。"""
    used = sum(share.used for share in shares.values()) + replies + others
    return State(window=window or open_window(), shares=shares, used=used, replies_used=replies)


def apply(state: State, request: Request) -> State:
    """放行以后的账：推送记在项目名下，回复记进回复；总已用都加 n。"""
    if request.kind == "reply":
        return dataclasses.replace(
            state, used=state.used + request.n, replies_used=state.replies_used + request.n
        )
    shares = dict(state.shares)
    share = shares[request.project]
    shares[request.project] = dataclasses.replace(share, used=share.used + request.n)
    return dataclasses.replace(state, shares=shares, used=state.used + request.n)


def run(state: State, *requests: Request, cfg: QuotaConfig = CFG, now: int = NOW):
    results = []
    for request in requests:
        decision = decide(state, request, cfg, now)
        results.append(decision)
        if isinstance(decision, Allow):
            state = apply(state, request)
    return results


def verdicts(results) -> list[str]:
    return ["ok" if isinstance(result, Allow) else result.code for result in results]


def push(project: str, n: int = 1, priority: str = "normal") -> Request:
    return Request(project, n=n, kind="push", priority=priority)


def reply(project: str = "_hub", n: int = 1) -> Request:
    return Request(project, n=n, kind="reply")


# ---------------------------------------------------------------- 窗口：开着还是关着


def test_there_is_no_window_before_the_user_has_ever_written():
    assert closed_reason(None, NOW, CFG) == "never_contacted"


def test_a_window_closes_exactly_when_the_window_length_has_passed():
    window = open_window(started_ms=NOW - 24 * HOUR + 1)  # 差 1 毫秒到期
    assert closed_reason(window, NOW, CFG) is None
    assert closed_reason(window, NOW + 1, CFG) == "window_closed"
    assert window_ends_ms(window, CFG) == NOW + 1


def test_a_window_starting_right_now_is_open():
    assert closed_reason(open_window(started_ms=NOW), NOW, CFG) is None


def test_the_window_length_comes_from_the_configuration():
    cfg = QuotaConfig(window_hours=12, window_limit=5)
    window = open_window(started_ms=NOW - 12 * HOUR + 1)

    assert closed_reason(window, NOW, cfg) is None
    assert closed_reason(window, NOW + 1, cfg) == "window_closed"
    assert window_ends_ms(window, cfg) == window.started_ms + 12 * HOUR


@pytest.mark.parametrize("kind", ["push", "reply"])
def test_nothing_is_decided_for_a_closed_window_pushes_and_replies_alike(kind):
    request = Request("alpha", kind=kind)
    alpha = {"alpha": Share(6, 8)}

    never = decide(State(window=None, shares=alpha), request, CFG, NOW)
    expired = decide(
        world(alpha, window=open_window(started_ms=NOW - 25 * HOUR)), request, CFG, NOW
    )

    assert never == Deny("never_contacted", n=1)
    assert expired == Deny("window_closed", n=1, started_ms=NOW - 25 * HOUR)


def test_a_closed_window_is_reported_before_anything_about_the_quota():
    full = world(
        {"alpha": Share(6, 8, used=8)}, replies=2, window=open_window(started_ms=NOW - 30 * HOUR)
    )

    assert decide(full, push("alpha"), CFG, NOW).code == "window_closed"
    assert decide(State(window=None, shares={"alpha": Share(6, 8)}), reply(), CFG, NOW).code == (
        "never_contacted"
    )


def test_a_closed_window_is_reported_before_the_pause():
    window = open_window(
        started_ms=NOW - 30 * HOUR, blocked_until_ms=NOW + 10 * MINUTE, blocked_code="40001"
    )

    assert decide(world({"alpha": Share(6, 8)}, window=window), push("alpha"), CFG, NOW).code == (
        "window_closed"
    )


# ---------------------------------------------------------------- 总额：free ≤ 0 和 n > free


def test_a_full_window_is_exhausted_for_pushes_and_replies():
    state = world({"alpha": Share(6, 8, used=8)}, replies=2)

    assert decide(state, push("alpha"), CFG, NOW) == Deny("quota_exhausted", n=1, free=0)
    assert decide(state, reply(), CFG, NOW) == Deny("quota_exhausted", n=1, free=0)


def test_a_window_that_overshot_is_exhausted_not_negative():
    """换窗口交界处的补记可能让总已用比总条数多几条（规格「预占和对账」）。"""
    state = dataclasses.replace(world({"alpha": Share(6, 8)}), used=12)

    assert decide(state, push("alpha"), CFG, NOW) == Deny("quota_exhausted", n=1, free=0)


def test_asking_for_more_than_is_left_is_insufficient_and_says_how_many_are_left():
    state = world({"alpha": Share(6, 8, used=3)}, replies=4)  # 已用 7，还剩 3

    assert decide(state, push("alpha", n=4), CFG, NOW) == Deny("quota_insufficient", n=4, free=3)
    assert decide(state, reply(n=4), CFG, NOW) == Deny("quota_insufficient", n=4, free=3)


def test_asking_for_exactly_what_is_left_is_not_insufficient():
    state = world({}, replies=7)

    assert decide(state, reply(n=3), CFG, NOW) == Allow(3)


def test_exhausted_comes_before_insufficient_and_before_deferrable_and_cap():
    state = world({"alpha": Share(6, 8, used=8)}, replies=2)

    for priority in ("urgent", "normal", "deferrable"):
        assert decide(state, push("alpha", n=3, priority=priority), CFG, NOW).code == (
            "quota_exhausted"
        )


def test_the_window_size_comes_from_the_configuration():
    cfg = QuotaConfig(window_limit=5, reply_reserve=1)
    state = world({"alpha": Share(3, 4, used=3)}, replies=1)  # 5 条里已用 4 条

    assert decide(state, push("alpha"), cfg, NOW) == Allow(1)
    assert decide(state, push("alpha", n=2), cfg, NOW) == Deny("quota_insufficient", n=2, free=1)


# ---------------------------------------------------------------- 回复到总额这一步就放行


def test_replies_ignore_the_reserve_the_guarantees_and_the_caps():
    """推送把 L − R 条都用光了，回复仍然放行；回复不受上限、保底、可延后限制。"""
    state = world({"alpha": Share(6, 8, used=8), "bravo": Share(2, 4)})

    assert run(state, reply(), reply()) == [Allow(1), Allow(1)]


def test_replies_by_the_hub_itself_need_no_project_share():
    assert decide(world({}), reply("_hub"), CFG, NOW) == Allow(1)


def test_replies_are_counted_against_the_window_and_eat_the_reserve():
    state = world({"alpha": Share(6, 8)})

    assert verdicts(run(state, reply(), reply(), reply())) == ["ok", "ok", "ok"]  # 保留是 2，仍放行


# ---------------------------------------------------------------- 规格里的数字场景


def test_guarantees_plus_the_reserve_fill_the_window_so_there_is_no_shared_pool():
    """L=10、R=2，A（保底 6、上限 8）、B（保底 2、上限 4）：A 连发 6 条，第 7 条被挡住。"""
    state = world({"alpha": Share(6, 8), "bravo": Share(2, 4)})

    results = run(state, *[push("alpha")] * 7)

    assert verdicts(results) == ["ok"] * 6 + ["reserved_for_others"]
    assert results[6] == Deny("reserved_for_others", n=1, free=4, reserved=4)


def test_beyond_the_guarantee_the_shared_pool_is_there_for_whoever_asks_first():
    """A（保底 4、上限 8）、B（保底 2、上限 4）：A 连发 7 条，前 6 条放行（4 保底加 2 共享池），
    第 7 条被挡；B 随后发 2 条都放行。"""
    state = world({"alpha": Share(4, 8), "bravo": Share(2, 4)})

    results = run(state, *[push("alpha")] * 7, push("bravo"), push("bravo"))

    assert verdicts(results) == ["ok"] * 6 + ["reserved_for_others", "ok", "ok"]


def test_the_cap_stops_a_project_even_when_the_window_has_room():
    """只有 A（保底 6、上限 8）：连发 9 条，前 8 条放行，第 9 条是上限（不是留给别人的）。"""
    state = world({"alpha": Share(6, 8)})

    results = run(state, *[push("alpha")] * 9)

    assert verdicts(results) == ["ok"] * 8 + ["project_cap"]
    assert results[8] == Deny("project_cap", n=1, free=2, cap=8, used=8)


def test_the_cap_refusal_carries_both_the_cap_and_what_the_project_has_used():
    """上限和已用不一样大（这一条让它超了上限）：原因文字要用的两个数都带对。"""
    state = world({"alpha": Share(6, 8, used=5)})

    assert decide(state, push("alpha", n=4), CFG, NOW) == Deny(
        "project_cap", n=4, free=5, cap=8, used=5
    )


def test_urgent_may_pass_its_own_cap_but_never_takes_the_others_guarantee():
    """A（保底 4、上限 4）已用 4，B（保底 2、上限 2）：normal 是上限；urgent 用共享池，用完被挡。"""
    state = world({"alpha": Share(4, 4, used=4), "bravo": Share(2, 2)})

    results = run(
        state,
        push("alpha"),
        push("alpha", priority="urgent"),
        push("alpha", priority="urgent"),
        push("alpha", priority="urgent"),
    )

    assert verdicts(results) == ["project_cap", "ok", "ok", "reserved_for_others"]


def test_deferrable_gives_way_when_the_window_gets_tight():
    """只有 A（保底 6、上限 8），已用 6 条：第一条 deferrable 放行，第二条让路，normal 照常。"""
    state = world({"alpha": Share(6, 8, used=6)})

    results = run(
        state,
        push("alpha", priority="deferrable"),
        push("alpha", priority="deferrable"),
        push("alpha"),
    )

    assert verdicts(results) == ["ok", "deferrable_denied", "ok"]
    assert results[1] == Deny("deferrable_denied", n=1, free=3)


def test_the_keep_for_deferrable_comes_from_the_configuration():
    state = world({"alpha": Share(6, 8, used=6)})  # 还剩 4

    assert decide(state, push("alpha", priority="deferrable"), CFG, NOW) == Allow(1)  # 4 - 1 ≥ 3
    stricter = QuotaConfig(deferrable_keep=4)
    assert decide(state, push("alpha", priority="deferrable"), stricter, NOW).code == (
        "deferrable_denied"
    )
    assert decide(
        state, push("alpha", priority="deferrable"), QuotaConfig(deferrable_keep=0), NOW
    ) == (Allow(1))


def test_deferrable_counts_the_whole_message_not_one_part():
    state = world({"alpha": Share(6, 8, used=2)})  # 还剩 8

    assert decide(state, push("alpha", n=5, priority="deferrable"), CFG, NOW) == Allow(
        5
    )  # 8 - 5 = 3
    assert decide(state, push("alpha", n=6, priority="deferrable"), CFG, NOW).code == (
        "deferrable_denied"
    )


def test_deferrable_is_judged_before_the_cap_and_the_guarantees():
    state = world({"alpha": Share(6, 6, used=6), "bravo": Share(2, 4)})  # 还剩 4，A 已到上限

    assert decide(state, push("alpha", n=2, priority="deferrable"), CFG, NOW).code == (
        "deferrable_denied"
    )


def test_urgent_and_normal_are_never_held_back_by_the_deferrable_rule():
    state = world({"alpha": Share(6, 8, used=6)})

    for priority in ("urgent", "normal"):
        assert decide(state, push("alpha", priority=priority), CFG, NOW) == Allow(1)


def test_replies_that_overran_the_reserve_leave_nothing_special_to_protect():
    """L=10、R=2，A（保底 6）、B（保底 2），回复已用 3：保留已经没有了；
    A 在保底内的 6 条都放行，窗口里只剩 1 条时 B 的保底只能用上 1 条（回复先于保底）。"""
    state = world({"alpha": Share(6, 8), "bravo": Share(2, 4)}, replies=3)

    results = run(state, *[push("alpha")] * 6, push("bravo"), push("bravo"))

    assert verdicts(results) == ["ok"] * 6 + ["ok", "quota_exhausted"]


def test_when_the_replies_used_up_the_reserve_the_last_slot_is_the_pools():
    """回复已用 2（保留用光），A 已推送 5、B 已推送 2（共用 9 条）：剩下 1 条，
    两个项目各发 1 条，只有第一个放行。"""
    state = world({"alpha": Share(4, 8, used=5), "bravo": Share(2, 4, used=2)}, replies=2)

    results = run(state, push("alpha"), push("bravo"))

    assert verdicts(results) == ["ok", "quota_exhausted"]


def test_when_the_reserve_is_still_there_the_last_slots_belong_to_the_replies():
    """同上但回复一条都没用（窗口已用 7 条）：放行一条之后只剩 2 条，都是留给回复的。"""
    state = world({"alpha": Share(4, 8, used=5), "bravo": Share(2, 4, used=2)})

    results = run(state, push("alpha"), push("bravo"))

    assert verdicts(results) == ["ok", "reserved_for_others"]
    assert results[1] == Deny("reserved_for_others", n=1, free=2, reserved=2)


def test_a_disabled_project_no_longer_holds_a_guarantee():
    """B（保底 2）停用以后不在 `shares` 里：共享池变成 2，A 可以用到 8 条。"""
    with_b = world({"alpha": Share(6, 8), "bravo": Share(2, 4)})
    without_b = world({"alpha": Share(6, 8)})

    assert verdicts(run(with_b, *[push("alpha")] * 9)) == ["ok"] * 6 + ["reserved_for_others"] * 3
    assert verdicts(run(without_b, *[push("alpha")] * 9)) == ["ok"] * 8 + ["project_cap"]


def test_internal_usage_counts_against_the_window_but_holds_no_guarantee():
    """`_hub`、`_import` 名下的用量只算进总已用：这里 `_import` 用掉 1 条，A 少一条可用。"""
    state = world({"alpha": Share(6, 8)}, others=1)

    results = run(state, *[push("alpha")] * 8)

    assert verdicts(results) == ["ok"] * 7 + ["reserved_for_others"]


def test_what_is_kept_for_others_is_never_more_than_what_is_left():
    """窗口只剩 1 条、回复保留还差 2 条：留给回复和其他项目的是这 1 条，不是 2 条。"""
    state = world({"alpha": Share(6, 8, used=5)}, others=4)  # 已用 9

    assert decide(state, push("alpha"), CFG, NOW) == Deny(
        "reserved_for_others", n=1, free=1, reserved=1
    )


def test_what_is_kept_for_others_counts_only_the_own_guarantee_when_the_pool_is_negative():
    """回复把保留用超了，别的项目的保底又占着剩下的条数，共享池是负数、按 0 算：
    A 只能用自己保底里剩下的 3 条，窗口剩下的 6 条里其余 3 条是留给别人的。"""
    state = world({"alpha": Share(3, 8), "bravo": Share(5, 8)}, replies=4)  # 剩 6，保底共 8

    assert decide(state, push("alpha", n=4), CFG, NOW) == Deny(
        "reserved_for_others", n=4, free=6, reserved=3
    )
    assert decide(state, push("alpha", n=3), CFG, NOW) == Allow(3)


def test_a_multi_part_message_is_judged_as_one_piece():
    state = world({"alpha": Share(6, 8), "bravo": Share(2, 4)})

    assert decide(state, push("alpha", n=6), CFG, NOW) == Allow(6)
    assert decide(state, push("alpha", n=7), CFG, NOW).code == "reserved_for_others"
    assert decide(state, push("alpha", n=9), CFG, NOW).code == "project_cap"
    assert decide(state, push("alpha", n=11), CFG, NOW).code == "quota_insufficient"


def test_the_guarantee_covers_a_part_of_a_message_and_the_pool_the_rest():
    state = world({"alpha": Share(4, 8, used=3), "bravo": Share(2, 4)})  # 池 = 7 - 2 - 1 - 2 = 2

    assert decide(state, push("alpha", n=3), CFG, NOW) == Allow(3)  # 保底剩 1 + 池 2
    assert decide(state, push("alpha", n=4), CFG, NOW).code == "reserved_for_others"


def test_a_project_with_no_guarantee_lives_on_the_shared_pool_alone():
    state = world({"alpha": Share(4, 8), "cron-job": Share(0, 3)})  # 池 = 10 - 2 - 4 = 4

    results = run(state, *[push("cron-job")] * 4)

    assert verdicts(results) == ["ok", "ok", "ok", "project_cap"]
    assert decide(state, push("cron-job", n=3), CFG, NOW) == Allow(3)


def test_a_project_with_cap_zero_can_never_push():
    state = world({"alpha": Share(4, 8), "mute": Share(0, 0)})

    assert decide(state, push("mute"), CFG, NOW) == Deny("project_cap", n=1, free=10, cap=0, used=0)
    assert decide(state, push("mute", priority="urgent"), CFG, NOW) == Allow(
        1
    )  # 紧急不受自己的上限


# ---------------------------------------------------------------- 腾讯拒绝后的暂停

HELD = open_window(blocked_until_ms=NOW + 20 * MINUTE, blocked_code="40001")


def test_a_push_is_refused_while_the_pause_lasts_and_the_reply_is_not():
    state = world({"alpha": Share(6, 8)}, window=HELD)

    assert decide(state, push("alpha"), CFG, NOW) == Deny(
        "upstream_blocked", n=1, hold_code="40001", retry_in_ms=20 * MINUTE
    )
    assert decide(state, reply(), CFG, NOW) == Allow(1)


def test_the_pause_comes_before_the_quota():
    state = world({"alpha": Share(6, 8, used=8)}, replies=2, window=HELD)

    assert decide(state, push("alpha"), CFG, NOW).code == "upstream_blocked"


def test_the_pause_holds_back_urgent_and_deferrable_too():
    state = world({"alpha": Share(6, 8)}, window=HELD)

    for priority in ("urgent", "deferrable"):
        assert decide(state, push("alpha", priority=priority), CFG, NOW).code == "upstream_blocked"


def test_the_pause_ends_at_the_deadline_and_the_next_push_is_the_probe():
    window = open_window(blocked_until_ms=NOW, blocked_code="40001")
    state = world({"alpha": Share(6, 8)}, window=window)

    assert decide(state, push("alpha"), CFG, NOW - 1).code == "upstream_blocked"
    assert decide(state, push("alpha"), CFG, NOW) == Allow(1, probe=True)
    assert decide(state, push("alpha"), CFG, NOW + HOUR) == Allow(1, probe=True)


def test_a_reply_is_never_a_probe():
    window = open_window(blocked_until_ms=NOW - MINUTE, blocked_code="40001")

    assert decide(world({}, window=window), reply(), CFG, NOW) == Allow(1)


def test_a_push_with_no_pause_is_not_a_probe():
    assert decide(world({"alpha": Share(6, 8)}), push("alpha"), CFG, NOW) == Allow(1, probe=False)


def test_a_probe_is_only_a_probe_if_it_is_let_through():
    window = open_window(blocked_until_ms=NOW - MINUTE, blocked_code="40001")
    state = world({"alpha": Share(6, 8, used=8)}, replies=2, window=window)

    assert decide(state, push("alpha"), CFG, NOW) == Deny("quota_exhausted", n=1, free=0)


def test_the_retry_time_is_how_long_the_pause_still_lasts():
    window = open_window(blocked_until_ms=NOW + 90_500, blocked_code="HTTP 403")
    state = world({"alpha": Share(6, 8)}, window=window)

    denied = decide(state, push("alpha"), CFG, NOW)

    assert (denied.hold_code, denied.retry_in_ms) == ("HTTP 403", 90_500)


# ---------------------------------------------------------------- 还能发几条


def test_left_for_a_fresh_window():
    state = world({"alpha": Share(6, 8), "bravo": Share(2, 4)})

    assert left(state, "alpha", CFG, NOW) == 6
    assert left(state, "bravo", CFG, NOW) == 2


def test_left_is_zero_once_the_guarantee_is_used_and_no_pool_remains():
    state = world({"alpha": Share(6, 8, used=6), "bravo": Share(2, 4)})

    assert left(state, "alpha", CFG, NOW) == 0
    assert left(state, "bravo", CFG, NOW) == 2


def test_the_status_example_with_a_neighbour_left_is_three():
    """L=10、R=2；调用方保底 6、上限 8、已推 3、回复 1；另一个启用的项目保底 2、一条没用。
    min(6-1, 8-3, 3+0) = 3。"""
    state = world({"alpha": Share(6, 8, used=3), "bravo": Share(2, 4)}, replies=1)

    assert left(state, "alpha", CFG, NOW) == 3


def test_the_status_example_alone_is_five():
    """只有这一个项目时共享池是 2：min(6-1, 8-3, 3+2) = 5。"""
    state = world({"alpha": Share(6, 8, used=3)}, replies=1)

    assert left(state, "alpha", CFG, NOW) == 5


@pytest.mark.parametrize(
    ("what", "state"),
    [
        ("没有窗口", State(window=None, shares={"alpha": Share(6, 8)})),
        ("窗口过期", world({"alpha": Share(6, 8)}, window=open_window(started_ms=NOW - 24 * HOUR))),
        ("暂停期内", world({"alpha": Share(6, 8)}, window=HELD)),
    ],
)
def test_left_is_zero_when_nothing_can_go_out(what, state):
    assert left(state, "alpha", CFG, NOW) == 0, what


def test_left_is_zero_when_the_login_is_not_ok():
    assert left(world({"alpha": Share(6, 8)}), "alpha", CFG, NOW, login_ok=False) == 0


def test_left_is_back_when_the_pause_has_run_out_because_a_probe_would_go_through():
    window = open_window(blocked_until_ms=NOW, blocked_code="40001")

    assert left(world({"alpha": Share(6, 8)}, window=window), "alpha", CFG, NOW) == 8


def test_a_negative_shared_pool_does_not_eat_into_a_guarantee():
    """回复已超过保留，窗口里剩下的条数兑现不了所有保底（共享池是负数）：自己的保底仍然算数。"""
    state = world({"alpha": Share(6, 8), "bravo": Share(2, 4)}, replies=3)  # 剩 7，保底共 8

    assert left(state, "alpha", CFG, NOW) == 6
    assert left(state, "bravo", CFG, NOW) == 2
    assert decide(state, push("alpha", n=6), CFG, NOW) == Allow(6)
    assert decide(state, push("alpha", n=7), CFG, NOW).code == "reserved_for_others"


def test_left_is_zero_for_a_project_that_is_not_enabled():
    assert left(world({"alpha": Share(6, 8)}), "ghost", CFG, NOW) == 0


def test_left_never_goes_below_zero():
    # 总已用超过总条数（换窗口交界处的补记），共享池是负数
    state = dataclasses.replace(
        world({"alpha": Share(6, 8, used=6), "bravo": Share(2, 4)}), used=13
    )

    assert left(state, "alpha", CFG, NOW) == 0
    assert left(state, "bravo", CFG, NOW) == 0


def test_left_is_limited_by_the_cap_by_the_window_and_by_the_reserve():
    assert left(world({"alpha": Share(1, 2)}), "alpha", CFG, NOW) == 2  # 上限
    assert (
        left(world({"alpha": Share(6, 8)}, replies=0, others=5), "alpha", CFG, NOW) == 3
    )  # 剩 5，留 2
    assert (
        left(world({"alpha": Share(6, 8)}, replies=2, others=4), "alpha", CFG, NOW) == 4
    )  # 保留已用掉


def test_left_agrees_with_what_decide_lets_through():
    """`left` 条正好放行，再多一条不放行（normal 的推送）。"""
    shapes = [
        world({"alpha": Share(6, 8), "bravo": Share(2, 4)}),
        world({"alpha": Share(4, 8, used=2), "bravo": Share(2, 4, used=1)}, replies=1),
        world({"alpha": Share(3, 3, used=1), "bravo": Share(0, 2)}, others=2),
        world({"alpha": Share(6, 8, used=6)}, replies=2),
        world({"alpha": Share(5, 9, used=5), "bravo": Share(5, 5)}, replies=1, others=1),
    ]
    for state in shapes:
        for name in state.shares:
            n = left(state, name, CFG, NOW)
            if n:
                assert decide(state, push(name, n=n), CFG, NOW) == Allow(n), (name, state)
            assert isinstance(decide(state, push(name, n=n + 1), CFG, NOW), Deny), (name, state)


# ---------------------------------------------------------------- 请求本身要合规


@pytest.mark.parametrize("n", [0, -1, 1.5, True, "1"])
def test_a_request_asks_for_a_whole_number_of_messages_from_one_up(n):
    with pytest.raises(ValueError, match="条数"):
        Request("alpha", n=n)


def test_kind_and_priority_must_be_known():
    with pytest.raises(ValueError, match="种类"):
        Request("alpha", kind="broadcast")
    with pytest.raises(ValueError, match="优先级"):
        Request("alpha", priority="whenever")


def test_a_push_from_a_project_that_is_not_enabled_is_a_programming_error():
    with pytest.raises(ValueError, match="alpha"):
        decide(world({"bravo": Share(2, 4)}), push("alpha"), CFG, NOW)
