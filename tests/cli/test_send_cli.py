"""`hubctl send`（任务 D3）：给没有 Python 客户端的脚本用的发消息命令。

这里用一个能随意编排回复的假服务（真的 HTTP，本机回环）测命令这一层：请求长什么样、密钥从哪来、
文字从哪来、每一种回复对应哪个退出码、各种网络故障怎么分成"一定没发出去"（10）和"可能已经发出"
（13）。接真服务的端到端（`sent`、`rejected`、各种状态各一遍）在 `tests/api/test_hubctl_send.py`。
规格 `hub-cli`「从脚本发消息」。

**退出码只用于 `hubctl send`**：0 sent / skipped；3 unconfirmed；4 partial；5 rejected；6 failed；
10 请求没发出去；11 密钥不对 / 项目已停用；12 其他接口错误；13 发出之后没有拿到结果；2 用法错误。
"""

from __future__ import annotations

import json
import time

import pytest
import requests
from typer.testing import CliRunner
from urllib3.exceptions import MaxRetryError, NewConnectionError

from clawbot_hub.cli import Env, app
from clawbot_hub.sendcmd import never_sent, shown_address
from tests.cli.conftest import mode_of
from tests.support.fakehttp import (
    FakeHttp,
    RecordedRequest,
    Reply,
    close,
    hang,
    html,
    reset,
    truncated,
)

KEY = "chub_" + "k" * 43
OTHER_KEY = "chub_" + "o" * 43
ENV_NAME = "CLAWBOT_HUB_KEY"
WINDOW_CLOSED = (
    "对话窗口过期了（你最后一次发消息是10月11日 09:30），给 ClawBot 发任意一句话就能恢复"
)


def result_body(
    status="sent", *, code=None, reason=None, parts=1, parts_sent=None, duplicate=False
) -> dict:
    """服务返回的 200 内容（固定的那几个字段）。"""
    if parts_sent is None:
        parts_sent = parts if status in ("sent", "partial") else 0
    return {
        "status": status,
        "reason_code": code,
        "reason": reason,
        "parts": parts,
        "parts_sent": parts_sent,
        "duplicate": duplicate,
        "id": "o_2f9c1a7e",
        "quota": {"left": 8, "limit": 10, "window_ends_at": "2026-10-11T18:20:00+08:00"},
    }


def error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


class FakeMessages(FakeHttp):
    """扮演共用服务的 `POST /v1/messages`：默认回 `sent`，别的用 `script("messages", …)` 排。"""

    endpoints = ("messages",)

    def route(self, path: str) -> str | None:
        return "messages" if path == "/v1/messages" else None

    def default(self, endpoint: str, request: RecordedRequest) -> Reply:
        return Reply(body=result_body())

    def reply_with(self, *replies: Reply) -> None:
        self.script("messages", *replies)

    def last(self) -> RecordedRequest:
        return self.requests_to("messages")[-1]

    def sent_json(self) -> dict:
        return self.last().json


@pytest.fixture
def server():
    with FakeMessages() as fake:
        yield fake


@pytest.fixture(autouse=True)
def _key_in_the_environment(monkeypatch):
    monkeypatch.setenv(ENV_NAME, KEY)


def send(run, server, *args: str, **kwargs):
    return run("send", "--url", server.base_url, *args, **kwargs)


# ---------------------------------------------------------------- 请求长什么样


def test_one_post_with_the_key_and_just_the_text(run, server):
    result = send(run, server, "盘点完成")

    assert result.exit_code == 0, result.output
    assert result.stdout == "sent\n" and result.stderr == ""  # 成功时没有多余的空行
    [request] = server.requests_to("messages")
    assert (request.method, request.path) == ("POST", "/v1/messages")
    assert request.header("Authorization") == f"Bearer {KEY}"
    assert request.header("Content-Type").startswith("application/json")
    assert request.json == {"text": "盘点完成"}  # 没给的选项不出现在请求里


def test_the_options_become_fields_of_the_request(run, server):
    send(run, server, "--priority", "urgent", "--dedupe-key", "daily-1", "--on-closed", "skip", "x")

    assert server.sent_json() == {
        "text": "x",
        "priority": "urgent",
        "dedupe_key": "daily-1",
        "on_closed": "skip",
    }


