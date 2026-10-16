"""命令行和常驻的服务同时用一个库（任务 E1，核对安装说明时发现的）。

服务在跑的时候，`hubctl status`、`doctor`、`project list`、`events`、`backup` 照样要开库、关库。
服务里每个线程各开一个连接，请求线程是后来才有的；如果建连接时对库文件做了一次普通的打开再关闭，
这个进程在库文件上的锁就全放掉了，下一个开了又关的命令行进程会当自己是最后一个使用者，把
`-wal` 和 `-shm` 删掉，服务再读写就是 `disk I/O error`、`database disk image is malformed`。
原理和最小的复现在 `tests/db/test_shared_with_other_processes.py`；这里用真的服务进程和真的
命令行进程把整条路走一遍。进程内的测试测不出来：同一个进程里的几个连接互相看得见锁。
"""

from __future__ import annotations

import threading
from pathlib import Path

import requests

from tests.process.conftest import auth


def many_status_requests(url: str, key: str, count: int = 6) -> list[int]:
    """同时发几个 `GET /v1/status`：服务的请求线程第一次要库，每个线程开一个自己的连接。"""
    codes: list[int] = []

    def one() -> None:
        codes.append(requests.get(f"{url}/v1/status", headers=auth(key), timeout=30).status_code)

    threads = [threading.Thread(target=one) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    return codes


def run_command(lab, *args: str):
    return lab.hubctl(*args, "--home", str(lab.home), "--config", str(lab.config_file))


def identity(path: Path) -> tuple[int, int] | None:
    """文件是哪一个（设备号加 inode）；文件没了是 `None`。被删掉又被别人重新建出来的文件，
    名字一样、是另一个文件：只看"存在"看不出来。"""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_dev, stat.st_ino


def test_commands_run_beside_the_service_do_not_break_its_database(lab):
    key = lab.seed("alpha")["alpha"]
    daemon = lab.start()
    assert many_status_requests(daemon.url, key) == [200] * 6
    wal, shm = lab.home / "hub.db-wal", lab.home / "hub.db-shm"
    before = (identity(wal), identity(shm))
    assert None not in before

    for args in (
        ("status",),
        ("doctor",),
        ("project", "list"),
        ("events",),
        ("backup",),
        ("version",),
    ):
        result = run_command(lab, *args)
        assert result.returncode == 0, (args, result.stdout, result.stderr)
        # 服务还开着库：日志文件不能被命令行收走（删掉再被服务重新建出来也不行）
        assert (identity(wal), identity(shm)) == before, f"hubctl {' '.join(args)} 动了日志文件"

    assert many_status_requests(daemon.url, key) == [200] * 6
    sent = requests.post(
        f"{daemon.url}/v1/messages",
        headers=auth(key),
        json={"text": "命令行用过库以后发一条"},
        timeout=30,
    )
    assert (sent.status_code, sent.json()["status"]) == (200, "sent")
    assert "ERROR" not in lab.log_text()
    assert "ERROR" not in daemon.output
    assert daemon.stop() == 0


def test_a_project_added_by_the_command_line_works_at_once_without_restarting(lab):
    first = lab.seed("alpha")["alpha"]
    daemon = lab.start()
    assert many_status_requests(daemon.url, first, 2) == [200, 200]

    added = run_command(
        lab,
        "project",
        "add",
        "beta",
        "--prefix",
        "贝塔",
        "--guaranteed",
        "2",
        "--cap",
        "3",
        "--yes",
    )
    assert added.returncode == 0, (added.stdout, added.stderr)
    key = next(word for word in added.stdout.split() if word.startswith("chub_"))

    assert requests.get(f"{daemon.url}/v1/status", headers=auth(key), timeout=30).status_code == 200
    assert many_status_requests(daemon.url, first, 2) == [200, 200]
    assert "ERROR" not in lab.log_text()
    assert daemon.stop() == 0
