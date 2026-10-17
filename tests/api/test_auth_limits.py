"""认证、认证失败的限流、请求限速、请求体大小、谁能连。规格：`hub-projects`「项目密钥」「请求限速和大小」
「项目之间互相看不到」，`hub-deploy`「谁能连」。

检查的先后（设计第 10 节）：谁能连 → 来源地址的失败限流（只对局域网来源）→ 认证 → 项目的请求
限速（只对发消息和回复）→ 请求体大小 → 内容。
"""

from __future__ import annotations

import pytest

from clawbot_hub.config import Config
from tests.api.conftest import LAN, LOCAL, OUTSIDE
from tests.support.hub import tweak

MAX_BODY = 64 * 1024


def wrong(number: int = 0) -> str:
    """格式对、没登记过的密钥（每个号码一把）。"""
    return "chub_" + f"{number:043d}"


def guess(api, source: str, times: int, *, start: int = 0, step: float = 0, **kw):
    """从 `source` 用错误的密钥请求 `times` 次，每次之间让假时钟走 `step` 秒；返回每次的响应。"""
    responses = []
    for number in range(times):
        responses.append(api.request("GET", "/v1/status", key=wrong(start + number), source=source))
        if step:
            api.hub.clock.advance(step)
    return responses


def error_code(response) -> str:
    return response.json()["error"]["code"]


def body_of_size(size: int) -> bytes:
    """恰好 `size` 个字节、内容合法的发消息请求体（用一个会被忽略的字段垫长）。"""
    base = '{"text": "你好", "pad": ""}'.encode()
    return base.replace(b'"pad": ""', b'"pad": "' + b"x" * (size - len(base)) + b'"')


def send_raw(api, body: bytes, **kw):
    return api.post(
        "/v1/messages", content=body, headers={"Content-Type": "application/json"}, **kw
    )


# ---------------------------------------------------------------- 认证


def test_a_registered_key_gets_in(api):
    assert api.get("/v1/status").status_code == 200


def test_the_scheme_word_is_not_case_sensitive(api):
    response = api.request("GET", "/v1/status", headers={"Authorization": f"bearer {api.key()}"})

    assert response.status_code == 200


VARIANTS = {
    "no header": lambda key: {},
    "empty header": lambda key: {"Authorization": ""},
    "scheme only": lambda key: {"Authorization": "Bearer"},
    "scheme and space": lambda key: {"Authorization": "Bearer "},
    "other scheme": lambda key: {"Authorization": f"Basic {key}"},
    "no scheme": lambda key: {"Authorization": key},
    "token scheme": lambda key: {"Authorization": f"Token {key}"},
    "two spaces": lambda key: {"Authorization": f"Bearer  {key}"},
    "trailing word": lambda key: {"Authorization": f"Bearer {key} extra"},
    "one char short": lambda key: {"Authorization": f"Bearer {key[:-1]}"},
    "one char more": lambda key: {"Authorization": f"Bearer {key}x"},
    "wrong prefix": lambda key: {"Authorization": f"Bearer chux_{key[5:]}"},
    "upper case": lambda key: {"Authorization": f"Bearer {key.upper()}"},
    "key in query only": lambda key: {},
}


@pytest.mark.parametrize("variant", list(VARIANTS))
def test_a_request_without_a_proper_key_is_401(api, variant):
    response = api.request("GET", "/v1/status", headers=VARIANTS[variant](api.key()))

    assert response.status_code == 401
    assert error_code(response) == "unauthorized"


def test_the_key_in_the_query_is_not_a_key(api):
    response = api.request("GET", f"/v1/status?key={api.key()}&token={api.key()}")

    assert response.status_code == 401


def test_every_kind_of_wrong_key_gets_the_same_answer(api):
    beta = api.hub.add("beta", guaranteed=1, cap=2)
    api.hub.projects.disable("beta")
    api.hub.projects.remove("beta", yes=True)
    gone = api.key(beta.name)

    answers = {
        (r.status_code, r.text)
        for r in (
            api.request("GET", "/v1/status"),
            api.request("GET", "/v1/status", key=wrong()),
            api.request("GET", "/v1/status", key="chub_short"),
            api.request("GET", "/v1/status", key=api.key()[:-1]),
            api.request("GET", "/v1/status", key=gone),  # 已移除项目的密钥
        )
    }

    assert len(answers) == 1
    assert next(iter(answers))[0] == 401


