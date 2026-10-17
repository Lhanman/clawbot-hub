"""`POST /v1/replies`。规格：`hub-send`「回复」，`hub-inbox`「回复句柄」「等确认」，
`hub-projects`「请求限速和大小」，`hub-send`「错误格式和 HTTP 状态码」。

发送本身（拆条、重试、额度、判定送达）在 `tests/sender/` 里测过，句柄的各种失效情形在
`tests/inbox/test_handles.py` 里测过；这里看的是接口这一层：字段怎么校验、句柄的错怎么回、
发完以后怎么记回复次数和等确认。
"""

from __future__ import annotations

import pytest

from clawbot_hub.config import Config
from clawbot_hub.loginstate import reset_conversation
from clawbot_hub.quota import PUSH
from clawbot_hub.upstream.base import Accepted, NoResponse, Rejected
from tests.sender.conftest import long_text, takes
from tests.support.fake_upstream import DEFAULT
from tests.support.hub import USER, tweak

SECOND = 1000
MINUTE = 60 * SECOND
HOUR = 60 * MINUTE
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


def mark(api, name: str = "alpha") -> int | None:
    """项目的等确认标记（记到什么时候）。"""
    return api.hub.projects.get(name).awaiting_until_ms


def replies_used(api, item_id: str) -> int:
    row = api.hub.db.conn().execute("SELECT replies FROM inbox WHERE id = ?", (item_id,))
    return row.fetchone()["replies"]


def nothing_was_sent(api) -> None:
    assert api.hub.upstream.sent == []
    assert api.hub.rows() == []
    assert api.hub.used() == {}


# ---------------------------------------------------------------- 发出去


def test_a_reply_goes_out_with_the_credential_of_the_message_it_answers(api):
    first = api.hear("先说的", token="ctx-first")
    api.hear("后说的", token="ctx-latest")

    response = api.reply(first["reply_handle"], "收到")

    assert response.status_code == 200
    (sent,) = api.hub.upstream.sent
    assert (sent.to_user_id, sent.context_token, sent.text) == (USER, "ctx-first", "收到")


def test_the_answer_has_the_same_shape_as_the_one_for_a_message(api):
    item = api.hear()

    response = api.reply(item["reply_handle"], "收到")

    assert response.status_code == 200
    body = response.json()
    assert list(body) == RESULT_FIELDS
    assert (body["status"], body["reason_code"], body["reason"]) == ("sent", None, None)
    assert (body["parts"], body["parts_sent"], body["duplicate"]) == (1, 1, False)
    assert list(body["quota"]) == ["left", "limit", "window_ends_at"]
    row = api.hub.row(body["id"])
    assert (row["kind"], row["status"]) == ("reply", "sent")
    assert row["project_id"] == api.hub.project("alpha").id


def test_a_reply_is_counted_as_a_reply_not_as_a_push(api):
    item = api.hear()

    api.reply(item["reply_handle"])

    assert api.hub.used() == {("alpha", "reply"): 1}


def test_a_reply_still_goes_out_when_the_project_has_used_up_its_pushes(api):
    api.hub.fill(8, project="alpha", kind=PUSH)
    item = api.hear()

    pushed = api.send("再推一条")
    replied = api.reply(item["reply_handle"])

    assert pushed.json()["status"] == "rejected"
    assert replied.json()["status"] == "sent"


def test_a_reply_carries_the_label_of_the_project(api):
    api.hub.add("beta", guaranteed=1, cap=2)
    item = api.hear("状态", to="beta")

    api.reply(item["reply_handle"], "一切正常", as_="beta")

    assert api.hub.texts == ["【beta】一切正常"]


def test_a_reply_cut_into_several_parts_uses_up_one_reply_not_one_per_part(api):
    item = api.hear()

    body = api.reply(item["reply_handle"], long_text(2)).json()

    assert (body["parts"], body["parts_sent"]) == (2, 2)
    assert replies_used(api, item["id"]) == 1


