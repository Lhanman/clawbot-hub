"""路由（任务 C1，规格 `hub-inbox`「一句话交给谁」「等确认」）。

路由是纯函数：给它一句话（文字、有没有文字项）和项目表的快照（启用、默认、前缀、在线、等确认标记），
它说这句话交给哪个项目（`ToProject`，带路由方式 `via`），或者由服务自己回复（`HubReply`）。
不碰数据库、不碰时钟：入队、限频、清标记都是调用者（poller）的事。
"""

from __future__ import annotations

import itertools

import pytest

from clawbot_hub.projects import Project
from clawbot_hub.router import HubReply, Reply, Snapshot, ToProject, route

NOW = 1_800_000_000_000
_ids = itertools.count(1)


def make(
    name: str,
    prefix: str | None = None,
    *,
    default: bool = False,
    enabled: bool = True,
    push_only: bool = False,
    removed: bool = False,
    awaiting: int | None = None,
) -> Project:
    return Project(
        id=next(_ids),
        name=name,
        prefix=prefix,
        guaranteed=1,
        cap=3,
        is_default=default,
        label=prefix,
        enabled=enabled,
        push_only=push_only,
        created_ms=0,
        removed_ms=0 if removed else None,
        awaiting_until_ms=awaiting,
    )


def world(
    *projects: Project,
    online: tuple[str, ...] | None = None,
    label: str = "服务",
    words: tuple[str, ...] = ("对", "是", "确认"),
) -> Snapshot:
    """`online` 不写就是所有启用着的、能收话的项目都在线。"""
    if online is None:
        online = tuple(p.name for p in projects if p.enabled and not p.push_only)
    return Snapshot(
        projects=tuple(projects),
        online=frozenset(online),
        label=label,
        confirm_words=words,
    )


QUANT = make("ashare-quant", "量化", default=True)
X = make("x", "x")
Y = make("y", "y")


def delivered(project: Project, text: str, via: str, *, has_text: bool = True, clear=()):
    return ToProject(project, text, has_text, via, clear_awaiting=tuple(clear))


def say(text: str, snapshot: Snapshot, has_text: bool = True):
    return route(text, has_text, snapshot, NOW)


# ---------------------------------------------------------------- 默认项目


def test_plain_text_goes_to_the_default_project():
    assert say("持仓", world(QUANT, X)) == delivered(QUANT, "持仓", "default")


@pytest.mark.parametrize("text", ["  持仓  ", "　持仓　", "\t持仓\n", "　 持仓 　", " 持仓"])
def test_text_without_prefix_is_stripped_of_edge_whitespace(text):
    assert say(text, world(QUANT)) == delivered(QUANT, "持仓", "default")


def test_inner_whitespace_and_width_are_left_alone():
    # 去的只是首尾空白；NFKC 只用来比较，不改写交给项目的文字
    assert say("查   持仓", world(QUANT)).text == "查   持仓"
    assert say(" ＡＢＣ　１２３ ", world(QUANT)).text == "ＡＢＣ　１２３"


def test_whitespace_only_text_is_still_text():
    assert say("   ", world(QUANT)) == delivered(QUANT, "", "default", has_text=True)
    assert say("", world(QUANT)) == delivered(QUANT, "", "default", has_text=True)


def test_unknown_prefix_hands_the_whole_text_to_the_default_project():
    assert say("foo 状态", world(QUANT, X)) == delivered(QUANT, "foo 状态", "default")


def test_a_prefix_in_the_middle_does_not_count():
    assert say("查 x 状态", world(QUANT, X)) == delivered(QUANT, "查 x 状态", "default")


# ---------------------------------------------------------------- 前缀


@pytest.mark.parametrize(
    "text",
    [
        "x 状态",
        "X：状态",
        "x，状态",
        "x、状态",
        "x:状态",
        "x,状态",
        "x　状态",
        "x\t状态",
        "  x 状态  ",
    ],
)
def test_prefix_with_any_separator(text):
    assert say(text, world(QUANT, X)) == delivered(X, "状态", "prefix")


