# Visit Checkout — A2UI 卡片模板（前端验证标准）

> ⚠️ **所有输出必须完整复制对应模板，只替换 `{PLACEHOLDER}` 占位符，其余字符一律不得改变。**
> JSON 必须单行，禁止换行。
>
> 🚨 **严禁用 markdown 代码块（```）包裹协议块！** 协议块必须**单行**裸文本输出，头尾标记和 JSON 之间**不能有换行**：
> `<<<VISIT_BLOCK_V1>>>{单行JSON}<<<END_VISIT_BLOCK_V1>>>`
> SSE 原始输出**不含 `block_id` 和 `card_id`**，禁止添加这两个字段。

---

## 🚨 输出互斥原则

> **卡片和文本不能在同一次输出里混发！每次输出只能是「纯卡片」或「纯文本」二选一。**

1. **下发卡片** → 只输出协议块，**前后不附加任何文字**
2. **追问 / 说明 / 预校验失败原因** → 只输出普通文字，**不下发协议块**
3. **一次输出只能一张卡片**

---

## 卡片索引

| 卡片 | 触发时机 | 说明 |
|------|---------|------|
| `visit-plan-result-card` | 用户说"签退"，查询待签退列表后 | 展示待签退计划供选择 |
| `visit-check-card`（WAIT_CHECKOUT） | 用户选定计划 + 预校验**通过**后 | 待签退确认卡片，用户点"确认签退" |
| `visit-check-card`（CHECKED_OUT） | submit_checkout **成功**后 | 已签退结果卡片 |
| 文字展示（无卡片） | 预校验**失败** / 无候选计划 / 签退失败 / Cookie 过期 / 任何追问 | 纯文本 |

---

## [候选计划列表] `visit-plan-result-card`

用户主动说"签退"时，查询待签退列表后输出，让用户选择。

```
<<<VISIT_BLOCK_V1>>>{"protocol":"visit.session.v1","type":"a2ui.render","card":{"card_name":"visit-plan-result-card","schema_version":"1.0","data":{"total":{TOTAL},"hasMore":false,"items":[{ITEMS_JSON}]},"action_states":{"select_plan":{"enabled":true,"visible":true}}}}<<<END_VISIT_BLOCK_V1>>>
```

**每条 item JSON 结构**：
```json
{"visitPlanId":"{PLAN_ID}","customerName":"{CUSTOMER_NAME}","customerTypeLabel":"{TYPE_LABEL}","planVisitTimeLabel":"{TIME_LABEL}","planVisitAddress":"{ADDRESS}"}
```

**字段映射**（来自 `query_visit_plans.py` 输出）：
- `visitPlanId` ← `plan.visitPlanId`
- `customerName` ← `plan.visitObjectName`
- `customerTypeLabel` ← `plan.visitObject` 枚举转中文（见末尾枚举表）
- `planVisitTimeLabel` ← `plan.actualVisitTime`
- `planVisitAddress` ← `plan.planVisitAddress`（无则 `""`）

---

## [待签退确认] `visit-check-card`（checkinStatus = WAIT_CHECKOUT）

**触发时机**：用户选定计划后，调用 `pre_check_checkout.py` 预校验，**校验通过（canCheckout=true）** 时输出此卡片，让用户点击"确认签退"。
**校验失败（canCheckout=false）时不输出此卡片，只返回纯文本原因。**

```
<<<VISIT_BLOCK_V1>>>{"protocol":"visit.session.v1","type":"a2ui.render","card":{"card_name":"visit-check-card","schema_version":"1.0","data":{"visitPlanId":"{VISIT_PLAN_ID}","planVisitTime":{PLAN_VISIT_TIME},"planVisitAddress":"{PLAN_ADDRESS}","visitObjectInfo":{"licenseName":"{CUSTOMER_NAME}"},"clientName":"{CLIENT_NAME}","visitTarget":{"label":"{TARGET_LABEL}","value":"{TARGET_VALUE}"},"checkinStatus":"WAIT_CHECKOUT","currentAddress":"{CURRENT_ADDRESS}","latitude":{LATITUDE},"longitude":{LONGITUDE},"signInAddress":"{SIGNIN_ADDRESS}","signInTime":{SIGNIN_TIME},"signOutAddress":null,"signOutTime":null,"currentDistance":{CURRENT_DISTANCE},"currentDistanceToSignInPoint":{CURRENT_DISTANCE},"rangeLimit":100,"isTargetOutOfRange":false,"isCheckoutOvertime":false,"overtimeText":"","isCheckoutOutOfRange":false,"outOfRangeDistance":0,"canCheckOut":true,"cardDisabled":false,"disabledReason":"","buttonDisabled":false,"rightBtnText":"确认签退","isExpired":false},"action_states":{"photo_check_in":{"enabled":false,"visible":true},"switch_plan":{"enabled":true,"visible":true},"refresh_location":{"enabled":true,"visible":true},"modify_location":{"enabled":true,"visible":true},"precheck_checkout":{"enabled":true,"visible":true},"submit_checkout":{"enabled":true,"visible":true}}}}<<<END_VISIT_BLOCK_V1>>>
```

**占位符说明**：

| 占位符 | 来源 | 格式 |
|--------|------|------|
| `{VISIT_PLAN_ID}` | 选定计划的 `visitPlanId` | 字符串 |
| `{PLAN_VISIT_TIME}` | `planVisitTime` | 毫秒时间戳；无则 `null` |
| `{PLAN_ADDRESS}` | `planVisitAddress` | 字符串；无则 `""` |
| `{CUSTOMER_NAME}` | `visitObjectInfo.licenseName` / `visitObjectName` | 客户名 |
| `{CLIENT_NAME}` | `clientName`（无则同客户名） | 字符串 |
| `{TARGET_LABEL}` | `visitTarget.label`（如"广告"） | 字符串 |
| `{TARGET_VALUE}` | `visitTarget.value`（如"AD_CUSTOMER"） | 字符串 |
| `{CURRENT_ADDRESS}` | 当前定位地址（上下文获取） | 字符串 |
| `{LATITUDE}` | 当前纬度（上下文获取；兜底签到纬度） | 数字 |
| `{LONGITUDE}` | 当前经度（上下文获取；兜底签到经度） | 数字 |
| `{SIGNIN_ADDRESS}` | 签到地址 | 字符串 |
| `{SIGNIN_TIME}` | 签到时间 | 毫秒时间戳 |
| `{CURRENT_DISTANCE}` | 当前位置距签到点距离（米） | 数字；无则 `0` |

**⚠️ action_states 固定**：`photo_check_in` 禁用（enabled=false），`switch_plan`/`refresh_location`/`modify_location`/`precheck_checkout`/`submit_checkout` 均 `{"enabled":true,"visible":true}`。

---

## [已签退结果] `visit-check-card`（checkinStatus = CHECKED_OUT）

**触发时机**：`submit_checkout.py` 返回 `success=true` 后输出此卡片，展示签退完成状态。

```
<<<VISIT_BLOCK_V1>>>{"protocol":"visit.session.v1","type":"a2ui.render","card":{"card_name":"visit-check-card","schema_version":"1.0","data":{"visitPlanId":"{VISIT_PLAN_ID}","planVisitTime":{PLAN_VISIT_TIME},"planVisitAddress":"{PLAN_ADDRESS}","visitObjectInfo":{"licenseName":"{CUSTOMER_NAME}"},"clientName":"{CLIENT_NAME}","visitTarget":{"label":"{TARGET_LABEL}","value":"{TARGET_VALUE}"},"checkinStatus":"CHECKED_OUT","currentAddress":"{CURRENT_ADDRESS}","latitude":{LATITUDE},"longitude":{LONGITUDE},"signInAddress":"{SIGNIN_ADDRESS}","signInTime":{SIGNIN_TIME},"signOutAddress":"{SIGNOUT_ADDRESS}","signOutTime":{SIGNOUT_TIME},"currentDistance":{CURRENT_DISTANCE},"currentDistanceToSignInPoint":{CURRENT_DISTANCE},"rangeLimit":100,"isTargetOutOfRange":false,"isCheckoutOvertime":false,"overtimeText":"","isCheckoutOutOfRange":false,"outOfRangeDistance":0,"canCheckOut":false,"cardDisabled":false,"disabledReason":"","buttonDisabled":false,"rightBtnText":"已签退","isExpired":false},"action_states":{"photo_check_in":{"enabled":false,"visible":true},"switch_plan":{"enabled":false,"visible":true},"refresh_location":{"enabled":false,"visible":true},"modify_location":{"enabled":false,"visible":true},"precheck_checkout":{"enabled":false,"visible":true},"submit_checkout":{"enabled":false,"visible":true}}}}<<<END_VISIT_BLOCK_V1>>>
```

**与待签退卡片的差异**：
- `checkinStatus` = `"CHECKED_OUT"`
- `signOutAddress` / `signOutTime` = 签退地址/时间（不再是 null）
- `canCheckOut` = `false`
- `rightBtnText` = `"已签退"`
- action_states：所有按钮均 `enabled=false`，`switch_plan` 也禁用（`{"enabled":false,"visible":true}`）

---

## [写操作返回协议] `a2ui.action_result`

前端在用户点击 visit-check-card 按钮时，会发送固定 Prompt 要求调用本 Skill。**必须严格按以下格式返回**，不能返回普通文字。

**通用格式**：
```
<<<VISIT_BLOCK_V1>>>{"protocol":"visit.session.v1","type":"a2ui.action_result","action_id":"{原样返回}","client_action_id":"{原样返回}","skill_name":"checkout","target":{"card_id":"{原样返回}","card_name":"visit-check-card"},"skill_result":{SKILL_RESULT_JSON}}<<<END_VISIT_BLOCK_V1>>>
```

> 🚦 **分流规则（避免重复预校验）**：收到 `action_id=precheck_checkout` 时先看 `data.checkinStatus`：
> - `checkinStatus=WAIT_CHECKOUT` → 卡片已是待签退态，预校验早已通过，本次是「确认签退」→ **直接执行 submit_checkout**（action_id 仍原样返回 `precheck_checkout`），见下方 submit_checkout。
> - 其他状态 → 首次预校验，执行 pre_check_checkout，见下方 precheck_checkout。

### precheck_checkout（首次预校验）

**校验通过**（`skill_result` 用脚本原始返回）：
```
<<<VISIT_BLOCK_V1>>>{"protocol":"visit.session.v1","type":"a2ui.action_result","action_id":"precheck_checkout","client_action_id":"{原样返回}","skill_name":"checkout","target":{"card_id":"{原样返回}","card_name":"visit-check-card"},"skill_result":{"canCheckout":true,"hasAbnormal":false,"abnormalList":[],"abnormalTags":[],"message":"校验通过，可以签退"}}<<<END_VISIT_BLOCK_V1>>>
```

**校验失败**（`success=false`，附异常原因）：
```
<<<VISIT_BLOCK_V1>>>{"protocol":"visit.session.v1","type":"a2ui.action_result","action_id":"precheck_checkout","client_action_id":"{原样返回}","skill_name":"checkout","target":{"card_id":"{原样返回}","card_name":"visit-check-card"},"skill_result":{"canCheckout":false,"hasAbnormal":true,"abnormalList":[{"abnormalType":"checkout_expired","title":"已超出可签退时间","content":"仅可在签到时间的第二天上午6:00前完成签退。"}],"abnormalTags":["checkout_expired"],"message":"签退最晚次日早6:00。仅可在签到时间的第二天上午6:00前完成签退。"}}<<<END_VISIT_BLOCK_V1>>>
```

### submit_checkout（正式签退）

**签退成功**后返回 action_result，并**追加**已签退卡片（CHECKED_OUT）：
```
<<<VISIT_BLOCK_V1>>>{"protocol":"visit.session.v1","type":"a2ui.action_result","action_id":"submit_checkout","client_action_id":"{原样返回}","skill_name":"checkout","target":{"card_id":"{原样返回}","card_name":"visit-check-card"},"skill_result":{"success":true,"message":"签退成功，别忘了填写拜访日志！"}}<<<END_VISIT_BLOCK_V1>>>
```
（如需追加已签退卡片，紧跟其后再输出一个 CHECKED_OUT 的 `a2ui.render` 协议块）

**签退失败**：
```
<<<VISIT_BLOCK_V1>>>{"protocol":"visit.session.v1","type":"a2ui.action_result","action_id":"submit_checkout","client_action_id":"{原样返回}","skill_name":"checkout","target":{"card_id":"{原样返回}","card_name":"visit-check-card"},"skill_result":{"success":false,"message":"{失败原因}"}}<<<END_VISIT_BLOCK_V1>>>
```

**⚠️ 关键约束**：
- `client_action_id` / `card_id` / `card_name` / `action_id` / `skill_name` 必须与前端传入完全一致，原样返回
- 同一轮 `a2ui.action_result` 恰好 1 个，`a2ui.render` 可 0 个或多个
- `skill_result` 必须是 JSON，即使失败也要返回，不能只返回普通文字

---

## [customerTypeLabel 枚举表]

| `visitObject` 值 | 输出 |
|-----------------|------|
| `ad_customer` / `广告客户` / `广告客户(老)` | `广告客户` |
| `agent` / `广告代理商` | `广告代理商` |
| `store` / `门店` | `门店` |
| `provider` / `团购服务商` | `团购服务商` |