def test_the_answer_only_comes_when_the_send_is_over(api):
    api.hub.upstream.script("send_text", takes(api.hub.clock, 2))
    item = api.hear()
    before = api.hub.clock.now_ms()

    body = api.reply(item["reply_handle"]).json()

    assert body["status"] == "sent"
    assert api.hub.clock.now_ms() - before == 2000


def test_a_reply_can_still_be_sent_after_the_message_was_acknowledged(api):
    item = api.hear("ZZ-UNIQUE-77")
    api.ack([item["id"]])

    response = api.reply(item["reply_handle"], "处理好了")

    assert response.json()["status"] == "sent"
    row = api.hub.db.conn().execute("SELECT state, text, context_token FROM inbox").fetchone()
    assert (row["state"], row["text"], row["context_token"] is not None) == ("acked", None, True)


def test_a_reply_can_be_sent_for_a_leased_message_that_has_since_expired(api):
    item = api.hear()
    api.hub.clock.advance(12 * 60)
    api.services.inbox.cleanup()  # 租期已过、收到已超过保留期：标成"过期"
    assert api.hub.db.conn().execute("SELECT state FROM inbox").fetchone()["state"] == "expired"

    response = api.reply(item["reply_handle"])

    assert response.status_code == 200 and response.json()["status"] == "sent"


def test_a_reply_after_the_handle_ran_out_is_410_and_the_credential_is_cleared(api):
    item = api.hear()
    api.hub.clock.advance(31 * 60)

    response = api.reply(item["reply_handle"])

    assert response.status_code == 410
    assert response.json()["error"] == {
        "code": "handle_expired",
        "message": "回复句柄已过期：话收到后 30 分钟内才能回复",
    }
    nothing_was_sent(api)
    row = api.hub.db.conn().execute("SELECT context_token FROM inbox").fetchone()
    assert row["context_token"] is None


def test_a_message_older_than_the_window_cannot_be_answered(api):
    now = api.hub.clock.now_ms()
    item = api.hear(at_ms=now - 30 * HOUR)

    response = api.reply(item["reply_handle"])

    assert response.status_code == 410
    assert response.json()["error"]["code"] == "handle_expired"
    assert response.json()["error"]["message"] == "这句话太旧，微信已不能回复"
    nothing_was_sent(api)


def test_a_handle_from_before_a_new_login_is_dead(api):
    item = api.hear()
    with api.hub.db.txn() as conn:
        reset_conversation(conn)

    response = api.reply(item["reply_handle"])

    assert response.status_code == 410
    assert response.json()["error"]["code"] == "handle_expired"
    nothing_was_sent(api)


# ---------------------------------------------------------------- 句柄不对


def test_a_handle_that_does_not_exist_is_404(api):
    response = api.reply("rh_" + "x" * 22)

    assert response.status_code == 404
    assert response.json()["error"] == {"code": "unknown_handle", "message": "回复句柄不存在"}
    nothing_was_sent(api)


def test_another_projects_handle_looks_exactly_like_one_that_does_not_exist(api):
    api.hub.add("beta", guaranteed=1, cap=2)
    theirs = api.hear("给 beta", to="beta")

    stolen = api.reply(theirs["reply_handle"], as_="alpha")
    made_up = api.reply("rh_" + "x" * 22, as_="alpha")

    assert stolen.status_code == made_up.status_code == 404
    assert stolen.json() == made_up.json()
    nothing_was_sent(api)
    assert replies_used(api, theirs["id"]) == 0


def test_a_push_only_project_has_no_handles(api):
    api.hub.add("pusher", push_only=True)
    item = api.hear()

    response = api.reply(item["reply_handle"], as_="pusher")

    assert response.status_code == 404
    nothing_was_sent(api)


def test_the_errors_about_the_handle_do_not_show_the_credential(api):
    item = api.hear(token="ctx-secret-xyz")
    api.hub.clock.advance(31 * 60)

    response = api.reply(item["reply_handle"])

    assert "ctx-secret-xyz" not in response.text


# ---------------------------------------------------------------- 回复次数


