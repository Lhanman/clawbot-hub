"""服务启动（任务 D6，规格 `hub-deploy`「启动自检和顺序」「谁能连」「数据目录和权限」「配置」）。

这里在测试进程里起完整的服务（`Service.start`，真 uvicorn、真线程、假时钟、假上游），看启动的八步：
先后、每一步做了什么、哪一步失败时前面已经做完了什么、失败的退出码。进程级的东西（信号、`kill -9`、
真的退出码）在 `test_lifecycle.py`。
"""

from __future__ import annotations

import asyncio
import dataclasses
import errno
import hashlib
import logging
import os
import socket
import sqlite3
import stat
import threading
import time
from pathlib import Path

import pytest
import requests
import uvicorn

import clawbot_hub.app as app_module
import clawbot_hub.db as db_module
from clawbot_hub.app import EXIT_BUSY, EXIT_DATABASE, EXIT_FAILED, Service, StartupError
from clawbot_hub.config import Config
from clawbot_hub.db import Migration, latest_version, stored_version
from clawbot_hub.instance import acquire_instance_lock, read_run_info
from clawbot_hub.loginstate import read_login
from clawbot_hub.paths import HubPaths, wide_permissions
from tests.process.conftest import call
from tests.support.fake_upstream import GET_UPDATES, NOTIFY_START, FakeUpstream
from tests.support.hub import tweak
from tests.support.threads import wait_for

STEPS = ["home", "lock", "database", "account", "restart", "listen", "threads"]


def sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def busy_port() -> tuple[socket.socket, int]:
    """占着一个本机端口（在监听）。"""
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    return blocker, blocker.getsockname()[1]


# ---------------------------------------------------------------- 先后和监听


def test_startup_runs_the_steps_in_the_order_of_the_spec(world):
    steps: list[str] = []

    world.start(on_step=steps.append)

    assert steps == STEPS


def test_a_fresh_start_creates_the_database_and_serves_requests(world, paths):
    assert not paths.db.exists()

    service = world.start()

    assert stored_version(paths.db) == latest_version()
    assert requests.get(f"{service.url}/v1/health", timeout=10).json()["ok"] is True


def test_the_default_listens_on_the_loopback_address_only(world):
    service = world.start()

    host, port = service.listening
    assert host == "127.0.0.1"
    assert service.url == f"http://127.0.0.1:{port}"
    assert port != 0  # 端口给 0 就让系统挑，这里是挑出来的


def test_the_lan_switch_listens_on_all_interfaces(make_world):
    world = make_world(tweak(Config(), server={"lan": True}))

    service = world.start()

    assert service.listening[0] == "0.0.0.0"
    assert requests.get(f"{service.url}/v1/health", timeout=10).status_code == 200


def test_an_ipv6_loopback_host_is_listened_on_as_it_is(make_world):
    try:
        with socket.socket(socket.AF_INET6) as probe:
            probe.bind(("::1", 0))
    except OSError:
        pytest.skip("这台机器没有 IPv6 回环地址")
    world = make_world(tweak(Config(), server={"host": "::1"}))

    service = world.start()

    assert service.listening[0] == "::1"
    assert requests.get(f"{service.url}/v1/health", timeout=10).status_code == 200


def test_an_ipv6_host_is_written_in_brackets_in_run_json_and_the_start_event(
    make_world, paths, monkeypatch
):
    real_bind = app_module._bind
    # 这台机器不一定有 IPv6：套接字照样绑在 IPv4 回环上，这里只看服务把监听地址写成什么样
    monkeypatch.setattr(app_module, "_bind", lambda host, port: real_bind("127.0.0.1", port))
    world = make_world(tweak(Config(), server={"host": "::1"}))

    service = world.start()

    assert service.listening[0] == "::1"
    wanted = f"[::1]:{service.listening[1]}"
    assert read_run_info(paths).listen == wanted
    assert any(wanted in event["detail"] for event in world.events())


def test_the_configured_port_is_used_when_none_is_forced(make_world):
    blocker, port = busy_port()
    blocker.close()  # 取一个眼下空闲的端口号
    world = make_world(tweak(Config(), server={"port": port}))

    service = world.start(port=None)

    assert service.listening == ("127.0.0.1", port)


