"""
查询拜访待办事项（今日待办 / 待签到 / 待签退 / 待填日志 / 待填写差旅计划）

接口：
  - queryVisitPlanList（PC 端 visit-service）
    路径：POST http://127.0.0.1:8080/rest/apicenter/visit/web/visitNew/queryVisitPlanList
  - queryAiVisit（通用只读查询，用于「待填写差旅计划」）
    路径：POST http://127.0.0.1:8080/rest/apicenter/visit/web/visitCenter/queryAiVisit

三类待办的筛选条件（queryVisitPlanList）：
  - 待签到 pending-check-in：filterCondition = [{"key":"clockStatus","value":["0"]}]
  - 待签退 pending-check-out：filterCondition = [{"key":"clockStatus","value":["1"]}]
  - 待填日志 pending-log：       filterCondition = [{"key":"visitStatus","value":["2"]}]

待填写差旅计划（queryAiVisit）：
  - queryType = "pending_travel"
  - 返回 dataJson 中的 list 即待补充差旅计划数组

时间标签规则（timeLabel）：
  - 待签到：取 planVisitTime，格式化为「MM月DD日」（纯日期）
  - 待签退 / 待填日志：取 actualVisitTime，格式化为「MM月DD日 HH：MM」（日期+时间，冒号用全角"："）

用法：
  python query_pending.py --creator-email lianjie03 --mode all            # 今日待办（四类全查）
  python query_pending.py --creator-email lianjie03 --mode check-in       # 仅待签到
  python query_pending.py --creator-email lianjie03 --mode check-out      # 仅待签退
  python query_pending.py --creator-email lianjie03 --mode log            # 仅待填日志
  python query_pending.py --creator-email lianjie03 --mode travel-plan    # 仅待填写差旅计划

输出 JSON（直接对应 visit-pending-card 的 data）：
{
  "total": 9,
  "pendingTravelPlanCount": 2,
  "pendingCheckInCount": 3,
  "pendingCheckOutCount": 2,
  "pendingLogCount": 2,
  "pendingTravelPlanItems": [...],
  "pendingCheckInItems": [...],
  "pendingCheckOutItems": [...],
  "pendingLogItems": [...]
}
"""

import argparse
import json
import sys
import os
from datetime import datetime, time, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_client import call_api


# 四类待办的元信息：筛选条件 + pendingType + 时间来源
PENDING_TYPES = {
    "check-in": {
        "pendingType": "pending-check-in",
        "filterCondition": [
            {"key": "clockStatus", "value": ["0"]},
            {"key": "visitStatus", "value": ["2", "3", "4", "5"]},  # 排除待填写计划(visitStatus=1)
        ],
        "timeField": "planVisitTime",   # 待签到用计划拜访时间
        "timeWithClock": False,          # 纯日期
    },
    "check-out": {
        "pendingType": "pending-check-out",
        "filterCondition": [{"key": "clockStatus", "value": ["1"]}],
        "timeField": "actualVisitTime",  # 待签退用实际签到时间
        "timeWithClock": True,           # 日期+时间
    },
    "log": {
        "pendingType": "pending-log",
        "filterCondition": [{"key": "visitStatus", "value": ["2"]}],
        "timeField": "actualVisitTime",  # 待填日志用实际签到时间
        "timeWithClock": True,           # 日期+时间
    },
    # 待填写差旅计划走另一个接口 queryAiVisit(queryType=pending_travel)，字段与上面三类不同
    "travel-plan": {
        "pendingType": "pending-travel-plan",
    },
}


def _format_time_label(raw_ts: str, with_clock: bool) -> str:
    """
    毫秒时间戳字符串 → 时间标签

    with_clock=False → "07月15日"
    with_clock=True  → "07月15日 15：30"（冒号用全角"："，与前端 demo 一致）
    """
    if not raw_ts or not str(raw_ts).isdigit():
        return ""
    try:
        ts = int(raw_ts)
        if ts > 1000000000000:
            ts = ts // 1000  # 毫秒转秒
        dt = datetime.fromtimestamp(ts)
        if with_clock:
            return dt.strftime("%m月%d日 %H：%M")
        return dt.strftime("%m月%d日")
    except (ValueError, OSError):
        return ""


