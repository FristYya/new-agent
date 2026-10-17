// mysql2/promise 提供 Promise 风格的连接池 API，能够直接配合 async/await。
// Pool 是连接池类型；RowDataPacket 用于约束 SELECT 查询返回的行。
import mysql, { type Pool, type RowDataPacket } from "mysql2/promise";
import { randomUUID } from "node:crypto";

// Pi Session 的 entries 使用 SDK 提供的 FileEntry 类型，
// 从数据库恢复的数据可以直接交给 SessionManager。
import type { FileEntry } from "@earendil-works/pi-coding-agent";
import { runtimeConfig } from "../config.ts";

/** 数据库中一条 Pi Session 恢复后交给运行时的结构。 */
export type StoredPiSession = {
  // Pi SDK 自己使用的稳定 Session ID。
  sessionId: string;
  // 完整会话树，包含消息、Tool 调用、Tool 结果、分支和压缩节点。
  entries: FileEntry[];
};

/** users 表返回给认证模块的稳定字段，不包含任何明文密码。 */
export type StoredAuthUser = {
  id: string;
  username: string;
  displayName: string;
  passwordHash: string;
  createdAt: string;
};

// pool 保存唯一连接池。第一次真正访问数据库时才创建。
let pool: Pool | undefined;

// verification 缓存正在执行或已经成功的校验 Promise。
// 多个请求同时到达时会等待同一次校验，不会重复查询表结构。
let verification: Promise<void> | undefined;

// 表由 database/001-new-agent.sql 创建。
// 应用启动只检查它们是否存在，不执行 CREATE 或 ALTER。
const requiredTables = [
  "schema_migrations",
  "users",
  "app_documents",
  "app_states",
  "pi_sessions",
  "observability_runs",
  "observability_events",
  "observability_alerts",
] as const;

export type VersionedDocument<T> = { value: T; version: number };

/** 判断当前是否启用 MySQL；只有显式配置 file 才使用文件回退。 */
export function usesMysqlStorage(env: NodeJS.ProcessEnv = process.env) {
  return env.NEW_AGENT_STORAGE?.toLowerCase() !== "file";
}

/** 根据 config.ts 中的最终配置创建连接池。 */
function createPool(): Pool {
  // 某些部署平台只提供一条 MYSQL_URL，因此连接串优先级最高。
  if (process.env.MYSQL_URL) {
    return mysql.createPool({
      uri: process.env.MYSQL_URL,
      connectionLimit: runtimeConfig.mysql.connectionLimit,
      connectTimeout: runtimeConfig.mysql.connectionTimeoutMs,
      // 长连接开启 KeepAlive，避免空闲一段时间后被网络设备静默断开。
      enableKeepAlive: true,
      keepAliveInitialDelay: 10_000,
      // 数据库连接统一使用 UTC，读取后再由展示层决定时区。
      timezone: "Z",
    });
  }

  // 默认走本机 127.0.0.1:3306，并使用 config.ts 中的账号密码。
  return mysql.createPool({
    host: runtimeConfig.mysql.host,
    port: runtimeConfig.mysql.port,
    user: runtimeConfig.mysql.user,
    password: runtimeConfig.mysql.password,
    database: runtimeConfig.mysql.database,
    connectionLimit: runtimeConfig.mysql.connectionLimit,
    connectTimeout: runtimeConfig.mysql.connectionTimeoutMs,
    enableKeepAlive: true,
    keepAliveInitialDelay: 10_000,
    timezone: "Z",
    // utf8mb4 可以完整保存中文和 Emoji。
    charset: "utf8mb4",
  });
}

/** 返回进程内共享连接池，不允许在 file 模式误调用 MySQL。 */
function getPool(): Pool {
  if (!usesMysqlStorage()) throw new Error("MySQL 存储未启用");
  // ??= 只在 pool 为空时创建一次，后续请求全部复用。
  pool ??= createPool();
  return pool;
}

/**
 * 校验数据库连接和表。
 */
