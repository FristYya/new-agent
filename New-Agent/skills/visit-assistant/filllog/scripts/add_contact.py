"""
新增/更新电话联系人

接口：addOrUpdateContact（PC 端 visit-service）
路径：POST http://127.0.0.1:8080/rest/apicenter/visit/web/visitNew/addOrUpdateContact

请求体：{ objectType, objectName, objectId, contact: {name, phone, isKp} }

流程：
1. 先 queryContacts 查询该拜访对象的联系人列表
2. 按手机号匹配：
   - 如果已存在 → 直接返回该联系人（含 id）
   - 如果不存在 → 调用 addOrUpdateContact 创建 → 再 queryContacts 拿到 id

用法：
  python add_contact.py --object-type provider --object-name "测试用的公司003" --object-id 1280915402000166 --contact-name "张三" --contact-phone 19837139264 --contact-is-kp 1

输出 JSON：
{
  "success": true,
  "contact": {
    "id": "2054117585150087170",
    "name": "张三",
    "phone": "173****2121",
    "isKp": 1
  }
}
"""

import argparse
import json
import sys
import os
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_client import call_api, _get_pc_cookie, BASE_URL_MAP, PATH_MAP


def add_contact(object_type: str, object_name: str, object_id: str,
                contact_name: str, contact_phone: str, contact_is_kp: int = 0) -> dict:
    """
    新增/更新联系人（先查再创，避免重复）

    Args:
        object_type: 拜访对象类型（如 provider）
        object_name: 拜访对象名称
        object_id: 拜访对象 ID
        contact_name: 联系人姓名
        contact_phone: 联系人手机号
        contact_is_kp: 是否 KP（1=是 0=否）

    Returns:
        {"success": bool, "contact": {id, name, phone, isKp}}
    """
    # 1. 先查询已有联系人
    from query_contacts import query_contacts
    existing = query_contacts(object_type, object_name, object_id)
    all_contacts = existing.get("contacts", [])

    # 2. 按手机号匹配（后端存的可能是脱敏格式 198****9264，所以也做前3位匹配）
    matched = _find_by_phone(all_contacts, contact_phone)

    if matched:
        # 已存在，直接返回（如果 isKp 不同，需要更新）
        if matched.get("isKp") != contact_is_kp:
            # 调用 addOrUpdateContact 更新 isKp
            _do_add_or_update(object_type, object_name, object_id,
                              contact_name, contact_phone, contact_is_kp)
            # 重新查询
            existing = query_contacts(object_type, object_name, object_id)
            matched = _find_by_phone(existing.get("contacts", []), contact_phone) or matched
        return {"success": True, "contact": matched}

    # 3. 不存在，创建
    result = _do_add_or_update(object_type, object_name, object_id,
                               contact_name, contact_phone, contact_is_kp)

    if not result.get("success"):
        return {"success": False, "contact": None, "message": result.get("message", "创建失败")}

    # 4. 重新查询获取带 id 的记录
    existing = query_contacts(object_type, object_name, object_id)
    all_contacts = existing.get("contacts", [])
    matched = _find_by_phone(all_contacts, contact_phone)

    if matched:
        return {"success": True, "contact": matched}

    # 实在找不到，取最后一个
    if all_contacts:
        return {"success": True, "contact": all_contacts[-1]}

    return {"success": False, "contact": None, "message": "创建后查询不到"}


def _do_add_or_update(object_type: str, object_name: str, object_id: str,
                      contact_name: str, contact_phone: str, contact_is_kp: int) -> dict:
    """
    直接调用 addOrUpdateContact API（绕过 call_api 的 sys.exit 机制，
    因为"手机号已存在"是业务错误但不是致命错误）
    """
    payload = {
        "objectType": object_type,
        "objectName": object_name,
        "objectId": object_id,
        "contact": {
            "name": contact_name,
            "phone": contact_phone,
            "isKp": contact_is_kp,
        }
    }

    base_url = BASE_URL_MAP.get("addOrUpdateContact", "http://127.0.0.1:8080")
    path_prefix = PATH_MAP.get("addOrUpdateContact", "/rest/apicenter/visit/web/visitNew")
    url = f"{base_url}{path_prefix}/addOrUpdateContact"

    try:
        pc_cookie = _get_pc_cookie() or ""
        resp = requests.post(url, json=payload, headers={
            "Cookie": pc_cookie,
            "Content-Type": "application/json",
            "Referer": "http://127.0.0.1:8080/visit/tools/visit",
        }, timeout=30)

        data = resp.json()
        success = data.get("success", False)
        message = data.get("baseResponse", {}).get("message", "")
        return {"success": success, "message": message}

    except Exception as e:
        return {"success": False, "message": str(e)}


def _find_by_phone(contacts: list, phone: str) -> dict:
    """
    在联系人列表中按手机号匹配

    后端可能存储脱敏格式（198****9264），所以做前3+后4位匹配
    """
    if not phone:
        return None

    # 精确匹配
    for c in contacts:
        if c.get("phone") == phone:
            return c

    # 脱敏匹配：前3位+后4位
    prefix = phone[:3]
    suffix = phone[-4:] if len(phone) >= 4 else phone
    for c in contacts:
        c_phone = c.get("phone", "")
        if c_phone.startswith(prefix) and c_phone.endswith(suffix) and "****" in c_phone:
            return c

    return None


def add_contact_from_detail(detail: dict, contact_name: str,
                            contact_phone: str, contact_is_kp: int = 0) -> dict:
    """
    从拜访详情自动提取参数并新增联系人

    Args:
        detail: queryVisitPlanDetails 返回的详情
        contact_name: 联系人姓名
        contact_phone: 联系人手机号
        contact_is_kp: 是否 KP

    Returns:
        {"success": bool, "contact": {id, name, phone, isKp}}
    """
    from query_contacts import _resolve_object_id, query_contacts_from_detail
    basic_info = detail.get("basicInfo", {})
    checkin_info = detail.get("visitCheckinInfo", {})
    object_type = basic_info.get("visitObject", "")

    # objectName: 不同类型用不同字段（与 query_contacts.py 保持一致）
    name_map = {
        "provider": basic_info.get("businessLicense", "") or detail.get("providerName", ""),
        "agent": basic_info.get("businessLicense", "") or detail.get("agentName", ""),
        "ad_customer": basic_info.get("businessLicense", ""),
        "store": basic_info.get("storeName", ""),
    }
    object_name = name_map.get(object_type, basic_info.get("businessLicense", ""))
    object_id = _resolve_object_id(detail)

    return add_contact(object_type, object_name, object_id,
                       contact_name, contact_phone, contact_is_kp)


def main():
    parser = argparse.ArgumentParser(description="新增/更新电话联系人")
    parser.add_argument("--object-type", required=True, help="拜访对象类型，如 provider")
    parser.add_argument("--object-name", required=True, help="拜访对象名称")
    parser.add_argument("--object-id", required=True, help="拜访对象 ID")
    parser.add_argument("--contact-name", required=True, help="联系人姓名")
    parser.add_argument("--contact-phone", required=True, help="联系人手机号")
    parser.add_argument("--contact-is-kp", type=int, default=0, help="是否KP: 1=是 0=否，默认0")
    args = parser.parse_args()

    result = add_contact(
        object_type=args.object_type,
        object_name=args.object_name,
        object_id=args.object_id,
        contact_name=args.contact_name,
        contact_phone=args.contact_phone,
        contact_is_kp=args.contact_is_kp,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