@pytest.mark.parametrize(
    "option", [("--priority", "loud"), ("--on-closed", "ignore"), ("--timeout", "soon")]
)
def test_an_option_value_that_makes_no_sense_is_a_usage_error_and_sends_nothing(
    run, server, option
):
    result = send(run, server, *option, "x")

    assert result.exit_code == 2
    assert server.requests == []


@pytest.mark.parametrize("seconds", ["0", "-3"])
def test_the_timeout_must_be_positive(run, server, seconds):
    assert send(run, server, "--timeout", seconds, "x").exit_code == 2
    assert server.requests == []


def test_the_text_goes_out_exactly_as_written(run, server):
    send(run, server, "  标题  \n\n正文  ")

    assert server.sent_json()["text"] == "  标题  \n\n正文  "  # 不去首尾空白


def test_the_default_timeout_is_35_seconds_and_connecting_has_its_own_limit(
    run, server, monkeypatch
):
    seen = {}
    original = requests.Session.send

    def spy(self, request, **kwargs):
        seen.update(kwargs)
        return original(self, request, **kwargs)

    monkeypatch.setattr(requests.Session, "send", spy)

    send(run, server, "x")
    assert seen["timeout"][1] == 35.0 and 0 < seen["timeout"][0] <= 10

    send(run, server, "--timeout", "7.5", "x")
    assert seen["timeout"][1] == 7.5 and seen["timeout"][0] <= 7.5


@pytest.mark.parametrize("status", [300, 301, 302, 307, 308])
def test_redirects_are_not_followed_so_the_key_never_travels_further(run, server, status):
    server.reply_with(Reply(status=status, headers={"Location": f"{server.base_url}/elsewhere"}))

    result = send(run, server, "x")

    assert result.exit_code == 12
    assert f"服务回了重定向（HTTP {status}）" in result.stderr
    assert len(server.requests) == 1


def test_proxies_and_netrc_from_the_environment_are_ignored(run, server, monkeypatch, tmp_path):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")  # 如果走代理，这里连不上
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    netrc = tmp_path.parent / "home-netrc"
    netrc.write_text("machine 127.0.0.1 login someone password secret\n", encoding="utf-8")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))

    result = send(run, server, "x")

    assert result.exit_code == 0, result.output
    assert server.last().header("Authorization") == f"Bearer {KEY}"


# ---------------------------------------------------------------- 文字从哪来


def test_a_dash_reads_the_text_from_standard_input_without_the_last_newline(run, server):
    result = send(run, server, "-", input="第一行\n第二行\n")

    assert result.exit_code == 0, result.output
    assert server.sent_json()["text"] == "第一行\n第二行"


def test_windows_line_endings_are_read_as_plain_newlines(run, server):
    send(run, server, "-", input="第一行\r\n第二行\r\n\r\n")

    assert server.sent_json()["text"] == "第一行\n第二行"  # 文本模式读标准输入，换行统一了


def test_no_argument_and_a_pipe_means_standard_input(run, server, terminal):
    terminal["on"] = False

    result = send(run, server, input="来自管道\n")

    assert result.exit_code == 0, result.output
    assert server.sent_json()["text"] == "来自管道"


def test_no_argument_in_a_terminal_is_a_usage_error_not_a_wait_for_typing(run, server, terminal):
    terminal["on"] = True

    result = send(run, server)

    assert result.exit_code == 2
    assert "没有要发的文字" in result.output
    assert server.requests == []


def test_an_explicit_text_wins_over_whatever_is_piped_in(run, server):
    send(run, server, "参数里的", input="管道里的\n")

    assert server.sent_json()["text"] == "参数里的"


def test_an_empty_text_is_left_to_the_service_to_refuse(run, server):
    server.reply_with(Reply(status=400, body=error_body("empty_text", "文字不能是空的")))

    result = send(run, server, "-", input="")

    assert result.exit_code == 12
    assert server.sent_json()["text"] == ""
    assert "文字不能是空的" in result.output


# ---------------------------------------------------------------- 密钥从哪来


def test_the_key_comes_from_the_default_environment_variable(run, server):
    send(run, server, "x")

    assert server.last().header("Authorization") == f"Bearer {KEY}"


