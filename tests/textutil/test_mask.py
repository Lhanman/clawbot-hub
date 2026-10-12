"""脱敏（任务 A1）：已知的秘密值和 `chub_` 开头的项目密钥一律换成 `***`。

这是日志过滤器和 `upstream/http.py` 报错信息的共同底层；即使调用方不小心把秘密写进了消息，
也不会落到日志和返回给项目的原因里。
"""

from __future__ import annotations

from clawbot_hub.textutil import MASK, mask

KEY = "chub_" + "A" * 43
OTHER_KEY = "chub_" + "b1_-" * 10 + "xyz"  # 5 + 43 个字符，含 _ 和 -


def test_mask_constant():
    assert MASK == "***"


def test_known_secrets_are_replaced_everywhere():
    text = "bot_token=tok123 ctx=ctx-9 again tok123"
    assert mask(text, ["tok123", "ctx-9"]) == "bot_token=*** ctx=*** again ***"


def test_project_keys_are_replaced_without_being_listed():
    assert mask(f"Authorization: Bearer {KEY} ok", []) == "Authorization: Bearer *** ok"
    assert mask(f"a {KEY} b {OTHER_KEY} c") == "a *** b *** c"
    assert len(OTHER_KEY) == 48


def test_only_the_key_shape_is_replaced_not_similar_words():
    assert mask("chub_ is a prefix, chub_short too") == "chub_ is a prefix, chub_short too"


def test_the_key_format_is_the_prefix_plus_exactly_43_characters():
    assert mask(KEY + "extra") == "***extra"  # 多出来的部分不属于密钥
    assert mask("chub_" + "A" * 42) == "chub_" + "A" * 42  # 少一个字符就不是


def test_empty_and_none_secrets_are_ignored():
    assert mask("plain text", ["", None]) == "plain text"


def test_longer_secrets_go_first_so_nothing_of_them_is_left():
    assert mask("abcdef", ["abc", "abcdef"]) == "***"
    assert mask("xx abcdef yy", ["abcdef", "abc"]) == "xx *** yy"


def test_secrets_that_contain_regex_characters_are_matched_literally():
    assert mask("tok.+*(1)", ["tok.+*(1)"]) == "***"
    assert mask("tokX+*(1)", ["tok.+*(1)"]) == "tokX+*(1)"


def test_text_without_secrets_is_unchanged():
    assert mask("正常的一句话", ["secret"]) == "正常的一句话"
    assert mask("") == ""


def test_masking_twice_changes_nothing():
    once = mask(f"{KEY} tok", ["tok"])
    assert mask(once, ["tok"]) == once
