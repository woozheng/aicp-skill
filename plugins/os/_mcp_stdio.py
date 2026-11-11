"""os/_mcp_stdio — 通用 stdio transport MCP 适配器（跨平台健壮版 v4）

与 _mcp_client 的契约：
    connect 返回的 tools 必须是完整对象数组（每个带 name/description/inputSchema），
    与 HTTP transport 的 tools/list 结果同构。_mcp_client 据此写缓存与契约。
"""

import asyncio
import json
import os
import shlex
import subprocess
import time
import uuid
from pathlib import Path

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "aicp-mcp-stdio", "version": "1.0.0"}

DATA_DIR = Path("data/os/_mcp_stdio")
SESSION_DIR = DATA_DIR / "sessions"
LOG_DIR = DATA_DIR / "logs"

DEFAULT_READY_TIMEOUT = 30
DEFAULT_TIMEOUT = 60
MAX_RESULT_CHARS = 32000
LOG_BAK_KEEP = 3


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


def _swallow_cancel(tag, agent=None, exc=None):
    try:
        if agent is not None and getattr(agent, "log", None):
            agent.log.debug("[mcp_stdio] swallowed internal CancelledError: %s" % tag)
    except Exception:
        pass


# ============================================================
# 落盘
# ============================================================

def _ensure_dirs():
    for d in (DATA_DIR, SESSION_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _session_meta_path(server):
    return SESSION_DIR / (server + ".json")


def _log_path(server):
    return LOG_DIR / (server + ".log")


def _rotate_log(server):
    p = _log_path(server)
    if not p.exists() or p.stat().st_size == 0:
        return
    try:
        for i in range(LOG_BAK_KEEP, 0, -1):
            src = LOG_DIR / (server + ".log.bak%d" % i)
            dst = LOG_DIR / (server + ".log.bak%d" % (i + 1))
            if src.exists():
                try:
                    if dst.exists():
                        dst.unlink()
                    src.rename(dst)
                except OSError:
                    pass
        p.rename(LOG_DIR / (server + ".log.bak1"))
    except OSError:
        pass


def _write_meta(server, data):
    _ensure_dirs()
    try:
        _session_meta_path(server).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _read_meta(server):
    try:
        p = _session_meta_path(server)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return {}


def _argv(command):
    cmd = (command or "").strip()
    if not cmd:
        return []
    if os.name == "nt":
        try:
            parts = shlex.split(cmd, posix=False)
        except ValueError:
            parts = cmd.split()
        out = []
        for p in parts:
            if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'":
                p = p[1:-1]
            out.append(p)
        return out
    return shlex.split(cmd)


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


# ============================================================
# 平台差异收敛点
# ============================================================

def _spawn_kwargs(env, cwd, log_file):
    kw = {
        "stdin": asyncio.subprocess.PIPE,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": log_file,
        "cwd": cwd or None,
        "env": env,
    }
    if os.name == "nt":
        kw["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008)
    else:
        kw["start_new_session"] = True
    return kw


async def _kill_proc(proc):
    if proc is None:
        return False
    try:
        if proc.returncode is not None:
            return False
    except Exception:
        pass
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/F", "/T"],
                           capture_output=True, text=True, timeout=15)
        else:
            os.killpg(os.getpgid(proc.pid), 15)
        return True
    except Exception:
        try:
            proc.kill()
            return True
        except Exception:
            return False


# ============================================================
# 会话对象
# ============================================================

class StdioSession:
    def __init__(self, server, argv, env=None, cwd=None):
        self.server = server
        self.argv = argv
        self.env = env or {}
        self.cwd = cwd
        self.proc = None
        self.log_file = None
        self.pending = {}
        self.reader_task = None
        self.next_id = 0
        self.server_info = {}
        self.protocol_version = PROTOCOL_VERSION
        self.ready = False
        self.error = ""
        self.frame_mode = None
        self._buf = b""
        self._closed = False

    async def start(self, ready_timeout=DEFAULT_READY_TIMEOUT):
        _ensure_dirs()
        _rotate_log(self.server)
        log_path = _log_path(self.server)
        try:
            self.log_file = open(str(log_path), "ab")
        except OSError:
            self.log_file = subprocess.DEVNULL

        env = os.environ.copy()
        for k, v in self.env.items():
            env[str(k)] = str(v)

        kwargs = _spawn_kwargs(env, self.cwd, self.log_file)

        try:
            self.proc = await asyncio.create_subprocess_exec(*self.argv, **kwargs)
        except NotImplementedError as e:
            return {"ok": False,
                    "error": "event loop 不支持 subprocess（%s）；"
                             "Windows 请使用 ProactorEventLoop" % e}
        except Exception as e:
            return {"ok": False,
                    "error": "启动失败: %s: %s" % (type(e).__name__, e)}

        self.reader_task = asyncio.ensure_future(self._read_loop())
        return {"ok": True, "pid": self.proc.pid, "log": str(log_path)}

    async def stop(self):
        if self._closed:
            return {"ok": True, "stopped": False,
                    "pid": self.proc.pid if self.proc else 0}
        self._closed = True

        if self.reader_task is not None:
            rt = self.reader_task
            self.reader_task = None
            if not rt.done():
                rt.cancel()
            try:
                await rt
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        killed = False
        try:
            killed = await _kill_proc(self.proc)
        except Exception:
            pass

        if self.log_file and self.log_file is not subprocess.DEVNULL:
            try:
                self.log_file.close()
            except Exception:
                pass
            self.log_file = None

        for fut in list(self.pending.values()):
            if not fut.done():
                try:
                    fut.set_exception(RuntimeError("session closed"))
                except Exception:
                    try:
                        fut.cancel()
                    except Exception:
                        pass
        self.pending.clear()
        self.ready = False
        return {"ok": True, "stopped": killed,
                "pid": self.proc.pid if self.proc else 0}

    @property
    def alive(self):
        if self.proc is None:
            return False
        try:
            return self.proc.returncode is None
        except Exception:
            return False

    async def _read_loop(self):
        try:
            while True:
                chunk = await self.proc.stdout.read(4096)
                if not chunk:
                    break
                self._buf += chunk
                self._drain()
        except asyncio.CancelledError:
            self.error = self.error or "reader cancelled"
            raise
        except Exception as e:
            self.error = "reader: %s: %s" % (type(e).__name__, e)
        finally:
            for fut in list(self.pending.values()):
                if not fut.done():
                    try:
                        code = None
                        try:
                            code = self.proc.returncode if self.proc else "?"
                        except Exception:
                            pass
                        fut.set_exception(RuntimeError(
                            self.error or "mcp server 进程退出 (code=%s)" % code))
                    except Exception:
                        pass
            self.pending.clear()
            self.ready = False

    def _drain(self):
        if self.frame_mode != "line":
            consumed = self._try_content_length()
            if consumed:
                return
        while b"\n" in self._buf:
            line, _, rest = self._buf.partition(b"\n")
            self._buf = rest
            text = line.strip()
            if not text:
                continue
            self.frame_mode = self.frame_mode or "line"
            self._handle_text(text)

    def _try_content_length(self):
        while True:
            idx = self._buf.find(b"\r\n\r\n")
            if idx < 0:
                idx = self._buf.find(b"\n\n")
                if idx < 0:
                    return False
                header_blob = self._buf[:idx]
                sep = 2
            else:
                header_blob = self._buf[:idx]
                sep = 4
            try:
                header_text = header_blob.decode("utf-8", "replace")
            except Exception:
                return False
            length = None
            for line in header_text.splitlines():
                if ":" not in line:
                    continue
                k, _, v = line.partition(":")
                if k.strip().lower() == "content-length":
                    try:
                        length = int(v.strip())
                    except ValueError:
                        length = None
            if length is None:
                self.frame_mode = "line"
                return False
            body_start = idx + sep
            if len(self._buf) < body_start + length:
                return False
            body = self._buf[body_start:body_start + length]
            self._buf = self._buf[body_start + length:]
            self.frame_mode = "content-length"
            try:
                self._handle_text(body.decode("utf-8", "replace").strip())
            except Exception:
                pass
            return True

    def _handle_text(self, text):
        if not text:
            return
        try:
            msg = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return
        if isinstance(msg, dict):
            self._dispatch(msg)

    def _dispatch(self, msg):
        mid = msg.get("id")
        if mid is None:
            return
        fut = self.pending.pop(str(mid), None)
        if fut is None or fut.done():
            return
        try:
            fut.set_result(msg)
        except Exception:
            pass

    async def _send(self, payload):
        if not self.alive:
            raise RuntimeError("mcp server 进程未运行")
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if self.frame_mode == "content-length":
            frame = b"Content-Length: %d\r\n\r\n" % len(data) + data
        else:
            frame = data + b"\n"
        try:
            self.proc.stdin.write(frame)
            await self.proc.stdin.drain()
        except Exception as e:
            raise RuntimeError("写入失败: %s: %s" % (type(e).__name__, e))

    async def request(self, method, params=None, timeout=DEFAULT_TIMEOUT):
        if not self.alive:
            return {"ok": False, "error": "session not alive"}

        self.next_id += 1
        rid = "%d-%s" % (self.next_id, uuid.uuid4().hex[:6])
        try:
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
        except Exception as e:
            return {"ok": False,
                    "error": "create future failed: %s: %s" % (type(e).__name__, e)}
        self.pending[str(rid)] = fut

        body = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            body["params"] = params

        try:
            await self._send(body)
        except asyncio.CancelledError:
            self.pending.pop(str(rid), None)
            if _is_being_cancelled():
                raise
            _swallow_cancel("request/send", None)
            return {"ok": False, "error": "send cancelled (internal)",
                    "_internal_cancel": True}
        except Exception as e:
            self.pending.pop(str(rid), None)
            return {"ok": False, "error": str(e)}

        try:
            msg = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self.pending.pop(str(rid), None)
            return {"ok": False, "error": "timeout after %ss" % timeout}
        except asyncio.CancelledError:
            self.pending.pop(str(rid), None)
            if _is_being_cancelled():
                raise
            return {"ok": False, "error": "request cancelled (internal)",
                    "_internal_cancel": True}
        except Exception as e:
            self.pending.pop(str(rid), None)
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}

        if not isinstance(msg, dict):
            return {"ok": False, "error": "bad response type"}
        if msg.get("error"):
            err = msg.get("error")
            msg_str = err.get("message") if isinstance(err, dict) else str(err)
            return {"ok": False, "error": msg_str or "jsonrpc error",
                    "code": err.get("code") if isinstance(err, dict) else None}
        return {"ok": True, "data": msg.get("result"), "raw": msg}

    async def notify(self, method, params=None):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        try:
            await self._send(body)
            return {"ok": True}
        except asyncio.CancelledError:
            if _is_being_cancelled():
                raise
            return {"ok": False, "error": "notify cancelled (internal)",
                    "_internal_cancel": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def initialize(self, timeout=DEFAULT_TIMEOUT):
        res = await self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        }, timeout=timeout)
        if not res.get("ok"):
            return res
        result = res.get("data") or {}
        if isinstance(result, dict):
            self.server_info = result.get("serverInfo") or {}
            self.protocol_version = result.get("protocolVersion") or PROTOCOL_VERSION
        try:
            await self.notify("notifications/initialized", {})
        except asyncio.CancelledError:
            if _is_being_cancelled():
                raise
        except Exception:
            pass
        self.ready = True
        return {"ok": True, "server_info": self.server_info,
                "protocol_version": self.protocol_version}

    async def list_tools(self, timeout=DEFAULT_TIMEOUT):
        tools, cursor, pages = [], None, 0
        while True:
            params = {"cursor": cursor} if cursor else {}
            res = await self.request("tools/list", params, timeout=timeout)
            if not res.get("ok"):
                return res
            data = res.get("data") or {}
            if isinstance(data, dict):
                tools.extend([t for t in (data.get("tools") or [])
                              if isinstance(t, dict)])
                cursor = data.get("nextCursor")
            else:
                cursor = None
            pages += 1
            if not cursor or pages >= 20:
                break
        return {"ok": True, "data": tools}

    async def call_tool(self, tool, arguments=None, timeout=DEFAULT_TIMEOUT):
        res = await self.request("tools/call", {
            "name": tool, "arguments": arguments or {}}, timeout=timeout)
        if not res.get("ok"):
            return res
        return _shape_result(res.get("data"))


