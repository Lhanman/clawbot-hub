# Spec Delta

## Purpose

规定服务自己什么时候要使用者知道、怎么告诉、怎么保证不重复也不漏。这些事只有服务看得见（登录失效、窗口快到期、腾讯拒绝发送、服务重启），要是让每个项目各自去提醒，就会重复、会漏、会互相矛盾。

告警只走 Bark，不走微信：微信通道正是出问题的那个，而且告警不该占那 10 条额度。

## ADDED Requirements

### Requirement: 告警渠道

告警 MUST 通过 Bark 发给使用者：`POST <服务器>/push`，JSON 里带设备码、标题、正文、分组（`alerts.group`，默认 `clawbot-hub`）。请求格式和设备码的写法与 ashare-quant 的 Bark 渠道一致（可以只填设备码，也可以粘贴 App 里的整个推送地址）；重试的做法不同，见下一条。设备码放在 `<home>/bark.json`（权限 0600）或者环境变量 `CLAWBOT_HUB_BARK_KEY`（两处都有时环境变量优先）；`hubctl alerts set-key` 从标准输入读设备码写入，`hubctl alerts test` 发一条测试告警并报告结果。

没有配置设备码时，告警 MUST 仍然写进事件记录（标成"未送出"），`hubctl status` 和 `hubctl doctor` 用警告提示"没有配置告警，服务出问题时不会通知你"；服务照常运行。

发告警 MUST NOT 阻塞收发消息：用独立的线程，每次请求最长 `alerts.bark_timeout_seconds`（默认 10）秒。

#### Scenario: 测试告警

- **WHEN** 配好设备码后执行 `hubctl alerts test`，假 Bark 服务回 `{"code":200}`
- **THEN** 假 Bark 服务收到一条标题含"测试"的告警，命令报告成功，退出码 0

#### Scenario: 没配设备码

- **WHEN** 没有 `bark.json` 也没有环境变量，登录失效
- **THEN** 事件记录里有"登录失效"（标成未送出）；`hubctl status` 提示没有配置告警；发送和收消息不受影响

#### Scenario: 环境变量优先

- **WHEN** `bark.json` 里的设备码是 K1，环境变量 `CLAWBOT_HUB_BARK_KEY` 是 K2，触发一条告警
- **THEN** 假 Bark 服务收到的请求用的是 K2；去掉环境变量重启后用 K1

#### Scenario: 告警里不含秘密和正文

- **WHEN** 触发所有类型的告警
- **THEN** 假 Bark 服务收到的标题、正文里找不到 `bot_token`、`context_token`、项目密钥，也找不到任何消息正文

#### Scenario: Bark 慢不拖累收发

- **WHEN** 假 Bark 服务收到请求后 30 秒不回话，同时有项目在发消息、使用者在发话
- **THEN** 发送和收消息照常，不受影响；这次 Bark 请求在 10 秒时超时

### Requirement: Bark 失败怎么办

Bark 请求失败时 MUST 分两类处理：

- **暂时的**（超时、连不上、HTTP 429、5xx）：立刻重试 `alerts.bark_retries`（默认 3）次，间隔从 1 秒起每次翻倍（1、2、4 秒），也就是最多 4 次尝试；还是失败就留在待发队列里（存在数据库里，重启不丢），每 `alerts.queue_retry_minutes`（默认 5）分钟重试一次，最多重试 `alerts.queue_give_up_minutes`（默认 60）分钟，之后放弃并写一条事件。
- **被拒绝的**（Bark 服务器明确回了错误：HTTP 4xx（429 除外），或者 2xx 但 JSON 里的 `code` 不是 200，常见原因是设备码填错）：不重试，写一条事件（说明被拒绝，不含设备码），`hubctl doctor` 用警告提示检查设备码。

这两类和 ashare-quant 的 Bark 渠道的做法一致，区别只有一点：ashare-quant 没有待发队列，暂时失败重试 3 次就放弃。

#### Scenario: Bark 暂时不通

- **WHEN** 假 Bark 服务对前 4 次请求（首次加 3 次立即重试，时刻约 0、1、3、7 秒）回 500，之后恢复
- **THEN** 告警留在待发队列，5 分钟后的重试发出，只发出一次，不重复

#### Scenario: 立即重试就成功

- **WHEN** 假 Bark 服务对前 3 次请求回 500，第 4 次回 `{"code":200}`
- **THEN** 告警在约 7 秒时发出，没有进待发队列，之后不会再发

#### Scenario: 重启不丢待发的告警

- **WHEN** 告警在待发队列里，服务重启，此时 Bark 服务已恢复
- **THEN** 重启后的下一次重试把它发出，只发一次

