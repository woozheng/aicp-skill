# ============================================================
# 活动状态聚合插件 —— 一次请求返回 6 个模块的"有活动"信号
# ============================================================
# E 经验背包 / T 任务看板 / H 心跳任务 / A 分身 / S 技能 / M MCP
#
# 设计要点：
#   1. 每个模块独立容错 —— 一个挂了只标记该模块 ok=false，其余照常返回
#   2. 只读，不做任何写操作
#   3. 失败时前端保留上一次高亮（不清空）
#   4. 每个模块带 scope 字段，前端据此分组渲染（前端不需要知道字母含义）：
#        user    —— 用户级（exp）
#        session —— 会话级（task / sub / skill）
#        system  —— 实例级（cron / mcp，不带 uid，多用户共享）
#   5. 路径统一走 engine.paths —— 多 agent 布局 memories/users/<uid>/<agent_id>/...
# ============================================================

import json
from pathlib import Path

# ============================================================
# 常量
# ============================================================

DATA_ROOT = Path("data")
CRON_DIR = DATA_ROOT / "os_cron"

MODULES = ["exp", "task", "cron", "sub", "skill", "mcp"]

# 模块作用域：user / session / system
SCOPES = {
    "exp": "user",
    "task": "session",
    "cron": "system",
    "sub": "session",
    "skill": "session",
    "mcp": "system",
}

SCOPE_LABELS = {
    "user": "全局",
    "session": "本会话",
    "system": "实例",
}


def _resolve_user_id(envelop) -> str:
    meta = envelop.meta or {}
    payload = envelop.payload or {}
    # ★ 网关会剥掉 meta 里的 user_id（防伪造），前端自报的 user_id 走 payload
    uid = (
        payload.get("user_id")
        or meta.get("user_id")
        or (meta.get("auth_result") or {}).get("user_id")
        or "local"
    )
    return str(uid).strip() or "local"


def _resolve_session_id(envelop) -> str:
    """优先 payload.session_id，其次 meta.session_id。"""
    payload = envelop.payload or {}
    meta = envelop.meta or {}
    sid = payload.get("session_id") or meta.get("session_id") or ""
    return str(sid).strip()


def _resolve_agent_id_from_index(paths, user_id: str, session_id: str) -> str:
    """从用户级 index.json 反查 session_id 对应的 agent_id。"""
    if not session_id:
        return ""
    try:
        idx_file = paths.user_index_file(user_id)
        if not idx_file.exists():
            return ""
        idx = json.loads(idx_file.read_text(encoding="utf-8", errors="replace"))
        meta = (idx.get("conversations") or {}).get(session_id, {})
        return meta.get("agent_id") or ""
    except Exception:
        return ""


# ============================================================
# 各模块探测 —— 每个都独立 try/except
# ============================================================

def _probe_exp(paths, user_id: str, agent_id: str) -> dict:
    """经验背包：agent_backpack_file 是否有 items。"""
    try:
        f = paths.experiences_file()
    except Exception as e:
        return {"ok": False, "active": False, "detail": f"路径解析失败: {e}", "count": 0}
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


def _probe_task(paths, user_id: str, agent_id: str, session_id: str) -> dict:
    """任务看板：session_taskboard_file 是否有内容。"""
    if not session_id:
        return {"ok": True, "active": False, "detail": "无 session", "pending": 0}
    p = paths.session_taskboard_file(user_id, agent_id, session_id)
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


