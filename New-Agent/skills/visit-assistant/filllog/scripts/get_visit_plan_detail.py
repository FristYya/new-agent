"""
查询拜访计划详情（含填写日志所需的完整字段）

接口：queryVisitPlanDetails（PC 端 visit-service）
路径：POST http://127.0.0.1:8080/rest/apicenter/visit/web/visitNew/queryVisitPlanDetails
请求体：{"id": "<visitPlanId>"}

用法一：选计划后直接生成 visit-log-card（空联系人/纪要，等用户在卡片填）
  python get_visit_plan_detail.py --visit-plan-id 7082572911711531

用法二：接收 submit_log data JSON → 生成 confirm-edit-visit-log-card（供填日志确认，已废弃，保留兼容）
  python get_visit_plan_detail.py --mode confirm --data '{...submit_log data JSON...}'

用法三：修改日志入口 → 查询详情并生成 confirm-edit-visit-log-card（含 updatedData/originalData/modifyFields）
  python get_visit_plan_detail.py --mode edit --visit-plan-id 7082572911711531
  # 可选：用户说要修改拜访纪要为 "xxx" → 传 --visit-summary
  python get_visit_plan_detail.py --mode edit --visit-plan-id 7082572911711531 \\
      --visit-summary "新的拜访纪要" --modify-fields contacts visitSummary
"""

import argparse
import json
import sys
import os
import requests as _req

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_client import call_api, _get_pc_cookie

# visitObject 类型码 → 中文显示名映射
VISIT_OBJECT_LABEL_MAP = {
    "ad_customer": "广告客户",
    "store": "门店",
    "agent": "代理商",
    "provider": "服务商",
    "other": "其他",
}


def _fetch_ai_summary(visit_plan_id: str) -> str:
    """
    查询该拜访计划的 AI 录音纪要。

    流程：
      1. getLlmStatus → 仅 llmStatus=2 时继续
      2. getAudioRecordDetail → 返回 llmSummary

    任何异常 / 状态非 2 均返回空字符串，不影响主流程。
    """
    try:
        visit_plan_id_int = int(visit_plan_id)
    except (ValueError, TypeError):
        return ""

    cookie = _get_pc_cookie() or ""
    headers = {
        "Cookie": cookie,
        "Content-Type": "application/json",
        "Referer": "http://127.0.0.1:8080/visit/tools/visit",
        "Origin": "http://127.0.0.1:8080",
        "trace-context": '{"laneId":"PRT.visit"}',
    }

    # Step 1: 检查 llmStatus
    try:
        resp0 = _req.post(
            "http://127.0.0.1:8080/rest/apicenter/visit/app/visitAi/visitCenter/getLlmStatus",
            json={"visitPlanId": visit_plan_id_int},
            headers=headers,
            timeout=(5, 10),
        )
        if resp0.status_code == 200:
            body0 = resp0.json()
            br0 = body0.get("baseResponse") or {}
            code0 = br0.get("resultCode", br0.get("code", -1))
            if code0 == 0:
                llm_status = (body0.get("data") or {}).get("llmStatus", -1)
                if llm_status != 2:
                    return ""  # 未整合/整合中/失败，不带纪要
    except Exception as e:
        print(f"[INFO] 该拜访无 AI 纪要（getLlmStatus异常）: {e}", file=sys.stderr)
        return ""

    # Step 2: 获取纪要内容
    try:
        resp1 = _req.post(
            "http://127.0.0.1:8080/rest/apicenter/visit/app/visitAi/visitCenter/getAudioRecordDetail",
            json={"visitPlanId": visit_plan_id_int},
            headers=headers,
            timeout=(5, 15),
        )
        if resp1.status_code == 200:
            body1 = resp1.json()
            br1 = body1.get("baseResponse") or {}
            code1 = br1.get("resultCode", br1.get("code", -1))
            if code1 == 0:
                return (body1.get("data") or {}).get("llmSummary", "") or ""
    except Exception as e:
        print(f"[INFO] 该拜访无 AI 纪要（getAudioRecordDetail异常）: {e}", file=sys.stderr)

    return ""


