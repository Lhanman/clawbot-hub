"""真机验证（E4）用的探针：每一项清单一个函数，只做清单上写的请求，只记"形状"。

形状 = HTTP 状态、返回体顶层的键名和值的类型、耗时、条数、有没有。**不记**登录凭证、对话凭证、
二维码、使用者和机器人的编号、消息正文：`Recorder` 写之前核对，字段里含已知秘密、或者长得像
正文，就直接报错、不写。结果追加在数据目录的 `e4-results.jsonl`（0600），同时打印成一行行字。

规矩（和 `docs/ops/真机验证清单.md` 一致）：

- 探针和服务不同时向腾讯收消息：同一时间只能有一个程序收；
- 回 -14（登录失效）就停（`SessionLost`），**不要扫码**，先恢复旧的微信问答，再告诉使用者；
- 不调 `notifystop`；发消息有总条数上限（`max_sends`）；
- 需要使用者发话的地方打印以 `▶ 请用户` 开头的一行，跑探针的人照着转告。

这个模块不带 `test_` 前缀，不是测试；`test_real_ilink.py` 里的真机测试和 `test_probes_selftest.py`
（对着假腾讯自测，每次都跑）都调它。
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import urlsplit

import requests

from clawbot_hub.account import AccountFile, AccountStore
from clawbot_hub.config import UpstreamConfig
from clawbot_hub.paths import FILE_MODE, HubPaths
from clawbot_hub.textutil import mask
from clawbot_hub.upstream import http as upstream_http
from clawbot_hub.upstream.base import (
    Accepted,
    Account,
    IncomingMessage,
    PollFailed,
    SessionExpired,
    UpstreamError,
)
from clawbot_hub.upstream.http import HttpUpstream
from clawbot_hub.upstream.profiles import get_profile

HOME_ENV = "HUB_LIVE_HOME"
WAIT_ENV = "HUB_LIVE_WAIT_SECONDS"
REPLY_MINUTES_ENV = "HUB_LIVE_REPLY_MINUTES"
BASE_URL_ENV = "HUB_LIVE_BASE_URL"
RESULTS_ENV = "HUB_LIVE_RESULTS"
MAX_SENDS_ENV = "HUB_LIVE_MAX_SENDS"

RESULTS_NAME = "e4-results.jsonl"
CONTEXT_NAME = "e4-last-context.json"
SENDS_NAME = "e4-sends.count"

DEFAULT_WAIT_SECONDS = 900.0
DEFAULT_REPLY_MINUTES = (0.0, 1.0, 2.0, 5.0, 10.0, 30.0)
# 发消息用的对话凭证最多多旧：凭证的有效期可能只有 1 到 2 分钟（第 6 项就是量这个），
# 第 1、2 项不该被"凭证过期"搅乱，所以太旧就让使用者再发一句
DEFAULT_CONTEXT_MAX_AGE_SECONDS = 90.0
DEFAULT_MAX_SENDS = 20
# 字段值超过这个长度就当成可能是正文，不记
MAX_FIELD_CHARS = 120

NOTIFY_PATHS = ("/ilink/bot/notifystart", "/ilink/bot/msg/notifystart")

SESSION_LOST_HINT = (
    "腾讯回了 -14（登录失效）。停下来：不要扫码，先恢复旧的微信问答，再告诉使用者；"
    "要不要重新扫码由使用者定"
)


class ProbeError(Exception):
    """探针没法继续。"""


class LiveNotConfigured(ProbeError):
    """没设 `HUB_LIVE_HOME`：不是 E4 的真机验证，真机测试跳过。"""


class SessionLost(ProbeError):
    """腾讯回了 -14。"""


class WaitTimeout(ProbeError):
    """等使用者发话等太久了。"""


class SecretLeak(ProbeError):
    """要记的东西里有秘密或像正文，没有写。"""


# ---------------------------------------------------------------- 时钟


@dataclass
class Clock:
    """探针里等和量用的时钟；自测里换成不真睡的。"""

    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    wall_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000  # noqa: E731


# ---------------------------------------------------------------- 形状


@dataclass(frozen=True)
class Shape:
    """一次请求的形状。没有拿到完整响应时 `status` 是 `None`、`failure` 说明原因。"""

    method: str
    path: str
    status: int | None
    keys: tuple[str, ...]
    types: dict[str, str]
    nested: dict[str, tuple[str, ...]]
    ret: int | str | None
    errcode: int | str | None
    errmsg: str
    seconds: float
    failure: str | None = None


def _plain(value: Any) -> int | str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and len(value) <= 20:
        return value
    return None


def _shape_of(
    method: str, path: str, reply: Any, secrets: Iterable[str | None], seconds: float
) -> Shape:
    body: Mapping[str, Any] = reply.body
    errmsg = body.get("errmsg")
    text = mask(errmsg[:80], secrets) if isinstance(errmsg, str) else ""
    return Shape(
        method=method,
        path=path,
        status=reply.status,
        keys=tuple(sorted(body)),
        types={key: type(value).__name__ for key, value in sorted(body.items())},
        nested={
            key: tuple(sorted(str(name) for name in value))
            for key, value in sorted(body.items())
            if isinstance(value, dict)
        },
        ret=_plain(body.get("ret")),
        errcode=_plain(body.get("errcode")),
        errmsg=text,
        seconds=round(seconds, 2),
    )


def _failed(method: str, path: str, started: float, failure: str) -> Shape:
    return Shape(
        method,
        path,
        None,
        (),
        {},
        {},
        None,
        None,
        "",
        round(time.monotonic() - started, 2),
        failure,
    )


@contextlib.contextmanager
def watching() -> Iterator[list[Shape]]:
    """记下块里每一次发给腾讯的请求的形状（从 `upstream.http._exchange` 这一层看，
    所以看得到 `HttpUpstream` 自己丢掉的返回体键名）；请求和结果照常，只多记一笔。"""
    shapes: list[Shape] = []
    real = upstream_http._exchange

    def spy(method, url, *, headers, timeout, secrets, params=None, json_body=None):
        started = time.monotonic()
        path = urlsplit(url).path
        try:
            reply = real(
                method,
                url,
                headers=headers,
                timeout=timeout,
                secrets=secrets,
                params=params,
                json_body=json_body,
            )
        except upstream_http._NotSent:
            shapes.append(_failed(method, path, started, "not_sent"))
            raise
        except upstream_http._Unknown as exc:
            failure = "read_timeout" if exc.read_timeout else "no_response"
            shapes.append(_failed(method, path, started, failure))
            raise
        shapes.append(_shape_of(method, path, reply, secrets, time.monotonic() - started))
        return reply

    with mock.patch.object(upstream_http, "_exchange", spy):
        yield shapes


def shape_fields(shape: Shape | None) -> dict[str, Any]:
    """形状变成可以记的字段。"""
    if shape is None:
        return {"status": None, "failure": "没有请求"}
    fields: dict[str, Any] = {
        "path": shape.path,
        "status": shape.status,
        "keys": list(shape.keys),
        "types": shape.types,
        "seconds": shape.seconds,
    }
    if shape.nested:
        fields["nested_keys"] = {key: list(names) for key, names in shape.nested.items()}
    for name in ("ret", "errcode"):
        value = getattr(shape, name)
        if value is not None:
            fields[name] = value
    if shape.errmsg:
        fields["errmsg"] = shape.errmsg
    if shape.failure:
        fields["failure"] = shape.failure
    return fields


def proxy_in_effect(url: str) -> bool:
    """`requests` 访问这个地址时会不会走代理（环境变量里的，Mac 上还有系统设置里的）。"""
    return bool(requests.utils.select_proxy(url, requests.utils.get_environ_proxies(url)))


# ---------------------------------------------------------------- 记录


class Recorder:
    """把结果追加到 jsonl 文件，同时说一行。含已知秘密或像正文的字段，报错不写。"""

    def __init__(
        self,
        path: Path | None,
        say: Callable[[str], None],
        clock: Clock,
    ) -> None:
        self._path = path
        self._say = say
        self._clock = clock
        self._secrets: set[str] = set()

    def watch_secret(self, *values: str | None) -> None:
        self._secrets.update(value for value in values if value)

    def record(self, item: str, step: str, **fields: Any) -> dict[str, Any]:
        line: dict[str, Any] = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._clock.wall_ms() / 1000)),
            "item": item,
            "step": step,
            **fields,
        }
        text = json.dumps(line, ensure_ascii=False, sort_keys=True)
        for secret in self._secrets:
            if secret in text:
                raise SecretLeak(f"{item}/{step}：要记的字段里有秘密，没有写")
        for name, value in _walk(fields):
            if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
                raise SecretLeak(f"{item}/{step}：字段 {name} 太长，像是正文，没有写")
        if self._path is not None:
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(text + "\n")
        short = " ".join(f"{name}={_brief(value)}" for name, value in fields.items())
        self._say(f"[{item}] {step}: {short}".rstrip())
        return line


def _walk(value: Any, label: str = "") -> Iterator[tuple[str, Any]]:
    """把嵌套的字典、列表摊平成（名字, 叶子值），好逐个检查。"""
    if isinstance(value, Mapping):
        for name, item in value.items():
            yield from _walk(item, f"{label}.{name}" if label else str(name))
    elif isinstance(value, (list, tuple)):
        for index, element in enumerate(value):
            yield from _walk(element, f"{label}[{index}]")
    else:
        yield label, value


def _brief(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value)


# ---------------------------------------------------------------- 环境


@dataclass(frozen=True)
class Context:
    """回复要用的对话凭证（使用者最近一条消息带来的）。"""

    to_user_id: str = field(repr=False)
    token: str = field(repr=False)
    at_ms: int


@dataclass(frozen=True)
class Received:
    """等到的使用者消息。"""

    messages: tuple[IncomingMessage, ...]
    buf_before: str = field(repr=False)
    buf_after: str = field(repr=False)
    waited_seconds: float

    def latest(self) -> IncomingMessage:
        return max(self.messages, key=lambda m: m.at_ms or 0)


class ProbeEnv:
    """一次真机验证的环境：数据目录（登录凭证、结果文件）、上游配置、时钟、输出。"""

    def __init__(
        self,
        home: Path,
        *,
        config: UpstreamConfig | None = None,
        clock: Clock | None = None,
        say: Callable[[str], None] | None = None,
        results_path: Path | None = None,
        wait_seconds: float = DEFAULT_WAIT_SECONDS,
        reply_minutes: tuple[float, ...] = DEFAULT_REPLY_MINUTES,
        context_max_age_seconds: float = DEFAULT_CONTEXT_MAX_AGE_SECONDS,
        max_sends: int = DEFAULT_MAX_SENDS,
    ) -> None:
        self.home = home
        self.paths = HubPaths(home)
        self.store = AccountStore(self.paths)
        self.config = config or UpstreamConfig()
        self.clock = clock or Clock()
        self.say = say or (lambda text: print(text, flush=True))
        self.wait_seconds = wait_seconds
        self.reply_minutes = reply_minutes
        self.context_max_age_seconds = context_max_age_seconds
        self.max_sends = max_sends
        results = results_path if results_path is not None else home / RESULTS_NAME
        self.recorder = Recorder(results, self.say, self.clock)
        self._known: AccountFile | None = None

    # ---- 账号 ----

    def account_file(self) -> AccountFile:
        found = self.store.read()
        if found is None:
            raise ProbeError(
                "E4 的数据目录里没有 account.json：先停旧的微信问答，"
                "再用 hubctl import-credentials 导入（见 docs/ops/真机验证清单.md）"
            )
        if self._known is None:
            self.recorder.watch_secret(found.bot_token, found.user_id, found.bot_id)
            self._known = found
        return found

    def account(self) -> Account:
        return self.account_file().to_account()

    def upstream(self, profile: str) -> HttpUpstream:
        return HttpUpstream(dataclasses.replace(self.config, profile=profile))

    # ---- 收消息进度（和服务共用 account.json 里的 updates_buf）----

    def cursor(self) -> str:
        return self.account_file().updates_buf

    def save_cursor(self, buf: str) -> None:
        account = self.account_file()
        if buf and buf != account.updates_buf:
            self.store.save_progress(account.bot_token, buf)
            self.recorder.watch_secret(buf)

    # ---- 已经发了几条（记在数据目录里：几个探针分几次跑，上限是合计的）----

    @property
    def _sends_path(self) -> Path:
        return self.home / SENDS_NAME

    @property
    def sends(self) -> int:
        try:
            return max(0, int(self._sends_path.read_text(encoding="utf-8").strip()))
        except (OSError, ValueError):
            return 0

    def count_send(self) -> int:
        total = self.sends + 1
        fd = os.open(self._sends_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(total))
        return total

    # ---- 对话凭证 ----

    @property
    def _context_path(self) -> Path:
        return self.home / CONTEXT_NAME

    def save_context(self, context: Context) -> None:
        self.recorder.watch_secret(context.token)
        data = {"to_user_id": context.to_user_id, "token": context.token, "at_ms": context.at_ms}
        fd = os.open(self._context_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def load_context(self) -> Context | None:
        try:
            data = json.loads(self._context_path.read_text(encoding="utf-8"))
            context = Context(str(data["to_user_id"]), str(data["token"]), int(data["at_ms"]))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        self.recorder.watch_secret(context.token)
        return context


def live_env(
    environ: Mapping[str, str] | None = None,
    *,
    say: Callable[[str], None] | None = None,
) -> ProbeEnv:
    """按环境变量造真机验证的环境。没设 `HUB_LIVE_HOME` 抛 `LiveNotConfigured`。

    - `HUB_LIVE_HOME`：E4 的数据目录（里面要有 account.json，探针的结果也写在这里）；
    - `HUB_LIVE_WAIT_SECONDS`：等使用者发话最多等多久，默认 900；
    - `HUB_LIVE_REPLY_MINUTES`：第 6 项隔多久回一条，默认 `0,1,2,5,10,30`；
    - `HUB_LIVE_BASE_URL`：腾讯的地址，默认官方的；只有拿假腾讯自测时才改；
    - `HUB_LIVE_RESULTS`：结果文件，默认 `<数据目录>/e4-results.jsonl`；
    - `HUB_LIVE_MAX_SENDS`：探针一共最多发几条，默认 20（已发的条数记在数据目录的
      `e4-sends.count`，分几次跑也合计）。
    """
    env = os.environ if environ is None else environ
    raw = env.get(HOME_ENV, "").strip()
    if not raw:
        raise LiveNotConfigured(
            f"没设 {HOME_ENV}：只有 E4 在真账号上验证时才设它（E4 的数据目录），"
            "见 docs/ops/真机验证清单.md"
        )
    home = Path(raw).expanduser()
    base_url = env.get(BASE_URL_ENV, "").strip()
    config = UpstreamConfig(base_url=base_url) if base_url else UpstreamConfig()
    minutes = env.get(REPLY_MINUTES_ENV, "").strip()
    results = env.get(RESULTS_ENV, "").strip()
    return ProbeEnv(
        home,
        config=config,
        say=say,
        results_path=Path(results).expanduser() if results else None,
        wait_seconds=float(env.get(WAIT_ENV, "") or DEFAULT_WAIT_SECONDS),
        max_sends=int(env.get(MAX_SENDS_ENV, "") or DEFAULT_MAX_SENDS),
        reply_minutes=(
            tuple(float(part) for part in minutes.split(",") if part.strip())
            if minutes
            else DEFAULT_REPLY_MINUTES
        ),
    )


# ---------------------------------------------------------------- 公共动作


def _lost(env: ProbeEnv, item: str, where: str) -> SessionLost:
    env.recorder.record(item, where, outcome="SessionExpired", ret=-14)
    return SessionLost(SESSION_LOST_HINT)


def wait_for_user_message(
    env: ProbeEnv, up: HttpUpstream, *, item: str, purpose: str, save_cursor: bool = True
) -> Received:
    """一直收，直到使用者发来一条带对话凭证的消息（最多等 `wait_seconds`）。
    调用的时候服务必须是停着的。收到的消息只留下条数和编号的形状，正文不看不记。"""
    account = env.account()
    before = env.cursor()
    buf = before
    env.say(f"▶ 请用户现在在微信里给 ClawBot 发一句话（{purpose}），内容随意，比如“测试”。我在等。")
    started = env.clock.monotonic()
    next_reminder = started + 60
    while True:
        outcome = up.get_updates(account, buf, float(env.config.poll_timeout))
        if isinstance(outcome, SessionExpired):
            raise _lost(env, item, "wait_for_user")
        if isinstance(outcome, PollFailed):
            env.recorder.record(
                item,
                "wait_poll_failed",
                completed=outcome.completed,
                reason=mask(outcome.reason, (account.bot_token,))[:100],
            )
            env.clock.sleep(5.0)
        else:
            if outcome.buf:
                buf = outcome.buf
            mine = tuple(
                m for m in outcome.messages if m.from_user_id == account.user_id and m.context_token
            )
            if mine:
                if save_cursor:
                    env.save_cursor(buf)
                for message in mine:
                    env.recorder.watch_secret(message.context_token)
                return Received(mine, before, buf, env.clock.monotonic() - started)
        now = env.clock.monotonic()
        if now - started > env.wait_seconds:
            raise WaitTimeout(f"等了 {int(now - started)} 秒，没有收到使用者的消息")
        if now >= next_reminder:
            env.say(f"…还在等使用者的消息（已等 {int(now - started)} 秒）")
            next_reminder = now + 60


def fresh_context(env: ProbeEnv, up: HttpUpstream, *, item: str, purpose: str) -> Context:
    """可以拿来回复的对话凭证：上一次存的还够新就用它，否则等使用者再发一句。"""
    saved = env.load_context()
    if saved is not None:
        age = (env.clock.wall_ms() - saved.at_ms) / 1000
        if -30 <= age <= env.context_max_age_seconds:  # 本机时钟和腾讯的差几秒也算
            env.say(f"（用刚才那条消息的对话凭证，{int(age)} 秒前收到的）")
            return saved
    received = wait_for_user_message(env, up, item=item, purpose=purpose)
    latest = received.latest()
    context = Context(
        latest.from_user_id, latest.context_token or "", latest.at_ms or env.clock.wall_ms()
    )
    env.save_context(context)
    return context


@dataclass(frozen=True)
class SendResult:
    outcome: str  # Accepted / Rejected / SessionExpired / Transient / NoResponse
    shape: Shape | None
    message_id_found: bool


def send_probe(
    env: ProbeEnv,
    up: HttpUpstream,
    *,
    item: str,
    step: str,
    to_user_id: str,
    token: str,
    text: str,
    profile: str,
) -> SendResult:
    """发一条探针消息并记形状。碰到 -14 就停（`SessionLost`）。"""
    if env.sends >= env.max_sends:
        raise ProbeError(
            f"探针一共已经发了 {env.sends} 条，到上限了（{MAX_SENDS_ENV}）：先看一下额度再说"
        )
    account = env.account()
    env.count_send()
    with watching() as shapes:
        outcome = up.send_text(
            account,
            to_user_id,
            token,
            text,
            f"e4-{uuid.uuid4().hex[:16]}",
            float(env.config.request_timeout),
        )
    shape = shapes[-1] if shapes else None
    found = isinstance(outcome, Accepted) and outcome.message_id is not None
    fields = shape_fields(shape)
    env.recorder.record(
        item,
        step,
        profile=profile,
        outcome=type(outcome).__name__,
        message_id_found=found,
        has_context_token=bool(token),
        sends_so_far=env.sends,
        **fields,
    )
    if isinstance(outcome, SessionExpired):
        raise SessionLost(SESSION_LOST_HINT)
    return SendResult(type(outcome).__name__, shape, found)


def _idle_poll(env: ProbeEnv, up: HttpUpstream, buf: str, timeout: float):
    account = env.account()
    with watching() as shapes:
        started = env.clock.monotonic()
        outcome = up.get_updates(account, buf, timeout)
        elapsed = env.clock.monotonic() - started
    return outcome, (shapes[-1] if shapes else None), elapsed


def _classify(outcome: Any) -> str:
    if isinstance(outcome, SessionExpired):
        return "session_expired"
    if isinstance(outcome, PollFailed):
        return "failed"
    if outcome.timed_out:
        return "client_read_timeout"
    return "messages" if outcome.messages else "empty_response"


# ---------------------------------------------------------------- 各项探针


def official_qrcode_post(env: ProbeEnv) -> dict[str, Any]:
    """第 3 项：official 的 POST 取二维码；legacy 的 GET 作对照。只取码，不扫，码也不记。"""
    item = "official_qrcode_post"
    base = env.config.base_url
    out: dict[str, Any] = {}
    for profile in ("legacy", "official"):
        up = env.upstream(profile)
        ok, why = True, ""
        with watching() as shapes:
            try:
                up.qr_start()
            except UpstreamError as exc:
                ok, why = False, str(exc)[:100]
        shape = shapes[0] if shapes else None
        method = shape.method if shape else ""
        env.recorder.record(
            item,
            f"qr_start_{profile}",
            method=method,
            ok=ok,
            error=why,
            via_proxy=proxy_in_effect(base),
            **shape_fields(shape),
        )
        out[profile] = {"ok": ok, "keys": list(shape.keys) if shape else []}
    out["same_keys"] = out["legacy"]["keys"] == out["official"]["keys"]
    env.recorder.record(item, "compare", same_keys=out["same_keys"])
    return out


def notify_paths(env: ProbeEnv) -> dict[str, Any]:
    """第 4 项：official 档 `notifystart` 的两个路径各请求一次，记状态码和返回体的形状。
    只测 `notifystart`，**不调 `notifystop`**（不知道它会不会让腾讯停止投递）。"""
    item = "notify_paths"
    account = env.account()
    profile = get_profile("official")
    base = (account.base_url or env.config.base_url).rstrip("/")
    out: dict[str, Any] = {}
    for path in NOTIFY_PATHS:
        with watching() as shapes:
            try:
                upstream_http._exchange(
                    "POST",
                    base + path,
                    headers=profile.bot_headers(account.bot_token),
                    json_body={"base_info": profile.base_info()},
                    timeout=(float(env.config.request_timeout), float(env.config.request_timeout)),
                    secrets=(account.bot_token,),
                )
            except (upstream_http._NotSent, upstream_http._Unknown):
                pass  # 形状里已经记了 failure
        shape = shapes[0] if shapes else None
        env.recorder.record(item, "notifystart", request_path=path, **shape_fields(shape))
        out[path] = shape.status if shape else None
    return out


def long_poll_timeout_round_trip(
    env: ProbeEnv, *, idle_runs: int = 2, extra_seconds: float = 45.0
) -> dict[str, Any]:
    """第 9 项：没有新消息时发 `getupdates`，量腾讯多久回、回的是什么。客户端读超时放宽到
    `poll_timeout + extra_seconds`，这样"腾讯自己回了个空结果"和"挂到我们读超时"分得开。
    要求使用者这段时间不发消息；服务必须是停着的。"""
    item = "long_poll_timeout_round_trip"
    up = env.upstream("legacy")
    timeout = float(env.config.poll_timeout) + extra_seconds
    env.say("▶ 请用户先不要给 ClawBot 发消息，大约 2 到 3 分钟（我在量没有新消息时腾讯怎么回）。")
    kinds: list[str] = []
    attempts = 0
    while len([k for k in kinds if k in ("empty_response", "client_read_timeout")]) < idle_runs:
        attempts += 1
        if attempts > idle_runs + 3:
            raise ProbeError("一直量不到空闲时的结果（有新消息，或者请求一直失败）：看上面的记录")
        buf = env.cursor()
        outcome, shape, elapsed = _idle_poll(env, up, buf, timeout)
        kind = _classify(outcome)
        kinds.append(kind)
        if kind == "session_expired":
            raise _lost(env, item, "idle_poll")
        fields: dict[str, Any] = {
            "attempt": attempts,
            "kind": kind,
            "elapsed_seconds": round(elapsed, 1),
            "client_timeout_seconds": timeout,
            **shape_fields(shape),
        }
        if not isinstance(outcome, (PollFailed, SessionExpired)):
            fields["message_count"] = len(outcome.messages)
            fields["longpolling_timeout_ms"] = outcome.longpolling_timeout_ms
            fields["buf_changed"] = bool(outcome.buf) and outcome.buf != buf
            if outcome.buf:
                env.save_cursor(outcome.buf)
        elif isinstance(outcome, PollFailed):
            fields["reason"] = mask(outcome.reason, ())[:100]
        env.recorder.record(item, "idle_poll", **fields)
    return {"kinds": kinds}


def old_buf_redelivers(env: ProbeEnv, *, quick_poll_seconds: float = 15.0) -> dict[str, Any]:
    """第 8 项：收一批消息记下新进度，再拿旧的进度要一次，看是不是同一批。需要使用者发一句话；
    服务必须是停着的。结束时把最新进度和这条消息的对话凭证留给后面几项。

    后面两次要进度（旧的、新的）只等 `quick_poll_seconds` 秒：有数据腾讯会马上回，等不到就是没有；
    这样后面几项探针接着用这条消息的对话凭证时，凭证还新（它的有效期是第 6 项才量的）。"""
    item = "old_buf_redelivers"
    up = env.upstream("legacy")
    account = env.account()
    timeout = min(float(env.config.poll_timeout), quick_poll_seconds)
    received = wait_for_user_message(
        env, up, item=item, purpose="第 8 项：量重发", save_cursor=False
    )
    first = received.messages
    env.recorder.record(
        item,
        "first_batch",
        message_count=len(first),
        seqs=[m.seq for m in first],
        has_message_id=[m.message_id is not None for m in first],
        buf_was_empty=received.buf_before == "",
        buf_changed=received.buf_after != received.buf_before,
        waited_seconds=round(received.waited_seconds, 1),
    )
    # 立刻拿旧的进度再要一次
    again, shape, elapsed = _idle_poll(env, up, received.buf_before, timeout)
    if isinstance(again, SessionExpired):
        raise _lost(env, item, "old_buf_again")
    kind = _classify(again)
    mine_again = (
        tuple(m for m in again.messages if m.from_user_id == account.user_id)
        if not isinstance(again, (PollFailed, SessionExpired))
        else ()
    )
    same_ids = bool(first) and [m.message_id for m in mine_again] == [m.message_id for m in first]
    same_seqs = bool(first) and [m.seq for m in mine_again] == [m.seq for m in first]
    env.recorder.record(
        item,
        "old_buf_again",
        kind=kind,
        message_count=len(mine_again),
        seqs=[m.seq for m in mine_again],
        same_message_ids_as_first=same_ids,
        same_seqs_as_first=same_seqs,
        elapsed_seconds=round(elapsed, 1),
        client_timeout_seconds=timeout,
        **shape_fields(shape),
    )
    # 再拿新的进度要一次：应该是空的
    final_buf = received.buf_after
    third, shape3, elapsed3 = _idle_poll(env, up, received.buf_after, timeout)
    if isinstance(third, SessionExpired):
        raise _lost(env, item, "new_buf_again")
    env.recorder.record(
        item,
        "new_buf_again",
        kind=_classify(third),
        message_count=0 if isinstance(third, PollFailed) else len(third.messages),
        elapsed_seconds=round(elapsed3, 1),
        client_timeout_seconds=timeout,
        **shape_fields(shape3),
    )
    if not isinstance(third, PollFailed) and third.buf:
        final_buf = third.buf
    env.save_cursor(final_buf)
    latest = received.latest()
    env.save_context(
        Context(
            latest.from_user_id, latest.context_token or "", latest.at_ms or env.clock.wall_ms()
        )
    )
    return {"redelivered": same_ids, "first_count": len(first), "again_count": len(mine_again)}


def message_id_in_send_response(env: ProbeEnv) -> dict[str, Any]:
    """第 1 项：legacy 档发 3 条，记返回体的键名；再发 1 条不带对话凭证的作对比。占 4 条额度。"""
    item = "message_id_in_send_response"
    up = env.upstream("legacy")
    context = fresh_context(env, up, item=item, purpose="第 1 项：发 4 条探针消息")
    results = []
    for number in (1, 2, 3):
        results.append(
            send_probe(
                env,
                up,
                item=item,
                step=f"normal_{number}",
                to_user_id=context.to_user_id,
                token=context.token,
                text=f"E4 验证 {number}/3（测试消息，可忽略）",
                profile="legacy",
            )
        )
    control = send_probe(
        env,
        up,
        item=item,
        step="no_context_token",
        to_user_id=context.to_user_id,
        token="",
        text="E4 验证 对比（没有对话凭证，多半收不到，可忽略）",
        profile="legacy",
    )
    env.say("▶ 请用户看一下微信：收到了几条“E4 验证”消息，各是第几条（对比那条多半收不到）。")
    return {
        "normal_with_id": [r.message_id_found for r in results],
        "control_with_id": control.message_id_found,
    }


def legacy_vs_official_profiles(env: ProbeEnv) -> dict[str, Any]:
    """第 2 项：legacy 与 official 各发 2 条（先 legacy 作基线）。占 4 条额度。"""
    item = "legacy_vs_official_profiles"
    context = fresh_context(
        env, env.upstream("legacy"), item=item, purpose="第 2 项：两档各发 2 条"
    )
    out: dict[str, Any] = {}
    for profile in ("legacy", "official"):
        up = env.upstream(profile)
        for number in (1, 2):
            result = send_probe(
                env,
                up,
                item=item,
                step=f"{profile}_{number}",
                to_user_id=context.to_user_id,
                token=context.token,
                text=f"E4 对比 {profile} {number}/2（测试消息，可忽略）",
                profile=profile,
            )
            out.setdefault(profile, []).append(result.outcome)
    env.say("▶ 请用户看一下微信：收到了几条“E4 对比”消息（legacy 和 official 各应有 2 条）。")
    return out


def context_token_lifetime(env: ProbeEnv) -> dict[str, Any]:
    """第 6 项：收一条使用者的消息，拿它的对话凭证按间隔（默认 0、1、2、5、10、30 分钟）
    各回复一条，记第一次被拒绝的时间。占 6 条额度，要 30 多分钟；服务必须是停着的。"""
    item = "context_token_lifetime"
    up = env.upstream("legacy")
    received = wait_for_user_message(
        env, up, item=item, purpose="第 6 项：这一条的对话凭证要用 30 多分钟"
    )
    latest = received.latest()
    token = latest.context_token or ""
    to_user = latest.from_user_id
    # 凭证的年龄从那条消息发出的时候算起（消息可能在我们收到之前就等了一会儿）
    initial_age = max(0.0, (env.clock.wall_ms() - (latest.at_ms or env.clock.wall_ms())) / 1000)
    origin = env.clock.monotonic() - initial_age
    env.recorder.record(item, "start", initial_age_seconds=round(initial_age, 1))
    offsets = env.reply_minutes
    first_rejected: float | None = None
    outcomes: list[str] = []
    for number, minutes in enumerate(offsets, 1):
        due = origin + minutes * 60
        next_note = env.clock.monotonic()
        while env.clock.monotonic() < due:
            remaining = due - env.clock.monotonic()
            if env.clock.monotonic() >= next_note:
                env.say(
                    f"…等到第 {minutes:g} 分钟再回第 {number} 条（还要 {remaining / 60:.1f} 分钟）"
                )
                next_note = env.clock.monotonic() + 300
            env.clock.sleep(min(remaining, 30.0))
        result = send_probe(
            env,
            up,
            item=item,
            step=f"reply_{number}",
            to_user_id=to_user,
            token=token,
            text=f"E4 回复 {number}/{len(offsets)}（凭证约 {minutes:g} 分钟，可忽略）",
            profile="legacy",
        )
        env.recorder.record(
            item,
            f"age_{number}",
            age_minutes=round((env.clock.monotonic() - origin) / 60, 1),
            outcome=result.outcome,
        )
        outcomes.append(result.outcome)
        if result.outcome != "Accepted":
            first_rejected = minutes
            break
    env.recorder.record(item, "summary", first_rejected_minutes=first_rejected, outcomes=outcomes)
    env.say("▶ 请用户看一下微信：收到了哪几条“E4 回复”（第几条到第几条）。")
    return {"first_rejected_minutes": first_rejected, "outcomes": outcomes}


def qr_login_from_home_network(env: ProbeEnv) -> dict[str, Any]:
    """第 11 项（只取码、查一次状态，不扫）：在家里的网络上能不能取到码、查到状态。
    真的扫码绑定只在使用者决定正式绑定或重新绑定时做。"""
    item = "qr_login_from_home_network"
    up = env.upstream("legacy")
    base = env.config.base_url
    with watching() as shapes:
        try:
            code = up.qr_start()
            status = up.qr_status(code.code, None, None)
            ok, why, state = True, "", status.status
        except UpstreamError as exc:
            ok, why, state = False, str(exc)[:100], ""
    env.recorder.record(
        item,
        "qr_start_and_status",
        ok=ok,
        error=why,
        qr_status=state,
        requests=[shape_fields(shape) for shape in shapes],
        via_proxy=proxy_in_effect(base),
    )
    return {"ok": ok, "status": state}
