"""数据目录里小文件的写法：先写一份临时文件、再改名盖掉原来的，读的一方永远读到完整的一份。

临时文件创建时就是 0600（不管进程的 umask 是什么），所以文件从出现的那一刻起就只有本人能读。
`bark.json`（设备码）和 `run.json`（运行记录）用这个；`account.json` 有自己的写法（见
`account.py`）。
"""

from __future__ import annotations

import contextlib
import os
import secrets
from pathlib import Path

from clawbot_hub.paths import FILE_MODE


def write_private(path: Path, text: str, *, durable: bool = True) -> None:
    """把 `text` 写成 `path`（0600）：整份换掉，旁边不留临时文件。

    `durable` 为真时写完先落盘再改名、改名后再落盘目录，断电也不会丢；心跳这种每 30 秒一次、
    丢了无害的写法传 `False`，省掉两次落盘。"""
    temporary = path.with_name(f"{path.name}.{os.getpid()}-{secrets.token_hex(4)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            if durable:
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
    if durable:
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
