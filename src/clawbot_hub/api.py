"""HTTP 接口：路由、认证、限速、请求体大小、统一的错误格式。

一个请求按这个先后过关（设计第 10 节），前一关不过，后面的都不做：

1. 守门人（最外层）：局域网监听打开时，来源不是本机或私有网段 → 403，正文为空；服务正在关闭 →
   503 `shutting_down`；后面任何没想到的异常 → 500 `internal`（只记错误类型和堆栈，不记异常信息，
   更不记请求的内容）；
2. 路由：路径不存在 404、方法不对 405（带 `Allow`）。路径末尾多一个斜杠也是不存在，不重定向；
3. 来源地址的认证失败限流：局域网来源 1 分钟内失败 10 次以后，请求在认证之前就被 429 拒绝；
   本机（127.0.0.0/8、`::1`）不计也不受限；
4. 认证：`Authorization: Bearer <密钥>`；没带 / 格式不对 / 认不出来 / 已移除一律同一个 401，
   已停用 403；
5. 项目的请求限速（`POST /v1/messages`、`POST /v1/replies` 合计，每分钟 `send.rate_per_minute`
   次）：认证通过的每个这样的请求都计数，不管后面成功还是被拒绝；超出的 429，不占额度、不再
   计数。取话和确认不限速；
6. 请求体大小：按实际读到的字节算（不信 `Content-Length`），超过 64 KiB 就 413；
7. 内容：JSON 对象、字段校验（`sender` 管），然后才是业务。回复先校验全部字段（400），再认回复
   句柄（404 / 410 / 429），最后才发。

所有非 200 的响应正文都是 `{"error": {"code": …, "message": …}}`，`message` 是中文、不含请求的
内容。框架自带的 422 校验失败一律改成 400 `bad_request`。只开 `/v1/...`，不开 `/docs`、`/redoc`、
`/openapi.json`。

来源地址只看 TCP 连接的对端（`scope["client"]`），不看任何请求头；`app.server_config` 把
uvicorn 的 `proxy_headers` 关掉，免得它按 `X-Forwarded-For` 改写来源。

规格没写、这里定下的小约定（任务单 C4 的实现说明里也有）：

- 取话：只推送的项目先于参数校验回 403 `push_only`；`wait`、`max` 只认 ASCII 数字（`+1`、`1.5`、
  空串、全角数字都是 400），`max=0` 时 `wait` 照样校验；同一个参数写了几次取最后一个；
- 确认：只推送的项目不拦（它没有待取的话，全部算 `unknown`）；`ids` 一次最多 50 个，重复的
  `id` 也各占一个名额；
- 回复：`confirm_seconds` 不管有没有 `awaiting_confirmation` 都要合格；`null`、`1.5`、`true`
  都不是正整数。同一个句柄的回复次数在发送成功以后才记，所以同时到的几个回复可能一起通过次数
  检查（最多多出同时在途的那几个）；`reply_limit` 的 `Retry-After` 是句柄还有几秒失效。

规格：`hub-send`「发消息接口」「回复」「错误格式和 HTTP 状态码」，`hub-inbox`「取话」「确认」
「回复句柄」，`hub-projects`「项目密钥」「请求限速和大小」，`hub-quota`「额度可查」，
`hub-deploy`「谁能连」「健康检查和版本」「正常关闭」。
"""

from __future__ import annotations

import json
import logging
import re
import traceback
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import clawbot_hub
from clawbot_hub import apischema
from clawbot_hub.inbox import HandleExpired, Leased, ReplyLimit, UnknownHandle
from clawbot_hub.lan import is_loopback, is_private_source, source_key
from clawbot_hub.projects import Project, ProjectDisabled, Unauthorized
from clawbot_hub.quota import closed_reason, window_ends_ms
from clawbot_hub.quota import left as quota_left
from clawbot_hub.sender import SendInvalid, SendResult, check_text
from clawbot_hub.services import Services
from clawbot_hub.textutil import is_encodable
from clawbot_hub.timefmt import iso_text

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024  # 请求体上限（固定值）
INBOX_DEFAULT_MAX = 10  # 取话一次最多几条：不写 `max` 时给几条（固定值）
INBOX_MAX_ITEMS = 50  # 取话一次最多几条的上限，超过的截成它（固定值）
ACK_MAX_IDS = 50  # 一次最多确认几个 `id`（固定值）
CONFIRM_SECONDS_MAX = (
    1800  # `confirm_seconds` 的上限（固定值，和配置 `routing.confirm_seconds` 一致）
)

