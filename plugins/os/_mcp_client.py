"""os/_mcp_client — MCP 客户端（反向接入：把外部 MCP Server 接进 AICP 体内）

契约统一：
    - HTTP 与 stdio 两条 transport 写出的 tools/<server>.json 与
      contracts/<server>.<tool>.json 结构完全一致（均为完整
      name/description/inputSchema）。
    - disconnect 后：state.connected=False、tools 缓存标记 stale、
      虚拟路由全部下掉。list_tools / cogitor 搜索都看不到该 server，
      不会出现「列表里没有但路由还在、调用又调不了」的不一致。
"""

import asyncio
import json
import os
import shlex
import socket
import subprocess
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

LAUNCH_DIR = DATA_DIR / "launch"
LAUNCH_LOG = LAUNCH_DIR / "launch.log"
LAUNCH_READY_TIMEOUT = 30

ACTIONS = ["connect", "disconnect", "remove_server", "list_servers", "list_tools",
           "call_tool", "refresh", "launch", "stop",
           "get_config", "update_server"]


def _is_being_cancelled() -> bool:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return False
    if task is None:
        return False
    try:
        if task.cancelling() > 0:
            return True
    except AttributeError:
        if task.cancelled():
            return True
    if task.cancelled():
        return True
    try:
        import sys
        f = sys._getframe()
        depth = 0
        while f and depth < 50:
            if f.f_code.co_name in ("wait_for", "timeout"):
                return True
            f = f.f_back
            depth += 1
    except Exception:
        pass
    return False


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


# ============================================================
# 统一的 tools 缓存/契约写入（HTTP 与 stdio 共用）
# ============================================================

def _write_tools_artifacts(server, tools, transport, base_or_url,
                           contract_source_fields=None):
    """统一写 tools/<server>.json + contracts/<server>.<tool>.json。

    tools 必须是完整对象数组（每个带 name/description/inputSchema）。
    这是 HTTP 与 stdio 的共同出口，保证两条路径产出的契约完全一致。
    """
    normalized = []
    for t in (tools or []):
        if isinstance(t, str):
            # 兼容：万一上游只给了名字，包成最小对象
            normalized.append({"name": t})
        elif isinstance(t, dict):
            normalized.append(t)
    # 只保留带 name 的
    normalized = [t for t in normalized if str(t.get("name") or "").strip()]

    _save(TOOLS_DIR / (server + ".json"), {
        "server": server,
        "fetched_at": time.time(),
        "base": base_or_url,
        "transport": transport,
        "stale": False,
        "tools": normalized,
    })

    for t in normalized:
        name = str(t.get("name") or "").strip()
        if not name:
            continue
        contract = {
            "server": server,
            "tool": name,
            "description": t.get("description") or "",
            "inputSchema": t.get("inputSchema") or {},
            "source": "mcp",
            "transport": transport,
            "fetched_at": time.time(),
        }
        if transport == "http":
            contract["url"] = base_or_url
        if contract_source_fields:
            for k, v in contract_source_fields.items():
                contract.setdefault(k, v)
        _save(CONTRACTS_DIR / ("%s.%s.json" % (server, name)), contract)


def _mark_cache_stale(server):
    """断开时标记缓存 stale：list_tools 跳过，call_tool 视作 need_connect。"""
    p = TOOLS_DIR / (server + ".json")
    cache = _load(p, {})
    if not cache:
        return
    cache["stale"] = True
    cache["stale_at"] = time.time()
    _save(p, cache)


# ============================================================
# HTTP transport
# ============================================================

async def _parse_sse(resp):
    try:
        text = await resp.text()
    except Exception:
        return {}
    data_lines = []
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif line == "" and data_lines:
            break
    payload_text = "\n".join(data_lines).strip()
    if not payload_text:
        return {}
    try:
        return json.loads(payload_text)
    except (json.JSONDecodeError, ValueError):
        return {"_raw_text": payload_text[:2000]}


def _is_local(url):
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host in ("127.0.0.1", "localhost", "0.0.0.0", "::1") or host.startswith("169.254.")


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
    except asyncio.CancelledError:
        if _is_being_cancelled():
            raise
        return {"status": 0, "session_id": session_id, "payload": {},
                "error": "rpc cancelled (internal)", "_internal_cancel": True}
    except asyncio.TimeoutError:
        return {"status": 0, "session_id": session_id, "payload": {},
                "error": "timeout after %ss" % timeout}
    except Exception as e:
        return {"status": 0, "session_id": session_id, "payload": {},
                "error": "%s: %s" % (type(e).__name__, e)}


