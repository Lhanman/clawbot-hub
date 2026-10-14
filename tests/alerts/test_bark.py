"""Bark 这一层：设备码怎么写、从哪里读，以及一次 `POST <服务器>/push` 的请求格式和结果分类。

请求格式、设备码的写法与 ashare-quant 的 Bark 渠道一致：JSON 里是设备码、标题、正文、分组
（不带 `level`、`markdown`、`subtitle`）；设备码可以只填一串，也可以粘贴 App 里的整个推送地址。
结果分两类（规格 `hub-alerts`「Bark 失败怎么办」）：

- **被拒绝**（Bark 服务器明确回了错误）：HTTP 4xx（429 除外），或者 2xx / 3xx 但 JSON 里的 `code`
  不是 200 （含响应体不是 JSON）；不重试；
- **暂时不通**：超时、连不上、连接被重置 / 断开、HTTP 429、5xx。

设备码不出现在任何结果的文字里（Bark 自己的错误信息里可能带着设备码，要遮掉）。
"""

from __future__ import annotations

import json
import os
import stat
import time

import pytest
import requests

from clawbot_hub.bark import (
    ENV_KEY,
    SERVER,
    BarkClient,
    BarkKey,
    Delivered,
    Refused,
    Unreachable,
    lookup_bark_key,
    parse_bark_key,
    write_bark_key,
)
from tests.alerts.conftest import DEVICE
from tests.support.fake_bark import FakeBark, bark_error
from tests.support.fakehttp import Reply, close, empty, hang, html, http_status, reset, truncated

TITLE = "ClawBot 共用服务 · 登录失效"
TEXT = "ClawBot 登录失效了：在电脑上运行 hubctl login 重新扫码"


# ---------------------------------------------------------------- 设备码的写法


def test_a_bare_code_uses_the_default_server():
    assert parse_bark_key("AbCd1234") == BarkKey(SERVER, "AbCd1234")


def test_surrounding_blanks_and_slashes_are_dropped():
    assert parse_bark_key("  AbCd1234/ \n") == BarkKey(SERVER, "AbCd1234")


def test_the_whole_push_address_from_the_app_works():
    assert parse_bark_key("https://api.day.app/AbCd1234/") == BarkKey(SERVER, "AbCd1234")


def test_only_the_first_path_segment_is_the_code():
    assert parse_bark_key("https://api.day.app/AbCd1234/标题/内容") == BarkKey(SERVER, "AbCd1234")


def test_a_self_hosted_server_is_kept():
    key = parse_bark_key("http://bark.example:8080/AbCd1234")

    assert key == BarkKey("http://bark.example:8080", "AbCd1234")


def test_an_ipv6_server_keeps_its_brackets_and_port():
    key = parse_bark_key("http://[::1]:8080/AbCd1234")

    assert key == BarkKey("http://[::1]:8080", "AbCd1234")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "\n",
        "https://api.day.app/",
        "http:///SECRETISH",
        "http://[SECRETISH/abc",  # 地址写坏了（括号没配对）
        "http://example.com:SECRETISH/abc",  # 端口不是数字
        "ftp://api.day.app/SECRETISH",  # 不是 http / https
        "file:///SECRETISH/x",
    ],
)
def test_a_value_without_a_device_code_is_refused_without_echoing_it(value):
    with pytest.raises(ValueError) as caught:
        parse_bark_key(value)

    assert "SECRETISH" not in str(caught.value)


def test_the_device_code_stays_out_of_repr():
    key = BarkKey(SERVER, DEVICE)

    assert DEVICE not in repr(key)
    assert DEVICE not in str(key)


# ---------------------------------------------------------------- 设备码从哪里读


def test_no_file_and_no_environment_means_no_key(paths):
    found = lookup_bark_key(paths, {})

    assert found.key is None
    assert found.source is None
    assert found.problem is None


def test_the_file_is_read(paths):
    write_bark_key(paths, "FILE-KEY")

    found = lookup_bark_key(paths, {})

    assert found.source == "file"
    assert found.key == BarkKey(SERVER, "FILE-KEY")


