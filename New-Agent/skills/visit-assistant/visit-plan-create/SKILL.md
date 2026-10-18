---
name: visit-plan-create
description: AI 拜访助手—创建拜访计划。用户表达新建/安排拜访时触发。本地/出差新建：收集槽位→create 提交→输出 visit-plan-created-card。出差场景内部会先查待补充计划，有则输出 visit-pending-card 让用户点选（由卡片直补流程接管），无则新建。「查询待填写计划/查有哪些待补充/补充差旅计划」等只读或补充查询意图，路由到 visit-pending skill。
---

> **【违反即本轮输出作废重写】** — 回复正文按照 `assets/display-templates.md` 里 `[ambiguous 候选]` 正确模板或卡片协议块输出，不能有任何其他字符。
>
> ## ⛔ 输出前强制自检（每次发送前逐步执行，不得跳过）
>
> 1. **取**回复第一个字符 → 必须是 `找`（模板 A/B/C）或 `请`（模板 D）或 `<`（卡片），否则**删掉整条重写**
> 2. **扫描**全文是否含黑名单词（`pendingAmbiguousSlot` / `ambiguous` / `visit_object_info` / `plan_visit_address` / `customer_type` / `candidates` / `slot` / `FSM` / `PENDING` / `CONFIRMING` 等）→ 命中任一**删掉整条重写**
> 3. **检查**是否有 `pendingAmbiguousSlot 为 xxx，需要先展示` / `根据 skill 指令` / `现在处理这个槽位` / `状态为 CONFIRMING` / `已确认槽位` 等推理说明 → 有则**删掉整条重写**
> 4. 以上全部通过 → 才允许发送

# 创建拜访计划

---

## ⛔ 输出唯一约束

本 skill 任何回复正文，有且只有以下两种内容之一：

1. **候选列表**：严格按 `assets/display-templates.md` 模板 A/B/C/D 之一（首行 + 候选列表 + 空行 + 末行，无其他字符）
2. **卡片协议块**：`_suggestedCard` 原样输出（`<<<VISIT_BLOCK_V1>>>...<<<END_VISIT_BLOCK_V1>>>`，块外零字符）

**违反此约束 = 本轮输出无效，必须重写**。任何分析说明、推理过程、状态描述、道歉解释一律不得出现在回复正文，全部留在思考块里。

---

## 凭据获取（必读）

> New-Agent 下 **不要** 传 `--cookies`。脚本会从已绑定的 `AGENT_RUNTIME_CONTEXT / RUN_CONTEXT` 自动读取。

**自动读取（exec 时省略鉴权参数）**：
- Cookie ← `bizContext.cookie`（也会投影到顶层 `cookie`）
- 对话 ID ← 可传 `--chat-id`；不传则用 `sessionId`

---

## 硬约束（违反会导致循环/错误）

1. **门店 ambiguous 时 `raw` 只能传 `poiId`**：从候选列表取用户选中项的 `poiId` 作为 `raw`。禁止传名称、禁止传序号、禁止自创额外字段。

2. **`visit-plan-card` / `visit-plan-created-card` 来自脚本 `_suggestedCard`**，禁止自行拼凑。其他卡片（`create-travel-info-card` 等）从 `display-templates.md` 构建，不受此限。

3. **禁止把门店 `poiAddress` 自动填入 `plan_visit_address`**：门店确认后地址会自动回填，不得自行把门店的 `poiAddress` 抄进 `plan_visit_address` 槽位。

4. ** 地址 ambiguous 时 `raw` 必须传候选的 `poiName` 字段：
   - 用户选"2" → 从 `candidates[1]` 取 `poiName` 字段值，作为 `raw` 重新提交
   - **禁止**把序号 "2" 当 `raw` 传，**禁止**传 `address`/`city`/`name` 等其他字段，**禁止**只传部分字段（如只传 city）
   - **正确**：`--items '[{"slot_name":"plan_visit_address","raw":"<poiName>"}]'`（`poiName` 来自用户选中候选项的 `poiName` 字段）
   - **错误**：`--items '[{"slot_name":"plan_visit_address","raw":"2"}]'`（传序号）
   - **错误**：`--items '[{"slot_name":"plan_visit_address","raw":"北京市"}]'`（只传 city）

