"""`POST /v1/messages`、`GET /v1/status`、`GET /v1/health`。规格：`hub-send`「发消息接口」「谁都不能
发给别人」「项目标签」，`hub-quota`「额度可查」，`hub-deploy`「健康检查和版本」，`hub-projects`
「项目之间互相看不到」。

发送本身的各种情形（拆条、重试、额度、去重）在 `tests/sender/` 里测过；这里看的是接口这一层：
请求怎么读、字段怎么校验、结果怎么原样交出去、状态和健康检查给什么。
"""

from __future__ import annotations

import json

import pytest

import clawbot_hub
from clawbot_hub.config import Config
from clawbot_hub.projects import ProjectDisabled
from clawbot_hub.quota import PUSH, REPLY
from clawbot_hub.upstream.base import Accepted, NoResponse, Rejected
from tests.sender.conftest import long_text, takes
from tests.support.hub import USER, tweak

BAD_TOKEN = Rejected(200, 40001, "bad token")
RESULT_FIELDS = [
    "status",
    "reason_code",
    "reason",
    "parts",
    "parts_sent",
    "duplicate",
    "id",
    "quota",
]


def nothing_was_left_behind(api) -> None:
    assert api.hub.rows() == []
    assert api.hub.used() == {}
    assert api.hub.upstream.calls == []


# ---------------------------------------------------------------- 发一条


def test_a_message_is_sent_and_the_answer_has_the_fixed_shape(api):
    response = api.send("盘点完成\n共 3 项")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert list(body) == RESULT_FIELDS
    assert body["status"] == "sent"
    assert body["reason_code"] is None and body["reason"] is None
    assert (body["parts"], body["parts_sent"], body["duplicate"]) == (1, 1, False)
    assert body["id"].startswith("o_")
    assert list(body["quota"]) == ["left", "limit", "window_ends_at"]
    assert body["quota"] == {"left": 7, "limit": 10, "window_ends_at": "2026-10-13T10:00:00+08:00"}
    assert len(api.hub.upstream.sent) == 1  # 假上游恰好收到一条


def test_the_id_is_the_send_record_in_the_service(api):
    body = api.send().json()

    row = api.hub.row(body["id"])
    assert (row["status"], row["parts_sent"], row["kind"]) == ("sent", 1, "push")


def test_what_is_sent_is_the_text_exactly_as_written(api):
    api.send("  盘点完成\n共 3 项  ")

    assert api.hub.texts == ["  盘点完成\n共 3 项  "]


def test_the_message_goes_to_the_owner_with_the_latest_conversation_credential(api):
    api.send("你好")

    (sent,) = api.hub.upstream.sent
    assert (sent.to_user_id, sent.context_token) == (USER, "ctx-1")


def test_the_recipient_and_the_credential_cannot_be_chosen(api):
    response = api.send("你好", to_user_id="someone@im.wechat", context_token="ctx-evil")

    assert response.status_code == 200
    (sent,) = api.hub.upstream.sent
    assert (sent.to_user_id, sent.context_token) == (USER, "ctx-1")


def test_unknown_fields_are_ignored(api):
    response = api.send("你好", foo="bar", nested={"a": [1, 2]}, other=None)

    assert response.status_code == 200
    assert response.json()["status"] == "sent"


def test_a_project_with_a_label_sends_under_it(api):
    api.hub.add("beta", guaranteed=1, cap=2)

    api.send("盘点完成\n共 3 项", as_="beta")

    assert api.hub.texts == ["【beta】盘点完成\n共 3 项"]


def test_the_default_project_sends_without_a_label(api):
    api.send("盘前简报")

    assert api.hub.texts == ["盘前简报"]


def test_the_answer_only_comes_when_the_send_is_over(api):
    api.hub.upstream.script("send_text", takes(api.hub.clock, 2))
    before = api.hub.clock.now_ms()

    body = api.send("你好").json()

    assert body["status"] == "sent"  # 不是先给一个"处理中"
    assert api.hub.clock.now_ms() - before == 2000


