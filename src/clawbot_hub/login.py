"""`hubctl login` 的两条路：扫码绑定（`qr_login`）和导入已有的凭证（`import_credentials`）。

**扫码**：取二维码（`bot_type=3`）、在终端里画出来并存一张 PNG（0600，结束后删掉）、每秒查一次
扫码状态，按状态表处理：`wait`/`scaned` 继续等；`expired` 或一张码等满 180 秒就换新码，连续 5 张
都没扫就失败；`need_verifycode` 问验证码；`verify_code_blocked` 和 `binded_redirect` 失败；
`scaned_but_redirect` 之后改用返回的域名查。扫码期间数据目录里有 `login.pending`（进程号和开始
时间），服务据此判断这时收到的 -14 不是真的失效。

**绑定成功**按这个顺序：先写 `account.json`（新凭证，收消息进度为空），再在**一个数据库事务里**
把所有窗口作废、把所有回复句柄的对话凭证置空。数据库还不存在（服务从没启动过）就只写文件。
两步之间被杀也没关系：服务的凭证监视（运行中）和启动比对（刚启动）发现摘要变了，会补做同一个事务。

规格：`hub-login`「扫码登录」「导入已有的凭证」。
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import logging
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import tzinfo
from pathlib import Path

import segno

from clawbot_hub import backup as backups
from clawbot_hub.account import AccountFile, AccountStore, bound_at_text
from clawbot_hub.clock import Clock
from clawbot_hub.db import Database, migrate
from clawbot_hub.events import add_event
from clawbot_hub.instance import InstanceBusy, acquire_instance_lock, service_running
from clawbot_hub.loginstate import (
    read_login,
    remove_scan_marker,
    reset_conversation,
    write_scan_marker,
)
from clawbot_hub.paths import FILE_MODE, HubPaths
from clawbot_hub.upstream.base import (
    Account,
    SessionExpired,
    Updates,
    Upstream,
    UpstreamError,
)

logger = logging.getLogger(__name__)

# 扫码的节奏（`hub-login`「扫码登录」）
QR_CODES = 5  # 连续换几张码都没扫就放弃
QR_WAIT_SECONDS = 180  # 一张码最多等多久（服务端约 2 分钟就回"过期"）
QR_POLL_SECONDS = 1.0  # 每隔多久查一次状态
QR_RETRY_SECONDS = 2.0  # 查询失败后等多久再查

BOUND_TEXT = "绑定成功。现在在微信里给 ClawBot 发任意一句话，之后 24 小时内它才能给你发消息。"


class LoginError(Exception):
    """命令没做成。信息可以原样给使用者看，里面没有凭证。"""


# ---------------------------------------------------------------- 画二维码


def draw_qr(content: str, png: Path) -> str:
    """把 `content` 存成 PNG（`png`，0600；旧文件更宽的权限也收回来），返回终端里画的样子。"""
    code = segno.make(content, error="m")
    fd = os.open(png, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)
        handle = os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        raise
    with handle:
        code.save(handle, kind="png", scale=8, border=3)
    drawing = io.StringIO()
    code.terminal(out=drawing, compact=True)
    return drawing.getvalue()


# ---------------------------------------------------------------- 扫码


def qr_login(
    *,
    upstream: Upstream,
    paths: HubPaths,
    store: AccountStore,
    clock: Clock,
    tz: tzinfo,
    say: Callable[[str], None],
    ask_verify_code: Callable[[], str],
    db: Database | None = None,
    show: Callable[[str, Path], str] = draw_qr,
    pid: int | None = None,
) -> AccountFile:
    """扫码绑定，成功返回写进 `account.json` 的凭证；没绑成功抛 `LoginError`（凭证文件不变），
    被 Ctrl-C 打断时 `KeyboardInterrupt` 照常抛出。不管怎么结束，`login.pending` 和二维码图片
    都会删掉。

    `say` 往终端打印一行；`ask_verify_code` 问使用者验证码；`show(链接, 图片路径)` 存图片并
    返回终端里画的样子；`db` 是已经打开的数据库（还不存在就传 `None`）。"""
    pid = os.getpid() if pid is None else pid
    write_scan_marker(paths, pid=pid, started_ms=clock.now_ms())
    try:
        account = _scan(upstream, paths, clock, say, ask_verify_code, show)
        return _bind(account, store, db, clock, tz, say)
    finally:
        remove_scan_marker(paths, pid=pid)
        with contextlib.suppress(OSError):
            paths.login_png.unlink()


def _scan(
    upstream: Upstream,
    paths: HubPaths,
    clock: Clock,
    say: Callable[[str], None],
    ask_verify_code: Callable[[], str],
    show: Callable[[str, Path], str],
) -> Account | None:
    for number in range(1, QR_CODES + 1):
        try:
            qr = upstream.qr_start()
        except UpstreamError as exc:
            raise LoginError(f"取二维码失败：{exc}。检查网络后重新运行 hubctl login") from None
        if number > 1:
            say("二维码过期了，换一张新的：")
        say(show(qr.image_content, paths.login_png))
        say(
            "用微信扫上面的二维码（约 2 分钟有效，过期会自动换新码）；"
            f"也可以打开图片 {paths.login_png}"
        )
        deadline_ms = clock.now_ms() + round(QR_WAIT_SECONDS * 1000)
        host: str | None = None  # 腾讯要求改用的域名；换了新码就回到默认的
        verify_code: str | None = None
        while clock.now_ms() < deadline_ms:
            try:
                status = upstream.qr_status(qr.code, verify_code, host)
            except UpstreamError as exc:
                logger.warning("查扫码状态失败，稍后再查：%s", exc)
                clock.sleep(QR_RETRY_SECONDS)
                continue
            name = status.status
            if name == "confirmed":
                return status.account
            if name == "expired":
                break
            if name == "verify_code_blocked":
                raise LoginError("验证码错的次数太多，这张二维码不能用了：重新运行 hubctl login")
            if name == "binded_redirect":
                raise LoginError("这个 ClawBot 已经绑定在当前实例上")
            if name == "need_verifycode":
                say("需要验证码：请输入微信里显示的验证码")
                verify_code = ask_verify_code().strip() or None
                continue  # 马上带着验证码再查
            if name == "scaned_but_redirect" and status.redirect_host:
                host = status.redirect_host
            clock.sleep(QR_POLL_SECONDS)  # wait、scaned 和不认识的状态都是继续等
    raise LoginError(f"连续 {QR_CODES} 张二维码都没扫，绑定没完成：重新运行 hubctl login")


def _bind(
    account: Account | None,
    store: AccountStore,
    db: Database | None,
    clock: Clock,
    tz: tzinfo,
    say: Callable[[str], None],
) -> AccountFile:
    if account is None or not account.bot_token or not account.user_id:
        raise LoginError("腾讯没有给出完整的登录凭证，绑定没完成：重新运行 hubctl login")
    bound = AccountFile(
        bot_token=account.bot_token,
        user_id=account.user_id,
        base_url=account.base_url,
        bot_id=account.bot_id,
        bound_at=bound_at_text(clock.now_ms(), tz),
        updates_buf="",
    )
    store.write(bound)  # 第一步：先写文件
    deferred = False
    if db is not None:
        try:
            with db.txn() as conn:  # 第二步：一个事务里作废窗口和回复句柄的对话凭证
                reset_conversation(conn)
                add_event(conn, clock.now_ms(), "login", "扫码绑定成功：对话窗口作废")
        except sqlite3.Error:
            deferred = True
    say(BOUND_TEXT)
    if deferred:
        say("提示：数据库这一步没做成（可能正被占用），服务会在几秒内自动补做，不用重新扫码。")
    return bound


# ---------------------------------------------------------------- 导入已有的凭证

HOUR_MS = 3_600_000


@dataclass(frozen=True)
class ProbeResult:
    """`--probe` 的结果。`status`：`valid` 腾讯没有拒绝；`expired` 回了 -14；`unknown` 没能确认。"""

    status: str
    reason: str = ""


@dataclass(frozen=True)
class ImportReport:
    """导入以后给使用者看的摘要：只有末 4 位、打码的编号、时间、条数，没有任何凭证。"""

    bot_id_tail: str
    user_masked: str
    bound_at: str
    window_left_ms: int | None  # 导入的窗口还剩多久；没有窗口是 None，已过期是负数或 0
    used: int | None  # 窗口里已用的条数；没有窗口是 None
    backup: Path | None  # `--replace` 时旧文件的备份
    probe: ProbeResult | None

    def lines(self) -> list[str]:
        out = ["已导入微信 ClawBot 的登录凭证（令牌不显示）"]
        out.append(f"机器人：…{self.bot_id_tail}" if self.bot_id_tail else "机器人：（没有编号）")
        out.append(f"使用者：{self.user_masked}")
        out.append(f"绑定时间：{self.bound_at}")
        if self.window_left_ms is None:
            out.append("对话窗口：没有（在微信里给 ClawBot 发任意一句话就会有）")
        elif self.window_left_ms <= 0:
            out.append(
                f"对话窗口：已经过期，已用 {self.used} 条（给 ClawBot 发任意一句话就能恢复）"
            )
        else:
            minutes = self.window_left_ms // 60_000
            out.append(
                f"对话窗口：还剩 {minutes // 60} 小时 {minutes % 60} 分钟，已用 {self.used} 条"
            )
        if self.backup is not None:
            out.append(f"旧的 account.json 已备份为 {self.backup.name}")
        if self.probe is not None:
            out.append(_probe_line(self.probe))
        return out


def _probe_line(result: ProbeResult) -> str:
    if result.status == "valid":
        return "探测：凭证有效（腾讯没有拒绝）"
    if result.status == "expired":
        return "探测：凭证已经失效（腾讯回 -14）：运行 hubctl login 重新扫码"
    return f"探测：没能确认（{result.reason}）"


def mask_user(user_id: str) -> str:
    """使用者编号打码：够长的留开头两个字符，其余用 `***`。"""
    return f"{user_id[:2]}***" if len(user_id) >= 6 else "***"


@dataclass(frozen=True)
class _Source:
    account: AccountFile
    context_token: str | None
    context_at_ms: int | None
    sent: int


def _is_whole(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _read_source(path: Path) -> _Source:
    """读 ashare-quant 的 `clawbot.json` 并检查。错误信息只说文件名和字段名，不带文件内容。"""
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        raise LoginError(f"导入文件读不出来：{path.name}") from None
    if not isinstance(data, dict):
        raise LoginError(f"导入文件读不出来：{path.name}（不是 JSON 对象）")
    missing = [
        name
        for name in ("bot_token", "user_id")
        if not (isinstance(data.get(name), str) and data[name])
    ]
    if missing:
        raise LoginError("导入文件缺少必需的字段（或不是文字）：" + "、".join(missing))
    text: dict[str, str] = {}
    for name in ("base_url", "bot_id", "bound_at", "updates_buf"):
        value = data.get(name)
        if value is not None and not isinstance(value, str):
            raise LoginError(f"导入文件里的字段 {name} 不是文字")
        text[name] = value or ""
    token = data.get("context_token")
    if token is not None and not isinstance(token, str):
        raise LoginError("导入文件里的字段 context_token 不是文字")
    at_ms = data.get("context_at_ms")
    if at_ms is not None and not (_is_whole(at_ms) and at_ms > 0):
        raise LoginError("导入文件里的字段 context_at_ms 不是正整数")
    sent = data.get("sent")
    if sent is not None and not (_is_whole(sent) and sent >= 0):
        raise LoginError("导入文件里的字段 sent 不是非负整数")
    account = AccountFile(bot_token=data["bot_token"], user_id=data["user_id"], **text)
    return _Source(account, token or None, at_ms, sent or 0)


def _record_import(
    db: Database, source: _Source, account: AccountFile, now_ms: int, replace: bool
) -> None:
    """导入的数据库一步（一个事务）：窗口、`_import` 名下的已用条数、新凭证的摘要。

    可以重复做：窗口按开始时间认，已用条数是设成 `sent`（不是累加）。"""
    with db.txn() as conn:
        row = read_login(conn)
        # `--replace`，或库里记着另一个账号：旧账号的窗口和回复句柄作废
        if replace or (row.digest is not None and row.digest != account.digest):
            reset_conversation(conn)
        if source.context_token and source.context_at_ms is not None:
            found = conn.execute(
                "SELECT id FROM windows WHERE started_ms = ? ORDER BY id LIMIT 1",
                (source.context_at_ms,),
            ).fetchone()
            if found is None:
                window_id = conn.execute(
                    "INSERT INTO windows (started_ms, context_token) VALUES (?, ?)",
                    (source.context_at_ms, source.context_token),
                ).lastrowid
            else:
                window_id = found["id"]
                conn.execute(
                    "UPDATE windows SET context_token = ?, voided = 0 WHERE id = ?",
                    (source.context_token, window_id),
                )
            project = conn.execute("SELECT id FROM projects WHERE name = '_import'").fetchone()
            conn.execute(
                "INSERT INTO usage (window_id, project_id, kind, n) VALUES (?, ?, 'push', ?)"
                " ON CONFLICT (window_id, project_id, kind) DO UPDATE SET n = excluded.n",
                (window_id, project["id"], source.sent),
            )
        conn.execute(
            "UPDATE account_state SET creds_digest = ?, login_state = 'ok', login_since_ms = ?"
            " WHERE id = 1",
            (account.digest, now_ms),
        )
        add_event(conn, now_ms, "login", "导入了已有的登录凭证")


def _backup_account(paths: HubPaths, clock: Clock, tz: tzinfo) -> Path:
    """覆盖前把旧的 `account.json` 原样备份成 `account.json.bak-<时间>`（0600）；
    同一秒里已经有同名的就加 `-2`、`-3`。"""
    stamp = backups.timestamp(clock.now_ms(), tz)
    raw = paths.account.read_bytes()
    for suffix in ("", *(f"-{number}" for number in range(2, 100))):
        target = paths.home / f"account.json.bak-{stamp}{suffix}"
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
        return target
    raise LoginError("旧的 account.json 备份不了：同名的备份太多，稍后再试")


def _probe(upstream: Upstream, account: AccountFile, timeout: float) -> ProbeResult:
    """用导入的凭证和导入的进度收一次消息，只看结果的种类：返回的消息和新进度都不处理、不保存。"""
    outcome = upstream.get_updates(account.to_account(), account.updates_buf, timeout)
    if isinstance(outcome, SessionExpired):
        return ProbeResult("expired", "-14")
    if isinstance(outcome, Updates):
        if outcome.timed_out:
            return ProbeResult("unknown", "读超时，没有收到响应")
        return ProbeResult("valid")
    return ProbeResult("unknown", outcome.reason)


def import_credentials(
    source: Path,
    *,
    paths: HubPaths,
    store: AccountStore,
    clock: Clock,
    tz: tzinfo,
    replace: bool = False,
    probe: Upstream | None = None,
    probe_timeout: float = 45.0,
    window_hours: int = 24,
) -> ImportReport:
    """把 ashare-quant 的 `clawbot.json` 接过来；做不成抛 `LoginError`。

    顺序：检查导入文件（什么都还没碰）→ 看服务有没有在运行 → 非阻塞拿 `hub.lock` 并一直持有 →
    已经有 `account.json` 又没说 `--replace` 就拒绝 → **先数据库**（一个事务）→ 备份旧文件 →
    **后写文件**。`probe` 给了才联网，用导入的凭证和进度收一次消息，只看是不是 -14。"""
    imported = _read_source(source)
    if service_running(paths, clock) is not None:
        raise LoginError("服务正在运行：先停服务再导入")
    try:
        lock = acquire_instance_lock(paths)
    except InstanceBusy:
        raise LoginError("服务刚好在启动，稍后再试") from None
    with lock:
        existed = paths.account.exists()
        if existed and not replace:
            raise LoginError("已经有 account.json 了：要覆盖请加 --replace（旧文件会先备份）")
        now = clock.now_ms()
        account = dataclasses.replace(
            imported.account, bound_at=imported.account.bound_at or bound_at_text(now, tz)
        )
        migrate(paths, clock=clock, tz=tz)
        db = Database(paths.db)
        try:
            _record_import(db, imported, account, now, replace)
        finally:
            db.close()
        saved_as = _backup_account(paths, clock, tz) if existed else None
        store.write(account)
        probed = _probe(probe, account, probe_timeout) if probe is not None else None
    has_window = bool(imported.context_token) and imported.context_at_ms is not None
    return ImportReport(
        bot_id_tail=account.bot_id[-4:],
        user_masked=mask_user(account.user_id),
        bound_at=account.bound_at,
        window_left_ms=(
            imported.context_at_ms + window_hours * HOUR_MS - now if has_window else None
        ),
        used=imported.sent if has_window else None,
        backup=saved_as,
        probe=probed,
    )
