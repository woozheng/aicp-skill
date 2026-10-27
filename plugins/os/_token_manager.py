"""Token 管理 — 签发 / 吊销 / 列出 / 切换认证模式

Admin 判定（A+B 混合）：
  - user_id == "local" 且 source == "local"（本机未带 token）
  - source == "static"（静态 token 等价 root）
  - token 里 perms.admin == true（远程管理员）

所有 admin 接口都从 envelop.meta 读 auth_user_id，绝不从 payload 读。
"""
import json
import time
import secrets
import hmac
import hashlib
import base64
from pathlib import Path


TOKENS_FILE = Path("data/mcp_tokens.json")


# ============================================================
# 入口
# ============================================================

async def execute(envelop, agent):
    try:
        raw = envelop.payload if isinstance(envelop.payload, dict) else {}
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
        action = payload.get("action", "mode")

        # 公开接口：任何人可查模式 / 验证自己的 token
        if action == "mode":
            return await _mode(envelop, agent)
        if action == "check":
            return await _check(envelop, agent)

        # 以下全是 admin 接口
        if not _is_admin(envelop):
            envelop.payload = {"ok": False, "error": "Admin only"}
            return envelop

        if action == "list":
            return await _list(envelop, agent)
        elif action == "issue_dynamic":
            return await _issue_dynamic(envelop, agent)
        elif action == "issue_hmac":
            return await _issue_hmac(envelop, agent)
        elif action == "revoke":
            return await _revoke(envelop, agent)
        elif action == "delete":
            return await _delete(envelop, agent)
        elif action == "extend":
            return await _extend(envelop, agent)
        elif action == "set_mode":
            return await _set_mode(envelop, agent)

        envelop.payload = {"ok": False, "error": f"Unknown action: {action}"}
        return envelop

    except Exception as e:
        envelop.payload = {"ok": False, "error": str(e)}
        return envelop


# ============================================================
# Admin 判定
# ============================================================

def _is_admin(envelop) -> bool:
    """只从 envelop.meta 读认证结果，不信 payload。"""
    user_id = envelop.meta.get("auth_user_id", "")
    source = envelop.meta.get("auth_source", "")
    perms = envelop.meta.get("auth_permissions", {}) or {}

    # 本机 local 用户
    if user_id == "local" and source in ("local", "static"):
        return True

    # token 里显式带 admin: true
    if perms.get("admin") is True:
        return True

    return False


# ============================================================
# 公开接口
# ============================================================

async def _mode(envelop, agent):
    """返回当前 auth 模式和当前身份（用于前端判断是否显示管理页）。"""
    auth_cfg = agent.config.get("auth") or {}
    mode = auth_cfg.get("mode")
    if mode not in ("local", "token", "hybrid"):
        mode = "local"

    envelop.payload = {
        "ok": True,
        "mode": mode,
        "is_admin": _is_admin(envelop),
        "user_id": envelop.meta.get("auth_user_id", ""),
        "source": envelop.meta.get("auth_source", ""),
    }
    return envelop


async def _check(envelop, agent):
    """验证一条 token，返回 uid / 权限 / 过期 / 来源。"""
    raw = envelop.payload if isinstance(envelop.payload, dict) else {}
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
    token = payload.get("token", "")

    if not token:
        envelop.payload = {"ok": False, "error": "Missing token"}
        return envelop

    # 1. 静态 token
    valid_tokens = set(agent.config.get("tokens", []))
    if token in valid_tokens:
        envelop.payload = {
            "ok": True,
            "user_id": "local",
            "source": "static",
            "permissions": {"*": True},
            "expires_at": None,
        }
        return envelop

    # 2. HMAC
    info = _verify_hmac(token, agent)
    if info:
        envelop.payload = {
            "ok": True,
            "user_id": info["uid"],
            "source": "hmac",
            "permissions": info.get("perms", {"*": True}),
            "expires_at": info.get("exp"),
        }
        return envelop

    # 3. 动态
    dynamic = _load_dynamic_token(token)
    if dynamic:
        envelop.payload = {
            "ok": True,
            "user_id": dynamic.get("user_id", ""),
            "source": "dynamic",
            "permissions": dynamic.get("permissions", {}),
            "expires_at": dynamic.get("expires_at"),
            "revoked": dynamic.get("revoked", False),
        }
        return envelop

    envelop.payload = {"ok": False, "error": "Invalid or expired token"}
    return envelop


# ============================================================
# Admin 接口
# ============================================================