export async function verifyMysqlStorage(): Promise<void> {
  // file 模式不需要 MySQL，直接结束。
  if (!usesMysqlStorage()) return;

  verification ??= (async () => {
    const db = getPool();

    // 第一步只验证数据库是否可以建立连接并执行查询。
    await db.query("SELECT 1");

    // 根据必需表数量生成 ?, ?, ?，表名仍通过参数传递。
    const placeholders = requiredTables.map(() => "?").join(", ");
    const [rows] = await db.execute<RowDataPacket[]>(
      `SELECT TABLE_NAME AS tableName
       FROM information_schema.TABLES
       WHERE TABLE_SCHEMA = ?
         AND TABLE_NAME IN (${placeholders})`,
      [runtimeConfig.mysql.database, ...requiredTables],
    );

    const existingTables = new Set(
      rows.map((row) => String(row.tableName)),
    );
    const missingTables = requiredTables.filter(
      (name) => !existingTables.has(name),
    );

    if (missingTables.length > 0) {
      throw new Error(
        `数据库 ${runtimeConfig.mysql.database} 缺少必需表：${missingTables.join(", ")}。请依次执行 database/001-new-agent.sql 和 database/002-observability.sql`,
      );
    }
  })().catch((error) => {
    // 校验失败后清空缓存，修好数据库后下一次调用可以重新检查。
    verification = undefined;
    throw error;
  });

  await verification;
}

/** mysql2 可能返回 JSON 字符串，也可能直接返回对象，这里统一处理。 */
function parseJson<T>(value: unknown): T {
  return typeof value === "string" ? JSON.parse(value) as T : value as T;
}

/** 服务启动时读取全部注册用户，供 UserRegistry 恢复登录身份。 */
export async function readAuthUsers(): Promise<StoredAuthUser[]> {
  await verifyMysqlStorage();

  const [rows] = await getPool().execute<RowDataPacket[]>(
    `SELECT
       id,
       username,
       display_name AS displayName,
       password_hash AS passwordHash,
       created_at AS createdAt
     FROM users
     ORDER BY created_at ASC`,
  );

  // 将数据库驱动返回值转换成认证模块需要的稳定字符串结构。
  return rows.map((row) => ({
    id: String(row.id),
    username: String(row.username),
    displayName: String(row.displayName),
    passwordHash: String(row.passwordHash),
    createdAt: row.createdAt instanceof Date
      ? row.createdAt.toISOString()
      : String(row.createdAt),
  }));
}

/** 注册成功时只新增当前用户，不重写其他用户数据。 */
export async function insertAuthUser(
  user: StoredAuthUser,
): Promise<void> {
  await verifyMysqlStorage();

  // 业务层统一使用 ISO 8601 字符串表示时间，例如 2026-10-11T05:19:24.455Z。
  // MySQL 的 TIMESTAMP(3) 不接受带 T 和 Z 的字符串直接写入，因此先转换为 Date。
  // mysql2 会结合连接池的 timezone: "Z"，将 Date 序列化为 MySQL 可接受的 UTC 时间。
  const createdAt = new Date(user.createdAt);

  // 如果业务层传入了损坏的时间字符串，应当在执行 SQL 前给出明确错误。
  if (Number.isNaN(createdAt.getTime())) {
    throw new Error(`用户创建时间格式无效：${user.createdAt}`);
  }

  // username 的唯一索引负责跨进程并发兜底。
  // 五个值全部通过占位符传递，避免 SQL 注入。
  await getPool().execute(
    `INSERT INTO users (
       id, username, display_name, password_hash, created_at
     ) VALUES (?, ?, ?, ?, ?)`,
    [
      user.id,
      user.username,
      user.displayName,
      user.passwordHash,
      // 向 mysql2 传入 Date，而不是带 T、Z 的 ISO 字符串。
      createdAt,
    ],
  );
}

/** 按用户 ID 读取完整 AppState；没有记录时返回 null。 */
export async function readStateDocument<T>(
  ownerKey: string,
): Promise<T | null> {
  await verifyMysqlStorage();
  const [rows] = await getPool().execute<RowDataPacket[]>(
    "SELECT state_json FROM app_states WHERE owner_key = ?",
    [ownerKey],
  );
  return rows[0] ? parseJson<T>(rows[0].state_json) : null;
}

/**
 * 保存用户完整 AppState。
 * 第一次执行 INSERT；owner_key 已存在时更新 JSON，并把版本号加一。
 */
export async function writeStateDocument(
  ownerKey: string,
  state: unknown,
): Promise<void> {
  await verifyMysqlStorage();
  await getPool().execute(
    `INSERT INTO app_states (owner_key, state_json)
     VALUES (?, CAST(? AS JSON))
     ON DUPLICATE KEY UPDATE
       state_json = VALUES(state_json), version = version + 1`,
    [ownerKey, JSON.stringify(state)],
  );
}

/** 按 document_key 读取独立通用文档。 */
export async function readDocument<T>(key: string): Promise<T | null> {
  await verifyMysqlStorage();
  const [rows] = await getPool().execute<RowDataPacket[]>(
    "SELECT document_json FROM app_documents WHERE document_key = ?",
    [key],
  );
  return rows[0] ? parseJson<T>(rows[0].document_json) : null;
}

