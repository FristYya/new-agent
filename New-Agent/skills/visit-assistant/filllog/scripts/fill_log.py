"""
填写拜访日志

接口：fillVisitLog（PC 端 visit-service）
路径：POST http://127.0.0.1:8080/rest/apicenter/visit/web/visit/new/work/fillVisitLog
      ⚠️ 注意：此接口路径前缀与其他接口不同！

请求体非常复杂，需要从两个来源组装：
  1. 拜访计划详情（queryVisitPlanDetails）：visitObjectInfo, checkin/checkout 地址/时间, visitType 等
  2. 用户输入：联系人（电话 + 企微）、拜访纪要

必填字段（☆为必须）：
  ☆ 拜访纪要 (visitSummary)
  ☆ 至少一个联系人（电话联系人或企微联系人）
  电话联系人：姓名、是否KP、联系方式、id（无 id 会自动创建）
  企微联系人：wechatUserId、wechatNickname、wechatAvatar、wechatRemark、wechatCorpId、是否KP
    （企微联系人不需要 id，后端会快照到 locallife_visit_visit_contact_log，不污染主表）

用法：
  # 最简单方式：传入 visitId + 必要字段
  python fill_log.py \
    --visit-id 7035097242904834 \
    --contact-name "张三" \
    --contact-is-kp 1 \
    --contact-phone "198****9628" \
    --contact-id "2072215032601649153" \
    --visit-summary "今天拜访了客户，讨论了合作方案..."

  # 多个电话联系人
  python fill_log.py \
    --visit-id 7035097242904834 \
    --contact-name "张三" --contact-is-kp 1 --contact-phone "198****9628" --contact-id "111" \
    --contact-name "李四" --contact-is-kp 0 --contact-phone "139****5678" --contact-id "222" \
    --visit-summary "拜访纪要内容..."

  # 企微联系人（不调用 addOrUpdateContact，直接快照到 contact_log）
  python fill_log.py \
    --visit-id 7035097242904834 \
    --wechat-user-id "wmQ2iWRwAAd31tVeWF8_bs_eSU74EIRA" \
    --wechat-nickname "Patrick Lin" \
    --wechat-avatar "http://wx.qlogo.cn/xxx" \
    --wechat-remark "Patrick Lin" \
    --wechat-corp-id local_life \
    --wechat-is-kp 1 \
    --visit-summary "拜访纪要内容..."

  # 电话联系人 + 企微联系人混合
  python fill_log.py \
    --visit-id 7035097242904834 \
    --contact-name "张三" --contact-is-kp 1 --contact-phone "198****9628" --contact-id "111" \
    --wechat-user-id "wmQ2iWRwAAd31tVeWF8_bs_eSU74EIRA" --wechat-nickname "Patrick Lin" \
    --wechat-avatar "http://wx.qlogo.cn/xxx" --wechat-remark "Patrick Lin" \
    --wechat-corp-id local_life --wechat-is-kp 1 \
    --visit-summary "拜访纪要内容..."

输出 JSON：
{
  "success": true,
  "visitId": "7035097242904834"
}
"""

import argparse
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_client import call_api


def query_visit_detail(visit_id: str) -> dict:
    """
    查询拜访计划详情，获取 fillVisitLog 需要的预填字段

    Args:
        visit_id: 拜访计划 ID

    Returns:
        VisitPlanInfoPb data 字段
    """
    payload = {"id": visit_id}
    return call_api("queryVisitPlanDetails", payload, step="query_detail")


