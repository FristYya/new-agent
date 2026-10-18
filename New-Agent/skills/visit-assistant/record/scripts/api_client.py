"""
拜访签退 Skill - HTTP API 客户端

封装签退相关接口的 HTTP 通信：
- queryVisitPlanList（PC 端，SSO 鉴权）：查询待签退计划列表
- preCheckCheckout / submitCheckout（APP 端）：签退预校验、提交签退

依赖：pip install requests

============================================================
Cookie / 凭证获取（均从上下文动态获取）
============================================================

PC 端 Cookie（查询列表用）优先级：
  1. runContext.cookies 环境变量（平台注入，JSON 对象）
  2. ~/.new-agent/sso/sso-state.json（SSO 自动登录）
  3. 环境变量 CHECKOUT_COOKIE
  4. skill 目录下 .cookie 文件

APP 端 Cookie（预校验 / 签退用）优先级：
  1. runContext.appCookie 环境变量（平台注入）
  2. 环境变量 CHECKOUT_APP_COOKIE
  3. skill 目录下 .app_cookie 文件（单行 cookie 字符串）
"""

import sys
import json
import time
import logging
import requests
import os
from typing import Optional

# ============================================================
# 域名 / 路径 / Cookie 类型映射（internal 域名，SSO 鉴权）
# ============================================================
# 全部走 internal 域名 + SSO 鉴权：
# - queryVisitPlanListApp:              http://127.0.0.1:8080  /rest/apicenter/visit/app/visitAi/visitNew
# - preCheckCheckout / submitCheckout:  http://127.0.0.1:8080  /rest/apicenter/visit/app/visitAi/visitCenter

BASE_URL_MAP = {
    "queryVisitPlanListApp": "http://127.0.0.1:8080",
    "preCheckCheckout": "http://127.0.0.1:8080",
    "submitCheckout": "http://127.0.0.1:8080",
}

PATH_MAP = {
    "queryVisitPlanListApp": "/rest/apicenter/visit/app/visitAi/visitNew",
    "preCheckCheckout": "/rest/apicenter/visit/app/visitAi/visitCenter",
    "submitCheckout": "/rest/apicenter/visit/app/visitAi/visitCenter",
}

