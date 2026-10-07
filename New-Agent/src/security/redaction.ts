// 用字段名识别敏感信息。
// 只要对象字段名包含 password、token、api_key 等关键字，就直接隐藏整个字段值，
// 避免我们必须先判断字段值是什么类型，也避免嵌套对象中的凭证被遗漏。
const SENSITIVE_KEY =
  /(?:password|passwd|secret|token|authorization|cookie|api[-_]?key|private[-_]?key)/i;

/**
 * 清理普通字符串中可能出现的敏感信息。
 *
 * 有些敏感数据并不在名为 token 或 password 的字段中，而是混在日志文本、
 * URL 或错误消息里，因此除了检查对象字段名，还需要对字符串内容做二次扫描。
 */
function redactString(value: string): string {
  return value
    // Authorization 请求头通常写成 `Bearer <token>`，保留 Bearer 便于判断凭证类型，
    // 但把后面的真实 token 替换掉。
    .replace(/\bBearer\s+[A-Za-z0-9._~+\/-]+=*/gi, "Bearer [REDACTED]")
    // 隐藏形如 sk-xxx 或 pk-xxx 的 API Key；长度限制可以减少普通短文本被误判。
    .replace(/\b(?:sk|pk)-[A-Za-z0-9_-]{12,}\b/g, "[REDACTED]")
    // 处理 URL 查询参数，例如 `?token=abc` 或 `&secret=abc`。
    // 第一个捕获组保留参数名与等号，只替换等号后面的敏感值。
    .replace(
      /([?&](?:token|key|secret|password)=)[^&\s]+/gi,
      "$1[REDACTED]",
    )
    // 处理普通文本中的 `token=abc`、`secret:abc`、`api_key=abc` 等写法。
    // 输出统一改成 `字段名=[REDACTED]`，方便日志阅读与检索。
    .replace(
      /\b(token|secret|password|api[-_]?key)\s*[:=]\s*[^\s&,;]+/gi,
      "$1=[REDACTED]",
    );
}

/**
 * 递归脱敏任意数据，同时处理对象、数组和循环引用。
 *
 * seen 会记录本轮递归已经访问过的对象。这样遇到 `obj.self = obj` 之类的
 * 循环引用时就不会无限递归，也不会导致服务崩溃。
 */
export function redactSensitive(
  value: unknown,
  seen = new WeakSet<object>(),
): unknown {
  // 字符串需要检查内容，因为 token 可能藏在 URL、请求头或错误消息中。
  if (typeof value === "string") return redactString(value);

  // null、undefined、数字、布尔值等非对象数据不包含可继续遍历的子字段，原样返回。
  if (!value || typeof value !== "object") return value;

  // 同一个对象再次出现说明存在循环引用，用占位符代替，终止这条递归路径。
  if (seen.has(value)) return "[CIRCULAR]";

  // 在继续遍历前先登记当前对象，确保它的子节点如果再次指向它时能够被识别。
  seen.add(value);

  // 数组本身没有业务字段名，因此逐项递归检查数组里的字符串、对象或嵌套数组。
  if (Array.isArray(value)) {
    return value.map((item) => redactSensitive(item, seen));
  }

  // 普通对象先拆成键值对，再为每个字段生成脱敏后的新值，最后重新组装成对象。
  return Object.fromEntries(
    Object.entries(value).map(([key, item]) => [
      key,
      // 字段名命中敏感关键字时直接隐藏整个值；未命中时继续递归检查字段内部。
      SENSITIVE_KEY.test(key)
        ? "[REDACTED]"
        : redactSensitive(item, seen),
    ]),
  );
}

/**
 * 生成适合日志、审批卡片和错误消息展示的安全预览文本。
 * 先脱敏、再转换为字符串、最后截断；顺序不能反过来，否则原始凭证可能先进入日志。
 */
export function safePreview(value: unknown, maxLength = 260): string {
  // 先处理所有敏感字段和字符串片段，后续步骤只接触脱敏后的副本。
  const redacted = redactSensitive(value);

  // 字符串可以直接展示；对象和数组需要序列化成单行 JSON，便于写入日志或 SSE。
  const text =
    typeof redacted === "string"
      ? redacted
      : JSON.stringify(redacted ?? "");

  // 限制预览长度，避免超大的 Tool 参数或错误对象撑满日志和审批卡片。
  return text.length > maxLength ? `${text.slice(0, maxLength)}...` : text;
}
