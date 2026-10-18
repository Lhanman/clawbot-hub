"""假腾讯服务（任务 0.4）：本机回环上的 HTTP 服务器，扮演 `ilinkai.weixin.qq.com`。

两种用法：

1. 测试里当库用：`with FakeIlink() as fake: ...`（`tests/conftest.py` 里有 `fake_ilink` 夹具）。
   它是真的 HTTP 服务，所以 `upstream/http.py` 的请求头、请求体、超时、断连这些都是真走一遍网络栈。
2. 独立运行：`uv run python -m tests.support.fake_ilink --port 9000`，给阶段 D 的演练和 E3 用。
   同一个端口上另开 `/_fake/` 控制接口（见 `FakeIlink._control`）。

它只模拟"协议"，不模拟腾讯的额度和窗口规则（那些规则本来就不是官方的）：要测被拒绝，
就编排一个拒绝的回复（`error(...)`）。

## 编排

- `script(端点, 回复, ...)`：给这个端点排一串回复，来一个请求用掉一个；用完回到默认行为。
- `set_default(端点, 回复)`：这个端点一直这样回，直到换掉（`None` 恢复）。
- `set_mode(名字)`：`normal` / `disconnect` / `session_expired` / `http_5xx`，
  对用到 `bot_token` 的端点生效。
- 回复由 `ok`、`error`、`session_expired`、`http_status`、`html`、`empty`、`hang`、`reset`、
  `close`、`truncated` 构造，`.after(秒)` 加一段（真实的）延迟。

## 收消息的位置模型

收件箱是一个只增不减的列表。`get_updates_buf` 是 `buf-<已收到的条数>`：带它来就从那个位置往后给，
所以拿旧的 buf 再来会收到同一批（`redeliver=False` 时改成已经给过的不再给）。空串从最近一次
扫码确认的位置开始（新登录只看得到新消息）。长轮询：没有新消息就挂住最多
`longpoll_seconds`（真实秒）。
"""

from __future__ import annotations

import argparse
import signal
import ssl
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from tests.support.fakehttp import (  # noqa: F401 - 其中一部分是给测试用的再导出
    TRANSPORT_HEADERS,
    FakeHttp,
    RecordedRequest,
    Reply,
    close,
    default_after,
    empty,
    hang,
    html,
    http_status,
    reset,
    truncated,
)

GETUPDATES = "getupdates"
SENDMESSAGE = "sendmessage"
GET_BOT_QRCODE = "get_bot_qrcode"
GET_QRCODE_STATUS = "get_qrcode_status"
NOTIFYSTART = "notifystart"
NOTIFYSTOP = "notifystop"
# 根地址 `/`：`hubctl doctor --online` 不带凭证访问它，只看连得上连不上。默认回一个 404 页面
# （真实的服务根地址回什么不重要），编排时和别的端点一样用
ROOT = "root"
API_ENDPOINTS = (
    GETUPDATES,
    SENDMESSAGE,
    GET_BOT_QRCODE,
    GET_QRCODE_STATUS,
    NOTIFYSTART,
    NOTIFYSTOP,
)
ENDPOINTS = (*API_ENDPOINTS, ROOT)
# 带着 bot_token 来的端点；`set_mode` 只影响这些（扫码要始终能用，否则掉线以后没法重新绑定）
BOT_TOKEN_ENDPOINTS = (GETUPDATES, SENDMESSAGE, NOTIFYSTART, NOTIFYSTOP)
_PATHS = {f"/ilink/bot/{name}": name for name in API_ENDPOINTS}
_PATHS["/"] = ROOT
# notifystart / notifystop 的路径：E4（2026-10-18）在真账号上量过，带 `/msg` 的可用（200，ret 0），
# 不带的回 404。两种都收、都记在 notifystart / notifystop 名下（可以编排），但不带 `/msg` 的默认回
# 404，和真的一样——生产代码走错了路径，测试里会看到警告
_PATHS["/ilink/bot/msg/notifystart"] = NOTIFYSTART
_PATHS["/ilink/bot/msg/notifystop"] = NOTIFYSTOP

DEFAULT_USER_ID = "fake-owner@im.wechat"
DEFAULT_BOT_ID = "fake-bot@im.bot"

_AUTO: Any = object()  # "不指定"的哨兵，区别于传 None（= 没有对话凭证）


# ---------------------------------------------------------------- 回复


def ok(**fields: Any) -> Reply:
    """HTTP 200，`{"ret": 0, "errmsg": ""}` 加上给的字段；没给消息编号就没有消息编号。"""
    return Reply(body={"ret": 0, "errmsg": "", **fields})


