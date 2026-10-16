"""合约测试：取话、回复、确认这一侧（任务 D5，规格 `hub-client`「合约测试」；`hub-inbox` 的「取话」
「确认」「回复句柄」「等确认」「项目没在线」）。

真 HTTP 服务加单文件客户端：使用者的话从假上游经收消息线程进来，项目用 `run_inbox` 或直接调接口
取话、回复、确认，回复经服务回复线程发回假上游。两个项目：alpha 是默认项目，beta 的前缀是“贝塔”。
服务里的时间是假的（租期、保留期、句柄有效期都由测试 `advance`），客户端的读超时、退避的次数和
心跳是真的。发消息这一侧在 `test_end_to_end.py`。
"""

from __future__ import annotations

import clawbot_hub_client as hub_client
import pytest

from clawbot_hub.config import Config
from tests.contract import covers
from tests.support.hub import tweak
from tests.support.threads import run_in_thread, wait_for

MINUTE = 60


class Script:
    """项目的处理函数：按话的文字查表回复（表里没有的回“好的”），值是（回复，要不要等使用者确认）。
    每次回复的结果记在 `results` 里：记上了，说明这次回复调用已经返回（服务那边也都记完了）。"""

    def __init__(
        self,
        answers: dict[str, tuple[str, bool]] | None = None,
        *,
        confirm_seconds: int | None = None,
    ) -> None:
        self.answers = answers or {}
        self.confirm_seconds = confirm_seconds
        self.results: list[hub_client.SendResult] = []

    def __call__(self, item: hub_client.Inbound, client: hub_client.HubClient) -> None:
        text, waiting = self.answers.get(item.text, ("好的", False))
        result = client.reply(
            item.reply_handle,
            text,
            awaiting_confirmation=waiting,
            confirm_seconds=self.confirm_seconds,
        )
        self.results.append(result)


def offline_notices(rig) -> list[str]:
    return [text for text in rig.hub.texts if "现在没在线" in text]


# ---------------------------------------------------------------- 一路走通


@covers("GET /v1/inbox", "POST /v1/replies", "POST /v1/inbox/ack")
def test_the_users_message_reaches_the_default_project_and_its_reply_goes_back(rig):
    script = Script({"持仓": ("共 3 只", False)})
    alpha = rig.consume("alpha", script)

    said = rig.say("持仓")
    wait_for(lambda: len(script.results) == 1)
    alpha.stop()

    [item] = alpha.seen
    assert (item.text, item.has_text, item.via, item.attempt) == ("持仓", True, "default", 1)
    assert item.at_ms == said.at_ms and item.reply_handle.startswith("rh_")
    assert script.results[0].status == "sent"
    sent = [(message.context_token, message.text) for message in rig.upstream.sent]
    assert sent == [(said.context_token, "共 3 只")]  # 回给使用者那句话的对话
    rig.clock.advance(2 * MINUTE)  # 处理完已经确认：租期过了也不会再给
    assert rig.client().inbox(wait=0) == []


@covers("GET /v1/inbox", "POST /v1/replies")
def test_a_yes_goes_to_the_project_that_asked_for_it_and_not_to_the_default_project(rig):
    beta_script = Script({"平仓": ("要平仓吗？回“对”确认", True), "对": ("已平仓", False)})
    alpha = rig.consume("alpha")
    beta = rig.consume("beta", beta_script)

    rig.say("贝塔 平仓")
    wait_for(lambda: len(beta_script.results) == 1)  # 回复发完、等确认的标记也记上了
    rig.say("对")
    wait_for(lambda: len(beta_script.results) == 2)
    rig.say("对")  # 这回没有项目在等：原样交给默认项目
    wait_for(lambda: alpha.texts == ["对"])
    alpha.stop()
    beta.stop()

    assert [(item.text, item.via) for item in beta.seen] == [("平仓", "prefix"), ("对", "confirm")]
    assert [(item.text, item.via) for item in alpha.seen] == [("对", "default")]
    assert rig.hub.texts == ["【贝塔】要平仓吗？回“对”确认", "【贝塔】已平仓"]


@covers("GET /v1/inbox")
def test_each_project_only_gets_its_own_messages_and_the_prefix_is_taken_off(rig):
    alpha = rig.consume("alpha")
    beta = rig.consume("beta")

    rig.say("持仓")
    rig.say("贝塔 状态")
    rig.say("贝塔：再看一眼")
    wait_for(lambda: alpha.texts == ["持仓"] and beta.texts == ["状态", "再看一眼"])
    alpha.stop()
    beta.stop()

    assert [item.via for item in alpha.seen] == ["default"]
    assert [item.via for item in beta.seen] == ["prefix", "prefix"]


