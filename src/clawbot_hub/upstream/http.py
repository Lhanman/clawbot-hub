"""腾讯 iLink 的 HTTP 实现（`base.Upstream`）。

把 `requests` 的各种情形归到固定的几类（`hub-send` 里有同样的表）；请求头和 `base_info` 的内容由
`profiles.py` 决定。**不做重试**：重试策略在 sender，"不偷偷重发"只在一处。

- 发送：`Accepted` / `Rejected` / `SessionExpired` / `Transient`（没发出去）/
  `NoResponse`（发出去了，不知道结果）。"发出去了"以请求的第一个字节要写出去为界
  （见 `_MarksTheRequest`）：之前的失败（域名解析、连接被拒、连接超时、证书握手、代理隧道）
  是没发出去，之后的（读超时、被重置、响应读到一半断开）是不知道结果。收到完整的 429 / 5xx
  响应不看响应体，也是没发出去。
- 业务码：先取 `ret`（不是空也不是 0），没有再取 `errcode`；-14 是登录失效。HTTP 429 / 5xx 之外的
  响应才看响应体，所以 4xx 里带 -14 也算登录失效。
- 每次调用一个新的 `requests.Session`、用完就关：每次都是新连接，服务端早已关掉的空闲连接
  不会把"没发出去"误判成"不知道到没到"；也不会有连接在线程之间共用。
- 不跟随重定向；不读 `~/.netrc`（它会悄悄换掉 `Authorization` 头）；环境变量里的 HTTP / HTTPS
  代理照常生效，SOCKS 代理不支持（连接类不是我们的，判断不了"发出去了没有"），按"没发出去"报。
- 返回的文字里，登录凭证、对话凭证、收消息进度、二维码和验证码都已换成 `***`。
"""

from __future__ import annotations

import logging
import math
import re
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

from clawbot_hub.config import UpstreamConfig
from clawbot_hub.textutil import mask
from clawbot_hub.upstream.base import (
    Accepted,
    Account,
    IncomingMessage,
    NoResponse,
    PollFailed,
    PollOutcome,
    QrCode,
    QrStatus,
    Rejected,
    SendOutcome,
    SessionExpired,
    Transient,
    Updates,
    UpstreamError,
)
from clawbot_hub.upstream.profiles import get_profile

log = logging.getLogger("clawbot_hub.upstream")
# urllib3 在 DEBUG 级别会打印完整的请求地址（扫码状态的查询串里有二维码）；不管全局日志级别怎么设，
# 它只打警告以上
logging.getLogger("urllib3").setLevel(logging.WARNING)

SESSION_EXPIRED_CODE = -14
DEFAULT_MESSAGE_ID_KEYS = ("message_id", "msg_id")

# ---------------------------------------------------------------- "发出去了"的分界

_phase = threading.local()


class _MarksTheRequest:
    """请求的第一个字节要写出去的时候，在这个线程上记一笔"已经开始发了"。

    连接是在 `send` 里才建立的（明文 HTTP），或者在它之前建立的（https，证书握手也在建连里），
    所以这里先把连接建好再记：建连阶段的失败不会被记成"已经发了"。经代理建 https 隧道时发的
    `CONNECT` 也走 `send`，但那不是我们的请求，不记。"""

    _tunneling = False

    def _tunnel(self) -> None:
        self._tunneling = True
        try:
            super()._tunnel()  # type: ignore[misc]
        finally:
            self._tunneling = False

    def send(self, data: Any) -> None:
        if self.sock is None and self.auto_open:  # type: ignore[attr-defined]
            self.connect()  # type: ignore[attr-defined]
        if not self._tunneling:
            _phase.sent = True
        super().send(data)  # type: ignore[misc]


class _Connection(_MarksTheRequest, HTTPConnection):
    pass


class _SecureConnection(_MarksTheRequest, HTTPSConnection):
    pass


class _Pool(HTTPConnectionPool):
    ConnectionCls = _Connection


class _SecurePool(HTTPSConnectionPool):
    ConnectionCls = _SecureConnection


def _use_marking_pools(manager: Any) -> None:
    manager.pool_classes_by_scheme = {"http": _Pool, "https": _SecurePool}


