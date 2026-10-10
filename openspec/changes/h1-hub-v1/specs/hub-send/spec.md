# Spec Delta

## Purpose

规定项目怎么让服务往微信发一条消息：接口长什么样、当场返回什么、一条话怎么拆、按什么顺序检查、怎么判断"发出去了"、发不出去时项目拿到什么原因、出错时 HTTP 响应长什么样。核心是三条：**同步**（当场知道结果，好决定走不走 Bark）、**不偷偷重发**（拿不准有没有送到的，交给调用方决定）、**消息正文不落盘**。

条数怎么分、保底和上限怎么算见 `hub-quota`；这里只说"要几条、够不够、被拒了怎么说"。

## ADDED Requirements

### Requirement: 发消息接口

`POST /v1/messages` MUST 接受 JSON：

| 字段 | 必填 | 说明 |
|---|---|---|
| `text` | 是 | 纯文本全文，第一行是标题。不发 Markdown。去掉首尾空白后不能为空（400 `empty_text`）；最长 `send.max_chars`（默认 8000）个字，超了 400 `text_too_long`。发出去的是原文，不去首尾空白 |
| `priority` | 否 | `urgent`、`normal`（默认）、`deferrable`，含义见 `hub-quota` |
| `dedupe_key` | 否 | 去重键，1 到 128 个字符（长度不对 400 `bad_request`），见"去重键" |
| `on_closed` | 否 | `fail`（默认）或 `skip`：对话窗口没开时，是当作发送失败，还是安静地跳过 |

其他字段 MUST 忽略（为以后加字段留余地）；`priority`、`on_closed` 的值不认识时 400 `bad_request` 并写明可选值。接口 MUST 在拿到最终结果以后才返回（同步）。整个调用（含等发送锁）共用一份时间预算 `send.timeout_seconds`（默认 30 秒），到点时怎么算见"逐条发送，判断送达"。

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
| `unconfirmed` | 腾讯没报错，但没有确认送达（没有消息编号、回话超时、或发送被中断）。条数按已用算 | 走 Bark，**不要**马上重发（可能已经到了） |
| `partial` | 拆成几条，前面的发出去了，后面的没发出去；`parts_sent` 是发出去的条数 | 走 Bark 补发后面的内容，或整条走 Bark |
| `rejected` | 服务按规则没发（对话窗口没开、额度不够、超了上限、可延后的先让路、腾讯拒绝后暂停……），没碰腾讯 | 走 Bark，末尾写明 `reason` |
| `failed` | 试着发了但没发出去（登录失效、没绑定、腾讯拒绝、连不上、服务正忙）；一条都没发出去 | 走 Bark，末尾写明 `reason` |
| `skipped` | 调用方选了 `on_closed: skip`，而窗口没开：不占条数、不算失败 | 什么都不用做 |

`reason_code` 是给程序看的稳定代码，`reason` 是给人看的中文原因（可以直接写进 Bark 的末尾），全部代码和原文见"原因代码和原文"。`id` 是服务里这条发送记录的编号，排错用。`quota` 是发完以后调用方自己能用的余量，不含别的项目的信息；`left` 按 `hub-quota` 的"还能发几条"算（一条 `normal` 的推送现在最多能拆成几条发出去），登录不正常、窗口没开、处于腾讯拒绝后的暂停期时是 0，窗口没开时 `window_ends_at` 为 null（`GET /v1/status` 里同一个值叫 `window.ends_at`）。`status` 是 `rejected` / `failed` / `skipped` 时 `parts_sent` 一定是 0。

#### Scenario: 正常发一条

- **WHEN** 窗口开着、额度够，项目发一条 300 字的 `normal` 消息，假上游回成功并带消息编号
- **THEN** 返回 `sent`、`parts` 1、`parts_sent` 1、`duplicate` false；假上游收到恰好一条 `sendmessage`

#### Scenario: 未知字段和错误值

- **WHEN** 请求里多带一个叫 `foo` 的字段 / `priority` 写成 `high`
- **THEN** 前者照常处理；后者 400 `bad_request`，提示可选值，没有发任何东西，也不占条数

#### Scenario: 空消息

- **WHEN** `text` 只有空格和换行
- **THEN** 400 `empty_text`，不占条数

#### Scenario: 文字太长

- **WHEN** `text` 有 8001 个字（`send.max_chars` 是默认值）
- **THEN** 400 `text_too_long`，不占条数

#### Scenario: 同步返回

- **WHEN** 假上游的 `sendmessage` 延迟 2 秒才回
- **THEN** 项目的请求在 2 秒后一次性拿到最终结果，而不是先拿到"处理中"

#### Scenario: 发出去的是原文

- **WHEN** `text` 是 `  盘点完成\n共 3 项  `（首尾有空白）
- **THEN** 假上游收到的文字与发出的完全一致，首尾空白还在

