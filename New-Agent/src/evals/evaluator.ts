import { evaluateToolCall } from "../policy/tool-policy.ts";

export type EvalCategory = "policy" | "structured_output" | "assertion" | "trajectory" | "agent_task" | "semantic_judge";
export type Severity = "low" | "medium" | "high" | "critical";

export type AssertionSpec = {
  name: string;
  operator: "equals" | "contains" | "not_contains" | "matches" | "exists" | "gt" | "gte" | "lt" | "lte";
  path?: string;
  expected?: unknown;
};

export type EvalCase = {
  id: string;
  name: string;
  description: string;
  category: EvalCategory;
  tags: string[];
  datasetId: string;
  severity: Severity;
  weight: number;
  owner: string;
  enabled: boolean;
  createdAt: string;
  updatedAt: string;
  [key: string]: unknown;
};

export type AssertionResult = {
  name: string;
  passed: boolean;
  expected: unknown;
  actual: unknown;
  message?: string;
};

export type EvalResult = {
  id: string;
  category: EvalCategory;
  passed: boolean;
  assertions: AssertionResult[];
  durationMs: number;
};

function readPath(value: unknown, path?: string): unknown {
  if (!path) return value;
  return path.split(".").filter(Boolean).reduce<unknown>((current, part) => {
    if (current === null || typeof current !== "object") return undefined;
    return (current as Record<string, unknown>)[part];
  }, value);
}

function contains(actual: unknown, expected: unknown): boolean {
  if (typeof actual === "string") return actual.includes(String(expected ?? ""));
  if (Array.isArray(actual)) return actual.some((item) => Object.is(item, expected));
  return JSON.stringify(actual).includes(JSON.stringify(expected));
}

function safeRegularExpression(pattern: string): RegExp {
  if (pattern.length > 256) throw new Error("正则表达式超过 256 字符");
  // 拒绝最常见的嵌套量词灾难回溯形式，例如 (a+)+、(.*)*。
  if (/\([^)]*[+*][^)]*\)[+*{]/.test(pattern)) throw new Error("正则表达式包含高风险嵌套量词");
  return new RegExp(pattern);
}

export function evaluateAssertion(actualRoot: unknown, spec: AssertionSpec): AssertionResult {
  const actual = readPath(actualRoot, spec.path);
  let passed = false;
  try {
    switch (spec.operator) {
      case "equals": passed = JSON.stringify(actual) === JSON.stringify(spec.expected); break;
      case "contains": passed = contains(actual, spec.expected); break;
      case "not_contains": passed = !contains(actual, spec.expected); break;
      case "matches": passed = safeRegularExpression(String(spec.expected ?? "")).test(String(actual ?? "")); break;
      case "exists": passed = spec.expected === false ? actual === undefined || actual === null : actual !== undefined && actual !== null; break;
      case "gt": passed = Number(actual) > Number(spec.expected); break;
      case "gte": passed = Number(actual) >= Number(spec.expected); break;
      case "lt": passed = Number(actual) < Number(spec.expected); break;
      case "lte": passed = Number(actual) <= Number(spec.expected); break;
    }
  } catch {
    passed = false;
  }
  return { name: spec.name, passed, expected: spec.expected, actual };
}

function result(item: EvalCase, started: number, assertions: AssertionResult[]): EvalResult {
  return { id: item.id, category: item.category, passed: assertions.every((entry) => entry.passed), assertions, durationMs: Date.now() - started };
}

