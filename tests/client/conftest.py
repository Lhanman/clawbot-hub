"""单文件客户端（任务 D4）测试共用的东西：一个能随意编排回复的假共用服务（真 HTTP，本机回环）。

客户端是 `client/clawbot_hub_client.py`，`pyproject.toml` 把 `client` 放进了 `pythonpath`，
所以测试里直接 `import clawbot_hub_client`。真服务加这个客户端的端到端在 D5 的 `tests/contract/`。
"""

from __future__ import annotations

import clawbot_hub_client as hub_client
import pytest

from tests.support.fakehttp import FakeHttp, RecordedRequest, Reply

KEY = "chub_" + "k" * 43
OTHER_KEY = "chub_" + "o" * 43


def result_body(status: str = "sent", **overrides) -> dict:
    """服务返回的 200 内容（固定的那几个字段）。"""
    body = {
        "status": status,
        "reason_code": None,
        "reason": None,
        "parts": 1,
        "parts_sent": 1 if status in ("sent", "partial") else 0,
        "duplicate": False,
        "id": "o_2f9c1a7e",
        "quota": {"left": 8, "limit": 10, "window_ends_at": "2026-10-11T18:20:00+08:00"},
    }
    body.update(overrides)
    return body


def error_body(code: str, message: str = "出错了") -> dict:
    return {"error": {"code": code, "message": message}}


def inbound(item_id: str = "in_1", text: str = "你好", **overrides) -> dict:
    """取话返回的一项。"""
    item = {
        "id": item_id,
        "text": text,
        "has_text": True,
        "at": "2026-10-12T10:00:00+08:00",
        "at_ms": 1_791_770_400_000,
        "received_at": "2026-10-12T10:00:01+08:00",
        "attempt": 1,
        "via": "route",
        "reply_handle": "h_" + item_id,
    }
    item.update(overrides)
    return item


def inbox_body(*items: dict) -> dict:
    return {"items": list(items), "lease_seconds": 60}


def status_body(send_timeout: float = 30, online: float = 90) -> dict:
    return {
        "service": {
            "version": "0.1.0",
            "api": "1",
            "send_timeout_seconds": send_timeout,
            "inbox_online_seconds": online,
        },
        "login": {"state": "ok"},
        "window": {"open": True, "started_at": None, "ends_at": None, "seconds_left": 3600},
        "quota": {"limit": 10, "used": 2, "free": 8, "project": {"left": 4}},
    }


def is_heartbeat(request: RecordedRequest) -> bool:
    return request.query.get("wait") == "0" and request.query.get("max") == "0"


class FakeHub(FakeHttp):
    """扮演共用服务的 `/v1` 接口。默认：发送回 `sent`，取话回空，确认全部算数，状态和探活正常。
    别的回复用 `hub.script("messages", …)` 排。"""

    endpoints = ("messages", "replies", "inbox", "ack", "status", "health")

    ROUTES = {
        "/v1/messages": "messages",
        "/v1/replies": "replies",
        "/v1/inbox": "inbox",
        "/v1/inbox/ack": "ack",
        "/v1/status": "status",
        "/v1/health": "health",
    }

    def route(self, path: str) -> str | None:
        return self.ROUTES.get(path)

    def default(self, endpoint: str, request: RecordedRequest) -> Reply:
        if endpoint in ("messages", "replies"):
            return Reply(body=result_body())
        if endpoint == "inbox":
            return Reply(body=inbox_body())
        if endpoint == "ack":
            return Reply(body={"acked": len(request.json["ids"]), "unknown": []})
        if endpoint == "status":
            return Reply(body=status_body())
        return Reply(body={"ok": True, "version": "0.1.0", "api": "1"})

    def heartbeats(self) -> list[RecordedRequest]:
        """取话请求里 `wait=0&max=0` 的那些（客户端发的心跳）。"""
        return [r for r in self.requests_to("inbox") if is_heartbeat(r)]

    def fetches(self) -> list[RecordedRequest]:
        """真正取话的请求（不含心跳）。"""
        return [r for r in self.requests_to("inbox") if not is_heartbeat(r)]


@pytest.fixture
def hub():
    with FakeHub() as fake:
        yield fake


@pytest.fixture
def client(hub) -> hub_client.HubClient:
    return hub_client.HubClient(hub.base_url, KEY)


@pytest.fixture
def pauses(monkeypatch) -> list[float]:
    """客户端重试之间的停顿换成只记录、不真等（`run_inbox` 的停顿走它自己的 `sleep` 参数）。"""
    waited: list[float] = []
    monkeypatch.setattr(hub_client, "_sleep", waited.append)
    return waited


@pytest.fixture
def quick(monkeypatch):
    """把读超时里"服务的发送总时限 + 余量"缩到 0，让"读超时"的测试能在零点几秒里做完。"""
    monkeypatch.setattr(hub_client, "DEFAULT_SEND_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(hub_client, "SEND_TIMEOUT_MARGIN", 0.0)
    monkeypatch.setattr(hub_client, "INBOX_TIMEOUT_MARGIN", 0.0)
