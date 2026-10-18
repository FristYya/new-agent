"""
AI 拜访助手 — 取消拜访计划脚本
仅使用 Python 标准库（无第三方依赖）。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

BASE_URL = "http://127.0.0.1:8080"
_PATH_PREFIX = "rest/apicenter/visit/web/visitCenter"
_HTTP_TIMEOUT_SEC = 15

def _extract_state_blob(data: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(data.get("state"), dict):
        return data["state"]
    if isinstance(data.get("data"), dict):
        inner = data["data"]
        if "fsmState" in inner or "slots" in inner:
            return inner
    if "fsmState" in data or "slots" in data:
        return data
    return {}

def _print_result(
    data: Dict[str, Any],
    session_created_at: int,
    chat_id: str,
    suggested_card: Optional[str] = None,
) -> None:
    """stdout 带 sessionCreatedAt；可确认/成功时附带 _suggestedCard 供模型原样输出。"""
    if isinstance(data, dict):
        out = dict(data)
    else:
        out = {"data": data}
    out["_clientMeta"] = {
        "chatId": chat_id,
        "sessionCreatedAt": session_created_at,
    }
    if suggested_card:
        out["_suggestedCard"] = suggested_card
        out["_suggestedCardRule"] = (
            "整条对用户回复必须且只能等于 _suggestedCard 字符串；"
            "禁止 Markdown/表格/emoji/自然语言确认或成功文案。"
        )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print(
        f"sessionCreatedAt={session_created_at}（后续 exec 必须带 --session-created-at {session_created_at}；禁止写文件缓存）",
        file=sys.stderr,
    )
    if suggested_card:
        print(
            "[BLOCKED] 检测到可出卡状态：请将 stdout._suggestedCard 原样作为整条回复输出（块外零字符）。",
            file=sys.stderr,
        )

def _biz_ok(data: Dict[str, Any]) -> bool:
    if not isinstance(data, dict):
        return False
    if data.get("error") or data.get("error_http_status"):
        return False
    base = data.get("baseResponse") or {}
    code = base.get("code")
    return code in (0, "0", None) and "error_http_status" not in data

# ------------------------------------------------------------------
# Runtime Context（New-Agent 自动凭据）
# ------------------------------------------------------------------

def _load_runtime_context() -> Optional[Dict[str, Any]]:
    """New-Agent exec 环境下读取已绑定的 运行上下文；未注入环境变量时返回 None。"""
    raw = os.getenv("AGENT_RUNTIME_CONTEXT") or os.getenv("RUN_CONTEXT")
    if not raw:
        return None
    try:
        ctx = json.loads(raw)
        return ctx if isinstance(ctx, dict) else None
    except (TypeError, ValueError) as e:
        print(f"[WARN] 读取运行上下文失败: {e}", file=sys.stderr)
        return None

def _biz_context(ctx: Dict[str, Any]) -> Dict[str, Any]:
    biz = ctx.get("bizContext")
    return biz if isinstance(biz, dict) else {}

def _resolve_cookies(cli_cookies: Optional[str], ctx: Optional[Dict[str, Any]]) -> str:
    if cli_cookies:
        return cli_cookies
    if not ctx:
        return ""
    biz = _biz_context(ctx)
    for key in ("cookie", "Cookie", "cookies", "COOKIE"):
        val = ctx.get(key)
        if isinstance(val, str) and val.strip():
            return val
        val = biz.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""

def _resolve_chat_id(cli_chat_id: Optional[str], ctx: Optional[Dict[str, Any]]) -> str:
    if cli_chat_id:
        return cli_chat_id
    if not ctx:
        return ""
    session_id = ctx.get("sessionId")
    return session_id.strip() if isinstance(session_id, str) else ""

# ------------------------------------------------------------------
# VisitClient
# ------------------------------------------------------------------

class VisitClient:
    def __init__(
        self,
        chat_id: str,
        session_created_at: Optional[int] = None,
        app: str = "visit",
        cookies: Optional[str] = None,
        require_session_created_at: bool = False,
    ):
        self.chat_id = chat_id
        self.app = app
        self._cookies = cookies

        # 不写任何本地文件：sessionCreatedAt 只能靠 CLI 回传跨 exec 保持一致
        if session_created_at is not None:
            self.session_created_at = int(session_created_at)
        elif require_session_created_at:
            raise SystemExit(
                "缺少 --session-created-at。"
                "请使用上一次 stdout._clientMeta.sessionCreatedAt；"
                "本脚本不落盘缓存，禁止依赖本地文件。"
            )
        else:
            self.session_created_at = int(time.time() * 1000)
            print(
                f"[WARN] 未传 --session-created-at，本次新生成 {self.session_created_at}；"
                "后续所有 exec 必须回传该值。",
                file=sys.stderr,
            )

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "trace-context": '{"laneId": "PRT.visit"}',
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
            ),
        }
        if self._cookies:
            headers["Cookie"] = self._cookies
        return headers

    def _url(self, method: str) -> str:
        return f"{BASE_URL}/{_PATH_PREFIX}/{method}"

    def _post(self, method: str, body: Dict[str, Any]) -> Dict[str, Any]:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._url(method),
            data=payload,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SEC) as resp:
                text = resp.read().decode("utf-8", errors="replace")
                status = getattr(resp, "status", 200)
                if status >= 400:
                    print(f"[WARN] HTTP {status} for {method}", file=sys.stderr)
                    print(text[:2000], file=sys.stderr)
                    return {"error_http_status": status, "body": text}
                return json.loads(text) if text.strip() else {}
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="replace") if e.fp else ""
            print(f"[WARN] HTTP {e.code} for {method}", file=sys.stderr)
            print(text[:2000], file=sys.stderr)
            return {"error_http_status": e.code, "body": text}
        except urllib.error.URLError as e:
            print(f"[ERROR] 网络错误 for {method}: {e.reason}", file=sys.stderr)
            return {"error": f"network_error: {e.reason}"}

    # ------------------------------------------------------------------
    # getAiVisitState
    # 字段命名：camelCase outer
    # ------------------------------------------------------------------
    def state(self, active_skill: str = "cancel", plan_id: str = "") -> int:
        # [WARN] cancel 场景：传 planId，不传 sessionCreatedAt（proto 注释明确：create 场景必传，其他场景不传）
        if not plan_id:
            print("[ERROR] --plan-id 必传（cancel 场景用 planId 初始化会话，不用 sessionCreatedAt）", file=sys.stderr)
            return 1
        body: Dict[str, Any] = {
            "chatId": self.chat_id,
            "activeSkill": active_skill,
            "app": self.app,
            "planId": plan_id,
        }
        data = self._post("getAiVisitState", body)
        _print_result(data, self.session_created_at, self.chat_id)

        if not _biz_ok(data):
            print("[ERROR] getAiVisitState 失败，禁止继续空转 exec", file=sys.stderr)
            return 1

        state = _extract_state_blob(data)
        payload = state or (data.get("data") if isinstance(data.get("data"), dict) else data)
        user_id = ""
        if isinstance(payload, dict):
            user_id = str(payload.get("userId") or payload.get("user_id") or "")

        print(
            "[NEXT] 下一步：若 waitingForList 非空 → STOP 追问；"
            "FSM=CONFIRMING 时调 cancel 执行取消。禁止再次无参 state。",
            file=sys.stderr,
        )
        return 0

    # ------------------------------------------------------------------
    # batchResolveAiVisitSlot（cancel 场景用于确认取消原因等槽位）
    # ------------------------------------------------------------------
    def batch_resolve(
        self,
        items_json: str,
        plan_id: Optional[str] = None,
        auto_patch: bool = True,
        active_skill: str = "cancel",
    ) -> int:
        raw_items = json.loads(items_json)
        if not isinstance(raw_items, list):
            print("[ERROR] --items 必须是 JSON 数组", file=sys.stderr)
            return 1
        if not raw_items:
            print("[ERROR] --items 不能为空", file=sys.stderr)
            return 1

        items = []
        for it in raw_items:
            slot_name = it.get("slotName") or it.get("slot_name", "")
            items.append({"slotName": slot_name, "raw": it.get("raw", "")})

        body: Dict[str, Any] = {
            "chatId": self.chat_id,
            "activeSkill": active_skill,
            "sessionCreatedAt": self.session_created_at,
            "autoPatch": auto_patch,
            "items": items,
        }
        if plan_id:
            body["planId"] = plan_id

        data = self._post("batchResolveAiVisitSlot", body)
        _print_result(data, self.session_created_at, self.chat_id)

        if not _biz_ok(data):
            print("[ERROR] batchResolveAiVisitSlot 失败，禁止重试空转", file=sys.stderr)
            return 1

        results = data.get("results") or []
        state = data.get("state") if isinstance(data.get("state"), dict) else {}
        print(f"\n--- 解析摘要 ---", file=sys.stderr)
        for r in results:
            sn = r.get("slotName") or r.get("slot_name", "?")
            st = r.get("status", "?")
            em = r.get("errMsg", "")
            line = f"  {sn}: {st}"
            if em:
                line += f"  ({em})"
            print(line, file=sys.stderr)
        wfl = state.get("waitingForList") or []
        fsm = state.get("fsmState") or "?"
        print(f"  fsmState={fsm}, waitingForList={wfl}", file=sys.stderr)
        return 0

    # ------------------------------------------------------------------
    # queryAiVisit（通用 query）
    # 字段命名：全 camelCase outer
    # ------------------------------------------------------------------
    def query(self, query_type: str, param: str = "", limit: int = 20) -> int:
        body: Dict[str, Any] = {
            "queryType": query_type,
            "param": param,
            "limit": limit,
        }
        data = self._post("queryAiVisit", body)
        _print_result(data, self.session_created_at, self.chat_id)
        return 0

    # ------------------------------------------------------------------
    # queryAiVisit plan_list（取消场景：查询可取消的计划列表）
    # ------------------------------------------------------------------
    def query_plan_list(self, keyword: str = "", limit: int = 20) -> int:
        param_obj: Dict[str, Any] = {"query_scene": "cancel"}  # 只返回可取消状态的计划
        if keyword:
            param_obj["keyword"] = keyword
        param_str = json.dumps(param_obj, ensure_ascii=False)
        body: Dict[str, Any] = {
            "queryType": "plan_list",
            "limit": limit,
            "param": param_str,
        }
        data = self._post("queryAiVisit", body)
        # 解析摘要
        plans: List[Dict[str, Any]] = []
        try:
            data_json = data.get("dataJson") or data.get("data_json")
            parsed = json.loads(data_json) if isinstance(data_json, str) and data_json else {}
            plans = parsed.get("list") or []
        except Exception:
            plans = []

        # 多条结果时自动构建 visit-plan-result-card _suggestedCard
        suggested_card: Optional[str] = None
        if len(plans) > 1:
            suggested_card = self._build_result_card(plans)

        _print_result(data, self.session_created_at, self.chat_id, suggested_card=suggested_card)

        print(f"\n--- plan-list 摘要 ---", file=sys.stderr)
        print(f"  返回条数={len(plans)}", file=sys.stderr)
        for i, item in enumerate(plans, 1):
            pid = item.get("visitPlanId", "?")
            biz = item.get("businessLicense", "?")
            date = item.get("planVisitDate", "?")
            status = item.get("status", "?")
            print(f"  {i}. [{pid}] {biz} | {date} | status={status}", file=sys.stderr)
        if suggested_card:
            print(f"  → 多条结果，已自动构建 visit-plan-result-card _suggestedCard", file=sys.stderr)
        return 0

    # ------------------------------------------------------------------
    # visitPurpose code → 中文 label
    # ------------------------------------------------------------------
    _VISIT_PURPOSE_CODE_TO_LABEL: Dict[str, str] = {
        "customer_maintenance": "客情维护",
        "meeting_new_customer": "会议场景/新客开拓方向的制定及跟进",
        "meeting_agent": "会议场景/新代理商/服务商建联",
        "meeting_product_training": "会议场景/产品培训",
        "meeting_review": "会议场景/复盘及分享",
        "meeting_framework": "会议场景/框架跟进",
        "meeting_policy": "会议场景/政策方案解读",
        "meeting_project": "会议场景/项目提案",
        "meeting_audit_training": "会议场景/审核培训",
        "meeting_key_customer": "会议场景/重点客户增量策略",
        "meeting_other": "会议场景/其他会议",
        "onsite_daily": "驻场支持/日常驻场",
        "onsite_big_event": "驻场支持/大场驻场",
        "onsite_other": "驻场支持/其他驻场",
        "other": "其他",
    }

    # ------------------------------------------------------------------
    # queryVisitPlanDetails — 查询单条计划富字段（同 visit-plan-create 一致）
    # 接口: POST /rest/apicenter/visit/web/visitNew/queryVisitPlanDetails
    # ------------------------------------------------------------------
    def _fetch_plan_detail(self, plan_id: str) -> Optional[Dict[str, Any]]:
        """调 queryVisitPlanDetails，返回 data（VisitPlanInfoPb），失败返回 None"""
        body: Dict[str, Any] = {
            "id": plan_id,
        }
        visit_new_url = BASE_URL + "/rest/apicenter/visit/web/visitNew/queryVisitPlanDetails"
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            visit_new_url,
            data=payload,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SEC) as resp:
                text = resp.read().decode("utf-8", errors="replace")
                raw = json.loads(text) if text.strip() else {}
        except Exception as e:
            print(f"[WARN] queryVisitPlanDetails 失败: {e}", file=sys.stderr)
            return None

        base_resp = raw.get("baseResponse") or raw.get("base_response") or {}
        code = base_resp.get("code")
        if code not in (0, "0", None):
            print(f"[WARN] queryVisitPlanDetails code={code} msg={base_resp.get('msg', '')}", file=sys.stderr)
            return None

        detail = raw.get("data") or {}
        if not isinstance(detail, dict):
            return None
        return detail

    # ------------------------------------------------------------------
    # 构建 cancel-visit-plan-card（字段解析与 visit-plan-create 完全一致）
    # ------------------------------------------------------------------
    def _build_cancel_card_from_detail(
        self, plan_id: str, detail: Optional[Dict[str, Any]]
    ) -> str:
        """
        从 queryVisitPlanDetails 返回的 VisitPlanInfoPb 构建 cancel-visit-plan-card。
        字段来源规则与 visit-plan-create._build_created_card_from_detail 完全一致。
        额外字段：planStatus 固定 "cancellable"，targetUrl 自动拼接。
        """
        import datetime

        if not isinstance(detail, dict):
            detail = {}

        basic = detail.get("basicInfo") or detail.get("basic_info") or {}
        checkin = detail.get("visitCheckinInfo") or detail.get("visit_checkin_info") or {}
        plan_info = detail

        # --- customerName ---
        customer_name = (
            basic.get("businessLicense") or basic.get("business_license")
            or basic.get("storeName") or basic.get("store_name")
            or plan_info.get("agentName") or plan_info.get("agent_name")
            or plan_info.get("providerName") or plan_info.get("provider_name")
            or ""
        )

        # --- visitObject 翻译 ---
        _VISIT_OBJECT_CODE_TO_LABEL = {
            "ad_customer": "广告客户",
            "store": "门店",
            "agent": "广告代理商",
            "provider": "团购服务商",
        }
        visit_object_raw = basic.get("visitObject") or basic.get("visit_object") or ""
        visit_object = _VISIT_OBJECT_CODE_TO_LABEL.get(visit_object_raw, visit_object_raw)

        # --- businessLicenseCode ---
        # 广告客户 → businessLicenseCode；门店 → storePoiId；代理商 → agentId；服务商 → providerId
        biz_license_code = (
            basic.get("businessLicenseCode") or basic.get("business_license_code")
            or checkin.get("storePoiId") or checkin.get("store_poi_id")
            or (
                str(plan_info.get("agentId") or plan_info.get("agent_id") or "")
                if ("代理商" in visit_object or visit_object_raw == "agent") else ""
            )
            or (
                str(plan_info.get("providerId") or plan_info.get("provider_id") or "")
                if ("服务商" in visit_object or visit_object_raw == "provider") else ""
            )
            or ""
        )

        # --- customerTypeLabel ---
        is_new_raw = basic.get("isNewCustomer") or basic.get("is_new_customer") or "0"
        is_new = str(is_new_raw) == "1"
        if "广告客户" in visit_object:
            customer_type_label = "广告客户(新)" if is_new else "广告客户(老)"
        elif "门店" in visit_object or "团购门店" in visit_object:
            customer_type_label = "门店(新)" if is_new else "门店(老)"
        elif "代理商" in visit_object or "广告代理商" in visit_object:
            customer_type_label = "广告代理商"
        elif "服务商" in visit_object or "团购服务商" in visit_object:
            customer_type_label = "团购服务商"
        else:
            customer_type_label = visit_object or ("新客" if is_new else "老客")

        # --- visitPurposeLabel ---
        purpose_code = basic.get("visitPurpose") or basic.get("visit_purpose") or ""
        purpose_label = self._VISIT_PURPOSE_CODE_TO_LABEL.get(purpose_code, purpose_code)

        # --- planVisitTimeLabel (ms → yyyy年M月d日) ---
        plan_time_ms = checkin.get("planVisitTime") or checkin.get("plan_visit_time")
        if plan_time_ms:
            try:
                dt = datetime.datetime.fromtimestamp(
                    int(plan_time_ms) / 1000,
                    tz=datetime.timezone(datetime.timedelta(hours=8)),
                )
                plan_visit_time_label = f"{dt.year}年{dt.month}月{dt.day}日"
            except Exception:
                plan_visit_time_label = str(plan_time_ms)
        else:
            plan_visit_time_label = ""

        # --- planVisitAddress ---
        addr_obj = checkin.get("planVisitAddress") or checkin.get("plan_visit_address") or {}
        plan_visit_address = (
            addr_obj.get("address") if isinstance(addr_obj, dict) else str(addr_obj or "")
        ) or ""

        card = {
            "protocol": "visit.session.v1",
            "type": "a2ui.render",
            "card": {
                "card_name": "cancel-visit-plan-card",
                "schema_version": "1.0",
                "data": {
                    "visitPlanId": str(plan_id),
                    "customerName": customer_name,
                    "businessLicenseCode": biz_license_code,
                    "customerTypeLabel": customer_type_label,
                    "visitPurposeLabel": purpose_label,
                    "planVisitTimeLabel": plan_visit_time_label,
                    "planVisitAddress": plan_visit_address,
                    "planStatus": "cancellable",
                    "targetUrl": (
                        f"locallifevisit://krn?bundleId=LocalLifeVisit"
                        f"&componentName=VisitDetailV2&themeStyle=3"
                        f"&visitPlanId={plan_id}"
                    ),
                    "pcTargetUrl": f"/visit/tools/visit?visitId={plan_id}&action=detail",
                },
                "action_states": {
                    "cancel_plan": {"enabled": True, "visible": True},
                },
            },
        }
        return (
            "<<<VISIT_BLOCK_V1>>>"
            + json.dumps(card, ensure_ascii=False, separators=(",", ":"))
            + "<<<END_VISIT_BLOCK_V1>>>"
        )

    # ------------------------------------------------------------------
    # 构建 visit-plan-result-card（候选计划列表）
    # ------------------------------------------------------------------
    def _build_result_card(self, plans: List[Dict[str, Any]]) -> str:
        """从 plan_list 返回的 plans 数组构建 visit-plan-result-card 协议块。"""
        import datetime

        _VISIT_OBJECT_CODE_TO_LABEL = {
            "ad_customer": "广告客户",
            "store": "门店",
            "agent": "广告代理商",
            "provider": "团购服务商",
        }

        items = []
        for plan in plans:
            visit_plan_id = str(plan.get("visitPlanId") or plan.get("id") or "")
            customer_name = plan.get("businessLicense") or plan.get("customerName") or ""
            visit_object_raw = plan.get("visitObject") or plan.get("visit_object") or ""
            customer_type_label = _VISIT_OBJECT_CODE_TO_LABEL.get(visit_object_raw, visit_object_raw)
            # planVisitTime → MM月DD日
            plan_time_ms = plan.get("planVisitTime") or plan.get("plan_visit_time")
            if plan_time_ms:
                try:
                    dt = datetime.datetime.fromtimestamp(
                        int(plan_time_ms) / 1000,
                        tz=datetime.timezone(datetime.timedelta(hours=8)),
                    )
                    plan_visit_time_label = f"{dt.month:02d}月{dt.day:02d}日"
                except Exception:
                    plan_visit_time_label = str(plan_time_ms)
            else:
                plan_visit_time_label = ""
            plan_visit_address = plan.get("planVisitAddress") or plan.get("plan_visit_address") or ""
            items.append({
                "visitPlanId": visit_plan_id,
                "customerName": customer_name,
                "customerTypeLabel": customer_type_label,
                "planVisitTimeLabel": plan_visit_time_label,
                "planVisitAddress": plan_visit_address,
            })

        card = {
            "protocol": "visit.session.v1",
            "type": "a2ui.render",
            "card": {
                "card_name": "visit-plan-result-card",
                "schema_version": "1.0",
                "data": {
                    "total": len(items),
                    "hasMore": False,
                    "items": items,
                },
                "action_states": {
                    "select_plan": {"enabled": True, "visible": True},
                },
            },
        }
        return (
            "<<<VISIT_BLOCK_V1>>>"
            + json.dumps(card, ensure_ascii=False, separators=(",", ":"))
            + "<<<END_VISIT_BLOCK_V1>>>"
        )

    # ------------------------------------------------------------------
    # 构建 action_result + visit-plan-cancelled-card（取消成功/失败双块）
    # ------------------------------------------------------------------
    def _build_cancelled_cards(
        self,
        plan_id: str,
        success: bool,
        client_action_id: str,
        card_id: str,
        plan_data: Dict[str, Any],
        error_code: str = "",
        error_message: str = "",
        retryable: bool = False,
    ) -> str:
        """构建 cancel_plan 写操作返回的两个协议块（action_result + visit-plan-cancelled-card）。

        成功时输出两块（action_result + 跟进卡），失败时只输出 action_result。
        """
        # planStatus: 成功 → cancelled；失败不可重试 → unavailable；失败可重试 → cancellable
        if success:
            plan_status = "cancelled"
        elif retryable:
            plan_status = "cancellable"
        else:
            plan_status = "unavailable"

        # 从 plan_data 提取回填字段（前端 input.data 原样回填）
        visit_plan_id = str(plan_data.get("visitPlanId") or plan_id)
        customer_name = plan_data.get("customerName") or ""
        biz_license_code = plan_data.get("businessLicenseCode") or ""
        customer_type_label = plan_data.get("customerTypeLabel") or ""
        purpose_label = plan_data.get("visitPurposeLabel") or ""
        time_label = plan_data.get("planVisitTimeLabel") or ""
        address = plan_data.get("planVisitAddress") or ""

        action_result = {
            "protocol": "visit.session.v1",
            "type": "a2ui.action_result",
            "client_action_id": client_action_id,
            "target": {
                "card_id": card_id,
                "card_name": "cancel-visit-plan-card",
            },
            "action_id": "cancel_plan",
            "skill_name": "visit-plan-cancel-v1",
            "skill_result": {
                "success": success,
                "code": "SUCCESS" if success else error_code,
                "message": "拜访计划已取消" if success else error_message,
                "data": {
                    "visitPlanId": visit_plan_id,
                    "customerName": customer_name,
                    "businessLicenseCode": biz_license_code,
                    "customerTypeLabel": customer_type_label,
                    "visitPurposeLabel": purpose_label,
                    "planVisitTimeLabel": time_label,
                    "planVisitAddress": address,
                    "planStatus": plan_status,
                },
            },
        }
        block1 = (
            "<<<VISIT_BLOCK_V1>>>"
            + json.dumps(action_result, ensure_ascii=False, separators=(",", ":"))
            + "<<<END_VISIT_BLOCK_V1>>>"
        )

        # 成功时追加第二块跟进卡
        if success:
            follow_up_card = {
                "protocol": "visit.session.v1",
                "type": "a2ui.render",
                "card": {
                    "card_name": "visit-plan-cancelled-card",
                    "schema_version": "1.0",
                    "data": {
                        "title": "已取消该计划，您可继续",
                        "actions": [
                            {"label": "取消其他拜访计划", "value": "cancel-another-visit-plan"},
                            {"label": "新建拜访计划", "value": "create-visit-plan"},
                        ],
                    },
                    "action_states": {},
                },
            }
            block2 = (
                "<<<VISIT_BLOCK_V1>>>"
                + json.dumps(follow_up_card, ensure_ascii=False, separators=(",", ":"))
                + "<<<END_VISIT_BLOCK_V1>>>"
            )
            return block1 + "\n" + block2

        return block1

    # ------------------------------------------------------------------
    # 字段命名：全 camelCase（统一规范）
    # ------------------------------------------------------------------
    def execute_cancel(
        self,
        plan_id: str,
        params_json: str = "",
        client_action_id: str = "",
        card_id: str = "",
        card_data_json: str = "",
    ) -> int:
        # 1. 自动初始化会话（cancel 场景必须先调 getAiVisitState 创建 Redis session）
        state_body: Dict[str, Any] = {
            "chatId": self.chat_id,
            "activeSkill": "cancel",
            "app": self.app,
            "planId": plan_id,
        }
        state_data = self._post("getAiVisitState", state_body)
        if not _biz_ok(state_data):
            print("[ERROR] getAiVisitState 初始化失败，无法执行取消", file=sys.stderr)
            return 1

        # 2. 执行取消（sessionCreatedAt 由 __init__ 自动生成，cancel 场景后端不依赖此值标识会话）
        body: Dict[str, Any] = {
            "chatId": self.chat_id,
            "activeSkill": "cancel",
            "planId": plan_id,
            "sessionCreatedAt": self.session_created_at,
            "paramsJson": params_json,
        }
        data = self._post("executeAiVisitAction", body)
        status = data.get("status")

        # 3. 自动构建 _suggestedCard（action_result + 跟进卡）
        # 解析前端 input 里的 card_data（用于回填 skill_result.data）
        card_data: Dict[str, Any] = {}
        if card_data_json:
            try:
                parsed = json.loads(card_data_json)
                if isinstance(parsed, dict):
                    card_data = parsed
            except Exception as e:
                print(f"[WARN] --card-data 解析失败：{e}", file=sys.stderr)

        success = (status == "done")
        error_code = ""
        error_message = ""
        retryable = False
        if not success:
            error_code = str(data.get("errorCode") or data.get("error_code") or "UNKNOWN")
            error_message = str(data.get("errorMsg") or data.get("error_msg") or "取消失败")
            # 根据错误码判断是否可重试
            retryable = error_code in ("VISIT_PLAN_NOT_CANCELLABLE",)

        suggested_card = self._build_cancelled_cards(
            plan_id=plan_id,
            success=success,
            client_action_id=client_action_id,
            card_id=card_id,
            plan_data=card_data,
            error_code=error_code,
            error_message=error_message,
            retryable=retryable,
        )

        _print_result(data, self.session_created_at, self.chat_id, suggested_card=suggested_card)
        if success:
            print(f"\n[OK] 取消成功！", file=sys.stderr)
        else:
            print(f"\n[ERROR] 取消失败：{error_message}", file=sys.stderr)
            return 1
        return 0

# ------------------------------------------------------------------
# CLI / Runtime Context resolver
# ------------------------------------------------------------------

def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--chat-id",
        default=None,
        help="对话 ID；不传则用 AGENT_RUNTIME_CONTEXT / RUN_CONTEXT.sessionId",
    )
    p.add_argument(
        "--session-created-at",
        type=int,
        default=None,
        help="会话毫秒时间戳；不落盘，须由 Agent 从上一次 _clientMeta 回传",
    )
    p.add_argument("--app", default="visit", help="应用标识，默认 visit")
    p.add_argument(
        "--cookies",
        default=None,
        help="Cookie；不传则用 运行上下文中的 bizContext.cookie（New-Agent 推荐不传）",
    )

def _make_client(a, require_session_created_at: bool = False) -> VisitClient:
    ctx = _load_runtime_context()
    chat_id = _resolve_chat_id(getattr(a, "chat_id", None), ctx)
    cookies = _resolve_cookies(getattr(a, "cookies", None), ctx)
    missing = []
    if not chat_id:
        missing.append("chat-id（或 runtime.sessionId）")
    if missing:
        raise SystemExit(
            "缺少必要上下文: " + "、".join(missing)
            + "。请通过 --chat-id 或运行上下文提供会话标识。"
        )
    return VisitClient(
        chat_id,
        session_created_at=a.session_created_at,
        app=a.app,
        cookies=cookies,
        require_session_created_at=require_session_created_at,
    )

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="AI 拜访助手 — 取消拜访计划调试脚本",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # ---- plan-list（Step 0：查询可取消计划列表）----
    ppl = sub.add_parser("plan-list", help="查询当前用户拜访计划列表（queryAiVisit plan_list）")
    _add_common(ppl)
    ppl.add_argument("--keyword", default="", help="关键词（可选，模糊过滤）")
    ppl.add_argument("--limit", type=int, default=20)
    ppl.set_defaults(handler=lambda a: _make_client(a).query_plan_list(a.keyword, a.limit))

    # ---- plan-detail（Step 0.3：选定计划后补全富字段，构建 cancel-visit-plan-card）----
    ppd = sub.add_parser("plan-detail", help="查询单条拜访计划详情，输出 cancel-visit-plan-card（queryVisitPlanDetails）")
    _add_common(ppd)
    ppd.add_argument("--plan-id", required=True, help="拜访计划 ID")
    def _plan_detail_handler(a: argparse.Namespace) -> int:
        c = _make_client(a)
        detail = c._fetch_plan_detail(a.plan_id)
        if detail is None:
            print("[ERROR] 查询计划详情失败", file=sys.stderr)
            return 1
        suggested = c._build_cancel_card_from_detail(a.plan_id, detail)
        _print_result(detail, c.session_created_at, c.chat_id, suggested_card=suggested)
        return 0
    ppd.set_defaults(handler=_plan_detail_handler)

    # ---- state ----
    ps = sub.add_parser("state", help="读取/初始化会话状态（getAiVisitState）")
    _add_common(ps)
    ps.add_argument("--active-skill", default="cancel",
                    choices=["create", "update", "cancel", "checkin"])
    ps.add_argument("--plan-id", required=True, help="拜访计划 ID（cancel 场景必传，替代 sessionCreatedAt 初始化会话）")
    ps.set_defaults(handler=lambda a: _make_client(a, require_session_created_at=False).state(
        a.active_skill, a.plan_id))

    # ---- batch-resolve ----
    pbr = sub.add_parser("batch-resolve", help="批量解析并回填槽位（batchResolveAiVisitSlot）")
    _add_common(pbr)
    pbr.add_argument(
        "--items", required=True,
        help='槽位列表 JSON，如: [{"slot_name":"cancel_reason","raw":"临时有事"}]',
    )
    pbr.add_argument("--plan-id", default=None, help="拜访计划 ID")
    pbr.add_argument("--auto-patch", default="true", choices=["true", "false"])
    pbr.add_argument("--active-skill", default="cancel",
                     choices=["create", "update", "cancel", "checkin"])
    pbr.set_defaults(
        handler=lambda a: _make_client(a, require_session_created_at=True).batch_resolve(
            a.items,
            plan_id=a.plan_id,
            auto_patch=(a.auto_patch == "true"),
            active_skill=a.active_skill,
        )
    )

    # ---- query（通用只读）----
    pq = sub.add_parser("query", help="只读查询（queryAiVisit）")
    _add_common(pq)
    pq.add_argument("--query-type", required=True,
                    choices=["plan_list", "plan_detail", "today_plans",
                             "pending_travel", "nearby_match", "city_unvisited"])
    pq.add_argument("--param", default="")
    pq.add_argument("--limit", type=int, default=20)
    pq.set_defaults(handler=lambda a: _make_client(a).query(a.query_type, a.param, a.limit))

    # ---- cancel ----
    pc = sub.add_parser("cancel", help="执行取消拜访计划（executeAiVisitAction active_skill=cancel）")
    _add_common(pc)
    pc.add_argument("--plan-id", required=True, help="拜访计划 ID")
    pc.add_argument("--params", default="", help="扩展参数 JSON")
    pc.add_argument("--client-action-id", default="",
                    help="前端 Prompt 中的 clientActionId（camelCase），原样回传到 action_result.client_action_id")
    pc.add_argument("--card-id", default="",
                    help="前端 Prompt 中的 target.card_id，原样回传到 action_result.target.card_id")
    pc.add_argument("--card-data", default="",
                    help='前端卡片 data 对象 JSON（含 visitPlanId/customerName 等，原样回填到 skill_result.data），如: {"visitPlanId":"...","customerName":"..."}')
    pc.set_defaults(handler=lambda a: _make_client(a, require_session_created_at=False).execute_cancel(
        a.plan_id, a.params, a.client_action_id, a.card_id, a.card_data))

    return p

def main() -> int:
    try:
        parser = build_parser()
        args = parser.parse_args()
        return args.handler(args)
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        print(f"[ERROR] 错误: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
