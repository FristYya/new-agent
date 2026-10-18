# Visit FillLog — A2UI 卡片模板（前端验证标准）

> ⚠️ **所有输出必须完整复制对应模板，只替换 `{PLACEHOLDER}` 占位符，其余字符一律不得改变。**
> JSON 必须单行，禁止换行。
>
> 🚨 **严禁用 markdown 代码块（\`\`\`）包裹协议块！** 协议块必须**单行**裸文本输出，头尾标记和JSON之间**不能有换行**：
> `<<<VISIT_BLOCK_V1>>>{单行JSON}<<<END_VISIT_BLOCK_V1>>>`
> SSE 原始输出**不含 `block_id` 和 `card_id`**，禁止添加这两个字段。

---

## 🚨 输出互斥原则

> **卡片和文本不能在同一次输出里混发！每次输出只能是「纯卡片」或「纯文本」二选一。**

1. **下发卡片** → 只输出协议块，**前后不附加任何文字**
2. **追问 / 收集字段 / 说明** → 只输出普通文字，**不下发协议块**
3. **收集字段期间** → 每轮只发文本，等必填信息收集完毕，再下发一张整体卡片
4. **一次输出只能一张卡片**

---

## 卡片索引

| 卡片 | 触发时机 |
|------|---------|
| `visit-plan-result-card` | 查询待填日志列表后 |
| `visit-log-card` | 收集完字段后，填写日志表单 |
| `edit-visit-log-card` | 修改已有日志时（与 visit-log-card 结构相同，仅 card_name 不同） |
| `confirm-edit-visit-log-card` | 修改日志提交前二次确认 |
| `visit-log-completed-card` | 日志提交成功后 |

---

## [候选计划列表] `visit-plan-result-card`

```
<<<VISIT_BLOCK_V1>>>{"protocol":"visit.session.v1","type":"a2ui.render","card":{"card_name":"visit-plan-result-card","schema_version":"1.0","data":{"total":{TOTAL},"hasMore":false,"items":[{ITEMS_JSON}]},"action_states":{"select_plan":{"enabled":true,"visible":true}}}}<<<END_VISIT_BLOCK_V1>>>
```

**每条 item JSON 结构**：
```json
{"visitPlanId":"{PLAN_ID}","customerName":"{CUSTOMER_NAME}","customerTypeLabel":"{TYPE_LABEL}","planVisitTimeLabel":"{TIME_LABEL}","planVisitAddress":"{ADDRESS}"}
```

**字段映射**：
- `visitPlanId` ← `plan.visitPlanId`
- `customerName` ← `plan.visitObjectName`
- `customerTypeLabel` ← `plan.visitObject` 枚举转中文
- `planVisitTimeLabel` ← `plan.actualVisitTime` 或 `plan.planVisitTime` 格式化为 `YYYY-MM-DD HH:mm`
- `planVisitAddress` ← `plan.actualVisitAddress` 或 `plan.planVisitAddress`

---

## [填写拜访日志] `visit-log-card`

收集完联系人、拜访纪要等必填字段后，输出此卡片让用户确认并点击提交。

**⚠️ action_states 必须包含 submit_log 和 visit_target_press 两个动作，均为 `{"enabled":true,"visible":true}`，不能再用空对象 {}。**

```
<<<VISIT_BLOCK_V1>>>{"protocol":"visit.session.v1","type":"a2ui.render","card":{"card_name":"visit-log-card","schema_version":"1.0","data":{"visitId":{VISIT_ID},"visitObjectName":"{CUSTOMER_NAME}","visitObject":"{VISIT_OBJECT}","visitObjectType":"{OBJECT_TYPE}","visitObjectInfo":{"licenseName":"{CUSTOMER_NAME}","licenseCode":"{LICENSE_CODE}","agentId":null,"agentName":null,"storePoiId":null,"storePoiName":null,"providerId":null,"providerName":null},"planVisitTime":{PLAN_VISIT_TIME},"planVisitAddress":"{PLAN_ADDRESS}","checkinTime":{CHECKIN_TIME},"checkinAddress":"{CHECKIN_ADDRESS}","checkinAbnormalInfo":null,"checkinAbnormalReason":null,"isCheckedOut":{IS_CHECKED_OUT},"checkoutTime":{CHECKOUT_TIME},"checkoutAddress":"{CHECKOUT_ADDRESS}","photoUrl":{PHOTO_URL},"checkoutPhotoUrl":null,"supplementAllowed":{SUPPLEMENT_ALLOWED},"actualVisitTime":{ACTUAL_VISIT_TIME},"contacts":{"contact":[{CONTACTS_JSON}],"wechatContact":[]},"visitSummary":"{VISIT_SUMMARY}"},"action_states":{"submit_log":{"enabled":true,"visible":true},"visit_target_press":{"enabled":true,"visible":true}}}}<<<END_VISIT_BLOCK_V1>>>
```

**占位符说明**：

| 占位符 | 来源 | 格式 |
|--------|------|------|
| `{VISIT_ID}` | `visitId` | 字符串或 `null` |
| `{CUSTOMER_NAME}` | `visitObjectName` | 客户名 |
| `{VISIT_OBJECT}` | `visitObject` | 如 `"广告客户"` |
| `{OBJECT_TYPE}` | `visitObjectType` | 如 `"ad_customer"` |
| `{LICENSE_CODE}` | `visitObjectInfo.licenseCode` | 字符串；无则 `""` |
| `{PLAN_VISIT_TIME}` | `planVisitTime` | 毫秒时间戳；无则 `null` |
| `{PLAN_ADDRESS}` | `planVisitAddress` | 字符串；无则 `""` |
| `{CHECKIN_TIME}` | `checkinTime` | 毫秒时间戳；无则 `null` |
| `{CHECKIN_ADDRESS}` | `checkinAddress` | 字符串；无则 `""` |
| `{IS_CHECKED_OUT}` | `isCheckedOut` | `0` 或 `1` |
| `{CHECKOUT_TIME}` | `checkoutTime` | 毫秒时间戳；无则 `null` |
| `{CHECKOUT_ADDRESS}` | `checkoutAddress` | 字符串；无则 `null` |
| `{PHOTO_URL}` | `photoUrl` | 字符串或 `null` |
| `{SUPPLEMENT_ALLOWED}` | `supplementAllowed` | `0` 或 `1` |
| `{ACTUAL_VISIT_TIME}` | `actualVisitTime` | 毫秒时间戳；无则 `null` |
| `{VISIT_SUMMARY}` | `visitSummary` | 纪要文字 |
| `{CONTACTS_JSON}` | 联系人列表 | 逗号分隔的 JSON 对象数组 |

**每条 contact**：
```json
{"id":"{ID}","name":"{NAME}","phone":"{PHONE}","isKp":{IS_KP}}
```

**前端验证示例**：
```
<<<VISIT_BLOCK_V1>>>{"protocol":"visit.session.v1","type":"a2ui.render","card":{"card_name":"visit-log-card","schema_version":"1.0","data":{"visitId":"demo-visit-001","visitObjectName":"测试-PRT-泳道提交--410","visitObject":"广告客户","visitObjectType":"ad_customer","visitObjectInfo":{"licenseName":"测试-PRT-泳道提交--410","licenseCode":"123333","agentId":null,"agentName":null,"storePoiId":null,"storePoiName":null,"providerId":null,"providerName":null},"planVisitTime":1736842000000,"planVisitAddress":"北京市海淀区西北旺唐家岭路188号","checkinTime":1736842000000,"checkinAddress":"北京市海淀区西北旺唐家岭路188号","checkinAbnormalInfo":null,"checkinAbnormalReason":null,"isCheckedOut":0,"checkoutTime":null,"checkoutAddress":null,"photoUrl":"https://example.com/visit/demo.jpg","checkoutPhotoUrl":null,"supplementAllowed":1,"actualVisitTime":null,"contacts":{"contact":[{"id":"demo-contact-001","name":"张三","phone":"13800138001","isKp":1},{"id":"demo-contact-002","name":"李四","phone":"13900139002","isKp":0}],"wechatContact":[]},"visitSummary":"今日与商户进行了深入沟通，就下一季度的合作方案达成了初步共识。"},"action_states":{"submit_log":{"enabled":true,"visible":true},"visit_target_press":{"enabled":true,"visible":true}}}}<<<END_VISIT_BLOCK_V1>>>
```

---

## [修改拜访日志] `edit-visit-log-card`

**与 visit-log-card 的 data 结构完全相同，区别仅在 card_name 与 action_states。**

**⚠️ action_states 含 confirm_edit_visit_log 和 switch_log 两个动作，均为 `{"enabled":true,"visible":true}`。**

```
<<<VISIT_BLOCK_V1>>>{"protocol":"visit.session.v1","type":"a2ui.render","card":{"card_name":"edit-visit-log-card","schema_version":"1.0","data":{"visitId":{VISIT_ID},"visitObjectName":"{CUSTOMER_NAME}","visitObject":"{VISIT_OBJECT}","visitObjectType":"{OBJECT_TYPE}","visitObjectInfo":{"licenseName":"{CUSTOMER_NAME}","licenseCode":"{LICENSE_CODE}","agentId":null,"agentName":null,"storePoiId":null,"storePoiName":null,"providerId":null,"providerName":null},"planVisitTime":{PLAN_VISIT_TIME},"planVisitAddress":"{PLAN_ADDRESS}","checkinTime":{CHECKIN_TIME},"checkinAddress":"{CHECKIN_ADDRESS}","checkinAbnormalInfo":null,"checkinAbnormalReason":null,"isCheckedOut":{IS_CHECKED_OUT},"checkoutTime":{CHECKOUT_TIME},"checkoutAddress":"{CHECKOUT_ADDRESS}","photoUrl":null,"checkoutPhotoUrl":null,"supplementAllowed":{SUPPLEMENT_ALLOWED},"actualVisitTime":{ACTUAL_VISIT_TIME},"contacts":{"contact":[{CONTACTS_JSON}],"wechatContact":[]},"visitSummary":"{VISIT_SUMMARY}"},"action_states":{"confirm_edit_visit_log":{"enabled":true,"visible":true},"switch_log":{"enabled":true,"visible":true}}}}<<<END_VISIT_BLOCK_V1>>>
```

---

## [确认修改拜访日志] `confirm-edit-visit-log-card`

**此卡片 data 结构不同：多了 modifyFields，contacts/contact 可为空数组。action_states 含 confirm_edit_log 和 switch_log 两个动作。**

```
<<<VISIT_BLOCK_V1>>>{"protocol":"visit.session.v1","type":"a2ui.render","card":{"card_name":"confirm-edit-visit-log-card","schema_version":"1.0","data":{"visitId":{VISIT_ID},"visitObjectName":"{CUSTOMER_NAME}","visitObject":"{VISIT_OBJECT}","visitObjectType":"{OBJECT_TYPE}","visitObjectInfo":{"licenseName":"{CUSTOMER_NAME}","licenseCode":"{LICENSE_CODE}","agentId":null,"agentName":null,"storePoiId":null,"storePoiName":null,"providerId":null,"providerName":null},"planVisitTime":{PLAN_VISIT_TIME},"planVisitAddress":"{PLAN_ADDRESS}","checkinTime":{CHECKIN_TIME},"checkinAbnormalInfo":null,"checkinAbnormalReason":null,"actualVisitTime":{ACTUAL_VISIT_TIME},"contacts":{"contact":[],"wechatContact":[]},"visitSummary":null,"modifyFields":[{MODIFY_FIELDS}]},"action_states":{"confirm_edit_log":{"enabled":true,"visible":true},"switch_log":{"enabled":true,"visible":true}}}}<<<END_VISIT_BLOCK_V1>>>
```

**`{MODIFY_FIELDS}`** 为字符串数组，可多选，元素取值：`"contacts"` / `"actualVisitTime"` / `"checkinAbnormalReason"` / `"visitSummary"`。
例：`["contacts","visitSummary"]`

---

## [拜访日志提交完成] `visit-log-completed-card`

**此卡片 data 含 `navigationTarget`（跳转详情链接对象）和 actions 数组。**

```
<<<VISIT_BLOCK_V1>>>{"protocol":"visit.session.v1","type":"a2ui.render","card":{"card_name":"visit-log-completed-card","schema_version":"1.0","data":{"visitPlanId":"{PLAN_ID}","navigationTarget":{"kind":"krn_scheme","url":"locallifevisit://krn?bundleId=LocalLifeVisit&componentName=VisitDetailV2&themeStyle=3&visitPlanId={PLAN_ID}"},"customerName":"{CUSTOMER_NAME}","planVisitAddress":"{PLAN_VISIT_ADDRESS}","checkinTimeLabel":"{CHECKIN_TIME_LABEL}","checkinAddress":"{CHECKIN_ADDRESS}","checkoutTimeLabel":"{CHECKOUT_TIME_LABEL}","checkoutAddress":"{CHECKOUT_ADDRESS}","contactLabel":"{CONTACT_LABEL}","visitSummary":"{VISIT_SUMMARY}","actions":[{"label":"修改拜访日志","value":"edit-visit-log"},{"label":"创建拜访计划","value":"create-visit-plan"}]},"action_states":{"follow_up_action":{"enabled":true,"visible":true},"edit_plan":{"enabled":true,"visible":true},"view_detail":{"enabled":true,"visible":true}}}}<<<END_VISIT_BLOCK_V1>>>
```

**占位符说明**：

| 占位符 | 来源 | 格式 |
|--------|------|------|
| `{PLAN_ID}` | `visitPlanId` | 字符串，同时用于拼接 `navigationTarget.url` |
| `{CUSTOMER_NAME}` | `visitObjectName` | 客户名 |
| `{PLAN_VISIT_ADDRESS}` | 计划拜访地址 | 无则 `""` |
| `{CHECKIN_TIME_LABEL}` | `checkinTime` 格式化 `YYYY-MM-DD HH:mm` | 无则 `""` |
| `{CHECKIN_ADDRESS}` | 签到地址 | 无则 `""` |
| `{CHECKOUT_TIME_LABEL}` | `checkoutTime` 格式化 `YYYY-MM-DD HH:mm` | 无则 `""` |
| `{CHECKOUT_ADDRESS}` | 签退地址 | 无则 `""` |
| `{CONTACT_LABEL}` | 联系人拼接 | `姓名 手机号（KP）`，非 KP 不标 |
| `{VISIT_SUMMARY}` | `visitSummary` | 纪要文字 |

**⚠️ actions 固定**：`[{"label":"修改拜访日志","value":"edit-visit-log"},{"label":"创建拜访计划","value":"create-visit-plan"}]`

---

## [customerTypeLabel 枚举表]

| `visitObject` 值 | 输出 |
|-----------------|------|
| `ad_customer` / `广告客户` / `广告客户(老)` | `广告客户` |
| `agent` / `广告代理商` | `广告代理商` |
| `store` / `门店` | `门店` |
| `provider` / `团购服务商` | `团购服务商` |

---

## [写操作返回协议] `a2ui.action_result`

前端发送写操作 Prompt 时，必须返回此格式。

```
<<<VISIT_BLOCK_V1>>>{"protocol":"visit.session.v1","type":"a2ui.action_result","action_id":"{原样返回}","client_action_id":"{原样返回}","skill_name":"visit-filllog","target":{"card_id":"{原样返回}","card_name":"{原样返回}"},"skill_result":{JSON}}<<<END_VISIT_BLOCK_V1>>>
```

- `client_action_id` / `card_id` / `card_name` / `action_id` 必须原样返回
- 同一轮 `a2ui.action_result` 恰好 1 个，`a2ui.render` 可 0 个或多个
- 失败也必须返回 `action_result`（`success: false`）
