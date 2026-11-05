"""
REA 逆向引擎 —— 进程保活 + MCP 接入一体化

REA (morluto/rea) 以 MCP server 形态提供 229 个逆向工具。本插件只做三件事：
  1. 拉起 / 保活 REA MCP 进程（幂等，不重复起）
  2. 确保 os/_mcp_client 已连上（自动选空闲端口）
  3. 暴露便捷调用入口

action:
  ensure    幂等：探活 → 必要时拉进程 → connect → 注册虚拟路由
  status    查看进程/端口/连接/工具数状态
  stop      停掉 REA 进程（不断 MCP 连接）
  call      直调 REA 工具（内部走 os/_mcp_client.call_tool）
  tools     列出可用 REA 工具名（分类筛选）

REA 启动命令（npm 全局安装的 tarball，自带 Windows 预编译 native）：
  node <npm-global>/rea-agents/scripts/rea.mjs mcp --http
环境变量：MCP_PORT / MCP_HOST
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

STATE_DIR = Path("data/mytools/rea_server")
PID_FILE = STATE_DIR / "rea.pid"
PORT_FILE = STATE_DIR / "rea.port"
LOG_FILE = STATE_DIR / "rea.log"

MCP_CLIENT = "os/_mcp_client"
SERVER_NAME = "rea"

# REA 包安装位置候选（npm 全局 root）
REA_ENTRY_CANDIDATES = [
    "scripts/rea.mjs",
    "dist/rea.mjs",
    "index.mjs",
]


def _payload(envelop):
    raw = getattr(envelop, "payload", None)
    if isinstance(raw, dict):
        inner = raw.get("payload")
        if isinstance(inner, dict):
            return inner
        return raw
    return {}


def _npm_global_root():
    """找 npm 全局 root，拿到 rea-agents 安装目录"""
    env = os.environ.get("REA_PACKAGE_DIR") or ""
    if env and Path(env).is_dir():
        return env
    # npm 全局 bin 已在 PATH → node_modules 通常在同级或上级的 node_modules
    which = shutil.which("rea") or shutil.which("rea-agents")
    if which:
        p = Path(which).resolve()
        # <root>/node_modules/rea-agents/rea(.cmd)
        for parent in p.parents:
            cand = parent / "node_modules" / "rea-agents"
            if cand.is_dir():
                return str(cand)
    # 兜底：npm root -g
    try:
        out = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=15)
        if out.returncode == 0:
            cand = Path(out.stdout.strip()) / "rea-agents"
            if cand.is_dir():
                return str(cand)
    except Exception:
        pass
    # Windows 默认位置
    appdata = os.environ.get("APPDATA")
    if appdata:
        cand = Path(appdata) / "npm" / "node_modules" / "rea-agents"
        if cand.is_dir():
            return str(cand)
    return ""


def _find_entry(pkg_dir):
    if not pkg_dir:
        return ""
    base = Path(pkg_dir)
    for rel in REA_ENTRY_CANDIDATES:
        cand = base / rel
        if cand.is_file():
            return str(cand)
    # 全盘找 rea.mjs
    for cand in base.rglob("rea.mjs"):
        return str(cand)
    return ""


def _free_port(preferred=None):
    """挑一个空闲端口。preferred 优先。"""
    if preferred:
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", int(preferred)))
            s.close()
            return int(preferred)
        except Exception:
            pass
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _port_alive(port, timeout=1.5):
    """探活：连得上且能拿到 MCP 响应头就算活着"""
    if not port:
        return False
    try:
        s = socket.create_connection(("127.0.0.1", int(port)), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


def _read_pid():
    try:
        if PID_FILE.exists():
            return int(PID_FILE.read_text(encoding="utf-8").strip() or 0)
    except Exception:
        pass
    return 0


def _pid_alive(pid):
    if not pid:
        return False
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                                 capture_output=True, text=True, timeout=10)
            return str(pid) in (out.stdout or "")
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _write_state(pid, port, entry):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(pid), encoding="utf-8")
    PORT_FILE.write_text(str(port), encoding="utf-8")
    (STATE_DIR / "rea.entry").write_text(entry or "", encoding="utf-8")


def _read_port():
    try:
        if PORT_FILE.exists():
            return int(PORT_FILE.read_text(encoding="utf-8").strip() or 0)
    except Exception:
        pass
    return 0


async def _mcp_call(agent, action, **kw):
    """调用 os/_mcp_client"""
    from core import Envelop
    payload = {"action": action}
    payload.update(kw)
    env = Envelop(receiver=MCP_CLIENT, payload=payload)
    r = await agent.system.call(env)
    return getattr(r, "payload", None) or {}


def _spawn(entry, port):
    """拉起 REA MCP server（detached，不随父进程死）"""
    env = os.environ.copy()
    env["MCP_PORT"] = str(port)
    env["MCP_HOST"] = "127.0.0.1"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log = open(str(LOG_FILE), "ab")
    kwargs = {"stdout": log, "stderr": log, "stdin": subprocess.DEVNULL, "env": env}
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008)
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        ["node", entry, "mcp", "--http"],
        **kwargs
    )
    return proc


async def _ensure(agent, force=False):
    result = {"ok": False, "actions": []}

    pkg_dir = _npm_global_root()
    entry = _find_entry(pkg_dir)
    result["package_dir"] = pkg_dir
    result["entry"] = entry
    if not entry:
        result["error"] = ("找不到 rea-agents 入口（rea.mjs）。请确认已 `npm i -g rea-agents`，"
                           "或设置环境变量 REA_PACKAGE_DIR 指向安装目录。")
        return result

    pid = _read_pid()
    port = _read_port()
    alive = _pid_alive(pid) and _port_alive(port)

    if alive and not force:
        result["actions"].append("already_running")
    else:
        if pid and _pid_alive(pid):
            # 进程在但端口不通 → 杀掉重来
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                                   capture_output=True, timeout=15)
                else:
                    os.kill(pid, 9)
            except Exception:
                pass
        port = _free_port(preferred=port or None)
        try:
            proc = _spawn(entry, port)
        except Exception as e:
            result["error"] = "启动 REA 失败: %s: %s" % (type(e).__name__, e)
            return result
        pid = proc.pid
        result["actions"].append("spawned")

        # 等端口起来
        ok = False
        for _ in range(60):
            await asyncio.sleep(0.5)
            if _port_alive(port):
                ok = True
                break
            if proc.poll() is not None:
                break
        if not ok:
            tail = ""
            try:
                if LOG_FILE.exists():
                    tail = LOG_FILE.read_text(encoding="utf-8", errors="ignore")[-800:]
            except Exception:
                pass
            result["error"] = "REA 进程启动后端口未就绪 (port=%s)。日志尾部: %s" % (port, tail)
            result["port"] = port
            result["pid"] = pid
            return result

    _write_state(pid, port, entry)
    result["pid"] = pid
    result["port"] = port
    result["url"] = "http://127.0.0.1:%d/mcp" % port

    # 确保 MCP 客户端已连接 + 虚拟路由已注册
    conn = await _mcp_call(agent, "connect", server=SERVER_NAME, url=result["url"])
    result["connect"] = {
        "ok": bool(conn.get("ok")),
        "tool_count": conn.get("tool_count"),
        "server_info": conn.get("server_info"),
        "error": conn.get("error"),
    }
    if conn.get("ok"):
        virt = conn.get("virtual") or {}
        result["virtual_registered"] = virt.get("registered")
        result["actions"].append("connected")
    result["ok"] = bool(conn.get("ok"))
    if not result["ok"]:
        result.setdefault("error", conn.get("error") or "connect failed")
    return result


async def _status(agent):
    pid = _read_pid()
    port = _read_port()
    pkg_dir = _npm_global_root()
    servers = await _mcp_call(agent, "list_servers")
    srv = None
    for s in (servers.get("data") or []):
        if s.get("server") == SERVER_NAME:
            srv = s
            break
    return {
        "ok": True,
        "package_dir": pkg_dir,
        "entry": _find_entry(pkg_dir),
        "pid": pid,
        "pid_alive": _pid_alive(pid),
        "port": port,
        "port_alive": _port_alive(port),
        "url": "http://127.0.0.1:%d/mcp" % port if port else "",
        "mcp_server": srv,
        "tool_count": (srv or {}).get("tool_count") or 0,
        "connected": bool((srv or {}).get("connected")),
    }


async def _stop(agent):
    pid = _read_pid()
    stopped = False
    if pid and _pid_alive(pid):
        try:
            if os.name == "nt":
                out = subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                                     capture_output=True, text=True, timeout=20)
                stopped = out.returncode == 0
            else:
                os.kill(pid, 9)
                stopped = True
        except Exception as e:
            return {"ok": False, "error": str(e)}
    for f in (PID_FILE, PORT_FILE):
        try:
            if f.exists():
                f.unlink()
        except Exception:
            pass
    return {"ok": True, "stopped": stopped, "pid": pid}


# 工具分类（供 tools action 速查）
CATEGORIES = {
    "session": ["open_binary", "close_binary", "binary_session", "binary_overview",
                "inspect_analysis_settings", "update_analysis_settings"],
    "native": ["procedure_pseudo_code", "procedure_assembly", "procedure_info",
               "procedure_callers", "procedure_callees", "analyze_function",
               "batch_decompile", "decompile_all", "get_call_graph", "list_procedures",
               "list_strings", "search_strings", "search_procedures", "search_bytes",
               "read_bytes", "xrefs", "trace_feature", "trace_call_path",
               "trace_native_values", "inspect_native_instruction", "address_name",
               "procedure_address", "resolve_containing_procedure"],
    "pe_elf": ["inspect_pe_imports", "inspect_pe_exports" if False else "inspect_native_exports",
               "inspect_pe_resources", "inspect_pe_certificates", "inspect_pe_load_config",
               "inspect_elf_notes", "inspect_binary_layout", "inspect_byte_entropy",
               "inspect_kernel_module", "inspect_devicetree", "inspect_go_binary",
               "inspect_rust_binary", "inspect_with_objdump", "execute_rizin_command"],
    "android": ["inspect_android_package", "inspect_android_class", "inspect_android_method",
                "search_android_classes", "trace_android_references", "inspect_apk_signature",
                "decode_android_resources", "inspect_dart_aot", "identify_flutter_build",
                "list_adb_devices", "inspect_adb_device", "list_adb_packages",
                "resolve_adb_packages", "pull_adb_package", "read_adb_logcat",
                "start_adb_app", "stop_adb_app", "install_adb_package", "uninstall_adb_package",
                "capture_adb_screen", "list_adb_processes", "list_adb_directory",
                "pull_adb_file", "push_adb_file", "inspect_adb_display", "inspect_adb_package",
                "read_adb_setting", "list_adb_features", "list_adb_services", "inspect_adb_window",
                "collect_adb_bugreport", "inspect_adb_client"],
    "web_js": ["analyze_web_bundle", "inspect_web_page", "analyze_javascript_application",
               "recover_javascript_sources", "export_web_scripts", "trace_web_module_imports",
               "trace_web_source_location", "inspect_electron_page", "list_electron_targets",
               "inspect_electron_fuses", "capture_browser_scenario", "capture_electron_scenario",
               "observe_web_execution", "observe_web_session", "discover_webmcp_tools",
               "capture_web_screenshot", "compare_web_captures", "compare_web_screenshots",
               "inspect_web_network_capture", "inspect_web_event_listeners",
               "list_javascript_runtime_targets", "observe_javascript_runtime",
               "reconcile_javascript_runtime", "trace_javascript_semantics",
               "trace_application_feature", "compare_application_versions",
               "compare_source_to_bundle", "compare_javascript_export_shapes",
               "inspect_analysis_view", "inspect_artifact", "extract_artifact"],
    "apple": ["inspect_macho", "inspect_signature", "inspect_plist", "list_architectures",
              "demangle_swift", "analyze_swift_types", "get_objc_classes", "get_objc_protocols",
              "inspect_native_dispatch_metadata", "decode_interface_builder",
              "inspect_keyed_archive", "inspect_asset_catalog", "trace_dylib_resolution",
              "open_bundle_resources", "inspect_apple_ips"],
    "dynamic": ["start_frida_session", "load_frida_script", "resume_frida_session",
                "unload_frida_script", "frida_session_status", "close_frida_session",
                "instrument_with_frida", "list_frida_devices", "list_frida_processes",
                "start_gdb_session", "gdb_console", "gdb_session_status", "close_gdb_session",
                "start_rizin_debug_session", "rizin_debug_command",
                "rizin_debug_session_status", "close_rizin_debug_session",
                "capture_process_scenario", "list_cutter_sessions", "cutter_command"],
    "net": ["inspect_packet_capture", "inspect_packet_fields", "inspect_pcap_capture",
            "inspect_pcap_client", "inspect_signal_decoders", "decode_signal_recording"],
    "evidence": ["get_evidence_bundle", "export_evidence_bundle", "import_evidence_bundle",
                 "list_unknowns", "record_unknown", "update_unknown",
                 "verify_unknown_resolution", "build_reconstruction_obligation_ledger",
                 "evaluate_reconstruction_coverage", "verify_reconstruction",
                 "compare_functions", "compare_artifacts", "compare_bundles",
                 "find_changed_behavior", "correlate_static_and_runtime", "build_call_path"],
}


async def execute(envelop, agent):
    try:
        payload = _payload(envelop)
        action = str(payload.get("action") or "ensure").strip().lower()

        if action == "ensure":
            envelop.payload = await _ensure(agent, force=bool(payload.get("force")))
        elif action == "status":
            envelop.payload = await _status(agent)
        elif action == "stop":
            envelop.payload = await _stop(agent)
        elif action == "call":
            tool = str(payload.get("tool") or "").strip()
            if not tool:
                envelop.payload = {"ok": False, "error": "缺少 tool"}
                return envelop
            args = payload.get("arguments")
            if not isinstance(args, dict):
                args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
            # 保证连接活着
            st = await _status(agent)
            if not (st.get("pid_alive") and st.get("port_alive")):
                ens = await _ensure(agent)
                if not ens.get("ok"):
                    envelop.payload = {"ok": False, "error": "REA 未就绪: %s" % ens.get("error")}
                    return envelop
            envelop.payload = await _mcp_call(agent, "call_tool", server=SERVER_NAME,
                                              tool=tool, arguments=args)
        elif action == "tools":
            res = await _mcp_call(agent, "list_tools", server=SERVER_NAME)
            all_tools = [t.get("tool") for t in (res.get("data") or [])]
            cat = str(payload.get("category") or "").strip().lower()
            if cat:
                names = CATEGORIES.get(cat, [])
                envelop.payload = {"ok": True, "category": cat, "total": len(names),
                                   "data": names}
            else:
                envelop.payload = {"ok": True, "total": len(all_tools), "data": all_tools,
                                   "categories": {k: len(v) for k, v in CATEGORIES.items()}}
        else:
            envelop.payload = {"ok": False,
                               "error": "未知 action，可用: ensure / status / stop / call / tools"}
        return envelop

    except Exception as e:
        try:
            agent.log.error("[rea_server] %s: %s" % (type(e).__name__, e))
        except Exception:
            pass
        envelop.payload = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        return envelop


def help():
    return {
        "route": "mytools/rea_server",
        "description": "REA 逆向引擎：进程保活 + MCP 接入（229 个逆向工具）",
        "actions": {
            "ensure": {"input": {"force": "可选，重启进程"}},
            "status": {},
            "stop": {},
            "call": {"input": {"tool": "REA 工具名", "arguments": {}}},
            "tools": {"input": {"category": "可选: session/native/pe_elf/android/web_js/apple/dynamic/net/evidence"}},
        },
    }
