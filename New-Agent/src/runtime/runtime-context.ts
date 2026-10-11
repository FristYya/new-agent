import {
  createAgentSession,
  DefaultResourceLoader,
  getAgentDir,
  type Skill,
} from "@earendil-works/pi-coding-agent";
import type { AuthUser } from "../auth-users.ts";
import { auditExtension } from "../extensions/audit.ts";
import { guardExtension } from "../extensions/guard.ts";
import type { ToolPolicyDecision } from "../policy/tool-policy.ts";
import type { ApprovalRecord } from "../store.ts";
import { ApprovalRegistry } from "./approval-registry.ts";

/** Pi SDK 创建出来的真实会话类型。 */
export type AgentSession = Awaited<
  ReturnType<typeof createAgentSession>
>["session"];

/**
 * 一个登录用户对应一个 RuntimeContext。
 *
 * AppState 和 Pi entries 已经持久化到 file/MySQL；这里仅保存当前进程运行时
 * 需要的对象，例如 ResourceLoader、当前活跃 Session 和待处理审批。
 */
export type RuntimeContext = {
  user: AuthUser;
  loader: DefaultResourceLoader;
  session?: AgentSession;
  approvalRegistry: ApprovalRegistry;
  approvalNotifier?: (approval: ApprovalRecord & { token: string }) => void;
  activeRunId?: string;
  activeConversationId?: string;
  activeAgentId?: string;
  runtimeAllowedTools: Set<string>;
  runtimeDisabledSkills: Set<string>;
  runtimePiSkills: Skill[];
  runtimeSkillDiagnostics: unknown[];
  runtimeSkillToolNames: Set<string>;
  runtimeSkillOwnedTools: Set<string>;
};

type RuntimeRegistryOptions = {
  /** Pi 内置 Tool 的初始集合；正式集合会在每轮运行前重新计算。 */
  coreTools: readonly string[];
  /** 读取第一节创建的系统提示词。 */
  readSystemPrompt: () => string;
  /** 根据当前 RuntimeContext 生成动态 Skill/Tool 提示。 */
  buildRuntimePrompt: (context: RuntimeContext) => string;
  /** guard 拦截高风险 Tool 后，把审批交还给 server 的业务流程。 */
  requestToolApproval: (
    context: RuntimeContext,
    event: any,
    decision: ToolPolicyDecision,
  ) => Promise<boolean>;
};

/**
 * 创建按用户隔离的运行时注册表。
 *
 * 注册表只存在于当前 Node.js 进程中。服务重启后，Session 会由
 * session-storage.ts 根据持久化的 entries 重新恢复。
 */
export function createRuntimeRegistry(options: RuntimeRegistryOptions) {
  // key 是用户 ID；value 是该用户独占的运行时对象。
  const runtimes = new Map<string, RuntimeContext>();

  function createRuntimeContext(user: AuthUser): RuntimeContext {
    // 先创建 context，再让 loader 的回调闭包引用它。
    // 这样每次 reload() 都能读取本轮最新的 Skill/Tool 状态。
    const context = {
      user,
      approvalRegistry: new ApprovalRegistry(),
      runtimeAllowedTools: new Set<string>(options.coreTools),
      runtimeDisabledSkills: new Set<string>(),
      runtimePiSkills: [],
      runtimeSkillDiagnostics: [],
      runtimeSkillToolNames: new Set<string>(),
      runtimeSkillOwnedTools: new Set<string>(),
    } as unknown as RuntimeContext;

    context.loader = new DefaultResourceLoader({
      cwd: process.cwd(),
      agentDir: getAgentDir(),
      // 不扫描 Pi 默认 Skill 目录，只使用 AppState 中显式启用的 Skill。
      noSkills: true,
      systemPromptOverride: options.readSystemPrompt,
      appendSystemPromptOverride: () => [
        options.buildRuntimePrompt(context),
      ],
      skillsOverride: () => ({
        skills: context.runtimePiSkills,
        diagnostics: context.runtimeSkillDiagnostics as any[],
      }),
      extensionFactories: [
        // guard 每次执行 Tool 前读取最新白名单，并在需要时发起审批。
        (pi: any) => guardExtension(
          pi,
          () => ({
            allowedTools: context.runtimeAllowedTools,
            disabledSkills: context.runtimeDisabledSkills,
          }),
          (event, decision) =>
            options.requestToolApproval(context, event, decision),
        ),
        // audit 继续记录 Tool 调用结果，不与 HTTP 路由耦合。
        auditExtension,
      ],
    });

    return context;
  }

  /** 同一 user.id 复用同一个 RuntimeContext；不同用户永不共享。 */
  function getRuntime(user: AuthUser): RuntimeContext {
    let runtime = runtimes.get(user.id);
    if (!runtime) {
      runtime = createRuntimeContext(user);
      runtimes.set(user.id, runtime);
    }
    return runtime;
  }

  /** 服务退出时统一释放当前进程缓存的全部 Pi Session。 */
  function disposeAll(): void {
    for (const runtime of runtimes.values()) {
      runtime.session?.dispose();
      runtime.session = undefined;
    }
    runtimes.clear();
  }

  return { getRuntime, disposeAll };
}

/**
 * 配置变化或会话切换时，仅释放内存 Session。
 * 已经保存到 JSONL/MySQL 的 entries 不会被删除。
 */
export function resetSession(
  runtime: RuntimeContext,
  reason: string,
): void {
  console.log(`重建 Session：${reason}`);
  runtime.session?.dispose();
  runtime.session = undefined;
  runtime.activeConversationId = undefined;
  runtime.activeAgentId = undefined;
}
