"""阶段 C 的端到端验收：真 uvicorn（随机端口）、真的收消息线程和服务回复线程、进程内假上游，两个
项目（alpha 默认、beta 前缀"贝塔"）用 HTTP 走一遍：

    使用者发话 → 路由 → 取话 → 回复 → 等确认 → 使用者回"对" → 路由到在等确认的项目 → 确认

再看服务自己回的指令和关闭时挂着的取话请求。收消息和服务回复的线程这里手工接在同一个待取队列上
（`hubctl serve` 的接线归阶段 D）。
"""

from __future__ import annotations

import pytest
import requests

from clawbot_hub.poller import Poller
from clawbot_hub.replier import Replier
from tests.support.hub import USER
from tests.support.threads import run_in_thread, wait_for

LONG_WAIT = 30


@pytest.fixture
def chain(make_api, serve):
    """两个项目、真服务、收消息线程和服务回复线程。返回一个小对象，测试用它说话、看假上游。"""
    api = make_api()
    hub = api.hub
    hub.ready("alpha", prefix="量化")
    hub.add("beta", prefix="贝塔", guaranteed=1, cap=2)
    hub.upstream.user_id = USER  # 假上游里的使用者就是凭证文件里的 user_id
    url, _ = serve(api)

    replier = Replier(hub.sender, hub.config.hub.label)
    poller = Poller(
        hub.config,
        hub.clock,
        hub.db,
        hub.upstream,
        hub.store,
        hub.monitor,
        hub.projects,
        hub.quota,
        api.services.inbox,
        replier,
    )
    replier.start()
    poller.start()
    yield Chain(api, url)
    poller.stop(10)
    replier.stop(10)


class Chain:
    def __init__(self, api, url: str) -> None:
        self.api = api
        self.url = url
        self.upstream = api.hub.upstream

    def headers(self, name: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api.key(name)}"}

    def start_fetch(self, name: str, wait: int = LONG_WAIT):
        """项目 `name` 挂上一个取话请求（在线程里），返回它的句柄。"""
        return run_in_thread(
            requests.get,
            f"{self.url}/v1/inbox?wait={wait}",
            headers=self.headers(name),
            timeout=60,
        )

    def reply(self, name: str, handle: str, text: str, **fields):
        return requests.post(
            f"{self.url}/v1/replies",
            json={"handle": handle, "text": text, **fields},
            headers=self.headers(name),
            timeout=20,
        )

    def ack(self, name: str, *ids: str):
        return requests.post(
            f"{self.url}/v1/inbox/ack",
            json={"ids": list(ids)},
            headers=self.headers(name),
            timeout=20,
        )

    def sent(self) -> list[tuple[str, str]]:
        """假上游收到的（对话凭证，文字），按发出的先后。"""
        return [(message.context_token, message.text) for message in self.upstream.sent]


def only_item(response: requests.Response) -> dict:
    assert response.status_code == 200, response.text
    [item] = response.json()["items"]
    return item


