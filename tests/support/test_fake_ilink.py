"""假腾讯服务自己的测试（任务 0.4）：假件按剧本给出每一种结果，免得假件写错而上层测试全绿。

全部走真实的 HTTP（本机回环），所以也顺带验证了 `requests` 在各种断连下抛的是什么。
"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import requests

from tests.support import fake_ilink as fi
from tests.support.fake_ilink import FakeIlink
from tests.support.threads import run_in_thread

REPO_ROOT = Path(__file__).resolve().parents[2]
BOT = "/ilink/bot/"


@pytest.fixture
def fake():
    with FakeIlink(longpoll_seconds=0.2) as server:
        yield server


def post(fake: FakeIlink, endpoint: str, body=None, *, timeout: float = 5, **kwargs):
    return requests.post(f"{fake.base_url}{BOT}{endpoint}", json=body, timeout=timeout, **kwargs)


def send_message(fake: FakeIlink, text: str = "hi", *, client_id: str = "c1", token="ctx-1", **kw):
    msg = {
        "from_user_id": "",
        "to_user_id": fake.user_id,
        "client_id": client_id,
        "message_type": 2,
        "message_state": 2,
        "context_token": token,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }
    return post(fake, "sendmessage", {"msg": msg, "base_info": {"channel_version": "1.0.2"}}, **kw)


def poll(fake: FakeIlink, buf: str = "", **kw):
    return post(fake, "getupdates", {"get_updates_buf": buf, "base_info": {}}, **kw).json()


def raw_request(fake: FakeIlink, path: str = "sendmessage", body: bytes = b"{}") -> socket.socket:
    """用裸套接字发一个请求，不读响应：用来看服务端怎么收尾。"""
    sock = socket.create_connection((fake.host, fake.port), timeout=3)
    head = (
        f"POST {BOT}{path} HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    )
    sock.sendall(head.encode() + body)
    return sock


# ---------------------------------------------------------------- 生命周期


def test_start_gives_a_loopback_url_and_stop_closes_the_port():
    server = FakeIlink().start()
    url = server.base_url
    assert url.startswith("http://127.0.0.1:")
    assert requests.post(f"{url}{BOT}notifystart", json={}, timeout=3).status_code == 200
    server.stop()
    server.stop()  # 再停一次没关系
    with pytest.raises(requests.ConnectionError):
        requests.post(f"{url}{BOT}notifystart", json={}, timeout=1)


def test_unknown_paths_are_404_and_not_recorded(fake):
    assert requests.get(f"{fake.base_url}/nothing", timeout=3).status_code == 404
    assert requests.get(f"{fake.base_url}{BOT}nothing", timeout=3).status_code == 404
    assert fake.requests == []


# ---------------------------------------------------------------- 记录


def test_requests_are_recorded_with_method_query_headers_and_json_body(fake):
    requests.get(
        f"{fake.base_url}{BOT}get_bot_qrcode?bot_type=3&x=1&x=2",
        headers={"X-Probe": "1"},
        timeout=3,
    )
    post(fake, "sendmessage", {"msg": {}}, headers={"Authorization": "Bearer t0k"})
    first, second = fake.requests
    assert (first.index, first.endpoint, first.method) == (1, "get_bot_qrcode", "GET")
    assert first.query == {"bot_type": "3", "x": "1"}  # 同名的取第一个
    assert first.header("x-probe") == "1"
    assert first.body == b"" and first.json is None
    assert (second.index, second.endpoint, second.method) == (2, "sendmessage", "POST")
    assert second.json == {"msg": {}}
    assert second.header("AUTHORIZATION") == "Bearer t0k"


def test_custom_header_names_leave_out_the_transport_headers(fake):
    post(fake, "sendmessage", {"msg": {}}, headers={"AuthorizationType": "ilink_bot_token"})
    names = {n.lower() for n in fake.requests[0].custom_header_names()}
    assert names == {"content-type", "authorizationtype"}


def test_connection_id_tells_new_connections_from_reused_ones(fake):
    with requests.Session() as session:
        for _ in range(2):
            session.post(f"{fake.base_url}{BOT}notifystart", json={}, timeout=3)
    post(fake, "notifystart", {})  # 另一条连接
    ids = [r.connection_id for r in fake.requests]
    assert ids[0] == ids[1] != ids[2]


def test_wait_for_requests_returns_when_enough_arrive_and_gives_up_otherwise(fake):
    worker = run_in_thread(fake.wait_for_requests, "sendmessage", 2)
    send_message(fake)
    assert worker.still_blocked()
    send_message(fake)
    worker.result()
    with pytest.raises(AssertionError, match="只收到 2 个"):
        fake.wait_for_requests("sendmessage", 3, timeout=0.1)


# ---------------------------------------------------------------- sendmessage


def test_sendmessage_default_is_success_with_an_increasing_message_id(fake):
    first = send_message(fake).json()
    second = send_message(fake).json()
    assert first["ret"] == 0 and first["errmsg"] == ""
    assert isinstance(first["message_id"], int) and first["message_id"] > 0
    assert second["message_id"] > first["message_id"]


def test_message_id_can_be_left_out_or_renamed():
    with FakeIlink(message_ids=False) as plain:
        assert send_message(plain).json() == {"ret": 0, "errmsg": ""}
    with FakeIlink(message_id_key="msg_id") as renamed:
        body = send_message(renamed).json()
        assert "msg_id" in body and "message_id" not in body


def test_sent_messages_are_parsed_out_of_the_requests(fake):
    msg = {
        "to_user_id": "u1",
        "client_id": "abc",
        "context_token": "ctx-9",
        "item_list": [
            {"type": 1, "text_item": {"text": "你好"}},
            {"type": 2, "image_item": {}},
            {"type": 1, "text_item": {"text": "，世界"}},
        ],
    }
    post(fake, "sendmessage", {"msg": msg})
    (sent,) = fake.sent
    assert (sent.index, sent.to_user_id, sent.client_id, sent.context_token) == (
        1,
        "u1",
        "abc",
        "ctx-9",
    )
    assert sent.text == "你好，世界"
    assert sent.msg == msg
    assert sent.request_index == fake.requests[0].index
    assert fake.sent_texts() == ["你好，世界"]


def test_sendmessage_with_a_broken_body_is_recorded_without_crashing(fake):
    response = requests.post(f"{fake.base_url}{BOT}sendmessage", data=b"not json", timeout=3)
    assert response.status_code == 200
    assert fake.requests[0].json is None
    assert fake.sent[0].text == ""


def test_clear_requests_forgets_requests_and_sent(fake):
    send_message(fake)
    fake.clear_requests()
    assert fake.requests == [] and fake.sent == []


# ---------------------------------------------------------------- 编排：回复的种类


def test_scripted_replies_are_used_one_per_request_then_the_default_returns(fake):
    fake.script("sendmessage", fi.session_expired(), fi.ok())
    assert send_message(fake).json()["ret"] == -14
    plain = send_message(fake).json()
    assert plain == {"ret": 0, "errmsg": ""}  # ok() 不带消息编号
    assert "message_id" in send_message(fake).json()  # 排完了，回到默认


def test_scripts_are_per_endpoint(fake):
    fake.script("getupdates", fi.http_status(503))
    assert send_message(fake).status_code == 200
    assert post(fake, "getupdates", {"get_updates_buf": ""}).status_code == 503


@pytest.mark.parametrize(
    ("reply", "status", "expect"),
    [
        (fi.ok(), 200, {"ret": 0, "errmsg": ""}),
        (fi.ok(message_id=5), 200, {"ret": 0, "errmsg": "", "message_id": 5}),
        (fi.error(40001, "bad token"), 200, {"ret": 40001, "errmsg": "bad token"}),
        (fi.error(5, "x", key="errcode"), 200, {"errcode": 5, "errmsg": "x"}),
        (fi.session_expired(), 200, {"ret": -14, "errmsg": "session expired"}),
        (
            fi.session_expired(status=401, key="errcode"),
            401,
            {"errcode": -14, "errmsg": "session expired"},
        ),
        (fi.session_expired(status=502), 502, {"ret": -14, "errmsg": "session expired"}),
    ],
)
def test_json_replies(fake, reply, status, expect):
    fake.script("sendmessage", reply)
    response = send_message(fake)
    assert response.status_code == status
    assert response.json() == expect


@pytest.mark.parametrize("status", [201, 302, 403, 429, 500, 503])
def test_http_status_replies_have_an_empty_body_unless_given(fake, status):
    fake.script("sendmessage", fi.http_status(status))
    response = send_message(fake, allow_redirects=False)
    assert response.status_code == status
    assert response.content == b""


def test_http_status_can_carry_a_body(fake):
    fake.script("sendmessage", fi.http_status(201, {"ret": 0}))
    response = send_message(fake)
    assert (response.status_code, response.json()) == (201, {"ret": 0})


def test_html_and_empty_replies_are_not_json(fake):
    fake.script("sendmessage", fi.html(), fi.empty())
    page = send_message(fake)
    assert page.status_code == 200 and "<html>" in page.text
    assert page.headers["Content-Type"].startswith("text/html")
    with pytest.raises(ValueError):
        page.json()
    blank = send_message(fake)
    assert blank.status_code == 200 and blank.content == b""


def test_extra_headers_such_as_retry_after_are_sent(fake):
    fake.script("sendmessage", fi.Reply(status=429, headers={"Retry-After": "7"}))
    assert send_message(fake).headers["Retry-After"] == "7"


def test_delay_is_real_time_before_answering(fake):
    fake.script("sendmessage", fi.ok().after(0.3))
    started = time.monotonic()
    send_message(fake)
    assert time.monotonic() - started >= 0.25


def test_default_after_waits_and_then_does_the_default(fake):
    fake.script("sendmessage", fi.default_after(0.2))
    started = time.monotonic()
    body = send_message(fake).json()
    assert time.monotonic() - started >= 0.15
    assert "message_id" in body
    assert len(fake.sent) == 1  # 默认行为照常记了一条


# ---------------------------------------------------------------- 编排：连接层的故障


def test_hang_makes_the_client_hit_its_read_timeout_and_stop_releases_the_handler():
    server = FakeIlink().start()
    server.script("sendmessage", fi.hang())
    with pytest.raises(requests.ReadTimeout):
        send_message(server, timeout=0.3)
    assert server.requests[0].endpoint == "sendmessage"  # 请求是收到了的
    started = time.monotonic()
    server.stop()
    assert time.monotonic() - started < 3  # 挂着的处理线程不拦着关服务


def test_reset_sends_a_tcp_rst_after_the_request_was_read(fake):
    fake.script("sendmessage", fi.reset())
    sock = raw_request(fake)
    with sock, pytest.raises(ConnectionResetError):
        sock.settimeout(3)
        sock.recv(100)
    assert len(fake.sent) == 0 and len(fake.requests) == 1


def test_reset_surfaces_as_a_connection_error_in_requests(fake):
    fake.script("sendmessage", fi.reset())
    with pytest.raises(requests.ConnectionError):
        send_message(fake)


def test_close_drops_the_connection_without_any_response(fake):
    fake.script("sendmessage", fi.close())
    sock = raw_request(fake)
    with sock:
        sock.settimeout(3)
        assert sock.recv(100) == b""  # 对方直接关了
    fake.script("sendmessage", fi.close())
    with pytest.raises(requests.ConnectionError):
        send_message(fake)


def test_truncated_response_breaks_in_the_middle_of_the_body(fake):
    fake.script("sendmessage", fi.truncated())
    with pytest.raises(requests.exceptions.ChunkedEncodingError):
        send_message(fake)


def test_close_after_response_closes_idle_connections_silently():
    with FakeIlink(close_after_response=True) as server:
        sock = raw_request(server, "notifystart")
        with sock:
            sock.settimeout(3)
            data = b""
            while b"\r\n\r\n" not in data:
                data += sock.recv(4096)
            assert b"Connection: close" not in data  # 没有声明要关
            while True:  # 读完响应体以后，对方把连接关了
                chunk = sock.recv(4096)
                if chunk == b"":
                    break


# ---------------------------------------------------------------- 模式（/_fake/mode 用的）


@pytest.mark.parametrize(
    ("mode", "status", "business"),
    [
        ("disconnect", None, None),
        ("session_expired", 200, -14),
        ("http_5xx", 503, None),
    ],
)
def test_modes_change_the_bot_token_endpoints_but_not_login(fake, mode, status, business):
    fake.set_mode(mode)
    for endpoint in fi.BOT_TOKEN_ENDPOINTS:
        if status is None:
            with pytest.raises(requests.ConnectionError):
                post(fake, endpoint, {"get_updates_buf": ""})
        else:
            response = post(fake, endpoint, {"get_updates_buf": ""})
            assert response.status_code == status
            if business is not None:
                assert response.json()["ret"] == business
    assert (
        requests.get(f"{fake.base_url}{BOT}get_bot_qrcode?bot_type=3", timeout=3).status_code == 200
    )
    fake.set_mode("normal")
    assert send_message(fake).json()["ret"] == 0


def test_set_mode_rejects_unknown_names(fake):
    with pytest.raises(ValueError, match="不认识的模式"):
        fake.set_mode("nope")


def test_set_default_is_sticky_and_clearing_restores(fake):
    fake.set_default("sendmessage", fi.error(40001, "no"))
    assert send_message(fake).json()["ret"] == 40001
    assert send_message(fake).json()["ret"] == 40001
    fake.set_default("sendmessage", None)
    assert send_message(fake).json()["ret"] == 0


def test_scripts_win_over_the_sticky_default_and_clear_scripts_resets_everything(fake):
    fake.set_default("sendmessage", fi.error(40001, "no"))
    fake.script("sendmessage", fi.ok())
    assert send_message(fake).json()["ret"] == 0
    assert send_message(fake).json()["ret"] == 40001
    fake.script("sendmessage", fi.http_status(500))
    fake.clear_scripts()
    assert send_message(fake).json()["ret"] == 0


# ---------------------------------------------------------------- getupdates


def test_empty_inbox_long_polls_then_answers_with_no_messages(fake):
    started = time.monotonic()
    body = poll(fake)
    assert time.monotonic() - started >= 0.15  # 挂住了 longpoll_seconds（0.2）
    assert body == {"ret": 0, "msgs": [], "get_updates_buf": "buf-0"}


def test_a_new_message_wakes_a_waiting_long_poll_immediately():
    with FakeIlink(longpoll_seconds=10) as server:
        worker = run_in_thread(poll, server)
        server.wait_for_requests("getupdates")
        assert worker.still_blocked()
        started = time.monotonic()
        server.push_user_message("你好", context_token="ctx-a", create_time_ms=1234)
        body = worker.result()
        assert time.monotonic() - started < 2
        (msg,) = body["msgs"]
        assert body["get_updates_buf"] == "buf-1"
        assert msg["from_user_id"] == server.user_id
        assert msg["to_user_id"] == server.bot_id
        assert (msg["message_type"], msg["message_state"]) == (1, 2)
        assert msg["context_token"] == "ctx-a" and msg["create_time_ms"] == 1234
        assert msg["item_list"] == [{"type": 1, "text_item": {"text": "你好"}}]
        assert isinstance(msg["message_id"], int)


def test_messages_come_in_order_and_the_buf_moves_forward(fake):
    fake.push_user_message("一")
    fake.push_user_message("二")
    first = poll(fake)
    assert [m["item_list"][0]["text_item"]["text"] for m in first["msgs"]] == ["一", "二"]
    assert first["get_updates_buf"] == "buf-2"
    fake.push_user_message("三")
    second = poll(fake, first["get_updates_buf"])
    assert [m["seq"] for m in second["msgs"]] == [3]
    assert poll(fake, second["get_updates_buf"])["msgs"] == []


def test_an_old_buf_gets_the_same_batch_again_by_default(fake):
    fake.push_user_message("一")
    first = poll(fake)
    again = poll(fake)  # 带着空 buf 再来
    assert again["msgs"] == first["msgs"]


def test_without_redelivery_a_replayed_buf_gets_nothing_new():
    with FakeIlink(longpoll_seconds=0.1, redeliver=False) as server:
        server.push_user_message("一")
        assert len(poll(server)["msgs"]) == 1
        assert poll(server)["msgs"] == []


def test_a_garbled_buf_is_treated_like_an_empty_one(fake):
    fake.push_user_message("一")
    assert len(poll(fake, "some-opaque-token-from-the-real-service")["msgs"]) == 1


def test_longpoll_hint_is_included_only_when_configured():
    with FakeIlink(longpoll_seconds=0.05, longpoll_hint_ms=60000) as server:
        assert poll(server)["longpolling_timeout_ms"] == 60000
    with FakeIlink(longpoll_seconds=0.05) as server:
        assert "longpolling_timeout_ms" not in poll(server)


def test_message_shapes_for_images_other_senders_and_missing_context_tokens(fake):
    fake.push_user_message(items=[{"type": 2, "image_item": {"url": "x"}}])
    fake.push_user_message("别人", from_user_id="someone-else")
    fake.push_user_message("没有凭证", context_token=None)
    fake.push_bot_message("机器人自己的")
    msgs = poll(fake)["msgs"]
    assert msgs[0]["item_list"][0]["type"] == 2
    assert msgs[1]["from_user_id"] == "someone-else"
    assert "context_token" not in msgs[2]
    assert msgs[3]["message_type"] == 2 and msgs[3]["from_user_id"] == fake.bot_id


def test_default_context_tokens_are_unique_and_create_time_follows_the_injected_clock():
    with FakeIlink(now_ms=lambda: 42_000, longpoll_seconds=0.05) as server:
        a = server.push_user_message("a")
        b = server.push_user_message("b")
        assert a["context_token"] != b["context_token"]
        assert a["create_time_ms"] == b["create_time_ms"] == 42_000


def test_stopping_releases_a_waiting_long_poll():
    server = FakeIlink(longpoll_seconds=30).start()
    worker = run_in_thread(
        lambda: requests.post(f"{server.base_url}{BOT}getupdates", json={}, timeout=10)
    )
    server.wait_for_requests("getupdates")
    started = time.monotonic()
    server.stop()
    worker.result()
    assert time.monotonic() - started < 3


# ---------------------------------------------------------------- 扫码


def test_get_bot_qrcode_works_with_get_and_post_and_issues_distinct_codes(fake):
    first = requests.get(
        f"{fake.base_url}{BOT}get_bot_qrcode", params={"bot_type": 3}, timeout=3
    ).json()
    second = requests.post(
        f"{fake.base_url}{BOT}get_bot_qrcode",
        params={"bot_type": 3},
        json={"local_token_list": []},
        timeout=3,
    ).json()
    assert first["qrcode"] != second["qrcode"]
    assert (
        first["qrcode_img_content"].startswith("https://")
        and first["qrcode"] in first["qrcode_img_content"]
    )
    get_request, post_request = fake.requests
    assert (get_request.method, post_request.method) == ("GET", "POST")
    assert post_request.json == {"local_token_list": []}


def status(fake: FakeIlink, code: str, **params):
    return requests.get(
        f"{fake.base_url}{BOT}get_qrcode_status", params={"qrcode": code, **params}, timeout=3
    ).json()


def new_code(fake: FakeIlink) -> str:
    return requests.get(
        f"{fake.base_url}{BOT}get_bot_qrcode", params={"bot_type": 3}, timeout=3
    ).json()["qrcode"]


def test_default_qr_flow_confirms_immediately_and_hands_out_a_new_token_each_time(fake):
    first = status(fake, new_code(fake))
    assert first["status"] == "confirmed"
    assert first["bot_token"] == "fake-token-1"
    assert first["ilink_bot_id"] == fake.bot_id and first["ilink_user_id"] == fake.user_id
    assert first["baseurl"] == fake.base_url
    second = status(fake, new_code(fake))
    assert second["bot_token"] == "fake-token-2" and fake.logins == 2


def test_default_qr_flow_waits_until_confirm_after():
    with FakeIlink(confirm_after=0.3) as server:
        code = new_code(server)
        assert status(server, code) == {"status": "wait"}
        time.sleep(0.35)
        assert status(server, code)["status"] == "confirmed"


def test_a_confirmed_code_keeps_answering_with_the_same_login(fake):
    code = new_code(fake)
    assert status(fake, code) == status(fake, code)
    assert fake.logins == 1


def test_unknown_codes_are_expired(fake):
    assert status(fake, "nope") == {"status": "expired"}


def test_qr_plans_script_each_code_and_repeat_the_last_entry(fake):
    fake.qr_plan(["wait", "scaned", "expired"], ["wait", "confirmed"])
    a, b, c = new_code(fake), new_code(fake), new_code(fake)
    assert [status(fake, a)["status"] for _ in range(5)] == [
        "wait",
        "scaned",
        "expired",
        "expired",
        "expired",
    ]
    assert [status(fake, b)["status"] for _ in range(2)] == ["wait", "confirmed"]
    assert status(fake, c)["status"] == "confirmed"  # 没排到的走默认


def test_qr_plan_entries_can_carry_extra_fields_for_redirects_and_verification(fake):
    fake.qr_plan(
        [
            {"status": "need_verifycode"},
            "verify_code_blocked",
        ],
        [{"status": "scaned_but_redirect", "redirect_host": "other.example"}, "binded_redirect"],
    )
    a, b = new_code(fake), new_code(fake)
    assert status(fake, a) == {"status": "need_verifycode"}
    assert status(fake, a, verify_code="123456")["status"] == "verify_code_blocked"
    assert fake.requests_to("get_qrcode_status")[1].query["verify_code"] == "123456"
    assert status(fake, b) == {"status": "scaned_but_redirect", "redirect_host": "other.example"}
    assert status(fake, b)["status"] == "binded_redirect"


def test_a_planned_confirmed_can_override_the_token(fake):
    fake.qr_plan([{"status": "confirmed", "bot_token": "mine"}])
    body = status(fake, new_code(fake))
    assert body["bot_token"] == "mine" and body["ilink_bot_id"] == fake.bot_id


def test_a_new_login_only_sees_messages_pushed_after_it(fake):
    fake.push_user_message("旧的")
    status(fake, new_code(fake))  # 扫码确认
    fake.push_user_message("新的")
    texts = [m["item_list"][0]["text_item"]["text"] for m in poll(fake)["msgs"]]
    assert texts == ["新的"]


# ---------------------------------------------------------------- notify


@pytest.mark.parametrize("path", ["notifystart", "notifystop", "msg/notifystart", "msg/notifystop"])
def test_notify_endpoints_answer_ok_under_both_spellings_of_the_path(fake, path):
    assert post(fake, path, {"base_info": {}}).json() == {"ret": 0, "errmsg": ""}
    assert fake.requests[0].endpoint == path.removeprefix("msg/")


def test_notify_failures_can_be_scripted(fake):
    fake.script("notifystart", fi.error(500, "boom"))
    assert post(fake, "notifystart", {}).json()["ret"] == 500


# ---------------------------------------------------------------- /_fake/ 控制接口


def test_control_inbox_puts_a_user_message_into_the_next_poll(fake):
    response = requests.post(
        f"{fake.base_url}/_fake/inbox", json={"text": "状态", "context_token": "ctx-x"}, timeout=3
    )
    assert response.json() == {"ok": True, "seq": 1, "context_token": "ctx-x"}
    (msg,) = poll(fake)["msgs"]
    assert msg["item_list"][0]["text_item"]["text"] == "状态" and msg["context_token"] == "ctx-x"


def test_control_inbox_rejects_a_non_object_body(fake):
    assert requests.post(f"{fake.base_url}/_fake/inbox", data=b"nope", timeout=3).status_code == 400


def test_control_sent_lists_what_sendmessage_received_and_supports_after(fake):
    send_message(fake, "一", client_id="a")
    send_message(fake, "二", client_id="b")
    everything = requests.get(f"{fake.base_url}/_fake/sent", timeout=3).json()["sent"]
    assert [(m["index"], m["client_id"], m["text"]) for m in everything] == [
        (1, "a", "一"),
        (2, "b", "二"),
    ]
    later = requests.get(f"{fake.base_url}/_fake/sent", params={"after": 1}, timeout=3).json()[
        "sent"
    ]
    assert [m["text"] for m in later] == ["二"]


def test_control_mode_switches_and_restores_and_rejects_nonsense(fake):
    url = f"{fake.base_url}/_fake/mode"
    assert requests.post(url, json={"mode": "session_expired"}, timeout=3).json() == {
        "ok": True,
        "mode": "session_expired",
    }
    assert send_message(fake).json()["ret"] == -14
    requests.post(url, json={"mode": "normal"}, timeout=3)
    assert send_message(fake).json()["ret"] == 0
    assert requests.post(url, json={"mode": "bogus"}, timeout=3).status_code == 400
    assert requests.post(url, json={}, timeout=3).status_code == 400


def test_control_requests_are_not_recorded_as_tencent_requests(fake):
    requests.get(f"{fake.base_url}/_fake/sent", timeout=3)
    requests.post(f"{fake.base_url}/_fake/mode", json={"mode": "normal"}, timeout=3)
    assert fake.requests == []
    assert requests.get(f"{fake.base_url}/_fake/nothing", timeout=3).status_code == 404


# ---------------------------------------------------------------- 并发


def test_concurrent_sends_are_all_recorded_with_distinct_message_ids(fake):
    results: list[int] = []
    lock = threading.Lock()

    def one(i: int) -> None:
        body = send_message(fake, f"m{i}", client_id=f"c{i}").json()
        with lock:
            results.append(body["message_id"])

    workers = [run_in_thread(one, i) for i in range(20)]
    for worker in workers:
        worker.result()
    assert len(fake.sent) == 20 and len(set(results)) == 20
    assert {m.client_id for m in fake.sent} == {f"c{i}" for i in range(20)}


# ---------------------------------------------------------------- 独立运行


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def standalone():
    port = free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tests.support.fake_ilink",
            "--port",
            str(port),
            "--confirm-after",
            "0.4",
            "--longpoll",
            "0.3",
        ],  # fmt: skip
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        line = proc.stdout.readline()  # 第一行是监听地址
        assert line.strip() == f"fake_ilink listening on http://127.0.0.1:{port}", line
        yield proc, f"http://127.0.0.1:{port}"
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_standalone_serves_login_inbox_send_and_control_on_one_port(standalone):
    proc, url = standalone
    code = requests.get(f"{url}{BOT}get_bot_qrcode", params={"bot_type": 3}, timeout=3).json()[
        "qrcode"
    ]
    # 取码后 3 秒（这里调成 0.4 秒）才自动确认：之前是 wait
    first = requests.get(f"{url}{BOT}get_qrcode_status", params={"qrcode": code}, timeout=3).json()
    assert first == {"status": "wait"}
    time.sleep(0.5)
    done = requests.get(f"{url}{BOT}get_qrcode_status", params={"qrcode": code}, timeout=3).json()
    assert done["status"] == "confirmed" and done["baseurl"] == url

    requests.post(f"{url}/_fake/inbox", json={"text": "帮助"}, timeout=3)
    got = requests.post(f"{url}{BOT}getupdates", json={"get_updates_buf": ""}, timeout=5).json()
    assert got["msgs"][0]["item_list"][0]["text_item"]["text"] == "帮助"

    sent = requests.post(
        f"{url}{BOT}sendmessage",
        json={
            "msg": {
                "to_user_id": "u",
                "client_id": "c",
                "context_token": "t",
                "item_list": [{"type": 1, "text_item": {"text": "收到"}}],
            }
        },  # fmt: skip
        timeout=3,
    )
    assert sent.json()["ret"] == 0
    listed = requests.get(f"{url}/_fake/sent", timeout=3).json()["sent"]
    assert [m["text"] for m in listed] == ["收到"]

    requests.post(f"{url}/_fake/mode", json={"mode": "session_expired"}, timeout=3)
    assert requests.post(f"{url}{BOT}getupdates", json={}, timeout=3).json()["ret"] == -14
    requests.post(f"{url}/_fake/mode", json={"mode": "normal"}, timeout=3)
    assert requests.post(f"{url}{BOT}getupdates", json={}, timeout=3).json()["ret"] == 0


def test_standalone_stops_cleanly_on_sigterm(standalone):
    proc, _ = standalone
    proc.terminate()
    assert proc.wait(timeout=5) == 0


def test_standalone_help_lists_the_documented_options():
    out = subprocess.run(
        [sys.executable, "-m", "tests.support.fake_ilink", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,  # fmt: skip
    ).stdout
    for option in (
        "--port",
        "--confirm-after",
        "--longpoll",
        "--no-message-id",
        "--close-after-response",
    ):
        assert option in out
