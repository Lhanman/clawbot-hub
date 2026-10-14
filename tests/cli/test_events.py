"""`hubctl events`（任务 D3）：看最近的事件记录——时间、级别、种类、项目、一句话，没有正文。

种类是封闭的八个，`--kind` 精确匹配，不认识的是用法错误。写事件的地方在各个模块里（登记项目、
发送被拒绝、告警……），各自有自己的测试；这里看的是命令：列几条、什么顺序、按种类挑、JSON 的样子、
没有数据库时怎么说。规格 `hub-cli`「事件记录」。
"""

from __future__ import annotations

import json

import pytest

from clawbot_hub.events import KINDS, add_event
from tests.cli.conftest import KEY, add, json_of

SECOND = 1000


def put(database, clock, kind: str, detail: str, *, level="INFO", project=None, ago=0) -> None:
    """写一条事件，时间是"现在"往前 `ago` 秒（负数是往后）。`database` 夹具登记的那个种子项目在
    "现在"留了一条事件，所以要比较先后的测试把自己的事件放在它后面（`ago` 取负数）。"""
    with database.txn() as conn:
        add_event(conn, clock.now_ms() - ago * SECOND, kind, detail, level=level, project=project)


def details(result) -> list[str]:
    """每行最后一栏（一句话）；列之间是两个空格。"""
    return [line.split("  ", 4)[-1] for line in result.stdout.splitlines()]


def test_the_latest_fifty_are_listed_oldest_first(database, run, clock):
    for number in range(1, 61):
        put(database, clock, "lifecycle", f"第 {number} 条", ago=-number)

    result = run("events")

    assert result.exit_code == 0, result.output
    shown = [d for d in details(result) if d.startswith("第 ")]
    assert shown == [f"第 {n} 条" for n in range(11, 61)]


def test_limit_changes_how_many(database, run, clock):
    for number in range(1, 11):
        put(database, clock, "lifecycle", f"第 {number} 条", ago=-number)

    result = run("events", "--limit", "3")

    assert [d for d in details(result) if d.startswith("第 ")] == ["第 8 条", "第 9 条", "第 10 条"]


@pytest.mark.parametrize("limit", ["0", "-1", "x"])
def test_a_limit_that_makes_no_sense_is_a_usage_error(database, run, limit):
    assert run("events", "--limit", limit).exit_code == 2


def test_a_line_has_the_time_the_level_the_kind_the_project_and_the_sentence(database, run, clock):
    put(database, clock, "alert", "告警放弃：登录失效", level="ERROR", project=None)
    put(database, clock, "project", "登记项目 alpha（保底 2、上限 4）", project="alpha")

    lines = run("events").stdout.splitlines()

    assert lines[-2].split("  ") == [
        "2026-10-12 10:00:00",
        "ERROR",
        "alert",
        "-",
        "告警放弃：登录失效",
    ]
    assert lines[-1].split("  ") == [
        "2026-10-12 10:00:00",
        "INFO",
        "project",
        "alpha",
        "登记项目 alpha（保底 2、上限 4）",
    ]


def test_times_are_written_in_the_display_time_zone(database, run, clock, home):
    put(database, clock, "lifecycle", "此刻")
    home.joinpath("config.yaml").write_text("display:\n  timezone: Asia/Tokyo\n", encoding="utf-8")

    assert "2026-10-12 11:00:00" in run("events").stdout  # 北京 10:00 = 东京 11:00


def test_kind_picks_exactly_that_kind(database, run, clock):
    put(database, clock, "project", "项目的事")
    put(database, clock, "alert", "告警的事")
    put(database, clock, "backup", "备份的事")

    result = run("events", "--kind", "project")

    shown = details(result)
    assert "项目的事" in shown and "告警的事" not in shown and "备份的事" not in shown


@pytest.mark.parametrize("kind", ["proj", "foo", "", "PROJECT"])
def test_an_unknown_kind_is_a_usage_error_that_lists_the_known_ones(database, run, kind):
    result = run("events", "--kind", kind)

    assert result.exit_code == 2
    for known in KINDS:
        assert known in result.output


def test_registering_a_project_leaves_an_event_without_the_key(database, run):
    result = add(run, "alpha", prefix="量化", guaranteed=2, cap=4)
    (key,) = KEY.findall(result.stdout)

    events = run("events", "--kind", "project")

    assert events.exit_code == 0, events.output
    assert any("登记项目 alpha（保底 2、上限 4）" in line for line in events.stdout.splitlines())
    assert key not in events.output and "chub_" not in events.output


def test_json_is_a_list_of_objects_with_the_same_order(database, run, clock):
    put(database, clock, "send", "发送被拒绝", level="WARNING", project="alpha", ago=-5)
    put(database, clock, "queue", "话作废", ago=-6)

    listed = json_of(run("events", "--json"))

    assert [e["detail"] for e in listed][-2:] == ["发送被拒绝", "话作废"]
    first = listed[-2]
    assert set(first) == {"id", "time", "level", "kind", "project", "detail"}
    assert (first["level"], first["kind"], first["project"]) == ("WARNING", "send", "alpha")
    assert first["time"] == "2026-10-12T10:00:05+08:00"
    assert listed[-1]["project"] is None


def test_json_respects_limit_and_kind(database, run, clock):
    for number in range(5):
        put(database, clock, "backup", f"备份 {number}", ago=10 - number)
    put(database, clock, "alert", "不相干", ago=1)

    listed = json_of(run("events", "--json", "--kind", "backup", "--limit", "2"))

    assert [e["detail"] for e in listed] == ["备份 3", "备份 4"]


def test_nothing_to_show_is_said_in_one_line(database, run):
    result = run("events", "--kind", "hold")

    assert result.exit_code == 0 and result.stdout.strip() == "没有事件记录"
    assert json.loads(run("events", "--kind", "hold", "--json").stdout) == []


def test_without_a_database_it_says_so_and_creates_none(run, hub_paths):
    result = run("events")

    assert result.exit_code == 0 and result.stdout.strip() == "没有事件记录"
    assert run("events", "--json").stdout.strip() == "[]"
    assert not hub_paths.db.exists()


def test_events_hold_no_message_text(database, run, clock):
    """事件只写编号、状态、原因，不写正文：命令只是原样列出库里的，这里确认列出来的就是库里的。"""
    put(database, clock, "send", "发送被拒绝：对话窗口过期了")

    assert "对话窗口过期了" in run("events").stdout
    assert "ZZ-UNIQUE" not in run("events", "--json").stdout
