# 安全与公开数据边界

## 凭据与运行数据

Telegram、X 数据和模型服务密钥、服务接口 Token、私人看板登录配置均由环境变量注入。`.env.example` 只含占位符。实际 `.env`、数据库、备份、日志、旧托管身份和私人截图不属于公开源码。

不要在 issue、演示截图或提交中上传生产数据库、完整环境变量、服务响应正文或带鉴权参数的 URL。若凭据进入公开仓库或外部日志，应在对应服务中轮换；仅从最新代码删除不能撤回已经泄露的值。整理公开源码不代表已完成生产凭据轮换。

## 公开页面

公开访问默认关闭。`ALPHA_PUBLIC_ENABLED=1` 才开放 `/showcase/` 和 `/api/public/alpha`，允许匿名读取脱敏快照。浏览页面不会重新研究项目、创建后台任务、修改数据库或发送频道消息。

公开结果不包含源频道 ID、原始消息、独立 KOL 身份、内部 Token 或登录配置。研究正文和外链仍须来自可公开使用的材料，请勿混入私人聊天内容。字段投影与 URL 过滤不能代替数据源权限管理。

## 私人访问

`/api/alpha` 使用服务 Bearer Token 或有效网页会话。私人看板使用派生密码哈希、签名会话、HTTPS 安全 Cookie、表单来源及 CSRF 校验；启用公开展示不改变这些鉴权要求。

`ALPHA_WEB_AUTH` 是以下结构的 JSON：

```json
{
  "sessionSecret": "replace_with_random_32_byte_or_longer_hex_secret",
  "passwordSalt": "replace_with_random_16_byte_or_longer_hex_salt",
  "passwordHash": "replace_with_pbkdf2_sha256_600000_round_32_byte_hex_hash",
  "origins": ["https://your-alpha-domain.example"]
}
```

以下脚本在自己的终端中交互读取 HTTPS 来源、新密码及密码确认，生成新的登录配置；它不会读取或修改现有部署凭据。输出也是敏感配置，应只存入部署环境。密码不会回显：

```bash
python scripts/create-dashboard-auth.py
```

将完整 JSON 作为 Railway 的 `ALPHA_WEB_AUTH` 值；本地 shell 格式 `.env` 中，用单引号包裹整段 JSON。不要将其写入前端资源。改域名后更新 `origins`；轮换 `sessionSecret` 会使旧会话失效。

## 部署与报告问题

- 使用 HTTPS 保护实际登录，生产会话 Cookie 为 Secure。
- SQLite 存放在持久卷，备份按私人数据管理。
- Bot 长轮询与 SQLite 以单实例为基准，不直接通过增加副本扩容。
- 不配置不需要的接口密钥，主模型和备用模型分别管理凭据及用量。

一般功能问题可以提交不含私人数据的最小复现。涉及凭据或个人信息时，使用仓库启用的私密安全报告渠道；若没有该渠道，先联系仓库所有者，不要公开粘贴敏感内容。
