"""`hubctl send` 的内核：用项目密钥调一次 `POST /v1/messages`，把结果变成输出和退出码。

给没有 Python 客户端的脚本用，所以退出码是它的接口：脚本里写 `hubctl send "…" || 走 Bark` 就够了，
只有真的没发出去或拿不准的时候才会走 Bark。**这组退出码只用于 `send`**（别的命令各有各的）：

- 0：`sent`（含去重命中的已发）和 `skipped`（用户要的"窗口关了就安静跳过"，不是失败）；
- 3 `unconfirmed`；4 `partial`；5 `rejected`；6 `failed`；
- 10：请求没有发出去（连不上服务、连接超时），一定没有发出任何东西；
- 11：密钥不对 / 项目已停用（401、403）；
- 12：其他接口错误（400 系列、429、503 `shutting_down`……），服务明确说了没发；
- 13：请求发出之后没有拿到结果（读超时、连接被重置、服务返回 500）——消息**可能已经发出**；
- 2：用法错误（没有密钥、密钥文件权限太宽、选项不对……），7 空着不用。

安全上的几处做法：密钥只从环境变量或文件读，不收命令行参数（会被 `ps` 看到）；文件权限比 0600 宽
（组或其他人能读）就拒绝；请求不走环境变量里的代理、不读 `~/.netrc`（它会用别的凭证盖掉
`Authorization`）、不跟随重定向（密钥不会被带到别处）；输出、报错里不出现密钥，地址里的用户名口令
也不打印。拿不准请求有没有发出去的故障一律当"可能已经发出"，宁可让脚本多走一次 Bark，也不要漏。

规格没写、这里定下的小约定：连接超时固定 10 秒（`--timeout` 管读超时，更短时连接也不超过它）；
从标准输入读文字时去掉末尾的换行（一个脚本 `echo` 出来的文字不该多带一行空行），别的原样；
`--json` 时标准输出是服务返回的原文（任何状态码都是），人读的说明写在标准错误里。

规格：`hub-cli`「从脚本发消息」。
"""

from __future__ import annotations

import socket
import stat
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError

KEY_ENV = "CLAWBOT_HUB_KEY"  # 默认从这个环境变量读密钥
DEFAULT_TIMEOUT_SECONDS = 35.0  # 读超时：比服务端发送总时限（30 秒）多 5 秒
CONNECT_TIMEOUT_SECONDS = 10.0

EXIT_UNCONFIRMED = 3
EXIT_PARTIAL = 4
EXIT_REJECTED = 5
EXIT_FAILED = 6
EXIT_NOT_SENT = 10
EXIT_KEY_REFUSED = 11
EXIT_API_ERROR = 12
EXIT_NO_RESULT = 13
EXIT_USAGE = 2

# 服务返回的 `status` → 退出码
STATUS_EXIT = {
    "sent": 0,
    "skipped": 0,
    "unconfirmed": EXIT_UNCONFIRMED,
    "partial": EXIT_PARTIAL,
    "rejected": EXIT_REJECTED,
    "failed": EXIT_FAILED,
}

MAY_HAVE_GONE_OUT = "消息可能已经发出，请不要重发（脚本里走 Bark 的话请写明“可能已经到了”）"


class UsageProblem(Exception):
    """用法错误：没有密钥、密钥文件权限太宽、地址不对……（退出码 2）。信息里没有密钥。"""


@dataclass(frozen=True)
class SendOutcome:
    exit_code: int
    out: str = ""  # 写到标准输出
    err: str = ""  # 写到标准错误


# ---------------------------------------------------------------- 用法检查


