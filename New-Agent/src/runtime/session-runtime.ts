import {
  // 根据模型、资源加载器、Tool 和 SessionManager 创建真正的 Pi AgentSession。
  createAgentSession,
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
  /** 为指定业务会话创建一个新的 AgentSession。 */
  async function createNewAgentSession(
    context: RuntimeContext,
    state: AppState,
    conversation: ConversationRecord,
    ownerId: string,
  ) {
    // 第一步：根据最新 Agent、Skill、Tool 配置计算白名单。
    // 不能沿用上一次结果，因为用户可能刚刚启用或禁用了某项能力。
    const executableTools = await options.applyRuntimePolicy(
      context,
      state,
      conversation.agentId,
    );

    // 第二步：发现 AppState 中所有可信 Skill 包声明的 Tool。
    // 这里负责“注册”，真正能否调用仍由 executableTools 和 guard 控制。
    const allSkillTools = await discoverSkillPackageTools(state.skills);
    const customTools = createSkillToolDefinitions(allSkillTools);

    // Pi SDK 创建 Session 时需要知道全部可注册 Tool 的名称。
    // Set 用于去重，避免 Skill Tool 与基础 Tool 重名时重复注册。
    const allToolNames = [
      ...new Set([
        ...options.coreTools,
        ...allSkillTools.map((item) => item.manifest.name),
      ]),
    ];

    // 第三步：由存储模块返回统一的 SessionManager。
    // MySQL 模式会从 pi_sessions 恢复 entries；file 模式会创建或打开 JSONL。
    const sessionManager = await conversationSessionManager(
      ownerId,
      conversation,
    );

    // 第四步：把模型、Loader、SessionManager 和 Tool 交给 Pi SDK，
    // 创建本次业务会话真正使用的 AgentSession。
    const { session } = await createAgentSession({
      // Tool 执行和相对路径解析都以项目根目录为工作目录。
      cwd: process.cwd(),
      model: options.model,
      modelRuntime: options.modelRuntime,
      // loader 已经绑定当前用户最新的系统提示词、Skill 和扩展。
      resourceLoader: context.loader,
      sessionManager,
      // customTools 是 Skill 包提供的自定义 Tool 实现。
      customTools,
      // tools 是基础 Tool 与 Skill Tool 的完整名称集合。
      tools: allToolNames,
    });

    // 第五步：先绑定 guard、audit 等扩展，再设置本轮白名单。
    // 这样模型最终只能看到并调用策略允许的 Tool。
    await session.bindExtensions({ mode: "print" });
    session.setActiveToolsByName(executableTools);

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

  // HTTP 层只依赖这两个公开函数：获取/切换 Session，以及保存 Pi entries。
  return {
    getConversationSession,
    persistPiSession,
  };
}