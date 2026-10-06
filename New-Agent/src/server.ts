// 完整代码
import express from "express";
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import path from "node:path";
import {
  createAgentSession,
  DefaultResourceLoader,
  getAgentDir,
  loadSkills,
  ModelRuntime,
  SessionManager,
  type Skill,
} from "@earendil-works/pi-coding-agent";
import { runtimeConfig } from "./config.ts";
import {
  createSkillToolDefinitions,
  discoverSkillPackageTools,
} from "./skill-tools.ts";
import {
  createRecord,
  deleteRecord,
  readState,
  updateRecord,
  writeState,
  type AgentRecord,
  type AppState,
  type ConversationRecord,
  type SkillRecord,
  type ToolRecord,
} from "./store.ts";

// 第二节只开放只读的 Pi 内置 Tool。
// bash、write、edit 等高风险 Tool 会在后续“安全审批”课程中接入。
const CORE_TOOLS = ["read", "grep", "find", "ls"];

// -----------------------------------------------------------------------------
// 1. 初始化模型
// -----------------------------------------------------------------------------

// ModelRuntime 负责读取 Pi 的模型与凭据配置。
// 它在服务启动时只创建一次，后续所有 AgentSession 共用这个运行时。
const modelRuntime = await ModelRuntime.create();
const availableModels = await modelRuntime.getAvailable();

if (availableModels.length === 0) {
  throw new Error("没有可用模型，请先完成 Pi 模型与凭据配置");
}

// AGENT_MODEL 同时支持 model-id 和 provider/model-id 两种格式。
const requestedModel = process.env.AGENT_MODEL?.trim();
const model = requestedModel
  ? availableModels.find(
      (item) =>
        item.id === requestedModel ||
        `${item.provider}/${item.id}` === requestedModel,
    )
  : availableModels[0];

if (!model) {
  throw new Error(`指定模型不可用：${requestedModel}`);
}

// 保存已经完成空值检查的模型，供后面的异步路由闭包稳定复用。
const selectedModel = model;
const modelLabel = `${selectedModel.provider}/${selectedModel.id}`;
console.log(`New-Agent 使用模型：${modelLabel}`);

// -----------------------------------------------------------------------------
// 2. 运行时结构与 Skill/Tool 策略
// -----------------------------------------------------------------------------

type RuntimeContext = {
  // ResourceLoader 负责把本节启用的 Skill 注入 Pi。
  loader: DefaultResourceLoader;
  // 第二节暂时只维护一个进程内 Session；会话树和持久化留到后续课程。
  session?: Awaited<ReturnType<typeof createAgentSession>>["session"];
  // 当前允许执行的 Tool 名称集合。
  runtimeAllowedTools: Set<string>;
  // 当前真正交给 Pi 的 Skill。
  runtimePiSkills: Skill[];
  // Skill 加载诊断信息，供管理页排查配置问题。
  runtimeSkillDiagnostics: unknown[];
  // 当前 PROD Skill 实际注册的 Tool 名称。
  runtimeSkillToolNames: Set<string>;
  // 所有 Skill Package 拥有的 Tool 名称，用于阻止绕过 Skill 状态直接启用。
  runtimeSkillOwnedTools: Set<string>;
};

// 相对路径统一以项目根目录为基准；绝对路径保持不变。
function resolveSkillPath(installPath: string): string {
  return path.isAbsolute(installPath)
    ? installPath
    : path.join(process.cwd(), installPath);
}

// 获取本次运行所使用的 Agent。
// 优先使用会话绑定的 agentId，否则使用 ACTIVE Agent，最后回退到第一条记录。
function getActiveAgent(state: AppState, agentId?: string): AgentRecord | undefined {
  return (
    state.agents.find((item) => item.id === agentId) ??
    state.agents.find((item) => item.status === "ACTIVE") ??
    state.agents[0]
  );
}

// Skill 必须同时满足两个条件：
// 1. 被当前 Agent 的 skillIds 引用；
// 2. 自身处于 PROD 状态。
function getEnabledSkillRecords(
  state: AppState,
  agentId?: string,
): SkillRecord[] {
  const agent = getActiveAgent(state, agentId);
  const enabledSkillIds = new Set(agent?.skillIds ?? []);

  return state.skills.filter(
    (skill) => skill.status === "PROD" && enabledSkillIds.has(skill.id),
  );
}

