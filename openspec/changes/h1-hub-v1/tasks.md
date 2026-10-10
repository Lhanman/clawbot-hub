# Tasks

规矩：**每一项先写测试（看它失败），再写实现（看它通过）**；单元测试不联网；每个阶段做完跑 `uv run ruff format --check && uv run ruff check && uv run pytest -q`，全绿才算这个阶段完成，并单独向使用者汇报、验收。**阶段 A 到 C 的验收一律是 pytest 驱动的**（不依赖后面阶段才写的命令），`curl` 之类的手工演示放到阶段 D。使用者点头规格和测试清单之前，下面一项都不开始。

以 `[云]` 标的在云里的线程做，以 `[Mac]` 标的要在家里的 Mac mini 上做（由使用者授权，经协调者转给 Mac 上的线程）。

## 0. 仓库骨架

- [ ] 0.1 `[云]` `pyproject.toml`（Python ≥ 3.12，`uv`；依赖 fastapi、uvicorn、requests、typer、pyyaml、segno；开发 pytest、ruff、httpx）、ruff 配置、`pytest` 配置（`live` 标记默认跳过）、`uv.lock`
- [ ] 0.2 `[云]` 先写测试：`tests/test_socket_guard.py`——连非回环地址抛"测试里不允许联网"，回环放行；再写 `tests/conftest.py` 里的守卫
- [ ] 0.3 `[云]` 先写测试 `tests/test_clock.py`（`FakeClock` 的 `now_ms()`、`sleep()` 不真等、`advance()`；真时钟的接口形状一致），再写 `clock.py`
- [ ] 0.4 `[云]` `tests/support/fake_ilink.py`：本机回环上的假腾讯服务（`getupdates` 长轮询、`sendmessage`、`get_bot_qrcode`（GET 和 POST）、`get_qrcode_status`、`notifystart`、`notifystop`；可编排：-14、5xx、429、读超时、连接被重置、空闲连接断开、无消息编号、延迟、扫码各状态；记录收到的每个请求的头和体）；**能独立运行**（`python -m tests.support.fake_ilink --port N`，给阶段 D 的演练和 E3 用）；先写它自己的测试（`tests/support/test_fake_ilink.py`，含独立运行模式）
- [ ] 0.5 `[云]` 假 Bark 服务（`tests/support/fake_bark.py`），先写测试（`test_fake_bark.py`：假件会按剧本给出每一种结果，免得假件写错而上层测试全绿）。进程内假上游（`fake_upstream.py`，要实现 `Upstream` 协议）等 A4 的 `upstream/base.py` 有了以后在 A4 里做
- [ ] 0.6 `[云]` `tests/live/test_real_ilink.py`：真机验证清单里每一项的测试骨架（`live` 标记，默认跳过；骨架里只有步骤和断言占位，E4 在 Mac 上授权后才填实测值）；测试里有一条检查"默认运行时这些测试被跳过"

## A. 地基：纯函数、配置、数据库、腾讯接口、登录、项目、额度、最小命令行

- [ ] A1 `textutil` `[云]`
  - 先写测试：`tests/textutil/test_split.py`（把 ashare-quant 的 `split_message`、`headline` 的测试搬过来，行为逐条对上；`utf8_size` 不搬，hub 里没有按字节拆的地方；新增：标题最多 30 个字、超出加"…"，正文预算 `max(part_chars − len(开头), part_chars // 2)`；整条不超过 `part_chars` 就一条发完、不扣开头的位置（标题 10 字、全文 1990 字 → 一条且没有开头，2001 字 → 两条）；5000 字一行也满足"每条含开头不超过 `part_chars`、去掉开头后拼回等于原文"）、`test_match.py`（NFKC、大小写、全角空格、分隔符、连续多个分隔符、前缀后面直接连字不算、按位置从原文切、`hub.label` 作指令词）、`test_mask.py`（已知秘密和 `chub_…` 换成 `***`）
  - 再实现 `textutil.py`
- [ ] A2 `config` `[云]`
  - 先写测试：`tests/config/test_config.py`——没有配置文件用全部默认值；错误一次列全（负数、端口越界、未知档位、`routing.confirm_words` 为空、未知时区）；`quota.reply_reserve ≥ window_limit` 拒绝；`send.part_chars` 小于 200 拒绝（200 通过）；`server.shutdown_seconds` 小于 `send.timeout_seconds` 只警告不拒绝；`server.lan` 与 `server.host` 的关系（`lan: false` 要回环地址，`lan: true` 要默认 host）；`routing.confirm_words` 默认只有对、是、确认
  - 再实现 `config.py`
