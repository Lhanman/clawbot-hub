# Design

## Context

设计方案（`docs/specs/2026-10-09-ClawBot共用服务-设计方案.md`，使用者 2026-10-10 批准方向）定了"做什么"：一个独立的常驻服务独占 ClawBot 的登录和收消息，各项目拿密钥通过本机接口收发。这份文档定"怎么做"。

现成可以直接参照的是 ashare-quant 里的 `notify/clawbot.py`（登录、收、发、记额度，约 470 行）、`notify/http.py`（重试、脱敏）、`notify/base.py`（拆条）和 `chat/bot.py`（收消息循环、退避、确认的 10 分钟）。搬过来的行为（拆条规则、-14 的判断顺序、脱敏、错误文案）与它**一致，设计方案第 14 节逐条列出的有意改动除外**；这些改动各对应一条测试。新写的是多项目的部分：项目登记、额度分配、路由、租约、告警。

规模：粗估服务本体 1800 行上下，测试同量级。

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
┌───────────────────────────── 同一个进程 ─────────────────────────────────┐
│ api ──► projects（认密钥、限速）                                          │
│   │ ├──► sender（拆条、预占额度、发、对账）──► upstream(发送用 Session)   │
│   │ └──► inbox（待取队列、租约、确认、回复句柄）                          │
│ poller ──► upstream(收消息用 Session).get_updates ──► router ──► inbox   │
│                          └──► replier 队列                               │
│ replier（服务自己的回复，单独的线程）──► sender                           │
│ watcher（每 5 秒比对凭证摘要）                                            │
│ scheduler（每 30 秒一次）──► 清理、窗口提醒、不通判断、备份、心跳         │
│ alerts（后台线程）──► Bark                                               │
│ 全部状态在 SQLite（WAL）里；凭证在 account.json                           │
└──────────────────────────────────────────────────────────────────────────┘
```

发送锁：同一时间只有一条消息在向腾讯发（`threading.Lock`），保证同一条消息的几段连续、不同项目的消息不穿插。额度判定和预占**在拿发送锁之前**做（数据库事务保证不超额），所以被拒绝的请求立刻返回，不用排队。等锁的时间算在这条发送自己的总时限里（第 6 节）。

`requests.Session` 不共用：收消息线程一个，发送一个（发送的每次请求用新连接，第 3 节），文档不保证 `Session` 线程安全。

服务自己的回复（"服务 状态"的回答、"没在线"提醒、"没有设默认项目"）不在收消息线程里发：poller 把它放进一个有上限的内存队列（20 条，满了丢最旧的），由 `replier` 线程按顺序调 `sender` 发，所以发送锁被占着时收消息不会停。这类回复是**尽力而为、至多一次**：队列里还没发出的，服务被杀就丢了，不补（话本身已经按规则入队，没有丢）。

### 2. 模块划分

```
src/clawbot_hub/
  config.py        配置：读取、默认值、校验（错误一次列全）
  clock.py         Clock：now_ms()、sleep()；测试里换假时钟
  textutil.py      拆条、取标题、NFKC 匹配、脱敏（纯函数，从 ashare-quant 搬）
  db.py            连接（secure_delete、外键、busy_timeout）、事务（BEGIN IMMEDIATE）、迁移执行器
  migrations/      0001_init.sql …
  upstream/        base.py（协议和结果类型）、http.py（腾讯 HTTP）、profiles.py（legacy / official）
  account.py       account.json 读写（0600、锁、原子改名、写回进度时核对 bot_token）
  login.py         扫码流程、导入
  watcher.py       凭证监视线程
  projects.py      项目增删改、密钥、校验
  quota.py         额度判定（纯函数）+ 预占 / 逐条对账 / 退回（事务）
  sender.py        发消息和回复：校验、去重、拆条、发送状态机、对账
  router.py        路由（纯函数）
  inbox.py         待取队列、租约、确认、回复句柄、清理
  poller.py        收消息循环：长轮询、排序、去重、窗口、路由、入队
  replier.py       服务自己的回复：内存队列 + 发送线程、限频
  alerts.py        告警规则、去重状态、Bark 发送与重试
  scheduler.py     定时任务
  api.py           FastAPI 路由、认证、限速、错误格式
  app.py           装配和生命周期（启动自检、关闭）
  cli.py           hubctl（typer）
  backup.py  doctor.py  logging_setup.py
