"""认证插件 — 协议 v3.0 系统插件（合并版）

统一认证，供 Gateway 和 MCP 共用。

- enable_auth: false → 全部放行
- enable_auth: true  → 验证 token / app token
- source: gateway → 检查公开路由
- source: mcp     → 跳过公开路由，只验 token
"""
import core
import hmac
import hashlib
import secrets
import json


async def execute(envelop, agent):
    action = envelop.payload.get("action", "verify")

    if action == "verify":
        return await _verify(envelop, agent)
    elif action == "login":
        return await _login(envelop, agent)
    elif action == "logout":
        return await _logout(envelop, agent)
    elif action == "generate_app_token":
        return await _generate_app_token(envelop, agent)

    envelop.payload = {"ok": False, "error": f"Unknown action: {action}"}
    return envelop


async def _verify(envelop, agent):
    """统一认证验证"""
    source = envelop.meta.get("source", "gateway")

    # 来自子目录应用的请求，直接放行
    if envelop.meta.get("is_app_request"):
        envelop.payload = {"ok": True, "message": "App request allowed"}
        return envelop

    # 读取配置
    enable_auth = agent.config.get("enable_auth", False)
    if not enable_auth:
        envelop.payload = {"ok": True, "message": "Auth disabled"}
        return envelop

    # Gateway 才检查公开路由；MCP 跳过
    if source != "mcp":
        route = envelop.payload.get("route", envelop.receiver)
        public_routes = {"auth/login", "ws_config", "health", "applications/gitee_manager/gitee_webhook"}
        if route in public_routes:
            envelop.payload = {"ok": True}
            return envelop

    # 回调签名验证
    if envelop.meta.get("is_callback"):
        signature = envelop.meta.get("signature", "")
        if hasattr(core, 'verify_signature') and core.verify_signature(envelop.payload, signature):
            envelop.payload = {"ok": True, "message": "Callback signature verified"}
            return envelop

    # 提取 token
    token = envelop.payload.get("token", "")
    if not token:
        token = envelop.meta.get("token", "")
    if not token:
        auth_header = envelop.meta.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    if not token:
        token = envelop.meta.get("cookie_token", "")

    # 1. 验证静态 token
    valid_tokens = set(agent.config.get("tokens", []))
    if token and token in valid_tokens:
        envelop.payload = {"ok": True, "user_id": "static-user", "permissions": {"*": True}}
        return envelop

    # 2. 验证动态 token（data/mcp_tokens.json）
    if token:
        dynamic = _verify_dynamic_token(token)
        if dynamic:
            envelop.payload = {
                "ok": True,
                "user_id": dynamic.get("user_id", "mcp-user"),
                "permissions": dynamic.get("permissions", {"*": True}),
                "source": "dynamic",
            }
            return envelop

    # 3. 验证应用 token
    if token:
        secret = agent.config.get("app_secret", "aicp_default_secret")
        for app_name in _list_app_names():
            expected = _generate_app_token_value(app_name, secret)
            if hmac.compare_digest(token, expected):
                envelop.meta["app_name"] = app_name
                envelop.meta["is_app_request"] = True
                envelop.payload = {"ok": True, "app": app_name}
                return envelop

    envelop.payload = {"ok": False, "error": "Unauthorized"}
    return envelop


def _verify_dynamic_token(token: str) -> dict:
    """验证动态 token（data/mcp_tokens.json）"""
    from pathlib import Path
    import time
    tokens_file = Path("data/mcp_tokens.json")
    if not tokens_file.exists():
        return None
    try:
        tokens = json.loads(tokens_file.read_text(encoding="utf-8"))
    except Exception:
        return None

    info = tokens.get(token)
    if not info:
        return None
    if info.get("revoked"):
        return None
    expires_at = info.get("expires_at")
    if expires_at:
        try:
            if time.time() > float(expires_at):
                return None
        except (ValueError, TypeError):
            pass

    # 更新 last_used
    info["last_used"] = time.time()
    tokens[token] = info
    try:
        tokens_file.write_text(json.dumps(tokens, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return info


async def _login(envelop, agent):
    username = envelop.payload.get("username", "")
    password = envelop.payload.get("password", "")

    if not username or not password:
        envelop.payload = {"ok": False, "error": "Missing username or password"}
        return envelop

    users = agent.config.get("users", {})
    if username in users:
        stored = users[username]
        if stored == password or _verify_password(password, stored):
            token = _generate_session_token(username, agent.config.get("token_secret", "aicp_session_secret"))
            envelop.payload = {"ok": True, "token": token, "username": username}
        else:
            envelop.payload = {"ok": False, "error": "Invalid credentials"}
    else:
        envelop.payload = {"ok": False, "error": "Invalid credentials"}

    return envelop


async def _logout(envelop, agent):
    envelop.payload = {"ok": True, "message": "Logged out"}
    return envelop


async def _generate_app_token(envelop, agent):
    app_name = envelop.payload.get("app_name", "")
    if not app_name:
        envelop.payload = {"ok": False, "error": "Missing app_name"}
        return envelop

    secret = agent.config.get("app_secret", "aicp_default_secret")
    token = _generate_app_token_value(app_name, secret)

    envelop.payload = {"ok": True, "app_name": app_name, "token": token}
    return envelop


# ============================================================
# 工具函数
# ============================================================

def _generate_app_token_value(app_name: str, secret: str) -> str:
    return hmac.new(secret.encode(), app_name.encode(), hashlib.md5).hexdigest()[:16]


def _generate_session_token(username: str, secret: str) -> str:
    nonce = secrets.token_hex(8)
    data = f"{username}:{nonce}"
    signature = hmac.new(secret.encode(), data.encode(), hashlib.sha256).hexdigest()[:32]
    return f"tok_{username}_{signature}"


def _verify_password(password: str, stored: str) -> bool:
    if stored.startswith("sha256:"):
        expected = stored[7:]
        actual = hashlib.sha256(password.encode()).hexdigest()
        return hmac.compare_digest(actual, expected)
    return False


def _list_app_names() -> list:
    from pathlib import Path
    www = Path("www")
    if not www.exists():
        return []
    return [d.name for d in www.iterdir() if d.is_dir()]
