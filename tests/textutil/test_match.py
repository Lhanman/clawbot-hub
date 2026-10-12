"""前缀匹配（任务 A1，规格 `hub-inbox`「一句话交给谁」）。

匹配时去掉首尾空白，全角空格当空格，英文不分大小写、全角半角视为相同（按 NFKC 比较）；
分隔符是空白和 `:` `：` `,` `，` `、`。交给项目的文字从原始字符串里按位置切出来，不被 NFKC 改写。
"""

from __future__ import annotations

import pytest

from clawbot_hub.textutil import is_separator, normalize, strip_prefix


@pytest.mark.parametrize("char", list(" \t\n　:：,，、"))
def test_separators(char):
    assert is_separator(char)


@pytest.mark.parametrize("char", list("a字0-_.。;；!！?？/") + ["​"])
def test_not_separators(char):
    assert not is_separator(char)


def test_normalize_folds_width_case_and_edges():
    assert normalize("  Ｘ状态　") == "x状态"
    assert normalize("ＡＢＣ　ｄｅｆ") == "abc def"
    assert normalize("") == ""


# ---------------------------------------------------------------- 命中


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
        "x\n状态",
    ],
)
def test_prefix_followed_by_any_separator(text):
    assert strip_prefix(text, "x") == "状态"


def test_several_separators_in_a_row_are_all_removed():
    assert strip_prefix("x，，  状态", "x") == "状态"
    assert strip_prefix("x:，、　 状态", "x") == "状态"
    assert strip_prefix("x ，：", "x") == ""  # 后面只剩分隔符：文字为空


def test_prefix_alone_gives_an_empty_text():
    assert strip_prefix("x", "x") == ""
    assert strip_prefix("x：  ", "x") == ""
    assert strip_prefix("  x  ", "x") == ""


def test_only_the_separators_right_after_the_prefix_are_removed():
    assert strip_prefix("x 状态，详细：一下", "x") == "状态，详细：一下"


def test_leading_and_trailing_whitespace_is_ignored_including_full_width_spaces():
    assert strip_prefix("　 x 状态 　", "x") == "状态"


def test_case_and_width_are_ignored_when_comparing():
    assert strip_prefix("X 状态", "x") == "状态"
    assert strip_prefix("x 状态", "X") == "状态"
    assert strip_prefix("ｘ 状态", "x") == "状态"  # 全角的 x
    assert strip_prefix("x 状态", "ｘ") == "状态"
    assert strip_prefix("ＱＵＡＮＴ 状态", "quant") == "状态"
    assert strip_prefix("量化 状态", "量化") == "状态"


def test_the_text_handed_over_comes_from_the_original_not_from_the_normalized_form():
    assert strip_prefix("x ＡＢＣ  Def ", "x") == "ＡＢＣ  Def"
    assert strip_prefix("Ｘ：Ｈｅｌｌｏ，ＷＯＲＬＤ", "x") == "Ｈｅｌｌｏ，ＷＯＲＬＤ"


def test_the_cut_follows_original_positions_even_when_normalization_changes_the_length():
    # "ﬁ"（连字，一个字符）按 NFKC 是两个字符 "fi"
    assert strip_prefix("ﬁ 状态", "fi") == "状态"
    assert strip_prefix("fi 状态", "ﬁ") == "状态"
    # "e" 加组合重音符按 NFKC 合成一个字符 "é"
    assert strip_prefix("é 状态", "é") == "状态"
    assert strip_prefix("é 状态", "é") == "状态"


def test_the_service_label_works_as_a_command_word():
    assert strip_prefix("服务 项目", "服务") == "项目"
    assert strip_prefix("服务", "服务") == ""
    assert strip_prefix("服务：状态", "服务") == "状态"


# ---------------------------------------------------------------- 不命中


def test_a_prefix_followed_directly_by_a_character_does_not_count():
    assert strip_prefix("量化因子", "量化") is None
    assert strip_prefix("服务器状态", "服务") is None
    assert strip_prefix("xx 状态", "x") is None
    assert strip_prefix("x1", "x") is None
    assert strip_prefix("x。状态", "x") is None  # 句号不是分隔符


def test_an_unknown_or_shorter_text_does_not_match():
    assert strip_prefix("foo 状态", "x") is None
    assert strip_prefix("", "x") is None
    assert strip_prefix("   ", "x") is None
    assert strip_prefix("量", "量化") is None


def test_the_prefix_must_be_at_the_start():
    assert strip_prefix("请 x 状态", "x") is None
    assert strip_prefix(", x 状态", "x") is None


def test_a_combining_mark_after_the_prefix_is_a_character_not_a_separator():
    assert strip_prefix("éx", "e") is None


def test_an_empty_prefix_is_a_programming_error():
    with pytest.raises(ValueError):
        strip_prefix("x 状态", "")
    with pytest.raises(ValueError):
        strip_prefix("x 状态", "  ")
