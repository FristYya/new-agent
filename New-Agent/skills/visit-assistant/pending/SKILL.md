---
name: visit-pending
description: 查询拜访待办事项。当用户说"今日待办/待办计划"、"有哪些未签到的"、"有哪些未签退的"、"哪些还没填日志/写纪要"、"待填写差旅/待补充差旅计划/有哪些差旅待补充/查询待填写计划/查看待补充计划"时触发。根据用户意图精准识别查询类别，返回 visit-pending-card 卡片。
---

# 拜访待办事项 Skill（visit-pending）

查询当前登录人的拜访待办，按用户意图精准识别为五种模式之一，**只返回一个 `visit-pending-card` 卡片**。

支持四类待办：
- **待填写差旅计划**（走 `queryAiVisit queryType=pending_travel`）
- **待签到** / **待签退** / **待填日志**（走 `queryVisitPlanList`）

---

## 运行环境约束（New-Agent / visit，必读）

> ⛔ **不要开启沙箱（Sandbox）**
>
> 本 Skill 只做 拜访服务 HTTP 接口调用（查询拜访待办），**不读写工作区文件，不依赖沙箱落盘**。
>
> - **禁止**配置或调用：`sandbox_create`、`sandbox_fs_write`、`sandbox_fs_edit`、`sandbox_fs_create_dir`、`sandbox_fs_delete_dir`、`workspace_read`、`sandbox_exec_run`
> - Agent / profile 的 toolAllowlist **不要**包含上述沙箱工具
> - 需要的工具仅限：`exec`、`read`（可选：`get_runtime_context`）
> - 误开沙箱不会带来能力收益，只会增加无关步骤和失败面。
>
> **脚本执行规范**：
> - 用 **`python3`** 执行（不要用 `uv`），且**仅依赖 Python 标准库 + requests**，无其它第三方包。
> - ⛔ **禁止写文件**：脚本不做任何本地落盘（无 session cache、不写 WORKSPACE）。

## ⚙️ 凭据获取（必读）

> New-Agent / visit 下 **不要**让模型传 `--cookies` / `--user_name`。脚本会从已绑定的 `AGENT_RUNTIME_CONTEXT / RUN_CONTEXT` 自动读取。

- Cookie ← `AGENT_RUNTIME_CONTEXT / RUN_CONTEXT` 的 `bizContext.cookie`（也会投影到顶层 `cookie`），**最高优先级**
- 无 runtime bridge 时自动降级：环境变量 `cookies` → SSO 登录态 `sso-state.json` → `FILLLOG_COOKIE` → skill 目录 `.cookie`
- **本地调试兜底**才显式传 `--cookies "..."`；平台运行时一律省略。

## ⛔ 禁止空转 exec

> 读完查询脚本后，**必须立刻决策并 STOP**，禁止同轮或无新用户输入时反复 `exec`。仅当用户**新回复**带来新的查询意图时才允许再次 `exec`。

## 🚨 输出互斥规则

> **每条回复必须二选一，严禁混合**：① 纯卡片协议块（块外零字符）；② 纯自然语言（无任何协议块）。⛔ 禁止输出"思考过程"/分析说明；⛔ 协议块内 JSON 用原始双引号，禁止 `\"` 等转义序列，且必须单行输出。

---

## 一、意图识别（关键）

根据用户说法，精准判断 mode，调用 `scripts/query_pending.py --mode <mode>`：

| 用户说法（示例） | mode | 说明 |
|-----------------|------|------|
| 今日待办、待办计划、我的待办、全部待办、有哪些拜访要处理 | `all` | **四类全查**（含待补充差旅） |
| 有哪些未签到的、待签到、还没签到、哪些没打卡 | `check-in` | 仅待签到（全部） |
| 今天待签到、今天还没签到、今天需要签到的、今天的待签到 | `check-in --today` | 仅今天计划时间的待签到 |
| 有哪些未签退的、待签退、还没签退、忘记签退 | `check-out` | 仅待签退 |
| 哪些还没填日志、待填日志、未写纪要、日志没填 | `log` | 仅待填日志 |
| 待填写差旅、待补充差旅、补充差旅计划、待填写计划、待补充计划、有哪些差旅要补充 | `travel-plan` | 仅待填写差旅计划 |

