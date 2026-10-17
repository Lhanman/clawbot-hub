"""服务自己说的话的文字（任务 C3，规格 `hub-inbox`「服务自己的指令和提示」「等确认」
「项目没在线」）。

全是纯函数：给它数据，它给文字。`【服务】` 这个开头是发送时加的（`hub-send` 的标签），不在这里。
"""

from __future__ import annotations

import pytest

from clawbot_hub.hubtext import (
    ProjectLine,
    StatusInfo,
    ambiguous_text,
    disabled_text,
    full_text,
    no_default_text,
    offline_text,
    projects_text,
    status_text,
    unknown_command_text,
)

QUANT = ProjectLine("量化", is_default=True, online=True)
X = ProjectLine("x", is_default=False, online=False)


# ---------------------------------------------------------------- 服务 项目


def test_the_projects_text_lists_each_project_with_its_state_and_how_to_address_it():
    text = projects_text([QUANT, X])
    assert text == (
        "现在能找的项目：\n"
        "量化（默认，在线）：不加前缀的话都给它，也可以写“量化 …”\n"
        "x（离线）：写“x …”交给它"
    )


def test_the_default_project_may_be_offline():
    lines = projects_text([ProjectLine("量化", is_default=True, online=False)]).splitlines()
    assert lines[1].startswith("量化（默认，离线）：")


def test_the_order_of_the_projects_is_kept():
    text = projects_text([X, QUANT])
    assert text.index("x（离线）") < text.index("量化（默认，在线）")


def test_without_any_project_the_text_says_so():
    assert projects_text([]) == "还没有能收话的项目"


# ---------------------------------------------------------------- 服务 状态


def status(**overrides) -> StatusInfo:
    fields = {
        "login": "ok",
        "window": "open",
        "remaining_seconds": 21 * 3600 + 5 * 60,
        "used": 3,
        "limit": 10,
        "projects": (QUANT, X),
    }
    fields.update(overrides)
    return StatusInfo(**fields)


def test_the_status_text_has_login_window_usage_and_projects():
    assert status_text(status()) == (
        "登录状态：正常\n对话窗口：还剩 21 小时 5 分钟，已用 3/10 条\n项目：量化 在线，x 离线"
    )


@pytest.mark.parametrize(
    ("login", "line"),
    [
        ("ok", "登录状态：正常"),
        ("expired", "登录状态：已失效，在电脑上运行 hubctl login 重新扫码"),
        ("missing", "登录状态：还没绑定，在电脑上运行 hubctl login"),
        ("broken", "登录状态：凭证文件读不出来，重新运行 hubctl login"),
    ],
)
def test_every_login_state_has_its_own_line(login, line):
    assert status_text(status(login=login)).splitlines()[0] == line


@pytest.mark.parametrize(
    ("window", "line"),
    [
        ("none", "对话窗口：还没收到过你的消息"),
        ("closed", "对话窗口：已过期，你再发一句话就开新的"),
    ],
)
def test_a_window_that_is_not_open_has_no_usage(window, line):
    assert status_text(status(window=window, used=0)).splitlines()[1] == line


@pytest.mark.parametrize(
    ("seconds", "left"),
    [(86_400, "1 天"), (3 * 3600, "3 小时"), (40 * 60, "40 分钟"), (30, "不到 1 分钟")],
)
def test_the_remaining_time_is_written_like_in_hubctl_status(seconds, left):
    line = status_text(status(remaining_seconds=seconds)).splitlines()[1]
    assert line == f"对话窗口：还剩 {left}，已用 3/10 条"


def test_the_status_text_without_projects():
    assert status_text(status(projects=())).splitlines()[2] == "项目：还没有能收话的项目"


def test_the_status_text_has_no_room_for_secrets():
    text = status_text(status())
    assert "token" not in text.lower() and "ctx" not in text.lower()


# ---------------------------------------------------------------- 提示


def test_the_help_line_names_both_commands_with_the_label():
    assert unknown_command_text("服务") == "我只认两个指令：服务 项目、服务 状态"
    assert unknown_command_text("助手") == "我只认两个指令：助手 项目、助手 状态"


def test_the_no_default_project_text():
    assert no_default_text("服务") == (
        "没有设默认项目，这句话没处理：请加项目前缀（发“服务 项目”看有哪些）"
    )
    assert "“助手 项目”" in no_default_text("助手")


def test_the_disabled_project_text():
    assert disabled_text("量化") == "项目「量化」已停用，这句话没处理"


def test_the_offline_project_text_says_how_long_the_message_is_kept():
    assert offline_text("x", 10) == (
        "「x」现在没在线。这句话我先留着：10 分钟内它回来就会处理，超过就作废"
    )
    assert "3 分钟内" in offline_text("x", 3)


def test_the_backlog_text():
    assert full_text("x") == "「x」积压太多，这句话没收"


def test_the_ambiguity_text_lists_the_projects_and_gives_an_example_with_the_users_word():
    assert ambiguous_text(["量化", "x"], "对") == (
        "有几个项目在等你确认（量化、x），请加前缀，例如“量化 对”"
    )
    assert ambiguous_text(["x", "量化"], "确认").endswith("例如“x 确认”")
