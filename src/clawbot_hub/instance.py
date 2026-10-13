"""单实例锁 `hub.lock`，以及"服务在不在运行"的判断。

**锁**：`hubctl serve` 启动时对 `hub.lock` 非阻塞地拿一把排他锁（`fcntl.flock`），拿不到说明已经有
一个在运行，退出码 2。拿到后把自己的进程号写进去，别人拿不到时可以说"进程号 N"。进程退出（含被
`kill -9`）时系统自动放锁，不会留下僵尸锁；锁文件里残留的旧进程号不碍事（只有拿不到锁时才读它）。

**在不在运行**：一律看 `run.json`（服务每 30 秒写一次心跳），不去试拿 `hub.lock`——试拿的一瞬间会让
正在启动的服务误以为已经有一个在运行。里面的进程号对应的进程还在、最后心跳不超过 60 秒前、
而且没有标记"正常退出"，才算在运行。

规格：`hub-login`「单实例」、`hub-cli`、`hub-deploy`「数据目录和权限」（`run.json` 的字段）。
"""

from __future__ import annotations

import fcntl
import json
import os
from dataclasses import dataclass
from types import TracebackType
from typing import Any

from clawbot_hub.clock import Clock
from clawbot_hub.paths import FILE_MODE, HubPaths

# `run.json` 的心跳超过这么久没更新，就当服务没在运行（心跳每 30 秒一次）
HEARTBEAT_STALE_SECONDS = 60


class InstanceBusy(Exception):
    """已经有一个在运行（或有程序正占着锁）。`pid` 是锁文件里记的进程号，读不出来是 `None`。"""

    def __init__(self, pid: int | None) -> None:
        self.pid = pid
        super().__init__("已经有一个在运行" if pid is None else f"已经有一个在运行（进程号 {pid}）")


class InstanceLock:
    """拿到的锁。`release()` 放锁（重复放没关系）；也可以当 `with` 用。"""

    def __init__(self, fd: int) -> None:
        self._fd: int | None = fd

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)  # 关掉文件就放锁
            self._fd = None

    def __enter__(self) -> InstanceLock:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def acquire_instance_lock(paths: HubPaths) -> InstanceLock:
    """非阻塞地拿 `hub.lock`；拿不到抛 `InstanceBusy`。"""
    fd = os.open(paths.lock, os.O_RDWR | os.O_CREAT, FILE_MODE)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstanceBusy(_holder_pid(fd)) from None
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
    except BaseException:
        os.close(fd)
        raise
    return InstanceLock(fd)


def _holder_pid(fd: int) -> int | None:
    try:
        text = os.pread(fd, 32, 0).decode("ascii", "replace").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def pid_alive(pid: int) -> bool:
    """这个编号的进程还在不在（发 0 号信号，不会打扰它）。别的用户的进程也算在。
    0 和负数在 `kill` 里表示进程组，一律当作不在。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass(frozen=True)
class RunInfo:
    """`run.json` 的内容。只有 `pid` 和 `last_heartbeat_ms` 是必须的。"""

    pid: int
    last_heartbeat_ms: int
    started_ms: int = 0
    version: str = ""
    listen: str = ""
    profile: str = ""
    confirm: str = ""
    clean_exit: bool = False


def _whole_number(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def read_run_info(paths: HubPaths) -> RunInfo | None:
    """读 `run.json`；没有、读不出来、缺必须的字段都当作没有。"""
    try:
        data = json.loads(paths.run.read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    pid = _whole_number(data.get("pid"))
    beat = _whole_number(data.get("last_heartbeat_ms"))
    if pid is None or beat is None:
        return None
    started = _whole_number(data.get("started_ms")) or 0
    text = {
        name: data[name] if isinstance(data.get(name), str) else ""
        for name in ("version", "listen", "profile", "confirm")
    }
    return RunInfo(
        pid=pid,
        last_heartbeat_ms=beat,
        started_ms=started,
        clean_exit=data.get("clean_exit") is True,
        **text,
    )


def service_running(paths: HubPaths, clock: Clock) -> RunInfo | None:
    """服务在运行就返回它的 `run.json`，没在运行返回 `None`。"""
    info = read_run_info(paths)
    if info is None or info.clean_exit or not pid_alive(info.pid):
        return None
    if clock.now_ms() - info.last_heartbeat_ms > HEARTBEAT_STALE_SECONDS * 1000:
        return None
    return info