client/clawbot_hub_client.py     单文件客户端
tests/  support/fake_ilink.py  contract/  …
```

依赖方向：`api → sender / inbox / projects → quota / db`；`poller → upstream / router / inbox / replier`；`cli → projects / login / backup / doctor`，只有 `cli serve` 才引入 `api` 和 `app`。纯函数模块（`quota` 的判定、`router`、`textutil`）不碰数据库和网络，表格式测试。`app.py` 提供 `create_app(config, upstream, clock)`，测试和 `hubctl serve` 用同一个装配函数。

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

`SendOutcome` 是一个小的联合：`Accepted(message_id | None)`、`Rejected(code, errmsg)`、`SessionExpired`、`Transient(reason)`（请求**没发出去**，可以重试）、`NoResponse`（请求**发出去了**、不知道结果：**不重试**）。重试策略在 `sender`，不在适配层，这样"不偷偷重发"只在一处。

`http.py` 把 `requests` 的各种情形归到这五类，这是一张固定的表（`hub-send` 里也有）：

| 情形 | 类别 |
|---|---|
| DNS 失败、连接被拒、连接超时、TLS 握手失败 | `Transient` |
| 收到完整的 HTTP 429 或 5xx 响应（不看响应体，里面带 -14 也一样） | `Transient` |
| 请求发完之后读超时；连接被对方重置或关闭；响应读到一半断开 | `NoResponse` |
| HTTP 4xx（429 除外）；业务码非 0 且不是 -14（先取 `ret`，没有再取 `errcode`） | `Rejected` |
| 业务码是 -14 | `SessionExpired` |
| HTTP 200 且业务码为空或 0 | `Accepted`（再看有没有消息编号） |

`sendmessage` **每次用新连接**（请求带 `Connection: close`，或发送专用的 `Session` 不复用连接）：服务端早已关掉的空闲连接，下一次 POST 的常见表现就是"被对端重置"，每天只发几条的服务里这几乎是"每天的第一条"，复用连接会让它被误判成"不知道到没到"。待确认：用假上游主动断开空闲连接复现一次，写进 `test_http_outcomes.py`。

`profiles.py` 决定请求头和 `base_info` 的内容：`legacy` 的期望值直接写在 `hub-login` 里（4 个请求头、`msg` 7 个字段、`channel_version "1.0.2"`），也是 A4 的测试夹具；`official` 补 `iLink-App-Id`、`iLink-App-ClientVersion`、`channel_version 2.4.8`、`bot_agent`，并启动时 / 退出时调 `notifystart` / `notifystop`，取二维码改 `POST`（官方文档里的写法，没有在真账号上验证过，列入真机验证清单）。

测试用两种假上游：**进程内的**（实现 `Upstream` 协议的小类，快，用于 sender / poller / 额度的组件测试）和 **HTTP 的**（`tests/support/fake_ilink.py`，本机回环上的真 HTTP 服务，也能用 `python -m tests.support.fake_ilink --port N` 独立运行，用于测 `http.py`、两档请求形式、合约测试和 Mac 上的演练）。两种假件各有自己的测试，免得假件写错而上层测试全绿。

### 4. 数据库

SQLite，WAL，外键开，`busy_timeout` 5 秒，每个连接 `PRAGMA secure_delete = ON`（被清掉的正文和凭证不留在空闲页里）。写事务一律 `BEGIN IMMEDIATE`。下面是主要的表（省略不重要的索引，细节在迁移脚本里）：

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);       -- schema_version、instance_id
CREATE TABLE account_state (                                         -- 一行；表按单账号设计，多号时再加列
  id INTEGER PRIMARY KEY CHECK (id = 1),
  login_state TEXT NOT NULL,            -- ok | expired | missing | broken
  login_since_ms INTEGER, expired_episode INTEGER NOT NULL DEFAULT 0,
  creds_digest TEXT,                    -- bot_token、base_url、bot_id、user_id 四项的摘要
  last_upstream_ok_ms INTEGER,          -- 最近一次与腾讯完成往返（upstream_down 的依据）
  last_poll_ok_ms INTEGER, last_poll_error TEXT, last_poll_error_ms INTEGER,
  ignored_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE projects (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
  prefix TEXT, prefix_key TEXT UNIQUE,                               -- prefix_key 是 NFKC + 小写后的比较值；移除项目时两列置空
  key_hash TEXT NOT NULL UNIQUE, prev_key_hash TEXT, prev_key_until_ms INTEGER,
  guaranteed INTEGER NOT NULL, cap INTEGER NOT NULL,
  is_default INTEGER NOT NULL DEFAULT 0,
  label TEXT,                                                        -- 登记时落成具体值，空 = 没有标签
  enabled INTEGER NOT NULL DEFAULT 1, push_only INTEGER NOT NULL DEFAULT 0,
  internal INTEGER NOT NULL DEFAULT 0,                               -- 1 = 服务内部名字 _hub、_import
  removed_ms INTEGER, created_ms INTEGER NOT NULL,
  last_seen_ms INTEGER, last_inbox_ms INTEGER,                       -- 最后出现 / 最近一次取话或确认（在线判断）
  awaiting_until_ms INTEGER);                                        -- 等确认标记
CREATE UNIQUE INDEX one_default ON projects(is_default) WHERE is_default = 1;
CREATE TABLE windows (                                               -- 每个使用者的新消息一行
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_ms INTEGER NOT NULL,
  context_token TEXT,                                                -- 窗口过期或被新窗口取代时置空
  blocked_until_ms INTEGER, blocked_code TEXT);                      -- 腾讯拒绝后的暂停
CREATE TABLE usage (                                                 -- 窗口里各项目已用条数（含预占）
  window_id INTEGER NOT NULL, project_id INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('push', 'reply')), n INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (window_id, project_id, kind));
CREATE TABLE outbox (                                                -- 发送记录，没有正文
  id TEXT PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
  kind TEXT NOT NULL CHECK (kind IN ('push', 'reply')), priority TEXT,
  window_id INTEGER REFERENCES windows(id) ON DELETE SET NULL,       -- 可为空：没有窗口时被拒绝的记录，或窗口已按留存期删掉
  dedupe_hash TEXT, parts INTEGER NOT NULL,
  parts_sent INTEGER NOT NULL DEFAULT 0, parts_unconfirmed INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,   -- sending|sent|unconfirmed|partial|rejected|failed|skipped|interrupted
  reason_code TEXT, reason TEXT,                                     -- reason：变量已代入的中文原因，不含正文
  chars INTEGER, no_message_id INTEGER NOT NULL DEFAULT 0,
  created_ms INTEGER NOT NULL, finished_ms INTEGER);
CREATE UNIQUE INDEX outbox_dedupe ON outbox(project_id, dedupe_hash)
  WHERE dedupe_hash IS NOT NULL
    AND status IN ('sending', 'sent', 'unconfirmed', 'partial', 'interrupted');
CREATE TABLE inbox (
  id TEXT PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
  upstream_key TEXT, window_id INTEGER,
  text TEXT, has_text INTEGER NOT NULL,
  at_ms INTEGER NOT NULL, received_ms INTEGER NOT NULL,              -- 取话按 at_ms、入库先后（rowid）排
  via TEXT NOT NULL CHECK (via IN ('prefix', 'confirm', 'default')),
  state TEXT NOT NULL,   -- queued|leased|acked|expired|dropped
  lease_until_ms INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
  handle TEXT UNIQUE, context_token TEXT, handle_expires_ms INTEGER,
  replies INTEGER NOT NULL DEFAULT 0);
CREATE INDEX inbox_fetch ON inbox(project_id, state, at_ms);
CREATE TABLE inbound_seen (upstream_key TEXT PRIMARY KEY, seen_ms INTEGER NOT NULL);
CREATE TABLE notice_state (key TEXT PRIMARY KEY, last_ms INTEGER NOT NULL);   -- 服务回复的限频：offline:<项目> / disabled:<项目> / nodefault
CREATE TABLE alert_state (key TEXT PRIMARY KEY, episode TEXT, last_sent_ms INTEGER,
  pending_text TEXT, pending_since_ms INTEGER, attempts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE events (id INTEGER PRIMARY KEY, ts_ms INTEGER NOT NULL, level TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('login','alert','send','hold','project','queue','backup','lifecycle')),
  project TEXT, detail TEXT);
```