### Requirement: 谁都不能发给别人

服务 MUST 只向绑定的使用者本人发消息：收件人只来自凭证里的 `user_id`，接口没有任何"发给谁"的参数；`text` 里写什么都不改变收件人。对话凭证（`context_token`）MUST 由服务选：推送用使用者最近一条消息的，回复用被回复那条消息的（见 `hub-inbox`），项目不能指定。

#### Scenario: 想指定收件人

- **WHEN** 请求里带 `to_user_id` 或 `context_token` 字段
- **THEN** 这些字段被忽略；假上游收到的 `to_user_id` 仍是凭证里的使用者

### Requirement: 项目标签

服务 MUST 在发出的文字的第一行前面加这个项目的标签（见 `hub-projects`）：`【标签】` 紧贴着原来的第一行（同一行），没有标签的项目（默认项目的默认设置）原样发。服务自己的回复（见 `hub-inbox`）同样处理，标签是 `hub.label`（默认"服务"）。加标签发生在拆条之前，所以每一条的标题（"第 2/3 条"那一行）也带标签。

#### Scenario: 其他项目的消息带标签

- **WHEN** 标签为 `x` 的项目发 `盘点完成\n共 3 项`
- **THEN** 假上游收到的文字是 `【x】盘点完成\n共 3 项`

#### Scenario: 默认项目不带

- **WHEN** 没有标签的默认项目发 `盘前简报\n……`
- **THEN** 假上游收到的文字与发出的完全一致

### Requirement: 拆条

一条消息（含第 2 条起多出来的开头）最多 `send.part_chars`（默认 2000，启动时校验不得小于 200）个字；超了就拆成几条。规则（行为与 ashare-quant 现在的 `split_message` 一致，只多一条标题长度的限制）：

1. 先按"项目标签"把标签加在第一行前面。
2. 加了标签的全文不超过 `part_chars` 个字的，就是一条，原样发、没有开头，下面的规则不用看。（ashare-quant 也是先按整条上限拆一遍，多于一条才扣掉开头的位置重拆；少了这一步，字数在"`part_chars` 减去开头的字数"和 `part_chars` 之间的消息会被多拆出一条，多占一条额度。）
3. 标题取（加了标签以后的）第一行；用在开头里的标题最多取前 30 个字，超出的截断并加"…"。（这是比 ashare-quant 现在多出来的限制：它的消息第一行都很短，没有这个问题；没有它，一行就超长的消息会让开头本身超过上限。）
4. 第 2 条起每条前面多一行开头 `<标题>（第 i/n 条）`，后面带换行。拆之前先给开头留位置：每条正文的预算 = max(`part_chars` − 开头的字数, `part_chars` 的一半取整)，开头的字数按 `n = quota.window_limit`（最多拆成这么多条）估。
5. 按这个预算拆全文：优先在换行处断开（换行符留在前一段末尾），一行就超长时才在行中间断，不会把一个字拆开。
6. 拆成多条时，第 2 条起加上开头。

结果：每条（含开头）不超过 `part_chars` 个字；去掉开头以后按顺序拼回等于加了标签的原文。

拆出的条数就是要占的额度。拆出的条数超过 M 的，直接 `rejected` / `too_long`（原因写明"这条要拆成 N 条，超过这个窗口最多能发的 M 条"），一条都不发：推送的 M 是 `窗口总条数 − 回复保留`，回复的 M 是 `窗口总条数`（回复可以用到保留的那几条）。这一步先于登录和额度的判定。

#### Scenario: 短消息不拆

- **WHEN** 发 1500 个字
- **THEN** 一条，开头没有"第 1/1 条"

#### Scenario: 全文在上限以内，不扣开头的位置

- **WHEN** 第一行 10 个字、全文 1990 个字（不超过 2000，但超过 2000 减去开头的字数）
- **THEN** 一条，没有开头，占一条额度；假上游只收到一次请求，文字与发出的一致

#### Scenario: 刚好超过上限

- **WHEN** 第一行 10 个字、全文 2001 个字
- **THEN** 拆成 2 条，第 2 条有开头；每条（含开头）不超过 2000 个字，去掉开头后拼回等于原文

#### Scenario: 长消息按行拆

- **WHEN** 发 4500 个字，每行约 50 个字
- **THEN** 拆成 3 条，每条（含开头）不超过 2000 个字，第 2、3 条开头是 `<标题>（第 2/3 条）` 和 `<标题>（第 3/3 条）`，去掉这些开头后顺序拼回等于原文，不在行中间断开

#### Scenario: 一行就超长

- **WHEN** 发一行 5000 个字、没有换行的文字
- **THEN** 在字符边界上断开；开头里的标题是这一行的前 30 个字加"…"；每条（含开头）不超过 2000 个字；去掉开头后拼回等于原文

