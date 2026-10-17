"""项目登记、修改、停用、启用、移除（规格 `hub-projects`，任务 A6）。

这里测"项目管理"的规矩本身（`projects.Projects`）；命令行的外壳（`--yes`、退出码、打印的样子）
在 A8，取话相关的几条（停用时挂着的取话、暂停的话启用后继续取）在 C2。
"""

from __future__ import annotations

import threading

import pytest

from clawbot_hub.projects import (
    NeedsConfirmation,
    PrefixConflict,
    ProjectError,
    ProjectNotFound,
    Projects,
)
from tests.projects.conftest import events, register, with_settings
from tests.support.threads import run_in_thread

# ---------------------------------------------------------------- 登记：默认项目


def test_the_first_project_that_can_receive_becomes_the_default(projects):
    result = projects.add("ashare-quant", prefix="量化", guaranteed=6, cap=8)

    project = result.project
    assert (project.name, project.prefix) == ("ashare-quant", "量化")
    assert (project.guaranteed, project.cap) == (6, 8)
    assert project.is_default and project.label is None
    assert project.enabled and not project.push_only and not project.removed
    assert result.key.startswith("chub_")
    assert result.replaced_default is None
    assert projects.default() == project
    assert projects.list() == [project]


def test_a_project_remembers_when_it_was_registered(projects, clock):
    clock.advance(5)
    registered = register(projects, "alpha").project
    clock.advance(600)

    assert registered.created_ms == clock.now_ms() - 600_000
    assert projects.get("alpha").created_ms == registered.created_ms


def test_a_project_carries_the_times_the_database_remembers_about_it(projects, database):
    register(projects, "alpha")
    with database.txn() as conn:
        conn.execute(
            "UPDATE projects SET last_seen_ms = 111, last_inbox_ms = 222, awaiting_until_ms = 333"
            " WHERE name = 'alpha'"
        )

    project = projects.get("alpha")

    assert (project.last_seen_ms, project.last_inbox_ms, project.awaiting_until_ms) == (
        111,
        222,
        333,
    )


def test_a_push_only_project_is_never_the_default(projects):
    cron = register(projects, "cron-job", push_only=True, guaranteed=0).project
    assert cron.push_only and cron.prefix is None
    assert not cron.is_default and projects.default() is None
    assert cron.label == "cron-job"

    xray = register(projects, "xray", prefix="x").project

    assert xray.is_default
    assert not projects.get("cron-job").is_default


def test_push_only_and_default_together_are_refused(projects):
    with pytest.raises(ProjectError, match="只推送的项目不能当默认项目"):
        projects.add("cron-job", push_only=True, guaranteed=0, cap=1, make_default=True)

    assert projects.list() == []


def test_asking_for_the_default_when_there_is_none_needs_no_confirmation(projects):
    result = register(projects, "alpha", make_default=True)

    assert result.project.is_default and result.replaced_default is None


def test_replacing_the_default_needs_confirmation_and_says_who_it_was(projects):
    register(projects, "alpha")

    with pytest.raises(NeedsConfirmation) as ask:
        register(projects, "beta", make_default=True)
    assert "alpha" in str(ask.value) and "beta" in str(ask.value)
    assert [p.name for p in projects.list()] == ["alpha"]  # 什么也没加

    result = register(projects, "beta", make_default=True, yes=True)

    assert result.replaced_default == "alpha"
    assert result.project.is_default and not projects.get("alpha").is_default
    assert projects.default().name == "beta"


def test_replacing_the_default_leaves_every_label_as_it_was(projects):
    register(projects, "alpha", prefix="a")
    register(projects, "bravo", prefix="x")  # 标签是 x
    register(projects, "charlie", prefix="c", make_default=True, yes=True)

    assert [p.label for p in projects.list()] == [None, "x", None]


def test_a_receiving_project_registered_after_the_default_left_becomes_the_default(projects):
    register(projects, "alpha")
    projects.disable("alpha")
    projects.remove("alpha", yes=True)
    assert projects.default() is None

    cron = register(projects, "cron-job", push_only=True).project
    assert not cron.is_default and projects.default() is None

    assert register(projects, "bravo", prefix="b").project.is_default


# ---------------------------------------------------------------- 登记：名字


@pytest.mark.parametrize("name", ["ab", "a1", "a-b", "a--", "a" + "b" * 31])
def test_names_that_follow_the_rule_are_accepted(projects, name):
    assert projects.add(name, push_only=True, guaranteed=0, cap=0).project.name == name


@pytest.mark.parametrize(
    "name",
    [
        "Ashare",
        "aShare",
        "abC",
        "1x",
        "a",
        "_hub",
        "_import",
        "a_b",
        "a b",
        "ab" + "c" * 31,
        "中文名",
        "",
        "-ab",
        "ab\n",
        "ab!",
        None,
        12,
    ],
    ids=repr,
)
def test_names_that_break_the_rule_are_refused(projects, name):
    with pytest.raises(ProjectError, match="名字"):
        projects.add(name, push_only=True, guaranteed=0, cap=0)

    assert projects.list() == []


