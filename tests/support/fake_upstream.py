"""进程内的假上游（任务 A4）：实现 `Upstream` 协议的小类，不走网络栈，毫秒级。

给 sender、poller、额度、登录这些组件的测试用。要连 HTTP 的细节（请求头、请求体、超时、断连）
用 `fake_ilink.py`（本机回环上的真 HTTP 服务）；两种假件各有自己的测试。

## 默认行为

- `send_text`：`Accepted`，每次一个不同的消息编号（`message_ids=False` 时没有编号）；
- `get_updates`：按"位置"从收件箱里给，和 `fake_ilink` 的位置模型一样（`buf-<已收到的条数>`，
  空串从最近一次扫码确认的位置开始，旧的位置会把同一批再给一次，`redeliver=False` 时不再给）；
  没有新消息就挂住最多 `longpoll_seconds`（真实秒，也不超过调用者给的 `timeout`）；
- `qr_start` / `qr_status`：每次一张新码，默认一查就是 `confirmed`，带一个新账号；
- `notify_start` / `notify_stop`：什么也不做（真实现从不抛出）。

## 编排

- `script(操作, 条目, ...)`：给这个操作排一串结果，来一次用掉一个；用完回到默认；
- `set_default(操作, 条目)`：这个操作一直这样回，直到换掉（`None` 恢复内置行为）；剧本排在它前面；
- `set_mode(名字)`：`normal` / `disconnect` / `session_expired` / `http_5xx`，影响发送和收消息，
  名字与 `fake_ilink` 一致；
- 条目可以是：这个操作该有的结果对象；一个函数（拿到 `Call`，返回结果，或返回 `DEFAULT` 表示
  这一次走内置行为，要么自己抛异常）；一个异常（来这一次就抛出）。扫码状态还可以写状态名或字典
  （`{"status": "scaned_but_redirect", "redirect_host": "…"}`）；
- `Gate`：让一次调用卡在半路，测试决定什么时候放行（`hold(操作)` 让该操作的每次调用都卡住）；
  `after(clock, 秒)`：在假时钟上等一段再回；
- `qr_plan(状态串, ...)`：给接下来取的第 1、2、3…… 张码各排一串状态（每次查询用掉一个，
  用完重复最后一个）。

## 记录

`calls`（所有调用，按顺序）、`sent`（每一次 `send_text`，不管结果；`outcome` 在调用返回之前是
`None`）。调用一开始就记下，所以卡在半路的那一条也看得到。
"""

from __future__ import annotations

import inspect
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

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
)

SEND_TEXT = "send_text"
GET_UPDATES = "get_updates"
QR_START = "qr_start"
QR_STATUS = "qr_status"
NOTIFY_START = "notify_start"
NOTIFY_STOP = "notify_stop"
OPERATIONS = (SEND_TEXT, GET_UPDATES, QR_START, QR_STATUS, NOTIFY_START, NOTIFY_STOP)

DEFAULT_USER_ID = "fake-owner@im.wechat"
DEFAULT_BOT_ID = "fake-bot@im.bot"
# `.invalid` 永远解析不了：万一哪个组件测试误把真实现指向它，也连不到任何真地址
DEFAULT_BASE_URL = "https://fake-upstream.invalid"

# 卡在门口的调用最多等多久（真实秒）：到了还没放行就判测试失败，免得整个测试静悄悄地挂住
GATE_PATIENCE = 5.0


class _Marker:
    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:
        return self._name


DEFAULT: Any = _Marker("DEFAULT")  # 剧本函数的返回值：这一次走内置行为
_AUTO: Any = _Marker("_AUTO")  # "不指定"，区别于传 None（= 没有）
_NONE: Any = _Marker("_NONE")  # 剧本里没有下一个条目

MODES: dict[str, tuple[SendOutcome, PollFailed | SessionExpired] | None] = {
    "normal": None,
    "disconnect": (Transient("连不上"), PollFailed("连不上", completed=False)),
    "session_expired": (SessionExpired(), SessionExpired()),
    "http_5xx": (Transient("HTTP 503"), PollFailed("HTTP 503", completed=False)),
}

