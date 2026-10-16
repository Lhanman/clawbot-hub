"""启动和关闭里几个固定的等待时间（任务 D6）。

`server.shutdown_seconds` 这类能调的数在配置里；下面这几个是实现细节，不进配置，写成 `app.py`
顶部的常量。它们的值定下来了就要钉住：改了它们等于改了"关闭最多拖多久"，不该悄悄改。单位都是
真实的秒，和注入的时钟无关。
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import clawbot_hub.app as app_module
from clawbot_hub.config import Config

TEMPLATE = (
    Path(__file__).resolve().parents[2] / "deploy/launchd/com.clawbot-hub.serve.plist.example"
)

FIXED = {
    # 关闭时，最多再等收消息线程这么久：它可能正挂在一次长轮询里，等不到就放着不管
    "POLLER_GRACE_SECONDS": 2.0,
    # 正常关闭时，等服务自己的几个线程（发送、定时任务、凭证监视、告警）各最多这么久
    "WORKER_JOIN_SECONDS": 10.0,
    # HTTP 服务起来最多等多久，超了启动失败
    "SERVER_START_SECONDS": 15.0,
    # 正常关闭时，等 HTTP 服务自己收尾最多多久，超了强行收掉
    "SERVER_STOP_SECONDS": 10.0,
    # 不是正常关闭时（强退之后、测试结束），停各线程各最多等这么久：卡住的不再等
    "CLOSE_JOIN_SECONDS": 1.0,
}


def test_the_fixed_waits_have_their_documented_values():
    assert {name: getattr(app_module, name) for name in FIXED} == FIXED


def test_the_default_shutdown_plus_the_poller_grace_fits_in_what_launchd_allows():
    """默认配置下，等发送最多 35 秒，再加收消息线程的 2 秒，要比 plist 里的 `ExitTimeOut` 短；
    多出来的留给停 HTTP 和各线程（它们都在等事件，平时是毫秒级），不然 launchd 先动手杀了。"""
    allowed = plistlib.loads(TEMPLATE.read_bytes())["ExitTimeOut"]

    assert Config().server.shutdown_seconds + app_module.POLLER_GRACE_SECONDS < allowed
