"""项目管理的命令行（任务 D3）：`project show / set / disable / enable / remove / rotate`。

`add` 和 `list` 在 `test_minimal_cli.py`；这里是其余的子命令，以及它们共用的"需要确认的操作"的规矩
（终端里问、`--yes` 不问、不是终端又没带 `--yes` 拒绝并退出 2、什么也不改）。规则本身（前缀怎么算
冲突、保底合计、默认项目怎么换……）在 `tests/projects/`，这里只看命令行把它们交给使用者的样子：
说了什么、退出码是几、库里到底变没变、事件里有没有记、有没有露出密钥。
规格 `hub-cli`「公共约定」，`hub-projects`「修改、停用、启用、移除」「密钥」「查看」。
"""

from __future__ import annotations

import pytest

from clawbot_hub.config import Config
from clawbot_hub.projects import Projects, Unauthorized
from tests.cli.conftest import KEY, add, db_text, json_of

MINUTE = 60


@pytest.fixture
def projects(database, clock) -> Projects:
    """另开的连接上的项目表（`database` 夹具已经登记了默认项目 `seed`）。"""
    return Projects(database, Config(), clock)


def events_of(database) -> list[str]:
    rows = database.conn().execute("SELECT detail FROM events WHERE kind = 'project' ORDER BY id")
    return [row["detail"] for row in rows]


def key_of(result) -> str:
    (key,) = KEY.findall(result.stdout)
    return key


# ---------------------------------------------------------------- show


def test_show_describes_one_project_in_labelled_lines(database, run):
    add(run, "alpha", "--label", "量化助手", prefix="量化", guaranteed=3, cap=5)

    result = run("project", "show", "alpha")

    assert result.exit_code == 0, result.output
    lines = [line.strip() for line in result.stdout.splitlines()]
    assert lines[0] == "alpha"
    for expected in (
        "前缀：量化",
        "默认项目：否",
        "标签：量化助手",
        "保底：3，上限：5",
        "状态：启用",
        "类型：能收话",
        "在线：离线（最后出现 还没有）",
        "本窗口已用：推送 0 条、回复 0 条",
    ):
        assert expected in lines
    assert any(line.startswith("登记时间：") for line in lines)


def test_show_of_the_default_project_and_of_a_push_only_one(database, run):
    add(run, "cron-job", "--push-only", prefix=None, guaranteed=0, cap=2)

    seed = run("project", "show", "seed").stdout
    cron = run("project", "show", "cron-job").stdout

    assert "默认项目：是" in seed and "类型：能收话" in seed
    assert "前缀：无" in cron and "类型：只推送" in cron
    assert "标签：cron-job" in cron  # 没写标签的项目，标签默认取名字（只推送）或前缀


def test_show_says_when_a_project_is_stopped_or_removed(database, run):
    add(run, "alpha")
    run("project", "disable", "alpha")
    assert "状态：已停用" in run("project", "show", "alpha").stdout

    add(run, "bravo", prefix="乙")
    run("project", "disable", "bravo")
    run("project", "remove", "bravo", "--yes")
    shown = run("project", "show", "bravo")

    assert shown.exit_code == 0
    assert "状态：已移除" in shown.stdout


def test_show_as_json_has_the_listing_fields_and_the_registration_time(database, run, clock):
    add(run, "alpha")

    shown = json_of(run("project", "show", "alpha", "--json"))
    listed = {p["name"]: p for p in json_of(run("project", "list", "--json"))}["alpha"]

    assert {key: shown[key] for key in listed} == listed
    assert set(shown) - set(listed) == {"created"}
    assert shown["created"].startswith("2026-10-12")  # 假时钟从 2026-10-12 起，带显示时区的偏移


def test_show_of_a_project_nobody_registered_is_one_sentence(database, run):
    result = run("project", "show", "nobody")

    assert result.exit_code == 1
    assert "没有叫 nobody 的项目" in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize("name", ["_hub", "_import"])
def test_the_internal_names_are_not_projects_to_show(database, run, name):
    result = run("project", "show", name)

    assert result.exit_code == 1
    assert f"没有叫 {name} 的项目" in result.output


