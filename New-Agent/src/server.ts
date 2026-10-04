import express from "express";
import { randomUUID } from "node:crypto";
import {
  appendMessage,
  createConversation,
  getConversation,
  listConversations,
} from "./conversation-store.js";
import {
  abortConversation,
  disposeAllSessions,
  getActiveModelLabel,
  getOrCreateSession,
} from "./session-runtime.js";

// 创建 Express 应用；端口允许通过 .env 中的 PORT 覆盖。
const app = express();
const port = Number(process.env.PORT || 3100);

// runId 标识一次生成任务；conversationId 标识长期会话。
// 停止按钮先用 runId 找到会话，再中止该会话正在进行的生成。
const activeRuns = new Map<string, { conversationId: string }>();

// 隐藏框架标识，解析 JSON 请求体，并托管 public 目录中的前端资源。
app.disable("x-powered-by");
app.use(express.json({ limit: "64kb" }));
app.use(express.static("public"));

// 写入一个完整 SSE 事件。
// 协议格式必须是 event 行 + data 行 + 空行；空行代表当前事件结束。
function writeSse(
  response: express.Response,
  event: string,
  data: unknown,
): void {
  // 单个换行结束 event 字段。
  response.write("event: " + event + "\n");
  // 两个换行中的第二个换行形成空行，浏览器才能立即解析这一帧。
  response.write("data: " + JSON.stringify(data) + "\n\n");
}

// 第一节使用固定演示用户，避免登录业务干扰 Agent 主线。
app.get("/api/auth/me", (_request, response) => {
  response.json({
    user: { id: "course-user", username: "student", displayName: "课程学员" },
  });
});

// 页面启动时读取整体状态。
// 尚未教学的模块返回稳定空结构，让完整版前端也能正常渲染。
app.get("/api/state", (_request, response) => {
  response.json({
    agents: [],
    skills: [],
    tools: [],
    knowledge: [],
    conversations: listConversations(),
    tasks: [],
    observability: { runs: [] },
    runtime: {
      model: getActiveModelLabel(),
      busy: activeRuns.size > 0,
      status: activeRuns.size > 0 ? "running" : "idle",
    },
  });
});

// 完整前端会请求可观测性接口；第一节先返回约定好的空数据结构。
app.get("/api/observability", (_request, response) => {
  response.json({
    overview: {},
    runs: [],
    alerts: [],
    skillStats: [],
    toolStats: [],
    trends: [],
  });
});

// 评测工作台同样保留空结构，避免页面出现 404 或字段缺失错误。
app.get("/api/evals", (_request, response) => {
  response.json({
    summary: { totalCases: 0 },
    cases: [],
    datasets: [],
    runs: [],
    jobs: [],
    gate: { minPassRate: 1, maxRegressions: 0 },
  });
});

// 创建应用会话；Pi AgentSession 会在第一次真正聊天时懒加载。
app.post("/api/conversations", (request, response) => {
  const title = String(request.body?.title || "新会话").trim() || "新会话";
  response.status(201).json(createConversation(title));
});

// 查询单个会话，用于切换左侧历史记录并恢复消息展示。
app.get("/api/conversations/:id", (request, response) => {
  const conversation = getConversation(request.params.id);
  if (!conversation) {
    return response.status(404).json({ error: "会话不存在" });
  }
  response.json(conversation);
});

// 取消指定 runId 对应的当前生成。
app.post("/api/runs/:runId/cancel", async (request, response) => {
  const run = activeRuns.get(request.params.runId);
  if (!run) {
    return response.status(404).json({ error: "运行不存在或已经结束" });
  }

  await abortConversation(run.conversationId);
  response.json({ ok: true });
});

