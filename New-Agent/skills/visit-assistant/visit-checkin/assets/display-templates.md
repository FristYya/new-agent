# Checkin Skill — A2UI 卡片模板

> - 有卡片场景 → 裸输出协议块，禁止 markdown 包裹
> - JSON 单行，不含换行，无 `\"` 转义
> - `visit-check-card`、`visit-plan-result-card`、`check-in-summary-card`（确认签到场景）由脚本自动生成（`_suggestedCard`），不在此定义

---

## [开启定位卡片] `enable-location-card`

触发：签到意图收到时，上下文缺 `latitude` / `longitude`，立即输出此卡片。

```
<<<VISIT_BLOCK_V1>>>{"protocol":"visit.session.v1","type":"a2ui.render","card":{"card_name":"enable-location-card","schema_version":"1.0","data":{"latitude":null,"longitude":null},"action_states":{"enable_location":{"enabled":true,"visible":true}}}}<<<END_VISIT_BLOCK_V1>>>
```

> 无占位符，原封不动输出。用户点击后从 `payload.latitude/longitude` 取经纬度，不输出 `action_result`，直接进入计划查询。

---

## [签到摘要卡片] `check-in-summary-card`（重新录制场景）

触发：用户在 `visit-summary-card` 点击「重新录制」（`payload.value=record-again`）

```
<<<VISIT_BLOCK_V1>>>{"protocol":"visit.session.v1","type":"a2ui.render","card":{"card_name":"check-in-summary-card","schema_version":"1.0","data":{"visitId":"{VISIT_ID}","isReRecord":true,"recordingStatus":"idle","list":[{"label":"当前计划签退","value":"check-out"},{"label":"修改拜访计划信息","value":"edit-visit-plan"}]},"action_states":{"recording_start":{"enabled":true,"visible":true},"recording_end":{"enabled":true,"visible":true},"visit_log_ready":{"enabled":true,"visible":true},"action_press":{"enabled":true,"visible":true}}}}<<<END_VISIT_BLOCK_V1>>>
```

**占位符**：
- `{VISIT_ID}` ← `payload.visitId`
- `isReRecord` 固定为 `true`，`recordingStatus` 固定为 `"idle"`
