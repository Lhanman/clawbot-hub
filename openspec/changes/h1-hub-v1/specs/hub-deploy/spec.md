# Spec Delta

## Purpose

规定服务怎么装、怎么跑、怎么停、怎么升级、怎么在出事时留下线索：数据放哪、谁能连、日志里不能有什么、启动时查什么、数据库怎么升级、测试怎么保证不联网。服务是家里 Mac mini 上一个 launchd 常驻程序，代码在云里写、在 Mac 上装。

## ADDED Requirements

### Requirement: 数据目录和权限

服务的所有数据 MUST 放在数据目录 `<home>`（默认 `~/.clawbot-hub`，环境变量 `CLAWBOT_HUB_HOME` 或 `--home` 可改）里，不放进任何项目的目录：

| 路径 | 内容 | 权限 |
|---|---|---|
| `<home>/` | 数据目录本身 | 0700 |
| `account.json` | 登录凭证（见 `hub-login`） | 0600 |
| `hub.db`（及 `-wal`、`-shm`） | 数据库 | 0600 |
| `config.yaml` | 配置（可选） | 0600 |
| `bark.json` | 告警设备码（可选） | 0600 |
| `hub.lock` | 单实例锁 | 0600 |
| `run.json` | 本次运行的标记（进程号、启动时间、是否正常退出） | 0600 |
| `logs/` | 日志 | 目录 0700，文件 0600 |
| `backups/` | 备份（含凭证） | 目录 0700，文件 0600 |
| `clawbot-login.png`、`login.pending` | 扫码期间的临时文件 | 0600 |

服务创建的任何文件 MUST 在创建时就是这个权限（进程的 umask 设为 077，临时文件也一样），不能"先创建再改权限"。启动时发现已有文件或目录的权限更宽，MUST 改回并在日志里写一条警告。

#### Scenario: 新建的文件权限

- **WHEN** 第一次启动服务、登记项目、绑定、产生日志和备份
- **THEN** 数据目录下所有文件和目录的权限符合上表

#### Scenario: 权限被放宽

- **WHEN** 启动前 `account.json` 的权限被改成 0644
- **THEN** 启动后它是 0600，日志里有一条警告

### Requirement: 谁能连

服务默认只监听 `127.0.0.1:8790`（`server.host`、`server.port` 可配）。局域网监听是一个开关 `server.lan`（默认 false）：打开以后监听 `0.0.0.0`，并且 MUST 只接受来自本机和私有网段（`10.0.0.0/8`、`172.16.0.0/12`、`192.168.0.0/16`、`127.0.0.0/8`、`169.254.0.0/16`、`::1`、`fc00::/7`、`fe80::/10`）的连接，其他来源返回 403 且不记请求内容（规矩与 ashare-quant 的网页控制台一致）。不信任 `X-Forwarded-For` 之类的头，只看连接的来源地址。不做 TLS，也不支持公网；README 和安装说明 MUST 写明：局域网监听时密钥是明文走局域网的，只在可信的家庭网络里开。

端口被占用、绑定失败时，启动失败并给出"端口 8790 已被占用"的提示，退出码 1，不监听、不收消息。

#### Scenario: 默认只在本机

- **WHEN** 用默认配置启动
- **THEN** 监听的地址是 `127.0.0.1`（有测试检查监听套接字）

#### Scenario: 局域网开关打开后只认私有网段

- **WHEN** `server.lan: true`，请求的来源地址是 `203.0.113.5` / `192.168.1.20`（测试里注入来源地址）
- **THEN** 前者 403，后者按正常流程处理

#### Scenario: 伪造转发头无效

- **WHEN** 来源是公网地址，但带了 `X-Forwarded-For: 192.168.1.20`
- **THEN** 仍然 403

#### Scenario: 端口被占

- **WHEN** 配置的端口已经被别的程序占着
- **THEN** 启动失败，提示端口被占用，退出码 1，没有向腾讯发任何请求

### Requirement: 健康检查和版本

`GET /v1/health`（不需要密钥）MUST 只返回 `{"ok": true, "version": "<服务版本>", "api": "1"}`，不含登录、窗口、项目的任何信息；服务正在关闭时返回 503 `shutting_down`。给 launchd 之外的监控或脚本探活用。`GET /v1/status` 需要密钥，内容见 `hub-quota`。

#### Scenario: 不带密钥的探活

- **WHEN** 不带密钥请求 `/v1/health`
- **THEN** 200，返回内容恰好只有 `ok`、`version`、`api` 三个字段

#### Scenario: 其他接口需要密钥

- **WHEN** 不带密钥请求 `/v1/status`、`/v1/inbox`、`/v1/messages`
- **THEN** 都是 401 `unauthorized`

### Requirement: 日志

日志写到 `<home>/logs/hub.log`（轮转：每份最大 5 MB，保留 5 份），同时输出到标准错误（launchd 会收走）。日志 MUST：