class _Adapter(HTTPAdapter):
    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        super().init_poolmanager(*args, **kwargs)
        _use_marking_pools(self.poolmanager)

    def proxy_manager_for(self, proxy: str, **proxy_kwargs: Any) -> Any:
        if proxy.lower().startswith("socks"):
            # SOCKS 的连接类不是我们的，没法知道请求有没有发出去：宁可不发，
            # 也不能把"不知道"当成"没发出去"
            raise requests.exceptions.InvalidProxyURL("不支持 SOCKS 代理")
        manager = super().proxy_manager_for(proxy, **proxy_kwargs)
        _use_marking_pools(manager)
        return manager


def _no_auth(request: requests.PreparedRequest) -> requests.PreparedRequest:
    """占住 `auth` 参数：`requests` 看到有 `auth` 就不读 `~/.netrc`（它会换掉 `Authorization`）。"""
    return request


# ---------------------------------------------------------------- 一次请求


@dataclass(frozen=True)
class _Reply:
    status: int
    body: dict[str, Any]  # 响应体不是 JSON 对象时是空字典


class _NotSent(Exception):
    """请求没发出去。"""


class _Unknown(Exception):
    """请求发出去了，不知道结果。"""

    def __init__(self, reason: str, read_timeout: bool = False) -> None:
        super().__init__(reason)
        self.read_timeout = read_timeout


def _describe(exc: BaseException, secrets: tuple[str | None, ...]) -> str:
    return mask(f"{type(exc).__name__}: {exc}", secrets)


