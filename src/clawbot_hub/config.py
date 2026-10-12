"""配置：读取、默认值、校验（错误一次列全）。

文件是 `<home>/config.yaml`，可以没有，全部项都有默认值；只在服务启动时读一次。
默认值和取值范围见设计第 13 节，规格见 `hub-deploy`「配置」「谁能连」。

配置是一棵冻结的数据类：`config.send.part_chars`。`parse_config` 校验一个已经读出来的字典，
`load_config` 先读文件。校验不通过抛 `ConfigError`，`errors` 里是全部错误，每条以
`<点号键>：` 开头。设计和规格没说的两处小约定：不认识的键算错误（拼错了不能悄悄不生效，
与 ashare-quant 的配置一致）；`upstream.base_url` 必须以 `http://` 或 `https://` 开头。
"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import yaml

from clawbot_hub.textutil import WORD_MAX_CHARS, is_word

# ---------------------------------------------------------------- 配置的结构和默认值


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8790
    lan: bool = False
    shutdown_seconds: int = 35

    @property
    def bind_host(self) -> str:
        """实际监听的地址：开了局域网监听就是 `0.0.0.0`，否则是 `host`。"""
        return "0.0.0.0" if self.lan else self.host


@dataclass(frozen=True)
class UpstreamConfig:
    base_url: str = "https://ilinkai.weixin.qq.com"
    profile: str = "legacy"
    confirm: str = "observe"
    poll_timeout: int = 45
    request_timeout: int = 15
    backoff_start: int = 5
    backoff_max: int = 60
    relogin_probe_seconds: int = 600


@dataclass(frozen=True)
class SendConfig:
    timeout_seconds: int = 30
    part_chars: int = 2000
    max_chars: int = 8000
    retries: int = 2
    rate_per_minute: int = 30
    dedupe_hours: int = 24
    message_id_keys: tuple[str, ...] = ("message_id", "msg_id")


@dataclass(frozen=True)
class QuotaConfig:
    window_hours: int = 24
    window_limit: int = 10
    reply_reserve: int = 2
    deferrable_keep: int = 3
    reject_hold_minutes: int = 30


@dataclass(frozen=True)
class InboxConfig:
    wait_seconds: int = 25
    max_wait: int = 55
    lease_seconds: int = 60
    max_attempts: int = 5
    max_queued: int = 50
    retention_minutes: int = 10
    online_seconds: int = 90
    offline_notice_seconds: int = 300


@dataclass(frozen=True)
class ReplyConfig:
    handle_minutes: int = 30
    max_per_handle: int = 5


@dataclass(frozen=True)
class RoutingConfig:
    confirm_words: tuple[str, ...] = ("对", "是", "确认")
    confirm_seconds: int = 600


@dataclass(frozen=True)
class HubConfig:
    label: str = "服务"


@dataclass(frozen=True)
class WindowWarnConfig:
    enabled: bool = True
    hours_before: int = 3
    between: str = "07:00-22:30"
    weekdays: tuple[int, ...] = (1, 2, 3, 4, 5)  # ISO 编号，周一 = 1


@dataclass(frozen=True)
class BlockedConfig:
    min_interval_hours: int = 12


@dataclass(frozen=True)
class AlertsConfig:
    group: str = "clawbot-hub"
    bark_timeout_seconds: int = 10
    bark_retries: int = 3
    queue_retry_minutes: int = 5
    queue_give_up_minutes: int = 60
    unconfirmed_streak: int = 3
    upstream_down_minutes: int = 10
    restart_merge_minutes: int = 10
    window_warn: WindowWarnConfig = field(default_factory=WindowWarnConfig)
    blocked: BlockedConfig = field(default_factory=BlockedConfig)


@dataclass(frozen=True)
class DisplayConfig:
    timezone: str = "Asia/Shanghai"

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class EventsConfig:
    keep_days: int = 90


@dataclass(frozen=True)
class BackupConfig:
    at: str = "04:00"
    keep: int = 14


@dataclass(frozen=True)
class DbConfig:
    busy_timeout_ms: int = 5000


@dataclass(frozen=True)
class LogConfig:
    level: str = "INFO"
    max_mb: int = 5
    keep: int = 5


@dataclass(frozen=True)
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    upstream: UpstreamConfig = field(default_factory=UpstreamConfig)
    send: SendConfig = field(default_factory=SendConfig)
    quota: QuotaConfig = field(default_factory=QuotaConfig)
    inbox: InboxConfig = field(default_factory=InboxConfig)
    reply: ReplyConfig = field(default_factory=ReplyConfig)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    hub: HubConfig = field(default_factory=HubConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    events: EventsConfig = field(default_factory=EventsConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    db: DbConfig = field(default_factory=DbConfig)
    log: LogConfig = field(default_factory=LogConfig)

    def flat(self) -> dict[str, Any]:
        """全部配置项：点号键 → 值，顺序同设计第 13 节（`hubctl config show` 用）。"""
        out: dict[str, Any] = {}

        def walk(node: Any, prefix: str) -> None:
            for f in fields(node):
                value = getattr(node, f.name)
                if is_dataclass(value):
                    walk(value, f"{prefix}{f.name}.")
                else:
                    out[f"{prefix}{f.name}"] = value

        walk(self, "")
        return out

    def warnings(self) -> list[str]:
        """不拒绝启动、但要记一条事件并让 `doctor` 报告的情形。"""
        found: list[str] = []
        if self.server.shutdown_seconds < self.send.timeout_seconds:
            found.append(
                f"server.shutdown_seconds（{self.server.shutdown_seconds}）比 "
                f"send.timeout_seconds（{self.send.timeout_seconds}）短："
                "关停时可能等不到最后一条发完，下次启动那条记录会是“中断”"
            )
        return found


class ConfigError(Exception):
    """配置不合格。`errors` 是全部错误，每条以 `<点号键>：` 开头。"""

    def __init__(self, errors: Iterable[str]):
        self.errors = tuple(errors)
        lines = "\n".join(f"  - {e}" for e in self.errors)
        super().__init__(f"配置有 {len(self.errors)} 处错误：\n{lines}")


# ---------------------------------------------------------------- 取值范围（设计第 13 节）

_MAX_INT = 1_000_000
# 表里没有的整数键：1 到 1000000
_DEFAULT_RANGE = (1, _MAX_INT)
_INT_RANGES: dict[str, tuple[int, int]] = {
    "server.port": (1, 65535),
    "server.shutdown_seconds": (1, 600),
    "upstream.poll_timeout": (5, 120),
    "upstream.request_timeout": (1, 120),
    "upstream.backoff_start": (1, 3600),
    "upstream.backoff_max": (1, 3600),
    "upstream.relogin_probe_seconds": (30, 86400),
    "send.timeout_seconds": (2, 600),
    "send.part_chars": (200, 4000),
    "send.max_chars": (200, 60000),
    "send.retries": (0, 5),
    "quota.window_hours": (2, 168),
    "quota.window_limit": (2, 100),
    "inbox.max_wait": (1, 600),
    "inbox.max_attempts": (1, 20),
    "routing.confirm_seconds": (1, 1800),
    "reply.handle_minutes": (1, 1440),
    "reply.max_per_handle": (1, 20),
    "alerts.bark_retries": (0, 10),
    "backup.keep": (1, 365),
    "db.busy_timeout_ms": (100, 60000),
    # 范围的上限跟着别的键走：这里只管下限，上限在 `_cross_checks` 里查
    "quota.reply_reserve": (0, _MAX_INT),
    "quota.deferrable_keep": (0, _MAX_INT),
    "inbox.wait_seconds": (0, _MAX_INT),
    "alerts.window_warn.hours_before": (1, _MAX_INT),
}
_RANGE_TEXT = {
    "quota.reply_reserve": "0 到 quota.window_limit − 1",
    "quota.deferrable_keep": "0 到 quota.window_limit",
    "inbox.wait_seconds": "0 到 inbox.max_wait",
    "alerts.window_warn.hours_before": "1 到 quota.window_hours − 1",
}

_ENUMS: dict[str, tuple[str, ...]] = {
    "upstream.profile": ("legacy", "official"),
    "upstream.confirm": ("strict", "observe", "off"),
}
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_CLOCK = r"(?:[01][0-9]|2[0-3]):[0-5][0-9]"
_CLOCK_RE = re.compile(_CLOCK)
_CLOCK_RANGE_RE = re.compile(f"{_CLOCK}-{_CLOCK}")


class _Problems:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.bad: set[str] = set()  # 已经报过错的键：依赖它们的跨键检查跳过，免得重复报

    def add(self, key: str, text: str) -> None:
        self.errors.append(f"{key}：{text}")
        self.bad.add(key)


def _join(path: str, name: str) -> str:
    return f"{path}.{name}" if path else name


# ---------------------------------------------------------------- 逐项检查


def _check_int(key: str, value: object, default: int, problems: _Problems) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        problems.add(key, f"必须是整数，收到 {value!r}")
        return default
    low, high = _INT_RANGES.get(key, _DEFAULT_RANGE)
    if not low <= value <= high:
        allowed = _RANGE_TEXT.get(key) or f"{low} 到 {high}"
        problems.add(key, f"{value} 不在允许的范围内（{allowed}）")
        return default
    return value


def _check_text(key: str, value: object, default: str, problems: _Problems) -> str:
    if not isinstance(value, str):
        problems.add(key, f"必须是字符串（数字、时间要加引号），收到 {value!r}")
        return default
    if key in _ENUMS:
        if value not in _ENUMS[key]:
            problems.add(key, f"{value!r} 不认识，可选：{'、'.join(_ENUMS[key])}")
            return default
        return value
    problem = _TEXT_RULES.get(key, _not_blank)(value)
    if problem:
        problems.add(key, problem)
        return default
    return value.upper() if key == "log.level" else value


def _not_blank(value: str) -> str | None:
    return None if value.strip() else "不能是空的"


def _timezone_problem(value: str) -> str | None:
    try:
        ZoneInfo(value)
    except (KeyError, ValueError, OSError):
        return f"{value!r} 不是认识的时区，例如 Asia/Shanghai"
    return None


def _base_url_problem(value: str) -> str | None:
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError:
        host = None
        parts = None
    if parts is None or parts.scheme not in ("http", "https") or not host:
        return f"{value!r} 不是地址，要以 http:// 或 https:// 开头"
    return None


def _clock_problem(value: str) -> str | None:
    if _CLOCK_RE.fullmatch(value):
        return None
    return f'{value!r} 不是 HH:MM 格式的时间，例如 "04:00"'


def _clock_range_problem(value: str) -> str | None:
    if _CLOCK_RANGE_RE.fullmatch(value):
        return None
    return f'{value!r} 不是 HH:MM-HH:MM 格式的时间段，例如 "07:00-22:30"'


def _label_problem(value: str) -> str | None:
    if is_word(value):
        return None
    return f"{value!r} 不合格：要 1 到 {WORD_MAX_CHARS} 个字符，只含字母、数字、汉字"


def _log_level_problem(value: str) -> str | None:
    if value.upper() in _LOG_LEVELS:
        return None
    return f"{value!r} 不认识，可选：{'、'.join(_LOG_LEVELS)}"


_TEXT_RULES = {
    "display.timezone": _timezone_problem,
    "upstream.base_url": _base_url_problem,
    "backup.at": _clock_problem,
    "alerts.window_warn.between": _clock_range_problem,
    "hub.label": _label_problem,
    "log.level": _log_level_problem,
}


def _check_list(key: str, value: object, default: tuple, problems: _Problems) -> tuple:
    if not isinstance(value, (list, tuple)):
        problems.add(key, f"必须是列表，收到 {value!r}")
        return default
    items = tuple(value)
    if key == "routing.confirm_words":
        if not items:
            problems.add(key, "不能是空的，至少要有一个确认词")
            return default
        bad = [item for item in items if not isinstance(item, str) or not is_word(item)]
        if bad:
            shown = "、".join(repr(item) for item in bad)
            problems.add(
                key, f"{shown} 不合格：每个词要 1 到 {WORD_MAX_CHARS} 个字符，只含字母、数字、汉字"
            )
            return default
    elif key == "send.message_id_keys":
        bad = [item for item in items if not isinstance(item, str) or not item.strip()]
        if bad:
            problems.add(
                key, f"{'、'.join(repr(item) for item in bad)} 不合格：每项要是非空的字符串"
            )
            return default
    elif key == "alerts.window_warn.weekdays":
        bad = [
            item
            for item in items
            if isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= 7
        ]
        if bad:
            shown = "、".join(repr(item) for item in bad)
            problems.add(key, f"{shown} 不合格：每项是 1 到 7 的整数（周一是 1，周日是 7）")
            return default
    return items


def _check(key: str, default: Any, value: object, problems: _Problems) -> Any:
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        problems.add(key, f"必须是 true 或 false，收到 {value!r}")
        return default
    if isinstance(default, int):
        return _check_int(key, value, default, problems)
    if isinstance(default, str):
        return _check_text(key, value, default, problems)
    if isinstance(default, tuple):
        return _check_list(key, value, default, problems)
    raise TypeError(f"{key} 的默认值类型 {type(default).__name__} 没有对应的检查")


def _build(cls: type, raw: object, path: str, problems: _Problems) -> Any:
    """按 `cls` 的字段读 `raw`（一组键值，或者没写），返回 `cls` 的实例；错误记进 `problems`。"""
    template = cls()
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        problems.add(path, f"应该是一组键值，收到 {raw!r}")
        return template
    known = [f.name for f in fields(cls)]
    for name in raw:
        if name not in known:
            problems.add(_join(path, str(name)), f"不认识的配置项，可选：{'、'.join(known)}")
    values: dict[str, Any] = {}
    for f in fields(cls):
        key = _join(path, f.name)
        default = getattr(template, f.name)
        if f.name not in raw:
            values[f.name] = default
        elif is_dataclass(default):
            values[f.name] = _build(type(default), raw[f.name], key, problems)
        else:
            values[f.name] = _check(key, default, raw[f.name], problems)
    return cls(**values)


# ---------------------------------------------------------------- 跨键的约束


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _cross_checks(config: Config, problems: _Problems) -> None:
    """几项的范围跟着别的键走。任何一边已经报过错，这一条就跳过。"""

    def usable(*keys: str) -> bool:
        return not any(key in problems.bad for key in keys)

    quota = config.quota
    if usable("quota.window_limit"):
        if usable("quota.reply_reserve") and quota.reply_reserve > quota.window_limit - 1:
            problems.add(
                "quota.reply_reserve",
                f"{quota.reply_reserve} 太大：窗口总条数 quota.window_limit 是 "
                f"{quota.window_limit}，回复保留最多 {quota.window_limit - 1}（要比它小 1）",
            )
        if usable("quota.deferrable_keep") and quota.deferrable_keep > quota.window_limit:
            problems.add(
                "quota.deferrable_keep",
                f"{quota.deferrable_keep} 太大：不能超过窗口总条数 quota.window_limit"
                f"（{quota.window_limit}）",
            )

    send = config.send
    if usable("send.part_chars", "send.max_chars") and send.part_chars > send.max_chars:
        problems.add(
            "send.part_chars",
            f"{send.part_chars} 太大：不能超过整条消息的上限 send.max_chars（{send.max_chars}）",
        )

    upstream = config.upstream
    if (
        usable("upstream.backoff_start", "upstream.backoff_max")
        and upstream.backoff_max < upstream.backoff_start
    ):
        problems.add(
            "upstream.backoff_max",
            f"{upstream.backoff_max} 太小：不能小于 upstream.backoff_start"
            f"（{upstream.backoff_start}）",
        )

    inbox = config.inbox
    if usable("inbox.wait_seconds", "inbox.max_wait") and inbox.wait_seconds > inbox.max_wait:
        problems.add(
            "inbox.wait_seconds",
            f"{inbox.wait_seconds} 太大：不能超过 inbox.max_wait（{inbox.max_wait}）",
        )

    warn = config.alerts.window_warn
    if (
        usable("alerts.window_warn.hours_before", "quota.window_hours")
        and warn.hours_before > quota.window_hours - 1
    ):
        problems.add(
            "alerts.window_warn.hours_before",
            f"{warn.hours_before} 太大：最多 {quota.window_hours - 1}"
            f"（窗口时长 quota.window_hours 是 {quota.window_hours}，要比它小 1）",
        )

    server = config.server
    if usable("server.host", "server.lan"):
        if server.lan:
            default_host = ServerConfig().host
            if server.host != default_host:
                problems.add(
                    "server.host",
                    f"server.lan 是 true 时服务监听 0.0.0.0，server.host 要保持默认值 "
                    f"{default_host}，收到 {server.host!r}",
                )
        elif not _is_loopback(server.host):
            problems.add(
                "server.host",
                f"server.lan 是 false 时只能写本机的回环地址（127.0.0.0/8 或 ::1），收到 "
                f"{server.host!r}；要让局域网里的设备连进来，把 server.lan 改成 true",
            )


# ---------------------------------------------------------------- 入口


def parse_config(data: object) -> Config:
    """校验一个读出来的配置字典（没写的项用默认值）。有错误就抛 `ConfigError`，一次列全。"""
    if data is not None and not isinstance(data, Mapping):
        raise ConfigError(["配置文件的顶层应该是一组键值（server:、send: 这样的段）"])
    problems = _Problems()
    config = _build(Config, data, "", problems)
    _cross_checks(config, problems)
    if problems.errors:
        raise ConfigError(problems.errors)
    return config


def load_config(path: str | os.PathLike[str] | None) -> Config:
    """读配置文件；没有这个文件（或者没给路径）就是全部默认值。"""
    if path is None:
        return Config()
    file = Path(path)
    if not file.exists():
        return Config()
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError([f"读不了配置文件 {file}：{type(exc).__name__}"]) from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError([_yaml_problem(file, exc)]) from None
    return parse_config(data)


def _yaml_problem(file: Path, exc: yaml.YAMLError) -> str:
    mark = getattr(exc, "problem_mark", None)
    where = f"第 {mark.line + 1} 行第 {mark.column + 1} 列" if mark is not None else "位置不明"
    problem = getattr(exc, "problem", None) or "格式不对"
    return f"配置文件 {file} 不是合法的 YAML（{where}）：{problem}"
