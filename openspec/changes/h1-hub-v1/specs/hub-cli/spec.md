# Spec Delta

## Purpose

规定运维用的命令行 `hubctl`：登录、登记项目、看状态、查毛病、备份、从脚本里发一条消息。管理只在命令行做，服务本身不开任何管理用的网页或接口（接口只给项目用，且只认项目密钥）。命令在服务运行时和停止时都能用，改的东西对服务的下一个请求立即生效，不用重启。

## ADDED Requirements

### Requirement: 公共约定

- 所有命令认 `--home <目录>`（默认环境变量 `CLAWBOT_HUB_HOME`，再默认 `~/.clawbot-hub`）和 `--config <文件>`（默认 `<home>/config.yaml`，不存在就用内置默认值）。数据目录不存在时 MUST 自动创建，权限 0700。
- 输出是中文；出错时一句话说清楚错在哪、怎么办。任何输出和报错都 MUST NOT 包含登录凭证、Bark 设备码、消息正文，也不包含项目密钥——只有一个例外：`project add` 和 `project rotate` 成功时向标准输出打印一次新密钥，它不进日志、不进事件记录，之后任何命令都不再显示。
- 退出码：0 成功；1 失败；2 用法错误（参数不对，含下面"不是终端又没带 `--yes`"的拒绝）。`serve` 另有：2 已经有一个在运行（见 `hub-login`）、3 数据目录或数据库有问题（版本比程序新、文件坏了）。`send` 有自己的一组退出码，见"从脚本发消息"；各命令的退出码按命令各自定义，互不通用。
- 需要确认的操作：`project remove`、`project rotate`、`project set` 改前缀、登记或修改时 `--default` 换掉已有的默认项目、`import-credentials --replace`。在终端里交互确认；带 `--yes` 时不问；不是终端又没带 `--yes` 时 MUST 拒绝并退出 2，什么也不改。
- 管理命令直接读写数据库，用写事务，等锁最长 `db.busy_timeout_ms`；服务对每个请求都读最新的项目设置，所以改动立即生效。`config.yaml` 的内容只在服务启动时读一次，改了要重启服务；`hubctl config show` 在配置文件比正在运行的服务启动得晚时提示"配置文件改过，重启服务才生效"。
- 判断"服务在不在运行"一律看 `run.json`：里面的进程号对应的进程还在，并且最后心跳不超过 60 秒前（心跳每 30 秒一次，见 `hub-deploy`）。不去试拿 `hub.lock`（试拿的一瞬间会让正在启动的服务误以为已经有一个在运行）。唯一拿锁的命令是 `import-credentials`：它先按这个办法判断没在运行，再拿锁是为了在导入期间占住、不让服务启动（见 `hub-login`），不是用锁来判断。

#### Scenario: 数据目录自动创建

- **WHEN** `~/.clawbot-hub` 不存在，运行 `hubctl status`
- **THEN** 目录被创建、权限 0700，输出显示"还没绑定"和"没有登记项目"，退出码 0

#### Scenario: 不是终端又没带 --yes

- **WHEN** 在脚本里（标准输入不是终端）运行 `hubctl project remove x` 不带 `--yes`
- **THEN** 拒绝，提示要加 `--yes`，退出码 2，什么也没删

#### Scenario: 带 --yes 不问

- **WHEN** 在脚本里运行 `hubctl project rotate x --yes`
- **THEN** 不提问，直接换钥，输出里有一次新密钥

#### Scenario: 密钥只打印一次

- **WHEN** `project add` 成功后，依次运行 `hubctl project list`、`hubctl status --json`、`hubctl events`、`hubctl doctor`，并检查服务日志
- **THEN** `project add` 的输出里有 `chub_` 开头的密钥；之后所有输出和日志里都搜不到 `chub_`

#### Scenario: 服务在不在运行

- **WHEN** `run.json` 里的进程号是一个已经不存在的进程；另一种情形是进程号对应的进程还在，但最后心跳是 5 分钟前
- **THEN** 两种情形下 `hubctl status` 都显示"服务：没有在运行"；进程在、心跳在 30 秒内时显示在运行

### Requirement: 命令一览

