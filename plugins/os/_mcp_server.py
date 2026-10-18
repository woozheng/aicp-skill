"""os/_mcp_server — MCP Server 入口插件（只暴露 call_plugin）

定位：当客户端不支持 HTTP 时，提供一个 MCP 入口来执行能力。
- 只暴露一个工具：call_plugin
- 接收 receiver / action / params，转成 Envelop，route 转发
- 认证走 os/_auth（source=mcp，跳过公开路由）
- initialize 总是建 session，返回 Mcp-Session-Id

启动：
    Envelop(sender="__main__", receiver="os/_mcp_server",
            payload={"action": "START", "host": "0.0.0.0", "port": 9100})
"""

import asyncio
import json
import time
import uuid
from collections import defaultdict
from aiohttp import web

import core


# ============================================================
# 常量
# ============================================================

PROTOCOL_VERSION = "2025-11-25"
SERVER_NAME = "aicp-mcp-server"
SERVER_VERSION = "1.0.0"

# JSON-RPC 错误码
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603
ERR_UNAUTHORIZED = -32001
ERR_FORBIDDEN = -32003
ERR_TIMEOUT = -32011
ERR_RATE_LIMITED = -32012
ERR_SESSION_EXPIRED = -32013

# Session
SESSION_IDLE_TIMEOUT = 3600
SESSION_CLEANUP_INTERVAL = 300

# 限流
MAX_CONCURRENT_PER_SESSION = 10
RATE_LIMIT_PER_MINUTE = 120

# 工具超时（秒）
TOOL_TIMEOUT = {
    "builtins/tools/fetch": 60,
    "os/file_utils_api": 30,
    "builtins/tools/cogitor": 15,
    "builtins/tools/contract_agent": 30,
    "builtins/tools/aicp_chat": 120,
    "default": 60,
}


# ============================================================
# Session Store
# ============================================================

class SessionStore:
    def __init__(self):
        self.sessions = {}
        self.token_map = {}
        self._lock = asyncio.Lock()

    async def create(self, token, user_id="anonymous", permissions=None, channel_id=None):
        async with self._lock:
            session_id = f"sess_{uuid.uuid4().hex[:16]}"
            session = {
                "session_id": session_id,
                "token": token,
                "user_id": user_id,
                "permissions": permissions or {"*": True},
                "channel_id": channel_id or f"ch_{uuid.uuid4().hex[:12]}",
                "created_at": time.time(),
                "last_active": time.time(),
                "initialized": False,
                "calls": 0,
                "errors": 0,
                "concurrent": 0,
                "rate_window": [],
            }
            self.sessions[session_id] = session
            if token:
                self.token_map[token] = session_id
            return session

    async def get(self, session_id, touch=True):
        async with self._lock:
            s = self.sessions.get(session_id)
            if s and touch:
                s["last_active"] = time.time()
            return s

    async def get_by_token(self, token):
        async with self._lock:
            sid = self.token_map.get(token)
            if sid:
                s = self.sessions.get(sid)
                if s:
                    s["last_active"] = time.time()
                return s
            return None

    async def remove(self, session_id):
        async with self._lock:
            s = self.sessions.pop(session_id, None)
            if s and s.get("token"):
                self.token_map.pop(s["token"], None)
            return s

    async def cleanup(self, max_idle=SESSION_IDLE_TIMEOUT):
        async with self._lock:
            now = time.time()
            expired = [
                sid for sid, s in self.sessions.items()
                if now - s["last_active"] > max_idle
            ]
            for sid in expired:
                s = self.sessions.pop(sid, None)
                if s and s.get("token"):
                    self.token_map.pop(s["token"], None)
            return len(expired)

    async def stats(self):
        async with self._lock:
            return {
                "total": len(self.sessions),
                "active": sum(
                    1 for s in self.sessions.values()
                    if time.time() - s["last_active"] < 300
                ),
            }


# ============================================================
# 指标
# ============================================================