@covers("GET /v1/inbox")
def test_a_picture_arrives_as_an_empty_text_that_says_it_has_none(rig):
    alpha = rig.consume("alpha")

    rig.upstream.push_user_message("", has_text=False)
    wait_for(lambda: len(alpha.seen) == 1)
    alpha.stop()

    [item] = alpha.seen
    assert (item.text, item.has_text, item.via) == ("", False, "default")


# ---------------------------------------------------------------- 租期和确认


@covers("GET /v1/inbox")
def test_a_message_nobody_acknowledged_is_given_again_with_the_same_handle_after_the_lease(rig):
    rig.present()
    rig.say("持仓")
    [first] = rig.take()
    client = rig.client()

    assert client.inbox(wait=0) == []  # 租期里不会再给
    rig.clock.advance(61)  # 租期 60 秒
    [again] = client.inbox(wait=0)

    assert (first.attempt, again.attempt) == (1, 2)
    assert (again.id, again.reply_handle) == (first.id, first.reply_handle)


@covers("GET /v1/inbox")
def test_a_message_given_five_times_without_an_acknowledgement_is_not_given_again(rig):
    rig.present()
    rig.say("持仓")
    attempts = []
    for _ in range(5):
        [item] = rig.take()
        attempts.append(item.attempt)
        rig.clock.advance(61)

    assert attempts == [1, 2, 3, 4, 5]
    assert rig.client().inbox(wait=0) == []


@covers("GET /v1/inbox")
def test_a_heartbeat_keeps_the_project_online_without_taking_any_message(rig):
    rig.present()
    rig.say("持仓")
    rig.flush()  # 这句话已经进了队

    assert rig.client().inbox(wait=0, max=0) == []

    [item] = rig.take()
    assert item.text == "持仓" and item.attempt == 1  # 心跳没有租走它


@covers("GET /v1/inbox")
def test_max_limits_how_many_messages_one_fetch_returns(rig):
    rig.present()
    for text in ("一", "二", "三"):
        rig.say(text)
    rig.flush()
    client = rig.client()

    assert [item.text for item in client.inbox(wait=0, max=2)] == ["一", "二"]
    assert [item.text for item in client.inbox(wait=0, max=2)] == ["三"]


@covers("POST /v1/inbox/ack")
def test_acknowledging_twice_or_acknowledging_what_is_not_yours_changes_nothing(rig):
    rig.present()
    rig.say("持仓")
    [item] = rig.take()
    alpha, beta = rig.client("alpha"), rig.client("beta")

    assert beta.ack([item.id]) == 0  # 别的项目的话
    assert alpha.ack(["m_不存在"]) == 0
    assert alpha.ack([item.id]) == 1
    assert alpha.ack([item.id]) == 0  # 第二次
    rig.clock.advance(2 * MINUTE)
    assert alpha.inbox(wait=0) == []


@covers("POST /v1/inbox/ack")
def test_more_than_fifty_ids_are_acknowledged_in_batches(rig):
    assert (
        rig.client().ack([f"m_{n}" for n in range(120)]) == 0
    )  # 服务一次只收 50 个，客户端自己分批
    assert rig.client().ack([]) == 0


@covers("GET /v1/inbox", "POST /v1/inbox/ack")
def test_a_message_whose_handler_failed_comes_again_and_is_handled_again(rig):
    calls = []

    def handler(item, client):
        calls.append(item)
        if len(calls) == 1:
            raise RuntimeError("第一次处理不了")
        client.reply(item.reply_handle, "这回好了")

    alpha = rig.consume("alpha", handler)
    rig.say("持仓")
    wait_for(lambda: len(calls) == 1)
    wait_for(lambda: rig.clock.blocked_count >= 1)  # 失败的这一项没确认，循环又挂在长轮询上
    rig.clock.advance(61)  # 租期 60 秒
    wait_for(lambda: len(calls) == 2)
    wait_for(lambda: rig.hub.texts == ["这回好了"])
    alpha.stop()

    first, second = calls
    assert (first.attempt, second.attempt) == (1, 2)
    assert (second.id, second.reply_handle) == (first.id, first.reply_handle)


