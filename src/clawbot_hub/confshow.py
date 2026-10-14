"""`hubctl config show` 的内容：生效的全部配置项（含默认值），一行一项，`点号键: 值`。

值的写法和 `config.yaml` 里一样（`true` / `false`、`[a, b]`），可以直接抄回去改。设备码不在配置
文件里（在环境变量或 `bark.json`），所以单独一行只说"有没有配、从哪来"，永远不写设备码本身。
服务启动之后改过配置文件时末尾再加一句提示。规格 `hub-cli`「命令一览」「配置显示」。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from clawbot_hub.bark import BarkLookup, key_summary
from clawbot_hub.config import Config
from clawbot_hub.status import CONFIG_CHANGED


def _value_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (tuple, list)):
        return "[" + ", ".join(_value_text(item) for item in value) + "]"
    return str(value)


def config_lines(
    config: Config, *, file: Path, exists: bool, key: BarkLookup, changed_since_start: bool
) -> Iterable[str]:
    """`file` 是读的那份配置文件（可能不存在，不存在时全部用内置默认值）。"""
    yield f"配置文件：{file}" if exists else f"配置文件：{file}（没有这个文件，全部用内置默认值）"
    last_alerts = max((name for name in config.flat() if name.startswith("alerts.")), default=None)
    for name, value in config.flat().items():
        yield f"{name}: {_value_text(value)}"
        if name == last_alerts:
            yield key_summary(key)
    if changed_since_start:
        yield CONFIG_CHANGED
