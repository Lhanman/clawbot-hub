"""至少送达一次的取话循环（任务 D4，规格 `hub-client`「至少送达一次的取话循环」）。

循环里的每一次取话、确认、心跳都只发一次请求（不叠加客户端自己的重试），所以假服务收到的失败请求
数就是退避的次数。睡觉走 `sleep` 参数，测试里只记录、不真睡；心跳用后台线程和真的时间，参数传很小
的数。
和真服务一起的"租期后再给一次""慢指令期间保持在线"在 D5 的 `tests/contract/`。
"""

from __future__ import annotations

import logging
import threading
import time

import clawbot_hub_client as hub_client
import pytest

from tests.client.conftest import KEY, FakeHub, error_body, inbound, inbox_body, status_body
from tests.support.fakehttp import Reply, close, default_after, reset, truncated

pytestmark = pytest.mark.usefixtures("pauses")

HEARTBEAT_THREAD = "clawbot-hub-heartbeat"


def client_for(hub, **kwargs) -> hub_client.HubClient:
    return hub_client.HubClient(hub.base_url, KEY, **kwargs)


def batch(*ids: str) -> Reply:
    return Reply(body=inbox_body(*[inbound(i, f"话 {i}") for i in ids]))


def err(status: int, code: str = "x", message: str = "原因", **headers: str) -> Reply:
    return Reply(status=status, body=error_body(code, message), headers=headers)


def after_acks(hub, count: int = 1):
    return lambda: len(hub.requests_to("ack")) >= count


def after_fetches(hub, count: int):
    return lambda: len(hub.fetches()) >= count


def ack_ids(hub) -> list[list[str]]:
    return [r.json["ids"] for r in hub.requests_to("ack")]


def run_loop(hub, handler, *, stop, client=None, **kwargs) -> list[float]:
    """跑 `run_inbox` 直到 `stop`，返回循环里睡过的每一次。默认的心跳间隔很大，不发心跳。"""
    sleeps: list[float] = []
    kwargs.setdefault("heartbeat_seconds", 3600.0)
    (client or client_for(hub)).run_inbox(handler, stop=stop, sleep=sleeps.append, **kwargs)
    return sleeps


def heartbeat_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == HEARTBEAT_THREAD]


# ---------------------------------------------------------------- 取话、处理、确认


def test_a_batch_is_handled_in_order_and_then_acknowledged_together(hub):
    hub.script("inbox", batch("in_1", "in_2", "in_3"))
    seen = []

    sleeps = run_loop(hub, lambda item: seen.append((item.id, item.text)), stop=after_acks(hub))

    assert seen == [("in_1", "话 in_1"), ("in_2", "话 in_2"), ("in_3", "话 in_3")]
    assert ack_ids(hub) == [["in_1", "in_2", "in_3"]]  # 处理完才确认，一次确认一批
    assert sleeps == []  # 有话就不睡
    assert hub.requests_to("status") == []  # 给了心跳间隔就不用去问服务


def test_the_ack_comes_after_every_item_of_the_batch_was_handled(hub):
    hub.script("inbox", batch("in_1", "in_2"))
    order = []

    def handler(item):
        order.append(("handled", item.id, len(hub.requests_to("ack"))))

    run_loop(hub, handler, stop=after_acks(hub))

    assert order == [("handled", "in_1", 0), ("handled", "in_2", 0)]


def test_the_handler_gets_inbound_objects_in_the_callers_thread(hub):
    hub.script("inbox", batch("in_1"))
    seen = []

    run_loop(hub, lambda item: seen.append((item, threading.get_ident())), stop=after_acks(hub))

    [(item, thread)] = seen
    assert isinstance(item, hub_client.Inbound) and item.reply_handle == "h_in_1"
    assert thread == threading.get_ident()


@pytest.mark.parametrize("returned", [None, False, 0, "", "done", {"a": 1}])
def test_whatever_the_handler_returns_a_normal_return_means_handled(hub, returned):
    hub.script("inbox", batch("in_1"))

    run_loop(hub, lambda item: returned, stop=after_acks(hub))

    assert ack_ids(hub) == [["in_1"]]


