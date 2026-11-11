"""信息流 —— 编号化存储 + 三档渲染。

存储：data/memories/<agent_id>/users/<uid>/sessions/<sid>/flow.jsonl
编号：##N.M / ###N.M.K，与旧 main_agent 完全一致。
"""
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from .constants import (
    BASE_ROUNDS, CONTEXT_PRESSURE_THRESHOLD,
    FLOW_ENTRY_MAX_BYTES, FLOW_LOAD_TAIL_BYTES,
    SIDE_EFFECT_TARGETS,
)
from .paths import session_flow_file
from runtime._llm import is_llm_error_string


class Flow:
    """编号化信息流。"""

    _DISCOVERY_TARGETS = ("cogitor", "os/_registry", "contract_agent")
    _DISCOVERY_ACTIONS = ("list", "list_plugins", "get_map", "search",
                          "list_dir", "list_servers", "list_tools")

    def __init__(self, user_id: str, session_id: str, agent_id: str):
        self.user_id = user_id
        self.session_id = session_id
        self.agent_id = agent_id
        self.flow_file = session_flow_file(user_id, agent_id, session_id).resolve()
        self._flow: List[dict] = []
        self._round_counter = 0
        self._chain_counter = 0
        self._step_counter = 0
        self._current_turn = 0
        self._ignored: Dict[str, int] = {}
        self._write_lock = asyncio.Lock()
        self._current_baseline = None
        self._task_summary = ""
        self._picked_skills: List[dict] = []
        self._round_snapshots: Dict[int, dict] = {}
        self._load()

    # ---- 编号 ----
    def _next_round(self):
        self._round_counter += 1
        self._chain_counter = 0
        self._step_counter = 0
        return self._round_counter

    def _next_chain(self):
        self._chain_counter += 1
        self._step_counter = 0
        return self._chain_counter

    def _next_step(self):
        self._step_counter += 1
        return self._step_counter

    def _restore_counters(self):
        for e in reversed(self._flow):
            if e.get("round_no"):
                self._round_counter = max(self._round_counter, e["round_no"])
                self._chain_counter = max(self._chain_counter, e.get("chain_no", 0) or 0)
                self._step_counter = max(self._step_counter, e.get("step_no", 0) or 0)
                return

    # ---- 加载 ----
    def _load(self):
        if not self.flow_file.exists():
            return
        try:
            size = self.flow_file.stat().st_size
            if size > FLOW_LOAD_TAIL_BYTES:
                with open(self.flow_file, "rb") as f:
                    f.seek(-FLOW_LOAD_TAIL_BYTES, os.SEEK_END)
                    raw = f.read()
                text = raw.decode("utf-8", errors="ignore")
                lines = text.split("\n")
                lines = lines[1:] if lines else []
            else:
                with open(self.flow_file, "r", encoding="utf-8") as f:
                    lines = f.read().split("\n")
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._flow.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
            first_user = next(
                (i for i, e in enumerate(self._flow) if e.get("from") == "user"), None
            )
            if first_user is not None and first_user > 0:
                self._flow = self._flow[first_user:]
            self._restore_counters()
            for e in self._flow:
                if e.get("action") == "round_snapshot":
                    r = e.get("round_no")
                    if r:
                        d = e.get("detail") or {}
                        self._round_snapshots[r] = {
                            "collapsed": d.get("collapsed", []) or [],
                            "ignored": d.get("ignored", []) or [],
                        }
        except Exception:
            self._flow = []

    # ---- 落盘 ----
    async def _append_line_locked(self, entry: dict):
        self.flow_file.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False, default=str)
        try:
            with open(self.flow_file, "a", encoding="utf-8", newline="") as f:
                f.write(line + "\n")
                f.flush()
        except Exception as e:
            print(f"[flow][WARN] 追加写入失败: {e}")

    async def save_locked(self):
        """全量重写（仅用于 clear_memory 这种内存被整体修改的场景）。"""
        async with self._write_lock:
            self.flow_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.flow_file, "w", encoding="utf-8", newline="") as f:
                for entry in self._flow:
                    f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())

    # ---- 写入 ----
    async def append_user(self, content: str):
        r = self._next_round()
        entry = {
            "timestamp": datetime.now().isoformat(),
            "from": "user",
            "content": content,
            "round_no": r,
            "chain_no": 0,
            "step_no": 0,
        }
        self._flow.append(entry)
        async with self._write_lock:
            await self._append_line_locked(entry)

    async def append_ai(self, think, call, content="", args=None, retain=0):
        if is_llm_error_string(content):
            return
        c = self._next_chain() if call == "use_tool" else 0
        entry = {
            "timestamp": datetime.now().isoformat(),
            "from": "ai",
            "think": think,
            "call": call,
            "content": content,
            "args": args or {},
            "retain": retain,
            "round_no": self._round_counter,
            "chain_no": c,
            "step_no": 0,
        }
        self._flow.append(entry)
        async with self._write_lock:
            await self._append_line_locked(entry)

    async def append_system(self, action, result_think, detail=None,
                            extra=None, retain=1, chain_no=None):
        detail = detail or {}
        _summarize_big_fields(detail)
        _truncate_if_too_big(detail)
        entry = {
            "timestamp": datetime.now().isoformat(),
            "from": "system",
            "action": action,
            "result": result_think,
            "detail": detail,
            "retain": retain,
            "round_no": self._round_counter,
            "chain_no": chain_no if chain_no is not None else self._chain_counter,
            "step_no": self._next_step(),
        }
        if extra:
            entry["extra"] = extra
        self._flow.append(entry)
        async with self._write_lock:
            await self._append_line_locked(entry)

    # ---- 异步回调 ----
    def _find_chain_by_trace(self, trace_id: str) -> str:
        if not trace_id:
            return ""
        for e in self._flow:
            if e.get("action") != "call_end":
                continue
            d = e.get("detail") or {}
            res = d.get("result") or {}
            if not isinstance(res, dict):
                continue
            data = res.get("data") if isinstance(res.get("data"), dict) else {}
            tid = data.get("trace_id", "") if isinstance(data, dict) else ""
            if not tid:
                tid = d.get("trace_id", "") or ""
            if tid == trace_id:
                r = e.get("round_no", 0) or 0
                c = e.get("chain_no", 0) or 0
                if r and c:
                    return f"##{r}.{c}"
        return ""

    def _current_round_has_reply(self) -> bool:
        cur = self._round_counter
        for e in self._flow:
            if (e.get("round_no") or 0) != cur:
                continue
            if e.get("from") == "ai" and e.get("call") == "reply":
                return True
        return False

    async def append_async_callback(self, trace_id, payload, trace_meta=None):
        related = self._find_chain_by_trace(trace_id)
        if self._round_counter > 0 and self._current_round_has_reply():
            await self.append_user("（系统：异步分身回调到达）")
        c = self._next_chain()
        r = self._round_counter
        await self.append_system(
            action="call_start",
            result_think="📤 处理异步回调",
            detail={
                "target": "async_callback",
                "related": related,
                "trace_id": trace_id,
                "params": {"trace_id": trace_id, **(trace_meta or {})},
            },
            retain=3,
        )
        await self.append_system(
            action="call_end",
            result_think="📨 回调处理完成",
            detail={
                "target": "async_callback",
                "related": related,
                "trace_id": trace_id,
                "result": payload,
            },
            retain=3,
        )
        return f"##{r}.{c}"

    # ---- 折叠 ----
    async def manage_flow(self, ignore=None) -> dict:
        ignore = ignore or []
        self._current_turn += 1
        applied = {"ignore": [], "invalid": []}
        for idx in ignore:
            s = str(idx or "").lstrip("#").strip()
            if not s or not self._index_exists(s):
                applied["invalid"].append(idx)
                continue
            self._ignored[s] = self._current_turn + 999
            applied["ignore"].append(s)
        self._gc_ignored()
        ctx_after = self.estimate_context_chars()
        still_over = ctx_after >= CONTEXT_PRESSURE_THRESHOLD
        need_free = max(0, ctx_after - CONTEXT_PRESSURE_THRESHOLD)
        n = len(applied["ignore"])
        if still_over:
            think = (
                f"已忽略 {n} 条；仍超阈值 {ctx_after:,}/{CONTEXT_PRESSURE_THRESHOLD:,}，"
                f"还需释放约 {need_free:,} 字。继续 ignore 或按编号补。"
            )
        else:
            think = f"已忽略 {n} 条；上下文 {ctx_after:,} 已低于阈值，可以继续干活。"
        return {
            "ok": True,
            "think": think,
            "data": {
                **applied,
                "ctx_after": ctx_after,
                "threshold": CONTEXT_PRESSURE_THRESHOLD,
                "still_over": still_over,
                "need_free": need_free,
            },
        }

    def _index_exists(self, s):
        parts = s.split(".")
        try:
            r = int(parts[0])
            c = int(parts[1]) if len(parts) > 1 else None
            sd = int(parts[2]) if len(parts) > 2 else None
        except (ValueError, IndexError):
            return False
        for e in self._flow:
            if (e.get("round_no") or 0) != r:
                continue
            if c is not None and (e.get("chain_no") or 0) != c:
                continue
            if sd is not None and (e.get("step_no") or 0) != sd:
                continue
            return True
        return False

    def _gc_ignored(self):
        cur = self._current_turn
        self._ignored = {k: v for k, v in self._ignored.items() if v > cur}

    def _is_ignored(self, idx):
        s = str(idx or "").lstrip("#").strip()
        exp = self._ignored.get(s)
        return bool(exp) and exp > self._current_turn

    # ---- 渲染 ----
    def _split_rounds(self):
        rounds, cur = [], []
        for e in self._flow:
            if e.get("from") == "user" and cur:
                rounds.append(cur)
                cur = []
            cur.append(e)
        if cur:
            rounds.append(cur)
        return rounds

    def _group_chains(self, entries):
        out = {}
        for e in entries:
            c = e.get("chain_no") or 0
            if not c:
                continue
            out.setdefault(c, []).append(e)
        return out

    @staticmethod
    def _chain_status(chain_entries):
        for e in chain_entries:
            if e.get("action") == "call_error":
                return "failed"
        for e in chain_entries:
            if e.get("action") == "call_end":
                return "ok"
        return "pending"

    def _chain_target_action(self, chain_entries):
        for e in chain_entries:
            if e.get("action") == "call_start":
                d = e.get("detail") or {}
                t = d.get("target", "") or ""
                p = d.get("params") or {}
                a = d.get("action") or ""
                if not a and isinstance(p, dict):
                    a = p.get("action", "") or ""
                if not a:
                    a = "none"
                return t, a
            if e.get("from") == "ai" and e.get("call") == "use_tool":
                args = e.get("args") or {}
                return args.get("target", "") or "", args.get("action", "") or ""
        return "", ""

    def get_messages(self) -> List[dict]:
        if not self._flow:
            return []
        rounds = self._split_rounds()
        rounds = [[e for e in r if (e.get("round_no") or 0) > 0] for r in rounds]
        rounds = [r for r in rounds if r]
        if not rounds:
            return []
        last_round = rounds[-1]
        last_r_no = last_round[0].get("round_no") or 0
        baseline = getattr(self, "_current_baseline", None)
        if baseline is None or not baseline:
            selected = rounds[-BASE_ROUNDS:]
        else:
            baseline_set = set(baseline) | {last_r_no}
            selected = [r for r in rounds if (r[0].get("round_no") or 0) in baseline_set]
        if last_r_no > 1:
            prev_r_no = last_r_no - 1
            if not any((r[0].get("round_no") or 0) == prev_r_no for r in selected):
                prev = next((r for r in rounds if (r[0].get("round_no") or 0) == prev_r_no), None)
                if prev:
                    selected.append(prev)
        seen, deduped = set(), []
        for r in selected:
            r_no = r[0].get("round_no") or 0
            if r_no in seen:
                continue
            seen.add(r_no)
            deduped.append(r)
        selected = sorted(deduped, key=lambda r: r[0].get("round_no") or 0)

        msgs = []
        skill_header = _render_skill_header(self)
        if skill_header:
            msgs.append({"role": "user", "content": skill_header})
        total = len(rounds)
        omitted = total - len(selected)
        if omitted > 0 and len(selected) > 1:
            msgs.append({"role": "user", "content": f"（{omitted} 轮历史已省略）"})
        for entries in selected:
            r_no = entries[0].get("round_no") or 0
            is_last = (r_no == last_r_no)
            is_prev = (r_no == last_r_no - 1)
            user_text = _render_round_user(self, entries)
            reply_text = _render_round_reply(self, entries)
            block = []
            if user_text:
                block.append({"role": "user", "content": user_text})
            if reply_text and not is_last:
                block.append({"role": "assistant", "content": reply_text})
            if is_last:
                msgs.extend(block)
                chains = self._group_chains(entries)
                for c_no in sorted(chains.keys()):
                    full = _render_chain_full(self, chains[c_no], r_no, c_no)
                    if full:
                        msgs.append({"role": "user", "content": full})
            elif is_prev:
                msgs.extend(block)
                chains = self._group_chains(entries)
                for c_no in sorted(chains.keys()):
                    t, a = self._chain_target_action(chains[c_no])
                    hint = _chain_result_hint(self, chains[c_no])
                    label = f"{t}.{a}" if a and a != "none" else t
                    msgs.append({"role": "user", "content": f"## {r_no}.{c_no} {label}{hint}"})
            else:
                msgs.extend(block)
        return msgs

    def estimate_context_chars(self):
        try:
            return sum(len(m.get("content") or "") for m in self.get_messages())
        except Exception:
            return 0

    async def snapshot_round(self, round_no):
        ignored = [
            k.lstrip("#") for k, v in self._ignored.items()
            if v > self._current_turn and k.count(".") == 2
        ]
        self._round_snapshots[round_no] = {"collapsed": [], "ignored": ignored}
        await self.append_system(
            action="round_snapshot",
            result_think=f"📌 固化第 {round_no} 轮 ignore 形态",
            detail={"ignored": ignored},
            retain=1,
            chain_no=0,
        )

    async def mark_interrupted(self, reason, error="", pending=None):
        completed = [
            f"##{e.get('round_no')}.{e.get('chain_no')}"
            for e in self._flow
            if e.get("action") == "call_end" and (e.get("chain_no") or 0) > 0
        ]
        side_effects = [
            f"##{e.get('round_no')}.{e.get('chain_no')}"
            for e in self._flow
            if e.get("action") == "call_end"
            and (e.get("chain_no") or 0) > 0
            and any(str((e.get("detail") or {}).get("target", "")).startswith(t)
                    for t in SIDE_EFFECT_TARGETS)
        ]
        await self.append_system(
            action="interrupted",
            result_think=f"⚠️ 本轮未正常结束（{reason}）",
            detail={
                "reason": reason,
                "error": (error or "")[:200],
                "pending": pending or {},
                "completed_chains": completed,
                "side_effects": side_effects,
            },
            retain=2,
        )
        return {"completed_chains": completed, "side_effects": side_effects}


