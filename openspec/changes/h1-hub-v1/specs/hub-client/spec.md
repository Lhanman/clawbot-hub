# Spec Delta

## Purpose

规定项目这一侧怎么接：一个只依赖标准库的单文件 Python 客户端，别的语言直接调 HTTP 接口。客户端只做三件事——把接口包成函数、把错误分清楚、把"至少送达一次"的取话循环写对。**走不走 Bark、怎么写 Bark 末尾那句原因，是项目自己的事**，客户端不替它决定。

## ADDED Requirements

### Requirement: 单文件、只依赖标准库

客户端 MUST 是仓库里的一个文件 `client/clawbot_hub_client.py`，只 import Python 标准库，在 Python 3.9 及以上能用（别的项目的 Python 版本不一定新），可以整份拷进别的项目里。文件里有常量 `CLIENT_VERSION` 和 `API_VERSION`（字符串 `"1"`，与服务健康检查里的 `api` 字段同一种类型），并有类型标注（不用 3.10 以后才有的语法）。

#### Scenario: 没有第三方依赖

- **WHEN** 用一个只有标准库的干净环境（测试里用 `python -I -S` 加最小路径）导入这个文件
- **THEN** 导入成功；文件里所有 `import` 语句涉及的模块都在标准库里（有测试检查）

#### Scenario: 语法兼容 Python 3.9

- **WHEN** 用 3.9 的语法规则检查这个文件（测试里用 `ast.parse(..., feature_version=(3, 9))`）
- **THEN** 通过，没有 `match` 语句，也没有运行时求值的 `X | Y` 类型写法

### Requirement: 接口

客户端 MUST 提供 `HubClient(base_url="http://127.0.0.1:8790", key=None, key_env="CLAWBOT_HUB_KEY", timeout=35.0, retries=1)`，密钥取 `key`，没给就读环境变量 `key_env`，都没有时**第一次调用方法时**抛 `HubAuthError`（不发请求；构造时不抛，因为项目里常有在导入时就构造的全局客户端）。方法：

| 方法 | 对应接口 | 返回 |
|---|---|---|
| `send(text, *, priority="normal", dedupe_key=None, on_closed="fail", timeout=None)` | `POST /v1/messages` | `SendResult` |
| `reply(handle, text, *, awaiting_confirmation=False, confirm_seconds=None)` | `POST /v1/replies` | `SendResult` |
| `inbox(wait=25, max=10)` | `GET /v1/inbox` | `list[Inbound]` |
| `ack(ids)` | `POST /v1/inbox/ack` | 确认了几条（`int`；一次最多 50 个，超过的客户端自动分批） |
| `status()` | `GET /v1/status` | `dict`（原样的 JSON） |
| `health()` | `GET /v1/health` | `dict` |
| `run_inbox(handler, *, stop=lambda: False, wait=25, max=10, sleep=time.sleep, heartbeat_seconds=None)` | 反复调用取话和确认 | 见"至少送达一次的取话循环" |

读超时（socket 的等待时间）按调用不同：`inbox` 取 `max(timeout, wait + 10)`（等 55 秒的取话不会在 35 秒时读超时）；`send`、`reply` 取 `max(timeout, 服务的发送总时限 + 5)`，服务的发送总时限来自 `status()` 返回里的 `service.send_timeout_seconds`（客户端调过 `status()` 就记下，没调过按 30 秒）；其余用 `timeout`。

`SendResult` 是数据类，字段与接口返回一一对应（`status`、`reason_code`、`reason`、`parts`、`parts_sent`、`duplicate`、`id`、`quota`），另有三个属性：`ok`（`status` 是 `sent` 或者 `skipped`）、`needs_fallback`（`not ok`，即调用方应当另走 Bark；服务以后若加了客户端不认识的 `status`，也按要走 Bark 处理）、`maybe_delivered`（`status` 是 `unconfirmed` 或 `partial`，即消息可能已经到了）。`Inbound` 是数据类，字段与取话返回的每一项一一对应。服务返回里多出来的未知字段 MUST 被忽略（不报错）。

