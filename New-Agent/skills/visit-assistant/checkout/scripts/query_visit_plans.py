"""
查询当前登录人待签退的拜访计划列表（Web 端）

接口：queryVisitPlanList（PC 端 visit-service）
路径：POST http://127.0.0.1:8080/rest/apicenter/visit/web/visitNew/queryVisitPlanList

筛选条件（待签退）：
  - checkinStatus = ["1"]（已签到未签退）

用法：
  python query_visit_plans.py --creator-email lianjie03
  python query_visit_plans.py --creator-email lianjie03 --limit 20

输出 JSON + SUGGESTED_CARD（visit-plan-result-card 协议块）
"""

import argparse
import json
import sys
import os
from datetime import datetime, time, timedelta
import requests as _req

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_client import call_api, _get_pc_cookie


def _fetch_detail_fallback(visit_plan_id: str) -> dict:
    """
    当列表接口关键字段缺失时，直接用 requests 调 queryVisitPlanDetails 补全。
    完全独立（避免全局 api_client sys.exit 污染），失败返回空 dict。
    """
    if not visit_plan_id:
        return {}
    try:
        cookie = _get_pc_cookie() or ""
        url = "http://127.0.0.1:8080/rest/apicenter/visit/web/visitNew/queryVisitPlanDetails"
        headers = {
            "Cookie": cookie,
            "Content-Type": "application/json",
            "Referer": "http://127.0.0.1:8080/visit/tools/visit",
        }
        resp = _req.post(url, json={"id": visit_plan_id}, headers=headers, timeout=(10, 15))
        if resp.status_code != 200:
            return {}
        body = resp.json()
        code = (body.get("baseResponse") or {}).get("resultCode",
               (body.get("baseResponse") or {}).get("code", 0))
        if code != 0:
            return {}
        data = body.get("data") or {}
        checkin_info = data.get("visitCheckinInfo", {}) or {}
        basic_info   = data.get("basicInfo", {}) or {}
        title_info   = data.get("titleInfo", {}) or {}

        def _addr(obj):
            if not obj:
                return ""
            if isinstance(obj, dict):
                return obj.get("address", "")
            if isinstance(obj, str):
                try:
                    parsed = json.loads(obj)
                    return parsed.get("address", "") if isinstance(parsed, dict) else obj
                except Exception:
                    return obj
            return str(obj)

        return {
            "planVisitTime":      checkin_info.get("planVisitTime"),
            "actualVisitTime":    checkin_info.get("actualVisitTime"),
            "planVisitAddress":   _addr(checkin_info.get("planVisitAddress")),
            "actualVisitAddress": _addr(checkin_info.get("checkinAddress")),
            # 广告客户取 businessLicense；门店取 storeName/storePoiName；代理商取 agentName；服务商取 providerName
            "businessLicense":    basic_info.get("businessLicense", "") or basic_info.get("storeName", "") or data.get("agentName", "") or data.get("providerName", "") or title_info.get("storePoiName", "") or title_info.get("visitObjectName", ""),
            "storeName":          basic_info.get("storeName", ""),
            "agentName":          data.get("agentName", ""),
            "providerName":       data.get("providerName", ""),
            "visitObject":        basic_info.get("visitObject", ""),
        }
    except Exception as e:
        print(f"[WARN] 详情补全失败 visitPlanId={visit_plan_id}: {e}", file=sys.stderr)
        return {}

CUSTOMER_TYPE_LABEL = {
    "ad_customer": "广告客户", "广告客户": "广告客户", "广告客户(老)": "广告客户",
    "agent": "广告代理商", "广告代理商": "广告代理商",
    "store": "门店", "门店": "门店",
    "provider": "团购服务商", "团购服务商": "团购服务商",
}


def _fmt_time_label(val) -> str:
    """时间戳(毫秒/秒)或已格式化字符串 → 'YYYY-MM-DD HH:mm'。无效返回 ''。"""
    if val is None or val == "" or val == 0 or val == -1:
        return ""
    s = str(val)
    if s in ("0", "-1"):
        return ""
    if "-" in s and ":" in s:
        return s[:16]
    if s.isdigit():
        try:
            ts = int(s)
            if ts <= 0:
                return ""
            if ts > 1_000_000_000_000:
                ts //= 1000
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError):
            return s
    return s


