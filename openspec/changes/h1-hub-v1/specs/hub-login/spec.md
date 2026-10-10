# Spec Delta

## Purpose

规定谁来登录 ClawBot、登录凭证放哪里、怎么扫码、怎么把已有的凭证接过来、登录失效了怎么办。腾讯那一侧的接口只有服务这一个程序碰，各项目拿不到任何腾讯的凭证。

## ADDED Requirements

### Requirement: 只有服务碰腾讯的接口

服务进程 MUST 是唯一调用腾讯 iLink 接口（`getupdates`、`sendmessage`、`get_bot_qrcode`、`get_qrcode_status`，以及 `official` 档位下的 `notifystart` / `notifystop`）的程序。登录凭证（`bot_token`）、对话凭证（`context_token`）、收消息进度（`get_updates_buf`）MUST NOT 出现在任何接口的返回、命令行输出、日志和错误信息里；上游的报错信息里带着它们时，先换成 `***` 再使用。

#### Scenario: 项目拿不到凭证

- **WHEN** 登记了一个项目，用它的密钥调遍所有 `/v1` 接口（发消息、取话、回复、状态、健康检查）
- **THEN** 所有返回内容里都找不到 `bot_token`、`context_token`、`get_updates_buf` 的值

#### Scenario: 上游报错带着凭证

- **WHEN** 假上游的错误信息里原样带着当前的 `bot_token`
- **THEN** 日志和返回给项目的原因里是 `***`，找不到原文

### Requirement: 单实例

`hubctl serve` 启动时 MUST 对数据目录下的 `hub.lock` 非阻塞地拿一把排他锁（`fcntl.flock`），拿不到就提示"已经有一个在运行（进程号 N）"并以退出码 2 结束，不收消息、不监听端口、不向腾讯发任何请求。进程退出（含被杀）时系统自动放锁，不会留下僵尸锁。

#### Scenario: 启动两次

- **WHEN** 已经有一个 `hubctl serve` 在运行，又启动一个
- **THEN** 第二个提示"已经有一个在运行"，退出码 2，不监听端口，假上游没有收到任何请求

#### Scenario: 上一个被强行杀掉

- **WHEN** 上一个 `hubctl serve` 被 `kill -9`，再启动一个
- **THEN** 能正常拿到锁并启动

### Requirement: 凭证文件

登录凭证放在 `<home>/account.json`（权限 0600，`<home>` 默认 `~/.clawbot-hub`），字段：`bot_token`、`base_url`、`bot_id`、`user_id`（使用者在腾讯那边的编号，也是"本人"的依据）、`bound_at`、`updates_buf`（收消息进度）。写文件 MUST 先写临时文件再改名，临时文件创建时就是 0600；读写在 `<home>/account.json.lock` 上加锁。使用者最近一条消息的对话凭证和窗口里的计数 MUST NOT 放在这个文件里（它们在数据库里，见 `hub-quota`、`hub-inbox`）。

文件读不出来（不是 JSON、缺字段）时，服务和命令行报"account.json 读不出来：重新运行 hubctl login"，不带文件内容。

#### Scenario: 权限

- **WHEN** 扫码成功后写凭证文件
- **THEN** 文件权限是 0600；写的过程中不会出现权限更宽的临时文件

#### Scenario: 文件坏了

- **WHEN** `account.json` 内容不是合法的 JSON
- **THEN** `hubctl status` 提示读不出来并给出重新登录的办法，输出里没有文件的内容

### Requirement: 还没绑定也能启动

`account.json` 不存在是正常状态（刚装好、还没扫码）：服务 MUST 照常启动并开放接口，但不向腾讯收消息；项目发消息得到 `failed`、原因码 `login_missing`、原因"还没绑定微信 ClawBot：运行 hubctl login"。服务每 5 秒看一次凭证文件有没有出现，出现了就开始收消息，不用重启。这样 launchd 不会因为没绑定而反复拉起。

#### Scenario: 没绑定时发消息

- **WHEN** 没有 `account.json`，项目调用发消息
- **THEN** 返回 `failed` / `login_missing`，假上游没有收到任何请求

#### Scenario: 启动后才扫码

- **WHEN** 服务在没绑定的状态下运行，另一个终端 `hubctl login` 成功
- **THEN** 5 秒内服务开始收消息，不用重启

### Requirement: 扫码登录

`hubctl login` MUST 走腾讯的二维码流程：取二维码（`bot_type=3`），在终端里画出来并存一张 PNG（`<home>/clawbot-login.png`，0600，登录结束后删掉），然后轮询扫码状态，按官方协议文档处理这些状态：

