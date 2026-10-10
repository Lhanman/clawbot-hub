# clawbot-hub

微信 ClawBot 共用服务：一个常驻程序独占 ClawBot 的登录，多个项目通过本机接口一起发消息、收指令；24 小时 10 条的额度、一句话交给哪个项目、登录失效和窗口到期的提醒，都由它统一管。

**状态（2026-10-10）：规格阶段。** 设计方案、协议调研、第一个变更 `h1-hub-v1` 的规格和测试清单已经写好，使用者点头以后才写代码。

## 看哪里

| 想知道 | 看 |
|---|---|
| 为什么做、做成什么样、各项目怎么接 | `docs/specs/2026-10-09-ClawBot共用服务-设计方案.md` |
| 腾讯 iLink 协议和已知的规则、没弄清的地方 | `docs/research/2026-10-09-ClawBot协议与规则调研.md` |
| 第一个变更要做的事（提案、设计、任务单、九份规格） | `openspec/changes/h1-hub-v1/` |
| 约定（语言、测试、密钥、不做什么） | `CLAUDE.md` |

## 预定的用法（代码写完才有）

```
uv sync
hubctl login                                   # 扫码绑定 ClawBot（只在服务这里扫）
hubctl project add ashare-quant --prefix 量化 --guaranteed 6 --cap 8   # 登记一个项目，打印一次密钥
hubctl serve                                   # 常驻（launchd 里跑）
hubctl status                                  # 登录、窗口、额度、各项目在不在线
```

各项目用密钥调本机接口 `http://127.0.0.1:8790/v1/…`；Python 项目直接拷一个只依赖标准库的单文件客户端 `clawbot_hub_client.py`。

## 它不做的事

不代替 Bark（各项目自己发 Bark，服务挂了也不受影响）；不改腾讯的规则（窗口和条数照旧）；不理解你的话（指令怎么认、怎么答在各项目里）；不接 AI、不下单；只认使用者本人。