def build_fill_log_payload(visit_id: str, visit_summary: str,
                           contacts: list, detail: dict,
                           wechat_contacts: list = None) -> dict:
    """
    构建 fillVisitLog 请求体

    Args:
        visit_id: 拜访计划 ID
        visit_summary: 拜访纪要
        contacts: 电话联系人列表 [{id, name, phone, isKp}]
        detail: 拜访计划详情（从 queryVisitPlanDetails 获取）
        wechat_contacts: 企微联系人列表 [{wechatUserId, wechatNickname, wechatAvatar,
                            wechatRemark, wechatCorpId, isKp}]，对齐后端 WechatContactInfo

    Returns:
        完整的 fillVisitLog 请求体
    """
    # ─── 从详情提取基础信息 ───
    basic_info = detail.get("basicInfo", {})
    checkin_info = detail.get("visitCheckinInfo", {})
    summary_info = detail.get("visitSummaryInfo", {})

    # visitObjectInfo：从详情的多个字段组装
    visit_object_info = {
        "isNewCustomer": _safe_int(basic_info.get("isNewCustomer", "0")),
        "isTargetCustomer": _safe_int(basic_info.get("isTargetNewCustomer", "否") == "是"),
        "targetCustomerId": detail.get("targetCustomerId", "") or basic_info.get("targetCustomerId", ""),
        "targetCustomerName": detail.get("targetCustomerName", "") or basic_info.get("targetCustomerName", ""),
        "licenseName": basic_info.get("businessLicense", ""),
        "licenseCode": basic_info.get("businessLicenseCode", ""),
        "platformId": (basic_info.get("platformAccount") or {}).get("platformId", ""),
        "platformNickname": (basic_info.get("platformAccount") or {}).get("platformName", ""),  # 注意字段名是 platformName 不是 platformNickname
        "agentId": detail.get("agentId", ""),
        "agentName": detail.get("agentName", ""),
        "storePoiId": checkin_info.get("storePoiId", ""),
        "storePoiName": basic_info.get("storeName", ""),
        "storePoiAddress": detail.get("storePoiAddress", ""),
        "storeCustomerId": detail.get("storeCustomerId", ""),
        "storeCustomerName": detail.get("storeCustomerName", ""),
        "storeCustomerAvatar": detail.get("storeCustomerAvatar", ""),
        "storeCustomerCity": detail.get("storeCustomerCity", ""),
        "providerId": detail.get("providerId", ""),
        "providerName": detail.get("providerName", ""),
        "isLicenseSelfCreated": detail.get("isLicenseSelfCreated", "0"),
    }

    # 签到/签退地址
    checkin_address = _extract_address(checkin_info.get("checkinAddress"))
    checkout_address = _extract_address(checkin_info.get("checkoutAddress"))
    plan_visit_address = _extract_address(checkin_info.get("planVisitAddress"))

    # 构建完整请求体
    wechat_contacts = wechat_contacts or []
    payload = {
        "visitId": visit_id,
        "visitType": basic_info.get("visitType", "local"),
        "visitObjectType": basic_info.get("visitObject", "ad_customer"),
        "visitPurpose": basic_info.get("visitPurpose", "other"),
        "planVisitAddress": plan_visit_address.get("address", "") if isinstance(plan_visit_address, dict) else plan_visit_address,
        "planVisitTime": checkin_info.get("planVisitTime", 0),
        "actualVisitTime": checkin_info.get("actualVisitTime", 0),
        "checkinAddress": checkin_address.get("address", "") if isinstance(checkin_address, dict) else checkin_address,
        "checkoutAddress": checkout_address.get("address", "") if isinstance(checkout_address, dict) else checkout_address,
        "checkinTime": checkin_info.get("checkinTime", 0),
        "checkoutTime": checkin_info.get("checkoutTime", 0),
        "planCheckinDistanceKm": (checkin_info.get("distanceInfo") or {}).get("planCheckinDistanceKm", 0),
        "visitSummary": visit_summary,
        "visitObjectInfo": visit_object_info,
        "accompanyUserIds": [],  # 不需要传，后端会保留原值
        "contacts": {
            "contact": contacts,          # 电话联系人 [{id, name, phone, isKp}]
            "wechatContact": wechat_contacts,  # 企微联系人，对齐后端 WechatContactInfo
        },
        "photoUrl": checkin_info.get("checkinPhotoUrl", ""),
        "onepointMeetingId": "",
        "isAutoGeneratedSummary": 0,
    }

    # visitType=travel 时必须带上差旅申请单号
    visit_type = basic_info.get("visitType", "local")
    if visit_type == "travel":
        travel_info = detail.get("travelInfo", {}) or {}
        travel_apply_no = travel_info.get("travelApplyNo", "")
        if travel_apply_no:
            payload["travelApplyNo"] = travel_apply_no

    # 清理空值字段（保留必须字段）
    return payload


