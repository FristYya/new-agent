# visit-checkout 接口契约

签退全流程统一走 **internal 域名**（`http://127.0.0.1:8080`）+ SSO 鉴权，三个接口身份必须一致。

---

## 接口清单

| 接口 | 方法路径 | 端 | 用途 |
|------|---------|-----|------|
| queryVisitPlanListApp | `POST /rest/apicenter/visit/app/visitAi/visitNew/queryVisitPlanListApp` | internal | 查询待签退计划列表 |
| preCheckCheckout | `POST /rest/apicenter/visit/app/visitAi/visitCenter/preCheckCheckout` | internal | 签退预校验 |
| submitCheckout | `POST /rest/apicenter/visit/app/visitAi/visitCenter/submitCheckout` | internal | 提交签退 |

> ⚠️ internal（visit）环境下：查询走 `app/visitAi/visitNew`，写操作（预校验/签退）走 `app/visitAi/visitCenter`，均走 SSO 鉴权。

---

## Cookie / 鉴权（SSO）

internal 环境走 SSO 鉴权，从上下文自动获取 PC 端 Cookie。

获取优先级（见 `scripts/api_client.py`）：
1. `runContext.cookies` 环境变量（平台注入，JSON 对象）
2. SSO 登录状态 `~/.new-agent/sso/sso-state.json`
3. `CHECKOUT_COOKIE` 环境变量
4. skill 目录下 `.cookie` 文件

必要 Header：
```
Content-Type: application/json
Referer: http://127.0.0.1:8080/visit/tools/visit
Origin: http://127.0.0.1:8080
Host: 127.0.0.1:8080
trace-context: {"laneId":"PRT.v1"}
```

---

## 1. queryVisitPlanListApp

**请求体**：
```json
{
  "pageInfo": {"pageNum": 1, "pageSize": 20},
  "filterCondition": [{"key": "clockStatus", "value": ["1"]}]
}
```
- `clockStatus=["1"]`：已签到未签退 → 待签退

**响应**：`data.list[]`，每项关键字段：
```json
{
  "visitId": "6940992561615064",
  "businessLicenseName": "福禄克测试仪器（上海）有限公司",
  "platformAccount": "福禄克Fluke官方账号",
  "visitObject": "广告客户",
  "visitType": "本地拜访",
  "realName": "苏帆",
  "visitStatus": "待填写日志",
  "actualVisitTime": "2026-6-8",
  "planVisitTime": "2026-6-8",
  "operations": ["cancel_visit", "fill_log", "checkout"]
}
```
- `visitId` 即 visitPlanId
- `operations` 含 `checkout` 才是真正可签退（脚本已过滤）

---

## 2. preCheckCheckout

**请求体**：
```json
{
  "visitPlanIdStr": "7082596457518176",
  "longitude": 116.28825241860271,
  "latitude": 40.053037617829595,
  "address": "北京市海淀区西北旺镇快手公司万家灯火大厦"
}
```

**响应 data**：
```json
{
  "canCheckout": true,
  "hasAbnormal": false,
  "abnormalList": [],
  "abnormalTags": [],
  "abnormalReason": ""
}
```

**校验失败示例**（超时）：
```json
{
  "canCheckout": false,
  "hasAbnormal": true,
  "abnormalList": [
    {"abnormalType": "checkout_expired", "title": "已超出可签退时间", "content": "仅可在签到时间的第二天上午6:00前完成签退。", "buttons": ["好的"]}
  ],
  "abnormalTags": ["checkout_expired"],
  "abnormalReason": "已超出可签退时间"
}
```

**异常类型**：
| abnormalType | 含义 | 提示 |
|--------------|------|------|
| `checkout_distance_abnormal` | 超出签到点范围 | 超出签到地1km范围则无法签退 |
| `checkout_expired` | 超出可签退时间 | 签退最晚次日早6:00 |

---

## 3. submitCheckout

**请求体**：同 preCheckCheckout。

**响应**：`baseResponse.code=0` 表示成功。脚本返回：
```json
{"success": true, "visitPlanId": "...", "message": "签退成功，别忘了填写拜访日志！"}
```

---

## 实测记录（2026-07-24）

- 查询：APP 用户"苏帆"返回 20 条待签退计划 ✅
- 预校验（今日计划 7082596457518176）：`canCheckout=true` ✅
- 预校验（旧计划 6940992561615064）：`canCheckout=false, checkout_expired` ✅（正确拦截）
- 签退（7082596457518176）：`success=true` ✅
- 签退后再预校验：`canCheckout=false`（已签退不可重复）✅