| 状态 | 做法 |
|---|---|
| `wait`、`scaned` | 继续等（每秒查一次，查失败等 2 秒再查） |
| `confirmed` | 写凭证文件（见下），提示"绑定成功。现在在微信里给 ClawBot 发任意一句话，之后 24 小时内它才能给你发消息" |
| `expired` | 换一张新二维码，提示"二维码过期了，换一张新的"；连续 5 张都没扫就以非 0 结束，凭证文件不变 |
| `need_verifycode` | 提示输入微信里显示的验证码，带上 `verify_code` 再查 |
| `verify_code_blocked` | 验证码错太多次：提示重新运行，以非 0 结束 |
| `scaned_but_redirect` | 之后改用返回的 `redirect_host` 查状态 |
| `binded_redirect` | 提示"这个 ClawBot 已经绑定在当前实例上"，以非 0 结束，凭证文件不变 |

一张二维码最多等 180 秒（服务端约 2 分钟回"过期"）。扫码期间 MUST 在数据目录里放一个标记文件 `login.pending`，结束（成功、失败、中断）时删掉。登录成功时 MUST 清掉旧的收消息进度（`updates_buf` 置空），并把数据库里"使用者最近一条消息"的窗口信息作废（新绑定后旧的对话凭证不再有效）。

#### Scenario: 一次扫成

- **WHEN** 假上游依次回 `wait`、`scaned`、`confirmed`
- **THEN** 写入凭证文件，输出绑定成功的提示，退出码 0，`login.pending` 已删

#### Scenario: 二维码过期换新的

- **WHEN** 第一张二维码回 `expired`，第二张回 `confirmed`
- **THEN** 提示换了新码，最终绑定成功

#### Scenario: 连续五张都没扫

- **WHEN** 五张二维码都回 `expired`
- **THEN** 提示"连续 5 张二维码都没扫，绑定没完成"，退出码非 0，已有的凭证文件原样不动，`login.pending` 已删

#### Scenario: 要验证码

- **WHEN** 状态回 `need_verifycode`，使用者输入 `123456`
- **THEN** 下一次查询带着 `verify_code=123456`

#### Scenario: 已经绑定在这个实例上

- **WHEN** 状态回 `binded_redirect`
- **THEN** 提示已经绑定，退出码非 0，凭证文件不变

#### Scenario: 中途按 Ctrl-C

- **WHEN** 等待扫码时被中断
- **THEN** 凭证文件不变，`login.pending` 和二维码图片都已删除

### Requirement: 登录后服务自动换上新凭证

服务运行时 MUST 在收消息循环的每一轮开头（至少每 5 秒）检查 `account.json` 有没有变（修改时间和内容摘要），变了就重新读取，换用新凭证，清掉"登录失效"状态，并按 `hub-alerts` 发"已恢复"（如果之前报过失效）。

收到 `-14` 时 MUST 先看凭证文件有没有变：变了就用新凭证重试一次，再决定算不算失效；`login.pending` 存在时（正在扫码），或者凭证文件在最近 60 秒内变过，收到的 `-14` 不报警，等新凭证换上。新绑定生效时腾讯会让旧凭证失效，不能因此报一条"假的登录失效"。

#### Scenario: 服务运行中重新扫码

- **WHEN** 服务处于"登录失效"状态，另一个终端 `hubctl login` 成功写入新凭证
- **THEN** 5 秒内服务用新凭证收消息，状态变回正常，发一条"登录已恢复"的 Bark

#### Scenario: 扫码期间的 -14 不报警

- **WHEN** 正在 `hubctl login`（`login.pending` 存在），服务正好收到一次 `-14`
- **THEN** 不发失效告警；扫码成功换上新凭证后一切正常，没有"失效"和"恢复"的 Bark

### Requirement: 导入已有的凭证

`hubctl import-credentials <文件>` MUST 能接过 ashare-quant 的 `data/notify/clawbot.json`（字段 `bot_token`、`base_url`、`bot_id`、`user_id`、`bound_at`、`updates_buf`、`context_token`、`context_at_ms`、`sent`）：校验必需字段（`bot_token`、`user_id`），写成 `account.json`；把 `context_token`、`context_at_ms` 作为当前窗口，`sent` 作为这个窗口里已经用掉的条数（记在名为 `_import` 的占位项目名下），一起写进数据库，这样迁移不会让额度凭空多出来；输出只写摘要（`bot_id` 末 4 位、`user_id` 打码、绑定时间、窗口还剩多久、已用几条），不写任何凭证。

