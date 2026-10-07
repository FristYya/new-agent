/** 当前 Agent 在运行时真正允许使用的 Tool 与被禁用的 Skill。 */
export type RuntimeToolPolicy = {
  allowedTools: Iterable<string>;
  disabledSkills: Iterable<string>;
};

/** Tool 风险等级：只读、写入、普通执行和破坏性执行。 */
export type ToolRisk = "read" | "write" | "execute" | "destructive";

/** guard 扩展根据这个结果决定放行、拒绝或发起人工审批。 */
export type ToolPolicyDecision = {
  allowed: boolean;
  risk: ToolRisk;
  reason?: string;
  requiresApproval?: boolean;
};

// 明确识别不可自动放行的高危 shell 命令。
const DESTRUCTIVE_SHELL_PATTERNS = [
  /(^|\s)rm\s+(?:-[^\s]*r[^\s]*f|-[^\s]*f[^\s]*r)(?:\s|$)/i,
  /(^|\s)(?:mkfs(?:\.[a-z0-9]+)?|shutdown|reboot|poweroff)(?:\s|$)/i,
  /(^|\s)dd\s+[^\n]*\bof=\/dev\//i,
  /(?:curl|wget)[^\n|]*\|\s*(?:sh|bash|zsh)(?:\s|$)/i,
  /(^|\s)(?:drop\s+(?:database|table)|truncate\s+table)(?:\s|$)/i,
];

// current-time Skill 被禁用后，也不能改用 bash 绕过 Skill 开关查询时间。
const TIME_COMMAND_PATTERN =
  /\bdate\b|\bcal\b|\btime\b|datetime|new\s+Date|Date\.now|Intl\.DateTimeFormat/i;

/** 从 Tool 名称和输入参数中计算风险等级。 */
export function classifyToolRisk(toolName: string, input?: unknown): ToolRisk {
  // 这些 Pi 内置 Tool 只读取本地内容，可以自动放行。
  if (["read", "grep", "find", "ls"].includes(toolName)) return "read";

  // write/edit 会修改文件，必须进入人工审批。
  if (toolName === "write" || toolName === "edit") return "write";

  if (toolName === "bash") {
    const command = input && typeof input === "object"
      ? String((input as Record<string, unknown>).command ?? "")
      : "";

    // 命中破坏性规则时使用最高风险等级。
    if (DESTRUCTIVE_SHELL_PATTERNS.some((pattern) => pattern.test(command))) {
      return "destructive";
    }
  }

  // 其他脚本型或自定义 Tool 归为普通执行。
  return "execute";
}

/**
 * Tool 调用的策略入口。
 * 顺序是：运行时白名单 → Skill 防绕过 → 风险审批。
 */
export function evaluateToolCall(
  toolName: string,
  input: unknown,
  policy?: RuntimeToolPolicy,
): ToolPolicyDecision {
  const risk = classifyToolRisk(toolName, input);
  const allowedTools = policy ? new Set(policy.allowedTools) : undefined;
  const disabledSkills = new Set(policy?.disabledSkills ?? []);

  // Tool 已注册不等于可以执行；不在当前 Agent 白名单中就直接拒绝。
  if (allowedTools && !allowedTools.has(toolName)) {
    return {
      allowed: false,
      risk,
      reason: `${toolName} 已被当前 Agent 配置禁用，不能调用。`,
    };
  }

  // Skill 开关必须成为安全边界，不能只影响系统提示词。
  if (
    disabledSkills.has("current_time") &&
    toolName === "bash" &&
    TIME_COMMAND_PATTERN.test(
      input && typeof input === "object"
        ? String((input as Record<string, unknown>).command ?? "")
        : "",
    )
  ) {
    return {
      allowed: false,
      risk,
      reason: "current_time Skill 已被禁用，不能通过 bash 绕行查询系统时间。",
    };
  }

  // 破坏性和写入操作先阻塞，等待前端给出一次性审批决定。
  if (risk === "destructive") {
    return {
      allowed: false,
      risk,
      requiresApproval: true,
      reason: "检测到破坏性命令，需要人工审批后才能执行。",
    };
  }

  if (risk === "write") {
    return {
      allowed: false,
      risk,
      requiresApproval: true,
      reason: "该工具会写入或修改数据，需要人工审批。",
    };
  }

  // 只读和普通执行 Tool 到这里才会被放行。
  return { allowed: true, risk };
}