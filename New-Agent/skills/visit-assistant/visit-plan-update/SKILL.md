---
name: visit-plan-update
description: AI 拜访助手—修改拜访计划。搜索计划 → 确认卡片（强制门禁） → 用户确认后提交。
---

> **【违反即本轮输出作废重写】** — 回复正文按照 `assets/display-templates.md` 里 `[ambiguous 候选]` 正确模板或卡片协议块输出，不能有任何其他字符。
>
> ## ⛔ 输出前强制自检（每次发送前逐步执行，不得跳过）
>
> 1. **取**回复第一个字符 → 必须是 `找`（模板 A/B/C）或 `请`（模板 D）或 `<`（卡片），否则**删掉整条重写**
> 2. **扫描**全文是否含黑名单词（`pendingAmbiguousSlot` / `ambiguous` / `visit_object_info` / `plan_visit_address` / `customer_type` / `candidates` / `slot` / `FSM` / `PENDING` / `CONFIRMING` 等）→ 命中任一**删掉整条重写**
> 3. **检查**是否有 `pendingAmbiguousSlot 为 xxx，需要先展示` / `根据 skill 指令` / `现在处理这个槽位` / `状态为 CONFIRMING` / `已确认槽位` 等推理说明 → 有则**删掉整条重写**
> 4. 以上全部通过 → 才允许发送

# 修改拜访计划

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

## Skill 加载强制规则（最优先执行）

> **🔴 强制前置 read（违反即本轮输出作废重写）**：
> - **无条件**：进入本 skill 后，**第一件事**就是 `read` 本 SKILL.md 和 `assets/display-templates.md`，**两个文件必须同步 read，禁止只读一个**
> - **禁止跳过**：即使你"感觉信息够用""用户已提供所有槽位""上下文已有 SKILL.md 内容"，也**必须**重新 read 两个文件，防止记忆偏差
> - **执行顺序**：read SKILL.md → read display-templates.md → 然后才允许执行脚本（state/batch-resolve 等）
> - **禁止先执行后补读**：禁止"先 batch-resolve 看看结果，再根据结果决定要不要 read display-templates.md"——必须在任何 exec 之前完成所有 read

每次收到消息时，先做 2 项检查：

1. **强制 read**（每轮消息开始时，无条件执行）：
   - `read` 本 SKILL.md
   - `read` `assets/display-templates.md`
   - 两个文件**必须同步 read**，禁止只读一个；禁止跳过；禁止先执行后补读
2. 消息含 `<<<VISIT_BLOCK_V1>>>`（写操作 Prompt）？**有** → 定位到「写操作快速路由」表格按 `card_name` + `action_id` 匹配路径执行，**不得从 Step 0 开始**；**没有** → 从 Step 0 开始

---

## 输出互斥规则

**每条回复二选一，严禁混合**：
- **模式 A 纯卡片**：整条回复 = 一个 `<<<VISIT_BLOCK_V1>>>{...}<<<END_VISIT_BLOCK_V1>>>` 协议块，块外零字符，三者必须在同一行
- **模式 B 纯自然语言**：整条回复无任何协议块

以下场景强制模式 A，禁止用自然语言替代：
- 候选计划列表（`visit-plan-result-card`）
- 修改计划卡片（`edit-visit-plan-card` / `confirm-edit-visit-plan-card`）
- 写操作返回（`a2ui.action_result`）+ 跟进卡
- **ambiguous 候选展示除外**（必须用自然语言，禁止输出卡片）

---

## `_suggestedCard` 原样输出规则（红线）

