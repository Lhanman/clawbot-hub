# Spec Delta

## Purpose

规定运维用的命令行 `hubctl`：登录、登记项目、看状态、查毛病、备份、从脚本里发一条消息。管理只在命令行做，服务本身不开任何管理用的网页或接口（接口只给项目用，且只认项目密钥）。命令在服务运行时和停止时都能用，改的东西对服务的下一个请求立即生效，不用重启。

## ADDED Requirements

### Requirement: 公共约定

- 所有命令认 `--home <目录>`（默认环境变量 `CLAWBOT_HUB_HOME`，再默认 `~/.clawbot-hub`）和 `--config <文件>`（默认 `<home>/config.yaml`，不存在就用内置默认值）。数据目录不存在时 MUST 自动创建，权限 0700。
- 输出是中文；出错时一句话说清楚错在哪、怎么办；任何输出和报错都 MUST NOT 包含凭证、项目密钥、Bark 设备码、消息正文。
- 退出码：0 成功；1 失败；2 用法错误（参数不对）。`serve` 另有：2 已经有一个在运行（见 `hub-login`）、3 数据目录或数据库有问题（版本比程序新、文件坏了）。
- 有破坏性的命令（`project remove`、`project rotate`、`import-credentials --replace`、`project set` 改前缀）在终端里交互确认；不是终端或者带 `--yes` 时不问。不是终端又没带 `--yes` 的 MUST 拒绝并退出 2。
- 管理命令直接读写数据库，用写事务，等锁最长 `db.busy_timeout_ms`；服务对每个请求都读最新的项目设置，所以改动立即生效。`config.yaml` 的内容只在服务启动时读一次，改了要重启服务；`hubctl config show` 在服务比文件旧时提示"配置文件改过，重启服务才生效"。

#### Scenario: 数据目录自动创建

- **WHEN** `~/.clawbot-hub` 不存在，运行 `hubctl status`
- **THEN** 目录被创建、权限 0700，输出显示"还没绑定"和"没有登记项目"，退出码 0

#### Scenario: 不是终端又没带 --yes

- **WHEN** 在脚本里（标准输入不是终端）运行 `hubctl project remove x` 不带 `--yes`
- **THEN** 拒绝，提示要加 `--yes`，退出码 2，什么也没删

### Requirement: 命令一览

| 命令 | 做什么 |
|---|---|
| `hubctl serve` | 启动常驻服务（行为见 `hub-login`、`hub-deploy`） |
| `hubctl login` | 扫码绑定 ClawBot（见 `hub-login`） |
| `hubctl import-credentials <文件>` | 接过 ashare-quant 的凭证（见 `hub-login`） |
| `hubctl status [--json]` | 看全貌（见下一条） |
| `hubctl project add / list / show / set / disable / enable / remove / rotate` | 项目管理（见 `hub-projects`） |
| `hubctl send` | 从脚本里发一条消息（见下面） |
| `hubctl alerts set-key / test` | 配置告警用的 Bark 设备码，发测试告警（见 `hub-alerts`） |
| `hubctl doctor [--online] [--fix]` | 检查毛病 |
| `hubctl backup [--to <目录>]` | 备份 |
| `hubctl events [--limit N] [--kind K] [--json]` | 看最近的事件记录（登录、告警、拒绝、中断、清理……，没有正文） |
| `hubctl config show` | 显示生效的配置（含默认值，密钥类打码） |
| `hubctl version` | 版本号、接口版本、数据库版本 |

#### Scenario: 帮助

- **WHEN** 运行 `hubctl --help`
- **THEN** 列出以上命令，每条一句中文说明

### Requirement: 看全貌

`hubctl status` MUST 在服务运行或停止时都能给出（读数据库和凭证文件，不连腾讯）：