async def _health(base, timeout=HEALTH_TIMEOUT):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as sess:
            async with sess.get(base + "/health") as resp:
                txt = await resp.text()
                return {"status": resp.status, "body": txt[:300]}
    except asyncio.CancelledError:
        if _is_being_cancelled():
            raise
        return {"status": 0, "error": "health cancelled (internal)",
                "_internal_cancel": True}
    except asyncio.TimeoutError:
        return {"status": 0, "error": "health timeout"}
    except Exception as e:
        return {"status": 0, "error": "%s: %s" % (type(e).__name__, e)}


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
            out["error"] = (parsed if isinstance(parsed, str)
                            else json.dumps(parsed, ensure_ascii=False)[:2000])
        return out
    out = {"ok": not is_error, "data": result}
    if is_error:
        out["error"] = "tool returned isError"
    return out


def _extract(resp):
    if resp.get("error") and not resp.get("status"):
        return {"ok": False, "error": resp["error"], "status": 0}
    payload = resp.get("payload") or {}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "bad response type", "status": resp.get("status")}
    err = payload.get("error")
    if err:
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return {"ok": False, "error": msg or "jsonrpc error",
                "code": (err.get("code") if isinstance(err, dict) else None),
                "status": resp.get("status")}
    result = payload.get("result")
    if result is None:
        return {"ok": False, "error": "empty result (status %s)" % resp.get("status"),
                "status": resp.get("status")}
    shaped = _shape_result(result)
    shaped["status"] = resp.get("status")
    return shaped


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
        return {"ok": False,
                "error": "initialize failed: %s" % init_res.get("error"),
                "notes": notes}

    sid = init.get("session_id") or ""
    if not sid:
        notes.append("no Mcp-Session-Id header")

    server_info = {}
    payload_result = (init.get("payload") or {}).get("result") or {}
    if isinstance(payload_result, dict):
        server_info = payload_result.get("serverInfo") or {}

    try:
        await _rpc(base, token, sid, "notifications/initialized", {}, timeout)
    except asyncio.CancelledError:
        if _is_being_cancelled():
            raise
    except Exception:
        pass

    tools, cursor, pages = [], None, 0
    while True:
        params = {"cursor": cursor} if cursor else {}
        r = await _rpc(base, token, sid, "tools/list", params, timeout)
        got = _extract(r)
        if not got.get("ok"):
            return {"ok": False,
                    "error": "tools/list failed: %s" % got.get("error"),
                    "notes": notes}
        data = got.get("data") or {}
        if isinstance(data, dict):
            tools.extend([t for t in (data.get("tools") or [])
                          if isinstance(t, dict)])
            cursor = data.get("nextCursor")
        else:
            cursor = None
        pages += 1
        if not cursor or pages >= 20:
            break

    # ★ 统一出口：HTTP 与 stdio 共用同一个写盘函数
    _write_tools_artifacts(server, tools, transport="http", base_or_url=base)

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
        "transport": "http",
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
        "tools": [t.get("name") for t in tools],   # 对外仍返回名字列表，兼容前端
        "notes": notes,
    }


# ============================================================
# HTTP 拉起（保持不变）
# ============================================================

def _launch_cfg(server):
    cfg = _servers().get(server) or {}
    lc = cfg.get("launch") or {}
    if not isinstance(lc, dict):
        return {}
    cmd = (lc.get("command") or "").strip()
    if not cmd:
        return {}
    return lc


def _launch_state_path(server):
    return LAUNCH_DIR / (server + ".json")


def _load_launch_state(server):
    return _load(_launch_state_path(server), {})


def _save_launch_state(server, data):
    _save(_launch_state_path(server), data)


def _pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                                 capture_output=True, text=True, timeout=10)
            return str(pid) in (out.stdout or "")
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _port_alive(port, timeout=1.5):
    try:
        port = int(port)
    except (TypeError, ValueError):
        return False
    if port <= 0:
        return False
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


def _launch_url(server, lc, port):
    host = lc.get("host") or "127.0.0.1"
    tmpl = (lc.get("url_template") or "").strip()
    if tmpl:
        try:
            return tmpl.format(port=port, host=host, server=server)
        except (KeyError, IndexError, ValueError):
            pass
    url = (lc.get("url") or "").strip()
    if not url:
        return ""
    if "{port}" in url:
        try:
            return url.format(port=port, host=host, server=server)
        except (KeyError, IndexError, ValueError):
            return ""
    return url


