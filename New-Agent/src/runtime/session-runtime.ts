import {
  // 根据模型、资源加载器、Tool 和 SessionManager 创建真正的 Pi AgentSession。
  createAgentSession,
  SessionManager,
  // ModelRuntime 负责发现模型，并提供模型运行时能力。
  type ModelRuntime,
} from "@earendil-works/pi-coding-agent";
import {
  // 把发现到的 Skill Tool 转成 Pi SDK 可以注册的 Tool 定义。
  createSkillToolDefinitions,
  // 扫描当前 AppState 中 Skill 包声明的全部 Tool。
  discoverSkillPackageTools,
} from "../skill-tools.ts";
// 只用于决定 ConversationRecord 应保存 piSessionId 还是 piSessionFile。
import { usesMysqlStorage } from "../storage/mysql.ts";
import type { AppState, ConversationRecord } from "../store.ts";
import type { RuntimeContext } from "./runtime-context.ts";
import {
  // 根据 file/MySQL 模式创建或恢复统一的 SessionManager。
  conversationSessionManager,
  // 一轮执行结束后，把完整 Pi entries 保存到 MySQL。
  persistPiSession,
} from "./session-storage.ts";
import {
  buildActiveConversationMessages,
  buildSessionTreeSnapshot,
} from "./session-tree.ts";

/**
 * 服务启动时最终选中的模型类型。
 *
 * getAvailable() 是异步函数，所以先用 ReturnType 获取 Promise，
 * 再用 Awaited 解开，最后用 [number] 取得模型数组中的单项类型。
 */
type SelectedModel = Awaited<
  ReturnType<ModelRuntime["getAvailable"]>
>[number];

/** 创建 SessionRuntime 时由 server.ts 注入的稳定依赖。 */
type SessionRuntimeOptions = {
  // Pi SDK 自带的基础 Tool 名称；Skill Tool 会在创建 Session 时追加。
  coreTools: readonly string[];
  // 服务启动后已经完成校验并选中的模型。
  model: SelectedModel;
  // 与模型提供商通信的运行时对象。
  modelRuntime: ModelRuntime;
  // 根据当前用户、AppState 和 Agent 计算本轮真正允许执行的 Tool 白名单。
  applyRuntimePolicy: (
    context: RuntimeContext,
    state: AppState,
    agentId?: string,
  ) => Promise<string[]>;
};

/**
 * 创建会话运行时服务。
 *
 * 模型、ModelRuntime 和策略函数在服务启动时注入一次；HTTP 路由只需要传入
 * 当前用户的 RuntimeContext、AppState 和 ConversationRecord，
 * 不再关心 Pi Session 的创建、切换和存储细节。
 */