def test_wait_and_max_are_passed_to_every_fetch(hub):
    hub.script("inbox", batch("in_1"), batch("in_2"))

    run_loop(hub, lambda item: None, stop=after_acks(hub, 2), wait=3, max=7)

    assert [r.target for r in hub.fetches()] == ["/v1/inbox?wait=3&max=7"] * 2


def test_the_defaults_ask_for_25_seconds_and_10_items(hub):
    hub.script("inbox", batch("in_1"))

    run_loop(hub, lambda item: None, stop=after_acks(hub))

    assert hub.fetches()[0].target == "/v1/inbox?wait=25&max=10"


def test_run_inbox_returns_nothing_when_stopped(hub):
    result = hub_client.HubClient(hub.base_url, KEY).run_inbox(
        lambda item: None, stop=lambda: True, sleep=lambda s: None, heartbeat_seconds=1
    )

    assert result is None


def test_stop_already_true_means_no_request_at_all(hub):
    run_loop(hub, lambda item: None, stop=lambda: True)

    assert hub.requests == []


def test_an_item_that_comes_again_is_handled_again(hub):
    """至少送达一次：租期过了没确认的话会再给，客户端不替项目去重（处理要幂等）。"""
    hub.script(
        "inbox",
        Reply(body=inbox_body(inbound("in_1", attempt=1))),
        Reply(body=inbox_body(inbound("in_1", attempt=2))),
    )
    attempts = []

    run_loop(hub, lambda item: attempts.append(item.attempt), stop=after_acks(hub, 2))

    assert attempts == [1, 2] and ack_ids(hub) == [["in_1"], ["in_1"]]


def test_a_batch_bigger_than_the_ack_limit_is_acknowledged_in_pieces(hub):
    ids = [f"in_{n}" for n in range(60)]
    hub.script("inbox", batch(*ids))

    run_loop(hub, lambda item: None, stop=after_acks(hub, 2), max=60)

    assert [len(batch_ids) for batch_ids in ack_ids(hub)] == [50, 10]
    assert [i for batch_ids in ack_ids(hub) for i in batch_ids] == ids


# ---------------------------------------------------------------- 处理失败


def test_an_item_whose_handler_raised_is_not_acknowledged_and_the_rest_goes_on(hub, caplog):
    hub.script("inbox", batch("in_1", "in_2", "in_3"))
    handled = []

    def handler(item):
        handled.append(item.id)
        if item.id == "in_2":
            raise ValueError("这一句处理不了")

    caplog.set_level(logging.WARNING, logger="clawbot_hub_client")
    run_loop(hub, handler, stop=after_acks(hub))

    assert handled == ["in_1", "in_2", "in_3"]  # 第 2 句失败了，第 3 句照常处理
    assert ack_ids(hub) == [["in_1", "in_3"]]
    [record] = [r for r in caplog.records if r.name == "clawbot_hub_client"]
    assert record.levelno == logging.WARNING and "in_2" in record.getMessage()


def test_the_log_of_a_failed_item_has_the_error_type_but_not_the_text_or_the_message(hub, caplog):
    hub.script("inbox", Reply(body=inbox_body(inbound("in_1", "使用者的私密内容-9f3"))))

    def handler(item):
        raise RuntimeError(f"没法处理：{item.text}")

    caplog.set_level(logging.WARNING, logger="clawbot_hub_client")
    run_loop(hub, handler, stop=after_fetches(hub, 1))

    assert "RuntimeError" in caplog.text and "in_1" in caplog.text
    assert "使用者的私密内容-9f3" not in caplog.text and KEY not in caplog.text


def test_the_full_traceback_is_there_for_whoever_turns_on_debug(hub, caplog):
    hub.script("inbox", batch("in_1"))

    def handler(item):
        raise RuntimeError("追查用的详细信息")

    caplog.set_level(logging.DEBUG, logger="clawbot_hub_client")
    run_loop(hub, handler, stop=after_fetches(hub, 1))

    debug = [r for r in caplog.records if r.levelno == logging.DEBUG and r.exc_info]
    assert debug and "追查用的详细信息" in caplog.text


def test_a_batch_where_everything_failed_acknowledges_nothing_and_goes_on(hub):
    hub.script("inbox", batch("in_1", "in_2"))

    def handler(item):
        raise ValueError

    run_loop(hub, handler, stop=after_fetches(hub, 2))

    assert hub.requests_to("ack") == [] and len(hub.fetches()) == 2


