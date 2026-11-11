"""Engine —— 引擎实例。

职责：
  1. 持有所有跨 envelop 的进程内状态（flows / turn_states / interrupt_signals /
     empty_retries / narration_retries / active_sessions）
  2. 提供 build(cfg) 编译 agent 配置为 execute 闭包
  3. 提供 execute(envelop) 处理 17 个 action + 分发到具体 agent
  4. 提供 get_flow / get_turn_state / signal_interrupt 给插件用
  5. 提供 paths 属性给插件 / code_runner 拿统一路径

存储：data/memories/users/<uid>/<agent_id>/sessions/<sid>/
"""
import asyncio
import json
import os
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional

import core
from . import backpack
from . import paths
from .backpack import load_backpack
from .constants import (
    MAX_RECURSION_DEPTH, CONTEXT_PRESSURE_THRESHOLD,
    SESSION_FLOWS_MAX,
)
from .flow import Flow
from .paths import (
    user_index_file, session_dir, session_taskboard_file,
    session_debug_dir,
)
from .pool import AgentPool
from .push import Push
from .schemas import SCHEMAS
from .tail import build_tail
from .think import (
    think, process, push_stats,
    EmptyOutputError, NarrationReplyError, ContentSafetyBlockedError,
)
from .tools import dispatch_tool, handle_skill, tool_manage_flow


# ============================================================
# 身份解析
# ============================================================
def _resolve_ids(envelop):
    payload = envelop.payload or {}
    meta = envelop.meta or {}
    user_id = (
        meta.get("user_id")
        or meta.get("auth_result", {}).get("user_id")
        or "local"
    )
    session_id = (
        payload.get("session_id")
        or meta.get("session_id")
        or f"auto_{uuid.uuid4().hex[:12]}"
    )
    return user_id, session_id


def _json_safe(obj, _depth=0):
    if _depth > 12:
        return str(obj)
    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8")
        except Exception:
            return repr(obj)
    if isinstance(obj, dict):
        return {str(k): _json_safe(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v, _depth + 1) for v in obj]
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    return str(obj)


def _key(agent_id, session_id):
    return f"{agent_id}:{session_id}"


_processing_locks: Dict[str, asyncio.Lock] = {}


def _get_processing_lock(agent_id, session_id):
    k = f"{agent_id}:{session_id}"
    if k not in _processing_locks:
        _processing_locks[k] = asyncio.Lock()
    return _processing_locks[k]


# ============================================================
# 会话索引
# ============================================================
def _index_file(agent_id, user_id) -> Path:
    return user_index_file(user_id)


def _load_index(agent_id, user_id) -> dict:
    f = _index_file(agent_id, user_id)
    if not f.exists():
        return {"conversations": {}}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {"conversations": {}}


def _save_index(agent_id, user_id, data):
    _index_file(agent_id, user_id).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _update_index_entry(agent_id, user_id, session_id, flow):
    idx = _load_index(agent_id, user_id)
    convs = idx.setdefault("conversations", {})
    meta = convs.get(session_id, {})
    meta["agent_id"] = agent_id
    if not meta.get("title"):
        for e in flow:
            if e.get("from") == "user" and e.get("content"):
                meta["title"] = str(e["content"])[:20]
                break
    if not meta.get("description"):
        for e in flow:
            if e.get("from") == "user" and e.get("content"):
                meta["description"] = " ".join(str(e["content"]).split())[:120]
                break
    if not meta.get("created_at") and flow:
        meta["created_at"] = flow[0].get("timestamp", "")
    meta["updated_at"] = ""
    for e in reversed(flow):
        if e.get("timestamp"):
            meta["updated_at"] = e["timestamp"]
            break
    meta["message_count"] = len([e for e in flow if e.get("from") == "user"])
    meta["entry_count"] = len(flow)
    for e in reversed(flow):
        if e.get("from") == "ai" and e.get("call") == "reply" and e.get("content"):
            meta["last_preview"] = str(e["content"])[:80]
            break
    meta.setdefault("archived", False)
    convs[session_id] = meta
    _save_index(agent_id, user_id, idx)
    return meta