UNAUTHORIZED_TEXT = "没带密钥，或者密钥不对：请求头里写 Authorization: Bearer <项目密钥>"
DISABLED_TEXT = "这个项目已停用：请联系运维启用后再试"
TOO_MANY_FAILURES_TEXT = "认证失败的次数太多，这个地址先停一会儿再试"
RATE_LIMITED_TEXT = "请求太频繁：发消息和回复每个项目每分钟有次数限制，等一会儿再试"
TOO_LARGE_TEXT = "请求太大：请求体最多 64 KiB"
NOT_JSON_TEXT = "请求体不是合法的 JSON"
NOT_OBJECT_TEXT = "请求体要是一个 JSON 对象"
NOT_FINISHED_TEXT = "请求没有发完"
SHUTTING_DOWN_TEXT = "服务正在关闭，稍后再试"
INTERNAL_TEXT = "服务内部出错，这次请求没有处理完"
NOT_FOUND_TEXT = "没有这个接口"
NOT_ALLOWED_TEXT = "这个接口不支持这种请求方法"
MALFORMED_TEXT = "请求的格式或字段类型不对"
PUSH_ONLY_TEXT = "这个项目是只推送的，不能取话"
IDS_TYPE_TEXT = "ids 要是字符串组成的列表"
IDS_TOO_MANY_TEXT = f"一次最多确认 {ACK_MAX_IDS} 个 id"
HANDLE_TYPE_TEXT = "handle 要是字符串"
AWAITING_TYPE_TEXT = "awaiting_confirmation 要是 true 或 false"
CONFIRM_SECONDS_TEXT = f"confirm_seconds 要是 1 到 {CONFIRM_SECONDS_MAX} 的整数"

# 框架自己抛出的 HTTP 错误（路径不存在、方法不对……）换成统一的代码和中文说明
FRAMEWORK_ERRORS = {
    404: ("not_found", NOT_FOUND_TEXT),
    405: ("method_not_allowed", NOT_ALLOWED_TEXT),
}


# ---------------------------------------------------------------- 错误


class ApiError(Exception):
    """要返回给调用方的错误。`message` 是中文，不含请求的内容；`headers` 是 `Retry-After` 之类。"""

    def __init__(
        self, status: int, code: str, message: str, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers


def error_response(
    status: int, code: str, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}}, status_code=status, headers=headers
    )


async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
    return error_response(exc.status, exc.code, exc.message, exc.headers)


async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    default = (
        ("bad_request", MALFORMED_TEXT) if exc.status_code < 500 else ("internal", INTERNAL_TEXT)
    )
    code, message = FRAMEWORK_ERRORS.get(exc.status_code, default)
    return error_response(exc.status_code, code, message, dict(exc.headers or {}))


async def _invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
    # 框架的校验失败信息里带着请求里的值，一个字也不转述
    return error_response(400, "bad_request", MALFORMED_TEXT)


def _log_internal(exc: Exception) -> None:
    """只记错误类型和堆栈（出事的函数和行），不记异常信息：它可能带着请求里的内容。"""
    stack = "".join(traceback.format_tb(exc.__traceback__))
    logger.error("接口内部出错：%s\n%s", type(exc).__name__, stack)


# ---------------------------------------------------------------- 守门人（最外层）


def client_host(scope: Scope) -> str | None:
    client = scope.get("client")
    return client[0] if client else None


