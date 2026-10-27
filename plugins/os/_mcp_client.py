"""os/_mcp_client — MCP 客户端（反向接入：把外部 MCP Server 接进 AICP 体内）

把外部 MCP Server 的 tool 变成 AICP 的能力，和内置插件平权调用。

actions:
    connect       建 session + 拉 tools/list + 写缓存与契约
    disconnect    DELETE /mcp，清 session（保留 tools 缓存）
    list_servers  配置 + 连通状态 + 工具数
    list_tools    无 server = 全部汇总；有 server = 该 server 的 schema
    call_tool     核心。lazy connect + 过期刷新 + 熔断 + 401 重连一次
    refresh       强制重拉 tools/list

数据落盘：
    data/os/_mcp_client/servers.json        配置（人可手改）
    data/os/_mcp_client/state.json          运行时状态（session / 熔断）
    data/os/_mcp_client/tools/<server>.json tools/list 缓存
    data/os/_mcp_client/contracts/<server>.<tool>.json  单工具契约（L1 直取）
"""

import asyncio
import json
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "aicp-mcp-client", "version": "1.0.0"}

DATA_DIR = Path("data/os/_mcp_client")
SERVERS_FILE = DATA_DIR / "servers.json"
STATE_FILE = DATA_DIR / "state.json"
TOOLS_DIR = DATA_DIR / "tools"
CONTRACTS_DIR = DATA_DIR / "contracts"

DEFAULT_TTL = 3600
DEFAULT_TIMEOUT = 60
HEALTH_TIMEOUT = 3
CIRCUIT_THRESHOLD = 3
CIRCUIT_COOLDOWN = 60
MAX_RESULT_CHARS = 32000

ACTIONS = ["connect", "disconnect", "remove_server", "list_servers", "list_tools", "call_tool", "refresh"]


# ============================================================
# 落盘
# ============================================================

def _ensure_dirs():
    for d in (DATA_DIR, TOOLS_DIR, CONTRACTS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _load(path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        pass
    return default


def _save(path, data):
    _ensure_dirs()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _servers():
    return _load(SERVERS_FILE, {})


def _state():
    return _load(STATE_FILE, {})


def _save_state(st):
    _save(STATE_FILE, st)


def _base_url(url):
    u = (url or "").strip().rstrip("/")
    if u.endswith("/mcp"):
        u = u[:-4]
    return u


def _is_local(url):
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host in ("127.0.0.1", "localhost", "0.0.0.0", "::1") or host.startswith("169.254.")


# ============================================================
# JSON-RPC / SSE
# ============================================================

async def _parse_sse(resp):
    """逐行解析 SSE，取第一个完整 data 块的 JSON-RPC 响应"""
    data_lines = []
    try:
        async for raw in resp.content:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif line == "" and data_lines:
                break
            elif line.startswith("event:") or line.startswith(":"):
                continue
    except Exception:
        pass
    text = "\n".join(data_lines).strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {"_raw_text": text[:2000]}


async def _rpc(base, token, session_id, method, params, timeout):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex[:12], "method": method}
    if params is not None:
        body["params"] = params

    url = base + "/mcp"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as sess:
            async with sess.post(url, json=body, headers=headers) as resp:
                sid = resp.headers.get("Mcp-Session-Id") or session_id
                status = resp.status
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if "text/event-stream" in ctype:
                    payload = await _parse_sse(resp)
                else:
                    text = await resp.text()
                    try:
                        payload = json.loads(text) if text.strip() else {}
                    except (json.JSONDecodeError, ValueError):
                        payload = {"_raw_text": text[:2000]}
                return {"status": status, "session_id": sid, "payload": payload}
    except asyncio.TimeoutError:
        return {"status": 0, "session_id": session_id, "payload": {}, "error": "timeout after %ss" % timeout}
    except Exception as e:
        return {"status": 0, "session_id": session_id, "payload": {}, "error": "%s: %s" % (type(e).__name__, e)}


async def _health(base, timeout=HEALTH_TIMEOUT):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as sess:
            async with sess.get(base + "/health") as resp:
                txt = await resp.text()
                return {"status": resp.status, "body": txt[:300]}
    except asyncio.TimeoutError:
        return {"status": 0, "error": "health timeout"}
    except Exception as e:
        return {"status": 0, "error": "%s: %s" % (type(e).__name__, e)}


# ============================================================
# 结果整形
# ============================================================

def _shape_result(result):
    if not isinstance(result, dict):
        return {"ok": True, "data": result}

    is_error = bool(result.get("isError"))
    content = result.get("content")
    if isinstance(content, list):
        texts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                texts.append(str(c.get("text") or ""))
        text = "\n".join(texts)
        parsed = text
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            pass
        truncated = False
        if isinstance(parsed, str) and len(parsed) > MAX_RESULT_CHARS:
            parsed = parsed[:MAX_RESULT_CHARS] + "...[truncated]"
            truncated = True
        out = {"ok": not is_error, "data": parsed}
        if truncated:
            out["truncated"] = True
        if is_error:
            out["error"] = parsed if isinstance(parsed, str) else json.dumps(parsed, ensure_ascii=False)[:2000]
        return out

    out = {"ok": not is_error, "data": result}
    if is_error:
        out["error"] = "tool returned isError"
    return out


def _extract(resp):
    """JSON-RPC 响应 → 统一结果"""
    if resp.get("error") and not resp.get("status"):
        return {"ok": False, "error": resp["error"], "status": 0}
    payload = resp.get("payload") or {}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "bad response type", "status": resp.get("status")}
    err = payload.get("error")
    if err:
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return {"ok": False, "error": msg or "jsonrpc error", "code": (err.get("code") if isinstance(err, dict) else None), "status": resp.get("status")}
    result = payload.get("result")
    if result is None:
        return {"ok": False, "error": "empty result (status %s)" % resp.get("status"), "status": resp.get("status")}
    shaped = _shape_result(result)
    shaped["status"] = resp.get("status")
    return shaped