- 服务：在不在运行（进程号、运行了多久）、版本、监听地址、上游档位（`legacy` / `official`）和送达确认档位；
- 登录：已绑定 / 没绑定 / 已失效；绑定时间；`bot_id` 末 4 位；最近一次成功收消息的时间和最近一次错误；
- 对话窗口：起点、剩余时间、已用 / 总条数（推送和回复分开）、是否处于"腾讯拒绝后暂停"；
- 每个项目一行：保底、上限、已用（推送 / 回复）、在线与否、最后出现、默认 / 只推送 / 已停用；
- 待处理：每个项目队列里有几条没取的话；中断的发送有几条；未送出的告警有几条；
- 提示：没配告警、窗口快到期、配置文件改过没重启、保底合计超过窗口总条数……

`--json` 输出同样内容的 JSON（给脚本和以后的状态页用）。

#### Scenario: 服务停着也能看

- **WHEN** 服务没在运行，运行 `hubctl status`
- **THEN** 第一行是"服务：没有在运行"，后面照常列出登录、窗口、项目；退出码 0

#### Scenario: 没有凭证和密钥

- **WHEN** 运行 `hubctl status --json`
- **THEN** 输出里搜不到 `bot_token`、`context_token`、`chub_`、Bark 设备码

### Requirement: 从脚本发消息

`hubctl send [--url <地址>] [--key-env <变量名> | --key-file <文件>] [--priority <档>] [--dedupe-key <键>] [--on-closed fail|skip] [--timeout <秒>] [--json] [<文字> | -]` MUST 用项目密钥调用服务的 `POST /v1/messages`，给没有 Python 客户端的脚本用。

- 密钥只从环境变量（默认 `CLAWBOT_HUB_KEY`）或文件读，**不提供**命令行参数直接给密钥（会被 `ps` 看到）；文件权限比 0600 宽时拒绝。地址默认 `http://127.0.0.1:8790`（配置里改了端口时读配置）。
- 文字来自参数；参数是 `-` 或者没给参数且标准输入不是终端时，从标准输入读。
- 默认输出一行：状态加原因（`rejected：对话窗口过期了……`）；`--json` 输出服务返回的原样 JSON。
- 退出码：0 `sent`（含去重命中的已发）；3 `unconfirmed`；4 `partial`；5 `rejected`；6 `failed`；7 `skipped`；10 连不上服务；11 密钥不对 / 项目停用；12 其他接口错误（含 400 系列）。这样脚本里写 `hubctl send "…" || 走 Bark` 就够了。

#### Scenario: 发一条

- **WHEN** 服务在运行，`CLAWBOT_HUB_KEY` 是有效密钥，运行 `hubctl send "盘点完成"`
- **THEN** 输出 `sent`，退出码 0，假上游收到一条

#### Scenario: 被拒绝

- **WHEN** 对话窗口已过期
- **THEN** 输出 `rejected：对话窗口过期了……`，退出码 5

#### Scenario: 服务没开

- **WHEN** 服务没在运行
- **THEN** 输出"连不上共用服务（地址）"，退出码 10，没有发出任何东西

#### Scenario: 从标准输入读

- **WHEN** `echo "第一行\n第二行" | hubctl send -`
- **THEN** 发出的文字是两行

#### Scenario: 不收命令行里的密钥

- **WHEN** 运行 `hubctl send --key chub_xxx "…"`
- **THEN** 用法错误，退出码 2

### Requirement: 查毛病

`hubctl doctor` MUST 逐项检查并给出 ✓ 通过、⚠ 警告、✗ 错误，最后一行汇总；有任何 ✗ 时退出码 1，只有 ⚠ 时 0：

