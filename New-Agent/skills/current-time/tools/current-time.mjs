const input = await new Promise((resolve) => {
  let text = "";
  process.stdin.on("data", (chunk) => {
    text += chunk;
  });
  process.stdin.on("end", () => resolve(JSON.parse(text || "{}")));
});

const timezone = input.timezone || "Asia/Shanghai";
const locale = input.locale || "zh-CN";
const now = new Date();
const formatted = new Intl.DateTimeFormat(locale, {
  timeZone: timezone,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
  weekday: "long",
}).format(now);

const text = `当前时间：${formatted}\n时区：${timezone}\nISO：${now.toISOString()}`;
console.log(JSON.stringify({ content: [{ type: "text", text }], details: { timezone, locale, iso: now.toISOString(), unixMs: now.getTime() } }));
