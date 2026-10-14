"""路由：一句话交给哪个项目，或者由服务自己回复（任务 C1）。

规格：`hub-inbox` 的「一句话交给谁」和「等确认」。

纯函数，不碰数据库和时钟：调用者（poller）在一个事务里读出项目表的快照，拿结果去建待取记录、
写限频记录、清等确认标记。顺序是规格定的，第一个命中的生效：

1. 没有文字项（图片、语音……）：默认项目；
2. 服务自己的指令：话是 `hub.label`，或者它后面跟着分隔符；
3. 带前缀：话以某个项目的前缀开头，后面紧跟分隔符或就此结束（停用的项目由服务回一句）；
4. 确认话：整句话正好是一个确认词，并且恰有一个项目在等确认；
5. 其余的话：默认项目（没有、或者已停用，由服务回一句）。

交给项目的文字是从**原始字符串**里按位置切出来的（NFKC 只用来比较，不改写它），去掉首尾空白。
不理解话的意思：指令词只有 `hub.label` 后面的两个固定词，其余一概交给项目自己看。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from clawbot_hub.projects import Project
from clawbot_hub.textutil import normalize, strip_prefix

Via = Literal["prefix", "confirm", "default"]

# `服务 项目`、`服务 状态` 里的两个固定词（`服务` 本身取 `hub.label`）
COMMAND_PROJECTS = "项目"
COMMAND_STATUS = "状态"


class Reply(StrEnum):
    """服务自己要回的话的种类；文字由 `replier` 按种类写。"""

    PROJECTS = "projects"  # 服务 项目
    STATUS = "status"  # 服务 状态
    UNKNOWN_COMMAND = "unknown_command"  # 服务 后面是别的字，或者没有字
    NO_DEFAULT = "no_default"  # 没有默认项目，这句话没处理
    DISABLED = "disabled"  # 目标项目已停用，这句话没处理
    CONFIRM_AMBIGUOUS = "confirm_ambiguous"  # 有几个项目在等确认，请加前缀


@dataclass(frozen=True)
class Snapshot:
    """路由要看的东西：项目表在这一刻的样子。

    `projects` 按登记先后排；只推送的、已移除的项目路由时不看（传进来也没关系）。
    `online` 是此刻在线的项目名（见规格 `hub-projects`「项目在线判断」），由调用者算好。
    """

    projects: tuple[Project, ...]
    online: frozenset[str]
    label: str  # `hub.label`
    confirm_words: tuple[str, ...]  # `routing.confirm_words`


@dataclass(frozen=True)
class ToProject:
    """这句话交给 `project`，文字是 `text`，路由方式是 `via`。

    `clear_awaiting`：这一刻发现已经不管用的等确认标记（到期、项目不在线、已停用），调用者
    要把这几个项目的标记清掉。只有收到确认话的那一刻才会有；目标项目自己的标记（收到任何话
    就清）不在里面，那是入队时的事。
    """

    project: Project
    text: str
    has_text: bool
    via: Via
    clear_awaiting: tuple[str, ...] = ()


@dataclass(frozen=True)
class HubReply:
    """由服务自己回复，这句话不交给任何项目。

    `project`：`DISABLED` 时是已停用的那个项目；`awaiting` 和 `word`：`CONFIRM_AMBIGUOUS` 时
    是在等确认的几个项目（按登记先后）和使用者发的那个词；`clear_awaiting` 同 `ToProject`。
    """

    kind: Reply
    project: Project | None = None
    awaiting: tuple[Project, ...] = ()
    word: str = ""
    clear_awaiting: tuple[str, ...] = ()


def route(text: str, has_text: bool, snapshot: Snapshot, now_ms: int) -> ToProject | HubReply:
    """决定一句话的去向。`has_text` 为 false（图片、语音等）时不看 `text`。"""
    receivers = tuple(p for p in snapshot.projects if not p.push_only and not p.removed)

    if not has_text:
        return _to_default(receivers, "", False, ())

    sentence = text.strip()

    command = strip_prefix(sentence, snapshot.label)
    if command is not None:
        return HubReply(_command(command))

    for project in receivers:
        if project.prefix is None:
            continue
        rest = strip_prefix(sentence, project.prefix)
        if rest is None:
            continue
        if not project.enabled:
            return HubReply(Reply.DISABLED, project=project)
        return ToProject(project, rest, True, "prefix")

    stale: tuple[str, ...] = ()
    if normalize(sentence) in {normalize(word) for word in snapshot.confirm_words}:
        asking, stale = _awaiting(receivers, snapshot.online, now_ms)
        if len(asking) == 1:
            return ToProject(asking[0], sentence, True, "confirm", stale)
        if len(asking) > 1:
            return HubReply(
                Reply.CONFIRM_AMBIGUOUS, awaiting=asking, word=sentence, clear_awaiting=stale
            )

    return _to_default(receivers, sentence, True, stale)


def _command(rest: str) -> Reply:
    word = normalize(rest)
    if word == COMMAND_PROJECTS:
        return Reply.PROJECTS
    if word == COMMAND_STATUS:
        return Reply.STATUS
    return Reply.UNKNOWN_COMMAND


def _awaiting(
    receivers: tuple[Project, ...], online: frozenset[str], now_ms: int
) -> tuple[tuple[Project, ...], tuple[str, ...]]:
    """真正在等确认的项目，和标记已经不管用的项目名。

    管用要同时满足：没到期、项目启用着、此刻在线（当场判断，不靠后台任务）。"""
    asking: list[Project] = []
    stale: list[str] = []
    for project in receivers:
        if project.awaiting_until_ms is None:
            continue
        if project.enabled and project.name in online and project.awaiting_until_ms > now_ms:
            asking.append(project)
        else:
            stale.append(project.name)
    return tuple(asking), tuple(stale)


def _to_default(
    receivers: tuple[Project, ...], text: str, has_text: bool, stale: tuple[str, ...]
) -> ToProject | HubReply:
    default = next((p for p in receivers if p.is_default), None)
    if default is None:
        return HubReply(Reply.NO_DEFAULT, clear_awaiting=stale)
    if not default.enabled:
        return HubReply(Reply.DISABLED, project=default, clear_awaiting=stale)
    return ToProject(default, text, has_text, "default", stale)