// 把管理状态中的 SkillRecord 转成 Pi SDK 可以读取的 Skill。
function buildRuntimePiSkills(state: AppState, agentId?: string) {
  const skillPaths = getEnabledSkillRecords(state, agentId)
    .map((skill) => resolveSkillPath(skill.installPath));

  return loadSkills({
    cwd: process.cwd(),
    agentDir: getAgentDir(),
    skillPaths,
    // 只加载 AppState 明确启用的 Skill，不扫描其他默认目录。
    includeDefaults: false,
  });
}

// 计算模型本轮真正可以调用的 Tool。
function getExecutableTools(
  ctx: RuntimeContext,
  state: AppState,
  agentId?: string,
): string[] {
  const agent = getActiveAgent(state, agentId);
  const agentToolNames = new Set(agent?.toolNames ?? []);

  // 普通 Tool 必须是 ENABLED，并且被当前 Agent 的 toolNames 引用。
  const enabledDirectTools = new Set(
    state.tools
      .filter(
        (tool) =>
          tool.status === "ENABLED" && agentToolNames.has(tool.name),
      )
      .map((tool) => tool.name),
  );

  // Skill 自带 Tool 不能通过 Agent.toolNames 绕过 Skill 的 PROD 状态。
  const enabledBaseTools = new Set(
    [...enabledDirectTools].filter(
      (toolName) => !ctx.runtimeSkillOwnedTools.has(toolName),
    ),
  );

  // 只有当前 Agent 启用的 PROD Skill 才能贡献 Tool。
  const enabledSkillTools = new Set(
    getEnabledSkillRecords(state, agentId)
      .flatMap((skill) => skill.toolNames),
  );

  // 最后与真实注册表求交集，配置中写错的 Tool 名称不会暴露给模型。
  const registeredTools = new Set([
    ...CORE_TOOLS,
    ...ctx.runtimeSkillToolNames,
  ]);

  return [...registeredTools].filter(
    (toolName) =>
      enabledBaseTools.has(toolName) || enabledSkillTools.has(toolName),
  );
}

// -----------------------------------------------------------------------------
// 3. 创建 ResourceLoader 与 AgentSession
// -----------------------------------------------------------------------------

function readSystemPrompt(): string {
  return readFileSync(
    path.join(process.cwd(), "prompts", "new-agent.md"),
    "utf-8",
  );
}

function createRuntimeContext(): RuntimeContext {
  // 先创建除 loader 以外的可变上下文。
  const ctx = {
    runtimeAllowedTools: new Set<string>(CORE_TOOLS),
    runtimePiSkills: [],
    runtimeSkillDiagnostics: [],
    runtimeSkillToolNames: new Set<string>(),
    runtimeSkillOwnedTools: new Set<string>(),
  } as unknown as RuntimeContext;

  ctx.loader = new DefaultResourceLoader({
    cwd: process.cwd(),
    agentDir: getAgentDir(),
    // 禁止默认扫描 Skill；本节只接受 skillsOverride 提供的结果。
    noSkills: true,
    // 沿用第一节的系统提示词。
    systemPromptOverride: () => readSystemPrompt(),
    appendSystemPromptOverride: () => [],
    // 回调读取 ctx 的最新值，因此配置更新后 reload() 即可生效。
    skillsOverride: () => ({
      skills: ctx.runtimePiSkills,
      diagnostics: ctx.runtimeSkillDiagnostics as any[],
    }),
  });

  return ctx;
}

const runtime = createRuntimeContext();

// 根据最新 AppState 重算 Skill、Tool 和 ResourceLoader 状态。
async function applyRuntimePolicy(
  ctx: RuntimeContext,
  state: AppState,
  agentId?: string,
): Promise<string[]> {
  // 扫描所有 Skill，用于识别每个 Tool 属于哪个 Skill。
  const allSkillTools = await discoverSkillPackageTools(state.skills);
  // 只有当前 Agent 启用的 PROD Skill 才能参与实际执行。
  const activeSkillTools = await discoverSkillPackageTools(
    getEnabledSkillRecords(state, agentId),
  );

  ctx.runtimeSkillOwnedTools = new Set(
    allSkillTools.map((item) => item.manifest.name),
  );
  ctx.runtimeSkillToolNames = new Set(
    activeSkillTools.map((item) => item.manifest.name),
  );

  const runtimeSkills = buildRuntimePiSkills(state, agentId);
  ctx.runtimePiSkills = runtimeSkills.skills;
  ctx.runtimeSkillDiagnostics = runtimeSkills.diagnostics;

  // 让 ResourceLoader 重新读取本轮计算出的 Skill。
  await ctx.loader.reload();

  const executableTools = getExecutableTools(ctx, state, agentId);
  ctx.runtimeAllowedTools = new Set(executableTools);
  return executableTools;
}

