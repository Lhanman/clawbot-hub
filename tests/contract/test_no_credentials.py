"""合约测试：项目拿到的任何返回里都没有腾讯的凭证（任务 D5，规格 `hub-send`「收件人和对话凭证由服务
选」「脱敏」，`hub-inbox`「回复句柄」）。

项目用自己的密钥调遍所有 `/v1` 接口（成功的和出错的都有），把每一次返回的头和正文记下来：里面不能
出现登录凭证（`bot_token`）、对话凭证（`context_token`）、收消息进度（`get_updates_buf`）的值，也不能
出现这几个字段名。上游报错时带着凭证，返回给项目的原因里是 `***`。收件人和对话凭证由服务选，项目在
请求里写什么都没用。
"""

from __future__ import annotations

import requests

from clawbot_hub.upstream.base import Rejected, Transient
from tests.contract import ENDPOINTS, covers
from tests.support.hub import BOT_TOKEN, USER

# 使用者说“持仓”那句话的对话凭证（它后来被 `flush()` 里那句指令的取代了，窗口里记的是后者）
USER_TOKEN = "CTX-IN-7f3a9d01"
# 窗口一开始记的对话凭证（`Hub.ready()` 里使用者说的第一句话）
FIRST_TOKEN = "ctx-1"
FIELD_NAMES = ("bot_token", "context_token", "get_updates_buf", "updates_buf")


class Wire:
    """用原生 HTTP 调接口，记下每一次返回（不经过客户端，看到的就是线上传的东西）。"""

    def __init__(self, rig) -> None:
        self.rig = rig
        self.responses: list[tuple[str, requests.Response]] = []

    def call(self, method: str, path: str, *, key: str | None = "alpha", **kwargs):
        headers = {}
        if key is not None:
            headers["Authorization"] = f"Bearer {self.rig.key(key)}"
        response = requests.request(
            method, self.rig.url + path, headers=headers, timeout=20, **kwargs
        )
        self.responses.append((f"{method} {path}", response))
        return response

    def everything(self) -> list[tuple[str, str]]:
        """每次返回的（说明，状态行以外的头和正文拼成的文字）。"""
        return [
            (label, f"{dict(response.headers)}\n{response.text}")
            for label, response in self.responses
        ]


def secrets_of(rig, *tokens: str) -> list[str]:
    """项目不该看到的值：登录凭证、对话凭证，和账号文件里现在的收消息进度。"""
    progress = rig.hub.store.read().updates_buf
    assert progress, "收消息线程应当已经把进度写回账号文件"
    return [BOT_TOKEN, FIRST_TOKEN, USER_TOKEN, *tokens, progress]


