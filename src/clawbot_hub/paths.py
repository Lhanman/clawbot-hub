"""数据目录的位置、里面的文件名、权限修复。

服务的所有数据都放在数据目录 `<home>` 里（默认 `~/.clawbot-hub`，环境变量 `CLAWBOT_HUB_HOME`
或命令行的 `--home` 可改），不放进任何项目的目录。目录 0700，里面的文件 0600。
规格见 `hub-deploy`「数据目录和权限」。
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

HOME_ENV = "CLAWBOT_HUB_HOME"
DEFAULT_HOME_NAME = ".clawbot-hub"

DIR_MODE = 0o700
FILE_MODE = 0o600


def resolve_home(
    home: str | os.PathLike[str] | None = None, env: Mapping[str, str] | None = None
) -> Path:
    """数据目录：`--home` 参数，再是环境变量 `CLAWBOT_HUB_HOME`，再是 `~/.clawbot-hub`。

    空串当没写；`~` 展开；相对路径按当前目录变成绝对路径（不解析符号链接）。"""
    if env is None:
        env = os.environ
    chosen = str(home) if home else ""
    chosen = chosen or env.get(HOME_ENV, "")
    path = Path(chosen).expanduser() if chosen else Path.home() / DEFAULT_HOME_NAME
    return path if path.is_absolute() else Path.cwd() / path


@dataclass(frozen=True)
class HubPaths:
    """数据目录里每个文件的位置。"""

    home: Path

    @property
    def db(self) -> Path:
        return self.home / "hub.db"

    @property
    def account(self) -> Path:
        return self.home / "account.json"

    @property
    def account_lock(self) -> Path:
        return self.home / "account.json.lock"

    @property
    def config(self) -> Path:
        return self.home / "config.yaml"

    @property
    def bark(self) -> Path:
        return self.home / "bark.json"

    @property
    def lock(self) -> Path:
        return self.home / "hub.lock"

    @property
    def run(self) -> Path:
        return self.home / "run.json"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def log_file(self) -> Path:
        return self.logs / "hub.log"

    @property
    def launchd_log(self) -> Path:
        return self.logs / "launchd.log"

    @property
    def backups(self) -> Path:
        return self.home / "backups"

    @property
    def login_png(self) -> Path:
        return self.home / "clawbot-login.png"

    @property
    def login_pending(self) -> Path:
        return self.home / "login.pending"


def ensure_home(paths: HubPaths) -> None:
    """数据目录不存在就建（0700）；已经有的不动它的权限（放宽了的由 `fix_permissions` 改回）。"""
    if paths.home.exists() and not paths.home.is_dir():
        raise NotADirectoryError(f"数据目录的位置上是个文件：{paths.home}")
    paths.home.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)


def _too_wide(path: Path) -> bool:
    return bool(stat.S_IMODE(path.lstat().st_mode) & 0o077)


@dataclass(frozen=True)
class WidePermission:
    """一个权限比规定的宽的文件或目录：现在是多少、应该是多少。"""

    path: Path
    mode: int
    wanted: int


def _check(path: Path, wanted: int, found: list[WidePermission]) -> None:
    if path.is_symlink() or not path.exists():
        return
    if _too_wide(path):
        found.append(WidePermission(path, stat.S_IMODE(path.lstat().st_mode), wanted))


def wide_permissions(paths: HubPaths) -> list[WidePermission]:
    """已有的文件和目录里权限比规定的宽（组或其他人有权限）的：目录规定 0700、文件 0600。

    不存在的、符号链接、本来就更严的都不算。覆盖数据目录本身、下面登记过名字的文件、`logs/` 和
    `backups/` 里的全部内容。只看不改（`hubctl doctor` 用）；改回去用 `fix_permissions`。"""
    found: list[WidePermission] = []
    _check(paths.home, DIR_MODE, found)
    db = paths.db
    for path in (
        paths.account,
        paths.account_lock,
        db,
        db.with_name(db.name + "-wal"),
        db.with_name(db.name + "-shm"),
        paths.config,
        paths.bark,
        paths.lock,
        paths.run,
        paths.login_png,
        paths.login_pending,
    ):
        _check(path, FILE_MODE, found)
    for tree in (paths.logs, paths.backups):
        if tree.is_symlink() or not tree.is_dir():
            continue
        _check(tree, DIR_MODE, found)
        for root, dirs, files in os.walk(tree):
            for name in dirs:
                _check(Path(root, name), DIR_MODE, found)
            for name in files:
                _check(Path(root, name), FILE_MODE, found)
    return found


def fix_permissions(paths: HubPaths) -> list[Path]:
    """`wide_permissions` 找到的都改回：目录 0700、文件 0600。

    返回改过的路径，调用者（启动、`hubctl doctor --fix`）逐条写警告。"""
    fixed: list[Path] = []
    for item in wide_permissions(paths):
        item.path.chmod(item.wanted)
        fixed.append(item.path)
    return fixed
