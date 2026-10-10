# Tasks

规矩：**每一项先写测试（看它失败），再写实现（看它通过）**；单元测试不联网；每个阶段做完跑 `uv run ruff format --check && uv run ruff check && uv run pytest -q`，全绿才算这个阶段完成，并单独向使用者汇报、验收。使用者点头规格和测试清单之前，下面一项都不开始。

以 `[云]` 标的在云里的线程做，以 `[Mac]` 标的要在家里的 Mac mini 上做（由使用者授权，经协调者转给 Mac 上的线程）。

## 0. 仓库骨架

- [ ] 0.1 `[云]` `pyproject.toml`（Python ≥ 3.12，`uv`；依赖 fastapi、uvicorn、requests、typer、pyyaml、segno；开发 pytest、ruff、httpx）、ruff 配置、`pytest` 配置（`live` 标记默认跳过）、`uv.lock`
- [ ] 0.2 `[云]` 先写测试：`tests/test_socket_guard.py`——连非回环地址抛"测试里不允许联网"，回环放行；再写 `tests/conftest.py` 里的守卫
- [ ] 0.3 `[云]` `clock.py`（`Clock`、`FakeClock`：`now_ms()`、`sleep()`、`advance()`）及其测试
- [ ] 0.4 `[云]` `tests/support/fake_ilink.py`：本机回环上的假腾讯服务（`getupdates` 长轮询、`sendmessage`、`get_bot_qrcode`、`get_qrcode_status`、`notifystart`、`notifystop`；可编排：-14、5xx、429、读超时、无消息编号、延迟、扫码各状态；记录收到的每个请求的头和体）；先写它自己的测试（`tests/support/test_fake_ilink.py`）
- [ ] 0.5 `[云]` 假 Bark 服务（同样放 `tests/support/`）和进程内假上游（实现 `Upstream` 协议）

## A. 地基：纯函数、配置、数据库、腾讯接口、登录、项目、额度

- [ ] A1 `textutil` `[云]`
  - 先写测试：`tests/textutil/test_split.py`（把 ashare-quant `split_message`、`headline`、`utf8_size` 的测试搬过来，行为逐条对上）、`test_match.py`（NFKC、大小写、全角空格、分隔符、前缀后面直接连字不算）、`test_mask.py`（已知秘密和 `chub_…` 换成 `***`）
  - 再实现 `textutil.py`
- [ ] A2 `config` `[云]`
  - 先写测试：`tests/config/test_config.py`——没有配置文件用全部默认值；错误一次列全（负数、端口越界、未知档位、保底超限）；`quota.reply_reserve ≥ window_limit` 拒绝
  - 再实现 `config.py`
- [ ] A3 `db` 与迁移 `[云]`
  - 先写测试：`tests/db/test_migrations.py`（建库、版本表、只往前、比程序新则拒绝、迁移中途出错回滚、升级前自动备份）、`tests/db/test_permissions.py`（新建文件 0600、目录 0700、放宽后启动时修回）、`tests/db/test_txn.py`（`BEGIN IMMEDIATE`、两个线程并发写不丢）
  - 再实现 `db.py`、`migrations/0001_init.sql`
- [ ] A4 腾讯接口适配层 `[云]`
  - 先写测试：`tests/upstream/test_http_profiles.py`（`legacy` 的请求头和体与 ashare-quant `tests/notify` 里记录的逐项相同；`official` 的新增项；`notifystart` / `notifystop`；失败不挡启动）、`test_http_outcomes.py`（`ret` / `errcode` 判定、-14、429 / 5xx → `Transient`、读超时 → `NoResponse`、消息编号取法、报错里的 `bot_token` 被换成 `***`）
  - 再实现 `upstream/base.py`、`upstream/http.py`、`upstream/profiles.py`
- [ ] A5 凭证文件与登录 `[云]`
  - 先写测试：`tests/account/test_account.py`（原子写、0600、临时文件不留宽权限、坏文件报错不带内容、换凭证 5 秒内重新读取）、`tests/login/test_qr_login.py`（一次扫成、过期换码、连续五张、验证码、重定向、已绑定、Ctrl-C 清理 `login.pending` 和二维码图、成功后清 `updates_buf` 和窗口）、`tests/login/test_import.py`（导入 ashare-quant 的 `clawbot.json`、窗口和已用条数接得上、已有凭证拒绝、服务在跑拒绝、缺字段、`--replace` 备份、输出不含凭证）、`tests/login/test_expiry.py`（-14 置失效且重启保持、探测间隔、扫码期间的 -14 不报警、`login_expired` / `login_missing` 的发送结果）
  - 再实现 `account.py`、`login.py`
