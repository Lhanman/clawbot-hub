"""连不上腾讯（`upstream_down`）和重新连上（`upstream_restored`）两种告警。

服务记一个"最近一次与腾讯完成往返的时间"（收到完整的 HTTP 响应、状态码不是 429 / 5xx，收和发
不分）。只在登录状态是正常时判断；距离这个时间已经 `alerts.upstream_down_minutes`（默认 10）分钟、
而且最近一次出错是连接类的（连不上、连接超时、TLS 失败、429、5xx）才告警；刚启动还没有往返过的，
从启动时刻算起。恢复（有了新的完整往返）以后发"已连上腾讯"，而且只在发过"连不上"之后发。

前半部分直接在库里写 poller 会写的那几列，精确地卡时间；后半部分让真的 `Poller` 对着假上游跑，
确认它写的列和这里读的列对得上（规格里的几个场景）。
"""

from __future__ import annotations

import sqlite3

import pytest

from clawbot_hub import alerts
from clawbot_hub.bark import Delivered, Unreachable
from clawbot_hub.config import Config
from clawbot_hub.poller import Poller
from clawbot_hub.upstream.base import PollFailed
from tests.alerts.conftest import fail_connection, make_poller, round_trip
from tests.support.hub import tweak

DOWN_TITLE = "ClawBot 共用服务 · 连不上腾讯"
RESTORED_TITLE = "ClawBot 共用服务 · 已连上腾讯"
EXPIRED_TITLE = "ClawBot 共用服务 · 登录失效"
MINUTE = 60


# ---------------------------------------------------------------- 在库里造出 poller 留下的痕迹


