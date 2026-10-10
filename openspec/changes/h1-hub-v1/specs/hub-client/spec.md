# Spec Delta

## Purpose

规定项目这一侧怎么接：一个只依赖标准库的单文件 Python 客户端，别的语言直接调 HTTP 接口。客户端只做三件事——把接口包成函数、把错误分清楚、把"至少送达一次"的取话循环写对。**走不走 Bark、怎么写 Bark 末尾那句原因，是项目自己的事**，客户端不替它决定。

## ADDED Requirements

### Requirement: 单文件、只依赖标准库

客户端 MUST 是仓库里的一个文件 `client/clawbot_hub_client.py`，只 import Python 标准库，在 Python 3.9 及以上能用（别的项目的 Python 版本不一定新），可以整份拷进别的项目里。文件里有常量 `CLIENT_VERSION` 和 `API_VERSION`（= 1），并有类型标注。

#### Scenario: 没有第三方依赖

- **WHEN** 用一个只有标准库的干净环境（测试里用 `python -I -S` 加最小路径）导入这个文件
- **THEN** 导入成功；文件里所有 `import` 语句涉及的模块都在标准库里（有测试检查）

### Requirement: 接口

客户端 MUST 提供 `HubClient(base_url="http://127.0.0.1:8790", key=None, key_env="CLAWBOT_HUB_KEY", timeout=35.0)`，密钥取 `key`，没给就读环境变量 `key_env`，都没有时抛 `HubAuthError`（不发请求）。方法：

| 方法 | 对应接口 | 返回 |
|---|---|---|
| `send(text, *, priority="normal", dedupe_key=None, on_closed="fail", timeout=None)` | `POST /v1/messages` | `SendResult` |
| `reply(handle, text, *, awaiting_confirmation=False, confirm_seconds=None)` | `POST /v1/replies` | `SendResult` |
| `inbox(wait=25, max=10)` | `GET /v1/inbox` | `list[Inbound]` |
| `ack(ids)` | `POST /v1/inbox/ack` | 确认了几条（`int`） |
| `status()` | `GET /v1/status` | `dict`（原样的 JSON） |
| `health()` | `GET /v1/health` | `dict` |

`SendResult` 是数据类，字段与接口返回一一对应（`status`、`reason_code`、`reason`、`parts`、`parts_sent`、`duplicate`、`id`、`quota`），另有两个属性：`ok`（`status` 是 `sent` 或者 `skipped`）和 `needs_fallback`（`status` 是 `unconfirmed`、`partial`、`rejected`、`failed` 之一，即调用方应当另走 Bark）。`Inbound` 是数据类，字段与取话返回的每一项一一对应。服务返回里多出来的未知字段 MUST 被忽略（不报错）。

#### Scenario: 发送成功

- **WHEN** 对着真实的服务（假上游）调用 `client.send("盘点完成")`
- **THEN** 返回 `SendResult`，`status == "sent"`、`ok` 为真、`needs_fallback` 为假

#### Scenario: 被拒绝不抛异常

- **WHEN** 窗口过期，调用 `client.send(...)`
- **THEN** 返回 `status == "rejected"` 的 `SendResult`，`reason` 是服务给的中文原因，`needs_fallback` 为真；没有异常

#### Scenario: 没有密钥

- **WHEN** 没给 `key`，环境变量也没设
- **THEN** 构造或第一次调用时抛 `HubAuthError`，没有发出任何请求

#### Scenario: 服务多返回了字段

- **WHEN** 服务的返回里多了客户端不认识的字段
- **THEN** 客户端照常工作，不报错

### Requirement: 错误分得清

客户端的异常 MUST 继承 `HubError`，并分成：

- `HubUnavailable`：连不上服务、连接超时、服务返回 5xx 或 503 `shutting_down`。项目遇到它就应该走 Bark。属性 `possibly_sent`：发消息的请求**已经发出去、却没等到回话**（读超时）时为真——这时不知道服务有没有发出，**不要当作没发**；其余情况为假。
- `HubAuthError`：401 / 403（密钥不对、项目停用、只推送的项目调了取话）。属性 `code`。
- `HubRequestError`：其他 4xx（400、404、410、413、429）。属性 `code`（如 `unknown_handle`、`handle_expired`、`rate_limited`）、`message`、`retry_after`（429 时有）。

`send`、`reply` 对 `rejected`、`failed`、`unconfirmed`、`partial`、`skipped` MUST NOT 抛异常（那是业务结果，不是错误）。

#### Scenario: 服务没开

- **WHEN** 服务没在运行，调用 `send`
- **THEN** 抛 `HubUnavailable`，`possibly_sent` 为假

#### Scenario: 发送途中读超时

- **WHEN** 服务收到请求但很久不回话，直到客户端读超时
- **THEN** 抛 `HubUnavailable`，`possibly_sent` 为真

#### Scenario: 限速

- **WHEN** 服务回 429 `rate_limited`，`Retry-After: 12`
- **THEN** 抛 `HubRequestError`，`code == "rate_limited"`，`retry_after == 12`

### Requirement: 不偷偷重发

