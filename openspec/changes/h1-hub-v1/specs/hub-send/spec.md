# Spec Delta

## Purpose

规定项目怎么让服务往微信发一条消息：接口长什么样、当场返回什么、一条话怎么拆、怎么判断"发出去了"、哪些情况不发、发不出去时项目拿到什么原因。核心是三条：**同步**（当场知道结果，好决定走不走 Bark）、**不偷偷重发**（拿不准有没有送到的，交给调用方决定）、**消息正文不落盘**。

条数怎么分、保底和上限怎么算见 `hub-quota`；这里只说"要几条、够不够、被拒了怎么说"。

## ADDED Requirements

### Requirement: 发消息接口

`POST /v1/messages` MUST 接受 JSON：

| 字段 | 必填 | 说明 |
|---|---|---|
| `text` | 是 | 纯文本全文，第一行是标题。不发 Markdown。去掉首尾空白后不能为空；最长 `send.max_chars`（默认 8000）个字，超了 400 `too_long` |
| `priority` | 否 | `urgent`、`normal`（默认）、`deferrable`，含义见 `hub-quota` |
| `dedupe_key` | 否 | 去重键，1 到 128 个字符，见下面"去重键" |
| `on_closed` | 否 | `fail`（默认）或 `skip`：对话窗口没开时，是当作发送失败，还是安静地跳过 |

其他字段 MUST 忽略（为以后加字段留余地）；`priority`、`on_closed` 的值不认识时 400 `bad_request` 并写明可选值。接口 MUST 在拿到最终结果以后才返回（同步），整个调用最长 `send.timeout_seconds`（默认 30）秒，超时前没发完的部分按下面"未确认送达"处理。

返回 200，内容固定为：

```json
{
  "status": "sent",
  "reason_code": null,
  "reason": null,
  "parts": 1,
  "parts_sent": 1,
  "duplicate": false,
  "id": "o_2f9c1a7e",
  "quota": {"left": 8, "limit": 10, "window_ends_at": "2026-10-11T18:20:00+08:00"}
}
```

`status` 只有下面几种，调用方 MUST 能只靠它决定要不要走 Bark：

| status | 意思 | 调用方通常怎么办 |
|---|---|---|
| `sent` | 每一条都发出去了，腾讯也确认了 | 不用做什么 |
| `unconfirmed` | 腾讯没报错，但没有确认送达（没有消息编号、或回话超时）。条数按已用算 | 走 Bark，**不要**马上重发（可能已经到了） |
| `partial` | 拆成几条，前面的发出去了，后面的没发出去；`parts_sent` 是发出去的条数 | 走 Bark 补发后面的内容，或整条走 Bark |
| `rejected` | 服务按规则没发（对话窗口没开、额度不够、超了上限、可延后的先让路……），没碰腾讯 | 走 Bark，末尾写明 `reason` |
| `failed` | 试着发了但没发出去（登录失效、没绑定、腾讯拒绝、连不上）；一条都没发出去 | 走 Bark，末尾写明 `reason` |
| `skipped` | 调用方选了 `on_closed: skip`，而窗口没开：不占条数、不算失败 | 什么都不用做 |

`reason_code` 是给程序看的稳定代码，`reason` 是给人看的中文原因（可以直接写进 Bark 的末尾）。`id` 是服务里这条发送记录的编号，排错用。`quota` 是发完以后调用方自己能用的余量，不含别的项目的信息；`left` 按 `hub-quota` 的"还能发几条"算（一条 `normal` 的推送现在最多能拆成几条发出去），窗口没开时是 0，`window_ends_at` 此时为 null。`status` 是 `rejected` / `failed` / `skipped` 时 `parts_sent` 一定是 0。

#### Scenario: 正常发一条

- **WHEN** 窗口开着、额度够，项目发一条 300 字的 `normal` 消息，假上游回成功并带消息编号
- **THEN** 返回 `sent`、`parts` 1、`parts_sent` 1、`duplicate` false；假上游收到恰好一条 `sendmessage`