class Guard:
    """最外层的 ASGI 中间件，见模块说明的第 1 关。"""

    def __init__(self, app: ASGIApp, services: Services) -> None:
        self.app = app
        self._services = services

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if self._services.config.server.lan and not is_private_source(client_host(scope)):
            await self._empty(send, 403)
            return
        if self._services.shutting_down.is_set():
            response = error_response(503, "shutting_down", SHUTTING_DOWN_TEXT)
            await response(scope, receive, send)
            return
        started = False

        async def watched(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, watched)
        except Exception as exc:  # noqa: BLE001 - 兜底：任何没想到的错都变成 500
            _log_internal(exc)
            if started:
                return  # 响应已经开始，改不了了；连接会在响应没写完时被关掉
            response = error_response(500, "internal", INTERNAL_TEXT)
            await response(scope, receive, send)

    @staticmethod
    async def _empty(send: Send, status: int) -> None:
        """正文为空的响应（局域网规则的 403：不透露任何东西，也不记请求的内容）。"""
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-length", b"0")],
            }
        )
        await send({"type": "http.response.body", "body": b""})


# ---------------------------------------------------------------- 依赖：认证、限速、读请求体


def _services(request: Request) -> Services:
    return request.app.state.services


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if header is None:
        return None
    scheme, _, token = header.partition(" ")
    return token if scheme.lower() == "bearer" else None


def authenticated(request: Request) -> Project:
    """认出是哪个项目。局域网来源先查失败限流（在认证之前），认证失败记一笔；本机不计也不受限。"""
    services = _services(request)
    host = client_host(request.scope)
    counted = not is_loopback(host)
    source = source_key(host)
    if counted:
        wait = services.limits.failures.retry_after(source)
        if wait is not None:
            raise ApiError(
                429, "too_many_failures", TOO_MANY_FAILURES_TEXT, {"Retry-After": str(wait)}
            )
    try:
        return services.projects.authenticate(_bearer(request))
    except Unauthorized:
        if counted:
            services.limits.failures.add(source)
        raise ApiError(401, "unauthorized", UNAUTHORIZED_TEXT) from None
    except ProjectDisabled:
        raise ApiError(403, "project_disabled", DISABLED_TEXT) from None


Authed = Annotated[Project, Depends(authenticated)]


def rate_limited(request: Request, project: Authed) -> Project:
    """发消息和回复的请求限速：认证通过的每个请求都计数；满了 429，这一次不计。"""
    wait = _services(request).limits.requests.hit(project.name)
    if wait is not None:
        raise ApiError(429, "rate_limited", RATE_LIMITED_TEXT, {"Retry-After": str(wait)})
    return project


async def limited_body(request: Request) -> bytes:
    """读请求体，按实际读到的字节数算，超过上限立刻停下来 413（后面的不再读）。"""
    chunks: list[bytes] = []
    total = 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                raise ApiError(413, "too_large", TOO_LARGE_TEXT)
            chunks.append(chunk)
    except ClientDisconnect:
        raise ApiError(400, "bad_request", NOT_FINISHED_TEXT) from None
    return b"".join(chunks)


Limited = Annotated[Project, Depends(rate_limited)]
Body = Annotated[bytes, Depends(limited_body)]


def json_object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ApiError(400, "bad_request", NOT_JSON_TEXT) from None
    if not isinstance(value, dict):
        raise ApiError(400, "bad_request", NOT_OBJECT_TEXT)
    return value


# ---------------------------------------------------------------- 路由

router = APIRouter()

MESSAGES_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "required": ["text"],
                "properties": {
                    "text": {"type": "string", "description": "纯文本全文，第一行是标题"},
                    "priority": {"enum": ["urgent", "normal", "deferrable"], "default": "normal"},
                    "dedupe_key": {"type": "string", "minLength": 1, "maxLength": 128},
                    "on_closed": {"enum": ["fail", "skip"], "default": "fail"},
                },
            }
        }
    },
}


INBOX_PARAMETERS = [
    {
        "name": "wait",
        "in": "query",
        "required": False,
        "description": "没有话时最多等几秒（0 不等；超过 inbox.max_wait 的截成它）。"
        "不写取 inbox.wait_seconds",
        "schema": {"type": "integer", "minimum": 0},
    },
    {
        "name": "max",
        "in": "query",
        "required": False,
        "description": f"最多返回几条（超过 {INBOX_MAX_ITEMS} 的截成它）；0 只是心跳，不租出话",
        "schema": {"type": "integer", "minimum": 0, "default": INBOX_DEFAULT_MAX},
    },
]