async function createNewAgentSession(
  ctx: RuntimeContext,
  state: AppState,
  conversation: ConversationRecord,
) {
  const executableTools = await applyRuntimePolicy(
    ctx,
    state,
    conversation.agentId,
  );

  // 注册全部可信 Skill Tool。
  // 是否允许模型调用，仍由 executableTools 白名单决定。
  const allSkillTools = await discoverSkillPackageTools(state.skills);
  const customTools = createSkillToolDefinitions(allSkillTools);
  const allToolNames = [
    ...new Set([
      ...CORE_TOOLS,
      ...allSkillTools.map((item) => item.manifest.name),
    ]),
  ];

  const { session } = await createAgentSession({
    cwd: process.cwd(),
    model: selectedModel,
    modelRuntime,
    resourceLoader: ctx.loader,
    // 第二节只要求进程内上下文；持久化 Session 在后续课程实现。
    sessionManager: SessionManager.inMemory(process.cwd()),
    customTools,
    tools: allToolNames,
  });

  // 完成扩展绑定后，再应用当前 Agent 的最终 Tool 白名单。
  await session.bindExtensions({ mode: "print" });
  session.setActiveToolsByName(executableTools);
  return session;
}

// Agent、Skill 或 Tool 配置变化后销毁旧 Session。
// 下一条消息会根据最新配置懒加载一个新 Session。
function resetSession(reason: string): void {
  console.log(`重建 Session：${reason}`);
  runtime.session?.dispose();
  runtime.session = undefined;
}

// -----------------------------------------------------------------------------
// 4. Express、SSE 与通用辅助函数
// -----------------------------------------------------------------------------

const app = express();
const port = runtimeConfig.port;

// runId 标识一次生成任务；conversationId 标识长期业务会话。
const activeRuns = new Map<string, { conversationId: string }>();

app.disable("x-powered-by");
app.use(express.json({ limit: "64kb" }));
app.use(express.static("public"));