def _extract_address(raw) -> str:
    """地址可能是纯字符串或 JSON 字符串 {address, longitude, latitude}"""
    if not raw:
        return ""
    if isinstance(raw, str):
        if raw.startswith("{"):
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict) and "address" in obj:
                    return obj["address"]
            except (json.JSONDecodeError, TypeError):
                pass
        return raw
    if isinstance(raw, dict):
        return raw.get("address", "")
    return str(raw)


def _fetch_detail_fallback(visit_id: str) -> dict:
    """
    当列表接口关键字段缺失时，直接用 requests 调 queryVisitPlanDetails 补全。
    完全独立于全局 api_client（避免 sys.exit 污染主流程），失败返回空 dict。
    """
    if not visit_id:
        return {}
    try:
        import requests as _req
        from api_client import _get_pc_cookie
        cookie = _get_pc_cookie() or ""
        url = "http://127.0.0.1:8080/rest/apicenter/visit/web/visitNew/queryVisitPlanDetails"
        headers = {
            "Cookie": cookie,
            "Content-Type": "application/json",
            "Referer": "http://127.0.0.1:8080/visit/tools/visit",
        }
        resp = _req.post(url, json={"id": visit_id}, headers=headers, timeout=(10, 15))
        if resp.status_code != 200:
            print(f"[WARN] 详情补全 HTTP {resp.status_code} visitId={visit_id}", file=sys.stderr)
            return {}
        body = resp.json()
        code = (body.get("baseResponse") or {}).get("resultCode", (body.get("baseResponse") or {}).get("code", 0))
        if code != 0:
            print(f"[WARN] 详情补全业务错误 code={code} visitId={visit_id}", file=sys.stderr)
            return {}
        data = body.get("data") or {}
        checkin_info = data.get("visitCheckinInfo", {}) or {}
        basic_info   = data.get("basicInfo", {}) or {}

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
            "businessLicense":    basic_info.get("businessLicense", ""),
            "visitObject":        basic_info.get("visitObject", ""),
        }
    except Exception as e:
        print(f"[WARN] 详情接口补全失败 visitId={visit_id}: {e}", file=sys.stderr)
        return {}


def _build_item(row_data: dict, meta: dict) -> dict:
    """把一行原始 data 转成卡片 item；关键字段缺失时自动调详情接口补全"""
    visit_id = row_data.get("visitId", "")
    pending_type = meta["pendingType"]

    # ── 判断关键字段是否需要补全 ──
    time_field = meta["timeField"]
    raw_time = row_data.get(time_field, "")
    # 地址：待签到取计划地址，待签退/待填日志取实际签到地址
    if pending_type == "pending-check-in":
        raw_address = row_data.get("planVisitAddress", "")
    else:
        raw_address = _extract_address(row_data.get("actualVisitAddress", "")) or row_data.get("planVisitAddress", "")
    customer_name = row_data.get("businessLicense", "")
    visit_object = row_data.get("visitObject", "")

    # 关键字段任意一个为空，调详情补全
    needs_fallback = (not raw_time or not raw_address or not customer_name)
    if needs_fallback and visit_id:
        print(f"[INFO] 列表字段缺失(timeField={raw_time!r}, addr={raw_address!r}, name={customer_name!r})，调详情补全 visitId={visit_id}", file=sys.stderr)
        detail = _fetch_detail_fallback(visit_id)
        if detail:
            if not raw_time:
                raw_time = str(detail.get(time_field) or detail.get("planVisitTime") or detail.get("actualVisitTime") or "")
            if not raw_address:
                if pending_type == "pending-check-in":
                    raw_address = detail.get("planVisitAddress") or ""
                else:
                    raw_address = detail.get("actualVisitAddress") or detail.get("planVisitAddress") or ""
            if not customer_name:
                customer_name = detail.get("businessLicense", "")
            if not visit_object:
                visit_object = detail.get("visitObject", "")

    # 地址二次解析（列表返回的 actualVisitAddress 可能是 JSON 字符串）
    address = _extract_address(raw_address) if raw_address else ""

    time_label = _format_time_label(raw_time, meta["timeWithClock"])

    return {
        "visitId": visit_id,
        "pendingType": pending_type,
        "customerName": customer_name,
        "customerTypeLabel": visit_object,
        "timeLabel": time_label,
        "address": address,
    }


