"""保底合计（任务 D3）：`hubctl status` 的提示和 `hubctl doctor` 共用的算法。

已停用的项目不占保底；合计加回复保留超过窗口总条数就是"超了"，刚好等于不算。
"""

from __future__ import annotations

from collections import namedtuple

from clawbot_hub.config import Config, parse_config
from clawbot_hub.projects import GuaranteeTotal, guarantee_total

Holder = namedtuple("Holder", "name guaranteed enabled")


def config(*, window_limit: int = 10, reply_reserve: int = 2) -> Config:
    return parse_config({"quota": {"window_limit": window_limit, "reply_reserve": reply_reserve}})


def test_only_enabled_projects_count_and_the_listing_names_them_in_order():
    holders = [Holder("seed", 6, True), Holder("old", 9, False), Holder("beta", 5, True)]

    result = guarantee_total(holders, config())

    assert result == GuaranteeTotal(total=11, reserve=2, limit=10, listing="seed 6、beta 5")


def test_the_excess_is_what_goes_over_the_window_after_the_reserve():
    assert guarantee_total([Holder("a", 6, True), Holder("b", 5, True)], config()).excess == 3


def test_exactly_filling_the_window_is_not_an_excess():
    result = guarantee_total([Holder("a", 8, True)], config())

    assert (result.total + result.reserve, result.limit) == (10, 10)
    assert result.excess == 0


def test_no_projects_is_an_empty_listing_and_no_excess():
    result = guarantee_total([], config())

    assert (result.total, result.listing, result.excess) == (0, "", 0)


def test_the_limits_come_from_the_configuration():
    result = guarantee_total([Holder("a", 3, True)], config(window_limit=4, reply_reserve=2))

    assert (result.reserve, result.limit, result.excess) == (2, 4, 1)