# ============================================================
# Engine
# ============================================================
class Engine:
    """引擎实例。挂在 agent 上：agent.engine = Engine(agent)。"""

    def __init__(self, agent):
        self.agent = agent
        agent.engine = self
        self.paths = paths
        self.backpack = backpack
        self.pool = AgentPool()
        self.flows: Dict[str, Flow] = {}
        self.turn_states: Dict[str, dict] = {}
        self.interrupt_signals: Dict[str, bool] = {}
        self.empty_retries: Dict[str, int] = {}
        self.narration_retries: Dict[str, int] = {}
        self.active_sessions: set = set()
        self._logger = getattr(agent, "log", None)
        # 预编译：每个内置 md → execute 闭包
        for agent_id, entry in self.pool._system_agents.items():
            entry["execute"] = self.build(entry["config"])

    # ---- 日志 ----
    def _log(self, msg):
        if self._logger:
            try:
                self._logger.info(f"[engine] {msg}")
            except Exception:
                print(f"[engine] {msg}")
        else:
            print(f"[engine] {msg}")

    def _warn(self, msg):
        if self._logger:
            try:
                self._logger.warning(f"[engine][WARN] {msg}")
            except Exception:
                print(f"[engine][WARN] {msg}")
        else:
            print(f"[engine][WARN] {msg}")

    # ---- Flow 缓存 ----
    def get_flow(self, agent_id, user_id, session_id) -> Flow:
        k = f"{agent_id}:{user_id}:{session_id}"
        if k in self.flows:
            return self.flows[k]
        if len(self.flows) >= SESSION_FLOWS_MAX:
            for old_k in list(self.flows.keys()):
                parts = old_k.split(":")
                old_agent = parts[0] if len(parts) >= 3 else ""
                old_sid = parts[-1]
                if _key(old_agent, old_sid) not in self.active_sessions:
                    self.flows.pop(old_k, None)
                    break
        self.flows[k] = Flow(user_id, session_id, agent_id)
        return self.flows[k]

    # ---- turn state ----
    def get_turn_state(self, agent_id, session_id) -> dict:
        st = self.turn_states.get(_key(agent_id, session_id))
        if not st:
            return {"running": False, "status": "idle"}
        return dict(st)

    def mark_turn_start(self, agent_id, session_id):
        import time as _t
        self.turn_states[_key(agent_id, session_id)] = {
            "running": True, "status": "running", "started_at": _t.time(),
        }

    def mark_turn_end(self, agent_id, session_id, reason="done"):
        import time as _t
        k = _key(agent_id, session_id)
        info = self.turn_states.get(k)
        if info is not None:
            info["status"] = "interrupted" if reason == "interrupted" else "idle"
            info["running"] = False
            info["ended_at"] = _t.time()
        self.interrupt_signals.pop(k, None)

    def signal_interrupt(self, agent_id, session_id) -> dict:
        k = _key(agent_id, session_id)
        info = self.turn_states.get(k)
        if not info or not info.get("running"):
            return {"ok": False, "reason": "no_active_turn"}
        if self.interrupt_signals.get(k, False):
            return {"ok": True, "reason": "already_interrupting"}
        self.interrupt_signals[k] = True
        return {"ok": True, "reason": "signaled"}

    def clear_interrupt(self, agent_id, session_id):
        self.interrupt_signals.pop(_key(agent_id, session_id), None)

    # ============================================================
    # build(cfg) —— 编译 agent 配置为 execute 闭包
    # ============================================================
    def build(self, cfg: dict):
        route = cfg["route"]
        agent_id = cfg["agent_id"] or route.split("/")[-1]
        max_depth = cfg.get("max_depth", MAX_RECURSION_DEPTH)
        pressure_threshold = cfg.get("pressure_threshold", CONTEXT_PRESSURE_THRESHOLD)
        narration_check = cfg.get("narration_check", True)
        tool_names = cfg["tools"] + cfg.get("internal_tools", [])
        tool_schemas = [SCHEMAS[n]() for n in tool_names if n in SCHEMAS]
        tail_builder = build_tail(cfg.get("tail", ["depth_hint", "datetime"]))
        system_actions = set(cfg.get("system_actions", ["whoami", "get_session", "interrupt"]))
        sp_cfg = cfg["system_prompt"]

        def _load_prompt(session_id):
            if "file" in sp_cfg:
                try:
                    text = Path(sp_cfg["file"]).read_text(encoding="utf-8")
                    return text.replace("{{session_id}}", session_id).replace(
                        "__SESSION_ID__", session_id)
                except Exception as e:
                    self._warn(f"读 system_prompt 失败: {e}")
                    return f"你是 BIO-1。Session: {session_id}"
            return sp_cfg.get("inline", "").replace("{{session_id}}", session_id)

        def _new_ctx(user_id, session_id, flow, depth=0):
            return SimpleNamespace(
                agent=self.agent, route=route, agent_id=agent_id,
                user_id=user_id, session_id=session_id, flow=flow,
                depth=depth, think="", chain_no=None,
                max_depth=max_depth, pressure_threshold=pressure_threshold,
                narration_check=narration_check,
                tool_schemas=tool_schemas,
                tail_builder=tail_builder,
                system_prompt=_load_prompt(session_id),
                push=Push(self.agent, route, agent_id),
                interrupt_signals=self.interrupt_signals,
                empty_retries=self.empty_retries,
                narration_retries=self.narration_retries,
            )

        async def _run_turn_inner(ctx):
            k = _key(agent_id, ctx.session_id)
            interrupted = False
            self.mark_turn_start(agent_id, ctx.session_id)
            self.active_sessions.add(k)
            try:
                async with _get_processing_lock(agent_id, ctx.session_id):
                    result = await process(ctx, 0)
            finally:
                self.active_sessions.discard(k)
                interrupted = bool(self.turn_states.get(k, {}).get("status") == "interrupted"
                                    or self.interrupt_signals.get(k))
                self.mark_turn_end(agent_id, ctx.session_id)
                self.clear_interrupt(agent_id, ctx.session_id)
                if interrupted:
                    try:
                        Push(self.agent, f"builtins/agents/{agent_id}", agent_id).progress(
                            ctx.user_id, ctx.session_id, "done", "⏸️ 已中断",
                            extra={"status": "interrupted"},
                        )
                    except Exception as e:
                        self._warn(f"[interrupt] 推 interrupted 失败: {e}")
                try:
                    _update_index_entry(agent_id, ctx.user_id, ctx.session_id, ctx.flow._flow)
                except Exception as e:
                    self._warn(f"更新 index 失败: {e}")
            return result

        async def _run_turn(ctx):
            try:
                await _run_turn_inner(ctx)
            except ConnectionError as e:
                self._warn(f"[network] {str(e)[:150]}")
                try:
                    await ctx.flow.mark_interrupted("network", str(e))
                    ctx.push.chat(ctx.user_id, ctx.session_id,
                                  f"⚠️ 网络中断，现场已保留。错误：{str(e)[:200]}")
                except Exception:
                    pass
            except ContentSafetyBlockedError as e:
                self._warn(f"[safety] {str(e)[:150]}")
                try:
                    await ctx.flow.mark_interrupted("llm_error", str(e))
                    ctx.push.chat(ctx.user_id, ctx.session_id,
                                  f"⚠️ LLM 调用失败，现场已保留。错误：{str(e)[:200]}")
                except Exception:
                    pass
            except asyncio.CancelledError:
                import time, traceback
                self._warn(f"[cancelled] {ctx.session_id} "
                           f"elapsed={time.time() - self.turn_states.get(_key(agent_id, ctx.session_id), {}).get('started_at', time.time()):.1f}s")
                self._warn("cancelled stack:\n" + "".join(traceback.format_stack()))
                raise
            except BaseException as e:
                self._log(f"异步处理异常: {type(e).__name__}: {e}")
                try:
                    ctx.push.chat(ctx.user_id, ctx.session_id,
                                  f"❌ 系统错误: {type(e).__name__}: {str(e)[:200]}")
                except Exception:
                    pass

        async def _handle_callback(envelop):
            user_id, session_id = _resolve_ids(envelop)
            session_id = envelop.meta.get("callback_session_id") or session_id
            user_id = envelop.meta.get("user_id") or user_id
            flow = self.get_flow(agent_id, user_id, session_id)
            trace_id = envelop.meta.get("trace_id", envelop.trace_id)
            payload = envelop.payload if isinstance(envelop.payload, dict) else {}
            try:
                await flow.append_async_callback(
                    trace_id=trace_id, payload=payload,
                    trace_meta={
                        "call_id": envelop.meta.get("_call_id", ""),
                        "task_id": payload.get("task_id", ""),
                    },
                )
            except Exception as e:
                self._warn(f"[callback] append_async_callback 失败: {e}")
            ctx = _new_ctx(user_id, session_id, flow, 0)
            ctx.push.progress(user_id, session_id, "callback",
                              f"📡 任务完成（trace={trace_id[:8] if trace_id else ''}）")
            try:
                result = await process(ctx, 0)
                envelop.payload = {"ok": True, "content": result.get("content", "")}
            except Exception as e:
                self._warn(f"回调处理异常: {e}")
                envelop.payload = {"ok": False, "error": str(e)}
            return envelop

        async def execute(envelop, agent):
            try:
                user_id, session_id = _resolve_ids(envelop)
                action = envelop.payload.get("action", "") if isinstance(envelop.payload, dict) else ""

                if action == "whoami":
                    envelop.payload = {"ok": True, "user_id": user_id,
                                       "session_id": session_id, "agent_id": agent_id}
                    return envelop
                if action == "get_session":
                    flow = self.get_flow(agent_id, user_id, session_id)
                    envelop.payload = {
                        "ok": True, "conversation_id": session_id, "agent_id": agent_id,
                        "flow": _json_safe(flow._flow), "entry_count": len(flow._flow),
                    }
                    return envelop
                if action == "interrupt":
                    r = self.signal_interrupt(agent_id, session_id)
                    envelop.payload = {"ok": r.get("ok", False),
                                       "waiting": r.get("ok", False),
                                       "call": "interrupt", "content": "", "args": {},
                                       "reason": r.get("reason", "")}
                    return envelop

                if envelop.meta.get("is_callback"):
                    return await _handle_callback(envelop)

                user_input = (envelop.payload.get("content", "") if isinstance(envelop.payload, dict) else "").strip()
                if not user_input:
                    envelop.payload = {"ok": False, "error": "请输入内容"}
                    return envelop

                self._log(f"用户输入 [{agent_id}/{session_id}]: {user_input[:60]}")
                flow = self.get_flow(agent_id, user_id, session_id)
                ctx = _new_ctx(user_id, session_id, flow, 0)
                ctx.push.progress(user_id, session_id, "thinking", "🧠 正在思考...")
                flow._current_baseline = None
                flow._task_summary = ""
                flow._picked_skills = []

                if hasattr(self.agent.llm, "reset_round"):
                    self.agent.llm.reset_round(session_id)
                self.empty_retries[_key(agent_id, session_id) + ":empty"] = 0

                await flow.append_user(user_input)
                try:
                    _update_index_entry(agent_id, user_id, session_id, flow._flow)
                except Exception as e:
                    self._warn(f"更新 index 失败: {e}")
                ctx.push.reset_dead(session_id)

                # ★ 分身调用：同步执行，返回最终 reply（不异步提交）
                if isinstance(session_id, str) and session_id.startswith("sub_"):
                    _p = envelop.payload if isinstance(envelop.payload, dict) else {}
                    _parent = (
                        _p.get("parent_session", "")
                        or envelop.meta.get("_parent_session", "")
                        or envelop.meta.get("callback_session_id", "")
                        or ""
                    )
                    try:
                        result = await _run_turn_inner(ctx)
                    except ConnectionError as e:
                        self._warn(f"[sub-agent][network] {str(e)[:150]}")
                        try:
                            await ctx.flow.mark_interrupted("network", str(e))
                        except Exception:
                            pass
                        result = {
                            "ok": False, "call": "reply",
                            "content": f"⚠️ 网络中断，现场已保留。错误：{str(e)[:200]}",
                            "args": {},
                        }
                    except ContentSafetyBlockedError as e:
                        self._warn(f"[sub-agent][safety] {str(e)[:150]}")
                        try:
                            await ctx.flow.mark_interrupted("llm_error", str(e))
                        except Exception:
                            pass
                        result = {
                            "ok": False, "call": "reply",
                            "content": f"⚠️ LLM 调用失败，现场已保留。错误：{str(e)[:200]}",
                            "args": {},
                        }
                    except Exception as e:
                        self._warn(f"[sub-agent][error] {e}")
                        try:
                            await ctx.flow.mark_interrupted("llm_error", str(e))
                        except Exception:
                            pass
                        result = {
                            "ok": False, "call": "reply",
                            "content": f"❌ 分身执行异常：{str(e)[:200]}",
                            "args": {},
                        }
                    envelop.payload = {
                        "ok": (result or {}).get("ok", True),
                        "call": (result or {}).get("call", "reply"),
                        "content": (result or {}).get("content", ""),
                        "args": {},
                        "task_id": _p.get("task_id", "") or envelop.meta.get("_task_id", ""),
                        "parent_session": _parent,
                        "agent_id": agent_id,
                    }
                    return envelop

                # 前端调用：异步提交
                asyncio.create_task(_run_turn(ctx))
                envelop.payload = {"ok": True, "waiting": False, "call": "processing",
                                   "content": "", "status": "processing", "args": {},
                                   "agent_id": agent_id}
                return envelop
            except Exception as e:
                self._warn(f"execute 异常: {e}")
                envelop.payload = {"ok": False, "error": str(e)[:200],
                                   "call": "reply", "agent_id": agent_id}
                return envelop

        execute.cfg = cfg
        execute.agent_id = agent_id
        execute.flow_cls = Flow
        return execute

    # ============================================================
    # execute —— 17 个 action + 分发
    # ============================================================
    async def execute(self, envelop):
        payload = envelop.payload if isinstance(envelop.payload, dict) else {}
        action = payload.get("action", "")
        user_id, _ = _resolve_ids(envelop)
        agent_id = (payload.get("agent_id") or envelop.meta.get("agent_id")
                    or self.pool.default_id(user_id))

        handler = self._HANDLERS.get(action)
        if handler:
            return await handler(self, envelop, agent_id)

        if envelop.meta.get("is_callback"):
            cb_agent = envelop.meta.get("agent_id") or agent_id
            return await self._dispatch(envelop, cb_agent)

        return await self._dispatch(envelop, agent_id)

    async def _dispatch(self, envelop, agent_id):
        user_id, _ = _resolve_ids(envelop)
        entry = self.pool.get(agent_id, user_id)
        if not entry:
            envelop.payload = {"ok": False, "error": f"unknown agent: {agent_id}",
                               "action": (envelop.payload or {}).get("action", "")}
            return envelop
        if not entry.get("execute"):
            entry["execute"] = self.build(entry["config"])
        return await entry["execute"](envelop, self.agent)

    # ============================================================
    # 17 个 action
    # ============================================================
    async def _act_whoami(self, envelop, agent_id):
        user_id, session_id = _resolve_ids(envelop)
        envelop.payload = {"ok": True, "user_id": user_id,
                           "session_id": session_id, "agent_id": agent_id}
        return envelop

    async def _act_list_conversations(self, envelop, agent_id):
        user_id, _ = _resolve_ids(envelop)
        include_archived = envelop.payload.get("include_archived", False)
        idx = _load_index(agent_id, user_id)
        items = []
        for cid, meta in (idx.get("conversations") or {}).items():
            if meta.get("agent_id") != agent_id:
                continue
            if meta.get("archived") and not include_archived:
                continue
            items.append({
                "id": cid,
                "agent_id": agent_id,
                "title": meta.get("title", ""),
                "created_at": meta.get("created_at", ""),
                "updated_at": meta.get("updated_at", ""),
                "message_count": meta.get("message_count", 0),
                "last_preview": meta.get("last_preview", ""),
                "description": meta.get("description", ""),
                "archived": meta.get("archived", False),
                "parent_session": meta.get("parent_session", ""),
            })
        items.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        envelop.payload = {"ok": True, "conversations": items, "total": len(items)}
        return envelop

    async def _act_new_conversation(self, envelop, agent_id):
        user_id, _ = _resolve_ids(envelop)
        new_sid = envelop.payload.get("session_id") or f"s_{uuid.uuid4().hex[:12]}"
        idx = _load_index(agent_id, user_id)
        convs = idx.setdefault("conversations", {})
        if new_sid not in convs:
            convs[new_sid] = {
                "agent_id": agent_id,
                "title": "", "created_at": "", "updated_at": "",
                "message_count": 0, "entry_count": 0, "archived": False,
            }
        _save_index(agent_id, user_id, idx)
        envelop.payload = {"ok": True, "conversation_id": new_sid, "agent_id": agent_id}
        return envelop

    async def _act_get_session(self, envelop, agent_id):
        user_id, _ = _resolve_ids(envelop)
        sid = envelop.payload.get("session_id", "")
        if not sid:
            envelop.payload = {"ok": False, "error": "session_id required"}
            return envelop
        idx_meta = (_load_index(agent_id, user_id).get("conversations") or {}).get(sid, {}) or {}
        real_agent_id = idx_meta.get("agent_id") or agent_id
        flow = Flow(user_id, sid, real_agent_id)
        all_entries = flow._flow
        rounds, cur = [], []
        for e in all_entries:
            if e.get("from") == "user" and cur:
                rounds.append(cur); cur = []
            cur.append(e)
        if cur:
            rounds.append(cur)
        before_ts = envelop.payload.get("before_ts", "")
        if before_ts:
            rounds = [r for r in rounds if r and r[0].get("timestamp", "") < before_ts]
        N = int(envelop.payload.get("limit", 10))
        recent = rounds[-N:] if len(rounds) > N else rounds
        flat = [e for r in recent for e in r]
        older = [r for r in rounds if not recent or (r and r[0].get("timestamp", "") < recent[0][0].get("timestamp", ""))]
        meta = (_load_index(agent_id, user_id).get("conversations") or {}).get(sid, {})
        envelop.payload = {
            "ok": True, "conversation_id": sid, "agent_id": agent_id,
            "flow": _json_safe(flat),
            "entry_count": len(all_entries),
            "round_count": len(rounds) if not before_ts else None,
            "returned_rounds": len(recent),
            "has_more": len(older) > 0,
            "oldest_ts": recent[0][0].get("timestamp") if recent else "",
            "title": meta.get("title", ""),
        }
        return envelop

    async def _act_delete_conversation(self, envelop, agent_id):
        user_id, _ = _resolve_ids(envelop)
        sid = envelop.payload.get("conversation_id") or envelop.payload.get("session_id")
        if not sid:
            envelop.payload = {"ok": False, "error": "conversation_id required"}
            return envelop
        shutil.rmtree(session_dir(user_id, agent_id, sid), ignore_errors=True)
        self.flows.pop(f"{agent_id}:{user_id}:{sid}", None)
        idx = _load_index(agent_id, user_id)
        if sid in (idx.get("conversations") or {}):
            del idx["conversations"][sid]
            _save_index(agent_id, user_id, idx)
        envelop.payload = {"ok": True, "deleted": sid}
        return envelop

    async def _act_rename_conversation(self, envelop, agent_id):
        user_id, _ = _resolve_ids(envelop)
        sid = envelop.payload.get("conversation_id") or envelop.payload.get("session_id")
        title = (envelop.payload.get("title") or "").strip()
        if not sid or not title:
            envelop.payload = {"ok": False, "error": "conversation_id and title required"}
            return envelop
        idx = _load_index(agent_id, user_id)
        convs = idx.setdefault("conversations", {})
        if sid not in convs:
            envelop.payload = {"ok": False, "error": "conversation not found"}
            return envelop
        convs[sid]["title"] = title[:50]
        _save_index(agent_id, user_id, idx)
        envelop.payload = {"ok": True, "conversation_id": sid, "title": title}
        return envelop

    async def _act_get_call_detail(self, envelop, agent_id):
        user_id, _ = _resolve_ids(envelop)
        sid = envelop.payload.get("session_id", "")
        call_id = envelop.payload.get("call_id", "")
        if not sid or not call_id:
            envelop.payload = {"ok": False, "error": "session_id and call_id required"}
            return envelop
        flow = Flow(user_id, sid, agent_id)
        matched, ai_entry = [], None
        for i, e in enumerate(flow._flow):
            if e.get("from") == "system" and (e.get("detail") or {}).get("call_id") == call_id:
                matched.append(e)
                if ai_entry is None:
                    for j in range(i - 1, -1, -1):
                        if flow._flow[j].get("from") == "ai":
                            ai_entry = flow._flow[j]
                            break
        if not matched:
            envelop.payload = {"ok": False, "error": "call_id not found"}
            return envelop
        envelop.payload = {"ok": True, "entries": _json_safe(matched),
                           "ai_entry": _json_safe(ai_entry), "call_id": call_id}
        return envelop

    async def _act_get_turn_state(self, envelop, agent_id):
        user_id, sid = _resolve_ids(envelop)
        sid = envelop.payload.get("session_id") or sid
        envelop.payload = {"ok": True, "session_id": sid,
                           "turn_state": self.get_turn_state(agent_id, sid),
                           "agent_id": agent_id}
        return envelop

    async def _act_interrupt(self, envelop, agent_id):
        user_id, sid = _resolve_ids(envelop)
        sid = envelop.payload.get("session_id") or sid
        r = self.signal_interrupt(agent_id, sid)
        # 首次打断推 interrupting（already_interrupting 幂等，不重复推）
        if r.get("ok") and r.get("reason") != "already_interrupting":
            try:
                Push(self.agent, f"builtins/agents/{agent_id}", agent_id).progress(
                    user_id, sid, "executing", "⏸️ 中断中...",
                    extra={"status": "interrupting"},
                )
            except Exception as e:
                self._warn(f"[interrupt] 推 interrupting 失败: {e}")
        envelop.payload = {"ok": r.get("ok", False),
                        "waiting": r.get("ok", False),
                        "call": "interrupt", "content": "", "args": {},
                        "reason": r.get("reason", "")}
        return envelop

    async def _act_get_state(self, envelop, agent_id):
        user_id, sid = _resolve_ids(envelop)
        sid = envelop.payload.get("session_id") or sid
        state_type = envelop.payload.get("type", "exp")
        if state_type == "exp":
            data = load_backpack(agent_id, user_id)
            items = data.get("items", [])
            lines = [f"- [{it.get('id','')}] {it.get('title','')}" for it in items]
            content = "\n".join(lines)
            envelop.payload = {"ok": True, "data": {
                "type": "exp", "items": items, "content": content,
                "size": len(content), "count": len(items),
            }}
        else:
            f = session_taskboard_file(user_id, agent_id, sid)
            content = f.read_text(encoding="utf-8") if f.exists() else ""
            envelop.payload = {"ok": True, "data": {
                "type": state_type, "content": content, "size": len(content),
            }}
        return envelop

    async def _act_save_state(self, envelop, agent_id):
        user_id, sid = _resolve_ids(envelop)
        sid = envelop.payload.get("session_id") or sid
        state_type = envelop.payload.get("type", "task")
        if state_type == "exp":
            envelop.payload = {"ok": False, "error": "经验背包不支持直接编辑"}
            return envelop
        content = envelop.payload.get("content", "")
        base = session_dir(user_id, agent_id, sid)
        base.mkdir(parents=True, exist_ok=True)
        session_taskboard_file(user_id, agent_id, sid).write_text(content, encoding="utf-8")
        envelop.payload = {"ok": True, "data": {
            "type": state_type, "saved": True, "size": len(content),
        }}
        return envelop

    async def _act_list_agents(self, envelop, agent_id):
        user_id, _ = _resolve_ids(envelop)
        envelop.payload = {"ok": True, "agents": self.pool.list(user_id),
                           "default": self.pool.default_id(user_id)}
        return envelop

    async def _act_reload_agents(self, envelop, agent_id):
        user_id, _ = _resolve_ids(envelop)
        self.pool.reload_all(user_id)
        for aid, entry in self.pool._system_agents.items():
            entry["execute"] = self.build(entry["config"])
        for aid, entry in self.pool._user_agents.get(user_id, {}).items():
            entry["execute"] = self.build(entry["config"])
        envelop.payload = {"ok": True, "count": len(self.pool.list(user_id)),
                           "agents": self.pool.list(user_id)}
        return envelop

    async def _act_list_debug_logs(self, envelop, agent_id):
        """列 _debug_context/ 下的 depth_*.json。"""
        import re as _re
        from datetime import datetime as _dt
        user_id, sid = _resolve_ids(envelop)
        sid = envelop.payload.get("session_id") or sid
        dbg_dir = session_debug_dir(user_id, agent_id, sid)
        if not dbg_dir.is_dir():
            envelop.payload = {"ok": True, "session_id": sid, "agent_id": agent_id,
                               "files": [], "total": 0, "dir": ""}
            return envelop
        entries = []
        for p in dbg_dir.glob("depth_*.json"):
            if not p.is_file():
                continue
            m = _re.match(r"depth_(\d+)", p.name)
            if not m:
                continue
            try:
                mtime = p.stat().st_mtime
                size = p.stat().st_size
            except Exception:
                mtime = 0
                size = 0
            entries.append({"path": p, "depth": int(m.group(1)), "mtime": mtime, "size": size})
        entries.sort(key=lambda x: x["depth"], reverse=True)
        idx = _load_index(agent_id, user_id)
        meta = (idx.get("conversations") or {}).get(sid, {}) or {}
        envelop.payload = {
            "ok": True, "session_id": sid, "agent_id": agent_id,
            "title": meta.get("title", ""),
            "updated_at": _dt.fromtimestamp(entries[0]["mtime"]).isoformat() if entries else "",
            "dir": str(dbg_dir).replace("\\", "/"),
            "files": [
                {
                    "name": x["path"].name,
                    "depth": x["depth"],
                    "ts": _dt.fromtimestamp(x["mtime"]).isoformat() if x["mtime"] else "",
                    "size": x["size"],
                    "path": str(x["path"]).replace("\\", "/"),
                }
                for x in entries
            ],
            "total": len(entries),
        }
        return envelop

    async def _act_read_debug_log(self, envelop, agent_id):
        """读单个 debug 文件。路径必须落在本 agent 的 sessions/<sid>/_debug_context/ 内。"""
        user_id, sid = _resolve_ids(envelop)
        sid = envelop.payload.get("session_id") or sid
        rel_path = envelop.payload.get("path", "")
        if not rel_path:
            envelop.payload = {"ok": False, "error": "path required"}
            return envelop
        target = Path(rel_path).resolve()
        allowed_root = session_dir(user_id, agent_id, sid).resolve()
        try:
            target.relative_to(allowed_root)
        except ValueError:
            envelop.payload = {"ok": False, "error": "forbidden path"}
            return envelop
        if not target.is_file() or target.suffix != ".json" or target.parent.name != "_debug_context":
            envelop.payload = {"ok": False, "error": "not a debug file"}
            return envelop
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            envelop.payload = {"ok": False, "error": str(e)}
            return envelop
        envelop.payload = {
            "ok": True, "name": target.name,
            "content": text, "size": target.stat().st_size,
        }
        return envelop

    async def _act_get_day_history(self, envelop, agent_id):
        envelop.payload = {"ok": False, "error": "get_day_history 已废弃，改用 list_conversations"}
        return envelop

    async def _act_clear_memory(self, envelop, agent_id):
        """清空 flow + index 条目。"""
        user_id, sid = _resolve_ids(envelop)
        sid = envelop.payload.get("session_id") or sid
        flow = self.get_flow(agent_id, user_id, sid)
        flow._flow = []
        flow._current_baseline = None
        flow._task_summary = ""
        flow._picked_skills = []
        await flow.save_locked()
        self.flows.pop(f"{agent_id}:{user_id}:{sid}", None)
        idx = _load_index(agent_id, user_id)
        if sid in (idx.get("conversations") or {}):
            meta = idx["conversations"][sid]
            meta["message_count"] = 0
            meta["entry_count"] = 0
            meta["last_preview"] = ""
            _save_index(agent_id, user_id, idx)
        envelop.payload = {"ok": True}
        return envelop

    # ---- 主提示词（.md 源文件）读写 ----

    def _agent_md_path(self, agent_id, user_id):
        """定位 agent 的 .md 源文件：用户级优先，系统级兜底。返回 (path, scope)。"""
        entry = self.pool.get(agent_id, user_id)
        if not entry:
            return None, ""
        if entry["meta"].get("source") == "user":
            p = paths.user_agents_dir(user_id) / f"{agent_id}.md"
            if p.is_file():
                return p, "user"
        p = paths.system_agent_md_file(agent_id)
        if p.is_file():
            return p, "system"
        return None, ""

    async def _act_get_agent_prompt(self, envelop, agent_id):
        """读 agent 主提示词原文（.md 全文，含 frontmatter）。任何用户可读。"""
        user_id, _ = _resolve_ids(envelop)
        target_id = str((envelop.payload or {}).get("target_agent_id") or agent_id)
        path, scope = self._agent_md_path(target_id, user_id)
        if not path:
            envelop.payload = {"ok": False, "error": f"agent prompt not found: {target_id}"}
            return envelop
        try:
            content = path.read_text(encoding="utf-8")
        except Exception as e:
            envelop.payload = {"ok": False, "error": str(e)}
            return envelop
        entry = self.pool.get(target_id, user_id) or {}
        envelop.payload = {
            "ok": True,
            "agent_id": target_id,
            "name": entry.get("meta", {}).get("name", target_id),
            "scope": scope,
            "path": str(path).replace("\\", "/"),
            "content": content,
            "chars": len(content),
            "can_edit": user_id == "local",
        }
        return envelop

    async def _act_save_agent_prompt(self, envelop, agent_id):
        """保存 agent 主提示词（整体覆盖 .md），随后重载 agent。仅 local 用户可写。"""
        user_id, _ = _resolve_ids(envelop)
        payload = envelop.payload or {}
        target_id = str(payload.get("target_agent_id") or agent_id)
        content = payload.get("content")
        if not isinstance(content, str):
            envelop.payload = {"ok": False, "error": "content must be a string"}
            return envelop
        if len(content) > 512 * 1024:
            envelop.payload = {"ok": False, "error": "content too large (max 512KB)"}
            return envelop
        if user_id != "local":
            envelop.payload = {"ok": False,
                               "error": "只读模式：仅 local 用户可修改 agent 主提示词"}
            return envelop
        path, scope = self._agent_md_path(target_id, user_id)
        if not path:
            envelop.payload = {"ok": False, "error": f"agent prompt not found: {target_id}"}
            return envelop
        # frontmatter 必须是合法 YAML 映射，否则拒绝保存（避免写坏 agent 定义）
        if content.startswith("---"):
            end = content.find("\n---", 3)
            if end != -1:
                fm_raw = content[3:end].strip()
                try:
                    import yaml
                    parsed = yaml.safe_load(fm_raw)
                except Exception as e:
                    envelop.payload = {"ok": False, "error": f"frontmatter YAML 无效: {e}"}
                    return envelop
                if parsed is not None and not isinstance(parsed, dict):
                    envelop.payload = {"ok": False, "error": "frontmatter 必须是 key: value 映射"}
                    return envelop
                if isinstance(parsed, dict) and parsed.get("id") and parsed["id"] != target_id:
                    envelop.payload = {"ok": False,
                                       "error": f"frontmatter id ({parsed['id']}) 与 agent_id ({target_id}) 不一致，此处不支持改名"}
                    return envelop
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(content, encoding="utf-8")
            os.replace(str(tmp), str(path))
        except Exception as e:
            envelop.payload = {"ok": False, "error": str(e)}
            return envelop
        # 重载：保存后下一个回合立刻用新提示词
        self.pool.reload_all(user_id)
        for _aid, entry in self.pool._system_agents.items():
            entry["execute"] = self.build(entry["config"])
        for _aid, entry in self.pool._user_agents.get(user_id, {}).items():
            entry["execute"] = self.build(entry["config"])
        envelop.payload = {"ok": True, "agent_id": target_id,
                           "scope": scope,
                           "path": str(path).replace("\\", "/"),
                           "chars": len(content)}
        return envelop

    _HANDLERS = {
        "whoami": _act_whoami,
        "list_conversations": _act_list_conversations,
        "new_conversation": _act_new_conversation,
        "get_session": _act_get_session,
        "delete_conversation": _act_delete_conversation,
        "rename_conversation": _act_rename_conversation,
        "get_call_detail": _act_get_call_detail,
        "get_turn_state": _act_get_turn_state,
        "interrupt": _act_interrupt,
        "get_state": _act_get_state,
        "save_state": _act_save_state,
        "list_agents": _act_list_agents,
        "reload_agents": _act_reload_agents,
        "get_agent_prompt": _act_get_agent_prompt,
        "save_agent_prompt": _act_save_agent_prompt,
        "list_debug_logs": _act_list_debug_logs,
        "read_debug_log": _act_read_debug_log,
        "get_day_history": _act_get_day_history,
        "clear_memory": _act_clear_memory,
    }
