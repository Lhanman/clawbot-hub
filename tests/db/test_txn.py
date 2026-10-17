"""事务（任务 A3）：写事务一律 `BEGIN IMMEDIATE`，两个线程并发写不丢，出错整个回滚。"""

from __future__ import annotations

import sqlite3

import pytest

from clawbot_hub.db import Database
from tests.support.threads import run_in_thread


def counter(db: Database) -> int:
    return int(db.conn().execute("SELECT value FROM meta WHERE key = 'counter'").fetchone()[0])


@pytest.fixture
def counting(database: Database) -> Database:
    with database.txn() as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES ('counter', '0')")
    return database


def test_a_transaction_commits_when_the_block_finishes(counting):
    with counting.txn() as conn:
        conn.execute("UPDATE meta SET value = '5' WHERE key = 'counter'")
    assert counter(counting) == 5
    assert not counting.conn().in_transaction


def test_the_block_gets_the_connection_of_the_calling_thread(counting):
    with counting.txn() as conn:
        assert conn is counting.conn()


def test_an_error_rolls_everything_back_and_is_raised_again(counting):
    with pytest.raises(ZeroDivisionError):
        with counting.txn() as conn:
            conn.execute("UPDATE meta SET value = '5' WHERE key = 'counter'")
            1 / 0  # noqa: B018
    assert counter(counting) == 0
    assert not counting.conn().in_transaction


def test_even_an_interrupt_rolls_the_transaction_back(counting):
    with pytest.raises(KeyboardInterrupt):
        with counting.txn() as conn:
            conn.execute("UPDATE meta SET value = '5' WHERE key = 'counter'")
            raise KeyboardInterrupt
    assert counter(counting) == 0
    assert not counting.conn().in_transaction


def test_a_failing_statement_rolls_back_the_earlier_ones_too(counting):
    with pytest.raises(sqlite3.IntegrityError):
        with counting.txn() as conn:
            conn.execute("UPDATE meta SET value = '7' WHERE key = 'counter'")
            conn.execute("INSERT INTO meta (key, value) VALUES ('counter', 'dup')")
    assert counter(counting) == 0


def test_a_transaction_that_sqlite_already_rolled_back_still_raises_the_original_error(counting):
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        with counting.txn() as conn:
            conn.execute("UPDATE meta SET value = '7' WHERE key = 'counter'")
            # OR ROLLBACK：冲突时 SQLite 自己把整个事务回滚了，我们不能再回滚一次把原来的错误盖掉
            conn.execute("INSERT OR ROLLBACK INTO meta (key, value) VALUES ('counter', 'dup')")
    assert counter(counting) == 0
    assert not counting.conn().in_transaction


def test_a_commit_that_fails_is_rolled_back_too(counting):
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        with counting.txn() as conn:
            conn.execute("PRAGMA defer_foreign_keys = ON")  # 外键到提交时才检查
            conn.execute("UPDATE meta SET value = '3' WHERE key = 'counter'")
            conn.execute(
                "INSERT INTO outbox (id, project_id, kind, parts, status, created_ms) "
                "VALUES ('x', 999, 'push', 1, 'sent', 1)"
            )
    assert not counting.conn().in_transaction
    assert counter(counting) == 0
    assert counting.conn().execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_transactions_do_not_nest(counting):
    with counting.txn():
        with pytest.raises(RuntimeError):
            with counting.txn():
                pass
        assert counting.conn().in_transaction  # 外层的事务没被破坏
    assert not counting.conn().in_transaction


def test_a_reader_never_sees_uncommitted_changes(counting):
    with counting.txn() as conn:
        conn.execute("UPDATE meta SET value = '9' WHERE key = 'counter'")
        assert run_in_thread(counter, counting).result() == 0
    assert run_in_thread(counter, counting).result() == 9


def test_begin_immediate_makes_a_second_writer_wait_even_before_the_first_one_writes(
    paths, database
):
    impatient = Database(paths.db, busy_timeout_ms=150)

    def try_to_write():
        with impatient.txn():
            pass

    try:
        with database.txn():  # 拿着写锁，一个字都还没写
            second = run_in_thread(try_to_write)
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                second.result()
    finally:
        impatient.close()


def test_a_writer_waits_for_the_lock_and_then_goes_ahead(counting):
    import threading

    holding = threading.Event()
    release = threading.Event()

    def hold():
        with counting.txn() as conn:
            conn.execute("UPDATE meta SET value = '1' WHERE key = 'counter'")
            holding.set()
            release.wait(5)

    first = run_in_thread(hold)
    assert holding.wait(5)

    def second_writer():
        with counting.txn() as conn:
            value = int(conn.execute("SELECT value FROM meta WHERE key = 'counter'").fetchone()[0])
            conn.execute("UPDATE meta SET value = ? WHERE key = 'counter'", (str(value + 10),))

    second = run_in_thread(second_writer)
    assert second.still_blocked(0.2)  # 第一个事务还没提交，第二个在等
    release.set()
    first.result()
    second.result()
    assert counter(counting) == 11  # 第二个读到的是第一个提交以后的值


def test_two_threads_incrementing_the_same_row_lose_nothing(counting):
    def work():
        for _ in range(150):
            with counting.txn() as conn:
                value = int(
                    conn.execute("SELECT value FROM meta WHERE key = 'counter'").fetchone()[0]
                )
                conn.execute("UPDATE meta SET value = ? WHERE key = 'counter'", (str(value + 1),))

    workers = [run_in_thread(work) for _ in range(2)]
    for worker in workers:
        worker.result(60)
    assert counter(counting) == 300


def test_close_closes_the_connections_of_every_thread(database):
    mine = database.conn()
    other = run_in_thread(database.conn).result()
    database.close()
    for conn in (mine, other):
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")


def test_a_closed_database_hands_out_nothing_more(database):
    database.conn()  # 这个线程已经有自己的连接了，关闭以后也不再给
    database.close()
    with pytest.raises(RuntimeError):
        database.conn()
    with pytest.raises(RuntimeError):
        run_in_thread(database.conn).result()  # 还没有连接的线程也一样
    database.close()  # 再关一次没关系


def test_a_thread_that_loses_the_race_with_close_gets_no_connection_that_would_leak(
    database, monkeypatch
):
    """白盒：线程过了"是否已关闭"的第一道检查，等锁的时候别的线程把数据库关了；
    拿到锁以后必须再查一次，否则会在关闭之后又建出一个没人来关的连接。"""
    real_lock = database._lock

    class ClosesWhileYouWait:
        def __enter__(self):
            monkeypatch.setattr(database, "_lock", real_lock)
            database.close()
            return real_lock.__enter__()

        def __exit__(self, *exc):
            return real_lock.__exit__(*exc)

    monkeypatch.setattr(database, "_lock", ClosesWhileYouWait())
    with pytest.raises(RuntimeError):
        database.conn()
    assert database._open == []


def test_the_connection_of_a_finished_thread_is_closed_when_the_next_one_is_made(database):
    mine = database.conn()  # 主线程还活着，它的连接不能被关
    first = run_in_thread(database.conn)
    gone = first.result()
    first.join()
    gone.execute("SELECT 1")  # 线程刚结束，还没人来清理

    run_in_thread(database.conn).result()  # 新线程要连接，顺手把已结束线程的关掉

    with pytest.raises(sqlite3.ProgrammingError):
        gone.execute("SELECT 1")
    mine.execute("SELECT 1")
    assert database.conn() is mine