#### Scenario: 未知字段和错误值

- **WHEN** 请求里多带一个叫 `foo` 的字段 / `priority` 写成 `high`
- **THEN** 前者照常处理；后者 400 `bad_request`，提示可选值，没有发任何东西，也不占条数

#### Scenario: 空消息

- **WHEN** `text` 只有空格和换行
- **THEN** 400 `empty_text`，不占条数

#### Scenario: 同步返回

- **WHEN** 假上游的 `sendmessage` 延迟 2 秒才回
- **THEN** 项目的请求在 2 秒后一次性拿到最终结果，而不是先拿到"处理中"

### Requirement: 谁都不能发给别人

服务 MUST 只向绑定的使用者本人发消息：收件人只来自凭证里的 `user_id`，接口没有任何"发给谁"的参数；`text` 里写什么都不改变收件人。对话凭证（`context_token`）MUST 由服务选：推送用使用者最近一条消息的，回复用被回复那条消息的（见 `hub-inbox`），项目不能指定。

#### Scenario: 想指定收件人

- **WHEN** 请求里带 `to_user_id` 或 `context_token` 字段
- **THEN** 这些字段被忽略；假上游收到的 `to_user_id` 仍是凭证里的使用者

### Requirement: 项目标签

服务 MUST 在发出的文字的第一行前面加这个项目的标签（见 `hub-projects`）：`【标签】` 紧贴着原来的第一行，没有标签的项目（默认项目的默认设置）原样发。加标签发生在拆条之前，所以每一条的标题（"第 2/3 条"那一行）也带标签。

#### Scenario: 其他项目的消息带标签

- **WHEN** 标签为 `x` 的项目发 `盘点完成\n共 3 项`
- **THEN** 假上游收到的文字是 `【x】盘点完成\n共 3 项`

#### Scenario: 默认项目不带

- **WHEN** 没有标签的默认项目发 `盘前简报\n……`
- **THEN** 假上游收到的文字与发出的完全一致

### Requirement: 拆条

一条消息最多 `send.part_chars`（默认 2000）个字；超了就拆成几条，规则和 ashare-quant 现在的 `split_message` 一致：优先在换行处断开（换行符留在前一段末尾），一行就超长时才在行中间断，不会把一个字拆开；按顺序拼回等于原文。拆成多条时，第 2 条起开头多一行 `<标题>（第 i/n 条）`，所以拆之前先把这一行的长度留出来（按"n 最多是窗口总条数"留，至少留出一半）。标题取（加了标签以后的）第一行。

拆出的条数就是要占的额度；拆出的条数超过 `窗口总条数 − 回复保留` 的，直接 `rejected` / `too_long`（原因写明"要拆成 N 条，超过这个窗口最多能发的 M 条"），一条都不发。

#### Scenario: 短消息不拆

- **WHEN** 发 1500 个字
- **THEN** 一条，开头没有"第 1/1 条"

#### Scenario: 长消息按行拆

- **WHEN** 发 4500 个字，每行约 50 个字
- **THEN** 拆成 3 条，每条不超过 2000 个字，第 2、3 条开头是 `<标题>（第 2/3 条）` 和 `<标题>（第 3/3 条）`，去掉这些开头后顺序拼回等于原文，不在行中间断开

#### Scenario: 一行就超长

- **WHEN** 发一行 5000 个字、没有换行的文字
- **THEN** 在字符边界上断开，每条不超过上限，拼回等于原文

#### Scenario: 拆出来太多条

- **WHEN** `send.max_chars` 调到 30000，发 25000 个字，窗口总条数 10、回复保留 2
- **THEN** `rejected` / `too_long`，假上游没有收到任何请求

### Requirement: 逐条发送，判断送达

多条按顺序一条一条发，上一条有结果才发下一条；同一条消息的几段 MUST 连续发出，中间不插进别的项目的消息（服务内部一把发送锁，等锁最多 `send.timeout_seconds`，等不到就 `failed` / `busy`，没占条数）。