def test_a_name_cannot_be_used_twice(projects):
    register(projects, "alpha")

    with pytest.raises(ProjectError, match="名字 alpha 已经有项目在用"):
        register(projects, "alpha", prefix="zzz")


def test_a_removed_projects_name_stays_taken(projects):
    register(projects, "alpha")
    projects.disable("alpha")
    projects.remove("alpha", yes=True)

    with pytest.raises(ProjectError, match="名字 alpha 属于一个已移除的项目"):
        register(projects, "alpha", prefix="zzz")


# ---------------------------------------------------------------- 登记：前缀


def test_a_project_that_receives_must_have_a_prefix(projects):
    with pytest.raises(ProjectError, match="需要收话的项目必须有前缀"):
        projects.add("alpha", guaranteed=1, cap=2)

    assert projects.list() == []


@pytest.mark.parametrize("prefix", ["x", ""])
def test_a_push_only_project_has_no_prefix(projects, prefix):
    with pytest.raises(ProjectError, match="只推送的项目没有前缀"):
        projects.add("cron-job", prefix=prefix, push_only=True, guaranteed=0, cap=1)


@pytest.mark.parametrize(
    ("prefix", "fragment"),
    [
        ("服务", "指令词"),
        ("对", "确认词"),
        ("是", "确认词"),
        ("确认", "确认词"),
        ("九个字的前缀九个字的", "1 到 8 个字符"),
        ("abcdefghi", "1 到 8 个字符"),
        ("a b", "字母、数字、汉字"),
        ("a!", "字母、数字、汉字"),
        ("量化。", "字母、数字、汉字"),
        ("", "1 到 8 个字符"),
        (123, "1 到 8 个字符"),
    ],
)
def test_prefixes_that_break_the_rules_are_refused(projects, prefix, fragment):
    with pytest.raises(ProjectError, match=fragment):
        register(projects, "alpha", prefix=prefix)

    assert projects.list() == []


def test_a_prefix_of_eight_characters_is_fine(projects):
    assert register(projects, "alpha", prefix="量化量化abcd").project.prefix == "量化量化abcd"


def test_the_hub_label_decides_which_prefixes_are_taken(database, config, clock):
    renamed = Projects(database, with_settings(config, hub_label="助手"), clock)

    with pytest.raises(ProjectError, match="助手.*指令词"):
        register(renamed, "alpha", prefix="助手")

    assert register(renamed, "alpha", prefix="服务").project.prefix == "服务"


def test_the_comparison_with_the_hub_label_ignores_case_and_width(database, config, clock):
    english = Projects(database, with_settings(config, hub_label="Service"), clock)

    for prefix in ("service", "ＳＥＲＶＩＣＥ", "SERVICE"):
        with pytest.raises(ProjectError, match="指令词"):
            register(english, "alpha", prefix=prefix)


def test_confirm_words_from_the_configuration_are_off_limits(database, config, clock):
    custom = Projects(database, with_settings(config, confirm_words=("ok", "好")), clock)

    for prefix in ("OK", "好"):
        with pytest.raises(ProjectError, match="确认词"):
            register(custom, "alpha", prefix=prefix)

    assert register(custom, "alpha", prefix="对").project.prefix == "对"  # 不再是确认词了


def test_a_prefix_belongs_to_one_project_ignoring_case_and_width(projects):
    register(projects, "xray", prefix="x")

    for prefix in ("X", "ｘ", "Ｘ"):
        with pytest.raises(ProjectError, match="前缀「.」已经被项目 xray 占着") as error:
            register(projects, "yank", prefix=prefix)
        assert "已停用" not in str(error.value)  # 占着前缀的项目还在启用

    assert [p.name for p in projects.list()] == ["xray"]


def test_the_form_a_prefix_is_compared_in_is_remembered_when_it_is_registered_or_changed(projects):
    """前缀按规范化以后的样子比较，登记和修改时都要把比较用的样子记下来。"""
    register(projects, "xray", prefix="ＡＢ")
    register(projects, "yank", prefix="y")
    projects.set("yank", prefix="Ｃｄ", yes=True)

    for taken, prefix in (("xray", "ab"), ("xray", "AB"), ("yank", "cd"), ("yank", "CD")):
        with pytest.raises(ProjectError, match=f"已经被项目 {taken} 占着"):
            register(projects, "zulu", prefix=prefix)


def test_the_prefix_is_kept_the_way_it_was_typed(projects):
    assert register(projects, "alpha", prefix="AbC").project.prefix == "AbC"


def test_a_disabled_project_still_holds_its_prefix(projects):
    register(projects, "xray", prefix="x")
    projects.disable("xray")

    with pytest.raises(ProjectError, match="已停用"):
        register(projects, "yank", prefix="x")


def test_a_removed_projects_prefix_can_be_used_again(projects):
    register(projects, "xray", prefix="x")
    projects.disable("xray")
    projects.remove("xray", yes=True)

    assert register(projects, "yank", prefix="x").project.prefix == "x"
    assert projects.get("xray").prefix is None


