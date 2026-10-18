"""
AI 拜访助手 — 修改拜访计划脚本
仅使用 Python 标准库（无第三方依赖）。
"""
import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

BASE_URL = "http://127.0.0.1:8080"
_PATH_PREFIX = "rest/apicenter/visit/web/visitCenter"
_HTTP_TIMEOUT_SEC = 120

# 写操作重试配置
_WRITE_RETRY_ATTEMPTS = 2  # 写操作失败最多重试 2 次（总共 3 次请求）
_WRITE_RETRY_DELAY_SEC = 3  # 重试间隔
_CONFIRM_FSM = frozenset({"CONFIRMING"})


def _parse_jsonish(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (dict, list, int, float, bool)):
        return raw
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return raw


def _slot_value(slots: Dict[str, Any], name: str) -> Any:
    slot = slots.get(name) or {}
    if not isinstance(slot, dict):
        return None
    return _parse_jsonish(slot.get("valueJson") or slot.get("value_json"))


def _enum_field(parsed: Any, key: str, default: Any = None) -> Any:
    if isinstance(parsed, dict):
        return parsed.get(key, default)
    return default if parsed is None else parsed


def _time_to_ms(parsed: Any) -> Optional[int]:
    if parsed is None:
        return None
    if isinstance(parsed, (int, float)):
        n = int(parsed)
        return n if n > 10_000_000_000 else n * 1000
    if isinstance(parsed, dict):
        for k in ("value", "timestamp", "time"):
            if k in parsed:
                return _time_to_ms(parsed[k])
        return None
    if isinstance(parsed, str):
        text = parsed.strip().strip('"')
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S"):
            try:
                from datetime import datetime
                return int(datetime.strptime(text, fmt).timestamp() * 1000)
            except Exception:
                continue
    return None


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


def _waiting_empty(state: Dict[str, Any]) -> bool:
    wfl = state.get("waitingForList")
    if wfl is None:
        wfl = state.get("waiting_for_list")
    return wfl is None or wfl == [] or wfl == ""


