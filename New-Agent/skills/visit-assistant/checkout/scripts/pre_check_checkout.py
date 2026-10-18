"""
签退预校验 / 签退执行（二合一）

接口：
  预校验：preCheckCheckout  POST .../visitCenter/preCheckCheckout
  签退：  submitCheckout    POST .../visitCenter/submitCheckout

说明：
  通过 --checkin-status 参数区分两种场景（AI 从卡片 data.checkinStatus 透传，无需任何判断）：
    --checkin-status WAIT_CHECKOUT → 用户点了「确认签退」，直接签退，输出 action_result + visit-log-card
    其他值（或不传）               → 首次预校验，通过则输出 WAIT_CHECKOUT 卡片

用法：
  # 首次预校验（用户刚选了计划，checkinStatus 不是 WAIT_CHECKOUT）
  python pre_check_checkout.py --visit-plan-id-str 7082550251951531 \\
      --longitude 116.29 --latitude 40.05 --address "北京市海淀区..."

  # 确认签退（用户点了「确认签退」，前端卡片 data.checkinStatus == "WAIT_CHECKOUT"）
  python pre_check_checkout.py --visit-plan-id-str 7082550251951531 \\
      --longitude 116.29 --latitude 40.05 --address "北京市海淀区..." \\
      --checkin-status WAIT_CHECKOUT \\
      --action-id submit_checkout \\
      --client-action-id client_action_xxx \\
      --card-id auto_visit-check-card_0 \\
      --card-name visit-check-card
"""

import argparse
import json
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_client import call_api


# ──────────────────────────────────────────────
# 详情接口（展示字段补充）
# ──────────────────────────────────────────────

def _fetch_plan_detail(visit_plan_id: str) -> dict:
    """调 queryVisitPlanDetails 补充展示字段（含 visit-log-card 所需全部字段）。失败返回空 dict，不影响主流程。"""
    try:
        data = call_api("queryVisitPlanDetails", {"id": visit_plan_id}, step="get_detail")
        checkin_info = data.get("visitCheckinInfo", {})
        basic_info   = data.get("basicInfo", {})
        title_info   = data.get("titleInfo", {})

        # ── 地址字段（均为嵌套对象 {"address":...}）──
        plan_addr_obj    = checkin_info.get("planVisitAddress", {})
        plan_address     = plan_addr_obj.get("address", "") if isinstance(plan_addr_obj, dict) else str(plan_addr_obj or "")

        checkin_addr_obj = checkin_info.get("checkinAddress", {})
        checkin_address  = checkin_addr_obj.get("address", "") if isinstance(checkin_addr_obj, dict) else str(checkin_addr_obj or "")

        checkout_addr_obj = checkin_info.get("checkoutAddress", {})
        checkout_address  = (checkout_addr_obj.get("address", "") if isinstance(checkout_addr_obj, dict) else str(checkout_addr_obj or "")) or None

        # ── 时间字段 ──
        checkin_time      = checkin_info.get("checkinTime")
        plan_visit_time   = checkin_info.get("planVisitTime")
        actual_visit_time = checkin_info.get("actualVisitTime")
        checkout_time_raw = checkin_info.get("checkoutTime", -1)
        checkout_time     = None if (checkout_time_raw is None or checkout_time_raw == -1) else checkout_time_raw

        # ── 客户信息 ──
        visit_object_type  = basic_info.get("visitObject", "")
        visit_object_label = {"ad_customer": "广告客户", "store": "门店", "agent": "代理商",
                              "provider": "服务商", "other": "其他"}.get(visit_object_type, visit_object_type)
        license_name = basic_info.get("businessLicense", "") or basic_info.get("storeName", "") or data.get("agentName", "") or data.get("providerName", "") or title_info.get("storePoiName", "") or title_info.get("visitObjectName", "")
        license_code = basic_info.get("businessLicenseCode", "")

        visit_id = title_info.get("visitId") or visit_plan_id

        # ── supplementAllowed：专用接口判断，失败降级 0 ──
        supplement_allowed = 0
        try:
            sup_resp = call_api("checkVisitSupplementAllowed", {"visitId": visit_id}, step="check_supplement")
            supplement_allowed = 1 if sup_resp.get("allowed") else 0
        except Exception:
            pass

        # ── 照片 ──
        photo_url = checkin_info.get("checkinPhotoUrl") or None
        if photo_url == "":
            photo_url = None

        return {
            # 兼容旧逻辑（CHECKED_OUT 卡片用）
            "planVisitAddress": plan_address,
            "planVisitTime":    plan_visit_time,
            "signinAddress":    checkin_address,
            "signinTime":       checkin_time,
            "customerName":     license_name,
            # visit-log-card 所需完整字段
            "visitPlanId":      visit_id,
            "visitObjectName":  license_name,
            "visitObject":      visit_object_label,
            "visitObjectType":  visit_object_type,
            "visitObjectInfo":  {
                "licenseName": license_name,
                "licenseCode": license_code,
                "agentId":    data.get("agentId") or None,
                "agentName":  data.get("agentName") or None,
                "storePoiId": checkin_info.get("storePoiId") or None,
                "storePoiName": data.get("storePoiName") or None,
                "providerId": data.get("providerId") or None,
                "providerName": data.get("providerName") or None,
            },
            "checkinTime":       checkin_time,
            "checkinAddress":    checkin_address,
            "checkoutTime":      checkout_time,
            "checkoutAddress":   checkout_address,
            "actualVisitTime":   actual_visit_time,
            "isCheckedOut":      1 if checkout_time is not None else 0,
            "photoUrl":          photo_url,
            "supplementAllowed": supplement_allowed,
            "contacts":          basic_info.get("contacts", {"contact": [], "wechatContact": []}),
        }
    except Exception as e:
        print(f"[WARN] 获取拜访详情失败: {e}", file=sys.stderr)
        return {}