脚本执行后 stdout 中的 `_suggestedCard` 字段是**已构建好的完整协议块字符串**，必须遵守：
- **100% 原样输出**：整条回复 = `_suggestedCard` 字符串本身，块外零字符
- **禁止增删改任何字符**：包括多余的 `}`、空格、换行，一个字符都不能改
- **禁止手动拼卡片**：脚本已处理所有字段转换（visitPurposeLabel、accompanyUserName、visitObjectInfo 等），无需也不得重新构造
- **禁止包裹 Markdown**：不得用 ``` 包裹，不得加"修改计划卡片如下"等说明文字
- **逐字符复制**：从 stdout `_suggestedCard` 字段值第一个字符 `<<<VISIT_BLOCK_V1>>>` 开始，到最后一个字符 `<<<END_VISIT_BLOCK_V1>>>` 结束，中间每一个字符都要原样复制，禁止任何重写、格式化、补全、美化

### `}` 数量限制（输出前强制自检，违反即失败）

`edit-visit-plan-card` 和 `confirm-edit-visit-plan-card` 协议块末尾的 `}` 数量**固定为 4 个**（`}}}}`），对应 4 层嵌套闭合：`data/action_states` → `card` → root。

**脚本生成的 `_suggestedCard` 末尾一定是 `}}}}<<<END_VISIT_BLOCK_V1>>>`（4 个 `}`），不可能多也不可能少。** 如果你输出时发现末尾不是 4 个 `}`，说明你在复制过程中多加/少加了字符，必须立即纠正。

**输出前必须数一遍**：协议块末尾 `<<<END_VISIT_BLOCK_V1>>>` 前的 `}` 数量。

| 协议块末尾 | 判定 | 处理 |
|------------|------|------|
| `}}}}<<<END_VISIT_BLOCK_V1>>>` | ✅ 正确（4 个） | 原样输出 |
| `}}}}}<<<END_VISIT_BLOCK_V1>>>` | ❌ 多 1 个（5 个） | 删掉多余的 `}`，改为 4 个 |
| `}}}<<<END_VISIT_BLOCK_V1>>>` | ❌ 少 1 个（3 个） | 从 `_suggestedCard` 原文重新复制，禁止自行增减 |

**最常见违规**：在 `_suggestedCard` 末尾 `}}}}<<<END_VISIT_BLOCK_V1>>>` 后多加一个 `}` 变成 `}}}}}<<<END_VISIT_BLOCK_V1>>>`，导致 JSON 不合法、前端解析失败。

> 脚本 `_suggestedCardRule` 字段已标注正确的 `}` 数量，输出前对照检查。**记不住 4 个就数 `}}}}` 的竖线，4 条竖线 = 4 个 = 正确。**

### 🔴 内部术语强制扫描（输出前逐字扫描，违反即本轮输出作废重写）

**每次生成回复后、发送前，必须对整条回复逐字扫描以下词汇。发现任何一个 → 立即删除整条回复重写，禁止放行：**

`pendingAmbiguousSlot` / `ambiguous` / `batch-resolve` / `candidates` / `candidatesJson` / `fsmState` / `waitingForList` / `slotName` / `slot` / `raw` / `valueJson` / `visit_object_info` / `visit_object_type` / `plan_visit_address` / `visit_purpose` / `customer_type` / `poiId` / `modifyFields` / `confirmed` / `ok` / `fail` / `FSM` / `PENDING` / `FILLING` / `CONFIRMING` / `DONE` / `meeting_project` / `ad_customer` / `agent` / `provider` / `store`（作为类型枚举值时）/ `已分析共` / `需先解决` / `状态为` / `槽位`

> **扫描执行时机**：写完回复 → 扫描 → 命中 → 删整条重写 → 再扫描 → 直到零命中 → 才允许发送。

---

## 执行约束

### 运行环境（New-Agent）

> **不要开启沙箱（Sandbox）**
>
> 本 Skill 只做 拜访服务 HTTP 接口调用（读状态 / 槽位解析 / 查询 / 修改），**不读写工作区文件，不依赖沙箱落盘**。
>
> - **禁止**配置或调用：`sandbox_create`、`sandbox_fs_write`、`sandbox_fs_edit`、`sandbox_fs_create_dir`、`sandbox_fs_delete_dir`、`workspace_read`、`sandbox_exec_run`
> - Agent / profile 的 toolAllowlist **不要**包含上述沙箱工具
> - 需要的工具仅限：`exec`、`read`（可选：`get_runtime_context`；若走平台 HTTP 再加 `fetch`）
>
> 脚本用 **`python3`** 执行（不要用 `uv`），且**仅依赖 Python 标准库**，无第三方包。

### 禁止空转 exec

> 读完查询类脚本后，**必须立刻决策并 STOP**，禁止同轮或无新用户输入时反复 `exec`。

---

## 1. 执行流程

> ### 整体流程概览
>
> ```
> 收到修改意图
>  ↓
> 含 <<<VISIT_BLOCK_V1>>> 写操作回调？→ 直接 Step 3（direct-update）
>  ↓（否，正常流程）
> Step 0：用户说了改什么？（记住这个判断，选计划后据此分叉）
>  ├─ 否（"帮我改一下"）→ 路径 A
>  └─ 是（"改地址到重庆"/"把营业执照改为XX"）→ 路径 B
>  ↓
> Step 1：plan-list 查询候选计划 → 输出 visit-plan-result-card，等用户选定
>  ↓
> 选定计划后按 Step 0 判断分叉：
>  路径 A → Step 2A：plan-card 生成 edit-visit-plan-card STOP
>         → 用户点确认 → Step 3 路径 A
>
>  路径 B → Step 2B：state 初始化 → batch-resolve 收集槽位（循环至全部 ok）
>                → build-confirm-card 生成 confirm-edit-visit-plan-card STOP
>         → 用户点确认 → Step 3 路径 B
>  ↓
> Step 3：direct-update → action_result + 修改成功跟进卡 STOP
> ```

> ### 最高优先级：写操作快速路由
>
> **每次收到用户消息，第一件事是检查是否为卡片写操作（两条路径格式相同）：**
>
> | 识别条件 | 直接跳到 |
> |---------|---------|
> | 自然语言 Prompt 中含 `edit-visit-plan-card` + `confirm_edit_visit_plan` + `本次操作 data 是：` | **Step 3 路径 A** |
> | 自然语言 Prompt 中含 `confirm-edit-visit-plan-card` + `confirm_edit_plan` + `本次操作 data 是：` | **Step 3 路径 B** |
>
> **绝对禁止**：收到上述写操作后走 state / batch-resolve / 槽位收集。前端卡片已包含所有字段，直接提交即可。
> **反向禁止**：如果当前消息**不包含**上述关键词，**绝对禁止进入 Step 3 调用 `direct-update`**。
> 槽位收集阶段的所有槽位 ok ≠ 可以提交，必须先输出确认卡，等前端回调才能进 Step 3。
> 两条路径 Prompt 格式完全一致，data 都是嵌套结构（含 `updatedData`/`originalData`）。

---

### Step 0：判断用户意图（两条路径的分叉点）

> **绝对禁止**：不论用户有没有指明具体计划，都**必须立即进入下方路径，不得先反问用户"您想改哪条计划"**。
> - 用户只说"我要修改拜访计划"/"帮我改一下" → **立刻调 `plan-list` 查全部可修改计划**，输出列表卡片让用户选择
> - 只有用户给了具体 keyword 或明确字段时才带参查询
>
> **严禁输出类似"请问您想修改哪条拜访计划"、"您可以告诉我如：帮我改一下XX客户"等引导性文字。** 直接查询，展示结果。

收到修改意图后，**先判断用户是否指明了要改哪些字段**：

| 用户说法 | 判断 | 走哪条路径 |
|---------|------|----------|
| "我要修改拜访计划" / "帮我改一下计划" / "修改拜访" | 未指明哪个计划 | **路径 A** → Step 1A（无 keyword，立即查全部可修改计划， 禁止先反问） |
| "帮我改一下铭诚星汇的计划" | 模糊，未说改什么 | **路径 A** → Step 1A（带 keyword） |
| "把铭诚星汇7月20号的计划拜访目的改成新品推广" | 明确，说了改什么 | **路径 B** → Step 1B |
| "改一下这个计划的地址和时间" | 明确，说了改什么（多字段）| **路径 B** → Step 1B |

---

### Step 1A：路径 A — 模糊修改（用户未说改什么）

**1A-1. 查询候选计划**：

- **用户说了哪个计划**（如"帮我改一下铭诚星汇的计划"）→ 带 keyword 查询：

```bash
python3 "<skill_directory>/scripts/visit_client.py" plan-list \
 --keyword "铭诚星汇" --plan-visit-date "2026-7-21" --limit 10