---

## 🔴 输出规则（辅助约束）

### 术语黑名单（输出前逐字扫描，发现即整条重写）

`pendingAmbiguousSlot` / `ambiguous` / `batch-resolve` / `candidates` / `candidatesJson` / `fsmState` / `waitingForList` / `slotName` / `slot` / `raw` / `valueJson` / `visit_object_info` / `visit_object_type` / `plan_visit_address` / `visit_purpose` / `customer_type` / `poiId` / `confirmed` / `ok` / `fail` / `FSM` / `PENDING` / `FILLING` / `CONFIRMING` / `DONE` / `meeting_project` / `ad_customer` / `agent` / `provider` / `store`（作为类型枚举值时）/ `已分析共` / `需先解决` / `状态为` / `槽位`

> **扫描执行时机**：写完回复 → 扫描 → 命中 → 删整条重写 → 再扫描 → 直到零命中 → 才允许发送。

### `_suggestedCard` 输出规则

若最近一次 exec 的 stdout 含 `_suggestedCard`，整条回复 **必须且只能** 等于 `_suggestedCard` 字符串，禁止改写、加字、换 Markdown；不必再 `read` 模板拼卡。

---

## 加载规则（每条消息先检查）

> **🔴 强制前置 read（违反即本轮输出作废重写）**：
> - **无条件**：进入本 skill 后，**第一件事**就是 `read` 本 SKILL.md 和 `assets/display-templates.md`，**两个文件必须同步 read，禁止只读一个**
> - **禁止跳过**：即使你"感觉信息够用""用户已提供所有槽位""上下文已有 SKILL.md 内容"，也**必须**重新 read 两个文件，防止记忆偏差
> - **执行顺序**：read SKILL.md → read display-templates.md → （若 `waitingForList` 含 `customer_type`/`visit_purpose`）read `references/params.md` → 然后才允许执行脚本（state/batch-resolve 等）
> - **禁止先执行后补读**：禁止"先 batch-resolve 看看结果，再根据结果决定要不要 read display-templates.md"——必须在任何 exec 之前完成所有 read

1. **强制 read**（每轮消息开始时，无条件执行）：
   - `read` 本 SKILL.md
   - `read` `assets/display-templates.md`
   - 若 `waitingForList` 含枚举型槽位（`customer_type` / `visit_purpose`）→ 再 `read` `references/params.md`
2. **`waitingForList` 含枚举型槽位（`customer_type` / `visit_purpose`）**？→ **必须先 `read references/params.md`** 再追问，禁止凭记忆编造选项
3. 消息含 `<<<VISIT_BLOCK_V1>>>` 且卡片 `operationType="create_plan"` 或无 `operationType`？→ 直接跳 Step 5（`direct-create`），不走槽位收集
4. **消息含 `<<<VISIT_BLOCK_V1>>>` 且卡片 `operationType="supplement_plan"`（补充差旅场景）**？
   → 直接跳 [supplement_plan 提交] 流程：
   - 取 `a2uiCardData`（即前端消息卡片 JSON 的 `data` 对象），必须含 `planId`
   - 调 `python3 visit_client.py direct-fill-card --data '<a2uiCardData JSON>' --client-action-id '$CLIENT_ACTION_ID' --card-id '$CARD_ID'`
   - 脚本自动构建 `_suggestedCard`（action_result + visit-plan-created-card），原样输出；禁止自然语言确认
5. 消息含 `card_name="visit-plan-created-card"` + `payload.value="add-companion"`？→ 跳「添加陪访人流程」（→ 见 `display-templates.md`），`visitPlanId` 从 `a2uiCardData.visitPlanId` 取（卡片数据，非 payload），禁止调 `state`/`batch-resolve`
6. **消息含 `card_name="visit-pending-card"` + `action_id="view_pending_item"` + `payload.pendingType="pending-travel-plan"`**（用户点击待补充差旅计划）？
   → 直接跳 [差旅直补卡] 流程：
   - 取 `payload.visitId` 作为 `planId`
   - 调 `python3 visit_client.py supplement-plan-card --plan-id '$PLAN_ID'`
   - 原样输出 `_suggestedCard`；禁止追问、禁止自然语言交互