def test_a_handler_that_fails_because_a_reply_failed_does_not_stop_the_loop(hub):
    hub.script("inbox", batch("in_1", "in_2"))
    hub.script("replies", err(503, "shutting_down"))
    client = client_for(hub)

    def handler(item):
        client.reply(item.reply_handle, "收到")  # 第一句的回复发不出去，抛 HubUnavailable

    run_loop(hub, handler, stop=after_acks(hub), client=client)

    assert ack_ids(hub) == [["in_2"]]


def test_a_keyboard_interrupt_in_the_handler_goes_up_and_stops_the_heartbeat(hub):
    hub.script("inbox", batch("in_1", "in_2"))
    handled = []

    def handler(item):
        handled.append(item.id)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_loop(hub, handler, stop=after_fetches(hub, 3), heartbeat_seconds=0.05)

    assert handled == ["in_1"] and hub.requests_to("ack") == []
    assert heartbeat_threads() == []


# ---------------------------------------------------------------- 连不上：退避


def test_the_wait_after_each_failed_fetch_doubles_and_starts_over_after_a_success(hub, pauses):
    hub.script(
        "inbox",
        err(503, "shutting_down"),
        err(503, "shutting_down"),
        err(503, "shutting_down"),
        batch("in_1"),
        err(503, "shutting_down"),
        batch("in_2"),
    )

    sleeps = run_loop(hub, lambda item: None, stop=after_acks(hub, 2))

    assert sleeps == [5.0, 10.0, 20.0, 5.0]
    assert len(hub.fetches()) == 6  # 每次调用只发一次请求：失败的请求数就是退避的次数
    assert pauses == []  # 客户端层的重试没有出手


def test_the_wait_stops_growing_at_60_seconds(hub):
    hub.script("inbox", *[err(503)] * 7, batch("in_1"))

    sleeps = run_loop(hub, lambda item: None, stop=after_acks(hub))

    assert sleeps == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0, 60.0]


UNAVAILABLE = [
    err(500, "internal"),
    err(502, "bad_gateway"),
    err(503, "shutting_down"),
    err(504, "gateway_timeout"),
    reset(),
    close(),
    truncated(),
]


@pytest.mark.parametrize("failure", UNAVAILABLE, ids=lambda r: f"{r.action}-{r.status}")
def test_every_kind_of_unavailable_makes_the_loop_back_off(hub, failure):
    hub.script("inbox", failure, batch("in_1"))

    sleeps = run_loop(hub, lambda item: None, stop=after_acks(hub))

    assert sleeps == [5.0] and len(hub.fetches()) == 2


def test_a_service_that_is_down_is_tried_again_with_growing_waits_until_it_is_back():
    """规格里的"服务重启时退避"：先把服务关了，退避到第三次睡完再把它在同一个端口开起来。"""
    with FakeHub() as first:
        port = first.port
    second = FakeHub(port=port)  # 同一个端口，还没开
    second.script("inbox", batch("in_1"))
    sleeps: list[float] = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            second.start()

    try:
        hub_client.HubClient(f"http://127.0.0.1:{port}", KEY).run_inbox(
            lambda item: None, stop=after_acks(second), sleep=sleep, heartbeat_seconds=3600
        )
    finally:
        second.stop()

    assert sleeps == [5.0, 10.0, 20.0]  # 三次连不上，之后恢复，取话正常
    assert ack_ids(second) == [["in_1"]]


def test_stop_while_waiting_after_a_failure_ends_the_loop_after_that_wait(hub):
    hub.script("inbox", err(503))

    sleeps = run_loop(hub, lambda item: None, stop=after_fetches(hub, 1))

    assert sleeps == [5.0] and len(hub.fetches()) == 1


def test_the_waits_of_the_loop_go_through_the_sleep_argument_only(hub, pauses):
    hub.script("inbox", err(503), batch("in_1"))
    own_thread = []

    def sleep(seconds):
        own_thread.append(threading.current_thread() is threading.main_thread())

    hub_client.HubClient(hub.base_url, KEY).run_inbox(
        lambda item: None, stop=after_acks(hub), sleep=sleep, heartbeat_seconds=3600
    )

    assert own_thread == [True] and pauses == []


