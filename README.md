# clawbot-hub

微信 ClawBot 共用服务：一个常驻程序独占 ClawBot 的登录，多个项目通过本机接口一起发消息、收指令；24 小时 10 条的额度、一句话交给哪个项目、登录失效和窗口到期的提醒，都由它统一管。

**状态（2026-10-16）：代码写完，测试全过；还没有在 Mac 上装过，也没有连过真实的 ClawBot。** 第一个变更 `h1-hub-v1` 的服务、命令行、告警、定时任务、单文件客户端、部署模板都做完了，整套测试五千五百多条全部通过（只用假腾讯和假 Bark，不联网）。下一步在 Mac 上：先照《安装说明》装好并用假腾讯演练，再按《真机验证清单》在真账号上测，最后才按《迁移手册》把 ashare-quant 切过来。这几步都要使用者点头。

## 看哪里

| 想知道 | 看 |
|---|---|
| 为什么做、做成什么样、各项目怎么接 | `docs/specs/2026-10-09-ClawBot共用服务-设计方案.md` |
| 腾讯 iLink 协议和已知的规则、没弄清的地方 | `docs/research/2026-10-09-ClawBot协议与规则调研.md` |
| 第一个变更的提案、设计、任务单、九份规格 | `openspec/changes/h1-hub-v1/` |
| 在 Mac 上怎么装、怎么从 ashare-quant 迁过来、出事怎么退、哪些规则要真机验证 | `docs/ops/` 下的《安装说明》《迁移手册》《回退手册》《真机验证清单》 |
| 项目怎么接入（发消息、取话、回复） | `docs/clients/接入指南.md`；别的语言看 `docs/api/v1.md` |
| 约定（语言、测试、密钥、不做什么） | `CLAUDE.md` |

## 用法

服务装在 Mac mini 上，用 launchd 常驻；完整步骤（含用假腾讯的演练）在《安装说明》。下面的输出是用假腾讯、临时数据目录跑出来的真实输出（号码、密钥、设备码都是假的或缩写了）：

```
uv sync
uv run hubctl login          # 扫码绑定 ClawBot（只在服务这里扫）
```

```
用微信扫上面的二维码（约 2 分钟有效，过期会自动换新码）；也可以打开图片 ~/.clawbot-hub/clawbot-login.png
绑定成功。现在在微信里给 ClawBot 发任意一句话，之后 24 小时内它才能给你发消息。
```

```
uv run hubctl project add ashare-quant --prefix 量化 --guaranteed 6 --cap 8 --default
```

```
已登记项目 ashare-quant（前缀「量化」，保底 6、上限 8，默认项目）
密钥（只显示这一次，请马上存好，之后任何命令都不再显示）：
chub_……（一长串）
提醒：服务分不清默认项目自己的指令里有没有和前缀「量化」同名的词。例如前缀取「状态」，默认项目整句「状态」就会被交给这个项目；请自己确认。
```

```
uv run hubctl serve          # 常驻；正式用 launchd 跑，Ctrl-C 或 SIGTERM 正常停
```

```
2026-10-17T00:43:02+08:00 INFO clawbot_hub.app 已启动：版本 0.1.0，监听 127.0.0.1:8790，上游档位 legacy，项目 1 个
```

另开一个窗口看全貌（这里是使用者已经说过一句话、项目还没来取的样子）：

```
uv run hubctl status
```

```
服务：在运行（进程号 32242，已运行 不到 1 分钟），版本 0.1.0，监听 127.0.0.1:8790，上游档位 legacy，送达确认 observe
登录：已绑定（10月17日 00:43 绑定，机器人号 ….bot）
  收消息：最近一次成功 10月17日 00:43，最近一次出错 没有
  忽略的陌生人消息：0 条
对话窗口：开着（10月17日 00:43 起，还剩约 23 小时 59 分钟）
  已用 1/10 条（推送 0、回复 1）
  腾讯拒绝后暂停：否
项目：
  ashare-quant（前缀「量化」，默认项目）：保底 6、上限 8，本窗口已推送 0 条、回复 0 条；离线；最后出现 还没有
待处理：
  等着项目取的话：ashare-quant 1 条
```

项目用密钥调本机接口 `http://127.0.0.1:8790/v1/…`。命令行也能直接发一条（给脚本用）：

```
CLAWBOT_HUB_KEY=<项目密钥> uv run hubctl send "盘点完成"
```

```
sent
```

Python 项目把只依赖标准库的单文件客户端 `client/clawbot_hub_client.py` 拷进去用，写法见《接入指南》：

```python
from clawbot_hub_client import HubClient

hub = HubClient()                    # 密钥读环境变量 CLAWBOT_HUB_KEY
result = hub.send("盘点完成", dedupe_key="inventory-2026-10-17")
```

### 命令一览

`uv run hubctl --help` 列出全部命令：

| 命令 | 做什么 |
|---|---|
| `serve` | 启动常驻服务（Ctrl-C 或 SIGTERM 停止） |
| `login` | 扫码绑定微信 ClawBot |
| `import-credentials` | 接过 ashare-quant 的登录凭证（不用重新扫码） |
| `status` | 看全貌：服务、登录、对话窗口、项目、待处理、提示 |
| `project` | 项目管理：`add` 登记、`list` `show` 查看、`set` 改前缀和条数、`disable` `enable` 停用启用、`remove` 移除、`rotate` 换密钥 |
| `send` | 用项目密钥发一条消息（给脚本用） |
| `alerts` | 告警：配置 Bark 设备码、发测试告警 |
| `doctor` | 逐项检查毛病，有错误退出码 1 |
| `backup` | 备份数据库、登录凭证和配置 |
| `events` | 看最近的事件记录（没有正文） |
| `config` | 配置：显示生效的配置 |
| `version` | 版本号、接口版本、数据库版本 |

### 开发

```
uv run pytest                # 默认跳过 live；默认并行，整套约 70 秒；调试单条加 -n0
uv run ruff format --check && uv run ruff check
uv run python -m clawbot_hub.apidoc --check
```

## 谁能连

服务默认只听本机 `127.0.0.1:8790`，只有本机上的程序连得上。配置 `server.lan: true` 才会监听局域网，这时只接受本机和私有网段（10.x、172.16 到 31.x、192.168.x 等）的连接，其他来源一律 403。**这条路上没有 TLS：局域网监听时项目密钥是明文走局域网的**，所以只在可信的家庭网络里开；不支持公网，路由器里别给它开端口转发。细节见《安装说明》。

## 它不做的事

不代替 Bark（各项目自己发 Bark，服务挂了也不受影响）；不改腾讯的规则（窗口和条数照旧）；不理解你的话（指令怎么认、怎么答在各项目里）；不接 AI、不下单；只认使用者本人。
