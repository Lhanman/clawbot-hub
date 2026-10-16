"""显示时区（规格 `hub-deploy`「显示时区」，任务 D6）。

数据库里的时间一律是 UTC 毫秒；给人看的地方（原因文字、接口里的 ISO 时间）按 `display.timezone`
显示，默认 `Asia/Shanghai`。原因文字和各处时间的细节分别在 `tests/sender/test_reasons.py`、
`tests/test_timefmt.py`、`tests/api/` 里测过；这里钉住规格里的两个场景，和"库里存的是 UTC"。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from clawbot_hub.config import Config
from tests.support.hub import tweak


def utc_ms(year: int, month: int, day: int, hour: int, minute: int) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp()) * 1000


def last_spoke_at(make_api, timezone: str | None, spoke_ms: int):
    config = Config() if timezone is None else tweak(Config(), display={"timezone": timezone})
    api = make_api(config)
    api.hub.bind()
    api.hub.speak(at_ms=spoke_ms)  # 使用者最后一次发消息；假时钟已经走过了 24 小时
    api.hub.add("alpha", guaranteed=6, cap=8)
    return api


@pytest.mark.parametrize(
    ("timezone", "shown"),
    [
        (None, "10月9日 18:20"),  # 默认 Asia/Shanghai
        ("Asia/Shanghai", "10月9日 18:20"),
        ("Asia/Tokyo", "10月9日 19:20"),
    ],
)
def test_the_reason_shows_the_last_message_time_in_the_display_timezone(make_api, timezone, shown):
    api = last_spoke_at(make_api, timezone, utc_ms(2026, 10, 9, 10, 20))

    body = api.send("你好").json()

    assert body["status"] == "rejected"
    assert f"你最后一次发消息是 {shown}" in body["reason"]


def test_the_database_keeps_utc_milliseconds_whatever_the_display_timezone(make_api):
    spoke = utc_ms(2026, 10, 9, 10, 20)
    api = last_spoke_at(make_api, "Asia/Tokyo", spoke)

    started = api.hub.db.conn().execute("SELECT started_ms FROM windows").fetchone()[0]

    assert started == spoke


@pytest.mark.parametrize(("timezone", "offset"), [(None, "+08:00"), ("Asia/Tokyo", "+09:00")])
def test_iso_times_in_the_api_carry_the_offset_of_the_display_timezone(make_api, timezone, offset):
    config = Config() if timezone is None else tweak(Config(), display={"timezone": timezone})
    api = make_api(config)
    api.hub.ready("alpha")

    body = api.send("你好").json()

    assert body["quota"]["window_ends_at"].endswith(offset)