# ---------------------------------------------------------------- 认证和请求错误


@pytest.mark.parametrize(
    ("reply", "code"),
    [
        (err(403, "project_disabled", "这个项目已停用"), "project_disabled"),
        (err(403, "push_only", "只推送的项目不能取话"), "push_only"),
        (err(401, "unauthorized", "密钥不对"), "unauthorized"),
        (Reply(status=403, body=None), None),
    ],
)
def test_a_key_that_is_refused_ends_the_loop_with_an_auth_error(hub, reply, code):
    hub.script("inbox", reply)
    sleeps: list[float] = []

    with pytest.raises(hub_client.HubAuthError) as info:
        client_for(hub).run_inbox(
            lambda item: None,
            stop=after_fetches(hub, 3),
            sleep=sleeps.append,
            heartbeat_seconds=3600,
        )

    assert info.value.code == code
    assert len(hub.fetches()) == 1 and sleeps == []  # 不重试，不睡


def test_no_key_at_all_ends_the_loop_before_any_request(hub, monkeypatch):
    monkeypatch.delenv("CLAWBOT_HUB_KEY", raising=False)

    with pytest.raises(hub_client.HubAuthError):
        hub_client.HubClient(hub.base_url).run_inbox(
            lambda item: None, stop=after_fetches(hub, 3), sleep=lambda s: None
        )

    assert hub.requests == []


def test_a_key_refused_for_the_status_question_still_ends_the_loop_at_the_first_fetch(hub):
    """开始时问服务"几秒算在线"碰上密钥不对：这一问的错不算数，接下来的取话会抛。"""
    hub.script("status", err(401, "unauthorized"))
    hub.script("inbox", err(401, "unauthorized"))

    with pytest.raises(hub_client.HubAuthError):
        client_for(hub).run_inbox(
            lambda item: None, stop=after_fetches(hub, 3), sleep=lambda s: None
        )

    assert len(hub.requests_to("status")) == 1 and len(hub.fetches()) == 1


def test_rate_limiting_sleeps_for_the_time_the_service_asked(hub):
    hub.script("inbox", err(429, "rate_limited", **{"Retry-After": "12"}), batch("in_1"))

    sleeps = run_loop(hub, lambda item: None, stop=after_acks(hub))

    assert sleeps == [12.0] and len(hub.fetches()) == 2


def test_rate_limiting_without_a_retry_after_sleeps_60_seconds(hub):
    hub.script("inbox", err(429, "too_many_failures"), batch("in_1"))

    sleeps = run_loop(hub, lambda item: None, stop=after_acks(hub))

    assert sleeps == [60.0]


def test_a_retry_after_of_zero_still_sleeps_a_second(hub):
    """规格没写 0 的情形：睡 0 秒会变成对着服务空转。"""
    hub.script("inbox", err(429, "rate_limited", **{"Retry-After": "0"}), batch("in_1"))

    sleeps = run_loop(hub, lambda item: None, stop=after_acks(hub))

    assert sleeps == [1.0]


def test_rate_limiting_does_not_count_as_a_failure_for_the_backoff(hub):
    hub.script(
        "inbox", err(503), err(429, "rate_limited", **{"Retry-After": "7"}), err(503), batch("in_1")
    )

    sleeps = run_loop(hub, lambda item: None, stop=after_acks(hub))

    assert sleeps == [5.0, 7.0, 10.0]  # 429 睡 7 秒，退避照常翻倍，不因为 429 重新计


@pytest.mark.parametrize("status", [400, 404, 410, 413])
def test_any_other_request_error_ends_the_loop(hub, status):
    hub.script("inbox", err(status, "bad_request", "出错了"))
    sleeps: list[float] = []

    with pytest.raises(hub_client.HubRequestError) as info:
        client_for(hub).run_inbox(
            lambda item: None,
            stop=after_fetches(hub, 3),
            sleep=sleeps.append,
            heartbeat_seconds=3600,
        )

    assert info.value.http_status == status and sleeps == [] and len(hub.fetches()) == 1


# ---------------------------------------------------------------- 确认失败不丢