def _split_command(cmd):
    """把命令字符串拆成参数列表，Windows/Unix 行为一致。

    Windows: 走 CommandLineToArgvW 等价语义——双引号内的空格不切分。
    Unix: 走 shlex。
    """
    cmd = (cmd or "").strip()
    if not cmd:
        return []
    if os.name != "nt":
        try:
            return shlex.split(cmd)
        except ValueError:
            return cmd.split()
    argv = []
    cur = []
    in_quotes = False
    has_token = False
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        if ch == "\\":
            if i + 1 < n and cmd[i + 1] == '"':
                cur.append('"')
                i += 2
                has_token = True
                continue
            cur.append(ch)
            i += 1
            has_token = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
            has_token = True
            i += 1
            continue
        if ch in " \t" and not in_quotes:
            if has_token:
                argv.append("".join(cur))
                cur = []
                has_token = False
            i += 1
            continue
        cur.append(ch)
        has_token = True
        i += 1
    if has_token:
        argv.append("".join(cur))
    return argv


def _launch_argv(lc):
    """解析启动命令行：command 字符串 + 可选的 args 数组。

    args 逐项作为独立参数追加，不做 shell 拆分/去引号处理。
    """
    cmd = (lc.get("command") or "").strip()
    if not cmd:
        return []
    argv = _split_command(cmd)
    extra = lc.get("args")
    if isinstance(extra, (list, tuple)):
        for a in extra:
            if a is None:
                continue
            argv.append(str(a))
    host = lc.get("host") or "127.0.0.1"
    port = lc.get("port") or ""
    out = []
    for a in argv:
        a = a.replace("{port}", str(port)).replace("{host}", str(host))
        out.append(a)
    return out


async def _launch_start(server, lc):
    res = {"ok": False, "server": server}
    argv = _launch_argv(lc)
    if not argv:
        res["error"] = "launch.command 为空"
        return res

    env = os.environ.copy()
    for k, v in (lc.get("env") or {}).items():
        env[str(k)] = str(v)
    if lc.get("port"):
        env[str(lc.get("port_env") or "MCP_PORT")] = str(lc["port"])
    if lc.get("host"):
        env[str(lc.get("host_env") or "MCP_HOST")] = str(lc["host"])
    if lc.get("cwd"):
        env["__CWD__"] = str(lc["cwd"])

    LAUNCH_DIR.mkdir(parents=True, exist_ok=True)
    log = open(str(LAUNCH_LOG), "ab")
    kwargs = {"stdout": log, "stderr": log, "stdin": subprocess.DEVNULL, "env": env}
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008)
    else:
        kwargs["start_new_session"] = True
    cwd = lc.get("cwd") or None
    try:
        proc = subprocess.Popen(argv, cwd=cwd, **kwargs)
    except Exception as e:
        res["error"] = "启动失败: %s: %s" % (type(e).__name__, e)
        return res

    port = lc.get("port") or ""
    if port:
        port = int(port)
        deadline = time.time() + int(lc.get("ready_timeout") or LAUNCH_READY_TIMEOUT)
        while time.time() < deadline:
            if _port_alive(port):
                break
            if proc.poll() is not None:
                res["error"] = "进程启动后立即退出 (code=%s)" % proc.returncode
                res["log"] = str(LAUNCH_LOG)
                return res
            await asyncio.sleep(0.4)
        else:
            res["error"] = "等待端口 %s 就绪超时" % port
            res["log"] = str(LAUNCH_LOG)
            return res

    url = _launch_url(server, lc, port) if port else _launch_url(server, lc, "")
    _save_launch_state(server, {
        "pid": proc.pid,
        "port": port,
        "url": url,
        "command": lc.get("command"),
        "started_at": time.time(),
    })
    res.update({"ok": True, "pid": proc.pid, "port": port, "url": url,
                "log": str(LAUNCH_LOG)})
    return res


async def _launch_stop(server):
    st = _load_launch_state(server)
    pid = st.get("pid")
    killed = False
    if pid and _pid_alive(pid):
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                               capture_output=True, text=True, timeout=15)
            else:
                os.killpg(os.getpgid(int(pid)), 15)
            killed = True
        except Exception:
            try:
                os.kill(int(pid), 15)
                killed = True
            except Exception:
                pass
    st.update({"pid": 0, "stopped_at": time.time()})
    _save_launch_state(server, st)
    return {"ok": True, "server": server, "stopped": killed, "pid": pid or 0}


async def _launch_ensure(server, agent=None):
    lc = _launch_cfg(server)
    if not lc:
        return {"ok": False, "error": "server '%s' 未配置 launch.command" % server}
    st = _load_launch_state(server)
    port = lc.get("port") or st.get("port") or ""
    pid = st.get("pid")
    if pid and _pid_alive(pid) and (not port or _port_alive(port)):
        return {"ok": True, "server": server, "started": False, "pid": pid,
                "port": port, "url": _launch_url(server, lc, port)}
    r = await _launch_start(server, lc)
    r["started"] = r.get("ok")
    return r