def test_the_key_is_read_from_the_environment_the_command_was_given(server, env, home):
    """`Env.environ` 是命令碰到环境变量的那个口子：给了它，密钥就从它里面找，不看进程自己的
    环境变量（那里放着另一把）。"""
    given = Env(
        clock=env.clock, upstream_factory=env.upstream_factory, environ={ENV_NAME: OTHER_KEY}
    )

    CliRunner().invoke(app, ["send", "--url", server.base_url, "x", "--home", str(home)], obj=given)

    assert server.last().header("Authorization") == f"Bearer {OTHER_KEY}"


def test_key_env_names_another_variable(run, server, monkeypatch):
    monkeypatch.setenv("MY_PROJECT_KEY", OTHER_KEY)

    send(run, server, "--key-env", "MY_PROJECT_KEY", "x")

    assert server.last().header("Authorization") == f"Bearer {OTHER_KEY}"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_a_missing_or_empty_variable_is_a_usage_error_that_names_it(
    run, server, monkeypatch, value
):
    monkeypatch.delenv(ENV_NAME)
    if value is not None:
        monkeypatch.setenv(ENV_NAME, value)

    result = send(run, server, "x")

    assert result.exit_code == 2
    assert ENV_NAME in result.output and "--key-file" in result.output
    assert server.requests == []


def test_a_key_file_works_and_its_line_ending_is_dropped(run, server, tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_NAME)
    key_file = tmp_path / "project.key"
    key_file.write_text(OTHER_KEY + "\n", encoding="utf-8")
    key_file.chmod(0o600)

    result = send(run, server, "--key-file", str(key_file), "x")

    assert result.exit_code == 0, result.output
    assert server.last().header("Authorization") == f"Bearer {OTHER_KEY}"


def test_a_read_only_key_file_is_fine(run, server, tmp_path):
    key_file = tmp_path / "project.key"
    key_file.write_text(OTHER_KEY, encoding="utf-8")
    key_file.chmod(0o400)

    assert send(run, server, "--key-file", str(key_file), "x").exit_code == 0


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o666])
def test_a_key_file_others_can_read_is_refused_before_anything_is_sent(run, server, tmp_path, mode):
    key_file = tmp_path / "project.key"
    key_file.write_text(KEY, encoding="utf-8")
    key_file.chmod(mode)

    result = send(run, server, "--key-file", str(key_file), "x")

    assert result.exit_code == 2
    assert "0600" in result.output
    assert KEY not in result.output
    assert server.requests == []
    assert mode_of(key_file) == mode  # 只拒绝，不替你改权限


@pytest.mark.parametrize("content", [None, "", "\n  \n"])
def test_a_key_file_that_is_missing_or_empty_is_a_usage_error(run, server, tmp_path, content):
    key_file = tmp_path / "project.key"
    if content is not None:
        key_file.write_text(content, encoding="utf-8")
        key_file.chmod(0o600)

    result = send(run, server, "--key-file", str(key_file), "x")

    assert result.exit_code == 2
    assert server.requests == []


def test_a_key_file_that_is_a_folder_is_refused_for_that_reason(run, server, tmp_path):
    folder = tmp_path / "project.key"
    folder.mkdir(mode=0o700)

    result = send(run, server, "--key-file", str(folder), "x")

    assert result.exit_code == 2
    assert "不是普通文件" in result.output
    assert server.requests == []


def test_a_key_file_and_a_key_variable_together_are_a_usage_error(run, server, tmp_path):
    key_file = tmp_path / "project.key"
    key_file.write_text(KEY, encoding="utf-8")
    key_file.chmod(0o600)

    result = send(run, server, "--key-file", str(key_file), "--key-env", ENV_NAME, "x")

    assert result.exit_code == 2
    assert server.requests == []


def test_there_is_no_option_that_takes_the_key_on_the_command_line(run, server):
    result = send(run, server, "--key", "chub_typed_on_the_command_line", "x")

    assert result.exit_code == 2
    assert "chub_typed_on_the_command_line" not in result.output
    assert server.requests == []


@pytest.mark.parametrize(
    "reply",
    [
        Reply(body=result_body()),
        Reply(status=401, body=error_body("unauthorized", "密钥不对")),
        Reply(status=500, body=error_body("internal", "出错了")),
        Reply(body=result_body("rejected", code="window_closed", reason=WINDOW_CLOSED)),
    ],
    ids=["sent", "401", "500", "rejected"],
)
@pytest.mark.parametrize("as_json", [False, True])
def test_the_key_is_in_no_output_whatever_happens(run, server, reply, as_json):
    server.reply_with(reply)

    result = send(run, server, *(["--json"] if as_json else []), "x")

    assert KEY not in result.output


