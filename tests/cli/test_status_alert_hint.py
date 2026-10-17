"""`hubctl status` 的第一条提示：没配告警设备码（任务 D3）。

规格 `hub-alerts`「没有设备码」、`hub-cli`「看全貌」：没有配置设备码时，`status` 用警告提示
"没有配置告警，服务出问题时不会通知你"，服务照常运行。设备码配错了也一样说一声，原因照写
（原因里没有设备码本身）。提示在窗口快到期等别的提示前面。
"""

from __future__ import annotations

import json
import os

import pytest
from typer.testing import CliRunner

from clawbot_hub.bark import ENV_KEY
from clawbot_hub.cli import Env, app
from clawbot_hub.config import Config
from clawbot_hub.db import Database, migrate
from clawbot_hub.paths import ensure_home
from clawbot_hub.quota import Quota
from tests.cli.conftest import HOUR, json_of
from tests.quota.conftest import speak

NO_ALERTS = "没有配置告警，服务出问题时不会通知你：运行 hubctl alerts set-key"


@pytest.fixture
def empty_database(hub_paths, clock, display_tz):
    ensure_home(hub_paths)
    migrate(hub_paths, clock=clock, tz=display_tz)
    db = Database(hub_paths.db)
    yield db
    db.close()


@pytest.fixture
def quota(clock) -> Quota:
    return Quota(Config(), clock)


def hints_of(result) -> list[str]:
    return json_of(result)["hints"]


def test_without_a_key_the_status_says_alerts_are_not_set_up(run, monkeypatch):
    monkeypatch.delenv(ENV_KEY)

    result = run("status")

    assert result.exit_code == 0
    assert result.stdout.splitlines()[-2:] == ["提示：", f"  {NO_ALERTS}"]


def test_the_json_status_carries_it_too(run, monkeypatch):
    monkeypatch.delenv(ENV_KEY)

    assert hints_of(run("status", "--json")) == [NO_ALERTS]


def test_a_key_in_the_environment_means_no_hint(run):
    assert hints_of(run("status", "--json")) == []


def test_a_key_in_the_file_means_no_hint(run, hub_paths, monkeypatch):
    monkeypatch.delenv(ENV_KEY)
    hub_paths.home.mkdir(mode=0o700)
    hub_paths.bark.write_text(json.dumps({"key": "FILE-KEY-123456"}), encoding="utf-8")

    result = run("status", "--json")

    assert hints_of(result) == []
    assert "FILE-KEY-123456" not in result.output


def test_a_key_file_with_the_wrong_content_is_called_wrong_not_missing(run, hub_paths, monkeypatch):
    monkeypatch.delenv(ENV_KEY)
    hub_paths.home.mkdir(mode=0o700)
    hub_paths.bark.write_text("这不是 JSON，里面还有 SECRET-INSIDE", encoding="utf-8")

    result = run("status", "--json")

    [hint] = hints_of(result)
    assert hint.startswith("告警设备码配置有误，服务出问题时不会通知你：")
    assert "bark.json 的内容不对" in hint and "hubctl alerts set-key" in hint
    assert "SECRET-INSIDE" not in result.output


def test_an_environment_value_that_is_not_a_key_is_called_wrong_without_showing_it(
    run, monkeypatch
):
    monkeypatch.setenv(ENV_KEY, "ftp://nonsense.example/SECRET-IN-ENV")

    result = run("status", "--json")

    [hint] = hints_of(result)
    assert ENV_KEY in hint and "配置有误" in hint
    assert "SECRET-IN-ENV" not in result.output


def test_it_comes_before_the_other_hints(empty_database, run, quota, clock, monkeypatch):
    monkeypatch.delenv(ENV_KEY)
    speak(empty_database, quota, clock.now_ms() - 22 * HOUR)

    hints = hints_of(run("status", "--json"))

    assert hints[0] == NO_ALERTS
    assert len(hints) == 2 and "快到期" in hints[1]


def test_the_status_reads_the_environment_the_command_was_given_not_the_real_one(
    env, home, monkeypatch
):
    """`Env.environ` 是命令行碰到环境变量的口子：给了就只看它。真环境里有设备码也不算。"""
    assert ENV_KEY in os.environ  # 全体命令行测试默认配了
    bare = Env(clock=env.clock, upstream_factory=env.upstream_factory, environ={})

    result = CliRunner().invoke(app, ["status", "--json", "--home", str(home)], obj=bare)

    assert hints_of(result) == [NO_ALERTS]


def test_looking_does_not_create_the_key_file(run, hub_paths, monkeypatch):
    monkeypatch.delenv(ENV_KEY)

    run("status")

    assert not hub_paths.bark.exists()
