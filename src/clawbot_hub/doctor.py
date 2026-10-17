"""`hubctl doctor`：逐项检查，每项一行 ✓ 通过 / ⚠ 警告 / ✗ 错误，最后一行汇总。

**只看不改**：不建库、不升级库（数据库是只读着开的，连接关闭时也不会合并日志）、不连腾讯
（`--online` 才连，而且只是不带凭证地访问一次根地址，不调收消息、发消息的接口，免得和运行中的服务
抢消息）、输出里没有登录凭证、设备码、项目密钥。唯一会动手的是 `--fix`：把放宽了的权限改回
0700 / 0600，别的毛病只提示。规格 `hub-cli`「查毛病」。

**这里定下的约定**（规格没写，任务单 D3 的实现说明里也有）：

- 每项一行，名字和先后固定（`NAMES`，开了 `--online` 末尾多一行"腾讯连通"）。
- 一个毛病只报一次：数据库不存在、打不开或者比程序旧时，依赖数据库的几项（登录状态、保底合计、
  前缀冲突、Bark 结果）不列；配置读不出来时，依赖配置的几项（端口、保底合计、前缀冲突、配置警告）
  不列；凭证文件没有或读不出来时不列登录状态。其余检查照常做。
- 数据库没有（服务从没启动过）算通过，"自动备份"也算通过——没有库就没什么可备份；数据库比程序旧
  也算通过，提示服务下次启动时自动升级。
- `--online` 去的是服务发消息用的那个地址：凭证文件里有 `base_url` 就用它，没有再用配置的
  `upstream.base_url`；等多久跟着 `upstream.request_timeout`。连上了就算通过，不看对方回的是什么
  状态码；连不上、TLS 握手失败、连上了但等不到回话都是警告。输出里的地址只写主机和端口（地址里
  的用户名口令、路径不写出来）；地址看不懂（不是 http:// 或 https:// 开头）也是警告，不发请求。
- 磁盘 100 MB、备份 2 天这两个阈值是运维提示，不是行为，写死在这里（设计第 13 节的固定值表）。
"""

from __future__ import annotations

import errno
import shutil
import socket
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import requests

from clawbot_hub.alerts import OK as BARK_OK
from clawbot_hub.alerts import REFUSED as BARK_REFUSED
from clawbot_hub.alerts import last_bark_outcome
from clawbot_hub.backup import latest_auto_backup
from clawbot_hub.bark import lookup_bark_key
from clawbot_hub.clock import Clock
from clawbot_hub.config import Config, ConfigError, load_config
from clawbot_hub.db import (
    Database,
    DatabaseProblem,
    DatabaseTooNew,
    check_integrity,
    is_busy,
    latest_version,
    read_version,
)
from clawbot_hub.instance import pid_alive, service_running
from clawbot_hub.loginstate import (
    SCAN_MARKER_MINUTES,
    read_login,
    read_scan_marker,
    scan_in_progress,
)
from clawbot_hub.paths import HubPaths, WidePermission, fix_permissions, wide_permissions
from clawbot_hub.projects import Projects, guarantee_total
from clawbot_hub.status import alert_key_hint, read_account
from clawbot_hub.timefmt import moment_text, span_text

OK = "ok"
WARN = "warn"
ERROR = "error"
_ICONS = {OK: "✓", WARN: "⚠", ERROR: "✗"}

HOME = "数据目录"
PERMISSIONS = "权限"
CREDENTIALS = "登录凭证"
LOGIN = "登录状态"
DATABASE = "数据库"
SERVICE = "服务"
PORT = "端口"
CONFIG = "配置"
GUARANTEES = "保底合计"
PREFIXES = "前缀冲突"
ALERT_KEY = "告警设备码"
BARK_RESULT = "Bark 结果"
CONFIG_WARNINGS = "配置警告"
SCAN_MARKER = "扫码标记"
DISK = "磁盘空间"
BACKUP = "自动备份"
ONLINE = "腾讯连通"
NAMES = (
    HOME,
    PERMISSIONS,
    CREDENTIALS,
    LOGIN,
    DATABASE,
    SERVICE,
    PORT,
    CONFIG,
    GUARANTEES,
    PREFIXES,
    ALERT_KEY,
    BARK_RESULT,
    CONFIG_WARNINGS,
    SCAN_MARKER,
    DISK,
    BACKUP,
)