async def _launch_apply(server, lc, url):
    if not url:
        return
    servers = _servers()
    cfg = servers.get(server)
    if not isinstance(cfg, dict):
        cfg = {}
    if cfg.get("url") == url:
        return
    cfg["url"] = url
    cfg.setdefault("enabled", True)
    servers[server] = cfg
    _save(SERVERS_FILE, servers)


# ============================================================
# stdio transport 转发
# ============================================================

async def _stdio_call(payload, agent=None, timeout=None):
    from core import Envelop
    if agent is None:
        return {"ok": False, "error": "agent is None，无法调用 os/_mcp_stdio"}
    env = Envelop(sender="os/_mcp_client", receiver="os/_mcp_stdio",
                  intent="API_CALL", payload=dict(payload or {}))
    try:
        if timeout is None:
            res = await agent.system.call(env)
        else:
            res = await agent.system.call(env, timeout=timeout)
    except asyncio.CancelledError:
        if _is_being_cancelled():
            raise
        try:
            agent.log.warning("[mcp_client] _stdio_call swallowed internal CancelledError")
        except Exception:
            pass
        return {"ok": False, "error": "stdio 调用被内部取消(非外部取消)",
                "_internal_cancel": True}
    except Exception as e:
        try:
            agent.log.warning("[mcp_client] _stdio_call error: %s: %s"
                              % (type(e).__name__, e))
        except Exception:
            pass
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    p = getattr(res, "payload", None)
    if isinstance(p, dict):
        return p
    return {"ok": False, "error": "stdio 调用无返回"}