def _build_result_card(plans: list) -> str:
    """构建 visit-plan-result-card 完整协议块（单行，无转义）。"""
    items = []
    for p in plans:
        items.append({
            "visitPlanId": str(p.get("visitPlanId", "")),
            "customerName": p.get("visitObjectName", ""),
            "customerTypeLabel": CUSTOMER_TYPE_LABEL.get(
                p.get("visitObject", ""), p.get("visitObject", "")),
            "planVisitTimeLabel": _fmt_time_label(
                p.get("planVisitTime") or p.get("actualVisitTime")),
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
    body = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
    return f"<<<VISIT_BLOCK_V1>>>{body}<<<END_VISIT_BLOCK_V1>>>"


def query_visit_plans(creator_email: str, limit: int = 20) -> dict:
    """
    查询待签退的拜访计划（已签到未签退，Web 端 PC Cookie）

    Args:
        creator_email: 创建人邮箱前缀（如 lianjie03）
        limit: 返回条数上限，默认20
    """
    # 签退有效期规则：签到当天 → 次日 06:00 截止
    # 若当前时间 >= 今天 06:00，说明昨天签到的已全部过期，只取今天 00:00 之后的签到
    # 若当前时间 < 今天 06:00，说明昨天签到截止是今天 06:00 还未到，取昨天 00:00 之后的签到
    now = datetime.now()
    today_6am = datetime.combine(now.date(), time(6, 0, 0))
    if now >= today_6am:
        # 今天 06:00 已过，只查今天 00:00 之后签到的（昨天签到的均已超期）
        sign_in_start = datetime.combine(now.date(), time(0, 0, 0))
    else:
        # 今天 06:00 未到，昨天签到的截止是今天 06:00，还有机会签退
        sign_in_start = datetime.combine(now.date() - timedelta(days=1), time(0, 0, 0))

    sign_in_start_ms = int(sign_in_start.timestamp() * 1000)
    sign_in_end_ms = int(now.timestamp() * 1000)

    payload = {
        "creatorEmail": creator_email,
        "filterCondition": [
            {"key": "clockStatus",   "value": ["1"]},       # 已签到未签退
            {"key": "visitStatus",   "value": ["1", "2"]},  # 待拜访 + 已签到待填写日志
        ],
        "timeCondition": {
            "signInTimeStart": sign_in_start_ms,
            "signInTimeEnd":   sign_in_end_ms,
        },
        "pageInfo": {"pageNum": 1, "pageSize": limit},
    }

    data = call_api("queryVisitPlanList", payload, step="query_plans")

    # DEBUG：打印 cookie 来源和接口原始返回（排查小助手查询为空问题）
    from api_client import _get_pc_cookie
    pc_cookie = _get_pc_cookie() or ""
    print(f"[DEBUG] cookie前10字符: {pc_cookie[:10]!r}  total={data.get('total')} rows={len(data.get('dataRow', []))}", file=__import__('sys').stderr)

    total = data.get("total", 0)
    data_rows = data.get("dataRow", [])

    plans = []
    for row in data_rows[:limit]:
        row_data = row.get("data", {})

        # checkinRecord 是 JSON 字符串，解析签到地址/时间
        signin_address = ""
        signin_time = None
        checkin_longitude = None
        checkin_latitude = None
        checkin_record_raw = row_data.get("checkinRecord", "")
        if checkin_record_raw:
            try:
                cr = json.loads(checkin_record_raw) if isinstance(checkin_record_raw, str) else checkin_record_raw
                signin_address = cr.get("actualVisitAddress") or ""
                # actualVisitTime 可能是 "2026-07-26 19:39:19" 格式
            except Exception:
                pass

        # actualVisitAddress 是 JSON 字符串 {"address":..., "longitude":..., "latitude":...}
        actual_address_raw = row_data.get("actualVisitAddress", "")
        actual_address = ""
        if actual_address_raw:
            try:
                addr_obj = json.loads(actual_address_raw) if actual_address_raw.startswith("{") else None
                if addr_obj:
                    actual_address = addr_obj.get("address", "")
                    checkin_longitude = addr_obj.get("longitude")
                    checkin_latitude = addr_obj.get("latitude")
                else:
                    actual_address = actual_address_raw
            except Exception:
                actual_address = actual_address_raw

        # planVisitTime 是毫秒时间戳字符串
        plan_visit_time_raw = row_data.get("planVisitTime", "")
        try:
            plan_visit_time = int(plan_visit_time_raw) if plan_visit_time_raw else None
        except (ValueError, TypeError):
            plan_visit_time = None

        # 只保留有 checkout 操作的计划
        operations = row.get("operations", [])
        op_keys = [op.get("key") for op in operations if op.get("isShow") == "1"]
        # web 端列表里签退通常走前端，不一定有 checkout key；直接用 checkinStatus=1 过滤即可

        plan = {
            "visitPlanId": row_data.get("visitId", ""),
            "visitObjectName": row_data.get("businessLicense", "") or row_data.get("storeName", "") or row_data.get("agentName", "") or row_data.get("providerName", ""),
            "visitObject": row_data.get("visitObject", ""),
            "visitType": row_data.get("visitType", ""),
            "actualVisitTime": row_data.get("actualVisitTime", ""),
            "planVisitTime": plan_visit_time,
            "planVisitAddress": row_data.get("planVisitAddress", ""),
            "signinAddress": signin_address or actual_address,
            "signinTime": signin_time,
            "checkinLongitude": checkin_longitude,
            "checkinLatitude": checkin_latitude,
            "visitStatus": row_data.get("visitStatus", ""),
            "checkinStatus": row_data.get("checkinStatus", ""),
        }

        # 关键展示字段缺失时调详情接口补全（PC 用户可能未开启该列展示）
        needs_fallback = (
            not plan["planVisitTime"]
            or not plan["planVisitAddress"]
            or not plan["visitObjectName"]
        )
        if needs_fallback and plan["visitPlanId"]:
            print(f"[INFO] 列表字段缺失，调详情补全 visitPlanId={plan['visitPlanId']}", file=sys.stderr)
            detail = _fetch_detail_fallback(plan["visitPlanId"])
            if detail:
                if not plan["planVisitTime"]:
                    plan["planVisitTime"] = detail.get("planVisitTime")
                if not plan["planVisitAddress"]:
                    plan["planVisitAddress"] = detail.get("planVisitAddress") or ""
                if not plan["signinAddress"]:
                    plan["signinAddress"] = detail.get("actualVisitAddress") or ""
                if not plan["visitObjectName"]:
                    plan["visitObjectName"] = detail.get("businessLicense", "") or detail.get("storeName", "") or detail.get("agentName", "") or detail.get("providerName", "")
                if not plan["visitObject"]:
                    plan["visitObject"] = detail.get("visitObject", "")

        plans.append(plan)

    return {"total": total, "plans": plans}


def main():
    parser = argparse.ArgumentParser(description="查询待签退的拜访计划列表（Web 端）")
    parser.add_argument("--creator-email", required=True, help="创建人邮箱前缀，如 lianjie03")
    parser.add_argument("--limit", type=int, default=20, help="返回条数上限，默认20")
    args = parser.parse_args()

    result = query_visit_plans(creator_email=args.creator_email, limit=args.limit)

    print(json.dumps(result, ensure_ascii=False, indent=2))

    if not result["plans"]:
        print("\n当前没有需要签退的拜访计划。", file=sys.stderr)
        sys.exit(0)

    suggested = _build_result_card(result["plans"])
    print("\n===== SUGGESTED_CARD (整条回复请原样复制下面这一行) =====")
    print(suggested)
    print(
        "⛔ 把上面 SUGGESTED_CARD 那一行原样作为整条回复输出，"
        "块外零字符，禁止加 \\\" 转义/换行/Markdown。",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
