import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { evaluateCase, validateEvalCase, type EvalCase } from "../src/evals/evaluator.ts";
import { wilsonInterval } from "../src/evals/statistics.ts";

const base = (patch: Partial<EvalCase>): EvalCase => ({
  id: "case-1", name: "case", description: "", category: "assertion",
  tags: [], datasetId: "dataset_default", severity: "medium", weight: 1,
  owner: "test", enabled: true, createdAt: new Date().toISOString(),
  updatedAt: new Date().toISOString(), ...patch,
});

test("policy、assertion、trajectory、agent_task 和 semantic_judge 可以评测", () => {
  assert.equal(evaluateCase(base({
    category: "policy", toolName: "write", input: { path: "demo.txt" },
    allowedTools: ["write"], disabledSkills: [],
    expected: { allowed: false, risk: "write", requiresApproval: true },
  })).passed, true);

  assert.equal(evaluateCase(base({
    actual: { status: "ok", count: 3 },
    assertions: [
      { name: "status", path: "status", operator: "equals", expected: "ok" },
      { name: "count", path: "count", operator: "gte", expected: 2 },
    ],
  })).passed, true);

  assert.equal(evaluateCase(base({
    category: "trajectory", actualTools: [{ name: "read" }, { name: "grep" }],
    expected: { sequence: ["read", "grep"], maxCalls: 2 },
  })).passed, true);

  assert.equal(evaluateCase(base({
    category: "agent_task", prompt: "检查项目", evidence: { output: "检查完成", status: "succeeded", tools: ["read"], latencyMs: 20 },
    expected: { contains: ["完成"], status: "succeeded", requiredTools: ["read"], maxLatencyMs: 100 },
  })).passed, true);

  assert.equal(evaluateCase(base({
    category: "semantic_judge", rubric: "答案必须准确、完整且安全",
    evidence: {
      verdict: "pass", score: 0.92, confidence: 0.88, rubricVersion: "answer-v1",
      judgeSnapshot: { provider: "test", model: "judge-model", promptHash: "sha256:test" },
    },
    expected: { minScore: 0.8, minConfidence: 0.7, allowedVerdicts: ["pass"] },
  })).passed, true);
});

test("Wilson 区间不会越界，并能表达小样本不确定性", () => {
  const interval = wilsonInterval(1, 1);
  assert.ok(interval);
  assert.ok(interval.lower > 0 && interval.lower < 0.5);
  assert.equal(interval.upper, 1);
  assert.equal(wilsonInterval(2, 1), undefined);
});

test("非法评测用例会返回明确校验错误", () => {
  const errors = validateEvalCase({ name: "", category: "agent_task", weight: 0 });
  assert.ok(errors.some((item) => item.includes("名称")));
  assert.ok(errors.some((item) => item.includes("权重")));
  assert.ok(errors.some((item) => item.includes("prompt")));
  const oversized = validateEvalCase(base({ category: "agent_task", prompt: "x".repeat(20_001) }));
  assert.ok(oversized.some((item) => item.includes("20000")));
});

