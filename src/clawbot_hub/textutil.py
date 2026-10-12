"""文本小函数：拆条、取标题、NFKC 前缀匹配、脱敏。全是纯函数，不碰数据库和网络。

拆条和标题从 ashare-quant 的 `notify/base.py`、`notify/clawbot.py` 搬来，行为一致；
有意不同的两处（设计方案第 14 节）：开头里的标题最多 30 个字，
以及整条消息不超过上限时一条发完、不扣开头的位置。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

# ---------------------------------------------------------------- 拆条和标题

# 开头里的标题最多取这么多个字，超出的截断并加"…"（固定常量，见设计的"固定常量"表）
TITLE_MAX_CHARS = 30


def headline(text: str) -> tuple[str, str]:
    """第一行当标题，其余（去掉开头的空行）当内容；只有一行时两者都用这一行。"""
    first, _, rest = text.partition("\n")
    title = first.strip()
    rest = rest.lstrip("\n")
    return title, rest if rest.strip() else title


def clip_title(title: str) -> str:
    """开头里用的标题：最多前 `TITLE_MAX_CHARS` 个字，超出的截断并加"…"。"""
    if len(title) <= TITLE_MAX_CHARS:
        return title
    return title[:TITLE_MAX_CHARS] + "…"


def part_head(title: str, i: int, n: int) -> str:
    """第 2 条起每条前面多出来的一行开头（带换行）。"""
    return f"{title}（第 {i}/{n} 条）\n"


def add_label(text: str, label: str) -> str:
    """项目标签：`【标签】` 紧贴着原来的第一行（同一行）。没有标签原样返回；原文不去空白。"""
    return f"【{label}】{text}" if label else text


def body_budget(part_chars: int, head_len: int) -> int:
    """每条正文的字数预算：留出开头的位置，但至少留一半。"""
    return max(part_chars - head_len, part_chars // 2)


def split_message(text: str, limit: int) -> list[str]:
    """拆成每段不超过 `limit` 个字、按顺序拼回等于原文的几段。

    优先在换行处断开（换行符留在前一段末尾）；单独一行就超过上限时，才在行中间断开，
    不会把一个字拆开。
    """
    if limit < 1:
        # 一段放不下一个字就永远拆不完，直接报错，免得死循环
        raise ValueError(f"每段的字数上限至少是 1，收到 {limit}")
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit) + 1  # 找不到换行时为 0
        if cut == 0:
            cut = limit
        parts.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        parts.append(rest)
    return parts


def build_parts(text: str, part_chars: int, window_limit: int) -> list[str]:
    """把（已经加了标签的）全文拆成要发的几条，每条（含开头）不超过 `part_chars` 个字。

    - 全文不超过 `part_chars`：就是一条，原样、没有开头；
    - 否则标题取第一行（最多 30 个字），第 2 条起每条前面加 `<标题>（第 i/n 条）` 一行；
      拆之前给开头留位置，按 n = `window_limit`（最多拆成这么多条）估开头的字数。
    去掉开头后按顺序拼回等于 `text`。
    """
    if len(text) <= part_chars:
        return [text] if text else []
    title = clip_title(headline(text)[0])
    budget = body_budget(part_chars, len(part_head(title, window_limit, window_limit)))
    pieces = split_message(text, budget)
    n = len(pieces)
    return [
        piece if i == 0 else part_head(title, i + 1, n) + piece for i, piece in enumerate(pieces)
    ]


# ---------------------------------------------------------------- 前缀匹配

# 分隔符：空白和这几个（按 NFKC 比较，所以全角的 `：` `，` 与半角的等价）
_SEPARATOR_CHARS = frozenset(":,、")


def normalize(text: str) -> str:
    """比较用的形式：NFKC（全角半角视为相同）、不分大小写、去掉首尾空白。"""
    return unicodedata.normalize("NFKC", text).casefold().strip()


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def is_separator(char: str) -> bool:
    """空白（含全角空格）和 `:` `：` `,` `，` `、`。"""
    if char.isspace():
        return True
    folded = unicodedata.normalize("NFKC", char)
    return folded.isspace() or folded in _SEPARATOR_CHARS


def strip_prefix(text: str, word: str) -> str | None:
    """`text` 以 `word` 开头、后面紧跟分隔符或者就此结束时，返回去掉 `word` 和它后面
    **全部**分隔符以后的文字；否则返回 `None`。

    比较按 NFKC 且不分大小写；返回的文字从原始字符串里按位置切出来（NFKC 不改写它）并去掉首尾空白。
    "词后面直接连着字"（量化因子、服务器状态）不算命中。
    """
    target = normalize(word)
    if not target:
        raise ValueError("要匹配的词不能是空的")
    original = text.strip()
    # 归一化会改变长度（"ﬁ" 变成 "fi"，"e" 加重音符合成 "é"），所以不按字数切，
    # 而是找原文里归一以后正好等于这个词的最短前缀
    for end in range(1, len(original) + 1):
        folded = _fold(original[:end])
        if folded == target:
            break
        if len(folded) > len(target) and not target.startswith(folded):
            return None
    else:
        return None
    if end < len(original) and not is_separator(original[end]):
        return None
    while end < len(original) and is_separator(original[end]):
        end += 1
    return original[end:]


# ---------------------------------------------------------------- 词的格式

# 前缀、服务指令词、确认词最多这么多个字符（固定常量）
WORD_MAX_CHARS = 8


def is_word(text: str) -> bool:
    """前缀、服务指令词（`hub.label`）、确认词的格式：1 到 8 个字符，只含字母、数字、汉字。

    先做 NFKC 规范化再判断（全角和半角一样，字数也按规范化以后的数）。字母包括各国文字的
    字母（汉字、假名都算），数字指十进制数字；空白、标点、符号一概不行。
    """
    folded = unicodedata.normalize("NFKC", text)
    if not 1 <= len(folded) <= WORD_MAX_CHARS:
        return False
    return all(char.isalpha() or char.isdecimal() for char in folded)


# ---------------------------------------------------------------- 脱敏

MASK = "***"
# 项目密钥的格式：chub_ 加 43 个字符
_PROJECT_KEY = re.compile(r"chub_[A-Za-z0-9_-]{43}")


def mask(text: str, secrets: Iterable[str | None] = ()) -> str:
    """把已知的秘密值（登录凭证、对话凭证、Bark 设备码……）和项目密钥的格式换成 `***`。

    长的先换，免得短的把长的切碎；空值忽略。"""
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(secret, MASK)
    return _PROJECT_KEY.sub(MASK, text)