#### Scenario: 标题里的标签

- **WHEN** 标签为 `x` 的项目发一条要拆成 2 条的消息，第一行是 `盘点`
- **THEN** 第 2 条的开头是 `【x】盘点（第 2/2 条）`

#### Scenario: 拆出来太多条

- **WHEN** `send.max_chars` 调到 30000，发 25000 个 ASCII 字符（不会先撞上 64 KiB 的请求体上限），窗口总条数 10、回复保留 2
- **THEN** `rejected` / `too_long`，假上游没有收到任何请求；即使此时登录已失效，也是 `too_long` 而不是 `login_expired`

#### Scenario: 回复可以多拆到保留的条数

- **WHEN** 窗口总条数 10、回复保留 2，一条回复要拆成 9 条
- **THEN** 不是 `too_long`（回复的 M 是 10），按额度规则继续判定

### Requirement: 发送前的检查顺序

服务 MUST 按这个顺序检查，第一个不通过的就是结果，后面的不再看。除第 2 项命中去重外，被拒的请求不碰腾讯、不占条数：

1. 字段校验（见"发消息接口"）：400。
2. 去重键命中（见"去重键"）：返回原来的结果。
3. 拆条，条数太多：`rejected` / `too_long`。
4. 登录状态不正常：`failed` + `login_missing` / `login_broken` / `login_expired`（不看 `on_closed`）。
5. 对话窗口没开（`never_contacted` / `window_closed`）：`on_closed: skip` 时 `skipped`，否则 `rejected`；回复没有 `on_closed`，一律 `rejected`。
6. 仅推送：腾讯拒绝后的暂停期内：`rejected` / `upstream_blocked`（见 `hub-quota`）。
7. 额度（`hub-quota` 里判定的第 2 至第 7 步）：`rejected`。

#### Scenario: 几个条件同时不满足

- **WHEN** 登录已失效、窗口也过期了，项目带 `on_closed: skip` 发消息
- **THEN** `failed` / `login_expired`（登录先于窗口检查，不看 `on_closed`）；假上游没有收到请求

#### Scenario: 暂停期内额度又用完

- **WHEN** 处于腾讯拒绝后的暂停期，并且窗口里的额度已经用完，项目发推送
- **THEN** `rejected` / `upstream_blocked`（暂停先于额度）

### Requirement: 逐条发送，判断送达

多条按顺序一条一条发，上一条有结果才发下一条；同一条消息的几段 MUST 连续发出，中间不插进别的项目的消息（服务内部一把发送锁）。整条消息的几段用同一个对话凭证（预占时定下，见 `hub-quota`）。

每一条向腾讯发一次 `sendmessage` 请求，**每次用新连接**（不复用空闲连接，免得服务端早已关掉的连接把"没发出去"误判成"不知道到没到"）。结果按下表判定；先看 HTTP 层，再看业务码，`ret` 和 `errcode` 的取法与 ashare-quant 相同——先取 `ret`（不是空也不是 0），没有再取 `errcode`，所以 `{"ret": 5, "errcode": -14}` 是拒绝（5），不是登录失效：

| 情形 | 类别 | 处理 |
|---|---|---|
| DNS 失败、连接被拒、连接超时、TLS 握手失败；收到完整的 HTTP 429 或 5xx 响应（不看响应体，里面带 -14 也一样） | 没发出去 | 可重试，最多 `send.retries`（默认 2）次，间隔 1 秒、2 秒；重试 MUST 用同一个 `client_id`；用完还不行：这一条没发出去，`upstream_unreachable` |
| 请求发完之后读超时；连接被对方重置或关闭；响应读到一半断开 | 不知道到没到 | **不重试**。这一条算 `unconfirmed`（`no_response`），条数保持已用；后面的条数不再发 |
| HTTP 4xx（429 除外），或业务码非 0 且不是 -14 | 明确拒绝 | 不重试；这一条没发出去，`upstream_rejected`（只有 HTTP 码时写 `HTTP <状态码>`） |
| 业务码是 -14 | 登录失效 | 见 `hub-login`；这一条和后面的都不发，`login_expired` |
| HTTP 200，`ret` 和 `errcode` 都没有或为 0 | 腾讯接受了 | 再看有没有**消息编号**（返回体里 `send.message_id_keys`，默认 `message_id`、`msg_id` 的任何一个有非空、非 0 的值）：有 → 这一条 `sent`；没有 → 看 `upstream.confirm`：`strict` 时这一条 `unconfirmed`（`no_message_id`），`observe`（默认）和 `off` 时 `sent`。`observe` 额外把"没有消息编号"记进数据库，连续出现会触发告警（见 `hub-alerts`）。这两种情形都继续发后面的条数 |