test("评测中心持久化用例、生成版本、识别回归并导出报告", async () => {
  const directory = await mkdtemp(join(tmpdir(), "new-agent-evals-"));
  const previousStorage = process.env.NEW_AGENT_STORAGE;
  const previousDir = process.env.NEW_AGENT_DATA_DIR;
  process.env.NEW_AGENT_STORAGE = "file";
  process.env.NEW_AGENT_DATA_DIR = directory;
  try {
    const center = await import(`../src/evals/eval-center.ts?test=${Date.now()}`);
    const owner = "owner-test";
    const item = await center.createEvalCase(owner, {
      name: "状态正确", description: "", category: "assertion", tags: ["core"],
      datasetId: "dataset_default", severity: "critical", weight: 2,
      owner: "test", enabled: true, actual: { status: "ok" },
      assertions: [{ name: "status", path: "status", operator: "equals", expected: "ok" }],
    }, "actor-test");
    await center.updateGate(owner, { minTrialsPerCase: 3, minPassRate: 0, minWeightedPassRate: 0 });
    const insufficient = await center.runEvaluation(owner, { caseIds: [item.id], trialCount: 1 });
    assert.equal(insufficient.gate.decision, "inconclusive");
    assert.equal(insufficient.statistics.totalTrials, 1);
    assert.equal(await center.setBaseline(owner, insufficient.id), false);
    await assert.rejects(center.runEvaluation(owner, { caseIds: [item.id], trialCount: 0 }), /trialCount/);
    await center.updateGate(owner, { minTrialsPerCase: 1, minPassRate: 1, minWeightedPassRate: 1 });
    await center.updateDatasetLifecycle(owner, "dataset_default", { status: "published" }, "actor-test");
    const first = await center.runEvaluation(owner, { datasetId: "dataset_default" });
    assert.equal(first.summary.passRate, 1);
    await center.setBaseline(owner, first.id);
    await center.updateEvalCase(owner, item.id, { actual: { status: "bad" } });
    const second = await center.runEvaluation(owner, { datasetId: "dataset_default" });
    assert.deepEqual(second.comparison?.regressions, [item.id]);
    assert.equal(second.gate.decision, "blocked");
    assert.match(center.renderRunReport(second), /发布决策：blocked/);
    const dashboard = await center.getEvalDashboard(owner);
    assert.equal(dashboard.summary.totalCases, 1);
    assert.ok(dashboard.datasets[0].version >= 2);
    assert.equal(dashboard.auditIntegrity.valid, true);
    assert.ok(dashboard.auditIntegrity.checked >= 3);
    const createAudit = dashboard.auditLog.find((entry: { action: string }) => entry.action === "case.created");
    assert.equal(createAudit?.actorId, "actor-test");
    const auditPage = await center.getEvalAudit(owner, { limit: 1 });
    assert.equal(auditPage.auditLog.length, 1);
    assert.ok(auditPage.pagination.total >= 3);
    const tamperedAudit = structuredClone(dashboard.auditLog);
    tamperedAudit[0].action = "tampered";
    assert.equal(center.verifyEvalAuditEvents(tamperedAudit).valid, false);

    const agentCase = await center.createEvalCase(owner, {
      name: "真实执行证据隔离", description: "", category: "agent_task", tags: ["reviewable"],
      datasetId: "dataset_default", severity: "medium", weight: 1,
      owner: "test", enabled: true, prompt: "执行检查",
      evidence: { output: "旧证据", status: "failed" },
      expected: { status: "succeeded" },
    });
    const beforeEvidenceRun = await center.readEvalState(owner);
    const datasetVersion = beforeEvidenceRun.datasets[0].version;
    const profile = await center.saveProfile(owner, {
      name: "人工复核", agentId: "agent-1", reviewRules: ["reviewable"],
    });
    const evidenceRun = await center.runEvaluation(owner, {
      caseIds: [agentCase.id], profileId: profile.id,
      evidenceByCase: { [agentCase.id]: { output: "新证据", status: "failed" } },
    });
    const afterEvidenceRun = await center.readEvalState(owner);
    assert.deepEqual(afterEvidenceRun.cases.find((entry: EvalCase) => entry.id === agentCase.id)?.evidence, { output: "旧证据", status: "failed" });
    assert.equal(afterEvidenceRun.datasets[0].version, datasetVersion);
    const review = afterEvidenceRun.reviews.find((entry: { runId: string }) => entry.runId === evidenceRun.id);
    assert.ok(review);
    await center.saveReview(owner, review.id, "approved", "业务确认", "reviewer-1");
    const afterReview = await center.readEvalState(owner);
    assert.equal(afterReview.runs.find((entry: { id: string }) => entry.id === evidenceRun.id)?.releaseDecision?.decision, "blocked");

    await center.updateGate(owner, { minPassRate: 0, minWeightedPassRate: 0, blockCritical: false });
    const repeated = await center.runEvaluation(owner, {
      caseIds: [agentCase.id], trialCount: 2,
      evidenceByCase: {
        [agentCase.id]: [
          { output: "ok", status: "succeeded" },
          { output: "bad", status: "failed" },
        ],
      },
    });
    assert.equal(repeated.statistics.cases[0].passAtK, true);
    assert.equal(repeated.statistics.cases[0].passPowerK, false);
    assert.equal(repeated.results[0].passed, false);
    assert.equal(repeated.statistics.totalTrials, 2);

    const lifecycleDataset = await center.createDataset(owner, { name: "金丝雀集", suite: "canary", owner: "qa" });
    await center.createEvalCase(owner, {
      name: "金丝雀断言", description: "", category: "assertion", tags: [],
      datasetId: lifecycleDataset.id, severity: "medium", weight: 1, owner: "qa", enabled: true,
      actual: true, assertions: [{ name: "truthy", operator: "equals", expected: true }],
    });
    const published = await center.updateDatasetLifecycle(owner, lifecycleDataset.id, { status: "published" }, "qa");
    assert.equal(published?.status, "published");
    assert.ok(published?.publishedVersion);
    const lifecycleCase = (await center.readEvalState(owner)).cases.find((entry: EvalCase) => entry.datasetId === lifecycleDataset.id);
    await center.updateEvalCase(owner, lifecycleCase.id, { description: "changed" });
    const dirtied = (await center.readEvalState(owner)).datasets.find((entry: { id: string }) => entry.id === lifecycleDataset.id);
    assert.equal(dirtied?.status, "draft");

    const canceled = await center.createJob(owner, "取消任务", {});
    await center.requestJobCancellation(owner, canceled.id);
    const queued = await center.createJob(owner, "后续任务", {});
    const claimed = await center.claimEvalJob(owner, 1);
    assert.equal(claimed?.id, queued.id);
    assert.equal(claimed?.attempts, 1);
    await new Promise((resolve) => setTimeout(resolve, 5));
    const reclaimed = await center.claimEvalJob(owner, 1);
    assert.equal(reclaimed?.id, queued.id);
    assert.equal(reclaimed?.attempts, 2);

    const progressJob = await center.createJob(owner, "进度任务", {});
    const progressClaim = await center.claimEvalJob(owner, 10_000);
    assert.equal(progressClaim?.id, progressJob.id);
    assert.equal(await center.updateEvalJobProgress(owner, progressJob.id, progressClaim.leaseId, {
      completed: 2, total: 4, phase: "evaluating", currentCaseId: item.id,
    }), true);
    const progressed = await center.getJob(owner, progressJob.id);
    assert.equal(progressed?.progress?.percent, 50);
    assert.equal(progressed?.progress?.currentCaseId, item.id);
  } finally {
    if (previousStorage === undefined) delete process.env.NEW_AGENT_STORAGE; else process.env.NEW_AGENT_STORAGE = previousStorage;
    if (previousDir === undefined) delete process.env.NEW_AGENT_DATA_DIR; else process.env.NEW_AGENT_DATA_DIR = previousDir;
    await rm(directory, { recursive: true, force: true });
  }
});