DISK_WARN_BYTES = 100 * 1024 * 1024  # 剩余空间不到 100 MB 警告
BACKUP_WARN_SECONDS = 2 * 24 * 3600  # 最近一次自动备份距今超过 2 天警告
PERMISSIONS_SHOWN = 5  # 权限一行里最多列几项，其余写个数


@dataclass(frozen=True)
class Finding:
    """一项检查的结论。"""

    level: str  # ok / warn / error
    name: str
    detail: str

    def line(self) -> str:
        return f"{_ICONS[self.level]} {self.name}：{self.detail}"


def summary_line(findings: list[Finding]) -> str:
    passed = sum(1 for found in findings if found.level == OK)
    warnings = sum(1 for found in findings if found.level == WARN)
    errors = sum(1 for found in findings if found.level == ERROR)
    return f"汇总：{passed} 项通过，{warnings} 个警告，{errors} 个错误"


def exit_code(findings: list[Finding]) -> int:
    """有 ✗ 是 1，只有 ⚠ 或全部通过是 0。"""
    return 1 if any(found.level == ERROR for found in findings) else 0


def _one_line(text: str) -> str:
    return " ".join(text.split())


def home_unusable(text: str) -> list[Finding]:
    """数据目录没法用（位置上是个文件、建不出来）：别的检查都没法做，只剩这一项。"""
    return [Finding(ERROR, HOME, text)]


# ---------------------------------------------------------------- 数据目录和权限


def _home(paths: HubPaths, existed: bool) -> Finding:
    if not existed:
        return Finding(
            WARN,
            HOME,
            f"{paths.home} 是这次运行刚建的：如果这不是你要看的那个目录，多半是 --home "
            "或环境变量 CLAWBOT_HUB_HOME 指错了；确认没指错就不用管",
        )
    return Finding(OK, HOME, str(paths.home))


def _shown(item: WidePermission, paths: HubPaths) -> str:
    if item.path == paths.home:
        return "数据目录本身"
    return str(item.path.relative_to(paths.home))  # `wide_permissions` 只会给数据目录里的东西


def _modes(items: list[WidePermission], paths: HubPaths, *, fixed: bool) -> str:
    parts = []
    for item in items[:PERMISSIONS_SHOWN]:
        name = _shown(item, paths)
        if fixed:
            parts.append(f"{name} {item.mode:04o} → {item.wanted:04o}")
        else:
            verb = "是" if item.path == paths.home else " 是"
            parts.append(f"{name}{verb} {item.mode:04o}（应该是 {item.wanted:04o}）")
    text = "；".join(parts)
    more = len(items) - PERMISSIONS_SHOWN
    return f"{text}（另有 {more} 项）" if more > 0 else text


def _permissions(paths: HubPaths, fix: bool) -> Finding:
    wide = wide_permissions(paths)
    if not wide:
        return Finding(OK, PERMISSIONS, "数据目录 0700，里面的文件 0600，没有放宽的")
    if fix:
        try:
            fix_permissions(paths)
        except OSError as exc:
            return Finding(
                ERROR, PERMISSIONS, f"--fix 没改成：{exc.filename or '某个文件'}（{exc.strerror}）"
            )
        return Finding(OK, PERMISSIONS, "已改回：" + _modes(wide, paths, fixed=True))
    return Finding(
        ERROR,
        PERMISSIONS,
        _modes(wide, paths, fixed=False)
        + "。改回去：文件 chmod 600、目录 chmod 700，或者加 --fix 一次改好",
    )


# ---------------------------------------------------------------- 登录


def _credentials(account_missing: bool, broken: bool) -> Finding:
    if broken:
        return Finding(ERROR, CREDENTIALS, "account.json 读不出来：重新运行 hubctl login")
    if account_missing:
        return Finding(
            WARN,
            CREDENTIALS,
            "还没绑定：运行 hubctl login 扫码绑定，或者用 hubctl import-credentials 接过 "
            "ashare-quant 的凭证",
        )
    return Finding(OK, CREDENTIALS, "已绑定，凭证文件读得出来")


def _login(db: Database, digest: str, tz) -> Finding:
    row = read_login(db.conn())
    if row.state == "expired" and row.digest == digest:
        since = "" if row.since_ms is None else f"（从 {moment_text(row.since_ms, tz)} 起）"
        return Finding(
            WARN, LOGIN, f"已失效{since}：在电脑上运行 hubctl login 重新扫码，服务会自动换上新凭证"
        )
    return Finding(OK, LOGIN, "正常，服务没有发现登录失效")


