"""`hubctl status` 和 `hubctl project list` 要给的东西：先收集成一份结构（`StatusView`、
`ProjectRow`），再排成中文（给人看）或 JSON（给脚本看）。

只读：服务停着也能看，不连腾讯；数据库还不存在（服务从没启动、也没登记过项目）就当它是空的。
输出里没有登录凭证、对话凭证、项目密钥。规格 `hub-cli`「看全貌」、`hub-projects`「查看」。

**这里定下的约定**（规格没写，任务单 A8 的实现说明里也有）：

- 登录状态按"服务现在启动会得到的结果"说：文件没有是"还没绑定"，读不出来是"读不出来"，文件好的时候
  库里记着"已失效"而且摘要和文件一致才是"已失效"，其余（含库里记的摘要和文件对不上，也就是服务停着时
  又扫了码）都是"已绑定"——和服务里 `LoginMonitor.sync` 的比对一致；
- 窗口里的"已用"只在窗口开着时有数，过期或没有窗口都是 0（下一次使用者说话开新窗口，从 0 起）；
  条数里的推送包括内部名字 `_import` 名下的，回复包括 `_hub` 名下的；
- 在线只看库（`projects.is_online`）；
- "提示"现在有三条：窗口快到期（按 `alerts.window_warn.hours_before`，`enabled` 关了就不提示）、
  配置文件比运行中的服务启动得晚、保底合计加回复保留超过窗口总条数；没配告警设备码这一条等告警模块
  （D2）有了设备码的读法再加。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any

from clawbot_hub.account import AccountFile, AccountFileError, AccountStore
from clawbot_hub.clock import Clock
from clawbot_hub.config import Config
from clawbot_hub.db import Database
from clawbot_hub.instance import RunInfo, service_running
from clawbot_hub.loginstate import read_login
from clawbot_hub.paths import HubPaths
from clawbot_hub.projects import Project, Projects, is_online
from clawbot_hub.quota import HOUR_MS, Quota, Window, closed_reason, window_ends_ms
from clawbot_hub.timefmt import iso_text, moment_text, span_text

BOT_ID_TAIL = 4  # 机器人号只显示最后几位


# ---------------------------------------------------------------- 数据


@dataclass(frozen=True)
class ProjectRow:
    """`project list` 的一行，也是 `status` 里的一行。已用条数是当前窗口（开着才有数）里的。"""

    name: str
    prefix: str | None
    is_default: bool
    label: str | None
    guaranteed: int
    cap: int
    enabled: bool
    push_only: bool
    removed: bool
    online: bool
    last_seen_ms: int | None
    push_used: int
    reply_used: int

    def to_json(self, tz: tzinfo) -> dict[str, Any]:
        return {
            "name": self.name,
            "prefix": self.prefix,
            "is_default": self.is_default,
            "label": self.label,
            "guaranteed": self.guaranteed,
            "cap": self.cap,
            "enabled": self.enabled,
            "push_only": self.push_only,
            "removed": self.removed,
            "online": self.online,
            "last_seen": _iso(self.last_seen_ms, tz),
            "push_used": self.push_used,
            "reply_used": self.reply_used,
        }


@dataclass(frozen=True)
class ServiceView:
    running: bool
    pid: int | None = None
    uptime_seconds: int | None = None
    version: str | None = None
    listen: str | None = None
    profile: str | None = None
    confirm: str | None = None


@dataclass(frozen=True)
class LoginView:
    state: str  # ok / expired / missing / broken
    bound_at_ms: int | None = None
    bot_id_tail: str | None = None
    since_ms: int | None = None  # 只有"已失效"才有：从什么时候起
    last_poll_ok_ms: int | None = None
    last_poll_error: str | None = None
    last_poll_error_ms: int | None = None
    ignored_count: int = 0
    has_details: bool = False  # 数据库在，收消息的情况才有数可说


@dataclass(frozen=True)
class WindowView:
    state: str  # none / open / closed
    limit: int
    started_ms: int | None = None
    ends_ms: int | None = None
    remaining_seconds: int | None = None
    used: int = 0
    push_used: int = 0
    reply_used: int = 0
    paused: bool = False
    hold_code: str | None = None
    hold_until_ms: int | None = None
    hold_left_seconds: int | None = None  # 暂停还有多久；暂停期已过是 0 以下


@dataclass(frozen=True)
class PendingView:
    queued: dict[str, int] = field(default_factory=dict)
    interrupted_sends: int = 0
    unsent_alerts: int = 0


@dataclass(frozen=True)
class StatusView:
    service: ServiceView
    login: LoginView
    window: WindowView
    projects: list[ProjectRow]
    pending: PendingView
    hints: list[str]


# ---------------------------------------------------------------- 收集


def _iso(ms: int | None, tz: tzinfo) -> str | None:
    return None if ms is None else iso_text(ms, tz)


def _current_window(
    db: Database | None, config: Config, clock: Clock
) -> tuple[Window | None, str, dict[str, tuple[int, int]], int, int]:
    """(当前窗口, none/open/closed, 各项目已用 {名字: (推送, 回复)}, 总推送, 总回复)。
    窗口不是开着的就没有已用条数。"""
    if db is None:
        return None, "none", {}, 0, 0
    conn = db.conn()
    window = Quota(config, clock).current_window(conn)
    if window is None:
        return None, "none", {}, 0, 0
    if closed_reason(window, clock.now_ms(), config.quota) is not None:
        return window, "closed", {}, 0, 0
    rows = conn.execute(
        "SELECT p.name AS name, p.internal AS internal, u.kind AS kind, u.n AS n"
        " FROM usage u JOIN projects p ON p.id = u.project_id WHERE u.window_id = ?",
        (window.id,),
    ).fetchall()
    per_project: dict[str, tuple[int, int]] = {}
    push_total = reply_total = 0
    for row in rows:
        push, reply = (row["n"], 0) if row["kind"] == "push" else (0, row["n"])
        push_total, reply_total = push_total + push, reply_total + reply
        if not row["internal"]:
            before = per_project.get(row["name"], (0, 0))
            per_project[row["name"]] = (before[0] + push, before[1] + reply)
    return window, "open", per_project, push_total, reply_total


def _project_row(
    project: Project, used: tuple[int, int], config: Config, now_ms: int
) -> ProjectRow:
    return ProjectRow(
        name=project.name,
        prefix=project.prefix,
        is_default=project.is_default,
        label=project.label,
        guaranteed=project.guaranteed,
        cap=project.cap,
        enabled=project.enabled,
        push_only=project.push_only,
        removed=project.removed,
        online=is_online(project, now_ms, config.inbox.online_seconds),
        last_seen_ms=project.last_seen_ms,
        push_used=used[0],
        reply_used=used[1],
    )


def collect_projects(
    db: Database | None, config: Config, clock: Clock, *, include_removed: bool = False
) -> list[ProjectRow]:
    """按登记先后列出项目（没有数据库就是空的）；`_hub`、`_import` 不列。"""
    if db is None:
        return []
    _, _, per_project, _, _ = _current_window(db, config, clock)
    now = clock.now_ms()
    listed = Projects(db, config, clock).list(include_removed=include_removed)
    return [_project_row(p, per_project.get(p.name, (0, 0)), config, now) for p in listed]


def _service(paths: HubPaths, clock: Clock) -> tuple[ServiceView, RunInfo | None]:
    info = service_running(paths, clock)
    if info is None:
        return ServiceView(running=False), None
    uptime = None
    if info.started_ms:
        uptime = max(0, (clock.now_ms() - info.started_ms) // 1000)
    view = ServiceView(
        running=True,
        pid=info.pid,
        uptime_seconds=uptime,
        version=info.version or None,
        listen=info.listen or None,
        profile=info.profile or None,
        confirm=info.confirm or None,
    )
    return view, info


def _read_account(paths: HubPaths) -> tuple[AccountFile | None, bool]:
    """(凭证, 是不是读不出来)。文件不存在就不去碰锁文件，免得只看一眼也在数据目录里留下东西。"""
    if not paths.account.exists():
        return None, False
    try:
        return AccountStore(paths).read(), False
    except AccountFileError:
        return None, True


def _bound_at_ms(text: str, tz: tzinfo) -> int | None:
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=tz)
    return int(when.timestamp() * 1000)


def _login(paths: HubPaths, db: Database | None, tz: tzinfo) -> LoginView:
    account, broken = _read_account(paths)
    row = read_login(db.conn()) if db is not None else None
    if broken:
        state = "broken"
    elif account is None:
        state = "missing"
    elif row is not None and row.state == "expired" and row.digest == account.digest:
        state = "expired"
    else:
        state = "ok"

    polled = None
    if db is not None:
        polled = (
            db.conn()
            .execute(
                "SELECT last_poll_ok_ms, last_poll_error, last_poll_error_ms, ignored_count"
                " FROM account_state WHERE id = 1"
            )
            .fetchone()
        )
    return LoginView(
        state=state,
        bound_at_ms=_bound_at_ms(account.bound_at, tz) if account and account.bound_at else None,
        bot_id_tail=(account.bot_id[-BOT_ID_TAIL:] or None) if account else None,
        since_ms=row.since_ms if state == "expired" and row is not None else None,
        last_poll_ok_ms=polled["last_poll_ok_ms"] if polled else None,
        last_poll_error=polled["last_poll_error"] if polled else None,
        last_poll_error_ms=polled["last_poll_error_ms"] if polled else None,
        ignored_count=polled["ignored_count"] if polled else 0,
        has_details=polled is not None,
    )


def _window(
    window: Window | None, state: str, totals: tuple[int, int], config: Config, clock: Clock
) -> WindowView:
    limit = config.quota.window_limit
    if window is None:
        return WindowView(state="none", limit=limit)
    now = clock.now_ms()
    ends = window_ends_ms(window, config.quota)
    if state == "closed":
        return WindowView(
            state="closed",
            limit=limit,
            started_ms=window.started_ms,
            ends_ms=ends,
            remaining_seconds=0,
        )
    paused = window.blocked_until_ms is not None
    return WindowView(
        state="open",
        limit=limit,
        started_ms=window.started_ms,
        ends_ms=ends,
        remaining_seconds=(ends - now) // 1000,
        used=totals[0] + totals[1],
        push_used=totals[0],
        reply_used=totals[1],
        paused=paused,
        hold_code=window.blocked_code if paused else None,
        hold_until_ms=window.blocked_until_ms,
        hold_left_seconds=(window.blocked_until_ms - now) // 1000 if paused else None,
    )


def _pending(db: Database | None) -> PendingView:
    if db is None:
        return PendingView()
    conn = db.conn()
    queued = conn.execute(
        "SELECT p.name AS name, COUNT(*) AS n FROM inbox i JOIN projects p ON p.id = i.project_id"
        " WHERE i.state = 'queued' AND p.internal = 0 GROUP BY p.id ORDER BY p.id"
    ).fetchall()
    interrupted = conn.execute("SELECT COUNT(*) FROM outbox WHERE status = 'interrupted'")
    unsent = conn.execute("SELECT COUNT(*) FROM alert_state WHERE pending_text IS NOT NULL")
    return PendingView(
        queued={row["name"]: row["n"] for row in queued},
        interrupted_sends=interrupted.fetchone()[0],
        unsent_alerts=unsent.fetchone()[0],
    )


def _hints(
    window: WindowView,
    projects: list[ProjectRow],
    run: RunInfo | None,
    paths: HubPaths,
    config: Config,
) -> list[str]:
    found: list[str] = []
    warn = config.alerts.window_warn
    if (
        warn.enabled
        and window.state == "open"
        and window.remaining_seconds is not None
        and window.remaining_seconds * 1000 < warn.hours_before * HOUR_MS
    ):
        found.append(
            f"对话窗口还剩不到 {warn.hours_before} 小时，快到期了：给 ClawBot 回一句话续期"
        )
    if run is not None and run.started_ms:
        try:
            changed_ms = int(paths.config.stat().st_mtime * 1000)
        except OSError:
            changed_ms = 0
        if changed_ms > run.started_ms:
            found.append("配置文件改过，重启服务才生效")
    holding = [p for p in projects if p.enabled]
    total = sum(p.guaranteed for p in holding)
    reserve, limit = config.quota.reply_reserve, config.quota.window_limit
    if total + reserve > limit:
        listing = "、".join(f"{p.name} {p.guaranteed}" for p in holding)
        found.append(
            f"保底合计 {total} 加上回复保留 {reserve} 超过窗口总条数 {limit}（{listing}）："
            "服务照常运行，只是登记和启用项目会被拒绝"
        )
    return found


def collect_status(
    paths: HubPaths, config: Config, clock: Clock, db: Database | None
) -> StatusView:
    tz = config.display.tzinfo
    service, run = _service(paths, clock)
    window, state, _, push_total, reply_total = _current_window(db, config, clock)
    window_view = _window(window, state, (push_total, reply_total), config, clock)
    projects = collect_projects(db, config, clock)
    return StatusView(
        service=service,
        login=_login(paths, db, tz),
        window=window_view,
        projects=projects,
        pending=_pending(db),
        hints=_hints(window_view, projects, run, paths, config),
    )


# ---------------------------------------------------------------- JSON


def status_json(view: StatusView, tz: tzinfo) -> dict[str, Any]:
    service, login, window = view.service, view.login, view.window
    return {
        "service": {
            "running": service.running,
            "pid": service.pid,
            "uptime_seconds": service.uptime_seconds,
            "version": service.version,
            "listen": service.listen,
            "profile": service.profile,
            "confirm": service.confirm,
        },
        "login": {
            "state": login.state,
            "bound_at": _iso(login.bound_at_ms, tz),
            "bot_id_tail": login.bot_id_tail,
            "since": _iso(login.since_ms, tz),
            "last_poll_ok": _iso(login.last_poll_ok_ms, tz),
            "last_poll_error": login.last_poll_error,
            "last_poll_error_at": _iso(login.last_poll_error_ms, tz),
            "ignored_count": login.ignored_count,
        },
        "window": {
            "state": window.state,
            "started": _iso(window.started_ms, tz),
            "ends": _iso(window.ends_ms, tz),
            "remaining_seconds": window.remaining_seconds,
            "limit": window.limit,
            "used": window.used,
            "push_used": window.push_used,
            "reply_used": window.reply_used,
            "paused": window.paused,
            "hold_code": window.hold_code,
            "hold_until": _iso(window.hold_until_ms, tz),
        },
        "projects": [row.to_json(tz) for row in view.projects],
        "pending": {
            "queued": dict(view.pending.queued),
            "interrupted_sends": view.pending.interrupted_sends,
            "unsent_alerts": view.pending.unsent_alerts,
        },
        "hints": list(view.hints),
    }


# ---------------------------------------------------------------- 中文


NO_PROJECTS = "没有登记项目（hubctl project add 登记）"


def project_line(row: ProjectRow, tz: tzinfo) -> str:
    """一个项目一句话，`project list` 和 `status` 一样。"""
    tags = []
    if row.prefix:
        tags.append(f"前缀「{row.prefix}」")
    if row.is_default:
        tags.append("默认项目")
    if row.label:
        tags.append(f"标签「{row.label}」")
    if row.push_only:
        tags.append("只推送")
    if row.removed:
        tags.append("已移除")
    elif not row.enabled:
        tags.append("已停用")
    head = row.name + (f"（{'，'.join(tags)}）" if tags else "")
    seen = "还没有" if row.last_seen_ms is None else moment_text(row.last_seen_ms, tz)
    return (
        f"{head}：保底 {row.guaranteed}、上限 {row.cap}，"
        f"本窗口已推送 {row.push_used} 条、回复 {row.reply_used} 条；"
        f"{'在线' if row.online else '离线'}；最后出现 {seen}"
    )


def _service_line(service: ServiceView) -> str:
    if not service.running:
        return "服务：没有在运行"
    inside = f"进程号 {service.pid}"
    if service.uptime_seconds is not None:
        inside += f"，已运行 {span_text(service.uptime_seconds)}"
    line = f"服务：在运行（{inside}）"
    for label, value in (
        ("版本", service.version),
        ("监听", service.listen),
        ("上游档位", service.profile),
        ("送达确认", service.confirm),
    ):
        if value:
            line += f"，{label} {value}"
    return line


def _login_lines(login: LoginView, tz: tzinfo) -> list[str]:
    if login.state == "missing":
        return ["登录：还没绑定（在电脑上运行 hubctl login 扫码）"]
    if login.state == "broken":
        return ["登录：读不出来（account.json 坏了；重新运行 hubctl login）"]
    if login.state == "expired":
        since = "" if login.since_ms is None else f"{moment_text(login.since_ms, tz)} 起；"
        lines = [f"登录：已失效（{since}在电脑上运行 hubctl login 重新扫码）"]
    else:
        facts = []
        if login.bound_at_ms is not None:
            facts.append(f"{moment_text(login.bound_at_ms, tz)} 绑定")
        if login.bot_id_tail:
            facts.append(f"机器人号 …{login.bot_id_tail}")
        lines = ["登录：已绑定" + (f"（{'，'.join(facts)}）" if facts else "")]
    if login.has_details:
        ok = "还没有" if login.last_poll_ok_ms is None else moment_text(login.last_poll_ok_ms, tz)
        if login.last_poll_error is None and login.last_poll_error_ms is None:
            failed = "没有"
        else:
            when = (
                ""
                if login.last_poll_error_ms is None
                else moment_text(login.last_poll_error_ms, tz)
            )
            failed = f"{when}（{login.last_poll_error}）" if login.last_poll_error else when
        lines.append(f"  收消息：最近一次成功 {ok}，最近一次出错 {failed}")
        lines.append(f"  忽略的陌生人消息：{login.ignored_count} 条")
    return lines


def _window_lines(window: WindowView, tz: tzinfo) -> list[str]:
    if window.state == "none":
        return ["对话窗口：还没收到过你的消息"]
    assert window.started_ms is not None and window.ends_ms is not None
    started, ends = moment_text(window.started_ms, tz), moment_text(window.ends_ms, tz)
    if window.state == "closed":
        return [f"对话窗口：已过期（{started} 起，{ends} 到期）；要等你再发一句话"]
    assert window.remaining_seconds is not None
    lines = [
        f"对话窗口：开着（{started} 起，还剩约 {span_text(window.remaining_seconds)}）",
        f"  已用 {window.used}/{window.limit} 条"
        f"（推送 {window.push_used}、回复 {window.reply_used}）",
    ]
    if not window.paused:
        lines.append("  腾讯拒绝后暂停：否")
    elif window.hold_left_seconds is not None and window.hold_left_seconds > 0:
        lines.append(
            f"  腾讯拒绝后暂停：是（代码 {window.hold_code}，"
            f"还有约 {span_text(window.hold_left_seconds)}）"
        )
    else:
        lines.append(
            f"  腾讯拒绝后暂停：暂停期已过，下一条推送会先试探一次（代码 {window.hold_code}）"
        )
    return lines


def _pending_lines(pending: PendingView) -> list[str]:
    lines = []
    if pending.queued:
        waiting = "、".join(f"{name} {n} 条" for name, n in pending.queued.items())
        lines.append(f"  等着项目取的话：{waiting}")
    if pending.interrupted_sends:
        lines.append(f"  中断的发送：{pending.interrupted_sends} 条")
    if pending.unsent_alerts:
        lines.append(f"  没送出的告警：{pending.unsent_alerts} 条")
    return ["待处理：没有"] if not lines else ["待处理：", *lines]


def status_lines(view: StatusView, tz: tzinfo) -> list[str]:
    lines = [_service_line(view.service)]
    lines += _login_lines(view.login, tz)
    lines += _window_lines(view.window, tz)
    if view.projects:
        lines.append("项目：")
        lines += [f"  {project_line(row, tz)}" for row in view.projects]
    else:
        lines.append(f"项目：{NO_PROJECTS}")
    lines += _pending_lines(view.pending)
    if view.hints:
        lines.append("提示：")
        lines += [f"  {hint}" for hint in view.hints]
    return lines
