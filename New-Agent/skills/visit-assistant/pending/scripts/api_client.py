"""
填写拜访日志 Skill - HTTP API 客户端

封装与 visit-service 后端的 HTTP 通信，包含：
- 动态 Cookie 鉴权（从 SSO 上下文 / 环境变量 / .cookie 文件获取）
- 统一错误处理和重试逻辑
- 请求/响应封装

依赖：pip install requests

Cookie 获取优先级（PC 端）：
  1. ~/.new-agent/sso/sso-state.json  — SSO 自动登录状态
  2. 环境变量 FILLLOG_COOKIE
  3. skill 目录下 .cookie 文件

Cookie 获取优先级（APP 端）：
  1. 环境变量 FILLLOG_APP_COOKIE / FILLLOG_APP_TOKEN / FILLLOG_APP_USER_ID
  2. skill 目录下 .app_cookie 文件（格式：第一行 cookie，第二行 token，第三行 userId）
"""

import sys
import json
import time
import logging
import requests
import os
from typing import Dict, Any, Optional

# ============================================================
# 域名 / 路径映射
# ============================================================
# 注意：fillVisitLog 路径前缀与其他接口不同！
#   - queryVisitPlanList: /rest/apicenter/visit/web/visitNew
#   - fillVisitLog:       /rest/apicenter/visit/web/visit/new/work  (不同的路径！)

# 方法 → Cookie 类型映射
COOKIE_TYPE_MAP = {
    "queryVisitPlanList": "pc",
    "preCheckCheckout": "pc",
    "submitCheckout": "pc",
    "getVisitPlanCache": "pc",
    "fillVisitLog": "pc",
    "saveVisitPlanCache": "pc",
    "queryVisitPlanDetails": "pc",
    "queryContacts": "pc",
    "addOrUpdateContact": "pc",
    "checkVisitSupplementAllowed": "pc",
    "submitLaterCheckin": "pc",
    "queryAiVisit": "pc",   # 通用只读查询（待补充差旅/今日待签到/计划列表等）
}

BASE_URL_MAP = {
    "queryVisitPlanList": "http://127.0.0.1:8080",
    "preCheckCheckout": "http://127.0.0.1:8080",
    "submitCheckout": "http://127.0.0.1:8080",
    "getVisitPlanCache": "http://127.0.0.1:8080",
    "fillVisitLog": "http://127.0.0.1:8080",
    "saveVisitPlanCache": "http://127.0.0.1:8080",
    "queryVisitPlanDetails": "http://127.0.0.1:8080",
    "queryContacts": "http://127.0.0.1:8080",
    "addOrUpdateContact": "http://127.0.0.1:8080",
    "checkVisitSupplementAllowed": "http://127.0.0.1:8080",
    "submitLaterCheckin": "http://127.0.0.1:8080",
    "queryAiVisit": "http://127.0.0.1:8080",
}

PATH_MAP = {
    "queryVisitPlanList": "/rest/apicenter/visit/web/visitNew",
    "preCheckCheckout": "/rest/apicenter/visit/app/visitAi/visitCenter",
    "submitCheckout": "/rest/apicenter/visit/app/visitAi/visitCenter",
    "getVisitPlanCache": "/rest/apicenter/visit/web/visitNew",
    "fillVisitLog": "/rest/apicenter/visit/web/visit/new/work",        # ⚠️ 特殊路径！
    "saveVisitPlanCache": "/rest/apicenter/visit/web/visitNew",
    "queryVisitPlanDetails": "/rest/apicenter/visit/web/visitNew",
    "queryContacts": "/rest/apicenter/visit/web/visitNew",
    "addOrUpdateContact": "/rest/apicenter/visit/web/visitNew",
    "checkVisitSupplementAllowed": "/rest/apicenter/visit/web/visitNew",
    "submitLaterCheckin": "/rest/apicenter/visit/web/visitNew",
    "queryAiVisit": "/rest/apicenter/visit/web/visitCenter",
}

# 错误重试配置
MAX_RETRY = 2
RETRYABLE_STATUS = {500, 502, 503, 504}
AUTH_STATUS = {401, 302}

# 超时配置
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30


# ============================================================
# Cookie / 凭证 动态获取
# ============================================================