export function validateEvalCase(input: Partial<EvalCase>): string[] {
  const errors: string[] = [];
  if (!String(input.name ?? "").trim()) errors.push("用例名称不能为空");
  if (!["policy", "structured_output", "assertion", "trajectory", "agent_task", "semantic_judge"].includes(String(input.category))) errors.push("评测类型无效");
  if (!Number.isFinite(Number(input.weight)) || Number(input.weight) <= 0) errors.push("权重必须大于 0");
  if (input.category === "policy" && !String(input.toolName ?? "").trim()) errors.push("policy 用例必须提供 toolName");
  if (input.category === "agent_task" && !String(input.prompt ?? "").trim()) errors.push("agent_task 用例必须提供 prompt");
  if (input.category === "semantic_judge" && !String(input.rubric ?? "").trim()) errors.push("semantic_judge 用例必须提供 rubric");
  if (String(input.prompt ?? "").length > 20_000) errors.push("agent_task prompt 不能超过 20000 字符");
  if (String(input.name ?? "").length > 120) errors.push("用例名称不能超过 120 字符");
  if (String(input.description ?? "").length > 4_000) errors.push("用例说明不能超过 4000 字符");
  if (!String(input.datasetId ?? "").trim()) errors.push("用例必须绑定数据集");
  if (!['low', 'medium', 'high', 'critical'].includes(String(input.severity ?? "medium"))) errors.push("严重级别无效");
  if (input.tags !== undefined && (!Array.isArray(input.tags) || input.tags.length > 50 || input.tags.some((tag) => typeof tag !== "string" || tag.length > 64))) errors.push("标签必须是不超过 50 项、单项不超过 64 字符的字符串数组");
  if (input.category === "assertion" && !Array.isArray(input.assertions)) errors.push("assertion 用例必须提供 assertions 数组");
  if (input.category === "trajectory" && !Array.isArray(input.actualTools)) errors.push("trajectory 用例必须提供 actualTools 数组");
  if (input.category === "assertion" && Array.isArray(input.assertions)) {
    const operators = new Set(["equals", "contains", "not_contains", "matches", "exists", "gt", "gte", "lt", "lte"]);
    if (!input.assertions.length || input.assertions.length > 100) errors.push("assertions 数量须为 1–100");
    for (const assertion of input.assertions as AssertionSpec[]) {
      if (!assertion || typeof assertion !== "object" || !String(assertion.name ?? "").trim() || !operators.has(assertion.operator)) {
        errors.push("assertion 结构或 operator 无效");
        break;
      }
      if (assertion.operator === "matches") {
        try { safeRegularExpression(String(assertion.expected ?? "")); }
        catch (error) { errors.push(error instanceof Error ? error.message : "正则表达式无效"); }
      }
    }
  }
  if (input.category === "trajectory" && Array.isArray(input.actualTools) && input.actualTools.length > 1_000) errors.push("Tool 轨迹不能超过 1000 项");
  if (input.category === "semantic_judge") {
    const expected = (input.expected ?? {}) as Record<string, unknown>;
    for (const field of ["minScore", "minConfidence"] as const) {
      const value = expected[field];
      if (value !== undefined && (!Number.isFinite(Number(value)) || Number(value) < 0 || Number(value) > 1)) {
        errors.push(`${field} 必须是 0–1 之间的数字`);
      }
    }
  }
  if (Buffer.byteLength(JSON.stringify(input), "utf8") > 64 * 1024) errors.push("单个评测用例不能超过 64KB");
  return errors;
}