# ============================================================
# connect / refresh
# ============================================================

async def _do_connect(server, cfg, agent):
    base = _base_url(cfg.get("url"))
    if not base:
        return {"ok": False, "error": "server '%s' 缺少 url" % server}

    token = cfg.get("token") or ""
    timeout = int(cfg.get("timeout") or DEFAULT_TIMEOUT)
    notes = []

    if _is_local(base):
        notes.append("target is local address")

    h = await _health(base)
    if h.get("status") != 200:
        notes.append("health check failed: %s" % (h.get("error") or h.get("status")))

    init = await _rpc(base, token, "", "initialize", {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": CLIENT_INFO,
    }, timeout)
    init_res = _extract(init)
    if not init_res.get("ok"):
        return {"ok": False, "error": "initialize failed: %s" % init_res.get("error"), "notes": notes}

    sid = init.get("session_id") or ""
    if not sid:
        notes.append("no Mcp-Session-Id header")

    server_info = {}
    payload_result = (init.get("payload") or {}).get("result") or {}
    if isinstance(payload_result, dict):
        server_info = payload_result.get("serverInfo") or {}

    # notifications/initialized（失败不致命）
    try:
        await _rpc(base, token, sid, "notifications/initialized", {}, timeout)
    except Exception:
        pass

    tools, cursor, pages = [], None, 0
    while True:
        params = {"cursor": cursor} if cursor else {}
        r = await _rpc(base, token, sid, "tools/list", params, timeout)
        got = _extract(r)
        if not got.get("ok"):
            return {"ok": False, "error": "tools/list failed: %s" % got.get("error"), "notes": notes}
        data = got.get("data") or {}
        if isinstance(data, dict):
            tools.extend([t for t in (data.get("tools") or []) if isinstance(t, dict)])
            cursor = data.get("nextCursor")
        else:
            cursor = None
        pages += 1
        if not cursor or pages >= 20:
            break

    # 写 tools 缓存
    _save(TOOLS_DIR / (server + ".json"), {
        "server": server,
        "fetched_at": time.time(),
        "base": base,
        "tools": tools,
    })

    # P2：每个 tool 的 inputSchema 落成契约文件（L1 直取，不靠 LLM 猜）
    for t in tools:
        name = str(t.get("name") or "").strip()
        if not name:
            continue
        _save(CONTRACTS_DIR / ("%s.%s.json" % (server, name)), {
            "server": server,
            "tool": name,
            "description": t.get("description") or "",
            "inputSchema": t.get("inputSchema") or {},
            "source": "mcp",
            "url": base,
            "fetched_at": time.time(),
        })

    # 更新 state
    st = _state()
    cur = st.get(server) or {}
    cur.update({
        "session_id": sid,
        "last_active": time.time(),
        "fail_count": 0,
        "circuit_open_until": 0,
        "connected": True,
        "last_error": "",
        "tool_count": len(tools),
    })
    st[server] = cur
    _save_state(st)

    return {
        "ok": True,
        "server": server,
        "base": base,
        "session_id": sid,
        "server_info": server_info,
        "tool_count": len(tools),
        "tools": [t.get("name") for t in tools],
        "notes": notes,
    }


