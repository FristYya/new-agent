import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import test from "node:test";
import { resolveScriptCommand } from "../src/skill-tools.ts";
import {
  createRecord,
  deleteRecord,
  updateRecord,
  type ToolRecord,
} from "../src/store.ts";

const skillDir = path.join(process.cwd(), "skills", "current-time");

test("Skill Tool 命令只能解析到 Skill 目录内部", () => {
  const command = resolveScriptCommand(
    skillDir,
    "node tools/current-time.mjs",
  );

  assert.equal(command.bin, "node");
  assert.equal(
    command.args[0],
    path.join(skillDir, "tools", "current-time.mjs"),
  );

  assert.throws(
    () => resolveScriptCommand(skillDir, "node tools/../../outside.mjs"),
    /脚本路径必须位于 Skill 包目录内/,
  );
});

test("current_time 返回有效 ISO 时间，而不是函数源码", () => {
  const command = resolveScriptCommand(
    skillDir,
    "node tools/current-time.mjs",
  );
  const result = spawnSync(command.bin, command.args, {
    cwd: skillDir,
    input: JSON.stringify({ timezone: "Asia/Shanghai", locale: "zh-CN" }),
    encoding: "utf-8",
  });

  assert.equal(result.status, 0, result.stderr);
  const payload = JSON.parse(result.stdout);
  assert.match(payload.details.iso, /^\d{4}-\d{2}-\d{2}T/);
  assert.match(payload.content[0].text, /ISO：\d{4}-\d{2}-\d{2}T/);
  assert.doesNotMatch(payload.content[0].text, /native code/);
});

test("内存 Store 的创建、修改和删除保持同一调用边界", () => {
  const tools: ToolRecord[] = [];
  const created = createRecord<ToolRecord>(tools, "tool", {
    name: "demo",
    label: "演示 Tool",
    description: "用于测试",
    category: "agent",
    access: "read",
    status: "ENABLED",
  });

  assert.equal(tools.length, 1);
  assert.equal(created.name, "demo");

  const updated = updateRecord(tools, created.id, { status: "DISABLED" });
  assert.equal(updated?.status, "DISABLED");

  assert.equal(deleteRecord(tools, created.id), true);
  assert.equal(tools.length, 0);
});
