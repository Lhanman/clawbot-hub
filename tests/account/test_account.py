"""凭证文件 `account.json`（规格 `hub-login`「凭证文件」）：怎么读、怎么写、什么叫"凭证变了"。"""

from __future__ import annotations

import fcntl
import json
import os
import stat
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from clawbot_hub import account as account_module
from clawbot_hub.account import (
    BROKEN_TEXT,
    AccountFile,
    AccountFileError,
    AccountStore,
    bound_at_text,
)
from clawbot_hub.upstream.base import Account
from tests.support.threads import run_in_thread

TOKEN = "tok-secret-111"
BUF = "buf-secret-222"


def make(**overrides) -> AccountFile:
    fields = {
        "bot_token": TOKEN,
        "user_id": "owner@im.wechat",
        "base_url": "https://ilink.example",
        "bot_id": "bot@im.bot",
        "bound_at": "2026-10-12T10:00:00+08:00",
        "updates_buf": BUF,
    }
    fields.update(overrides)
    return AccountFile(**fields)


@pytest.fixture
def store(paths) -> AccountStore:
    return AccountStore(paths)


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ---------------------------------------------------------------- 凭证摘要


@pytest.mark.parametrize("field", ["bot_token", "base_url", "bot_id", "user_id"])
def test_the_digest_changes_when_any_of_the_four_credential_fields_changes(field):
    assert make().digest != make(**{field: "something-else"}).digest


@pytest.mark.parametrize("field", ["bound_at", "updates_buf"])
def test_the_digest_ignores_the_binding_time_and_the_progress(field):
    # 服务每收一批消息都会改写 updates_buf，那不能算"凭证变了"
    assert make().digest == make(**{field: "something-else"}).digest


def test_the_digest_is_a_sha256_in_hex_and_does_not_contain_the_token():
    digest = make().digest
    assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")
    assert TOKEN not in digest


def test_the_digest_is_the_same_every_time_and_for_equal_credentials():
    assert make().digest == make().digest == make(bound_at="", updates_buf="").digest


def test_the_digest_keeps_neighbouring_fields_apart():
    first = make(bot_token="ab", base_url="c")
    second = make(bot_token="a", base_url="bc")
    assert first.digest != second.digest


def test_the_digest_tells_an_empty_field_from_a_missing_neighbour():
    assert make(base_url="", bot_id="x").digest != make(base_url="x", bot_id="").digest


def test_the_representation_never_shows_the_token_or_the_progress():
    text = repr(make()) + str(make())
    assert TOKEN not in text and BUF not in text


def test_the_account_for_the_upstream_has_the_four_credential_fields():
    assert make().to_account() == Account(
        bot_token=TOKEN,
        base_url="https://ilink.example",
        bot_id="bot@im.bot",
        user_id="owner@im.wechat",
    )


def test_the_binding_time_is_written_in_the_display_time_zone_to_the_second():
    beijing = ZoneInfo("Asia/Shanghai")
    assert bound_at_text(1_791_770_400_999, beijing) == "2026-10-12T10:00:00+08:00"
    assert bound_at_text(1_791_770_400_000, ZoneInfo("UTC")) == "2026-10-12T02:00:00+00:00"


# ---------------------------------------------------------------- 读


def test_a_missing_file_reads_as_nothing(store):
    assert store.read() is None


def test_what_is_written_reads_back_exactly(store):
    account = make(bound_at="2026-10-12T10:00:00+08:00")
    store.write(account)
    assert store.read() == account


def test_the_file_holds_all_six_fields_as_plain_text(store, paths):
    store.write(make())
    data = json.loads(paths.account.read_text(encoding="utf-8"))
    assert data == {
        "bot_token": TOKEN,
        "base_url": "https://ilink.example",
        "bot_id": "bot@im.bot",
        "user_id": "owner@im.wechat",
        "bound_at": "2026-10-12T10:00:00+08:00",
        "updates_buf": BUF,
    }


def test_the_file_ends_with_a_newline(store, paths):
    store.write(make())
    assert paths.account.read_text(encoding="utf-8").endswith("}\n")


