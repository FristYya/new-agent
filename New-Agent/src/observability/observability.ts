import { randomUUID } from "node:crypto";
import { mutateOwnerDocument, readOwnerDocument } from "../document-store.ts";
import { safePreview } from "../security/redaction.ts";
import { runtimeConfig } from "../config.ts";
import {
  acknowledgeObservabilityAlert,
  appendObservabilityEvent,
  finishObservabilityRun,
  insertObservabilityRun,
  pruneObservabilityRows,
  readObservabilityRows,
  usesMysqlStorage,
} from "../storage/mysql.ts";

export type RunStatus = "running" | "succeeded" | "failed" | "canceled" | "timed_out";

export type ModelUsage = {
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheWriteTokens: number;
  totalTokens: number;
  costUsd: number;
  ttftMs?: number;
  tokensPerSecond?: number;
  retryCount: number;
};

export type TraceEvent = {
  id: string;
  runId: string;
  type: string;
  status: "running" | "success" | "failed" | "timeout" | "aborted";
  createdAt: string;
  toolCallId?: string;
  toolName?: string;
  name?: string;
  skillName?: string;
  inputPreview?: string;
  outputPreview?: string;
  message?: string;
  durationMs?: number;
};

export type ObservedRun = {
  id: string;
  traceId: string;
  conversationId: string;
  agentId: string;
  model: string;
  prompt: string;
  outputPreview?: string;
  error?: string;
  status: RunStatus;
  startedAt: string;
  updatedAt: string;
  finishedAt?: string;
  durationMs?: number;
  toolCalls: number;
  retryable: boolean;
  loadedSkills: string[];
  activeTools: string[];
  targetSnapshot: Record<string, unknown>;
  modelUsage: ModelUsage;
};

type Alert = {
  id: string;
  runId: string;
  ruleName: string;
  message: string;
  status: "active" | "acknowledged";
  createdAt: string;
  acknowledgedAt?: string;
  severity?: "warning" | "critical";
};

type ObservabilityState = {
  runs: ObservedRun[];
  events: TraceEvent[];
  alerts: Alert[];
};

const emptyState = (): ObservabilityState => ({ runs: [], events: [], alerts: [] });
const MAX_RUNS = 500;
const MAX_EVENTS = 10_000;

function normalizeState(value?: Partial<ObservabilityState> | null): ObservabilityState {
  const emptyUsage: ModelUsage = {
    inputTokens: 0, outputTokens: 0, cacheReadTokens: 0,
    cacheWriteTokens: 0, totalTokens: 0, costUsd: 0, retryCount: 0,
  };
  return {
    runs: Array.isArray(value?.runs) ? value.runs.map((run) => ({
      ...run,
      loadedSkills: Array.isArray(run.loadedSkills) ? run.loadedSkills : [],
      activeTools: Array.isArray(run.activeTools) ? run.activeTools : [],
      targetSnapshot: run.targetSnapshot ?? {},
      modelUsage: { ...emptyUsage, ...(run.modelUsage ?? {}) },
    })) : [],
    events: Array.isArray(value?.events) ? value.events : [],
    alerts: Array.isArray(value?.alerts) ? value.alerts : [],
  };
}

async function readState(ownerId: string): Promise<ObservabilityState> {
  if (usesMysqlStorage()) {
    const current = normalizeState(await readObservabilityRows(ownerId, MAX_RUNS));
    // 兼容升级前存放在 app_documents 中的观测历史；新数据只写规范化表。
    const legacy = await readOwnerDocument<ObservabilityState>(ownerId, "observability");
    if (!legacy) return current;
    const normalizedLegacy = normalizeState(legacy);
    const currentRunIds = new Set(current.runs.map((run) => run.id));
    const runs = [...current.runs, ...normalizedLegacy.runs.filter((run) => !currentRunIds.has(run.id))]
      .sort((a, b) => b.startedAt.localeCompare(a.startedAt)).slice(0, MAX_RUNS);
    const retained = new Set(runs.map((run) => run.id));
    return {
      runs,
      events: [...current.events, ...normalizedLegacy.events].filter((event) => retained.has(event.runId)).slice(-MAX_EVENTS),
      alerts: [...current.alerts, ...normalizedLegacy.alerts].filter((alert) => retained.has(alert.runId)).slice(0, 500),
    };
  }
  const value = await readOwnerDocument<ObservabilityState>(ownerId, "observability");
  return normalizeState(value);
}