def fill_visit_log(visit_id: str, visit_summary: str,
                   contacts: list, use_draft: bool = True,
                   wechat_contacts: list = None) -> dict:
    """
    填写拜访日志（主入口）

    流程：
    1. 查询拜访详情 → 获取 visitObjectInfo、签到/签退信息等
    2. 如果电话联系人没有 id，先调用 addOrUpdateContact 创建 → 拿到带 id 的联系人
       企微联系人不需要创建，后端会快照到 visit_contact_log
    3. 如果有草稿，合并草稿内容
    4. 构建完整请求体
    5. 调用 fillVisitLog

    Args:
        visit_id: 拜访计划 ID
        visit_summary: 拜访纪要
        contacts: 电话联系人列表 [{id, name, phone, isKp}]
                  id 为空时会自动调用 addOrUpdateContact 创建
        use_draft: 是否合并草稿内容
        wechat_contacts: 企微联系人列表 [{wechatUserId, wechatNickname, wechatAvatar,
                            wechatRemark, wechatCorpId, isKp}]

    Returns:
        {"success": bool, "visitId": str}
    """
    wechat_contacts = wechat_contacts or []

    # 1. 查询拜访详情
    detail = query_visit_detail(visit_id)

    # 2. 确保电话联系人都有 id（后端校验联系人必须存在）
    #    企微联系人不需要此步骤，后端 contactHandle V2 会直接快照到 visit_contact_log
    resolved_contacts = _resolve_contacts(contacts, detail)

    # 3. 可选：合并草稿
    draft_data = None
    if use_draft:
        try:
            from get_draft import get_draft
            draft_result = get_draft(visit_id)
            if draft_result.get("hasDraft"):
                draft_data = draft_result.get("draft")
        except Exception:
            pass  # 草稿查询失败不影响主流程

    # 4. 构建请求体
    payload = build_fill_log_payload(visit_id, visit_summary, resolved_contacts, detail,
                                     wechat_contacts=wechat_contacts)

    # 5. 如果有草稿，用草稿内容覆盖部分字段（用户可能之前已填写部分内容）
    if draft_data:
        if draft_data.get("visitSummary") and not visit_summary:
            payload["visitSummary"] = draft_data["visitSummary"]
        if draft_data.get("selectedContacts"):
            # 草稿中联系人格式可能是 [{id, name, phone, isKp}]
            if not contacts:
                payload["contacts"]["contact"] = draft_data["selectedContacts"]
        if draft_data.get("visitPurpose"):
            payload["visitPurpose"] = draft_data["visitPurpose"]

    # 6. 调用 fillVisitLog
    data = call_api("fillVisitLog", payload, step="fill_log")

    return {
        "success": data.get("success", False),
        "visitId": visit_id,
        "_detail": detail,
        "_contacts": resolved_contacts,
        "_wechat_contacts": wechat_contacts,
        "_visitSummary": payload.get("visitSummary", visit_summary),
    }


def _fmt_time_label(val) -> str:
    """时间戳(毫秒/秒) → 'YYYY-MM-DD HH:mm'。无效返回 ''。"""
    if val is None or val == "" or val == -1 or val == 0:
        return ""
    s = str(val)
    if "-" in s and ":" in s:
        return s[:16]
    if s.lstrip("-").isdigit():
        try:
            ts = int(s)
            if ts <= 0:
                return ""
            if ts > 1000000000000:
                ts //= 1000
            from datetime import datetime
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError):
            return ""
    return s