def _build_log_card(detail: dict, contacts: list, visit_summary: str) -> str:
    """构建 visit-log-card 完整协议块（单行，无转义）。

    Args:
        detail: get_visit_plan_detail 返回的字段
        contacts: [{"id","name","phone","isKp"}]，id 必须非空字符串
        visit_summary: 拜访纪要
    """
    # 联系人：id 必须非空，缺失时用占位串，否则前端渲染失败
    norm_contacts = []
    for i, c in enumerate(contacts, start=1):
        cid = c.get("id")
        if not (isinstance(cid, str) and cid.strip()):
            cid = f"contact-{i:03d}"
        norm_contacts.append({
            "id": cid,
            "name": c.get("name", ""),
            "phone": c.get("phone") if c.get("phone") else None,
            "isKp": int(c.get("isKp", 0) or 0),
        })

    info = detail.get("visitObjectInfo", {}) or {}
    card = {
        "protocol": "visit.session.v1",
        "type": "a2ui.render",
        "block_id": "b002",
        "card": {
            "card_id": "c002",
            "card_name": "visit-log-card",
            "schema_version": "1.0",
            "data": {
                "visitId": detail.get("visitPlanId"),
                "visitObjectName": detail.get("visitObjectName", ""),
                "visitObject": detail.get("visitObject", ""),
                "visitObjectType": detail.get("visitObjectType", ""),
                "visitObjectInfo": {
                    "licenseName": info.get("licenseName") or detail.get("visitObjectName", ""),
                    "licenseCode": info.get("licenseCode") or "",
                    "agentId": info.get("agentId"),
                    "agentName": info.get("agentName"),
                    "storePoiId": info.get("storePoiId"),
                    "storePoiName": info.get("storePoiName"),
                    "providerId": info.get("providerId"),
                    "providerName": info.get("providerName"),
                },
                "planVisitTime": detail.get("planVisitTime"),
                "planVisitAddress": detail.get("planVisitAddress") or "",
                "checkinTime": detail.get("checkinTime"),
                "checkinAddress": detail.get("checkinAddress") or "",
                "checkinAbnormalInfo": None,
                "checkinAbnormalReason": None,
                "isCheckedOut": int(detail.get("isCheckedOut", 0) or 0),
                "checkoutTime": detail.get("checkoutTime"),
                "checkoutAddress": detail.get("checkoutAddress"),
                "photoUrl": detail.get("photoUrl"),
                "checkoutPhotoUrl": None,
                "supplementAllowed": int(detail.get("supplementAllowed", 0) or 0),
                "actualVisitTime": detail.get("actualVisitTime"),
                "contacts": {"contact": norm_contacts, "wechatContact": []},
                "visitSummary": visit_summary or "",
            },
            "action_states": {
                "submit_log": {"enabled": True, "visible": True},
                "visit_target_press": {"enabled": True, "visible": True},
            },
        },
    }
    body = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
    return f"<<<VISIT_BLOCK_V1>>>{body}<<<END_VISIT_BLOCK_V1>>>"


def _build_confirm_card(data: dict) -> str:
    """
    接收 submit_log / confirm_edit_visit_log 操作传入的 data JSON，
    构建 confirm-edit-visit-log-card 协议块（供用户二次确认/修改）。
    """
    contacts_raw = data.get("contacts", {}).get("contact", [])
    norm_contacts = []
    for i, c in enumerate(contacts_raw, start=1):
        cid = c.get("id")
        if not (isinstance(cid, str) and cid.strip()):
            cid = f"contact-{i:03d}"
        norm_contacts.append({
            "id": cid,
            "name": c.get("name", ""),
            "phone": c.get("phone") or None,
            "isKp": int(c.get("isKp", 0) or 0),
        })

    card = {
        "protocol": "visit.session.v1",
        "type": "a2ui.render",
        "block_id": "b003",
        "card": {
            "card_id": "c003",
            "card_name": "confirm-edit-visit-log-card",
            "schema_version": "1.0",
            "data": {
                "visitId": data.get("visitId"),
                "visitObjectName": data.get("visitObjectName", ""),
                "visitObject": data.get("visitObject", ""),
                "visitObjectType": data.get("visitObjectType", ""),
                "visitObjectInfo": data.get("visitObjectInfo") or {
                    "licenseName": data.get("visitObjectName", ""),
                    "licenseCode": "",
                    "agentId": None, "agentName": None,
                    "storePoiId": None, "storePoiName": None,
                    "providerId": None, "providerName": None,
                },
                "planVisitTime": data.get("planVisitTime"),
                "planVisitAddress": data.get("planVisitAddress", ""),
                "checkinTime": data.get("checkinTime"),
                "checkinAbnormalInfo": data.get("checkinAbnormalInfo"),
                "checkinAbnormalReason": data.get("checkinAbnormalReason"),
                "actualVisitTime": data.get("actualVisitTime"),
                "contacts": {"contact": norm_contacts, "wechatContact": []},
                "visitSummary": data.get("visitSummary") or "",
                "modifyFields": ["contacts", "visitSummary"],
            },
            "action_states": {
                "confirm_edit_log": {"enabled": True, "visible": True},
                "switch_log": {"enabled": True, "visible": True},
            },
        },
    }
    body = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
    return f"<<<VISIT_BLOCK_V1>>>{body}<<<END_VISIT_BLOCK_V1>>>"


