import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import test from "node:test";

function loadProductionConfig(overrides: NodeJS.ProcessEnv) {
  return spawnSync(process.execPath, [
    "--import", "tsx", "--input-type=module", "--eval", "await import('./src/config.ts')",
  ], {
    cwd: process.cwd(),
    encoding: "utf8",
    env: {
      ...process.env,
      NODE_ENV: "production",
      NEW_AGENT_STORAGE: "mysql",
      MYSQL_PASSWORD: "production-database-password",
      AUTH_SECRET: "production-cookie-secret-with-more-than-32-characters",
      AUTH_COOKIE_SECURE: "true",
      ...overrides,
    },
  });
}

test("生产配置拒绝开发凭据和 file 存储", () => {
  const unsafe = loadProductionConfig({
    NEW_AGENT_STORAGE: "file",
    MYSQL_PASSWORD: "",
    AUTH_SECRET: "short",
    AUTH_COOKIE_SECURE: "false",
  });
  assert.notEqual(unsafe.status, 0);
  assert.match(unsafe.stderr, /生产配置不安全/);

  const safe = loadProductionConfig({});
  assert.equal(safe.status, 0, safe.stderr);
});