def read_key(*, key_env: str | None, key_file: Path | None, environ: Mapping[str, str]) -> str:
    """密钥从环境变量（默认 `CLAWBOT_HUB_KEY`）或文件读；两个都指定是用法错误。"""
    if key_env is not None and key_file is not None:
        raise UsageProblem("--key-env 和 --key-file 只能用一个")
    if key_file is not None:
        return _key_from_file(key_file)
    name = key_env or KEY_ENV
    value = (environ.get(name) or "").strip()
    if not value:
        raise UsageProblem(
            f"没有密钥：环境变量 {name} 没有设置（或者是空的）。把项目密钥放进这个环境变量，"
            "或者用 --key-file 指向一个权限 0600 的文件（命令行参数里不收密钥，会被 ps 看到）"
        )
    return value


def _key_from_file(path: Path) -> str:
    try:
        info = path.stat()
    except OSError as exc:
        raise UsageProblem(f"密钥文件读不了：{path}（{exc.strerror}）") from None
    if not stat.S_ISREG(info.st_mode):
        raise UsageProblem(f"密钥文件不是普通文件：{path}")
    if info.st_mode & 0o077:  # 组或其他人能读写它
        raise UsageProblem(
            f"密钥文件的权限太宽（{stat.S_IMODE(info.st_mode):04o}）：{path}。"
            f"请改成 0600：chmod 600 {path}"
        )
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise UsageProblem(f"密钥文件读不了：{path}（{type(exc).__name__}）") from None
    if not value:
        raise UsageProblem(f"密钥文件是空的：{path}")
    return value


def check_address(url: str) -> str:
    """地址要是 `http://` 或 `https://` 开头、有主机名；去掉末尾的斜杠。"""
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - 端口写得不对会在这里抛 ValueError
    except ValueError:
        raise UsageProblem("--url 的写法不对：应该是 http://主机:端口") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UsageProblem("--url 的写法不对：应该是 http://主机:端口")
    return url.rstrip("/")


def shown_address(url: str) -> str:
    """给人看的地址：只留 `协议://主机:端口`，用户名口令和路径都不写。"""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    return f"{parts.scheme}://{host}" + (f":{port}" if port else "")


def read_text(argument: str | None, *, stdin: Any, interactive: bool) -> str:
    """要发的文字：参数给了（不是 `-`）就用参数，原样；是 `-`，或者没给而标准输入不是终端，
    从标准输入读（去掉末尾的换行）；没给又是终端就是用法错误，不等你敲字。"""
    if argument is not None and argument != "-":
        return argument
    if argument is None and interactive:
        raise UsageProblem(
            "没有要发的文字：把文字写在命令后面，或者用 - 从标准输入读（echo 文字 | hubctl send -）"
        )
    return stdin.read().rstrip("\r\n")


# ---------------------------------------------------------------- 一次发送


def _chain(error: BaseException) -> Iterator[BaseException]:
    """沿着原因链把相关的异常都走一遍（`requests` 把 urllib3 的异常塞在参数里）。"""
    seen: set[int] = set()
    pending: list[BaseException] = [error]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        candidates = [
            current.__cause__,
            current.__context__,
            getattr(current, "reason", None),
            *current.args,
        ]
        pending.extend(item for item in candidates if isinstance(item, BaseException))


def never_sent(error: BaseException) -> bool:
    """这个网络故障是不是说明请求**一定没有发出去**：连接没建立起来（拒绝连接、没有路由、域名
    解析不了、连接超时）、握手没完成、地址写得根本发不出去。拿不准的一律算"可能已经发出"。"""
    if isinstance(
        error,
        (
            requests.exceptions.ConnectTimeout,
            requests.exceptions.SSLError,
            requests.exceptions.InvalidURL,
            requests.exceptions.InvalidSchema,
            requests.exceptions.MissingSchema,
        ),
    ):
        return True
    return any(
        isinstance(item, (NewConnectionError, ConnectTimeoutError, socket.gaierror))
        for item in _chain(error)
    )


def _error_parts(response: requests.Response) -> tuple[str | None, str | None]:
    """服务的错误格式 `{"error": {"code": …, "message": …}}` 里的两项；读不出来是 `None`。"""
    try:
        body = response.json()
        error = body["error"]
        code, message = error.get("code"), error.get("message")
    except (ValueError, KeyError, TypeError, AttributeError):
        return None, None
    return (code if isinstance(code, str) else None), (
        message if isinstance(message, str) else None
    )


