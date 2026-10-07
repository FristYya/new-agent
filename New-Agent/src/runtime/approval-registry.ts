import { createHash, randomUUID, timingSafeEqual } from "node:crypto";

/**
 * 只保存在当前 Node.js 进程内的待审批任务。
 *
 * ApprovalRecord 负责保存可查询、可持久化的业务状态；
 * PendingApproval 保存 Promise 的 resolve 和定时器等运行时对象，不能写入 JSON 或数据库。
 */
type PendingApproval = {
  // 记录审批属于哪一次 Agent 运行，取消 run 时可以批量找到相关审批。
  runId: string;
  // 服务端内存中也不保存明文 token，只保存 SHA-256 哈希用于后续校验。
  tokenHash: string;
  // create() 创建的 Promise 会把 resolve 函数保存到这里。
  // 用户作出决定后调用 resolve(true/false)，guard 中的 await 才会继续执行。
  resolve: (approved: boolean) => void;
  // 保存超时定时器，审批完成或 run 取消时必须清理，避免重复触发。
  timeout: ReturnType<typeof setTimeout>;
};

/**
 * 将一次性审批 token 转换成 SHA-256 哈希。
 * 浏览器拿到明文 token；服务端状态和 Registry 只保存哈希，降低 token 泄露风险。
 */
export function approvalTokenHash(token: string): string {
  return createHash("sha256").update(token).digest("hex");
}

/**
 * 使用恒定时间比较两个哈希，避免普通字符串比较因提前结束而泄露匹配位置。
 * timingSafeEqual 要求两个 Buffer 长度一致，因此必须先检查长度。
 */
export function secureHashEqual(left: string, right: string): boolean {
  const a = Buffer.from(left);
  const b = Buffer.from(right);
  return a.length === b.length && timingSafeEqual(a, b);
}

/** 管理一次性审批 token、超时定时器，以及正在等待用户决定的 Tool Promise。 */
export class ApprovalRegistry {
  // key 是 approvalId，value 是只能存在于本进程内的待审批运行时数据。
  private readonly pending = new Map<string, PendingApproval>();

  /**
   * 创建一条待审批任务。
   * 返回值中的 token 只发送给当前浏览器一次，tokenHash 可写入 AppState，
   * promise 则交给 guard 等待最终的批准或拒绝结果。
   */
  create(
    approvalId: string,
    runId: string,
    timeoutMs: number,
    onExpire: () => void | Promise<void>,
  ) {
    // 为当前审批生成不可预测的一次性凭证，并立即计算哈希。
    const token = randomUUID();
    const tokenHash = approvalTokenHash(token);

    // 先声明变量，再在 Promise 构造器中接收 resolve，供 decide()/cancelRun() 稍后调用。
    let resolveDecision!: (approved: boolean) => void;

    // Promise 创建后保持 pending；guard 会 await 它，但不会阻塞整个 Node.js 服务。
    const promise = new Promise<boolean>((resolve) => {
      resolveDecision = resolve;
    });

    // 用户在规定时间内没有操作时：先删除 Registry 记录，防止过期 token 再被使用；
    // 然后用 false 恢复等待中的 Tool，最后通知状态层把 ApprovalRecord 标记为 expired。
    const timeout = setTimeout(async () => {
      const pending = this.pending.get(approvalId);
      if (!pending) return;
      this.pending.delete(approvalId);
      pending.resolve(false);
      await onExpire();
    }, timeoutMs);

    // 审批定时器本身不应该阻止 Node.js 进程正常退出。
    timeout.unref();

    // 保存恢复 Promise 所需的数据，后续通过 approvalId 精确找到本次 Tool 调用。
    this.pending.set(approvalId, {
      runId,
      tokenHash,
      resolve: resolveDecision,
      timeout,
    });

    // 明文 token 不进入 Map；调用方应只通过当前 SSE 把它交给浏览器。
    return { token, tokenHash, promise };
  }

  /**
   * 消费一次审批决定。
   * 只有 approvalId 和一次性 token 同时匹配才成功；成功后立即删除记录，防止重放。
   */
  decide(approvalId: string, token: string, approved: boolean): boolean {
    // approvalId 找不到，或 token 哈希不匹配，都表示审批无效。
    const pending = this.pending.get(approvalId);
    if (
      !pending ||
      !secureHashEqual(pending.tokenHash, approvalTokenHash(token))
    ) {
      return false;
    }

    // 用户已经作出决定，不再需要超时回调。
    clearTimeout(pending.timeout);

    // 必须先删除再 resolve，确保 Promise 恢复后同一个 token 已经不能再次使用。
    this.pending.delete(approvalId);

    // true 表示批准并继续 Tool；false 表示拒绝并由 guard 阻断 Tool。
    pending.resolve(approved);
    return true;
  }

  /**
   * 只验证 approvalId 与 token 是否匹配，不消费 token，也不恢复 Promise。
   * 决定接口可先验证内存凭证，再更新 AppState，最后调用 decide() 真正恢复 Tool。
   */
  verify(approvalId: string, token: string): boolean {
    const pending = this.pending.get(approvalId);
    return Boolean(
      pending &&
      secureHashEqual(pending.tokenHash, approvalTokenHash(token)),
    );
  }

  /**
   * 页面重新获取待审批列表时轮换一次性 token。
   * Registry 只保留新 token 的哈希，因此旧页面中的 token 会立即失效。
   */
  rotateToken(approvalId: string) {
    const pending = this.pending.get(approvalId);
    if (!pending) return undefined;

    // 生成新 token，并用新哈希覆盖旧哈希。
    const token = randomUUID();
    pending.tokenHash = approvalTokenHash(token);
    return { token, tokenHash: pending.tokenHash };
  }

  /**
   * 用户取消整次 Agent 运行时，清理该 runId 下的全部待审批项。
   * 每个等待中的 Promise 都用 false 恢复，避免 session 永久停在审批位置。
   */
  cancelRun(runId: string): number {
    let canceled = 0;
    for (const [approvalId, pending] of this.pending) {
      // Registry 可能同时保存其他运行的审批，只处理当前 runId。
      if (pending.runId !== runId) continue;

      // 清理定时器和 Map 记录，再按拒绝恢复对应的 Tool 调用。
      clearTimeout(pending.timeout);
      this.pending.delete(approvalId);
      pending.resolve(false);
      canceled += 1;
    }

    // 返回取消数量，便于日志记录或后续监控。
    return canceled;
  }
}
