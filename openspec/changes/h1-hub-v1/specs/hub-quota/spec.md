# Spec Delta

## Purpose

规定几个项目怎么共用腾讯给的那一份额度："使用者发一条消息之后，24 小时内机器人最多能发 10 条"。这两个数不是官方写明的、而且在变，所以窗口多长、总共多少条都是配置；服务只负责**分**，加不出额度。

目标：回使用者的话永远发得出去；每个项目至少有自己的保底；谁也不能把别人的日报挤掉；额度紧的时候先让可延后的消息让路；服务重启、发到一半出错，账都不能错。

## ADDED Requirements

### Requirement: 窗口

使用者（凭证里的 `user_id`）每发来一条消息，服务 MUST 开一个新窗口：窗口的起点是那条消息的时间（腾讯给的 `create_time_ms`，没有就用收到的时间），对话凭证换成那条消息的，窗口里的已用条数清零。窗口长度 `quota.window_hours`（默认 24），总条数 `quota.window_limit`（默认 10）。时间早于当前窗口起点的消息（补收的旧消息）MUST NOT 把窗口倒回去，只有时间不早于当前起点的才开新窗口。

窗口里的已用条数是服务自己记的：每成功发出一条（含"未确认送达"）加一，发出的是推送还是回复分开记，哪个项目发的也分开记。窗口过期（现在 ≥ 起点 + 窗口长度）或者从来没收到过使用者的消息时，窗口是"关着"的。

#### Scenario: 说一句话就开窗口

- **WHEN** 使用者的一条消息到达，窗口里已经用了 7 条
- **THEN** 新窗口已用 0 条，起点是这条消息的时间，用来推送的对话凭证换成这条

#### Scenario: 旧消息不倒回窗口

- **WHEN** 当前窗口起点是 10:00，收到一批补收的消息，其中一条的时间是 09:30
- **THEN** 窗口起点仍是 10:00，已用条数不变；10:05 的那条会开新窗口

#### Scenario: 窗口过期

- **WHEN** 现在是起点之后 24 小时零 1 分钟
- **THEN** `GET /v1/status` 里窗口是关着的，`left` 为 0

#### Scenario: 时间和条数是配置

- **WHEN** 配置 `quota.window_hours: 12`、`quota.window_limit: 5`
- **THEN** 12 小时后窗口过期；窗口里最多能发 5 条；对应的原因文字里写 12 小时和 5 条

### Requirement: 谁能用几条

窗口里的 `quota.window_limit` 条（记作 L）这样分：

- **回复保留**（`quota.reply_reserve`，默认 2，记作 R）：留给回复使用者的话（项目的回复和服务自己的回复）。推送不能动它，直到它被回复用掉。
- 每个启用的项目有**保底** g 和**上限** c（登记时定，见 `hub-projects`）：保底是别的项目动不了的；上限是这个项目在一个窗口里最多推送的条数。
- 其余是**共享池**：没有被保底和回复保留占着的条数。

记号：T 是窗口里的总已用条数（所有项目的推送加所有回复，含正在发的预占）；free = L − T；V 是回复已用条数；reserve_left = max(R − V, 0)；U_p 是项目 p 已用的推送条数；own_left = max(g_p − U_p, 0)；pool = free − reserve_left − Σ（所有启用项目的 max(g_q − U_q, 0)）。`_hub`、`_import` 这两个内部名下的用量只算进 T 和 V（见 `hub-inbox`、`hub-login`），它们没有保底和上限。

一次要发 n 条，判定如下，**按这个顺序**，第一个不满足的就是返回的原因（原因文字见 `hub-send`）。在整个发送流程里，登录状态和"腾讯拒绝后的暂停"在这之前检查（见 `hub-send` 的检查顺序）；下面第 1 步保留，是因为判定这个纯函数自己也要能处理窗口关着。

1. 窗口关着：从没收到过消息 → `never_contacted`；已过期 → `window_closed`。
2. free ≤ 0 → `quota_exhausted`；n > free → `quota_insufficient`。
3. **回复**到这里就放行（回复不受保留、保底、上限限制，回使用者的话永远优先）。
4. 推送：优先级是 `deferrable`，且 free − n < `quota.deferrable_keep`（默认 3）→ `deferrable_denied`。
5. 优先级不是 `urgent`，且 U_p + n > c_p → `project_cap`。（`urgent` 可以超过自己的上限。）
6. n > free − reserve_left → `reserved_for_others`；超出保底的部分 n_over = max(n − own_left, 0) 大于 max(pool, 0) → `reserved_for_others`。
7. 都过了：放行。

