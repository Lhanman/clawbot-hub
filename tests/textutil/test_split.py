"""拆条和标题（任务 A1）。

前半是从 ashare-quant 的 `tests/notify/test_split.py` 搬来的，行为逐条对上（`utf8_size` 不搬：
hub 里没有按字节拆的地方）；后半是规格 `hub-send`「项目标签」「拆条」要求的新增部分。
"""

from __future__ import annotations

import random

import pytest

from clawbot_hub.textutil import (
    TITLE_MAX_CHARS,
    add_label,
    body_budget,
    build_parts,
    clip_title,
    headline,
    part_head,
    split_message,
)

# ---------------------------------------------------------------- 从 ashare-quant 搬来


def test_long_brief_splits_on_newlines():
    """5000 字、每行不超过 200 字，按 4096 字拆成两条，都在换行处断开，拼回等于原文。"""
    lines = [f"第{i:03d}行" + "字" * 150 for i in range(32)]
    text = "\n".join(lines)
    assert 4096 < len(text) < 6000

    parts = split_message(text, 4096)

    assert len(parts) == 2
    assert "".join(parts) == text
    assert all(len(p) <= 4096 for p in parts)
    assert parts[0].endswith("\n")


def test_single_overlong_line_cut_in_the_middle():
    text = "短行\n" + "长" * 5000 + "\n尾巴"

    parts = split_message(text, 4096)

    assert "".join(parts) == text
    assert all(len(p) <= 4096 for p in parts)
    assert parts[0] == "短行\n"  # 先在换行处断开，长行自己再切
    assert len(parts) == 3


def test_exactly_limit_is_one_part():
    text = "x" * 4096

    assert split_message(text, 4096) == [text]


def test_exactly_limit_with_newlines_is_still_one_part():
    """刚好等于上限、中间有换行：照样是一条，不会在换行处多拆一刀；多一个字才拆。"""
    text = "ab\ncd"

    assert split_message(text, 5) == [text]
    assert split_message(text, 4) == ["ab\n", "cd"]


def test_empty_message():
    assert split_message("", 4096) == []


@pytest.mark.parametrize("limit", [0, -1])
def test_a_limit_that_cannot_hold_one_character_is_an_error_not_an_endless_loop(limit):
    with pytest.raises(ValueError):
        split_message("abc", limit)


@pytest.mark.parametrize("limit", [5, 7, 10])
def test_small_limit_round_trips(limit):
    text = "ab\ncdefghijkl\n\nmn\n"
    parts = split_message(text, limit)

    assert "".join(parts) == text
    assert all(0 < len(p) <= limit for p in parts)


@pytest.mark.parametrize(
    ("text", "title", "rest"),
    [
        ("盘前简报 · 9月29日\n\n大盘  纳指 −0.9%\n", "盘前简报 · 9月29日", "大盘  纳指 −0.9%\n"),
        ("只有一行", "只有一行", "只有一行"),
        ("标题\n", "标题", "标题"),
    ],
)
def test_headline(text, title, rest):
    """第一行当标题，其余（去掉开头的空行）当内容；只有一行时两者都用这一行。"""
    assert headline(text) == (title, rest)


# ---------------------------------------------------------------- 标题和开头


def test_title_limit_is_thirty_characters():
    assert TITLE_MAX_CHARS == 30


def test_clip_title_keeps_short_titles_and_cuts_long_ones_with_an_ellipsis():
    assert clip_title("盘点") == "盘点"
    assert clip_title("字" * 30) == "字" * 30  # 正好 30 个字不动
    assert clip_title("字" * 31) == "字" * 30 + "…"
    assert clip_title("字" * 5000) == "字" * 30 + "…"
    assert clip_title("") == ""


def test_part_head_format_is_the_same_as_ashare_quant():
    assert part_head("盘点", 2, 3) == "盘点（第 2/3 条）\n"
    assert part_head("【x】盘点", 3, 10) == "【x】盘点（第 3/10 条）\n"


def test_add_label_puts_the_label_in_front_of_the_first_line_on_the_same_line():
    assert add_label("盘点完成\n共 3 项", "x") == "【x】盘点完成\n共 3 项"
    assert add_label("原样", "") == "原样"  # 没有标签的项目原样发
    assert add_label("  前后有空白  \n", "x") == "【x】  前后有空白  \n"  # 原文不去空白


def test_body_budget_leaves_room_for_the_head_but_never_less_than_half():
    assert body_budget(2000, 43) == 1957
    assert body_budget(200, 150) == 100  # 开头太长时，至少留一半
    assert body_budget(201, 150) == 100  # 取整


# ---------------------------------------------------------------- 整条消息怎么拆


def strip_heads(parts: list[str], title: str) -> str:
    """去掉第 2 条起的开头，拼回正文。"""
    n = len(parts)
    pieces = [parts[0]]
    for i, part in enumerate(parts[1:], start=2):
        head = part_head(clip_title(title), i, n)
        assert part.startswith(head), (part[:60], head)
        pieces.append(part[len(head) :])
    return "".join(pieces)


