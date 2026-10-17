"""能不能编码成 UTF-8（`textutil.is_encodable`）：接口收到的字符串先过这一关。"""

from __future__ import annotations

import json

import pytest

from clawbot_hub.textutil import is_encodable


@pytest.mark.parametrize("text", ["", "abc", "持仓", "😀", "\u0000", "a\nb"])
def test_ordinary_text_can_be_encoded(text):
    assert is_encodable(text)


@pytest.mark.parametrize("text", ["\ud800", "x\udfffy", "\ud83d"])
def test_a_lone_surrogate_cannot(text):
    assert not is_encodable(text)


def test_json_can_carry_a_lone_surrogate_which_is_why_the_check_exists():
    assert not is_encodable(json.loads('"\\ud800"'))
