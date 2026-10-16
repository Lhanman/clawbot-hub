"""launchd 模板（规格 `hub-deploy`「运行环境」→「launchd 模板可用」，任务 D6）。

模板在 `deploy/launchd/com.clawbot-hub.serve.plist.example`，里面有四个占位符，安装时用 `sed`
换成实际路径（`docs/ops/安装说明.md` 第 8 步）。这里用 `plistlib`（`plutil -lint` 只有 Mac 上才有）
把换好的模板解析出来，钉住规格点名的每一项，并且保证模板里的占位符和安装说明里的 `sed` 命令是
同一套。
"""

from __future__ import annotations

import plistlib
import re
from pathlib import Path

import pytest

from clawbot_hub.config import Config

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "deploy" / "launchd" / "com.clawbot-hub.serve.plist.example"
INSTALL = ROOT / "docs" / "ops" / "安装说明.md"

VALUES = {
    "__LABEL__": "com.clawbot-hub.serve",
    "__REPO_DIR__": "/opt/example/clawbot-hub",
    "__UV_PATH__": "/opt/example/bin/uv",
    "__DATA_DIR__": "/opt/example/data/.clawbot-hub",
}
PLACEHOLDER = re.compile(r"__[A-Z_]+__")


def template_text() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def filled(**changes: str) -> dict:
    text = template_text()
    for name, value in {**VALUES, **changes}.items():
        text = text.replace(name, value)
    return plistlib.loads(text.encode("utf-8"))


@pytest.fixture(scope="module")
def plist() -> dict:
    return filled()


# ---------------------------------------------------------------- 能用


def test_the_template_exists_and_parses_with_and_without_the_placeholders_replaced(plist):
    assert TEMPLATE.is_file()
    assert plistlib.loads(template_text().encode("utf-8"))["Label"] == "__LABEL__"
    assert plist["Label"] == VALUES["__LABEL__"]


def test_all_placeholders_are_replaced_by_the_four_documented_values(plist):
    assert set(PLACEHOLDER.findall(template_text())) == set(VALUES)  # 模板里只有这四个
    assert PLACEHOLDER.findall(plistlib.dumps(plist).decode("utf-8")) == []


def test_the_install_guide_substitutes_exactly_the_placeholders_of_the_template():
    guide = INSTALL.read_text(encoding="utf-8")
    seds = set(re.findall(r'-e "s\|(__[A-Z_]+__)\|', guide))

    assert seds == set(VALUES)
    assert "com.clawbot-hub.serve.plist.example" in guide


def test_it_runs_hubctl_serve_through_uv_in_the_repository_with_the_data_directory(plist):
    arguments = plist["ProgramArguments"]

    assert arguments[0] == VALUES["__UV_PATH__"]
    assert arguments[1:4] == ["run", "hubctl", "serve"]
    assert arguments[arguments.index("--home") + 1] == VALUES["__DATA_DIR__"]
    assert plist["WorkingDirectory"] == VALUES["__REPO_DIR__"]


# ---------------------------------------------------------------- 规格点名的项


def test_it_starts_at_login(plist):
    assert plist["RunAtLoad"] is True


def test_keep_alive_restarts_only_after_a_crash_or_a_non_zero_exit(plist):
    assert plist["KeepAlive"] == {"SuccessfulExit": False}


def test_throttle_interval_is_30_seconds(plist):
    assert plist["ThrottleInterval"] == 30


def test_exit_time_out_is_longer_than_the_default_graceful_shutdown(plist):
    default = Config().server.shutdown_seconds

    assert plist["ExitTimeOut"] == 45
    assert plist["ExitTimeOut"] > default  # 否则优雅关停没等满就被杀


def test_umask_is_63_which_is_077(plist):
    assert plist["Umask"] == 63
    assert oct(plist["Umask"]) == "0o77"


def test_logs_go_to_launchd_log_in_the_data_directory(plist):
    expected = f"{VALUES['__DATA_DIR__']}/logs/launchd.log"

    assert plist["StandardOutPath"] == expected
    assert plist["StandardErrorPath"] == expected


# ---------------------------------------------------------------- 干净


def test_the_template_has_no_secrets_and_no_local_paths():
    text = template_text()

    assert not re.search(r"chub_[A-Za-z0-9_-]{20,}", text)  # 项目密钥
    for word in ("bot_token", "BARK", "device_key", "api.day.app", "@im.wechat"):
        assert word not in text, word
    assert "/Users/" not in text and "/home/" not in text  # 路径都是占位符
    assert "CLAWBOT_HUB_BARK_KEY" not in text


def test_the_environment_variables_hold_nothing_secret(plist):
    names = set(plist["EnvironmentVariables"])

    assert names == {"PATH", "LANG", "PYTHONIOENCODING", "PYTHONUNBUFFERED"}