已经有 `account.json` 时拒绝，加 `--replace` 才覆盖（覆盖前把旧文件备份成 `account.json.bak-<时间>`，权限 0600）。默认不联网，加 `--probe` 才用导入的凭证向腾讯收一次消息确认凭证还有效。服务正在运行时 MUST 拒绝导入（避免两个程序同时收消息），提示先停服务。

#### Scenario: 迁移时窗口接得上

- **WHEN** 导入的文件里 `context_at_ms` 是 3 小时前、`sent` 是 4
- **THEN** `hubctl status` 显示窗口还剩约 21 小时、已用 4 条

#### Scenario: 已经有凭证

- **WHEN** `account.json` 已经存在，不带 `--replace` 导入
- **THEN** 拒绝，提示用 `--replace`，文件不变

#### Scenario: 服务在跑

- **WHEN** 服务正在运行时导入
- **THEN** 拒绝，提示先停服务，文件不变

#### Scenario: 导入文件缺字段

- **WHEN** 文件里没有 `bot_token`
- **THEN** 报缺哪个字段，不写 `account.json`，输出里没有文件内容

### Requirement: 登录失效

腾讯任何一次返回里 `ret` 或 `errcode` 等于 `-14` 都 MUST 当作登录失效（先按"登录后服务自动换上新凭证"排除正在换凭证的情况）：把登录状态置为"失效"（写进数据库，重启后保持），此后：

- 不再调 `sendmessage`：项目发消息立刻得到 `failed`、原因码 `login_expired`、原因"ClawBot 登录失效了：在电脑上运行 hubctl login 重新扫码"；
- 收消息改成每 600 秒探测一次（调一次 `getupdates`）；探测成功（不再是 -14）就恢复正常；
- 按 `hub-alerts` 发一次 Bark（同一次失效只发一次）。

#### Scenario: 发消息时发现失效

- **WHEN** 假上游对 `sendmessage` 回 `ret=-14`
- **THEN** 这次发送返回 `failed` / `login_expired`；之后的发送不再访问假上游就直接返回同样的结果；发出一条失效告警

#### Scenario: 收消息时发现失效

- **WHEN** 假上游对 `getupdates` 回 `errcode=-14`
- **THEN** 登录状态变为失效，收消息进入 600 秒一次的探测；探测时假上游恢复正常，状态恢复，发出"已恢复"告警

#### Scenario: 重启后仍是失效

- **WHEN** 服务在失效状态下被重启
- **THEN** 启动后仍是失效状态，不发重复的失效告警

### Requirement: 上游请求的形式

访问腾讯接口的请求形式 MUST 可以用配置 `upstream.profile` 切换，默认 `legacy`：

- `legacy`：和 ashare-quant 现在发的完全一致——请求头 `Content-Type: application/json`、`AuthorizationType: ilink_bot_token`、`Authorization: Bearer <bot_token>`、`X-WECHAT-UIN`（随机 uint32 的十进制字符串再 base64）；请求体里 `base_info` 只有 `channel_version: "1.0.2"`。发消息的消息体字段（`from_user_id` 为空、`to_user_id`、`client_id`、`message_type: 2`、`message_state: 2`、`context_token`、`item_list: [{type: 1, text_item: {text}}]`）一致。
- `official`：按腾讯官方协议文档（openclaw-weixin 2.4.8）补齐——多带 `iLink-App-Id: bot`、`iLink-App-ClientVersion`（版本号按 `0x00MMNNPP` 编成十进制字符串，2.4.8 是 `132104`）、`base_info.channel_version: "2.4.8"` 和 `base_info.bot_agent`（`clawbot-hub/<版本>`），服务启动时调 `notifystart`、正常退出时调 `notifystop`（失败只记警告，不影响启动和退出）；取二维码改用 `POST`，请求体 `{"local_token_list": []}`（`legacy` 仍用 `GET` 加参数，两种都已知能用）。

两档的请求都 MUST 经同一个上游适配层发出（见设计），这样协议变了只改一处。

#### Scenario: 默认和现在一致

- **WHEN** 用默认配置发一条消息
- **THEN** 假上游收到的请求头、请求体字段与 ashare-quant `tests/notify` 里记录的 `sendmessage` 请求逐项相同（`client_id`、`X-WECHAT-UIN` 这类随机值只比格式）

#### Scenario: 切到官方档

- **WHEN** 配置 `upstream.profile: official`
- **THEN** 请求头多出 `iLink-App-Id` 和 `iLink-App-ClientVersion`（值 `132104`），`base_info` 里是 2.4.8 和 `bot_agent`，启动时假上游收到一次 `notifystart`

#### Scenario: notifystart 失败不挡启动

- **WHEN** `official` 档下 `notifystart` 返回错误
- **THEN** 只记一条警告，服务照常开始收消息
