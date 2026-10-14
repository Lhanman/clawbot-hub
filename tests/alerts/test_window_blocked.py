"""推送没发出去的汇总告警 `window_blocked`（规格 `hub-alerts`「哪些事要告警」「去重要记下来」）。

不在被拒绝的那一刻发，由 30 秒一轮的定时任务汇总：素材就是发送记录里被这三种原因拒绝的推送
（`window_closed`、`never_contacted`、`quota_exhausted`；`on_closed: skip` 的不算）。

- N 是涉及的**不同项目**数；
- 一个窗口（没有窗口时用固定的 `none`）只告警一次；
- 两条同类告警之间不少于 `alerts.blocked.min_interval_hours`（默认 12）小时；被间隔压住的拒绝
  不丢，累计到间隔一满的下一轮；
- 使用者发话开出新窗口时，`none` 这一条清掉，以后（比如重新绑定账号之后）可以再告警。
"""

from __future__ import annotations

import dataclasses

import pytest

from clawbot_hub import alerts
from clawbot_hub.config import BlockedConfig, Config
from clawbot_hub.quota import NONE_WINDOW_KEY
from clawbot_hub.sender import Origin
from clawbot_hub.upstream.base import Rejected
from tests.sender.conftest import long_text
from tests.support.hub import tweak

TITLE = "ClawBot 共用服务 · 微信消息没发出去"
HOUR = 3600
HUB = Origin("_hub", "服务")


def setup_projects(rig, *names: str):
    """绑定、使用者说一句话、登记项目（`names` 默认 alpha、beta）。"""
    rig.hub.bind()
    rig.hub.speak()
    return [rig.hub.add(name, guaranteed=3, cap=4) for name in (names or ("alpha", "beta"))]


def expire_window(rig):
    rig.clock.advance(25 * HOUR)


def exhaust_window(rig):
    rig.hub.fill(rig.config.quota.window_limit)


def count_text(projects: int) -> str:
    return alerts.window_blocked_text(projects)


def markers(rig) -> set[str]:
    return {key for key in rig.states() if key.startswith("window_blocked:")}


# ---------------------------------------------------------------- 汇总


def test_two_projects_blocked_by_an_expired_window_make_one_alert(rig):
    alpha, beta = setup_projects(rig)
    expire_window(rig)

    assert rig.hub.sender.push(alpha, "你好").status == "rejected"
    assert rig.hub.sender.push(beta, "你好").status == "rejected"
    assert rig.alerts.check_window_blocked()

    assert rig.pending() == {alerts.WINDOW_BLOCKED: count_text(2)}
    rig.deliver()
    assert rig.bark.titles == [TITLE]


def test_a_push_that_was_skipped_does_not_count(rig):
    alpha, beta = setup_projects(rig)
    expire_window(rig)
    rig.hub.sender.push(alpha, "你好")
    rig.hub.sender.push(beta, "你好")
    skipped = rig.hub.sender.push(alpha, "没关系的话", on_closed="skip")
    assert skipped.status == "skipped"

    rig.alerts.check_window_blocked()

    assert rig.pending() == {alerts.WINDOW_BLOCKED: count_text(2)}  # A、B 被拒，A 另有一条 skip


def test_a_skipped_push_alone_does_not_alert(rig):
    alpha, _ = setup_projects(rig)
    expire_window(rig)
    rig.hub.sender.push(alpha, "没关系的话", on_closed="skip")

    assert not rig.alerts.check_window_blocked()
    assert rig.pending() == {}


def test_the_count_is_of_projects_not_of_rejections(rig):
    alpha, beta = setup_projects(rig)
    expire_window(rig)
    for _ in range(3):
        rig.hub.sender.push(alpha, "又一条")
    rig.hub.sender.push(beta, "一条")

    rig.alerts.check_window_blocked()

    assert rig.pending() == {alerts.WINDOW_BLOCKED: count_text(2)}


def test_one_project_makes_a_count_of_one(rig):
    alpha, _ = setup_projects(rig)
    expire_window(rig)
    rig.hub.sender.push(alpha, "你好")

    rig.alerts.check_window_blocked()

    assert rig.pending() == {alerts.WINDOW_BLOCKED: count_text(1)}


def test_nothing_to_report_means_nothing_is_sent(rig):
    setup_projects(rig)

    assert not rig.alerts.check_window_blocked()
    assert rig.pending() == {}