def test_the_environment_is_read(paths):
    found = lookup_bark_key(paths, {ENV_KEY: "ENV-KEY"})

    assert found.source == "env"
    assert found.key == BarkKey(SERVER, "ENV-KEY")


def test_the_environment_wins_over_the_file(paths):
    write_bark_key(paths, "K1")

    found = lookup_bark_key(paths, {ENV_KEY: "K2"})

    assert found.source == "env"
    assert found.key.device == "K2"


def test_a_blank_environment_value_counts_as_not_set(paths):
    write_bark_key(paths, "K1")

    found = lookup_bark_key(paths, {ENV_KEY: "  "})

    assert found.source == "file"
    assert found.key.device == "K1"


def test_the_file_is_read_again_every_time(paths):
    write_bark_key(paths, "K1")
    assert lookup_bark_key(paths, {}).key.device == "K1"

    write_bark_key(paths, "K2")  # `hubctl alerts set-key` 在服务运行中换了设备码

    assert lookup_bark_key(paths, {}).key.device == "K2"


def test_the_environment_defaults_to_the_real_one(paths, monkeypatch):
    monkeypatch.setenv(ENV_KEY, "FROM-OS-ENV")

    assert lookup_bark_key(paths).key.device == "FROM-OS-ENV"


def test_a_broken_environment_value_is_a_problem_not_a_fallback(paths):
    write_bark_key(paths, "K1")

    found = lookup_bark_key(paths, {ENV_KEY: "http:///SECRETISH"})

    assert found.source == "env"
    assert found.key is None
    assert ENV_KEY in found.problem
    assert "SECRETISH" not in found.problem


@pytest.mark.parametrize(
    "content",
    ["这不是 JSON", "[]", "{}", '{"key": 5}', '{"key": ""}', '{"key": "http:///SECRETISH"}'],
)
def test_a_broken_file_is_a_problem_that_does_not_show_the_content(paths, content):
    paths.bark.write_text(content, encoding="utf-8")

    found = lookup_bark_key(paths, {})

    assert found.source == "file"
    assert found.key is None
    assert "bark.json" in found.problem
    assert "SECRETISH" not in found.problem


def test_a_file_that_cannot_be_read_is_a_problem_that_names_only_the_error_type(paths):
    paths.bark.mkdir()  # 本该是文件的地方是个目录：读它抛 IsADirectoryError

    found = lookup_bark_key(paths, {})

    assert found.source == "file"
    assert found.key is None
    assert found.problem == "bark.json 读不出来（IsADirectoryError）"


def test_a_file_that_is_not_there_is_not_a_problem(paths):
    found = lookup_bark_key(paths, {})

    assert (found.key, found.source, found.problem) == (None, None, None)


# ---------------------------------------------------------------- 写设备码


def test_writing_stores_the_value_as_given_in_a_private_file(paths):
    write_bark_key(paths, " https://api.day.app/AbCd1234/ ")

    assert json.loads(paths.bark.read_text(encoding="utf-8")) == {
        "key": "https://api.day.app/AbCd1234/"
    }
    assert stat.S_IMODE(os.stat(paths.bark).st_mode) == 0o600
    assert lookup_bark_key(paths, {}).key == BarkKey(SERVER, "AbCd1234")


def test_writing_over_an_existing_file_keeps_it_private_and_leaves_no_scraps(paths):
    paths.bark.write_text('{"key": "OLD"}', encoding="utf-8")
    os.chmod(paths.bark, 0o644)

    write_bark_key(paths, "NEW")

    assert stat.S_IMODE(os.stat(paths.bark).st_mode) == 0o600
    assert lookup_bark_key(paths, {}).key.device == "NEW"
    assert sorted(entry.name for entry in paths.home.iterdir() if "bark" in entry.name) == [
        "bark.json"
    ]


def test_writing_the_key_is_synced_to_disk(paths, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real(fd))[1])

    write_bark_key(paths, "K1")

    assert len(synced) == 2  # 文件一次，目录一次


@pytest.mark.parametrize("value", ["", "  ", "http:///SECRETISH"])
def test_a_value_that_cannot_work_is_not_written(paths, value):
    write_bark_key(paths, "GOOD")

    with pytest.raises(ValueError) as caught:
        write_bark_key(paths, value)

    assert "SECRETISH" not in str(caught.value)
    assert lookup_bark_key(paths, {}).key.device == "GOOD"  # 原来的没动