要点：

- 服务内部用的两个"项目"——`_hub`（服务自己的回复记在它名下）和 `_import`（导入 ashare-quant 凭证时带过来的已用条数，类别是推送）——在 `0001_init.sql` 里预置两行：`internal = 1`、`enabled = 0`、`push_only = 1`、`guaranteed = 0`、`cap = 0`，`key_hash` 填 `internal:_hub` 这样的占位值（永远不会等于 64 位十六进制的 SHA-256 摘要，所以不可能认成它们）。`project list`、`服务 项目`、路由、保底合计、默认项目候选一律过滤 `internal = 1`。它们名下的用量只算进窗口总已用（`_hub` 的回复同时算进回复已用）。
- 移除项目：保留这一行（名字不能再用，`key_hash` 仍占着唯一性，认证时过滤 `removed_ms IS NULL`），`prefix`、`prefix_key` 置空，前缀就释放了。
- 去重键靠 `outbox_dedupe` 这个**部分唯一索引**：只有"发送中 / 已发 / 未确认 / 部分 / 中断"的记录占着键，`rejected` / `failed` / `skipped` 一变状态键就自动释放，不用另写释放代码。`interrupted` 占着键，因为它正是"不知道发出去了几条"；500 `internal` 时已经向腾讯发过请求的记录标 `interrupted`，没碰过腾讯的标 `failed`。键从 `created_ms` 起占 `send.dedupe_hours`，到点把 `dedupe_hash` 置空（检查前先做，不依赖定时任务）。
- 去重键本身只存 SHA-256 摘要（加项目作盐），数据库里没有原文；`reason` 存中文原因（变量已代入，不含正文），重放去重结果时用它还原。
- 项目密钥只存摘要（SHA-256）；密钥是 256 位随机数，不需要慢哈希。
- 正文只存在 `inbox.text`，确认、过期、作废时置空（`secure_delete` 保证旧字节被擦掉）；发送的正文不落盘。对话凭证（`windows.context_token`、`inbox.context_token`）的清理时机见 `hub-deploy`。
- 窗口、用量、发送记录同一个留存期（`events.keep_days`）；删窗口时 `outbox.window_id` 因 `ON DELETE SET NULL` 自动置空。