async def _connect_stdio(server, cfg, payload, agent):
    lc = dict(cfg.get("launch") or {})
    command = (lc.get("command") or payload.get("command") or "").strip()
    if not command:
        return {"ok": False, "error": "stdio 模式缺少启动命令"}

    servers = _servers()
    cfg["protocol"] = "stdio"
    cfg["launch"] = lc
    servers[server] = cfg
    _save(SERVERS_FILE, servers)

    stdio_timeout = int(lc.get("timeout") or cfg.get("timeout") or DEFAULT_TIMEOUT)
    stdio_cfg = {
        "command": command,
        "args": lc.get("args"),
        "env": lc.get("env"),
        "cwd": lc.get("cwd"),
        "timeout": stdio_timeout,
    }
    res = await _stdio_call({"action": "connect", "server": server,
                             "config": stdio_cfg}, agent,
                            timeout=stdio_timeout + 15)
    if not res.get("ok"):
        # 失败：置 connected=False，缓存标 stale，下掉虚拟路由
        st = _state()
        cur = st.get(server) or {}
        cur.update({
            "session_id": "",
            "last_active": time.time(),
            "connected": False,
            "last_error": str(res.get("error"))[:300],
            "transport": "stdio",
            "pid": 0,
        })
        st[server] = cur
        _save_state(st)
        _mark_cache_stale(server)
        try:
            await _unregister_virtual({"server": server}, agent)
        except Exception:
            pass
        return res

    # ★ 回传的是完整对象数组（_mcp_stdio v4 已保证）
    tools = res.get("tools") or []
    _write_tools_artifacts(server, tools, transport="stdio",
                           base_or_url=res.get("log") or "stdio")

    st = _state()
    cur = st.get(server) or {}
    cur.update({
        "session_id": "",
        "last_active": time.time(),
        "fail_count": 0,
        "circuit_open_until": 0,
        "connected": True,
        "last_error": "",
        "tool_count": len([t for t in tools
                           if isinstance(t, dict) and str(t.get("name") or "").strip()]),
        "transport": "stdio",
        "pid": res.get("pid") or 0,
    })
    st[server] = cur
    _save_state(st)

    out = dict(res)
    out["server"] = server
    out["tools"] = [t.get("name") if isinstance(t, dict) else t for t in tools]
    return out


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
        connected = bool(s.get("connected")) and not cache.get("stale")
        out.append({
            "server": name,
            "url": cfg.get("url"),
            "protocol": (cfg.get("protocol") or "http"),
            "enabled": cfg.get("enabled", True),
            "connected": connected,
            "session_id": s.get("session_id") or "",
            "stateless": connected and not bool(s.get("session_id")),
            "tool_count": len(cache.get("tools") or []) if connected else 0,
            "tools_fetched_at": cache.get("fetched_at"),
            "stale": bool(cache.get("stale")),
            "circuit_open_until": s.get("circuit_open_until") or 0,
            "last_error": s.get("last_error") or "",
            "launch": bool(cfg.get("launch") and cfg["launch"].get("command")),
            "launch_command": (cfg.get("launch") or {}).get("command") or "",
            "launch_running": bool((_load_launch_state(name) or {}).get("pid")
                                   and _pid_alive((_load_launch_state(name) or {}).get("pid"))),
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
    lc = payload.get("launch")
    if isinstance(lc, dict) and (lc.get("command") or "").strip():
        merged = dict(cfg.get("launch") or {})
        merged.update({k: v for k, v in lc.items() if v is not None})
        cfg["launch"] = merged
    if url or isinstance(lc, dict):
        servers[server] = cfg
        _save(SERVERS_FILE, servers)

    if (payload.get("protocol") or cfg.get("protocol") or "").strip().lower() == "stdio":
        return await _connect_stdio(server, cfg, payload, agent)

    if not cfg.get("url"):
        return {"ok": False, "error": "server '%s' 未注册 url，请传 url" % server}

    launch_res = None
    if _launch_cfg(server):
        launch_res = await _launch_ensure(server, agent)
        if launch_res.get("ok"):
            new_url = launch_res.get("url")
            if new_url:
                cfg = _servers().get(server) or cfg
                cfg["url"] = new_url
                servers[server] = cfg
                _save(SERVERS_FILE, servers)
        elif not payload.get("skip_launch"):
            return {"ok": False, "error": "拉起失败: %s" % launch_res.get("error"),
                    "launch": launch_res}

    res = await _do_connect(server, cfg, agent)
    if launch_res is not None:
        res["launch"] = launch_res
    return res


async def _get_config(payload, agent=None):
    """取某个 server 的完整配置（供前端 JSON 编辑器回填）。"""
    server = (payload.get("server") or "").strip()
    if not server:
        return {"ok": False, "error": "缺少 server"}
    servers = _servers()
    if server not in servers:
        return {"ok": False, "error": "server '%s' 不存在" % server}
    return {"ok": True, "server": server, "config": servers.get(server) or {}}


async def _update_server(payload, agent=None):
    """整体覆盖某个 server 的配置（前端 JSON 编辑器保存）。

    config 里的 null 值表示删除该字段；server 键不可改名。
    """
    server = (payload.get("server") or "").strip()
    if not server:
        return {"ok": False, "error": "缺少 server"}
    cfg = payload.get("config")
    if not isinstance(cfg, dict):
        return {"ok": False, "error": "config 必须是对象"}
    servers = _servers()
    if server not in servers:
        return {"ok": False, "error": "server '%s' 不存在" % server}
    merged = dict(servers.get(server) or {})
    for k, v in cfg.items():
        if v is None:
            merged.pop(k, None)
        else:
            merged[k] = v
    merged.pop("server", None)
    servers[server] = merged
    _save(SERVERS_FILE, servers)
    return {"ok": True, "server": server, "config": merged}


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
        is_stdio = (cfg.get("protocol") or "").strip().lower() == "stdio"
        if is_stdio:
            # stdio：直接走 connect 重拉
            r = await _connect_stdio(name, cfg, {"protocol": "stdio"}, agent)
        else:
            if not cfg.get("url"):
                results.append({"server": name, "ok": False, "error": "缺少 url"})
                continue
            launch_res = None
            if _launch_cfg(name):
                launch_res = await _launch_ensure(name, agent)
                if launch_res.get("ok") and launch_res.get("url"):
                    cfg = _servers().get(name) or cfg
                    cfg["url"] = launch_res["url"]
                    servers[name] = cfg
                    _save(SERVERS_FILE, servers)
                elif not launch_res.get("ok"):
                    results.append({"server": name, "ok": False,
                                    "error": "拉起失败: %s" % launch_res.get("error")})
                    continue
            r = await _do_connect(name, cfg, agent)
            if launch_res is not None:
                r["launch"] = launch_res
        results.append({"server": name, "ok": r.get("ok"),
                        "tool_count": r.get("tool_count"),
                        "error": r.get("error")})
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
    try:
        await _launch_stop(server)
    except Exception:
        pass
    st = _state()
    if server in st:
        del st[server]
        _save_state(st)
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
    virtual = await _unregister_virtual({"server": server}, agent)
    return {"ok": True, "server": server, "virtual": virtual}


async def _disconnect(payload, agent=None):
    """断开：state=false + 缓存 stale + 下掉虚拟路由，三件事同步。"""
    server = (payload.get("server") or "").strip()
    servers = _servers()
    if not server:
        return {"ok": False, "error": "缺少 server"}
    cfg = servers.get(server) or {}
    is_stdio = (cfg.get("protocol") or "").strip().lower() == "stdio"

    launch_res = None
    if is_stdio:
        launch_res = await _stdio_call({"action": "disconnect", "server": server}, agent)
    else:
        st = _state()
        cur = st.get(server) or {}
        sid = cur.get("session_id") or ""
        base = _base_url(cfg.get("url"))
        deleted = False
        if base and sid:
            try:
                headers = {"Mcp-Session-Id": sid,
                           "Accept": "application/json, text/event-stream"}
                if cfg.get("token"):
                    headers["Authorization"] = "Bearer " + cfg["token"]
                async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=10)) as sess:
                    async with sess.delete(base + "/mcp", headers=headers) as resp:
                        deleted = resp.status < 400
            except asyncio.CancelledError:
                if _is_being_cancelled():
                    raise
            except Exception:
                pass
        if _launch_cfg(server):
            try:
                launch_res = await _launch_stop(server)
            except Exception as e:
                launch_res = {"ok": False, "error": str(e)}

    # ★ 同步三件事
    st = _state()
    cur = st.get(server) or {}
    cur.update({"session_id": "", "connected": False,
                "last_active": time.time(), "pid": 0})
    st[server] = cur
    _save_state(st)
    _mark_cache_stale(server)
    await _unregister_virtual({"server": server}, agent)

    return {"ok": True, "server": server, "protocol": "stdio" if is_stdio else "http",
            "tools_cache_kept": True, "stale": True,
            "virtual_unregistered": True, "launch": launch_res}