# ---------------------------------------------------------------- 登记：保底和上限


def test_the_guaranteed_total_plus_the_reply_reserve_may_not_pass_the_window(projects):
    register(projects, "alpha", guaranteed=6, cap=8)

    with pytest.raises(ProjectError) as error:
        register(projects, "beta", guaranteed=4, cap=4)

    text = str(error.value)
    assert "保底合计 10 加上回复保留 2 超过窗口总条数 10" in text
    assert "alpha 6" in text  # 列出现在各项目的保底
    assert [p.name for p in projects.list()] == ["alpha"]


def test_a_total_that_exactly_fills_the_window_is_fine(projects):
    register(projects, "alpha", guaranteed=6, cap=8)

    assert register(projects, "beta", guaranteed=2, cap=2).project.guaranteed == 2


def test_the_listing_in_the_refusal_names_every_project_that_counts(projects):
    register(projects, "alpha", guaranteed=3, cap=3)
    register(projects, "beta", guaranteed=3, cap=3)
    register(projects, "gamma", guaranteed=1, cap=1)
    projects.disable("gamma")  # 停用的不占保底，也不列

    with pytest.raises(ProjectError, match="alpha 3、beta 3") as error:
        register(projects, "delta", guaranteed=3, cap=3)

    assert "gamma" not in str(error.value)


def test_the_guaranteed_of_a_disabled_project_does_not_count(projects):
    register(projects, "alpha", guaranteed=6, cap=8)
    projects.disable("alpha")

    assert register(projects, "beta", guaranteed=8, cap=8).project.guaranteed == 8


def test_the_refusal_says_so_when_no_other_project_holds_a_guarantee(database, config, clock):
    """配置后来改小了窗口：停用着的项目启用时，光它自己的保底就超了，别的项目一个也没有。"""
    register(Projects(database, config, clock), "alpha", guaranteed=6, cap=6)
    projects = Projects(database, config, clock)
    projects.disable("alpha")
    smaller = Projects(database, with_settings(config, window_limit=7), clock)

    with pytest.raises(ProjectError, match="别的项目的保底：（没有）"):
        smaller.enable("alpha")


def test_the_window_and_the_reserve_come_from_the_configuration(database, config, clock):
    small = Projects(database, with_settings(config, window_limit=6, reply_reserve=1), clock)

    with pytest.raises(ProjectError, match="上限最多 5"):
        register(small, "alpha", guaranteed=1, cap=6)
    register(small, "alpha", guaranteed=3, cap=5)
    with pytest.raises(ProjectError, match="保底合计 6 加上回复保留 1 超过窗口总条数 6，超了 1 条"):
        register(small, "bravo", guaranteed=3, cap=5)

    assert register(small, "bravo", guaranteed=2, cap=5).project.guaranteed == 2


@pytest.mark.parametrize(
    ("guaranteed", "cap", "message"),
    [
        (1, 9, "上限最多 8"),
        (5, 4, "保底不能大于上限"),
        (-1, 2, "保底要是 0 以上的整数"),
        (1, -1, "上限要是 0 以上的整数"),
        (1.5, 2, "保底要是 0 以上的整数"),
        (True, 2, "保底要是 0 以上的整数"),
        ("1", 2, "保底要是 0 以上的整数"),
        (1, 2.0, "上限要是 0 以上的整数"),
    ],
)
def test_guaranteed_and_cap_must_be_sensible_numbers(projects, guaranteed, cap, message):
    with pytest.raises(ProjectError, match=message):
        register(projects, "alpha", guaranteed=guaranteed, cap=cap)

    assert projects.list() == []


def test_the_edges_of_the_numbers_are_allowed(projects):
    assert register(projects, "alpha", guaranteed=0, cap=0).project.cap == 0
    assert register(projects, "beta", guaranteed=8, cap=8).project.guaranteed == 8


def test_two_registrations_at_the_same_time_cannot_overshoot_the_total(database, config, clock):
    barrier = threading.Barrier(2)

    def register_one(name: str) -> str:
        barrier.wait()
        Projects(database, config, clock).add(name, prefix=name, guaranteed=5, cap=5)
        return name

    workers = [run_in_thread(register_one, "left"), run_in_thread(register_one, "right")]
    outcomes = []
    for worker in workers:
        try:
            outcomes.append(worker.result())
        except ProjectError as error:
            outcomes.append(error)

    refused = [o for o in outcomes if isinstance(o, ProjectError)]
    assert len(refused) == 1 and "保底合计" in str(refused[0])
    assert len(Projects(database, config, clock).list()) == 1


# ---------------------------------------------------------------- 登记：标签


def test_labels_are_settled_when_the_project_is_registered(projects):
    labels = {
        "default": register(projects, "alpha", prefix="a").project.label,
        "second": register(projects, "bravo", prefix="x").project.label,
        "no-label": register(projects, "charlie", prefix="y", no_label=True).project.label,
        "given": register(projects, "delta", prefix="z", label="备用").project.label,
        "push-only": register(projects, "cron-job", push_only=True).project.label,
        "long": register(
            projects, "weekly-report-generator", push_only=True, guaranteed=0
        ).project.label,
    }

    assert labels == {
        "default": None,
        "second": "x",
        "no-label": None,
        "given": "备用",
        "push-only": "cron-job",
        "long": "weekly-report-ge",
    }