> `create-travel-info-card` / `create-business-license-card` 点击后前端转自然语言消息，Agent 提取值走正常 batch-resolve 即可。
> 禁止凭记忆操作，以当前读取的 SKILL.md 为准。

---

## 执行约束

### 运行环境（New-Agent）

> **不要开启沙箱（Sandbox）**
>
> 本 Skill 只做 拜访服务 HTTP 接口调用（读状态 / 槽位解析 / 查询 / 创建），**不读写工作区文件，不依赖沙箱落盘**。
>
> - **禁止**配置或调用：`sandbox_create`、`sandbox_fs_write`、`sandbox_fs_edit`、`sandbox_fs_create_dir`、`sandbox_fs_delete_dir`、`workspace_read`、`sandbox_exec_run`
> - Agent / profile 的 toolAllowlist **不要**包含上述沙箱工具
> - 需要的工具仅限：`exec`、`read`（可选：`get_runtime_context`；若走平台 HTTP 再加 `fetch`）
>
> 脚本用 **`python3`** 执行（不要用 `uv`），且**仅依赖 Python 标准库**，无第三方包。

### 禁止空转 exec

读完 `state` / `batch-resolve` 后，**必须立刻决策并 STOP**，禁止同轮或无新用户输入时反复 exec。

1. 同一用户消息内，同一子命令**最多调用 1 次**
2. **`sessionCreatedAt` — 每次发起创建流程时生成当前毫秒时间戳**：同一会话可创建多个拜访计划，每个创建流程用不同的时间戳标识。**同一创建流程内**所有命令（state/batch-resolve/create）必须带同一个 `--session-created-at` 值，不同创建流程用不同的时间戳。
3. `state` / `batch-resolve` 返回 `waitingForList` 非空 + 用户消息无新槽位 → 禁止再 exec，输出卡片/追问后等待
4. 首次建会话（PENDING + slots 空）→ 直接输出 [初始空白卡]，禁止追问
5. `batch-resolve` 返回 `results=[]` 且无 state → 自然语言告知失败，STOP
6. 仅当用户**新回复**带来可解析槽位或确认提交时，才允许再次 exec

---

## 1) 槽位定义

| 槽位名 | 触发条件 | 值举例 | 说明 |
|--------|---------|--------|------|
| `visit_type` | 每次必填 | `"本地"/"出差"` | local/travel |
| `visit_object_type` | 每次必填 | `"广告客户"` | ad_customer/agent/store/provider |
| `visit_object_info` | 用户说改客户名/门店时直接提交 | `"新巴克咖啡"` / `"3005970399235150104"` | **营业执照类**（ad_customer/agent/provider）：传用户原文；**门店类**（store）：首次传原文，ambiguous 后必须传候选的 `poiId`（禁止传名称）；|
| `visit_purpose` | 每次必填 | `"customer_maintenance"` | **必须先 `read references/params.md` 再追问，禁止凭记忆编造选项**；从枚举列表选 code 值提交 `raw`，展示给用户时用中文；用户问"目的有哪些"时也必须读 params.md 展示官方选项 |
| `plan_visit_time` | 必填 | `"明天10点"` | **用户只说日期（"今天""明天""7月1日"）不含时刻时，自行解析为标准日期格式（如 `2026-08-13`）后提交，禁止追问时段——系统会自动补 09:00:00** |
| `plan_visit_address` | 每次必填 | `"济南站北广场"` | 用户原文；**门店场景禁止把 `poiAddress` 自动填入此槽位** |
| `travel_apply_no` | visit_type=travel | 脚本自动查列表后取得的单号 | 出差必填； **禁止让 Agent 把"查看"等关键词当 raw 传入**；单号来源：① 脚本 `travel-apply-list` 返回列表后用户选定 ② 用户通过 `create-travel-info-card` 卡片填写后回调 `payload.travelApplyNo` |
| `customer_type` | **仅** visit_object_type=ad_customer 或 store | **固定二选一：`"新客"` / `"老客"`** | 详见下方 **customer_type 规则** |
| `ad_is_target` | **仅** ad_customer + 新客 + 有靶向候选时 | `"是"/"否"` | 详见下方 **ad_is_target 规则** |

