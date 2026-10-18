---
name: visit-assistant
description: 拜访助手总控 skill，覆盖拜访全流程：查询拜访待办、拜访签退、拜访签到、填写拜访日志、修改拜访日志、拜访录音、创建拜访计划、修改拜访计划、取消拜访计划。当用户说了以下行为或信息时触发本 skill：签退、我要签退、结束拜访、签退当前拜访、填日志、填写日志、写拜访纪要、补录、修改日志、修改拜访日志、修改拜访纪要、改日志、改纪要、改一下日志、改一下纪要、编辑日志、编辑拜访纪要、录音、我要录音、录制会议音频、记录拜访、今日待办、待办、未签到、未签退、待填日志、哪些没填纪要、签到、我要签到、开始拜访、创建拜访计划、新建拜访计划、创建计划、新建计划、创建拜访、新建拜访、帮我建个拜访、建一个拜访、我要创建拜访、安排拜访、安排一个拜访、约拜访、加个拜访、修改拜访计划、修改计划、编辑计划、编辑拜访计划、改拜访计划、改一下拜访计划、我要改拜访计划、更新拜访计划、取消拜访计划、取消计划、删除计划、删除拜访计划、取消拜访、不要拜访了、撤销拜访、撤销计划、不去了、取消那个拜访、出差拜访、本地拜访、去出差拜访、本地去看客户、补充差旅计划、补充拜访计划、填差旅计划、差旅待补充、补充出差拜访、补充出差、待填写差旅、待补充差旅、有哪些差旅待补充、查询待填写计划、查看待补充计划、待填写计划、查看待填写、查询待填写、有待填写计划、待填写的拜访。
---

> **【违反即本轮输出作废重写】** — 本文件是路由层，路由命中后输出只能是：(a) 候选列表（由子 skill 模板定义）或 (b) 卡片协议块。**禁止输出任何内部话术、状态描述、推理过程**。

# 拜访助手（visit-assistant）— 总控路由

> **本文件只做意图识别 + 路由指令。**
> 路由命中后，必须先 **read 对应子 skill 的 SKILL.md**，再严格按子文件规则执行，禁止自行发挥。

---

## ★ 第一步：意图识别

### 1.0 前端结构化 Prompt 路由（最高优先级，优先于一切）

前端有时会发送固定格式的结构化 Prompt，**必须精确匹配，直接执行，不做任何语义理解**：

| Prompt 模式 | 执行指令 |
|------------|----------|
| `加载拜访 {visitId} 已生成的纪要，并返回对应的 visit-summary-card 的 a2ui.render 协议块` | **立即** read `record/SKILL.md`，按场景 B 执行：提取 visitId → `python3 scripts/query_visit_plans.py --mode summary --visit-plan-id {visitId}` → 原样输出结果 |

**卡片点击回调路由**（消息含 `<<<VISIT_BLOCK_V1>>>` 时精确匹配 `card_name` + `action_id`）：

| 条件 | 执行指令 |
|------|---------|
| `card_name="visit-pending-card"` + `action_id="view_pending_item"` + `payload.pendingType="pending-travel-plan"` | **立即** read `visit-plan-create/SKILL.md`，按加载规则第 6 条执行：取 `payload.visitId` → 调 `supplement-plan-card --plan-id $PLAN_ID` → 原样输出 `_suggestedCard`。**禁止追问、禁止自然语言交互** |
| `card_name="visit-plan-card"` + `payload.action_id="supplement_plan"` 或 `operationType="supplement_plan"` | **立即** read `visit-plan-create/SKILL.md`，按加载规则第 4 条执行：取 `a2uiCardData` → 调 `direct-fill-card --data '<a2uiCardData>'` → 原样输出 `_suggestedCard` |

> ⚠️ 收到上述卡片点击时：直接按上表执行，**不追问、不路由到其他子 skill、不做语义理解**。

### 1.1 写操作路由（最高优先级）

前端卡片写操作 Prompt 中含 `skill_name` 字段，**按 skill_name 直接路由**，无需意图识别：