```


- **用户未指明哪个计划**（如"我要修改拜访计划"）→ 不传 keyword，查全部可修改计划：

```bash
python3 "<skill_directory>/scripts/visit_client.py" plan-list \
 --limit 20
```


> `plan-list` 过滤参数（均可选）：`--keyword` / `--plan-visit-date`（yyyy-M-d）/ `--visit-object-type` / `--visit-type` / `--visit-mode`
> 脚本自动过滤可修改状态的计划，无需额外传状态。

返回后：
- **无论 1 条或多条**：脚本已在 stdout 的 `_suggestedCard` 字段生成好 `visit-plan-result-card`

两种情况均**直接将 `_suggestedCard` 字符串作为整条回复输出（块外零字符）**，无需手动拼卡片。

> **禁止跳过候选展示**：即使 `plan-list` 只返回 1 条计划，也**必须先输出 `visit-plan-result-card` 让用户确认选定**，禁止直接跳到 `edit-visit-plan-card`。用户说"修改长沙米朵那个"时需要先确认这是他要改的那条计划。
>
> **禁止用自然语言列出候选计划**：`plan-list` 返回结果后，**必须**输出 `_suggestedCard` 卡片，禁止用自然语言文字罗列计划。

**1A-2. 用户选定计划**：

| 方式 | 处理 |
|------|------|
| 点击卡片条目（`a2ui.card_interaction`, `action_id: select_plan`） | 取 `payload.item.visitPlanId` 作为 `$PLAN_ID` |
| 文字说"第一条" | 从缓存列表匹配，取对应 visitPlanId |

> **路径分叉判断（选定计划后立即执行）**：
> - **若首轮消息已包含修改字段意图**（如"把营业执照改为XX"/"改地址到重庆"）→ 走 **路径 B**，直接跳 **Step 2B**（state → batch-resolve → build-confirm-card），**禁止走 plan-card 输出 edit-visit-plan-card**
> - **若首轮消息只说改计划未指明字段**（如"帮我改一下"/"修改长沙米朵那个"）→ 走 **路径 A**，继续 **1A-3** 输出 `edit-visit-plan-card`

**1A-3. 立即输出 `edit-visit-plan-card`**（仅路径 A 执行）：

调 `plan-card` 脚本按 planId 生成卡片，直接输出 `_suggestedCard`（块外零字符）：

```bash
python3 "<skill_directory>/scripts/visit_client.py" plan-card \
 --plan-id "$PLAN_ID"