def test_after_five_successful_replies_the_sixth_is_429_with_retry_after(api):
    item = api.hear()
    for _ in range(5):
        assert api.reply(item["reply_handle"]).json()["status"] == "sent"
    sent_before = len(api.hub.upstream.sent)

    response = api.reply(item["reply_handle"])

    assert response.status_code == 429
    assert response.json()["error"] == {
        "code": "reply_limit",
        "message": "这句话已经回复了 5 次，最多 5 次",
    }
    assert response.headers["Retry-After"] == "1800"  # 句柄还有 30 分钟失效
    assert len(api.hub.upstream.sent) == sent_before
    assert replies_used(api, item["id"]) == 5


def test_the_retry_after_of_the_reply_limit_is_what_is_left_of_the_handle(api):
    item = api.hear()
    for _ in range(5):
        api.reply(item["reply_handle"])
    api.hub.clock.advance(10 * 60)

    response = api.reply(item["reply_handle"])

    assert response.headers["Retry-After"] == "1200"


def test_the_limit_comes_from_the_config(make_api):
    api = make_api(tweak(Config(), reply={"max_per_handle": 2}))
    api.hub.ready()
    item = api.hear()

    results = [api.reply(item["reply_handle"]).status_code for _ in range(3)]

    assert results == [200, 200, 429]


def test_another_handle_has_its_own_count(api):
    first = api.hear("一")
    second = api.hear("二")
    for _ in range(5):
        api.reply(first["reply_handle"])

    assert api.reply(first["reply_handle"]).status_code == 429
    assert api.reply(second["reply_handle"]).status_code == 200


def test_a_reply_that_was_refused_or_failed_does_not_use_up_a_reply(api):
    item = api.hear()
    api.hub.upstream.script("send_text", BAD_TOKEN)

    failed = api.reply(item["reply_handle"]).json()
    assert (failed["status"], failed["parts_sent"]) == ("failed", 0)
    api.hub.fill(10)
    refused = api.reply(item["reply_handle"]).json()
    assert refused["status"] == "rejected"

    assert replies_used(api, item["id"]) == 0


def test_a_reply_that_may_have_arrived_uses_up_a_reply(api):
    item = api.hear()
    api.hub.upstream.script("send_text", NoResponse("读超时", read_timeout=True))

    body = api.reply(item["reply_handle"]).json()

    assert (body["status"], body["parts_sent"]) == ("unconfirmed", 1)
    assert replies_used(api, item["id"]) == 1


def test_a_reply_that_got_part_of_the_way_uses_up_a_reply(api):
    item = api.hear()
    api.hub.upstream.script("send_text", Accepted("m-1"), BAD_TOKEN)

    body = api.reply(item["reply_handle"], long_text(3)).json()

    assert (body["status"], body["parts_sent"]) == ("partial", 1)
    assert replies_used(api, item["id"]) == 1


# ---------------------------------------------------------------- 等确认


def test_a_reply_that_is_waiting_for_confirmation_marks_the_project(api):
    item = api.hear()

    api.reply(item["reply_handle"], "要执行吗？回“对”", awaiting_confirmation=True)

    assert mark(api) == api.hub.clock.now_ms() + 600 * SECOND  # 默认 routing.confirm_seconds


def test_confirm_seconds_sets_how_long_the_wait_lasts(api):
    item = api.hear()

    api.reply(item["reply_handle"], awaiting_confirmation=True, confirm_seconds=120)

    assert mark(api) == api.hub.clock.now_ms() + 120 * SECOND


def test_the_default_comes_from_the_config(make_api):
    api = make_api(tweak(Config(), routing={"confirm_seconds": 90}))
    api.hub.ready()
    item = api.hear()

    api.reply(item["reply_handle"], awaiting_confirmation=True)

    assert mark(api) == api.hub.clock.now_ms() + 90 * SECOND


@pytest.mark.parametrize("seconds", [1, 1800])
def test_the_ends_of_the_allowed_range_are_fine(api, seconds):
    item = api.hear()

    response = api.reply(item["reply_handle"], awaiting_confirmation=True, confirm_seconds=seconds)

    assert response.status_code == 200
    assert mark(api) == api.hub.clock.now_ms() + seconds * SECOND


