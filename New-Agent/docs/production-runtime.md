# 观测与评测运行时

## 数据边界

观测和评测数据按登录用户隔离。MySQL 模式下，评测治理状态存储在带版本号的
`app_documents`，所有 read-modify-write 使用乐观锁；高频观测数据分别逐行写入
`observability_runs`、`observability_events` 和 `observability_alerts`。file 模式写入
`data/documents/<userId>/` 并使用进程内串行化和原子替换。Run 最多保留 500 条，
旧 Run 在 MySQL 中删除时由外键级联清理事件和告警。

所有输入、输出、Tool 参数和错误在进入观测存储前都会通过 `safePreview()` 脱敏并
截断。Pi Session 的完整上下文仍只保存在 `pi_sessions`，观测数据不能用于恢复会话。

## 观测采集

`POST /chat` 创建 Run，然后把同一个 Pi `AgentSession` 的事件同时发送给 SSE 翻译器
和观测记录器。记录器采集首 Token、模型 usage、重试以及 Tool 生命周期。纯提示词
Skill 只有“已加载”记录，不宣称模型实际调用过它。

观测故障不会阻断聊天主链路；启动记录失败时服务会写错误日志并继续生成。
Run 查询支持 `status`、`query`、`from`、`to`、`limit` 和 `offset`，返回分页元数据。
失败、单次耗时超限和单次成本超限会生成告警；阈值可通过
`OBSERVABILITY_ALERT_DURATION_MS` 与 `OBSERVABILITY_ALERT_COST_USD` 配置。
运行结束时仍未闭合的 Tool 调用会自动补写 timeout/aborted/failed 终止事件并触发告警，
避免 Trace 永久显示为 running。Provider 返回的 usage 会先做有限非负数校验。

## 评测执行

确定性用例由 `src/evals/evaluator.ts` 执行。运行时会冻结用例、数据集、Profile 和
目标快照，随后计算普通通过率、加权通过率、分类结果、基线差异及发布门禁。

Agent Runner 使用独立的内存 `SessionManager`，不会读写业务会话。出于安全考虑，
当前只开放 `read`、`grep`、`find`、`ls` 四个只读内置 Tool。每个用例运行结束后会
销毁 Session，并把输出、Tool 轨迹、状态和延迟作为 `agent_task` 证据。

确定性任务和 Agent Runner 都通过异步 Job 执行，payload、尝试次数、租约和取消状态
会持久化；服务启动及周期扫描时会重新领取过期任务，运行中通过心跳续租。Agent Runner
使用跨实例租约阻止同一用户并发执行，取消任务时会中止当前隔离 Session。当前 Worker
仍与 Web 服务同进程部署，生产扩缩容前应结合容量目标决定是否拆为独立 Worker。
Job 会记录阶段、当前用例、完成数量和百分比。用例、数据集、Profile、门禁、基线与
人工复核的变更会写入仅追加的审计记录，保存操作者、时间和变更前后内容哈希。
新审计记录还包含前序哈希与自身哈希，可通过接口验证内容和顺序是否被意外改写；这不是
外部不可变审计仓库的替代品，高合规场景仍应把审计事件同步到独立 SIEM/WORM 存储。
单次运行用例数、每用户用例总数和 Agent 证据输出长度分别由
`EVAL_MAX_CASES_PER_RUN`、`EVAL_MAX_STORED_CASES`、`EVAL_MAX_OUTPUT_CHARS` 限制；
单个用例配置不得超过 64KB，避免异常请求造成内存和文档存储膨胀。

## 生产启动与探针

设置 `NODE_ENV=production` 后，服务会拒绝使用 file 存储、默认数据库密码、默认或少于
32 字符的 Cookie 签名密钥，以及未开启 Secure 的 Cookie。`GET /health/live` 只检查进程
是否能响应；`GET /health/ready` 检查存储连接与必需表，原 `/health` 保持为就绪探针别名。

## API

- `GET /api/observability`：聚合指标、Run、Trace、告警和 Tool/Skill 统计。
- `GET /api/observability/runs`：搜索及筛选 Run。
- `POST /api/evals/agent-runner`：执行真实 Agent 回归。
- `POST /api/evals/import-run/:runId`：从观测 Run 创建待复核用例。
- `GET /api/evals/audit/verify`：验证评测治理审计哈希链。
- `GET /api/evals/runs/:id/report.md`：导出冻结的 Markdown 报告。

其余用例、数据集、Profile、门禁、Job、基线和复核接口见 `src/server.ts`。
