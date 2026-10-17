"""`GET /v1/inbox`、`POST /v1/inbox/ack`。规格：`hub-inbox`「取话」「确认」「回复句柄」，
`hub-deploy`「正常关闭」，`hub-send`「错误格式和 HTTP 状态码」。

待取队列本身（租约、重发、保留期、排序、顶掉旧请求……）在 `tests/inbox/` 里测过；这里看的是
接口这一层：参数怎么读、结果长什么样、错误怎么回、认证和限速怎么管它、关闭时怎么放人。
"""

from __future__ import annotations

import pytest

from clawbot_hub.config import Config
from tests.support.hub import tweak
from tests.support.threads import run_in_thread, wait_for

SECOND = 1000
MINUTE = 60 * SECOND
HOUR = 60 * MINUTE


def ids_of(response) -> list[str]:
    return [item["id"] for item in response.json()["items"]]


def states(api) -> list[str]:
    rows = api.hub.db.conn().execute("SELECT state FROM inbox ORDER BY rowid").fetchall()
    return [row["state"] for row in rows]


def waiting(api, query: str, **kw):
    """另开一个线程发一个会等的取话请求，等它挂上（睡进了假时钟）再返回。"""
    before = api.hub.clock.blocked_count
    worker = run_in_thread(api.fetch, query, **kw)
    wait_for(lambda: api.hub.clock.blocked_count > before)
    return worker


# ---------------------------------------------------------------- 取话：结果的样子


def test_a_waiting_message_comes_back_at_once_with_the_fixed_shape(api):
    now = api.hub.clock.now_ms()
    item_id = api.say("持仓", at_ms=now - 2 * SECOND, received_ms=now - SECOND, via="prefix")

    response = api.fetch("wait=30")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert list(body) == ["items", "lease_seconds"]
    assert body["lease_seconds"] == 60
    [item] = body["items"]
    assert list(item) == [
        "id",
        "text",
        "has_text",
        "at",
        "at_ms",
        "received_at",
        "attempt",
        "via",
        "reply_handle",
    ]
    assert item["id"] == item_id
    assert (item["text"], item["has_text"], item["attempt"], item["via"]) == (
        "持仓",
        True,
        1,
        "prefix",
    )
    assert item["at_ms"] == now - 2 * SECOND
    assert item["at"] == "2026-10-12T09:59:58+08:00"
    assert item["received_at"] == "2026-10-12T09:59:59+08:00"
    assert api.hub.clock.now_ms() == now  # 有话就不等


def test_the_handle_is_rh_plus_22_random_characters(api):
    api.say()

    [item] = api.fetch().json()["items"]

    assert item["reply_handle"].startswith("rh_")
    assert len(item["reply_handle"]) == 25


def test_the_times_are_written_in_the_display_timezone(make_api):
    api = make_api(tweak(Config(), display={"timezone": "UTC"}))
    api.hub.ready()
    now = api.hub.clock.now_ms()
    api.say(at_ms=now - HOUR, received_ms=now)

    [item] = api.fetch().json()["items"]

    assert item["at"] == "2026-10-12T01:00:00+00:00"
    assert item["received_at"] == "2026-10-12T02:00:00+00:00"
    assert item["at_ms"] == now - HOUR


def test_a_late_message_shows_both_its_own_time_and_when_it_was_received(api):
    now = api.hub.clock.now_ms()
    api.say(at_ms=now - 3 * HOUR, received_ms=now)

    [item] = api.fetch().json()["items"]

    assert (item["at"], item["received_at"]) == (
        "2026-10-12T07:00:00+08:00",
        "2026-10-12T10:00:00+08:00",
    )


def test_a_message_without_text_has_empty_text_and_no_has_text(api):
    api.say("", has_text=False)

    [item] = api.fetch().json()["items"]

    assert (item["text"], item["has_text"]) == ("", False)


def test_the_lease_seconds_comes_from_the_config(make_api):
    api = make_api(tweak(Config(), inbox={"lease_seconds": 90}))
    api.hub.ready()

    assert api.fetch().json()["lease_seconds"] == 90


def test_nothing_queued_and_no_wait_is_an_empty_list_at_once(api):
    now = api.hub.clock.now_ms()

    response = api.fetch("wait=0")

    assert (response.status_code, response.json()) == (200, {"items": [], "lease_seconds": 60})
    assert api.hub.clock.now_ms() == now


def test_the_credential_never_appears_in_what_the_project_gets(api):
    api.say("持仓", token="ctx-secret-xyz")

    response = api.fetch()

    assert "ctx-secret-xyz" not in response.text
    assert "tok-bot-secret" not in response.text