# ---------------------------------------------------------------- HTTP 服务起得慢、起不来


def test_start_does_not_go_on_before_the_http_server_is_really_up(world, monkeypatch):
    real_startup = uvicorn.Server.startup
    up_at: list[float] = []

    async def slow_startup(self, sockets=None):
        await asyncio.sleep(0.3)  # 真实时间，比轮询的间隔（0.05 秒）长得多
        await real_startup(self, sockets=sockets)
        up_at.append(time.monotonic())

    monkeypatch.setattr(uvicorn.Server, "startup", slow_startup)
    reached: dict[str, float] = {}

    def note(step: str) -> None:
        reached[step] = time.monotonic()

    service = world.start(on_step=note)

    assert up_at and reached["threads"] >= up_at[0]  # 第 7 步（起线程）在 HTTP 服务起来之后
    assert requests.get(f"{service.url}/v1/health", timeout=10).status_code == 200


def test_a_http_server_that_stops_before_it_is_up_fails_the_start(world, paths, monkeypatch):
    async def gives_up(self, sockets=None):
        self.should_exit = True  # uvicorn 随后直接结束线程，`started` 始终是假

    monkeypatch.setattr(uvicorn.Server, "startup", gives_up)
    monkeypatch.setattr(app_module, "SERVER_START_SECONDS", 1.0)
    before = {thread.name for thread in threading.enumerate()}
    started = time.monotonic()

    with pytest.raises(StartupError) as caught:
        world.start()

    assert str(caught.value) == "HTTP 服务没有起来"  # 线程死了马上知道，不等到超时
    assert time.monotonic() - started < 1.0
    assert caught.value.exit_code == EXIT_FAILED
    assert {thread.name for thread in threading.enumerate()} <= before
    acquire_instance_lock(paths).release()


def test_a_http_server_that_never_comes_up_fails_the_start_after_the_limit(
    world, paths, monkeypatch
):
    async def hangs(self, sockets=None):
        while not self.should_exit:  # 关闭时停下，免得测试结束后留着线程
            await asyncio.sleep(0.01)

    monkeypatch.setattr(uvicorn.Server, "startup", hangs)
    monkeypatch.setattr(app_module, "SERVER_START_SECONDS", 1.0)
    before = {thread.name for thread in threading.enumerate()}

    with pytest.raises(StartupError) as caught:
        world.start()

    assert str(caught.value) == "HTTP 服务 1 秒内没有起来"
    assert caught.value.exit_code == EXIT_FAILED
    assert {thread.name for thread in threading.enumerate()} <= before  # 关闭时把它叫停了
    acquire_instance_lock(paths).release()


# ---------------------------------------------------------------- 失败：每一种退出码


def test_the_exit_codes_are_the_ones_the_spec_names():
    """规格「启动自检和顺序」「正常关闭」：0 正常，1 一般失败（配置错、端口被占、关闭时间用完被
    强退），2 已经有一个在运行，3 数据库比程序新、文件坏了、升级失败。launchd 和使用者都靠这几个数
    判断。"""
    assert (app_module.EXIT_OK, EXIT_FAILED, EXIT_BUSY, EXIT_DATABASE) == (0, 1, 2, 3)


def test_a_file_where_the_data_directory_should_be_fails_the_start_with_the_reason(world, tmp_path):
    occupied = tmp_path / "occupied"
    occupied.write_text("not a directory", encoding="utf-8")
    steps: list[str] = []

    with pytest.raises(StartupError) as caught:
        Service.start(
            Config(),
            paths=HubPaths(occupied),
            upstream=world.upstream,
            clock=world.clock,
            environ={},
            bark=world.bark,
            port=0,
            on_step=steps.append,
        )

    assert str(caught.value) == f"数据目录的位置上是个文件：{occupied}"
    assert caught.value.exit_code == EXIT_FAILED
    assert steps == ["home"]
    assert occupied.read_text(encoding="utf-8") == "not a directory"  # 那个文件原样
    assert world.upstream.calls == []


