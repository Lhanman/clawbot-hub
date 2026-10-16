"""导出接口描述：`python -m clawbot_hub.apidoc` 把服务的 OpenAPI 描述写进 `docs/api/openapi.json`。

服务本身不开 `/docs`、`/redoc`、`/openapi.json`（那些页面不要密钥，与"服务不开任何管理用的网页"
冲突），描述从应用对象的 `openapi()` 导出。接口描述只和路由有关，造应用时用不到服务对象，所以
导出不需要数据库、凭证和配置。

- `python -m clawbot_hub.apidoc`：写 `docs/api/openapi.json`（`--out <路径>` 换个地方，
  `-` 是标准输出）；
- `python -m clawbot_hub.apidoc --check`：不写，文件和服务实际的描述不一致时退出码 1。

测试 `tests/contract/test_docs_match.py` 检查两者一致：改了接口没重新导出，测试就失败。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

from clawbot_hub.api import build_app
from clawbot_hub.services import Services

# 仓库根目录下的导出位置（`src/clawbot_hub/apidoc.py` 往上两层是仓库根）
DEFAULT_PATH = Path(__file__).resolve().parents[2] / "docs" / "api" / "openapi.json"


def build_openapi() -> dict[str, Any]:
    """服务现在的接口描述。"""
    return build_app(cast(Services, None)).openapi()


def render(description: dict[str, Any] | None = None) -> str:
    """接口描述的文本形式：键排序、缩进 2 格、中文不转义、末尾一个换行（文件里就是这个样子）。"""
    document = build_openapi() if description is None else description
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m clawbot_hub.apidoc", description=__doc__)
    parser.add_argument("--out", default=str(DEFAULT_PATH), help="写到哪里，`-` 是标准输出")
    parser.add_argument("--check", action="store_true", help="只比较，不一致时退出码 1")
    args = parser.parse_args(argv)

    text = render()
    if args.out == "-":
        sys.stdout.write(text)
        return 0
    path = Path(args.out)
    if args.check:
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == text:
            return 0
        print(f"{path} 和服务现在的接口描述不一致：运行 python -m clawbot_hub.apidoc 重新导出")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"已写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
