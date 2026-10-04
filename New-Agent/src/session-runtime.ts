import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  createAgentSession,
  DefaultResourceLoader,
  getAgentDir,
  ModelRuntime,
  SessionManager,
} from "@earendil-works/pi-coding-agent";

// 从 SDK 工厂函数的真实返回类型推导 Session，避免手写类型随 SDK 升级失效。
type PiSession = Awaited<ReturnType<typeof createAgentSession>>["session"];

// 一个 conversationId 对应一个长生命周期 AgentSession。
// 同一会话复用历史，不同会话使用不同实例，避免上下文串线。
const sessions = new Map<string, PiSession>();

// 模型运行时只初始化一次。
// 保存 Promise 还能让多个并发请求共享同一次异步初始化过程。
const modelRuntimePromise = ModelRuntime.create();

// 页面启动时尚未选择模型；首次创建 Session 后会更新真实模型名称。
let activeModelLabel = "等待首次对话";

// 每次创建 Session 时从项目目录读取提示词，方便课程中直接修改和版本管理。
function readSystemPrompt(): string {
  const promptPath = join(process.cwd(), "prompts", "new-agent.md");
  return readFileSync(promptPath, "utf8");
}

// 从 Pi 当前可用模型中选出本项目要使用的模型。
async function selectModel() {
  const modelRuntime = await modelRuntimePromise;
  const availableModels = await modelRuntime.getAvailable();

  // 没有可用模型通常意味着尚未登录或没有配置模型凭据。
  if (availableModels.length === 0) {
    throw new Error("没有可用模型，请先完成 Pi 模型与凭据配置");
  }

  // AGENT_MODEL 支持 model-id，也支持更明确的 provider/model-id。
  const requestedModel = process.env.AGENT_MODEL?.trim();
  const model = requestedModel
    ? availableModels.find(
        (item) =>
          item.id === requestedModel ||
          item.provider + "/" + item.id === requestedModel,
      )
    : availableModels[0];

  // 配置了模型却没有匹配时立即报错，不能静默切换到其他模型。
  if (!model) {
    throw new Error("指定模型不可用：" + requestedModel);
  }

  activeModelLabel = model.provider + "/" + model.id;
  return { modelRuntime, model };
}

// 获取已有 Session；只有 conversationId 第一次出现时才创建新 Session。
export async function getOrCreateSession(
  conversationId: string,
): Promise<PiSession> {
  const existing = sessions.get(conversationId);
  if (existing) return existing;

  const { modelRuntime, model } = await selectModel();

  // ResourceLoader 负责加载系统提示词以及 Pi 的资源配置。
  const resourceLoader = new DefaultResourceLoader({
    cwd: process.cwd(),
    agentDir: getAgentDir(),
    systemPromptOverride: () => readSystemPrompt(),
    // 第一节没有额外追加提示词，明确返回空数组。
    appendSystemPromptOverride: () => [],
  });

  // 先扫描资源并完成诊断；失败时不要创建一个配置不完整的 Session。
  await resourceLoader.reload();

  const { session } = await createAgentSession({
    cwd: process.cwd(),
    modelRuntime,
    model,
    resourceLoader,
    // 第一节只要求进程内多轮记忆，重启恢复留到持久化课程。
    sessionManager: SessionManager.inMemory(process.cwd()),
    // 禁用全部工具，确保第一节只关注会话和流式事件。
    noTools: "all",
  });

  // 创建成功后再缓存，避免把初始化失败的 Session 放入 Map。
  sessions.set(conversationId, session);
  return session;
}

// 提供给 /api/state，便于前端展示当前真实模型。
export function getActiveModelLabel(): string {
  return activeModelLabel;
}

// 取消当前生成，但保留 Session 与已经形成的多轮上下文。
export async function abortConversation(
  conversationId: string,
): Promise<boolean> {
  const session = sessions.get(conversationId);
  if (!session) return false;
  await session.abort();
  return true;
}

// Node.js 进程退出前释放所有监听器和模型相关资源。
export function disposeAllSessions(): void {
  for (const session of sessions.values()) {
    session.dispose();
  }
  sessions.clear();
}