def test_show_without_a_database_creates_none_and_says_there_is_no_such_project(run, hub_paths):
    result = run("project", "show", "alpha")

    assert result.exit_code == 1
    assert "没有叫 alpha 的项目" in result.output
    assert not hub_paths.db.exists()


def test_show_never_prints_a_key_or_its_digest(database, run, hub_paths):
    key = key_of(add(run, "alpha"))
    digest = database.conn().execute("SELECT key_hash FROM projects WHERE name = 'alpha'")
    digest = digest.fetchone()["key_hash"]

    for args in (("project", "show", "alpha"), ("project", "show", "alpha", "--json")):
        output = run(*args).output
        assert key not in output and "chub_" not in output and digest not in output


# ---------------------------------------------------------------- set


def test_set_changes_a_number_and_says_what_changed(database, run, projects):
    add(run, "alpha", guaranteed=2, cap=4)

    result = run("project", "set", "alpha", "--cap", "6")

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "已修改项目 alpha：上限 4 → 6"
    assert projects.get("alpha").cap == 6


def test_set_changes_several_things_at_once(database, run, projects):
    add(run, "alpha", guaranteed=2, cap=4)

    result = run("project", "set", "alpha", "--guaranteed", "3", "--cap", "7", "--label", "新标签")

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "已修改项目 alpha：保底 2 → 3、上限 4 → 7、标签 → 新标签"
    alpha = projects.get("alpha")
    assert (alpha.guaranteed, alpha.cap, alpha.label) == (3, 7, "新标签")


def test_set_leaves_an_event_and_never_the_key(database, run, hub_paths):
    add(run, "alpha", guaranteed=2, cap=4)

    run("project", "set", "alpha", "--cap", "6")

    assert "改了项目 alpha：上限 4 → 6" in events_of(database)
    assert b"chub_" not in db_text(hub_paths)


def test_a_new_cap_works_for_the_very_next_push_without_restarting_anything(
    database, run, projects
):
    add(run, "alpha", guaranteed=1, cap=2)
    assert projects.get("alpha").cap == 2

    run("project", "set", "alpha", "--cap", "5")

    assert projects.get("alpha").cap == 5  # 服务每个请求都读最新的项目设置


def test_set_with_nothing_to_change_is_a_usage_error(database, run):
    add(run, "alpha")

    result = run("project", "set", "alpha")

    assert result.exit_code == 2
    assert "--prefix" in result.output and "--cap" in result.output


def test_set_label_and_no_label_together_is_a_usage_error(database, run, projects):
    add(run, "alpha", "--label", "旧标签")

    result = run("project", "set", "alpha", "--label", "新", "--no-label")

    assert result.exit_code == 2
    assert projects.get("alpha").label == "旧标签"


def test_set_to_the_values_it_already_has_says_nothing_changed(database, run):
    add(run, "alpha", guaranteed=2, cap=4)

    result = run("project", "set", "alpha", "--cap", "4")

    assert result.exit_code == 0
    assert result.stdout.strip() == "项目 alpha 没有变化"
    assert not any(event.startswith("改了项目") for event in events_of(database))


def test_set_no_label_takes_the_label_away(database, run, projects):
    add(run, "alpha", "--label", "标签一")

    result = run("project", "set", "alpha", "--no-label")

    assert result.exit_code == 0, result.output
    assert "标签 → 去掉" in result.stdout
    assert projects.get("alpha").label is None


def test_set_rules_are_the_registration_rules_and_nothing_changes_on_refusal(
    database, run, projects
):
    add(run, "alpha", guaranteed=2, cap=4)

    too_small = run("project", "set", "alpha", "--cap", "1")
    too_much = run("project", "set", "alpha", "--guaranteed", "9")

    assert too_small.exit_code == 1 and "保底不能大于上限" in too_small.output
    assert too_much.exit_code == 1 and "Traceback" not in too_much.output
    assert (projects.get("alpha").guaranteed, projects.get("alpha").cap) == (2, 4)