`hubctl` MUST 提供下面这些命令（`hubctl --help` 列出全部）：

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
| `hubctl events [--limit N] [--kind K] [--json]` | 看最近的事件记录（没有正文） |
| `hubctl config show` | 显示生效的配置（含默认值，密钥类打码） |
| `hubctl version` | 版本号、接口版本、数据库版本 |

#### Scenario: 帮助

- **WHEN** 运行 `hubctl --help`
- **THEN** 列出以上全部命令（`serve`、`login`、`import-credentials`、`status`、`project`、`send`、`alerts`、`doctor`、`backup`、`events`、`config`、`version`），每条一句中文说明

#### Scenario: 版本

- **WHEN** 运行 `hubctl version`
- **THEN** 输出程序版本、接口版本（`1`）、数据库版本，退出码 0

#### Scenario: 配置显示

- **WHEN** 运行 `hubctl config show`，环境变量 `CLAWBOT_HUB_BARK_KEY` 里有 Bark 设备码
- **THEN** 显示全部生效的配置项（含默认值）；输出里没有设备码，告警那一行只写"设备码：已配置（环境变量）"、"已配置（bark.json）"或"未配置"；服务启动之后改过配置文件时，末尾提示"配置文件改过，重启服务才生效"

#### Scenario: 告警命令

- **WHEN** 运行 `echo K | hubctl alerts set-key`，再运行 `hubctl alerts test`，假 Bark 服务回 200
- **THEN** `bark.json` 权限是 0600，输出里没有设备码；测试告警送到，退出码 0；事件里有一条"Bark 测试成功"的记录

### Requirement: 看全貌

`hubctl status` MUST 在服务运行或停止时都能给出（读数据库、`run.json` 和凭证文件，不连腾讯）：

- 服务：在不在运行（进程号、运行了多久）、版本、监听地址、上游档位（`legacy` / `official`）和送达确认档位（这几项来自 `run.json`，服务没在运行时写"没有在运行"）；
- 登录：已绑定 / 没绑定 / 已失效 / 读不出来；绑定时间；`bot_id` 末 4 位；最近一次成功收消息的时间和最近一次错误；忽略的陌生人消息数；
- 对话窗口：起点、剩余时间、已用 / 总条数（推送和回复分开）、是否处于"腾讯拒绝后暂停"；
- 每个项目一行：保底、上限、已用（推送 / 回复）、在线与否、最后出现、默认 / 只推送 / 已停用；
- 待处理：每个项目队列里有几条没取的话；中断的发送有几条；未送出的告警有几条；
- 提示：没配告警、窗口快到期、配置文件改过没重启、保底合计加回复保留超过窗口总条数（配置里把窗口总条数改小之后会出现，服务照常运行，只是登记和启用项目会被拒绝）……

`--json` 输出同样内容的 JSON（给脚本和以后的状态页用）。

#### Scenario: 服务停着也能看

- **WHEN** 服务没在运行，运行 `hubctl status`
- **THEN** 第一行是"服务：没有在运行"，后面照常列出登录、窗口、项目；退出码 0

#### Scenario: 没有凭证和密钥

- **WHEN** 运行 `hubctl status --json`
- **THEN** 输出里搜不到 `bot_token`、`context_token`、`chub_`、Bark 设备码

#### Scenario: 保底合计超了的提示

- **WHEN** 已有保底合计 8 的项目，配置把 `quota.window_limit` 改成 8（8 加回复保留 2 超过 8），重启服务
- **THEN** 服务正常启动；`hubctl status` 有一条提示说明保底合计超过了窗口总条数，并列出各项目的保底

### Requirement: 从脚本发消息

`hubctl send [--url <地址>] [--key-env <变量名> | --key-file <文件>] [--priority <档>] [--dedupe-key <键>] [--on-closed fail|skip] [--timeout <秒>] [--json] [<文字> | -]` MUST 用项目密钥调用服务的 `POST /v1/messages`，给没有 Python 客户端的脚本用。