def test_a_data_directory_that_cannot_be_made_fails_the_start_with_the_reason(
    world, paths, monkeypatch
):
    def refused(_paths):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(app_module, "ensure_home", refused)
    steps: list[str] = []

    with pytest.raises(StartupError) as caught:
        world.start(on_step=steps.append)

    assert str(caught.value) == f"数据目录建不出来：{paths.home}（Permission denied）"
    assert caught.value.exit_code == EXIT_FAILED
    assert steps == ["home"]
    assert world.upstream.calls == []


def test_a_port_that_is_taken_fails_with_exit_1_after_the_earlier_steps_were_done(world, paths):
    blocker, port = busy_port()
    steps: list[str] = []

    with blocker, pytest.raises(StartupError) as caught:
        world.start(port=port, on_step=steps.append)

    assert caught.value.exit_code == EXIT_FAILED
    assert f"端口 {port} 已被占用" in str(caught.value)
    assert steps == STEPS[: STEPS.index("listen") + 1]  # 走到监听这一步为止，线程没起
    assert stored_version(paths.db) == latest_version()  # 前面几步照常做过：库已经建好、升好
    assert not paths.run.exists()  # 没开始运行
    assert world.upstream.calls == []  # 没有向腾讯发任何请求
    acquire_instance_lock(paths).release()  # 锁已经放掉了


def test_a_failed_start_gives_back_everything_it_took(world, paths):
    blocker, port = busy_port()
    before = {thread.name for thread in threading.enumerate()}
    handlers = list(logging.getLogger().handlers)

    with blocker, pytest.raises(StartupError):
        world.start(port=port)

    assert {thread.name for thread in threading.enumerate()} <= before  # 后台线程一个也没留
    assert logging.getLogger().handlers == handlers  # 装上的日志处理器也卸了


def test_a_start_that_fails_after_the_port_was_bound_gives_the_port_back(world, paths, monkeypatch):
    async def gives_up(self, sockets=None):
        self.should_exit = True  # uvicorn 没走到自己收尾的那一步，套接字得由服务来关

    monkeypatch.setattr(uvicorn.Server, "startup", gives_up)
    holder, port = busy_port()
    holder.close()  # 取一个眼下空闲的端口号

    with pytest.raises(StartupError) as caught:
        world.start(port=port)

    # `caught` 的回溯还抓着没起来的 Service：端口是它自己放掉的，不是等垃圾回收
    assert caught.traceback
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))


def test_a_normal_stop_closes_the_database(world):
    service = world.start()

    service.stop()

    with pytest.raises(RuntimeError, match="数据库已经关闭"):
        service.db.conn()


def test_close_without_a_normal_stop_closes_the_database_the_port_and_the_lock(world, paths):
    service = world.start()
    host, port = service.listening

    service.close()

    with pytest.raises(RuntimeError, match="数据库已经关闭"):
        service.db.conn()
    with socket.socket() as probe:
        probe.bind((host, port))
    acquire_instance_lock(paths).release()


def test_a_second_instance_exits_with_2_and_touches_nothing(world, paths):
    first = world.start()
    other = FakeUpstream(clock=world.clock)
    steps: list[str] = []
    run_before = paths.run.read_bytes()

    with pytest.raises(StartupError) as caught:
        world.start(upstream=other, on_step=steps.append)

    assert caught.value.exit_code == EXIT_BUSY
    assert f"已经有一个在运行（进程号 {os.getpid()}）" in str(caught.value)
    assert steps == ["home", "lock"]  # 没有碰数据库，更没有监听端口
    assert other.calls == []
    assert paths.run.read_bytes() == run_before  # 第一个的 run.json 原样
    assert requests.get(f"{first.url}/v1/health", timeout=10).status_code == 200


def test_the_lock_is_free_again_after_a_normal_stop(world, paths):
    service = world.start()
    assert service.stop() == 0

    acquire_instance_lock(paths).release()
    again = world.start()  # 停了再起，没有任何残留

    assert requests.get(f"{again.url}/v1/health", timeout=10).status_code == 200