| `skill_name` 值         | 执行指令 |
|------------------------|---------|
| `visit-checkout`       | **立即** read `checkout/SKILL.md`，按其规则全量执行 |
| `visit-filllog`        | **立即** read `filllog/SKILL.md`，按其规则全量执行 |
| `visit-pending`        | **立即** read `pending/SKILL.md`，按其规则全量执行 |
| `visit-record`         | **立即** read `record/SKILL.md`，按其规则全量执行 |
| `visit-checkin`        | **立即** read `visit-checkin/SKILL.md`，按其规则全量执行 |
| `visit-plan-create`    | **立即** read `visit-plan-create/SKILL.md`，按其规则全量执行 |
| `visit-plan-update`    | **立即** read `visit-plan-update/SKILL.md`，按其规则全量执行 |
| `visit-plan-cancel-v1` | **立即** read `visit-plan-cancel-v1/SKILL.md`，按其规则全量执行 |

### 1.2 用户意图路由

| 用户意图关键词（命中任一即路由） | 执行指令 |
|--------------|---------|
| 签退、我要签退、结束拜访、签退当前拜访、checkout | **立即** read `checkout/SKILL.md`，按其规则全量执行 |
| 查看录音摘要、录音纪要、拜访录音摘要、录音摘要 | **立即** read `record/SKILL.md`，按其规则全量执行 |
| 填日志、填写拜访日志、写拜访纪要、补录、fill log | **立即** read `filllog/SKILL.md`，按其规则全量执行 |
| 修改日志、修改拜访日志、修改拜访纪要、改日志、改纪要、改一下日志、改一下纪要、编辑日志、编辑拜访纪要 | **立即** read `filllog/SKILL.md`，按其规则全量执行（⚠️ 走 filllog 场景 B 修改日志，不是 visit-plan-update） |
| 今日待办、待办计划、未签到、未签退、待填日志、哪些没填纪要 | **立即** read `pending/SKILL.md`，按其规则全量执行 |
| 待填写差旅、待补充差旅计划、有哪些差旅待补充、查询待填写计划、查看待补充计划、待填写计划、查看待填写、查询待填写、有待填写计划、待填写的拜访 | **立即** read `pending/SKILL.md`，按其规则全量执行（mode=travel-plan，只读查询待填写差旅计划列表） |
| 录音、我要录音、录制会议音频、记录拜访、record | **立即** read `record/SKILL.md`，按其规则全量执行 |
| 签到、我要签到、开始拜访 | **立即** read `visit-checkin/SKILL.md`，按其规则全量执行 |
| 创建拜访计划、新建拜访计划、创建计划、新建计划、创建拜访、新建拜访、帮我建个拜访、建一个拜访、安排拜访、安排一个拜访、约拜访、加个拜访、我要创建拜访 | **立即** read `visit-plan-create/SKILL.md`，按其规则全量执行 |
| 补充差旅计划、补充拜访计划、填差旅计划、补充出差拜访、补充出差 | **立即** read `visit-plan-create/SKILL.md`，**直接跳到「★ 快速通道：补充差旅拜访计划」**，跳过 FSM 初始化，第一步执行 `pending-travel-plan`。⚠️ 注意：此路由只在用户明确表示**要动手补充/填写差旅计划**时走；如果用户只是**查有哪些待补充**，应走上面 pending 路由 |
| 修改拜访计划、修改计划、编辑计划、编辑拜访计划、改拜访计划、改一下拜访计划、我要改拜访计划、更新拜访计划 | **立即** read `visit-plan-update/SKILL.md`，按其规则全量执行 |
| 注意：以上 visit-plan-update 只处理「修改拜访计划本身」（改计划时间/地点/对象），不处理「修改拜访日志/纪要」。「修改日志」「修改拜访纪要」「改日志」「改纪要」等必须走上面的 filllog 路由 |
| 取消拜访计划、取消计划、删除计划、删除拜访计划、取消拜访、不要拜访了、撤销拜访、撤销计划、不去了、取消那个拜访 | **立即** read `visit-plan-cancel-v1/SKILL.md`，按其规则全量执行 |

### 1.3 意图识别规则

