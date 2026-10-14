"""服务自己的回复：有上限的内存队列和一个发送线程（任务 C3，规格 `hub-inbox`「服务自己的指令
和提示」）。

收消息的线程把要回的话放进队列就继续收，不等它发完；发送线程按放进去的先后一条一条发，走
`Sender.reply`（所以标签 `【服务】`、条数记在内部名字 `_hub` 名下的"回复"一类、发送锁、窗口和
登录状态的检查都是发送器的）。

**尽力而为、至多一次**：

- 队列只在内存里（`REPLY_QUEUE_MAX` 条，满了丢最旧的），服务在放进队列之后、发出之前被杀，这条
  回复丢了不补；话本身已经按规则入队或处理，没有丢；
- 一条回复只试一次：发不出去（窗口已过期、登录失效、腾讯拒绝、出错）就算了，不重试、不告警——
  补收到的旧消息触发的回复发不出去是常事；
- 关闭时（`stop`）正在发的那一条让它发完，队列里剩下的不发。

日志只写错误的类型，不写回复的文字和对话凭证。
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field

from clawbot_hub.sender import Origin, Sender

logger = logging.getLogger(__name__)

# 回复队列的长度（固定常量）：使用者一个人发话，同时攒下这么多回复不会是正常情况
REPLY_QUEUE_MAX = 20

# 记在内部名字 `_hub` 名下（`hub-projects`）
HUB_NAME = "_hub"

# `stop` 最多等发送线程多久（真实秒）：一条发送最长 `send.timeout_seconds`，比它长就不等了
JOIN_SECONDS = 60.0


@dataclass(frozen=True)
class Outgoing:
    """一条要回的话：用 `context_token`（使用者那句话的）发 `text`。`repr` 里不写任何一项。"""

    context_token: str = field(repr=False)
    text: str = field(repr=False)


class Replier:
    def __init__(self, sender: Sender, label: str) -> None:
        self._sender = sender
        self._author = Origin(HUB_NAME, label)
        self._cond = threading.Condition()
        self._queue: deque[Outgoing] = deque()
        self._thread: threading.Thread | None = None
        self._stopping = False

    # ------------------------------------------------ 队列

    def submit(self, outgoing: Outgoing) -> int:
        """放进队列，返回为它挤掉了几条（队列满了挤最旧的；没满是 0）。"""
        with self._cond:
            dropped = 0
            while len(self._queue) >= REPLY_QUEUE_MAX:
                self._queue.popleft()
                dropped += 1
            self._queue.append(outgoing)
            self._cond.notify()
        if dropped:
            logger.warning("服务回复的队列满了，丢掉最旧的 %d 条", dropped)
        return dropped

    @property
    def pending(self) -> int:
        with self._cond:
            return len(self._queue)

    def process_one(self) -> bool:
        """发队首的一条（没有就返回假）。发不出去、出错都只记日志，不抛出，也不重试。"""
        with self._cond:
            if not self._queue:
                return False
            outgoing = self._queue.popleft()
        try:
            self._sender.reply(self._author, outgoing.text, context_token=outgoing.context_token)
        except Exception as exc:  # noqa: BLE001 - 回复尽力而为，出错不能连累发送线程
            logger.error("服务回复发送出错：%s", type(exc).__name__)
        return True

    # ------------------------------------------------ 线程

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._cond:
            if self.running:
                raise RuntimeError("服务回复的发送线程已经在运行")
            self._stopping = False
            self._thread = threading.Thread(target=self._run, name="hub-replier", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = JOIN_SECONDS) -> None:
        """停下发送线程：正在发的那一条发完，队列里剩下的丢掉。没启动、重复停都没关系。"""
        with self._cond:
            self._stopping = True
            self._queue.clear()
            self._cond.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._stopping:
                    self._cond.wait()
                if self._stopping:
                    return
            self.process_one()
