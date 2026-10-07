/** AgentRecord 是一个可运行 Agent 的配置结构。 */
export type AgentRecord = {
  id: string;
  name: string;
  key: string;
  description: string;
  model: string;
  status: "ACTIVE" | "DRAFT" | "DISABLED";
  maxLoop: number;
  timeoutMs: number;
  skillIds: string[];
  toolNames: string[];
  updatedAt: string;
};

/** SkillRecord 描述磁盘上的 Skill Package 及其发布状态。 */
export type SkillRecord = {
  id: string;
  name: string;
  label: string;
  description: string;
  status: "PROD" | "DRAFT" | "DISABLED";
  version: string;
  sourceType: "builtin" | "local" | "inline";
  installPath: string;
  entryFile: string;
  toolNames: string[];
  content?: string;
  updatedAt: string;
};

/** ToolRecord 只保存管理状态，真正的执行函数由 Pi 或 skill-tools.ts 提供。 */
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

export type AppState = {
  agents: AgentRecord[];
  skills: SkillRecord[];
  tools: ToolRecord[];
  conversations: ConversationRecord[];
};

const now = () => new Date().toISOString();
const id = (prefix: string) =>
  `${prefix}_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
const clone = <T>(value: T): T => structuredClone(value);
const updatedAt = now();

// 第二节暂时使用内存状态；后续课程会把内部实现替换为 JSON/MySQL。
let state: AppState = {
  agents: [
    {
      id: "agent_new_agent",
      name: "New Agent",
      key: "new-agent",
      description: "由 Pi SDK 驱动的可配置 Agent",
      model: "",
      status: "ACTIVE",
      maxLoop: 20,
      timeoutMs: 120_000,
      skillIds: ["skill_current_time"],
      toolNames: ["read", "grep", "find", "ls"],
      updatedAt,
    },
  ],
  skills: [
    {
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
    },
  ],
  tools: [
    {
      id: "tool_read",
      name: "read",
      label: "读取文件",
      description: "读取工作区文件内容",
      category: "filesystem",
      access: "read",
      status: "ENABLED",
      updatedAt,
    },
    {
      id: "tool_grep",
      name: "grep",
      label: "搜索文本",
      description: "在工作区中搜索文本",
      category: "search",
      access: "read",
      status: "ENABLED",
      updatedAt,
    },
    {
      id: "tool_find",
      name: "find",
      label: "查找文件",
      description: "按名称查找工作区文件",
      category: "search",
      access: "read",
      status: "ENABLED",
      updatedAt,
    },
    {
      id: "tool_ls",
      name: "ls",
      label: "列出目录",
      description: "列出目录内容",
      category: "filesystem",
      access: "read",
      status: "ENABLED",
      updatedAt,
    },
    {
      id: "tool_bash",
      name: "bash",
      label: "执行命令",
      description: "执行终端命令",
      category: "shell",
      access: "execute",
      status: "DISABLED",
      updatedAt,
    },
    {
      id: "tool_write",
      name: "write",
      label: "写入文件",
      description: "创建或覆盖文件",
      category: "filesystem",
      access: "write",
      status: "DISABLED",
      updatedAt,
    },
    {
      id: "tool_edit",
      name: "edit",
      label: "编辑文件",
      description: "修改已有文件",
      category: "filesystem",
      access: "write",
      status: "DISABLED",
      updatedAt,
    },
  ],
  conversations: [],
};

/** 返回状态副本，避免路由在未调用 writeState 时意外修改全局状态。 */
export async function readState(model: {
  provider: string;
  id: string;
}): Promise<AppState> {
  const snapshot = clone(state);
  const activeAgent =
    snapshot.agents.find((item) => item.status === "ACTIVE") ??
    snapshot.agents[0];

  // 第一次读取时把 Pi 实际模型写入默认 Agent，方便管理页展示。
  if (activeAgent && !activeAgent.model) {
    activeAgent.model = `${model.provider}/${model.id}`;
  }

  return snapshot;
}

/** 第二节只写回内存；调用边界会在持久化课程中保持不变。 */
export async function writeState(nextState: AppState): Promise<void> {
  state = clone(nextState);
}

/** 创建带统一 id 和 updatedAt 的记录，并直接加入目标集合。 */
export function createRecord<T extends { id: string; updatedAt: string }>(
  collection: T[],
  prefix: string,
  input: Omit<T, "id" | "updatedAt"> & Partial<Pick<T, "id" | "updatedAt">>,
): T {
  const record = {
    ...input,
    id: input.id || id(prefix),
    updatedAt: now(),
  } as T;
  collection.unshift(record);
  return record;
}

/** 按 id 修改记录；不存在时返回 undefined。 */
export function updateRecord<T extends { id: string; updatedAt: string }>(
  collection: T[],
  recordId: string,
  patch: Partial<Omit<T, "id">>,
): T | undefined {
  const record = collection.find((item) => item.id === recordId);
  if (!record) return undefined;
  Object.assign(record, patch, { id: record.id, updatedAt: now() });
  return record;
}

/** 按 id 删除记录，并返回是否实际删除。 */
export function deleteRecord<T extends { id: string }>(
  collection: T[],
  recordId: string,
): boolean {
  const index = collection.findIndex((item) => item.id === recordId);
  if (index < 0) return false;
  collection.splice(index, 1);
  return true;
}

/** 创建格式统一的会话消息，供后续课程继续复用。 */
export function createMessage(
  role: ConversationMessage["role"],
  content: string,
): ConversationMessage {
  return {
    id: id("msg"),
    role,
    content,
    createdAt: now(),
  };
}

/** 更新记录时间戳，避免各路由重复拼接时间逻辑。 */
export function touchRecord(record: { updatedAt: string }): void {
  record.updatedAt = now();
}
