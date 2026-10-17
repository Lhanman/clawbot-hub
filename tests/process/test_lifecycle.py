"""真的服务进程（任务 D6，规格 `hub-deploy`「启动自检和顺序」「正常关闭」「数据目录和权限」）。

别的测试都在测试进程里起服务（假时钟、进程内的假上游）；这里起的是真的 `hubctl serve` 进程：
真信号（`SIGTERM`、`SIGINT`）、真的 `kill -9`、真的退出码、真的端口和锁、真的 umask，腾讯和 Bark 是
测试进程里回环上的假服务。每个测试要起一两个进程（导入加启动大约一秒），所以只放进程这一层才
看得到的东西；逻辑细节在 `test_startup.py`、`test_shutdown.py` 里用假时钟测。

腾讯的响应慢用 `ok().after(秒)`（真实的秒），所以延迟都取得很短。
"""

from __future__ import annotations

import hashlib
import json
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import time

import pytest
import requests

from clawbot_hub.instance import acquire_instance_lock, read_run_info
from clawbot_hub.paths import wide_permissions
from tests.process.conftest import DEVICE, REPO_ROOT, auth, child_env
from tests.support.fake_bark import PUSH, bark_error
from tests.support.fake_ilink import GETUPDATES, SENDMESSAGE, ok
from tests.support.hub import BOT_TOKEN
from tests.support.threads import run_in_thread, wait_for

SENDING = [{"status": "sending"}]


def sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def post(daemon, key: str, text: str, **fields):
    return requests.post(
        f"{daemon.url}/v1/messages",
        headers=auth(key),
        json={"text": text, **fields},
        timeout=60,
    )


def sending(lab) -> bool:
    return lab.rows("SELECT status FROM outbox") == SENDING


def upstream_texts(lab) -> list[str]:
    """腾讯收到的 sendmessage 请求里的文字。`script` 过的端点假腾讯不再记"已发出的消息"，
    所以这里直接看收到的请求。"""
    return [
        item["text_item"]["text"]
        for request in lab.ilink.requests
        if request.endpoint == SENDMESSAGE
        for item in request.json["msg"]["item_list"]
        if item.get("type") == 1
    ]


# ---------------------------------------------------------------- 正常停

STOP_SIGNALS = [signal.SIGTERM, signal.SIGINT]


@pytest.mark.parametrize("sig", STOP_SIGNALS, ids=["SIGTERM", "SIGINT"])
def test_a_stop_signal_ends_the_process_with_exit_0_and_a_clean_mark(lab, sig):
    lab.seed("alpha")
    daemon = lab.start()
    assert read_run_info(lab.paths).clean_exit is False  # 运行中：还没有"正常退出"

    code = daemon.stop(sig)

    assert code == 0, daemon.output
    run = read_run_info(lab.paths)
    assert run is not None and run.clean_exit is True
    assert run.pid == daemon.pid
    acquire_instance_lock(lab.paths).release()  # 锁放了
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", lab.port), timeout=2)  # 端口也还了
    assert "已启动" in daemon.output  # 标准错误里有（launchd 会收走）
    assert "正常退出" in daemon.output
    assert "已启动" in lab.log_text() and "正常退出" in lab.log_text()
    assert "Traceback" not in daemon.output


def test_python_dash_m_runs_the_same_command_line():
    done = subprocess.run(
        [sys.executable, "-m", "clawbot_hub", "--help"],
        cwd=REPO_ROOT,
        env=child_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )

    assert done.returncode == 0, done.stderr
    assert "serve" in done.stdout


def test_a_waiting_inbox_request_gets_an_empty_list_within_a_second_of_the_signal(lab):
    keys = lab.seed("alpha")
    daemon = lab.start()
    waiting = run_in_thread(
        requests.get,
        f"{daemon.url}/v1/inbox",
        headers=auth(keys["alpha"]),
        params={"wait": 25},
        timeout=60,
    )
    wait_for(
        lambda: lab.rows("SELECT last_seen_ms FROM projects WHERE name = 'alpha'")[0][
            "last_seen_ms"
        ],
        20,
    )
    assert waiting.still_blocked(0.3)  # 服务收到了，正挂着等话

    signalled = time.monotonic()
    daemon.send_signal(signal.SIGTERM)
    response = waiting.result(timeout=10)

    assert time.monotonic() - signalled < 1.0
    assert response.status_code == 200 and response.json()["items"] == []
    assert daemon.wait() == 0


