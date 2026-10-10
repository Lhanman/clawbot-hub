# Design

## Context

设计方案（`docs/specs/2026-10-09-ClawBot共用服务-设计方案.md`，使用者 2026-10-10 批准方向）定了"做什么"：一个独立的常驻服务独占 ClawBot 的登录和收消息，各项目拿密钥通过本机接口收发。这份文档定"怎么做"。

现成可以直接参照的是 ashare-quant 里的 `notify/clawbot.py`（登录、收、发、记额度，约 470 行）、`notify/http.py`（重试、脱敏）、`notify/base.py`（拆条）和 `chat/bot.py`（收消息循环、退避、确认的 10 分钟）。搬过来的行为（拆条规则、-14 处理、脱敏、错误文案）一个字都不改；新写的是多项目的部分：项目登记、额度分配、路由、租约、告警。

规模：粗估服务本体 1500 行上下，测试同量级。

## Goals / Non-Goals

**Goals**

- 只有一个进程碰腾讯：登录、收消息、发消息、记账都在这里，谁也挤不掉谁。
- 项目这一侧简单：几个 HTTP 调用，错了知道怎么办（走 Bark）。
- 账不会错：重启、崩溃、并发都不丢、不超额；拿不准的就说拿不准（"未确认"），不偷偷重发。
- 腾讯的规则会变，所以窗口、条数、协议档位都是配置；协议只在一处跟进。
- 测试先行，不联网，不需要真的 ClawBot。

**Non-Goals**

见提案：不做图片语音、多号、网页界面、公网、摘要合并、AI；不改任何项目的代码。

## Decisions

### 1. 同步线程模型，不用 asyncio

负载很小（每天几十条消息、几个项目），瓶颈是腾讯接口的往返。同步代码 + 线程最容易写对、最容易测：`requests` 发腾讯请求，`sqlite3` 存状态，FastAPI 的同步接口跑在线程池里，长轮询用 `threading.Condition` 等。

线程只有这几个：

```
uvicorn（HTTP 服务，请求在线程池里）
   │  发消息 / 回复 / 取话 / 确认 / 状态
   ▼
┌───────────────────────────── 同一个进程 ─────────────────────────────┐
│ api ──► projects（认密钥）                                            │
│   │ ├──► sender（拆条、预占额度、发、对账）──► upstream ──► 腾讯      │
│   │ └──► inbox（待取队列、租约、确认、回复句柄）                      │
│ poller（后台线程）──► upstream.get_updates ──► router ──► inbox       │
│                                    └──► sender（服务自己的回复）      │
│ scheduler（每 30 秒一次）──► 清理、窗口提醒、不通判断、备份、心跳     │
│ alerts（后台线程）──► Bark                                            │
│ 全部状态在 SQLite（WAL）里；凭证在 account.json                       │
└──────────────────────────────────────────────────────────────────────┘
```

发送锁：同一时间只有一条消息在向腾讯发（`threading.Lock`），保证同一条消息的几段连续、不同项目的消息不穿插。额度判定和预占**在拿发送锁之前**做（数据库事务保证不超额），所以被拒绝的请求立刻返回，不用排队。

### 2. 模块划分

```
src/clawbot_hub/
  config.py        配置：读取、默认值、校验（错误一次列全）
  clock.py         Clock：now_ms()、sleep()；测试里换假时钟
  textutil.py      拆条、取标题、NFKC 匹配、脱敏（纯函数，从 ashare-quant 搬）
  db.py            连接、事务（BEGIN IMMEDIATE）、迁移执行器
  migrations/      0001_init.sql …
  upstream/        base.py（协议和结果类型）、http.py（腾讯 HTTP）、profiles.py（legacy / official）
  account.py       account.json 读写（0600、锁、原子改名）
  login.py         扫码流程
  projects.py      项目增删改、密钥、校验
  quota.py         额度判定（纯函数）+ 预占 / 退回（事务）
  sender.py        发消息和回复：校验、去重、拆条、发送状态机、对账
  router.py        路由（纯函数）
  inbox.py         待取队列、租约、确认、回复句柄、清理
  poller.py        收消息循环：长轮询、去重、窗口、路由、服务自己的回复
  alerts.py        告警规则、去重状态、Bark 发送与重试
  scheduler.py     定时任务
  api.py           FastAPI 路由、认证、限速、错误格式
  app.py           装配和生命周期（启动自检、关闭）
  cli.py           hubctl（typer）
  backup.py  doctor.py  logging_setup.py
client/clawbot_hub_client.py     单文件客户端
tests/  support/fake_ilink.py  contract/  …
```