- [ ] A6 项目 `[云]`
  - 先写测试：`tests/projects/test_projects.py`（名字、前缀、保底上限、默认项目唯一、标签、只推送、保底合计、停用 / 启用 / 移除规则、前缀与确认词冲突）、`tests/projects/test_keys.py`（格式、库里只有摘要、恒定时间比较、换钥与宽限、停用后 403、反复猜密钥 429）
  - 再实现 `projects.py`
- [ ] A7 额度 `[云]`
  - 先写测试：`tests/quota/test_decide.py`（`hub-quota` 里每个数字场景一行：保底加保留、共享池、上限、紧急、可延后、回复超过保留、停用释放、`left`）、`tests/quota/test_ledger.py`（开窗口 / 清零 / 旧消息不倒回、预占与对账、拆三条第二条失败退回两条、发送途中换窗口、两个线程同时抢最后一条、重启不丢账、发送中被杀标"中断"并保留预占）、`tests/quota/test_hold.py`（腾讯拒绝后暂停、过期后试探、新窗口解除、回复不受影响）
  - 再实现 `quota.py`
- 阶段 A 验收：`uv run pytest -q tests/textutil tests/config tests/db tests/upstream tests/account tests/login tests/projects tests/quota` 全过；`uv run hubctl login`、`hubctl project add`、`hubctl status` 对着假上游能走通（命令行的完整测试在阶段 D）

## B. 发消息

- [ ] B1 `sender` `[云]`
  - 先写测试：`tests/sender/test_send_states.py`（`sent` / `unconfirmed`（严格档）/ `partial` / `rejected` / `failed` / `skipped` 各一组；观察档下无编号算 `sent` 并记标记；重试用同一个 `client_id`；读超时不重试；腾讯拒绝不重试；发送中 -14）、`test_labels_split.py`（标签加在拆条之前、拆条与 ashare-quant 一致、拆出太多条 `too_long`）、`test_reasons.py`（`hub-send` 里那张原因表的每一行，前四条文字与 ashare-quant 逐字相同）、`test_dedupe.py`（命中返回原结果、被拒后键释放、两个项目互不影响、同时两次只发一条）、`test_reply.py`（用被回复那条消息的凭证、句柄校验、过期、次数上限、等确认标记只在发送成功后记）、`test_no_body.py`（数据库、日志里搜不到独特字符串）、`test_busy.py`（等发送锁超时 → `failed` / `busy` 且不占条数）
  - 再实现 `sender.py`
- [ ] B2 接口 `[云]`
  - 先写测试：`tests/api/test_auth_limits.py`（401 / 403 / 429 / 413、反复猜密钥、限速 30 次 / 分钟、项目之间互相看不到、局域网开关只认私有网段、伪造转发头无效）、`tests/api/test_messages.py`（`/v1/messages`、`/v1/replies`、`/v1/status`、`/v1/health`：字段、未知字段忽略、错误值 400、空消息 400）
  - 再实现 `api.py`、`app.py` 里发送相关的部分
- 阶段 B 验收：`uv run pytest -q tests/sender tests/api`；用 `curl` 对着起好的服务（假上游）发一条消息，拿到 `sent`

## C. 收话和分发

- [ ] C1 `router` `[云]`
  - 先写测试：`tests/router/test_route.py`（`hub-inbox` 里"一句话交给谁"的每个场景：不带前缀、带前缀的各种分隔符、前缀后直连字、不认识的前缀、只发前缀、图片、没有默认项目、服务指令、确认词的各种情形）
  - 再实现 `router.py`
- [ ] C2 `inbox` `[云]`
  - 先写测试：`tests/inbox/test_queue.py`（取话立刻返回 / 等待 / 超时、租约与重发、`attempt`、超过次数作废、确认幂等且清正文、队列溢出、保留期过期、新请求顶掉旧请求、句柄不含凭证、句柄过期清凭证、清理任务）
  - 再实现 `inbox.py`
- [ ] C3 `poller` `[云]`
  - 先写测试：`tests/inbox/test_poller.py`（收一句话、去重、事务提交后才写回 `updates_buf` 且两步之间被杀不重复、非本人丢弃只计数、错误退避 5 / 10 / 20 …、-14 进入探测、服务自己的回复（服务 项目 / 状态 / 未知指令）、没在线提醒与限频、等确认的标记与几个项目同时在等、`awaiting` 的清除时机、没有默认项目）
  - 再实现 `poller.py`
