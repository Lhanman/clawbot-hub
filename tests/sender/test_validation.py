"""发送前的字段校验：正文、优先级、去重键、`on_closed`。校验不过什么都不留下（没有发送记录、不占条数、
不碰腾讯）。规格：`hub-send`「发消息接口」「回复」，`hub-projects`「停用」。
"""

from __future__ import annotations

import pytest

from clawbot_hub.config import Config
from clawbot_hub.projects import ProjectDisabled
from clawbot_hub.sender import Origin, SendInvalid
from tests.sender.conftest import long_text
from tests.support.hub import tweak
from tests.support.threads import run_in_thread


def nothing_was_left_behind(hub) -> None:
    assert hub.rows() == []
    assert hub.used() == {}
    assert hub.upstream.calls == []


# ---------------------------------------------------------------- 正文


@pytest.mark.parametrize("text", ["", "   ", "\n\n", " \t\n "])
def test_a_text_with_nothing_but_blanks_is_empty(hub, text):
    a = hub.ready()

    with pytest.raises(SendInvalid) as caught:
        hub.sender.push(a, text)

    assert caught.value.code == "empty_text"
    assert caught.value.message == "文字不能是空的"
    nothing_was_left_behind(hub)


@pytest.mark.parametrize("text", [None, 123, ["你好"], b"hello"])
def test_a_text_that_is_not_a_string_is_a_bad_request(hub, text):
    a = hub.ready()

    with pytest.raises(SendInvalid) as caught:
        hub.sender.push(a, text)

    assert caught.value.code == "bad_request"
    nothing_was_left_behind(hub)


@pytest.mark.parametrize("text", ["\ud800", "你好\udfff", "\ud83d"])
def test_a_text_that_cannot_be_encoded_is_a_bad_request(hub, text):
    a = hub.ready()  # JSON 里写 "\\ud800" 是合法的，Python 读进来是一个单独的代理字符

    with pytest.raises(SendInvalid) as caught:
        hub.sender.push(a, text)

    assert caught.value.code == "bad_request"
    assert caught.value.message == "text 里有不能编码成 UTF-8 的字符"
    nothing_was_left_behind(hub)


def test_the_longest_allowed_text_is_sent(hub):
    a = hub.ready()

    result = hub.sender.push(a, "字" * 8000)

    assert result.status == "sent"
    assert result.parts == 5  # 每条留出开头的位置，拆成 5 条


def test_one_character_more_is_too_long(hub):
    a = hub.ready()

    with pytest.raises(SendInvalid) as caught:
        hub.sender.push(a, "字" * 8001)

    assert caught.value.code == "text_too_long"
    assert caught.value.message == "文字太长：最多 8000 个字"
    nothing_was_left_behind(hub)


def test_the_limit_is_the_configured_one(make_hub):
    hub = make_hub(tweak(Config(), send={"max_chars": 300, "part_chars": 200}))
    a = hub.ready()

    assert hub.sender.push(a, "字" * 300).status == "sent"
    with pytest.raises(SendInvalid) as caught:
        hub.sender.push(a, "字" * 301)

    assert caught.value.code == "text_too_long"
    assert "300" in caught.value.message


def test_the_limit_counts_the_text_not_the_label(make_hub):
    hub = make_hub()
    hub.bind()
    hub.speak()
    hub.add("alpha")
    beta = hub.add("beta", guaranteed=6, cap=8)  # 标签 beta

    result = hub.sender.push(beta, "字" * 8000)

    assert result.status == "sent"


# ---------------------------------------------------------------- 优先级、去重键、on_closed


@pytest.mark.parametrize("value", ["high", "", "URGENT", None, 1])
def test_an_unknown_priority_is_a_bad_request_that_names_the_choices(hub, value):
    a = hub.ready()

    with pytest.raises(SendInvalid) as caught:
        hub.sender.push(a, "你好", priority=value)

    assert caught.value.code == "bad_request"
    assert caught.value.message == "priority 的值不对，可选：urgent、normal、deferrable"
    nothing_was_left_behind(hub)


@pytest.mark.parametrize("priority", ["urgent", "normal", "deferrable"])
def test_each_priority_is_accepted(hub, priority):
    a = hub.ready()

    assert hub.sender.push(a, "你好", priority=priority).status == "sent"
    assert hub.row(hub.rows()[-1]["id"])["priority"] == priority


