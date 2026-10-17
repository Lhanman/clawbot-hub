"""日志：写到 `<home>/logs/hub.log`（按大小轮转），同时输出到标准错误（launchd 会收走）。

规格 `hub-deploy`「日志」：日志里不能有登录凭证、项目密钥、Bark 设备码、消息正文。代码本身就不
往日志里写这些；这里是第二道防线——日志层统一过滤，已知的秘密值和 `chub_` 开头的项目密钥格式
一律换成 `***`，即使哪一处调用不小心把它们写进了日志消息或错误信息（含 `logger.exception` 的
堆栈）也一样。

**已知的秘密值**（`SecretBook`）：`account.json` 里的 `bot_token`，Bark 设备码（环境变量或
`bark.json`）。每写一行日志问一次；文件的内容按（修改时间、大小）缓存，没变就不重读，所以服务
运行中重新绑定了账号、`hubctl alerts set-key` 换了设备码，新的值从下一行日志起就被遮住。看到过
的值一直记着：文件后来换了、删了，旧值也还要遮（它可能还留在某个正在往下传的错误信息里）。
文件读不出来（坏了、没有）不影响写日志。对话凭证不在这里：它只在数据库里，代码从不拿它写日志。

**级别**：`log.level` 只管 `clawbot_hub` 自己的日志；别的库（urllib3、uvicorn……）一律只放行警告
以上——它们的调试输出会带上请求的地址和细节，DEBUG 级别也不能记正文和凭证。

**权限**：`logs/` 目录 0700，日志文件和轮转出来的旧份 0600，文件创建时就是这个权限（不看 umask）。

**时间**按 `display.timezone` 写成带时区偏移的 ISO 8601（精确到秒）。

调用者（`hubctl serve`）在启动的第 2 步调 `setup_logging`，退出时 `close()`；测试里同样成对用。
再调一次会先换掉上一次装的处理器，不会一行写两遍。
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from pathlib import Path
from typing import IO, Any

from clawbot_hub.bark import lookup_bark_key, parse_bark_key
from clawbot_hub.config import LogConfig
from clawbot_hub.paths import DIR_MODE, FILE_MODE, HubPaths
from clawbot_hub.textutil import mask

# 我们自己的日志都挂在这个名字下面（`clawbot_hub.poller`、`clawbot_hub.api`……）
OUR_LOGGER = "clawbot_hub"
# 别的库的日志只放行这个级别以上
OTHERS_LEVEL = logging.WARNING
# 打在我们装的处理器上的记号，再次安装时据此认出上一次的
_MARK = "_clawbot_hub_handler"

_LINE = "%(asctime)s %(levelname)s %(name)s %(message)s"
_MEGABYTE = 1024 * 1024

Signature = tuple[int, int]


class SecretBook:
    """已知的秘密值，见模块说明。`environ` 是读 Bark 设备码用的环境变量（默认取进程的，启动时读
    一次，和告警一样，所以服务运行中改环境变量不生效）。"""

    def __init__(self, paths: HubPaths, environ: Mapping[str, str] | None = None) -> None:
        self._paths = paths
        self._known: dict[str, None] = {}  # 有序的集合：先看到的在前
        self._signatures: dict[Path, Signature | None] = {}
        self._lock = threading.Lock()
        found = lookup_bark_key(paths, environ)  # 不给就用进程的环境变量
        if found.source == "env" and found.key is not None:
            self._known[found.key.device] = None
        self.values()  # 启动时就读一遍：文件在第一行日志之前被删掉，里面的值也已经记住了

    def values(self) -> tuple[str, ...]:
        with self._lock:
            self._refresh(self._paths.account, _token_in)
            self._refresh(self._paths.bark, _bark_code_in)
            return tuple(self._known)

    def _refresh(self, path: Path, read: Callable[[bytes], str | None]) -> None:
        try:
            info = path.stat()
            signature: Signature | None = (info.st_mtime_ns, info.st_size)
        except OSError:
            signature = None
        if signature is None or self._signatures.get(path) == signature:
            self._signatures[path] = signature
            return
        self._signatures[path] = signature
        try:
            value = read(path.read_bytes())
        except OSError:
            return
        if value:
            self._known[value] = None


def _token_in(raw: bytes) -> str | None:
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        return None
    token = data.get("bot_token") if isinstance(data, dict) else None
    return token if isinstance(token, str) else None


def _bark_code_in(raw: bytes) -> str | None:
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        return None
    text = data.get("key") if isinstance(data, dict) else None
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return parse_bark_key(text).device
    except ValueError:
        return None


class _Formatter(logging.Formatter):
    def __init__(self, tz: tzinfo, secrets: SecretBook) -> None:
        super().__init__(_LINE)
        self._tz = tz
        self._secrets = secrets

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return datetime.fromtimestamp(record.created, self._tz).isoformat(timespec="seconds")

    def format(self, record: logging.LogRecord) -> str:
        return mask(super().format(record), self._secrets.values())


class _PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """文件创建的那一刻就是 0600：不先按 umask 建出来、再改权限。轮转出新的 `hub.log` 时同样。"""

    def _open(self) -> IO[Any]:
        return open(  # noqa: SIM115 - 流由处理器管着，在 close() 里关
            self.baseFilename,
            self.mode,
            encoding=self.encoding,
            errors=self.errors,
            opener=lambda path, flags: os.open(path, flags, FILE_MODE),
        )


@dataclass
class LogSetup:
    """装好的日志。`close()` 把处理器和级别还原（重复关没关系）。"""

    file_handler: logging.Handler
    stream_handler: logging.Handler
    _root_level: int
    _our_level: int
    _closed: bool = field(default=False, repr=False)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        root = logging.getLogger()
        for handler in (self.file_handler, self.stream_handler):
            root.removeHandler(handler)  # 已经不在了也没关系
            handler.close()
        root.setLevel(self._root_level)
        logging.getLogger(OUR_LOGGER).setLevel(self._our_level)


def _remove_previous(root: logging.Logger) -> None:
    for handler in list(root.handlers):
        if getattr(handler, _MARK, False):
            root.removeHandler(handler)
            handler.close()


def setup_logging(
    paths: HubPaths,
    log: LogConfig,
    *,
    tz: tzinfo,
    environ: Mapping[str, str] | None = None,
    stream: IO[str] | None = None,
) -> LogSetup:
    """建 `logs/`（0700）、装文件和标准错误两个处理器。`stream` 不给就是 `sys.stderr`。"""
    paths.logs.mkdir(mode=DIR_MODE, exist_ok=True)
    root = logging.getLogger()
    ours = logging.getLogger(OUR_LOGGER)
    _remove_previous(root)
    previous = (root.level, ours.level)

    formatter = _Formatter(tz, SecretBook(paths, environ))
    file_handler = _PrivateRotatingFileHandler(
        paths.log_file,
        maxBytes=log.max_mb * _MEGABYTE,
        backupCount=log.keep,
        encoding="utf-8",
    )
    stream_handler = logging.StreamHandler(stream)  # 不给就是标准错误
    for handler in (file_handler, stream_handler):
        handler.setFormatter(formatter)
        setattr(handler, _MARK, True)
        root.addHandler(handler)

    root.setLevel(OTHERS_LEVEL)
    ours.setLevel(logging.getLevelNamesMapping()[log.level.upper()])
    return LogSetup(file_handler, stream_handler, *previous)
