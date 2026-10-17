"""登录凭证文件 `<home>/account.json`：读、写、"凭证变了"的摘要。

文件里是 `bot_token`、`base_url`、`bot_id`、`user_id`（使用者在腾讯那边的编号，也是"本人"的依据）、
`bound_at`、`updates_buf`（收消息的进度）。权限 0600；写的时候先写临时文件再改名，临时文件创建时
就是 0600；读写都在 `account.json.lock` 上加锁（`flock`，进程死了系统自动放锁）。
使用者最近一条消息的对话凭证和窗口里的计数不在这个文件里，它们在数据库里。

**什么叫"凭证变了"**：只看 `bot_token`、`base_url`、`bot_id`、`user_id` 这四项，它们的摘要是
`AccountFile.digest`；`updates_buf`、`bound_at` 的变化不算——服务每收一批消息都会改写 `updates_buf`。

规格：`hub-login`「凭证文件」。
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, tzinfo
from pathlib import Path

from clawbot_hub.paths import FILE_MODE, HubPaths
from clawbot_hub.upstream.base import Account

BROKEN_TEXT = "account.json 读不出来：重新运行 hubctl login"

_OPTIONAL_TEXT_FIELDS = ("base_url", "bot_id", "bound_at", "updates_buf")


class AccountFileError(Exception):
    """文件读不出来（不是 JSON、缺字段、类型不对）。信息里没有文件的内容。"""


@dataclass(frozen=True)
class AccountFile:
    bot_token: str = field(repr=False)
    user_id: str
    base_url: str = ""
    bot_id: str = ""
    bound_at: str = ""
    updates_buf: str = field(default="", repr=False)

    @property
    def digest(self) -> str:
        """四项凭证的摘要（SHA-256 的十六进制）。四项放进一个 JSON 数组再算，所以相邻两项之间
        的边界不会含糊（"ab" + "c" 和 "a" + "bc" 不一样）。"""
        four = [self.bot_token, self.base_url, self.bot_id, self.user_id]
        return hashlib.sha256(json.dumps(four, ensure_ascii=False).encode("utf-8")).hexdigest()

    def to_account(self) -> Account:
        return Account(
            bot_token=self.bot_token,
            base_url=self.base_url,
            bot_id=self.bot_id,
            user_id=self.user_id,
        )


def bound_at_text(now_ms: int, tz: tzinfo) -> str:
    """绑定时间的写法：显示时区的 ISO 8601，精确到秒（与 ashare-quant 的凭证文件一致）。"""
    return datetime.fromtimestamp(now_ms / 1000, tz).isoformat(timespec="seconds")


class AccountStore:
    def __init__(self, paths: HubPaths) -> None:
        self._paths = paths

    def read(self) -> AccountFile | None:
        """没有文件返回 `None`；读不出来抛 `AccountFileError`。"""
        with self._locked(fcntl.LOCK_SH):
            return self._load()

    def write(self, account: AccountFile) -> None:
        with self._locked(fcntl.LOCK_EX):
            self._dump(account)

    def save_progress(self, bot_token: str, updates_buf: str) -> bool:
        """把新的收消息进度写回。在锁里重新读一遍文件，只有文件里的 `bot_token` 仍然是这一轮
        收消息所用的那个才改；否则（`hubctl login` 刚写了新凭证，进度已经清空）丢弃这次写回，
        返回 `False`。没有文件或读不出来也是 `False`。"""
        with self._locked(fcntl.LOCK_EX):
            try:
                current = self._load()
            except AccountFileError:
                return False
            if current is None or current.bot_token != bot_token:
                return False
            if current.updates_buf != updates_buf:
                self._dump(replace(current, updates_buf=updates_buf))
            return True

    # ------------------------------------------------ 内部

    @contextmanager
    def _locked(self, kind: int) -> Iterator[None]:
        fd = os.open(self._paths.account_lock, os.O_RDWR | os.O_CREAT, FILE_MODE)
        try:
            fcntl.flock(fd, kind)
            yield
        finally:
            os.close(fd)  # 关掉文件就放锁

    def _load(self) -> AccountFile | None:
        try:
            raw = self._paths.account.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            raise AccountFileError(BROKEN_TEXT) from None
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:  # 含 UnicodeDecodeError、JSONDecodeError
            raise AccountFileError(BROKEN_TEXT) from None
        if not isinstance(data, dict):
            raise AccountFileError(BROKEN_TEXT)
        token, user = data.get("bot_token"), data.get("user_id")
        if not (isinstance(token, str) and token and isinstance(user, str) and user):
            raise AccountFileError(BROKEN_TEXT)
        optional: dict[str, str] = {}
        for name in _OPTIONAL_TEXT_FIELDS:
            value = data.get(name, "")
            if not isinstance(value, str):
                raise AccountFileError(BROKEN_TEXT)
            optional[name] = value
        return AccountFile(bot_token=token, user_id=user, **optional)

    def _dump(self, account: AccountFile) -> None:
        data = {
            "bot_token": account.bot_token,
            "base_url": account.base_url,
            "bot_id": account.bot_id,
            "user_id": account.user_id,
            "bound_at": account.bound_at,
            "updates_buf": account.updates_buf,
        }
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        target = self._paths.account
        temporary = target.with_name(f"{target.name}.{os.getpid()}-{secrets.token_hex(4)}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
            raise
        _fsync_directory(target.parent)


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