#### Scenario: 超过一小时放弃

- **WHEN** 假 Bark 服务一直回 500，持续 61 分钟
- **THEN** 这条告警被放弃，事件里有一条记录；不再重试

#### Scenario: 设备码错了不重试

- **WHEN** 假 Bark 服务回 `{"code":400,"message":"failed to get device token"}`
- **THEN** 只请求了 1 次，没有进待发队列，事件里有一条"被拒绝"的记录，`hubctl doctor` 有警告，事件和输出里没有设备码

### Requirement: 哪些事要告警

下面这些事件，服务 MUST 各发一条告警，标题统一以"ClawBot 共用服务 ·"开头，正文是一两句中文，说明发生了什么、使用者要做什么：

| 事件 | 标题（"·"后面） | 触发 | 正文要点 |
|---|---|---|---|
| `login_expired` | 登录失效 | 登录状态由正常变成失效（见 `hub-login`） | ClawBot 登录失效了：在电脑上运行 hubctl login 重新扫码 |
| `login_restored` | 登录已恢复 | 失效之后恢复（只在发过 `login_expired` 之后发） | 登录已恢复 |
| `window_expiring` | 窗口快到期 | 窗口快到期（见下面的"窗口快到期的提醒"） | 微信窗口还有约 N 小时到期（M月D日 HH:MM）：给 ClawBot 回一句话续期 |
| `window_blocked` | 微信消息没发出去 | 有推送因为窗口没开 / 没收过消息 / 这个窗口的额度用完被拒绝（`window_closed`、`never_contacted`、`quota_exhausted`；`on_closed: skip` 的不算） | 有 N 个项目的微信消息没发出去（已经由它们自己走 Bark）：给 ClawBot 发一句话恢复 |
| `send_unconfirmed` | 发送未确认 | 连续 `alerts.unconfirmed_streak`（默认 3）次 `sendmessage` 没有确认送达 | 最近 3 次发送腾讯都没有给消息编号，消息可能没到微信：看一眼手机（数字取配置值） |
| `upstream_blocked` | 腾讯拒绝发送 | 腾讯明确拒绝了推送、服务进入暂停（见 `hub-quota`） | 腾讯拒绝了这个窗口里的发送（代码 …），已暂停 N 分钟；规则可能变了 |
| `upstream_down` | 连不上腾讯 | 见下面的定义 | 连不上腾讯的 ClawBot 服务已经 N 分钟 |
| `upstream_restored` | 已连上腾讯 | 不通之后恢复（只在发过 `upstream_down` 之后发） | 连上了 |
| `service_restarted` | 服务重启 | 服务启动时发现上一次不是正常退出（崩溃、被杀、断电、关停时间用完强行退出），或者有中断的发送（见 `hub-quota`）；启动时只登记，由告警线程发出（见 `hub-deploy` 的启动顺序） | 共用服务重启了（上次运行到 HH:MM）；有 N 条发送中断，可能少发或重发 |
| `backup_failed` | 备份失败 | 每日备份失败，10 分钟后重试仍失败 | 备份失败，原因 … |

触发条件的细节（实现和测试都按这里）：

- **`upstream_down`**：服务记一个"最近一次与腾讯完成往返的时间"——收到了完整的 HTTP 响应，状态码不是 429 / 5xx（长轮询读超时是服务端正常挂着的结果，也算完成往返；待真机确认），收和发不分。只在登录状态是正常时判断（登录失效、没绑定时不收消息，不算连不上；-14 是一次完成的往返，所以也不算）。距离这个时间已经 `alerts.upstream_down_minutes`（默认 10）分钟，并且最近一次出错是连接类的（连不上、连接超时、TLS 失败、429、5xx）时告警；刚启动还没有往返过的，从启动时刻算起。
- **`send_unconfirmed`**：按一次 `sendmessage` 请求算，不按消息算。"没有确认送达"指：腾讯回了成功但没有消息编号（`observe` 档也算），或者请求发出后读超时没有回话。拿到消息编号的请求把连续次数归零；`rejected`、`failed`（没发出去的）既不计数也不归零。连续次数刚好达到 `alerts.unconfirmed_streak` 时发一条，之后要先有一次确认送达，再连续那么多次才会再发。
- **`window_blocked`**：不在被拒绝的那一刻发，由 30 秒一轮的定时任务汇总。素材就是发送记录里被这三种原因拒绝的推送（不另存队列；记录里没有窗口时，窗口一律记为固定的 `none`，有窗口用窗口编号）。每一轮：取出自上一条同类告警发出以来（没发过就是全部）的这类拒绝，去掉窗口已经告警过的（去重单位，见"去重要记下来"），没有剩的就什么也不发；有剩的，**但距上一条同类告警不足 `alerts.blocked.min_interval_hours`（默认 12）小时，这一轮也不发——被压住的拒绝不丢，留在记录里继续累计**，间隔一满的下一轮就发。发的时候 N = 这些拒绝涉及的**不同项目**数，发出后把涉及的窗口记为已告警。
- **`backup_failed`**：备份失败后 10 分钟重试一次，仍失败才告警；一天一条。
- **`service_restarted`**：正文里的"上次运行到 HH:MM"取 `run.json` 里的最后心跳时间（见 `hub-deploy`），没有就不写这半句。