# ============================================================
# actions
# ============================================================

async def _list_servers(agent=None):
    await _prune_orphan_routes(agent)
    servers = _servers()
    st = _state()
    out = []
    for name, cfg in servers.items():
        s = st.get(name) or {}
        cache = _load(TOOLS_DIR / (name + ".json"), {})
        out.append({
            "server": name,
            "url": cfg.get("url"),
            "enabled": cfg.get("enabled", True),
            "connected": bool(s.get("connected")),
            "session_id": s.get("session_id") or "",
            "stateless": bool(s.get("connected")) and not bool(s.get("session_id")),
            "tool_count": len(cache.get("tools") or []),
            "tools_fetched_at": cache.get("fetched_at"),
            "circuit_open_until": s.get("circuit_open_until") or 0,
            "last_error": s.get("last_error") or "",
        })
    return {"ok": True, "data": out, "total": len(out)}


async def _connect(payload, agent):
    server = (payload.get("server") or payload.get("name") or "").strip()
    url = (payload.get("url") or "").strip()
    if not server and url:
        server = "default"
    if not server:
        return {"ok": False, "error": "缺少 server（给个名字）; url 可选（已有配置可不传）"}

    servers = _servers()
    cfg = servers.get(server) or {}
    if url:
        cfg.update({
            "url": url,
            "token": payload.get("token") or cfg.get("token") or "",
            "ttl": int(payload.get("ttl") or cfg.get("ttl") or DEFAULT_TTL),
            "timeout": int(payload.get("timeout") or cfg.get("timeout") or DEFAULT_TIMEOUT),
            "enabled": True,
        })
        servers[server] = cfg
        _save(SERVERS_FILE, servers)

    if not cfg.get("url"):
        return {"ok": False, "error": "server '%s' 未注册 url，请传 url" % server}

    return await _do_connect(server, cfg, agent)


async def _refresh(payload, agent):
    servers = _servers()
    targets = []
    if payload.get("server"):
        targets = [payload["server"]]
    else:
        targets = [k for k, v in servers.items() if v.get("enabled", True)]
    if not targets:
        return {"ok": False, "error": "没有可刷新的 server"}

    results = []
    for name in targets:
        cfg = servers.get(name) or {}
        if not cfg.get("url"):
            results.append({"server": name, "ok": False, "error": "缺少 url"})
            continue
        r = await _do_connect(name, cfg, agent)
        results.append({"server": name, "ok": r.get("ok"), "tool_count": r.get("tool_count"), "error": r.get("error")})
    # 工具列表可能变化，重新同步虚拟路由
    for name in targets:
        await _register_virtual({"server": name}, agent)
    return {"ok": all(x.get("ok") for x in results), "data": results}