# 每个操作该返回什么（剧本函数的返回值按这个查）
_SEND_RESULTS = (Accepted, Rejected, SessionExpired, Transient, NoResponse)
_POLL_RESULTS = (Updates, SessionExpired, PollFailed)
_RESULTS: dict[str, tuple[type, ...]] = {
    SEND_TEXT: _SEND_RESULTS,
    GET_UPDATES: _POLL_RESULTS,
    QR_START: (QrCode,),
    QR_STATUS: (QrStatus, str, dict),
    NOTIFY_START: (type(None),),
    NOTIFY_STOP: (type(None),),
}
# 剧本里可以直接放的值（通知没有结果可放，只能放函数或异常）
_ITEMS: dict[str, tuple[type, ...]] = {
    **_RESULTS,
    NOTIFY_START: (),
    NOTIFY_STOP: (),
}
_STATUS_FIELDS = {"status", "redirect_host", "account"}


# ---------------------------------------------------------------- 记录


@dataclass(frozen=True)
class Call:
    """一次调用：第几次（所有操作一起数，从 1 起）、哪个操作、收到的参数（按协议里的参数名）。"""

    index: int
    operation: str
    args: dict[str, Any]


@dataclass
class SentMessage:
    """一次 `send_text`。`outcome` 在调用返回之前是 `None`（还在路上，或者抛了异常）。"""

    index: int  # 第几次 send_text，从 1 起
    call_index: int
    acct: Account
    to_user_id: str
    context_token: str
    text: str
    client_id: str
    timeout: float
    outcome: SendOutcome | None = None


@dataclass
class _Qr:
    plan: list[Any] | None
    calls: int = 0
    confirmed: QrStatus | None = None


@dataclass(frozen=True)
class _Ready:
    """内置行为给出的扫码状态，已经处理完了（和剧本里待转换的条目区分开）。"""

    status: QrStatus


# ---------------------------------------------------------------- 卡在半路和慢


class Gate:
    """一道门：调用到了门口就停住，测试调 `release()` 才放行。

    `entered` 在第一次有调用到门口时置位，`entries` 是到过门口的次数。`outcome` 是放行以后
    调用得到的结果（默认走内置行为）。放行之前等不过 `patience` 真实秒就抛 `AssertionError`。"""

    def __init__(self, outcome: Any = DEFAULT, *, patience: float = GATE_PATIENCE) -> None:
        self.entered = threading.Event()
        self.entries = 0
        self._outcome = outcome
        self._patience = patience
        self._open = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, call: Call) -> Any:
        with self._lock:
            self.entries += 1
        self.entered.set()
        if not self._open.wait(self._patience):
            raise AssertionError(
                f"第 {call.index} 次调用（{call.operation}）在门口等了 {self._patience} 秒"
                "（真实时间）也没有放行：测试忘了 release()？"
            )
        return self._outcome

    def release(self) -> None:
        self._open.set()


def after(clock: Any, seconds: float, outcome: Any = DEFAULT) -> Callable[[Call], Any]:
    """剧本函数：在 `clock`（假时钟）上睡 `seconds` 秒再回 `outcome`（默认走内置行为）。"""

    def delayed(call: Call) -> Any:
        clock.sleep(seconds)
        return outcome

    return delayed


def an_account(**overrides: str) -> Account:
    """测试里随手要一个账号时用。"""
    fields = {
        "bot_token": "fake-token-0",
        "base_url": DEFAULT_BASE_URL,
        "bot_id": DEFAULT_BOT_ID,
        "user_id": DEFAULT_USER_ID,
    }
    fields.update(overrides)
    return Account(**fields)


# ---------------------------------------------------------------- 假上游