依赖方向：`api → sender / inbox / projects → quota / db`；`poller → upstream / router / inbox / sender`；`cli → projects / login / backup / doctor`，只有 `cli serve` 才引入 `api` 和 `app`。纯函数模块（`quota` 的判定、`router`、`textutil`）不碰数据库和网络，表格式测试。

### 3. 上游适配层

`upstream/base.py` 定义协议（`Protocol`）和结果类型，服务其余部分**只**通过它碰腾讯：

```python
class Upstream(Protocol):
    def get_updates(self, acct: Account, buf: str, timeout: float) -> UpdatesResult: ...
    def send_text(self, acct: Account, to_user_id: str, context_token: str,
                  text: str, client_id: str, timeout: float) -> SendOutcome: ...
    def qr_start(self) -> QrCode: ...
    def qr_status(self, code: str, verify_code: str | None, host: str | None) -> QrStatus: ...
    def notify_start(self, acct: Account) -> None: ...
    def notify_stop(self, acct: Account) -> None: ...
```

`SendOutcome` 是一个小的联合：`Accepted(message_id | None)`、`Rejected(code, errmsg)`、`SessionExpired`、`Transient(reason)`（连不上、429、5xx、连接超时：请求没发出去，可以重试）、`NoResponse`（请求发出去了、等回话读超时：**不重试**）。重试策略在 `sender`，不在适配层，这样"不偷偷重发"只在一处。

`http.py` 的实现是 ashare-quant `_post` / `poll` / `login` 的搬家：`requests.Session`、`_headers`、`_error_code`、`-14`。`profiles.py` 决定请求头和 `base_info` 的内容：`legacy` 与 ashare-quant 现在逐项相同；`official` 补 `iLink-App-Id`、`iLink-App-ClientVersion`、`channel_version 2.4.8`、`bot_agent`，并启动时 / 退出时调 `notifystart` / `notifystop`。

测试用两种假上游：**进程内的**（实现 `Upstream` 协议的小类，快，用于 sender / poller / 额度的组件测试）和 **HTTP 的**（`tests/support/fake_ilink.py`，本机回环上的真 HTTP 服务，用于测 `http.py`、两档请求形式、合约测试）。

### 4. 数据库

SQLite，WAL，外键开，`busy_timeout` 5 秒。写事务一律 `BEGIN IMMEDIATE`。下面是主要的表（省略索引和不重要的列，细节在迁移脚本里）：

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);       -- schema_version、instance_id
CREATE TABLE account_state (                                         -- 一行；以后多号再扩展
  id INTEGER PRIMARY KEY CHECK (id = 1),
  login_state TEXT NOT NULL,            -- ok | expired | missing | broken
  login_since_ms INTEGER, expired_episode INTEGER NOT NULL DEFAULT 0,
  last_poll_ok_ms INTEGER, last_poll_error TEXT, last_poll_error_ms INTEGER,
  ignored_count INTEGER NOT NULL DEFAULT 0, creds_digest TEXT);
CREATE TABLE projects (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
  prefix TEXT, prefix_key TEXT UNIQUE,                               -- prefix_key 是 NFKC + 小写后的比较值
  key_hash TEXT NOT NULL UNIQUE, prev_key_hash TEXT, prev_key_until_ms INTEGER,
  guaranteed INTEGER NOT NULL, cap INTEGER NOT NULL,
  is_default INTEGER NOT NULL DEFAULT 0, label TEXT,
  enabled INTEGER NOT NULL DEFAULT 1, push_only INTEGER NOT NULL DEFAULT 0,
  removed_ms INTEGER, created_ms INTEGER NOT NULL,
  last_seen_ms INTEGER, last_inbox_ms INTEGER, awaiting_until_ms INTEGER,
  internal INTEGER NOT NULL DEFAULT 0);                              -- 1 = 服务内部项目（_hub、_import）
CREATE UNIQUE INDEX one_default ON projects(is_default) WHERE is_default = 1;
CREATE TABLE windows (                                               -- 每个使用者的新消息一行
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_ms INTEGER NOT NULL, context_token TEXT,
  blocked_until_ms INTEGER, blocked_code TEXT);                      -- 腾讯拒绝后的暂停