@pytest.mark.parametrize("text", ["Ｘ 状态", "ｘ：状态", "Ｘ　状态", "ｘ，状态"])
def test_prefix_matches_by_nfkc_and_case(text):
    assert say(text, world(QUANT, X)) == delivered(X, "状态", "prefix")


def test_mixed_case_prefix_is_compared_without_case():
    ab = make("ab-project", "Ab")
    assert say("aB:hi", world(QUANT, ab)) == delivered(ab, "hi", "prefix")
    assert say("ＡＢ，hi", world(QUANT, ab)) == delivered(ab, "hi", "prefix")


def test_several_separators_in_a_row_are_all_removed():
    assert say("x，，  状态", world(QUANT, X)) == delivered(X, "状态", "prefix")
    assert say("x:，、　 状态", world(QUANT, X)) == delivered(X, "状态", "prefix")


@pytest.mark.parametrize("text", ["x", "x：  ", "  x  ", "x ，：", "X"])
def test_prefix_alone_gives_empty_text_but_has_text(text):
    assert say(text, world(QUANT, X)) == delivered(X, "", "prefix", has_text=True)


def test_text_after_the_prefix_is_cut_from_the_original():
    assert say("x ＡＢＣ　１２３ ", world(QUANT, X)) == delivered(X, "ＡＢＣ　１２３", "prefix")
    assert say("x 查   持仓", world(QUANT, X)).text == "查   持仓"


def test_default_project_can_also_be_addressed_by_prefix():
    assert say("量化 状态", world(QUANT, X)) == delivered(QUANT, "状态", "prefix")


@pytest.mark.parametrize("text", ["量化因子", "x状态", "xx 状态", "量化。状态"])
def test_prefix_followed_directly_by_a_character_is_not_a_prefix(text):
    result = say(text, world(QUANT, X))
    assert result == delivered(QUANT, text, "default")


def test_the_longer_prefix_wins_because_the_shorter_one_needs_a_separator():
    quant2 = make("quant-2", "量化二")
    snapshot = world(QUANT, quant2)
    assert say("量化二 状态", snapshot) == delivered(quant2, "状态", "prefix")
    assert say("量化二", snapshot) == delivered(quant2, "", "prefix")
    assert say("量化 状态", snapshot) == delivered(QUANT, "状态", "prefix")
    assert say("量化二状态", snapshot) == delivered(QUANT, "量化二状态", "default")


def test_prefix_of_a_disabled_project_gets_a_notice_instead():
    off = make("x", "x", enabled=False)
    result = say("x 状态", world(QUANT, off))
    assert result == HubReply(Reply.DISABLED, project=off)


def test_projects_that_cannot_receive_are_not_routed_to():
    pusher = make("pusher", "p", push_only=True)
    gone = make("gone", "g", removed=True)
    snapshot = world(QUANT, pusher, gone)
    assert say("p 状态", snapshot) == delivered(QUANT, "p 状态", "default")
    assert say("g 状态", snapshot) == delivered(QUANT, "g 状态", "default")


def test_a_receiver_without_a_prefix_is_skipped_by_the_prefix_step():
    # 登记时能收话的项目一定有前缀；库被手工改坏了也不能因此出错
    bare = make("bare", None)
    assert say("bare 状态", world(QUANT, bare)) == delivered(QUANT, "bare 状态", "default")


# ---------------------------------------------------------------- 图片、没有默认项目、默认项目停用


def test_no_text_goes_to_the_default_project():
    assert say("", world(QUANT, X), has_text=False) == delivered(
        QUANT, "", "default", has_text=False
    )


def test_no_text_ignores_whatever_text_was_passed():
    assert say("x 状态", world(QUANT, X), has_text=False) == delivered(
        QUANT, "", "default", has_text=False
    )


def test_no_text_without_a_default_project():
    assert say("", world(X), has_text=False) == HubReply(Reply.NO_DEFAULT)


def test_no_text_with_a_disabled_default_project():
    off = make("ashare-quant", "量化", default=True, enabled=False)
    assert say("", world(off, X), has_text=False) == HubReply(Reply.DISABLED, project=off)


