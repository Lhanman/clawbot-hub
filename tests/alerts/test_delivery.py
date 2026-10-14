"""告警怎么送出去：独立的投递线程、Bark 出问题时的重试和待发队列、设备码从哪里读、
重启和被杀时不丢、不泄密。

一条告警被判定要发的时候，只是在告警状态表里登记（待发文字 `pending_text` 和去重状态在同一个
事务里）；投递线程把待发的一条条送出去：

- 第一次送（还没试过）：立刻试，暂时不通的话间隔 1、2、4 秒再试，最多 `alerts.bark_retries`
  （默认 3）次重试，也就是最多 4 次请求；
- 还不行就留在待发队列（在数据库里，重启不丢），每 `alerts.queue_retry_minutes`（默认 5）分钟
  试一次，登记以后超过 `alerts.queue_give_up_minutes`（默认 60）分钟就放弃并写一条事件；
- Bark 明确拒绝（设备码错了之类）：不重试，写一条事件；
- 成功以后才清掉待发文字、记下发出的时刻。判定通过之后、Bark 回话之前被杀，重启后补发（至少一次）。

`deliver()` 是投递线程每一轮做的事：把到期的待发告警都送一遍，返回"离下一次到期还有几秒"
（没有待发的返回 `None`）。测试里的假时钟 `sleep` 立刻返回并让假时间走过去，所以立即重试的
1、2、4 秒会体现在 Bark 替身记下的时刻上。
"""

from __future__ import annotations

import logging
import sqlite3
import time

import pytest

from clawbot_hub import alerts
from clawbot_hub.alerts import Alerts, last_bark_outcome, send_test_alert
from clawbot_hub.bark import ENV_KEY, Delivered, Refused, Unreachable, write_bark_key
from clawbot_hub.config import Config
from clawbot_hub.instance import RunInfo
from clawbot_hub.upstream.base import Accepted, Rejected, Updates
from tests.alerts.conftest import (
    DEVICE,
    fail_connection,
    go_to,
    make_poller,
    ms_of,
    round_trip,
)
from tests.support.hub import BOT_TOKEN, tweak
from tests.support.threads import wait_for

EXPIRED_TITLE = "ClawBot 共用服务 · 登录失效"
RESTARTED_TITLE = "ClawBot 共用服务 · 服务重启"
BACKUP_TITLE = "ClawBot 共用服务 · 备份失败"
TEST_TITLE = "ClawBot 共用服务 · 测试"
MINUTE = 60
UNREACHABLE = Unreachable("HTTP 500")


def register(rig) -> None:
    """登录失效，登记一条待发告警。"""
    rig.hub.bind()
    rig.hub.expire()


