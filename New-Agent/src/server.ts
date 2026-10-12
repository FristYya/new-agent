import express from "express";
import { createHmac, randomUUID, timingSafeEqual } from "node:crypto";
import { readFileSync } from "node:fs";
import path from "node:path";
import {
  getAgentDir,
  loadSkills,
  ModelRuntime,
} from "@earendil-works/pi-coding-agent";
import {
  UserRegistry,
  UserRegistryError,
  type AuthUser,
} from "./auth-users.ts";
import { runtimeConfig } from "./config.ts";
import type { ToolPolicyDecision } from "./policy/tool-policy.ts";
import {
  createRuntimeRegistry,
  resetSession,
  type RuntimeContext,
} from "./runtime/runtime-context.ts";
import { createSessionRuntime } from "./runtime/session-runtime.ts";
import { safePreview } from "./security/redaction.ts";
import { discoverSkillPackageTools } from "./skill-tools.ts";
import {
  closeMysqlStorage,
  deletePiSession,
  mysqlStorageHealth,
  usesMysqlStorage,
  verifyMysqlStorage,
} from "./storage/mysql.ts";
import {
  createRecord,
  deleteRecord,
  readState,
  updateRecord,
  writeState,
  type AgentRecord,
  type ApprovalRecord,
  type AppState,
  type ConversationRecord,
  type SkillRecord,
  type ToolRecord,
} from "./store.ts";

// 第三节开放完整 Pi 内置 Tool。
// 是否允许执行由 Agent 配置与 guard 安全策略共同决定。
const CORE_TOOLS = ["read", "bash", "write", "edit", "grep", "find", "ls"];

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

// file 模式会跳过；MySQL 模式只校验连接和课前准备好的五张表。
// 应用启动期间不会执行 CREATE DATABASE、CREATE TABLE 或结构迁移。
await verifyMysqlStorage();
console.log(`数据存储：${usesMysqlStorage() ? "MySQL" : runtimeConfig.storage}`);

// 注册账号与 AppState 使用同一个持久化后端。
// MySQL 模式写入独立 users 表；file 模式写入 data/auth-users.json。
const users = await UserRegistry.create(
  path.join(process.cwd(), "data", "auth-users.json"),
);

const AUTH_COOKIE = "new_agent_session";
const AUTH_MAX_AGE_SECONDS = runtimeConfig.authMaxAgeSeconds;
const LOGIN_WINDOW_MS = 15 * 60 * 1000;
const LOGIN_MAX_ATTEMPTS = 10;
const REGISTRATION_WINDOW_MS = 15 * 60 * 1000;
const REGISTRATION_MAX_ATTEMPTS = 5;
const loginAttempts = new Map<string, { count: number; resetAt: number }>();
const registrationAttempts = new Map<string, { count: number; resetAt: number }>();

// runId 标识一次生成任务；conversationId 标识长期业务会话。
// 审批回调和取消接口都通过这张表确认 run 属于哪个登录用户。
const activeRuns = new Map<
  string,
  { conversationId: string; userId: string }
>();

// -----------------------------------------------------------------------------
// 2. 运行时结构与 Skill/Tool 策略
// -----------------------------------------------------------------------------

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