def _build_edit_card(detail: dict, visit_summary: str = None,
                     modify_fields: list = None) -> str:
    """
    修改日志入口：查询详情后生成 confirm-edit-visit-log-card。

    - originalData: 详情接口返回的当前真实数据（联系人 + visitSummary）
    - updatedData:  同 originalData，但若用户明确说了要改纪要则把 visitSummary 替换
    - modifyFields: 由调用方决定（contacts / visitSummary / 两者都有）
    """
    info = detail.get("visitObjectInfo", {}) or {}

    # 原始电话联系人
    raw_contacts = detail.get("contacts", {}).get("contact", []) or []
    norm_contacts = []
    for i, c in enumerate(raw_contacts, start=1):
        cid = c.get("id")
        if not (isinstance(cid, str) and cid.strip()):
            cid = f"contact-{i:03d}"
        norm_contacts.append({
            "id": cid,
            "name": c.get("name", ""),
            "phone": c.get("phone") or None,
            "isKp": int(c.get("isKp", 0) or 0),
        })

    # 原始企微联系人（后端详情接口返回的 contacts.wechatContact 已带 wechatCorpId）
    raw_wechat_contacts = detail.get("contacts", {}).get("wechatContact", []) or []
    norm_wechat_contacts = []
    for wc in raw_wechat_contacts:
        norm_wechat_contacts.append({
            "wechatUserId":   wc.get("wechatUserId", "") or "",
            "wechatNickname": wc.get("wechatNickname", "") or "",
            "wechatAvatar":   wc.get("wechatAvatar", "") or "",
            "wechatRemark":   wc.get("wechatRemark", "") or "",
            "wechatCorpId":   wc.get("wechatCorpId", "") or "",
            "isKp":           int(wc.get("isKp", 0) or 0),
        })

    # 原始纪要（get_visit_plan_detail 已提到顶层，兼容兜底 visitSummaryInfo）
    original_summary = detail.get("visitSummary") or None
    if not original_summary:
        original_summary = (detail.get("visitSummaryInfo") or {}).get("visitSummary") or None

    # updatedData.visitSummary：用户明确指定则用用户值，否则留 null
    updated_summary = visit_summary if visit_summary is not None else None

    # modifyFields 默认两者都带，方便前端展示全部可修改项
    if not modify_fields:
        modify_fields = ["contacts", "visitSummary"]

    card = {
        "protocol": "visit.session.v1",
        "type": "a2ui.render",
        "block_id": "b008",
        "card": {
            "card_id": "c008",
            "card_name": "confirm-edit-visit-log-card",
            "schema_version": "1.0",
            "data": {
                "visitId": detail.get("visitPlanId"),
                "visitObjectName": detail.get("visitObjectName", ""),
                "visitObject": detail.get("visitObject", ""),
                "visitObjectType": detail.get("visitObjectType", ""),
                "visitObjectInfo": {
                    "licenseName": info.get("licenseName") or detail.get("visitObjectName", ""),
                    "licenseCode": info.get("licenseCode") or "",
                    "agentId": info.get("agentId"),
                    "agentName": info.get("agentName"),
                    "storePoiId": info.get("storePoiId"),
                    "storePoiName": info.get("storePoiName"),
                    "providerId": info.get("providerId"),
                    "providerName": info.get("providerName"),
                },
                "planVisitTime": detail.get("planVisitTime"),
                "planVisitAddress": detail.get("planVisitAddress") or "",
                "checkinTime": detail.get("checkinTime"),
                "checkinAbnormalInfo": None,
                "checkinAbnormalReason": None,
                "actualVisitTime": detail.get("actualVisitTime"),
                # updatedData：用户本次打算改成的内容
                "updatedData": {
                    "contacts": {"contact": norm_contacts, "wechatContact": norm_wechat_contacts},
                    "visitSummary": updated_summary,
                },
                # originalData：当前已有的真实数据
                "originalData": {
                    "contacts": {"contact": norm_contacts, "wechatContact": norm_wechat_contacts},
                    "visitSummary": original_summary,
                },
                "modifyFields": modify_fields,
            },
            "action_states": {
                "confirm_edit_log": {"enabled": True, "visible": True},
                "switch_log": {"enabled": True, "visible": True},
            },
        },
    }
    body = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
    return f"<<<VISIT_BLOCK_V1>>>{body}<<<END_VISIT_BLOCK_V1>>>"


