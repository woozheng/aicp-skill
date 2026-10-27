"""os/mcp_oauth — 通用 MCP OAuth 登录器

把任意支持 OAuth 2.1 的 MCP Server 接进 AICP 体内，token 直接落到
data/os/_mcp_client/servers.json，由 os/_mcp_client 消费。

标准实现：
  RFC 8414  授权服务器元数据 /.well-known/oauth-authorization-server
  RFC 9728  受保护资源元数据 /.well-known/oauth-protected-resource
  RFC 7591  动态客户端注册 DCR
  RFC 7636  PKCE (S256)
  RFC 8252  公共客户端（token_endpoint_auth_method=none）

actions:
  discover   探测 MCP Server / issuer 的授权元数据（不登录）
  configure  预登记 server（issuer / mcp_url / scope / client_id …）
  start      发起授权，返回 authorize_url（浏览器打开）
  callback   处理授权回调 → 换 token → 落盘
  status     查看登录状态（不传 server = 全部）
  refresh    用 refresh_token 换新 token
  logout     清除 token
  list       列出已登记的 server 及授权元数据

数据落盘：
  data/os/mcp_oauth/servers.json   每个 server 的授权配置
  data/os/mcp_oauth/state.json      进行中的授权流程（state / verifier）
  data/os/_mcp_client/servers.json  最终 token（os/_mcp_client 消费）
"""

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
from pathlib import Path

import aiohttp

DATA_DIR = Path("data/os/mcp_oauth")
CONFIG_FILE = DATA_DIR / "servers.json"
STATE_FILE = DATA_DIR / "state.json"
MCP_SERVERS_FILE = Path("data/os/_mcp_client/servers.json")

DEFAULT_CLIENT_NAME = "AICP BIO-1"
HTTP_TIMEOUT = 20

ACTIONS = [
    "discover", "configure", "start", "callback",
    "status", "refresh", "logout", "list",
]

# discovery 失败时的端点回退候选
AUTH_CANDIDATES = ["/authorize", "/oauth/authorize", "/oauth2/authorize"]
TOKEN_CANDIDATES = ["/token", "/oauth/token", "/oauth2/token"]
REG_CANDIDATES = ["/register", "/oauth/register", "/client/register", "/oauth2/register"]

CONFIG_KEYS = (
    "issuer", "mcp_url", "scope", "client_name", "client_id",
    "redirect_uri", "authorization_endpoint", "token_endpoint",
    "registration_endpoint",
)


# ============================================================
# 落盘
# ============================================================

