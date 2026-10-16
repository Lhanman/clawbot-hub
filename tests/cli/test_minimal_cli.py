"""最小命令行（任务 A8）：公共约定、`login`、`project add / list`、`status`。

用 `CliRunner` 调 `hubctl`：时钟、腾讯和"标准输入是不是终端"都换成假的（见 `conftest.py`）。公共约定
（`--home`、`--config`、退出码、`--yes` 的规矩）后面的命令都照这里的样子；别的子命令和 `doctor`、
`send` 等在 D3。规格 `hub-cli`「公共约定」「看全貌」、`hub-projects`「登记项目」「查看」、
`hub-login`「扫码登录」。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from clawbot_hub.account import AccountStore
from clawbot_hub.cli import Env, app
from clawbot_hub.config import Config
from clawbot_hub.db import Database, migrate
from clawbot_hub.instance import RunInfo
from clawbot_hub.paths import ensure_home
from clawbot_hub.projects import Projects
from clawbot_hub.quota import Quota
from clawbot_hub.status import config_changed_since_start
from clawbot_hub.upstream.http import HttpUpstream
from tests.cli.conftest import KEY, add, db_text, json_of, mode_of, write_run_json
from tests.login.conftest import account_file, dead_pid
from tests.quota.conftest import REJECTED, reserve, settle, speak
from tests.support.fake_upstream import QR_STATUS

SHANGHAI = ZoneInfo("Asia/Shanghai")
MINUTE = 60_000
HOUR = 60 * MINUTE


@pytest.fixture
def empty_database(hub_paths, clock, display_tz):
    """还没有任何项目的数据库（直接建，不经过命令）。"""
    ensure_home(hub_paths)
    migrate(hub_paths, clock=clock, tz=display_tz)
    db = Database(hub_paths.db)
    yield db
    db.close()


@pytest.fixture
def quota(clock) -> Quota:
    return Quota(Config(), clock)


# ---------------------------------------------------------------- 公共约定：数据目录


def test_the_data_directory_is_created_with_private_permissions(run, home):
    assert not home.exists()

    result = run("status")

    assert result.exit_code == 0, result.output
    assert home.is_dir() and mode_of(home) == 0o700
    assert "还没绑定" in result.stdout and "没有登记项目" in result.stdout


def test_reading_commands_create_nothing_but_the_directory(run, home):
    run("status")
    run("project", "list")

    assert list(home.iterdir()) == []  # 没有数据库，也没有别的文件


def test_an_existing_directory_keeps_the_permissions_it_has(run, home):
    home.mkdir(mode=0o755)
    home.chmod(0o755)

    assert run("status").exit_code == 0

    assert mode_of(home) == 0o755  # 放宽了的权限由 doctor --fix 改，别的命令不动


def test_the_directory_comes_from_the_option_then_the_environment_then_the_default(
    run, tmp_path, monkeypatch
):
    from_option = tmp_path / "from-option"
    from_env = tmp_path / "from-env"
    monkeypatch.setenv("CLAWBOT_HUB_HOME", str(from_env))

    run("status", "--home", str(from_option), home_option=False)
    assert from_option.is_dir() and not from_env.exists()

    run("status", home_option=False)
    assert from_env.is_dir()

    monkeypatch.delenv("CLAWBOT_HUB_HOME")
    run("status", home_option=False)
    assert (Path(os.environ["HOME"]) / ".clawbot-hub").is_dir()


def test_a_file_where_the_directory_should_be_is_one_sentence_not_a_traceback(run, home):
    home.write_text("not a directory")

    result = run("status")

    assert result.exit_code == 1
    assert "数据目录" in result.output and "Traceback" not in result.output


def test_a_directory_that_cannot_be_created_is_one_sentence_not_a_traceback(run, home, monkeypatch):
    def refuse(paths):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("clawbot_hub.cli.ensure_home", refuse)

    result = run("status")

    assert result.exit_code == 1
    assert f"数据目录建不出来：{home}（Permission denied）" in result.output
    assert "Traceback" not in result.output


def test_without_a_prepared_environment_the_real_clock_and_terminal_are_used(home):
    """不带假环境直接调（`hubctl` 装好以后就是这样）：用真的时钟，也不去碰腾讯。"""
    runner = CliRunner()
    before = int(time.time() * 1000)

    added = runner.invoke(
        app,
        ["project", "add", "alpha", "--prefix", "量化", "--guaranteed", "2", "--cap", "4"]
        + ["--home", str(home)],
    )
    listed = runner.invoke(app, ["project", "list", "--json", "--home", str(home)])

    assert added.exit_code == 0, added.output
    assert [p["name"] for p in json_of(listed)] == ["alpha"]
    with sqlite3.connect(home / "hub.db") as conn:
        (created,) = conn.execute("SELECT created_ms FROM projects WHERE name = 'alpha'").fetchone()
    assert before <= created <= int(time.time() * 1000)


def test_the_default_environment_builds_the_real_tencent_client_without_connecting():
    """`hubctl` 装好以后没有假环境：默认"造腾讯客户端"造出来的是真的那个（造的时候不联网）。"""
    upstream = Env().upstream_factory(Config())

    assert isinstance(upstream, HttpUpstream)


@pytest.mark.parametrize("answer", [True, False])
def test_the_default_terminal_check_asks_standard_input(monkeypatch, answer):
    class FakeStdin:
        def isatty(self):
            return answer

    monkeypatch.setattr("sys.stdin", FakeStdin())

    assert Env().is_terminal() is answer


# ---------------------------------------------------------------- 公共约定：配置文件


NARROW = "quota:\n  window_limit: 6\n  reply_reserve: 1\n"


def test_the_configuration_defaults_to_config_yaml_in_the_data_directory(run, home):
    home.mkdir(mode=0o700)
    (home / "config.yaml").write_text(NARROW, encoding="utf-8")

    result = add(run, "alpha", guaranteed=1, cap=6)

    assert result.exit_code == 1
    assert "上限最多 5" in result.output  # 6 条窗口减回复保留 1


def test_the_config_option_names_another_file(run, tmp_path, home):
    home.mkdir(mode=0o700)
    (home / "config.yaml").write_text(NARROW, encoding="utf-8")
    other = tmp_path / "other.yaml"
    other.write_text("quota:\n  window_limit: 20\n", encoding="utf-8")

    result = add(run, "alpha", "--config", str(other), guaranteed=1, cap=6)

    assert result.exit_code == 0, result.output


def test_without_a_configuration_file_the_built_in_defaults_apply(run):
    assert add(run, "alpha", guaranteed=1, cap=8).exit_code == 0  # 10 条窗口减回复保留 2
    assert "上限最多 8" in add(run, "bravo", "--no-label", prefix="乙", guaranteed=1, cap=9).output


def test_a_config_option_that_points_nowhere_is_an_error_not_the_defaults(run, tmp_path):
    result = run("status", "--config", str(tmp_path / "typo.yaml"))

    assert result.exit_code == 1
    assert "配置文件不存在" in result.output


def test_a_config_option_that_is_a_directory_is_an_error_too(run, tmp_path):
    result = run("status", "--config", str(tmp_path))

    assert result.exit_code == 1
    assert f"配置文件不存在或不是文件：{tmp_path}" in result.output
    assert "Traceback" not in result.output


def test_a_broken_configuration_lists_every_mistake_at_once(run, home):
    home.mkdir(mode=0o700)
    (home / "config.yaml").write_text(
        "quota:\n  window_limit: 1\nserver:\n  port: 99999\n", encoding="utf-8"
    )

    result = run("status")

    assert result.exit_code == 1
    assert "配置有 2 处错误" in result.output
    assert "quota.window_limit" in result.output and "server.port" in result.output


# ---------------------------------------------------------------- 公共约定：数据库


def test_a_database_held_by_another_writer_is_one_sentence_not_a_traceback(
    database, run, home, hub_paths
):
    (home / "config.yaml").write_text("db:\n  busy_timeout_ms: 100\n", encoding="utf-8")
    holder = sqlite3.connect(hub_paths.db, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        result = add(run, "other", prefix="乙")
        waited = time.monotonic() - started
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert result.exit_code == 1
    assert "数据库正忙" in result.output and "Traceback" not in result.output
    assert 0.05 <= waited < 3  # 等了配置里的 100 毫秒，不是默认的 5 秒
    assert "other" not in [p["name"] for p in json_of(run("project", "list", "--json"))]


def test_a_database_error_that_is_not_about_waiting_is_not_hidden(run, monkeypatch):
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(Projects, "add", broken)

    result = add(run, "alpha")

    assert result.exit_code == 1
    assert isinstance(result.exception, sqlite3.OperationalError)
    assert "数据库正忙" not in result.output


def test_the_database_is_closed_when_a_command_ends_whether_it_worked_or_not(run, monkeypatch):
    closed = []
    real_close = Database.close
    monkeypatch.setattr(Database, "close", lambda self: (closed.append(self), real_close(self))[1])

    assert add(run, "alpha").exit_code == 0
    assert add(run, "bravo", guaranteed=9, cap=4).exit_code == 1  # 规则拒绝
    assert run("status").exit_code == 0
    assert run("project", "list").exit_code == 0

    assert len(closed) == 4


# ---------------------------------------------------------------- 公共约定：退出码和 --yes


def test_a_missing_required_option_is_a_usage_error(run):
    result = run("project", "add", "alpha", "--prefix", "量化", "--guaranteed", "2")

    assert result.exit_code == 2


def test_a_number_that_is_not_a_number_is_a_usage_error(run):
    result = run("project", "add", "alpha", "--prefix", "量化", "--guaranteed", "x", "--cap", "4")

    assert result.exit_code == 2


def test_an_unknown_command_is_a_usage_error(run):
    assert run("no-such-command").exit_code == 2


def test_a_refusal_from_the_rules_exits_one_with_one_sentence(database, run):
    result = run("project", "add", "seed", "--prefix", "乙", "--guaranteed", "1", "--cap", "2")

    assert result.exit_code == 1
    assert "名字 seed 已经有项目在用" in result.output
    assert "Traceback" not in result.output


def test_a_one_sentence_error_goes_to_standard_error_and_nothing_to_standard_output(run):
    """脚本里 `hubctl … > 文件` 时，错误不能混进文件里。"""
    result = run("project", "show", "nobody")

    assert result.exit_code == 1
    assert result.stderr.strip() == "没有叫 nobody 的项目"
    assert result.stdout == ""


def test_an_unexpected_error_gets_a_plain_traceback_not_the_frameworks_dressed_up_one(
    monkeypatch, capsys, tmp_path
):
    """Typer 默认的"漂亮报错"会把源码（有时还有局部变量）打在屏幕上，变量里可能有密钥；关掉，
    没料到的错误就是 Python 平常的 Traceback。"""

    def boom(*_args, **_kwargs):
        secret_in_a_local_variable = "chub_not_for_the_screen"  # noqa: F841
        raise RuntimeError("没料到的错误")

    monkeypatch.setattr("clawbot_hub.cli.load_config", boom)
    monkeypatch.setattr("sys.excepthook", sys.excepthook)  # `app(...)` 会换掉它；测完还原
    with pytest.raises(RuntimeError) as caught:
        app(["status", "--home", str(tmp_path / "hub-home")])

    sys.excepthook(type(caught.value), caught.value, caught.value.__traceback__)
    shown = capsys.readouterr().err

    assert "Traceback (most recent call last):" in shown
    assert "RuntimeError: 没料到的错误" in shown
    assert "╭" not in shown and "chub_not_for_the_screen" not in shown


def test_not_a_terminal_and_no_yes_refuses_with_two_and_changes_nothing(database, run):
    result = add(run, "other", "--default", prefix="乙")

    assert result.exit_code == 2
    assert "--yes" in result.output and "原来的默认项目是 seed" in result.output
    assert [p.name for p in Projects(database, Config(), None).list()] == ["seed"]
    assert Projects(database, Config(), None).default().name == "seed"


def test_yes_goes_ahead_without_asking(database, run, clock):
    result = add(run, "other", "--default", "--yes", prefix="乙")

    assert result.exit_code == 0, result.output
    assert "原来的默认项目 seed 不再是默认项目" in result.stdout
    assert Projects(database, Config(), clock).default().name == "other"


def test_in_a_terminal_the_user_is_asked_and_a_yes_goes_ahead(database, run, terminal, clock):
    terminal["on"] = True

    result = add(run, "other", "--default", prefix="乙", input="y\n")

    assert result.exit_code == 0, result.output
    assert "要把默认项目换成 other" in result.output
    assert Projects(database, Config(), clock).default().name == "other"


def test_in_a_terminal_a_no_changes_nothing_and_exits_one(database, run, terminal, clock):
    terminal["on"] = True

    result = add(run, "other", "--default", prefix="乙", input="n\n")

    assert result.exit_code == 1
    assert "已取消" in result.output and "什么也没改" in result.output
    assert Projects(database, Config(), clock).default().name == "seed"
    assert [p.name for p in Projects(database, Config(), clock).list()] == ["seed"]


def test_in_a_terminal_with_no_answer_at_all_cancels_too(database, run, terminal):
    terminal["on"] = True

    result = add(run, "other", "--default", prefix="乙", input="")

    assert result.exit_code == 1 and "已取消" in result.output


def test_yes_is_not_needed_when_nothing_has_to_be_confirmed(run):
    assert add(run, "alpha").exit_code == 0  # 第一个能收话的项目自己成为默认项目


# ---------------------------------------------------------------- project add


def test_adding_a_project_prints_the_key_once_and_creates_the_database(run, hub_paths, clock):
    result = add(run, "alpha", prefix="量化", guaranteed=6, cap=8)

    assert result.exit_code == 0, result.output
    assert "已登记项目 alpha" in result.stdout
    assert "前缀「量化」" in result.stdout and "保底 6、上限 8" in result.stdout
    assert "默认项目" in result.stdout
    assert "只显示这一次" in result.stdout
    (key,) = KEY.findall(result.stdout)
    assert KEY.findall(result.stderr) == []
    assert hub_paths.db.exists() and mode_of(hub_paths.db) == 0o600
    db = Database(hub_paths.db)
    try:
        assert Projects(db, Config(), clock).authenticate(key).name == "alpha"
    finally:
        db.close()


def test_the_registration_says_what_was_registered(run):
    first = add(run, "alpha")
    push_only = add(run, "cron-job", "--push-only", prefix=None, guaranteed=0, cap=2)

    assert (
        first.stdout.splitlines()[0] == "已登记项目 alpha（前缀「量化」，保底 2、上限 4，默认项目）"
    )
    assert push_only.stdout.splitlines()[0] == "已登记项目 cron-job（保底 0、上限 2，只推送）"


def test_adding_a_project_reminds_about_the_default_projects_own_words(run):
    result = add(run, "alpha", prefix="状态")

    assert "提醒" in result.stdout and "前缀「状态」" in result.stdout


def test_a_push_only_project_has_no_prefix_and_no_reminder(run):
    result = add(run, "cron-job", "--push-only", prefix=None, guaranteed=0, cap=2)

    assert result.exit_code == 0, result.output
    assert "只推送" in result.stdout and "提醒" not in result.stdout
    assert "默认项目" not in result.stdout


def test_the_label_options_reach_the_registration(run):
    run("project", "list")  # 先有个数据目录
    assert add(run, "alpha", prefix="甲").exit_code == 0
    assert add(run, "bravo", "--label", "乙乙", prefix="乙").exit_code == 0
    assert add(run, "charlie", "--no-label", prefix="丙").exit_code == 0
    assert add(run, "delta", prefix="丁").exit_code == 0

    labels = {p["name"]: p["label"] for p in json_of(run("project", "list", "--json"))}

    assert labels == {"alpha": None, "bravo": "乙乙", "charlie": None, "delta": "丁"}


def test_the_rules_of_a_registration_come_back_as_they_are(run):
    assert "需要收话的项目必须有前缀" in add(run, "alpha", prefix=None).output
    assert "保底不能大于上限" in add(run, "alpha", guaranteed=5, cap=4).output
    assert "名字" in add(run, "Alpha").output
    assert "不能同时" in add(run, "alpha", "--label", "甲", "--no-label").output


def test_the_key_is_nowhere_in_the_database_the_events_or_any_later_output(run, hub_paths):
    (key,) = KEY.findall(add(run, "alpha").stdout)

    later = [
        run("project", "list"),
        run("project", "list", "--json"),
        run("project", "list", "--all"),
        run("status"),
        run("status", "--json"),
    ]

    for result in later:
        assert result.exit_code == 0, result.output
        assert "chub_" not in result.output
    assert key.encode() not in db_text(hub_paths)
    assert b"chub_" not in db_text(hub_paths)


def test_the_registration_is_in_the_event_log_without_the_key(database, run):
    add(run, "alpha", prefix="量化", guaranteed=2, cap=4)

    rows = database.conn().execute("SELECT kind, project, detail FROM events").fetchall()

    assert ("project", "alpha", "登记项目 alpha（保底 2、上限 4）") in [tuple(r) for r in rows]


# ---------------------------------------------------------------- project list


def test_an_empty_json_listing_is_an_empty_list_with_or_without_a_database(run, hub_paths):
    assert run("project", "list", "--json").stdout.strip() == "[]"
    assert not hub_paths.db.exists()

    assert add(run, "alpha").exit_code == 0
    assert run("project", "list", "--json").stdout.strip() != "[]"
    with sqlite3.connect(hub_paths.db) as conn:
        conn.execute("UPDATE projects SET removed_ms = 1 WHERE name = 'alpha'")
    assert run("project", "list", "--json").stdout.strip() == "[]"


def test_listing_with_nothing_registered_says_so(run):
    result = run("project", "list")

    assert result.exit_code == 0 and "没有登记项目" in result.stdout


def test_listing_shows_one_line_per_project_and_never_the_internal_names(empty_database, run):
    add(run, "ashare-quant", prefix="量化", guaranteed=6, cap=8)
    add(run, "cron-job", "--push-only", prefix=None, guaranteed=0, cap=2)

    result = run("project", "list")

    assert result.exit_code == 0, result.output
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 2
    first, second = lines
    assert first.startswith("ashare-quant") and "前缀「量化」" in first and "默认项目" in first
    assert "保底 6、上限 8" in first
    assert second.startswith("cron-job") and "只推送" in second and "标签「cron-job」" in second
    assert "_hub" not in result.stdout and "_import" not in result.stdout


def test_a_project_is_online_when_it_took_a_message_within_the_configured_seconds(
    empty_database, run, clock
):
    add(run, "alpha")
    add(run, "bravo", prefix="乙")
    with empty_database.txn() as conn:
        conn.execute(
            "UPDATE projects SET last_inbox_ms = ?, last_seen_ms = ? WHERE name = 'alpha'",
            (clock.now_ms() - 30_000, clock.now_ms() - 10_000),
        )
        conn.execute(
            "UPDATE projects SET last_inbox_ms = ?, last_seen_ms = ? WHERE name = 'bravo'",
            (clock.now_ms() - 120_000, clock.now_ms() - 5_000),  # 刚推送过，但很久没取话
        )

    rows = {p["name"]: p for p in json_of(run("project", "list", "--json"))}

    assert rows["alpha"]["online"] is True and rows["bravo"]["online"] is False
    lines = run("project", "list").stdout.splitlines()
    assert "在线" in next(line for line in lines if line.startswith("alpha"))
    assert "离线" in next(line for line in lines if line.startswith("bravo"))


def test_the_online_window_follows_the_configuration(empty_database, run, clock, home):
    add(run, "alpha")
    with empty_database.txn() as conn:
        conn.execute("UPDATE projects SET last_inbox_ms = ?", (clock.now_ms() - 120_000,))
    (home / "config.yaml").write_text("inbox:\n  online_seconds: 300\n", encoding="utf-8")

    (alpha,) = json_of(run("project", "list", "--json"))

    assert alpha["online"] is True


def test_last_seen_is_written_in_the_display_time_zone(empty_database, run, clock):
    add(run, "alpha")
    seen = int(datetime(2026, 10, 12, 9, 41, tzinfo=SHANGHAI).timestamp() * 1000)
    with empty_database.txn() as conn:
        conn.execute("UPDATE projects SET last_seen_ms = ?", (seen,))

    (alpha,) = json_of(run("project", "list", "--json"))
    line = run("project", "list").stdout

    assert alpha["last_seen"] == "2026-10-12T09:41:00+08:00"
    assert "最后出现 10月12日 09:41" in line


def test_a_project_that_never_showed_up_says_so(empty_database, run):
    add(run, "alpha")

    (alpha,) = json_of(run("project", "list", "--json"))

    assert alpha["last_seen"] is None
    assert "最后出现 还没有" in run("project", "list").stdout


def test_listing_shows_what_the_current_window_has_used(empty_database, run, quota, clock):
    add(run, "alpha", guaranteed=3, cap=4)
    add(run, "bravo", prefix="乙")
    speak(empty_database, quota, clock.now_ms() - 5 * MINUTE)
    reserve(empty_database, quota, "alpha", 2)
    reserve(empty_database, quota, "alpha", 1, kind="reply")

    rows = {p["name"]: p for p in json_of(run("project", "list", "--json"))}
    text = run("project", "list").stdout

    assert (rows["alpha"]["push_used"], rows["alpha"]["reply_used"]) == (2, 1)
    assert (rows["bravo"]["push_used"], rows["bravo"]["reply_used"]) == (0, 0)
    assert "本窗口已推送 2 条、回复 1 条" in next(
        line for line in text.splitlines() if line.startswith("alpha")
    )


def test_nothing_counts_as_used_once_the_window_has_run_out(empty_database, run, quota, clock):
    add(run, "alpha")
    speak(empty_database, quota, clock.now_ms())
    reserve(empty_database, quota, "alpha", 2)
    clock.advance(24 * 3600)  # 窗口到期，使用者还没再说话

    (alpha,) = json_of(run("project", "list", "--json"))

    assert (alpha["push_used"], alpha["reply_used"]) == (0, 0)


def test_the_json_listing_has_every_field_and_no_secret(empty_database, run):
    add(run, "alpha", "--label", "甲", prefix="量化", guaranteed=6, cap=8)

    (alpha,) = json_of(run("project", "list", "--json"))

    assert alpha == {
        "name": "alpha",
        "prefix": "量化",
        "is_default": True,
        "label": "甲",
        "guaranteed": 6,
        "cap": 8,
        "enabled": True,
        "push_only": False,
        "removed": False,
        "online": False,
        "last_seen": None,
        "push_used": 0,
        "reply_used": 0,
    }


def test_disabled_projects_are_marked_and_removed_ones_only_show_with_all(
    empty_database, run, clock
):
    add(run, "alpha")
    add(run, "bravo", prefix="乙")
    add(run, "charlie", prefix="丙")
    projects = Projects(empty_database, Config(), clock)
    projects.disable("bravo")
    projects.disable("charlie")
    projects.remove("charlie", yes=True)

    plain = run("project", "list").stdout
    everything = run("project", "list", "--all").stdout
    flags = {p["name"]: p for p in json_of(run("project", "list", "--all", "--json"))}

    assert "bravo" in plain and "已停用" in next(
        line for line in plain.splitlines() if line.startswith("bravo")
    )
    assert "charlie" not in plain
    removed_line = next(line for line in everything.splitlines() if line.startswith("charlie"))
    assert "已移除" in removed_line and "已停用" not in removed_line
    assert flags["charlie"]["removed"] is True and flags["bravo"]["removed"] is False
    assert flags["bravo"]["enabled"] is False
    assert [p["name"] for p in json_of(run("project", "list", "--json"))] == ["alpha", "bravo"]


def test_listing_keeps_the_order_of_registration(empty_database, run):
    for name, prefix in (("mike", "甲"), ("alpha", "乙"), ("zulu", "丙")):
        add(run, name, prefix=prefix, guaranteed=1)

    assert [p["name"] for p in json_of(run("project", "list", "--json"))] == [
        "mike",
        "alpha",
        "zulu",
    ]


# ---------------------------------------------------------------- login


def test_login_walks_through_the_fake_upstream(run, upstream, hub_paths, home):
    result = run("login")

    assert result.exit_code == 0, result.output
    assert "绑定成功" in result.stdout
    assert upstream.logins == 1
    stored = AccountStore(hub_paths).read()
    assert stored is not None and stored.user_id == upstream.user_id
    assert mode_of(hub_paths.account) == 0o600
    assert not hub_paths.login_pending.exists() and not hub_paths.login_png.exists()
    assert not hub_paths.db.exists()  # 数据库还不存在：只写文件，服务第一次启动时再比对


def test_login_draws_the_code_and_says_how_long_it_lasts(run):
    result = run("login")

    assert "二维码" in result.stdout and "2 分钟" in result.stdout
    assert "█" in result.stdout or "▀" in result.stdout or "▄" in result.stdout


def test_login_never_prints_the_credentials(run, upstream):
    result = run("login")

    stored = upstream.calls_to(QR_STATUS)
    assert stored  # 确实走了扫码流程
    for secret in ("fake-token", upstream.user_id, upstream.bot_id):
        assert secret not in result.output


def test_login_with_an_existing_database_voids_the_window_in_the_same_run(
    database, run, quota, clock
):
    speak(database, quota, clock.now_ms() - HOUR, "tok-old")

    result = run("login")

    assert result.exit_code == 0, result.output
    row = database.conn().execute("SELECT voided, context_token FROM windows").fetchone()
    assert (row["voided"], row["context_token"]) == (1, None)
    events = database.conn().execute("SELECT kind, detail FROM events WHERE kind = 'login'")
    assert any("扫码绑定成功" in r["detail"] for r in events.fetchall())


def test_login_asks_for_the_verification_code_in_the_terminal(run, upstream, terminal):
    terminal["on"] = True
    upstream.qr_plan(["need_verifycode", "confirmed"])

    result = run("login", input="123456\n")

    assert result.exit_code == 0, result.output
    codes = [call.args["verify_code"] for call in upstream.calls_to(QR_STATUS)]
    assert codes[-1] == "123456"
    assert "验证码: " in result.output and "[]" not in result.output  # 提示里不带空的默认值


def test_login_that_needs_a_code_but_gets_no_answer_fails_cleanly(run, upstream, hub_paths):
    upstream.qr_plan(["need_verifycode", "confirmed"])

    result = run("login", input="")

    assert result.exit_code == 1
    assert "验证码" in result.output and "Traceback" not in result.output
    assert not hub_paths.account.exists()
    assert not hub_paths.login_pending.exists()


def test_login_failing_exits_one_and_leaves_the_account_alone(run, upstream, hub_paths):
    first = run("login")
    assert first.exit_code == 0
    before = hub_paths.account.read_bytes()
    upstream.qr_plan(["verify_code_blocked"])

    result = run("login")

    assert result.exit_code == 1
    assert "验证码错的次数太多" in result.output
    assert hub_paths.account.read_bytes() == before
    assert not hub_paths.login_pending.exists() and not hub_paths.login_png.exists()


def test_login_interrupted_with_ctrl_c_says_so_and_cleans_up(run, upstream, hub_paths):
    def interrupt(call):
        raise KeyboardInterrupt

    upstream.script(QR_STATUS, interrupt)

    result = run("login")

    assert result.exit_code == 1
    assert "已中断" in result.output and "没有改动" in result.output
    assert not hub_paths.account.exists()
    assert not hub_paths.login_pending.exists() and not hub_paths.login_png.exists()


def test_login_interrupted_leaves_an_existing_account_untouched_and_says_so(
    run, upstream, hub_paths
):
    assert run("login").exit_code == 0
    before = hub_paths.account.read_bytes()

    def interrupt(call):
        raise KeyboardInterrupt

    upstream.script(QR_STATUS, interrupt)

    result = run("login")

    assert result.exit_code == 1
    assert "已中断：登录凭证没有改动" in result.output
    assert hub_paths.account.read_bytes() == before


def test_login_interrupted_after_the_new_credentials_were_saved_says_the_service_takes_over(
    database, run, hub_paths, monkeypatch
):
    def interrupt(conn):
        raise KeyboardInterrupt

    monkeypatch.setattr("clawbot_hub.login.reset_conversation", interrupt)

    result = run("login")

    assert result.exit_code == 1
    assert "已中断" in result.output and "新的登录凭证已经写好了" in result.output
    assert "没有改动" not in result.output
    assert AccountStore(hub_paths).read() is not None
    assert not hub_paths.login_pending.exists() and not hub_paths.login_png.exists()


def test_login_writes_the_binding_time_of_the_clock_it_was_given(run, hub_paths, clock):
    assert run("login").exit_code == 0

    stored = AccountStore(hub_paths).read()
    moment = datetime.fromtimestamp(clock.now_ms() // 1000, SHANGHAI)
    assert stored is not None and stored.bound_at == moment.isoformat(timespec="seconds")


# ---------------------------------------------------------------- status：服务


def test_status_starts_with_the_service_not_running_when_there_is_no_run_file(run):
    result = run("status")

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0] == "服务：没有在运行"


def test_a_run_file_of_a_process_that_is_gone_means_not_running(run, hub_paths, clock):
    hub_paths.home.mkdir(mode=0o700)
    write_run_json(hub_paths, clock, pid=dead_pid())

    assert run("status").stdout.splitlines()[0] == "服务：没有在运行"


def test_a_heartbeat_older_than_a_minute_means_not_running(run, hub_paths, clock):
    hub_paths.home.mkdir(mode=0o700)
    write_run_json(hub_paths, clock, last_heartbeat_ms=clock.now_ms() - 5 * MINUTE)

    assert run("status").stdout.splitlines()[0] == "服务：没有在运行"


def test_a_clean_exit_means_not_running_even_with_a_fresh_heartbeat(run, hub_paths, clock):
    hub_paths.home.mkdir(mode=0o700)
    write_run_json(hub_paths, clock, clean_exit=True)

    assert run("status").stdout.splitlines()[0] == "服务：没有在运行"


def test_a_live_process_with_a_recent_heartbeat_is_running(run, hub_paths, clock):
    hub_paths.home.mkdir(mode=0o700)
    write_run_json(hub_paths, clock)

    first = run("status").stdout.splitlines()[0]

    assert first.startswith(f"服务：在运行（进程号 {os.getpid()}，已运行 1 小时 5 分钟）")
    for fact in ("版本 0.1.0", "监听 127.0.0.1:8790", "上游档位 legacy", "送达确认 observe"):
        assert fact in first


def test_a_start_time_in_the_future_reads_as_just_started(run, hub_paths, clock):
    """时钟被调慢过：运行时长不会写成负数。"""
    hub_paths.home.mkdir(mode=0o700)
    write_run_json(hub_paths, clock, started_ms=clock.now_ms() + 5 * MINUTE)

    assert run("status").stdout.splitlines()[0].startswith("服务：在运行（进程号")
    assert "已运行 不到 1 分钟" in run("status").stdout.splitlines()[0]
    assert json_of(run("status", "--json"))["service"]["uptime_seconds"] == 0


def test_a_run_file_with_only_the_required_fields_leaves_the_rest_out(run, hub_paths, clock):
    hub_paths.home.mkdir(mode=0o700)
    beat = {"pid": os.getpid(), "last_heartbeat_ms": clock.now_ms() - 5_000}
    hub_paths.run.write_text(json.dumps(beat), encoding="utf-8")

    assert run("status").stdout.splitlines()[0] == f"服务：在运行（进程号 {os.getpid()}）"
    assert json_of(run("status", "--json"))["service"] == {
        "running": True,
        "pid": os.getpid(),
        "uptime_seconds": None,
        "version": None,
        "listen": None,
        "profile": None,
        "confirm": None,
    }


def test_status_does_not_touch_tencent(run, upstream):
    run("status")
    run("status", "--json")
    run("project", "list")
    add(run, "alpha")

    assert upstream.calls == []


# ---------------------------------------------------------------- status：登录


def test_not_bound_yet(run):
    line = next(x for x in run("status").stdout.splitlines() if x.startswith("登录："))

    assert line == "登录：还没绑定（在电脑上运行 hubctl login 扫码）"


def test_bound_shows_when_and_the_last_four_of_the_bot_id(run, hub_paths):
    hub_paths.home.mkdir(mode=0o700)
    AccountStore(hub_paths).write(
        account_file(bot_id="bot-id-0042", bound_at="2026-10-02T09:05:00+08:00")
    )

    line = next(x for x in run("status").stdout.splitlines() if x.startswith("登录："))

    assert line == "登录：已绑定（10月2日 09:05 绑定，机器人号 …0042）"


def test_the_binding_time_is_shown_in_the_display_time_zone(run, hub_paths):
    hub_paths.home.mkdir(mode=0o700)
    AccountStore(hub_paths).write(account_file(bound_at="2026-10-02T09:05:00+09:00"))

    line = next(x for x in run("status").stdout.splitlines() if x.startswith("登录："))

    assert "10月2日 08:05 绑定" in line  # 东京的九点五分是上海的八点五分


def test_a_binding_time_without_an_offset_is_read_in_the_display_time_zone(run, hub_paths):
    hub_paths.home.mkdir(mode=0o700)
    AccountStore(hub_paths).write(account_file(bound_at="2026-10-02T09:05:00"))

    line = next(x for x in run("status").stdout.splitlines() if x.startswith("登录："))

    assert "10月2日 09:05 绑定" in line
    assert json_of(run("status", "--json"))["login"]["bound_at"] == "2026-10-02T09:05:00+08:00"


def test_an_account_without_a_bot_id_leaves_it_out(run, hub_paths):
    hub_paths.home.mkdir(mode=0o700)
    AccountStore(hub_paths).write(account_file(bot_id=""))

    line = next(x for x in run("status").stdout.splitlines() if x.startswith("登录："))

    assert "机器人号" not in line
    assert json_of(run("status", "--json"))["login"]["bot_id_tail"] is None


def test_a_binding_time_that_makes_no_sense_is_left_out(run, hub_paths):
    hub_paths.home.mkdir(mode=0o700)
    AccountStore(hub_paths).write(account_file(bound_at="昨天"))

    line = next(x for x in run("status").stdout.splitlines() if x.startswith("登录："))

    assert line.startswith("登录：已绑定") and "绑定，" not in line and "昨天" not in line


def test_an_unreadable_account_file_is_reported_without_its_content(run, hub_paths):
    hub_paths.home.mkdir(mode=0o700)
    hub_paths.account.write_text("{this is not json: secret-token-xyz", encoding="utf-8")

    result = run("status")
    line = next(x for x in result.stdout.splitlines() if x.startswith("登录："))

    assert result.exit_code == 0
    assert line == "登录：读不出来（account.json 坏了；重新运行 hubctl login）"
    assert "secret-token-xyz" not in result.output


def test_a_login_the_service_found_expired_is_reported(empty_database, run, hub_paths, clock):
    account = account_file()
    AccountStore(hub_paths).write(account)
    with empty_database.txn() as conn:
        conn.execute(
            "UPDATE account_state SET login_state = 'expired', login_since_ms = ?,"
            " creds_digest = ? WHERE id = 1",
            (clock.now_ms() - 2 * HOUR, account.digest),
        )

    line = next(x for x in run("status").stdout.splitlines() if x.startswith("登录："))

    assert line.startswith("登录：已失效（10月12日 08:00 起")
    assert "hubctl login" in line


def test_a_login_the_service_considers_fine_is_not_called_expired(
    empty_database, run, hub_paths, clock
):
    """库里的摘要和文件一致、状态是 ok（换过凭证时起点也会记着）：不是"已失效"，也没有失效起点。"""
    account = account_file()
    AccountStore(hub_paths).write(account)
    with empty_database.txn() as conn:
        conn.execute(
            "UPDATE account_state SET login_state = 'ok', login_since_ms = ?,"
            " creds_digest = ? WHERE id = 1",
            (clock.now_ms() - HOUR, account.digest),
        )

    line = next(x for x in run("status").stdout.splitlines() if x.startswith("登录："))
    login = json_of(run("status", "--json"))["login"]

    assert line.startswith("登录：已绑定")
    assert (login["state"], login["since"]) == ("ok", None)


def test_the_json_status_of_an_expired_login_has_its_start(empty_database, run, hub_paths, clock):
    account = account_file()
    AccountStore(hub_paths).write(account)
    with empty_database.txn() as conn:
        conn.execute(
            "UPDATE account_state SET login_state = 'expired', login_since_ms = ?,"
            " creds_digest = ? WHERE id = 1",
            (clock.now_ms() - 2 * HOUR, account.digest),
        )

    login = json_of(run("status", "--json"))["login"]

    assert (login["state"], login["since"]) == ("expired", "2026-10-12T08:00:00+08:00")


def test_a_new_login_made_while_the_service_was_stopped_is_not_called_expired(
    empty_database, run, hub_paths, clock
):
    """库里记着"已失效"，但文件里已经是另一个账号：服务启动时的比对会换上新凭证，这里按现状说。"""
    AccountStore(hub_paths).write(account_file(bot_token="new-token"))
    with empty_database.txn() as conn:
        conn.execute(
            "UPDATE account_state SET login_state = 'expired', creds_digest = 'old-digest'"
            " WHERE id = 1"
        )

    line = next(x for x in run("status").stdout.splitlines() if x.startswith("登录："))

    assert line.startswith("登录：已绑定")


def test_a_database_that_says_missing_while_the_file_is_fine_is_bound(
    empty_database, run, hub_paths
):
    AccountStore(hub_paths).write(account_file())

    line = next(x for x in run("status").stdout.splitlines() if x.startswith("登录："))

    assert line.startswith("登录：已绑定")


def test_the_receiving_details_are_shown_once_the_database_exists(
    empty_database, run, hub_paths, clock
):
    AccountStore(hub_paths).write(account_file())
    with empty_database.txn() as conn:
        conn.execute(
            "UPDATE account_state SET last_poll_ok_ms = ?, last_poll_error = '连不上腾讯',"
            " last_poll_error_ms = ?, ignored_count = 3 WHERE id = 1",
            (clock.now_ms() - 5 * MINUTE, clock.now_ms() - HOUR),
        )

    lines = run("status").stdout.splitlines()

    assert (
        "  收消息：最近一次成功 10月12日 09:55，最近一次出错 10月12日 09:00（连不上腾讯）" in lines
    )
    assert "  忽略的陌生人消息：3 条" in lines


def test_the_receiving_details_say_so_when_nothing_has_happened(empty_database, run, hub_paths):
    AccountStore(hub_paths).write(account_file())

    lines = run("status").stdout.splitlines()

    assert "  收消息：最近一次成功 还没有，最近一次出错 没有" in lines
    assert "  忽略的陌生人消息：0 条" in lines


def test_there_are_no_receiving_details_before_the_database_exists(run, hub_paths):
    hub_paths.home.mkdir(mode=0o700)
    AccountStore(hub_paths).write(account_file())

    output = run("status").stdout

    assert "登录：已绑定" in output
    assert "收消息" not in output and "忽略的陌生人消息" not in output


@pytest.mark.parametrize(
    ("error", "ago", "shown"),
    [
        ("连不上腾讯", None, "（连不上腾讯）"),
        (None, HOUR, "10月12日 09:00"),
        ("连不上腾讯", HOUR, "10月12日 09:00（连不上腾讯）"),
    ],
    ids=["only-the-reason", "only-the-time", "both"],
)
def test_the_last_receiving_error_shows_whatever_is_known_of_it(
    empty_database, run, hub_paths, clock, error, ago, shown
):
    AccountStore(hub_paths).write(account_file())
    with empty_database.txn() as conn:
        conn.execute(
            "UPDATE account_state SET last_poll_error = ?, last_poll_error_ms = ? WHERE id = 1",
            (error, None if ago is None else clock.now_ms() - ago),
        )

    lines = run("status").stdout.splitlines()

    assert f"  收消息：最近一次成功 还没有，最近一次出错 {shown}" in lines


# ---------------------------------------------------------------- status：对话窗口


def window_lines(run) -> list[str]:
    lines = run("status").stdout.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("对话窗口："))
    end = next((i for i in range(start + 1, len(lines)) if not lines[i].startswith("  ")), None)
    return lines[start:end]


def test_no_window_before_the_user_has_ever_written(run):
    assert window_lines(run) == ["对话窗口：还没收到过你的消息"]


def test_no_window_either_when_the_database_exists_but_nobody_wrote(empty_database, run):
    assert window_lines(run) == ["对话窗口：还没收到过你的消息"]


def test_an_open_window_shows_when_it_started_how_long_is_left_and_what_is_used(
    empty_database, run, quota, clock
):
    add(run, "alpha", guaranteed=3, cap=4)
    speak(empty_database, quota, clock.now_ms() - 2 * HOUR - 55 * MINUTE)
    reserve(empty_database, quota, "alpha", 2)
    reserve(empty_database, quota, "alpha", 1, kind="reply")
    reserve(empty_database, quota, "_hub", 1, kind="reply")

    assert window_lines(run) == [
        "对话窗口：开着（10月12日 07:05 起，还剩约 21 小时 5 分钟）",
        "  已用 4/10 条（推送 2、回复 2）",
        "  腾讯拒绝后暂停：否",
    ]


def test_the_total_includes_what_was_imported_under_the_internal_name(
    empty_database, run, quota, clock
):
    speak(empty_database, quota, clock.now_ms())
    window = empty_database.conn().execute("SELECT MAX(id) FROM windows").fetchone()[0]
    with empty_database.txn() as conn:
        conn.execute(
            "INSERT INTO usage (window_id, project_id, kind, n)"
            " SELECT ?, id, 'push', 3 FROM projects WHERE name = '_import'",
            (window,),
        )

    assert "  已用 3/10 条（推送 3、回复 0）" in window_lines(run)


def test_the_limit_comes_from_the_configuration(empty_database, run, quota, clock, home):
    speak(empty_database, quota, clock.now_ms())
    (home / "config.yaml").write_text("quota:\n  window_limit: 6\n", encoding="utf-8")

    assert "  已用 0/6 条（推送 0、回复 0）" in window_lines(run)


def test_a_pause_after_tencent_refused_is_shown_with_the_code_and_the_time_left(
    empty_database, run, quota, clock
):
    add(run, "alpha")
    speak(empty_database, quota, clock.now_ms())
    rejected = reserve(empty_database, quota, "alpha")
    settle(empty_database, quota, rejected, REJECTED, code="40001")
    clock.advance(10 * 60)

    assert "  腾讯拒绝后暂停：是（代码 40001，还有约 20 分钟）" in window_lines(run)


def test_a_pause_that_has_run_out_waits_for_the_next_push_to_probe(
    empty_database, run, quota, clock
):
    add(run, "alpha")
    speak(empty_database, quota, clock.now_ms())
    rejected = reserve(empty_database, quota, "alpha")
    settle(empty_database, quota, rejected, REJECTED, code="40001")
    clock.advance(31 * 60)

    assert "  腾讯拒绝后暂停：暂停期已过，下一条推送会先试探一次（代码 40001）" in window_lines(run)


def test_a_pause_that_ends_exactly_now_already_lets_the_next_push_probe(
    empty_database, run, quota, clock
):
    add(run, "alpha")
    speak(empty_database, quota, clock.now_ms())
    rejected = reserve(empty_database, quota, "alpha")
    settle(empty_database, quota, rejected, REJECTED, code="40001")
    clock.advance(30 * 60)  # 暂停 30 分钟，刚好到点

    assert "  腾讯拒绝后暂停：暂停期已过，下一条推送会先试探一次（代码 40001）" in window_lines(run)


def test_an_expired_window_says_it_waits_for_the_user_to_write_again(
    empty_database, run, quota, clock
):
    speak(empty_database, quota, clock.now_ms() - 25 * HOUR)

    assert window_lines(run) == [
        "对话窗口：已过期（10月11日 09:00 起，10月12日 09:00 到期）；要等你再发一句话"
    ]


def test_a_voided_window_counts_as_never_having_heard_from_the_user(
    empty_database, run, quota, clock
):
    speak(empty_database, quota, clock.now_ms())
    with empty_database.txn() as conn:
        conn.execute("UPDATE windows SET voided = 1")

    assert window_lines(run) == ["对话窗口：还没收到过你的消息"]


# ---------------------------------------------------------------- status：项目和待处理


def test_status_lists_the_projects_in_the_same_words_as_the_project_list(
    empty_database, run, quota, clock
):
    add(run, "ashare-quant", prefix="量化", guaranteed=6, cap=8)
    add(run, "cron-job", "--push-only", prefix=None, guaranteed=0, cap=2)
    speak(empty_database, quota, clock.now_ms())
    reserve(empty_database, quota, "ashare-quant", 2)

    status = run("status").stdout.splitlines()
    listed = run("project", "list").stdout.splitlines()

    start = status.index("项目：")
    assert [line.strip() for line in status[start + 1 : start + 3]] == listed
    assert status[start + 1].startswith("  ashare-quant")


def test_status_without_projects_points_at_the_add_command(run):
    assert "项目：没有登记项目（hubctl project add 登记）" in run("status").stdout.splitlines()


def test_nothing_pending_is_said_in_one_line(empty_database, run):
    assert "待处理：没有" in run("status").stdout.splitlines()


def test_pending_counts_what_waits_for_the_projects_and_what_went_wrong(
    empty_database, run, hub_paths, clock
):
    add(run, "alpha", prefix="甲")
    add(run, "bravo", prefix="乙")
    with empty_database.txn() as conn:
        ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM projects").fetchall()}
        for number, (name, state) in enumerate(
            [
                ("alpha", "queued"),
                ("alpha", "queued"),
                ("bravo", "queued"),
                ("bravo", "leased"),
                ("_hub", "queued"),  # 内部名字不是项目，不列
            ]
        ):
            conn.execute(
                "INSERT INTO inbox (id, project_id, text, has_text, at_ms, received_ms, via, state)"
                " VALUES (?, ?, '正文', 1, 1, 1, 'prefix', ?)",
                (f"m{number}", ids[name], state),
            )
        for number, state in enumerate(["interrupted", "interrupted", "sent"]):
            conn.execute(
                "INSERT INTO outbox (id, project_id, kind, parts, status, created_ms)"
                " VALUES (?, ?, 'push', 1, ?, 1)",
                (f"o{number}", ids["alpha"], state),
            )
        conn.execute("INSERT INTO alert_state (key, pending_text) VALUES ('a', '没送出去的告警')")
        conn.execute("INSERT INTO alert_state (key, pending_text) VALUES ('b', NULL)")

    lines = run("status").stdout.splitlines()
    pending = json_of(run("status", "--json"))["pending"]

    start = lines.index("待处理：")
    assert lines[start + 1 : start + 4] == [
        "  等着项目取的话：alpha 2 条、bravo 1 条",
        "  中断的发送：2 条",
        "  没送出的告警：1 条",
    ]
    assert pending == {
        "queued": {"alpha": 2, "bravo": 1},
        "interrupted_sends": 2,
        "unsent_alerts": 1,
    }
    assert "没送出去的告警" not in "\n".join(lines)  # 告警的正文不在状态里


# ---------------------------------------------------------------- status：提示


EXPIRY_HINT = "对话窗口还剩不到 3 小时，快到期了：给 ClawBot 回一句话续期"


def hint_lines(run) -> list[str]:
    lines = run("status").stdout.splitlines()
    if "提示：" not in lines:
        return []
    return [line.strip() for line in lines[lines.index("提示：") + 1 :]]


def test_no_hints_section_when_nothing_is_wrong(run):
    assert "提示：" not in run("status").stdout


def test_a_window_close_to_its_end_is_worth_a_hint(empty_database, run, quota, clock):
    speak(empty_database, quota, clock.now_ms() - 21 * HOUR - 30 * MINUTE)  # 还剩 2 小时 30 分钟

    (hint,) = hint_lines(run)

    assert hint == "对话窗口还剩不到 3 小时，快到期了：给 ClawBot 回一句话续期"


def test_a_window_with_enough_time_left_has_no_hint(empty_database, run, quota, clock):
    speak(empty_database, quota, clock.now_ms() - 21 * HOUR)  # 刚好还剩 3 小时

    assert hint_lines(run) == []


def test_a_window_one_second_short_of_the_warning_time_gets_the_hint(
    empty_database, run, quota, clock
):
    speak(empty_database, quota, clock.now_ms() - 21 * HOUR - 1_000)  # 还剩 2 小时 59 分 59 秒

    assert hint_lines(run) == [EXPIRY_HINT]


def test_the_warning_hours_come_from_the_configuration(empty_database, run, quota, clock, home):
    speak(empty_database, quota, clock.now_ms() - 10 * HOUR)  # 还剩 14 小时
    (home / "config.yaml").write_text(
        "alerts:\n  window_warn:\n    hours_before: 15\n", encoding="utf-8"
    )

    (hint,) = hint_lines(run)

    assert "对话窗口还剩不到 15 小时" in hint


def test_guarantees_that_no_longer_fit_the_window_are_listed(empty_database, run, home):
    add(run, "alpha", prefix="甲", guaranteed=5, cap=6)
    add(run, "bravo", prefix="乙", guaranteed=3, cap=4)
    (home / "config.yaml").write_text("quota:\n  window_limit: 8\n", encoding="utf-8")

    (hint,) = hint_lines(run)

    assert hint == (
        "保底合计 8 加上回复保留 2 超过窗口总条数 8（alpha 5、bravo 3）："
        "服务照常运行，只是登记和启用项目会被拒绝"
    )


def test_guarantees_that_fit_exactly_are_not_a_problem(empty_database, run, home):
    add(run, "alpha", prefix="甲", guaranteed=5, cap=5)
    add(run, "bravo", prefix="乙", guaranteed=3, cap=3)

    assert hint_lines(run) == []


def test_disabled_projects_do_not_count_towards_the_guarantee_hint(
    empty_database, run, clock, home
):
    add(run, "alpha", prefix="甲", guaranteed=5, cap=5)
    add(run, "bravo", prefix="乙", guaranteed=3, cap=3)
    Projects(empty_database, Config(), clock).disable("bravo")
    (home / "config.yaml").write_text("quota:\n  window_limit: 8\n", encoding="utf-8")

    assert hint_lines(run) == []


def test_the_reply_reserve_of_the_guarantee_hint_comes_from_the_configuration(run, home):
    home.mkdir(mode=0o700)
    (home / "config.yaml").write_text("quota:\n  reply_reserve: 1\n", encoding="utf-8")
    assert add(run, "alpha", prefix="甲", guaranteed=5, cap=5).exit_code == 0
    assert add(run, "bravo", prefix="乙", guaranteed=4, cap=4).exit_code == 0

    assert hint_lines(run) == []  # 9 加 1 刚好 10；要是按默认的 2 算就会误报


def test_a_window_that_has_run_out_has_no_expiry_hint(empty_database, run, quota, clock):
    speak(empty_database, quota, clock.now_ms() - 25 * HOUR)

    assert hint_lines(run) == []


def test_the_expiry_hint_can_be_switched_off(empty_database, run, quota, clock, home):
    speak(empty_database, quota, clock.now_ms() - 22 * HOUR)
    (home / "config.yaml").write_text(
        "alerts:\n  window_warn:\n    enabled: false\n", encoding="utf-8"
    )

    assert hint_lines(run) == []


def test_a_configuration_changed_after_the_service_started_is_a_hint(run, hub_paths, clock, home):
    hub_paths.home.mkdir(mode=0o700)
    write_run_json(hub_paths, clock, started_ms=clock.now_ms() - HOUR)
    config = hub_paths.config
    config.write_text("log:\n  level: DEBUG\n", encoding="utf-8")
    later = (clock.now_ms() - 10 * MINUTE) / 1000
    os.utime(config, (later, later))

    assert "配置文件改过，重启服务才生效" in hint_lines(run)


def test_a_configuration_older_than_the_service_is_not_a_hint(run, hub_paths, clock):
    hub_paths.home.mkdir(mode=0o700)
    write_run_json(hub_paths, clock, started_ms=clock.now_ms() - HOUR)
    hub_paths.config.write_text("log:\n  level: DEBUG\n", encoding="utf-8")
    earlier = (clock.now_ms() - 2 * HOUR) / 1000
    os.utime(hub_paths.config, (earlier, earlier))

    assert hint_lines(run) == []


def test_a_changed_configuration_means_nothing_while_the_service_is_stopped(run, hub_paths, clock):
    hub_paths.home.mkdir(mode=0o700)
    hub_paths.config.write_text("log:\n  level: DEBUG\n", encoding="utf-8")

    assert hint_lines(run) == []


def test_a_configuration_saved_in_the_very_second_the_service_started_is_not_newer(
    run, hub_paths, clock
):
    hub_paths.home.mkdir(mode=0o700)
    started = clock.now_ms() - HOUR
    write_run_json(hub_paths, clock, started_ms=started)
    hub_paths.config.write_text("log:\n  level: DEBUG\n", encoding="utf-8")
    os.utime(hub_paths.config, ns=(started * 1_000_000, started * 1_000_000))

    assert hint_lines(run) == []


def test_a_running_service_without_any_configuration_file_has_nothing_to_restart_for(
    run, hub_paths, clock
):
    hub_paths.home.mkdir(mode=0o700)
    write_run_json(hub_paths, clock)

    assert hint_lines(run) == []


def test_a_run_file_without_a_start_time_cannot_say_the_configuration_is_newer(
    run, hub_paths, clock
):
    hub_paths.home.mkdir(mode=0o700)
    beat = {"pid": os.getpid(), "last_heartbeat_ms": clock.now_ms() - 5_000}
    hub_paths.run.write_text(json.dumps(beat), encoding="utf-8")
    hub_paths.config.write_text("log:\n  level: DEBUG\n", encoding="utf-8")

    assert hint_lines(run) == []


def test_the_sections_come_in_a_fixed_order_and_the_hints_are_indented(
    empty_database, run, hub_paths, quota, clock
):
    write_run_json(hub_paths, clock)
    AccountStore(hub_paths).write(account_file())
    add(run, "alpha")
    speak(empty_database, quota, clock.now_ms() - 22 * HOUR)

    lines = run("status").stdout.splitlines()

    heads = [line.split("：")[0] for line in lines if not line.startswith(" ")]
    assert heads == ["服务", "登录", "对话窗口", "项目", "待处理", "提示"]
    assert lines[-2:] == ["提示：", f"  {EXPIRY_HINT}"]


def test_the_changed_since_start_answer_is_a_real_boolean_in_every_case(hub_paths, clock):
    hub_paths.home.mkdir(mode=0o700)
    started = clock.now_ms() - HOUR
    running = RunInfo(pid=os.getpid(), last_heartbeat_ms=clock.now_ms(), started_ms=started)
    no_start_time = RunInfo(pid=os.getpid(), last_heartbeat_ms=clock.now_ms())

    assert config_changed_since_start(hub_paths, None) is False  # 服务没在运行
    assert config_changed_since_start(hub_paths, no_start_time) is False  # 不知道什么时候启动的
    assert config_changed_since_start(hub_paths, running) is False  # 没有配置文件

    hub_paths.config.write_text("log:\n  level: DEBUG\n", encoding="utf-8")
    later = (clock.now_ms() - 10 * MINUTE) / 1000
    os.utime(hub_paths.config, (later, later))
    assert config_changed_since_start(hub_paths, running) is True
    earlier = (started - MINUTE) / 1000
    os.utime(hub_paths.config, (earlier, earlier))
    assert config_changed_since_start(hub_paths, running) is False


# ---------------------------------------------------------------- status --json


def test_the_json_status_has_the_same_content_in_a_fixed_shape(run):
    data = json_of(run("status", "--json"))

    assert list(data) == ["service", "login", "window", "projects", "pending", "hints"]
    assert data["service"] == {
        "running": False,
        "pid": None,
        "uptime_seconds": None,
        "version": None,
        "listen": None,
        "profile": None,
        "confirm": None,
    }
    assert data["login"] == {
        "state": "missing",
        "bound_at": None,
        "bot_id_tail": None,
        "since": None,
        "last_poll_ok": None,
        "last_poll_error": None,
        "last_poll_error_at": None,
        "ignored_count": 0,
    }
    assert data["window"] == {
        "state": "none",
        "started": None,
        "ends": None,
        "remaining_seconds": None,
        "limit": 10,
        "used": 0,
        "push_used": 0,
        "reply_used": 0,
        "paused": False,
        "hold_code": None,
        "hold_until": None,
    }
    assert data["projects"] == []
    assert data["pending"] == {"queued": {}, "interrupted_sends": 0, "unsent_alerts": 0}
    assert data["hints"] == []


def test_the_json_status_of_a_running_service_a_bound_login_and_an_open_window(
    empty_database, run, hub_paths, quota, clock
):
    write_run_json(hub_paths, clock)
    AccountStore(hub_paths).write(
        account_file(bot_id="bot-id-0042", bound_at="2026-10-02T09:05:00+08:00")
    )
    add(run, "alpha", guaranteed=3, cap=4)
    speak(empty_database, quota, clock.now_ms() - HOUR)
    reserve(empty_database, quota, "alpha", 2)
    with empty_database.txn() as conn:
        conn.execute(
            "UPDATE account_state SET last_poll_ok_ms = ?, last_poll_error = '连不上腾讯',"
            " last_poll_error_ms = ?, ignored_count = 4 WHERE id = 1",
            (clock.now_ms() - MINUTE, clock.now_ms() - 2 * HOUR),
        )

    data = json_of(run("status", "--json"))

    assert data["service"] == {
        "running": True,
        "pid": os.getpid(),
        "uptime_seconds": 3900,
        "version": "0.1.0",
        "listen": "127.0.0.1:8790",
        "profile": "legacy",
        "confirm": "observe",
    }
    assert data["login"]["state"] == "ok"
    assert data["login"]["bound_at"] == "2026-10-02T09:05:00+08:00"
    assert data["login"]["bot_id_tail"] == "0042"
    assert data["login"]["last_poll_ok"] == "2026-10-12T09:59:00+08:00"
    assert data["login"]["last_poll_error"] == "连不上腾讯"
    assert data["login"]["last_poll_error_at"] == "2026-10-12T08:00:00+08:00"
    assert data["login"]["ignored_count"] == 4
    assert data["window"] == {
        "state": "open",
        "started": "2026-10-12T09:00:00+08:00",
        "ends": "2026-10-13T09:00:00+08:00",
        "remaining_seconds": 23 * 3600,
        "limit": 10,
        "used": 2,
        "push_used": 2,
        "reply_used": 0,
        "paused": False,
        "hold_code": None,
        "hold_until": None,
    }
    assert [p["name"] for p in data["projects"]] == ["alpha"]
    assert data["projects"][0]["push_used"] == 2


def test_the_json_status_of_an_expired_and_of_a_paused_window(empty_database, run, quota, clock):
    add(run, "alpha")
    speak(empty_database, quota, clock.now_ms() - 25 * HOUR)
    expired = json_of(run("status", "--json"))["window"]
    speak(empty_database, quota, clock.now_ms())
    rejected = reserve(empty_database, quota, "alpha")
    settle(empty_database, quota, rejected, REJECTED, code="40001")
    paused = json_of(run("status", "--json"))["window"]

    assert expired["state"] == "closed"
    assert expired["remaining_seconds"] == 0
    assert (expired["used"], expired["push_used"], expired["reply_used"]) == (0, 0, 0)
    assert paused["state"] == "open" and paused["paused"] is True
    assert paused["hold_code"] == "40001"
    assert paused["hold_until"] == "2026-10-12T10:30:00+08:00"


def test_the_json_status_carries_the_hints(empty_database, run, quota, clock):
    speak(empty_database, quota, clock.now_ms() - 22 * HOUR)

    assert json_of(run("status", "--json"))["hints"] == [EXPIRY_HINT]


def test_the_json_output_is_readable_text_and_indented(empty_database, run):
    add(run, "alpha", prefix="量化")

    status = run("status", "--json").stdout
    listing = run("project", "list", "--json").stdout

    assert '"prefix": "量化"' in status and '"prefix": "量化"' in listing
    assert "\\u" not in status and "\\u" not in listing
    assert status.startswith('{\n  "service": {')
    assert listing.startswith('[\n  {\n    "name": "alpha"')


def test_the_json_status_has_no_credentials_and_no_keys(empty_database, run, hub_paths, upstream):
    run("login")
    (key,) = KEY.findall(add(run, "alpha").stdout)
    stored = AccountStore(hub_paths).read()

    text = run("status", "--json").output + run("status").output

    for secret in ("bot_token", "context_token", "chub_", key, stored.bot_token, stored.user_id):
        assert secret not in text


def test_a_database_that_is_too_new_is_a_clear_failure(run, hub_paths):
    assert add(run, "alpha").exit_code == 0

    with sqlite3.connect(hub_paths.db) as conn:
        conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")

    for argv in (("status",), ("project", "list"), ("project", "list", "--json")):
        result = run(*argv)
        assert result.exit_code == 1
        assert "数据库版本比程序新" in result.output and "Traceback" not in result.output