**还能发几条**（`left`）：一条 `normal` 的推送现在最多能拆成几条发出去，等于 max(0, min(free − reserve_left, c_p − U_p, own_left + max(pool, 0)))。登录状态不正常、窗口关着、推送处于腾讯拒绝后的暂停期时是 0。

#### Scenario: 保底加回复保留正好占满，没有共享池

- **WHEN** L=10、R=2，项目 A（保底 6、上限 8）、项目 B（保底 2、上限 4），新窗口里 A 连发 6 条（每次 1 条），再发第 7 条
- **THEN** 前 6 条都放行；第 7 条 `rejected` / `reserved_for_others`（此时 free=4，减去回复保留 2、B 没用的保底 2，共享池为 0）

#### Scenario: 保底之外有共享池

- **WHEN** L=10、R=2，项目 A（保底 4、上限 8）、项目 B（保底 2、上限 4），新窗口里 A 连发 7 条
- **THEN** 前 6 条放行（4 条保底加共享池的 2 条），第 7 条 `rejected` / `reserved_for_others`；B 随后发 2 条都放行

#### Scenario: 上限

- **WHEN** 只有项目 A（保底 6、上限 8）一个启用的项目，L=10、R=2，连发 9 条
- **THEN** 前 8 条放行，第 9 条 `rejected` / `project_cap`

#### Scenario: 紧急的可以超过自己的上限，但动不了别人的保底

- **WHEN** L=10、R=2，A（保底 4、上限 4）、B（保底 2、上限 2），A 已用 4 条，A 再发一条 `normal`、一条 `urgent`、又发 2 条 `urgent`
- **THEN** `normal` 是 `project_cap`；第一条 `urgent` 放行（共享池还有 2）；第二条 `urgent` 放行（共享池用完）；第三条 `urgent` 是 `reserved_for_others`

#### Scenario: 可延后的先让路

- **WHEN** 只有项目 A（保底 6、上限 8）一个启用的项目，L=10、R=2，`quota.deferrable_keep` 是默认的 3，窗口里已经用了 6 条（都是 A 的推送）；A 先发一条 `deferrable`，再发一条 `deferrable` 和一条 `normal`
- **THEN** 第一条 `deferrable` 放行（free − 1 = 3，不少于 3；窗口用到 7 条）；第二条 `deferrable` 是 `deferrable_denied`（此时 free=3，3 − 1 = 2 少于 3）；`normal` 只按保底、上限、共享池的规则判定，放行

#### Scenario: 回复超过保留

- **WHEN** L=10、R=2，A（保底 6）、B（保底 2），窗口里回复已用了 3 条，A 发 6 条推送
- **THEN** reserve_left 为 0；A 在保底内发的 6 条都放行；之后窗口里只剩 1 条，B 的保底只能用上 1 条（回复先于保底）

#### Scenario: 回复不受推送挤占

- **WHEN** 推送把 L − R 条都用光了，使用者发来一句话，服务（或项目）回复
- **THEN** 新窗口已用清零，回复放行；即使没有新消息，窗口里还有的回复保留 2 条，回复也放行

#### Scenario: 停用的项目不再占保底

- **WHEN** 项目 B（保底 2）被停用，A（保底 6、上限 8）原来共享池为 0
- **THEN** 共享池变成 2，A 可以用到 8 条

#### Scenario: 还能发几条

- **WHEN** 新窗口、A 保底 6、上限 8、B 保底 2、L=10、R=2，A 已用 0
- **THEN** A 的 `left` 是 6；A 用了 6 条以后 `left` 是 0

#### Scenario: 登录失效或暂停时 left 是 0

- **WHEN** 窗口开着、额度充足，但登录状态是失效（或者推送处于腾讯拒绝后的暂停期）
- **THEN** 项目的 `left` 是 0；回复仍然能发（暂停期内），登录失效时回复也发不出

### Requirement: 预占和对账