def test_a_project_only_gets_its_own_messages(api):
    api.hub.add("beta", guaranteed=1, cap=2)
    mine = api.say("给 alpha", to="alpha")
    theirs = api.say("给 beta", to="beta")

    assert ids_of(api.fetch(as_="alpha")) == [mine]
    assert ids_of(api.fetch(as_="beta")) == [theirs]


def test_messages_come_in_the_order_of_their_own_time(api):
    now = api.hub.clock.now_ms()
    c = api.say("c", at_ms=now - 1 * MINUTE)
    a = api.say("a", at_ms=now - 3 * MINUTE)
    b = api.say("b", at_ms=now - 2 * MINUTE)

    assert ids_of(api.fetch()) == [a, b, c]


def test_a_message_that_is_not_confirmed_comes_again_with_the_same_handle(api):
    api.say("持仓")
    [first] = api.fetch().json()["items"]
    assert api.fetch().json()["items"] == []  # 租期里不会再给

    api.hub.clock.advance(61)
    [again] = api.fetch().json()["items"]

    assert (again["id"], again["reply_handle"], again["attempt"]) == (
        first["id"],
        first["reply_handle"],
        2,
    )


# ---------------------------------------------------------------- 取话：wait


def test_the_request_waits_until_a_message_arrives(api):
    worker = waiting(api, "wait=30")
    assert worker.still_blocked()

    item_id = api.say("持仓")

    assert ids_of(worker.result()) == [item_id]


def test_the_request_gives_up_when_the_wait_is_over(api):
    worker = waiting(api, "wait=2")

    api.hub.clock.advance(1)
    assert worker.still_blocked()
    api.hub.clock.advance(1)

    response = worker.result()
    assert (response.status_code, response.json()["items"]) == (200, [])


def test_without_wait_the_config_default_is_used(make_api):
    api = make_api(tweak(Config(), inbox={"wait_seconds": 2}))
    api.hub.ready()
    worker = waiting(api, "max=10")

    api.hub.clock.advance(1)
    assert worker.still_blocked()
    api.hub.clock.advance(1)

    assert worker.result().json()["items"] == []


def test_a_wait_over_the_limit_is_cut_to_the_limit_not_refused(make_api):
    api = make_api(tweak(Config(), inbox={"max_wait": 3}))
    api.hub.ready()
    worker = waiting(api, "wait=100")

    api.hub.clock.advance(3)

    response = worker.result()
    assert (response.status_code, response.json()["items"]) == (200, [])


def test_the_default_limit_of_the_wait_is_55_seconds(api):
    worker = waiting(api, "wait=100")

    for _ in range(10):  # 等待分成最长 5 秒一段：每走 5 秒它醒一次，核对完再睡
        api.hub.clock.advance(5)
        wait_for(lambda: api.hub.clock.pending_deadlines())
    assert not worker.done  # 50 秒了还在等
    api.hub.clock.advance(5)

    response = worker.result()
    assert (response.status_code, response.json()["items"]) == (200, [])


@pytest.mark.parametrize("digits", ["9" * 13, "9" * 30, "9" * 5000])
def test_a_huge_number_is_also_just_cut_to_the_limit(make_api, digits):
    api = make_api(tweak(Config(), inbox={"max_wait": 3}))
    api.hub.ready()
    worker = waiting(api, f"wait={digits}")

    api.hub.clock.advance(3)

    assert worker.result().status_code == 200


def test_leading_zeros_do_not_change_the_number(make_api):
    api = make_api(tweak(Config(), inbox={"max_wait": 3}))
    api.hub.ready()
    zeros = "0" * 40
    worker = waiting(api, f"wait={zeros}2")

    api.hub.clock.advance(1)
    assert worker.still_blocked()
    api.hub.clock.advance(1)

    assert worker.result().status_code == 200


def test_an_all_zero_wait_is_no_wait(api):
    now = api.hub.clock.now_ms()

    response = api.fetch("wait=" + "0" * 40)

    assert (response.status_code, response.json()["items"]) == (200, [])
    assert api.hub.clock.now_ms() == now


def test_when_the_same_parameter_is_written_twice_the_last_one_counts(api):
    now = api.hub.clock.now_ms()
    for _ in range(3):
        api.say()

    first = api.fetch("wait=30&wait=0&max=1&max=2")

    assert len(first.json()["items"]) == 2  # `max` 取最后一个
    assert api.hub.clock.now_ms() == now  # `wait` 取最后一个 0：不等


def test_unknown_parameters_are_ignored(api):
    api.say()

    response = api.fetch("wait=0&foo=bar&project=beta")

    assert len(response.json()["items"]) == 1


