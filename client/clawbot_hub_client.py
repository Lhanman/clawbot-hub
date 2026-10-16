"""clawbot-hub 的 Python 客户端：一个文件，只用标准库，Python 3.9 起都能跑，整份拷进自己的项目就行。

最短的用法::

    from clawbot_hub_client import HubClient, HubError

    hub = HubClient()  # 地址默认 http://127.0.0.1:8790，密钥读环境变量 CLAWBOT_HUB_KEY
    try:
        result = hub.send("早报\\n正文……", dedupe_key="brief-2026-10-12")
    except HubError as exc:
        ...  # 服务不可用 / 密钥不对 / 请求不对：见下面的三种异常
    else:
        if result.needs_fallback:
            ...  # 微信这边没送到：项目自己走 Bark，把 result.reason 写进 Bark 的末尾

客户端只做三件事：把接口包成函数、把错误分清楚、把"至少送达一次"的取话循环写对（`run_inbox`）。
走不走 Bark、Bark 末尾怎么写，是项目自己的事，客户端不替项目决定。

两条要紧的规矩：

- 业务结果（`rejected`、`failed`、`unconfirmed`、`partial`、`skipped`）不抛异常，看 `SendResult`；
  HTTP 层面的错误才抛异常（`HubUnavailable`、`HubAuthError`、`HubRequestError`）；
- 发消息和回复不会偷偷重发：只有给了 `dedupe_key`、而且确定请求没被处理（连不上、502 / 503 / 504）
  才重试；读超时、连接被重置、500 这几种"可能已经发出去了"的情形一律不重发，`possibly_sent` 为真。

日志走标准库的 `logging`（名字 `clawbot_hub_client`），里面没有密钥，也没有消息正文。
接口细节见仓库里的 `docs/api/v1.md`，接入的步骤和惯例见 `docs/clients/接入指南.md`。
"""

from __future__ import annotations

import http.client
import json
import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlsplit

CLIENT_VERSION = "0.1.0"
API_VERSION = "1"  # 字符串，和服务健康检查里的 `api` 字段是同一种类型

DEFAULT_BASE_URL = "http://127.0.0.1:8790"
DEFAULT_KEY_ENV = "CLAWBOT_HUB_KEY"
DEFAULT_TIMEOUT = 35.0  # 读超时（秒）
DEFAULT_RETRIES = 1

CONNECT_TIMEOUT_SECONDS = 5.0  # 连接超时；服务一般在本机或局域网，连得上就是立刻连上
DEFAULT_SEND_TIMEOUT_SECONDS = 30.0  # 调过 `status()` 之前，按服务的发送总时限默认值算
SEND_TIMEOUT_MARGIN = 5.0  # 发送 / 回复的读超时 = max(timeout, 服务的发送总时限 + 这个)
INBOX_TIMEOUT_MARGIN = 10.0  # 取话的读超时 = max(timeout, wait + 这个)
RETRY_INTERVAL_SECONDS = 1.0  # 客户端层的重试之间停多久
ACK_BATCH_SIZE = 50  # 服务一次最多确认几个 id
DEFAULT_INBOX_ONLINE_SECONDS = 90.0  # 取不到 `status()` 时，按服务的在线判定时间默认值算
HEARTBEAT_DIVISOR = 3  # 心跳间隔 = 在线判定时间 / 这个数
HEARTBEAT_JOIN_SECONDS = 2.0  # 处理完一句话后最多等心跳线程收尾多久

BACKOFF_START_SECONDS = 5.0  # 连不上服务时的退避：从这个数起每次翻倍
BACKOFF_MAX_SECONDS = 60.0
RATE_LIMITED_SLEEP_SECONDS = 60.0  # 429 没有 Retry-After 时睡多久
IDLE_SLEEP_SECONDS = 1.0  # 取话很快返回空列表时睡多久，免得空转

logger = logging.getLogger("clawbot_hub_client")
logger.addHandler(logging.NullHandler())  # 库自己不往标准错误打日志；怎么记，由项目配置决定