export function createSessionRuntime(options: SessionRuntimeOptions) {
  async function buildAgentSession(
    context: RuntimeContext,
    state: AppState,
    agentId: string | undefined,
    sessionManager: SessionManager,
    safeEvaluation = false,
  ) {
    const executableTools = await options.applyRuntimePolicy(context, state, agentId);
    const allSkillTools = await discoverSkillPackageTools(state.skills);
    const customTools = createSkillToolDefinitions(allSkillTools);
    const allToolNames = [...new Set([...options.coreTools, ...allSkillTools.map((item) => item.manifest.name)])];
    const { session } = await createAgentSession({
      cwd: process.cwd(), model: options.model, modelRuntime: options.modelRuntime,
      resourceLoader: context.loader, sessionManager, customTools, tools: allToolNames,
    });
    await session.bindExtensions({ mode: "print" });
    const activeTools = safeEvaluation
      ? executableTools.filter((name) => ["read", "grep", "find", "ls"].includes(name))
      : executableTools;
    session.setActiveToolsByName(activeTools);
    return session;
  }

  /** 为指定业务会话创建一个新的 AgentSession。 */
  async function createNewAgentSession(
    context: RuntimeContext,
    state: AppState,
    conversation: ConversationRecord,
    ownerId: string,
  ) {
    // 由存储模块返回统一的 SessionManager。
    // MySQL 模式会从 pi_sessions 恢复 entries；file 模式会创建或打开 JSONL。
    const sessionManager = await conversationSessionManager(
      ownerId,
      conversation,
    );

    // 第四步：把模型、Loader、SessionManager 和 Tool 交给 Pi SDK，
    // 创建本次业务会话真正使用的 AgentSession。
    const session = await buildAgentSession(
      context, state, conversation.agentId, sessionManager,
    );

    // 第六步：业务会话只保存当前存储模式需要的定位信息，
    // 避免同时保留 file 和 MySQL 两种来源而产生歧义。
    if (usesMysqlStorage()) {
      // MySQL 使用稳定的 Pi session id 定位 entries 树。
      conversation.piSessionId = session.sessionManager.getSessionId();
      conversation.piSessionFile = undefined;
    } else {
      // file 模式记录 Pi SDK 创建或打开的 JSONL 文件路径。
      conversation.piSessionFile = session.sessionFile;
      conversation.piSessionId = undefined;
    }

    // 记录这份内存 Session 当前属于哪个业务会话和 Agent。
    // 下一次请求会通过这两个字段判断能否安全复用。
    context.activeConversationId = conversation.id;
    context.activeAgentId = conversation.agentId;
    return session;
  }

  /**
   * 获取当前业务会话对应的 AgentSession。
   * 可以复用时更新权限后复用；不能复用时释放旧 Session 并恢复目标会话。
   */
  async function getConversationSession(
    context: RuntimeContext,
    state: AppState,
    conversation: ConversationRecord,
    ownerId: string,
  ) {
    // 只有 Session 已存在、conversationId 相同、agentId 也相同，
    // 才说明内存中的模型上下文确实属于本次请求。
    const matchesActiveSession =
      context.session &&
      context.activeConversationId === conversation.id &&
      context.activeAgentId === conversation.agentId;

    if (matchesActiveSession) {
      // 会话归属虽然没有变化，但管理端可能修改过 Skill 或 Tool 配置，
      // 因此每一轮仍要重新计算白名单，不能直接沿用上一轮权限。
      const executableTools = await options.applyRuntimePolicy(
        context,
        state,
        conversation.agentId,
      );
      context.session!.setActiveToolsByName(executableTools);
      return context.session!;
    }

    // 当前缓存属于其他会话或其他 Agent 时，先释放旧内存 Session。
    // dispose() 不删除 JSONL/MySQL 中的 entries，只释放对象和事件订阅。
    context.session?.dispose();
    context.session = undefined;
    // 同时清空归属标记，避免创建新 Session 失败后留下错误状态。
    context.activeConversationId = undefined;
    context.activeAgentId = undefined;

    // createNewAgentSession() 会调用存储模块，恢复目标会话自己的 entries，
    // 然后重新装配该会话对应的模型、Skill、Tool 和扩展。
    context.session = await createNewAgentSession(
      context,
      state,
      conversation,
      ownerId,
    );
    return context.session;
  }

  /** 读取指定业务会话的 Pi 树；返回值已去除 Tool 参数等敏感原始内容。 */
  async function getConversationTree(
    context: RuntimeContext,
    state: AppState,
    conversation: ConversationRecord,
    ownerId: string,
  ) {
    const session = await getConversationSession(
      context,
      state,
      conversation,
      ownerId,
    );
    const tree = buildSessionTreeSnapshot(session);

    // 首次打开一条空会话的树也会创建 Pi Session。
    // 在这里同步 piSessionFile/piSessionId/piLeafId，避免重启后丢失定位信息。
    await persistPiSession(ownerId, conversation, session);
    return tree;
  }

  /**
   * 使用 Pi SDK 在同一个 Session 内切换树节点。
   * 选择历史节点后，下一次 prompt 会从该节点继续，从而自然形成新分支。
   */
  async function navigateConversationTree(
    context: RuntimeContext,
    state: AppState,
    conversation: ConversationRecord,
    ownerId: string,
    targetId: string,
    summarize: boolean,
  ) {
    const session = await getConversationSession(
      context,
      state,
      conversation,
      ownerId,
    );

    // 先在当前用户、当前业务会话的 SessionManager 中校验节点，
    // 防止把其他用户或其他会话的 entry id 传给 SDK。
    if (!session.sessionManager.getEntry(targetId)) {
      throw new Error("会话树节点不存在");
    }

    const result = await session.navigateTree(targetId, { summarize });
    if (result.cancelled) {
      return {
        cancelled: true,
        editorText: result.editorText,
        tree: buildSessionTreeSnapshot(session),
      };
    }

    // 页面消息只是 Pi 当前分支的投影。切换后立即重建，不能继续展示旧分支。
    conversation.messages = buildActiveConversationMessages(session);
    conversation.updatedAt = new Date().toISOString();

    // MySQL 模式保存完整 entries；两种模式都会把当前 leaf 写回 piLeafId。
    await persistPiSession(ownerId, conversation, session);

    return {
      cancelled: false,
      editorText: result.editorText,
      tree: buildSessionTreeSnapshot(session),
    };
  }

  // HTTP 层通过这些函数使用 Session，不直接操作 Pi 的内部树结构。
  return {
    getConversationSession,
    getConversationTree,
    navigateConversationTree,
    persistPiSession,
    /** 为评测创建不写入业务会话的独立 Session；只开放只读内置 Tool。 */
    createEvaluationSession: (
      context: RuntimeContext,
      state: AppState,
      agentId?: string,
    ) => buildAgentSession(
      context,
      state,
      agentId,
      SessionManager.inMemory(process.cwd()),
      true,
    ),
  };
}