def test_set_of_a_removed_project_is_refused(database, run):
    add(run, "alpha", prefix="甲")
    run("project", "disable", "alpha")
    run("project", "remove", "alpha", "--yes")

    result = run("project", "set", "alpha", "--cap", "9")

    assert result.exit_code == 1 and "已移除" in result.output


def test_set_of_an_unknown_project_is_one_sentence(database, run):
    result = run("project", "set", "nobody", "--cap", "3")

    assert result.exit_code == 1 and "没有叫 nobody 的项目" in result.output


def test_a_push_only_project_has_no_prefix_to_change(database, run):
    add(run, "cron-job", "--push-only", prefix=None, guaranteed=0, cap=2)

    result = run("project", "set", "cron-job", "--prefix", "定时")

    assert result.exit_code == 1 and "只推送的项目没有前缀" in result.output


# ---- 需要确认：改前缀、换掉默认项目


def test_changing_the_prefix_not_in_a_terminal_without_yes_changes_nothing(database, run, projects):
    add(run, "alpha", prefix="甲")

    result = run("project", "set", "alpha", "--prefix", "乙")

    assert result.exit_code == 2
    assert "--yes" in result.output and "前缀从「甲」改成「乙」" in result.output
    assert projects.get("alpha").prefix == "甲"


def test_changing_the_prefix_with_yes_goes_ahead_and_repeats_the_reminder(database, run, projects):
    add(run, "alpha", prefix="甲")

    result = run("project", "set", "alpha", "--prefix", "状态", "--yes")

    assert result.exit_code == 0, result.output
    assert "前缀「甲」→「状态」" in result.stdout
    assert "提醒" in result.stdout and "前缀「状态」" in result.stdout
    assert projects.get("alpha").prefix == "状态"


def test_changing_the_prefix_in_a_terminal_asks_and_a_yes_goes_ahead(
    database, run, terminal, projects
):
    add(run, "alpha", prefix="甲")
    terminal["on"] = True

    result = run("project", "set", "alpha", "--prefix", "乙", input="y\n")

    assert result.exit_code == 0, result.output
    assert "前缀从「甲」改成「乙」" in result.output
    assert projects.get("alpha").prefix == "乙"


def test_changing_the_prefix_in_a_terminal_and_answering_no_cancels(
    database, run, terminal, projects
):
    add(run, "alpha", prefix="甲")
    terminal["on"] = True

    result = run("project", "set", "alpha", "--prefix", "乙", input="n\n")

    assert result.exit_code == 1 and "已取消" in result.output
    assert projects.get("alpha").prefix == "甲"


def test_the_same_prefix_is_not_a_change_and_asks_nothing(database, run):
    add(run, "alpha", prefix="甲")

    result = run("project", "set", "alpha", "--prefix", "甲")

    assert result.exit_code == 0 and result.stdout.strip() == "项目 alpha 没有变化"


def test_making_another_project_the_default_asks_first_and_moves_it(database, run, projects):
    add(run, "alpha", prefix="甲")

    refused = run("project", "set", "alpha", "--default")
    assert refused.exit_code == 2 and "原来的默认项目是 seed" in refused.output
    assert projects.default().name == "seed"

    result = run("project", "set", "alpha", "--default", "--yes")

    assert result.exit_code == 0, result.output
    assert "成为默认项目" in result.stdout
    assert "原来的默认项目 seed 不再是默认项目" in result.stdout
    assert projects.default().name == "alpha"


def test_the_default_project_made_default_again_changes_nothing_and_asks_nothing(
    database, run, projects
):
    result = run("project", "set", "seed", "--default")

    assert result.exit_code == 0 and result.stdout.strip() == "项目 seed 没有变化"
    assert projects.default().name == "seed"


def test_a_push_only_project_cannot_become_the_default(database, run):
    add(run, "cron-job", "--push-only", prefix=None, guaranteed=0, cap=2)

    result = run("project", "set", "cron-job", "--default", "--yes")

    assert result.exit_code == 1 and "只推送的项目不能当默认项目" in result.output


# ---------------------------------------------------------------- disable / enable


