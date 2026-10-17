"""所有测试共用的夹具。

- 联网守卫：`live` 以外的测试只能连本机回环（`tests/support/netguard.py`）
- 家目录隔离：`HOME`、`CLAWBOT_HUB_HOME` 指向临时目录，任何测试都碰不到真实的数据目录
- 代理环境清掉：回环请求不绕代理。环境变量里的代理项一律删掉，再放一个只含回环地址的 `NO_PROXY`：
  环境变量里一个代理项都没有时，Python 会去读系统的代理设置（Mac 上开着代理软件就是
  127.0.0.1:7890，而 `[::1]` 和公网地址都不在系统的例外名单里），请求被代理接走，测试结果就
  取决于这台机器的网络设置了。放了 `NO_PROXY` 以后只看环境变量，系统设置不再参与
- 卡死看门狗：一个测试超过 `HUB_TEST_TIMEOUT` 秒（默认 120）就打印所有线程的栈并退出，
  免得死锁把整个测试静悄悄地挂住
"""

from __future__ import annotations

import faulthandler
import os
from zoneinfo import ZoneInfo

import pytest

from tests.support import netguard
from tests.support.fake_bark import FakeBark
from tests.support.fake_clock import FakeClock
from tests.support.fake_ilink import FakeIlink
from tests.support.fake_upstream import FakeUpstream

PROXY_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)
# 测试里唯一不走代理的名单：本机回环（`[::1]` 带方括号和不带的写法都列上，
# `requests` 和 `urllib` 各认一种）。只有回环：测试里故意设了 `HTTP_PROXY` / `HTTPS_PROXY`
# 去验证代理行为的，目标都是假域名
LOOPBACK_NO_PROXY = "127.0.0.1,localhost,::1,[::1]"


def _is_live(request: pytest.FixtureRequest) -> bool:
    return request.node.get_closest_marker("live") is not None


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAWBOT_HUB_HOME", str(home / ".clawbot-hub"))


@pytest.fixture(autouse=True)
def _network_guard(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    if _is_live(request):
        yield
        return
    for name in PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", LOOPBACK_NO_PROXY)
    monkeypatch.setenv("no_proxy", LOOPBACK_NO_PROXY)
    with netguard.block_network():
        yield


@pytest.fixture(autouse=True)
def _hang_watchdog():
    seconds = float(os.environ.get("HUB_TEST_TIMEOUT", "120"))
    if seconds > 0:
        faulthandler.dump_traceback_later(seconds, exit=True)
    yield
    if seconds > 0:
        faulthandler.cancel_dump_traceback_later()


@pytest.fixture
def clock() -> FakeClock:
    """每个测试一个新的假时钟（sleep 阻塞到 advance；单线程跑到底的测试自己建 auto_sleep 的）。"""
    return FakeClock()


@pytest.fixture
def display_tz() -> ZoneInfo:
    """显示时区（备份目录名里的时间、每天几点备份都按它）。"""
    return ZoneInfo("Asia/Shanghai")


@pytest.fixture
def paths():
    """每个测试一个空的数据目录（临时目录里，0700），还没有 hub.db。"""
    from clawbot_hub.paths import HubPaths, ensure_home, resolve_home

    hub_paths = HubPaths(resolve_home())
    ensure_home(hub_paths)
    return hub_paths


@pytest.fixture
def database(paths, clock: FakeClock, display_tz: ZoneInfo):
    """跑完全部迁移的数据库（`Database`，每个线程各用各的连接）。"""
    from clawbot_hub.db import Database, migrate

    migrate(paths, clock=clock, tz=display_tz)
    db = Database(paths.db)
    yield db
    db.close()


@pytest.fixture
def fake_ilink(clock: FakeClock):
    """本机回环上的假腾讯服务，消息时间戳跟着假时钟。"""
    with FakeIlink(now_ms=clock.now_ms) as server:
        yield server


@pytest.fixture
def fake_bark(clock: FakeClock):
    """本机回环上的假 Bark 服务。"""
    with FakeBark(now_ms=clock.now_ms) as server:
        yield server


@pytest.fixture
def fake_upstream(clock: FakeClock) -> FakeUpstream:
    """进程内的假上游（不走网络，按剧本给出每一种结果），组件测试用它代替 `HttpUpstream`。"""
    return FakeUpstream(clock=clock)
