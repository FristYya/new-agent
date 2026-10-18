# visit-pending-card 卡片模板

拜访待办场景**只返回这一个卡片**（`visit-pending-card`），不返回其他卡片，不输出 Markdown 表格。

---

## 协议块格式（严格单行，无换行）

⚠️ 头标记 + JSON + 尾标记必须在**同一行、无换行**，否则前端无法解析（卡片暂时无法展示）。

正确格式：

```
<<<VISIT_BLOCK_V1>>>{单行完整JSON}<<<END_VISIT_BLOCK_V1>>>
```

---

## visit-pending-card 模板

把 `query_pending.py` 返回的 JSON 整体填入 `data`，占位符对应关系：

- `{TOTAL}` = 返回的 total
- `{TRAVEL_PLAN_COUNT}` = pendingTravelPlanCount
- `{CHECK_IN_COUNT}` = pendingCheckInCount
- `{CHECK_OUT_COUNT}` = pendingCheckOutCount
- `{LOG_COUNT}` = pendingLogCount
- `{TRAVEL_PLAN_ITEMS}` = pendingTravelPlanItems 数组（原样填入）
- `{CHECK_IN_ITEMS}` = pendingCheckInItems 数组（原样填入）
- `{CHECK_OUT_ITEMS}` = pendingCheckOutItems 数组（原样填入）
- `{LOG_ITEMS}` = pendingLogItems 数组（原样填入）

模板（单行）：

```
<<<VISIT_BLOCK_V1>>>{"protocol":"visit.session.v1","type":"a2ui.render","card":{"card_name":"visit-pending-card","schema_version":"1.0","data":{"total":{TOTAL},"pendingTravelPlanCount":{TRAVEL_PLAN_COUNT},"pendingCheckInCount":{CHECK_IN_COUNT},"pendingCheckOutCount":{CHECK_OUT_COUNT},"pendingLogCount":{LOG_COUNT},"pendingTravelPlanItems":{TRAVEL_PLAN_ITEMS},"pendingCheckInItems":{CHECK_IN_ITEMS},"pendingCheckOutItems":{CHECK_OUT_ITEMS},"pendingLogItems":{LOG_ITEMS}},"action_states":{}}}<<<END_VISIT_BLOCK_V1>>>
```

---

## item 字段说明

### travel-plan item（待填写差旅计划）

| 字段 | 含义 | 来源 |
|------|------|------|
| `visitId` | 拜访计划 ID | `queryAiVisit` 返回的 `visitPlanId` / `planId` |
| `pendingType` | 固定 `pending-travel-plan` | — |
| `travelOrderNo` | 差旅单号 | `travelApplyNo` |
| `travelLocation` | 目的地 | `cities` 数组拼接为「北京-上海-杭州」 |
| `travelTime` | 拜访时间标签 | `planVisitTime` 格式化为「MM月DD日」 |

### check-in / check-out / log item

每个 item（`query_pending.py` 已组装好，直接用）：

| 字段 | 含义 | 来源 |
|------|------|------|
| `visitId` | 拜访计划 ID | 列表 `visitId` |
| `pendingType` | 待办类型：`pending-check-in` / `pending-check-out` / `pending-log` | 按查询类别固定 |
| `customerName` | 客户名称 | 列表 `businessLicense` |
| `customerTypeLabel` | 客户类型 | 列表 `visitObject` |
| `timeLabel` | 时间标签 | 待签到=planVisitTime「MM月DD日」；待签退/待填=actualVisitTime「MM月DD日 HH：MM」 |
| `address` | 地址 | 列表 `planVisitAddress` |

---

## 单意图规则（重要）

- 用户问「今日待办 / 全部待办」→ mode=all，**四类全查**，四个列表都填。
- 用户问「未签到 / 待签到」→ mode=check-in，**只填 pendingCheckInItems**，其他三类为空数组、count=0、total 只计待签到数。
- 用户问「未签退 / 待签退」→ mode=check-out，只填 pendingCheckOutItems。
- 用户问「待填日志 / 未写纪要」→ mode=log，只填 pendingLogItems。
- 用户问「待填写差旅 / 待补充差旅 / 补充差旅计划」→ mode=travel-plan，只填 pendingTravelPlanItems。

无论哪种模式，都用同一个 `visit-pending-card` 输出（其余列表为空即可）。

---

## 接口说明

| 待办类型 | 接口 | 筛选条件 |
|---|---|---|
| 待填写差旅计划 | `queryAiVisit`（路径 `/rest/apicenter/visit/web/visitCenter/queryAiVisit`） | `queryType=pending_travel` |
| 待签到 | `queryVisitPlanList` | `clockStatus=["0"]` |
| 待签退 | `queryVisitPlanList` | `clockStatus=["1"]` + `signInTime` 范围 |
| 待填日志 | `queryVisitPlanList` | `visitStatus=["2"]` |