def test_acks_that_cannot_reach_the_service_keep_the_ids_and_back_off(hub, pauses):
    hub.script("inbox", batch("in_1", "in_2"))
    hub.script("ack", err(503), err(503))

    sleeps = run_loop(hub, lambda item: None, stop=after_fetches(hub, 2))

    assert ack_ids(hub) == [["in_1", "in_2"]] * 3  # 同一批 id 一直留着，第三次才成功
    assert [r.endpoint for r in hub.requests] == ["inbox", "ack", "ack", "ack", "inbox"]
    assert sleeps == [5.0, 10.0, 1.0]  # 5、10 是确认的退避；最后的 1 是第二次取话立刻回了空列表
    assert pauses == []


@pytest.mark.parametrize("failure", UNAVAILABLE, ids=lambda r: f"{r.action}-{r.status}")
def test_every_kind_of_unavailable_during_the_ack_keeps_the_ids(hub, failure):
    hub.script("inbox", batch("in_1"))
    hub.script("ack", failure)

    sleeps = run_loop(hub, lambda item: None, stop=after_fetches(hub, 2))

    assert ack_ids(hub) == [["in_1"], ["in_1"]]
    assert sleeps[0] == 5.0


def test_the_next_batch_waits_for_the_ack_of_the_one_before(hub):
    hub.script("inbox", batch("in_1"), batch("in_2"))
    hub.script("ack", err(503))

    run_loop(hub, lambda item: None, stop=after_acks(hub, 3))

    assert [r.endpoint for r in hub.requests] == ["inbox", "ack", "ack", "inbox", "ack"]


def test_the_ack_backoff_starts_over_for_the_next_batch(hub):
    hub.script("inbox", batch("in_1"), batch("in_2"))
    hub.script("ack", err(503), Reply(body={"acked": 1, "unknown": []}), err(503))

    sleeps = run_loop(hub, lambda item: None, stop=after_acks(hub, 4))

    assert sleeps == [5.0, 5.0]


def test_ack_failures_and_fetch_failures_share_one_backoff_that_a_success_resets(hub):
    hub.script("inbox", err(503), batch("in_1"), err(503))
    hub.script("ack", err(503), err(503))

    sleeps = run_loop(hub, lambda item: None, stop=after_fetches(hub, 3))

    # 取话失败 5；取话成功重新计，确认失败 5、10；确认成功重新计，取话失败 5
    assert sleeps == [5.0, 5.0, 10.0, 5.0]


def test_an_ack_that_keeps_failing_is_given_up_when_asked_to_stop(hub, caplog):
    hub.script("inbox", batch("in_1"))
    hub.script("ack", *[err(503)] * 10)
    caplog.set_level(logging.WARNING, logger="clawbot_hub_client")

    sleeps = run_loop(hub, lambda item: None, stop=after_acks(hub))

    assert (
        len(hub.requests_to("ack")) == 1 and sleeps == []
    )  # 要停了：不再退避重试，直接放弃这一批的确认


def test_giving_up_the_ack_on_stop_leaves_a_warning_that_the_items_will_come_again(hub, caplog):
    hub.script("inbox", batch("in_1", "in_2"))
    hub.script("ack", err(503))
    caplog.set_level(logging.WARNING, logger="clawbot_hub_client")

    run_loop(hub, lambda item: None, stop=after_acks(hub))

    [record] = [r for r in caplog.records if "租期" in r.getMessage()]
    assert "2 句" in record.getMessage()
    assert KEY not in caplog.text and "话 in_1" not in caplog.text


def test_a_refused_key_during_the_ack_ends_the_loop(hub):
    hub.script("inbox", batch("in_1"))
    hub.script("ack", err(403, "project_disabled"))

    with pytest.raises(hub_client.HubAuthError):
        run_loop(hub, lambda item: None, stop=after_fetches(hub, 3))


def test_a_request_error_during_the_ack_ends_the_loop(hub):
    hub.script("inbox", batch("in_1"))
    hub.script("ack", err(400, "bad_request"))

    with pytest.raises(hub_client.HubRequestError):
        run_loop(hub, lambda item: None, stop=after_fetches(hub, 3))


