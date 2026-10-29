# plugins/builtins/tools/search_memory_agent.py

"""会话记忆检索插件。

三种模式（rounds / steps / keywords 三选一）：
- rounds     按轮次返回「因果叙事」（一段话）
- steps      按轮内工具链（对应 UI 的 ##N.M）返回「因果叙事」
- keywords   按关键词召回原文片段并生成答案

定位文件：只需 session_id（全局唯一），自动在
    <repo>/data/memories/main_agent/users/*/conversations/<session_id>.jsonl
中查找；user_id 变成可选参数，仅用于加速与消歧。
"""

import json
import re
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

MEMORY_DIR_REL = Path("data") / "memories" / "main_agent"


# ============================================================
# 基础：定位会话文件
# ============================================================

def _base_dir() -> Path:
    candidates = []
    try:
        here = Path(__file__).resolve()
        candidates.append(here.parents[3] / MEMORY_DIR_REL)
    except Exception:
        pass
    candidates.append(MEMORY_DIR_REL)
    for c in candidates:
        try:
            if c.is_dir():
                return c
        except Exception:
            continue
    return MEMORY_DIR_REL


def _parse_date(date_str: str) -> Optional[datetime]:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except Exception:
        return None


def _session_files(session_id: str, user_id: Optional[str] = None) -> List[Path]:
    """按 session_id 找 flow.jsonl；只认新布局 sessions/{sid}/flow.jsonl。

    路径约定与 main_agent._conversations_dir 一致：
        data/memories/main_agent/users/{uid}/sessions/{sid}/flow.jsonl
    user_id 可选，用于加速与消歧。
    """
    base = _base_dir()
    found: List[Path] = []
    seen = set()

    def add(p: Path) -> None:
        try:
            if p.is_file():
                key = str(p.resolve()).lower()
                if key not in seen:
                    seen.add(key)
                    found.append(p)
        except Exception:
            pass

    def _try_user(uid: str) -> None:
        add(base / "users" / uid / "sessions" / session_id / "flow.jsonl")

    if user_id:
        _try_user(user_id)
        if not found:
            users_dir = base / "users"
            try:
                if users_dir.is_dir():
                    for u in sorted(users_dir.iterdir()):
                        if u.is_dir() and u.name.lower() == str(user_id).lower():
                            _try_user(u.name)
            except Exception:
                pass
    else:
        users_dir = base / "users"
        try:
            if users_dir.is_dir():
                for u in sorted(users_dir.iterdir()):
                    if u.is_dir():
                        _try_user(u.name)
        except Exception:
            pass

    if not found:
        try:
            for p in base.rglob(f"sessions/{session_id}/flow.jsonl"):
                add(p)
        except Exception:
            pass
    return found


def _available_sessions(limit: int = 30) -> List[str]:
    """列出可用 session_id（只认 sessions/{sid}/flow.jsonl）。"""
    base = _base_dir()
    ids: List[str] = []
    try:
        for p in base.rglob("flow.jsonl"):
            if "sessions" in p.parts:
                ids.append(p.parent.name)
    except Exception:
        pass
    return sorted(set(ids))[-limit:]


def _int_or_none(v: Any) -> Optional[int]:
    try:
        s = str(v).strip()
        if s.lstrip("-").isdigit():
            return int(s)
    except Exception:
        pass
    return None


