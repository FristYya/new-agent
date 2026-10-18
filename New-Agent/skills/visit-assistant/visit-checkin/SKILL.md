---
name: visit-checkin
description: AI 拜访助手—签到。定位今日待签到计划。
---

# 签到

## 凭据获取

New-Agent 环境下**不要传 `--cookies`**，脚本自动从 `AGENT_RUNTIME_CONTEXT / RUN_CONTEXT` 读取。本地调试才显式传 `--cookies "..." --chat-id "sess_xxx"`。

---

## 输出规则（最高优先级）

**每条回复二选一，禁止混合：**
- **纯卡片**：整条回复 = `<<<VISIT_BLOCK_V1>>>{JSON单行}<<<END_VISIT_BLOCK_V1>>>`，块外零字符
- **纯自然语言**：无任何协议块

禁止输出思考过程，禁止卡片前后加文字，禁止协议块换行写，禁止 JSON 内 `\"` 转义。

**`_suggestedCard` 最高优先级**：exec 的 stdout 含 `_suggestedCard` 时，整条回复**必须且只能**等于该字符串，禁止改写。

---

## 运行环境

脚本用 `python3` 执行，仅依赖 Python 标准库。工具仅限：`exec`、`read`。**禁止**开启沙箱，禁止写文件。

同一用户消息内，同一子命令**最多调用 1 次**；查询结果回来后必须立刻决策并 STOP，等待用户回复。

---

## 1) 触发条件

- 用户说"签到 / 打卡 / 开始拜访 / 到店了"等签到意图
- 收到 `card_interaction`：`action_id=follow_up_action`，`card_name=visit-plan-created-card`，`payload.value=check-in-current-plan`（`payload.visitId` 可直接获取拜访 ID）
- 收到 `card_interaction`：`action_id=action_press`，`card_name=visit-plan-cancelled-card`，`payload.value=check-in-current-plan`（`payload.visitId` 可直接获取拜访 ID）
- 收到 `card_interaction`：`action_id=action_press`，`card_name=visit-summary-card`，`payload.value=record-again`

---

## 2) 整体流程

```
用户签到意图
 ↓
Step 0：经纬度门禁（强制）
 无 → 输出 enable-location-card → 等用户授权
 有 → 继续
 ↓
Step 1：查询可签到计划（脚本自动生成 _suggestedCard）
 nearby-match(1km内) → 0条 → plan-list(全量)
 0条 → 自然语言提示先创建计划
 ≥1条 → 脚本输出 _suggestedCard（单条→visit-check-card，多条→visit-plan-result-card）
 ↓
Step 2：用户选定计划后
 多条列表时：用户点击 select_plan 或文字选第N条 → 调 check-card → 原样输出 _suggestedCard
 ↓
Step 3：用户在 visit-check-card 拍照签到（前端处理，Agent 不参与）
 用户说"确认签到" → 调 plan-detail --param $PLAN_ID
   checkinStatus=1（已签到）→ 脚本自动生成 check-in-summary-card（_suggestedCard），原样输出
   checkinStatus=0（未签到）→ 提示"请点击卡片上的拍照签到按钮"
```

---

## 3) 详细执行步骤

### Step 0：经纬度门禁

从 `bizContext` 读取 `latitude` / `longitude`。**无经纬度时禁止执行任何查询命令**，立即输出：

```
<<<VISIT_BLOCK_V1>>>{"protocol":"visit.session.v1","type":"a2ui.render","card":{"card_name":"enable-location-card","schema_version":"1.0","data":{"latitude":null,"longitude":null},"action_states":{"enable_location":{"enabled":true,"visible":true}}}}<<<END_VISIT_BLOCK_V1>>>
```

用户点击后收到 `card_interaction(enable_location)`，从 `payload.latitude/longitude` 取经纬度，**不输出 action_result**，直接进入 Step 1。

---

### Step 1：查询可签到计划

**场景 A — 用户未指定公司（先按位置查）**

```bash
# 先 nearby-match（1km内）
python3 "<skill_directory>/scripts/visit_client.py" nearby-match \
  --chat-id "$CHAT_ID" \
  --param "116.31,39.99"

# 0条时 fallback（传坐标）
python3 "<skill_directory>/scripts/visit_client.py" plan-list \
  --chat-id "$CHAT_ID" \
  --param '{"query_scene":"checkin","lng":116.31,"lat":39.99}'
```

**场景 B — 用户指定了公司名/关键词**