class FakeUpstream:
    def __init__(
        self,
        *,
        clock: Any = None,
        user_id: str = DEFAULT_USER_ID,
        bot_id: str = DEFAULT_BOT_ID,
        base_url: str = DEFAULT_BASE_URL,
        longpoll_seconds: float = 0.05,
        longpoll_hint_ms: int | None = None,
        message_ids: bool = True,
        redeliver: bool = True,
    ) -> None:
        self.user_id = user_id
        self.bot_id = bot_id
        self.base_url = base_url
        self.longpoll_seconds = longpoll_seconds
        self.longpoll_hint_ms = longpoll_hint_ms
        self.message_ids = message_ids
        self.redeliver = redeliver
        self.logins = 0  # 扫码确认过几次
        self._clock = clock
        self._cv = threading.Condition()
        self._closed = False
        self._calls: list[Call] = []
        self._sent: list[SentMessage] = []
        self._scripts: dict[str, deque[Any]] = {op: deque() for op in OPERATIONS}
        self._defaults: dict[str, Any] = {}
        self._inbox: list[IncomingMessage] = []
        self._floor = 0  # 空位置从这里开始（最近一次扫码确认时收件箱的长度）
        self._delivered = 0  # 已经给出去过的最大位置（redeliver=False 用）
        self._incoming_ids = 0
        self._outgoing_ids = 0
        self._qr_issued = 0
        self._qr_plans: deque[list[Any]] = deque()
        self._qr: dict[str, _Qr] = {}

    def __enter__(self) -> FakeUpstream:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """叫醒所有挂着的收消息；之后的收消息不再等。"""
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    # ------------------------------------------------ 记录

    @property
    def calls(self) -> list[Call]:
        with self._cv:
            return list(self._calls)

    def calls_to(self, operation: str) -> list[Call]:
        _check_operation(operation)
        return [c for c in self.calls if c.operation == operation]

    @property
    def sent(self) -> list[SentMessage]:
        with self._cv:
            return list(self._sent)

    def sent_texts(self) -> list[str]:
        return [m.text for m in self.sent]

    def clear_calls(self) -> None:
        """清掉调用记录和发送记录（收件箱、消息编号、剧本不动）。"""
        with self._cv:
            self._calls.clear()
            self._sent.clear()

    # ------------------------------------------------ 编排

    def script(self, operation: str, *items: Any) -> None:
        _check_operation(operation)
        for item in items:
            _check_item(operation, item)
        with self._cv:
            self._scripts[operation].extend(items)

    def set_default(self, operation: str, item: Any) -> None:
        _check_operation(operation)
        if item is not None:
            _check_item(operation, item)
        with self._cv:
            if item is None:
                self._defaults.pop(operation, None)
            else:
                self._defaults[operation] = item

    def clear_scripts(self) -> None:
        """清掉没用完的剧本和扫码计划；`set_default` 设下的不动。"""
        with self._cv:
            for queue in self._scripts.values():
                queue.clear()
            self._qr_plans.clear()

    def set_mode(self, mode: str) -> None:
        """`normal` / `disconnect` / `session_expired` / `http_5xx`，影响发送和收消息。"""
        if mode not in MODES:
            raise ValueError(f"不认识的模式 {mode!r}，可选：{', '.join(MODES)}")
        sending, polling = MODES[mode] or (None, None)
        self.set_default(SEND_TEXT, sending)
        self.set_default(GET_UPDATES, polling)

    def hold(self, operation: str) -> Gate:
        """这个操作的每一次调用都卡在门口，直到返回的门 `release()`。"""
        gate = Gate()
        self.set_default(operation, gate)
        return gate

    def qr_plan(self, *sequences: list[Any]) -> None:
        """给接下来取的第 1、2、3…… 张码各排一串状态；每串里每次查询用掉一个，用完重复最后一个。
        条目是状态名、字典（`status`，可选 `redirect_host`、`account`）或现成的 `QrStatus`。
        没排到的码一查就是 `confirmed`。"""
        checked: list[list[Any]] = []
        for sequence in sequences:
            entries = list(sequence)
            if not entries:
                raise ValueError("每一串状态至少要有一个")
            for entry in entries:
                _check_status_entry(entry)
            checked.append(entries)
        with self._cv:
            self._qr_plans.extend(checked)

    # ------------------------------------------------ 收件箱

    def push_user_message(
        self,
        text: str = "",
        *,
        context_token: str | None = _AUTO,
        at_ms: int | None = _AUTO,
        from_user_id: str | None = None,
        message_type: int = 1,
        has_text: bool = True,
    ) -> IncomingMessage:
        """使用者发来一条消息，返回放进收件箱的那条。`context_token`、`at_ms` 不给就自动编 / 取当前
        时间，传 `None` 表示这条消息没有。"""
        if not has_text and text:
            raise ValueError("has_text=False 的消息不能有文字")
        with self._cv:
            number = len(self._inbox) + 1
            self._incoming_ids += 1
            message = IncomingMessage(
                from_user_id=from_user_id if from_user_id is not None else self.user_id,
                message_type=message_type,
                context_token=f"ctx-{number}" if context_token is _AUTO else context_token,
                at_ms=self._now_ms() if at_ms is _AUTO else at_ms,
                text=text,
                has_text=has_text,
                message_id=str(7_000_000 + self._incoming_ids),
                seq=number,
            )
            self._inbox.append(message)
            self._cv.notify_all()
            return message

    def push_bot_message(self, text: str = "") -> IncomingMessage:
        """机器人自己发的消息（`message_type` 为 2）也会出现在收件箱里时的样子。"""
        return self.push_user_message(
            text, message_type=2, from_user_id=self.bot_id, context_token=None
        )

    @property
    def inbox_size(self) -> int:
        with self._cv:
            return len(self._inbox)

    # ------------------------------------------------ Upstream 协议

    def get_updates(self, acct: Account, buf: str, timeout: float) -> PollOutcome:
        call = self._record(GET_UPDATES, {"acct": acct, "buf": buf, "timeout": timeout})
        return self._resolve(call, self._deliver)

    def send_text(
        self,
        acct: Account,
        to_user_id: str,
        context_token: str,
        text: str,
        client_id: str,
        timeout: float,
    ) -> SendOutcome:
        args = {
            "acct": acct,
            "to_user_id": to_user_id,
            "context_token": context_token,
            "text": text,
            "client_id": client_id,
            "timeout": timeout,
        }
        with self._cv:
            call = self._new_call(SEND_TEXT, args)
            record = SentMessage(index=len(self._sent) + 1, call_index=call.index, **args)
            self._sent.append(record)
        outcome = self._resolve(call, self._accept)
        with self._cv:
            record.outcome = outcome
        return outcome

    def qr_start(self) -> QrCode:
        call = self._record(QR_START, {})
        return self._resolve(call, self._new_qr)

    def qr_status(self, code: str, verify_code: str | None, host: str | None) -> QrStatus:
        call = self._record(QR_STATUS, {"code": code, "verify_code": verify_code, "host": host})
        result = self._resolve(call, lambda c: _Ready(self._planned_status(code)))
        return result.status if isinstance(result, _Ready) else self._make_status(result)

    def notify_start(self, acct: Account) -> None:
        self._resolve(self._record(NOTIFY_START, {"acct": acct}), lambda c: None)

    def notify_stop(self, acct: Account) -> None:
        self._resolve(self._record(NOTIFY_STOP, {"acct": acct}), lambda c: None)

    # ------------------------------------------------ 内部：记录和剧本

    def _now_ms(self) -> int:
        if self._clock is not None:
            return self._clock.now_ms()
        return time.time_ns() // 1_000_000

    def _new_call(self, operation: str, args: dict[str, Any]) -> Call:
        call = Call(index=len(self._calls) + 1, operation=operation, args=args)
        self._calls.append(call)
        return call

    def _record(self, operation: str, args: dict[str, Any]) -> Call:
        with self._cv:
            return self._new_call(operation, args)

    def _resolve(self, call: Call, built_in: Callable[[Call], Any]) -> Any:
        """这一次调用该回什么：先看剧本的下一个条目，再看 `set_default`，最后是内置行为。
        条目放在锁外面跑，所以卡在门口的调用不会挡住别的调用。"""
        operation = call.operation
        with self._cv:
            queue = self._scripts[operation]
            scripted = queue.popleft() if queue else _NONE
            standing = self._defaults.get(operation, _NONE)
        for item in (scripted, standing):
            if item is _NONE:
                continue
            result = self._run(item, call)
            if result is not DEFAULT:
                return result
        return built_in(call)

    def _run(self, item: Any, call: Call) -> Any:
        if isinstance(item, BaseException):
            raise item
        if item is DEFAULT:
            return DEFAULT
        if callable(item):
            result = item(call)
            if result is not DEFAULT:
                _check_result(call.operation, result)
            return result
        return item

    # ------------------------------------------------ 内部：内置行为

    def _accept(self, call: Call) -> Accepted:
        with self._cv:
            self._outgoing_ids += 1
            number = self._outgoing_ids
        return Accepted(f"fake-msg-{number}" if self.message_ids else None)

    def _deliver(self, call: Call) -> Updates:
        buf, timeout = call.args["buf"], call.args["timeout"]
        deadline = time.monotonic() + max(0.0, min(timeout, self.longpoll_seconds))
        with self._cv:
            position = self._floor
            if isinstance(buf, str) and buf.startswith("buf-") and buf[4:].isdigit():
                position = int(buf[4:])
            start = position if self.redeliver else max(position, self._delivered)
            while len(self._inbox) <= start and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cv.wait(remaining)
            messages = tuple(self._inbox[start:])
            end = len(self._inbox)
            self._delivered = max(self._delivered, end)
        new_buf = f"buf-{end if messages else max(end, position)}"
        return Updates(messages=messages, buf=new_buf, longpolling_timeout_ms=self.longpoll_hint_ms)

    def _new_qr(self, call: Call) -> QrCode:
        with self._cv:
            self._qr_issued += 1
            code = f"fake-qr-{self._qr_issued}"
            plan = self._qr_plans.popleft() if self._qr_plans else None
            self._qr[code] = _Qr(plan)
        return QrCode(code=code, image_content=f"{self.base_url}/q?qrcode={code}&bot_type=3")

    def _planned_status(self, code: str) -> QrStatus:
        with self._cv:
            qr = self._qr.get(code)
            if qr is None:
                return QrStatus("expired")
            if qr.confirmed is not None:
                return qr.confirmed
            entry: Any = "confirmed"
            if qr.plan:
                entry = qr.plan[min(qr.calls, len(qr.plan) - 1)]
            qr.calls += 1
        status = self._make_status(entry)
        if status.status == "confirmed":
            with self._cv:
                qr.confirmed = status
        return status

    def _make_status(self, entry: Any) -> QrStatus:
        """状态名、字典、`QrStatus` 变成 `QrStatus`；确认了就算一次登录：补一个新账号（如果没带），
        并把空位置挪到现在的收件箱末尾（新登录只看得到新消息）。"""
        if isinstance(entry, QrStatus):
            status = entry
        elif isinstance(entry, str):
            status = QrStatus(entry)
        else:
            status = QrStatus(
                entry["status"],
                account=entry.get("account"),
                redirect_host=entry.get("redirect_host"),
            )
        if status.status != "confirmed":
            return status
        with self._cv:
            self.logins += 1
            number = self.logins
            self._floor = len(self._inbox)
        if status.account is None:
            account = Account(
                bot_token=f"fake-token-{number}",
                base_url=self.base_url,
                bot_id=self.bot_id,
                user_id=self.user_id,
            )
            status = replace(status, account=account)
        return status


