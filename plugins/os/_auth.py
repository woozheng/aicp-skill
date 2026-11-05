"""认证插件 — 协议 v3.0 系统插件（合并版 + 多用户模式）

统一认证，供 Gateway 和 MCP 共用。

模式（auth.mode）：
  - local  : 全部放行，user_id="local"，只允许本机访问（默认，开箱即用）
  - token  : 必须带合法 token，user_id 来自 token（公网/服务器部署）
  - hybrid : 本机/同源放行（local），远程必须 token（个人项目给朋友用）

token 来源（按顺序尝试）：
  1. 静态 token（config.tokens）
  2. HMAC 自包含 token（BIO_SECRET 签发，无存储，MCP/gateway 共用）
  3. 动态 token（data/mcp_tokens.json，可吊销）
  4. 应用 token（HMAC(app_name)）

铁律：
  - user_id 只允许 "local" 或真实 uid，禁止 "anonymous"
  - mode 只控制"拦不拦请求"，不控制"数据写哪个目录"
"""
import core
import hmac
import hashlib
import json
import os
import time
import base64
from pathlib import Path


# ============================================================
# 入口
# ============================================================

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
    elif action == "mode":
        # 调试用：返回当前模式
        envelop.payload = {"ok": True, "mode": _resolve_mode(agent)}
        return envelop

    envelop.payload = {"ok": False, "error": f"Unknown action: {action}"}
    return envelop


# ============================================================
# 模式解析
# ============================================================

def _resolve_mode(agent) -> str:
    """解析认证模式：优先 auth.mode，缺省 local。"""
    auth_cfg = agent.config.get("auth") or {}
    mode = auth_cfg.get("mode")
    if mode in ("local", "token", "hybrid"):
        return mode
    return "local"


# ============================================================
# HMAC 自包含 token（方案 B）
# ============================================================

def _secret() -> str:
    """HMAC 密钥（认证业务自有，不进 core）。"""
    return os.environ.get("BIO_SECRET", "change-me-in-prod")


def issue_hmac_token(user_id: str, ttl_seconds: int = 30 * 86400, perms=None) -> str:
    """签发 HMAC 自包含 token（认证业务实现，core 不参与）。"""
    if not user_id:
        return ""
    exp = int(time.time()) + int(ttl_seconds)
    body = {"uid": str(user_id), "exp": exp}
    if isinstance(perms, dict):
        body["perms"] = perms
    raw = base64.urlsafe_b64encode(json.dumps(body, sort_keys=True).encode()).decode().rstrip("=")
    sig = hmac.new(_secret().encode(), raw.encode(), hashlib.sha256).digest()
    sig_b = base64.urlsafe_b64encode(sig).decode().rstrip("=")
    return f"{raw}.{sig_b}"


def verify_hmac_token(token: str) -> dict | None:
    """验证 HMAC 自包含 token，返回 {uid, perms?} 或 None。"""
    if not token or "." not in token:
        return None
    try:
        raw, sig_b = token.split(".", 1)
        raw_b = raw.encode()
        expect = hmac.new(_secret().encode(), raw_b, hashlib.sha256).digest()
        got = base64.urlsafe_b64decode(sig_b + "=" * (-len(sig_b) % 4))
        if not hmac.compare_digest(expect, got):
            return None
        body = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        if int(body.get("exp", 0)) < time.time():
            return None
        uid = str(body.get("uid") or "").strip()
        if not uid:
            return None
        out = {"uid": uid}
        if isinstance(body.get("perms"), dict):
            out["perms"] = body["perms"]
        return out
    except Exception:
        return None


# ============================================================
# 请求来源判定（供 gateway / MCP 复用）
# ============================================================

def is_local_request(remote: str, forwarded_for: str = "") -> bool:
    if forwarded_for:
        return False
    return remote in ("127.0.0.1", "localhost", "::1")


def is_same_origin(origin: str, host: str) -> bool:
    if not origin:
        return False
    return origin in (f"http://{host}", f"https://{host}")


# ============================================================
# 验证主逻辑
# ============================================================