**总时限。** 预算从服务开始处理这个 POST 起算 `send.timeout_seconds`（默认 30）秒，等发送锁和向腾讯发请求共用这一份。每一次向腾讯发请求，连接超时和读超时都取 `min(upstream.request_timeout, 剩下的预算)`（`upstream.request_timeout` 默认 15 秒，所以默认下一次请求最多等 15 秒）。**每一次向腾讯发请求之前（包括下一条的第一次尝试和重试），剩下的预算不足 1 秒（固定值，见 design 的定值表）的，就不再开始**：这一条算"没来得及发"；重试被这条规则拦下时，这一条沿用它上一次失败的原因（例如 `upstream_unreachable`），不改成 `send_timeout`。到点时：

- 还在等发送锁：`failed` / `busy`，预占的条数全部退回（服务正在关闭时，还在等锁的请求也马上这样结束，见 `hub-deploy`）；
- 某一条已经发出、正在等回话：就是读超时，按上表算 `unconfirmed`（`no_response`）；
- 还没开始发的条数：退回，算"没来得及发"，各自的原因码是 `send_timeout`。

**合并。** 整条消息的结果由各条合成：全部 `sent` → `sent`；每一条都是 `sent` 或 `unconfirmed`、至少一条 `unconfirmed` → `unconfirmed`；至少一条 `sent` 或 `unconfirmed`、同时至少一条没发出去（失败或没来得及发）→ `partial`；一条 `sent` 或 `unconfirmed` 都没有 → `failed`（`rejected` 只用于服务自己按规则不发的）。`parts_sent` 是 `sent` 和 `unconfirmed` 的条数之和。`reason_code` 和 `reason` 取**第一个出问题的那一条**的：没来得及发的条数排在已经发出的条数后面，所以前面的条数都 `sent` 而后面的没来得及发时，整条的原因码就是 `send_timeout`（原因写"发送超过 N 秒还没发完，后面的 m 条没有发"）；拆成多条且不是全部 `sent` 时，`reason` 末尾加"；发出了 i 条，共 n 条"（与 ashare-quant 一致）。

#### Scenario: 腾讯接受但没有消息编号，严格档

- **WHEN** `upstream.confirm: strict`，假上游回 `{"ret":0}`
- **THEN** 返回 `unconfirmed`，原因码 `no_message_id`；条数照扣；假上游只收到一次请求（没有重发）

#### Scenario: 同样的返回，观察档

- **WHEN** `upstream.confirm: observe`（默认），假上游回 `{"ret":0}`
- **THEN** 返回 `sent`；数据库里这条记录标着"没有消息编号"

#### Scenario: 腾讯明确拒绝

- **WHEN** 假上游回 HTTP 200、`{"ret": 40001, "errmsg": "bad token"}`
- **THEN** `failed` / `upstream_rejected`，原因含"微信 ClawBot 拒绝：40001 bad token"；假上游只收到一次；这一条的额度退回

#### Scenario: 只有 HTTP 码的拒绝

- **WHEN** 假上游回 HTTP 403，没有业务码
- **THEN** `failed` / `upstream_rejected`，原因含"微信 ClawBot 拒绝：HTTP 403"；只收到一次请求

#### Scenario: ret 和 errcode 同时有

- **WHEN** 假上游回 `{"ret": 5, "errcode": -14}`
- **THEN** 按 `ret` 取 5：`failed` / `upstream_rejected`，登录状态不变

#### Scenario: 服务端出错重试

- **WHEN** 假上游前两次回 503，第三次成功
- **THEN** 共 3 次请求，三次的 `client_id` 相同，最终 `sent`

#### Scenario: 5xx 响应里带 -14 也只是重试

- **WHEN** 假上游回 HTTP 502，响应体里有 `errcode: -14`，第二次成功
- **THEN** 当作可重试的失败，重试后 `sent`；登录状态不变

#### Scenario: 重试用完

- **WHEN** 假上游连续 3 次连不上（连接被拒）
- **THEN** `failed` / `upstream_unreachable`，原因含"重试 2 次后仍然失败"；共 3 次尝试，同一个 `client_id`；额度退回

#### Scenario: 回话超时不重发

- **WHEN** 假上游收到请求但不回话，直到读超时
- **THEN** 返回 `unconfirmed` / `no_response`；假上游只收到一次请求

#### Scenario: 发出去以后连接被重置

- **WHEN** 假上游收完请求就断开连接，没有任何回话
- **THEN** 同样是 `unconfirmed` / `no_response`，只收到一次请求，没有重发

#### Scenario: 每次用新连接，旧的空闲连接不碍事

- **WHEN** 第一条发送成功后，假上游把那条空闲的连接关掉（模拟对方的保活超时），项目随后又发一条
- **THEN** 第二条是 `sent`，假上游对它只收到一次请求，没有被误判成 `unconfirmed`；假上游记录显示两次请求来自两条不同的连接