async def _list(envelop, agent):
    """列出所有动态 token（不含 token 本体，只显示打码）。"""
    tokens = _load_all()
    now = time.time()
    result = []
    for tok, info in tokens.items():
        masked = tok[:8] + "..." + tok[-4:] if len(tok) > 12 else tok
        exp = info.get("expires_at")
        remaining = None
        if exp:
            try:
                remaining = int(float(exp) - now)
            except (ValueError, TypeError):
                pass
        result.append({
            "token_masked": masked,
            "token_hash": _short_hash(tok),
            "user_id": info.get("user_id", ""),
            "created_at": info.get("created_at"),
            "expires_at": exp,
            "remaining_seconds": remaining,
            "permissions": info.get("permissions", {}),
            "revoked": info.get("revoked", False),
            "last_used": info.get("last_used"),
        })
    result.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    envelop.payload = {"ok": True, "tokens": result, "total": len(result)}
    return envelop


async def _issue_dynamic(envelop, agent):
    """签发动态 token（可吊销、可查列表）。"""
    raw = envelop.payload if isinstance(envelop.payload, dict) else {}
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw

    user_id = str(payload.get("user_id", "")).strip()
    days = payload.get("days", 30)
    perms = payload.get("permissions")

    if not user_id:
        envelop.payload = {"ok": False, "error": "user_id required"}
        return envelop
    try:
        days = int(days)
        if days <= 0 or days > 3650:
            raise ValueError
    except (ValueError, TypeError):
        envelop.payload = {"ok": False, "error": "days must be 1-3650"}
        return envelop

    if perms is None:
        perms = {"*": True}
    elif not isinstance(perms, dict):
        envelop.payload = {"ok": False, "error": "permissions must be object"}
        return envelop

    token = "tok_" + secrets.token_urlsafe(24)
    now = time.time()
    info = {
        "user_id": user_id,
        "created_at": now,
        "expires_at": now + days * 86400,
        "permissions": perms,
        "revoked": False,
        "last_used": None,
    }

    tokens = _load_all()
    tokens[token] = info
    _save_all(tokens)

    envelop.payload = {
        "ok": True,
        "token": token,
        "user_id": user_id,
        "expires_at": info["expires_at"],
        "permissions": perms,
    }
    return envelop


async def _issue_hmac(envelop, agent):
    """签发 HMAC 自包含 token（无存储，关了就没了）。"""
    raw = envelop.payload if isinstance(envelop.payload, dict) else {}
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw

    user_id = str(payload.get("user_id", "")).strip()
    days = payload.get("days", 30)
    perms = payload.get("permissions")

    if not user_id:
        envelop.payload = {"ok": False, "error": "user_id required"}
        return envelop
    try:
        days = int(days)
        if days <= 0 or days > 3650:
            raise ValueError
    except (ValueError, TypeError):
        envelop.payload = {"ok": False, "error": "days must be 1-3650"}
        return envelop

    if perms is None:
        perms = {"*": True}
    elif not isinstance(perms, dict):
        envelop.payload = {"ok": False, "error": "permissions must be object"}
        return envelop

    secret = _secret(agent)
    token = _issue_hmac_token(secret, user_id, days * 86400, perms)

    envelop.payload = {
        "ok": True,
        "token": token,
        "user_id": user_id,
        "expires_at": time.time() + days * 86400,
        "permissions": perms,
        "note": "HMAC token 无存储，请立即复制保存，关闭后无法找回",
    }
    return envelop


async def _revoke(envelop, agent):
    """吊销动态 token（标记 revoked，不删除记录）。"""
    raw = envelop.payload if isinstance(envelop.payload, dict) else {}
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
    token_hash = payload.get("token_hash", "")

    if not token_hash:
        envelop.payload = {"ok": False, "error": "token_hash required"}
        return envelop

    tokens = _load_all()
    found = None
    for tok, info in tokens.items():
        if _short_hash(tok) == token_hash:
            found = tok
            break

    if not found:
        envelop.payload = {"ok": False, "error": "Token not found"}
        return envelop

    tokens[found]["revoked"] = True
    tokens[found]["revoked_at"] = time.time()
    _save_all(tokens)

    envelop.payload = {"ok": True, "user_id": tokens[found].get("user_id")}
    return envelop


async def _delete(envelop, agent):
    """彻底删除动态 token（从文件里移除）。"""
    raw = envelop.payload if isinstance(envelop.payload, dict) else {}
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
    token_hash = payload.get("token_hash", "")

    if not token_hash:
        envelop.payload = {"ok": False, "error": "token_hash required"}
        return envelop

    tokens = _load_all()
    found = None
    for tok, info in tokens.items():
        if _short_hash(tok) == token_hash:
            found = tok
            break

    if not found:
        envelop.payload = {"ok": False, "error": "Token not found"}
        return envelop

    user_id = tokens[found].get("user_id")
    del tokens[found]
    _save_all(tokens)

    envelop.payload = {"ok": True, "user_id": user_id}
    return envelop