- 密钥只从环境变量（默认 `CLAWBOT_HUB_KEY`）或文件读，**不提供**命令行参数直接给密钥（会被 `ps` 看到）；文件权限比 0600 宽时拒绝（退出码 2）。地址默认 `http://127.0.0.1:8790`（配置里改了端口时读配置）。
- 文字来自参数；参数是 `-` 或者没给参数且标准输入不是终端时，从标准输入读。
- 默认输出一行：状态加原因（`rejected：对话窗口过期了……`）；`--json` 输出服务返回的原样 JSON。
- `--timeout` 是读超时，默认 35 秒（比服务端发送总时限多 5 秒）。
- **以下退出码只用于 `hubctl send`**：
  - 0：`sent`（含去重命中的已发），以及 `skipped`（输出仍写 `skipped：…`；`skipped` 是使用者要的"窗口关了就安静跳过"，不是失败）；
  - 3 `unconfirmed`；4 `partial`；5 `rejected`；6 `failed`；
  - 10：请求没有发出去（连不上服务、连接超时），一定没有发出任何东西；
  - 11：密钥不对 / 项目已停用（401、403）；
  - 12：其他接口错误（400 系列、429、503 `shutting_down`……）；
  - 13：请求发出之后没有拿到结果（读超时、连接被重置、服务返回 500）——消息**可能已经发出**，脚本要在走 Bark 时写明"可能已经到了"；
  - 7 空着不用；2 用法错误。

  这样脚本里写 `hubctl send "…" || 走 Bark` 就够了：只有真的没发出去或拿不准的时候才会走 Bark，窗口关了安静跳过的不会。

#### Scenario: 发一条

- **WHEN** 服务在运行，`CLAWBOT_HUB_KEY` 是有效密钥，运行 `hubctl send "盘点完成"`
- **THEN** 输出 `sent`，退出码 0，假上游收到一条

#### Scenario: 被拒绝

- **WHEN** 对话窗口已过期
- **THEN** 输出 `rejected：对话窗口过期了……`，退出码 5

#### Scenario: 跳过不算失败

- **WHEN** 对话窗口已过期，运行 `hubctl send --on-closed skip "…" || echo 走Bark`
- **THEN** 输出 `skipped：…`，退出码 0，没有输出"走Bark"

#### Scenario: 其余的退出码

- **WHEN** 假上游分别让一次发送得到 `unconfirmed`、`partial`（第 2 条之后停下）、`failed`（登录失效），用错误的密钥，用已停用项目的密钥，发一条超长文字（400）
- **THEN** 六次的退出码依次是 3、4、6、11、11、12

#### Scenario: 服务没开

- **WHEN** 服务没在运行
- **THEN** 输出"连不上共用服务（地址）"，退出码 10，没有发出任何东西

#### Scenario: 发出之后没有结果

- **WHEN** 服务收到请求后 40 秒不回话（读超时 35 秒）；另一次是服务返回 500 `internal`
- **THEN** 两次都输出"可能已经发出，请不要重发"，退出码 13

#### Scenario: 从标准输入读

- **WHEN** `echo "第一行\n第二行" | hubctl send -`
- **THEN** 发出的文字是两行

#### Scenario: 不收命令行里的密钥

- **WHEN** 运行 `hubctl send --key chub_xxx "…"`
- **THEN** 用法错误，退出码 2

#### Scenario: 密钥文件权限太宽

- **WHEN** `--key-file` 指向的文件权限是 0644
- **THEN** 拒绝，提示改成 0600，退出码 2，没有向服务发任何请求

### Requirement: 查毛病

`hubctl doctor` MUST 逐项检查并给出 ✓ 通过、⚠ 警告、✗ 错误，最后一行汇总；有任何 ✗ 时退出码 1，只有 ⚠ 时 0。每一项失败时的级别如下：