def _build_action_result(
    visit_id: str,
    detail: dict,
    contacts: list,
    visit_summary: str,
    action_id: str = "submit_log",
    client_action_id: str = "",
    card_id: str = "auto_visit-log-card_0",
    card_name: str = "visit-log-card",
    wechat_contacts: list = None,
) -> str:
    """
    构建 a2ui.action_result 协议块（单行，无转义）。
    前端收到后会将原 visit-log-card 的提交按钮置灰。
    """
    wechat_contacts = wechat_contacts or []
    basic_info   = detail.get("basicInfo", {}) or {}
    checkin_info = detail.get("visitCheckinInfo", {}) or {}
    title_info   = detail.get("titleInfo", {}) or {}

    customer_name    = basic_info.get("businessLicense", "") or basic_info.get("storeName", "") or detail.get("agentName", "") or detail.get("providerName", "") or title_info.get("storePoiName", "") or title_info.get("visitObjectName", "")
    visit_object_type = basic_info.get("visitObject", "")
    visit_object_label = {"ad_customer": "广告客户", "store": "门店", "agent": "代理商",
                          "provider": "服务商", "other": "其他"}.get(visit_object_type, visit_object_type)

    plan_addr    = _extract_address(checkin_info.get("planVisitAddress"))
    checkin_addr = _extract_address(checkin_info.get("checkinAddress"))
    checkout_addr = _extract_address(checkin_info.get("checkoutAddress"))

    contacts_clean = [
        {
            "id":    c.get("id", ""),
            "name":  c.get("name", ""),
            "phone": c.get("phone", ""),
            "isKp":  int(c.get("isKp", 0) or 0),
        }
        for c in contacts
    ]
    wechat_contacts_clean = [
        {
            "wechatUserId":   wc.get("wechatUserId", ""),
            "wechatNickname": wc.get("wechatNickname", ""),
            "wechatAvatar":   wc.get("wechatAvatar", ""),
            "wechatRemark":   wc.get("wechatRemark", ""),
            "wechatCorpId":   wc.get("wechatCorpId", ""),
            "isKp":           int(wc.get("isKp", 0) or 0),
        }
        for wc in wechat_contacts
    ]

    skill_data = {
        "visitId":          str(visit_id),
        "visitObjectName":  customer_name,
        "visitObject":      visit_object_label,
        "visitObjectType":  visit_object_type,
        "visitObjectInfo": {
            "licenseName": customer_name,
            "licenseCode": basic_info.get("businessLicenseCode", ""),
            "agentId":    detail.get("agentId") or None,
            "agentName":  detail.get("agentName") or None,
            "storePoiId": checkin_info.get("storePoiId") or None,
            "storePoiName": detail.get("storePoiName") or None,
            "providerId": detail.get("providerId") or None,
            "providerName": detail.get("providerName") or None,
        },
        "planVisitTime":    checkin_info.get("planVisitTime"),
        "planVisitAddress": plan_addr or "",
        "checkinTime":      checkin_info.get("checkinTime"),
        "checkinAddress":   checkin_addr or "",
        "checkinAbnormalInfo":   None,
        "checkinAbnormalReason": None,
        "isCheckedOut":     1 if checkin_info.get("checkoutTime") else 0,
        "checkoutTime":     checkin_info.get("checkoutTime") or None,
        "checkoutAddress":  checkout_addr or None,
        "photoUrl":         checkin_info.get("checkinPhotoUrl") or None,
        "checkoutPhotoUrl": None,
        "supplementAllowed": 0,
        "actualVisitTime":  checkin_info.get("actualVisitTime"),
        "needSupplementary": 0,
        "contacts": {"contact": contacts_clean, "wechatContact": wechat_contacts_clean},
        "visitSummary": visit_summary or "",
    }

    result = {
        "action_id":        action_id,
        "client_action_id": client_action_id,
        "protocol":         "visit.session.v1",
        "skill_name":       "submit_log",
        "skill_result": {
            "success": True,
            "data":    skill_data,
        },
        "target": {
            "card_id":   card_id,
            "card_name": card_name,
        },
        "type": "a2ui.action_result",
    }
    body = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    return f"<<<VISIT_BLOCK_V1>>>{body}<<<END_VISIT_BLOCK_V1>>>"


