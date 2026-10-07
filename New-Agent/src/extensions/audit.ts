import { safePreview } from "../security/redaction.ts";

/**
 * Pi Extension：监听 Tool 的开始与结束事件，并输出脱敏后的审计日志。
 * 它只负责“记录发生了什么”，不会决定 Tool 是否允许执行。
 */
export function auditExtension(pi: any) {
  // 同一时间可能有多个 Tool 并发执行，因此用 toolCallId 分别保存它们的开始时间。
  // Map 的 value 是 Date.now() 返回的毫秒时间戳，用于在结束事件里计算耗时。
  const starts = new Map<string, number>();

  // Pi 即将执行某个 Tool 时触发。此时可以记录 Tool 名称与调用参数。
  pi.on("tool_execution_start", (event: any) => {
    // 必须先保存开始时间；等结束事件到达时，再用相同 toolCallId 找回它。
    starts.set(event.toolCallId, Date.now());

    // event.args 可能为空，所以用空对象兜底。
    // safePreview 会先脱敏再把内容限制在 500 个字符内，原始凭证不会进入控制台。
    console.log(
      `[tool:start] ${event.toolName} ${safePreview(event.args ?? {}, 500)}`,
    );
  });

  // Tool 执行完成或失败后触发。该事件用于记录结果状态与总耗时。
  pi.on("tool_execution_end", (event: any) => {
    // 正常情况下能够读取到开始时间；如果服务在中途加载或事件缺失，
    // 就用当前时间兜底，使耗时为 0 左右，而不是输出 NaN 或让审计逻辑报错。
    const start = starts.get(event.toolCallId) ?? Date.now();

    // 当前 Tool 已结束，立即删除对应记录，避免 Map 长期积累造成内存泄漏。
    starts.delete(event.toolCallId);

    // isError 为 true 表示 Tool 执行失败，否则记为 ok。
    // 当前时间减去开始时间，得到从 start 到 end 的总耗时（单位：毫秒）。
    console.log(
      `[tool:end] ${event.toolName} ${event.isError ? "failed" : "ok"} ${Date.now() - start}ms`,
    );
  });
}
