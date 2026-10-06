import type { Model } from "@mariozechner/pi-ai";
import { Mode } from "fs";

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
    // 当前只开放安全的只读工具。
    toolNames: ["read", "grep", "find", "ls"],
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
  // bash/write/edit 先登记但禁用，后续完成审批后再开放。
  tools: [
    ["read", "读取文件", "filesystem", "read", "ENABLED"],
    ["grep", "搜索文本", "search", "read", "ENABLED"],
    ["find", "查找文件", "search", "read", "ENABLED"],
    ["ls", "列出目录", "filesystem", "read", "ENABLED"],
    ["bash", "执行命令", "shell", "execute", "DISABLED"],
    ["write", "写入文件", "filesystem", "write", "DISABLED"],
    ["edit", "编辑文件", "filesystem", "write", "DISABLED"],
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
};

export async function readState(model?: Model<any>, _tools?: unknown[]) {
    const snapshot = clone(state);
    if (model) snapshot.agents[0].model = model.id;
    return snapshot;
}

export async function writeState(next: AppState) {
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
) {
  const record = collection.find((item) => item.id === recordId);
  if (!record) return null;
  // id 永远不允许被 patch 覆盖，只更新业务字段和时间。
  Object.assign(record, patch, { updatedAt: now() });
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