def test_a_database_newer_than_the_program_fails_with_exit_3_and_is_left_alone(world, paths):
    with world.db.txn() as conn:
        conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    world.db.close()
    del world.__dict__["db"]  # 不再用这个连接（关掉了）
    before = sha(paths.db)
    steps: list[str] = []

    with pytest.raises(StartupError) as caught:
        world.start(on_step=steps.append)

    assert caught.value.exit_code == EXIT_DATABASE
    assert "数据库版本比程序新，请升级程序，不要回退" in str(caught.value)
    assert steps == ["home", "lock", "database"]
    assert sha(paths.db) == before  # 数据库文件一个字节都没动
    assert not [name for name in os.listdir(paths.home) if name.startswith("hub.db-")]
    assert world.upstream.calls == []
    acquire_instance_lock(paths).release()


def test_a_file_that_is_not_a_database_fails_with_exit_3(world, paths):
    paths.db.write_bytes(b"this is not a sqlite database at all\n" * 20)
    before = sha(paths.db)

    with pytest.raises(StartupError) as caught:
        world.start()

    assert caught.value.exit_code == EXIT_DATABASE
    assert sha(paths.db) == before
    assert world.upstream.calls == []


def sqlite_error(code: int, text: str) -> sqlite3.OperationalError:
    """像 sqlite3 自己抛的那样带着错误码。"""
    error = sqlite3.OperationalError(text)
    error.sqlite_errorcode = code
    return error


@pytest.mark.parametrize(
    "code",
    [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_BUSY_SNAPSHOT],
    ids=["busy", "locked", "extended-busy-code"],
)
def test_a_database_held_by_another_program_fails_with_exit_1_and_a_plain_reason(
    world, paths, monkeypatch, code
):
    def held(*args, **kwargs):
        raise sqlite_error(code, "database is locked")

    monkeypatch.setattr(app_module, "migrate", held)
    steps: list[str] = []

    with pytest.raises(StartupError) as caught:
        world.start(on_step=steps.append)

    assert str(caught.value) == "数据库正忙：别的程序占着它，稍后再试"
    assert caught.value.exit_code == EXIT_FAILED
    assert steps == ["home", "lock", "database"]
    assert world.upstream.calls == []
    acquire_instance_lock(paths).release()  # 锁还了


def test_any_other_database_error_is_not_passed_off_as_busy(world, paths, monkeypatch):
    unexpected = sqlite_error(sqlite3.SQLITE_ERROR, "no such table: nowhere")

    def broken(*args, **kwargs):
        raise unexpected

    monkeypatch.setattr(app_module, "migrate", broken)

    with pytest.raises(sqlite3.OperationalError) as caught:
        world.start()

    assert caught.value is unexpected  # 原样抛出，不改写成“正忙”
    acquire_instance_lock(paths).release()  # 失败了照样把拿到的东西还掉


def test_a_failed_migration_fails_with_exit_3_and_keeps_the_old_data_and_the_backup(
    world, paths, monkeypatch
):
    world.hub.add("alpha", guaranteed=6, cap=8)
    world.db.close()
    del world.__dict__["db"], world.__dict__["hub"]
    real = db_module.load_migrations

    def with_a_broken_second_script():
        scripts = real()
        broken = Migration(len(scripts) + 1, "broken", "CREATE TABLE half (id INTEGER); NOT SQL;")
        return [*scripts, broken]

    monkeypatch.setattr(db_module, "load_migrations", with_a_broken_second_script)

    with pytest.raises(StartupError) as caught:
        world.start()

    assert caught.value.exit_code == EXIT_DATABASE
    assert stored_version(paths.db) == 1  # 回到升级前的样子
    conn = sqlite3.connect(f"file:{paths.db}?mode=ro", uri=True)  # 不再经过会重新升级的 world.db
    try:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        assert "half" not in names
        assert conn.execute("SELECT name FROM projects WHERE name = 'alpha'").fetchall() == [
            ("alpha",)
        ]
    finally:
        conn.close()
    assert [p.name for p in paths.backups.iterdir() if p.name.startswith("pre-upgrade-v1-v2-")]


