/** AgentRecord 是一个可运行 Agent 的最终配置结构。 */
export type AgentRecord = {
  id: string;
  name: string;
  key: string;
  description: string;
  model: string;
  // 同一阶段只有 ACTIVE Agent 参与默认运行。
  status: "ACTIVE" | "DRAFT" | "DISABLED";
  maxLoop: number;
  timeoutMs: number;
  // 通过 ID 关联允许装配的 Skill。
  skillIds: string[];
  // 直接启用的 Pi 内置 Tool 名称。
  toolNames: string[];
  updatedAt: string;
};

/** SkillRecord 描述磁盘上的 Skill Package 及其发布状态。 */
export type SkillRecord = {
  id: string;
  name: string;
  label: string;
  description: string;
  // 只有 PROD Skill 会被 loadSkills 加载。
  status: "PROD" | "DRAFT" | "DISABLED";
  version: string;
  sourceType: "builtin" | "local" | "inline";
  // 相对项目根目录的安装路径，例如 skills/current-time。
  installPath: string;
  entryFile: string;
  toolNames: string[];
  content?: string;
  updatedAt: string;
};

/** ToolRecord 是管理面开关；真正执行函数由 Pi 或 skill-tools.ts 提供。 */
export type ToolRecord = {
  id: string;
  name: string;
  label: string;
  description: string;
  category: "filesystem" | "shell" | "search" | "agent";
  access: "read" | "write" | "execute";
  status: "ENABLED" | "DISABLED";
  updatedAt: string;
};

export type ConversationMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  createdAt: string;
};

export type ConversationRecord = {
  id: string;
  title: string;
  agentId: string;
  messages: ConversationMessage[];
  createdAt: string;
  updatedAt: string;
};
/** 一次高风险 Tool 调用对应一条审批记录。 */
export type ApprovalRecord = {
  // 审批记录唯一 ID，也是决定接口 URL 的一部分。
  id: string;
  // 关联本轮生成任务，取消 run 时可以批量取消其审批。
  runId: string;
  // 关联业务会话，后续持久化时仍能追踪来源。
  conversationId: string;
  // Pi 为本次 Tool 调用生成的唯一 ID。
  toolCallId: string;
  // 请求执行的 Tool 名称，例如 write、edit、bash。
  toolName: string;
  // 本节只有写入和破坏性操作会创建审批。
  risk: "write" | "destructive";
  // pending 表示等待用户决定；其余状态都不可再次处理。
  status: "pending" | "approved" | "rejected" | "expired" | "canceled";
  // 只保存经过脱敏和截断的参数预览，不保存完整敏感输入。
  inputPreview: string;
  // 告诉用户为什么本次调用需要审批。
  reason: string;
  // 状态层只保存哈希，不保存明文 token。
  tokenHash: string;
  createdAt: string;
  expiresAt: string;
  decidedAt?: string;
  decidedBy?: string;
};


export type AppState = {
  agents: AgentRecord[];
  skills: SkillRecord[];
  tools: ToolRecord[];
  conversations: ConversationRecord[];
  approvals: ApprovalRecord[];
};

// store.ts 只需要模型标识，不应依赖 Pi SDK 内部使用的具体 Model 类型。
// 使用最小结构类型可以让存储层保持独立，也避免额外安装模型包。
type ModelIdentity = {
  provider: string;
  id: string;
};

