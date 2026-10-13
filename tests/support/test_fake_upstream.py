"""进程内假上游自己的测试（任务 A4）：假件按剧本给出每一种结果。

sender、poller、额度、登录的组件测试全靠它给"成功、被拒绝、暂时不通、不知道结果、登录失效、
很慢、卡在半路"这几种结果，所以这里逐种验证，免得假件写错而上层测试全绿。
另外核对它和 `Upstream` 协议、真实现 `HttpUpstream` 的方法签名一致，三者不会各走各的。
"""

from __future__ import annotations

import inspect
import threading
import time

import pytest

from clawbot_hub.upstream.base import (
    Accepted,
    Account,
    IncomingMessage,
    NoResponse,
    PollFailed,
    QrCode,
    QrStatus,
    Rejected,
    SessionExpired,
    Transient,
    Updates,
    Upstream,
    UpstreamError,
)
from clawbot_hub.upstream.http import HttpUpstream
from tests.support import fake_upstream as fu
from tests.support.fake_clock import FakeClock
from tests.support.fake_upstream import (
    DEFAULT,
    GET_UPDATES,
    NOTIFY_START,
    NOTIFY_STOP,
    QR_START,
    QR_STATUS,
    SEND_TEXT,
    FakeUpstream,
    Gate,
    after,
    an_account,
)
from tests.support.threads import run_in_thread

ACCT = an_account()


@pytest.fixture
def fake():
    with FakeUpstream() as upstream:
        yield upstream


def send(fake: FakeUpstream, text: str = "你好", **overrides):
    args = {
        "acct": ACCT,
        "to_user_id": "someone@im.wechat",
        "context_token": "ctx-9",
        "text": text,
        "client_id": "client-1",
        "timeout": 5.0,
    }
    args.update(overrides)
    return fake.send_text(**args)


def poll(fake: FakeUpstream, buf: str = "", timeout: float = 5.0):
    return fake.get_updates(ACCT, buf, timeout)


# ---------------------------------------------------------------- 和协议、真实现对得上


METHODS = ("get_updates", "send_text", "qr_start", "qr_status", "notify_start", "notify_stop")


def _shape(function) -> list[tuple[str, object, object]]:
    return [
        (p.name, p.kind, p.default)
        for p in inspect.signature(function).parameters.values()
        if p.name != "self"
    ]


@pytest.mark.parametrize("name", METHODS)
def test_the_fake_has_every_method_of_the_protocol_with_the_same_parameters(name):
    assert _shape(getattr(FakeUpstream, name)) == _shape(getattr(Upstream, name))


@pytest.mark.parametrize("name", METHODS)
def test_the_real_implementation_has_the_same_parameters_too(name):
    assert _shape(getattr(HttpUpstream, name)) == _shape(getattr(Upstream, name))


def test_the_fake_can_be_used_where_an_upstream_is_expected(fake):
    upstream: Upstream = fake
    assert isinstance(upstream.send_text(ACCT, "u", "c", "t", "id", 1.0), Accepted)


def test_an_account_for_tests_has_all_four_fields_and_overrides():
    account = an_account()
    assert account.bot_token and account.bot_id and account.user_id and account.base_url
    assert an_account(bot_token="other").bot_token == "other"
    assert an_account(base_url="").base_url == ""


def test_the_default_address_can_never_be_resolved():
    # 万一哪个组件测试误把真实现指向它，也只会解析失败，不会连到真地址
    assert fu.DEFAULT_BASE_URL.split("//")[1].endswith(".invalid")


# ---------------------------------------------------------------- 发送：默认


def test_a_send_is_accepted_with_a_message_id_by_default(fake):
    outcome = send(fake)
    assert isinstance(outcome, Accepted)
    assert isinstance(outcome.message_id, str) and outcome.message_id


def test_every_default_send_gets_a_different_message_id(fake):
    ids = {send(fake).message_id for _ in range(5)}
    assert len(ids) == 5


def test_without_message_ids_a_send_is_accepted_but_has_no_id():
    with FakeUpstream(message_ids=False) as fake:
        assert send(fake) == Accepted(None)


def test_every_send_is_recorded_with_everything_the_caller_passed(fake):
    send(fake, "第一条", to_user_id="a@im.wechat", context_token="c1", client_id="k1", timeout=7.5)
    send(fake, "第二条", to_user_id="b@im.wechat", context_token="c2", client_id="k2", timeout=3.0)
    first, second = fake.sent
    assert (first.index, second.index) == (1, 2)
    assert (first.to_user_id, first.context_token, first.client_id) == ("a@im.wechat", "c1", "k1")
    assert (first.text, first.timeout, first.acct) == ("第一条", 7.5, ACCT)
    assert (second.text, second.timeout) == ("第二条", 3.0)
    assert fake.sent_texts() == ["第一条", "第二条"]


