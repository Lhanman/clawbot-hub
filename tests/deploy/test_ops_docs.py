"""D1：运维文档（规格 `hub-deploy`「运行环境」「谁能连」，任务单 D1）。

文档是给人照着做的，测试只钉住规格点名的那些硬要求，免得以后改文档时悄悄丢掉：

- 四份文档都在；
- 停服务一律写 `launchctl bootout`，没有单独的 `kill`、`launchctl stop` 让人以为能停住；
- 真机验证清单里列了 `hub-deploy` 点名的每一项，并且和 `tests/live/test_real_ilink.py` 里的测试
  一一对应（文档和测试骨架谁多谁少都会报错）；
- `README.md` 和安装说明写明局域网监听时密钥是明文走局域网的；
- 安装说明写明演练用单独的数据目录（`CLAWBOT_HUB_HOME`）、单独的端口和 launchd label；
- 迁移手册的第一步检查演练遗留，导入凭证之前先 `launchctl bootout` 停服务；
- 每一步写明预期输出；
- 公开仓库里的文档不带本机路径、邮箱和密钥。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OPS = ROOT / "docs" / "ops"
README = ROOT / "README.md"
INSTALL = OPS / "安装说明.md"
MIGRATE = OPS / "迁移手册.md"
ROLLBACK = OPS / "回退手册.md"
CHECKLIST = OPS / "真机验证清单.md"
LIVE_TESTS = ROOT / "tests" / "live" / "test_real_ilink.py"

DOCS = [INSTALL, MIGRATE, ROLLBACK, CHECKLIST]


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def sections(text: str, prefix: str) -> dict[str, str]:
    """把文档按「## <prefix> N 步」切开：{标题: 这一节的全文}。"""
    pattern = re.compile(rf"^## ({re.escape(prefix)}\s*\d+\s*步.*)$", re.MULTILINE)
    found = list(pattern.finditer(text))
    out: dict[str, str] = {}
    for index, match in enumerate(found):
        end = found[index + 1].start() if index + 1 < len(found) else len(text)
        out[match.group(1).strip()] = text[match.start() : end]
    return out


# ---------------------------------------------------------------- 文档都在


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_doc_exists_and_is_not_empty(path: Path):
    assert path.is_file(), f"缺少 {path.relative_to(ROOT)}"
    assert len(read(path).strip()) > 500


def test_doc_names_are_chinese_titles():
    names = {p.name for p in OPS.glob("*.md")}
    assert {"安装说明.md", "迁移手册.md", "回退手册.md", "真机验证清单.md"} <= names


# ---------------------------------------------------------------- 停服务


def code_blocks(text: str) -> str:
    """文档里所有 ``` 围起来的代码块（人会照着敲的命令都在这里），拼成一份。"""
    return "\n".join(re.findall(r"(?s)```[^\n]*\n(.*?)```", text))


@pytest.mark.parametrize("path", [INSTALL, MIGRATE, ROLLBACK], ids=lambda p: p.name)
def test_stopping_the_service_is_launchctl_bootout(path: Path):
    text = read(path)
    assert "launchctl bootout" in code_blocks(text), "停服务的命令要写在代码块里"
    # 人会照着敲的命令里，不能有单独的 kill / launchctl stop 让人以为能停住
    # （正文里写「不要用 kill」这样的提醒是可以的）
    commands = code_blocks(text)
    for bad in ("launchctl stop", "launchctl unload", "launchctl remove"):
        assert bad not in commands, f"{path.name} 的命令里不该出现 {bad}"
    assert not re.search(r"(?m)^\s*(?:\$ )?(?:sudo )?(?:kill|pkill|killall)\b", commands)


def test_migration_stops_the_service_before_importing_credentials():
    text = read(MIGRATE)
    stop = text.index("launchctl bootout")
    importing = text.index("hubctl import-credentials")
    assert stop < importing, "导入凭证之前必须先 launchctl bootout 停掉服务"
    assert "导入凭证前必须先" in text


# ---------------------------------------------------------------- 演练和正式分开


def test_install_guide_separates_rehearsal_from_the_real_thing():
    text = read(INSTALL)
    assert "CLAWBOT_HUB_HOME" in text
    assert "~/.clawbot-hub-rehearsal" in text
    assert "演练" in text and "端口" in text
    assert "com.clawbot-hub.rehearsal" in text, "演练要用另一个 launchd label"
    assert "移走" in text, "演练完整个目录移走"


