"""Bark（iPhone 推送 App）这一层：设备码的写法和存放、一次 `POST <服务器>/push` 的格式、结果的分类。

请求格式和设备码的写法与 ashare-quant 的 Bark 渠道一致：JSON 里是设备码、标题、正文、分组（不带
`level`、`markdown`、`subtitle`）；设备码可以只填一串，也可以粘贴 App 里的整个推送地址。
和它不同的是失败的分法和后续（重试、待发队列在 `alerts.py`，规格 `hub-alerts`「Bark 失败怎么办」）：

- **被拒绝**（Bark 服务器明确回了错误）：HTTP 3xx / 4xx（429 除外），或者 2xx 但 JSON 里的 `code`
  不是 200（含响应体不是 JSON）；不重试。重定向不跟随（设备码在请求体里，不能被带到别处），所以
  3xx 一律当被拒绝。只有 HTTP 2xx 且 JSON 里 `code` 是 200 才算送到。
- **暂时不通**：超时、连不上、连接被重置 / 断开、响应读到一半断了、HTTP 429、5xx。

**设备码不出现在任何结果的文字里**：Bark 自己的错误信息里可能带着设备码（它会把收到的设备码回显），
所以服务器的话要先遮掉；暂时不通的原因只写状态码或异常的类型名，不写别的。`BarkKey` 的 `repr`
也不含设备码。

**设备码从哪里读**（`lookup_bark_key`）：环境变量 `CLAWBOT_HUB_BARK_KEY` 优先，其次
`<home>/bark.json`（`{"key": "…"}`，0600）。环境变量由调用者在启动时读一次；文件每次发告警之前都
再读，所以服务运行中 `hubctl alerts set-key` 换了设备码，下一条告警就用新的，不用重启。环境变量
写坏了是"问题"而不是退回去用文件（悄悄换一个设备码比报错更糟）。

每次发送用一个新的 `requests.Session`、用完就关：服务器关掉空闲连接不会影响下一次；环境变量里的
HTTP / HTTPS 代理照常生效；超时（连接、等响应各自）取 `alerts.bark_timeout_seconds`。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import requests

from clawbot_hub.fileio import write_private
from clawbot_hub.paths import HubPaths
from clawbot_hub.textutil import mask

ENV_KEY = "CLAWBOT_HUB_BARK_KEY"
SERVER = "https://api.day.app"  # 默认的 Bark 服务器（只填设备码时用它）


# ---------------------------------------------------------------- 设备码


@dataclass(frozen=True)
class BarkKey:
    """Bark 服务器地址和设备码。`repr` 里没有设备码。"""

    server: str
    device: str = field(repr=False)


def parse_bark_key(value: str) -> BarkKey:
    """可以只填设备码，也可以把 App 里的整个推送地址粘进来（取地址里的第一段路径当设备码）。

    看不出设备码就抛 `ValueError`；信息里不含 `value`（它可能就是设备码）。"""
    text = value.strip()
    if "://" not in text:
        device = text.strip("/")
        if not device:
            raise ValueError("设备码是空的")
        return BarkKey(SERVER, device)
    try:
        parts = urlsplit(text)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise ValueError("看不出设备码：推送地址的写法不对") from None
    segments = [segment for segment in parts.path.split("/") if segment]
    if parts.scheme not in ("http", "https") or not host or not segments:
        raise ValueError(
            "看不出设备码：应该是 Bark App 首页的推送地址（https://…/设备码），或者地址里的设备码"
        )
    shown = f"[{host}]" if ":" in host else host  # IPv6 地址要带方括号
    netloc = shown if port is None else f"{shown}:{port}"
    return BarkKey(f"{parts.scheme}://{netloc}", segments[0])


@dataclass(frozen=True)
class BarkLookup:
    """查设备码的结果：`key` 有就能发；没有时 `problem` 说明是没配（空）还是配错了。"""

    key: BarkKey | None
    source: Literal["env", "file"] | None
    problem: str | None


def lookup_bark_key(paths: HubPaths, env: Mapping[str, str] | None = None) -> BarkLookup:
    """环境变量优先，其次 `bark.json`；都没有是 `BarkLookup(None, None, None)`。
    `env` 不给就用进程的环境变量。"""
    source = env if env is not None else os.environ
    configured = (source.get(ENV_KEY) or "").strip()
    if configured:
        try:
            return BarkLookup(parse_bark_key(configured), "env", None)
        except ValueError as exc:
            return BarkLookup(None, "env", f"环境变量 {ENV_KEY} 的值不对：{exc}")
    try:
        raw = paths.bark.read_bytes()
    except FileNotFoundError:
        return BarkLookup(None, None, None)
    except OSError as exc:
        return BarkLookup(None, "file", f"bark.json 读不出来（{type(exc).__name__}）")
    try:
        data = json.loads(raw.decode("utf-8"))
        value = data.get("key") if isinstance(data, dict) else None
        if not (isinstance(value, str) and value.strip()):
            raise ValueError
        return BarkLookup(parse_bark_key(value), "file", None)
    except ValueError:  # 含 JSON 读不出来、没有 key、设备码看不出来；信息不带内容
        return BarkLookup(None, "file", 'bark.json 的内容不对：应该是 {"key": "设备码或推送地址"}')


def key_summary(lookup: BarkLookup) -> str:
    """设备码是怎么配的，一句话，绝不含设备码：`设备码：已配置（环境变量）`、`…（bark.json）`、
    `设备码：未配置`，或者 `设备码：配置有误（原因）`。"""
    if lookup.key is not None:
        place = "环境变量" if lookup.source == "env" else "bark.json"
        return f"设备码：已配置（{place}）"
    if lookup.problem is not None:
        return f"设备码：配置有误（{lookup.problem}）"
    return "设备码：未配置"


def write_bark_key(paths: HubPaths, value: str) -> None:
    """把设备码（或整个推送地址）原样写进 `bark.json`（0600，整份换掉）。
    看不出设备码就抛 `ValueError`，原来的文件不动。"""
    parse_bark_key(value)  # 写之前先试一遍
    text = json.dumps({"key": value.strip()}, ensure_ascii=False) + "\n"
    write_private(paths.bark, text)


# ---------------------------------------------------------------- 一次请求的结果


@dataclass(frozen=True)
class Delivered:
    """Bark 收下了。"""


@dataclass(frozen=True)
class Refused:
    """Bark 服务器明确拒绝（常见原因是设备码填错）：不重试。`reason` 里没有设备码。"""

    reason: str


@dataclass(frozen=True)
class Unreachable:
    """暂时没送到（超时、连不上、429、5xx）：可以重试。`reason` 是状态码或异常的类型名。"""

    reason: str


BarkResult = Delivered | Refused | Unreachable


class BarkSender(Protocol):
    """发一条通知。告警模块只认这个接口，测试里换成替身。"""

    def send(self, key: BarkKey, title: str, text: str) -> BarkResult: ...


def _no_auth(request: requests.PreparedRequest) -> requests.PreparedRequest:
    """占住 `auth` 参数：`requests` 看到有 `auth` 就不读 `~/.netrc`。"""
    return request


def _json_object(content: bytes) -> dict[str, Any]:
    try:
        data = json.loads(content)
    except ValueError:  # 含空内容、不是 JSON、编码不对
        return {}
    return data if isinstance(data, dict) else {}


class BarkClient:
    def __init__(self, group: str, *, timeout_seconds: float) -> None:
        self._group = group
        self._timeout = timeout_seconds

    def send(self, key: BarkKey, title: str, text: str) -> BarkResult:
        payload = {"device_key": key.device, "title": title, "body": text, "group": self._group}
        try:
            with requests.Session() as session:
                reply = session.post(
                    f"{key.server}/push",
                    json=payload,
                    timeout=self._timeout,
                    allow_redirects=False,
                    auth=_no_auth,
                )
                status, content = reply.status_code, reply.content
        except (requests.RequestException, OSError) as exc:
            return Unreachable(type(exc).__name__)
        if status == 429 or status >= 500:
            return Unreachable(f"HTTP {status}")
        body = _json_object(content)
        if 200 <= status < 300 and body.get("code") == 200:
            return Delivered()
        message = mask(str(body.get("message") or "").strip(), [key.device])
        return Refused(message or f"HTTP {status}")