def test_rate_limiting_during_the_ack_sleeps_and_sends_the_same_ids_again(hub):
    hub.script("inbox", batch("in_1", "in_2"))
    hub.script("ack", err(429, "too_many_failures", **{"Retry-After": "9"}))

    sleeps = run_loop(hub, lambda item: None, stop=after_acks(hub, 2))

    assert sleeps == [9.0] and ack_ids(hub) == [["in_1", "in_2"]] * 2


def test_a_batch_that_was_cut_into_pieces_is_sent_again_whole_after_a_failure(hub):
    """确认是幂等的：第二片失败就整批再来一遍，服务把已经确认的当 unknown 就是了。"""
    ids = [f"in_{n}" for n in range(60)]
    hub.script("inbox", batch(*ids))
    hub.script("ack", Reply(body={"acked": 50, "unknown": []}), err(503))

    run_loop(hub, lambda item: None, stop=after_acks(hub, 4), max=60)

    assert [len(b) for b in ack_ids(hub)] == [50, 10, 50, 10]


# ---------------------------------------------------------------- 空转保护


def test_with_a_wait_of_zero_every_empty_answer_is_followed_by_a_second_of_sleep(hub):
    sleeps = run_loop(hub, lambda item: None, stop=after_fetches(hub, 3), wait=0)

    assert sleeps == [1.0, 1.0, 1.0]


def test_an_empty_answer_that_came_back_at_once_is_followed_by_a_second_of_sleep(hub):
    """被同一项目的另一个取话请求顶掉、或者服务正在关闭，都会很快返回空列表。"""
    sleeps = run_loop(hub, lambda item: None, stop=after_fetches(hub, 2), wait=25)

    assert sleeps == [1.0, 1.0]


def test_a_service_that_waited_the_full_time_is_not_an_error_and_no_sleep_follows(hub):
    hub.script("inbox", default_after(1.2))

    sleeps = run_loop(hub, lambda item: None, stop=after_fetches(hub, 1), wait=2)

    assert sleeps == []


def scripted_clock(monkeypatch, *readings: float) -> None:
    values = iter(readings)

    def now() -> float:
        try:
            return next(values)
        except StopIteration:
            raise AssertionError("循环读钟的次数比预想的多") from None

    monkeypatch.setattr(hub_client, "_monotonic", now)


@pytest.mark.parametrize(
    ("elapsed", "sleeps"),
    [
        (0.0, [1.0]),
        (12.4999, [1.0]),
        (12.5, []),  # 刚好等于 wait 的一半：算服务等满了
        (12.6, []),
        (25.0, []),
        (40.0, []),
    ],
)
def test_the_half_of_the_wait_is_where_an_empty_answer_stops_being_suspicious(
    hub, monkeypatch, elapsed, sleeps
):
    scripted_clock(monkeypatch, 100.0, 100.0 + elapsed)

    assert run_loop(hub, lambda item: None, stop=after_fetches(hub, 1), wait=25) == sleeps


@pytest.mark.parametrize("elapsed", [0.0, 0.5, 30.0, 1000.0])
def test_with_a_wait_of_zero_the_elapsed_time_does_not_matter(hub, monkeypatch, elapsed):
    scripted_clock(monkeypatch, 5.0, 5.0 + elapsed)

    assert run_loop(hub, lambda item: None, stop=after_fetches(hub, 1), wait=0) == [1.0]


@pytest.mark.parametrize(
    ("wait", "elapsed", "sleeps"),
    [
        (1, 0.0, [1.0]),
        (1, 0.4999, [1.0]),
        (1, 0.5, []),
        (1, 1.0, []),
        (2, 0.9999, [1.0]),
        (2, 1.0, []),
    ],
)
def test_a_small_wait_is_judged_by_the_same_half(hub, monkeypatch, wait, elapsed, sleeps):
    """`wait` 是 1 的时候也只有 0 才一律睡：1 秒的等待要看实际等了多久。"""
    scripted_clock(monkeypatch, 100.0, 100.0 + elapsed)

    assert run_loop(hub, lambda item: None, stop=after_fetches(hub, 1), wait=wait) == sleeps


def test_a_batch_with_items_never_triggers_the_idle_sleep(hub):
    hub.script("inbox", batch("in_1"), batch("in_2"), batch("in_3"))

    sleeps = run_loop(hub, lambda item: None, stop=after_acks(hub, 3), wait=0)

    assert sleeps == []


