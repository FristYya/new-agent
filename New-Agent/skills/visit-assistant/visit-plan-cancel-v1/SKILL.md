---
name: visit-plan-cancel-v1
description: AI 拜访助手—取消拜访计划。先查询可取消计划，用户确认后按 plan_id 执行取消。
---

# 取消拜访计划

> ## 输出互斥规则（最高优先级）
>
> **绝对禁止输出"思考过程"**：不得输出任何分析说明（如"根据 skill 指令..."、"按照规则..."）。
>
> **每条回复必须二选一，严禁混合：**
> - **模式 A — 纯卡片**：整条回复 = `_suggestedCard` 原样输出，块外**零字符**
> - **模式 B — 纯自然语言**：整条回复无任何协议块
>
> 有 `_suggestedCard` 时必须用模式 A。协议块写法：`<<<VISIT_BLOCK_V1>>>{JSON单行}<<<END_VISIT_BLOCK_V1>>>`，禁止转义、禁止包裹在代码块中。

## 凭据获取

> New-Agent 下 **不要** 传 `--cookies`。脚本从 `AGENT_RUNTIME_CONTEXT / RUN_CONTEXT` 自动读取。
> Cookie ← `bizContext.cookie`，对话 ID ← `--chat-id` 或 `sessionId`。

## 运行环境约束

> **不要开启沙箱**。本 Skill 只做 HTTP 接口调用，不读写文件。脚本用 **`python3`** 执行，仅依赖标准库。
> 读完查询类脚本后，**必须立刻决策并 STOP**，禁止同轮反复 `exec`。

---

## 1. 触发场景

用户表达以下意图时触发：
- 取消拜访计划 / 不去这条拜访了 / 把某条计划撤销/删掉

不属于本 Skill：新建 → `visit-plan-create`，修改 → `visit-plan-update`，签到/签退 → `visit-checkin`

---

## 2. 标准执行流程

### 整体流程

```
用户说"取消 xxx"
 ↓
Step 0：plan-list 查计划（keyword=用户提到的关键词）
 ├─ 返回多条 → 脚本自动构建 visit-plan-result-card _suggestedCard，原样输出
 └─ 返回唯一1条 → 直接定位（跳过列表卡，但不跳过确认卡）
 ↓
Step 0.3：plan-detail 构建确认卡（脚本自动输出 _suggestedCard）
 ↓
原样输出 cancel-visit-plan-card _suggestedCard，等用户点击"取消计划"按钮
 ↓
Step 1：cancel 执行取消（脚本自动构建 action_result + visit-plan-cancelled-card _suggestedCard）
 ↓
原样输出 _suggestedCard
```

> **核心防跳规则**：用户说了计划名 ≠ 已确认取消。**必须 Step 0 → Step 0.3 → 输出确认卡 → 用户点击卡片按钮 → 才能执行 Step 1。** 自然语言"确认/取消吧"不是有效确认。

---

### Step 0：查询当前用户的拜访计划

```bash
python3 "<skill_directory>/scripts/visit_client.py" plan-list \
 --keyword "$KEYWORD" \
 --limit 20
```

> - 用户说了客户名/时间/地址时，`--keyword` 填用户提到的关键词；没有限定时留空

返回后的处理规则：
- **返回多条**：脚本自动构建 `visit-plan-result-card` `_suggestedCard`，**原样输出**（块外零字符），等用户点击选定某条
- **返回唯一一条**：**跳过**列表卡，直接以该条的 `visitPlanId` 进入 Step 0.3
- **任何情况都禁止跳过 Step 0.3 确认卡直接取消**

用户选定某条 → 取 `payload.item.visitPlanId` 作为 `$PLAN_ID`，进入 Step 0.3。

> **planId 提取规则**：取 `payload.item.visitPlanId`，禁止把整个 JSON 或 `intent` 字段当作计划 ID。

### Step 0.3：补全计划详情

```bash
python3 "<skill_directory>/scripts/visit_client.py" plan-detail \
 --plan-id "$PLAN_ID"
```

脚本自动构建 `cancel-visit-plan-card` `_suggestedCard`，**原样输出**。等用户点击"取消计划"按钮。

| 操作 | 处理 |
|------|------|
| 点击**取消计划**（`cancel_plan`） | 执行 Step 1，原样输出 `_suggestedCard` |
| 自然语言"确认"/"取消吧" | **不执行**，回复：请在卡片上点击"取消计划"按钮 |

### Step 1：执行取消

```bash
python3 "<skill_directory>/scripts/visit_client.py" cancel \
 --chat-id "$CHAT_ID" \
 --plan-id "$PLAN_ID" \
 --client-action-id "$CLIENT_ACTION_ID" \
 --card-id "$CARD_ID" \
 --card-data "$CARD_DATA" \
 --app visit
```

**参数说明**（从前端 Prompt `input` 提取）：
- `--client-action-id` ← `input.clientActionId`，原样回传
- `--card-id` ← `input.target.card_id`，原样回传
- `--card-data` ← `input.data` JSON 字符串（脚本回填到 `skill_result.data`）

脚本自动构建 `_suggestedCard`：成功时含两块（`action_result` + `visit-plan-cancelled-card`），失败时只含 `action_result` 一块。**原样输出，禁止手工拼凑。**
