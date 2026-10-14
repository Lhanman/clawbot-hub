"""项目标签和拆条，从发送接口到假上游走一遍。规格：`hub-send`「项目标签」「拆条」。
（拆条的纯计算在 `tests/textutil/` 里测过，这里看的是：标签加在拆条之前、每一条各发一次、
条数就是占的额度。）
"""

from __future__ import annotations

import re

import pytest

from clawbot_hub.config import Config
from clawbot_hub.sender import Origin
from tests.support.hub import tweak

PART_CHARS = 2000


def lined(total: int, *, title: str = "盘点", line: int = 50) -> str:
    """第一行是标题、其余每行 `line` 个字（含换行）的文字，总共恰好 `total` 个字
    （最后一行不够长就短一点，不带换行）。"""
    head = title + "\n"
    rows: list[str] = []
    left = total - len(head)
    while left > 0:
        size = min(line, left)
        rows.append(
            f"{len(rows):04d}".ljust(size - (size == line), "字") + ("\n" if size == line else "")
        )
        left -= size
    text = head + "".join(rows)
    assert len(text) == total, (len(text), total)
    return text


def rebuilt(texts: list[str], title: str) -> str:
    """把发出去的几条拼回去：第 2 条起去掉开头 `<标题>（第 i/n 条）` 那一行。"""
    count = len(texts)
    pieces = [texts[0]]
    for number, text in enumerate(texts[1:], start=2):
        header = f"{title}（第 {number}/{count} 条）\n"
        assert text.startswith(header), (number, text[:60])
        pieces.append(text[len(header) :])
    return "".join(pieces)


@pytest.fixture
def hub(hub):
    hub.ready()
    return hub


# ---------------------------------------------------------------- 标签


def test_a_project_with_a_label_gets_it_on_the_first_line(hub):
    x = hub.add("xray", label="x")

    hub.sender.push(x, "盘点完成\n共 3 项")

    assert hub.texts == ["【x】盘点完成\n共 3 项"]


def test_the_default_project_sends_the_text_as_it_is(hub):
    alpha = hub.project("alpha")

    hub.sender.push(alpha, "盘前简报\n……")

    assert hub.texts == ["盘前简报\n……"]


def test_the_service_replies_with_its_own_label(hub):
    hub.sender.reply(Origin("_hub", "服务"), "收到", context_token="ctx-1")

    assert hub.texts == ["【服务】收到"]


def test_the_original_is_kept_in_every_other_way(hub):
    x = hub.add("xray", label="x")
    text = "  盘点 \n\n 末尾有空行\n\n"

    hub.sender.push(x, text)

    assert hub.texts == ["【x】  盘点 \n\n 末尾有空行\n\n"]


def test_the_record_counts_the_characters_the_project_sent_not_the_label(hub):
    x = hub.add("xray", label="x")

    result = hub.sender.push(x, "盘点完成\n共 3 项")

    assert hub.row(result.id)["chars"] == len("盘点完成\n共 3 项")


# ---------------------------------------------------------------- 不拆


def test_a_short_message_is_one_part_without_a_header(hub):
    alpha = hub.project("alpha")
    text = "盘前简报\n" + "字" * 1495  # 1500 个字

    result = hub.sender.push(alpha, text)

    assert (result.parts, result.parts_sent) == (1, 1)
    assert hub.texts == [text]


def test_a_text_within_the_limit_is_not_split_to_make_room_for_a_header(hub):
    alpha = hub.project("alpha")
    text = "盘" * 10 + "\n" + "字" * 1979  # 1990 个字：不超过 2000，但超过 2000 减去开头的字数

    result = hub.sender.push(alpha, text)

    assert (result.parts, result.parts_sent) == (1, 1)
    assert hub.texts == [text]
    assert hub.used() == {("alpha", "push"): 1}
    assert len(hub.upstream.sent) == 1


def test_exactly_the_limit_is_one_part(hub):
    alpha = hub.project("alpha")
    text = "盘" * 10 + "\n" + "字" * 1989  # 2000 个字

    assert hub.sender.push(alpha, text).parts == 1


def test_the_label_counts_towards_the_limit(hub):
    x = hub.add("xray", label="x")  # 【x】占 3 个字

    fits = hub.sender.push(x, lined(1997))  # 加了标签正好 2000
    over = hub.sender.push(x, lined(1998))  # 加了标签 2001

    assert (fits.parts, over.parts) == (1, 2)
    assert all(len(text) <= PART_CHARS for text in hub.texts)