def test_no_default_project():
    assert say("持仓", world(X, Y)) == HubReply(Reply.NO_DEFAULT)
    assert say("持仓", world()) == HubReply(Reply.NO_DEFAULT)


def test_prefix_still_works_without_a_default_project():
    assert say("x 状态", world(X, Y)) == delivered(X, "状态", "prefix")


def test_disabled_default_project():
    off = make("ashare-quant", "量化", default=True, enabled=False)
    snapshot = world(off, X)
    assert say("持仓", snapshot) == HubReply(Reply.DISABLED, project=off)
    assert say("量化 状态", snapshot) == HubReply(Reply.DISABLED, project=off)
    assert say("x 状态", snapshot) == delivered(X, "状态", "prefix")


def test_a_default_project_that_only_pushes_is_no_default():
    # 登记时不允许这样，但路由不能因此把话交给一个收不了话的项目
    pusher = make("pusher", None, default=True, push_only=True)
    assert say("持仓", world(pusher)) == HubReply(Reply.NO_DEFAULT)


# ---------------------------------------------------------------- 服务自己的指令


@pytest.mark.parametrize(
    "text",
    ["服务 项目", "服务：项目", "服务，项目", "服务、项目", " 服务  项目 ", "　服务　项目　"],
)
def test_hub_command_projects(text):
    assert say(text, world(QUANT, X)) == HubReply(Reply.PROJECTS)


@pytest.mark.parametrize("text", ["服务 状态", "服务:状态", "服务,状态", "\t服务\t状态\n"])
def test_hub_command_status(text):
    assert say(text, world(QUANT, X)) == HubReply(Reply.STATUS)


@pytest.mark.parametrize(
    "text",
    ["服务", "服务 重启", "服务：", "服务 项目 状态", "服务 状态 详情", "服务 项目：", "服务  "],
)
def test_other_text_after_the_label_gets_the_help_line(text):
    assert say(text, world(QUANT, X)) == HubReply(Reply.UNKNOWN_COMMAND)


@pytest.mark.parametrize("text", ["服务器状态", "服务项目", "服务状态", "服务器"])
def test_label_followed_directly_by_a_character_is_not_a_command(text):
    assert say(text, world(QUANT)) == delivered(QUANT, text, "default")


def test_hub_command_words_are_compared_by_nfkc():
    assert say("服务 項目", world(QUANT)) == HubReply(Reply.UNKNOWN_COMMAND)  # 繁体不是同一个词
    # 部首字符 U+2F6C 按 NFKC 就是"目"；指令词和前缀、标签一样按 NFKC 比较
    assert say("服务 项\u2f6c", world(QUANT)) == HubReply(Reply.PROJECTS)


def test_changing_the_label_changes_the_command_word():
    snapshot = world(QUANT, label="助手")
    assert say("助手 状态", snapshot) == HubReply(Reply.STATUS)
    assert say("助手 项目", snapshot) == HubReply(Reply.PROJECTS)
    assert say("服务 状态", snapshot) == delivered(QUANT, "服务 状态", "default")


def test_a_latin_label_is_matched_without_case_and_width():
    snapshot = world(QUANT, label="Hub")
    assert say("hub 状态", snapshot) == HubReply(Reply.STATUS)
    assert say("ＨＵＢ：项目", snapshot) == HubReply(Reply.PROJECTS)
    assert say("hubs 状态", snapshot) == delivered(QUANT, "hubs 状态", "default")


def test_hub_command_does_not_need_a_default_project_and_survives_a_disabled_one():
    off = make("ashare-quant", "量化", default=True, enabled=False)
    assert say("服务 项目", world(X)) == HubReply(Reply.PROJECTS)
    assert say("服务 状态", world(off)) == HubReply(Reply.STATUS)


def test_hub_command_does_not_touch_waiting_markers():
    waiting = make("x", "x", awaiting=NOW - 1)  # 已过期的标记，但不是它被清的时刻
    assert say("服务 状态", world(QUANT, waiting)) == HubReply(Reply.STATUS)


