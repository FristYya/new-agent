import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { UserRegistry, UserRegistryError } from "../src/auth-users.ts";

test("注册用户使用 scrypt 哈希持久化，并能在重启后登录", async () => {
  const temporaryDirectory = await mkdtemp(
    path.join(tmpdir(), "new-agent-lesson-04-auth-"),
  );
  const userFile = path.join(temporaryDirectory, "auth-users.json");
  const previousStorage = process.env.NEW_AGENT_STORAGE;

  try {
    // 单元测试显式使用 file，避免读写课堂真实 MySQL。
    process.env.NEW_AGENT_STORAGE = "file";
    const registry = await UserRegistry.create(userFile);
    const user = await registry.register(
      "course_user",
      "course-password-123",
      "课程用户",
    );

    assert.match(user.id, /^user_/);
    assert.equal(user.username, "course_user");
    assert.equal(user.displayName, "课程用户");

    // 磁盘中只能出现哈希格式，绝不能出现注册时输入的明文密码。
    const persistedText = await readFile(userFile, "utf-8");
    assert.match(persistedText, /scrypt\$/);
    assert.doesNotMatch(persistedText, /course-password-123/);

    // 创建新 Registry 模拟服务重启，再验证正确和错误密码。
    const restored = await UserRegistry.create(userFile);
    const credential = restored.findByUsername("course_user");
    assert.ok(credential);
    assert.equal(
      await restored.verifyPassword(credential, "course-password-123"),
      true,
    );
    assert.equal(
      await restored.verifyPassword(credential, "wrong-password"),
      false,
    );
  } finally {
    if (previousStorage === undefined) delete process.env.NEW_AGENT_STORAGE;
    else process.env.NEW_AGENT_STORAGE = previousStorage;
    await rm(temporaryDirectory, { recursive: true, force: true });
  }
});

test("注册校验会拒绝弱密码和重复用户名", async () => {
  const temporaryDirectory = await mkdtemp(
    path.join(tmpdir(), "new-agent-lesson-04-auth-validation-"),
  );
  const userFile = path.join(temporaryDirectory, "auth-users.json");
  const previousStorage = process.env.NEW_AGENT_STORAGE;

  try {
    process.env.NEW_AGENT_STORAGE = "file";
    const registry = await UserRegistry.create(userFile);

    await assert.rejects(
      registry.register("course_user", "short", "课程用户"),
      (error: unknown) =>
        error instanceof UserRegistryError && error.statusCode === 400,
    );

    await registry.register(
      "course_user",
      "course-password-123",
      "课程用户",
    );
    await assert.rejects(
      registry.register(
        "course_user",
        "another-password-123",
        "另一个用户",
      ),
      (error: unknown) =>
        error instanceof UserRegistryError && error.statusCode === 409,
    );
  } finally {
    if (previousStorage === undefined) delete process.env.NEW_AGENT_STORAGE;
    else process.env.NEW_AGENT_STORAGE = previousStorage;
    await rm(temporaryDirectory, { recursive: true, force: true });
  }
});