# ---------------------------------------------------------------- 拆


def test_one_character_over_the_limit_is_two_parts(hub):
    alpha = hub.project("alpha")
    text = lined(2001, title="盘" * 10)

    result = hub.sender.push(alpha, text)

    assert (result.parts, result.parts_sent) == (2, 2)
    assert len(hub.texts) == 2
    assert hub.texts[1].startswith("盘盘盘盘盘盘盘盘盘盘（第 2/2 条）\n")
    assert all(len(t) <= PART_CHARS for t in hub.texts)
    assert rebuilt(hub.texts, "盘" * 10) == text
    assert hub.used() == {("alpha", "push"): 2}


def test_a_long_message_is_split_on_line_breaks(hub):
    alpha = hub.project("alpha")
    text = lined(4503)

    result = hub.sender.push(alpha, text)

    assert (result.parts, result.parts_sent) == (3, 3)
    texts = hub.texts
    assert texts[1].startswith("盘点（第 2/3 条）\n")
    assert texts[2].startswith("盘点（第 3/3 条）\n")
    assert all(len(t) <= PART_CHARS for t in texts)
    assert rebuilt(texts, "盘点") == text
    assert all(t.endswith("\n") for t in texts[:2])  # 换行符留在前一段末尾，没有在行中间断开


def test_a_single_very_long_line_is_cut_on_character_boundaries(hub):
    alpha = hub.project("alpha")
    text = "".join(chr(0x4E00 + number % 500) for number in range(5000))
    title = text[:30] + "…"

    result = hub.sender.push(alpha, text)

    assert result.parts == len(hub.texts) >= 3
    assert all(len(t) <= PART_CHARS for t in hub.texts)
    assert rebuilt(hub.texts, title) == text


def test_the_label_is_on_the_headers_of_the_later_parts_too(hub):
    x = hub.add("xray", label="x")
    text = lined(2503)

    result = hub.sender.push(x, text)

    assert result.parts == 2
    assert hub.texts[0].startswith("【x】盘点\n")
    assert hub.texts[1].startswith("【x】盘点（第 2/2 条）\n")
    assert rebuilt(hub.texts, "【x】盘点") == "【x】" + text


def test_every_part_is_one_request_with_its_own_client_id(hub):
    alpha = hub.project("alpha")

    hub.sender.push(alpha, lined(4503))

    sent = hub.upstream.sent
    assert len(sent) == 3
    assert len({m.client_id for m in sent}) == 3
    assert {m.context_token for m in sent} == {"ctx-1"}


def test_the_headers_do_not_change_what_a_part_may_hold(hub):
    alpha = hub.project("alpha")

    hub.sender.push(alpha, lined(4503))

    assert re.fullmatch(r"盘点（第 [23]/3 条）\n.*", hub.texts[1], flags=re.S)
    assert len(hub.texts[0]) > 1900  # 第一条没有开头，也装得满


# ---------------------------------------------------------------- 配置


def test_the_part_size_is_configurable(make_hub):
    hub = make_hub(tweak(Config(), send={"part_chars": 200, "max_chars": 2000}))
    alpha = hub.ready()
    text = lined(603)

    result = hub.sender.push(alpha, text)

    assert result.parts == len(hub.texts) > 3
    assert all(len(t) <= 200 for t in hub.texts)
    assert rebuilt(hub.texts, "盘点") == text


def test_a_message_under_the_configured_size_is_one_part(make_hub):
    hub = make_hub(tweak(Config(), send={"part_chars": 200, "max_chars": 2000}))
    alpha = hub.ready()

    result = hub.sender.push(alpha, "字" * 200)

    assert result.parts == 1


@pytest.mark.parametrize("limit", [6, 10, 100])
def test_the_room_kept_for_the_headers_follows_the_window_limit(make_hub, limit):
    cfg = tweak(
        Config(), send={"max_chars": 6000}, quota={"window_limit": limit, "reply_reserve": 1}
    )
    hub = make_hub(cfg)
    hub.bind()
    hub.speak()
    alpha = hub.add("alpha", guaranteed=1, cap=3)
    text = "标题" + "字" * 3000  # 一整行，没有换行可断

    result = hub.sender.push(alpha, text)

    # 开头里写的是 `第 i/n 条`，n 按窗口总条数估：条数越大，开头越长，第一条能装的越少
    title = text[:30] + "…"
    assert result.parts == 2
    assert len(hub.texts[0]) == PART_CHARS - len(f"{title}（第 {limit}/{limit} 条）\n")
