"""`hubctl alerts set-key / test`、`config show`、`version`（任务 D3）。

告警命令用真的 `BarkClient` 打本机回环上的假 Bark 服务（设备码写成"整个推送地址"的样子，服务器
地址就是假服务的）。设备码在任何输出、事件里都不能出现；`bark.json` 权限 0600。规格 `hub-alerts`
「告警渠道」、`hub-cli`「命令一览」。
"""

from __future__ import annotations

import json

import pytest

import clawbot_hub
from clawbot_hub.alerts import last_bark_outcome
from clawbot_hub.bark import ENV_KEY
from clawbot_hub.config import Config
from tests.cli.conftest import HOUR, mode_of, write_run_json
from tests.support.fake_bark import FakeBark, bark_ok
from tests.support.fakehttp import http_status

DEVICE = "SecretDeviceKey0123456789"


@pytest.fixture
def bark():
    with FakeBark() as server:
        yield server


@pytest.fixture
def no_env_key(monkeypatch):
    """去掉环境变量里的设备码（命令行测试默认有一个），让 `bark.json` 说了算。"""
    monkeypatch.delenv(ENV_KEY)


def pushed_key(bark: FakeBark, device: str = DEVICE) -> str:
    """ "整个推送地址"的写法：服务器地址就是假 Bark 的。"""
    return f"{bark.base_url}/{device}"


def set_key(run, value: str, **kwargs):
    return run("alerts", "set-key", input=value + "\n", **kwargs)


def event_lines(run, kind: str = "alert") -> list[str]:
    return run("events", "--kind", kind).stdout.splitlines()


# ---------------------------------------------------------------- alerts set-key


def test_set_key_reads_the_key_from_standard_input_and_keeps_it_private(run, hub_paths, no_env_key):
    result = set_key(run, DEVICE)

    assert result.exit_code == 0, result.output
    assert json.loads(hub_paths.bark.read_text(encoding="utf-8")) == {"key": DEVICE}
    assert mode_of(hub_paths.bark) == 0o600
    assert DEVICE not in result.output
    assert "hubctl alerts test" in result.stdout  # 下一步做什么
    assert not hub_paths.db.exists()  # 只是存个文件，不建库


def test_set_key_accepts_the_whole_push_address_from_the_app(run, hub_paths, no_env_key):
    address = f"https://bark.example.net/{DEVICE}/这里是标题"

    result = set_key(run, address)

    assert result.exit_code == 0, result.output
    assert json.loads(hub_paths.bark.read_text(encoding="utf-8")) == {"key": address}
    assert DEVICE not in result.output and "bark.example.net" not in result.output


@pytest.mark.parametrize("typed", [f"  {DEVICE}  \n", f"{DEVICE}\r\n", f"{DEVICE}\n后面还有一行\n"])
def test_only_the_first_line_counts_and_spaces_are_dropped(run, hub_paths, no_env_key, typed):
    result = run("alerts", "set-key", input=typed)

    assert result.exit_code == 0, result.output
    assert json.loads(hub_paths.bark.read_text(encoding="utf-8")) == {"key": DEVICE}


@pytest.mark.parametrize("typed", ["", "\n", "   \n"])
def test_nothing_typed_is_a_failure_and_changes_nothing(run, hub_paths, no_env_key, typed):
    set_key(run, "OLD-KEY")

    result = run("alerts", "set-key", input=typed)

    assert result.exit_code == 1
    assert "没有读到设备码" in result.output
    assert json.loads(hub_paths.bark.read_text(encoding="utf-8")) == {"key": "OLD-KEY"}


def test_an_address_without_a_device_key_is_refused_without_echoing_it(run, hub_paths, no_env_key):
    set_key(run, "OLD-KEY")

    result = set_key(run, "ftp://secret-host.example/TOKEN-ABC")

    assert result.exit_code == 1
    assert "看不出设备码" in result.output
    assert "secret-host" not in result.output and "TOKEN-ABC" not in result.output
    assert json.loads(hub_paths.bark.read_text(encoding="utf-8")) == {"key": "OLD-KEY"}