def test_the_wait_counts_from_when_the_sending_is_over(api):
    item = api.hear()
    start = api.hub.clock.now_ms()
    api.hub.upstream.script("send_text", takes(api.hub.clock, 5))

    api.reply(item["reply_handle"], awaiting_confirmation=True)

    assert mark(api) == start + 5 * SECOND + 600 * SECOND


@pytest.mark.parametrize("fields", [{}, {"awaiting_confirmation": False}])
def test_a_reply_that_is_not_waiting_does_not_mark_the_project(api, fields):
    item = api.hear()

    api.reply(item["reply_handle"], **fields)

    assert mark(api) is None


def test_confirm_seconds_alone_does_not_mark_the_project(api):
    item = api.hear()

    api.reply(item["reply_handle"], confirm_seconds=120)

    assert mark(api) is None


def test_a_reply_that_did_not_go_out_does_not_mark_the_project(api):
    item = api.hear()
    api.hub.upstream.script("send_text", BAD_TOKEN)

    body = api.reply(item["reply_handle"], awaiting_confirmation=True).json()

    assert body["status"] == "failed"
    assert mark(api) is None


def test_a_reply_that_was_turned_down_by_the_quota_does_not_mark_the_project(api):
    item = api.hear()
    api.hub.fill(10)

    body = api.reply(item["reply_handle"], awaiting_confirmation=True).json()

    assert body["status"] == "rejected"
    assert mark(api) is None


def test_a_reply_that_may_have_arrived_marks_the_project(api):
    item = api.hear()
    api.hub.upstream.script("send_text", NoResponse("读超时", read_timeout=True))

    body = api.reply(item["reply_handle"], awaiting_confirmation=True).json()

    assert body["status"] == "unconfirmed"
    assert mark(api) is not None


def test_a_reply_that_got_part_of_the_way_marks_the_project(api):
    item = api.hear()
    api.hub.upstream.script("send_text", Accepted("m-1"), BAD_TOKEN)

    body = api.reply(item["reply_handle"], long_text(3), awaiting_confirmation=True).json()

    assert body["status"] == "partial"
    assert mark(api) is not None


def test_a_newer_message_means_the_project_is_not_waiting_for_a_confirmation(api):
    older = api.hear("旧话")
    api.hub.clock.advance(1)
    api.say("新话")

    api.reply(older["reply_handle"], awaiting_confirmation=True)

    assert mark(api) is None


def test_a_message_that_arrives_while_the_reply_is_being_sent_cancels_the_wait(api):
    item = api.hear("A")

    def meanwhile(call):
        api.say("B")
        return DEFAULT

    api.hub.upstream.script("send_text", meanwhile)

    body = api.reply(item["reply_handle"], awaiting_confirmation=True).json()

    assert body["status"] == "sent"
    assert mark(api) is None


def test_a_message_for_another_project_does_not_cancel_the_wait(api):
    api.hub.add("beta", guaranteed=1, cap=2)
    item = api.hear("A")
    api.say("给 beta", to="beta")

    api.reply(item["reply_handle"], awaiting_confirmation=True)

    assert mark(api) is not None
    assert mark(api, "beta") is None


def test_a_second_waiting_reply_moves_the_mark(api):
    item = api.hear()
    api.reply(item["reply_handle"], awaiting_confirmation=True, confirm_seconds=60)
    first = mark(api)
    api.hub.clock.advance(30)

    api.reply(item["reply_handle"], awaiting_confirmation=True, confirm_seconds=300)

    assert mark(api) == api.hub.clock.now_ms() + 300 * SECOND
    assert mark(api) != first


def test_a_later_reply_that_is_not_waiting_leaves_the_mark_alone(api):
    item = api.hear()
    api.reply(item["reply_handle"], awaiting_confirmation=True)
    first = mark(api)

    api.reply(item["reply_handle"], "补充一句")

    assert mark(api) == first


# ---------------------------------------------------------------- 字段不合格