def test_the_text_is_kept_exactly_as_sent(fake):
    send(fake, "  首尾有空白\n\n")
    assert fake.sent_texts() == ["  首尾有空白\n\n"]


def test_a_send_record_points_at_its_entry_in_the_call_log(fake):
    poll(fake)
    send(fake, "第二次调用")
    (record,) = fake.sent
    assert fake.calls[record.call_index - 1].args["text"] == "第二次调用"
    assert record.call_index == 2


def test_a_record_carries_the_outcome_the_caller_got(fake):
    outcome = send(fake)
    assert fake.sent[0].outcome == outcome


def test_failed_attempts_are_recorded_too(fake):
    fake.script(SEND_TEXT, Transient("连不上"), Rejected(200, 40001, "bad token"))
    send(fake, "一")
    send(fake, "二")
    send(fake, "三")
    assert fake.sent_texts() == ["一", "二", "三"]
    assert [type(m.outcome) for m in fake.sent] == [Transient, Rejected, Accepted]


def test_sent_is_a_copy_so_the_caller_cannot_change_the_log(fake):
    send(fake)
    fake.sent.clear()
    assert len(fake.sent) == 1


# ---------------------------------------------------------------- 剧本


def test_a_script_is_used_in_order_and_then_the_default_takes_over(fake):
    fake.script(SEND_TEXT, Transient("a"), NoResponse("b", read_timeout=True), SessionExpired())
    outcomes = [send(fake) for _ in range(4)]
    assert outcomes[:3] == [Transient("a"), NoResponse("b", read_timeout=True), SessionExpired()]
    assert isinstance(outcomes[3], Accepted)


def test_scripts_add_up(fake):
    fake.script(SEND_TEXT, Transient("a"))
    fake.script(SEND_TEXT, Transient("b"), Transient("c"))
    assert [send(fake).reason for _ in range(3)] == ["a", "b", "c"]


def test_every_kind_of_send_outcome_can_be_scripted(fake):
    kinds = [
        Accepted("m1"),
        Accepted(None),
        Rejected(200, 40001, "bad token"),
        Rejected(403, None),
        SessionExpired(),
        Transient("连不上"),
        NoResponse("读超时", read_timeout=True),
    ]
    fake.script(SEND_TEXT, *kinds)
    assert [send(fake) for _ in kinds] == kinds


def test_a_default_stays_until_it_is_replaced_and_none_restores_the_built_in(fake):
    fake.set_default(SEND_TEXT, Rejected(200, 5, "no"))
    assert send(fake) == send(fake) == Rejected(200, 5, "no")
    fake.set_default(SEND_TEXT, None)
    assert isinstance(send(fake), Accepted)


def test_a_script_goes_before_the_default(fake):
    fake.set_default(SEND_TEXT, Rejected(200, 5, "no"))
    fake.script(SEND_TEXT, Transient("once"))
    assert [type(send(fake)) for _ in range(3)] == [Transient, Rejected, Rejected]


def test_clearing_the_scripts_drops_what_is_left_but_not_the_default(fake):
    fake.set_default(SEND_TEXT, Rejected(200, 5, "no"))
    fake.script(SEND_TEXT, Transient("a"))
    fake.script(GET_UPDATES, SessionExpired())
    fake.clear_scripts()
    assert send(fake) == Rejected(200, 5, "no")
    assert isinstance(poll(fake), Updates)


def test_clearing_the_scripts_drops_the_qr_plans_too(fake):
    fake.qr_plan(["expired"])
    fake.clear_scripts()
    assert fake.qr_status(fake.qr_start().code, None, None).status == "confirmed"


def test_each_operation_has_its_own_script(fake):
    fake.script(GET_UPDATES, PollFailed("x", completed=False))
    assert isinstance(send(fake), Accepted)
    assert poll(fake) == PollFailed("x", completed=False)


def test_a_callable_gets_the_call_and_decides_the_outcome(fake):
    seen = []

    def decide(call):
        seen.append(call)
        return Rejected(200, 7, call.args["text"])

    fake.script(SEND_TEXT, decide)
    assert send(fake, "原文") == Rejected(200, 7, "原文")
    (call,) = seen
    assert call.operation == SEND_TEXT and call.index == 1
    assert call.args["to_user_id"] == "someone@im.wechat" and call.args["client_id"] == "client-1"