# ============================================================
# 渲染辅助
# ============================================================
def _summarize_big_fields(detail):
    _BIG_KEYS = ("b64", "base64", "image", "images", "bytes", "blob", "raw")
    try:
        r = detail.get("result")
        if isinstance(r, dict):
            data = r.get("data")
            if isinstance(data, dict):
                for k in list(data.keys()):
                    if k not in _BIG_KEYS:
                        continue
                    v = data.get(k)
                    if isinstance(v, str) and len(v) > 200:
                        data[k] = f"<{len(v)} 字 base64，已省略>"
                    elif isinstance(v, list) and v and isinstance(v[0], str) and len(v[0]) > 200:
                        data[k] = f"<{len(v)} 项 base64，已省略>"
                    elif isinstance(v, bytes):
                        data[k] = f"<{len(v)} 字节，已省略>"
    except Exception:
        pass


def _truncate_if_too_big(detail):
    try:
        s = json.dumps(detail, ensure_ascii=False, default=str)
        if len(s.encode("utf-8")) > FLOW_ENTRY_MAX_BYTES:
            r = detail.get("result")
            if isinstance(r, dict):
                r_str = json.dumps(r, ensure_ascii=False, default=str)
                if len(r_str.encode("utf-8")) > FLOW_ENTRY_MAX_BYTES:
                    detail["result"] = {
                        "_truncated": True,
                        "_original_size": len(r_str),
                        "preview": r_str[: FLOW_ENTRY_MAX_BYTES // 2],
                    }
    except Exception:
        pass


def _render_skill_header(flow):
    task_summary = getattr(flow, "_task_summary", "") or ""
    picked_skills = getattr(flow, "_picked_skills", []) or []
    if not task_summary and not picked_skills:
        return ""
    pinned = [s for s in picked_skills if s.get("pinned")]
    normal = [s for s in picked_skills if not s.get("pinned")]
    lines = ["【当前任务可能用到的技能】"]
    if task_summary:
        lines.append(f"任务意图：{task_summary}")
    if normal:
        lines.append("可能相关（按需 use_tool(target=\"skill/<name>\") 读取全文）：")
        for sk in normal:
            name = sk.get("name", "")
            desc = sk.get("desc", "")
            if name:
                lines.append(f"- {name} — {desc}" if desc else f"- {name}")
    if pinned:
        lines.append("")
        lines.append("【已钉住的技能（用户手工加载，全文如下，必须遵守）】")
        total_budget = 50000
        used = 0
        for i, sk in enumerate(pinned):
            name = sk.get("name", "")
            desc = sk.get("desc", "")
            full = sk.get("full_text", "") or ""
            if name:
                lines.append(f"### {name}" + (f" — {desc}" if desc else ""))
            if full:
                remaining = total_budget - used
                if remaining <= 0:
                    lines.append(f"（已省略后续 {len(pinned) - i} 个钉住技能的全文）")
                    break
                cut = min(20000, remaining)
                if len(full) > cut:
                    full = full[:cut] + f"\n…（截断，全文 {len(full)} 字）"
                for ln in full.splitlines():
                    lines.append(("> " + ln) if ln else ">")
                used += len(full)
            else:
                lines.append("（无全文，请用 use_tool(target=\"skill/...\") 读取）")
    return "\n".join(lines)


def _render_round_user(flow, entries):
    r_no = entries[0].get("round_no") or 0
    if r_no and flow._is_ignored(str(r_no)):
        return ""
    for e in entries:
        if e.get("from") == "user" and (e.get("content") or ""):
            return f"👤 用户：{e['content']}"
    return ""


def _render_round_reply(flow, entries):
    r_no = entries[0].get("round_no") or 0
    if r_no and flow._is_ignored(str(r_no)):
        return ""
    reply, think = "", ""
    for e in entries:
        if e.get("from") == "ai" and e.get("call") == "reply" and (e.get("content") or ""):
            reply = e["content"]
            think = (e.get("think") or "").strip()
            break
    if not reply:
        return ""
    think_line = ""
    if think:
        t = think.replace("\n", " ").strip()
        if len(t) > 100:
            t = t[:100] + "…"
        think_line = f"（上一轮 think，未经验证，不可当事实：{t}）"
    return (think_line + "\n\n" + reply) if think_line else reply


def _render_chain_full(flow, chain_entries, r_no, c_no):
    if not chain_entries:
        return ""
    target, action = flow._chain_target_action(chain_entries)
    label = f"{target}.{action}" if action and action != "none" else target
    chain_think = ""
    for e in chain_entries:
        if e.get("from") == "ai" and e.get("call") == "use_tool":
            chain_think = (e.get("think") or "").strip()
            break
    chain_think = chain_think.replace("\n", " ").strip()
    if len(chain_think) > 100:
        chain_think = chain_think[:100] + "…"
    if flow._is_ignored(f"##{r_no}.{c_no}") or flow._is_ignored(str(r_no)):
        return ""
    lines = [f"【本轮工具原始返回（待你交付）##{r_no}.{c_no}】{label}"]
    if chain_think:
        # 标注来源：think 是「我上一轮的想法」，不是工具返回的事实
        lines.append(f"上一轮 think（想法，未经验证，不可当事实）：{chain_think}")
    start_params = None
    for e in chain_entries:
        if e.get("action") == "call_start":
            start_params = (e.get("detail") or {}).get("params") or {}
            break
    if start_params:
        try:
            p_str = json.dumps(start_params, ensure_ascii=False, default=str)
        except Exception:
            p_str = str(start_params)
        lines.append(f"入参：{p_str}")
    end_step_no = None
    for e in chain_entries:
        if e.get("action") == "call_end":
            end_step_no = e.get("step_no") or 0
            break
    if end_step_no and flow._is_ignored(f"###{r_no}.{c_no}.{end_step_no}"):
        return ""
    end_result, end_error = None, None
    for e in chain_entries:
        if e.get("action") == "call_end":
            end_result = (e.get("detail") or {}).get("result")
            break
        if e.get("action") == "call_error":
            end_error = (e.get("detail") or {}).get("error", "")
            break
    if end_error:
        lines.append(f"❌ 错误：{str(end_error)}")
    elif end_result is not None:
        skill_body, skill_path = None, ""
        if isinstance(end_result, dict):
            data = end_result.get("data")
            if isinstance(data, dict) and isinstance(data.get("content"), str):
                skill_body = data["content"]
                skill_path = str(data.get("path") or "")
        if skill_body is not None:
            if len(skill_body) > 20000:
                skill_body = skill_body[:20000] + f"\n…（截断，全文 {len(skill_body)} 字）"
            head = f"skill 全文（{skill_path}）：" if skill_path else "skill 全文："
            lines.append(head)
            for ln in skill_body.splitlines():
                lines.append(("> " + ln) if ln else ">")
        else:
            _BIG_KEYS = ("b64", "base64", "image", "images", "bytes", "blob", "raw")
            def _summarize(obj, _d=0):
                if _d > 6:
                    return obj
                if isinstance(obj, dict):
                    out = {}
                    for k, v in obj.items():
                        if k in _BIG_KEYS:
                            if isinstance(v, str) and len(v) > 200:
                                out[k] = f"<{len(v)} 字 base64，已省略>"
                                continue
                            if isinstance(v, list):
                                out[k] = f"<{len(v)} 项 base64，已省略>"
                                continue
                            if isinstance(v, bytes):
                                out[k] = f"<{len(v)} 字节，已省略>"
                                continue
                        out[k] = _summarize(v, _d + 1)
                    return out
                if isinstance(obj, list):
                    return [_summarize(x, _d + 1) for x in obj]
                return obj
            try:
                safe = _summarize(end_result)
                r_str = json.dumps(safe, ensure_ascii=False, default=str)
            except Exception:
                r_str = str(end_result)
            if len(r_str) > 20000:
                r_str = r_str[:20000] + f"…（截断，全文 {len(r_str)} 字）"
            lines.append(f"返回：{r_str}")
    else:
        lines.append(f"返回：{r_str}")
        lines.append("（以上为原始返回。若用户要清单/结论/汇总，直接产出成品，不要回复\"我将...\"）")
        # 失败链：think 里的「已读完/已确认」未被本次返回证实
    _failed = bool(end_error) or (
        isinstance(end_result, dict) and end_result.get("ok") is False
    ) or (
        isinstance(end_result, dict)
        and isinstance(end_result.get("data"), dict)
        and end_result["data"].get("total") == 0
    )
    if _failed and chain_think:
        lines.append(
            "（注意：本次工具调用失败或 0 命中，"
            "上面「上一轮想法」里声称的\"已读完/已确认\"未被证实，不要当事实。）"
        )
    return "\n".join(lines)


def _chain_result_hint(flow, chain_entries):
    target, action = flow._chain_target_action(chain_entries)
    is_disc = any(x in (target or "").lower() for x in flow._DISCOVERY_TARGETS) or \
              (action or "").lower() in flow._DISCOVERY_ACTIONS
    for e in chain_entries:
        if e.get("action") == "call_error":
            err = str((e.get("detail") or {}).get("error", ""))[:60]
            return f" → ❌ {err}" if err else " → ❌ 失败"
    for e in chain_entries:
        if e.get("action") != "call_end":
            continue
        res = (e.get("detail") or {}).get("result")
        if not isinstance(res, dict):
            continue
        if not res.get("ok", True):
            err = str(res.get("error", ""))[:60]
            return f" → ❌ {err}" if err else " → ❌ 失败"
        data = res.get("data") if isinstance(res.get("data"), (dict, list)) else res
        if is_disc:
            n = None
            if isinstance(data, list):
                n = len(data)
            elif isinstance(data, dict):
                for k in ("total", "count", "total_plugins", "total_frontend",
                          "mcp_tool_count", "total_skills", "lines_count"):
                    v = data.get(k)
                    if isinstance(v, (int, float)):
                        n = v
                        break
            return f" → {n}条" if n is not None else " → ✅"
        s = str(res.get("summary", "") or "")
        if s:
            first = s.split("\n")[0].strip()
            if first:
                return " → " + first[:60]
        if isinstance(data, dict):
            content = data.get("full_content") or data.get("content")
            if isinstance(content, str) and content:
                size = data.get("size") or len(content)
                if size < 1024:
                    return f" → {size}B 文本"
                if size < 1024 * 1024:
                    return f" → {size/1024:.1f}KB 文本"
                return f" → {size/1024/1024:.1f}MB 文本"
            path = data.get("path") or data.get("file_path")
            if path:
                return f" → {str(path).replace(chr(92), '/').split('/')[-1]}"
            stderr = data.get("stderr")
            if isinstance(stderr, str) and stderr.strip():
                return " → ❌ " + stderr.strip().split("\n")[0][:60]
            stdout = data.get("stdout")
            if isinstance(stdout, str) and stdout.strip():
                return " → " + stdout.strip().split("\n")[0][:60]
    return ""
