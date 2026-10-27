# ============================================================
# 活动状态聚合插件 —— 一次请求返回 6 个模块的"有活动"信号
# ============================================================
# E 经验背包 / T 任务看板 / H 心跳任务 / A 分身 / S 技能 / M MCP
#
# 设计要点：
#   1. 每个模块独立容错 —— 一个挂了只标记该模块 ok=false，其余照常返回
#   2. 只读，不做任何写操作
#   3. 失败时前端保留上一次高亮（不清空）
#   4. Step 1-4 后：exp 读 backpack.json；task / skill 读 sessions/{sid}/ 下的文件
# ============================================================

import json
from pathlib import Path

# ============================================================
# 常量
# ============================================================

DATA_ROOT = Path("data")
MEMORY_DIR = DATA_ROOT / "memories" / "main_agent"
USERS_DIR = MEMORY_DIR / "users"
SKILLS_DIR = DATA_ROOT / "skills" / "aicp"
CRON_DIR = DATA_ROOT / "os_cron"

MODULES = ["exp", "task", "cron", "sub", "skill", "mcp"]


def _user_dir(user_id: str) -> Path:
    return USERS_DIR / user_id


def _sessions_dir(user_id: str) -> Path:
    return _user_dir(user_id) / "sessions"


def _resolve_user_id(envelop) -> str:
    meta = envelop.meta or {}
    uid = (
        meta.get("user_id")
        or meta.get("auth_result", {}).get("user_id")
        or "local"
    )
    return str(uid).strip() or "local"


def _resolve_session_id(envelop) -> str:
    """优先 payload.session_id，其次 meta.session_id。"""
    payload = envelop.payload or {}
    meta = envelop.meta or {}
    sid = payload.get("session_id") or meta.get("session_id") or ""
    return str(sid).strip()


# ============================================================
# 各模块探测 —— 每个都独立 try/except
# ============================================================

def _probe_exp(user_id: str) -> dict:
    """经验背包：backpack.json 是否有 items（Step 4 结构化）"""
    f = _user_dir(user_id) / "backpack.json"
    if not f.exists():
        return {"ok": True, "active": False, "detail": "无背包文件", "count": 0}
    try:
        obj = json.loads(f.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:
        return {"ok": False, "active": False, "detail": f"读取失败: {e}", "count": 0}
    items = obj.get("items") if isinstance(obj, dict) else None
    if not isinstance(items, list):
        items = []
    return {
        "ok": True,
        "active": len(items) > 0,
        "detail": f"{len(items)} 条经验",
        "count": len(items),
    }


def _probe_task(user_id: str, session_id: str) -> dict:
    """任务看板：sessions/{sid}/taskboard.txt 是否有内容（Step 2 迁 session 级）"""
    if not session_id:
        return {"ok": True, "active": False, "detail": "无 session", "pending": 0}
    p = _sessions_dir(user_id) / session_id / "taskboard.txt"
    if not p.exists():
        return {"ok": True, "active": False, "detail": "无看板文件", "pending": 0}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"ok": False, "active": False, "detail": f"读取失败: {e}", "pending": 0}
    lines = [l for l in text.splitlines() if l.strip()]
    pending = 0
    for l in lines:
        low = l.lower()
        if "已完成" in l or low.startswith("- [x]") or "[x]" in low:
            continue
        pending += 1
    return {
        "ok": True,
        "active": bool(lines),
        "detail": f"{len(lines)} 行 / {pending} 项未完成",
        "count": len(lines),
        "pending": pending,
    }


def _probe_cron() -> dict:
    """心跳任务：os_cron/tasks.json 里真实登记的任务（文件存在 != 有任务）"""
    tasks = {}
    tf = CRON_DIR / "tasks.json"
    if tf.exists():
        try:
            obj = json.loads(tf.read_text(encoding="utf-8", errors="replace"))
            if isinstance(obj, dict):
                tasks = obj
            elif isinstance(obj, list):
                tasks = {str(i): t for i, t in enumerate(obj)}
        except Exception:
            tasks = {}
    count = len(tasks)
    running = 0
    for t in tasks.values():
        if isinstance(t, dict) and str(t.get("status", "")).lower() == "running":
            running += 1
    return {
        "ok": True,
        "active": count > 0,
        "detail": f"{count} 个任务 / {running} 运行中",
        "count": count,
        "running": running,
    }


def _probe_sub(user_id: str) -> dict:
    """分身：task_manager 落盘的任务（文件存在 != 有分身任务）"""
    tdir = _user_dir(user_id) / "tasks"
    if not tdir.exists():
        return {"ok": True, "active": False, "detail": "无分身", "count": 0}
    try:
        files = list(tdir.glob("*.json"))
    except Exception as e:
        return {"ok": False, "active": False, "detail": f"扫描失败: {e}", "count": 0}
    running = 0
    done = 0
    total = 0
    for f in files:
        try:
            obj = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        for t in obj.values():
            if not isinstance(t, dict):
                continue
            total += 1
            st = str(t.get("status", "")).lower()
            if st == "running":
                running += 1
            elif st in ("done", "completed", "failed", "cancelled"):
                done += 1
    return {
        "ok": True,
        "active": total > 0,
        "detail": f"{total} 个分身任务 / {running} 运行中",
        "count": total,
        "running": running,
        "finished": done,
    }