def test_a_callable_may_hand_back_to_the_built_in_behaviour(fake):
    fake.script(SEND_TEXT, lambda call: DEFAULT)
    assert isinstance(send(fake), Accepted)


def test_an_exception_in_a_script_is_raised_once(fake):
    fake.script(SEND_TEXT, RuntimeError("意外"))
    with pytest.raises(RuntimeError, match="意外"):
        send(fake)
    assert isinstance(send(fake), Accepted)


def test_an_exception_raised_by_a_callable_goes_through(fake):
    def boom(call):
        raise KeyboardInterrupt

    fake.script(SEND_TEXT, boom)
    with pytest.raises(KeyboardInterrupt):
        send(fake)


def test_an_attempt_that_raised_is_still_recorded(fake):
    fake.script(SEND_TEXT, RuntimeError("意外"))
    with pytest.raises(RuntimeError):
        send(fake, "炸了")
    assert fake.sent_texts() == ["炸了"] and fake.sent[0].outcome is None


def test_an_outcome_of_the_wrong_kind_is_refused_when_it_is_scripted(fake):
    with pytest.raises(TypeError, match="send_text"):
        fake.script(SEND_TEXT, Updates())
    with pytest.raises(TypeError, match="get_updates"):
        fake.script(GET_UPDATES, Accepted("m"))
    with pytest.raises(TypeError):
        fake.script(SEND_TEXT, "accepted")
    with pytest.raises(TypeError):
        fake.set_default(SEND_TEXT, 42)


def test_nothing_is_added_to_a_script_when_one_of_its_items_is_refused(fake):
    with pytest.raises(TypeError):
        fake.script(SEND_TEXT, Transient("a"), "不是结果")
    assert isinstance(send(fake), Accepted)


def test_a_callable_that_returns_the_wrong_kind_is_reported_when_it_is_called(fake):
    fake.script(SEND_TEXT, lambda call: Updates())
    with pytest.raises(TypeError, match="send_text"):
        send(fake)


def test_an_unknown_operation_is_refused_and_the_known_ones_are_listed(fake):
    with pytest.raises(ValueError, match="send_text"):
        fake.script("sendmessage", Transient("x"))
    with pytest.raises(ValueError):
        fake.set_default("nope", None)
    with pytest.raises(ValueError):
        fake.calls_to("nope")


# ---------------------------------------------------------------- 模式


def test_disconnect_mode_means_nothing_gets_through(fake):
    fake.set_mode("disconnect")
    assert isinstance(send(fake), Transient)
    failed = poll(fake)
    assert isinstance(failed, PollFailed) and failed.completed is False


def test_session_expired_mode_expires_both_sending_and_polling(fake):
    fake.set_mode("session_expired")
    assert send(fake) == SessionExpired()
    assert poll(fake) == SessionExpired()


def test_http_5xx_mode_is_transient_and_not_a_round_trip(fake):
    fake.set_mode("http_5xx")
    outcome = send(fake)
    assert isinstance(outcome, Transient) and "503" in outcome.reason
    failed = poll(fake)
    assert isinstance(failed, PollFailed) and failed.completed is False and "503" in failed.reason


def test_normal_mode_puts_everything_back(fake):
    fake.set_mode("session_expired")
    fake.set_mode("normal")
    assert isinstance(send(fake), Accepted)
    assert isinstance(poll(fake), Updates)


def test_modes_do_not_touch_the_qr_calls(fake):
    fake.set_mode("disconnect")
    assert isinstance(fake.qr_start(), QrCode)
    assert fake.qr_status("fake-qr-1", None, None).status == "confirmed"


def test_a_script_goes_before_a_mode(fake):
    fake.set_mode("session_expired")
    fake.script(SEND_TEXT, Accepted("m1"))
    assert [send(fake), send(fake)] == [Accepted("m1"), SessionExpired()]


def test_an_unknown_mode_is_refused_and_the_known_ones_are_listed(fake):
    with pytest.raises(ValueError, match="session_expired"):
        fake.set_mode("flaky")


def test_the_modes_have_the_same_names_as_the_http_fake():
    from tests.support import fake_ilink

    assert set(fu.MODES) == set(fake_ilink.MODES)


# ---------------------------------------------------------------- 调用记录


