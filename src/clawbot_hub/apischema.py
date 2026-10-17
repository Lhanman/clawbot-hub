"""接口描述（OpenAPI）里的返回结构和错误说明（任务 D5）。

`api.py` 的每个路由用这里的 `responses_of(...)` 声明"成功时返回什么、可能出哪些错"，导出的
`docs/api/openapi.json` 因此是一份完整的接口描述（请求体和参数在 `api.py` 里）。这里只有描述用的
数据，不参与任何请求的处理；改了接口的返回就要同步改这里，再运行 `python -m clawbot_hub.apidoc`
重新导出（有测试检查导出的文件和服务实际的描述一致）。

返回里每个字段的含义见 `docs/api/v1.md`。
"""

from __future__ import annotations

from typing import Any

_NULLABLE_TEXT = {"type": ["string", "null"]}
_TIME = {"type": "string", "format": "date-time"}
_NULLABLE_TIME = {"type": ["string", "null"], "format": "date-time"}

# ---------------------------------------------------------------- 成功时的返回

SEND_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "status",
        "reason_code",
        "reason",
        "parts",
        "parts_sent",
        "duplicate",
        "id",
        "quota",
    ],
    "properties": {
        "status": {
            "enum": ["sent", "unconfirmed", "partial", "rejected", "failed", "skipped"],
            "description": "最终结果。sent 和 skipped 算微信这边没问题；其余都应当走自己的兜底",
        },
        "reason_code": {**_NULLABLE_TEXT, "description": "给程序看的稳定代码；sent 时是 null"},
        "reason": {**_NULLABLE_TEXT, "description": "给人看的中文原因，可以直接写进 Bark 的末尾"},
        "parts": {"type": "integer", "description": "这条话拆成了几条"},
        "parts_sent": {"type": "integer", "description": "其中发出去的（含拿不准到没到的）条数"},
        "duplicate": {"type": "boolean", "description": "是不是 dedupe_key 命中的重放"},
        "id": {"type": "string", "description": "服务里这条发送记录的编号，排错用"},
        "quota": {
            "type": "object",
            "required": ["left", "limit", "window_ends_at"],
            "properties": {
                "left": {"type": "integer", "description": "一条 normal 的推送现在最多能拆成几条"},
                "limit": {"type": "integer", "description": "一个窗口总共能发几条"},
                "window_ends_at": {**_NULLABLE_TIME, "description": "窗口没开时是 null"},
            },
        },
    },
}

INBOX_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "id",
        "text",
        "has_text",
        "at",
        "at_ms",
        "received_at",
        "attempt",
        "via",
        "reply_handle",
    ],
    "properties": {
        "id": {"type": "string", "description": "确认时用的编号"},
        "text": {
            "type": "string",
            "description": "使用者的话（去掉了前缀）；图片之类没有文字时是空串",
        },
        "has_text": {"type": "boolean"},
        "at": {**_TIME, "description": "使用者发这句话的时间，和 at_ms 是同一个时刻"},
        "at_ms": {"type": "integer", "description": "同一个时刻的毫秒时间戳"},
        "received_at": {**_TIME, "description": "服务收到的时间"},
        "attempt": {"type": "integer", "minimum": 1, "description": "第几次交给项目"},
        "via": {
            "enum": ["default", "prefix", "confirm"],
            "description": "怎么路由到本项目的：默认项目、带前缀、可能是对你上一条等确认回复的回答",
        },
        "reply_handle": {"type": "string", "description": "回复这句话用，30 分钟内有效"},
    },
}

INBOX_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["items", "lease_seconds"],
    "properties": {
        "items": {"type": "array", "items": {"$ref": "#/components/schemas/InboxItem"}},
        "lease_seconds": {"type": "integer", "description": "租期：这么多秒内没确认会再给一次"},
    },
}

ACK_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["acked", "unknown"],
    "properties": {
        "acked": {"type": "integer", "description": "这次确认了几条"},
        "unknown": {
            "type": "array",
            "items": {"type": "string"},
            "description": "没算数的编号：已经确认过的、过期的、作废的、别人的、不存在的，不分哪种",
        },
    },
}

STATUS_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["service", "login", "window", "quota"],
    "properties": {
        "service": {
            "type": "object",
            "required": ["version", "api", "send_timeout_seconds", "inbox_online_seconds"],
            "properties": {
                "version": {"type": "string"},
                "api": {"type": "string"},
                "send_timeout_seconds": {
                    "type": "number",
                    "description": "服务处理一次发送的总时限",
                },
                "inbox_online_seconds": {"type": "number", "description": "多久没取话算不在线"},
            },
        },
        "login": {
            "type": "object",
            "required": ["state"],
            "properties": {"state": {"enum": ["ok", "expired", "missing", "broken"]}},
        },
        "window": {
            "type": "object",
            "required": ["open", "started_at", "ends_at", "seconds_left"],
            "properties": {
                "open": {"type": "boolean"},
                "started_at": _NULLABLE_TIME,
                "ends_at": _NULLABLE_TIME,
                "seconds_left": {"type": "integer"},
            },
        },
        "quota": {
            "type": "object",
            "required": ["limit", "used", "free", "project"],
            "properties": {
                "limit": {"type": "integer"},
                "used": {"type": "integer"},
                "free": {"type": "integer"},
                "project": {
                    "type": "object",
                    "required": ["guaranteed", "cap", "used", "left", "replies_used"],
                    "properties": {
                        "guaranteed": {"type": "integer"},
                        "cap": {"type": "integer"},
                        "used": {"type": "integer"},
                        "left": {"type": "integer"},
                        "replies_used": {"type": "integer"},
                    },
                },
            },
        },
    },
}

