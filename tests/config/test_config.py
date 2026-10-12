"""配置（任务 A2）：默认值、取值范围、枚举和格式、跨字段约束、错误一次列全、只警告的情形、读文件。

规格：`hub-deploy`「配置」「谁能连」；设计第 13 节的默认值表和取值范围表。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from clawbot_hub.config import Config, ConfigError, load_config, parse_config

# 设计第 13 节的默认值，逐项抄在这里；改默认值要同时改设计、代码和这张表
EXPECTED_DEFAULTS = {
    "server.host": "127.0.0.1",
    "server.port": 8790,
    "server.lan": False,
    "server.shutdown_seconds": 35,
    "upstream.base_url": "https://ilinkai.weixin.qq.com",
    "upstream.profile": "legacy",
    "upstream.confirm": "observe",
    "upstream.poll_timeout": 45,
    "upstream.request_timeout": 15,
    "upstream.backoff_start": 5,
    "upstream.backoff_max": 60,
    "upstream.relogin_probe_seconds": 600,
    "send.timeout_seconds": 30,
    "send.part_chars": 2000,
    "send.max_chars": 8000,
    "send.retries": 2,
    "send.rate_per_minute": 30,
    "send.dedupe_hours": 24,
    "send.message_id_keys": ("message_id", "msg_id"),
    "quota.window_hours": 24,
    "quota.window_limit": 10,
    "quota.reply_reserve": 2,
    "quota.deferrable_keep": 3,
    "quota.reject_hold_minutes": 30,
    "inbox.wait_seconds": 25,
    "inbox.max_wait": 55,
    "inbox.lease_seconds": 60,
    "inbox.max_attempts": 5,
    "inbox.max_queued": 50,
    "inbox.retention_minutes": 10,
    "inbox.online_seconds": 90,
    "inbox.offline_notice_seconds": 300,
    "reply.handle_minutes": 30,
    "reply.max_per_handle": 5,
    "routing.confirm_words": ("对", "是", "确认"),
    "routing.confirm_seconds": 600,
    "hub.label": "服务",
    "alerts.group": "clawbot-hub",
    "alerts.bark_timeout_seconds": 10,
    "alerts.bark_retries": 3,
    "alerts.queue_retry_minutes": 5,
    "alerts.queue_give_up_minutes": 60,
    "alerts.unconfirmed_streak": 3,
    "alerts.upstream_down_minutes": 10,
    "alerts.restart_merge_minutes": 10,
    "alerts.window_warn.enabled": True,
    "alerts.window_warn.hours_before": 3,
    "alerts.window_warn.between": "07:00-22:30",
    "alerts.window_warn.weekdays": (1, 2, 3, 4, 5),
    "alerts.blocked.min_interval_hours": 12,
    "display.timezone": "Asia/Shanghai",
    "events.keep_days": 90,
    "backup.at": "04:00",
    "backup.keep": 14,
    "db.busy_timeout_ms": 5000,
    "log.level": "INFO",
    "log.max_mb": 5,
    "log.keep": 5,
}


def build(overrides: dict[str, object] | None = None) -> dict:
    """把 {"server.port": 9000} 这样的点号键展开成嵌套的配置字典。"""
    data: dict = {}
    for dotted, value in (overrides or {}).items():
        node = data
        *parents, leaf = dotted.split(".")
        for name in parents:
            node = node.setdefault(name, {})
        node[leaf] = value
    return data


def accepts(overrides: dict[str, object] | None = None) -> Config:
    return parse_config(build(overrides))


def errors_of(overrides: dict[str, object] | None = None) -> list[str]:
    with pytest.raises(ConfigError) as info:
        parse_config(build(overrides))
    return list(info.value.errors)


def only_error(overrides: dict[str, object]) -> str:
    errors = errors_of(overrides)
    assert len(errors) == 1, errors
    return errors[0]


# ---------------------------------------------------------------- 默认值


def test_no_config_file_means_all_defaults(tmp_path):
    assert load_config(tmp_path / "config.yaml") == Config()
    assert load_config(None) == Config()


def test_defaults_match_the_design_table():
    assert Config().flat() == EXPECTED_DEFAULTS


def test_defaults_pass_their_own_validation():
    assert parse_config(dataclasses.asdict(Config())) == Config()
    assert Config().warnings() == []


@pytest.mark.parametrize("text", ["", "# 只有注释\n", "---\n", "server:\n", "server: {}\n"])
def test_an_empty_file_or_empty_sections_mean_defaults(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    assert load_config(path) == Config()


def test_confirm_words_default_is_exactly_the_three_words():
    assert Config().routing.confirm_words == ("对", "是", "确认")


def test_partial_override_keeps_every_other_default():
    config = accepts({"server.port": 9000, "alerts.window_warn.hours_before": 5})
    assert config.server.port == 9000
    assert config.server.host == "127.0.0.1"
    assert config.alerts.window_warn.hours_before == 5
    assert config.alerts.window_warn.enabled is True  # 同一段里没写的保持默认
    assert config.alerts.blocked.min_interval_hours == 12
    expected = dict(EXPECTED_DEFAULTS)
    expected["server.port"] = 9000
    expected["alerts.window_warn.hours_before"] = 5
    assert config.flat() == expected


def test_lists_become_tuples_and_the_config_is_frozen():
    config = accepts({"routing.confirm_words": ["好", "行"]})
    assert config.routing.confirm_words == ("好", "行")
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.server.port = 1  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.alerts.window_warn.enabled = False  # type: ignore[misc]


def test_flat_lists_every_key_in_design_order():
    keys = list(Config().flat())
    assert keys[0] == "server.host"
    assert keys[-1] == "log.keep"
    assert keys.index("alerts.window_warn.between") < keys.index(
        "alerts.blocked.min_interval_hours"
    )
    assert len(keys) == len(set(keys)) == len(EXPECTED_DEFAULTS)


# ---------------------------------------------------------------- 规格里的场景


def test_several_errors_are_listed_at_once():
    errors = errors_of(
        {"quota.window_limit": -1, "server.port": 70000, "upstream.profile": "x"},
    )
    assert len(errors) == 3
    assert any("quota.window_limit" in e and "100" in e for e in errors)
    assert any("server.port" in e and "65535" in e for e in errors)
    assert any("upstream.profile" in e and "legacy" in e and "official" in e for e in errors)


def test_out_of_range_numbers_each_say_their_allowed_range():
    errors = errors_of(
        {
            "routing.confirm_seconds": 3600,
            "send.timeout_seconds": 1,
            "send.max_chars": 70000,
            "send.retries": 9,
        }
    )
    assert len(errors) == 4
    assert any("routing.confirm_seconds" in e and "1800" in e for e in errors)
    assert any("send.timeout_seconds" in e and "600" in e for e in errors)
    assert any("send.max_chars" in e and "60000" in e for e in errors)
    assert any("send.retries" in e and "5" in e for e in errors)
    accepts(
        {
            "routing.confirm_seconds": 1800,
            "send.timeout_seconds": 2,
            "send.max_chars": 60000,
            "send.retries": 5,
        }
    )


def test_service_word_and_confirm_words_have_the_prefix_format():
    errors = errors_of({"hub.label": "", "routing.confirm_words": ["对", "好,的"]})
    assert len(errors) == 2
    assert any("hub.label" in e for e in errors)
    assert any("routing.confirm_words" in e and "好,的" in e for e in errors)


def test_a_part_cannot_be_too_short_for_title_and_head():
    assert "send.part_chars" in only_error({"send.part_chars": 150})
    assert accepts({"send.part_chars": 200}).send.part_chars == 200
    # 连同其他错误一次列出
    errors = errors_of({"send.part_chars": 150, "server.port": 0})
    assert len(errors) == 2


# ---------------------------------------------------------------- 取值范围表

# (键, 下限, 上限, 为了不碰到别的约束而一起改的项)
RANGES = [
    ("server.port", 1, 65535, {}),
    ("server.shutdown_seconds", 1, 600, {}),
    ("upstream.poll_timeout", 5, 120, {}),
    ("upstream.request_timeout", 1, 120, {}),
    ("upstream.backoff_start", 1, 3600, {"upstream.backoff_max": 3600}),
    ("upstream.backoff_max", 1, 3600, {"upstream.backoff_start": 1}),
    ("upstream.relogin_probe_seconds", 30, 86400, {}),
    ("send.timeout_seconds", 2, 600, {}),
    ("send.part_chars", 200, 4000, {}),
    ("send.max_chars", 200, 60000, {"send.part_chars": 200}),
    ("send.retries", 0, 5, {}),
    ("quota.window_hours", 2, 168, {"alerts.window_warn.hours_before": 1}),
    (
        "quota.window_limit",
        2,
        100,
        {"quota.reply_reserve": 1, "quota.deferrable_keep": 2},
    ),
    ("inbox.max_wait", 1, 600, {"inbox.wait_seconds": 0}),
    ("inbox.max_attempts", 1, 20, {}),
    ("routing.confirm_seconds", 1, 1800, {}),
    ("reply.handle_minutes", 1, 1440, {}),
    ("reply.max_per_handle", 1, 20, {}),
    ("alerts.bark_retries", 0, 10, {}),
    ("backup.keep", 1, 365, {}),
    ("db.busy_timeout_ms", 100, 60000, {}),
]
# 范围跟着别的键走的四项，单独测
DEPENDENT_RANGE_KEYS = {
    "quota.reply_reserve",
    "quota.deferrable_keep",
    "inbox.wait_seconds",
    "alerts.window_warn.hours_before",
}
# 表里没有的数值键：1 到 1000000 的整数
OTHER_INTEGER_KEYS = [
    "send.rate_per_minute",
    "send.dedupe_hours",
    "quota.reject_hold_minutes",
    "inbox.lease_seconds",
    "inbox.max_queued",
    "inbox.retention_minutes",
    "inbox.online_seconds",
    "inbox.offline_notice_seconds",
    "alerts.bark_timeout_seconds",
    "alerts.queue_retry_minutes",
    "alerts.queue_give_up_minutes",
    "alerts.unconfirmed_streak",
    "alerts.upstream_down_minutes",
    "alerts.restart_merge_minutes",
    "alerts.blocked.min_interval_hours",
    "events.keep_days",
    "log.max_mb",
    "log.keep",
]


@pytest.mark.parametrize(("key", "low", "high", "extra"), RANGES, ids=[r[0] for r in RANGES])
def test_range_edges(key, low, high, extra):
    assert accepts({**extra, key: low}).flat()[key] == low
    assert accepts({**extra, key: high}).flat()[key] == high
    for outside in (low - 1, high + 1):
        message = only_error({**extra, key: outside})
        assert key in message
        assert str(low) in message and str(high) in message


@pytest.mark.parametrize("key", OTHER_INTEGER_KEYS)
def test_keys_not_in_the_table_take_one_to_a_million(key):
    assert accepts({key: 1}).flat()[key] == 1
    assert accepts({key: 1_000_000}).flat()[key] == 1_000_000
    for outside in (0, -1, 1_000_001):
        message = only_error({key: outside})
        assert key in message and "1000000" in message


def test_every_integer_key_has_a_tested_range():
    integer_keys = {key for key, value in Config().flat().items() if type(value) is int}
    tested = {r[0] for r in RANGES} | DEPENDENT_RANGE_KEYS | set(OTHER_INTEGER_KEYS)
    assert integer_keys == tested


# ---------------------------------------------------------------- 范围跟着别的键走


def test_reply_reserve_is_below_the_window_limit():
    assert accepts({"quota.window_limit": 10, "quota.reply_reserve": 9}).quota.reply_reserve == 9
    assert accepts({"quota.reply_reserve": 0}).quota.reply_reserve == 0
    message = only_error({"quota.window_limit": 10, "quota.reply_reserve": 10})
    assert "quota.reply_reserve" in message and "quota.window_limit" in message
    assert "quota.reply_reserve" in only_error({"quota.reply_reserve": -1})


def test_window_limit_two_needs_the_reserve_and_keep_to_come_down_too():
    # 默认的回复保留 2、保底留存 3 都放不进 2 条的窗口
    errors = errors_of({"quota.window_limit": 2})
    assert len(errors) == 2
    assert any("quota.reply_reserve" in e for e in errors)
    assert any("quota.deferrable_keep" in e for e in errors)


def test_deferrable_keep_is_up_to_the_window_limit():
    assert accepts({"quota.deferrable_keep": 10}).quota.deferrable_keep == 10
    assert accepts({"quota.deferrable_keep": 0}).quota.deferrable_keep == 0
    assert "quota.deferrable_keep" in only_error({"quota.deferrable_keep": 11})
    assert "quota.deferrable_keep" in only_error({"quota.deferrable_keep": -1})


def test_wait_seconds_is_up_to_max_wait():
    assert accepts({"inbox.wait_seconds": 0}).inbox.wait_seconds == 0
    assert accepts({"inbox.wait_seconds": 55}).inbox.wait_seconds == 55
    assert "inbox.max_wait" in only_error({"inbox.wait_seconds": 56})
    assert "inbox.wait_seconds" in only_error({"inbox.wait_seconds": -1})
    assert accepts({"inbox.max_wait": 100, "inbox.wait_seconds": 100}).inbox.max_wait == 100


def test_warning_hours_stay_inside_the_window():
    assert accepts({"alerts.window_warn.hours_before": 1}).alerts.window_warn.hours_before == 1
    assert accepts({"alerts.window_warn.hours_before": 23}).alerts.window_warn.hours_before == 23
    assert "quota.window_hours" in only_error({"alerts.window_warn.hours_before": 24})
    assert "alerts.window_warn.hours_before" in only_error({"alerts.window_warn.hours_before": 0})
    assert accepts({"quota.window_hours": 48, "alerts.window_warn.hours_before": 47})


def test_part_chars_cannot_exceed_max_chars():
    message = only_error({"send.part_chars": 3000, "send.max_chars": 2000})
    assert "send.part_chars" in message and "send.max_chars" in message
    accepts({"send.part_chars": 2000, "send.max_chars": 2000})


def test_backoff_max_is_not_below_backoff_start():
    message = only_error({"upstream.backoff_start": 30, "upstream.backoff_max": 10})
    assert "upstream.backoff_max" in message and "upstream.backoff_start" in message
    accepts({"upstream.backoff_start": 30, "upstream.backoff_max": 30})


def test_a_dependent_check_is_skipped_when_the_other_key_is_itself_wrong():
    # 窗口总条数写错了，只报它这一处，不顺带把依赖它的两项也报一遍
    message = only_error({"quota.window_limit": -1})
    assert "quota.window_limit" in message
    only_error({"send.max_chars": 70000})  # part_chars 默认 2000 不受牵连


# ---------------------------------------------------------------- 类型


@pytest.mark.parametrize("bad", ["8790", 8790.0, True, None, [8790]])
def test_integer_keys_reject_other_types(bad):
    message = only_error({"server.port": bad})
    assert "server.port" in message and "整数" in message


@pytest.mark.parametrize("bad", ["true", 1, 0, None, "yes"])
def test_boolean_keys_accept_only_true_or_false(bad):
    message = only_error({"server.lan": bad})
    assert "server.lan" in message and "true" in message and "false" in message


@pytest.mark.parametrize("bad", [123, True, None, ["a"]])
def test_string_keys_reject_other_types(bad):
    message = only_error({"alerts.group": bad})
    assert "alerts.group" in message and "字符串" in message


@pytest.mark.parametrize("bad", ["对", 5, None, {"a": 1}])
def test_list_keys_must_be_lists(bad):
    message = only_error({"routing.confirm_words": bad})
    assert "routing.confirm_words" in message and "列表" in message


def test_unquoted_clock_time_gets_a_hint_about_quotes(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("backup:\n  at: 4:00\n", encoding="utf-8")  # YAML 会读成整数 240
    with pytest.raises(ConfigError) as info:
        load_config(path)
    assert len(info.value.errors) == 1
    assert "backup.at" in info.value.errors[0] and "引号" in info.value.errors[0]


# ---------------------------------------------------------------- 枚举和格式


@pytest.mark.parametrize(
    ("key", "good", "bad"),
    [
        ("upstream.profile", ["legacy", "official"], ["x", "Legacy", "", "official "]),
        ("upstream.confirm", ["strict", "observe", "off"], ["maybe", "Strict", ""]),
    ],
)
def test_enum_values(key, good, bad):
    for value in good:
        assert accepts({key: value}).flat()[key] == value
    for value in bad:
        message = only_error({key: value})
        assert key in message and all(name in message for name in good)


def test_log_level_names_are_case_insensitive_and_normalised():
    for name in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        assert accepts({"log.level": name}).log.level == name
    assert accepts({"log.level": "warning"}).log.level == "WARNING"
    assert "log.level" in only_error({"log.level": "LOUD"})
    assert "log.level" in only_error({"log.level": ""})


@pytest.mark.parametrize("value", ["00:00", "04:00", "23:59"])
def test_backup_time_accepts_clock_times(value):
    assert accepts({"backup.at": value}).backup.at == value


@pytest.mark.parametrize("value", ["4:00", "24:00", "04:60", "0400", "04:00:00", "04：00", 240, ""])
def test_backup_time_rejects_the_rest(value):
    assert "backup.at" in only_error({"backup.at": value})


@pytest.mark.parametrize("value", ["07:00-22:30", "00:00-23:59", "07:00-07:00"])
def test_warning_period_accepts_a_range_of_clock_times(value):
    assert accepts({"alerts.window_warn.between": value}).alerts.window_warn.between == value


@pytest.mark.parametrize(
    "value", ["7:00-22:30", "07:00–22:30", "07:00-25:00", "07:00", "07:00-", "-22:30", ""]
)
def test_warning_period_rejects_the_rest(value):
    assert "alerts.window_warn.between" in only_error({"alerts.window_warn.between": value})


def test_warning_weekdays_are_iso_numbers():
    config = accepts({"alerts.window_warn.weekdays": [1, 2, 3, 4, 5, 6, 7]})
    assert config.alerts.window_warn.weekdays == (1, 2, 3, 4, 5, 6, 7)
    assert accepts({"alerts.window_warn.weekdays": []}).alerts.window_warn.weekdays == ()
    for bad in ([0], [8], ["1"], [True], [1.5], [1, None], 3):
        message = only_error({"alerts.window_warn.weekdays": bad})
        assert "alerts.window_warn.weekdays" in message


@pytest.mark.parametrize("name", ["Asia/Shanghai", "Asia/Tokyo", "UTC"])
def test_known_time_zones(name):
    config = accepts({"display.timezone": name})
    assert config.display.timezone == name
    assert str(config.display.tzinfo) == name


@pytest.mark.parametrize(
    "name", ["Mars/Olympus", "", "Asia/Shanghai ", "/etc/passwd", "America", 8]
)
def test_unknown_time_zones(name):
    assert "display.timezone" in only_error({"display.timezone": name})


@pytest.mark.parametrize(
    "url", ["https://ilinkai.weixin.qq.com", "http://127.0.0.1:8123", "https://example.com/"]
)
def test_base_url_accepts_http_and_https(url):
    assert accepts({"upstream.base_url": url}).upstream.base_url == url


@pytest.mark.parametrize("url", ["ilinkai.weixin.qq.com", "ftp://x.example", "https://", "", " "])
def test_base_url_rejects_the_rest(url):
    assert "upstream.base_url" in only_error({"upstream.base_url": url})


def test_message_id_keys_are_a_list_of_names():
    assert accepts({"send.message_id_keys": ["id"]}).send.message_id_keys == ("id",)
    assert accepts({"send.message_id_keys": []}).send.message_id_keys == ()
    for bad in ([""], [1], ["a", None], "message_id"):
        assert "send.message_id_keys" in only_error({"send.message_id_keys": bad})


@pytest.mark.parametrize("word", ["服务", "hub", "量化2", "ＡＢＣ", "字" * 8])
def test_service_word_accepts_the_prefix_format(word):
    assert accepts({"hub.label": word}).hub.label == word


@pytest.mark.parametrize("word", ["", " ", "a b", "好,的", "字" * 9, "a-b", "【服务】", 5])
def test_service_word_rejects_the_rest(word):
    assert "hub.label" in only_error({"hub.label": word})


def test_confirm_words_must_not_be_empty_and_each_word_must_be_valid():
    assert "routing.confirm_words" in only_error({"routing.confirm_words": []})
    for bad in (["对", ""], ["对", "a b"], ["对", 5], ["字" * 9]):
        assert "routing.confirm_words" in only_error({"routing.confirm_words": bad})
    assert accepts({"routing.confirm_words": ["对"]}).routing.confirm_words == ("对",)


# ---------------------------------------------------------------- 谁能连


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1"])
def test_without_lan_the_host_may_be_any_loopback_address(host):
    config = accepts({"server.host": host})
    assert config.server.bind_host == host


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "localhost", "::"])
def test_without_lan_any_other_host_is_an_error(host):
    message = only_error({"server.host": host})
    assert "server.host" in message and "server.lan" in message


def test_an_empty_host_is_an_error_whatever_the_switch_says():
    assert "server.host" in only_error({"server.host": ""})
    assert "server.host" in only_error({"server.host": "", "server.lan": True})


def test_lan_listens_on_all_addresses_and_keeps_the_default_host():
    config = accepts({"server.lan": True})
    assert config.server.bind_host == "0.0.0.0"
    assert config.server.host == "127.0.0.1"
    accepts({"server.lan": True, "server.host": "127.0.0.1"})  # 写得和默认值一样不算改


def test_lan_with_another_host_is_an_error():
    message = only_error({"server.lan": True, "server.host": "192.168.1.5"})
    assert "server.host" in message and "server.lan" in message


def test_the_two_mismatches_from_the_spec_both_fail():
    only_error({"server.lan": False, "server.host": "0.0.0.0"})
    only_error({"server.lan": True, "server.host": "192.168.1.5"})


def test_a_wrongly_typed_switch_does_not_add_a_host_error():
    message = only_error({"server.lan": "yes", "server.host": "192.168.1.5"})
    assert "server.lan" in message


def test_default_bind_host_is_loopback():
    assert Config().server.bind_host == "127.0.0.1"


# ---------------------------------------------------------------- 只警告的情形


def test_shutdown_shorter_than_the_send_deadline_only_warns():
    config = accepts({"server.shutdown_seconds": 20, "send.timeout_seconds": 30})
    warnings = config.warnings()
    assert len(warnings) == 1
    assert "server.shutdown_seconds" in warnings[0] and "send.timeout_seconds" in warnings[0]


def test_equal_or_longer_shutdown_wait_gives_no_warning():
    assert accepts({"server.shutdown_seconds": 30, "send.timeout_seconds": 30}).warnings() == []
    assert accepts({"server.shutdown_seconds": 35, "send.timeout_seconds": 30}).warnings() == []
    assert Config().warnings() == []


# ---------------------------------------------------------------- 不认识的键、顶层的形状


def test_an_unknown_section_is_an_error_with_a_hint():
    message = only_error({"serve.port": 1})
    assert "serve" in message and "server" in message


def test_an_unknown_key_is_an_error_with_a_hint():
    message = only_error({"server.prot": 8790})
    assert "server.prot" in message and "port" in message


def test_an_unknown_key_in_a_nested_section_is_an_error():
    message = only_error({"alerts.window_warn.hour_before": 3})
    assert "alerts.window_warn.hour_before" in message


def test_non_string_keys_are_unknown_keys_too():
    with pytest.raises(ConfigError) as info:
        parse_config({"server": {1: 2}})
    assert len(info.value.errors) == 1 and "server" in info.value.errors[0]


def test_a_section_must_be_a_mapping():
    for bad in (5, "x", [1]):
        with pytest.raises(ConfigError) as info:
            parse_config({"server": bad})
        assert len(info.value.errors) == 1 and "server" in info.value.errors[0]
    with pytest.raises(ConfigError) as info:
        parse_config({"alerts": {"window_warn": 5}})
    assert "alerts.window_warn" in info.value.errors[0]


def test_the_top_level_must_be_a_mapping():
    for bad in ([1, 2], "text", 5):
        with pytest.raises(ConfigError) as info:
            parse_config(bad)
        assert len(info.value.errors) == 1
        assert "顶层" in info.value.errors[0]  # 说得清楚是文件整体的形状不对，不是某一项


def test_errors_come_in_design_order_and_the_message_lists_them_all():
    with pytest.raises(ConfigError) as info:
        parse_config({"log": {"keep": 0}, "server": {"port": 0}, "quota": {"window_limit": 1}})
    errors = info.value.errors
    assert [e.split("：")[0] for e in errors] == ["server.port", "quota.window_limit", "log.keep"]
    text = str(info.value)
    assert "3 处" in text
    assert all(e in text for e in errors)


# ---------------------------------------------------------------- 读文件


def test_values_are_read_from_a_yaml_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "# 我的配置\n"
        "server:\n"
        "  port: 9001   # 换个端口\n"
        "alerts: { window_warn: { weekdays: [1, 3, 5], between: '08:00-21:00' } }\n"
        "routing:\n"
        "  confirm_words: [对, 是, 好]\n"
        "display:\n"
        "  timezone: Asia/Tokyo\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.server.port == 9001
    assert config.alerts.window_warn.weekdays == (1, 3, 5)
    assert config.alerts.window_warn.between == "08:00-21:00"
    assert config.routing.confirm_words == ("对", "是", "好")
    assert config.display.timezone == "Asia/Tokyo"
    assert config.server.host == "127.0.0.1"


def test_invalid_yaml_is_one_error_that_says_where(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("server:\n  port: [\n", encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_config(path)
    assert len(info.value.errors) == 1
    assert "YAML" in info.value.errors[0] and "行" in info.value.errors[0]


def test_a_file_that_is_not_text_is_a_config_error(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(ConfigError) as info:
        load_config(path)
    assert len(info.value.errors) == 1


def test_a_directory_in_place_of_the_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError) as info:
        load_config(tmp_path)
    assert len(info.value.errors) == 1


def test_file_errors_are_listed_with_the_other_validation_errors(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("server: { port: 70000 }\nupstream: { profile: x }\n", encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_config(path)
    assert len(info.value.errors) == 2


def test_a_string_path_works_too(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("server: { port: 9002 }\n", encoding="utf-8")
    assert load_config(str(path)).server.port == 9002
    assert load_config(Path(path)).server.port == 9002
