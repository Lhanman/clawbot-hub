"""所有非 200 的响应都是同一个样子：`{"error": {"code": …, "message": …}}`。规格：`hub-send`
「错误格式和 HTTP 状态码」，`hub-deploy`「谁能连」「健康检查和版本」「正常关闭」。

只有局域网规则拒绝的 403 例外（正文为空，见 `test_auth_limits.py`）。每种状态码的"什么时候出现"
在各自的测试里；这里看格式：英文代码、中文说明、说明里没有请求的内容、该带的头带了。"""

from __future__ import annotations

import logging
import re
import sqlite3

import pytest
from pydantic import BaseModel

from clawbot_hub.config import Config
from clawbot_hub.upstream.base import Accepted
from tests.api.conftest import LAN, OUTSIDE
from tests.sender.conftest import long_text
from tests.support.hub import tweak
from tests.support.threads import run_in_thread

MAX_BODY = 64 * 1024
MARK = "SECRET-MARK-4711"
CJK = re.compile("[一-鿿]")


# ---------------------------------------------------------------- 每一种错误各造一个


def bad_json(api):
    return api.post("/v1/messages", content=b"not json")


def empty_text(api):
    return api.send("  ")


def text_too_long(api):
    return api.send("字" * 8001)


def unauthorized(api):
    return api.get("/v1/status", as_=None)


def project_disabled(api):
    api.hub.projects.disable("alpha")
    return api.get("/v1/status")


def not_found(api):
    return api.get("/v1/no-such-thing")


def trailing_slash(api):
    return api.get("/v1/status/")


def wrong_method(api):
    return api.get("/v1/messages")


def too_large(api):
    return api.post("/v1/messages", content=b"x" * (MAX_BODY + 1))


def rate_limited(api):
    for _ in range(30):
        api.send("")
    return api.send("你好")


def too_many_failures(api):
    for number in range(10):
        api.request("GET", "/v1/status", key=f"chub_{number:043d}", source=LAN)
    return api.get("/v1/status", source=LAN)


def internal(api):
    def boom(*args, **kwargs):
        raise RuntimeError("坏了")

    api.services.sender.push = boom
    return api.send("你好")


def shutting_down(api):
    api.services.begin_shutdown()
    return api.get("/v1/health", as_=None)


ERRORS = {
    "bad_request": (bad_json, 400),
    "empty_text": (empty_text, 400),
    "text_too_long": (text_too_long, 400),
    "unauthorized": (unauthorized, 401),
    "project_disabled": (project_disabled, 403),
    "not_found": (not_found, 404),
    "not_found/": (trailing_slash, 404),
    "method_not_allowed": (wrong_method, 405),
    "too_large": (too_large, 413),
    "rate_limited": (rate_limited, 429),
    "too_many_failures": (too_many_failures, 429),
    "internal": (internal, 500),
    "shutting_down": (shutting_down, 503),
}


@pytest.mark.parametrize("name", list(ERRORS))
def test_every_error_has_the_same_shape(api, name):
    make, status = ERRORS[name]

    response = make(api)

    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert list(body) == ["error"]
    assert list(body["error"]) == ["code", "message"]
    assert body["error"]["code"] == name.rstrip("/")
    assert CJK.search(body["error"]["message"])  # 说明是中文
    assert body["error"]["message"].strip()


@pytest.mark.parametrize("name", ["rate_limited", "too_many_failures"])
def test_a_429_says_how_long_to_wait_in_whole_seconds(api, name):
    response = ERRORS[name][0](api)

    assert re.fullmatch(r"[1-9][0-9]*", response.headers["Retry-After"])


@pytest.mark.parametrize(
    "name", [n for n in ERRORS if n not in ("rate_limited", "too_many_failures")]
)
def test_only_a_429_has_a_retry_after(api, name):
    assert "Retry-After" not in ERRORS[name][0](api).headers


def test_the_codes_the_error_table_promises_are_the_ones_covered():
    assert set(code.rstrip("/") for code in ERRORS) == {
        "bad_request",
        "empty_text",
        "text_too_long",
        "unauthorized",
        "project_disabled",
        "not_found",
        "method_not_allowed",
        "too_large",
        "rate_limited",
        "too_many_failures",
        "internal",
        "shutting_down",
    }


