"""fetch — 通用 HTTP 请求工具（内置基础工具）

支持 GET/POST/PUT/DELETE/PATCH/HEAD/OPTIONS 等方法，
可自定义 headers、body、超时、代理、SSL 验证等。
返回状态码、响应头、响应体（文本/JSON）。
"""

import asyncio
import json

import requests


# ============================================================
# 核心请求逻辑
# ============================================================

def _do_request(url, method, headers, body, json_body, params,
                timeout, proxy, verify_ssl, allow_redirects):
    proxies = None
    if proxy:
        proxies = {"http": proxy, "https": proxy}

    request_kwargs = {
        "headers": headers or {},
        "params": params or None,
        "timeout": timeout,
        "proxies": proxies,
        "verify": verify_ssl,
        "allow_redirects": allow_redirects,
    }

    if json_body is not None:
        request_kwargs["json"] = json_body
        if "Content-Type" not in request_kwargs["headers"] \
                and "content-type" not in request_kwargs["headers"]:
            request_kwargs["headers"]["Content-Type"] = "application/json"
    elif body is not None:
        request_kwargs["data"] = body

    resp = requests.request(method, url, **request_kwargs)

    resp_headers = dict(resp.headers)
    text = resp.text

    json_data = None
    content_type = resp.headers.get("Content-Type", "").lower()
    if "application/json" in content_type \
            or text.strip().startswith(("{", "[")):
        try:
            json_data = resp.json()
        except (ValueError, json.JSONDecodeError):
            json_data = None

    return {
        "status_code": resp.status_code,
        "reason": resp.reason,
        "url": resp.url,
        "headers": resp_headers,
        "body": text,
        "json": json_data,
        "encoding": resp.encoding,
        "elapsed_ms": round(resp.elapsed.total_seconds() * 1000, 2),
        "content_length": len(resp.content),
    }


# ============================================================
# 主入口
# ============================================================

async def execute(envelop, agent):
    """发送 HTTP 请求"""
    # ★★★ 兼容两种传参方式 ★★★
    params = envelop.payload.get("params", {})
    if not params:
        params = {k: v for k, v in envelop.payload.items() if k != "action"}

    action = envelop.payload.get("action", "request")

    if action == "request":
        url = params.get("url")
        if not url:
            envelop.payload = {"ok": False, "error": "缺少 url 参数"}
            return envelop

        method = params.get("method", "GET").upper()
        valid_methods = {"GET", "POST", "PUT", "DELETE", "PATCH",
                         "HEAD", "OPTIONS"}
        if method not in valid_methods:
            envelop.payload = {"ok": False,
                               "error": f"不支持的 HTTP 方法: {method}"}
            return envelop

        headers = params.get("headers")
        body = params.get("body")
        json_body = params.get("json_body")
        query_params = params.get("params")
        timeout = params.get("timeout", 30)
        proxy = params.get("proxy")
        verify_ssl = params.get("verify_ssl", True)
        allow_redirects = params.get("allow_redirects", True)

        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: _do_request(
                    url, method, headers, body, json_body,
                    query_params, timeout, proxy,
                    verify_ssl, allow_redirects
                )
            )
            envelop.payload = {"ok": True, "data": result}
            return envelop
        except requests.exceptions.Timeout:
            envelop.payload = {"ok": False,
                               "error": f"请求超时（{timeout}s）: {url}"}
            return envelop
        except requests.exceptions.ConnectionError as e:
            envelop.payload = {"ok": False, "error": f"连接失败: {e}"}
            return envelop
        except requests.exceptions.ProxyError as e:
            envelop.payload = {"ok": False, "error": f"代理错误: {e}"}
            return envelop
        except requests.exceptions.RequestException as e:
            envelop.payload = {"ok": False, "error": f"请求失败: {e}"}
            return envelop
        except Exception as e:
            envelop.payload = {"ok": False, "error": f"未知错误: {e}"}
            return envelop

    else:
        envelop.payload = {"ok": False, "error": f"未知操作: {action}"}
        return envelop


def help():
    return {
        "route": "builtins/tools/fetch",
        "description": "通用 HTTP 请求工具，支持 GET/POST/PUT/DELETE 等方法",
        "actions": {
            "request": {
                "description": "发送 HTTP 请求",
                "input": {
                    "url": "目标 URL（必填）",
                    "method": "HTTP 方法，默认 GET",
                    "headers": "请求头字典",
                    "body": "请求体文本",
                    "json_body": "JSON 请求体（自动设 Content-Type）",
                    "params": "URL 查询参数字典",
                    "timeout": "超时秒数，默认 30",
                    "proxy": "代理地址，如 http://127.0.0.1:8080",
                    "verify_ssl": "是否验证 SSL，默认 true",
                    "allow_redirects": "是否允许重定向，默认 true",
                },
                "output": {
                    "ok": "是否成功",
                    "data": {
                        "status_code": "HTTP 状态码",
                        "reason": "状态描述",
                        "url": "最终 URL",
                        "headers": "响应头字典",
                        "body": "响应体文本",
                        "json": "解析后的 JSON（失败则为 null）",
                        "encoding": "编码",
                        "elapsed_ms": "耗时（毫秒）",
                        "content_length": "响应体字节数",
                    }
                }
            }
        }
    }