# ──────────────────────────────────────────────
# 卡片构建
# ──────────────────────────────────────────────

def _build_wait_checkout_card(
    visit_plan_id, plan_visit_time, plan_address, customer_name,
    current_address, latitude, longitude, signin_address, signin_time,
) -> str:
    card = {
        "protocol": "visit.session.v1",
        "type": "a2ui.render",
        "card": {
            "card_name": "visit-check-card",
            "schema_version": "1.0",
            "data": {
                "visitPlanId":       visit_plan_id,
                "locationModifyLink": f"locallifevisit://krn?bundleId=LocalLifeVisit&componentName=VisitCheck&themeStyle=3&visitPlanId={visit_plan_id}",
                "planVisitTime":     plan_visit_time,
                "planVisitAddress":  plan_address,
                "visitObjectInfo":   {"licenseName": customer_name},
                "clientName":        customer_name,
                "visitTarget":       {"label": "", "value": ""},
                "checkinStatus":     "WAIT_CHECKOUT",
                "currentAddress":    current_address,
                "latitude":          latitude,
                "longitude":         longitude,
                "signInAddress":     signin_address,
                "signInTime":        signin_time,
                "signOutAddress":    None,
                "signOutTime":       None,
                "currentDistance":              0,
                "currentDistanceToSignInPoint": 0,
                "rangeLimit":          100,
                "isTargetOutOfRange":  False,
                "isCheckoutOvertime":  False,
                "overtimeText":        "",
                "isCheckoutOutOfRange": False,
                "outOfRangeDistance":   0,
                "canCheckOut":    True,
                "cardDisabled":   False,
                "disabledReason": "",
                "buttonDisabled": False,
                "rightBtnText":   "确认签退",
                "isExpired":      False,
            },
            "action_states": {
                "photo_check_in":    {"enabled": False, "visible": True},
                "switch_plan":       {"enabled": True,  "visible": True},
                "refresh_location":  {"enabled": True,  "visible": True},
                "modify_location":   {"enabled": True,  "visible": True},
                "precheck_checkout": {"enabled": True,  "visible": True},
                "submit_checkout":   {"enabled": True,  "visible": True},
            },
        },
    }
    body = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
    return f"<<<VISIT_BLOCK_V1>>>{body}<<<END_VISIT_BLOCK_V1>>>"