# ============================================================
# 会话管理（per-server 锁）
# ============================================================

class StdioManager:
    def __init__(self):
        self._sessions = {}
        self._locks = {}

    @property
    def sessions(self):
        return self._sessions

    def _lock_for(self, server):
        lock = self._locks.get(server)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[server] = lock
        return lock

    async def get(self, server, cfg):
        lock = self._lock_for(server)
        async with lock:
            try:
                return await self._get_locked(server, cfg)
            except asyncio.CancelledError:
                if _is_being_cancelled():
                    raise
                return None, False
            except Exception:
                return None, False

    async def _get_locked(self, server, cfg):
        sess = self._sessions.get(server)

        if sess and sess.alive:
            if sess.ready:
                return sess, False
            timeout = int((cfg.get("launch") or {}).get("timeout")
                          or cfg.get("timeout") or DEFAULT_TIMEOUT)
            init = await sess.initialize(timeout=timeout)
            if init.get("ok"):
                return sess, True
            await self._safe_stop(sess)
            self._sessions.pop(server, None)
            sess = None

        if sess:
            await self._safe_stop(sess)
            self._sessions.pop(server, None)

        lc = cfg.get("launch") or {}
        command = (lc.get("command") or "").strip()
        if not command:
            return None, False
        argv = _argv(command)
        if not argv:
            return None, False
        # 标准 MCP 配置：command 与 args 分离，args 追加到可执行文件之后
        extra_args = lc.get("args")
        if isinstance(extra_args, list):
            argv.extend([str(a) for a in extra_args
                         if a is not None and str(a).strip()])

        env = {}
        for k, v in (lc.get("env") or {}).items():
            env[str(k)] = str(v)
        if lc.get("port"):
            env[str(lc.get("port_env") or "MCP_PORT")] = str(lc["port"])
        if lc.get("host"):
            env[str(lc.get("host_env") or "MCP_HOST")] = str(lc["host"])

        sess = StdioSession(server, argv, env=env, cwd=lc.get("cwd"))
        started = await sess.start()
        if not started.get("ok"):
            return None, False

        timeout = int(lc.get("timeout") or cfg.get("timeout") or DEFAULT_TIMEOUT)
        init = await sess.initialize(timeout=timeout)
        if not init.get("ok"):
            await self._safe_stop(sess)
            return None, False

        self._sessions[server] = sess
        _write_meta(server, {
            "server": server,
            "pid": sess.proc.pid,
            "command": command,
            "transport": "stdio",
            "frame_mode": sess.frame_mode,
            "server_info": sess.server_info,
            "started_at": time.time(),
            "log": str(_log_path(server)),
        })
        return sess, True

    async def _safe_stop(self, sess):
        try:
            await sess.stop()
        except asyncio.CancelledError:
            if _is_being_cancelled():
                raise
        except Exception:
            pass

    async def drop(self, server):
        lock = self._lock_for(server)
        async with lock:
            sess = self._sessions.pop(server, None)
            if sess:
                await self._safe_stop(sess)
            try:
                p = _session_meta_path(server)
                if p.exists():
                    p.unlink()
            except OSError:
                pass
            return {"ok": True, "server": server,
                    "disconnected": sess is not None}

    async def stop_all(self):
        for server in list(self._sessions.keys()):
            try:
                await self.drop(server)
            except asyncio.CancelledError:
                if _is_being_cancelled():
                    raise
            except Exception:
                pass