def test_disable_says_it_took_effect_and_leaves_an_event(database, run, projects):
    add(run, "alpha")

    result = run("project", "disable", "alpha")

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "已停用项目 alpha（对下一个请求立即生效）"
    assert not projects.get("alpha").enabled
    assert "停用了项目 alpha" in events_of(database)


def test_disable_needs_no_confirmation(database, run, projects, terminal):
    add(run, "alpha")
    terminal["on"] = False

    assert run("project", "disable", "alpha").exit_code == 0  # 没带 --yes，也不是终端
    assert run("project", "enable", "alpha").exit_code == 0


def test_disabling_twice_is_fine_and_leaves_one_event(database, run):
    add(run, "alpha")
    run("project", "disable", "alpha")

    result = run("project", "disable", "alpha")

    assert result.exit_code == 0
    assert result.stdout.strip() == "项目 alpha 本来就是停用的"
    assert events_of(database).count("停用了项目 alpha") == 1


def test_enable_brings_it_back(database, run, projects):
    add(run, "alpha")
    run("project", "disable", "alpha")

    result = run("project", "enable", "alpha")

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "已启用项目 alpha"
    assert projects.get("alpha").enabled
    assert "启用了项目 alpha" in events_of(database)


def test_enabling_an_enabled_project_says_so(database, run):
    add(run, "alpha")

    result = run("project", "enable", "alpha")

    assert result.exit_code == 0 and result.stdout.strip() == "项目 alpha 本来就是启用的"


def test_enable_is_refused_when_the_guarantees_would_no_longer_fit(database, run, projects):
    add(run, "bravo", prefix="乙", guaranteed=4, cap=6)
    run("project", "disable", "bravo")
    add(run, "charlie", prefix="丙", guaranteed=4, cap=6)
    # 保底合计 seed 1 + charlie 4 = 5；bravo 的 4 再加进来是 9，加回复保留 2 超过窗口总条数 10

    result = run("project", "enable", "bravo")

    assert result.exit_code == 1
    assert "保底" in result.output and "Traceback" not in result.output
    assert not projects.get("bravo").enabled


@pytest.mark.parametrize("command", ["disable", "enable", "remove", "rotate"])
def test_the_lifecycle_commands_name_a_project_that_does_not_exist(database, run, command):
    extra = ["--yes"] if command in ("remove", "rotate") else []

    result = run("project", command, "nobody", *extra)

    assert result.exit_code == 1 and "没有叫 nobody 的项目" in result.output


# ---------------------------------------------------------------- remove


def test_a_project_must_be_stopped_before_it_can_be_removed(database, run, projects):
    add(run, "alpha", prefix="甲")

    result = run("project", "remove", "alpha", "--yes")

    assert result.exit_code == 1
    assert "hubctl project disable alpha" in result.output
    assert not projects.get("alpha").removed


def test_remove_not_in_a_terminal_without_yes_changes_nothing(database, run, projects):
    add(run, "alpha", prefix="甲")
    run("project", "disable", "alpha")

    result = run("project", "remove", "alpha")

    assert result.exit_code == 2 and "--yes" in result.output
    assert not projects.get("alpha").removed


def test_remove_with_yes_takes_the_project_out_of_the_list_and_cancels_its_key(
    database, run, projects
):
    key = key_of(add(run, "alpha", prefix="甲"))
    run("project", "disable", "alpha")

    result = run("project", "remove", "alpha", "--yes")

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "已移除项目 alpha（没取走的话已作废，名字不能再用）"
    assert [p["name"] for p in json_of(run("project", "list", "--json"))] == ["seed"]
    listed = run("project", "list", "--all").stdout.splitlines()
    assert any(line.startswith("alpha（") and "已移除" in line.split("：")[0] for line in listed)
    with pytest.raises(Unauthorized):
        projects.authenticate(key)
    assert "移除了项目 alpha" in events_of(database)


def test_remove_in_a_terminal_asks_and_a_no_cancels(database, run, terminal, projects):
    add(run, "alpha", prefix="甲")
    run("project", "disable", "alpha")
    terminal["on"] = True

    cancelled = run("project", "remove", "alpha", input="n\n")
    assert cancelled.exit_code == 1 and "已取消" in cancelled.output
    assert not projects.get("alpha").removed

    agreed = run("project", "remove", "alpha", input="y\n")
    assert agreed.exit_code == 0 and projects.get("alpha").removed


