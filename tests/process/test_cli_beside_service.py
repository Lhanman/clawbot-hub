"""命令行和常驻的服务同时用一个库（任务 E1，核对安装说明时发现的）。

服务在跑的时候，`hubctl status`、`doctor`、`project list`、`events`、`backup` 照样要开库、关库。
服务里每个线程各开一个连接，请求线程是后来才有的；如果建连接时对库文件做了一次普通的打开再关闭，
这个进程在库文件上的锁就全放掉了，下一个开了又关的命令行进程会当自己是最后一个使用者，把
`-wal` 和 `-shm` 删掉，服务再读写就是 `disk I/O error`、`database disk image is malformed`。
原理和最小的复现在 `tests/db/test_shared_with_other_processes.py`；这里用真的服务进程和真的
命令行进程把整条路走一遍。进程内的测试测不出来：同一个进程里的几个连接互相看得见锁。

后半是改东西的命令：`project set`、`disable`、`enable`、`rotate`、`remove`、改前缀、重新 `login`。
安装说明和运维手册都写"服务在跑也能改，下一个请求就照办，不用重启"（重新扫码是服务每隔几秒看一次
凭证文件，5 秒内换上），这里用真进程核对，并且每条命令之后确认服务的库日志文件没被动过。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import requests

from tests.process.conftest import auth
from tests.support.hub import BOT_TOKEN
from tests.support.threads import wait_for


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


def key_in(output: str) -> str:
    """命令输出里那一把项目密钥（只显示这一次的那一行）。"""
    return next(word for word in output.split() if word.startswith("chub_"))


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


def test_project_changes_made_by_the_command_line_apply_to_the_running_service_at_once(lab):
    key = lab.seed("alpha")["alpha"]
    daemon = lab.start()
    assert many_status_requests(daemon.url, key, 4) == [200] * 4
    wal, shm = lab.home / "hub.db-wal", lab.home / "hub.db-shm"
    before = (identity(wal), identity(shm))
    assert None not in before

    def status_of(project_key: str) -> requests.Response:
        return requests.get(f"{daemon.url}/v1/status", headers=auth(project_key), timeout=30)

    def run(*args: str) -> str:
        """跑一条命令，要成功，而且服务的库日志文件不能被它动过。返回它的输出。"""
        result = run_command(lab, *args)
        assert result.returncode == 0, (args, result.stdout, result.stderr)
        assert (identity(wal), identity(shm)) == before, f"hubctl {' '.join(args)} 动了日志文件"
        return result.stdout

    # 改上限：服务下一次回答的就是新的数
    run("project", "set", "alpha", "--cap", "7")
    quota = status_of(key).json()["quota"]["project"]
    assert (quota["guaranteed"], quota["cap"]) == (6, 7)

    # 停用、启用：下一个请求就见效
    run("project", "disable", "alpha")
    assert status_of(key).status_code == 403
    run("project", "enable", "alpha")
    assert status_of(key).status_code == 200

    # 换密钥：旧的立刻失效，新的马上能用
    new_key = key_in(run("project", "rotate", "alpha", "--yes"))
    assert new_key != key
    assert (status_of(key).status_code, status_of(new_key).status_code) == (401, 200)

    # 另一个项目：登记、停用、移除，密钥一路跟着变
    beta = key_in(
        run(
            "project", "add", "beta", "--prefix", "贝塔", "--guaranteed", "2", "--cap", "3", "--yes"
        )
    )
    assert status_of(beta).status_code == 200
    run("project", "disable", "beta")
    assert status_of(beta).status_code == 403
    run("project", "remove", "beta", "--yes")
    assert status_of(beta).status_code == 401

    assert many_status_requests(daemon.url, new_key, 4) == [200] * 4
    sent = requests.post(
        f"{daemon.url}/v1/messages",
        headers=auth(new_key),
        json={"text": "命令行改过设置以后发一条"},
        timeout=30,
    )
    assert (sent.status_code, sent.json()["status"]) == (200, "sent")
    assert "ERROR" not in lab.log_text()
    assert "ERROR" not in daemon.output
    assert daemon.stop() == 0


def test_a_prefix_changed_by_the_command_line_routes_the_next_message_at_once(lab):
    lab.seed("alpha")  # 前缀就是名字 alpha；它也是唯一的项目，不带前缀的话也会交给它
    daemon = lab.start()

    changed = run_command(lab, "project", "set", "alpha", "--prefix", "甲", "--yes")
    assert changed.returncode == 0, (changed.stdout, changed.stderr)
    lab.ilink.push_user_message("甲 看一下持仓")
    wait_for(lambda: lab.rows("SELECT id FROM inbox"), 20)

    # 是按新前缀交的（via 是 prefix、前缀已经从正文里去掉），不是当成没写前缀交给默认项目
    [row] = lab.rows(
        "SELECT p.name AS project, i.text AS text, i.via AS via "
        "FROM inbox i JOIN projects p ON p.id = i.project_id"
    )
    assert (row["project"], row["text"], row["via"]) == ("alpha", "看一下持仓", "prefix")
    assert "ERROR" not in lab.log_text()
    assert daemon.stop() == 0


def test_a_new_login_made_beside_the_running_service_is_used_without_restarting(lab):
    key = lab.seed("alpha")["alpha"]
    daemon = lab.start()
    assert many_status_requests(daemon.url, key, 4) == [200] * 4
    wal, shm = lab.home / "hub.db-wal", lab.home / "hub.db-shm"
    before = (identity(wal), identity(shm))
    assert None not in before

    def post(text: str) -> dict:
        reply = requests.post(
            f"{daemon.url}/v1/messages", headers=auth(key), json={"text": text}, timeout=30
        )
        assert reply.status_code == 200, reply.text
        return reply.json()

    def token_of_last_send() -> str | None:
        return lab.ilink.requests_to("sendmessage")[-1].header("Authorization")

    assert post("重新扫码以前发一条")["status"] == "sent"
    assert token_of_last_send() == f"Bearer {BOT_TOKEN}"

    last_event = lab.rows("SELECT MAX(id) AS id FROM events")[0]["id"]
    login = run_command(lab, "login")
    assert login.returncode == 0, (login.stdout, login.stderr)
    assert "绑定成功" in login.stdout
    assert (identity(wal), identity(shm)) == before
    new_token = json.loads(lab.paths.account.read_text(encoding="utf-8"))["bot_token"]
    assert new_token != BOT_TOKEN

    # 服务每隔几秒（真实时间）看一次凭证文件：看到换了，就记一笔、换上新的凭证。
    # 命令行扫码成功时自己也记了一笔"对话窗口作废"，要等的是服务记的这一笔（比扫码以前的事件新）
    noticed = (
        "SELECT id FROM events WHERE id > ? AND kind = 'login' AND detail LIKE '登录凭证已更换%'"
    )
    wait_for(lambda: lab.rows(noticed, last_event), 30)
    # 收消息的线程也换了：之后每一次向腾讯取话带的都是新凭证
    wait_for(
        lambda: (
            lab.ilink.requests_to("getupdates")[-1].header("Authorization") == f"Bearer {new_token}"
        ),
        10,
    )
    refused = post("重新扫码以后、使用者还没说话时发一条")
    assert (refused["status"], refused["reason_code"]) == ("rejected", "never_contacted")

    # 使用者在新的登录下说一句话，窗口重新开；这以后发出去的话带的是新凭证
    lab.ilink.push_user_message("我回来了")
    wait_for(lambda: lab.rows("SELECT id FROM inbox"), 20)
    assert post("使用者说话以后发一条")["status"] == "sent"
    assert token_of_last_send() == f"Bearer {new_token}"
    assert (identity(wal), identity(shm)) == before
    assert "ERROR" not in lab.log_text()
    assert new_token not in lab.log_text() + daemon.output
    assert daemon.stop() == 0
