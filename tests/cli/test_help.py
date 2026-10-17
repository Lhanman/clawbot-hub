"""`hubctl --help` 和各命令的帮助（任务 D3）。

规格 `hub-cli`「命令一览」：`hubctl --help` 列出全部命令，每条一句中文说明；所有命令认 `--home`
和 `--config`。另外把运维文档（`docs/ops/*.md`）和项目那一侧的文档（`docs/api/v1.md`、
`docs/clients/接入指南.md`）里写到的每一条 `hubctl …` 对一遍：命令和选项都得真有，免得文档里的
命令照抄下来跑不通。

`serve`（D6）和别的命令一样受这些检查：说明、`--home`、`--config`、文档里的写法。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from clawbot_hub.cli import COMMAND_ORDER, app

SPEC_ORDER = [
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
]
SUBCOMMANDS = {
    "project": ["add", "list", "show", "set", "disable", "enable", "remove", "rotate"],
    "alerts": ["set-key", "test"],
    "config": ["show"],
}
CJK = re.compile(r"[一-鿿]")
ROOT = Path(__file__).resolve().parents[2]
OPS_DOCS = sorted((ROOT / "docs" / "ops").glob("*.md"))
# 项目那一侧的文档（D5）里也写着 `hubctl …` 的命令，一样要对得上
CLIENT_DOCS = [ROOT / "docs" / "api" / "v1.md", ROOT / "docs" / "clients" / "接入指南.md"]
COMMAND_DOCS = [*OPS_DOCS, *CLIENT_DOCS]


def root():
    return typer.main.get_command(app)


def leaves() -> list[tuple[str, ...]]:
    """每个能直接运行的命令的路径：`("status",)`、`("project", "add")` ……"""
    found = []
    for name, command in root().commands.items():
        if hasattr(command, "commands"):
            found.extend((name, sub) for sub in command.commands)
        else:
            found.append((name,))
    return found


def table_names(output: str) -> list[str]:
    """帮助里"Commands"那张表的命令名，按出现的先后（折行的说明不算）。"""
    return [
        m[1] for line in output.splitlines() if (m := re.match(r"│ ([a-z][a-z-]*)\s{2,}\S", line))
    ]


# ---------------------------------------------------------------- 列表


def test_help_lists_every_command_in_the_order_of_the_spec():
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert table_names(result.output) == SPEC_ORDER


def test_the_order_written_in_the_code_is_the_one_in_the_spec():
    assert list(COMMAND_ORDER) == SPEC_ORDER


def test_the_command_set_is_exactly_the_spec_one():
    assert sorted(root().commands) == sorted(SPEC_ORDER)
    for group, names in SUBCOMMANDS.items():
        assert list(root().commands[group].commands) == names  # 先后也同规格


def test_with_no_command_the_help_is_shown():
    result = CliRunner().invoke(app, [])

    assert "Usage: hubctl" in result.output and "login" in table_names(result.output)


def test_no_shell_completion_options_are_offered():
    """补全安装会往使用者的 shell 配置里写东西，规格没要，不提供。"""
    for argv in (["--help"], ["project", "--help"]):
        assert "completion" not in CliRunner().invoke(app, argv).output, argv

    assert CliRunner().invoke(app, ["--install-completion"]).exit_code == 2


def test_a_command_group_without_a_subcommand_shows_what_it_has():
    for group, names in SUBCOMMANDS.items():
        result = CliRunner().invoke(app, [group])

        assert f"Usage: hubctl {group}" in result.output, group
        assert table_names(result.output) == names, group


# ---------------------------------------------------------------- 每条一句中文说明


@pytest.mark.parametrize("path", leaves() + [(name,) for name in SUBCOMMANDS], ids="-".join)
def test_every_command_has_a_one_line_description_in_chinese(path):
    command = root()
    for name in path:
        command = command.commands[name]

    short = command.get_short_help_str(limit=1000)

    assert CJK.search(short), short
    assert "\n" not in short


def test_the_list_shows_each_description_in_full_without_cutting_it_short():
    """说明太长的话在 80 列的终端里会被截成"…"：列表里用短说明。"""
    result = CliRunner().invoke(app, ["--help"])

    assert "…" not in result.output


# ---------------------------------------------------------------- 选项


@pytest.mark.parametrize("path", leaves(), ids="-".join)
def test_every_command_takes_home_and_config(path):
    command = root()
    for name in path:
        command = command.commands[name]
    options = {opt for param in command.params for opt in (*param.opts, *param.secondary_opts)}

    assert {"--home", "--config"} <= options


@pytest.mark.parametrize("path", leaves(), ids="-".join)
def test_every_command_prints_its_own_help(path):
    result = CliRunner().invoke(app, [*path, "--help"])

    assert result.exit_code == 0, result.output
    assert f"Usage: hubctl {' '.join(path)}" in result.output
    assert "--home" in result.output and "--config" in result.output


# ---------------------------------------------------------------- 运维文档里的命令都真有


HUBCTL = re.compile(r"hubctl((?:[ \t]+[^\s`|]+)+)")


def doc_invocations() -> list[tuple[str, int, list[str]]]:
    """(文件名, 行号, 词表)：文档里每一处 `hubctl` 后面跟着的词，到反引号、竖线或行尾为止。"""
    found = []
    for doc in COMMAND_DOCS:
        for number, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), start=1):
            for match in HUBCTL.finditer(line):
                words = match[1].split()
                # 只留 ASCII 的词：中文的说明接在命令后面时，从中文开始就不是命令的一部分了
                kept = []
                for word in words:
                    if not word.isascii() and not word.startswith("<"):
                        break
                    kept.append(word)
                if kept:
                    found.append((doc.name, number, kept))
    return found


def check_invocation(words: list[str]) -> str | None:
    """文档里的这一句对得上命令行吗？对得上返回 `None`，对不上返回哪里不对。"""
    top = root()
    first = words[0]
    if first.startswith("-"):  # `hubctl --help`
        return None if first in ("--help",) else f"根命令没有选项 {first}"
    command = top.commands.get(first)
    if command is None:
        return f"没有命令 {first}"
    rest = words[1:]
    path = [first]
    if hasattr(command, "commands"):
        if not rest or rest[0].startswith("-"):
            return f"{first} 后面要跟子命令"
        sub = command.commands.get(rest[0])
        if sub is None:
            return f"{first} 下面没有子命令 {rest[0]}"
        command, rest, path = sub, rest[1:], [first, rest[0]]
    options = {opt for param in command.params for opt in (*param.opts, *param.secondary_opts)}
    options.add("--help")
    for word in rest:
        if word.startswith("--") and word.split("=")[0] not in options:
            return f"hubctl {' '.join(path)} 没有选项 {word}"
    return None


def test_the_ops_docs_were_found():
    assert len(OPS_DOCS) >= 4
    assert all(doc.is_file() for doc in CLIENT_DOCS)
    assert len(doc_invocations()) >= 30  # 少了多半是解析坏了
    assert {name for name, _, _ in doc_invocations()} >= {doc.name for doc in CLIENT_DOCS}


@pytest.mark.parametrize(
    "doc, number, words", doc_invocations(), ids=lambda v: v if isinstance(v, str) else None
)
def test_every_command_in_the_docs_exists(doc, number, words):
    problem = check_invocation(words)

    assert problem is None, f"{doc} 第 {number} 行 `hubctl {' '.join(words)}`：{problem}"


def test_the_install_guide_lists_exactly_the_commands_there_are():
    """安装说明第 2 步的预期输出写着"`hubctl --help` 列出全部命令：…"，那串名字和真实的命令一致
    （先后也同 `--help`）。"""
    guide = (OPS_DOCS[0].parent / "安装说明.md").read_text(encoding="utf-8")
    sentence = re.search(r"`hubctl --help` 列出全部命令：([^。]*)。", guide)

    assert sentence, "安装说明里找不到“hubctl --help 列出全部命令”那句"
    listed = re.findall(r"`([a-z-]+)`", sentence[1])
    assert listed == SPEC_ORDER


def test_the_checker_itself_notices_a_wrong_command_or_option():
    """检查器本身：对的放过、错的拦住（不然上面那组测试什么都查不出来）。"""
    assert check_invocation(["status"]) is None
    assert check_invocation(["project", "add", "x", "--prefix", "量", "--guaranteed", "1"]) is None
    assert check_invocation(["statuz"]) == "没有命令 statuz"
    assert "没有子命令 zap" in check_invocation(["project", "zap"])
    assert "后面要跟子命令" in check_invocation(["project"])
    assert "没有选项 --nope" in check_invocation(["doctor", "--nope"])
    assert "没有选项 --nope" in check_invocation(["project", "list", "--nope"])
    assert check_invocation(["doctor", "--fix", "--online"]) is None
    assert check_invocation(["serve"]) is None  # 还没做，文档里先写着
