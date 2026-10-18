"""
查询拜访对象关联的联系人列表

接口：queryContacts（PC 端 visit-service）
路径：POST http://127.0.0.1:8080/rest/apicenter/visit/web/visitNew/queryContacts

请求体：{ objectType, objectName, objectId }
- objectType: 拜访对象类型（如 "provider", "ad_customer"）
- objectName: 拜访对象名称（如 "测试用的公司003"）
- objectId:   拜访对象 ID（如 providerId, targetCustomerId 等）

这些字段可以从 queryVisitPlanDetails 的 basicInfo 和顶层字段获取：
- objectType = basicInfo.visitObject
- objectName = basicInfo.businessLicense
- objectId   = 根据 visitObject 类型选择：
    provider → providerId
    ad_customer → targetCustomerId 或 licenseCode
    agent → agentId

用法：
  python query_contacts.py --object-type provider --object-name "测试用的公司003" --object-id 1280915402000166

输出 JSON：
{
  "contacts": [
    {
      "id": "2054117585150087170",
      "name": "苏",
      "phone": "173****2121",
      "isKp": 1
    }
  ]
}
"""

import argparse
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_client import call_api


def query_contacts(object_type: str, object_name: str, object_id: str) -> dict:
    """
    查询拜访对象关联的联系人

    Args:
        object_type: 拜访对象类型（如 provider, ad_customer）
        object_name: 拜访对象名称
        object_id: 拜访对象 ID

    Returns:
        {"contacts": [{id, name, phone, isKp}]}
    """
    payload = {
        "objectType": object_type,
        "objectName": object_name,
        "objectId": object_id,
    }

    data = call_api("queryContacts", payload, step="query_contacts")

    contacts = data.get("contacts", [])
    return {"contacts": contacts}


def query_contacts_from_detail(detail: dict) -> dict:
    """
    从拜访详情自动提取参数并查询联系人

    Args:
        detail: queryVisitPlanDetails 返回的详情数据

    Returns:
        {"contacts": [{id, name, phone, isKp}]}
    """
    basic_info = detail.get("basicInfo", {})
    object_type = basic_info.get("visitObject", "")
    # objectName: 不同类型用不同字段
    name_map = {
        "provider": basic_info.get("businessLicense", "") or detail.get("providerName", ""),
        "agent": basic_info.get("businessLicense", "") or detail.get("agentName", ""),
        "ad_customer": basic_info.get("businessLicense", ""),
        "store": basic_info.get("storeName", ""),
    }
    object_name = name_map.get(object_type, basic_info.get("businessLicense", ""))

    # 根据 visitObject 类型确定 objectId
    object_id = _resolve_object_id(detail)

    if not object_type or not object_id:
        return {"contacts": []}

    return query_contacts(object_type, object_name, object_id)


def _resolve_object_id(detail: dict) -> str:
    """根据 visitObject 类型选择对应的 objectId"""
    basic_info = detail.get("basicInfo", {})
    checkin_info = detail.get("visitCheckinInfo", {})
    visit_object = basic_info.get("visitObject", "")

    # 映射关系：
    # provider → providerId (顶层)
    # ad_customer → targetCustomerId (basicInfo)
    # agent → agentId (顶层)
    # store → storePoiId (checkinInfo)
    id_map = {
        "provider": detail.get("providerId", ""),
        "agent": detail.get("agentId", ""),
        "ad_customer": basic_info.get("targetCustomerId", ""),
        "store": checkin_info.get("storePoiId", ""),
    }

    return id_map.get(visit_object, "")


def main():
    parser = argparse.ArgumentParser(description="查询拜访对象关联的联系人")
    parser.add_argument("--object-type", required=True, help="拜访对象类型，如 provider")
    parser.add_argument("--object-name", required=True, help="拜访对象名称")
    parser.add_argument("--object-id", required=True, help="拜访对象 ID")
    args = parser.parse_args()

    result = query_contacts(
        object_type=args.object_type,
        object_name=args.object_name,
        object_id=args.object_id,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
