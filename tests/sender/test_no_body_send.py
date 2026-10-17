"""发出去的消息正文不落盘：数据库、日志、事件里都搜不到。规格：`hub-send`「消息正文不落盘」。
（收到的话、过期和作废的话、服务自己处理的话的正文归阶段 C。）
"""

from __future__ import annotations

import logging

import pytest

from clawbot_hub.config import Config
from clawbot_hub.sender import Origin
from clawbot_hub.upstream.base import NoResponse, Rejected, SessionExpired, Transient
from tests.sender.conftest import long_text
from tests.support.fake_upstream import DEFAULT
from tests.support.hub import tweak

SECRET = "ZZ-UNIQUE-77"
HUB = Origin("_hub", "服务")


def everything_on_disk(hub) -> bytes:
    """数据库文件、预写日志、共享内存文件的全部内容（先做一次检查点，把日志并回主文件）。"""
    hub.db.conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return b"".join(path.read_bytes() for path in sorted(hub.paths.home.glob("hub.db*")))


def text_with_the_secret(parts: int = 1) -> str:
    """含独特字符串的文字：1 条是一小段，几条是按行拆成恰好那么多条的长文。"""
    if parts == 1:
        return f"标题 {SECRET}\n正文 {SECRET}"
    return long_text(parts, title=f"标题 {SECRET}")


def assert_nowhere(hub, caplog) -> None:
    assert SECRET.encode() not in everything_on_disk(hub)
    assert all(SECRET not in record.getMessage() for record in caplog.records)
    for event in hub.events():
        assert SECRET not in event["detail"]
    for row in hub.rows():
        assert all(SECRET not in str(value) for value in tuple(row))


def test_a_sent_message_leaves_no_trace_of_its_text(hub, caplog):
    a = hub.ready()
    caplog.set_level(logging.DEBUG)

    result = hub.sender.push(a, text_with_the_secret())

    assert result.status == "sent"
    assert SECRET in hub.texts[0]  # 假上游确实收到了
    assert_nowhere(hub, caplog)


def test_a_long_message_leaves_no_trace_either(hub, caplog):
    a = hub.ready()
    caplog.set_level(logging.DEBUG)

    hub.sender.push(a, text_with_the_secret(3), dedupe_key=f"键 {SECRET}")

    assert SECRET in hub.texts[0]
    assert_nowhere(hub, caplog)


@pytest.mark.parametrize(
    "outcome",
    [
        Rejected(200, 40001, "bad token"),
        Rejected(403, None, ""),
        NoResponse("读超时", read_timeout=True),
        Transient("连不上"),
        SessionExpired(),
    ],
    ids=["rejected", "http-403", "no-response", "unreachable", "login-expired"],
)
def test_a_failed_message_leaves_no_trace_of_its_text(hub, caplog, outcome):
    a = hub.ready()
    hub.upstream.set_default("send_text", outcome)
    caplog.set_level(logging.DEBUG)

    result = hub.sender.push(a, text_with_the_secret(2))

    assert result.status in ("failed", "unconfirmed", "partial")
    assert_nowhere(hub, caplog)


def test_a_refused_message_leaves_no_trace_of_its_text(hub, caplog):
    a = hub.ready()
    hub.clock.advance(25 * 3600)
    caplog.set_level(logging.DEBUG)

    result = hub.sender.push(a, text_with_the_secret())

    assert result.reason_code == "window_closed"
    assert_nowhere(hub, caplog)


def test_an_interrupted_message_leaves_no_trace_of_its_text(hub, caplog):
    a = hub.ready()
    hub.upstream.script("send_text", DEFAULT, RuntimeError("出意外了"))
    caplog.set_level(logging.DEBUG)

    with pytest.raises(RuntimeError):
        hub.sender.push(a, text_with_the_secret(3))

    assert_nowhere(hub, caplog)


def test_a_reply_leaves_no_trace_of_its_text(hub, caplog):
    hub.ready()
    caplog.set_level(logging.DEBUG)

    hub.sender.reply(HUB, text_with_the_secret(), context_token="ctx-1")

    assert_nowhere(hub, caplog)


def test_the_record_keeps_only_the_number_of_characters(make_hub):
    hub = make_hub(tweak(Config(), send={"max_chars": 20000}))
    a = hub.ready()
    text = text_with_the_secret()

    result = hub.sender.push(a, text)

    row = hub.row(result.id)
    assert row["chars"] == len(text)
    assert set(row.keys()) == {
        "id",
        "project_id",
        "kind",
        "priority",
        "window_id",
        "dedupe_hash",
        "parts",
        "parts_sent",
        "parts_unconfirmed",
        "status",
        "reason_code",
        "reason",
        "chars",
        "no_message_id",
        "created_ms",
        "finished_ms",
    }  # 没有存正文的列
