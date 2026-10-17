"""服务关闭（任务 D6，规格 `hub-deploy`「正常关闭」）。

收到停止信号以后：新请求 503 `shutting_down`；等发送锁的发送马上 `failed` / `busy`；正在向腾讯发的
那一条发完（最多等 `server.shutdown_seconds`）；取话请求马上返回空；`official` 档调 `notifystop`；
写"正常退出"标记、放锁。信号本身（`SIGTERM` / `SIGINT`）在 `test_lifecycle.py`，这里直接调
`Service.stop()`，时间用假时钟。
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time

import pytest
import requests
import uvicorn

import clawbot_hub.app as app_module
from clawbot_hub.config import Config
from clawbot_hub.instance import acquire_instance_lock, read_run_info
from tests.process.conftest import call
from tests.support.fake_upstream import (
    DEFAULT,
    GET_UPDATES,
    NOTIFY_START,
    NOTIFY_STOP,
    SEND_TEXT,
)
from tests.support.hub import tweak
from tests.support.threads import run_in_thread, wait_for


def official() -> Config:
    return tweak(Config(), upstream={"profile": "official"})


def operations(world) -> list[str]:
    return [c.operation for c in world.upstream.calls]


def shutting_down(service):
    return wait_for(service.services.shutting_down.is_set)


def in_flight(service, count: int):
    return wait_for(lambda: service.services.in_flight.count >= count)


def stop_in_thread(world, service, seconds: int):
    """另起线程调 `service.stop()`，等到它自己睡下、要等到 `seconds` 秒以后为止才返回。

    别的线程（心跳、定时任务）也可能正好在等同一刻，所以数的是"这一刻上多了一个等待"，不是
    "有没有人在等这一刻"：否则测试会赶在 `stop` 算出期限之前就把假时间拨过去，期限跟着往后移。"""
    deadline = world.clock.now_ms() + seconds * 1000
    others = world.clock.pending_deadlines().count(deadline)
    stopping = run_in_thread(service.stop)
    wait_for(lambda: world.clock.pending_deadlines().count(deadline) == others + 1)
    return stopping


# ---------------------------------------------------------------- 平常的关闭


def test_stop_marks_a_clean_exit_and_gives_back_the_lock_and_the_port(world, paths, caplog):
    service = world.start()
    port = service.listening[1]

    with caplog.at_level(logging.INFO):
        code = service.stop()

    assert code == 0
    assert service.forced is False  # 没有被强退
    run = read_run_info(paths)
    assert run is not None and run.clean_exit is True
    acquire_instance_lock(paths).release()
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port), timeout=2)
    details = [event["detail"] for event in world.events()]
    assert any("正常退出" in detail for detail in details)
    assert any("正常退出" in line for line in paths.log_file.read_text("utf-8").splitlines())


def test_stop_twice_is_harmless(world):
    service = world.start()

    assert service.stop() == 0
    assert service.stop() == 0
    service.close()  # 关完了再关也没关系


def test_a_stopped_service_leaves_no_threads_behind(world):
    import threading

    before = {thread.name for thread in threading.enumerate()}
    service = world.start()

    service.stop()

    assert {thread.name for thread in threading.enumerate()} <= before


# ---------------------------------------------------------------- 正在发的那一条


def test_an_in_flight_send_finishes_before_the_service_exits(world, paths):
    world.hub.ready("alpha")
    gate = world.upstream.hold(SEND_TEXT)
    service = world.start()
    sending = run_in_thread(
        call, service, "POST", "/v1/messages", key=world.key(), json={"text": "盘点完成"}
    )
    assert gate.entered.wait(5)

    stopping = run_in_thread(service.stop)
    shutting_down(service)
    assert stopping.still_blocked()  # 还在等那一条发完
    assert read_run_info(paths).clean_exit is False  # 没发完之前不写"正常退出"

    gate.release()
    response = sending.result()
    assert response.status_code == 200 and response.json()["status"] == "sent"
    assert stopping.result() == 0
    assert read_run_info(paths).clean_exit is True
    assert world.upstream.sent[0].text == "盘点完成"


def test_requests_during_shutdown_get_503_and_never_touch_tencent(world):
    world.hub.ready("alpha")
    gate = world.upstream.hold(SEND_TEXT)
    service = world.start()
    sending = run_in_thread(
        call, service, "POST", "/v1/messages", key=world.key(), json={"text": "在途的一条"}
    )
    assert gate.entered.wait(5)
    stopping = run_in_thread(service.stop)
    shutting_down(service)

    posted = call(service, "POST", "/v1/messages", key=world.key(), json={"text": "新来的"})
    fetched = call(service, "GET", "/v1/inbox", key=world.key(), params={"wait": 0})
    health = requests.get(f"{service.url}/v1/health", timeout=10)

    for response in (posted, fetched, health):
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "shutting_down"
    assert len(world.upstream.sent) == 1  # 只有那条在途的，新来的没碰腾讯
    gate.release()
    assert sending.result().json()["status"] == "sent"
    assert stopping.result() == 0
    assert [m.text for m in world.upstream.sent] == ["在途的一条"]


def test_a_send_waiting_for_the_lock_gets_busy_and_its_quota_back(world):
    hub = world.hub
    hub.ready("alpha")
    hub.add("beta", guaranteed=2, cap=4)
    gate = world.upstream.hold(SEND_TEXT)
    service = world.start()
    first = run_in_thread(
        call, service, "POST", "/v1/messages", key=world.key(), json={"text": "占着发送锁"}
    )
    assert gate.entered.wait(5)
    second = run_in_thread(
        call, service, "POST", "/v1/messages", key=world.key("beta"), json={"text": "在等锁"}
    )
    wait_for(lambda: world.rows("SELECT id FROM outbox WHERE status = 'sending'").__len__() == 2)

    stopping = run_in_thread(service.stop)
    shutting_down(service)

    waited = second.result()  # 马上结束，不用等占锁的那条
    body = waited.json()
    assert (body["status"], body["reason_code"]) == ("failed", "busy")
    assert body["parts_sent"] == 0
    used = world.rows(
        "SELECT u.n AS n FROM usage u JOIN projects p ON p.id = u.project_id"
        " WHERE p.name = 'beta' AND u.kind = 'push'"
    )
    assert all(row["n"] == 0 for row in used)  # 预占的条数退回了
    assert len(world.upstream.sent) == 1  # 没碰腾讯
    gate.release()
    assert first.result().json()["status"] == "sent"
    assert stopping.result() == 0
    assert [m.text for m in world.upstream.sent] == ["占着发送锁"]


def test_a_waiting_inbox_request_returns_an_empty_list_within_a_second(world):
    world.hub.ready("alpha")
    service = world.start()
    waiting = run_in_thread(call, service, "GET", "/v1/inbox", key=world.key(), params={"wait": 25})
    in_flight(service, 1)  # 请求到了，挂着

    started = time.monotonic()
    stopping = run_in_thread(service.stop)
    response = waiting.result()

    assert time.monotonic() - started < 1.0
    assert response.status_code == 200
    assert response.json()["items"] == []
    assert stopping.result() == 0


def test_a_service_reply_in_flight_is_waited_for_too(world):
    world.hub.ready("alpha")
    gate = world.upstream.hold(SEND_TEXT)
    service = world.start()
    world.upstream.push_user_message("服务 状态")  # 服务自己回，由回复线程去发
    assert gate.entered.wait(5)

    stopping = run_in_thread(service.stop)
    shutting_down(service)
    assert stopping.still_blocked()
    gate.release()

    assert stopping.result() == 0
    assert len(world.upstream.sent) == 1
    assert world.upstream.sent[0].text.startswith("【服务】")


def test_a_service_reply_stuck_past_the_shutdown_time_forces_the_exit_like_any_send(world):
    world.hub.ready("alpha")
    gate = world.upstream.hold(SEND_TEXT)
    service = world.start()
    world.upstream.push_user_message("服务 状态")
    assert gate.entered.wait(5)

    stopping = stop_in_thread(
        world, service, 35
    )  # 和项目发的消息一样：按 server.shutdown_seconds 等
    world.clock.advance(36)
    try:
        assert stopping.result() == 1
        assert service.forced is True
    finally:
        gate.release()


# ---------------------------------------------------------------- 关闭时间用完


def test_when_the_shutdown_time_runs_out_the_exit_is_forced(make_world, paths, caplog):
    world = make_world(official())
    world.hub.ready("alpha")
    gate = world.upstream.hold(SEND_TEXT)
    service = world.start()
    sending = run_in_thread(
        call, service, "POST", "/v1/messages", key=world.key(), json={"text": "卡住的一条"}
    )
    assert gate.entered.wait(5)

    with caplog.at_level(logging.ERROR):
        stopping = stop_in_thread(world, service, 35)  # stop 睡下等发送
        world.clock.advance(36)
        code = stopping.result()

    try:
        assert code == 1
        assert service.forced is True
        assert read_run_info(paths).clean_exit is False  # 不记"正常退出"
        assert world.rows("SELECT status FROM outbox") == [
            {"status": "sending"}
        ]  # 这条下次启动标中断
        assert NOTIFY_STOP not in operations(world)  # 强退不做收尾
        assert any("强行退出" in r.getMessage() for r in caplog.records)
        assert not any("正常退出" in e["detail"] for e in world.events())
    finally:
        gate.release()
        sending.result()


def test_after_a_forced_exit_the_next_start_interrupts_the_send_and_alerts(make_world, paths):
    world = make_world()
    world.hub.ready("alpha")
    gate = world.upstream.hold(SEND_TEXT)
    service = world.start()
    sending = run_in_thread(
        call, service, "POST", "/v1/messages", key=world.key(), json={"text": "卡住的一条"}
    )
    assert gate.entered.wait(5)
    used = world.rows("SELECT project_id, kind, n FROM usage")
    stopping = stop_in_thread(world, service, 35)
    world.clock.advance(36)
    assert stopping.result() == 1
    service.close()  # 真的进程会被 os._exit 带走；这里把锁和端口还回去

    try:
        second = world.start()

        assert second.interrupted == 1
        assert world.rows("SELECT status FROM outbox") == [{"status": "interrupted"}]
        assert world.rows("SELECT project_id, kind, n FROM usage") == used  # 预占的条数保留
        wait_for(lambda: len(world.bark.calls) == 1)
        assert world.bark.titles == ["ClawBot 共用服务 · 服务重启"]
    finally:
        gate.release()
        sending.result()


def test_the_wait_is_as_long_as_the_configured_shutdown_seconds(make_world):
    world = make_world(tweak(Config(), server={"shutdown_seconds": 5}))
    world.hub.ready("alpha")
    gate = world.upstream.hold(SEND_TEXT)
    service = world.start()
    sending = run_in_thread(
        call, service, "POST", "/v1/messages", key=world.key(), json={"text": "卡住的一条"}
    )
    assert gate.entered.wait(5)

    stopping = stop_in_thread(world, service, 5)  # 别的线程也有一个 5 秒的定时，别混了
    world.clock.advance(4)
    assert stopping.still_blocked()  # 4 秒还没到期
    world.clock.advance(2)

    try:
        assert stopping.result() == 1
    finally:
        gate.release()
        sending.result()


# ---------------------------------------------------------------- 各线程最多等多久


WORKERS = ["poller", "replier", "watcher", "scheduler", "alerts"]


def record_stop_waits(service, monkeypatch) -> list[tuple[str, float]]:
    """记下服务叫各个后台线程停下时给的等待时间（真实秒），线程照样停。"""
    given: list[tuple[str, float]] = []
    for name in WORKERS:
        worker = getattr(service, name)

        def recording(timeout, _name=name, _real=worker.stop):
            given.append((_name, timeout))
            _real(timeout)

        monkeypatch.setattr(worker, "stop", recording)
    return given


def test_a_normal_stop_lets_each_worker_take_the_long_wait_to_finish_what_it_is_doing(
    world, monkeypatch
):
    service = world.start()
    given = record_stop_waits(service, monkeypatch)

    assert service.stop() == 0

    long_wait = app_module.WORKER_JOIN_SECONDS
    assert sorted(given) == [  # 每个线程只叫停一次（收尾时的 `close()` 不会再叫一遍）
        ("alerts", long_wait),
        ("poller", app_module.POLLER_GRACE_SECONDS),  # 它可能挂在腾讯那边，只多等一小会儿
        ("replier", long_wait),
        ("scheduler", long_wait),
        ("watcher", long_wait),
    ]


def test_closing_without_a_normal_stop_gives_the_workers_only_the_short_wait(world, monkeypatch):
    service = world.start()
    given = record_stop_waits(service, monkeypatch)

    service.close()

    short_wait = app_module.CLOSE_JOIN_SECONDS
    assert sorted(given) == [
        ("alerts", short_wait),
        ("poller", app_module.POLLER_GRACE_SECONDS),
        ("replier", short_wait),
        ("scheduler", short_wait),
        ("watcher", short_wait),
    ]
    assert not any(getattr(service, name).running for name in WORKERS)

    service.close()  # 重复调没关系：不会再叫一遍
    assert len(given) == len(WORKERS)


def test_a_http_server_that_will_not_wind_down_is_forced_after_the_wait(world, monkeypatch):
    async def hangs_until_forced(self, sockets=None):
        while not self.force_exit:  # 自己收尾收不完，要等到被催才结束
            await asyncio.sleep(0.1)  # 慢一点看：不等它结束就返回的话，返回时它还在

    monkeypatch.setattr(uvicorn.Server, "shutdown", hangs_until_forced)
    monkeypatch.setattr(app_module, "SERVER_STOP_SECONDS", 0.1)
    service = world.start()
    started = time.monotonic()

    assert service.stop() == 0

    took = time.monotonic() - started
    assert "hub-http" not in {thread.name for thread in threading.enumerate()}  # 催了，也等到了
    assert took < 0.9  # 正常关闭时只等 SERVER_STOP_SECONDS（这里 0.1 秒）就催，不是等满 1 秒


def test_closing_without_a_normal_stop_also_starts_refusing_requests(world):
    service = world.start()
    assert not service.services.shutting_down.is_set()

    service.close()

    assert service.services.shutting_down.is_set()


# ---------------------------------------------------------------- 官方档的 notifystop


def test_the_official_profile_calls_notifystop_before_it_exits(make_world):
    world = make_world(official())
    world.hub.bind()
    service = world.start()
    wait_for(lambda: GET_UPDATES in operations(world))

    assert service.stop() == 0

    assert operations(world).count(NOTIFY_START) == 1
    assert operations(world).count(NOTIFY_STOP) == 1
    assert operations(world).index(NOTIFY_STOP) > operations(world).index(NOTIFY_START)


def test_notifystop_goes_out_after_the_http_server_and_every_worker_have_stopped(make_world):
    """告诉腾讯“下线了”之后，不该再有收消息、发送、对外的 HTTP 在动。"""
    world = make_world(official())
    world.hub.bind()
    service = world.start()
    wait_for(lambda: GET_UPDATES in operations(world))
    workers = ["poller", "replier", "watcher", "scheduler", "alerts"]
    seen: dict[str, bool] = {}

    def look(_call):
        seen.update({name: getattr(service, name).running for name in workers})
        seen["hub-http"] = "hub-http" in {thread.name for thread in threading.enumerate()}
        return DEFAULT

    world.upstream.script(NOTIFY_STOP, look)

    assert service.stop() == 0

    assert seen == {**dict.fromkeys(workers, False), "hub-http": False}


def test_a_failing_notifystop_is_only_a_warning(make_world, caplog):
    world = make_world(official())
    world.hub.bind()
    service = world.start()
    wait_for(lambda: GET_UPDATES in operations(world))
    world.upstream.script(NOTIFY_STOP, RuntimeError("notifystop 返回了 500"))

    with caplog.at_level(logging.WARNING):
        code = service.stop()

    assert code == 0  # 照常退出
    assert operations(world).count(NOTIFY_STOP) == 1
    assert any(
        "notifystop" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records
    )
    assert read_run_info(world.paths).clean_exit is True


def test_the_legacy_profile_does_not_call_notifystop(world):
    world.hub.bind()
    service = world.start()
    wait_for(lambda: GET_UPDATES in operations(world))

    service.stop()

    assert NOTIFY_STOP not in operations(world)


def test_notifystop_is_skipped_without_credentials(make_world):
    world = make_world(official())
    service = world.start()

    service.stop()

    assert world.upstream.calls == []


# ---------------------------------------------------------------- 收消息线程还在长轮询


def test_a_poller_still_waiting_on_tencent_does_not_hold_up_the_exit(world, paths, monkeypatch):
    world.hub.bind()
    monkeypatch.setattr(app_module, "POLLER_GRACE_SECONDS", 0.2)
    gate = world.upstream.hold(GET_UPDATES)
    service = world.start()
    assert gate.entered.wait(5)  # 收消息线程正等着腾讯回话

    started = time.monotonic()
    code = service.stop()

    try:
        assert code == 0
        assert time.monotonic() - started < 5
        assert read_run_info(paths).clean_exit is True
    finally:
        gate.release()  # 腾讯终于回话了：这一批不处理，线程自己退出
