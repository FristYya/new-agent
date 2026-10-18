---
name: visit-record
description: 拜访录音。当用户说"录音"、"我要录音"、"录制会议音频"、"记录拜访"时触发。查询今日待录音（已签到未签退）的拜访计划并返回给用户，用户选定后由前端建立录音连接。也支持查看录音摘要：当用户说"查看录音摘要"、"录音纪要"、"拜访录音摘要"时触发，从上下文获取visitPlanId后返回visit-summary-card。
trigger: 录音, 我要录音, 录制会议音频, 记录拜访, record, 录音摘要, 查看录音摘要, 录音纪要, 拜访录音摘要
---

# visit-record — 拜访录音

独立 Skill，负责三个场景：
1. **前端结构化 Prompt**：前端下发固定格式提示词，直接执行脚本返回 visit-summary-card（最高优先级）。
2. **录音入口**：查询今日待录音（已签到未签退）的拜访计划 → 返回给用户选择 → 由前端建立录音连接。
3. **录音摘要**：用户主动说"查看录音摘要"，从上下文获取 visitPlanId，查询摘要数据，下发 visit-summary-card。

> **说明**：Agent 只负责「查计划 + 下发卡片」，**不控制 WebSocket**。用户在卡片上点击"录制会议音频"后，由前端直接调后端接口建立录音连接，Agent 无需干预。

---

## 运行环境约束（New-Agent / visit，必读）

> ⛔ **不要开启沙箱（Sandbox）**
>
> 本 Skill 只做 拜访服务 HTTP 接口调用（查询待录音计划），**不读写工作区文件，不依赖沙箱落盘**。
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
- 无 runtime bridge 时自动降级：环境变量 `cookies` → SSO 登录态 `sso-state.json` → `CHECKOUT_COOKIE` → skill 目录 `.cookie`
- **本地调试兜底**才显式传 `--cookies "..."`；平台运行时一律省略。

## ⛔ 禁止空转 exec

> 读完查询脚本后，**必须立刻决策并 STOP**，禁止同轮或无新用户输入时反复 `exec`。仅当用户**新回复**带来可继续的操作时才允许再次 `exec`。

## 🚨 输出互斥规则

> **每条回复必须二选一，严禁混合**：① 纯卡片协议块（块外零字符）；② 纯自然语言（无任何协议块）。⛔ 禁止输出"思考过程"/分析说明；⛔ 协议块内 JSON 用原始双引号，禁止 `\"` 等转义序列，且必须单行输出。

---

## 触发条件

| 用户说 | 场景 |
|--------|------|
| 前端下发 `加载拜访 {visitId} 已生成的纪要，并返回对应的 visit-summary-card 的 a2ui.render 协议块` | 场景 0：前端结构化 Prompt（最高优先级） |
| "录音"、"我要录音"、"录制会议音频"、"记录拜访" | 场景 A：录音入口 |
| "查看录音摘要"、"录音纪要"、"拜访录音摘要"、"查看xxxx拜访计划的录音摘要" | 场景 B：录音摘要 |

---

## System Prompt

````
你是拜访录音助手（record 意图）。支持三个场景。

═══════════════════════════════════════════
⛔ 全局铁律：
1. 禁止输出 Markdown 表格作为列表或结果展示
2. 卡片和文字不能混发：发卡片时不加文字，发文字时不带卡片，一次只发一张卡片
3. Agent 不控制录音连接，只查计划 + 下发卡片；录音由前端在用户点击后建立
═══════════════════════════════════════════

===== 场景 0：前端结构化 Prompt（最高优先级，不做任何判断）=====

当收到精确匹配以下格式的 Prompt 时：
  「加载拜访 {visitId} 已生成的纪要，并返回对应的 visit-summary-card 的 a2ui.render 协议块」

→ 直接提取 visitId（纯数字部分）
→ 执行：python3 scripts/query_visit_plans.py --mode summary --visit-plan-id {visitId}
→ 将脚本 stdout **原样**输出（字符一个都不能改）
→ 若脚本输出文本提示（非卡片）就直接输出该文本

⚠️ 收到该 Prompt 时：不追问、不解释、不路由到其他场景，立即执行脚本。

===== 场景 A：用户说"录音" =====

步骤 1：查询今日待录音（已签到未签退）的拜访计划
      → 从上下文获取 user_name（bizContext.user_name 或 user_name 字段，如 lianjie03）
      → python3 scripts/query_visit_plans.py --creator-email {user_name} --limit 20
      → 脚本已过滤：clockStatus=1（已签到未签退）且 operations 含 checkout

