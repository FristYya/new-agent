# API Contract（通用请求约定）

拜访助手所有意图共用的 domain / Cookie / Header / endpoint / 状态码。

**双后端架构说明：**
- **拜访服务 后端**（`127.0.0.1:8080`）：create / update / cancel / checkin / query
- **拜访服务 后端**（`127.0.0.1:8080`）：checkout / fill_log / todos / record

两个后端独立运行，使用不同的 Cookie 和接口路径。

## Domain

### 拜访服务 后端（create / update / cancel / checkin / query）

```
http://127.0.0.1:8080
```

### 拜访服务 后端（checkout / fill_log / todos / record）

```
http://127.0.0.1:8080
```

两个后端完全独立，不能混用 Cookie 和接口。

## Headers

### 拜访服务 后端

| Header | Value |
| --- | --- |
| `Content-Type` | `application/json` |
| `trace-context` | `{"laneId": "PRT.operation_assistan"}` |
| `Cookie` | 见下文 FIXED_COOKIE（拜访服务 专用） |

### 拜访服务 后端

| Header | Value |
| --- | --- |
| `Content-Type` | `application/json` |
| `trace-context` | `{"laneId": "PRT.v1"}` |
| `Referer` | `http://127.0.0.1:8080/visit/tools/visit` |
| `Origin` | `http://127.0.0.1:8080` |
| `Host` | `127.0.0.1:8080` |
| `Cookie` | 自动从 SSO 登录状态获取 |

**FIXED_COOKIE**：完整值见 `docs/create-visit-plan-skill.md` 第 4 节（本地测试用，上线由平台注入）。

**拜访服务 Cookie 获取优先级：**
1. 从 SSO 登录状态 (`~/.new-agent/sso/sso-state.json`) 提取 — 推荐，自动获取
2. 从环境变量 `VISIT_COOKIE` 读取
3. 从 skill 目录下的 `.cookie` 文件读取
4. 回退到硬编码 Cookie（仅测试用）

## 拜访服务 后端通用 Body 字段（camelCase）

| 字段 | create | update / cancel / checkin | 说明 |
| --- | --- | --- | --- |
| `chatId` | ✅ | ✅ | LLM 生成的全流程唯一 ID |
| `activeSkill` | `"create"` | `"update"` / `"cancel"` / `"checkin"` | 固定 |
| `app` | `"visit"` | 同 | 固定 |
| `sessionCreatedAt` | ✅ 毫秒时间戳 | ❌ | 仅 create 用 |
| `planId` | ❌ | ✅ | 其它 3 个必传 |
| `baseRequest.user_name` | ✅ | ✅ | 快手账号（如 `lizhifeng05`） |

**拜访服务 后端不需要这些 body 字段**，直接调用 Python 脚本即可。

## 拜访服务 后端 Endpoint

### 通用（create / update / cancel / checkin）

| Endpoint | 用途 |
| --- | --- |
| `POST /rest/apicenter/visit/web/visitCenter/getAiVisitState` | 读取当前会话 slots / waiting_for_list / fsm_state |
| `POST /rest/apicenter/visit/web/visitCenter/batchResolveAiVisitSlot` | 提交 LLM 抽取的 items，后端解析并写入 slots |
| `POST /rest/apicenter/visit/web/visitCenter/executeAiVisitAction` | 落库 / 取消 / 签到 |

### 查询专用

| Endpoint | 用途 |
| --- | --- |
| `POST /rest/apicenter/visit/web/visitCenter/queryAiVisit` | 列 today_plans / plan_list / nearby_match 等 |

## 拜访服务 后端 Endpoint

### 签退（checkout）

| Endpoint | 用途 |
| --- | --- |
| `POST /rest/apicenter/visit/web/visitNew/queryVisitPlanList` | 查询拜访计划列表 |
| `POST /rest/apicenter/visit/app/visitAi/visitCenter/preCheckCheckout` | 签退预校验 |
| `POST /rest/apicenter/visit/app/visitAi/visitCenter/submitCheckout` | 提交签退 |

### 填写日志（fill_log）