# 改为 internal 后全部走 SSO 鉴权（PC 端 Cookie）
COOKIE_TYPE_MAP = {
    "queryVisitPlanListApp": "pc",
    "preCheckCheckout": "pc",
    "submitCheckout": "pc",
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
    """从上下文中动态获取 PC 端 Cookie（查询列表用，SSO 鉴权）。"""
    # 0. AGENT_RUNTIME_CONTEXT / RUN_CONTEXT bizContext.cookie（visit 平台注入，最高优先级）
    rt_cookie = _get_runtime_cookie()
    if rt_cookie:
        return rt_cookie

    # 1. runContext.cookies（平台注入为环境变量 cookies）
    run_context_cookies = os.environ.get("cookies", "").strip()
    if run_context_cookies:
        try:
            cookie_obj = json.loads(run_context_cookies)
            if isinstance(cookie_obj, dict) and cookie_obj:
                return _build_cookie_str_from_dict(cookie_obj)
        except (json.JSONDecodeError, TypeError):
            if "=" in run_context_cookies:
                return run_context_cookies

    # 2. SSO 登录状态
    sso_state_path = os.path.expanduser("~/.new-agent/sso/sso-state.json")
    if os.path.exists(sso_state_path):
        try:
            with open(sso_state_path, 'r', encoding='utf-8') as f:
                state = json.load(f)
            cookies = state.get("cookies", [])
            if cookies:
                return "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        except Exception:
            pass

    # 3. 环境变量 CHECKOUT_COOKIE（兼容 FILLLOG_COOKIE）
    cookie = os.environ.get("CHECKOUT_COOKIE", "").strip() or os.environ.get("FILLLOG_COOKIE", "").strip()
    if cookie:
        return cookie

    # 4. skill 目录下 .cookie 文件
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


def _get_app_cookie() -> Optional[str]:
    """从上下文中动态获取 APP 端 Cookie（预校验 / 签退用）。"""
    # 0. AGENT_RUNTIME_CONTEXT / RUN_CONTEXT bizContext.cookie（visit 平台注入，最高优先级；改 internal 后统一走 SSO）
    rt_cookie = _get_runtime_cookie()
    if rt_cookie:
        return rt_cookie

    # 1. runContext.appCookie（平台注入）
    app_ctx = os.environ.get("appCookie", "").strip()
    if app_ctx:
        try:
            cookie_obj = json.loads(app_ctx)
            if isinstance(cookie_obj, dict) and cookie_obj:
                return _build_cookie_str_from_dict(cookie_obj)
        except (json.JSONDecodeError, TypeError):
            if "=" in app_ctx:
                return app_ctx

    # 2. 环境变量 CHECKOUT_APP_COOKIE
    app_cookie = os.environ.get("CHECKOUT_APP_COOKIE", "").strip()
    if app_cookie:
        return app_cookie

    # 3. skill 目录下 .app_cookie 文件（单行 cookie）
    script_dir = os.path.dirname(os.path.abspath(__file__))
    skill_dir = os.path.dirname(script_dir)
    app_cookie_file = os.path.join(skill_dir, ".app_cookie")
    if os.path.exists(app_cookie_file):
        try:
            with open(app_cookie_file, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except Exception:
            pass

    return None


# ============================================================
# API 客户端
# ============================================================

class CheckoutApiClient:
    """拜访签退 HTTP API 客户端（PC 查询 + APP 签退）"""

    def __init__(self, pc_cookie: Optional[str] = None, app_cookie: Optional[str] = None):
        self._pc_cookie = pc_cookie or _get_pc_cookie()
        self._app_cookie = app_cookie or _get_app_cookie()
        self._session = requests.Session()
        self._retry_count = {}

    def _get_headers(self, method: str) -> dict:
        """根据方法选择对应的 Cookie 和 Headers"""
        cookie_type = COOKIE_TYPE_MAP.get(method, "pc")
        if cookie_type == "pc":
            if not self._pc_cookie:
                print(
                    "[ERROR:AUTH] 未找到 PC 端 Cookie。请通过 SSO 登录，或设置 CHECKOUT_COOKIE 环境变量，"
                    "或在 skill 目录创建 .cookie 文件。",
                    file=sys.stderr
                )
                sys.exit(1)
            return {
                "Cookie": self._pc_cookie,
                "Content-Type": "application/json",
                "Referer": "http://127.0.0.1:8080/visit/tools/visit",
                "Origin": "http://127.0.0.1:8080",
                "Host": "127.0.0.1:8080",
                "trace-context": '{"laneId":"PRT.v1"}',
            }
        else:
            if not self._app_cookie:
                print(
                    "[ERROR:AUTH] 未找到 APP 端 Cookie。请设置 CHECKOUT_APP_COOKIE 环境变量，"
                    "或在 skill 目录创建 .app_cookie 文件。",
                    file=sys.stderr
                )
                sys.exit(1)
            return {
                "Cookie": self._app_cookie,
                "Content-Type": "application/json;charset=UTF-8",
                "referer": "http://127.0.0.1:3100/",
                "accept": "*/*",
                "accept-language": "zh-Hans-CN;q=1",
                "user-agent": "locallife_visit/3.7.10 (iPhone; iOS 18.7.8; Scale/3.00)",
                "x-client-info": "model=iPhone16,2;os=iOS;nqe-score=-1;network=WIFI;",
            }

    def post(self, method: str, payload: dict, step: str = "default") -> dict:
        """调用后端 HTTP API（POST）— 自动选择域名、路径、Cookie"""
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
                    url, json=payload,
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                    headers=headers
                )

                if resp.status_code in AUTH_STATUS:
                    print(f"[ERROR:AUTH] {method} 认证失败(HTTP {resp.status_code})，Cookie 可能已过期", file=sys.stderr)
                    sys.exit(1)

                if resp.status_code == 403:
                    print(f"[ERROR:403] {method} 权限不足", file=sys.stderr)
                    sys.exit(3)

                if resp.status_code in RETRYABLE_STATUS:
                    if attempt < MAX_RETRY:
                        logging.warning(f"[RETRY] {method} 收到 {resp.status_code}，重试 {attempt+1}/{MAX_RETRY}")
                        time.sleep(1)
                        continue
                    print(f"[ERROR:5xx] {method} 服务端错误 {resp.status_code}，已重试 {MAX_RETRY} 次", file=sys.stderr)
                    sys.exit(2)

                if resp.status_code != 200:
                    print(f"[ERROR:HTTP] {method} HTTP {resp.status_code}", file=sys.stderr)
                    sys.exit(2)

                data = resp.json()

                # 业务返回码校验（兼容 baseResponse.code / result）
                base_resp = data.get("baseResponse", {})
                code = base_resp.get("code", base_resp.get("resultCode", 0))
                if code != 0:
                    msg = base_resp.get("message", base_resp.get("resultMessage", "业务错误"))
                    print(f"[ERROR:BIZ] {method} code={code} msg={msg}", file=sys.stderr)
                    sys.exit(3)

                # APP 端网关错误码（result != 0 表示失败，如 24 需内部账号登录）
                gw_result = data.get("result")
                if gw_result is not None and gw_result != 0 and gw_result != 1:
                    msg = data.get("error_msg", "网关错误")
                    print(f"[ERROR:GW] {method} result={gw_result} msg={msg}", file=sys.stderr)
                    sys.exit(3)

                return data.get("data", data)

            except requests.Timeout:
                if attempt < MAX_RETRY:
                    logging.warning(f"[TIMEOUT] {method} 超时，重试 {attempt+1}/{MAX_RETRY}")
                    time.sleep(1)
                    continue
                print(f"[ERROR:TIMEOUT] {method} 请求超时，已重试 {MAX_RETRY} 次", file=sys.stderr)
                sys.exit(2)

            except requests.ConnectionError:
                if attempt < MAX_RETRY:
                    logging.warning(f"[CONN] {method} 连接失败，重试 {attempt+1}/{MAX_RETRY}")
                    time.sleep(2)
                    continue
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

_default_client: Optional[CheckoutApiClient] = None


def get_client(pc_cookie: Optional[str] = None, app_cookie: Optional[str] = None) -> CheckoutApiClient:
    """获取全局 API 客户端（懒初始化）"""
    global _default_client
    if _default_client is None:
        _default_client = CheckoutApiClient(pc_cookie=pc_cookie, app_cookie=app_cookie)
    return _default_client


def call_api(method: str, payload: dict, step: str = "default") -> dict:
    """便捷调用函数"""
    client = get_client()
    return client.post(method, payload, step=step)