def test_the_directory_is_synced_after_the_rename_so_the_new_name_survives_a_crash(
    store, paths, monkeypatch
):
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def spy_fsync(fd):
        events.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        return real_fsync(fd)

    def spy_replace(source, target):
        events.append("replace")
        return real_replace(source, target)

    monkeypatch.setattr(account_module.os, "fsync", spy_fsync)
    monkeypatch.setattr(account_module.os, "replace", spy_replace)
    store.write(make())
    assert events == ["file", "replace", "dir"]


def test_only_the_token_and_the_user_are_required(store, paths):
    paths.account.write_text(json.dumps({"bot_token": "t", "user_id": "u"}), encoding="utf-8")
    assert store.read() == AccountFile(bot_token="t", user_id="u")


def test_fields_this_version_does_not_know_are_ignored(store, paths):
    data = {"bot_token": "t", "user_id": "u", "from_the_future": [1, 2], "updates_buf": "b"}
    paths.account.write_text(json.dumps(data), encoding="utf-8")
    assert store.read() == AccountFile(bot_token="t", user_id="u", updates_buf="b")


BROKEN = {
    "not json": "SECRET-CONTENT {",
    "empty file": "",
    "a list": '["SECRET-CONTENT"]',
    "a string": '"SECRET-CONTENT"',
    "no token": '{"user_id": "SECRET-CONTENT"}',
    "no user": '{"bot_token": "SECRET-CONTENT"}',
    "empty token": '{"bot_token": "", "user_id": "SECRET-CONTENT"}',
    "empty user": '{"bot_token": "SECRET-CONTENT", "user_id": ""}',
    "token not text": '{"bot_token": 12345, "user_id": "SECRET-CONTENT"}',
    "user not text": '{"bot_token": "SECRET-CONTENT", "user_id": ["x"]}',
    "address not text": '{"bot_token": "t", "user_id": "SECRET-CONTENT", "base_url": 5}',
    "bot not text": '{"bot_token": "t", "user_id": "SECRET-CONTENT", "bot_id": null}',
    "time not text": '{"bot_token": "t", "user_id": "SECRET-CONTENT", "bound_at": 1}',
    "progress not text": '{"bot_token": "t", "user_id": "SECRET-CONTENT", "updates_buf": {}}',
}


@pytest.mark.parametrize("content", BROKEN.values(), ids=BROKEN.keys())
def test_a_file_that_cannot_be_read_is_reported_without_its_content(store, paths, content):
    paths.account.write_text(content, encoding="utf-8")
    with pytest.raises(AccountFileError) as error:
        store.read()
    assert str(error.value) == BROKEN_TEXT
    assert "SECRET-CONTENT" not in str(error.value)
    # 连带的原始异常（可能带着文件内容）不能跟着出现在回溯里
    assert error.value.__cause__ is None
    assert error.value.__context__ is None or error.value.__suppress_context__


def test_the_report_tells_how_to_log_in_again():
    assert BROKEN_TEXT == "account.json 读不出来：重新运行 hubctl login"


def test_a_file_with_bytes_that_are_not_text_is_broken_too(store, paths):
    paths.account.write_bytes(b"\xff\xfe\x00SECRET")
    with pytest.raises(AccountFileError):
        store.read()


def test_something_that_is_not_a_file_is_broken_too(store, paths):
    paths.account.mkdir()
    with pytest.raises(AccountFileError):
        store.read()


# ---------------------------------------------------------------- 写


def test_the_file_is_private_and_so_is_the_lock_file(store, paths):
    store.write(make())
    assert mode_of(paths.account) == 0o600
    assert mode_of(paths.account_lock) == 0o600


def test_a_loose_umask_cannot_make_it_wider_and_a_tight_one_does_not_break_it(store, paths):
    old = os.umask(0o000)
    try:
        store.write(make())
    finally:
        os.umask(old)
    assert mode_of(paths.account) == 0o600


def test_the_temporary_file_is_private_from_the_moment_it_exists(store, paths, monkeypatch):
    seen = []
    real_replace = os.replace

    def spy(source, target):
        seen.append((Path(source).name, mode_of(Path(source))))
        return real_replace(source, target)

    monkeypatch.setattr(account_module.os, "replace", spy)
    store.write(make())
    ((name, mode),) = seen
    assert name != "account.json" and name.startswith("account.json.")
    assert mode == 0o600


