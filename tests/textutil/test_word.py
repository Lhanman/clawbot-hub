"""词的格式（任务 A2 用到，A6 的前缀规则也用同一套）。

前缀、服务指令词（`hub.label`）、确认词的格式：1 到 8 个字符，只含字母、数字、汉字，
判断前先做 NFKC 规范化（全角和半角一样）。规格见 `hub-projects`「前缀」和 `hub-deploy`「配置」。
"""

from __future__ import annotations

import pytest

from clawbot_hub.textutil import WORD_MAX_CHARS, is_word


@pytest.mark.parametrize(
    "word",
    [
        "对",
        "服务",
        "确认",
        "quant",
        "Quant",
        "量化2",
        "ＡＢＣ",  # 全角字母
        "２０２６",  # 全角数字
        "x²",  # 规范化以后是 x2
        "ab12中文字符",  # 8 个
        "あいう",  # 假名也是字母
    ],
)
def test_letters_digits_and_hanzi_are_words(word):
    assert is_word(word)


@pytest.mark.parametrize(
    "word",
    [
        "",
        " ",
        "a b",
        "a\tb",
        "a\nb",
        "好,的",
        "好，的",
        "好、的",
        "量化:",
        "a-b",
        "a_b",
        "a.b",
        "【量化】",
        "😀",
        "量化😀",
        "ab12中文字符x",  # 9 个
        "ﬃﬃﬃ",  # 规范化以后是 9 个字符
        "́",  # 单独一个组合用的重音符号不是字母
    ],
)
def test_anything_else_is_not_a_word(word):
    assert not is_word(word)


def test_length_is_counted_after_normalisation():
    assert is_word("é")  # 规范化合成一个字符
    assert is_word("ﬁ")  # 规范化以后是 fi，两个字符，仍在 8 以内


def test_the_limit_is_eight_characters():
    assert WORD_MAX_CHARS == 8
    assert is_word("字" * 8)
    assert not is_word("字" * 9)