@pytest.mark.parametrize(
    "guess_",
    [
        "internal:_hub",
        "internal:_import",
        "_hub",
        "_import",
        "chub__hub",
        "chub_" + "_" * 43,
        "chub_" + "0" * 43,
        "removed:1",
        "removed:3",
    ],
)
def test_the_internal_names_cannot_be_logged_in_as(api, guess_):
    response = api.request("GET", "/v1/status", key=guess_)

    assert response.status_code == 401
    assert error_code(response) == "unauthorized"


def test_a_disabled_project_is_403_and_the_others_are_fine(api):
    api.hub.add("beta", guaranteed=1, cap=2)
    api.hub.projects.disable("alpha")

    status = api.get("/v1/status")
    send = api.send("你好")

    assert (status.status_code, send.status_code) == (403, 403)
    assert error_code(status) == error_code(send) == "project_disabled"
    assert api.get("/v1/status", as_="beta").status_code == 200
    assert api.hub.upstream.calls == []


def test_a_project_enabled_again_works_at_once(api):
    api.hub.projects.disable("alpha")
    assert api.get("/v1/status").status_code == 403

    api.hub.projects.enable("alpha")

    assert api.get("/v1/status").status_code == 200


def test_a_removed_projects_key_is_401_like_any_unknown_key(api):
    api.hub.projects.disable("alpha")
    api.hub.projects.remove("alpha", yes=True)

    response = api.get("/v1/status")

    assert response.status_code == 401


def test_the_old_key_works_during_its_grace_and_not_after(api):
    old = api.key()
    new = api.hub.projects.rotate("alpha", grace_minutes=10, yes=True).key

    during = api.request("GET", "/v1/status", key=old)
    api.hub.clock.advance(10 * 60)
    after = api.request("GET", "/v1/status", key=old)

    assert during.status_code == 200
    assert after.status_code == 401
    assert api.request("GET", "/v1/status", key=new).status_code == 200


def test_a_good_request_updates_when_the_project_was_last_seen(api):
    api.hub.clock.advance(5)

    api.get("/v1/status")

    assert api.hub.projects.get("alpha").last_seen_ms == api.hub.clock.now_ms()


def test_a_bad_key_leaves_nobody_seen(api):
    before = api.hub.projects.get("alpha").last_seen_ms
    api.hub.clock.advance(5)

    api.request("GET", "/v1/status", key=wrong())

    assert api.hub.projects.get("alpha").last_seen_ms == before


def test_the_keys_of_two_projects_do_not_open_each_others_things(api):
    api.hub.add("beta", guaranteed=1, cap=2)

    api.send("来自 alpha")
    api.send("来自 beta", as_="beta")

    rows = {row["project_id"] for row in api.hub.rows()}
    assert len(rows) == 2  # 各记各的


# ---------------------------------------------------------------- 认证失败的限流（只对局域网来源）


def test_ten_wrong_keys_and_the_next_request_is_turned_away(api):
    results = guess(api, LAN, 10)
    assert [r.status_code for r in results] == [401] * 10

    response = api.get("/v1/status", source=LAN)  # 这回用对的密钥

    assert response.status_code == 429
    assert error_code(response) == "too_many_failures"
    assert response.headers["Retry-After"] == "60"


def test_nine_wrong_keys_are_still_fine(api):
    guess(api, LAN, 9)

    assert api.get("/v1/status", source=LAN).status_code == 200


def test_the_wait_is_counted_from_the_oldest_failure(api):
    guess(api, LAN, 10, step=1)  # 第 0、1、……、9 秒各一次，现在是第 10 秒

    response = api.get("/v1/status", source=LAN)

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "50"


def test_the_address_is_let_in_when_the_oldest_failure_is_a_minute_old(api):
    guess(api, LAN, 10, step=1)
    api.hub.clock.advance(49)  # 现在是第 59 秒
    still = api.get("/v1/status", source=LAN)
    api.hub.clock.advance(1)  # 第 60 秒：第 0 秒那次满 1 分钟了

    later = api.get("/v1/status", source=LAN)

    assert (still.status_code, still.headers["Retry-After"]) == (429, "1")
    assert later.status_code == 200