```

脚本内部查详情 → 注入 `accompanyUserName` → 生成 `_suggestedCard`，**原样输出即可，无需手动填模板**。

> 不需要调用 state / batch-resolve，`edit-visit-plan-card` 的数据直接来自计划记录。

用户在卡片里修改完毕后点击确认 → 前端以**自然语言 Prompt** 发给 Agent（与路径 B 格式一致），含 `card_name: edit-visit-plan-card`, `action_id: confirm_edit_visit_plan`, data 嵌套结构（含 `updatedData`/`originalData`/`modifyFields`）→ 跳 **Step 3 路径 A**。

---

### Step 1B：路径 B — 精准修改（用户指明了改什么）

**1B-1. 查询候选计划**（与路径 A 相同，输出 `visit-plan-result-card`）

**1B-2. 用户选定计划** → 取 `$PLAN_ID`，**禁止走 plan-card / edit-visit-plan-card**，直接进入 **Step 2B**（state → batch-resolve → build-confirm-card）

---

### Step 2B：路径 B — 槽位收集（state + batch-resolve 循环）

用户说了要改哪些字段，通过 FSM 槽位机制逐步解析每个要改的值。

> **修改为 PATCH 语义**：用户说改哪个就提交哪个，无需填全部字段。**用户未明确说要改的字段（尤其 `visitObjectType`），一律保持原值，禁止自行推断修改**。

> ** 陪访人（accompany_user_name）不走槽位，禁止放入 batch-resolve items！** 陪访人走独立快速路径（见下方 [陪访人修改快速路径](#陪访人修改快速路径)）。若用户说改陪访人，不要调 `batch-resolve`，直接走快速路径。

以下为可修改的槽位列表（**不含 accompany_user_name**）：

| 槽位名 | 触发条件 | 值举例 | 说明 |
|--------|---------|--------|------|
| `visit_type` | 用户说改拜访类型时 | `"本地"/"出差"` | local/travel；改为出差时 `travel_apply_no` 变为必填 |
| `visit_object_type` | 用户**明确说了**改拜访对象类型时 | `"广告客户"` | ad_customer / agent / store / provider；|
| `visit_object_info` | 用户说改客户名/门店时直接提交 | `"新巴克咖啡"` / `"3005970399235150104"` | **营业执照类**（ad_customer/agent/provider）：传用户原文；**门店类**（store）：首次传原文，ambiguous 后必须传候选的 `poiId`（禁止传名称）；|
| `visit_purpose` | 用户说改拜访目的时 | `"customer_maintenance"` | **必须先 `read references/params.md` 再追问，禁止凭记忆编造选项**；从枚举列表选 code 值提交 `raw`，展示给用户时用中文；用户问"目的有哪些"时也必须读 params.md 展示官方选项 |
| `plan_visit_time` | 用户说改拜访时间时 | `"明天10点"` / `"明天"` / `"7月1日"` | 自行解析为标准日期格式后提交（纯日期如 `2026-08-13`，系统自动补 09:00:00；含时段如 `2026-08-13 10:00:00`）；**禁止因时间不含时分而追问用户** |
| `plan_visit_address` | 用户说改拜访地址时 | `"重庆"` | 用户原文；**可能返回 ambiguous（多个地址候选），需用户选择后重提交** |
| `travel_apply_no` | **仅** `visit_type=travel`（出差）时 | 脚本查列表后取得的单号 | 出差必填；**禁止让 Agent 把"查看"等关键词当 raw 传入**；单号来源：① 脚本 `travel-apply-list` 返回列表后用户选定 ② 用户通过卡片填写后回调 |
| `customer_type` | **仅** visit_object_type=ad_customer 或 store | **固定二选一：`"新客"` / `"老客"`** | 详见下方 **customer_type 规则** |
| `ad_is_target` | **仅** ad_customer + 新客 + `waitingForList` 出现 `ad_is_target` | `"是"/"否"` | 详见下方 **ad_is_target 规则** |

> ** `accompany_user_name`（陪访人）禁止走本表槽位填充！** 禁止把 `accompany_user_name` 放入 `batch-resolve` 的 items。陪访人走独立快速路径，详见下方 [陪访人修改快速路径](#陪访人修改快速路径)。

> **门店场景约束**：① ambiguous 时 `raw` 只能传 `poiId`（详见 `assets/display-templates.md` [ambiguous 候选]）；② 门店确认后禁止把 `poiAddress` 自动填入 `plan_visit_address`（地址会自动回填）。

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

### 陪访人修改快速路径

当用户明确说"把陪访人改为 XX"（只改陪访人，不改槽位字段）时，**不走 FSM batch-resolve**，直接：

> 1. 调 `search-accompany-user --keyword "XX"` 搜索新陪访人
> 2. 若唯一候选 → 直接取其 `avatar`/`userName`/`userCode`；若多候选 → 自然语言展示让用户选一次
> 3. 调 `build-confirm-card` 生成确认卡，脚本自动拉原计划 + 合并陪访人覆盖值：

```bash
python3 "<skill_directory>/scripts/visit_client.py" build-confirm-card \
  --plan-id "$PLAN_ID" \
  --modify-fields "accompanyUserName" \
  --accompany-user-name '["{\"avatar\":\"...\",\"userName\":\"连杰\",\"userCode\":\"lianjie03\"}"]'