| 检查项 | 失败时 |
|---|---|
| 本次运行刚建了数据目录（`<home>` 原来不存在） | ⚠，提示"数据目录是刚建的"（也许 `--home` 指错了） |
| 数据目录权限是 0700；`account.json`、`bark.json`、`hub.db`、`config.yaml`（存在时）权限是 0600 | ✗ |
| `account.json` 不存在 | ⚠（还没绑定） |
| `account.json` 读不出来、缺字段 | ✗ |
| 登录状态已失效 | ⚠ |
| 数据库能打开、`PRAGMA integrity_check` 是 ok、版本不比程序新 | ✗ |
| 服务在不在运行（判法见"公共约定"） | ⚠（没在运行；也许是你有意停的） |
| 端口被别的程序占着 | ✗，只在服务没在运行时检查（运行时端口是它自己占的） |
| 配置读得出来、值在 `design.md` 第 13 节"取值范围"表给的范围内、显示时区认识、`hub.label` 和确认词的格式对 | ✗ |
| 保底合计加回复保留超过窗口总条数 | ✗，列出各项目的保底 |
| 某个项目的前缀和 `routing.confirm_words` 或 `hub.label` 冲突 | ✗，指出是哪个前缀 |
| 没配告警设备码 | ⚠ |
| 最近一次发 Bark 的结果是被拒绝，之后没有成功发出过一条（设备码可能错了；`hubctl alerts test` 成功也算一次成功），从事件记录里取 | ⚠ |
| `server.shutdown_seconds` 小于 `send.timeout_seconds` | ⚠，写出两个值（关停时可能等不到最后一条发完） |
| 残留的 `login.pending`（里面的进程已经不在，或者开始超过 20 分钟） | ⚠ |
| 磁盘剩余空间不到 100 MB | ⚠ |
| 最近一次自动备份距今超过 2 天，或没做过 | ⚠ |
| `--online`：只试探地连一下腾讯的服务器（TCP 加 TLS 握手，再不带凭证地访问一次根地址），失败 | ⚠ |

`--online` **不**调用收消息、发消息接口（避免和运行中的服务抢消息）。`--fix` 只修一类问题：把权限放宽了的文件和目录改回 0600 / 0700，并说明改了什么；其他问题只提示不动手。

#### Scenario: 权限太宽

- **WHEN** `account.json` 的权限是 0644，运行 `hubctl doctor`
- **THEN** 该项是 ✗，提示 `chmod 600` 或者加 `--fix`，退出码 1；加 `--fix` 重跑以后变成 ✓ 并说明改了哪个文件

#### Scenario: 保底合计超了

- **WHEN** 有人直接改了数据库，让保底合计加回复保留超过了窗口总条数
- **THEN** 该项是 ✗，列出各项目的保底

#### Scenario: 只有警告时退出码是 0

- **WHEN** 没配告警设备码、服务没在运行，其余都正常
- **THEN** 有两个 ⚠，没有 ✗，退出码 0

#### Scenario: 关停等待比发送总时限短

- **WHEN** 配置 `server.shutdown_seconds` 为 20、`send.timeout_seconds` 为 30，设备码已配好、服务在运行、其余都正常，运行 `hubctl doctor`
- **THEN** 有一项 ⚠，写出 20 和 30 两个值；没有 ✗，退出码 0

#### Scenario: Bark 拒绝过，换对设备码后警告消失

- **WHEN** 最近一次发 Bark 被拒绝（假 Bark 服务回 `{"code":400}`），运行 `hubctl doctor`；然后 `hubctl alerts set-key` 换成对的设备码，`hubctl alerts test` 成功，再运行 `hubctl doctor`
- **THEN** 第一次该项是 ⚠，第二次没有这一项警告

#### Scenario: 数据目录是刚建的

- **WHEN** `--home` 指向一个不存在的目录，运行 `hubctl doctor`
- **THEN** 目录被建出来（权限 0700），有一项 ⚠ 提示数据目录是刚建的；其他检查照常进行

#### Scenario: 服务运行时不报端口被占

- **WHEN** 服务在运行（占着自己的端口），运行 `hubctl doctor`
- **THEN** 端口那一项不报问题；服务停掉、另一个程序占着这个端口时，该项是 ✗

#### Scenario: 扫码进行中不误报

- **WHEN** `hubctl login` 正在扫码（进程活着，开始 12 分钟），另一个终端运行 `hubctl doctor`
- **THEN** `login.pending` 一项没有警告；进程已经不在的残留标记则是 ⚠

#### Scenario: 离线检查不碰腾讯

- **WHEN** 运行 `hubctl doctor`（不带 `--online`）
- **THEN** 假上游没有收到任何请求

#### Scenario: 在线检查不抢消息

- **WHEN** 服务在运行，运行 `hubctl doctor --online`
- **THEN** 假上游没有收到 `getupdates` 和 `sendmessage`

### Requirement: 备份

`hubctl backup` MUST 把 `hub.db`（用 SQLite 的在线备份接口，服务运行时也安全）、`account.json`、`config.yaml`、`bark.json` 复制到 `<home>/backups/<YYYYmmdd-HHMMSS>/`（目录 0700、文件 0600），打印目录和各文件大小。备份里有登录凭证，输出的提示 MUST 写明这一点（"备份里有登录凭证，不要放进云盘或 git"）。