@covers("POST /v1/inbox/ack")
def test_an_acknowledgement_the_service_could_not_take_is_retried_and_not_lost(rig):
    def finish_while_the_service_goes_down(item, client):
        client.reply(item.reply_handle, "做完了")
        rig.api.services.shutting_down.set()  # 确认的时候服务正在关闭

    def bring_the_service_back_at_the_second_pause(pauses):
        if len(pauses) >= 2:
            rig.api.services.shutting_down.clear()

    alpha = rig.consume(
        "alpha",
        finish_while_the_service_goes_down,
        on_pause=bring_the_service_back_at_the_second_pause,
    )
    rig.say("持仓")
    wait_for(lambda: len(alpha.pauses) >= 2)
    rig.clock.wait_until_blocked(1)  # 确认成功了，循环回到长轮询
    rig.clock.advance(2 * MINUTE)  # 租期早过了：没确认成功的话这时会再给一次
    alpha.stop()

    assert [pause for pause in alpha.pauses if pause != 1.0][:2] == [5.0, 10.0]
    assert alpha.texts == ["持仓"]  # 只处理过一次
    assert rig.hub.texts == ["做完了"]


# ---------------------------------------------------------------- 项目没在线、处理慢指令


@covers("GET /v1/inbox")
def test_a_message_for_a_project_that_is_not_there_is_kept_and_the_user_is_told(rig):
    rig.say("贝塔 状态")
    wait_for(lambda: len(rig.hub.texts) == 1)
    assert "「贝塔」现在没在线" in rig.hub.texts[0] and "10 分钟" in rig.hub.texts[0]

    rig.clock.advance(5 * MINUTE)  # 它 5 分钟后回来
    [item] = rig.take("beta")

    assert (item.text, item.attempt, item.via) == ("状态", 1, "prefix")


@covers("GET /v1/inbox")
def test_a_message_the_project_came_back_too_late_for_is_dropped(rig):
    rig.say("贝塔 状态")
    rig.flush()

    rig.clock.advance(11 * MINUTE)

    assert rig.client("beta").inbox(wait=0) == []


@covers("GET /v1/inbox")
def test_a_slow_command_does_not_make_the_project_look_offline(make_rig):
    rig = make_rig(tweak(Config(), inbox={"online_seconds": 1}))

    def slow_command(item, client):
        if item.text != "慢指令":
            return
        for _ in range(10):  # 处理了假时间的 5 秒，是“在线”判断期限的 5 倍
            rig.clock.advance(0.5)
            now = rig.clock.now_ms()
            # 心跳线程每发一次，服务里的“最近取话”就更新到当时的假时间
            wait_for(lambda now=now: rig.hub.projects.get("alpha").last_inbox_ms == now)
        rig.say("在吗")  # 处理期间使用者又发了一句
        rig.flush()

    alpha = rig.consume("alpha", slow_command, heartbeat_seconds=0.01)
    rig.say("慢指令")
    wait_for(lambda: alpha.texts == ["慢指令", "在吗"])  # 新话进了队，处理完第一句后取到
    alpha.stop()

    assert offline_notices(rig) == []


@covers("GET /v1/inbox")
def test_without_heartbeats_a_slow_command_does_make_the_project_look_offline(make_rig):
    rig = make_rig(tweak(Config(), inbox={"online_seconds": 1}))

    def slow_command(item, client):
        if item.text != "慢指令":
            return
        rig.clock.advance(5)  # 这段时间里没有任何取话、确认或心跳
        rig.say("在吗")
        rig.flush()

    alpha = rig.consume("alpha", slow_command)  # 心跳间隔很大，等于没有
    rig.say("慢指令")
    wait_for(lambda: alpha.texts == ["慢指令", "在吗"])
    alpha.stop()

    [notice] = offline_notices(rig)
    assert "「alpha」" in notice


# ---------------------------------------------------------------- 一个项目一个取话循环、停用


@covers("GET /v1/inbox")
def test_a_second_fetch_for_the_same_project_pushes_the_first_one_out(rig):
    first = run_in_thread(rig.client().inbox, wait=25)
    rig.clock.wait_until_blocked(1)
    second = run_in_thread(rig.client().inbox, wait=25)

    assert first.result() == []  # 旧的马上返回空列表
    assert second.still_blocked()
    rig.say("持仓")
    [item] = second.result()
    assert item.text == "持仓"