@pytest.mark.parametrize("priority", ["urgent", "normal", "deferrable"])
def test_each_priority_is_accepted(api, priority):
    body = api.send("你好", priority=priority).json()

    assert body["status"] == "sent"
    assert api.hub.row(body["id"])["priority"] == priority


def test_the_priority_is_normal_when_not_given(api):
    body = api.send("你好").json()

    assert api.hub.row(body["id"])["priority"] == "normal"


# ---------------------------------------------------------------- 发送的每一种结果原样交出去


def test_a_closed_window_is_a_rejection_in_a_200(api):
    api.hub.clock.advance(25 * 3600)

    response = api.send("你好")

    assert response.status_code == 200  # 业务上的"没发"不是 HTTP 错误
    body = response.json()
    assert (body["status"], body["reason_code"], body["parts_sent"]) == (
        "rejected",
        "window_closed",
        0,
    )
    assert body["reason"] == (
        "对话窗口过期了（你最后一次发消息是 10月12日 10:00），给 ClawBot 发任意一句话就能恢复"
    )
    assert body["quota"] == {"left": 0, "limit": 10, "window_ends_at": None}
    assert api.hub.upstream.calls == []


def test_a_dead_login_is_a_failure_in_a_200(api):
    api.hub.expire()

    body = api.send("你好").json()

    assert (body["status"], body["reason_code"]) == ("failed", "login_expired")
    assert body["parts_sent"] == 0


def test_skip_turns_a_closed_window_into_a_skip(api):
    api.hub.clock.advance(25 * 3600)

    body = api.send("你好", on_closed="skip").json()

    assert (body["status"], body["reason_code"], body["parts_sent"]) == (
        "skipped",
        "window_closed",
        0,
    )


def test_an_answer_that_never_came_is_unconfirmed(api):
    api.hub.upstream.script("send_text", NoResponse("读超时", read_timeout=True))

    body = api.send("你好").json()

    assert (body["status"], body["reason_code"], body["parts_sent"]) == (
        "unconfirmed",
        "no_response",
        1,
    )


def test_a_long_message_cut_off_halfway_is_partial(api):
    api.hub.upstream.script("send_text", Accepted("m-1"), BAD_TOKEN)

    body = api.send(long_text(3)).json()

    assert (body["status"], body["reason_code"]) == ("partial", "upstream_rejected")
    assert (body["parts"], body["parts_sent"]) == (3, 1)
    assert body["reason"].endswith("；发出了 1 条，共 3 条")


def test_the_answer_is_what_the_send_record_says(api):
    api.hub.upstream.script("send_text", Accepted("m-1"), BAD_TOKEN)

    body = api.send(long_text(3)).json()

    row = api.hub.row(body["id"])
    assert (row["status"], row["reason_code"], row["reason"]) == (
        body["status"],
        body["reason_code"],
        body["reason"],
    )
    assert (row["parts"], row["parts_sent"]) == (body["parts"], body["parts_sent"])


def test_the_same_dedupe_key_gets_the_first_answer_again(api):
    first = api.send("你好", dedupe_key="daily").json()

    second = api.send("你好", dedupe_key="daily").json()

    assert first["duplicate"] is False and second["duplicate"] is True
    assert second["id"] == first["id"]
    assert second["status"] == "sent"
    assert len(api.hub.upstream.sent) == 1


def test_a_dedupe_key_is_the_projects_own(api):
    api.hub.add("beta", guaranteed=1, cap=2)

    first = api.send("你好", dedupe_key="daily").json()
    second = api.send("你好", dedupe_key="daily", as_="beta").json()

    assert (first["status"], second["status"]) == ("sent", "sent")
    assert (first["duplicate"], second["duplicate"]) == (False, False)
    assert len(api.hub.upstream.sent) == 2


def test_a_project_disabled_during_the_send_gets_a_403(api, monkeypatch):
    def refuse(project, *args, **kwargs):
        raise ProjectDisabled(project)

    monkeypatch.setattr(api.services.sender, "push", refuse)

    response = api.send("你好")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "project_disabled"