// 生成一帧符合 SSE 协议的字符串。
function sse(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

// 第一节已有的写入函数继续保留，普通状态事件仍可直接调用它。
function writeSse(
  response: express.Response,
  event: string,
  data: unknown,
): void {
  response.write(sse(event, data));
}

// 给业务会话追加一条展示消息。
function appendConversationMessage(
  conversation: ConversationRecord,
  role: "user" | "assistant",
  content: string,
): void {
  const createdAt = new Date().toISOString();
  conversation.messages.push({
    id: `msg_${randomUUID()}`,
    role,
    content,
    createdAt,
  });
  conversation.updatedAt = createdAt;
}

// 把 Pi SDK 内部事件翻译成前端约定的 SSE 事件。
// 返回 null 表示这个事件不需要发送给前端。
function translateEvent(event: any): string | null {
  switch (event.type) {
    // 文本增量：用于前端逐字显示。
    case "message_update": {
      const inner = event.assistantMessageEvent;
      if (inner?.type === "text_delta") {
        return sse("text", { delta: inner.delta });
      }
      return null;
    }

    // 消息结束：补发完整文本，修正网络抖动造成的增量丢失。
    case "message_end": {
      const message = event.message;
      if (message?.role !== "assistant") return null;

      const text = (message.content ?? [])
        .filter((block: any) => block?.type === "text" && block.text)
        .map((block: any) => block.text)
        .join("");

      if (message.stopReason === "error" && message.errorMessage) {
        return sse("error", { message: message.errorMessage });
      }

      return text ? sse("text_full", { text }) : null;
    }

    // Tool 生命周期事件使用同一个 toolCallId，前端据此更新同一张卡片。
    case "tool_execution_start":
      return sse("tool_start", {
        id: event.toolCallId,
        name: event.toolName,
        args: event.args,
      });

    case "tool_execution_update":
      return sse("tool_update", {
        id: event.toolCallId,
        name: event.toolName,
        result: event.result?.content?.[0]?.text ?? "",
      });

    case "tool_execution_end":
      return sse("tool_end", {
        id: event.toolCallId,
        name: event.toolName,
        result: event.result?.content?.[0]?.text ?? "",
        isError: event.isError,
      });

    case "agent_start":
      return sse("status", { state: "running", label: "开始执行" });

    case "agent_settled":
      return sse("status", { state: "settled", label: "执行完成" });

    default:
      return null;
  }
}

// -----------------------------------------------------------------------------
// 5. 基础接口与运行状态
// -----------------------------------------------------------------------------

// 第二节继续使用固定演示用户，不引入登录业务。
app.get("/api/auth/me", (_request, response) => {
  response.json({
    user: {
      id: "course-user",
      username: "student",
      displayName: "课程学员",
    },
  });
});

// 返回管理页面状态，并附带 Pi 当前真实加载的 Skill/Tool 信息。
app.get("/api/state", async (_request, response) => {
  const state = await readState(selectedModel);
  const runtimeSkills = buildRuntimePiSkills(state);

  response.json({
    ...state,
    // 后续课程才实现的模块先返回稳定空结构，保证成品前端可正常渲染。
    knowledge: [],
    tasks: [],
    observability: { runs: [] },
    runtime: {
      model: modelLabel,
      busy: activeRuns.size > 0,
      status: activeRuns.size > 0 ? "running" : "idle",
      tools: getExecutableTools(runtime, state),
      skills: runtimeSkills.skills.map((skill) => ({
        name: skill.name,
        description: skill.description,
        filePath: skill.filePath,
      })),
      skillDiagnostics: runtimeSkills.diagnostics,
    },
  });
});

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

// -----------------------------------------------------------------------------
// 6. Agent、Skill、Tool 管理接口
// -----------------------------------------------------------------------------

function registerCrud<
  T extends AgentRecord | SkillRecord | ToolRecord,
>(
  route: string,
  key: "agents" | "skills" | "tools",
  prefix: string,
): void {
  // 查询列表。
  app.get(route, async (_request, response) => {
    const state = await readState(selectedModel);
    response.json(state[key]);
  });

  // 新增记录，并在保存后让旧 Session 失效。
  app.post(route, async (request, response) => {
    const state = await readState(selectedModel);
    const record = createRecord(state[key] as T[], prefix, request.body);
    await writeState(state);
    resetSession(`${key} 配置新增`);
    response.status(201).json(record);
  });

  // 修改记录；找不到 id 时返回 404。
  app.put(`${route}/:id`, async (request, response) => {
    const state = await readState(selectedModel);
    const record = updateRecord(
      state[key] as T[],
      request.params.id,
      request.body,
    );

    if (!record) {
      return response.status(404).json({ error: "记录不存在" });
    }

    await writeState(state);
    resetSession(`${key} 配置变更`);
    response.json(record);
  });

  // 删除记录；删除成功后同样重建 Session。
  app.delete(`${route}/:id`, async (request, response) => {
    const state = await readState(selectedModel);
    const deleted = deleteRecord(state[key] as T[], request.params.id);

    if (!deleted) {
      return response.status(404).json({ error: "记录不存在" });
    }

    await writeState(state);
    resetSession(`${key} 配置删除`);
    response.json({ ok: true });
  });
}

registerCrud<AgentRecord>("/api/agents", "agents", "agent");
registerCrud<SkillRecord>("/api/skills", "skills", "skill");
registerCrud<ToolRecord>("/api/tools", "tools", "tool");

// -----------------------------------------------------------------------------
// 7. 会话接口
// -----------------------------------------------------------------------------

app.post("/api/conversations", async (request, response) => {
  const state = await readState(selectedModel);
  const activeAgent = getActiveAgent(state, request.body?.agentId);
  const title = String(request.body?.title || "新会话").trim() || "新会话";

  const record = createRecord<ConversationRecord>(
    state.conversations,
    "conv",
    {
      title,
      agentId: activeAgent?.id ?? "agent_new_agent",
      createdAt: new Date().toISOString(),
      messages: [],
    },
  );

  await writeState(state);
  response.status(201).json(record);
});

app.get("/api/conversations/:id", async (request, response) => {
  const state = await readState(selectedModel);
  const conversation = state.conversations.find(
    (item) => item.id === request.params.id,
  );

  if (!conversation) {
    return response.status(404).json({ error: "会话不存在" });
  }

  response.json(conversation);
});

// 取消指定 runId 对应的当前生成。
app.post("/api/runs/:runId/cancel", async (request, response) => {
  const run = activeRuns.get(request.params.runId);
  if (!run || !runtime.session) {
    return response.status(404).json({ error: "运行不存在或已经结束" });
  }

  await runtime.session.abort();
  response.json({ ok: true });
});

// -----------------------------------------------------------------------------
// 8. 核心聊天接口
// -----------------------------------------------------------------------------

app.post("/chat", async (request, response) => {
  const message = String(request.body?.message || "").trim();
  const conversationId = String(
    request.body?.conversationId || "",
  ).trim();

  if (!message) {
    return response.status(400).json({ error: "message 不能为空" });
  }
  if (!conversationId) {
    return response.status(400).json({ error: "conversationId 不能为空" });
  }
  if (activeRuns.size > 0) {
    return response.status(429).json({ error: "Agent 正忙，请稍后再试" });
  }

  // 每次请求都读取最新状态，避免使用管理页面修改前的旧快照。
  const state = await readState(selectedModel);
  const conversation = state.conversations.find(
    (item) => item.id === conversationId,
  );

  if (!conversation) {
    return response.status(404).json({ error: "会话不存在" });
  }

  // 在发送 SSE 响应头之前初始化 Session。
  // 这样模型或 Skill 配置错误仍可返回普通 JSON 错误。
  try {
    if (!runtime.session) {
      runtime.session = await createNewAgentSession(
        runtime,
        state,
        conversation,
      );
    }
  } catch (error) {
    const text = error instanceof Error ? error.message : "会话初始化失败";
    return response.status(500).json({ error: text });
  }

  const session = runtime.session;
  const runId = randomUUID();
  let clientClosed = false;
  let assistantText = "";

  activeRuns.set(runId, { conversationId });
  appendConversationMessage(conversation, "user", message);
  await writeState(state);

  response.writeHead(200, {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache, no-transform",
    Connection: "keep-alive",
    "X-Accel-Buffering": "no",
  });
  response.flushHeaders?.();

  writeSse(response, "run", { id: runId });
  writeSse(response, "conversation", { id: conversationId });
  writeSse(response, "status", { state: "running", label: "思考中" });

  // 必须先订阅，再调用 prompt()，否则快速返回时可能漏掉首个事件。
  const unsubscribe = session.subscribe((event) => {
    // 后端单独累计完整文本，用于写入业务会话记录。
    if (
      event.type === "message_update" &&
      event.assistantMessageEvent?.type === "text_delta"
    ) {
      assistantText += event.assistantMessageEvent.delta || "";
    }

    const payload = translateEvent(event);
    if (payload && !clientClosed) {
      response.write(payload);
    }
  });

  // 浏览器刷新、关闭或 AbortController 取消 fetch 时触发。
  response.on("close", () => {
    if (!response.writableEnded) {
      clientClosed = true;
      void session.abort();
    }
  });

  try {
    await session.prompt(message);

    const finalText = assistantText.trim() || "模型本轮没有返回文本。";
    appendConversationMessage(conversation, "assistant", finalText);
    await writeState(state);

    if (!clientClosed) {
      writeSse(response, "status", { state: "idle", label: "空闲" });
      writeSse(response, "done", { runId });
    }
  } catch (error) {
    if (!clientClosed) {
      const text = error instanceof Error ? error.message : "Agent 执行失败";
      writeSse(response, "error", { message: text });
    }
  } finally {
    unsubscribe();
    activeRuns.delete(runId);
    if (!clientClosed) response.end();
  }
});

// -----------------------------------------------------------------------------
// 9. 启动与退出清理
// -----------------------------------------------------------------------------

const server = app.listen(port, () => {
  console.log(`New-Agent 第二节：http://localhost:${port}`);
});

function shutdown(signal: string): void {
  console.log(`收到 ${signal}，正在释放会话资源`);
  runtime.session?.dispose();
  runtime.session = undefined;
  server.close(() => process.exit(0));
}

process.once("SIGINT", () => shutdown("SIGINT"));
process.once("SIGTERM", () => shutdown("SIGTERM"));
