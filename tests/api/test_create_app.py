"""最小装配 `create_app`：把部件接到同一个数据库、时钟和上游上，给出能收请求的应用；
`server_config` 给出和它配套的 uvicorn 设置。规格：`hub-deploy`「谁能连」。
"""

from __future__ import annotations

import warnings

import pytest

from clawbot_hub.account import AccountFile
from clawbot_hub.app import create_app, server_config
from clawbot_hub.config import Config
from clawbot_hub.sender import CONFIRMED
from clawbot_hub.services import Hooks
from clawbot_hub.upstream.base import Rejected
from tests.projects.conftest import register
from tests.support.hub import BOT_TOKEN, USER, tweak

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient


def prepare(app, clock):
    """绑定、使用者说一句话、登记项目 alpha；返回它的密钥。全部通过应用自己的部件做。"""
    services = app.state.services
    services.store.write(
        AccountFile(
            bot_token=BOT_TOKEN,
            user_id=USER,
            base_url="https://ilink.example",
            bot_id="bot@im.bot",
            bound_at="2026-10-12T10:00:00+08:00",
            updates_buf="",
        )
    )
    services.monitor.sync()
    with services.db.txn() as conn:
        services.quota.open_window(conn, clock.now_ms(), "ctx-1")
        conn.execute("UPDATE account_state SET creds_changed_ms = NULL WHERE id = 1")
    return register(services.projects, "alpha", guaranteed=6, cap=8).key


@pytest.fixture
def make_app(paths, database, clock, fake_upstream):
    def build(config=None, hooks=None):
        return create_app(
            config or Config(), fake_upstream, clock, paths=paths, db=database, hooks=hooks
        )

    return build


def post(app, key, text="你好", source="127.0.0.1"):
    with TestClient(app, client=(source, 50000)) as client:
        return client.post(
            "/v1/messages", json={"text": text}, headers={"Authorization": f"Bearer {key}"}
        )


def test_the_parts_share_one_database_one_clock_and_one_upstream(make_app, clock, fake_upstream):
    app = make_app()
    key = prepare(app, clock)

    response = post(app, key)

    assert response.status_code == 200
    assert response.json()["status"] == "sent"
    assert fake_upstream.sent_texts() == ["你好"]


def test_the_parts_are_on_the_app_for_whoever_runs_it(make_app, database, paths, clock):
    app = make_app()
    services = app.state.services

    assert services.config == Config()
    assert services.db is database and services.paths is paths and services.clock is clock
    assert services.sender is not None and services.projects is not None
    assert services.limits is not None
    assert not services.shutting_down.is_set()


def test_only_the_v1_interface_is_open(make_app):
    app = make_app()

    assert app.docs_url is None and app.redoc_url is None and app.openapi_url is None
    assert app.router.redirect_slashes is False


def test_the_configuration_reaches_the_parts(make_app, clock):
    app = make_app(tweak(Config(), send={"max_chars": 300, "part_chars": 200}))
    key = prepare(app, clock)

    assert post(app, key, "字" * 300).json()["status"] == "sent"
    assert post(app, key, "字" * 301).status_code == 400


def test_the_hooks_reach_the_sender_and_the_login_monitor(make_app, clock, fake_upstream):
    kinds, holds, expired, restored = [], [], [], []
    app = make_app(
        hooks=Hooks(
            on_expired=expired.append,
            on_restored=restored.append,
            on_hold=holds.append,
            on_request=kinds.append,
        )
    )
    key = prepare(app, clock)
    services = app.state.services

    post(app, key)
    fake_upstream.script("send_text", Rejected(200, 40001, "bad token"))
    post(app, key)
    services.monitor.session_expired(services.monitor.account().digest)
    services.store.write(
        AccountFile(
            bot_token="tok-bot-secret-2",
            user_id=USER,
            base_url="https://ilink.example",
            bot_id="bot@im.bot",
            bound_at="2026-10-12T11:00:00+08:00",
            updates_buf="",
        )
    )
    services.monitor.sync()

    assert kinds == [CONFIRMED]  # 被腾讯明确拒绝的那次不算
    assert [hold.code for hold in holds] == ["40001"]
    assert (expired, restored) == ([1], [1])


def test_no_hooks_is_fine(make_app, clock):
    app = make_app()
    key = prepare(app, clock)

    assert post(app, key).status_code == 200


def test_shutting_down_stops_new_requests_and_new_sends(make_app, clock):
    app = make_app()
    key = prepare(app, clock)
    services = app.state.services

    services.begin_shutdown()

    assert post(app, key).status_code == 503
    assert services.shutting_down.is_set()
    result = services.sender.push(services.projects.get("alpha"), "直接调发送层")
    assert (result.status, result.reason_code) == ("failed", "busy")


# ---------------------------------------------------------------- uvicorn 的设置


def test_the_server_listens_on_the_loopback_by_default(make_app):
    app = make_app()

    settings = server_config(app, Config())

    assert (settings.host, settings.port) == ("127.0.0.1", 8790)


def test_the_lan_switch_listens_on_every_address(make_app):
    config = tweak(Config(), server={"lan": True, "port": 9001})

    settings = server_config(make_app(config), config)

    assert (settings.host, settings.port) == ("0.0.0.0", 9001)  # noqa: S104 - 局域网开关的本意


def test_a_port_can_be_chosen_by_the_caller(make_app):
    assert server_config(make_app(), Config(), port=0).port == 0


def test_the_server_does_not_believe_forwarding_headers_and_stays_quiet(make_app):
    settings = server_config(make_app(), Config())

    assert settings.proxy_headers is False  # 来源只看连接的对端
    assert settings.server_header is False
    assert settings.access_log is False  # 请求日志自己记，不含查询串
    assert settings.log_config is None  # 日志的设置归阶段 D
    assert settings.lifespan == "off"