def test_every_call_is_logged_in_order_with_its_arguments(fake):
    send(fake, "x")
    poll(fake, "buf-0", timeout=1.0)
    fake.notify_start(ACCT)
    fake.qr_start()
    fake.qr_status("fake-qr-1", "123456", "host.example:8443")
    fake.notify_stop(ACCT)
    assert [c.operation for c in fake.calls] == [
        SEND_TEXT,
        GET_UPDATES,
        NOTIFY_START,
        QR_START,
        QR_STATUS,
        NOTIFY_STOP,
    ]
    assert [c.index for c in fake.calls] == [1, 2, 3, 4, 5, 6]
    assert fake.calls[1].args == {"acct": ACCT, "buf": "buf-0", "timeout": 1.0}
    assert fake.calls[4].args == {
        "code": "fake-qr-1",
        "verify_code": "123456",
        "host": "host.example:8443",
    }
    assert fake.calls[2].args == {"acct": ACCT}
    assert fake.calls[3].args == {}


def test_calls_is_a_copy_too(fake):
    send(fake)
    fake.calls.clear()
    assert len(fake.calls) == 1


def test_calls_can_be_picked_out_by_operation(fake):
    send(fake)
    poll(fake)
    send(fake)
    assert [c.index for c in fake.calls_to(SEND_TEXT)] == [1, 3]
    assert fake.calls_to(NOTIFY_STOP) == []


def test_nothing_is_logged_before_anything_is_called(fake):
    assert fake.calls == [] and fake.sent == []


def test_clearing_the_log_empties_calls_and_sent_but_keeps_the_inbox_and_the_numbers(fake):
    fake.push_user_message("在")
    first = send(fake)
    fake.clear_calls()
    assert fake.calls == [] and fake.sent == []
    assert send(fake) != first  # 消息编号接着往下，不重复
    assert fake.sent[0].index == 1 and fake.calls[0].index == 1
    assert len(poll(fake).messages) == 1


def test_a_scripted_failure_is_still_a_logged_call(fake):
    fake.script(QR_START, UpstreamError("连不上"))
    with pytest.raises(UpstreamError):
        fake.qr_start()
    assert [c.operation for c in fake.calls] == [QR_START]


# ---------------------------------------------------------------- 收消息


def test_a_pushed_message_comes_out_of_get_updates_with_every_field(fake):
    pushed = fake.push_user_message(
        "你好", context_token="ctx-x", at_ms=1_700_000_000_123, from_user_id="u@im.wechat"
    )
    updates = poll(fake)
    assert isinstance(updates, Updates) and not updates.timed_out
    assert updates.messages == (pushed,)
    message = updates.messages[0]
    assert isinstance(message, IncomingMessage)
    assert (message.from_user_id, message.message_type) == ("u@im.wechat", 1)
    assert (message.context_token, message.at_ms) == ("ctx-x", 1_700_000_000_123)
    assert (message.text, message.has_text, message.seq) == ("你好", True, 1)
    assert isinstance(message.message_id, str) and message.message_id


def test_defaults_come_from_the_fake_owner_and_the_clock():
    clock = FakeClock()
    with FakeUpstream(clock=clock, user_id="owner@im.wechat") as fake:
        message = fake.push_user_message("嗨")
        assert message.from_user_id == "owner@im.wechat"
        assert message.at_ms == clock.now_ms()
        clock.advance(60)
        assert fake.push_user_message("再嗨").at_ms == clock.now_ms()


def test_without_a_clock_the_time_is_the_real_time(fake):
    before = time.time_ns() // 1_000_000
    at_ms = fake.push_user_message("嗨").at_ms
    assert before <= at_ms <= time.time_ns() // 1_000_000


def test_sequence_numbers_and_message_ids_and_context_tokens_are_all_different(fake):
    messages = [fake.push_user_message(str(n)) for n in range(4)]
    assert [m.seq for m in messages] == [1, 2, 3, 4]
    assert len({m.message_id for m in messages}) == 4
    assert len({m.context_token for m in messages}) == 4


def test_a_message_without_a_conversation_token_or_time_or_text_can_be_pushed(fake):
    fake.push_user_message(context_token=None, at_ms=None, has_text=False)
    (message,) = poll(fake).messages
    assert message.context_token is None and message.at_ms is None and message.has_text is False


def test_a_message_that_has_no_text_cannot_also_carry_text(fake):
    with pytest.raises(ValueError):
        fake.push_user_message("字", has_text=False)
    assert fake.inbox_size == 0


def test_a_bot_message_has_type_2_comes_from_the_bot_and_has_no_token(fake):
    pushed = fake.push_bot_message("我自己发的")
    assert pushed.message_type == 2 and pushed.from_user_id == fake.bot_id
    assert pushed.context_token is None and pushed.text == "我自己发的"


def test_the_inbox_is_only_ever_added_to(fake):
    assert fake.inbox_size == 0
    fake.push_user_message("a")
    fake.push_user_message("b")
    assert fake.inbox_size == 2