def error(ret: int, errmsg: str = "", *, key: str = "ret", status: int = 200) -> Reply:
    """业务错误：`{"ret": 40001, "errmsg": "bad token"}`；`key="errcode"` 时用 errcode 表示。"""
    return Reply(status=status, body={key: ret, "errmsg": errmsg})


def _not_found_page() -> Reply:
    return Reply(status=404, body="<html><body>Not Found</body></html>")


def session_expired(*, status: int = 200, key: str = "ret") -> Reply:
    """登录失效（-14）。`status` 可以是 401、502 之类，用来测"响应体里带 -14"的各种情形。"""
    return error(-14, "session expired", key=key, status=status)


MODES: dict[str, Reply | None] = {
    "normal": None,
    "disconnect": close(),
    "session_expired": session_expired(),
    "http_5xx": http_status(503),
}


# ---------------------------------------------------------------- 记录


@dataclass
class SentMessage:
    index: int  # 第几条 sendmessage，从 1 起
    request_index: int
    to_user_id: str
    client_id: str
    context_token: str | None
    text: str  # 文字项拼起来
    msg: dict[str, Any]  # 请求里的 msg 原样


@dataclass
class _Qr:
    code: str
    number: int
    issued_at: float
    plan: list[Any] | None
    calls: int = 0
    confirmed: dict[str, Any] | None = None


# ---------------------------------------------------------------- 服务


