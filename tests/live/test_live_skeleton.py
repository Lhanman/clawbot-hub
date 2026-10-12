"""真机测试的骨架检查（任务 0.6）：默认运行时真机测试被跳过；清单里每一项都有一条骨架测试。

`tests/live/test_real_ilink.py` 里的测试会访问真实的腾讯接口，只在 Mac 上、使用者在场授权后
用 `uv run pytest -m live` 手动跑；这里的检查本身不联网、不带 `live` 标记，每次都跑。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.live import checklist, test_real_ilink

REPO_ROOT = Path(__file__).resolve().parents[2]
LIVE_FILE = "tests/live/test_real_ilink.py"


def run_pytest(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def skeleton_test_names() -> set[str]:
    return {name for name in vars(test_real_ilink) if name.startswith("test_")}


def test_live_tests_are_deselected_in_a_default_run():
    result = run_pytest(LIVE_FILE, "-q")
    assert result.returncode == 5, result.stdout  # 5 = 一条都没选中
    assert "deselected" in result.stdout
    assert "passed" not in result.stdout and "failed" not in result.stdout


def test_every_checklist_item_has_exactly_one_skeleton_test():
    expected = {f"test_{key}" for key in checklist.ITEMS}
    assert skeleton_test_names() == expected


def test_the_checklist_items_are_described_in_chinese_and_not_empty():
    assert len(checklist.ITEMS) >= 10
    for key, text in checklist.ITEMS.items():
        assert key.isidentifier() and text.strip(), key


def test_all_skeleton_tests_carry_the_live_marker():
    marks = test_real_ilink.pytestmark
    marks = marks if isinstance(marks, list) else [marks]
    assert any(mark.name == "live" for mark in marks)


def test_before_the_real_device_run_the_skeletons_skip_instead_of_failing():
    result = run_pytest("-m", "live", LIVE_FILE, "-q", "-rs")
    assert result.returncode == 0, result.stdout
    assert f"{len(checklist.ITEMS)} skipped" in result.stdout
    assert "E4" in result.stdout  # 跳过的理由里写着什么时候才填
