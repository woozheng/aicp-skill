"""task_manager — 分身任务管理器

职责：
- 创建分身任务（sub_ session）
- 给已有分身发送消息（续跑/改进）
- 拼装所有 meta（callback_receiver / _task_id）
- 接收回调，匹配 task_id，更新状态
- 提供 list / status / collect 查询
- 通知 main_agent 有新进展

路径：data/memories/main_agent/users/{user_id}/tasks/{parent_session}.json
user_id 从 envelop.meta["user_id"] 取，缺省 "local"

每次分身完成都通知 main_agent，由 LLM 自己决定 reply / send / collect。
"""

import json
import uuid
import asyncio
from datetime import datetime
from pathlib import Path
import tempfile
import os
import core


ACTIONS_SCHEMA = {
    "create": {
        "description": "创建一个分身任务（自动分配 session，异步执行）",
        "params": {
            "description": {"type": "string", "description": "任务简述（用于列表展示）"},
            "task": {"type": "string", "description": "给分身的完整任务描述"}
        },
        "required": ["description", "task"]
    },
    "send": {
        "description": "给已有分身发送消息（续跑/改进，保留分身的记忆）",
        "params": {
            "task_id": {"type": "string", "description": "任务 ID（sub_xxx）"},
            "message": {"type": "string", "description": "要发送的消息"}
        },
        "required": ["task_id", "message"]
    },
    "list": {
        "description": "列出当前 session 的所有分身任务",
        "params": {
            "status": {"type": "string", "description": "可选筛选：running / done / failed / timeout"}
        },
        "required": []
    },
    "status": {
        "description": "查看某个分身任务的详细状态",
        "params": {
            "task_id": {"type": "string", "description": "任务 ID"}
        },
        "required": ["task_id"]
    },
    "cancel": {
        "description": "取消某个分身任务（仅标记，不强制中断）",
        "params": {
            "task_id": {"type": "string", "description": "任务 ID"}
        },
        "required": ["task_id"]
    },
    "collect": {
        "description": "收集所有已完成任务的结果",
        "params": {
            "task_id": {"type": "string", "description": "可选，指定单个任务 ID"},
            "wait": {"type": "boolean", "description": "是否等待未完成任务（默认 false）"},
            "timeout": {"type": "number", "description": "等待超时秒数（默认 30，上限 30）"}
        },
        "required": []
    },
    "clear": {
        "description": "批量清理所有已结束的分身（任务记录 + flow 文件）",
        "params": {},
        "required": []
    },
    "delete": {
        "description": "永久删除指定的分身（任务记录 + flow 文件）",
        "params": {
            "task_ids": {
                "type": "array",
                "description": "要删除的任务 ID 列表，如 [\"sub_a3f8b2c1\", \"sub_9e520e28\"]"
            },
            "task_id": {
                "type": "string",
                "description": "（可选）单个任务 ID，等价于 task_ids=[task_id]"
            }
        },
        "required": []
    }
}


# ============================================================
# 常量
# ============================================================

MEMORY_DIR = Path("data/memories/main_agent")
USER_ROOT = MEMORY_DIR / "users"
CONVERSATIONS_DIR = MEMORY_DIR / "conversations"
MAX_CONCURRENT_TASKS = 5
TASK_TIMEOUT_SECONDS = 600
MAX_COLLECT_WAIT = 30
SUB_AGENT_TIMEOUT = 3600   # 分身单次执行最长 1 小时


# ============================================================
# 身份 / 路径
# ============================================================

def _resolve_user_id(envelop) -> str:
    meta = envelop.meta or {}
    uid = (
        meta.get("user_id")
        or meta.get("auth_result", {}).get("user_id")
        or "local"
    )
    return str(uid).strip() or "local"


def _tasks_file(user_id: str, parent_session: str) -> Path:
    """分身任务存储：sessions/{parent_session}/tasks.json（session 级，与 main_agent 一致）"""
    d = USER_ROOT / user_id / "sessions" / parent_session
    d.mkdir(parents=True, exist_ok=True)
    return d / "tasks.json"


# ============================================================
# 存储
# ============================================================