@covers(*ENDPOINTS)
def test_nothing_a_project_can_call_returns_a_credential(rig):
    wire = Wire(rig)
    rig.present()
    rig.say("持仓", token=USER_TOKEN)
    current = rig.flush().context_token  # 这句话已经进了队，窗口记的对话凭证换成了这句指令的

    wire.call("GET", "/v1/health", key=None)
    wire.call("GET", "/v1/status")
    wire.call("POST", "/v1/messages", json={"text": "盘点完成", "dedupe_key": "k-1"})
    wire.call("POST", "/v1/messages", json={"text": "盘点完成", "dedupe_key": "k-1"})  # 重复
    unreachable = Transient(f"连不上，令牌是 {BOT_TOKEN}，对话是 {current}")
    rig.upstream.script("send_text", unreachable, unreachable, unreachable)
    wire.call("POST", "/v1/messages", json={"text": "会连不上的"})
    rig.upstream.script("send_text", Rejected(200, 500, f"令牌 {BOT_TOKEN} 和 {current} 无效"))
    wire.call("POST", "/v1/messages", json={"text": "会被拒绝的"})

    fetched = wire.call("GET", "/v1/inbox?wait=0&max=10")
    [item] = fetched.json()["items"]
    wire.call("GET", "/v1/inbox?wait=0&max=0")
    wire.call("POST", "/v1/replies", json={"handle": item["reply_handle"], "text": "共 3 只"})
    wire.call("POST", "/v1/replies", json={"handle": "rh_" + "x" * 22, "text": "瞎编的句柄"})
    wire.call("POST", "/v1/inbox/ack", json={"ids": [item["id"], "m_不存在"]})

    # 出错的各种样子
    wire.call("GET", "/v1/status", key=None)
    wire.call("POST", "/v1/messages", json={"text": "没带密钥"}, key=None)
    wire.call("POST", "/v1/messages", data="{不是 JSON".encode())
    wire.call("POST", "/v1/messages", json={"text": "  "})
    wire.call("POST", "/v1/messages", data=b"x" * 70000)
    wire.call("POST", "/v1/replies", json={"handle": 1, "text": "句柄不是字符串"})
    wire.call("POST", "/v1/inbox/ack", json={"ids": "m_1"})
    wire.call("GET", "/v1/inbox?wait=abc")
    wire.call("GET", "/v1/messages")
    wire.call("GET", "/v1/没有这个接口")

    seen = wire.everything()
    assert len(seen) == 21
    assert [response.status_code for _, response in wire.responses][:4] == [200, 200, 200, 200]
    for secret in secrets_of(rig, current):
        leaked = [label for label, text in seen if secret in text]
        assert leaked == [], f"{secret!r} 出现在这些返回里：{leaked}"
    for name in FIELD_NAMES:
        leaked = [label for label, text in seen if name in text]
        assert leaked == [], f"字段名 {name} 出现在这些返回里：{leaked}"


@covers("POST /v1/messages")
def test_a_reason_from_the_upstream_shows_stars_where_the_credentials_were(rig):
    rig.upstream.script("send_text", *[Transient(f"连不上，令牌是 {BOT_TOKEN}")] * 3)

    unreachable = rig.client().send("会连不上的")

    assert unreachable.reason_code == "upstream_unreachable"
    assert unreachable.reason == "微信 ClawBot 发送失败，重试 2 次后仍然失败：连不上，令牌是 ***"

    rig.upstream.script("send_text", Rejected(200, 500, f"令牌 {BOT_TOKEN} 和对话 {FIRST_TOKEN}"))

    rejected = rig.client().send("会被拒绝的")

    assert rejected.reason_code == "upstream_rejected"
    assert rejected.reason == "微信 ClawBot 拒绝：500 令牌 *** 和对话 ***"


@covers("POST /v1/messages", "POST /v1/replies")
def test_the_recipient_and_the_conversation_are_chosen_by_the_service(rig):
    rig.present()
    rig.say("持仓", token=USER_TOKEN)
    current = rig.flush().context_token
    wire = Wire(rig)
    forged = {"to_user_id": "someone-else@im.wechat", "context_token": "forged-ctx"}

    wire.call("POST", "/v1/messages", json={"text": "推送", **forged})
    [item] = rig.take()
    wire.call("POST", "/v1/replies", json={"handle": item.reply_handle, "text": "回复", **forged})

    pushed, replied = rig.upstream.sent[-2:]
    # 推送用使用者最近一句话的对话凭证，回复用被回复那句话的
    assert (pushed.text, pushed.to_user_id, pushed.context_token) == ("推送", USER, current)
    assert (replied.text, replied.to_user_id, replied.context_token) == ("回复", USER, USER_TOKEN)
    assert [response.status_code for _, response in wire.responses] == [200, 200]


@covers("GET /v1/inbox")
def test_a_reply_handle_is_not_made_of_a_credential(rig):
    rig.present()
    rig.say("持仓", token=USER_TOKEN)
    [item] = rig.take()

    assert item.reply_handle.startswith("rh_") and len(item.reply_handle) == 25
    for secret in (USER_TOKEN, FIRST_TOKEN, BOT_TOKEN):
        assert secret not in item.reply_handle
        assert secret[:6] not in item.reply_handle