> **拜访对象类型强约束**：`visit_object_info` 解析**依赖** `visit_object_type` 已确认。若用户直接说了客户名但未说对象类型，必须先追问类型（广告客户/代理商/门店/服务商），确认后再提交 `visit_object_info`。batch-resolve 时如果 `visit_object_type` 未 confirmed，不得同时提交 `visit_object_info`。

> **拜访时间强约束（禁止追问时段）**：用户说"明天""今天""后天""7月1日"等只含日期不含时分的表述时，**禁止追问"明天几点？"**。应自行将用户表述解析为标准日期格式后作为 raw 提交：纯日期（如"明天"）→ `2026-08-13`（系统自动补 09:00:00）；含时段（如"明天上午10点"）→ `2026-08-13 10:00:00`。**禁止因时间不含时分而追问用户**。

### customer_type 规则

**适用范围**：只有 `visit_object_type=ad_customer`（广告客户）或 `store`（门店）才需要填 `customer_type`。`agent`（广告代理商）和 `provider`（团购服务商）**没有** `customer_type` 字段，不得追问新老客。

**合法值**：只有两个——**`新客`**（new）和 **`老客`**（old）。**追问前必须先 `read references/params.md` 的 customer_type 枚举，禁止凭记忆。**

- 「靶向新客」**不是** `customer_type` 的选项，它是 `ad_is_target` 槽位的语义，只有广告客户选了新客后才可能追加
- 追问时固定格式：`请问该客户是新客还是老客？1. 新客 2. 老客`（用户说"新客户"即映射为「新客」）
- `batch-resolve` 返回 `ambiguous` 时，**必须原样展示 candidates 中的「新客」和「老客」**，等用户选择后再提交

**新客后续流程（仅广告客户）**：广告客户选了「新客」后，系统会检查是否有靶向候选：
- 有靶向候选 → `waitingForList` 追加 `ad_is_target`，追问「是否为靶向新客？1. 是 2. 否」
- 无靶向候选 → `ad_is_target` 不进入 `waitingForList`，无需用户选择，直接按普通新客提交
- 门店(store)即使选了新客，**也不会**出现 `ad_is_target`，不存在靶向门店概念

### ad_is_target 规则

**执行规则**：不需要自行判断是否有靶向候选——**只看 `waitingForList` 里有没有 `ad_is_target`**，有就追问，没有就不问。门店(store)、代理商(agent)、服务商(provider) 场景下 `ad_is_target` 永远不会出现。禁止在 `customer_type` 还没确认时就问「是否靶向」。

**靶向候选为空时的处理**：若用户明确说了「靶向新客」但 `batch-resolve` 返回 `status=ok` 且 `warning` 非空：**直接告知用户**该营业执照在系统靶向客户列表中暂无记录，已自动按普通新客处理。禁止提示用户"更换拜访客户"或暗示系统有问题。

---

## 2) 详细执行步骤

### Step 1：识别拜访类型

**场景 A — 用户给出泛化创建意图（如"帮我创建一个拜访计划"）**
- 不立即调用 state/batch-resolve
- **读取 `assets/display-templates.md`，使用 [初始空白卡] 模板，逐字符完整复制输出**
- 输出空白卡后 STOP，等待用户操作

**场景 B — 用户明确指定了部分/全部字段**
- 用户说"出差"/"差旅" → `visit_type=travel`，进入出差分流。
- 用户说"本地" 或不明确 → 追问「这是本地拜访还是出差拜访？」

### Step 2：出差分流

**查待补充差旅拜访计划**

**脚本**: `pending-travel-plan`

从用户消息中提取**城市关键词**（如"上海""深圳""杭州"），作为 `--param` 传入；用户未提城市时不传（返回全部）：

```bash
# 指定城市（优先）
python3 "<skill_directory>/scripts/visit_client.py" pending-travel-plan \
 --chat-id "$CHAT_ID" \
 --param "上海"

# 未提城市时
python3 "<skill_directory>/scripts/visit_client.py" pending-travel-plan \
 --chat-id "$CHAT_ID"
```