#### Scenario: 发送成功

- **WHEN** 对着真实的服务（假上游）调用 `client.send("盘点完成")`
- **THEN** 返回 `SendResult`，`status == "sent"`、`ok` 为真、`needs_fallback` 为假

#### Scenario: 被拒绝不抛异常

- **WHEN** 窗口过期，调用 `client.send(...)`
- **THEN** 返回 `status == "rejected"` 的 `SendResult`，`reason` 是服务给的中文原因，`needs_fallback` 为真；没有异常

#### Scenario: 跳过算没问题

- **WHEN** 窗口过期，调用 `client.send(..., on_closed="skip")`
- **THEN** `status == "skipped"`，`ok` 为真，`needs_fallback` 为假

#### Scenario: 不认识的状态要走兜底

- **WHEN** 服务返回的 `status` 是客户端不认识的 `queued`
- **THEN** `ok` 为假，`needs_fallback` 为真，`maybe_delivered` 为假

#### Scenario: 可能已经到了

- **WHEN** 服务返回 `status` 为 `unconfirmed` 或 `partial`
- **THEN** `needs_fallback` 和 `maybe_delivered` 都为真

#### Scenario: 没有密钥

- **WHEN** 没给 `key`，环境变量也没设，先构造 `HubClient()`，再调用 `send`
- **THEN** 构造成功；第一次调用时抛 `HubAuthError`，没有发出任何请求

#### Scenario: 服务多返回了字段

- **WHEN** 服务的返回里多了客户端不认识的字段
- **THEN** 客户端照常工作，不报错

#### Scenario: 取话的读超时跟着 wait 走

- **WHEN** 调用 `client.inbox(wait=55)`，`timeout` 是默认的 35 秒，服务在第 50 秒才返回空列表
- **THEN** 返回空列表，没有读超时

#### Scenario: 发送的读超时跟着服务的总时限走

- **WHEN** `status()` 返回的 `service.send_timeout_seconds` 是 60，随后调用 `send`，服务第 50 秒才回话
- **THEN** 客户端等到了结果（读超时按 65 秒）

#### Scenario: 一次确认超过 50 条

- **WHEN** 调用 `client.ack` 传入 120 个 `id`
- **THEN** 分成 50、50、20 三次请求，返回的是三次确认数之和

### Requirement: 错误分得清

客户端的异常 MUST 继承 `HubError`，并分成：

- `HubUnavailable`：连不上服务、连接超时、服务返回 5xx（含 503 `shutting_down`）。项目遇到它就应该走 Bark。属性 `possibly_sent`：只对发消息和回复的请求有意义，**连接已经建立之后**才出的错（读超时、连接被重置或断开）和服务返回 500 `internal` 时为真——这时不知道服务有没有发出（500 时前几条可能已经发出），**不要当作没发**；连不上、连接超时、502、503（含 `shutting_down`）、504 时为假，那些一定没发出去。
- `HubAuthError`：401 / 403（密钥不对、项目停用、只推送的项目调了取话）。属性 `code`（`unauthorized`、`project_disabled`、`push_only`；局域网规则拒绝的 403 没有正文，`code` 为 `None`）。
- `HubRequestError`：其他 4xx（400、404、410、413、429）。属性 `code`（如 `bad_request`、`text_too_long`、`unknown_handle`、`handle_expired`、`rate_limited`）、`message`、`retry_after`（429 时有）。

`send`、`reply` 对 `rejected`、`failed`、`unconfirmed`、`partial`、`skipped` MUST NOT 抛异常（那是业务结果，不是错误）。注意 `HubRequestError.code`（HTTP 错误）和 `SendResult.reason_code`（业务结果）是两套，不混用。

#### Scenario: 服务没开

- **WHEN** 服务没在运行，调用 `send`
- **THEN** 抛 `HubUnavailable`，`possibly_sent` 为假

#### Scenario: 发送途中读超时

- **WHEN** 服务收到请求但很久不回话，直到客户端读超时
- **THEN** 抛 `HubUnavailable`，`possibly_sent` 为真

