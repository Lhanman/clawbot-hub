"""假腾讯独立运行（任务 D6，规格 `hub-deploy`「测试不联网」→「假腾讯能独立运行」）。

走的是 `docs/ops/安装说明.md`「演练：用假腾讯走一遍」那条路，只是在测试里自动做：另起一个进程
跑 `python -m tests.support.fake_ilink`，`hubctl login` 扫码（假腾讯几秒后自动确认）、登记项目、
`hubctl serve`，再用假腾讯的 `/_fake/` 控制接口放一条使用者消息、看服务发出的话、切到回 -14。
几个进程都装了联网守卫（`HUB_TEST_GUARD`）：碰到本机回环以外的地址就抛错，所以"全程没有访问外网"
是测出来的，不是说出来的。
"""

from __future__ import annotations

import json
import re
import signal
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass

import pytest
import requests

from tests.process.conftest import REPO_ROOT, auth, child_env, free_port
from tests.support.threads import run_in_thread, wait_for

KEY_PATTERN = re.compile(r"chub_[A-Za-z0-9_-]{43}")


@dataclass
class Standalone:
    proc: subprocess.Popen
    url: str

    def post(self, path: str, **body):
        return requests.post(f"{self.url}{path}", json=body, timeout=10)

    def sent(self) -> list[str]:
        return [
            m["text"] for m in requests.get(f"{self.url}/_fake/sent", timeout=10).json()["sent"]
        ]


@pytest.fixture
def standalone(lab):
    port = free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tests.support.fake_ilink",
            "--port",
            str(port),
            "--confirm-after",
            "1",
            "--longpoll",
            "1",
        ],  # fmt: skip
        cwd=REPO_ROOT,
        env=child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    url = f"http://127.0.0.1:{port}"
    try:
        line = proc.stdout.readline()
        assert line.strip() == f"fake_ilink listening on {url}", line
        lab.configure(upstream={"base_url": url})  # 服务和命令行都连它
        yield Standalone(proc, url)
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            with suppress(subprocess.TimeoutExpired):
                proc.wait(5)
        if proc.poll() is None:
            proc.kill()
        proc.wait(10)


def test_the_hub_runs_against_the_standalone_fake_from_login_to_login_expired(lab, standalone):
    where = ["--home", str(lab.home), "--config", str(lab.config_file)]

    # 扫码：假腾讯在取码 1 秒后自动确认
    login = lab.hubctl("login", *where)
    assert login.returncode == 0, login.stdout + login.stderr
    assert "绑定成功" in login.stdout
    account = json.loads(lab.paths.account.read_text(encoding="utf-8"))
    assert account["base_url"] == standalone.url

    added = lab.hubctl(
        "project", "add", "alpha", "--guaranteed", "6", "--cap", "8", "--prefix", "量化", "--yes",
        *where,
    )  # fmt: skip
    assert added.returncode == 0, added.stdout + added.stderr
    key = KEY_PATTERN.search(added.stdout).group(0)

    daemon = lab.start(guard=True)

    # 收：项目先来取话（在线），再放一条使用者消息进假腾讯，服务收到并交给默认项目
    fetching = run_in_thread(
        requests.get,
        f"{daemon.url}/v1/inbox",
        headers=auth(key),
        params={"wait": 20},
        timeout=60,
    )
    wait_for(
        lambda: lab.rows("SELECT last_seen_ms FROM projects WHERE name = 'alpha'")[0][
            "last_seen_ms"
        ],
        20,
    )
    assert standalone.post("/_fake/inbox", text="你好").json()["ok"] is True
    [item] = fetching.result(timeout=20).json()["items"]
    assert item["text"] == "你好"

    # 回：用取到的回复句柄回一句，假腾讯收到
    reply = requests.post(
        f"{daemon.url}/v1/replies",
        headers=auth(key),
        json={"handle": item["reply_handle"], "text": "收到，盘点中"},
        timeout=30,
    )
    assert reply.status_code == 200 and reply.json()["status"] == "sent"
    assert any("收到，盘点中" in text for text in standalone.sent())

    # 推：项目主动发一条，假腾讯收到；命令行也能发
    pushed = requests.post(
        f"{daemon.url}/v1/messages", headers=auth(key), json={"text": "盘点完成"}, timeout=30
    )
    assert pushed.json()["status"] == "sent"
    assert any("盘点完成" in text for text in standalone.sent())
    cli = lab.hubctl("send", "--url", daemon.url, "命令行发的一条", CLAWBOT_HUB_KEY=key)
    assert cli.returncode == 0, cli.stdout + cli.stderr
    assert cli.stdout.split()[0] == "sent"
    assert any("命令行发的一条" in text for text in standalone.sent())

    # 登录失效：假腾讯切到回 -14，下一次发消息得到 failed / login_expired
    assert (
        standalone.post("/_fake/mode", mode="session_expired").json()["mode"] == "session_expired"
    )
    expired = requests.post(
        f"{daemon.url}/v1/messages", headers=auth(key), json={"text": "失效后的一条"}, timeout=30
    ).json()
    assert (expired["status"], expired["reason_code"]) == ("failed", "login_expired")
    assert standalone.post("/_fake/mode", mode="normal").json()["mode"] == "normal"

    assert daemon.stop() == 0, daemon.output
    assert "不允许联网" not in daemon.output + login.stdout + login.stderr + lab.log_text()