class Metrics:
    def __init__(self):
        self.counters = defaultdict(int)
        self.timings = defaultdict(list)
        self._lock = asyncio.Lock()

    async def inc(self, name, value=1):
        async with self._lock:
            self.counters[name] += value

    async def observe(self, name, ms):
        async with self._lock:
            self.timings[name].append(ms)
            if len(self.timings[name]) > 1000:
                self.timings[name] = self.timings[name][-1000:]

    async def snapshot(self):
        async with self._lock:
            result = {"counters": dict(self.counters)}
            p95 = {}
            for name, vals in self.timings.items():
                if vals:
                    sorted_vals = sorted(vals)
                    idx = int(len(sorted_vals) * 0.95)
                    p95[name] = sorted_vals[min(idx, len(sorted_vals) - 1)]
            result["p95_ms"] = p95
            return result


# ============================================================
# 工具定义（只暴露一个）
# ============================================================

MCP_TOOLS = [
    {
        "name": "call_plugin",
        "description": "调用内部 AICP 插件。传入 receiver（插件路由）、可选 action 和 params。返回插件执行后的 payload。",
        "inputSchema": {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {
                "receiver": {"type": "string", "description": "插件路由，如 builtins/tools/fetch"},
                "action": {"type": "string", "description": "插件 action，可选"},
                "params": {"type": "object", "description": "插件参数", "additionalProperties": True},
            },
            "required": ["receiver"],
            "additionalProperties": False,
        },
    },
]


# ============================================================
# 权限
# ============================================================

def _check_permission(session, receiver, action=None):
    perms = session.get("permissions") or {}
    if perms.get("*"):
        return True
    for prefix, allowed in perms.items():
        if prefix == "*":
            continue
        if allowed and receiver.startswith(prefix):
            if isinstance(allowed, dict):
                if action is None:
                    return True
                return allowed.get(action, False) or allowed.get("*", False)
            return True
    return False


# ============================================================
# 限流
# ============================================================

def _check_rate_limit(session):
    now = time.time()
    window = session["rate_window"]
    session["rate_window"] = [t for t in window if now - t < 60]
    if len(session["rate_window"]) >= RATE_LIMIT_PER_MINUTE:
        return False
    session["rate_window"].append(now)
    return True


# ============================================================
# 插件入口
# ============================================================

async def execute(envelop, agent):
    action = envelop.payload.get("action", "START")

    if action == "START":
        return await _start(envelop, agent)
    elif action == "STOP":
        return await _stop(envelop, agent)
    elif action == "sessions":
        store = getattr(agent, "_mcp_sessions", None)
        if store:
            envelop.payload = {"ok": True, "data": await store.stats()}
        else:
            envelop.payload = {"ok": True, "data": {"total": 0, "active": 0}}
        return envelop
    elif action == "metrics":
        metrics = getattr(agent, "_mcp_metrics", None)
        if metrics:
            envelop.payload = {"ok": True, "data": await metrics.snapshot()}
        else:
            envelop.payload = {"ok": True, "data": {}}
        return envelop
    else:
        envelop.payload = {"ok": False, "error": f"Unknown action: {action}"}
        return envelop


