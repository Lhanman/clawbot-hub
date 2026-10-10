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

**什么叫"凭证变了"**：只看 `bot_token`、`base_url`、`bot_id`、`user_id` 这四项（它们的摘要记作 `creds_digest`）；`updates_buf`、`bound_at` 的变化不算——服务每收一批消息都会改写 `updates_buf`。

**写回收消息进度**：服务把新的 `updates_buf` 写回文件时，MUST 在锁里重新读一遍文件，只有文件里的 `bot_token` 仍然等于这一轮收消息所用的那个，才改 `updates_buf`；否则（`hubctl login` 刚写了新凭证）丢弃这次写回，新登录已经把进度清空了。

文件读不出来（不是 JSON、缺字段）时，服务和命令行报"account.json 读不出来：重新运行 hubctl login"，不带文件内容；服务照常启动，登录状态是 `broken`。

#### Scenario: 权限

- **WHEN** 扫码成功后写凭证文件
- **THEN** 文件权限是 0600；写的过程中不会出现权限更宽的临时文件

#### Scenario: 文件坏了

- **WHEN** `account.json` 内容不是合法的 JSON
- **THEN** `hubctl status` 提示读不出来并给出重新登录的办法，输出里没有文件的内容

#### Scenario: 收消息进度不算凭证变了

- **WHEN** 服务连续收到几批消息，`updates_buf` 每次都变，随后收到一次 -14
- **THEN** 凭证摘要没变，立刻按登录失效处理（不会因为"文件刚被写过"而被当成换凭证）

#### Scenario: 写回时被新登录抢先

- **WHEN** 服务读出凭证、发起一轮收消息，这时 `hubctl login` 写入了新凭证（`updates_buf` 为空），收消息返回后服务要写回新的 `updates_buf`
- **THEN** 新凭证保留，`updates_buf` 仍是空的；服务随后换用新凭证

### Requirement: 还没绑定也能启动

`account.json` 不存在是正常状态（刚装好、还没扫码）：服务 MUST 照常启动并开放接口，但不向腾讯收消息；项目发消息得到 `failed`、原因码 `login_missing`、原因"还没绑定微信 ClawBot：运行 hubctl login"。凭证监视线程（见"登录后服务自动换上新凭证"）每 5 秒看一次凭证文件有没有出现，出现了就开始收消息，不用重启。这样 launchd 不会因为没绑定而反复拉起。

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

一张二维码最多等 180 秒（服务端约 2 分钟回"过期"），到点还没结果也按过期处理，换下一张。

扫码期间 MUST 在数据目录里放一个标记文件 `login.pending`，内容是 `hubctl login` 的进程号和开始时间；结束（成功、失败、中断）时删掉。如果 `hubctl login` 被 `kill -9` 或断电，标记会残留：凡是用到这个标记的地方（见下一条和 `doctor`）都要核对"那个进程号还活着、开始不到 20 分钟"，进程已经不在了就当没有这个标记。

登录成功时 MUST 按这个顺序做：

1. 先写 `account.json`：新凭证，`updates_buf` 为空（旧的收消息进度随旧凭证作废；写法见"凭证文件"）；
2. 再在**一个数据库事务里**把所有窗口作废，并把所有还没过期的回复句柄的对话凭证置空（之后用旧句柄回复得到 410 `handle_expired`）。数据库还不存在（服务从没启动过）时跳过这一步，首次启动时的比对会做同样的事。

**窗口作废**的意思：窗口表里所有还没作废的行标上 `voided = 1`、清掉 `context_token`（行留着，发送记录还指着它们）。作废的窗口既不算"开着"，也不算"使用者最近一条消息"，所以新绑定以后、使用者再发话之前，推送得到 `rejected` / `never_contacted`，`hubctl status` 显示"还没收到消息"。

先写文件、后改数据库的顺序是故意的：反过来的话，文件没写成而窗口已被作废，会白白丢掉一个还有效的窗口。两步之间 `hubctl login` 被杀也没关系：服务的凭证监视线程（运行中）和启动比对（刚启动）发现 `account.json` 的摘要和数据库里记的不一样时，会**补做同一个数据库事务**（幂等，做两遍结果一样），然后才记下新摘要（见"登录后服务自动换上新凭证"）。

#### Scenario: 一次扫成

- **WHEN** 假上游依次回 `wait`、`scaned`、`confirmed`
- **THEN** 写入凭证文件，输出绑定成功的提示，退出码 0，`login.pending` 已删