__all__ = [
    "API_VERSION",
    "CLIENT_VERSION",
    "HubAuthError",
    "HubClient",
    "HubError",
    "HubRequestError",
    "HubUnavailable",
    "Inbound",
    "SendResult",
]

_KEY_FORMAT = re.compile(r"[\x21-\x7e]+")  # 可打印的 ASCII，没有空白：写进请求头不会出错

# 重试的三种规矩（见 `HubClient._call`）
_REPEAT = "repeat"  # 重复调用也没事：任何 HubUnavailable 都重试
_UNPROCESSED = "unprocessed"  # 只在确定请求没被处理时重试
_NEVER = "never"  # 不重试


def _sleep(seconds: float) -> None:
    """客户端层重试之间的停顿。单独一个函数，测试里换掉它就不用真等。"""
    time.sleep(seconds)


_monotonic = time.monotonic  # 取话循环量"这次取话等了多久"用；测试里换掉它


# ---------------------------------------------------------------- 异常


class HubError(Exception):
    """客户端所有异常的父类。

    `message` 是中文说明（不含密钥，不含消息正文）；`http_status` 是服务回的 HTTP 状态码，
    请求根本没发出去（没给密钥）或者没得到回话（连不上、读超时……）时是 `None`。
    """

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        # 不用再调 `Exception.__init__`：`Exception.__new__` 已经把 message 记进 `args`
        self.message = message
        self.http_status = http_status


class HubUnavailable(HubError):
    """连不上服务、连接超时、读超时、连接被断开，或者服务回了 5xx（含 503 `shutting_down`）。

    项目遇到它就应该走 Bark。`possibly_sent` 只对发消息和回复有意义：

    - 为真：连接已经建立之后才出的错（读超时、被重置、断开、回的内容看不懂）或者 500 `internal`，
      不知道服务有没有把消息发出去，**不要当作没发**；
    - 为假：连不上、连接超时、502、503、504，那些一定没发出去。
    """

    def __init__(
        self, message: str, *, possibly_sent: bool = False, http_status: int | None = None
    ) -> None:
        super().__init__(message, http_status=http_status)
        self.possibly_sent = possibly_sent


class HubAuthError(HubError):
    """401 / 403：密钥不对、项目被停用、只推送的项目调了取话；也包括根本没给（或没法用）密钥。

    `code` 是服务给的 `unauthorized`、`project_disabled`、`push_only`；局域网规则拒绝的 403
    没有正文，本地就发现没有密钥时也没有，这两种 `code` 是 `None`（后一种 `http_status`
    也是 `None`）。
    """

    def __init__(
        self, message: str, *, code: str | None = None, http_status: int | None = None
    ) -> None:
        super().__init__(message, http_status=http_status)
        self.code = code