def raw(api, content: bytes, **kw):
    return api.post(
        "/v1/replies", content=content, headers={"Content-Type": "application/json"}, **kw
    )


BAD_FIELDS = [
    ({"handle": None}, "bad_request"),
    ({"handle": 5}, "bad_request"),
    ({"handle": ["rh_x"]}, "bad_request"),
    ({"handle": {"a": 1}}, "bad_request"),
    ({"text": None}, "bad_request"),
    ({"text": 5}, "bad_request"),
    ({"text": ["收到"]}, "bad_request"),
    ({"text": ""}, "empty_text"),
    ({"text": " \n\t　"}, "empty_text"),
    ({"text": "字" * 8001}, "text_too_long"),
    ({"awaiting_confirmation": "true"}, "bad_request"),
    ({"awaiting_confirmation": 1}, "bad_request"),
    ({"awaiting_confirmation": 0}, "bad_request"),
    ({"awaiting_confirmation": None}, "bad_request"),
    ({"awaiting_confirmation": []}, "bad_request"),
    ({"confirm_seconds": 0}, "bad_request"),
    ({"confirm_seconds": -1}, "bad_request"),
    ({"confirm_seconds": 1801}, "bad_request"),
    ({"confirm_seconds": 3600}, "bad_request"),
    ({"confirm_seconds": 1.5}, "bad_request"),
    ({"confirm_seconds": 600.0}, "bad_request"),
    ({"confirm_seconds": "600"}, "bad_request"),
    ({"confirm_seconds": True}, "bad_request"),
    ({"confirm_seconds": None}, "bad_request"),
    ({"confirm_seconds": [600]}, "bad_request"),
]


@pytest.mark.parametrize(("change", "code"), BAD_FIELDS, ids=lambda value: repr(value)[:40])
def test_a_field_that_does_not_fit_is_refused_and_the_handle_is_not_used(api, change, code):
    item = api.hear()
    fields = {"handle": item["reply_handle"], "text": "收到", **change}

    response = api.post("/v1/replies", json=fields)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == code
    nothing_was_sent(api)
    assert replies_used(api, item["id"]) == 0
    assert mark(api) is None
    assert api.reply(item["reply_handle"]).json()["status"] == "sent"  # 句柄还好好的


def test_confirm_seconds_has_to_fit_even_when_the_reply_is_not_waiting(api):
    item = api.hear()

    response = api.reply(item["reply_handle"], confirm_seconds=3600)

    assert response.status_code == 400
    nothing_was_sent(api)


def test_a_missing_handle_or_text_is_refused(api):
    item = api.hear()

    no_handle = api.post("/v1/replies", json={"text": "收到"})
    no_text = api.post("/v1/replies", json={"handle": item["reply_handle"]})

    assert (no_handle.status_code, no_text.status_code) == (400, 400)
    nothing_was_sent(api)


def test_a_handle_or_text_that_cannot_be_encoded_is_refused(api):
    item = api.hear()
    handle = item["reply_handle"].encode()

    bad_handle = raw(api, b'{"handle": "\\ud800", "text": "ok"}')
    bad_text = raw(api, b'{"handle": "' + handle + b'", "text": "\\ud800"}')

    assert (bad_handle.status_code, bad_text.status_code) == (400, 400)
    assert bad_text.json()["error"]["code"] == "bad_request"
    nothing_was_sent(api)


def test_the_fields_are_checked_before_the_handle(api):
    bad_text = api.reply("rh_" + "x" * 22, "")
    bad_seconds = api.reply("rh_" + "x" * 22, confirm_seconds=0)
    bad_flag = api.reply("rh_" + "x" * 22, awaiting_confirmation="yes")
    bad_handle = api.post("/v1/replies", json={"handle": 5, "text": ""})

    assert [r.json()["error"]["code"] for r in (bad_text, bad_seconds, bad_flag, bad_handle)] == [
        "empty_text",
        "bad_request",
        "bad_request",
        "bad_request",
    ]