每一条的结果按腾讯的返回判定：

- `ret` 和 `errcode` 都没有或为 0，且 HTTP 200：腾讯接受了。再看有没有**消息编号**（返回体里 `send.message_id_keys`，默认 `message_id`、`msg_id` 的任何一个有非空、非 0 的值）：
  - 有：这一条算 `sent`。
  - 没有：看 `upstream.confirm`——`strict`：这一条算 `unconfirmed`；`observe`（默认）和 `off`：算 `sent`。`observe` 额外把"没有消息编号"记进数据库，连续出现会触发告警（见 `hub-alerts`）。
- `ret` 或 `errcode` 为 -14：登录失效，见 `hub-login`，这一条和之后的都不发，整个结果是 `failed` / `login_expired`（已经发出去的前几条算 `partial`）。
- 其他非 0 的 `ret` / `errcode`，或 HTTP 4xx：腾讯明确拒绝，不重试；这一条没发出去，原因是 `微信 ClawBot 拒绝：<代码> <errmsg>`（`upstream_rejected`）。
- HTTP 429、5xx、连不上、连接超时（请求没发出去）：可重试，最多 `send.retries` 次（默认 2），间隔 1 秒、2 秒；重试时 MUST 用同一个 `client_id`（腾讯有没有按它去重不知道，但至少不比换新的差）。用完还不行，这一条没发出去，原因 `upstream_unreachable`。
- **请求已发出但等回话超时**（读超时）：不知道到没到。**不重试**，这一条算 `unconfirmed`（原因 `no_response`）；后面的条数也不再发。

整条消息的结果由各条的结果（`sent`、`unconfirmed`、`failed`、没来得及发）合起来：全部 `sent` → `sent`；每一条都是 `sent` 或 `unconfirmed`、至少一条 `unconfirmed` → `unconfirmed`；至少有一条 `sent` 或 `unconfirmed`、同时至少有一条 `failed` 或没来得及发 → `partial`；一条 `sent` 或 `unconfirmed` 都没有 → `failed`（`rejected` 只用于服务自己按规则不发的）。`parts_sent` 是 `sent` 和 `unconfirmed` 的条数之和。

#### Scenario: 腾讯接受但没有消息编号，严格档

- **WHEN** `upstream.confirm: strict`，假上游回 `{"ret":0}`
- **THEN** 返回 `unconfirmed`，原因码 `no_message_id`；条数照扣；假上游只收到一次请求（没有重发）

#### Scenario: 同样的返回，观察档

- **WHEN** `upstream.confirm: observe`（默认），假上游回 `{"ret":0}`
- **THEN** 返回 `sent`；数据库里这条记录标着"没有消息编号"

#### Scenario: 腾讯明确拒绝

- **WHEN** 假上游回 HTTP 200、`{"ret": 40001, "errmsg": "bad token"}`
- **THEN** `failed` / `upstream_rejected`，原因含"微信 ClawBot 拒绝：40001 bad token"；假上游只收到一次；这一条的额度退回

#### Scenario: 服务端出错重试

- **WHEN** 假上游前两次回 503，第三次成功
- **THEN** 共 3 次请求，三次的 `client_id` 相同，最终 `sent`

#### Scenario: 回话超时不重发

- **WHEN** 假上游收到请求但不回话，直到读超时
- **THEN** 返回 `unconfirmed` / `no_response`；假上游只收到一次请求

#### Scenario: 多条里中途失败

- **WHEN** 拆成 3 条，第 1 条成功，第 2 条回 `ret: 40001`
- **THEN** 返回 `partial`、`parts` 3、`parts_sent` 1；第 2、3 条的额度退回，第 3 条没有发出

#### Scenario: 发送期间登录失效

- **WHEN** 第 1 条成功，第 2 条回 `ret: -14`
- **THEN** `partial`，原因码 `login_expired`；登录状态变成失效，之后的发送直接 `failed` / `login_expired`

