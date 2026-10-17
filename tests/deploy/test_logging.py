"""日志（任务 D6，规格 `hub-deploy`「日志」）。

日志写到 `<home>/logs/hub.log`（按大小轮转）并同时输出到标准错误；里面不能有登录凭证、项目密钥、
Bark 设备码、消息正文：日志层统一把已知的秘密值和 `chub_` 开头的密钥格式换成 `***`，是代码本身
不往日志里写这些以外的第二道防线。请求日志只记方法、路径（不含查询串）、状态码、项目名、耗时。

这里分三块：日志层本身（文件、权限、轮转、级别、脱敏）、请求日志（接口层写的一行）、收到消息时
写的那一行。
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import stat
import types
from zoneinfo import ZoneInfo

import pytest

import clawbot_hub.api as api_module
from clawbot_hub.account import AccountFile, AccountStore
from clawbot_hub.api import LOGGED_PATH_CHARS, PROJECT_SCOPE_KEY, Guard
from clawbot_hub.bark import ENV_KEY
from clawbot_hub.config import Config, LogConfig
from clawbot_hub.logging_setup import setup_logging
from clawbot_hub.paths import DIR_MODE, FILE_MODE
from tests.api.conftest import LAN, OUTSIDE
from tests.support.hub import tweak

TZ = ZoneInfo("Asia/Shanghai")
TOKEN = "tok-live-bot-secret-0123456789"
BARK_CODE = "BARKdevice0123456789abcd"
PROJECT_KEY = "chub_" + "Ab3_-" * 8 + "xyz"  # chub_ 加 43 个字符
assert len(PROJECT_KEY) == 5 + 43

LOGGER = logging.getLogger("clawbot_hub.testing")


class Logs:
    """一次日志配置：文件在数据目录里，标准错误换成内存里的一份，好比对。"""

    def __init__(self, paths, **settings: object) -> None:
        self.paths = paths
        self.stream = io.StringIO()
        environ = settings.pop("environ", {})
        self.setup = setup_logging(
            paths, LogConfig(**settings), tz=TZ, environ=environ, stream=self.stream
        )

    def flush(self) -> None:
        for handler in logging.getLogger().handlers:
            handler.flush()

    @property
    def file(self) -> str:
        self.flush()
        return self.paths.log_file.read_text(encoding="utf-8")

    @property
    def err(self) -> str:
        return self.stream.getvalue()

    @property
    def lines(self) -> list[str]:
        return self.file.splitlines()


@pytest.fixture
def make_logs(paths):
    started: list[Logs] = []

    def build(**settings: object) -> Logs:
        logs = Logs(paths, **settings)
        started.append(logs)
        return logs

    yield build
    for logs in started:
        logs.setup.close()


def bind(paths, token: str = TOKEN) -> None:
    AccountStore(paths).write(AccountFile(bot_token=token, user_id="owner@im.wechat"))


# ---------------------------------------------------------------- 日志层本身


def test_a_line_goes_to_the_file_and_to_standard_error(make_logs):
    logs = make_logs()

    LOGGER.info("你好 %s", "世界")

    pattern = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+08:00 INFO clawbot_hub\.testing 你好 世界"
    assert re.fullmatch(pattern, logs.lines[-1])
    assert re.fullmatch(pattern, logs.err.splitlines()[-1])


def test_the_time_is_written_in_the_display_timezone(paths):
    stream = io.StringIO()
    setup = setup_logging(paths, LogConfig(), tz=ZoneInfo("Asia/Tokyo"), stream=stream)
    try:
        LOGGER.info("东京")
    finally:
        setup.close()

    assert re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+09:00 ", stream.getvalue())


def test_the_log_directory_and_file_are_made_private(make_logs, paths):
    assert not paths.logs.exists()

    make_logs()

    assert stat.S_IMODE(paths.logs.stat().st_mode) == DIR_MODE
    assert stat.S_IMODE(paths.log_file.stat().st_mode) == FILE_MODE


def test_the_file_is_private_whatever_the_umask_is(make_logs, paths):
    old = os.umask(0)  # 最宽松的 umask：权限必须是创建时就带着的，不能指望 umask
    try:
        logs = make_logs(max_mb=1, keep=2)
        for _ in range(1200):
            LOGGER.info("填满一份日志" + "x" * 1000)
        logs.flush()
    finally:
        os.umask(old)

    names = sorted(path.name for path in paths.logs.iterdir())
    assert names == ["hub.log", "hub.log.1"]
    for path in paths.logs.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == FILE_MODE, path.name


def test_rotation_keeps_the_configured_number_of_files(make_logs, paths):
    logs = make_logs(max_mb=1, keep=3)

    for _ in range(6000):  # 约 6 MB
        LOGGER.info("轮转测试" + "y" * 1000)
    logs.flush()

    names = sorted(path.name for path in paths.logs.iterdir())
    assert names == ["hub.log", "hub.log.1", "hub.log.2", "hub.log.3"]
    for path in paths.logs.iterdir():
        assert path.stat().st_size <= 1024 * 1024, path.name
        assert stat.S_IMODE(path.stat().st_mode) == FILE_MODE, path.name


def test_the_defaults_are_five_megabytes_and_five_files(make_logs, paths):
    make_logs()

    handler = next(h for h in logging.getLogger().handlers if hasattr(h, "maxBytes"))
    assert (handler.maxBytes, handler.backupCount) == (5 * 1024 * 1024, 5)


def test_an_existing_log_file_is_appended_to_not_replaced(make_logs, paths):
    paths.logs.mkdir(mode=DIR_MODE)
    paths.log_file.write_text("上一次留下的一行\n", encoding="utf-8")
    paths.log_file.chmod(FILE_MODE)

    logs = make_logs()
    LOGGER.info("这一次的")

    assert logs.lines[0] == "上一次留下的一行"
    assert logs.lines[-1].endswith("这一次的")


# ---------------------------------------------------------------- 级别


def test_the_default_level_is_info(make_logs):
    logs = make_logs()

    LOGGER.debug("看不见的调试")
    LOGGER.info("看得见的")

    assert "看不见的调试" not in logs.file
    assert "看得见的" in logs.file


def test_a_higher_level_hides_the_lower_ones(make_logs):
    logs = make_logs(level="WARNING")

    LOGGER.info("普通")
    LOGGER.warning("警告")

    assert "普通" not in logs.file
    assert "警告" in logs.file


def test_debug_level_shows_our_debug_lines_and_children_follow(make_logs):
    logs = make_logs(level="DEBUG")

    logging.getLogger("clawbot_hub.poller").debug("子模块的调试")

    assert "子模块的调试" in logs.file


@pytest.fixture
def loud_root():
    """根日志器原来是最啰嗦的 DEBUG（别的程序、测试工具都可能这样设）：日志层装上以后，别的库
    的日志得被压回警告以上。"""
    root = logging.getLogger()
    saved = root.level
    root.setLevel(logging.DEBUG)
    yield root
    root.setLevel(saved)


def test_other_libraries_stay_quiet_even_at_debug(loud_root, make_logs):
    """DEBUG 级别也不记正文和凭证：别的库（urllib3、uvicorn……）的调试输出会带请求的细节，
    一律只放行警告以上。"""
    logs = make_logs(level="DEBUG")

    logging.getLogger("urllib3.connectionpool").debug("GET /device-code/title/body")
    logging.getLogger("uvicorn.error").info("Started server process")
    logging.getLogger("urllib3.connectionpool").warning("连接池满了")

    assert "device-code" not in logs.file and "Started server" not in logs.file
    assert "连接池满了" in logs.file


def test_closing_puts_logging_back_the_way_it_was(paths):
    root = logging.getLogger()
    ours = logging.getLogger("clawbot_hub")
    saved = (root.level, ours.level)
    root.setLevel(logging.ERROR)  # 都不是日志层自己会设的值，还原没做就看得出来
    ours.setLevel(logging.CRITICAL)
    try:
        before = (list(root.handlers), root.level, ours.level)

        setup = setup_logging(paths, LogConfig(level="DEBUG"), tz=TZ, stream=io.StringIO())
        assert (root.level, ours.level) != before[1:]  # 装上以后确实变了
        setup.close()

        assert (list(root.handlers), root.level, ours.level) == before
        assert setup.file_handler.stream is None  # 日志文件也关了
        LOGGER.error("关掉以后")
        assert "关掉以后" not in paths.log_file.read_text(encoding="utf-8")

        root.setLevel(logging.INFO)  # 别人后来改的
        setup.close()  # 重复关没关系，也不会把别人改的又改回去
        assert root.level == logging.INFO
    finally:
        root.setLevel(saved[0])
        ours.setLevel(saved[1])


def test_setting_up_twice_does_not_double_the_lines(paths):
    first_stream, second_stream = io.StringIO(), io.StringIO()
    first = setup_logging(paths, LogConfig(), tz=TZ, stream=first_stream)
    second = setup_logging(paths, LogConfig(), tz=TZ, stream=second_stream)
    try:
        assert first.file_handler.stream is None  # 前一个的日志文件已经关了
        LOGGER.info("只出现一次")
    finally:
        second.close()
        first.close()

    assert paths.log_file.read_text(encoding="utf-8").count("只出现一次") == 1
    assert "只出现一次" not in first_stream.getvalue()  # 前一个已经被换掉
    assert second_stream.getvalue().count("只出现一次") == 1


# ---------------------------------------------------------------- 脱敏


def both(logs: Logs) -> str:
    return logs.file + "\n" + logs.err


def test_the_login_token_is_masked(make_logs, paths):
    bind(paths)
    logs = make_logs()

    LOGGER.error("请求失败，凭证是 %s，再试", TOKEN)

    assert TOKEN not in both(logs)
    assert "请求失败，凭证是 ***，再试" in logs.file
    assert "请求失败，凭证是 ***，再试" in logs.err


def test_a_project_key_is_masked_without_being_known_in_advance(make_logs):
    logs = make_logs()

    LOGGER.warning("有人拿 %s 来", PROJECT_KEY)
    LOGGER.warning(f"又一次 Bearer {PROJECT_KEY}!")

    assert PROJECT_KEY not in both(logs)
    assert "有人拿 *** 来" in logs.file
    assert "又一次 Bearer ***!" in logs.file


def test_a_bark_code_from_the_environment_is_masked(make_logs):
    logs = make_logs(environ={ENV_KEY: BARK_CODE})

    LOGGER.error("推送失败：%s", BARK_CODE)

    assert BARK_CODE not in both(logs)
    assert "推送失败：***" in logs.file


def test_a_bark_code_inside_a_push_address_is_masked_too(make_logs):
    logs = make_logs(environ={ENV_KEY: f"https://bark.example.test/{BARK_CODE}"})

    LOGGER.error("推送失败：%s", BARK_CODE)
    LOGGER.error("推送地址 https://bark.example.test/%s/标题/正文", BARK_CODE)

    assert BARK_CODE not in both(logs)


def test_a_bark_code_from_the_file_is_masked(make_logs, paths):
    paths.bark.write_text(f'{{"key": "{BARK_CODE}"}}', encoding="utf-8")
    logs = make_logs()

    LOGGER.error("推送失败：%s", BARK_CODE)

    assert BARK_CODE not in both(logs)


def test_all_three_at_once(make_logs, paths):
    bind(paths)
    logs = make_logs(environ={ENV_KEY: BARK_CODE})

    LOGGER.error("凭证 %s 密钥 %s 设备码 %s", TOKEN, PROJECT_KEY, BARK_CODE)

    assert logs.lines[-1].endswith("凭证 *** 密钥 *** 设备码 ***")
    for secret in (TOKEN, PROJECT_KEY, BARK_CODE):
        assert secret not in both(logs)


def test_a_token_that_shows_up_after_the_start_is_masked_from_then_on(make_logs, paths):
    logs = make_logs()
    LOGGER.info("还没绑定")

    bind(paths)  # 服务运行中 hubctl login 写好了凭证
    LOGGER.error("凭证 %s", TOKEN)

    assert TOKEN not in both(logs)


def test_a_bark_code_set_while_running_is_masked_from_then_on(make_logs, paths):
    logs = make_logs()
    LOGGER.info("还没配设备码")

    paths.bark.write_text(f'{{"key": "{BARK_CODE}"}}', encoding="utf-8")  # hubctl alerts set-key
    LOGGER.error("推送失败：%s", BARK_CODE)

    assert BARK_CODE not in both(logs)


def test_an_old_token_stays_masked_after_the_account_is_rebound(make_logs, paths):
    bind(paths)
    logs = make_logs()
    LOGGER.info("先用旧的")

    bind(paths, "tok-the-new-token-9876543210")
    LOGGER.error("旧的 %s 新的 %s", TOKEN, "tok-the-new-token-9876543210")

    assert TOKEN not in both(logs)
    assert "tok-the-new-token-9876543210" not in both(logs)


def test_an_old_token_stays_masked_after_the_account_file_is_gone(make_logs, paths):
    bind(paths)
    logs = make_logs()
    paths.account.unlink()

    LOGGER.error("凭证 %s", TOKEN)

    assert TOKEN not in both(logs)


def test_an_unreadable_secret_file_does_not_stop_the_logging(make_logs, paths):
    paths.account.write_text("这不是 JSON", encoding="utf-8")
    paths.bark.write_text("[1, 2, 3]", encoding="utf-8")
    logs = make_logs()

    LOGGER.info("照常写")

    assert "照常写" in logs.file


def test_a_traceback_is_masked_too(make_logs, paths):
    bind(paths)
    logs = make_logs()

    try:
        raise RuntimeError(f"连接 {TOKEN} 失败")
    except RuntimeError:
        LOGGER.exception("出错了")

    assert TOKEN not in both(logs)
    assert "RuntimeError: 连接 *** 失败" in logs.file
    assert "Traceback" in logs.file


def test_a_secret_in_the_arguments_of_a_lazy_message_is_masked(make_logs, paths):
    bind(paths)
    logs = make_logs()

    LOGGER.info("%s-%s", "前", {"token": TOKEN})

    assert TOKEN not in both(logs)


def test_nothing_is_masked_when_there_is_nothing_to_hide(make_logs):
    logs = make_logs()

    LOGGER.info("普通的一行 chub_ 和 chub_short 都不是密钥")

    assert logs.lines[-1].endswith("普通的一行 chub_ 和 chub_short 都不是密钥")


# ---------------------------------------------------------------- 请求日志


def request_lines(logs: Logs) -> list[str]:
    return [line.split(" ", 3)[3] for line in logs.lines if " clawbot_hub.api " in line]


def test_a_request_is_logged_with_method_path_status_project_and_time(api, make_logs):
    logs = make_logs()

    response = api.fetch("wait=0")

    assert response.status_code == 200
    [line] = [line for line in logs.lines if " clawbot_hub.api " in line]
    assert re.search(r"INFO clawbot_hub\.api GET /v1/inbox 200 alpha \d+ms$", line)


def test_the_query_string_and_the_key_never_reach_the_log(api, make_logs):
    logs = make_logs()

    api.fetch("wait=0&max=3")
    api.get("/v1/status?secret-query=1")

    text = both(logs)
    assert "wait=0" not in text and "max=3" not in text and "secret-query" not in text
    assert api.key("alpha") not in text
    assert "Authorization" not in text and "Bearer" not in text


def test_the_body_of_a_request_never_reaches_the_log(api, make_logs):
    logs = make_logs(level="DEBUG")

    api.send("这是不能进日志的机密正文")
    api.post("/v1/messages", content=b"\xff\xfe broken", headers={"Content-Type": "text/plain"})

    assert "机密正文" not in both(logs)
    assert any(line.startswith("POST /v1/messages 200 alpha ") for line in request_lines(logs))


def test_an_unauthenticated_request_has_no_project(api, make_logs):
    logs = make_logs()

    response = api.get("/v1/status", as_=None)

    assert response.status_code == 401
    [line] = request_lines(logs)
    assert re.fullmatch(r"GET /v1/status 401 - \d+ms", line)


def test_an_unknown_path_is_logged_without_its_query(api, make_logs):
    logs = make_logs()

    response = api.get("/nope?x=1", as_=None)

    assert response.status_code == 404
    [line] = request_lines(logs)
    assert re.fullmatch(r"GET /nope 404 - \d+ms", line)


def test_a_path_cannot_forge_a_second_log_line(api, make_logs):
    logs = make_logs()

    api.get("/v1/a%0A2026-10-16T00:00:00+08:00 INFO fake line%0D", as_=None)

    assert len(logs.lines) == 1  # 路径里的换行没有把一行变成两行
    assert "\r" not in logs.file and "INFO fake line" in logs.file


def test_a_very_long_path_is_cut_short(api, make_logs):
    logs = make_logs()

    api.get("/" + "a" * 5000, as_=None)

    [line] = request_lines(logs)
    assert len(line) < 300
    assert line.startswith("GET /" + "a" * (LOGGED_PATH_CHARS - 1) + "… 404 ")


def test_a_path_of_exactly_the_limit_is_logged_whole_and_one_more_character_is_cut(api, make_logs):
    assert LOGGED_PATH_CHARS == 200
    logs = make_logs()

    api.get("/" + "a" * (LOGGED_PATH_CHARS - 1), as_=None)
    api.get("/" + "a" * LOGGED_PATH_CHARS, as_=None)

    whole, cut = request_lines(logs)
    assert re.fullmatch(r"GET /a{199} 404 - \d+ms", whole)
    assert re.fullmatch(r"GET /a{199}… 404 - \d+ms", cut)


def test_the_duration_is_logged_in_whole_milliseconds(api, make_logs, monkeypatch):
    ticks = iter([100.0, 102.5004])  # 守门人看两次表：进来一次、处理完一次
    stand_in = types.SimpleNamespace(perf_counter=lambda: next(ticks))
    monkeypatch.setattr(api_module, "time", stand_in)
    logs = make_logs()

    api.get("/v1/status", as_=None)

    [line] = request_lines(logs)
    assert line.endswith(" 401 - 2500ms")


def test_a_request_that_never_got_a_response_is_logged_with_status_0(api, make_logs):
    async def silent(scope, receive, send):
        return None  # 没发任何响应就结束了（比如连接断了）

    logs = make_logs()
    scope = {"type": "http", "method": "GET", "path": "/v1/vanished", "client": ("127.0.0.1", 1)}
    scope[PROJECT_SCOPE_KEY] = "alpha"

    asyncio.run(Guard(silent, api.services)(scope, None, None))

    [line] = request_lines(logs)
    assert re.fullmatch(r"GET /v1/vanished 0 alpha \d+ms", line)
    assert api.services.in_flight.count == 0  # 记进来的也记出去了


def test_a_key_pasted_into_the_path_is_masked(api, make_logs):
    logs = make_logs()

    api.get(f"/v1/{PROJECT_KEY}", as_=None)

    assert PROJECT_KEY not in both(logs)


def test_the_status_during_shutdown_is_logged(api, make_logs):
    logs = make_logs()
    api.services.shutting_down.set()

    api.get("/v1/health", as_=None)

    [line] = request_lines(logs)
    assert re.fullmatch(r"GET /v1/health 503 - \d+ms", line)


def test_a_refused_lan_source_leaves_no_trace_of_the_request(make_api, make_logs):
    api = make_api(tweak(Config(), server={"lan": True}))
    api.hub.ready()
    logs = make_logs()

    refused = api.get("/v1/status?x=1", source=OUTSIDE)
    served = api.get("/v1/status", source=LAN)

    assert refused.status_code == 403 and served.status_code == 200
    assert [line.split(" ")[0:3] for line in request_lines(logs)] == [["GET", "/v1/status", "200"]]
    assert "203.0.113.5" not in both(logs)


def test_every_request_is_one_line_at_info_and_none_below_warning(api, make_logs):
    logs = make_logs(level="WARNING")

    api.fetch("wait=0")

    assert request_lines(logs) == []


# ---------------------------------------------------------------- 收到消息那一行


def test_a_message_for_a_project_is_logged_by_length_and_project_only(rig, caplog):
    rig.bind()
    rig.add("ashare-quant", prefix="量化")
    rig.heartbeat(rig.hub.projects.get("ashare-quant"))
    caplog.set_level(logging.DEBUG, logger="clawbot_hub")

    said = "量化 查一下独一无二的持仓"
    rig.say(said)
    rig.step()

    text = caplog.text
    assert f"收到一条消息，{len(said)} 个字，交给 ashare-quant" in text
    assert "独一无二" not in text and "持仓" not in text


def test_a_message_the_hub_answers_itself_is_logged_the_same_way(rig, caplog):
    rig.bind()
    rig.add("ashare-quant", prefix="量化")
    caplog.set_level(logging.DEBUG, logger="clawbot_hub")

    rig.say("服务 状态")
    rig.step()

    assert "收到一条消息，5 个字，服务自己回复" in caplog.text


def test_a_strangers_message_is_counted_but_not_quoted(rig, caplog):
    rig.bind()
    rig.add("ashare-quant", prefix="量化")
    caplog.set_level(logging.DEBUG, logger="clawbot_hub")

    rig.upstream.push_user_message("陌生人说的独特内容", from_user_id="stranger@im.wechat")
    rig.step()

    assert "丢弃了 1 条" in caplog.text
    assert "独特内容" not in caplog.text


def test_several_strangers_are_counted_in_one_line_for_the_batch(rig, caplog):
    rig.bind()
    rig.add("ashare-quant", prefix="量化")
    caplog.set_level(logging.DEBUG, logger="clawbot_hub")

    for _ in range(3):
        rig.upstream.push_user_message("陌生人说的话", from_user_id="stranger@im.wechat")
    rig.step()

    assert caplog.messages.count("丢弃了 3 条不是使用者本人发的消息（陌生人或机器人）") == 1
    assert "没有对话凭证" not in caplog.text


def test_a_message_without_a_conversation_token_is_counted_not_quoted(rig, caplog):
    rig.bind()
    rig.add("ashare-quant", prefix="量化")
    caplog.set_level(logging.DEBUG, logger="clawbot_hub")

    for _ in range(2):
        rig.upstream.push_user_message("没有凭证的独特内容", context_token=None)
    rig.step()

    assert caplog.messages.count("2 条消息没有对话凭证，不能回复，没有收") == 1
    assert "独特内容" not in caplog.text
    assert "丢弃了" not in caplog.text and "收到一条消息" not in caplog.text


def test_an_ordinary_batch_logs_no_discard_or_missing_token_lines(rig, caplog):
    rig.bind()
    rig.add("ashare-quant", prefix="量化")
    rig.heartbeat(rig.hub.projects.get("ashare-quant"))
    caplog.set_level(logging.DEBUG, logger="clawbot_hub")

    rig.say("量化 你好")
    rig.step()

    assert "丢弃了" not in caplog.text and "没有对话凭证" not in caplog.text