- [ ] A3 `db` 与迁移 `[云]`
  - 先写测试：`tests/db/test_migrations.py`（建库、版本表、只往前、比程序新则拒绝、迁移中途出错回滚、升级前自动备份 `pre-upgrade-…`、预置的 `_hub`、`_import` 两行）、`tests/db/test_backup_core.py`（`backup.py` 的核心：做出一份能打开的一致性副本、`pre-upgrade-` / `auto-` / `manual-` 三种命名、只清理 `auto-` 的旧份、服务运行时也能备份）、`tests/db/test_permissions.py`（新建文件 0600、目录 0700、放宽后启动时修回）、`tests/db/test_txn.py`（`BEGIN IMMEDIATE`、两个线程并发写不丢）、`tests/db/test_pragmas.py`（每个连接 `secure_delete`、外键、`busy_timeout`；被清掉的正文做检查点后搜不到；`outbox_dedupe` 的状态集合含 `interrupted`；`outbox.window_id` 可为空且 `ON DELETE SET NULL`）
  - 再实现 `db.py`、`backup.py`（只有核心函数；`hubctl backup` 命令、`--to` 和云盘路径警告留到 D3，按时间点触发留到 D2）、`migrations/0001_init.sql`
- [ ] A4 腾讯接口适配层 `[云]`
  - 先写测试：`tests/upstream/test_http_profiles.py`（`legacy` 与 `hub-login` 里写出的清单逐项相同：4 个请求头、`msg` 恰好 7 个字段、`base_info` 只有 `channel_version "1.0.2"`、`getupdates` 请求体两个键、取码用 GET；`official` 的新增项与 `POST` 取码；`notifystart` / `notifystop`；失败不挡启动）、`test_http_outcomes.py`（`hub-send` 分类表的每一行：DNS / 连接被拒 / 连接超时 / TLS → `Transient`；完整的 429 / 5xx → `Transient` 且不看响应体（502 里带 -14 也重试）；读超时 / 被重置 / 读到一半断开 → `NoResponse`；`ret` 和 `errcode` 的取法（`{"ret":5,"errcode":-14}` 是拒绝）；-14 → `SessionExpired`；消息编号取法；空闲连接被服务端关掉后第一次 `sendmessage` 不被误判（每次新连接）；报错里的 `bot_token` 被换成 `***`）
  - 再实现 `upstream/base.py`、`upstream/http.py`、`upstream/profiles.py`
  - 然后做进程内假上游 `tests/support/fake_upstream.py`（实现 `Upstream` 协议，按剧本给出每一种结果），先写它自己的测试 `tests/support/test_fake_upstream.py`；A5 以后的组件测试用它
- [ ] A5 凭证文件与登录 `[云]`
  - 先写测试：`tests/account/test_account.py`（原子写、0600、临时文件不留宽权限、坏文件报错不带内容、凭证摘要只看四项、写回 `updates_buf` 时核对 `bot_token`、读出后被新登录抢先时新凭证保留）、`tests/account/test_watcher.py`（5 秒内换上新凭证并置位"唤醒收消息线程"的事件；没绑定时每 5 秒看文件；服务停着时 `hubctl login`、启动时比对摘要（函数层，启动流程留到 D6）；-14 先重读文件；`creds_changed_ms` 60 秒内的 -14 不报警；摘要变了就补做数据库事务（窗口作废、回复句柄的凭证置空），做两遍结果不变）、`tests/login/test_qr_login.py`（一次扫成、过期换码、一张码等满 180 秒换码、连续五张、验证码、`verify_code_blocked`、`scaned_but_redirect` 之后的查询发往 `redirect_host`、`binded_redirect`、Ctrl-C 清理 `login.pending` 和二维码图、成功后先写 `account.json`（`updates_buf` 为空）、再在一个事务里作废窗口（`voided`）并清所有 `inbox.context_token`、两步之间被杀后由比对补做、数据库不存在时只写文件、`login.pending` 里的进程号和 20 分钟规则）、`tests/login/test_import.py`（导入 ashare-quant 的 `clawbot.json`、窗口和已用条数接得上且记在 `_import` 名下（推送）、已有凭证拒绝、服务在跑拒绝（先看 `run.json`，再非阻塞拿 `hub.lock` 并一直持有，导入期间服务起不来（退出码 2）、导入后启动不作废刚导入的窗口）、先写数据库后写文件且可重复导入（做到一半被杀后再导入，已用条数不翻倍）、缺字段、`--replace` 备份、`--probe` 不处理消息也不保存进度、输出不含凭证）、`tests/login/test_expiry.py`（登录状态机，函数层：-14 置失效且重启保持、恢复条件只有"摘要变了"或"HTTP 200 且 `ret`、`errcode` 为空或 0"（502、连不上、`ret: 500` 不算恢复）、扫码期间的 -14 不报警；探测间隔归 C3 的 `test_poller.py`，各登录状态下的发送结果归 B1）
  - 再实现 `account.py`、`login.py`、`watcher.py`