def _probe_skill(user_id: str, session_id: str) -> dict:
    """技能：本 session 真正钉住（pinned）的技能数 —— 读 sessions/{sid}/pinned.json
    （Step 3 后 pinned 是 session 级，user 级 skill_state.json 已废弃）"""
    if not session_id:
        return {"ok": True, "active": False, "detail": "无 session", "count": 0, "pinned": 0}
    f = _sessions_dir(user_id) / session_id / "pinned.json"
    pinned = 0
    total = 0
    if f.exists():
        try:
            obj = json.loads(f.read_text(encoding="utf-8", errors="replace"))
            pinned_map = obj.get("pinned", {}) if isinstance(obj, dict) else {}
            items = pinned_map.values() if isinstance(pinned_map, dict) else (pinned_map or [])
            total = len(items)
            pinned = sum(
                1 for v in items
                if isinstance(v, dict) and str(v.get("status", "")).lower() == "pinned"
            )
        except Exception:
            pass
    # 技能库总量（仅作展示，不参与 active 判定）
    lib = 0
    if SKILLS_DIR.exists():
        try:
            lib = len([d.name for d in SKILLS_DIR.iterdir() if d.is_dir()])
        except Exception:
            lib = 0
    return {
        "ok": True,
        "active": pinned > 0,
        "detail": f"{lib} 个技能 / {pinned} 项钉住",
        "count": lib,
        "pinned": pinned,
        "total": total,
    }


def _probe_mcp() -> dict:
    """MCP：已连接的 server 数（读 _mcp_client 的落盘状态）"""
    base = DATA_ROOT / "os" / "mcp_client"
    servers_file = base / "servers.json"
    state_file = base / "state.json"
    count = 0
    connected = 0
    if servers_file.exists():
        try:
            obj = json.loads(servers_file.read_text(encoding="utf-8", errors="replace"))
            servers = obj if isinstance(obj, list) else obj.get("servers", [])
            if isinstance(servers, dict):
                servers = list(servers.values())
            count = len(servers)
        except Exception:
            count = 0
    if state_file.exists():
        try:
            st = json.loads(state_file.read_text(encoding="utf-8", errors="replace"))
            connected = len(st.get("connected", []) or [])
        except Exception:
            connected = 0
    if not count and not connected:
        alt = DATA_ROOT / "mcp_client"
        if alt.exists():
            try:
                count = len([f for f in alt.rglob("*.json") if f.is_file()])
            except Exception:
                count = 0
    return {
        "ok": True,
        "active": count > 0 or connected > 0,
        "detail": f"{count} 个 server / {connected} 已连接",
        "count": count,
        "connected": connected,
    }


# ============================================================
# 主入口
# ============================================================

async def execute(envelop, agent):
    params = envelop.payload.get("params", {}) or {}
    action = envelop.payload.get("action", "snapshot")
    user_id = _resolve_user_id(envelop)
    session_id = _resolve_session_id(envelop)

    if action != "snapshot":
        envelop.payload = {"ok": False, "error": f"未知 action: {action}"}
        return envelop

    probes = {
        "exp": lambda: _probe_exp(user_id),
        "task": lambda: _probe_task(user_id, session_id),
        "cron": _probe_cron,
        "sub": lambda: _probe_sub(user_id),
        "skill": lambda: _probe_skill(user_id, session_id),
        "mcp": _probe_mcp,
    }

    modules = {}
    for key in MODULES:
        try:
            modules[key] = probes[key]()
        except Exception as e:
            modules[key] = {"ok": False, "active": False, "detail": f"探测异常: {e}"}

    envelop.payload = {
        "ok": True,
        "data": {
            "modules": modules,
            "active": [k for k, v in modules.items() if v.get("active")],
            "user_id": user_id,
            "session_id": session_id,
        },
        "message": f"活动模块: {', '.join(k for k, v in modules.items() if v.get('active')) or '无'}",
    }
    return envelop


def help():
    return {
        "route": "builtins/tools/activity_status",
        "description": "活动状态聚合 —— 一次请求返回 E/T/H/A/S/M 六个模块是否有活动",
        "input": {
            "action": "snapshot（默认）",
            "session_id": "可选；不传则从 meta.session_id 取（task / skill 需要）",
        },
        "output": {
            "ok": "是否成功",
            "data.modules.<key>.ok": "该模块探测是否成功",
            "data.modules.<key>.active": "该模块是否有活动（前端据此高亮）",
            "data.modules.<key>.detail": "简要说明",
            "data.active": "有活动的模块 key 列表",
            "data.session_id": "本次探测的会话",
        },
        "examples": [
            'action="snapshot" → {"modules": {"exp": {...}, "task": {...}, ...}, "active": ["exp"]}',
        ],
    }