CREATE TABLE usage (                                                 -- 窗口里各项目已用条数（含预占）
  window_id INTEGER NOT NULL, project_id INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('push', 'reply')), n INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (window_id, project_id, kind));
CREATE TABLE outbox (                                                -- 发送记录，没有正文
  id TEXT PRIMARY KEY, project_id INTEGER NOT NULL, kind TEXT NOT NULL, priority TEXT,
  window_id INTEGER NOT NULL, dedupe_hash TEXT, parts INTEGER NOT NULL,
  parts_sent INTEGER NOT NULL DEFAULT 0, parts_unconfirmed INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,   -- sending|sent|unconfirmed|partial|rejected|failed|skipped|interrupted
  reason_code TEXT, chars INTEGER, no_message_id INTEGER NOT NULL DEFAULT 0,
  created_ms INTEGER NOT NULL, finished_ms INTEGER);
CREATE UNIQUE INDEX outbox_dedupe ON outbox(project_id, dedupe_hash)
  WHERE dedupe_hash IS NOT NULL AND status IN ('sending','sent','unconfirmed','partial');
CREATE TABLE inbox (
  id TEXT PRIMARY KEY, project_id INTEGER NOT NULL, upstream_key TEXT, window_id INTEGER,
  text TEXT, has_text INTEGER NOT NULL, at_ms INTEGER NOT NULL, received_ms INTEGER NOT NULL,
  via TEXT NOT NULL, state TEXT NOT NULL,  -- queued|leased|acked|expired|dropped
  lease_until_ms INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
  handle TEXT UNIQUE, context_token TEXT, handle_expires_ms INTEGER,
  replies INTEGER NOT NULL DEFAULT 0);