- [ ] A6 项目 `[云]`
  - 先写测试：`tests/projects/test_projects.py`（名字规则、前缀规则（能收话的必须有、只推送的不能有、与确认词和 `hub.label` 冲突、大小写重复）、保底上限（上限 ≤ 窗口 − 回复保留）、默认项目（第一个能收话的自动成为、`--default --yes` 换、只推送的不能当）、标签规则与 `--no-label`（含只推送项目的名字超过 16 个字符时取前 16 个）、换默认不改标签、保底合计、停用 / 启用 / 移除规则（移除默认项目的规矩；"停用后暂停的话启用后继续取"和"移除时作废没取走的话"要用到取话，归 C2）、内部名字 `_hub`、`_import` 不露面、配置与已有前缀冲突的检查函数（返回冲突列表；启动时打印警告归 D6））、`tests/projects/test_keys.py`（格式、库里只有摘要、恒定时间比较、换钥与宽限、宽限期内再换一次、已移除的密钥 401、停用后 403、密钥只在输出里出现一次）
  - 再实现 `projects.py`
- [ ] A7 额度 `[云]`
  - 先写测试：`tests/quota/test_decide.py`（`hub-quota` 里每个数字场景一行：保底加保留、共享池、上限、紧急、可延后、回复超过保留、停用释放、`left`（登录非 ok、暂停期、窗口关着时为 0；`hub-quota` 状态示例里 `left` = 3 的算式）、作废的窗口按"没收到过消息"处理）、`tests/quota/test_ledger.py`（开窗口 / 清零 / 旧消息不倒回、预占与逐条对账、拆三条第二条失败退回两条、发送途中换窗口（换在第 2 条在飞时新窗口已用 2、换在第 3 条在飞时已用 1、换窗口后没发出去的不退）、回复记进最新窗口、两个线程同时抢最后一条、"回复保留已用完 / 还在"两个对照场景、重启不丢账、发送中被杀标"中断"并保留预占）、`tests/quota/test_hold.py`（腾讯拒绝后暂停、过期后试探并发时只放行一条、试探连不上撤回临时值、新窗口解除、回复不受影响）
  - 再实现 `quota.py`
- [ ] A8 最小命令行 `[云]`：`cli.py` 的入口和 `login`、`project add / list`、`status`（不含 `serve`），共用后面阶段会扩充的公共约定（`--home`、`--config`、退出码、`--yes` 规矩）；先写测试 `tests/cli/test_minimal_cli.py`（`CliRunner`：数据目录自动创建、`project add` 打印一次密钥、`login` 对着假上游走通、`status` 服务停着也能看、不是终端又没带 `--yes` 退出 2），再实现
- 阶段 A 验收（pytest 驱动）：`uv run pytest -q tests/textutil tests/config tests/db tests/upstream tests/account tests/login tests/projects tests/quota tests/cli/test_minimal_cli.py tests/support tests/test_clock.py tests/test_socket_guard.py` 全过；`uv run ruff format --check && uv run ruff check` 干净

## B. 发消息

- [ ] B1 `sender` `[云]`
  - 先写测试：`tests/sender/test_send_states.py`（`sent` / `unconfirmed`（严格档）/ `partial` / `rejected` / `failed` / `skipped` 各一组；观察档下无编号算 `sent` 并记标记；重试用同一个 `client_id`、间隔 1 秒 2 秒；读超时不重试；腾讯拒绝不重试；发送中 -14；登录 `login_missing` / `login_expired` / `login_broken` 各一行的发送结果；重新绑定后窗口作废，推送得 `never_contacted`）、`test_check_order.py`（验证 → 去重键 → `too_long` → 登录 → 窗口 → 暂停 → 额度 的先后；同时满足几个拒绝条件时取最前面的；`too_long` 的 M 对推送是 L−R、对回复是 L）、`test_budget.py`（总时限：等锁用掉预算、第 2 条来不及开始、读超时只剩一部分、`send_timeout`；默认下单次请求最多等 15 秒；剩不到 1 秒不再开始（下一条的第一次尝试和重试都算），重试被拦下沿用上一次失败的原因；几段连续不被别的项目插进来）、`test_labels_split.py`（标签与正文同一行、加在拆条之前、拆条与 ashare-quant 一致、拆出太多条 `too_long`、`dedupe_key` 长度、原文不去空白）、`test_reasons.py`（`hub-send` 里那张 20 行原因表的每一行，前四条文字与 ashare-quant 逐字相同，时间按显示时区）、`test_dedupe.py`（命中返回原结果、被拒后键释放、两个项目互不影响、同时两次只发一条且第二个等第一个、`interrupted` 占键并按 `unconfirmed` / `interrupted` 重放、`duplicate_in_flight`（第一次的发送线程超出自己的预算时才出现）、`dedupe_hours` 到期释放）、`test_reply_core.py`（回复的 sender 层，直接给对话凭证，不经句柄：用给定的凭证发、整条消息用同一个凭证、记在"回复"一类、回复的 `too_long` 上限是 L、暂停期不拦回复；句柄、有效期、次数上限、等确认标记归 C4）、`test_window_change.py`（发送途中换窗口的逐条记账与回复记最新窗口）、`test_internal_error.py`（发送途中抛出意外：500 `internal`，已发的保留、没发的退回，重启后账是对的；碰腾讯之前出错：条数退回、键释放，同键再发当成新的；碰过腾讯之后出错：键占着，同键再发得 `unconfirmed` / `interrupted`）、`test_no_body_send.py`（发出去的正文：数据库、日志、事件里搜不到独特字符串；取走的话、过期和作废的话、服务自己处理的话的正文归 C5）、`test_busy.py`（等发送锁超时 → `failed` / `busy` 且不占条数）、`test_on_closed.py`（`skip` 不触发"消息没发出去"的告警）
  - 再实现 `sender.py`