def test_a_label_of_sixteen_characters_is_fine(projects):
    assert register(projects, "alpha", label="一" * 16).project.label == "一" * 16


@pytest.mark.parametrize(
    "label", ["【x", "x】", "a\nb", "a\rb", "x" * 17, "", "服务", " 服务", 123, b"x"], ids=repr
)
def test_bad_labels_are_refused(projects, label):
    with pytest.raises(ProjectError, match="标签"):
        register(projects, "alpha", label=label)

    assert projects.list() == []


def test_a_label_and_no_label_cannot_come_together(projects):
    with pytest.raises(ProjectError, match="标签.*不能同时"):
        register(projects, "alpha", label="x", no_label=True)


def test_a_label_taken_from_the_name_that_equals_the_hub_label_is_refused(database, config, clock):
    renamed = Projects(database, with_settings(config, hub_label="helper"), clock)

    with pytest.raises(ProjectError, match="--label"):
        register(renamed, "helper", push_only=True)

    assert register(renamed, "helper", push_only=True, label="助理").project.label == "助理"
    assert register(renamed, "helper2", push_only=True, no_label=True).project.label is None


def test_the_label_taken_from_the_name_is_compared_with_the_hub_label_ignoring_case(
    database, config, clock
):
    renamed = Projects(database, with_settings(config, hub_label="Helper"), clock)

    with pytest.raises(ProjectError, match="--label"):
        register(renamed, "helper", push_only=True)


def test_a_name_of_exactly_sixteen_characters_is_the_whole_label(projects):
    assert register(projects, "a" * 16, push_only=True, guaranteed=0).project.label == "a" * 16


# ---------------------------------------------------------------- 登记：提醒和记录


def test_registering_reminds_that_the_default_projects_own_words_may_collide(projects):
    result = register(projects, "alpha", prefix="状态")

    (note,) = result.notes
    assert "前缀「状态」" in note and "默认项目" in note


def test_a_push_only_project_has_nothing_to_remind_about(projects):
    assert register(projects, "cron-job", push_only=True).notes == ()


def test_registering_leaves_one_event_that_does_not_contain_the_key(projects, database):
    result = register(projects, "alpha", guaranteed=3, cap=5)

    (event,) = events(database)
    assert (event["kind"], event["level"], event["project"]) == ("project", "INFO", "alpha")
    assert event["detail"] == "登记项目 alpha（保底 3、上限 5），成为默认项目"
    assert result.key not in event["detail"]


def test_the_registration_event_says_what_is_special_about_the_project(projects, database):
    register(projects, "alpha")
    register(projects, "cron-job", push_only=True, guaranteed=0, cap=2)
    register(projects, "bravo", guaranteed=2, cap=4)
    register(projects, "charlie", guaranteed=1, cap=3, make_default=True, yes=True)

    details = [event["detail"] for event in events(database)]

    assert details == [
        "登记项目 alpha（保底 1、上限 2），成为默认项目",
        "登记项目 cron-job（保底 0、上限 2），只推送",
        "登记项目 bravo（保底 2、上限 4）",
        "登记项目 charlie（保底 1、上限 3），成为默认项目，原来的默认项目是 alpha",
    ]


def test_a_refused_registration_leaves_no_event(projects, database):
    with pytest.raises(ProjectError):
        register(projects, "Alpha")

    assert events(database) == []


# ---------------------------------------------------------------- 内部的两个名字


def test_the_internal_names_do_not_show_up_anywhere(projects):
    assert projects.list() == [] and projects.list(include_removed=True) == []
    for name in ("_hub", "_import"):
        with pytest.raises(ProjectNotFound):
            projects.get(name)
        with pytest.raises(ProjectNotFound):
            projects.disable(name)

    assert register(projects, "alpha", guaranteed=8, cap=8).project.is_default
    assert [p.name for p in projects.list(include_removed=True)] == ["alpha"]


def test_the_internal_projects_are_not_default_candidates_nor_in_the_total(projects):
    assert projects.default() is None
    assert register(projects, "alpha", guaranteed=8, cap=8).project.guaranteed == 8


def test_altered_internal_rows_still_do_not_count_in_the_guaranteed_total(projects, database):
    """内部的两行平时是停用、保底 0。改成最容易混进去的样子，登记和启用时仍然不算它们。"""
    with database.txn() as conn:
        conn.execute("UPDATE projects SET enabled = 1, guaranteed = 5, cap = 5 WHERE internal = 1")

    assert register(projects, "alpha", guaranteed=8, cap=8).project.guaranteed == 8
    projects.disable("alpha")
    assert projects.enable("alpha").enabled


def test_an_altered_internal_row_is_still_not_the_default(projects, database):
    with database.txn() as conn:
        conn.execute("UPDATE projects SET is_default = 1, enabled = 1 WHERE name = '_hub'")

    assert projects.default() is None