正常退出（`SIGTERM`、`SIGINT`、`hubctl serve` 收到停止信号）MUST 记"正常退出"标记；下次启动看到标记就不发 `service_restarted`。手动停了再起不是出事。

#### Scenario: 登录失效只告警一次

- **WHEN** 假上游对 `getupdates` 回 -14，探测了 3 次仍是 -14
- **THEN** 假 Bark 服务只收到一条"登录失效"

#### Scenario: 恢复以后告知

- **WHEN** 上面的失效之后，重新扫码成功
- **THEN** 假 Bark 服务收到一条"登录已恢复"；再次失效时又能发一条"登录失效"

#### Scenario: 没发过失效就不发恢复

- **WHEN** 扫码绑定一个全新的账号（之前没有失效记录）
- **THEN** 没有"登录已恢复"告警

#### Scenario: 正常停掉再启动

- **WHEN** 给服务发 `SIGTERM`，等它退出，再启动
- **THEN** 没有 `service_restarted` 告警

#### Scenario: 被杀以后启动

- **WHEN** 服务被 `kill -9`，再启动
- **THEN** 发一条 `service_restarted`，正文含"上次运行到"的时间（来自最后心跳）

#### Scenario: 连续未确认

- **WHEN** 观察档下连续 3 次 `sendmessage` 返回里都没有消息编号，第 4 次有
- **THEN** 第 3 次之后发一条 `send_unconfirmed`，正文里的次数是 3；第 4 次之后连续次数归零，再有连续 3 次才会再发

#### Scenario: 被拒绝的发送不影响连续次数

- **WHEN** 连续 2 次没有消息编号，中间插进一次被腾讯拒绝（`rejected`）的发送，再有 1 次没有编号
- **THEN** 连续次数是 3，在最后这次之后发 `send_unconfirmed`

#### Scenario: 腾讯连不上

- **WHEN** 假上游对所有请求都连不上，持续 11 分钟，然后恢复
- **THEN** 第 10 分钟发一条 `upstream_down`，恢复时发一条 `upstream_restored`；中间不重复发

#### Scenario: 腾讯回 5xx 也算不通

- **WHEN** 假上游对所有请求回 HTTP 502，持续 11 分钟
- **THEN** 发一条 `upstream_down`

#### Scenario: 登录失效不算连不上

- **WHEN** 假上游对 `getupdates` 回 -14（完整的 HTTP 200），服务按失效处理并每 10 分钟探测一次，持续 3 小时
- **THEN** 只有一条"登录失效"，没有 `upstream_down`

#### Scenario: 没绑定不算连不上

- **WHEN** 服务在没有凭证的状态下运行 3 小时
- **THEN** 没有 `upstream_down`

#### Scenario: 腾讯拒绝发送

- **WHEN** 假上游对一次推送明确回 `ret: 5`，服务进入暂停
- **THEN** 发一条"腾讯拒绝发送"，正文里有代码 5 和暂停的分钟数；同一个窗口里暂停之后再被拒绝不重复发

#### Scenario: 推送被拒的汇总

- **WHEN** 窗口已过期，项目 A 和项目 B 的推送都被拒绝（`window_closed`）；A 还有一条带 `on_closed: skip` 的推送被跳过；30 秒的定时任务跑一轮
- **THEN** 发一条"微信消息没发出去"，N 是 2（跳过的不算）

#### Scenario: 同一个窗口只告警一次

- **WHEN** 上面告警发出后 13 小时，项目 D 的推送也因为同一个窗口过期被拒绝，使用者一直没说话
- **THEN** 不再发（去重单位是同一个窗口）；使用者发话开了新窗口、新窗口又用完额度时才会再发

#### Scenario: 换了窗口也要隔 12 小时