def _build_completed_card(visit_id: str, detail: dict,
                          contacts: list, visit_summary: str,
                          wechat_contacts: list = None) -> str:
    """构建 visit-log-completed-card 完整协议块（单行，无转义）。"""
    wechat_contacts = wechat_contacts or []
    basic_info = detail.get("basicInfo", {}) or {}
    checkin_info = detail.get("visitCheckinInfo", {}) or {}
    title_info = detail.get("titleInfo", {}) or {}

    customer_name = basic_info.get("businessLicense", "") or basic_info.get("storeName", "") or detail.get("agentName", "") or detail.get("providerName", "") or title_info.get("storePoiName", "") or title_info.get("visitObjectName", "")
    plan_addr = _extract_address(checkin_info.get("planVisitAddress"))
    checkin_addr = _extract_address(checkin_info.get("checkinAddress"))
    checkout_addr = _extract_address(checkin_info.get("checkoutAddress"))

    # 联系人标签：电话联系人「姓名 手机号（KP）」+ 企微联系人「微信昵称（KP）」
    labels = []
    for c in contacts:
        seg = c.get("name", "")
        if c.get("phone"):
            seg += f" {c['phone']}"
        if int(c.get("isKp", 0) or 0) == 1:
            seg += "（KP）"
        if seg:
            labels.append(seg)
    for wc in wechat_contacts:
        seg = wc.get("wechatNickname", "") or wc.get("wechatRemark", "")
        if int(wc.get("isKp", 0) or 0) == 1:
            seg += "（KP）"
        if seg:
            labels.append(seg)
    contact_label = "、".join(labels)

    card = {
        "protocol": "visit.session.v1",
        "type": "a2ui.render",
        "block_id": "b004",
        "card": {
            "card_id": "c004",
            "card_name": "visit-log-completed-card",
            "schema_version": "1.0",
            "data": {
                "visitPlanId": str(visit_id),
                "navigationTarget": {
                    "kind": "krn_scheme",
                    "url": f"locallifevisit://krn?bundleId=LocalLifeVisit&componentName=VisitDetailV2&themeStyle=3&visitPlanId={visit_id}",
                },
                "pcTargetUrl": f"/visit/tools/visit?visitId={visit_id}&action=detail",
                "pcViewEditUrl": f"/visit/tools/visit?visitId={visit_id}&action=edit",
                "customerName": customer_name,
                "planVisitAddress": plan_addr or "",
                "checkinTimeLabel": _fmt_time_label(checkin_info.get("checkinTime")),
                "checkinAddress": checkin_addr or "",
                "checkoutTimeLabel": _fmt_time_label(checkin_info.get("checkoutTime")),
                "checkoutAddress": checkout_addr or "",
                "contactLabel": contact_label,
                "visitSummary": visit_summary or "",
                "actions": [
                    {"label": "修改拜访日志", "value": "edit-visit-log"},
                    {"label": "创建拜访计划", "value": "create-visit-plan"},
                ],
            },
            "action_states": {
                "follow_up_action": {"enabled": True, "visible": True},
                "edit_plan":        {"enabled": True, "visible": True},
                "view_detail":      {"enabled": True, "visible": True},
            },
        },
    }
    body = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
    return f"<<<VISIT_BLOCK_V1>>>{body}<<<END_VISIT_BLOCK_V1>>>"