def test_an_altered_internal_row_is_still_not_someone_else_who_can_receive(projects, database):
    register(projects, "alpha")
    projects.disable("alpha")
    with database.txn() as conn:
        conn.execute("UPDATE projects SET enabled = 1, push_only = 0 WHERE name = '_hub'")

    assert projects.remove("alpha", yes=True).removed


# ---------------------------------------------------------------- 查看


def test_listing_is_in_registration_order_and_hides_removed_projects(projects):
    register(projects, "mike")
    register(projects, "alpha")
    register(projects, "zulu")
    register(projects, "bravo")
    projects.disable("alpha")
    projects.remove("alpha", yes=True)

    assert [p.name for p in projects.list()] == ["mike", "zulu", "bravo"]
    listed = projects.list(include_removed=True)
    assert [(p.name, p.removed) for p in listed] == [
        ("mike", False),
        ("alpha", True),
        ("zulu", False),
        ("bravo", False),
    ]


def test_get_finds_a_removed_project_but_not_an_unknown_one(projects):
    register(projects, "alpha")
    projects.disable("alpha")
    projects.remove("alpha", yes=True)

    assert projects.get("alpha").removed
    with pytest.raises(ProjectNotFound, match="没有叫 nobody 的项目"):
        projects.get("nobody")


def test_a_project_is_called_by_its_prefix_or_else_by_its_name(projects):
    assert register(projects, "alpha", prefix="量化").project.display == "量化"
    assert register(projects, "cron-job", push_only=True).project.display == "cron-job"


# ---------------------------------------------------------------- 修改


def test_changes_take_effect_at_once_and_are_checked_like_a_registration(projects):
    register(projects, "alpha", guaranteed=3, cap=3)

    changed = projects.set("alpha", cap=5)
    assert (changed.guaranteed, changed.cap) == (3, 5)
    assert projects.get("alpha").cap == 5

    with pytest.raises(ProjectError, match="保底不能大于上限"):
        projects.set("alpha", guaranteed=6)
    with pytest.raises(ProjectError, match="上限最多 8"):
        projects.set("alpha", cap=9)
    assert (projects.get("alpha").guaranteed, projects.get("alpha").cap) == (3, 5)  # 原设置不变


def test_a_change_may_not_push_the_guaranteed_total_over_the_window(projects):
    register(projects, "alpha", guaranteed=3, cap=3)
    register(projects, "beta", guaranteed=3, cap=3)

    with pytest.raises(ProjectError, match="保底合计 10 加上回复保留 2 超过窗口总条数 10"):
        projects.set("beta", guaranteed=7, cap=7)
    assert projects.set("beta", guaranteed=5, cap=5).guaranteed == 5  # 5 + 3 + 2 刚好


def test_the_guaranteed_of_a_disabled_project_may_change_without_the_total_check(projects):
    register(projects, "alpha", guaranteed=6, cap=6)
    register(projects, "beta", guaranteed=2, cap=2)
    projects.disable("beta")

    assert projects.set("beta", guaranteed=8, cap=8).guaranteed == 8  # 不占保底，启用时再查


def test_guaranteed_and_cap_are_checked_together_when_both_change(projects):
    register(projects, "alpha", guaranteed=3, cap=3)

    assert projects.set("alpha", guaranteed=7, cap=8).guaranteed == 7


def test_changing_the_prefix_needs_confirmation(projects):
    register(projects, "xray", prefix="x")

    with pytest.raises(NeedsConfirmation, match="x.*y"):
        projects.set("xray", prefix="y")
    assert projects.get("xray").prefix == "x"

    assert projects.set("xray", prefix="y", yes=True).prefix == "y"


def test_a_new_prefix_must_follow_the_same_rules(projects):
    register(projects, "xray", prefix="x")
    register(projects, "yank", prefix="y")

    with pytest.raises(ProjectError, match="已经被项目 yank 占着"):
        projects.set("xray", prefix="Y", yes=True)
    with pytest.raises(ProjectError, match="指令词"):
        projects.set("xray", prefix="服务", yes=True)
    with pytest.raises(ProjectError, match="确认词"):
        projects.set("xray", prefix="对", yes=True)
    assert projects.get("xray").prefix == "x"


def test_setting_the_same_prefix_again_changes_nothing_and_asks_nothing(projects):
    register(projects, "xray", prefix="x")

    assert projects.set("xray", prefix="x").prefix == "x"
    assert projects.set("xray", prefix="X").prefix == "x"  # 规范化以后相同：不算改


def test_a_push_only_project_cannot_get_a_prefix(projects):
    register(projects, "cron-job", push_only=True)

    with pytest.raises(ProjectError, match="只推送的项目没有前缀"):
        projects.set("cron-job", prefix="c", yes=True)


