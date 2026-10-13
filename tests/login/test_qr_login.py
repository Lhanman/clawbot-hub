"""扫码登录（`hubctl login` 的流程，函数层）。

规格 `hub-login`「扫码登录」。腾讯那一侧用进程内的假上游，时钟用会自己往前走的假时钟（单线程从头
跑到尾，睡一秒就是假时间走一秒）。
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import stat
from datetime import datetime
from pathlib import Path

import pytest
import segno

from clawbot_hub.account import AccountFile, AccountStore
from clawbot_hub.login import LoginError, draw_qr, qr_login
from clawbot_hub.loginstate import (
    LoginMonitor,
    ScanMarker,
    read_scan_marker,
    scan_in_progress,
    write_scan_marker,
)
from clawbot_hub.upstream.base import Account, QrCode, QrStatus, UpstreamError
from tests.login.conftest import (
    account_file,
    conversation_tokens,
    events,
    login_row,
    seed_conversation,
)
from tests.support.fake_clock import FakeClock
from tests.support.fake_upstream import QR_START, QR_STATUS, FakeUpstream

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def clock() -> FakeClock:
    """会自己往前走的假时钟：`sleep(1)` 立刻返回，同时假时间走一秒。"""
    return FakeClock(auto_sleep=True)


@pytest.fixture
def upstream(clock) -> FakeUpstream:
    return FakeUpstream(clock=clock)


class Screen:
    """代替终端：记下说过的话、画过的二维码、问过验证码，并在每个动作发生的时刻看一眼现场。"""

    def __init__(self, paths, clock, answers=()) -> None:
        self.paths = paths
        self.clock = clock
        self.said: list[str] = []
        self.shown: list[str] = []
        self.asked = 0
        self.answers = list(answers)
        self.scanning_when_shown: list[bool] = []
        self.account_when_shown: list[bytes | None] = []

    def say(self, text: str) -> None:
        self.said.append(text)

    def show(self, content: str, png: Path) -> str:
        self.shown.append(content)
        self.scanning_when_shown.append(scan_in_progress(self.paths, self.clock))
        self.account_when_shown.append(
            self.paths.account.read_bytes() if self.paths.account.exists() else None
        )
        png.write_bytes(PNG_MAGIC + b"fake")
        os.chmod(png, 0o600)
        return f"[二维码 {content}]"

    def ask(self) -> str:
        self.asked += 1
        return self.answers.pop(0) if self.answers else ""

    @property
    def text(self) -> str:
        return "\n".join(self.said)


@pytest.fixture
def store(paths) -> AccountStore:
    return AccountStore(paths)


@pytest.fixture
def screen(paths, clock) -> Screen:
    return Screen(paths, clock)


def run_login(upstream, paths, store, clock, display_tz, screen, **extra):
    extra.setdefault("pid", os.getpid())
    return qr_login(
        upstream=upstream,
        paths=paths,
        store=store,
        clock=clock,
        tz=display_tz,
        say=screen.say,
        ask_verify_code=screen.ask,
        show=screen.show,
        **extra,
    )


@pytest.fixture
def login(upstream, paths, store, clock, display_tz, screen):
    def run(**extra):
        return run_login(upstream, paths, store, clock, display_tz, screen, **extra)

    return run


def nothing_left_behind(paths) -> None:
    assert not paths.login_pending.exists(), "login.pending 没有删"
    assert not paths.login_png.exists(), "二维码图片没有删"


# ------------------------------------------------------------------ 一次扫成


def test_one_scan_succeeds(login, upstream, store, paths, screen, clock, display_tz):
    upstream.qr_plan(["wait", "scaned", "confirmed"])
    started = clock.now_ms()

    account = login()

    assert isinstance(account, AccountFile)
    saved = store.read()
    assert saved == account
    assert (saved.bot_token, saved.user_id) == ("fake-token-1", "fake-owner@im.wechat")
    assert saved.bot_id == "fake-bot@im.bot"
    assert saved.base_url == upstream.base_url
    assert saved.updates_buf == ""
    assert saved.bound_at == datetime.fromtimestamp(clock.now_ms() / 1000, display_tz).isoformat(
        timespec="seconds"
    )
    assert "绑定成功" in screen.text
    assert "给 ClawBot 发任意一句话" in screen.text
    assert "24 小时" in screen.text
    nothing_left_behind(paths)
    assert [c.args["code"] for c in upstream.calls_to(QR_STATUS)] == ["fake-qr-1"] * 3
    assert len(upstream.calls_to(QR_START)) == 1
    assert clock.now_ms() - started == 2_000  # wait、scaned 之后各睡一秒，确认了就不睡
    assert {c.operation for c in upstream.calls} == {QR_START, QR_STATUS}


def test_the_credentials_file_is_private(login, paths):
    login()

    assert stat.S_IMODE(paths.account.stat().st_mode) == 0o600


def test_the_qr_is_shown_with_the_marker_in_place(login, upstream, screen, paths):
    login()

    assert screen.shown == [f"{upstream.base_url}/q?qrcode=fake-qr-1&bot_type=3"]
    assert screen.scanning_when_shown == [True]
    assert "[二维码 " in screen.text
    assert "用微信扫上面的二维码" in screen.text
    assert str(paths.login_png) in screen.text


def test_the_marker_holds_our_pid_and_the_start_time(
    upstream, paths, store, clock, display_tz, screen
):
    seen: list[ScanMarker | None] = []
    upstream.script(QR_STATUS, lambda call: seen.append(read_scan_marker(paths)) or "confirmed")
    started = clock.now_ms()

    run_login(upstream, paths, store, clock, display_tz, screen, pid=424242)

    assert seen == [ScanMarker(pid=424242, started_ms=started)]


def test_only_the_upstream_scan_calls_are_used(login, upstream):
    login()

    assert {c.operation for c in upstream.calls} <= {QR_START, QR_STATUS}


def test_rebinding_replaces_the_old_credentials(login, store, paths):
    store.write(account_file(bot_token="tok-old"))

    account = login()

    assert store.read().bot_token == "fake-token-1"
    assert account.bot_token == "fake-token-1"


def test_the_old_credentials_are_untouched_until_the_scan_succeeds(
    login, upstream, store, paths, screen
):
    store.write(account_file(bot_token="tok-old"))
    original = paths.account.read_bytes()
    seen: list[bytes] = []
    upstream.script(
        QR_STATUS,
        lambda call: seen.append(paths.account.read_bytes()) or "wait",
        "confirmed",
    )

    login()

    assert screen.account_when_shown == [original]
    assert seen == [original]
    assert paths.account.read_bytes() != original


# ------------------------------------------------------------------ 过期、超时


def test_an_expired_code_is_replaced(login, upstream, screen, store):
    upstream.qr_plan(["expired"], ["confirmed"])

    login()

    assert len(upstream.calls_to(QR_START)) == 2
    assert [c.args["code"] for c in upstream.calls_to(QR_STATUS)] == ["fake-qr-1", "fake-qr-2"]
    assert screen.text.count("二维码过期了，换一张新的") == 1
    assert len(screen.shown) == 2
    assert store.read().bot_token == "fake-token-1"


def test_a_code_that_is_never_scanned_is_replaced_after_180_seconds(login, upstream, screen, clock):
    upstream.qr_plan(["wait"], ["confirmed"])
    started = clock.now_ms()

    login()

    first = [c for c in upstream.calls_to(QR_STATUS) if c.args["code"] == "fake-qr-1"]
    assert len(first) == 180  # 每秒查一次，查满 180 秒
    assert clock.now_ms() - started == 180_000
    assert len(upstream.calls_to(QR_START)) == 2
    assert "二维码过期了，换一张新的" in screen.text


def test_five_expired_codes_end_without_a_binding(login, upstream, store, paths, screen):
    old = account_file(bot_token="tok-old")
    store.write(old)
    original = paths.account.read_bytes()
    upstream.qr_plan(*([["expired"]] * 5))

    with pytest.raises(LoginError, match="连续 5 张二维码都没扫，绑定没完成"):
        login()

    assert len(upstream.calls_to(QR_START)) == 5
    assert paths.account.read_bytes() == original
    assert screen.text.count("二维码过期了，换一张新的") == 4
    nothing_left_behind(paths)


def test_five_codes_that_are_never_scanned_take_fifteen_minutes(login, upstream, clock, paths):
    upstream.set_default(QR_STATUS, "wait")
    started = clock.now_ms()

    with pytest.raises(LoginError, match="连续 5 张"):
        login()

    assert clock.now_ms() - started == 5 * 180_000
    assert len(upstream.calls_to(QR_START)) == 5
    nothing_left_behind(paths)


def test_the_failure_message_says_what_to_do(login, upstream):
    upstream.set_default(QR_STATUS, "expired")

    with pytest.raises(LoginError) as raised:
        login()

    assert "重新运行 hubctl login" in str(raised.value)


# ------------------------------------------------------------------ 验证码


def test_a_verify_code_is_asked_and_sent_with_the_next_query(
    upstream, paths, store, clock, display_tz
):
    screen = Screen(paths, clock, answers=["123456"])
    upstream.qr_plan(["need_verifycode", "confirmed"])
    started = clock.now_ms()

    run_login(upstream, paths, store, clock, display_tz, screen)

    assert [c.args["verify_code"] for c in upstream.calls_to(QR_STATUS)] == [None, "123456"]
    assert screen.asked == 1
    assert "验证码" in screen.text
    assert clock.now_ms() == started  # 问完验证码马上带着它再查，不先等一秒


def test_the_code_keeps_being_sent_while_the_same_qr_goes_on(
    upstream, paths, store, clock, display_tz
):
    screen = Screen(paths, clock, answers=["123456"])
    upstream.qr_plan(["need_verifycode", "wait", "scaned", "confirmed"])

    run_login(upstream, paths, store, clock, display_tz, screen)

    assert [c.args["verify_code"] for c in upstream.calls_to(QR_STATUS)] == [
        None,
        "123456",
        "123456",
        "123456",
    ]


def test_a_wrong_code_is_asked_for_again(upstream, paths, store, clock, display_tz):
    screen = Screen(paths, clock, answers=["111111", "222222"])
    upstream.qr_plan(["need_verifycode", "need_verifycode", "confirmed"])

    run_login(upstream, paths, store, clock, display_tz, screen)

    assert [c.args["verify_code"] for c in upstream.calls_to(QR_STATUS)] == [
        None,
        "111111",
        "222222",
    ]
    assert screen.asked == 2


def test_the_code_is_trimmed_and_blank_means_none(upstream, paths, store, clock, display_tz):
    screen = Screen(paths, clock, answers=["  987654 \n", "   "])
    upstream.qr_plan(["need_verifycode", "need_verifycode", "confirmed"])

    run_login(upstream, paths, store, clock, display_tz, screen)

    assert [c.args["verify_code"] for c in upstream.calls_to(QR_STATUS)] == [None, "987654", None]


def test_a_new_qr_starts_without_the_old_verify_code(upstream, paths, store, clock, display_tz):
    screen = Screen(paths, clock, answers=["123456"])
    upstream.qr_plan(["need_verifycode", "expired"], ["confirmed"])

    run_login(upstream, paths, store, clock, display_tz, screen)

    assert [c.args["verify_code"] for c in upstream.calls_to(QR_STATUS)] == [
        None,
        "123456",
        None,
    ]


def test_too_many_wrong_codes_end_the_login(login, upstream, store, paths, screen):
    store.write(account_file(bot_token="tok-old"))
    original = paths.account.read_bytes()
    upstream.qr_plan(["verify_code_blocked"])

    with pytest.raises(LoginError, match="重新运行"):
        login()

    assert paths.account.read_bytes() == original
    assert len(upstream.calls_to(QR_START)) == 1  # 不换码
    nothing_left_behind(paths)


# ------------------------------------------------------------------ 改用别的域名、已经绑定


def test_the_redirect_host_is_used_for_the_following_queries(login, upstream):
    upstream.qr_plan(
        ["wait", {"status": "scaned_but_redirect", "redirect_host": "other.example"}, "wait"]
        + ["confirmed"]
    )

    login()

    assert [c.args["host"] for c in upstream.calls_to(QR_STATUS)] == [
        None,
        None,
        "other.example",
        "other.example",
    ]


def test_a_second_redirect_replaces_the_first(login, upstream):
    upstream.qr_plan(
        [
            {"status": "scaned_but_redirect", "redirect_host": "one.example"},
            {"status": "scaned_but_redirect", "redirect_host": "two.example"},
            "confirmed",
        ]
    )

    login()

    assert [c.args["host"] for c in upstream.calls_to(QR_STATUS)] == [
        None,
        "one.example",
        "two.example",
    ]


def test_a_redirect_without_a_host_keeps_the_one_we_had(login, upstream):
    upstream.qr_plan(
        [
            {"status": "scaned_but_redirect", "redirect_host": "one.example"},
            {"status": "scaned_but_redirect"},
            "confirmed",
        ]
    )

    login()

    assert [c.args["host"] for c in upstream.calls_to(QR_STATUS)] == [
        None,
        "one.example",
        "one.example",
    ]


def test_a_new_qr_goes_back_to_the_default_host(login, upstream):
    upstream.qr_plan(
        [{"status": "scaned_but_redirect", "redirect_host": "other.example"}, "expired"],
        ["confirmed"],
    )

    login()

    assert [c.args["host"] for c in upstream.calls_to(QR_STATUS)] == [None, "other.example", None]


def test_already_bound_to_this_instance(login, upstream, store, paths, screen):
    store.write(account_file(bot_token="tok-old"))
    original = paths.account.read_bytes()
    upstream.qr_plan(["binded_redirect"])

    with pytest.raises(LoginError, match="已经绑定在当前实例上"):
        login()

    assert paths.account.read_bytes() == original
    nothing_left_behind(paths)


def test_an_unknown_status_is_just_waited_out(login, upstream, store):
    upstream.qr_plan(["something_new", "confirmed"])

    login()

    assert store.read().bot_token == "fake-token-1"


# ------------------------------------------------------------------ 出错


def test_a_failed_status_query_is_retried_after_two_seconds(login, upstream, clock):
    upstream.script(QR_STATUS, UpstreamError("连不上"), UpstreamError("连不上"))
    started = clock.now_ms()

    login()

    assert len(upstream.calls_to(QR_STATUS)) == 3
    assert clock.now_ms() - started == 4_000


def test_failing_queries_still_use_up_the_180_seconds(login, upstream, clock, paths):
    upstream.set_default(QR_STATUS, UpstreamError("连不上"))
    started = clock.now_ms()

    with pytest.raises(LoginError, match="连续 5 张"):
        login()

    assert len(upstream.calls_to(QR_START)) == 5
    assert len(upstream.calls_to(QR_STATUS)) == 5 * 90  # 每次失败睡 2 秒，一张码查 90 次
    assert clock.now_ms() - started == 5 * 180_000
    nothing_left_behind(paths)


def test_no_qr_means_no_login(login, upstream, store, paths):
    store.write(account_file(bot_token="tok-old"))
    original = paths.account.read_bytes()
    upstream.script(QR_START, UpstreamError("HTTP 502"))

    with pytest.raises(LoginError) as raised:
        login()

    assert "取二维码失败：HTTP 502" in str(raised.value)
    assert "重新运行 hubctl login" in str(raised.value)
    assert paths.account.read_bytes() == original
    nothing_left_behind(paths)


@pytest.mark.parametrize(
    "broken",
    [{"user_id": ""}, {"bot_token": ""}],
    ids=["没有用户号", "没有令牌"],
)
def test_a_confirmation_without_the_essentials_is_not_saved(login, upstream, store, paths, broken):
    store.write(account_file(bot_token="tok-old"))
    original = paths.account.read_bytes()
    account = Account(
        **{"bot_token": "tok-x", "base_url": "", "bot_id": "b", "user_id": "u", **broken}
    )
    upstream.qr_plan([QrStatus("confirmed", account=account)])

    with pytest.raises(LoginError, match="绑定没完成"):
        login()

    assert paths.account.read_bytes() == original
    nothing_left_behind(paths)


def test_a_confirmation_without_an_account_is_not_saved(paths, store, clock, display_tz, screen):
    """真实现不会这样（响应里没有凭证它就抛错），但流程不能因此写出一个空的凭证文件。"""

    class Bare:
        def qr_start(self):
            return QrCode(code="c", image_content="https://example.invalid/q")

        def qr_status(self, code, verify_code, host):
            return QrStatus("confirmed", account=None)

    with pytest.raises(LoginError, match="绑定没完成"):
        run_login(Bare(), paths, store, clock, display_tz, screen)

    assert not paths.account.exists()
    nothing_left_behind(paths)


def test_ctrl_c_while_waiting_cleans_up(login, upstream, store, paths):
    store.write(account_file(bot_token="tok-old"))
    original = paths.account.read_bytes()
    upstream.script(QR_STATUS, "wait", KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        login()

    assert paths.account.read_bytes() == original
    nothing_left_behind(paths)


def test_ctrl_c_at_the_verify_code_prompt_cleans_up(upstream, paths, store, clock, display_tz):
    screen = Screen(paths, clock)

    def interrupted() -> str:
        raise KeyboardInterrupt

    upstream.qr_plan(["need_verifycode"])

    with pytest.raises(KeyboardInterrupt):
        qr_login(
            upstream=upstream,
            paths=paths,
            store=store,
            clock=clock,
            tz=display_tz,
            say=screen.say,
            ask_verify_code=interrupted,
            show=screen.show,
            pid=os.getpid(),
        )

    nothing_left_behind(paths)
    assert not paths.account.exists()


def test_a_stale_marker_and_picture_from_a_killed_login_are_replaced(login, paths):
    paths.login_pending.write_text("garbage")
    paths.login_png.write_bytes(b"old")

    login()

    nothing_left_behind(paths)


def test_someone_elses_marker_is_not_deleted(login, upstream, paths, clock):
    """另一个 `hubctl login` 在扫码：我们结束时不能把它的标记删了。"""
    other = ScanMarker(pid=os.getpid() + 1, started_ms=clock.now_ms())

    def steal(call):
        write_scan_marker(paths, pid=other.pid, started_ms=other.started_ms)
        return "confirmed"

    upstream.script(QR_STATUS, steal)

    login()

    assert read_scan_marker(paths) == other


# ------------------------------------------------------------------ 成功以后的顺序


def test_success_writes_the_file_first_then_voids_the_windows(
    login, database, store, paths, monkeypatch
):
    seed_conversation(database)
    order: list[str] = []
    real_write = store.write
    real_txn = database.txn

    def recording_write(account):
        order.append("file")
        real_write(account)

    def recording_txn():
        order.append("db")
        return real_txn()

    monkeypatch.setattr(store, "write", recording_write)
    monkeypatch.setattr(database, "txn", recording_txn)

    login(db=database)

    assert order == ["file", "db"]


def test_success_voids_every_window_and_every_handle_in_one_go(login, database, store):
    seed_conversation(database)

    account = login(db=database)

    state = conversation_tokens(database)
    assert all(row[1] is None and row[2] == 1 for row in state["windows"])
    assert all(row[4] is None for row in state["inbox"])
    assert account == store.read()


def test_the_binding_is_logged_without_credentials(login, database):
    login(db=database)

    (event,) = events(database)
    assert (event["kind"], event["level"]) == ("login", "INFO")
    assert "绑定" in event["detail"]
    dumped = json.dumps(events(database), ensure_ascii=False)
    for secret in ("fake-token-1", "fake-owner@im.wechat", "fake-bot@im.bot"):
        assert secret not in dumped


def test_the_database_step_is_one_transaction(login, database, monkeypatch):
    seed_conversation(database)
    opened = []
    real_txn = database.txn

    def counting():
        opened.append(1)
        return real_txn()

    monkeypatch.setattr(database, "txn", counting)

    login(db=database)

    assert len(opened) == 1


def test_without_a_database_only_the_file_is_written(login, paths, store):
    login(db=None)

    assert store.read() is not None
    assert not paths.db.exists()


def test_the_file_is_written_even_if_the_login_dies_before_the_database_step(
    login, database, store, paths, monkeypatch
):
    """写完文件后被杀：文件已经是新的，窗口还没作废；服务的比对随后补做。"""
    seed_conversation(database)

    class Killed(BaseException):
        pass

    def killed():
        raise Killed

    monkeypatch.setattr(database, "txn", killed)

    with pytest.raises(Killed):
        login(db=database)

    assert store.read().bot_token == "fake-token-1"
    assert sum(1 for r in conversation_tokens(database)["windows"] if r[2] == 0) == 2
    nothing_left_behind(paths)
    monkeypatch.undo()

    monitor = LoginMonitor(paths, database, store, FakeClock())
    result = monitor.sync()

    assert result.changed
    state = conversation_tokens(database)
    assert all(row[1] is None and row[2] == 1 for row in state["windows"])
    assert all(row[4] is None for row in state["inbox"])
    # 再做一遍，结果不变
    again = monitor.sync()
    assert not again.changed
    assert conversation_tokens(database) == state
    assert login_row(database).state == "ok"


def test_a_database_that_is_busy_does_not_undo_the_binding(
    login, database, store, screen, monkeypatch
):
    seed_conversation(database)

    def busy():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(database, "txn", busy)

    account = login(db=database)

    assert store.read() == account
    assert "绑定成功" in screen.text
    assert "补做" in screen.text
    assert "locked" not in screen.text


def test_nothing_secret_is_printed(login, screen, upstream):
    login()

    secrets = ("fake-token-1", "fake-owner@im.wechat", "fake-bot@im.bot")
    assert not any(secret in screen.text for secret in secrets)


# ------------------------------------------------------------------ 画二维码


def test_draw_qr_saves_a_private_png_and_returns_the_terminal_drawing(paths):
    drawing = draw_qr("https://example.invalid/q?qrcode=abc&bot_type=3", paths.login_png)

    assert paths.login_png.read_bytes().startswith(PNG_MAGIC)
    assert len(paths.login_png.read_bytes()) > 200
    assert stat.S_IMODE(paths.login_png.stat().st_mode) == 0o600
    assert drawing.strip()
    assert "█" in drawing or "▀" in drawing or "▄" in drawing
    assert "https://example.invalid" not in drawing


def test_the_picture_and_the_drawing_are_what_ashare_quant_makes(paths):
    content = "https://example.invalid/q?qrcode=abc&bot_type=3"

    drawing = draw_qr(content, paths.login_png)

    code = segno.make(content, error="m")
    expected = io.BytesIO()
    code.save(expected, kind="png", scale=8, border=3)
    assert paths.login_png.read_bytes() == expected.getvalue()
    _, height = code.symbol_size(border=4)
    assert len(drawing.splitlines()) == (height + 1) // 2  # 两行点阵并成一行


def test_draw_qr_overwrites_an_old_wider_picture(paths):
    paths.login_png.write_bytes(b"x" * 100_000)
    paths.login_png.chmod(0o644)

    draw_qr("hello", paths.login_png)

    data = paths.login_png.read_bytes()
    assert data.startswith(PNG_MAGIC) and len(data) < 100_000
    assert stat.S_IMODE(paths.login_png.stat().st_mode) == 0o600


def test_different_contents_draw_differently(paths):
    one = draw_qr("https://example.invalid/one", paths.login_png)
    first = paths.login_png.read_bytes()
    two = draw_qr("https://example.invalid/two", paths.login_png)

    assert one != two
    assert first != paths.login_png.read_bytes()