def test_an_older_database_is_upgraded_after_a_backup_and_keeps_its_data(world, paths, monkeypatch):
    world.hub.ready("alpha")
    world.db.close()
    del world.__dict__["db"], world.__dict__["hub"]
    real = db_module.load_migrations

    def with_a_second_script():
        scripts = real()
        return [*scripts, Migration(len(scripts) + 1, "extra", "CREATE TABLE extra (id INTEGER);")]

    monkeypatch.setattr(db_module, "load_migrations", with_a_second_script)

    service = world.start()

    assert stored_version(paths.db) == 2
    assert [p.name for p in paths.backups.iterdir() if p.name.startswith("pre-upgrade-v1-v2-")]
    assert service.db.conn().execute("SELECT COUNT(*) FROM projects").fetchone()[0] >= 1
    assert service.db.conn().execute("SELECT COUNT(*) FROM windows").fetchone()[0] == 1


# ---------------------------------------------------------------- 凭证文件（第 5 步）


def test_a_broken_account_file_still_starts_and_sends_fail(world):
    world.hub.add("alpha", guaranteed=6, cap=8)
    world.paths.account.write_text("{this is not json", encoding="utf-8")
    world.paths.account.chmod(0o600)

    service = world.start()

    response = call(service, "POST", "/v1/messages", key=world.key(), json={"text": "你好"})
    body = response.json()
    assert (body["status"], body["reason_code"]) == ("failed", "login_broken")
    assert world.upstream.calls == []  # 没有向腾讯发任何请求


def test_no_account_file_still_starts_and_sends_fail(world):
    world.hub.add("alpha", guaranteed=6, cap=8)
    assert not world.paths.account.exists()

    service = world.start()

    response = call(service, "POST", "/v1/messages", key=world.key(), json={"text": "你好"})
    body = response.json()
    assert (body["status"], body["reason_code"]) == ("failed", "login_missing")
    assert world.upstream.calls == []


def test_the_first_start_records_the_digest_of_the_credentials_on_disk(world):
    world.hub.bind()
    with world.db.txn() as conn:  # 像是库里还没有摘要的第一次启动
        conn.execute("UPDATE account_state SET creds_digest = NULL, login_state = 'missing'")

    service = world.start()

    state = read_login(service.db.conn())
    assert state.state == "ok"
    assert state.digest == world.hub.store.read().digest
    assert state.changed_ms == world.clock.now_ms()  # 没有摘要也算变了


def test_credentials_rebound_while_the_service_was_down_are_adopted_at_startup(world):
    hub = world.hub
    hub.ready("alpha")
    hub.expire()  # 之前登录失效了
    rebound = dataclasses.replace(hub.store.read(), bot_token="tok-rebound-while-down")
    hub.store.write(rebound)  # 服务停着时做过 hubctl login：只改了文件

    service = world.start()

    state = read_login(service.db.conn())
    assert state.state == "ok"  # 清掉了登录失效
    assert state.digest == rebound.digest
    assert service.db.conn().execute("SELECT voided FROM windows").fetchall()[0]["voided"] == 1
    assert world.rows("SELECT context_token FROM windows") == [{"context_token": None}]


def test_unchanged_credentials_keep_the_window_open(world):
    world.hub.ready("alpha")

    service = world.start()

    assert service.db.conn().execute("SELECT voided FROM windows").fetchone()["voided"] == 0


# ---------------------------------------------------------------- 上一次的运行（第 6 步）


def test_a_killed_previous_run_interrupts_sends_and_registers_one_restart_alert(world):
    world.hub.ready("alpha")
    outbox_id = world.leave_sending("alpha", parts=2)
    used = world.rows("SELECT project_id, kind, n FROM usage")
    world.run_json()  # 上一次没有正常退出

    service = world.start()

    [row] = world.rows("SELECT status, reason_code FROM outbox WHERE id = ?", outbox_id)
    assert (row["status"], row["reason_code"]) == ("interrupted", "interrupted")
    assert world.rows("SELECT project_id, kind, n FROM usage") == used  # 预占的条数保留
    wait_for(lambda: len(world.bark.calls) == 1)  # 由告警线程发出
    [alert] = world.bark.calls
    assert alert.title == "ClawBot 共用服务 · 服务重启"
    assert "1 条发送中断" in alert.text
    assert service.interrupted == 1
    assert service.previous is not None and service.previous.clean_exit is False
    details = [event["detail"] for event in world.events()]
    # 假时钟从北京时间 10:00 起，`run_json()` 的最后心跳是 2 分钟前
    assert any(
        "没有正常退出（最后心跳 09:58），1 条仍在发送的记录已标成中断" in detail
        for detail in details
    )