def _manager(agent=None):
    if agent is not None:
        mgr = getattr(agent, "_mcp_stdio_manager", None)
        if mgr is None:
            mgr = StdioManager()
            try:
                agent._mcp_stdio_manager = mgr
            except Exception:
                pass
        return mgr
    return _MODULE_MANAGER


_MODULE_MANAGER = StdioManager()


async def _get_session(server, cfg, agent=None):
    return await _manager(agent).get(server, cfg)


async def _drop_session(server, agent=None):
    return await _manager(agent).drop(server)


# ============================================================
# actions
# ============================================================

async def _action_connect(payload, agent=None):
    try:
        return await _action_connect_impl(payload, agent)
    except asyncio.CancelledError:
        if _is_being_cancelled():
            raise
        _swallow_cancel("_action_connect", agent)
        return {"ok": False, "error": "connect cancelled (internal)",
                "_internal_cancel": True}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


async def _action_connect_impl(payload, agent=None):
    server = (payload.get("server") or payload.get("name") or "").strip()
    if not server:
        return {"ok": False, "error": "缺少 server"}

    cfg = payload.get("config") or {}
    if not isinstance(cfg, dict):
        cfg = {}
    command = (cfg.get("command") or (cfg.get("launch") or {}).get("command")
               or payload.get("command") or "").strip()
    if not command:
        return {"ok": False, "error": "缺少 command"}

    lc = dict(cfg.get("launch") or {})
    lc["command"] = command
    for k in ("args", "env", "cwd", "port", "host", "port_env", "host_env",
              "timeout", "ready_timeout"):
        if cfg.get(k) is not None and k not in lc:
            lc[k] = cfg.get(k)

    full_cfg = {"launch": lc, "timeout": cfg.get("timeout") or DEFAULT_TIMEOUT}

    sess, started = await _get_session(server, full_cfg, agent)
    if not sess:
        meta = _read_meta(server)
        return {"ok": False,
                "error": "stdio 启动/握手失败，详见日志 %s" % meta.get("log", ""),
                "log": meta.get("log", "")}

    timeout = int(lc.get("timeout") or DEFAULT_TIMEOUT)
    tools_res = await sess.list_tools(timeout=timeout)
    if not tools_res.get("ok"):
        return {"ok": False, "error": "tools/list failed: %s" % tools_res.get("error")}

    tools = tools_res.get("data") or []
    # ★ 回传完整对象数组（name/description/inputSchema），与 HTTP tools/list 同构
    return {
        "ok": True,
        "server": server,
        "transport": "stdio",
        "started": started,
        "pid": sess.proc.pid,
        "frame_mode": sess.frame_mode,
        "server_info": sess.server_info,
        "protocol_version": sess.protocol_version,
        "tool_count": len(tools),
        "tools": tools,                       # ← 完整对象，不是名字列表
        "log": str(_log_path(server)),
    }