const now = () => new Date().toISOString();
const id = (prefix: string) =>
  `${prefix}_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
const clone = <T>(value: T): T => structuredClone(value);
const updatedAt = now();

// 第二节先保存在内存中。后续会替换为 JSON/MySQL，调用方不变。
let state: AppState = {
  agents: [{
    id: "agent_new_agent",
    name: "New Agent",
    key: "new-agent",
    description: "由 Pi SDK 驱动的可配置 Agent",
    model: "",
    // 默认运行 Agent。
    status: "ACTIVE",
    maxLoop: 20,
    timeoutMs: 120_000,
    // 把成品 current-time Skill 装配给默认 Agent。
    skillIds: ["skill_current_time"],
    // 开放完整 Pi 内置 Tool；写操作由 guard 统一审批。
    toolNames: ["read", "bash", "write", "edit", "grep", "find", "ls"],
    updatedAt,
  }],
  skills: [{
    id: "skill_current_time",
    name: "current-time",
    label: "当前时间",
    description: "查询日期、时间、时区与时间戳",
    status: "PROD",
    version: "1.0.0",
    sourceType: "local",
    installPath: "skills/current-time",
    entryFile: "SKILL.md",
    toolNames: ["current_time"],
    updatedAt,
  }],
  // 启用写工具，但真正执行前必须通过人工审批。
  tools: [
    ["read", "读取文件", "filesystem", "read", "ENABLED"],
    ["grep", "搜索文本", "search", "read", "ENABLED"],
    ["find", "查找文件", "search", "read", "ENABLED"],
    ["ls", "列出目录", "filesystem", "read", "ENABLED"],
    ["bash", "执行命令", "shell", "execute", "ENABLED"],
    ["write", "写入文件", "filesystem", "write", "ENABLED"],
    ["edit", "编辑文件", "filesystem", "write", "ENABLED"],
  ].map(([name, label, category, access, status]) => ({
    id: `tool_${name}`,
    name: name as string,
    label: label as string,
    description: label as string,
    category: category as ToolRecord["category"],
    access: access as ToolRecord["access"],
    status: status as ToolRecord["status"],
    updatedAt,
  })),
  conversations: [],
  approvals: [],
};

// 第二节先用内存；后续只替换这两个函数内部，调用方保持不变。
export async function readState(model?: ModelIdentity): Promise<AppState> {
  // 返回深拷贝，禁止路由层绕过 writeState 直接修改共享对象。
  const snapshot = clone(state);

  // 优先更新 ACTIVE Agent；没有 ACTIVE 时回退到第一条。
  // Agent 数组为空时保持安全，不访问 agents[0].model。
  const activeAgent =
    snapshot.agents.find((agent) => agent.status === "ACTIVE") ??
    snapshot.agents[0];

  // 只为尚未配置模型的 Agent 填充当前运行模型，避免覆盖管理页显式配置。
  if (model && activeAgent && !activeAgent.model) {
    activeAgent.model = `${model.provider}/${model.id}`;
  }

  return snapshot;
}

export async function writeState(next: AppState): Promise<void> {
  // 写入时同样复制，避免调用方后续修改污染已保存状态。
  state = clone(next);
}

// CRUD helper
export function createRecord<T extends { id: string; updatedAt: string }>(
  collection: T[], prefix: string, payload: Omit<T, "id" | "updatedAt">,
) {
  const record = { ...payload, id: id(prefix), updatedAt: now() } as T;
  // 新记录放在数组顶部，前端列表会优先展示。
  collection.unshift(record);
  return record;
}

export function updateRecord<T extends { id: string; updatedAt: string }>(
  collection: T[], recordId: string, patch: Partial<Omit<T, "id">>,
): T | null {
  const record = collection.find((item) => item.id === recordId);
  if (!record) return null;

  // HTTP 请求体在运行时仍可能携带 id，因此先保存真实 id，并在赋值末尾恢复。
  const stableId = record.id;
  // id 永远不允许被 patch 覆盖，只更新业务字段和时间。
  Object.assign(record, patch, {
    id: stableId,
    updatedAt: now(),
  });
  return record;
}

export function deleteRecord<T extends { id: string }>(collection: T[], recordId: string) {
  const index = collection.findIndex((item) => item.id === recordId);
  if (index < 0) return false;
  // 原地修改集合，保证调用者持有的 state 引用仍然有效。
  collection.splice(index, 1);
  return true;
}

export function createMessage(role: ConversationMessage["role"], content: string) {
  return { id: id("msg"), role, content, createdAt: now() };
}

export function touchRecord(record: { updatedAt: string }) {
  record.updatedAt = now();
}
