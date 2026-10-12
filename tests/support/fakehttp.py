"""假 HTTP 服务的公共部分：假腾讯（`fake_ilink.py`）和假 Bark（`fake_bark.py`）都建在它上面。

真的 HTTP 服务器（本机回环上的 `ThreadingHTTPServer`），所以被测的 `requests` 调用从请求头、请求体、
超时到断连都是真走一遍网络栈。公共的东西：

- 记录收到的每个请求（`RecordedRequest`：方法、路径、查询、请求头、请求体、连接编号）；
- 编排回复（`script` 排一串、`set_default` 一直这样回），回复的种类（`Reply`）：
  正常的 JSON、任意 HTTP 状态码、网关页面、空响应体，以及连接层的故障——
  挂住不回（读超时）、TCP RST、不回话直接关、响应读到一半断开，还可以加一段真实的延迟；
- `close_after_response`：每次回完话悄悄把连接关了，像服务端关掉了空闲连接；
- 停止服务时挂着的处理线程马上放手。

子类实现三件事：`route(path)` 认出哪个端点、`default(端点, 请求)` 默认怎么回、
可选的 `control(请求)`（`/_fake/` 下的控制接口）。
"""

from __future__ import annotations

import dataclasses
import json
import socket
import socketserver
import struct
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, urlsplit

# requests / urllib3 / 浏览器自带的传输层请求头，比较"自定义请求头"时不算
TRANSPORT_HEADERS = frozenset(
    {"host", "content-length", "connection", "user-agent", "accept", "accept-encoding"}
)

# ---------------------------------------------------------------- 回复


@dataclass(frozen=True)
class Reply:
    """一次响应的编排。`action` 决定怎么回：

    - `respond`：按 `status`、`body`、`headers` 回；
    - `default`：走这个端点的默认行为（只想加延迟时用）；
    - `hang`：读完请求就不回了，一直挂着（客户端读超时），`hang_seconds` 后或服务停止时才断开；
    - `reset`：读完请求后发 TCP RST（客户端看到"连接被对方重置"）；
    - `close`：读完请求后不回话直接关连接；
    - `truncate`：声明 `declared` 字节，只发 `body` 的前面一点就断开（"响应读到一半断开"）。
    """

    status: int = 200
    body: Any = None  # dict / list → JSON；str / bytes → 原样；None → 空
    headers: Mapping[str, str] = field(default_factory=dict)
    content_type: str | None = None
    delay: float = 0.0  # 先真实地等这么久再回
    action: str = "respond"
    hang_seconds: float = 30.0
    declared: int = 1000

    def after(self, seconds: float) -> Reply:
        return dataclasses.replace(self, delay=seconds)

    def render(self) -> tuple[bytes, str | None]:
        if self.body is None:
            return b"", None
        if isinstance(self.body, (dict, list)):
            return json.dumps(self.body, ensure_ascii=False).encode(), "application/json"
        if isinstance(self.body, bytes):
            return self.body, self.content_type or "application/octet-stream"
        return str(self.body).encode(), self.content_type or "text/html; charset=utf-8"


def http_status(status: int, body: Any = None) -> Reply:
    """只给 HTTP 状态码（响应体默认是空的）。"""
    return Reply(status=status, body=body)


def html(status: int = 200) -> Reply:
    """网关页面那种非 JSON 的响应体。"""
    return Reply(status=status, body="<html><body>Bad Gateway</body></html>")


def empty(status: int = 200) -> Reply:
    return Reply(status=status, body=None)


def hang(seconds: float = 30.0) -> Reply:
    return Reply(action="hang", hang_seconds=seconds)


def reset() -> Reply:
    return Reply(action="reset")


def close() -> Reply:
    return Reply(action="close")


def truncated(declared: int = 1000, sent: bytes = b'{"ret":0,') -> Reply:
    return Reply(action="truncate", body=sent, declared=declared)


def default_after(seconds: float) -> Reply:
    """先等一会儿，然后按默认行为回。"""
    return Reply(action="default", delay=seconds)


# ---------------------------------------------------------------- 记录


@dataclass
class RecordedRequest:
    index: int  # 在这个假服务收到的（非控制）请求里的序号，从 1 起
    endpoint: str
    method: str
    path: str
    query: dict[str, str]  # 每个键取第一个值
    headers: list[tuple[str, str]]  # 原样（含大小写）
    body: bytes
    connection_id: int  # 同一条 TCP 连接上的请求编号相同
    at: float  # time.monotonic()
    json: Any = None  # 请求体是合法 JSON 时解析出来

    def header(self, name: str) -> str | None:
        for key, value in self.headers:
            if key.lower() == name.lower():
                return value
        return None

    def custom_header_names(self) -> list[str]:
        """去掉传输层自带的头以后剩下的请求头名字（原样大小写）。"""
        return [k for k, _ in self.headers if k.lower() not in TRANSPORT_HEADERS]