def test_the_last_heartbeat_in_the_event_is_shown_in_the_display_timezone(make_world):
    world = make_world(tweak(Config(), display={"timezone": "Asia/Tokyo"}))
    world.run_json()

    world.start()

    details = [event["detail"] for event in world.events()]
    assert any("没有正常退出（最后心跳 10:58）" in detail for detail in details)


def test_a_killed_previous_run_without_unfinished_sends_still_alerts_once(world):
    world.hub.ready("alpha")
    world.run_json()

    world.start()

    wait_for(lambda: len(world.bark.calls) == 1)
    assert "条发送中断" not in world.bark.calls[0].text
    [event] = [e for e in world.events() if "没有正常退出" in e["detail"]]
    assert event["detail"] == "上一次运行没有正常退出（最后心跳 09:58）"  # 没有中断的就不提
    assert event["level"] == "WARNING"


def test_a_clean_previous_run_is_not_an_incident(world):
    world.hub.ready("alpha")
    world.run_json(clean_exit=True)

    service = world.start()

    assert service.interrupted == 0
    assert world.rows("SELECT * FROM alert_state WHERE key = 'service_restarted'") == []
    assert world.bark.calls == []


def test_the_very_first_start_is_not_an_incident(world):
    service = world.start()

    assert service.previous is None
    assert world.rows("SELECT * FROM alert_state WHERE key = 'service_restarted'") == []
    assert world.bark.calls == []


def test_unfinished_sends_are_interrupted_even_when_the_previous_run_looks_clean(world):
    world.hub.ready("alpha")
    outbox_id = world.leave_sending("alpha")
    world.run_json(clean_exit=True)  # 标记说正常退出，库里却还有"发送中"：以库为准

    service = world.start()

    [row] = world.rows("SELECT status FROM outbox WHERE id = ?", outbox_id)
    assert row["status"] == "interrupted"
    assert service.interrupted == 1
    wait_for(lambda: len(world.bark.calls) == 1)
    [event] = [e for e in world.events() if "仍在发送" in e["detail"]]
    assert event["detail"] == "上一次运行留下 1 条仍在发送的记录，已标成中断"
    assert event["level"] == "WARNING"


def test_bark_trouble_does_not_delay_listening(world, make_world):
    from tests.process.conftest import Slow

    slow = Slow(world.clock)
    world.hub.ready("alpha")
    world.run_json()

    service = world.start(bark=slow)

    slow.entered.wait(5)  # 告警线程已经在发了，卡在 Bark 那边
    assert slow.calls == []  # 还没有回话
    assert requests.get(f"{service.url}/v1/health", timeout=10).status_code == 200  # 照常监听
    response = call(service, "GET", "/v1/status", key=world.key())
    assert response.status_code == 200  # 接口也照常用
    slow.release()
    wait_for(lambda: len(slow.calls) == 1)


def test_a_missing_bark_key_does_not_stop_the_service(world):
    world.hub.ready("alpha")
    world.run_json()

    service = world.start(environ={})  # 没有设备码

    assert requests.get(f"{service.url}/v1/health", timeout=10).status_code == 200
    assert world.bark.calls == []


# ---------------------------------------------------------------- 警告：只写事件，不拒绝启动


def warnings_of(world) -> list[str]:
    return [e["detail"] for e in world.events() if e["level"] == "WARNING"]


def test_guarantees_above_the_window_are_only_a_warning_event(make_world):
    world = make_world()
    world.hub.add("alpha", guaranteed=6, cap=8)
    world.hub.add("beta", guaranteed=2, cap=4)
    shrunk = tweak(world.config, quota={"window_limit": 8})  # 6 + 2 + 回复保留 2 > 8

    service = world.start(shrunk)

    [detail] = [d for d in warnings_of(world) if "保底合计" in d]
    assert "8" in detail and "alpha" in detail and "beta" in detail
    assert requests.get(f"{service.url}/v1/health", timeout=10).status_code == 200