def keep_failing(rig, minutes: int, *, every: int = 60) -> list[bool]:
    """接下来 `minutes` 分钟里一直连不上：每隔 `every` 秒出一次错、查一次告警，返回每次查的结果。"""
    results = []
    for _ in range(minutes * MINUTE // every):
        rig.clock.advance(every)
        fail_connection(rig)
        results.append(rig.alerts.check_upstream())
    return results


def titles(rig) -> list[str]:
    return rig.bark.titles


# ---------------------------------------------------------------- 连不上


def test_the_alert_comes_after_the_configured_minutes_of_connection_errors(rig):
    rig.hub.bind()
    round_trip(rig)
    fail_connection(rig)

    rig.clock.advance(10 * MINUTE - 1)
    fail_connection(rig)
    assert not rig.alerts.check_upstream()
    assert rig.pending() == {}

    rig.clock.advance(1)
    fail_connection(rig)
    assert rig.alerts.check_upstream()

    assert rig.pending() == {alerts.UPSTREAM_DOWN: alerts.upstream_down_text(10)}
    rig.deliver()
    assert titles(rig) == [DOWN_TITLE]
    assert "已经 10 分钟" in rig.bark.texts[0]


def test_the_minutes_follow_the_configuration(make_rig):
    rig = make_rig(tweak(Config(), alerts={"upstream_down_minutes": 3}))
    rig.hub.bind()
    round_trip(rig)

    rig.clock.advance(3 * MINUTE - 1)
    fail_connection(rig)
    assert not rig.alerts.check_upstream()
    rig.clock.advance(1)
    fail_connection(rig)

    assert rig.alerts.check_upstream()
    assert rig.pending() == {alerts.UPSTREAM_DOWN: alerts.upstream_down_text(3)}


def test_it_is_reported_once_however_long_it_goes_on(rig):
    rig.hub.bind()
    round_trip(rig)

    results = keep_failing(rig, 180)
    rig.deliver()

    assert results.count(True) == 1
    assert titles(rig) == [DOWN_TITLE]


def test_the_alert_and_its_state_are_one_write(rig):
    rig.hub.bind()
    round_trip(rig)
    keep_failing(rig, 10)

    row = rig.state(alerts.UPSTREAM_DOWN)

    assert row["pending_text"] == alerts.upstream_down_text(10)
    assert row["pending_since_ms"] == rig.clock.now_ms()
    assert row["episode"] is not None
    assert row["attempts"] == 0


def test_counting_starts_at_the_service_start_when_there_was_no_round_trip_yet(rig):
    rig.hub.bind()  # 服务刚启动，一次往返都没有过，只有错
    rig.clock.advance(MINUTE)
    fail_connection(rig)

    rig.clock.advance(9 * MINUTE - 1)  # 启动后 9 分 59 秒
    fail_connection(rig)
    assert not rig.alerts.check_upstream()

    rig.clock.advance(1)  # 启动后整 10 分钟
    fail_connection(rig)
    assert rig.alerts.check_upstream()


def test_counting_starts_at_the_last_round_trip_not_at_the_service_start(rig):
    rig.hub.bind()
    rig.clock.advance(30 * MINUTE)  # 服务已经跑了半小时，一直正常
    round_trip(rig)
    fail_connection(rig)

    rig.clock.advance(5 * MINUTE)
    fail_connection(rig)
    assert not rig.alerts.check_upstream()  # 距最后一次往返才 5 分钟

    rig.clock.advance(5 * MINUTE)
    fail_connection(rig)
    assert rig.alerts.check_upstream()


def test_a_stale_look_at_the_state_does_not_register_the_same_outage_twice(rig, monkeypatch):
    """另一处刚登记了这一段不通，这一处读到的还是旧的：进了事务再看一遍，已经登记过就不再登记。"""
    rig.hub.bind()
    round_trip(rig)
    keep_failing(rig, 10)
    before = rig.state(alerts.UPSTREAM_DOWN)
    rig.clock.advance(60)
    fail_connection(rig)
    real = alerts.Alerts._row
    reads = []

    def stale(conn, key):
        reads.append(key)
        return None if len(reads) == 1 else real(conn, key)  # 外面那一次读到的是旧的

    monkeypatch.setattr(alerts.Alerts, "_row", staticmethod(stale))

    assert not rig.alerts.check_upstream()

    assert rig.state(alerts.UPSTREAM_DOWN)["pending_since_ms"] == before["pending_since_ms"]


def test_a_restart_moves_the_counting_to_the_new_start(rig):
    rig.hub.bind()
    round_trip(rig)  # 最后一次往返在 20 分钟前，之后出过错（但这 20 分钟里没有人查过告警）
    rig.clock.advance(MINUTE)
    fail_connection(rig)
    rig.clock.advance(19 * MINUTE)

    rig.restart()
    fail_connection(rig)
    assert not rig.alerts.check_upstream()  # 从重启算起，不是从 20 分钟前

    rig.clock.advance(10 * MINUTE - 1)
    fail_connection(rig)
    assert not rig.alerts.check_upstream()
    rig.clock.advance(1)
    fail_connection(rig)
    assert rig.alerts.check_upstream()


def test_an_old_error_followed_by_a_good_round_trip_is_not_a_problem(rig):
    rig.hub.bind()
    fail_connection(rig)
    rig.clock.advance(MINUTE)
    round_trip(rig)  # 后来又通了

    rig.clock.advance(30 * MINUTE)  # 之后一直没有新的错，也没有新的往返

    assert not rig.alerts.check_upstream()
    assert rig.pending() == {}


def test_an_error_with_a_round_trip_at_the_same_moment_is_not_a_connection_problem(rig):
    """腾讯回了完整的响应、但响应是个错：错误和往返记在同一时刻。这一刻起很久没有新的往返，
    也不能因此说成"连不上"。"""
    rig.hub.bind()
    now = rig.clock.now_ms()
    rig.execute(
        "UPDATE account_state SET last_poll_error = ?, last_poll_error_ms = ?,"
        " last_upstream_ok_ms = ? WHERE id = 1",
        "HTTP 403",
        now,
        now,
    )
    rig.clock.advance(11 * MINUTE)

    assert not rig.alerts.check_upstream()
    assert rig.pending() == {}


def test_without_any_error_there_is_no_connection_problem(rig):
    rig.hub.bind()  # 登录是好的，但 poller 一次也没有出过错
    rig.clock.advance(3 * 3600)

    assert not rig.alerts.check_upstream()
    assert rig.pending() == {}


@pytest.mark.parametrize("login", ["expired", "missing", "broken"])
def test_nothing_is_down_while_the_login_is_not_normal(rig, login):
    rig.hub.bind()
    round_trip(rig)
    fail_connection(rig)
    {"expired": rig.hub.expire, "missing": rig.hub.unbind, "broken": rig.hub.corrupt}[login]()

    results = keep_failing(rig, 60)

    assert not any(results)
    assert alerts.UPSTREAM_DOWN not in rig.pending()


# ---------------------------------------------------------------- 恢复


def test_a_good_round_trip_sends_the_restored_alert(rig):
    rig.hub.bind()
    round_trip(rig)
    keep_failing(rig, 10)
    rig.deliver()

    rig.clock.advance(30)
    round_trip(rig)
    assert rig.alerts.check_upstream()

    assert rig.pending() == {alerts.UPSTREAM_RESTORED: alerts.upstream_restored_text()}
    rig.deliver()
    assert titles(rig) == [DOWN_TITLE, RESTORED_TITLE]


def test_a_round_trip_at_the_very_moment_of_the_alert_is_not_yet_a_recovery(rig):
    rig.hub.bind()
    round_trip(rig)
    keep_failing(rig, 10)
    assert rig.clock.now_ms() == rig.state(alerts.UPSTREAM_DOWN)["pending_since_ms"]

    round_trip(rig)  # 同一毫秒的往返：「之后」要严格晚于登记
    assert not rig.alerts.check_upstream()

    rig.clock.advance(0.001)
    round_trip(rig)
    assert rig.alerts.check_upstream()


def test_while_an_outage_is_reported_and_nothing_changed_the_check_only_reads(rig):
    """每 30 秒一次的检查在"已经报过、还没恢复"的这段时间里只读，不抢写锁。"""
    rig.hub.bind()
    round_trip(rig)
    keep_failing(rig, 10)
    rig.clock.advance(30)
    fail_connection(rig)
    holder = sqlite3.connect(rig.paths.db, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")  # 别的连接占着写锁
    try:
        assert not rig.alerts.check_upstream()
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_two_checks_racing_to_announce_the_recovery_announce_it_once(rig, monkeypatch):
    rig.hub.bind()
    round_trip(rig)
    keep_failing(rig, 10)
    stale = (
        rig.db.conn()
        .execute("SELECT * FROM alert_state WHERE key = ?", (alerts.UPSTREAM_DOWN,))
        .fetchone()
    )
    rig.clock.advance(30)
    round_trip(rig)
    assert rig.alerts.check_upstream()  # 第一处：登记"已连上腾讯"
    first = rig.state(alerts.UPSTREAM_RESTORED)["pending_since_ms"]
    rig.clock.advance(5)
    real = alerts.Alerts._row
    reads = []

    def racing(conn, key):
        reads.append(key)
        return stale if len(reads) == 1 else real(conn, key)  # 第二处读到的还是"没恢复"

    monkeypatch.setattr(alerts.Alerts, "_row", staticmethod(racing))

    assert not rig.alerts.check_upstream()  # 进了事务一看，已经恢复过了

    assert rig.state(alerts.UPSTREAM_RESTORED)["pending_since_ms"] == first


def test_the_restored_alert_is_sent_once(rig):
    rig.hub.bind()
    round_trip(rig)
    keep_failing(rig, 10)
    rig.clock.advance(30)
    round_trip(rig)
    rig.alerts.check_upstream()
    rig.deliver()
    rig.bark.calls.clear()

    for _ in range(10):
        rig.clock.advance(30)
        round_trip(rig)
        assert not rig.alerts.check_upstream()
    rig.deliver()

    assert rig.bark.calls == []


def test_no_restored_alert_when_nothing_was_ever_down(rig):
    rig.hub.bind()
    round_trip(rig)

    rig.clock.advance(30)
    round_trip(rig)

    assert not rig.alerts.check_upstream()
    assert rig.pending() == {}


def test_a_new_outage_after_the_recovery_alerts_again(rig):
    rig.hub.bind()
    round_trip(rig)
    keep_failing(rig, 10)
    rig.clock.advance(30)
    round_trip(rig)
    rig.alerts.check_upstream()
    rig.deliver()

    keep_failing(rig, 10)  # 又断了 10 分钟
    rig.deliver()

    assert titles(rig) == [DOWN_TITLE, RESTORED_TITLE, DOWN_TITLE]


def test_the_second_outage_gets_its_own_restored_alert(rig):
    rig.hub.bind()
    round_trip(rig)
    for _ in range(2):
        keep_failing(rig, 10)
        rig.clock.advance(30)
        round_trip(rig)
        rig.alerts.check_upstream()
        rig.deliver()

    assert titles(rig) == [DOWN_TITLE, RESTORED_TITLE, DOWN_TITLE, RESTORED_TITLE]


def test_both_alerts_wait_for_bark_and_go_out_in_order(rig):
    rig.bark.default = Unreachable("连不上 Bark")
    rig.hub.bind()
    round_trip(rig)
    keep_failing(rig, 10)
    rig.deliver()  # Bark 不通：留在待发队列里
    rig.clock.advance(30)
    round_trip(rig)
    rig.alerts.check_upstream()
    assert set(rig.pending()) == {alerts.UPSTREAM_DOWN, alerts.UPSTREAM_RESTORED}

    rig.bark.default = Delivered()
    rig.bark.calls.clear()
    rig.clock.advance(5 * MINUTE)
    rig.deliver()

    assert titles(rig) == [DOWN_TITLE, RESTORED_TITLE]
    assert rig.pending() == {}


# ---------------------------------------------------------------- 重启


def test_a_restart_does_not_repeat_an_alert_that_is_still_true(rig):
    rig.hub.bind()
    round_trip(rig)
    keep_failing(rig, 10)
    rig.deliver()
    rig.bark.calls.clear()

    rig.restart()
    results = keep_failing(rig, 60)
    rig.deliver()

    assert not any(results)
    assert rig.bark.calls == []


def test_recovery_after_a_restart_is_still_announced(rig):
    rig.hub.bind()
    round_trip(rig)
    keep_failing(rig, 10)
    rig.deliver()

    rig.restart()
    rig.clock.advance(30)
    round_trip(rig)
    assert rig.alerts.check_upstream()
    rig.deliver()

    assert titles(rig) == [DOWN_TITLE, RESTORED_TITLE]


# ---------------------------------------------------------------- 真的 poller 对着假上游


def drive(rig, poller: Poller, seconds: int, *, step: int = 10) -> None:
    """让收消息的一轮一轮和每 30 秒一次的检查走过 `seconds` 秒（假时间）。"""
    end = rig.clock.now_ms() + seconds * 1000
    next_poll = next_check = rig.clock.now_ms()
    while rig.clock.now_ms() < end:
        now = rig.clock.now_ms()
        if now >= next_poll:
            next_poll = now + round(poller.step() * 1000)
        if now >= next_check:
            rig.alerts.check_upstream()
            next_check = now + 30_000
        rig.clock.advance(step)


@pytest.mark.parametrize("mode", ["disconnect", "http_5xx"])
def test_eleven_minutes_without_a_connection_then_recovery(rig, mode):
    rig.hub.bind()
    poller = make_poller(rig)
    started = rig.clock.now_ms()
    rig.hub.upstream.set_mode(mode)

    drive(rig, poller, 11 * MINUTE)

    assert rig.state(alerts.UPSTREAM_DOWN)["pending_since_ms"] == started + 10 * MINUTE * 1000
    rig.deliver()
    assert titles(rig) == [DOWN_TITLE]

    rig.hub.upstream.set_mode("normal")
    drive(rig, poller, 2 * MINUTE)  # 退避最长 60 秒，之后的一轮就收到了正常的响应
    rig.deliver()

    assert titles(rig) == [DOWN_TITLE, RESTORED_TITLE]


def test_a_rate_limit_counts_as_a_connection_problem(rig):
    rig.hub.bind()
    poller = make_poller(rig)
    rig.hub.upstream.set_default("get_updates", PollFailed("HTTP 429", completed=False))

    drive(rig, poller, 11 * MINUTE)

    assert alerts.UPSTREAM_DOWN in rig.pending()


def test_errors_with_a_complete_response_are_not_a_connection_problem(rig):
    """腾讯回了完整的响应（403、业务码不对）：线是通的，是别的毛病，不报"连不上"。"""
    rig.hub.bind()
    poller = make_poller(rig)
    rig.hub.upstream.set_default("get_updates", PollFailed("HTTP 403", completed=True))

    drive(rig, poller, 30 * MINUTE)

    assert rig.pending() == {}


def test_an_expired_login_is_one_alert_and_not_a_connection_problem(rig):
    """腾讯每次都回 -14（完整的响应），服务按失效处理并每 10 分钟探测一次，持续 3 小时。"""
    rig.hub.bind()
    poller = make_poller(rig)
    rig.hub.upstream.set_mode("session_expired")

    drive(rig, poller, 3 * 3600, step=30)
    rig.deliver()

    assert titles(rig) == [EXPIRED_TITLE]


def test_a_service_without_credentials_is_not_a_connection_problem(rig):
    poller = make_poller(rig)  # 没有绑定：不收消息

    drive(rig, poller, 3 * 3600, step=30)

    assert rig.pending() == {}