```

> 4. `--accompany-user-name` 格式：`string[]` JSON 数组，每项为 JSON 字符串（含 `avatar`/`userName`/`userCode` 三个字段）
> 5. 脚本自动将 `accompanyUserName` 加入 `modifyFields`，覆盖 `updatedData.accompanyUserName`，生成 `_suggestedCard`（`confirm-edit-visit-plan-card`）
> 6. 原样输出 `_suggestedCard`，用户点卡确认 → `direct-update`

### 混合修改场景（槽位字段 + 陪访人）

当用户同时修改**槽位字段和陪访人**时（如"把营业执照改为XX，陪访人改为YY"）：

> 1. 槽位字段走正常路径 B 流程：`state` → `batch-resolve` → 等待所有槽位 `ok`
> 2. 陪访人走快速路径：`search-accompany-user --keyword "YY"` 搜索新陪访人
> 3. 构建确认卡时，**传入 `--accompany-user-name` 参数**，脚本会自动获取槽位值，并将陪访人覆盖值合并进 `updatedData`：

```bash
python3 "<skill_directory>/scripts/visit_client.py" build-confirm-card \
  --plan-id "$PLAN_ID" \
  --modify-fields "businessLicense,accompanyUserName" \
  --accompany-user-name '["{\"avatar\":\"...\",\"userName\":\"连杰\",\"userCode\":\"lianjie03\"}"]'