def test_a_new_key_replaces_the_old_one_without_asking_and_loosens_nothing(
    run, hub_paths, no_env_key
):
    hub_paths.home.mkdir()
    hub_paths.bark.write_text('{"key": "OLD-KEY"}', encoding="utf-8")
    hub_paths.bark.chmod(0o644)

    result = set_key(run, DEVICE)  # 不是终端、没带 --yes 也不拒绝

    assert result.exit_code == 0, result.output
    assert json.loads(hub_paths.bark.read_text(encoding="utf-8")) == {"key": DEVICE}
    assert mode_of(hub_paths.bark) == 0o600


def test_in_a_terminal_the_key_is_asked_for_without_echo(run, hub_paths, no_env_key, terminal):
    terminal["on"] = True

    result = set_key(run, DEVICE)

    assert result.exit_code == 0, result.output
    assert json.loads(hub_paths.bark.read_text(encoding="utf-8")) == {"key": DEVICE}
    assert DEVICE not in result.output  # 提示语之后输入的字不回显


def test_an_environment_key_that_wins_over_the_file_is_pointed_out(run, hub_paths, monkeypatch):
    monkeypatch.setenv(ENV_KEY, "FROM-ENVIRONMENT")

    result = set_key(run, DEVICE)

    assert result.exit_code == 0, result.output
    assert json.loads(hub_paths.bark.read_text(encoding="utf-8")) == {"key": DEVICE}
    assert ENV_KEY in result.stdout and "优先" in result.stdout
    assert "FROM-ENVIRONMENT" not in result.output and DEVICE not in result.output


# ---------------------------------------------------------------- alerts test


def test_a_test_alert_reaches_bark_and_the_command_says_so(run, bark, no_env_key, hub_paths):
    set_key(run, pushed_key(bark))

    result = run("alerts", "test")

    assert result.exit_code == 0, result.output
    assert "测试告警已送到" in result.stdout
    [push] = bark.pushes
    assert push.device_key == DEVICE and "测试" in push.title
    assert push.group == "clawbot-hub"
    assert DEVICE not in result.output
    assert any("Bark 测试成功" in line for line in event_lines(run))


def test_the_group_comes_from_the_config(run, bark, no_env_key, home):
    home.mkdir()
    home.joinpath("config.yaml").write_text("alerts:\n  group: my-hub\n", encoding="utf-8")
    set_key(run, pushed_key(bark))

    assert run("alerts", "test").exit_code == 0

    assert bark.pushes[0].group == "my-hub"


def test_the_environment_key_is_used_and_wins_over_the_file(run, bark, monkeypatch, hub_paths):
    set_key(run, "FILE-KEY-NOT-USED")
    monkeypatch.setenv(ENV_KEY, pushed_key(bark, "ENV-KEY"))

    result = run("alerts", "test")

    assert result.exit_code == 0, result.output
    assert [p.device_key for p in bark.pushes] == ["ENV-KEY"]


def test_a_refused_test_alert_fails_and_points_at_the_key(run, bark, no_env_key, hub_paths):
    """真的 Bark 会把收到的设备码回显在错误信息里：输出和事件里都不能出现它。"""
    bark.valid_keys = {"SOMEBODY-ELSE"}
    set_key(run, pushed_key(bark))

    result = run("alerts", "test")

    assert result.exit_code == 1
    assert "被 Bark 拒绝" in result.output and "hubctl alerts set-key" in result.output
    assert DEVICE not in result.output
    assert len(bark.pushes) == 1  # 测试告警只发一次，结果直接告诉你
    [event] = [line for line in event_lines(run) if "Bark 测试被拒绝" in line]
    assert DEVICE not in event and "***" in event


def test_a_test_alert_that_cannot_get_through_fails_with_the_reason(run, bark, no_env_key):
    bark.script("push", http_status(500))
    set_key(run, pushed_key(bark))

    result = run("alerts", "test")

    assert result.exit_code == 1
    assert "没送到" in result.output and "HTTP 500" in result.output
    assert len(bark.pushes) == 1
    assert any("Bark 测试没送到：HTTP 500" in line for line in event_lines(run))