- 不含登录凭证、对话凭证、项目密钥、Bark 设备码、消息正文：日志层统一过滤，已知的秘密值和 `chub_` 开头的密钥格式一律换成 `***`（即使调用方不小心把它们写进了日志消息，也是第二道防线）；
- 请求日志只记方法、路径（不含查询串）、状态码、耗时、项目名；不记请求头和请求体；
- 对收到的话只写"收到一条消息，N 个字，交给 <项目>"这一类信息。

默认级别 INFO，`log.level` 可调；DEBUG 级别也不记正文和凭证。

#### Scenario: 秘密被遮掉

- **WHEN** 测试往日志里故意写一条含当前 `bot_token`、一把项目密钥、一个 Bark 设备码的消息
- **THEN** 日志文件和标准错误里只有 `***`

#### Scenario: 请求日志不含查询串和密钥

- **WHEN** 项目带着密钥请求 `/v1/inbox?wait=25`
- **THEN** 日志里有 `GET /v1/inbox 200 项目名`，没有 `wait=25`，没有 `Authorization` 的值

### Requirement: 启动自检和顺序

`hubctl serve` 启动时 MUST 按这个顺序做，任何一步失败就停下、给出一句话原因、不收消息：

1. 读配置并校验，错误一次全部列出（退出码 1）；
2. 创建 / 修复数据目录的权限；
3. 拿 `hub.lock`（拿不到：退出码 2，见 `hub-login`）；
4. 打开数据库，检查完整性，按需升级（见下一条）；数据库版本比程序新或文件坏了：退出码 3；
5. 读 `account.json`（不存在是正常的，见 `hub-login`）；读不出来时服务照常启动但登录状态是"凭证坏了"，发消息得到 `failed`；
6. 看上一次的 `run.json`：不是正常退出的，记入"需要发 service_restarted 告警"；把仍标着"发送中"的发送记录标成"中断"（见 `hub-quota`）；
7. 绑定端口、开始监听（失败：退出码 1）；
8. 启动后台线程（收消息、定时任务、告警），写新的 `run.json`，日志写"已启动：版本、监听地址、上游档位、项目数"。

#### Scenario: 配置有多处错误

- **WHEN** 配置里窗口总条数写成 `-1`、端口写成 `70000`
- **THEN** 一次列出两处错误，退出码 1，没有监听端口

#### Scenario: 数据库比程序新

- **WHEN** 数据库的版本号是 99
- **THEN** 提示"数据库版本比程序新，请升级程序，不要回退"，退出码 3，数据库文件没被改动

### Requirement: 正常关闭

收到 `SIGTERM` 或 `SIGINT` 时，服务 MUST：停止接受新请求（新请求 503 `shutting_down`）；等正在发送的那一条发完（最多 `server.shutdown_seconds`，默认 10 秒）；正在等待的取话请求马上返回空列表；`official` 档下调 `notifystop`；写 `run.json` 的"正常退出"标记；释放锁；退出码 0。

#### Scenario: 发送中收到停止信号

- **WHEN** 一条发送正在进行，服务收到 `SIGTERM`
- **THEN** 这条发送完成、结果正常返回，之后服务退出，退出码 0，`run.json` 标着正常退出

#### Scenario: 取话请求被放掉

- **WHEN** 有项目的取话请求正在等待，服务收到 `SIGTERM`
- **THEN** 取话请求在 1 秒内返回空列表

### Requirement: 数据库和升级

数据库是 SQLite（WAL 模式，外键打开，`busy_timeout` 为 `db.busy_timeout_ms`，默认 5000），库里有一张版本表。升级 MUST 只往前：按编号顺序执行 `clawbot_hub/migrations/` 里的脚本，每个脚本在一个事务里；升级之前先自动备份（见 `hub-cli`）。启动时看到版本比程序新，退出码 3。

数据留存：发送记录和事件保留 `events.keep_days`（默认 90）天，待取记录见 `hub-inbox`，窗口记录保留 30 天，去重键保留 `send.dedupe_hours` 之后再留 1 天；每天清理一次。数据库里 MUST NOT 有 `bot_token`（它只在 `account.json` 里）；对话凭证只出现在窗口表和待取记录表里，过了回复句柄的有效期就清掉；项目密钥只存摘要。

#### Scenario: 数据库里没有登录凭证

- **WHEN** 绑定、登记项目、收发若干消息之后，检查整个数据库文件（含 `-wal`）的全部内容
- **THEN** 找不到 `bot_token` 的值和任何项目密钥原文

#### Scenario: 从旧版本升级

- **WHEN** 用测试夹具里的版本 1 的数据库启动版本 2 的程序（测试里放一个假的第二版迁移）
- **THEN** 先有一份自动备份，再升级；升级后项目、窗口、发送记录都在，版本号变成 2

#### Scenario: 升级失败不留半截