async def _list_tools(payload, agent=None):
    """列工具：只返回 connected 且 non-stale 的 server。

    与 disconnect 的 stale 标记配合，保证「断开即查不到」。
    """
    await _prune_orphan_routes(agent)
    server = (payload.get("server") or "").strip()
    servers = _servers()
    st = _state()
    names = [server] if server else list(servers.keys())
    include_disconnected = bool(payload.get("include_disconnected", False))
    out = []
    for name in names:
        cache = _load(TOOLS_DIR / (name + ".json"), {})
        s = st.get(name) or {}
        # ★ 一致性检查：未连接 或 缓存 stale 一律不返回
        if not include_disconnected:
            if not s.get("connected") or cache.get("stale"):
                continue
        for t in (cache.get("tools") or []):
            out.append({
                "server": name,
                "tool": t.get("name"),
                "description": (t.get("description") or "")[:300],
                "inputSchema": t.get("inputSchema") or {},
            })
    return {"ok": True, "data": out, "total": len(out)}


async def _launch_dispatch(server, payload, agent):
    cfg = _servers().get(server) or {}
    if (cfg.get("protocol") or "").strip().lower() == "stdio":
        cmd = (payload.get("command")
               or (cfg.get("launch") or {}).get("command") or "").strip()
        if not cmd:
            return {"ok": False, "error": "stdio 模式缺少启动命令"}
        lc2 = cfg.get("launch") or {}
        stdio_timeout = int(cfg.get("timeout") or lc2.get("timeout") or DEFAULT_TIMEOUT)
        stdio_cfg = {"command": cmd,
                     "args": lc2.get("args"),
                     "env": lc2.get("env"),
                     "cwd": lc2.get("cwd"),
                     "timeout": stdio_timeout}
        res = await _stdio_call({"action": "connect", "server": server,
                                 "config": stdio_cfg}, agent,
                                timeout=stdio_timeout + 15)
        if res.get("ok"):
            res.setdefault("started", True)
            res["transport"] = "stdio"
        return res
    return await _launch_ensure(server, agent)


async def _stop_dispatch(server, agent):
    cfg = _servers().get(server) or {}
    if (cfg.get("protocol") or "").strip().lower() == "stdio":
        return await _stdio_call({"action": "disconnect", "server": server}, agent)
    return await _launch_stop(server)


