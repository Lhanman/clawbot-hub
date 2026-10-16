"""事件记录：写进 events 表的小函数。种类是封闭的集合（hub-cli「事件记录」）。"""

from __future__ import annotations

import dataclasses

import pytest

from clawbot_hub.events import KINDS, LEVELS, add_event, recent_events


def rows(db):
    return [dict(r) for r in db.conn().execute("SELECT * FROM events ORDER BY id")]


def test_kinds_are_the_closed_set_of_the_spec():
    assert KINDS == (
        "login",
        "alert",
        "send",
        "hold",
        "project",
        "queue",
        "backup",
        "lifecycle",
    )


def test_levels_follow_the_logging_names():
    assert LEVELS == ("INFO", "WARNING", "ERROR")


def test_add_event_writes_one_row(database):
    with database.txn() as conn:
        add_event(conn, 1_234, "login", "登录已恢复：重新扫码", level="WARNING", project="x")

    assert rows(database) == [
        {
            "id": 1,
            "ts_ms": 1_234,
            "level": "WARNING",
            "kind": "login",
            "project": "x",
            "detail": "登录已恢复：重新扫码",
        }
    ]


def test_defaults_are_info_and_no_project(database):
    with database.txn() as conn:
        add_event(conn, 5, "lifecycle", "启动")

    (row,) = rows(database)
    assert row["level"] == "INFO"
    assert row["project"] is None


def test_events_keep_their_order(database):
    with database.txn() as conn:
        add_event(conn, 3, "send", "第一条")
        add_event(conn, 2, "send", "第二条")

    assert [r["detail"] for r in rows(database)] == ["第一条", "第二条"]


@pytest.mark.parametrize("kind", ["foo", "proj", "Login", "", "login "])
def test_unknown_kind_is_refused_and_nothing_written(database, kind):
    with pytest.raises(ValueError, match="种类.*可用：login、alert、send"), database.txn() as conn:
        add_event(conn, 1, kind, "x")

    assert rows(database) == []


@pytest.mark.parametrize("level", ["info", "DEBUG", "", "CRITICAL"])
def test_unknown_level_is_refused(database, level):
    with pytest.raises(ValueError, match="级别.*可用：INFO、WARNING、ERROR"):
        with database.txn() as conn:
            add_event(conn, 1, "login", "x", level=level)

    assert rows(database) == []


@pytest.mark.parametrize("detail", ["", "   ", "\n"])
def test_empty_detail_is_refused(database, detail):
    with pytest.raises(ValueError, match="内容"), database.txn() as conn:
        add_event(conn, 1, "login", detail)

    assert rows(database) == []


def test_the_event_belongs_to_the_callers_transaction(database):
    class Boom(Exception):
        pass

    with pytest.raises(Boom), database.txn() as conn:
        add_event(conn, 1, "login", "会被回滚")
        raise Boom

    assert rows(database) == []


def test_a_row_read_back_cannot_be_changed(database):
    with database.txn() as conn:
        add_event(conn, 1, "login", "登录已恢复")

    [row] = recent_events(database.conn())

    with pytest.raises(dataclasses.FrozenInstanceError):
        row.detail = "改了"  # type: ignore[misc]