def test_migration_first_step_checks_for_rehearsal_leftovers():
    steps = sections(read(MIGRATE), "第")
    assert steps, "迁移手册要分「第 N 步」"
    first = next(iter(steps.values()))
    assert re.match(r"第\s*1\s*步", next(iter(steps)))
    for word in ("演练", "account.json", "hubctl project list"):
        assert word in first, f"迁移手册第一步没提到 {word}"


# ---------------------------------------------------------------- 局域网是明文


@pytest.mark.parametrize("path", [README, INSTALL], ids=lambda p: p.name)
def test_lan_listening_is_documented_as_plaintext(path: Path):
    text = read(path)
    assert "明文" in text
    assert "可信的家庭网络" in text
    assert "server.lan" in text


# ---------------------------------------------------------------- 每一步有预期输出


def test_every_install_step_has_expected_output():
    steps = sections(read(INSTALL), "第")
    assert len(steps) >= 8
    for title, body in steps.items():
        assert "预期" in body, f"安装说明「{title}」没写预期输出"


def test_install_guide_has_the_required_steps():
    text = read(INSTALL)
    for word in (
        "uv sync",
        "hubctl login",
        "hubctl project add",
        "hubctl alerts set-key",
        "hubctl doctor",
        "hubctl status",
        "ls -l",
        "launchd.log",
        "0600",
    ):
        assert word in text, f"安装说明缺少 {word}"


def test_migration_marks_the_steps_that_need_the_owner():
    text = read(MIGRATE)
    assert "使用者本人在场" in text
    assert "周末" in text
    for word in ("--probe", "clawbot.json", "回退手册"):
        assert word in text, f"迁移手册缺少 {word}"


def test_rollback_manual_covers_direct_mode_bark_only_and_backups():
    text = read(ROLLBACK)
    for word in ("direct", "Bark", "hubctl backup", "hub.db", "account.json", "pre-upgrade"):
        assert word in text, f"回退手册缺少 {word}"


# ---------------------------------------------------------------- 真机验证清单


def live_test_names() -> list[str]:
    return re.findall(r"(?m)^def (test_\w+)\(", read(LIVE_TESTS))


def test_checklist_matches_the_live_test_skeletons_one_to_one():
    text = read(CHECKLIST)
    names = live_test_names()
    assert len(names) == 11
    for name in names:
        assert f"`{name}`" in text, f"真机验证清单没有列 {name}"
    mentioned = set(re.findall(r"`(test_\w+)`", text))
    assert mentioned == set(names), "清单里的测试名和 tests/live/test_real_ilink.py 对不上"


def test_checklist_lists_every_item_the_deploy_spec_names():
    text = read(CHECKLIST)
    for word in (
        "消息编号",  # 决定 upstream.confirm 能不能改成 strict
        "strict",
        "legacy",
        "official",
        "POST",  # official 的取二维码
        "窗口",
        "对话凭证",
        "一号一绑",
        "get_updates_buf",
        "长轮询",
        "Umask",
        "/dev/null",
    ):
        assert word in text, f"真机验证清单缺少 {word}"


def test_each_checklist_item_says_how_to_test_and_how_to_record():
    text = read(CHECKLIST)
    for name in live_test_names():
        heading = re.search(rf"(?m)^## \d+\. `{name}`.*$", text)
        assert heading, f"{name} 没有自己的一节（「## N. `{name}`：…」）"
        rest = text[heading.end() :]
        following = re.search(r"(?m)^## ", rest)
        block = rest[: following.start()] if following else rest
        assert "怎么测" in block, f"{name} 没写怎么测"
        assert "怎么记" in block, f"{name} 没写怎么记结果"
        assert "决定什么" in block and "回填" in block, f"{name} 没写决定什么、回填到哪"


def test_checklist_warns_to_ask_before_using_the_real_account():
    text = read(CHECKLIST)
    for word in ("额度", "不要再扫", "使用者在场"):
        assert word in text, f"真机验证清单缺少提醒：{word}"


# ---------------------------------------------------------------- 公开仓库里不带私人信息


@pytest.mark.parametrize("path", [README, *DOCS], ids=lambda p: p.name)
def test_docs_carry_no_secrets_or_personal_paths(path: Path):
    text = read(path)
    assert not re.search(r"/Users/|/home/[a-z]", text), "文档里别写本机的绝对路径"
    assert not re.search(r"chub_[A-Za-z0-9_-]{20,}", text), "文档里别放真的项目密钥"
    assert not re.search(r"[\w.+-]+@(?:gmail|qq|163|outlook)\.", text), "文档里别放邮箱"
    assert not re.search(r"wxid_\w+|@im\.wechat", text), "文档里别放微信号"
