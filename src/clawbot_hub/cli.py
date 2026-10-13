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
import sqlite3
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import tzinfo
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from clawbot_hub.account import AccountStore
from clawbot_hub.clock import Clock, RealClock
from clawbot_hub.config import Config, ConfigError, load_config
from clawbot_hub.db import Database, DatabaseProblem, is_busy, migrate
from clawbot_hub.login import LoginError, qr_login
from clawbot_hub.paths import HubPaths, ensure_home, resolve_home
from clawbot_hub.projects import NeedsConfirmation, ProjectError, Projects
from clawbot_hub.status import (
    NO_PROJECTS,
    collect_projects,
    collect_status,
    project_line,
    status_json,
    status_lines,
)
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
    try:
        config = load_config(config_path or paths.config)
    except ConfigError as exc:
        _fail(str(exc))
    return Session(env, paths, config)


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


app = typer.Typer(
    name="hubctl",
    help="微信 ClawBot 共用服务的命令行",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
project_app = typer.Typer(
    help="项目管理：登记、查看", no_args_is_help=True, pretty_exceptions_enable=False
)
app.add_typer(project_app, name="project")


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
        view = collect_status(session.paths, session.config, session.env.clock, db)
    if as_json:
        _say(json.dumps(status_json(view, session.tz), ensure_ascii=False, indent=2))
    else:
        for line in status_lines(view, session.tz):
            _say(line)


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
