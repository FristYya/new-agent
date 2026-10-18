"""
补录签退（APP 端，签退校验失败时走补录流程）

涉及两个接口：
1. checkVisitSupplementAllowed — 检查是否允许补录
2. submitLaterCheckin — 执行补录签退

用法：
  # 仅检查是否可补录
  python check_supplement.py --visit-id 6935322433955077 --check-only

  # 检查并尝试补录
  python check_supplement.py --visit-id 6935322433955077 --check-out-type 2
"""

import argparse
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_client import call_api


def check_supplement_allowed(visit_id: str) -> dict:
    """检查是否允许补录"""
    payload = {"visitId": visit_id}
    data = call_api("checkVisitSupplementAllowed", payload, step="check_supplement")

    allowed = data.get("allowed", False)
    return {"allowed": allowed, "message": "可以补录签退" if allowed else "没有可用的补录信息，请确认是否已开启打卡提醒"}


def submit_later_checkin(visit_id: str, check_out_type: int = 2) -> dict:
    """执行补录签退"""
    payload = {"visitId": visit_id, "checkOutType": check_out_type}
    data = call_api("submitLaterCheckin", payload, step="submit_later_checkin")

    checkout_time = data.get("checkoutTime", "")
    checkout_address = data.get("checkoutAddress", "")
    return {
        "supplemented": True,
        "checkoutTime": checkout_time,
        "checkoutAddress": checkout_address,
        "message": "补录签退成功，别忘了填写拜访日志！"
    }


def check_and_supplement(visit_id: str, check_out_type: int = 2) -> dict:
    """完整补录流程：先检查是否允许 → 允许则补录"""
    check_result = check_supplement_allowed(visit_id)

    if not check_result["allowed"]:
        return {"allowed": False, "supplemented": False, "message": check_result["message"]}

    supplement_result = submit_later_checkin(visit_id, check_out_type)
    return {
        "allowed": True,
        "supplemented": True,
        "checkoutTime": supplement_result["checkoutTime"],
        "checkoutAddress": supplement_result["checkoutAddress"],
        "message": supplement_result["message"]
    }


def main():
    parser = argparse.ArgumentParser(description="补录签退")
    parser.add_argument("--visit-id", required=True, help="拜访计划ID")
    parser.add_argument("--check-only", action="store_true", help="仅检查是否允许补录，不执行补录")
    parser.add_argument("--check-out-type", type=int, default=2, help="补录类型: 1-签退, 2-补录（默认2）")
    args = parser.parse_args()

    if args.check_only:
        result = check_supplement_allowed(args.visit_id)
    else:
        result = check_and_supplement(visit_id=args.visit_id, check_out_type=args.check_out_type)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