### 5. 额度判定

`quota.decide(window, projects, request, cfg) -> Decision` 是纯函数，按 `hub-quota` 里写的顺序判定，输出 `Allow(n)` 或 `Deny(reason_code, 数字)`；原因文字由 `sender` 用同一个模板表生成（`hub-send` 的表）。`quota.reserve(conn, ...)` 在一个 `BEGIN IMMEDIATE` 事务里：读当前窗口和各项目用量 → `decide` → 通过就 `UPDATE usage` 并插入 `outbox`（状态 `sending`，记下预占窗口 W 和对话凭证）。

用量的算式（再写一遍，实现按这个）：

```
free          = L − Σ usage（本窗口，推送 + 回复，含 _hub、_import）
reserve_left  = max(R − 回复已用, 0)
unused_g(q)   = max(g_q − 推送已用_q, 0)          只算启用的项目
pool          = free − reserve_left − Σ unused_g
允许 n 条推送  ⟺  n ≤ free − reserve_left
               且 (urgent 或 已用 + n ≤ cap)
               且 max(n − unused_g(p), 0) ≤ max(pool, 0)
               且 (非 deferrable 或 free − n ≥ deferrable_keep)
```

**逐条对账**（不等整条结束）：每发完一条在自己的小事务里记一次。预占窗口为 W，这一条发出去了（`sent`、`unconfirmed`）：当前窗口还是 W 就保持；已经换了新窗口，在**当前窗口**的 `usage` 里（同项目、同类别）加 1，W 不退。这一条没发出去：当前窗口还是 W 就从 W 退回；已经换了就不退。回复一律记进预占时的最新窗口。这与 ashare-quant 一致（它每发一条看一次窗口，`recount`）。

### 6. 发送状态机

检查顺序（`hub-send` 里有同样的表）：校验正文和字段 → 去重键 → `too_long`（拆出的条数超过 L−R 或回复的 L）→ 登录状态（`login_*`，不看 `on_closed`）→ 窗口关着（`on_closed: skip` 则 `skipped`，否则 `rejected`）→ 推送处于暂停期（`upstream_blocked`）→ `quota.decide` → 预占 → 拿发送锁 → 逐条发。

```
请求 ──► 校验 ──► 去重键占位 ──► 登录/窗口/暂停 ──► 判定额度 ──► 预占 ──► 拿发送锁
                       │ 命中                │ 拒绝/跳过             │ 等锁到时限、或服务在关闭
                       ▼                     ▼                      ▼
             返回原结果(duplicate)    rejected / skipped    退回预占，failed(busy)
拿到锁 ─► 逐条发 ──► Accepted(有编号)            → sent，对账
                 ├─► Accepted(无编号) strict      → unconfirmed
                 │                    observe/off → sent（observe 记 no_message_id）
                 ├─► Rejected                     → 这条没发出去，退回，不重试
                 ├─► SessionExpired               → 登录置失效，这条和后面的退回
                 ├─► Transient                    → 同一个 client_id 重试 ≤ retries 次，仍失败则退回
                 └─► NoResponse                   → unconfirmed，不重试，后面的不发
结束 ─► 汇总各条 → 整条状态 → 写发送记录 → 返回
```

