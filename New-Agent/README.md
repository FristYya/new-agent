# New-Agent

这是五节 Pi SDK Agent 课程中第三节完成后的代码。它在第二节“Skill、Tool 装配”基础上，继续加入 Tool 风险分级、Pi guard 扩展、人工审批、一次性 token 和日志脱敏。

## 当前已完成

- 初始化 Pi `ModelRuntime` 并选择可用模型
- 使用 `AgentSession` 完成流式对话与取消
- 使用内存 `store.ts` 管理 Agent、Skill、Tool 和会话
- 加载 `skills/current-time/SKILL.md`
- 将 `skill.json` 中的脚本适配成 Pi Tool
- 根据 Agent、Skill 和 Tool 状态计算执行白名单
- 通过 SSE 推送 `tool_start`、`tool_update`、`tool_end`
- 提供 Agent、Skill、Tool 的 CRUD 接口
- 通过 `tool_call` guard 拦截越权、写入和破坏性 Tool
- 使用一次性审批 token 连接后端等待任务与前端决定
- 审批超时、运行取消时自动拒绝待执行 Tool
- 对审批参数、日志和错误消息做递归脱敏
- 通过 SSE 推送 `approval_required`，并在聊天区完成批准或拒绝

会话树、磁盘持久化、可观测和评测属于后续课程，本节暂不实现。

## 启动

```bash
npm install
npm run typecheck
npm test
npm run dev
```

浏览器访问 `http://localhost:3100`。

启动前需要先完成 Pi 模型与凭据配置。可选环境变量包括：

- `PORT`：HTTP 端口，默认 `3100`
- `AGENT_MODEL`：指定 `model-id` 或 `provider/model-id`
- `SKILL_TIMEOUT_MS`：Skill Tool 执行超时
- `SKILL_MAX_INPUT_BYTES`：Skill Tool 最大输入字节数
- `SKILL_MAX_OUTPUT_BYTES`：Skill Tool 最大输出字节数
- `APPROVAL_TIMEOUT_MS`：人工审批最长等待时间，默认 5 分钟

## 第三节新增关键文件

```text
src/policy/tool-policy.ts
src/runtime/approval-registry.ts
src/security/redaction.ts
src/extensions/guard.ts
src/extensions/audit.ts
public/index.html
tests/lesson-03.test.ts
```

第二节的 `config.ts`、`store.ts` 和 `server.ts` 继续在原文件上递增，不创建临时 Demo。