// 核心聊天接口：接收 JSON，返回 text/event-stream。
app.post("/chat", async (request, response) => {
  // trim 防止只包含空格的消息进入模型。
  const message = String(request.body?.message || "").trim();
  const conversationId = String(request.body?.conversationId || "").trim();

  if (!message) {
    return response.status(400).json({ error: "message 不能为空" });
  }
  if (!conversationId) {
    return response.status(400).json({ error: "conversationId 不能为空" });
  }

  // 应用会话必须先存在，否则不能建立展示记录与 Pi Session 的关联。
  const conversation = getConversation(conversationId);
  if (!conversation) {
    return response.status(404).json({ error: "会话不存在" });
  }

  // 在写 SSE 响应头之前初始化 Session。
  // 这样模型配置错误仍能返回普通 JSON 错误，前端更容易处理。
  let session;
  try {
    session = await getOrCreateSession(conversationId);
  } catch (error) {
    const text = error instanceof Error ? error.message : "会话初始化失败";
    return response.status(500).json({ error: text });
  }

  const runId = randomUUID();
  let clientClosed = false;
  let assistantText = "";

  // 先登记运行并保存用户消息，随后再开始向客户端发送流。
  activeRuns.set(runId, { conversationId });
  appendMessage(conversationId, "user", message);

  // 明确声明 SSE，并关闭代理缓冲。
  response.writeHead(200, {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache, no-transform",
    Connection: "keep-alive",
    "X-Accel-Buffering": "no",
  });
  // 立即发送响应头，不等待第一个模型 token。
  response.flushHeaders?.();

  // 先把本次 runId、conversationId 和运行状态同步给页面。
  writeSse(response, "run", { id: runId });
  writeSse(response, "conversation", { id: conversationId });
  writeSse(response, "status", { state: "running", label: "思考中" });

  // 一定要在 session.prompt 之前订阅，否则快速返回时可能错过首个 delta。
  const unsubscribe = session.subscribe((event) => {
    // Pi 的 message_update 中包含多种子事件；这里只转发文本增量。
    if (
      event.type === "message_update" &&
      event.assistantMessageEvent?.type === "text_delta"
    ) {
      const delta = event.assistantMessageEvent.delta || "";

      // 后端累计完整文本，用于结束后写入会话展示存储。
      assistantText += delta;

      // 客户端仍在线时，把当前增量立即发送，而不是等待完整回答。
      if (!clientClosed) {
        writeSse(response, "text", { delta });
      }
    }
  });

  // 浏览器关闭、刷新或 AbortController 取消 fetch 时会触发 close。
  response.on("close", () => {
    // 正常 response.end 也会触发 close；只处理中途断开的情况。
    if (!response.writableEnded) {
      clientClosed = true;
      // close 回调不能 await，因此显式忽略 Promise，但仍发起中止。
      void session.abort();
    }
  });

  try {
    // prompt 会等待本轮 Agent 完整结束；文本增量通过上面的订阅实时发送。
    await session.prompt(message);

    // 正常结束后把累计文本写入页面投影，便于切换会话后恢复。
    const finalText = assistantText.trim() || "模型本轮没有返回文本。";
    appendMessage(conversationId, "assistant", finalText);

    if (!clientClosed) {
      writeSse(response, "status", { state: "idle", label: "空闲" });
      writeSse(response, "done", { runId });
    }
  } catch (error) {
    // 只有连接仍然存在时才发送错误事件。
    if (!clientClosed) {
      const text = error instanceof Error ? error.message : "Agent 执行失败";
      writeSse(response, "error", { message: text });
    }
  } finally {
    // 无论成功、失败还是取消，都必须清理监听器和运行表。
    unsubscribe();
    activeRuns.delete(runId);

    // 中途断开的连接无需再次 end；正常连接主动结束 SSE 响应。
    if (!clientClosed) response.end();
  }
});

// 启动 HTTP 服务。
const server = app.listen(port, () => {
  console.log("New-Agent 第一节：http://localhost:" + port);
});

// 收到退出信号时先释放 Session，再关闭 HTTP 服务。
function shutdown(signal: string): void {
  console.log("收到 " + signal + "，正在释放会话资源");
  disposeAllSessions();
  server.close(() => process.exit(0));
}

// Ctrl+C 与容器停止分别对应 SIGINT 和 SIGTERM。
process.once("SIGINT", () => shutdown("SIGINT"));
process.once("SIGTERM", () => shutdown("SIGTERM"));