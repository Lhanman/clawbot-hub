"""假 Bark 服务自己的测试（任务 0.5）：假件按剧本给出每一种结果。

告警相关的上层测试（`hub-alerts`）全靠它给"成功、被拒绝、暂时不通、很慢"这几种结果，
所以这里逐种验证，免得假件写错而上层测试全绿。
"""

from __future__ import annotations

import time

import pytest
import requests

from tests.support import fake_bark as fb
from tests.support import fakehttp as fh
from tests.support.fake_bark import FakeBark

KEY = "AbCdEf1234567890xyz"


@pytest.fixture
def bark():
    with FakeBark() as server:
        yield server


def push(bark: FakeBark, *, timeout: float = 5, **fields):
    body = {"device_key": KEY, "title": "标题", "body": "正文", "group": "clawbot-hub", **fields}
    return requests.post(f"{bark.base_url}/push", json=body, timeout=timeout)


# ---------------------------------------------------------------- 成功


def test_default_reply_is_the_real_success_shape(bark):
    response = push(bark)
    body = response.json()
    assert response.status_code == 200
    assert body["code"] == 200 and body["message"] == "success"
    assert isinstance(body["timestamp"], int)


def test_pushes_are_parsed_and_recorded(bark):
    push(bark, subtitle="副标题", level="timeSensitive")
    push(bark, markdown="**加粗**", body=None)
    first, second = bark.pushes
    assert (first.index, first.device_key, first.title, first.body) == (1, KEY, "标题", "正文")
    assert (first.group, first.subtitle, first.level, first.markdown) == (
        "clawbot-hub",
        "副标题",
        "timeSensitive",
        None,
    )
    assert second.markdown == "**加粗**"
    assert first.raw["title"] == "标题"
    assert first.request_index == bark.requests[0].index
    assert bark.titles() == ["标题", "标题"]


def test_only_the_push_path_exists(bark):
    assert requests.post(f"{bark.base_url}/{KEY}", json={}, timeout=3).status_code == 404
    assert requests.get(f"{bark.base_url}/ping", timeout=3).status_code == 404
    assert bark.pushes == []


# ---------------------------------------------------------------- 默认的拒绝


def test_unknown_device_key_is_rejected_like_the_real_server_when_keys_are_configured():
    with FakeBark(valid_keys={"good-key"}) as server:
        bad = push(server)
        assert bad.status_code == 400
        assert bad.json()["code"] == 400
        # 真的 Bark 会把设备码写进错误信息里：告警代码必须自己把它去掉
        assert KEY in bad.json()["message"]
        good = requests.post(
            f"{server.base_url}/push",
            json={"device_key": "good-key", "title": "t", "body": "b"},
            timeout=3,
        )
        assert good.status_code == 200 and good.json()["code"] == 200


def test_missing_device_key_or_bad_body_is_a_400(bark):
    missing = requests.post(f"{bark.base_url}/push", json={"title": "t"}, timeout=3)
    assert missing.status_code == 400 and missing.json()["code"] == 400
    garbage = requests.post(f"{bark.base_url}/push", data=b"not json", timeout=3)
    assert garbage.status_code == 400 and garbage.json()["code"] == 400


# ---------------------------------------------------------------- 编排：每一种结果


def test_rejected_with_http_4xx_and_a_code(bark):
    bark.script("push", fb.bark_error(400, "failed to get device token"))
    response = push(bark)
    assert response.status_code == 400
    assert response.json() == {"code": 400, "message": "failed to get device token", "timestamp": 0}


def test_rejected_with_http_200_but_a_code_that_is_not_200(bark):
    bark.script("push", fb.bark_error(500, "oops", status=200))
    response = push(bark)
    assert response.status_code == 200 and response.json()["code"] == 500


def test_temporary_failures_then_recovery(bark):
    bark.script("push", fh.http_status(500), fh.http_status(500), fh.http_status(429), fb.bark_ok())
    statuses = [push(bark).status_code for _ in range(4)]
    assert statuses == [500, 500, 429, 200]
    assert push(bark).json()["code"] == 200  # 排完了回到默认
    assert len(bark.pushes) == 5  # 失败的请求内容也收到了，照样记录


def test_gateway_page_and_empty_body(bark):
    bark.script("push", fh.html(502), fh.empty(200))
    assert "<html>" in push(bark).text
    assert push(bark).content == b""


def test_slow_bark_makes_the_client_time_out_and_stopping_releases_the_handler():
    server = FakeBark().start()
    server.script("push", fh.hang())
    with pytest.raises(requests.ReadTimeout):
        push(server, timeout=0.3)
    started = time.monotonic()
    server.stop()
    assert time.monotonic() - started < 3


def test_connection_level_failures(bark):
    bark.script("push", fh.reset(), fh.close(), fh.truncated())
    with pytest.raises(requests.ConnectionError):
        push(bark)
    with pytest.raises(requests.ConnectionError):
        push(bark)
    with pytest.raises(requests.exceptions.ChunkedEncodingError):
        push(bark)
    assert push(bark).status_code == 200


def test_delay_then_default(bark):
    bark.script("push", fh.default_after(0.25))
    started = time.monotonic()
    assert push(bark).json()["code"] == 200
    assert time.monotonic() - started >= 0.2


def test_clear_requests_forgets_pushes_too(bark):
    push(bark)
    bark.clear_requests()
    assert bark.pushes == [] and bark.requests == []