def test_polling_from_the_position_it_gave_returns_only_what_is_new(fake):
    fake.push_user_message("一")
    first = poll(fake)
    assert [m.text for m in first.messages] == ["一"]
    assert poll(fake, first.buf).messages == ()
    fake.push_user_message("二")
    third = poll(fake, first.buf)
    assert [m.text for m in third.messages] == ["二"] and third.buf != first.buf


def test_the_position_does_not_move_when_nothing_is_new(fake):
    fake.push_user_message("一")
    first = poll(fake)
    assert poll(fake, first.buf).buf == first.buf


def test_a_position_from_the_future_is_kept_as_it_is(fake):
    fake.push_user_message("一")
    updates = poll(fake, "buf-5")
    assert updates.messages == () and updates.buf == "buf-5"


def test_an_old_position_gets_the_same_batch_again(fake):
    fake.push_user_message("一")
    fake.push_user_message("二")
    first = poll(fake, "")
    again = poll(fake, "")
    assert first.messages == again.messages and len(first.messages) == 2


def test_without_redelivery_a_message_is_handed_out_only_once():
    with FakeUpstream(redeliver=False) as fake:
        fake.push_user_message("一")
        assert len(poll(fake).messages) == 1
        assert poll(fake).messages == ()
        assert poll(fake, "buf-0").messages == ()


def test_an_empty_position_starts_after_the_latest_login(fake):
    fake.push_user_message("登录前的")
    code = fake.qr_start().code
    assert fake.qr_status(code, None, None).status == "confirmed"
    fake.push_user_message("登录后的")
    assert [m.text for m in poll(fake, "").messages] == ["登录后的"]


def test_before_any_login_an_empty_position_starts_at_the_beginning(fake):
    fake.push_user_message("先到的")
    assert [m.text for m in poll(fake, "").messages] == ["先到的"]


def test_a_position_that_is_not_ours_is_treated_like_an_empty_one(fake):
    fake.push_user_message("一")
    assert len(poll(fake, "garbage").messages) == 1
    assert len(poll(fake, "buf-").messages) == 1


def test_the_suggested_long_poll_time_is_passed_on_when_set():
    with FakeUpstream(longpoll_hint_ms=35_000) as fake:
        assert poll(fake).longpolling_timeout_ms == 35_000
    with FakeUpstream() as fake:
        assert poll(fake).longpolling_timeout_ms is None


def test_with_nothing_to_deliver_the_poll_waits_a_little_and_returns_empty():
    with FakeUpstream(longpoll_seconds=0.1) as fake:
        started = time.monotonic()
        updates = poll(fake)
        assert 0.08 <= time.monotonic() - started < 2
        assert updates.messages == () and updates.buf


def test_the_wait_never_exceeds_the_timeout_the_caller_gave():
    with FakeUpstream(longpoll_seconds=5.0) as fake:
        started = time.monotonic()
        poll(fake, timeout=0.05)
        assert time.monotonic() - started < 2


def test_a_waiting_poll_returns_as_soon_as_a_message_arrives():
    with FakeUpstream(longpoll_seconds=5.0) as fake:
        worker = run_in_thread(poll, fake)
        assert worker.still_blocked(0.1)
        fake.push_user_message("来了")
        updates = worker.result(timeout=2)
        assert [m.text for m in updates.messages] == ["来了"]


def test_closing_wakes_a_waiting_poll():
    fake = FakeUpstream(longpoll_seconds=5.0)
    worker = run_in_thread(poll, fake)
    assert worker.still_blocked(0.1)
    fake.close()
    assert worker.result(timeout=2).messages == ()


def test_a_poll_after_closing_does_not_wait():
    fake = FakeUpstream(longpoll_seconds=5.0)
    fake.close()
    started = time.monotonic()
    poll(fake)
    assert time.monotonic() - started < 1


def test_closing_twice_is_harmless():
    fake = FakeUpstream()
    fake.close()
    fake.close()


def test_the_scripted_poll_results_come_before_the_inbox(fake):
    fake.push_user_message("在收件箱里")
    fake.script(
        GET_UPDATES,
        SessionExpired(),
        PollFailed("403", completed=True),
        Updates(timed_out=True),
        Updates(buf="custom"),
    )
    assert poll(fake) == SessionExpired()
    assert poll(fake) == PollFailed("403", completed=True)
    assert poll(fake) == Updates(timed_out=True)
    assert poll(fake) == Updates(buf="custom")
    assert [m.text for m in poll(fake).messages] == ["在收件箱里"]