# ---------------------------------------------------------------- 停下来


def test_stop_turning_true_in_the_middle_of_a_batch_finishes_and_acknowledges_the_batch(hub):
    hub.script("inbox", batch("in_1", "in_2"))
    stopped = threading.Event()
    handled = []

    def handler(item):
        handled.append(item.id)
        stopped.set()  # 处理第一句时请求停止

    run_loop(hub, handler, stop=stopped.is_set)

    assert handled == ["in_1", "in_2"]
    assert ack_ids(hub) == [["in_1", "in_2"]]
    assert len(hub.fetches()) == 1  # 之后不再取话


def test_stop_is_asked_between_two_fetches(hub):
    calls = []

    def stop():
        calls.append(len(hub.fetches()))
        return len(calls) > 3

    run_loop(hub, lambda item: None, stop=stop)

    assert calls == [0, 1, 2, 3] and len(hub.fetches()) == 3


# ---------------------------------------------------------------- 心跳


def test_heartbeats_go_out_while_a_handler_is_slow_and_stop_when_it_ends(hub):
    hub.script("inbox", batch("in_1"))

    def slow(item):
        time.sleep(0.3)

    run_loop(hub, slow, stop=after_acks(hub), heartbeat_seconds=0.05)

    count = len(hub.heartbeats())
    assert count >= 3
    for beat in hub.heartbeats():
        assert (beat.method, beat.target) == ("GET", "/v1/inbox?wait=0&max=0")
        assert beat.header("Authorization") == f"Bearer {KEY}"
    time.sleep(0.3)
    assert len(hub.heartbeats()) == count  # 处理结束以后不再有心跳
    assert heartbeat_threads() == []


def test_heartbeats_stop_too_when_the_handler_raises(hub):
    hub.script("inbox", batch("in_1"))

    def slow_and_broken(item):
        time.sleep(0.3)
        raise ValueError

    run_loop(hub, slow_and_broken, stop=after_fetches(hub, 1), heartbeat_seconds=0.05)

    count = len(hub.heartbeats())
    assert count >= 3
    time.sleep(0.3)
    assert len(hub.heartbeats()) == count
    assert heartbeat_threads() == []


def test_a_handler_that_returns_at_once_does_not_cause_a_heartbeat(hub):
    hub.script("inbox", batch("in_1", "in_2", "in_3"))

    run_loop(hub, lambda item: None, stop=after_acks(hub), heartbeat_seconds=10)

    assert hub.heartbeats() == []
    assert heartbeat_threads() == []


def test_each_handler_call_gets_its_own_heartbeat(hub):
    hub.script("inbox", batch("in_1", "in_2"))

    def slow(item):
        time.sleep(0.2)

    run_loop(hub, slow, stop=after_acks(hub), heartbeat_seconds=0.05)

    assert len(hub.heartbeats()) >= 4  # 两个 0.2 秒各自至少 2 次


def test_the_heartbeat_thread_is_a_daemon_and_has_a_name(hub):
    hub.script("inbox", batch("in_1"))
    seen = []

    def handler(item):
        seen.extend((t.name, t.daemon) for t in heartbeat_threads())

    run_loop(hub, handler, stop=after_acks(hub), heartbeat_seconds=60)

    assert seen == [(HEARTBEAT_THREAD, True)]


@pytest.mark.parametrize(
    "failure",
    [err(503), err(500), reset(), close(), err(401, "unauthorized")],
    ids=lambda r: f"{r.action}-{r.status}",
)
def test_a_failed_heartbeat_is_only_logged_and_the_handler_carries_on(hub, caplog, failure):
    hub.script("inbox", batch("in_1"), *[failure] * 60)
    caplog.set_level(logging.WARNING, logger="clawbot_hub_client")

    def slow(item):
        time.sleep(0.25)

    run_loop(hub, slow, stop=after_acks(hub), heartbeat_seconds=0.05)

    assert ack_ids(hub) == [["in_1"]]  # 处理照常，照常确认
    assert len(hub.heartbeats()) >= 2
    assert any("心跳" in r.getMessage() for r in caplog.records)
    assert KEY not in caplog.text and "话 in_1" not in caplog.text