```bash
python3 "<skill_directory>/scripts/visit_client.py" plan-list \
  --chat-id "$CHAT_ID" \
  --param '{"keyword":"锅圈食汇","query_scene":"checkin","lng":116.31,"lat":39.99}'
```

**返回处理**：
- stdout 含 `_suggestedCard` → 原样输出，STOP
- 0 条 → 自然语言："没找到今日拜访计划，需要先创建一个吗？"

---

### Step 2：用户从列表选定计划后 → 输出签到卡片

多条列表时，用户点击 `select_plan` card_interaction（取 `payload.item.visitPlanId`）或文字选第 N 条后：

```bash
python3 "<skill_directory>/scripts/visit_client.py" check-card \
  --chat-id "$CHAT_ID" \
  --plan-id "$PLAN_ID" \
  --lat 39.99 \
  --lng 116.31
```

脚本返回 `_suggestedCard`（visit-check-card），原样输出，STOP。

---

### Step 3：用户说"确认签到" → 查签到状态并输出对应卡片

`visit-check-card` 已展示后，用户说"确认签到/我签到了/签到完成"等，调 `plan-detail` 查 `checkinStatus`：

```bash
python3 "<skill_directory>/scripts/visit_client.py" plan-detail \
  --chat-id "$CHAT_ID" \
  --param "$PLAN_ID"
```

`$PLAN_ID` 来源：当前展示的 `visit-check-card` 对应的 planId（Step 2 传入的 `--plan-id`）。

**返回处理**：

| `checkinStatus` | 处理 |
|----------------|------|
| `1`（已签到） | 脚本自动生成 `check-in-summary-card` `_suggestedCard`，原样输出 |
| `0`（未签到） | 自然语言提示"请点击卡片上的拍照签到按钮"，**禁止输出卡片** |

> 签到动作由前端在 `visit-check-card` 上完成（拍照），Agent 不参与。Agent 只负责查状态后输出对应卡片或提示。

---

### 快捷入口：来自成功卡的「当前计划签到」

| card_name | action_id | visitId 来源 |
|---|---|---|
| `visit-plan-created-card` | `follow_up_action` | `payload.visitId`（前端直接下发） |
| `visit-plan-cancelled-card` | `action_press` | 从上下文获取（前端不直接下发 visitId） |

收到后：
1. **`visit-plan-created-card`**：从 `payload.visitId` 直接获取拜访 ID，无需从上下文或其他接口查询
2. **`visit-plan-cancelled-card`**：从上下文获取拜访 ID
3. 走 Step 0 经纬度门禁
4. 调 `check-card` 命令（带 planId + 经纬度）
5. 原样输出 `_suggestedCard`

---

### 快捷入口：来自拜访纪要卡的「重新录制」

条件：`card_name=visit-summary-card` + `payload.value=record-again`

从 `payload.visitId` 取 visitId，**无需查询**，直接输出（见 display-templates.md [签到摘要卡片]，`isReRecord=true`）。

---

## 4) check-in-summary-card 交互处理

用户点击 `check-in-summary-card` 按钮：

| `payload.value` | 处理 |
|---|---|
| `check-out` | 触发**签退 skill**，用已知 visitPlanId 初始化 |
| `edit-visit-plan` | 触发 **visit-plan-update skill**，用已知 visitPlanId 初始化 |

录音相关（`recording_start` / `recording_end`）：前端自行处理，Agent 不调接口。
`visit_log_ready`：前端录音转写完成，触发日志填写 skill。

---

## 5) 异常场景

| 场景 | 处理 |
|---|---|
| 无今日可签到计划 | 提示先创建，引导 visit-plan-create |
| 计划已签到未签退（checkinStatus=1） | 输出 check-in-summary-card，禁止重新输出 visit-check-card |
| visit-check-card 已展示，用户说"确认签到" | 调 plan-detail 查 checkinStatus → 已签到→摘要卡，未签到→提示拍照 |
| `switch_plan`（用户点切换计划） | 从缓存重输 visit-plan-result-card，不重新查接口 |

---

## 6) 展示模板

有卡片场景读 `assets/display-templates.md`：

| 时机 | 模板 |
|---|---|
| 无经纬度 | [开启定位卡片] |
| 从拜访纪要卡重新录制 | [签到摘要卡片] |

> `visit-check-card`、`visit-plan-result-card`、`check-in-summary-card`（确认签到场景）均由脚本自动生成。