# ---------------------------------------------------------------- 确认话


def waiting(name: str, prefix: str | None, seconds: int = 60, **kwargs) -> Project:
    return make(name, prefix, awaiting=NOW + seconds * 1000, **kwargs)


@pytest.mark.parametrize("word", ["对", "是", "确认"])
def test_confirm_word_goes_to_the_only_project_that_waits(word):
    asking = waiting("x", "x")
    assert say(word, world(QUANT, asking)) == delivered(asking, word, "confirm")


def test_confirm_word_to_the_default_project_is_still_via_confirm():
    asking = waiting("ashare-quant", "量化", default=True)
    assert say("对", world(asking, X)) == delivered(asking, "对", "confirm")


def test_confirm_word_is_the_whole_sentence_with_edge_whitespace_removed():
    asking = waiting("x", "x")
    assert say("  对　", world(QUANT, asking)) == delivered(asking, "对", "confirm")


@pytest.mark.parametrize("text", ["ok", "OK", " Ok ", "ｏｋ", "ＯＫ"])
def test_confirm_words_are_compared_by_nfkc_and_case(text):
    asking = waiting("x", "x")
    result = say(text, world(QUANT, asking, words=("OK", "好的")))
    assert result == delivered(asking, text.strip(), "confirm")


def test_configured_confirm_words_replace_the_defaults():
    asking = waiting("x", "x")
    snapshot = world(QUANT, asking, words=("好的",))
    assert say("好的", snapshot) == delivered(asking, "好的", "confirm")
    assert say("对", snapshot) == delivered(QUANT, "对", "default", clear=())


@pytest.mark.parametrize(
    "text", ["对的", "对对", "对。", "不对", "是不是", "确认一下", "对 对", "好"]
)
def test_anything_but_the_exact_word_is_an_ordinary_sentence(text):
    asking = waiting("x", "x")
    assert say(text, world(QUANT, asking)) == delivered(QUANT, text, "default")


def test_prefix_beats_the_confirm_word():
    asking = waiting("x", "x")
    # 带了前缀就是指名道姓，不管谁在等
    assert say("量化 对", world(QUANT, asking)) == delivered(QUANT, "对", "prefix")
    assert say("x 对", world(QUANT, asking)) == delivered(asking, "对", "prefix")


def test_several_projects_waiting_ask_the_user_to_name_one():
    a = waiting("ashare-quant", "量化", default=True)
    b = waiting("x", "x")
    result = say("对", world(a, b, Y))
    assert result == HubReply(Reply.CONFIRM_AMBIGUOUS, awaiting=(a, b), word="对")
    # 例子里的词是使用者发的那个
    assert say(" 确认 ", world(a, b)).word == "确认"


def test_the_ambiguity_reply_lists_projects_in_registration_order():
    a = waiting("x", "x")
    b = waiting("ashare-quant", "量化", default=True)
    assert say("对", world(a, b)).awaiting == (a, b)
    assert say("对", world(b, a)).awaiting == (b, a)


def test_naming_one_of_several_waiting_projects_is_a_prefix_route():
    a = waiting("ashare-quant", "量化", default=True)
    b = waiting("x", "x")
    assert say("量化 对", world(a, b)) == delivered(a, "对", "prefix")


def test_no_one_waiting_hands_the_confirm_word_to_the_default_project():
    assert say("对", world(QUANT, X)) == delivered(QUANT, "对", "default")


def test_no_one_waiting_and_no_default_project():
    assert say("对", world(X)) == HubReply(Reply.NO_DEFAULT)


def test_no_one_waiting_and_the_default_project_disabled():
    off = make("ashare-quant", "量化", default=True, enabled=False)
    assert say("对", world(off, X)) == HubReply(Reply.DISABLED, project=off)


# 标记不管用的几种情形：都当作没人在等，并告诉调用者这几个标记要清掉


def test_marker_that_has_expired_is_ignored_and_reported():
    old = make("x", "x", awaiting=NOW - 1)
    assert say("对", world(QUANT, old)) == delivered(QUANT, "对", "default", clear=["x"])