# ---------------------------------------------------------------- 地址


def test_the_default_address_is_this_machine_and_the_configured_port(run, server, home):
    home.mkdir()
    home.joinpath("config.yaml").write_text(f"server:\n  port: {server.port}\n", encoding="utf-8")

    result = run("send", "x")  # 不带 --url

    assert result.exit_code == 0, result.output
    assert len(server.requests_to("messages")) == 1


def test_the_default_port_is_8790(run, monkeypatch):
    seen = {}

    def refuse(self, request, **kwargs):
        seen["url"] = request.url
        refused = NewConnectionError(None, "Failed to establish a new connection")
        raise requests.exceptions.ConnectionError(
            MaxRetryError(None, request.url, reason=refused), request=request
        )

    monkeypatch.setattr(requests.Session, "send", refuse)  # 不真的去碰本机上可能有的服务

    result = run("send", "x")

    assert seen["url"] == "http://127.0.0.1:8790/v1/messages"
    assert result.exit_code == 10 and "http://127.0.0.1:8790" in result.output


def test_a_trailing_slash_in_the_address_does_not_matter(run, server):
    result = run("send", "--url", server.base_url + "/", "x")

    assert result.exit_code == 0, result.output
    assert server.last().target == "/v1/messages"


@pytest.mark.parametrize("url", ["not a url", "localhost:8790", "ftp://127.0.0.1/"])
def test_an_address_that_is_not_http_is_a_usage_error(run, url):
    result = run("send", "--url", url, "x")

    assert result.exit_code == 2
    assert KEY not in result.output


# ---------------------------------------------------------------- 状态对应的退出码


@pytest.mark.parametrize(
    ("status", "code", "reason", "exit_code"),
    [
        ("sent", None, None, 0),
        ("skipped", "window_closed", WINDOW_CLOSED, 0),
        ("unconfirmed", "no_response", "微信 ClawBot 没有回话，这条可能已经到了，不要重发", 3),
        ("partial", "send_timeout", "发送超过 30 秒还没发完，后面的 2 条没有发", 4),
        ("rejected", "window_closed", WINDOW_CLOSED, 5),
        ("failed", "login_expired", "ClawBot 登录失效了：在电脑上运行 hubctl login 重新扫码", 6),
    ],
)
def test_every_status_has_its_exit_code_and_a_one_line_summary(
    run, server, status, code, reason, exit_code
):
    server.reply_with(Reply(body=result_body(status, code=code, reason=reason)))

    result = send(run, server, "x")

    assert result.exit_code == exit_code, result.output
    expected = status if reason is None else f"{status}：{reason}"
    assert result.stdout.strip() == expected


def test_a_message_the_service_had_already_sent_is_just_sent(run, server):
    server.reply_with(Reply(body=result_body(duplicate=True)))

    result = send(run, server, "x", "--dedupe-key", "k")

    assert result.exit_code == 0 and result.stdout.strip() == "sent"


def test_json_prints_what_the_service_returned_unchanged(run, server):
    body = result_body("rejected", code="window_closed", reason=WINDOW_CLOSED)
    server.reply_with(Reply(body=body))

    result = send(run, server, "--json", "x")

    assert result.exit_code == 5
    assert json.loads(result.stdout) == body


@pytest.mark.parametrize(
    "reply",
    [
        Reply(status=401, body=error_body("unauthorized", "密钥不对")),
        Reply(status=400, body=error_body("text_too_long", "文字太长")),
        Reply(status=500, body=error_body("internal", "出错了")),
        Reply(status=307, body="跳转页"),
        Reply(body=result_body("rejected", code="window_closed", reason=WINDOW_CLOSED)),
    ],
    ids=["401", "400", "500", "307", "rejected"],
)
def test_without_json_standard_output_never_carries_the_raw_answer(run, server, reply):
    server.reply_with(reply)

    result = send(run, server, "x")

    assert "{" not in result.stdout and "跳转页" not in result.stdout


def test_json_prints_the_answer_raw_even_for_a_redirect_page(run, server):
    server.reply_with(Reply(status=307, body="跳转页\n"))

    result = send(run, server, "--json", "x")

    assert result.exit_code == 12 and result.stdout == "跳转页\n"  # 末尾的换行由 _say 补回


