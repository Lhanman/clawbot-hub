"""项目密钥（规格 `hub-projects`「项目密钥」，任务 A6）：格式、库里只有摘要、认人、换钥和宽限。

接口层的 401 / 403 外壳、认证失败的限流在 D 阶段的 `tests/api`；这里测"认人"本身。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re

import pytest

from clawbot_hub import projects as projects_module
from clawbot_hub.projects import (
    NeedsConfirmation,
    ProjectDisabled,
    ProjectError,
    ProjectNotFound,
    Unauthorized,
)
from tests.projects.conftest import events, register

KEY_FORMAT = re.compile(r"chub_[A-Za-z0-9_-]{43}")
MINUTE_MS = 60_000


def digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def stored(database, name: str = "alpha"):
    return database.conn().execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()


# ---------------------------------------------------------------- 格式


def test_a_key_is_chub_plus_43_urlsafe_characters(projects):
    keys = [register(projects, name).key for name in ("alpha", "bravo", "charlie")]

    for key in keys:
        assert KEY_FORMAT.fullmatch(key) and len(key) == 48
    assert len(set(keys)) == 3


def test_the_43_characters_are_32_random_bytes(projects):
    key = register(projects, "alpha").key

    assert len(base64.urlsafe_b64decode(key.removeprefix("chub_") + "=")) == 32


# ---------------------------------------------------------------- 库里只有摘要


def test_the_database_holds_the_sha256_digest_and_never_the_key(projects, database, paths):
    key = register(projects, "alpha").key
    rotated = projects.rotate("alpha", grace_minutes=10, yes=True).key

    assert stored(database)["key_hash"] == digest(rotated)
    assert stored(database)["prev_key_hash"] == digest(key)
    database.conn().execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    everything = b"".join(path.read_bytes() for path in paths.home.glob("hub.db*"))
    dump = "\n".join(database.conn().iterdump()).encode()
    for secret in (key, rotated):
        assert secret.encode() not in everything
        assert secret.encode() not in dump


def test_neither_the_events_nor_the_log_ever_see_a_key(projects, database, caplog):
    with caplog.at_level(logging.DEBUG):
        first = register(projects, "alpha").key
        second = projects.rotate("alpha", grace_minutes=5, yes=True).key
        projects.authenticate(first)
        projects.authenticate(second)
        with pytest.raises(Unauthorized):
            projects.authenticate("chub_" + "A" * 43)
        projects.set("alpha", cap=3)
        projects.disable("alpha")

    assert "chub_" not in caplog.text
    assert "chub_" not in repr(events(database))


# ---------------------------------------------------------------- 认人


def test_a_good_key_gives_the_project_and_marks_it_as_seen(projects, clock):
    registration = register(projects, "alpha")
    clock.advance(30)

    project = projects.authenticate(registration.key)

    assert project.name == "alpha" and project.last_seen_ms == clock.now_ms()
    assert projects.get("alpha").last_seen_ms == clock.now_ms()
    clock.advance(30)
    assert projects.authenticate(registration.key).last_seen_ms == clock.now_ms()


def test_every_project_is_recognised_by_its_own_key(projects):
    keys = {name: register(projects, name).key for name in ("alpha", "bravo", "charlie")}

    for name, key in keys.items():
        assert projects.authenticate(key).name == name


def test_the_check_compares_digests_in_constant_time(projects, monkeypatch):
    key = register(projects, "alpha").key
    calls = []
    real = projects_module.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(projects_module.hmac, "compare_digest", spy)

    projects.authenticate(key)

    assert calls == [(digest(key), digest(key))]


def test_the_old_key_in_its_grace_period_is_compared_in_constant_time_too(projects, monkeypatch):
    old = register(projects, "alpha").key
    new = projects.rotate("alpha", grace_minutes=5, yes=True).key
    calls = []
    real = projects_module.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(projects_module.hmac, "compare_digest", spy)

    projects.authenticate(old)

    assert calls == [(digest(new), digest(old)), (digest(old), digest(old))]


BAD_KEYS = [
    None,
    "",
    "chub_",
    "x",
    "Bearer",
    "chub_" + "A" * 42,  # 短一个
    "chub_" + "A" * 44,  # 长一个
    "chub_" + "A" * 42 + "=",  # 不是 URL 安全的字符
    "chub_" + "A" * 42 + " ",
    "chux_" + "A" * 43,
    "CHUB_" + "A" * 43,
    "chub_" + "A" * 43,  # 格式对，没登记过
    "internal:_hub",
    "internal:_import",
    b"chub_" + b"A" * 43,
    12345,
]


@pytest.mark.parametrize("bad", BAD_KEYS, ids=repr)
def test_keys_that_are_wrong_in_any_way_are_all_the_same_refusal(projects, bad):
    register(projects, "alpha")

    with pytest.raises(Unauthorized) as error:
        projects.authenticate(bad)

    assert str(error.value) == "unauthorized"


def test_a_good_key_with_anything_added_is_refused(projects):
    key = register(projects, "alpha").key

    for changed in (" " + key, key + " ", key + "\n", key[:-1] + ("A" if key[-1] != "A" else "B")):
        with pytest.raises(Unauthorized):
            projects.authenticate(changed)


def test_a_refused_key_marks_nobody_as_seen(projects):
    register(projects, "alpha")

    with pytest.raises(Unauthorized):
        projects.authenticate("chub_" + "A" * 43)

    assert projects.get("alpha").last_seen_ms is None


def test_the_key_of_a_removed_project_is_refused_like_an_unknown_one(projects):
    key = register(projects, "alpha").key
    projects.disable("alpha")
    projects.remove("alpha", yes=True)

    with pytest.raises(Unauthorized) as removed:
        projects.authenticate(key)
    with pytest.raises(Unauthorized) as unknown:
        projects.authenticate("chub_" + "A" * 43)

    assert str(removed.value) == str(unknown.value)


def test_the_key_of_a_disabled_project_is_known_but_not_allowed(projects):
    key = register(projects, "alpha").key
    projects.disable("alpha")

    with pytest.raises(ProjectDisabled) as error:
        projects.authenticate(key)

    assert error.value.project.name == "alpha"
    assert str(error.value) == "project_disabled"
    assert projects.get("alpha").last_seen_ms is None
    projects.enable("alpha")
    assert projects.authenticate(key).name == "alpha"


def test_the_internal_projects_cannot_be_logged_in_as(projects, database):
    rows = database.conn().execute("SELECT key_hash FROM projects WHERE internal = 1").fetchall()
    assert len(rows) == 2
    for row in rows:
        assert not re.fullmatch(r"[0-9a-f]{64}", row["key_hash"])  # 永远不会等于任何摘要
        with pytest.raises(Unauthorized):
            projects.authenticate(row["key_hash"])


def test_a_row_marked_removed_never_logs_in_even_if_it_kept_a_working_key(projects, database):
    key = register(projects, "alpha").key
    with database.txn() as conn:
        conn.execute("UPDATE projects SET removed_ms = 1 WHERE name = 'alpha'")

    with pytest.raises(Unauthorized):
        projects.authenticate(key)


def test_an_internal_row_never_logs_in_even_with_a_real_digest_in_it(projects, database):
    key = "chub_" + "B" * 43
    with database.txn() as conn:
        conn.execute("UPDATE projects SET key_hash = ? WHERE name = '_hub'", (digest(key),))

    with pytest.raises(Unauthorized):
        projects.authenticate(key)


def test_a_project_setting_changed_by_the_cli_is_seen_by_the_next_request(projects, database):
    key = register(projects, "alpha").key
    projects.authenticate(key)
    with database.txn() as conn:  # 另一个进程（hubctl）直接改了库
        conn.execute("UPDATE projects SET enabled = 0 WHERE name = 'alpha'")

    with pytest.raises(ProjectDisabled):
        projects.authenticate(key)


# ---------------------------------------------------------------- 换钥


def test_rotating_gives_a_new_key_and_the_old_one_stops_at_once(projects, database):
    old = register(projects, "alpha").key

    rotation = projects.rotate("alpha", yes=True)

    assert KEY_FORMAT.fullmatch(rotation.key) and rotation.key != old
    assert rotation.project.name == "alpha" and rotation.grace_until_ms is None
    assert projects.authenticate(rotation.key).name == "alpha"
    with pytest.raises(Unauthorized):
        projects.authenticate(old)
    row = stored(database)
    assert row["key_hash"] == digest(rotation.key)
    assert row["prev_key_hash"] is None and row["prev_key_until_ms"] is None


def test_rotating_takes_confirmation(projects):
    old = register(projects, "alpha").key

    with pytest.raises(NeedsConfirmation, match="alpha.*旧密钥立刻失效"):
        projects.rotate("alpha")
    with pytest.raises(NeedsConfirmation, match="alpha.*旧密钥再用 10 分钟"):
        projects.rotate("alpha", grace_minutes=10)

    assert projects.authenticate(old).name == "alpha"


def test_the_old_key_lives_through_the_grace_period_and_not_a_moment_longer(projects, clock):
    old = register(projects, "alpha").key

    rotation = projects.rotate("alpha", grace_minutes=10, yes=True)

    assert rotation.grace_until_ms == clock.now_ms() + 10 * MINUTE_MS
    clock.advance(10 * 60 - 1)
    assert projects.authenticate(old).name == "alpha"
    assert projects.authenticate(rotation.key).name == "alpha"
    clock.advance(1)  # 刚好 10 分钟
    with pytest.raises(Unauthorized):
        projects.authenticate(old)
    assert projects.authenticate(rotation.key).name == "alpha"


def test_the_grace_period_may_be_anything_from_zero_to_sixty_minutes(projects):
    register(projects, "alpha")

    for minutes in (0, 1, 60):
        assert projects.rotate("alpha", grace_minutes=minutes, yes=True).project.name == "alpha"


@pytest.mark.parametrize("minutes", [61, -1, 100, 1.5, True, "10"])
def test_a_grace_period_outside_zero_to_sixty_is_refused_and_nothing_changes(projects, minutes):
    old = register(projects, "alpha").key

    with pytest.raises(ProjectError, match="宽限期要在 0 到 60 分钟之间"):
        projects.rotate("alpha", grace_minutes=minutes, yes=True)

    assert projects.authenticate(old).name == "alpha"


def test_rotating_again_inside_the_grace_period_keeps_only_the_previous_key(projects, clock):
    first = register(projects, "alpha").key
    second = projects.rotate("alpha", grace_minutes=10, yes=True).key
    clock.advance(2 * 60)

    third = projects.rotate("alpha", yes=True).key

    assert projects.authenticate(third).name == "alpha"
    for gone in (first, second):
        with pytest.raises(Unauthorized):
            projects.authenticate(gone)


def test_rotating_again_with_a_grace_period_drops_the_older_key_and_keeps_the_last_one(
    projects, clock
):
    first = register(projects, "alpha").key
    second = projects.rotate("alpha", grace_minutes=10, yes=True).key
    clock.advance(2 * 60)

    third = projects.rotate("alpha", grace_minutes=5, yes=True).key

    with pytest.raises(Unauthorized):
        projects.authenticate(first)
    assert projects.authenticate(second).name == "alpha"
    assert projects.authenticate(third).name == "alpha"
    clock.advance(5 * 60)
    with pytest.raises(Unauthorized):
        projects.authenticate(second)


def test_a_key_in_its_grace_period_works_for_the_right_project_only(projects):
    old_alpha = register(projects, "alpha").key
    register(projects, "bravo")
    projects.rotate("alpha", grace_minutes=10, yes=True)

    assert projects.authenticate(old_alpha).name == "alpha"


def test_rotating_a_disabled_project_is_allowed_but_it_stays_disabled(projects):
    old = register(projects, "alpha").key
    projects.disable("alpha")

    rotation = projects.rotate("alpha", grace_minutes=10, yes=True)

    for key in (rotation.key, old):
        with pytest.raises(ProjectDisabled):
            projects.authenticate(key)


def test_a_removed_or_unknown_project_cannot_be_rotated(projects):
    register(projects, "alpha")
    projects.disable("alpha")
    projects.remove("alpha", yes=True)

    with pytest.raises(ProjectError, match="已移除"):
        projects.rotate("alpha", yes=True)
    for name in ("nobody", "_hub"):
        with pytest.raises(ProjectNotFound):
            projects.rotate(name, yes=True)


def test_rotating_leaves_one_event_without_the_key(projects, database):
    register(projects, "alpha")
    before = len(events(database))

    key = projects.rotate("alpha", grace_minutes=5, yes=True).key

    (event,) = events(database)[before:]
    assert (event["kind"], event["level"], event["project"]) == ("project", "INFO", "alpha")
    assert key not in event["detail"] and "5" in event["detail"]