@pytest.mark.parametrize("value", ["ignore", "", "SKIP", None, True])
def test_an_unknown_on_closed_is_a_bad_request_that_names_the_choices(hub, value):
    a = hub.ready()

    with pytest.raises(SendInvalid) as caught:
        hub.sender.push(a, "你好", on_closed=value)

    assert caught.value.code == "bad_request"
    assert caught.value.message == "on_closed 的值不对，可选：fail、skip"
    nothing_was_left_behind(hub)


@pytest.mark.parametrize("key", ["", "k" * 129, 5, ["k"], "k\ud800"])
def test_a_dedupe_key_of_the_wrong_length_or_type_is_a_bad_request(hub, key):
    a = hub.ready()

    with pytest.raises(SendInvalid) as caught:
        hub.sender.push(a, "你好", dedupe_key=key)

    assert caught.value.code == "bad_request"
    assert caught.value.message == "dedupe_key 要是 1 到 128 个字符的字符串"
    nothing_was_left_behind(hub)


@pytest.mark.parametrize("length", [1, 128])
def test_the_shortest_and_longest_dedupe_keys_are_fine(hub, length):
    a = hub.ready()

    assert hub.sender.push(a, "你好", dedupe_key="k" * length).status == "sent"


def test_the_messages_do_not_repeat_what_the_project_sent(hub):
    a = hub.ready()

    for bad in ({"priority": "SECRET-1"}, {"on_closed": "SECRET-2"}, {"dedupe_key": "S" * 129}):
        with pytest.raises(SendInvalid) as caught:
            hub.sender.push(a, "你好", **bad)
        assert "SECRET" not in caught.value.message and "SSSS" not in caught.value.message


def test_validation_comes_before_the_dedupe_check(hub):
    a = hub.ready()
    first = hub.sender.push(a, "你好", dedupe_key="same")

    with pytest.raises(SendInvalid) as caught:
        hub.sender.push(a, "   ", dedupe_key="same")

    assert caught.value.code == "empty_text"
    assert first.status == "sent"
    assert len(hub.upstream.sent) == 1


# ---------------------------------------------------------------- 回复的校验与项目状态


def test_a_reply_is_checked_the_same_way(hub):
    hub.ready()
    author = Origin("_hub", "服务")

    with pytest.raises(SendInvalid) as empty:
        hub.sender.reply(author, "  ", context_token="ctx-1")
    with pytest.raises(SendInvalid) as long:
        hub.sender.reply(author, "字" * 8001, context_token="ctx-1")

    assert (empty.value.code, long.value.code) == ("empty_text", "text_too_long")
    nothing_was_left_behind(hub)


@pytest.mark.parametrize("token", ["", None])
def test_a_reply_without_a_conversation_credential_is_a_programming_error(hub, token):
    hub.ready()

    with pytest.raises(ValueError, match="对话凭证"):
        hub.sender.reply(Origin("_hub", "服务"), "你好", context_token=token)

    nothing_was_left_behind(hub)


def test_a_project_disabled_after_it_was_recognised_cannot_push(hub):
    a = hub.ready()
    hub.projects.disable("alpha")

    with pytest.raises(ProjectDisabled) as caught:
        hub.sender.push(a, "你好")

    assert caught.value.project.name == "alpha"
    assert caught.value.project.enabled is False
    nothing_was_left_behind(hub)


def test_a_project_that_does_not_exist_is_a_programming_error(hub):
    hub.ready()

    with pytest.raises(ValueError, match="不存在"):
        hub.sender.push(Origin("ghost", None), "你好")

    nothing_was_left_behind(hub)


def test_a_project_disabled_while_its_message_is_being_sent_gets_that_message_finished(hub):
    a = hub.ready()
    gate = hub.upstream.hold("send_text")
    sending = run_in_thread(hub.sender.push, a, long_text(2))
    assert gate.entered.wait(5)

    hub.projects.disable("alpha")
    gate.release()

    result = sending.result()
    assert (result.status, result.parts_sent) == ("sent", 2)  # 正在发的那条发完
    with pytest.raises(ProjectDisabled):
        hub.sender.push(a, "下一条")
