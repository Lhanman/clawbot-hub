-- 0001_init：第一版的全部表（设计第 4 节）。
-- 迁移执行器已经在一个事务里运行本脚本，脚本里不要写 BEGIN / COMMIT / ROLLBACK；
-- 版本号（meta.schema_version）也由执行器在同一个事务里写。
-- 数据库里没有 bot_token（它只在 account.json 里）；发送的正文不落盘；
-- 对话凭证只存在 windows 和 inbox 两张表里。

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO meta (key, value) VALUES ('instance_id', lower(hex(randomblob(16))));

-- 一行；表按单账号设计，多号时再加列
CREATE TABLE account_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  login_state TEXT NOT NULL CHECK (login_state IN ('ok', 'expired', 'missing', 'broken')),
  login_since_ms INTEGER,               -- 进入当前 login_state 的时刻
  expired_episode INTEGER NOT NULL DEFAULT 0,
  creds_digest TEXT,                    -- 登录凭证里四项（令牌、接口地址、机器人号、用户号）的摘要
  creds_changed_ms INTEGER,             -- 服务最近一次发现摘要变了的时刻
  last_upstream_ok_ms INTEGER,          -- 最近一次与腾讯完成往返
  last_poll_ok_ms INTEGER, last_poll_error TEXT, last_poll_error_ms INTEGER,
  ignored_count INTEGER NOT NULL DEFAULT 0
);
INSERT INTO account_state (id, login_state) VALUES (1, 'missing');

CREATE TABLE projects (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
  prefix TEXT, prefix_key TEXT UNIQUE,  -- prefix_key 是 NFKC + 小写后的比较值；移除项目时两列置空
  key_hash TEXT NOT NULL UNIQUE, prev_key_hash TEXT, prev_key_until_ms INTEGER,
  guaranteed INTEGER NOT NULL CHECK (guaranteed >= 0),
  cap INTEGER NOT NULL CHECK (cap >= 0),
  is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
  label TEXT,                           -- 登记时落成具体值，空 = 没有标签
  enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
  push_only INTEGER NOT NULL DEFAULT 0 CHECK (push_only IN (0, 1)),
  internal INTEGER NOT NULL DEFAULT 0 CHECK (internal IN (0, 1)),  -- 1 = 服务内部名字 _hub、_import
  removed_ms INTEGER, created_ms INTEGER NOT NULL,
  last_seen_ms INTEGER, last_inbox_ms INTEGER,   -- 最后出现 / 最近一次取话或确认（在线判断）
  awaiting_until_ms INTEGER                      -- 等确认标记
);
CREATE UNIQUE INDEX one_default ON projects(is_default) WHERE is_default = 1;

-- 服务内部用的两个"项目"：_hub（服务自己的回复记在它名下）、_import（导入 ashare-quant 凭证时带过来的
-- 已用条数，类别是推送）。key_hash 填占位值，永远不会等于 64 位十六进制的 SHA-256 摘要，所以认不成它们
INSERT INTO projects (name, key_hash, guaranteed, cap, enabled, push_only, internal, created_ms)
VALUES ('_hub', 'internal:_hub', 0, 0, 0, 1, 1, 0),
       ('_import', 'internal:_import', 0, 0, 0, 1, 1, 0);

-- 每个使用者的新消息一行
CREATE TABLE windows (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_ms INTEGER NOT NULL,
  context_token TEXT,                   -- 窗口过期、被新窗口取代或作废时置空
  voided INTEGER NOT NULL DEFAULT 0 CHECK (voided IN (0, 1)),   -- 1 = 重新绑定账号时作废
  blocked_until_ms INTEGER, blocked_code TEXT                   -- 腾讯拒绝后的暂停
);
CREATE INDEX windows_started ON windows(started_ms);

-- 窗口里各项目已用条数（含预占）
CREATE TABLE usage (
  window_id INTEGER NOT NULL, project_id INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('push', 'reply')),
  n INTEGER NOT NULL DEFAULT 0 CHECK (n >= 0),
  PRIMARY KEY (window_id, project_id, kind)
);

-- 发送记录，没有正文
CREATE TABLE outbox (
  id TEXT PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
  kind TEXT NOT NULL CHECK (kind IN ('push', 'reply')), priority TEXT,
  window_id INTEGER REFERENCES windows(id) ON DELETE SET NULL,  -- 可为空：没有窗口时被拒绝的记录，或窗口已按留存期删掉
  dedupe_hash TEXT, parts INTEGER NOT NULL,
  parts_sent INTEGER NOT NULL DEFAULT 0, parts_unconfirmed INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN
    ('sending', 'sent', 'unconfirmed', 'partial', 'rejected', 'failed', 'skipped', 'interrupted')),
  reason_code TEXT, reason TEXT,        -- reason：变量已代入的中文原因，不含正文
  chars INTEGER,
  no_message_id INTEGER NOT NULL DEFAULT 0,   -- 这条消息里腾讯没给消息编号的请求数，只用于 status 展示
  created_ms INTEGER NOT NULL, finished_ms INTEGER
);
-- 去重键只被"发送中 / 已发 / 未确认 / 部分 / 中断"的记录占着；rejected / failed / skipped 一变状态就自动释放
CREATE UNIQUE INDEX outbox_dedupe ON outbox(project_id, dedupe_hash)
  WHERE dedupe_hash IS NOT NULL
    AND status IN ('sending', 'sent', 'unconfirmed', 'partial', 'interrupted');
CREATE INDEX outbox_created ON outbox(created_ms);
CREATE INDEX outbox_project ON outbox(project_id, created_ms);
CREATE INDEX outbox_window ON outbox(window_id);

CREATE TABLE inbox (
  id TEXT PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
  upstream_key TEXT, window_id INTEGER,
  text TEXT, has_text INTEGER NOT NULL CHECK (has_text IN (0, 1)),
  at_ms INTEGER NOT NULL, received_ms INTEGER NOT NULL,   -- 取话按 at_ms、入库先后（rowid）排
  via TEXT NOT NULL CHECK (via IN ('prefix', 'confirm', 'default')),
  state TEXT NOT NULL CHECK (state IN ('queued', 'leased', 'acked', 'expired', 'dropped')),
  lease_until_ms INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
  handle TEXT UNIQUE, context_token TEXT, handle_expires_ms INTEGER,
  replies INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX inbox_fetch ON inbox(project_id, state, at_ms);

CREATE TABLE inbound_seen (upstream_key TEXT PRIMARY KEY, seen_ms INTEGER NOT NULL);
CREATE INDEX inbound_seen_ms ON inbound_seen(seen_ms);

-- 服务回复的限频：offline:<项目> / disabled:<项目> / full:<项目> / nodefault
CREATE TABLE notice_state (key TEXT PRIMARY KEY, last_ms INTEGER NOT NULL);

CREATE TABLE alert_state (
  key TEXT PRIMARY KEY, episode TEXT, last_sent_ms INTEGER,
  pending_text TEXT, pending_since_ms INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
  counter INTEGER NOT NULL DEFAULT 0    -- send_unconfirmed 用：连续没有确认送达的 sendmessage 请求数，重启保留
);

CREATE TABLE events (
  id INTEGER PRIMARY KEY, ts_ms INTEGER NOT NULL, level TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN
    ('login', 'alert', 'send', 'hold', 'project', 'queue', 'backup', 'lifecycle')),
  project TEXT, detail TEXT
);
CREATE INDEX events_ts ON events(ts_ms);