def test_the_fields_are_checked_before_an_expired_handle_is_looked_at(api):
    item = api.hear()
    api.hub.clock.advance(31 * 60)

    response = api.reply(item["reply_handle"], "")

    assert response.status_code == 400  # 不是 410
    row = api.hub.db.conn().execute("SELECT context_token FROM inbox").fetchone()
    assert row["context_token"] is not None  # 校验没过，句柄一点没碰


@pytest.mark.parametrize("content", [b"", b"not json", b"[]", b'"x"', b"null", b"5"])
def test_a_body_that_is_not_a_json_object_is_refused(api, content):
    response = raw(api, content)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_unknown_fields_are_ignored(api):
    item = api.hear()

    response = api.reply(item["reply_handle"], priority="urgent", dedupe_key="k", on_closed="skip")

    assert response.status_code == 200
    assert response.json()["status"] == "sent"
    assert api.hub.row(response.json()["id"])["priority"] is None


def test_there_is_no_dedupe_for_replies(api):
    item = api.hear()

    first = api.reply(item["reply_handle"], dedupe_key="k").json()
    second = api.reply(item["reply_handle"], dedupe_key="k").json()

    assert (first["duplicate"], second["duplicate"]) == (False, False)
    assert first["id"] != second["id"]
    assert len(api.hub.upstream.sent) == 2


# ---------------------------------------------------------------- 认证、限速、大小


def test_a_missing_or_wrong_key_is_401_and_the_handle_is_not_touched(api):
    item = api.hear()

    missing = api.reply(item["reply_handle"], as_=None)
    wrong = api.request(
        "POST",
        "/v1/replies",
        key="chub_" + "0" * 43,
        json={"handle": item["reply_handle"], "text": "收到"},
    )

    assert (missing.status_code, wrong.status_code) == (401, 401)
    nothing_was_sent(api)


def test_a_disabled_project_cannot_reply(api):
    item = api.hear()
    api.hub.projects.disable("alpha")

    response = api.reply(item["reply_handle"])

    assert (response.status_code, response.json()["error"]["code"]) == (403, "project_disabled")
    nothing_was_sent(api)


def test_the_wrong_method_is_405_with_the_allowed_one(api):
    response = api.get("/v1/replies")

    assert (response.status_code, response.json()["error"]["code"]) == (405, "method_not_allowed")
    assert response.headers["Allow"] == "POST"


def test_a_body_over_64_kib_is_413(api):
    response = api.post("/v1/replies", content=b"x" * (64 * 1024 + 1))

    assert (response.status_code, response.json()["error"]["code"]) == (413, "too_large")


def test_replies_and_messages_share_one_rate_limit(make_api):
    api = make_api(tweak(Config(), send={"rate_per_minute": 3}))
    api.hub.ready()
    item = api.hear()

    first = api.send("推送")
    second = api.reply(item["reply_handle"])
    third = api.reply(item["reply_handle"])
    over_reply = api.reply(item["reply_handle"])
    over_message = api.send("又一条")

    assert [r.status_code for r in (first, second, third)] == [200, 200, 200]
    assert over_reply.status_code == over_message.status_code == 429
    assert over_reply.json()["error"]["code"] == "rate_limited"
    assert over_reply.headers["Retry-After"].isdigit()
    assert replies_used(api, item["id"]) == 2  # 被限速的那次没有碰句柄


def test_the_reply_that_is_rate_limited_leaves_nothing_behind(make_api):
    api = make_api(tweak(Config(), send={"rate_per_minute": 1}))
    api.hub.ready()
    item = api.hear()
    api.send("占掉这一分钟的次数")
    rows_before = len(api.hub.rows())

    response = api.reply(item["reply_handle"], awaiting_confirmation=True)

    assert response.status_code == 429
    assert len(api.hub.rows()) == rows_before
    assert mark(api) is None
    assert replies_used(api, item["id"]) == 0


def test_the_credential_never_appears_in_the_answer(api):
    item = api.hear(token="ctx-secret-xyz")

    response = api.reply(item["reply_handle"], "收到")

    assert "ctx-secret-xyz" not in response.text
    assert "tok-bot-secret" not in response.text