#### Scenario: 连接被对端重置

- **WHEN** 服务收到发送请求后直接断开连接，没回任何话
- **THEN** 抛 `HubUnavailable`，`possibly_sent` 为真

#### Scenario: 服务内部出错

- **WHEN** 服务对发送请求回 500 `internal`
- **THEN** 抛 `HubUnavailable`，`possibly_sent` 为真

#### Scenario: 服务正在关闭

- **WHEN** 服务回 503 `shutting_down`
- **THEN** 抛 `HubUnavailable`，`possibly_sent` 为假

#### Scenario: 限速

- **WHEN** 服务回 429 `rate_limited`，`Retry-After: 12`
- **THEN** 抛 `HubRequestError`，`code == "rate_limited"`，`retry_after == 12`

#### Scenario: 文字太长

- **WHEN** 服务回 400 `text_too_long`
- **THEN** 抛 `HubRequestError`，`code == "text_too_long"`，和业务结果的 `reason_code` 不混

#### Scenario: 停用的项目

- **WHEN** 服务回 403 `project_disabled`
- **THEN** 抛 `HubAuthError`，`code == "project_disabled"`

### Requirement: 不偷偷重发

`send` 和 `reply` MUST NOT 自动重试，除非调用方给了 `dedupe_key`（`reply` 没有去重键，永远不自动重试）：给了去重键时，**只在确定请求没被处理**的情形下重试——连不上、连接超时、502、503、504——最多 `retries`（构造函数的参数，默认 1）次，间隔 1 秒；读超时、连接被重置和 500（`possibly_sent`）一律不重试。`inbox`、`ack`、`status`、`health` 可以安全重试，任何 `HubUnavailable` 都按同样的次数和间隔重试。

#### Scenario: 没有去重键不重试

- **WHEN** 服务第一次回 503，调用 `send("…")`（没给 `dedupe_key`）
- **THEN** 只发出一次请求，抛 `HubUnavailable`

#### Scenario: 有去重键可以重试

- **WHEN** 服务第一次回 503，第二次成功，调用 `send("…", dedupe_key="k")`
- **THEN** 发出两次请求，都带 `dedupe_key: "k"`，最终返回 `sent`

#### Scenario: 可能已发的不重试

- **WHEN** 调用 `send("…", dedupe_key="k")`，第一次请求读超时
- **THEN** 只发出一次请求，抛 `HubUnavailable`，`possibly_sent` 为真

#### Scenario: 重试次数可调

- **WHEN** 构造 `HubClient(retries=0)`，服务回 503，调用 `send("…", dedupe_key="k")`；另一个客户端 `retries=2`，服务连续回 503 两次再成功
- **THEN** 前者只发一次请求就抛 `HubUnavailable`；后者发三次请求，最终 `sent`

### Requirement: 至少送达一次的取话循环

`HubClient.run_inbox(handler, *, stop=lambda: False, wait=25, max=10, sleep=time.sleep, heartbeat_seconds=None)` MUST 把"取话、处理、确认、连不上时退避"这个循环写对，项目不用自己再写一遍。每个项目同一时间只能有一个取话循环（服务只留一个正在等的取话请求，两个循环会互相顶掉）。