async def _verify(envelop, agent):
    """统一认证验证"""
    source = envelop.meta.get("source", "gateway")
    mode = _resolve_mode(agent)

    # 来自子目录应用的请求，直接放行
    if envelop.meta.get("is_app_request"):
        envelop.payload = {"ok": True, "user_id": "local", "message": "App request allowed"}
        return envelop

    # 提取 token（放前面，local 模式也用它判断"是否带 token 模拟用户"）
    token = _extract_token(envelop)

    # ---------- local 模式 ----------
    if mode == "local":
        # 护栏：非本机直接拒绝
        remote = envelop.meta.get("remote", "")
        forwarded_for = envelop.meta.get("forwarded_for", "")
        if remote and not is_local_request(remote, forwarded_for):
            envelop.payload = {
                "ok": False,
                "error": "local mode: only 127.0.0.1 allowed; set auth.mode: token or hybrid for remote access",
            }
            return envelop

        # local 模式下带了合法 HMAC token，也尊重它（便于本地模拟多用户）
        if token:
            info = verify_hmac_token(token)
            if info:
                envelop.payload = {
                    "ok": True,
                    "user_id": info["uid"],
                    "permissions": info.get("perms", {"*": True}),
                    "source": "hmac",
                }
                return envelop

        envelop.payload = {
            "ok": True,
            "user_id": "local",
            "permissions": {"*": True},
            "message": "Auth disabled (local mode)",
        }
        return envelop

    # ---------- token / hybrid 模式 ----------

    # hybrid：本机/同源放行（有反向代理时同源判定不生效，防止 Cloudflare 隧道等场景被全网免登）
    if mode == "hybrid":
        remote = envelop.meta.get("remote", "")
        forwarded_for = envelop.meta.get("forwarded_for", "")
        origin = envelop.meta.get("origin", "")
        host = envelop.meta.get("host", "")
        same_origin_ok = (not forwarded_for) and is_same_origin(origin, host)
        if is_local_request(remote, forwarded_for) or same_origin_ok:
            # 本机带了合法 token 也尊重
            if token:
                info = verify_hmac_token(token)
                if info:
                    envelop.payload = {
                        "ok": True,
                        "user_id": info["uid"],
                        "permissions": info.get("perms", {"*": True}),
                        "source": "hmac",
                    }
                    return envelop
            envelop.payload = {
                "ok": True,
                "user_id": "local",
                "permissions": {"*": True},
                "message": "Local access allowed (hybrid mode)",
            }
            return envelop

    # Gateway 才检查公开路由；MCP 跳过
    if source != "mcp":
        route = envelop.payload.get("route", envelop.receiver)
        public_routes = {"auth/login", "ws_config", "health", "applications/gitee_manager/gitee_webhook"}
        if route in public_routes:
            envelop.payload = {"ok": True, "user_id": "local"}
            return envelop

    # 回调签名验证
    if envelop.meta.get("is_callback"):
        signature = envelop.meta.get("signature", "")
        if hasattr(core, 'verify_signature') and core.verify_signature(envelop.payload, signature):
            envelop.payload = {"ok": True, "user_id": "local", "message": "Callback signature verified"}
            return envelop

    # 1. 静态 token（config.tokens）
    valid_tokens = set(agent.config.get("tokens", []))
    if token and token in valid_tokens:
        envelop.payload = {"ok": True, "user_id": "local", "permissions": {"*": True}, "source": "static"}
        return envelop

    # 2. HMAC 自包含 token（方案 B）
    if token:
        info = verify_hmac_token(token)
        if info:
            envelop.payload = {
                "ok": True,
                "user_id": info["uid"],
                "permissions": info.get("perms", {"*": True}),
                "source": "hmac",
            }
            return envelop

    # 3. 动态 token（data/mcp_tokens.json，可吊销）
    if token:
        dynamic = _verify_dynamic_token(token)
        if dynamic:
            envelop.payload = {
                "ok": True,
                "user_id": dynamic.get("user_id", "local"),
                "permissions": dynamic.get("permissions", {"*": True}),
                "source": "dynamic",
            }
            return envelop

    # 4. 应用 token
    if token:
        secret = agent.config.get("app_secret", "aicp_default_secret")
        for app_name in _list_app_names():
            expected = _generate_app_token_value(app_name, secret)
            if hmac.compare_digest(token, expected):
                envelop.meta["app_name"] = app_name
                envelop.meta["is_app_request"] = True
                envelop.payload = {"ok": True, "user_id": "local", "app": app_name}
                return envelop

    # ★ 带诊断信息的失败返回（agent 友好）。
    #   保留 ok / error 两个键不变（兼容既有调用方），
    #   额外给 reason（机器可读，供 agent 程序化分支）、
    #   detail（人读的一句话）、hint（agent 的下一步动作）。
    #   agent 拿到 401 后要能判断"是自己没带凭证、凭证过期、还是权限不够"，
    #   否则只会反复重试同一请求。
    envelop.payload = {
        "ok": False,
        "error": "Unauthorized",
        "reason": "invalid_token" if token else "missing_token",
        "detail": (
            "token was provided but rejected by all 4 sources (static/hmac/dynamic/app)"
            if token else
            "no token provided"
        ),
        "hint": (
            "check token expiry and that BIO_SECRET matches the issuing side"
            if token else
            "send 'Authorization: Bearer <token>' or 'X-AICP-Token' header; "
            "obtain one with os/_auth issue_hmac_token"
        ),
        "auth_mode": mode,
    }
    return envelop


def _extract_token(envelop) -> str:
    token = envelop.payload.get("token", "")
    if not token:
        token = envelop.meta.get("token", "")
    if not token:
        auth_header = envelop.meta.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    if not token:
        token = envelop.meta.get("cookie_token", "")
    return token or ""


# ============================================================
# 动态 token（保留原逻辑）
# ============================================================

def _verify_dynamic_token(token: str) -> dict:
    """验证动态 token（data/mcp_tokens.json）"""
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

    info["last_used"] = time.time()
    tokens[token] = info
    try:
        tokens_file.write_text(json.dumps(tokens, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return info


# ============================================================
# 登录 / 登出 / 应用 token（保留原逻辑）
# ============================================================

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
            token = issue_hmac_token(username)
            envelop.payload = {"ok": True, "token": token, "username": username, "user_id": username}
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


def _verify_password(password: str, stored: str) -> bool:
    if stored.startswith("sha256:"):
        expected = stored[7:]
        actual = hashlib.sha256(password.encode()).hexdigest()
        return hmac.compare_digest(actual, expected)
    return False


def _list_app_names() -> list:
    www = Path("www/apps")
    if not www.exists():
        return []
    return [d.name for d in www.iterdir() if d.is_dir()]


def help():
    return {
        "route": "os/_auth",
        "description": "统一认证（gateway / MCP 共用），支持 local / token / hybrid 三档",
        "actions": {
            "verify": "验证请求",
            "login": "用户名密码登录",
            "logout": "登出",
            "generate_app_token": "生成应用 token",
            "mode": "查询当前模式",
        },
        "modes": {
            "local": "全放行，user_id=local，仅本机（默认）",
            "token": "必须 token，user_id 来自 token",
            "hybrid": "本机放行，远程必须 token",
        },
        "token_sources": ["static(config.tokens)", "hmac(BIO_SECRET)", "dynamic(data/mcp_tokens.json)", "app_token"],
    }