# ---------------------------------------------------------------- 数据库


def _open_database(paths: HubPaths, busy_timeout_ms: int) -> tuple[Finding, Database | None]:
    """数据库这一项的结论，和能用来查依赖数据库的几项的只读连接（不能查就是 `None`）。"""
    if not paths.db.exists():
        return Finding(OK, DATABASE, "还没有（服务第一次启动或登记第一个项目时建）"), None
    db = Database(paths.db, busy_timeout_ms=busy_timeout_ms, read_only=True)
    try:
        conn = db.conn()
        check_integrity(conn)
        version = read_version(conn)
    except DatabaseProblem as exc:
        db.close()
        return Finding(ERROR, DATABASE, _one_line(str(exc))), None
    except sqlite3.Error as exc:
        db.close()
        if is_busy(exc):
            return (
                Finding(WARN, DATABASE, "正忙（别的程序占着它），这次没检查成：稍后再运行一次"),
                None,
            )
        return Finding(ERROR, DATABASE, f"打不开：{_one_line(str(exc))}"), None
    latest = latest_version()
    if version > latest:
        db.close()
        return Finding(ERROR, DATABASE, _one_line(str(DatabaseTooNew(version, latest)))), None
    if version == 0:
        db.close()
        return Finding(OK, DATABASE, "是个空文件（还没建表）：服务第一次启动时建好"), None
    if version < latest:
        db.close()
        return (
            Finding(
                OK,
                DATABASE,
                f"第 {version} 版，程序要的是第 {latest} 版：服务下次启动时自动升级"
                "（升级前先备份）",
            ),
            None,
        )
    return Finding(OK, DATABASE, f"第 {version} 版，能打开，完整性检查通过"), db


# ---------------------------------------------------------------- 服务和端口


def _service(paths: HubPaths, clock: Clock) -> tuple[Finding, bool]:
    info = service_running(paths, clock)
    if info is None:
        return (
            Finding(WARN, SERVICE, "没有在运行（如果是你有意停的，不用管）"),
            False,
        )
    uptime = ""
    if info.started_ms:
        uptime = f"，已运行 {span_text((clock.now_ms() - info.started_ms) / 1000)}"
    return Finding(OK, SERVICE, f"在运行（进程号 {info.pid}{uptime}）"), True


def _can_bind(host: str, port: int) -> None:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # 服务自己也是这样绑的
        probe.bind((host, port))


def _port(config: Config, running: bool) -> Finding:
    host, port = config.server.bind_host, config.server.port
    where = f"{host}:{port}"
    if running:
        return Finding(OK, PORT, f"{where}（服务在运行，端口是它自己占着的）")
    try:
        _can_bind(host, port)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            return Finding(
                ERROR,
                PORT,
                f"{where} 被别的程序占着：换一个端口（配置 server.port），或者先停掉占着它的程序",
            )
        return Finding(ERROR, PORT, f"{where} 绑不上：{exc.strerror or type(exc).__name__}")
    return Finding(OK, PORT, f"{where} 空着")


# ---------------------------------------------------------------- 配置


def _load_config(paths: HubPaths, config_path: Path | None) -> tuple[Config | None, Finding]:
    if config_path is not None and not config_path.is_file():
        return None, Finding(ERROR, CONFIG, f"配置文件不存在或不是文件：{config_path}")
    file = config_path or paths.config
    try:
        config = load_config(file)
    except ConfigError as exc:
        problems = "；".join(_one_line(error) for error in exc.errors)
        return None, Finding(ERROR, CONFIG, f"{file.name} 有 {len(exc.errors)} 处错误：{problems}")
    if not file.exists():
        return config, Finding(OK, CONFIG, f"没有 {file.name}，用的是内置默认值")
    return config, Finding(OK, CONFIG, f"{file.name} 读得出来，值都在范围内")


def _guarantees(db: Database, config: Config, clock: Clock) -> Finding:
    projects = Projects(db, config, clock).list()
    total = guarantee_total(projects, config)
    if total.excess:
        return Finding(
            ERROR,
            GUARANTEES,
            f"保底合计 {total.total} 加上回复保留 {total.reserve} 超过窗口总条数 {total.limit}，"
            f"超了 {total.excess} 条；各项目的保底：{total.listing}。服务照常运行，只是登记和"
            "启用项目会被拒绝：用 hubctl project set 调小保底，或把 quota.window_limit 改回去",
        )
    return Finding(
        OK,
        GUARANTEES,
        f"保底合计 {total.total}，加回复保留 {total.reserve}，不超过窗口总条数 {total.limit}",
    )


