"""`hubctl import-credentials`（任务 D3）：命令这一层——参数、确认、输出、退出码。

导入的流程本身（先数据库后文件、窗口怎么接、探测不吃消息……）在 `tests/login/test_import.py` 测过；
这里看：不带 `--replace` 会拒绝、`--replace` 要确认（脚本里没带 `--yes` 退出 2、什么也不改）、
输出只有摘要没有凭证、服务在跑就拒绝、`--probe` 才联网。规格 `hub-login`「导入已有的凭证」、
`hub-cli`「公共约定」。
"""

from __future__ import annotations

import json

import pytest

from clawbot_hub.account import AccountStore
from clawbot_hub.upstream.base import PollFailed, SessionExpired
from tests.cli.conftest import HOUR, mode_of, write_run_json
from tests.support.fake_upstream import GET_UPDATES

TOKEN = "tok-import-secret-9"
BUF = "progress-secret-xyz"


@pytest.fixture
def source(tmp_path, clock):
    """一份 ashare-quant 的 `clawbot.json`：3 小时前使用者说过话，窗口里用掉 4 条。"""
    path = tmp_path / "clawbot.json"
    path.write_text(
        json.dumps(
            {
                "bot_token": TOKEN,
                "base_url": "https://ilink.example",
                "bot_id": "bot_abcd1234",
                "user_id": "owner-id-5678@im.wechat",
                "bound_at": "2026-10-01T09:00:00+08:00",
                "updates_buf": BUF,
                "context_token": "ctx-secret-1",
                "context_at_ms": clock.now_ms() - 3 * HOUR,
                "sent": 4,
            }
        ),
        encoding="utf-8",
    )
    return path


def stored(hub_paths):
    return AccountStore(hub_paths).read()


# ---------------------------------------------------------------- 成功


def test_importing_writes_the_account_and_prints_a_summary_without_credentials(
    run, source, hub_paths
):
    result = run("import-credentials", str(source))

    assert result.exit_code == 0, result.output
    assert stored(hub_paths).bot_token == TOKEN
    assert mode_of(hub_paths.account) == 0o600
    out = result.output
    assert "1234" in out and "窗口" in out and "已用 4 条" in out  # 摘要
    for secret in (TOKEN, BUF, "ctx-secret-1", "owner-id-5678", "abcd"):
        assert secret not in out, secret


def test_the_imported_window_is_the_current_one_with_the_used_count(run, source):
    run("import-credentials", str(source))

    status = json.loads(run("status", "--json").stdout)

    window = status["window"]
    assert window["state"] == "open"
    assert (window["push_used"], window["reply_used"]) == (4, 0)
    assert 20 * 3600 < window["remaining_seconds"] <= 21 * 3600


def test_importing_the_same_file_twice_gives_the_same_result(run, source, hub_paths):
    assert run("import-credentials", str(source)).exit_code == 0

    again = run("import-credentials", str(source), "--replace", "--yes")

    assert again.exit_code == 0, again.output
    window = json.loads(run("status", "--json").stdout)["window"]
    assert window["push_used"] == 4  # 不累加


# ---------------------------------------------------------------- 拒绝


def test_an_existing_account_is_refused_without_replace(run, source, hub_paths):
    run("import-credentials", str(source))
    before = hub_paths.account.read_bytes()

    result = run("import-credentials", str(source))

    assert result.exit_code == 1
    assert "--replace" in result.output
    assert hub_paths.account.read_bytes() == before


def test_a_running_service_is_refused_and_nothing_changes(run, source, hub_paths, clock):
    write_run_json(hub_paths, clock)

    result = run("import-credentials", str(source))

    assert result.exit_code == 1
    assert "先停服务" in result.output
    assert not hub_paths.account.exists()


@pytest.mark.parametrize("missing", ["bot_token", "user_id"])
def test_a_file_without_a_required_field_says_which_and_writes_nothing(
    run, source, hub_paths, missing
):
    data = json.loads(source.read_text(encoding="utf-8"))
    del data[missing]
    source.write_text(json.dumps(data), encoding="utf-8")

    result = run("import-credentials", str(source))

    assert result.exit_code == 1
    assert missing in result.output
    assert not hub_paths.account.exists()
    assert TOKEN not in result.output  # 输出里没有文件内容


def test_a_file_that_is_not_json_does_not_leak_what_is_in_it(run, tmp_path, hub_paths):
    broken = tmp_path / "broken.json"
    broken.write_text('{"bot_token": "' + TOKEN + '", 这里坏了', encoding="utf-8")

    result = run("import-credentials", str(broken))

    assert result.exit_code == 1
    assert TOKEN not in result.output
    assert not hub_paths.account.exists()