#### Scenario: 多条里中途失败

- **WHEN** 拆成 3 条，第 1 条成功，第 2 条回 `ret: 40001`
- **THEN** 返回 `partial`、`parts` 3、`parts_sent` 1，原因含"发出了 1 条，共 3 条"；第 2、3 条的额度退回，第 3 条没有发出

#### Scenario: 未确认以后不再发后面的

- **WHEN** 拆成 3 条，第 1 条成功，第 2 条读超时
- **THEN** `partial`、`parts_sent` 2、原因码 `no_response`；第 3 条没有发出，它的额度退回；第 1、2 条保持已用

#### Scenario: 发送期间登录失效

- **WHEN** 第 1 条成功，第 2 条回 `ret: -14`
- **THEN** `partial`，原因码 `login_expired`；登录状态变成失效，之后的发送直接 `failed` / `login_expired`

#### Scenario: 单次请求默认最多等 15 秒

- **WHEN** 配置都是默认值，假上游收到请求后 20 秒（假时钟）才回话
- **THEN** 15 秒时读超时，这一条是 `unconfirmed` / `no_response`；假上游只收到一次请求，没有重发

#### Scenario: 总时限用完前一条没来得及开始

- **WHEN** `upstream.request_timeout` 配成 30，`send.timeout_seconds` 是 30；拆成 2 条，第 1 条（假时钟）花了 29.5 秒才成功
- **THEN** 第 2 条不开始（剩下 0.5 秒，不足 1 秒）：`partial`、`parts_sent` 1、原因码 `send_timeout`；第 2 条的额度退回，假上游只收到一次请求

#### Scenario: 总时限卡在回话上

- **WHEN** `upstream.request_timeout` 配成 25，`send.timeout_seconds` 是 30；拆成 3 条，假上游每次回话要 20 秒（假时钟）
- **THEN** 第 1 条 20 秒时 `sent`；第 2 条的读超时只剩 10 秒（`min(25, 10)`），到点算 `unconfirmed`（`no_response`）；第 3 条没发，额度退回；整条 `partial`、`parts_sent` 2，原因码 `no_response`

#### Scenario: 重试被预算拦下

- **WHEN** `send.timeout_seconds` 配成 3，假上游拒绝连接，第一次尝试（假时钟）用掉 2.5 秒
- **THEN** 重试之前剩下 0.5 秒，不足 1 秒，不再重试：`failed` / `upstream_unreachable`（不是 `send_timeout`），假上游只收到一次连接尝试；额度退回

#### Scenario: 等发送锁等到预算用完

- **WHEN** 另一条发送占着发送锁超过 `send.timeout_seconds`
- **THEN** 这个请求 `failed` / `busy`，预占的条数全部退回，没有碰腾讯

#### Scenario: 几段连续发出

- **WHEN** 项目 A、B 同时各发一条要拆成 2 条的消息
- **THEN** 假上游收到的顺序是 A1、A2、B1、B2 或 B1、B2、A1、A2，不会交错

### Requirement: 原因代码和原文

被服务拒绝、没发出去或没确认的，`reason_code` 和 `reason` 必须是下表里的（`<…>` 是变量）。前四条与 ashare-quant 现在的文字一致，这样项目 Bark 末尾写的原因不变；`login_expired` 只把命令名从 `aq wechat login` 换成 `hubctl login`。

