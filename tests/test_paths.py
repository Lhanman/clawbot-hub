"""数据目录的位置和里面的文件名（规格 `hub-deploy`「数据目录和权限」、`hub-cli` 公共约定）。"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from clawbot_hub.paths import HOME_ENV, HubPaths, ensure_home, resolve_home


def test_the_home_argument_wins_over_the_environment_and_the_default(tmp_path):
    env = {HOME_ENV: str(tmp_path / "from-env")}
    assert resolve_home(tmp_path / "from-arg", env) == tmp_path / "from-arg"
    assert resolve_home(str(tmp_path / "from-arg"), env) == tmp_path / "from-arg"


def test_the_environment_wins_over_the_default(tmp_path):
    assert resolve_home(None, {HOME_ENV: str(tmp_path / "from-env")}) == tmp_path / "from-env"


def test_the_default_is_a_dot_directory_in_the_user_home():
    assert resolve_home(None, {}) == Path.home() / ".clawbot-hub"


def test_empty_values_fall_through_to_the_next_source(tmp_path):
    assert resolve_home("", {HOME_ENV: ""}) == Path.home() / ".clawbot-hub"
    assert resolve_home("", {HOME_ENV: str(tmp_path / "e")}) == tmp_path / "e"


def test_tilde_and_relative_paths_become_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert resolve_home("relative", {}) == tmp_path / "relative"
    assert resolve_home("~/somewhere", {}) == Path.home() / "somewhere"


def test_without_arguments_it_reads_the_real_environment(tmp_path, monkeypatch):
    # 用一个和默认位置不同的值，免得两条路碰巧给出同一个答案；
    # 变量名直接写字面量：文档、launchd 配置里写的就是这个名字，常量改了名这里要报
    monkeypatch.setenv("CLAWBOT_HUB_HOME", str(tmp_path / "from-the-real-environment"))
    assert resolve_home() == tmp_path / "from-the-real-environment"
    monkeypatch.delenv("CLAWBOT_HUB_HOME")
    assert resolve_home() == Path.home() / ".clawbot-hub"


def test_the_environment_variable_is_called_clawbot_hub_home():
    assert HOME_ENV == "CLAWBOT_HUB_HOME"


def test_every_file_lives_directly_in_the_home_directory(tmp_path):
    paths = HubPaths(tmp_path / "hub")
    home = tmp_path / "hub"
    assert paths.home == home
    assert paths.db == home / "hub.db"
    assert paths.account == home / "account.json"
    assert paths.account_lock == home / "account.json.lock"
    assert paths.config == home / "config.yaml"
    assert paths.bark == home / "bark.json"
    assert paths.lock == home / "hub.lock"
    assert paths.run == home / "run.json"
    assert paths.logs == home / "logs"
    assert paths.log_file == home / "logs" / "hub.log"
    assert paths.launchd_log == home / "logs" / "launchd.log"
    assert paths.backups == home / "backups"
    assert paths.login_png == home / "clawbot-login.png"
    assert paths.login_pending == home / "login.pending"


def test_ensure_home_creates_a_private_directory_and_its_parents(tmp_path):
    paths = HubPaths(tmp_path / "a" / "b" / "hub")
    ensure_home(paths)
    assert paths.home.is_dir()
    assert stat.S_IMODE(paths.home.stat().st_mode) == 0o700


def test_ensure_home_leaves_an_existing_directory_as_it_is(tmp_path):
    home = tmp_path / "hub"
    home.mkdir(mode=0o755)
    home.chmod(0o755)
    (home / "keep.txt").write_text("x")
    ensure_home(HubPaths(home))
    assert stat.S_IMODE(home.stat().st_mode) == 0o755  # 放宽了的权限由 fix_permissions 管
    assert (home / "keep.txt").read_text() == "x"


def test_ensure_home_refuses_a_file_in_the_way(tmp_path):
    (tmp_path / "hub").write_text("not a directory")
    with pytest.raises(NotADirectoryError):
        ensure_home(HubPaths(tmp_path / "hub"))