def _load_tasks(user_id: str, parent_session: str) -> dict:
    f = _tasks_file(user_id, parent_session)
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_tasks(user_id: str, parent_session: str, tasks: dict):
    f = _tasks_file(user_id, parent_session)
    content = json.dumps(tasks, ensure_ascii=False, indent=2)
    temp_fd, temp_path = tempfile.mkstemp(
        dir=str(f.parent), prefix=f".{f.name}_", suffix=".tmp"
    )
    try:
        with os.fdopen(temp_fd, 'w', encoding='utf-8') as fp:
            fp.write(content)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(temp_path, str(f))
    except Exception:
        try:
            os.close(temp_fd)
            os.unlink(temp_path)
        except Exception:
            pass


def _now() -> str:
    return datetime.now().isoformat()


def _update_task(user_id: str, parent_session: str, task_id: str, patch: dict):
    tasks = _load_tasks(user_id, parent_session)
    if task_id not in tasks:
        return False
    tasks[task_id].update(patch)
    _save_tasks(user_id, parent_session, tasks)
    return True


def _count_running(user_id: str, parent_session: str) -> int:
    tasks = _load_tasks(user_id, parent_session)
    return sum(1 for t in tasks.values() if t.get("status") == "running")


def _check_timeouts(user_id: str, parent_session: str):
    tasks = _load_tasks(user_id, parent_session)
    now = datetime.now()
    changed = False
    for tid, t in tasks.items():
        if t.get("status") != "running":
            continue
        try:
            created = datetime.fromisoformat(t["created_at"])
            if (now - created).total_seconds() > TASK_TIMEOUT_SECONDS:
                t["status"] = "timeout"
                t["error"] = f"任务超过 {TASK_TIMEOUT_SECONDS} 秒未完成"
                t["finished_at"] = _now()
                changed = True
        except Exception:
            pass
    if changed:
        _save_tasks(user_id, parent_session, tasks)


def _format_summary(user_id: str, parent_session: str) -> str:
    tasks = _load_tasks(user_id, parent_session)
    if not tasks:
        return ""
    lines = ["【分身任务】"]
    for tid, t in sorted(tasks.items(), key=lambda x: x[1].get("created_at", "")):
        status = t.get("status", "unknown")
        icon = {
            "running": "⏳",
            "done": "✅",
            "failed": "❌",
            "timeout": "⏰",
            "cancelled": "🚫",
        }.get(status, "?")
        desc = t.get("description", "")
        lines.append(f"{icon} {tid}: {desc} [{status}]")
    return "\n".join(lines)

SESSIONS_DIR = MEMORY_DIR / "sessions"     # ← 改名，路径变了

def _delete_flow_files(user_id: str, task_ids: list) -> int:
    """删分身的 flow 目录（sessions/{tid}/）+ 清 index 条目。"""
    deleted = 0
    sessions_base = USER_ROOT / user_id / "sessions"
    if not sessions_base.exists():
        return 0
    for tid in task_ids:
        session_dir = sessions_base / tid
        if session_dir.exists():
            try:
                import shutil
                shutil.rmtree(session_dir, ignore_errors=True)
                deleted += 1
            except Exception:
                pass

    # 清理 index.json 里的孤儿记录
    index_file = sessions_base / "index.json"
    if index_file.exists():
        try:
            index = json.loads(index_file.read_text(encoding="utf-8"))
            convs = index.get("conversations", {})
            changed = False
            for tid in task_ids:
                if tid in convs:
                    del convs[tid]
                    changed = True
            if changed:
                index["conversations"] = convs
                index_file.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    return deleted