def _refusal(prefix: str, response: requests.Response) -> str:
    code, message = _error_parts(response)
    label = f"HTTP {response.status_code}" + (f" {code}" if code else "")
    return f"{prefix}（{label}）" + (f"：{message}" if message else "")


def _seconds(timeout: float) -> str:
    return f"{timeout:g}"


def send_message(
    address: str, key: str, payload: Mapping[str, Any], *, timeout: float, as_json: bool
) -> SendOutcome:
    """`POST <address>/v1/messages`。`address` 已经过 `check_address`。"""
    with requests.Session() as session:
        session.trust_env = False  # 不读代理环境变量，也不读 ~/.netrc
        try:
            response = session.post(
                f"{address}/v1/messages",
                json=dict(payload),
                headers={"Authorization": f"Bearer {key}"},
                timeout=(min(timeout, CONNECT_TIMEOUT_SECONDS), timeout),
                allow_redirects=False,
            )
        except Exception as exc:  # noqa: BLE001 - 不管是什么故障，都要变成一个确定的退出码
            if never_sent(exc):
                return SendOutcome(
                    EXIT_NOT_SENT,
                    err=f"连不上共用服务（{shown_address(address)}）：没有发出任何东西。"
                    "服务是不是没开？",
                )
            if isinstance(exc, requests.exceptions.ReadTimeout):
                cause = f"等了 {_seconds(timeout)} 秒，服务还没有回话"
            else:
                cause = f"请求发出以后连接出了问题（{type(exc).__name__}）"
            return SendOutcome(EXIT_NO_RESULT, err=f"{cause}。{MAY_HAVE_GONE_OUT}")
    return _outcome(response, timeout=timeout, as_json=as_json)


def _outcome(response: requests.Response, *, timeout: float, as_json: bool) -> SendOutcome:
    status = response.status_code
    shown = response.text.rstrip("\n") if as_json else ""
    if 200 <= status < 300:
        return _from_result(response, shown, as_json=as_json)
    if status in (401, 403):
        message = _refusal("服务不认这把密钥", response)
        if _error_parts(response)[1] is None:
            message += "：密钥不对，或者这个项目已经停用"
        return SendOutcome(EXIT_KEY_REFUSED, shown, message)
    if status >= 500 and status != 503:
        message = _refusal("服务出了内部错误", response)
        return SendOutcome(EXIT_NO_RESULT, shown, f"{message}。{MAY_HAVE_GONE_OUT}")
    if 300 <= status < 400:
        message = f"服务回了重定向（HTTP {status}），命令不会跟过去（密钥不能被带到别处）"
        return SendOutcome(EXIT_API_ERROR, shown, message)
    message = _refusal("服务拒绝了请求", response)
    if status == 429:
        wait = response.headers.get("Retry-After", "").strip()
        message += f"；{wait} 秒后再试" if wait.isdigit() else "；稍后再试"
    return SendOutcome(EXIT_API_ERROR, shown, message)


def _from_result(response: requests.Response, shown: str, *, as_json: bool) -> SendOutcome:
    try:
        body = response.json()
        result = body["status"]
    except (ValueError, KeyError, TypeError):
        result = None
        body = None
    if not isinstance(result, str):
        return SendOutcome(
            EXIT_NO_RESULT,
            shown,
            f"服务回了 HTTP {response.status_code}，但内容读不懂。{MAY_HAVE_GONE_OUT}",
        )
    code = STATUS_EXIT.get(result)
    if code is None:
        return SendOutcome(
            EXIT_API_ERROR, shown, f"服务返回了不认识的结果状态：{result}（命令比服务旧了吗？）"
        )
    reason = body.get("reason")
    line = f"{result}：{reason}" if isinstance(reason, str) and reason else result
    return SendOutcome(code, shown if as_json else line)
