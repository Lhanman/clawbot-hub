"""备份的核心：做一份能打开的一致性副本、三种命名、清理旧的自动备份。

`hubctl backup` 命令（`cli.py`，含 `--to` 的提示和云盘路径警告）、按时间点触发的自动备份
（`scheduler.py`）和 `hubctl doctor` 的"最近一次自动备份"都调这里的函数。规格见 `hub-cli`「备份」。

备份里有登录凭证：目录 0700、文件 0600，创建时就是这个权限；已经存在的父目录不动它的权限。
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from urllib.parse import quote

from clawbot_hub.paths import DIR_MODE, FILE_MODE, HubPaths

# 和 hub.db 一起备份的文件（存在才复制）
COPIED_FILES = ("account.json", "config.yaml", "bark.json")

_STAMP = re.compile(r"[0-9]{8}-[0-9]{6}")
_AUTO = re.compile(r"auto-[0-9]{8}-[0-9]{6}")


class BackupError(Exception):
    """备份没做成。信息里不含凭证。"""


@dataclass(frozen=True)
class BackupResult:
    directory: Path
    files: dict[str, int]  # 文件名 → 字节数


def timestamp(now_ms: int, tz: tzinfo) -> str:
    """备份目录名里的时间：`YYYYmmdd-HHMMSS`，按显示时区。"""
    return datetime.fromtimestamp(now_ms // 1000, tz).strftime("%Y%m%d-%H%M%S")


def backup_name(kind: str, stamp: str, *, old: int | None = None, new: int | None = None) -> str:
    """三种备份的目录名：自动的 `auto-<时间>`，手动的 `<时间>`，
    升级前的 `pre-upgrade-v<旧>-v<新>-<时间>`。"""
    if not _STAMP.fullmatch(stamp):
        raise ValueError(f"时间应该是 YYYYmmdd-HHMMSS 的格式：{stamp!r}")
    if kind == "auto":
        return f"auto-{stamp}"
    if kind == "manual":
        return stamp
    if kind == "pre-upgrade":
        if old is None or new is None:
            raise ValueError("升级前的备份要给出旧版本号和新版本号")
        return f"pre-upgrade-v{old}-v{new}-{stamp}"
    raise ValueError(f"不认识的备份种类：{kind!r}")


def _copy_private(source: Path, destination: Path) -> None:
    """复制文件内容，目标文件创建时就是 0600（原文件权限怎样都不影响）。"""
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, FILE_MODE)
    with os.fdopen(fd, "wb") as out, open(source, "rb") as src:
        shutil.copyfileobj(src, out)


def _give_up_when_stuck(
    busy_timeout_ms: int, now: Callable[[], float] = time.monotonic
) -> Callable[[int, int, int], None]:
    """给在线备份的进度回调：源库一直被别人占着（`BUSY` / `LOCKED`），Python 的备份函数会每 0.25 秒
    重试一次、永远不放弃；这里超过 `busy_timeout_ms` 就报 `BackupError`。库没被占着时（`OK` 和
    `DONE`）不管备份花了多久，都不打断。"""
    started = now()

    def progress(status: int, remaining: int, total: int) -> None:
        if status not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return
        waited = now() - started
        if waited * 1000 >= busy_timeout_ms:
            raise BackupError(
                f"数据库一直被占着，等了 {waited:.1f} 秒没能开始备份（db.busy_timeout_ms 是 "
                f"{busy_timeout_ms}）；是不是有别的程序在独占它？"
            )

    return progress


def _copy_database(
    database: Path, destination: Path, source: sqlite3.Connection | None, busy_timeout_ms: int
) -> None:
    """用 SQLite 的在线备份接口复制：服务在写的时候也安全，拿到的是某一时刻完整的快照。"""
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, FILE_MODE)
    os.close(fd)  # 先建好 0600 的空文件，SQLite 往里写
    own = None
    target = sqlite3.connect(destination)
    try:
        if source is None:
            own = sqlite3.connect(database)
            own.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
            source = own
        source.backup(target, progress=_give_up_when_stuck(busy_timeout_ms))
        target.execute("PRAGMA journal_mode = DELETE")  # 副本是单个自包含的文件，不带 -wal
    finally:
        target.close()
        if own is not None:
            own.close()


def _verify(copy: Path) -> None:
    conn = sqlite3.connect(f"file:{quote(str(copy))}?mode=ro", uri=True)
    try:
        rows = [row[0] for row in conn.execute("PRAGMA integrity_check")]
    finally:
        conn.close()
    if rows != ["ok"]:
        raise BackupError("备份出来的数据库没通过完整性检查（integrity_check）")


def make_backup(
    paths: HubPaths,
    parent: str | os.PathLike[str],
    name: str,
    *,
    source: sqlite3.Connection | None = None,
    busy_timeout_ms: int = 5000,
) -> BackupResult:
    """把 `hub.db`（在线备份）和存在的 `account.json`、`config.yaml`、`bark.json` 复制到
    `<parent>/<name>/`。父目录不存在就建（0700），已经有的不改权限；`<name>` 已经有了就拒绝，
    不覆盖。失败时不留下半截目录。`source` 可以传服务手里已经开着的连接。"""
    if not paths.db.exists():
        raise BackupError("没有数据库可备份")
    parent = Path(parent)
    destination = parent / name
    try:
        parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"建不了存放备份的目录 {parent}：{exc}") from exc
    try:
        destination.mkdir(mode=DIR_MODE)
    except FileExistsError:
        raise BackupError(f"备份目录已经有了，不覆盖：{destination}") from None
    except OSError as exc:
        raise BackupError(f"建不了备份目录 {destination}：{exc}") from exc
    try:
        files: dict[str, int] = {}
        _copy_database(paths.db, destination / "hub.db", source, busy_timeout_ms)
        _verify(destination / "hub.db")
        files["hub.db"] = (destination / "hub.db").stat().st_size
        for filename in COPIED_FILES:
            original = paths.home / filename
            if original.is_file():
                _copy_private(original, destination / filename)
                files[filename] = (destination / filename).stat().st_size
    except (OSError, sqlite3.Error, BackupError) as exc:
        shutil.rmtree(destination, ignore_errors=True)
        if isinstance(exc, BackupError):
            raise
        raise BackupError(f"备份没做成：{exc}") from exc
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return BackupResult(destination, files)


def auto_backup_names(parent: str | os.PathLike[str]) -> list[str]:
    """`parent` 里自动备份的目录名（`auto-<时间>`，得是目录、不能是符号链接），按名字排，
    也就是按时间，老的在前。手动的、升级前的、名字像但不是的都不算；目录不存在是空表。"""
    parent = Path(parent)
    if not parent.is_dir():
        return []
    return sorted(
        entry.name
        for entry in parent.iterdir()
        if _AUTO.fullmatch(entry.name) and entry.is_dir() and not entry.is_symlink()
    )


def latest_auto_backup(parent: str | os.PathLike[str], tz: tzinfo) -> tuple[str, datetime] | None:
    """最近一份自动备份：(目录名, 做备份的时刻)。时刻是目录名里的时间，按显示时区；
    名字里的日期不存在的（`auto-20261340-…`）跳过。一份也没有是 `None`。"""
    for name in reversed(auto_backup_names(parent)):
        try:
            when = datetime.strptime(name.removeprefix("auto-"), "%Y%m%d-%H%M%S")
        except ValueError:
            continue
        return name, when.replace(tzinfo=tz)
    return None


def prune_auto_backups(parent: str | os.PathLike[str], keep: int) -> list[str]:
    """只留最近 `keep` 份自动备份（名字是 `auto-<时间>` 的目录，按名字排）；别的不动
    （手动的、升级前的、名字像但不是的）。返回被删掉的目录名。"""
    if keep < 1:
        raise ValueError("至少要保留 1 份")
    parent = Path(parent)
    names = auto_backup_names(parent)
    doomed = names[:-keep]
    for name in doomed:
        shutil.rmtree(parent / name)
    return doomed
