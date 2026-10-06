// 读取 Skill 清单、启动子进程和校验路径所需的 Node.js 标准库。
import { readFile } from "fs/promises";
import { spawn } from "child_process";
import { isAbsolute, join, normalize, relative, resolve } from "path";
import { Type } from "@sinclair/typebox";
import { defineTool, type ToolDefinition } from "@earendil-works/pi-coding-agent";
import type { SkillRecord, ToolRecord } from "./store.ts";
import { runtimeConfig } from "./config.ts";

// skill.json 中每个 tools[] 元素的结构；这些字段最终会映射为 Pi Tool。
export type SkillToolManifest = {
  name: string;
  label: string;
  description: string;
  category?: ToolRecord["category"];
  access?: ToolRecord["access"];
  command: string;
  inputSchema?: unknown;
};

// Skill 包顶层清单。字段设为可选，便于在加载阶段统一做容错。
type SkillManifest = {
  name?: string;
  label?: string;
  description?: string;
  version?: string;
  tools?: SkillToolManifest[];
};

// 完成“磁盘清单 → 运行时 Tool”转换后使用的内部结构。
export type SkillPackageTool = {
  skillId: string;
  skillName: string;
  baseDir: string;
  manifest: SkillToolManifest;
};

// 默认只允许常见脚本解释器，防止 skill.json 随意执行系统程序。
const DEFAULT_ALLOWED_BINARIES = ["node", "python", "python3"];

// 把相对安装路径转换为以项目根目录为基准的绝对路径。
function resolveProjectPath(pathValue: string) {
  if (!pathValue) return "";
  return isAbsolute(pathValue) ? pathValue : join(process.cwd(), pathValue);
}

// 安全边界：脚本必须仍在当前 Skill 目录内，禁止通过 ../ 越权访问。
function assertInsideSkill(baseDir: string, target: string) {
  const relativePath = relative(baseDir, target);
  if (relativePath.startsWith("..") || isAbsolute(relativePath)) {
    throw new Error(`脚本路径必须位于 Skill 包目录内：${target}`);
  }
}

