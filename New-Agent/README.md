# New-Agent｜Pi SDK 课程脚手架

这个目录只包含课程起点，不包含最终 Agent 后端答案。`public/index.html` 是完整前端；五节课中只累计开发 Node.js/TypeScript Agent 后端。

## 第一次启动

```bash
npm install
cp .env.example .env
npm run dev
```

浏览器访问 `http://localhost:3100`。此时页面可打开，但发送消息等 Agent 接口会返回 `501`，这是正确的起始状态。

## 五节课完成后的能力

1. Pi 模型、系统提示词和 `AgentSession` 生命周期。
2. Web SSE 流式输出、取消和稳定事件协议。
3. Tool、Skill、Extension、工具白名单与人工审批。
4. 原生 Pi 会话树、多会话隔离和持久化恢复。
5. 可观测、评测、结构化结果、MySQL 适配与最终装配。

拜访、CRM 和其他业务事务不属于本课程。后续 Java 服务通过受控 Tool/API 接入，Node.js 层只负责编排 Agent。
