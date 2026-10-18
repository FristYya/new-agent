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

// 读取允许为 0 的数值配置，例如 0 表示关闭成本告警。
function nonNegativeNumber(value: string | undefined, fallback: number, name: string) {
    const parsed = Number(value ?? fallback);
    if (!Number.isFinite(parsed) || parsed < 0) throw new Error(`${name} 必须是非负数字`);
    return parsed;
}

const DEFAULT_AUTH_SECRET = "new-agent-course-auth-secret-change-in-production";
const DEFAULT_MYSQL_PASSWORD = "b2535abbc080c621a129f55ac81ba09e";

export const runtimeConfig = {
    // 服务端口，如果没有配置 PORT 默认使用 3100
    port: positiveInteger(process.env.PORT, 3100, "PORT"),
    // 拜访业务由独立 Spring Boot 服务承载，New-Agent 只做同源认证代理。
    visitApiBaseUrl: process.env.VISIT_API_BASE_URL?.trim() || "http://127.0.0.1:8080",
    visitApiTimeoutMs: positiveInteger(
        process.env.VISIT_API_TIMEOUT_MS,
        10_000,
        "VISIT_API_TIMEOUT_MS",
    ),
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

    // 进程异常退出后，超过该时长仍未结束的观测 Run 会被恢复任务标记为失败。
    observabilityStaleRunMs: positiveInteger(
        process.env.OBSERVABILITY_STALE_RUN_MS,
        30 * 60 * 1000,
        "OBSERVABILITY_STALE_RUN_MS",
    ),
    observabilityAlertDurationMs: positiveInteger(
        process.env.OBSERVABILITY_ALERT_DURATION_MS,
        120_000,
        "OBSERVABILITY_ALERT_DURATION_MS",
    ),
    // 设为 0 可关闭单次运行成本告警。
    observabilityAlertCostUsd: nonNegativeNumber(
        process.env.OBSERVABILITY_ALERT_COST_USD,
        1,
        "OBSERVABILITY_ALERT_COST_USD",
    ),

    // 评测资源预算，防止单次运行或证据快照无限膨胀。
    evalMaxCasesPerRun: positiveInteger(
        process.env.EVAL_MAX_CASES_PER_RUN,
        200,
        "EVAL_MAX_CASES_PER_RUN",
    ),
    evalMaxStoredCases: positiveInteger(
        process.env.EVAL_MAX_STORED_CASES,
        2_000,
        "EVAL_MAX_STORED_CASES",
    ),
    evalMaxOutputChars: positiveInteger(
        process.env.EVAL_MAX_OUTPUT_CHARS,
        200_000,
        "EVAL_MAX_OUTPUT_CHARS",
    ),
    // 限制非确定性评测的重复执行次数，避免一次请求意外放大模型成本。
    evalMaxTrialsPerCase: positiveInteger(
        process.env.EVAL_MAX_TRIALS_PER_CASE,
        20,
        "EVAL_MAX_TRIALS_PER_CASE",
    ),
    evalMaxEvidenceBytes: positiveInteger(
        process.env.EVAL_MAX_EVIDENCE_BYTES,
        16 * 1024 * 1024,
        "EVAL_MAX_EVIDENCE_BYTES",
    ),

    // mysql 配置
    storage: process.env.NEW_AGENT_STORAGE?.toLowerCase() === "file"
        ? "file"
        : "mysql",

    mysql: {
        host: process.env.MYSQL_HOST || "127.0.0.1",
        port: positiveInteger(process.env.MYSQL_PORT, 3306, "MYSQL_PORT"),
        database: process.env.MYSQL_DATABASE || "new_agent",
        user: process.env.MYSQL_USER || "root",
        password: process.env.MYSQL_PASSWORD || DEFAULT_MYSQL_PASSWORD,
        connectionLimit: positiveInteger(
            process.env.MYSQL_CONNECTION_LIMIT,
            10,
            "MYSQL_CONNECTION_LIMIT",
        ),
        connectionTimeoutMs: positiveInteger(
            process.env.MYSQL_CONNECT_TIMEOUT_MS,
            5000,
            "MYSQL_CONNECT_TIMEOUT_MS",
        ),
    },

    // 登录 cookie 的签名秘钥
    authSecret:
        process.env.AUTH_SECRET || DEFAULT_AUTH_SECRET,

    authCookieSecure:
        process.env.AUTH_COOKIE_SECURE?.toLocaleLowerCase() === "true",

    // Cookie 的有效期
    authMaxAgeSeconds: positiveInteger(
        process.env.AUTH_MAX_AGE_SECONDS,
        7 * 24 * 60 * 60,
        "AUTH_MAX_AGE_SECONDS",
    ),
} as const;


if (process.env.NODE_ENV === "production") {
    const violations: string[] = [];
    if (runtimeConfig.storage !== "mysql") violations.push("NEW_AGENT_STORAGE 必须为 mysql");
    if (!process.env.MYSQL_PASSWORD || runtimeConfig.mysql.password === DEFAULT_MYSQL_PASSWORD) {
        violations.push("必须显式配置非默认 MYSQL_PASSWORD");
    }
    if (!process.env.AUTH_SECRET || runtimeConfig.authSecret === DEFAULT_AUTH_SECRET || runtimeConfig.authSecret.length < 32) {
        violations.push("必须配置至少 32 字符的非默认 AUTH_SECRET");
    }
    if (!runtimeConfig.authCookieSecure) violations.push("AUTH_COOKIE_SECURE 必须为 true");
    if (violations.length) throw new Error(`生产配置不安全：${violations.join("；")}`);
}