def test_the_label_can_be_set_or_cleared(projects):
    register(projects, "alpha", prefix="a")
    register(projects, "bravo", prefix="b")

    assert projects.set("bravo", label="乙").label == "乙"
    assert projects.set("bravo", no_label=True).label is None
    with pytest.raises(ProjectError, match="标签"):
        projects.set("bravo", label="【乙】")
    with pytest.raises(ProjectError, match="标签"):
        projects.set("bravo", label="服务")
    with pytest.raises(ProjectError, match="不能同时"):
        projects.set("bravo", label="乙", no_label=True)


def test_changing_the_default_takes_confirmation_and_leaves_the_labels_alone(projects):
    register(projects, "alpha", prefix="a")
    register(projects, "bravo", prefix="x")

    with pytest.raises(NeedsConfirmation, match="alpha"):
        projects.set("bravo", make_default=True)
    assert projects.default().name == "alpha"

    projects.set("bravo", make_default=True, yes=True)

    assert projects.default().name == "bravo"
    assert not projects.get("alpha").is_default
    assert (projects.get("alpha").label, projects.get("bravo").label) == (None, "x")


def test_making_the_default_the_default_again_is_nothing(projects):
    register(projects, "alpha", prefix="a")

    assert projects.set("alpha", make_default=True).is_default


def test_a_push_only_project_cannot_be_made_the_default(projects):
    register(projects, "alpha", prefix="a")
    register(projects, "cron-job", push_only=True)

    with pytest.raises(ProjectError, match="只推送的项目不能当默认项目"):
        projects.set("cron-job", make_default=True, yes=True)


def test_a_project_can_become_the_default_when_there_is_none(projects, database):
    register(projects, "alpha", prefix="a")
    register(projects, "bravo", prefix="b")
    with database.txn() as conn:  # 正常操作到不了"有能收话的项目、却没有默认"，直接改库造出来
        conn.execute("UPDATE projects SET is_default = 0")

    assert projects.set("bravo", make_default=True).is_default  # 没有要换掉的，不用确认
    assert projects.default().name == "bravo"


def test_changes_to_a_removed_or_unknown_project_are_refused(projects):
    register(projects, "alpha")
    projects.disable("alpha")
    projects.remove("alpha", yes=True)

    with pytest.raises(ProjectError, match="已移除"):
        projects.set("alpha", cap=3)
    with pytest.raises(ProjectNotFound):
        projects.set("nobody", cap=3)


def test_asking_for_no_change_is_not_an_error_and_leaves_no_event(projects, database):
    register(projects, "alpha")
    before = len(events(database))

    assert projects.set("alpha").name == "alpha"
    assert len(events(database)) == before


def test_a_refused_change_leaves_nothing_behind(projects, database):
    register(projects, "alpha", guaranteed=3, cap=3)
    before = len(events(database))

    with pytest.raises(ProjectError):
        projects.set("alpha", guaranteed=9)

    assert len(events(database)) == before


def test_a_change_leaves_one_event(projects, database):
    register(projects, "alpha", guaranteed=3, cap=3)

    projects.set("alpha", cap=5)

    last = events(database)[-1]
    assert (last["kind"], last["project"]) == ("project", "alpha")


def test_the_event_of_a_change_names_exactly_what_changed(projects, database):
    register(projects, "alpha", prefix="a", guaranteed=3, cap=3)
    register(projects, "bravo", prefix="b", guaranteed=1, cap=2)

    def last_detail() -> str:
        return events(database)[-1]["detail"]

    projects.set("alpha", cap=5)
    assert last_detail() == "改了项目 alpha：上限 3 → 5"
    projects.set("alpha", guaranteed=2)
    assert last_detail() == "改了项目 alpha：保底 3 → 2"
    projects.set("alpha", guaranteed=1, cap=4)
    assert last_detail() == "改了项目 alpha：保底 2 → 1，上限 5 → 4"
    projects.set("alpha", prefix="aa", yes=True)
    assert last_detail() == "改了项目 alpha：前缀 a → aa"
    projects.set("bravo", label="乙")
    assert last_detail() == "改了项目 bravo：标签 改成 乙"
    projects.set("bravo", no_label=True)
    assert last_detail() == "改了项目 bravo：标签 去掉"
    projects.set("bravo", make_default=True, yes=True)
    assert last_detail() == "改了项目 bravo：成为默认项目"
    projects.set("alpha", prefix="a", cap=2, label="甲", make_default=True, yes=True)
    assert last_detail() == ("改了项目 alpha：前缀 aa → a，上限 4 → 2，标签 改成 甲，成为默认项目")


def test_setting_what_is_already_so_changes_nothing_and_leaves_no_event(projects, database):
    register(projects, "alpha", prefix="a", guaranteed=3, cap=5, label="甲")
    before = len(events(database))

    projects.set("alpha", guaranteed=3)
    projects.set("alpha", cap=5)
    projects.set("alpha", guaranteed=3, cap=5)
    projects.set("alpha", label="甲")
    projects.set("alpha", prefix="A")

    assert len(events(database)) == before
    alpha = projects.get("alpha")
    assert (alpha.guaranteed, alpha.cap, alpha.label, alpha.prefix) == (3, 5, "甲", "a")


# ---------------------------------------------------------------- 停用和启用


