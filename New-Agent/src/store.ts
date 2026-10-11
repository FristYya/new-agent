import { mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import {
  readStateDocument,
  usesMysqlStorage,
  writeStateDocument,
} from "./storage/mysql.ts";
import { Agent } from "node:http";

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
  // file 模式保存 Pi SDK 的 JSONL Session 路径。
  piSessionFile?: string;
  // MySQL 模式保存稳定 Session ID，完整 entries 位于 pi_sessions 表。
  piSessionId?: string;
  // 页面展示消息与 Pi Session 分开保存，不能用它重建模型上下文。
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
  // 关联业务会话，持久化后仍能追踪来源。
  conversationId: string;
  // Pi 为本次 Tool 调用生成的唯一 ID。
  toolCallId: string;
  // 请求执行的 Tool 名称，例如 write、edit、bash。
  toolName: string;
  // 写入和破坏性操作会进入不同风险级别的审批。
  risk: "write" | "destructive";
  // pending 表示等待用户决定；其余状态都不可再次处理。
  status: "pending" | "approved" | "rejected" | "expired" | "canceled";
  // 只保存经过脱敏和截断的参数预览，不保存完整敏感输入。
  inputPreview: string;
  // 告诉用户为什么本次调用需要审批。
  reason: string;
  // 状态层只保存哈希；明文 token 仅通过一次 SSE 发送。
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

// Store 只需要模型标识，不依赖 Pi SDK 内部的完整 Model 类型。
type ModelIdentity = {
  provider: string;
  id: string;
};

const now = () => new Date().toISOString();
const id = (prefix: string) =>
  `${prefix}_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
const clone = <T>(value: T): T => structuredClone(value);
const updatedAt = now();

// file 回退模式的所有运行数据都放在 data 目录中。
const DATA_DIR = process.env.NEW_AGENT_DATA_DIR || join(process.cwd(), "data");
const STORE_FILE = join(DATA_DIR, "app-state.json");
const USER_STORE_DIR = join(DATA_DIR, "users");

// 同一 owner 的写入必须串行，避免较早的异步写覆盖较新的状态。
const writeQueues = new Map<string, Promise<void>>();

// 该对象只用于首次初始化，不再充当进程内 Store。
const defaultState: AppState = {
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

// enqueueWrite
function enqueueWrite(
  key: string,
  operation:() => Promise<void>,
): Promise<void> {
  const previous = writeQueues.get(key) ?? Promise.resolve();
  // 新操作必须在 previous 之后执行
  const current = previous.catch(() => undefined).then(operation);
  writeQueues.set(key, current);

  return current.finally(() => {
    if (writeQueues.get(key) === current) writeQueues.delete(key);
  });
}

// 使用 file 模式，每个用户需要独立的状态文件
function stateFile(ownerKey = "default"): string {
  if (ownerKey === "default") return STORE_FILE;

  // encodeURIComponent：把斜杠、空格等特殊字符编码
  const safeKey = encodeURIComponent(ownerKey).replaceAll("%", "_");

  // data/user/<safe-user-id>.json   data/user/xxx-11.json
  return join(USER_STORE_DIR, `${safeKey}.json`);
}

function applyRuntimeModel(
  snapshot: AppState,
  model?: ModelIdentity,
): AppState {

  const activeAgent = 
    snapshot.agents.find((agent) => agent.status === "ACTIVE") ??
    snapshot.agents[0];

  if (model && activeAgent && !activeAgent.model) {
    activeAgent.model = `${model.provider}/${model.id}`;
  }

  return snapshot;
}

async function atomicWriteState(
  // filePath 是 stateFile() 为当前 owner 计算出的正式状态文件路径。
  filePath: string,
  // next 是本次需要完整保存的 AppState 快照。
  next: AppState,
): Promise<void> {
  // dirname(filePath) 只取父目录；recursive: true 允许父目录不存在时
  // 一次性创建整条目录，也允许目录已经存在而不报错。
  await mkdir(dirname(filePath), { recursive: true });

  // 不直接覆盖正式文件，而是在相同目录下生成一个唯一临时文件。
  // PID 区分进程，时间戳区分写入时刻，随机尾缀进一步避免同毫秒重名。
  // 临时文件与正式文件位于同一目录，也能让后面的 rename 保持在同一文件系统内。
  const temporaryFile = `${filePath}.${process.pid}.${Date.now()}.${Math.random().toString(36).slice(2)}.tmp`;

  try {
    // 第一步只写临时文件。即使进程在写入过程中退出，原正式文件仍保持完整。
    await writeFile(
      temporaryFile,
      // null, 2 表示用两个空格格式化 JSON；末尾换行方便人工查看和版本比较。
      `${JSON.stringify(next, null, 2)}\n`,
      {
        // 明确使用 UTF-8 保存中文等文本内容。
        encoding: "utf-8",
        // 0o600 表示只有当前系统用户能够读写新建的临时文件，
        // 避免状态、会话或审批信息被同一台机器上的其他账号读取。
        mode: 0o600,
      },
    );

    // 第二步才用完整的临时文件替换正式文件。
    // 同一文件系统中的 rename 是原子操作：读取方只会看到旧文件或新文件，
    // 不会看到只写了一半的 JSON，因此不会产生损坏的 AppState。
    await rename(temporaryFile, filePath);
  } catch (error) {
    // 写入或重命名失败时，只清理本次生成的精确临时文件。
    // force: true 允许临时文件尚未创建或已经不存在，清理过程不会覆盖原始错误。
    await rm(temporaryFile, { force: true }).catch(() => undefined);

    // 把原始错误继续抛给 writeState() 和接口层，不能把持久化失败伪装成成功。
    throw error;
  }
}

export async function readState(
  model?: ModelIdentity,
  ownerKey = "default",
): Promise<AppState> {
  
  const filePath = stateFile(ownerKey);

  if (usesMysqlStorage()) {
    const persisted = await readStateDocument<AppState>(ownerKey);
    
    if (persisted) {
      return applyRuntimeModel(clone(persisted), model);
    }
  }

  try {
    const local = JSON.parse(
      await readFile(filePath, "utf-8"),
    )as AppState;

    const snapshot = applyRuntimeModel(local, model);

    await writeState(snapshot, ownerKey)

    return clone(snapshot);
  } catch (error: any) {
    // ENDENT：当前 owner 没有这个json
    if (error?.code !== "ENDENT") {
      throw new Error(
        `读取 AppState 失败：${error?.message ?? String(error)}`,
      );
    }
  }

  const initinalState = applyRuntimeModel(clone(defaultState), model);

  await writeState(initinalState, ownerKey);
  return clone(initinalState);
}

export async function writeState(
  next: AppState,
  ownerKey = "default",
): Promise<void> {
    const snapshot = clone(next);

    if (usesMysqlStorage()) {
      await enqueueWrite(
        `mysql:state:${ownerKey}`,
        () => writeStateDocument(ownerKey, snapshot),
      );

      return;
    }

    const filePath = stateFile(ownerKey);

    await enqueueWrite(
      filePath,
      () => atomicWriteState(filePath, snapshot),
    );
}



// 下列 CRUD helper 保持前三节调用边界不变。
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

  // HTTP 请求体可能携带 id，因此保存真实 id，并在赋值结束后恢复。
  const stableId = record.id;
  // id 永远不允许被 patch 覆盖，只更新业务字段和更新时间。
  Object.assign(record, patch, { id: stableId, updatedAt: now() });
  return record;
}

export function deleteRecord<T extends { id: string }>(collection: T[], recordId: string) {
  const index = collection.findIndex((item) => item.id === recordId);
  if (index < 0) return false;
  // 原地修改集合，保证调用者持有的 AppState 引用仍然有效。
  collection.splice(index, 1);
  return true;
}

export function createMessage(role: ConversationMessage["role"], content: string) {
  return { id: id("msg"), role, content, createdAt: now() };
}

export function touchRecord(record: { updatedAt: string }) {
  record.updatedAt = now();
}
