"""项目在线判断（规格 `hub-projects`「项目在线判断」，任务 A8 的 `project list` 用）。

在线 = 启用着、能收话、最近 `inbox.online_seconds` 秒内取过话（取话和确认都会更新"最近取话"）。
此刻挂着的取话请求只有服务进程自己知道，但那个请求开始时已经更新了"最近取话"，读库就够了。
"""

from __future__ import annotations

import dataclasses

import pytest

from clawbot_hub.projects import Project, is_online

NOW = 1_800_000_000_000
SECOND = 1000


def make_project(**overrides) -> Project:
    fields = {
        "id": 1,
        "name": "alpha",
        "prefix": "量化",
        "guaranteed": 1,
        "cap": 2,
        "is_default": False,
        "label": None,
        "enabled": True,
        "push_only": False,
        "created_ms": 0,
        "last_inbox_ms": NOW - 10 * SECOND,
    }
    fields.update(overrides)
    return Project(**fields)


def test_a_project_that_took_a_message_a_moment_ago_is_online():
    assert is_online(make_project(), NOW, 90)


def test_the_window_is_the_configured_number_of_seconds_and_includes_its_edge():
    on_the_edge = make_project(last_inbox_ms=NOW - 90 * SECOND)
    just_over = make_project(last_inbox_ms=NOW - 90 * SECOND - 1)

    assert is_online(on_the_edge, NOW, 90)
    assert not is_online(just_over, NOW, 90)
    assert is_online(just_over, NOW, 91)
    assert not is_online(on_the_edge, NOW, 89)


def test_a_project_that_never_took_a_message_is_offline():
    assert not is_online(make_project(last_inbox_ms=None), NOW, 90)


def test_making_other_requests_does_not_make_a_project_online():
    """只调过 `POST /v1/messages` 的项目：更新的是"最后出现"，不是"最近取话"。"""
    pushed_only_recently = make_project(last_inbox_ms=NOW - 120 * SECOND, last_seen_ms=NOW)

    assert not is_online(pushed_only_recently, NOW, 90)


@pytest.mark.parametrize(
    "overrides",
    [
        {"enabled": False},
        {"push_only": True},
        {"removed_ms": NOW - 1},
    ],
    ids=["disabled", "push-only", "removed"],
)
def test_disabled_push_only_and_removed_projects_are_never_online(overrides):
    assert not is_online(make_project(**overrides), NOW, 90)


def test_a_future_timestamp_counts_as_online_rather_than_crashing():
    """时钟被调慢过：库里的时间比现在还晚，不当成离线。"""
    assert is_online(make_project(last_inbox_ms=NOW + 5 * SECOND), NOW, 90)


def test_the_project_object_is_not_changed():
    project = make_project()

    is_online(project, NOW, 90)

    assert dataclasses.asdict(project) == dataclasses.asdict(make_project())