ACK_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "required": ["ids"],
                "properties": {
                    "ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": ACK_MAX_IDS,
                        "description": "处理完的话的编号（取话返回的 id）",
                    }
                },
            }
        }
    },
}

REPLIES_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "required": ["handle", "text"],
                "properties": {
                    "handle": {"type": "string", "description": "取话时给的回复句柄"},
                    "text": {"type": "string", "description": "纯文本全文，第一行是标题"},
                    "awaiting_confirmation": {"type": "boolean", "default": False},
                    "confirm_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": CONFIRM_SECONDS_MAX,
                        "description": "等确认留多久，默认 routing.confirm_seconds",
                    },
                },
            }
        }
    },
}


def _send_result(result: SendResult, tz) -> dict[str, Any]:
    ends = result.quota.window_ends_ms
    return {
        "status": result.status,
        "reason_code": result.reason_code,
        "reason": result.reason,
        "parts": result.parts,
        "parts_sent": result.parts_sent,
        "duplicate": result.duplicate,
        "id": result.id,
        "quota": {
            "left": result.quota.left,
            "limit": result.quota.limit,
            "window_ends_at": None if ends is None else iso_text(ends, tz),
        },
    }


@router.post(
    "/v1/messages",
    summary="发一条消息",
    operation_id="send_message",
    openapi_extra={"requestBody": MESSAGES_BODY, **apischema.SECURED},
    responses=apischema.MESSAGES_RESPONSES,
)
def post_message(request: Request, project: Limited, body: Body) -> JSONResponse:
    """发一条消息，拿到最终结果才返回（同步）。"""
    services = _services(request)
    fields = json_object(body)
    try:
        result = services.sender.push(
            project,
            fields.get("text"),
            priority=fields.get("priority", "normal"),
            dedupe_key=fields.get("dedupe_key"),
            on_closed=fields.get("on_closed", "fail"),
        )
    except SendInvalid as exc:
        raise ApiError(400, exc.code, exc.message) from None
    except ProjectDisabled:
        raise ApiError(403, "project_disabled", DISABLED_TEXT) from None
    return JSONResponse(_send_result(result, services.config.display.tzinfo))


_WHOLE_NUMBER = re.compile(r"[0-9]+")


def _count_param(request: Request, name: str, default: int, cap: int) -> int:
    """取话的 `wait`、`max`：没写取 `default`；写了要是不小于 0 的整数，超过 `cap` 的截成 `cap`
    （不是错误）。只认 ASCII 数字，`-1`、`+1`、`1.5`、空串、全角数字都是 400。"""
    raw = request.query_params.get(name)
    if raw is None:
        return default
    if not _WHOLE_NUMBER.fullmatch(raw):
        raise ApiError(400, "bad_request", f"{name} 要是不小于 0 的整数")
    # 13 位数已经比任何上限都大，只取前 13 位：几千位的数字串转成整数会出错
    digits = raw.lstrip("0")[:13]
    return min(int(digits or "0"), cap)


def _inbox_item(item: Leased, tz) -> dict[str, Any]:
    return {
        "id": item.id,
        "text": item.text,
        "has_text": item.has_text,
        "at": iso_text(item.at_ms, tz),
        "at_ms": item.at_ms,
        "received_at": iso_text(item.received_ms, tz),
        "attempt": item.attempt,
        "via": item.via,
        "reply_handle": item.reply_handle,
    }