export function evaluateCase(item: EvalCase): EvalResult {
  const started = Date.now();
  if (item.category === "policy") {
    const actual = evaluateToolCall(String(item.toolName), item.input, {
      allowedTools: Array.isArray(item.allowedTools) ? item.allowedTools as string[] : [],
      disabledSkills: Array.isArray(item.disabledSkills) ? item.disabledSkills as string[] : [],
    });
    const expected = (item.expected ?? {}) as Record<string, unknown>;
    return result(item, started, [
      { name: "是否放行", passed: actual.allowed === Boolean(expected.allowed), expected: Boolean(expected.allowed), actual: actual.allowed },
      { name: "风险等级", passed: actual.risk === expected.risk, expected: expected.risk, actual: actual.risk },
      { name: "是否审批", passed: Boolean(actual.requiresApproval) === Boolean(expected.requiresApproval), expected: Boolean(expected.requiresApproval), actual: Boolean(actual.requiresApproval) },
    ]);
  }
  if (item.category === "structured_output") {
    let parsed: unknown;
    try { parsed = JSON.parse(String(item.output ?? "")); } catch { parsed = undefined; }
    const expected = (item.expected ?? {}) as Record<string, unknown>;
    const object = parsed && typeof parsed === "object" ? parsed as Record<string, unknown> : {};
    return result(item, started, [
      { name: "输出是合法 JSON", passed: parsed !== undefined, expected: "valid JSON", actual: parsed === undefined ? "invalid JSON" : "valid JSON" },
      { name: "运行状态", passed: String(item.runStatus) === "succeeded", expected: "succeeded", actual: item.runStatus },
      { name: "输出模式", passed: expected.mode === undefined || object.mode === expected.mode, expected: expected.mode, actual: object.mode },
      { name: "结果状态", passed: expected.status === undefined || object.status === expected.status, expected: expected.status, actual: object.status },
    ]);
  }
  if (item.category === "assertion") {
    const specs = Array.isArray(item.assertions) ? item.assertions as AssertionSpec[] : [];
    return result(item, started, specs.map((spec) => evaluateAssertion(item.actual, spec)));
  }
  if (item.category === "trajectory") {
    const actual = Array.isArray(item.actualTools) ? item.actualTools as Array<string | { name?: string }> : [];
    const names = actual.map((entry) => typeof entry === "string" ? entry : String(entry.name ?? ""));
    const expected = (item.expected ?? {}) as { sequence?: string[]; includes?: string[]; excludes?: string[]; maxCalls?: number };
    const assertions: AssertionResult[] = [];
    if (expected.sequence) assertions.push({ name: "Tool 调用顺序", passed: JSON.stringify(names) === JSON.stringify(expected.sequence), expected: expected.sequence, actual: names });
    if (expected.includes) for (const name of expected.includes) assertions.push({ name: `必须调用 ${name}`, passed: names.includes(name), expected: true, actual: names.includes(name) });
    if (expected.excludes) for (const name of expected.excludes) assertions.push({ name: `禁止调用 ${name}`, passed: !names.includes(name), expected: false, actual: names.includes(name) });
    if (expected.maxCalls !== undefined) assertions.push({ name: "最大调用次数", passed: names.length <= expected.maxCalls, expected: expected.maxCalls, actual: names.length });
    return result(item, started, assertions.length ? assertions : [{ name: "轨迹存在", passed: names.length > 0, expected: "> 0", actual: names.length }]);
  }

  if (item.category === "semantic_judge") {
    const evidence = (item.evidence ?? {}) as {
      verdict?: "pass" | "fail" | "review";
      score?: number;
      confidence?: number;
      rubricVersion?: string;
      judgeSnapshot?: { provider?: string; model?: string; promptHash?: string };
      reasons?: string[];
    };
    const expected = (item.expected ?? {}) as { minScore?: number; minConfidence?: number; allowedVerdicts?: string[] };
    const allowedVerdicts = expected.allowedVerdicts?.length ? expected.allowedVerdicts : ["pass"];
    const score = Number(evidence.score);
    const confidence = Number(evidence.confidence);
    return result(item, started, [
      { name: "裁判结论", passed: allowedVerdicts.includes(String(evidence.verdict)), expected: allowedVerdicts, actual: evidence.verdict },
      { name: "语义得分", passed: Number.isFinite(score) && score >= Number(expected.minScore ?? 0.8), expected: `>= ${expected.minScore ?? 0.8}`, actual: evidence.score },
      { name: "裁判置信度", passed: Number.isFinite(confidence) && confidence >= Number(expected.minConfidence ?? 0.7), expected: `>= ${expected.minConfidence ?? 0.7}`, actual: evidence.confidence },
      { name: "Rubric 版本快照", passed: Boolean(String(evidence.rubricVersion ?? "").trim()), expected: "non-empty", actual: evidence.rubricVersion },
      { name: "Judge 模型快照", passed: Boolean(evidence.judgeSnapshot?.provider && evidence.judgeSnapshot?.model && evidence.judgeSnapshot?.promptHash), expected: "provider/model/promptHash", actual: evidence.judgeSnapshot },
    ]);
  }

  const evidence = (item.evidence ?? {}) as { output?: string; status?: string; tools?: string[]; latencyMs?: number };
  const expected = (item.expected ?? {}) as { contains?: string[]; notContains?: string[]; status?: string; requiredTools?: string[]; forbiddenTools?: string[]; maxLatencyMs?: number };
  const assertions: AssertionResult[] = [];
  for (const text of expected.contains ?? []) assertions.push({ name: `输出包含 ${text}`, passed: String(evidence.output ?? "").includes(text), expected: text, actual: evidence.output });
  for (const text of expected.notContains ?? []) assertions.push({ name: `输出不包含 ${text}`, passed: !String(evidence.output ?? "").includes(text), expected: `not ${text}`, actual: evidence.output });
  if (expected.status) assertions.push({ name: "运行状态", passed: evidence.status === expected.status, expected: expected.status, actual: evidence.status });
  for (const tool of expected.requiredTools ?? []) assertions.push({ name: `必须调用 ${tool}`, passed: (evidence.tools ?? []).includes(tool), expected: true, actual: evidence.tools });
  for (const tool of expected.forbiddenTools ?? []) assertions.push({ name: `禁止调用 ${tool}`, passed: !(evidence.tools ?? []).includes(tool), expected: false, actual: evidence.tools });
  if (expected.maxLatencyMs !== undefined) assertions.push({ name: "延迟上限", passed: Number(evidence.latencyMs ?? Infinity) <= expected.maxLatencyMs, expected: expected.maxLatencyMs, actual: evidence.latencyMs });
  return result(item, started, assertions.length ? assertions : [{ name: "Agent 成功执行", passed: evidence.status === "succeeded", expected: "succeeded", actual: evidence.status }]);
}