| reason_code | 出现在 | reason |
|---|---|---|
| `never_contacted` | rejected / skipped | 还没收到你在微信里发的消息：给 ClawBot 发任意一句话就能开始接收 |
| `window_closed` | rejected / skipped | 对话窗口过期了（你最后一次发消息是 `<M月D日 HH:MM>`），给 ClawBot 发任意一句话就能恢复 |
| `quota_exhausted` | rejected | 这 24 小时的 `<10>` 条已经用完，给 ClawBot 发任意一句话就能恢复 |
| `quota_insufficient` | rejected | 这 24 小时的 `<10>` 条只剩 `<N>` 条，不够发这 `<M>` 条；给 ClawBot 发任意一句话就能恢复 |
| `project_cap` | rejected | `<项目>` 这 24 小时最多发 `<cap>` 条，已经发了 `<used>` 条，不够发这 `<M>` 条 |
| `reserved_for_others` | rejected | 剩下的 `<N>` 条是留给回复和其他项目的，这 `<M>` 条先不发 |
| `deferrable_denied` | rejected | 额度比较紧，可延后的消息先不发（还剩 `<N>` 条） |
| `too_long` | rejected | 这条要拆成 `<N>` 条，超过这个窗口最多能发的 `<M>` 条 |
| `upstream_blocked` | rejected | 腾讯拒绝了这个窗口里的发送（`<代码>`），先不再试；`<N>` 分钟后再试，或者给 ClawBot 发一句话重新开窗口 |
| `login_missing` | failed | 还没绑定微信 ClawBot：运行 hubctl login |
| `login_expired` | failed / partial | ClawBot 登录失效了：在电脑上运行 hubctl login 重新扫码 |
| `login_broken` | failed | account.json 读不出来：重新运行 hubctl login |
| `upstream_rejected` | failed / partial | 微信 ClawBot 拒绝：`<代码>` `<errmsg>` |
| `upstream_unreachable` | failed / partial | 微信 ClawBot 发送失败，重试 `<n>` 次后仍然失败：`<原因>`（与 ashare-quant 同文案，`<原因>` 已去掉凭证） |
| `no_response` | unconfirmed / partial | 微信 ClawBot 没有回话，这条可能已经到了，不要重发 |
| `no_message_id` | unconfirmed / partial | 微信 ClawBot 没有返回消息编号，这条可能没有到 |
| `send_timeout` | partial / failed | 发送超过 `<N>` 秒还没发完，后面的 `<m>` 条没有发 |
| `busy` | failed | 服务正忙，这条没有发，稍后再试 |
| `interrupted` | unconfirmed（只在重放去重结果时） | 上次发送被中断，不知道发出了几条，不要重发 |
| `duplicate_in_flight` | unconfirmed（只在重放去重结果时；只有上一次的发送线程超出自己的预算还没收尾才会出现） | 同一个去重键的上一次发送还没结束，先不要重发 |

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

#### Scenario: 每个代码的原文都对得上

- **WHEN** 逐个触发上表里的每个 `reason_code`（测试里用假上游和假时钟造出对应的情形）
- **THEN** 每个 `reason` 都与上表的模板一致（变量代入以后），状态与"出现在"一栏一致；没有表以外的代码出现

### Requirement: 去重键

带 `dedupe_key` 的消息，同一个项目、同一个键 MUST 在 `send.dedupe_hours`（默认 24）小时内只发一次，键按项目分开（两个项目用同一个键互不影响）。键从发送记录创建时起占用，到期后失效；发送记录本身按 `events.keep_days` 删除。第二次请求返回第一次的结果，并带 `"duplicate": true`，不再占条数、不再碰腾讯。

- **占着键的状态**：发送中、`sent`、`unconfirmed`、`partial`，以及服务中途被杀或内部出错留下的"中断"（可能已经有一部分到了）。**释放键的状态**：`rejected`、`failed`、`skipped`（一条都没发出去），下一次相同的请求当成新的发送。
- 第一次还没收尾时，第二个请求 MUST 等它收尾，最长等自己的预算（从它收到请求起算 `send.timeout_seconds`）：时限内收尾的，不管结果是什么都返回它的结果并带 `duplicate: true`，**不**自动重发（键如果已经释放，留给之后的新请求）；时限内仍没收尾的，返回 `unconfirmed` / `duplicate_in_flight`，带 `duplicate: true`，不占条数。
- 重放的结果：`status`、`reason_code`、`reason`、`parts`、`parts_sent`、`id` 取第一次的；`quota` 是**现在**的余量，不是第一次的；"中断"对外是 `unconfirmed` / `interrupted`。
- 键是区分大小写的字符串，数据库里只保存摘要（加项目名当盐）和结果，不保存正文。

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

#### Scenario: 被中断的发送不能用同一个键重发

- **WHEN** 一条带键的发送中途服务被 `kill -9`，重启后项目用同一个键再发
- **THEN** 返回 `unconfirmed` / `interrupted`，`duplicate: true`；假上游没有收到新的请求

#### Scenario: 内部出错也占着键

- **WHEN** 发送的第 2 条途中服务内部出错（测试里注入，此时第 1 条已经发给假上游），项目用同一个键再发
- **THEN** 第一次返回 500 `internal`；第二次是 `unconfirmed` / `interrupted`，`duplicate: true`，假上游没有收到新的请求

#### Scenario: 碰腾讯之前出错，键已释放

- **WHEN** 发送在碰腾讯之前就内部出错（测试里在预占之后、第一次向假上游发请求之前注入），项目用同一个键再发
- **THEN** 第一次返回 500 `internal`，预占的条数全部退回；第二次当成新的发送，正常发出

#### Scenario: 第一次的发送线程卡住，第二个请求等不到

- **WHEN** 第一次发送的线程超出了它自己的预算（测试里让它阻塞在发送锁里、不受超时约束），第二个同键请求等了自己的 `send.timeout_seconds`
- **THEN** 第二个返回 `unconfirmed` / `duplicate_in_flight`，`duplicate: true`，不占条数，不碰腾讯

#### Scenario: 重放带的是现在的余量