def _query_one(creator_email: str, mode_key: str, limit: int, today: bool = False) -> list:
    """查询单类待办，返回 item 列表；同时把接口 URL/入参/原始响应打印到 stderr 供排查"""
    meta = PENDING_TYPES[mode_key]
    payload = {
        "creatorEmail": creator_email,
        "filterCondition": meta["filterCondition"],
        "pageInfo": {"pageNum": 1, "pageSize": limit},
    }

    # 待签退需要限制签到时间范围：签到当天 → 次日 06:00 截止，过期的不可签退不展示
    if mode_key == "check-out":
        now = datetime.now()
        today_6am = datetime.combine(now.date(), time(6, 0, 0))
        if now >= today_6am:
            sign_in_start = datetime.combine(now.date(), time(0, 0, 0))
        else:
            sign_in_start = datetime.combine(now.date() - timedelta(days=1), time(0, 0, 0))
        payload["timeCondition"] = {
            "signInTimeStart": int(sign_in_start.timestamp() * 1000),
            "signInTimeEnd":   int(now.timestamp() * 1000),
        }

    # 待签到 + today 模式：只查计划拜访时间在今天的
    if mode_key == "check-in" and today:
        now = datetime.now()
        today_start = datetime.combine(now.date(), time(0, 0, 0))
        today_end   = datetime.combine(now.date(), time(23, 59, 59))
        payload["timeCondition"] = {
            "plannedVisitTimeStart": int(today_start.timestamp() * 1000),
            "plannedVisitTimeEnd":   int(today_end.timestamp() * 1000),
        }

    # ── 调试信息：接口 URL + 入参 ──
    debug_url = f"http://127.0.0.1:8080/rest/apicenter/visit/web/visitNew/queryVisitPlanList"
    debug_info = {
        "step": f"pending_{mode_key}",
        "url": debug_url,
        "request_payload": payload,
    }
    print(f"[DEBUG] 接口调用: {json.dumps(debug_info, ensure_ascii=False)}", file=sys.stderr)

    try:
        data = call_api("queryVisitPlanList", payload, step=f"pending_{mode_key}")
        debug_info["response_data"] = data
        print(f"[DEBUG] 接口响应: {json.dumps(data, ensure_ascii=False)}", file=sys.stderr)
    except Exception as e:
        debug_info["error"] = str(e)
        print(f"[DEBUG] 接口异常: {e}", file=sys.stderr)
        # 异常时把调试信息记到 _debug_log 供 main() 展示
        _DEBUG_LOG.append(debug_info)
        raise

    rows = data.get("dataRow", [])
    items = [_build_item(row.get("data", {}), meta) for row in rows]

    debug_info["response_data"] = data
    debug_info["parsed_count"] = len(items)
    _DEBUG_LOG.append(debug_info)

    return items