# ---------------------------------------------------------------- 一次请求的格式


@pytest.fixture
def client() -> BarkClient:
    return BarkClient("clawbot-hub", timeout_seconds=2)


def aim(fake_bark: FakeBark, device: str = DEVICE) -> BarkKey:
    return BarkKey(fake_bark.base_url, device)


def test_a_push_is_one_post_with_exactly_the_four_fields(client, fake_bark):
    result = client.send(aim(fake_bark), TITLE, TEXT)

    assert result == Delivered()
    [request] = fake_bark.requests
    assert (request.method, request.path) == ("POST", "/push")
    assert request.header("Content-Type").startswith("application/json")
    assert request.json == {
        "device_key": DEVICE,
        "title": TITLE,
        "body": TEXT,
        "group": "clawbot-hub",
    }


def test_no_level_markdown_or_subtitle_is_sent(client, fake_bark):
    client.send(aim(fake_bark), TITLE, TEXT)

    [push] = fake_bark.pushes
    assert (push.level, push.markdown, push.subtitle) == (None, None, None)
    assert set(push.raw) == {"device_key", "title", "body", "group"}


def test_the_group_follows_the_configuration(fake_bark):
    BarkClient("别的分组", timeout_seconds=2).send(aim(fake_bark), TITLE, TEXT)

    assert fake_bark.pushes[0].group == "别的分组"


def test_chinese_text_arrives_intact(client, fake_bark):
    client.send(aim(fake_bark), "标题 · 测试", "第一行\n第二行：全角，标点。")

    push = fake_bark.pushes[0]
    assert (push.title, push.body) == ("标题 · 测试", "第一行\n第二行：全角，标点。")


def test_every_send_is_independent_of_the_servers_idle_connection_closing(client):
    with FakeBark(close_after_response=True) as bark:
        results = [client.send(aim(bark), TITLE, TEXT) for _ in range(3)]

    assert results == [Delivered()] * 3


def test_credentials_in_the_users_netrc_are_never_sent(client, fake_bark, tmp_path, monkeypatch):
    """`requests` 默认会按主机名去 `~/.netrc` 找账号密码、塞进 `Authorization` 头；设备码之外
    的任何凭证都不该跟着请求出去。"""
    netrc = tmp_path / "netrc-home" / ".netrc"
    netrc.parent.mkdir()
    netrc.write_text("machine 127.0.0.1 login someone password secret\n", encoding="utf-8")
    netrc.chmod(0o600)
    monkeypatch.setenv("HOME", str(netrc.parent))

    client.send(aim(fake_bark), TITLE, TEXT)

    [request] = fake_bark.requests
    assert request.header("Authorization") is None


def test_a_redirect_is_not_followed(client, fake_bark):
    fake_bark.script(
        "push", Reply(status=307, headers={"Location": "http://127.0.0.1:9/elsewhere"})
    )

    result = client.send(aim(fake_bark), TITLE, TEXT)

    assert isinstance(result, Refused)
    assert len(fake_bark.requests) == 1  # 设备码不会被带到别处


# ---------------------------------------------------------------- 结果分类

REFUSED = {
    "device code is wrong": bark_error(400, "failed to get device token"),
    "forbidden": bark_error(403, "forbidden"),
    "not found": bark_error(404, "not found"),
    "http 200 but another code": bark_error(500, "boom", status=200),
    "http 200 but a client error code": bark_error(400, "bad", status=200),
    "http 404 but code 200": Reply(status=404, body={"code": 200}),
    "http 302 but code 200": Reply(status=302, body={"code": 200}),
    "http 200 but not json": Reply(status=200, body="<html>ok</html>"),
    "http 200 without a body": empty(200),
    "http 200 with a json list": Reply(status=200, body=[1, 2]),
    "redirect": Reply(status=302),
}
UNREACHABLE = {
    "too many requests": http_status(429),
    "server error": http_status(500),
    "bad gateway": http_status(502),
    "gateway page": html(503),
    "connection reset": reset(),
    "closed without an answer": close(),
    "answer cut off": truncated(),
}