def test_disabling_takes_a_project_out_at_once_and_clears_the_awaiting_mark(projects, database):
    register(projects, "alpha")
    with database.txn() as conn:
        conn.execute("UPDATE projects SET awaiting_until_ms = 99 WHERE name = 'alpha'")

    disabled = projects.disable("alpha")

    assert not disabled.enabled and disabled.awaiting_until_ms is None
    assert not projects.get("alpha").enabled


def test_disabling_and_enabling_each_leave_an_event(projects, database):
    register(projects, "alpha")

    projects.disable("alpha")
    disabled = events(database)[-1]
    projects.enable("alpha")
    enabled = events(database)[-1]

    assert (disabled["kind"], disabled["level"], disabled["project"]) == (
        "project",
        "INFO",
        "alpha",
    )
    assert disabled["detail"] == "停用了项目 alpha"
    assert enabled["detail"] == "启用了项目 alpha"


def test_disabling_keeps_the_default_flag_and_the_settings(projects):
    register(projects, "alpha", guaranteed=3, cap=5)

    disabled = projects.disable("alpha")

    assert disabled.is_default and (disabled.guaranteed, disabled.cap) == (3, 5)


def test_disabling_twice_or_enabling_an_enabled_project_is_harmless(projects, database):
    register(projects, "alpha")
    projects.disable("alpha")
    count = len(events(database))

    assert not projects.disable("alpha").enabled
    projects.enable("alpha")
    assert projects.enable("alpha").enabled
    assert len(events(database)) == count + 1  # 只有真启用的那一次有记录


def test_enabling_checks_the_total_again(projects):
    register(projects, "alpha", guaranteed=4, cap=4)
    register(projects, "beta", guaranteed=4, cap=4)
    projects.disable("beta")
    projects.set("alpha", guaranteed=6, cap=6)

    with pytest.raises(ProjectError) as error:
        projects.enable("beta")

    assert "超了 2 条" in str(error.value)
    assert "保底合计 10 加上回复保留 2 超过窗口总条数 10" in str(error.value)
    assert not projects.get("beta").enabled

    projects.set("alpha", guaranteed=4)
    assert projects.enable("beta").enabled


def test_enabling_a_removed_project_is_refused(projects):
    register(projects, "alpha")
    projects.disable("alpha")
    projects.remove("alpha", yes=True)

    with pytest.raises(ProjectError, match="已移除"):
        projects.enable("alpha")


# ---------------------------------------------------------------- 移除


def test_a_project_must_be_disabled_before_it_can_be_removed(projects):
    register(projects, "alpha")

    with pytest.raises(ProjectError, match="先停用.*project disable alpha"):
        projects.remove("alpha", yes=True)

    assert not projects.get("alpha").removed


def test_removing_needs_confirmation(projects):
    register(projects, "alpha")
    projects.disable("alpha")

    with pytest.raises(NeedsConfirmation, match="alpha"):
        projects.remove("alpha")

    assert not projects.get("alpha").removed


def test_a_removed_project_keeps_its_record_but_gives_up_prefix_default_and_key(
    projects, database, clock
):
    result = register(projects, "alpha", prefix="a", guaranteed=3, cap=4)
    projects.rotate("alpha", grace_minutes=10, yes=True)  # 旧密钥还在宽限期里
    projects.disable("alpha")
    clock.advance(60)

    removed = projects.remove("alpha", yes=True)

    assert removed.removed and not removed.enabled
    assert removed.removed_ms == clock.now_ms()
    assert removed.prefix is None and not removed.is_default
    assert (removed.guaranteed, removed.cap) == (3, 4)  # 记录保留
    row = database.conn().execute("SELECT * FROM projects WHERE name = 'alpha'").fetchone()
    assert row["prefix_key"] is None
    assert row["prev_key_hash"] is None and row["prev_key_until_ms"] is None
    assert result.key not in repr(dict(row))
    assert projects.default() is None


def test_removing_clears_the_text_and_the_conversation_token_of_what_was_not_taken(
    projects, database
):
    register(projects, "alpha")
    register(projects, "bravo", prefix="b")
    projects.disable("alpha")
    with database.txn() as conn:
        project_id = conn.execute("SELECT id FROM projects WHERE name = 'alpha'").fetchone()[0]
        other_id = conn.execute("SELECT id FROM projects WHERE name = 'bravo'").fetchone()[0]
        for number, (owner, state) in enumerate(
            [
                (project_id, "queued"),
                (project_id, "leased"),
                (project_id, "acked"),
                (other_id, "queued"),
            ]
        ):
            conn.execute(
                "INSERT INTO inbox (id, project_id, text, has_text, at_ms, received_ms, via, state,"
                " handle, context_token) VALUES (?, ?, '正文', 1, 1, 1, 'prefix', ?, ?, 'ctx')",
                (f"m{number}", owner, state, f"h{number}"),
            )

    projects.set("bravo", make_default=True, yes=True)
    projects.remove("alpha", yes=True)

    rows = {
        row["id"]: dict(row)
        for row in database.conn().execute("SELECT * FROM inbox ORDER BY id").fetchall()
    }
    for dropped in ("m0", "m1"):  # 还没取走的（待取、已租出）作废
        assert rows[dropped]["state"] == "dropped"
        assert rows[dropped]["text"] is None and rows[dropped]["context_token"] is None
        assert rows[dropped]["handle"] is None
    assert rows["m2"]["state"] == "acked" and rows["m2"]["text"] == "正文"  # 取走并确认的不动
    assert rows["m3"]["state"] == "queued" and rows["m3"]["text"] == "正文"  # 别的项目的不动