**同一差旅单号可能对应多条计划（不同拜访时间），卡片里用 `travelTime` 字段区分，用户自行点选。**

**返回后决策**：
- 脚本 stdout 含 `_suggestedCard`（total > 0）→ **直接原样转发 `_suggestedCard`**（它已是完整的 `<<<VISIT_BLOCK_V1>>>...<<<END_VISIT_BLOCK_V1>>>` 卡片协议块），块外零字符，禁止自然语言，禁止从 `total`/`items` 字段自己拼文案；用户点击卡片里的差旅条目 → 触发加载规则第6条（`card_name=visit-pending-card` + `action_id=view_pending_item` + `pendingType=pending-travel-plan`）→ `supplement-plan-card` → 用户填卡 → `direct-fill-card`
- 返回 **0 条** → 告知"未找到相关待补充差旅拜访计划，是否需要新建出差拜访？"
 - 用户说"新建" → **查差旅申请列表**：

```bash
python3 "<skill_directory>/scripts/visit_client.py" travel-apply-list \
 --chat-id "$CHAT_ID"
```

 → 返回结构（stdout JSON）：`{"total": N, "tips": "...", "list": [{"travelApplyNo":"...","cities":[...],"startTime":"...","endTime":"...","status":"..."}]}`
 → **`list` 非空（有差旅申请）**：展示列表让用户选一条，用户选定后取该条的 `travelApplyNo` 作为 `raw` 执行 batch-resolve：
 ```json
 [{"slot_name": "travel_apply_no", "raw": "<travelApplyNo>"}]
 ```
 进入 Step 3 新建出差拜访，**Step 5 调 create**
 - **`list` 为空（无差旅申请）** → 读 `assets/display-templates.md` [新建差旅信息卡] 模板，**下发 `create-travel-info-card`**， **禁止提示用户去 OA/差旅系统申请**，等待前端 `a2ui.card_interaction` 回调
 - 用户在卡片填写差旅单号后点击「创建差旅信息」，前端以 **`a2ui.card_interaction`（`action_id=create_travel_info`）** 回调，Agent 从 `payload.travelApplyNo` 取单号 → **直接传 `raw` 提交 batch-resolve**：
 ```json
 [{"slot_name": "travel_apply_no", "raw": "<travelApplyNo>"}]
 ```
 进入 Step 3 新建出差拜访，**Step 5 调 create**
 - **禁止**：不能让用户文字输入单号；不能提示"请先去 OA/差旅系统申请"；不能自己编造单号

### Step 3：循环槽位收集

> ⚡ **`pending_ambiguous_slot` 非空（不为 null/undefined/空字符串 `""`）时，禁止输出任何卡片协议块**，只输出自然语言候选列表让用户选择。
>
> 以下三种情况例外，**允许在 Step 3 中间下发卡片**：
> 1. `visit_object_info` batch-resolve 返回 `status=fail`（零结果）且 `visitObjectType ∈ {ad_customer, agent, provider}` → 下发 `create-business-license-card`
> 2. 出差流程中 `travel-apply-list` 返回空列表 → 下发 `create-travel-info-card`（见 Step 2）
> 3. `fsmState=CONFIRMING` 且 `waitingForList` 为空 → 下发最终确认卡

**每轮固定流程**：

**3.1 先读状态**

```bash
python3 "<skill_directory>/scripts/visit_client.py" state \
 --chat-id "$CHAT_ID" \
 --session-created-at "$SESSION_CREATED_AT"
```

读取 `waitingForList` / `slots` / `fsmState` / `pendingAmbiguousSlot`。

> **从 [初始空白卡] 进入时**：用户首次回复后，先初始化 state（生成 sessionCreatedAt），然后直接将用户回复中的所有槽位批量提交。

**3.2 批量提交用户消息中的槽位**

```bash
python3 "<skill_directory>/scripts/visit_client.py" batch-resolve \
 --chat-id "$CHAT_ID" \
 --session-created-at "$SESSION_CREATED_AT" \
 --items '[{"slot_name":"visit_type","raw":"本地"},{"slot_name":"visit_object_type","raw":"广告客户"}]'
```