- **关键词匹配优先**：用户消息中包含上表任一关键词，即命中对应路由，**不需要语义理解**
- **模糊意图兜底**：用户说"我要拜访"、"弄个拜访"等不含精确关键词时，按以下规则判断：
  - 含"日志/纪要/补录" + "改/修改/编辑" → 修改拜访日志（走 filllog 场景 B）
  - 含"创建/新建/安排/建/加/约" → 创建拜访计划
  - 含"补充/填写" + "差旅/出差/计划" → 进一步区分意图：
    - 含"查/看/有哪些/多少" → **查待填写差旅计划**（走 pending，只读）
    - 不含查询意图，明确要**动手补充** → **补充差旅拜访计划**（走 visit-plan-create 快速通道）
  - 含"改/修改/编辑/更新" + "计划/拜访计划" → 修改拜访计划
  - 含"改/修改/编辑/更新" 但未提"日志/纪要/计划" → 追问是「修改拜访计划」还是「修改拜访日志/纪要」
  - 含"取消/删除/撤销/不要/不去" → 取消拜访计划
  - 含"去/到/开始" → 签到
  - 含"结束/完成/回来" → 签退
  - 仍无法判断 → 追问

- **多意图**同时出现：优先级 **签退 > 签到 > 录音摘要 > 填日志 > 修改日志 > 取消计划 > 修改拜访计划 > 创建计划 > 录音 > 查待办（含查差旅待补充）**，只处理最高优先级，其余提示用户继续。
  -  关键区分：「修改日志/修改拜访纪要」优先级高于「修改拜访计划」，因为前者查的是已完成的拜访、后者查的是待拜访的计划，两者互不冲突
  -  **"修改拜访"歧义区分**："修改拜访" 这个词有歧义（可能改计划也可能改日志），务必看完整句：
  - 含"日志/纪要"（如"修改拜访日志""修改拜访纪要"）→ filllog
  - 含"计划"或无明确对象（如"修改拜访计划""改一下拜访"）→ 追问明确是改计划还是改日志

- **意图不明**：追问「请问您想进行哪项操作？签退、签到、填写日志、查待办、录音、创建拜访计划、修改拜访计划、取消拜访计划？」

---

## ★ 第二步：执行（read 子 SKILL.md 后严格按其规则）

路由命中后的固定动作：

```
1. read 对应子 skill 的 SKILL.md（路径见上表）
2. 读取完成后，完全按子 SKILL.md 的规则执行，包括：
   - 调用哪些脚本、传什么参数
   - 输出什么格式的卡片
   - 如何处理写操作
3. ⛔ 禁止跳过 read 步骤直接凭记忆执行
4. ⛔ 禁止混合多个子 skill 的规则
```

---

## 通用约束

- 协议块格式：`<<<VISIT_BLOCK_V1>>>{单行JSON}<<<END_VISIT_BLOCK_V1>>>`，必须单行，禁止 `\"` 转义
- 卡片和文字不能混发：发卡片时不加文字，发文字时不带卡片
- 用 **`python3`** 执行脚本，不用 `uv`
- 所有脚本只能通过当前运行时批准的工具执行；写操作必须等待平台审批，禁止通过其他命令绕过审批。

---

## 目录结构

```
visit-assistant/
├── SKILL.md                ← 本文件（总控路由，只做路由）
├── checkout/               ← 签退子 skill
│   ├── SKILL.md
│   ├── scripts/
│   └── assets/
├── filllog/                ← 填写日志子 skill
│   ├── SKILL.md
│   ├── scripts/
│   ├── assets/
│   └── references/
├── pending/                ← 查询待办子 skill
│   ├── SKILL.md
│   ├── scripts/
│   └── assets/
├── record/                 ← 录音子 skill
│   ├── SKILL.md
│   └── scripts/
├── visit-checkin/        ← 签到子 skill
│   ├── SKILL.md
│   ├── scripts/
│   ├── assets/
├── visit-plan-create/      ← 创建拜访计划子 skill
│   ├── SKILL.md
│   ├── scripts/
│   ├── assets/
│   └── references/
├── visit-plan-update/      ← 修改拜访计划子 skill
│   ├── SKILL.md
│   ├── scripts/
│   ├── assets/
│   └── references/
└── visit-plan-cancel-v1/   ← 取消拜访计划子 skill
│   ├── SKILL.md
│   ├── scripts/
│   ├── assets/

```