def get_visit_plan_detail(visit_plan_id: str) -> dict:
    """
    查询拜访计划详情，返回填写日志卡片所需全部字段
    """
    payload = {"id": visit_plan_id}
    data = call_api("queryVisitPlanDetails", payload, step="get_detail")

    checkin_info = data.get("visitCheckinInfo", {})
    basic_info = data.get("basicInfo", {})
    title_info = data.get("titleInfo", {})

    # ===== 客户信息 =====
    visit_object_type = basic_info.get("visitObject", "")  # "ad_customer"
    visit_object_label = VISIT_OBJECT_LABEL_MAP.get(visit_object_type, visit_object_type)
    license_name = basic_info.get("businessLicense", "") or basic_info.get("storeName", "") or data.get("agentName", "") or data.get("providerName", "") or title_info.get("storePoiName", "") or title_info.get("visitObjectName", "")
    license_code = basic_info.get("businessLicenseCode", "")

    # ===== 签到/签退信息 =====
    checkin_addr_obj = checkin_info.get("checkinAddress", {})
    checkin_address = checkin_addr_obj.get("address", "")

    plan_addr_obj = checkin_info.get("planVisitAddress", {})
    plan_address = plan_addr_obj.get("address", "")

    checkout_addr_obj = checkin_info.get("checkoutAddress", {})
    checkout_address = checkout_addr_obj.get("address", "") or None

    # checkinTime/actualVisitTime: -1 表示未签到，统一转为 null
    checkin_time_raw = checkin_info.get("checkinTime")
    checkin_time = None if (checkin_time_raw is None or checkin_time_raw == -1) else checkin_time_raw

    actual_visit_time_raw = checkin_info.get("actualVisitTime")
    actual_visit_time = None if (actual_visit_time_raw is None or actual_visit_time_raw == -1) else actual_visit_time_raw

    plan_visit_time = checkin_info.get("planVisitTime")

    # checkoutTime: -1 表示未签退，统一转为 null
    checkout_time_raw = checkin_info.get("checkoutTime", -1)
    checkout_time = None if (checkout_time_raw is None or checkout_time_raw == -1) else checkout_time_raw
    is_checked_out = 1 if (checkout_time is not None) else 0

    # 签到照片
    photo_url = checkin_info.get("checkinPhotoUrl") or None
    if photo_url == "":
        photo_url = None

    # supplementAllowed: 调用专用接口判断，失败则降级为 0（不影响主流程）
    visit_id = title_info.get("visitId") or visit_plan_id
    supplement_allowed = 0
    try:
        sup_resp = call_api(
            "checkVisitSupplementAllowed",
            {"visitId": visit_id},
            step="check_supplement",
        )
        supplement_allowed = 1 if sup_resp.get("allowed") else 0
    except Exception:
        pass  # 接口异常，保持 supplementAllowed=0，不影响主流程

    return {
        "visitPlanId": visit_id,
        # 客户信息
        "visitObjectName": license_name,
        "visitObject": visit_object_label,        # 显示名，如 "广告客户"
        "visitObjectType": visit_object_type,     # 类型码，如 "ad_customer"
        "visitObjectInfo": {
            "licenseName": license_name,
            "licenseCode": license_code,
            "agentId": data.get("agentId") or None,
            "agentName": data.get("agentName") or None,
            "storePoiId": checkin_info.get("storePoiId") or None,
            "storePoiName": data.get("storePoiName") or title_info.get("storePoiName") or None,
            "providerId": data.get("providerId") or None,
            "providerName": data.get("providerName") or None,
        },
        # 时间
        "planVisitTime": plan_visit_time,
        "actualVisitTime": actual_visit_time,
        "checkinTime": checkin_time,
        "checkoutTime": checkout_time,
        # 地址
        "planVisitAddress": plan_address,
        "checkinAddress": checkin_address,
        "checkoutAddress": checkout_address,
        # 状态
        "isCheckedOut": is_checked_out,
        "photoUrl": photo_url,
        "supplementAllowed": supplement_allowed,
        # 联系人（初始为空，由用户填写）
        "contacts": basic_info.get("contacts", {"contact": [], "wechatContact": []}),
        # 拜访纪要（后端放在 visitSummaryInfo 子对象里，这里提到顶层方便复用）
        "visitSummary": (data.get("visitSummaryInfo") or {}).get("visitSummary") or "",
    }