# ---------------------------------------------------------------- 请求不合格


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"text": ""}, "empty_text"),
        ({"text": "   "}, "empty_text"),
        ({"text": "\n\t \n"}, "empty_text"),
        ({"text": "字" * 8001}, "text_too_long"),
        ({}, "bad_request"),
        ({"text": None}, "bad_request"),
        ({"text": 5}, "bad_request"),
        ({"text": ["你好"]}, "bad_request"),
        ({"text": {"a": 1}}, "bad_request"),
        ({"text": "你好", "priority": "high"}, "bad_request"),
        ({"text": "你好", "priority": 1}, "bad_request"),
        ({"text": "你好", "priority": None}, "bad_request"),
        ({"text": "你好", "on_closed": "ignore"}, "bad_request"),
        ({"text": "你好", "on_closed": True}, "bad_request"),
        ({"text": "你好", "dedupe_key": ""}, "bad_request"),
        ({"text": "你好", "dedupe_key": "k" * 129}, "bad_request"),
        ({"text": "你好", "dedupe_key": 5}, "bad_request"),
    ],
    ids=lambda value: (
        json.dumps(value, ensure_ascii=False)[:30] if isinstance(value, dict) else value
    ),
)
def test_a_request_with_a_wrong_field_is_a_400_that_leaves_nothing_behind(api, fields, code):
    response = api.post("/v1/messages", json=fields)

    assert response.status_code == 400
    assert list(response.json()) == ["error"]
    assert response.json()["error"]["code"] == code
    nothing_was_left_behind(api)


def test_a_wrong_priority_names_the_choices(api):
    error = api.send("你好", priority="high").json()["error"]

    assert error["message"] == "priority 的值不对，可选：urgent、normal、deferrable"


def test_a_wrong_on_closed_names_the_choices(api):
    error = api.send("你好", on_closed="ignore").json()["error"]

    assert error["message"] == "on_closed 的值不对，可选：fail、skip"


def test_an_empty_text_and_a_too_long_text_say_what_is_wrong(api):
    empty = api.send("  ").json()["error"]
    long = api.send("字" * 8001).json()["error"]

    assert empty == {"code": "empty_text", "message": "文字不能是空的"}
    assert long == {"code": "text_too_long", "message": "文字太长：最多 8000 个字"}


def test_the_longest_allowed_text_is_sent(api):
    assert api.send("字" * 8000).json()["status"] == "sent"


def test_the_text_limit_is_the_configured_one(make_api):
    api = make_api(tweak(Config(), send={"max_chars": 300, "part_chars": 200}))
    api.hub.ready()

    assert api.send("字" * 300).json()["status"] == "sent"
    over = api.send("字" * 301)

    assert over.status_code == 400
    assert over.json()["error"]["code"] == "text_too_long"