/** 读取文档及其版本，用于跨进程乐观并发控制。 */
export async function readVersionedDocument<T>(key: string): Promise<VersionedDocument<T> | null> {
  await verifyMysqlStorage();
  const [rows] = await getPool().execute<RowDataPacket[]>(
    "SELECT document_json, version FROM app_documents WHERE document_key = ?",
    [key],
  );
  return rows[0]
    ? { value: parseJson<T>(rows[0].document_json), version: Number(rows[0].version) }
    : null;
}

/**
 * 仅当数据库版本仍等于 expectedVersion 时写入。返回 false 表示其他实例已经抢先修改，
 * 调用方必须重新读取、重新执行变更，不能覆盖新数据。
 */
export async function compareAndSwapDocument(
  key: string,
  value: unknown,
  expectedVersion: number,
): Promise<boolean> {
  await verifyMysqlStorage();
  if (expectedVersion === 0) {
    const [result] = await getPool().execute<any>(
      `INSERT IGNORE INTO app_documents (document_key, document_json, version)
       VALUES (?, CAST(? AS JSON), 1)`,
      [key, JSON.stringify(value)],
    );
    return Number(result.affectedRows) === 1;
  }
  const [result] = await getPool().execute<any>(
    `UPDATE app_documents
     SET document_json = CAST(? AS JSON), version = version + 1
     WHERE document_key = ? AND version = ?`,
    [JSON.stringify(value), key, expectedVersion],
  );
  return Number(result.affectedRows) === 1;
}

/** MySQL 正式模式下按行保存高频观测数据，避免反复覆盖大型 JSON 文档。 */
export async function insertObservabilityRun(
  ownerId: string,
  run: { id: string; startedAt: string },
  events: Array<{ id: string; runId: string; createdAt: string }>,
): Promise<void> {
  await verifyMysqlStorage();
  const connection = await getPool().getConnection();
  try {
    await connection.beginTransaction();
    await connection.execute(
      `INSERT INTO observability_runs (owner_id, run_id, started_at, run_json)
       VALUES (?, ?, ?, CAST(? AS JSON))`,
      [ownerId, run.id, new Date(run.startedAt), JSON.stringify(run)],
    );
    for (const event of events) {
      await connection.execute(
        `INSERT INTO observability_events (owner_id, event_id, run_id, created_at, event_json)
         VALUES (?, ?, ?, ?, CAST(? AS JSON))`,
        [ownerId, event.id, event.runId, new Date(event.createdAt), JSON.stringify(event)],
      );
    }
    await connection.commit();
  } catch (error) {
    await connection.rollback();
    throw error;
  } finally {
    connection.release();
  }
}

export async function appendObservabilityEvent(
  ownerId: string,
  event: { id: string; runId: string; createdAt: string },
): Promise<void> {
  await verifyMysqlStorage();
  await getPool().execute(
    `INSERT INTO observability_events (owner_id, event_id, run_id, created_at, event_json)
     VALUES (?, ?, ?, ?, CAST(? AS JSON))`,
    [ownerId, event.id, event.runId, new Date(event.createdAt), JSON.stringify(event)],
  );
}

export async function finishObservabilityRun(
  ownerId: string,
  run: { id: string; finishedAt?: string },
  finalEvent: { id: string; runId: string; createdAt: string },
  alert?: { id: string; runId: string; createdAt: string },
): Promise<void> {
  await verifyMysqlStorage();
  const connection = await getPool().getConnection();
  try {
    await connection.beginTransaction();
    await connection.execute(
      `UPDATE observability_runs SET finished_at = ?, run_json = CAST(? AS JSON)
       WHERE owner_id = ? AND run_id = ?`,
      [run.finishedAt ? new Date(run.finishedAt) : null, JSON.stringify(run), ownerId, run.id],
    );
    await connection.execute(
      `INSERT INTO observability_events (owner_id, event_id, run_id, created_at, event_json)
       VALUES (?, ?, ?, ?, CAST(? AS JSON))`,
      [ownerId, finalEvent.id, finalEvent.runId, new Date(finalEvent.createdAt), JSON.stringify(finalEvent)],
    );
    if (alert) {
      await connection.execute(
        `INSERT INTO observability_alerts (owner_id, alert_id, run_id, status, created_at, alert_json)
         VALUES (?, ?, ?, 'active', ?, CAST(? AS JSON))`,
        [ownerId, alert.id, alert.runId, new Date(alert.createdAt), JSON.stringify(alert)],
      );
    }
    await connection.commit();
  } catch (error) {
    await connection.rollback();
    throw error;
  } finally {
    connection.release();
  }
}

