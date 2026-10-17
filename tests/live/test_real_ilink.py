"""真机验证（任务 0.6、E4）：腾讯一侧的规则，只能在真账号上量。

**默认不跑**（`live` 标记），而且没设 `HUB_LIVE_HOME` 时全部跳过。只在 Mac 上、使用者在场并明确
授权后手动跑，而且要先问清楚：会占用多少条额度，会不会影响现有的微信问答。每项的步骤写在测试的
文档字符串里，和 `docs/ops/真机验证清单.md` 一一对应。

怎么跑（`HUB_LIVE_HOME` 是 E4 的数据目录，里面要有 `account.json`，结果也写在这里的
`e4-results.jsonl`；`-s` 才看得到“请用户……”的提示；`HUB_TEST_TIMEOUT=0` 关掉看门狗）：

    HUB_LIVE_HOME=~/.clawbot-hub-e4 HUB_TEST_TIMEOUT=0 \\
        uv run pytest -m live -n0 -s tests/live/test_real_ilink.py -k <测试名>

探针本身在 `tests/live/probes.py`（对着假腾讯自测，每次都跑）：只做清单上写的请求，只记形状
（状态码、返回体的键名和类型、耗时、条数），登录凭证、对话凭证、消息正文不进输出和结果文件。
量到的值写在清单末尾的「结果记录」，再回填到文档和配置默认值，**不进这个文件**。
注意：这些测试跑起来会访问真实的腾讯接口，不装联网守卫，不清代理环境（所以走不走代理跟这台
机器的设置一样，结果里的 `via_proxy` 会记下来）。探针和服务不能同时向腾讯收消息。
"""

from __future__ import annotations

import pytest

from tests.live import probes

pytestmark = pytest.mark.live


@pytest.fixture
def env() -> probes.ProbeEnv:
    try:
        return probes.live_env()
    except probes.LiveNotConfigured as exc:
        pytest.skip(str(exc))


def test_message_id_in_send_response(env):
    """步骤：已绑定、窗口开着（探针会请使用者先在微信里给 ClawBot 发一句话）。用 legacy 档发 3 条短
    消息（占 3 条额度），记录每次返回体里出现的键名，不记正文；再发 1 条不带对话凭证的作对比
    （第 4 条额度）。断言：发了 3 条正常的；记录里有每条的键名，是不是有数字的 message_id /
    msg_id 看记录。"""
    result = probes.message_id_in_send_response(env)
    assert len(result["normal_with_id"]) == 3


def test_legacy_vs_official_profiles(env):
    """步骤：legacy 与 official 各发一批（每批 2 条，占 4 条额度），先用 legacy 作基线，对比：
    是否都成功、返回体是否一样、有没有被拒绝。断言：两档各发了 2 条；差别看记录。
    （official 启动时 notifystart 有没有报错，要起服务看 `hubctl events --kind lifecycle`，
    见运行手册。）"""
    result = probes.legacy_vs_official_profiles(env)
    assert {profile: len(outcomes) for profile, outcomes in result.items()} == {
        "legacy": 2,
        "official": 2,
    }


def test_official_qrcode_post(env):
    """步骤：对着真实接口 POST /ilink/bot/get_bot_qrcode?bot_type=3，请求体
    {"local_token_list": []}（legacy 的 GET 作对照）。只取码、不扫，码也不记。
    注意：不要扫这张码（会挤掉正在用的绑定）。断言：两种写法都取了一次；返回的形状一样不一样
    看记录。"""
    result = probes.official_qrcode_post(env)
    assert set(result) >= {"legacy", "official"}


def test_notify_paths(env):
    """步骤：official 档下分别请求 /ilink/bot/notifystart 和 /ilink/bot/msg/notifystart，
    记录 HTTP 状态码和返回体的形状。只测 notifystart，不调 notifystop。断言：两个路径都请求了；
    哪一个可用看记录，回填到 upstream/http.py 和假腾讯。"""
    result = probes.notify_paths(env)
    assert set(result) == set(probes.NOTIFY_PATHS)


def test_window_length_and_quota():
    """步骤：不影响日常使用的时段，用服务的发送记录和被拒绝的时间点估算——使用者发一句话后，
    每隔一段时间发一条，记下第一次被拒绝的时刻和已发条数。断言（E4 里使用者另点头才做）：
    窗口时长、条数上限（默认 24 小时 / 10 条是用户报告，不是官方的）。"""
    pytest.skip("E4 里这一项要用满一个窗口的额度、要跨一天以上，开始前单独问使用者，不跟着其他项跑")


def test_context_token_lifetime(env):
    """步骤：取一条使用者消息的 context_token，之后每隔一段时间（0、1、2、5、10、30 分钟）
    用它回复一条（占 6 条额度），记录第一次被拒绝的时间。断言：至少回复了第一条；
    凭证实际能用多久看记录，回填 reply.handle_minutes。"""
    result = probes.context_token_lifetime(env)
    assert result["outcomes"]


def test_one_login_per_wechat():
    """只观察，**不要用再扫一次来试**（会挤掉正在用的绑定）：问使用者 2026-10-10 00:16 到 00:32
    （东京时间）有没有给别的项目扫过码；以后有第二个项目接入时，观察掉线的时间点。
    断言：无。把观察到的情况写进文档。"""
    pytest.skip("只观察，不是探针；E4 时当面问使用者，答案写进结果记录")


def test_old_buf_redelivers(env):
    """步骤：收一批消息并记下返回的 get_updates_buf，再用上一次的（旧的）buf 请求一次，
    看是否重发同一批；再用新的 buf 要一次，应该是空的。ashare-quant 现在就依赖这一点。
    断言：第一批收到了；重发 / 不重发看记录。"""
    result = probes.old_buf_redelivers(env)
    assert result["first_count"] >= 1


def test_long_poll_timeout_round_trip(env):
    """步骤：没有新消息时发一次 getupdates，记录从发出到收到返回的耗时、返回体
    （空的 msgs 还是别的）、有没有 longpolling_timeout_ms。客户端读超时放宽到 90 秒，
    这样“腾讯自己回了个空结果”和“挂到我们读超时”分得开。断言：量到了两次空闲时的结果；
    读超时算不算一次完成的往返看记录。"""
    result = probes.long_poll_timeout_round_trip(env)
    assert len(result["kinds"]) >= 2


def test_launchd_umask_for_log():
    """步骤：按安装说明装好 launchd 服务（演练用的单独数据目录和 label），启动后 ls -l 看
    launchd.log 的权限。断言：是 0600 就生效；不是，就把标准输出和错误指向 /dev/null
    （服务自己已经写 hub.log）。"""
    pytest.skip(
        "已在 2026-10-17 的 E3 演练里量过（launchd.log 是 0600），见清单的结果记录；E4 不重做"
    )


def test_qr_login_from_home_network(env):
    """步骤：在家里的网络上取码、查一次扫码状态（不扫），记录状态和提示。云服务器上扫码曾被微信拦
    （“运行环境存在风险”）。真的扫码绑定只在使用者决定正式绑定或重新绑定时做，不为了测试而扫。
    断言：取到码、查到状态；万一不能，记下提示的原文。"""
    result = probes.qr_login_from_home_network(env)
    assert "ok" in result