```

> 4. 脚本自动获取结构化 `state.slots`（含 `valueJson` 格式），无需手动传入
> 5. `--accompany-user-name` 格式：`string[]` JSON 数组，每项为 JSON 字符串（含 `avatar`/`userName`/`userCode` 三个字段）
> 6. 脚本自动将 `accompanyUserName` 加入 `modifyFields`，并覆盖 `updatedData.accompanyUserName`

**2B-1. 初始化 FSM 状态**：

用户选定计划拿到 `$PLAN_ID` 后，**必须先调 `state --plan-id` 初始化会话**。

```bash
python3 "<skill_directory>/scripts/visit_client.py" state \
 --chat-id "$CHAT_ID" --plan-id "$PLAN_ID"
```


> update 场景用 `planId` 标识要改哪个计划，**不存在 sessionCreatedAt 概念**。
> - `state` 命令：只传 `--plan-id`，不需要 `--session-created-at`
> - `batch-resolve` 命令：只传 `--plan-id`，不需要 `--session-created-at`
> - `direct-update` 命令：只传 `--plan-id`，不需要 `--session-created-at`

**2B-2. 批量提交用户说的修改值**：

只提交用户指明要改的字段，未提到的字段不提交。**`visitObjectType` 不得提交——除非用户明确说了改类型**（如"改成广告客户"/"换成门店"），否则 `visitObjectType` 始终保持原计划值，batch-resolve 不传此槽位。

```bash
python3 "<skill_directory>/scripts/visit_client.py" batch-resolve \
 --chat-id "$CHAT_ID" \
 --plan-id "$PLAN_ID" \
 --items '[{"slot_name":"plan_visit_address","raw":"重庆"}]'
