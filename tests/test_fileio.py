"""数据目录里小文件的写法（`fileio.write_private`）：整份换掉、从出现起就是 0600、旁边不留临时
文件、`durable` 为真才落盘。`bark.json`（设备码）和 `run.json`（运行记录）靠它。
"""

from __future__ import annotations

import os
import stat

import pytest

from clawbot_hub import fileio
from clawbot_hub.fileio import write_private
from clawbot_hub.paths import FILE_MODE


@pytest.fixture
def open_umask():
    """把进程的 umask 放到最松（0），证明 0600 不是靠 umask 凑出来的。"""
    previous = os.umask(0)
    yield
    os.umask(previous)


def mode_of(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_the_file_holds_exactly_the_text_and_is_private(tmp_path, open_umask):
    target = tmp_path / "bark.json"

    write_private(target, '{"key": "设备码"}\n')

    assert target.read_text(encoding="utf-8") == '{"key": "设备码"}\n'
    assert mode_of(target) == FILE_MODE == 0o600


def test_an_existing_file_is_replaced_whole_and_stays_private(tmp_path, open_umask):
    target = tmp_path / "run.json"
    target.write_text("很长很长很长很长很长的旧内容" * 10, encoding="utf-8")
    target.chmod(0o644)

    write_private(target, "新的")

    assert target.read_text(encoding="utf-8") == "新的"
    assert mode_of(target) == 0o600


def test_no_scrap_is_left_beside_the_file(tmp_path):
    target = tmp_path / "run.json"

    for text in ("一", "二", "三"):
        write_private(target, text)
        write_private(target, text, durable=False)

    assert [entry.name for entry in tmp_path.iterdir()] == ["run.json"]


def test_a_failed_write_keeps_the_old_file_and_leaves_no_scrap(tmp_path):
    target = tmp_path / "bark.json"
    write_private(target, "旧的")

    with pytest.raises(UnicodeEncodeError):
        write_private(target, "\ud800")  # 单独的代理字符编不成 UTF-8

    assert target.read_text(encoding="utf-8") == "旧的"
    assert [entry.name for entry in tmp_path.iterdir()] == ["bark.json"]


def test_a_durable_write_syncs_the_file_and_then_the_directory(tmp_path, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(fileio.os, "fsync", lambda fd: (synced.append(fd), real(fd))[1])

    write_private(tmp_path / "bark.json", "内容")

    assert len(synced) == 2  # 文件一次，目录一次


def test_a_light_write_syncs_nothing(tmp_path, monkeypatch):
    synced = []
    monkeypatch.setattr(fileio.os, "fsync", lambda fd: synced.append(fd))

    write_private(tmp_path / "run.json", "心跳", durable=False)

    assert synced == []
    assert (tmp_path / "run.json").read_text(encoding="utf-8") == "心跳"


def test_the_file_is_there_whole_or_not_at_all_for_a_reader(tmp_path, monkeypatch):
    """改名之前读到的还是旧的整份，改名之后读到的是新的整份。"""
    target = tmp_path / "bark.json"
    write_private(target, "旧的")
    seen = []
    real = os.replace

    def spy(source, destination):
        seen.append(target.read_text(encoding="utf-8"))  # 改名前一刻
        assert os.path.dirname(source) == os.path.dirname(destination)  # 同一个目录才能原子改名
        real(source, destination)

    monkeypatch.setattr(fileio.os, "replace", spy)

    write_private(target, "新的")

    assert seen == ["旧的"]
    assert target.read_text(encoding="utf-8") == "新的"


def test_two_writers_at_the_same_moment_do_not_clash(tmp_path, monkeypatch):
    """第一个写到一半（临时文件已建、还没改名）时，第二个也来写：各用各的临时文件。"""
    target = tmp_path / "bark.json"
    real = os.replace
    nested = []

    def interrupted(source, destination):
        if not nested:
            nested.append(True)
            write_private(target, "第二个写的")  # 临时文件重名的话，这里会撞上
        real(source, destination)

    monkeypatch.setattr(fileio.os, "replace", interrupted)

    write_private(target, "第一个写的")

    assert target.read_text(encoding="utf-8") == "第一个写的"  # 后改名的盖掉先改名的
    assert [entry.name for entry in tmp_path.iterdir()] == ["bark.json"]


def test_no_file_handle_is_left_open(tmp_path):
    target = tmp_path / "bark.json"
    write_private(target, "先热一下")
    before = len(os.listdir("/dev/fd"))

    for _ in range(30):
        write_private(target, "内容")
        write_private(target, "内容", durable=False)

    assert len(os.listdir("/dev/fd")) == before
