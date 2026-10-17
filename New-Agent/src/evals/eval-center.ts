import { createHash, randomUUID } from "node:crypto";
import { runtimeConfig } from "../config.ts";
import { mutateOwnerDocument, readOwnerDocument } from "../document-store.ts";
import { evaluateCase, validateEvalCase, type EvalCase, type EvalResult } from "./evaluator.ts";
import { wilsonInterval, type ConfidenceInterval } from "./statistics.ts";

export type EvalSuite = "capability" | "regression" | "canary" | "shadow" | "adversarial";
export type DatasetStatus = "draft" | "published" | "retired";
export type EvalDataset = {
  id: string;
  name: string;
  suite: EvalSuite;
  status: DatasetStatus;
  owner: string;
  version: number;
  versions: DatasetVersion[];
  publishedVersion?: number;
  publishedAt?: string;
  retiredAt?: string;
  createdAt: string;
  updatedAt: string;
};
export type DatasetVersion = { version: number; hash: string; caseIds: string[]; cases: EvalCase[]; createdAt: string };
export type EvalProfile = { id: string; agentId: string; name: string; version: number; objectives: string[]; metrics: Record<string, number>; redLines: string[]; requiredTags: string[]; reviewRules: string[]; createdAt: string; updatedAt: string };
export type ReleaseGate = {
  minPassRate: number;
  minWeightedPassRate: number;
  maxRegressions: number;
  blockCritical: boolean;
  requirePublishedDataset: boolean;
  minTrialsPerCase: number;
  minConfidenceLowerBound: number;
};
export type ReleaseDecision = "approved" | "review_required" | "inconclusive" | "blocked";
export type EvalReview = { id: string; runId: string; caseId: string; status: "pending" | "approved" | "rejected" | "false_positive" | "resolved"; note?: string; createdAt: string; updatedAt: string; decidedBy?: string };
export type EvalAuditEvent = {
  id: string;
  action: string;
  actorId: string;
  entityType: "case" | "dataset" | "profile" | "gate" | "baseline" | "review";
  entityId: string;
  beforeHash?: string;
  afterHash?: string;
  metadata?: Record<string, unknown>;
  createdAt: string;
  /** Hash chain fields are optional only for records created before this schema existed. */
  previousHash?: string;
  eventHash?: string;
};

export type EvalAuditIntegrity = {
  valid: boolean;
  checked: number;
  legacy: number;
  errors: Array<{ id: string; reason: "hash_mismatch" | "chain_broken" }>;
};
export type EvalJob = {
  id: string;
  name: string;
  status: "queued" | "running" | "completed" | "failed" | "canceled";
  payload?: RunEvalOptions;
  attempts?: number;
  leaseId?: string;
  leaseExpiresAt?: string;
  cancelRequested?: boolean;
  runId?: string;
  error?: string;
  progress?: { completed: number; total: number; percent: number; phase: string; currentCaseId?: string; updatedAt: string };
  createdAt: string;
  updatedAt: string;
};

export type EvalRun = {
  id: string; jobId?: string; name: string; startedAt: string; finishedAt: string; durationMs: number;
  datasetId?: string; datasetVersion?: number; datasetHash: string;
  profileSnapshot?: EvalProfile; targetSnapshot: Record<string, unknown>; caseSnapshot: EvalCase[];
  evidenceSnapshot?: Record<string, unknown>;
  results: EvalResult[];
  trials: Array<{ caseId: string; trial: number; result: EvalResult }>;
  statistics: {
    requestedTrialsPerCase: number;
    totalTrials: number;
    passedTrials: number;
    trialPassRate: number;
    confidenceInterval?: ConfidenceInterval;
    cases: Array<{
      caseId: string;
      trials: number;
      passedTrials: number;
      successRate: number;
      passAtK: boolean;
      passPowerK: boolean;
      confidenceInterval?: ConfidenceInterval;
    }>;
  };
  summary: { total: number; passed: number; failed: number; passRate: number; weightedPassRate: number };
  categories: Array<{ category: string; total: number; passed: number; passRate: number }>;
  comparison?: { baselineRunId: string; passRateDelta: number; regressions: string[]; improvements: string[] };
  gate: { passed: boolean; decision: ReleaseDecision; reasons: string[] };
  releaseDecision?: { passed: boolean; decision: ReleaseDecision; reasons: string[]; updatedAt: string };
  reportSnapshot: { failedCaseIds: string[]; regressions: string[]; improvements: string[]; checklist: string[] };
};

export type EvalState = {
  cases: EvalCase[]; datasets: EvalDataset[]; profiles: EvalProfile[]; runs: EvalRun[];
  reviews: EvalReview[]; jobs: EvalJob[]; gate: ReleaseGate; baselineRunId?: string;
  baselineRunIds?: Record<string, string>;
  runnerLease?: { id: string; expiresAt: string };
  auditLog: EvalAuditEvent[];
};

const now = () => new Date().toISOString();
const clone = <T>(value: T): T => structuredClone(value);

function initialState(): EvalState {
  const createdAt = now();
  return {
    cases: [],
    datasets: [{ id: "dataset_default", name: "默认回归集", suite: "regression", status: "published", owner: "system", version: 0, versions: [], publishedAt: createdAt, createdAt, updatedAt: createdAt }],
    profiles: [], runs: [], reviews: [], jobs: [], auditLog: [],
    gate: { minPassRate: 1, minWeightedPassRate: 1, maxRegressions: 0, blockCritical: true, requirePublishedDataset: true, minTrialsPerCase: 1, minConfidenceLowerBound: 0 },
  };
}