class HubRequestError(HubError):
    """其余的 4xx（400、404、410、413、429）：请求本身有问题，原样再发也没用。

    `code` 是服务给的，如 `bad_request`、`text_too_long`、`unknown_handle`、`handle_expired`、
    `rate_limited`；`retry_after` 是 `Retry-After` 头里的秒数（429 时有）。注意它和
    `SendResult.reason_code` 是两套代码：前者是 HTTP 错误，后者是业务结果，不混用。
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retry_after: int | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message, http_status=http_status)
        self.code = code
        self.retry_after = retry_after


# ---------------------------------------------------------------- 数据类


def _text_of(value: Any) -> str | None:
    return value if isinstance(value, str) else None


@dataclass(frozen=True)
class SendResult:
    """发消息 / 回复的结果，字段和接口返回的一一对应；多出来的未知字段忽略。

    `status` 是 `sent`、`skipped`、`rejected`、`failed`、`unconfirmed`、`partial` 之一
    （以后服务加了新的，客户端不认识，也别慌：`needs_fallback` 会是真）。
    """

    status: str
    reason_code: str | None = None
    reason: str | None = None
    parts: int = 0
    parts_sent: int = 0
    duplicate: bool = False
    id: str | None = None
    quota: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """微信这边没问题：`sent`，或者调用方自己说了窗口关着就算了（`skipped`）。"""
        return self.status in ("sent", "skipped")

    @property
    def needs_fallback(self) -> bool:
        """调用方应当另走 Bark（`not ok`）。服务以后加了客户端不认识的状态，也按要走 Bark 算。"""
        return not self.ok

    @property
    def maybe_delivered(self) -> bool:
        """消息可能已经到了（`unconfirmed`、`partial`）：走 Bark 时要在原因里说明。"""
        return self.status in ("unconfirmed", "partial")

    @classmethod
    def from_json(cls, data: Any) -> SendResult:
        if not isinstance(data, dict):
            raise ValueError("不是 JSON 对象")
        quota = data.get("quota")
        return cls(
            status=data["status"] if isinstance(data.get("status"), str) else "",
            reason_code=_text_of(data.get("reason_code")),
            reason=_text_of(data.get("reason")),
            parts=_whole(data.get("parts")),
            parts_sent=_whole(data.get("parts_sent")),
            duplicate=data.get("duplicate") is True,
            id=_text_of(data.get("id")),
            quota=quota if isinstance(quota, dict) else {},
        )


def _whole(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


@dataclass(frozen=True)
class Inbound:
    """取话取到的一句话，字段和接口返回的每一项一一对应；多出来的未知字段忽略。

    `via` 是这句话怎么路由到本项目的（`confirm` 表示它可能是对你上一条"等确认"回复的回答，
    是不是由项目自己判断）；`reply_handle` 用来回复这句话，三十分钟内有效，别存着不用。
    """

    id: str
    text: str
    has_text: bool
    at: str
    at_ms: int
    received_at: str
    attempt: int
    via: str
    reply_handle: str

    @classmethod
    def from_json(cls, data: Any) -> Inbound:
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            raise ValueError("取话返回的一项缺少 id")
        return cls(
            id=data["id"],
            text=data["text"] if isinstance(data.get("text"), str) else "",
            has_text=data.get("has_text") is True,
            at=_text_of(data.get("at")) or "",
            at_ms=_whole(data.get("at_ms")),
            received_at=_text_of(data.get("received_at")) or "",
            attempt=_whole(data.get("attempt")),
            via=_text_of(data.get("via")) or "",
            reply_handle=_text_of(data.get("reply_handle")) or "",
        )


# ---------------------------------------------------------------- 小工具


def _positive_finite(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and value > 0


def _checked_timeout(value: Any, name: str) -> float:
    if not _positive_finite(value):
        raise ValueError(f"{name} 要是大于 0 的有限数字")
    return float(value)


def _encode(body: dict[str, Any]) -> bytes:
    try:
        return json.dumps(body, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        # 文字里有孤立的代理字符（比如从没法解码的文件名来的）：转义成 \uXXXX 照样发，
        # 让服务去回 400，调用方看到的错误种类不会因此变乱
        return json.dumps(body).encode("ascii")


def _error_fields(raw: bytes) -> tuple[str | None, str | None]:
    """服务的错误正文 `{"error": {"code": …, "message": …}}`；不是这个形状就两样都没有。"""
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        return None, None
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return None, None
    code, message = error.get("code"), error.get("message")
    return (
        code if isinstance(code, str) and code else None,
        message if isinstance(message, str) and message else None,
    )


def _retry_after(header: str | None) -> int | None:
    """`Retry-After` 头里的整数秒；日期写法、小数、负数都不认。"""
    if header is None:
        return None
    text = header.strip()
    return int(text) if text.isascii() and text.isdigit() else None


def _brief(exc: BaseException) -> str:
    """日志里描述一个失败：错误类型加 HTTP 状态码，没有服务回的文字、没有请求里的东西。"""
    status = getattr(exc, "http_status", None)
    return type(exc).__name__ if status is None else f"{type(exc).__name__}，HTTP {status}"


def _unreadable(what: str, why: str = "") -> HubUnavailable:
    """服务回了 200，可是内容看不懂：请求已经被处理过，所以"可能已经发出去了"。"""
    detail = f"（{why}）" if why else ""
    return HubUnavailable(
        f"服务回了 200，但{what}的内容看不懂{detail}", possibly_sent=True, http_status=200
    )


def _as_object(data: Any, what: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise _unreadable(what, "不是 JSON 对象")
    return data


def _never_stop() -> bool:
    return False


def _rate_limit_pause(exc: HubRequestError, sleep: Callable[[float], None]) -> None:
    """429 就睡到服务说的时间（没说睡 60 秒；说 0 也睡一秒，免得空转）；别的请求错误原样抛出。"""
    if exc.http_status != 429:
        raise exc
    if exc.retry_after is None:
        pause = RATE_LIMITED_SLEEP_SECONDS
    else:
        pause = float(exc.retry_after) if exc.retry_after > 0 else IDLE_SLEEP_SECONDS
    logger.warning("服务说请求太频繁，%g 秒后再试", pause)
    sleep(pause)


class _Backoff:
    """连不上服务时的退避：5 秒起每次翻倍，最多 60 秒，成功一次就重新计。"""

    def __init__(self, sleep: Callable[[float], None]) -> None:
        self._sleep = sleep
        self.next_wait = BACKOFF_START_SECONDS

    def wait(self) -> None:
        seconds = self.next_wait
        self.next_wait = min(seconds * 2, BACKOFF_MAX_SECONDS)
        self._sleep(seconds)

    def reset(self) -> None:
        self.next_wait = BACKOFF_START_SECONDS


class _Heartbeat:
    """处理一句话期间，在后台线程里每隔 `interval` 秒告诉服务"这个项目还活着"。

    当 `with` 用：进去时起线程，出来时（含 `handler` 抛异常）停线程。计时用 `Event.wait`，
    不用取话循环的 `sleep` 参数；心跳失败只记日志，不影响处理。
    """

    def __init__(self, client: HubClient, interval: float) -> None:
        self._client = client
        self._interval = interval
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name="clawbot-hub-heartbeat", daemon=True)

    def __enter__(self) -> _Heartbeat:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stopped.set()
        self._thread.join(HEARTBEAT_JOIN_SECONDS)

    def _run(self) -> None:
        while not self._stopped.wait(self._interval):
            try:
                self._client._beat()
            except Exception as exc:
                logger.warning("心跳没发出去（%s），处理不受影响", _brief(exc))


# ---------------------------------------------------------------- 客户端


class HubClient:
    """共用服务的客户端。线程安全：每次调用自己开一个连接，用完就关。

    - `base_url`：服务的地址，可以带路径前缀（服务放在反向代理后面时）；
    - `key`：项目密钥；不给就读环境变量 `key_env`。构造时不检查，第一次调用方法时才抛
      `HubAuthError`（项目里常有在导入时就构造的全局客户端）；
    - `timeout`：读超时的下限（秒）。取话和发送的读超时要更长，见 `inbox` 和 `send`；
    - `retries`：安全重试的次数，见 `send`。
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        key: str | None = None,
        key_env: str = DEFAULT_KEY_ENV,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
    ) -> None:
        scheme, host, port, prefix = _parse_base_url(base_url)
        if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
            raise ValueError("retries 要是不小于 0 的整数")
        self.base_url = base_url.rstrip("/")
        self.timeout = _checked_timeout(timeout, "timeout")
        self.retries = retries
        self.key_env = key_env
        self._key = key
        self._scheme, self._host, self._port, self._prefix = scheme, host, port, prefix
        self._reported_send_deadline: float | None = None  # `status()` 里服务说的发送总时限

    def __repr__(self) -> str:
        return f"HubClient(base_url={self.base_url!r})"  # 不带密钥

    # ---- 接口 ----

    def send(
        self,
        text: str,
        *,
        priority: str = "normal",
        dedupe_key: str | None = None,
        on_closed: str = "fail",
        timeout: float | None = None,
    ) -> SendResult:
        """发一条消息（`POST /v1/messages`），拿到最终结果才返回。

        `priority` 是 `urgent`、`normal`、`deferrable`；`on_closed="skip"` 表示窗口关着就算了
        （结果是 `skipped`，`ok` 为真）；`dedupe_key` 是稳定的去重键（如 `brief-2026-10-12`）。

        **不偷偷重发**：没给 `dedupe_key` 一律不重试；给了，也只在确定请求没被处理时（连不上、连接
        超时、502 / 503 / 504）最多重试 `retries` 次，间隔 1 秒。读超时、连接被重置、500 一律
        不重试，抛 `HubUnavailable`，`possibly_sent` 为真。`rejected`、`failed`、`unconfirmed`、
        `partial`、`skipped` 都是业务结果，不抛异常。

        读超时取 `max(timeout, 服务的发送总时限 + 5)`：想等得更久可以给大一点的 `timeout`，
        给小了没用，因为服务可能真的要那么久。
        """
        body: dict[str, Any] = {"text": text, "priority": priority, "on_closed": on_closed}
        if dedupe_key is not None:
            body["dedupe_key"] = dedupe_key
        data = self._call(
            "POST",
            "/v1/messages",
            body=body,
            read_timeout=self._send_read_timeout(timeout),
            retry=_NEVER if dedupe_key is None else _UNPROCESSED,
        )
        return _parsed(SendResult.from_json, data, "发送结果")

    def reply(
        self,
        handle: str,
        text: str,
        *,
        awaiting_confirmation: bool = False,
        confirm_seconds: int | None = None,
    ) -> SendResult:
        """回复使用者的一句话（`POST /v1/replies`）。`handle` 是取话时给的 `reply_handle`。

        需要等使用者回"对"的回复带 `awaiting_confirmation=True`（`confirm_seconds` 是等多久）。
        回复没有去重键，永远不自动重试。
        """
        body: dict[str, Any] = {
            "handle": handle,
            "text": text,
            "awaiting_confirmation": awaiting_confirmation,
        }
        if confirm_seconds is not None:
            body["confirm_seconds"] = confirm_seconds
        data = self._call(
            "POST",
            "/v1/replies",
            body=body,
            read_timeout=self._send_read_timeout(None),
            retry=_NEVER,
        )
        return _parsed(SendResult.from_json, data, "回复结果")

    def inbox(self, wait: int = 25, max: int = 10) -> list[Inbound]:
        """取话（`GET /v1/inbox`）：没有话就最多等 `wait` 秒，最多取 `max` 条。

        取到的话要处理完再 `ack`；没确认的话租期（默认 60 秒）过后服务会再给一次。
        读超时取 `max(timeout, wait + 10)`。自己写循环的项目要自己发心跳，或者直接用 `run_inbox`。
        """
        return self._fetch(wait, max, retries=None)

    def ack(self, ids: Iterable[str]) -> int:
        """确认这些话处理完了（`POST /v1/inbox/ack`），返回服务确认了几条。

        服务一次最多收 50 个，多的客户端自动分批。确认是幂等的：重复的、不存在的不报错。
        """
        return self._ack(ids, retries=None)

    def status(self) -> dict[str, Any]:
        """本项目的额度和全局的登录、窗口状态（`GET /v1/status`），原样的 JSON。

        顺带记下服务的发送总时限（`service.send_timeout_seconds`），之后发送的读超时用它算。
        """
        return self._status(retries=None)

    def health(self) -> dict[str, Any]:
        """探活（`GET /v1/health`）：用不着密钥，所以没有密钥也能调。"""
        return _as_object(self._call("GET", "/v1/health", authorize=False), "探活")

    def run_inbox(
        self,
        handler: Callable[[Inbound], object],
        *,
        stop: Callable[[], bool] = _never_stop,
        wait: int = 25,
        max: int = 10,
        sleep: Callable[[float], None] = time.sleep,
        heartbeat_seconds: float | None = None,
    ) -> None:
        """ "至少送达一次"的取话循环：取话、处理、确认、连不上时退避，一直跑到 `stop()` 为真。

        每个项目同一时间只能有一个取话循环（两个会互相顶掉）。

        - 对每一项调用 `handler(item)`（在调用方的线程里）。`handler` **正常返回**才算处理完，
          这一项的 `id` 才进确认列表，整批处理完统一 `ack`。`handler` 抛异常的那一项不确认
          （记一条日志，继续处理这一批里后面的），租期过后服务会再给一次，所以 `handler` 要幂等；
        - `handler` 运行期间，后台线程每 `heartbeat_seconds` 秒发一次
          `GET /v1/inbox?wait=0&max=0`，让服务知道项目还活着，处理一句慢指令超过 90 秒也不会被
          判成没在线。不传时取服务的 `inbox_online_seconds / 3`（开始时问一次，问不到按 90 秒算，
          即 30 秒）。心跳失败只记日志；
        - 连不上服务（`HubUnavailable`）时退避：5 秒起每次翻倍，最多 60 秒，成功一次就重新计。
          循环里的取话、确认、心跳每次调用都只发一次请求，不叠加 `retries`；
        - `ack` 失败时这一批的 `id` 不丢，用同样的退避只重试 `ack`，成功了才继续取话。要停的时候
          （`stop()` 为真）不再等：放弃这一批的确认，话会在租期过后再给；
        - `HubAuthError`（密钥不对、项目被停用）直接抛出；`HubRequestError` 里 429 睡
          `retry_after` 秒（没有就 60 秒）后继续，其余抛出；
        - `stop()` 在两次取话之间检查，为真时退出（当前这一批处理完并确认）；
        - 一次耗时不到 `wait / 2` 秒的空列表（`wait=0` 时一律）睡 1 秒再取，免得空转；服务已经等满的
          空列表不算错误，也不睡。

        `sleep` 是退避和空转保护用的睡觉函数，测试里换成只记录的。
        """
        interval = self._heartbeat_interval(heartbeat_seconds)
        backoff = _Backoff(sleep)
        while not stop():
            started = _monotonic()
            try:
                items = self._fetch(wait, max, retries=0)
            except HubUnavailable as exc:
                logger.warning("取话没成功（%s），%g 秒后再试", _brief(exc), backoff.next_wait)
                backoff.wait()
                continue
            except HubRequestError as exc:
                _rate_limit_pause(exc, sleep)
                continue
            backoff.reset()
            if not items:
                if wait <= 0 or _monotonic() - started < wait / 2:
                    sleep(IDLE_SLEEP_SECONDS)
                continue
            handled = [item.id for item in items if self._handle(handler, item, interval)]
            self._ack_until_done(handled, backoff, stop, sleep)  # 一个也没处理好就不发请求

    # ---- 内部：取话循环 ----

    def _handle(self, handler: Callable[[Inbound], object], item: Inbound, interval: float) -> bool:
        with _Heartbeat(self, interval):
            try:
                handler(item)
            except Exception as exc:
                # 日志里只写话的编号和错误类型：错误信息里常带着话的内容
                logger.warning(
                    "处理 %s 时出错（%s），这一项不确认，租期过后服务会再给一次",
                    item.id,
                    type(exc).__name__,
                )
                logger.debug("处理 %s 出错的详细信息", item.id, exc_info=True)
                return False
        return True

    def _ack_until_done(
        self,
        ids: list[str],
        backoff: _Backoff,
        stop: Callable[[], bool],
        sleep: Callable[[float], None],
    ) -> None:
        while True:
            try:
                self._ack(ids, retries=0)
            except HubUnavailable as exc:
                if stop():
                    logger.warning(
                        "要停了，还有 %d 句话没能确认（%s），它们会在租期过后由服务再给一次",
                        len(ids),
                        _brief(exc),
                    )
                    return
                logger.warning(
                    "确认没成功（%s），%g 秒后再试，这 %d 句话的编号先留着",
                    _brief(exc),
                    backoff.next_wait,
                    len(ids),
                )
                backoff.wait()
            except HubRequestError as exc:
                _rate_limit_pause(exc, sleep)
            else:
                backoff.reset()
                return

    def _heartbeat_interval(self, given: float | None) -> float:
        if given is not None:
            return _checked_timeout(given, "heartbeat_seconds")
        online = DEFAULT_INBOX_ONLINE_SECONDS
        try:
            service = self._status(retries=0).get("service")
        except HubError:
            service = None  # 问不到就按默认的算；真有问题（密钥不对……）接下来的取话会抛
        reported = service.get("inbox_online_seconds") if isinstance(service, dict) else None
        if _positive_finite(reported):
            online = float(reported)
        return online / HEARTBEAT_DIVISOR

    def _beat(self) -> None:
        """一次心跳：取话接口的 `wait=0&max=0`，只让服务知道项目还活着，不租出话。只发一次请求。"""
        self._call("GET", "/v1/inbox", query={"wait": 0, "max": 0}, retries=0)

    # ---- 内部：各个接口 ----

    def _fetch(self, wait: int, count: int, *, retries: int | None) -> list[Inbound]:
        data = self._call(
            "GET",
            "/v1/inbox",
            query={"wait": wait, "max": count},
            read_timeout=self._inbox_read_timeout(wait),
            retries=retries,
        )
        items = _as_object(data, "取话").get("items")
        if not isinstance(items, list):
            raise _unreadable("取话", "没有 items 列表")
        return [_parsed(Inbound.from_json, item, "取话") for item in items]

    def _ack(self, ids: Iterable[str], *, retries: int | None) -> int:
        pending = list(ids)
        total = 0
        for start in range(0, len(pending), ACK_BATCH_SIZE):
            data = self._call(
                "POST",
                "/v1/inbox/ack",
                body={"ids": pending[start : start + ACK_BATCH_SIZE]},
                retries=retries,
            )
            acked = _as_object(data, "确认").get("acked")
            if isinstance(acked, bool) or not isinstance(acked, int):
                raise _unreadable("确认", "没有 acked 数")
            total += acked
        return total

    def _status(self, *, retries: int | None) -> dict[str, Any]:
        data = _as_object(self._call("GET", "/v1/status", retries=retries), "状态")
        service = data.get("service")
        deadline = service.get("send_timeout_seconds") if isinstance(service, dict) else None
        if _positive_finite(deadline):
            self._reported_send_deadline = float(deadline)
        return data

    def _send_read_timeout(self, timeout: float | None) -> float:
        deadline = self._reported_send_deadline
        if deadline is None:
            deadline = DEFAULT_SEND_TIMEOUT_SECONDS
        wanted = self.timeout if timeout is None else _checked_timeout(timeout, "timeout")
        return max(wanted, deadline + SEND_TIMEOUT_MARGIN)

    def _inbox_read_timeout(self, wait: float) -> float:
        return max(self.timeout, wait + INBOX_TIMEOUT_MARGIN)

    # ---- 内部：一次请求 ----

    def _resolve_key(self) -> str:
        raw = self._key if self._key is not None else os.environ.get(self.key_env)
        key = raw.strip() if isinstance(raw, str) else ""
        if not key:
            raise HubAuthError(
                f"没有密钥：构造 HubClient 时给 key，或者设置环境变量 {self.key_env}"
            )
        if not _KEY_FORMAT.fullmatch(key):
            # 不把密钥的内容写进错误信息；这种字符放进请求头，标准库的报错会把整个头值带出来
            raise HubAuthError("密钥的格式不对：只能是可打印的 ASCII 字符，中间不能有空白或换行")
        return key

    def _call(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        authorize: bool = True,
        read_timeout: float | None = None,
        retry: str = _REPEAT,
        retries: int | None = None,
    ) -> Any:
        """发一个请求，返回解析好的 JSON；失败按 `retry` 的规矩重试，重试用完抛最后一次的异常。

        `retry`：`_REPEAT` 任何 `HubUnavailable` 都重试，`_UNPROCESSED` 只在 `possibly_sent` 为假时
        重试，`_NEVER` 不重试。`retries` 覆盖构造时的次数（取话循环传 0）。
        """
        allowed = 0 if retry == _NEVER else (self.retries if retries is None else retries)
        failures = 0
        while True:
            try:
                return self._attempt(method, path, query, body, authorize, read_timeout)
            except HubUnavailable as exc:
                if failures >= allowed or (retry == _UNPROCESSED and exc.possibly_sent):
                    raise
                failures += 1
                logger.warning(
                    "请求 %s %s 没成功（%s），%g 秒后重试",
                    method,
                    path,
                    _brief(exc),
                    RETRY_INTERVAL_SECONDS,
                )
                _sleep(RETRY_INTERVAL_SECONDS)

    def _attempt(
        self,
        method: str,
        path: str,
        query: dict[str, Any] | None,
        body: dict[str, Any] | None,
        authorize: bool,
        read_timeout: float | None,
    ) -> Any:
        headers = {
            "Accept": "application/json",
            "Connection": "close",
            "User-Agent": f"clawbot-hub-client/{CLIENT_VERSION}",
        }
        if authorize:
            headers["Authorization"] = f"Bearer {self._resolve_key()}"
        payload = None
        if body is not None:
            payload = _encode(body)
            headers["Content-Type"] = "application/json; charset=utf-8"
        target = self._prefix + path + (f"?{urlencode(query)}" if query else "")

        connection = self._connect()
        try:
            try:
                # 连接在 `_connect` 里已经建立，换成这一次的读超时（连接超时只管建立那一步）
                connection.sock.settimeout(self.timeout if read_timeout is None else read_timeout)
                connection.request(method, target, body=payload, headers=headers)
                response = connection.getresponse()
                raw = response.read()
            except (OSError, http.client.HTTPException) as exc:
                # 连接已经建立，请求可能已经被服务收到：不知道它有没有处理
                raise HubUnavailable(
                    f"和共用服务 {self._where()} 的通信中断了（{type(exc).__name__}）",
                    possibly_sent=True,
                ) from None
            status, retry_after = response.status, response.getheader("Retry-After")
        finally:
            connection.close()
        return self._interpret(status, retry_after, raw)

    def _where(self) -> str:
        host = f"[{self._host}]" if ":" in self._host else self._host
        return f"{host}:{self._port}"

    def _connect(self) -> http.client.HTTPConnection:
        connect = (
            http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        )
        connection = connect(self._host, self._port, timeout=CONNECT_TIMEOUT_SECONDS)
        try:
            connection.connect()
        except (OSError, UnicodeError) as exc:
            connection.close()
            raise HubUnavailable(
                f"连不上共用服务 {self._where()}（{type(exc).__name__}）", possibly_sent=False
            ) from None
        return connection

    @staticmethod
    def _interpret(status: int, retry_after: str | None, raw: bytes) -> Any:
        if status == 200:
            try:
                return json.loads(raw.decode("utf-8"))
            except ValueError:
                raise _unreadable("返回") from None
        code, message = _error_fields(raw)
        if status in (401, 403):
            raise HubAuthError(
                message or f"共用服务拒绝了这次请求（HTTP {status}）：检查密钥和这台机器能不能连它",
                code=code,
                http_status=status,
            )
        if status >= 500:
            raise HubUnavailable(
                message or f"共用服务出错了（HTTP {status}）",
                possibly_sent=status not in (502, 503, 504),
                http_status=status,
            )
        if status >= 400:
            raise HubRequestError(
                message or f"共用服务不接受这个请求（HTTP {status}）",
                code=code,
                retry_after=_retry_after(retry_after),
                http_status=status,
            )
        # 3xx、204 之类：服务从不这样回。不跟随重定向，也不当作成功
        raise HubUnavailable(
            f"共用服务回了意料之外的状态码 {status}", possibly_sent=True, http_status=status
        )


def _parsed(factory: Callable[[Any], Any], data: Any, what: str) -> Any:
    try:
        return factory(data)
    except ValueError as exc:
        raise _unreadable(what, str(exc)) from None


def _parse_base_url(base_url: str) -> tuple[str, str, int, str]:
    """拆开服务地址：协议、主机、端口、路径前缀。不合格抛 `ValueError`（配置写错了，早点知道）。"""
    problem = "base_url 要是 http:// 或 https:// 开头、带主机名的地址，不能带用户名口令"
    if not isinstance(base_url, str):
        raise ValueError(problem)
    try:
        parts = urlsplit(base_url)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise ValueError(problem) from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not host or parts.username or parts.password:
        raise ValueError(problem)
    return scheme, host, port or (443 if scheme == "https" else 80), parts.path.rstrip("/")