def test_a_file_that_is_not_there_is_one_sentence(run, tmp_path):
    result = run("import-credentials", str(tmp_path / "nowhere.json"))

    assert result.exit_code == 1
    assert "Traceback" not in result.output


def test_the_file_argument_is_required(run):
    assert run("import-credentials").exit_code == 2


# ---------------------------------------------------------------- --replace 要确认


def test_replace_without_yes_outside_a_terminal_is_refused_with_two_and_changes_nothing(
    run, source, hub_paths
):
    run("import-credentials", str(source))
    before = hub_paths.account.read_bytes()
    data = json.loads(source.read_text(encoding="utf-8"))
    data["bot_token"] = "another-token"
    source.write_text(json.dumps(data), encoding="utf-8")

    result = run("import-credentials", str(source), "--replace")

    assert result.exit_code == 2
    assert "--yes" in result.output
    assert hub_paths.account.read_bytes() == before
    assert not list(hub_paths.home.glob("account.json.bak-*"))


def test_replace_with_yes_backs_up_the_old_file_privately(run, source, hub_paths):
    run("import-credentials", str(source))
    old = hub_paths.account.read_bytes()
    data = json.loads(source.read_text(encoding="utf-8"))
    data["bot_token"] = "another-token"
    source.write_text(json.dumps(data), encoding="utf-8")

    result = run("import-credentials", str(source), "--replace", "--yes")

    assert result.exit_code == 0, result.output
    assert stored(hub_paths).bot_token == "another-token"
    [backup] = hub_paths.home.glob("account.json.bak-*")
    assert backup.read_bytes() == old and mode_of(backup) == 0o600
    assert backup.name in result.output
    assert "another-token" not in result.output


def test_replace_in_a_terminal_asks_and_a_yes_goes_ahead(run, source, hub_paths, terminal):
    run("import-credentials", str(source))
    terminal["on"] = True

    result = run("import-credentials", str(source), "--replace", input="y\n")

    assert result.exit_code == 0, result.output
    assert "确认吗" in result.output and "备份" in result.output


def test_replace_in_a_terminal_a_no_changes_nothing(run, source, hub_paths, terminal):
    run("import-credentials", str(source))
    before = hub_paths.account.read_bytes()
    terminal["on"] = True

    result = run("import-credentials", str(source), "--replace", input="n\n")

    assert result.exit_code == 1
    assert "已取消" in result.output
    assert hub_paths.account.read_bytes() == before


def test_replace_with_nothing_to_replace_needs_no_question(run, source, hub_paths):
    result = run("import-credentials", str(source), "--replace")  # 不是终端，也没带 --yes

    assert result.exit_code == 0, result.output
    assert stored(hub_paths).bot_token == TOKEN


# ---------------------------------------------------------------- --probe


def test_by_default_the_network_is_not_touched(run, source, upstream):
    run("import-credentials", str(source))

    assert upstream.calls == []


def test_probe_reports_valid_credentials_without_keeping_what_came_back(
    run, source, upstream, hub_paths
):
    upstream.push_user_message("这条不能被吃掉")
    config = hub_paths.home
    config.mkdir(exist_ok=True)
    (config / "config.yaml").write_text("upstream:\n  poll_timeout: 12\n", encoding="utf-8")

    result = run("import-credentials", str(source), "--probe")

    assert result.exit_code == 0, result.output
    assert "凭证有效" in result.output
    [call] = upstream.calls_to(GET_UPDATES)
    assert call.args["acct"].bot_token == TOKEN and call.args["buf"] == BUF
    assert call.args["timeout"] == 12.0  # 取配置里收消息的时限
    assert stored(hub_paths).updates_buf == BUF
    assert "这条不能被吃掉" not in result.output


def test_probe_that_gets_minus_14_says_so_but_the_import_stands(run, source, upstream, hub_paths):
    upstream.script(GET_UPDATES, SessionExpired())

    result = run("import-credentials", str(source), "--probe")

    assert result.exit_code == 0, result.output
    assert "失效" in result.output and "hubctl login" in result.output
    assert stored(hub_paths) is not None


def test_probe_that_cannot_tell_says_why(run, source, upstream):
    upstream.script(GET_UPDATES, PollFailed("HTTP 502", completed=False))

    result = run("import-credentials", str(source), "--probe")

    assert result.exit_code == 0, result.output
    assert "没能确认" in result.output and "HTTP 502" in result.output