async def _remove_server(payload, agent):
    server = (payload.get("server") or "").strip()
    if not server:
        return {"ok": False, "error": "缺少 server"}
    servers = _servers()
    if server not in servers:
        return {"ok": False, "error": "server '%s' 不存在" % server}
    del servers[server]
    _save(SERVERS_FILE, servers)
    # 清 state
    st = _state()
    if server in st:
        del st[server]
        _save_state(st)
    # 清 tools 缓存和契约
    tool_file = TOOLS_DIR / (server + ".json")
    if tool_file.exists():
        try:
            tool_file.unlink()
        except OSError:
            pass
    for f in CONTRACTS_DIR.glob(server + ".*.json"):
        try:
            f.unlink()
        except OSError:
            pass
    # 回收虚拟路由（与 connect 的注册对称，避免留下幽灵路由）
    virtual = await _unregister_virtual({"server": server}, agent)
    return {"ok": True, "server": server, "virtual": virtual}


async def _disconnect(payload):
    server = (payload.get("server") or "").strip()
    servers = _servers()
    if not server:
        return {"ok": False, "error": "缺少 server"}
    cfg = servers.get(server) or {}
    st = _state()
    cur = st.get(server) or {}
    sid = cur.get("session_id") or ""
    base = _base_url(cfg.get("url"))

    deleted = False
    if base and sid:
        try:
            headers = {"Mcp-Session-Id": sid, "Accept": "application/json, text/event-stream"}
            if cfg.get("token"):
                headers["Authorization"] = "Bearer " + cfg["token"]
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as sess:
                async with sess.delete(base + "/mcp", headers=headers) as resp:
                    deleted = resp.status < 400
        except Exception:
            deleted = False

    cur.update({"session_id": "", "connected": False, "last_active": time.time()})
    st[server] = cur
    _save_state(st)
    return {"ok": True, "server": server, "session_deleted": deleted, "tools_cache_kept": True}


async def _list_tools(payload, agent=None):
    # 先回收孤儿路由（servers.json 已删但 core.plugins/registry 仍挂着）
    await _prune_orphan_routes(agent)
    server = (payload.get("server") or "").strip()
    servers = _servers()
    names = [server] if server else list(servers.keys())
    out = []
    for name in names:
        cache = _load(TOOLS_DIR / (name + ".json"), {})
        for t in (cache.get("tools") or []):
            out.append({
                "server": name,
                "tool": t.get("name"),
                "description": (t.get("description") or "")[:300],
                "inputSchema": t.get("inputSchema") or {},
            })
    return {"ok": True, "data": out, "total": len(out)}