def test_the_temporary_file_is_created_with_the_final_mode_not_fixed_up_afterwards(
    store, monkeypatch
):
    modes = []
    real_open = os.open

    def spy(path, flags, mode=0o777, **kwargs):
        if "account.json." in str(path) and flags & os.O_CREAT:
            modes.append(mode)
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(account_module.os, "open", spy)
    store.write(make())
    assert modes and all(m == 0o600 for m in modes)


def test_a_temporary_name_that_is_already_taken_is_never_written_through(
    store, paths, tmp_path, monkeypatch
):
    """临时文件名撞上了已经存在的东西（哪怕是别人放的符号链接）：不写进去，更不顺着链接写到别处，
    也不去删不是自己建的东西。"""
    store.write(make())
    before = paths.account.read_bytes()
    monkeypatch.setattr(account_module.secrets, "token_hex", lambda _n: "00000000")
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("别处的文件", encoding="utf-8")
    planted = paths.home / f"account.json.{os.getpid()}-00000000.tmp"
    planted.symlink_to(elsewhere)

    with pytest.raises(FileExistsError):
        store.write(make(bot_token="new-token"))

    assert elsewhere.read_text(encoding="utf-8") == "别处的文件"
    assert planted.is_symlink()
    assert paths.account.read_bytes() == before


def test_nothing_but_the_file_and_its_lock_is_left_in_the_directory(store, paths):
    store.write(make())
    store.write(make(bot_token="second"))
    assert sorted(p.name for p in paths.home.iterdir()) == ["account.json", "account.json.lock"]


def test_a_write_that_fails_leaves_the_old_file_and_no_temporary_file(store, paths, monkeypatch):
    store.write(make())
    before = paths.account.read_bytes()

    def broken(*args):
        raise OSError("磁盘满了")

    monkeypatch.setattr(account_module.os, "replace", broken)
    with pytest.raises(OSError, match="磁盘满了"):
        store.write(make(bot_token="new-token"))
    assert paths.account.read_bytes() == before
    assert sorted(p.name for p in paths.home.iterdir()) == ["account.json", "account.json.lock"]


def test_a_write_that_dies_before_the_rename_leaves_the_old_file_untouched(
    store, paths, monkeypatch
):
    store.write(make())
    before = paths.account.read_bytes()

    def die(fd):
        raise KeyboardInterrupt

    monkeypatch.setattr(account_module.os, "fsync", die)
    with pytest.raises(KeyboardInterrupt):
        store.write(make(bot_token="new-token"))
    assert paths.account.read_bytes() == before
    assert sorted(p.name for p in paths.home.iterdir()) == ["account.json", "account.json.lock"]


def test_the_data_is_forced_to_disk_before_the_rename(store, monkeypatch):
    events = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(
        account_module.os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1]
    )
    monkeypatch.setattr(
        account_module.os, "replace", lambda a, b: (events.append("replace"), real_replace(a, b))[1]
    )
    store.write(make())
    assert events.index("fsync") < events.index("replace")


def test_writing_over_an_existing_file_replaces_it(store):
    store.write(make())
    store.write(make(bot_token="second", updates_buf=""))
    assert store.read() == make(bot_token="second", updates_buf="")


def test_writing_text_that_is_not_ascii_keeps_it_readable(store, paths):
    store.write(make(bot_id="机器人"))
    assert "机器人" in paths.account.read_text(encoding="utf-8")
    assert store.read().bot_id == "机器人"


# ---------------------------------------------------------------- 加锁


def hold(paths, mode: int):
    """另一个"程序"拿着凭证文件的锁（flock 跟着打开的文件走，同一进程里再打开一次也会冲突）。"""
    fd = os.open(paths.account_lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, mode)
    return fd


def release(fd):
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def test_reading_waits_while_someone_is_writing(store, paths):
    store.write(make())
    fd = hold(paths, fcntl.LOCK_EX)
    worker = run_in_thread(store.read)
    assert worker.still_blocked(0.1)
    release(fd)
    assert worker.result(timeout=2) == make()


