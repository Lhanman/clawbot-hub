"""登录失效、登录已恢复两种告警（规格 `hub-alerts`「哪些事要告警」「去重要记下来」）。

去重的单位是"一次失效"（从失效开始到恢复）：同一次失效只发一次；"已恢复"只在发过"失效"之后发；
重启不重复；再次失效又能发。
"""

from __future__ import annotations

from clawbot_hub import alerts
from clawbot_hub.upstream.base import PollFailed, Updates

EXPIRED_TITLE = "ClawBot 共用服务 · 登录失效"
RESTORED_TITLE = "ClawBot 共用服务 · 登录已恢复"


def test_expiry_registers_one_alert(rig):
    rig.hub.bind()

    rig.hub.expire()

    assert rig.pending() == {alerts.LOGIN_EXPIRED: alerts.login_expired_text()}
    assert rig.state(alerts.LOGIN_EXPIRED)["episode"] == "1"


def test_the_alert_goes_out_through_bark(rig):
    rig.hub.bind()
    rig.hub.expire()

    rig.deliver()

    assert rig.bark.titles == [EXPIRED_TITLE]
    assert rig.bark.texts == [alerts.login_expired_text()]
    assert rig.pending() == {}


def test_probing_three_times_still_gives_one_alert(rig):
    rig.hub.bind()
    rig.hub.expire()
    for _ in range(3):  # 探测 3 次仍是 -14：监视器不再叫回调（已经是失效状态）
        rig.hub.monitor.session_expired(rig.hub.monitor.account().digest)
        rig.hub.monitor.probe_finished(PollFailed("连不上", completed=False))

    rig.deliver()

    assert rig.bark.titles == [EXPIRED_TITLE]


def test_a_probe_that_works_sends_the_restored_alert(rig):
    rig.hub.bind()
    rig.hub.expire()
    rig.deliver()

    assert rig.hub.monitor.probe_finished(Updates())
    rig.deliver()

    assert rig.bark.titles == [EXPIRED_TITLE, RESTORED_TITLE]
    assert rig.state(alerts.LOGIN_RESTORED)["episode"] == "1"


def test_new_credentials_also_send_the_restored_alert(rig):
    rig.hub.bind()
    rig.hub.expire()
    rig.deliver()

    rig.hub.bind(bot_token="tok-bot-secret-2")  # 重新扫码：摘要变了
    rig.deliver()

    assert rig.bark.titles == [EXPIRED_TITLE, RESTORED_TITLE]


def test_a_brand_new_account_has_no_restored_alert(rig):
    rig.hub.bind()

    assert rig.states() == {}
    assert rig.hub.restored == []


def test_no_restored_alert_when_no_expired_alert_was_sent_for_that_failure(rig):
    rig.hub.bind()

    rig.alerts.on_restored(1)  # 没发过失效告警，就不发恢复

    assert rig.pending() == {}


def test_failing_again_alerts_again(rig):
    rig.hub.bind()
    rig.hub.expire()
    rig.deliver()
    rig.hub.monitor.probe_finished(Updates())
    rig.deliver()

    rig.hub.expire()  # 第二次失效
    rig.deliver()

    assert rig.bark.titles == [EXPIRED_TITLE, RESTORED_TITLE, EXPIRED_TITLE]
    assert rig.state(alerts.LOGIN_EXPIRED)["episode"] == "2"


def test_the_second_failure_gets_its_own_restored_alert(rig):
    rig.hub.bind()
    rig.hub.expire()
    rig.hub.monitor.probe_finished(Updates())
    rig.hub.expire()
    rig.deliver()
    rig.bark.calls.clear()

    rig.hub.monitor.probe_finished(Updates())
    rig.deliver()

    assert rig.bark.titles == [RESTORED_TITLE]
    assert rig.state(alerts.LOGIN_RESTORED)["episode"] == "2"


def test_a_restart_does_not_repeat_the_alert(rig):
    rig.hub.bind()
    rig.hub.expire()
    rig.deliver()
    rig.bark.calls.clear()

    new = rig.restart()
    new.on_expired(1)  # 重启后登录仍是失效；就算回调又来一次，同一次失效也不再发
    rig.deliver()

    assert rig.bark.calls == []
    assert rig.pending() == {}


def test_the_restored_alert_is_sent_once_per_failure(rig):
    rig.hub.bind()
    rig.hub.expire()
    rig.hub.monitor.probe_finished(Updates())
    rig.deliver()
    rig.bark.calls.clear()

    rig.alerts.on_restored(1)
    rig.deliver()

    assert rig.bark.calls == []


def test_an_alert_still_waiting_to_go_out_is_not_registered_twice(rig):
    rig.hub.bind()
    rig.hub.expire()
    first = rig.state(alerts.LOGIN_EXPIRED)["pending_since_ms"]
    rig.clock.advance(60)

    rig.alerts.on_expired(1)

    assert rig.state(alerts.LOGIN_EXPIRED)["pending_since_ms"] == first


def test_the_decision_and_the_pending_text_are_one_write(rig):
    """去重状态和待发文字在同一个事务里：看得到去重记录的时候一定也看得到待发的文字。"""
    rig.hub.bind()
    rig.hub.expire()

    row = rig.state(alerts.LOGIN_EXPIRED)

    assert row["episode"] == "1"
    assert row["pending_text"] == alerts.login_expired_text()
    assert row["pending_since_ms"] == rig.clock.now_ms()
    assert row["attempts"] == 0
