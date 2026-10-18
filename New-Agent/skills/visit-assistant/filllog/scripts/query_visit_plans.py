"""
查询拜访计划列表

接口：queryVisitPlanList（PC 端 visit-service）
路径：POST http://127.0.0.1:8080/rest/apicenter/visit/web/visitNew/queryVisitPlanList

筛选条件：
  - visitStatus = ["2"]（待填写日志）— 默认，填写日志场景
  - visitStatus = ["4"]（已完成）— 修改日志场景，用 --status 4 指定
  - creatorEmail = 当前登录人邮箱前缀

状态枚举：
  1=待填写计划, 2=待填写日志, 3=待拜访, 4=已完成, 5=已取消

用法：
  # 填写日志：查待填写日志的计划（默认）
  python query_visit_plans.py --creator-email wb_sufan
  python query_visit_plans.py --creator-email wb_sufan --limit 5

  # 修改日志：查已完成的计划
  python query_visit_plans.py --creator-email wb_sufan --status 4

输出 JSON：
{
  "total": 3,
  "plans": [
    {
      "visitPlanId": "123456",
      "visitObjectName": "XX科技有限公司",
      "visitObject": "广告客户",
      "visitType": "local",
      "actualVisitTime": "2026-06-30 10:30:00",
      "actualVisitAddress": "北京市朝阳区XX路XX号",
      "visitStatus": "2",
      "checkinStatus": "1"
    }
  ]
}
"""

import argparse
import json
import sys
import os
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
            "planVisitTime":    checkin_info.get("planVisitTime"),
            "actualVisitTime":  checkin_info.get("actualVisitTime"),
            "planVisitAddress": _addr(checkin_info.get("planVisitAddress")),
            "actualVisitAddress": _addr(checkin_info.get("checkinAddress")),
            "businessLicense":  basic_info.get("businessLicense", "") or basic_info.get("storeName", "") or data.get("agentName", "") or data.get("providerName", "") or title_info.get("visitObjectName", "") or title_info.get("storePoiName", ""),
            "visitObject":      basic_info.get("visitObject", ""),
        }
    except Exception as e:
        print(f"[WARN] 详情补全失败 visitPlanId={visit_plan_id}: {e}", file=sys.stderr)
        return {}

# visitObject → customerTypeLabel 中文映射
_CUSTOMER_TYPE_LABEL = {
    "ad_customer": "广告客户", "广告客户": "广告客户", "广告客户(老)": "广告客户",
    "agent": "广告代理商", "广告代理商": "广告代理商",
    "store": "门店", "门店": "门店",
    "provider": "团购服务商", "团购服务商": "团购服务商",
}


def _fmt_time_label(val) -> str:
    """时间戳(毫秒/秒)或已格式化字符串 → 'YYYY-MM-DD HH:mm'。"""
    if val is None or val == "" or val == 0 or val == -1:
        return ""
    s = str(val)
    if s in ("0", "-1"):
        return ""
    # 已是可读格式（含 '-'），截到分钟
    if "-" in s and ":" in s:
        return s[:16]
    if s.isdigit():
        try:
            ts = int(s)
            if ts <= 0:
                return ""
            if ts > 1000000000000:
                ts //= 1000
            from datetime import datetime
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
            "customerTypeLabel": _CUSTOMER_TYPE_LABEL.get(
                p.get("visitObject", ""), p.get("visitObject", "")),
            "planVisitTimeLabel": _fmt_time_label(p.get("planVisitTime")),
            "planVisitAddress": p.get("planVisitAddress", ""),
        })
    card = {
        "protocol": "visit.session.v1",
        "type": "a2ui.render",
        "block_id": "b001",
        "card": {
            "card_id": "c001",
            "card_name": "visit-plan-result-card",
            "schema_version": "1.0",
            "data": {"total": len(items), "hasMore": False, "items": items},
            "action_states": {"select_plan": {"enabled": True, "visible": True}},
        },
    }
    body = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
    return f"<<<VISIT_BLOCK_V1>>>{body}<<<END_VISIT_BLOCK_V1>>>"