def test_an_error_body_is_printed_unchanged_with_json_too(run, server):
    body = error_body("unauthorized", "密钥不对")
    server.reply_with(Reply(status=401, body=body))

    result = send(run, server, "--json", "x")

    assert result.exit_code == 11
    assert json.loads(result.stdout) == body


def test_a_status_nobody_knows_is_an_interface_error(run, server):
    server.reply_with(Reply(body=result_body("mystery")))

    result = send(run, server, "x")

    assert result.exit_code == 12
    assert "不认识" in result.output and "mystery" in result.output


def test_a_script_can_use_or_to_fall_back_only_when_it_matters(run, server):
    """退出码为 0 的只有"发了"和"安静跳过"：`|| 走 Bark` 因此只在真没发出去或拿不准时触发。"""
    outcomes = {}
    for status, code in (
        ("sent", None),
        ("skipped", "window_closed"),
        ("rejected", "window_closed"),
    ):
        server.reply_with(
            Reply(body=result_body(status, code=code, reason="原因" if code else None))
        )
        outcomes[status] = send(run, server, "x").exit_code == 0

    assert outcomes == {"sent": True, "skipped": True, "rejected": False}


# ---------------------------------------------------------------- 接口错误：没发成，知道没发成


KEY_HINT = "：密钥不对，或者这个项目已经停用"


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (
            403,
            error_body("key_rejected", "项目已停用"),
            "服务不认这把密钥（HTTP 403 key_rejected）：项目已停用",
        ),
        (
            401,
            {"error": {"code": "key_rejected"}},
            "服务不认这把密钥（HTTP 401 key_rejected）" + KEY_HINT,
        ),
        (403, {"error": {"message": "项目已停用"}}, "服务不认这把密钥（HTTP 403）：项目已停用"),
        (401, {}, "服务不认这把密钥（HTTP 401）" + KEY_HINT),
        (
            403,
            {"error": {"code": 5, "message": ["不是字符串"]}},
            "服务不认这把密钥（HTTP 403）" + KEY_HINT,
        ),
        (403, "<html>不是 JSON</html>", "服务不认这把密钥（HTTP 403）" + KEY_HINT),
        (400, {"error": {"code": "x"}}, "服务拒绝了请求（HTTP 400 x）"),
        (400, {"error": {"message": "文字太长"}}, "服务拒绝了请求（HTTP 400）：文字太长"),
        (400, {"error": {"code": "", "message": ""}}, "服务拒绝了请求（HTTP 400）"),
    ],
    ids=[
        "both",
        "code-only",
        "message-only",
        "empty",
        "not-strings",
        "html",
        "400-code-only",
        "400-message-only",
        "400-empty-strings",
    ],
)
def test_the_error_line_has_exactly_the_parts_the_service_gave(run, server, status, body, expected):
    server.reply_with(Reply(status=status, body=body))

    result = send(run, server, "x")

    assert result.stderr.strip() == expected
    assert result.stdout == ""  # 没发出去的时候标准输出是空的，脚本不会误以为拿到了结果
    assert "None" not in result.output


@pytest.mark.parametrize("status", [401, 403])
def test_a_wrong_or_disabled_key_is_11_with_the_reason_from_the_service(run, server, status):
    server.reply_with(Reply(status=status, body=error_body("key_rejected", "密钥不对或项目已停用")))

    result = send(run, server, "x")

    assert result.exit_code == 11
    assert "密钥不对或项目已停用" in result.output and str(status) in result.output


@pytest.mark.parametrize(
    "reply",
    [
        Reply(status=400, body=error_body("text_too_long", "文字太长了")),
        Reply(status=404, body=error_body("not_found", "没有这个地址")),
        Reply(status=405, body=error_body("method_not_allowed", "方法不对")),
        Reply(status=413, body=error_body("payload_too_large", "请求太大")),
        Reply(status=429, headers={"Retry-After": "12"}, body=error_body("rate_limited", "太快了")),
        Reply(status=503, body=error_body("shutting_down", "服务正在关闭")),
        Reply(status=307, headers={"Location": "http://elsewhere.invalid/"}),
        Reply(status=400, body="<html>不是 JSON</html>"),
    ],
    ids=["400", "404", "405", "413", "429", "503", "307", "400-html"],
)
def test_other_interface_errors_are_12_and_say_the_service_did_not_send_it(run, server, reply):
    server.reply_with(reply)

    result = send(run, server, "x")

    assert result.exit_code == 12
    assert "请不要重发" not in result.output  # 这些是明确没发出去的