async def _call_tool_stdio(server, tool, args, cfg, agent):
    ct_timeout = int((cfg.get("launch") or {}).get("timeout")
                     or cfg.get("timeout") or DEFAULT_TIMEOUT)
    res = await _stdio_call({"action": "call_tool", "server": server,
                             "tool": tool, "arguments": args}, agent,
                            timeout=ct_timeout + 15)
    if not isinstance(res, dict):
        return {"ok": False, "error": "stdio 调用无返回"}
    if res.get("ok"):
        st = _state()
        cur = st.get(server) or {}
        cur.update({"fail_count": 0, "circuit_open_until": 0,
                    "connected": True, "last_active": time.time(),
                    "last_error": ""})
        st[server] = cur
        _save_state(st)
    else:
        st = _state()
        cur = st.get(server) or {}
        fc = int(cur.get("fail_count") or 0) + 1
        cur["fail_count"] = fc
        cur["last_error"] = str(res.get("error"))[:300]
        if fc >= CIRCUIT_THRESHOLD:
            cur["circuit_open_until"] = time.time() + CIRCUIT_COOLDOWN
            cur["fail_count"] = 0
            res["circuit_opened"] = True
        st[server] = cur
        _save_state(st)
    res["server"] = server
    res["tool"] = tool
    res.setdefault("transport", "stdio")
    return res


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
    if not cfg:
        return {"ok": False, "error": "server '%s' 未注册，请先 connect" % server}
    if cfg.get("enabled") is False:
        return {"ok": False, "error": "server '%s' 已禁用" % server}

    st = _state()
    cur = st.get(server) or {}
    cache = _load(TOOLS_DIR / (server + ".json"), {})
    # ★ stale 缓存不可用：视为需要重连
    cache_stale = bool(cache.get("stale"))

    if (cfg.get("protocol") or "").strip().lower() == "stdio":
        if cache_stale:
            # 自动重连一次
            conn = await _connect_stdio(server, cfg, {"protocol": "stdio"}, agent)
            if not conn.get("ok"):
                return {"ok": False,
                        "error": "stdio reconnect failed: %s" % conn.get("error")}
        return await _call_tool_stdio(server, tool, args, cfg, agent)

    if not cfg.get("url"):
        return {"ok": False,
                "error": "server '%s' 未注册 url，请先 connect 并传 url" % server}

    now = time.time()
    if (cur.get("circuit_open_until") or 0) > now:
        return {"ok": False,
                "error": "circuit open（%.0fs 后重试）"
                         % (cur["circuit_open_until"] - now)}

    ttl = int(cfg.get("ttl") or DEFAULT_TTL)
    timeout = int(cfg.get("timeout") or DEFAULT_TIMEOUT)
    base = _base_url(cfg.get("url"))
    token = cfg.get("token") or ""

    cached_tools = cache.get("tools") or []
    cached_names = {str(t.get("name")) for t in cached_tools
                    if isinstance(t, dict)}
    need_connect = (not cache) or (not cur.get("connected")) or cache_stale
    need_refresh = (not need_connect) and (
        (now - float(cache.get("fetched_at") or 0)) > ttl)
    if need_connect or need_refresh or (tool not in cached_names):
        conn = await _do_connect(server, cfg, agent)
        if not conn.get("ok"):
            return {"ok": False,
                    "error": "connect/refresh failed: %s" % conn.get("error")}
        st = _state()
        cur = st.get(server) or {}
        cache = _load(TOOLS_DIR / (server + ".json"), {})
        cached_names = {str(t.get("name"))
                        for t in (cache.get("tools") or [])
                        if isinstance(t, dict)}
        if tool not in cached_names:
            return {"ok": False,
                    "error": "server '%s' 无工具 '%s'" % (server, tool),
                    "available": sorted(cached_names)}

    sid = cur.get("session_id") or ""
    resp = await _rpc(base, token, sid, "tools/call",
                      {"name": tool, "arguments": args}, timeout)
    status = resp.get("status") or 0

    if status in (401, 403) or (
            isinstance(resp.get("payload"), dict)
            and (resp["payload"].get("error") or {}).get("code") == -32013):
        conn = await _do_connect(server, cfg, agent)
        if conn.get("ok"):
            st = _state()
            cur = st.get(server) or {}
            resp = await _rpc(base, token, cur.get("session_id") or "",
                              "tools/call",
                              {"name": tool, "arguments": args}, timeout)
            status = resp.get("status") or 0

    got = _extract(resp)

    st = _state()
    cur = st.get(server) or {}
    if got.get("ok"):
        cur.update({"fail_count": 0, "circuit_open_until": 0,
                    "connected": True, "last_active": time.time(),
                    "last_error": ""})
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
# 虚拟插件注册
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
                        if k not in ("action", "payload", "server", "tool",
                                     "arguments")}
            else:
                args = {k: v for k, v in args.items()
                        if k not in ("action", "payload", "server", "tool")}
        except Exception as e:
            envelop.payload = {"ok": False,
                               "error": "bad arguments: %s: %s"
                                        % (type(e).__name__, e)}
            return envelop

        try:
            result = await _call_tool(
                {"server": server, "tool": tool, "arguments": args}, agent)
            envelop.payload = result if isinstance(result, dict) else {
                "ok": False, "error": "non-dict result from _call_tool"}
        except asyncio.CancelledError:
            if _is_being_cancelled():
                raise
            try:
                if agent is not None and getattr(agent, "log", None):
                    agent.log.warning(
                        "[mcp_client] _handler swallowed internal CancelledError "
                        "server=%s tool=%s" % (server, tool))
            except Exception:
                pass
            envelop.payload = {
                "ok": False,
                "error": "mcp tool '%s/%s' 被内部取消(非外部取消)" % (server, tool),
                "_internal_cancel": True,
            }
        except Exception as e:
            try:
                if agent is not None and getattr(agent, "log", None):
                    agent.log.warning(
                        "[mcp_client] _handler error server=%s tool=%s: %s: %s"
                        % (server, tool, type(e).__name__, e))
            except Exception:
                pass
            envelop.payload = {"ok": False,
                               "error": "%s: %s" % (type(e).__name__, e)}

        return envelop
    return _handler