HEALTH_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["ok", "version", "api"],
    "properties": {
        "ok": {"type": "boolean"},
        "version": {"type": "string"},
        "api": {"type": "string"},
    },
}

ERROR_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["error"],
    "properties": {
        "error": {
            "type": "object",
            "required": ["code", "message"],
            "properties": {
                "code": {"type": "string", "description": "英文代码，给程序看"},
                "message": {"type": "string", "description": "中文说明，不含请求的内容和凭证"},
            },
        }
    },
}

# 导出的描述里 `components.schemas` 就是这一张表；各个路由的返回用 `$ref` 指过来
SCHEMAS: dict[str, dict[str, Any]] = {
    "SendResult": SEND_RESULT_SCHEMA,
    "InboxItem": INBOX_ITEM_SCHEMA,
    "InboxResult": INBOX_RESULT_SCHEMA,
    "AckResult": ACK_RESULT_SCHEMA,
    "Status": STATUS_RESULT_SCHEMA,
    "Health": HEALTH_RESULT_SCHEMA,
    "Error": ERROR_RESULT_SCHEMA,
}


def ref(name: str) -> dict[str, str]:
    assert name in SCHEMAS, name
    return {"$ref": f"#/components/schemas/{name}"}


# ---------------------------------------------------------------- 可能出的错

BAD_REQUEST = (400, "请求有问题：bad_request、empty_text、text_too_long 等，见 docs/api/v1.md")
UNAUTHORIZED = (401, "没带密钥或密钥不对：unauthorized")
DISABLED = (403, "项目已停用：project_disabled（只推送的项目取话是 push_only）")
UNKNOWN_HANDLE = (404, "回复句柄不存在或不属于调用方：unknown_handle")
HANDLE_EXPIRED = (410, "回复句柄过期：handle_expired")
TOO_LARGE = (413, "请求体超过 64 KiB：too_large")
RATE_LIMITED = (429, "请求太多：rate_limited、too_many_failures 或 reply_limit，带 Retry-After")
INTERNAL = (500, "服务内部出错：internal（发送接口上可能已经向腾讯发过请求）")
SHUTTING_DOWN = (503, "服务正在关闭：shutting_down（没有发出任何东西）")


def responses_of(ok: str, *errors: tuple[int, str]) -> dict[int | str, dict[str, Any]]:
    """一个路由的 `responses`：200 的返回结构（`SCHEMAS` 里的名字），加上可能出的错（统一格式）。"""
    result: dict[int | str, dict[str, Any]] = {
        200: {"description": "成功", "content": {"application/json": {"schema": ref(ok)}}}
    }
    for status, description in errors:
        result[status] = {
            "description": description,
            "content": {"application/json": {"schema": ref("Error")}},
        }
    return result


MESSAGES_RESPONSES = responses_of(
    "SendResult",
    BAD_REQUEST,
    UNAUTHORIZED,
    DISABLED,
    TOO_LARGE,
    RATE_LIMITED,
    INTERNAL,
    SHUTTING_DOWN,
)
REPLIES_RESPONSES = responses_of(
    "SendResult",
    BAD_REQUEST,
    UNAUTHORIZED,
    DISABLED,
    UNKNOWN_HANDLE,
    HANDLE_EXPIRED,
    TOO_LARGE,
    RATE_LIMITED,
    INTERNAL,
    SHUTTING_DOWN,
)
INBOX_RESPONSES = responses_of(
    "InboxResult",
    BAD_REQUEST,
    UNAUTHORIZED,
    DISABLED,
    RATE_LIMITED,
    INTERNAL,
    SHUTTING_DOWN,
)
ACK_RESPONSES = responses_of(
    "AckResult",
    BAD_REQUEST,
    UNAUTHORIZED,
    DISABLED,
    TOO_LARGE,
    RATE_LIMITED,
    INTERNAL,
    SHUTTING_DOWN,
)
STATUS_RESPONSES = responses_of(
    "Status", UNAUTHORIZED, DISABLED, RATE_LIMITED, INTERNAL, SHUTTING_DOWN
)
HEALTH_RESPONSES = responses_of("Health", INTERNAL, SHUTTING_DOWN)

# ---------------------------------------------------------------- 整份描述

DESCRIPTION = (
    "clawbot-hub 的项目接口。除 GET /v1/health 外，每个请求都要带 "
    "`Authorization: Bearer <项目密钥>`。非 200 的响应正文一律是 "
    '`{"error": {"code": …, "message": …}}`。全部说明、curl 示例和错误代码表见 docs/api/v1.md。'
)

SECURITY_SCHEME_NAME = "projectKey"
SECURITY_SCHEMES = {
    SECURITY_SCHEME_NAME: {
        "type": "http",
        "scheme": "bearer",
        "description": "项目密钥（chub_ 开头），由运维用 hubctl project add 登记时给出，只显示一次",
    }
}
# 要密钥的路由在 `openapi_extra` 里带这一条
SECURED = {"security": [{SECURITY_SCHEME_NAME: []}]}