@router.get(
    "/v1/inbox",
    summary="取话",
    operation_id="fetch_inbox",
    openapi_extra={"parameters": INBOX_PARAMETERS, **apischema.SECURED},
    responses=apischema.INBOX_RESPONSES,
)
def get_inbox(request: Request, project: Authed) -> JSONResponse:
    """取话：分给调用方这个项目的话，没有就等（最多 `inbox.max_wait` 秒）。同步函数，框架把它
    放进线程池，所以等的时候不占事件循环。"""
    services = _services(request)
    if project.push_only:
        raise ApiError(403, "push_only", PUSH_ONLY_TEXT)
    cfg = services.config.inbox
    wait = _count_param(request, "wait", cfg.wait_seconds, cfg.max_wait)
    count = _count_param(request, "max", INBOX_DEFAULT_MAX, INBOX_MAX_ITEMS)
    try:
        items = services.inbox.fetch(project, wait=wait, max_items=count)
    except ProjectDisabled:
        raise ApiError(403, "project_disabled", DISABLED_TEXT) from None
    tz = services.config.display.tzinfo
    return JSONResponse(
        {"items": [_inbox_item(item, tz) for item in items], "lease_seconds": cfg.lease_seconds}
    )


@router.post(
    "/v1/inbox/ack",
    summary="确认处理完了",
    operation_id="ack_inbox",
    openapi_extra={"requestBody": ACK_BODY, **apischema.SECURED},
    responses=apischema.ACK_RESPONSES,
)
def post_ack(request: Request, project: Authed, body: Body) -> JSONResponse:
    """确认处理完了：不再给，立即清掉正文。重复、别人的、不存在的都不报错，列在 `unknown`。"""
    fields = json_object(body)
    ids = fields.get("ids")
    if not isinstance(ids, list):
        raise ApiError(400, "bad_request", IDS_TYPE_TEXT)
    if len(ids) > ACK_MAX_IDS:
        raise ApiError(400, "bad_request", IDS_TOO_MANY_TEXT)
    if not all(isinstance(item, str) and is_encodable(item) for item in ids):
        raise ApiError(400, "bad_request", IDS_TYPE_TEXT)
    result = _services(request).inbox.ack(project, ids)
    return JSONResponse({"acked": result.acked, "unknown": result.unknown})


@router.post(
    "/v1/replies",
    summary="回复使用者的一句话",
    operation_id="reply",
    openapi_extra={"requestBody": REPLIES_BODY, **apischema.SECURED},
    responses=apischema.REPLIES_RESPONSES,
)
def post_reply(request: Request, project: Limited, body: Body) -> JSONResponse:
    """回复使用者的一句话，拿到最终结果才返回（同步）。用取话时给的回复句柄找到那句话的对话凭证。"""
    services = _services(request)
    fields = json_object(body)
    handle = fields.get("handle")
    if not isinstance(handle, str) or not is_encodable(handle):
        raise ApiError(400, "bad_request", HANDLE_TYPE_TEXT)
    try:
        text = check_text(fields.get("text"), services.config.send.max_chars)
    except SendInvalid as exc:
        raise ApiError(400, exc.code, exc.message) from None
    awaiting = fields.get("awaiting_confirmation", False)
    if not isinstance(awaiting, bool):
        raise ApiError(400, "bad_request", AWAITING_TYPE_TEXT)
    confirm_seconds = fields.get("confirm_seconds", services.config.routing.confirm_seconds)
    if (
        not isinstance(confirm_seconds, int)
        or isinstance(confirm_seconds, bool)
        or not 1 <= confirm_seconds <= CONFIRM_SECONDS_MAX
    ):
        raise ApiError(400, "bad_request", CONFIRM_SECONDS_TEXT)

    try:
        target = services.inbox.resolve_reply(project, handle)
    except UnknownHandle as exc:
        raise ApiError(404, "unknown_handle", str(exc)) from None
    except HandleExpired as exc:
        raise ApiError(410, "handle_expired", str(exc)) from None
    except ReplyLimit as exc:
        headers = {"Retry-After": str(exc.retry_after)}
        raise ApiError(429, "reply_limit", str(exc), headers) from None

    result = services.sender.reply(project, text, context_token=target.context_token)
    if result.parts_sent >= 1:  # 至少发出了一条（含拿不准到没到的）：占一次回复次数，记等确认
        services.inbox.count_reply(target.item_id)
        if awaiting:
            until_ms = services.clock.now_ms() + confirm_seconds * 1000
            services.inbox.mark_awaiting(project, target.item_id, until_ms)
    return JSONResponse(_send_result(result, services.config.display.tzinfo))