async def _action_disconnect(payload, agent=None):
    try:
        server = (payload.get("server") or payload.get("name") or "").strip()
        if not server:
            return {"ok": False, "error": "缺少 server"}
        res = await _drop_session(server, agent)
        return {"ok": True, "server": server,
                "disconnected": bool(res.get("disconnected"))}
    except asyncio.CancelledError:
        if _is_being_cancelled():
            raise
        _swallow_cancel("_action_disconnect", agent)
        return {"ok": False, "error": "disconnect cancelled (internal)",
                "_internal_cancel": True}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


async def _action_list_tools(payload, agent=None):
    try:
        server = (payload.get("server") or "").strip()
        sess = _manager(agent).sessions.get(server) if server else None
        if not sess or not sess.alive:
            return {"ok": False, "error": "stdio 会话不存在或已退出，请先 connect"}
        res = await sess.list_tools(
            timeout=int(payload.get("timeout") or DEFAULT_TIMEOUT))
        if not res.get("ok"):
            return res
        tools = res.get("data") or []
        # ★ 返回完整对象，与 _mcp_client 的 list_tools 同构
        return {"ok": True, "server": server, "tool_count": len(tools),
                "data": [{"name": t.get("name"),
                          "description": t.get("description") or "",
                          "inputSchema": t.get("inputSchema") or {}}
                         for t in tools]}
    except asyncio.CancelledError:
        if _is_being_cancelled():
            raise
        _swallow_cancel("_action_list_tools", agent)
        return {"ok": False, "error": "list_tools cancelled (internal)",
                "_internal_cancel": True}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