def test_a_shutdown_wait_shorter_than_the_send_timeout_is_only_a_warning_event(make_world):
    world = make_world(tweak(Config(), server={"shutdown_seconds": 20}))

    service = world.start()

    [detail] = [d for d in warnings_of(world) if "shutdown_seconds" in d]
    assert "20" in detail and "30" in detail
    assert requests.get(f"{service.url}/v1/health", timeout=10).status_code == 200


def test_a_prefix_equal_to_a_confirm_word_is_only_a_warning_event(make_world):
    world = make_world()
    world.hub.add("alpha", guaranteed=2, cap=4, prefix="量化")
    changed = tweak(world.config, routing={"confirm_words": ["量化", "是"]})

    service = world.start(changed)

    [detail] = [d for d in warnings_of(world) if "量化" in d]
    assert "alpha" in detail and "routing.confirm_words" in detail
    assert requests.get(f"{service.url}/v1/health", timeout=10).status_code == 200


def test_no_warnings_when_everything_is_in_order(world):
    world.hub.add("alpha", guaranteed=2, cap=4)

    world.start()

    assert warnings_of(world) == []


# ---------------------------------------------------------------- 权限（第 2 步）


def test_wide_permissions_are_repaired_with_one_warning_each(world, paths, caplog):
    paths.account.write_text("{}", encoding="utf-8")
    paths.logs.mkdir(mode=0o700, exist_ok=True)
    paths.launchd_log.write_text("launchd 写的\n", encoding="utf-8")
    paths.account.chmod(0o644)
    paths.launchd_log.chmod(0o644)

    with caplog.at_level(logging.WARNING):
        world.start()

    assert stat.S_IMODE(paths.account.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.launchd_log.stat().st_mode) == 0o600
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len([m for m in warned if "account.json" in m]) == 1
    assert len([m for m in warned if "launchd.log" in m]) == 1
    assert wide_permissions(paths) == []


def test_permissions_that_cannot_be_repaired_stop_the_start_and_name_the_file(
    world, paths, monkeypatch
):
    def refused(_paths):
        raise PermissionError(errno.EPERM, "Operation not permitted", str(paths.account))

    monkeypatch.setattr(app_module, "fix_permissions", refused)
    steps: list[str] = []

    with pytest.raises(StartupError) as caught:
        world.start(on_step=steps.append)

    message = str(caught.value)
    assert message.startswith(f"{paths.account} 的权限比规定的宽，又改不回去")
    assert "Operation not permitted" in message
    assert "chmod 700 / 600" in message
    assert caught.value.exit_code == EXIT_FAILED
    assert steps == ["home"]  # 凭证放在别人读得到的地方，不如不起：锁、数据库都没碰
    assert world.upstream.calls == []
    acquire_instance_lock(paths).release()


def test_an_unrepairable_permission_with_no_file_named_points_at_the_data_directory(
    world, paths, monkeypatch
):
    def refused(_paths):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(app_module, "fix_permissions", refused)

    with pytest.raises(StartupError) as caught:
        world.start()

    message = str(caught.value)
    assert message.startswith(f"{paths.home} 的权限比规定的宽，又改不回去")
    assert "None" not in message


def test_a_repaired_path_is_named_relative_to_the_data_directory_when_it_is_inside(paths):
    assert app_module._relative(paths.home / "logs" / "launchd.log", paths) == "logs/launchd.log"


def test_a_repaired_path_outside_the_data_directory_is_named_in_full(paths):
    elsewhere = Path("/opt/example/elsewhere.log")

    assert app_module._relative(elsewhere, paths) == str(elsewhere)


def test_a_fresh_start_creates_only_private_files_and_directories(world, paths):
    service = world.start()
    service.stop()

    assert wide_permissions(paths) == []
    assert stat.S_IMODE(paths.home.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.logs.stat().st_mode) == 0o700
    for file in (paths.db, paths.lock, paths.run, paths.log_file):
        assert stat.S_IMODE(file.stat().st_mode) == 0o600, file.name


# ---------------------------------------------------------------- 后台线程（第 7 步）