- **WHEN** 第一次发完时 `quota.left` 是 8，之后别的项目又发了几条，项目用同一个键重放
- **THEN** 返回的 `status` 与第一次相同，`quota.left` 是现在的值

#### Scenario: 键太长

- **WHEN** `dedupe_key` 有 129 个字符
- **THEN** 400 `bad_request`，没有发任何东西

### Requirement: 回复

`POST /v1/replies` MUST 接受 `handle`（取话时给的回复句柄，见 `hub-inbox`）、`text`，以及可选的 `awaiting_confirmation`（布尔，默认 false）、`confirm_seconds`（正整数，默认取 `routing.confirm_seconds`，最大 1800，越界 400 `bad_request`）；校验、拆条、检查顺序、判定送达、返回格式与发消息完全一致（`priority`、`dedupe_key`、`on_closed` 不适用）。区别只有三处：

- 用被回复那条消息的对话凭证发；
- 条数记在"回复"一类里，优先于推送（见 `hub-quota`）；
- `awaiting_confirmation: true` 表示这句回复在等使用者回"对"之类的确认话，服务据此把之后短短的确认话交给这个项目（见 `hub-inbox`）。结果的 `parts_sent` 至少 1（`sent`、`unconfirmed`、`partial`）时记下等待——拿不准有没有到时，多记比漏记安全，标记最多留 `confirm_seconds`；`rejected`、`failed`、`skipped` 不记。另外，如果在被回复的那句话之后，这个项目又收到过新的话，也不记（项目自己已经在处理新话了；"之后"按 `(at_ms, 入库先后)` 比较，与取话的排序一致，见 `hub-inbox`）。

句柄找不到、属于别的项目：404 `unknown_handle`（两种情况一模一样）；句柄过期（见 `hub-inbox`）：410 `handle_expired`；同一个句柄成功回复（`parts_sent` ≥ 1）满 `reply.max_per_handle`（默认 5）次以后再回复，429 `reply_limit`；被拒绝或失败的回复不占次数。

#### Scenario: 回复用那句话自己的凭证

- **WHEN** 使用者先后发了 A、B 两句话，项目拿 A 的句柄回复
- **THEN** 假上游收到的 `context_token` 是 A 那句话的，不是最近一句 B 的

#### Scenario: 等确认

- **WHEN** 项目回复时带 `awaiting_confirmation: true`，发送成功
- **THEN** 之后 10 分钟内，使用者回"对"，这句话交给这个项目（见 `hub-inbox` 的场景）

#### Scenario: 回复没发出去不记等待

- **WHEN** 带 `awaiting_confirmation: true` 的回复因为窗口过期 `rejected`
- **THEN** 不记"等确认"，"对"之后仍归默认项目

#### Scenario: 未确认也记等待

- **WHEN** 带 `awaiting_confirmation: true` 的回复得到 `unconfirmed`
- **THEN** 记了等待（可能已经到了），10 分钟内使用者回"对"仍交给这个项目

#### Scenario: 回复期间又来了新话

- **WHEN** 项目对话 A 回复（带 `awaiting_confirmation`）还在发送，使用者的新话 B 已经交给了这个项目，回复才发完
- **THEN** 不记等待；之后使用者回"对"，按没有人在等处理

#### Scenario: 回复次数用完

- **WHEN** 同一个句柄已经成功回复 5 次，再回复一次；另一个句柄第一次回复因为窗口过期被拒绝，随后再回
- **THEN** 前者 429 `reply_limit`；后者的被拒不占次数

#### Scenario: confirm_seconds 越界

- **WHEN** 回复带 `confirm_seconds: 3600`
- **THEN** 400 `bad_request`，没有发任何东西

### Requirement: 消息正文不落盘

服务 MUST NOT 把任何消息的正文（发出的、收到的）写进日志、数据库的长期记录或错误信息。发送记录只存：编号、项目、类型（推送或回复）、优先级、去重键的摘要、条数、各条的结果、原因码和中文原因（不含正文）、字数、时间。收到的话在项目确认、过期、作废（三种终态）时 MUST 立刻从数据库里清掉正文，只留一条不带正文的记录；对话凭证在过期、作废时同时清掉，确认了的话留到回复句柄失效（见 `hub-inbox`）。服务自己处理的话（"服务 状态"之类、没地方可去的话）不存正文。数据库的每个连接 MUST 打开 `PRAGMA secure_delete = ON`，被清掉的内容不留在空闲页里。日志里写"收到一条消息，N 个字"这类信息，不写内容。

#### Scenario: 发完以后找不到正文

- **WHEN** 项目发一条含独特字符串 `ZZ-UNIQUE-77` 的消息，发送完成后做一次 `PRAGMA wal_checkpoint(TRUNCATE)`
- **THEN** 数据库文件和日志文件里都搜不到这个字符串

