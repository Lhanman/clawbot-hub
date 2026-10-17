"""假 Bark 服务（任务 0.5）：本机回环上的 HTTP 服务器，扮演 `https://api.day.app`。

告警（`hub-alerts`）只会 `POST /push`，JSON 里有 `device_key`、`title`、`body`、`group`，
可选 `subtitle`、`level`、`markdown`。默认回真的 Bark 的成功形状；要别的结果用 `script("push", …)`
排一串：`bark_error(…)`（被拒绝）、`http_status(500)`（暂时不通）、`hang()`（很慢）、
`reset()` / `close()` / `truncated()`（连接层的故障），和假腾讯是同一套（见 `fakehttp.py`）。

`valid_keys` 不为空时，不在里面的设备码会被拒绝（HTTP 400，`code: 400`），错误信息里带着设备码——
真的 Bark 就是这么回的，所以告警代码写事件、写日志时必须自己把设备码去掉。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from tests.support.fakehttp import FakeHttp, RecordedRequest, Reply

PUSH = "push"


def bark_ok(timestamp: int = 0) -> Reply:
    return Reply(body={"code": 200, "message": "success", "timestamp": timestamp})


def bark_error(code: int = 400, message: str = "", *, status: int | None = None) -> Reply:
    """被拒绝。`status` 不给就和 `code` 一样（真的 Bark 就是这样）；
    `status=200` 是"HTTP 2xx 但 JSON 里的 code 不是 200"那种。"""
    return Reply(status=status if status is not None else code, body={
        "code": code, "message": message, "timestamp": 0,
    })  # fmt: skip


@dataclass
class Push:
    index: int  # 第几个 /push 请求，从 1 起
    request_index: int
    device_key: str
    title: str
    body: str | None
    group: str | None
    subtitle: str | None
    level: str | None
    markdown: str | None
    raw: dict[str, Any]  # 请求体原样


class FakeBark(FakeHttp):
    endpoints = (PUSH,)

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        valid_keys: Iterable[str] | None = None,
        now_ms: Callable[[], int] | None = None,
        close_after_response: bool = False,
    ) -> None:
        super().__init__(host=host, port=port, close_after_response=close_after_response)
        self.valid_keys = set(valid_keys) if valid_keys is not None else None
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)
        self._pushes: list[Push] = []

    # ---- 记录 ----

    @property
    def pushes(self) -> list[Push]:
        """收到的每个 /push 请求（不管后来回的是成功还是失败，请求内容都收到了）。"""
        with self._cv:
            return list(self._pushes)

    def titles(self) -> list[str]:
        return [p.title for p in self.pushes]

    def clear_requests(self) -> None:
        super().clear_requests()
        with self._cv:
            self._pushes.clear()

    # ---- 怎么回 ----

    def route(self, path: str) -> str | None:
        return PUSH if path == "/push" else None

    def _record(self, request: RecordedRequest) -> None:
        super()._record(request)
        data = request.json if isinstance(request.json, dict) else None
        if data is None:
            return
        with self._cv:
            self._pushes.append(
                Push(
                    index=len(self._pushes) + 1,
                    request_index=request.index,
                    device_key=str(data.get("device_key") or ""),
                    title=str(data.get("title") or ""),
                    body=data.get("body"),
                    group=data.get("group"),
                    subtitle=data.get("subtitle"),
                    level=data.get("level"),
                    markdown=data.get("markdown"),
                    raw=data,
                )
            )

    def default(self, endpoint: str, request: RecordedRequest) -> Reply:
        data = request.json
        if not isinstance(data, dict):
            return bark_error(400, "request body is not valid json")
        key = str(data.get("device_key") or "")
        if not key:
            return bark_error(400, "device key is empty")
        if self.valid_keys is not None and key not in self.valid_keys:
            return bark_error(
                400, f"failed to get device token: failed to get [{key}] device token"
            )
        return bark_ok(self._now_ms() // 1000)