def test_a_bark_server_that_is_not_there_is_reported_as_not_getting_through(run, bark, no_env_key):
    address = pushed_key(bark)
    bark.stop()
    set_key(run, address)

    result = run("alerts", "test")

    assert result.exit_code == 1
    assert "没送到" in result.output
    assert DEVICE not in result.output


def test_without_a_key_it_says_what_to_do_and_sends_nothing(run, bark, no_env_key):
    result = run("alerts", "test")

    assert result.exit_code == 1
    assert "没有配置设备码" in result.output and "hubctl alerts set-key" in result.output
    assert bark.pushes == []


def test_a_broken_key_file_is_explained(run, bark, no_env_key, hub_paths):
    hub_paths.home.mkdir(exist_ok=True)
    hub_paths.bark.write_text("这不是 JSON", encoding="utf-8")

    result = run("alerts", "test")

    assert result.exit_code == 1
    assert "bark.json 的内容不对" in result.output
    assert bark.pushes == []


def test_the_last_outcome_follows_the_latest_answer_from_bark(run, bark, no_env_key, database):
    bark.script("push", http_status(400, {"code": 400, "message": "bad"}), bark_ok())
    set_key(run, pushed_key(bark))

    run("alerts", "test")
    assert last_bark_outcome(database.conn()) == "refused"

    run("alerts", "test")
    assert last_bark_outcome(database.conn()) == "ok"  # 测试成功也算一次成功


# ---------------------------------------------------------------- config show


def shown(result) -> dict[str, str]:
    """`键: 值` 的行，取成字典（只认键里有点号的）。"""
    out = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition(": ")
        if sep and "." in key and " " not in key:
            out[key] = value
    return out


def test_every_setting_is_shown_with_its_default(run, no_env_key):
    result = run("config", "show")

    assert result.exit_code == 0, result.output
    lines = shown(result)
    assert set(lines) == set(Config().flat())
    assert lines["server.port"] == "8790" and lines["server.lan"] == "false"
    assert lines["upstream.base_url"] == "https://ilinkai.weixin.qq.com"
    assert lines["routing.confirm_words"] == "[对, 是, 确认]"
    assert lines["alerts.window_warn.weekdays"] == "[1, 2, 3, 4, 5]"
    assert lines["alerts.window_warn.between"] == "07:00-22:30"
    assert lines["send.message_id_keys"] == "[message_id, msg_id]"


def test_values_from_the_file_replace_the_defaults(run, home):
    home.mkdir()
    home.joinpath("config.yaml").write_text(
        "send:\n  part_chars: 1500\nrouting:\n  confirm_words: [好, 行]\n"
        "alerts:\n  window_warn:\n    enabled: false\n",
        encoding="utf-8",
    )

    lines = shown(run("config", "show"))

    assert lines["send.part_chars"] == "1500"
    assert lines["routing.confirm_words"] == "[好, 行]"
    assert lines["alerts.window_warn.enabled"] == "false"
    assert lines["send.max_chars"] == "8000"  # 没写的还是默认值


def test_the_first_line_says_which_file_was_read_or_that_there_is_none(run, home, tmp_path):
    missing = run("config", "show")
    assert missing.stdout.splitlines()[0].startswith("配置文件：")
    assert "没有这个文件" in missing.stdout.splitlines()[0] and "默认值" in missing.stdout

    custom = tmp_path / "elsewhere.yaml"
    custom.write_text("send:\n  retries: 0\n", encoding="utf-8")
    result = run("config", "show", "--config", str(custom))
    assert str(custom) in result.stdout.splitlines()[0]
    assert "没有这个文件" not in result.stdout.splitlines()[0]
    assert shown(result)["send.retries"] == "0"


def test_a_bad_file_fails_with_every_problem_listed(run, home):
    home.mkdir()
    home.joinpath("config.yaml").write_text(
        "server:\n  port: 99999\nsend:\n  retries: -1\n", encoding="utf-8"
    )

    result = run("config", "show")

    assert result.exit_code == 1
    assert "server.port" in result.output and "send.retries" in result.output


def test_a_config_file_that_is_not_there_is_an_error_when_named(run, tmp_path):
    result = run("config", "show", "--config", str(tmp_path / "typo.yaml"))

    assert result.exit_code == 1 and "typo.yaml" in result.output