def test_marker_expires_exactly_at_its_deadline():
    edge = make("x", "x", awaiting=NOW)
    just_before = make("x", "x", awaiting=NOW + 1)
    assert say("对", world(QUANT, edge)).via == "default"
    assert say("对", world(QUANT, just_before)).via == "confirm"


def test_marker_of_an_offline_project_is_ignored_and_reported():
    asking = waiting("x", "x")
    snapshot = world(QUANT, asking, online=("ashare-quant",))
    assert say("对", snapshot) == delivered(QUANT, "对", "default", clear=["x"])


def test_marker_of_a_disabled_project_is_ignored_and_reported():
    asking = waiting("x", "x", enabled=False)
    assert say("对", world(QUANT, asking)) == delivered(QUANT, "对", "default", clear=["x"])


def test_marker_of_a_disabled_project_is_ignored_even_if_it_still_looks_online():
    # 刚停用、在线判断还没跟上的瞬间：停用本身就让标记失效，不靠"不在线"
    asking = waiting("x", "x", enabled=False)
    snapshot = world(QUANT, asking, online=("ashare-quant", "x"))
    assert say("对", snapshot) == delivered(QUANT, "对", "default", clear=["x"])


def test_marker_of_a_project_that_cannot_receive_is_ignored():
    pusher = waiting("pusher", None, push_only=True)
    gone = waiting("gone", None, removed=True)
    snapshot = world(QUANT, pusher, gone, online=("pusher", "gone", "ashare-quant"))
    assert say("对", snapshot) == delivered(QUANT, "对", "default")


def test_stale_markers_are_reported_next_to_a_delivery_to_the_one_that_waits():
    asking = waiting("ashare-quant", "量化", default=True)
    old = make("x", "x", awaiting=NOW - 5)
    off = waiting("y", "y")
    snapshot = world(asking, old, off, online=("ashare-quant", "x"))
    assert say("对", snapshot) == delivered(asking, "对", "confirm", clear=["x", "y"])


def test_stale_markers_are_reported_next_to_the_notice_for_a_missing_default_project():
    old = make("x", "x", awaiting=NOW - 1)
    assert say("对", world(old)) == HubReply(Reply.NO_DEFAULT, clear_awaiting=("x",))


def test_stale_markers_are_reported_next_to_the_notice_for_a_disabled_default_project():
    off = make("ashare-quant", "量化", default=True, enabled=False)
    old = make("x", "x", awaiting=NOW - 1)
    assert say("对", world(off, old)) == HubReply(
        Reply.DISABLED, project=off, clear_awaiting=("x",)
    )


def test_stale_markers_are_reported_next_to_the_ambiguity_reply():
    a = waiting("ashare-quant", "量化", default=True)
    b = waiting("x", "x")
    old = make("y", "y", awaiting=NOW - 5)
    result = say("对", world(a, b, old))
    assert result == HubReply(
        Reply.CONFIRM_AMBIGUOUS, awaiting=(a, b), word="对", clear_awaiting=("y",)
    )


def test_stale_markers_are_only_reported_when_a_confirm_word_arrives():
    old = make("x", "x", awaiting=NOW - 1)
    snapshot = world(QUANT, old)
    for text in ["持仓", "x 状态", "量化 状态", "服务 状态", "对的"]:
        assert say(text, snapshot).clear_awaiting == ()
    assert say("", snapshot, has_text=False).clear_awaiting == ()


def test_a_live_marker_is_never_reported_as_stale():
    asking = waiting("x", "x")
    assert say("对", world(QUANT, asking)).clear_awaiting == ()


# ---------------------------------------------------------------- 纯函数


def test_route_does_not_change_its_inputs():
    asking = waiting("x", "x")
    snapshot = world(QUANT, asking)
    before = (snapshot.projects, snapshot.online, snapshot.label, snapshot.confirm_words)
    say("对", snapshot)
    say("x 状态", snapshot)
    assert (snapshot.projects, snapshot.online, snapshot.label, snapshot.confirm_words) == before