async def _extend(envelop, agent):
    """延长有效期（days 可以是负数表示缩短）。"""
    raw = envelop.payload if isinstance(envelop.payload, dict) else {}
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
    token_hash = payload.get("token_hash", "")
    days = payload.get("days", 30)

    if not token_hash:
        envelop.payload = {"ok": False, "error": "token_hash required"}
        return envelop
    try:
        days = int(days)
    except (ValueError, TypeError):
        envelop.payload = {"ok": False, "error": "days must be integer"}
        return envelop

    tokens = _load_all()
    found = None
    for tok, info in tokens.items():
        if _short_hash(tok) == token_hash:
            found = tok
            break

    if not found:
        envelop.payload = {"ok": False, "error": "Token not found"}
        return envelop

    now = time.time()
    current = tokens[found].get("expires_at") or now
    try:
        current = float(current)
    except (ValueError, TypeError):
        current = now
    new_exp = max(now + 86400, current + days * 86400)
    tokens[found]["expires_at"] = new_exp
    tokens[found]["revoked"] = False  # 延长默认恢复
    _save_all(tokens)

    envelop.payload = {
        "ok": True,
        "user_id": tokens[found].get("user_id"),
        "expires_at": new_exp,
    }
    return envelop


async def _set_mode(envelop, agent):
    """切换 auth 模式（写入 aicp.yaml 或 aicp.local.yaml）。"""
    raw = envelop.payload if isinstance(envelop.payload, dict) else {}
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
    mode = payload.get("mode", "")

    if mode not in ("local", "token", "hybrid"):
        envelop.payload = {"ok": False, "error": "mode must be local/token/hybrid"}
        return envelop

    # 调用 _settings 的 set 接口写入配置
    env = type("E", (), {})()
    env.sender = "os/_token_manager"
    env.receiver = "builtins/control/_settings"
    env.intent = "API_CALL"
    env.payload = {"action": "set", "key": "auth.mode", "value": mode}
    env.meta = {}
    env.status = 200
    env.channel_id = None
    env.ttl = 30

    result = await agent.system.call(env)
    if hasattr(result, "payload") and isinstance(result.payload, dict):
        envelop.payload = result.payload
    else:
        envelop.payload = {"ok": False, "error": "Failed to set mode"}
    return envelop


# ============================================================
# 工具函数
# ============================================================

def _load_all() -> dict:
    if not TOKENS_FILE.exists():
        return {}
    try:
        return json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_all(tokens: dict):
    TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKENS_FILE.write_text(json.dumps(tokens, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_dynamic_token(token: str) -> dict | None:
    tokens = _load_all()
    info = tokens.get(token)
    if not info:
        return None
    exp = info.get("expires_at")
    if exp:
        try:
            if time.time() > float(exp):
                return None
        except (ValueError, TypeError):
            pass
    return info


def _short_hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def _secret(agent) -> str:
    import os
    return os.environ.get("BIO_SECRET", agent.config.get("bio_secret", "change-me-in-prod"))


def _issue_hmac_token(secret: str, user_id: str, ttl_seconds: int, perms=None) -> str:
    body = {"uid": user_id, "exp": int(time.time()) + ttl_seconds}
    if perms:
        body["perms"] = perms
    raw = base64.urlsafe_b64encode(
        json.dumps(body, separators=(",", ":")).encode()
    ).rstrip(b"=")
    sig = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), raw, hashlib.sha256).digest()
    ).rstrip(b"=")
    return f"{raw.decode()}.{sig.decode()}"


def _verify_hmac(token: str, agent) -> dict | None:
    if not token or "." not in token:
        return None
    try:
        raw, sig_b = token.split(".", 1)
        raw_b = raw.encode()
        secret = _secret(agent).encode()
        expect = hmac.new(secret, raw_b, hashlib.sha256).digest()
        got = base64.urlsafe_b64decode(sig_b + "=" * (-len(sig_b) % 4))
        if not hmac.compare_digest(expect, got):
            return None
        body = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        if int(body.get("exp", 0)) < time.time():
            return None
        uid = str(body.get("uid") or "").strip()
        if not uid:
            return None
        out = {"uid": uid, "exp": body.get("exp")}
        if isinstance(body.get("perms"), dict):
            out["perms"] = body["perms"]
        return out
    except Exception:
        return None


def help():
    return {
        "route": "os/_token_manager",
        "description": "Token 签发与管理（admin 接口受 auth_user_id 保护）",
        "actions": {
            "mode": "查当前模式 + 是否 admin（公开）",
            "check": "验证一条 token（公开）",
            "list": "列出所有动态 token（admin）",
            "issue_dynamic": "签发动态 token（admin）",
            "issue_hmac": "签发 HMAC token（admin）",
            "revoke": "吊销动态 token（admin）",
            "delete": "删除动态 token（admin）",
            "extend": "延长有效期（admin）",
            "set_mode": "切换 auth 模式（admin）",
        },
    }