def _register_server_tools(server):
    import core as _core
    cache = _load(TOOLS_DIR / (server + ".json"), {})
    if cache.get("stale"):
        return []   # stale 缓存不注册
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
    await _prune_orphan_routes(agent)
    server = (payload.get("server") or "").strip()
    names = [server] if server else list(_servers().keys())
    for s in names:
        await _unregister_virtual({"server": s}, agent)
    items = []
    for s in names:
        items.extend(_register_server_tools(s))
    if items:
        try:
            env = _core.Envelop(sender="os/_mcp_client", receiver="os/_registry",
                                intent="API_CALL",
                                payload={"action": "register_virtual",
                                         "items": items})
            await agent.system.call(env)
        except asyncio.CancelledError:
            if _is_being_cancelled():
                raise
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
                            payload={"action": "unregister_virtual",
                                     "prefix": prefix})
        await agent.system.call(env)
    except asyncio.CancelledError:
        if _is_being_cancelled():
            raise
    except Exception as e:
        try:
            agent.log.warning("[mcp_client] unregister_virtual failed: %s" % e)
        except Exception:
            pass
    return {"ok": True, "server": server, "prefix": prefix, "removed": removed}


async def _prune_orphan_routes(agent):
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
                    res["virtual"] = await _register_virtual(
                        {"server": res.get("server")}, agent)
                except asyncio.CancelledError:
                    if _is_being_cancelled():
                        raise
                    res["virtual_error"] = "internal cancel during register"
                except Exception as e:
                    res["virtual_error"] = str(e)
            envelop.payload = res
        elif action == "remove_server":
            envelop.payload = await _remove_server(payload, agent)
        elif action == "disconnect":
            envelop.payload = await _disconnect(payload, agent)
        elif action == "list_tools":
            envelop.payload = await _list_tools(payload, agent)
        elif action == "call_tool":
            envelop.payload = await _call_tool(payload, agent)
        elif action == "get_config":
            envelop.payload = await _get_config(payload, agent)
        elif action == "update_server":
            envelop.payload = await _update_server(payload, agent)
        elif action == "refresh":
            envelop.payload = await _refresh(payload, agent)
        elif action == "register_virtual":
            envelop.payload = await _register_virtual(payload, agent)
        elif action == "unregister_virtual":
            envelop.payload = await _unregister_virtual(payload, agent)
        elif action == "launch":
            name = (payload.get("server") or "").strip()
            if not name:
                envelop.payload = {"ok": False, "error": "缺少 server"}
            else:
                envelop.payload = await _launch_dispatch(name, payload, agent)
        elif action == "stop":
            name = (payload.get("server") or "").strip()
            if not name:
                envelop.payload = {"ok": False, "error": "缺少 server"}
            else:
                envelop.payload = await _stop_dispatch(name, agent)
        else:
            envelop.payload = {
                "ok": False,
                "error": "Unknown action: %s" % action,
                "actions": ACTIONS,
            }
        return envelop

    except asyncio.CancelledError:
        if _is_being_cancelled():
            raise
        try:
            agent.log.warning("[mcp_client] execute swallowed internal CancelledError")
        except Exception:
            pass
        envelop.payload = {"ok": False, "error": "internal cancel",
                           "_internal_cancel": True}
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
            "connect": {"input": {"server": "名字",
                                 "url": "http://host:port/mcp（stdio 可省略）",
                                 "protocol": "http|stdio",
                                 "token": "可选", "ttl": "可选", "timeout": "可选",
                                 "launch": "可选 {command,env,cwd}"}},
            "list_servers": {},
            "list_tools": {"input": {"server": "可选，不传=全部",
                                     "include_disconnected": "可选，默认 False"}},
            "call_tool": {"input": {"server": "名字", "tool": "工具名",
                                    "arguments": {}}},
            "refresh": {"input": {"server": "可选"}},
            "disconnect": {"input": {"server": "名字"}},
            "remove_server": {"input": {"server": "名字"}},
            "launch": {"input": {"server": "名字"}},
            "stop": {"input": {"server": "名字"}},
        },
    }
