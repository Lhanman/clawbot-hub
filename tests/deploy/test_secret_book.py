"""日志层认得的秘密值（`SecretBook`）：从哪里读、什么时候重读、读到不像样的东西怎么办。

脱敏的整体效果（日志里真的看不到）在 `test_logging.py`；这里钉住它读文件、读环境变量的细节。
规格 `hub-deploy`「日志」：日志里不能有登录凭证和 Bark 设备码。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clawbot_hub.bark import ENV_KEY
from clawbot_hub.logging_setup import SecretBook

TOKEN = "tok-secret-book-0123456789"
DEVICE = "BARKsecretbookdevice01"


def write(path: Path, content: object) -> None:
    raw = content if isinstance(content, bytes) else json.dumps(content).encode("utf-8")
    path.write_bytes(raw)


# ---------------------------------------------------------------- 从哪里读


def test_it_knows_the_token_in_the_account_file_and_the_code_in_bark_json(paths):
    write(paths.account, {"bot_token": TOKEN})
    write(paths.bark, {"key": DEVICE})

    assert set(SecretBook(paths, {}).values()) == {TOKEN, DEVICE}


def test_a_push_address_in_bark_json_gives_the_code_inside_it(paths):
    write(paths.bark, {"key": f"https://bark.example.test/{DEVICE}"})

    assert SecretBook(paths, {}).values() == (DEVICE,)


def test_the_code_in_the_given_environment_is_known_from_the_start(paths):
    assert SecretBook(paths, {ENV_KEY: DEVICE}).values() == (DEVICE,)


def test_a_push_address_in_the_environment_gives_the_code_inside_it(paths):
    book = SecretBook(paths, {ENV_KEY: f"https://bark.example.test/{DEVICE}"})

    assert book.values() == (DEVICE,)


def test_without_an_environment_given_the_process_environment_is_used(paths, monkeypatch):
    monkeypatch.setenv(ENV_KEY, DEVICE)

    assert SecretBook(paths).values() == (DEVICE,)


def test_an_empty_environment_given_means_there_is_no_code_in_it(paths, monkeypatch):
    monkeypatch.setenv(ENV_KEY, DEVICE)  # 进程的环境变量里有，但调用者给的是另一份（空的）

    assert SecretBook(paths, {}).values() == ()


def test_an_environment_value_that_is_not_a_code_is_ignored(paths):
    assert SecretBook(paths, {ENV_KEY: "://"}).values() == ()


def test_nothing_is_known_when_there_is_nothing(paths):
    assert SecretBook(paths, {}).values() == ()


# ---------------------------------------------------------------- 什么时候重读


def test_a_file_is_read_again_only_when_it_changed(paths, monkeypatch):
    write(paths.account, {"bot_token": TOKEN})
    reads: list[str] = []
    real = Path.read_bytes

    def counting(self: Path) -> bytes:
        reads.append(self.name)
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    book = SecretBook(paths, {})
    for _ in range(5):  # 每写一行日志都要问一遍
        book.values()

    assert reads.count("account.json") == 1

    write(paths.account, {"bot_token": TOKEN + "-rebound"})  # 大小变了
    assert TOKEN + "-rebound" in book.values()
    assert reads.count("account.json") == 2
    book.values()
    assert reads.count("account.json") == 2


def test_an_old_value_stays_known_after_the_file_changes(paths):
    write(paths.account, {"bot_token": TOKEN})
    book = SecretBook(paths, {})

    write(paths.account, {"bot_token": TOKEN + "-rebound"})

    assert book.values() == (TOKEN, TOKEN + "-rebound")  # 先看到的在前


def test_a_value_is_remembered_once_however_often_it_is_seen(paths):
    write(paths.account, {"bot_token": TOKEN})
    book = SecretBook(paths, {})
    write(paths.account, {"bot_token": "x"})
    write(paths.account, {"bot_token": TOKEN})  # 又换回来

    assert book.values().count(TOKEN) == 1


def test_a_file_that_appears_later_is_picked_up(paths):
    book = SecretBook(paths, {})
    assert book.values() == ()

    write(paths.bark, {"key": DEVICE})

    assert book.values() == (DEVICE,)


def test_a_file_that_is_gone_leaves_what_was_learned(paths):
    write(paths.account, {"bot_token": TOKEN})
    book = SecretBook(paths, {})

    paths.account.unlink()

    assert book.values() == (TOKEN,)


# ---------------------------------------------------------------- 读到不像样的东西

NOT_AN_ACCOUNT = [
    pytest.param(b"", id="empty"),
    pytest.param(b"not json at all", id="not-json"),
    pytest.param(b"\xff\xfe\x00 not utf-8", id="not-utf-8"),
    pytest.param([], id="list"),
    pytest.param("just a string", id="string"),
    pytest.param(12345, id="number"),
    pytest.param(None, id="null"),
    pytest.param({}, id="no-token"),
    pytest.param({"bot_token": ""}, id="empty-token"),
    pytest.param({"bot_token": None}, id="null-token"),
    pytest.param({"bot_token": 12345}, id="number-token"),
    pytest.param({"bot_token": ["a"]}, id="list-token"),
]


@pytest.mark.parametrize("content", NOT_AN_ACCOUNT)
def test_an_account_file_without_a_usable_token_teaches_nothing(paths, content):
    write(paths.account, content)

    assert SecretBook(paths, {}).values() == ()


NOT_A_BARK_FILE = [
    pytest.param(b"", id="empty"),
    pytest.param(b"not json at all", id="not-json"),
    pytest.param(b"\xff\xfe\x00 not utf-8", id="not-utf-8"),
    pytest.param([], id="list"),
    pytest.param("just a string", id="string"),
    pytest.param({}, id="no-key"),
    pytest.param({"key": ""}, id="empty-key"),
    pytest.param({"key": "   "}, id="blank-key"),
    pytest.param({"key": None}, id="null-key"),
    pytest.param({"key": 12345}, id="number-key"),
    pytest.param({"key": "://"}, id="address-without-a-code"),
]


@pytest.mark.parametrize("content", NOT_A_BARK_FILE)
def test_a_bark_file_without_a_usable_code_teaches_nothing(paths, content):
    write(paths.bark, content)

    assert SecretBook(paths, {}).values() == ()


def test_a_file_that_cannot_be_read_teaches_nothing_and_does_not_break_the_logging(paths):
    paths.account.mkdir()  # 是个目录：stat 得到、读不了

    assert SecretBook(paths, {}).values() == ()
