// Node.js 异步文件 API：仅在从旧 JSONL 迁移到 MySQL 时读取历史文件。
import { readFile } from "node:fs/promises";
// path 用于拼接目录、解析绝对路径以及校验文件是否越过用户目录。
import path from "node:path";
import {
  // 把 Pi SDK 的 JSONL 文本还原成完整的 Session entries 数组。
  parseSessionEntries,
  // SessionManager 是 AgentSession 读写上下文树的统一存储接口。
  SessionManager,
  // FileEntry 表示 Session 文件中的 header、message、branch 等记录。
  type FileEntry,
} from "@earendil-works/pi-coding-agent";
import {
  // 从 pi_sessions 表读取指定用户、指定会话的上下文树。
  readPiSession,
  // 判断当前配置究竟使用 MySQL 还是本地 JSONL。
  usesMysqlStorage,
  // 把完整 Session entries 写入 pi_sessions 表。
  writePiSession,
} from "../storage/mysql.ts";
import type { ConversationRecord } from "../store.ts";
import type { AgentSession } from "./runtime-context.ts";

/**
 * 为每个用户计算受控的 Pi JSONL 目录。
 *
 * file 模式下，每个用户拥有自己的 data/pi-sessions/<ownerId> 目录，
 * 因此不同账号的会话文件不会混在一起。
 */
export function sessionDirectory(ownerId: string): string {
  // ownerId 来自用户数据，不能直接当成路径使用。
  // 这里只保留字母、数字、下划线和短横线，其余字符统一替换成下划线。
  // 因此类似 ../other-user 的内容无法逃离 data/pi-sessions。
  const safeOwnerId = ownerId.replace(/[^a-zA-Z0-9_-]/g, "_");
  return path.join(process.cwd(), "data", "pi-sessions", safeOwnerId);
}

/**
 * 根据当前存储模式创建或恢复 Pi SessionManager。
 *
 * file 模式直接使用 Pi SDK 的 JSONL；MySQL 模式把完整 entries 树放在
 * pi_sessions 表中，并在首次切换存储方式时兼容导入旧 JSONL。
 * 上层 session-runtime.ts 不需要知道具体存储后端，只消费统一的 SessionManager。
 */
export async function conversationSessionManager(
  // ownerId 是当前登录用户的 user.id，也是会话存储的第一层隔离键。
  ownerId: string,
  // conversation 提供业务会话 ID，以及旧 JSONL 或 MySQL Session 的定位信息。
  conversation: ConversationRecord,
): Promise<SessionManager> {
  // 即使当前使用 MySQL，也先计算受控目录，供旧 JSONL 迁移时做安全校验。
  const sessionDir = sessionDirectory(ownerId);

  if (usesMysqlStorage()) {
    // MySQL 模式先按 ownerId + conversation.id 查询。
    // 这两个字段共同保证用户 A 无法恢复用户 B 的会话树。
    const stored = await readPiSession(ownerId, conversation.id);
    if (stored) {
      // 数据库已有记录时，用保存的 sessionId 和 entries 恢复内存 SessionManager。
      // inMemory() 不会再写 JSONL，本轮结束后由 persistPiSession() 回写数据库。
      return SessionManager.inMemory(
        process.cwd(),
        { id: stored.sessionId },
        stored.entries,
      );
    }

    // 数据库没有记录，但 ConversationRecord 仍保存 piSessionFile，说明项目可能
    // 刚从 file 模式切到 MySQL；此时尝试一次性导入旧 JSONL。
    if (conversation.piSessionFile) {
      // 把记录中的相对路径解析为绝对路径，便于进行目录边界校验。
      const requested = path.resolve(process.cwd(), conversation.piSessionFile);
      const relativePath = path.relative(sessionDir, requested);

      // 正常文件的相对路径应位于 sessionDir 内部。
      // 以 .. 开头或仍是绝对路径都代表它逃出了当前用户目录，必须拒绝读取。
      if (relativePath.startsWith("..") || path.isAbsolute(relativePath)) {
        throw new Error("会话关联的 Pi Session 路径非法");
      }

      try {
        // readFile 得到 JSONL 文本，再由 Pi SDK 解析成有顺序的 entries。
        const entries = parseSessionEntries(await readFile(requested, "utf-8"));
        // type === "session" 的记录是 header，里面保存稳定的 Pi session id。
        const header = entries.find((entry) => entry.type === "session");
        if (header) {
          // 先把完整 entries 写入 MySQL，确保迁移数据已经安全落库。
          await writePiSession(ownerId, conversation.id, header.id, entries);
          // ConversationRecord 改为记录 MySQL 中的 session id。
          conversation.piSessionId = header.id;
          // 迁移完成后清空文件定位信息，后续不再重复读取旧 JSONL。
          conversation.piSessionFile = undefined;
          return SessionManager.inMemory(
            process.cwd(),
            { id: header.id },
            entries,
          );
        }
      } catch (error: any) {
        // ENOENT 只表示旧文件已经不存在，此时可以继续创建全新会话。
        // 权限错误、JSONL 损坏等其他异常不能被吞掉，必须交给上层处理。
        if (error?.code !== "ENOENT") throw error;
      }
    }

    // MySQL 中没有历史记录，也没有可迁移文件时，先创建空的内存 SessionManager。
    // 第一轮对话结束后，persistPiSession() 会把生成的 header 和 entries 写入数据库。
    return SessionManager.inMemory(process.cwd());
  }

  // 下面是 file 模式：ConversationRecord 没有关联旧文件时，
  // 让 Pi SDK 在当前用户的受控目录中创建新的 JSONL。
  if (!conversation.piSessionFile) {
    return SessionManager.create(process.cwd(), sessionDir);
  }

  // 已有关联文件时，同样先解析成绝对路径并校验目录边界。
  const requested = path.resolve(process.cwd(), conversation.piSessionFile);
  const relativePath = path.relative(sessionDir, requested);
  if (relativePath.startsWith("..") || path.isAbsolute(relativePath)) {
    throw new Error("会话关联的 Pi Session 路径非法");
  }
  // 路径合法后，交给 Pi SDK 打开原 JSONL 并恢复上下文树。
  return SessionManager.open(requested, sessionDir, process.cwd());
}

/**
 * 把当前 AgentSession 的完整 entries 树保存到 MySQL。
 * 返回 session id，供调用方更新业务会话的定位信息。
 */
export async function persistPiSession(
  ownerId: string,
  conversation: ConversationRecord,
  session: AgentSession,
): Promise<string | undefined> {
  // file 模式由 Pi SDK 自己持续写 JSONL，不需要在这里重复保存。
  if (!usesMysqlStorage()) return undefined;

  // AgentSession 内部持有本轮实际使用的 SessionManager。
  const manager = session.sessionManager;
  // header 保存 session id 等根信息；还没产生 header 时没有可持久化内容。
  const header = manager.getHeader();
  if (!header) return undefined;

  // getEntries() 只返回 header 之后的记录，不包含 session header。
  // 恢复完整树时必须把 header 放回数组第一项，否则会丢失 Session 根节点。
  const entries: FileEntry[] = [header, ...manager.getEntries()];
  // 使用 ownerId + conversation.id 做隔离键，把本轮完整快照写入 MySQL。
  await writePiSession(ownerId, conversation.id, header.id, entries);
  // 同步更新业务会话中的 Pi session id，随后由 writeState() 保存 AppState。
  conversation.piSessionId = header.id;
  return header.id;
}