@pytest.mark.parametrize("reply", list(REFUSED.values()), ids=list(REFUSED))
def test_an_error_answer_from_bark_is_a_refusal(client, fake_bark, reply):
    fake_bark.script("push", reply)

    result = client.send(aim(fake_bark), TITLE, TEXT)

    assert isinstance(result, Refused), result
    assert result.reason.strip()


@pytest.mark.parametrize(
    ("status", "delivered"),
    [(200, True), (201, True), (302, False), (307, False), (400, False), (404, False)],
)
def test_only_a_2xx_answer_with_code_200_counts_as_delivered(client, fake_bark, status, delivered):
    fake_bark.script("push", Reply(status=status, body={"code": 200, "message": "success"}))

    result = client.send(aim(fake_bark), TITLE, TEXT)

    assert (result == Delivered()) is delivered, result


@pytest.mark.parametrize("reply", list(UNREACHABLE.values()), ids=list(UNREACHABLE))
def test_trouble_getting_through_is_not_a_refusal(client, fake_bark, reply):
    fake_bark.script("push", reply)

    result = client.send(aim(fake_bark), TITLE, TEXT)

    assert isinstance(result, Unreachable), result
    assert result.reason.strip()


def test_a_server_that_never_answers_times_out(fake_bark):
    fake_bark.script("push", hang())
    patient = BarkClient("clawbot-hub", timeout_seconds=0.3)

    started = time.monotonic()
    result = patient.send(aim(fake_bark), TITLE, TEXT)

    assert isinstance(result, Unreachable)
    assert "Timeout" in result.reason
    assert time.monotonic() - started < 3


def test_nobody_listening_is_unreachable(client):
    with FakeBark() as bark:
        address = aim(bark)
    # 服务已经关了，端口上没有人

    result = client.send(address, TITLE, TEXT)

    assert isinstance(result, Unreachable)


def test_the_refusal_keeps_the_servers_message(client, fake_bark):
    fake_bark.script("push", bark_error(400, "failed to get device token"))

    result = client.send(aim(fake_bark), TITLE, TEXT)

    assert result == Refused("failed to get device token")


def test_a_refusal_without_a_message_names_the_http_status(client, fake_bark):
    fake_bark.script("push", Reply(status=404, body={"code": 404}))

    result = client.send(aim(fake_bark), TITLE, TEXT)

    assert result == Refused("HTTP 404")


def test_the_device_code_is_masked_in_what_the_server_says_back(client, fake_bark):
    fake_bark.valid_keys = {"SOME-OTHER-KEY"}  # 假 Bark 对不认识的设备码回的错误里带着设备码

    result = client.send(aim(fake_bark), TITLE, TEXT)

    assert isinstance(result, Refused)
    assert DEVICE not in result.reason
    assert "device token" in result.reason


def test_the_device_code_is_masked_in_every_failure_text(client, fake_bark):
    fake_bark.script("push", http_status(500, {"message": f"oops {DEVICE}"}))

    result = client.send(aim(fake_bark), TITLE, TEXT)

    assert isinstance(result, Unreachable)
    assert DEVICE not in result.reason


def test_the_refusal_message_is_trimmed(client, fake_bark):
    fake_bark.script("push", Reply(status=400, body={"code": 400, "message": "  bad key \n"}))

    result = client.send(aim(fake_bark), TITLE, TEXT)

    assert result == Refused("bad key")


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_a_status_that_means_try_again_is_named_by_its_number_only(client, fake_bark, status):
    fake_bark.script("push", http_status(status, {"message": "上游说的话不该进原因"}))

    result = client.send(aim(fake_bark), TITLE, TEXT)

    assert result == Unreachable(f"HTTP {status}")


def test_an_os_error_that_requests_did_not_wrap_is_also_just_unreachable(
    client, fake_bark, monkeypatch
):
    def broken(self, *args, **kwargs):
        raise ConnectionAbortedError("secret detail")  # `OSError` 的子类，`requests` 没包起来

    monkeypatch.setattr(requests.Session, "post", broken)

    result = client.send(aim(fake_bark), TITLE, TEXT)

    assert result == Unreachable("ConnectionAbortedError")