def test_several_projects_can_be_removed(projects):
    for name in ("alpha", "bravo", "charlie"):
        register(projects, name)
    for name in ("bravo", "charlie"):
        projects.disable(name)
        assert projects.remove(name, yes=True).removed

    assert [p.name for p in projects.list(include_removed=True) if p.removed] == [
        "bravo",
        "charlie",
    ]


def test_a_removed_name_cannot_come_back_but_its_prefix_can_be_used(projects):
    register(projects, "alpha", prefix="a")
    projects.disable("alpha")
    projects.remove("alpha", yes=True)

    with pytest.raises(ProjectError, match="已移除"):
        register(projects, "alpha", prefix="b")
    assert register(projects, "bravo", prefix="a").project.prefix == "a"


def test_removing_the_default_asks_to_hand_the_role_over_when_someone_else_can_receive(projects):
    register(projects, "alpha", prefix="a")
    register(projects, "bravo", prefix="b")
    projects.disable("alpha")

    with pytest.raises(ProjectError, match="project set bravo --default") as error:
        projects.remove("alpha", yes=True)
    assert "默认项目" in str(error.value)
    assert not projects.get("alpha").removed

    projects.set("bravo", make_default=True, yes=True)

    assert projects.remove("alpha", yes=True).removed


def test_the_only_project_that_can_receive_may_be_removed_even_as_the_default(projects):
    register(projects, "alpha", prefix="a")
    register(projects, "cron-job", push_only=True)  # 只推送的不算"别的能收话的项目"
    projects.disable("alpha")

    assert projects.remove("alpha", yes=True).removed
    assert projects.default() is None


def test_only_a_project_that_is_neither_removed_nor_push_only_counts_as_someone_else(projects):
    register(projects, "alpha", prefix="a")
    register(projects, "bravo", prefix="b")
    register(projects, "charlie", prefix="c")
    projects.disable("bravo")  # 停用的还是能收话的项目（启用后继续），所以算
    projects.disable("alpha")
    with pytest.raises(ProjectError, match="project set"):
        projects.remove("alpha", yes=True)

    projects.disable("charlie")
    projects.remove("bravo", yes=True)
    projects.remove("charlie", yes=True)

    assert projects.remove("alpha", yes=True).removed  # 其余的都移除了，剩它一个可以直接移除


def test_removing_leaves_an_event(projects, database):
    register(projects, "alpha")
    projects.disable("alpha")

    count = len(events(database))

    projects.remove("alpha", yes=True)

    (added,) = events(database)[count:]
    assert (added["kind"], added["level"], added["project"]) == ("project", "INFO", "alpha")
    assert added["detail"] == "移除了项目 alpha"


# ---------------------------------------------------------------- 配置与已有前缀冲突


def test_a_prefix_that_became_a_confirm_word_or_the_hub_label_is_reported(
    projects, database, config, clock
):
    register(projects, "alpha", prefix="好")
    register(projects, "bravo", prefix="助手")
    register(projects, "charlie", prefix="c")
    register(projects, "cron-job", push_only=True)  # 没有前缀，不会撞
    changed = with_settings(config, hub_label="助手", confirm_words=("对", "是", "确认", "好"))

    found = Projects(database, changed, clock).prefix_conflicts()

    assert found == [
        PrefixConflict(project="alpha", prefix="好", setting="routing.confirm_words", word="好"),
        PrefixConflict(project="bravo", prefix="助手", setting="hub.label", word="助手"),
    ]


def test_no_conflict_when_nothing_clashes(projects):
    register(projects, "alpha", prefix="a")

    assert projects.prefix_conflicts() == []


def test_a_removed_project_has_no_prefix_to_clash(projects, database, config, clock):
    register(projects, "alpha", prefix="好")
    projects.disable("alpha")
    projects.remove("alpha", yes=True)
    changed = with_settings(config, confirm_words=("好",))

    assert Projects(database, changed, clock).prefix_conflicts() == []


def test_the_conflict_check_ignores_case_and_width(projects, database, config, clock):
    register(projects, "alpha", prefix="Ok")
    changed = with_settings(config, confirm_words=("ＯＫ",))

    (found,) = Projects(database, changed, clock).prefix_conflicts()

    assert found == PrefixConflict("alpha", "Ok", "routing.confirm_words", "ＯＫ")  # 都是原来的样子
    relabeled = Projects(database, with_settings(config, hub_label="ＯＫ"), clock)
    assert relabeled.prefix_conflicts() == [PrefixConflict("alpha", "Ok", "hub.label", "ＯＫ")]