def _query_travel_plans(creator_email: str, limit: int) -> list:
    """查询待填写差旅计划（queryAiVisit queryType=pending_travel）

    返回的 dataJson 是 JSON 字符串，解析后结构为：
      {"total": N, "list": [{visitPlanId, travelApplyNo, cities, planVisitTime, ...}, ...]}
    或直接为 list。兼容两种形态。

    组装成 visit-pending-card 的 pendingTravelPlanItems 元素，字段：
      visitId          ← visitPlanId / planId
      pendingType      ← "pending-travel-plan"
      travelOrderNo    ← travelApplyNo（差旅单号）
      travelLocation   ← cities 拼接，或单个 city 字段（"北京-上海-杭州"格式）
      travelTime       ← planVisitTime 格式化为「MM月DD日」（纯日期）
    """
    payload = {
        "queryType": "pending_travel",
        "limit": limit,
        "baseRequest": {"user_name": creator_email},
    }

    debug_url = "http://127.0.0.1:8080/rest/apicenter/visit/web/visitCenter/queryAiVisit"
    debug_info = {
        "step": "pending_travel-plan",
        "url": debug_url,
        "request_payload": payload,
    }
    print(f"[DEBUG] 接口调用: {json.dumps(debug_info, ensure_ascii=False)}", file=sys.stderr)

    try:
        data = call_api("queryAiVisit", payload, step="pending_travel-plan")
        debug_info["response_data"] = data
        print(f"[DEBUG] 接口响应: {json.dumps(data, ensure_ascii=False)}", file=sys.stderr)
    except Exception as e:
        debug_info["error"] = str(e)
        print(f"[DEBUG] 接口异常: {e}", file=sys.stderr)
        _DEBUG_LOG.append(debug_info)
        raise

    # dataJson 是 JSON 字符串，解析出 list
    data_json_str = data.get("dataJson") or data.get("data_json", "")
    plans: list = []
    if data_json_str:
        try:
            parsed = json.loads(data_json_str)
            if isinstance(parsed, dict):
                raw_list = parsed.get("list") or []
            elif isinstance(parsed, list):
                raw_list = parsed
            else:
                raw_list = []
            plans = [p for p in raw_list if isinstance(p, dict)]
        except (json.JSONDecodeError, TypeError) as e:
            print(f"[WARN] dataJson 解析失败: {e}", file=sys.stderr)
    # 兼容：data 直接是 list 的情况
    elif isinstance(data, list):
        plans = [p for p in data if isinstance(p, dict)]
    # 兼容：data.list 的情况
    elif isinstance(data, dict) and isinstance(data.get("list"), list):
        plans = [p for p in data["list"] if isinstance(p, dict)]

    items = [_build_travel_plan_item(p) for p in plans]

    debug_info["parsed_count"] = len(items)
    _DEBUG_LOG.append(debug_info)

    return items


def _build_travel_plan_item(plan: dict) -> dict:
    """把一条差旅待补充计划组装成 visit-pending-card 的 travel-plan item

    接口实际返回结构（getPendingTravelPlans）：
      {
        "visitId": "7173211095476482",
        "planVisitTime": "2026-08-18",          # 已格式化的日期字符串 yyyy-MM-dd
        "travelApply": "{\"travelApplyNo\":\"xxx\", \"travelStartCity\":\"北京\",
                         \"travelViaCity\":\"上海\", ...}"  # JSON 字符串，需二次解析
      }
    """
    visit_id = plan.get("visitId") or plan.get("visitPlanId") or plan.get("planId") or ""

    # travelApply 是 JSON 字符串，需要解析
    travel_apply_raw = plan.get("travelApply") or ""
    travel_apply: dict = {}
    if travel_apply_raw:
        if isinstance(travel_apply_raw, dict):
            travel_apply = travel_apply_raw
        elif isinstance(travel_apply_raw, str):
            try:
                parsed = json.loads(travel_apply_raw)
                if isinstance(parsed, dict):
                    travel_apply = parsed
            except (json.JSONDecodeError, TypeError):
                pass

    # 差旅单号：travelApply.travelApplyNo
    travel_order_no = travel_apply.get("travelApplyNo") or ""

    # 目的地：travelApply.travelViaCity（目的地城市），出发地为 travelStartCity
    travel_location = travel_apply.get("travelViaCity") or travel_apply.get("travelStartCity") or ""

    # 拜访时间：planVisitTime 已经是格式化字符串 "yyyy-MM-dd"，转为「MM月DD日」
    raw_time_str = plan.get("planVisitTime") or ""
    travel_time = ""
    if raw_time_str:
        try:
            dt = datetime.strptime(str(raw_time_str).strip(), "%Y-%m-%d")
            travel_time = dt.strftime("%m月%d日")
        except ValueError:
            # 兜底：如果是毫秒时间戳
            travel_time = _format_time_label(str(raw_time_str), with_clock=False)

    return {
        "visitId": visit_id,
        "pendingType": "pending-travel-plan",
        "travelOrderNo": travel_order_no,
        "travelLocation": travel_location,
        "travelTime": travel_time,
    }


