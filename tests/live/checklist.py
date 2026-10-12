"""真机验证清单的条目：和 `docs/ops/真机验证清单.md` 一一对应（该文档在阶段 D1 写）。

键是 `test_real_ilink.py` 里测试函数名 `test_<键>` 的后缀；`tests/live/test_live_skeleton.py`
检查两边对得上。腾讯一侧的规则（有没有消息编号、窗口多长、凭证能用多久……）不是官方文档写明的，
只能在真账号上量出来，量出来的值再回填到配置默认值和文档。
"""

from __future__ import annotations

ITEMS: dict[str, str] = {
    "message_id_in_send_response": (
        "发送返回里到底有没有消息编号（决定 upstream.confirm 能不能改成 strict）"
    ),
    "legacy_vs_official_profiles": "legacy 与 official 两档在真账号上有没有差别",
    "official_qrcode_post": (
        "official 的 POST 取二维码（请求体 local_token_list）在真账号上能不能用"
    ),
    "notify_paths": (
        "notifystart / notifystop 的真实路径（/ilink/bot/notifystart 还是带一层 /msg）"
    ),
    "window_length_and_quota": "窗口时长和条数的实测",
    "context_token_lifetime": "回复用的对话凭证能用多久",
    "one_login_per_wechat": "一个微信号是不是只认最后一次扫码的登录（只观察，不用再扫一次来试）",
    "old_buf_redelivers": "带着旧的 get_updates_buf 再收，是否重发同一批消息",
    "long_poll_timeout_round_trip": "长轮询读超时算不算完成往返（影响 upstream_down 的判断）",
    "launchd_umask_for_log": "launchd 的 Umask 对 launchd.log 是否生效",
    "qr_login_from_home_network": "家用网络能不能正常扫码",
}
