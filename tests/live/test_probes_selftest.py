"""真机验证探针（`probes.py`）的自测：对着假腾讯跑一遍，每次都跑，不联网（只连本机回环）。

真机上第一次跑探针的时候，不该是探针自己的毛病先暴露：这里确认每一项探针
- 发的请求就是清单上写的那几个（该发几条、带什么对话凭证、哪个路径、有没有调 `notifystop`）；
- 记下的是形状（状态码、键名、类型、耗时、条数），登录凭证、对话凭证、使用者和机器人的编号、
  消息正文**一个字也不会**进输出和结果文件；
- 碰到 -14 就停、发消息有总条数上限、等使用者发话有时限。
"""

from __future__ import annotations

import json
import stat
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from clawbot_hub.account import AccountFile, AccountStore
from clawbot_hub.config import UpstreamConfig
from clawbot_hub.paths import HubPaths, ensure_home
from tests.live import probes
from tests.support.fake_ilink import (
    GETUPDATES,
    SENDMESSAGE,
    FakeIlink,
    error,
    hang,
    ok,
)

BOT_TOKEN = "tok-SECRET-1"
USER_TEXT = "秘密正文XYZ"


@dataclass
class Lab:
    fake: FakeIlink
    env: probes.ProbeEnv
    lines: list[str]
    home: Path
    skipped: list[float]

    def results(self) -> list[dict]:
        path = self.home / probes.RESULTS_NAME
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def steps(self, item: str) -> dict[str, dict]:
        return {row["step"]: row for row in self.results() if row["item"] == item}

    def assert_clean(self) -> None:
        """结果文件和打印出来的字里，没有任何秘密、编号、正文。"""
        text = (self.home / probes.RESULTS_NAME).read_text(encoding="utf-8")
        text += "\n".join(self.lines)
        for needle in (
            BOT_TOKEN,
            "ctx-",
            self.fake.user_id,
            self.fake.bot_id,
            USER_TEXT,
            "fake-qr-",
            "buf-",
        ):
            assert needle not in text, f"输出里不该有 {needle}"


def skipping_clock() -> tuple[probes.Clock, list[float]]:
    """等待不真睡：睡多久就把时钟往前拨多久。"""
    skipped = [0.0]
    clock = probes.Clock(
        monotonic=lambda: time.monotonic() + skipped[0],
        sleep=lambda seconds: skipped.__setitem__(0, skipped[0] + seconds),
        wall_ms=lambda: time.time_ns() // 1_000_000 + int(skipped[0] * 1000),
    )
    return clock, skipped


def make_lab(tmp_path: Path, fake: FakeIlink, **env_options) -> Lab:
    home = tmp_path / "e4"
    paths = HubPaths(home)
    ensure_home(paths)
    AccountStore(paths).write(
        AccountFile(
            bot_token=BOT_TOKEN,
            user_id=fake.user_id,
            base_url=fake.base_url,
            bot_id=fake.bot_id,
            updates_buf="buf-0",
        )
    )
    clock, skipped = skipping_clock()
    lines: list[str] = []
    env = probes.ProbeEnv(
        home,
        config=UpstreamConfig(base_url=fake.base_url, poll_timeout=1, request_timeout=5),
        clock=clock,
        say=lines.append,
        wait_seconds=env_options.pop("wait_seconds", 30),
        **env_options,
    )
    return Lab(fake, env, lines, home, skipped)


@pytest.fixture
def lab(tmp_path):
    with FakeIlink(longpoll_seconds=0.2) as fake:
        yield make_lab(tmp_path, fake)


# ---------------------------------------------------------------- 记录员