def _prefixes(db: Database, config: Config, clock: Clock) -> Finding:
    conflicts = Projects(db, config, clock).prefix_conflicts()
    if not conflicts:
        return Finding(OK, PREFIXES, "没有项目的前缀和 hub.label、routing.confirm_words 里的词相同")
    parts = [
        f"项目 {item.project} 的前缀「{item.prefix}」和 {item.setting} 里的「{item.word}」相同"
        for item in conflicts
    ]
    return Finding(
        ERROR,
        PREFIXES,
        "；".join(parts) + "：用 hubctl project set 换一个前缀，或者改配置里的那个词",
    )


# ---------------------------------------------------------------- 告警


def _alert_key(paths: HubPaths, environ: Mapping[str, str] | None) -> Finding:
    hint = alert_key_hint(paths, environ)
    if hint is not None:
        return Finding(WARN, ALERT_KEY, hint)
    lookup = lookup_bark_key(paths, environ)
    place = "环境变量" if lookup.source == "env" else "bark.json"
    return Finding(OK, ALERT_KEY, f"已配置（{place}）")


def _bark_result(db: Database) -> Finding:
    outcome = last_bark_outcome(db.conn())
    if outcome == BARK_REFUSED:
        return Finding(
            WARN,
            BARK_RESULT,
            "最近一次发 Bark 被拒绝了，之后没有成功过：设备码多半填错了。运行 hubctl alerts "
            "set-key 换对，再用 hubctl alerts test 确认",
        )
    if outcome == BARK_OK:
        return Finding(OK, BARK_RESULT, "最近一次发 Bark 成功了")
    return Finding(OK, BARK_RESULT, "还没发过告警，没有可说的")


def _config_warnings(config: Config) -> Finding:
    found = config.warnings()
    if found:
        return Finding(WARN, CONFIG_WARNINGS, "；".join(found))
    return Finding(OK, CONFIG_WARNINGS, "没有")


# ---------------------------------------------------------------- 扫码标记、磁盘、备份


def _scan_marker(paths: HubPaths, clock: Clock) -> Finding:
    if not paths.login_pending.exists():
        return Finding(OK, SCAN_MARKER, "没有残留的 login.pending")
    marker = read_scan_marker(paths)
    elapsed_ms = None if marker is None else clock.now_ms() - marker.started_ms
    if marker is not None and scan_in_progress(paths, clock):
        assert elapsed_ms is not None
        return Finding(
            OK, SCAN_MARKER, f"正在扫码（进程 {marker.pid}，开始 {span_text(elapsed_ms / 1000)}了）"
        )
    if marker is None:
        why = "内容读不出来"
    elif not pid_alive(marker.pid):
        why = "里面的进程已经不在了"
    elif elapsed_ms is not None and elapsed_ms < 0:
        why = "开始时间在将来"
    else:
        why = f"开始已经超过 {SCAN_MARKER_MINUTES} 分钟"
    return Finding(
        WARN, SCAN_MARKER, f"login.pending 是残留的（{why}）：不影响服务，删掉这个文件就行"
    )


def _size_text(size: int) -> str:
    megabytes = size // (1024 * 1024)
    if megabytes < 1024:
        return f"{megabytes} MB"
    return f"{size / 1024**3:.1f} GB"


def _disk(paths: HubPaths) -> Finding:
    try:
        free = shutil.disk_usage(paths.home).free
    except OSError as exc:
        return Finding(WARN, DISK, f"查不出剩余空间（{exc.strerror}）")
    if free < DISK_WARN_BYTES:
        return Finding(
            WARN,
            DISK,
            f"只剩 {_size_text(free)}，不到 100 MB：先腾出空间，不然写库和备份可能失败",
        )
    return Finding(OK, DISK, f"还剩 {_size_text(free)}")