def _build_cookie_str_from_dict(cookie_dict: dict) -> str:
    """将 cookie 字典拼成 'key=value; key=value' 字符串"""
    return "; ".join(f"{k}={v}" for k, v in cookie_dict.items() if v)


def _get_runtime_cookie() -> Optional[str]:
    """New-Agent/visit 环境下从 AGENT_RUNTIME_CONTEXT / RUN_CONTEXT 读取 bizContext.cookie（最高优先级）。

    与 visit-plan-create 等已在 visit 平台跑通的 Skill 保持一致：
    平台会把当前登录态注入 运行上下文，脚本无需再传 --cookies。
    未注入环境变量时返回 None，自动降级到环境变量 / SSO / 文件。
    """
    raw = os.getenv("AGENT_RUNTIME_CONTEXT") or os.getenv("RUN_CONTEXT")
    if not raw:
        return None
    try:
        ctx = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(ctx, dict):
        return None
    biz = ctx.get("bizContext")
    biz = biz if isinstance(biz, dict) else {}
    for key in ("cookie", "Cookie", "cookies", "COOKIE"):
        val = ctx.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        val = biz.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _get_pc_cookie() -> Optional[str]:
    """从上下文中动态获取 PC 端 Cookie。

    优先级：
    0. AGENT_RUNTIME_CONTEXT / RUN_CONTEXT 的 bizContext.cookie（visit 平台注入）— 最高优先级
    1. runContext.cookies 环境变量（New-Agent 平台从前端注入，JSON 对象格式）
    2. 从 SSO 登录状态 (sso-state.json) 提取
    3. 从环境变量 FILLLOG_COOKIE 读取（完整 cookie 字符串）
    4. 从 skill 目录下的 .cookie 文件读取
    """
    # 0. AGENT_RUNTIME_CONTEXT / RUN_CONTEXT bizContext.cookie（visit 平台注入，最高优先级）
    rt_cookie = _get_runtime_cookie()
    if rt_cookie:
        return rt_cookie

    # 1. 优先从 runContext.cookies 读取（前端通过 runContext 注入，平台注入为环境变量 cookies）
    run_context_cookies = os.environ.get("cookies", "").strip()
    if run_context_cookies:
        try:
            cookie_obj = json.loads(run_context_cookies)
            if isinstance(cookie_obj, dict) and cookie_obj:
                return _build_cookie_str_from_dict(cookie_obj)
        except (json.JSONDecodeError, TypeError):
            # 如果不是 JSON，可能已经是 cookie 字符串，直接使用
            if "=" in run_context_cookies:
                return run_context_cookies

    # 2. 从 SSO 登录上下文中提取 cookie
    sso_state_path = os.path.expanduser("~/.new-agent/sso/sso-state.json")
    if os.path.exists(sso_state_path):
        try:
            with open(sso_state_path, 'r', encoding='utf-8') as f:
                state = json.load(f)
            cookies = state.get("cookies", [])
            if cookies:
                cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                return cookie_str
        except Exception:
            pass

    # 3. 从环境变量 FILLLOG_COOKIE 读取
    cookie = os.environ.get("FILLLOG_COOKIE", "").strip()
    if cookie:
        return cookie

    # 4. 从 skill 目录下的 .cookie 文件读取
    script_dir = os.path.dirname(os.path.abspath(__file__))
    skill_dir = os.path.dirname(script_dir)
    cookie_file = os.path.join(skill_dir, ".cookie")

    if os.path.exists(cookie_file):
        try:
            with open(cookie_file, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except Exception:
            pass

    return None


def _get_app_credentials() -> tuple:
    """从上下文中动态获取 APP 端凭证（cookie, token, userId）。

    优先级：
    1. 从环境变量 FILLLOG_APP_COOKIE / FILLLOG_APP_TOKEN / FILLLOG_APP_USER_ID 读取
    2. 从 skill 目录下的 .app_cookie 文件读取
       文件格式（每行一项）：
         第1行：Cookie 字符串
         第2行：token
         第3行：userId
    """
    app_cookie = os.environ.get("FILLLOG_APP_COOKIE", "").strip()
    app_token = os.environ.get("FILLLOG_APP_TOKEN", "").strip()
    app_user_id = os.environ.get("FILLLOG_APP_USER_ID", "").strip()

    if app_cookie and app_token and app_user_id:
        return app_cookie, app_token, app_user_id

    # 尝试从 .app_cookie 文件读取
    script_dir = os.path.dirname(os.path.abspath(__file__))
    skill_dir = os.path.dirname(script_dir)
    app_cookie_file = os.path.join(skill_dir, ".app_cookie")

    if os.path.exists(app_cookie_file):
        try:
            with open(app_cookie_file, 'r', encoding='utf-8') as f:
                lines = [line.strip() for line in f.readlines()]
            if len(lines) >= 3:
                return lines[0], lines[1], lines[2]
            elif len(lines) == 1:
                return lines[0], "", ""
        except Exception:
            pass

    return None, None, None


# ============================================================
# API 客户端
# ============================================================

class FillLogApiClient:
    """填写拜访日志 HTTP API 客户端（PC + APP 双端）"""

    def __init__(self, pc_cookie: Optional[str] = None, app_cookie: Optional[str] = None,
                 app_token: Optional[str] = None, app_user_id: Optional[str] = None):
        # PC 端 Cookie：优先使用传参，否则从上下文动态获取
        self._pc_cookie = pc_cookie or _get_pc_cookie()
        if not self._pc_cookie:
            print(
                "[ERROR:AUTH] 未找到 PC 端 Cookie。\n"
                "请通过以下任一方式提供：\n"
                "  1. SSO 登录：确保 ~/.new-agent/sso/sso-state.json 存在\n"
                "  2. 环境变量：export FILLLOG_COOKIE='your_cookie_string'\n"
                "  3. .cookie 文件：在 skill 目录下创建 .cookie 文件并写入 Cookie",
                file=sys.stderr
            )
            sys.exit(1)

        # APP 端凭证：优先使用传参，否则从上下文动态获取
        _app_cookie, _app_token, _app_user_id = _get_app_credentials()
        self._app_cookie = app_cookie or _app_cookie or ""
        self._app_token = app_token or _app_token or ""
        self._app_user_id = app_user_id or _app_user_id or ""

        self._session = requests.Session()
        self._retry_count = {}

    def _get_headers(self, method: str) -> dict:
        """根据方法选择对应的 Cookie 和 Headers"""
        cookie_type = COOKIE_TYPE_MAP.get(method, "pc")
        if cookie_type == "pc":
            headers = {
                "Cookie": self._pc_cookie,
                "Content-Type": "application/json",
                "Referer": "http://127.0.0.1:8080/visit/tools/visit",
                "Origin": "http://127.0.0.1:8080",
                "Host": "127.0.0.1:8080",
                "trace-context": '{"laneId":"PRT.v1"}',
            }
            # queryAiVisit 需要带泳道头路由到正确 staging 实例，用与 visit-plan-create 一致的 laneId
            if method == "queryAiVisit":
                headers["trace-context"] = '{"laneId":"PRT.visit"}'
            return headers
        else:
            return {"Cookie": self._app_cookie, "Content-Type": "application/json"}

    def post(self, method: str, payload: dict, step: str = "default") -> dict:
        """
        调用后端 HTTP API（POST）— 自动选择域名、路径

        Args:
            method: 接口方法名（如 fillVisitLog）
            payload: 请求体 JSON
            step: 当前步骤标识（用于重试计数）

        Returns:
            响应 data 字段内容

        Raises:
            SystemExit: 不可恢复错误时退出
        """
        base_url = BASE_URL_MAP.get(method, "http://127.0.0.1:8080")
        path_prefix = PATH_MAP.get(method, "/rest/apicenter/visit/web/visitNew")
        url = f"{base_url}{path_prefix}/{method}"
        headers = self._get_headers(method)
        retry_key = f"{step}:{method}"
        retry_count = self._retry_count.get(retry_key, 0)

        for attempt in range(retry_count, MAX_RETRY + 1):
            self._retry_count[retry_key] = attempt + 1

            try:
                resp = self._session.post(
                    url,
                    json=payload,
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                    headers=headers
                )

                # --- 认证失败 ---
                if resp.status_code in AUTH_STATUS:
                    print(f"[ERROR:AUTH] {method} 认证失败(HTTP {resp.status_code})，Cookie 可能已过期", file=sys.stderr)
                    sys.exit(1)

                # --- 权限不足 ---
                if resp.status_code == 403:
                    print(f"[ERROR:403] {method} 权限不足", file=sys.stderr)
                    sys.exit(3)

                # --- 服务端错误：重试 ---
                if resp.status_code in RETRYABLE_STATUS:
                    if attempt < MAX_RETRY:
                        logging.warning(f"[RETRY] {method} 收到 {resp.status_code}，重试 {attempt+1}/{MAX_RETRY}")
                        time.sleep(1)
                        continue
                    else:
                        print(f"[ERROR:5xx] {method} 服务端错误 {resp.status_code}，已重试 {MAX_RETRY} 次", file=sys.stderr)
                        sys.exit(2)

                # --- 其他非 200 ---
                if resp.status_code != 200:
                    print(f"[ERROR:HTTP] {method} HTTP {resp.status_code}", file=sys.stderr)
                    sys.exit(2)

                # --- 解析响应 ---
                data = resp.json()

                # 检查业务返回码
                base_resp = data.get("baseResponse", {})
                # 兼容 resultCode 和 code 两种字段名
                code = base_resp.get("resultCode", base_resp.get("code", 0))
                if code != 0:
                    msg = base_resp.get("resultMessage", base_resp.get("message", "业务错误"))
                    print(f"[ERROR:BIZ] {method} code={code} msg={msg}", file=sys.stderr)
                    sys.exit(3)

                # 返回：优先取 data 字段；如果没有 data 字段（如 fillVisitLog），返回整个响应
                if "data" in data:
                    return data.get("data", {})
                else:
                    # fillVisitLog 返回 {"success": true, "baseResponse": {...}}，无 data 字段
                    return data

            except requests.Timeout:
                if attempt < MAX_RETRY:
                    logging.warning(f"[TIMEOUT] {method} 超时，重试 {attempt+1}/{MAX_RETRY}")
                    time.sleep(1)
                    continue
                else:
                    print(f"[ERROR:TIMEOUT] {method} 请求超时，已重试 {MAX_RETRY} 次", file=sys.stderr)
                    sys.exit(2)

            except requests.ConnectionError:
                if attempt < MAX_RETRY:
                    logging.warning(f"[CONN] {method} 连接失败，重试 {attempt+1}/{MAX_RETRY}")
                    time.sleep(2)
                    continue
                else:
                    print(f"[ERROR:CONN] {method} 连接失败，已重试 {MAX_RETRY} 次", file=sys.stderr)
                    sys.exit(2)

            except json.JSONDecodeError:
                print(f"[ERROR:JSON] {method} 响应非 JSON", file=sys.stderr)
                sys.exit(2)

        print(f"[ERROR] {method} 超过最大重试次数", file=sys.stderr)
        sys.exit(2)


# ============================================================
# 便捷函数
# ============================================================

_default_client: Optional[FillLogApiClient] = None


def get_client(pc_cookie: Optional[str] = None, app_cookie: Optional[str] = None,
             app_token: Optional[str] = None, app_user_id: Optional[str] = None) -> FillLogApiClient:
    """获取全局 API 客户端（懒初始化）"""
    global _default_client
    if _default_client is None:
        _default_client = FillLogApiClient(pc_cookie=pc_cookie, app_cookie=app_cookie,
                                          app_token=app_token, app_user_id=app_user_id)
    return _default_client


def call_api(method: str, payload: dict, step: str = "default",
             pc_cookie: Optional[str] = None, app_cookie: Optional[str] = None,
             app_token: Optional[str] = None, app_user_id: Optional[str] = None) -> dict:
    """
    便捷调用函数

    Args:
        method: 接口方法名
        payload: 请求体
        step: 步骤标识
        pc_cookie: 可选 PC 端 Cookie（不传则从上下文自动获取）
        app_cookie: 可选 APP 端 Cookie
        app_token: 可选 APP 端 Token
        app_user_id: 可选 APP 端 UserId

    Returns:
        响应 data 字段
    """
    client = get_client(pc_cookie=pc_cookie, app_cookie=app_cookie,
                        app_token=app_token, app_user_id=app_user_id)
    return client.post(method, payload, step=step)
