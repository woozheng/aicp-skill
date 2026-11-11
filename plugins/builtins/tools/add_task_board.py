"""add_task_board — 更新当前任务看板（session 级）

路径（与引擎 runtime/engine/paths.py 保持一致）：
  data/memories/users/{user_id}/{agent_id}/sessions/{session_id}/taskboard.txt

user_id / session_id / agent_id 从 envelop.meta 取，缺省 "local" / "main_agent"。
"""

from pathlib import Path


# ============================================================
# 内部工具函数
# ============================================================

MEMORY_DIR = Path("data/memories")
USER_ROOT = MEMORY_DIR / "users"
DEFAULT_AGENT_ID = "main_agent"


def _resolve_session_id(envelop) -> str:
    """从 meta 取 session_id（引擎已透传）。"""
    meta = envelop.meta or {}
    sid = meta.get("session_id") or meta.get("callback_session_id") or ""
    return str(sid).strip()


def _resolve_user_id(envelop) -> str:
    """从 meta 取 user_id，缺省 local（与引擎 resolve_ids 一致）。"""
    meta = envelop.meta or {}
    uid = (
        meta.get("user_id")
        or meta.get("auth_result", {}).get("user_id")
        or "local"
    )
    return str(uid).strip() or "local"


def _resolve_agent_id(envelop) -> str:
    """从 meta 取 agent_id，缺省 main_agent（引擎 dispatch_tool 已透传）。"""
    meta = envelop.meta or {}
    aid = meta.get("agent_id") or DEFAULT_AGENT_ID
    return str(aid).strip() or DEFAULT_AGENT_ID


def _user_dir(user_id: str) -> Path:
    p = USER_ROOT / user_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def _get_taskboard_file(user_id: str, agent_id: str, session_id: str) -> Path:
    """获取任务看板文件路径（session 级：users/<uid>/<agent_id>/sessions/<sid>/taskboard.txt）"""
    if not session_id:
        # 兜底：没有 session_id 时退回 agent 级（兼容旧调用），但正常路径不该走到
        base = USER_ROOT / user_id / agent_id
        base.mkdir(parents=True, exist_ok=True)
        return base / "taskboard.txt"
    base = USER_ROOT / user_id / agent_id / "sessions" / session_id
    base.mkdir(parents=True, exist_ok=True)
    return base / "taskboard.txt"


def _load_taskboard(user_id: str, agent_id: str, session_id: str) -> str:
    file_path = _get_taskboard_file(user_id, agent_id, session_id)
    if file_path.exists():
        return file_path.read_text(encoding="utf-8").strip()
    return ""


def _save_taskboard(user_id: str, agent_id: str, session_id: str, content: str):
    file_path = _get_taskboard_file(user_id, agent_id, session_id)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")


# ============================================================
# 主入口
# ============================================================

async def execute(envelop, agent):
    """更新当前任务看板

    action:
      - replace（默认）：覆盖看板
      - append：追加到看板
      - clear：清空看板（不需要 content）
    """
    # ★ 兼容两种传参：params 子对象 或 平铺
    params = envelop.payload.get("params", {})
    if not isinstance(params, dict) or not params:
        params = {
            k: v for k, v in envelop.payload.items()
            if k not in ("action", "target", "think", "retain")
        }

    # ★ 身份级：从 meta 取 user_id / session_id / agent_id（引擎已透传）
    user_id = _resolve_user_id(envelop)
    session_id = _resolve_session_id(envelop)
    agent_id = _resolve_agent_id(envelop)
    if not session_id:
        envelop.payload = {"ok": False, "error": "缺少 session_id，无法定位任务看板（需经 main_agent 调用）"}
        return envelop
    # ★ action 决定操作
    action = str(envelop.payload.get("action", "") or params.get("action", "") or "replace").lower()
    if action not in ("replace", "append", "clear"):
        action = "replace"

    content = params.get("content", "")

    # clear：不需要 content
    if action == "clear":
        try:
            _save_taskboard(user_id, agent_id, session_id, "")
        except Exception as e:
            envelop.payload = {"ok": False, "error": f"清空任务看板失败: {e}"}
            return envelop
        envelop.payload = {
            "ok": True,
            "message": "✅ 任务看板已清空",
            "action": "clear",
            "user_id": user_id,
            "agent_id": agent_id,
            "size": 0,
        }
        return envelop

    # replace / append：需要 content
    if not content:
        envelop.payload = {"ok": False, "error": "缺少 content 参数"}
        return envelop

    try:
        current = _load_taskboard(user_id, agent_id, session_id)
        if action == "append":
            new_content = (current + "\n" + content) if current else content
        else:
            new_content = content

        _save_taskboard(user_id, agent_id, session_id, new_content)

        envelop.payload = {
            "ok": True,
            "message": f"✅ 任务看板已{'追加' if action == 'append' else '更新'}，当前 {len(new_content)} 字",
            "action": action,
            "user_id": user_id,
            "agent_id": agent_id,
            "size": len(new_content),
        }
        return envelop

    except Exception as e:
        envelop.payload = {"ok": False, "error": f"保存任务看板失败: {e}"}
        return envelop


def help():
    return {
        "route": "builtins/tools/add_task_board",
        "description": "更新当前任务看板（session 级：data/memories/users/{user_id}/{agent_id}/sessions/{session_id}/taskboard.txt；agent_id 取自 envelop.meta，缺省 main_agent；记录当前任务进度和上下文）",
        "input": {
            "action": "replace（默认）/ append / clear",
            "content": "任务看板内容（纯文本；clear 时不需要）",
        },
        "output": {
            "ok": "是否成功",
            "message": "结果消息",
            "action": "使用的操作",
            "user_id": "写入的身份目录（local / u_xxx）",
            "agent_id": "写入的 agent 目录",
            "size": "看板大小",
        },
    }
