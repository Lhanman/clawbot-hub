"""共用夹具（`tests/conftest.py`）：假时钟、假腾讯、假 Bark 每个测试各一份，和同一个假时钟对表。"""

from __future__ import annotations

import requests

from tests.support.fake_clock import DEFAULT_START_MS, FakeClock


def test_clock_fixture_is_a_fresh_fake_clock(clock):
    assert isinstance(clock, FakeClock)
    assert clock.now_ms() == DEFAULT_START_MS
    clock.advance(5)  # 下一个测试拿到的还是新的（见下一条）


def test_clock_fixture_is_not_shared_between_tests(clock):
    assert clock.now_ms() == DEFAULT_START_MS


def test_fake_ilink_fixture_is_running_and_stamps_messages_with_the_fake_time(fake_ilink, clock):
    clock.advance(90)
    message = fake_ilink.push_user_message("你好")
    assert message["create_time_ms"] == DEFAULT_START_MS + 90_000
    reply = requests.post(f"{fake_ilink.base_url}/ilink/bot/msg/notifystart", json={}, timeout=3)
    assert reply.json()["ret"] == 0


def test_fake_bark_fixture_is_running_and_stamps_replies_with_the_fake_time(fake_bark, clock):
    clock.advance(10)
    reply = requests.post(f"{fake_bark.base_url}/push", json={"device_key": "k"}, timeout=3)
    assert reply.json()["timestamp"] == (DEFAULT_START_MS + 10_000) // 1000
