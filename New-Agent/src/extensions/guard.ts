import {
  evaluateToolCall,
  type RuntimeToolPolicy,
  type ToolPolicyDecision,
} from "../policy/tool-policy.ts";

/**
 * Pi Extension：在 Tool 真正执行之前统一执行安全策略。
 *
 * Pi 把一次 Tool 调用交给本扩展后，扩展只会返回两类结果：
 * - `undefined`：不拦截，Pi 可以继续执行原来的 Tool；
 * - `{ block: true, reason }`：阻断 Tool，并把原因交还给 Pi 组织回复。
 */
export function guardExtension(
  // Pi 的 Extension API，通过 pi.on() 注册 Tool 调用前的监听器。
  pi: any,
  // 用函数而不是固定对象读取策略，保证每次 Tool 调用都能拿到最新的 Agent 配置。
  getRuntimePolicy?: () => RuntimeToolPolicy,
  // 高风险操作的审批回调。Promise 完成前，当前 Tool 调用会一直暂停在 await 位置。
  requestApproval?: (
    event: any,
    decision: ToolPolicyDecision,
  ) => Promise<boolean>,
) {
  // `tool_call` 发生在 Tool 真正执行之前，因此可以在这里安全地放行或阻断调用。
  // 监听器使用 async，是因为写入和破坏性操作可能需要等待用户稍后审批。
  pi.on("tool_call", async (event: any) => {
    // 每次事件到达时都重新调用 getRuntimePolicy()，而不是复用启动时的旧配置。
    // event.toolName 是 Tool 名称，event.input 是模型为该 Tool 生成的调用参数。
    const decision = evaluateToolCall(
      event.toolName,
      event.input,
      getRuntimePolicy?.(),
    );

    // requiresApproval 表示策略已经识别出写入或破坏性操作。
    // 只有同时提供 requestApproval 回调时才创建审批，并暂停等待用户决定。
    if (decision.requiresApproval && requestApproval) {
      // requestApproval() 会创建审批记录、向前端推送审批卡片，
      // 然后返回一个 Promise。await 会暂停本次 Tool，但不会阻塞整个 Node.js 进程。
      const approved = await requestApproval(event, decision);

      // 用户批准时返回 undefined，Pi 随即继续原 Tool；
      // 用户拒绝、审批超时或运行被取消时返回 block，Tool 不会执行。
      return approved
        ? undefined
        : { block: true, reason: "工具调用未获得人工审批。" };
    }

    // 不需要审批时，根据策略的 allowed 字段返回最终结果：
    // - 只读或普通执行：allowed 为 true，返回 undefined 直接放行；
    // - 越权调用或 Skill 绕过：allowed 为 false，携带策略给出的原因阻断。
    // 如果某个高风险调用需要审批但 requestApproval 未配置，decision.allowed 仍为 false，
    // 因而会在这里按“默认拒绝”处理，避免审批链路缺失时意外放行。
    return decision.allowed
      ? undefined
      : { block: true, reason: decision.reason };
  });
}