### Requirement: 不够发就不发，并说明原因

发之前服务 MUST 先判断能不能发（条数规则见 `hub-quota`），不能发就不碰腾讯，返回 `rejected`（项目选了 `on_closed: skip` 且原因是窗口没开时返回 `skipped`），`reason` 的中文原文如下（`<…>` 是变量），其中前四条与 ashare-quant 现在的文字一致，这样项目 Bark 末尾写的原因不变：

| reason_code | reason |
|---|---|
| `never_contacted` | 还没收到你在微信里发的消息：给 ClawBot 发任意一句话就能开始接收 |
| `window_closed` | 对话窗口过期了（你最后一次发消息是 `<M月D日 HH:MM>`），给 ClawBot 发任意一句话就能恢复 |
| `quota_exhausted` | 这 24 小时的 `<10>` 条已经用完，给 ClawBot 发任意一句话就能恢复 |
| `quota_insufficient` | 这 24 小时的 `<10>` 条只剩 `<N>` 条，不够发这 `<M>` 条；给 ClawBot 发任意一句话就能恢复 |
| `project_cap` | `<项目>` 这 24 小时最多发 `<cap>` 条，已经发了 `<used>` 条，不够发这 `<M>` 条 |
| `reserved_for_others` | 剩下的 `<N>` 条是留给回复和其他项目的，这 `<M>` 条先不发 |
| `deferrable_denied` | 额度比较紧，可延后的消息先不发（还剩 `<N>` 条） |
| `too_long` | 这条要拆成 `<N>` 条，超过这个窗口最多能发的 `<M>` 条 |

时间一律按配置的显示时区（`display.timezone`，默认 `Asia/Shanghai`）写，格式 `M月D日 HH:MM`。"24 小时""10 条"按配置里实际的窗口长度和总条数写。

#### Scenario: 窗口过期

- **WHEN** 使用者最后一条消息是 25 小时前，项目发消息
- **THEN** `rejected` / `window_closed`，`reason` 含"你最后一次发消息是"和那条消息的时间；假上游没有收到请求

#### Scenario: 还没人说过话

- **WHEN** 刚绑定、使用者还没给 ClawBot 发过任何消息
- **THEN** `rejected` / `never_contacted`

#### Scenario: 窗口没开，选择跳过

- **WHEN** 窗口过期，项目带 `on_closed: skip` 发消息
- **THEN** `skipped`，原因码 `window_closed`，条数一分没扣，没有发送失败的记录，也不触发"消息没发出去"的告警

#### Scenario: 条数不够

- **WHEN** 窗口里还剩 1 条，项目发一条要拆成 3 条的消息
- **THEN** `rejected` / `quota_insufficient`，原因里写"只剩 1 条，不够发这 3 条"；一条都没发

### Requirement: 去重键

带 `dedupe_key` 的消息，同一个项目、同一个键 MUST 在 `send.dedupe_hours`（默认 24）小时内只发一次，键按项目分开（两个项目用同一个键互不影响）。第二次请求返回第一次的结果，并带 `"duplicate": true`，不再占条数、不再碰腾讯。

- 第一次还没发完时，第二次 MUST 等它发完再返回同样的结果。
- 第一次的结果是 `rejected`、`failed`、`skipped`（一条都没发出去）时，键 MUST 释放，下一次相同的请求当成新的发送。第一次是 `sent`、`unconfirmed`、`partial` 时键保持占用（可能已经有一部分到了）。
- 键是区分大小写的字符串，只保存摘要和结果，不保存正文。

#### Scenario: 项目重跑

- **WHEN** 同一个项目先后两次用键 `brief-2026-10-12` 发消息，第一次成功
- **THEN** 第二次返回 `sent` 和 `duplicate: true`；假上游一共只收到一条；额度只扣了一次

#### Scenario: 第一次被拒，第二次能重来

