import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

test("观测记录会聚合 Run、Trace、Tool 与告警", async () => {
  const directory = await mkdtemp(join(tmpdir(), "new-agent-observe-"));
  const previousStorage = process.env.NEW_AGENT_STORAGE;
  const previousDir = process.env.NEW_AGENT_DATA_DIR;
  process.env.NEW_AGENT_STORAGE = "file";
  process.env.NEW_AGENT_DATA_DIR = directory;
  try {
    const observe = await import(`../src/observability/observability.ts?test=${Date.now()}`);
    const run = await observe.startObservedRun("owner", {
      conversationId: "conversation", agentId: "agent", model: "provider/model",
      prompt: "读取项目", loadedSkills: [], activeTools: ["read"],
    });
    const recorder = observe.createRunRecorder("owner", run);
    recorder.consumePiEvent({ type: "message_update", assistantMessageEvent: { type: "text_delta", delta: "完成" } });
    recorder.consumePiEvent({ type: "tool_execution_start", toolCallId: "tool-1", toolName: "read", args: { path: "README.md" } });
    recorder.consumePiEvent({ type: "tool_execution_end", toolCallId: "tool-1", toolName: "read", result: { content: [{ type: "text", text: "ok" }] }, isError: false });
    await recorder.finish("succeeded");
    const dashboard = await observe.getObservability("owner");
    assert.equal(dashboard.overview.totalRuns, 1);
    assert.equal(dashboard.overview.successRuns, 1);
    assert.equal(dashboard.toolStats[0].name, "read");
    assert.ok(dashboard.events.some((event: { type: string }) => event.type === "tool_end"));
    assert.equal(dashboard.trends.length, 1);
    assert.equal(dashboard.trends[0].runs, 1);

    const failedRun = await observe.startObservedRun("owner", {
      conversationId: "conversation", agentId: "agent", model: "provider/model",
      prompt: "失败任务", loadedSkills: [], activeTools: [],
    });
    await observe.createRunRecorder("owner", failedRun).finish("failed", "模拟失败");
    const failedDashboard = await observe.getObservability("owner");
    assert.equal(failedDashboard.overview.activeAlerts, 1);
    assert.equal(failedDashboard.alerts[0].severity, "critical");
    const firstPage = await observe.getObservability("owner", { limit: 1, offset: 0 });
    const secondPage = await observe.getObservability("owner", { limit: 1, offset: 1 });
    assert.equal(firstPage.pagination.total, 2);
    assert.equal(firstPage.pagination.hasMore, true);
    assert.equal(firstPage.runs.length, 1);
    assert.ok(firstPage.events.every((event: { runId: string }) => event.runId === firstPage.runs[0].id));
    assert.notEqual(firstPage.runs[0].id, secondPage.runs[0].id);
    const failedOnly = await observe.getObservability("owner", { status: "failed" });
    assert.equal(failedOnly.pagination.total, 1);
    assert.equal(failedOnly.runs[0].id, failedRun.id);
    assert.equal(await observe.acknowledgeAlert("owner", failedDashboard.alerts[0].id), true);
    assert.equal((await observe.getObservability("owner")).overview.activeAlerts, 0);

    const interruptedRun = await observe.startObservedRun("owner", {
      conversationId: "conversation", agentId: "agent", model: "provider/model",
      prompt: "中断任务", loadedSkills: [], activeTools: ["read"],
    });
    const interruptedRecorder = observe.createRunRecorder("owner", interruptedRun);
    interruptedRecorder.consumePiEvent({ type: "tool_execution_start", toolCallId: "tool-open", toolName: "read", args: {} });
    interruptedRecorder.consumePiEvent({
      type: "message_end", message: { role: "assistant", usage: { input: -1, output: "not-a-number", totalTokens: Infinity, cost: -2 } },
    });
    await interruptedRecorder.finish("canceled", "用户中断");
    const interruptedDashboard = await observe.getObservability("owner");
    const storedInterruptedRun = interruptedDashboard.runs.find((item: { id: string }) => item.id === interruptedRun.id);
    assert.equal(storedInterruptedRun.modelUsage.totalTokens, 0);
    assert.ok(Number.isFinite(storedInterruptedRun.modelUsage.costUsd));
    assert.ok(interruptedDashboard.events.some((event: { toolCallId?: string; status: string }) => event.toolCallId === "tool-open" && event.status === "aborted"));
    assert.ok(interruptedDashboard.alerts.some((alert: { runId: string; message: string }) => alert.runId === interruptedRun.id && alert.message.includes("仍未完成")));
  } finally {
    if (previousStorage === undefined) delete process.env.NEW_AGENT_STORAGE; else process.env.NEW_AGENT_STORAGE = previousStorage;
    if (previousDir === undefined) delete process.env.NEW_AGENT_DATA_DIR; else process.env.NEW_AGENT_DATA_DIR = previousDir;
    await rm(directory, { recursive: true, force: true });
  }
});
