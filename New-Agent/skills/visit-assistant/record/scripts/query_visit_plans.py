"""
查询当前登录人待录音的拜访计划列表（Web 端）

接口：queryVisitPlanList（PC 端 visit-service）
路径：POST http://127.0.0.1:8080/rest/apicenter/visit/web/visitNew/queryVisitPlanList

筛选条件（待录音）：
  - clockStatus = ["1"]（已签到未签退）
  - visitStatus = ["2"]（已签到待填写日志）

用法：
  # 查询待录音列表（默认）
  python query_visit_plans.py --creator-email lianjie03
  python query_visit_plans.py --creator-email lianjie03 --limit 20

  # 查询录音摘要（输出 visit-summary-card 协议块）
  python query_visit_plans.py --mode summary --visit-plan-id 7082591366325640

输出：
  list 模式：JSON + SUGGESTED_CARD（visit-plan-result-card 协议块）
  summary 模式：visit-summary-card 协议块（单行，直接输出到 stdout）
"""

import argparse
import json
import sys
import os
from datetime import datetime
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
            "businessLicense":    basic_info.get("businessLicense", "") or title_info.get("visitObjectName", ""),
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


def _fmt_time_label_cn(val) -> str:
    """时间戳(毫秒/秒)或已格式化字符串 → 'YYYY年M月D日 HH:mm'（中文格式）。无效返回 ''。"""
    if val is None or val == "" or val == 0 or val == -1:
        return ""
    s = str(val)
    if s in ("0", "-1"):
        return ""
    dt = None
    if s.isdigit():
        try:
            ts = int(s)
            if ts <= 0:
                return ""
            if ts > 1_000_000_000_000:
                ts //= 1000
            dt = datetime.fromtimestamp(ts)
        except (ValueError, OSError):
            return s
    elif "-" in s and ":" in s:
        try:
            dt = datetime.strptime(s[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            return s
    if dt:
        return f"{dt.year}年{dt.month}月{dt.day}日 {dt.strftime('%H:%M')}"
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


# llmStatus 含义
_LLM_STATUS_MSG = {
    0: "该拜访录音暂未整合，请稍后再试。",
    1: "该拜访录音正在整合中，请稍后再查看。",
    3: "该拜访录音整合失败，请稍后重试或联系支持。",
}


def _get_audio_summary_card(visit_plan_id: str) -> str:
    """
    查询录音摘要，组装 visit-summary-card 协议块（单行）。

    调用三个接口：
      0. getLlmStatus         → llmStatus（仅 2=整合成功时才继续）
      1. getAudioRecordDetail  → audioUrl(cdnUrl) / visitSummary(llmSummary)
      2. queryVisitPlanDetails → visitObjectType / visitObjectInfo（客户信息）

    非 2 状态时返回以 "TEXT:" 开头的错误文本，main() 负责输出。
    """
    cookie = _get_pc_cookie() or ""
    headers = {
        "Cookie": cookie,
        "Content-Type": "application/json",
        "Referer": "http://127.0.0.1:8080/visit/tools/visit",
        "Origin": "http://127.0.0.1:8080",
        "trace-context": '{"laneId":"PRT.visit"}',
    }

    # visitPlanId 后端定义为 Long，必须传数字类型
    try:
        visit_plan_id_int = int(visit_plan_id)
    except (ValueError, TypeError):
        return f"TEXT:visitPlanId 格式错误：{visit_plan_id}"

    # ── 0. 先查 llmStatus ──
    try:
        url0 = "http://127.0.0.1:8080/rest/apicenter/visit/app/visitAi/visitCenter/getLlmStatus"
        resp0 = _req.post(url0, json={"visitPlanId": visit_plan_id_int}, headers=headers, timeout=(10, 15))
        if resp0.status_code == 200:
            body0 = resp0.json()
            br0 = body0.get("baseResponse") or {}
            code0 = br0.get("resultCode", br0.get("code", 0))
            if code0 == 0:
                llm_status = (body0.get("data") or {}).get("llmStatus", -1)
                if llm_status != 2:
                    msg = _LLM_STATUS_MSG.get(llm_status, f"录音状态异常（status={llm_status}），请稍后重试。")
                    return f"TEXT:{msg}"
    except Exception as e:
        print(f"[WARN] getLlmStatus 失败: {e}", file=sys.stderr)
        # 接口异常时降级继续查摘要，不阻断主流程

    # ── 1. 录音摘要 ──
    audio_url = ""
    visit_summary = ""
    try:
        url = "http://127.0.0.1:8080/rest/apicenter/visit/app/visitAi/visitCenter/getAudioRecordDetail"
        resp = _req.post(url, json={"visitPlanId": visit_plan_id_int}, headers=headers, timeout=(10, 30))
        if resp.status_code == 200:
            body = resp.json()
            br = body.get("baseResponse") or {}
            code = br.get("resultCode", br.get("code", 0))
            if code == 0:
                d = body.get("data") or {}
                audio_url = d.get("cdnUrl", "")
                visit_summary = d.get("llmSummary", "")
    except Exception as e:
        print(f"[WARN] getAudioRecordDetail 失败: {e}", file=sys.stderr)

    # ── 2. 拜访详情（客户信息 + 计划信息）──
    visit_object_type = ""
    visit_object_info = {}
    plan_visit_time = ""
    plan_visit_address = ""
    try:
        url2 = "http://127.0.0.1:8080/rest/apicenter/visit/web/visitNew/queryVisitPlanDetails"
        resp2 = _req.post(url2, json={"id": visit_plan_id}, headers=headers, timeout=(10, 15))
        if resp2.status_code == 200:
            body2 = resp2.json()
            br2 = body2.get("baseResponse") or {}
            code2 = br2.get("resultCode", br2.get("code", 0))
            if code2 == 0:
                data2 = body2.get("data") or {}
                basic_info = data2.get("basicInfo", {}) or {}
                title_info = data2.get("titleInfo", {}) or {}
                checkin_info2 = data2.get("visitCheckinInfo", {}) or {}
                visit_object_type = basic_info.get("visitObject", "")
                license_name = (
                    basic_info.get("businessLicense", "")
                    or basic_info.get("storeName", "")
                    or data2.get("agentName", "")
                    or data2.get("providerName", "")
                    or title_info.get("storePoiName", "")
                    or title_info.get("visitObjectName", "")
                )
                visit_object_info = {
                    "licenseName": license_name,
                    "agentName": data2.get("agentName") or None,
                    "storePoiName": title_info.get("storePoiName") or None,
                    "providerName": data2.get("providerName") or None,
                }
                # planVisitTime 格式化为 "YYYY年M月D日 HH:mm"
                raw_time = checkin_info2.get("planVisitTime") or checkin_info2.get("actualVisitTime")
                plan_visit_time = _fmt_time_label_cn(raw_time)
                # planVisitAddress 取 address 字段
                addr_obj = checkin_info2.get("planVisitAddress")
                if isinstance(addr_obj, dict):
                    plan_visit_address = addr_obj.get("address", "")
                elif isinstance(addr_obj, str):
                    try:
                        import json as _json2
                        parsed = _json2.loads(addr_obj)
                        plan_visit_address = parsed.get("address", "") if isinstance(parsed, dict) else addr_obj
                    except Exception:
                        plan_visit_address = addr_obj
    except Exception as e:
        print(f"[WARN] queryVisitPlanDetails 失败: {e}", file=sys.stderr)

    card = {
        "protocol": "visit.session.v1",
        "type": "a2ui.render",
        "block_id": "b_visit_summary_001",
        "card": {
            "card_id": "c_visit_summary_001",
            "card_name": "visit-summary-card",
            "schema_version": "1.0",
            "data": {
                "visitId": str(visit_plan_id),
                "audioUrl": audio_url,
                "visitObjectType": visit_object_type,
                "visitObjectInfo": visit_object_info,
                "planVisitTime": plan_visit_time,
                "planVisitAddress": plan_visit_address,
                "visitSummary": visit_summary,
                "list": [
                    {"label": "重新录制", "value": "record-again"},
                    {"label": "去签退", "value": "checkout"},
                ],
            },
            "action_states": {
                "action_press": {"enabled": True, "visible": True},
                "switch_plan": {"enabled": True, "visible": True},
            },
        },
    }
    body = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
    return f"<<<VISIT_BLOCK_V1>>>{body}<<<END_VISIT_BLOCK_V1>>>"


def query_visit_plans(creator_email: str, limit: int = 20) -> dict:
    """
    查询待录音的拜访计划（已签到未签退，Web 端 PC Cookie）

    Args:
        creator_email: 创建人邮箱前缀（如 lianjie03）
        limit: 返回条数上限，默认20
    """
    payload = {
        "creatorEmail": creator_email,
        "filterCondition": [
            {"key": "clockStatus",   "value": ["1"]},  # 已签到未签退
            {"key": "visitStatus",   "value": ["2"]},  # 已签到待填写日志
        ],
        "pageInfo": {"pageNum": 1, "pageSize": limit},
    }

    data = call_api("queryVisitPlanList", payload, step="query_plans")

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
    parser = argparse.ArgumentParser(description="查询待录音的拜访计划列表（Web 端）")
    parser.add_argument("--creator-email", help="创建人邮箱前缀，如 lianjie03（list 模式必填）")
    parser.add_argument("--limit", type=int, default=20, help="返回条数上限，默认20")
    parser.add_argument("--mode", choices=["list", "summary"], default="list",
                        help="list=查询待录音列表（默认）; summary=查询录音摘要卡片")
    parser.add_argument("--visit-plan-id", help="拜访计划 ID（summary 模式必填）")
    args = parser.parse_args()

    # ── summary 模式：查询录音摘要，输出 visit-summary-card ──
    if args.mode == "summary":
        if not args.visit_plan_id:
            print("[ERROR] --mode summary 时必须提供 --visit-plan-id", file=sys.stderr)
            sys.exit(1)
        result = _get_audio_summary_card(args.visit_plan_id)
        if result.startswith("TEXT:"):
            # llmStatus 非 2，输出提示文本
            print(result[5:])
        else:
            # llmStatus=2，输出卡片协议块
            print(result)
        return

    # ── list 模式（默认）：查询待录音列表 ──
    if not args.creator_email:
        print("[ERROR] --mode list 时必须提供 --creator-email", file=sys.stderr)
        sys.exit(1)

    result = query_visit_plans(creator_email=args.creator_email, limit=args.limit)

    print(json.dumps(result, ensure_ascii=False, indent=2))

    if not result["plans"]:
        print("\n当前没有需要录音的拜访计划。", file=sys.stderr)
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