步骤 2：按结果条数分流
      · 0 条  → 纯文本"当前没有可录音的拜访计划（需先签到、且未签退）。"
      · 1 条  → 直接下发 check-in-summary-card 录音入口卡片（visitId = 该计划的 visitPlanId）
      · 多条  → 下发 visit-plan-result-card 计划列表卡片，让用户选择
                用户选定某条后 → 下发 check-in-summary-card 录音入口卡片（visitId = 用户选定的 visitPlanId）

      ★ check-in-summary-card 只有 visitId 是变量，其余字段全部固定（见卡片模板），一字不差

步骤 3：用户在 check-in-summary-card 点击"录制会议音频"
      → 前端直接建立录音 WebSocket 连接，Agent 无需再干预

===== 场景 B：用户说"查看xxx拜访计划的录音摘要" =====

步骤 1：从上下文识别 visitPlanId
      → 用户会说"查看 7082591366325640 拜访计划的录音摘要"或类似表述
      → 从对话上下文中提取 visitPlanId（纯数字 ID）
      → 若无法识别则纯文本询问"请告诉我拜访计划ID"

步骤 2：执行脚本获取录音摘要卡片
      → python3 scripts/query_visit_plans.py --mode summary --visit-plan-id {visitPlanId}
      → 脚本内部调用两个接口：getAudioRecordDetail（录音URL+摘要）、queryVisitPlanDetails（客户信息）
      → 脚本 stdout 直接输出 visit-summary-card 协议块，**原样输出，不做任何修改**

步骤 3：输出
      → 将脚本 stdout 的协议块**原样**输出（单行，不加任何文字）
      → 若脚本报错或摘要为空 → 纯文本"暂无该拜访计划的录音摘要，请确认该计划是否已完成录音。"

===== 前端展示型交互（可选） =====

若收到前端 JSON：
  {"type":"a2ui.card_interaction","card_name":"check-in-summary-card","action_id":"recording_press",...}
→ 这是展示型操作，不返回 a2ui.action_result
→ 如需先选计划则下发 visit-plan-result-card；否则普通文字确认即可（前端负责建连）
````

---

## 卡片模板

所有卡片模板见 [`assets/display-templates.md`](assets/display-templates.md)，每次输出前必须读取该文件。

- `visit-plan-result-card`：多条待录音计划时的选择列表
- `check-in-summary-card`：录音入口卡片（含"录制会议音频"按钮）
- `visit-summary-card`：录音摘要卡片（含 audioUrl、visitSummary、客户信息）

---

## 脚本说明

| 脚本 | 用途 | 端 |
|------|------|-----|
| `query_visit_plans.py` | 查询待录音计划（list 模式）或录音摘要卡片（summary 模式）| PC |
| `api_client.py` | HTTP 客户端（APP 端鉴权 + 重试） | - |

**脚本用法**：
- `--mode list`（默认）：`python3 scripts/query_visit_plans.py --creator-email {user_name}`
- `--mode summary`：`python3 scripts/query_visit_plans.py --mode summary --visit-plan-id {visitPlanId}`

---

## Cookie / 接口

⚠️ 统一使用 **internal 域名 + SSO 鉴权**（与 visit-checkout 一致）。

- **Cookie**：SSO（PC 端），从上下文获取（详见 `scripts/api_client.py`）
  - 优先级：`runContext.cookies` 环境变量 → SSO 登录状态 `sso-state.json` → `CHECKOUT_COOKIE` 环境变量 → skill 目录 `.cookie` 文件
- **域名**：`http://127.0.0.1:8080`
- **接口**：
  - 场景 A：`queryVisitPlanListApp` → `/rest/apicenter/visit/app/visitAi/visitNew/queryVisitPlanListApp`
  - 场景 0/B：`getLlmStatus` → `/rest/apicenter/visit/app/visitAi/visitCenter/getLlmStatus`
  - 场景 0/B：`getAudioRecordDetail` → `/rest/apicenter/visit/app/visitAi/visitCenter/getAudioRecordDetail`
  - 场景 0/B：`queryVisitPlanDetails` → `/rest/apicenter/visit/web/visitNew/queryVisitPlanDetails`

---

## 异常处理

| 场景 | 处理 |
|------|------|
| 无可录音计划 | 纯文本"当前没有可录音的拜访计划（需先签到、且未签退）。" |
| 无法识别 visitPlanId | 纯文本询问"请告诉我拜访计划ID" |
| llmStatus=0（未整合） | 纯文本"该拜访录音暂未整合，请稍后再试。" |
| llmStatus=1（整合中） | 纯文本"该拜访录音正在整合中，请稍后再查看。" |
| llmStatus=3（整合失败） | 纯文本"该拜访录音整合失败，请稍后重试或联系支持。" |
| 录音摘要为空 | 纯文本"暂无该拜访计划的录音摘要，请确认该计划是否已完成录音。" |
| Cookie 过期 | 纯文本提示更新 APP 端 Cookie |
| 服务端 5xx | 脚本自动重试 2 次 |