@covers("GET /v1/inbox")
def test_run_inbox_ends_with_an_auth_error_when_the_project_is_disabled(rig):
    rig.hub.projects.disable("alpha")

    with pytest.raises(hub_client.HubAuthError) as info:
        rig.client().run_inbox(
            lambda item: None, wait=0, sleep=lambda seconds: None, heartbeat_seconds=3600.0
        )

    assert (info.value.code, info.value.http_status) == ("project_disabled", 403)


@covers("GET /v1/inbox")
def test_a_running_loop_learns_that_its_project_was_disabled(rig):
    alpha = rig.consume("alpha")

    rig.hub.projects.disable("alpha")
    rig.clock.advance(6)  # 长轮询每 5 秒核对一次启用状态
    failure = alpha.failure()

    assert isinstance(failure, hub_client.HubAuthError)
    assert (failure.code, failure.http_status) == ("project_disabled", 403)


# ---------------------------------------------------------------- 回复句柄


@covers("POST /v1/replies")
def test_another_projects_handle_and_a_made_up_handle_look_the_same(rig):
    rig.present()
    rig.say("持仓")
    [item] = rig.take()

    errors = []
    for handle in (item.reply_handle, "rh_" + "x" * 22):
        with pytest.raises(hub_client.HubRequestError) as info:
            rig.client("beta").reply(handle, "冒充")
        errors.append((info.value.code, info.value.http_status, info.value.message))

    assert errors[0][:2] == ("unknown_handle", 404)
    assert errors[0] == errors[1]
    assert rig.hub.texts == []


@covers("POST /v1/replies")
def test_a_handle_works_for_thirty_minutes_after_the_message_and_then_it_is_gone(rig):
    rig.present()
    rig.say("持仓")
    [item] = rig.take()
    client = rig.client()

    rig.clock.advance(29 * MINUTE)
    assert client.reply(item.reply_handle, "还来得及").status == "sent"
    rig.clock.advance(2 * MINUTE)
    with pytest.raises(hub_client.HubRequestError) as info:
        client.reply(item.reply_handle, "太晚了")

    assert (info.value.code, info.value.http_status) == ("handle_expired", 410)
    assert rig.hub.texts == ["还来得及"]


@covers("POST /v1/replies")
def test_a_handle_takes_five_replies_and_the_sixth_is_refused(rig):
    rig.present()
    rig.say("持仓")
    [item] = rig.take()
    client = rig.client()

    for number in range(1, 6):
        assert client.reply(item.reply_handle, f"第 {number} 句").status == "sent"
    with pytest.raises(hub_client.HubRequestError) as info:
        client.reply(item.reply_handle, "第 6 句")

    assert (info.value.code, info.value.http_status) == ("reply_limit", 429)
    assert info.value.retry_after is not None and info.value.retry_after > 0
    assert len(rig.hub.texts) == 5


@covers("POST /v1/replies")
def test_a_reply_that_cannot_be_sent_is_a_result_and_not_an_exception(rig):
    rig.present()
    rig.say("持仓")
    [item] = rig.take()
    rig.upstream.set_mode("disconnect")

    result = rig.client().reply(item.reply_handle, "回不去")

    assert (result.status, result.needs_fallback) == ("failed", True)
    assert result.reason_code == "upstream_unreachable"


@covers("POST /v1/replies")
def test_a_reply_with_a_bad_field_is_refused_before_anything_is_sent(rig):
    rig.present()
    rig.say("持仓")
    [item] = rig.take()

    with pytest.raises(hub_client.HubRequestError) as info:
        rig.client().reply(item.reply_handle, "   ")

    assert (info.value.code, info.value.http_status) == ("empty_text", 400)
    assert rig.hub.texts == []
    # 还没发出去，这个句柄的次数也没用掉
    assert rig.client().reply(item.reply_handle, "好的").status == "sent"


@covers("POST /v1/replies")
def test_the_wait_for_a_yes_runs_out_after_the_seconds_the_reply_asked_for(rig):
    beta_script = Script({"平仓": ("要平仓吗？", True)}, confirm_seconds=30)
    alpha = rig.consume("alpha")
    rig.consume("beta", beta_script)

    rig.say("贝塔 平仓")
    wait_for(lambda: len(beta_script.results) == 1)
    rig.clock.advance(31)  # 比 confirm_seconds 多一秒
    rig.say("对")
    wait_for(lambda: alpha.texts == ["对"])
    alpha.stop()

    assert [item.via for item in alpha.seen] == ["default"]  # 标记过期了：当作没有项目在等