CREATE TABLE inbound_seen (upstream_key TEXT PRIMARY KEY, seen_ms INTEGER NOT NULL);
CREATE TABLE alert_state (key TEXT PRIMARY KEY, episode TEXT, last_sent_ms INTEGER,
  pending_text TEXT, pending_since_ms INTEGER, attempts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE events (id INTEGER PRIMARY KEY, ts_ms INTEGER NOT NULL, level TEXT NOT NULL,
  kind TEXT NOT NULL, project TEXT, detail TEXT);
```

要点：

- 服务内部用的两个“项目”——`_hub`（服务自己的回复记在它名下）和 `_import`（导入 ashare-quant 凭证时带过来的已用条数）——在 `projects` 里各有一行，`internal = 1`、没有密钥（`key_hash` 填一个永远不会匹配的占位值）、保底和上限为 0、不参与保底合计、前缀、默认项目和任何列表（`hubctl project list`、`服务 项目`）。
- 去重键靠 `outbox_dedupe` 这个**部分唯一索引**：只有"发送中 / 已发 / 未确认 / 部分"的记录占着键，`rejected` / `failed` / `skipped` 一变状态键就自动释放，不用另写释放代码。检查前先把超过 `dedupe_hours` 的记录的 `dedupe_hash` 置空。
- 去重键本身只存 SHA-256 摘要（加项目作盐），数据库里没有原文。
- 项目密钥只存摘要（SHA-256）；密钥是 256 位随机数，不需要慢哈希。
- 正文只存在 `inbox.text`，确认或过期时置空；发送的正文不落盘。

### 5. 额度判定

`quota.decide(window, projects, request, cfg) -> Decision` 是纯函数，按 `hub-quota` 里写的顺序判定，输出 `Allow(n)` 或 `Deny(reason_code, 数字)`；原因文字由 `sender` 用同一个模板表生成（`hub-send` 的表）。`quota.reserve(conn, ...)` 在一个 `BEGIN IMMEDIATE` 事务里：读当前窗口和各项目用量 → `decide` → 通过就 `UPDATE usage` 并插入 `outbox`（状态 `sending`）。对账（`settle`）也是一个事务：按各条的结果退回没发出去的条数；窗口已换就把已发出去的条数记进新窗口。

用量的算式（再写一遍，实现按这个）：

```
free          = L − Σ usage（本窗口，推送 + 回复）
reserve_left  = max(R − 回复已用, 0)
unused_g(q)   = max(g_q − 推送已用_q, 0)          只算启用的项目
pool          = free − reserve_left − Σ unused_g
允许 n 条推送  ⟺  n ≤ free − reserve_left
               且 (urgent 或 已用 + n ≤ cap)
               且 max(n − unused_g(p), 0) ≤ max(pool, 0)
               且 (非 deferrable 或 free − n ≥ deferrable_keep)
```

### 6. 发送状态机

```
请求 ──► 校验（正文、字段）──► 去重键占位 ──► 判定窗口和额度 ──► 预占 ──► 拿发送锁
                                    │ 命中                │ 拒绝              │ 等锁超时
                                    ▼                     ▼                   ▼
                          返回原结果(duplicate)      rejected / skipped    退回预占，failed(busy)
拿到锁 ─► 逐条发 ──► Accepted(有编号)            → sent
                 ├─► Accepted(无编号) strict      → unconfirmed
                 │                    observe/off → sent（observe 记 no_message_id）
                 ├─► Rejected                     → 这条没发出去，退回，不重试
                 ├─► SessionExpired               → 登录置失效，这条和后面的退回
                 ├─► Transient                    → 同一个 client_id 重试 ≤ retries 次，仍失败则退回
                 └─► NoResponse                   → unconfirmed，不重试，后面的不发
结束 ─► 汇总各条 → 整条状态 → 对账（事务）→ 写发送记录 → 返回
```

整个调用有总时限 `send.timeout_seconds`；时限到了还没发完，已经发出去的按 `sent` / `unconfirmed` 算，没发的退回，整条是 `partial` 或 `failed`。

### 7. 收消息和路由

`poller` 一轮：`get_updates` → 对每条消息：不是本人的计数后丢掉 → 在**一个事务**里去重、开窗口（时间不早于当前起点才开）、调 `router.route(...)`、建待取记录 → 提交 → 写回 `updates_buf` → 发服务自己的回复（"服务 状态"、没在线提醒、有几个项目在等确认……，由 poller 线程调 `sender`，作为回复一类，记在 `_hub` 名下）。

`router.route(text, has_text, projects, now) -> Route` 是纯函数，返回 `ToProject(project, text, via)` 或 `HubReply(kind, text)`。前缀匹配把输入和每个前缀都做 NFKC + 小写再比较，只在"前缀后面是分隔符或到结尾"时命中；交给项目的文字从**原始字符串**里按匹配到的长度切（NFKC 在前缀这一小段上长度可能变，所以逐字符对齐匹配，不整句规范化）。

### 8. 取话：租约和长轮询

`inbox.fetch(project, wait, max)`：先在事务里把该项目 `queued` 的和 `leased` 且租期已过的记录（按 `received_ms` 先后）取最多 `max` 条，置 `leased`、`lease_until = now + lease`、`attempts + 1`，超过 `max_attempts` 的置 `dropped`；没有就在该项目的 `Condition` 上等（超时返回空），`poller` 存入新话以后 `notify`。同一项目的新请求会把旧请求唤醒并让它返回空（每个项目记一个"当前等待者"令牌）。"在线"= 内存里有正在等的请求，或 `last_inbox_ms` 在 `online_seconds` 之内。

### 9. 回复句柄

`rh_` 加 22 个 `secrets.token_urlsafe` 字符，存在 `inbox.handle`（唯一）。回复时按句柄查记录，核对项目、`handle_expires_ms`、`replies < max_per_handle`；不属于该项目和不存在返回同一个 404。清理任务到期后把 `context_token` 置空。

### 10. 项目密钥和认证

密钥 `chub_` + `secrets.token_urlsafe(32)`（43 个字符）。认证依赖（FastAPI `Depends`）：取 `Authorization: Bearer`，算 SHA-256，用 `hmac.compare_digest` 和库里的摘要比（先按摘要查，查到再做恒定时间比较）；换钥宽限期内也认 `prev_key_hash`。失败计数、限速都是内存里的滑动窗口（重启清零，可接受）：失败按来源地址（1 分钟 10 次），请求按项目（`send.rate_per_minute`）。

### 11. 送达确认

`message_id` 是不是出现在腾讯的发送返回里，是整个"未确认送达"机制的前提，但官方文档的示例返回是 `{"ret":0,"errmsg":""}`，没有编号；"有编号才算送达"只来自用户在腾讯问题单里的报告。所以：

- 默认档位是 `observe`：每次都记"有没有编号"，连续没有就告警，但**不**把它判成未确认。
- 在 Mac 上用真账号发几条（`docs/ops/真机验证清单.md`），确认正常账号的返回里确实有编号以后，把配置改成 `strict`。
- `strict` 才会返回 `unconfirmed` / `no_message_id`。
- 编号取哪些键（`send.message_id_keys`，默认 `message_id`、`msg_id`）也是配置。

### 12. 告警和定时任务

`scheduler` 每 30 秒一轮，里面是一组无状态的小检查（都读数据库和假时钟，所以好测）：清理过期（每分钟）、窗口快到期、腾讯不通时长、告警重试、备份（每天 `backup.at`）、心跳（更新 `run.json`）。`alerts` 把"该不该发"和"怎么发"分开：`evaluate(...)` 产出 `Alert(key, episode, text)`，`AlertState` 按 `hub-alerts` 里的去重单位判断发过没有，通过后丢给后台线程发 Bark（重试策略在那里）。

### 13. 配置

`<home>/config.yaml`，没有也行。全部有默认值，启动时校验（错误一次列全）：

```yaml
server:   { host: 127.0.0.1, port: 8790, lan: false, shutdown_seconds: 10 }
upstream: { base_url: "https://ilinkai.weixin.qq.com", profile: legacy, confirm: observe,
            poll_timeout: 45, request_timeout: 15, backoff_start: 5, backoff_max: 60,
            relogin_probe_seconds: 600 }
send:     { timeout_seconds: 30, part_chars: 2000, max_chars: 8000, retries: 2,
            rate_per_minute: 30, dedupe_hours: 24, message_id_keys: [message_id, msg_id] }
quota:    { window_hours: 24, window_limit: 10, reply_reserve: 2, deferrable_keep: 3,
            reject_hold_minutes: 30 }
inbox:    { wait_seconds: 25, max_wait: 55, lease_seconds: 60, max_attempts: 5, max_queued: 50,
            retention_minutes: 10, online_seconds: 90, offline_notice_seconds: 300 }
reply:    { handle_minutes: 30, max_per_handle: 5 }
routing:  { confirm_words: [对, 是, 确认, 好, ok, yes, y], confirm_seconds: 600 }
hub:      { label: 服务 }
alerts:   { group: clawbot-hub, unconfirmed_streak: 3, upstream_down_minutes: 10,
            window_warn: { enabled: true, hours_before: 3, between: "07:00-22:30",
                           weekdays: [1, 2, 3, 4, 5] },
            blocked: { min_interval_hours: 12 } }
display:  { timezone: Asia/Shanghai }
events:   { keep_days: 90 }
backup:   { at: "04:00", keep: 14 }
db:       { busy_timeout_ms: 5000 }
log:      { level: INFO }
```

`quota.window_hours`、`window_limit`、`reply_reserve` 和保底之和之间的约束在启动和登记 / 修改项目时都检查。

### 14. 日志和脱敏

`logging_setup` 装一个过滤器，在日志记录格式化之后把已知的秘密（当前 `bot_token`、飞行中的 `context_token`、Bark 设备码）和 `chub_[A-Za-z0-9_-]{43}` 换成 `***`；`requests` / `urllib3` 的日志级别固定在 WARNING。`http.py` 在抛出的错误信息里也先脱敏（沿用 ashare-quant 的 `_mask`），这是第一道，日志过滤器是第二道。

### 15. 备份、迁移、升级

备份用 `sqlite3.Connection.backup()`（在线，不锁写），连同凭证、配置、告警设备码复制到带时间的目录。迁移脚本按编号顺序执行，每个在事务里；升级前自动备份；版本比程序新则拒绝启动（退出码 3）。服务升级的做法：停服务 → 拉新代码 → `uv sync` → 启动（自动迁移）→ `hubctl doctor`。

### 16. 测试策略

| 层 | 测什么 | 怎么测 |
|---|---|---|
| 纯函数 | 拆条、标题、NFKC 前缀匹配、脱敏、额度判定、路由、配置校验、密钥格式 | 表格式单元测试，规格里的每个数字场景一行 |
| 组件 | 发送状态机的每个状态、预占与对账、去重键、租约与确认、轮询循环与退避、告警规则与去重、定时任务 | 临时 SQLite + 假时钟 + 进程内假上游 |
| 上游 | 两档请求形式、`-14`、各种错误映射、扫码各状态 | `http.py` 对着 `fake_ilink.py`（HTTP） |
| 接口 / 合约 | 认证、限速、错误格式、客户端、`hubctl send`、文档与接口一致 | 真 uvicorn（随机端口）+ 假上游 + 单文件客户端 |
| 命令行 | `status`、`doctor`、`backup`、`project …`、`import-credentials`、退出码 | `typer.testing.CliRunner` |
| 进程 | 单实例锁、`kill -9` 后恢复、`SIGTERM` 优雅退出、权限 | 子进程，少量 |
| 真机 | 腾讯一侧的规则（有无消息编号、窗口实测等） | `live` 标记，默认不跑，Mac 上使用者授权后手动跑 |

测试文件按模块分目录，和 `src/clawbot_hub/` 一一对应（`tests/quota/`、`tests/sender/` ……），像 ashare-quant 那样方便只跑受影响的部分。

## Risks / Trade-offs

- **单点**：服务挂了所有项目的微信都没了。→ Bark 不经过服务；launchd 拉起；客户端 `HubUnavailable` 就走 Bark；状态页 / 日报显示服务状态。
- **同步发送占线程**：腾讯慢的时候，发送请求会占着 FastAPI 的线程。→ 每个项目每分钟 30 次的限速 + 发送总时限 30 秒 + 线程池上限，量级远在安全范围。
- **"有消息编号"没验证**：→ 默认 `observe`，真机验证后才改 `strict`；这是使用者要点头的默认值之一。
- **窗口和条数不是官方的**：→ 全是配置；被拒绝就暂停并告警（`upstream_blocked`）；窗口快到期提醒。
- **一号一绑不确定**：→ 设计按"成立"来，只有服务登录；迁移时不重新扫码，用导入。
- **回复用的对话凭证能用多久不知道**（有用户说只有 1–2 分钟）：→ 回复被拒绝时项目拿到原因；`reply.handle_minutes` 只是服务这边的上限；真机验证项里测。
- **SQLite 多进程写**（服务 + `hubctl` 管理命令）：→ WAL + `busy_timeout` + 管理命令的写事务都很短。
- **客户端 Python 3.9 兼容**：→ 不用 3.10 以后的语法（`match`、`X | Y` 类型写法放进字符串注解或用 `typing`），测试里用语法检查工具验证。

## Migration Plan

服务本身是新的，没有数据要迁。要迁的是 ashare-quant 现在的微信凭证和额度状态：

1. 本变更（`h1-hub-v1`）在新仓库里完成，用假上游测通，ashare-quant 一行不动。
2. Mac 上装服务、绑定到假上游或只做离线检查（`hubctl doctor`），登记 ashare-quant，演练。
3. ashare-quant 里另立一个变更加"走服务"的模式（`wechat.mode: direct | hub`，默认 `direct`），先和 Mac 上未推送的本机提交对齐。
4. 挑周末：停旧的微信问答任务 → 备份 `data/notify/clawbot.json` → `hubctl import-credentials` → 启动服务 → 把 ashare-quant 切到 `hub` → 手动推一条简报、微信里发一句"状态"，两边都收到才算通。
5. 出问题就退：停服务，把备份放回去，配置改回 `direct`，重启旧任务（只要中间没重新扫过码，凭证还有效）。
6. 稳定一段时间后，在 ashare-quant 里删掉直连腾讯的代码（单独一个变更）。

具体步骤和每步的检查写在 `docs/ops/迁移手册.md`。

## Open Questions

1. 发送返回里到底有没有消息编号（决定 `confirm` 能不能改 `strict`）——真机验证。
2. 其他项目的情况（几个、在哪台机器、用什么语言、每天推几条）——决定要不要开局域网监听、各项目保底多少。默认值按"ashare-quant 保底 6、第一个其他项目保底 2、回复保留 2"。
3. 其他项目的前缀叫什么（登记时再定）。
4. `official` 档位是否值得切换（腾讯服务端有没有开始校验那几个请求头）——真机验证，默认 `legacy`。
5. 仓库的最终名字和位置（默认 `Lhanman/clawbot-hub`，私有）。