class FakeIlink(FakeHttp):
    endpoints = ENDPOINTS

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        user_id: str = DEFAULT_USER_ID,
        bot_id: str = DEFAULT_BOT_ID,
        now_ms: Callable[[], int] | None = None,
        longpoll_seconds: float = 0.3,
        longpoll_hint_ms: int | None = None,
        message_ids: bool = True,
        message_id_key: str = "message_id",
        close_after_response: bool = False,
        redeliver: bool = True,
        confirm_after: float = 0.0,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        super().__init__(
            host=host,
            port=port,
            close_after_response=close_after_response,
            ssl_context=ssl_context,
        )
        self.user_id = user_id
        self.bot_id = bot_id
        self.longpoll_seconds = longpoll_seconds
        self.longpoll_hint_ms = longpoll_hint_ms
        self.message_ids = message_ids
        self.message_id_key = message_id_key
        self.redeliver = redeliver
        self.confirm_after = confirm_after
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)
        self._sent: list[SentMessage] = []
        self._inbox: list[dict[str, Any]] = []
        self._floor = 0  # 空 buf 从这里开始（最近一次扫码确认时的收件箱长度）
        self._delivered = 0  # 已经给出去过的最大位置（redeliver=False 用）
        self._message_id_counter = 0
        self._qr_plans: deque[list[Any]] = deque()
        self._qr_issued = 0
        self._qr: dict[str, _Qr] = {}
        self.logins = 0  # 扫码确认过几次

    # ---- 记录 ----

    @property
    def sent(self) -> list[SentMessage]:
        with self._cv:
            return list(self._sent)

    def sent_texts(self) -> list[str]:
        return [m.text for m in self.sent]

    def clear_requests(self) -> None:
        super().clear_requests()
        with self._cv:
            self._sent.clear()

    # ---- 编排 ----

    def set_mode(self, mode: str) -> None:
        """`normal` / `disconnect` / `session_expired` / `http_5xx`，对带 bot_token 的端点生效。"""
        if mode not in MODES:
            raise ValueError(f"不认识的模式 {mode!r}，可选：{', '.join(MODES)}")
        for endpoint in BOT_TOKEN_ENDPOINTS:
            self.set_default(endpoint, MODES[mode])

    def clear_scripts(self) -> None:
        super().clear_scripts()
        with self._cv:
            self._qr_plans.clear()

    # ---- 收件箱 ----

    def push_user_message(
        self,
        text: str = "",
        *,
        context_token: str | None = _AUTO,
        create_time_ms: int | None = None,
        from_user_id: str | None = None,
        items: list[dict[str, Any]] | None = None,
        message_type: int = 1,
    ) -> dict[str, Any]:
        """使用者发来一条消息，返回放进收件箱的那条消息（JSON 原样）。

        `context_token` 不给就自动编一个；传 `None` 表示这条消息没有对话凭证。
        `items` 不给就是一个文字项；要图片之类自己传 `[{"type": 2, ...}]`。"""
        with self._cv:
            number = len(self._inbox) + 1
            self._message_id_counter += 1
            if context_token is _AUTO:
                context_token = f"ctx-{number}"
            msg: dict[str, Any] = {
                "seq": number,
                "message_id": 7_000_000 + self._message_id_counter,
                "from_user_id": from_user_id if from_user_id is not None else self.user_id,
                "to_user_id": self.bot_id,
                "client_id": "",
                "create_time_ms": create_time_ms if create_time_ms is not None else self._now_ms(),
                "message_type": message_type,
                "message_state": 2,
                "item_list": items
                if items is not None
                else [{"type": 1, "text_item": {"text": text}}],
            }
            if context_token is not None:
                msg["context_token"] = context_token
            self._inbox.append(msg)
            self._cv.notify_all()
            return dict(msg)

    def push_bot_message(self, text: str = "") -> dict[str, Any]:
        """机器人自己发的消息（`message_type` 为 2）也会出现在收件箱里时的样子。"""
        return self.push_user_message(
            text, message_type=2, from_user_id=self.bot_id, context_token=None
        )

    @property
    def inbox_size(self) -> int:
        with self._cv:
            return len(self._inbox)

    # ---- 扫码 ----

    def qr_plan(self, *sequences: list[Any]) -> None:
        """给接下来取的第 1、2、3…… 张二维码各排一串状态；每串里每次查询用掉一个，
        用完以后一直重复最后一个。条目是状态名，或带额外字段的字典
        （`{"status": "scaned_but_redirect", "redirect_host": "…"}`）。
        没排到的二维码走默认：`confirm_after` 秒以内 `wait`，之后 `confirmed`。"""
        with self._cv:
            self._qr_plans.extend(list(s) for s in sequences)

    # ---- 内部：请求怎么回 ----

    def route(self, path: str) -> str | None:
        return _PATHS.get(path)

    def default(self, endpoint: str, request: RecordedRequest) -> Reply:
        handler = {
            GETUPDATES: self._getupdates,
            SENDMESSAGE: self._sendmessage,
            GET_BOT_QRCODE: self._get_bot_qrcode,
            GET_QRCODE_STATUS: self._get_qrcode_status,
            NOTIFYSTART: self._notify,
            NOTIFYSTOP: self._notify,
            ROOT: lambda _r: _not_found_page(),
        }[endpoint]
        return handler(request)

    def _notify(self, request: RecordedRequest) -> Reply:
        return ok() if request.path.startswith("/ilink/bot/msg/") else _not_found_page()

    def _getupdates(self, request: RecordedRequest) -> Reply:
        body = request.json if isinstance(request.json, dict) else {}
        buf = body.get("get_updates_buf") or ""
        position = self._floor
        if isinstance(buf, str) and buf.startswith("buf-") and buf[4:].isdigit():
            position = int(buf[4:])
        deadline = time.monotonic() + self.longpoll_seconds
        with self._cv:
            start = position if self.redeliver else max(position, self._delivered)
            while len(self._inbox) <= start and not self._stopping.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cv.wait(remaining)
            messages = [dict(m) for m in self._inbox[start:]]
            end = len(self._inbox)
            self._delivered = max(self._delivered, end)
        reply: dict[str, Any] = {
            "ret": 0,
            "msgs": messages,
            "get_updates_buf": f"buf-{max(end, position) if not messages else end}",
        }
        if self.longpoll_hint_ms is not None:
            reply["longpolling_timeout_ms"] = self.longpoll_hint_ms
        return Reply(body=reply)

    def _sendmessage(self, request: RecordedRequest) -> Reply:
        data = request.json if isinstance(request.json, dict) else {}
        msg = data.get("msg") if isinstance(data.get("msg"), dict) else {}
        texts = [
            (item.get("text_item") or {}).get("text") or ""
            for item in msg.get("item_list") or []
            if isinstance(item, dict) and item.get("type") == 1
        ]
        with self._cv:
            self._sent.append(
                SentMessage(
                    index=len(self._sent) + 1,
                    request_index=request.index,
                    to_user_id=msg.get("to_user_id", ""),
                    client_id=msg.get("client_id", ""),
                    context_token=msg.get("context_token"),
                    text="".join(texts),
                    msg=msg,
                )
            )
            self._message_id_counter += 1
            number = 9_000_000 + self._message_id_counter
        extra = {self.message_id_key: number} if self.message_ids else {}
        return ok(**extra)

    def _get_bot_qrcode(self, request: RecordedRequest) -> Reply:
        with self._cv:
            self._qr_issued += 1
            number = self._qr_issued
            code = f"fake-qr-{number}"
            plan = self._qr_plans.popleft() if self._qr_plans else None
            self._qr[code] = _Qr(code, number, time.monotonic(), plan)
        return Reply(
            body={
                "qrcode": code,
                "qrcode_img_content": f"https://liteapp.weixin.qq.com/q/fake?qrcode={code}&bot_type=3",
            }
        )

    def _get_qrcode_status(self, request: RecordedRequest) -> Reply:
        with self._cv:
            qr = self._qr.get(request.query.get("qrcode", ""))
            if qr is None:
                return Reply(body={"status": "expired"})
            if qr.confirmed is not None:
                return Reply(body=dict(qr.confirmed))
            if qr.plan:
                entry = qr.plan[min(qr.calls, len(qr.plan) - 1)]
            elif time.monotonic() - qr.issued_at >= self.confirm_after:
                entry = "confirmed"
            else:
                entry = "wait"
            qr.calls += 1
            body = {"status": entry} if isinstance(entry, str) else dict(entry)
            if body.get("status") == "confirmed":
                self.logins += 1
                body.setdefault("bot_token", f"fake-token-{self.logins}")
                body.setdefault("ilink_bot_id", self.bot_id)
                body.setdefault("ilink_user_id", self.user_id)
                body.setdefault("baseurl", self.base_url)
                self._floor = len(self._inbox)  # 新登录只看得到新消息
                qr.confirmed = dict(body)
            return Reply(body=body)

    # ---- 内部：/_fake/ 控制接口 ----

    def control(self, request: RecordedRequest) -> Reply:
        """独立运行时给人和脚本用的控制接口（同一个端口）：

        - `POST /_fake/inbox`：`{"text": "…"}`（可选 `context_token`、`from_user_id`、
          `create_time_ms`）放一条使用者消息；
        - `GET /_fake/sent?after=N`：看收到的 `sendmessage`（第 N 条以后的）；
        - `POST /_fake/mode`：`{"mode": "normal|disconnect|session_expired|http_5xx"}`。
        """
        route = (request.method, request.path)
        if route == ("POST", "/_fake/inbox"):
            if not isinstance(request.json, dict):
                return Reply(status=400, body={"error": "请求体要是 JSON 对象"})
            allowed = {"context_token", "from_user_id", "create_time_ms"}
            extra = {k: v for k, v in request.json.items() if k in allowed}
            msg = self.push_user_message(str(request.json.get("text", "")), **extra)
            return Reply(
                body={"ok": True, "seq": msg["seq"], "context_token": msg.get("context_token")}
            )
        if route == ("GET", "/_fake/sent"):
            after = int(request.query.get("after", "0") or 0)
            sent = [
                {
                    "index": m.index,
                    "to_user_id": m.to_user_id,
                    "client_id": m.client_id,
                    "context_token": m.context_token,
                    "text": m.text,
                }
                for m in self.sent
                if m.index > after
            ]
            return Reply(body={"sent": sent})
        if route == ("POST", "/_fake/mode"):
            mode = request.json.get("mode") if isinstance(request.json, dict) else None
            if mode not in MODES:
                return Reply(status=400, body={"error": f"mode 要是 {', '.join(MODES)} 之一"})
            self.set_mode(mode)
            return Reply(body={"ok": True, "mode": mode})
        return Reply(status=404, body={"error": "没有这个控制接口"})


# ---------------------------------------------------------------- 独立运行


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.support.fake_ilink",
        description="独立运行的假腾讯服务（演练和真机前的联调用）；同一个端口上有 /_fake/ 控制接口",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="0 = 自动选一个，启动时打印出来")
    parser.add_argument("--confirm-after", type=float, default=3.0, help="取码后几秒自动变成已确认")
    parser.add_argument("--longpoll", type=float, default=25.0, help="收消息最多挂住几秒")
    parser.add_argument("--no-message-id", action="store_true", help="发送成功的返回里不带消息编号")
    parser.add_argument(
        "--close-after-response", action="store_true", help="每次回完话就悄悄关连接"
    )
    args = parser.parse_args(argv)

    fake = FakeIlink(
        host=args.host,
        port=args.port,
        confirm_after=args.confirm_after,
        longpoll_seconds=args.longpoll,
        message_ids=not args.no_message_id,
        close_after_response=args.close_after_response,
    )
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):  # 先接信号再报"在听了"：看到那一行就能放心地停它
        signal.signal(sig, lambda *_: stop.set())
    fake.start()
    print(f"fake_ilink listening on {fake.base_url}", flush=True)
    stop.wait()
    fake.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