def lines_text(first_line: str, total: int) -> str:
    """第一行是 `first_line`，后面是每行 100 个字（含换行）的正文，全文共 `total` 个字。"""
    head = first_line + "\n"
    body = total - len(head)
    full, rest = divmod(body, 100)
    return head + ("字" * 99 + "\n") * full + "字" * rest


def test_short_message_is_one_part_without_a_head():
    text = "盘点完成\n" + "字" * 1494  # 1500 个字
    assert build_parts(text, 2000, 10) == [text]


TEN = "一二三四五六七八九十"


def test_whole_text_within_the_limit_does_not_pay_for_a_head():
    """第一行 10 个字、全文 1990 个字（不超 2000，但超过 2000 减开头的字数）：一条，无开头。"""
    text = lines_text(TEN, 1990)
    assert len(text) == 1990
    assert build_parts(text, 2000, 10) == [text]


def test_exactly_the_limit_is_still_one_part():
    text = lines_text(TEN, 2000)
    assert build_parts(text, 2000, 10) == [text]


def test_just_over_the_limit_makes_two_parts_and_the_second_has_a_head():
    text = lines_text(TEN, 2001)
    assert len(text) == 2001
    parts = build_parts(text, 2000, 10)
    assert len(parts) == 2
    assert parts[1].startswith(f"{TEN}（第 2/2 条）\n")
    assert all(len(p) <= 2000 for p in parts)
    assert strip_heads(parts, TEN) == text


def test_long_message_splits_by_lines_into_three_parts():
    """4500 个字、每行约 50 个字：拆成 3 条，不在行中间断开。"""
    lines = [f"第{i:03d}行" + "字" * 44 for i in range(90)]  # 每行 50 个字（含换行）
    text = "盘点\n" + "\n".join(lines)
    assert 4400 < len(text) < 4600

    parts = build_parts(text, 2000, 10)

    assert len(parts) == 3
    assert parts[1].startswith("盘点（第 2/3 条）\n") and parts[2].startswith("盘点（第 3/3 条）\n")
    assert all(len(p) <= 2000 for p in parts)
    assert strip_heads(parts, "盘点") == text
    for part in parts[:-1]:
        assert part.endswith("\n")  # 都在换行处断开


def test_one_very_long_line_is_cut_on_character_boundaries_and_titled_with_its_first_30_chars():
    text = "长" * 5000
    parts = build_parts(text, 2000, 10)
    assert all(len(p) <= 2000 for p in parts)
    assert parts[1].startswith("长" * 30 + "…（第 2/")
    assert strip_heads(parts, headline(text)[0]) == text


def test_label_goes_into_the_head_of_every_later_part():
    text = add_label(lines_text("盘点", 2500), "x")
    parts = build_parts(text, 2000, 10)
    assert len(parts) == 2
    assert parts[1].startswith("【x】盘点（第 2/2 条）\n")


def test_head_room_is_estimated_with_the_window_limit_not_the_real_part_count():
    """正文预算 = max(part_chars − 开头的字数, part_chars // 2)，开头按 n = window_limit 估。"""
    text = "长" * 1000  # 一行，标题取前 30 个字加"…"
    title = clip_title(headline(text)[0])
    budget = body_budget(200, len(part_head(title, 10, 10)))
    assert budget == 200 - (31 + len("（第 10/10 条）\n"))

    parts = build_parts(text, 200, 10)

    assert len(parts[0]) == budget  # 第一段（没有开头）也按同一个预算切
    assert all(len(p) <= 200 for p in parts)
    assert len(parts) == 7  # 1000 ÷ 157，向上取整


def test_nothing_to_send_is_no_parts():
    assert build_parts("", 2000, 10) == []


def test_text_is_never_stripped_or_changed():
    text = "  盘点  \n\n" + "字 " * 1500 + "\n\n"
    parts = build_parts(text, 2000, 10)
    assert strip_heads(parts, "盘点") == text


@pytest.mark.parametrize("part_chars", [200, 201, 500, 2000])
@pytest.mark.parametrize("window_limit", [1, 2, 10, 99])
def test_every_part_fits_and_the_text_comes_back(part_chars, window_limit):
    rng = random.Random(part_chars * 1000 + window_limit)
    alphabet = "字符abc 0123456789，。\n\n\n"
    for _ in range(25):
        size = rng.choice([1, 50, part_chars - 1, part_chars, part_chars + 1, part_chars * 5])
        text = "".join(rng.choice(alphabet) for _ in range(size))
        title = clip_title(headline(text)[0])
        parts = build_parts(text, part_chars, window_limit)
        assert all(len(p) <= part_chars for p in parts), (size, [len(p) for p in parts])
        if text:
            assert strip_heads(parts, headline(text)[0]) == text
            if len(text) <= part_chars:
                assert parts == [text]
            else:
                assert len(parts) >= 2 and parts[1].startswith(title)
