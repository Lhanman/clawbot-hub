"""`Alerts.hooks()`：接到服务装配（`services.assemble`）上的四个口子。

装配把这四个回调分别交给登录监视器（失效、恢复）和发送层（腾讯拒绝后的暂停、每个请求有没有
确认送达）；装配本身怎么传在 `tests/api/test_create_app.py` 里测，这里只测"口子接的是告警自己
对应的那个判定"，不会张冠李戴。
"""

from __future__ import annotations


def test_the_four_hooks_are_the_alerts_own_handlers(rig):
    hooks = rig.alerts.hooks()

    assert hooks.on_expired == rig.alerts.on_expired
    assert hooks.on_restored == rig.alerts.on_restored
    assert hooks.on_hold == rig.alerts.on_hold
    assert hooks.on_request == rig.alerts.on_request
