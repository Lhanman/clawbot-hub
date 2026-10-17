"""阶段 B 的端到端验收：真 uvicorn（随机端口）加进程内假上游，用 HTTP 发一条消息拿到 `sent`，
再发一条超限的拿到 `rejected`；再看几件只有真服务才看得出来的事（来源地址不被转发头改写、
请求体太大时客户端照样拿到 413、关闭时的新请求）。
"""

from __future__ import annotations

import socket

import pytest
import requests

from clawbot_hub.config import Config
from tests.support.hub import tweak
from tests.support.threads import run_in_thread

MAX_BODY = 64 * 1024


def headers(api, name="alpha"):
    return {"Authorization": f"Bearer {api.key(name)}"}


def test_one_message_over_http_is_sent(api, serve):
    url, _ = serve(api)

    response = requests.post(
        f"{url}/v1/messages", json={"text": "盘点完成\n共 3 项"}, headers=headers(api), timeout=10
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["parts"], body["parts_sent"]) == ("sent", 1, 1)
    assert api.hub.texts == ["盘点完成\n共 3 项"]


def test_a_message_over_the_limit_is_rejected(api, serve):
    url, _ = serve(api)
    api.hub.fill(10)  # 窗口里的 10 条用完了

    response = requests.post(
        f"{url}/v1/messages", json={"text": "再来一条"}, headers=headers(api), timeout=10
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["reason_code"], body["parts_sent"]) == (
        "rejected",
        "quota_exhausted",
        0,
    )
    assert api.hub.upstream.calls == []


def test_status_and_health_over_http(api, serve):
    url, _ = serve(api)

    health = requests.get(f"{url}/v1/health", timeout=10)
    status = requests.get(f"{url}/v1/status", headers=headers(api), timeout=10)
    nothing = requests.get(f"{url}/v1/nothing", timeout=10)

    assert health.json()["ok"] is True
    assert status.json()["quota"]["project"]["guaranteed"] == 6
    assert (nothing.status_code, nothing.json()["error"]["code"]) == (404, "not_found")
    assert nothing.headers["content-type"].startswith("application/json")
    assert "server" not in {name.lower() for name in nothing.headers}


def test_forwarding_headers_do_not_change_who_is_calling(make_api, serve):
    api = make_api(tweak(Config(), server={"lan": True}))
    api.hub.ready()
    url, _ = serve(api)
    forged = {**headers(api), "X-Forwarded-For": "203.0.113.5", "X-Real-IP": "203.0.113.5"}

    response = requests.get(f"{url}/v1/status", headers=forged, timeout=10)

    assert response.status_code == 200  # 连接是从本机来的，转发头不算数


def test_wrong_keys_from_this_machine_are_never_limited(api, serve):
    url, _ = serve(api)
    forged = {"X-Forwarded-For": "192.168.1.20"}

    codes = {
        requests.get(
            f"{url}/v1/status",
            headers={**forged, "Authorization": f"Bearer chub_{n:043d}"},
            timeout=10,
        ).status_code
        for n in range(15)
    }

    assert codes == {401}


@pytest.mark.parametrize("size", [MAX_BODY + 1, 1_000_000, 8_000_000])
def test_a_body_that_is_too_large_gets_its_413_even_though_the_client_keeps_sending(
    api, serve, size
):
    url, _ = serve(api)

    response = requests.post(
        f"{url}/v1/messages", data=b"x" * size, headers=headers(api), timeout=20
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "too_large"
    assert api.hub.rows() == []


def test_a_chunked_body_that_is_too_large_gets_its_413(api, serve):
    url, _ = serve(api)

    def chunks():
        for _ in range(200):
            yield b"x" * 1024

    response = requests.post(f"{url}/v1/messages", data=chunks(), headers=headers(api), timeout=20)

    assert response.status_code == 413


def raw_request(port: int, head: str, body: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(head.encode() + body)
        data = b""
        while b"\r\n\r\n" not in data or not data.endswith(b"}"):
            piece = sock.recv(4096)
            if not piece:
                break
            data += piece
        return data


def test_the_declared_length_is_not_what_decides(api, serve):
    _, port = serve(api)
    key = api.key()
    body = b'{"text": "' + b"x" * (MAX_BODY + 1000) + b'"}'
    head = (
        f"POST /v1/messages HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {key}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
    )

    reply = raw_request(port, head, body)

    assert reply.startswith(b"HTTP/1.1 413")


def test_a_send_in_progress_does_not_hold_up_a_status_request(api, serve):
    url, _ = serve(api)
    gate = api.hub.upstream.hold("send_text")
    sending = run_in_thread(
        requests.post, f"{url}/v1/messages", json={"text": "慢的"}, headers=headers(api), timeout=20
    )
    assert gate.entered.wait(5)

    status = requests.get(f"{url}/v1/status", headers=headers(api), timeout=5)

    assert status.status_code == 200
    gate.release()
    assert sending.result(10).json()["status"] == "sent"


def test_shutting_down_lets_the_send_finish_and_turns_new_requests_away(api, serve):
    url, _ = serve(api)
    gate = api.hub.upstream.hold("send_text")
    sending = run_in_thread(
        requests.post, f"{url}/v1/messages", json={"text": "慢的"}, headers=headers(api), timeout=20
    )
    assert gate.entered.wait(5)

    api.services.begin_shutdown()
    health = requests.get(f"{url}/v1/health", timeout=5)
    late = requests.post(
        f"{url}/v1/messages", json={"text": "太晚了"}, headers=headers(api), timeout=5
    )

    assert (health.status_code, late.status_code) == (503, 503)
    assert health.json()["error"]["code"] == "shutting_down"
    gate.release()
    result = sending.result(10)
    assert (result.status_code, result.json()["status"]) == (200, "sent")
    assert api.hub.texts == ["慢的"]