`send` 和 `reply` MUST NOT 自动重试，除非调用方给了 `dedupe_key`（`reply` 没有去重键，永远不自动重试）：给了去重键时，连接失败（请求没发出去）或服务 5xx 时重试，最多 `retries`（默认 1）次，间隔 1 秒；读超时（`possibly_sent`）一律不重试。`inbox`、`ack`、`status`、`health` 可以安全重试，失败时按同样的次数和间隔处理。

#### Scenario: 没有去重键不重试

- **WHEN** 服务第一次回 503，调用 `send("…")`（没给 `dedupe_key`）
- **THEN** 只发出一次请求，抛 `HubUnavailable`

#### Scenario: 有去重键可以重试

- **WHEN** 服务第一次回 503，第二次成功，调用 `send("…", dedupe_key="k")`
- **THEN** 发出两次请求，都带 `dedupe_key: "k"`，最终返回 `sent`

### Requirement: 至少送达一次的取话循环

客户端 MUST 提供 `run_inbox(handler, *, stop=lambda: False, wait=25, max=10, sleep=time.sleep)`，把"取话、处理、确认、连不上时退避"这个循环写对，项目不用自己再写一遍：

- 循环取话；对每一项调用 `handler(item)`，`handler` **正常返回**才把这一项的 `id` 加进确认列表，处理完这一批以后统一调 `ack`；`handler` 抛异常的那一项不确认（记日志，继续处理这一批里后面的），租期过后服务会再给一次；
- 连不上服务（`HubUnavailable`）时退避：5 秒起每次翻倍、最多 60 秒，恢复后重新计；
- `HubAuthError` 时抛出（密钥不对或项目被停用，重试没有用）；
- `stop()` 为真时在两次取话之间退出；
- 一次也没取到话（空列表）不算错误，也不睡觉（服务端已经等过了）。

#### Scenario: 处理失败的不确认

- **WHEN** 一批 3 句话，处理第 2 句时 `handler` 抛异常
- **THEN** 确认的是第 1、3 句；第 2 句在租期后又出现，`attempt` 为 2

#### Scenario: 服务重启时退避

- **WHEN** 服务连续 3 次连不上，之后恢复
- **THEN** 退避时间依次是 5、10、20 秒，恢复后取话正常，下一次再失败又从 5 秒开始

#### Scenario: 密钥被停用

- **WHEN** 取话时服务回 403 `project_disabled`
- **THEN** `run_inbox` 抛 `HubAuthError`，不再重试

### Requirement: 接入指南和对接约定

仓库 MUST 带 `docs/clients/接入指南.md`，写清楚一个项目接入的步骤和惯例，让别的项目不用再读服务的代码：登记（`hubctl project add`）、放密钥（环境变量或 `.env`，不进 git）、发消息、取话和回复、**把"微信发不出去"接到自己的 Bark 兜底上**。其中对接约定写明：

- `sent`、`skipped` 算微信这边没问题；`unconfirmed`、`partial`、`rejected`、`failed` 以及 `HubUnavailable` 都应当走 Bark，并把 `reason` 写进 Bark 的末尾（ashare-quant 现在就是"微信没发出去：……"）；
- `unconfirmed` 和 `HubUnavailable(possibly_sent=True)` 的原因里要说明"可能已经到了"；
- 推送用稳定的 `dedupe_key`（比如 `brief-2026-10-12`），这样重跑不会发两份；
- 取话处理要幂等；回复用取话给的句柄，需要等使用者确认的回复带 `awaiting_confirmation=True`；
- 不要自己向使用者的微信发任何别的东西，也不要保存回复句柄超过 30 分钟。

另带一份 `docs/api/v1.md`，用 `curl` 示例写出每个接口的请求、返回、错误码，给不用 Python 的项目看；以及从服务导出的 `docs/api/openapi.json`，有测试检查它和服务实际的接口一致（接口改了没更新文档，测试就失败）。

#### Scenario: 文档和接口一致

- **WHEN** 运行测试
- **THEN** 从服务生成的 OpenAPI 描述与 `docs/api/openapi.json` 相同；不同时测试失败并提示重新导出

#### Scenario: 接入指南里的例子能跑

- **WHEN** 测试把 `docs/api/v1.md` 里标记为可执行的 `curl` 示例对着测试服务运行
- **THEN** 返回和文档写的一致（只比字段，不比 `id` 这类随机值）

### Requirement: 合约测试

仓库 MUST 有一组合约测试 `tests/contract/`：用真正的 HTTP 服务（本机回环上的随机端口，假上游）加上单文件客户端，把"项目这一侧能用到的全部行为"跑一遍——发消息的每种状态、回复、取话的租约和确认、等确认的路由、停用和换钥、限速、错误映射。ashare-quant 或别的项目以后改成走服务时，合约测试就是它们和服务之间的约定；服务升级前这组测试必须全过。

#### Scenario: 合约测试覆盖接口

- **WHEN** 运行 `pytest tests/contract`
- **THEN** `docs/api/v1.md` 里的每个接口至少有一个对应的合约测试通过（有测试检查接口清单和测试的对应关系）
