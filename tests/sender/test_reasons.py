"""原因代码和原文：逐个触发 `hub-send` 原因表里的每个代码，核对状态、代码和文字。
规格：`hub-send`「原因代码和原文」。

预期的文字都是照着规格表手写的（不从被测的代码里引用），变量代入以后逐字比较。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from clawbot_hub.config import Config
from clawbot_hub.quota import PUSH, Deny, Quota
from clawbot_hub.sender import Origin
from clawbot_hub.upstream.base import Accepted, NoResponse, Rejected, SessionExpired, Transient
from tests.sender.conftest import long_text, takes
from tests.support.fake_upstream import DEFAULT
from tests.support.hub import tweak
from tests.support.threads import run_in_thread

HUB = Origin("_hub", "服务")

# 规格表里的每个代码，和它可以出现在哪些状态里（"出现在"一栏）
APPEARS_AS = {
    "never_contacted": {"rejected", "skipped"},
    "window_closed": {"rejected", "skipped"},
    "quota_exhausted": {"rejected"},
    "quota_insufficient": {"rejected"},
    "project_cap": {"rejected"},
    "reserved_for_others": {"rejected"},
    "deferrable_denied": {"rejected"},
    "too_long": {"rejected"},
    "upstream_blocked": {"rejected"},
    "login_missing": {"failed"},
    "login_expired": {"failed", "partial"},
    "login_broken": {"failed"},
    "upstream_rejected": {"failed", "partial"},
    "upstream_unreachable": {"failed", "partial"},
    "no_response": {"unconfirmed", "partial"},
    "no_message_id": {"unconfirmed", "partial"},
    "send_timeout": {"partial", "failed"},
    "busy": {"failed"},
    "interrupted": {"unconfirmed"},
    "duplicate_in_flight": {"unconfirmed"},
}

NEVER = "还没收到你在微信里发的消息：给 ClawBot 发任意一句话就能开始接收"
RECOVER = "给 ClawBot 发任意一句话就能恢复"

CASES: dict[str, tuple[str, Callable]] = {}


def case(code: str, variant: str = ""):
    def register(build: Callable) -> Callable:
        CASES[f"{code}/{variant}" if variant else code] = (code, build)
        return build

    return register


def beijing(year: int, month: int, day: int, hour: int, minute: int) -> int:
    when = datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai"))
    return int(when.timestamp() * 1000)


def bound(make_hub, config: Config | None = None, **project):
    """绑定、使用者说了一句话、登记 alpha（保底 6、上限 8，要改的用关键字传）。"""
    hub = make_hub(config)
    project.setdefault("guaranteed", 6)
    project.setdefault("cap", 8)
    hub.bind()
    hub.speak()
    return hub, hub.add("alpha", **project)


# ---------------------------------------------------------------- 窗口没开


@case("never_contacted")
def _never_contacted(make_hub):
    hub = make_hub()
    hub.bind()
    a = hub.add("alpha", guaranteed=6, cap=8)
    return hub, hub.sender.push(a, "你好"), "rejected", NEVER


@case("never_contacted", "skipped")
def _never_contacted_skipped(make_hub):
    hub = make_hub()
    hub.bind()
    a = hub.add("alpha", guaranteed=6, cap=8)
    return hub, hub.sender.push(a, "你好", on_closed="skip"), "skipped", NEVER


@case("window_closed")
def _window_closed(make_hub):
    hub, a = bound(make_hub)
    hub.clock.advance(25 * 3600)
    text = f"对话窗口过期了（你最后一次发消息是 10月12日 10:00），{RECOVER}"
    return hub, hub.sender.push(a, "你好"), "rejected", text


@case("window_closed", "skipped")
def _window_closed_skipped(make_hub):
    hub, a = bound(make_hub)
    hub.clock.advance(25 * 3600)
    text = f"对话窗口过期了（你最后一次发消息是 10月12日 10:00），{RECOVER}"
    return hub, hub.sender.push(a, "你好", on_closed="skip"), "skipped", text


@case("window_closed", "display-timezone")
def _window_closed_tokyo(make_hub):
    hub, a = bound(make_hub, tweak(Config(), display={"timezone": "Asia/Tokyo"}))
    hub.clock.advance(25 * 3600)
    text = f"对话窗口过期了（你最后一次发消息是 10月12日 11:00），{RECOVER}"
    return hub, hub.sender.push(a, "你好"), "rejected", text


@case("window_closed", "single-digit-month-and-day")
def _window_closed_single_digits(make_hub):
    hub = make_hub()
    hub.bind()
    hub.speak(at_ms=beijing(2026, 9, 5, 9, 5))
    a = hub.add("alpha", guaranteed=6, cap=8)
    text = f"对话窗口过期了（你最后一次发消息是 9月5日 09:05），{RECOVER}"
    return hub, hub.sender.push(a, "你好"), "rejected", text


# ---------------------------------------------------------------- 额度


@case("quota_exhausted")
def _quota_exhausted(make_hub):
    hub, a = bound(make_hub)
    hub.fill(10)
    text = f"这 24 小时的 10 条已经用完，{RECOVER}"
    return hub, hub.sender.push(a, "你好"), "rejected", text


@case("quota_exhausted", "configured-window")
def _quota_exhausted_configured(make_hub):
    cfg = tweak(Config(), quota={"window_hours": 12, "window_limit": 6})
    hub, a = bound(make_hub, cfg, guaranteed=3, cap=4)
    hub.fill(6)
    text = f"这 12 小时的 6 条已经用完，{RECOVER}"
    return hub, hub.sender.push(a, "你好"), "rejected", text


@case("quota_insufficient")
def _quota_insufficient(make_hub):
    hub, a = bound(make_hub)
    hub.fill(9)
    text = f"这 24 小时的 10 条只剩 1 条，不够发这 3 条；{RECOVER}"
    return hub, hub.sender.push(a, long_text(3)), "rejected", text


@case("project_cap")
def _project_cap(make_hub):
    hub, a = bound(make_hub, guaranteed=1, cap=2)
    hub.sender.push(a, "一")
    hub.sender.push(a, "二")
    text = "alpha 这 24 小时最多发 2 条，已经发了 2 条，不够发这 1 条"
    return hub, hub.sender.push(a, "三"), "rejected", text


@case("project_cap", "partly-used")
def _project_cap_partly_used(make_hub):
    hub, a = bound(make_hub, guaranteed=1, cap=3)
    hub.sender.push(a, "一")
    hub.sender.push(a, "二")
    text = "alpha 这 24 小时最多发 3 条，已经发了 2 条，不够发这 2 条"
    return hub, hub.sender.push(a, long_text(2)), "rejected", text


@case("reserved_for_others")
def _reserved_for_others(make_hub):
    hub, a = bound(make_hub)
    hub.add("beta", guaranteed=2, cap=4)
    for number in range(6):
        assert hub.sender.push(a, f"第 {number} 条").status == "sent"
    text = "剩下的 4 条是留给回复和其他项目的，这 1 条先不发"
    return hub, hub.sender.push(a, "第七条"), "rejected", text


@case("reserved_for_others", "some-room-left")
def _reserved_for_others_some_room_left(make_hub):
    hub, a = bound(make_hub)
    hub.add("beta", guaranteed=2, cap=4)
    for number in range(5):
        assert hub.sender.push(a, f"第 {number} 条").status == "sent"
    # 窗口里还剩 5 条：回复留 2、beta 的保底 2，alpha 自己能用的只剩 1 条，这条要 2 条
    text = "剩下的 4 条是留给回复和其他项目的，这 2 条先不发"
    return hub, hub.sender.push(a, long_text(2)), "rejected", text


@case("deferrable_denied")
def _deferrable_denied(make_hub):
    hub, a = bound(make_hub)
    hub.fill(6, project="alpha", kind=PUSH)
    assert hub.sender.push(a, "先让路的第一条", priority="deferrable").status == "sent"
    text = "额度比较紧，可延后的消息先不发（还剩 3 条）"
    return hub, hub.sender.push(a, "第二条", priority="deferrable"), "rejected", text


@case("too_long")
def _too_long(make_hub):
    hub = make_hub(tweak(Config(), send={"max_chars": 20000}))
    hub.bind()
    hub.speak()
    a = hub.add("alpha", guaranteed=6, cap=8)
    text = "这条要拆成 9 条，超过这个窗口最多能发的 8 条"
    return hub, hub.sender.push(a, long_text(9)), "rejected", text


@case("upstream_blocked")
def _upstream_blocked(make_hub):
    hub, a = bound(make_hub)
    hub.upstream.script("send_text", Rejected(200, 40001, "bad token"))
    hub.sender.push(a, "第一条")  # 被腾讯拒绝，窗口进入 30 分钟的暂停
    hub.clock.advance(10 * 60)
    text = (
        "腾讯拒绝了这个窗口里的发送（40001），先不再试；20 分钟后再试，"
        "或者给 ClawBot 发一句话重新开窗口"
    )
    return hub, hub.sender.push(a, "第二条"), "rejected", text


@case("upstream_blocked", "minutes-are-rounded-up")
def _upstream_blocked_rounded_up(make_hub):
    hub, a = bound(make_hub)
    hub.upstream.script("send_text", Rejected(403, None, ""))
    hub.sender.push(a, "第一条")
    hub.clock.advance(29 * 60 + 1)  # 还剩 59 秒
    text = (
        "腾讯拒绝了这个窗口里的发送（HTTP 403），先不再试；1 分钟后再试，"
        "或者给 ClawBot 发一句话重新开窗口"
    )
    return hub, hub.sender.push(a, "第二条"), "rejected", text


# ---------------------------------------------------------------- 登录


@case("login_missing")
def _login_missing(make_hub):
    hub, a = bound(make_hub)
    hub.unbind()
    return hub, hub.sender.push(a, "你好"), "failed", "还没绑定微信 ClawBot：运行 hubctl login"


@case("login_expired")
def _login_expired(make_hub):
    hub, a = bound(make_hub)
    hub.expire()
    text = "ClawBot 登录失效了：在电脑上运行 hubctl login 重新扫码"
    return hub, hub.sender.push(a, "你好"), "failed", text


@case("login_expired", "midway")
def _login_expired_midway(make_hub):
    hub, a = bound(make_hub)
    hub.upstream.script("send_text", DEFAULT, SessionExpired())
    text = "ClawBot 登录失效了：在电脑上运行 hubctl login 重新扫码；发出了 1 条，共 2 条"
    return hub, hub.sender.push(a, long_text(2)), "partial", text


@case("login_broken")
def _login_broken(make_hub):
    hub, a = bound(make_hub)
    hub.corrupt()
    text = "account.json 读不出来：重新运行 hubctl login"
    return hub, hub.sender.push(a, "你好"), "failed", text


# ---------------------------------------------------------------- 腾讯的结果


@case("upstream_rejected")
def _upstream_rejected(make_hub):
    hub, a = bound(make_hub)
    hub.upstream.script("send_text", Rejected(200, 40001, "bad token"))
    return hub, hub.sender.push(a, "你好"), "failed", "微信 ClawBot 拒绝：40001 bad token"


@case("upstream_rejected", "http-code-only")
def _upstream_rejected_http_only(make_hub):
    hub, a = bound(make_hub)
    hub.upstream.script("send_text", Rejected(403, None, ""))
    return hub, hub.sender.push(a, "你好"), "failed", "微信 ClawBot 拒绝：HTTP 403"


@case("upstream_rejected", "midway")
def _upstream_rejected_midway(make_hub):
    hub, a = bound(make_hub)
    hub.upstream.script("send_text", DEFAULT, Rejected(200, 40001, "bad token"))
    text = "微信 ClawBot 拒绝：40001 bad token；发出了 1 条，共 3 条"
    return hub, hub.sender.push(a, long_text(3)), "partial", text


@case("upstream_unreachable")
def _upstream_unreachable(make_hub):
    hub, a = bound(make_hub)
    hub.upstream.set_default("send_text", Transient("连接被拒绝"))
    text = "微信 ClawBot 发送失败，重试 2 次后仍然失败：连接被拒绝"
    return hub, hub.sender.push(a, "你好"), "failed", text


@case("upstream_unreachable", "midway")
def _upstream_unreachable_midway(make_hub):
    hub, a = bound(make_hub)
    hub.upstream.script("send_text", Accepted("m-1"))
    hub.upstream.set_default("send_text", Transient("连接被拒绝"))  # 第一条之后一直连不上
    text = "微信 ClawBot 发送失败，重试 2 次后仍然失败：连接被拒绝；发出了 1 条，共 3 条"
    return hub, hub.sender.push(a, long_text(3)), "partial", text


@case("no_response")
def _no_response(make_hub):
    hub, a = bound(make_hub)
    hub.upstream.script("send_text", NoResponse("读超时", read_timeout=True))
    text = "微信 ClawBot 没有回话，这条可能已经到了，不要重发"
    return hub, hub.sender.push(a, "你好"), "unconfirmed", text


@case("no_response", "midway")
def _no_response_midway(make_hub):
    hub, a = bound(make_hub)
    hub.upstream.script("send_text", DEFAULT, NoResponse("连接被重置"))
    text = "微信 ClawBot 没有回话，这条可能已经到了，不要重发；发出了 2 条，共 3 条"
    return hub, hub.sender.push(a, long_text(3)), "partial", text


@case("no_message_id")
def _no_message_id(make_hub):
    hub, a = bound(make_hub, tweak(Config(), upstream={"confirm": "strict"}))
    hub.upstream.script("send_text", Accepted(None))
    text = "微信 ClawBot 没有返回消息编号，这条可能没有到"
    return hub, hub.sender.push(a, "你好"), "unconfirmed", text


@case("no_message_id", "midway")
def _no_message_id_midway(make_hub):
    hub, a = bound(make_hub, tweak(Config(), upstream={"confirm": "strict"}))
    hub.upstream.script("send_text", Accepted(None), Rejected(200, 40001, "bad token"))
    text = "微信 ClawBot 没有返回消息编号，这条可能没有到；发出了 1 条，共 3 条"
    return hub, hub.sender.push(a, long_text(3)), "partial", text


# ---------------------------------------------------------------- 时限、忙、重放


@case("send_timeout")
def _send_timeout(make_hub):
    hub, a = bound(make_hub, tweak(Config(), upstream={"request_timeout": 30}))
    hub.upstream.script("send_text", takes(hub.clock, 29.5))
    text = "发送超过 30 秒还没发完，后面的 1 条没有发；发出了 1 条，共 2 条"
    return hub, hub.sender.push(a, long_text(2)), "partial", text


@case("send_timeout", "nothing-sent")
def _send_timeout_nothing_sent(make_hub):
    hub, a = bound(make_hub)
    b = hub.add("beta", guaranteed=1, cap=2)
    gate = hub.upstream.hold("send_text")
    sending = run_in_thread(hub.sender.push, a, "在发的")
    assert gate.entered.wait(5)
    waiting = run_in_thread(hub.sender.push, b, "等着的")
    hub.clock.wait_until_blocked(1)
    hub.clock.advance(29.5)
    gate.release()
    sending.result()
    return hub, waiting.result(), "failed", "发送超过 30 秒还没发完，后面的 1 条没有发"


@case("busy")
def _busy(make_hub):
    hub, a = bound(make_hub)
    b = hub.add("beta", guaranteed=1, cap=2)
    gate = hub.upstream.hold("send_text")
    sending = run_in_thread(hub.sender.push, a, "在发的")
    assert gate.entered.wait(5)
    waiting = run_in_thread(hub.sender.push, b, "等着的")
    hub.clock.wait_until_blocked(1)
    hub.clock.advance(30)  # 等锁等到预算用完
    result = waiting.result()
    gate.release()
    sending.result()
    return hub, result, "failed", "服务正忙，这条没有发，稍后再试"


@case("interrupted")
def _interrupted(make_hub):
    hub, a = bound(make_hub)

    def blow_up(call):
        raise RuntimeError("出意外了")

    hub.upstream.script("send_text", DEFAULT, blow_up)
    with pytest.raises(RuntimeError):
        hub.sender.push(a, long_text(3), dedupe_key="brief")
    result = hub.sender.push(a, long_text(3), dedupe_key="brief")
    assert result.duplicate
    return hub, result, "unconfirmed", "上次发送被中断，不知道发出了几条，不要重发"


@case("duplicate_in_flight")
def _duplicate_in_flight(make_hub):
    hub, a = bound(make_hub)
    gate = hub.upstream.hold("send_text")
    first = run_in_thread(hub.sender.push, a, "你好", dedupe_key="brief")
    assert gate.entered.wait(5)
    second = run_in_thread(hub.sender.push, a, "你好", dedupe_key="brief")
    hub.clock.wait_until_blocked(1)
    hub.clock.advance(30)  # 第二个等到自己的预算用完，第一个还没收尾
    result = second.result()
    gate.release()
    first.result()
    assert result.duplicate
    return hub, result, "unconfirmed", "同一个去重键的上一次发送还没结束，先不要重发"


# ---------------------------------------------------------------- 逐个核对


@pytest.mark.parametrize("name", sorted(CASES))
def test_each_reason_has_the_text_the_table_gives(name, make_hub):
    code, build = CASES[name]

    hub, result, status, reason = build(make_hub)

    assert (result.status, result.reason_code, result.reason) == (status, code, reason)
    assert result.status in APPEARS_AS[code]
    if not result.duplicate:  # 重放给的是第一次的那条记录，另外看
        row = hub.row(result.id)
        assert (row["status"], row["reason_code"], row["reason"]) == (status, code, reason)
    # 这一场里留下的所有记录，原因代码都在表里
    recorded = {row["reason_code"] for row in hub.rows() if row["reason_code"] is not None}
    assert recorded <= set(APPEARS_AS)


def test_every_code_in_the_table_is_exercised():
    assert {code for code, _ in CASES.values()} == set(APPEARS_AS)


def test_a_reason_code_nobody_wrote_a_text_for_is_a_programming_error(make_hub, clock):
    class Odd(Quota):
        def reserve(self, conn, request, *, outbox_id=None):
            return Deny("mystery", request.n)

    hub = make_hub(quota=Odd(Config(), clock))
    a = hub.ready()

    with pytest.raises(ValueError, match="原因代码不认识"):
        hub.sender.push(a, "你好")

    assert hub.rows() == []  # 判定和建记录是一个事务：一起回滚
    assert hub.upstream.calls == []