async def _call_tool(payload, agent):
    server = (payload.get("server") or "").strip()
    tool = (payload.get("tool") or payload.get("name") or "").strip()
    args = payload.get("arguments")
    if not isinstance(args, dict):
        args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    if not server or not tool:
        return {"ok": False, "error": "需要 server 和 tool"}

    servers = _servers()
    cfg = servers.get(server)
    if not cfg or not cfg.get("url"):
        return {"ok": False, "error": "server '%s' 未注册，请先 connect 并传 url" % server}
    if cfg.get("enabled") is False:
        return {"ok": False, "error": "server '%s' 已禁用" % server}

    st = _state()
    cur = st.get(server) or {}
    now = time.time()
    if (cur.get("circuit_open_until") or 0) > now:
        return {"ok": False, "error": "circuit open（%.0fs 后重试）" % (cur["circuit_open_until"] - now)}

    ttl = int(cfg.get("ttl") or DEFAULT_TTL)
    timeout = int(cfg.get("timeout") or DEFAULT_TIMEOUT)
    base = _base_url(cfg.get("url"))
    token = cfg.get("token") or ""

    # ---- 1. 工具缓存校验（懒连接 + 过期刷新）----
    cache = _load(TOOLS_DIR / (server + ".json"), {})
    cached_tools = cache.get("tools") or []
    cached_names = {str(t.get("name")) for t in cached_tools if isinstance(t, dict)}
    # 无状态的 server 不返回 Mcp-Session-Id，用 connected 判定，避免每次调用都重跑 initialize
    need_connect = not cache or not cur.get("connected")
    need_refresh = (not need_connect) and ((now - float(cache.get("fetched_at") or 0)) > ttl)
    if need_connect or need_refresh or (tool not in cached_names):
        conn = await _do_connect(server, cfg, agent)
        if not conn.get("ok"):
            return {"ok": False, "error": "connect/refresh failed: %s" % conn.get("error")}
        st = _state()
        cur = st.get(server) or {}
        cache = _load(TOOLS_DIR / (server + ".json"), {})
        cached_names = {str(t.get("name")) for t in (cache.get("tools") or []) if isinstance(t, dict)}
        if tool not in cached_names:
            return {"ok": False, "error": "server '%s' 无工具 '%s'" % (server, tool), "available": sorted(cached_names)}

    sid = cur.get("session_id") or ""

    # ---- 2. 调用 ----
    resp = await _rpc(base, token, sid, "tools/call", {"name": tool, "arguments": args}, timeout)
    status = resp.get("status") or 0

    # ---- 3. session 失效 → 重连重试一次 ----
    if status in (401, 403) or (isinstance(resp.get("payload"), dict) and (resp["payload"].get("error") or {}).get("code") == -32013):
        conn = await _do_connect(server, cfg, agent)
        if conn.get("ok"):
            st = _state()
            cur = st.get(server) or {}
            resp = await _rpc(base, token, cur.get("session_id") or "", "tools/call", {"name": tool, "arguments": args}, timeout)
            status = resp.get("status") or 0

    got = _extract(resp)

    # ---- 4. 失败计数 / 熔断 ----
    st = _state()
    cur = st.get(server) or {}
    if got.get("ok"):
        cur.update({"fail_count": 0, "circuit_open_until": 0, "connected": True, "last_active": time.time(), "last_error": ""})
    else:
        fc = int(cur.get("fail_count") or 0) + 1
        cur["fail_count"] = fc
        cur["last_error"] = str(got.get("error"))[:300]
        if fc >= CIRCUIT_THRESHOLD:
            cur["circuit_open_until"] = time.time() + CIRCUIT_COOLDOWN
            cur["fail_count"] = 0
            got["circuit_opened"] = True
    st[server] = cur
    _save_state(st)

    got["server"] = server
    got["tool"] = tool
    return got


# ============================================================
# 入口
# ============================================================


# ============================================================
# 虚拟插件注册：把外部 tool 变成 AICP 能力（cogitor 可搜 + core.plugins 可调）
# ============================================================

def _tool_route(server, tool):
    return "mcp/%s/%s" % (server, tool)


def _make_handler(server, tool):
    async def _handler(envelop, agent):
        try:
            p = envelop.payload if isinstance(envelop.payload, dict) else {}
            args = p.get("arguments")
            if not isinstance(args, dict):
                args = {k: v for k, v in p.items()
                        if k not in ("action", "payload", "server", "tool", "arguments")}
            envelop.payload = await _call_tool(
                {"server": server, "tool": tool, "arguments": args}, agent)
        except Exception as e:
            envelop.payload = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        return envelop
    return _handler


def _register_server_tools(server):
    import core as _core
    cache = _load(TOOLS_DIR / (server + ".json"), {})
    tools = cache.get("tools") or []
    items = []
    for t in tools:
        name = str(t.get("name") or "").strip()
        if not name:
            continue
        route = _tool_route(server, name)
        _core.plugins[route] = _make_handler(server, name)
        items.append({"name": route,
                      "desc": (t.get("description") or "").strip()[:300],
                      "schema": t.get("inputSchema") or {},
                      "owner": server,
                      "source": "mcp"})
    return items


async def _register_virtual(payload, agent):
    import core as _core
    # 先回收孤儿路由（servers.json 已删但 core.plugins/registry 仍挂着）
    await _prune_orphan_routes(agent)
    server = (payload.get("server") or "").strip()
    names = [server] if server else list(_servers().keys())
    # 先清旧路由（工具列表可能变化），再按最新缓存注册
    for s in names:
        await _unregister_virtual({"server": s}, agent)
    items = []
    for s in names:
        items.extend(_register_server_tools(s))
    if items:
        try:
            env = _core.Envelop(sender="os/_mcp_client", receiver="os/_registry",
                               intent="API_CALL",
                               payload={"action": "register_virtual", "items": items})
            await agent.system.call(env)
        except Exception as e:
            try:
                agent.log.warning("[mcp_client] register_virtual failed: %s" % e)
            except Exception:
                pass
    return {"ok": True, "server": server or "*", "registered": len(items),
            "routes": [i["name"] for i in items]}