@pytest.mark.parametrize("reason", ["window_closed", "never_contacted", "quota_exhausted"])
def test_the_three_reasons_are_the_ones_that_count(make_rig, reason):
    rig = make_rig()
    rig.hub.bind()
    if reason != "never_contacted":
        rig.hub.speak()
    alpha = rig.hub.add("alpha", guaranteed=3, cap=4)
    if reason == "window_closed":
        expire_window(rig)
    if reason == "quota_exhausted":
        exhaust_window(rig)

    result = rig.hub.sender.push(alpha, "你好")
    assert (result.status, result.reason_code) == ("rejected", reason)

    assert rig.alerts.check_window_blocked()


def test_other_refusals_do_not_count(make_rig):
    """窗口还开着、额度没用完时的各种拒绝（太长、超上限、剩下的不够、可延后的先不发、留给别人）
    都不是"微信消息没发出去"。场面：alpha 和 beta 各发满 4 条，窗口里剩 2 条，正好是回复保留的。"""
    rig = make_rig(tweak(Config(), send={"max_chars": 30000}))  # 放宽字数，才会走到"条数太多"
    alpha, beta = setup_projects(rig)
    gamma = rig.hub.add("gamma", guaranteed=0, cap=4)
    for project in (alpha, beta):
        for _ in range(4):
            assert rig.hub.sender.push(project, "发出去了").status == "sent"
    sender = rig.hub.sender

    refused = {
        "too_long": sender.push(gamma, long_text(9)),
        "project_cap": sender.push(alpha, "超上限"),
        "quota_insufficient": sender.push(gamma, long_text(3)),
        "deferrable_denied": sender.push(gamma, "可以晚点", priority="deferrable"),
        "reserved_for_others": sender.push(gamma, "留给回复的"),
    }

    for expected, result in refused.items():
        assert (result.status, result.reason_code) == ("rejected", expected)
        assert expected not in alerts.BLOCKED_REASONS
    assert not rig.alerts.check_window_blocked()
    assert rig.pending() == {}


def test_a_pause_after_a_refusal_by_tencent_does_not_count(rig):
    alpha, _ = setup_projects(rig)
    rig.hub.upstream.script("send_text", Rejected(200, 5, ""))
    rig.hub.sender.push(alpha, "第一条，被腾讯拒绝")
    second = rig.hub.sender.push(alpha, "暂停期间")
    assert second.reason_code == "upstream_blocked"

    assert not rig.alerts.check_window_blocked()


def test_replies_do_not_count(rig):
    setup_projects(rig)
    expire_window(rig)

    result = rig.hub.sender.reply(HUB, "回复", context_token="ctx-1")  # 窗口过期，回复也发不了

    assert (result.status, result.reason_code) == ("rejected", "window_closed")
    assert not rig.alerts.check_window_blocked()  # 但那不是"推送没发出去"


# ---------------------------------------------------------------- 一个窗口一次


def test_the_same_window_is_alerted_once(rig):
    alpha, beta = setup_projects(rig)
    expire_window(rig)
    rig.hub.sender.push(alpha, "你好")
    rig.alerts.check_window_blocked()
    rig.deliver()
    rig.bark.calls.clear()

    rig.clock.advance(13 * HOUR)  # 使用者一直没说话；项目 D 的推送也因为同一个窗口过期被拒绝
    rig.hub.sender.push(beta, "你好")

    assert not rig.alerts.check_window_blocked()
    rig.deliver()
    assert rig.bark.calls == []
    assert rig.pending() == {}


def test_a_window_is_marked_when_it_is_alerted(rig):
    alpha, _ = setup_projects(rig)
    window = rig.hub.quota.current_window(rig.db.conn())
    expire_window(rig)
    rig.hub.sender.push(alpha, "你好")

    rig.alerts.check_window_blocked()

    assert markers(rig) == {f"window_blocked:{window.id}"}


# ---------------------------------------------------------------- 12 小时间隔


def test_a_new_window_still_waits_for_the_interval_and_the_rejection_is_kept(rig):
    alpha, beta = setup_projects(rig)
    expire_window(rig)
    rig.hub.sender.push(alpha, "你好")
    rig.alerts.check_window_blocked()  # 时刻 T：发出
    rig.deliver()
    rig.bark.calls.clear()
    t = rig.clock.now_ms()

    rig.clock.advance(5 * HOUR)  # T+5 小时：使用者发话开了新窗口，额度很快用完，A 被拒
    rig.hub.speak()
    exhaust_window(rig)
    assert rig.hub.sender.push(alpha, "又被拒").reason_code == "quota_exhausted"

    assert not rig.alerts.check_window_blocked()  # 距上一条不足 12 小时
    assert rig.pending() == {}

    rig.clock.advance(7 * HOUR - 30)  # T+12 小时少 30 秒
    assert not rig.alerts.check_window_blocked()

    rig.clock.advance(30)  # T+12 小时整：间隔一满，下一轮就发
    assert rig.clock.now_ms() == t + 12 * HOUR * 1000
    assert rig.alerts.check_window_blocked()
    assert rig.pending() == {alerts.WINDOW_BLOCKED: count_text(1)}  # N 是 1（项目 A）
    rig.deliver()
    assert len(rig.bark.calls) == 1