def test_requests_turned_away_are_not_counted_as_failures(api):
    guess(api, LAN, 10)
    api.hub.clock.advance(30)
    during = guess(api, LAN, 12, start=100)  # 被挡住期间继续猜
    assert {r.status_code for r in during} == {429}
    api.hub.clock.advance(30)  # 最早的 10 次失败满 1 分钟

    after = api.get("/v1/status", source=LAN)

    assert after.status_code == 200  # 如果被挡住的那 12 次也算失败，这里还在被挡着


def test_the_wait_does_not_grow_while_the_address_keeps_trying(api):
    guess(api, LAN, 10)
    api.hub.clock.advance(20)

    first = api.get("/v1/status", source=LAN)
    api.hub.clock.advance(10)
    second = api.get("/v1/status", source=LAN)

    assert (first.headers["Retry-After"], second.headers["Retry-After"]) == ("40", "30")


def test_failures_a_minute_apart_do_not_add_up(api):
    guess(api, LAN, 9)
    api.hub.clock.advance(61)
    guess(api, LAN, 9, start=50)

    assert api.get("/v1/status", source=LAN).status_code == 200


def test_the_recovered_address_is_counted_afresh(api):
    guess(api, LAN, 10)
    api.hub.clock.advance(60)

    again = guess(api, LAN, 10, start=200)

    assert {r.status_code for r in again} == {401}
    assert api.get("/v1/status", source=LAN).status_code == 429


def test_each_address_has_its_own_count(api):
    guess(api, LAN, 10)

    assert api.get("/v1/status", source="192.168.1.21").status_code == 200
    assert api.get("/v1/status", source=LAN).status_code == 429


def test_one_address_written_two_ways_is_one_address(api):
    guess(api, "192.168.1.20", 5)
    guess(api, "::ffff:192.168.1.20", 5, start=50)

    assert api.get("/v1/status", source="192.168.1.20").status_code == 429
    assert api.get("/v1/status", source="::ffff:192.168.1.20").status_code == 429


def test_sources_that_are_not_addresses_are_counted_together(api):
    guess(api, "testclient", 10)

    assert api.get("/v1/status", source="some-other-name").status_code == 429


@pytest.mark.parametrize("source", ["127.0.0.1", "127.8.9.10", "::1", "::ffff:127.0.0.1"])
def test_the_local_machine_is_never_limited(api, source):
    results = guess(api, source, 25)

    assert {r.status_code for r in results} == {401}  # 一次 429 都没有
    assert api.get("/v1/status", source=source).status_code == 200


def test_a_local_project_failing_twenty_times_does_not_block_another(api):
    api.hub.add("beta", guaranteed=1, cap=2)

    results = guess(api, LOCAL, 20)

    assert [r.status_code for r in results] == [401] * 20
    assert api.get("/v1/status", as_="beta", source=LOCAL).status_code == 200


@pytest.mark.parametrize(
    "forged",
    [
        {"X-Forwarded-For": LAN},
        {"X-Forwarded-For": f"{LAN}, 10.0.0.1"},
        {"X-Real-IP": LAN},
        {"Forwarded": f"for={LAN}"},
        {"X-Forwarded-For": LAN, "X-Forwarded-Proto": "https"},
    ],
    ids=["xff", "xff-chain", "real-ip", "forwarded", "xff-proto"],
)
def test_forwarding_headers_do_not_move_a_local_request_to_the_lan(api, forged):
    results = [
        api.request("GET", "/v1/status", key=wrong(n), source=LOCAL, headers=forged)
        for n in range(15)
    ]

    assert {r.status_code for r in results} == {401}


@pytest.mark.parametrize(
    "forged",
    [{"X-Forwarded-For": "127.0.0.1"}, {"X-Real-IP": "::1"}, {"Forwarded": "for=127.0.0.1"}],
    ids=["xff", "real-ip", "forwarded"],
)
def test_forwarding_headers_do_not_make_a_lan_request_local(api, forged):
    for number in range(10):
        api.request("GET", "/v1/status", key=wrong(number), source=LAN, headers=forged)

    response = api.request("GET", "/v1/status", key=api.key(), source=LAN, headers=forged)

    assert response.status_code == 429