def _extract_content(payload: dict) -> str:
    """从 payload 里提取正文。"""
    if not isinstance(payload, dict):
        return ""
    for key in ("content", "reply", "result", "data"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            for k2 in ("content", "reply", "result"):
                v2 = v.get(k2)
                if isinstance(v2, str) and v2.strip():
                    return v2.strip()
    return ""


# ============================================================
# action 处理器
# ============================================================

async def _handle_create(agent, args, user_id, session_id, flow):
    description = args.get("description", "").strip()
    task_text = args.get("task", "").strip()

    if not description or not task_text:
        return {"ok": False, "think": "缺少 description 或 task"}

    if _count_running(user_id, session_id) >= MAX_CONCURRENT_TASKS:
        return {
            "ok": False,
            "think": f"并发任务已达上限（{MAX_CONCURRENT_TASKS}）。请等现有任务完成，或用 task_manager.list 查看。"
        }

    task_id = f"sub_{uuid.uuid4().hex[:8]}"

    tasks = _load_tasks(user_id, session_id)
    tasks[task_id] = {
        "task_id": task_id,
        "parent_session": session_id,
        "user_id": user_id,
        "description": description,
        "task": task_text,
        "status": "running",
        "created_at": _now(),
        "finished_at": None,
        "result": None,
        "error": None,
    }
    _save_tasks(user_id, session_id, tasks)

    call_meta = {
        "user_id": user_id,
        "callback_receiver": "builtins/tools/task_manager",
        "callback_session_id": session_id,
        "_task_id": task_id,
        "_parent_session": session_id,
        "_description": description,   # ★ 加
    }

        # ★ 提交到后台，不阻塞 create 返回（否则主控 _process_internal 挂起，
        #   前端在等 create 的 HTTP 响应期间，session 列表/flow 都刷不出来）
    async def _run_sub():
        # asyncio.create_task 不继承 contextvars，显式设置当前 session
        try:
            from runtime._llm import set_current_session
            set_current_session(task_id)
        except Exception:
            pass
        try:
            await agent.system.call(core.Envelop(
                sender="builtins/tools/task_manager",
                receiver="builtins/agents/main_agent",
                payload={
                    "content": task_text,
                    "session_id": task_id,
                    "action": "",
                },
                meta=call_meta,
            ), timeout=SUB_AGENT_TIMEOUT)
        except Exception as e:
            _update_task(user_id, session_id, task_id, {
                "status": "failed",
                "error": f"创建分身失败: {e}",
                "finished_at": _now(),
            })

    asyncio.create_task(_run_sub())

    return {
        "ok": True,
        "think": f"✅ 分身任务已创建：{task_id}",
        "data": {
            "task_id": task_id,
            "description": description,
            "status": "running",
        },
        "_summary": _format_summary(user_id, session_id),
    }


async def _handle_send(agent, args, user_id, session_id, flow):
    task_id = args.get("task_id", "").strip()
    message = args.get("message", "").strip()

    if not task_id or not message:
        return {"ok": False, "think": "缺少 task_id 或 message"}

    tasks = _load_tasks(user_id, session_id)
    t = tasks.get(task_id)
    if not t:
        return {"ok": False, "think": f"任务不存在: {task_id}"}

    _update_task(user_id, session_id, task_id, {
        "status": "running",
        "finished_at": None,
        "error": None,
    })

    call_meta = {
        "user_id": user_id,
        "callback_receiver": "builtins/tools/task_manager",
        "callback_session_id": session_id,
        "_task_id": task_id,
        "_parent_session": session_id,
    }

    # ★ 提交到后台，不阻塞 create 返回（否则主控 _process_internal 挂起，
    #   前端在等 create 的 HTTP 响应期间，session 列表/flow 都刷不出来）
    async def _run_sub():
        # asyncio.create_task 不继承 contextvars，显式设置当前 session
        try:
            from runtime._llm import set_current_session
            set_current_session(task_id)
        except Exception:
            pass
        try:
            await agent.system.call(core.Envelop(
                sender="builtins/tools/task_manager",
                receiver="builtins/agents/main_agent",
                payload={
                    "content": task_text,
                    "session_id": task_id,
                    "action": "",
                },
                meta=call_meta,
            ), timeout=SUB_AGENT_TIMEOUT)
        except Exception as e:
            _update_task(user_id, session_id, task_id, {
                "status": "failed",
                "error": f"创建分身失败: {e}",
                "finished_at": _now(),
            })

    asyncio.create_task(_run_sub())

    return {
        "ok": True,
        "think": f"✅ 分身任务已创建：{task_id}",
        "data": {
            "task_id": task_id,
            "description": description,
            "status": "running",
        },
        "_summary": _format_summary(user_id, session_id),
    }


async def _handle_list(agent, args, user_id, session_id, flow):
    _check_timeouts(user_id, session_id)
    tasks = _load_tasks(user_id, session_id)
    status_filter = args.get("status", "")

    result = []
    for tid, t in tasks.items():
        if status_filter and t.get("status") != status_filter:
            continue
        result.append({
            "task_id": tid,
            "description": t.get("description", ""),
            "status": t.get("status", ""),
            "created_at": t.get("created_at"),
            "finished_at": t.get("finished_at"),
        })

    return {
        "ok": True,
        "think": f"共 {len(result)} 个任务",
        "data": {
            "tasks": result,
            "running_count": _count_running(user_id, session_id),
            "max_concurrent": MAX_CONCURRENT_TASKS,
        },
        "_summary": _format_summary(user_id, session_id),
    }


async def _handle_status(agent, args, user_id, session_id, flow):
    task_id = args.get("task_id", "")
    if not task_id:
        return {"ok": False, "think": "缺少 task_id"}

    tasks = _load_tasks(user_id, session_id)
    t = tasks.get(task_id)
    if not t:
        return {"ok": False, "think": f"任务不存在: {task_id}"}

    return {
        "ok": True,
        "think": f"任务 {task_id} 状态: {t.get('status')}",
        "data": t,
    }


async def _handle_cancel(agent, args, user_id, session_id, flow):
    task_id = args.get("task_id", "")
    if not task_id:
        return {"ok": False, "think": "缺少 task_id"}

    tasks = _load_tasks(user_id, session_id)
    t = tasks.get(task_id)
    if not t:
        return {"ok": False, "think": f"任务不存在: {task_id}"}

    if t.get("status") != "running":
        return {"ok": False, "think": f"任务 {task_id} 已结束（{t.get('status')}），无法取消"}

    _update_task(user_id, session_id, task_id, {
        "status": "cancelled",
        "error": "被调用方取消",
        "finished_at": _now(),
    })

    return {
        "ok": True,
        "think": f"任务 {task_id} 已标记取消",
        "data": {"task_id": task_id},
        "_summary": _format_summary(user_id, session_id),
    }


async def _handle_collect(agent, args, user_id, session_id, flow):
    task_id = args.get("task_id", "")
    wait = bool(args.get("wait", False))
    try:
        timeout = float(args.get("timeout", MAX_COLLECT_WAIT))
    except (TypeError, ValueError):
        timeout = MAX_COLLECT_WAIT
    timeout = min(max(timeout, 0), MAX_COLLECT_WAIT)

    if task_id:
        _check_timeouts(user_id, session_id)
        tasks = _load_tasks(user_id, session_id)
        t = tasks.get(task_id)
        if not t:
            return {"ok": False, "think": f"任务不存在: {task_id}"}
        result = t.get("result") or {}
        return {
            "ok": True,
            "think": f"任务 {task_id} 状态: {t.get('status')}",
            "data": {
                "task_id": task_id,
                "description": t.get("description", ""),
                "status": t.get("status"),
                "content": (result.get("content") if isinstance(result, dict) else "") or "",
                "result": result,
                "error": t.get("error"),
                "created_at": t.get("created_at"),
                "finished_at": t.get("finished_at"),
            },
            "_summary": _format_summary(user_id, session_id),
        }

    _check_timeouts(user_id, session_id)

    if wait and _count_running(user_id, session_id) > 0:
        try:
            await agent.system.call(core.Envelop(
                sender="builtins/tools/task_manager",
                receiver="os/_websocket",
                payload={
                    "action": "push",
                    "channel_id": f"pa_{user_id}",
                    "data": {
                        "type": "progress",
                        "session_id": session_id,
                        "step": "executing",
                        "status": "start",
                        "call_id": f"collect_wait_{uuid.uuid4().hex[:6]}",
                        "target": "task_manager.collect",
                        "msg": f"⏳ 等待分身完成（最多 {timeout:.0f}s）",
                        "agent_type": "task_manager",
                    }
                }
            ))
        except Exception:
            pass

        start = asyncio.get_event_loop().time()
        while True:
            if _count_running(user_id, session_id) == 0:
                break
            elapsed = asyncio.get_event_loop().time() - start
            if elapsed >= timeout:
                break
            await asyncio.sleep(0.5)
            _check_timeouts(user_id, session_id)

    tasks = _load_tasks(user_id, session_id)
    done = {}
    running = {}
    failed = {}

    for tid, t in tasks.items():
        status = t.get("status")
        result = t.get("result") or {}
        if status == "done":
            done[tid] = {
                "description": t.get("description", ""),
                "content": (result.get("content") if isinstance(result, dict) else "") or "",
                "result": result,
            }
        elif status == "running":
            running[tid] = t.get("description", "")
        else:
            failed[tid] = {
                "description": t.get("description", ""),
                "status": status,
                "error": t.get("error"),
            }

    return {
        "ok": True,
        "think": f"完成 {len(done)} / 运行中 {len(running)} / 失败 {len(failed)}",
        "data": {
            "done": done,
            "running": running,
            "failed": failed,
        },
        "_summary": _format_summary(user_id, session_id),
    }


async def _handle_clear(agent, args, user_id, session_id, flow):
    tasks = _load_tasks(user_id, session_id)
    kept = {tid: t for tid, t in tasks.items() if t.get("status") in ("pending", "running")}
    removed_tids = [tid for tid in tasks if tid not in kept]
    removed = len(removed_tids)
    _save_tasks(user_id, session_id, kept)
    deleted_flows = _delete_flow_files(user_id, removed_tids)

    try:
        await agent.system.call(core.Envelop(
            sender="builtins/tools/task_manager",
            receiver="os/_websocket",
            payload={
                "action": "push",
                "channel_id": f"pa_{user_id}",
                "data": {
                    "type": "progress",
                    "session_id": session_id,
                    "step": "done",
                    "msg": f"清理分身：{removed} 个记录，{deleted_flows} 个 flow",
                    "agent_type": "task_manager"
                }
            }
        ))
    except Exception:
        pass

    return {
        "ok": True,
        "think": f"已清空 {removed} 个已结束任务（含 {deleted_flows} 个 flow 文件）",
        "data": {
            "removed": removed,
            "deleted_flows": deleted_flows,
            "kept": len(kept),
        },
        "_summary": _format_summary(user_id, session_id),
    }


async def _handle_delete(agent, args, user_id, session_id, flow):
    task_ids = args.get("task_ids", [])
    if not task_ids:
        single = args.get("task_id", "").strip()
        if single:
            task_ids = [single]
    if not task_ids:
        return {"ok": False, "think": "缺少 task_ids 或 task_id"}
    if not isinstance(task_ids, list):
        task_ids = [task_ids]

    tasks = _load_tasks(user_id, session_id)
    deleted = []
    skipped = []

    for tid in task_ids:
        tid = str(tid).strip()
        if not tid:
            continue
        if tid not in tasks:
            skipped.append({"task_id": tid, "reason": "不存在"})
            continue
        if tasks[tid].get("status") == "running":
            skipped.append({"task_id": tid, "reason": "正在运行，请先 cancel"})
            continue
        del tasks[tid]
        deleted.append(tid)

    _save_tasks(user_id, session_id, tasks)
    deleted_flows = _delete_flow_files(user_id, removed_tids)

    try:
        await agent.system.call(core.Envelop(
            sender="builtins/tools/task_manager",
            receiver="os/_websocket",
            payload={
                "action": "push",
                "channel_id": f"pa_{user_id}",
                "data": {
                    "type": "progress",
                    "session_id": session_id,
                    "step": "done",
                    "msg": f"删除分身：{len(deleted)} 个记录，{deleted_flows} 个 flow",
                    "agent_type": "task_manager"
                }
            }
        ))
    except Exception:
        pass

    return {
        "ok": True,
        "think": f"已删除 {len(deleted)} 个分身（含 {deleted_flows} 个 flow 文件）",
        "data": {
            "deleted": deleted,
            "deleted_flows": deleted_flows,
            "skipped": skipped,
        },
        "_summary": _format_summary(user_id, session_id),
    }


# ============================================================
# 回调处理
# ============================================================

async def _handle_callback(envelop, agent, user_id):
    """分身完成后的回调。每次回调都通知 main_agent，由 LLM 自主决策。"""
    meta = envelop.meta or {}
    task_id = meta.get("_task_id", "")
    parent_session = meta.get("callback_session_id", "")

    if not task_id or not parent_session:
        return envelop

    payload = envelop.payload or {}
    if not isinstance(payload, dict):
        payload = {"data": payload}

    # 中间态：分身刚提交，不更新状态
    p_status = str(payload.get("status") or "").lower()
    p_call = str(payload.get("call") or "").lower()
    if p_status == "processing" or p_call == "processing":
        _update_task(user_id, parent_session, task_id, {
            "last_callback_at": _now(),
            "last_callback_status": "processing",
        })
        return envelop

    # 最终态
    final_content = _extract_content(payload)
    plugin_ok = payload.get("ok", True) is not False
    error = str(payload.get("error", "") or "")[:500] if not plugin_ok else ""
    if not final_content and not plugin_ok:
        error = error or "分身返回空结果"

    tasks = _load_tasks(user_id, parent_session)
    t = tasks.get(task_id, {})
    if t.get("status") in ("done", "failed") and t.get("finished_at"):
        return envelop

    _update_task(user_id, parent_session, task_id, {
        "status": "done" if plugin_ok else "failed",
        "finished_at": _now(),
        "result": {
            "ok": plugin_ok,
            "content": final_content,
            "error": error or None,
            "raw": payload,
        },
        "error": error or None,
    })

    # ★ 每次回调都通知 main_agent（LLM 自己决定 reply / send / collect）
    try:
        await agent.system.call(core.Envelop(
            sender="builtins/tools/task_manager",
            receiver="builtins/agents/main_agent",
            payload={"content": "", "action": ""},
            meta={
                "session_id": parent_session,
                "user_id": user_id,
                "is_callback": True,
                "callback_session_id": parent_session,
                "_task_id": task_id,
                "_task_summary": _format_summary(user_id, parent_session),
            },
        ))
    except Exception:
        pass

    return envelop


# ============================================================
# 主入口
# ============================================================

async def execute(envelop, agent):
    user_id = _resolve_user_id(envelop)

    if envelop.meta.get("is_callback"):
        return await _handle_callback(envelop, agent, user_id)

    params = envelop.payload.get("params", {})
    if not params:
        params = {k: v for k, v in envelop.payload.items() if k != "action"}

    action = envelop.payload.get("action", "list")
    session_id = envelop.meta.get("session_id", "default")

    if session_id.startswith("sub_"):
        envelop.payload = {
            "ok": False,
            "error": "分身不能再使用 task_manager（避免无限嵌套）。请直接在当前执行线上完成任务。"
        }
        return envelop

    handlers = {
        "create": _handle_create,
        "send": _handle_send,
        "list": _handle_list,
        "status": _handle_status,
        "cancel": _handle_cancel,
        "collect": _handle_collect,
        "clear": _handle_clear,
        "delete": _handle_delete,
    }

    handler = handlers.get(action)
    if not handler:
        envelop.payload = {"ok": False, "error": f"未知 action: {action}"}
        return envelop

    try:
        result = await handler(agent, params, user_id, session_id, None)
    except Exception as e:
        envelop.payload = {"ok": False, "error": f"task_manager 异常: {e}"}
        return envelop

    summary = result.pop("_summary", None)
    envelop.payload = {
        "ok": result.get("ok", False),
        "data": result.get("data", {}),
        "message": result.get("think", ""),
    }
    if summary:
        envelop.payload["data"]["_summary"] = summary

    return envelop


def help():
    return {
        "route": "builtins/tools/task_manager",
        "description": "分身任务管理器 —— 创建、追踪、续跑、收集、清理并行子任务（users/{user_id}/tasks/{session_id}.json）",
        "input": {
            "action": "create | send | list | status | cancel | collect | clear | delete",
            "description": "任务简述（create 时）",
            "task": "完整任务描述（create 时）",
            "task_id": "任务 ID（send / status / cancel / delete 时）",
            "message": "要发送的消息（send 时）",
            "task_ids": "任务 ID 列表（delete 时，支持批量）",
            "status": "筛选状态（list 时）",
            "wait": "是否等待（collect 时，最大 30 秒）",
            "timeout": "等待超时秒数（collect 时，默认 30，上限 30）",
        },
        "output": {
            "ok": "是否成功",
            "data.task_id": "任务 ID（create / send 时）",
            "data.tasks": "任务列表",
            "data.done": "已完成任务结果（collect 时，含 content）",
            "data.deleted": "已删除的任务 ID（delete 时）",
            "message": "结果消息",
        },
        "examples": [
            'create(description="调研 A", task="详细调研 A 方向的三个子问题") → 自动分配 sub_xxx',
            'send(task_id="sub_xxx", message="把 X 部分重做，用 Y 角度") → 分身带着记忆继续',
            'collect() → {"done": {"sub_xxx": {"content": "正文", ...}}, "running": {...}, "failed": {...}}',
            'delete(task_ids=["sub_a3f8b2c1", "sub_9e520e28"]) → 精确删除多个分身',
            'clear() → 批量清理所有已结束的分身（含 flow 文件）',
        ],
    }