def test_start_runs_every_background_worker_and_a_normal_stop_ends_them_all(world):
    service = world.start()
    workers = [service.alerts, service.replier, service.poller, service.watcher, service.scheduler]
    names = {"hub-http", "hub-poller", "hub-replier", "scheduler", "credential-watcher", "alerts"}

    assert all(worker.running for worker in workers)
    mine = [thread for thread in threading.enumerate() if thread.name in names]
    assert {thread.name for thread in mine} == names
    assert all(thread.daemon for thread in mine)  # 停不下来的线程不能拖住进程退出

    assert service.stop() == 0

    assert not any(worker.running for worker in workers)
    assert not names & {thread.name for thread in threading.enumerate()}


# ---------------------------------------------------------------- run.json、启动日志、事件


def test_run_json_describes_this_run(world):
    started = world.clock.now_ms()

    service = world.start()

    run = read_run_info(world.paths)
    assert run is not None
    assert (run.pid, run.started_ms, run.clean_exit) == (os.getpid(), started, False)
    assert run.listen == f"127.0.0.1:{service.listening[1]}"
    assert (run.profile, run.confirm) == ("legacy", "observe")
    assert run.version != ""
    assert run.last_heartbeat_ms >= started


def test_the_heartbeat_is_renewed_every_30_seconds(world):
    started = world.clock.now_ms()
    world.start()

    for beat in (1, 2):  # 70 秒：线程睡下以后再拨，一次拨 30 秒
        wait_for(lambda beat=beat: started + beat * 30_000 in world.clock.pending_deadlines())
        world.clock.advance(30)
    world.clock.advance(10)

    wait_for(lambda: read_run_info(world.paths).last_heartbeat_ms >= started + 60_000)


def test_the_started_line_names_version_address_profile_and_project_count(world, paths):
    world.hub.add("alpha", guaranteed=2, cap=4)
    world.hub.add("beta", guaranteed=2, cap=4)

    service = world.start()

    text = paths.log_file.read_text(encoding="utf-8")
    [line] = [line for line in text.splitlines() if "已启动" in line]
    port = service.listening[1]
    assert f"监听 127.0.0.1:{port}" in line
    assert "上游档位 legacy" in line
    assert "项目 2 个" in line
    assert "版本 " in line


def test_start_leaves_a_lifecycle_event(world):
    service = world.start()

    details = [event["detail"] for event in world.events()]
    assert any("服务启动" in detail and str(service.listening[1]) in detail for detail in details)


# ---------------------------------------------------------------- 官方档的 notifystart


def official() -> Config:
    return tweak(Config(), upstream={"profile": "official"})


def test_the_official_profile_calls_notifystart_before_receiving(make_world):
    world = make_world(official())
    world.hub.bind()

    world.start()

    wait_for(lambda: any(c.operation == GET_UPDATES for c in world.upstream.calls))
    operations = [c.operation for c in world.upstream.calls]
    assert operations.count(NOTIFY_START) == 1
    assert operations.index(NOTIFY_START) < operations.index(GET_UPDATES)


def test_the_legacy_profile_never_calls_notifystart(world):
    world.hub.bind()

    world.start()

    wait_for(lambda: any(c.operation == GET_UPDATES for c in world.upstream.calls))
    assert NOTIFY_START not in [c.operation for c in world.upstream.calls]


def test_a_failing_notifystart_does_not_stop_the_service(make_world, caplog):
    world = make_world(official())
    world.hub.bind()
    world.upstream.script(NOTIFY_START, RuntimeError("notifystart 返回了错误"))

    with caplog.at_level(logging.WARNING):
        service = world.start()

    wait_for(lambda: any(c.operation == GET_UPDATES for c in world.upstream.calls))  # 照常开始收
    assert requests.get(f"{service.url}/v1/health", timeout=10).status_code == 200
    assert any(
        "notifystart" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records
    )


def test_there_is_no_notifystart_without_credentials(make_world, caplog):
    world = make_world(official())

    with caplog.at_level(logging.WARNING):
        service = world.start()
        assert service.stop() == 0

    assert world.upstream.calls == []  # 没绑定：不知道用谁的名义通知（开始和退出都不通知）
    assert [
        r.getMessage() for r in caplog.records if "notify" in r.getMessage()
    ] == []  # 也不是出错