**时间预算**：整个调用共用一份 `send.timeout_seconds`（默认 30），从服务开始处理这个 POST 起算，等发送锁和向腾讯发请求都在里面；每次向腾讯发请求的连接和读超时取 `min(upstream.request_timeout, 剩下的预算)`，重试之前剩不到 1 秒就不再开始新的尝试；预算用完：还在等锁的 `failed` / `busy`，已发出在等回话的按读超时算 `unconfirmed`，没开始的退回。服务关闭的等待 `server.shutdown_seconds`（默认 35）比一次发送的上限（30 秒预算）多 5 秒，所以正常关闭时在发的那条一定能发完；还在等锁的请求关闭时马上 `busy` 结束。

同一个去重键的并发请求：第二个请求等第一个收尾，最长等自己的预算；第一个在时限内收尾就返回它的结果（`duplicate: true`），否则返回 `unconfirmed` / `duplicate_in_flight`。500 `internal` 时已向腾讯发过请求的记录标 `interrupted`（占着键），重放时对外按 `unconfirmed` / `interrupted` 返回。

### 7. 收消息和路由

`poller` 一轮：`get_updates` → 先把这一批消息按 `at_ms` 排序（与 ashare-quant 一致）→ 对每条消息：不是本人的计数后丢掉，没有对话凭证的不收 → 在**一个事务**里去重、开窗口（时间不早于当前起点才开；新窗口开出时清掉被取代的窗口的对话凭证）、调 `router.route(...)`、建待取记录和需要的限频记录 → 提交 → 在 `account.json.lock` 里写回 `updates_buf`（文件里的 `bot_token` 还是这一轮用的那个才写）→ 把服务自己的回复放进 `replier` 队列，不等它发。

`router.route(text, has_text, snapshot, now) -> Route` 是纯函数，`snapshot` 是项目表（启用、是否默认、前缀、在线、等确认）的快照，返回 `ToProject(project, text, via)` 或 `HubReply(kind, args)`，不碰数据库。前缀匹配把输入和每个前缀都做 NFKC + 小写再比较，只在"前缀后面是分隔符或到结尾"时命中，命中以后连续的分隔符全部剥掉；交给项目的文字从**原始字符串**里按匹配到的长度切（NFKC 在前缀这一小段上长度可能变，所以逐字符对齐匹配，不整句规范化），再去首尾空白。"服务"这个指令词和前缀的禁用词都取 `hub.label`。

路由之后由 poller 做两件事：目标项目不在线（`hub-projects` 的定义）时入队的同时加一条"没在线"提醒；"没有默认项目""项目已停用""没在线"三种提示用 `notice_state` 限频（同一种、同一个目标在 `inbox.offline_notice_seconds` 内最多一条，重启不清零）。

### 8. 取话：租约和长轮询

`inbox.fetch(project, wait, max)`：先在事务里把该项目 `queued` 的和 `leased` 且租期已过的记录取出——先把收到已超过保留期的标 `expired`（清正文和凭证），再按 `at_ms`、`rowid` 取最多 `max` 条没超过 `max_attempts` 的，置 `leased`、`lease_until = now + lease`、`attempts + 1`；没有就在该项目的 `Condition` 上等（超时返回空），`poller` 存入新话以后 `notify`。每次被唤醒和返回之前，都再读一次项目的启用状态，已停用就 403 `project_disabled`。同一项目的新请求会把旧请求唤醒并让它返回空（每个项目记一个"当前等待者"令牌，重启清零）。`max=0` 不租出任何话，立刻返回，只当心跳。

**在线**：此刻有它的取话请求挂着（内存里的等待者），或 `last_inbox_ms`（任何取话或确认请求到达、挂着的请求返回时更新）在 `inbox.online_seconds` 之内；已停用 / 只推送的永远不在线。`last_inbox_ms` 在库里，所以重启后仍算数。

超过 `max_attempts` 的话不在取话里作废：每分钟的清理任务处理"租期已过且已租出 `max_attempts` 次"的记录——标 `dropped`、清正文和凭证、写事件。

### 9. 回复句柄

`rh_` + `secrets.token_urlsafe(16)`（16 字节，22 个字符），存在 `inbox.handle`（唯一）。回复时按句柄查记录，先核对项目（不属于该项目和不存在返回同一个 404），再核对有效期 `min(received_ms + handle_minutes, at_ms + window_hours)` 和话的状态（过期、作废、重新登录之后的都是 410），再看 `replies < max_per_handle`。`hubctl login` 成功时在同一个事务里把所有 `inbox.context_token` 置空。清理任务在句柄失效后把 `context_token` 置空；有效期的判断本身按时间现算，不依赖清理任务。