function normalizeState(value?: Partial<EvalState> | null): EvalState {
  const base = initialState();
  return {
    ...base,
    ...value,
    cases: Array.isArray(value?.cases) ? value.cases : [],
    datasets: Array.isArray(value?.datasets) && value.datasets.length
      ? value.datasets.map((dataset) => ({
        ...dataset,
        suite: dataset.suite ?? "regression",
        status: dataset.status ?? "draft",
        owner: dataset.owner ?? "",
      }))
      : base.datasets,
    profiles: Array.isArray(value?.profiles) ? value.profiles : [],
    runs: Array.isArray(value?.runs) ? value.runs : [],
    reviews: Array.isArray(value?.reviews) ? value.reviews : [],
    jobs: Array.isArray(value?.jobs) ? value.jobs : [],
    auditLog: Array.isArray(value?.auditLog) ? value.auditLog : [],
    gate: { ...base.gate, ...(value?.gate ?? {}) },
  };
}

export async function readEvalState(ownerId: string): Promise<EvalState> {
  return normalizeState(await readOwnerDocument<EvalState>(ownerId, "evaluations"));
}

function mutate<T>(ownerId: string, action: (state: EvalState) => T | Promise<T>): Promise<T> {
  return mutateOwnerDocument(ownerId, "evaluations", initialState, async (state) => {
    Object.assign(state, normalizeState(state));
    const result = await action(state);
    const baselineIds = new Set(Object.values(state.baselineRunIds ?? {}));
    if (state.baselineRunId) baselineIds.add(state.baselineRunId);
    state.runs = state.runs.filter((run, index) => index < 200 || baselineIds.has(run.id));
    const retainedRunIds = new Set(state.runs.map((run) => run.id));
    state.reviews = state.reviews.filter((review) => retainedRunIds.has(review.runId)).slice(0, 2_000);
    const activeJobs = state.jobs.filter((job) => job.status === "queued" || job.status === "running");
    const terminalJobs = state.jobs.filter((job) => job.status !== "queued" && job.status !== "running").slice(0, 100);
    state.jobs = [...activeJobs, ...terminalJobs];
    state.auditLog = state.auditLog.slice(0, 5_000);
    return result;
  });
}

