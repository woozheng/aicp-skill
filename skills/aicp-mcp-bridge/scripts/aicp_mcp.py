#!/usr/bin/env python3
"""aicp_mcp.py - diagnose / plan / apply / verify an AICP MCP registration.

  python3 aicp_mcp.py doctor [--json]
  python3 aicp_mcp.py plan   --client claude_code [--json]
  python3 aicp_mcp.py apply  --client claude_code --yes [--json]
  python3 aicp_mcp.py verify [--json]
  python3 aicp_mcp.py ensure [--api URL] [--port 9100]

Only stdlib. No writes happen without --yes on `apply`.
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_MCP_URL = os.environ.get("AICP_MCP_URL", "http://127.0.0.1:9100/mcp")
DEFAULT_API = os.environ.get("AICP_API", "http://127.0.0.1:9000")
SESSION_NAME = "aicp"
BRIDGE = Path(__file__).resolve().parent / "mcp_stdio_bridge.py"


# ---------------------------------------------------------------- transport

def http_json(url, payload=None, headers=None, timeout=10, method=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method or ("POST" if data is not None else "GET")
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
        return resp.status, dict(resp.headers), body


def parse_body(body):
    text = body.strip()
    if not text:
        return None
    if text.startswith("{"):
        return json.loads(text)
    for line in reversed(text.splitlines()):
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if chunk and chunk != "[DONE]":
                return json.loads(chunk)
    return None


class Session:
    """Minimal MCP streamable-HTTP client."""

    def __init__(self, url):
        self.url = url
        self.session_id = None
        self.next_id = 0

    def rpc(self, method, params=None, notify=False):
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notify:
            self.next_id += 1
            payload["id"] = self.next_id
        headers = {}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        status, hdrs, body = http_json(self.url, payload, headers)
        for key, value in hdrs.items():
            if key.lower() == "mcp-session-id":
                self.session_id = value
        parsed = parse_body(body)
        if parsed and "error" in parsed:
            raise RuntimeError("%s: %s" % (method, parsed["error"]))
        return status, parsed

    def handshake(self):
        status, body = self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "aicp-skill", "version": "1"},
            },
        )
        self.rpc("notifications/initialized", notify=True)
        return body

    def tools(self):
        _, body = self.rpc("tools/list")
        return (body or {}).get("result", {}).get("tools", [])

    def call(self, receiver, action=None, params=None):
        arguments = {"receiver": receiver}
        if action:
            arguments["action"] = action
        if params:
            arguments["params"] = params
        _, body = self.rpc("tools/call", {"name": "call_plugin", "arguments": arguments})
        result = (body or {}).get("result", {})
        texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        raw = texts[0] if texts else ""
        try:
            return json.loads(raw), bool(result.get("isError"))
        except ValueError:
            return {"raw": raw}, bool(result.get("isError"))


# ---------------------------------------------------------------- json / jsonc

def strip_jsonc(text):
    out = []
    i, n, in_string = 0, len(text), False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def load_document(path):
    """Return (data, strict). strict=False means comments or trailing commas were present."""
    if not path.exists():
        return {}, True
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        return {}, True
    try:
        return json.loads(text), True
    except ValueError:
        return json.loads(strip_jsonc(text)), False


# ---------------------------------------------------------------- clients

def _xdg(default):
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / default))


def _appdata():
    return Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))


def _vscode_user():
    if sys.platform == "win32":
        return _appdata() / "Code" / "User"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Code" / "User"
    return _xdg(".config") / "Code" / "User"


def client_path(name):
    if name == "claude_code":
        base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home())
        return base / ".claude.json"
    if name == "claude_desktop":
        if sys.platform == "win32":
            return _appdata() / "Claude" / "claude_desktop_config.json"
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        return _xdg(".config") / "Claude" / "claude_desktop_config.json"
    if name == "cline":
        return _vscode_user() / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json"
    if name == "cursor":
        return Path.home() / ".cursor" / "mcp.json"
    if name == "vscode":
        return Path.cwd() / ".vscode" / "mcp.json"
    if name == "codex":
        base = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
        return base / "config.toml"
    return None


CLIENTS = {
    "claude_code": {"kind": "json", "key": "mcpServers", "transport": "http"},
    "claude_desktop": {"kind": "json", "key": "mcpServers", "transport": "stdio"},
    "cline": {"kind": "json", "key": "mcpServers", "transport": "http"},
    "cursor": {"kind": "json", "key": "mcpServers", "transport": "http"},
    "vscode": {"kind": "json", "key": "servers", "transport": "http"},
    "codex": {"kind": "toml", "key": "mcp_servers", "transport": "http"},
}


def entry_for(client, url, name=SESSION_NAME):
    spec = CLIENTS[client]
    if spec["transport"] == "stdio":
        return {
            "command": sys.executable or "python3",
            "args": [str(BRIDGE), url],
            "env": {},
        }
    if client == "vscode":
        return {"type": "http", "url": url}
    if client == "cline":
        return {"type": "streamableHttp", "url": url, "disabled": False}
    if client == "claude_code":
        return {"type": "http", "url": url}
    return {"url": url}


def entry_matches(current, entry):
    if not isinstance(current, dict):
        return False
    if "url" in entry:
        return current.get("url") == entry["url"]
    args = current.get("args") or []
    return bool(args) and entry["args"][-1] in [str(a) for a in args]


def detect(name, url):
    path = client_path(name)
    if path is None:
        return {"client": name, "path": None, "present": False, "state": "unsupported"}
    info = {"client": name, "path": str(path), "present": path.exists()}
    if not path.exists():
        info["state"] = "missing"
        return info
    try:
        if CLIENTS[name]["kind"] == "toml":
            text = path.read_text(encoding="utf-8-sig")
            info["state"] = "configured" if "[mcp_servers.%s]" % SESSION_NAME in text else "absent"
            info["aligned"] = url in text
            return info
        data, strict = load_document(path)
        info["jsonc"] = not strict
        servers = data.get(CLIENTS[name]["key"], {}) if isinstance(data, dict) else {}
        current = servers.get(SESSION_NAME)
        info["entry"] = current
        info["state"] = "configured" if current else "absent"
        info["aligned"] = entry_matches(current, entry_for(name, url))
    except Exception as exc:
        info["state"] = "invalid"
        info["error"] = "%s: %s" % (type(exc).__name__, exc)
    return info


# ---------------------------------------------------------------- checks

def check_endpoint(url):
    base = url.rsplit("/mcp", 1)[0]
    try:
        status, _, body = http_json(base + "/health", timeout=4)
        return {"name": "endpoint", "ok": status == 200, "detail": body[:160]}
    except Exception as exc:
        return {
            "name": "endpoint",
            "ok": False,
            "detail": "%s: %s" % (type(exc).__name__, exc),
            "remediation": "Start the AICP MCP server (see `ensure`) or point --url at the real endpoint.",
        }


def check_protocol(url):
    try:
        session = Session(url)
        info = session.handshake()
        server = (info or {}).get("result", {}).get("serverInfo", {})
        tools = session.tools()
        names = [t.get("name") for t in tools]
        ok = "call_plugin" in names
        check = {
            "name": "mcp_handshake",
            "ok": ok,
            "detail": "session=%s server=%s tools=%s" % (
                session.session_id,
                server.get("name", "?"),
                names,
            ),
        }
        if not ok:
            check["remediation"] = "Endpoint responded but does not expose call_plugin; check which server owns this port."
        return check
    except Exception as exc:
        return {
            "name": "mcp_handshake",
            "ok": False,
            "detail": "%s: %s" % (type(exc).__name__, exc),
            "remediation": "Port is open but MCP JSON-RPC failed; inspect the AICP-side log for os/_mcp_server.",
        }


def check_call(url):
    try:
        session = Session(url)
        session.handshake()
        result, is_error = session.call(
            "builtins/tools/cogitor", "search", {"terms": ["mcp"], "limit": 1}
        )
        ok = (not is_error) and bool(result.get("ok"))
        return {
            "name": "tool_call",
            "ok": ok,
            "detail": "cogitor.search -> ok=%s total=%s" % (result.get("ok"), result.get("total")),
            "remediation": None if ok else "call_plugin reached the gateway but the plugin call failed; check receiver routing.",
        }
    except Exception as exc:
        return {"name": "tool_call", "ok": False, "detail": "%s: %s" % (type(exc).__name__, exc)}


def check_bridge():
    present = BRIDGE.exists()
    return {
        "name": "stdio_bridge",
        "ok": present,
        "detail": str(BRIDGE),
        "remediation": None if present else "mcp_stdio_bridge.py is missing next to aicp_mcp.py; stdio-only clients cannot connect.",
    }


def doctor(url, clients):
    checks = [check_endpoint(url), check_protocol(url), check_call(url), check_bridge()]
    reports = []
    for name in clients:
        info = detect(name, url)
        ok = info.get("state") == "configured" and info.get("aligned") is True
        check = {
            "name": "client:%s" % name,
            "ok": ok,
            "detail": "%s state=%s aligned=%s" % (info.get("path"), info.get("state"), info.get("aligned")),
        }
        if not ok:
            check["remediation"] = (
                "Not registered or pointing elsewhere; run `plan` then `apply` for this client."
                if info.get("state") in ("missing", "absent")
                else "Registration differs from this endpoint; re-apply or remove the stale entry."
            )
        reports.append(info)
        checks.append(check)
    return {"url": url, "checks": checks, "clients": reports,
            "ok": all(c.get("ok") for c in checks if c["name"] != "stdio_bridge")}


# ---------------------------------------------------------------- apply

def backup_path(path):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return path.with_name(path.name + ".bak." + stamp)


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


def apply_json(client, url, name=SESSION_NAME):
    path = client_path(client)
    data, strict = load_document(path)
    key = CLIENTS[client]["key"]
    servers = data.get(key)
    if not isinstance(servers, dict):
        servers = {}
    existing = servers.get(name)
    entry = entry_for(client, url, name)
    # Keep user-owned fields (auth headers, env) that this writer does not manage.
    if isinstance(existing, dict):
        for keep in ("headers", "env", "envFile", "alwaysAllow"):
            if keep in existing and keep not in entry:
                entry[keep] = existing[keep]
    if entry_matches(existing, entry):
        return {"ok": True, "status": "unchanged", "path": str(path), "entry": entry, "backup": None}
    servers[name] = entry
    data[key] = servers
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if not strict and not ALLOW_JSONC:
        return {
            "ok": False,
            "status": "blocked",
            "path": str(path),
            "reason": "jsonc",
            "remediation": "File contains comments/trailing commas; rewriting would drop them. Rerun with --allow-jsonc (a backup is kept) or edit by hand.",
            "entry": entry,
            "existing": existing,
        }
    backup = None
    if path.exists():
        backup = backup_path(path)
        shutil.copy2(str(path), str(backup))
    atomic_write(path, payload)
    again, _ = load_document(path)
    written = (again.get(key) or {}).get(name)
    return {
        "ok": entry_matches(written, entry),
        "status": "configured" if entry_matches(written, entry) else "readback_failed",
        "path": str(path),
        "entry": entry,
        "previous": existing,
        "backup": str(backup) if backup else None,
        "comments_lost": not strict,
    }


def apply_toml(client, url, name=SESSION_NAME):
    path = client_path(client)
    text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    header = "[mcp_servers.%s]" % name
    block = '%s\nurl = "%s"\n' % (header, url)
    lines = text.splitlines(True)
    out, skipping, placed = [], False, False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if skipping:
                skipping = False
                out.append(block)
                placed = True
            if stripped == header:
                skipping = True
                if not placed:
                    out.append(block)
                    placed = True
                continue
        if skipping:
            continue
        out.append(line)
    if skipping and not placed:
        out.append(block)
        placed = True
    if not placed:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        if out and out[-1].strip():
            out.append("\n")
        out.append(block)
    payload = "".join(out)
    if payload == text and header in text:
        return {"ok": url in text, "status": "unchanged", "path": str(path), "backup": None}
    backup = None
    if path.exists():
        backup = backup_path(path)
        shutil.copy2(str(path), str(backup))
    atomic_write(path, payload)
    readback = path.read_text(encoding="utf-8-sig")
    ok = header in readback and url in readback
    return {
        "ok": ok,
        "status": "configured" if ok else "readback_failed",
        "path": str(path),
        "backup": str(backup) if backup else None,
        "mode": "toml",
    }


# ---------------------------------------------------------------- ensure

def ensure(api, port, host="127.0.0.1"):
    payload = {"action": "START", "host": host, "port": port}
    try:
        status, _, body = http_json("%s/api/os/_mcp_server" % api.rstrip("/"), payload, timeout=20)
    except Exception as exc:
        return {"ok": False, "error": "gateway call failed: %s: %s" % (type(exc).__name__, exc)}
    parsed = parse_body(body) or {}
    inner = parsed.get("data") if isinstance(parsed.get("data"), dict) else parsed
    url = "http://%s:%s/mcp" % (host, port)
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            code, _, _ = http_json(url.rsplit("/mcp", 1)[0] + "/health", timeout=2)
            if code == 200:
                return {"ok": True, "url": url, "start": inner}
        except Exception:
            time.sleep(0.5)
    return {"ok": False, "url": url, "start": inner, "error": "started but /health never answered"}


# ---------------------------------------------------------------- cli

# ---------------------------------------------------------------- bootstrap

SKILL_DOC = "SKILL.md"
CLINERULES_DIR = ".clinerules"


def detect_installed_clients():
    """探测本机装过哪些 MCP 客户端（配置文件或它的父目录存在即算）。"""
    found = []
    for name in CLIENTS:
        try:
            path = client_path(name)
        except Exception:
            continue
        if not path:
            continue
        info = {"client": name, "path": str(path), "configured": path.exists()}
        if path.exists():
            try:
                info["mtime"] = path.stat().st_mtime
            except OSError:
                info["mtime"] = 0
            found.append(info)
        elif path.parent.exists():
            info["mtime"] = 0
            found.append(info)
    found.sort(key=lambda item: (item["configured"], item["mtime"]), reverse=True)
    return found


def resolve_clients(names):
    """把 --client 的取值（名字 / auto / all）解析成真实客户端列表。"""
    names = [str(n).strip() for n in (names or []) if str(n).strip()]
    if "all" in names:
        return sorted(CLIENTS)
    if "auto" not in names:
        return names
    detected = [item["client"] for item in detect_installed_clients()]
    explicit = [n for n in names if n != "auto"]
    picked = []
    for name in detected + explicit:
        if name in CLIENTS and name not in picked:
            picked.append(name)
    return picked


def slim_rule_text(url, script_path, skill_path):
    """给 .clinerules 用的瘦入口：只写指针和铁律，不复制整份手册。"""
    base = url.rsplit("/mcp", 1)[0]
    return "\n".join([
        "# AICP 接入",
        "",
        "本机 AICP 的 MCP 端点：%s（server 名 aicp）。" % url,
        "健康检查：curl %s/health — 不通说明引擎没起。" % base,
        "",
        "连上后 initialize 返回的 instructions 已内含操作手册，不必先读本地文档。",
        "需要更细的插件用法时读：%s" % skill_path,
        "",
        "三条铁律：",
        "1. receiver 必须是完整路由（builtins/tools/code_runner、os/_cron、applications/<proj>/<name>），写插件名会 plugin not found。",
        "2. 有副作用的动作（删文件、发布应用、改系统配置）先问用户。",
        "3. 端点只绑 127.0.0.1，不要转发到公网。",
        "",
        "端点不通时：python \"%s\" ensure" % script_path,
        "",
    ])


def write_slim_rule(client, url, root=None):
    """只有 .clinerules 型客户端需要落一份瘦入口；认 skills 目录的客户端跳过。"""
    if client != "cline":
        return {"client": client, "status": "skipped", "reason": "client reads skills dir or initialize.instructions"}
    root = Path(root) if root else Path.cwd()
    target = root / CLINERULES_DIR / "aicp.md"
    script = Path(__file__).resolve()
    skill = script.parents[1] / SKILL_DOC
    text = slim_rule_text(url, script, skill)
    if target.is_dir():
        return {"client": client, "status": "skipped", "reason": "%s is a directory" % target}
    if target.exists():
        try:
            if target.read_text(encoding="utf-8", errors="ignore") == text:
                return {"client": client, "path": str(target), "status": "unchanged"}
        except OSError:
            pass
    parent = target.parent
    if parent.exists() and not parent.is_dir():
        return {"client": client, "status": "skipped", "reason": "%s is a file, not a directory" % parent}
    parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if target.exists():
        backup = target.with_name(target.name + ".bak")
        shutil.copy2(str(target), str(backup))
    target.write_text(text, encoding="utf-8")
    return {
        "client": client,
        "path": str(target),
        "status": "written",
        "backup": str(backup) if backup else None,
        "lines": text.count("\n") + 1,
    }


def bootstrap(clients_spec, url, api, port, name=SESSION_NAME, yes=False, root=None,
              write_rule=True, autostart=True):
    """一条命令完成：探测客户端 → 探活/拉起端点 → 写 MCP 配置 → 落瘦入口。"""
    steps = []
    detected = detect_installed_clients()
    steps.append({
        "step": "detect",
        "found": [item["client"] for item in detected],
        "detail": detected,
    })

    resolved = resolve_clients(clients_spec)
    unknown = [c for c in resolved if c not in CLIENTS]
    if unknown:
        steps.append({"step": "resolve", "clients": resolved})
        return {"ok": False, "steps": steps, "known": sorted(CLIENTS),
                "error": "unknown client: %s" % ", ".join(unknown)}
    steps.append({"step": "resolve", "clients": resolved})
    if not resolved:
        return {"ok": False, "steps": steps, "known": sorted(CLIENTS),
                "error": "no MCP client detected; pass --client <name> or --client all"}

    if autostart:
        probe = check_endpoint(url)
        if probe.get("ok"):
            steps.append({"step": "ensure", "ok": True, "skipped": "endpoint already healthy"})
        else:
            steps.append({"step": "ensure", **ensure(api, port)})

    outcomes = []
    for client in resolved:
        info = detect(client, url)
        plan = {
            "client": client,
            "path": str(client_path(client)),
            "action": "unchanged" if info.get("aligned") else (
                "update" if info.get("state") == "configured" else "create"),
        }
        if not yes:
            outcomes.append({"client": client, "status": "planned", "plan": plan})
            continue
        if plan["action"] == "unchanged":
            outcomes.append({"client": client, "status": "unchanged", "plan": plan})
            continue
        outcome = apply_toml(client, url, name) if CLIENTS[client]["kind"] == "toml" else apply_json(client, url, name)
        outcome["client"] = client
        outcome["plan"] = plan
        outcomes.append(outcome)
    steps.append({"step": "register", "dry_run": not yes, "results": outcomes})

    if yes and write_rule:
        steps.append({"step": "rule", "results": [write_slim_rule(c, url, root) for c in resolved]})

    ok = True
    for item in outcomes:
        if item.get("status") in ("planned", "unchanged", "configured"):
            continue
        if not item.get("ok"):
            ok = False

    return {
        "ok": ok,
        "url": url,
        "steps": steps,
        "next": (
            "重启客户端窗口（Ctrl+Shift+P → Reload Window），然后问它 'AICP 能干什么' 验证"
            if yes else "计划已生成，加 --yes 执行"
        ),
    }


def emit(payload, as_json):
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if "checks" in payload:
        for check in payload["checks"]:
            print("%s  %-22s %s" % ("OK  " if check.get("ok") else "FAIL", check["name"], check.get("detail")))
            if not check.get("ok") and check.get("remediation"):
                print("      -> %s" % check["remediation"])
        print("\nverdict: %s" % ("ready" if payload.get("ok") else "needs work"))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv=None):
    global ALLOW_JSONC
    parser = argparse.ArgumentParser(prog="aicp_mcp.py")
    parser.add_argument("command", choices=["doctor", "plan", "apply", "verify", "ensure", "bootstrap"])
    parser.add_argument("--client", action="append", default=[])
    parser.add_argument("--url", default=DEFAULT_MCP_URL)
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--name", default=SESSION_NAME)
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--allow-jsonc", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--root", default=None, help="bootstrap 落 .clinerules 的根目录，默认当前目录")
    args = parser.parse_args(argv)
    ALLOW_JSONC = args.allow_jsonc
    clients = args.client or ["claude_code", "claude_desktop", "cline", "cursor", "vscode", "codex"]

    if args.command == "bootstrap":
        result = bootstrap(
            args.client or ["auto"], args.url, args.api, args.port, args.name,
            yes=args.yes, root=args.root,
        )
        emit(result, args.json)
        if result.get("ok") and args.yes:
            print("\n%s" % result["next"], file=sys.stderr)
        return 0 if result.get("ok") else 1

    if args.command == "doctor":
        report = doctor(args.url, clients)
        emit(report, args.json)
        return 0 if report["ok"] else 1

    if args.command == "verify":
        report = doctor(args.url, [])
        emit(report, args.json)
        return 0 if report["ok"] else 1

    if args.command == "ensure":
        result = ensure(args.api, args.port)
        emit(result, args.json)
        return 0 if result.get("ok") else 1

    if not args.client:
        print("plan/apply need --client (one of: %s)" % ", ".join(sorted(CLIENTS)), file=sys.stderr)
        return 2

    plans = []
    for client in args.client:
        if client not in CLIENTS:
            print("unknown client: %s" % client, file=sys.stderr)
            return 2
        path = client_path(client)
        entry = entry_for(client, args.url, args.name)
        info = detect(client, args.url)
        plan = {
            "client": client,
            "path": str(path),
            "exists": path.exists(),
            "jsonc": info.get("jsonc", False),
            "server_key": CLIENTS[client]["key"],
            "entry": entry,
            "current": info.get("entry"),
            "action": "unchanged" if info.get("aligned") else ("update" if info.get("state") == "configured" else "create"),
            "backup": str(backup_path(path)) if path.exists() else None,
            "skill_dir": str(Path(__file__).resolve().parents[1]),
        }
        plans.append(plan)

    if args.command == "plan":
        emit({"command": "plan", "url": args.url, "plans": plans, "dry_run": True}, args.json)
        return 0

    if not args.yes:
        emit({"ok": False, "error": "apply needs --yes after the plan was approved", "plans": plans}, args.json)
        return 2

    results = []
    exit_code = 0
    for plan in plans:
        client = plan["client"]
        if plan["action"] == "unchanged":
            results.append({"client": client, "ok": True, "status": "unchanged", "path": plan["path"]})
            continue
        outcome = apply_toml(client, args.url, args.name) if CLIENTS[client]["kind"] == "toml" else apply_json(client, args.url, args.name)
        outcome["client"] = client
        results.append(outcome)
        if not outcome.get("ok"):
            exit_code = 1
    emit({"command": "apply", "results": results}, args.json)
    if exit_code == 0:
        print("\nRestart the client(s), then run: python3 %s verify --json" % Path(__file__).name, file=sys.stderr)
    return exit_code


ALLOW_JSONC = False

if __name__ == "__main__":
    sys.exit(main())