def test_the_recorder_refuses_secrets_and_text_that_looks_like_a_body(tmp_path):
    lines: list[str] = []
    path = tmp_path / "results.jsonl"
    recorder = probes.Recorder(path, lines.append, probes.Clock())
    recorder.watch_secret("tok-SECRET")
    recorder.record("x", "fine", status=200, keys=["ret"], nested={"a": ["b"]})
    with pytest.raises(probes.SecretLeak):
        recorder.record("x", "leak", reason="带着 tok-SECRET 的一句话")
    with pytest.raises(probes.SecretLeak):
        recorder.record("x", "long", requests=[{"errmsg": "字" * 200}])
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1  # 只写了第一条
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_live_env_reads_the_environment_and_skips_when_not_configured():
    with pytest.raises(probes.LiveNotConfigured, match="E4"):
        probes.live_env({})
    env = probes.live_env(
        {
            probes.HOME_ENV: "/tmp/e4-home",
            probes.WAIT_ENV: "5",
            probes.REPLY_MINUTES_ENV: "0, 3",
            probes.BASE_URL_ENV: "http://127.0.0.1:9",
            probes.MAX_SENDS_ENV: "12",
        },
        say=lambda text: None,
    )
    assert env.home == Path("/tmp/e4-home")
    assert env.wait_seconds == 5
    assert env.max_sends == 12
    assert env.reply_minutes == (0.0, 3.0)
    assert env.config.base_url == "http://127.0.0.1:9"


# ---------------------------------------------------------------- 取码类（不要登录凭证）


def test_official_qrcode_post_fetches_both_ways_without_recording_the_code(tmp_path):
    with FakeIlink() as fake:
        lab = make_lab(tmp_path, fake)
        result = probes.official_qrcode_post(lab.env)
        assert result["same_keys"] is True
        steps = lab.steps("official_qrcode_post")
        assert steps["qr_start_legacy"]["method"] == "GET"
        assert steps["qr_start_official"]["method"] == "POST"
        assert steps["qr_start_official"]["keys"] == ["qrcode", "qrcode_img_content"]
        assert [r.method for r in fake.requests_to("get_bot_qrcode")] == ["GET", "POST"]
        lab.assert_clean()


def test_qr_login_from_home_network_fetches_and_checks_the_status_once(tmp_path):
    with FakeIlink(confirm_after=999) as fake:
        lab = make_lab(tmp_path, fake)
        result = probes.qr_login_from_home_network(lab.env)
        assert result == {"ok": True, "status": "wait"}
        assert fake.logins == 0  # 没有扫，也没有被确认
        lab.assert_clean()


def test_qr_failures_are_recorded_not_raised(tmp_path):
    with FakeIlink() as fake:
        lab = make_lab(tmp_path, fake)
        fake.script("get_bot_qrcode", error(500, "boom"), error(500, "boom"))
        result = probes.official_qrcode_post(lab.env)
        assert result["legacy"]["ok"] is False and result["official"]["ok"] is False
        assert lab.steps("official_qrcode_post")["qr_start_official"]["error"]


# ---------------------------------------------------------------- notifystart


def test_notify_paths_probes_both_paths_with_the_official_headers_and_never_notifystop(lab):
    # 假腾讯默认就和 E4 量到的真腾讯一样：不带 /msg 回 404，带 /msg 回 200
    result = probes.notify_paths(lab.env)
    assert result == {"/ilink/bot/notifystart": 404, "/ilink/bot/msg/notifystart": 200}
    requests_made = lab.fake.requests_to("notifystart")
    assert len(requests_made) == 2
    assert all(r.header("iLink-App-Id") == "bot" for r in requests_made)
    assert lab.fake.requests_to("notifystop") == []
    steps = [r for r in lab.results() if r["item"] == "notify_paths"]
    assert [r["request_path"] for r in steps] == list(probes.NOTIFY_PATHS)
    lab.assert_clean()


# ---------------------------------------------------------------- 长轮询