def _build_checked_out_card(
    visit_plan_id, plan_visit_time, plan_address, customer_name,
    current_address, latitude, longitude,
    signin_address, signin_time, signout_address, signout_time,
    action_id="submit_checkout", client_action_id="", card_id="auto_visit-check-card_0", card_name="visit-check-card",
) -> str:
    """构建 action_result 格式的 CHECKED_OUT 协议块（skill_result.data 嵌入卡片数据）。"""
    card_data = {
        "visitPlanId":       visit_plan_id,
        "planVisitTime":     plan_visit_time,
        "planVisitAddress":  plan_address,
        "visitObjectInfo":   {"licenseName": customer_name},
        "clientName":        customer_name,
        "visitTarget":       {"label": "", "value": ""},
        "checkinStatus":     "CHECKED_OUT",
        "currentAddress":    current_address,
        "latitude":          latitude,
        "longitude":         longitude,
        "signInAddress":     signin_address,
        "signInTime":        signin_time,
        "signOutAddress":    signout_address,
        "signOutTime":       signout_time,
        "currentDistance":              0,
        "currentDistanceToSignInPoint": 0,
        "rangeLimit":          100,
        "isTargetOutOfRange":  False,
        "isCheckoutOvertime":  False,
        "overtimeText":        "",
        "isCheckoutOutOfRange": False,
        "outOfRangeDistance":   0,
        "canCheckOut":    False,
        "cardDisabled":   False,
        "disabledReason": "",
        "buttonDisabled": False,
        "rightBtnText":   "已签退",
        "isExpired":      False,
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


def _build_fill_log_card(detail: dict, checkout_time, checkout_address: str) -> str:
    """visit-log-card（签退成功后紧跟输出，联系人用接口真实数据，纪要留空）。"""
    info = detail.get("visitObjectInfo", {}) or {}

    # 联系人：id 为 null 时补占位串，否则前端渲染失败
    raw_contacts = detail.get("contacts", {}).get("contact", [])
    norm_contacts = []
    for i, c in enumerate(raw_contacts, start=1):
        cid = c.get("id")
        if not (isinstance(cid, str) and cid.strip()):
            cid = f"contact-{i:03d}"
        norm_contacts.append({
            "id":   cid,
            "name": c.get("name", ""),
            "phone": c.get("phone") if c.get("phone") else None,
            "isKp": int(c.get("isKp", 0) or 0),
        })

    card = {
        "protocol": "visit.session.v1",
        "type": "a2ui.render",
        "card": {
            "card_name": "visit-log-card",
            "schema_version": "1.0",
            "data": {
                "visitId":          detail.get("visitPlanId"),
                "visitObjectName":  detail.get("visitObjectName", ""),
                "visitObject":      detail.get("visitObject", ""),
                "visitObjectType":  detail.get("visitObjectType", ""),
                "visitObjectInfo": {
                    "licenseName": info.get("licenseName") or detail.get("visitObjectName", ""),
                    "licenseCode": info.get("licenseCode") or "",
                    "agentId":     info.get("agentId"),
                    "agentName":   info.get("agentName"),
                    "storePoiId":  info.get("storePoiId"),
                    "storePoiName":info.get("storePoiName"),
                    "providerId":  info.get("providerId"),
                    "providerName":info.get("providerName"),
                },
                "planVisitTime":    detail.get("planVisitTime"),
                "planVisitAddress": detail.get("planVisitAddress") or "",
                "checkinTime":      detail.get("checkinTime"),
                "checkinAddress":   detail.get("checkinAddress") or "",
                "checkinAbnormalInfo":   None,
                "checkinAbnormalReason": None,
                "isCheckedOut":     1,
                "checkoutTime":     checkout_time,
                "checkoutAddress":  checkout_address or None,
                "photoUrl":         detail.get("photoUrl"),
                "checkoutPhotoUrl": None,
                "supplementAllowed": int(detail.get("supplementAllowed", 0) or 0),
                "actualVisitTime":  detail.get("actualVisitTime"),
                "contacts":         {"contact": norm_contacts, "wechatContact": []},
                "visitSummary":     "",
            },
            "action_states": {
                "submit_log":         {"enabled": True, "visible": True},
                "visit_target_press": {"enabled": True, "visible": True},
            },
        },
    }
    body = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
    return f"<<<VISIT_BLOCK_V1>>>{body}<<<END_VISIT_BLOCK_V1>>>"


# ──────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="签退预校验 / 签退执行")
    parser.add_argument("--visit-plan-id-str", required=True)
    parser.add_argument("--longitude", type=float, required=True)
    parser.add_argument("--latitude",  type=float, required=True)
    parser.add_argument("--address", default="")
    parser.add_argument("--checkin-status", default="",
                        help="从卡片 data.checkinStatus 透传。WAIT_CHECKOUT 时直接签退，其他值做预校验")
    parser.add_argument("--checkin-longitude", type=float, default=None,
                        help="签到时的经度（超距时对比展示，来自 query_visit_plans 返回的 checkinLongitude）")
    parser.add_argument("--checkin-latitude", type=float, default=None,
                        help="签到时的纬度（超距时对比展示，来自 query_visit_plans 返回的 checkinLatitude）")
    parser.add_argument("--action-id", default="submit_checkout",
                        help="前端回传的 action_id，原样返回")
    parser.add_argument("--client-action-id", default="",
                        help="前端回传的 client_action_id，原样返回")
    parser.add_argument("--card-id", default="auto_visit-check-card_0",
                        help="前端回传的 card_id，原样返回")
    parser.add_argument("--card-name", default="visit-check-card",
                        help="前端回传的 card_name，原样返回")
    args = parser.parse_args()

    # ── 经纬度兜底：如果传入 0.0，自动换用签到经纬度 ────────────────────
    longitude = args.longitude
    latitude  = args.latitude
    if (longitude == 0.0 or latitude == 0.0):
        if args.checkin_longitude and args.checkin_latitude:
            print(
                f"[INFO] 当前经纬度为 0.0，兜底使用签到经纬度: "
                f"lng={args.checkin_longitude}, lat={args.checkin_latitude}",
                file=sys.stderr,
            )
            longitude = args.checkin_longitude
            latitude  = args.checkin_latitude
        else:
            print(
                "[WARN] 当前经纬度为 0.0，且无签到经纬度兜底，将使用 0.0 继续",
                file=sys.stderr,
            )

    # ── 路径 A：checkinStatus=WAIT_CHECKOUT，直接签退 ─────────────────
    if args.checkin_status == "WAIT_CHECKOUT":
        call_api("submitCheckout", {
            "visitPlanIdStr": args.visit_plan_id_str,
            "longitude": longitude,
            "latitude":  latitude,
            "address":   args.address,
        }, step="submit_checkout")

        result = {"success": True, "visitPlanId": args.visit_plan_id_str}
        print(json.dumps(result, ensure_ascii=False, indent=2))

        detail = _fetch_plan_detail(args.visit_plan_id_str)
        signout_time = int(time.time() * 1000)
        suggested = _build_checked_out_card(
            visit_plan_id=args.visit_plan_id_str,
            plan_visit_time=detail.get("planVisitTime"),
            plan_address=detail.get("planVisitAddress", ""),
            customer_name=detail.get("customerName", ""),
            current_address=args.address,
            latitude=latitude,
            longitude=longitude,
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
        # 签退成功后紧接着下发填写日志卡片
        fill_log_card = _build_fill_log_card(
            detail=detail,
            checkout_time=signout_time,
            checkout_address=args.address,
        )
        print(fill_log_card)
        print("⛔ 签退成功：把上面 SUGGESTED_CARD 下方的两行（CHECKED_OUT卡片 + visit-log-card）"
              "依次原样输出，块外零字符，禁止加 \\\" 转义/换行/Markdown。", file=sys.stderr)
        return

    # ── 路径 B：其他状态，做预校验 ───────────────────────────────────
    data = call_api("preCheckCheckout", {
        "visitPlanIdStr": args.visit_plan_id_str,
        "longitude": longitude,
        "latitude":  latitude,
        "address":   args.address,
    }, step="pre_check")

    can_checkout = data.get("canCheckout", False)
    has_abnormal = data.get("hasAbnormal", False)
    abnormal_list_raw = data.get("abnormalList", [])
    abnormal_tags = data.get("abnormalTags", [])

    abnormal_list = [{"abnormalType": i.get("abnormalType",""), "title": i.get("title",""),
                      "content": i.get("content",""), "buttons": i.get("buttons",[])}
                     for i in abnormal_list_raw]

    if can_checkout and not has_abnormal:
        message = "校验通过，可以签退"
    elif can_checkout:
        message = "校验通过但有异常，确认后可签退"
    else:
        message = ""
        for ab in abnormal_list:
            if ab["abnormalType"] == "checkout_distance_abnormal":
                # 超出 1km：附上签到位置 vs 当前位置坐标，方便用户直观了解原因
                coord_info = ""
                if args.checkin_longitude is not None and args.checkin_latitude is not None:
                    coord_info = (
                        f"\n【位置对比】"
                        f"\n签到位置：纬度 {args.checkin_latitude:.6f}，经度 {args.checkin_longitude:.6f}"
                        f"\n当前位置：纬度 {latitude:.6f}，经度 {longitude:.6f}"
                    )
                message = f"超出签到地1km范围则无法签退。{ab.get('content', '')}{coord_info}"; break
            elif ab["abnormalType"] == "checkout_expired":
                message = f"签退最晚次日早6:00。{ab.get('content', '')}"; break
        if not message:
            message = data.get("abnormalReason") or "签退校验未通过，无法签退"

    result = {"canCheckout": can_checkout, "hasAbnormal": has_abnormal,
              "abnormalList": abnormal_list, "abnormalTags": abnormal_tags, "message": message}
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if not can_checkout:
        print(f"⛔ 预校验未通过: {message}", file=sys.stderr)
        sys.exit(1)

    detail = _fetch_plan_detail(args.visit_plan_id_str)
    suggested = _build_wait_checkout_card(
        visit_plan_id=args.visit_plan_id_str,
        plan_visit_time=detail.get("planVisitTime"),
        plan_address=detail.get("planVisitAddress", ""),
        customer_name=detail.get("customerName", ""),
        current_address=args.address,
        latitude=latitude,
        longitude=longitude,
        signin_address=detail.get("signinAddress", ""),
        signin_time=detail.get("signinTime"),
    )
    print("\n===== SUGGESTED_CARD (整条回复请原样复制下面这一行) =====")
    print(suggested)
    print("⛔ 预校验通过：把上面 SUGGESTED_CARD 那一行原样作为整条回复输出，"
          "块外零字符，禁止加 \\\" 转义/换行/Markdown。", file=sys.stderr)


if __name__ == "__main__":
    main()