### 10. 项目密钥和认证

密钥 `chub_` + `secrets.token_urlsafe(32)`（43 个字符）。认证依赖（FastAPI `Depends`）：取 `Authorization: Bearer`，算 SHA-256，先按摘要查（过滤已移除的），查到再用 `hmac.compare_digest` 做恒定时间比较；换钥宽限期内也认 `prev_key_hash`。

检查的先后：来源地址的失败限流（只对非回环地址）→ 认证 → 项目级请求限速（`send.rate_per_minute`，只对发消息和回复）→ 请求体大小 → 业务。限流和限速都是内存里的滑动窗口（重启清零，可接受）：认证失败按来源地址（局域网来源 1 分钟 10 次，127.0.0.0/8 和 `::1` 不计也不受限，被 429 拒绝的请求不再计入失败），请求按项目。来源地址取连接的对端地址，测试里可以注入。

### 11. 送达确认

`message_id` 是不是出现在腾讯的发送返回里，是整个"未确认送达"机制的前提，但官方文档的示例返回是 `{"ret":0,"errmsg":""}`，没有编号；"有编号才算送达"只来自用户在腾讯问题单里的报告。所以：

- 默认档位是 `observe`：每次都记"有没有编号"，连续没有就告警，但**不**把它判成未确认。
- 在 Mac 上用真账号发几条（`docs/ops/真机验证清单.md`），确认正常账号的返回里确实有编号以后，把配置改成 `strict`。
- `strict` 才会返回 `unconfirmed` / `no_message_id`。
- 编号取哪些键（`send.message_id_keys`，默认 `message_id`、`msg_id`）也是配置。

### 12. 告警和定时任务

`scheduler` 每 30 秒一轮，里面是一组无状态的小检查（都读数据库和假时钟，所以好测）：清理（每分钟：过期 / 作废的话、7 天前的记录、去重键到期）、窗口快到期、窗口被拒的汇总、腾讯不通时长、告警重试、备份（每天 `backup.at`，失败 10 分钟后重试一次）、心跳（更新 `run.json` 的 `last_heartbeat_ms`）。`alerts` 把"该不该发"和"怎么发"分开：`evaluate(...)` 产出 `Alert(key, episode, text)`，`AlertState` 按 `hub-alerts` 里的去重单位判断发过没有，通过后丢给后台线程发 Bark（立即重试 `alerts.bark_retries` 次，间隔从 1 秒起翻倍；暂时失败的进待发队列，每 `queue_retry_minutes` 分钟重试，`queue_give_up_minutes` 分钟后放弃；Bark 明确拒绝的不重试）。

### 13. 配置

`<home>/config.yaml`，没有也行。全部有默认值，启动时校验（错误一次列全），只在启动时读一次：

```yaml
server:   { host: 127.0.0.1, port: 8790, lan: false, shutdown_seconds: 35 }
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
routing:  { confirm_words: [对, 是, 确认], confirm_seconds: 600 }
hub:      { label: 服务 }
alerts:   { group: clawbot-hub, bark_timeout_seconds: 10, bark_retries: 3,
            queue_retry_minutes: 5, queue_give_up_minutes: 60,
            unconfirmed_streak: 3, upstream_down_minutes: 10, restart_merge_minutes: 10,
            window_warn: { enabled: true, hours_before: 3, between: "07:00-22:30",
                           weekdays: [1, 2, 3, 4, 5] },      # ISO 编号，周一 = 1
            blocked: { min_interval_hours: 12 } }
display:  { timezone: Asia/Shanghai }
events:   { keep_days: 90 }
backup:   { at: "04:00", keep: 14 }
db:       { busy_timeout_ms: 5000 }
log:      { level: INFO, max_mb: 5, keep: 5 }
```

约束：`quota.window_limit` 至少比 `reply_reserve` 大 1；`server.lan` 与 `server.host` 的关系见 `hub-deploy`；登记、修改、启用项目时检查保底之和加回复保留不超过窗口总条数。配置里把 `window_limit` 改小，和库里已登记的保底冲突：启动**只警告**（事件、`status` 提示、`doctor` 错误），不拒绝启动，`decide` 里的 `max(pool, 0)` 保证这种状态下不会算出负的额度。

**固定常量**（不进配置，写在代码里并各有测试）：

