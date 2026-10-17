"""合约测试：文档和服务对得上（任务 D5，规格 `hub-client`「接入指南和对接约定」「合约测试」）。

文档是给别的项目看的：它写的和服务做的不一样，就是错的。这里钉住：

- 导出的接口描述 `docs/api/openapi.json` 和服务现在的描述一样，不一样时提示重新导出；描述里写的
  返回结构，真实的返回对得上；
- 每个接口都在文档里、在接口描述里，并且至少有一个标了 `@covers(...)` 的合约测试；
- `docs/api/v1.md` 里标明可执行的 `curl` 示例，对着测试服务按顺序真的运行，返回要和文档写的一致
  （只比字段和固定的值，不比 `id`、时间、句柄这类每次不同的值），取话示例里的 `at` 和 `at_ms`
  是同一个时刻；
- 文档里的原因代码、错误代码、`status` 取值和服务实际的对得上，文档里引用的默认数字就是配置的
  默认值；
- `docs/clients/接入指南.md` 写了规格要求的那几条对接约定，里面的 Python 例子能按 3.9 的语法解析、
  用到的客户端函数和参数真的存在。

可执行示例的写法（`docs/api/v1.md` 里）：围栏 ```` ```bash exec ```` 的代码块是命令，紧跟着的
```` ```json <HTTP 状态码> ```` 是期望的返回；命令的围栏前一行可以有 HTML 注释形式的准备动作
`<!-- exec: say 持仓 -->`（使用者在微信里说一句话，项目在线）、`<!-- exec: closed -->`（使用者
24 小时没说话，对话窗口关了）。命令里可以用 `$HUB`、`$KEY`，以及前面的取话示例里取到的
`$HANDLE`、`$ITEM_ID`。
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import clawbot_hub_client as hub_client
import pytest

from clawbot_hub import api, apidoc
from clawbot_hub.config import Config
from clawbot_hub.upstream.base import NoResponse, Rejected
from tests.contract import ENDPOINTS
from tests.sender.test_reasons import APPEARS_AS
from tests.support.fake_upstream import DEFAULT
from tests.support.threads import wait_for

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "clawbot_hub"
API_DOC = ROOT / "docs" / "api" / "v1.md"
OPENAPI = ROOT / "docs" / "api" / "openapi.json"
GUIDE = ROOT / "docs" / "clients" / "接入指南.md"
DAY = 24 * 3600


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 读 Markdown


@dataclass
class Block:
    """文档里的一个围栏代码块。"""

    info: str  # 围栏后面的说明：`bash exec`、`json 200`、`python` ……
    text: str
    line: int  # 围栏所在的行（从 1 起）
    heading: str  # 所在的二级标题（不含 `## `）
    directives: list[str] = field(default_factory=list)  # 紧挨在围栏前的 `<!-- exec: … -->`


FENCE = re.compile(r"^```(.*)$")
DIRECTIVE = re.compile(r"^<!--\s*exec:\s*(.+?)\s*-->$")


def blocks_of(markdown: str) -> list[Block]:
    blocks: list[Block] = []
    heading = ""
    directives: list[str] = []
    inside: Block | None = None
    body: list[str] = []
    for number, line in enumerate(markdown.splitlines(), start=1):
        if inside is not None:
            if line.strip() == "```":
                inside.text = "\n".join(body)
                blocks.append(inside)
                inside, body = None, []
            else:
                body.append(line)
            continue
        opened = FENCE.match(line)
        if opened:
            inside = Block(opened[1].strip(), "", number, heading, directives)
            directives = []
            continue
        if line.startswith("## "):
            heading = line[3:].strip()
        found = DIRECTIVE.match(line.strip())
        if found:
            directives.append(found[1])
        elif line.strip():
            directives = []  # 准备动作必须紧挨着围栏，中间不能夹别的文字
    assert inside is None, f"第 {inside.line} 行的代码块没有关上" if inside else ""
    assert not directives, f"有准备动作后面没有代码块：{directives}"
    return blocks


@dataclass
class Example:
    command: Block
    response: Block

    @property
    def status(self) -> int:
        return int(self.response.info.split()[1])


def examples_of(markdown: str) -> list[Example]:
    """文档里的可执行示例。`bash exec` 后面必须紧跟 `json <状态码>`，后者也不能单独出现。"""
    blocks = blocks_of(markdown)
    found: list[Example] = []
    index = 0
    while index < len(blocks):
        block = blocks[index]
        wanted = (
            re.fullmatch(r"json \d{3}", blocks[index + 1].info) if index + 1 < len(blocks) else None
        )
        if block.info == "bash exec":
            assert wanted, f"第 {block.line} 行的可执行示例后面要紧跟 ```json <状态码> 的期望返回"
            found.append(Example(block, blocks[index + 1]))
            index += 2
            continue
        assert block.directives == [], f"第 {block.line} 行的准备动作后面不是 bash exec 代码块"
        assert not re.fullmatch(r"json \d{3}", block.info), (
            f"第 {block.line} 行的 {block.info} 代码块前面没有对应的 bash exec 示例"
        )
        index += 1
    return found


def table_after(markdown: str, heading: str) -> list[list[str]]:
    """`heading` 那一行下面的第一张表：去掉表头和分隔行，每行拆成去了反引号的单元格。"""
    lines = markdown.splitlines()
    start = lines.index(heading)
    rows: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("|"):
            rows.append(line)
        elif rows:
            break
    assert len(rows) > 2, f"{heading} 下面没有表"
    return [
        [cell.strip().strip("`").strip() for cell in row.strip().strip("|").split("|")]
        for row in rows[2:]
    ]


# ---------------------------------------------------------------- 接口描述


def test_the_exported_description_is_what_the_service_describes_now():
    exported = read(OPENAPI)

    assert exported == apidoc.render(), (
        "docs/api/openapi.json 和服务现在的接口描述不一致：运行 "
        "`uv run python -m clawbot_hub.apidoc` 重新导出"
    )


def test_the_export_command_writes_checks_and_prints(tmp_path, capsys):
    target = tmp_path / "docs" / "openapi.json"

    assert apidoc.main(["--out", str(target), "--check"]) == 1  # 还没有这个文件
    assert "重新导出" in capsys.readouterr().out
    assert apidoc.main(["--out", str(target)]) == 0
    assert read(target) == apidoc.render()
    assert apidoc.main(["--out", str(target), "--check"]) == 0
    target.write_text(read(target).replace("clawbot-hub", "别的名字", 1), encoding="utf-8")
    assert apidoc.main(["--out", str(target), "--check"]) == 1
    assert "重新导出" in capsys.readouterr().out
    capsys.readouterr()
    assert apidoc.main(["--out", "-"]) == 0
    assert capsys.readouterr().out == apidoc.render()


def test_the_export_command_runs_as_a_module_and_the_committed_file_passes_its_check():
    done = subprocess.run(
        [sys.executable, "-m", "clawbot_hub.apidoc", "--check"],
        cwd=ROOT,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 0, done.stdout.decode() + done.stderr.decode()


def test_the_description_lists_exactly_the_endpoints_the_tests_know():
    paths = apidoc.build_openapi()["paths"]

    described = {f"{method.upper()} {path}" for path, ops in paths.items() for method in ops}

    assert described == set(ENDPOINTS)


def test_the_description_says_who_needs_a_key_and_what_comes_back():
    description = apidoc.build_openapi()
    scheme = description["components"]["securitySchemes"]["projectKey"]
    assert (scheme["type"], scheme["scheme"]) == ("http", "bearer")
    error = {"$ref": "#/components/schemas/Error"}
    declared: set[int] = set()
    for path, operations in description["paths"].items():
        for method, operation in operations.items():
            label = f"{method.upper()} {path}"
            assert (operation.get("security") == [{"projectKey": []}]) == (path != "/v1/health"), (
                f"{label}：要不要密钥写得不对"
            )
            responses = operation["responses"]
            assert "422" not in responses, f"{label}：框架自带的 422 不会出现（一律是 400）"
            success = responses["200"]["content"]["application/json"]["schema"]
            assert "$ref" in success, label
            for status, response in responses.items():
                if status != "200":
                    assert response["content"]["application/json"]["schema"] == error, label
                    declared.add(int(status))
    assert declared <= {status for status, _ in error_table()}


# ---------------------------------------------------------------- 接口、文档、合约测试


def test_the_api_guide_documents_exactly_the_endpoints_there_are():
    text = read(API_DOC)

    headings = re.findall(r"(?m)^## ((?:GET|POST) /v1/\S+)$", text)
    overview = [row[0] for row in table_after(text, "## 接口一览")]

    assert sorted(headings) == sorted(ENDPOINTS)
    assert sorted(overview) == sorted(ENDPOINTS)


def test_every_endpoint_has_an_executable_example_in_the_api_guide():
    examples = examples_of(read(API_DOC))

    called = {" ".join(endpoint_of(example.command.text) or ()) for example in examples}

    assert set(ENDPOINTS) <= called
    assert len(examples) >= 15  # 少了多半是解析坏了


def covered_endpoints() -> dict[str, list[str]]:
    """每个接口有哪些合约测试（标了 `@covers`）；同时要求这几个测试文件里的每条测试都标了。"""
    covered: dict[str, list[str]] = {endpoint: [] for endpoint in ENDPOINTS}
    here = Path(__file__)
    for path in sorted(here.parent.glob("test_*.py")):
        if path == here:
            continue
        module = importlib.import_module(f"tests.contract.{path.stem}")
        tests = [
            (name, value)
            for name, value in vars(module).items()
            if name.startswith("test_") and inspect.isfunction(value)
        ]
        assert tests, f"{path.name} 里没有测试"
        for name, value in tests:
            assert getattr(value, "covers", ()), f"{path.name}::{name} 没有标 @covers(...)"
            for endpoint in value.covers:
                covered[endpoint].append(f"{path.stem}::{name}")
    return covered


def test_every_endpoint_has_a_contract_test():
    covered = covered_endpoints()

    assert [endpoint for endpoint, tests in covered.items() if not tests] == []
    assert sum(len(tests) for tests in covered.values()) >= 59  # 标记被丢掉的话，这里会少


# ---------------------------------------------------------------- 可执行示例


def queued(rig) -> int:
    row = rig.hub.db.conn().execute("SELECT COUNT(*) FROM inbox WHERE state = 'queued'").fetchone()
    return row[0]


def prepare(rig, directive: str) -> None:
    action, _, argument = directive.partition(" ")
    if action == "say":
        rig.present()  # 项目在取话，使用者的话才会进它的队列，服务不会回"现在没在线"
        before = queued(rig)
        rig.say(argument)
        wait_for(lambda: queued(rig) > before)
    elif action == "closed":
        rig.clock.advance(DAY + 3600)
    else:
        raise AssertionError(f"不认识的准备动作 {directive!r}")


def run_command(command: str, env: dict[str, str]) -> tuple[int, str]:
    """运行一条示例命令，返回（HTTP 状态码，返回的正文）。命令末尾补上 curl 的 `-w`，让它在正文
    后面另起一行打出状态码。"""
    script = f"{command.rstrip()} -w '\\n%{{http_code}}'"
    done = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, timeout=60, check=False
    )
    if done.returncode != 0:
        raise AssertionError(
            f"命令退出码 {done.returncode}：{done.stderr.decode(errors='replace')}"
        )
    body, _, code = done.stdout.decode().rpartition("\n")
    return int(code), body


# 每次运行都不同、只比类型不比值的字段（列表里的每一项也一样）
VOLATILE = frozenset(
    {
        "id",
        "reply_handle",
        "at",
        "at_ms",
        "received_at",
        "started_at",
        "ends_at",
        "window_ends_at",
        "seconds_left",
        "left",
        "used",
        "free",
        "replies_used",
        "guaranteed",
        "cap",
        "version",
        "unknown",
    }
)
# 中文说明：数字（日期、条数）换成 N 以后逐字比较
WORDS = frozenset({"reason", "message"})


def kind_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


def differences(
    documented: Any, actual: Any, path: str = "$", *, loose: bool = False, words: bool = False
) -> list[str]:
    """文档里写的返回和服务实际的返回有哪些不同（空列表就是一致）。"""
    if kind_of(documented) != kind_of(actual):
        return [f"{path}：文档写的是 {kind_of(documented)}，服务返回的是 {actual!r}"]
    found: list[str] = []
    if isinstance(documented, dict):
        missing = sorted(set(documented) - set(actual))
        unwritten = sorted(set(actual) - set(documented))
        if missing:
            found.append(f"{path}：文档里有、服务没返回的字段 {missing}")
        if unwritten:
            found.append(f"{path}：服务返回了、文档没写的字段 {unwritten}")
        for key in sorted(set(documented) & set(actual)):
            found += differences(
                documented[key],
                actual[key],
                f"{path}.{key}",
                loose=loose or key in VOLATILE,
                words=key in WORDS,
            )
    elif isinstance(documented, list):
        if len(documented) != len(actual):
            return [f"{path}：文档写了 {len(documented)} 项，服务返回了 {len(actual)} 项"]
        for index, (written, real) in enumerate(zip(documented, actual, strict=True)):
            found += differences(written, real, f"{path}[{index}]", loose=loose)
    elif not loose:
        if words and isinstance(documented, str):
            documented, actual = re.sub(r"\d+", "N", documented), re.sub(r"\d+", "N", actual)
        if documented != actual:
            found.append(f"{path}：文档写的是 {documented!r}，服务返回的是 {actual!r}")
    return found


def is_type(value: Any, kind: str) -> bool:
    return {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
    }[kind]


def fits(
    value: Any, schema: dict[str, Any], components: dict[str, Any], path: str = "$"
) -> list[str]:
    """返回的 JSON 符合接口描述里的结构吗（只支持这份描述用到的那几个关键字）。"""
    if "$ref" in schema:
        return fits(
            value, components["schemas"][schema["$ref"].rsplit("/", 1)[1]], components, path
        )
    wanted = schema.get("type")
    kinds = [wanted] if isinstance(wanted, str) else list(wanted or [])
    if kinds and not any(is_type(value, kind) for kind in kinds):
        return [f"{path}：描述写的类型是 {kinds}，实际是 {value!r}"]
    found: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        found.append(f"{path}：{value!r} 不在描述列的取值 {schema['enum']} 里")
    if schema.get("format") == "date-time" and isinstance(value, str):
        try:
            datetime.fromisoformat(value)
        except ValueError:
            found.append(f"{path}：{value!r} 不是 ISO-8601 时间")
    if "minimum" in schema and isinstance(value, (int, float)) and value < schema["minimum"]:
        found.append(f"{path}：{value!r} 小于描述写的最小值 {schema['minimum']}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        found += [
            f"{path}：缺少描述里必需的字段 {key}"
            for key in schema.get("required", [])
            if key not in value
        ]
        for key, item in value.items():
            if key not in properties:
                found.append(f"{path}：描述里没有字段 {key}")
            else:
                found += fits(item, properties[key], components, f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            found += fits(item, schema["items"], components, f"{path}[{index}]")
    return found


def endpoint_of(command: str) -> tuple[str, str] | None:
    """示例命令调的是哪个接口：（方法, 路径）；调的不是 `$HUB/v1/…` 就是 `None`。"""
    path = re.search(r"\$HUB(/v1/[a-z/]+)", command)
    if path is None:
        return None
    return ("POST" if re.search(r"-X\s+POST", command) else "GET"), path[1]


def described_problems(description: dict[str, Any], example: Example, actual: Any) -> list[str]:
    """示例的实际返回对得上接口描述吗：命令调的接口有描述的，按描述里这个状态码的返回结构；用错了
    方法或路径、框架自己回的错，按统一的错误格式。"""
    components = description["components"]
    error = {"$ref": "#/components/schemas/Error"}
    status = example.status
    called = endpoint_of(example.command.text)
    operation = description["paths"].get(called[1], {}).get(called[0].lower()) if called else None
    if operation is None:
        if status == 200:
            return [f"接口描述里没有 {called}"]
        return fits(actual, error, components)
    declared = operation["responses"].get(str(status))
    if declared is None:
        return [f"接口描述没有声明 {' '.join(called)} 会返回 {status}"]
    return fits(actual, declared["content"]["application/json"]["schema"], components)


def test_every_executable_example_runs_and_the_service_answers_as_the_guide_says(rig, tmp_path):
    if shutil.which("curl") is None:
        pytest.skip("没有 curl")
    description = apidoc.build_openapi()
    variables: dict[str, str] = {}
    problems: list[str] = []
    ran = 0
    for example in examples_of(read(API_DOC)):
        where = f"docs/api/v1.md 第 {example.command.line} 行（{example.command.heading}）"
        used = set(re.findall(r"\$([A-Z_]+)", example.command.text))
        unknown = sorted(used - {"HUB", "KEY"} - set(variables))
        if unknown:
            problems.append(f"{where}：用到了前面的示例还没取到的变量 {unknown}")
            continue
        for directive in example.command.directives:
            prepare(rig, directive)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path),  # 不读到别人的 .curlrc；也不带任何代理设置
            "HUB": rig.url,
            "KEY": rig.key("alpha"),
            **variables,
        }
        try:
            status, body = run_command(example.command.text, env)
            actual = json.loads(body)
        except (AssertionError, ValueError, subprocess.TimeoutExpired) as exc:
            problems.append(f"{where}：{exc}")
            continue
        ran += 1
        if isinstance(actual, dict) and actual.get("items"):  # 取话的示例给后面的示例留下句柄和编号
            variables["HANDLE"] = actual["items"][0]["reply_handle"]
            variables["ITEM_ID"] = actual["items"][0]["id"]
        if status != example.status:
            problems.append(
                f"{where}：文档写的状态码是 {example.status}，服务返回了 {status}：{body[:200]}"
            )
        problems += [
            f"{where}：{p}" for p in differences(json.loads(example.response.text), actual)
        ]
        problems += [f"{where}：{p}" for p in described_problems(description, example, actual)]
    assert not problems, "\n" + "\n".join(problems)
    assert ran >= 15


def test_the_example_grammar_is_strict_about_a_missing_response_or_a_stray_one():
    with pytest.raises(AssertionError, match="期望返回"):
        examples_of("## x\n\n```bash exec\ncurl\n```\n\n文字\n")
    with pytest.raises(AssertionError, match="前面没有对应"):
        examples_of("## x\n\n```json 200\n{}\n```\n")
    with pytest.raises(AssertionError, match="准备动作"):
        examples_of("## x\n\n<!-- exec: closed -->\n\n```bash\ncurl\n```\n")
    with pytest.raises(AssertionError, match="准备动作"):
        examples_of("## x\n\n<!-- exec: closed -->\n")
    [example] = examples_of(
        "## x\n\n<!-- exec: say 你好 -->\n```bash exec\ncurl\n```\n\n```json 404\n{}\n```\n"
    )
    assert example.command.directives == ["say 你好"] and example.status == 404
    assert example.command.heading == "x"


def test_the_checker_itself_notices_a_different_answer():
    written = {
        "status": "sent",
        "id": "o_1",
        "parts": 1,
        "reason": "窗口 10月12日 08:30",
        "unknown": [],
    }

    assert differences(written, dict(written, id="o_2", reason="窗口 9月5日 09:05")) == []
    assert differences(written, dict(written, status="failed"))[0].startswith("$.status")
    assert differences(written, dict(written, parts=2))[0].startswith("$.parts")
    assert differences(written, dict(written, reason="别的原因"))[0].startswith("$.reason")
    assert differences(written, dict(written, id=None))[0].startswith("$.id")
    assert differences(written, dict(written, unknown=["m_1"]))[0].startswith("$.unknown")
    assert differences(written, dict(written, extra=1)) == [
        "$：服务返回了、文档没写的字段 ['extra']"
    ]
    assert differences(written, {"status": "sent"})[0].startswith("$：文档里有、服务没返回的字段")
    components = {
        "schemas": {
            "Thing": {"type": "object", "required": ["a"], "properties": {"a": {"enum": ["x"]}}}
        }
    }
    thing = {"$ref": "#/components/schemas/Thing"}
    assert fits({"a": "x"}, thing, components) == []
    assert fits({"a": "y"}, thing, components)[0].startswith("$.a")
    assert fits({}, thing, components) == ["$：缺少描述里必需的字段 a"]
    assert fits({"a": "x", "b": 1}, thing, components) == ["$：描述里没有字段 b"]
    assert fits([], thing, components)[0].startswith("$：描述写的类型")


def test_at_and_at_ms_in_the_inbox_example_are_the_same_instant():
    checked = 0
    for example in examples_of(read(API_DOC)):
        body = json.loads(example.response.text)
        for item in body.get("items", []) if isinstance(body, dict) else []:
            at = datetime.fromisoformat(item["at"])
            assert at.timestamp() * 1000 == item["at_ms"], f"第 {example.response.line} 行"
            assert datetime.fromisoformat(item["received_at"]) >= at
            checked += 1
    assert checked >= 1


def test_the_status_example_adds_up():
    [status] = [
        json.loads(example.response.text)
        for example in examples_of(read(API_DOC))
        if example.command.heading == "GET /v1/status"
    ]
    window, quota = status["window"], status["quota"]

    started, ends = (
        datetime.fromisoformat(window["started_at"]),
        datetime.fromisoformat(window["ends_at"]),
    )
    assert (ends - started).total_seconds() == Config().quota.window_hours * 3600
    assert window["seconds_left"] <= Config().quota.window_hours * 3600
    assert quota["limit"] == Config().quota.window_limit
    assert quota["free"] == quota["limit"] - quota["used"]
    assert status["service"]["send_timeout_seconds"] == Config().send.timeout_seconds
    assert status["service"]["inbox_online_seconds"] == Config().inbox.online_seconds


# ---------------------------------------------------------------- 表和默认数字


def test_the_status_table_lists_the_statuses_the_description_lists():
    description = apidoc.build_openapi()
    listed = description["components"]["schemas"]["SendResult"]["properties"]["status"]["enum"]
    rows = table_after(read(API_DOC), "### `status` 取值和项目该怎么办")

    assert sorted(row[0] for row in rows) == sorted(listed)
    for row in rows:
        assert hub_client.SendResult(status=row[0]).ok == (row[2] == "是"), row[0]
    assert hub_client.SendResult(status="from-the-future").needs_fallback  # 不认识的也走兜底


def test_the_reason_code_table_is_the_reason_table_of_the_service():
    rows = table_after(read(API_DOC), "## 原因代码")

    documented = {row[0]: set(row[1].split(" / ")) for row in rows}

    assert len(documented) == len(rows)  # 没有重复的代码
    assert documented == APPEARS_AS


def error_table() -> list[tuple[int, str]]:
    return [(int(row[0]), row[1]) for row in table_after(read(API_DOC), "## 错误代码")]


def error_pairs_in_source() -> set[tuple[int, str]]:
    """服务的源码里写明的（HTTP 状态码, 错误代码）：`ApiError(…)`、`error_response(…)`、
    字段校验失败的 `SendInvalid(…)`（回 400）和框架错误的对照表。"""
    found: set[tuple[int, str]] = set()
    for path in SRC.glob("*.py"):
        for node in ast.walk(ast.parse(read(path))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                args = node.args
                if node.func.id in ("ApiError", "error_response") and len(args) >= 2:
                    first, second = args[0], args[1]
                    if isinstance(first, ast.Constant) and isinstance(second, ast.Constant):
                        found.add((first.value, second.value))
                if node.func.id == "SendInvalid" and isinstance(args[0], ast.Constant):
                    found.add((400, args[0].value))
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "FRAMEWORK_ERRORS"
                for target in node.targets
            ):
                assert isinstance(node.value, ast.Dict)
                for key, value in zip(node.value.keys, node.value.values, strict=True):
                    assert isinstance(key, ast.Constant) and isinstance(value, ast.Tuple)
                    first = value.elts[0]
                    assert isinstance(first, ast.Constant)
                    found.add((key.value, first.value))
    return found


def test_the_error_table_is_the_error_codes_the_source_gives():
    documented = error_table()

    assert len(set(documented)) == len(documented)
    assert set(documented) == error_pairs_in_source()
    assert len(documented) == 16


def test_the_numbers_the_guide_quotes_are_the_defaults_of_the_service():
    config = Config()
    text = read(API_DOC)
    words = "、".join(f'"{word}"' for word in config.routing.confirm_words)
    expected = [
        f"`http://{config.server.host}:{config.server.port}`",
        f"每个项目每分钟 {config.send.rate_per_minute} 次",
        f"请求体最多 {api.MAX_BODY_BYTES // 1024} KiB",
        f"最多 {config.send.max_chars} 个字符",
        f"每条的字数上限 {config.send.part_chars}",
        f"{config.send.dedupe_hours} 小时内同一个键只发一次",
        f"{config.reply.handle_minutes} 分钟内有效，最多回复 {config.reply.max_per_handle} 次",
        f"默认 {config.routing.confirm_seconds} 秒",
        f"使用者回{words}",
        f"1 到 {api.CONFIRM_SECONDS_MAX}",
        f"不写取 {config.inbox.wait_seconds}；超过 {config.inbox.max_wait}",
        f"不写取 {api.INBOX_DEFAULT_MAX}；超过 {api.INBOX_MAX_ITEMS} 的按 {api.INBOX_MAX_ITEMS} 算",
        f"`lease_seconds`（{config.inbox.lease_seconds}）秒内没有确认",
        f"最多给 {config.inbox.max_attempts} 次",
        f"最近 {config.inbox.online_seconds} 秒内取过话",
        f"话留 {config.inbox.retention_minutes} 分钟",
        f"一次最多 {api.ACK_MAX_IDS} 个",
    ]

    assert [fragment for fragment in expected if fragment not in text] == []


# ---------------------------------------------------------------- 接入指南


def guide_python() -> list[Block]:
    return [block for block in blocks_of(read(GUIDE)) if block.info == "python"]


def test_the_guide_exists_and_has_the_steps_of_connecting_a_project():
    text = read(GUIDE)

    headings = re.findall(r"(?m)^## (.+)$", text)

    assert len(text) > 3000
    for topic in ("登记", "密钥", "客户端", "发消息", "取话", "前缀", "不用 Python", "出了问题"):
        assert any(topic in heading for heading in headings), f"接入指南缺少「{topic}」那一节"


def test_the_guide_has_the_conventions_the_spec_asks_for():
    text = read(GUIDE)
    conventions = {
        "登记": ["hubctl project add", "--guaranteed", "--cap", "--prefix", "--push-only"],
        "放密钥": ["CLAWBOT_HUB_KEY", ".env", "不进 git"],
        "发消息": ["hub.send", "dedupe_key"],
        "稳定的去重键": ["brief-2026-10-12", "重跑"],
        "微信发不出去走 Bark": [
            "needs_fallback",
            "Bark",
            "微信没发出去：",
            "HubUnavailable",
            "unconfirmed",
            "partial",
            "rejected",
            "failed",
            "sent",
            "skipped",
        ],
        "可能已经到了": ["maybe_delivered", "possibly_sent", "可能已经到了"],
        "取话循环和心跳": [
            "run_inbox",
            "心跳",
            "online_seconds",
            "GET /v1/inbox?wait=0&max=0",
            "同一时间只能有一个",
        ],
        "取话要幂等": ["幂等", "至少送达一次"],
        "回复和等确认": ["reply_handle", "awaiting_confirmation=True", "30 分钟"],
        "via 是 confirm": ["`via`", "`confirm`", "自己判断"],
        "前缀和默认项目指令同名": ["同名", "默认项目", "分不清"],
        "不自己发微信": ["不要自己向使用者的微信发"],
        "不用 Python": ["docs/api/v1.md", "docs/api/openapi.json"],
    }

    missing = {
        name: [phrase for phrase in phrases if phrase not in text]
        for name, phrases in conventions.items()
    }

    assert {name: phrases for name, phrases in missing.items() if phrases} == {}


def test_the_python_examples_in_the_guide_parse_as_python_39():
    blocks = guide_python()

    assert len(blocks) >= 3
    for block in blocks:
        try:
            ast.parse(block.text, feature_version=(3, 9))
        except SyntaxError as exc:
            raise AssertionError(f"接入指南第 {block.line} 行的 Python 例子读不通：{exc}") from exc


def test_the_python_examples_in_the_guide_only_use_what_the_client_has():
    known_attributes = {
        *hub_client.SendResult.__dataclass_fields__,
        *hub_client.Inbound.__dataclass_fields__,
        *("ok", "needs_fallback", "maybe_delivered", "message", "http_status"),
        *("possibly_sent", "code", "retry_after"),
    }
    names_used = 0
    for block in guide_python():
        for node in ast.walk(ast.parse(block.text)):
            where = f"接入指南第 {block.line} 行起的例子"
            if isinstance(node, ast.ImportFrom) and node.module == "clawbot_hub_client":
                for alias in node.names:
                    assert hasattr(hub_client, alias.name), f"{where}：客户端里没有 {alias.name}"
                    names_used += 1
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                target = node.func.value
                if isinstance(target, ast.Name) and target.id == "hub":
                    method = getattr(hub_client.HubClient, node.func.attr, None)
                    assert callable(method), f"{where}：客户端没有 hub.{node.func.attr}()"
                    parameters = inspect.signature(method).parameters
                    for keyword in node.keywords:
                        assert keyword.arg in parameters, (
                            f"{where}：hub.{node.func.attr}() 没有参数 {keyword.arg}"
                        )
                    names_used += 1
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id in ("result", "item", "exc"):
                    assert node.attr in known_attributes, f"{where}：{node.value.id}.{node.attr}"
    assert names_used >= 8


def guide_namespace(contains: str, leave_out: str | None = None, **stubs: Any) -> dict[str, Any]:
    """接入指南里含 `contains` 的那个 Python 例子跑一遍（只定义函数、造客户端，不连服务），返回它的
    命名空间。例子里项目自己的函数（`send_bark` 之类）用 `stubs` 代替；`leave_out` 是要去掉的那一行
    的开头（起取话循环的语句：循环由测试自己起，假时钟下要有人往前推时间）。"""
    [block] = [block for block in guide_python() if contains in block.text]
    lines = block.text.splitlines()
    if leave_out is not None:
        kept = [line for line in lines if not line.startswith(leave_out)]
        assert len(kept) == len(lines) - 1, f"例子里应该正好有一行以 {leave_out!r} 开头"
        lines = kept
    namespace: dict[str, Any] = dict(stubs)
    exec(compile("\n".join(lines), str(GUIDE), "exec"), namespace)
    return namespace


class HungHub:
    """读超时：服务有没有把消息发出去，不知道。"""

    def send(self, text: str, **kwargs: Any) -> hub_client.SendResult:
        raise hub_client.HubUnavailable("读超时", possibly_sent=True)


PUSH_CASES = {
    "sent": (lambda rig: None, [], ["微信没发出去"]),
    "unconfirmed": (
        lambda rig: rig.upstream.script("send_text", NoResponse("回话超时", read_timeout=True)),
        ["微信没发出去：", "没有回话", "可能已经到了"],
        [],
    ),
    "partial": (
        lambda rig: rig.upstream.script(
            "send_text", lambda call: DEFAULT, Rejected(200, 12345, "拒")
        ),
        ["微信没发出去：", "发出了 1 条", "可能已经到了"],
        [],
    ),
    "rejected": (
        lambda rig: rig.clock.advance(DAY + 3600),
        ["微信没发出去：对话窗口过期了"],
        ["可能已经到了"],
    ),
    "failed": (
        lambda rig: rig.upstream.set_mode("disconnect"),
        ["微信没发出去：微信 ClawBot 发送失败"],
        ["可能已经到了"],
    ),
    "service-down": (
        lambda rig: hub_client.HubClient("http://127.0.0.1:1", rig.key(), retries=0),
        ["微信没发出去：微信服务不可用："],
        ["可能已经到了"],
    ),
    "read-timeout": (
        lambda rig: HungHub(),
        ["微信没发出去：微信服务不可用：读超时", "可能已经到了"],
        [],
    ),
    "wrong-key": (
        lambda rig: hub_client.HubClient(rig.url, "chub_" + "x" * 43, retries=0),
        ["微信没发出去：微信服务拒绝了请求："],
        ["可能已经到了"],
    ),
}


@pytest.mark.parametrize("case", sorted(PUSH_CASES))
def test_the_push_example_in_the_guide_falls_back_to_bark_as_the_guide_says(rig, case):
    prepare_case, required, forbidden = PUSH_CASES[case]
    barks: list[str] = []
    namespace = guide_namespace("def push(", send_bark=barks.append)
    namespace["hub"] = rig.client(retries=0)
    replacement = prepare_case(rig)
    if replacement is not None:
        namespace["hub"] = replacement
    text = "很长的一段话。" * 400 if case == "partial" else "盘点完成\n共 12 只"

    namespace["push"](text, "pick-1")

    if case == "sent":
        assert barks == [] and rig.hub.texts == [text]
        return
    [bark] = barks
    assert bark.startswith(text + "\n\n微信没发出去：")
    assert [word for word in required if word not in bark] == []
    assert [word for word in forbidden if word in bark] == []


def test_the_inbox_example_in_the_guide_asks_for_a_confirmation_and_acts_on_the_yes(rig):
    executed: list[str] = []
    barks: list[str] = []
    pending: dict[str, str] = {}
    namespace = guide_namespace(
        "def handle(",
        "hub.run_inbox(",
        send_bark=barks.append,
        already_done=lambda item_id: False,
        pending_confirmation=lambda: bool(pending),
        remember_pending_confirmation=lambda text: pending.update(what=text),
        run_confirmed_action=lambda: executed.append(pending.pop("what")),
        handle_command=lambda text: f"收到：{text}",
    )
    namespace["hub"] = rig.client()
    handled: list[str] = []

    def run(item: hub_client.Inbound, client: hub_client.HubClient) -> None:
        namespace["handle"](item)
        handled.append(item.text)  # 回复调用返回了：服务那边“在等确认”的标记也记上了

    alpha = rig.consume("alpha", run)

    rig.say("平仓 600519")
    wait_for(lambda: len(handled) == 1)
    rig.say("对")
    wait_for(lambda: len(handled) == 2)
    rig.say("持仓")
    wait_for(lambda: len(handled) == 3)
    alpha.stop()

    assert rig.hub.texts == ["要平仓吗？回“对”确认", "已执行", "收到：持仓"]
    assert executed == ["平仓 600519"] and barks == []
    assert [(item.text, item.via) for item in alpha.seen] == [
        ("平仓 600519", "default"),
        ("对", "confirm"),
        ("持仓", "default"),
    ]


def test_the_inbox_example_in_the_guide_sends_a_late_reply_to_bark_and_still_acknowledges(rig):
    barks: list[str] = []
    namespace = guide_namespace(
        "def handle(",
        "hub.run_inbox(",
        send_bark=barks.append,
        already_done=lambda item_id: False,
        pending_confirmation=lambda: False,
        handle_command=lambda text: f"收到：{text}",
    )
    namespace["hub"] = rig.client()

    def late(item: hub_client.Inbound, client: hub_client.HubClient) -> None:
        rig.clock.advance(31 * 60)  # 句柄只管 30 分钟
        namespace["handle"](item)

    alpha = rig.consume("alpha", late)

    rig.say("持仓")
    wait_for(lambda: len(barks) == 1)
    alpha.stop()

    assert barks[0].startswith("收到：持仓\n\n微信没发出去：")
    assert rig.hub.texts == []
    rig.clock.advance(2 * 60)
    assert rig.client().inbox(wait=0) == []  # handle 没有抛异常：这句话确认了，不会再给


@pytest.mark.parametrize("path", [API_DOC, GUIDE], ids=lambda p: p.name)
def test_the_links_between_the_docs_point_at_files_that_exist(path: Path):
    links = re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", read(path))

    relative = [link for link in links if not link.startswith(("http://", "https://"))]

    assert relative, f"{path.name} 里应该链到别的文档"
    for link in relative:
        assert (path.parent / link).resolve().exists(), f"{path.name} 链到了不存在的 {link}"


@pytest.mark.parametrize("path", [API_DOC, GUIDE], ids=lambda p: p.name)
def test_the_client_docs_carry_no_secrets_or_personal_paths(path: Path):
    text = read(path)

    assert not re.search(r"/Users/|/home/[a-z]", text), "文档里别写本机的绝对路径"
    assert not re.search(r"chub_[A-Za-z0-9_-]{20,}", text), "文档里别放真的项目密钥"
    assert not re.search(r"[\w.+-]+@(?:gmail|qq|163|outlook)\.", text), "文档里别放邮箱"
    assert not re.search(r"wxid_\w+|@im\.wechat", text), "文档里别放微信号"