def _resolve_contacts(contacts: list, detail: dict) -> list:
    """
    确保联系人都有 id。

    后端 fillVisitLog 校验联系人必须存在于 VisitContactPO 表。
    如果联系人没有 id（或 id 为空），自动调用 addOrUpdateContact 创建，
    然后从 queryContacts 拿到带 id 的记录。

    Args:
        contacts: 原始联系人列表 [{id, name, phone, isKp}]
        detail: 拜访详情

    Returns:
        带 id 的联系人列表
    """
    if not contacts:
        return contacts

    resolved = []
    needs_create = False
    for c in contacts:
        if c.get("id"):
            resolved.append(c)
        else:
            needs_create = True

    if not needs_create:
        return contacts

    # 有联系人缺少 id，需要先创建
    from add_contact import add_contact_from_detail

    for c in contacts:
        if c.get("id"):
            resolved.append(c)
        else:
            # 调用 addOrUpdateContact 创建联系人
            result = add_contact_from_detail(
                detail=detail,
                contact_name=c.get("name", ""),
                contact_phone=c.get("phone", ""),
                contact_is_kp=c.get("isKp", 0),
            )
            if result.get("success") and result.get("contact"):
                resolved.append(result["contact"])
            else:
                msg = result.get("message", "未知错误")
                print(f"[ERROR] 联系人 '{c.get('name')}' 创建失败: {msg}", file=sys.stderr)
                sys.exit(3)

    return resolved


# ============================================================
# 辅助函数
# ============================================================

def _safe_int(val, default=0):
    """安全转为 int"""
    try:
        if isinstance(val, bool):
            return 1 if val else 0
        if isinstance(val, str):
            if val in ("是", "yes", "true", "True"):
                return 1
            if val in ("否", "no", "false", "False"):
                return 0
            return int(val)
        return int(val)
    except (ValueError, TypeError):
        return default


def _extract_address(addr) -> str:
    """
    从地址字段提取纯文本地址

    地址可能是：
    - 字符串："北京市朝阳区..."
    - dict: {"address": "北京市朝阳区...", "longitude": 116.4, "latitude": 39.9}
    """
    if addr is None:
        return ""
    if isinstance(addr, dict):
        return addr.get("address", "")
    if isinstance(addr, str):
        try:
            obj = json.loads(addr)
            if isinstance(obj, dict) and "address" in obj:
                return obj["address"]
        except (json.JSONDecodeError, TypeError):
            pass
        return addr
    return str(addr)


def _parse_contacts(args) -> list:
    """
    从命令行参数解析联系人列表

    --contact-name, --contact-is-kp, --contact-phone, --contact-id 可重复
    """
    names = args.contact_name or []
    is_kps = args.contact_is_kp or []
    phones = args.contact_phone or []
    ids = args.contact_id or []

    contacts = []
    for i, name in enumerate(names):
        contact = {
            "name": name,
            "isKp": int(is_kps[i]) if i < len(is_kps) else 0,
            "phone": phones[i] if i < len(phones) else "",
            "id": ids[i] if i < len(ids) else "",
        }
        contacts.append(contact)

    return contacts


def _parse_wechat_contacts(args) -> list:
    """
    从命令行参数解析企微联系人列表

    --wechat-user-id, --wechat-nickname, --wechat-avatar, --wechat-remark,
    --wechat-corp-id, --wechat-is-kp 可重复

    返回结构对齐后端 WechatContactInfo protobuf：
        [{wechatUserId, wechatNickname, wechatAvatar, wechatRemark, wechatCorpId, isKp}]
    """
    user_ids = args.wechat_user_id or []
    nicknames = args.wechat_nickname or []
    avatars = args.wechat_avatar or []
    remarks = args.wechat_remark or []
    corp_ids = args.wechat_corp_id or []
    is_kps = args.wechat_is_kp or []

    wechat_contacts = []
    for i, wechat_user_id in enumerate(user_ids):
        corp_id = corp_ids[i] if i < len(corp_ids) else ""
        wechat_contacts.append({
            "wechatUserId": wechat_user_id,
            "wechatNickname": nicknames[i] if i < len(nicknames) else "",
            "wechatAvatar": avatars[i] if i < len(avatars) else "",
            "wechatRemark": remarks[i] if i < len(remarks) else "",
            "wechatCorpId": corp_id,
            "isKp": int(is_kps[i]) if i < len(is_kps) else 0,
        })

    return wechat_contacts