| 常量 | 值 | 为什么固定 |
|---|---|---|
| 项目名、前缀、标签的格式和长度 | 名字 `^[a-z][a-z0-9-]{1,31}$`；前缀 1–8 字；标签 1–16 字 | 格式规则，改了会让已有数据失效 |
| 项目密钥 | `chub_` + 43 字符 | 格式 |
| 回复句柄 | `rh_` + 22 字符 | 格式 |
| 请求体上限 | 64 KiB | 防护栏 |
| 认证失败限流 | 局域网来源 1 分钟 10 次 | 安全限流，不该被调松 |
| 换钥宽限上限 | 60 分钟 | 防护栏 |
| `dedupe_key` 长度 | 1–128 字符 | 格式 |
| `confirm_seconds` 上限 | 1800 秒 | 防护栏 |
| 取话 `max` | 默认 10、上限 50；`ack` 一次最多 50 个 | 防护栏 |
| 扫码 | 最多 5 张，每张最多等 180 秒，每秒查一次，查失败等 2 秒 | 与腾讯的二维码寿命配套 |
| 凭证监视 | 每 5 秒；`login.pending` 20 分钟规则；凭证 60 秒内变过的 -14 不报警 | 实现细节 |
| 定时任务周期 | 30 秒（心跳同周期，`run.json` 超过 60 秒没更新就当服务没在运行） | 实现细节 |
| 待取记录、收消息去重记录的保留 | 7 天 | 与句柄、保留期配套 |
| 服务回复的内存队列 | 20 条 | 防护栏 |
| `doctor` 的阈值 | 磁盘 100 MB、备份 2 天 | 运维提示，不是行为 |
| 发送重试的间隔 | 1 秒起每次翻倍（次数 `send.retries` 可配） | 与 ashare-quant 一致 |
| 拆条标题上限 | 30 个字，超出加"…" | 设计方案第 14 节的有意改动 |

**重启后的状态**（`hub-deploy` 有对应场景）：

| 状态 | 在哪 | 重启后 |
|---|---|---|
| 窗口、用量、发送记录、去重摘要 | 数据库 | 保持（"发送中"变"中断"） |
| 待取队列、租约、回复句柄 | 数据库 | 保持 |
| 等确认标记、最近取话时间 | 数据库 | 保持 |
| 告警状态和待发队列、服务回复的限频时间 | 数据库 | 保持 |
| 登录状态 | 数据库 + `account.json` | 保持 |
| 请求限速计数、认证失败计数 | 内存 | 清零 |
| 取话的等待者令牌和正挂着的请求 | 内存 | 清零（连接断了，项目重新连） |
| 服务回复的内存队列 | 内存 | 丢（至多一次） |

### 14. 日志和脱敏

`logging_setup` 装一个过滤器，在日志记录格式化之后把已知的秘密（当前 `bot_token`、飞行中的 `context_token`、Bark 设备码）和 `chub_[A-Za-z0-9_-]{43}` 换成 `***`；`requests` / `urllib3` 的日志级别固定在 WARNING。`http.py` 在抛出的错误信息里也先脱敏（沿用 ashare-quant 的 `_mask`），这是第一道，日志过滤器是第二道。日志轮转用标准库的 `RotatingFileHandler`（`log.max_mb`、`log.keep`）。

### 15. 备份、迁移、升级

备份用 `sqlite3.Connection.backup()`（在线，不锁写），连同凭证、配置、告警设备码复制到带时间的目录；目录名区分三种：自动的 `auto-<时间>`、升级前的 `pre-upgrade-v<旧>-v<新>-<时间>`、手动的 `<时间>`，自动清理只数 `auto-` 开头的。迁移脚本按编号顺序执行，每个在事务里；升级前自动备份；版本比程序新则拒绝启动（退出码 3）。服务升级的做法：`launchctl bootout` 停服务 → 拉新代码 → `uv sync` → 启动（自动迁移）→ `hubctl doctor`。

### 16. 测试策略

| 层 | 测什么 | 怎么测 |
|---|---|---|
| 纯函数 | 拆条、标题、NFKC 前缀匹配、脱敏、额度判定、路由、配置校验、密钥格式 | 表格式单元测试，规格里的每个数字场景一行 |
| 组件 | 发送状态机的每个状态、预占与对账、去重键、租约与确认、轮询循环与退避、告警规则与去重、定时任务 | 临时 SQLite + 假时钟 + 进程内假上游 |
| 上游 | 两档请求形式、`-14` 的判断顺序、各种错误映射、扫码各状态 | `http.py` 对着 `fake_ilink.py`（HTTP） |
| 接口 / 合约 | 认证、限速、错误格式、客户端、`hubctl send`、文档与接口一致 | 真 uvicorn（随机端口）+ 假上游 + 单文件客户端 |
| 命令行 | `status`、`doctor`、`backup`、`project …`、`import-credentials`、退出码 | `typer.testing.CliRunner` |
| 进程 | 单实例锁、`kill -9` 后恢复、`SIGTERM` 优雅退出、权限 | 子进程，少量 |
| 真机 | 腾讯一侧的规则（有无消息编号、窗口实测等） | `tests/live/test_real_ilink.py`，`live` 标记，默认不跑，Mac 上使用者授权后手动跑 |