- [ ] B2 接口与最小装配 `[云]`
  - 先写测试：`tests/api/test_auth_limits.py`（401 / 403 / 429 / 413；反复猜密钥只对局域网来源（注入来源地址）；本机一个项目错 20 次不挡别的项目；被 429 拒绝的不再计入失败；`Retry-After`；限速 30 次 / 分钟且被限速的不占额度；检查先后：认证、限速、大小；项目之间互相看不到；局域网开关只认私有网段；伪造转发头无效）、`tests/api/test_errors.py`（所有非 200 的错误格式、`message` 不含请求内容、不存在的路径 404（路径末尾多一个斜杠也是 404，不重定向）、用错方法 405 且带 `Allow`、框架自带的 422 一律成 400 `bad_request`、`/docs` `/redoc` `/openapi.json` 404、局域网规则的 403 正文为空；`wait` / `max` 越界是截断不是 400 的部分归 C4）、`tests/api/test_messages.py`（`/v1/messages`、`/v1/status`（含 `service.send_timeout_seconds`、`inbox_online_seconds`、`login.state` 四个取值）、`/v1/health`：字段、未知字段忽略、错误值 400、空消息 400、非 JSON 400、`on_closed` 值不认识 400）
  - 再实现 `api.py`，并在 `app.py` 里写最小装配 `create_app(config, upstream, clock)`（`FastAPI(docs_url=None, redoc_url=None, openapi_url=None)`；生命周期和 `hubctl serve` 留到阶段 D）
- 阶段 B 验收（pytest 驱动）：`uv run pytest -q tests/sender tests/api` 全过；`tests/api/test_e2e_send.py`：真 uvicorn（随机端口）加进程内假上游，用 HTTP 发一条消息拿到 `sent`、再发一条超限的拿到 `rejected`

## C. 收话和分发

- [ ] C1 `router` `[云]`
  - 先写测试：`tests/router/test_route.py`（`hub-inbox`"一句话交给谁"的每个场景：不带前缀、带前缀的各种分隔符含连续多个、前缀后直连字、不认识的前缀、只发前缀、图片、没有默认项目、默认项目已停用、前缀项目已停用、服务指令词取 `hub.label`、"服务器状态"不是指令、确认词的各种情形（默认三个词）、没有前缀的话也去首尾空白）
  - 再实现 `router.py`
- [ ] C2 `inbox` `[云]`
  - 先写测试：`tests/inbox/test_queue.py`（取话立刻返回 / 等待 / 超时、按 `at_ms` 再按入库先后排、租约与重发且 `reply_handle` 不变、`attempt`、`max_attempts` 由清理任务作废并清正文和凭证、租出去的话也受保留期约束、`wait` / `max` 边界（截断、400、`max=0` 当心跳）、确认幂等且清正文、`ack` 的 `unknown`、超过 50 个 400、租期已过才确认也算 `acked`、队列溢出丢最旧的没租出去的并记事件、队列里全是租出去的话时新话不入队并产生"队列满了"提示、`wait` 超过 `inbox.max_wait` 截断而负数 400、新请求顶掉旧请求、只推送的项目 403、停用时正等着的取话以 403 结束、停用的话暂停后启用继续取、移除项目时作废没取走的话、清理任务）、`test_handles.py`（句柄格式 `rh_` + 22 字符且不含凭证、有效期 `min(收到后 30 分钟, at_ms 之后一个窗口)`、补收的旧消息回复得 410 且假上游没收到 `sendmessage`、确认之后在有效期内仍可回复、`hubctl login` 之后旧句柄作废、过期和作废的话句柄 410）、`test_online.py`（在线的三个依据：挂着的请求、最近的取话 / 确认 / 心跳、重启后仍按最近取话时间算；停用和只推送永远不在线；处理 120 秒期间发心跳不被判离线）、`test_awaiting.py`（标记的写入函数和条件（"之后"按 `(at_ms, 入库先后)` 比较；调用方在 C4 的回复接口里）、清除条件（到期、收到任何话、停用、收到确认话时不在线）、问服务和别的项目不影响、重启不清、几个项目同时在等）
  - 再实现 `inbox.py`