def test_a_blocked_address_is_turned_away_before_the_key_is_looked_at(api):
    api.hub.add("beta", guaranteed=1, cap=2)
    api.hub.projects.disable("beta")
    guess(api, LAN, 10)

    answers = [
        api.request("GET", "/v1/status", source=LAN),  # 没带密钥
        api.get("/v1/status", as_="beta", source=LAN),  # 已停用项目的密钥
        api.get("/v1/status", source=LAN),  # 对的密钥
        api.send("你好", source=LAN),
    ]

    assert [r.status_code for r in answers] == [429] * 4
    assert api.hub.upstream.calls == []


def test_a_disabled_projects_right_key_is_not_a_guess(api):
    api.hub.projects.disable("alpha")

    results = [api.get("/v1/status", source=LAN) for _ in range(15)]

    assert {r.status_code for r in results} == {403}


def test_the_health_check_and_unknown_paths_do_not_take_part(api):
    guess(api, LAN, 10)

    assert api.get("/v1/health", as_=None, source=LAN).status_code == 200
    assert api.get("/no/such/thing", as_=None, source=LAN).status_code == 404


def test_the_health_check_never_counts_as_a_failure(api):
    for number in range(15):
        response = api.request("GET", "/v1/health", key=wrong(number), source=LAN)
        assert response.status_code == 200

    assert api.get("/v1/status", source=LAN).status_code == 200


def test_a_missing_key_counts_as_a_failure_too(api):
    for _ in range(10):
        assert api.request("GET", "/v1/status", source=LAN).status_code == 401

    assert api.get("/v1/status", source=LAN).status_code == 429


def test_a_good_request_does_not_wipe_the_failures(api):
    guess(api, LAN, 9)
    assert api.get("/v1/status", source=LAN).status_code == 200
    guess(api, LAN, 1, start=60)

    assert api.get("/v1/status", source=LAN).status_code == 429


# ---------------------------------------------------------------- 请求限速


def test_the_31st_request_in_a_minute_is_turned_away(api):
    results = [api.send("") for _ in range(30)]  # 空消息：400，但认证通过的请求都计数
    assert {r.status_code for r in results} == {400}

    response = api.send("你好")

    assert response.status_code == 429
    assert error_code(response) == "rate_limited"
    assert response.headers["Retry-After"] == "60"
    assert api.hub.upstream.calls == []


def test_real_sends_are_counted_whatever_became_of_them(api):
    statuses = [api.send("你好").json()["status"] for _ in range(30)]
    assert statuses.count("sent") == 8  # 上限 8 条，后面的被额度挡住
    assert set(statuses) == {"sent", "rejected"}

    assert api.send("你好").status_code == 429


def test_the_wait_is_counted_from_the_oldest_request(api):
    for _ in range(30):
        api.send("")
        api.hub.clock.advance(1)  # 第 0 到 29 秒各一次，现在是第 30 秒

    assert api.send("你好").headers["Retry-After"] == "30"


def test_the_window_slides(api):
    for _ in range(15):
        api.send("")
    api.hub.clock.advance(30)
    for _ in range(15):
        api.send("")
    api.hub.clock.advance(29)
    assert api.send("").status_code == 429  # 第一批要到第 60 秒才满 1 分钟

    api.hub.clock.advance(1)

    results = [api.send("").status_code for _ in range(16)]
    assert results == [400] * 15 + [429]  # 第一批的 15 次腾出来了


def test_requests_turned_away_leave_no_trace(api):
    for _ in range(30):
        api.send("")
    used, rows = api.hub.used(), api.hub.rows()
    api.hub.clock.advance(20)

    limited = [api.send("你好") for _ in range(3)]

    assert [r.status_code for r in limited] == [429] * 3
    assert api.hub.used() == used and api.hub.rows() == rows
    assert api.hub.upstream.calls == []
    assert limited[-1].headers["Retry-After"] == "40"  # 没有被这 3 次推后
    api.hub.clock.advance(40)
    assert api.send("你好").status_code == 200


