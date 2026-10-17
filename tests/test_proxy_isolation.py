"""测试里的请求不受这台机器的系统代理影响（第一次在 Mac 上跑整套测试时发现的）。

Python 的 `urllib.request.getproxies()` 在环境变量里一个代理项都没有时，会去读系统的代理设置
（macOS 的"网络"偏好设置，经 `_scproxy`）。Mac 上开着代理软件，系统设置就是 127.0.0.1:7890，
例外名单里通常只有 127.0.0.1、localhost：连 `[::1]` 的请求被代理接走、回了 502，连公网地址
（本该被联网守卫拦下）也被接走。云里（Linux）没有这条路，所以这里在子进程里装一个假的 `_scproxy`、
让 `urllib.request` 按 macOS 的样子导入，模拟出同样的系统设置：

- 对照：环境变量里没有代理项时，`[::1]` 和公网地址确实被模拟的系统代理接走；
- 测试进程和测试起的子进程的环境（`tests/conftest.py`、`child_env()`）下，四个地址都不走代理。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from tests.process.conftest import child_env

SYSTEM_PROXY = "http://127.0.0.1:7890"

URLS = {
    "ipv4": "http://127.0.0.1:80/",
    "name": "http://localhost:80/",
    "ipv6": "http://[::1]:80/",
    "public": "http://example.com/",
}

# 假的 macOS：系统代理 127.0.0.1:7890，例外名单 127.0.0.1、localhost、*.local（和 Mac 上看到的
# 一样，没有 [::1]）。先导入 urllib.request 会用到的模块，再临时把 sys.platform 改成 darwin
# 导入它，导完立刻改回来；之后 `requests` 用的就是 urllib 在 macOS 上的那一套
# （`getproxies`、`proxy_bypass`）
PROBE = f"""
import http.client, json, os, socket, ssl, subprocess, sys, types

real_platform = sys.platform
sys.platform = "darwin"
fake = types.ModuleType("_scproxy")
fake._get_proxy_settings = lambda: {{
    "exclude_simple": 0,
    "exceptions": ["127.0.0.1", "localhost", "*.local"],
}}
fake._get_proxies = lambda: {{"http": "{SYSTEM_PROXY}", "https": "{SYSTEM_PROXY}"}}
sys.modules["_scproxy"] = fake
try:
    import urllib.request
finally:
    sys.platform = real_platform

import requests.utils

urls = {URLS!r}
print(json.dumps({{
    name: requests.utils.select_proxy(url, requests.utils.get_environ_proxies(url))
    for name, url in urls.items()
}}))
"""


def proxies_seen_by_requests(source: dict[str, str], keep: tuple[str, ...] = ()) -> dict:
    """`requests` 对四个地址各会选哪个代理。环境像一台没设过代理变量的 Mac：名字以 `_proxy`
    结尾的变量一律不留（云里的环境有 `GLOBAL_AGENT_HTTPS_PROXY` 之类），`keep` 里的除外。"""
    env = {k: v for k, v in source.items() if k in keep or not k.lower().endswith("_proxy")}
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_the_simulated_system_proxy_takes_over_when_the_environment_has_no_proxy_entry():
    seen = proxies_seen_by_requests(dict(os.environ))

    assert seen == {"ipv4": None, "name": None, "ipv6": SYSTEM_PROXY, "public": SYSTEM_PROXY}, (
        "模拟 macOS 系统代理的办法失灵了（Python 或 urllib 变了？）："
        "先看这个测试里的 PROBE 还能不能让 urllib.request 按 macOS 的样子导入"
    )


@pytest.mark.parametrize("who", ["test process", "child process"])
def test_the_environment_tests_run_under_never_goes_through_the_system_proxy(who):
    source = dict(os.environ) if who == "test process" else child_env()

    seen = proxies_seen_by_requests(source, keep=("NO_PROXY", "no_proxy"))

    assert seen == {"ipv4": None, "name": None, "ipv6": None, "public": None}
