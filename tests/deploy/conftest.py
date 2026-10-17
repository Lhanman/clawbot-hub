"""部署相关测试共用的夹具：接口层的 `api`、`make_api`、`clock`，收消息线程的 `rig`、`make_rig`。

本目录的测试要同时看"接口层写的日志""收消息线程写的日志"，所以把两处现成的夹具接过来，
测试文件里按名字直接用。
"""

from tests.api.conftest import api, clock, make_api  # noqa: F401 - 夹具，按名字被用到
from tests.poller.conftest import make_rig, rig  # noqa: F401 - 夹具，按名字被用到