def test_other_projects_are_not_limited(api):
    api.hub.add("beta", guaranteed=1, cap=2)
    for _ in range(30):
        api.send("")

    assert api.send("你好").status_code == 429
    assert api.send("你好", as_="beta").status_code == 200


def test_the_limit_is_per_project_not_per_address(api):
    api.hub.add("beta", guaranteed=1, cap=2)
    for _ in range(30):
        assert api.send("", source=LAN).status_code == 400
        assert api.send("", as_="beta", source=LAN).status_code == 400

    assert api.send("你好", source=LAN).status_code == 429
    assert api.send("你好", as_="beta", source=LAN).status_code == 429


def test_the_limit_is_configurable(make_api):
    api = make_api(tweak(Config(), send={"rate_per_minute": 3}))
    api.hub.ready()

    assert [api.send("").status_code for _ in range(4)] == [400, 400, 400, 429]


def test_status_and_health_are_not_counted(api):
    for _ in range(60):
        api.get("/v1/status")
        api.get("/v1/health", as_=None)

    assert api.send("你好").status_code == 200


def test_requests_without_a_proper_key_do_not_use_up_a_projects_requests(api):
    for number in range(40):
        assert (
            api.request("POST", "/v1/messages", key=wrong(number), json={"text": "x"}).status_code
            == 401
        )

    assert [api.send("").status_code for _ in range(30)] == [400] * 30


def test_every_authenticated_request_counts_whatever_became_of_it(api):
    big = body_of_size(MAX_BODY + 1)
    for _ in range(10):
        assert send_raw(api, big).status_code == 413
    for _ in range(10):
        assert api.post("/v1/messages", content=b"not json").status_code == 400
    for _ in range(10):
        assert api.send("你好").status_code == 200

    assert api.send("你好").status_code == 429


def test_a_disabled_projects_requests_are_not_counted(api):
    api.hub.projects.disable("alpha")
    for _ in range(40):
        assert api.send("").status_code == 403
    api.hub.projects.enable("alpha")

    assert [api.send("").status_code for _ in range(30)] == [400] * 30


# ---------------------------------------------------------------- 检查的先后


def test_the_blocked_address_comes_before_authentication_and_the_rate(api):
    for _ in range(30):
        api.send("", source=LAN)
    guess(api, LAN, 10)

    response = api.send("你好", source=LAN)

    assert error_code(response) == "too_many_failures"


def test_authentication_comes_before_the_rate_limit(api):
    for _ in range(30):
        api.send("")

    response = api.request("POST", "/v1/messages", key=wrong(), json={"text": "你好"})

    assert response.status_code == 401


def test_the_rate_limit_comes_before_the_size(api):
    for _ in range(30):
        api.send("")

    response = send_raw(api, body_of_size(MAX_BODY + 1))

    assert response.status_code == 429
    assert error_code(response) == "rate_limited"