def offsets(rig, since_ms: int) -> list[int]:
    """Bark 替身收到每个请求的时刻，离 `since_ms` 多少秒。"""
    return [(call.at_ms - since_ms) // 1000 for call in rig.bark.calls]


def crashed() -> RunInfo:
    return RunInfo(pid=1, last_heartbeat_ms=ms_of("2026-10-12 09:58"), clean_exit=False)


def drain(rig, *, limit: int = 100) -> None:
    """一直送到没有待发的为止（每次睡到下一次到期）。"""
    for _ in range(limit):
        due = rig.deliver()
        if due is None:
            return
        rig.clock.advance(due)
    raise AssertionError("送了很多轮还有待发的告警")


def event_texts(rig, prefix: str) -> list[str]:
    return [e["detail"] for e in rig.events() if e["detail"].startswith(prefix)]


# ---------------------------------------------------------------- 送出去、清掉


def test_a_registered_alert_is_sent_and_cleared(rig):
    register(rig)
    sent_at = rig.clock.now_ms()

    assert rig.deliver() is None

    [call] = rig.bark.calls
    assert call.title == EXPIRED_TITLE
    assert call.text == alerts.login_expired_text()
    assert call.key.device == DEVICE
    row = rig.state(alerts.LOGIN_EXPIRED)
    assert row["pending_text"] is None
    assert row["pending_since_ms"] is None
    assert row["attempts"] == 0
    assert row["last_sent_ms"] == sent_at
    assert event_texts(rig, "告警已送出：")[0].endswith("登录失效")


def test_nothing_pending_means_nothing_to_do(rig):
    assert rig.deliver() is None

    assert rig.bark.calls == []


def test_the_pending_text_is_kept_until_bark_has_answered(rig):
    register(rig)
    seen = {}
    original = rig.bark.send

    def peek(key, title, text):
        seen["row"] = rig.state(alerts.LOGIN_EXPIRED)
        return original(key, title, text)

    rig.bark.send = peek

    rig.deliver()

    assert seen["row"]["pending_text"] == alerts.login_expired_text()  # 请求在路上时还留着
    assert seen["row"]["last_sent_ms"] is None
    assert rig.pending() == {}  # 回话以后清掉


def test_a_replacement_registered_while_sending_is_not_cleared(rig):
    register(rig)
    original = rig.bark.send

    def replace_meanwhile(key, title, text):
        rig.execute(
            "UPDATE alert_state SET pending_text = ? WHERE key = ?",
            "更新过的文字",
            alerts.LOGIN_EXPIRED,
        )
        return original(key, title, text)

    rig.bark.send = replace_meanwhile

    rig.deliver()

    assert rig.pending() == {alerts.LOGIN_EXPIRED: "更新过的文字"}  # 发出去的是旧的，新的还要发


def test_alerts_never_touch_wechat_or_its_quota(rig):
    rig.hub.ready()
    before = (rig.hub.upstream.sent_texts(), rig.hub.used())
    rig.hub.expire()

    rig.alerts.backup_failed("磁盘满了")
    rig.deliver()

    assert rig.bark.titles != []
    assert (rig.hub.upstream.sent_texts(), rig.hub.used()) == before


# ---------------------------------------------------------------- Bark 暂时不通


def test_four_failed_requests_then_the_queue_retry_sends_it_once(rig):
    """前 4 次请求（首次加 3 次立即重试，约 0、1、3、7 秒）回 500，之后恢复：留在待发队列，
    5 分钟后的重试发出，只发一次。"""
    register(rig)
    start = rig.clock.now_ms()
    rig.bark.script(*[UNREACHABLE] * 4)

    due = rig.deliver()

    assert offsets(rig, start) == [0, 1, 3, 7]
    assert rig.pending() == {alerts.LOGIN_EXPIRED: alerts.login_expired_text()}
    assert due == 5 * MINUTE

    rig.clock.advance(5 * MINUTE)
    assert rig.deliver() is None

    assert offsets(rig, start) == [0, 1, 3, 7, 307]
    assert rig.pending() == {}
    rig.clock.advance(3600)
    rig.deliver()
    assert len(rig.bark.calls) == 5  # 不会再发


def test_the_immediate_retries_can_succeed_without_ever_queueing(rig):
    register(rig)
    start = rig.clock.now_ms()
    rig.bark.script(*[UNREACHABLE] * 3)

    assert rig.deliver() is None

    assert offsets(rig, start) == [0, 1, 3, 7]
    assert rig.pending() == {}
    assert event_texts(rig, "告警暂时没发出去") == []
    rig.clock.advance(3600)
    rig.deliver()
    assert len(rig.bark.calls) == 4


@pytest.mark.parametrize(
    ("retries", "expected"), [(0, [0]), (1, [0, 1]), (2, [0, 1, 3]), (5, [0, 1, 3, 7, 15, 31])]
)
def test_the_number_of_immediate_retries_follows_the_configuration(make_rig, retries, expected):
    rig = make_rig(tweak(Config(), alerts={"bark_retries": retries}))
    register(rig)
    start = rig.clock.now_ms()
    rig.bark.default = UNREACHABLE

    rig.deliver()

    assert offsets(rig, start) == expected


def test_the_first_round_that_fails_leaves_one_event_and_no_more_per_retry(rig):
    register(rig)
    rig.bark.default = UNREACHABLE

    drain(rig)

    queued = event_texts(rig, "告警暂时没发出去，留在待发队列：")
    assert len(queued) == 1
    assert queued[0].endswith("登录失效（HTTP 500）")  # 原因写在事件里，方便查
    [event] = [e for e in rig.events() if e["detail"] == queued[0]]
    assert event["level"] == "WARNING"


def test_after_an_hour_of_failures_the_alert_is_given_up(rig):
    """假 Bark 一直回 500，持续 61 分钟：这条告警被放弃，事件里有一条记录，不再重试。"""
    register(rig)
    start = rig.clock.now_ms()
    rig.bark.default = UNREACHABLE

    drain(rig)

    assert offsets(rig, start) == [0, 1, 3, 7] + [307 + 300 * k for k in range(11)]
    assert rig.pending() == {}
    [state] = rig.states().values()
    assert (state["pending_text"], state["pending_since_ms"], state["attempts"]) == (None, None, 0)
    [gave_up] = [e for e in rig.events() if e["detail"].startswith("告警放弃：")]
    assert gave_up["level"] == "ERROR"
    assert "登录失效" in gave_up["detail"]
    calls = len(rig.bark.calls)
    rig.clock.advance(86400)
    assert rig.deliver() is None
    assert len(rig.bark.calls) == calls


def test_the_queue_settings_are_used(make_rig):
    rig = make_rig(tweak(Config(), alerts={"queue_retry_minutes": 1, "queue_give_up_minutes": 3}))
    register(rig)
    start = rig.clock.now_ms()
    rig.bark.default = UNREACHABLE

    drain(rig)

    assert offsets(rig, start) == [0, 1, 3, 7, 67, 127]  # 187 秒到期时已经过了 3 分钟：放弃
    [gave_up] = event_texts(rig, "告警放弃：")
    assert gave_up == "告警放弃：登录失效（超过 3 分钟还没发出去）"


def test_the_sent_time_is_when_it_went_out_not_when_it_was_registered(rig):
    register(rig)
    registered = rig.clock.now_ms()
    rig.bark.script(*[UNREACHABLE] * 4)
    rig.deliver()  # 第一轮没送成，进了队列
    rig.clock.advance(5 * MINUTE)

    rig.deliver()  # 5 分钟后送出去

    assert rig.state(alerts.LOGIN_EXPIRED)["last_sent_ms"] == rig.clock.now_ms()
    assert rig.clock.now_ms() > registered


def test_stopping_during_the_immediate_retries_ends_the_round_and_keeps_the_alert(rig):
    register(rig)
    rig.bark.default = UNREACHABLE
    original = rig.bark.send

    def stopping(key, title, text):
        rig.alerts.stop()  # 第一个请求的时候收到停止
        return original(key, title, text)

    rig.bark.send = stopping

    rig.deliver()

    assert len(rig.bark.calls) == 1  # 不再重试
    assert alerts.LOGIN_EXPIRED in rig.pending()  # 告警还在库里，下次启动接着送


def test_stopping_during_one_alert_leaves_the_rest_for_the_next_start(rig):
    rig.alerts.backup_failed("磁盘满了")
    rig.clock.advance(10)
    rig.alerts.service_restarted(crashed(), 0)
    original = rig.bark.send

    def stopping(key, title, text):
        rig.alerts.stop()  # 送第一条的时候收到停止
        return original(key, title, text)

    rig.bark.send = stopping

    rig.deliver()

    assert rig.bark.titles == [BACKUP_TITLE]  # 第二条不送了
    assert set(rig.pending()) == {alerts.SERVICE_RESTARTED}  # 还在库里


def test_a_new_failure_with_the_same_text_registered_while_sending_is_not_cleared(rig):
    """登录第二次失效，告警的文字和第一次一模一样；第一条正在送的时候登记的第二条不能被误清。"""
    register(rig)
    original = rig.bark.send

    def during_the_request(key, title, text):
        result = original(key, title, text)
        if len(rig.bark.calls) == 1:
            rig.clock.advance(1)
            rig.alerts.on_expired(2)  # 第二次失效
        return result

    rig.bark.send = during_the_request

    rig.deliver()
    assert alerts.LOGIN_EXPIRED in rig.pending()  # 发出去的是第一次的，第二次的还要发

    rig.deliver()
    assert rig.pending() == {}
    assert len(rig.bark.calls) == 2


def test_registering_again_while_one_waits_in_the_queue_starts_a_fresh_round(rig):
    """排队中的告警被同一种的新告警换掉：当新的送，马上来一整轮（首次加立即重试），不等 5 分钟。"""
    register(rig)
    rig.bark.default = UNREACHABLE
    rig.deliver()  # 第一次失效的告警进了队列
    rig.bark.calls.clear()
    rig.clock.advance(MINUTE)

    rig.alerts.on_expired(2)  # 第二次失效
    rig.deliver()

    assert len(rig.bark.calls) == 4
    assert (
        len(event_texts(rig, "告警暂时没发出去，留在待发队列：")) == 2
    )  # 新的第一轮没送成也写一条


def test_an_alert_replaced_during_a_failing_request_is_not_touched_by_the_old_attempt(rig):
    register(rig)
    rig.bark.default = UNREACHABLE
    original = rig.bark.send

    def replaced_meanwhile(key, title, text):
        if not rig.bark.calls:
            rig.clock.advance(1)
            rig.alerts.on_expired(2)  # 第一个请求发出去的时候，来了新的一条
        return original(key, title, text)

    rig.bark.send = replaced_meanwhile

    rig.deliver()

    row = rig.state(alerts.LOGIN_EXPIRED)
    assert row["attempts"] == 0  # 旧的那次没送成，不算在新的头上
    assert event_texts(rig, "告警暂时没发出去，留在待发队列：") == []
    rig.bark.calls.clear()
    assert rig.deliver() == 5 * MINUTE
    assert len(rig.bark.calls) == 4  # 新的一条马上有自己的一整轮


def test_giving_up_never_clears_a_newer_registration(rig):
    """放弃用的是这一条的登记时刻：同一种告警已经换成新的，就不动新的，也不写放弃的事件。"""
    register(rig)
    since = rig.state(alerts.LOGIN_EXPIRED)["pending_since_ms"]
    rig.clock.advance(1)
    rig.alerts.on_expired(2)

    rig.alerts._give_up(alerts.LOGIN_EXPIRED, since)

    assert alerts.LOGIN_EXPIRED in rig.pending()
    assert event_texts(rig, "告警放弃：") == []


def test_the_hour_runs_from_the_registration_not_from_the_last_try(rig):
    register(rig)
    rig.bark.default = UNREACHABLE
    start = rig.clock.now_ms()
    rig.deliver()

    rig.clock.advance(55 * MINUTE)
    assert rig.deliver() is not None  # 登记后 55 分 7 秒：还在试

    rig.clock.advance(5 * MINUTE)
    assert rig.deliver() is None  # 登记后超过 60 分钟：放弃
    assert rig.clock.now_ms() - start > 60 * MINUTE * 1000
    assert rig.pending() == {}


def test_the_hour_is_not_over_at_exactly_sixty_minutes_but_is_a_millisecond_later(rig):
    """登记以后刚好满 60 分钟还试最后一次，多过 1 毫秒才放弃。"""
    register(rig)
    registered = rig.clock.now_ms()
    rig.bark.default = UNREACHABLE
    rig.deliver()
    rig.clock.advance((60 * MINUTE * 1000 - (rig.clock.now_ms() - registered)) / 1000)
    assert rig.clock.now_ms() - registered == 60 * MINUTE * 1000
    calls = len(rig.bark.calls)

    rig.deliver()

    assert len(rig.bark.calls) == calls + 1  # 刚好 60 分钟：还没超过，又试了一次
    assert alerts.LOGIN_EXPIRED in rig.pending()
    assert event_texts(rig, "告警放弃：") == []

    rig.clock.advance(0.001)
    assert rig.deliver() is None

    assert len(rig.bark.calls) == calls + 1  # 多了 1 毫秒：不再请求，放弃
    assert rig.pending() == {}
    assert len(event_texts(rig, "告警放弃：")) == 1


def test_deliver_says_how_long_until_the_next_try(rig):
    register(rig)
    rig.bark.default = UNREACHABLE
    assert rig.deliver() == 300

    rig.clock.advance(100)
    calls = len(rig.bark.calls)
    assert rig.deliver() == 200  # 还没到点：不请求，只告诉还要等多久

    assert len(rig.bark.calls) == calls


def test_an_unexpected_error_in_the_client_is_trouble_not_a_crash(rig, caplog):
    register(rig)
    rig.bark.script(RuntimeError("boom 不该出现在日志里"))

    with caplog.at_level(logging.DEBUG):
        rig.deliver()  # 不抛出来；立即重试那一次送出去了

    assert rig.pending() == {}
    assert len(rig.bark.calls) == 2
    assert "RuntimeError" in caplog.text
    assert "boom" not in caplog.text


def test_an_unexpected_error_shows_its_type_in_the_event_but_never_its_message(rig):
    register(rig)
    rig.bark.script(*[RuntimeError("boom 秘密内容") for _ in range(4)])

    rig.deliver()

    [queued] = event_texts(rig, "告警暂时没发出去，留在待发队列：")
    assert queued.endswith("登录失效（RuntimeError）")
    assert "boom" not in queued and "秘密" not in queued


# ---------------------------------------------------------------- Bark 明确拒绝


def test_a_refused_alert_is_not_retried_and_leaves_an_event(rig):
    register(rig)
    rig.bark.script(Refused("failed to get device token"))

    assert rig.deliver() is None
    assert rig.state(alerts.LOGIN_EXPIRED)["last_sent_ms"] is None  # 没有发出去，不记发出的时刻

    assert len(rig.bark.calls) == 1  # 只请求了 1 次
    assert rig.pending() == {}  # 没有进待发队列
    [refused] = event_texts(rig, "告警被 Bark 拒绝：")
    assert "failed to get device token" in refused
    assert DEVICE not in refused
    event = next(e for e in rig.events() if e["detail"] == refused)
    assert event["level"] == "WARNING"
    rig.clock.advance(86400)
    rig.deliver()
    assert len(rig.bark.calls) == 1


def test_a_refusal_that_echoes_the_device_code_is_masked_in_the_event(rig):
    register(rig)
    rig.bark.script(Refused(f"failed to get device token {DEVICE}"))

    rig.deliver()

    [event] = event_texts(rig, "告警被 Bark 拒绝：")
    assert "failed to get device token" in event
    assert DEVICE not in event


def test_the_last_outcome_follows_the_latest_decisive_answer(rig):
    conn = rig.db.conn()
    assert last_bark_outcome(conn) is None

    rig.alerts.backup_failed("磁盘满了")
    rig.deliver()
    assert last_bark_outcome(conn) == "ok"

    rig.bark.script(Refused("failed to get device token"))
    rig.alerts.service_restarted(crashed(), 0)
    rig.deliver()
    assert last_bark_outcome(conn) == "refused"

    rig.bark.default = UNREACHABLE  # 连不上说明不了设备码对不对：不改变上一次的结论
    register(rig)
    rig.deliver()
    assert last_bark_outcome(conn) == "refused"

    rig.bark.default = Delivered()
    rig.clock.advance(5 * MINUTE)
    rig.deliver()
    assert last_bark_outcome(conn) == "ok"


# ---------------------------------------------------------------- 重启和被杀


def test_a_waiting_alert_goes_out_after_a_restart_exactly_once(rig):
    register(rig)
    rig.bark.default = UNREACHABLE
    rig.deliver()
    assert alerts.LOGIN_EXPIRED in rig.pending()

    rig.restart()  # 服务重启，此时 Bark 已恢复
    rig.bark.default = Delivered()
    rig.bark.calls.clear()
    assert rig.deliver() is None  # 重启后的第一次就送，不用再等 5 分钟

    assert rig.bark.titles == [EXPIRED_TITLE]
    rig.clock.advance(3600)
    rig.deliver()
    assert len(rig.bark.calls) == 1


def test_killed_after_the_decision_before_bark_answered_the_alert_is_sent_after_restart(rig):
    register(rig)  # 判定通过、登记了；服务在 Bark 回话之前被杀，一个请求都没发出去

    rig.restart()
    rig.deliver()

    assert rig.bark.titles == [EXPIRED_TITLE]  # 去重状态里记着这一次失效，但待发的没有丢


def test_the_dedupe_state_does_not_swallow_an_alert_that_never_went_out(rig):
    register(rig)
    rig.restart()
    rig.alerts.on_expired(1)  # 重启后回调又来一次：同一次失效，不再登记，但待发的那条还在

    rig.deliver()

    assert rig.bark.titles == [EXPIRED_TITLE]


# ---------------------------------------------------------------- 设备码从哪里读


def test_without_a_key_the_alert_waits_and_an_event_says_so(make_rig):
    rig = make_rig(env_key=None)
    register(rig)

    assert rig.deliver() == 5 * MINUTE

    assert rig.bark.calls == []
    assert alerts.LOGIN_EXPIRED in rig.pending()
    [event] = event_texts(rig, "告警未送出（没有配置设备码）：")
    assert event.endswith("登录失效")
    rig.clock.advance(5 * MINUTE)
    rig.deliver()
    assert len(event_texts(rig, "告警未送出（没有配置设备码）：")) == 1  # 不是每 5 分钟一条


def test_the_service_runs_on_without_a_key(make_rig):
    rig = make_rig(env_key=None)
    pusher = rig.hub.ready()
    rig.hub.expire()
    rig.deliver()
    rig.hub.bind(bot_token="tok-bot-secret-2")  # 重新扫码：恢复了
    rig.hub.speak()  # 换了账号凭证，窗口作废，使用者再说一句话

    assert rig.hub.sender.push(pusher, "还能发").status == "sent"


def test_a_key_set_later_gets_the_waiting_alert_out(make_rig):
    rig = make_rig(env_key=None)
    register(rig)
    rig.deliver()

    write_bark_key(rig.paths, "LATE-KEY")
    rig.clock.advance(5 * MINUTE)
    assert rig.deliver() is None

    [call] = rig.bark.calls
    assert call.key.device == "LATE-KEY"
    assert rig.pending() == {}


def test_a_malformed_key_file_waits_like_a_missing_one_and_the_event_says_why(make_rig):
    """`bark.json` 手写坏了：告警和没配设备码一样留在待发队列、不丢，事件说明是配置有问题，
    而且事件里不带文件的内容；改好以后下一次试就送出去。"""
    rig = make_rig(env_key=None)
    rig.paths.bark.write_text("这不是 JSON，里面也许有设备码 SECRET-IN-FILE", encoding="utf-8")
    register(rig)

    assert rig.deliver() == 5 * MINUTE

    assert rig.bark.calls == []
    assert alerts.LOGIN_EXPIRED in rig.pending()
    [event] = event_texts(rig, "告警未送出（设备码配置有问题）：")
    assert event.startswith("告警未送出（设备码配置有问题）：登录失效（bark.json 的内容不对")
    assert "SECRET-IN-FILE" not in event
    assert event_texts(rig, "告警未送出（没有配置设备码）：") == []

    write_bark_key(rig.paths, "FIXED-KEY")
    rig.clock.advance(5 * MINUTE)
    assert rig.deliver() is None

    assert [call.key.device for call in rig.bark.calls] == ["FIXED-KEY"]
    assert rig.pending() == {}


def test_a_malformed_environment_value_does_not_fall_back_to_the_file(make_rig):
    """环境变量写坏了：不悄悄换用文件里的设备码，告警留在待发队列，事件点名是环境变量的问题。"""
    rig = make_rig(env_key="://")
    write_bark_key(rig.paths, "FILE-KEY")
    register(rig)

    assert rig.deliver() == 5 * MINUTE

    assert rig.bark.calls == []
    [event] = event_texts(rig, "告警未送出（设备码配置有问题）：")
    assert ENV_KEY in event
    assert "FILE-KEY" not in event


def test_a_missing_key_is_given_up_after_the_hour_like_any_other_failure(make_rig):
    rig = make_rig(env_key=None)
    register(rig)

    drain(rig)

    assert rig.pending() == {}
    assert len(event_texts(rig, "告警放弃：")) == 1


def test_the_file_is_read_before_every_alert(make_rig):
    rig = make_rig(env_key=None)
    write_bark_key(rig.paths, "K1")
    rig.alerts.backup_failed("磁盘满了")
    rig.deliver()

    write_bark_key(rig.paths, "K2")  # 服务运行中换了设备码，不用重启
    rig.alerts.service_restarted(crashed(), 0)
    rig.deliver()

    assert [call.key.device for call in rig.bark.calls] == ["K1", "K2"]


def test_the_environment_wins_and_is_read_at_start(make_rig):
    rig = make_rig(env_key="K2")
    write_bark_key(rig.paths, "K1")
    rig.alerts.backup_failed("磁盘满了")
    rig.deliver()
    assert rig.bark.calls[-1].key.device == "K2"

    rig.env_key = None  # 去掉环境变量重启：用文件里的
    rig.restart()
    rig.alerts.service_restarted(crashed(), 0)
    rig.deliver()

    assert rig.bark.calls[-1].key.device == "K1"


def test_a_wrong_key_then_the_right_one_without_restarting(make_rig, fake_bark):
    """`bark.json` 里是错的设备码（假 Bark 回 400），随后换成对的，下一条告警用新的发出并成功。"""
    fake_bark.valid_keys = {"RIGHT-KEY"}
    rig = make_rig(env_key=None)
    rig.alerts = Alerts(rig.db, rig.config, rig.clock, rig.paths, env={})  # 真的 BarkClient
    write_bark_key(rig.paths, f"{fake_bark.base_url}/WRONG-KEY")
    register(rig)

    rig.deliver()

    assert [push.device_key for push in fake_bark.pushes] == ["WRONG-KEY"]
    assert rig.pending() == {}
    [refused] = event_texts(rig, "告警被 Bark 拒绝：")
    assert "WRONG-KEY" not in refused
    assert last_bark_outcome(rig.db.conn()) == "refused"

    write_bark_key(rig.paths, f"{fake_bark.base_url}/RIGHT-KEY")
    assert rig.hub.monitor.probe_finished(Updates())  # 登录恢复了：又有一条告警
    rig.deliver()

    assert [push.device_key for push in fake_bark.pushes] == ["WRONG-KEY", "RIGHT-KEY"]
    assert fake_bark.pushes[1].title == "ClawBot 共用服务 · 登录已恢复"
    assert fake_bark.pushes[1].group == "clawbot-hub"
    assert last_bark_outcome(rig.db.conn()) == "ok"


def test_the_configured_group_is_used(make_rig, fake_bark):
    rig = make_rig(tweak(Config(), alerts={"group": "别的分组"}), env_key=None)
    rig.alerts = Alerts(rig.db, rig.config, rig.clock, rig.paths, env={})
    write_bark_key(rig.paths, f"{fake_bark.base_url}/{DEVICE}")

    rig.alerts.backup_failed("磁盘满了")
    rig.deliver()

    assert fake_bark.pushes[0].group == "别的分组"


# ---------------------------------------------------------------- 不泄密


SECRET_BODY = "SECRET-BODY-TEXT-不该出现"


def trigger_every_kind(rig) -> list[str]:
    """用真实的触发路线把十种告警都造出来；系统里放着各种秘密，返回这些秘密。"""
    hub = rig.hub
    hub.bind()
    hub.speak()
    alpha = hub.add("alpha", guaranteed=3, cap=4)
    for _ in range(3):  # send_unconfirmed
        hub.upstream.script("send_text", Accepted(None))
        hub.sender.push(alpha, SECRET_BODY)
    hub.upstream.script("send_text", Rejected(200, 5, SECRET_BODY))  # upstream_blocked
    hub.sender.push(alpha, SECRET_BODY)
    hub.expire()  # login_expired、login_restored
    hub.monitor.probe_finished(Updates())
    round_trip(rig)  # upstream_down、upstream_restored
    for _ in range(10):
        rig.clock.advance(MINUTE)
        fail_connection(rig)
        rig.alerts.check_upstream()
    rig.clock.advance(30)
    round_trip(rig)
    rig.alerts.check_upstream()
    go_to(rig.clock, "2026-10-13 08:00")  # window_expiring：周二早上，窗口还剩 2 小时
    rig.alerts.check_window_expiring()
    go_to(rig.clock, "2026-10-13 10:30")  # window_blocked：窗口过期以后项目的推送被拒
    hub.sender.push(alpha, SECRET_BODY)
    rig.alerts.check_window_blocked()
    rig.alerts.service_restarted(crashed(), 2)  # service_restarted、backup_failed
    rig.alerts.backup_failed("磁盘满了")
    return [BOT_TOKEN, hub.keys["alpha"], SECRET_BODY, "ctx-1", "tok-bot", "ctx-"]


def test_every_kind_goes_out_and_none_carries_a_secret_or_a_message(rig):
    secrets = trigger_every_kind(rig)

    drain(rig)

    assert sorted(rig.bark.titles) == sorted(alerts.title(kind) for kind in alerts.KINDS)
    for call in rig.bark.calls:
        for secret in secrets:
            assert secret not in call.title, (secret, call.title)
            assert secret not in call.text, (secret, call.text)


def test_the_device_code_stays_out_of_events_logs_and_the_database(make_rig, fake_bark, caplog):
    fake_bark.valid_keys = {"SOME-OTHER-KEY"}  # 假 Bark 回的错误里带着收到的设备码
    rig = make_rig(env_key=None)
    rig.alerts = Alerts(rig.db, rig.config, rig.clock, rig.paths, env={})
    write_bark_key(rig.paths, f"{fake_bark.base_url}/{DEVICE}")
    register(rig)

    with caplog.at_level(logging.DEBUG):
        rig.deliver()

    assert fake_bark.pushes[0].device_key == DEVICE
    assert DEVICE not in caplog.text
    for event in rig.events(kind=None):
        assert DEVICE not in event["detail"]
    for row in rig.states().values():
        assert DEVICE not in repr(row)
    assert last_bark_outcome(rig.db.conn()) == "refused"


# ---------------------------------------------------------------- 多条待发


def test_alerts_go_out_oldest_first(rig):
    rig.alerts.backup_failed("磁盘满了")
    rig.clock.advance(10)
    rig.alerts.service_restarted(crashed(), 0)
    rig.clock.advance(10)
    register(rig)

    rig.deliver()

    assert rig.bark.titles == [BACKUP_TITLE, RESTARTED_TITLE, EXPIRED_TITLE]


def test_the_order_is_by_registration_time_not_by_when_the_kind_first_appeared(rig):
    """备份失败这一种先出现过（库里它的行更靠前），但这一次是后登记的：按登记先后送。"""
    rig.alerts.backup_failed("磁盘满了")
    rig.deliver()  # 送完了，这一行还在
    rig.clock.advance(10)
    rig.alerts.service_restarted(crashed(), 0)
    rig.clock.advance(24 * 3600)  # 第二天备份又失败了一次
    rig.alerts.backup_failed("磁盘还是满的")
    rig.bark.calls.clear()

    rig.deliver()

    assert rig.bark.titles == [RESTARTED_TITLE, BACKUP_TITLE]


def test_a_refused_alert_does_not_hold_back_the_next_one(rig):
    rig.alerts.backup_failed("磁盘满了")
    rig.clock.advance(10)
    rig.alerts.service_restarted(crashed(), 0)
    rig.bark.script(Refused("failed to get device token"))

    rig.deliver()

    assert rig.bark.titles == [BACKUP_TITLE, RESTARTED_TITLE]
    assert rig.pending() == {}


def test_an_alert_that_cannot_get_through_does_not_hold_back_the_next_one(rig):
    rig.alerts.backup_failed("磁盘满了")
    rig.clock.advance(10)
    rig.alerts.service_restarted(crashed(), 0)
    rig.bark.script(*[UNREACHABLE] * 4)  # 第一条的一轮请求都失败，第二条的请求成功

    due = rig.deliver()

    assert rig.bark.titles == [BACKUP_TITLE] * 4 + [RESTARTED_TITLE]
    assert set(rig.pending()) == {alerts.BACKUP_FAILED}
    assert due == 5 * MINUTE


def test_each_waiting_alert_has_its_own_schedule(rig):
    rig.alerts.backup_failed("磁盘满了")
    rig.bark.default = UNREACHABLE
    rig.deliver()  # 第一条进了队列，5 分钟后再试
    rig.clock.advance(2 * MINUTE)
    rig.alerts.service_restarted(crashed(), 0)
    rig.deliver()  # 第二条是新的，马上试，失败后也是它自己的 5 分钟

    rig.bark.default = Delivered()
    rig.bark.calls.clear()
    rig.clock.advance(3 * MINUTE)  # 第一条到点了，第二条还没有
    rig.deliver()

    assert rig.bark.titles == [BACKUP_TITLE]


def test_deliver_says_how_long_until_the_nearest_of_several_waiting_alerts(rig):
    rig.alerts.backup_failed("磁盘满了")
    rig.bark.default = UNREACHABLE
    rig.deliver()  # 第一条进了队列，5 分钟后再试
    rig.clock.advance(2 * MINUTE)
    rig.alerts.service_restarted(crashed(), 0)

    due = rig.deliver()  # 第二条是新的：一轮请求用掉 7 秒，然后也排 5 分钟

    assert due == 3 * MINUTE - 7  # 先到点的是第一条


def test_deliver_never_says_to_wait_a_negative_time(rig):
    rig.alerts.backup_failed("磁盘满了")
    rig.bark.default = UNREACHABLE
    rig.deliver()  # 第一条进了队列
    rig.clock.advance(MINUTE)
    rig.alerts.service_restarted(crashed(), 0)
    original = rig.bark.send

    def slow(key, title, text):  # Bark 很慢：一个请求过去 6 分钟，第一条在这期间到点了
        rig.clock.advance(6 * MINUTE)
        return original(key, title, text)

    rig.bark.send = slow

    assert rig.deliver() == 0


# ---------------------------------------------------------------- 测试告警（`hubctl alerts test`）


def test_a_test_alert_goes_out_with_a_test_title(rig):
    result = send_test_alert(
        rig.db, rig.config, rig.clock, rig.paths, client=rig.bark, env={ENV_KEY: DEVICE}
    )

    assert result.status == "delivered"
    [call] = rig.bark.calls
    assert call.title == TEST_TITLE
    assert "测试" in call.title
    assert event_texts(rig, "Bark 测试成功") != []
    assert last_bark_outcome(rig.db.conn()) == "ok"
    assert rig.states() == {}  # 测试告警不占去重状态，也不进待发队列


def test_a_refused_test_alert_says_so_without_the_device_code(rig):
    rig.bark.script(Refused(f"failed to get device token {DEVICE}"))  # 对方把设备码回显了

    result = send_test_alert(
        rig.db, rig.config, rig.clock, rig.paths, client=rig.bark, env={ENV_KEY: DEVICE}
    )

    assert result.status == "refused"
    assert "failed to get device token" in result.detail
    assert DEVICE not in result.detail
    assert all(DEVICE not in event["detail"] for event in rig.events())
    [event] = [e for e in rig.events() if e["detail"].startswith("Bark 测试被拒绝")]
    assert event["detail"] == "Bark 测试被拒绝：failed to get device token ***"
    assert event["level"] == "WARNING"
    assert last_bark_outcome(rig.db.conn()) == "refused"
    assert len(rig.bark.calls) == 1  # 测试告警只请求一次，结果直接告诉你


def test_a_test_alert_that_cannot_get_through_is_reported(rig):
    rig.bark.script(UNREACHABLE)

    result = send_test_alert(
        rig.db, rig.config, rig.clock, rig.paths, client=rig.bark, env={ENV_KEY: DEVICE}
    )

    assert result.status == "unreachable"
    assert "HTTP 500" in result.detail
    assert len(rig.bark.calls) == 1
    assert last_bark_outcome(rig.db.conn()) is None
    [event] = [e for e in rig.events() if e["detail"].startswith("Bark 测试没送到")]
    assert event["detail"] == "Bark 测试没送到：HTTP 500"
    assert event["level"] == "WARNING"


def test_a_test_alert_without_a_key_says_there_is_no_key(rig):
    result = send_test_alert(rig.db, rig.config, rig.clock, rig.paths, client=rig.bark, env={})

    assert result.status == "no_key"
    assert rig.bark.calls == []


def test_a_test_alert_with_a_malformed_key_file_says_what_is_wrong(rig):
    rig.paths.bark.write_text("这不是 JSON", encoding="utf-8")

    result = send_test_alert(rig.db, rig.config, rig.clock, rig.paths, client=rig.bark, env={})

    assert result.status == "no_key"
    assert "bark.json 的内容不对" in result.detail
    assert rig.bark.calls == []


def test_an_unexpected_error_in_the_client_is_reported_by_type_only_in_a_test_alert(rig):
    rig.bark.script(RuntimeError("boom 秘密内容"))

    result = send_test_alert(
        rig.db, rig.config, rig.clock, rig.paths, client=rig.bark, env={ENV_KEY: DEVICE}
    )

    assert result.status == "unreachable"
    assert result.detail == "RuntimeError"
    assert all("boom" not in event["detail"] for event in rig.events())


def test_a_test_alert_reads_the_key_from_the_file_too(rig):
    write_bark_key(rig.paths, "FILE-KEY")

    result = send_test_alert(rig.db, rig.config, rig.clock, rig.paths, client=rig.bark, env={})

    assert result.status == "delivered"
    assert rig.bark.calls[0].key.device == "FILE-KEY"


# ---------------------------------------------------------------- 独立的投递线程


@pytest.fixture
def running(rig):
    """已经起了投递线程的 `rig`；测试结束时停掉。"""
    rig.alerts.start()
    yield rig
    rig.alerts.stop()


def test_the_worker_sends_a_new_alert_without_being_asked(running):
    register(running)

    wait_for(lambda: running.pending() == {} and len(running.bark.calls) == 1)

    assert running.bark.titles == [EXPIRED_TITLE]


def test_the_worker_retries_when_the_clock_says_so(running):
    running.bark.script(*[UNREACHABLE] * 4)
    register(running)
    wait_for(lambda: len(running.bark.calls) == 4)
    running.clock.wait_until_blocked()  # 一轮做完了，线程在等下一次到期

    running.clock.advance(5 * MINUTE)

    wait_for(lambda: len(running.bark.calls) == 5 and running.pending() == {})


def test_the_worker_does_not_retry_before_the_clock_says_so(running):
    running.bark.script(*[UNREACHABLE] * 4)
    register(running)
    wait_for(lambda: len(running.bark.calls) == 4)
    running.clock.wait_until_blocked()

    running.clock.advance(4 * MINUTE)
    time.sleep(0.05)  # 给线程一点真实时间：它不该醒

    assert len(running.bark.calls) == 4


def test_starting_the_worker_sends_what_was_left_over(rig):
    register(rig)
    rig.bark.default = UNREACHABLE
    rig.deliver()
    rig.restart()
    rig.bark.default = Delivered()
    rig.bark.calls.clear()

    rig.alerts.start()
    try:
        wait_for(lambda: rig.pending() == {})
    finally:
        rig.alerts.stop()

    assert rig.bark.titles == [EXPIRED_TITLE]


def test_stopping_ends_the_worker_and_is_harmless_to_repeat(rig):
    rig.alerts.start()
    rig.clock.wait_until_blocked()  # 线程已经睡在等待里：停止要把它叫醒
    assert rig.alerts.running

    rig.alerts.stop()
    rig.alerts.stop()

    assert not rig.alerts.running


def test_a_round_that_fails_is_logged_and_the_worker_pauses_before_trying_again(
    running, monkeypatch, caplog
):
    calls = []

    def broken():
        calls.append(running.clock.now_ms())
        raise sqlite3.OperationalError("database is locked 带点内容")

    monkeypatch.setattr(running.alerts, "deliver", broken)

    with caplog.at_level(logging.ERROR):
        register(running)  # 新登记的叫醒线程，这一轮出错
        wait_for(lambda: running.clock.pending_deadlines() == [running.clock.now_ms() + 30_000])

    assert len(calls) == 1  # 歇 30 秒，不是一直空转
    assert "OperationalError" in caplog.text
    assert "带点内容" not in caplog.text
    running.clock.advance(30)
    wait_for(lambda: len(calls) == 2)


def test_a_stop_that_arrives_while_a_round_is_running_is_not_lost(running):
    """停止在一轮送到一半时到达（这一轮开头清掉的唤醒信号，停止没来得及再置）：这一轮一完
    线程就结束，不会再去睡着等一个不会来的唤醒。"""
    original = running.bark.send

    def stopping_from_inside(key, title, text):
        running.alerts._stop.set()  # 只置位，不叫醒
        return original(key, title, text)

    running.bark.send = stopping_from_inside
    register(running)

    wait_for(lambda: not running.alerts.running)


def test_stopping_a_worker_that_never_started_is_fine(rig):
    rig.alerts.stop()

    assert not rig.alerts.running


def test_the_worker_cannot_be_started_twice(running):
    with pytest.raises(RuntimeError):
        running.alerts.start()


def test_a_slow_bark_does_not_hold_up_sending_or_receiving(make_rig, fake_bark):
    """假 Bark 收到请求后不回话：发送和收消息照常，不受影响；这次 Bark 请求在超时时放弃。"""
    from tests.support.fakehttp import hang

    rig = make_rig(tweak(Config(), alerts={"bark_timeout_seconds": 2}), env_key=None)
    rig.alerts = Alerts(rig.db, rig.config, rig.clock, rig.paths, env={})  # 真的 BarkClient
    write_bark_key(rig.paths, f"{fake_bark.base_url}/{DEVICE}")
    pusher = rig.hub.ready()
    poller = make_poller(rig)
    fake_bark.script("push", hang(30))
    rig.alerts.start()
    try:
        rig.alerts.backup_failed("磁盘满了")
        fake_bark.wait_for_requests("push", 1)  # 告警线程正卡在 Bark 的请求里

        started = time.monotonic()
        sent = rig.hub.sender.push(pusher, "你好")
        poller.step()
        rig.hub.speak()
        elapsed = time.monotonic() - started
        still_waiting = rig.pending() != {}

        assert sent.status == "sent"
        assert elapsed < 1.0
        assert still_waiting  # 那条告警还卡在 Bark 那里
        wait_for(lambda: rig.pending() == {}, timeout=10)  # 超时以后立即重试，这回通了
    finally:
        rig.alerts.stop()

    assert len(fake_bark.pushes) == 2