def test_an_in_flight_send_finishes_before_the_process_exits(lab):
    keys = lab.seed("alpha")
    lab.ilink.script(SENDMESSAGE, ok().after(2.0))  # 腾讯 2 秒后才回话
    daemon = lab.start()
    sent = run_in_thread(post, daemon, keys["alpha"], "盘点完成")
    wait_for(lambda: sending(lab), 20)

    daemon.send_signal(signal.SIGTERM)
    response = sent.result(timeout=20)

    assert response.status_code == 200 and response.json()["status"] == "sent"
    assert daemon.wait() == 0, daemon.output
    assert read_run_info(lab.paths).clean_exit is True
    [text] = upstream_texts(lab)
    assert "盘点完成" in text  # 腾讯收到了，只收到一次
    assert lab.rows("SELECT status FROM outbox") == [{"status": "sent"}]


# ---------------------------------------------------------------- 被杀和强退


def test_after_kill_9_the_next_start_interrupts_the_unfinished_send_and_alerts_once(lab):
    keys = lab.seed("alpha")
    lab.ilink.script(SENDMESSAGE, ok().after(6.0))
    first = lab.start()
    run_in_thread(lambda: _ignore_errors(post, first, keys["alpha"], "卡住的一条"))
    wait_for(lambda: sending(lab), 20)
    used = lab.rows("SELECT project_id, kind, n FROM usage")

    assert first.kill() == -signal.SIGKILL
    assert read_run_info(lab.paths).clean_exit is False  # 来不及写"正常退出"

    second = lab.start()  # 锁是系统放的，不会留下僵尸锁

    assert lab.rows("SELECT status, reason_code FROM outbox") == [
        {"status": "interrupted", "reason_code": "interrupted"}
    ]
    assert lab.rows("SELECT project_id, kind, n FROM usage") == used  # 预占的条数保留
    wait_for(lambda: lab.bark.titles() == ["ClawBot 共用服务 · 服务重启"], 20)
    assert "1 条发送中断" in lab.bark.pushes[0].body
    assert second.stop() == 0


def _ignore_errors(fn, *args):
    try:
        return fn(*args)
    except requests.RequestException:
        return None  # 服务被杀，连接断了


def test_when_the_shutdown_time_runs_out_the_process_exits_with_1_and_the_next_start_alerts(lab):
    keys = lab.seed("alpha")
    lab.configure(server={"shutdown_seconds": 1})
    lab.ilink.script(SENDMESSAGE, ok().after(8.0))  # 比关停等待久得多
    first = lab.start()
    run_in_thread(lambda: _ignore_errors(post, first, keys["alpha"], "卡住的一条"))
    wait_for(lambda: sending(lab), 20)
    started = time.monotonic()

    first.send_signal(signal.SIGTERM)
    code = first.wait(20)

    assert code == 1, first.output
    assert time.monotonic() - started < 6  # 只等了约 1 秒，没有等满 8 秒
    assert "强行退出" in first.output
    assert read_run_info(lab.paths).clean_exit is False
    assert lab.rows("SELECT status FROM outbox") == SENDING  # 这条下次启动标成中断

    second = lab.start()

    assert lab.rows("SELECT status FROM outbox") == [{"status": "interrupted"}]
    wait_for(lambda: lab.bark.titles() == ["ClawBot 共用服务 · 服务重启"], 20)
    assert second.stop() == 0


# ---------------------------------------------------------------- 起不来：退出码


def test_a_taken_port_exits_with_1_after_the_database_was_made(lab):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", lab.port))
    blocker.listen()
    try:
        daemon = lab.spawn()
        code = daemon.wait()
    finally:
        blocker.close()

    assert code == 1, daemon.output
    assert f"端口 {lab.port} 已被占用" in daemon.output
    assert lab.paths.db.exists()  # 前面几步照常做过
    assert not lab.paths.run.exists()  # 没开始运行
    assert lab.ilink.requests == []  # 没有向腾讯发任何请求
    acquire_instance_lock(lab.paths).release()


def test_a_second_instance_exits_with_2_and_leaves_the_first_alone(lab):
    lab.seed("alpha")
    first = lab.start()
    before = read_run_info(lab.paths)

    second = lab.spawn()  # 同一份配置、同一个数据目录：不是端口先冲突，是锁
    code = second.wait()

    assert code == 2, second.output
    assert f"已经有一个在运行（进程号 {first.pid}）" in second.output
    after = read_run_info(lab.paths)
    assert (after.pid, after.started_ms, after.clean_exit) == (first.pid, before.started_ms, False)
    assert requests.get(f"{first.url}/v1/health", timeout=10).status_code == 200  # 第一个照常
    assert first.stop() == 0