def test_a_scripted_poll_does_not_use_up_the_inbox(fake):
    fake.push_user_message("还在")
    fake.script(GET_UPDATES, SessionExpired())
    poll(fake)
    assert len(poll(fake).messages) == 1


# ---------------------------------------------------------------- 扫码


def test_each_qr_code_is_different_and_carries_the_link_to_draw(fake):
    first, second = fake.qr_start(), fake.qr_start()
    assert isinstance(first, QrCode) and first.code != second.code
    assert first.code in first.image_content and second.code in second.image_content


def test_without_a_plan_a_code_is_confirmed_straight_away_with_a_new_account(fake):
    status = fake.qr_status(fake.qr_start().code, None, None)
    assert status.status == "confirmed" and status.redirect_host is None
    account = status.account
    assert isinstance(account, Account) and account.bot_token
    assert (account.bot_id, account.user_id) == (fake.bot_id, fake.user_id)
    assert account.base_url == fu.DEFAULT_BASE_URL
    assert fake.logins == 1


def test_every_login_gets_a_different_token_numbered_from_one(fake):
    tokens = [fake.qr_status(fake.qr_start().code, None, None).account.bot_token for _ in range(3)]
    assert tokens == ["fake-token-1", "fake-token-2", "fake-token-3"] and fake.logins == 3
    assert an_account().bot_token == "fake-token-0"  # 随手要的账号不会和某次登录撞车


def test_asking_again_about_a_confirmed_code_gives_the_same_answer_without_a_new_login(fake):
    code = fake.qr_start().code
    first = fake.qr_status(code, None, None)
    assert fake.qr_status(code, None, None) == first
    assert fake.logins == 1


def test_a_code_nobody_issued_is_expired(fake):
    assert fake.qr_status("never-issued", None, None) == QrStatus("expired")
    assert fake.logins == 0


def test_a_plan_gives_the_statuses_in_order_and_repeats_the_last(fake):
    fake.qr_plan(["wait", "scaned", "expired"])
    code = fake.qr_start().code
    answers = [fake.qr_status(code, None, None).status for _ in range(5)]
    assert answers == ["wait", "scaned", "expired", "expired", "expired"]


def test_each_code_follows_its_own_plan_and_later_codes_fall_back_to_confirming(fake):
    fake.qr_plan(["expired"], ["wait", "confirmed"])
    first, second, third = (fake.qr_start().code for _ in range(3))
    assert fake.qr_status(first, None, None).status == "expired"
    assert fake.qr_status(second, None, None).status == "wait"
    assert fake.qr_status(second, None, None).status == "confirmed"
    assert fake.qr_status(third, None, None).status == "confirmed"


def test_plans_add_up(fake):
    fake.qr_plan(["expired"])
    fake.qr_plan(["wait"])
    codes = [fake.qr_start().code for _ in range(2)]
    assert [fake.qr_status(c, None, None).status for c in codes] == ["expired", "wait"]


def test_a_plan_entry_can_carry_a_redirect_host(fake):
    fake.qr_plan([{"status": "scaned_but_redirect", "redirect_host": "redirect.example:8443"}])
    status = fake.qr_status(fake.qr_start().code, None, None)
    assert status == QrStatus("scaned_but_redirect", redirect_host="redirect.example:8443")


def test_a_plan_entry_can_be_a_ready_made_status(fake):
    ready = QrStatus("confirmed", account=Account("my-token", "https://x.invalid", "b", "u"))
    fake.qr_plan(["need_verifycode", ready])
    code = fake.qr_start().code
    assert fake.qr_status(code, None, None).status == "need_verifycode"
    assert fake.qr_status(code, "123456", None) == ready


def test_a_plan_entry_can_carry_the_account_to_hand_out(fake):
    mine = Account("my-token", "https://x.invalid", "b", "u")
    fake.qr_plan([{"status": "confirmed", "account": mine}])
    assert fake.qr_status(fake.qr_start().code, None, None) == QrStatus("confirmed", account=mine)


def test_a_confirmed_entry_without_an_account_gets_a_new_one(fake):
    fake.qr_plan(["scaned", {"status": "confirmed"}])
    code = fake.qr_start().code
    fake.qr_status(code, None, None)
    status = fake.qr_status(code, None, None)
    assert status.status == "confirmed" and status.account.bot_token and fake.logins == 1


def test_statuses_the_real_service_might_invent_are_passed_through(fake):
    fake.qr_plan(["verify_code_blocked"], ["binded_redirect"], ["something_new"])
    codes = [fake.qr_start().code for _ in range(3)]
    assert [fake.qr_status(c, None, None).status for c in codes] == [
        "verify_code_blocked",
        "binded_redirect",
        "something_new",
    ]