- **WHEN** 上面告警发出（时刻 T）后 5 小时，使用者发话开了新窗口，新窗口的额度很快用完，项目 A 的推送被拒绝（`quota_exhausted`）；T 后 13 小时，使用者又发话开了第三个窗口，额度又用完，项目 B 的推送被拒绝
- **THEN** T 后 5 小时那一轮不发（距上一条不足 12 小时），A 的拒绝记录留着不丢；T 后 12 小时的第一轮发出，N 是 1（项目 A）；T 后 13 小时那次不发（距刚发的才 1 小时），到 T 后 24 小时的第一轮发出，N 是 1（项目 B）

#### Scenario: 从没收到过消息

- **WHEN** 数据库里没有任何窗口（`never_contacted`），项目的推送被拒绝；10 分钟后另一个项目也被拒绝
- **THEN** 只发一条（去重单位 `none`）

#### Scenario: 备份失败

- **WHEN** 每日备份因为磁盘满失败，10 分钟后重试仍失败；当天晚些时候备份又失败
- **THEN** 重试失败之后发一条"备份失败"，正文里有原因；当天不再发

#### Scenario: 备份重试成功就不告警

- **WHEN** 每日备份失败，10 分钟后重试成功
- **THEN** 没有告警，事件里有一条失败和一条成功的记录

### Requirement: 窗口快到期的提醒

提醒是为了让使用者回一句话续期，所以只在"有人在用、提醒了有意义"的时候发。`window_expiring` MUST 同时满足这些条件才发，**每个窗口最多一次**，并且所有项目合成一条（不各项目各发各的）：

- 窗口还开着，剩余不超过 `alerts.window_warn.hours_before`（默认 3）小时；
- 现在在 `alerts.window_warn.between`（默认 07:00 到 22:30，按显示时区）之内，并且今天是 `alerts.window_warn.weekdays`（默认 `[1, 2, 3, 4, 5]`）之一，星期用 ISO 编号（周一是 1，周日是 7，实现用 `isoweekday()`）；
- 这个窗口里有项目发过推送（回复不算）——没人在用微信推送的窗口过期了也不用提醒；
- 条件一时不满足（比如到点时是夜里），在窗口关闭之前每个检查周期（30 秒）再看一次，满足了就发。

使用者在提醒之后又说了话，新窗口重新开始，这条提醒不再有意义，也不补发。`alerts.window_warn.enabled` 可以关掉这个提醒。

#### Scenario: 工作日白天到点提醒

- **WHEN** 周三窗口剩 3 小时，是 15:30，窗口里项目发过推送
- **THEN** 发一条提醒，正文含窗口到期的时间

#### Scenario: 夜里到点，等天亮

- **WHEN** 窗口将在周四 09:30 到期，剩 3 小时是周四 06:30（天还没亮，不在 07:00 到 22:30 之内）
- **THEN** 06:30 不发，07:00 发一次（窗口还没关）；若窗口是 06:00 就到期，则一条都不发

#### Scenario: 周末不提醒

- **WHEN** 窗口在周六剩 3 小时，周六不在 `weekdays` 里；另一次是周日
- **THEN** 两次都不发；同样的窗口放在周一（`weekdays` 里的 1）就发

#### Scenario: 窗口里没推送过

- **WHEN** 窗口里只有使用者问、项目答的回复，没有任何推送，窗口剩 3 小时
- **THEN** 不发

#### Scenario: 每个窗口一次

- **WHEN** 同一个窗口剩 3 小时时发过提醒，之后服务重启
- **THEN** 重启以后不再发（已经提醒过这个窗口）

### Requirement: 去重要记下来

每种告警的"发过没有、哪一次"MUST 存在数据库里（告警状态表），服务重启不会重复发，也不会因为重启漏掉还没发出去的。去重的单位：

- `login_expired` / `login_restored`：一次失效（从失效开始到恢复）。
- `window_expiring`、`upstream_blocked`：一个窗口。
- `window_blocked`：一个窗口（没有窗口时用固定的 `none`）只告警一次，并且两条同类告警之间不少于 `alerts.blocked.min_interval_hours`（默认 12）小时；被间隔压住的拒绝累计到间隔一满的下一轮。"已告警窗口"记在告警状态表里（键 `window_blocked:<窗口编号或 none>`）。
- `send_unconfirmed`：一段连续未确认的时间，一次确认送达后重置。
- `upstream_down` / `upstream_restored`：一段连不上的时间。
- `service_restarted`：`alerts.restart_merge_minutes`（默认 10）分钟内最多一次（防止崩溃后反复重启时刷屏）。
- `backup_failed`：一天一次。

#### Scenario: 重启不重复

- **WHEN** 登录失效告警已发出，服务重启，登录仍然失效
- **THEN** 不再发

#### Scenario: 崩溃循环不刷屏

- **WHEN** 服务在 5 分钟里被反复杀掉并重新拉起 4 次
- **THEN** 只发一条 `service_restarted`