def test_the_interval_runs_from_the_last_alert_not_from_the_last_rejection(rig):
    alpha, beta = setup_projects(rig)
    expire_window(rig)
    rig.hub.sender.push(alpha, "你好")
    rig.alerts.check_window_blocked()
    rig.deliver()
    rig.bark.calls.clear()

    rig.clock.advance(5 * HOUR)
    rig.hub.speak()
    exhaust_window(rig)
    rig.hub.sender.push(alpha, "T+5h 被拒")
    rig.alerts.check_window_blocked()
    rig.clock.advance(7 * HOUR)
    assert rig.alerts.check_window_blocked()  # T+12h 发出，N=1（项目 A）
    rig.deliver()
    rig.bark.calls.clear()

    rig.clock.advance(1 * HOUR)  # T+13h：使用者又发话开了第三个窗口，额度又用完，B 被拒
    rig.hub.speak()
    exhaust_window(rig)
    assert rig.hub.sender.push(beta, "T+13h 被拒").reason_code == "quota_exhausted"
    assert not rig.alerts.check_window_blocked()  # 距刚发的才 1 小时

    rig.clock.advance(11 * HOUR)  # T+24h 的第一轮
    assert rig.alerts.check_window_blocked()
    assert rig.pending() == {alerts.WINDOW_BLOCKED: count_text(1)}  # N 是 1（项目 B）


def test_the_interval_setting_is_used(make_rig):
    rig = make_rig(tweak(Config(), alerts={"blocked": _blocked(min_interval_hours=2)}))
    alpha, beta = setup_projects(rig)
    expire_window(rig)
    rig.hub.sender.push(alpha, "你好")
    rig.alerts.check_window_blocked()
    rig.deliver()
    rig.hub.speak()
    exhaust_window(rig)
    rig.hub.sender.push(beta, "新窗口里被拒")

    rig.clock.advance(1 * HOUR)
    assert not rig.alerts.check_window_blocked()
    rig.clock.advance(1 * HOUR)
    assert rig.alerts.check_window_blocked()


def test_an_alert_still_waiting_to_go_out_counts_for_the_interval(rig):
    alpha, beta = setup_projects(rig)
    expire_window(rig)
    rig.hub.sender.push(alpha, "你好")
    rig.alerts.check_window_blocked()  # 登记了，但 Bark 不通，还没发出去
    assert alerts.WINDOW_BLOCKED in rig.pending()

    rig.clock.advance(5 * HOUR)
    rig.hub.speak()
    exhaust_window(rig)
    rig.hub.sender.push(beta, "新窗口里被拒")

    assert not rig.alerts.check_window_blocked()


def test_nothing_is_counted_twice(rig):
    alpha, beta = setup_projects(rig)
    expire_window(rig)
    rig.hub.sender.push(alpha, "你好")
    rig.alerts.check_window_blocked()
    rig.deliver()

    rig.clock.advance(13 * HOUR)
    rig.hub.speak()
    exhaust_window(rig)
    rig.hub.sender.push(beta, "新窗口里被拒")
    rig.alerts.check_window_blocked()

    assert rig.pending() == {alerts.WINDOW_BLOCKED: count_text(1)}  # A 的那一条上次已经报过了


def test_a_restart_does_not_alert_again(rig):
    alpha, _ = setup_projects(rig)
    expire_window(rig)
    rig.hub.sender.push(alpha, "你好")
    rig.alerts.check_window_blocked()
    rig.deliver()
    rig.bark.calls.clear()

    rig.restart()
    rig.clock.advance(13 * HOUR)

    assert not rig.alerts.check_window_blocked()
    rig.deliver()
    assert rig.bark.calls == []


# ---------------------------------------------------------------- 从没收到过消息


def test_never_contacted_is_alerted_once_for_all_projects(rig):
    rig.hub.bind()
    alpha = rig.hub.add("alpha", guaranteed=3, cap=4)
    beta = rig.hub.add("beta", guaranteed=3, cap=4)
    assert rig.hub.sender.push(alpha, "你好").reason_code == "never_contacted"
    assert rig.alerts.check_window_blocked()
    rig.deliver()
    assert markers(rig) == {NONE_WINDOW_KEY}

    rig.clock.advance(600)  # 10 分钟后另一个项目也被拒绝
    assert rig.hub.sender.push(beta, "你好").reason_code == "never_contacted"

    assert not rig.alerts.check_window_blocked()
    rig.deliver()
    assert len(rig.bark.calls) == 1