export async function readObservabilityRows(ownerId: string, limit = 500): Promise<{ runs: any[]; events: any[]; alerts: any[] }> {
  await verifyMysqlStorage();
  const boundedLimit = Math.max(1, Math.min(1_000, Math.floor(limit)));
  const [runRows] = await getPool().query<RowDataPacket[]>(
    `SELECT run_json FROM observability_runs WHERE owner_id = ? ORDER BY started_at DESC LIMIT ${boundedLimit}`,
    [ownerId],
  );
  const runs = runRows.map((row) => parseJson<any>(row.run_json));
  if (!runs.length) return { runs: [], events: [], alerts: [] };
  const runIds = runs.map((run) => run.id);
  const placeholders = runIds.map(() => "?").join(",");
  const [eventRows] = await getPool().execute<RowDataPacket[]>(
    `SELECT event_json FROM (
       SELECT event_json, created_at, event_id FROM observability_events
       WHERE owner_id = ? AND run_id IN (${placeholders})
       ORDER BY created_at DESC, event_id DESC LIMIT 10000
     ) AS recent_events
     ORDER BY created_at ASC, event_id ASC`,
    [ownerId, ...runIds],
  );
  const [alertRows] = await getPool().execute<RowDataPacket[]>(
    `SELECT alert_json FROM observability_alerts
     WHERE owner_id = ? AND run_id IN (${placeholders}) ORDER BY created_at DESC LIMIT 500`,
    [ownerId, ...runIds],
  );
  return {
    runs,
    events: eventRows.map((row) => parseJson<any>(row.event_json)),
    alerts: alertRows.map((row) => parseJson<any>(row.alert_json)),
  };
}

export async function acknowledgeObservabilityAlert(ownerId: string, alertId: string, alert: unknown): Promise<boolean> {
  await verifyMysqlStorage();
  const [result] = await getPool().execute<any>(
    `UPDATE observability_alerts SET status = 'acknowledged', alert_json = CAST(? AS JSON)
     WHERE owner_id = ? AND alert_id = ?`,
    [JSON.stringify(alert), ownerId, alertId],
  );
  return Number(result.affectedRows) === 1;
}

/** 删除超出保留上限的旧 Run；外键级联清理事件和告警。 */
export async function pruneObservabilityRows(ownerId: string, retain = 500): Promise<number> {
  await verifyMysqlStorage();
  const boundedRetain = Math.max(1, Math.min(10_000, Math.floor(retain)));
  const [result] = await getPool().execute<any>(
    `DELETE FROM observability_runs
     WHERE owner_id = ?
       AND run_id NOT IN (
         SELECT run_id FROM (
           SELECT run_id FROM observability_runs
           WHERE owner_id = ? ORDER BY started_at DESC LIMIT ${boundedRetain}
         ) AS retained_runs
       )`,
    [ownerId, ownerId],
  );
  return Number(result.affectedRows);
}

/** 把进程崩溃遗留的 running Run 收敛为失败，并生成可追踪的事件和告警。 */
export async function recoverStaleObservabilityRuns(maxAgeMs: number): Promise<number> {
  if (!usesMysqlStorage()) return 0;
  await verifyMysqlStorage();
  const connection = await getPool().getConnection();
  try {
    await connection.beginTransaction();
    const cutoff = new Date(Date.now() - Math.max(60_000, maxAgeMs));
    const [rows] = await connection.execute<RowDataPacket[]>(
      `SELECT owner_id, run_id, run_json
       FROM observability_runs
       WHERE finished_at IS NULL AND started_at < ?
       ORDER BY started_at ASC LIMIT 100
       FOR UPDATE SKIP LOCKED`,
      [cutoff],
    );
    let recovered = 0;
    for (const row of rows) {
      const ownerId = String(row.owner_id);
      const runId = String(row.run_id);
      const run = parseJson<any>(row.run_json);
      if (run.status !== "running") continue;
      const finishedAt = new Date().toISOString();
      run.status = "failed";
      run.finishedAt = finishedAt;
      run.updatedAt = finishedAt;
      run.durationMs = Math.max(0, Date.now() - new Date(run.startedAt).getTime());
      run.error = "服务异常退出，运行状态已由恢复任务收敛";
      run.retryable = true;
      const event = {
        id: `trace_${randomUUID()}`, runId, type: "recovery", status: "failed",
        createdAt: finishedAt, message: run.error, durationMs: run.durationMs,
      };
      const alert = {
        id: `alert_${randomUUID()}`, runId, ruleName: "Agent 运行超时未收敛",
        message: run.error, status: "active", createdAt: finishedAt,
      };
      const [update] = await connection.execute<any>(
        `UPDATE observability_runs SET finished_at = ?, run_json = CAST(? AS JSON)
         WHERE owner_id = ? AND run_id = ? AND finished_at IS NULL`,
        [new Date(finishedAt), JSON.stringify(run), ownerId, runId],
      );
      if (Number(update.affectedRows) !== 1) continue;
      await connection.execute(
        `INSERT INTO observability_events (owner_id, event_id, run_id, created_at, event_json)
         VALUES (?, ?, ?, ?, CAST(? AS JSON))`,
        [ownerId, event.id, runId, new Date(finishedAt), JSON.stringify(event)],
      );
      await connection.execute(
        `INSERT INTO observability_alerts (owner_id, alert_id, run_id, status, created_at, alert_json)
         VALUES (?, ?, ?, 'active', ?, CAST(? AS JSON))`,
        [ownerId, alert.id, runId, new Date(finishedAt), JSON.stringify(alert)],
      );
      recovered += 1;
    }
    await connection.commit();
    return recovered;
  } catch (error) {
    await connection.rollback();
    throw error;
  } finally {
    connection.release();
  }
}