# ---------------------------------------------------------------- 说明里没有请求的内容


def leaky_requests(api):
    """每个请求都带着 `MARK`，结果都是错误。"""
    key = api.key()
    api.hub.add("beta", guaranteed=1, cap=2)
    return {
        "not json": api.post("/v1/messages", content=f"{MARK} 不是 JSON".encode()),
        "text is a number": api.post("/v1/messages", json={"text": 5, "other": MARK}),
        "bad priority": api.send("你好", priority=MARK),
        "bad on_closed": api.send("你好", on_closed=MARK),
        "bad dedupe_key": api.send("你好", dedupe_key=MARK * 20),
        "array": api.post("/v1/messages", json=[MARK]),
        "path": api.get(f"/v1/{MARK}"),
        "path under messages": api.post(f"/v1/messages/{MARK}", json={"text": MARK}),
        "query": api.get(f"/v1/{MARK}?x={MARK}"),
        "bad key": api.request("GET", "/v1/status", key=f"chub_{MARK}"),
        "other scheme": api.request(
            "GET", "/v1/status", headers={"Authorization": f"Basic {MARK}"}
        ),
        "header": api.get(f"/v1/{MARK}", headers={"X-Marker": MARK}),
        "wrong method": api.request("DELETE", "/v1/messages", key=key, content=MARK.encode()),
        "too large": api.post("/v1/messages", content=MARK.encode() + b"x" * MAX_BODY),
        "unknown key near the real one": api.request("GET", "/v1/status", key=key[:-1] + "!"),
    }


def test_no_error_repeats_what_the_caller_sent(api):
    responses = leaky_requests(api)

    for name, response in responses.items():
        assert response.status_code >= 400, name
        assert MARK not in response.text, name
        assert all(MARK not in value for value in response.headers.values()), name
        assert api.key() not in response.text, name


def test_an_error_never_contains_a_key(api):
    seen = [api.request("GET", "/v1/status", key=api.key()[:-1] + "!")]
    seen.append(api.request("GET", "/v1/status", key=api.key().replace("chub_", "chux_")))

    assert all(api.key()[6:20] not in r.text for r in seen)


# ---------------------------------------------------------------- 找不到、用错方法


NO_SUCH_PATHS = [
    "/",
    "/v1",
    "/v1/",
    "/v1/nope",
    "/v1/messages/1",
    "/v1/inbox/",
    "/v1/inbox/nope",
    "/v1/inbox/ack/",
    "/v1/inbox/ack/1",
    "/v1/replies/",
    "/v1/replies/1",
    "/v2/status",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
    "/openapi.json",
    "/favicon.ico",
    "/v1/status/",
    "/v1/health/",
    "//v1/status",
]


@pytest.mark.parametrize("path", NO_SUCH_PATHS)
@pytest.mark.parametrize("with_key", [True, False], ids=["with-key", "no-key"])
def test_an_unknown_path_is_404_whether_or_not_there_is_a_key(api, path, with_key):
    response = api.get(path, as_="alpha" if with_key else None)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert "Location" not in response.headers  # 不重定向


def test_a_trailing_slash_is_not_forgiven_and_does_not_redirect(api):
    slashed = [
        ("POST", "/v1/messages/"),
        ("GET", "/v1/status/"),
        ("GET", "/v1/health/"),
        ("GET", "/v1/inbox/"),
        ("POST", "/v1/inbox/ack/"),
        ("POST", "/v1/replies/"),
    ]
    for method, path in slashed:
        response = api.request(method, path, key=api.key(), json={"text": "你好"})

        assert response.status_code == 404, path
        assert "Location" not in response.headers
    assert api.hub.upstream.calls == []


def test_the_documentation_pages_are_not_there(api):
    for path in ("/docs", "/redoc", "/openapi.json"):
        response = api.get(path, as_=None)

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


def test_the_description_can_still_be_exported_from_the_app_object(api):
    paths = api.app.openapi()["paths"]

    assert {
        "/v1/messages",
        "/v1/status",
        "/v1/health",
        "/v1/inbox",
        "/v1/inbox/ack",
        "/v1/replies",
    } <= set(paths)
    assert not any(path.startswith(("/docs", "/redoc", "/openapi")) for path in paths)


