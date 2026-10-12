import type {
  SessionEntry,
  SessionTreeNode,
} from "@earendil-works/pi-coding-agent";
import type { ConversationMessage } from "../store.ts";
import type { AgentSession } from "./runtime-context.ts";

/** 前端展示的一棵安全会话树；不返回 Tool 参数、结果等原始敏感数据。 */
export type SessionTreeViewNode = {
  id: string;
  parentId: string | null;
  type: string;
  role?: string;
  title: string;
  label?: string;
  timestamp: string;
  // technical=true 的节点默认在前端精简视图隐藏，但仍保留在完整树响应中。
  technical: boolean;
  active: boolean;
  leaf: boolean;
  branchPoint: boolean;
  children: SessionTreeViewNode[];
};

export type SessionTreeSnapshot = {
  sessionId: string;
  leafId: string | null;
  nodes: SessionTreeViewNode[];
};

/** 把 Pi 消息的 string/内容块统一提取为可展示文本。 */
function contentText(content: unknown): string {
  if (typeof content === "string") return content.trim();
  if (!Array.isArray(content)) return "";

  return content
    .filter((block): block is { type: string; text?: string } =>
      Boolean(block) && typeof block === "object" && "type" in block,
    )
    .filter((block) => block.type === "text" && typeof block.text === "string")
    .map((block) => block.text)
    .join("")
    .trim();
}

/** 限制树节点预览长度，避免长回答撑满树面板。 */
function preview(value: string, fallback: string): string {
  const normalized = value.replace(/\s+/g, " ").trim();
  if (!normalized) return fallback;
  return normalized.length > 90
    ? `${normalized.slice(0, 87)}...`
    : normalized;
}

function entryTitle(entry: SessionEntry): { role?: string; title: string } {
  if (entry.type === "message") {
    const role = String(entry.message.role);
    // AgentMessage 还可能是 BashExecutionMessage 等无 content 的消息，
    // 因此必须先做属性收窄，不能假设所有 message 都包含正文。
    const text = "content" in entry.message
      ? contentText(entry.message.content)
      : "";
    const roleLabel = {
      user: "用户",
      assistant: "助手",
      toolResult: "Tool 结果",
      system: "系统",
    }[role] ?? role;
    return { role, title: preview(text, roleLabel) };
  }

  if (entry.type === "branch_summary") {
    return { title: preview(entry.summary, "分支摘要") };
  }
  if (entry.type === "compaction") {
    return { title: preview(entry.summary, "上下文压缩") };
  }
  if (entry.type === "custom_message") {
    return { title: preview(contentText(entry.content), "自定义消息") };
  }

  const labels: Record<string, string> = {
    model_change: "切换模型",
    thinking_level_change: "调整思考级别",
    usage: "用量记录",
    context_edit: "上下文编辑",
    label: "节点标签",
    session_info: "会话信息",
    custom: "扩展状态",
  };
  return { title: labels[entry.type] ?? entry.type };
}

/**
 * 判断节点是否属于 Pi 的运行细节。
 *
 * 默认树只需要帮助用户理解“在哪轮对话产生了分支”，因此隐藏：
 * - 系统、模型、用量和思考级别等状态节点；
 * - Tool 结果；
 * - 没有正文或仍在发起 Tool 调用的中间助手消息。
 * 完整节点仍会返回给前端，可通过“显示技术节点”开关查看。
 */
function isTechnicalEntry(entry: SessionEntry): boolean {
  if (entry.type === "message") {
    if (entry.message.role === "user") return false;
    if (entry.message.role !== "assistant") return true;

    if (!("content" in entry.message)) return true;
    const content = entry.message.content;
    const text = contentText(content);
    const hasToolCall = Array.isArray(content) && content.some((block) => {
      if (!block || typeof block !== "object" || !("type" in block)) {
        return false;
      }
      return block.type === "toolCall";
    });

    // 只有真正面向用户的最终文本回复进入默认精简树。
    return !text || hasToolCall;
  }

  return entry.type !== "branch_summary" && entry.type !== "compaction";
}

function mapTreeNode(
  node: SessionTreeNode,
  activeIds: Set<string>,
  leafId: string | null,
): SessionTreeViewNode {
  const display = entryTitle(node.entry);
  return {
    id: node.entry.id,
    parentId: node.entry.parentId,
    type: node.entry.type,
    role: display.role,
    title: display.title,
    label: node.label,
    timestamp: node.entry.timestamp,
    technical: isTechnicalEntry(node.entry),
    active: activeIds.has(node.entry.id),
    leaf: node.entry.id === leafId,
    branchPoint: node.children.length > 1,
    children: node.children.map((child) =>
      mapTreeNode(child, activeIds, leafId),
    ),
  };
}

/** 生成前端所需的会话树快照。 */
export function buildSessionTreeSnapshot(
  session: AgentSession,
): SessionTreeSnapshot {
  const manager = session.sessionManager;
  const leafId = manager.getLeafId();
  const activeIds = new Set(manager.getBranch().map((entry) => entry.id));

  return {
    sessionId: manager.getSessionId(),
    leafId,
    nodes: manager
      .getTree()
      .map((node) => mapTreeNode(node, activeIds, leafId)),
  };
}

/**
 * 从 Pi 当前叶子到根节点的路径重建聊天区。
 * ConversationRecord.messages 只是页面投影，真正模型上下文仍以 Pi entries 为准。
 */
export function buildActiveConversationMessages(
  session: AgentSession,
): ConversationMessage[] {
  const messages: ConversationMessage[] = [];

  for (const entry of session.sessionManager.getBranch()) {
    if (entry.type === "message") {
      if (entry.message.role !== "user" && entry.message.role !== "assistant") {
        continue;
      }
      const content = contentText(entry.message.content);
      if (!content) continue;
      messages.push({
        id: `pi_${entry.id}`,
        role: entry.message.role,
        content,
        createdAt: entry.timestamp,
      });
      continue;
    }

    // 分支摘要会进入模型上下文，也应让用户知道切换时继承了哪些信息。
    if (entry.type === "branch_summary") {
      messages.push({
        id: `pi_${entry.id}`,
        role: "assistant",
        content: `**分支摘要**\n\n${entry.summary}`,
        createdAt: entry.timestamp,
      });
    }
  }

  return messages;
}