def _ambiguous_empty(data: Dict[str, Any], state: Dict[str, Any]) -> bool:
    pending = data.get("pendingAmbiguousSlot")
    if pending is None:
        pending = data.get("pending_ambiguous_slot")
    if pending is None:
        pending = state.get("pendingAmbiguousSlot") or state.get("pending_ambiguous_slot")
    return pending in (None, "", [])


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
        # 计算协议块内 JSON 的末尾 } 数量，供 AI 自检
        json_body = suggested_card
        for prefix in ("<<<VISIT_BLOCK_V1>>>",):
            if json_body.startswith(prefix):
                json_body = json_body[len(prefix):]
        for suffix in ("<<<END_VISIT_BLOCK_V1>>>",):
            if json_body.endswith(suffix):
                json_body = json_body[:-len(suffix)]
        brace_count = 0
        for c in reversed(json_body.rstrip()):
            if c == "}":
                brace_count += 1
            else:
                break
        brace_hint = "}" * brace_count
        out["_suggestedCardRule"] = (
            "整条对用户回复必须且只能等于 _suggestedCard 字符串；"
            "禁止 Markdown/表格/emoji/自然语言确认或成功文案；"
            "禁止增删改任何字符，必须 100% 原样输出；"
            f"自检：协议块末尾 {brace_hint} 数量必须是 {brace_count} 个，你输出前数一下，多一个或少一个都会导致前端解析失败。"
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


# ------------------------------------------------------------------
# 卡片生成辅助
# ------------------------------------------------------------------

_VISIT_OBJECT_LABEL: Dict[str, str] = {
    "ad_customer": "广告客户",
    "agent":       "广告代理商",
    "store":       "门店",
    "provider":    "团购服务商",
}


def _build_plan_result_card(plans: List[Dict[str, Any]]) -> str:
    """根据 plan 列表生成 visit-plan-result-card 协议块字符串（多条结果时使用）。"""
    import datetime
    items = []
    for p in plans:
        ts = p.get("planVisitTime")
        try:
            if ts:
                dt = datetime.datetime.fromtimestamp(int(ts) / 1000)
                time_label = dt.strftime("%m月%d日")
            else:
                time_label = p.get("planVisitDate", "")
        except Exception:
            time_label = p.get("planVisitDate", "")
        obj_type = p.get("visitObjectType") or p.get("visitObject") or ""
        type_label = _VISIT_OBJECT_LABEL.get(obj_type, obj_type)
        obj_info = p.get("visitObjectInfo") or {}
        src_field = _LICENSE_NAME_SOURCE.get(obj_type, "licenseName")
        customer_name = (
            obj_info.get(src_field)
            or obj_info.get("licenseName")
            or p.get("customerName")
            or p.get("businessLicense")
            or ""
        )
        items.append({
            "visitPlanId":        str(p.get("visitPlanId", "")),
            "customerName":       customer_name,
            "customerTypeLabel":  type_label,
            "planVisitTimeLabel": time_label,
            "planVisitAddress":   p.get("planVisitAddress", ""),
        })
    card = {
        "protocol": "visit.session.v1",
        "type": "a2ui.render",
        "card": {
            "card_name": "visit-plan-result-card",
            "schema_version": "1.0",
            "data": {"total": len(items), "hasMore": False, "items": items},
            "action_states": {"select_plan": {"enabled": True, "visible": True}},
        },
    }
    return (
        "<<<VISIT_BLOCK_V1>>>"
        + json.dumps(card, ensure_ascii=False, separators=(",", ":"))
        + "<<<END_VISIT_BLOCK_V1>>>"
    )


# visitObjectType → licenseName 来源字段名（plan-list 返回数据中）
_LICENSE_NAME_SOURCE: Dict[str, str] = {
    "ad_customer": "licenseName",
    "agent":       "agentName",
    "provider":    "providerName",
    "store":       "storePoiName",
}


def _build_obj_info_from_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """从 plan 数据构建标准 visitObjectInfo，统一 licenseName 填充。"""
    obj_info_raw = plan.get("visitObjectInfo") or {}
    obj_type = plan.get("visitObjectType", "")
    # licenseName 统一：agent/provider/store 把对应名称字段复制到 licenseName
    license_name = obj_info_raw.get("licenseName") or ""
    src_field = _LICENSE_NAME_SOURCE.get(obj_type)
    if src_field and src_field != "licenseName":
        license_name = obj_info_raw.get(src_field) or license_name
    return {
        "isNewCustomer":      obj_info_raw.get("isNewCustomer", 0),
        "licenseName":        license_name,
        "licenseCode":        obj_info_raw.get("licenseCode"),
        "agentId":            obj_info_raw.get("agentId"),
        "agentName":          obj_info_raw.get("agentName") or "",
        "storePoiId":         obj_info_raw.get("storePoiId"),
        "storePoiName":       obj_info_raw.get("storePoiName"),
        "storeCustomerId":    obj_info_raw.get("storeCustomerId"),
        "storeCustomerName":  obj_info_raw.get("storeCustomerName"),
        "providerId":         obj_info_raw.get("providerId"),
        "providerName":       obj_info_raw.get("providerName"),
        "isTargetCustomer":   obj_info_raw.get("isTargetCustomer", 0),
        "targetCustomerId":   obj_info_raw.get("targetCustomerId") or "",
        "targetCustomerName": obj_info_raw.get("targetCustomerName"),
    }


def _build_snapshot_from_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """从 plan 数据构建 updatedData/originalData 快照（全字段）。"""
    return {
        "visitType":          plan.get("visitType", ""),
        "travelApplyNo":      plan.get("travelApplyNo"),
        "visitObjectType":    plan.get("visitObjectType", ""),
        "visitObjectInfo":    _build_obj_info_from_plan(plan),
        "visitPurposeLabel":  plan.get("visitPurposeLabel", ""),
        "planVisitTime":      plan.get("planVisitTime"),
        "planVisitAddress":   plan.get("planVisitAddress", ""),
        "planVisitLatitude":  plan.get("planVisitLatitude"),
        "planVisitLongitude": plan.get("planVisitLongitude"),
        "accompanyUserName":  plan.get("accompanyUserName") or [],
    }


def _build_edit_visit_plan_card(plan: Dict[str, Any]) -> str:
    """根据 plan 数据生成 edit-visit-plan-card 协议块字符串。

    updatedData 与 originalData 完全相同，均来自 plan 数据；
    AI 后续修改时只更新 updatedData 中对应字段。
    """
    snapshot = _build_snapshot_from_plan(plan)
    card = {
        "protocol": "visit.session.v1",
        "type": "a2ui.render",
        "card": {
            "card_name": "edit-visit-plan-card",
            "schema_version": "1.0",
            "data": {
                "updatedData": snapshot,
                "originalData": snapshot,
            },
            "action_states": {
                "confirm_edit_visit_plan": {"enabled": True, "visible": True},
                "switch_plan":             {"enabled": True, "visible": True},
            },
        },
    }
    return (
        "<<<VISIT_BLOCK_V1>>>"
        + json.dumps(card, ensure_ascii=False, separators=(",", ":"))
        + "<<<END_VISIT_BLOCK_V1>>>"
    )


# slot name → visitObjectInfo 中的名称字段（batch-resolve 返回的 corporationName/poiName 映射）
_SLOT_OBJ_INFO_KEYS: Dict[str, str] = {
    "visit_object_info": "",   # 特殊处理
}

# modifyFields 枚举 → 哪些顶层字段会被覆盖（用于 updatedData 合并）
_MODIFY_FIELD_TOP_KEYS: Dict[str, list] = {
    "visitType":          ["visitType"],
    "travelApplyNo":      ["travelApplyNo"],
    "visitPurposeLabel":  ["visitPurposeLabel"],
    "planVisitTime":      ["planVisitTime"],
    "planVisitAddress":   ["planVisitAddress", "planVisitLatitude", "planVisitLongitude"],
    "visitObjectType":    ["visitObjectType", "visitObjectInfo"],
    "customerType":       ["visitObjectInfo"],  # isNewCustomer
    "businessLicense":    ["visitObjectInfo"],
    "store":              ["visitObjectInfo"],
    "accompanyUserName":  ["accompanyUserName"],
}


def _extract_slot_value(slots: Dict[str, Any], slot_name: str) -> Any:
    """从 state.slots 取指定槽位的解析值（对齐 create 脚本的 _slot_value）。

    proto 格式：slot = {"valueJson": "<JSON 字符串>", "confirmed": true}
    valueJson 里可能是 JSON 字符串（如 '"travel"'）或 JSON 对象字符串（如 '{"value":"travel"}'）
    """
    sv = slots.get(slot_name) or {}
    if not isinstance(sv, dict):
        return None
    return _parse_jsonish(sv.get("valueJson") or sv.get("value_json"))


def _apply_slots_to_snapshot(
    base: Dict[str, Any],
    slots: Dict[str, Any],
    modify_fields: list,
) -> Dict[str, Any]:
    """用 state.slots 的值覆盖 base 快照，返回 updatedData。

    """
    import copy
    updated = copy.deepcopy(base)

    # ---- 1. visit_object_info：只要 slots 里有就全部回填 ----
    info_raw = _extract_slot_value(slots, "visit_object_info")
    if isinstance(info_raw, dict):
        new_obj = dict(updated.get("visitObjectInfo") or {})
        corp = info_raw.get("corporationName") or info_raw.get("displayName") or info_raw.get("licenseName") or ""
        license_no = info_raw.get("licenseNo") or info_raw.get("licenseCode") or info_raw.get("creditCode") or ""

        # isNewCustomer：优先从 customer_type 槽位读
        ct_raw = _extract_slot_value(slots, "customer_type")
        ct_code = _enum_field(ct_raw, "value") if isinstance(ct_raw, dict) else (ct_raw or "")
        is_new = 1 if ct_code == "new" else 0
        if is_new == 0:
            ct_val = info_raw.get("customerType") or info_raw.get("customer_type")
            if ct_val in ("new", "新客") or info_raw.get("isNewCustomer") in (1, True, "1"):
                is_new = 1

        # 通用字段
        if corp:
            new_obj["licenseName"] = corp
        if license_no:
            new_obj["licenseCode"] = license_no
        new_obj["isNewCustomer"] = is_new

        # agent 专属
        if info_raw.get("agentId"):
            new_obj["agentId"] = info_raw["agentId"]
        agent_name = info_raw.get("agentName") or corp
        if agent_name:
            new_obj["agentName"] = agent_name
            new_obj["licenseName"] = agent_name  # agent 场景 licenseName 与 agentName 保持一致
        if license_no and not info_raw.get("agentId"):
            new_obj["licenseCode"] = license_no

        # store 专属
        _store_poi_id = info_raw.get("poiId") or info_raw.get("storePoiId")
        if _store_poi_id:
            new_obj["storePoiId"] = _store_poi_id
        _poi_name = info_raw.get("poiName") or info_raw.get("storePoiName") or corp
        if _poi_name:
            new_obj["storePoiName"] = _poi_name
        if info_raw.get("storeCustomerId"):
            new_obj["storeCustomerId"] = info_raw["storeCustomerId"]
        if info_raw.get("storeCustomerName"):
            new_obj["storeCustomerName"] = info_raw["storeCustomerName"]
        if info_raw.get("storeCustomerCity"):
            new_obj["storeCustomerCity"] = info_raw["storeCustomerCity"]

        # provider 专属：providerName 固定取 corporationName，不取槽位的 providerName
        if info_raw.get("providerId"):
            new_obj["providerId"] = info_raw["providerId"]
        elif "providerId" in info_raw:
            new_obj["providerId"] = ""  # 槽位有 key 但值为空，传空串
        if corp:
            new_obj["providerName"] = corp  # 固定用 corporationName
            new_obj["licenseName"] = corp   # provider 场景 licenseName 与 providerName 保持一致

        # ad_customer 靶向
        if info_raw.get("isTargetCustomer") in (1, True, "1"):
            new_obj["isTargetCustomer"] = 1
            new_obj["targetCustomerId"] = info_raw.get("targetCustomerId") or info_raw.get("quotaId") or ""
            new_obj["targetCustomerName"] = info_raw.get("targetCustomerName") or info_raw.get("quotaName") or ""
        elif "isTargetCustomer" in info_raw:
            new_obj["isTargetCustomer"] = 0

        # createSource
        if info_raw.get("createSource"):
            new_obj["createSource"] = info_raw["createSource"]

        # 类型切换清理：根据候选的 visitObjectType 清空不属于当前类型的字段
        # 防止原计划的其他类型字段残留在 updatedData 中
        _candidate_type = info_raw.get("visitObjectType") or ""
        if _candidate_type == "store":
            # store 场景：清空 ad_customer/agent/provider 字段
            for _k in ("licenseName", "licenseCode", "agentId", "agentName",
                       "providerId", "providerName", "isTargetCustomer",
                       "targetCustomerId", "targetCustomerName"):
                if _k in new_obj:
                    new_obj[_k] = 0 if _k == "isTargetCustomer" else ""
            print(f"[apply-slots] store 场景清空非门店字段（licenseName/agentName 等）", file=sys.stderr)
        elif _candidate_type in ("ad_customer", "agent", "provider"):
            # 非门店场景：清空 store 字段
            for _k in ("storePoiId", "storePoiName", "storeCustomerId",
                       "storeCustomerName", "storeCustomerCity"):
                if _k in new_obj:
                    new_obj[_k] = ""
            print(f"[apply-slots] {_candidate_type} 场景清空门店字段（storePoiId 等）", file=sys.stderr)

        updated["visitObjectInfo"] = new_obj

    # ---- 2. visit_object_type：如果 slots 里有就回填 ----
    vot_raw = _extract_slot_value(slots, "visit_object_type")
    new_type = _enum_field(vot_raw, "value") if isinstance(vot_raw, dict) else vot_raw
    if new_type:
        updated["visitObjectType"] = new_type
        # store 场景：visitObjectType 不应出现在 modifyFields，从列表中移除
        if new_type == "store" and "visitObjectType" in modify_fields:
            modify_fields.remove("visitObjectType")
            print("[build-confirm-card] store 场景自动去除 modifyFields 中的 visitObjectType", file=sys.stderr)

    # ---- 3. visit_type ----
    vt_raw = _extract_slot_value(slots, "visit_type")
    vt_val = _enum_field(vt_raw, "value") if isinstance(vt_raw, dict) else vt_raw
    if vt_val is not None:
        updated["visitType"] = vt_val

    # ---- 4. travel_apply_no ----
    travel_raw = _extract_slot_value(slots, "travel_apply_no")
    if isinstance(travel_raw, dict):
        travel_val = travel_raw.get("value") or travel_raw.get("travelApplyNo")
        if travel_val is not None:
            updated["travelApplyNo"] = travel_val
    elif travel_raw is not None:
        updated["travelApplyNo"] = travel_raw

    # ---- 5. visit_purpose / visitPurposeLabel ----
    purpose_raw = _extract_slot_value(slots, "visit_purpose")
    if isinstance(purpose_raw, dict):
        label = str(purpose_raw.get("label") or "")
        parent = str(purpose_raw.get("parentLabel") or "")
        updated["visitPurposeLabel"] = f"{parent}/{label}" if parent else label
    elif purpose_raw is not None:
        updated["visitPurposeLabel"] = str(purpose_raw)

    # ---- 6. plan_visit_time ----
    time_raw = _extract_slot_value(slots, "plan_visit_time")
    ms = _time_to_ms(time_raw)
    if ms is not None:
        updated["planVisitTime"] = ms

    # ---- 7. plan_visit_address ----
    addr_raw = _extract_slot_value(slots, "plan_visit_address")
    if isinstance(addr_raw, dict):
        addr = addr_raw.get("address") or addr_raw.get("poiName") or ""
        if addr:
            updated["planVisitAddress"] = addr
        if addr_raw.get("latitude") is not None:
            updated["planVisitLatitude"] = addr_raw["latitude"]
        if addr_raw.get("longitude") is not None:
            updated["planVisitLongitude"] = addr_raw["longitude"]
    elif addr_raw is not None:
        updated["planVisitAddress"] = str(addr_raw)

    # ---- 8. accompany_user_name ----
    accompany_raw = _extract_slot_value(slots, "accompany_user_name")
    if accompany_raw is not None:
        updated["accompanyUserName"] = accompany_raw if isinstance(accompany_raw, list) else [accompany_raw]

    return updated


def _build_confirm_edit_plan_card(
    original_data: Dict[str, Any],
    updated_data: Dict[str, Any],
    modify_fields: list,
) -> str:
    """生成 confirm-edit-visit-plan-card 协议块字符串。"""
    card = {
        "protocol": "visit.session.v1",
        "type": "a2ui.render",
        "card": {
            "card_name": "confirm-edit-visit-plan-card",
            "schema_version": "1.0",
            "data": {
                "updatedData":  updated_data,
                "originalData": original_data,
                "modifyFields": modify_fields,
            },
            "action_states": {
                "confirm_edit_plan": {"enabled": True, "visible": True},
                "switch_plan":       {"enabled": True, "visible": True},
            },
        },
    }
    return (
        "<<<VISIT_BLOCK_V1>>>"
        + json.dumps(card, ensure_ascii=False, separators=(",", ":"))
        + "<<<END_VISIT_BLOCK_V1>>>"
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
        except socket.timeout:
            print(f"[ERROR] 超时（{_HTTP_TIMEOUT_SEC}s）for {method}", file=sys.stderr)
            return {"error": f"timeout: {_HTTP_TIMEOUT_SEC}s"}

    def _post_write(self, method: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """写操作 POST，带重试（网络超时/5xx 自动重试，业务错误不重试）"""
        import time as _time
        last_result = {}
        for attempt in range(1, _WRITE_RETRY_ATTEMPTS + 2):  # 最多 3 次
            result = self._post(method, body)
            last_result = result
            # 成功或有业务错误（4xx/业务 code≠0）→ 不重试
            if "error" not in result or "error_http_status" in result:
                if attempt > 1:
                    print(f"[重试] 第 {attempt} 次请求成功", file=sys.stderr)
                return result
            # 网络/超时错误 → 重试
            is_timeout = "timeout" in result.get("error", "")
            is_network = "network_error" in result.get("error", "")
            if (is_timeout or is_network) and attempt <= _WRITE_RETRY_ATTEMPTS + 1:
                print(f"[重试] 第 {attempt} 次失败({result['error']})，{_WRITE_RETRY_DELAY_SEC}s 后重试...", file=sys.stderr)
                _time.sleep(_WRITE_RETRY_DELAY_SEC)
                continue
            return result
        return last_result

    # ------------------------------------------------------------------
    # getAiVisitState
    # ------------------------------------------------------------------
    def state(self, active_skill: str = "update", plan_id: str = "") -> int:
        # [WARN] update 场景：传 planId，不传 sessionCreatedAt（proto 注释明确：create 场景必传，其他场景不传）
        if not plan_id:
            print("[ERROR] --plan-id 必传（update 场景用 planId 初始化会话，不用 sessionCreatedAt）", file=sys.stderr)
            return 1
        body: Dict[str, Any] = {
            "chatId": self.chat_id,
            "activeSkill": active_skill,
            "app": self.app,
            "planId": plan_id,
        }
        data = self._post("getAiVisitState", body)
        state = _extract_state_blob(data)
        suggested = None
        if _biz_ok(data) and _ambiguous_empty(data, state):
            fsm = state.get("fsmState") or state.get("fsm_state")
            if fsm in _CONFIRM_FSM and _waiting_empty(state):
                suggested = None  # update 场景无确认卡，直接展示摘要
        _print_result(data, self.session_created_at, self.chat_id, suggested_card=suggested)

        if not _biz_ok(data):
            print("[ERROR] getAiVisitState 失败，禁止继续空转 exec", file=sys.stderr)
            return 1

        payload = state or (data.get("data") if isinstance(data.get("data"), dict) else data)
        user_id = ""
        if isinstance(payload, dict):
            user_id = str(payload.get("userId") or payload.get("user_id") or "")
        if not user_id:
            print(
                "[WARN] 返回 userId 为空：请检查 Cookie 是否与当前用户一致",
                file=sys.stderr,
            )

        print(
            "[NEXT] 下一步：若 waitingForList 非空且无新槽位 → STOP 追问；"
            "有槽位则带同一 --session-created-at 调一次 batch-resolve。"
            "禁止再次无参 state。",
            file=sys.stderr,
        )
        return 0

    # ------------------------------------------------------------------
    # batchResolveAiVisitSlot
    # items 内必须用 slotName（camelCase），slot_name 会被后端忽略！
    # ------------------------------------------------------------------
    def batch_resolve(
        self,
        items_json: str,
        plan_id: Optional[str] = None,
        auto_patch: bool = True,
        active_skill: str = "update",
    ) -> int:
        raw_items = json.loads(items_json)
        if not isinstance(raw_items, list):
            print("[ERROR] --items 必须是 JSON 数组", file=sys.stderr)
            return 1
        if not raw_items:
            print("[ERROR] --items 不能为空", file=sys.stderr)
            return 1

        # [WARN] items 里的字段必须用 slotName（camelCase），slot_name（snake）会被后端忽略
        items = []
        for it in raw_items:
            slot_name = it.get("slotName") or it.get("slot_name", "")
            items.append({"slotName": slot_name, "raw": it.get("raw", "")})

        # 脚本自动补充 visit_object_type 槽位，让后端写入 state。
        # 优先级：1) Agent 已传 → 不覆盖；2) 当前 state 已确认的类型 → 保持；3) 原计划类型 → 兜底
        _has_vot_from_agent = any(it.get("slotName") == "visit_object_type" for it in items)
        if plan_id and not _has_vot_from_agent:
            _vot_to_insert = ""
            # 优先从当前 state 读已确认的 visit_object_type
            try:
                state_body: Dict[str, Any] = {
                    "chatId": self.chat_id,
                    "activeSkill": active_skill,
                    "app": self.app,
                    "planId": plan_id,
                }
                state_resp = self._post("getAiVisitState", state_body)
                if _biz_ok(state_resp):
                    state_blob = _extract_state_blob(state_resp)
                    if state_blob and isinstance(state_blob, dict):
                        vot_slot = (state_blob.get("slots") or {}).get("visit_object_type")
                        if vot_slot:
                            vot_vj = vot_slot.get("valueJson") or ""
                            if vot_vj:
                                try:
                                    vot_parsed = json.loads(vot_vj) if isinstance(vot_vj, str) else vot_vj
                                    _vot_to_insert = vot_parsed.get("value", "") if isinstance(vot_parsed, dict) else str(vot_parsed)
                                except (json.JSONDecodeError, TypeError):
                                    _vot_to_insert = str(vot_vj).strip('"')
                        if _vot_to_insert:
                            print(f"[batch-resolve] 从 state 读到已确认的 visit_object_type={_vot_to_insert}，保持不变", file=sys.stderr)
            except Exception as e:
                print(f"[batch-resolve] [WARN] 读 state 失败({e})，降级查原计划", file=sys.stderr)
            # state 里没有，降级查原计划
            if not _vot_to_insert:
                try:
                    detail_body: Dict[str, Any] = {
                        "chatId": self.chat_id,
                        "queryType": "plan_detail",
                        "param": str(plan_id),
                    }
                    detail_resp = self._post("queryAiVisit", detail_body)
                    plans = (detail_resp.get("data") or {}).get("plans") or []
                    if not plans:
                        djs = detail_resp.get("dataJson") or detail_resp.get("data_json", "")
                        if djs:
                            try:
                                pd = json.loads(djs)
                                plans = pd.get("list") or [pd] if isinstance(pd, dict) else []
                            except Exception:
                                pass
                    if plans:
                        _vot_to_insert = plans[0].get("visitObjectType") or plans[0].get("visitObject") or ""
                    if _vot_to_insert:
                        print(f"[batch-resolve] 从原计划读到 visit_object_type={_vot_to_insert}，兜底插入", file=sys.stderr)
                except Exception as e:
                    print(f"[batch-resolve] [WARN] 查原计划 visitObjectType 失败({e})，不插入兜底槽位", file=sys.stderr)
            if _vot_to_insert:
                items.insert(0, {"slotName": "visit_object_type", "raw": _vot_to_insert})

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
        state = data.get("state") if isinstance(data.get("state"), dict) else {}
        _print_result(data, self.session_created_at, self.chat_id)

        if not _biz_ok(data):
            print("[ERROR] batchResolveAiVisitSlot 失败，禁止重试空转", file=sys.stderr)
            return 1

        results = data.get("results") or []
        print(f"\n--- 解析摘要 ---", file=sys.stderr)
        for r in results:
            sn = r.get("slotName") or r.get("slot_name", "?")
            st = r.get("status", "?")
            em = r.get("errMsg", "")
            line = f"  {sn}: {st}"
            if em:
                line += f"  ({em})"
            print(line, file=sys.stderr)
        wfl = state.get("waitingForList") or state.get("waiting_for_list") or []
        fsm = state.get("fsmState") or "?"
        print(f"  fsmState={fsm}, waitingForList={wfl}", file=sys.stderr)
        return 0

    # ------------------------------------------------------------------
    # queryAiVisit（plan-list 专用：支持丰富筛选参数）
    # ------------------------------------------------------------------
    def plan_list(
        self,
        keyword: str = "",
        plan_visit_date: str = "",
        visit_object_type: str = "",
        visit_type: str = "",
        visit_mode: str = "",
        limit: int = 10,
    ) -> int:
        param_obj: Dict[str, str] = {}
        if keyword:
            param_obj["keyword"] = keyword
        if plan_visit_date:
            param_obj["plan_visit_date"] = plan_visit_date
        if visit_object_type:
            param_obj["visit_object_type"] = visit_object_type
        if visit_type:
            param_obj["visit_type"] = visit_type
        if visit_mode:
            param_obj["visit_mode"] = visit_mode
        param_obj["query_scene"] = "update"  # 只返回可修改状态的计划
        body: Dict[str, Any] = {
            "queryType": "plan_list",
            "limit": limit,
        }
        if param_obj:
            body["param"] = json.dumps(param_obj, ensure_ascii=False)
        data = self._post("queryAiVisit", body)
        # 注入 visitPurposeLabel：把每条计划的 visitPurpose code 转为中文，供 edit-visit-plan-card 使用
        # 注入 visitPurposeLabel / accompanyUserName，同时检查是否只有 1 条（可直接出卡）
        suggested_card: Optional[str] = None
        try:
            data_json_str = data.get("dataJson") or data.get("data_json", "")
            if data_json_str:
                parsed_data = json.loads(data_json_str)
                for item in (parsed_data.get("list") or []):
                    code = item.get("visitPurpose") or ""
                    if code:
                        # 始终用 code → 完整中文映射覆盖，后端返回的 visitPurposeLabel 可能缺一级分类前缀
                        item["visitPurposeLabel"] = self._VISIT_PURPOSE_CODE_TO_LABEL.get(code, code)
                    # accompanyUserIds → accompanyUserName（字段重命名，供 edit-visit-plan-card 使用）
                    if "accompanyUserName" not in item:
                        item["accompanyUserName"] = item.get("accompanyUserIds") or []
                data["dataJson"] = json.dumps(parsed_data, ensure_ascii=False)
                # 无论 1 条或多条，统一生成 visit-plan-result-card，让用户先确认选定再继续
                # 禁止只有 1 条时直接跳到 edit-visit-plan-card
                lst = parsed_data.get("list") or []
                if len(lst) >= 1:
                    suggested_card = _build_plan_result_card(lst)
        except Exception:
            pass
        _print_result(data, self.session_created_at, self.chat_id, suggested_card=suggested_card)
        # 解析摘要
        data_json = data.get("dataJson") or data.get("data_json", "")
        if data_json:
            try:
                parsed = json.loads(data_json)
                total = parsed.get("total", "?")
                lst = parsed.get("list") or []
                print(f"\n--- plan-list 摘要 ---", file=sys.stderr)
                print(f"  total={total}, 返回条数={len(lst)}", file=sys.stderr)
                for i, item in enumerate(lst, 1):
                    pid = item.get("visitPlanId", "?")
                    biz = item.get("businessLicense", "?")
                    date = item.get("planVisitDate", "?")
                    addr = item.get("planVisitAddress", "?")
                    status = item.get("status", "?")
                    print(f"  {i}. [{pid}] {biz} | {date} | {addr} | status={status}", file=sys.stderr)
                if suggested_card:
                    if len(lst) == 1:
                        print(f"  → 单条结果，已生成 edit-visit-plan-card（AI 直接输出 _suggestedCard）", file=sys.stderr)
                    else:
                        print(f"  → 多条结果，已生成 visit-plan-result-card（AI 直接输出 _suggestedCard）", file=sys.stderr)
            except Exception:
                pass
        return 0

    # ------------------------------------------------------------------
    # queryAiVisit（通用 query）
    # ------------------------------------------------------------------
    def query(self, query_type: str, param: str = "", limit: int = 10) -> int:
        body: Dict[str, Any] = {
            "queryType": query_type,
            "limit": limit,
        }
        if param:
            body["param"] = param
        data = self._post("queryAiVisit", body)
        _print_result(data, self.session_created_at, self.chat_id)
        return 0

    # ------------------------------------------------------------------
    # plan-card：按 planId 拉单条计划 → 生成 edit-visit-plan-card
    # 多条 plan-list 后用户选定某条时使用
    # ------------------------------------------------------------------
    def plan_card(self, plan_id: str) -> int:
        body: Dict[str, Any] = {
            "queryType": "plan_detail",
            "param": plan_id,
            "limit": 1,
        }
        data = self._post("queryAiVisit", body)
        if not _biz_ok(data):
            _print_result(data, self.session_created_at, self.chat_id)
            return 1
        # 从 dataJson 取计划数据
        suggested_card: Optional[str] = None
        try:
            data_json_str = data.get("dataJson") or data.get("data_json", "")
            if data_json_str:
                plan = json.loads(data_json_str)
                # plan_detail 后端可能返回 visitPurposeLabel 但可能缺一级分类前缀，统一用 code 覆盖
                purpose_code = plan.get("visitPurpose") or ""
                if purpose_code:
                    plan["visitPurposeLabel"] = self._VISIT_PURPOSE_CODE_TO_LABEL.get(purpose_code, purpose_code)
                if "accompanyUserName" not in plan:
                    plan["accompanyUserName"] = plan.get("accompanyUserIds") or []
                suggested_card = _build_edit_visit_plan_card(plan)
        except Exception:
            pass
        if not suggested_card:
            print(f"[WARN] plan_id={plan_id} 未能生成卡片，请检查计划是否存在", file=sys.stderr)
            _print_result(data, self.session_created_at, self.chat_id)
            return 1
        _print_result(data, self.session_created_at, self.chat_id, suggested_card=suggested_card)
        return 0

    # ------------------------------------------------------------------
    # build-confirm-card：所有槽位 ok 后构建 confirm-edit-visit-plan-card
    # 脚本负责拉原计划、合并槽位值、处理名称/id 转换、组装确认卡
    # ------------------------------------------------------------------
    def build_confirm_card(
        self,
        plan_id: str,
        modify_fields_str: str,
        accompany_user_name_json: str = "",
    ) -> int:
        # 1. 解析 modify_fields
        modify_fields = [f.strip() for f in modify_fields_str.split(",") if f.strip()]
        if not modify_fields:
            print("[ERROR] --modify-fields 不能为空", file=sys.stderr)
            return 1

        # 解析陪访人覆盖值（用于混合修改场景：既改槽位字段又改陪访人）
        accompany_override: Optional[List[str]] = None
        if accompany_user_name_json:
            try:
                accompany_override = json.loads(accompany_user_name_json)
                if not isinstance(accompany_override, list):
                    print("[ERROR] --accompany-user-name 必须是 JSON 数组", file=sys.stderr)
                    return 1
            except Exception as e:
                print(f"[ERROR] --accompany-user-name 解析失败：{e}", file=sys.stderr)
                return 1

        # 校验 modify_fields：必须是合法枚举值，防止 AI 误传槽位名或嵌套 JSON
        _VALID_MODIFY_FIELDS = {
            "visitType", "travelApplyNo", "visitPurposeLabel", "planVisitTime",
            "planVisitAddress", "visitObjectType", "customerType", "businessLicense",
            "store", "accompanyUserName",
        }
        invalid = [f for f in modify_fields if f not in _VALID_MODIFY_FIELDS]
        if invalid:
            _SLOT_TO_MODIFY_FIELD = {
                "visit_object_info": "businessLicense",
                "visit_object_type": "visitObjectType",
                "visit_purpose": "visitPurposeLabel",
                "plan_visit_time": "planVisitTime",
                "plan_visit_address": "planVisitAddress",
                "customer_type": "customerType",
                "accompany_user_name": "accompanyUserName",
                "travel_apply_no": "travelApplyNo",
                "visit_type": "visitType",
            }
            corrected = []
            for f in modify_fields:
                if f in _VALID_MODIFY_FIELDS:
                    corrected.append(f)
                elif f in _SLOT_TO_MODIFY_FIELD:
                    corrected.append(_SLOT_TO_MODIFY_FIELD[f])
                    print(f"[build-confirm-card] 自动纠正 modifyFields: {f} → {_SLOT_TO_MODIFY_FIELD[f]}", file=sys.stderr)
                else:
                    try:
                        parsed = json.loads(f)
                        if isinstance(parsed, list):
                            for item in parsed:
                                item_str = str(item).strip()
                                if item_str in _SLOT_TO_MODIFY_FIELD:
                                    corrected.append(_SLOT_TO_MODIFY_FIELD[item_str])
                                elif item_str in _VALID_MODIFY_FIELDS:
                                    corrected.append(item_str)
                    except (json.JSONDecodeError, TypeError):
                        pass
            if corrected:
                print(f"[build-confirm-card] 修正后 modifyFields: {modify_fields} → {corrected}", file=sys.stderr)
                modify_fields = corrected
            else:
                print(f"[ERROR] --modify-fields 含非法值: {invalid}，合法值: {sorted(_VALID_MODIFY_FIELDS)}", file=sys.stderr)
                return 1

        # 2. 从 getAiVisitState 获取结构化 slots（key 已是 snake_case，与 AiSlotEnum.getCode() 一致）
        slots: Dict[str, Any] = {}
        if plan_id:
            try:
                state_body: Dict[str, Any] = {
                    "chatId": self.chat_id,
                    "activeSkill": "update",
                    "app": self.app,
                    "planId": plan_id,
                }
                state_resp = self._post("getAiVisitState", state_body)
                if _biz_ok(state_resp):
                    state_blob = _extract_state_blob(state_resp)
                    if state_blob and isinstance(state_blob, dict):
                        # slots 的 key 是 snake_case（如 visit_object_type, plan_visit_address），
                        # value 是 {"valueJson": "<JSON字符串>", "confirmed": true}
                        # 直接用，与创建脚本一致
                        slots = dict(state_blob.get("slots") or {})
                    print(f"[build-confirm-card] 从 getAiVisitState 获取到 {len(slots)} 个槽位", file=sys.stderr)
                else:
                    print(f"[build-confirm-card] [WARN] getAiVisitState 返回非成功，slots 可能为空", file=sys.stderr)
            except Exception as e:
                print(f"[build-confirm-card] [WARN] getAiVisitState 获取失败({e})", file=sys.stderr)

        # 3. 拉原计划数据（originalData）
        body: Dict[str, Any] = {
            "queryType": "plan_detail",
            "param": plan_id,
            "limit": 1,
        }
        data = self._post("queryAiVisit", body)
        if not _biz_ok(data):
            print(f"[ERROR] 查询原计划失败 plan_id={plan_id}", file=sys.stderr)
            _print_result(data, self.session_created_at, self.chat_id)
            return 1
        try:
            data_json_str = data.get("dataJson") or data.get("data_json", "")
            plan = json.loads(data_json_str) if data_json_str else {}
            purpose_code = plan.get("visitPurpose") or ""
            if purpose_code:
                plan["visitPurposeLabel"] = self._VISIT_PURPOSE_CODE_TO_LABEL.get(purpose_code, purpose_code)
            if "accompanyUserName" not in plan:
                plan["accompanyUserName"] = plan.get("accompanyUserIds") or []
        except Exception as e:
            print(f"[ERROR] 原计划数据解析失败：{e}", file=sys.stderr)
            return 1

        # 3. 构建 originalData 快照
        original_data = _build_snapshot_from_plan(plan)

        # 4. 用 slots 覆盖 updatedData（按 modifyFields）
        updated_data = _apply_slots_to_snapshot(original_data, slots, modify_fields)

        # 4.5 陪访人覆盖（混合修改场景：既改槽位字段又改陪访人）
        if accompany_override is not None:
            updated_data["accompanyUserName"] = accompany_override
            if "accompanyUserName" not in modify_fields:
                modify_fields.append("accompanyUserName")
                print(f"[build-confirm-card] 陪访人覆盖，追加 modifyFields: accompanyUserName", file=sys.stderr)

        # 5. 生成确认卡
        suggested_card = _build_confirm_edit_plan_card(original_data, updated_data, modify_fields)
        print(f"[build-confirm-card] plan_id={plan_id} modifyFields={modify_fields}", file=sys.stderr)
        _print_result(data, self.session_created_at, self.chat_id, suggested_card=suggested_card)
        return 0

    # ------------------------------------------------------------------
    # executeAiVisitAction（update）
    # 字段命名：全 camelCase（统一规范）
    # sessionCreatedAt 固定传 0（update 场景）
    # ------------------------------------------------------------------
    def execute_update(self, plan_id: str, params_json: str = "") -> int:
        body: Dict[str, Any] = {
            "chatId": self.chat_id,
            "activeSkill": "update",
            "planId": plan_id,
            "sessionCreatedAt": 0,
            "paramsJson": params_json,
        }
        data = self._post("executeAiVisitAction", body)
        _print_result(data, self.session_created_at, self.chat_id)
        status = data.get("status")
        if status == "done":
            result_plan_id = data.get("planId") or data.get("plan_id", "?")
            print(f"\n[OK] 修改成功！planId={result_plan_id}", file=sys.stderr)
        elif status == "error":
            em = data.get("errorMsg") or data.get("error_msg", "未知错误")
            print(f"\n[ERROR] 修改失败：{em}", file=sys.stderr)
            return 1
        elif status in ("confirm", "fallback"):
            print(f"\n[WARN] 需要二次确认或降级: {data.get('errorMsg', '')}", file=sys.stderr)
            return 2
        return 0

    # ------------------------------------------------------------------
    # updateVisitPlan（direct-update）
    # edit-visit-plan-card 专用：前端已填好所有字段，直接提交，跳过槽位流程
    # ------------------------------------------------------------------

    # visitPurpose code → 中文 label（plan-list 返回时注入，供 edit-visit-plan-card 直接展示）
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

    # visitPurposeLabel（中文）→ visitPurpose code 映射表（来自 VisitPurposeEnum）
    # visitPurposeLabel → visitPurpose code 映射表
    # 前端可能传两种格式：
    #   1. 纯 desc：如 "大场驻场"（后端 queryAiVisit 返回的 visitPurposeLabel）
    #   2. category/desc：如 "驻场支持/大场驻场"（前端卡片选择器拼接格式）
    # 两种格式都需要覆盖；"新代理商/服务商建联" desc 本身含 /，不能用 rsplit 处理，须显式列出
    _VISIT_PURPOSE_LABEL_TO_CODE: Dict[str, str] = {
        # --- 纯 desc 格式 ---
        "客情维护": "customer_maintenance",
        "新客开拓方向的制定及跟进": "meeting_new_customer",
        "新代理商/服务商建联": "meeting_agent",
        "产品培训": "meeting_product_training",
        "复盘及分享": "meeting_review",
        "框架跟进": "meeting_framework",
        "政策方案解读": "meeting_policy",
        "项目提案": "meeting_project",
        "审核培训": "meeting_audit_training",
        "重点客户增量策略": "meeting_key_customer",
        "其他会议": "meeting_other",
        "日常驻场": "onsite_daily",
        "大场驻场": "onsite_big_event",
        "其他驻场": "onsite_other",
        "其他": "other",
        # --- category/desc 格式（前端卡片选择器传值）---
        "会议场景/新客开拓方向的制定及跟进": "meeting_new_customer",
        "会议场景/新代理商/服务商建联": "meeting_agent",
        "会议场景/产品培训": "meeting_product_training",
        "会议场景/复盘及分享": "meeting_review",
        "会议场景/框架跟进": "meeting_framework",
        "会议场景/政策方案解读": "meeting_policy",
        "会议场景/项目提案": "meeting_project",
        "会议场景/审核培训": "meeting_audit_training",
        "会议场景/重点客户增量策略": "meeting_key_customer",
        "会议场景/其他会议": "meeting_other",
        "驻场支持/日常驻场": "onsite_daily",
        "驻场支持/大场驻场": "onsite_big_event",
        "驻场支持/其他驻场": "onsite_other",
        # --- code 自映射（前端有时直接传英文 code）---
        "customer_maintenance": "customer_maintenance",
        "meeting_new_customer": "meeting_new_customer",
        "meeting_agent": "meeting_agent",
        "meeting_product_training": "meeting_product_training",
        "meeting_review": "meeting_review",
        "meeting_framework": "meeting_framework",
        "meeting_policy": "meeting_policy",
        "meeting_project": "meeting_project",
        "meeting_audit_training": "meeting_audit_training",
        "meeting_key_customer": "meeting_key_customer",
        "meeting_other": "meeting_other",
        "onsite_daily": "onsite_daily",
        "onsite_big_event": "onsite_big_event",
        "onsite_other": "onsite_other",
        "other": "other",
    }

    # ------------------------------------------------------------------
    # searchAccompanyUserV（搜索陪访人，供修改计划时替换陪访人使用）
    # ------------------------------------------------------------------
    def search_accompany_user(self, keyword: str = "", limit: int = 30) -> int:
        """搜索陪访人列表。

        输出格式（stdout JSON，供 AI 读取）：
        {
          "list": [
            {"userId": 123, "userName": "张三", "userCode": "zhangsan01",
             "department": "销售部", "position": "BD", "avatar": "https://..."},
            ...
          ]
        }
        """
        body: Dict[str, Any] = {
            "keyword": keyword,
            "limit": limit,
        }
        data = self._post("searchAccompanyUserV", body)
        _print_result(data, self.session_created_at, self.chat_id)

        resp_data = data.get("data") or {}
        lst = resp_data.get("list") or []
        normalized = []
        for item in lst:
            normalized.append({
                "userId": item.get("userId") or item.get("user_id", 0),
                "userName": item.get("userName") or item.get("user_name", ""),
                "userCode": item.get("userCode") or item.get("user_code", ""),
                "department": item.get("department", ""),
                "position": item.get("position", ""),
                "avatar": item.get("avatar", ""),
            })
        print(json.dumps({"list": normalized}, ensure_ascii=False))
        return 0

    # ------------------------------------------------------------------
    # action_result + follow-up card builder
    # ------------------------------------------------------------------
    @staticmethod
    def _build_action_result_blocks(
        action_id: str,
        client_action_id: str,
        card_id: str,
        card_name: str,
        success: bool,
        message: str,
        plan_id: str,
    ) -> str:
        """构建 action_result 协议块 + 可选修改成功跟进卡，返回 _suggestedCard 字符串。"""
        action_result = {
            "protocol": "visit.session.v1",
            "type": "a2ui.action_result",
            "client_action_id": client_action_id,
            "target": {"card_id": card_id, "card_name": card_name},
            "action_id": action_id,
            "skill_name": "visit-plan-update",
            "skill_result": {"success": success, "message": message},
        }
        block1 = (
            "<<<VISIT_BLOCK_V1>>>"
            + json.dumps(action_result, ensure_ascii=False)
            + "<<<END_VISIT_BLOCK_V1>>>"
        )
        if success:
            follow_up = {
                "protocol": "visit.session.v1",
                "type": "a2ui.render",
                "card": {
                    "card_name": "visit-plan-cancelled-card",
                    "schema_version": "1.0",
                    "data": {
                        "visitPlanId": str(plan_id),
                        "title": "已修改成功，您可继续",
                        "actions": [
                            {"label": "当前计划签到", "value": "check-in-current-plan"}
                        ],
                    },
                    "action_states": {"action_press": {"enabled": True, "visible": True}},
                },
            }
            block2 = (
                "<<<VISIT_BLOCK_V1>>>"
                + json.dumps(follow_up, ensure_ascii=False)
                + "<<<END_VISIT_BLOCK_V1>>>"
            )
            return block1 + "\n" + block2
        return block1

    def execute_direct_update(self, plan_id: str, data_json: str,
                              client_action_id: str = "", card_id: str = "",
                              action_id: str = "confirm_edit_plan",
                              card_name: str = "confirm-edit-visit-plan-card") -> int:
        """
        direct-update：前端卡片 data 就是修改后的值，直接映射为 updateVisitPlan 入参。
        只做必要的格式转换：null→空串、visitPurposeLabel→code、时间戳→datetime。
        """
        try:
            card_data = json.loads(data_json) if isinstance(data_json, str) else data_json
        except Exception as e:
            print(f"[ERROR] --data 解析失败：{e}", file=sys.stderr)
            return 1

        # ---- confirm-edit-visit-plan-card 路径 B：data 是嵌套结构，取 updatedData ----
        # edit-visit-plan-card 路径 A：data 是扁平结构，直接用
        if "updatedData" in card_data:
            print("[direct-update] 检测到 updatedData（路径B confirm-edit-visit-plan-card），提取 updatedData 作为修改值", file=sys.stderr)
            card_data = card_data["updatedData"]

        # ---- 必填字段缺失兜底：从原计划补全 ----
        # 前端 SDK 可能移除未修改的 null 字段，导致 visitType/visitPurposeLabel 缺失
        _required_fields = ["visitType", "visitObjectType", "visitPurposeLabel"]
        missing_required = [f for f in _required_fields if not card_data.get(f)]
        base_plan: Dict[str, Any] = {}
        if missing_required:
            print(f"[direct-update] [WARN] 必填字段缺失: {missing_required}，查原计划补全", file=sys.stderr)
            try:
                detail_body: Dict[str, Any] = {
                    "chatId": self.chat_id,
                    "queryType": "plan_detail",
                    "param": str(plan_id),
                }
                detail_resp = self._post("queryAiVisit", detail_body)
                plans = (detail_resp.get("data") or {}).get("plans") or []
                if plans:
                    base_plan = plans[0]
                    for f in missing_required:
                        if base_plan.get(f) and not card_data.get(f):
                            card_data[f] = base_plan[f]
                            print(f"[direct-update] 补全 {f}={base_plan[f]}", file=sys.stderr)
            except Exception as e:
                print(f"[direct-update] [WARN] 查原计划补全失败：{e}", file=sys.stderr)

        # ---- visitObjectInfo：null → 空串，确保后端不报空 ----
        obj_info = card_data.get("visitObjectInfo") or {}
        clean_obj_info = {
            "isNewCustomer": obj_info.get("isNewCustomer") if obj_info.get("isNewCustomer") is not None else 0,
            "isTargetCustomer": obj_info.get("isTargetCustomer") if obj_info.get("isTargetCustomer") is not None else 0,
            "licenseName": obj_info.get("licenseName") or "",
            "licenseCode": obj_info.get("licenseCode") or "",
            "agentId": obj_info.get("agentId") or "",
            "agentName": obj_info.get("agentName") or "",
            "storePoiId": obj_info.get("storePoiId") or "",
            "storePoiName": obj_info.get("storePoiName") or "",
            "storeCustomerId": obj_info.get("storeCustomerId") or "",
            "storeCustomerName": obj_info.get("storeCustomerName") or "",
            "providerId": obj_info.get("providerId") or "",
            "providerName": obj_info.get("providerName") or "",
            "targetCustomerId": obj_info.get("targetCustomerId") or "",
            "targetCustomerName": obj_info.get("targetCustomerName") or "",
        }

        # ---- visitPurposeLabel → visitPurpose code ----
        purpose_label = card_data.get("visitPurposeLabel") or ""
        visit_purpose_code = self._VISIT_PURPOSE_LABEL_TO_CODE.get(purpose_label, "")
        if not visit_purpose_code:
            # 映射表找不到 → 从原计划的 visitPurpose 取 code
            if base_plan and base_plan.get("visitPurpose"):
                visit_purpose_code = base_plan.get("visitPurpose")
                print(f"[direct-update] [WARN] visitPurposeLabel '{purpose_label}' 不在映射表，用原计划 visitPurpose={visit_purpose_code}", file=sys.stderr)
            else:
                # 原计划也没有 → 查一次原计划
                try:
                    detail_body: Dict[str, Any] = {
                        "chatId": self.chat_id,
                        "queryType": "plan_detail",
                        "param": str(plan_id),
                    }
                    detail_resp = self._post("queryAiVisit", detail_body)
                    plans = (detail_resp.get("data") or {}).get("plans") or []
                    if plans and plans[0].get("visitPurpose"):
                        visit_purpose_code = plans[0]["visitPurpose"]
                        print(f"[direct-update] 查原计划补全 visitPurpose={visit_purpose_code}", file=sys.stderr)
                    else:
                        visit_purpose_code = purpose_label  # 最后兜底：原样传
                        print(f"[direct-update] [WARN] 原计划也查不到 visitPurpose，原样传 '{purpose_label}'", file=sys.stderr)
                except Exception as e:
                    visit_purpose_code = purpose_label
                    print(f"[direct-update] [WARN] 查原计划失败({e})，原样传 '{purpose_label}'", file=sys.stderr)

        # ---- accompanyUserName：白名单过滤，只保留 avatar/userName/userCode ----
        # avatar 为空时自动调 searchAccompanyUserV 补全
        accompany_user_name_raw = card_data.get("accompanyUserName") or []
        ALLOWED_ACCOMPANY_FIELDS = ("avatar", "userName", "userCode")
        accompany_user_name: list = []
        for item in accompany_user_name_raw:
            if isinstance(item, str):
                try:
                    parsed = json.loads(item)
                    if isinstance(parsed, dict):
                        cleaned = {k: parsed[k] for k in ALLOWED_ACCOMPANY_FIELDS if k in parsed}
                        # avatar 为空时自动补全
                        if not cleaned.get("avatar") and cleaned.get("userCode"):
                            try:
                                search_body = {"keyword": cleaned["userCode"], "limit": 1}
                                search_data = self._post("searchAccompanyUserV", search_body)
                                search_list = (search_data.get("data") or {}).get("list") or []
                                if search_list and search_list[0].get("avatar"):
                                    cleaned["avatar"] = search_list[0]["avatar"]
                                    print(f"[direct-update] 补全 avatar: {cleaned.get('userName','')} → {cleaned['avatar'][:50]}...", file=sys.stderr)
                            except Exception as e:
                                print(f"[direct-update] 补全 avatar 失败({e})", file=sys.stderr)
                        if "avatar" not in cleaned:
                            cleaned["avatar"] = ""
                        accompany_user_name.append(json.dumps(cleaned, ensure_ascii=False))
                    else:
                        accompany_user_name.append(item)
                except (json.JSONDecodeError, TypeError):
                    accompany_user_name.append(item)
            else:
                accompany_user_name.append(json.dumps(item, ensure_ascii=False) if not isinstance(item, str) else item)

        # ---- 直接映射前端 data → updateVisitPlan 请求体 ----
        body: Dict[str, Any] = {
            "visitPlanIdStr": str(plan_id),
            "visitType": card_data.get("visitType") or "",
            "visitObjectType": card_data.get("visitObjectType") or "",
            "visitObjectInfo": clean_obj_info,
            "visitPurpose": visit_purpose_code,
            "travelApplyNo": card_data.get("travelApplyNo") or "",
            "planVisitTime": self._ms_to_datetime(card_data.get("planVisitTime")),
            "planVisitAddress": card_data.get("planVisitAddress") or "",
            "planVisitLatitude": card_data.get("planVisitLatitude") or 0.0,
            "planVisitLongitude": card_data.get("planVisitLongitude") or 0.0,
            "accompanyUserName": accompany_user_name,
        }

        print(f"[direct-update] 请求体：{json.dumps(body, ensure_ascii=False)}", file=sys.stderr)

        data = self._post_write("updateVisitPlan", body)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        code = (data.get("baseResponse") or {}).get("code")
        if code in (0, "0", None) and "error_http_status" not in data:
            result_id = data.get("id") or data.get("planId") or plan_id
            print(f"\n[OK] 直接修改成功！planId={result_id}", file=sys.stderr)
            suggested = self._build_action_result_blocks(
                action_id=action_id,
                client_action_id=client_action_id,
                card_id=card_id,
                card_name=card_name,
                success=True,
                message="拜访计划已修改",
                plan_id=str(result_id),
            )
            _print_result({"success": True, "planId": str(result_id)}, self.session_created_at, self.chat_id, suggested_card=suggested)
            print(
                "➡️  下一步：STOP。整条回复原样输出 _suggestedCard（action_result + 修改成功跟进卡）。禁止 Markdown。",
                file=sys.stderr,
            )
            return 0
        else:
            em = (data.get("baseResponse") or {}).get("msg") or data.get("errorMsg") or "未知错误"
            print(f"\n[ERROR] 直接修改失败：{em}", file=sys.stderr)
            suggested = self._build_action_result_blocks(
                action_id=action_id,
                client_action_id=client_action_id,
                card_id=card_id,
                card_name=card_name,
                success=False,
                message=em,
                plan_id=str(plan_id),
            )
            _print_result({"success": False, "error": em}, self.session_created_at, self.chat_id, suggested_card=suggested)
            return 1

    @staticmethod
    def _ms_to_datetime(ms: Any) -> str:
        """毫秒时间戳 → 'YYYY-MM-DD HH:mm:ss' 字符串（updateVisitPlan planVisitTime 字段格式）"""
        if ms is None:
            return ""
        try:
            import datetime
            ts = int(ms) / 1000
            dt = datetime.datetime.fromtimestamp(ts)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(ms)


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
        description="AI 拜访助手 — 修改拜访计划调试脚本",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # ---- plan-list（Step 0：模糊搜索待修改计划）----
    ppl = sub.add_parser("plan-list", help="模糊搜索拜访计划（queryAiVisit query_type=plan_list）")
    _add_common(ppl)
    ppl.add_argument("--keyword", default="", help="关键词（模糊匹配 OR）")
    ppl.add_argument("--plan-visit-date", default="", help="日期过滤，如 2026-7-21")
    ppl.add_argument("--visit-object-type", default="", help="ad_customer / agent / store / provider")
    ppl.add_argument("--visit-type", default="", help="local / travel")
    ppl.add_argument("--visit-mode", default="", help="online / offline")
    ppl.add_argument("--limit", type=int, default=10)
    ppl.set_defaults(
        handler=lambda a: _make_client(a).plan_list(
            a.keyword, a.plan_visit_date, a.visit_object_type,
            a.visit_type, a.visit_mode, a.limit,
        )
    )

    # ---- plan-card（多条 plan-list 用户选定后，按 planId 生成 edit-visit-plan-card）----
    ppc = sub.add_parser("plan-card", help="按 planId 生成 edit-visit-plan-card（queryAiVisit plan_detail）")
    _add_common(ppc)
    ppc.add_argument("--plan-id", required=True, help="拜访计划 ID")
    ppc.set_defaults(handler=lambda a: _make_client(a).plan_card(a.plan_id))

    # ---- build-confirm-card（所有槽位 ok 后生成 confirm-edit-visit-plan-card）----
    pbcc = sub.add_parser(
        "build-confirm-card",
        help="批量槽位 ok 后生成 confirm-edit-visit-plan-card",
    )
    _add_common(pbcc)
    pbcc.add_argument("--plan-id", required=True, help="拜访计划 ID")
    pbcc.add_argument(
        "--modify-fields", required=True,
        help="本次修改的字段枚举，逗号分隔，如 businessLicense,planVisitAddress",
    )
    pbcc.add_argument(
        "--accompany-user-name", default="",
        help='陪访人覆盖值 JSON 数组（混合修改场景：既改槽位又改陪访人时使用），如: ["{\\"avatar\\":\\"...\\",\\"userName\\":\\"连杰\\",\\"userCode\\":\\"lianjie03\\"}"]',
    )
    pbcc.set_defaults(
        handler=lambda a: _make_client(a).build_confirm_card(
            a.plan_id, a.modify_fields, a.accompany_user_name,
        )
    )

    # ---- state ----
    ps = sub.add_parser("state", help="读取会话状态（getAiVisitState）")
    _add_common(ps)
    ps.add_argument("--active-skill", default="update",
                    choices=["create", "update", "cancel", "checkin"])
    ps.add_argument("--plan-id", default=None, help="拜访计划 ID（必传）")
    ps.set_defaults(handler=lambda a: _make_client(a, require_session_created_at=False).state(
        a.active_skill, a.plan_id))
    pbr = sub.add_parser("batch-resolve", help="批量槽位解析（batchResolveAiVisitSlot）")
    _add_common(pbr)
    pbr.add_argument(
        "--items", required=True,
        help='槽位列表 JSON，如: [{"slot_name":"plan_visit_time","raw":"2026-07-22 14:00"}]',
    )
    pbr.add_argument("--plan-id", default=None, help="拜访计划 ID（update 时必传）")
    pbr.add_argument("--auto-patch", default="true", choices=["true", "false"])
    pbr.add_argument("--active-skill", default="update",
                     choices=["create", "update", "cancel", "checkin"])
    pbr.set_defaults(
        handler=lambda a: _make_client(a, require_session_created_at=False).batch_resolve(
            a.items,
            plan_id=a.plan_id,
            auto_patch=(a.auto_patch == "true"),
            active_skill=a.active_skill,
        )
    )

    # ---- update ----
    pu = sub.add_parser("update", help="执行修改（executeAiVisitAction active_skill=update）")
    _add_common(pu)
    pu.add_argument("--plan-id", required=True, help="拜访计划 ID")
    pu.add_argument("--params", default="", help="扩展参数 JSON")
    pu.set_defaults(handler=lambda a: _make_client(a, require_session_created_at=False).execute_update(
        a.plan_id, a.params))

    # ---- direct-update（updateVisitPlan 直接提交，edit-visit-plan-card + confirm-edit-visit-plan-card 专用）----
    pdu = sub.add_parser("direct-update", help="直接调 updateVisitPlan（无需 state/batch-resolve）")
    _add_common(pdu)
    pdu.add_argument("--plan-id", required=True, help="拜访计划 ID（int64 字符串）")
    pdu.add_argument("--data", required=True, help="前端卡片 data 字段的完整 JSON 字符串")
    pdu.add_argument("--client-action-id", default="",
                     help="前端 Prompt 中的 clientActionId（camelCase），原样回传到 action_result.client_action_id")
    pdu.add_argument("--card-id", default="",
                     help="前端 Prompt 中的 target.card_id，原样回传到 action_result.target.card_id")
    pdu.add_argument("--action-id", default="confirm_edit_plan",
                     choices=["confirm_edit_plan", "confirm_edit_visit_plan"],
                     help="action_id：路径B用 confirm_edit_plan（默认），路径A用 confirm_edit_visit_plan")
    pdu.add_argument("--card-name", default="confirm-edit-visit-plan-card",
                     help="target.card_name：路径B用 confirm-edit-visit-plan-card（默认），路径A用 edit-visit-plan-card")
    pdu.set_defaults(
        handler=lambda a: _make_client(a).execute_direct_update(
            a.plan_id, a.data,
            client_action_id=a.client_action_id, card_id=a.card_id,
            action_id=a.action_id, card_name=a.card_name)
    )

    # ---- search-accompany-user（搜索陪访人）----
    psau = sub.add_parser("search-accompany-user", help="搜索陪访人（searchAccompanyUserV）")
    _add_common(psau)
    psau.add_argument("--keyword", default="", help="搜索关键词（姓名/工号）")
    psau.add_argument("--limit", type=int, default=30)
    psau.set_defaults(handler=lambda a: _make_client(a, require_session_created_at=False).search_accompany_user(a.keyword, a.limit))

    # ---- query（通用，plan_detail 等）----
    pq = sub.add_parser("query", help="只读查询（queryAiVisit）")
    _add_common(pq)
    pq.add_argument("--query-type", required=True,
                    choices=["plan_list", "plan_detail", "today_plans",
                             "pending_travel", "nearby_match", "city_unvisited"])
    pq.add_argument("--param", default="", help="plan_detail 时传 plan_id")
    pq.add_argument("--limit", type=int, default=10)
    pq.set_defaults(handler=lambda a: _make_client(a).query(a.query_type, a.param, a.limit))

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