发送前，服务 MUST 在同一个数据库事务里完成"判定 + 记账"：判定通过的 n 条立刻记进窗口的已用条数（预占），并建发送记录；两个请求同时来也不会一起超额（数据库写事务 `BEGIN IMMEDIATE`，等锁最长 `db.busy_timeout_ms`）。

**预占时定下三件事**：

- **预占窗口 W**：判定那一刻的当前窗口（推送和回复都一样；回复的条数一律记进最新的窗口，不管被回复的那句话属于哪个窗口）；
- **对话凭证**：推送用 W 的（使用者最近一条消息的），回复用被回复那句话的；整条消息的几段都用这一个，中途不换；
- 发送记录里记下 W 的编号（W 可以为空：因为没有窗口而被拒绝的记录没有窗口）。

**之后逐条对账**（发完一条就记一条，不等整条结束）：

- 这一条发出去了（`sent`、`unconfirmed`）：如果这时当前窗口已经不是 W（发送期间使用者说了话，开了新窗口），这一条在**当前窗口**的已用里再加一（同一个项目、同一类别）；W 里的数不退。窗口没换就保持已用。
- 这一条没发出去（腾讯拒绝、连不上、没来得及发、前一条拿不准所以没发）：窗口还是 W 就从 W 退回这一条；窗口已经换了就不用退（旧窗口的数字不再重要）。
- 服务在发送中途挂了：重启时，仍标着"发送中"的记录 MUST 标成"中断"，它的预占 MUST 保留不退（不知道发出去了几条），并写一条事件记录。

这与 ashare-quant 现在的做法一致：它每发出一条就看一次窗口，换了之后发出的每条才加一，换之前发出的留在旧窗口。

#### Scenario: 拆成三条，第二条失败

- **WHEN** 一条消息拆成 3 条，第 2 条被腾讯拒绝
- **THEN** 窗口已用先是 +3，结束后是 +1（第 1 条算用掉；第 2、3 条退回）

#### Scenario: 两个项目同时发（回复保留已被回复用完）

- **WHEN** L=10、R=2，A（保底 4、上限 8）、B（保底 2、上限 4），回复已用 2 条，A 已推送 5 条、B 已推送 2 条（窗口已用 9 条，只剩 1 条，它在共享池里）；两个项目各发 1 条，几乎同时到达
- **THEN** 两个项目单独看都能发；只有一个放行，另一个是 `quota_exhausted`；已用不超过总条数

#### Scenario: 两个项目同时发（回复保留还在）

- **WHEN** 同上，但回复一条都没用（窗口已用 7 条）；两个项目各发 1 条
- **THEN** 一个放行，另一个是 `reserved_for_others`（放行之后只剩 2 条，都是留给回复的）；已用不超过总条数

#### Scenario: 发送途中使用者说了话，换窗口发生在第 2 条在飞时

- **WHEN** 一条 3 条的消息，第 1 条已发出，第 2 条还没回话时使用者的新消息到达，开了新窗口，第 2、3 条随后都发出去了
- **THEN** 新窗口的已用是 2（第 2、3 条）；对话凭证全程是预占时的那一个

#### Scenario: 发送途中使用者说了话，换窗口发生在第 3 条在飞时

- **WHEN** 同上，新消息在第 3 条还没回话时到达
- **THEN** 新窗口的已用是 1（第 3 条）

#### Scenario: 换窗口以后没发出去的条不退

- **WHEN** 3 条的消息，第 1 条发出后使用者说话开了新窗口，第 2 条被腾讯拒绝
- **THEN** 新窗口的已用是 0（第 1 条发在换窗口之前，留在旧窗口；第 2、3 条没发出去，旧窗口不用退）

#### Scenario: 回复记进最新窗口

- **WHEN** 使用者先后发了 A、B 两句话（B 开了新窗口），项目拿 A 的句柄回复
- **THEN** 这条回复记在 B 的窗口里（"回复"一类加一），用的是 A 的对话凭证

#### Scenario: 发送中途服务挂了

- **WHEN** 发送记录是"发送中"、预占 3 条时服务被 `kill -9`，再启动
- **THEN** 记录变成"中断"，窗口已用仍包含那 3 条，事件里有一条记录，`hubctl status` 的提示里提到有一条中断的发送

#### Scenario: 重启不丢账