**单意图规则**：用户只问某一类时，只查该类，其余三类在卡片里返回空数组、count=0、total 只计当前类。只有说"今日待办/全部"才四类全查。

若用户说法含糊、无法判断（如只说"拜访"），默认按 `all` 处理。

---

## 二、执行流程

1. 确定 mode（见上表）。
2. 执行脚本（creator-email 传从上下文获取的 user_name，如 lianjie03）：

   ```bash
   cd <skill_dir> && python3 scripts/query_pending.py --creator-email {从上下文获取的 user_name} --mode <mode> [--today]
   ```

   `--today` 仅在 mode=`check-in` 且用户明确说"今天"时加上，其他场景不传。

   Cookie 通过环境变量 `FILLLOG_COOKIE` 或平台注入的 `cookies` 自动获取，无需手动传。

3. 拿到脚本输出的 JSON（已是 visit-pending-card 的 data 结构）。

4. 读取 `assets/display-templates.md`，把 JSON 各字段填入模板占位符。
5. **裸输出单行协议块**（头标记+JSON+尾标记同一行、无换行），无任何前后文字。
6. 停止。

---

## 三、输出格式（严格）

⛔ 不输出 Markdown 表格
⛔ 不返回多个卡片（本场景只有 visit-pending-card 一个卡片）
⛔ 不在协议块外附加解释文字

✅ 只输出一个单行协议块：

```
<<<VISIT_BLOCK_V1>>>{单行完整JSON}<<<END_VISIT_BLOCK_V1>>>
```

---

## 四、字段映射速查

`query_pending.py` 已组装好卡片 item，直接使用。

### check-in / check-out / log item 字段

- `customerName` ← 列表 `businessLicense`
- `customerTypeLabel` ← 列表 `visitObject`
- `timeLabel`：
  - 待签到 → `planVisitTime`，格式「MM月DD日」（纯日期）
  - 待签退 / 待填日志 → `actualVisitTime`，格式「MM月DD日 HH：MM」（全角冒号）
- `address` ← 列表 `planVisitAddress`
- `pendingType` ← `pending-check-in` / `pending-check-out` / `pending-log`

### travel-plan item 字段（走另一个接口 `queryAiVisit`）

- `visitId` ← `queryAiVisit` 返回的 `visitPlanId` / `planId`
- `pendingType` ← 固定 `pending-travel-plan`
- `travelOrderNo` ← `travelApplyNo`（差旅单号）
- `travelLocation` ← `cities` 数组拼接为「北京-上海-杭州」
- `travelTime` ← `planVisitTime` 格式化为「MM月DD日」

---

## 五、接口说明

| 待办类型 | 接口 | 路径 | 筛选条件 |
|---|---|---|---|
| 待填写差旅计划 | `queryAiVisit` | `POST /rest/apicenter/visit/web/visitCenter/queryAiVisit` | `queryType=pending_travel` |
| 待签到 | `queryVisitPlanList` | `POST /rest/apicenter/visit/web/visitNew/queryVisitPlanList` | `clockStatus=["0"]` |
| 待签退 | `queryVisitPlanList` | 同上 | `clockStatus=["1"]` + `signInTime` 范围 |
| 待填日志 | `queryVisitPlanList` | 同上 | `visitStatus=["2"]` |

> **注意**：`queryAiVisit` 走 `visitCenter` 路径前缀，与 `queryVisitPlanList` 的 `visitNew` 不同，且必须带 `trace-context` 泳道头（`PRT.visit`），脚本 `api_client.py` 已自动处理。

---

## 六、目录结构

```
visit-pending/
├── SKILL.md
├── assets/
│   └── display-templates.md   # visit-pending-card 单行模板（含四类待办字段）
└── scripts/
    ├── api_client.py          # HTTP 客户端（含 queryVisitPlanList + queryAiVisit 双接口）
    └── query_pending.py       # 待办查询主脚本（五种模式）
```