def test_writing_waits_while_someone_is_reading_or_writing(store, paths):
    store.write(make())
    for mode in (fcntl.LOCK_SH, fcntl.LOCK_EX):
        fd = hold(paths, mode)
        worker = run_in_thread(store.write, make(bot_token=f"after-{mode}"))
        assert worker.still_blocked(0.1)
        release(fd)
        worker.result(timeout=2)
    assert store.read().bot_token == f"after-{fcntl.LOCK_EX}"


def test_several_readers_do_not_hold_each_other_up(store, paths):
    store.write(make())
    fd = hold(paths, fcntl.LOCK_SH)
    try:
        assert run_in_thread(store.read).result(timeout=2) == make()
    finally:
        release(fd)


def test_the_lock_is_released_after_a_failed_write(store, paths, monkeypatch):
    monkeypatch.setattr(
        account_module.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("x"))
    )
    with pytest.raises(OSError):
        store.write(make())
    monkeypatch.undo()
    assert run_in_thread(store.write, make()).result(timeout=2) is None


def test_the_lock_is_released_after_a_broken_read(store, paths):
    paths.account.write_text("not json")
    with pytest.raises(AccountFileError):
        store.read()
    assert run_in_thread(store.write, make()).result(timeout=2) is None


# ---------------------------------------------------------------- 写回收消息进度


def test_progress_is_written_back_when_the_token_is_still_the_one_in_use(store):
    store.write(make(updates_buf=""))
    assert store.save_progress(TOKEN, "buf-5") is True
    assert store.read() == make(updates_buf="buf-5")


def test_only_the_progress_changes(store, paths):
    store.write(make(updates_buf=""))
    store.save_progress(TOKEN, "buf-5")
    data = json.loads(paths.account.read_text(encoding="utf-8"))
    assert (
        data["bound_at"] == "2026-10-12T10:00:00+08:00"
        and data["base_url"] == "https://ilink.example"
    )


def test_progress_from_before_a_new_login_is_dropped(store):
    # 服务读出旧凭证发起一轮收消息，这时 hubctl login 写进了新凭证（进度为空）
    store.write(make(bot_token="new-token", updates_buf=""))
    assert store.save_progress(TOKEN, "buf-from-the-old-account") is False
    assert store.read() == make(bot_token="new-token", updates_buf="")


def test_progress_is_dropped_when_there_is_no_file_or_it_is_broken(store, paths):
    assert store.save_progress(TOKEN, "buf-1") is False
    paths.account.write_text("not json")
    assert store.save_progress(TOKEN, "buf-1") is False
    assert paths.account.read_text() == "not json"


def test_the_file_is_read_again_inside_the_lock(store, paths):
    store.write(make(updates_buf=""))
    fd = hold(paths, fcntl.LOCK_EX)  # 这时 hubctl login 正在写
    worker = run_in_thread(store.save_progress, TOKEN, "buf-from-the-old-account")
    assert worker.still_blocked(0.1)
    # 新凭证在锁放开之前就位
    new = make(bot_token="new-token", updates_buf="")
    paths.account.write_text(
        json.dumps(
            {**json.loads(paths.account.read_text()), "bot_token": new.bot_token, "updates_buf": ""}
        ),
        encoding="utf-8",
    )
    release(fd)
    assert worker.result(timeout=2) is False
    assert store.read() == new


def test_progress_that_has_not_changed_is_not_written_again(store, paths):
    store.write(make(updates_buf="buf-5"))
    before = paths.account.stat().st_ino
    assert store.save_progress(TOKEN, "buf-5") is True
    assert paths.account.stat().st_ino == before


def test_a_failed_progress_write_changes_nothing(store, paths, monkeypatch):
    store.write(make(updates_buf=""))
    before = paths.account.read_bytes()
    monkeypatch.setattr(
        account_module.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("x"))
    )
    with pytest.raises(OSError):
        store.save_progress(TOKEN, "buf-5")
    assert paths.account.read_bytes() == before