def test_two_projects_talk_to_the_user_through_the_whole_chain(chain):
    alpha = chain.start_fetch("alpha")
    beta = chain.start_fetch("beta")
    chain.api.hub.clock.wait_until_blocked(2)

    # 不加前缀的话交给默认项目，贝塔那边的请求继续挂着
    chain.upstream.push_user_message("持仓", context_token="ctx-u1")
    first = only_item(alpha.result(10))
    assert (first["text"], first["via"], first["attempt"]) == ("持仓", "default", 1)
    assert first["has_text"] is True and first["reply_handle"].startswith("rh_")
    assert not beta.done

    # alpha 用句柄回复，要使用者确认。回复用的是使用者那句话的对话凭证；默认项目的文字原样发出，
    # 别的项目前面加“【称呼】”
    asked = chain.reply(
        "alpha", first["reply_handle"], "要平掉 600519 吗？\n回“对”确认", awaiting_confirmation=True
    )
    assert asked.status_code == 200
    assert (asked.json()["status"], asked.json()["parts_sent"]) == ("sent", 1)
    assert chain.sent() == [("ctx-u1", "要平掉 600519 吗？\n回“对”确认")]

    # 使用者回"对"：alpha 在等确认，这句话路由给它
    alpha = chain.start_fetch("alpha")
    chain.api.hub.clock.wait_until_blocked(2)
    chain.upstream.push_user_message("对", context_token="ctx-u2")
    second = only_item(alpha.result(10))
    assert (second["text"], second["via"]) == ("对", "confirm")
    assert not beta.done

    done = chain.reply("alpha", second["reply_handle"], "已平仓，成交价 1500")
    assert done.status_code == 200
    assert chain.sent()[-1] == ("ctx-u2", "已平仓，成交价 1500")

    # 两句话一起确认
    acked = chain.ack("alpha", first["id"], second["id"])
    assert acked.json() == {"acked": 2, "unknown": []}

    # 带前缀的话交给贝塔，贝塔那边挂着的请求这时才返回
    chain.upstream.push_user_message("贝塔 状态", context_token="ctx-u3")
    third = only_item(beta.result(10))
    assert (third["text"], third["via"]) == ("状态", "prefix")
    assert chain.reply("beta", third["reply_handle"], "运行正常").status_code == 200
    assert chain.sent()[-1] == ("ctx-u3", "【贝塔】运行正常")
    assert chain.ack("beta", third["id"]).json() == {"acked": 1, "unknown": []}

    # 确认过的话没有正文了，对话凭证还在（回复句柄没失效）
    rows = (
        chain.api.hub.db.conn().execute("SELECT state, text, context_token FROM inbox").fetchall()
    )
    assert sorted((row["state"], row["text"], row["context_token"]) for row in rows) == [
        ("acked", None, "ctx-u1"),
        ("acked", None, "ctx-u2"),
        ("acked", None, "ctx-u3"),
    ]


def test_the_service_answers_its_own_commands_and_no_project_gets_them(chain):
    alpha = chain.start_fetch("alpha")
    chain.api.hub.clock.wait_until_blocked(1)

    chain.upstream.push_user_message("服务 项目", context_token="ctx-u1")

    wait_for(lambda: len(chain.sent()) == 1)
    [(token, text)] = chain.sent()
    assert token == "ctx-u1"
    assert text.startswith("【服务】现在能找的项目：")
    assert "量化（默认，在线）" in text and "贝塔（离线）" in text
    assert not alpha.done  # 指令是服务自己答的，项目没收到
    chain.upstream.push_user_message("持仓", context_token="ctx-u2")  # 放走挂着的请求
    assert only_item(alpha.result(10))["text"] == "持仓"


def test_a_message_for_a_project_that_is_not_asking_waits_for_it_and_the_user_is_told(chain):
    chain.upstream.push_user_message("贝塔 状态", context_token="ctx-u1")

    wait_for(lambda: len(chain.sent()) == 1)
    [(token, text)] = chain.sent()
    assert token == "ctx-u1"
    assert text.startswith("【服务】")
    assert "贝塔" in text and "没在线" in text  # 提示使用者：话先收着，项目起来就能取

    late = chain.start_fetch("beta", wait=0)
    item = only_item(late.result(10))
    assert (item["text"], item["via"]) == ("状态", "prefix")  # 话还在


def test_a_hanging_request_returns_empty_at_once_when_the_service_starts_shutting_down(chain):
    waiting = chain.start_fetch("alpha")
    chain.api.hub.clock.wait_until_blocked(1)

    chain.api.services.begin_shutdown()

    response = waiting.result(10)
    assert response.status_code == 200
    assert response.json()["items"] == []
    late = chain.start_fetch("alpha", wait=0).result(10)
    assert late.status_code == 503 and late.json()["error"]["code"] == "shutting_down"


def test_a_stranger_cannot_get_through_to_any_project(chain):
    waiting = chain.start_fetch("alpha")
    chain.api.hub.clock.wait_until_blocked(1)

    chain.upstream.push_user_message("我是陌生人", context_token="ctx-s1", from_user_id="stranger")
    chain.upstream.push_user_message("持仓", context_token="ctx-u1")  # 随后使用者本人说话

    item = only_item(waiting.result(10))
    assert (item["text"], item["via"]) == ("持仓", "default")
    texts = [row["text"] for row in chain.api.hub.db.conn().execute("SELECT text FROM inbox")]
    assert texts == ["持仓"]