def test_long_poll_records_an_empty_reply_from_the_server_and_its_hint(tmp_path):
    with FakeIlink(longpoll_seconds=0.2, longpoll_hint_ms=35000) as fake:
        lab = make_lab(tmp_path, fake)
        result = probes.long_poll_timeout_round_trip(lab.env, idle_runs=2, extra_seconds=1)
        assert result["kinds"] == ["empty_response", "empty_response"]
        row = [r for r in lab.results() if r["step"] == "idle_poll"][0]
        assert row["kind"] == "empty_response"
        assert row["longpolling_timeout_ms"] == 35000
        assert row["message_count"] == 0
        assert row["types"]["msgs"] == "list"
        assert any("▶ 请用户先不要" in line for line in lab.lines)
        lab.assert_clean()


def test_long_poll_tells_a_hanging_server_from_an_empty_reply(lab):
    lab.fake.set_default(GETUPDATES, hang(5))
    result = probes.long_poll_timeout_round_trip(lab.env, idle_runs=1, extra_seconds=0.2)
    assert result["kinds"] == ["client_read_timeout"]
    row = [r for r in lab.results() if r["step"] == "idle_poll"][0]
    assert row["elapsed_seconds"] >= 1.0  # 等满了 poll_timeout + extra


def test_long_poll_consumed_messages_are_not_idle_and_the_text_is_never_recorded(lab):
    lab.fake.push_user_message(USER_TEXT)
    result = probes.long_poll_timeout_round_trip(lab.env, idle_runs=1, extra_seconds=1)
    assert result["kinds"] == ["messages", "empty_response"]
    first = [r for r in lab.results() if r["step"] == "idle_poll"][0]
    assert first["message_count"] == 1
    assert lab.env.cursor() == "buf-1"  # 收走的进度存下了
    lab.assert_clean()


def test_session_expired_stops_every_probe_that_polls(lab):
    lab.fake.set_mode("session_expired")
    with pytest.raises(probes.SessionLost, match="不要扫码"):
        probes.long_poll_timeout_round_trip(lab.env)
    with pytest.raises(probes.SessionLost):
        probes.old_buf_redelivers(lab.env)
    assert [r["outcome"] for r in lab.results() if "outcome" in r] == ["SessionExpired"] * 2


# ---------------------------------------------------------------- 旧的进度


@pytest.mark.parametrize("redeliver", [True, False])
def test_old_buf_probe_tells_redelivery_from_no_redelivery(tmp_path, redeliver):
    with FakeIlink(longpoll_seconds=0.2, redeliver=redeliver) as fake:
        lab = make_lab(tmp_path, fake)
        fake.push_user_message(USER_TEXT)
        result = probes.old_buf_redelivers(lab.env)
        assert result == {
            "redelivered": redeliver,
            "first_count": 1,
            "again_count": 1 if redeliver else 0,
        }
        steps = lab.steps("old_buf_redelivers")
        assert steps["first_batch"]["message_count"] == 1
        assert steps["first_batch"]["buf_changed"] is True
        assert steps["old_buf_again"]["same_seqs_as_first"] is redeliver
        assert steps["new_buf_again"]["message_count"] == 0
        # 留给后面几项：最新的进度和这条消息的对话凭证
        assert lab.env.cursor() == "buf-1"
        assert lab.env.load_context().token == "ctx-1"
        lab.assert_clean()


def test_the_probe_only_counts_messages_from_the_owner(lab):
    lab.fake.push_bot_message("机器人自己的话")
    lab.fake.push_user_message("别人的话", from_user_id="stranger@im.wechat")
    lab.fake.push_user_message(USER_TEXT)
    received = probes.wait_for_user_message(
        lab.env, lab.env.upstream("legacy"), item="t", purpose="x"
    )
    assert [m.context_token for m in received.messages] == ["ctx-3"]
    assert lab.env.cursor() == "buf-3"