function hash(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

function audit(
  state: EvalState,
  action: string,
  actorId: string | undefined,
  entityType: EvalAuditEvent["entityType"],
  entityId: string,
  before?: unknown,
  after?: unknown,
  metadata?: Record<string, unknown>,
): void {
  const event: Omit<EvalAuditEvent, "eventHash"> = {
    id: `audit_${randomUUID()}`,
    action,
    actorId: actorId || "system",
    entityType,
    entityId,
    beforeHash: before === undefined ? undefined : hash(before),
    afterHash: after === undefined ? undefined : hash(after),
    metadata,
    createdAt: now(),
    previousHash: state.auditLog[0]?.eventHash,
  };
  state.auditLog.unshift({ ...event, eventHash: hash(event) });
}

/** Verify both event contents and ordering. The oldest retained hash is treated as the chain anchor. */
export function verifyEvalAuditEvents(events: EvalAuditEvent[]): EvalAuditIntegrity {
  const chronological = [...events].reverse();
  const errors: EvalAuditIntegrity["errors"] = [];
  let checked = 0;
  let legacy = 0;
  let expectedPrevious = chronological[0]?.previousHash;
  for (const event of chronological) {
    if (!event.eventHash) {
      legacy += 1;
      expectedPrevious = undefined;
      continue;
    }
    checked += 1;
    if (event.previousHash !== expectedPrevious) {
      errors.push({ id: event.id, reason: "chain_broken" });
    }
    const { eventHash, ...body } = event;
    if (hash(body) !== eventHash) {
      errors.push({ id: event.id, reason: "hash_mismatch" });
    }
    expectedPrevious = eventHash;
  }
  return { valid: errors.length === 0, checked, legacy, errors };
}

function snapshotDataset(state: EvalState, datasetId: string): DatasetVersion {
  const dataset = state.datasets.find((item) => item.id === datasetId);
  if (!dataset) throw new Error("评测数据集不存在");
  const cases = clone(state.cases.filter((item) => item.datasetId === datasetId));
  const digest = hash(cases);
  const latest = dataset.versions.at(-1);
  if (latest?.hash === digest) return latest;
  const version = { version: dataset.version + 1, hash: digest, caseIds: cases.map((item) => item.id), cases, createdAt: now() };
  dataset.version = version.version;
  dataset.updatedAt = version.createdAt;
  dataset.versions.push(version);
  dataset.versions = dataset.versions.slice(-50);
  return version;
}

function markDatasetDirty(state: EvalState, datasetId: string): void {
  const dataset = state.datasets.find((item) => item.id === datasetId);
  if (dataset?.status === "published") {
    dataset.status = "draft";
    dataset.updatedAt = now();
  }
}

export async function getEvalDashboard(ownerId: string) {
  const state = await readEvalState(ownerId);
  const latest = state.runs[0];
  return {
    ...state,
    auditLog: state.auditLog.slice(0, 200),
    auditTotal: state.auditLog.length,
    auditIntegrity: verifyEvalAuditEvents(state.auditLog),
    summary: { totalCases: state.cases.length, enabledCases: state.cases.filter((item) => item.enabled).length, totalRuns: state.runs.length, latestPassRate: latest?.summary.passRate ?? 0 },
  };
}

export async function verifyEvalAudit(ownerId: string): Promise<EvalAuditIntegrity> {
  return verifyEvalAuditEvents((await readEvalState(ownerId)).auditLog);
}

export async function getEvalAudit(
  ownerId: string,
  filters: { action?: string; entityType?: string; limit?: number; offset?: number } = {},
) {
  const state = await readEvalState(ownerId);
  const items = state.auditLog.filter((item) =>
    (!filters.action || item.action === filters.action) &&
    (!filters.entityType || item.entityType === filters.entityType)
  );
  const requestedLimit = Number(filters.limit ?? 100);
  const requestedOffset = Number(filters.offset ?? 0);
  const limit = Number.isFinite(requestedLimit) ? Math.max(1, Math.min(500, Math.floor(requestedLimit))) : 100;
  const offset = Number.isFinite(requestedOffset) ? Math.max(0, Math.floor(requestedOffset)) : 0;
  const auditLog = items.slice(offset, offset + limit);
  return { auditLog, pagination: { total: items.length, limit, offset, hasMore: offset + auditLog.length < items.length } };
}

export async function createEvalCase(ownerId: string, payload: Partial<EvalCase>, actorId?: string): Promise<EvalCase> {
  const errors = validateEvalCase(payload);
  if (errors.length) throw new Error(errors.join("；"));
  return mutate(ownerId, (state) => {
    if (state.cases.length >= runtimeConfig.evalMaxStoredCases) {
      throw new Error(`评测用例已达到 ${runtimeConfig.evalMaxStoredCases} 条上限`);
    }
    if (!state.datasets.some((item) => item.id === payload.datasetId)) throw new Error("评测数据集不存在");
    const timestamp = now();
    const item = { ...payload, id: `case_${randomUUID()}`, description: String(payload.description ?? ""), tags: Array.isArray(payload.tags) ? payload.tags.map(String) : [], severity: payload.severity ?? "medium", weight: Number(payload.weight ?? 1), owner: String(payload.owner ?? ""), enabled: payload.enabled !== false, createdAt: timestamp, updatedAt: timestamp } as EvalCase;
    state.cases.unshift(item);
    snapshotDataset(state, item.datasetId);
    markDatasetDirty(state, item.datasetId);
    audit(state, "case.created", actorId, "case", item.id, undefined, item, { datasetId: item.datasetId });
    return clone(item);
  });
}

export async function updateEvalCase(ownerId: string, caseId: string, payload: Partial<EvalCase>, actorId?: string): Promise<EvalCase | null> {
  return mutate(ownerId, (state) => {
    const item = state.cases.find((entry) => entry.id === caseId);
    if (!item) return null;
    const before = clone(item);
    const next = { ...item, ...payload, id: item.id, createdAt: item.createdAt, updatedAt: now() } as EvalCase;
    const errors = validateEvalCase(next);
    if (errors.length) throw new Error(errors.join("；"));
    if (!state.datasets.some((dataset) => dataset.id === next.datasetId)) throw new Error("评测数据集不存在");
    Object.assign(item, next);
    if (before.datasetId !== item.datasetId) {
      snapshotDataset(state, before.datasetId);
      markDatasetDirty(state, before.datasetId);
    }
    snapshotDataset(state, item.datasetId);
    markDatasetDirty(state, item.datasetId);
    audit(state, "case.updated", actorId, "case", item.id, before, item, { datasetId: item.datasetId });
    return clone(item);
  });
}

export async function deleteEvalCase(ownerId: string, caseId: string, actorId?: string): Promise<boolean> {
  return mutate(ownerId, (state) => {
    const index = state.cases.findIndex((item) => item.id === caseId);
    if (index < 0) return false;
    const [removed] = state.cases.splice(index, 1);
    snapshotDataset(state, removed.datasetId);
    markDatasetDirty(state, removed.datasetId);
    audit(state, "case.deleted", actorId, "case", removed.id, removed, undefined, { datasetId: removed.datasetId });
    return true;
  });
}

export async function createDataset(
  ownerId: string,
  payload: string | { name?: string; suite?: EvalSuite; owner?: string },
  actorId?: string,
): Promise<EvalDataset> {
  const name = typeof payload === "string" ? payload : String(payload.name ?? "");
  const suite = typeof payload === "string" ? "regression" : (payload.suite ?? "regression");
  const datasetOwner = typeof payload === "string" ? actorId ?? ownerId : String(payload.owner ?? actorId ?? ownerId);
  if (!name.trim()) throw new Error("数据集名称不能为空");
  if (!["capability", "regression", "canary", "shadow", "adversarial"].includes(suite)) throw new Error("数据集 Suite 无效");
  return mutate(ownerId, (state) => {
    const timestamp = now();
    const item: EvalDataset = {
      id: `dataset_${randomUUID()}`,
      name: name.trim(),
      suite,
      status: "draft",
      owner: datasetOwner.trim(),
      version: 0,
      versions: [],
      createdAt: timestamp,
      updatedAt: timestamp,
    };
    state.datasets.unshift(item);
    audit(state, "dataset.created", actorId, "dataset", item.id, undefined, item);
    return clone(item);
  });
}

export async function updateDatasetLifecycle(
  ownerId: string,
  datasetId: string,
  patch: { status?: DatasetStatus; suite?: EvalSuite; owner?: string },
  actorId?: string,
): Promise<EvalDataset | null> {
  if (patch.status && !["draft", "published", "retired"].includes(patch.status)) throw new Error("数据集状态无效");
  if (patch.suite && !["capability", "regression", "canary", "shadow", "adversarial"].includes(patch.suite)) throw new Error("数据集 Suite 无效");
  return mutate(ownerId, (state) => {
    const dataset = state.datasets.find((item) => item.id === datasetId);
    if (!dataset) return null;
    const before = clone(dataset);
    const timestamp = now();
    if (patch.suite) dataset.suite = patch.suite;
    if (patch.owner !== undefined) dataset.owner = patch.owner.trim();
    if (patch.status && patch.status !== dataset.status) {
      if (patch.status === "published") {
        const version = snapshotDataset(state, dataset.id);
        if (!version.caseIds.length) throw new Error("空数据集不能发布");
        dataset.publishedAt = timestamp;
        dataset.publishedVersion = version.version;
        dataset.retiredAt = undefined;
      } else if (patch.status === "retired") {
        dataset.retiredAt = timestamp;
      } else {
        dataset.retiredAt = undefined;
      }
      dataset.status = patch.status;
    }
    dataset.updatedAt = timestamp;
    audit(state, "dataset.lifecycle_updated", actorId, "dataset", dataset.id, before, dataset, { status: dataset.status, suite: dataset.suite });
    return clone(dataset);
  });
}

export async function diffDataset(ownerId: string, datasetId: string) {
  const state = await readEvalState(ownerId);
  const dataset = state.datasets.find((item) => item.id === datasetId);
  if (!dataset) return null;
  const [before, after] = dataset.versions.slice(-2);
  if (!after) return { fromVersion: 0, toVersion: before?.version ?? 0, added: before?.caseIds ?? [], removed: [], changed: [], unchanged: [] };
  const oldMap = new Map(before.cases.map((item) => [item.id, item]));
  const newMap = new Map(after.cases.map((item) => [item.id, item]));
  const added = [...newMap.keys()].filter((id) => !oldMap.has(id));
  const removed = [...oldMap.keys()].filter((id) => !newMap.has(id));
  const shared = [...newMap.keys()].filter((id) => oldMap.has(id));
  const changed = shared.filter((id) => hash(oldMap.get(id)) !== hash(newMap.get(id)));
  return { fromVersion: before.version, toVersion: after.version, added, removed, changed, unchanged: shared.filter((id) => !changed.includes(id)) };
}

export async function saveProfile(ownerId: string, payload: Partial<EvalProfile>, profileId?: string, actorId?: string): Promise<EvalProfile> {
  if (!String(payload.name ?? "").trim()) throw new Error("Profile 名称不能为空");
  if (!String(payload.agentId ?? "").trim()) throw new Error("Profile 必须绑定 Agent");
  if (String(payload.name).length > 120) throw new Error("Profile 名称不能超过 120 字符");
  for (const [metric, weight] of Object.entries(payload.metrics ?? {})) {
    if (!metric.trim() || !Number.isFinite(Number(weight)) || Number(weight) < 0) throw new Error("Profile 指标权重必须是非负数字");
  }
  return mutate(ownerId, (state) => {
    const existing = profileId ? state.profiles.find((item) => item.id === profileId) : undefined;
    const timestamp = now();
    if (existing) {
      const before = clone(existing);
      Object.assign(existing, payload, { id: existing.id, version: existing.version + 1, createdAt: existing.createdAt, updatedAt: timestamp });
      audit(state, "profile.updated", actorId, "profile", existing.id, before, existing);
      return clone(existing);
    }
    const item: EvalProfile = { id: `profile_${randomUUID()}`, agentId: String(payload.agentId ?? ""), name: String(payload.name ?? "默认 Profile"), version: 1, objectives: payload.objectives ?? [], metrics: payload.metrics ?? {}, redLines: payload.redLines ?? [], requiredTags: payload.requiredTags ?? [], reviewRules: payload.reviewRules ?? [], createdAt: timestamp, updatedAt: timestamp };
    state.profiles.unshift(item);
    audit(state, "profile.created", actorId, "profile", item.id, undefined, item);
    return clone(item);
  });
}

export async function updateGate(ownerId: string, patch: Partial<ReleaseGate>, actorId?: string): Promise<ReleaseGate> {
  for (const [name, value] of [["minPassRate", patch.minPassRate], ["minWeightedPassRate", patch.minWeightedPassRate], ["maxRegressions", patch.maxRegressions], ["minTrialsPerCase", patch.minTrialsPerCase], ["minConfidenceLowerBound", patch.minConfidenceLowerBound]] as const) {
    if (value !== undefined && !Number.isFinite(Number(value))) throw new Error(`${name} 必须是有效数字`);
  }
  return mutate(ownerId, (state) => {
    const before = clone(state.gate);
    state.gate = {
      ...state.gate,
      ...patch,
      minPassRate: Math.min(1, Math.max(0, Number(patch.minPassRate ?? state.gate.minPassRate))),
      minWeightedPassRate: Math.min(1, Math.max(0, Number(patch.minWeightedPassRate ?? state.gate.minWeightedPassRate))),
      maxRegressions: Math.max(0, Math.floor(Number(patch.maxRegressions ?? state.gate.maxRegressions))),
      minTrialsPerCase: Math.min(runtimeConfig.evalMaxTrialsPerCase, Math.max(1, Math.floor(Number(patch.minTrialsPerCase ?? state.gate.minTrialsPerCase)))),
      minConfidenceLowerBound: Math.min(1, Math.max(0, Number(patch.minConfidenceLowerBound ?? state.gate.minConfidenceLowerBound))),
    };
    audit(state, "gate.updated", actorId, "gate", "release_gate", before, state.gate);
    return clone(state.gate);
  });
}

export type RunEvalOptions = {
  caseIds?: string[];
  datasetId?: string;
  profileId?: string;
  targetSnapshot?: Record<string, unknown>;
  evidenceByCase?: Record<string, unknown | unknown[]>;
  trialCount?: number;
  name?: string;
  runner?: boolean;
  agentId?: string;
  jobId?: string;
};

export async function runEvaluation(ownerId: string, options: RunEvalOptions = {}): Promise<EvalRun> {
  return mutate(ownerId, (state) => {
    const existing = options.jobId ? state.runs.find((item) => item.jobId === options.jobId) : undefined;
    if (existing) return clone(existing);
    const started = Date.now();
    const selected = state.cases.filter((item) => item.enabled && (!options.datasetId || item.datasetId === options.datasetId) && (!options.caseIds?.length || options.caseIds.includes(item.id)));
    if (!selected.length) throw new Error("没有可运行的评测用例");
    if (selected.length > runtimeConfig.evalMaxCasesPerRun) {
      throw new Error(`单次评测最多运行 ${runtimeConfig.evalMaxCasesPerRun} 条用例`);
    }
    const requestedTrialsPerCase = Number(options.trialCount ?? 1);
    if (!Number.isInteger(requestedTrialsPerCase) || requestedTrialsPerCase <= 0 || requestedTrialsPerCase > runtimeConfig.evalMaxTrialsPerCase) {
      throw new Error(`trialCount 必须是 1–${runtimeConfig.evalMaxTrialsPerCase} 的整数`);
    }
    if (options.evidenceByCase && Buffer.byteLength(JSON.stringify(options.evidenceByCase), "utf8") > runtimeConfig.evalMaxEvidenceBytes) {
      throw new Error(`评测证据快照不能超过 ${runtimeConfig.evalMaxEvidenceBytes} 字节`);
    }
    const datasetForRun = options.datasetId ? state.datasets.find((item) => item.id === options.datasetId) : undefined;
    const datasetVersion = options.datasetId ? snapshotDataset(state, options.datasetId) : undefined;
    const trials: EvalRun["trials"] = [];
    const caseStatistics: EvalRun["statistics"]["cases"] = [];
    const results = selected.map((item) => {
      const suppliedEvidence = options.evidenceByCase?.[item.id];
      const trialEvidence = Array.isArray(suppliedEvidence)
        ? suppliedEvidence.slice(0, requestedTrialsPerCase)
        : suppliedEvidence === undefined
          ? Array.from({ length: requestedTrialsPerCase }, () => undefined)
          : [suppliedEvidence];
      const caseTrials = (trialEvidence.length ? trialEvidence : [undefined]).map((evidence, index) => {
        const evaluated = evaluateCase(evidence === undefined ? item : { ...item, evidence });
        trials.push({ caseId: item.id, trial: index + 1, result: evaluated });
        return evaluated;
      });
      const passedTrials = caseTrials.filter((trial) => trial.passed).length;
      const passAtK = passedTrials > 0;
      const passPowerK = passedTrials === caseTrials.length;
      caseStatistics.push({
        caseId: item.id,
        trials: caseTrials.length,
        passedTrials,
        successRate: passedTrials / caseTrials.length,
        passAtK,
        passPowerK,
        confidenceInterval: wilsonInterval(passedTrials, caseTrials.length),
      });
      const first = caseTrials[0];
      return {
        ...first,
        passed: passPowerK,
        durationMs: caseTrials.reduce((sum, trial) => sum + trial.durationMs, 0),
        assertions: caseTrials.length === 1 ? first.assertions : [
          ...first.assertions,
          { name: `稳定通过 pass^${caseTrials.length}`, passed: passPowerK, expected: caseTrials.length, actual: passedTrials },
        ],
      };
    });
    const profile = state.profiles.find((item) => item.id === options.profileId);
    const passed = results.filter((item) => item.passed).length;
    const effectiveWeight = (item: EvalCase) => item.weight * Number(profile?.metrics[item.category] ?? 1);
    const totalWeight = selected.reduce((sum, item) => sum + effectiveWeight(item), 0);
    const passedWeight = selected.reduce((sum, item, index) => sum + (results[index].passed ? effectiveWeight(item) : 0), 0);
    const categories = [...new Set(selected.map((item) => item.category))].map((category) => {
      const categoryResults = results.filter((item) => item.category === category);
      const count = categoryResults.filter((item) => item.passed).length;
      return { category, total: categoryResults.length, passed: count, passRate: count / categoryResults.length };
    });
    const baselineScope = [
      options.datasetId ?? "selection",
      options.profileId ?? "no-profile",
      String(options.targetSnapshot?.agentId ?? "any-agent"),
    ].join(":");
    const scopedBaselineId = state.baselineRunIds?.[baselineScope];
    const baseline = state.runs.find((item) => item.id === scopedBaselineId);
    const previous = new Map(baseline?.results.map((item) => [item.id, item.passed]) ?? []);
    const regressions = results.filter((item) => previous.get(item.id) === true && !item.passed).map((item) => item.id);
    const improvements = results.filter((item) => previous.get(item.id) === false && item.passed).map((item) => item.id);
    const passRate = passed / results.length;
    const weightedPassRate = totalWeight ? passedWeight / totalWeight : 0;
    const passedTrials = trials.filter((item) => item.result.passed).length;
    const confidenceInterval = wilsonInterval(passedTrials, trials.length);
    const critical = selected.filter((item, index) => item.severity === "critical" && !results[index].passed).map((item) => item.id);
    const missingTags = (profile?.requiredTags ?? []).filter((tag) => selected.some((item, index) => item.tags.includes(tag) && !results[index].passed));
    const redLineFailures = selected.filter((item, index) =>
      !results[index].passed && item.tags.some((tag) => (profile?.redLines ?? []).includes(tag))
    ).map((item) => item.id);
    const reasons: string[] = [];
    const inconclusiveReasons: string[] = [];
    if (passRate < state.gate.minPassRate) reasons.push(`通过率 ${(passRate * 100).toFixed(1)}% 低于门禁 ${(state.gate.minPassRate * 100).toFixed(1)}%`);
    if (weightedPassRate < state.gate.minWeightedPassRate) reasons.push("加权通过率未达标");
    if (regressions.length > state.gate.maxRegressions) reasons.push(`回归数 ${regressions.length} 超过上限 ${state.gate.maxRegressions}`);
    if (state.gate.blockCritical && critical.length) reasons.push(`存在 ${critical.length} 个 critical 失败`);
    if (missingTags.length) reasons.push(`必过标签失败：${missingTags.join("、")}`);
    if (redLineFailures.length) reasons.push(`Profile 红线失败：${redLineFailures.length} 项`);
    if (state.gate.requirePublishedDataset && options.datasetId && datasetForRun?.status !== "published") {
      inconclusiveReasons.push(`数据集尚未发布，当前状态为 ${datasetForRun?.status ?? "unknown"}`);
    }
    const underSampled = caseStatistics.filter((item) => item.trials < state.gate.minTrialsPerCase).map((item) => item.caseId);
    if (underSampled.length) inconclusiveReasons.push(`${underSampled.length} 条用例的 Trial 数低于门禁 ${state.gate.minTrialsPerCase}`);
    if (state.gate.minConfidenceLowerBound > 0 && (confidenceInterval?.lower ?? 0) < state.gate.minConfidenceLowerBound) {
      reasons.push(`95% 置信区间下界 ${((confidenceInterval?.lower ?? 0) * 100).toFixed(1)}% 低于门禁 ${(state.gate.minConfidenceLowerBound * 100).toFixed(1)}%`);
    }
    const timestamp = now();
    const failedIds = results.filter((item) => !item.passed).map((item) => item.id);
    const reviewCaseIds = selected.filter((item, index) =>
      !results[index].passed && (!(profile?.reviewRules.length) || item.tags.some((tag) => profile.reviewRules.includes(tag)))
    ).map((item) => item.id);
    const decision: EvalRun["gate"]["decision"] = reasons.length || (failedIds.length > 0 && reviewCaseIds.length === 0)
      ? "blocked"
      : inconclusiveReasons.length
        ? "inconclusive"
        : reviewCaseIds.length
          ? "review_required"
          : "approved";
    const runId = `evalrun_${randomUUID()}`;
    for (const caseId of reviewCaseIds) {
      state.reviews.unshift({ id: `review_${randomUUID()}`, runId, caseId, status: "pending", createdAt: timestamp, updatedAt: timestamp });
    }
    const gateReasons = [...reasons, ...inconclusiveReasons];
    const run: EvalRun = {
      id: runId, jobId: options.jobId,
      name: options.name ?? `评测运行 ${new Date().toLocaleString("zh-CN")}`,
      startedAt: new Date(started).toISOString(), finishedAt: timestamp, durationMs: Date.now() - started,
      datasetId: options.datasetId, datasetVersion: datasetVersion?.version,
      datasetHash: datasetVersion?.hash ?? hash(selected), profileSnapshot: profile ? clone(profile) : undefined,
      targetSnapshot: clone(options.targetSnapshot ?? {}), caseSnapshot: clone(selected),
      evidenceSnapshot: options.evidenceByCase ? clone(options.evidenceByCase) : undefined,
      results,
      trials,
      statistics: {
        requestedTrialsPerCase,
        totalTrials: trials.length,
        passedTrials,
        trialPassRate: trials.length ? passedTrials / trials.length : 0,
        confidenceInterval,
        cases: caseStatistics,
      },
      summary: { total: results.length, passed, failed: results.length - passed, passRate, weightedPassRate }, categories,
      comparison: baseline ? { baselineRunId: baseline.id, passRateDelta: passRate - baseline.summary.passRate, regressions, improvements } : undefined,
      gate: { passed: decision === "approved", decision, reasons: gateReasons },
      releaseDecision: { passed: decision === "approved", decision, reasons: gateReasons, updatedAt: timestamp },
      reportSnapshot: { failedCaseIds: failedIds, regressions, improvements, checklist: [decision === "approved" ? "发布门禁通过" : `发布门禁结论：${decision}`, critical.length ? "存在 critical 失败" : "无 critical 失败", failedIds.length ? "检查失败或待复核用例" : "无稳定性失败"] },
    };
    state.runs.unshift(run);
    return clone(run);
  });
}

export async function setBaseline(ownerId: string, runId: string, actorId?: string): Promise<boolean> {
  return mutate(ownerId, (state) => {
    const run = state.runs.find((item) => item.id === runId);
    if (!run || (run.releaseDecision ?? run.gate).decision !== "approved") return false;
    const scope = [
      run.datasetId ?? "selection",
      run.profileSnapshot?.id ?? "no-profile",
      String(run.targetSnapshot.agentId ?? "any-agent"),
    ].join(":");
    state.baselineRunIds ??= {};
    const before = state.baselineRunIds[scope];
    state.baselineRunIds[scope] = runId;
    state.baselineRunId = runId;
    audit(state, "baseline.selected", actorId, "baseline", scope, before ? { runId: before } : undefined, { runId }, { runId, scope });
    return true;
  });
}

export async function saveReview(ownerId: string, reviewId: string, status: EvalReview["status"], note?: string, decidedBy?: string): Promise<EvalReview | null> {
  if (!["pending", "approved", "rejected", "false_positive", "resolved"].includes(status)) {
    throw new Error("人工复核状态无效");
  }
  if (note && note.length > 4_000) throw new Error("复核说明不能超过 4000 字符");
  return mutate(ownerId, (state) => {
    const review = state.reviews.find((item) => item.id === reviewId);
    if (!review) return null;
    const before = clone(review);
    review.status = status; review.note = note; review.decidedBy = decidedBy; review.updatedAt = now();
    audit(state, "review.decided", decidedBy, "review", review.id, before, review, { runId: review.runId, caseId: review.caseId, status });
    const run = state.runs.find((item) => item.id === review.runId);
    if (run) {
      const reviews = state.reviews.filter((item) => item.runId === run.id);
      if (reviews.some((item) => item.status === "rejected")) {
        run.releaseDecision = { passed: false, decision: "blocked", reasons: [...new Set([...run.gate.reasons, "人工复核拒绝发布"])], updatedAt: now() };
      } else if (
        run.gate.reasons.length === 0 &&
        reviews.length &&
        reviews.every((item) => ["approved", "false_positive", "resolved"].includes(item.status)) &&
        (run.releaseDecision?.decision ?? run.gate.decision) === "review_required"
      ) {
        run.releaseDecision = { passed: true, decision: "approved", reasons: [], updatedAt: now() };
      }
    }
    return clone(review);
  });
}

export async function createJob(ownerId: string, name = "评测任务", payload: RunEvalOptions = {}): Promise<EvalJob> {
  if (!name.trim() || name.length > 120) throw new Error("任务名称长度须为 1–120 字符");
  return mutate(ownerId, (state) => {
    const pending = state.jobs.filter((job) => job.status === "queued" || job.status === "running").length;
    if (pending >= 100) throw new Error("待执行评测任务过多，请稍后再试");
    const timestamp = now();
    const job: EvalJob = {
      id: `job_${randomUUID()}`, name, status: "queued", payload: clone(payload), attempts: 0,
      progress: { completed: 0, total: 0, percent: 0, phase: "queued", updatedAt: timestamp },
      createdAt: timestamp, updatedAt: timestamp,
    };
    state.jobs.unshift(job); return clone(job);
  });
}

export async function claimEvalJob(ownerId: string, leaseMs = 60_000): Promise<EvalJob | null> {
  return mutate(ownerId, (state) => {
    const currentTime = Date.now();
    for (const item of state.jobs) {
      if (item.cancelRequested && (item.status === "queued" || (item.status === "running" && new Date(item.leaseExpiresAt ?? 0).getTime() <= currentTime))) {
        item.status = "canceled";
        item.leaseId = undefined;
        item.leaseExpiresAt = undefined;
        item.updatedAt = now();
      }
    }
    const job = state.jobs.find((item) =>
      !item.cancelRequested && (item.status === "queued" ||
      (item.status === "running" && new Date(item.leaseExpiresAt ?? 0).getTime() <= currentTime))
    );
    if (!job) return null;
    job.status = "running";
    job.attempts = Number(job.attempts ?? 0) + 1;
    job.leaseId = `joblease_${randomUUID()}`;
    job.leaseExpiresAt = new Date(currentTime + leaseMs).toISOString();
    job.updatedAt = now();
    job.progress = { ...(job.progress ?? { completed: 0, total: 0, percent: 0 }), phase: "running", updatedAt: job.updatedAt };
    return clone(job);
  });
}

export async function finishEvalJob(
  ownerId: string,
  jobId: string,
  leaseId: string,
  patch: Pick<EvalJob, "status" | "runId" | "error">,
): Promise<EvalJob | null> {
  return mutate(ownerId, (state) => {
    const job = state.jobs.find((item) => item.id === jobId);
    if (!job || job.leaseId !== leaseId) return null;
    job.status = job.cancelRequested ? "canceled" : patch.status;
    job.runId = job.cancelRequested ? undefined : patch.runId;
    job.error = job.cancelRequested ? undefined : patch.error;
    job.leaseId = undefined;
    job.leaseExpiresAt = undefined;
    job.updatedAt = now();
    const completed = job.progress?.completed ?? 0;
    const total = job.progress?.total ?? completed;
    job.progress = {
      completed: job.status === "completed" ? total : completed,
      total,
      percent: job.status === "completed" ? 100 : (job.progress?.percent ?? 0),
      phase: job.status,
      updatedAt: job.updatedAt,
    };
    return clone(job);
  });
}

export async function renewEvalJobLease(
  ownerId: string,
  jobId: string,
  leaseId: string,
  leaseMs = 90_000,
): Promise<boolean> {
  return mutate(ownerId, (state) => {
    const job = state.jobs.find((item) => item.id === jobId);
    if (!job || job.status !== "running" || job.leaseId !== leaseId || job.cancelRequested) return false;
    job.leaseExpiresAt = new Date(Date.now() + leaseMs).toISOString();
    job.updatedAt = now();
    return true;
  });
}

export async function updateEvalJobProgress(
  ownerId: string,
  jobId: string,
  leaseId: string,
  progress: { completed: number; total: number; phase: string; currentCaseId?: string },
): Promise<boolean> {
  return mutate(ownerId, (state) => {
    const job = state.jobs.find((item) => item.id === jobId);
    if (!job || job.status !== "running" || job.leaseId !== leaseId) return false;
    const total = Math.max(0, Math.floor(progress.total));
    const completed = Math.max(0, Math.min(total, Math.floor(progress.completed)));
    const updatedAt = now();
    job.progress = {
      completed,
      total,
      percent: total ? Math.round(completed / total * 100) : 0,
      phase: progress.phase.slice(0, 80),
      currentCaseId: progress.currentCaseId,
      updatedAt,
    };
    job.updatedAt = updatedAt;
    return true;
  });
}

export async function requestJobCancellation(ownerId: string, jobId: string): Promise<EvalJob | null> {
  return mutate(ownerId, (state) => {
    const job = state.jobs.find((item) => item.id === jobId);
    if (!job) return null;
    if (["completed", "failed", "canceled"].includes(job.status)) return clone(job);
    job.cancelRequested = true;
    if (job.status === "queued") job.status = "canceled";
    job.updatedAt = now();
    if (job.status === "canceled") {
      job.progress = { ...(job.progress ?? { completed: 0, total: 0, percent: 0 }), phase: "canceled", updatedAt: job.updatedAt };
    }
    return clone(job);
  });
}

export async function updateJob(ownerId: string, jobId: string, patch: Partial<EvalJob>): Promise<EvalJob | null> {
  return mutate(ownerId, (state) => {
    const job = state.jobs.find((item) => item.id === jobId);
    if (!job) return null;
    Object.assign(job, patch, { id: job.id, createdAt: job.createdAt, updatedAt: now() });
    return clone(job);
  });
}

export async function getJob(ownerId: string, jobId: string): Promise<EvalJob | null> {
  const state = await readEvalState(ownerId);
  return clone(state.jobs.find((item) => item.id === jobId) ?? null);
}

export async function acquireRunnerLease(ownerId: string, ttlMs = 60 * 60 * 1000): Promise<string | null> {
  return mutate(ownerId, (state) => {
    if (state.runnerLease && new Date(state.runnerLease.expiresAt).getTime() > Date.now()) return null;
    const id = `lease_${randomUUID()}`;
    state.runnerLease = { id, expiresAt: new Date(Date.now() + ttlMs).toISOString() };
    return id;
  });
}

export async function releaseRunnerLease(ownerId: string, leaseId: string): Promise<void> {
  await mutate(ownerId, (state) => {
    if (state.runnerLease?.id === leaseId) state.runnerLease = undefined;
  });
}

export function renderRunReport(run: EvalRun): string {
  const effective = run.releaseDecision ?? run.gate;
  const statistics = run.statistics;
  const interval = statistics?.confidenceInterval;
  const lines = [
    `# ${run.name}`,
    "",
    `- 开始时间：${run.startedAt}`,
    `- 数据集版本：${run.datasetVersion ?? "临时选择"}`,
    `- 稳定通过率（pass^k）：${(run.summary.passRate * 100).toFixed(1)}%`,
    `- 加权通过率：${(run.summary.weightedPassRate * 100).toFixed(1)}%`,
    ...(statistics ? [`- Trial：${statistics.passedTrials}/${statistics.totalTrials} 通过`, `- Trial 通过率：${(statistics.trialPassRate * 100).toFixed(1)}%`] : []),
    ...(interval ? [`- 95% Wilson 置信区间：${(interval.lower * 100).toFixed(1)}%–${(interval.upper * 100).toFixed(1)}%`] : []),
    `- 原始门禁决策：${run.gate.decision}`,
    `- 当前发布决策：${effective.decision}`,
    "",
    "## 结果",
    "",
  ];
  const names = new Map(run.caseSnapshot.map((item) => [item.id, item.name]));
  for (const item of run.results) lines.push(`- ${item.passed ? "✅" : "❌"} ${names.get(item.id) ?? item.id}`);
  if (run.gate.reasons.length) lines.push("", "## 门禁原因", "", ...run.gate.reasons.map((reason) => `- ${reason}`));
  return `${lines.join("\n")}\n`;
}