- [ ] C3 `poller` 与服务自己的回复 `[云]`
  - 先写测试：`tests/poller/test_poller.py`（收一句话、去重、一批消息按 `at_ms` 排序、多个文字项依次拼接、没有对话凭证的不收、事务提交后才写回 `updates_buf` 且两步之间被杀不重复、写回时核对 `bot_token`、非本人丢弃只计数、机器人自己的消息（`message_type` 为 2）丢弃、错误退避 5 / 10 / 20 …并在恢复后重新计、`longpolling_timeout_ms` 加 10 秒、-14 进入探测且探测间隔是 `relogin_probe_seconds`、凭证监视置位唤醒事件后睡在探测间隔里的 poller 马上用新凭证收一次）、`tests/poller/test_replier.py`（服务 项目 / 状态 / 未知指令的回复内容和 `【服务】` 开头、`hub.label` 可改、回复记在 `_hub` 名下算回复、没在线提醒与四种提示（没有默认项目、项目已停用、没在线、队列满了）的限频（同一个目标 5 分钟内一条，重启不清零）、没有默认项目与默认项目已停用的回复文字、发送锁被占着时 poller 不停、至多一次（放进队列后被杀不补发）、队列满丢最旧、补收的旧消息触发的回复发不出去也不告警、指令和无处可去的话不存正文）
  - 再实现 `poller.py`、`replier.py`
- [ ] C4 取话和回复接口 `[云]`
  - 先写测试：`tests/api/test_inbox.py`（`/v1/inbox`、`/v1/inbox/ack`：字段（示例里 `at` 与 `at_ms` 对得上）、`wait` / `max` 边界、只推送项目 403、只看得到自己的、停用时的 403）、`tests/api/test_replies.py`（`POST /v1/replies`：句柄找不到或属于别的项目 404（两种一模一样）、过期 410、同一个句柄成功回复满 `reply.max_per_handle` 次后 429 `reply_limit` 且被拒的不占次数、`confirm_seconds` 越界 400、用被回复那句话的凭证发、等确认标记只在 `parts_sent ≥ 1` 且被回复的话之后没有新话时记、未确认也记、回复没发出去不记、回复期间又来了新话不记）
  - 再实现 `api.py` 里取话和回复的部分
- [ ] C5 正文不落盘（收话这一侧）`[云]`
  - 先写测试：`tests/inbox/test_no_body.py`（取走并确认的话、放到过期的话、租出 5 次作废的话、服务自己处理的话和没地方可去的话，数据库（含 `-wal`）、日志、事件里都搜不到独特字符串；对话凭证在回复句柄失效时才清）
  - 再补实现中遗漏的清理
- 阶段 C 验收（pytest 驱动）：`uv run pytest -q tests/router tests/inbox tests/poller tests/api tests/sender` 全过；`tests/api/test_e2e_inbox.py`：两个项目对着进程内假上游（真 uvicorn）走一遍"发话 → 路由 → 取话 → 回复 → 等确认 → 确认"

## D. 运维文档、告警、命令行、客户端、部署

- [ ] D1 运维文档 `[云]`（本阶段第一项，后面的演练照着它做）：`docs/ops/安装说明.md`、`迁移手册.md`（导入凭证前先 `launchctl bootout` 停服务）、`回退手册.md`、`真机验证清单.md`（内容见 `hub-deploy`）；先写一个小测试 `tests/deploy/test_ops_docs.py`（四份文档存在、停服务的写法是 `launchctl bootout`、真机验证清单里列了 `hub-deploy` 点名的每一项）
- [ ] D2 告警与定时任务 `[云]`
  - 先写测试：`tests/alerts/test_rules.py`（每种告警的触发和去重单位：`upstream_down` 的往返定义（5xx 算不通、登录失效不算、没绑定不算）、`send_unconfirmed` 按请求数且 `rejected` 不重置、`window_blocked` 的汇总 N、一个窗口一次、12 小时间隔（被间隔压住的拒绝不丢，间隔一满的下一轮就发）、没窗口时的 `none`（使用者发话开出窗口时清掉，重新绑定后可再告警）、`upstream_blocked`、`backup_failed` 失败 10 分钟后重试才告警、`service_restarted` 取最后心跳；重启不重复、恢复只在报过失效之后、窗口提醒的 4 个条件含 ISO 星期（周一、周日各一行）、崩溃循环不刷屏）、`test_bark.py`（请求格式、设备码两种写法、环境变量优先于 `bark.json`、前 4 次 500 才进待发队列而前 3 次 500 第 4 次成功不进、队列 5 分钟重试 1 小时后放弃、重启不丢待发、设备码错（`code ≠ 200`）不重试并让 `doctor` 警告、没配设备码的行为、不含秘密和正文、Bark 慢不拖累收发）、`tests/scheduler/test_scheduler.py`（清理、心跳写 `run.json`、备份按时间点触发并命名 `auto-…`（用 A3 的 `backup.py`）、假时钟推进）
  - 再实现 `alerts.py`、`scheduler.py`