# ---------------------------------------------------------------- 检查


def _check_operation(operation: str) -> None:
    if operation not in OPERATIONS:
        raise ValueError(f"不认识的操作 {operation!r}，可选：{', '.join(OPERATIONS)}")


def _kinds(types: tuple[type, ...]) -> str:
    return "、".join(t.__name__ for t in types) or "（没有）"


def _check_item(operation: str, item: Any) -> None:
    """剧本和默认里可以放的东西：异常、函数、`DEFAULT`、或这个操作该有的结果。"""
    if inspect.isclass(item):
        raise TypeError(f"{operation} 的剧本里放的是类 {item.__name__}，要放它的实例")
    if isinstance(item, BaseException) or item is DEFAULT or callable(item):
        return
    allowed = _ITEMS[operation]
    if operation == QR_STATUS and isinstance(item, str | dict):
        _check_status_entry(item)
        return
    if isinstance(item, allowed):
        return
    raise TypeError(
        f"{operation} 的剧本里放不进 {item!r}：要 {_kinds(allowed)}，或者一个函数、一个异常"
    )


def _check_result(operation: str, result: Any) -> None:
    allowed = _RESULTS[operation]
    if not isinstance(result, allowed):
        raise TypeError(f"{operation} 的剧本函数返回了 {result!r}：要 {_kinds(allowed)}")
    if operation == QR_STATUS and isinstance(result, str | dict):
        _check_status_entry(result)


def _check_status_entry(entry: Any) -> None:
    if isinstance(entry, QrStatus | str):
        return
    if isinstance(entry, dict) and isinstance(entry.get("status"), str):
        unknown = set(entry) - _STATUS_FIELDS
        if not unknown:
            return
        raise TypeError(
            f"扫码状态字典里有不认识的键 {sorted(unknown)}，可用：{sorted(_STATUS_FIELDS)}"
        )
    raise TypeError(f"扫码状态要是状态名、带 status 的字典或 QrStatus，不是 {entry!r}")