async def _action_call(payload, agent=None):
    try:
        server = (payload.get("server") or "").strip()
        tool = (payload.get("tool") or payload.get("name") or "").strip()
        if not server or not tool:
            return {"ok": False, "error": "需要 server 和 tool"}
        args = payload.get("arguments")
        if not isinstance(args, dict):
            args = payload.get("args") if isinstance(payload.get("args"), dict) else {}

        sess = _manager(agent).sessions.get(server)
        if not sess or not sess.alive:
            return {"ok": False, "error": "stdio 会话不存在或已退出，请先 connect"}
        res = await sess.call_tool(
            tool, args, timeout=int(payload.get("timeout") or DEFAULT_TIMEOUT))
        if isinstance(res, dict):
            res["server"] = server
            res["tool"] = tool
        return res
    except asyncio.CancelledError:
        if _is_being_cancelled():
            raise
        _swallow_cancel("_action_call", agent)
        return {"ok": False, "error": "call_tool cancelled (internal)",
                "_internal_cancel": True}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


async def _action_list_servers(payload, agent=None):
    try:
        out = []
        for name, sess in _manager(agent).sessions.items():
            meta = _read_meta(name)
            out.append({
                "server": name,
                "transport": "stdio",
                "alive": sess.alive,
                "ready": sess.ready,
                "pid": sess.proc.pid if sess.proc else 0,
                "frame_mode": sess.frame_mode,
                "server_info": sess.server_info,
                "log": str(_log_path(name)),
            })
        return {"ok": True, "data": out, "total": len(out)}
    except asyncio.CancelledError:
        if _is_being_cancelled():
            raise
        _swallow_cancel("_action_list_servers", agent)
        return {"ok": False, "error": "list_servers cancelled (internal)",
                "_internal_cancel": True}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