#### Scenario: 二维码过期换新的

- **WHEN** 第一张二维码回 `expired`，第二张回 `confirmed`
- **THEN** 提示换了新码，最终绑定成功

#### Scenario: 一张码等满 180 秒

- **WHEN** 假上游对一张二维码一直回 `wait`（假时钟走过 180 秒）
- **THEN** 提示换新码，取第二张二维码

#### Scenario: 连续五张都没扫

- **WHEN** 五张二维码都回 `expired`
- **THEN** 提示"连续 5 张二维码都没扫，绑定没完成"，退出码非 0，已有的凭证文件原样不动，`login.pending` 已删

#### Scenario: 要验证码

- **WHEN** 状态回 `need_verifycode`，使用者输入 `123456`
- **THEN** 下一次查询带着 `verify_code=123456`

#### Scenario: 验证码错太多次

- **WHEN** 状态回 `verify_code_blocked`
- **THEN** 提示重新运行，退出码非 0，凭证文件不变

#### Scenario: 要求改用别的域名

- **WHEN** 状态回 `scaned_but_redirect` 并带 `redirect_host: other.example`，之后回 `confirmed`
- **THEN** 之后的查询发往 `other.example`，最终绑定成功

#### Scenario: 已经绑定在这个实例上

- **WHEN** 状态回 `binded_redirect`
- **THEN** 提示已经绑定，退出码非 0，凭证文件不变

#### Scenario: 中途按 Ctrl-C

- **WHEN** 等待扫码时被中断
- **THEN** 凭证文件不变，`login.pending` 和二维码图片都已删除

#### Scenario: 残留的标记

- **WHEN** 一个 `login.pending` 里的进程号已经不存在（`hubctl login` 当年被强杀）
- **THEN** 它被当作没有；服务收到真正的 -14 时照常报登录失效

#### Scenario: 新绑定以后旧句柄不能用

- **WHEN** 项目手里有一个 10 分钟前取到的回复句柄，随后 `hubctl login` 成功重新绑定，项目用旧句柄回复
- **THEN** 410 `handle_expired`，没有向腾讯发任何东西

#### Scenario: 新绑定以后旧窗口作废

- **WHEN** 使用者 3 小时前发过话（窗口开着），随后 `hubctl login` 成功重新绑定，项目发一条推送；之后使用者在新账号下发话，项目再发一条
- **THEN** 第一条 `rejected` / `never_contacted`，假上游没有收到请求，`hubctl status` 显示还没收到消息；第二条正常发出

#### Scenario: 写完文件后被杀

- **WHEN** `hubctl login` 写好了新的 `account.json`，在做数据库那一步之前被杀（测试里在两步之间注入）；再启动服务
- **THEN** 启动比对发现摘要变了，补做同一个事务：窗口作废、旧句柄回复得到 410；之后再做一遍结果不变

### Requirement: 登录后服务自动换上新凭证

服务 MUST 有一个凭证监视线程：每 5 秒比较一次 `account.json` 的凭证摘要（见"凭证文件"）和数据库里记的，没绑定时看文件有没有出现。变了就依次：重新读取并换用新凭证；补做"登录成功"里的数据库事务（幂等）；记下新摘要和变化的时刻（`account_state.creds_changed_ms`）；清掉"登录失效"状态，并按 `hub-alerts` 发"已恢复"（如果之前报过失效）；**唤醒收消息线程**，让它马上用新凭证开始新一轮，不等 600 秒的探测间隔或错误退避。服务启动时（读完 `account.json` 之后、开始收消息之前）也做一次同样的比对，所以服务停着时做的 `hubctl login` 在启动时就生效。正在进行的那一次长轮询还在用旧凭证，最迟在它结束（不超过 `upstream.poll_timeout`）后换上新凭证。

收到 -14 时 MUST 先重新读文件：摘要变了就用新凭证重试一次（只限收消息，发送见下一段），再决定算不算失效。以下两种情形收到的 -14 不报警、不改登录状态，等新凭证换上：`login.pending` 存在且它记的进程还活着、开始不到 20 分钟（正在扫码）；`creds_changed_ms` 在最近 60 秒内。新绑定生效时腾讯会让旧凭证失效，不能因此报一条"假的登录失效"。