def test_waiting_for_the_owner_times_out(tmp_path):
    with FakeIlink(longpoll_seconds=0.2) as fake:
        lab = make_lab(tmp_path, fake, wait_seconds=1.0)
        with pytest.raises(probes.WaitTimeout):
            probes.wait_for_user_message(lab.env, lab.env.upstream("legacy"), item="t", purpose="x")
        assert any("▶ 请用户现在在微信里给 ClawBot 发一句话" in line for line in lab.lines)


# ---------------------------------------------------------------- 发消息


def test_message_id_probe_sends_three_with_the_token_and_one_without(lab):
    lab.fake.push_user_message(USER_TEXT)
    result = probes.message_id_in_send_response(lab.env)
    assert result == {"normal_with_id": [True, True, True], "control_with_id": True}
    sent = lab.fake.sent
    assert [m.context_token for m in sent] == ["ctx-1", "ctx-1", "ctx-1", ""]
    assert all(m.to_user_id == lab.fake.user_id for m in sent)
    assert len({m.client_id for m in sent}) == 4
    steps = lab.steps("message_id_in_send_response")
    assert steps["normal_1"]["keys"] == ["errmsg", "message_id", "ret"]
    assert steps["normal_1"]["types"]["message_id"] == "int"
    assert steps["no_context_token"]["has_context_token"] is False
    assert any("▶ 请用户看一下微信" in line for line in lab.lines)
    lab.assert_clean()


def test_message_id_probe_records_what_a_reply_without_an_id_looks_like(tmp_path):
    with FakeIlink(longpoll_seconds=0.2, message_ids=False) as fake:
        lab = make_lab(tmp_path, fake)
        fake.push_user_message(USER_TEXT)
        result = probes.message_id_in_send_response(lab.env)
        assert result["normal_with_id"] == [False, False, False]
        assert lab.steps("message_id_in_send_response")["normal_1"]["keys"] == ["errmsg", "ret"]


def test_message_id_probe_records_a_rejection_and_keeps_going(lab):
    lab.fake.push_user_message(USER_TEXT)
    lab.fake.script(SENDMESSAGE, error(40001, "no quota"), ok(message_id=1), ok(message_id=2))
    probes.message_id_in_send_response(lab.env)
    steps = lab.steps("message_id_in_send_response")
    assert steps["normal_1"]["outcome"] == "Rejected"
    assert steps["normal_1"]["ret"] == 40001
    assert steps["normal_1"]["errmsg"] == "no quota"
    assert steps["normal_2"]["outcome"] == "Accepted"


def test_the_context_from_a_moment_ago_is_reused_and_an_old_one_is_not(lab):
    up = lab.env.upstream("legacy")
    lab.fake.push_user_message(USER_TEXT)
    first = probes.fresh_context(lab.env, up, item="t", purpose="x")
    again = probes.fresh_context(lab.env, up, item="t", purpose="x")
    assert again == first
    assert any(line.startswith("（用刚才那条消息的对话凭证") for line in lab.lines)
    lab.env.clock.sleep(200)  # 比 90 秒久：不能再用
    lab.fake.push_user_message(USER_TEXT)
    newer = probes.fresh_context(lab.env, up, item="t", purpose="x")
    assert newer.token == "ctx-2"


def test_profiles_probe_sends_two_per_profile_with_each_profiles_headers(lab):
    lab.fake.push_user_message(USER_TEXT)
    result = probes.legacy_vs_official_profiles(lab.env)
    assert result == {"legacy": ["Accepted"] * 2, "official": ["Accepted"] * 2}
    requests_made = lab.fake.requests_to("sendmessage")
    assert [r.header("iLink-App-Id") for r in requests_made] == [None, None, "bot", "bot"]
    assert [m.text for m in lab.fake.sent][0].startswith("E4 对比 legacy 1/2")
    lab.assert_clean()