| Endpoint | 用途 |
| --- | --- |
| `POST /rest/apicenter/visit/web/visitNew/queryVisitPlanList` | 查询拜访计划列表 |
| `POST /rest/apicenter/visit/web/visitNew/getVisitPlanCache` | 查询草稿 |
| `POST /rest/apicenter/visit/web/visit/new/work/fillVisitLog` | 填写日志 ⚠️ 特殊路径 |
| `POST /rest/apicenter/visit/web/visitNew/saveVisitPlanCache` | 保存草稿 |
| `POST /rest/apicenter/visit/web/visitNew/queryVisitPlanDetails` | 查询详情 |
| `POST /rest/apicenter/visit/web/visitNew/queryContacts` | 查询联系人 |
| `POST /rest/apicenter/visit/web/visitNew/addOrUpdateContact` | 新增联系人 |
| `POST /rest/apicenter/visit/web/visitNew/checkVisitSupplementAllowed` | 检查补录资格 |
| `POST /rest/apicenter/visit/web/visitNew/submitLaterCheckin` | 提交补录 |

### 待办查询（todos）

| Endpoint | 用途 |
| --- | --- |
| `POST /rest/apicenter/visit/web/visitNew/queryVisitPlanList` | 查询拜访计划列表 |

### 录音（record）

| Endpoint | 用途 |
| --- | --- |
| `POST /rest/apicenter/visit/web/visitNew/queryVisitPlanList` | 查询拜访计划列表 |

## queryAiVisit（意图识别辅助）

**用途**：当用户缺 `planId` 时，让意图识别层主动帮用户找到目标计划。

```bash
curl -X POST 'http://127.0.0.1:8080/rest/apicenter/visit/web/visitCenter/queryAiVisit' \
  -H 'trace-context: {"laneId": "PRT.operation_assistan"}' \
  -H 'Cookie: <FIXED_COOKIE>' -H 'Content-Type: application/json' \
  -d '{
    "chatId":"<chat_id>",
    "queryType":"<today_plans|plan_list|nearby_match|pending_travel|city_unvisited>",
    "param":"<关键词或 lng,lat>",
    "limit":10,
    "baseRequest":{"user_name":"lizhifeng05"}
  }'
```

**queryType 说明**：

| queryType | param | 用途 |
| --- | --- | --- |
| `today_plans` | `"lng,lat"`（必） | 今日未签到的拜访计划 |
| `pending_travel` | 无 | 待补的差旅拜访 |
| `plan_list` | 关键词（可空） | 关键词模糊搜索计划 |
| `nearby_match` | `"lng,lat"` | 1km 内今日计划（签到自动匹配） |
| `city_unvisited` | `"lng,lat"` | 50km 内今日未签到 |

**响应**：`data` 是列表，每个元素含 `planId / planTime / planAddress / customerName` 等，LLM 用这些字段生成用户可读的候选清单。

**常见错误**：
- 返 error `"不支持的查询类型: xxx"`：检查 `queryType` 拼写
- 候选为空：向用户说明"未找到匹配的计划，请提供更多信息或先创建"

## 通用状态码

| 字段 | 值 | 含义 |
| --- | --- | --- |
| `status` | `done` | 执行成功 |
|  | `confirm` | 后端要求二次确认（如 checkin 异常、update 冲突） |
|  | `fallback` | 后端主动降级，跳转人工/UI |
|  | `error` | 硬失败，展示 `error_msg` |
| `fsm_state` | `FILLING` | 收集槽位阶段 |
|  | `CONFIRMING` | 全部槽位就绪，等用户确认（create / checkin） |
|  | `PENDING` | 会话新建 |
|  | `FALLBACK` | 降级 |
| `results[].status` | `ok` / `ambiguous` / `fail` | 单个 slot 解析结果 |

## Session Key 规则（Redis）

| 意图 | Session Key |
| --- | --- |
| create | `vai:state:{chatId}:create:{sessionCreatedAt}` |
| update | `vai:state:{chatId}:update:{planId}` |
| cancel | `vai:state:{chatId}:cancel:{planId}` |
| checkin | `vai:state:{chatId}:checkin:{planId}` |

TTL 24h 滑动续期。