- 循环取话；对每一项调用 `handler(item)`，`handler` **正常返回**才把这一项的 `id` 加进确认列表，处理完这一批以后统一调 `ack`；`handler` 抛异常的那一项不确认（记日志，继续处理这一批里后面的），租期过后服务会再给一次；
- **心跳**：`handler` 运行期间，客户端 MUST 每 `heartbeat_seconds` 秒调一次 `GET /v1/inbox?wait=0&max=0`，让服务知道项目还活着（处理一句慢指令超过 90 秒也不会被判成"没在线"）。`heartbeat_seconds` 不传（`None`）时取 `inbox_online_seconds / 3`（`inbox_online_seconds` 来自 `status()` 的 `service.inbox_online_seconds`，开始循环时取一次，取不到按 90，所以默认是 30 秒）。`handler` 在调用方的线程里运行，心跳由客户端自己起的**后台线程**发：每个 `handler` 开始前启动、结束后（含抛异常）停止，线程用 `threading.Event.wait` 计时，不用 `sleep` 参数；`heartbeat_seconds` 是留给测试用的，传一个很小的数就能在几百毫秒里测完。心跳失败只记日志，不影响处理；
- 连不上服务（`HubUnavailable`）时退避：5 秒起每次翻倍、最多 60 秒，恢复后重新计；
- `ack` 失败（`HubUnavailable`）时不能丢掉这一批的待确认 `id`：用同样的退避只重试 `ack`，成功之后才继续取话（服务保证确认是幂等的）；
- `HubAuthError` 时抛出（密钥不对或项目被停用，重试没有用）；`HubRequestError` 中 429 睡 `retry_after` 秒（没有就 60 秒）后继续，其余抛出；
- `stop()` 为真时在两次取话之间退出（当前这一批处理完并确认）；
- 一次耗时不到 `wait / 2` 秒的空列表（`wait=0` 时一律，被同一项目的另一个取话请求顶掉和服务正在关闭时也会很快返回）睡 1 秒再取，免得空转；服务已经等满的空列表（耗时不少于 `wait / 2`）不算错误，也不睡觉。

#### Scenario: 处理失败的不确认

- **WHEN** 一批 3 句话，处理第 2 句时 `handler` 抛异常
- **THEN** 确认的是第 1、3 句；第 2 句在租期后又出现，`attempt` 为 2

#### Scenario: 服务重启时退避

- **WHEN** 服务连续 3 次连不上，之后恢复
- **THEN** 退避时间依次是 5、10、20 秒，恢复后取话正常，下一次再失败又从 5 秒开始

#### Scenario: 密钥被停用

- **WHEN** 取话时服务回 403 `project_disabled`
- **THEN** `run_inbox` 抛 `HubAuthError`，不再重试

#### Scenario: 处理慢指令时发心跳

- **WHEN** 测试里把 `heartbeat_seconds` 设成 0.05，`handler` 处理一句话阻塞 0.3 秒
- **THEN** 处理期间服务收到至少 3 次 `GET /v1/inbox?wait=0&max=0`；`handler` 一结束心跳就停（之后 0.3 秒内不再有心跳请求）；`handler` 抛异常时同样停

#### Scenario: 心跳间隔的默认值

- **WHEN** 不传 `heartbeat_seconds`，`status()` 返回的 `service.inbox_online_seconds` 是 90；另一次取不到 `status()`
- **THEN** 前一次的心跳间隔是 30 秒，后一次按 90 秒算也是 30 秒（直接检查算出来的间隔，不用真等）

#### Scenario: 慢指令期间保持在线

- **WHEN** 项目的 `handler` 处理一句话用了比 `inbox.online_seconds` 还长的时间（测试里把 `inbox.online_seconds` 配成 1 秒、`heartbeat_seconds` 设成 0.2、`handler` 阻塞 3 秒），期间使用者又发了一句话
- **THEN** 服务一直认为这个项目在线，第二句话正常入队，使用者没有收到"没在线"的提醒

#### Scenario: 确认失败不丢

- **WHEN** 处理完一批后 `ack` 连续两次连不上，第三次成功
- **THEN** 同一批的 `id` 一直保留，退避 5、10 秒，第三次确认成功，之后才取下一批；服务那边没有重复交出这批话

#### Scenario: 限速时睡一会儿

- **WHEN** 服务回 429 `rate_limited`，`Retry-After: 12`
- **THEN** `run_inbox` 睡 12 秒后继续；其他 4xx（比如 400）则抛出

#### Scenario: 空转保护

- **WHEN** 取话立刻返回空列表（`wait=0`，或者被另一个取话请求顶掉）
- **THEN** 两次取话之间至少睡 1 秒（`wait=0` 时每次都睡）；服务等满 25 秒才返回的空列表，下一次取话前不睡