# ---------------------------------------------------------------- 服务


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False  # 挂着的处理线程不拦着关服务
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, address: tuple[str, int], fake: FakeHttp) -> None:
        self.fake = fake
        super().__init__(address, _Handler)

    def server_bind(self) -> None:
        # 跳过 getfqdn：有的机器上反查本机名要好几秒
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30  # 空闲的长连接最多留 30 秒
    server: _Server

    def setup(self) -> None:
        super().setup()
        self.connection_id = self.server.fake._next_connection_id()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 沿用基类签名
        pass

    def do_GET(self) -> None:
        self._handle()

    do_POST = do_PUT = do_DELETE = do_PATCH = do_GET

    # ---- 处理 ----

    def _handle(self) -> None:
        fake = self.server.fake
        parts = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        parsed: Any = None
        if body:
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
        request = RecordedRequest(
            index=0,
            endpoint="",
            method=self.command,
            path=parts.path,
            query=dict(reversed(parse_qsl(parts.query, keep_blank_values=True))),
            headers=list(self.headers.items()),
            body=body,
            connection_id=self.connection_id,
            at=time.monotonic(),
            json=parsed,
        )
        if parts.path.startswith("/_fake/"):
            self._perform(fake.control(request), None)
            return
        endpoint = fake.route(parts.path)
        if endpoint is None:
            self._perform(Reply(status=404, body={"errmsg": "not found"}), None)
            return
        request.endpoint = endpoint
        fake._record(request)
        self._perform(fake._scripted(endpoint), (endpoint, request))

    def _perform(self, reply: Reply | None, default_for: tuple[str, RecordedRequest] | None):
        fake = self.server.fake
        if reply is not None and reply.delay > 0 and fake._stopping.wait(reply.delay):
            self.close_connection = True
            return
        if reply is None or reply.action == "default":
            assert default_for is not None
            reply = fake.default(*default_for)
        action = reply.action
        if action == "hang":
            fake._stopping.wait(reply.hang_seconds)
            self.close_connection = True
        elif action == "close":
            self.close_connection = True
        elif action == "reset":
            # 关闭时发 RST 而不是 FIN。读写用的文件对象占着套接字的引用，要先关掉，
            # 否则 close() 不会真的关，而基类收尾时又会先 shutdown（发 FIN）
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            self.close_connection = True
            self.rfile.close()
            self.connection.close()
        elif action == "truncate":
            self.send_response(reply.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(reply.declared))
            self.end_headers()
            data, _ = reply.render()
            self.wfile.write(data)
            self.wfile.flush()
            self.close_connection = True
        else:
            self._send(reply)

    def _send(self, reply: Reply) -> None:
        data, content_type = reply.render()
        self.send_response(reply.status)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for key, value in reply.headers.items():
            self.send_header(key, value)
        if self.server.fake.close_after_response:
            self.close_connection = True  # 不声明 Connection: close：就像服务端悄悄把空闲连接关了
        self.end_headers()
        self.wfile.write(data)


class FakeHttp:
    """假 HTTP 服务的基类。"""

    endpoints: tuple[str, ...] = ()

    def __init__(
        self, *, host: str = "127.0.0.1", port: int = 0, close_after_response: bool = False
    ) -> None:
        self.host = host
        self.close_after_response = close_after_response
        self._requested_port = port
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        # 下面这些都由 _cv 保护；记录、收件箱之类变化时 notify_all
        self._cv = threading.Condition()
        self._requests: list[RecordedRequest] = []
        self._scripts: dict[str, deque[Reply]] = {name: deque() for name in self.endpoints}
        self._defaults: dict[str, Reply | None] = {name: None for name in self.endpoints}
        self._connections = 0

    # ---- 子类实现 ----

    def route(self, path: str) -> str | None:
        raise NotImplementedError

    def default(self, endpoint: str, request: RecordedRequest) -> Reply:
        raise NotImplementedError

    def control(self, request: RecordedRequest) -> Reply:
        return Reply(status=404, body={"error": "没有这个控制接口"})

    # ---- 生命周期 ----

    def start(self):
        if self._server is not None:
            return self
        self._stopping.clear()
        self._server = _Server((self.host, self._requested_port), self)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name=f"{type(self).__name__}",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        server, thread = self._server, self._thread
        if server is None:
            return
        self._server = self._thread = None
        self._stopping.set()
        with self._cv:
            self._cv.notify_all()  # 叫醒挂着的长轮询
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    @property
    def port(self) -> int:
        assert self._server is not None, "假服务还没启动"
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    # ---- 记录 ----

    @property
    def requests(self) -> list[RecordedRequest]:
        with self._cv:
            return list(self._requests)

    def requests_to(self, endpoint: str) -> list[RecordedRequest]:
        return [r for r in self.requests if r.endpoint == endpoint]

    def wait_for_requests(self, endpoint: str, count: int = 1, timeout: float = 5.0) -> None:
        """等这个端点至少收到 `count` 个请求（真实秒）；后台线程在发请求的测试用。"""

        def got() -> int:
            return sum(1 for r in self._requests if r.endpoint == endpoint)

        with self._cv:
            if not self._cv.wait_for(lambda: got() >= count, timeout):
                raise AssertionError(
                    f"{timeout} 秒内 {endpoint} 只收到 {got()} 个请求，要 {count} 个"
                )

    def clear_requests(self) -> None:
        with self._cv:
            self._requests.clear()

    # ---- 编排 ----

    def script(self, endpoint: str, *replies: Reply) -> None:
        """给这个端点排一串回复，来一个请求用掉一个；用完回到默认行为。"""
        with self._cv:
            self._scripts[endpoint].extend(replies)

    def set_default(self, endpoint: str, reply: Reply | None) -> None:
        """这个端点一直这样回，直到换掉（`None` 恢复默认行为）。排好的回复优先。"""
        with self._cv:
            self._defaults[endpoint] = reply

    def clear_scripts(self) -> None:
        with self._cv:
            for queue in self._scripts.values():
                queue.clear()
            self._defaults = {name: None for name in self.endpoints}

    # ---- 内部 ----

    def _next_connection_id(self) -> int:
        with self._cv:
            self._connections += 1
            return self._connections

    def _record(self, request: RecordedRequest) -> None:
        with self._cv:
            request.index = len(self._requests) + 1
            self._requests.append(request)
            self._cv.notify_all()

    def _scripted(self, endpoint: str) -> Reply | None:
        with self._cv:
            if self._scripts[endpoint]:
                return self._scripts[endpoint].popleft()
            return self._defaults[endpoint]
