"""`hubctl doctor`（任务 D3）：逐项检查，每项一行 ✓ / ⚠ / ✗，最后一行汇总；有 ✗ 退出 1。

先造一份"什么毛病都没有"的数据目录（`healthy`），再一次弄坏一处，看对应的那一行变成什么、
别的行有没有跟着变。端口用本机上空着的随机端口（真机上 8790 可能正被真服务占着）。
规格 `hub-cli`「查毛病」。
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import socket
import sqlite3
import time
from collections import namedtuple

import pytest
import yaml

from clawbot_hub import doctor as doctor_module
from clawbot_hub.account import AccountStore
from clawbot_hub.bark import ENV_KEY
from clawbot_hub.loginstate import write_scan_marker
from tests.cli.conftest import HOUR, MINUTE, add, mode_of, write_run_json
from tests.login.conftest import account_file, dead_pid
from tests.support import fake_ilink as ilink
from tests.support.fake_bark import FakeBark
from tests.support.fakehttp import hang, http_status
from tests.support.wal import leave_log_behind, wal_of

ROW = re.compile(r"^(✓|⚠|✗) ([^：]+)：(.*)$")
NAMES = [
    "数据目录",
    "权限",
    "登录凭证",
    "登录状态",
    "数据库",
    "服务",
    "端口",
    "配置",
    "保底合计",
    "前缀冲突",
    "告警设备码",
    "Bark 结果",
    "配置警告",
    "扫码标记",
    "磁盘空间",
    "自动备份",
]
Usage = namedtuple("Usage", "total used free")
GB = 1024**3
MB = 1024**2


def rows(result) -> dict[str, tuple[str, str]]:
    """输出里每一项：名字 → (图标, 说明)。"""
    found = {}
    for line in result.stdout.splitlines():
        match = ROW.match(line)
        if match:
            found[match[2]] = (match[1], match[3])
    return found


def icons(result) -> dict[str, str]:
    return {name: icon for name, (icon, _) in rows(result).items()}


def warnings(result) -> set[str]:
    return {name for name, icon in icons(result).items() if icon == "⚠"}


def errors(result) -> set[str]:
    return {name for name, icon in icons(result).items() if icon == "✗"}


@pytest.fixture
def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def disk(monkeypatch):
    """假的"磁盘还剩多少"：测试改 `disk["free"]`。"""
    state = {"free": 10 * GB}
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _path: Usage(100 * GB, 100 * GB - state["free"], state["free"])
    )
    return state


def set_config(hub_paths, port: int, **sections) -> None:
    """写 `config.yaml`：`server.port` 用给定的端口，别的节照写（`server=` 里的项并进去）。"""
    sections["server"] = {"port": port, **sections.get("server", {})}
    hub_paths.home.mkdir(mode=0o700, exist_ok=True)
    hub_paths.config.write_text(yaml.safe_dump(sections, allow_unicode=True), encoding="utf-8")
    hub_paths.config.chmod(0o600)


def make_auto_backup(hub_paths, stamp: str) -> None:
    folder = hub_paths.backups / f"auto-{stamp}"
    folder.mkdir(parents=True, mode=0o700)
    for directory in (hub_paths.backups, folder):
        directory.chmod(0o700)


@pytest.fixture
def healthy(run, database, hub_paths, clock, free_port, disk):
    """什么毛病都没有：已绑定、登录正常、服务在运行、配置读得出来、昨天做过自动备份。"""
    account = account_file()
    AccountStore(hub_paths).write(account)
    with database.txn() as conn:
        conn.execute(
            "UPDATE account_state SET login_state = 'ok', login_since_ms = ?, creds_digest = ?"
            " WHERE id = 1",
            (clock.now_ms() - 5 * HOUR, account.digest),
        )
    set_config(hub_paths, free_port)
    write_run_json(hub_paths, clock)
    make_auto_backup(hub_paths, "20261011-040000")  # 假时钟现在是 2026-10-12 10:00
    return hub_paths


# ---------------------------------------------------------------- 全都好的时候


def test_a_healthy_house_passes_every_item_in_a_fixed_order(run, healthy):
    result = run("doctor")

    assert result.exit_code == 0, result.output
    assert list(rows(result)) == NAMES
    assert set(icons(result).values()) == {"✓"}


def test_called_directly_it_only_looks_and_leaves_out_the_online_visit(
    healthy, hub_paths, clock, fake_ilink
):
    """`diagnose` 的默认值：不改权限、不连腾讯、数据目录当作原来就有的。"""
    hub_paths.account.chmod(0o644)

    findings = {f.name: f for f in doctor_module.diagnose(hub_paths, clock=clock)}

    assert findings["权限"].level == doctor_module.ERROR  # 没有被 --fix 改回去
    assert mode_of(hub_paths.account) == 0o644
    assert findings["数据目录"].level == doctor_module.OK
    assert "腾讯连通" not in findings and fake_ilink.requests == []


def test_the_last_line_counts_what_was_found(run, healthy):
    result = run("doctor")

    assert result.stdout.splitlines()[-1] == f"汇总：{len(NAMES)} 项通过，0 个警告，0 个错误"


def test_the_summary_counts_warnings_and_errors_separately(run, healthy, hub_paths):
    hub_paths.account.chmod(0o644)  # 一个 ✗
    hub_paths.run.unlink()  # 服务没在运行：一个 ⚠，端口也要查了

    result = run("doctor")

    assert result.stdout.splitlines()[-1] == f"汇总：{len(NAMES) - 2} 项通过，1 个警告，1 个错误"


def test_doctor_changes_nothing_and_asks_nothing(run, healthy, hub_paths):
    before = sorted((p.name, mode_of(p)) for p in hub_paths.home.iterdir())

    run("doctor")

    assert sorted((p.name, mode_of(p)) for p in hub_paths.home.iterdir()) == before


# ---------------------------------------------------------------- 退出码


def test_only_warnings_exit_zero(run, healthy, hub_paths, monkeypatch):
    monkeypatch.delenv(ENV_KEY)  # 没配告警设备码
    hub_paths.run.unlink()  # 服务没在运行

    result = run("doctor")

    assert warnings(result) == {"服务", "告警设备码"}
    assert errors(result) == set()
    assert result.exit_code == 0


def test_any_error_exits_one(run, healthy, hub_paths):
    hub_paths.account.chmod(0o644)

    assert run("doctor").exit_code == 1


# ---------------------------------------------------------------- 数据目录和权限


def test_a_directory_made_by_this_very_run_is_a_warning_and_the_rest_goes_on(
    run, home, tmp_path, free_port, disk
):
    config = tmp_path / "elsewhere.yaml"
    config.write_text(f"server:\n  port: {free_port}\n", encoding="utf-8")
    assert not home.exists()

    result = run("doctor", "--config", str(config))

    assert home.is_dir() and mode_of(home) == 0o700
    icon, detail = rows(result)["数据目录"]
    assert icon == "⚠" and "刚建" in detail and "--home" in detail
    assert len(rows(result)) >= 8  # 其他检查照常进行
    assert result.exit_code == 0


def test_an_existing_directory_is_not_called_new(run, healthy):
    assert rows(run("doctor"))["数据目录"][0] == "✓"


def test_a_fresh_directory_has_exactly_the_warnings_the_install_guide_lists(
    run, home, tmp_path, free_port, disk, monkeypatch
):
    """安装说明第 4 步说：刚建的目录上应该没有 ✗，只有这几个 ⚠。"""
    monkeypatch.delenv(ENV_KEY)
    config = tmp_path / "elsewhere.yaml"
    config.write_text(f"server:\n  port: {free_port}\n", encoding="utf-8")

    result = run("doctor", "--config", str(config))

    assert warnings(result) == {"数据目录", "登录凭证", "服务", "告警设备码"}
    assert errors(result) == set()
    assert result.exit_code == 0


@pytest.mark.parametrize("name", ["account.json", "bark.json", "hub.db", "config.yaml"])
def test_a_file_that_others_can_read_is_an_error_with_the_way_out(run, healthy, hub_paths, name):
    hub_paths.bark.write_text('{"key": "x"}', encoding="utf-8")
    (hub_paths.home / name).chmod(0o644)

    result = run("doctor")

    icon, detail = rows(result)["权限"]
    assert icon == "✗" and result.exit_code == 1
    assert name in detail and "0644" in detail
    assert "chmod 600" in detail and "--fix" in detail


def test_a_directory_that_others_can_enter_is_an_error_too(run, healthy, hub_paths):
    hub_paths.home.chmod(0o755)

    icon, detail = rows(run("doctor"))["权限"]

    assert icon == "✗" and "chmod 700" in detail and "0755" in detail


def test_fix_tightens_them_and_says_which(run, healthy, hub_paths):
    hub_paths.account.chmod(0o644)
    hub_paths.home.chmod(0o755)

    result = run("doctor", "--fix")

    icon, detail = rows(result)["权限"]
    assert icon == "✓" and "account.json" in detail and "0644" in detail and "0600" in detail
    assert mode_of(hub_paths.account) == 0o600 and mode_of(hub_paths.home) == 0o700
    assert result.exit_code == 0
    assert rows(run("doctor"))["权限"][0] == "✓"  # 再跑一遍也是好的


def test_fix_touches_nothing_else(run, healthy, hub_paths, monkeypatch):
    monkeypatch.delenv(ENV_KEY)
    hub_paths.account.write_text("这不是 JSON", encoding="utf-8")  # 另一个毛病：fix 不管它
    before = hub_paths.account.read_bytes()

    result = run("doctor", "--fix")

    assert hub_paths.account.read_bytes() == before
    assert rows(result)["登录凭证"][0] == "✗" and rows(result)["告警设备码"][0] == "⚠"


def test_stricter_modes_are_fine(run, healthy, hub_paths):
    hub_paths.account.chmod(0o400)

    assert rows(run("doctor"))["权限"][0] == "✓"


# ---------------------------------------------------------------- 登录


def test_not_bound_yet_is_a_warning_that_says_what_to_do(run, healthy, hub_paths):
    hub_paths.account.unlink()

    icon, detail = rows(run("doctor"))["登录凭证"]

    assert icon == "⚠" and "还没绑定" in detail and "hubctl login" in detail


@pytest.mark.parametrize(
    "content", ["这不是 JSON", '{"bot_token": "tok-secret-1"}', "[]", '{"user_id": 5}'], ids=str
)
def test_an_account_file_that_cannot_be_read_is_an_error_without_its_content(
    run, healthy, hub_paths, content
):
    hub_paths.account.write_text(content, encoding="utf-8")

    result = run("doctor")

    icon, detail = rows(result)["登录凭证"]
    assert icon == "✗" and "读不出来" in detail and result.exit_code == 1
    assert "tok-secret-1" not in result.output


def test_a_login_the_service_found_expired_is_a_warning(run, healthy, database, clock):
    with database.txn() as conn:
        conn.execute(
            "UPDATE account_state SET login_state = 'expired', login_since_ms = ? WHERE id = 1",
            (clock.now_ms() - 2 * HOUR,),
        )

    result = run("doctor")

    icon, detail = rows(result)["登录状态"]
    assert icon == "⚠" and "失效" in detail and "hubctl login" in detail
    assert result.exit_code == 0


# ---------------------------------------------------------------- 数据库


def test_a_database_that_is_not_one_is_an_error(run, healthy, hub_paths):
    hub_paths.db.write_bytes(b"this is not a database at all" * 50)

    result = run("doctor")

    assert rows(result)["数据库"][0] == "✗" and result.exit_code == 1
    assert "登录状态" not in rows(result)  # 库打不开，依赖库的几项不查


def test_a_database_newer_than_the_program_is_an_error(run, healthy, database):
    with database.txn() as conn:
        conn.execute("UPDATE meta SET value = '9' WHERE key = 'schema_version'")

    icon, detail = rows(run("doctor"))["数据库"]

    assert icon == "✗" and "9" in detail and "新" in detail


def test_a_damaged_database_is_an_error(run, healthy, hub_paths, database):
    database.close()
    with sqlite3.connect(hub_paths.db) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    with open(hub_paths.db, "r+b") as handle:  # 把第一个数据页之后的一段盖掉
        handle.seek(4096 * 2)
        handle.write(b"\xff" * 4096)

    assert rows(run("doctor"))["数据库"][0] == "✗"


def test_no_database_yet_is_fine_and_dependent_items_are_left_out(run, healthy, hub_paths):
    for suffix in ("", "-wal", "-shm"):
        hub_paths.db.with_name(hub_paths.db.name + suffix).unlink(missing_ok=True)

    result = run("doctor")

    assert rows(result)["数据库"][0] == "✓" and "还没有" in rows(result)["数据库"][1]
    for name in ("登录状态", "保底合计", "前缀冲突", "Bark 结果"):
        assert name not in rows(result)
    assert rows(result)["自动备份"][0] == "✓" and "没有什么可备份" in rows(result)["自动备份"][1]
    assert not hub_paths.db.exists()  # 检查不建库
    assert result.exit_code == 0


def test_doctor_does_not_touch_the_database_or_its_log(run, healthy, hub_paths):
    log = hub_paths.db.with_name(hub_paths.db.name + "-wal")
    before = (hub_paths.db.read_bytes(), log.read_bytes())
    assert before[1]  # 写过的库：日志里还有没合并回去的内容

    run("doctor")

    assert (hub_paths.db.read_bytes(), log.read_bytes()) == before


def test_doctor_leaves_alone_a_log_that_a_killed_service_left_behind(
    run, healthy, hub_paths, database
):
    """服务被杀掉、日志没合并、没人开着库：`doctor` 读完，库文件和日志一个字节不变。
    （普通连接最后一个关闭时会合并日志并删掉它，所以上面那条有别的连接开着的测试抓不住。）"""
    database.close()
    leave_log_behind(hub_paths.db, "INSERT INTO meta (key, value) VALUES ('probe', 'x')")
    log = wal_of(hub_paths.db)
    before = (hub_paths.db.read_bytes(), log.read_bytes())

    result = run("doctor")

    assert rows(result)["数据库"][0] == "✓"
    assert (hub_paths.db.read_bytes(), log.read_bytes()) == before


@pytest.fixture
def opened_databases(monkeypatch):
    """`doctor` 开过的每一个 `Database`，记着是不是只读开的、有没有关。"""
    opened = []

    class Tracked(doctor_module.Database):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.asked_read_only = kwargs.get("read_only", False)
            self.closed = False
            opened.append(self)

        def close(self):
            self.closed = True
            super().close()

    monkeypatch.setattr(doctor_module, "Database", Tracked)
    return opened


def break_nothing(hub_paths, database, monkeypatch):
    pass


def break_the_file(hub_paths, database, monkeypatch):
    hub_paths.db.write_bytes(b"this is not a database at all" * 50)


def break_by_busy(hub_paths, database, monkeypatch):
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    monkeypatch.setattr("clawbot_hub.doctor.check_integrity", failing_check(error))


def break_by_other_error(hub_paths, database, monkeypatch):
    error = sqlite3.OperationalError("unable to open database file")
    monkeypatch.setattr("clawbot_hub.doctor.check_integrity", failing_check(error))


def break_by_newer_version(hub_paths, database, monkeypatch):
    with database.txn() as conn:
        conn.execute("UPDATE meta SET value = '9' WHERE key = 'schema_version'")


def break_by_empty_file(hub_paths, database, monkeypatch):
    database.close()
    for suffix in ("", "-wal", "-shm"):
        hub_paths.db.with_name(hub_paths.db.name + suffix).unlink(missing_ok=True)
    hub_paths.db.touch(mode=0o600)


def break_by_older_version(hub_paths, database, monkeypatch):
    monkeypatch.setattr("clawbot_hub.doctor.latest_version", lambda: 2)


DATABASE_CASES = [
    break_nothing,
    break_the_file,
    break_by_busy,
    break_by_other_error,
    break_by_newer_version,
    break_by_empty_file,
    break_by_older_version,
]


@pytest.mark.parametrize("break_it", DATABASE_CASES, ids=lambda f: f.__name__)
def test_the_database_is_opened_read_only_and_always_closed_again(
    run, healthy, hub_paths, database, monkeypatch, opened_databases, break_it
):
    break_it(hub_paths, database, monkeypatch)

    run("doctor")

    assert len(opened_databases) == 1
    assert opened_databases[0].asked_read_only is True
    assert opened_databases[0].closed


def test_an_older_database_is_not_upgraded_by_looking_at_it(run, healthy, hub_paths, monkeypatch):
    """库比程序旧：服务下次启动才升级，`doctor` 只是说一声，不动它，依赖库的几项不列。"""
    monkeypatch.setattr("clawbot_hub.doctor.latest_version", lambda: 2)
    before = hub_paths.db.read_bytes()

    result = run("doctor")

    icon, detail = rows(result)["数据库"]
    assert icon == "✓" and "第 1 版" in detail and "第 2 版" in detail and "自动升级" in detail
    for name in ("登录状态", "保底合计", "前缀冲突", "Bark 结果"):
        assert name not in rows(result)
    assert hub_paths.db.read_bytes() == before
    assert result.exit_code == 0


def test_an_empty_database_file_is_fine_and_waits_for_the_first_start(run, healthy, hub_paths):
    for suffix in ("", "-wal", "-shm"):
        hub_paths.db.with_name(hub_paths.db.name + suffix).unlink(missing_ok=True)
    hub_paths.db.touch(mode=0o600)

    icon, detail = rows(run("doctor"))["数据库"]

    assert icon == "✓" and "空文件" in detail


def failing_check(error: Exception):
    """换掉 `check_integrity`：一调用就抛这个错。"""

    def check(_conn):
        raise error

    return check


def test_a_database_that_is_busy_is_a_warning_not_an_error(run, healthy, monkeypatch):
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    monkeypatch.setattr("clawbot_hub.doctor.check_integrity", failing_check(error))

    result = run("doctor")

    icon, detail = rows(result)["数据库"]
    assert icon == "⚠" and "正忙" in detail
    assert result.exit_code == 0
    assert "登录状态" not in rows(result)


def test_a_database_that_cannot_be_opened_is_an_error_with_the_reason(run, healthy, monkeypatch):
    error = sqlite3.OperationalError("unable to open database file")
    monkeypatch.setattr("clawbot_hub.doctor.check_integrity", failing_check(error))

    icon, detail = rows(run("doctor"))["数据库"]

    assert icon == "✗" and "打不开" in detail and "unable to open database file" in detail


# ---------------------------------------------------------------- 服务和端口


def test_a_service_that_is_not_running_is_a_warning(run, healthy, hub_paths):
    hub_paths.run.unlink()

    icon, detail = rows(run("doctor"))["服务"]

    assert icon == "⚠" and "没有在运行" in detail


def test_a_stale_heartbeat_does_not_count_as_running(run, healthy, hub_paths, clock):
    write_run_json(hub_paths, clock, last_heartbeat_ms=clock.now_ms() - 5 * MINUTE)

    assert rows(run("doctor"))["服务"][0] == "⚠"


def test_a_port_taken_by_another_program_is_an_error_when_the_service_is_not_running(
    run, healthy, hub_paths, free_port
):
    hub_paths.run.unlink()
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", free_port))
        holder.listen()

        result = run("doctor")

    icon, detail = rows(result)["端口"]
    assert icon == "✗" and str(free_port) in detail and "占着" in detail
    assert result.exit_code == 1


def test_a_free_port_is_fine(run, healthy, hub_paths):
    hub_paths.run.unlink()

    assert rows(run("doctor"))["端口"][0] == "✓"


def test_a_port_in_use_is_not_a_problem_while_the_service_is_running(run, healthy, free_port):
    """服务在运行时，端口是它自己占着的。"""
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", free_port))
        holder.listen()

        result = run("doctor")

    assert rows(result)["端口"][0] == "✓"
    assert errors(result) == set()


def test_the_port_that_is_checked_is_the_configured_one(run, healthy, hub_paths, free_port):
    hub_paths.run.unlink()
    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen()
        taken = other.getsockname()[1]
        set_config(hub_paths, taken)

        result = run("doctor")

    assert rows(result)["端口"][0] == "✗" and str(taken) in rows(result)["端口"][1]


# ---------------------------------------------------------------- 配置


def test_a_missing_config_file_means_the_built_in_defaults(run, healthy, hub_paths):
    hub_paths.config.unlink()

    icon, detail = rows(run("doctor"))["配置"]

    assert icon == "✓" and "默认值" in detail


def test_a_config_that_reads_fine_says_so(run, healthy):
    icon, detail = rows(run("doctor"))["配置"]

    assert icon == "✓" and "config.yaml" in detail and "读得出来" in detail


def test_a_config_with_mistakes_lists_every_one_and_the_other_items_still_run(
    run, healthy, hub_paths, free_port
):
    set_config(hub_paths, free_port, send={"retries": -1}, display={"timezone": "Mars/Base"})
    hub_paths.config.write_text(
        hub_paths.config.read_text(encoding="utf-8").replace(f"port: {free_port}", "port: 99999"),
        encoding="utf-8",
    )

    result = run("doctor")

    icon, detail = rows(result)["配置"]
    assert icon == "✗" and result.exit_code == 1
    for mistake in ("server.port", "send.retries", "display.timezone"):
        assert mistake in detail, mistake
    assert "数据库" in rows(result) and "自动备份" in rows(result)  # 其他项目照常查


def test_a_hub_label_in_the_wrong_format_is_an_error(run, healthy, hub_paths, free_port):
    set_config(hub_paths, free_port, hub={"label": "带 空格 的标签"})

    icon, detail = rows(run("doctor"))["配置"]

    assert icon == "✗" and "hub.label" in detail


def test_an_explicit_config_that_is_not_there_is_an_error_row(run, healthy, tmp_path):
    result = run("doctor", "--config", str(tmp_path / "typo.yaml"))

    icon, detail = rows(result)["配置"]
    assert icon == "✗" and "typo.yaml" in detail
    assert result.exit_code == 1


# ---------------------------------------------------------------- 项目：保底合计、前缀冲突


def test_guarantees_that_no_longer_fit_are_an_error_that_lists_the_projects(run, healthy, database):
    assert add(run, "beta", prefix="乙", guaranteed=5, cap=8).exit_code == 0
    with database.txn() as conn:  # 有人直接改了库
        conn.execute("UPDATE projects SET guaranteed = 6, cap = 8 WHERE name = 'seed'")

    result = run("doctor")

    icon, detail = rows(result)["保底合计"]
    assert icon == "✗" and result.exit_code == 1
    assert "seed 6" in detail and "beta 5" in detail
    assert "11" in detail and "10" in detail  # 保底合计 11（外加回复保留 2），窗口总条数 10
    assert "超了 3 条" in detail


def test_disabled_projects_do_not_count_for_the_guarantee_total(run, healthy, database):
    with database.txn() as conn:
        conn.execute("UPDATE projects SET guaranteed = 9, cap = 9, enabled = 0 WHERE name = 'seed'")

    assert rows(run("doctor"))["保底合计"][0] == "✓"


def test_a_prefix_that_now_clashes_with_the_label_or_a_confirm_word_is_an_error(
    run, healthy, hub_paths, free_port
):
    set_config(
        hub_paths,
        free_port,
        hub={"label": "种子"},  # seed 的前缀是「种子」
        routing={"confirm_words": ["对", "是", "确认"]},
    )

    result = run("doctor")

    icon, detail = rows(result)["前缀冲突"]
    assert icon == "✗" and "种子" in detail and "hub.label" in detail
    assert result.exit_code == 1


def test_a_confirm_word_clash_names_the_word(run, healthy, hub_paths, free_port):
    set_config(hub_paths, free_port, routing={"confirm_words": ["种子", "好"]})

    icon, detail = rows(run("doctor"))["前缀冲突"]

    assert icon == "✗" and "routing.confirm_words" in detail and "种子" in detail


# ---------------------------------------------------------------- 告警


def test_no_alert_key_is_a_warning_that_says_how_the_service_will_behave(run, healthy, monkeypatch):
    monkeypatch.delenv(ENV_KEY)

    icon, detail = rows(run("doctor"))["告警设备码"]

    assert icon == "⚠" and "不会通知你" in detail and "hubctl alerts set-key" in detail


def test_the_key_row_says_where_it_comes_from_and_never_the_key(run, healthy, hub_paths):
    result = run("doctor")

    icon, detail = rows(result)["告警设备码"]
    assert icon == "✓" and "环境变量" in detail
    assert "A-TEST-DEVICE-KEY" not in result.output


def test_a_key_in_the_file_is_recognised(run, healthy, hub_paths, monkeypatch):
    monkeypatch.delenv(ENV_KEY)
    hub_paths.bark.write_text(json.dumps({"key": "FILE-KEY-123456"}), encoding="utf-8")
    hub_paths.bark.chmod(0o600)

    result = run("doctor")

    assert rows(result)["告警设备码"] == ("✓", "已配置（bark.json）")
    assert "FILE-KEY-123456" not in result.output


def test_a_broken_key_file_is_a_warning(run, healthy, hub_paths, monkeypatch):
    monkeypatch.delenv(ENV_KEY)
    hub_paths.bark.write_text("这不是 JSON", encoding="utf-8")
    hub_paths.bark.chmod(0o600)

    icon, detail = rows(run("doctor"))["告警设备码"]

    assert icon == "⚠" and "bark.json 的内容不对" in detail


def test_a_refusal_from_bark_is_a_warning_until_a_test_alert_gets_through(
    run, healthy, hub_paths, monkeypatch
):
    """设备码填错 → ⚠；`set-key` 换对、`alerts test` 成功 → 警告消失。"""
    monkeypatch.delenv(ENV_KEY)
    with FakeBark(valid_keys={"RIGHT-KEY"}) as bark:
        run("alerts", "set-key", input=f"{bark.base_url}/WRONG-KEY\n")
        assert run("alerts", "test").exit_code == 1  # 被拒绝，库里记下了
        first = run("doctor")

        run("alerts", "set-key", input=f"{bark.base_url}/RIGHT-KEY\n")
        assert run("alerts", "test").exit_code == 0
        second = run("doctor")

    icon, detail = rows(first)["Bark 结果"]
    assert icon == "⚠" and "拒绝" in detail and "hubctl alerts set-key" in detail
    assert first.exit_code == 0  # 只是警告
    assert rows(second)["Bark 结果"][0] == "✓"
    assert "Bark 结果" not in warnings(second)


def test_before_any_alert_was_sent_the_bark_row_is_fine(run, healthy):
    icon, detail = rows(run("doctor"))["Bark 结果"]

    assert icon == "✓" and "还没" in detail


def test_the_shutdown_wait_shorter_than_the_send_limit_is_a_warning_with_both_numbers(
    run, healthy, hub_paths, free_port
):
    set_config(hub_paths, free_port, server={"shutdown_seconds": 20}, send={"timeout_seconds": 30})

    result = run("doctor")

    icon, detail = rows(result)["配置警告"]
    assert icon == "⚠" and "20" in detail and "30" in detail
    assert errors(result) == set() and result.exit_code == 0


# ---------------------------------------------------------------- 扫码标记、磁盘、备份


def test_a_scan_that_is_going_on_is_not_a_warning(run, healthy, hub_paths, clock):
    write_scan_marker(hub_paths, pid=os.getpid(), started_ms=clock.now_ms() - 12 * MINUTE)

    icon, detail = rows(run("doctor"))["扫码标记"]

    assert icon == "✓" and "正在扫码" in detail


def test_a_marker_whose_process_is_gone_is_a_warning(run, healthy, hub_paths, clock):
    write_scan_marker(hub_paths, pid=dead_pid(), started_ms=clock.now_ms() - 2 * MINUTE)

    icon, detail = rows(run("doctor"))["扫码标记"]

    assert icon == "⚠" and "login.pending" in detail


def test_a_marker_older_than_twenty_minutes_is_a_warning_even_if_the_process_lives(
    run, healthy, hub_paths, clock
):
    write_scan_marker(hub_paths, pid=os.getpid(), started_ms=clock.now_ms() - 21 * MINUTE)

    assert rows(run("doctor"))["扫码标记"][0] == "⚠"


def test_less_than_100_mb_free_is_a_warning(run, healthy, disk):
    disk["free"] = 99 * MB

    icon, detail = rows(run("doctor"))["磁盘空间"]

    assert icon == "⚠" and "99" in detail


def test_exactly_100_mb_free_is_fine(run, healthy, disk):
    disk["free"] = 100 * MB

    assert rows(run("doctor"))["磁盘空间"][0] == "✓"


def test_no_automatic_backup_yet_is_a_warning(run, healthy, hub_paths):
    shutil.rmtree(hub_paths.backups)

    icon, detail = rows(run("doctor"))["自动备份"]

    assert icon == "⚠" and "没做过" in detail


def test_a_backup_more_than_two_days_old_is_a_warning(run, healthy, hub_paths):
    shutil.rmtree(hub_paths.backups)
    make_auto_backup(hub_paths, "20261010-095959")  # 2 天零 1 秒前

    icon, detail = rows(run("doctor"))["自动备份"]

    assert icon == "⚠" and "2 天" in detail


def test_a_backup_of_exactly_two_days_ago_is_still_fine(run, healthy, hub_paths):
    shutil.rmtree(hub_paths.backups)
    make_auto_backup(hub_paths, "20261010-100000")

    assert rows(run("doctor"))["自动备份"][0] == "✓"


def test_only_automatic_backups_count(run, healthy, hub_paths):
    shutil.rmtree(hub_paths.backups)
    for name in ("20261012-090000", "pre-upgrade-v0-v1-20261012-090000", "auto-garbage"):
        (hub_paths.backups / name).mkdir(parents=True, mode=0o700)

    assert rows(run("doctor"))["自动备份"][0] == "⚠"


def test_the_newest_automatic_backup_decides(run, healthy, hub_paths):
    make_auto_backup(hub_paths, "20261012-040000")
    make_auto_backup(hub_paths, "20260101-040000")

    assert rows(run("doctor"))["自动备份"][0] == "✓"


# ---------------------------------------------------------------- 在线检查


def bind(hub_paths, base_url: str) -> None:
    """让凭证文件指向这个（假的）腾讯地址；`--online` 去的就是服务发消息用的地址。"""
    AccountStore(hub_paths).write(account_file(base_url=base_url))


def test_without_online_tencent_is_never_touched(run, healthy, hub_paths, fake_ilink, upstream):
    bind(hub_paths, fake_ilink.base_url)

    result = run("doctor")

    assert "腾讯连通" not in rows(result)
    assert fake_ilink.requests == [] and upstream.calls == []


def test_online_makes_one_unauthenticated_visit_to_the_root_and_nothing_else(
    run, healthy, hub_paths, fake_ilink, upstream
):
    bind(hub_paths, fake_ilink.base_url)

    result = run("doctor", "--online")

    icon, detail = rows(result)["腾讯连通"]
    assert icon == "✓" and "404" in detail
    [visit] = fake_ilink.requests
    assert (visit.method, visit.path, visit.endpoint) == ("GET", "/", ilink.ROOT)
    assert visit.header("Authorization") is None
    assert visit.body == b""
    assert upstream.calls == []  # 没有收消息、发消息，不会和运行中的服务抢消息
    assert list(rows(result))[-1] == "腾讯连通"  # 在最后


def test_online_goes_to_the_address_the_service_sends_to_not_the_configured_one(
    run, healthy, hub_paths, fake_ilink, free_port
):
    """凭证文件里的 base_url 优先（服务收发消息用的就是它），配置的 upstream.base_url 只是备用。"""
    bind(hub_paths, fake_ilink.base_url)
    set_config(hub_paths, free_port, upstream={"base_url": "http://127.0.0.1:9"})

    assert rows(run("doctor", "--online"))["腾讯连通"][0] == "✓"
    assert len(fake_ilink.requests_to(ilink.ROOT)) == 1


def test_online_falls_back_to_the_configured_address_when_the_account_has_none(
    run, healthy, hub_paths, fake_ilink, free_port
):
    bind(hub_paths, "")
    set_config(hub_paths, free_port, upstream={"base_url": fake_ilink.base_url})

    assert rows(run("doctor", "--online"))["腾讯连通"][0] == "✓"
    assert len(fake_ilink.requests_to(ilink.ROOT)) == 1


def test_online_without_a_bound_account_uses_the_configured_address(
    run, healthy, hub_paths, fake_ilink, free_port
):
    hub_paths.account.unlink()
    set_config(hub_paths, free_port, upstream={"base_url": fake_ilink.base_url})

    assert rows(run("doctor", "--online"))["腾讯连通"][0] == "✓"


def test_online_names_only_the_host_and_port_not_the_whole_address(
    run, healthy, hub_paths, fake_ilink
):
    bind(hub_paths, fake_ilink.base_url + "/some/path")

    detail = rows(run("doctor", "--online"))["腾讯连通"][1]

    host = fake_ilink.base_url.removeprefix("http://")
    assert detail.startswith(f"连得上 {host}（")
    assert "http://" not in detail and "/some/path" not in detail


def test_online_never_prints_a_user_name_or_password_written_in_the_address(
    run, healthy, hub_paths, fake_ilink
):
    host = fake_ilink.base_url.removeprefix("http://")
    bind(hub_paths, f"http://someone:secret-pw@{host}")

    result = run("doctor", "--online")

    assert rows(result)["腾讯连通"][0] == "✓"
    assert "someone" not in result.output and "secret-pw" not in result.output
    [visit] = fake_ilink.requests
    assert visit.header("Authorization") is None  # 地址里的口令也不会被当成登录信息发出去


@pytest.mark.parametrize(
    "address", ["not a url", "ilink.example", "ftp://ilink.example", "http://"]
)
def test_online_with_an_address_that_makes_no_sense_is_a_warning_without_a_request(
    run, healthy, hub_paths, fake_ilink, address
):
    bind(hub_paths, address)

    result = run("doctor", "--online")

    icon, detail = rows(result)["腾讯连通"]
    assert icon == "⚠" and "地址看不懂" in detail
    assert address not in result.output.replace("http:// 或 https://", "")
    assert fake_ilink.requests == []
    assert result.exit_code == 0


def test_an_ipv6_address_is_written_with_its_brackets(run, healthy, hub_paths, fake_ilink):
    bind(hub_paths, "http://[::1]:9")  # 九号端口没人听：连不上，但地址要写得对

    detail = rows(run("doctor", "--online"))["腾讯连通"][1]

    assert detail.startswith("连不上 [::1]:9：")


@pytest.mark.parametrize(
    ("address", "shown"),
    [
        ("https://ilink.example", "ilink.example"),  # 没写端口就只写主机
        ("https://ilink.example/", "ilink.example"),
        ("http://ilink.example:8080/a/b?c=d", "ilink.example:8080"),
        ("https://user:pw@ilink.example:443/x", "ilink.example:443"),
        ("http://[::1]", "[::1]"),
        ("http://[::1]:9/x", "[::1]:9"),
        ("HTTP://Ilink.Example:81", "ilink.example:81"),  # 主机名由解析器统一成小写
    ],
)
def test_the_address_is_shown_as_host_and_port_only_and_only_the_port_that_was_written(
    address, shown
):
    assert doctor_module._where(address) == shown


@pytest.mark.parametrize(
    "address", ["", "ilink.example", "ftp://ilink.example", "http://", "http://[::1"]
)
def test_an_address_that_cannot_be_read_is_not_shown_at_all(address):
    assert doctor_module._where(address) is None


def test_an_address_without_a_port_goes_to_the_default_port_and_is_named_without_one(
    run, healthy, hub_paths, fake_ilink
):
    bind(hub_paths, "http://127.0.0.1")  # 80 号端口没人听：连不上，但地址里不能冒出 :None

    detail = rows(run("doctor", "--online"))["腾讯连通"][1]

    assert detail.startswith("连不上 127.0.0.1：")


def test_online_that_cannot_connect_is_only_a_warning(run, healthy, hub_paths, fake_ilink):
    address = fake_ilink.base_url
    fake_ilink.stop()
    bind(hub_paths, address)

    result = run("doctor", "--online")

    icon, detail = rows(result)["腾讯连通"]
    assert icon == "⚠" and "连不上" in detail
    assert result.exit_code == 0


def test_online_ignores_what_the_server_answers_as_long_as_it_answers(
    run, healthy, hub_paths, fake_ilink
):
    fake_ilink.set_default(ilink.ROOT, http_status(502))
    bind(hub_paths, fake_ilink.base_url)

    icon, detail = rows(run("doctor", "--online"))["腾讯连通"]

    assert icon == "✓" and "502" in detail


def test_online_that_gets_no_answer_in_time_is_a_warning_and_waits_as_long_as_the_service_would(
    run, healthy, hub_paths, fake_ilink, free_port
):
    fake_ilink.set_default(ilink.ROOT, hang(5))
    bind(hub_paths, fake_ilink.base_url)
    set_config(hub_paths, free_port, upstream={"request_timeout": 1})

    started = time.monotonic()
    result = run("doctor", "--online")

    icon, detail = rows(result)["腾讯连通"]
    assert icon == "⚠" and "没有回话" in detail and "1 秒" in detail
    assert 0.9 < time.monotonic() - started < 4  # 等的是 upstream.request_timeout


def test_online_does_not_send_credentials_from_a_netrc_file(
    run, healthy, hub_paths, fake_ilink, tmp_path, monkeypatch
):
    """`~/.netrc` 里有这个地址的账号密码也不带（访问是不带凭证的）。"""
    netrc = tmp_path / "netrc"
    netrc.write_text("machine 127.0.0.1 login someone password secret\n", encoding="utf-8")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))
    bind(hub_paths, fake_ilink.base_url)

    assert rows(run("doctor", "--online"))["腾讯连通"][0] == "✓"

    [visit] = fake_ilink.requests
    assert visit.header("Authorization") is None


def test_online_does_not_follow_a_redirect(run, healthy, hub_paths, fake_ilink):
    """回了跳转就算连上了；跟过去的话会连另一个没人听的地址而报警告。"""
    fake_ilink.set_default(
        ilink.ROOT, ilink.Reply(status=302, headers={"Location": "http://127.0.0.1:9/elsewhere"})
    )
    bind(hub_paths, fake_ilink.base_url)

    icon, detail = rows(run("doctor", "--online"))["腾讯连通"]

    assert icon == "✓" and "302" in detail
    assert len(fake_ilink.requests) == 1


# ---------------------------------------------------------------- 别的


def test_a_file_where_the_directory_should_be_is_an_error_row_not_a_traceback(run, home):
    home.parent.mkdir(parents=True, exist_ok=True)
    home.write_text("我是个文件", encoding="utf-8")

    result = run("doctor")

    assert result.exit_code == 1
    assert rows(result)["数据目录"][0] == "✗"
    assert "Traceback" not in result.output


def test_nothing_in_the_output_is_a_secret(run, healthy, hub_paths):
    result = run("doctor", "--online")

    for secret in ("tok-", "A-TEST-DEVICE-KEY", "chub_"):
        assert secret not in result.output


# ---------------------------------------------------------------- 补充：措辞、边界、什么都不留下


def test_the_data_directory_is_named_in_words_not_as_a_path_inside_itself(run, healthy, hub_paths):
    hub_paths.home.chmod(0o755)

    assert "数据目录本身是 0755（应该是 0700）" in rows(run("doctor"))["权限"][1]


def test_files_in_backups_and_logs_are_checked_and_named_by_their_path_under_the_data_directory(
    run, healthy, hub_paths
):
    make_auto_backup(hub_paths, "20261012-040000")
    (hub_paths.backups / "auto-20261012-040000" / "hub.db").write_text("x")
    (hub_paths.backups / "auto-20261012-040000" / "hub.db").chmod(0o644)

    icon, detail = rows(run("doctor"))["权限"]

    assert icon == "✗" and "backups/auto-20261012-040000/hub.db 是 0644" in detail


def test_a_long_list_of_loose_files_is_cut_short_and_counted(run, healthy, hub_paths):
    make_auto_backup(hub_paths, "20261012-040000")
    for number in range(8):
        loose = hub_paths.backups / "auto-20261012-040000" / f"f{number}.txt"
        loose.write_text("x")
        loose.chmod(0o644)

    icon, detail = rows(run("doctor"))["权限"]

    assert icon == "✗"
    assert detail.count("是 0644") == 5 and "（另有 3 项）" in detail


@pytest.mark.parametrize(
    "count, more", [(1, None), (4, None), (5, None), (6, "（另有 1 项）"), (7, "（另有 2 项）")]
)
def test_the_list_says_how_many_more_only_when_it_is_cut_short(
    run, healthy, hub_paths, count, more
):
    make_auto_backup(hub_paths, "20261012-040000")
    for number in range(count):
        loose = hub_paths.backups / "auto-20261012-040000" / f"f{number}.txt"
        loose.write_text("x")
        loose.chmod(0o644)

    detail = rows(run("doctor"))["权限"][1]

    assert detail.count("是 0644") == min(count, 5)
    if more is None:
        assert "另有" not in detail
    else:
        assert more in detail


def test_fix_reports_the_cut_short_list_the_same_way(run, healthy, hub_paths):
    make_auto_backup(hub_paths, "20261012-040000")
    for number in range(8):
        loose = hub_paths.backups / "auto-20261012-040000" / f"f{number}.txt"
        loose.write_text("x")
        loose.chmod(0o644)

    detail = rows(run("doctor", "--fix"))["权限"][1]

    assert detail.count("0644 → 0600") == 5 and "（另有 3 项）" in detail
    assert all(mode_of(p) == 0o600 for p in (hub_paths.backups / "auto-20261012-040000").glob("f*"))


def test_fix_that_cannot_change_a_file_says_which_and_why(run, healthy, hub_paths, monkeypatch):
    hub_paths.account.chmod(0o644)

    def refuse(_paths):
        raise PermissionError(errno.EPERM, "Operation not permitted", str(hub_paths.account))

    monkeypatch.setattr(doctor_module, "fix_permissions", refuse)

    result = run("doctor", "--fix")

    icon, detail = rows(result)["权限"]
    assert icon == "✗" and "--fix 没改成" in detail and "account.json" in detail
    assert "Operation not permitted" in detail
    assert result.exit_code == 1


def test_an_account_that_is_not_there_has_no_login_state_row(run, healthy, hub_paths):
    hub_paths.account.unlink()

    assert "登录状态" not in rows(run("doctor"))


def test_an_account_that_cannot_be_read_has_no_login_state_row(run, healthy, hub_paths):
    hub_paths.account.write_text("这不是 JSON", encoding="utf-8")

    assert "登录状态" not in rows(run("doctor"))


def test_an_expired_mark_for_older_credentials_does_not_count_any_more(
    run, healthy, database, clock
):
    """库里记着"已失效"，但是记的是换掉之前的那份凭证：服务会自己换上新的，现在算正常。"""
    with database.txn() as conn:
        conn.execute(
            "UPDATE account_state SET login_state = 'expired', login_since_ms = ?,"
            " creds_digest = 'digest-of-the-previous-credentials' WHERE id = 1",
            (clock.now_ms() - HOUR,),
        )

    assert rows(run("doctor"))["登录状态"][0] == "✓"


def test_an_expired_login_without_a_time_is_still_reported(run, healthy, database, hub_paths):
    with database.txn() as conn:
        conn.execute(
            "UPDATE account_state SET login_state = 'expired', login_since_ms = NULL WHERE id = 1"
        )

    icon, detail = rows(run("doctor"))["登录状态"]

    assert icon == "⚠" and detail.startswith("已失效：") and "（从" not in detail


def test_the_expired_login_row_says_since_when(run, healthy, database, clock):
    with database.txn() as conn:
        conn.execute(
            "UPDATE account_state SET login_state = 'expired', login_since_ms = ? WHERE id = 1",
            (clock.now_ms() - 2 * HOUR,),
        )

    assert "（从 10月12日 08:00 起）" in rows(run("doctor"))["登录状态"][1]


def test_a_service_without_a_start_time_still_shows_as_running(run, healthy, hub_paths, clock):
    write_run_json(hub_paths, clock, started_ms=0)

    icon, detail = rows(run("doctor"))["服务"]

    assert icon == "✓" and "进程号" in detail and "已运行" not in detail


def test_the_running_service_row_says_the_pid_and_how_long(run, healthy):
    detail = rows(run("doctor"))["服务"][1]

    assert f"进程号 {os.getpid()}" in detail and "已运行 1 小时 5 分钟" in detail


def test_the_port_row_names_the_address_it_looked_at(run, healthy, hub_paths, free_port):
    hub_paths.run.unlink()

    assert rows(run("doctor"))["端口"][1] == f"127.0.0.1:{free_port} 空着"


def test_with_lan_listening_the_whole_network_address_is_the_one_checked(
    run, healthy, hub_paths, free_port
):
    hub_paths.run.unlink()
    set_config(hub_paths, free_port, server={"lan": True})

    assert rows(run("doctor"))["端口"][1] == f"0.0.0.0:{free_port} 空着"


def test_a_port_that_cannot_be_bound_for_another_reason_says_the_reason(
    run, healthy, hub_paths, monkeypatch
):
    hub_paths.run.unlink()

    def refuse(_host, _port):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(doctor_module, "_can_bind", refuse)

    icon, detail = rows(run("doctor"))["端口"]

    assert icon == "✗" and "绑不上" in detail and "Permission denied" in detail


def test_the_probe_binds_the_way_the_service_does(monkeypatch):
    """同一个地址族、打开地址重用（刚关掉的服务留下的 TIME_WAIT 不算占着）、只绑不听。"""
    calls = []

    class Probe:
        def __init__(self, family, kind):
            calls.append(("socket", family, kind))

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def setsockopt(self, level, option, value):
            calls.append(("setsockopt", level, option, value))

        def bind(self, address):
            calls.append(("bind", address))

        def listen(self, *args):
            calls.append(("listen",))

    monkeypatch.setattr(doctor_module.socket, "socket", Probe)

    doctor_module._can_bind("127.0.0.1", 8790)
    doctor_module._can_bind("::1", 8790)

    assert calls == [
        ("socket", socket.AF_INET, socket.SOCK_STREAM),
        ("setsockopt", socket.SOL_SOCKET, socket.SO_REUSEADDR, 1),
        ("bind", ("127.0.0.1", 8790)),
        ("socket", socket.AF_INET6, socket.SOCK_STREAM),
        ("setsockopt", socket.SOL_SOCKET, socket.SO_REUSEADDR, 1),
        ("bind", ("::1", 8790)),
    ]


def test_a_config_with_mistakes_leaves_out_what_depends_on_its_values(
    run, healthy, hub_paths, free_port
):
    set_config(hub_paths, free_port, send={"retries": -1})

    shown = rows(run("doctor"))

    for name in ("端口", "保底合计", "前缀冲突", "配置警告"):
        assert name not in shown, name
    for name in ("权限", "登录凭证", "数据库", "服务", "告警设备码", "磁盘空间"):
        assert name in shown, name


def test_a_config_that_cannot_be_read_as_yaml_is_one_error_row(run, healthy, hub_paths):
    hub_paths.config.write_text("server: [unclosed\n", encoding="utf-8")

    icon, detail = rows(run("doctor"))["配置"]

    assert icon == "✗" and "YAML" in detail and "1 处错误" in detail


def test_the_config_given_on_the_command_line_is_the_one_that_is_read(
    run, healthy, tmp_path, free_port
):
    other = tmp_path / "other.yaml"
    other.write_text(f"server:\n  port: {free_port}\nsend:\n  retries: 99\n", encoding="utf-8")

    icon, detail = rows(run("doctor", "--config", str(other)))["配置"]

    assert icon == "✗" and "other.yaml" in detail and "send.retries" in detail


@pytest.mark.parametrize(
    "free, text",
    [
        (500 * MB, "还剩 500 MB"),
        (1023 * MB + MB - 1, "还剩 1023 MB"),  # 差 1 字节到 1 GB：还是 MB
        (1024 * MB, "还剩 1.0 GB"),
        (1536 * MB, "还剩 1.5 GB"),
        (100 * GB, "还剩 100.0 GB"),  # 换算用的是 1024 的三次方，不是别的数
    ],
)
def test_amounts_of_free_space_are_written_in_megabytes_or_gigabytes(
    run, healthy, disk, free, text
):
    disk["free"] = free

    assert rows(run("doctor"))["磁盘空间"][1] == text


def test_the_low_space_warning_says_what_it_means(run, healthy, disk):
    disk["free"] = 5 * MB

    assert "不到 100 MB" in rows(run("doctor"))["磁盘空间"][1]


def test_free_space_that_cannot_be_read_is_a_warning(run, healthy, monkeypatch):
    def refuse(_path):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(shutil, "disk_usage", refuse)

    icon, detail = rows(run("doctor"))["磁盘空间"]

    assert icon == "⚠" and "Input/output error" in detail


def test_the_free_space_checked_is_the_one_where_the_data_directory_is(
    run, healthy, hub_paths, monkeypatch
):
    asked = []

    def record(path):
        asked.append(path)
        return Usage(100 * GB, 0, 100 * GB)

    monkeypatch.setattr(shutil, "disk_usage", record)

    run("doctor")

    assert [os.fspath(path) for path in asked] == [os.fspath(hub_paths.home)]


def test_the_backup_row_says_when_the_last_one_was(run, healthy):
    assert rows(run("doctor"))["自动备份"][1] == "最近一次是 10月11日 04:00（1 天 6 小时前）"


def test_the_missing_backup_row_says_when_the_service_makes_them(run, healthy, hub_paths):
    shutil.rmtree(hub_paths.backups)

    assert "每天 04:00 自动做一次" in rows(run("doctor"))["自动备份"][1]


def test_the_missing_backup_row_follows_the_configured_time(run, healthy, hub_paths, free_port):
    shutil.rmtree(hub_paths.backups)
    set_config(hub_paths, free_port, backup={"at": "23:30"})

    assert "每天 23:30 自动做一次" in rows(run("doctor"))["自动备份"][1]


def test_the_old_backup_row_says_how_old(run, healthy, hub_paths):
    shutil.rmtree(hub_paths.backups)
    make_auto_backup(hub_paths, "20261008-040000")

    detail = rows(run("doctor"))["自动备份"][1]

    assert "10月8日 04:00" in detail and "4 天 6 小时前" in detail


def test_backup_times_in_the_directory_names_are_read_in_the_display_time_zone(
    run, healthy, hub_paths, free_port
):
    """显示时区换成东京以后，同一个目录名是东京时间：比北京时间早了 1 小时就差不多整整 2 天。"""
    shutil.rmtree(hub_paths.backups)
    make_auto_backup(hub_paths, "20261010-110000")
    set_config(hub_paths, free_port, display={"timezone": "Asia/Tokyo"})

    # 假时钟 10:00 北京 = 11:00 东京；2026-10-10 11:00 东京正好 2 天前
    assert rows(run("doctor"))["自动备份"][0] == "✓"


def test_a_scan_marker_with_unreadable_content_is_a_warning(run, healthy, hub_paths):
    hub_paths.login_pending.write_text("乱写的", encoding="utf-8")
    hub_paths.login_pending.chmod(0o600)

    icon, detail = rows(run("doctor"))["扫码标记"]

    assert icon == "⚠" and "内容读不出来" in detail


def test_each_kind_of_leftover_scan_marker_says_why(run, healthy, hub_paths, clock):
    write_scan_marker(hub_paths, pid=dead_pid(), started_ms=clock.now_ms() - 2 * MINUTE)
    assert "里面的进程已经不在了" in rows(run("doctor"))["扫码标记"][1]

    write_scan_marker(hub_paths, pid=os.getpid(), started_ms=clock.now_ms() - 21 * MINUTE)
    assert "开始已经超过 20 分钟" in rows(run("doctor"))["扫码标记"][1]

    write_scan_marker(hub_paths, pid=os.getpid(), started_ms=clock.now_ms() + 5 * MINUTE)
    assert "开始时间在将来" in rows(run("doctor"))["扫码标记"][1]


def test_a_scan_in_progress_says_who_and_for_how_long(run, healthy, hub_paths, clock):
    write_scan_marker(hub_paths, pid=os.getpid(), started_ms=clock.now_ms() - 12 * MINUTE)

    assert rows(run("doctor"))["扫码标记"][1] == f"正在扫码（进程 {os.getpid()}，开始 12 分钟了）"


def test_a_fresh_directory_is_left_with_nothing_in_it(run, home, tmp_path, free_port, disk):
    config = tmp_path / "elsewhere.yaml"
    config.write_text(f"server:\n  port: {free_port}\n", encoding="utf-8")

    run("doctor", "--config", str(config))

    assert list(home.iterdir()) == []


def test_the_bark_row_after_a_good_test_says_it_got_through(run, healthy, monkeypatch):
    monkeypatch.delenv(ENV_KEY)
    with FakeBark(valid_keys={"RIGHT-KEY"}) as bark:
        run("alerts", "set-key", input=f"{bark.base_url}/RIGHT-KEY\n")
        assert run("alerts", "test").exit_code == 0

        assert rows(run("doctor"))["Bark 结果"] == ("✓", "最近一次发 Bark 成功了")