def test_while_credentials_are_being_imported_the_service_cannot_start(lab, tmp_path):
    source = tmp_path / "clawbot.json"
    source.write_text(
        json.dumps(
            {
                "bot_token": BOT_TOKEN,
                "base_url": lab.ilink.base_url,
                "bot_id": "fake-bot@im.bot",
                "user_id": lab.ilink.user_id,
                "bound_at": "2026-10-01T09:00:00+08:00",
                "updates_buf": "progress-1",
                "context_token": "ctx-1",
                "context_at_ms": int(time.time() * 1000) - 3_600_000,
                "sent": 1,
            }
        ),
        encoding="utf-8",
    )
    # --probe 要向腾讯收一次消息，假腾讯慢慢回：导入这段时间一直占着锁
    lab.ilink.script(GETUPDATES, ok(msgs=[], get_updates_buf="progress-1").after(6.0))
    where = ["--home", str(lab.home), "--config", str(lab.config_file)]
    importing = lab.background("import-credentials", str(source), "--probe", *where)
    wait_for(lambda: any(r.endpoint == GETUPDATES for r in lab.ilink.requests), 30)

    refused = lab.spawn()
    code = refused.wait()

    assert code == 2, refused.output
    assert f"已经有一个在运行（进程号 {importing.pid}）" in refused.output
    assert importing.running  # 导入还没做完：是锁挡住了服务，不是别的
    assert [r.endpoint for r in lab.ilink.requests] == [GETUPDATES]  # 服务没有碰腾讯

    assert importing.wait() == 0, importing.output
    daemon = lab.start()  # 导入结束，锁放了：服务正常起来
    assert daemon.stop() == 0, daemon.output


def test_a_database_newer_than_the_program_exits_with_3_and_is_not_touched(lab):
    lab.seed("alpha")
    conn = sqlite3.connect(lab.paths.db)
    conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    before = sha(lab.paths.db)

    daemon = lab.spawn()
    code = daemon.wait()

    assert code == 3, daemon.output
    assert "数据库版本比程序新，请升级程序，不要回退" in daemon.output
    assert sha(lab.paths.db) == before
    assert lab.ilink.requests == []


def test_config_errors_exit_with_1_and_nothing_is_created(lab):
    lab.configure(
        quota={"window_limit": -1},
        server={"port": 70000},
        upstream={"profile": "x"},
    )

    daemon = lab.spawn()
    code = daemon.wait()

    assert code == 1, daemon.output
    assert "配置有 3 处错误" in daemon.output
    assert not lab.home.exists()  # 数据目录都没建，更没有 hub.db
    assert lab.ilink.requests == []


# ---------------------------------------------------------------- 权限和日志


def test_files_are_private_whatever_umask_the_process_starts_with(lab):
    daemon = lab.start(umask=0o022)  # 空的数据目录：什么都是服务自己建的
    daemon.stop()

    assert wide_permissions(lab.paths) == []
    assert stat.S_IMODE(lab.paths.home.stat().st_mode) == 0o700
    assert stat.S_IMODE(lab.paths.logs.stat().st_mode) == 0o700
    for file in (lab.paths.db, lab.paths.lock, lab.paths.run, lab.paths.log_file):
        assert stat.S_IMODE(file.stat().st_mode) == 0o600, file.name


def test_a_real_run_leaves_no_secret_or_message_body_in_the_logs(lab):
    keys = lab.seed("alpha")
    killed = {"pid": 4242, "last_heartbeat_ms": int(time.time() * 1000) - 120_000}
    lab.paths.run.write_text(json.dumps(killed) + "\n", encoding="utf-8")  # 上次被杀：要发告警
    lab.bark.script(
        PUSH, bark_error(400, f"device {DEVICE} not found")
    )  # 告警被拒，回话里带着设备码
    daemon = lab.start(guard=True)
    wait_for(lambda: lab.bark.pushes, 20)
    lab.ilink.push_user_message("INBOUND-BODY-0451")
    wait_for(lambda: lab.rows("SELECT id FROM inbox"), 20)
    sent = post(daemon, keys["alpha"], "OUTBOUND-BODY-0451", dedupe_key="DEDUPE-KEY-0451")
    assert sent.json()["status"] == "sent"
    probe = requests.get(
        f"{daemon.url}/v1/inbox?wait=0&probe=QUERY-0451", headers=auth(keys["alpha"]), timeout=10
    )
    assert probe.status_code == 200
    daemon.stop()

    text = daemon.output + "\n" + lab.log_text()
    for secret in (
        keys["alpha"],  # 项目密钥
        BOT_TOKEN,  # 登录凭证
        DEVICE,  # Bark 设备码
        "INBOUND-BODY-0451",  # 收到的话
        "OUTBOUND-BODY-0451",  # 发出的话
        "QUERY-0451",  # 请求的查询串
    ):
        assert secret not in text, secret
    assert "不允许联网" not in text  # 联网守卫在位，没有踩到：全程只碰了本机回环
    assert "收到一条消息" in text  # 日志本身有内容，只是没有正文
