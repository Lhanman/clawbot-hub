"""两档请求形式的小零件（任务 A4）：请求头、`base_info`、版本号的编法。

整个请求发出去以后的样子在 `test_http_profiles.py`；这里不联网，直接看 `profiles.py` 给出的东西。
"""

from __future__ import annotations

import base64

import pytest

from clawbot_hub import __version__
from clawbot_hub.upstream.profiles import (
    LEGACY,
    OFFICIAL,
    PROFILES,
    encode_client_version,
    get_profile,
)


def test_the_client_version_is_packed_as_0x00_major_minor_patch():
    assert encode_client_version("2.4.8") == "132104"
    assert encode_client_version("0.0.1") == "1"
    assert encode_client_version("0.1.0") == "256"
    assert encode_client_version("1.0.0") == "65536"
    assert encode_client_version("1.2.3") == str(0x010203)


def test_legacy_adds_nothing_to_the_headers_or_to_base_info():
    assert LEGACY.app_headers() == {}
    assert LEGACY.base_info() == {"channel_version": "1.0.2"}


def test_official_adds_two_headers_and_two_base_info_fields():
    assert OFFICIAL.app_headers() == {"iLink-App-Id": "bot", "iLink-App-ClientVersion": "132104"}
    assert OFFICIAL.base_info() == {
        "channel_version": "2.4.8",
        "bot_agent": f"clawbot-hub/{__version__}",
    }


def test_the_headers_that_carry_the_login_token():
    legacy = LEGACY.bot_headers("tok-1")
    assert set(legacy) == {"Content-Type", "AuthorizationType", "Authorization", "X-WECHAT-UIN"}
    assert legacy["Content-Type"] == "application/json"
    assert legacy["AuthorizationType"] == "ilink_bot_token"
    assert legacy["Authorization"] == "Bearer tok-1"

    official = OFFICIAL.bot_headers("tok-1")
    assert set(official) == set(legacy) | {"iLink-App-Id", "iLink-App-ClientVersion"}
    assert official["Authorization"] == "Bearer tok-1"


def test_the_uin_is_a_base64_decimal_that_covers_the_whole_uint32_range():
    values = []
    for _ in range(60):
        text = base64.b64decode(LEGACY.bot_headers("t")["X-WECHAT-UIN"]).decode()
        assert text.isdigit() and str(int(text)) == text
        values.append(int(text))
    assert all(0 <= value < 2**32 for value in values)
    assert max(values) >= 2**31 > min(values)  # 少一位或多一位都会露馅（漏过的概率约 1e-18）


def test_the_two_profiles_are_registered_by_name():
    assert set(PROFILES) == {"legacy", "official"}
    assert get_profile("legacy") is LEGACY and get_profile("official") is OFFICIAL


def test_only_official_posts_the_qr_request_and_calls_notify():
    assert (LEGACY.qr_start_by_post, LEGACY.uses_notify) == (False, False)
    assert (OFFICIAL.qr_start_by_post, OFFICIAL.uses_notify) == (True, True)


def test_an_unknown_profile_lists_the_known_ones():
    with pytest.raises(ValueError, match="legacy.*official"):
        get_profile("nope")