- **WHEN** 迁移脚本中途抛错
- **THEN** 数据库回到升级前的样子，服务启动失败，退出码 3，备份还在

#### Scenario: 留存清理

- **WHEN** 数据库里有 91 天前的发送记录和 89 天前的发送记录
- **THEN** 清理以后前者没了，后者还在

### Requirement: 接口版本

接口路径带 `/v1`，在 v1 里 MUST 只加不改：可以加新接口、新字段，不得改已有字段的含义、不得删字段、不得改已有错误码。`status` 的取值（`sent`、`unconfirmed`、`partial`、`rejected`、`failed`、`skipped`）在 v1 里固定，客户端遇到不认识的值 MUST 当作失败（`needs_fallback` 为真）。要做不兼容的改动就开 `/v2`，`/v1` 继续服务至少一个大版本。

#### Scenario: 不认识的状态

- **WHEN** 客户端收到 `status: "queued"`（一个假想的将来取值）
- **THEN** `needs_fallback` 为真，`ok` 为假

### Requirement: 运行环境

仓库 MUST 带：

- `deploy/launchd/com.clawbot-hub.serve.plist.example`：登录后启动（`RunAtLoad`）、退出后自动拉起（`KeepAlive`，`ThrottleInterval` 30 秒，避免退出码 2 时空转）、工作目录、`uv run hubctl serve` 的完整路径占位符、日志指向 `<home>/logs/launchd.log`。
- `docs/ops/安装说明.md`：在 Mac 上从零装起来的步骤（装 uv、拉代码、`uv sync`、`hubctl login`、`hubctl project add`、`hubctl alerts set-key`、装 launchd、`hubctl doctor`），每一步写明预期输出。
- `docs/ops/迁移手册.md`：把 ashare-quant 现有的 ClawBot 凭证迁进来的逐步操作，含停旧启新的顺序、校验方法、回退步骤（对应设计方案第 8 节），并标明哪些步骤要使用者本人在场（周末切换、手机上发一句话看结果）。
- `docs/ops/回退手册.md`：服务出问题时让项目退回直连 / 只用 Bark 的办法，以及怎么恢复备份。
- `docs/ops/真机验证清单.md`：腾讯一侧的规则必须在真机上确认的几项——发送返回里到底有没有消息编号（决定 `upstream.confirm` 能不能改成 `strict`）、`legacy` 与 `official` 两档在真账号上有没有差别、窗口时长和条数的实测、回复用的对话凭证能用多久、"一号一绑"是否成立——每项写明怎么测、怎么记结果。

这些文档在代码阶段 D 之前写好；真机验证在 Mac 上由使用者授权后进行，结果写回文档和配置默认值。

#### Scenario: launchd 模板可用

- **WHEN** 把模板里的占位符换成实际路径、运行 `plutil -lint`（在 Mac 上）或用测试里的 plist 解析器解析
- **THEN** 是合法的 plist，含 `RunAtLoad`、`KeepAlive`、`ThrottleInterval`，不含任何密钥

### Requirement: 测试不联网

单元测试和合约测试 MUST 不联网：`pytest` 启动时装一个套接字守卫，只放行本机回环（`127.0.0.1`、`::1`），连其他地址直接抛错；腾讯的接口一律由 `tests/support/fake_ilink.py`（一个本机回环上的假服务，能按剧本模拟登录各状态、收发、-14、5xx、超时、没有消息编号、窗口规则）代替，Bark 也用本机假服务。真实腾讯接口的测试用 `live` 标记，默认不运行（`uv run pytest -m live` 才运行，且需要使用者在场授权）。服务里所有用到时间和睡眠的地方 MUST 通过可替换的时钟（测试里用假时钟，不真等）。

#### Scenario: 守卫生效

- **WHEN** 测试里尝试连接 `example.com:443`
- **THEN** 守卫抛出明确的错误（"测试里不允许联网"）

#### Scenario: 默认不跑真机测试

- **WHEN** 运行 `uv run pytest`
- **THEN** 所有 `live` 标记的测试被跳过，整个测试套件在 90 秒内跑完

### Requirement: 显示时区

数据库里的时间 MUST 统一存 UTC 毫秒；给人看的地方（微信回复、原因文字、告警、命令行输出、接口里的 ISO 时间）MUST 按 `display.timezone` 显示，默认 `Asia/Shanghai`（与 ashare-quant 现在的微信文字一致）。接口里的 ISO 时间带时区偏移（如 `+08:00`）。

#### Scenario: 原因文字里的时间

- **WHEN** 使用者最后一次发消息是 UTC 2026-10-09 10:20，显示时区是 `Asia/Shanghai`，窗口已过期
- **THEN** `reason` 里写"10月9日 18:20"

#### Scenario: 换时区

- **WHEN** 配置 `display.timezone: Asia/Tokyo`
- **THEN** 同一条原因里写"10月9日 19:20"