def test_a_plan_entry_of_the_wrong_kind_is_refused(fake):
    with pytest.raises(TypeError):
        fake.qr_plan([42])
    with pytest.raises(TypeError):
        fake.qr_plan([{"redirect_host": "x"}])  # 没有 status
    with pytest.raises(TypeError):
        fake.qr_plan(["wait", None])


def test_a_typo_in_a_status_dictionary_is_refused_instead_of_ignored(fake):
    with pytest.raises(TypeError, match="redirect_hots"):
        fake.qr_plan([{"status": "scaned_but_redirect", "redirect_hots": "x"}])
    with pytest.raises(TypeError, match="redirect_hots"):
        fake.script(QR_STATUS, {"status": "scaned_but_redirect", "redirect_hots": "x"})


def test_an_empty_plan_and_a_plan_with_a_bad_entry_add_nothing(fake):
    with pytest.raises(ValueError):
        fake.qr_plan(["wait"], [])
    with pytest.raises(TypeError):
        fake.qr_plan(["wait"], ["wait", 42])
    assert fake.qr_status(fake.qr_start().code, None, None).status == "confirmed"


def test_the_marker_for_the_built_in_behaviour_can_be_scripted_directly(fake):
    fake.script(SEND_TEXT, DEFAULT, Transient("x"))
    assert isinstance(send(fake), Accepted)
    assert send(fake) == Transient("x")
    fake.script(QR_STATUS, DEFAULT)
    assert fake.qr_status(fake.qr_start().code, None, None).status == "confirmed"


def test_a_scripted_function_may_return_a_status_name_or_dictionary_but_only_a_good_one(fake):
    fake.script(QR_STATUS, lambda call: "scaned", lambda call: {"status": "wait", "oops": 1})
    assert fake.qr_status("c", None, None).status == "scaned"
    with pytest.raises(TypeError, match="oops"):
        fake.qr_status("c", None, None)


def test_a_class_where_an_instance_belongs_is_refused(fake):
    with pytest.raises(TypeError, match="Accepted"):
        fake.script(SEND_TEXT, Accepted)


def test_qr_calls_can_be_scripted_like_the_others(fake):
    fake.script(QR_START, QrCode("fixed", "https://x.invalid/q"), UpstreamError("连不上"))
    assert fake.qr_start() == QrCode("fixed", "https://x.invalid/q")
    with pytest.raises(UpstreamError, match="连不上"):
        fake.qr_start()
    assert fake.qr_start().code.startswith("fake-qr-")
    fake.script(QR_STATUS, QrStatus("scaned"), "wait", {"status": "expired"})
    assert [fake.qr_status("c", None, None).status for _ in range(3)] == [
        "scaned",
        "wait",
        "expired",
    ]


def test_a_scripted_status_comes_before_the_plan_and_does_not_advance_it(fake):
    fake.qr_plan(["wait", "confirmed"])
    code = fake.qr_start().code
    fake.script(QR_STATUS, UpstreamError("连不上"))
    with pytest.raises(UpstreamError):
        fake.qr_status(code, None, None)
    assert fake.qr_status(code, None, None).status == "wait"


def test_the_query_arguments_are_logged(fake):
    code = fake.qr_start().code
    fake.qr_status(code, "654321", "h.example:1")
    (call,) = fake.calls_to(QR_STATUS)
    assert call.args == {"code": code, "verify_code": "654321", "host": "h.example:1"}


def test_a_login_moves_the_start_of_the_inbox_only_when_it_is_confirmed(fake):
    fake.qr_plan(["wait", "confirmed"])
    code = fake.qr_start().code
    fake.push_user_message("扫码前")
    fake.qr_status(code, None, None)  # wait
    assert len(poll(fake, "").messages) == 1
    fake.qr_status(code, None, None)  # confirmed
    assert poll(fake, "").messages == ()


# ---------------------------------------------------------------- 通知


def test_notify_calls_do_nothing_but_are_logged(fake):
    assert fake.notify_start(ACCT) is None
    assert fake.notify_stop(ACCT) is None
    assert [c.operation for c in fake.calls] == [NOTIFY_START, NOTIFY_STOP]
    assert fake.calls[0].args["acct"] == ACCT


def test_notify_can_be_made_to_fail_with_a_script(fake):
    fake.script(NOTIFY_STOP, RuntimeError("意外"))
    with pytest.raises(RuntimeError):
        fake.notify_stop(ACCT)
    assert fake.notify_stop(ACCT) is None