**items 规则**：
- **严格按 `waitingForList` 顺序收集**：禁止跳过前面的槽位先填后面的（如还没确认 `visit_type` 就填 `visit_object_info`）
- 只抽 `waitingForList` 中包含的槽位，用户没提到的不要瞎猜
- 用户一次说多个槽位（"本地 广告客户 济南盘古"）→ 可以全塞入 items，**但只限 already-confirmed 前面的槽位**；如果前面的槽位有 ambiguous，后面的不应该提交（等 ambiguous 解决后再提交）
- **`visit_object_info` 门店 ambiguous 二次选择时，`raw` 必须传 `poiId`**（→ 见 硬约束第1条）
- **`plan_visit_address` 门店确认后 raw 传 `""`**（→ 见 硬约束第3条）
- `customer_type` 顺序：同时有 `visit_object_info` 和 `customer_type` 时，优先提交 `customer_type`

**3.3 处理响应**

```
响应结构（camelCase）：
{
 "results": [
 {"slotName": "...", "status": "ok/ambiguous/fail", "valueJson": "...", "candidatesJson": [...], "reason": "..."}
 ],
 "state": {
 "waitingForList": [...],
 "fsmState": "FILLING/CONFIRMING",
 "slots": {...}
 },
 "pendingAmbiguousSlot": "..."
}
```

| status | 处理 |
|--------|------|
| `ok` | 已写入；若同次响应里还有别的 ambiguous，只处理**一个**后 STOP |
| `ambiguous` | 见下方 **ambiguous 序号映射规则** |
| `fail`（普通字段） | 转述 `reason`，请用户换个说法 |
| `fail`（`visit_object_info`，且 `visitObjectType ∈ {ad_customer, agent, provider}`） | **营业执照零结果降级**：读 `assets/display-templates.md` [新建营业执照卡] 模板，**在 Step 3 中间直接下发** `create-business-license-card`；用户填写名称后点击「创建」，前端以自然语言形式发给 Agent，Agent 从中提取 `licenseName` → 再次 batch-resolve `visit_object_info=<licenseName>` → 继续 Step 3 |

#### ambiguous 序号映射规则

**展示规则详见 `assets/display-templates.md` [ambiguous 候选]**。核心约束：

> **🔴 输出 ambiguous 候选前的硬门禁（违反即本轮输出作废重写）**：
> - 输出 ambiguous 候选前，**必须先 `read` `assets/display-templates.md`**，定位到 `[ambiguous 候选]` 章节，按对应模板（A/B/C/D）照填
> - **禁止凭记忆拼候选格式**：即使你记得模板内容，也必须先 read 再输出，防止格式记错
> - **禁止跳过 read 直接输出**：即使你上一轮刚读过，本轮 ambiguous 仍需重新 read（避免上下文压缩后记忆偏差）
> - **read 后输出检查**：输出完候选后，对照模板检查首行、末行、候选行格式、序号、独占一行是否完全一致，不一致则整条重写

1. **一次只问一个槽位**：有多个 ambiguous 时，只展示 `pendingAmbiguousSlot` 的候选；解决完再处理下一个
2. **候选必须带阿拉伯数字序号**，格式固定为：`1. 名称 2. 名称 ...`。**有候选列表时，用户必须回序号**，禁止用名称选择（同名门店按名称匹配会永远命中第一个）。若用户回复的不是序号，追问「请回复序号（如 1、2、3）」
3. **选定后不再重复确认同一候选项**：该槽位 `ok` 后，转到下一个缺失/ambiguous 槽位或进入确认卡
4. 用户回复后只允许 **一次** `batch-resolve`，然后按 3.4 决策并 STOP

**3.4 每轮 state / batch-resolve 后的输出判断**

---

#### 【第0步，最高优先级】特殊槽位检查（在所有判断之前执行）

> **只要 `waitingForList` 里出现以下槽位，必须走专属流程，禁止用自然语言追问：**

| 槽位 | 必须执行 |
|------|---------|
| `travel_apply_no` | 调 `travel-apply-list`：有列表→展示让用户选→`batch-resolve(raw=单号)`；列表为空→下发 [新建差旅信息卡]`create-travel-info-card`，等回调取 `payload.travelApplyNo` |
| `visit_object_info`（且上轮 `status=fail`，类型 ad_customer/agent/provider） | 下发 [新建营业执照卡]`create-business-license-card`，等 `card_interaction(create_business_license)` 回调 |

