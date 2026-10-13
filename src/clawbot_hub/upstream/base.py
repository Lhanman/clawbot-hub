"""上游协议和结果类型（设计第 3 节）。

服务其余部分只通过这里的 `Upstream` 碰腾讯；`http.py` 是真实现，测试里另有进程内的假实现
（`tests/support/fake_upstream.py`）。结果类型都是冻结的数据类，里面的文字已经把登录凭证、
对话凭证、收消息进度换成了 `***`。

发送的结果（`SendOutcome`）分五类，**重试策略不在这里，在 sender**：

- `Accepted`：腾讯接受了；
- `Rejected`：腾讯明确拒绝，重试也不会好；
- `SessionExpired`：登录失效（-14）；
- `Transient`：请求**没发出去**（连不上、证书握手失败、收到完整的 429 / 5xx），可以重试；
- `NoResponse`：请求**发出去了**，不知道结果（读超时、连接被重置、响应读到一半断开），**不重试**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

# ---------------------------------------------------------------- 账号


@dataclass(frozen=True)
class Account:
    """一个已登录的 ClawBot 账号。`bot_token` 不出现在 `repr` 里，免得误打进日志。"""

    bot_token: str = field(repr=False)
    base_url: str = ""  # 空 = 用 `upstream.base_url`
    bot_id: str = ""
    user_id: str = ""


# ---------------------------------------------------------------- 发送的结果


@dataclass(frozen=True)
class Accepted:
    """HTTP 200 且没有业务码。`message_id` 是腾讯给的消息编号，没有给就是 `None`。"""

    message_id: str | None = None


@dataclass(frozen=True)
class Rejected:
    """明确拒绝。`code` 是业务码（先取 `ret`，没有再取 `errcode`），没有业务码就是 `None`，
    这时 `http_status` 就是原因。"""

    http_status: int
    code: int | str | None
    errmsg: str = ""

    @property
    def detail(self) -> str:
        """给人看的原因，与 ashare-quant 一致：`40001 bad token`，只有 HTTP 码时 `HTTP 403`。"""
        head = self.code if self.code is not None else f"HTTP {self.http_status}"
        return f"{head} {self.errmsg}".strip()


@dataclass(frozen=True)
class SessionExpired:
    """业务码 -14：登录失效。"""


@dataclass(frozen=True)
class Transient:
    """请求没发出去，可以重试。"""

    reason: str


@dataclass(frozen=True)
class NoResponse:
    """请求发出去了，不知道到没到，不重试。`read_timeout` 区分"读超时"和"连接断了"。"""

    reason: str
    read_timeout: bool = False


SendOutcome = Accepted | Rejected | SessionExpired | Transient | NoResponse


# ---------------------------------------------------------------- 收消息的结果


@dataclass(frozen=True)
class IncomingMessage:
    """`getupdates` 里的一条消息，字段原样带出，丢不丢、给谁由 poller 决定。

    `text` 是所有文字项（`type` 为 1）依次拼接；`has_text` 表示有没有文字项（文字可以是空的）。
    `at_ms` 是腾讯给的 `create_time_ms`，没有（或不是正数）就是 `None`。"""

    from_user_id: str
    message_type: int | None
    context_token: str | None
    at_ms: int | None
    text: str
    has_text: bool
    message_id: str | None
    seq: int | None


@dataclass(frozen=True)
class Updates:
    """一次正常的收消息。

    `buf` 是新的收消息进度（腾讯没给就是 `None`）；`longpolling_timeout_ms` 是腾讯建议的
    长轮询时长；`timed_out` 表示这次是我们这边读超时（长轮询没等到消息，算没有新消息）。"""

    messages: tuple[IncomingMessage, ...] = ()
    buf: str | None = None
    longpolling_timeout_ms: int | None = None
    timed_out: bool = False


@dataclass(frozen=True)
class PollFailed:
    """收消息出错，不能当成"没有新消息"。

    `completed`：是否收到了完整的 HTTP 响应且状态码不是 429 / 5xx（即算一次"完成往返"，
    `hub-alerts` 的 `upstream_down` 据此计时）。403、`ret: 500` 是完成了往返的出错；
    连不上、证书握手失败、429、5xx、连接中途断掉都不是。"""

    reason: str
    completed: bool


PollOutcome = Updates | SessionExpired | PollFailed


# ---------------------------------------------------------------- 扫码


@dataclass(frozen=True)
class QrCode:
    code: str  # 查状态时要带的码
    image_content: str  # 要画成二维码的链接


@dataclass(frozen=True)
class QrStatus:
    """扫码状态。`status` 原样带出（`wait`、`scaned`、`expired`、`need_verifycode`……，
    不认识的也带出来，由登录流程决定怎么办）；`confirmed` 时带着新账号；
    `scaned_but_redirect` 时带着已经检查过格式的 `redirect_host`。"""

    status: str
    account: Account | None = None
    redirect_host: str | None = None


class UpstreamError(Exception):
    """扫码这类"一次一问"的调用没问成：连不上、状态不对、响应不是该有的样子。
    信息里的二维码、验证码、登录凭证已经换成 `***`。"""


# ---------------------------------------------------------------- 协议


class Upstream(Protocol):
    def get_updates(self, acct: Account, buf: str, timeout: float) -> PollOutcome: ...

    def send_text(
        self,
        acct: Account,
        to_user_id: str,
        context_token: str,
        text: str,
        client_id: str,
        timeout: float,
    ) -> SendOutcome: ...

    def qr_start(self) -> QrCode: ...

    def qr_status(self, code: str, verify_code: str | None, host: str | None) -> QrStatus: ...

    def notify_start(self, acct: Account) -> None: ...

    def notify_stop(self, acct: Account) -> None: ...