- [ ] C4 接口 `[云]`
  - 先写测试：`tests/api/test_inbox.py`（`/v1/inbox`、`/v1/inbox/ack`：字段、`wait` / `max` 边界、只推送项目 403、只看得到自己的）
  - 再实现 `api.py` 里取话相关的部分
- 阶段 C 验收：`uv run pytest -q tests/router tests/inbox tests/api`；两个项目对着假上游走一遍"发话 → 路由 → 取话 → 回复 → 等确认 → 确认"

## D. 告警、命令行、客户端、部署

- [ ] D1 告警与定时任务 `[云]`
  - 先写测试：`tests/alerts/test_rules.py`（每种告警的触发、去重单位、重启不重复、恢复只在报过失效之后、窗口提醒的 4 个条件、崩溃循环不刷屏）、`test_bark.py`（请求格式、设备码两种写法、重试 3 次、待发队列 5 分钟重试 1 小时、没配设备码的行为、不含秘密和正文）、`tests/scheduler/test_scheduler.py`（清理、心跳、备份按时间点触发、假时钟推进）
  - 再实现 `alerts.py`、`scheduler.py`
- [ ] D2 命令行 `[云]`
  - 先写测试：`tests/cli/test_status_doctor.py`（服务停着也能看、`--json`、没有凭证和密钥、`doctor` 各项含 `--fix` 和 `--online` 不碰腾讯）、`test_project_cli.py`（`project` 各子命令、破坏性命令的确认和 `--yes`）、`test_send_cli.py`（退出码 0 / 3 / 4 / 5 / 6 / 7 / 10 / 11 / 12、不收命令行密钥、从标准输入读）、`test_backup.py`（备份能打开、服务运行时备份、保留份数）、`test_events.py`
  - 再实现 `cli.py`、`backup.py`、`doctor.py`
- [ ] D3 单文件客户端 `[云]`
  - 先写测试：`tests/client/test_client.py`（接口、数据类、`ok` / `needs_fallback`、异常分类和 `possibly_sent`、不偷偷重发的规则、`run_inbox` 的确认 / 退避 / 停用）、`test_stdlib_only.py`（只 import 标准库、语法兼容 3.9）
  - 再实现 `client/clawbot_hub_client.py`
- [ ] D4 合约测试与接口文档 `[云]`
  - 先写测试：`tests/contract/test_end_to_end.py`（两个项目、真 HTTP 服务、假上游、单文件客户端：发每种状态、回复、等确认路由、停用和换钥、限速）、`tests/contract/test_docs_match.py`（OpenAPI 导出与 `docs/api/openapi.json` 一致、`docs/api/v1.md` 的示例能跑、每个接口都有合约测试）
  - 再写 `docs/api/v1.md`、`docs/clients/接入指南.md`，导出 `docs/api/openapi.json`
- [ ] D5 进程与部署 `[云]`
  - 先写测试：`tests/process/test_lifecycle.py`（第二个实例退出码 2 且不碰假上游、`kill -9` 后能启动、`SIGTERM` 发送完再退且取话请求 1 秒内返回、端口被占退出码 1、数据库比程序新退出码 3、启动顺序）、`tests/deploy/test_logging.py`（脱敏、请求日志不含查询串和密钥）、`test_plist.py`（launchd 模板合法）、`test_timezone.py`
  - 再实现 `app.py` 的生命周期、`logging_setup.py`，写 `deploy/launchd/com.clawbot-hub.serve.plist.example`
- [ ] D6 运维文档 `[云]`：`docs/ops/安装说明.md`、`迁移手册.md`、`回退手册.md`、`真机验证清单.md`（内容见 `hub-deploy`）
- 阶段 D 验收：`uv run ruff format --check && uv run ruff check && uv run pytest -q` 全绿（整套 90 秒内）；`uv run hubctl --help` 列出全部命令

## E. 验收和交接

- [ ] E1 `[云]` 把 `README.md` 的"状态"改成可用，补"预定的用法"里的实际输出；`CLAUDE.md` 的"常用命令"改成真实命令
- [ ] E2 `[云]` 向使用者汇报：做了什么、测试结果、默认值、没做的、下一步要在 Mac 上做的事
- [ ] E3 `[Mac]` 使用者授权后：装环境（`uv sync`）、`hubctl doctor`、装 launchd，**先不连真实的 ClawBot**，用 `tests/support/fake_ilink.py` 的独立运行模式演练一遍：登记 ashare-quant、发一条、收一条、断一次网、重启
- [ ] E4 `[Mac]` 使用者授权后：按 `docs/ops/真机验证清单.md` 在真账号上测（先问清楚会占用额度、会不会影响现有微信问答），结果写回文档，必要时把配置默认值（`upstream.confirm`、`upstream.profile`、窗口长度和条数）改成实测值
- [ ] E5 `[云]` 用 `openspec archive` 把本变更归档进 `openspec/specs/`

