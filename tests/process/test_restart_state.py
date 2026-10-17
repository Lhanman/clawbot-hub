"""重启后的状态（任务 D6，规格 `hub-deploy`「重启后的状态」）。

服务重启（正常或被杀）以后：窗口和用量、发送记录、去重摘要、待取队列和租约、等确认标记、告警状态、
服务回复的限频时间、项目设置、每个项目最近一次取话的时间 MUST 保持（都在数据库里）；每个项目的请求
限速计数、认证失败计数 MUST 清零（只在内存里）。每个测试都是"起服务、做些事、停服务、再起一个"。
"""

from __future__ import annotations

from clawbot_hub.bark import Delivered, Unreachable
from clawbot_hub.config import Config
from tests.process.conftest import call
from tests.support.hub import tweak
from tests.support.threads import wait_for


def snapshot(world) -> dict:
    return {
        "windows": world.rows("SELECT id, started_ms, context_token, voided FROM windows"),
        "usage": world.rows("SELECT window_id, project_id, kind, n FROM usage ORDER BY 1, 2, 3"),
        "inbox": world.rows(
            "SELECT id, state, attempts, handle, lease_until_ms, replies FROM inbox ORDER BY rowid"
        ),
        "awaiting": world.rows("SELECT name, awaiting_until_ms FROM projects WHERE name = 'alpha'"),
        "projects": world.rows(
            "SELECT name, prefix, guaranteed, cap, is_default, enabled FROM projects"
            " WHERE internal = 0 ORDER BY id"
        ),
    }


def restart(world, service, **start_arguments):
    assert service.stop() == 0
    return world.start(**start_arguments)


def test_window_usage_lease_and_awaiting_mark_survive_a_restart(world):
    world.hub.ready("alpha")
    key = world.key()
    service = world.start()
    assert call(service, "GET", "/v1/inbox", key=key, params={"wait": 0}).status_code == 200  # 在线
    world.upstream.push_user_message("持仓")  # 使用者说了话，开了新窗口
    wait_for(lambda: world.rows("SELECT id FROM inbox WHERE state = 'queued'"))
    for text in ("一", "二", "三"):
        sent = call(service, "POST", "/v1/messages", key=key, json={"text": text})
        assert sent.json()["status"] == "sent"
    [item] = call(service, "GET", "/v1/inbox", key=key, params={"wait": 0}).json()["items"]
    reply = {"handle": item["reply_handle"], "text": "要平仓吗？回对确认"}
    reply.update(awaiting_confirmation=True, confirm_seconds=300)
    assert call(service, "POST", "/v1/replies", key=key, json=reply).json()["status"] == "sent"
    before = snapshot(world)
    assert sum(row["n"] for row in before["usage"]) == 4  # 窗口已用 4 条
    assert before["awaiting"][0]["awaiting_until_ms"] is not None
    assert [row["state"] for row in before["inbox"]] == ["leased"]  # 租出去了，没确认

    service = restart(world, service)

    assert snapshot(world) == before  # 窗口起点、已用条数、租约、等确认标记都原样
    world.clock.advance(61)  # 租期过了
    [again] = call(service, "GET", "/v1/inbox", key=key, params={"wait": 0}).json()["items"]
    assert (again["id"], again["reply_handle"]) == (item["id"], item["reply_handle"])
    assert again["attempt"] == 2  # 再次交出，句柄不变


def test_a_dedupe_key_still_answers_with_the_original_result_after_a_restart(world):
    world.hub.ready("alpha")
    key = world.key()
    service = world.start()
    body = {"text": "只发一次", "dedupe_key": "report-2026-10-16"}
    first = call(service, "POST", "/v1/messages", key=key, json=body).json()
    assert first["status"] == "sent"

    service = restart(world, service)
    again = call(service, "POST", "/v1/messages", key=key, json=body).json()

    assert again["status"] == "sent"
    assert again["id"] == first["id"]  # 命中去重，返回原来那条的结果
    assert [m.text for m in world.upstream.sent] == ["只发一次"]  # 没有再发一次


def test_a_service_notice_is_not_repeated_after_a_restart(world):
    world.hub.ready("alpha")  # alpha 是默认项目，但没人去取话：不在线
    service = world.start()
    world.upstream.push_user_message("持仓")
    wait_for(lambda: len(world.upstream.sent) == 1)
    assert "没在线" in world.upstream.sent[0].text or "alpha" in world.upstream.sent[0].text

    service = restart(world, service)
    world.upstream.push_user_message("再问一次")
    wait_for(lambda: len(world.rows("SELECT id FROM inbox")) == 2)
    world.clock.advance(60)

    assert len(world.upstream.sent) == 1  # 5 分钟内同一个目标只提醒一次，重启不重置


def test_a_project_stays_online_for_90_seconds_after_a_restart(world):
    world.hub.ready("alpha")
    service = world.start()
    assert call(service, "GET", "/v1/inbox", key=world.key(), params={"wait": 0}).status_code == 200

    service = restart(world, service)
    projects = service.services.projects
    inbox = service.services.inbox
    world.clock.advance(60)
    assert inbox.online(projects.get("alpha")) is True  # 最近一次取话还不到 90 秒
    world.clock.advance(31)

    assert inbox.online(projects.get("alpha")) is False


def test_project_settings_survive_a_restart(world):
    world.hub.ready("alpha")
    world.hub.add("beta", guaranteed=2, cap=4, prefix="贝塔")
    service = world.start()
    before = snapshot(world)["projects"]

    restart(world, service)

    assert snapshot(world)["projects"] == before


def test_the_counters_in_memory_start_from_zero_after_a_restart(make_world):
    world = make_world(tweak(Config(), send={"rate_per_minute": 2}))
    world.hub.ready("alpha")
    key = world.key()
    service = world.start()
    for _ in range(2):
        assert (
            call(service, "POST", "/v1/messages", key=key, json={"text": "好"}).status_code == 200
        )
    assert call(service, "POST", "/v1/messages", key=key, json={"text": "好"}).status_code == 429
    for _ in range(10):
        service.services.limits.failures.add("203.0.113.5")
    assert service.services.limits.failures.retry_after("203.0.113.5") is not None

    service = restart(world, service)

    assert service.services.limits.failures.retry_after("203.0.113.5") is None  # 失败计数清零
    assert call(service, "POST", "/v1/messages", key=key, json={"text": "好"}).status_code == 200


def test_a_pending_alert_survives_a_restart_and_is_sent_once(world):
    def pending() -> str | None:
        [row] = world.rows("SELECT pending_text FROM alert_state WHERE key = 'service_restarted'")
        return row["pending_text"]

    world.hub.ready("alpha")
    world.run_json()  # 上一次被杀：启动时登记一条"服务重启"告警
    world.bark.default = Unreachable("连不上")  # Bark 一直不通
    service = world.start()
    wait_for(lambda: len(world.bark.calls) >= 1)
    assert pending()  # 没送到，留在待发队列里

    world.bark.default = Delivered()  # 之后 Bark 好了
    tried = len(world.bark.calls)
    service = restart(world, service)  # 正常重启：待发队列在库里，重启不丢

    wait_for(lambda: pending() is None)  # 重启后的第一轮就送出去了
    delivered = len(world.bark.calls)
    assert delivered > tried
    service = restart(world, service)
    world.clock.advance(3600)
    assert len(world.bark.calls) == delivered  # 发过了就不再发
    service.stop()