测试文件按模块分目录，大体对应 `src/clawbot_hub/`（`tests/quota/`、`tests/sender/` ……）；跨模块的（收消息循环放在 `tests/poller/`，备份和 `doctor` 在 `tests/cli/`）以 `tasks.md` 的汇总表为准，只跑受影响的部分时按那张表选目录。

## Risks / Trade-offs

- **单点**：服务挂了所有项目的微信都没了。→ Bark 不经过服务；launchd 拉起；客户端 `HubUnavailable` 就走 Bark；状态页 / 日报显示服务状态。
- **同步发送占线程**：腾讯慢的时候，发送请求会占着 FastAPI 的线程。→ 每个项目每分钟 30 次的限速 + 发送总时限 30 秒 + 线程池上限，量级远在安全范围。
- **"有消息编号"没验证**：→ 默认 `observe`，真机验证后才改 `strict`；这是使用者要点头的默认值之一。
- **窗口和条数不是官方的**：→ 全是配置；被拒绝就暂停并告警（`upstream_blocked`）；窗口快到期提醒。
- **一号一绑不确定**：→ 设计按"成立"来，只有服务登录；迁移时不重新扫码，用导入。
- **回复用的对话凭证能用多久不知道**（有用户说只有 1–2 分钟）：→ 回复被拒绝时项目拿到原因；`reply.handle_minutes` 只是服务这边的上限；真机验证项里测。
- **SQLite 多进程写**（服务 + `hubctl` 管理命令）：→ WAL + `busy_timeout` + 管理命令的写事务都很短。
- **客户端 Python 3.9 兼容**：→ 不用 3.10 以后的语法（`match`、`X | Y` 类型写法放进字符串注解或用 `typing`），测试里用语法检查工具验证。
- **服务自己的回复至多一次**：→ 被杀时丢一条"没在线"之类的提醒；话本身已入队，不丢，可接受。

## Migration Plan

服务本身是新的，没有数据要迁。要迁的是 ashare-quant 现在的微信凭证和额度状态：

1. 本变更（`h1-hub-v1`）在新仓库里完成，用假上游测通，ashare-quant 一行不动。
2. Mac 上装服务、绑定到假上游或只做离线检查（`hubctl doctor`），登记 ashare-quant，演练。
3. ashare-quant 里另立一个变更加"走服务"的模式（`wechat.mode: direct | hub`，默认 `direct`），先和 Mac 上未推送的本机提交对齐。
4. 挑周末：停旧的微信问答任务 → 备份 `data/notify/clawbot.json` → `launchctl bootout` 停掉服务（如果已装）→ `hubctl import-credentials` → 启动服务 → 把 ashare-quant 切到 `hub` → 手动推一条简报、微信里发一句"状态"，两边都收到才算通。
5. 出问题就退：停服务，把备份放回去，配置改回 `direct`，重启旧任务（只要中间没重新扫过码，凭证还有效）。
6. 稳定一段时间后，在 ashare-quant 里删掉直连腾讯的代码（单独一个变更）。

具体步骤和每步的检查写在 `docs/ops/迁移手册.md`。

## Open Questions

1. 发送返回里到底有没有消息编号（决定 `confirm` 能不能改 `strict`）——真机验证。
2. 其他项目的情况（几个、在哪台机器、用什么语言、每天推几条）——决定要不要开局域网监听、各项目保底多少。默认值按"ashare-quant 保底 6、第一个其他项目保底 2、回复保留 2"。
3. 其他项目的前缀叫什么（登记时再定；前缀不能和默认项目自己的指令同名）。
4. `official` 档位是否值得切换（腾讯服务端有没有开始校验那几个请求头）——真机验证，默认 `legacy`。
5. 仓库的最终名字和位置（默认 `Lhanman/clawbot-hub`，私有）。
6. 长轮询读超时算不算"完成往返"（影响 `upstream_down` 的判断）、带旧的 `get_updates_buf` 再收是否重发同一批消息、launchd 的 `Umask` 对 `launchd.log` 是否生效——真机验证。
