"""告警的标题和正文（规格 `hub-alerts`「哪些事要告警」那张表）：纯函数，表格式测试。

标题统一以"ClawBot 共用服务 ·"开头；正文一两句中文，说明发生了什么、使用者要做什么；
不含登录凭证、项目密钥、消息正文（这些东西根本传不进这些函数）。
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from clawbot_hub import alerts
from tests.alerts.conftest import ms_of

TZ = ZoneInfo("Asia/Shanghai")

TITLES = {
    alerts.LOGIN_EXPIRED: "登录失效",
    alerts.LOGIN_RESTORED: "登录已恢复",
    alerts.WINDOW_EXPIRING: "窗口快到期",
    alerts.WINDOW_BLOCKED: "微信消息没发出去",
    alerts.SEND_UNCONFIRMED: "发送未确认",
    alerts.UPSTREAM_BLOCKED: "腾讯拒绝发送",
    alerts.UPSTREAM_DOWN: "连不上腾讯",
    alerts.UPSTREAM_RESTORED: "已连上腾讯",
    alerts.SERVICE_RESTARTED: "服务重启",
    alerts.BACKUP_FAILED: "备份失败",
}


def test_there_are_exactly_ten_kinds_of_alerts():
    assert set(alerts.KINDS) == set(TITLES)
    assert len(alerts.KINDS) == 10


@pytest.mark.parametrize("kind", sorted(TITLES))
def test_every_title_starts_with_the_service_name(kind):
    assert alerts.title(kind) == f"ClawBot 共用服务 · {TITLES[kind]}"


def test_an_unknown_kind_has_no_title():
    with pytest.raises(KeyError):
        alerts.title("something_else")


def test_login_expired_tells_the_owner_what_to_do():
    assert alerts.login_expired_text() == "ClawBot 登录失效了：在电脑上运行 hubctl login 重新扫码"


def test_login_restored():
    assert "登录已恢复" in alerts.login_restored_text()


def test_window_expiring_names_the_hours_and_the_moment_it_closes():
    ends = ms_of("2026-10-14 18:30")

    text = alerts.window_expiring_text(3 * 3_600_000, ends, TZ)

    assert text == "微信窗口还有约 3 小时到期（10月14日 18:30）：给 ClawBot 回一句话续期"


@pytest.mark.parametrize(
    ("remaining_ms", "hours"),
    [
        (3 * 3_600_000, 3),
        (3 * 3_600_000 - 30_000, 3),  # 差半分钟也说约 3 小时
        (2 * 3_600_000 + 20 * 60_000, 2),
        (2 * 3_600_000 + 40 * 60_000, 3),
        (20 * 60_000, 1),  # 不足一小时也不写 0
        (1_000, 1),
    ],
)
def test_window_expiring_rounds_the_hours_but_never_says_zero(remaining_ms, hours):
    text = alerts.window_expiring_text(remaining_ms, ms_of("2026-10-14 18:30"), TZ)

    assert f"约 {hours} 小时" in text


def test_window_expiring_uses_the_display_time_zone():
    ends = ms_of("2026-10-14 18:30")

    text = alerts.window_expiring_text(3_600_000, ends, ZoneInfo("Asia/Tokyo"))

    assert "10月14日 19:30" in text


@pytest.mark.parametrize("projects", [1, 2, 7])
def test_window_blocked_counts_the_projects(projects):
    assert alerts.window_blocked_text(projects) == (
        f"有 {projects} 个项目的微信消息没发出去（已经由它们自己走 Bark）：给 ClawBot 发一句话恢复"
    )


@pytest.mark.parametrize("streak", [2, 3, 10])
def test_send_unconfirmed_uses_the_configured_streak(streak):
    assert alerts.send_unconfirmed_text(streak) == (
        f"最近 {streak} 次发送腾讯都没有给消息编号，消息可能没到微信：看一眼手机"
    )


def test_upstream_blocked_has_the_code_and_the_pause():
    text = alerts.upstream_blocked_text("5", 30)

    assert "代码 5" in text
    assert "已暂停 30 分钟" in text
    assert "规则可能变了" in text


def test_upstream_down_says_how_long():
    assert alerts.upstream_down_text(10) == "连不上腾讯的 ClawBot 服务已经 10 分钟"


def test_upstream_restored():
    assert "连上" in alerts.upstream_restored_text()


def test_service_restarted_with_everything():
    text = alerts.service_restarted_text(ms_of("2026-10-14 03:15"), 2, TZ)

    assert text == "共用服务重启了（上次运行到 03:15）；有 2 条发送中断，可能少发或重发"


def test_service_restarted_without_a_last_heartbeat_drops_that_half():
    text = alerts.service_restarted_text(None, 1, TZ)

    assert text == "共用服务重启了；有 1 条发送中断，可能少发或重发"
    assert "上次运行到" not in text


def test_service_restarted_without_interrupted_sends_drops_that_half():
    text = alerts.service_restarted_text(ms_of("2026-10-14 03:15"), 0, TZ)

    assert text == "共用服务重启了（上次运行到 03:15）"


def test_service_restarted_with_nothing_known():
    assert alerts.service_restarted_text(None, 0, TZ) == "共用服务重启了"


def test_service_restarted_shows_the_heartbeat_in_the_display_time_zone():
    text = alerts.service_restarted_text(ms_of("2026-10-14 03:15"), 0, ZoneInfo("Asia/Tokyo"))

    assert "上次运行到 04:15" in text


def test_backup_failed_carries_the_reason():
    assert alerts.backup_failed_text("磁盘满了") == "备份失败，原因：磁盘满了"