@pytest.mark.parametrize(
    ("environment", "file", "expected"),
    [
        (None, None, "设备码：未配置"),
        ("ENV-" + DEVICE, None, "设备码：已配置（环境变量）"),
        (None, "FILE-" + DEVICE, "设备码：已配置（bark.json）"),
        ("ENV-" + DEVICE, "FILE-" + DEVICE, "设备码：已配置（环境变量）"),  # 两处都有：环境变量优先
    ],
)
def test_the_alert_line_says_where_the_key_comes_from_and_never_the_key(
    run, hub_paths, monkeypatch, environment, file, expected
):
    monkeypatch.delenv(ENV_KEY)
    if environment:
        monkeypatch.setenv(ENV_KEY, environment)
    if file:
        hub_paths.home.mkdir(exist_ok=True)
        hub_paths.bark.write_text(json.dumps({"key": file}), encoding="utf-8")

    result = run("config", "show")

    assert expected in result.stdout
    assert DEVICE not in result.output


def test_a_key_that_is_configured_wrongly_is_called_wrong(run, hub_paths, no_env_key):
    hub_paths.home.mkdir(exist_ok=True)
    hub_paths.bark.write_text("这不是 JSON", encoding="utf-8")

    result = run("config", "show")

    assert result.exit_code == 0
    assert "设备码：配置有误" in result.stdout and "bark.json 的内容不对" in result.stdout


def test_a_config_changed_after_the_service_started_is_pointed_out(run, home, hub_paths, clock):
    home.mkdir()
    home.joinpath("config.yaml").write_text("send:\n  retries: 1\n", encoding="utf-8")
    started_ms = int(hub_paths.config.stat().st_mtime * 1000) - HOUR
    write_run_json(hub_paths, clock, started_ms=started_ms)

    result = run("config", "show")

    assert result.stdout.splitlines()[-1] == "配置文件改过，重启服务才生效"


def test_no_such_hint_when_the_service_started_after_the_change_or_is_not_running(
    run, home, hub_paths, clock
):
    home.mkdir()
    home.joinpath("config.yaml").write_text("send:\n  retries: 1\n", encoding="utf-8")
    changed_ms = int(hub_paths.config.stat().st_mtime * 1000)

    write_run_json(hub_paths, clock, started_ms=changed_ms + HOUR)
    assert "重启服务才生效" not in run("config", "show").stdout

    write_run_json(hub_paths, clock, started_ms=changed_ms - HOUR, pid=999_999_999)
    assert "重启服务才生效" not in run("config", "show").stdout  # 服务没在运行


def test_showing_the_config_creates_nothing_but_the_directory(run, home):
    assert run("config", "show").exit_code == 0

    assert list(home.iterdir()) == []


# ---------------------------------------------------------------- version


def test_version_shows_the_program_the_api_and_the_database(run, database):
    result = run("version")

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == f"hubctl {clawbot_hub.__version__}"
    assert lines[1] == f"接口版本：{clawbot_hub.API_VERSION}" and clawbot_hub.API_VERSION == "1"
    assert lines[2] == "数据库版本：程序需要 1，库文件里是 1"


def test_version_without_a_database_file_says_so_and_creates_none(run, hub_paths):
    result = run("version")

    assert result.exit_code == 0
    assert "数据库版本：程序需要 1，还没有库文件" in result.stdout
    assert not hub_paths.db.exists()


def test_version_reports_a_database_that_is_newer_than_the_program(run, database, hub_paths):
    with database.txn() as conn:
        conn.execute("UPDATE meta SET value = '9' WHERE key = 'schema_version'")

    result = run("version")

    assert result.exit_code == 0, result.output
    assert "库文件里是 9（比程序新，程序不会动它）" in result.stdout


def test_version_reports_a_file_that_is_not_the_hub_database(run, hub_paths):
    hub_paths.home.mkdir(exist_ok=True)
    hub_paths.db.write_bytes(b"this is not a database at all" * 20)

    result = run("version")

    assert result.exit_code == 0
    assert "库文件读不懂" in result.stdout