// 把运行时真实的 Skill/Tool 开关追加进系统提示词。
// 这能减少模型发起无效 Tool 调用，但真正的安全边界仍然是 guard。
function buildRuntimePrompt(ctx: RuntimeContext): string {
  const allowed = [...ctx.runtimeAllowedTools].join("、") || "无";
  const disabled = [...ctx.runtimeDisabledSkills].join("、") || "无";
  const skills = ctx.runtimePiSkills.map((skill) => skill.name).join("、") || "无";

  return [
    "当前运行时能力配置：",
    `- 当前已安装并启用 Skill：${skills}`,
    `- 当前可调用工具：${allowed}`,
    `- 当前已禁用 Skill：${disabled}`,
    "- 只能使用当前可调用工具完成任务。",
    "- 如果某个 Skill 已禁用，不能通过其他工具绕行完成该 Skill 的专属能力。",
  ].join("\n");
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

// /api/state 会在第一次聊天之前被前端调用。
// 此时正式 RuntimeContext 尚未执行 applyRuntimePolicy()，所以不能直接读取其中的
// runtimeSkillToolNames；这里基于当前配置创建一份只读预览，避免状态页漏掉 Skill Tool。
async function previewExecutableTools(
  ctx: RuntimeContext,
  state: AppState,
  agentId?: string,
): Promise<string[]> {
  const [allSkillTools, activeSkillTools] = await Promise.all([
    discoverSkillPackageTools(state.skills),
    discoverSkillPackageTools(getEnabledSkillRecords(state, agentId)),
  ]);

  const previewContext: RuntimeContext = {
    ...ctx,
    runtimeSkillOwnedTools: new Set(
      allSkillTools.map((item) => item.manifest.name),
    ),
    runtimeSkillToolNames: new Set(
      activeSkillTools.map((item) => item.manifest.name),
    ),
  };

  return getExecutableTools(previewContext, state, agentId);
}

// 返回被当前 Agent 绑定、但未处于 PROD 状态的 Skill 和其 Tool 名称。
function getDisabledSkillNames(state: AppState, agentId?: string): string[] {
  const agent = getActiveAgent(state, agentId);
  const agentSkillIds = new Set(agent?.skillIds ?? []);

  return state.skills
    .filter(
      (skill) => agentSkillIds.has(skill.id) && skill.status !== "PROD",
    )
    .flatMap((skill) => [skill.name, ...skill.toolNames]);
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

// 审批超时后同步更新 AppState；Registry 会同时把等待中的 Tool 按拒绝恢复。
async function expireApproval(
  ctx: RuntimeContext,
  approvalId: string,
): Promise<void> {
  const state = await readState(selectedModel, ctx.user.id);
  const approval = state.approvals.find((item) => item.id === approvalId);
  if (!approval || approval.status !== "pending") return;

  approval.status = "expired";
  approval.decidedAt = new Date().toISOString();
  await writeState(state, ctx.user.id);
}

// guard 发现写入或破坏性操作时调用本函数，并等待用户决定。
async function requestToolApproval(
  ctx: RuntimeContext,
  event: any,
  decision: ToolPolicyDecision,
): Promise<boolean> {
  if (!ctx.activeRunId || !ctx.activeConversationId) return false;
  const activeRun = activeRuns.get(ctx.activeRunId);
  if (!activeRun || activeRun.userId !== ctx.user.id) return false;

  const state = await readState(selectedModel, ctx.user.id);
  const createdAt = new Date();
  const approvalId = `approval_${randomUUID()}`;
  const pending = ctx.approvalRegistry.create(
    approvalId,
    ctx.activeRunId,
    runtimeConfig.approvalTimeoutMs,
    () => expireApproval(ctx, approvalId),
  );

  const approval: ApprovalRecord = {
    id: approvalId,
    runId: ctx.activeRunId,
    conversationId: ctx.activeConversationId,
    toolCallId: String(event.toolCallId || `tool_call_${randomUUID()}`),
    toolName: String(event.toolName || "unknown"),
    risk: decision.risk === "destructive" ? "destructive" : "write",
    status: "pending",
    // 审批页面只接收脱敏后的有限长度预览。
    inputPreview: safePreview(event.input, 500),
    reason: decision.reason || "该工具调用需要人工审批",
    tokenHash: pending.tokenHash,
    createdAt: createdAt.toISOString(),
    expiresAt: new Date(
      createdAt.getTime() + runtimeConfig.approvalTimeoutMs,
    ).toISOString(),
  };

  state.approvals.unshift(approval);
  // 内存课程版本最多保留 500 条，避免列表无限增长。
  state.approvals = state.approvals.slice(0, 500);
  await writeState(state, ctx.user.id);

  // 明文 token 只通过本次 SSE 交给前端；AppState 中只保存哈希。
  ctx.approvalNotifier?.({ ...approval, token: pending.token });
  return pending.promise;
}

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
  ctx.runtimeDisabledSkills = new Set(
    getDisabledSkillNames(state, agentId),
  );
  return executableTools;
}

// 用户级运行时注册表只负责 RuntimeContext 的创建、复用与统一释放。
const runtimeRegistry = createRuntimeRegistry({
  coreTools: CORE_TOOLS,
  readSystemPrompt,
  buildRuntimePrompt,
  requestToolApproval,
});

// 会话运行时负责 AgentSession 的创建、恢复、切换和持久化。
const {
  getConversationSession,
  getConversationTree,
  navigateConversationTree,
  persistPiSession,
} = createSessionRuntime({
  coreTools: CORE_TOOLS,
  model: selectedModel,
  modelRuntime,
  applyRuntimePolicy,
});

// -----------------------------------------------------------------------------
// 4. Express、SSE 与通用辅助函数
// -----------------------------------------------------------------------------

const app = express();
const port = runtimeConfig.port;

app.disable("x-powered-by");
app.use(express.json({ limit: "64kb" }));
app.use(express.static("public"));

// 观测面板可以直接复用这个存储健康信号。
app.get("/health", async (_request, response) => {
  const storage = await mysqlStorageHealth();
  response.status(storage.healthy ? 200 : 503).json({
    ok: storage.healthy,
    model: modelLabel,
    storage,
  });
});

function parseCookies(header?: string): Record<string, string> {
  return Object.fromEntries(
    String(header || "")
      .split(";")
      .map((item) => item.trim())
      .filter(Boolean)
      .map((item) => {
        const index = item.indexOf("=");
        return index >= 0
          ? [item.slice(0, index), decodeURIComponent(item.slice(index + 1))]
          : [item, ""];
      }),
  );
}

function cookieHeader(
  name: string,
  value: string,
  maxAgeSeconds: number,
): string {
  const secure = runtimeConfig.authCookieSecure ? "; Secure" : "";
  return `${name}=${encodeURIComponent(value)}; HttpOnly; SameSite=Lax; Path=/; Max-Age=${maxAgeSeconds}${secure}`;
}

function signAuthPayload(payload: string): string {
  return createHmac("sha256", runtimeConfig.authSecret)
    .update(payload)
    .digest("base64url");
}

function createAuthToken(user: AuthUser): string {
  const expiresAt = Date.now() + AUTH_MAX_AGE_SECONDS * 1000;
  // Cookie 只保存用户 ID 和过期时间，不保存密码或完整用户对象。
  const payload = Buffer.from(
    JSON.stringify({ userId: user.id, expiresAt }),
    "utf-8",
  ).toString("base64url");
  return `${payload}.${signAuthPayload(payload)}`;
}

function parseAuthToken(token: string): AuthUser | undefined {
  try {
    const [payload, signature] = token.split(".");
    if (!payload || !signature) return undefined;

    const expected = Buffer.from(signAuthPayload(payload));
    const actual = Buffer.from(signature);
    if (
      expected.length !== actual.length ||
      !timingSafeEqual(expected, actual)
    ) return undefined;

    const data = JSON.parse(
      Buffer.from(payload, "base64url").toString("utf-8"),
    ) as { userId?: string; expiresAt?: number };
    if (!data.userId || !data.expiresAt || data.expiresAt < Date.now()) {
      return undefined;
    }

    const user = users.findById(data.userId);
    return user
      ? {
          id: user.id,
          username: user.username,
          displayName: user.displayName,
        }
      : undefined;
  } catch {
    return undefined;
  }
}

function getAuthUser(request: express.Request): AuthUser | undefined {
  const token = parseCookies(request.headers.cookie)[AUTH_COOKIE];
  return token ? parseAuthToken(token) : undefined;
}

function requestUser(response: express.Response): AuthUser {
  return response.locals.user as AuthUser;
}

function requestRuntime(response: express.Response): RuntimeContext {
  return response.locals.runtime as RuntimeContext;
}

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

app.get("/api/auth/me", (request, response) => {
  const user = getAuthUser(request);
  if (!user) return response.status(401).json({ error: "未登录" });
  response.json({ user });
});

app.post("/api/auth/register", async (request, response) => {
  const username = String(request.body?.username || "").trim();
  const password = String(request.body?.password || "");
  const displayName = String(request.body?.displayName || username).trim();
  const attemptKey = `${request.ip}:${username.toLocaleLowerCase()}`;
  const currentTime = Date.now();
  const previous = registrationAttempts.get(attemptKey);
  const attempt = !previous || previous.resetAt <= currentTime
    ? { count: 0, resetAt: currentTime + REGISTRATION_WINDOW_MS }
    : previous;

  if (attempt.count >= REGISTRATION_MAX_ATTEMPTS) {
    response.setHeader(
      "Retry-After",
      String(Math.ceil((attempt.resetAt - currentTime) / 1000)),
    );
    return response.status(429).json({ error: "注册尝试过多，请稍后再试" });
  }

  try {
    const user = await users.register(username, password, displayName);
    registrationAttempts.delete(attemptKey);
    const token = createAuthToken(user);
    runtimeRegistry.getRuntime(user);
    response.setHeader(
      "Set-Cookie",
      cookieHeader(AUTH_COOKIE, token, AUTH_MAX_AGE_SECONDS),
    );
    return response.status(201).json({ user });
  } catch (error) {
    attempt.count += 1;
    registrationAttempts.set(attemptKey, attempt);

    if (error instanceof UserRegistryError) {
      return response.status(error.statusCode).json({ error: error.message });
    }
    console.error("注册用户失败", error);
    return response.status(500).json({ error: "注册失败，请稍后重试" });
  }
});

app.post("/api/auth/login", async (request, response) => {
  const username = String(request.body?.username || "").trim();
  const password = String(request.body?.password || "");

  if (!username || username.length > 64 || !password || password.length > 128) {
    return response.status(400).json({ error: "用户名或密码格式不正确" });
  }

  // 防止攻击者用大量不同用户名无限撑大进程内限流表。
  if (loginAttempts.size > 1_000) {
    const currentTime = Date.now();
    for (const [key, value] of loginAttempts) {
      if (value.resetAt <= currentTime) loginAttempts.delete(key);
    }
  }

  const attemptKey = `${request.ip}:${username}`;
  const currentTime = Date.now();
  const previous = loginAttempts.get(attemptKey);
  const attempt = !previous || previous.resetAt <= currentTime
    ? { count: 0, resetAt: currentTime + LOGIN_WINDOW_MS }
    : previous;

  if (attempt.count >= LOGIN_MAX_ATTEMPTS) {
    response.setHeader(
      "Retry-After",
      String(Math.ceil((attempt.resetAt - currentTime) / 1000)),
    );
    return response.status(429).json({ error: "登录尝试过多，请稍后再试" });
  }

  const credential = users.findByUsername(username);
  if (!credential || !(await users.verifyPassword(credential, password))) {
    attempt.count += 1;
    loginAttempts.set(attemptKey, attempt);
    return response.status(401).json({ error: "用户名或密码错误" });
  }

  loginAttempts.delete(attemptKey);
  const user: AuthUser = {
    id: credential.id,
    username: credential.username,
    displayName: credential.displayName,
  };
  const token = createAuthToken(user);
  runtimeRegistry.getRuntime(user);
  response.setHeader(
    "Set-Cookie",
    cookieHeader(AUTH_COOKIE, token, AUTH_MAX_AGE_SECONDS),
  );
  response.json({ user });
});

app.post("/api/auth/logout", (_request, response) => {
  response.setHeader("Set-Cookie", cookieHeader(AUTH_COOKIE, "", 0));
  response.json({ ok: true });
});

// 认证接口保持公开；从这里开始，所有业务 API 与聊天请求都必须登录。
app.use((request, response, next) => {
  const isProtectedPath =
    request.path === "/chat" || request.path.startsWith("/api/");
  if (!isProtectedPath) return next();

  const user = getAuthUser(request);
  if (!user) {
    return response.status(401).json({ error: "请先登录 New-Agent" });
  }

  response.locals.user = user;
  response.locals.runtime = runtimeRegistry.getRuntime(user);
  next();
});

// 返回管理页面状态，并附带 Pi 当前真实加载的 Skill/Tool 信息。
app.get("/api/state", async (_request, response) => {
  const user = requestUser(response);
  const runtime = requestRuntime(response);
  const state = await readState(selectedModel, user.id);
  const runtimeSkills = buildRuntimePiSkills(state);
  const runtimeTools = await previewExecutableTools(runtime, state);
  const busy = Boolean(
    runtime.activeRunId && activeRuns.has(runtime.activeRunId),
  );

  response.json({
    ...state,
    // 后续课程才实现的模块先返回稳定空结构，保证成品前端可正常渲染。
    knowledge: [],
    tasks: [],
    observability: { runs: [] },
    runtime: {
      model: modelLabel,
      busy,
      status: busy ? "running" : "idle",
      tools: runtimeTools,
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
    const user = requestUser(response);
    const state = await readState(selectedModel, user.id);
    response.json(state[key]);
  });

  // 新增记录，并在保存后让旧 Session 失效。
  app.post(route, async (request, response) => {
    const user = requestUser(response);
    const runtime = requestRuntime(response);
    const state = await readState(selectedModel, user.id);
    const record = createRecord(state[key] as T[], prefix, request.body);
    await writeState(state, user.id);
    resetSession(runtime, `${key} 配置新增`);
    response.status(201).json(record);
  });

  // 修改记录；找不到 id 时返回 404。
  app.put(`${route}/:id`, async (request, response) => {
    const user = requestUser(response);
    const runtime = requestRuntime(response);
    const state = await readState(selectedModel, user.id);
    const record = updateRecord(
      state[key] as T[],
      request.params.id,
      request.body,
    );

    if (!record) {
      return response.status(404).json({ error: "记录不存在" });
    }

    await writeState(state, user.id);
    resetSession(runtime, `${key} 配置变更`);
    response.json(record);
  });

  // 删除记录；删除成功后同样重建 Session。
  app.delete(`${route}/:id`, async (request, response) => {
    const user = requestUser(response);
    const runtime = requestRuntime(response);
    const state = await readState(selectedModel, user.id);
    const deleted = deleteRecord(state[key] as T[], request.params.id);

    if (!deleted) {
      return response.status(404).json({ error: "记录不存在" });
    }

    await writeState(state, user.id);
    resetSession(runtime, `${key} 配置删除`);
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
  const user = requestUser(response);
  const state = await readState(selectedModel, user.id);
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

  await writeState(state, user.id);
  response.status(201).json(record);
});

app.get("/api/conversations/:id", async (request, response) => {
  const user = requestUser(response);
  const state = await readState(selectedModel, user.id);
  const conversation = state.conversations.find(
    (item) => item.id === request.params.id,
  );

  if (!conversation) {
    return response.status(404).json({ error: "会话不存在" });
  }

  response.json(conversation);
});

// 读取同一个 Pi Session 内的完整分支树。
// 返回的是经过裁剪的展示数据，不会把 Tool 输入和结果原文发送到浏览器。
app.get("/api/conversations/:id/tree", async (request, response) => {
  const user = requestUser(response);
  const runtime = requestRuntime(response);

  // 树导航会修改 Session 的当前叶子；生成过程中不允许并发读取/切换另一棵树。
  if (
    runtime.treeNavigationInProgress ||
    (runtime.activeRunId && activeRuns.has(runtime.activeRunId))
  ) {
    return response.status(409).json({ error: "Agent 正在执行，完成或停止后再查看会话树" });
  }

  const state = await readState(selectedModel, user.id);
  const conversation = state.conversations.find(
    (item) => item.id === request.params.id,
  );
  if (!conversation) {
    return response.status(404).json({ error: "会话不存在" });
  }

  runtime.treeNavigationInProgress = true;
  try {
    const tree = await getConversationTree(
      runtime,
      state,
      conversation,
      user.id,
    );
    // getConversationTree 可能首次创建 Pi Session，需要保存它的定位信息。
    await writeState(state, user.id);
    response.json(tree);
  } catch (error) {
    const message = error instanceof Error ? error.message : "读取会话树失败";
    response.status(500).json({ error: message });
  } finally {
    runtime.treeNavigationInProgress = false;
  }
});

// 切换到同一个 Pi Session 的历史节点。
// 后续再次发送消息时，Pi 会从该节点继续追加并形成新分支。
app.post("/api/conversations/:id/tree/navigate", async (request, response) => {
  const user = requestUser(response);
  const runtime = requestRuntime(response);
  const targetId = String(request.body?.targetId || "").trim();
  const summarize = request.body?.summarize === true;

  if (!targetId || targetId.length > 200) {
    return response.status(400).json({ error: "targetId 不合法" });
  }
  if (
    runtime.treeNavigationInProgress ||
    (runtime.activeRunId && activeRuns.has(runtime.activeRunId))
  ) {
    return response.status(409).json({ error: "Agent 正在执行，不能切换会话树" });
  }

  const state = await readState(selectedModel, user.id);
  const conversation = state.conversations.find(
    (item) => item.id === request.params.id,
  );
  if (!conversation) {
    return response.status(404).json({ error: "会话不存在" });
  }

  runtime.treeNavigationInProgress = true;
  try {
    const result = await navigateConversationTree(
      runtime,
      state,
      conversation,
      user.id,
      targetId,
      summarize,
    );
    if (result.cancelled) {
      return response.status(409).json({ error: "会话树切换已被扩展取消" });
    }

    // 保存 active branch 的页面消息投影和 piLeafId；Pi entries 已由运行时持久化。
    await writeState(state, user.id);
    response.json({
      conversation,
      tree: result.tree,
      editorText: result.editorText,
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : "切换会话树失败";
    const status = message === "会话树节点不存在" ? 404 : 409;
    response.status(status).json({ error: message });
  } finally {
    runtime.treeNavigationInProgress = false;
  }
});

app.put("/api/conversations/:id", async (request, response) => {
  const user = requestUser(response);
  const runtime = requestRuntime(response);
  const state = await readState(selectedModel, user.id);
  const conversation = updateRecord(
    state.conversations,
    request.params.id,
    request.body,
  );

  if (!conversation) {
    return response.status(404).json({ error: "会话不存在" });
  }

  await writeState(state, user.id);

  // 同一会话换 Agent 后，原 Session 的 Skill/Tool 组合已经不再适用。
  if (
    runtime.activeConversationId === conversation.id &&
    runtime.activeAgentId !== conversation.agentId
  ) {
    resetSession(runtime, "会话绑定 Agent 变更");
  }

  response.json(conversation);
});

app.delete("/api/conversations/:id", async (request, response) => {
  const user = requestUser(response);
  const runtime = requestRuntime(response);
  const state = await readState(selectedModel, user.id);
  const deleted = deleteRecord(state.conversations, request.params.id);

  if (!deleted) {
    return response.status(404).json({ error: "会话不存在" });
  }

  await writeState(state, user.id);
  // file 模式会跳过；MySQL 模式同步删除会话树，避免孤儿数据。
  await deletePiSession(user.id, request.params.id);

  if (runtime.activeConversationId === request.params.id) {
    resetSession(runtime, "当前会话已删除");
  }

  response.json({ ok: true });
});

// 查询审批记录。待审批 token 每次查询都会轮换，旧页面中的 token 随即失效。
app.get("/api/approvals", async (request, response) => {
  const user = requestUser(response);
  const runtime = requestRuntime(response);
  const state = await readState(selectedModel, user.id);
  const requestedStatus =
    typeof request.query.status === "string"
      ? request.query.status
      : undefined;
  const approvals = state.approvals.filter(
    (item) => !requestedStatus || item.status === requestedStatus,
  );

  let changed = false;
  const result = approvals.map((approval) => {
    if (approval.status !== "pending") return approval;
    const rotated = runtime.approvalRegistry.rotateToken(approval.id);
    if (!rotated) return approval;

    approval.tokenHash = rotated.tokenHash;
    changed = true;
    return { ...approval, token: rotated.token };
  });

  if (changed) await writeState(state, user.id);
  response.json({ approvals: result });
});

// 提交批准或拒绝。approvalId 和一次性 token 必须同时匹配。
app.post("/api/approvals/:id/decision", async (request, response) => {
  const user = requestUser(response);
  const runtime = requestRuntime(response);
  const decision = request.body?.decision;
  const token = String(request.body?.token || "");

  if (!["approved", "rejected"].includes(decision) || !token) {
    return response.status(400).json({
      error: "decision 和一次性审批 token 不能为空",
    });
  }

  if (!runtime.approvalRegistry.verify(request.params.id, token)) {
    return response.status(409).json({
      error: "审批已失效或 token 不正确",
    });
  }

  const state = await readState(selectedModel, user.id);
  const approval = state.approvals.find(
    (item) => item.id === request.params.id,
  );

  if (!approval || approval.status !== "pending") {
    return response.status(409).json({ error: "审批已处理或不存在" });
  }

  approval.status = decision;
  approval.decidedAt = new Date().toISOString();
  approval.decidedBy = user.id;
  await writeState(state, user.id);

  // 最后才恢复 guard 中等待的 Promise；批准返回 true，拒绝返回 false。
  runtime.approvalRegistry.decide(
    approval.id,
    token,
    decision === "approved",
  );

  // 不把 tokenHash 返回给浏览器。
  const { tokenHash: _tokenHash, ...safeApproval } = approval;
  response.json({ approval: safeApproval });
});

// 将某次运行仍在等待的审批记录统一标记为取消。
async function cancelApprovalsForRun(
  runtime: RuntimeContext,
  user: AuthUser,
  runId: string,
): Promise<void> {
  runtime.approvalRegistry.cancelRun(runId);
  const state = await readState(selectedModel, user.id);
  let changed = false;

  for (const approval of state.approvals) {
    if (approval.runId !== runId || approval.status !== "pending") continue;
    approval.status = "canceled";
    approval.decidedAt = new Date().toISOString();
    changed = true;
  }

  if (changed) await writeState(state, user.id);
}

// 取消指定 runId 对应的当前生成。
app.post("/api/runs/:runId/cancel", async (request, response) => {
  const user = requestUser(response);
  const runtime = requestRuntime(response);
  const run = activeRuns.get(request.params.runId);
  if (!run || run.userId !== user.id || !runtime.session) {
    return response.status(404).json({ error: "运行不存在或已经结束" });
  }

  await cancelApprovalsForRun(runtime, user, request.params.runId);
  await runtime.session.abort();
  response.json({ ok: true });
});

// -----------------------------------------------------------------------------
// 8. 核心聊天接口
// -----------------------------------------------------------------------------

app.post("/chat", async (request, response) => {
  const user = requestUser(response);
  const runtime = requestRuntime(response);
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
  if (runtime.activeRunId && activeRuns.has(runtime.activeRunId)) {
    return response.status(429).json({ error: "Agent 正忙，请稍后再试" });
  }
  if (runtime.treeNavigationInProgress) {
    return response.status(409).json({ error: "会话树正在读取或切换，请稍后再试" });
  }

  // 每次请求都读取最新状态，避免使用管理页面修改前的旧快照。
  const state = await readState(selectedModel, user.id);
  const conversation = state.conversations.find(
    (item) => item.id === conversationId,
  );

  if (!conversation) {
    return response.status(404).json({ error: "会话不存在" });
  }

  // 使用局部变量锁定本次与 conversationId 匹配的 Session。
  let session;
  try {
    // 同一会话复用；切换会话或进程重启后从持久化数据恢复。
    session = await getConversationSession(
      runtime,
      state,
      conversation,
      user.id,
    );
  } catch (error) {
    const text = error instanceof Error ? error.message : "会话初始化失败";
    return response.status(500).json({ error: text });
  }

  const runId = randomUUID();
  let clientClosed = false;
  let assistantText = "";

  activeRuns.set(runId, { conversationId, userId: user.id });
  runtime.activeRunId = runId;
  appendConversationMessage(conversation, "user", message);
  await writeState(state, user.id);

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

  // guard 请求审批时，通过当前聊天连接把审批卡片立即推送给浏览器。
  runtime.approvalNotifier = (approval) => {
    if (clientClosed || response.writableEnded) return;
    const { tokenHash: _tokenHash, ...payload } = approval;
    writeSse(response, "approval_required", payload);
  };

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
      void cancelApprovalsForRun(runtime, user, runId);
      void session.abort();
    }
  });

  try {
    await session.prompt(message);

    const finalText = assistantText.trim() || "模型本轮没有返回文本。";

    // 审批接口会在 Tool 等待期间更新 AppState。
    // 因此这里重新读取最新状态，避免用聊天开始时的旧快照覆盖审批结果。
    const latestState = await readState(selectedModel, user.id);
    const latestConversation = latestState.conversations.find(
      (item) => item.id === conversation.id,
    );
    if (latestConversation) {
      // 先保存 Pi 树并同步 piLeafId，再写页面消息，保证 done 事件发出前数据已落库。
      await persistPiSession(user.id, latestConversation, session);
      appendConversationMessage(latestConversation, "assistant", finalText);
      await writeState(latestState, user.id);
    }

    if (!clientClosed) {
      writeSse(response, "status", { state: "idle", label: "空闲" });
      writeSse(response, "done", { runId });
    }
  } catch (error) {
    if (!clientClosed) {
      const text = safePreview(
        error instanceof Error ? error.message : "Agent 执行失败",
        500,
      );
      writeSse(response, "error", { message: text });
    }
  } finally {
    unsubscribe();

    // 无论本轮成功、失败还是取消，都以最新 AppState 保存完整 Pi entries 和叶子游标。
    try {
      const finalState = await readState(selectedModel, user.id);
      const finalConversation = finalState.conversations.find(
        (item) => item.id === conversation.id,
      );
      if (finalConversation) {
        await persistPiSession(user.id, finalConversation, session);
        await writeState(finalState, user.id);
      }
    } catch (error) {
      console.error(
        "[pi-session:persist-error]",
        error instanceof Error ? error.message : String(error),
      );
    }

    activeRuns.delete(runId);
    if (runtime.activeRunId === runId) {
      runtime.activeRunId = undefined;
      runtime.approvalNotifier = undefined;
      // activeConversationId/activeAgentId 描述缓存 Session 的归属，不能在这里清空。
    }
    if (!clientClosed) response.end();
  }
});

// -----------------------------------------------------------------------------
// 9. 启动与退出清理
// -----------------------------------------------------------------------------

const server = app.listen(port, () => {
  console.log(`New-Agent：http://localhost:${port}`);
});

function shutdown(signal: string): void {
  console.log(`收到 ${signal}，正在释放会话资源`);
  runtimeRegistry.disposeAll();
  server.close(() => {
    // 等连接池正常关闭后再退出，避免丢失仍在完成中的数据库操作。
    void closeMysqlStorage().finally(() => process.exit(0));
  });
}

process.once("SIGINT", () => shutdown("SIGINT"));
process.once("SIGTERM", () => shutdown("SIGTERM"));