function mutate<T>(ownerId: string, action: (state: ObservabilityState) => T | Promise<T>): Promise<T> {
  return mutateOwnerDocument(ownerId, "observability", emptyState, async (state) => {
    Object.assign(state, normalizeState(state));
    const result = await action(state);
    state.runs = state.runs.slice(0, MAX_RUNS);
    const retained = new Set(state.runs.map((run) => run.id));
    state.events = state.events.filter((event) => retained.has(event.runId)).slice(-MAX_EVENTS);
    state.alerts = state.alerts.filter((alert) => retained.has(alert.runId)).slice(0, 500);
    return result;
  });
}

function trace(runId: string, type: string, status: TraceEvent["status"], fields: Partial<TraceEvent> = {}): TraceEvent {
  return { id: `trace_${randomUUID()}`, runId, type, status, createdAt: new Date().toISOString(), ...fields };
}

/** Provider usage fields are external input; reject NaN/Infinity/negative values before aggregation. */
function usageNumber(value: unknown): number {
  const parsed = Number(value ?? 0);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
}

export type RunStart = Pick<ObservedRun, "conversationId" | "agentId" | "model"> & {
  prompt: string;
  loadedSkills: string[];
  activeTools: string[];
  targetSnapshot?: Record<string, unknown>;
};

export async function startObservedRun(ownerId: string, input: RunStart): Promise<ObservedRun> {
  const now = new Date().toISOString();
  const run: ObservedRun = {
    id: `run_${randomUUID()}`,
    traceId: randomUUID(),
    conversationId: input.conversationId,
    agentId: input.agentId,
    model: input.model,
    prompt: safePreview(input.prompt, 1000),
    status: "running",
    startedAt: now,
    updatedAt: now,
    toolCalls: 0,
    retryable: false,
    loadedSkills: input.loadedSkills,
    activeTools: input.activeTools,
    targetSnapshot: input.targetSnapshot ?? {},
    modelUsage: {
      inputTokens: 0, outputTokens: 0, cacheReadTokens: 0,
      cacheWriteTokens: 0, totalTokens: 0, costUsd: 0, retryCount: 0,
    },
  };
  const events = [trace(run.id, "run_start", "running", { inputPreview: run.prompt })];
  for (const skillName of run.loadedSkills) {
    events.push(trace(run.id, "skill_trace", "success", {
        name: skillName,
        skillName,
        message: "本轮已加载该 Skill（不代表模型实际调用）",
    }));
  }
  if (usesMysqlStorage()) {
    await insertObservabilityRun(ownerId, run, events);
  } else {
    await mutate(ownerId, (state) => {
      state.runs.unshift(run);
      state.events.push(...events);
    });
  }
  return run;
}

export type RunRecorder = ReturnType<typeof createRunRecorder>;

