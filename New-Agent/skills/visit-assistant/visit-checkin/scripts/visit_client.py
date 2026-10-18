"""
AI 拜访助手 — 签到脚本
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
_PATH_PREFIX_WEB = "rest/apicenter/visit/web/visitCenter"   # getAiVisitState / queryAiVisit
_HTTP_TIMEOUT_SEC = 15

# visitObjectType 枚举 → 中文
_VISIT_OBJECT_LABEL = {
    "ad_customer": "广告客户",
    "agent": "广告代理商",
    "store": "门店",
    "provider": "团购服务商",
}


# ------------------------------------------------------------------
# 卡片生成辅助
# ------------------------------------------------------------------

def _build_visit_check_card(plan: Dict[str, Any], lat: float, lng: float) -> str:
    """根据 plan 对象 + 当前经纬度生成 visit-check-card 协议块字符串。"""
    plan_id = str(plan.get("visitPlanId", ""))
    obj_type = plan.get("visitObjectType") or plan.get("visitObject") or ""
    obj_label = _VISIT_OBJECT_LABEL.get(obj_type, obj_type)
    customer_name = (
        (plan.get("visitObjectInfo") or {}).get("licenseName")
        or plan.get("customerName")
        or plan.get("businessLicense")
        or ""
    )
    data = {
        "visitPlanId": plan_id,
        "watermarkPreviewLink": (
            f"locallifevisit://krn?bundleId=LocalLifeVisit&componentName=WatermarkPreview"
            f"&themeStyle=3&visitPlanId={plan_id}&fromPage=VisitCheck"
        ),
        "locationModifyLink": (
            f"locallifevisit://krn?bundleId=LocalLifeVisit&componentName=VisitCheck"
            f"&themeStyle=3&visitPlanId={plan_id}"
        ),
        "planVisitTime": plan.get("planVisitTime"),
        "planVisitAddress": plan.get("planVisitAddress", ""),
        "visitObjectInfo": {"licenseName": customer_name},
        "clientName": customer_name,
        "visitTarget": {"label": obj_label, "value": obj_type},
        "checkinStatus": "WAIT_CHECKIN",
        "currentAddress": plan.get("currentAddress", ""),
        "latitude": lat,
        "longitude": lng,
        "signInAddress": None,
        "signInTime": None,
        "signOutAddress": None,
        "signOutTime": None,
        "currentDistance": plan.get("distance", 0),
        "currentDistanceToSignInPoint": None,
        "rangeLimit": plan.get("rangeLimit"),
        "isTargetOutOfRange": plan.get("isTargetOutOfRange", False),
        "isCheckoutOvertime": False,
        "overtimeText": "",
        "isCheckoutOutOfRange": False,
        "outOfRangeDistance": 0,
        "canCheckOut": False,
        "cardDisabled": False,
        "disabledReason": "",
        "buttonDisabled": False,
        "rightBtnText": "拍照签到",
        "isExpired": False,
    }
    action_states = {
        "photo_check_in": {"enabled": True, "visible": True},
        "switch_plan": {"enabled": True, "visible": True},
        "refresh_location": {"enabled": True, "visible": True},
        "modify_location": {"enabled": True, "visible": True},
    }
    card = {
        "protocol": "visit.session.v1",
        "type": "a2ui.render",
        "card": {
            "card_name": "visit-check-card",
            "schema_version": "1.0",
            "data": data,
            "action_states": action_states,
        },
    }
    return (
        "<<<VISIT_BLOCK_V1>>>"
        + json.dumps(card, ensure_ascii=False, separators=(",", ":"))
        + "<<<END_VISIT_BLOCK_V1>>>"
    )


def _build_plan_result_card(plans: List[Dict[str, Any]]) -> str:
    """根据 plan 列表生成 visit-plan-result-card 协议块字符串。"""
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
        items.append({
            "visitPlanId": str(p.get("visitPlanId", "")),
            "customerName": (
                (p.get("visitObjectInfo") or {}).get("licenseName")
                or p.get("customerName")
                or p.get("businessLicense")
                or ""
            ),
            "customerTypeLabel": type_label,
            "planVisitTimeLabel": time_label,
            "planVisitAddress": p.get("planVisitAddress", ""),
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


def _build_checkin_summary_card(visit_id: str, is_re_record: bool = False) -> str:
    """生成 check-in-summary-card 协议块字符串。"""
    card = {
        "protocol": "visit.session.v1",
        "type": "a2ui.render",
        "card": {
            "card_name": "check-in-summary-card",
            "schema_version": "1.0",
            "data": {
                "visitId": str(visit_id),
                "isReRecord": is_re_record,
                "recordingStatus": "idle",
                "list": [
                    {"label": "当前计划签退", "value": "check-out"},
                    {"label": "修改拜访计划信息", "value": "edit-visit-plan"},
                ],
            },
            "action_states": {
                "recording_start": {"enabled": True, "visible": True},
                "recording_end": {"enabled": True, "visible": True},
                "visit_log_ready": {"enabled": True, "visible": True},
                "action_press": {"enabled": True, "visible": True},
            },
        },
    }
    return (
        "<<<VISIT_BLOCK_V1>>>"
        + json.dumps(card, ensure_ascii=False, separators=(",", ":"))
        + "<<<END_VISIT_BLOCK_V1>>>"
    )


# ------------------------------------------------------------------
# stdout 输出
# ------------------------------------------------------------------

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
    ):
        self.chat_id = chat_id
        self.app = app
        self._cookies = cookies
        if session_created_at is not None:
            self.session_created_at = int(session_created_at)
        else:
            self.session_created_at = int(time.time() * 1000)

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
        return f"{BASE_URL}/{_PATH_PREFIX_WEB}/{method}"

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
                    return {"error_http_status": status, "body": text}
                return json.loads(text) if text.strip() else {}
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="replace") if e.fp else ""
            print(f"[WARN] HTTP {e.code} for {method}", file=sys.stderr)
            return {"error_http_status": e.code, "body": text}
        except urllib.error.URLError as e:
            print(f"[ERROR] 网络错误 for {method}: {e.reason}", file=sys.stderr)
            return {"error": f"network_error: {e.reason}"}

    # ------------------------------------------------------------------
    # getAiVisitState（签到场景仅用于确认 checkinStatus）
    # ------------------------------------------------------------------
    def state(self, plan_id: str) -> int:
        if not plan_id:
            print("[ERROR] --plan-id 必传", file=sys.stderr)
            return 1
        body: Dict[str, Any] = {
            "chatId": self.chat_id,
            "activeSkill": "checkin",
            "app": self.app,
            "planId": plan_id,
        }
        data = self._post("getAiVisitState", body)
        _print_result(data, self.session_created_at, self.chat_id)
        if not _biz_ok(data):
            print("[ERROR] getAiVisitState 失败", file=sys.stderr)
            return 1
        return 0

    # ------------------------------------------------------------------
    # queryAiVisit — 位置匹配 + 计划列表
    # 自动解析 dataJson，若有结果则生成 _suggestedCard
    # ------------------------------------------------------------------
    def query(
        self,
        query_type: str,
        param: str = "",
        limit: int = 10,
        lat: Optional[float] = None,
        lng: Optional[float] = None,
        plan_id: Optional[str] = None,
    ) -> int:
        body: Dict[str, Any] = {"queryType": query_type, "limit": limit}
        if param:
            body["param"] = param
        data = self._post("queryAiVisit", body)

        # 解析 dataJson，提取 plan 列表
        plans: List[Dict[str, Any]] = []
        data_json_str = data.get("dataJson") or data.get("data_json", "")
        if data_json_str:
            try:
                parsed = json.loads(data_json_str)
                if isinstance(parsed, list):
                    plans = parsed
                elif isinstance(parsed, dict):
                    plans = parsed.get("list") or parsed.get("planToMap") or []
            except Exception:
                pass

        # 若指定了 plan_id，只取该条计划
        if plan_id and plans:
            matched = [p for p in plans if str(p.get("visitPlanId", "")) == str(plan_id)]
            if matched:
                plans = matched

        # 生成 _suggestedCard
        suggested_card: Optional[str] = None
        if plans and lat is not None and lng is not None:
            if len(plans) == 1:
                # 单条 → 直接生成签到卡片
                suggested_card = _build_visit_check_card(plans[0], lat, lng)
            else:
                # 多条 → 生成列表卡片
                suggested_card = _build_plan_result_card(plans)
        elif plans:
            # 无经纬度（通常不会到这里，经纬度门禁保证）
            suggested_card = _build_plan_result_card(plans)

        # stderr 摘要
        print(f"--- query [{query_type}] plans={len(plans)} ---", file=sys.stderr)
        for i, p in enumerate(plans, 1):
            print(
                f"  {i}. planId={p.get('visitPlanId','?')} "
                f"dist={p.get('distance','?')}m "
                f"{p.get('planVisitAddress','?')}",
                file=sys.stderr,
            )

        _print_result(data, self.session_created_at, self.chat_id, suggested_card)
        return 0

    # ------------------------------------------------------------------
    # check-card：直接通过 plan_id + plan-list 生成签到卡片
    # 用于快捷入口（来自创建/取消成功卡的「当前计划签到」）
    # ------------------------------------------------------------------
    def check_card(self, plan_id: str, lat: float, lng: float) -> int:
        if not plan_id:
            print("[ERROR] --plan-id 必传", file=sys.stderr)
            return 1
        param = json.dumps(
            {"query_scene": "checkin", "plan_id": plan_id, "lng": lng, "lat": lat},
            ensure_ascii=False,
        )
        return self.query("plan_list", param=param, lat=lat, lng=lng, plan_id=plan_id)

    # ------------------------------------------------------------------
    # plan-detail（用于已签到状态检查，checkinStatus=1 时自动输出摘要卡）
    # ------------------------------------------------------------------
    def plan_detail(self, plan_id: str) -> int:
        data = self._post("queryAiVisit", {"queryType": "plan_detail", "param": plan_id})

        # 检查 checkinStatus：已签到未签退 → 自动生成 check-in-summary-card
        suggested_card: Optional[str] = None
        checkin_status = None
        data_json_str = data.get("dataJson") or data.get("data_json", "")
        if data_json_str:
            try:
                parsed = json.loads(data_json_str)
                if isinstance(parsed, dict):
                    checkin_status = parsed.get("checkinStatus")
                elif isinstance(parsed, list) and parsed:
                    checkin_status = parsed[0].get("checkinStatus")
            except Exception:
                pass
        if checkin_status == 1:
            suggested_card = _build_checkin_summary_card(plan_id, is_re_record=False)
            print(f"[INFO] checkinStatus=1，已生成 check-in-summary-card", file=sys.stderr)

        _print_result(data, self.session_created_at, self.chat_id, suggested_card)
        return 0


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--chat-id", default=None)
    p.add_argument("--session-created-at", type=int, default=None)
    p.add_argument("--app", default="visit")
    p.add_argument("--cookies", default=None)


def _make_client(a) -> VisitClient:
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
    return VisitClient(chat_id, session_created_at=a.session_created_at, app=a.app, cookies=cookies)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="AI 拜访助手 — 签到脚本")
    sub = p.add_subparsers(dest="cmd", required=True)

    # ---- state（仅用于 checkinStatus 检查）----
    ps = sub.add_parser("state", help="读取签到会话状态（getAiVisitState）")
    _add_common(ps)
    ps.add_argument("--plan-id", required=True)
    ps.set_defaults(handler=lambda a: _make_client(a).state(a.plan_id))

    # ---- nearby-match：位置匹配，自动生成 _suggestedCard ----
    pnm = sub.add_parser("nearby-match", help="1km 内今日可签到计划（--param=lng,lat --lat --lng）")
    _add_common(pnm)
    pnm.add_argument("--param", required=True, help="lng,lat 字符串")
    pnm.add_argument("--lat", type=float, default=None)
    pnm.add_argument("--lng", type=float, default=None)
    def _nearby(a):
        lat, lng = a.lat, a.lng
        if lat is None or lng is None:
            try:
                parts = a.param.split(",")
                lng, lat = float(parts[0].strip()), float(parts[1].strip())
            except Exception:
                pass
        return _make_client(a).query("nearby_match", param=a.param, lat=lat, lng=lng)
    pnm.set_defaults(handler=_nearby)

    # ---- plan-list：计划列表，自动生成 _suggestedCard ----
    ppl = sub.add_parser("plan-list", help="计划列表搜索（--param=JSON --lat --lng）")
    _add_common(ppl)
    ppl.add_argument("--param", required=True, help="JSON 字符串，含 query_scene/keyword/lng/lat")
    ppl.add_argument("--lat", type=float, default=None)
    ppl.add_argument("--lng", type=float, default=None)
    def _plan_list(a):
        lat, lng = a.lat, a.lng
        if lat is None or lng is None:
            try:
                pj = json.loads(a.param)
                lat = lat or pj.get("lat")
                lng = lng or pj.get("lng")
            except Exception:
                pass
        return _make_client(a).query("plan_list", param=a.param, lat=lat, lng=lng)
    ppl.set_defaults(handler=_plan_list)

    # ---- check-card：快捷入口，指定 plan_id 直接生成签到卡片 ----
    pcc = sub.add_parser("check-card", help="快捷签到卡片（已知 plan_id，--plan-id --lat --lng）")
    _add_common(pcc)
    pcc.add_argument("--plan-id", required=True)
    pcc.add_argument("--lat", type=float, required=True)
    pcc.add_argument("--lng", type=float, required=True)
    pcc.set_defaults(handler=lambda a: _make_client(a).check_card(a.plan_id, a.lat, a.lng))

    # ---- plan-detail（仅 Step 2.0 已签到状态检查）----
    ppd = sub.add_parser("plan-detail", help="计划详情（checkinStatus 检查用）")
    _add_common(ppd)
    ppd.add_argument("--param", required=True, help="plan_id")
    ppd.set_defaults(handler=lambda a: _make_client(a).plan_detail(a.param))

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