async def _action_status(payload, agent=None):
    try:
        server = (payload.get("server") or "").strip()
        if not server:
            return {"ok": False, "error": "缺少 server"}
        sess = _manager(agent).sessions.get(server)
        meta = _read_meta(server)
        return {
            "ok": True,
            "server": server,
            "in_memory": sess is not None,
            "alive": bool(sess and sess.alive),
            "ready": bool(sess and sess.ready),
            "pid": (sess.proc.pid if sess and sess.proc else meta.get("pid", 0)),
            "frame_mode": (sess.frame_mode if sess else meta.get("frame_mode")),
            "meta": meta,
        }
    except asyncio.CancelledError:
        if _is_being_cancelled():
            raise
        _swallow_cancel("_action_status", agent)
        return {"ok": False, "error": "status cancelled (internal)",
                "_internal_cancel": True}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


# ============================================================
# 入口
# ============================================================

async def execute(envelop, agent):
    try:
        raw = envelop.payload if isinstance(envelop.payload, dict) else {}
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
        action = payload.get("action") or ""

        if action == "connect":
            envelop.payload = await _action_connect(payload, agent)
        elif action == "disconnect":
            envelop.payload = await _action_disconnect(payload, agent)
        elif action == "list_tools":
            envelop.payload = await _action_list_tools(payload, agent)
        elif action == "call_tool":
            envelop.payload = await _action_call(payload, agent)
        elif action == "list_servers":
            envelop.payload = await _action_list_servers(payload, agent)
        elif action == "status":
            envelop.payload = await _action_status(payload, agent)
        else:
            envelop.payload = {
                "ok": False,
                "error": "Unknown action: %s" % action,
                "actions": ["connect", "disconnect", "list_tools", "call_tool",
                            "list_servers", "status"],
            }
        return envelop
    except asyncio.CancelledError:
        if _is_being_cancelled():
            raise
        _swallow_cancel("execute", agent)
        envelop.payload = {"ok": False, "error": "internal cancel",
                           "_internal_cancel": True}
        return envelop
    except Exception as e:
        try:
            agent.log.error("[mcp_stdio] %s: %s" % (type(e).__name__, e))
        except Exception:
            pass
        envelop.payload = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        return envelop


def help():
    return {
        "route": "os/_mcp_stdio",
        "description": "通用 stdio transport MCP 适配器：拉起 stdio 型 MCP server 并调用其工具",
        "actions": {
            "connect": {"input": {"server": "名字", "command": "启动命令",
                                 "env": "可选环境变量", "cwd": "可选工作目录"}},
            "disconnect": {"input": {"server": "名字"}},
            "list_tools": {"input": {"server": "名字"}},
            "call_tool": {"input": {"server": "名字", "tool": "工具名", "arguments": {}}},
            "list_servers": {},
            "status": {"input": {"server": "名字"}},
        },
    }


async def start_stdio_service(agent):
    mgr = StdioManager()
    try:
        agent._mcp_stdio_manager = mgr
    except Exception:
        pass
    try:
        agent.log.info("[mcp_stdio] stdio manager ready")
    except Exception:
        pass
    return mgr


async def stop_stdio_service(agent):
    mgr = getattr(agent, "_mcp_stdio_manager", None)
    if mgr is None:
        return {"ok": True, "stopped": 0}
    count = len(mgr.sessions)
    try:
        await mgr.stop_all()
    except asyncio.CancelledError:
        if _is_being_cancelled():
            raise
    except Exception:
        pass
    try:
        agent._mcp_stdio_manager = None
    except Exception:
        pass
    try:
        agent.log.info("[mcp_stdio] stopped %d session(s)" % count)
    except Exception:
        pass
    return {"ok": True, "stopped": count}