# ---------------------------------------------------------------- 取话：max


def test_max_defaults_to_ten(api):
    for n in range(12):
        api.say(f"话 {n}")

    assert len(api.fetch().json()["items"]) == 10
    assert len(api.fetch().json()["items"]) == 2


def test_max_limits_the_number_returned(api):
    for n in range(5):
        api.say(f"话 {n}")

    assert len(api.fetch("wait=0&max=3").json()["items"]) == 3


def test_max_over_50_is_cut_to_50(make_api):
    api = make_api(tweak(Config(), inbox={"max_queued": 80}))
    api.hub.ready()
    for n in range(60):
        api.say(f"话 {n}")

    response = api.fetch("wait=0&max=100")

    assert (response.status_code, len(response.json()["items"])) == (200, 50)
    assert states(api).count("leased") == 50


@pytest.mark.parametrize("digits", ["9" * 13, "9" * 5000])
def test_a_huge_max_is_also_cut_to_50(make_api, digits):
    api = make_api(tweak(Config(), inbox={"max_queued": 80}))
    api.hub.ready()
    for n in range(60):
        api.say(f"话 {n}")

    response = api.fetch(f"wait=0&max={digits}")

    assert (response.status_code, len(response.json()["items"])) == (200, 50)


def test_max_zero_is_a_heartbeat_that_leases_nothing_and_does_not_wait(api):
    api.say()
    now = api.hub.clock.now_ms()

    response = api.fetch("wait=30&max=0")

    assert (response.status_code, response.json()) == (200, {"items": [], "lease_seconds": 60})
    assert api.hub.clock.now_ms() == now  # `wait` 被忽略
    assert states(api) == ["queued"]


def test_a_heartbeat_counts_as_being_online(api):
    project = api.hub.project("alpha")
    assert api.services.inbox.online(project) is False

    api.fetch("wait=0&max=0")

    assert api.services.inbox.online(api.hub.project("alpha")) is True


# ---------------------------------------------------------------- 取话：不合格的参数


BAD_NUMBERS = ["-1", "abc", "1.5", "", "+1", "1e3", "0x10", "١٢", "１２", " 5", "5 ", "1_0", "--1"]


@pytest.mark.parametrize("value", BAD_NUMBERS)
def test_a_wait_that_is_not_a_whole_number_of_at_least_zero_is_refused(api, value):
    api.say()

    response = api.fetch(f"wait={value}")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    assert "wait" in response.json()["error"]["message"]
    assert states(api) == ["queued"]  # 什么也没租出


@pytest.mark.parametrize("value", BAD_NUMBERS)
def test_a_max_that_is_not_a_whole_number_of_at_least_zero_is_refused(api, value):
    api.say()

    response = api.fetch(f"max={value}")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    assert "max" in response.json()["error"]["message"]
    assert states(api) == ["queued"]


def test_max_zero_does_not_excuse_a_bad_wait(api):
    response = api.fetch("wait=abc&max=0")

    assert response.status_code == 400


def test_the_refusal_does_not_repeat_what_was_asked(api):
    response = api.fetch("wait=SECRET-VALUE-1")

    assert "SECRET-VALUE-1" not in response.text


# ---------------------------------------------------------------- 取话：谁能取


def test_a_push_only_project_cannot_fetch(api):
    api.hub.add("pusher", push_only=True)

    response = api.fetch("wait=0", as_="pusher")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "push_only"


def test_a_push_only_project_is_told_so_before_the_parameters_are_looked_at(api):
    api.hub.add("pusher", push_only=True)

    response = api.fetch("wait=abc", as_="pusher")

    assert (response.status_code, response.json()["error"]["code"]) == (403, "push_only")


def test_a_disabled_project_cannot_fetch(api):
    api.hub.projects.disable("alpha")

    response = api.fetch()

    assert (response.status_code, response.json()["error"]["code"]) == (403, "project_disabled")


def test_a_project_disabled_while_waiting_is_refused_and_gets_nothing(api):
    worker = waiting(api, "wait=30")
    api.hub.projects.disable("alpha")
    api.hub.clock.advance(5)  # 等待最长 5 秒一段，每醒来一次核对一次

    response = worker.result()

    assert (response.status_code, response.json()["error"]["code"]) == (403, "project_disabled")
    assert "items" not in response.json()


def test_a_message_that_arrives_after_the_project_is_disabled_is_not_handed_over(api):
    worker = waiting(api, "wait=30")
    api.hub.projects.disable("alpha")
    api.say("持仓")  # 叫醒等着的请求

    response = worker.result()

    assert response.status_code == 403
    assert states(api) == ["queued"]


