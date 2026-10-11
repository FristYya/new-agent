# 第四节数据库准备

应用默认连接 `127.0.0.1:3306` 上已经启动的 MySQL。数据库和表必须在启动应用前准备好；应用只校验连接和表，不执行 DDL。

首次准备时执行：

```bash
mysql -uroot -p < database/001-new-agent.sql
```

建表脚本会创建 `new_agent` 数据库以及五张表：

- `schema_migrations`：记录已经执行的结构版本。
- `users`：逐行保存注册用户、显示名与 scrypt 密码哈希，用户名具有唯一索引。
- `app_documents`：保存第五节可复用的通用 JSON 文档。
- `app_states`：保存完整 AppState 快照。
- `pi_sessions`：保存每条业务会话的完整 Pi entries 会话树。

默认连接参数位于 `src/config.ts`，本机环境不同时可以使用环境变量覆盖。