def test_the_send_total_has_a_limit_that_adds_up_across_probes_and_runs(tmp_path):
    with FakeIlink(longpoll_seconds=0.2) as fake:
        lab = make_lab(tmp_path, fake, max_sends=6)
        fake.push_user_message(USER_TEXT)
        probes.message_id_in_send_response(lab.env)  # 3 条加 1 条对比
        assert lab.env.sends == 4
        with pytest.raises(probes.ProbeError, match="上限"):
            probes.legacy_vs_official_profiles(lab.env)  # 想再发 4 条，发到第 6 条就停
        assert len(fake.requests_to("sendmessage")) == 6
        # 探针分几次跑（新的进程、新的环境对象）也接着数，不是每次从零开始
        again = probes.ProbeEnv(
            lab.home,
            config=lab.env.config,
            clock=lab.env.clock,
            say=lambda text: None,
            max_sends=6,
        )
        assert again.sends == 6
        with pytest.raises(probes.ProbeError, match="上限"):
            probes.send_probe(
                again,
                again.upstream("legacy"),
                item="x",
                step="y",
                to_user_id=fake.user_id,
                token="ctx-x",
                text="测试",
                profile="legacy",
            )
        assert len(fake.requests_to("sendmessage")) == 6
        assert stat.S_IMODE((lab.home / probes.SENDS_NAME).stat().st_mode) == 0o600


def test_a_send_that_meets_minus_14_stops_the_probe(lab):
    lab.fake.push_user_message(USER_TEXT)
    lab.fake.script(SENDMESSAGE, ok(message_id=1), error(-14, "session expired"))
    with pytest.raises(probes.SessionLost):
        probes.message_id_in_send_response(lab.env)
    assert len(lab.fake.requests_to("sendmessage")) == 2  # 第 2 条碰到 -14 就停，没有再发


# ---------------------------------------------------------------- 对话凭证的有效期


def test_token_lifetime_probe_replies_on_schedule_and_stops_at_the_first_rejection(tmp_path):
    with FakeIlink(longpoll_seconds=0.2) as fake:
        lab = make_lab(tmp_path, fake, reply_minutes=(0.0, 1.0, 2.0, 5.0))
        fake.push_user_message(USER_TEXT)
        fake.script(SENDMESSAGE, ok(message_id=1), ok(message_id=2), error(40001, "expired"))
        result = probes.context_token_lifetime(lab.env)
        assert result == {
            "first_rejected_minutes": 2.0,
            "outcomes": ["Accepted", "Accepted", "Rejected"],
        }
        tokens = [r.json["msg"]["context_token"] for r in fake.requests_to("sendmessage")]
        assert tokens == ["ctx-1"] * 3  # 三条都用同一条消息的对话凭证，被拒绝以后不再发
        assert lab.skipped[0] >= 119  # 真的按间隔等过（时钟拨过了 2 分钟）
        ages = [r["age_minutes"] for r in lab.results() if r["step"].startswith("age_")]
        assert ages == sorted(ages) and ages[-1] >= 2.0
        summary = lab.steps("context_token_lifetime")["summary"]
        assert summary["first_rejected_minutes"] == 2.0
        lab.assert_clean()


def test_token_lifetime_probe_goes_through_every_offset_when_nothing_is_rejected(tmp_path):
    with FakeIlink(longpoll_seconds=0.2) as fake:
        lab = make_lab(tmp_path, fake, reply_minutes=(0.0, 1.0, 5.0))
        fake.push_user_message(USER_TEXT)
        result = probes.context_token_lifetime(lab.env)
        assert result["first_rejected_minutes"] is None
        assert len(fake.sent) == 3
        assert lab.skipped[0] >= 299


def test_results_file_is_private_and_appended(lab):
    lab.fake.push_user_message(USER_TEXT)
    probes.message_id_in_send_response(lab.env)
    path = lab.home / probes.RESULTS_NAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((lab.home / probes.CONTEXT_NAME).stat().st_mode) == 0o600
    count = len(path.read_text(encoding="utf-8").splitlines())
    probes.official_qrcode_post(lab.env)
    assert len(path.read_text(encoding="utf-8").splitlines()) > count