async def _start(envelop, agent):
    host = envelop.payload.get("host", "127.0.0.1")
    port = envelop.payload.get("port", 9100)

    if not hasattr(agent, "_mcp_sessions"):
        agent._mcp_sessions = SessionStore()
    if not hasattr(agent, "_mcp_metrics"):
        agent._mcp_metrics = Metrics()
    store = agent._mcp_sessions
    metrics = agent._mcp_metrics

    app = web.Application(client_max_size=10 * 1024 * 1024)

    @web.middleware
    async def cors(request, handler):
        if request.method == "OPTIONS":
            return web.Response(status=200, headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization, Mcp-Session-Id, MCP-Protocol-Version",
            })
        resp = await handler(request)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    app.middlewares.append(cors)

    def _rpc_ok(req_id, result):
        return web.json_response({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _rpc_error(req_id, code, message, data=None):
        err = {"code": code, "message": message}
        if data:
            err["data"] = data
        return web.json_response({"jsonrpc": "2.0", "id": req_id, "error": err})

    def _to_mcp_content(result, is_error=False):
        return {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}],
            "isError": is_error,
        }

    async def _log(level, event, **kwargs):
        try:
            msg = json.dumps({"event": event, **kwargs}, ensure_ascii=False, default=str)
            if level == "info":
                agent.log.info(f"[MCP] {msg}")
            elif level == "warning":
                agent.log.warning(f"[MCP] {msg}")
            else:
                agent.log.error(f"[MCP] {msg}")
        except Exception:
            pass

    # ---------- 认证（走 os/_auth，source=mcp）----------
    async def _verify_token(token):
        if not token:
            return None
        auth_env = core.Envelop(
            sender="os/_mcp_server",
            receiver="os/_auth",
            payload={"action": "verify", "token": token},
            meta={"token": token, "source": "mcp"},
        )
        try:
            result = await agent.system.call(auth_env)
            if result and result.payload.get("ok"):
                return {
                    "user_id": result.payload.get("user_id", "anonymous"),
                    "permissions": result.payload.get("permissions", {"*": True}),
                }
        except Exception as e:
            await _log("error", "auth_error", error=str(e))
        return None

    # ---------- session 解析：总是返回一个 session ----------
    async def _resolve_session(request):
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        session_id = request.headers.get("Mcp-Session-Id", "")

        # 1. 优先 session_id
        if session_id:
            s = await store.get(session_id)
            if s:
                return s
            # session 过期，重建
            await _log("warning", "session_rebuild", old=session_id)

        # 2. 按 token 查
        if token:
            s = await store.get_by_token(token)
            if s:
                return s

        # 3. 验证 token（如果带了）
        info = await _verify_token(token) if token else None
        if not info:
            info = {"user_id": "anonymous", "permissions": {"*": True}}

        # 4. 建 session
        s = await store.create(
            token or "",
            user_id=info["user_id"],
            permissions=info["permissions"],
        )
        return s

    async def _call_internal(session, receiver, action=None, params=None):
        payload = dict(params or {})
        if action:
            payload["action"] = action

        env = core.Envelop(
            sender="os/_mcp_server",
            receiver=receiver,
            payload=payload,
            meta={
                "session_id": session["session_id"],
                "user_id": session["user_id"],
                "channel_id": session["channel_id"],
                "source": "mcp",
            },
        )

        timeout = TOOL_TIMEOUT.get(receiver, TOOL_TIMEOUT["default"])
        start = time.time()
        try:
            result = await asyncio.wait_for(agent.system.call(env), timeout=timeout)
            elapsed = (time.time() - start) * 1000
            await metrics.observe("call_plugin", elapsed)
            await metrics.inc("call_plugin_ok")
            if result is None:
                return {"ok": False, "error": "no response"}
            return result.payload if isinstance(result.payload, dict) else {"ok": True, "data": result.payload}
        except asyncio.TimeoutError:
            await metrics.inc("call_plugin_timeout")
            return {"ok": False, "error": f"timeout after {timeout}s"}
        except Exception as e:
            await metrics.inc("call_plugin_error")
            await _log("error", "plugin_error", receiver=receiver, error=f"{type(e).__name__}: {e}")
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ---------- MCP 主处理 ----------
    async def handle_mcp(request):
        accept = request.headers.get("Accept", "")
        if "application/json" not in accept and "text/event-stream" not in accept:
            return _rpc_error(None, ERR_INVALID_REQUEST, "Accept must include application/json or text/event-stream")

        try:
            body = await request.json()
        except Exception:
            return _rpc_error(None, ERR_PARSE, "parse error")

        if not isinstance(body, dict):
            return _rpc_error(None, ERR_INVALID_REQUEST, "invalid request")

        method = body.get("method", "")
        req_id = body.get("id")
        params = body.get("params", {})

        # ----- initialize：总是建 session，返回 Mcp-Session-Id -----
        if method == "initialize":
            client_proto = params.get("protocolVersion", PROTOCOL_VERSION)
            session = await _resolve_session(request)

            result = {
                "protocolVersion": client_proto,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
            resp = _rpc_ok(req_id, result)
            resp.headers["Mcp-Session-Id"] = session["session_id"]
            return resp

        # ----- 其他方法：解析 session（总是能拿到）-----
        session = await _resolve_session(request)

        if method == "notifications/initialized":
            session["initialized"] = True
            return web.json_response({"jsonrpc": "2.0", "id": req_id, "result": {}})

        if method == "ping":
            return _rpc_ok(req_id, {})

        if method == "tools/list":
            return _rpc_ok(req_id, {"tools": MCP_TOOLS})

        if method == "tools/call":
            tool_name = params.get("name", "")
            args = params.get("arguments", {})

            if tool_name != "call_plugin":
                return _rpc_error(req_id, ERR_METHOD_NOT_FOUND, f"unknown tool: {tool_name}")

            if not _check_rate_limit(session):
                await metrics.inc("rate_limited")
                return _rpc_error(req_id, ERR_RATE_LIMITED, "rate limited")

            if session["concurrent"] >= MAX_CONCURRENT_PER_SESSION:
                await metrics.inc("concurrent_limited")
                return _rpc_error(req_id, ERR_RATE_LIMITED, "too many concurrent calls")

            receiver = args.get("receiver", "")
            action = args.get("action")
            params_inner = args.get("params", {})

            if not receiver:
                return _rpc_error(req_id, ERR_INVALID_PARAMS, "receiver required")

            if not _check_permission(session, receiver, action):
                await metrics.inc("permission_denied")
                return _rpc_error(req_id, ERR_FORBIDDEN, f"permission denied: {receiver}")

            session["concurrent"] += 1
            session["calls"] += 1
            start = time.time()
            try:
                result = await _call_internal(session, receiver, action=action, params=params_inner)
                is_error = not result.get("ok", False)
                if is_error:
                    session["errors"] += 1
                return _rpc_ok(req_id, _to_mcp_content(result, is_error))
            finally:
                session["concurrent"] -= 1
                elapsed = (time.time() - start) * 1000
                await _log("info", "tool_call",
                           session=session["session_id"],
                           user=session["user_id"],
                           receiver=receiver,
                           action=action,
                           ms=round(elapsed, 1))

        return _rpc_error(req_id, ERR_METHOD_NOT_FOUND, f"unknown method: {method}")

    async def handle_delete(request):
        session_id = request.headers.get("Mcp-Session-Id", "")
        if not session_id:
            return web.json_response({"error": "Mcp-Session-Id required"}, status=400)
        removed = await store.remove(session_id)
        if removed:
            return web.json_response({"ok": True, "session": session_id})
        return web.json_response({"error": "session not found"}, status=404)

    async def handle_health(request):
        return web.json_response({"status": "ok", **await store.stats()})

    async def handle_ready(request):
        try:
            ready = "os/_mcp_server" in core.plugins
            return web.json_response({"ready": ready, "protocol": PROTOCOL_VERSION})
        except Exception as e:
            return web.json_response({"ready": False, "error": str(e)}, status=503)

    async def handle_metrics(request):
        return web.json_response(await metrics.snapshot())

    app.router.add_post("/mcp", handle_mcp)
    app.router.add_delete("/mcp", handle_delete)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/ready", handle_ready)
    app.router.add_get("/metrics", handle_metrics)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()

    agent._mcp_runner = runner

    async def _cleanup_loop():
        while True:
            try:
                await asyncio.sleep(SESSION_CLEANUP_INTERVAL)
                n = await store.cleanup()
                if n > 0:
                    await _log("info", "session_cleanup", removed=n)
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    agent._mcp_cleanup_task = asyncio.create_task(_cleanup_loop())

    agent.log.info(f"[MCP] Server listening on http://{host}:{port}/mcp")

    envelop.payload = {
        "ok": True,
        "status": "listening",
        "host": host,
        "port": port,
        "url": f"http://{host}:{port}/mcp",
        "protocol": PROTOCOL_VERSION,
    }
    return envelop


async def _stop(envelop, agent):
    store = getattr(agent, "_mcp_sessions", None)
    if store:
        for sid in list(store.sessions.keys()):
            await store.remove(sid)

    task = getattr(agent, "_mcp_cleanup_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        agent._mcp_cleanup_task = None

    runner = getattr(agent, "_mcp_runner", None)
    if runner:
        try:
            await runner.cleanup()
        except Exception:
            pass
        agent._mcp_runner = None

    envelop.payload = {"ok": True, "status": "stopped"}
    return envelop


def help():
    return {
        "route": "os/_mcp_server",
        "description": "MCP Server，只暴露 call_plugin，认证走 os/_auth",
        "actions": {
            "START": {"input": {"host": "默认 127.0.0.1", "port": "默认 9100"}, "output": {"ok": True, "url": "http://host:port/mcp"}},
            "STOP": {"output": {"ok": True, "status": "stopped"}},
            "sessions": {"output": {"ok": True, "data": "session 统计"}},
            "metrics": {"output": {"ok": True, "data": "指标快照"}},
        },
        "mcp_tools": ["call_plugin"],
        "protocol_version": PROTOCOL_VERSION,
    }