def _load_entries(
    files: List[Path],
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> List[Dict]:
    start = _parse_date(date_from) if date_from else None
    end = _parse_date(date_to) if date_to else None
    if end:
        end = end.replace(hour=23, minute=59, second=59, microsecond=999999)

    entries: List[Dict] = []
    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(e, dict):
                        continue
                    if start or end:
                        ts = e.get("timestamp", "")
                        try:
                            dt = datetime.fromisoformat(ts)
                        except Exception:
                            continue
                        if start and dt < start:
                            continue
                        if end and dt > end:
                            continue
                    e["_src"] = str(f)
                    entries.append(e)
        except Exception:
            continue
    entries.sort(key=lambda x: x.get("timestamp", ""))
    # ★ 给每条加稳定 _idx，避免 id() 做 key 的脆性
    for i, e in enumerate(entries):
        e["_idx"] = i
    return entries


# ============================================================
# 切轮 / 切工具链
# ============================================================

def _group_rounds(entries: List[Dict]) -> List[Dict]:
    """返回 [{round_no, entries}]；无 round_no 时回退按 user 消息切轮。"""
    rounds: List[Dict] = []
    cur: Optional[Dict] = None
    for e in entries:
        rn = _int_or_none(e.get("round_no"))
        if rn is None:
            if e.get("from") == "user" and cur is not None:
                rounds.append(cur)
                cur = None
            if cur is None:
                cur = {"round_no": len(rounds) + 1, "entries": []}
                rounds.append(cur)
            cur["entries"].append(e)
            continue
        if cur is None or cur["round_no"] != rn:
            cur = {"round_no": rn, "entries": []}
            rounds.append(cur)
        cur["entries"].append(e)
    if cur is not None:
        rounds.append(cur)
    return rounds


def _group_chains(round_entries: List[Dict]) -> List[Dict]:
    """一轮拆工具链：按 chain_no 切；缺 chain_no 时按 ai 决策切。"""
    chains: List[Dict] = []
    cur: Optional[Dict] = None
    for e in round_entries:
        cn = _int_or_none(e.get("chain_no"))
        if cn is None:
            if cur is None:
                cur = {"chain_no": len(chains), "entries": []}
                chains.append(cur)
            elif e.get("from") == "ai" and cur["entries"]:
                cur = {"chain_no": len(chains), "entries": []}
                chains.append(cur)
            cur["entries"].append(e)
            continue
        if cur is None or cur["chain_no"] != cn:
            cur = {"chain_no": cn, "entries": []}
            chains.append(cur)
        cur["entries"].append(e)
    return chains


def _round_question(round_entries: List[Dict]) -> str:
    for e in round_entries:
        if e.get("from") == "user":
            return str(e.get("content") or "")
    return ""


# ============================================================
# 精简工具
# ============================================================

def _slim_params(params: dict, max_len: int = 80) -> str:
    if not isinstance(params, dict):
        return ""
    priority = ("path", "action", "query", "keywords", "target", "url",
                "text", "code", "content", "id")
    parts = []
    for k in priority:
        if k in params:
            v = params[k]
            if isinstance(v, str):
                parts.append(f"{k}={v[:50]}")
            elif isinstance(v, (int, float, bool)):
                parts.append(f"{k}={v}")
            elif isinstance(v, list):
                parts.append(f"{k}[{len(v)}]")
            elif isinstance(v, dict):
                parts.append(f"{k}{{{len(v)}}}")
    for k, v in params.items():
        if k in priority:
            continue
        parts.append(f"{k}=?")
        if sum(len(p) for p in parts) > max_len:
            break
    return " ".join(parts)[:max_len]


def _slim_result(res, max_len: int = 100) -> str:
    if not isinstance(res, dict):
        return str(res)[:max_len]
    ok = res.get("ok", True)
    if not ok:
        err = str(res.get("error", ""))[:max_len]
        return f"❌ {err}"
    data = res.get("data", {})
    if not isinstance(data, dict):
        return f"✅ {str(data)[:max_len]}"
    keys = []
    for k in ("path", "size", "size_kb", "total_lines", "lines_count",
              "returned", "status", "matches_count", "total", "saved",
              "loaded", "compile_ok", "error_category", "pinned"):
        if k in data:
            v = data[k]
            keys.append(f"{k}={str(v)[:40]}")
    if keys:
        return "✅ " + " ".join(keys)[:max_len]
    return f"✅ keys={list(data.keys())[:5]}"


# ============================================================
# 因果链构建
# ============================================================

def _decision_signature(entry: Dict[str, Any]):
    """从 AI 决策条目提取 (target, action, plugin_args)。"""
    args = entry.get("args")
    if not isinstance(args, dict):
        return "", "", {}
    target = str(args.get("target") or "")
    action = str(args.get("action") or "")
    pa = args.get("plugin_args")
    if not isinstance(pa, dict):
        pa = {}
    return target, action, pa


def _collect_calls(round_entries: List[Dict]) -> List[Dict]:
    """按 call_id 把 call_start / call_end / call_error 配成完整调用。

    ★ 判定粒度是 call_id，不是 chain_no：同一个 chain 内可能有多组 call_id，
      且 call_end 可能落到下一条 chain，只按 chain 粗判会误标「未执行」。
    """
    calls: Dict[str, Dict] = {}
    order: List[Dict] = []
    for e in round_entries:
        act = e.get("action")
        if act not in ("call_start", "call_end", "call_error"):
            continue
        detail = e.get("detail")
        if not isinstance(detail, dict):
            continue
        cid = detail.get("call_id")
        if not cid:
            continue
        c = calls.get(cid)
        if c is None:
            c = {
                "call_id": cid,
                "target": detail.get("target") or "",
                "action": detail.get("action") or "",
                "params": {},
                "result": None,
                "error": "",
                "done": False,
            }
            calls[cid] = c
            order.append(c)
        if act == "call_start":
            c["action"] = detail.get("action") or c["action"]
            params = detail.get("params")
            if isinstance(params, dict):
                c["params"] = params
        elif act == "call_end":
            c["done"] = True
            c["result"] = detail.get("result")
        elif act == "call_error":
            c["done"] = True
            c["error"] = str(detail.get("error") or "")[:300]
    return order


def _assign_calls(decisions: List[Dict], calls: List[Dict]) -> None:
    """把 AI 决策和真实调用配对；配不上的即「规划步，未执行」。"""
    used = set()
    for d in decisions:
        want = _slim_params(d.get("params") or {}, 200)
        for c in calls:
            if c["call_id"] in used:
                continue
            if c["target"] != d["target"]:
                continue
            if d["action"] and c["action"] and c["action"] != d["action"]:
                continue
            got = _slim_params(c.get("params") or {}, 200)
            if want and got and want == got:
                d["call"] = c
                used.add(c["call_id"])
                break
    # 兜底：只有「唯一候选」才认，避免把别的调用的结果算到这步头上
    for d in decisions:
        if d.get("call"):
            continue
        cands = [c for c in calls
                 if c["call_id"] not in used
                 and c["target"] == d["target"]
                 and not (d["action"] and c["action"] and c["action"] != d["action"])]
        if len(cands) != 1:
            continue
        d["call"] = cands[0]
        used.add(cands[0]["call_id"])


def _call_result(call: Optional[Dict]) -> Dict[str, Any]:
    """把一次真实调用转成 result 结构。"""
    if call is None:
        return {"ok": None, "note": "规划步，未执行"}
    if call.get("error"):
        return {"ok": False, "error": str(call["error"])[:150], "key_fields": ""}
    if not call.get("done"):
        return {"ok": None, "note": "调用已发起，未见返回（可能中断）"}
    res = call.get("result")
    if isinstance(res, dict):
        ok = res.get("ok", True)
        return {
            "ok": ok,
            "error": (str(res.get("error", ""))[:150] if not ok else ""),
            "key_fields": _slim_result(res) if ok else "",
        }
    if res is not None:
        return {"ok": True, "error": "", "key_fields": str(res)[:120]}
    return {"ok": None, "note": "无结果字段"}


def _build_links(round_entries: List[Dict],
                 only_chain_no: Optional[int] = None) -> List[Dict]:
    """一轮 → 多个「工具链」链接。每环描述一次决策/调用/结果。

    ★ 调用先按 call_id 配对，再回挂到发起它的 AI 决策上，
      不再按 chain_no 粗判「这一步到底跑没跑」。
    """
    chains = _group_chains(round_entries)
    calls = _collect_calls(round_entries)

    decisions: List[Dict] = []
    for ci, ch in enumerate(chains):
        for e in ch["entries"]:
            if e.get("from") != "ai":
                continue
            tgt, act, pa = _decision_signature(e)
            if not tgt:
                continue
            decisions.append({
                "chain_idx": ci, "entry": e,
                "target": tgt, "action": act, "params": pa,
            })

    _assign_calls(decisions, calls)

    links: List[Dict] = []
    for ci, ch in enumerate(chains):
        if only_chain_no is not None and ch["chain_no"] != only_chain_no:
            continue
        mine = [d for d in decisions if d["chain_idx"] == ci]
        if not mine:
            cids = set()
            for e in ch["entries"]:
                det = e.get("detail")
                if isinstance(det, dict) and det.get("call_id"):
                    cids.add(det.get("call_id"))
            orphan = [c for c in calls if c["call_id"] in cids]
            if not orphan:
                continue
            c = orphan[0]
            mine = [{"chain_idx": ci, "entry": None, "target": c["target"],
                     "action": c["action"], "params": c.get("params") or {},
                     "call": c}]
        for d in mine:
            links.append(_make_link(chains, ci, d))
    return links


def _make_link(chains: List[Dict], ci: int, d: Dict[str, Any]) -> Dict[str, Any]:
    """单条「决策 → 调用 → 结果」链接。"""
    ch = chains[ci]
    entries = ch["entries"]
    aie = d.get("entry")
    if aie is None:
        aie = next((e for e in entries if e.get("from") == "ai"), None)
    trigger = ""
    if aie:
        trigger = (aie.get("think", "") or "")[:150] or \
            (aie.get("content", "") or "")[:150]

    call = d.get("call")
    target = d.get("target") or (call.get("target") if call else "")
    action = d.get("action") or (call.get("action") if call else "")
    params_hint = _slim_params(d.get("params") or {})
    if not params_hint and call:
        params_hint = _slim_params(call.get("params") or {})

    next_trigger = ""
    if ci + 1 < len(chains):
        nxt = next((e for e in chains[ci + 1]["entries"] if e.get("from") == "ai"), None)
        if nxt:
            next_trigger = (nxt.get("think", "") or "")[:100] or \
                (nxt.get("content", "") or "")[:100]

    return {
        "chain_no": ch["chain_no"],
        "trigger": trigger,
        "target": target,
        "action": action,
        "params_hint": params_hint,
        "result": _call_result(call),
        "next_step": next_trigger,
    }


def _links_to_text(links: List[Dict], round_no: int, question: str = "") -> str:
    if not links:
        return f"第 {round_no} 轮无工具记录。"
    parts = []
    for c in links:
        seg = ""
        if c["trigger"]:
            seg += f"因为{c['trigger']}，"
        target = c.get("target") or ""
        action = c.get("action") or ""
        seg += f"调了 {target}/{action}" if action else f"调了 {target}"
        if c.get("params_hint"):
            seg += f"（{c['params_hint']}）"
        r = c.get("result") or {}
        if isinstance(r, dict):
            if r.get("ok") is None:
                seg += f"，{r.get('note') or '未执行'}"
            elif r.get("ok"):
                seg += f"，结果 {r.get('key_fields') or '成功'}"
            else:
                seg += f"，结果失败：{r.get('error') or '未知错误'}"
        if c.get("next_step"):
            seg += f"；于是{c['next_step']}"
        parts.append(seg)
    head = f"第 {round_no} 轮"
    if question:
        head += f"（用户问：{question[:60]}）"
    return head + "：" + "。".join(parts) + "。"


NARRATE_CHAIN_SYSTEM_PROMPT = """你是因果链叙述器。你的职责：把一轮的「因果链」压成一段「因为 A，所以 B，导致 C」的话。

## 要求

- **一段话**，不超过 300 字
- **以因果为主线**：「因为用户说 X，所以读了 Y，发现 Z，于是改了 W，结果 V，下一步要 U」
- **保留关键信息**：文件名、错误类型、成功/失败、下一轮要做什么
- **省略**：具体代码、完整参数、重复步骤
- **不说「第一步、第二步」**，直接说因果
- **必须说清「这一轮的最终结果」和「下一轮的起点」**
- 如果某一步是「规划步，未执行」，不要写成「失败」，写「计划做 X 但未执行」

## 输出格式

只输出一段话，不要标题、不要列表、不要解释。"""


async def _narrate_chain(agent, round_no: int, links: List[Dict],
                         step_count: int, question: str = "") -> str:
    if not links:
        return f"第 {round_no} 轮无因果记录。"

    chain_json = json.dumps(links, ensure_ascii=False, indent=2)
    q_line = f"用户本轮提问：{question}\n\n" if question else ""

    prompt = (f"第 {round_no} 轮，共 {step_count} 个工具链。因果链：\n\n"
              f"{chain_json}\n\n"
              f"{q_line}"
              "请压成一段话（不超过 300 字），**以因果为主线**，"
              "说清「因为什么，做了什么，结果如何，下一步是什么」。")

    try:
        raw = await agent.llm.chat([
            {"role": "system", "content": NARRATE_CHAIN_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ])
        raw = (raw or "").strip()
        if not raw:
            return _links_to_text(links, round_no, question)
        return raw[:1000]
    except Exception:
        return _links_to_text(links, round_no, question)


# ============================================================
# 关键词召回：字段拆分 + 加权打分
# ============================================================

FIELD_WEIGHTS = {
    "content": 6,
    "summary": 5,
    "think": 4,
    "args": 3,
    "tool": 2,
    "result": 1,
}

PATH_KEYS = ("path", "file", "file_path", "target", "skill_id", "plugin", "url")


def _entry_fields(entry: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k in ("content", "summary", "think"):
        v = entry.get(k)
        if v:
            out[k] = str(v)

    args = entry.get("args")
    if isinstance(args, dict):
        out["args"] = json.dumps(args, ensure_ascii=False, default=str)[:20000]
    elif isinstance(args, str) and args.strip():
        out["args"] = args[:20000]

    detail = entry.get("detail")
    if isinstance(detail, dict):
        out["tool"] = json.dumps(detail, ensure_ascii=False, default=str)[:20000]
        res = detail.get("result")
        if isinstance(res, dict):
            out["result"] = json.dumps(res, ensure_ascii=False, default=str)[:20000]
        elif res is not None:
            out["result"] = str(res)[:20000]
    elif isinstance(detail, str) and detail.strip():
        out["tool"] = detail[:20000]

    r = entry.get("result")
    if isinstance(r, str) and r.strip():
        out.setdefault("result", r[:20000])
    return out


def _path_hits(entry: Dict[str, Any], keywords: List[str]) -> List[str]:
    args = entry.get("args")
    if not isinstance(args, dict):
        return []
    hits: List[str] = []
    for key in PATH_KEYS:
        v = args.get(key)
        if not isinstance(v, str) or not v:
            continue
        lv = v.lower()
        for kw in keywords:
            if kw and kw.lower() in lv and kw not in hits:
                hits.append(kw)
    return hits


def _score_entry(entry: Dict[str, Any], keywords: List[str]) -> Tuple[int, List[str], Dict[str, List[str]]]:
    fields = _entry_fields(entry)
    if not fields:
        return 0, [], {}
    lowered = {k: v.lower() for k, v in fields.items()}
    total = 0
    hit_kw: List[str] = []
    hit_fields: Dict[str, List[str]] = {}
    for kw in keywords:
        k = (kw or "").lower().strip()
        if not k:
            continue
        best = 0
        for fname, weight in FIELD_WEIGHTS.items():
            if fname not in lowered:
                continue
            if k in lowered[fname]:
                if weight > best:
                    best = weight
                hit_fields.setdefault(fname, [])
                if kw not in hit_fields[fname]:
                    hit_fields[fname].append(kw)
        if best:
            total += best
            hit_kw.append(kw)
    return total, hit_kw, hit_fields


def _display_text(entry: Dict[str, Any]) -> str:
    frm = entry.get("from")
    if frm == "user":
        return str(entry.get("content") or "")
    if frm == "ai":
        parts = []
        c = str(entry.get("content") or "").strip()
        if c:
            parts.append(c)
        t = str(entry.get("think") or "").strip()
        if t:
            parts.append("[决策] " + t)
        args = entry.get("args")
        if isinstance(args, dict) and args:
            parts.append("[参数] " + _slim_params(args, 300))
        elif isinstance(args, str) and args.strip():
            parts.append("[参数] " + args[:300])
        return "\n".join(parts)
    if frm == "system":
        action = entry.get("action", "")
        detail = entry.get("detail")
        bits = [f"[{action}]"]
        if isinstance(detail, dict):
            t = detail.get("target")
            ac = detail.get("action")
            if t:
                bits.append(f"{t}/{ac}" if ac else str(t))
            res = detail.get("result")
            if isinstance(res, dict):
                bits.append(_slim_result(res, 220))
            elif res is not None:
                bits.append(str(res)[:220])
        elif isinstance(detail, str):
            bits.append(detail[:220])
        return " ".join(bits)
    return str(entry.get("content") or "")


_CN_STOP = {
    "什么", "怎么", "如何", "哪些", "一个", "这个", "那个", "可以", "需要",
    "的是", "了的", "是在", "了吗", "有的", "没有", "就是", "不是", "还是",
    "我们", "你们", "他们", "自己", "现在", "已经", "可能", "应该",
    "因为", "所以", "但是", "如果", "然后", "以及", "还有", "一下",
}

_EN_STOP = {
    "the", "and", "for", "with", "was", "were", "is", "are", "be", "been",
    "of", "to", "in", "on", "at", "by", "as", "that", "this", "it", "its",
    "a", "an", "from", "or", "not", "no", "yes", "do", "does", "did",
}


def _extract_keywords(query: str, limit: int = 10) -> List[str]:
    q = (query or "").strip()
    if not q:
        return []
    raw = re.findall(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*|[\u4e00-\u9fff]+", q)
    out: List[str] = []
    for t in raw:
        if re.match(r"^[\u4e00-\u9fff]+$", t):
            if len(t) <= 4:
                if t not in _CN_STOP:
                    out.append(t)
            else:
                for i in range(len(t) - 1):
                    bg = t[i:i + 2]
                    if bg not in _CN_STOP:
                        out.append(bg)
        elif len(t) >= 2:
            lt = t.lower()
            if lt not in _EN_STOP:
                out.append(lt)

    seen = set()
    result = []
    for t in out:
        if not t or t in seen:
            continue
        seen.add(t)
        result.append(t)
        if len(result) >= limit:
            break
    return result


def _search_entries_with_context(
    files: List[Path],
    keywords: List[str],
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 30,
    max_chars: int = 30000,
) -> Tuple[List[Dict], int, List[Dict]]:
    """返回 (召回片段, 区间总条数, 命中明细)。"""
    kws = list(dict.fromkeys(kw.strip() for kw in keywords if kw and kw.strip()))
    entries = _load_entries(files, date_from, date_to)
    if not entries or not kws:
        return [], len(entries), []

    total = len(entries)

    scored = []
    for i, entry in enumerate(entries):
        score, hit_kw, hit_fields = _score_entry(entry, kws)
        if score > 0:
            score += 3 * len(_path_hits(entry, kws))
            scored.append((score, i, hit_kw, hit_fields))
    if not scored:
        return [], total, []

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    anchors = scored[:60]

    rounds = _group_rounds(entries)
    idx_map: Dict[int, Tuple[Optional[int], Optional[int]]] = {}
    question_of: Dict[int, str] = {}
    for r in rounds:
        question_of[r["round_no"]] = _round_question(r["entries"])
        for ch in _group_chains(r["entries"]):
            for e in ch["entries"]:
                idx_map[e.get("_idx")] = (r["round_no"], ch["chain_no"])

    matched: List[Dict] = []
    hit_rounds = set()
    seen_keys = set()
    for score, i, hit_kw, hit_fields in anchors:
        entry = entries[i]
        rn, cn = idx_map.get(entry.get("_idx"), (None, None))
        text = _display_text(entry)
        if not text.strip():
            continue
        key = (rn, cn, entry.get("from"), text[:120])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        if rn is not None:
            hit_rounds.add(rn)
        matched.append({
            "round_no": rn,
            "chain_no": cn,
            "from": entry.get("from"),
            "timestamp": entry.get("timestamp", ""),
            "score": score,
            "matched_keywords": hit_kw,
            "matched_fields": sorted(hit_fields.keys()),
            "content": text[:1500],
        })

    question_entries = []
    for rn in sorted(hit_rounds):
        q = question_of.get(rn, "")
        if not q.strip():
            continue
        question_entries.append({
            "round_no": rn,
            "chain_no": 0,
            "from": "user",
            "timestamp": "",
            "score": 99,
            "matched_keywords": kws,
            "matched_fields": ["question"],
            "content": f"[第 {rn} 轮提问] " + q[:800],
        })

    all_items = question_entries + matched

    ordered = sorted(all_items, key=lambda e: (-e.get("score", 0), e.get("timestamp", "")))
    kept: List[Dict] = []
    chars = 0
    for e in ordered:
        c = len(e.get("content", ""))
        if kept and chars + c > max_chars:
            continue
        kept.append(e)
        chars += c
    kept.sort(key=lambda e: (e.get("round_no") or 0, e.get("chain_no") or 0))

    if len(kept) > limit:
        kept = kept[:limit]

    detail = [{
        "round_no": e["round_no"],
        "chain_no": e["chain_no"],
        "from": e["from"],
        "matched_keywords": e["matched_keywords"],
        "matched_fields": e["matched_fields"],
        "preview": (e.get("content") or "")[:160],
    } for e in kept]

    return kept, total, detail


# ============================================================
# 关键词搜索 → 答案
# ============================================================

async def _generate_answer(agent, query: str, entries: List[Dict],
                            date_from: str, date_to: str) -> str:
    if not entries:
        return (f"在 {date_from or '不限'} ~ {date_to or '不限'} 期间，"
                f"未找到与「{query}」相关的记录。")

    items_json = json.dumps(entries, ensure_ascii=False, indent=2)

    prompt = f"""用户想问：{query}

时间范围：{date_from or '不限'} ~ {date_to or '不限'}
召回到的对话片段（已按轮次排序，round_no=第几轮，chain_no=轮内第几个工具链）：

{items_json}

请根据这些片段回答用户的问题。要求：
- 只用片段里的信息，不要编造；片段里没有就明说缺什么
- 引用时带上「第 N 轮」，方便回溯
- 文件路径、参数名、错误信息、版本号等要原样保留，不要改写
- 按时间顺序还原事件过程
- 如果记录不足以回答，明确写「记录中只提到 X，缺少 Y」
- 直接回答问题，不要输出 JSON、不要复述本提示
"""

    try:
        raw = await agent.llm.chat([{"role": "user", "content": prompt}])
        if not raw or not raw.strip():
            return (f"找到 {len(entries)} 条相关记录，但生成回答时返回空。\n\n"
                    f"匹配到的记录摘要：\n{items_json[:1500]}")
        return raw.strip()
    except Exception as e:
        return f"生成回答失败：{e}"


# ============================================================
# 主入口
# ============================================================

async def execute(envelop, agent):
    payload = envelop.payload or {}
    action = payload.get("action", "get")

    if action != "get":
        envelop.payload = {
            "ok": False,
            "error": f"不支持 action={action}（只保留 get；经验沉淀请用 add_experience）",
        }
        return envelop

    if payload.get("list_sessions"):
        envelop.payload = {
            "ok": True,
            "available_sessions": _available_sessions(),
            "hint": "取一个 session_id 传入即可检索；无需 user_id",
        }
        return envelop

    session_ids = payload.get("session_id")
    if isinstance(session_ids, str):
        session_ids = [session_ids] if session_ids.strip() else []
    if not session_ids:
        envelop.payload = {
            "ok": False,
            "error": "缺少 session_id",
            "available_sessions": _available_sessions(),
        }
        return envelop

    user_id = payload.get("user_id") or None
    date_from = payload.get("date_from") or None
    date_to = payload.get("date_to") or None

    files: List[Path] = []
    resolved: List[str] = []
    for sid in session_ids:
        sid = str(sid).strip()
        if not sid:
            continue
        for f in _session_files(sid, user_id):
            if f not in files:
                files.append(f)
                resolved.append(str(f))
    if not files:
        envelop.payload = {
            "ok": False,
            "error": f"未找到 session_id={session_ids} 对应的记忆文件",
            "available_sessions": _available_sessions(),
            "searched_under": str(_base_dir()),
        }
        return envelop

    # ---------- 按轮次搜 ----------
    rounds = payload.get("rounds")
    if rounds is not None:
        if isinstance(rounds, (int, str)):
            rounds = [rounds]
        if not isinstance(rounds, list):
            envelop.payload = {"ok": False, "error": "rounds 必须是 int 或 list[int]"}
            return envelop
        rounds = [int(r) for r in rounds if str(r).strip().isdigit()]

        all_entries = _load_entries(files, date_from, date_to)
        round_groups = _group_rounds(all_entries)
        by_no = {r["round_no"]: r for r in round_groups}

        picked = []
        missing = []
        for n in rounds:
            grp = by_no.get(n)
            if grp is None and 1 <= n <= len(round_groups):
                grp = round_groups[n - 1]
            if grp is None:
                missing.append(n)
                continue
            links = _build_links(grp["entries"])
            question = _round_question(grp["entries"])
            narrative = await _narrate_chain(agent, n, links, len(links), question)
            picked.append({
                "round": n,
                "chain_count": len(links),
                "question": question[:200],
                "narrative": narrative,
            })

        envelop.payload = {
            "ok": True,
            "rounds": rounds,
            "total_rounds": len(round_groups),
            "picked": picked,
            "missing_rounds": missing,
            "resolved_files": resolved,
        }
        return envelop

    # ---------- 按步骤搜 ----------
    steps_ref = payload.get("steps")
    if steps_ref is not None:
        if isinstance(steps_ref, (str, int)):
            steps_ref = [steps_ref]
        if not isinstance(steps_ref, list):
            envelop.payload = {"ok": False, "error": "steps 必须是 str 或 list[str]，如 '3.1'"}
            return envelop

        all_entries = _load_entries(files, date_from, date_to)
        round_groups = _group_rounds(all_entries)
        by_no = {r["round_no"]: r for r in round_groups}

        picked = []
        missing = []
        for ref in steps_ref:
            m = re.match(r"^(\d+)\.(\d+)$", str(ref).strip())
            if not m:
                missing.append(str(ref))
                continue
            rn, sn = int(m.group(1)), int(m.group(2))
            grp = by_no.get(rn)
            if grp is None and 1 <= rn <= len(round_groups):
                grp = round_groups[rn - 1]
            if grp is None:
                missing.append(str(ref))
                continue
            chains = _group_chains(grp["entries"])
            target = None
            for ch in chains:
                if ch["chain_no"] == sn:
                    target = ch
                    break
            if target is None and 1 <= sn <= len(chains):
                target = chains[sn - 1]
            if target is None:
                missing.append(str(ref))
                continue
            links = _build_links(grp["entries"])
            single = [l for l in links if l["chain_no"] == target["chain_no"]] or links[:1]
            question = _round_question(grp["entries"])
            narrative = await _narrate_chain(agent, rn, single, 1, question)
            picked.append({
                "ref": f"{rn}.{sn}",
                "round": rn,
                "chain_no": target["chain_no"],
                "question": question[:200],
                "narrative": narrative,
            })

        envelop.payload = {
            "ok": True,
            "steps": [str(s) for s in steps_ref],
            "picked": picked,
            "missing_steps": missing,
            "resolved_files": resolved,
        }
        return envelop

    # ---------- 按关键词搜 ----------
    query = payload.get("query")
    keywords = payload.get("keywords")
    if isinstance(keywords, str):
        keywords = [keywords]
    if keywords is None:
        keywords = _extract_keywords(query or "")
    if not keywords:
        envelop.payload = {
            "ok": False,
            "error": "缺少 query 参数（keywords 可省略，会自动从 query 抽取）",
        }
        return envelop
    if not query:
        query = "、".join(keywords)

    entries, total, detail = _search_entries_with_context(
        files, keywords, date_from, date_to, limit=30
    )

    answer = await _generate_answer(agent, query, entries, date_from, date_to)

    envelop.payload = {
        "ok": True,
        "answer": answer,
        "matched_count": len(entries),
        "total_in_range": total,
        "range": f"{date_from or '不限'} ~ {date_to or '不限'}",
        "keywords": keywords,
        "matched": detail,
        "resolved_files": resolved,
    }
    return envelop


def help() -> dict:
    return {
        "route": "builtins/tools/search_memory_agent",
        "description": (
            "搜索会话记忆：只需 session_id 即可定位文件。"
            "关键词（返回答案+命中片段）/ 轮次（返回因果叙事）/ 步骤（返回因果叙事）"
        ),
        "input": {
            "action": "get（唯一支持）",
            "session_id": "会话ID（必填，唯一；也接受 list 跨会话检索）",
            "user_id": "用户ID（可选，仅用于加速与消歧；不传自动搜索）",
            "list_sessions": "true 时返回可用 session_id 列表（可选）",
            "query": "关键词模式：用户想问的问题",
            "keywords": "关键词模式：关键词列表（可选，缺省自动从 query 抽取）",
            "rounds": "轮次模式：int 或 list[int]，如 [2, 3]；返回每轮因果叙事",
            "steps": "步骤模式：str 或 list[str]，如 ['3.1']（轮内第几个工具链）",
            "date_from": "开始日期 YYYY-MM-DD（可选，不传=不限）",
            "date_to": "结束日期 YYYY-MM-DD（可选，不传=不限）",
        },
        "output": {
            "ok": "是否成功",
            "answer": "关键词模式的回答",
            "matched": "命中片段明细（round_no/chain_no/matched_keywords/preview）",
            "picked": "轮次或步骤模式的因果叙事",
            "total_rounds": "总轮数（rounds 模式）",
            "matched_count": "召回条数（关键词模式）",
            "total_in_range": "区间内总条数",
            "resolved_files": "实际命中的记忆文件路径",
            "available_sessions": "可用 session_id 列表（出错时给出）",
            "range": "日期范围",
            "keywords": "实际使用的关键词",
        },
    }