def test_a_rate_limit_says_how_long_to_wait(run, server):
    server.reply_with(
        Reply(status=429, headers={"Retry-After": "12"}, body=error_body("rate_limited", "太快了"))
    )

    result = send(run, server, "x")

    assert result.exit_code == 12 and "12 秒" in result.output


@pytest.mark.parametrize(
    ("headers", "tail"),
    [
        ({"Retry-After": "12"}, "；12 秒后再试"),
        ({"Retry-After": " 3 "}, "；3 秒后再试"),
        ({}, "；稍后再试"),
        ({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, "；稍后再试"),  # 写成日期的不读
        ({"Retry-After": "-5"}, "；稍后再试"),
        ({"Retry-After": ""}, "；稍后再试"),
    ],
)
def test_a_rate_limit_waits_as_long_as_the_service_says_and_only_if_it_says_it_in_seconds(
    run, server, headers, tail
):
    server.reply_with(Reply(status=429, headers=headers, body=error_body("rate_limited", "太快了")))

    result = send(run, server, "x")

    assert result.stderr.strip() == "服务拒绝了请求（HTTP 429 rate_limited）：太快了" + tail


def test_other_errors_never_tell_you_to_try_again_later(run, server):
    server.reply_with(Reply(status=400, headers={"Retry-After": "9"}, body=error_body("x", "y")))

    assert "再试" not in send(run, server, "x").stderr


@pytest.mark.parametrize("reason", [None, "", 5, ["列表"]])
def test_a_reason_that_is_not_a_sentence_is_left_out_of_the_one_line_summary(run, server, reason):
    body = result_body("skipped", code="window_closed")
    body["reason"] = reason
    server.reply_with(Reply(body=body))

    result = send(run, server, "x")

    assert result.exit_code == 0 and result.stdout.strip() == "skipped"


def test_the_message_of_an_error_comes_from_the_service(run, server):
    server.reply_with(
        Reply(status=400, body=error_body("text_too_long", "文字太长了：8001 > 8000"))
    )

    result = send(run, server, "x")

    assert "文字太长了：8001 > 8000" in result.output and "text_too_long" in result.output


# ---------------------------------------------------------------- 发出之后没有结果：可能已经发出


@pytest.mark.parametrize("status", [500, 502, 504])
def test_a_server_error_means_it_may_have_gone_out(run, server, status):
    server.reply_with(Reply(status=status, body=error_body("internal", "内部出错")))

    result = send(run, server, "x")

    assert result.exit_code == 13
    assert "可能已经发出，请不要重发" in result.output


def test_a_gateway_page_instead_of_json_is_the_same(run, server):
    server.reply_with(html(502))

    result = send(run, server, "x")

    assert result.exit_code == 13 and "可能已经发出，请不要重发" in result.output


def test_a_success_without_readable_json_is_the_same(run, server):
    server.reply_with(Reply(body="这不是 JSON"))

    result = send(run, server, "x")

    assert result.exit_code == 13 and "可能已经发出，请不要重发" in result.output


@pytest.mark.parametrize("fault", [reset(), close(), truncated()], ids=["reset", "close", "cut"])
def test_a_connection_that_breaks_after_the_request_went_out_may_have_gone_out(run, server, fault):
    server.reply_with(fault)

    result = send(run, server, "x")

    assert result.exit_code == 13, result.output
    assert "可能已经发出，请不要重发" in result.output
    assert "请求发出以后连接出了问题" in result.output and "等了" not in result.output
    assert len(server.requests_to("messages")) == 1  # 命令自己绝不重发


def test_a_service_that_does_not_answer_in_time_may_have_sent_it(run, server):
    server.reply_with(hang(5))
    started = time.monotonic()

    result = send(run, server, "--timeout", "0.5", "x")

    assert result.exit_code == 13, result.output
    assert time.monotonic() - started < 4
    assert "可能已经发出，请不要重发" in result.output and "0.5" in result.output


# ---------------------------------------------------------------- 连不上：一定没发出去


def test_a_service_that_is_not_there_is_10_and_nothing_went_out(run, server):
    address = server.base_url
    server.stop()

    result = run("send", "--url", address, "x")

    assert result.exit_code == 10
    assert f"连不上共用服务（{address}）" in result.output
    assert "可能已经发出" not in result.output


def test_credentials_in_the_address_are_never_printed(run, server):
    address = server.base_url.replace("http://", "http://someone:hunter2@")
    server.stop()

    result = run("send", "--url", address, "x")

    assert result.exit_code == 10
    assert "hunter2" not in result.output and "someone" not in result.output


# ---------------------------------------------------------------- 不留痕迹


def test_sending_creates_no_data_directory_and_no_database(run, server, home):
    send(run, server, "x")

    assert not home.exists()


def test_nothing_is_read_from_the_database_or_the_config_when_they_are_not_needed(
    run, server, home
):
    result = send(run, server, "x")  # 数据目录不存在，用内置默认值

    assert result.exit_code == 0 and not home.exists()


def test_a_broken_config_file_only_matters_when_the_address_has_to_come_from_it(run, server, home):
    home.mkdir()
    home.joinpath("config.yaml").write_text("server:\n  port: 99999\n", encoding="utf-8")

    explicit = send(run, server, "x")
    assert explicit.exit_code == 0, explicit.output  # 地址是自己给的，用不着配置

    implicit = run("send", "x")
    assert implicit.exit_code == 2
    assert "server.port" in implicit.output and "--url" in implicit.output
    assert len(server.requests_to("messages")) == 1


def test_a_config_option_that_points_nowhere_is_a_usage_error_when_needed(run, tmp_path):
    result = run("send", "--config", str(tmp_path / "typo.yaml"), "x")

    assert result.exit_code == 2 and "typo.yaml" in result.output


# ---------------------------------------------------------------- 哪些网络故障算"一定没发出去"


def refused() -> BaseException:
    """连接建立不起来（拒绝连接、没有路由、域名解析不了都是这个形状）。"""
    inner = NewConnectionError(None, "Failed to establish a new connection")
    return requests.exceptions.ConnectionError(MaxRetryError(None, "http://x/", reason=inner))


@pytest.mark.parametrize(
    ("error", "never_went_out"),
    [
        (refused(), True),
        (requests.exceptions.ConnectTimeout("连接超时"), True),
        (requests.exceptions.SSLError("握手失败"), True),  # 握手没完成，请求还没发
        (requests.exceptions.ReadTimeout("读超时"), False),
        (
            requests.exceptions.ConnectionError(
                "Connection aborted.", ConnectionResetError(104, "Connection reset by peer")
            ),
            False,
        ),
        (requests.exceptions.ChunkedEncodingError("响应读到一半断了"), False),
        (requests.exceptions.ConnectionError("说不清是哪一步"), False),  # 拿不准就当可能发出了
        (RuntimeError("别的错误"), False),
    ],
    ids=["refused", "connect-timeout", "ssl", "read-timeout", "reset", "cut", "unknown", "other"],
)
def test_only_a_failure_to_connect_counts_as_certainly_not_sent(error, never_went_out):
    assert never_sent(error) is never_went_out


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:8790", "http://127.0.0.1:8790"),
        ("https://example.com/some/path", "https://example.com"),  # 没写端口不带 :None
        ("http://user:pw@example.com:81/x", "http://example.com:81"),  # 口令和路径不写
        ("http://[::1]:9", "http://[::1]:9"),
        ("http://[::1]", "http://[::1]"),
        ("http://example.com:99999", "http://example.com"),  # 端口写坏了就不写
    ],
)
def test_the_address_shown_to_people_is_scheme_host_and_port_only(url, expected):
    assert shown_address(url) == expected


def test_a_cycle_in_the_chain_of_causes_does_not_hang():
    first, second = RuntimeError("a"), RuntimeError("b")
    first.__cause__, second.__cause__ = second, first

    assert never_sent(first) is False


def test_an_error_hidden_deep_in_the_chain_of_causes_is_still_found():
    inner = NewConnectionError(None, "Failed to establish a new connection")
    outer = RuntimeError("外面套了一层")
    outer.__cause__ = RuntimeError("又一层")
    outer.__cause__.__context__ = requests.exceptions.ConnectionError(
        MaxRetryError(None, "http://x/", reason=inner)
    )

    assert never_sent(outer) is True