- 数据目录存在、权限是 0700；`account.json`、`bark.json`、`hub.db`、`config.yaml`（存在时）权限是 0600；
- `account.json` 读得出来、字段齐全；数据库能打开、`PRAGMA integrity_check` 是 ok、版本不比程序新；
- 服务在不在运行、端口有没有被别的程序占着；
- 配置读得出来、值在合理范围、保底合计加回复保留不超过窗口总条数、显示时区认识；
- 是否配了告警设备码；残留的 `login.pending`（超过 10 分钟）；磁盘剩余空间不到 100 MB；上一次备份距今超过 2 天；
- `--online`：只试探地连一下腾讯的服务器（TCP 加 TLS 握手，再不带凭证地访问一次根地址），**不**调用收消息、发消息接口（避免和运行中的服务抢消息）。

`--fix` 只修一类问题：把权限放宽了的文件和目录改回 0600 / 0700，并说明改了什么；其他问题只提示不动手。

#### Scenario: 权限太宽

- **WHEN** `account.json` 的权限是 0644，运行 `hubctl doctor`
- **THEN** 该项是 ✗，提示 `chmod 600` 或者加 `--fix`，退出码 1；加 `--fix` 重跑以后变成 ✓ 并说明改了哪个文件

#### Scenario: 保底合计超了

- **WHEN** 有人直接改了数据库，让保底合计加回复保留超过了窗口总条数
- **THEN** 该项是 ✗，列出各项目的保底

#### Scenario: 离线检查不碰腾讯

- **WHEN** 运行 `hubctl doctor`（不带 `--online`）
- **THEN** 假上游没有收到任何请求

#### Scenario: 在线检查不抢消息

- **WHEN** 服务在运行，运行 `hubctl doctor --online`
- **THEN** 假上游没有收到 `getupdates` 和 `sendmessage`

### Requirement: 备份

`hubctl backup` MUST 把 `hub.db`（用 SQLite 的在线备份接口，服务运行时也安全）、`account.json`、`config.yaml`、`bark.json` 复制到 `<home>/backups/<YYYYmmdd-HHMMSS>/`（目录 0700、文件 0600），打印目录和各文件大小。备份里有登录凭证，输出的提示 MUST 写明这一点（"备份里有登录凭证，不要放进云盘或 git"）。`--to <目录>` 把备份放到别处（同样 0700 / 0600，目录不存在就创建）。

服务本身每天 `backup.at`（默认 04:00，显示时区）做一次备份，只留最近 `backup.keep`（默认 14）份；手动备份和 `--to` 的不会被自动清理。备份失败见 `hub-alerts`。

#### Scenario: 备份能打开

- **WHEN** 登记了两个项目、窗口里用了 3 条，运行 `hubctl backup`，再用备份里的数据库
- **THEN** 能打开，两个项目和窗口已用 3 条都在，`integrity_check` 是 ok

#### Scenario: 服务运行时备份

- **WHEN** 服务正在不停地收发（假上游），运行 `hubctl backup`
- **THEN** 备份成功，服务没有出错，收发没有中断

#### Scenario: 保留份数

- **WHEN** 自动备份已经有 14 份，今天又做了一份
- **THEN** 最早的一份被删掉，留 14 份；手动 `--to` 的备份不受影响

### Requirement: 事件记录

服务 MUST 把值得事后查的事写进事件表（时间、级别、种类、项目、一句话，**没有正文**）：登录失效 / 恢复、告警发出 / 未送出、发送被拒绝 / 失败 / 未确认 / 中断、腾讯拒绝后暂停、项目登记 / 修改 / 停用 / 启用 / 移除 / 换钥、队列溢出和话作废、备份、启动 / 正常退出。保留 `events.keep_days`（默认 90）天。`hubctl events` 默认列最近 50 条，`--kind` 按种类筛。

#### Scenario: 登记项目有记录

- **WHEN** 登记项目 x 后运行 `hubctl events --kind project`
- **THEN** 有一条"登记项目 x（保底 2、上限 4）"，没有密钥

#### Scenario: 事件里没有正文

- **WHEN** 项目发了含独特字符串的消息、使用者发了含独特字符串的话之后，查看全部事件
- **THEN** 事件里搜不到这些字符串