@pytest.mark.parametrize(
    "body",
    [
        b"",
        "这不是 JSON".encode(),
        b"{not json}",
        '{"text": "你好"'.encode(),  # 没写完
        '{"text": "你好"} {"text": "再来"}'.encode(),  # 后面还有东西
        b"[]",
        '["你好"]'.encode(),
        b"5",
        '"你好"'.encode(),
        b"null",
        b"true",
        b'{"text": "\xff\xfe"}',  # 不是 UTF-8
        b"[" * 20000 + b"]" * 20000,  # 嵌套深到解析器放弃
    ],
    ids=[
        "empty",
        "words",
        "braces",
        "unfinished",
        "two-objects",
        "array",
        "array-of-text",
        "number",
        "string",
        "null",
        "true",
        "not-utf8",
        "too-deep",
    ],
)
def test_a_body_that_is_not_a_json_object_is_a_400(api, body):
    response = api.post("/v1/messages", content=body, headers={"Content-Type": "application/json"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    nothing_was_left_behind(api)


def test_a_lone_surrogate_in_the_text_is_a_400_not_a_crash(api):
    response = api.post(
        "/v1/messages",
        content=b'{"text": "\\ud800"}',  # JSON 里合法的写法，但没法编码成 UTF-8
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    nothing_was_left_behind(api)


def test_the_content_type_is_not_checked(api):
    response = api.post(
        "/v1/messages",
        content='{"text": "你好"}'.encode(),
        headers={"Content-Type": "text/plain"},
    )

    assert response.status_code == 200


# ---------------------------------------------------------------- 状态


def test_the_status_has_the_fixed_shape(api):
    response = api.get("/v1/status")

    assert response.status_code == 200
    body = response.json()
    assert list(body) == ["service", "login", "window", "quota"]
    assert list(body["service"]) == [
        "version",
        "api",
        "send_timeout_seconds",
        "inbox_online_seconds",
    ]
    assert list(body["login"]) == ["state"]
    assert list(body["window"]) == ["open", "started_at", "ends_at", "seconds_left"]
    assert list(body["quota"]) == ["limit", "used", "free", "project"]
    assert list(body["quota"]["project"]) == ["guaranteed", "cap", "used", "left", "replies_used"]


def test_the_status_of_a_fresh_window(api):
    body = api.get("/v1/status").json()

    assert body["service"] == {
        "version": clawbot_hub.__version__,
        "api": "1",
        "send_timeout_seconds": 30,
        "inbox_online_seconds": 90,
    }
    assert body["login"] == {"state": "ok"}
    assert body["window"] == {
        "open": True,
        "started_at": "2026-10-12T10:00:00+08:00",
        "ends_at": "2026-10-13T10:00:00+08:00",
        "seconds_left": 86400,
    }
    assert body["quota"] == {
        "limit": 10,
        "used": 0,
        "free": 10,
        "project": {"guaranteed": 6, "cap": 8, "used": 0, "left": 8, "replies_used": 0},
    }


def test_the_time_left_runs_down(api):
    api.hub.clock.advance(3600 + 0.9)

    window = api.get("/v1/status").json()["window"]

    assert window["seconds_left"] == 82799  # 秒数向下取整


def test_the_status_tells_the_clients_the_services_timeouts(make_api):
    api = make_api(tweak(Config(), send={"timeout_seconds": 45}, inbox={"online_seconds": 120}))
    api.hub.ready()

    service = api.get("/v1/status").json()["service"]

    assert (service["send_timeout_seconds"], service["inbox_online_seconds"]) == (45, 120)


def test_the_status_example_in_the_spec(api):
    """规格「额度可查」的示例：L=10、R=2，另有一个启用的项目保底 2 一条没用；
    窗口里的 4 条是调用方自己的 3 条推送加 1 条回复。"""
    api.hub.add("beta", guaranteed=2, cap=3)
    api.hub.fill(3, project="alpha", kind=PUSH)
    api.hub.fill(1, project="alpha", kind=REPLY)

    quota = api.get("/v1/status").json()["quota"]

    assert quota == {
        "limit": 10,
        "used": 4,
        "free": 6,
        "project": {"guaranteed": 6, "cap": 8, "used": 3, "left": 3, "replies_used": 1},
    }


def test_the_status_with_no_other_project(api):
    api.hub.fill(3, project="alpha", kind=PUSH)
    api.hub.fill(1, project="alpha", kind=REPLY)

    project = api.get("/v1/status").json()["quota"]["project"]

    assert project["left"] == 5  # 只有这一个项目时共享池是 2


def test_the_status_counts_the_whole_windows_use_but_only_my_own_in_the_project_part(api):
    beta = api.hub.add("beta", guaranteed=2, cap=3)
    api.hub.fill(2, project=beta.name, kind=PUSH)
    api.hub.fill(1, project="_hub", kind=REPLY)
    api.hub.fill(2, project="alpha", kind=PUSH)

    quota = api.get("/v1/status").json()["quota"]

    assert (quota["used"], quota["free"]) == (5, 5)
    assert quota["project"]["used"] == 2
    assert quota["project"]["replies_used"] == 0


def test_the_status_left_is_what_a_send_would_report(api):
    api.hub.add("beta", guaranteed=2, cap=3)
    sent = api.send("你好").json()

    left = api.get("/v1/status").json()["quota"]["project"]["left"]

    assert left == sent["quota"]["left"]


def test_the_status_does_not_show_other_projects(api):
    beta = api.hub.add("beta", prefix="贝塔", guaranteed=2, cap=3)
    api.hub.fill(2, project=beta.name, kind=PUSH)

    text = api.get("/v1/status").text

    assert "beta" not in text and "贝塔" not in text


def test_a_push_only_project_has_a_status_too(api):
    api.hub.add("cron-job", guaranteed=1, cap=2, push_only=True)

    response = api.get("/v1/status", as_="cron-job")

    assert response.status_code == 200
    assert response.json()["quota"]["project"]["guaranteed"] == 1


def test_a_closed_window_in_the_status(api):
    api.send("你好")
    api.hub.clock.advance(25 * 3600)

    body = api.get("/v1/status").json()

    assert body["window"] == {
        "open": False,
        "started_at": "2026-10-12T10:00:00+08:00",  # 你最后一次说话的时间
        "ends_at": None,
        "seconds_left": 0,
    }
    assert body["quota"] == {
        "limit": 10,
        "used": 0,
        "free": 0,
        "project": {"guaranteed": 6, "cap": 8, "used": 0, "left": 0, "replies_used": 0},
    }


def test_no_window_at_all_in_the_status(make_api):
    api = make_api()
    api.hub.bind()
    api.hub.add("alpha", guaranteed=6, cap=8)

    body = api.get("/v1/status").json()

    assert body["window"] == {
        "open": False,
        "started_at": None,
        "ends_at": None,
        "seconds_left": 0,
    }
    assert body["quota"]["project"]["left"] == 0


def test_a_rebinding_closes_the_window_in_the_status(api):
    api.hub.bind(bot_token="tok-bot-secret-2")  # 换了账号：旧窗口作废
    api.services.monitor.sync()

    window = api.get("/v1/status").json()["window"]

    assert window["open"] is False and window["started_at"] is None


@pytest.mark.parametrize("state", ["expired", "missing", "broken"])
def test_the_login_states_in_the_status(api, state):
    {"expired": api.hub.expire, "missing": api.hub.unbind, "broken": api.hub.corrupt}[state]()

    body = api.get("/v1/status").json()

    assert body["login"] == {"state": state}
    assert body["quota"]["project"]["left"] == 0
    assert body["window"]["open"] is True  # 窗口本身还开着，是登录不行


def test_the_login_state_is_ok_when_all_is_well(api):
    assert api.get("/v1/status").json()["login"]["state"] == "ok"


def test_a_pause_after_a_refusal_shows_as_nothing_left(api):
    api.hub.upstream.script("send_text", BAD_TOKEN)
    api.send("你好")

    body = api.get("/v1/status").json()

    assert body["window"]["open"] is True
    assert body["quota"]["project"]["left"] == 0


def test_the_status_is_cheap_to_ask(api):
    for _ in range(40):  # 限速只管发消息和回复
        assert api.get("/v1/status").status_code == 200


# ---------------------------------------------------------------- 健康检查


def test_health_has_exactly_three_fields_and_needs_no_key(api):
    response = api.get("/v1/health", as_=None)

    assert response.status_code == 200
    assert response.json() == {"ok": True, "version": clawbot_hub.__version__, "api": "1"}
    assert isinstance(response.json()["api"], str)


def test_health_says_nothing_about_login_windows_or_projects(api):
    api.hub.expire()

    text = api.get("/v1/health", as_=None).text

    for word in ("alpha", "expired", "login", "window", "quota", "project"):
        assert word not in text


def test_health_does_not_touch_the_database_or_the_upstream(api):
    api.hub.upstream.clear_calls()
    api.services.db.close()  # 库打不开了，探活照样回答

    assert api.get("/v1/health", as_=None).status_code == 200
    assert api.hub.upstream.calls == []