def test_a_missing_or_wrong_key_is_401(api):
    missing = api.fetch(as_=None)
    wrong = api.request("GET", "/v1/inbox", key="chub_" + "0" * 43)

    for response in (missing, wrong):
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"


def test_the_wrong_method_is_405_with_the_allowed_one(api):
    response = api.post("/v1/inbox", json={})

    assert (response.status_code, response.json()["error"]["code"]) == (405, "method_not_allowed")
    assert response.headers["Allow"] == "GET"


# ---------------------------------------------------------------- 取话：新请求顶掉旧请求


def test_a_new_waiting_request_replaces_the_old_one(api):
    old = waiting(api, "wait=30")

    new = run_in_thread(api.fetch, "wait=30")

    first = old.result()  # 旧的马上返回，不用等时间走
    assert (first.status_code, first.json()["items"]) == (200, [])
    wait_for(lambda: api.hub.clock.blocked_count == 1)  # 新的睡下了
    assert new.still_blocked()
    item_id = api.say("持仓")
    assert ids_of(new.result()) == [item_id]


def test_a_heartbeat_does_not_replace_a_waiting_request(api):
    worker = waiting(api, "wait=30")

    api.fetch("wait=0&max=0")
    api.fetch("wait=0")

    assert worker.still_blocked()
    item_id = api.say("持仓")
    assert ids_of(worker.result()) == [item_id]


# ---------------------------------------------------------------- 取话：关闭


def test_a_waiting_request_is_let_go_when_the_service_starts_to_shut_down(api):
    api.say("持仓")
    api.fetch()  # 租出去，队列里没有可取的了
    worker = waiting(api, "wait=30")

    api.services.begin_shutdown()

    response = worker.result()
    assert (response.status_code, response.json()["items"]) == (200, [])


def test_a_request_let_go_by_the_shutdown_leases_nothing(api):
    worker = waiting(api, "wait=30")
    api.say("持仓", wake=False)  # 话在库里了，只是没人叫醒等着的请求

    api.services.begin_shutdown()  # 叫醒它的是关闭

    assert worker.result().json()["items"] == []
    assert states(api) == ["queued"]


def test_new_requests_during_the_shutdown_are_503(api):
    api.services.begin_shutdown()

    for response in (api.fetch(), api.ack([]), api.reply("rh_x")):
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "shutting_down"


# ---------------------------------------------------------------- 取话和确认不受请求限速管


def test_fetching_and_acknowledging_are_not_counted_against_the_request_rate(make_api):
    api = make_api(tweak(Config(), send={"rate_per_minute": 3}))
    api.hub.ready()

    for _ in range(10):
        assert api.fetch().status_code == 200
        assert api.ack([]).status_code == 200

    assert api.send().status_code == 200  # 一次都没被计入


# ---------------------------------------------------------------- 确认


def test_acknowledging_a_leased_message_counts_it_and_clears_the_text(api):
    api.say("ZZ-UNIQUE-77")
    [item] = api.fetch().json()["items"]

    response = api.ack([item["id"]])

    assert (response.status_code, response.json()) == (200, {"acked": 1, "unknown": []})
    row = api.hub.db.conn().execute("SELECT state, text FROM inbox").fetchone()
    assert (row["state"], row["text"]) == ("acked", None)
    api.hub.clock.advance(61)
    assert api.fetch().json()["items"] == []  # 不再给


def test_an_empty_list_is_fine(api):
    response = api.ack([])

    assert (response.status_code, response.json()) == (200, {"acked": 0, "unknown": []})


def test_an_acknowledgement_after_the_lease_ran_out_still_counts(api):
    api.say()
    [item] = api.fetch().json()["items"]
    api.hub.clock.advance(70)

    response = api.ack([item["id"]])

    assert response.json() == {"acked": 1, "unknown": []}


def test_acknowledging_twice_is_fine_the_second_is_unknown(api):
    api.say()
    [item] = api.fetch().json()["items"]

    first = api.ack([item["id"]])
    second = api.ack([item["id"]])

    assert first.json() == {"acked": 1, "unknown": []}
    assert (second.status_code, second.json()) == (200, {"acked": 0, "unknown": [item["id"]]})


