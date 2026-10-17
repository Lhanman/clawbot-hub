"""真机验证（任务 0.6）：腾讯一侧的规则，只能在真账号上量。

**默认不跑**（`live` 标记）。只在 Mac 上、使用者在场并明确授权后，
用 `uv run pytest -m live` 手动跑，而且要先问清楚：会占用多少条额度，
会不会影响现有的微信问答。每项的步骤写在测试的文档字符串里，
和 `docs/ops/真机验证清单.md` 一一对应；量到的值回填到文档和配置默认值，不进这个文件。

现在是骨架（阶段 0）：只有步骤和断言占位，一律跳过。E4 在 Mac 上授权后才填实测值。
注意：这些测试跑起来会访问真实的腾讯接口，不装联网守卫，不清代理环境。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.live

SKELETON = "骨架：E4 在 Mac 上经使用者授权后才填实测值"


def test_message_id_in_send_response():
    """步骤：已绑定、窗口开着（先在微信里给 ClawBot 发一句话）。用 legacy 档发 3 条短消息
    （占 3 条额度），记录每次返回体里出现的键名，不记正文；再在窗口外（或没发过话）试一次作对比。
    断言（E4 填）：正常账号的返回里有数字的 message_id / msg_id；对比组没有。"""
    pytest.skip(SKELETON)


def test_legacy_vs_official_profiles():
    """步骤：legacy 与 official 各发一批（每批 2 条），先用 legacy 作基线，对比：
    是否都成功、返回体是否一样、有没有被拒绝。断言（E4 填）：两档的结果一致，或记下差别。"""
    pytest.skip(SKELETON)


def test_official_qrcode_post():
    """步骤：对着真实接口 POST /ilink/bot/get_bot_qrcode?bot_type=3，请求体
    {"local_token_list": []}。只取码、不扫，看返回有没有 qrcode 和 qrcode_img_content。
    注意：不要扫这张码（会挤掉正在用的绑定）。断言（E4 填）：返回形状和 GET 的一样。"""
    pytest.skip(SKELETON)


def test_notify_paths():
    """步骤：official 档下分别请求 /ilink/bot/notifystart 和 /ilink/bot/msg/notifystart，
    记录 HTTP 状态码和返回体。断言（E4 填）：哪一个路径可用，回填到 upstream/http.py 和假腾讯。"""
    pytest.skip(SKELETON)


def test_window_length_and_quota():
    """步骤：不影响日常使用的时段，用服务的发送记录和被拒绝的时间点估算——使用者发一句话后，
    每隔一段时间发一条，记下第一次被拒绝的时刻和已发条数。断言（E4 填）：窗口时长、条数上限
    （默认 24 小时 / 10 条是用户报告，不是官方的）。"""
    pytest.skip(SKELETON)


def test_context_token_lifetime():
    """步骤：取一条使用者消息的 context_token，之后每隔一段时间（1、2、5、10、30 分钟……）
    用它回复一条，记录第一次被拒绝的时间。
    断言（E4 填）：凭证实际能用多久，回填 reply.handle_minutes。"""
    pytest.skip(SKELETON)


def test_one_login_per_wechat():
    """只观察，**不要用再扫一次来试**（会挤掉正在用的绑定）：问使用者 2026-10-10 00:16 到 00:32
    （东京时间）有没有给别的项目扫过码；以后有第二个项目接入时，观察掉线的时间点。
    断言（E4 填）：无。把观察到的情况写进文档。"""
    pytest.skip(SKELETON)


def test_old_buf_redelivers():
    """步骤：收一批消息并记下返回的 get_updates_buf，再用上一次的（旧的）buf 请求一次，
    看是否重发同一批。ashare-quant 现在就依赖这一点。断言（E4 填）：重发 / 不重发。"""
    pytest.skip(SKELETON)


def test_long_poll_timeout_round_trip():
    """步骤：没有新消息时发一次 getupdates，记录从发出到收到返回的耗时、返回体
    （空的 msgs 还是别的）、有没有 longpolling_timeout_ms。
    断言（E4 填）：读超时算不算一次完成的往返。"""
    pytest.skip(SKELETON)


def test_launchd_umask_for_log():
    """步骤：按安装说明装好 launchd 服务（演练用的单独数据目录和 label），启动后 ls -l 看
    launchd.log 的权限。断言（E4 填）：是 0600 就生效；不是，就把标准输出和错误指向 /dev/null
    （服务自己已经写 hub.log）。"""
    pytest.skip(SKELETON)


def test_qr_login_from_home_network():
    """步骤：在家里的网络上取码、扫码、确认，记录各状态。云服务器上扫码曾被微信拦
    （"运行环境存在风险"）。断言（E4 填）：家里能正常扫码；万一不能，记下提示的原文。"""
    pytest.skip(SKELETON)