#### Scenario: 取走的话清掉正文

- **WHEN** 使用者的一句话含独特字符串，项目取走并确认以后（做一次检查点）
- **THEN** 数据库文件里搜不到字符串，该记录的正文是空的；对话凭证还在，直到回复句柄失效

#### Scenario: 过期和作废的话也清掉

- **WHEN** 一条话放到过期，另一条话被租出 5 次都没确认而作废
- **THEN** 两条的正文和对话凭证都已清空，字符串搜不到

#### Scenario: 服务自己处理的话不存正文

- **WHEN** 使用者发"服务 状态"和一句没有地方可去的话（含独特字符串）
- **THEN** 数据库里搜不到这两句话的正文

### Requirement: 错误格式和 HTTP 状态码

所有非 200 的响应，正文 MUST 是 `{"error": {"code": "<英文代码>", "message": "<中文说明>"}}`，`message` 不含请求内容和任何凭证；429 另带 `Retry-After` 响应头（整数秒）。不存在的路径返回 404 `not_found`，格式相同。只有局域网规则拒绝的 403（见 `hub-deploy`）例外：正文为空，不记请求内容（与 ashare-quant 的网页控制台一致），客户端按 `HubAuthError(code=None)` 处理。全部状态码和代码：

| HTTP | `error.code` | 什么时候 | 规定在 |
|---|---|---|---|
| 400 | `bad_request` | 请求体不是合法 JSON、字段类型不对（框架自带的 422 校验失败一律改成这个）、枚举值不认识、`dedupe_key` 长度不对、`wait` / `max` 为负数或不是整数（超过上限的是截断，不是错误，见 `hub-inbox`）、`confirm_seconds` 超过 1800 或不是正整数 | 各处 |
| 400 | `empty_text` | 文字去掉首尾空白后为空 | `hub-send` |
| 400 | `text_too_long` | 文字超过 `send.max_chars` | `hub-send` |
| 401 | `unauthorized` | 没带密钥、格式不对、认不出来 | `hub-projects` |
| 403 | `project_disabled` | 项目已停用 | `hub-projects` |
| 403 | `push_only` | 只推送的项目调了取话 | `hub-inbox` |
| 404 | `unknown_handle` | 回复句柄不存在或不属于调用方 | `hub-inbox` |
| 404 | `not_found` | 路径不存在（路径末尾多一个斜杠也是不存在，不做重定向） | `hub-send` |
| 405 | `method_not_allowed` | 路径存在但用错了方法（例如 `GET /v1/messages`），带 `Allow` 响应头 | `hub-send` |
| 410 | `handle_expired` | 回复句柄过期 | `hub-inbox` |
| 413 | `too_large` | 请求体超过 64 KiB | `hub-projects` |
| 429 | `rate_limited` | 每分钟请求太多 | `hub-projects` |
| 429 | `too_many_failures` | 局域网来源认证失败太多 | `hub-projects` |
| 429 | `reply_limit` | 同一个句柄回复次数用完 | `hub-send` |
| 500 | `internal` | 服务内部出错 | `hub-send` |
| 503 | `shutting_down` | 服务正在关闭 | `hub-deploy` |

注意：200 响应里的 `reason_code`（业务结果，上面"原因代码和原文"）和这里的 `error.code`（HTTP 错误）是两套，互不混用。

发送接口遇到服务内部出错：500 `internal`；已占的额度，没有碰过腾讯的退回，已经向腾讯发过请求的保留（记录标成"中断"，占着去重键）；日志里记错误类型和堆栈，不含正文。服务正在关闭：新请求 503 `shutting_down`，在发的那条发完。

#### Scenario: 错误响应的格式

- **WHEN** 项目发了一个不是 JSON 的请求体 / 请求一个不存在的路径
- **THEN** 前者 400，后者 404，正文都是 `{"error": {"code": …, "message": …}}`，message 里没有请求内容

#### Scenario: 框架自带的错误也是同一个格式

- **WHEN** 项目 `GET /v1/messages`（用错方法）；`POST /v1/messages` 的 `text` 写成数字；请求 `/v1/messages/`（多一个斜杠）
- **THEN** 依次是 405 `method_not_allowed`（带 `Allow: POST`）、400 `bad_request`（不是 422）、404 `not_found`（没有 307 重定向）；正文都是 `{"error": {"code": …, "message": …}}`

#### Scenario: 限速带等待时间

- **WHEN** 项目触发限速（见 `hub-projects`）
- **THEN** 429 `rate_limited`，响应头有整数的 `Retry-After`

#### Scenario: 内部出错额度不丢

- **WHEN** 发送的第 2 条途中服务内部抛出意外（测试里注入）
- **THEN** 返回 500 `internal`；已发出的第 1 条占的额度保留，没发的退回；重启后数据库里的账是对的