# 全局调试日志，供 main() 在结果为 0 或异常时展示
_DEBUG_LOG: list = []


def query_pending(creator_email: str, mode: str, limit: int = 20, today: bool = False) -> dict:
    """
    查询待办

    Args:
        creator_email: 创建人邮箱前缀（如 lianjie03）
        mode: all | check-in | check-out | log | travel-plan
        limit: 每类返回条数上限
        today: 为 True 时，check-in 模式只查今天计划拜访的

    Returns:
        visit-pending-card 的 data 结构。
        单意图模式下，其他三类列表返回空数组、count=0，total 只计当前类。
        all 模式下四类全查，total 为四类之和。
    """
    check_in_items = []
    check_out_items = []
    log_items = []
    travel_plan_items = []

    if mode in ("all", "check-in"):
        check_in_items = _query_one(creator_email, "check-in", limit, today=today)
    if mode in ("all", "check-out"):
        check_out_items = _query_one(creator_email, "check-out", limit)
    if mode in ("all", "log"):
        log_items = _query_one(creator_email, "log", limit)
    if mode in ("all", "travel-plan"):
        travel_plan_items = _query_travel_plans(creator_email, limit)

    pending_travel_plan_count = len(travel_plan_items)
    pending_check_in_count = len(check_in_items)
    pending_check_out_count = len(check_out_items)
    pending_log_count = len(log_items)
    total = (
        pending_travel_plan_count
        + pending_check_in_count
        + pending_check_out_count
        + pending_log_count
    )

    return {
        "total": total,
        "pendingTravelPlanCount": pending_travel_plan_count,
        "pendingCheckInCount": pending_check_in_count,
        "pendingCheckOutCount": pending_check_out_count,
        "pendingLogCount": pending_log_count,
        "pendingTravelPlanItems": travel_plan_items,
        "pendingCheckInItems": check_in_items,
        "pendingCheckOutItems": check_out_items,
        "pendingLogItems": log_items,
    }


def main():
    parser = argparse.ArgumentParser(description="查询拜访待办事项")
    parser.add_argument("--creator-email", required=True, help="创建人邮箱前缀，如 lianjie03")
    parser.add_argument("--mode", required=True,
                        choices=["all", "check-in", "check-out", "log", "travel-plan"],
                        help="all=今日待办(四类全查) | check-in=仅待签到 | check-out=仅待签退 "
                             "| log=仅待填日志 | travel-plan=仅待填写差旅计划")
    parser.add_argument("--limit", type=int, default=20, help="每类返回条数上限，默认20")
    parser.add_argument("--today", action="store_true", default=False,
                        help="check-in 模式下只查今天计划拜访时间的待签到")
    args = parser.parse_args()

    error_msg = None
    result = None
    try:
        result = query_pending(creator_email=args.creator_email, mode=args.mode,
                               limit=args.limit, today=args.today)
    except Exception as e:
        error_msg = str(e)
        result = {
            "total": 0,
            "pendingTravelPlanCount": 0,
            "pendingCheckInCount": 0,
            "pendingCheckOutCount": 0,
            "pendingLogCount": 0,
            "pendingTravelPlanItems": [],
            "pendingCheckInItems": [],
            "pendingCheckOutItems": [],
            "pendingLogItems": [],
        }

    print(json.dumps(result, ensure_ascii=False, indent=2))

    if error_msg:
        print(f"[ERROR] 脚本执行异常: {error_msg}", file=sys.stderr)

if __name__ == "__main__":
    main()