def test_speaking_clears_the_marker_for_no_window(rig):
    rig.hub.bind()
    alpha = rig.hub.add("alpha", guaranteed=3, cap=4)
    rig.hub.sender.push(alpha, "你好")
    rig.alerts.check_window_blocked()
    assert rig.state(NONE_WINDOW_KEY) is not None

    rig.hub.speak()  # 使用者发话开出窗口：「没收到过消息」那一段结束了

    assert rig.state(NONE_WINDOW_KEY) is None


def test_speaking_does_not_bring_the_old_never_contacted_rejections_back(rig):
    rig.hub.bind()
    alpha = rig.hub.add("alpha", guaranteed=3, cap=4)
    rig.hub.sender.push(alpha, "你好")  # 从没收到过消息：这一条已经报过了
    assert rig.alerts.check_window_blocked()
    rig.deliver()
    rig.bark.calls.clear()

    rig.hub.speak()  # 使用者发话：「没有窗口」的标记清掉
    rig.clock.advance(13 * HOUR)  # 间隔也满了

    assert not rig.alerts.check_window_blocked()  # 旧的那条被标记盖住过，以后不再被数到
    assert rig.pending() == {}


def test_rejections_covered_by_the_marker_stay_covered_after_it_is_cleared(rig):
    rig.hub.bind()
    alpha = rig.hub.add("alpha", guaranteed=3, cap=4)
    beta = rig.hub.add("beta", guaranteed=3, cap=4)
    rig.hub.sender.push(alpha, "你好")
    assert rig.alerts.check_window_blocked()
    rig.deliver()
    rig.clock.advance(600)
    rig.hub.sender.push(beta, "你好")  # 同一段「没收到过消息」，已经报过了
    assert not rig.alerts.check_window_blocked()

    rig.hub.speak()  # 标记清掉
    rig.clock.advance(13 * HOUR)  # 间隔也满了

    assert not rig.alerts.check_window_blocked()  # 刚才被盖住的那条不再冒出来
    assert rig.pending() == {}


def test_rejections_that_vanish_before_the_transaction_are_not_an_error(rig, monkeypatch):
    """只读检查看到了、进事务时没了（被别的进程清掉之类）：什么都不做，不出错。"""
    rig.hub.bind()
    alpha = rig.hub.add("alpha", guaranteed=3, cap=4)
    rig.hub.sender.push(alpha, "你好")
    real = alerts.Alerts._rejections
    reads = []

    def vanishing(conn, cursor):
        reads.append(cursor)
        return real(conn, cursor) if len(reads) == 1 else []

    monkeypatch.setattr(alerts.Alerts, "_rejections", staticmethod(vanishing))

    assert not rig.alerts.check_window_blocked()

    assert len(reads) == 2
    assert rig.pending() == {}


def test_after_rebinding_never_contacted_can_alert_again(rig):
    rig.hub.bind()
    alpha = rig.hub.add("alpha", guaranteed=3, cap=4)
    rig.hub.sender.push(alpha, "你好")  # 从没收到过消息：告警一次
    rig.alerts.check_window_blocked()
    rig.deliver()
    rig.bark.calls.clear()

    rig.clock.advance(1 * HOUR)
    rig.hub.speak()  # 使用者后来发话开了窗口
    rig.clock.advance(13 * HOUR)
    rig.hub.bind(bot_token="tok-bot-secret-2")  # 又过了 13 小时 hubctl login 重新绑定：窗口作废
    assert rig.hub.sender.push(alpha, "你好").reason_code == "never_contacted"

    assert rig.alerts.check_window_blocked()
    rig.deliver()
    assert rig.bark.titles == [TITLE]


def test_no_window_inside_the_interval_still_waits(rig):
    rig.hub.bind()
    alpha = rig.hub.add("alpha", guaranteed=3, cap=4)
    rig.hub.sender.push(alpha, "你好")
    rig.alerts.check_window_blocked()
    rig.deliver()
    rig.clock.advance(1 * HOUR)
    rig.hub.speak()
    rig.clock.advance(5 * HOUR)
    rig.hub.bind(bot_token="tok-bot-secret-2")  # 重新绑定：窗口作废
    rig.hub.sender.push(alpha, "你好")

    assert not rig.alerts.check_window_blocked()  # 距上一条才 6 小时，被间隔压住（但留着）

    rig.clock.advance(7 * HOUR)
    assert rig.alerts.check_window_blocked()


def _blocked(**changes) -> BlockedConfig:
    return dataclasses.replace(BlockedConfig(), **changes)
