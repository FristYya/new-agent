import assert from "node:assert/strict";
import test from "node:test";
import {
  forwardVisitRequest,
  VisitProxyError,
  visitUpstreamUrl,
} from "../src/visit-api-proxy.ts";
import { readFile } from "node:fs/promises";
import path from "node:path";

test("拜访代理保留路径和查询参数", () => {
  assert.equal(
    visitUpstreamUrl("http://127.0.0.1:8080/internal", "/api/visit/conversations/c1?full=true").href,
    "http://127.0.0.1:8080/api/visit/conversations/c1?full=true",
  );
  assert.throws(
    () => visitUpstreamUrl("http://127.0.0.1:8080", "/api/state"),
    (error: unknown) => error instanceof VisitProxyError && error.statusCode === 400,
  );
});

test("拜访代理只注入已登录用户并透传 Spring 响应", async () => {
  let received: RequestInit | undefined;
  const mockFetch: typeof fetch = async (_input, init) => {
    received = init;
    return new Response(JSON.stringify({ message: "ok" }), {
      status: 201,
      headers: { "content-type": "application/json" },
    });
  };
  const result = await forwardVisitRequest({
    baseUrl: "http://127.0.0.1:8080",
    timeoutMs: 1_000,
    userId: "authenticated-user",
    method: "POST",
    originalUrl: "/api/visit/create/start",
    body: { message: "创建计划" },
  }, mockFetch);

  assert.equal(result.status, 201);
  assert.equal(new Headers(received?.headers).get("x-user-id"), "authenticated-user");
  assert.equal(received?.body, JSON.stringify({ message: "创建计划" }));
  assert.deepEqual(JSON.parse(Buffer.from(result.body).toString("utf8")), { message: "ok" });
});

test("创建计划客户下拉框使用后端实时向量检索", async () => {
  const source = await readFile(path.join(process.cwd(), "public", "visit-assistant.html"), "utf8");

  assert.match(source, /\/api\/visit\/customers\/search\?\$\{params\}/);
  assert.match(source, /scheduleCustomerSearch\(field\.value\.trim\(\), card\)/);
  assert.match(source, /id,\s*name,\s*objectType,/);
  assert.match(source, /相似度/);
});
