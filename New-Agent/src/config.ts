// 把环境变量转换为正整数
function positiveInteger(
    value: string | undefined,
    fallback: number,
    name: string,
) {
    const parsed = Number(value ?? fallback);
    if (!Number.isInteger(parsed) || parsed <= 0) {
        throw new Error(`${name} 必须是正整数`);
    }
    return parsed;
}

export const runtimeConfig = {
    // 服务端口，如果没有配置 PORT 默认使用 3100
    port: positiveInteger(process.env.PORT, 3100, "PORT"),
    // 单次 SKILL TOOL 最长的执行时间
    skillTimeoutMs: positiveInteger(
        process.env.SKILL_TIMEOUT_MS,
        15000,
        "SKILL_TIMEOUT_MS",
    ),
    // 写入的json最大字节数
    skillMaxInputBytes: positiveInteger(
        process.env.SKILL_MAX_INPUT_BYTES,
        64 * 1024,
        "SKILL_MAX_INPUT_BYTES",
    ),
    skillMaxOutputBytes: positiveInteger(
        process.env.SKILL_MAX_OUTPUT_BYTES,
        1024 * 1024,
        "SKILL_MAX_OUTPUT_BYTES",
    ),
    // 人工审批最长等待时间。
    // 超时后按拒绝处理，避免 session.prompt() 永久停在 Tool 调用处。
    approvalTimeoutMs: positiveInteger(
        process.env.APPROVAL_TIMEOUT_MS,
        5 * 60 * 1000,
        "APPROVAL_TIMEOUT_MS",
    ),
} as const;