`--to <目录>` 指的是备份的**父目录**：备份写到 `<目录>/<YYYYmmdd-HHMMSS>/`。父目录不存在就创建（0700）；已经存在的父目录 MUST NOT 被改权限，只有这次新建的子目录和里面的文件设 0700 / 0600。路径含 `Mobile Documents` 或 `CloudStorage`（iCloud 云盘、同步目录）时打印更醒目的警告，但仍然执行。

三种备份用名字区分，都在 `<home>/backups/` 下：服务每天 `backup.at`（默认 04:00，显示时区）自动做的叫 `auto-<时间>`；升级前自动做的叫 `pre-upgrade-v<旧版本>-v<新版本>-<时间>`（见 `hub-deploy`）；手动的叫 `<时间>`。自动清理只数 `auto-` 开头的，只留最近 `backup.keep`（默认 14）份，另外两种和 `--to` 的不会被自动清理。备份失败见 `hub-alerts`。

#### Scenario: 备份能打开

- **WHEN** 登记了两个项目、窗口里用了 3 条，运行 `hubctl backup`，再用备份里的数据库
- **THEN** 能打开，两个项目和窗口已用 3 条都在，`integrity_check` 是 ok

#### Scenario: 服务运行时备份

- **WHEN** 服务正在不停地收发（假上游），运行 `hubctl backup`
- **THEN** 备份成功，服务没有出错，收发没有中断

#### Scenario: 保留份数

- **WHEN** `backups/` 里已经有 14 份 `auto-` 备份、2 份手动备份和 1 份升级前的备份，今天又自动做了一份
- **THEN** 最早的一份 `auto-` 被删掉，留 14 份；手动的、升级前的都还在

#### Scenario: 备份到别的目录

- **WHEN** 运行 `hubctl backup --to ~/Documents`（目录已存在，权限 0755）
- **THEN** 备份在 `~/Documents/<时间>/` 里，这个子目录是 0700、文件是 0600，`~/Documents` 的权限仍是 0755；输出提醒备份里有登录凭证

#### Scenario: 备份到云盘目录的警告

- **WHEN** `--to` 的路径含 `Mobile Documents`
- **THEN** 输出醒目的警告（备份里有登录凭证，云盘会同步它），备份仍然完成

### Requirement: 事件记录

服务 MUST 把值得事后查的事写进事件表（时间、级别、种类、项目、一句话，**没有正文**）。种类是下面这个封闭的集合，不会有别的：

| 种类 | 包括 |
|---|---|
| `login` | 绑定 / 导入凭证、登录失效 / 恢复 |
| `alert` | 告警发出、未送出、被 Bark 拒绝、放弃 |
| `send` | 发送被拒绝 / 失败 / 未确认 / 中断 |
| `hold` | 腾讯拒绝后暂停的开始、试探、解除 |
| `project` | 登记 / 修改 / 停用 / 启用 / 移除 / 换钥 / 默认项目变更 |
| `queue` | 队列溢出丢话、话作废 |
| `backup` | 备份成功 / 失败 / 清理 |
| `lifecycle` | 启动、正常退出、发现上次异常退出、配置警告（例如前缀与确认词冲突） |

保留 `events.keep_days`（默认 90）天。`hubctl events` 默认列最近 50 条，`--limit` 改条数；`--kind` 按种类**精确**匹配，不认识的种类报用法错误（退出码 2，并列出可用的种类）。

#### Scenario: 登记项目有记录

- **WHEN** 登记项目 x 后运行 `hubctl events --kind project`
- **THEN** 有一条"登记项目 x（保底 2、上限 4）"，没有密钥

#### Scenario: 种类精确匹配

- **WHEN** 运行 `hubctl events --kind proj`，再运行 `hubctl events --kind foo`
- **THEN** 两次都是用法错误，退出码 2，输出列出 `login`、`alert`、`send`、`hold`、`project`、`queue`、`backup`、`lifecycle`

#### Scenario: 事件里没有正文

- **WHEN** 项目发了含独特字符串的消息、使用者发了含独特字符串的话之后，查看全部事件
- **THEN** 事件里搜不到这些字符串