def main():
    parser = argparse.ArgumentParser(description="查询拜访详情/生成卡片")
    parser.add_argument("--visit-plan-id", help="拜访计划 ID（detail/edit 模式必填）")
    parser.add_argument(
        "--mode", choices=["detail", "confirm", "edit"], default="detail",
        help="detail=查询详情并生成 visit-log-card（默认）；confirm=根据 --data 生成 confirm-edit-visit-log-card；edit=修改日志入口，查询详情后生成 confirm-edit-visit-log-card",
    )
    parser.add_argument("--data", help="confirm 模式：submit_log 操作的 data JSON 字符串")
    parser.add_argument("--visit-summary", default=None,
                        help="edit 模式：用户明确要修改的拜访纪要内容（可选）")
    parser.add_argument("--modify-fields", nargs="+", default=None,
                        help="edit 模式：本次要修改的字段列表，如 contacts visitSummary（默认两者都带）")
    args = parser.parse_args()

    # ===== edit 模式：修改日志入口，查询详情 → 生成 confirm-edit-visit-log-card =====
    if args.mode == "edit":
        if not args.visit_plan_id:
            print("❌ edit 模式需要 --visit-plan-id 参数", file=sys.stderr)
            sys.exit(1)
        result = get_visit_plan_detail(visit_plan_id=args.visit_plan_id)

        # 兜底：联系人 id 非空
        raw_contacts = result.get("contacts", {}).get("contact", [])
        for i, c in enumerate(raw_contacts):
            if not (isinstance(c.get("id"), str) and c["id"].strip()):
                c["id"] = f"contact-{i + 1:03d}"

        # 把已查到的联系人写回 detail
        result.setdefault("contacts", {})["contact"] = raw_contacts

        card_line = _build_edit_card(
            detail=result,
            visit_summary=args.visit_summary,
            modify_fields=args.modify_fields,
        )
        print("\n===== SUGGESTED_CARD (整条回复请原样复制下面这一行) =====")
        print(card_line)
        print(
            "⛔ 把上面 SUGGESTED_CARD 那一行原样输出，"
            "块外零字符，禁止加 \\\" 转义/换行/Markdown。",
            file=sys.stderr,
        )
        return

    # ===== confirm 模式：生成 confirm-edit-visit-log-card =====
    if args.mode == "confirm":
        if not args.data:
            print("❌ confirm 模式需要 --data 参数", file=sys.stderr)
            sys.exit(1)
        try:
            data = json.loads(args.data)
        except json.JSONDecodeError as e:
            print(f"❌ --data 不是合法 JSON: {e}", file=sys.stderr)
            sys.exit(1)
        card_line = _build_confirm_card(data)
        # ⚠️ 裸输出，绝不能塞进 json.dumps，否则双引号被转义成 \"
        print("\n===== SUGGESTED_CARD (整条回复请原样复制下面这一行) =====")
        print(card_line)
        print(
            "⛔ 把上面 SUGGESTED_CARD 那一行紧跟在 action_result 后原样输出，"
            "块外零字符，禁止加 \\\" 转义/换行/Markdown。",
            file=sys.stderr,
        )
        return

    # ===== detail 模式（默认）：查询详情 → 直接生成 visit-log-card =====
    if not args.visit_plan_id:
        print("❌ detail 模式需要 --visit-plan-id 参数", file=sys.stderr)
        sys.exit(1)

    result = get_visit_plan_detail(visit_plan_id=args.visit_plan_id)

    # 尝试获取 AI 录音纪要（失败不影响主流程）
    ai_summary = _fetch_ai_summary(args.visit_plan_id)

    # 直接生成 visit-log-card：
    # - 联系人一律为空，联系人只允许用户在卡片上选择（不从 API 或用户语言注入）
    # - visitSummary 用 AI 纪要（无则留空让用户填）
    # ⚠️ 协议块裸输出，绝不能塞进 json.dumps，否则双引号被转义成 \"
    suggested = _build_log_card(
        detail=result,
        contacts=[],
        visit_summary=ai_summary,
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("\n===== SUGGESTED_CARD (整条回复请原样复制下面这一行) =====")
    print(suggested)
    print(
        "⛔ 检测到可出卡状态：把上面 SUGGESTED_CARD 那一行原样作为整条回复输出，"
        "块外零字符，禁止加 \\\" 转义/换行/Markdown/再调用 fill_log.py。",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