- [ ] D3 命令行 `[云]`
  - 先写测试：`tests/cli/test_status_doctor.py`（服务停着也能看、`--json`、没有凭证和密钥、"服务在不在运行"按 `run.json` 的进程号和 60 秒心跳判、`doctor` 逐项级别（`hub-cli` 那张表的每一行）、退出码只看 ✗、`--fix`、`--online` 不碰腾讯、端口检查只在服务没运行时、扫码进行中不误报 `login.pending`、保底合计超了和前缀与确认词冲突为 ✗）、`test_project_cli.py`（`project` 各子命令、需要确认的操作的确认和 `--yes`、`set --default`、`--no-label`、输出里的前缀撞指令提醒、`list` 不露内部名字且 `--all` 显示已移除）、`test_send_cli.py`（退出码 0（含 `skipped`）/ 3 / 4 / 5 / 6 / 10 / 11 / 12 / 13、不收命令行密钥、`--key-file` 权限太宽拒绝、从标准输入读）、`test_backup.py`（`hubctl backup` 命令层：备份能打开、服务运行时备份、`--to` 是父目录且不改已有目录权限、云盘路径警告；核心函数和命名已在 A3 测过）、`test_events.py`（封闭的八种类、`--kind` 精确匹配、未知种类退出 2）、`test_misc_cmds.py`（`alerts set-key` / `alerts test`、`config show`（含默认值、打码、"配置文件改过"提示）、`version`、`--help` 列出全部命令、密钥只打印一次且之后搜不到）、`test_login_cmd.py`（`login` 和 `import-credentials` 的命令层：`--probe`、`--replace` 的交互确认）
  - 再实现 `cli.py`、`doctor.py`、`backup` 子命令（`hubctl serve` 留到 D6）
- [ ] D4 单文件客户端 `[云]`
  - 先写测试：`tests/client/test_client.py`（接口、数据类、`ok` / `needs_fallback` / `maybe_delivered`、不认识的 `status` 要走兜底、没密钥第一次调用才抛、异常分类和 `possibly_sent`（读超时、被重置、500 为真；连不上、502 / 503 / 504 为假）、`retries` 构造参数与不偷偷重发的规则、读超时随 `wait` 和服务的发送总时限变化、`ack` 超过 50 个分批、未知字段忽略）、`test_run_inbox.py`（确认 / 退避 5 10 20 / 停用抛 `HubAuthError` / 429 睡 `retry_after` / `ack` 失败不丢 / 处理期间后台线程发心跳（`heartbeat_seconds` 传很小的数、`handler` 阻塞几百毫秒，结束或抛异常后心跳停）、心跳间隔的默认值（`inbox_online_seconds / 3`，取不到按 90）、慢指令期间项目一直在线 / 空转保护（`wait=0` 每次都睡 1 秒，服务等满的空列表不睡）/ `stop`）、`test_stdlib_only.py`（只 import 标准库、`ast.parse(..., feature_version=(3, 9))`、没有运行时求值的 `X | Y`）
  - 再实现 `client/clawbot_hub_client.py`
- [ ] D5 合约测试与接口文档 `[云]`
  - 先写测试：`tests/contract/test_end_to_end.py`（两个项目、真 HTTP 服务、假上游、单文件客户端：发每种状态、回复、等确认路由、停用和换钥、限速、错误映射、`retries`、读超时）、`tests/contract/test_no_credentials.py`（项目用自己的密钥调遍所有 `/v1` 接口，所有返回里都找不到 `bot_token`、`context_token`、`get_updates_buf` 的值；上游报错带着 `bot_token` 时返回给项目的原因里是 `***`）、`tests/contract/test_docs_match.py`（OpenAPI 导出与 `docs/api/openapi.json` 一致、`docs/api/v1.md` 的示例能跑且 `at` 与 `at_ms` 是同一个时刻、每个接口都有合约测试、接入指南里有 `hub-client` 要求的那几条约定）
  - 再写 `docs/api/v1.md`、`docs/clients/接入指南.md`，导出 `docs/api/openapi.json`