**发送途中**收到的 -14 不重试（那一条带的对话凭证属于旧账号，换了新凭证也发不出去）：同样先重新读文件；摘要变了，或者处于上面两种情形之一，这一条和后面的段不发、额度退回，结果是 `failed` / `login_expired`（前面已经发出去的段让整条成为 `partial`），**不改登录状态、不发告警**；都不是才把登录状态置为失效并告警（见"登录失效"）。

#### Scenario: 服务运行中重新扫码

- **WHEN** 服务处于"登录失效"状态（收消息线程正睡在 600 秒的探测间隔里），另一个终端 `hubctl login` 成功写入新凭证
- **THEN** 5 秒内服务把状态变回正常，并唤醒收消息线程：假上游在这 5 秒内就收到一次用新凭证的 `getupdates`，不用等探测间隔；发一条"登录已恢复"的 Bark

#### Scenario: 正常轮询期间换了凭证

- **WHEN** 服务正在一次长轮询中，凭证被换掉
- **THEN** 这次长轮询结束后（最迟 `upstream.poll_timeout` 秒）用新凭证收消息

#### Scenario: 服务停着时重新扫了码

- **WHEN** 服务停着，数据库里记着登录失效，期间 `hubctl login` 写入了新凭证，再启动服务
- **THEN** 启动时发现摘要变了，登录状态变回正常，之前报过失效所以发一条"已恢复"，不用等一次 600 秒的探测

#### Scenario: 扫码期间的 -14 不报警

- **WHEN** 正在 `hubctl login`（`login.pending` 存在且进程还活着），服务正好收到一次 `-14`
- **THEN** 不发失效告警；扫码成功换上新凭证后一切正常，没有"失效"和"恢复"的 Bark

#### Scenario: 刚换完凭证的 -14

- **WHEN** 凭证 30 秒前刚换过，服务收到一次 -14
- **THEN** 重新读文件、用新凭证重试一次；重试成功就没有失效；重试仍是 -14 时也不报警（60 秒内），超过 60 秒仍是 -14 才按失效处理

#### Scenario: 换凭证期间发送遇到 -14

- **WHEN** 凭证 30 秒前刚换过（窗口已作废，登录状态正常），一条发送在飞，假上游对 `sendmessage` 回 `ret=-14`
- **THEN** 这一条 `failed` / `login_expired`，额度退回；假上游对这一条只收到一次请求（不重试）；登录状态仍是正常，没有失效告警

#### Scenario: 发送遇到 -14 时凭证文件已经换了

- **WHEN** `hubctl login` 刚写好新的 `account.json`，监视线程还没来得及换上（5 秒周期内），此时一条发送收到 `-14`
- **THEN** 同样不改登录状态、不发失效告警，结果 `failed` / `login_expired`；监视线程随后换上新凭证，状态保持正常，没有"失效"和"恢复"的 Bark

### Requirement: 导入已有的凭证

`hubctl import-credentials <文件>` MUST 能接过 ashare-quant 的 `data/notify/clawbot.json`（字段 `bot_token`、`base_url`、`bot_id`、`user_id`、`bound_at`、`updates_buf`、`context_token`、`context_at_ms`、`sent`）：校验必需字段（`bot_token`、`user_id`），写成 `account.json`；把 `context_token`、`context_at_ms` 作为当前窗口，`sent` 作为这个窗口里已经用掉的条数（记在内部项目 `_import` 名下，类别是"推送"，这样不会动到回复保留），一起写进数据库，这样迁移不会让额度凭空多出来；输出只写摘要（`bot_id` 末 4 位、`user_id` 打码、绑定时间、窗口还剩多久、已用几条），不写任何凭证。

导入前 MUST 先按 `hubctl` 通用的办法判断服务是否在运行（看 `run.json`，见 `hub-cli`），在运行就拒绝并提示先停服务；没在运行才非阻塞地拿 `hub.lock`，导入期间一直持有（拿不到就也拒绝，提示"服务刚好在启动，稍后再试"）：导入没做完服务起不来（它会以退出码 2 结束，launchd 稍后重新拉起），避免两个程序同时收消息。已经有 `account.json` 时拒绝，加 `--replace` 才覆盖（覆盖前把旧文件备份成 `account.json.bak-<时间>`，权限 0600）。

