"""
AI 拜访助手 — 创建拜访计划脚本

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
        # already ms or seconds
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


def _build_visit_object_info(object_type: str, info: Any, slots: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = {
        "isNewCustomer": 0,
        "licenseName": None,
        "licenseCode": None,
        "agentId": None,
        "agentName": None,
        "storePoiId": None,
        "storePoiName": None,
        "storeCustomerId": None,
        "storeCustomerName": None,
        "providerId": None,
        "providerName": None,
        "isTargetCustomer": 0,
        "targetCustomerId": None,
        "targetCustomerName": None,
    }
    if not isinstance(info, dict):
        return base
    corp = info.get("corporationName") or info.get("displayName") or info.get("licenseName")
    license_no = info.get("licenseNo") or info.get("licenseCode") or info.get("creditCode")

    # isNewCustomer 优先从 slots.customer_type 读（新客=1，老客=0），兜底从 info 里读
    is_new = 0
    if slots and isinstance(slots, dict):
        ct_raw = _slot_value(slots, "customer_type")
        ct_code = _enum_field(ct_raw, "value") if isinstance(ct_raw, dict) else (ct_raw or "")
        if ct_code == "new":
            is_new = 1
    if is_new == 0:
        # 兜底：info 里已有后端 enrich 写入的值
        customer_type = info.get("customerType") or info.get("customer_type")
        if customer_type in ("new", "新客") or info.get("isNewCustomer") in (1, True, "1"):
            is_new = 1

    if object_type == "ad_customer":
        base["licenseName"] = corp
        base["licenseCode"] = license_no
        base["isNewCustomer"] = is_new
        if info.get("isTargetCustomer") in (1, True, "1"):
            base["isTargetCustomer"] = 1
            base["targetCustomerId"] = info.get("targetCustomerId") or info.get("quotaId")
            base["targetCustomerName"] = info.get("targetCustomerName") or info.get("quotaName")
    elif object_type == "store":
        base["isNewCustomer"] = is_new
        base["storePoiId"] = info.get("poiId") or info.get("storePoiId")
        base["storePoiName"] = info.get("poiName") or info.get("storePoiName") or corp
        base["storeCustomerId"] = info.get("storeCustomerId")
        base["storeCustomerName"] = info.get("storeCustomerName")
    elif object_type == "agent":
        base["agentId"] = info.get("agentId")
        base["agentName"] = info.get("agentName") or corp
        if license_no and not base["agentId"]:
            base["licenseCode"] = license_no
    elif object_type == "provider":
        base["providerId"] = info.get("providerId") or ""
        base["providerName"] = corp
        base["licenseName"] = corp
    else:
        base["licenseName"] = corp
        base["licenseCode"] = license_no
        base["agentName"] = info.get("agentName")
        base["agentId"] = info.get("agentId")
    return base


def _wrap_protocol(card_payload: Dict[str, Any]) -> str:
    body = json.dumps(card_payload, ensure_ascii=False, separators=(",", ":"))
    return f"<<<VISIT_BLOCK_V1>>>{body}<<<END_VISIT_BLOCK_V1>>>"


def _extract_state_blob(data: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(data.get("state"), dict):
        return data["state"]
    if isinstance(data.get("data"), dict):
        # getAiVisitState often nests under data
        inner = data["data"]
        # proto JSON camelCase: fsmState; snake_case fallback: fsm_state
        if "fsmState" in inner or "fsm_state" in inner or "slots" in inner:
            return inner
    if "fsmState" in data or "fsm_state" in data or "slots" in data:
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


def _build_confirm_suggested_card(state: Dict[str, Any]) -> Optional[str]:
    fsm = state.get("fsmState") or state.get("fsm_state")
    if fsm not in _CONFIRM_FSM or not _waiting_empty(state):
        return None
    slots = state.get("slots") or {}
    if not isinstance(slots, dict):
        return None

    visit_type = _enum_field(_slot_value(slots, "visit_type"), "value")
    object_type = _enum_field(_slot_value(slots, "visit_object_type"), "value")
    # visit_purpose: parentLabel 有值时拼接为 "会议场景/新代理商/服务商建联"，无值时取 label
    purpose_slot = _slot_value(slots, "visit_purpose")
    purpose_label = ""
    purpose_code = ""
    if isinstance(purpose_slot, dict):
        purpose_label = str(purpose_slot.get("label") or "")
        purpose_code = str(purpose_slot.get("value") or "")
        parent_label = str(purpose_slot.get("parentLabel") or "")
        if parent_label:
            purpose_label = f"{parent_label}/{purpose_label}"
    info_raw = _slot_value(slots, "visit_object_info")
    addr = _slot_value(slots, "plan_visit_address")
    time_raw = _slot_value(slots, "plan_visit_time")
    travel = _slot_value(slots, "travel_apply_no")

    travel_apply_no = None
    if visit_type == "travel":
        if isinstance(travel, dict):
            travel_apply_no = travel.get("value") or travel.get("travelApplyNo")
        elif isinstance(travel, str) and travel:
            travel_apply_no = travel

    address = None
    lat = None
    lng = None
    if isinstance(addr, dict):
        address = addr.get("address") or addr.get("poiName")
        lat = addr.get("latitude")
        lng = addr.get("longitude")
    elif isinstance(addr, str):
        address = addr

    visit_obj_info = _build_visit_object_info(str(object_type or ""), info_raw, slots)

    card = {
        "protocol": "visit.session.v1",
        "type": "a2ui.render",
        "card": {
            "card_name": "visit-plan-card",
            "schema_version": "1.0",
            "data": {
                "operationType": "create_plan",
                "visitType": visit_type,
                "travelApplyNo": travel_apply_no,
                "visitObjectType": object_type,
                "visitObjectInfo": visit_obj_info,
                "visitPurposeLabel": purpose_label,
                "planVisitTime": _time_to_ms(time_raw),
                "planVisitAddress": address,
                "planVisitLatitude": lat,
                "planVisitLongitude": lng,
            },
            "action_states": {"create_plan": {"enabled": True, "visible": True}},
        },
    }
    return _wrap_protocol(card)


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
            "检测到可出卡状态：请将 stdout._suggestedCard 原样作为整条回复输出（块外零字符）。",
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
                f" 未传 --session-created-at，本次新生成 {self.session_created_at}；"
                "后续所有 exec 必须回传该值。",
                file=sys.stderr,
            )

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        """
        所有请求都带 trace-context 泳道头，不带 Origin/Referer。
        泳道头让请求路由到正确的 staging 实例（getAiVisitState/batchResolve/execute 都需要）。
        queryAiVisit 虽查 DB，但带泳道后 code=0，用 camelCase 能正确解析 userId 拿到数据。
        """
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
                    print(f" HTTP {status} for {method}", file=sys.stderr)
                    print(text[:2000], file=sys.stderr)
                    return {"error_http_status": status, "body": text}
                return json.loads(text) if text.strip() else {}
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="replace") if e.fp else ""
            print(f" HTTP {e.code} for {method}", file=sys.stderr)
            print(text[:2000], file=sys.stderr)
            return {"error_http_status": e.code, "body": text}
        except urllib.error.URLError as e:
            print(f" 网络错误 for {method}: {e.reason}", file=sys.stderr)
            return {"error": f"network_error: {e.reason}"}
        except socket.timeout:
            print(f"超时（{_HTTP_TIMEOUT_SEC}s）for {method}", file=sys.stderr)
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
    def state(self, active_skill: str = "create", plan_id: Optional[str] = None) -> int:
        """读取当前会话状态，返回 waitingForList / slots / fsmState"""
        body: Dict[str, Any] = {
            "chatId": self.chat_id,
            "activeSkill": active_skill,
            "app": self.app,
            "sessionCreatedAt": self.session_created_at,
        }
        if plan_id:
            body["planId"] = plan_id
        data = self._post("getAiVisitState", body)
        state = _extract_state_blob(data)
        suggested = None
        if _biz_ok(data) and _ambiguous_empty(data, state):
            suggested = _build_confirm_suggested_card(state)
        _print_result(data, self.session_created_at, self.chat_id, suggested_card=suggested)

        if not _biz_ok(data):
            print("[ERROR] getAiVisitState 失败，禁止继续空转 exec", file=sys.stderr)
            return 1

        if suggested:
            print(
                "➡️  下一步：STOP。整条回复原样输出 _suggestedCard（最终确认卡）。禁止 Markdown。",
                file=sys.stderr,
            )
        else:
            print(
                "➡️  下一步：若 waitingForList 非空且无新槽位 → STOP 追问/出卡；"
                "有槽位则带同一 --session-created-at 调一次 batch-resolve。"
                "禁止再次无参 state。",
                file=sys.stderr,
            )
        return 0

    # ------------------------------------------------------------------
    # batchResolveAiVisitSlot
    # ------------------------------------------------------------------
    def batch_resolve(self, items_json: str, auto_patch: bool = True,
                      active_skill: str = "create", plan_id: Optional[str] = None) -> int:
        """批量槽位解析，autoPatch=true 时后端自动写入并推进 FSM

        items_json 格式：[{"slot_name": "visit_type", "raw": "本地"}, ...]
        """
        raw_items = json.loads(items_json)
        if not isinstance(raw_items, list):
            print("[ERROR] --items 必须是 JSON 数组", file=sys.stderr)
            return 1
        if not raw_items:
            print("[ERROR] --items 不能为空", file=sys.stderr)
            return 1

        # [WARN] items 里的字段必须用 slotName（camelCase），slot_name（snake）会被后端忽略
        # 同时支持两种写法，统一转换为 slotName；resolvedValueJson 透传（绕过后端校验用）
        items = []
        for it in raw_items:
            slot_name = it.get("slotName") or it.get("slot_name", "")
            item: Dict[str, Any] = {"slotName": slot_name, "raw": it.get("raw", "")}
            # 支持 resolvedValueJson（camelCase）和 resolved_value_json（snake_case）两种写法
            resolved = it.get("resolvedValueJson") or it.get("resolved_value_json")
            if resolved is not None:
                # resolvedValueJson 告诉后端跳过 resolver，直接写入 slot
                # 后端 slotStr 对非 Map 值直接 toString，裸单号字符串就够，不需要包一层
                if isinstance(resolved, dict):
                    # 已经是 dict（如 {"value": "..."}）→ 序列化成 JSON 字符串
                    item["resolvedValueJson"] = json.dumps(resolved, ensure_ascii=False)
                else:
                    # 裸字符串/数字 → 直接序列化为 JSON 字符串（如 "123123141231"）
                    item["resolvedValueJson"] = json.dumps(str(resolved), ensure_ascii=False)
            items.append(item)

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
        suggested = None
        if _biz_ok(data) and state and _ambiguous_empty(data, state):
            suggested = _build_confirm_suggested_card(state)
        _print_result(data, self.session_created_at, self.chat_id, suggested_card=suggested)

        if not _biz_ok(data):
            print("[ERROR] batchResolveAiVisitSlot 失败，禁止重试空转", file=sys.stderr)
            return 1

        # 解析摘要
        results = data.get("results") or []
        print(f"\n--- 解析摘要 ---", file=sys.stderr)
        for r in results:
            sn = r.get("slotName") or r.get("slot_name", "?")
            st = r.get("status", "?")
            print(f"  {sn}: {st}", file=sys.stderr)
        wfl = state.get("waitingForList") or state.get("waiting_for_list") or []
        fsm = state.get("fsmState") or state.get("fsm_state", "?")
        print(f"  fsmState={fsm}, waitingForList={wfl}", file=sys.stderr)

        # results 空且无 state：通常是 sessionCreatedAt 不一致或身份无效，禁止模型空转重试
        if not results and not state:
            print(
                " batch-resolve 返回 results=[] 且无 state。"
                f"请确认沿用同一 --session-created-at {self.session_created_at}。"
                "禁止再次盲目 state/batch-resolve。",
                file=sys.stderr,
            )
            return 2

        if suggested:
            print(
                " 下一步：STOP。整条回复原样输出 _suggestedCard（最终确认卡）。禁止 Markdown/再 exec。",
                file=sys.stderr,
            )
        else:
            print(
                " 下一步：按 results/state 追问、出卡或（CONFIRMING）create；"
                "禁止无新用户输入时再 exec。",
                file=sys.stderr,
            )
        return 0

    # ------------------------------------------------------------------
    # queryAiVisit
    # ------------------------------------------------------------------
    def query(self, query_type: str, param: str = "", limit: int = 10) -> int:
        """只读查询

        query_type 取值：
        - pending_travel    待补充差旅拜访计划（状态=待填写计划+关联差旅单）
                            返回时自动构建 _suggestedCard（visit-pending-card，只含差旅条目）
        - today_plans           今日待拜访计划列表
        - plan_list             计划列表模糊搜索（param=keyword）
        - plan_detail           计划详情（param=plan_id）
        - nearby_match          按经纬度匹配（param="lng,lat"）
        - city_unvisited        同城市未拜访公司列表（param=城市名）
        """
        body: Dict[str, Any] = {
            "queryType": query_type,
            "limit": limit,
        }
        if param:
            body["param"] = param

        data = self._post("queryAiVisit", body)

        # 解析 dataJson
        data_json_str = data.get("dataJson") or data.get("data_json", "")
        parsed = None
        if data_json_str:
            try:
                parsed = json.loads(data_json_str)
                print(f"\n--- dataJson 解析 ---", file=sys.stderr)
                if isinstance(parsed, dict):
                    total = parsed.get("total", "?")
                    lst = parsed.get("list") or []
                    print(f"  total={total}, list.len={len(lst)}", file=sys.stderr)
                elif isinstance(parsed, list):
                    print(f"  list.len={len(parsed)}", file=sys.stderr)
            except Exception:
                parsed = None

        # pending_travel：自动构建 visit-pending-card 的 _suggestedCard，不走默认 _print_result
        if query_type == "pending_travel":
            if parsed is not None:
                # 兼容 {total, list} 和直接 list 两种形态
                if isinstance(parsed, dict):
                    raw_list = parsed.get("list") or []
                elif isinstance(parsed, list):
                    raw_list = parsed
                else:
                    raw_list = []
            else:
                raw_list = []

            items = [self._build_pending_travel_item(p) for p in raw_list]
            count = len(items)

            card_data = {
                "total": count,
                "pendingTravelPlanCount": count,
                "pendingCheckInCount": 0,
                "pendingCheckOutCount": 0,
                "pendingLogCount": 0,
                "pendingTravelPlanItems": items,
                "pendingCheckInItems": [],
                "pendingCheckOutItems": [],
                "pendingLogItems": [],
            }
            card_json = json.dumps({
                "protocol": "visit.session.v1",
                "type": "a2ui.render",
                "card": {
                    "card_name": "visit-pending-card",
                    "schema_version": "1.0",
                    "data": card_data,
                    "action_states": {},
                },
            }, ensure_ascii=False, separators=(",", ":"))
            suggested_card = f"<<<VISIT_BLOCK_V1>>>{card_json}<<<END_VISIT_BLOCK_V1>>>"

            out: Dict[str, Any] = {
                "total": count,
                "items": items,
            }
            if count > 0:
                out["_suggestedCard"] = suggested_card
                out["_suggestedCardRule"] = (
                    "整条对用户回复必须且只能等于 _suggestedCard 字符串；块外零字符，禁止 Markdown。"
                )
                out["_nextStep"] = "STOP。整条回复原样输出 _suggestedCard（待补充差旅卡）。禁止 Markdown。"
            else:
                out["_nextStep"] = "返回 0 条待补充差旅计划，告知用户并询问是否新建出差拜访。"
            print(json.dumps(out, ensure_ascii=False), flush=True)
        else:
            # 其他 query_type，走默认 _print_result
            _print_result(data, self.session_created_at, self.chat_id)

        return 0

    @staticmethod
    def _build_pending_travel_item(plan: dict) -> dict:
        """把一条差旅待补充计划组装成 visit-pending-card 的 pendingTravelPlanItems 元素。

        接口返回结构：
          visitId        顶层字段
          planVisitTime  已格式化日期字符串 "yyyy-MM-dd"
          travelApply    JSON 字符串，含 travelApplyNo / travelViaCity / travelStartCity
        """
        visit_id = plan.get("visitId") or plan.get("visitPlanId") or plan.get("planId") or ""

        # travelApply 是 JSON 字符串，需二次解析
        travel_apply_raw = plan.get("travelApply") or ""
        travel_apply: dict = {}
        if travel_apply_raw:
            if isinstance(travel_apply_raw, dict):
                travel_apply = travel_apply_raw
            elif isinstance(travel_apply_raw, str):
                try:
                    p = json.loads(travel_apply_raw)
                    if isinstance(p, dict):
                        travel_apply = p
                except (json.JSONDecodeError, TypeError):
                    pass

        travel_order_no = travel_apply.get("travelApplyNo") or ""
        travel_location = travel_apply.get("travelViaCity") or travel_apply.get("travelStartCity") or ""

        # planVisitTime 已是 "yyyy-MM-dd" 字符串，转为「MM月DD日」
        raw_time_str = str(plan.get("planVisitTime") or "").strip()
        travel_time = ""
        if raw_time_str:
            try:
                from datetime import datetime as _dt
                travel_time = _dt.strptime(raw_time_str, "%Y-%m-%d").strftime("%m月%d日")
            except ValueError:
                travel_time = raw_time_str  # 兜底原样输出

        return {
            "visitId": visit_id,
            "pendingType": "pending-travel-plan",
            "travelOrderNo": travel_order_no,
            "travelLocation": travel_location,
            "travelTime": travel_time,
        }

    # ------------------------------------------------------------------
    # listTravelApplyV  — 差旅申请列表（独立 HTTP 端点，非 queryAiVisit）
    # 接口: POST /rest/apicenter/visit/web/visitCenter/listTravelApplyV
    # ------------------------------------------------------------------
    def list_travel_apply(self) -> int:
        """查询当前用户的差旅申请列表（审批通过的差旅申请单）。

        输出格式（stdout JSON，供 AI 读取）：
        {
          "total": N,
          "tips": "...",
          "list": [
            {"travelApplyNo": "...", "cities": ["..."], "startTime": "...", "endTime": "...", "status": "..."},
            ...
          ]
        }
        列表为空时 list=[]，AI 应下发 create-travel-info-card。
        """
        body: Dict[str, Any] = {}
        data = self._post("listTravelApplyV", body)
        _print_result(data, self.session_created_at, self.chat_id)

        resp_data = data.get("data") or {}
        lst = resp_data.get("list") or []
        total = resp_data.get("total", len(lst))
        tips = resp_data.get("tips", "")

        # 规范化字段名（camelCase），输出给 AI 的结构化结果
        normalized = []
        for item in lst:
            normalized.append({
                "travelApplyNo": item.get("travelApplyNo") or item.get("travel_apply_no", ""),
                "cities": item.get("cities") or [],
                "startTime": item.get("startTime") or item.get("start_time", ""),
                "endTime": item.get("endTime") or item.get("end_time", ""),
                "status": item.get("status", ""),
            })

        result = {"total": total, "tips": tips, "list": normalized}
        print(json.dumps(result, ensure_ascii=False))
        return 0

    # ------------------------------------------------------------------
    # searchAccompanyUserV  — 搜索陪访人（独立 HTTP 端点）
    # 接口: POST /rest/apicenter/visit/web/visitCenter/searchAccompanyUserV
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

    def add_accompany_user(self, plan_id: str, users_json: str) -> int:
        """给已创建的拜访计划添加陪访人。

        plan_id: 拜访计划 ID 字符串
        users_json: 选定陪访人的 JSON 数组，每条格式:
          [{"avatar":"...","userName":"张三","userCode":"zhangsan01"}, ...]
          也支持简化格式（Agent 只传 userName/userCode）：
          [{"userName":"张三","userCode":"zhangsan01"}, ...]
          脚本会自动搜索补全 avatar。

        调用 updateVisitPlan，仅更新 accompanyUserName 字段。
        成功输出: {"success": true}
        失败输出: {"success": false, "error": "..."}
        """
        try:
            users: List[Dict[str, Any]] = json.loads(users_json)
        except Exception as e:
            print(json.dumps({"success": False, "error": f"users_json 解析失败: {e}"}, ensure_ascii=False))
            return 1


        ALLOWED_FIELDS = ("avatar", "userName", "userCode")
        cleaned_users: List[Dict[str, Any]] = []
        for u in users:
            if not isinstance(u, dict):
                continue
            cleaned = {k: u[k] for k in ALLOWED_FIELDS if k in u}
            if "userName" not in cleaned or "userCode" not in cleaned:
                print(json.dumps({"success": False, "error": f"每条必须含 userName 和 userCode，缺少: {u}"}, ensure_ascii=False))
                return 1
            # avatar 缺失时自动搜索补全
            if not cleaned.get("avatar"):
                try:
                    search_body = {"keyword": cleaned["userCode"], "limit": 1}
                    search_data = self._post("searchAccompanyUserV", search_body)
                    search_list = (search_data.get("data") or {}).get("list") or []
                    if search_list:
                        avatar_val = search_list[0].get("avatar", "")
                        if avatar_val:
                            cleaned["avatar"] = avatar_val
                            print(f"[add-accompany-user] 自动补全 avatar: {cleaned['userName']} → {avatar_val[:50]}...", file=sys.stderr)
                except Exception as e:
                    print(f"[add-accompany-user] 搜索 avatar 补全失败({e})，留空", file=sys.stderr)
            if "avatar" not in cleaned:
                cleaned["avatar"] = ""
            cleaned_users.append(cleaned)

        # accompanyUserName 字段格式：每条是 JSON 字符串
        accompany_user_name = [json.dumps(u, ensure_ascii=False) for u in cleaned_users]

        body: Dict[str, Any] = {
            "visitPlanIdStr": plan_id,
            "accompanyUserName": accompany_user_name,
        }
        data = self._post_write("updateVisitPlan", body)
        _print_result(data, self.session_created_at, self.chat_id)

        base_resp = data.get("baseResponse") or data.get("base_response") or {}
        code = base_resp.get("code", data.get("code", -1))
        if code == 0 or code == 200:
            print(json.dumps({"success": True}, ensure_ascii=False))
            return 0
        else:
            msg = base_resp.get("msg") or base_resp.get("message") or data.get("msg", "未知错误")
            print(json.dumps({"success": False, "error": msg}, ensure_ascii=False))
            return 1

    # ------------------------------------------------------------------
    # createVisitPlanV  — 直接创建拜访计划（跳过 FSM state/batchResolve/executeAction）
    # 接口: POST /rest/apicenter/visit/web/visitCenter/createVisitPlanV
    # ------------------------------------------------------------------

    # visitPurposeLabel 中文/父子格式 → code 映射（从 VisitPurposeEnum 整理）
    _VISIT_PURPOSE_LABEL_TO_CODE: Dict[str, str] = {
        "客情维护": "customer_maintenance",
        "客情维护及其他": "customer_maintenance",
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
        # code 自映射
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

    # visitPurpose code → 中文 label（basicInfo.visitPurpose 返回 code，卡片需要中文）
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
    # queryVisitPlanDetails — 查询拜访计划详情
    # 接口: POST /rest/apicenter/visit/web/visitNew/queryVisitPlanDetails
    # ------------------------------------------------------------------

    def _fetch_plan_detail(self, plan_id: str) -> Optional[Dict[str, Any]]:
        """调 queryVisitPlanDetails，返回 data（VisitPlanInfoPb），失败返回 None"""
        body: Dict[str, Any] = {
            "id": plan_id,
        }
        # 注意：这个接口路径是 visitNew，不是 visitCenter
        url_bak = BASE_URL + "/" + _PATH_PREFIX  # 原 visitCenter 路径
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
    # 构建 action_result + visit-plan-created-card 双块（create_plan / supplement_plan 提交后）
    # ------------------------------------------------------------------
    def _build_action_result_blocks(
        self,
        action_id: str,
        success: bool,
        client_action_id: str,
        card_id: str,
        card_name: str,
        message: str,
        created_card_block: str,
    ) -> str:
        """构建 action_result 协议块，成功时追加 created-card 块。"""
        action_result = {
            "protocol": "visit.session.v1",
            "type": "a2ui.action_result",
            "client_action_id": client_action_id,
            "target": {
                "card_id": card_id,
                "card_name": card_name,
            },
            "action_id": action_id,
            "skill_name": "visit-plan-create",
            "skill_result": {
                "success": success,
                "message": message,
            },
        }
        block1 = _wrap_protocol(action_result)
        if success and created_card_block:
            return block1 + "\n" + created_card_block
        return block1

    # ------------------------------------------------------------------
    # 构建 visit-plan-created-card
    # ------------------------------------------------------------------
    def _build_created_card_from_detail(
        self, plan_id: str, detail: Optional[Dict[str, Any]]
    ) -> str:
        """
        从 queryVisitPlanDetails 返回的 VisitPlanInfoPb 构建 visit-plan-created-card。

        字段来源规则：
          customerName        basicInfo.businessLicense 或 basicInfo.storeName（优先 businessLicense）
          businessLicenseCode basicInfo.businessLicenseCode 或 visitCheckinInfo.storePoiId
          customerTypeLabel   由 basicInfo.visitObject 判断：
                                广告客户/门店 → 看 basicInfo.isNewCustomer（1=新,0=老）
                                广告代理商    → basicInfo.visitObject（直接用中文值）或 agentName
                                团购服务商    → basicInfo.visitObject（直接用中文值）或 providerName
          visitPurposeLabel   basicInfo.visitPurpose（code → 中文 label）
          planVisitTimeLabel  visitCheckinInfo.planVisitTime（ms → yyyy年M月d日）
          planVisitAddress    visitCheckinInfo.planVisitAddress.address
        """
        import datetime

        if not isinstance(detail, dict):
            detail = {}

        basic = detail.get("basicInfo") or detail.get("basic_info") or {}
        checkin = detail.get("visitCheckinInfo") or detail.get("visit_checkin_info") or {}
        plan_info = detail  # VisitPlanInfoPb 顶层字段

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
            # visitObject 已是人类可读中文，直接用；兜底用 isNewCustomer
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
                "card_name": "visit-plan-created-card",
                "schema_version": "1.0",
                "data": {
                    "visitPlanId": str(plan_id),
                    "customerName": customer_name,
                    "businessLicenseCode": biz_license_code,
                    "customerTypeLabel": customer_type_label,
                    "visitPurposeLabel": purpose_label,
                    "planVisitTimeLabel": plan_visit_time_label,
                    "planVisitAddress": plan_visit_address,
                    "targetUrl": (
                        f"locallifevisit://krn?bundleId=LocalLifeVisit"
                        f"&componentName=VisitDetailV2&themeStyle=3"
                        f"&visitPlanId={plan_id}"
                    ),
                    "viewEditUrl": (
                        f"locallifevisit://krn?bundleId=LocalLifeVisit"
                        f"&componentName=VisitCreate&themeStyle=3"
                        f"&visitPlanId={plan_id}&status=edit"
                    ),
                    "pcTargetUrl": f"/visit/tools/visit?visitId={plan_id}&action=detail",
                    "pcViewEditUrl": f"/visit/tools/visit?visitId={plan_id}&action=edit",
                    "actions": [
                        {"label": "当前计划签到", "value": "check-in-current-plan"},
                        {"label": "添加陪访人信息", "value": "add-companion"},
                    ],
                },
                "action_states": {
                    "edit_plan": {"enabled": True, "visible": True},
                    "view_plan": {"enabled": True, "visible": True},
                },
            },
        }
        return _wrap_protocol(card)

    def _build_supplement_plan_card(self, plan_id: str, detail: Optional[Dict[str, Any]]) -> str:
        """
        从 queryVisitPlanDetails 返回的 VisitPlanInfoPb 构建 visit-plan-card（supplement_plan 类型）。
        用于从 visit-pending-card 点击待补充差旅计划时直接下发，无需自然语言交互。

        前端收到卡片后，用户在卡片内填写缺失字段，点击「补充计划」按钮触发 supplement_plan action。
        """
        if not isinstance(detail, dict):
            detail = {}

        basic = detail.get("basicInfo") or {}
        checkin = detail.get("visitCheckinInfo") or {}
        travel_info = detail.get("travelInfo") or {}

        # 差旅待补充计划固定为出差
        visit_type = "travel"

        # travelApplyNo：在 travelInfo 里，不在 basicInfo/checkinInfo
        travel_apply_no = (
            travel_info.get("travelApplyNo") or travel_info.get("travel_apply_no")
            or basic.get("travelApplyNo") or basic.get("travel_apply_no")
            or checkin.get("travelApplyNo") or checkin.get("travel_apply_no")
            or None
        )

        # visitObjectType：code 映射
        _OBJ_TO_CODE = {
            "广告客户": "ad_customer", "ad_customer": "ad_customer",
            "门店": "store", "store": "store",
            "广告代理商": "agent", "agent": "agent",
            "团购服务商": "provider", "provider": "provider",
        }
        visit_object_raw = basic.get("visitObject") or basic.get("visit_object") or ""
        visit_object_type = _OBJ_TO_CODE.get(visit_object_raw, visit_object_raw) or None

        # visitObjectInfo
        is_new_raw = basic.get("isNewCustomer") or basic.get("is_new_customer") or "0"
        is_new = 1 if str(is_new_raw) == "1" else 0
        visit_obj_info = {
            "isNewCustomer": is_new,
            "licenseName": basic.get("businessLicense") or basic.get("business_license") or None,
            "licenseCode": basic.get("businessLicenseCode") or basic.get("business_license_code") or None,
            "agentId": str(detail.get("agentId") or detail.get("agent_id") or "") or None,
            "agentName": detail.get("agentName") or detail.get("agent_name") or None,
            "storePoiId": None,
            "storePoiName": None,
            "storeCustomerId": None,
            "storeCustomerName": None,
            "providerId": None,
            "providerName": None,
            "isTargetCustomer": 0,
            "targetCustomerId": None,
            "targetCustomerName": None,
        }

        # visitPurposeLabel（code → 中文）
        purpose_code = basic.get("visitPurpose") or basic.get("visit_purpose") or ""
        purpose_label = self._VISIT_PURPOSE_CODE_TO_LABEL.get(purpose_code, purpose_code) or None

        # planVisitTime（ms）
        plan_time_ms = checkin.get("planVisitTime") or checkin.get("plan_visit_time") or None

        # planVisitAddress / lat / lng
        addr_obj = checkin.get("planVisitAddress") or checkin.get("plan_visit_address") or {}
        if isinstance(addr_obj, dict):
            plan_visit_address = addr_obj.get("address") or None
            lat = addr_obj.get("latitude") or None
            lng = addr_obj.get("longitude") or None
        else:
            plan_visit_address = str(addr_obj) if addr_obj else None
            lat = None
            lng = None

        card = {
            "protocol": "visit.session.v1",
            "type": "a2ui.render",
            "card": {
                "card_name": "visit-plan-card",
                "schema_version": "1.0",
                "data": {
                    "operationType": "supplement_plan",
                    "planId": str(plan_id),
                    "visitType": visit_type,
                    "travelApplyNo": travel_apply_no,
                    "visitObjectType": visit_object_type,
                    "visitObjectInfo": visit_obj_info,
                    "visitPurposeLabel": purpose_label,
                    "planVisitTime": plan_time_ms,
                    "planVisitAddress": plan_visit_address,
                    "planVisitLatitude": lat,
                    "planVisitLongitude": lng,
                },
                "action_states": {"supplement_plan": {"enabled": True, "visible": True}},
            },
        }
        return _wrap_protocol(card)

    def supplement_plan_card(self, plan_id: str) -> int:
        """查询差旅待补充计划详情，构建 visit-plan-card(supplement_plan) 直接下发。

        触发时机：用户在 visit-pending-card 点击待补充差旅计划（action_id=supplement_travel_plan）。
        无需自然语言交互，直接输出 _suggestedCard。
        """
        detail = self._fetch_plan_detail(plan_id)
        suggested = self._build_supplement_plan_card(plan_id, detail)

        done_data: Dict[str, Any] = {"status": "ok", "planId": plan_id}
        _print_result(done_data, self.session_created_at, self.chat_id, suggested_card=suggested)

        if suggested:
            print(
                "➡️  下一步：STOP。整条回复原样输出 _suggestedCard（差旅直补卡）。禁止 Markdown。",
                file=sys.stderr,
            )
        return 0

    def direct_fill_from_card(
        self,
        data_json: str,
        client_action_id: str = "",
        card_id: str = "",
    ) -> int:
        """
        直接从卡片 data 调 updateVisitPlan，跳过 state/batchResolve/executeAction 流程。
        用于前端卡片 supplement_plan 按钮点击（触发方式 supplement-A）。

        需要 data.planId（前端 a2uiCardData.planId，即 supplement_plan 卡片里的 planId 字段）。
        注意：不传 visitType/travelApplyNo，避免后端出差校验（原值保留在 DB）。

        成功后自动构建 _suggestedCard（action_result + visit-plan-created-card），
        AI 原样输出即可，禁止手工拼凑协议块。
        """
        try:
            data = json.loads(data_json) if isinstance(data_json, str) else data_json
        except Exception as e:
            print(f"[ERROR] --data 不是合法 JSON: {e}", file=sys.stderr)
            return 1

        plan_id = str(data.get("planId") or data.get("visitPlanId") or "")
        if not plan_id:
            print("[ERROR] 缺少 planId，无法补充差旅计划。请确认前端 a2uiCardData.planId 已注入", file=sys.stderr)
            return 1

        # visitPurposeLabel → visitPurpose code
        purpose_label = data.get("visitPurposeLabel") or data.get("visitPurpose") or ""
        purpose_code = self._VISIT_PURPOSE_LABEL_TO_CODE.get(purpose_label, purpose_label)
        if not purpose_code:
            print(f"[WARN] visitPurposeLabel={purpose_label!r} 未命中枚举映射，原样传递", file=sys.stderr)

        # planVisitTime: 毫秒时间戳 → "yyyy-MM-dd HH:mm:ss"
        plan_visit_time = self._ms_to_datetime_str(data.get("planVisitTime"))

        # visitObjectInfo 字段清洗
        visit_object_info = self._build_visit_object_info_from_card(data)

        body: Dict[str, Any] = {
            "visitPlanIdStr": plan_id,
            "visitObjectType": data.get("visitObjectType") or "",
            "visitObjectInfo": visit_object_info,
            "visitPurpose": purpose_code,
            "planVisitTime": plan_visit_time,
            "planVisitAddress": data.get("planVisitAddress") or "",
        }

        # 可选：经纬度
        if data.get("planVisitLongitude") is not None:
            body["planVisitLongitude"] = float(data["planVisitLongitude"])
        if data.get("planVisitLatitude") is not None:
            body["planVisitLatitude"] = float(data["planVisitLatitude"])

        # 注意：不传 visitType/travelApplyNo（fill 场景保留原值，不触发后端出差校验）

        print(f"[direct-fill-card] 请求体：{json.dumps(body, ensure_ascii=False)}", file=sys.stderr)

        resp = self._post_write("updateVisitPlan", body)
        code = (resp.get("baseResponse") or {}).get("code")
        if code in (0, "0", None) and "error_http_status" not in resp:
            result_id = (resp.get("data") or {}).get("id") or plan_id
            print(f"[OK] 差旅计划补充成功，planId={result_id}", file=sys.stderr)

            # 查详情 → 构建 visit-plan-created-card
            detail = self._fetch_plan_detail(result_id) if result_id else None
            created_card_block = self._build_created_card_from_detail(result_id, detail)

            # 自动构建 action_result + visit-plan-created-card 双块
            suggested = self._build_action_result_blocks(
                action_id="supplement_plan",
                success=True,
                client_action_id=client_action_id,
                card_id=card_id,
                card_name="visit-plan-card",
                message="补充成功",
                created_card_block=created_card_block,
            )

            done_data: Dict[str, Any] = {"status": "done", "planId": result_id}
            _print_result(done_data, self.session_created_at, self.chat_id, suggested_card=suggested)
            return 0
        else:
            em = (resp.get("baseResponse") or {}).get("msg") or resp.get("errorMsg") or "未知错误"
            print(f"[ERROR] 差旅计划补充失败：{em}", file=sys.stderr)

            # 失败时也构建 action_result _suggestedCard
            suggested = self._build_action_result_blocks(
                action_id="supplement_plan",
                success=False,
                client_action_id=client_action_id,
                card_id=card_id,
                card_name="visit-plan-card",
                message=em,
                created_card_block="",
            )
            _print_result(resp, self.session_created_at, self.chat_id, suggested_card=suggested)
            return 1

    def _ms_to_datetime_str(self, ms: Any) -> str:
        """毫秒时间戳 → yyyy-MM-dd HH:mm:ss（createVisitPlanV 接口要求）"""
        import datetime
        if ms is None:
            return ""
        try:
            ts = int(ms)
            if ts < 1_000_000_000_000:
                ts = ts * 1000
            dt = datetime.datetime.fromtimestamp(
                ts / 1000, tz=datetime.timezone(datetime.timedelta(hours=8))
            )
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(ms)

    def _build_visit_object_info_from_card(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        从前端卡片 data.visitObjectInfo 构建接口所需结构。
        null 值：字符串字段→空字符串；int 字段→0。
        对应 proto VisitObjectInfo（HTTP 转发层负责 camelCase→snake_case）。
        """
        raw = data.get("visitObjectInfo") or {}
        if not isinstance(raw, dict):
            return {}
        def _str(v: Any) -> str:
            return str(v) if v is not None else ""
        def _int(v: Any) -> int:
            return int(v) if v is not None else 0
        return {
            "isNewCustomer": _int(raw.get("isNewCustomer")),
            "isTargetCustomer": _int(raw.get("isTargetCustomer")),
            "targetCustomerId": _str(raw.get("targetCustomerId")),
            "targetCustomerName": _str(raw.get("targetCustomerName")),
            "licenseName": _str(raw.get("licenseName")),
            "licenseCode": _str(raw.get("licenseCode")),
            "agentId": _str(raw.get("agentId")),
            "agentName": _str(raw.get("agentName")),
            "storePoiId": _str(raw.get("storePoiId")),
            "storePoiName": _str(raw.get("storePoiName")),
            "storeCustomerId": _str(raw.get("storeCustomerId")),
            "storeCustomerName": _str(raw.get("storeCustomerName")),
            "providerId": _str(raw.get("providerId")),
            "providerName": _str(raw.get("providerName")),
        }

    def direct_create(
        self,
        data_json: str,
        client_action_id: str = "",
        card_id: str = "",
    ) -> int:
        """
        直接调 createVisitPlanV，跳过 state/batchResolve/executeAction 流程。
        用于前端卡片 create_plan 按钮点击（触发方式 A）或 a2uiCardData 全填满（触发方式 C）。

        成功后自动构建 _suggestedCard（action_result + visit-plan-created-card），
        AI 原样输出即可，禁止手工拼凑协议块。
        """
        try:
            data = json.loads(data_json) if isinstance(data_json, str) else data_json
        except Exception as e:
            print(f"[ERROR] --data 不是合法 JSON: {e}", file=sys.stderr)
            return 1

        # visitPurposeLabel → visitPurpose code
        purpose_label = data.get("visitPurposeLabel") or data.get("visitPurpose") or ""
        purpose_code = self._VISIT_PURPOSE_LABEL_TO_CODE.get(purpose_label, purpose_label)
        if not purpose_code:
            print(f"[WARN] visitPurposeLabel={purpose_label!r} 未命中枚举映射，原样传递", file=sys.stderr)

        # planVisitTime: 毫秒时间戳 → "yyyy-MM-dd HH:mm:ss"
        plan_visit_time = self._ms_to_datetime_str(data.get("planVisitTime"))

        # visitObjectInfo 字段清洗（null → 空值）
        visit_object_info = self._build_visit_object_info_from_card(data)

        body: Dict[str, Any] = {
            "visitType": data.get("visitType") or "",
            "visitObjectType": data.get("visitObjectType") or "",
            "visitObjectInfo": visit_object_info,
            "visitPurpose": purpose_code,
            "planVisitTime": plan_visit_time,
            "planVisitAddress": data.get("planVisitAddress") or "",
        }

        # 可选：经纬度
        if data.get("planVisitLongitude") is not None:
            body["planVisitLongitude"] = float(data["planVisitLongitude"])
        if data.get("planVisitLatitude") is not None:
            body["planVisitLatitude"] = float(data["planVisitLatitude"])

        # 可选：差旅申请单号（出差时填）
        if data.get("travelApplyNo"):
            body["travelApplyNo"] = str(data["travelApplyNo"])

        resp = self._post_write("createVisitPlanV", body)

        if not _biz_ok(resp):
            print(f"[ERROR] createVisitPlanV 失败: {json.dumps(resp, ensure_ascii=False)}", file=sys.stderr)
            # 失败时也构建 action_result _suggestedCard
            error_msg = resp.get("msg") or resp.get("message") or "创建失败"
            suggested = self._build_action_result_blocks(
                action_id="create_plan",
                success=False,
                client_action_id=client_action_id,
                card_id=card_id,
                card_name="visit-plan-card",
                message=error_msg,
                created_card_block="",
            )
            _print_result(resp, self.session_created_at, self.chat_id, suggested_card=suggested)
            return 1

        # 取 planId
        resp_data = resp.get("data") or resp
        plan_id = str(
            resp_data.get("id") or resp_data.get("planId") or resp_data.get("visitPlanId") or ""
        )
        print(f"[OK] createVisitPlanV 成功，planId={plan_id}", file=sys.stderr)

        # 查详情 → 构建 visit-plan-created-card
        detail = self._fetch_plan_detail(plan_id) if plan_id else None
        created_card_block = self._build_created_card_from_detail(plan_id, detail)

        # 自动构建 action_result + visit-plan-created-card 双块
        suggested = self._build_action_result_blocks(
            action_id="create_plan",
            success=True,
            client_action_id=client_action_id,
            card_id=card_id,
            card_name="visit-plan-card",
            message="创建成功",
            created_card_block=created_card_block,
        )

        done_data: Dict[str, Any] = {"status": "done", "planId": plan_id}
        _print_result(done_data, self.session_created_at, self.chat_id, suggested_card=suggested)
        return 0

    # ---- card-resolve 辅助 ----

    _CARD_REQUIRED_FIELDS = [
        "visitType", "visitObjectType", "visitObjectInfo",
        "visitPurposeLabel", "planVisitTime", "planVisitAddress",
    ]
    _OBJECT_INFO_ID_FIELDS = [
        "licenseName", "agentId", "agentName",
        "storePoiId", "storePoiName", "providerId", "providerName",
    ]

    def _card_data_complete(self, card: Dict[str, Any]) -> bool:
        for f in self._CARD_REQUIRED_FIELDS:
            val = card.get(f)
            if val is None:
                return False
            if f == "visitObjectInfo":
                if not isinstance(val, dict):
                    return False
                if not any(val.get(k) for k in self._OBJECT_INFO_ID_FIELDS):
                    return False
        return True

    def _card_to_batch_items(self, card: Dict[str, Any]) -> list:
        items = []
        mapping = {
            "visitType": "visit_type",
            "visitObjectType": "visit_object_type",
            "visitObjectInfo": "visit_object_info",
            "visitPurposeLabel": "visit_purpose",
            "planVisitTime": "plan_visit_time",
            "planVisitAddress": "plan_visit_address",
            "travelApplyNo": "travel_apply_no",
        }
        for card_key, slot_name in mapping.items():
            val = card.get(card_key)
            if val is None:
                continue
            if card_key == "visitObjectInfo":
                if not isinstance(val, dict) or not any(val.get(k) for k in self._OBJECT_INFO_ID_FIELDS):
                    continue
                items.append({
                    "slotName": slot_name,
                    "raw": (val.get("licenseName") or val.get("agentName")
                            or val.get("storePoiName") or val.get("providerName") or ""),
                    "resolvedValueJson": json.dumps(val, ensure_ascii=False),
                })
            else:
                items.append({"slotName": slot_name, "raw": str(val)})
        return items

    def card_resolve(self, card_json: str, client_action_id: str = "", card_id: str = "") -> int:
        try:
            card = json.loads(card_json) if isinstance(card_json, str) else card_json
        except Exception as e:
            print(f"[ERROR] --card-data 不是合法 JSON: {e}", file=sys.stderr)
            return 1
        if self._card_data_complete(card):
            print("_cardResolvePath=direct", file=sys.stderr)
            return self.direct_create(json.dumps(card, ensure_ascii=False), client_action_id, card_id)
        items = self._card_to_batch_items(card)
        if not items:
            print("_cardResolvePath=batch(空白卡，走 state)", file=sys.stderr)
            return self.state()
        print(f"_cardResolvePath=batch({len(items)} 字段，走 batch-resolve)", file=sys.stderr)
        return self.batch_resolve(json.dumps(items, ensure_ascii=False), auto_patch=True)