def test_one_heartbeat_is_one_request_with_no_client_level_retry(hub, pauses):
    hub.script("inbox", err(503), err(503))
    client = client_for(hub)  # retries=1

    with pytest.raises(hub_client.HubUnavailable):
        client._beat()

    assert len(hub.requests_to("inbox")) == 1 and pauses == []


def test_the_loop_never_uses_the_sleep_argument_from_the_heartbeat_thread(hub):
    hub.script("inbox", batch("in_1"))
    threads = []

    def sleep(seconds):
        threads.append(threading.current_thread().name)

    def slow(item):
        time.sleep(0.2)

    client_for(hub).run_inbox(slow, stop=after_fetches(hub, 2), sleep=sleep, heartbeat_seconds=0.05)

    assert len(hub.heartbeats()) >= 2
    assert set(threads) <= {threading.current_thread().name}


class RecordingHeartbeat:
    """顶替 `_Heartbeat`：只记下算出来的间隔，不真的发。"""

    intervals: list[float] = []

    def __init__(self, client, interval):
        type(self).intervals.append(interval)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


@pytest.fixture
def intervals(monkeypatch) -> list[float]:
    RecordingHeartbeat.intervals = []
    monkeypatch.setattr(hub_client, "_Heartbeat", RecordingHeartbeat)
    return RecordingHeartbeat.intervals


def run_with_default_interval(hub) -> None:
    hub.script("inbox", batch("in_1"))
    client_for(hub).run_inbox(lambda item: None, stop=after_acks(hub), sleep=lambda s: None)


def test_the_default_heartbeat_interval_is_a_third_of_the_online_seconds(hub, intervals):
    hub.script("status", Reply(body=status_body(online=90)))

    run_with_default_interval(hub)

    assert intervals == [30.0]
    assert len(hub.requests_to("status")) == 1


def test_a_different_online_seconds_gives_a_different_interval(hub, intervals):
    hub.script("status", Reply(body=status_body(online=12)))

    run_with_default_interval(hub)

    assert intervals == [4.0]


def test_when_the_status_cannot_be_had_90_seconds_is_assumed(hub, intervals):
    hub.script("status", err(503))

    run_with_default_interval(hub)

    assert intervals == [30.0]
    assert len(hub.requests_to("status")) == 1  # 取一次，取不到就算了，不重试


@pytest.mark.parametrize("online", [None, "90", 0, -3, True, [], float("nan")])
def test_an_online_seconds_that_makes_no_sense_means_90(hub, intervals, online):
    body = status_body()
    body["service"]["inbox_online_seconds"] = online
    hub.script("status", Reply(body=body))

    run_with_default_interval(hub)

    assert intervals == [30.0]


def test_a_given_heartbeat_interval_is_used_as_it_is_without_asking_the_service(hub, intervals):
    hub.script("inbox", batch("in_1"))

    client_for(hub).run_inbox(
        lambda item: None, stop=after_acks(hub), sleep=lambda s: None, heartbeat_seconds=0.5
    )

    assert intervals == [0.5]
    assert hub.requests_to("status") == []


def test_the_service_is_asked_once_per_loop_and_not_once_per_batch(hub, intervals):
    hub.script("inbox", batch("in_1"), batch("in_2"), batch("in_3"))

    client_for(hub).run_inbox(lambda item: None, stop=after_acks(hub, 3), sleep=lambda s: None)

    assert intervals == [30.0, 30.0, 30.0]  # 每句话各起一个心跳，间隔一样
    assert len(hub.requests_to("status")) == 1


@pytest.mark.parametrize("bad", [0, -1, 0.0, float("nan"), float("inf"), "1", True])
def test_a_heartbeat_interval_that_would_hammer_the_service_or_never_fire_is_refused(hub, bad):
    with pytest.raises(ValueError, match="heartbeat_seconds"):
        client_for(hub).run_inbox(
            lambda item: None, stop=lambda: True, sleep=lambda s: None, heartbeat_seconds=bad
        )

    assert hub.requests == []


def test_the_status_question_at_the_start_also_teaches_the_client_the_send_deadline(hub):
    hub.script("status", Reply(body=status_body(send_timeout=60)))
    client = client_for(hub)

    client.run_inbox(lambda item: None, stop=lambda: True, sleep=lambda s: None)

    assert client._send_read_timeout(None) == 65.0