- [ ] D6 进程与部署 `[云]`
  - 先写测试：`tests/process/test_lifecycle.py`（第二个实例退出码 2 且不碰假上游、`kill -9` 后能启动并把"发送中"标成"中断"、`SIGTERM` 发送完再退且取话请求 1 秒内返回、等锁的请求关闭时马上 `busy`、关闭时间用完强退（退出码 1、`run.json` 不记正常退出、下次启动发 `service_restarted`）、端口被占退出码 1、数据库比程序新退出码 3、配置错误时什么都没碰、启动 8 步的先后、凭证坏了 / 没绑定照常启动、`run.json` 的心跳和 `clean_exit`、上次被杀时 `service_restarted` 先登记、由告警线程发出，Bark 不通也不拖慢监听；配置与已有前缀冲突的启动警告）、`tests/deploy/test_logging.py`（脱敏、请求日志不含查询串和密钥、轮转份数和大小）、`test_plist.py`（launchd 模板合法：`KeepAlive` 是 `SuccessfulExit: false` 的字典、`ThrottleInterval`、`Umask` 63）、`test_timezone.py`、`test_config_rules.py`（`lan` 与 `host`、窗口总条数改小后只警告、`send.part_chars` 小于 200 报错、`server.shutdown_seconds` 小于发送总时限只警告且 `doctor` 报告、腾讯地址的来源）、`test_retention.py`（91 / 89 天、删窗口时 `outbox.window_id` 置空、去重键到期释放、对话凭证的清理时机）、`test_restart_state.py`（重启后保持和清零的清单）、`test_standalone_fake.py`（`hubctl serve` 的 `upstream.base_url` 指向独立运行的假腾讯，扫码、收、发全程走通）
  - 再实现 `app.py` 的生命周期、`hubctl serve`、`logging_setup.py`，写 `deploy/launchd/com.clawbot-hub.serve.plist.example`
- 阶段 D 验收：`uv run ruff format --check && uv run ruff check && uv run pytest -q` 全绿（整套 90 秒内）；`uv run hubctl --help` 列出全部命令；脚本化演练：独立运行的假腾讯加 `hubctl serve`，用 `curl` 发一条、`hubctl send` 发一条、`hubctl status` 看全貌

## E. 验收和交接

- [ ] E1 `[云]` 把 `README.md` 的"状态"改成可用，补"预定的用法"里的实际输出；`CLAUDE.md` 的"常用命令"改成真实命令
- [ ] E2 `[云]` 向使用者汇报：做了什么、测试结果、默认值、没做的、下一步要在 Mac 上做的事
- [ ] E3 `[Mac]` 使用者授权后：照 `docs/ops/安装说明.md` 装环境（`uv sync`）、`hubctl doctor`、装 launchd（装好后 `ls -l` 看 `launchd.log` 的权限），**先不连真实的 ClawBot**，用 `tests/support/fake_ilink.py` 的独立运行模式演练一遍：登记 ashare-quant、发一条、收一条、断一次网、重启、`launchctl bootout` 停服务
- [ ] E4 `[Mac]` 使用者授权后：按 `docs/ops/真机验证清单.md` 在真账号上测（先问清楚会占用额度、会不会影响现有微信问答），把实测值填进 `tests/live/test_real_ilink.py`，结果写回文档，必要时把配置默认值（`upstream.confirm`、`upstream.profile`、窗口长度和条数）改成实测值
- [ ] E5 `[云]` 用 `openspec archive` 把本变更归档进 `openspec/specs/`

## 不在本变更里（另立变更，等使用者点头）

- ashare-quant 加"走服务"的模式（`wechat.mode: direct | hub`，默认 `direct`）：改 `notify/clawbot.py`（换成薄客户端）、`notify/channels.py`、`cli.py` 的 `aq wechat …`、`chat/bot.py` 和 `chat/wiring.py`、`web/system_view.py`、`overview_data.py`、`push.py`、launchd 模板和安装说明。动手前先和 Mac 上还没推送的本机提交对齐。
- 周末的切换（停旧任务、导入凭证、启动服务、切模式、观察一个交易日），按 `docs/ops/迁移手册.md`，使用者本人在场。
- 稳定一段时间后，删掉 ashare-quant 里直连腾讯的代码。
- 其他项目的接入：各自一个小改动，按 `docs/clients/接入指南.md`。

## 测试清单（汇总）