写入的顺序是先数据库、后文件：在**一个数据库事务里**（`--replace` 时先把旧的窗口作废、旧句柄的凭证清掉）写窗口、`_import` 名下的已用条数，以及新凭证的摘要（启动比对因此不会把刚导入的窗口当成"凭证变了"而作废），事务提交之后才写 `account.json`。同一份文件可以重复导入，结果相同——窗口按 `context_at_ms` 认，已用条数设为 `sent`（不累加）；这样事务做完而文件没写成时，再导入一次即可。默认不联网，加 `--probe` 才用导入的凭证和导入的 `updates_buf` 向腾讯收一次消息，只看是不是 -14 和 HTTP 是否 200；`--probe` MUST NOT 处理返回的任何消息，也 MUST NOT 保存返回的新进度（保存了的话，这批消息就再也收不到了）。

#### Scenario: 迁移时窗口接得上

- **WHEN** 导入的文件里 `context_at_ms` 是 3 小时前、`sent` 是 4
- **THEN** `hubctl status` 显示窗口还剩约 21 小时、已用 4 条，其中推送 4 条、回复 0 条

#### Scenario: 已经有凭证

- **WHEN** `account.json` 已经存在，不带 `--replace` 导入
- **THEN** 拒绝，提示用 `--replace`，文件不变

#### Scenario: 服务在跑

- **WHEN** 服务正在运行（`run.json` 显示进程活着）时导入
- **THEN** 拒绝，提示先停服务，文件不变

#### Scenario: 导入期间服务起不来

- **WHEN** 导入命令正持有 `hub.lock`，另一个终端启动 `hubctl serve`
- **THEN** 服务提示已有一个在运行，退出码 2；导入结束后再启动正常

#### Scenario: 导入后启动不会作废窗口

- **WHEN** 导入成功（窗口来自文件里的 `context_at_ms`），随后启动服务
- **THEN** 启动比对发现摘要和数据库里记的一样，窗口仍然开着，没有被作废

#### Scenario: 导入做到一半被杀

- **WHEN** 数据库事务已提交、`account.json` 还没写时命令被杀（测试里注入），之后用同一份文件再导入一次
- **THEN** 导入成功；库里只有一个来自这份文件的窗口，已用条数是文件里的 `sent`，不是它的两倍

#### Scenario: 导入文件缺字段

- **WHEN** 文件里没有 `bot_token`
- **THEN** 报缺哪个字段，不写 `account.json`，输出里没有文件内容

#### Scenario: 探测不吃掉消息

- **WHEN** 导入时加 `--probe`，假上游的 `getupdates` 返回了一条使用者的消息和新的进度
- **THEN** 命令报告凭证有效；数据库里没有这条消息，`account.json` 里的 `updates_buf` 仍是导入的那个值

### Requirement: 登录失效

腾讯任何一次返回里 `ret`（没有就看 `errcode`）等于 `-14` 都 MUST 当作登录失效（判定顺序见 `hub-send`：HTTP 429 / 5xx 的响应不看响应体；先按"登录后服务自动换上新凭证"排除正在换凭证的情况）：把登录状态置为"失效"（写进数据库，重启后保持），此后：

- 不再调 `sendmessage`：项目发消息立刻得到 `failed`、原因码 `login_expired`、原因"ClawBot 登录失效了：在电脑上运行 hubctl login 重新扫码"；
- 收消息改成每 `upstream.relogin_probe_seconds`（默认 600）秒探测一次（调一次 `getupdates`）；
- 按 `hub-alerts` 发一次 Bark（同一次失效只发一次）。

**恢复**只有两个条件，满足任何一个：凭证摘要和失效时记下的不一样（重新扫了码）；或者探测的 `getupdates` 得到 HTTP 200 且 `ret`、`errcode` 都为空或 0。其他结果（连不上、HTTP 5xx、非 -14 的错误码）都保持失效，探测间隔不变，不发"已恢复"。

#### Scenario: 发消息时发现失效

- **WHEN** 假上游对 `sendmessage` 回 `ret=-14`
- **THEN** 这次发送返回 `failed` / `login_expired`；之后的发送不再访问假上游就直接返回同样的结果；发出一条失效告警

#### Scenario: 收消息时发现失效

- **WHEN** 假上游对 `getupdates` 回 `errcode=-14`
- **THEN** 登录状态变为失效，收消息进入每 600 秒一次的探测；探测时假上游恢复正常，状态恢复，发出"已恢复"告警

#### Scenario: 探测碰到别的错误不算恢复

- **WHEN** 登录失效期间，探测的 `getupdates` 回 HTTP 502（或连不上，或 `ret: 500`）
- **THEN** 仍是失效，不发"已恢复"；下一次探测仍在 600 秒后

