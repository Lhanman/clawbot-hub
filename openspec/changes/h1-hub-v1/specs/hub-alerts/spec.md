# Spec Delta

## Purpose

规定服务自己什么时候要使用者知道、怎么告诉、怎么保证不重复也不漏。这些事只有服务看得见（登录失效、窗口快到期、腾讯拒绝发送、服务重启），要是让每个项目各自去提醒，就会重复、会漏、会互相矛盾。

告警只走 Bark，不走微信：微信通道正是出问题的那个，而且告警不该占那 10 条额度。

## ADDED Requirements

### Requirement: 告警渠道

告警 MUST 通过 Bark 发给使用者：`POST <服务器>/push`，JSON 里带设备码、标题、正文、分组（`alerts.group`，默认 `clawbot-hub`），做法与 ashare-quant 的 Bark 渠道一致（可以只填设备码，也可以粘贴 App 里的整个推送地址）。设备码放在 `<home>/bark.json`（权限 0600）或者环境变量 `CLAWBOT_HUB_BARK_KEY`（有环境变量时优先）；`hubctl alerts set-key` 从标准输入读设备码写入，`hubctl alerts test` 发一条测试告警并报告结果。

没有配置设备码时，告警 MUST 仍然写进事件记录（标成"未送出"），`hubctl status` 和 `hubctl doctor` 用警告提示"没有配置告警，服务出问题时不会通知你"；服务照常运行。

Bark 请求失败（超时、5xx、`code` 不是 200）时，立刻重试 3 次（间隔 1、2、4 秒）；还是失败就留在待发队列里，每 5 分钟重试一次，最多重试 1 小时，之后放弃并写一条事件。发告警 MUST NOT 阻塞收发消息：用独立的线程，每次请求最长 10 秒。

#### Scenario: 测试告警

- **WHEN** 配好设备码后执行 `hubctl alerts test`，假 Bark 服务回 `{"code":200}`
- **THEN** 假 Bark 服务收到一条标题含"测试"的告警，命令报告成功，退出码 0

#### Scenario: 没配设备码

- **WHEN** 没有 `bark.json` 也没有环境变量，登录失效
- **THEN** 事件记录里有"登录失效"（标成未送出）；`hubctl status` 提示没有配置告警；发送和收消息不受影响

#### Scenario: Bark 暂时不通

- **WHEN** 假 Bark 服务前 3 次回 500，之后恢复，5 分钟后重试
- **THEN** 告警留在待发队列，恢复后发出一次，不重复

#### Scenario: 告警里不含秘密和正文

- **WHEN** 触发所有类型的告警
- **THEN** 假 Bark 服务收到的标题、正文里找不到 `bot_token`、`context_token`、项目密钥，也找不到任何消息正文

### Requirement: 哪些事要告警

下面这些事件，服务 MUST 各发一条告警，标题统一以"ClawBot 共用服务 ·"开头，正文是一两句中文，说明发生了什么、使用者要做什么：

| 事件 | 触发 | 正文要点 |
|---|---|---|
| `login_expired` | 登录状态由正常变成失效（见 `hub-login`） | ClawBot 登录失效了：在电脑上运行 hubctl login 重新扫码 |
| `login_restored` | 失效之后恢复（只在发过 `login_expired` 之后发） | 登录已恢复 |
| `window_expiring` | 窗口快到期（见下一条） | 微信窗口还有约 N 小时到期（M月D日 HH:MM）：给 ClawBot 回一句话续期 |
| `window_blocked` | 有推送因为窗口没开 / 没收过消息 / 这个窗口的额度用完被拒绝（`window_closed`、`never_contacted`、`quota_exhausted`；`on_closed: skip` 的不算） | 有 N 个项目的微信消息没发出去（已经由它们自己走 Bark）：给 ClawBot 发一句话恢复 |
| `send_unconfirmed` | 连续 `alerts.unconfirmed_streak`（默认 3）次发送没有确认送达（`unconfirmed`，或观察档下返回里没有消息编号） | 最近 3 次发送腾讯都没有给消息编号，消息可能没到微信：看一眼手机 |
| `upstream_blocked` | 腾讯拒绝了推送、服务进入暂停（见 `hub-quota`） | 腾讯拒绝了这个窗口里的发送（代码 …），已暂停 N 分钟；规则可能变了 |
| `upstream_down` | 连续 `alerts.upstream_down_minutes`（默认 10）分钟，收消息和发消息都连不上腾讯（登录失效不算） | 连不上腾讯的 ClawBot 服务已经 N 分钟 |
| `upstream_restored` | 不通之后恢复（只在发过 `upstream_down` 之后发） | 连上了 |
| `service_restarted` | 服务启动时发现上一次不是正常退出（崩溃、被杀、断电），或者有中断的发送（见 `hub-quota`） | 共用服务重启了（上次运行到 HH:MM）；有 N 条发送中断，可能少发或重发 |
| `backup_failed` | 每日备份连续失败 | 备份失败，原因 … |

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
- **THEN** 发一条 `service_restarted`，正文含"上次运行到"的时间

#### Scenario: 连续未确认

- **WHEN** 观察档下连续 3 次发送返回里都没有消息编号，第 4 次有
- **THEN** 第 3 次之后发一条 `send_unconfirmed`；第 4 次之后连续次数归零，再有连续 3 次才会再发

#### Scenario: 腾讯连不上

- **WHEN** 假上游对所有请求都连不上，持续 11 分钟，然后恢复
- **THEN** 第 10 分钟发一条 `upstream_down`，恢复时发一条 `upstream_restored`；中间不重复发

### Requirement: 窗口快到期的提醒

提醒是为了让使用者回一句话续期，所以只在"有人在用、提醒了有意义"的时候发。`window_expiring` MUST 同时满足这些条件才发，**每个窗口最多一次**，并且所有项目合成一条（不各项目各发各的）：

- 窗口还开着，剩余不超过 `alerts.window_warn.hours_before`（默认 3）小时；
- 现在在 `alerts.window_warn.between`（默认 07:00 到 22:30，按显示时区）之内，并且今天是 `alerts.window_warn.weekdays`（默认 周一到周五）之一；
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

- **WHEN** 窗口在周六剩 3 小时，周六不在 `weekdays` 里
- **THEN** 不发

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
- `window_blocked`：一个窗口，并且距离上一条同类告警不少于 `alerts.blocked.min_interval_hours`（默认 12）小时。
- `send_unconfirmed`：一段连续未确认的时间，一次确认送达后重置。
- `upstream_down` / `upstream_restored`：一段连不上的时间。
- `service_restarted`：10 分钟内最多一次（防止崩溃后反复重启时刷屏）。
- `backup_failed`：一天一次。

#### Scenario: 重启不重复

- **WHEN** 登录失效告警已发出，服务重启，登录仍然失效
- **THEN** 不再发

#### Scenario: 崩溃循环不刷屏

- **WHEN** 服务在 5 分钟里被反复杀掉并重新拉起 4 次
- **THEN** 只发一条 `service_restarted`