export function createRunRecorder(ownerId: string, run: ObservedRun) {
  const toolStarted = new Map<string, { startedAt: number; toolName?: string }>();
  let firstTokenAt: number | undefined;
  let output = "";
  let usage = { ...run.modelUsage };
  let toolCalls = 0;
  const pendingEvents = new Set<Promise<void>>();

  async function event(type: string, status: TraceEvent["status"], fields: Partial<TraceEvent> = {}) {
    const item = trace(run.id, type, status, fields);
    if (usesMysqlStorage()) await appendObservabilityEvent(ownerId, item);
    else await mutate(ownerId, (state) => state.events.push(item));
  }

  function queueEvent(type: string, status: TraceEvent["status"], fields: Partial<TraceEvent> = {}): void {
    const pending = event(type, status, fields)
      .catch((eventError) => console.error("[observability:event-error]", eventError))
      .finally(() => pendingEvents.delete(pending));
    pendingEvents.add(pending);
  }

  function consumePiEvent(piEvent: any): void {
    const now = Date.now();
    if (piEvent.type === "message_update" && piEvent.assistantMessageEvent?.type === "text_delta") {
      firstTokenAt ??= now;
      if (output.length < 20_000) output += String(piEvent.assistantMessageEvent.delta ?? "").slice(0, 20_000 - output.length);
      return;
    }
    if (piEvent.type === "message_end" && piEvent.message?.role === "assistant") {
      const item = piEvent.message;
      const raw = item.usage ?? {};
      const inputTokens = usageNumber(raw.input ?? raw.inputTokens);
      const outputTokens = usageNumber(raw.output ?? raw.outputTokens);
      usage.inputTokens += inputTokens;
      usage.outputTokens += outputTokens;
      usage.cacheReadTokens += usageNumber(raw.cacheRead ?? raw.cacheReadTokens);
      usage.cacheWriteTokens += usageNumber(raw.cacheWrite ?? raw.cacheWriteTokens);
      usage.costUsd += usageNumber(raw.cost?.total ?? raw.cost);
      usage.totalTokens += raw.totalTokens === undefined
        ? inputTokens + outputTokens
        : usageNumber(raw.totalTokens);
      return;
    }
    if (piEvent.type === "tool_execution_start") {
      toolCalls += 1;
      toolStarted.set(piEvent.toolCallId, { startedAt: now, toolName: piEvent.toolName });
      queueEvent("tool_start", "running", {
        toolCallId: piEvent.toolCallId, toolName: piEvent.toolName, name: piEvent.toolName,
        inputPreview: safePreview(piEvent.args ?? {}, 500),
      });
      return;
    }
    if (piEvent.type === "tool_execution_end") {
      const started = toolStarted.get(piEvent.toolCallId);
      toolStarted.delete(piEvent.toolCallId);
      queueEvent("tool_end", piEvent.isError ? "failed" : "success", {
        toolCallId: piEvent.toolCallId, toolName: piEvent.toolName, name: piEvent.toolName,
        outputPreview: safePreview(piEvent.result ?? {}, 500),
        durationMs: started ? now - started.startedAt : undefined,
      });
      return;
    }
    if (piEvent.type === "auto_retry_start") {
      usage.retryCount += 1;
      queueEvent("retry", "running", { message: safePreview(piEvent.errorMessage ?? "模型重试", 500) });
    }
  }

  async function finish(status: Exclude<RunStatus, "running">, error?: unknown): Promise<void> {
    // 确保最终状态落库前，前面的 Tool/重试事件已经完成；单条事件失败只记录日志，
    // 不阻断聊天主链路，也不会让 Run 永久停在 running。
    await Promise.allSettled([...pendingEvents]);
    const finished = Date.now();
    const danglingToolCount = toolStarted.size;
    for (const [toolCallId, started] of toolStarted) {
      queueEvent("tool_end", status === "timed_out" ? "timeout" : status === "canceled" ? "aborted" : "failed", {
        toolCallId,
        toolName: started.toolName,
        name: started.toolName,
        message: status === "succeeded" ? "运行结束前 Tool 未返回结果" : `运行以 ${status} 结束，Tool 调用未完成`,
        durationMs: Math.max(0, finished - started.startedAt),
      });
    }
    toolStarted.clear();
    await Promise.allSettled([...pendingEvents]);
    const durationMs = Math.max(0, finished - new Date(run.startedAt).getTime());
    const ttftMs = firstTokenAt ? firstTokenAt - new Date(run.startedAt).getTime() : undefined;
    usage = {
      ...usage,
      ttftMs,
      tokensPerSecond: durationMs > 0 ? usage.outputTokens / (durationMs / 1000) : undefined,
    };
    const finishedAt = new Date(finished).toISOString();
    const stored: ObservedRun = {
      ...run,
      status,
      finishedAt,
      updatedAt: finishedAt,
      durationMs,
      outputPreview: safePreview(output, 2000),
      error: error ? safePreview(error instanceof Error ? error.message : error, 1000) : undefined,
      retryable: status === "failed" || status === "timed_out" || status === "canceled",
      modelUsage: usage,
      toolCalls,
    };
    const finalEvent = trace(run.id, status === "succeeded" ? "run_end" : "error", status === "succeeded" ? "success" : status === "timed_out" ? "timeout" : status === "canceled" ? "aborted" : "failed", {
      outputPreview: stored.outputPreview,
      message: stored.error,
      durationMs,
    });
    const alertReasons: string[] = [];
    if (status !== "succeeded") alertReasons.push(stored.error || `运行状态：${status}`);
    if (danglingToolCount > 0) alertReasons.push(`${danglingToolCount} 个 Tool 调用在运行结束时仍未完成`);
    if (durationMs >= runtimeConfig.observabilityAlertDurationMs) {
      alertReasons.push(`运行耗时 ${durationMs}ms 超过阈值 ${runtimeConfig.observabilityAlertDurationMs}ms`);
    }
    if (runtimeConfig.observabilityAlertCostUsd > 0 && usage.costUsd >= runtimeConfig.observabilityAlertCostUsd) {
      alertReasons.push(`单次成本 $${usage.costUsd.toFixed(4)} 超过阈值 $${runtimeConfig.observabilityAlertCostUsd.toFixed(4)}`);
    }
    const alert: Alert | undefined = alertReasons.length ? {
      id: `alert_${randomUUID()}`, runId: run.id,
      ruleName: status === "succeeded" ? "Agent 运行 SLO 告警" : "Agent 运行异常",
      message: alertReasons.join("；"), status: "active", createdAt: finishedAt,
      severity: status === "succeeded" ? "warning" : "critical",
    } : undefined;
    if (usesMysqlStorage()) {
      await finishObservabilityRun(ownerId, stored, finalEvent, alert);
      void pruneObservabilityRows(ownerId, MAX_RUNS).catch((pruneError) => {
        console.error("[observability:retention-error]", pruneError);
      });
    } else {
      await mutate(ownerId, (state) => {
        const index = state.runs.findIndex((item) => item.id === run.id);
        if (index >= 0) state.runs[index] = stored;
        state.events.push(finalEvent);
        if (alert) state.alerts.unshift(alert);
      });
    }
  }
  return { consumePiEvent, finish, event, getOutput: () => output };
}

