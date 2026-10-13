"""假的 HTTP 代理（本机回环上的裸套接字），测"经代理发请求"时请求到底发出去没有。

`requests` 会读环境变量 `HTTP_PROXY` / `HTTPS_PROXY`：https 的目标先对代理发一个 `CONNECT` 建隧道，
明文 http 的目标把完整地址写在请求行里交给代理。三种代理各演一种情形：

- `RefusingProxy`：对什么都回 403，隧道建不起来，我们的请求根本没发出去；
- `TunnelProxy`：`CONNECT` 之后把字节原样转给指定的本机端口（假腾讯），不管客户端想连谁；
- `SwallowingProxy`：把整个请求读完就关连接，不回话：请求发出去了，结果不知道。
"""

from __future__ import annotations

import socket
import threading


class _Proxy:
    def __init__(self) -> None:
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self.port: int = self._listener.getsockname()[1]
        self.connects = 0  # 收到几个 CONNECT
        self.request_lines: list[str] = []  # 每个请求的第一行
        self._lock = threading.Lock()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        with client:
            head = _read_head(client)
            if not head:
                return
            first = head.split(b"\r\n", 1)[0].decode("latin-1")
            with self._lock:
                self.request_lines.append(first)
                if first.startswith("CONNECT"):
                    self.connects += 1
            self.respond(client, head)

    def respond(self, client: socket.socket, head: bytes) -> None:
        raise NotImplementedError

    def close(self) -> None:
        self._listener.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _read_head(client: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = client.recv(4096)
        if not chunk:
            return b""
        data += chunk
    return data


def _declared_length(head: bytes) -> int:
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            return int(value.strip() or 0)
    return 0


class RefusingProxy(_Proxy):
    def respond(self, client: socket.socket, head: bytes) -> None:
        client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")


class SwallowingProxy(_Proxy):
    def respond(self, client: socket.socket, head: bytes) -> None:
        wanted = _declared_length(head) - (len(head) - head.index(b"\r\n\r\n") - 4)
        while wanted > 0:  # 把请求体也读完，"发出去了"才算数
            chunk = client.recv(min(wanted, 4096))
            if not chunk:
                return
            wanted -= len(chunk)


class TunnelProxy(_Proxy):
    def __init__(self, target_port: int) -> None:
        self._target_port = target_port
        super().__init__()

    def respond(self, client: socket.socket, head: bytes) -> None:
        if not head.startswith(b"CONNECT"):
            client.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            return
        with socket.create_connection(("127.0.0.1", self._target_port)) as target:
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            back = threading.Thread(target=_pump, args=(target, client), daemon=True)
            back.start()
            _pump(client, target)
            back.join(timeout=5)


def _pump(source: socket.socket, sink: socket.socket) -> None:
    """把 `source` 读到的都写给 `sink`，直到对面关了或出错；最后让 `sink` 那边也知道结束了。"""
    try:
        while chunk := source.recv(65536):
            sink.sendall(chunk)
    except OSError:
        pass
    finally:
        try:
            sink.shutdown(socket.SHUT_WR)
        except OSError:
            pass
