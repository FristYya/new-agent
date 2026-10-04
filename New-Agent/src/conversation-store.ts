import { randomUUID } from "node:crypto";
import { off } from "node:process";

// 我们只展示 user 消息和 assistant 消息
export type ChatRole = "user" | "assistant";

// 单条消息展示的数据结构
export interface ChatMessage {
    id: string;
    role: ChatRole;
    content: string;
    createdAt: string;
}

// 一个应用会话的页面投影
export interface ConversationRecord {
    id: string;
    title: string;
    createdAt: string;
    updatedAt: string;
    messages: ChatMessage[];
}

// 定义map，key 是 conversationId，value 是会话展示记录
// todo: 内存存储，服务重启后 map 清空，后期用 mysql 改造
const conversations = new Map<string, ConversationRecord>();

// 对外返回 clone 副本， 避免调用方直接修改 messages 数组污染真实数据
function cloneConversation(record: ConversationRecord): ConversationRecord {
    return {
        ...record,
        messages: record.messages.map((message) => ({...message})),
    };
}
 
// 返回会话列表，根据时间倒排
export function listConversations(): ConversationRecord[] {
    return [...conversations.values()]
        .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt))
        .map(cloneConversation);
}

// 创建会话
export function createConversation(title = "新会话"): ConversationRecord {
    // 一次创建使用同一个时间，保证 createdAt 和 updatedAt 初始化一致
    const now = new Date().toISOString();
    const record: ConversationRecord = {
        id: randomUUID(),
        title,
        createdAt: now,
        updatedAt: now,
        messages: [],
    };

    // 先写入内存 map，再返回副本
    conversations.set(record.id, record);
    return cloneConversation(record);
}

// 根据 conversationId 查询会话
export function getConversation(id: string): ConversationRecord | undefined {
    const record = conversations.get(id);
    return record ? cloneConversation(record) : undefined;
}

// 向指定会话追加消息
export function appendMessage(
    conversationId: string,
    role: ChatRole,
    content: string,
): ConversationRecord {
    // 校验会话，避免把消息写入有问题的 conversationId
    const record = conversations.get(conversationId);
    if (!record) throw new Error("会话不存在");

    const now = new Date().toISOString();
    record.messages.push({
        id: randomUUID(),
        role,
        content,
        createdAt: now,
    });

    // 仅用第一条用户消息生成标题， 防止后续追问不断改变会话的标题
    if (role === "user" && record.title === "新会话") {
        record.title = content.slice(0, 24) || "新会话";
    }

    record.updatedAt = now;
    return cloneConversation(record);
}