function percentile(values: number[], fraction: number): number {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.floor((sorted.length - 1) * fraction))];
}

export async function getObservability(ownerId: string, filters?: {
  query?: string;
  status?: string;
  from?: string;
  to?: string;
  limit?: number;
  offset?: number;
}) {
  const state = await readState(ownerId);
  const query = filters?.query?.toLowerCase();
  const from = filters?.from ? new Date(filters.from).getTime() : Number.NEGATIVE_INFINITY;
  const to = filters?.to ? new Date(filters.to).getTime() : Number.POSITIVE_INFINITY;
  const filteredRuns = state.runs.filter((run) =>
    (!filters?.status || run.status === filters.status) &&
    (Number.isNaN(from) || new Date(run.startedAt).getTime() >= from) &&
    (Number.isNaN(to) || new Date(run.startedAt).getTime() <= to) &&
    (!query || `${run.prompt} ${run.outputPreview ?? ""} ${run.error ?? ""} ${run.traceId}`.toLowerCase().includes(query))
  );
  const requestedLimit = Number(filters?.limit ?? MAX_RUNS);
  const requestedOffset = Number(filters?.offset ?? 0);
  const limit = Number.isFinite(requestedLimit) ? Math.max(1, Math.min(MAX_RUNS, Math.floor(requestedLimit))) : MAX_RUNS;
  const offset = Number.isFinite(requestedOffset) ? Math.max(0, Math.floor(requestedOffset)) : 0;
  const runs = filteredRuns.slice(offset, offset + limit);
  const pageRunIds = new Set(runs.map((run) => run.id));
  const completed = state.runs.filter((run) => run.status !== "running");
  const durations = completed.map((run) => run.durationMs ?? 0);
  const successful = completed.filter((run) => run.status === "succeeded");
  const toolEnds = state.events.filter((event) => event.type === "tool_end");
  const toolNames = new Set(toolEnds.map((event) => event.toolName).filter(Boolean) as string[]);
  const toolStats = [...toolNames].map((name) => {
    const items = toolEnds.filter((event) => event.toolName === name);
    const failed = items.filter((event) => event.status === "failed").length;
    return { name, label: name, status: failed ? "degraded" : "healthy", calls: items.length, failed, avgMs: items.reduce((sum, item) => sum + (item.durationMs ?? 0), 0) / items.length };
  });
  const skillNames = new Set(state.runs.flatMap((run) => run.loadedSkills));
  const skillStats = [...skillNames].map((name) => {
    const items = state.runs.filter((run) => run.loadedSkills.includes(name));
    const failed = items.filter((run) => run.status === "failed" || run.status === "timed_out").length;
    return {
      name,
      label: `${name}（已加载）`,
      status: failed ? "degraded" : "healthy",
      calls: items.length,
      failed,
      avgMs: items.reduce((sum, run) => sum + (run.durationMs ?? 0), 0) / items.length,
    };
  });
  const overview = {
    totalRuns: state.runs.length,
    successRuns: successful.length,
    failedRuns: completed.length - successful.length,
    runningRuns: state.runs.filter((run) => run.status === "running").length,
    successRate: completed.length ? Math.round(successful.length / completed.length * 100) : 0,
    p50DurationMs: percentile(durations, .5), p95DurationMs: percentile(durations, .95), p99DurationMs: percentile(durations, .99),
    inputTokens: state.runs.reduce((sum, run) => sum + run.modelUsage.inputTokens, 0),
    outputTokens: state.runs.reduce((sum, run) => sum + run.modelUsage.outputTokens, 0),
    totalCostUsd: state.runs.reduce((sum, run) => sum + run.modelUsage.costUsd, 0),
    avgTtftMs: completed.length ? completed.reduce((sum, run) => sum + (run.modelUsage.ttftMs ?? 0), 0) / completed.length : 0,
    avgTokensPerSecond: completed.length ? completed.reduce((sum, run) => sum + (run.modelUsage.tokensPerSecond ?? 0), 0) / completed.length : 0,
    modelRetries: state.runs.reduce((sum, run) => sum + run.modelUsage.retryCount, 0),
    activeAlerts: state.alerts.filter((alert) => alert.status === "active").length,
  };
  const trendMap = new Map<string, { date: string; runs: number; succeeded: number; durationMs: number[]; costUsd: number }>();
  for (const run of state.runs) {
    const date = run.startedAt.slice(0, 10);
    const point = trendMap.get(date) ?? { date, runs: 0, succeeded: 0, durationMs: [], costUsd: 0 };
    point.runs += 1;
    if (run.status === "succeeded") point.succeeded += 1;
    if (run.durationMs !== undefined) point.durationMs.push(run.durationMs);
    point.costUsd += run.modelUsage.costUsd;
    trendMap.set(date, point);
  }
  const trends = [...trendMap.values()].sort((a, b) => a.date.localeCompare(b.date)).slice(-30).map((point) => ({
    date: point.date,
    runs: point.runs,
    successRate: point.runs ? Math.round(point.succeeded / point.runs * 100) : 0,
    p95DurationMs: percentile(point.durationMs, .95),
    costUsd: point.costUsd,
  }));
  return {
    overview,
    runs,
    events: state.events.filter((event) => pageRunIds.has(event.runId)),
    alerts: state.alerts,
    toolStats,
    skillStats,
    trends,
    pagination: { total: filteredRuns.length, limit, offset, hasMore: offset + runs.length < filteredRuns.length },
  };
}

