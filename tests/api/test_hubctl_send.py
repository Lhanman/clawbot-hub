"""`hubctl send` 对着真服务（真 uvicorn，随机端口，进程内假上游）的端到端验收（任务 D3）。

命令这一层的细节（密钥从哪来、每个状态码对应哪个退出码、网络故障怎么分）在
`tests/cli/test_send_cli.py` 用假服务测过；这里把规格里的场景用真服务各走一遍：
发一条、被拒绝、安静跳过、其余六个退出码、服务没开、发出之后没有结果、从标准输入读。
规格 `hub-cli`「从脚本发消息」。
"""

from __future__ import annotations

import json
import socket
import threading

from typer.testing import CliRunner

from clawbot_hub.cli import Env, app
from clawbot_hub.upstream.base import Accepted, NoResponse, Rejected
from tests.sender.conftest import long_text

BAD_TOKEN = Rejected(200, 40001, "bad token")


def hubctl(api, url, *args, key=None, input=None):
    """像脚本那样跑 `hubctl send`：密钥在环境变量里，不是终端。"""
    env = Env(clock=api.hub.clock, is_terminal=lambda: False)
    return CliRunner().invoke(
        app,
        ["send", "--url", url, *args],
        obj=env,
        input=input,
        env={"CLAWBOT_HUB_KEY": key or api.key()},
    )


def test_one_message_is_sent(api, serve):
    url, _ = serve(api)

    result = hubctl(api, url, "盘点完成")

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "sent"
    assert api.hub.texts == ["盘点完成"]


def test_a_rejected_message_says_why_and_exits_five(api, serve):
    url, _ = serve(api)
    api.hub.clock.advance(25 * 3600)  # 使用者最后一句话是 25 小时前：窗口过期了

    result = hubctl(api, url, "盘点完成")

    assert result.exit_code == 5
    assert result.stdout.startswith("rejected：对话窗口过期了")
    assert api.hub.upstream.calls == []


def test_skipping_when_the_window_is_closed_is_not_a_failure(api, serve):
    url, _ = serve(api)
    api.hub.clock.advance(25 * 3600)

    result = hubctl(api, url, "--on-closed", "skip", "盘点完成")

    assert result.exit_code == 0  # `|| 走 Bark` 不会被触发
    assert result.stdout.startswith("skipped：对话窗口过期了")
    assert api.hub.upstream.calls == []


def test_the_other_exit_codes_in_the_order_of_the_spec(api, serve):
    """unconfirmed、partial、failed（登录失效）、错误的密钥、已停用项目的密钥、超长文字。"""
    url, _ = serve(api)
    codes = []

    api.hub.upstream.script("send_text", NoResponse("读超时", read_timeout=True))
    codes.append(hubctl(api, url, "你好").exit_code)  # 3 unconfirmed

    api.hub.upstream.script("send_text", Accepted("m-1"), BAD_TOKEN)
    codes.append(hubctl(api, url, long_text(3)).exit_code)  # 4 partial（第 2 条起停下）

    api.hub.expire()
    codes.append(hubctl(api, url, "你好").exit_code)  # 6 failed（登录失效）

    codes.append(hubctl(api, url, "你好", key="chub_" + "x" * 43).exit_code)  # 11 错误的密钥

    api.hub.projects.disable("alpha")
    codes.append(hubctl(api, url, "你好").exit_code)  # 11 已停用项目的密钥
    api.hub.projects.enable("alpha")

    codes.append(hubctl(api, url, "字" * 8001).exit_code)  # 12 超长文字（400）

    assert codes == [3, 4, 6, 11, 11, 12]


def test_a_service_that_is_not_running_exits_ten_and_sends_nothing(api):
    # 另起一个没有人监听的端口
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = f"http://127.0.0.1:{probe.getsockname()[1]}"

    result = hubctl(api, closed, "你好")

    assert result.exit_code == 10
    assert f"连不上共用服务（{closed}）" in result.output
    assert api.hub.upstream.calls == []


def test_a_server_error_after_the_request_went_out_exits_thirteen(api, serve, monkeypatch):
    url, _ = serve(api)

    def boom(*args, **kwargs):
        raise RuntimeError("坏了")

    monkeypatch.setattr(api.services.sender, "push", boom)

    result = hubctl(api, url, "你好")

    assert result.exit_code == 13
    assert "可能已经发出，请不要重发" in result.output


def test_a_service_that_does_not_answer_in_time_exits_thirteen(api, serve, monkeypatch):
    url, _ = serve(api)
    release = threading.Event()

    def stuck(*args, **kwargs):
        release.wait(10)
        raise RuntimeError("放手")

    monkeypatch.setattr(api.services.sender, "push", stuck)
    try:
        result = hubctl(api, url, "--timeout", "0.5", "你好")
    finally:
        release.set()

    assert result.exit_code == 13
    assert "可能已经发出，请不要重发" in result.output


def test_text_from_standard_input_arrives_as_written(api, serve):
    url, _ = serve(api)

    result = hubctl(api, url, "-", input="第一行\n第二行\n")

    assert result.exit_code == 0, result.output
    assert api.hub.texts == ["第一行\n第二行"]


def test_the_options_reach_the_service(api, serve):
    url, _ = serve(api)

    first = hubctl(api, url, "--dedupe-key", "daily-1", "--priority", "urgent", "盘点完成")
    again = hubctl(api, url, "--dedupe-key", "daily-1", "--json", "盘点完成")

    assert first.exit_code == 0 and again.exit_code == 0
    assert api.hub.texts == ["盘点完成"]  # 第二次是去重命中，没有再发
    assert json.loads(again.stdout)["duplicate"] is True


def test_json_is_what_the_service_returned(api, serve):
    url, _ = serve(api)

    result = hubctl(api, url, "--json", "盘点完成")

    body = json.loads(result.stdout)
    assert (body["status"], body["parts"], body["parts_sent"]) == ("sent", 1, 1)
    assert list(body) == [
        "status",
        "reason_code",
        "reason",
        "parts",
        "parts_sent",
        "duplicate",
        "id",
        "quota",
    ]