def test_other_projects_and_made_up_ids_are_unknown_and_nothing_happens(api):
    api.hub.add("beta", guaranteed=1, cap=2)
    api.say("给 beta", to="beta")
    [theirs] = api.fetch(as_="beta").json()["items"]

    response = api.ack([theirs["id"], "m_00000000"])

    assert response.status_code == 200
    assert response.json() == {"acked": 0, "unknown": [theirs["id"], "m_00000000"]}
    row = api.hub.db.conn().execute("SELECT state FROM inbox WHERE id = ?", (theirs["id"],))
    assert row.fetchone()["state"] == "leased"


def test_a_message_that_was_never_fetched_is_unknown(api):
    item_id = api.say()

    response = api.ack([item_id])

    assert response.json() == {"acked": 0, "unknown": [item_id]}
    assert states(api) == ["queued"]


def test_expired_and_dropped_messages_are_unknown_and_stay_as_they_are(make_api):
    api = make_api(tweak(Config(), inbox={"max_attempts": 1}))
    api.hub.ready()
    expired = api.say("过期")
    api.hub.clock.advance(11 * 60)  # 超过保留期
    assert api.fetch().json()["items"] == []  # 取话时现判断：标成"过期"
    fresh = api.say("会被作废")
    api.fetch()
    api.hub.clock.advance(61)
    api.services.inbox.cleanup()  # 租满了次数的话由清理作废
    assert states(api) == ["expired", "dropped"]

    response = api.ack([expired, fresh])

    assert response.json() == {"acked": 0, "unknown": [expired, fresh]}
    assert states(api) == ["expired", "dropped"]


def test_the_same_id_written_twice_is_counted_once(api):
    api.say()
    [item] = api.fetch().json()["items"]

    response = api.ack([item["id"], item["id"]])

    assert response.json() == {"acked": 1, "unknown": []}


def test_fifty_ids_are_fine_and_fifty_one_are_refused_without_acknowledging_any(api):
    api.say()
    [item] = api.fetch().json()["items"]
    fillers = [f"m_{n:08x}" for n in range(50)]

    refused = api.ack([item["id"], *fillers])
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "bad_request"
    assert states(api) == ["leased"]  # 一条也没有确认

    accepted = api.ack([item["id"], *fillers[:49]])
    assert accepted.status_code == 200
    assert accepted.json()["acked"] == 1


def test_repeated_ids_count_toward_the_fifty(api):
    assert api.ack(["m_00000000"] * 50).status_code == 200
    assert api.ack(["m_00000000"] * 51).status_code == 400


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"ids": None},
        {"ids": "m_00000000"},
        {"ids": 5},
        {"ids": {"m_00000000": 1}},
        {"ids": [1]},
        {"ids": [None]},
        {"ids": [["m_00000000"]]},
        {"ids": ["m_00000000", 2]},
        {"ids": [True]},
        {"id": ["m_00000000"]},
    ],
    ids=repr,
)
def test_ids_that_are_not_a_list_of_strings_are_refused(api, body):
    api.say()
    [item] = api.fetch().json()["items"]

    response = api.post("/v1/inbox/ack", json=body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    assert states(api) == ["leased"]


def test_an_id_that_cannot_be_encoded_is_refused(api):
    response = api.post(
        "/v1/inbox/ack",
        content=b'{"ids": ["\\ud800"]}',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400


@pytest.mark.parametrize("content", [b"", b"not json", b"[]", b'"ids"', b"null", b"5"])
def test_a_body_that_is_not_a_json_object_is_refused(api, content):
    response = api.post(
        "/v1/inbox/ack", content=content, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_a_body_over_64_kib_is_413(api):
    response = api.post("/v1/inbox/ack", content=b"x" * (64 * 1024 + 1))

    assert (response.status_code, response.json()["error"]["code"]) == (413, "too_large")


def test_a_push_only_project_may_acknowledge_but_everything_is_unknown(api):
    api.hub.add("pusher", push_only=True)

    response = api.ack(["m_00000000"], as_="pusher")

    assert (response.status_code, response.json()) == (
        200,
        {"acked": 0, "unknown": ["m_00000000"]},
    )


def test_acknowledging_needs_a_key_and_the_right_method(api):
    assert api.ack([], as_=None).status_code == 401
    wrong_method = api.get("/v1/inbox/ack")
    assert (wrong_method.status_code, wrong_method.headers["Allow"]) == (405, "POST")


def test_a_disabled_project_cannot_acknowledge(api):
    api.hub.projects.disable("alpha")

    response = api.ack([])

    assert (response.status_code, response.json()["error"]["code"]) == (403, "project_disabled")


def test_an_acknowledgement_does_not_hand_out_the_credential(api):
    api.say(token="ctx-secret-xyz")
    [item] = api.fetch().json()["items"]

    response = api.ack([item["id"]])

    assert "ctx-secret-xyz" not in response.text