## 不在本变更里（另立变更，等使用者点头）

- ashare-quant 加"走服务"的模式（`wechat.mode: direct | hub`，默认 `direct`）：改 `notify/clawbot.py`（换成薄客户端）、`notify/channels.py`、`cli.py` 的 `aq wechat …`、`chat/bot.py` 和 `chat/wiring.py`、`web/system_view.py`、`overview_data.py`、`push.py`、launchd 模板和安装说明。动手前先和 Mac 上还没推送的本机提交对齐。
- 周末的切换（停旧任务、导入凭证、启动服务、切模式、观察一个交易日），按 `docs/ops/迁移手册.md`，使用者本人在场。
- 稳定一段时间后，删掉 ashare-quant 里直连腾讯的代码。
- 其他项目的接入：各自一个小改动，按 `docs/clients/接入指南.md`。

## 测试清单（汇总）

| 模块 | 测试文件 | 证明了什么 |
|---|---|---|
| 守卫与假件 | `tests/test_socket_guard.py`、`tests/support/test_fake_ilink.py` | 测试不能联网；假腾讯能按剧本模拟各种情形 |
| 文本 | `tests/textutil/test_split.py`、`test_match.py`、`test_mask.py` | 拆条与 ashare-quant 一致；前缀匹配规则；秘密被遮掉 |
| 配置 | `tests/config/test_config.py` | 默认值齐全；错误一次列全；保底与总条数的约束 |
| 数据库 | `tests/db/test_migrations.py`、`test_permissions.py`、`test_txn.py` | 只往前升级、失败回滚；权限；并发写不丢 |
| 腾讯接口 | `tests/upstream/test_http_profiles.py`、`test_http_outcomes.py` | 两档请求形式；各种返回如何判定；报错不泄露凭证 |
| 凭证与登录 | `tests/account/test_account.py`、`tests/login/test_qr_login.py`、`test_import.py`、`test_expiry.py` | 凭证文件安全；扫码各状态；导入时额度接得上；-14 的处理 |
| 项目 | `tests/projects/test_projects.py`、`test_keys.py` | 登记规则；密钥只存摘要；换钥；防猜 |
| 额度 | `tests/quota/test_decide.py`、`test_ledger.py`、`test_hold.py` | 保底、上限、共享池、紧急、可延后；预占与对账不错账；腾讯拒绝后暂停 |
| 发送 | `tests/sender/test_send_states.py`、`test_labels_split.py`、`test_reasons.py`、`test_dedupe.py`、`test_reply.py`、`test_no_body.py`、`test_busy.py` | 每种结果状态；不偷偷重发；原因文字；去重；回复；正文不落盘 |
| 路由 | `tests/router/test_route.py` | 一句话交给谁的全部规则 |
| 收话 | `tests/inbox/test_queue.py`、`test_poller.py` | 租约、确认、至少一次；收消息循环、退避、去重、等确认不串线 |
| 告警 | `tests/alerts/test_rules.py`、`test_bark.py`、`tests/scheduler/test_scheduler.py` | 每种告警何时发、不重复、重启不重复；Bark 请求与重试 |
| 接口 | `tests/api/test_auth_limits.py`、`test_messages.py`、`test_inbox.py` | 认证、限速、隔离、局域网规则；各接口字段 |
| 命令行 | `tests/cli/…` | 各命令行为与退出码；备份能用；`doctor` 不碰腾讯 |
| 客户端 | `tests/client/test_client.py`、`test_stdlib_only.py` | 异常分类、不偷偷重发、取话循环；只依赖标准库 |
| 合约 | `tests/contract/test_end_to_end.py`、`test_docs_match.py` | 项目这一侧的全部约定；文档与接口一致 |
| 进程与部署 | `tests/process/test_lifecycle.py`、`tests/deploy/…` | 单实例、崩溃恢复、优雅退出；日志脱敏；launchd 模板；时区 |
| 真机（默认不跑） | `tests/live/test_real_ilink.py` | 腾讯一侧的规则（有无消息编号等），Mac 上使用者授权后手动运行 |
