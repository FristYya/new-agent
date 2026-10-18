"""
提交签退

接口：submitCheckout
路径：POST http://127.0.0.1:8080/rest/apicenter/visit/app/visitAi/visitCenter/submitCheckout（APP 端）

请求体：
  {visitPlanIdStr, longitude, latitude, address}

说明：
  签退成功后，脚本会自动调 queryVisitPlanDetails 接口补充展示字段，然后生成 CHECKED_OUT 卡片。
  无需 AI 模型手动传 customer-name / plan-address 等展示参数。

用法：
  python submit_checkout.py --visit-plan-id-str 7082550251951531 --longitude 116.29 --latitude 40.05 --address "北京市海淀区..."
"""

import argparse
import json
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_client import call_api


def submit_checkout(visit_plan_id_str: str, longitude: float, latitude: float, address: str) -> dict:
    """提交签退"""
    payload = {
        "visitPlanIdStr": visit_plan_id_str,
        "longitude": longitude,
        "latitude": latitude,
        "address": address,
    }
    call_api("submitCheckout", payload, step="submit_checkout")
    return {"success": True, "visitPlanId": visit_plan_id_str, "message": "签退成功，别忘了填写拜访日志！"}


def _fetch_plan_detail(visit_plan_id: str) -> dict:
    """
    调 queryVisitPlanDetails 获取卡片展示字段。
    失败返回空 dict，不影响主流程。
    """
    try:
        data = call_api("queryVisitPlanDetails", {"id": visit_plan_id}, step="get_detail")
        checkin_info = data.get("visitCheckinInfo", {})
        basic_info = data.get("basicInfo", {})
        title_info = data.get("titleInfo", {})

        plan_addr_obj = checkin_info.get("planVisitAddress", {})
        plan_address = plan_addr_obj.get("address", "") if isinstance(plan_addr_obj, dict) else str(plan_addr_obj or "")

        checkin_addr_obj = checkin_info.get("checkinAddress", {})
        signin_address = checkin_addr_obj.get("address", "") if isinstance(checkin_addr_obj, dict) else str(checkin_addr_obj or "")

        customer_name = basic_info.get("businessLicense", "") or title_info.get("visitObjectName", "")

        return {
            "planVisitAddress": plan_address,
            "planVisitTime": checkin_info.get("planVisitTime"),
            "signinAddress": signin_address,
            "signinTime": checkin_info.get("checkinTime"),
            "customerName": customer_name,
        }
    except Exception as e:
        print(f"[WARN] 获取拜访详情失败（不影响签退结果）: {e}", file=sys.stderr)
        return {}


def _build_checked_out_card(
    visit_plan_id: str,
    plan_visit_time,
    plan_address: str,
    customer_name: str,
    current_address: str,
    latitude: float,
    longitude: float,
    signin_address: str,
    signin_time,
    signout_address: str,
    signout_time,
    action_id: str = "submit_checkout",
    client_action_id: str = "",
    card_id: str = "auto_visit-check-card_0",
    card_name: str = "visit-check-card",
) -> str:
    """构建 action_result 格式的 CHECKED_OUT 协议块（skill_result.data 嵌入卡片数据）。"""
    card_data = {
        "visitPlanId": visit_plan_id,
        "planVisitTime": plan_visit_time,
        "planVisitAddress": plan_address,
        "visitObjectInfo": {"licenseName": customer_name},
        "clientName": customer_name,
        "visitTarget": {"label": "", "value": ""},
        "checkinStatus": "CHECKED_OUT",
        "currentAddress": current_address,
        "latitude": latitude,
        "longitude": longitude,
        "signInAddress": signin_address,
        "signInTime": signin_time,
        "signOutAddress": signout_address,
        "signOutTime": signout_time,
        "currentDistance": 0,
        "currentDistanceToSignInPoint": 0,
        "rangeLimit": 100,
        "isTargetOutOfRange": False,
        "isCheckoutOvertime": False,
        "overtimeText": "",
        "isCheckoutOutOfRange": False,
        "outOfRangeDistance": 0,
        "canCheckOut": False,
        "cardDisabled": False,
        "disabledReason": "",
        "buttonDisabled": False,
        "rightBtnText": "已签退",
        "isExpired": False,
    }
    result = {
        "action_id":        action_id,
        "client_action_id": client_action_id,
        "protocol":         "visit.session.v1",
        "skill_name":       "checkout",
        "skill_result": {
            "success": True,
            "data": card_data,
        },
        "target": {
            "card_id":   card_id,
            "card_name": card_name,
        },
    }
    body = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    return f"<<<VISIT_BLOCK_V1>>>{body}<<<END_VISIT_BLOCK_V1>>>"


def main():
    parser = argparse.ArgumentParser(description="提交签退")
    parser.add_argument("--visit-plan-id-str", required=True, help="拜访计划ID（字符串）")
    parser.add_argument("--longitude", type=float, required=True, help="签退经度")
    parser.add_argument("--latitude", type=float, required=True, help="签退纬度")
    parser.add_argument("--address", default="", help="签退地址")
    parser.add_argument("--action-id", default="submit_checkout",
                        help="前端回传的 action_id，原样返回")
    parser.add_argument("--client-action-id", default="",
                        help="前端回传的 client_action_id，原样返回")
    parser.add_argument("--card-id", default="auto_visit-check-card_0",
                        help="前端回传的 card_id，原样返回")
    parser.add_argument("--card-name", default="visit-check-card",
                        help="前端回传的 card_name，原样返回")
    args = parser.parse_args()

    result = submit_checkout(args.visit_plan_id_str, args.longitude, args.latitude, args.address)

    out = {"success": result.get("success", False), "visitPlanId": result.get("visitPlanId")}
    print(json.dumps(out, ensure_ascii=False, indent=2))

    if result.get("success"):
        # 自动调详情接口补充卡片展示字段
        detail = _fetch_plan_detail(args.visit_plan_id_str)

        signout_time = int(time.time() * 1000)
        suggested = _build_checked_out_card(
            visit_plan_id=args.visit_plan_id_str,
            plan_visit_time=detail.get("planVisitTime"),
            plan_address=detail.get("planVisitAddress", ""),
            customer_name=detail.get("customerName", ""),
            current_address=args.address,
            latitude=args.latitude,
            longitude=args.longitude,
            signin_address=detail.get("signinAddress", ""),
            signin_time=detail.get("signinTime"),
            signout_address=args.address,
            signout_time=signout_time,
            action_id=args.action_id,
            client_action_id=args.client_action_id,
            card_id=args.card_id,
            card_name=args.card_name,
        )
        print("\n===== SUGGESTED_CARD (整条回复请原样复制下面这一行) =====")
        print(suggested)
        print(
            "⛔ 签退成功：把上面 SUGGESTED_CARD 那一行原样作为整条回复输出，"
            "块外零字符，禁止加 \\\" 转义/换行/Markdown。",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
