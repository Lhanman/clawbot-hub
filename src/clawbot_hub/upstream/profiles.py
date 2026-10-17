"""两档请求形式：`legacy`（和 ashare-quant 现在发的一致）与 `official`（按腾讯官方协议文档补齐）。

期望值逐项写在 `hub-login` 的「上游请求的形式」里，`tests/upstream/test_http_profiles.py` 逐项核对。
这里只决定请求头和 `base_info` 的内容，请求怎么发、结果怎么分类在 `http.py`。
"""

from __future__ import annotations

import base64
import random
from dataclasses import dataclass

from clawbot_hub import __version__


def encode_client_version(version: str) -> str:
    """官方的 `iLink-App-ClientVersion`：版本号按 `0x00MMNNPP` 编成十进制字符串。"""
    major, minor, patch = (int(part) for part in version.split("."))
    return str((major << 16) | (minor << 8) | patch)


def _uin() -> str:
    """`X-WECHAT-UIN`：随机 uint32 的十进制字符串再 base64，每次请求不同。"""
    return base64.b64encode(str(random.getrandbits(32)).encode()).decode()


@dataclass(frozen=True)
class Profile:
    name: str
    channel_version: str
    app_id: str | None = None  # 官方档才有
    client_version: str | None = None
    bot_agent: str | None = None
    qr_start_by_post: bool = False  # 官方文档里取二维码是 POST，legacy 是 GET
    uses_notify: bool = False  # 官方档启动 / 退出时调 notifystart / notifystop

    def base_info(self) -> dict[str, str]:
        info = {"channel_version": self.channel_version}
        if self.bot_agent:
            info["bot_agent"] = self.bot_agent
        return info

    def app_headers(self) -> dict[str, str]:
        """不带登录凭证的请求（取二维码、查扫码状态）也要有的头；legacy 没有。"""
        headers: dict[str, str] = {}
        if self.app_id:
            headers["iLink-App-Id"] = self.app_id
        if self.client_version:
            headers["iLink-App-ClientVersion"] = self.client_version
        return headers

    def bot_headers(self, bot_token: str) -> dict[str, str]:
        """带登录凭证的请求的头。legacy 恰好这 4 个自定义的头（传输层自带的不算）。"""
        return {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {bot_token}",
            "X-WECHAT-UIN": _uin(),
            **self.app_headers(),
        }


OFFICIAL_VERSION = "2.4.8"

LEGACY = Profile(name="legacy", channel_version="1.0.2")
OFFICIAL = Profile(
    name="official",
    channel_version=OFFICIAL_VERSION,
    app_id="bot",
    client_version=encode_client_version(OFFICIAL_VERSION),
    bot_agent=f"clawbot-hub/{__version__}",
    qr_start_by_post=True,
    uses_notify=True,
)

PROFILES = {profile.name: profile for profile in (LEGACY, OFFICIAL)}


def get_profile(name: str) -> Profile:
    try:
        return PROFILES[name]
    except KeyError:
        known = "、".join(PROFILES)
        raise ValueError(f"不认识的 upstream.profile：{name!r}（可选：{known}）") from None
