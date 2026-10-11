import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { SessionManager } from "@earendil-works/pi-coding-agent";
import { usesMysqlStorage } from "../src/storage/mysql.ts";

test("默认使用 MySQL，只有显式选择 file 才回退文件存储", () => {
  assert.equal(usesMysqlStorage({}), true);
  assert.equal(usesMysqlStorage({ NEW_AGENT_STORAGE: "mysql" }), true);
  assert.equal(usesMysqlStorage({ NEW_AGENT_STORAGE: "MYSQL" }), true);
  assert.equal(usesMysqlStorage({ NEW_AGENT_STORAGE: "file" }), false);
});

test("AppState 会原子写入 JSON，重新读取时不依赖调用方内存", async () => {
  const temporaryDataDirectory = await mkdtemp(
    path.join(tmpdir(), "new-agent-lesson-04-state-"),
  );
  const previousStorage = process.env.NEW_AGENT_STORAGE;
  const previousDataDirectory = process.env.NEW_AGENT_DATA_DIR;

  try {
    process.env.NEW_AGENT_STORAGE = "file";
    process.env.NEW_AGENT_DATA_DIR = temporaryDataDirectory;

    // 查询参数确保 DATA_DIR 在本测试设置环境变量后重新计算。
    const moduleUrl = new URL("../src/store.ts", import.meta.url);
    moduleUrl.searchParams.set("lesson", `04-${Date.now()}`);
    const store = await import(moduleUrl.href) as typeof import("../src/store.ts");

    const first = await store.readState({
      provider: "course-provider",
      id: "course-model",
    });
    const conversation = store.createRecord(first.conversations, "conv", {
      title: "持久化课程",
      agentId: "agent_new_agent",
      piSessionFile: "/tmp/course-session.jsonl",
      messages: [],
      createdAt: new Date().toISOString(),
    });

    await store.writeState(first);
    conversation.title = "不应污染磁盘数据";

    const restored = await store.readState();
    const restoredConversation = restored.conversations.find(
      (item) => item.id === conversation.id,
    );
    assert.equal(restoredConversation?.title, "持久化课程");
    assert.equal(restoredConversation?.piSessionFile, "/tmp/course-session.jsonl");

    const persisted = JSON.parse(
      await readFile(path.join(temporaryDataDirectory, "app-state.json"), "utf-8"),
    );
    assert.equal(persisted.conversations[0].title, "持久化课程");
  } finally {
    if (previousStorage === undefined) delete process.env.NEW_AGENT_STORAGE;
    else process.env.NEW_AGENT_STORAGE = previousStorage;

    if (previousDataDirectory === undefined) delete process.env.NEW_AGENT_DATA_DIR;
    else process.env.NEW_AGENT_DATA_DIR = previousDataDirectory;

    await rm(temporaryDataDirectory, { recursive: true, force: true });
  }
});

test("Pi SessionManager 能从 JSONL 恢复完整会话树", async () => {
  const temporaryProject = await mkdtemp(
    path.join(tmpdir(), "new-agent-lesson-04-session-"),
  );
  const sessionDirectory = path.join(temporaryProject, "pi-sessions");

  try {
    const manager = SessionManager.create(temporaryProject, sessionDirectory);
    const rootId = manager.appendMessage({
      role: "user",
      content: [{ type: "text", text: "记住口令：blue" }],
      timestamp: Date.now(),
    });
    manager.appendMessage({
      role: "user",
      content: [{ type: "text", text: "第一条回复" }],
      timestamp: Date.now(),
    });
    manager.branch(rootId);
    manager.appendMessage({
      role: "user",
      content: [{ type: "text", text: "第二条分支回复" }],
      timestamp: Date.now(),
    });

    const sessionFile = manager.getSessionFile();
    assert.ok(sessionFile, "追加首条消息后应生成 JSONL 文件");
    const restored = SessionManager.open(
      sessionFile,
      sessionDirectory,
      temporaryProject,
    );
    const tree = restored.getTree();

    assert.equal(restored.getEntryCount(), 3);
    assert.equal(tree.length, 1);
    assert.equal(tree[0]?.children.length, 2);
    assert.equal(restored.getBranch().length, 2);
  } finally {
    await rm(temporaryProject, { recursive: true, force: true });
  }
});
