"""单文件客户端能整份拷进别的项目（任务 D4）：只依赖标准库，Python 3.9 就能跑。

规格 `hub-client`「单文件、只依赖标准库」。除了导入和语法检查，还遍历语法树找 3.10 以后才有的
写法——`ast.parse(feature_version=(3, 9))` 抓不住运行时才出错的那几种（`zip(strict=…)`、
`dataclass(slots=…)`、注解以外的 `X | None`）。
"""

from __future__ import annotations

import ast
import io
import json
import shutil
import subprocess
import sys
import tokenize
from pathlib import Path

import pytest

CLIENT = Path(__file__).resolve().parents[2] / "client" / "clawbot_hub_client.py"
SOURCE = CLIENT.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE, feature_version=(3, 9))


def test_the_file_is_where_the_spec_says_and_is_one_file():
    assert CLIENT.is_file()
    assert [p.name for p in CLIENT.parent.iterdir() if p.suffix == ".py"] == [CLIENT.name]


def test_it_imports_in_a_bare_interpreter_without_site_packages():
    """`-I -S`：不读用户目录、不加载 site-packages，只剩标准库。"""
    code = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('clawbot_hub_client', {str(CLIENT)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "sys.modules['clawbot_hub_client'] = module\n"
        "spec.loader.exec_module(module)\n"
        "print(module.API_VERSION, module.CLIENT_VERSION)\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code], capture_output=True, text=True, timeout=30
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.split()[0] == "1"


def imported_modules() -> set[str]:
    found = set()
    for node in ast.walk(TREE):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "不能有相对导入"
            found.add((node.module or "").split(".")[0])
    return found


def test_every_import_is_from_the_standard_library():
    outside = imported_modules() - set(sys.stdlib_module_names)

    assert outside == set()


def test_the_version_constants_are_strings():
    namespace: dict = {}
    exec(compile(SOURCE, str(CLIENT), "exec"), namespace)  # noqa: S102 - 自己仓库里的文件

    assert namespace["API_VERSION"] == "1"
    assert isinstance(namespace["CLIENT_VERSION"], str) and namespace["CLIENT_VERSION"]


def test_the_first_statement_after_the_docstring_enables_postponed_annotations():
    body = list(TREE.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]  # 文档字符串

    first = body[0]
    assert isinstance(first, ast.ImportFrom) and first.module == "__future__"
    assert [alias.name for alias in first.names] == ["annotations"]


def test_it_has_a_module_docstring():
    assert ast.get_docstring(TREE)


def keyword_names(call: ast.Call) -> set[str]:
    return {keyword.arg for keyword in call.keywords if keyword.arg}


def test_no_syntax_from_python_3_10_or_later():
    problems = []
    for node in ast.walk(TREE):
        if isinstance(node, ast.Match):
            problems.append(f"第 {node.lineno} 行：match 语句")
        if isinstance(node, ast.TryStar):
            problems.append(f"第 {node.lineno} 行：except*")
    assert problems == []


def callee_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def test_no_dataclass_with_slots_or_kw_only_and_no_zip_with_strict():
    problems = []
    for node in ast.walk(TREE):
        if isinstance(node, ast.Call):
            name = callee_name(node)
            if name == "dataclass" and keyword_names(node) & {"slots", "kw_only"}:
                problems.append(f"第 {node.lineno} 行：dataclass 带 slots= 或 kw_only=")
            if name == "zip" and "strict" in keyword_names(node):
                problems.append(f"第 {node.lineno} 行：zip 带 strict=")
    assert problems == []


def annotation_nodes() -> set[int]:
    """注解位置上所有节点的编号（有 `from __future__ import annotations`，那里不会求值）。"""
    ids: set[int] = set()

    def mark(annotation: ast.AST | None) -> None:
        if annotation is not None:
            ids.update(id(inner) for inner in ast.walk(annotation))

    for node in ast.walk(TREE):
        if isinstance(node, ast.AnnAssign):
            mark(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            mark(node.returns)
            arguments = node.args
            for arg in (
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
                arguments.vararg,
                arguments.kwarg,
            ):
                if arg is not None:
                    mark(arg.annotation)
    return ids


def test_no_union_with_a_bar_outside_annotations():
    """`int | None` 写在注解里没事（不求值），写在别处（`isinstance` 里、别名）3.9 会出错。"""
    inside = annotation_nodes()
    problems = [
        f"第 {node.lineno} 行"
        for node in ast.walk(TREE)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr) and id(node) not in inside
    ]
    assert problems == []


def test_the_checker_itself_would_notice_what_it_looks_for():
    """检查器本身：把 3.10 才有的写法放进去，每一种都得被抓到（不然上面几条什么都查不出来）。"""
    bad = (
        "from dataclasses import dataclass\n"
        "@dataclass(slots=True)\n"
        "class A:\n    x: int\n"
        "y = zip([1], [2], strict=True)\n"
        "z = isinstance(1, int | str)\n"
        "alias = int | None\n"
    )
    tree = ast.parse(bad)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    names = {callee_name(c): keyword_names(c) for c in calls}
    assert names["dataclass"] == {"slots"} and names["zip"] == {"strict"}
    bars = [n for n in ast.walk(tree) if isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr)]
    assert len(bars) == 2


def f_string_problems(source: str) -> list[str]:
    """3.12 才许的 f 字符串写法（PEP 701）：表达式里再用外层的引号、表达式里有反斜杠或注释、
    单引号 f 字符串里的表达式换行。`ast.parse(feature_version=…)` 查不出，3.9 上却是语法错误。"""
    problems = []
    delimiters: list[str] = []  # 外面各层 f 字符串的引号（`"`、`'`、`\"\"\"`、`\'\'\'`）
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        line = token.start[0]
        if token.type == tokenize.FSTRING_START:
            delimiter = token.string.lstrip("rRfFbBuU")
            for outer in delimiters:
                clash = outer[0] in delimiter if len(outer) == 1 else delimiter == outer
                if clash:
                    problems.append(f"第 {line} 行：f 字符串的表达式里用了外层的引号")
            delimiters.append(delimiter)
        elif token.type == tokenize.FSTRING_END:
            delimiters.pop()
        elif delimiters and token.type != tokenize.FSTRING_MIDDLE:
            if token.type == tokenize.STRING:
                inner = token.string.lstrip("rRfFbBuU")
                for outer in delimiters:
                    clash = outer[0] in inner if len(outer) == 1 else inner.startswith(outer)
                    if clash:
                        problems.append(f"第 {line} 行：f 字符串的表达式里用了外层的引号")
                if "\\" in token.string:
                    problems.append(f"第 {line} 行：f 字符串的表达式里有反斜杠")
            elif token.type == tokenize.COMMENT:
                problems.append(f"第 {line} 行：f 字符串的表达式里有注释")
            elif token.type in (tokenize.NL, tokenize.NEWLINE) and len(delimiters[-1]) == 1:
                problems.append(f"第 {line} 行：单引号 f 字符串的表达式里换行")
    return problems


def test_no_f_string_uses_what_only_python_3_12_allows():
    assert f_string_problems(SOURCE) == []


@pytest.mark.parametrize(
    "bad",
    [
        'x = f"{d["k"]}"\n',
        "x = f'{d['k']}'\n",
        'x = f"{"\\n".join(z)}"\n',
        'x = f"{  # 注释\n  y}"\n',
        'x = f"{\n y}"\n',
        'x = f"a{f"b{c}"}"\n',
        'x = f"{d["""k"""]}"\n',
    ],
)
def test_the_f_string_checker_notices_each_kind_of_py312_only_writing(bad):
    assert f_string_problems(bad) != []


@pytest.mark.parametrize(
    "fine",
    [
        "x = f\"{d['k']}\"\n",
        "x = f'{d[\"k\"]}'\n",
        'x = f"""{d["k"]}"""\n',
        'x = f"a{b}c" "d{e}"\n',
        'x = f"{a!r:>{width}}"\n',
        'x = f"{{没有表达式}}"\n',
        'x = f"{y}\\n"\n',
        'x = f"""{\n y}"""\n',
    ],
)
def test_the_f_string_checker_lets_the_old_writing_through(fine):
    assert f_string_problems(fine) == []


def older_pythons() -> list[str]:
    """机器上找得到的 3.9 到 3.11 的解释器（Mac 自带的 `/usr/bin/python3` 就是 3.9）。"""
    found: dict[str, str] = {}
    for name in ("python3.9", "python3.10", "python3.11", "/usr/bin/python3"):
        path = shutil.which(name)
        if path is None:
            continue
        probe = subprocess.run(
            [path, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        version = probe.stdout.strip()
        if probe.returncode == 0 and version in ("3.9", "3.10", "3.11"):
            found.setdefault(version, path)
    return list(found.values())


OLD_PYTHON_SESSION = """
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("clawbot_hub_client", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules["clawbot_hub_client"] = module
spec.loader.exec_module(module)
client = module.HubClient(sys.argv[2], sys.argv[3])
sent = client.send("你好 ✓", dedupe_key="k-1")
items = client.inbox(wait=0, max=3)
print(json.dumps([sys.version_info[:2], client.health()["api"], sent.status, sent.ok,
                  [item.id for item in items], client.ack([item.id for item in items])]))
"""


def test_it_really_runs_under_the_oldest_python_on_this_machine():
    """能找到 3.9 到 3.11 的解释器就真的用它跑一遍；找不到（比如开发机上只有 3.12）就跳过。"""
    from tests.client.conftest import KEY, FakeHub, inbound, inbox_body
    from tests.support.fakehttp import Reply

    pythons = older_pythons()
    if not pythons:
        pytest.skip("这台机器上没有 3.9 到 3.11 的 Python")

    for python in pythons:
        with FakeHub() as hub:
            hub.script("inbox", Reply(body=inbox_body(inbound("in_1"))))
            result = subprocess.run(
                [python, "-I", "-S", "-c", OLD_PYTHON_SESSION, str(CLIENT), hub.base_url, KEY],
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert result.returncode == 0, result.stderr
            version, api, status, ok, ids, acked = json.loads(result.stdout)
            assert version[0] == 3 and 9 <= version[1] <= 11
            assert (api, status, ok, ids, acked) == ("1", "sent", True, ["in_1"], 1)
            assert hub.requests_to("messages")[0].json["dedupe_key"] == "k-1"


@pytest.mark.parametrize(
    "forbidden", ["requests", "urllib3", "yaml", "typer", "fastapi", "pydantic", "httpx", "attr"]
)
def test_no_third_party_name_is_imported(forbidden):
    assert forbidden not in imported_modules()