def test_authentication_comes_before_the_size(api):
    response = api.request(
        "POST",
        "/v1/messages",
        key=wrong(),
        content=body_of_size(MAX_BODY + 1),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 401


def test_the_size_comes_before_the_content(api):
    response = api.post("/v1/messages", content=b"\xff" * (MAX_BODY + 1))

    assert response.status_code == 413  # 不是 400


# ---------------------------------------------------------------- 请求体大小


def test_a_body_of_exactly_64_kib_is_read(api):
    body = body_of_size(MAX_BODY)
    assert len(body) == MAX_BODY

    response = send_raw(api, body)

    assert response.status_code == 200
    assert response.json()["status"] == "sent"


def test_one_byte_more_is_too_large(api):
    body = body_of_size(MAX_BODY + 1)
    assert len(body) == MAX_BODY + 1

    response = send_raw(api, body)

    assert response.status_code == 413
    assert error_code(response) == "too_large"
    assert list(response.json()) == ["error"]


def test_a_body_that_is_too_large_leaves_nothing_behind(api):
    send_raw(api, body_of_size(MAX_BODY * 3))

    assert api.hub.rows() == []
    assert api.hub.used() == {}
    assert api.hub.upstream.calls == []


def test_a_chunked_body_is_counted_as_it_comes(api):
    def chunks(total: int):
        for _ in range(total // 1024):
            yield b"x" * 1024

    over = api.post("/v1/messages", content=chunks(MAX_BODY + 1024))  # 没有 Content-Length
    under = api.post("/v1/messages", content=chunks(MAX_BODY))

    assert "content-length" not in over.request.headers
    assert over.status_code == 413
    assert under.status_code == 400  # 64 KiB 以内：读完了，只是不是 JSON


def test_the_declared_length_is_not_believed(api):
    body = body_of_size(MAX_BODY + 4096)

    response = api.post(
        "/v1/messages",
        content=body,
        headers={"Content-Length": "100", "Content-Type": "application/json"},
    )

    assert response.status_code == 413


def test_the_error_does_not_repeat_the_body(api):
    body = b'{"text": "SECRET-PAYLOAD-77", "pad": "' + b"x" * MAX_BODY + b'"}'

    response = send_raw(api, body)

    assert "SECRET-PAYLOAD-77" not in response.text


# ---------------------------------------------------------------- 项目之间互相看不到


def test_projects_cannot_see_each_others_numbers(api):
    api.hub.add("beta", prefix="贝塔", guaranteed=2, cap=3)
    api.send("来自 alpha")

    beta = api.get("/v1/status", as_="beta").json()

    assert beta["quota"]["project"]["used"] == 0
    assert beta["quota"]["used"] == 1  # 窗口的总用量是大家共有的信息
    assert "alpha" not in api.get("/v1/status", as_="beta").text


# ---------------------------------------------------------------- 谁能连（局域网开关）


@pytest.fixture
def lan_api(make_api):
    api = make_api(tweak(Config(), server={"lan": True}))
    api.hub.ready()
    return api


PRIVATE_SOURCES = [
    "127.0.0.1",
    "::1",
    "10.1.2.3",
    "172.16.0.9",
    "172.31.255.1",
    "192.168.1.20",
    "169.254.7.7",
    "fc00::5",
    "fd00::5",
    "fe80::5",
    "::ffff:192.168.1.20",
]
OUTSIDE_SOURCES = [
    OUTSIDE,
    "8.8.8.8",
    "172.15.0.1",
    "172.32.0.1",
    "192.169.0.1",
    "100.64.0.1",
    "2001:db8::1",
    "::ffff:8.8.8.8",
    "not-an-address",
]


@pytest.mark.parametrize("source", PRIVATE_SOURCES)
def test_the_lan_switch_lets_the_local_machine_and_private_networks_in(lan_api, source):
    assert lan_api.get("/v1/status", source=source).status_code == 200
    assert lan_api.get("/v1/health", as_=None, source=source).status_code == 200


@pytest.mark.parametrize("source", OUTSIDE_SOURCES)
def test_the_lan_switch_keeps_everyone_else_out_with_an_empty_403(lan_api, source):
    for method, path, key in [
        ("GET", "/v1/status", lan_api.key()),
        ("GET", "/v1/status", None),
        ("GET", "/v1/health", None),
        ("POST", "/v1/messages", lan_api.key()),
        ("GET", "/no/such/thing", None),
        ("DELETE", "/v1/messages", lan_api.key()),
    ]:
        response = lan_api.request(method, path, key=key, source=source)

        assert response.status_code == 403, (method, path)
        assert response.content == b""
    assert lan_api.hub.upstream.calls == []


def test_forged_forwarding_headers_do_not_get_a_stranger_in(lan_api):
    forged = {
        "X-Forwarded-For": LAN,
        "X-Real-IP": LAN,
        "Forwarded": f"for={LAN}",
        "X-Forwarded-Host": "192.168.1.1",
    }

    response = lan_api.get("/v1/status", source=OUTSIDE, headers=forged)

    assert response.status_code == 403
    assert response.content == b""


def test_a_stranger_leaves_no_failures_behind(lan_api):
    for number in range(15):
        lan_api.request("GET", "/v1/status", key=wrong(number), source=OUTSIDE)

    assert lan_api.get("/v1/status", source=LAN).status_code == 200


def test_the_default_switch_does_not_look_at_the_source(api):
    # 默认只监听 127.0.0.1，连得进来的本来就只有本机；应用这一层不再另外查
    assert api.get("/v1/status", source=OUTSIDE).status_code == 200