def _backup(paths: HubPaths, config: Config | None, tz, clock: Clock) -> Finding:
    if not paths.db.exists():
        return Finding(OK, BACKUP, "还没有数据库，没有什么可备份的")
    at = "" if config is None else f" {config.backup.at}"
    latest = latest_auto_backup(paths.backups, tz)
    if latest is None:
        return Finding(
            WARN,
            BACKUP,
            f"一份也没做过：服务每天{at} 自动做一次；服务没跑过，或者一直失败（hubctl events "
            "--kind backup 看原因）。可以先手动运行 hubctl backup",
        )
    _, when = latest
    age_seconds = clock.now_ms() // 1000 - int(when.timestamp())
    moment = moment_text(int(when.timestamp()) * 1000, tz)
    if age_seconds > BACKUP_WARN_SECONDS:
        return Finding(
            WARN,
            BACKUP,
            f"最近一次是 {moment}，已经 {span_text(age_seconds)}前了：服务有没有在运行？"
            "hubctl events --kind backup 能看到有没有失败",
        )
    return Finding(OK, BACKUP, f"最近一次是 {moment}（{span_text(age_seconds)}前）")


# ---------------------------------------------------------------- 在线


def _no_auth(request: requests.PreparedRequest) -> requests.PreparedRequest:
    """占住 `auth` 参数：`requests` 看到有 `auth` 就不读 `~/.netrc`（它会补上 `Authorization`）。"""
    return request


def _where(base_url: str) -> str | None:
    """输出里写的地址：只有主机和端口，不带用户名口令、路径。看不懂的地址是 `None`。"""
    try:
        parts = urlsplit(base_url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    host = f"[{host}]" if ":" in host else host
    return host if port is None else f"{host}:{port}"


def _online(base_url: str, timeout: float) -> Finding:
    where = _where(base_url)
    if where is None:
        return Finding(
            WARN,
            ONLINE,
            "没法试：地址看不懂（要以 http:// 或 https:// 开头）。看看凭证文件里的 base_url "
            "和配置里的 upstream.base_url",
        )
    try:
        with requests.Session() as session:
            response = session.get(
                base_url.rstrip("/") + "/",
                timeout=(timeout, timeout),
                allow_redirects=False,
                auth=_no_auth,
            )
    except requests.exceptions.SSLError:
        reason = "TLS 握手失败（证书不对，或者这台机器的系统时间不准）"
    except requests.exceptions.ConnectTimeout:
        reason = f"{timeout:g} 秒内连不上"
    except requests.exceptions.ReadTimeout:
        reason = f"连上了，但 {timeout:g} 秒内没有回话"
    except requests.exceptions.RequestException as exc:
        reason = type(exc).__name__
    else:
        return Finding(
            OK, ONLINE, f"连得上 {where}（不带凭证访问根地址，回了 HTTP {response.status_code}）"
        )
    return Finding(
        WARN, ONLINE, f"连不上 {where}：{reason}。看看网络和代理，或者 upstream.base_url 对不对"
    )


# ---------------------------------------------------------------- 总入口


def diagnose(
    paths: HubPaths,
    *,
    clock: Clock,
    config_path: Path | None = None,
    home_existed: bool = True,
    fix: bool = False,
    online: bool = False,
    environ: Mapping[str, str] | None = None,
) -> list[Finding]:
    """做全部检查，按固定的先后返回各项结论。数据目录要已经存在（刚建的由调用者建好，
    并用 `home_existed=False` 告诉这里）。"""
    config, config_finding = _load_config(paths, config_path)
    effective = config or Config()
    tz = effective.display.tzinfo

    permissions = _permissions(paths, fix)
    account, broken = read_account(paths)
    database_finding, db = _open_database(paths, effective.db.busy_timeout_ms)
    try:
        service_finding, running = _service(paths, clock)

        rows: list[Finding | None] = [
            _home(paths, home_existed),
            permissions,
            _credentials(account is None, broken),
            _login(db, account.digest, tz) if db is not None and account is not None else None,
            database_finding,
            service_finding,
            _port(config, running) if config is not None else None,
            config_finding,
            _guarantees(db, config, clock) if db is not None and config is not None else None,
            _prefixes(db, config, clock) if db is not None and config is not None else None,
            _alert_key(paths, environ),
            _bark_result(db) if db is not None else None,
            _config_warnings(config) if config is not None else None,
            _scan_marker(paths, clock),
            _disk(paths),
            _backup(paths, config, tz, clock),
        ]
        if online:
            base_url = (
                account.base_url if account is not None else ""
            ) or effective.upstream.base_url
            rows.append(_online(base_url, effective.upstream.request_timeout))
    finally:
        if db is not None:
            db.close()
    return [row for row in rows if row is not None]
