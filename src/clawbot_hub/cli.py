"""`hubctl`：命令行入口。

这一版（任务 A8）有 `login`、`project add / list`、`status`，连同后面所有命令共用的约定：

- 每个命令都认 `--home`（数据目录，默认环境变量 `CLAWBOT_HUB_HOME`，再默认 `~/.clawbot-hub`）和
  `--config`（配置文件，默认 `<数据目录>/config.yaml`，不存在就用内置默认值；自己指定的文件不存在
  是错误，免得手误写错路径时悄悄用默认值）。数据目录不存在就建出来（0700），已有的不改它的权限。
- 退出码：0 成功；1 失败（一句话说清楚错在哪、怎么办）；2 用法错误（参数不对，含"不是终端又没带
  `--yes`"的拒绝）。各命令另有自己的退出码时在命令里写明。
- 要确认的操作：在终端里交互问；带 `--yes` 不问；不是终端又没带 `--yes` 拒绝、退出 2、什么也不改。
- 读数据库的命令（`status`、`project list`）在数据库还不存在时当它是空的，不去建；会写的命令
  （`project add`）建库、升级库。输出里不出现登录凭证、对话凭证；项目密钥只在 `project add` 成功时
  打印一次。

测试里用 `Env` 换掉时钟、腾讯和"标准输入是不是终端"（`CliRunner.invoke(app, ..., obj=Env(...))`）。
规格：`hub-cli`「公共约定」「看全貌」，`hub-projects`「登记项目」「查看」，`hub-login`「扫码登录」。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import tzinfo
from enum import StrEnum
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from typer.core import TyperGroup

import clawbot_hub
from clawbot_hub.account import AccountStore
from clawbot_hub.alerts import send_test_alert
from clawbot_hub.backup import BackupError, backup_name, make_backup, timestamp
from clawbot_hub.bark import ENV_KEY, lookup_bark_key, write_bark_key
from clawbot_hub.clock import Clock, RealClock
from clawbot_hub.config import Config, ConfigError, load_config
from clawbot_hub.confshow import config_lines
from clawbot_hub.db import (
    Database,
    DatabaseProblem,
    is_busy,
    latest_version,
    migrate,
    stored_version,
)
from clawbot_hub.doctor import diagnose, exit_code, home_unusable, summary_line
from clawbot_hub.events import DEFAULT_LIMIT, KINDS, add_event, recent_events
from clawbot_hub.instance import service_running
from clawbot_hub.login import LoginError, import_credentials, qr_login
from clawbot_hub.paths import HubPaths, ensure_home, resolve_home
from clawbot_hub.projects import (
    GRACE_MAX_MINUTES,
    NeedsConfirmation,
    Project,
    ProjectError,
    ProjectNotFound,
    Projects,
    prefix_reminder,
)
from clawbot_hub.sendcmd import (
    DEFAULT_TIMEOUT_SECONDS,
    KEY_ENV,
    UsageProblem,
    check_address,
    read_key,
    read_text,
    send_message,
)
from clawbot_hub.sendcmd import (
    EXIT_USAGE as SEND_EXIT_USAGE,
)
from clawbot_hub.status import (
    NO_PROJECTS,
    collect_project,
    collect_projects,
    collect_status,
    config_changed_since_start,
    project_detail_json,
    project_detail_lines,
    project_line,
    status_json,
    status_lines,
)
from clawbot_hub.timefmt import moment_text
from clawbot_hub.upstream.base import Upstream
from clawbot_hub.upstream.http import HttpUpstream

EXIT_FAILED = 1
EXIT_USAGE = 2


# ---------------------------------------------------------------- 环境


def _http_upstream(config: Config) -> Upstream:
    return HttpUpstream(config.upstream, message_id_keys=config.send.message_id_keys)


def _stdin_is_terminal() -> bool:
    return sys.stdin.isatty()


@dataclass
class Env:
    """命令行碰到外面世界的几个口子；测试里换成假的。"""

    clock: Clock = field(default_factory=RealClock)
    upstream_factory: Callable[[Config], Upstream] = _http_upstream
    is_terminal: Callable[[], bool] = _stdin_is_terminal
    environ: Mapping[str, str] | None = None  # `None` 就是真的环境变量


@dataclass(frozen=True)
class Session:
    """一次命令运行要用的东西：数据目录的各个路径和读好的配置。"""

    env: Env
    paths: HubPaths
    config: Config
    config_file: Path  # 读的那份配置文件（可能不存在）

    @property
    def tz(self) -> tzinfo:
        return self.config.display.tzinfo


HomeOption = Annotated[
    Path | None,
    typer.Option("--home", help="数据目录（默认环境变量 CLAWBOT_HUB_HOME，再默认 ~/.clawbot-hub）"),
]
ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", help="配置文件（默认 <数据目录>/config.yaml，不存在就用内置默认值）"),
]
JsonOption = Annotated[bool, typer.Option("--json", help="输出 JSON，给脚本用")]
YesOption = Annotated[bool, typer.Option("--yes", help="需要确认的操作不再问，直接做")]


def _say(text: str = "") -> None:
    typer.echo(text)


def _fail(text: str, code: int = EXIT_FAILED) -> NoReturn:
    typer.echo(text, err=True)
    raise typer.Exit(code)


def _session(ctx: typer.Context, home: Path | None, config_path: Path | None) -> Session:
    env = ctx.obj
    paths = HubPaths(resolve_home(home, env.environ))
    try:
        ensure_home(paths)
    except NotADirectoryError as exc:
        _fail(str(exc))
    except OSError as exc:
        _fail(f"数据目录建不出来：{paths.home}（{exc.strerror}）")
    if config_path is not None and not config_path.is_file():
        _fail(f"配置文件不存在或不是文件：{config_path}")
    config_file = config_path or paths.config
    try:
        config = load_config(config_file)
    except ConfigError as exc:
        _fail(str(exc))
    return Session(env, paths, config, config_file)


@contextmanager
def _database(session: Session, *, create: bool) -> Iterator[Database | None]:
    """打开数据库（会写的命令 `create=True`，必要时建库、升级库；只读的命令库不存在就给 `None`）。
    库的毛病和"别人占着锁"在这里变成一句话。"""
    db = None
    try:
        if create or session.paths.db.exists():
            busy_ms = session.config.db.busy_timeout_ms
            migrate(session.paths, clock=session.env.clock, tz=session.tz, busy_timeout_ms=busy_ms)
            db = Database(session.paths.db, busy_timeout_ms=busy_ms)
        yield db
    except DatabaseProblem as exc:
        _fail(str(exc))
    except sqlite3.OperationalError as exc:
        if is_busy(exc):
            _fail("数据库正忙（别的命令或服务正在写），稍后再试")
        raise
    finally:
        if db is not None:
            db.close()


@contextmanager
def _projects(session: Session, name: str) -> Iterator[Projects]:
    """改一个已登记项目的命令用：数据库还不存在就是"没有这个项目"，不去建库。"""
    with _database(session, create=False) as db:
        if db is None:
            _fail(f"没有叫 {name} 的项目")
        yield Projects(db, session.config, session.env.clock)


def _confirmed[T](session: Session, action: Callable[[bool], T], yes: bool) -> T:
    """做一件可能要使用者确认的事：`action(yes)` 抛 `NeedsConfirmation` 时，终端里问一句，
    不是终端就拒绝（退出 2，什么也没改）。"""
    try:
        return action(yes)
    except NeedsConfirmation as ask:
        question = str(ask)
        if not session.env.is_terminal():
            _fail(f"{question}。不是在终端里运行，要执行请加 --yes（什么也没改）", EXIT_USAGE)
        try:
            agreed = typer.confirm(f"{question}，确认吗？")
        except typer.Abort:
            agreed = False
        if not agreed:
            _fail("已取消，什么也没改", EXIT_FAILED)
        return action(True)


# ---------------------------------------------------------------- 命令


# `hubctl --help` 里命令的先后：同规格「命令一览」那张表（`serve` 在 D6 加）
COMMAND_ORDER = (
    "serve",
    "login",
    "import-credentials",
    "status",
    "project",
    "send",
    "alerts",
    "doctor",
    "backup",
    "events",
    "config",
    "version",
)


class _InSpecOrder(TyperGroup):
    """Typer 默认把单个命令排在前面、命令组排在后面；这里改成规格里的先后。"""

    def list_commands(self, ctx: typer.Context) -> list[str]:
        def place(name: str) -> int:
            return COMMAND_ORDER.index(name) if name in COMMAND_ORDER else len(COMMAND_ORDER)

        return sorted(super().list_commands(ctx), key=place)


app = typer.Typer(
    name="hubctl",
    cls=_InSpecOrder,
    help="微信 ClawBot 共用服务的命令行",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
project_app = typer.Typer(
    help="项目管理：登记、查看", no_args_is_help=True, pretty_exceptions_enable=False
)
app.add_typer(project_app, name="project")
alerts_app = typer.Typer(
    help="告警：配置 Bark 设备码、发测试告警", no_args_is_help=True, pretty_exceptions_enable=False
)
app.add_typer(alerts_app, name="alerts")
config_app = typer.Typer(
    help="配置：显示生效的配置", no_args_is_help=True, pretty_exceptions_enable=False
)
app.add_typer(config_app, name="config")


@app.callback()
def _root(ctx: typer.Context) -> None:
    if not isinstance(ctx.obj, Env):
        ctx.obj = Env()


@app.command("login", help="扫码绑定微信 ClawBot")
def login(ctx: typer.Context, home: HomeOption = None, config: ConfigOption = None) -> None:
    session = _session(ctx, home, config)
    store = AccountStore(session.paths)
    before = _read_bytes(session.paths.account)
    with _database(session, create=False) as db:
        try:
            qr_login(
                upstream=session.env.upstream_factory(session.config),
                paths=session.paths,
                store=store,
                clock=session.env.clock,
                tz=session.tz,
                say=_say,
                ask_verify_code=_ask_verify_code,
                db=db,
            )
        except LoginError as exc:
            _fail(str(exc))
        except KeyboardInterrupt:
            if _read_bytes(session.paths.account) == before:
                _fail("已中断：登录凭证没有改动")
            _fail("已中断：新的登录凭证已经写好了，服务会自动换上（hubctl status 可以看）")


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _ask_verify_code() -> str:
    try:
        return typer.prompt("验证码", default="", show_default=False)
    except typer.Abort:
        raise LoginError("需要验证码，但没有输入：重新运行 hubctl login") from None


@app.command("status", help="看全貌：服务、登录、对话窗口、项目、待处理、提示")
def status(
    ctx: typer.Context,
    home: HomeOption = None,
    config: ConfigOption = None,
    as_json: JsonOption = False,
) -> None:
    session = _session(ctx, home, config)
    with _database(session, create=False) as db:
        view = collect_status(
            session.paths, session.config, session.env.clock, db, session.env.environ
        )
    if as_json:
        _say(json.dumps(status_json(view, session.tz), ensure_ascii=False, indent=2))
    else:
        for line in status_lines(view, session.tz):
            _say(line)


@app.command(
    "doctor",
    short_help="逐项检查毛病，有错误退出码 1",
    help="逐项检查毛病：权限、凭证、数据库、服务、端口、配置、告警、磁盘、备份。"
    "每项一行，✓ 通过、⚠ 警告、✗ 错误，最后一行汇总；有 ✗ 时退出码 1。",
)
def doctor(
    ctx: typer.Context,
    home: HomeOption = None,
    config: ConfigOption = None,
    online: Annotated[
        bool,
        typer.Option("--online", help="再试探地连一下腾讯的服务器（不带凭证，不收发消息）"),
    ] = False,
    fix: Annotated[
        bool, typer.Option("--fix", help="把放宽了的权限改回 0700 / 0600（只修这一类）")
    ] = False,
) -> None:
    # 不走 `_session`：配置有错、数据目录有问题都要变成检查结果里的一行，而不是直接退出
    env = ctx.obj
    paths = HubPaths(resolve_home(home, env.environ))
    existed = paths.home.exists()
    try:
        ensure_home(paths)
    except NotADirectoryError as exc:
        findings = home_unusable(str(exc))
    except OSError as exc:
        findings = home_unusable(f"数据目录建不出来：{paths.home}（{exc.strerror}）")
    else:
        findings = diagnose(
            paths,
            clock=env.clock,
            config_path=config,
            home_existed=existed,
            fix=fix,
            online=online,
            environ=env.environ,
        )
    for finding in findings:
        _say(finding.line())
    _say(summary_line(findings))
    raise typer.Exit(exit_code(findings))


@project_app.command("add", help="登记一个项目，并打印它的密钥（只打印这一次）")
def project_add(
    ctx: typer.Context,
    name: Annotated[
        str, typer.Argument(help="项目名字：小写字母开头，2 到 32 个字符，只含小写字母、数字、减号")
    ],
    guaranteed: Annotated[int, typer.Option("--guaranteed", help="保底条数")],
    cap: Annotated[int, typer.Option("--cap", help="上限条数")],
    prefix: Annotated[
        str | None, typer.Option("--prefix", help="前缀（能收话的项目必须有，只推送的项目没有）")
    ] = None,
    make_default: Annotated[
        bool, typer.Option("--default", help="成为默认项目（已有默认项目时要确认）")
    ] = False,
    label: Annotated[str | None, typer.Option("--label", help="消息第一行前的【标签】")] = None,
    no_label: Annotated[bool, typer.Option("--no-label", help="不要标签")] = False,
    push_only: Annotated[bool, typer.Option("--push-only", help="只推送、不取话")] = False,
    yes: YesOption = False,
    home: HomeOption = None,
    config: ConfigOption = None,
) -> None:
    session = _session(ctx, home, config)
    with _database(session, create=True) as db:
        projects = Projects(db, session.config, session.env.clock)

        def register(confirmed: bool):
            return projects.add(
                name,
                prefix=prefix,
                guaranteed=guaranteed,
                cap=cap,
                make_default=make_default,
                yes=confirmed,
                label=label,
                no_label=no_label,
                push_only=push_only,
            )

        try:
            registration = _confirmed(session, register, yes)
        except ProjectError as exc:
            _fail(str(exc))
    project = registration.project
    facts = []
    if project.prefix:
        facts.append(f"前缀「{project.prefix}」")
    facts.append(f"保底 {project.guaranteed}、上限 {project.cap}")
    if project.push_only:
        facts.append("只推送")
    if project.is_default:
        facts.append("默认项目")
    _say(f"已登记项目 {project.name}（{'，'.join(facts)}）")
    if registration.replaced_default is not None:
        _say(f"原来的默认项目 {registration.replaced_default} 不再是默认项目")
    _say("密钥（只显示这一次，请马上存好，之后任何命令都不再显示）：")
    _say(registration.key)
    for note in registration.notes:
        _say(note)


@project_app.command("list", help="列出项目：名字、前缀、保底、上限、在线与否、本窗口已用条数")
def project_list(
    ctx: typer.Context,
    home: HomeOption = None,
    config: ConfigOption = None,
    all_projects: Annotated[bool, typer.Option("--all", help="连已移除的也列出来")] = False,
    as_json: JsonOption = False,
) -> None:
    session = _session(ctx, home, config)
    with _database(session, create=False) as db:
        rows = collect_projects(db, session.config, session.env.clock, include_removed=all_projects)
    if as_json:
        _say(json.dumps([row.to_json(session.tz) for row in rows], ensure_ascii=False, indent=2))
    elif rows:
        for row in rows:
            _say(project_line(row, session.tz))
    else:
        _say(NO_PROJECTS)


@project_app.command("show", help="看一个项目的详情：前缀、保底、上限、状态、在线与否、已用条数")
def project_show(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="项目名字")],
    home: HomeOption = None,
    config: ConfigOption = None,
    as_json: JsonOption = False,
) -> None:
    session = _session(ctx, home, config)
    with _database(session, create=False) as db:
        if db is None:
            _fail(f"没有叫 {name} 的项目")
        try:
            project, row = collect_project(db, session.config, session.env.clock, name)
        except ProjectNotFound as exc:
            _fail(str(exc))
    if as_json:
        _say(
            json.dumps(project_detail_json(project, row, session.tz), ensure_ascii=False, indent=2)
        )
    else:
        for line in project_detail_lines(project, row, session.tz):
            _say(line)


def _changes(before: Project, after: Project) -> list[str]:
    said = []
    if before.prefix != after.prefix:
        said.append(f"前缀「{before.prefix}」→「{after.prefix}」")
    if before.guaranteed != after.guaranteed:
        said.append(f"保底 {before.guaranteed} → {after.guaranteed}")
    if before.cap != after.cap:
        said.append(f"上限 {before.cap} → {after.cap}")
    if before.label != after.label:
        said.append("标签 → 去掉" if after.label is None else f"标签 → {after.label}")
    if after.is_default and not before.is_default:
        said.append("成为默认项目")
    return said


@project_app.command("set", help="改项目的前缀、保底、上限、标签，或把它设成默认项目")
def project_set(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="项目名字")],
    prefix: Annotated[str | None, typer.Option("--prefix", help="新前缀（要确认）")] = None,
    guaranteed: Annotated[int | None, typer.Option("--guaranteed", help="新的保底条数")] = None,
    cap: Annotated[int | None, typer.Option("--cap", help="新的上限条数")] = None,
    label: Annotated[str | None, typer.Option("--label", help="消息第一行前的【标签】")] = None,
    no_label: Annotated[bool, typer.Option("--no-label", help="不要标签")] = False,
    make_default: Annotated[
        bool, typer.Option("--default", help="成为默认项目（已有默认项目时要确认）")
    ] = False,
    yes: YesOption = False,
    home: HomeOption = None,
    config: ConfigOption = None,
) -> None:
    if label is not None and no_label:
        _fail("--label 和 --no-label 不能同时用", EXIT_USAGE)
    nothing = prefix is None and guaranteed is None and cap is None and label is None
    if nothing and not no_label and not make_default:
        _fail(
            "没有要改的：请给 --prefix、--guaranteed、--cap、--label、--no-label 或 --default",
            EXIT_USAGE,
        )
    session = _session(ctx, home, config)
    with _projects(session, name) as projects:
        try:
            before = projects.get(name)
            previous_default = projects.default()

            def change(confirmed: bool) -> Project:
                return projects.set(
                    name,
                    prefix=prefix,
                    guaranteed=guaranteed,
                    cap=cap,
                    label=label,
                    no_label=no_label,
                    make_default=make_default,
                    yes=confirmed,
                )

            after = _confirmed(session, change, yes)
        except ProjectError as exc:
            _fail(str(exc))
    said = _changes(before, after)
    if not said:
        _say(f"项目 {name} 没有变化")
        return
    _say(f"已修改项目 {name}：" + "、".join(said))
    if after.is_default and not before.is_default and previous_default is not None:
        _say(f"原来的默认项目 {previous_default.name} 不再是默认项目")
    if before.prefix != after.prefix and after.prefix:
        _say(prefix_reminder(after.prefix))


@project_app.command("disable", help="停用一个项目：对下一个请求立即生效")
def project_disable(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="项目名字")],
    home: HomeOption = None,
    config: ConfigOption = None,
) -> None:
    session = _session(ctx, home, config)
    with _projects(session, name) as projects:
        try:
            was_enabled = projects.get(name).enabled
            projects.disable(name)
        except ProjectError as exc:
            _fail(str(exc))
    if was_enabled:
        _say(f"已停用项目 {name}（对下一个请求立即生效）")
    else:
        _say(f"项目 {name} 本来就是停用的")


@project_app.command("enable", help="启用一个项目（会重新核对保底合计）")
def project_enable(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="项目名字")],
    home: HomeOption = None,
    config: ConfigOption = None,
) -> None:
    session = _session(ctx, home, config)
    with _projects(session, name) as projects:
        try:
            was_enabled = projects.get(name).enabled
            projects.enable(name)
        except ProjectError as exc:
            _fail(str(exc))
    if was_enabled:
        _say(f"项目 {name} 本来就是启用的")
    else:
        _say(f"已启用项目 {name}")


@project_app.command("remove", help="移除一个项目（要先停用；没取走的话作废，名字不能再用）")
def project_remove(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="项目名字")],
    yes: YesOption = False,
    home: HomeOption = None,
    config: ConfigOption = None,
) -> None:
    session = _session(ctx, home, config)
    with _projects(session, name) as projects:
        try:
            _confirmed(session, lambda confirmed: projects.remove(name, yes=confirmed), yes)
        except ProjectError as exc:
            _fail(str(exc))
    _say(f"已移除项目 {name}（没取走的话已作废，名字不能再用）")


@project_app.command("rotate", help="给项目换一把新密钥，并打印它（只打印这一次）")
def project_rotate(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="项目名字")],
    grace: Annotated[
        int, typer.Option("--grace", help=f"旧密钥再用几分钟（0 到 {GRACE_MAX_MINUTES}，默认 0）")
    ] = 0,
    yes: YesOption = False,
    home: HomeOption = None,
    config: ConfigOption = None,
) -> None:
    if not 0 <= grace <= GRACE_MAX_MINUTES:
        _fail(f"宽限期要在 0 到 {GRACE_MAX_MINUTES} 分钟之间", EXIT_USAGE)
    session = _session(ctx, home, config)
    with _projects(session, name) as projects:
        try:
            rotation = _confirmed(
                session,
                lambda confirmed: projects.rotate(name, grace_minutes=grace, yes=confirmed),
                yes,
            )
        except ProjectError as exc:
            _fail(str(exc))
    if rotation.grace_until_ms is None:
        _say(f"已给项目 {name} 换了密钥，旧密钥立刻失效")
    else:
        until = moment_text(rotation.grace_until_ms, session.tz)
        _say(f"已给项目 {name} 换了密钥，旧密钥再用 {grace} 分钟（到 {until}）")
    _say("新密钥（只显示这一次，请马上存好，之后任何命令都不再显示）：")
    _say(rotation.key)


@app.command(
    "events",
    short_help="看最近的事件记录（没有正文）",
    help="看最近的事件记录：时间、级别、种类、项目、一句话（没有正文）",
)
def events(
    ctx: typer.Context,
    home: HomeOption = None,
    config: ConfigOption = None,
    limit: Annotated[
        int, typer.Option("--limit", min=1, help=f"列最近多少条（默认 {DEFAULT_LIMIT}）")
    ] = DEFAULT_LIMIT,
    kind: Annotated[
        str | None, typer.Option("--kind", help="只看一种：" + "、".join(KINDS))
    ] = None,
    as_json: JsonOption = False,
) -> None:
    if kind is not None and kind not in KINDS:
        _fail(f"不认识的事件种类：{kind or '（空）'}。可用的种类：{'、'.join(KINDS)}", EXIT_USAGE)
    session = _session(ctx, home, config)
    with _database(session, create=False) as db:
        rows = [] if db is None else recent_events(db.conn(), limit=limit, kind=kind)
    if as_json:
        _say(json.dumps([row.to_json(session.tz) for row in rows], ensure_ascii=False, indent=2))
    elif rows:
        for row in rows:
            _say(row.line(session.tz))
    else:
        _say("没有事件记录")


@alerts_app.command("set-key", help="从标准输入读 Bark 设备码（或 App 里的整个推送地址）存起来")
def alerts_set_key(
    ctx: typer.Context, home: HomeOption = None, config: ConfigOption = None
) -> None:
    session = _session(ctx, home, config)
    if session.env.is_terminal():
        try:
            typed = typer.prompt(
                "Bark 设备码或推送地址（输入时不显示）",
                hide_input=True,
                default="",
                show_default=False,
            )
        except typer.Abort:
            typed = ""
    else:
        typed = sys.stdin.readline()
    lines = typed.splitlines()
    value = lines[0].strip() if lines else ""
    if not value:
        _fail(
            "没有读到设备码：把设备码（或 Bark App 里的推送地址）从标准输入给我，例如 "
            "echo 设备码 | hubctl alerts set-key"
        )
    try:
        write_bark_key(session.paths, value)
    except ValueError as exc:
        _fail(str(exc))
    except OSError as exc:
        _fail(f"设备码存不进 bark.json（{exc.strerror}）")
    _say("设备码已保存到 bark.json（权限 0600）。运行 hubctl alerts test 发一条测试告警。")
    if lookup_bark_key(session.paths, session.env.environ).source == "env":
        _say(f"注意：环境变量 {ENV_KEY} 里也有设备码，它优先；要用这里存的，先取消那个环境变量。")


@alerts_app.command("test", help="发一条测试告警，报告 Bark 收没收到")
def alerts_test(ctx: typer.Context, home: HomeOption = None, config: ConfigOption = None) -> None:
    session = _session(ctx, home, config)
    with _database(session, create=True) as db:
        result = send_test_alert(
            db, session.config, session.env.clock, session.paths, env=session.env.environ
        )
    if result.status == "delivered":
        _say("测试告警已送到：Bark 收下了，手机上应该很快会收到一条标题含「测试」的通知。")
    elif result.status == "refused":
        _fail(
            f"测试告警被 Bark 拒绝：{result.detail}。"
            "多半是设备码填错了，重新运行 hubctl alerts set-key"
        )
    elif result.status == "unreachable":
        _fail(f"测试告警没送到：{result.detail}。检查这台电脑的网络，或者稍后再试")
    else:
        _fail(f"{result.detail}。先运行 hubctl alerts set-key")


@config_app.command("show", help="显示生效的配置（含默认值；设备码只写有没有配）")
def config_show(ctx: typer.Context, home: HomeOption = None, config: ConfigOption = None) -> None:
    session = _session(ctx, home, config)
    lines = config_lines(
        session.config,
        file=session.config_file,
        exists=session.config_file.is_file(),
        key=lookup_bark_key(session.paths, session.env.environ),
        changed_since_start=config_changed_since_start(
            session.paths, service_running(session.paths, session.env.clock)
        ),
    )
    for line in lines:
        _say(line)


@app.command("version", help="版本号、接口版本、数据库版本")
def version(ctx: typer.Context, home: HomeOption = None, config: ConfigOption = None) -> None:
    session = _session(ctx, home, config)
    _say(f"hubctl {clawbot_hub.__version__}")
    _say(f"接口版本：{clawbot_hub.API_VERSION}")
    needed = latest_version()
    try:
        stored = stored_version(session.paths.db, session.config.db.busy_timeout_ms)
    except DatabaseProblem:
        found = "库文件读不懂"
    except sqlite3.OperationalError as exc:
        if not is_busy(exc):
            raise
        found = "库文件正忙，读不到"
    else:
        if stored is None:
            found = "还没有库文件"
        elif stored > needed:
            found = f"库文件里是 {stored}（比程序新，程序不会动它）"
        else:
            found = f"库文件里是 {stored}"
    _say(f"数据库版本：程序需要 {needed}，{found}")


# 路径里有这两个词之一，说明在 iCloud 云盘或别的同步目录里
CLOUD_MARKERS = ("Mobile Documents", "CloudStorage")
CREDENTIALS_WARNING = "备份里有登录凭证，不要放进云盘或 git。"


def _in_cloud_folder(path: Path) -> bool:
    shown = {str(path), str(path.resolve())}  # 符号链接指到云盘里去的也算
    return any(marker in text for text in shown for marker in CLOUD_MARKERS)


@app.command(
    "backup",
    short_help="备份数据库、登录凭证和配置",
    help="备份数据库、登录凭证和配置到 <数据目录>/backups/<时间>/",
)
def backup(
    ctx: typer.Context,
    home: HomeOption = None,
    config: ConfigOption = None,
    to: Annotated[
        Path | None,
        typer.Option("--to", help="备份的父目录：备份写到 <目录>/<时间>/，已有的目录不改它的权限"),
    ] = None,
) -> None:
    session = _session(ctx, home, config)
    parent = session.paths.backups if to is None else to.expanduser()
    with _database(session, create=False) as db:
        if db is None:
            _fail("没有数据库可备份：还没有登记过项目，也没有绑定过账号")
        if to is not None and _in_cloud_folder(parent):
            typer.echo(
                f"⚠⚠ 警告：{parent} 在云盘或同步目录里，云盘会把备份同步出去；{CREDENTIALS_WARNING}"
                "仍然照做，请尽快把它挪走。",
                err=True,
            )
        clock = session.env.clock
        name = backup_name("manual", timestamp(clock.now_ms(), session.tz))
        try:
            made = make_backup(
                session.paths, parent, name, busy_timeout_ms=session.config.db.busy_timeout_ms
            )
        except BackupError as exc:
            _fail(str(exc))
        try:
            with db.txn() as conn:
                add_event(conn, clock.now_ms(), "backup", f"手动备份完成：{name}")
            recorded = True
        except sqlite3.OperationalError as exc:  # 备份已经做成了，别因为记不上事件就说它失败
            if not is_busy(exc):
                raise
            recorded = False
    _say(f"备份完成：{made.directory}")
    for filename, size in made.files.items():
        _say(f"  {filename}：{size} 字节")
    if not recorded:
        _say("（数据库正忙，这次备份没能写进事件记录）")
    _say(CREDENTIALS_WARNING)


@app.command(
    "import-credentials",
    short_help="接过 ashare-quant 的登录凭证",
    help="接过 ashare-quant 的 clawbot.json（登录凭证和当前窗口）",
)
def import_credentials_command(
    ctx: typer.Context,
    source: Annotated[Path, typer.Argument(help="ashare-quant 的 data/notify/clawbot.json")],
    home: HomeOption = None,
    config: ConfigOption = None,
    replace: Annotated[
        bool, typer.Option("--replace", help="已经有 account.json 时覆盖它（旧的先备份）")
    ] = False,
    probe: Annotated[
        bool, typer.Option("--probe", help="导入后用这份凭证向腾讯收一次消息，只看有没有失效")
    ] = False,
    yes: YesOption = False,
) -> None:
    session = _session(ctx, home, config)

    def action(confirmed: bool):
        if replace and session.paths.account.exists() and not confirmed:
            raise NeedsConfirmation(
                "--replace 会覆盖已有的登录凭证（旧文件先备份成 account.json.bak-<时间>）"
            )
        return import_credentials(
            source,
            paths=session.paths,
            store=AccountStore(session.paths),
            clock=session.env.clock,
            tz=session.tz,
            replace=replace,
            probe=session.env.upstream_factory(session.config) if probe else None,
            probe_timeout=float(session.config.upstream.poll_timeout),
            window_hours=session.config.quota.window_hours,
        )

    try:
        report = _confirmed(session, action, yes)
    except LoginError as exc:
        _fail(str(exc))
    for line in report.lines():
        _say(line)


class Priority(StrEnum):
    urgent = "urgent"
    normal = "normal"
    deferrable = "deferrable"


class OnClosed(StrEnum):
    fail = "fail"
    skip = "skip"


def _send_address(
    ctx_env: Env, home: Path | None, config_path: Path | None, url: str | None
) -> str:
    """服务的地址：`--url` 给了就用它（用不着读配置）；没给就是本机加配置里的端口。
    `send` 是给脚本用的，所以不建数据目录。"""
    if url is not None:
        return check_address(url)
    if config_path is not None and not config_path.is_file():
        raise UsageProblem(
            f"配置文件不存在或不是文件：{config_path}（或者用 --url 直接给出服务的地址）"
        )
    file = config_path or HubPaths(resolve_home(home, ctx_env.environ)).config
    try:
        port = load_config(file).server.port
    except ConfigError as exc:
        raise UsageProblem(f"{exc}\n或者用 --url 直接给出服务的地址") from None
    return f"http://127.0.0.1:{port}"


@app.command(
    "send",
    short_help="用项目密钥发一条消息（给脚本用）",
    help="用项目密钥给 ClawBot 发一条消息（给脚本用；退出码：0 发了或安静跳过，3 未确认，"
    "4 只发了一部分，5 被拒绝，6 失败，10 没发出去，11 密钥不对，12 其他接口错误，13 可能已发出）",
)
def send(
    ctx: typer.Context,
    text: Annotated[
        str | None,
        typer.Argument(help="要发的文字；写 - 或者不写（且标准输入不是终端）就从标准输入读"),
    ] = None,
    home: HomeOption = None,
    config: ConfigOption = None,
    url: Annotated[
        str | None, typer.Option("--url", help="服务的地址（默认 http://127.0.0.1:配置里的端口）")
    ] = None,
    key_env: Annotated[
        str | None, typer.Option("--key-env", help=f"从哪个环境变量读项目密钥（默认 {KEY_ENV}）")
    ] = None,
    key_file: Annotated[
        Path | None, typer.Option("--key-file", help="从这个文件读项目密钥（权限要 0600）")
    ] = None,
    priority: Annotated[Priority | None, typer.Option("--priority", help="优先级")] = None,
    dedupe_key: Annotated[
        str | None, typer.Option("--dedupe-key", help="去重键：同一个键 24 小时内只发一次")
    ] = None,
    on_closed: Annotated[
        OnClosed | None,
        typer.Option("--on-closed", help="对话窗口没开时：fail 当作失败（默认），skip 安静跳过"),
    ] = None,
    timeout: Annotated[
        float, typer.Option("--timeout", help="等服务回话的秒数（读超时）")
    ] = DEFAULT_TIMEOUT_SECONDS,
    as_json: JsonOption = False,
) -> None:
    env: Env = ctx.obj
    environ = os.environ if env.environ is None else env.environ
    try:
        if timeout <= 0:
            raise UsageProblem("--timeout 要大于 0")
        key = read_key(key_env=key_env, key_file=key_file, environ=environ)
        message = read_text(text, stdin=sys.stdin, interactive=env.is_terminal())
        address = _send_address(env, home, config, url)
    except UsageProblem as exc:
        _fail(str(exc), SEND_EXIT_USAGE)
    payload: dict[str, str] = {"text": message}
    if priority is not None:
        payload["priority"] = priority.value
    if dedupe_key is not None:
        payload["dedupe_key"] = dedupe_key
    if on_closed is not None:
        payload["on_closed"] = on_closed.value
    outcome = send_message(address, key, payload, timeout=timeout, as_json=as_json)
    if outcome.out:
        _say(outcome.out)
    if outcome.err:
        typer.echo(outcome.err, err=True)
    raise typer.Exit(outcome.exit_code)
