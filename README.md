# Alpha Atlas

**Web3 早期项目监测与 AI 研究平台**

Alpha Atlas 从 Telegram 频道中的 X 新增关注事件提取早期项目线索，将关注聚合、生态识别、资料研究和 Telegram 播报连接起来，并通过网页展示已保存的研究结果。

它关注项目公开资料和关注变化，不要求项目已经发币或公布合约地址。项目简介、项目本人推文、官网资料以及已观察到的生态关注关系都会参与判断，推定和明确自述分别展示。

- [在线展示与实时看板](https://alpha-atlas-zhang.up.railway.app/showcase/)
- [GitHub 源码与部署说明](https://github.com/0xzhang-888/alpha-atlas)
- [技术架构](docs/ARCHITECTURE.md) · [简历项目描述](RESUME.md) · [安全与数据边界](SECURITY.md)

## 核心功能

- **关注信号监测**：解析已有 Telegram 监测频道的关注事件，去重、持久化并聚合独立关注者数量。3 分钟内至少 2 位独立 KOL 关注同一账号时触发快速预警。
- **项目与开发者研究**：查询粉丝数据、识别账号类别，研究官网、文档、项目叙事及开发者近期事件，为结论保留可追溯的来源。
- **动态多链归类**：先做生态预检，再进行深度研究；支持计划上线、测试网及尚未公布 CA 的项目，保留多链和未知状态，不从普通 EVM 地址推断具体链。
- **动态公开演示**：显式启用后，匿名只读接口提供脱敏研究快照，公开看板每 30 秒读取最新结果；访问网页不会发起 AI 研究或发送 Telegram 消息。
- **私人看板**：密码登录和受保护接口保留，公开站与私人访问入口分开。
- **运行成本控制**：异步并发、研究缓存、单账号工具预算、超时和备用模型路由；每轮符合条件的研究账号数量不设上限。

## 技术栈

| 层次 | 实现 |
| --- | --- |
| 事件接入与推送 | Python 3.12、python-telegram-bot |
| 任务调度与编排 | asyncio、APScheduler |
| AI 与资料工具 | DeepSeek V4.1 Flash，经 TeamoRouter 接入；Grok / xAI 备用路由；第三方 X 数据 API、网页及文档读取 |
| 数据与接口 | SQLite、aiohttp |
| 网页 | 原生 HTML、CSS、JavaScript，响应式布局 |
| 部署 | Docker、Railway 持久卷 |
| 验证 | Python unittest、Node.js 数据契约与接口测试 |

本项目接收已有频道提供的关注事件，并结合第三方 X 数据查询进行研究，不直接维护全量 X 关注关系爬虫。数据新鲜度取决于上游事件、外部接口、调度和缓存状态。

## 本地运行

需要 Python 3.12，以及自行配置的 Telegram Bot 和可用数据 / 模型接口。下载源码后运行：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`，至少填写 `BOT_TOKEN`、`SOURCE_CHAT_ID`、`TARGET_CHAT_ID` 和使用中的模型 / 数据服务密钥。将机器人添加到接收事件的源频道，并给予向目标频道发送消息所需的权限。源频道消息格式需能被 `src/parser.py` 识别。

程序从进程环境变量读取配置，没有自动加载 `.env` 的依赖。确认 `.env` 是自己维护的本地配置后，加载并启动：

```bash
set -a
source .env
set +a
python -m src.bot
```

示例默认数据库路径是 `./data/messages.db`。没有监测数据时看板显示空状态；源码不携带生产数据库，不伪造实时记录。

### 主要配置

完整占位符见 [.env.example](.env.example)。

| 变量 | 用途 |
| --- | --- |
| `BOT_TOKEN` | Telegram Bot 凭据 |
| `SOURCE_CHAT_ID` / `TARGET_CHAT_ID` | 接收事件及发送播报的频道 ID |
| `TWITTER_TOKEN` | 第三方 X 资料与推文查询凭据 |
| `FRONTRUN_API_KEY` | 可选的 KOL 关注数量补充数据 |
| `DEEPSEEK_API_KEY` | DeepSeek 兼容接口的凭据 |
| `DEEPSEEK_MODEL` | 默认 `deepseek-flash`，对应 DeepSeek V4.1 Flash |
| `DEEPSEEK_BASE_URL` | 基础分类使用的 Chat Completions API 根地址 |
| `DEEPSEEK_MESSAGES_URL` | 深度研究与生态预检使用的 Messages 完整地址 |
| `TEAMOROUTER_API_KEY` / `XAI_API_KEY` | 可选 Grok / xAI 备用服务，各自使用独立凭据 |
| `DB_PATH` | SQLite 文件位置；云端应位于持久卷 |
| `ALPHA_API_TOKEN` | 私人服务间接口 Bearer Token；配置后启动 HTTP 服务 |
| `ALPHA_WEB_AUTH` | 私人网页登录的密码哈希、会话密钥和允许来源配置 |
| `ALPHA_WEB_DIR` | 私人看板目录；本地设为 `./alpha-atlas/web` |
| `ALPHA_PUBLIC_ENABLED` | 默认 `0`；明确设为 `1` 才开放公开展示和快照 |
| `ALPHA_PUBLIC_DIR` | 公开页面目录；Docker 内为 `/app/showcase`，本地为 `./showcase` |
| `PORT` | HTTP 监听端口，默认 `8080` |
| `TIMEZONE` | 调度时区，默认 `Asia/Shanghai` |
| `QUIET_START_HOUR` / `QUIET_END_HOUR` | 常规汇总静默时段及早间补发时间；快速预警继续接收事件 |

TeamoRouter 的两个协议端点分别配置为：

```dotenv
DEEPSEEK_BASE_URL=https://api.teamorouter.com/v1
DEEPSEEK_MESSAGES_URL=https://api.teamorouter.com/v1/messages
DEEPSEEK_MODEL=deepseek-flash
```

换用其他兼容服务时，应同时验证基础分类、工具调用、联网检索与多轮 Messages 协议；模型名称相同不代表网关支持相同功能。

### 网页与接口

| 路径 | 用途 |
| --- | --- |
| `/showcase/` | 显式启用后，无需登录的项目介绍与动态演示 |
| `/api/public/alpha` | 显式启用后的公开脱敏快照，不触发研究或推送 |
| `/login`、`/` | 私人看板登录与主页 |
| `/api/alpha` | 受会话或服务 Token 保护的研究接口 |
| `/healthz` | 服务健康检查 |

私人网页会话使用 HTTPS 安全 Cookie。本地接口可以走 HTTP，完整登录流程应在本地 HTTPS 反向代理或正式 HTTPS 域名下验证。登录配置生成方式见 [SECURITY.md](SECURITY.md)。

## 测试

后端测试使用合成事件和模拟外部服务，覆盖消息解析、独立关注者计数、研究编排、生态证据、快照接口及登录鉴权：

```bash
python -m unittest discover -s tests -v
```

私人前端的数据契约测试可独立运行，不依赖历史托管平台的构建产物：

```bash
node alpha-atlas/tests/data-contract.mjs
```

## Docker / Railway 部署

仓库提供 `Dockerfile` 和 `railway.json`。Docker 镜像包含 Python 服务、私人看板资源与公开展示页，不依赖旧托管平台的身份文件。

```bash
docker build -t alpha-atlas .
docker volume create alpha-atlas-data
docker run --rm --env-file .env -e DB_PATH=/data/messages.db \
  -p 8080:8080 -v alpha-atlas-data:/data alpha-atlas
```

在 Railway 中从仓库或本地代码部署，配置环境变量，将持久卷挂载到 `/data`，设置 `DB_PATH=/data/messages.db`。健康检查路径为 `/healthz`。有效的 `ALPHA_API_TOKEN` 用于启动 HTTP 监听；公开展示额外需要 `ALPHA_PUBLIC_ENABLED=1`，不会改变私人接口的鉴权。

绑定或更换域名时，同步更新 `ALPHA_WEB_AUTH.origins` 中的 HTTPS 来源。当前 SQLite 与 Bot 长轮询设计适合单实例；多实例扩展前需处理共享存储、重复任务和 Bot 消费冲突。

## 个人贡献与边界

项目作者负责需求定义、关注信号规则、研究口径、证据展示、产品验收和上线迭代，使用 Codex 等 AI 编程工具辅助实现和调试。项目描述不使用未经统计的效率提升、投资收益或早期发现成功率。

输出用于整理公开信息。关注增加、生态推定和模型研究结论均不等于项目背书；页面保留证据、时间和资料不足状态。