// 把命令拆成“程序 + 参数”；同时保留双引号包裹的带空格参数。
function commandParts(command: string) {
  const parts = command.match(/(?:[^\s"]+|"[^"]*")+/g)
    ?.map((part) => part.replace(/^"|"$/g, "")) ?? [];
  if (parts.length === 0) throw new Error("Skill Tool command 不能为空");
  return parts;
}

// 解析并校验脚本命令：工具目录参数会被转成安全的绝对路径。
export function resolveScriptCommand(baseDir: string, command: string) {
  const [bin, ...args] = commandParts(command);
  const resolvedArgs = args.map((arg) => {
    if (!arg.startsWith("tools/") && !arg.startsWith("./tools/")) return arg;
    const scriptPath = resolve(baseDir, normalize(arg));
    assertInsideSkill(baseDir, scriptPath);
    return scriptPath;
  });

  const allowed = new Set(
    (process.env.NEW_AGENT_SKILL_BINARIES || DEFAULT_ALLOWED_BINARIES.join(","))
      .split(",").map((item) => item.trim()).filter(Boolean),
  );
  if (!allowed.has(bin)) throw new Error(`Skill Tool 不允许执行该程序：${bin}`);
  return { bin, args: resolvedArgs };
}

// 为子进程创建最小环境变量集合，避免把模型密钥等敏感信息传给 Skill。
function buildSkillEnvironment(skillName: string, toolName: string) {
  // 不把 API Key 等全部环境变量交给子进程，只复制白名单。
  const safeNames = new Set(["PATH", "LANG", "LC_ALL", "TMPDIR"]);
  const env: NodeJS.ProcessEnv = {};
  for (const name of safeNames) {
    if (process.env[name] !== undefined) env[name] = process.env[name];
  }
  env.NEW_AGENT_SKILL_NAME = skillName;
  env.NEW_AGENT_TOOL_NAME = toolName;
  return env;
}

// Pi Tool 需要 TypeBox Schema；未声明参数时使用空对象。
function toToolSchema(schema: unknown) {
  return schema && typeof schema === "object" ? schema as any : Type.Object({});
}

// 从 Skill 安装目录读取 skill.json；无效 JSON 或文件缺失时返回 null。
export async function readSkillManifest(skill: SkillRecord) {
  const baseDir = resolveProjectPath(skill.installPath);
  if (!baseDir) return null;
  try {
    return JSON.parse(
      await readFile(join(baseDir, "skill.json"), "utf-8"),
    ) as SkillManifest;
  } catch {
    return null;
  }
}

// 扫描所有 Skill，只收集字段完整、可以真正执行的 Tool。
export async function discoverSkillPackageTools(skills: SkillRecord[]) {
  const result: SkillPackageTool[] = [];
  for (const skill of skills) {
    const manifest = await readSkillManifest(skill);
    const baseDir = resolveProjectPath(skill.installPath);
    for (const tool of manifest?.tools ?? []) {
      if (!tool.name || !tool.label || !tool.description || !tool.command) continue;
      result.push({
        skillId: skill.id,
        skillName: skill.name,
        baseDir,
        manifest: {
          ...tool,
          category: tool.category ?? "agent",
          access: tool.access ?? "execute",
        },
      });
    }
  }
  return result;
}

// 执行单个脚本型 Tool，并把它适配为 Pi SDK 约定的 ToolResult。
async function runScriptTool(
  tool: SkillPackageTool,
  params: Record<string, unknown>,
  signal?: AbortSignal,
) {
  // 第一步：解析白名单程序和脚本参数，拒绝危险命令。
  const { bin, args } = resolveScriptCommand(tool.baseDir, tool.manifest.command);
  // Tool 参数统一序列化为一份 JSON，通过 stdin 交给子进程。
  const serializedInput = JSON.stringify(params ?? {});
  if (Buffer.byteLength(serializedInput) > runtimeConfig.skillMaxInputBytes) {
    throw new Error(`Tool 输入超过 ${runtimeConfig.skillMaxInputBytes} bytes 限制`);
  }

  // cwd 固定到 Skill 包目录，stdin/stdout/stderr 都由主进程接管。
  const child = spawn(bin, args, {
    cwd: tool.baseDir,
    stdio: ["pipe", "pipe", "pipe"],
    env: buildSkillEnvironment(tool.skillName, tool.manifest.name),
  });

  // 超时、取消或输出超限时先发送 SIGTERM，1 秒后仍未退出则强制结束。
  let timedOut = false;
  let outputExceeded = false;
  const terminate = () => {
    child.kill("SIGTERM");
    setTimeout(() => child.kill("SIGKILL"), 1_000).unref();
  };
  const timeout = setTimeout(() => {
    timedOut = true;
    terminate();
  }, runtimeConfig.skillTimeoutMs);
  const abort = () => terminate();
  signal?.addEventListener("abort", abort, { once: true });

  // 分别收集标准输出和错误输出，并持续检查输出体积。
  let stdout = "";
  let stderr = "";
  child.stdout.on("data", (chunk) => {
    stdout += String(chunk);
    if (Buffer.byteLength(stdout) > runtimeConfig.skillMaxOutputBytes) {
      outputExceeded = true;
      terminate();
    }
  });
  child.stderr.on("data", (chunk) => {
    stderr += String(chunk);
    if (Buffer.byteLength(stderr) > runtimeConfig.skillMaxOutputBytes) {
      outputExceeded = true;
      terminate();
    }
  });

  // 一次执行只写入一份 JSON，随后关闭 stdin。
  // end() 会在写入 JSON 后关闭 stdin，让脚本知道输入已经结束。
  child.stdin.end(serializedInput);
  const exitCode = await new Promise<number | null>((resolveExit, reject) => {
    child.on("error", reject);
    child.on("close", resolveExit);
  }).finally(() => {
    clearTimeout(timeout);
    signal?.removeEventListener("abort", abort);
  });

  // 将不同失败原因转换成清晰错误，便于前端展示和课堂排查。
  if (timedOut) throw new Error(`${tool.manifest.name} 执行超时，已终止`);
  if (outputExceeded) throw new Error(`${tool.manifest.name} 输出过大，已终止`);
  if (signal?.aborted) throw new Error(`${tool.manifest.name} 已取消`);
  if (exitCode !== 0) {
    throw new Error(stderr.trim() || `执行失败，exitCode=${exitCode}`);
  }

  // 优先接受脚本直接返回的标准 ToolResult；否则把 stdout 包装成文本结果。
  const text = stdout.trim();
  try {
    const parsed = JSON.parse(text);
    if (parsed && Array.isArray(parsed.content)) return parsed;
  } catch {
    // 兼容只输出纯文本的简单脚本。
  }
  return {
    content: [{ type: "text", text }],
    details: {
      skill: tool.skillName,
      tool: tool.manifest.name,
      stderr: stderr.trim() || undefined,
    },
  };
}

// 把发现到的 Skill Tool 批量注册为 Pi SDK 的 ToolDefinition。
export function createSkillToolDefinitions(
  skillTools: SkillPackageTool[],
): ToolDefinition[] {
  return skillTools.map((tool) => defineTool({
    name: tool.manifest.name,
    label: tool.manifest.label,
    description: tool.manifest.description,
    parameters: toToolSchema(tool.manifest.inputSchema),
    // Pi 调用 Tool 时会进入 execute；signal 用来响应用户取消。
    async execute(_id, params, signal) {
      return runScriptTool(tool, params as Record<string, unknown>, signal);
    },
  }));
}