def _probe_sub(paths, user_id: str, agent_id: str, session_id: str) -> dict:
    """分身：session_tasks_file 是否有任务（与 task_manager 落盘一致）。"""
    if not session_id:
        return {"ok": True, "active": False, "detail": "无 session", "count": 0}
    f = paths.session_tasks_file(user_id, agent_id, session_id)
    if not f.exists():
        return {"ok": True, "active": False, "detail": "无分身", "count": 0}
    try:
        obj = json.loads(f.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:
        return {"ok": False, "active": False, "detail": f"读取失败: {e}", "count": 0}
    items = obj.values() if isinstance(obj, dict) else (obj or [])
    running = 0
    done = 0
    total = 0
    for t in items:
        if not isinstance(t, dict):
            continue
        total += 1
        st = str(t.get("status", "")).lower()
        if st == "running":
            running += 1
        elif st in ("done", "completed", "failed", "cancelled", "canceled"):
            done += 1
    return {
        "ok": True,
        "active": total > 0,
        "detail": f"{total} 个分身任务 / {running} 运行中",
        "count": total,
        "running": running,
        "finished": done,
    }


def _probe_skill(paths, user_id: str, agent_id: str, session_id: str) -> dict:
    """技能：本 session pinned.json 的 pinned 数。"""
    if not session_id:
        return {"ok": True, "active": False, "detail": "无 session", "count": 0, "pinned": 0}
    # skill_loader 实际落盘在旧路径 data/memories/main_agent/users/<uid>/sessions/<sid>/pinned.json
    # 新路径 paths.session_dir(...) 读不到，两处都试
    candidates = [paths.session_dir(user_id, agent_id, session_id) / "pinned.json"]
    try:
        old_dir = DATA_ROOT / "memories" / "main_agent" / "users" / user_id / "sessions" / session_id
        candidates.append(old_dir / "pinned.json")
    except Exception:
        pass
    pinned = 0
    total = 0
    for f in candidates:
        if not f.exists():
            continue
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
            continue
        if total:
            break
    # 技能库总量（跨 agent 共享）：遍历所有分类，数分类下的子目录总和
    lib = 0
    try:
        sd = paths.skills_dir()
        if sd.exists():
            for cat in sd.iterdir():
                if not cat.is_dir():
                    continue
                try:
                    lib += len([d for d in cat.iterdir() if d.is_dir()])
                except Exception:
                    pass
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
    """MCP：已连接的 server 数（读 os/_mcp_client 的落盘状态）。

    实际路径：data/os/_mcp_client/{servers.json, state.json}
    """
    base = DATA_ROOT / "os" / "_mcp_client"
    servers_file = base / "servers.json"
    state_file = base / "state.json"
    count = 0
    connected = 0
    enabled_count = 0
    if servers_file.exists():
        try:
            obj = json.loads(servers_file.read_text(encoding="utf-8", errors="replace"))
            # servers.json 是 {server_name: {url, enabled, ...}}
            if isinstance(obj, dict) and "servers" in obj:
                servers = obj.get("servers") or {}
            else:
                servers = obj or {}
            if isinstance(servers, dict):
                count = len(servers)
                for v in servers.values():
                    if isinstance(v, dict) and v.get("enabled", True):
                        enabled_count += 1
            elif isinstance(servers, list):
                count = len(servers)
                enabled_count = count
        except Exception:
            count = 0
    if state_file.exists():
        try:
            st = json.loads(state_file.read_text(encoding="utf-8", errors="replace"))
            # state.json 是 {server_name: {connected: bool, ...}}
            if isinstance(st, dict):
                for v in st.values():
                    if isinstance(v, dict) and v.get("connected"):
                        connected += 1
        except Exception:
            connected = 0
    return {
        "ok": True,
        "active": connected > 0,
        "detail": f"{count} 个 server / {connected} 已连接",
        "count": count,
        "enabled": enabled_count,
        "connected": connected,
    }


# ============================================================
# 主入口
# ============================================================

async def execute(envelop, agent):
    action = envelop.payload.get("action", "snapshot")
    user_id = _resolve_user_id(envelop)
    session_id = _resolve_session_id(envelop)

    if action != "snapshot":
        envelop.payload = {"ok": False, "error": f"未知 action: {action}"}
        return envelop

    # ★ 拿 engine.paths（统一路径来源）
    engine = getattr(agent, "engine", None)
    paths = getattr(engine, "paths", None) if engine else None
    if paths is None:
        envelop.payload = {"ok": False, "error": "engine paths 不可用"}
        return envelop

    # ★ 解析 agent_id：payload > meta > index.json 反查 > 兜底 main_v2
    meta = envelop.meta or {}
    agent_id = (
        (envelop.payload or {}).get("agent_id")
        or meta.get("agent_id")
        or _resolve_agent_id_from_index(paths, user_id, session_id)
        or "main_v2"
    )

    probes = {
        "exp": lambda: _probe_exp(paths, user_id, agent_id),
        "task": lambda: _probe_task(paths, user_id, agent_id, session_id),
        "cron": _probe_cron,
        "sub": lambda: _probe_sub(paths, user_id, agent_id, session_id),
        "skill": lambda: _probe_skill(paths, user_id, agent_id, session_id),
        "mcp": _probe_mcp,
    }

    modules = {}
    for key in MODULES:
        scope = SCOPES.get(key, "user")
        try:
            res = probes[key]() or {}
        except Exception as e:
            res = {"ok": False, "active": False, "detail": f"探测异常: {e}"}
        res["scope"] = scope
        res["scope_label"] = SCOPE_LABELS.get(scope, scope)
        modules[key] = res

    envelop.payload = {
        "ok": True,
        "data": {
            "modules": modules,
            "active": [k for k, v in modules.items() if v.get("active")],
            "scopes": SCOPES,
            "user_id": user_id,
            "agent_id": agent_id,
            "session_id": session_id,
        },
        "message": f"活动模块: {', '.join(k for k, v in modules.items() if v.get('active')) or '无'}",
    }
    return envelop


def help():
    return {
        "route": "builtins/tools/activity_status",
        "description": "活动状态聚合 —— 一次请求返回 E/T/H/A/S/M 六个模块是否有活动，并标注每个模块的作用域",
        "input": {
            "action": "snapshot（默认）",
            "session_id": "可选；不传则从 meta.session_id 取（task / sub / skill 需要）",
            "agent_id": "可选；不传则从 index.json 反查，兜底 main_v2",
        },
        "output": {
            "ok": "是否成功",
            "data.modules.<key>.ok": "该模块探测是否成功",
            "data.modules.<key>.active": "该模块是否有活动（前端据此高亮）",
            "data.modules.<key>.detail": "简要说明",
            "data.modules.<key>.scope": "作用域 user / session / system（前端据此分组）",
            "data.active": "有活动的模块 key 列表",
            "data.scopes": "模块 → 作用域 的映射表",
            "data.user_id": "本次探测的用户",
            "data.agent_id": "本次探测的 agent",
            "data.session_id": "本次探测的会话",
        },
        "scopes": {
            "user": "用户级 —— exp",
            "session": "会话级 —— task / sub / skill",
            "system": "实例级 —— cron / mcp",
        },
        "examples": [
            'action="snapshot" → {"modules": {"exp": {"scope": "user"}, "task": {"scope": "session"}}, "active": ["exp"]}',
        ],
    }