```


> update 场景 `batch-resolve` **必须带 `--plan-id`**。不需要 `--session-created-at`。

**2B-3. 处理 batch-resolve 响应**：

| status | 处理 |
|--------|------|
| `ok` | 该字段已写入。若还有未 ok 的字段 → 继续提交下一个字段；若**所有字段均已 ok** → 进入 Step 2B-4 检查，再进入 Step 2B-5 构建确认卡 |
| `ambiguous` | **展示候选列表让用户选择**（见下方 ambiguous 处理流程） |
| `fail` | 转述 `reason`，请用户换个说法 |

**2B-3a. ambiguous 处理流程**：

`batch-resolve` 返回某个 slot 的 `status=ambiguous` 时，`results` 里该 slot 会有 `candidates` 数组。**展示规则详见 `assets/display-templates.md` [ambiguous 候选]**。

> **🔴 输出 ambiguous 候选前的硬门禁（违反即本轮输出作废重写）**：
> - 输出 ambiguous 候选前，**必须先 `read` `assets/display-templates.md`**，定位到 `[ambiguous 候选]` 章节，按对应模板（A/B/C/D）照填
> - **禁止凭记忆拼候选格式**：即使你记得模板内容，也必须先 read 再输出，防止格式记错
> - **禁止跳过 read 直接输出**：即使你上一轮刚读过，本轮 ambiguous 仍需重新 read（避免上下文压缩后记忆偏差）
> - **read 后输出检查**：输出完候选后，对照模板检查首行、末行、候选行格式、序号、独占一行是否完全一致，不一致则整条重写

1. 用**自然语言**列出候选（带序号）， **禁止输出卡片**。具体格式按 `display-templates.md` 模板 A/B/C/D 照填，禁止自拟格式、禁止自拟首行末行、禁止加 `---` 等分隔线

2. 用户回复序号（如"1"或"第一个"）→ 从 `candidates` 取对应项，**用候选的完整可读名称作为 `raw` 重新提交**，**禁止传序号、禁止传部分字段**：

> ** 地址候选重提交规则**：
> - 用户选"2" → 从 `candidates[1]` 取 `poiName` 字段值，作为 `raw` 重新提交
> - **禁止**把序号 "2" 当 `raw` 传，**禁止**传 `address`/`city`/`name` 等其他字段，**禁止**只传部分字段（如只传 city）
> - **正确**：`--items '[{"slot_name":"plan_visit_address","raw":"<poiName>"}]'`（`poiName` 来自用户选中候选项的 `poiName` 字段）
> - **错误**：`--items '[{"slot_name":"plan_visit_address","raw":"2"}]'`（传序号）
> - **错误**：`--items '[{"slot_name":"plan_visit_address","raw":"北京市"}]'`（只传 city）

```bash
python3 "<skill_directory>/scripts/visit_client.py" batch-resolve \
 --chat-id "$CHAT_ID" \
 --plan-id "$PLAN_ID" \
 --items '[{"slot_name":"plan_visit_address","raw":"重庆市渝中区解放碑"}]'
```

3. 重新提交后：
- 返回 `ok` → 该 slot 填充完成，继续下一个未 ok 的字段；若所有字段均已 ok → 进入 Step 2B-4 → Step 2B-5 构建确认卡
- 返回 `ambiguous` → 再次展示候选（重复步骤 1-3）
- 返回 `fail` → 转述原因

> **只有 `status=ok` 才算填充完成**。ambiguous → 选择 → 重提交，必须循环到 ok 为止。

**2B-4. 循环终止条件**：

用户指明要改的所有字段均 `status=ok` 且无 `pendingAmbiguousSlot` → 进入 **Step 2B-5**。

**2B-5. 输出 `confirm-edit-visit-plan-card`**（写操作强制门禁，禁止跳过）：

> **写操作两阶段门禁（红线约束）**：
>
> **阶段一（本步）**：所有槽位 ok 后，**唯一允许的下一步是调 `build-confirm-card` 输出确认卡**。**禁止跳过确认卡直接调任何写接口**（`direct-update` / 自行构造 `--data` 均禁止）。
>
> **阶段二（下一步）**：输出确认卡后**立即 STOP**，等用户在卡片上点确认。只有前端回调含 `confirm-edit-visit-plan-card` + `confirm_edit_plan` 关键词时才允许进 Step 3。用户文字说"确认"/"改吧"≠ 合法入口，须回复"请点击卡片上的确认按钮"。

所有待改字段均 `status=ok` 后，调 `build-confirm-card` 脚本：

> **`--modify-fields` 取法**：按用户实际要改的内容，从 `assets/display-templates.md` [modifyFields 枚举值] 表选对应枚举值，逗号分隔传入。**禁止传槽位名**（如 `visit_object_info`），**禁止传嵌套 JSON**，必须用枚举值（如 `businessLicense`/`store`/`visitPurposeLabel`）。

**类型变更场景必读**：

| 用户意图 | `--modify-fields` 必须包含 | 说明 |
|---------|---------------------------|------|
| 改为广告客户 / 广告代理商 / 团购服务商 | `visitObjectType` **+** `businessLicense` | 改类型**必须加 `visitObjectType`**，同时加 `businessLicense` 表示改了营业执照信息 |
| 改为门店 | `store` | **只填 `store`，禁止填 `visitObjectType`**（门店类型由 `store` 驱动） |
| 不改类型，只改其他字段 | 按实际改的字段选 | 不加 `visitObjectType`，也不加 `store` |

```bash
# 改为广告代理商/广告客户/团购服务商（必须含 visitObjectType + businessLicense）
python3 "<skill_directory>/scripts/visit_client.py" build-confirm-card \
 --plan-id "$PLAN_ID" \
 --modify-fields "visitObjectType,businessLicense,planVisitAddress"