async def _unregister_virtual(payload, agent):
    import core as _core
    server = (payload.get("server") or "").strip()
    if not server:
        return {"ok": False, "error": "missing server"}
    prefix = "mcp/%s" % server
    removed = 0
    for k in [k for k in list(_core.plugins.keys())
              if k == prefix or k.startswith(prefix + "/")]:
        _core.plugins.pop(k, None)
        removed += 1
    try:
        env = _core.Envelop(sender="os/_mcp_client", receiver="os/_registry",
                           intent="API_CALL",
                           payload={"action": "unregister_virtual", "prefix": prefix})
        await agent.system.call(env)
    except Exception as e:
        try:
            agent.log.warning("[mcp_client] unregister_virtual failed: %s" % e)
        except Exception:
            pass
    return {"ok": True, "server": server, "prefix": prefix, "removed": removed}

async def _prune_orphan_routes(agent):
    """回收 servers.json 里已不存在的 server 所残留的 mcp/<x>/* 路由"""
    import core as _core
    servers = _servers()
    orphans = set()
    for k in list(_core.plugins.keys()):
        if not k.startswith("mcp/"):
            continue
        name = k[4:].split("/", 1)[0]
        if name and name not in servers:
            orphans.add(name)
    removed = 0
    for name in sorted(orphans):
        res = await _unregister_virtual({"server": name}, agent)
        removed += int(res.get("removed") or 0)
    return {"ok": True, "orphans": sorted(orphans), "removed": removed}


async def execute(envelop, agent):
    try:
        raw = envelop.payload if isinstance(envelop.payload, dict) else {}
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
        action = payload.get("action")

        if action == "list_servers":
            envelop.payload = await _list_servers(agent)
        elif action == "connect":
            res = await _connect(payload, agent)
            if isinstance(res, dict) and res.get("ok"):
                try:
                    res["virtual"] = await _register_virtual({"server": res.get("server")}, agent)
                except Exception as e:
                    res["virtual_error"] = str(e)
            envelop.payload = res
        elif action == "remove_server":
            envelop.payload = await _remove_server(payload, agent)
        elif action == "disconnect":
            envelop.payload = await _disconnect(payload)
        elif action == "list_tools":
            envelop.payload = await _list_tools(payload, agent)
        elif action == "call_tool":
            envelop.payload = await _call_tool(payload, agent)
        elif action == "refresh":
            envelop.payload = await _refresh(payload, agent)
        elif action == "register_virtual":
            envelop.payload = await _register_virtual(payload, agent)
        elif action == "unregister_virtual":
            envelop.payload = await _unregister_virtual(payload, agent)
        else:
            envelop.payload = {
                "ok": False,
                "error": "Unknown action: %s" % action,
                "actions": ACTIONS,
            }
        return envelop

    except Exception as e:
        try:
            agent.log.error("[mcp_client] %s: %s" % (type(e).__name__, e))
        except Exception:
            pass
        envelop.payload = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        return envelop


def help():
    return {
        "route": "os/_mcp_client",
        "description": "MCP 客户端：把外部 MCP Server 的 tool 接进 AICP，和内置插件平权调用",
        "actions": {
            "connect": {"input": {"server": "名字", "url": "http://host:port/mcp", "token": "可选", "ttl": "可选", "timeout": "可选"}},
            "list_servers": {},
            "list_tools": {"input": {"server": "可选，不传=全部"}},
            "call_tool": {"input": {"server": "名字", "tool": "工具名", "arguments": {}}},
            "refresh": {"input": {"server": "可选"}},
            "disconnect": {"input": {"server": "名字"}},
            "remove_server": {"input": {"server": "名字"}},
        },
    }