def submit_later_checkin(visit_id: str) -> bool:
    """
    补录签退接口。失败时返回 False，不抛异常（不影响主填日志流程）。
    """
    try:
        data = call_api(
            "submitLaterCheckin",
            {"visitId": visit_id, "checkOutType": 2},
            step="submit_later_checkin",
        )
        return bool(data.get("success", True))  # 接口无明确 success 字段时认为成功
    except Exception as e:
        print(f"[WARN] 补录签退失败（不影响主流程）: {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(description="填写/修改拜访日志")
    parser.add_argument(
        "--mode", choices=["fill", "edit"], default="fill",
        help="fill=填写日志（默认，成功后输出 visit-log-completed-card）；edit=修改日志（confirm-edit-visit-log-card 回传，成功后仅输出 JSON，无卡片）",
    )
    parser.add_argument("--visit-id", required=True, help="拜访计划 ID")
    parser.add_argument("--visit-summary", default="", help="拜访纪要")
    # 联系人参数（可重复）
    parser.add_argument("--contact-name", action="append", help="联系人姓名（可多次指定）")
    parser.add_argument("--contact-is-kp", action="append", type=int, help="是否KP: 1=是 0=否")
    parser.add_argument("--contact-phone", action="append", help="联系方式")
    parser.add_argument("--contact-id", action="append", help="联系人ID")
    # 企微联系人参数（可重复，与 --contact-* 相互独立）
    parser.add_argument("--wechat-user-id", action="append", help="企微联系人UserId（可多次指定）")
    parser.add_argument("--wechat-nickname", action="append", help="企微微信昵称")
    parser.add_argument("--wechat-avatar", action="append", help="企微头像URL")
    parser.add_argument("--wechat-remark", action="append", help="企微备注名")
    parser.add_argument("--wechat-corp-id", action="append", help="企微CorpId（local_life/commercial）")

    parser.add_argument("--wechat-is-kp", action="append", type=int, help="企微联系人是否KP: 1=是 0=否")
    parser.add_argument(
        "--need-supplementary", type=int, default=0,
        help="1=先调补录签退接口再填日志；0=直接填日志（默认）",
    )
    # fill 模式：前端透传的 action_id/client_action_id/card_id/card_name（用于 action_result 置灰原卡片）
    parser.add_argument("--action-id", default="submit_log", help="前端回传的 action_id，原样返回")
    parser.add_argument("--client-action-id", default="", help="前端回传的 client_action_id，原样返回")
    parser.add_argument("--card-id", default="auto_visit-log-card_0", help="前端回传的 card_id，原样返回")
    parser.add_argument("--card-name", default="visit-log-card", help="前端回传的 card_name，原样返回")
    # edit 模式专用：前端透传的 card data JSON（含 updatedData / modifyFields 等）
    parser.add_argument(
        "--card-data", default=None,
        help="edit 模式：confirm-edit-visit-log-card 回传的完整 data JSON 字符串",
    )
    args = parser.parse_args()

    # ── edit 模式：从 --card-data 提取联系人和纪要 ──────────────────────────
    if args.mode == "edit":
        if not args.card_data:
            print("[ERROR] edit 模式需要 --card-data 参数", file=sys.stderr)
            sys.exit(1)
        try:
            card_data = json.loads(args.card_data)
        except json.JSONDecodeError as e:
            print(f"[ERROR] --card-data 不是合法 JSON: {e}", file=sys.stderr)
            sys.exit(1)

        updated = card_data.get("updatedData", {}) or {}
        visit_summary = updated.get("visitSummary") or args.visit_summary or ""
        contacts_raw = updated.get("contacts", {}).get("contact", [])
        contacts = [
            {
                "id": c.get("id", ""),
                "name": c.get("name", ""),
                "phone": c.get("phone", ""),
                "isKp": int(c.get("isKp", 0) or 0),
            }
            for c in contacts_raw
        ]
        wechat_contacts_raw = updated.get("contacts", {}).get("wechatContact", [])
        wechat_contacts = [
            {
                "wechatUserId":   wc.get("wechatUserId", ""),
                "wechatNickname": wc.get("wechatNickname", ""),
                "wechatAvatar":   wc.get("wechatAvatar", ""),
                "wechatRemark":   wc.get("wechatRemark", ""),
                "wechatCorpId":   wc.get("wechatCorpId", ""),
                "isKp":           int(wc.get("isKp", 0) or 0),
            }
            for wc in wechat_contacts_raw
        ]
        visit_id = card_data.get("visitId") or args.visit_id

        result = fill_visit_log(
            visit_id=visit_id,
            visit_summary=visit_summary,
            contacts=contacts,
            wechat_contacts=wechat_contacts,
        )
        out = {
            "success": result.get("success", False),
            "visitId": result.get("visitId"),
            "_card_data": card_data,  # 供 Agent 构造 action_result.skill_result.data
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))

        if result.get("success"):
            suggested = _build_completed_card(
                visit_id=visit_id,
                detail=result.get("_detail", {}),
                contacts=result.get("_contacts", contacts),
                visit_summary=visit_summary,
                wechat_contacts=result.get("_wechat_contacts", wechat_contacts),
            )
            print("\n===== SUGGESTED_CARD (整条回复请原样复制下面这一行) =====")
            print(suggested)
            print(
                "⛔ 修改日志成功：先输出 action_result（skill_result.data 使用 _card_data），"
                "再把上面 SUGGESTED_CARD 那一行原样输出，块外零字符，禁止加 \\\" 转义/换行/Markdown。",
                file=sys.stderr,
            )
        return

    # ── fill 模式（默认）──────────────────────────────────────────────────────
    contacts = _parse_contacts(args)
    wechat_contacts = _parse_wechat_contacts(args)
    if not contacts and not wechat_contacts:
        print("[ERROR] 至少需要提供一个联系人（--contact-name 或 --wechat-user-id）", file=sys.stderr)
        sys.exit(1)

    # 补录签退：needSupplementary=1 时先调 submitLaterCheckin，失败不影响主流程
    if args.need_supplementary == 1:
        ok = submit_later_checkin(args.visit_id)
        if ok:
            print("[INFO] 补录签退成功", file=sys.stderr)
        else:
            print("[WARN] 补录签退失败，继续填写日志", file=sys.stderr)

    result = fill_visit_log(
        visit_id=args.visit_id,
        visit_summary=args.visit_summary,
        contacts=contacts,
        wechat_contacts=wechat_contacts,
    )

    out = {"success": result.get("success", False), "visitId": result.get("visitId")}
    action_result_block = None
    suggested = None
    if result.get("success"):
        action_result_block = _build_action_result(
            visit_id=result.get("visitId"),
            detail=result.get("_detail", {}),
            contacts=result.get("_contacts", contacts),
            visit_summary=result.get("_visitSummary", args.visit_summary),
            action_id=args.action_id,
            client_action_id=args.client_action_id,
            card_id=args.card_id,
            card_name=args.card_name,
            wechat_contacts=result.get("_wechat_contacts", wechat_contacts),
        )
        suggested = _build_completed_card(
            visit_id=result.get("visitId"),
            detail=result.get("_detail", {}),
            contacts=result.get("_contacts", contacts),
            visit_summary=result.get("_visitSummary", args.visit_summary),
            wechat_contacts=result.get("_wechat_contacts", wechat_contacts),
        )

    # 先打印结果（供调试），再单独一行裸输出干净协议块。
    # ⚠️ 关键：协议块绝不能塞进 json.dumps，否则内部双引号会被转义成 \" 导致前端无法解析。
    print(json.dumps(out, ensure_ascii=False, indent=2))

    if action_result_block and suggested:
        print("\n===== SUGGESTED_CARD (整条回复请原样复制下面两行) =====")
        print(action_result_block)
        print(suggested)
        print(
            "⛔ 提交成功：把上面 SUGGESTED_CARD 下面两行（action_result + visit-log-completed-card）"
            "原样作为整条回复输出，块外零字符，禁止加 \\\" 转义/换行/Markdown。",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