# ------------------------------------------------------------------
# CLI / Runtime Context
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
        description="AI 拜访助手 — 创建拜访计划调试脚本",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # ---- state ----
    ps = sub.add_parser("state", help="读取当前会话状态（getAiVisitState）")
    _add_common(ps)
    ps.add_argument("--active-skill", default="create",
                    choices=["create", "update", "fill", "cancel", "checkin"])
    ps.add_argument("--plan-id", default=None, help="fill/update/cancel 场景传")
    ps.set_defaults(handler=lambda a: _make_client(a, require_session_created_at=(a.active_skill == "create")).state(a.active_skill, a.plan_id))

    # ---- batch-resolve ----
    pbr = sub.add_parser("batch-resolve", help="批量槽位解析（batchResolveAiVisitSlot）")
    _add_common(pbr)
    pbr.add_argument(
        "--items", required=True,
        help='槽位列表 JSON，如: [{"slot_name":"visit_type","raw":"本地"}]',
    )
    pbr.add_argument("--auto-patch", default="true", choices=["true", "false"],
                     help="是否自动回填 state（默认 true）")
    pbr.add_argument("--active-skill", default="create",
                     choices=["create", "update", "cancel", "checkin", "fill"])
    pbr.add_argument("--plan-id", default=None)
    pbr.set_defaults(
        handler=lambda a: _make_client(a, require_session_created_at=(a.active_skill == "create")).batch_resolve(
            a.items,
            auto_patch=(a.auto_patch == "true"),
            active_skill=a.active_skill,
            plan_id=a.plan_id,
        )
    )

    # ---- query 子命令（创建 skill 只用 pending-travel-plan）----
    ptp = sub.add_parser("pending-travel-plan",
                         help="待补充差旅拜访计划（--param=城市关键词，不传则返回全部）（queryAiVisit）")
    _add_common(ptp)
    ptp.add_argument("--param", default="", help="城市关键词（可选）")
    ptp.add_argument("--limit", type=int, default=10)
    ptp.set_defaults(handler=lambda a: _make_client(a).query("pending_travel", a.param, a.limit))

    # ---- travel-apply-list（独立接口 listTravelApplyV）----
    pta = sub.add_parser("travel-apply-list",
                         help="查询差旅申请列表（listTravelApplyV，独立接口非 queryAiVisit）")
    _add_common(pta)
    pta.set_defaults(handler=lambda a: _make_client(a).list_travel_apply())

    # ---- search-accompany-user ----
    psau = sub.add_parser("search-accompany-user",
                          help="搜索陪访人列表（searchAccompanyUserV）")
    _add_common(psau)
    psau.add_argument("--keyword", default="", help="姓名/工号关键词，不填返回所有")
    psau.add_argument("--limit", type=int, default=30, help="返回数量上限（默认30）")
    psau.set_defaults(handler=lambda a: _make_client(a).search_accompany_user(a.keyword, a.limit))

    # ---- add-accompany-user ----
    paau = sub.add_parser("add-accompany-user",
                          help="给拜访计划添加陪访人（updateVisitPlan.accompanyUserName）")
    _add_common(paau)
    paau.add_argument("--plan-id", required=True, dest="plan_id", help="拜访计划 ID 字符串")
    paau.add_argument("--users", required=True, dest="users_json",
                      help='选定陪访人 JSON 数组，如 \'[{"avatar":"...","userName":"张三","userCode":"zhangsan01"}]\'')
    paau.set_defaults(
        handler=lambda a: _make_client(a, require_session_created_at=False).add_accompany_user(a.plan_id, a.users_json)
    )

    # ---- direct-create ----
    pdc = sub.add_parser("direct-create", help="直接创建拜访计划（createVisitPlan，跳过 state/batch-resolve）")
    _add_common(pdc)
    pdc.add_argument("--data", required=True, help="前端卡片 data 的 JSON 字符串")
    pdc.add_argument("--client-action-id", default="",
                     help="前端 Prompt 中的 clientActionId（camelCase），原样回传到 action_result.client_action_id")
    pdc.add_argument("--card-id", default="",
                     help="前端 Prompt 中的 target.card_id，原样回传到 action_result.target.card_id")
    pdc.set_defaults(
        handler=lambda a: _make_client(a, require_session_created_at=False).direct_create(
            a.data, a.client_action_id, a.card_id)
    )

    # ---- card-resolve ----
    pcr = sub.add_parser(
        "card-resolve",
        help="从 a2uiCardData 自动分流：全填满→direct-create；部分填写→batch-resolve（不走模糊搜索）",
    )
    _add_common(pcr)
    pcr.add_argument("--card-data", required=True, dest="card_data",
                     help="bizContext.a2uiCardData 的 JSON 字符串（前端卡片当前状态）")
    pcr.add_argument("--client-action-id", default="",
                     help="前端 Prompt 中的 clientActionId（camelCase），原样回传到 action_result.client_action_id")
    pcr.add_argument("--card-id", default="",
                     help="前端 Prompt 中的 target.card_id，原样回传到 action_result.target.card_id")
    pcr.set_defaults(
        handler=lambda a: _make_client(a, require_session_created_at=False).card_resolve(
            a.card_data, a.client_action_id, a.card_id)
    )

    # ---- supplement-plan-card ----
    pspc = sub.add_parser(
        "supplement-plan-card",
        help="查询差旅待补充计划详情，直接构建并输出 visit-plan-card(supplement_plan)（无需自然语言交互）"
    )
    _add_common(pspc)
    pspc.add_argument("--plan-id", required=True, dest="plan_id",
                      help="待补充计划的 planId（从 visit-pending-card 点击时的 payload.visitId 取）")
    pspc.set_defaults(
        handler=lambda a: _make_client(a, require_session_created_at=False).supplement_plan_card(a.plan_id)
    )

    # ---- direct-fill-card ----
    pdfc = sub.add_parser(
        "direct-fill-card",
        help="从卡片 data 直接补充差旅计划（supplement_plan 按钮点击触发，绕开 FSM）"
    )
    _add_common(pdfc)
    pdfc.add_argument("--data", required=True,
                      help="前端 a2uiCardData 的 JSON 字符串（必须含 planId 字段）")
    pdfc.add_argument("--client-action-id", default="",
                      help="前端 Prompt 中的 clientActionId（camelCase），原样回传到 action_result.client_action_id")
    pdfc.add_argument("--card-id", default="",
                      help="前端 Prompt 中的 target.card_id，原样回传到 action_result.target.card_id")
    pdfc.set_defaults(
        handler=lambda a: _make_client(a, require_session_created_at=False).direct_fill_from_card(
            a.data, a.client_action_id, a.card_id)
    )

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