def project_status(services: Services, project: Project) -> dict[str, Any]:
    """`GET /v1/status` 的内容：全局的登录和窗口，加上调用方自己的额度，没有别的项目的任何信息。
    窗口没开（从没收到过消息、过期、作废）时用量一律按 0 说，`left` 是 0。"""
    config = services.config
    cfg = config.quota
    tz = config.display.tzinfo
    now = services.clock.now_ms()
    state = services.quota.read_state(services.db.conn())
    login = services.monitor.login().state
    window = state.window
    is_open = window is not None and closed_reason(window, now, cfg) is None
    ends = window_ends_ms(window, cfg) if is_open else None
    share = state.shares.get(project.name)
    own_used = share.used if is_open and share is not None else 0
    used = state.used if is_open else 0
    replies = 0
    if is_open:
        row = (
            services.db.conn()
            .execute(
                "SELECT n FROM usage WHERE window_id = ? AND project_id = ? AND kind = 'reply'",
                (window.id, project.id),
            )
            .fetchone()
        )
        replies = 0 if row is None else row["n"]
    return {
        "service": {
            "version": clawbot_hub.__version__,
            "api": clawbot_hub.API_VERSION,
            "send_timeout_seconds": config.send.timeout_seconds,
            "inbox_online_seconds": config.inbox.online_seconds,
        },
        "login": {"state": login},
        "window": {
            "open": is_open,
            "started_at": None if window is None else iso_text(window.started_ms, tz),
            "ends_at": None if ends is None else iso_text(ends, tz),
            "seconds_left": (ends - now) // 1000 if ends is not None else 0,
        },
        "quota": {
            "limit": cfg.window_limit,
            "used": used,
            "free": max(0, cfg.window_limit - used) if is_open else 0,
            "project": {
                "guaranteed": project.guaranteed,
                "cap": project.cap,
                "used": own_used,
                "left": quota_left(state, project.name, cfg, now, login_ok=login == "ok"),
                "replies_used": replies,
            },
        },
    }


@router.get(
    "/v1/status",
    summary="额度和状态",
    operation_id="get_status",
    openapi_extra=apischema.SECURED,
    responses=apischema.STATUS_RESPONSES,
)
def get_status(request: Request, project: Authed) -> JSONResponse:
    """调用方自己的额度和全局的登录、窗口状态。"""
    return JSONResponse(project_status(_services(request), project))


@router.get(
    "/v1/health",
    summary="探活",
    operation_id="get_health",
    responses=apischema.HEALTH_RESPONSES,
)
def get_health() -> dict[str, Any]:
    """探活：不要密钥，只说服务活着和版本，不含登录、窗口、项目的任何信息。"""
    return {"ok": True, "version": clawbot_hub.__version__, "api": clawbot_hub.API_VERSION}


# ---------------------------------------------------------------- 装应用


def _openapi(app: FastAPI) -> dict[str, Any]:
    """导出接口描述：框架生成的，加上"密钥怎么带"（框架不知道我们自己读 `Authorization`）。"""
    if app.openapi_schema is None:
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        components = schema.setdefault("components", {})
        components["securitySchemes"] = apischema.SECURITY_SCHEMES
        components["schemas"] = apischema.SCHEMAS
        app.openapi_schema = schema
    return app.openapi_schema


def build_app(services: Services) -> FastAPI:
    """把接口装成一个应用。`/docs`、`/redoc`、`/openapi.json` 不开（导出接口描述用
    `app.openapi()`）；路径末尾多一个斜杠不重定向。"""
    app = FastAPI(
        title="clawbot-hub",
        version=clawbot_hub.__version__,
        description=apischema.DESCRIPTION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )
    app.openapi = lambda: _openapi(app)  # type: ignore[method-assign]
    app.state.services = services
    app.include_router(router)
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _invalid_request)
    app.add_middleware(Guard, services=services)
    return app
