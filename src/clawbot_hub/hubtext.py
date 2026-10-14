"""服务自己说的话的文字（任务 C3，规格 `hub-inbox`「服务自己的指令和提示」「等确认」
「项目没在线」）。

全是纯函数：给它数据，它给文字。`【服务】` 这个开头是发送时加的（`hub-send` 的标签），不在这里。
文字里不会出现登录凭证、对话凭证、项目密钥——这里根本拿不到它们。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from clawbot_hub.timefmt import span_text

WindowState = Literal["none", "open", "closed"]

NO_PROJECTS = "还没有能收话的项目"

# 登录状态每种一行（同样的状态在 `hubctl status` 里另有写法，这里是发给使用者看的）
_LOGIN_LINES = {
    "ok": "登录状态：正常",
    "expired": "登录状态：已失效，在电脑上运行 hubctl login 重新扫码",
    "missing": "登录状态：还没绑定，在电脑上运行 hubctl login",
    "broken": "登录状态：凭证文件读不出来，重新运行 hubctl login",
}


@dataclass(frozen=True)
class ProjectLine:
    """`服务 项目`、`服务 状态` 里的一个项目：称呼、是不是默认、在不在线。"""

    display: str
    is_default: bool
    online: bool


@dataclass(frozen=True)
class StatusInfo:
    """`服务 状态` 要的数据。`window`：`none` 还没收到过消息、`closed` 已过期、`open` 开着；
    只有 `open` 时 `remaining_seconds`、`used` 才有意义。"""

    login: str
    window: WindowState
    remaining_seconds: float = 0
    used: int = 0
    limit: int = 0
    projects: Sequence[ProjectLine] = ()


def _presence(online: bool) -> str:
    return "在线" if online else "离线"


# ---------------------------------------------------------------- 两个指令的回答


def projects_text(projects: Sequence[ProjectLine]) -> str:
    """`服务 项目`：每个能收话的项目一行。"""
    if not projects:
        return NO_PROJECTS
    lines = ["现在能找的项目："]
    for project in projects:
        name = project.display
        if project.is_default:
            head = f"{name}（默认，{_presence(project.online)}）"
            lines.append(f"{head}：不加前缀的话都给它，也可以写“{name} …”")
        else:
            lines.append(f"{name}（{_presence(project.online)}）：写“{name} …”交给它")
    return "\n".join(lines)


def status_text(info: StatusInfo) -> str:
    """`服务 状态`：登录状态、对话窗口、各项目在不在线。"""
    login = _LOGIN_LINES[info.login]
    if info.window == "open":
        window = (
            f"对话窗口：还剩 {span_text(info.remaining_seconds)}，已用 {info.used}/{info.limit} 条"
        )
    elif info.window == "closed":
        window = "对话窗口：已过期，你再发一句话就开新的"
    else:
        window = "对话窗口：还没收到过你的消息"
    if info.projects:
        presence = "，".join(f"{p.display} {_presence(p.online)}" for p in info.projects)
    else:
        presence = NO_PROJECTS
    return f"{login}\n{window}\n项目：{presence}"


def unknown_command_text(label: str) -> str:
    return f"我只认两个指令：{label} 项目、{label} 状态"


# ---------------------------------------------------------------- 路由时的提示


def no_default_text(label: str) -> str:
    return f"没有设默认项目，这句话没处理：请加项目前缀（发“{label} 项目”看有哪些）"


def disabled_text(display: str) -> str:
    return f"项目「{display}」已停用，这句话没处理"


def offline_text(display: str, minutes: int) -> str:
    return f"「{display}」现在没在线。这句话我先留着：{minutes} 分钟内它回来就会处理，超过就作废"


def full_text(display: str) -> str:
    return f"「{display}」积压太多，这句话没收"


def ambiguous_text(displays: Sequence[str], word: str) -> str:
    """有几个项目在等确认：括号里是各项目的称呼，例子里用第一个项目和使用者发的那个词。"""
    return f"有几个项目在等你确认（{'、'.join(displays)}），请加前缀，例如“{displays[0]} {word}”"