export async function acknowledgeAlert(ownerId: string, alertId: string): Promise<boolean> {
  if (usesMysqlStorage()) {
    const state = await readState(ownerId);
    const alert = state.alerts.find((item) => item.id === alertId);
    if (!alert) return false;
    alert.status = "acknowledged";
    alert.acknowledgedAt = new Date().toISOString();
    if (await acknowledgeObservabilityAlert(ownerId, alertId, alert)) return true;
    // 旧版 JSON 历史中的告警仍允许确认，便于无停机升级后逐步消化。
    return mutate(ownerId, (legacy) => {
      const item = legacy.alerts.find((entry) => entry.id === alertId);
      if (!item) return false;
      Object.assign(item, alert);
      return true;
    });
  }
  return mutate(ownerId, (state) => {
    const alert = state.alerts.find((item) => item.id === alertId);
    if (!alert) return false;
    alert.status = "acknowledged";
    alert.acknowledgedAt = new Date().toISOString();
    return true;
  });
}

export async function getObservedRun(ownerId: string, runId: string): Promise<{ run: ObservedRun; events: TraceEvent[] } | null> {
  const state = await readState(ownerId);
  const run = state.runs.find((item) => item.id === runId);
  return run ? { run, events: state.events.filter((event) => event.runId === runId) } : null;
}