def test_the_default_project_cannot_be_removed_while_another_can_take_over(database, run):
    add(run, "alpha", prefix="甲")
    run("project", "disable", "seed")

    result = run("project", "remove", "seed", "--yes")

    assert result.exit_code == 1
    assert "hubctl project set alpha --default" in result.output


def test_the_only_receiving_project_can_be_removed_even_as_the_default(database, run, projects):
    run("project", "disable", "seed")

    result = run("project", "remove", "seed", "--yes")

    assert result.exit_code == 0, result.output
    assert projects.default() is None


# ---------------------------------------------------------------- rotate


def test_rotate_prints_the_new_key_once_and_the_old_one_stops_working(database, run, projects):
    old = key_of(add(run, "alpha", prefix="甲"))

    result = run("project", "rotate", "alpha", "--yes")

    assert result.exit_code == 0, result.output
    new = key_of(result)
    assert new != old and old not in result.output
    assert "旧密钥立刻失效" in result.stdout
    assert "只显示这一次" in result.stdout
    assert projects.authenticate(new).name == "alpha"
    with pytest.raises(Unauthorized):
        projects.authenticate(old)


def test_rotate_not_in_a_terminal_without_yes_changes_nothing(database, run, projects):
    old = key_of(add(run, "alpha", prefix="甲"))

    result = run("project", "rotate", "alpha")

    assert result.exit_code == 2 and "--yes" in result.output
    assert "chub_" not in result.output
    assert projects.authenticate(old).name == "alpha"


def test_rotate_in_a_terminal_asks_first(database, run, terminal, projects):
    old = key_of(add(run, "alpha", prefix="甲"))
    terminal["on"] = True

    cancelled = run("project", "rotate", "alpha", input="n\n")
    assert cancelled.exit_code == 1 and "已取消" in cancelled.output
    assert projects.authenticate(old).name == "alpha"

    agreed = run("project", "rotate", "alpha", input="y\n")

    assert agreed.exit_code == 0, agreed.output
    assert "要给项目 alpha 换密钥，旧密钥立刻失效" in agreed.output
    with pytest.raises(Unauthorized):
        projects.authenticate(old)


def test_rotate_with_a_grace_period_keeps_the_old_key_for_that_long(database, run, projects, clock):
    old = key_of(add(run, "alpha", prefix="甲"))

    result = run("project", "rotate", "alpha", "--grace", "10", "--yes")

    assert result.exit_code == 0, result.output
    assert "旧密钥再用 10 分钟" in result.stdout
    new = key_of(result)
    clock.advance(9 * MINUTE)
    assert projects.authenticate(old).name == "alpha"
    assert projects.authenticate(new).name == "alpha"
    clock.advance(2 * MINUTE)
    with pytest.raises(Unauthorized):
        projects.authenticate(old)


@pytest.mark.parametrize("grace", ["61", "-1"])
def test_a_grace_period_out_of_range_is_a_usage_error_and_changes_nothing(
    database, run, projects, grace
):
    old = key_of(add(run, "alpha", prefix="甲"))

    result = run("project", "rotate", "alpha", "--grace", grace, "--yes")

    assert result.exit_code == 2
    assert "0 到 60 分钟" in result.output
    assert projects.authenticate(old).name == "alpha"


def test_the_new_key_is_in_no_later_output_database_or_event(database, run, hub_paths):
    new = key_of(run("project", "rotate", "seed", "--yes"))

    later = [
        run("project", "list"),
        run("project", "list", "--json"),
        run("project", "show", "seed"),
        run("project", "show", "seed", "--json"),
        run("status"),
        run("status", "--json"),
    ]

    for result in later:
        assert result.exit_code == 0, result.output
        assert "chub_" not in result.output
    assert new.encode() not in db_text(hub_paths)
    assert "给项目 seed 换了密钥，旧密钥立刻失效" in events_of(database)
    assert all("chub_" not in event for event in events_of(database))