/** 新建或覆盖通用 JSON 文档，同时递增版本号。 */
export async function writeDocument(
  key: string,
  value: unknown,
): Promise<void> {
  await verifyMysqlStorage();
  await getPool().execute(
    `INSERT INTO app_documents (document_key, document_json)
     VALUES (?, CAST(? AS JSON))
     ON DUPLICATE KEY UPDATE
       document_json = VALUES(document_json), version = version + 1`,
    [key, JSON.stringify(value)],
  );
}

/** 按“用户 + 业务会话”读取 Pi Session 的完整 entries 树。 */
export async function readPiSession(
  ownerId: string,
  conversationId: string,
): Promise<StoredPiSession | null> {
  await verifyMysqlStorage();
  const [rows] = await getPool().execute<RowDataPacket[]>(
    `SELECT session_id, entries_json FROM pi_sessions
     WHERE owner_id = ? AND conversation_id = ?`,
    [ownerId, conversationId],
  );

  if (!rows[0]) return null;
  return {
    sessionId: String(rows[0].session_id),
    entries: parseJson<FileEntry[]>(rows[0].entries_json),
  };
}

/** 新建或更新一条业务会话对应的 Pi Session。 */
export async function writePiSession(
  ownerId: string,
  conversationId: string,
  sessionId: string,
  entries: FileEntry[],
): Promise<void> {
  await verifyMysqlStorage();
  await getPool().execute(
    `INSERT INTO pi_sessions (
       owner_id, conversation_id, session_id, entries_json
     ) VALUES (?, ?, ?, CAST(? AS JSON))
     ON DUPLICATE KEY UPDATE
       session_id = VALUES(session_id),
       entries_json = VALUES(entries_json),
       version = version + 1`,
    [
      ownerId,
      conversationId,
      sessionId,
      JSON.stringify(entries),
    ],
  );
}

/** 删除业务会话时同步删除数据库中的 Pi 上下文。 */
export async function deletePiSession(
  ownerId: string,
  conversationId: string,
): Promise<void> {
  // file 模式由文件存储逻辑处理，这里不访问 MySQL。
  if (!usesMysqlStorage()) return;
  await verifyMysqlStorage();
  await getPool().execute(
    "DELETE FROM pi_sessions WHERE owner_id = ? AND conversation_id = ?",
    [ownerId, conversationId],
  );
}

/** 给 /health 路由返回统一、可序列化的存储健康状态。 */
export async function mysqlStorageHealth() {
  if (!usesMysqlStorage()) {
    return { backend: "file" as const, healthy: true };
  }

  try {
    await verifyMysqlStorage();
    await getPool().query("SELECT 1");
    return { backend: "mysql" as const, healthy: true };
  } catch (error: any) {
    // 健康检查不把异常抛给 Express，而是返回可展示的错误信息。
    return {
      backend: "mysql" as const,
      healthy: false,
      error: error?.message || String(error),
    };
  }
}

/** 服务退出时关闭连接池，避免连接残留或测试进程无法结束。 */
export async function closeMysqlStorage(): Promise<void> {
  if (!pool) return;

  // 先保存局部变量并清空模块状态，关闭后仍可在下一次启动重新创建。
  const activePool = pool;
  pool = undefined;
  verification = undefined;
  await activePool.end();
}