#### Scenario: 重启后仍是失效

- **WHEN** 服务在失效状态下被重启
- **THEN** 启动后仍是失效状态，不发重复的失效告警

### Requirement: 上游请求的形式

访问腾讯接口的请求形式 MUST 可以用配置 `upstream.profile` 切换，默认 `legacy`。收发消息用 `account.json` 里的 `base_url`（没有就用 `upstream.base_url`，默认 `https://ilinkai.weixin.qq.com`）；取二维码、查扫码状态用 `upstream.base_url`（回了 `scaned_but_redirect` 之后改用返回的 `redirect_host`）。

- `legacy`：和 ashare-quant 现在发的请求一致，逐项如下——
  - 自定义的请求头恰好这 4 个（`requests` 自己带的 `Host`、`Content-Length`、`Connection`、`User-Agent`、`Accept` 这类传输层的头不算在内，测试不比较）：`Content-Type: application/json`、`AuthorizationType: ilink_bot_token`、`Authorization: Bearer <bot_token>`、`X-WECHAT-UIN`（随机 uint32 的十进制字符串再 base64，每次请求不同）。
  - `getupdates`：`POST <base_url>/ilink/bot/getupdates`，请求体只有 `get_updates_buf` 和 `base_info`；`base_info` 只有 `channel_version: "1.0.2"`。
  - `sendmessage`：`POST <base_url>/ilink/bot/sendmessage`，请求体 `{"msg": {…}, "base_info": {"channel_version": "1.0.2"}}`，`msg` 恰好 7 个字段：`from_user_id`（空字符串）、`to_user_id`、`client_id`（每条一个新的随机值，重试时沿用）、`message_type: 2`、`message_state: 2`、`context_token`、`item_list: [{"type": 1, "text_item": {"text": …}}]`。
  - 取二维码：`GET <base_url>/ilink/bot/get_bot_qrcode?bot_type=3`；查状态：`GET <base_url>/ilink/bot/get_qrcode_status?qrcode=<码>`。
- `official`：按腾讯官方协议文档（openclaw-weixin 2.4.8）补齐——多带 `iLink-App-Id: bot`、`iLink-App-ClientVersion`（版本号按 `0x00MMNNPP` 编成十进制字符串，2.4.8 是 `132104`）、`base_info.channel_version: "2.4.8"` 和 `base_info.bot_agent`（`clawbot-hub/<版本>`），服务启动时调 `notifystart`、正常退出时调 `notifystop`（失败只记警告，不影响启动和退出）；取二维码改用 `POST`，请求体 `{"local_token_list": []}`。官方文档里的 `POST` 取码**还没有在真账号上试过**，`local_token_list` 固定发空数组对服务端有没有影响也不知道，都列入真机验证清单；`legacy` 的 `GET` 是 ashare-quant 用着、已知能用的。

两档的请求都 MUST 经同一个上游适配层发出（见设计），这样协议变了只改一处。

#### Scenario: 默认和现在一致

- **WHEN** 用默认配置发一条消息
- **THEN** 假上游收到的请求头、请求体字段与上面 `legacy` 的清单逐项相同：自定义的请求头恰好 4 个（传输层自带的头不比），`msg` 恰好 7 个字段，`base_info` 只有 `channel_version: "1.0.2"`（`client_id`、`X-WECHAT-UIN` 这类随机值只比格式）

#### Scenario: 收消息请求的形式

- **WHEN** 用默认配置收一次消息
- **THEN** 假上游收到的 `getupdates` 请求体只有 `get_updates_buf` 和 `base_info` 两个键

#### Scenario: 切到官方档

- **WHEN** 配置 `upstream.profile: official`
- **THEN** 请求头多出 `iLink-App-Id` 和 `iLink-App-ClientVersion`（值 `132104`），`base_info` 里是 2.4.8 和 `bot_agent`，启动时假上游收到一次 `notifystart`；正常退出时收到一次 `notifystop`

#### Scenario: 官方档取二维码用 POST

- **WHEN** `official` 档下运行 `hubctl login`
- **THEN** 假上游收到的取二维码请求是 `POST`，请求体是 `{"local_token_list": []}`

#### Scenario: notifystart 失败不挡启动

- **WHEN** `official` 档下 `notifystart` 返回错误
- **THEN** 只记一条警告，服务照常开始收消息