def query_visit_plans(creator_email: str, limit: int = 5, status: int = 2) -> dict:
    """
    查询拜访计划列表

    Args:
        creator_email: 创建人邮箱前缀（如 wb_sufan）
        limit: 返回条数上限，默认5
        status: 拜访状态过滤值，默认2（待填写日志）。
                1=待填写计划, 2=待填写日志, 3=待拜访, 4=已完成, 5=已取消
                修改日志场景应传 4（已完成）
    """
    payload = {
        "creatorEmail": creator_email,
        "filterCondition": [
            {"key": "visitStatus", "value": [str(status)]}
        ],
        "pageInfo": {
            "pageNum": 1,
            "pageSize": limit
        }
    }

    data = call_api("queryVisitPlanList", payload, step="query_plans")

    # 解析响应：VisitPlanListData → {headers, dataRow, total, pageNum}
    total = data.get("total", 0)
    data_rows = data.get("dataRow", [])

    # 提取每行的关键字段
    # 字段 key 来自 VisitFieldEnum（后端 buildDataRow 方法确认）
    plans = []
    for row in data_rows[:limit]:
        row_data = row.get("data", {})

        # actualVisitAddress 是 JSON 字符串，包含 address/longitude/latitude（签到时记录的位置）
        actual_address_raw = row_data.get("actualVisitAddress", "")
        actual_address = actual_address_raw
        checkin_longitude = None
        checkin_latitude = None
        try:
            addr_obj = json.loads(actual_address_raw) if actual_address_raw and actual_address_raw.startswith("{") else None
            if addr_obj:
                actual_address = addr_obj.get("address", actual_address_raw)
                # 提取签到时的经纬度，供签退时复用
                checkin_longitude = addr_obj.get("longitude") or addr_obj.get("lng")
                checkin_latitude = addr_obj.get("latitude") or addr_obj.get("lat")
        except (json.JSONDecodeError, TypeError):
            pass

        # actualVisitTime 是时间戳字符串（毫秒），转为可读格式
        actual_time_raw = row_data.get("actualVisitTime", "")
        actual_time = actual_time_raw
        if actual_time_raw and actual_time_raw.isdigit():
            try:
                ts = int(actual_time_raw)
                if ts > 1000000000000:
                    ts = ts // 1000  # 毫秒转秒
                from datetime import datetime
                actual_time = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, OSError):
                pass

        plan = {
            "visitPlanId": row_data.get("visitId", ""),
            # 门店类型 businessLicense 为空，用 storeName 兜底；代理商/服务商用 agentName/providerName 兜底
            "visitObjectName": row_data.get("businessLicense", "") or row_data.get("storeName", "") or row_data.get("agentName", "") or row_data.get("providerName", ""),
            "visitObject": row_data.get("visitObject", ""),
            "visitType": row_data.get("visitType", ""),
            "planVisitTime": row_data.get("planVisitTime", ""),
            "planVisitAddress": row_data.get("planVisitAddress", ""),
            "actualVisitTime": actual_time,
            "actualVisitAddress": actual_address,
            "checkinLongitude": checkin_longitude,
            "checkinLatitude": checkin_latitude,
            "visitStatus": row_data.get("visitStatus", ""),
            "checkinStatus": row_data.get("checkinStatus", ""),
        }

        # 关键展示字段缺失时调详情接口补全（PC 用户可能未开启该列展示）
        needs_fallback = (
            not (plan["actualVisitTime"] or plan["planVisitTime"])
            or not (plan["actualVisitAddress"] or plan["planVisitAddress"])
            or not plan["visitObjectName"]
        )
        if needs_fallback and plan["visitPlanId"]:
            print(f"[INFO] 列表字段缺失，调详情补全 visitPlanId={plan['visitPlanId']}", file=sys.stderr)
            detail = _fetch_detail_fallback(plan["visitPlanId"])
            if detail:
                if not plan["actualVisitTime"]:
                    raw = detail.get("actualVisitTime") or detail.get("planVisitTime")
                    plan["actualVisitTime"] = _fmt_time_label(raw)
                if not plan["actualVisitAddress"]:
                    plan["actualVisitAddress"] = detail.get("actualVisitAddress") or detail.get("planVisitAddress") or ""
                if not plan["visitObjectName"]:
                    plan["visitObjectName"] = detail.get("businessLicense", "")
                if not plan["visitObject"]:
                    plan["visitObject"] = detail.get("visitObject", "")

        plans.append(plan)

    return {"total": total, "plans": plans}


def main():
    parser = argparse.ArgumentParser(description="查询拜访计划列表")
    parser.add_argument("--creator-email", required=True, help="创建人邮箱前缀，如 wb_sufan")
    parser.add_argument("--limit", type=int, default=5, help="返回条数上限，默认5")
    parser.add_argument("--status", type=int, default=2,
        help="拜访状态过滤：1=待填写计划, 2=待填写日志(默认), 3=待拜访, 4=已完成, 5=已取消。修改日志场景传 4")
    args = parser.parse_args()

    result = query_visit_plans(creator_email=args.creator_email, limit=args.limit, status=args.status)

    status_desc = {1: "待填写计划", 2: "待填写日志", 3: "待拜访", 4: "已完成", 5: "已取消"}
    if result["total"] == 0 or not result["plans"]:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"\n当前没有状态为「{status_desc.get(args.status, args.status)}」的拜访计划。", file=sys.stderr)
        sys.exit(0)

    # 先打印原始数据（供调试），再单独一行裸输出干净协议块。
    # ⚠️ 关键：协议块绝不能塞进 json.dumps，否则内部双引号会被转义成 \" 导致前端无法解析。
    print(json.dumps(result, ensure_ascii=False, indent=2))

    suggested = _build_result_card(result["plans"])
    print("\n===== SUGGESTED_CARD (整条回复请原样复制下面这一行) =====")
    print(suggested)

    print(
        "⛔ 检测到可出卡状态：把上面 SUGGESTED_CARD 那一行（<<<VISIT_BLOCK_V1>>>...<<<END...>>>）"
        "原样作为整条回复输出，块外零字符，禁止加 \\\" 转义/换行/Markdown。",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