def test_notify_takes_no_plain_values_in_a_script(fake):
    with pytest.raises(TypeError):
        fake.script(NOTIFY_START, Accepted("m"))
    with pytest.raises(TypeError):
        fake.script(NOTIFY_STOP, None)


def test_a_function_in_a_notify_script_may_return_nothing(fake):
    seen = []
    fake.script(NOTIFY_START, lambda call: seen.append(call.index))
    assert fake.notify_start(ACCT) is None and seen == [1]


# ---------------------------------------------------------------- 卡在半路


def test_a_gate_holds_a_send_until_it_is_released(fake):
    gate = Gate()
    fake.script(SEND_TEXT, gate)
    worker = run_in_thread(send, fake, "卡住的")
    assert gate.entered.wait(2)
    assert worker.still_blocked(0.1)
    # 请求已经发出（在记录里），只是还没有结果
    assert fake.sent_texts() == ["卡住的"] and fake.sent[0].outcome is None
    gate.release()
    assert isinstance(worker.result(timeout=2), Accepted)
    assert fake.sent[0].outcome is not None


def test_a_gate_can_decide_the_outcome_when_it_opens(fake):
    gate = Gate(NoResponse("读超时", read_timeout=True))
    fake.script(SEND_TEXT, gate)
    worker = run_in_thread(send, fake)
    assert gate.entered.wait(2)
    gate.release()
    assert worker.result(timeout=2) == NoResponse("读超时", read_timeout=True)


def test_other_calls_go_on_while_a_send_is_held(fake):
    gate = Gate()
    fake.script(SEND_TEXT, gate)
    worker = run_in_thread(send, fake)
    assert gate.entered.wait(2)
    fake.push_user_message("照常")
    assert [m.text for m in poll(fake).messages] == ["照常"]
    assert isinstance(send(fake, "第二条"), Accepted)  # 剧本只管第一条
    gate.release()
    worker.result(timeout=2)


def test_a_gate_installed_as_the_default_holds_every_call(fake):
    gate = fake.hold(SEND_TEXT)
    workers = [run_in_thread(send, fake, str(n)) for n in range(3)]
    deadline = time.monotonic() + 2
    while gate.entries < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert gate.entries == 3 and all(w.still_blocked(0.01) for w in workers)
    gate.release()
    assert all(isinstance(w.result(timeout=2), Accepted) for w in workers)


def test_a_gate_that_was_already_released_lets_calls_straight_through(fake):
    gate = fake.hold(SEND_TEXT)
    gate.release()
    assert isinstance(send(fake), Accepted)


def test_a_gate_that_is_never_opened_fails_the_test_instead_of_hanging(fake):
    fake.script(SEND_TEXT, Gate(patience=0.05))
    with pytest.raises(AssertionError, match="放行"):
        send(fake)


def test_holding_can_be_undone(fake):
    fake.hold(SEND_TEXT)
    fake.set_default(SEND_TEXT, None)
    assert isinstance(send(fake), Accepted)


def test_a_delay_waits_on_the_fake_clock_not_the_real_one():
    clock = FakeClock()
    with FakeUpstream(clock=clock) as fake:
        fake.script(SEND_TEXT, after(clock, 5, Accepted("late")))
        worker = run_in_thread(send, fake)
        clock.wait_until_blocked(1)
        clock.advance(4)
        assert worker.still_blocked(0.05)
        clock.advance(1)
        assert worker.result(timeout=2) == Accepted("late")


def test_a_delay_without_an_outcome_ends_in_the_normal_behaviour():
    clock = FakeClock(auto_sleep=True)
    with FakeUpstream(clock=clock) as fake:
        fake.script(SEND_TEXT, after(clock, 2))
        started = clock.now_ms()
        outcome = send(fake)
        assert isinstance(outcome, Accepted) and clock.now_ms() - started == 2000


# ---------------------------------------------------------------- 多线程


def test_concurrent_sends_get_distinct_records_and_ids(fake):
    results = []
    lock = threading.Lock()

    def work(n):
        outcome = send(fake, f"线程 {n}")
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert len(fake.sent) == 20
    assert sorted(m.index for m in fake.sent) == list(range(1, 21))
    assert sorted(c.index for c in fake.calls) == list(range(1, 21))
    assert len({o.message_id for o in results}) == 20
    assert len({m.text for m in fake.sent}) == 20


def test_pushing_from_many_threads_loses_nothing(fake):
    threads = [
        threading.Thread(target=lambda n=n: fake.push_user_message(f"消息 {n}")) for n in range(20)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    messages = poll(fake).messages
    assert len(messages) == 20 and sorted(m.seq for m in messages) == list(range(1, 21))