def test_the_exported_description_says_what_the_inbox_and_reply_endpoints_take(api):
    paths = api.app.openapi()["paths"]

    inbox = {p["name"]: p for p in paths["/v1/inbox"]["get"]["parameters"]}
    assert set(inbox) == {"wait", "max"}
    assert inbox["max"]["schema"]["default"] == 10 and inbox["wait"]["required"] is False
    ack = paths["/v1/inbox/ack"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert ack["required"] == ["ids"] and ack["properties"]["ids"]["maxItems"] == 50
    reply = paths["/v1/replies"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert reply["required"] == ["handle", "text"]
    assert set(reply["properties"]) == {
        "handle",
        "text",
        "awaiting_confirmation",
        "confirm_seconds",
    }
    assert reply["properties"]["confirm_seconds"]["maximum"] == 1800


@pytest.mark.parametrize("with_key", [True, False], ids=["with-key", "no-key"])
def test_the_wrong_method_is_405_with_the_allowed_one(api, with_key):
    as_ = "alpha" if with_key else None

    get_messages = api.get("/v1/messages", as_=as_)
    post_status = api.post("/v1/status", as_=as_, json={})
    post_health = api.post("/v1/health", as_=as_, json={})

    assert [r.status_code for r in (get_messages, post_status, post_health)] == [405] * 3
    assert get_messages.headers["Allow"] == "POST"
    assert post_status.headers["Allow"] == "GET"
    assert post_health.headers["Allow"] == "GET"
    for response in (get_messages, post_status, post_health):
        assert response.json()["error"]["code"] == "method_not_allowed"


def test_the_wrong_method_on_the_inbox_and_reply_endpoints_is_405_with_the_allowed_one(api):
    post_inbox = api.post("/v1/inbox", json={})
    get_ack = api.get("/v1/inbox/ack")
    get_replies = api.get("/v1/replies")

    assert [r.status_code for r in (post_inbox, get_ack, get_replies)] == [405] * 3
    assert post_inbox.headers["Allow"] == "GET"
    assert get_ack.headers["Allow"] == "POST"
    assert get_replies.headers["Allow"] == "POST"
    for response in (post_inbox, get_ack, get_replies):
        assert response.json()["error"]["code"] == "method_not_allowed"


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
def test_other_methods_are_405_too(api, method):
    response = api.request(method, "/v1/messages", key=api.key())

    assert response.status_code == 405
    assert response.headers["Allow"] == "POST"
    if method != "HEAD":
        assert response.json()["error"]["code"] == "method_not_allowed"
    assert api.hub.upstream.calls == []


# ---------------------------------------------------------------- 框架自带的校验失败（422）改成 400


class Probe(BaseModel):
    number: int


def number_probe(number: int):  # 查询串里的整数
    return {"number": number}


def body_probe(probe: Probe):  # 请求体里的整数
    return {"number": probe.number}


@pytest.fixture
def probed(api):
    """挂两个自带校验的测试接口（以后取话接口的 `wait` / `max` 这类参数就是这么校验的）。"""
    api.app.add_api_route("/v1/_probe", number_probe, methods=["GET"])
    api.app.add_api_route("/v1/_probe_body", body_probe, methods=["POST"])
    return api


def test_the_frameworks_own_422_is_a_400_bad_request(probed):
    query = probed.get("/v1/_probe?number=abc", as_=None)
    missing = probed.get("/v1/_probe", as_=None)
    body = probed.post("/v1/_probe_body", as_=None, json={"number": "abc"})

    for response in (query, missing, body):
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_request"
        assert list(response.json()["error"]) == ["code", "message"]
    assert probed.get("/v1/_probe?number=7", as_=None).json() == {"number": 7}


def test_the_422_message_does_not_repeat_the_input(probed):
    query = probed.get(f"/v1/_probe?number={MARK}", as_=None)
    body = probed.post("/v1/_probe_body", as_=None, json={"number": MARK, MARK: MARK})
    not_json = probed.post("/v1/_probe_body", as_=None, content=MARK.encode())

    for response in (query, body, not_json):
        assert response.status_code == 400
        assert MARK not in response.text


# ---------------------------------------------------------------- 内部出错


def test_an_unexpected_error_is_a_500_internal_that_says_nothing_about_it(api, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError(MARK)

    monkeypatch.setattr(api.services.sender, "push", boom)

    response = api.send(f"正文 {MARK}")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal"
    assert MARK not in response.text


def test_the_log_has_the_kind_of_error_and_the_stack_but_not_the_message_or_the_text(
    api, monkeypatch, caplog
):
    def boom(*args, **kwargs):
        raise RuntimeError(MARK)  # 堆栈里会有这一行的源码，但不会有 MARK 的值

    monkeypatch.setattr(api.services.sender, "push", boom)
    caplog.set_level(logging.DEBUG)

    api.send(f"正文 {MARK}")

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "RuntimeError" in logged
    assert "boom" in logged  # 堆栈里有出事的函数
    assert MARK not in logged
    assert all(record.exc_text is None and not record.exc_info for record in caplog.records)


def test_an_error_before_the_send_is_a_500_too(api, monkeypatch):
    def locked(key):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(api.services.projects, "authenticate", locked)

    response = api.get("/v1/status")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal"
    assert "locked" not in response.text


def test_the_service_carries_on_after_a_500(api, monkeypatch):
    with monkeypatch.context() as patched:
        patched.setattr(api.services.sender, "push", lambda *a, **k: 1 / 0)
        assert api.send("你好").status_code == 500

    assert api.send("你好").json()["status"] == "sent"


def test_a_failure_halfway_keeps_what_went_out_and_gives_the_rest_back(api):
    def boom(call):
        raise RuntimeError("坏了")

    api.hub.upstream.script("send_text", Accepted("m-1"), boom)

    response = api.send(long_text(3), dedupe_key="same")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal"
    (row,) = api.hub.rows()
    assert row["status"] == "interrupted"  # 碰过腾讯：不知道发出了几条，占着去重键
    assert api.hub.used() == {("alpha", "push"): 2}  # 发出的 1 条和卡住的那 1 条算已用，第 3 条退回
    again = api.send(long_text(3), dedupe_key="same").json()
    assert (again["status"], again["duplicate"]) == ("unconfirmed", True)
    assert api.send("下一条").json()["status"] == "sent"


# ---------------------------------------------------------------- 正在关闭


def test_while_shutting_down_every_new_request_is_a_503(api):
    api.services.begin_shutdown()

    answers = [
        api.get("/v1/health", as_=None),
        api.get("/v1/status"),
        api.get("/v1/status", as_=None),
        api.send("你好"),
        api.get("/no/such/path", as_=None),
        api.request("DELETE", "/v1/messages"),
    ]

    assert [r.status_code for r in answers] == [503] * 6
    assert {r.json()["error"]["code"] for r in answers} == {"shutting_down"}
    assert api.hub.upstream.calls == []
    assert api.hub.rows() == []


def test_the_local_rule_still_comes_first_while_shutting_down(make_api):
    api = make_api(tweak(Config(), server={"lan": True}))
    api.hub.ready()
    api.services.begin_shutdown()

    stranger = api.get("/v1/health", as_=None, source=OUTSIDE)
    neighbour = api.get("/v1/health", as_=None, source=LAN)

    assert (stranger.status_code, stranger.content) == (403, b"")
    assert neighbour.status_code == 503


def test_the_send_under_way_finishes_and_a_request_waiting_for_the_lock_is_let_go(api):
    api.hub.add("beta", guaranteed=1, cap=2)
    gate = api.hub.upstream.hold("send_text")
    running = run_in_thread(api.send, "先到的")
    assert gate.entered.wait(5)
    waiting = run_in_thread(api.send, "后到的", as_="beta")
    api.hub.clock.wait_until_blocked()  # 它在等发送锁

    api.services.begin_shutdown()

    let_go = waiting.result().json()
    assert (let_go["status"], let_go["reason_code"], let_go["parts_sent"]) == ("failed", "busy", 0)
    assert running.still_blocked()  # 正在发的那条还在发
    gate.release()
    assert running.result().json()["status"] == "sent"
    assert api.hub.used() == {("alpha", "push"): 1}  # 等锁的那条没占条数