> 上表槽位命中后直接 STOP，不再走下方决策树。

---

```
if pending_ambiguous_slot 非空:
 → 自然语言列出候选让用户选择 → STOP

else: # waitingForList 仍有缺失
 → 自然语言追问下一个缺失的必填字段 → STOP
```

**3.5 循环终止条件**

- `waitingForList` 为空 且 `fsmState=CONFIRMING` 且 `pendingAmbiguousSlot` 为空 → 进入 Step 4

> 「循环」仅指：**跨多轮用户对话**反复收集；**不是**在同一轮里连续多次 `exec`。

---

#### 候选确认后的强制推进

当用户通过**自然语言回复序号**或**卡片点击**选择了候选后：

1. **立即提取**用户选中项的**完整显示名称**
2. **必须在同轮**执行 `batch-resolve`，`raw` 传该完整名称，不得停下来输出任何中间确认语
3. **该槽位返回 `status=ok` 后**，立即按 3.4 决策逻辑推进（禁止停留）
4. **禁止行为**：候选确认后输出"已为您选择 XX"等自然语言然后停止——**必须推进**到确认卡或下一个槽位。

> **上下文丢失兜底**：如果因会话压缩导致 Agent **未看到**用户的选择回复，但当前 state 里仍有未确认槽位：**必须先调 `state`** 查询当前槽位真实状态（带 `--session-created-at`）。若 `pendingAmbiguousSlot` 已为空且槽位 `confirmed=true` → **直接视为该槽位已 ok**，按 3.4 继续推进。若 `pendingAmbiguousSlot` 仍非空 → 重新展示候选让用户选择。

### Step 4：最终确认

#### 进入条件（必须同时满足）
1. `fsmState` 为 **`CONFIRMING`**
2. `waitingForList` 为空
3. `pendingAmbiguousSlot` 为空

脚本 `state`/`batch-resolve` 返回 `_suggestedCard` 时，**直接原样输出**，禁止自然语言确认、禁止 Markdown 表格代替卡片、禁止块外任何字符。

用户要求修改字段 → 继续 batch-resolve → 再进 CONFIRMING → 再出确认。

### Step 5：用户确认后提交

> **FSM 须为 `CONFIRMING`** 才可提交

**触发方式 — 卡片点击（`a2ui.action_request`）**

前端点击 `visit-plan-card` 上的 `create_plan` 按钮时，Agent 收到的消息里包含：
- `input` 文本里的 **`本次操作 data 是：{...}`**：卡片当前所有字段值
- `input` 文本里的 **`clientActionId`** / **`target.card_id`**：脚本回传到 action_result

**5-1. 执行 direct-create**

从 `input` 里的 `data` 对象提取，直接传给脚本 `direct-create` 命令：

```bash
python3 "<skill_directory>/scripts/visit_client.py" direct-create \
 --data "<data 的 JSON 字符串>" \
 --client-action-id "$CLIENT_ACTION_ID" \
 --card-id "$CARD_ID"
```

**参数说明**（从前端 Prompt `input` 提取）：
- `--client-action-id` ← `input.clientActionId`（camelCase），原样回传
- `--card-id` ← `input.target.card_id`，原样回传

**5-2. 输出 `_suggestedCard`**

脚本自动构建 `_suggestedCard`，包含：
- **成功时**：两块协议块（`action_result` + `visit-plan-created-card`），原样输出 `_suggestedCard`（块外零字符）
- **失败时**：只含 `action_result` 一块，原样输出 `_suggestedCard`

> **禁止手工拼凑协议块**，必须原样输出脚本 `_suggestedCard`。

---

## 3) 异常场景处理

| 场景 | 处理 |
|------|------|
| 用户说"取消" | 停止流程，告知已取消 |
| `sessionCreatedAt` 丢失（上下文压缩） | 从最近 exec 的 stdout `_clientMeta.sessionCreatedAt` 或 stderr 提示取回；都无法取回时诚实告知用户「会话信息丢失，需要重新开始创建」，禁止用新时间戳重新调 state（会覆盖原流程） |