#### Scenario: 请求停止

- **WHEN** 一批 2 句话处理到一半，`stop()` 变成真
- **THEN** 这一批处理完并确认，之后不再取话，`run_inbox` 返回

### Requirement: 接入指南和对接约定

仓库 MUST 带 `docs/clients/接入指南.md`，写清楚一个项目接入的步骤和惯例，让别的项目不用再读服务的代码：登记（`hubctl project add`）、放密钥（环境变量或 `.env`，不进 git）、发消息、取话和回复、**把"微信发不出去"接到自己的 Bark 兜底上**。其中对接约定写明：

- `sent`、`skipped` 算微信这边没问题；`unconfirmed`、`partial`、`rejected`、`failed` 以及 `HubUnavailable` 都应当走 Bark，并把 `reason` 写进 Bark 的末尾（ashare-quant 现在就是"微信没发出去：……"）；
- `unconfirmed`、`partial` 和 `HubUnavailable(possibly_sent=True)` 的原因里要说明"可能已经到了"；
- 推送用稳定的 `dedupe_key`（比如 `brief-2026-10-12`），这样重跑不会发两份；
- 取话处理要幂等；回复用取话给的句柄，需要等使用者确认的回复带 `awaiting_confirmation=True`；`via` 是 `confirm` 的话，由项目自己判断这句话是不是它要的确认，不是就当普通话处理；
- 处理一句话可能超过 `online_seconds`（默认 90 秒）的项目用 `run_inbox`（它会发心跳）；自己写取话循环的项目要自己发心跳（`GET /v1/inbox?wait=0&max=0`）；每个项目同一时间只能有一个取话循环；
- 取话的前缀由使用者在登记时定；服务分不清默认项目自己的指令里有没有和别的项目前缀同名的词，登记前缀时请自己确认；
- 不要自己向使用者的微信发任何别的东西，也不要保存回复句柄超过 30 分钟。

另带一份 `docs/api/v1.md`，用 `curl` 示例写出每个接口的请求、返回、错误码，给不用 Python 的项目看；以及从服务导出的 `docs/api/openapi.json`（用应用对象的 `openapi()` 导出，服务本身不开 `/docs` 之类的页面），有测试检查它和服务实际的接口一致（接口改了没更新文档，测试就失败）。

#### Scenario: 文档和接口一致

- **WHEN** 运行测试
- **THEN** 从服务生成的 OpenAPI 描述与 `docs/api/openapi.json` 相同；不同时测试失败并提示重新导出

#### Scenario: 接入指南里的例子能跑

- **WHEN** 测试把 `docs/api/v1.md` 里标记为可执行的 `curl` 示例对着测试服务运行
- **THEN** 返回和文档写的一致（只比字段，不比 `id` 这类随机值）

#### Scenario: 文档里的时间互相对得上

- **WHEN** 测试读取 `docs/api/v1.md` 里取话示例的 `at` 和 `at_ms`
- **THEN** 两者是同一个时刻

#### Scenario: 指南里有这些约定

- **WHEN** 测试检查 `docs/clients/接入指南.md`
- **THEN** 其中提到了 `run_inbox` 与心跳、`via` 为 `confirm` 的处理、`dedupe_key`、走 Bark 兜底、前缀与默认项目指令同名的提醒

### Requirement: 合约测试

仓库 MUST 有一组合约测试 `tests/contract/`：用真正的 HTTP 服务（本机回环上的随机端口，假上游）加上单文件客户端，把"项目这一侧能用到的全部行为"跑一遍——发消息的每种状态、回复、取话的租约和确认、等确认的路由、停用和换钥、限速、错误映射、`retries`、读超时。ashare-quant 或别的项目以后改成走服务时，合约测试就是它们和服务之间的约定；服务升级前这组测试必须全过。

#### Scenario: 合约测试覆盖接口

- **WHEN** 运行 `pytest tests/contract`
- **THEN** `docs/api/v1.md` 里的每个接口至少有一个对应的合约测试通过（有测试检查接口清单和测试的对应关系）