- **WHEN** 第一次因为窗口没开 `rejected`，使用者随后说了话，项目用同一个键再发
- **THEN** 第二次当成新的发送，正常发出

#### Scenario: 两个项目同一个键

- **WHEN** 项目 A、项目 B 各用键 `daily` 发一条
- **THEN** 两条都发出，互不影响

#### Scenario: 同时来两次

- **WHEN** 同一个键的两次请求几乎同时到达
- **THEN** 假上游只收到一条；两次请求拿到的结果一致，后到的那个 `duplicate: true`

### Requirement: 回复

`POST /v1/replies` MUST 接受 `handle`（取话时给的回复句柄，见 `hub-inbox`）、`text`，以及可选的 `awaiting_confirmation`（布尔，默认 false）、`confirm_seconds`（默认 600，最大 1800）；校验、拆条、判定送达、返回格式与发消息完全一致（`priority` 和 `dedupe_key` 不适用）。区别只有三处：

- 用被回复那条消息的对话凭证发；
- 条数记在"回复"一类里，优先于推送（见 `hub-quota`）；
- `awaiting_confirmation: true` 表示这句回复在等使用者回"对"之类的确认话，服务据此把之后短短的确认话交给这个项目（见 `hub-inbox`）。发送没成功（`rejected`、`failed`）时不记等待确认。

句柄找不到、属于别的项目：404 `unknown_handle`（两种情况一模一样）；句柄过期（超过 `reply.handle_minutes`，默认 30 分钟）：410 `handle_expired`；同一个句柄最多回 `reply.max_per_handle`（默认 5）次，超了 429 `reply_limit`。

#### Scenario: 回复用那句话自己的凭证

- **WHEN** 使用者先后发了 A、B 两句话，项目拿 A 的句柄回复
- **THEN** 假上游收到的 `context_token` 是 A 那句话的，不是最近一句 B 的

#### Scenario: 等确认

- **WHEN** 项目回复时带 `awaiting_confirmation: true`，发送成功
- **THEN** 之后 10 分钟内，使用者回"对"，这句话交给这个项目（见 `hub-inbox` 的场景）

#### Scenario: 回复没发出去不记等待

- **WHEN** 带 `awaiting_confirmation: true` 的回复因为窗口过期 `rejected`
- **THEN** 不记"等确认"，"对"之后仍归默认项目

### Requirement: 消息正文不落盘

服务 MUST NOT 把任何消息的正文（发出的、收到的）写进日志、数据库的长期记录或错误信息。发送记录只存：编号、项目、类型（推送或回复）、优先级、去重键的摘要、条数、各条的结果、原因码、字数、时间。收到的话在项目取走（确认）或过期以后 MUST 从数据库里清掉正文，只留一条不带正文的记录。日志里写"收到一条消息，N 个字"这类信息，不写内容。

#### Scenario: 发完以后找不到正文

- **WHEN** 项目发一条含独特字符串 `ZZ-UNIQUE-77` 的消息，发送完成后
- **THEN** 数据库文件和日志文件里都搜不到这个字符串

#### Scenario: 取走的话清掉正文

- **WHEN** 使用者的一句话含独特字符串，项目取走并确认以后（正文保留期过了）
- **THEN** 数据库里该记录的正文是空的，字符串搜不到

### Requirement: 服务这边的限制和失败方式

- 请求体不是合法 JSON 或字段类型不对：400 `bad_request`，不带请求内容。
- 发送接口遇到服务内部出错：500 `internal`，已占的额度 MUST 退回，已发出去的条数不退；日志里记错误类型和堆栈，不含正文。
- 项目停用：403 `project_disabled`；密钥不对：401（见 `hub-projects`）。
- 服务正在关闭：新请求 503 `shutting_down`，在发的那条发完。

#### Scenario: 内部出错额度不丢

- **WHEN** 发送的第 2 条途中服务内部抛出意外（测试里注入）
- **THEN** 返回 500 `internal`；已发出的第 1 条占的额度保留，没发的退回；重启后数据库里的账是对的