| 模块 | 测试文件 | 证明了什么 |
|---|---|---|
| 守卫、时钟、假件 | `tests/test_socket_guard.py`、`tests/test_clock.py`、`tests/support/test_fake_ilink.py`、`test_fake_bark.py`、`test_fake_upstream.py` | 测试不能联网；假时钟不真等；假腾讯、假 Bark、假上游能按剧本模拟各种情形（含独立运行） |
| 文本 | `tests/textutil/test_split.py`、`test_match.py`、`test_mask.py` | 拆条与 ashare-quant 一致（标题上限是有意改动）；前缀匹配规则；秘密被遮掉 |
| 配置 | `tests/config/test_config.py`、`tests/deploy/test_config_rules.py` | 默认值齐全；错误一次列全；`lan` 与 `host`；保底与总条数的约束；腾讯地址的来源 |
| 数据库 | `tests/db/test_migrations.py`、`test_permissions.py`、`test_txn.py`、`test_pragmas.py`、`test_backup_core.py` | 只往前升级、失败回滚；权限；并发写不丢；`secure_delete`；表结构的关键约束；备份核心与命名 |
| 腾讯接口 | `tests/upstream/test_http_profiles.py`、`test_http_outcomes.py` | 两档请求形式；各种返回如何分类；-14 的判断顺序；每次新连接；报错不泄露凭证 |
| 凭证与登录 | `tests/account/test_account.py`、`test_watcher.py`、`tests/login/test_qr_login.py`、`test_import.py`、`test_expiry.py` | 凭证文件安全且不被覆盖；换凭证 5 秒内生效；扫码各状态；导入时额度接得上；-14 的处理和恢复条件 |
| 项目 | `tests/projects/test_projects.py`、`test_keys.py` | 登记规则；密钥只存摘要；换钥；防猜 |
| 额度 | `tests/quota/test_decide.py`、`test_ledger.py`、`test_hold.py` | 保底、上限、共享池、紧急、可延后；预占与逐条对账不错账；腾讯拒绝后暂停 |
| 最小命令行 | `tests/cli/test_minimal_cli.py` | 阶段 A 的 `login`、`project add / list`、`status` |
| 发送 | `tests/sender/test_send_states.py`、`test_check_order.py`、`test_budget.py`、`test_labels_split.py`、`test_reasons.py`、`test_dedupe.py`、`test_reply_core.py`、`test_window_change.py`、`test_internal_error.py`、`test_no_body_send.py`、`test_busy.py`、`test_on_closed.py` | 每种结果状态；检查先后；总时限与 1 秒保护；不偷偷重发；原因文字；去重；回复的发送层；换窗口记账；500 不丢账；发出去的正文不落盘 |
| 路由 | `tests/router/test_route.py` | 一句话交给谁的全部规则 |
| 收话 | `tests/inbox/test_queue.py`、`test_handles.py`、`test_online.py`、`test_awaiting.py`、`test_no_body.py` | 租约、确认、至少一次；回复句柄；在线判断；等确认不串线；收到的话不留正文 |
| 收消息与服务回复 | `tests/poller/test_poller.py`、`test_replier.py` | 收消息循环、退避、去重、排序；服务自己的回复、限频、至多一次 |
| 告警 | `tests/alerts/test_rules.py`、`test_bark.py`、`tests/scheduler/test_scheduler.py` | 每种告警何时发、不重复、重启不重复；Bark 请求与重试；定时任务 |
| 接口 | `tests/api/test_auth_limits.py`、`test_errors.py`、`test_messages.py`、`test_inbox.py`、`test_replies.py`、`test_e2e_send.py`、`test_e2e_inbox.py` | 认证、限速、隔离、局域网规则；错误格式（含 405、422 映射）；各接口字段；回复接口的句柄规则；阶段 B、C 的端到端 |
| 命令行 | `tests/cli/test_status_doctor.py`、`test_project_cli.py`、`test_send_cli.py`、`test_backup.py`、`test_events.py`、`test_misc_cmds.py`、`test_login_cmd.py` | 各命令行为与退出码；备份能用；`doctor` 逐项级别且不碰腾讯；事件种类 |
| 客户端 | `tests/client/test_client.py`、`test_run_inbox.py`、`test_stdlib_only.py` | 异常分类、不偷偷重发、取话循环与心跳；只依赖标准库、兼容 3.9 |
| 合约 | `tests/contract/test_end_to_end.py`、`test_no_credentials.py`、`test_docs_match.py` | 项目这一侧的全部约定；项目拿不到任何腾讯凭证；文档与接口一致 |
| 进程与部署 | `tests/process/test_lifecycle.py`、`tests/deploy/test_logging.py`、`test_plist.py`、`test_timezone.py`、`test_retention.py`、`test_restart_state.py`、`test_standalone_fake.py`、`test_ops_docs.py` | 单实例、崩溃恢复、优雅退出、启动顺序；日志脱敏；launchd 模板；时区；留存；重启后的状态；运维文档 |
| 真机（默认不跑） | `tests/live/test_real_ilink.py` | 腾讯一侧的规则（有无消息编号、窗口实测等），Mac 上使用者授权后手动运行 |