# 改为门店（只填 store，禁止填 visitObjectType）
python3 "<skill_directory>/scripts/visit_client.py" build-confirm-card \
 --plan-id "$PLAN_ID" \
 --modify-fields "store,planVisitAddress"

# 不改类型，只改地址和目的
python3 "<skill_directory>/scripts/visit_client.py" build-confirm-card \
 --plan-id "$PLAN_ID" \
 --modify-fields "planVisitAddress,visitPurposeLabel"

# 核心原则：用户修改了什么字段，就传对应的 --modify-fields 枚举值（没改的不传，改了的必传）
# 如：改了时间 + 目的 + 陪访人 → --modify-fields "planVisitTime,visitPurposeLabel,accompanyUserName"
# 如：改了差旅单号 → --modify-fields "travelApplyNo"
# 如：改了新客老客（当前 ad_customer）→ --modify-fields "customerType"
# 如：改了拜访类型为出差 → --modify-fields "visitType"
```

输出 `_suggestedCard`，**原样输出，块外零字符，STOP**。

**输出确认卡后本轮结束。** 用户点击确认 → 前端以**自然语言 Prompt** 发给 Agent（含 `confirm-edit-visit-plan-card` + `confirm_edit_plan` 关键词）→ 下一轮进入 **Step 3 路径 B**。

---

### Step 3：执行 direct-update

> 前端卡片 data 就是修改后的值，脚本直接提交，无需 state / batch-resolve / query。

#### 路径 A — `edit-visit-plan-card` + `confirm_edit_visit_plan`

前端以自然语言 Prompt 发来，data 为嵌套结构（含 `updatedData`/`originalData`/`modifyFields`）。

```bash
python3 "<skill_directory>/scripts/visit_client.py" direct-update \
 --plan-id "<Step1A $PLAN_ID>" \
 --data '<Prompt data 整体的完整 JSON 字符串>' \
 --client-action-id "$CLIENT_ACTION_ID" \
 --card-id "$CARD_ID" \
 --action-id confirm_edit_visit_plan \
 --card-name edit-visit-plan-card
```

#### 路径 B — `confirm-edit-visit-plan-card` + `confirm_edit_plan`

> **【前置校验】（强制，不满足则拒绝执行）**：
> 只有当前 Prompt 中**明确包含**以下两个关键词时，才允许执行本路径：
> - `confirm-edit-visit-plan-card`
> - `confirm_edit_plan`
    > **不满足上述条件时，本路径禁止执行。** 不得自行决定跳入，不得在槽位收集完成后直接跳入。
    > 如果当前处于 Step 2B 槽位收集阶段且所有槽位已 ok，应回到 **Step 2B-5** 输出确认卡，而非跳入本步。

```bash
python3 "<skill_directory>/scripts/visit_client.py" direct-update \
 --plan-id "<Step1B $PLAN_ID>" \
 --data '<Prompt data 整体的完整 JSON 字符串>' \
 --client-action-id "$CLIENT_ACTION_ID" \
 --card-id "$CARD_ID" \
 --action-id confirm_edit_plan \
 --card-name confirm-edit-visit-plan-card
```

> `--data` 传整体 data 即可，脚本内部自动提取 `updatedData`，无需 Agent 手动拆包。

---

**返回结果处理（两条路径相同）**：

脚本自动构建 `_suggestedCard`，包含：
- **成功时**：两块协议块（`action_result` + `visit-plan-cancelled-card`），原样输出 `_suggestedCard`（块外零字符）
- **失败时**：只含 `action_result` 一块，原样输出 `_suggestedCard`

> **禁止手工拼凑协议块**，必须原样输出脚本 `_suggestedCard`。