def _load_json(path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _configs():
    return _load_json(CONFIG_FILE, {})


def _save_configs(cfg):
    _save_json(CONFIG_FILE, cfg)


def _states():
    return _load_json(STATE_FILE, {})


def _save_states(st):
    _save_json(STATE_FILE, st)


def _mcp_servers():
    return _load_json(MCP_SERVERS_FILE, {})


def _save_mcp_servers(data):
    _save_json(MCP_SERVERS_FILE, data)


def _base_url(agent):
    base = str(getattr(agent, "base_url", "") or "").rstrip("/")
    if not base:
        base = str(os.environ.get("AICP_BASE_URL", "") or "").rstrip("/")
    return base


def _pkce():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


# ============================================================
# HTTP / discovery
# ============================================================

async def _get_json(session, url, timeout=HTTP_TIMEOUT):
    try:
        async with session.get(url, timeout=timeout,
                              headers={"Accept": "application/json"}) as r:
            txt = await r.text()
            if r.status >= 300:
                return None
            try:
                return json.loads(txt)
            except Exception:
                return None
    except Exception:
        return None


async def _probe_path(session, base, candidates, timeout=8):
    """探测端点是否存在：只有 404 判定为不存在（400/405/302 都算存在）"""
    base = (base or "").rstrip("/")
    for c in candidates:
        url = base + c
        try:
            async with session.get(url, timeout=timeout, allow_redirects=False) as r:
                if r.status != 404:
                    return url
        except Exception:
            continue
    return None


async def _discover_pr(session, mcp_url):
    """RFC 9728 受保护资源元数据"""
    u = urllib.parse.urlsplit(mcp_url or "")
    if not u.scheme or not u.netloc:
        return None, None
    base = u.scheme + "://" + u.netloc
    path = (u.path or "").rstrip("/")
    urls = []
    if path:
        urls.append(base + "/.well-known/oauth-protected-resource" + path)
    urls.append(base + "/.well-known/oauth-protected-resource")
    for url in urls:
        data = await _get_json(session, url)
        if isinstance(data, dict) and data:
            return data, url
    return None, None


async def _discover_as(session, issuer):
    """RFC 8414 / OIDC 授权服务器元数据"""
    issuer = (issuer or "").rstrip("/")
    if not issuer:
        return None, None
    u = urllib.parse.urlsplit(issuer)
    urls = []
    if u.path and u.path != "/":
        urls.append(u.scheme + "://" + u.netloc
                    + "/.well-known/oauth-authorization-server" + u.path)
    urls.append(issuer + "/.well-known/oauth-authorization-server")
    urls.append(issuer + "/.well-known/openid-configuration")
    for url in urls:
        data = await _get_json(session, url)
        if isinstance(data, dict) and data:
            return data, url
    return None, None


async def _discover(session, issuer=None, mcp_url=None):
    out = {"issuer": issuer or "", "mcp_url": mcp_url or "", "sources": []}
    as_meta = None

    if mcp_url:
        pr, pr_url = await _discover_pr(session, mcp_url)
        if pr:
            out["sources"].append(pr_url)
            out["protected_resource"] = pr
            servers = pr.get("authorization_servers") or []
            if servers:
                out["issuer"] = out["issuer"] or servers[0]
                if len(servers) > 1:
                    out["authorization_servers"] = servers

    issuer = out["issuer"]
    if issuer:
        as_meta, as_url = await _discover_as(session, issuer)
        if as_url:
            out["sources"].append(as_url)
            out["authorization_server_metadata"] = as_meta

    if as_meta:
        out["authorization_endpoint"] = as_meta.get("authorization_endpoint")
        out["token_endpoint"] = as_meta.get("token_endpoint")
        out["registration_endpoint"] = as_meta.get("registration_endpoint")
        out["scopes_supported"] = as_meta.get("scopes_supported") or []
        out["code_challenge_methods_supported"] = (
            as_meta.get("code_challenge_methods_supported") or [])

    if issuer:
        if not out.get("authorization_endpoint"):
            ep = await _probe_path(session, issuer, AUTH_CANDIDATES)
            if ep:
                out["authorization_endpoint"] = ep
                out["sources"].append("probe:" + ep)
        if not out.get("token_endpoint"):
            ep = await _probe_path(session, issuer, TOKEN_CANDIDATES)
            if ep:
                out["token_endpoint"] = ep
                out["sources"].append("probe:" + ep)
        if not out.get("registration_endpoint"):
            ep = await _probe_path(session, issuer, REG_CANDIDATES)
            if ep:
                out["registration_endpoint"] = ep
                out["sources"].append("probe:" + ep)

    out["ok"] = bool(out.get("authorization_endpoint") and out.get("token_endpoint"))
    return out


# ============================================================
# DCR / token
# ============================================================

async def _register_client(session, cfg, redirect_uri, scope=None):
    issuer = (cfg.get("issuer") or "").rstrip("/")
    candidates = []
    if cfg.get("registration_endpoint"):
        candidates.append(cfg["registration_endpoint"])
    for c in REG_CANDIDATES:
        candidates.append(issuer + c)

    body = {
        "client_name": cfg.get("client_name") or DEFAULT_CLIENT_NAME,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    if scope:
        body["scope"] = scope

    last = ""
    for ep in candidates:
        try:
            async with session.post(ep, json=body, timeout=HTTP_TIMEOUT,
                                    headers={"Accept": "application/json"}) as r:
                txt = await r.text()
                if r.status >= 300:
                    last = "%s -> %s: %s" % (ep, r.status, txt[:300])
                    continue
                try:
                    reg = json.loads(txt)
                except Exception:
                    last = "%s: bad json %s" % (ep, txt[:200])
                    continue
                cid = reg.get("client_id")
                if cid:
                    cfg["client_id"] = cid
                    cfg["registration_endpoint"] = ep
                    if reg.get("client_id_issued_at"):
                        cfg["client_id_issued_at"] = reg["client_id_issued_at"]
                    return cfg, ""
                last = "%s: no client_id in %s" % (ep, txt[:200])
        except Exception as e:
            last = "%s: %s" % (ep, e)
    return None, last or "no registration endpoint"


async def _post_form(session, candidates, form):
    headers = {"Accept": "application/json"}
    last = ""
    for ep in candidates:
        try:
            async with session.post(ep, data=form, headers=headers, timeout=25) as r:
                txt = await r.text()
                if r.status >= 300:
                    last = "%s -> %s: %s" % (ep, r.status, txt[:300])
                    if r.status in (404, 405):
                        continue
                    return None, last
                try:
                    return json.loads(txt), ""
                except Exception:
                    return None, "%s: bad json %s" % (ep, txt[:200])
        except Exception as e:
            last = "%s: %s" % (ep, e)
    return None, last or "no token endpoint"


def _token_candidates(cfg, entry=None):
    out = []
    for src in (cfg or {}, entry or {}):
        ep = src.get("token_endpoint")
        if ep and ep not in out:
            out.append(ep)
    issuer = ((cfg or {}).get("issuer") or (entry or {}).get("issuer") or "").rstrip("/")
    if issuer:
        for c in TOKEN_CANDIDATES:
            u = issuer + c
            if u not in out:
                out.append(u)
    return out


def _write_token(server, cfg, tok, force_url=False):
    """把 token 合并进 os/_mcp_client/servers.json（不覆盖已有 url）"""
    data = _mcp_servers()
    cur = data.get(server) if isinstance(data.get(server), dict) else {}
    new = dict(cur)

    mcp_url = (cfg.get("mcp_url") or "").strip()
    if not mcp_url and cfg.get("issuer"):
        mcp_url = cfg["issuer"].rstrip("/") + "/mcp"
    if mcp_url and (not new.get("url") or force_url):
        new["url"] = mcp_url

    new["token"] = tok.get("access_token") or ""
    new["enabled"] = True
    new.setdefault("ttl", 3600)
    new.setdefault("timeout", 60)
    if tok.get("refresh_token"):
        new["refresh_token"] = tok["refresh_token"]
    if tok.get("expires_in"):
        try:
            new["expires_in"] = int(tok["expires_in"])
            new["expires_at"] = time.time() + int(tok["expires_in"])
        except Exception:
            pass
    if tok.get("scope"):
        new["scope"] = tok["scope"]
    new["token_type"] = tok.get("token_type") or "Bearer"
    new["updated_at"] = time.time()

    data[server] = new
    _save_mcp_servers(data)
    return new


# ============================================================
# actions
# ============================================================

async def _action_configure(payload, agent):
    server = (payload.get("server") or payload.get("name") or "").strip()
    if not server:
        return {"ok": False, "error": "缺少 server（要登记的 MCP Server 名字）"}
    configs = _configs()
    cfg = dict(configs.get(server) or {})
    for k in CONFIG_KEYS:
        v = payload.get(k)
        if v is not None and v != "":
            cfg[k] = v
    if isinstance(payload.get("scopes_supported"), list):
        cfg["scopes_supported"] = payload["scopes_supported"]
    cfg["server"] = server
    cfg["updated_at"] = time.time()
    configs[server] = cfg
    _save_configs(configs)
    return {"ok": True, "server": server, "config": cfg}


async def _action_discover(payload, agent):
    server = (payload.get("server") or payload.get("name") or "").strip()
    configs = _configs()
    cfg = dict(configs.get(server) or {}) if server else {}
    issuer = payload.get("issuer") or cfg.get("issuer") or ""
    mcp_url = payload.get("mcp_url") or cfg.get("mcp_url") or ""
    if not issuer and not mcp_url:
        return {"ok": False, "error": "需要 issuer 或 mcp_url"}
    async with aiohttp.ClientSession() as sess:
        disc = await _discover(sess, issuer, mcp_url)
    if server:
        for k in ("issuer", "authorization_endpoint", "token_endpoint",
                  "registration_endpoint", "scopes_supported"):
            if disc.get(k):
                cfg[k] = disc[k]
        cfg["discovered_at"] = time.time()
        cfg["discovery_sources"] = disc.get("sources") or []
        configs[server] = cfg
        _save_configs(configs)
    return {"ok": True, "server": server, "discovery": disc}


async def _action_start(payload, agent):
    server = (payload.get("server") or payload.get("name") or "").strip()
    if not server:
        return {"ok": False, "error": "缺少 server（要登录的 MCP Server 名字）"}

    configs = _configs()
    cfg = dict(configs.get(server) or {})
    for k in CONFIG_KEYS:
        v = payload.get(k)
        if v is not None and v != "":
            cfg[k] = v
    cfg.setdefault("client_name", DEFAULT_CLIENT_NAME)
    cfg["server"] = server

    if not cfg.get("issuer") and not cfg.get("mcp_url"):
        return {"ok": False, "error": "缺少 issuer 或 mcp_url（先 discover 或 configure）"}

    base = _base_url(agent)
    registered = cfg.get("redirect_uri") or ""
    redirect = (payload.get("redirect_uri") or registered
                or (base + "/api/os/mcp_oauth" if base else ""))
    if not redirect:
        return {"ok": False, "error": "无法确定 redirect_uri，请显式传 redirect_uri"}

    needs_register = not cfg.get("client_id") or (registered and registered != redirect)

    async with aiohttp.ClientSession() as sess:
        if needs_register or not cfg.get("authorization_endpoint"):
            disc = await _discover(sess, cfg.get("issuer"), cfg.get("mcp_url"))
            if disc.get("issuer"):
                cfg["issuer"] = cfg.get("issuer") or disc["issuer"]
            for k in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
                if not cfg.get(k) and disc.get(k):
                    cfg[k] = disc[k]
            if disc.get("scopes_supported"):
                cfg["scopes_supported"] = disc["scopes_supported"]
            cfg["discovery_sources"] = disc.get("sources") or []
            cfg["discovered_at"] = time.time()

        if needs_register:
            if not cfg.get("registration_endpoint"):
                return {"ok": False,
                        "error": "该授权服务器未提供 DCR 注册端点，请手工传 client_id"}
            cfg, err = await _register_client(sess, cfg, redirect, cfg.get("scope"))
            if cfg is None:
                return {"ok": False, "error": "动态注册失败: " + err}
            cfg["redirect_uri"] = redirect

    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    scope = (payload.get("scope") or cfg.get("scope")
             or " ".join(cfg.get("scopes_supported") or []) or "")

    auth_ep = cfg.get("authorization_endpoint")
    if not auth_ep:
        return {"ok": False, "error": "未找到 authorization_endpoint"}

    q = {
        "response_type": "code",
        "client_id": cfg.get("client_id") or "",
        "redirect_uri": redirect,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if scope:
        q["scope"] = scope
    url = auth_ep + ("&" if "?" in auth_ep else "?") + urllib.parse.urlencode(q)

    if payload.get("update_url"):
        cfg["_force_url"] = True

    states = _states()
    states[server] = {
        "state": state,
        "verifier": verifier,
        "redirect_uri": redirect,
        "client_id": cfg.get("client_id"),
        "authorization_endpoint": auth_ep,
        "token_endpoint": cfg.get("token_endpoint"),
        "issuer": cfg.get("issuer"),
        "scope": scope,
        "started_at": time.time(),
        "authorize_url": url,
    }
    _save_states(states)
    configs[server] = cfg
    _save_configs(configs)

    return {
        "ok": True,
        "server": server,
        "authorize_url": url,
        "redirect_uri": redirect,
        "callback_url": redirect,
        "client_id": cfg.get("client_id"),
        "issuer": cfg.get("issuer"),
        "mcp_url": cfg.get("mcp_url"),
        "scope": scope,
        "scopes_supported": cfg.get("scopes_supported") or [],
        "next": "在浏览器打开 authorize_url 完成授权；回调会自动落到 callback_url",
    }


async def _action_callback(payload, agent):
    err = payload.get("error")
    if err:
        return {"ok": False,
                "error": "%s %s" % (err, payload.get("error_description") or "")}

    code = payload.get("code")
    state = payload.get("state")
    if not code:
        return {"ok": False, "error": "missing authorization code"}

    states = _states()
    server = (payload.get("server") or "").strip()
    entry = None
    if server and (states.get(server) or {}).get("state") == state:
        entry = states.get(server)
    else:
        for name, st in states.items():
            if st.get("state") and st.get("state") == state:
                entry = st
                server = name
                break
    if entry is None:
        return {"ok": False, "error": "state 不匹配或已过期，请重新 start"}
    if not entry.get("verifier"):
        return {"ok": False, "error": "授权流程已失效，请重新 start"}

    configs = _configs()
    cfg = dict(configs.get(server) or {})
    cfg.setdefault("client_id", entry.get("client_id"))
    cfg.setdefault("issuer", entry.get("issuer"))

    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": entry.get("redirect_uri") or "",
        "client_id": cfg.get("client_id") or entry.get("client_id") or "",
        "code_verifier": entry.get("verifier") or "",
    }
    async with aiohttp.ClientSession() as sess:
        tok, err = await _post_form(sess, _token_candidates(cfg, entry), form)
    if tok is None:
        return {"ok": False, "error": "换取 token 失败: " + err}
    if not tok.get("access_token"):
        return {"ok": False, "error": "响应里没有 access_token"}

    force = bool(cfg.pop("_force_url", False))
    saved = _write_token(server, cfg, tok, force_url=force)

    cfg["signed_in_at"] = time.time()
    cfg["redirect_uri"] = entry.get("redirect_uri") or cfg.get("redirect_uri")
    if cfg.get("mcp_url") is None and saved.get("url"):
        cfg["mcp_url"] = saved["url"]
    configs[server] = cfg
    _save_configs(configs)

    states.pop(server, None)
    _save_states(states)

    return {
        "ok": True,
        "server": server,
        "saved_to": str(MCP_SERVERS_FILE),
        "mcp_url": saved.get("url"),
        "scope": tok.get("scope") or entry.get("scope"),
        "expires_in": tok.get("expires_in"),
        "has_refresh_token": bool(saved.get("refresh_token")),
        "next": "可以调用 os/_mcp_client 的 connect 拉取工具列表",
    }


async def _action_status(payload, agent):
    server = (payload.get("server") or "").strip()
    configs = _configs()
    states = _states()
    data = _mcp_servers()
    names = [server] if server else sorted(set(list(configs.keys()) + list(data.keys())))
    now = time.time()
    out = []
    for n in names:
        cfg = configs.get(n) or {}
        cur = data.get(n) or {}
        exp = cur.get("expires_at")
        out.append({
            "server": n,
            "issuer": cfg.get("issuer") or "",
            "mcp_url": cfg.get("mcp_url") or cur.get("url") or "",
            "client_id": cfg.get("client_id") or "",
            "signed_in": bool(cur.get("token")),
            "has_refresh_token": bool(cur.get("refresh_token")),
            "expires_at": exp,
            "expires_in_remaining": (int(exp - now) if exp else None),
            "expired": bool(exp and exp <= now),
            "scope": cur.get("scope") or cfg.get("scope") or "",
            "pending_authorization": bool((states.get(n) or {}).get("state")),
            "endpoints": {
                "authorization_endpoint": cfg.get("authorization_endpoint") or "",
                "token_endpoint": cfg.get("token_endpoint") or "",
                "registration_endpoint": cfg.get("registration_endpoint") or "",
            },
        })
    return {"ok": True, "data": out, "total": len(out)}


async def _action_refresh(payload, agent):
    server = (payload.get("server") or "").strip()
    if not server:
        return {"ok": False, "error": "缺少 server"}
    data = _mcp_servers()
    cur = data.get(server) or {}
    rt = cur.get("refresh_token") or payload.get("refresh_token") or ""
    if not rt:
        return {"ok": False, "error": "server '%s' 没有 refresh_token" % server}
    configs = _configs()
    cfg = dict(configs.get(server) or {})
    form = {
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "client_id": cfg.get("client_id") or "",
    }
    async with aiohttp.ClientSession() as sess:
        tok, err = await _post_form(sess, _token_candidates(cfg), form)
    if tok is None:
        return {"ok": False, "error": "刷新 token 失败: " + err}
    if not tok.get("access_token"):
        return {"ok": False, "error": "响应里没有 access_token"}
    saved = _write_token(server, cfg, tok)
    cfg["refreshed_at"] = time.time()
    configs[server] = cfg
    _save_configs(configs)
    return {
        "ok": True,
        "server": server,
        "mcp_url": saved.get("url"),
        "expires_in": tok.get("expires_in"),
        "has_refresh_token": bool(saved.get("refresh_token")),
    }


async def _action_logout(payload, agent):
    server = (payload.get("server") or "").strip()
    if not server:
        return {"ok": False, "error": "缺少 server"}
    remove = bool(payload.get("remove"))
    data = _mcp_servers()
    changed = False
    if server in data:
        if remove:
            data.pop(server, None)
        else:
            cur = data[server]
            cur["token"] = ""
            cur["refresh_token"] = ""
            cur["enabled"] = False
            data[server] = cur
        _save_mcp_servers(data)
        changed = True
    configs = _configs()
    if server in configs:
        configs[server].pop("signed_in_at", None)
        _save_configs(configs)
    states = _states()
    if server in states:
        states.pop(server, None)
        _save_states(states)
    return {"ok": True, "server": server, "removed": remove, "changed": changed}


async def _action_list(payload, agent):
    configs = _configs()
    data = _mcp_servers()
    out = []
    for name in sorted(set(list(configs.keys()) + list(data.keys()))):
        cfg = configs.get(name) or {}
        cur = data.get(name) or {}
        out.append({
            "server": name,
            "issuer": cfg.get("issuer") or "",
            "mcp_url": cfg.get("mcp_url") or cur.get("url") or "",
            "client_id": cfg.get("client_id") or "",
            "signed_in": bool(cur.get("token")),
            "scope": cur.get("scope") or cfg.get("scope") or "",
            "expires_at": cur.get("expires_at"),
            "expires_in_remaining": (int(cur.get("expires_at") - time.time())
                                    if cur.get("expires_at") else None),
            "expired": bool(cur.get("expires_at") and cur.get("expires_at") <= time.time()),
            "endpoints": {
                "authorization_endpoint": cfg.get("authorization_endpoint") or "",
                "token_endpoint": cfg.get("token_endpoint") or "",
                "registration_endpoint": cfg.get("registration_endpoint") or "",
            },
        })
    return {"ok": True, "data": out, "total": len(out)}


# ============================================================
# HTML 渲染（浏览器直连时用）
# ============================================================

def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


_CSS = (
    "body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;max-width:760px;"
    "margin:48px auto;padding:0 20px;color:#222;line-height:1.65}"
    "h2{color:COLOR;margin:0 0 4px;font-size:20px}"
    "h3{font-size:14px;margin:22px 0 8px;color:#555;font-weight:600}"
    "sub{display:block;color:#888;font-size:12px;margin:0 0 18px}"
    "table{border-collapse:collapse;margin:16px 0;width:100%;font-size:13px}"
    "td{padding:7px 10px;border-bottom:1px solid #eee;word-break:break-all;vertical-align:top}"
    "td.k{color:#888;width:130px}"
    "a.btn{display:inline-block;background:COLOR;color:#fff;padding:10px 20px;"
    "border-radius:8px;text-decoration:none;font-weight:600}"
    "a.mini{display:inline-block;padding:3px 10px;font-size:12px;border:1px solid #ddd;"
    "border-radius:6px;color:#555;text-decoration:none;background:#fff;cursor:pointer}"
    "a.mini:hover{border-color:COLOR;color:COLOR}"
    "code{background:#f5f5f5;padding:2px 6px;border-radius:4px;word-break:break-all}"
    "pre{white-space:pre-wrap;background:#f7f7f7;padding:12px;border-radius:8px;overflow:auto}"
    ".badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px}"
    ".badge.ok{background:#e6f7ed;color:#0a7}"
    ".badge.warn{background:#fff4e5;color:#b26a00}"
    ".badge.err{background:#fdeaea;color:#c33}"
    ".box{border:1px solid #e5e5e5;border-radius:10px;padding:14px 16px;margin:16px 0;background:#fafafa}"
    ".muted{color:#888;font-size:12px}"
    ".row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}"
    ".mini-in{padding:7px 10px;border:1px solid #ddd;border-radius:6px;font-size:13px}"
    ".mini-btn{padding:7px 14px;border:none;border-radius:6px;background:COLOR;color:#fff;"
    "cursor:pointer;font-size:13px}"
)


def _html_page(title, body, ok=True, subtitle=""):
    color = "#0a7" if ok else "#c33"
    if isinstance(body, (list, tuple)):
        body = "".join(body)
    css = _CSS.replace("COLOR", color)
    sub = ("<sub>%s</sub>" % _esc(subtitle)) if subtitle else ""
    return (
        "<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>" + _esc(title) + "</title><style>" + css + "</style></head><body>"
        "<h2>" + _esc(title) + "</h2>" + sub + body +
        "<p style=\"margin-top:32px;color:#666\">可以关闭本页，回到对话继续。</p>"
        "</body></html>"
    )


def _manage_page(res):
    """授权管理面板：手工新增 / 授权 / 续期 / 注销（不依赖 LLM）"""
    items = res.get('data') or []
    rows = []
    for it in items:
        name = str(it.get('server') or '')
        signed = bool(it.get('signed_in'))
        remaining = it.get('expires_in_remaining')
        if not signed:
            state = '<span class="badge warn">未授权</span>'
        elif remaining is None:
            state = '<span class="badge ok">已授权</span>'
        elif remaining > 0:
            state = ('<span class="badge ok">已授权</span>'
                     ' <span class="muted">%d 分钟后过期</span>'
                     % (int(remaining) // 60))
        else:
            state = '<span class="badge err">已过期</span>'
        q = urllib.parse.quote(name)
        if signed:
            acts = ('<a class="mini" href="/api/os/mcp_oauth?action=refresh&server=%s&format=html">续期</a>'
                    ' <a class="mini" href="/api/os/mcp_oauth?action=logout&server=%s&format=html">注销</a>'
                    % (q, q))
        else:
            u = urllib.parse.quote(it.get('mcp_url') or '')
            start_url = "/api/os/mcp_oauth?action=start&server=%s&format=html" % q
            if u:
                start_url += "&mcp_url=" + u
            acts = '<a class="mini" href="%s">授权</a>' % start_url
        rows.append(
            '<tr><td><b>%s</b></td><td>%s</td><td>%s</td>'
            '<td class="muted">%s</td><td>%s</td></tr>'
            % (_esc(name), state, _esc(it.get('mcp_url') or ''),
               _esc(it.get('scope') or ''), acts))
    if rows:
        table = ('<table><tr><td class="k">服务</td><td class="k">状态</td>'
                 '<td class="k">MCP 地址</td><td class="k">scope</td>'
                 '<td class="k">操作</td></tr>%s</table>' % ''.join(rows))
    else:
        table = '<p class="muted">还没有登记任何 MCP Server。</p>'

    form = (
        '<div class="box"><h3>手工新增 / 授权</h3>'
        '<div class="row">'
        '<input class="mini-in" id="srvName" placeholder="服务名，例如 my-server">'
        '<input class="mini-in" id="srvUrl" placeholder="MCP 地址，例如 https://host/mcp">'
        '<input class="mini-in" id="srvScope" placeholder="scope（留空=自动发现）">'
        '<button class="mini-btn" onclick="goAuth()">开始授权</button>'
        '</div>'
        '<p class="muted">流程：RFC 9728 / 8414 discovery → RFC 7591 动态注册 → '
        'RFC 7636 PKCE(S256) → 跳授权页；回调自动换 token 并写入 '
        'data/os/_mcp_client/servers.json。</p></div>'
    )
    script = (
        "<script>"
        "async function goAuth(){"
        "var n=(document.getElementById('srvName')||{}).value||'';"
        "var u=(document.getElementById('srvUrl')||{}).value||'';"
        "var s=(document.getElementById('srvScope')||{}).value||'';"
        "n=n.trim();u=u.trim();s=s.trim();"
        "if(!n){alert('请填写服务名');return;}"
        "var b={action:'start',server:n};"
        "if(u)b.mcp_url=u;"
        "if(s)b.scope=s;"
        "try{"
        "var r=await fetch('/api/os/mcp_oauth',{method:'POST',"
        "headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});"
        "var j=await r.json();"
        "if(j&&j.ok&&j.authorize_url){window.location.href=j.authorize_url;}"
        "else{alert((j&&j.error)||'授权启动失败');}"
        "}catch(e){alert(String(e));}"
        "}"
        "</script>"
    )
    body = table + form + script
    return _html_page('MCP 授权管理', [body], True,
                      '手工管理每个 MCP Server 的 OAuth 授权状态')


def _render_html(action, res):
    ok = bool(res.get("ok"))
    back = ('<p><a class="btn" href="/api/os/mcp_oauth?action=list&format=html">'
            '返回授权管理</a></p>')
    if action == "callback":
        if ok:
            rows = []
            for k in ("server", "mcp_url", "scope", "expires_in"):
                if res.get(k) is not None:
                    rows.append("<tr><td><b>%s</b></td><td><code>%s</code></td></tr>"
                                % (k, _esc(res[k])))
            body = ("<p>授权成功，token 已写入 <code>%s</code>。</p><table>%s</table>%s"
                    % (_esc(res.get("saved_to") or ""), "".join(rows), back))
            title = "MCP 授权成功"
        else:
            body = "<p>%s</p>%s" % (_esc(res.get("error")), back)
            title = "MCP 授权失败"
        return _html_page(title, [body], ok)

    if action == "start":
        if ok:
            url = res.get("authorize_url") or ""
            rows = []
            for k in ("server", "issuer", "mcp_url", "scope", "redirect_uri"):
                if res.get(k):
                    rows.append("<tr><td><b>%s</b></td><td><code>%s</code></td></tr>"
                                % (k, _esc(res[k])))
            body = ("<p>点击按钮完成授权，授权后浏览器会自动跳回回调地址并写入本地 token 配置。</p>"
                    "<p><a class=\"btn\" href=\"%s\">前往授权</a></p><table>%s</table>"
                    "<p>按钮无法点击时，复制链接到浏览器：<br><code>%s</code></p>%s"
                    % (_esc(url), "".join(rows), _esc(url), back))
            title = "MCP 授权启动"
        else:
            body = "<p>%s</p>%s" % (_esc(res.get("error")), back)
            title = "MCP 授权启动失败"
        return _html_page(title, [body], ok)

    if action in ("list", "manage", "status"):
        return _manage_page(res)

    if action in ("refresh", "logout"):
        body = ('<div class="box %s"><p>%s</p></div>%s<pre>%s</pre>'
                % ('ok' if ok else 'err',
                   '操作成功' if ok else '操作失败',
                   back,
                   _esc(json.dumps(res, indent=2, ensure_ascii=False))))
        return _html_page("MCP OAuth · %s" % action, [body], ok)

    body = ("<pre>%s</pre>"
            % _esc(json.dumps(res, indent=2, ensure_ascii=False)))
    return _html_page("MCP OAuth · %s" % action, [body], ok)


# ============================================================
# 入口
# ============================================================

async def execute(envelop, agent):
    try:
        raw = envelop.payload if isinstance(envelop.payload, dict) else {}
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
        meta = dict(envelop.meta or {}) if isinstance(envelop.meta, dict) else {}
        method = str(meta.get("method") or "").upper()
        action = str(payload.get("action") or "").strip().lower()
        if not action:
            # 未指定 action 时按上下文推断：带 code = 授权回调；否则查状态
            action = "callback" if payload.get("code") else "status"

        if action == "discover":
            res = await _action_discover(payload, agent)
        elif action == "configure":
            res = await _action_configure(payload, agent)
        elif action == "start":
            res = await _action_start(payload, agent)
        elif action == "callback":
            res = await _action_callback(payload, agent)
        elif action == "status":
            res = await _action_status(payload, agent)
        elif action == "refresh":
            res = await _action_refresh(payload, agent)
        elif action == "logout":
            res = await _action_logout(payload, agent)
        elif action == "list":
            res = await _action_list(payload, agent)
        else:
            res = {"ok": False, "error": "Unknown action: %s" % action, "actions": ACTIONS}

        envelop.payload = res

        # 输出分流：浏览器 GET / 显式 format=html → HTML 页面
        # LLM 编排调用与前端 POST → JSON（envelop.payload 保持结构化）
        wants_html = (payload.get("format") == "html") or (method == "GET")

        if wants_html and isinstance(res, dict):
            html = _render_html(action, res).encode("utf-8")
            envelop.meta = dict(meta)
            envelop.meta["static_content"] = html
            # 注意：网关用 web.Response(body=..., content_type=...) 渲染，
            # aiohttp 禁止 content_type 里带 charset（会抛 ValueError → 500）
            envelop.meta["content_type"] = "text/html"
        return envelop

    except Exception as e:
        try:
            agent.log.error("[mcp_oauth] %s: %s" % (type(e).__name__, e))
        except Exception:
            pass
        envelop.payload = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        return envelop


def help():
    return {
        "route": "os/mcp_oauth",
        "description": "通用 MCP OAuth 登录器：discovery + DCR + PKCE，token 落盘给 os/_mcp_client 使用",
        "actions": {
            "discover": {"input": {"server": "可选名字", "issuer": "可选",
                                   "mcp_url": "可选 https://host/mcp"}},
            "configure": {"input": {"server": "必填", "issuer": "可选", "mcp_url": "可选",
                                    "scope": "可选", "client_id": "可选"}},
            "start": {"input": {"server": "必填", "issuer": "可选", "mcp_url": "可选",
                                "scope": "可选", "redirect_uri": "可选", "client_id": "可选",
                                "update_url": "可选，是否覆盖 servers.json 里的 url"}},
            "callback": {"input": {"code": "授权码", "state": "state", "server": "可选"}},
            "status": {"input": {"server": "可选，不传=全部"}},
            "refresh": {"input": {"server": "必填"}},
            "logout": {"input": {"server": "必填", "remove": "可选，是否彻底删条目"}},
            "list": {"input": {}},
        },
    }