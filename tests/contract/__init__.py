"""合约测试（任务 D5）：项目这一侧能用到的全部行为。见 `conftest.py`。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# 服务的全部接口（方法 + 路径）。`test_docs_match.py` 拿它和 OpenAPI 描述对照，再检查每个接口
# 都有至少一条标了 `@covers(...)` 的合约测试
ENDPOINTS = (
    "GET /v1/health",
    "GET /v1/status",
    "POST /v1/messages",
    "POST /v1/replies",
    "GET /v1/inbox",
    "POST /v1/inbox/ack",
)


def covers(*endpoints: str) -> Callable[[Any], Any]:
    """标明这条测试覆盖了哪几个接口，例如 `@covers("POST /v1/messages")`。"""
    unknown = set(endpoints) - set(ENDPOINTS)
    assert not unknown, f"不认识的接口：{sorted(unknown)}"

    def mark(test: Any) -> Any:
        test.covers = (*getattr(test, "covers", ()), *endpoints)
        return test

    return mark
