# clawbot-hub

微信 ClawBot 共用服务。开始任何工作前先读设计方案 `docs/specs/2026-10-09-ClawBot共用服务-设计方案.md` 和当前变更 `openspec/changes/h1-hub-v1/`（提案、设计、任务单、九份规格）。腾讯协议和已知规则的调研在 `docs/research/`。

## 约定

- 和使用者用中文交流；文档用中文，文档文件名用中文标题式命名；代码标识符用英文，代码注释用中文。
- 使用者 2026-10-10 批准的方向（设计方案第 1 节，不要推翻，要改先和使用者确认）：只有服务登录 ClawBot、收消息；各项目通过本机接口收发；Bark 不经过服务，仍是主渠道和兜底；10 条额度是腾讯的，服务只分、加不出来；代码在独立仓库，不属于任何项目。
- 来源项目 `ashare-quant`（Lhanman/ashare-quant）里的微信推送和问答是第一个使用者。它的 `src/aq/notify/clawbot.py`、`notify/http.py`、`notify/base.py`、`chat/bot.py` 是现成的协议和用法参考：搬代码时先看规格和设计方案第 14 节——规格写了的按规格做（拆条预算、额度算法、重试、窗口记账这几处有意和它不同，别为了和旧代码一致改回去）；规格没写的，保留它的行为（请求写法、脱敏、错误文案），不要凭空改，也不要自己添新规矩。
- 在使用者点头规格和测试清单之前不写服务代码；批准之后每个阶段单独验收。**任何时候都不动任何项目的代码，也不动 ClawBot 的绑定**，除非使用者在对应的步骤上明确说做（切换那天的步骤写在 `docs/ops/`，由 Mac 上的线程执行）。
- 测试先行；单元测试不联网：pytest 带套接字守卫，只放行本机回环；腾讯接口一律用假的（`tests/support/fake_ilink.py`）；`live` 标记的真机测试默认不跑。
- 登录凭证、项目密钥、Bark 密钥、数据库不进 git（见 `.gitignore`）；日志和报错里不出现凭证、密钥、消息正文。
- 服务不理解消息内容，不接 AI，不下单，不碰任何项目的业务；只认使用者本人发的消息。
- 条数、时间、端口这类可调的数一律进配置并给默认值，不写死在代码里。
- 真实环境是家里的 Mac mini（launchd 常驻）。安装、导入凭证、切换在 Mac 上做；开发和测试可以在云里。

## 常用命令

- `uv sync`：安装依赖（Python ≥ 3.12，锁文件 `uv.lock`）
- `uv run pytest`：单元测试，默认跳过 `live`，也不允许联网；默认最多 4 个进程并行（整套约 70 秒，单进程要 4 分多钟），调试单条加 `-n0`
- `uv run pytest -m live`：真机测试，访问真实的腾讯接口、占用真账号的额度，**要使用者在场授权后才能跑**（做法见 `docs/ops/真机验证清单.md`）
- `uv run ruff format` / `uv run ruff check`：格式化、静态检查（只检查不改用 `ruff format --check`）
- `uv run python -m clawbot_hub.apidoc`：把接口描述导出到 `docs/api/openapi.json`，加 `--check` 只核对；改了接口没重新导出，测试会红
- `uv run python -m tests.support.fake_ilink --port <端口>`：独立运行的假腾讯（演练和联调用，`/_fake/` 下有控制接口）
- `uv run hubctl --help`：命令行入口，每个命令都认 `--home`（数据目录）和 `--config`
  - `serve`：常驻服务（`SIGTERM` / Ctrl-C 正常停；也可以 `python -m clawbot_hub serve`），launchd 模板在 `deploy/launchd/`
  - `login`（扫码绑定）、`import-credentials`（接过 ashare-quant 的凭证，导入前先停服务）
  - `project add|list|show|set|disable|enable|remove|rotate`、`send`（用项目密钥发一条，给脚本用）
  - `status`（看全貌）、`doctor`（逐项检查，`--fix` 修权限，`--online` 联网查）、`events`、`backup`、`alerts set-key|test`、`config show`、`version`
- 数据目录默认 `~/.clawbot-hub`，用 `CLAWBOT_HUB_HOME` 或 `--home` 改；配置文件 `<数据目录>/config.yaml`。演练一律用单独的数据目录和端口，见 `docs/ops/安装说明.md`
