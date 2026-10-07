# New-Agent

这是五节 Pi SDK Agent 课程中第二节完成后的代码。它在第一节“模型、AgentSession、Web SSE 与取消”基础上，继续加入 Skill、Tool、运行时白名单和管理接口。

## 当前已完成

- 初始化 Pi `ModelRuntime` 并选择可用模型
- 使用 `AgentSession` 完成流式对话与取消
- 使用内存 `store.ts` 管理 Agent、Skill、Tool 和会话
- 加载 `skills/current-time/SKILL.md`
- 将 `skill.json` 中的脚本适配成 Pi Tool
- 根据 Agent、Skill 和 Tool 状态计算执行白名单
- 通过 SSE 推送 `tool_start`、`tool_update`、`tool_end`
- 提供 Agent、Skill、Tool 的 CRUD 接口

会话树、磁盘持久化、安全审批、可观测和评测属于后续课程，本节暂不实现。

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

## 第二节关键文件

```text
src/config.ts
src/server.ts
src/skill-tools.ts
src/store.ts
skills/current-time/SKILL.md
skills/current-time/skill.json
skills/current-time/tools/current-time.mjs
public/index.html
```

`conversation-store.ts` 和 `session-runtime.ts` 是第一节的临时拆分文件，第二节完成后不再保留。
