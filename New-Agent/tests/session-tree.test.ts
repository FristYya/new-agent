import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { SessionManager } from "@earendil-works/pi-coding-agent";
import type { ConversationRecord } from "../src/store.ts";
import type { AgentSession } from "../src/runtime/runtime-context.ts";
import {
  buildActiveConversationMessages,
  buildSessionTreeSnapshot,
} from "../src/runtime/session-tree.ts";
import {
  restoreConversationLeaf,
} from "../src/runtime/session-storage.ts";

function userMessage(text: string) {
  return {
    role: "user" as const,
    content: [{ type: "text" as const, text }],
    timestamp: Date.now(),
  };
}

function fakeSession(manager: SessionManager): AgentSession {
  return { sessionManager: manager } as AgentSession;
}

function conversation(): ConversationRecord {
  const now = new Date().toISOString();
  return {
    id: "conv_tree_test",
    title: "会话树测试",
    agentId: "agent_new_agent",
    messages: [],
    createdAt: now,
    updatedAt: now,
  };
}

test("会话树快照保留全部分支，并标记当前活动路径", () => {
  const manager = SessionManager.inMemory(process.cwd());
  const rootId = manager.appendMessage(userMessage("根消息"));
  const firstLeafId = manager.appendMessage(userMessage("分支 A"));
  manager.branch(rootId);
  const secondLeafId = manager.appendMessage(userMessage("分支 B"));

  const snapshot = buildSessionTreeSnapshot(fakeSession(manager));
  assert.equal(snapshot.leafId, secondLeafId);
  assert.equal(snapshot.nodes.length, 1);
  assert.equal(snapshot.nodes[0]?.branchPoint, true);
  assert.equal(snapshot.nodes[0]?.technical, false);
  assert.equal(snapshot.nodes[0]?.children.length, 2);
  assert.equal(snapshot.nodes[0]?.active, true);
  assert.equal(snapshot.nodes[0]?.children[0]?.id, firstLeafId);
  assert.equal(snapshot.nodes[0]?.children[0]?.active, false);
  assert.equal(snapshot.nodes[0]?.children[1]?.active, true);

  const messages = buildActiveConversationMessages(fakeSession(manager));
  assert.deepEqual(messages.map((item) => item.content), ["根消息", "分支 B"]);
});

test("会话树会标记系统状态节点为技术节点，供前端默认隐藏", () => {
  const manager = SessionManager.inMemory(process.cwd());
  manager.appendModelChange("course-provider", "course-model");
  manager.appendMessage(userMessage("用户真正关心的问题"));

  const snapshot = buildSessionTreeSnapshot(fakeSession(manager));
  assert.equal(snapshot.nodes[0]?.type, "model_change");
  assert.equal(snapshot.nodes[0]?.technical, true);
  assert.equal(snapshot.nodes[0]?.children[0]?.role, "user");
  assert.equal(snapshot.nodes[0]?.children[0]?.technical, false);
});

test("piLeafId 可以恢复分支位置，null 可以恢复到树根之前", () => {
  const manager = SessionManager.inMemory(process.cwd());
  const rootId = manager.appendMessage(userMessage("根消息"));
  manager.appendMessage(userMessage("后续消息"));

  const record = conversation();
  record.piLeafId = rootId;
  restoreConversationLeaf(manager, record);
  assert.equal(manager.getLeafId(), rootId);

  record.piLeafId = null;
  restoreConversationLeaf(manager, record);
  assert.equal(manager.getLeafId(), null);
});

test("file Session 重启后使用 piLeafId 恢复用户选择的旧分支", async () => {
  const projectDirectory = await mkdtemp(
    path.join(tmpdir(), "new-agent-tree-restart-"),
  );
  const sessionDirectory = path.join(projectDirectory, "sessions");

  try {
    const manager = SessionManager.create(projectDirectory, sessionDirectory);
    const rootId = manager.appendMessage(userMessage("根消息"));
    const selectedLeafId = manager.appendMessage(userMessage("分支 A"));
    manager.branch(rootId);
    manager.appendMessage(userMessage("分支 B"));

    const sessionFile = manager.getSessionFile();
    assert.ok(sessionFile);

    const restored = SessionManager.open(
      sessionFile,
      sessionDirectory,
      projectDirectory,
    );
    const record = conversation();
    record.piSessionFile = sessionFile;
    record.piLeafId = selectedLeafId;
    restoreConversationLeaf(restored, record);

    assert.equal(restored.getLeafId(), selectedLeafId);
    assert.deepEqual(
      restored.getBranch().map((entry) => entry.id),
      [rootId, selectedLeafId],
    );
  } finally {
    await rm(projectDirectory, { recursive: true, force: true });
  }
});
