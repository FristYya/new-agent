# Visit Record — A2UI 卡片模板（前端验证标准）

> ⚠️ **所有输出必须完整复制对应模板，只替换 `{PLACEHOLDER}` 占位符，其余字符一律不得改变。**
> JSON 必须单行，禁止换行。
>
> 🚨 **严禁用 markdown 代码块（```）包裹协议块！** 协议块必须**单行**裸文本输出：
> `<<<VISIT_BLOCK_V1>>>{单行JSON}<<<END_VISIT_BLOCK_V1>>>`
> SSE 原始输出**不含 `block_id` 和 `card_id`**，禁止添加这两个字段。

---

## 🚨 输出互斥原则

> **卡片和文本不能在同一次输出里混发！每次输出只能是「纯卡片」或「纯文本」二选一。**

1. **下发卡片** → 只输出协议块，**前后不附加任何文字**
2. **追问 / 说明** → 只输出普通文字，**不下发协议块**
3. **一次输出只能一张卡片**

---

## 卡片索引

| 卡片 / 输出 | 触发时机 |
|------------|---------|
| `check-in-summary-card` | 只有 1 条待录音计划，或用户从列表选定计划后 |
| `visit-plan-result-card` | 有多条待录音计划，需要先选择 |
| 文字展示（无卡片） | 无候选计划 / Cookie 过期 / 任何追问 |

---

## [候选计划列表] `visit-plan-result-card`

有多条待录音计划时输出，让用户选择。

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
- `planVisitAddress` ← `""`（APP 端列表不含地址）

---

## [录音入口] `check-in-summary-card`

只有 1 条待录音计划、或用户从列表选定计划后输出。卡片含“填写拜访纪要”和“录制会议音频”两个选项。

```
<<<VISIT_BLOCK_V1>>>{"protocol":"visit.session.v1","type":"a2ui.render","card":{"card_name":"check-in-summary-card","schema_version":"1.0","data":{"list":[{"label":"填写拜访纪要","value":"fill_log"},{"label":"录制会议音频","value":"record_audio"}],"visitId":"{visitPlanId}","isReRecord":true},"action_states":{"recording_press":{"enabled":true,"visible":true},"action_press":{"enabled":true,"visible":true}}}}<<<END_VISIT_BLOCK_V1>>>
```

**变量**：
- `visitId` ← 选定计划的 visitPlanId（字符串）
- 其余字段全部固定，一字不差

**说明**：
- `recording_press`：用户点击“录制会议音频”，**前端直接调后端接口建立 WebSocket 录音连接**，Agent 无需干预
- `action_press`：用户点击“填写拜访纪要”，前端触发 fill_log 流程
- 卡片点击后属于展示型操作，Agent **不返回 a2ui.action_result**

---

---

## [录音摘要] `visit-summary-card`

用户说"查看xxx拜访计划的录音摘要"时输出。

```
<<<VISIT_BLOCK_V1>>>{"protocol":"visit.session.v1","type":"a2ui.render","block_id":"b_visit_summary_001","card":{"card_id":"c_visit_summary_001","card_name":"visit-summary-card","schema_version":"1.0","data":{"visitId":"{VISIT_PLAN_ID}","audioUrl":"{AUDIO_URL}","visitObjectType":"{VISIT_OBJECT_TYPE}","visitObjectInfo":{"licenseName":"{LICENSE_NAME}","agentName":{AGENT_NAME},"storePoiName":{STORE_POI_NAME},"providerName":{PROVIDER_NAME}},"planVisitTime":"{PLAN_VISIT_TIME}","planVisitAddress":"{PLAN_VISIT_ADDRESS}","visitSummary":"{VISIT_SUMMARY}","list":[{"label":"重新录制","value":"record-again"},{"label":"去签退","value":"checkout"}]},"action_states":{"action_press":{"enabled":true,"visible":true},"switch_plan":{"enabled":true,"visible":true}}}}<<<END_VISIT_BLOCK_V1>>>
```

**字段来源**（直接使用脚本输出，无需手动填充）：
- `visitId` ← visitPlanId（字符串）
- `audioUrl` ← `getAudioRecordDetail` 响应 `data.cdnUrl`
- `visitSummary` ← `getAudioRecordDetail` 响应 `data.llmSummary`
- `visitObjectType` / `visitObjectInfo` ← `queryVisitPlanDetails` 响应
- `planVisitTime` ← `queryVisitPlanDetails` 响应 `visitCheckinInfo.planVisitTime`（格式化为"YYYY年M月D日 HH:mm"）
- `planVisitAddress` ← `queryVisitPlanDetails` 响应 `visitCheckinInfo.planVisitAddress.address`
- `list` / `action_states` 固定值，不可更改

> ⚠️ 直接使用脚本 stdout 输出，**不要手动拼装此卡片**

---

## [customerTypeLabel 枚举表]

| `visitObject` 值 | 输出 |
|-----------------|------|
| `ad_customer` / `广告客户` / `广告客户(老)` | `广告客户` |
| `agent` / `广告代理商` | `广告代理商` |
| `store` / `门店` | `门店` |
| `provider` / `团购服务商` | `团购服务商` |
