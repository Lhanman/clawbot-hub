"""发送测试共用的夹具和小函数。"""

from __future__ import annotations

import pytest

from clawbot_hub.config import Config
from clawbot_hub.upstream.base import NoResponse
from tests.support.fake_clock import FakeClock
from tests.support.fake_upstream import DEFAULT
from tests.support.hub import Hub

__all__ = ["long_text", "stamp", "takes"]


@pytest.fixture
def clock() -> FakeClock:
    """重试的间隔要睡 1 秒、2 秒：单线程从头跑到尾的发送测试用 `sleep` 立刻返回的假时钟。"""
    return FakeClock(auto_sleep=True)


@pytest.fixture
def make_hub(paths, database, clock, fake_upstream):
    """造一整套（`tests/support/hub.py`）；要改配置或换零件时传进来。"""

    def build(config: Config | None = None, **sender_kwargs) -> Hub:
        return Hub(paths, database, clock, fake_upstream, config, **sender_kwargs)

    return build


@pytest.fixture
def hub(make_hub) -> Hub:
    return make_hub()


def stamp(clock, log: list, outcome=DEFAULT):
    """假上游的剧本函数：把这一次调用的时刻（假时钟的毫秒）记进 `log`，然后回 `outcome`。"""

    def record(call):
        log.append(clock.now_ms())
        return outcome

    return record


def takes(clock, seconds: float, outcome=DEFAULT):
    """假上游的剧本函数：要 `seconds` 秒（假时钟）才回话，回 `outcome`。调用者给的超时
    （`timeout` 参数）不够就在超时那一刻读超时：回 `NoResponse`。"""

    def slow(call):
        timeout = call.args["timeout"]
        if seconds > timeout:
            clock.sleep(timeout)
            return NoResponse("读超时", read_timeout=True)
        clock.sleep(seconds)
        return outcome

    return slow


def long_text(parts: int, *, title: str = "盘点", config: Config | None = None) -> str:
    """按默认配置恰好拆成 `parts` 条的文字：每行 50 个字，第一行是标题。"""
    from clawbot_hub.textutil import build_parts

    cfg = config or Config()
    lines = [f"{number:04d}" + "字" * 45 for number in range(40 * parts)]
    for count in range(1, len(lines) + 1):
        text = title + "\n" + "\n".join(lines[:count])
        if len(build_parts(text, cfg.send.part_chars, cfg.quota.window_limit)) == parts:
            return text
    raise AssertionError(f"造不出恰好 {parts} 条的文字")
