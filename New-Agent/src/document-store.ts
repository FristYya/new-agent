import { mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import {
  compareAndSwapDocument,
  readDocument,
  readVersionedDocument,
  usesMysqlStorage,
  writeDocument,
} from "./storage/mysql.ts";

const dataDir = process.env.NEW_AGENT_DATA_DIR || join(process.cwd(), "data");
const queues = new Map<string, Promise<void>>();

function safePart(value: string): string {
  return encodeURIComponent(value).replaceAll("%", "_");
}

function filePath(ownerId: string, name: string): string {
  return join(dataDir, "documents", safePart(ownerId), `${safePart(name)}.json`);
}

function mysqlKey(ownerId: string, name: string): string {
  return `${name}:${ownerId}`;
}

export async function readOwnerDocument<T>(
  ownerId: string,
  name: string,
): Promise<T | null> {
  if (usesMysqlStorage()) return readDocument<T>(mysqlKey(ownerId, name));
  try {
    return JSON.parse(await readFile(filePath(ownerId, name), "utf-8")) as T;
  } catch (error: any) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
}

export async function writeOwnerDocument(
  ownerId: string,
  name: string,
  value: unknown,
): Promise<void> {
  const queueKey = `${ownerId}:${name}`;
  const previous = queues.get(queueKey) ?? Promise.resolve();
  const current = previous.catch(() => undefined).then(async () => {
    if (usesMysqlStorage()) {
      await writeDocument(mysqlKey(ownerId, name), value);
      return;
    }
    const target = filePath(ownerId, name);
    const temporary = `${target}.${process.pid}.${Date.now()}.tmp`;
    await mkdir(dirname(target), { recursive: true });
    try {
      await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, {
        encoding: "utf-8",
        mode: 0o600,
      });
      await rename(temporary, target);
    } catch (error) {
      await rm(temporary, { force: true }).catch(() => undefined);
      throw error;
    }
  });
  queues.set(queueKey, current);
  await current.finally(() => {
    if (queues.get(queueKey) === current) queues.delete(queueKey);
  });
}

/** 跨进程安全的 read-modify-write；MySQL 冲突时重新读取并重放 mutation。 */
export async function mutateOwnerDocument<T, R>(
  ownerId: string,
  name: string,
  initial: () => T,
  mutation: (value: T) => R | Promise<R>,
): Promise<R> {
  if (!usesMysqlStorage()) {
    const queueKey = `mutation:${ownerId}:${name}`;
    const previous = queues.get(queueKey) ?? Promise.resolve();
    let result!: R;
    const current = previous.catch(() => undefined).then(async () => {
      const value = await readOwnerDocument<T>(ownerId, name) ?? initial();
      result = await mutation(value);
      await writeOwnerDocument(ownerId, name, value);
    });
    queues.set(queueKey, current);
    await current.finally(() => {
      if (queues.get(queueKey) === current) queues.delete(queueKey);
    });
    return result;
  }

  const key = mysqlKey(ownerId, name);
  for (let attempt = 0; attempt < 8; attempt += 1) {
    const stored = await readVersionedDocument<T>(key);
    const value = stored?.value ?? initial();
    const result = await mutation(value);
    if (await compareAndSwapDocument(key, value, stored?.version ?? 0)) return result;
  }
  throw new Error(`并发更新冲突：${name}，请稍后重试`);
}