- **WHEN** 窗口已用 4 条，服务重启
- **THEN** 重启后 `hubctl status` 显示的窗口起点、已用条数不变

### Requirement: 腾讯拒绝以后先停一会儿

推送被腾讯**明确拒绝**（业务码非 0 且不是 -14，或 HTTP 4xx，见 `hub-send`）时，服务 MUST 记下"这个窗口里腾讯拒绝过发送"：之后 `quota.reject_hold_minutes`（默认 30）分钟内，同一个窗口里的推送直接 `rejected` / `upstream_blocked`，不碰腾讯、不占条数。**回复不受这个影响**（回复用的是刚到的那句话自己的凭证）。

过了这段时间，下一条推送放行一次作为**试探**：在放行试探的同一个判定事务里，把暂停的截止时间临时设为"现在 + `send.timeout_seconds`"，这样同时到达的其他推送继续被拦在门外。试探的结果：`sent` → 解除；又被腾讯明确拒绝 → 再停 `reject_hold_minutes`；`unconfirmed` 或连不上 → 撤回临时值，下一条推送仍当试探。新窗口（使用者又说了话）MUST 解除。

这是因为窗口和条数的规则腾讯可能改，并且不一定会说：被拒就当窗口已不可用，别再往里发，让调用方马上改走 Bark，同时给运维一个明确的信号（见 `hub-alerts`）。

#### Scenario: 被拒之后暂停

- **WHEN** 假上游对一条推送回 `ret: 40001`，30 分钟内项目又发一条推送
- **THEN** 第一条是 `failed` / `upstream_rejected`；第二条是 `rejected` / `upstream_blocked`，假上游只收到了第一条

#### Scenario: 过了暂停期试一次

- **WHEN** 暂停期过了，假上游恢复正常，项目发消息
- **THEN** 放行并成功，之后的推送正常

#### Scenario: 试探时别的推送被拦住

- **WHEN** 暂停期刚过，两个项目的推送几乎同时到达
- **THEN** 只有一个作为试探放行，另一个是 `rejected` / `upstream_blocked`；假上游只收到一条

#### Scenario: 试探连不上，下一条继续试探

- **WHEN** 试探的推送连不上（`failed` / `upstream_unreachable`），随后另一条推送到达
- **THEN** 后一条仍然被当作试探放行（临时的暂停被撤回）

#### Scenario: 说一句话就解除

- **WHEN** 暂停期内使用者发来新消息
- **THEN** 新窗口里的推送直接放行；回复在暂停期内本来就放行

### Requirement: 额度可查

`GET /v1/status`（用项目密钥）MUST 返回调用方自己的额度，不含别的项目的名字和用量：

```json
{
  "service": {"version": "1.0.0", "api": "1",
              "send_timeout_seconds": 30, "inbox_online_seconds": 90},
  "login": {"state": "ok"},
  "window": {"open": true, "started_at": "…", "ends_at": "…", "seconds_left": 74100},
  "quota": {"limit": 10, "used": 4, "free": 6,
            "project": {"guaranteed": 6, "cap": 8, "used": 3, "left": 3, "replies_used": 1}}
}
```

`login.state` 是 `ok`（正常）、`expired`（登录失效）、`missing`（还没绑定）、`broken`（凭证文件读不出来）；窗口关着时 `window.open` 为 false、`started_at` 可能为 null、`quota.project.left` 为 0。`service.send_timeout_seconds` 和 `inbox_online_seconds` 是服务现在的 `send.timeout_seconds` 和 `inbox.online_seconds`，给客户端算超时和心跳间隔用。`hubctl status` MUST 给运维看到完整的画面：每个项目的保底、上限、已用（推送和回复分开）、在线与否，窗口起点和剩余时间。

#### Scenario: 项目看不到别人

- **WHEN** 项目 A 调 `GET /v1/status`
- **THEN** 返回里有窗口的总用量和 A 自己的数字，找不到别的项目的名字和它们的用量

#### Scenario: 运维看全貌

- **WHEN** 执行 `hubctl status`
- **THEN** 输出里每个项目一行：保底、上限、已用推送、已用回复、在线与否；另有窗口起点、剩余时间、总已用 / 总条数

#### Scenario: 凭证文件坏了

- **WHEN** `account.json` 读不出来
- **THEN** `login.state` 是 `broken`，`quota.project.left` 为 0
