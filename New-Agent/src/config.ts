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

    // mysql 配置
    storage: process.env.NEW_AGENT_STORAGE?.toLowerCase() === "file"
        ? "file"
        : "mysql",

    mysql: {
        host: process.env.MYSQL_HOST || "127.0.0.1",
        port: positiveInteger(process.env.MYSQL_PORT, 3306, "MYSQL_PORT"),
        database: process.env.MYSQL_DATABASE || "new_agent",
        user: process.env.MYSQL_USER || "root",
        password: process.env.MYSQL_PASSWORD || "b2535abbc080c621a129f55ac81ba09e",
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
        process.env.AUTH_SECRET || 
        "new-agent-course-auth-secret-change-in-production",

    authCookieSecure:
        process.env.AUTH_COOKIE_SECURE?.toLocaleLowerCase() === "true",

    // Cookie 的有效期
    authMaxAgeSeconds: positiveInteger(
        process.env.AUTH_MAX_AGE_SECONDS,
        7 * 24 * 60 * 60,
        "AUTH_MAX_AGE_SECONDS",
    ),
} as const;