def _json_object(response: requests.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _exchange(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    timeout: tuple[float, float],
    secrets: tuple[str | None, ...],
    params: dict[str, Any] | None = None,
    json_body: Any = None,
) -> _Reply:
    """发一次请求。失败抛 `_NotSent`（没发出去）或 `_Unknown`（发出去了，不知道结果）。"""
    _phase.sent = False
    session = requests.Session()
    adapter = _Adapter(max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    try:
        response = session.request(
            method,
            url,
            headers=headers,
            params=params,
            json=json_body,
            timeout=timeout,
            allow_redirects=False,
            auth=_no_auth,
        )
        return _Reply(response.status_code, _json_object(response))
    except requests.exceptions.ReadTimeout as exc:
        raise _Unknown(_describe(exc, secrets), read_timeout=True) from None
    except requests.exceptions.RequestException as exc:
        if _phase.sent:
            raise _Unknown(_describe(exc, secrets)) from None
        raise _NotSent(_describe(exc, secrets)) from None
    finally:
        session.close()


# ---------------------------------------------------------------- 响应体怎么读


def _business_code(body: dict[str, Any]) -> Any:
    """业务码：先取 `ret`，没有再取 `errcode`；空、空串和 0 都不算。没有返回 `None`。"""
    for key in ("ret", "errcode"):
        value = body.get(key)
        if value is None or value == "" or value == 0:
            continue
        return value
    return None


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _usable_id(value: Any) -> str | None:
    """消息编号：非空的字符串，或者不是 0 的数；布尔、列表这类不算。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (int, float)) and math.isfinite(value) and value != 0:
        return str(value)
    return None


def _first_usable_id(body: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        found = _usable_id(body.get(key))
        if found:
            return found
    return None


def _whole_number(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _positive_ms(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if math.isfinite(value) and value > 0 else None


def _non_empty_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _text_of(entry: dict[str, Any]) -> str:
    inner = entry.get("text_item")
    text = inner.get("text") if isinstance(inner, dict) else None
    return text if isinstance(text, str) else ""


def _message(raw: dict[str, Any]) -> IncomingMessage:
    items = raw.get("item_list")
    texts = [
        _text_of(entry)
        for entry in (items if isinstance(items, list) else [])
        if isinstance(entry, dict) and entry.get("type") == 1
    ]
    sender = raw.get("from_user_id")
    return IncomingMessage(
        from_user_id=sender if isinstance(sender, str) else "",
        message_type=_whole_number(raw.get("message_type")),
        context_token=_non_empty_text(raw.get("context_token")),
        at_ms=_positive_ms(raw.get("create_time_ms")),
        text="".join(texts),
        has_text=bool(texts),
        message_id=_usable_id(raw.get("message_id")),
        seq=_whole_number(raw.get("seq")),
    )


def _messages(raw: Any) -> tuple[IncomingMessage, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(_message(entry) for entry in raw if isinstance(entry, dict))


# 扫码返回的 redirect_host：主机名（字母、数字、点、减号），可带一个端口；不接受路径、账号、空格
_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::([0-9]{1,5}))?")


def _check_host(host: Any) -> str:
    match = _HOST.fullmatch(host) if isinstance(host, str) else None
    if match is None or (match.group(1) is not None and int(match.group(1)) > 65535):
        raise UpstreamError("腾讯给的重定向地址格式不对，没有使用")
    return host


# ---------------------------------------------------------------- 实现


class HttpUpstream:
    """`base.Upstream` 的 HTTP 实现。没有状态，可以被几个线程同时用。

    `message_id_keys` 是发送成功的返回里可能带消息编号的键（配置 `send.message_id_keys`）；
    `qr_start_timeout`、`qr_status_timeout` 是取二维码、查扫码状态的超时（秒）。"""

    def __init__(
        self,
        config: UpstreamConfig,
        *,
        message_id_keys: tuple[str, ...] = DEFAULT_MESSAGE_ID_KEYS,
        qr_start_timeout: float = 20.0,
        qr_status_timeout: float = 40.0,
    ) -> None:
        self._config = config
        self._profile = get_profile(config.profile)
        self._message_id_keys = tuple(message_id_keys)
        self._qr_start_timeout = qr_start_timeout
        self._qr_status_timeout = qr_status_timeout

    # ---- 地址 ----

    def _bot_base(self, acct: Account) -> str:
        return (acct.base_url or self._config.base_url).rstrip("/")

    def _qr_base(self, host: str | None) -> str:
        if host is None:
            return self._config.base_url.rstrip("/")
        scheme = urlsplit(self._config.base_url).scheme
        return f"{scheme}://{_check_host(host)}"

    # ---- 收消息 ----

    def get_updates(self, acct: Account, buf: str, timeout: float) -> PollOutcome:
        secrets = (acct.bot_token, buf)
        try:
            reply = _exchange(
                "POST",
                f"{self._bot_base(acct)}/ilink/bot/getupdates",
                headers=self._profile.bot_headers(acct.bot_token),
                json_body={"get_updates_buf": buf, "base_info": self._profile.base_info()},
                timeout=(min(timeout, self._config.request_timeout), timeout),
                secrets=secrets,
            )
        except _NotSent as exc:
            return PollFailed(str(exc), completed=False)
        except _Unknown as exc:
            if exc.read_timeout:  # 长轮询没等到消息：没有新消息
                return Updates(timed_out=True)
            return PollFailed(str(exc), completed=False)

        status, body = reply.status, reply.body
        if status == 429 or status >= 500:  # 不看响应体
            return PollFailed(f"HTTP {status}", completed=False)
        code = _business_code(body)
        if code == SESSION_EXPIRED_CODE:
            return SessionExpired()
        if status != 200 or code is not None:
            rejected = Rejected(status, code, mask(_text(body.get("errmsg")), secrets))
            return PollFailed(rejected.detail, completed=True)
        return Updates(
            messages=_messages(body.get("msgs")),
            buf=_non_empty_text(body.get("get_updates_buf")),
            longpolling_timeout_ms=self._longpolling_hint(body),
        )

    @staticmethod
    def _longpolling_hint(body: dict[str, Any]) -> int | None:
        value = _whole_number(body.get("longpolling_timeout_ms"))
        return value if value is not None and value > 0 else None

    # ---- 发消息 ----

    def send_text(
        self,
        acct: Account,
        to_user_id: str,
        context_token: str,
        text: str,
        client_id: str,
        timeout: float,
    ) -> SendOutcome:
        secrets = (acct.bot_token, context_token)
        message = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id,
            "message_type": 2,  # 机器人发的
            "message_state": 2,  # 一次发完
            "context_token": context_token,
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        }
        try:
            reply = _exchange(
                "POST",
                f"{self._bot_base(acct)}/ilink/bot/sendmessage",
                headers=self._profile.bot_headers(acct.bot_token),
                json_body={"msg": message, "base_info": self._profile.base_info()},
                timeout=(timeout, timeout),
                secrets=secrets,
            )
        except _NotSent as exc:
            return Transient(str(exc))
        except _Unknown as exc:
            return NoResponse(str(exc), read_timeout=exc.read_timeout)

        status, body = reply.status, reply.body
        if status == 429 or status >= 500:  # 不看响应体，里面带 -14 也一样
            return Transient(f"HTTP {status}")
        code = _business_code(body)
        if code == SESSION_EXPIRED_CODE:
            return SessionExpired()
        if status != 200 or code is not None:
            return Rejected(status, code, mask(_text(body.get("errmsg")), secrets))
        return Accepted(message_id=_first_usable_id(body, self._message_id_keys))

    # ---- 扫码 ----

    def _qr_exchange(
        self,
        method: str,
        url: str,
        *,
        timeout: float,
        secrets: tuple[str | None, ...],
        params: dict[str, Any],
        json_body: Any = None,
    ) -> dict[str, Any]:
        try:
            reply = _exchange(
                method,
                url,
                headers=self._profile.app_headers(),
                params=params,
                json_body=json_body,
                timeout=(timeout, timeout),
                secrets=secrets,
            )
        except (_NotSent, _Unknown) as exc:
            raise UpstreamError(str(exc)) from None
        code = _business_code(reply.body)
        if reply.status != 200 or code is not None:
            rejected = Rejected(reply.status, code, mask(_text(reply.body.get("errmsg")), secrets))
            raise UpstreamError(rejected.detail)
        return reply.body

    def qr_start(self) -> QrCode:
        by_post = self._profile.qr_start_by_post
        body = self._qr_exchange(
            "POST" if by_post else "GET",
            f"{self._qr_base(None)}/ilink/bot/get_bot_qrcode",
            params={"bot_type": 3},
            json_body={"local_token_list": []} if by_post else None,
            timeout=self._qr_start_timeout,
            secrets=(),
        )
        code = _non_empty_text(body.get("qrcode"))
        content = _non_empty_text(body.get("qrcode_img_content"))
        if code is None or content is None:
            raise UpstreamError("二维码的响应里缺 qrcode 或 qrcode_img_content")
        return QrCode(code=code, image_content=content)

    def qr_status(self, code: str, verify_code: str | None, host: str | None) -> QrStatus:
        base = self._qr_base(host)
        params = {"qrcode": code}
        if verify_code:
            params["verify_code"] = verify_code
        body = self._qr_exchange(
            "GET",
            f"{base}/ilink/bot/get_qrcode_status",
            params=params,
            timeout=self._qr_status_timeout,
            secrets=(code, verify_code),
        )
        status = _non_empty_text(body.get("status"))
        if status is None:
            raise UpstreamError("扫码状态的响应里没有 status")
        if status == "confirmed":
            token = _non_empty_text(body.get("bot_token"))
            if token is None:
                raise UpstreamError("扫码确认了，但响应里没有登录凭证")
            account = Account(
                bot_token=token,
                base_url=_text(body.get("baseurl")),
                bot_id=_text(body.get("ilink_bot_id")),
                user_id=_text(body.get("ilink_user_id")),
            )
            return QrStatus(status=status, account=account)
        if status == "scaned_but_redirect":
            return QrStatus(status=status, redirect_host=_check_host(body.get("redirect_host")))
        return QrStatus(status=status)

    # ---- 官方档的通知 ----

    def notify_start(self, acct: Account) -> None:
        self._notify("notifystart", acct)

    def notify_stop(self, acct: Account) -> None:
        self._notify("notifystop", acct)

    def _notify(self, name: str, acct: Account) -> None:
        """只有 `official` 档才调；失败只记一条警告，不影响启动和退出。"""
        if not self._profile.uses_notify:
            return
        secrets = (acct.bot_token,)
        timeout = self._config.request_timeout
        try:
            reply = _exchange(
                "POST",
                f"{self._bot_base(acct)}/ilink/bot/msg/{name}",
                headers=self._profile.bot_headers(acct.bot_token),
                json_body={"base_info": self._profile.base_info()},
                timeout=(timeout, timeout),
                secrets=secrets,
            )
        except (_NotSent, _Unknown) as exc:
            log.warning("%s 没成功（不影响使用）：%s", name, exc)
            return
        code = _business_code(reply.body)
        if reply.status != 200 or code is not None:
            rejected = Rejected(reply.status, code, mask(_text(reply.body.get("errmsg")), secrets))
            log.warning("%s 没成功（不影响使用）：%s", name, rejected.detail)
