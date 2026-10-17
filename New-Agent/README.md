# New-Agent

这是 Pi SDK Agent 课程项目。当前分支在会话、Skill/Tool、审批和持久化基础上，增加了真实运行观测与评测治理闭环。

## 观测与评测

- 聊天运行自动生成 Run、Trace、Tool 生命周期、Token、成本、TTFT、重试和告警记录。
- 支持确定性评测、数据集版本、Evaluation Profile、基线对比、发布门禁和人工复核。
- `agent_task` 可以通过隔离的 Pi Agent Runner 真实执行，也可以从观测 Run 导入。
- 评测报告按运行冻结并支持 Markdown 导出。
- 运行时边界和数据隔离见 `docs/production-runtime.md`。

## 第四节完成内容

- 默认直连本机 MySQL，不需要额外启动脚本，也不会在启动时询问密码。
- 基础表由 `database/001-new-agent.sql` 创建，观测行存储由 `database/002-observability.sql` 创建。
- 应用启动时只校验连接和表是否存在，不执行建库、建表或结构迁移。
- `app_states` 持久化 Agent、Skill、Tool、会话列表、展示消息和审批记录。
- `pi_sessions` 持久化 Pi SDK 的完整 entries 会话树，服务重启后可以续聊。
- 每个业务会话绑定独立 Pi Session，切换会话不会串上下文。
- 保留 `file` 回退模式，使用原子 JSON 写入和 Pi JSONL Session。
- 提供 `/health/live` 存活探针、`/health/ready`（及兼容别名 `/health`）存储就绪探针，以及会话修改、删除接口。
- 使用成品前端已有的登录、注册和退出页面，不再自动注入 mock 用户。
- 注册密码使用带随机 salt 的 `scrypt` 哈希保存，登录状态使用签名 `HttpOnly` Cookie。
- 每个用户使用自己的 `user.id` 作为 owner，隔离 AppState、Pi Session、审批和运行时。
- `server.ts` 只保留 HTTP/SSE 控制器与依赖组装；用户运行时和 Pi Session 生命周期拆到 `src/runtime/`。

## 登录注册与 owner

首次打开页面时先注册账号，注册成功后会自动登录。MySQL 模式把每个账号单独保存在 `users` 表中；file 回退模式则写入 `data/auth-users.json`。

登录成功后，服务端从签名 Cookie 中恢复用户，并将 `user.id` 传给所有存储入口：

```text
user.id
  ├─ app_states.owner_key
  ├─ pi_sessions.owner_id
  └─ 独立 RuntimeContext（Pi Session + ApprovalRegistry）
```

Cookie 不保存密码。正式部署时必须通过 `AUTH_SECRET` 覆盖课堂默认签名密钥，并在 HTTPS 环境设置 `AUTH_COOKIE_SECURE=true`。

## 课前准备数据库

确保本机 MySQL 已经启动，然后只需执行一次：

```bash
mysql -uroot -p < database/001-new-agent.sql
mysql -uroot -p new_agent < database/002-observability.sql
```

核心表的用途：

- `schema_migrations`：记录数据库结构版本。
- `users`：保存注册用户、显示名和 scrypt 密码哈希，使用唯一索引约束用户名。
- `app_documents`：为第五节评测配置、数据集等通用文档预留持久化入口。
- `app_states`：保存完整 AppState 快照。
- `pi_sessions`：保存完整 Pi entries 会话树。
- `observability_runs`：逐行保存 Run 快照，支持按用户和时间索引。
- `observability_events`：逐行保存 Trace/Tool 事件。
- `observability_alerts`：逐行保存可确认的运行告警。

## 启动与验证

```bash
npm install
npm run typecheck
npm test
npm start
```

浏览器访问 `http://localhost:3100`。存活和就绪检查地址分别为 `http://localhost:3100/health/live` 与 `http://localhost:3100/health/ready`。

正式部署需设置 `NODE_ENV=production`；服务会在存储、数据库密码、Cookie 密钥或 Secure
Cookie 仍为开发配置时拒绝启动，具体要求见 `docs/production-runtime.md`。

课堂默认连接参数位于 `src/config.ts`。没有 `.env` 时直接使用这些默认值；如果学员机器的连接信息不同，可以参考 `.env.example` 使用环境变量覆盖。设置 `NEW_AGENT_STORAGE=file` 可以临时切换到文件存储。

## 第四节新增关键文件

```text
database/001-new-agent.sql
database/002-observability.sql
database/README.md
src/storage/mysql.ts
src/auth-users.ts
src/runtime/runtime-context.ts
src/runtime/session-storage.ts
src/runtime/session-runtime.ts
tests/lesson-04-auth.test.ts
tests/lesson-04.test.ts
```

前三节的 `config.ts`、`store.ts` 和 `server.ts` 继续在原文件上递增，没有另建 Demo。第四节只按职责拆出可复用模块，不改变路由地址和前端调用方式。
