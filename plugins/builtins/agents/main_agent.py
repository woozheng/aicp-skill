"""main_agent — 主入口（tool use 版，单文件，补全推送）

推送时序：
  execute        → thinking
  LLMThinker     → summary_stream（每个文本 token）
  LLMThinker     → raw（收到 tool_call 时，含 think + tool + args）
  ToolRegistry   → executing（调工具前）
  ToolRegistry   → executing（异步提交）
  ToolRegistry   → error（失败时）
  CoreProcessor  → done（工具完成 / 回复完成）
  _handle_callback → callback（回调到达）
"""

import json
import tempfile
import time
import uuid
import os
import re
import asyncio
import shutil
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional,Tuple

import core
from runtime._llm import is_llm_error_string

# ============================================================
# 上下文基线参数（可调，用于调试）
# ============================================================
CONTEXT_BUDGET = 15000          # 每轮基线预算（字符数）
CONTEXT_HARD_LIMIT = 500000     # 递归内总长硬上限，超了重建
MIN_BASELINE_ROUNDS = 3         # 最低基线：至少保留往前 N 轮的 user/reply
CONTEXT_DEBUG = True            # 打开后每次计算基线都打日志

# ============================================================
# flow 落盘策略（JSONL 追加 + 原子 compact）
# ============================================================
_COMPACT_MAX_LINES = 2000                       # 追加 N 行触发 compact
_COMPACT_MAX_BYTES = 8 * 1024 * 1024            # 或文件超过 8M 触发 compact
_COMPACT_MIN_INTERVAL = 30.0                    # 两次 compact 至少间隔 30s（防抖）
# ============================================================
# 常量
# ============================================================
# ============================================================
# DEBUG：把 context 落盘成 md
# ============================================================
_DEBUG_CTX_ENABLED = True      # 关掉改 False
_DEBUG_CTX_DIR_NAME = "_debug_context"


def _debug_dump_context(flow: "InformationFlow", context: str, depth: int) -> str:
    """把本轮 context 写成 md 文件，返回路径。
    路径：<user_dir>/_debug_context/<session_id>/depth_{depth:02d}_{HHMMSS}.md
    """
    if not _DEBUG_CTX_ENABLED:
        return ""

    base = _conversations_dir(flow.user_id, flow.session_id) / _DEBUG_CTX_DIR_NAME
    base.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%H%M%S")
    fname = f"depth_{depth:02d}_{ts}.md"
    path = base / fname

    # 头部信息
    header = (
        f"# Context Debug\n\n"
        f"- session_id: `{flow.session_id}`\n"
        f"- user_id: `{flow.user_id}`\n"
        f"- depth: {depth}\n"
        f"- timestamp: {datetime.now().isoformat()}\n"
        f"- flow_entries: {len(flow._flow)}\n"
        f"- rounds: {flow._round_counter}\n"
        f"- chains: {flow._chain_counter}\n"
        f"- current_turn: {flow._current_turn}\n"
        f"- context_len: {len(context)}\n"
        f"- expanded: {dict(flow._expanded)}\n"
        f"- ignored: {dict(flow._ignored)}\n\n"
        f"---\n\n"
    )

    path.write_text(header + context, encoding="utf-8")

    # 顺便维护一个"最新"软链/副本，方便一直打开同一个文件
    try:
        latest = base / "_latest.md"
        latest.write_text(
            f"<!-- 最新：{fname} depth={depth} ts={ts} -->\n\n" + header + context,
            encoding="utf-8",
        )
    except Exception:
        pass

    Logger.info(f"[debug] context dumped → {path}")
    return str(path)


PROJECT = Path(__file__).parent.name
MEMORY_DIR = Path("data/memories/main_agent")
MEMORY_DIR.mkdir(parents=True, exist_ok=True)


# 单对话上限
CONV_SOFT_LIMIT = 2000          # flow 条数软上限，超了 prompt 提示
CONV_HARD_LIMIT_MB = 10        # flow 文件硬上限，超了自动归档


MAX_RECURSION_DEPTH = 40

LLM_ERROR_PREFIXES = (
    "[LLM stream error:",
    "[LLM请求失败:",
    "[系统错误:",
    "[服务请求超时",
    "[模型返回空响应",
    "[LLM 达到最大重试次数]",
    "[LLM 未配置]",
    "[空响应]",
)
_SKILL_PATH = Path(__file__).parent / "main_agent_skill.md"
_FALLBACK_SKILL = "你是 BIO-1。Session: __SESSION_ID__"
SIDE_EFFECT_TARGETS = (
    "builtins/tools/save_blocks",
    "os/_cron",
    "builtins/agents/main_agent",
)

def _load_skill() -> str:
    """每次重读 main_agent_skill.md。

    不用模块级缓存：缓存会「第一次读到空/兜底」就永久污染，
    导致后续所有调用（含分身）都拿不到真实 skill。
    文件约 50KB，read_text 开销 <1ms，远小于一轮 LLM 调用。
    """
    try:
        if not _SKILL_PATH.exists():
            Logger.warn(f"main_agent_skill.md 不存在: {_SKILL_PATH}")
            return _FALLBACK_SKILL
        text = _SKILL_PATH.read_text(encoding="utf-8")
        # Logger.info(f"[_load_skill] path={_SKILL_PATH} len={len(text)} head={text[:200]!r}")
        if not text.strip():
            Logger.warn(f"main_agent_skill.md 为空: {_SKILL_PATH}")
            return _FALLBACK_SKILL
        return text
    except Exception as e:
        Logger.error(f"读取 main_agent_skill.md 失败: {e}", e)
        return _FALLBACK_SKILL
# ============================================================
# Logger
# ============================================================

class Logger:
    @staticmethod
    def info(msg: str):
        print(f"[main_agent] {msg}")

    @staticmethod
    def warn(msg: str):
        print(f"[main_agent][WARN] {msg}")

    @staticmethod
    def error(msg: str, exc: Optional[Exception] = None):
        print(f"[main_agent][ERROR] {msg}")
        if exc:
            import traceback
            traceback.print_exc()

    @staticmethod
    def debug(msg: str):
        pass

class EmptyOutputError(RuntimeError):
    """LLM 流正常结束，但既没有 tool_call 也没有 text（模型只输出了 thinking）"""
    pass
# ============================================================
# 异常
# ============================================================

class ContentSafetyBlockedError(RuntimeError):
    """LLM 调用失败（内容安全 / 网络 / 超时）"""
    pass

# ============================================================
# 两级 ID 解析（身份级 user_id + 会话级 session_id）
# ============================================================

def resolve_ids(envelop) -> tuple[str, str]:
    """返回 (user_id, session_id)。

    user_id：身份级，只信任网关盖章的 meta.user_id；
             缺省 local（不再回退 session_id，避免内部调用把一人拆成多目录）。
    session_id：会话级，客户端可指定，缺省自动生成。

    铁律：user_id 只从 meta 取，不从 payload 取（防越权）。
    """
    payload = envelop.payload or {}
    meta = envelop.meta or {}

    user_id = (
        meta.get("user_id")
        or meta.get("auth_result", {}).get("user_id")   # gateway 短路时塞的
        or "local"
    )

    session_id = (
        payload.get("session_id")
        or meta.get("session_id")
        or f"auto_{uuid.uuid4().hex[:12]}"
    )

    return user_id, session_id
# ============================================================
# 经验背包 / 任务看板
# ============================================================
USER_ROOT = MEMORY_DIR / "users"


def _user_dir(user_id: str) -> Path:
    p = USER_ROOT / user_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def _get_experience_file(user_id: str) -> Path:
    return _user_dir(user_id) / "backpack.txt"


def _load_experience_backpack(user_id: str) -> str:
    f = _get_experience_file(user_id)
    if f.exists():
        return f.read_text(encoding="utf-8").strip()
    return ""


def _save_experience_backpack(user_id: str, content: str):
    _get_experience_file(user_id).write_text(content, encoding="utf-8")


def _get_taskboard_file(user_id: str, session_id: str) -> Path:
    return _conversations_dir(user_id, session_id) / "taskboard.txt"


def _load_taskboard(user_id: str, session_id: str) -> str:
    f = _get_taskboard_file(user_id, session_id)
    if f.exists():
        return f.read_text(encoding="utf-8").strip()
    return ""


# ============================================================
# 技能钉住：L1 目录 + 闲置自动降级（pinned -> indexed）
# ============================================================

SKILL_IDLE_ROUNDS = 6  # 钉住后连续 N 个用户回合未使用则自动降回目录

SKILL_BLOCK_RE = re.compile(
    r"<!-- loaded: (.+?)(?: hash: [0-9a-f]+)? -->\n(.*?)(?=\n*<!-- loaded: |\Z)",
    re.DOTALL,
)


def _skill_sid_from_path(file_path: str) -> str:
    name = Path(file_path).stem
    name = re.sub(r"_skill$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[^a-z0-9_]+", "_", name.lower())
    return name or "unknown"


def _skill_plugins_root() -> Path:
    """plugins/ 根目录（main_agent.py 位于 plugins/builtins/agents/）。"""
    return Path(__file__).parent.parent.parent


def _skill_sid_for_file(file_path) -> str:
    """按文件路径生成稳定 skill id（统一命名，跨目录去重靠它对齐）。

    - plugins/<ppath>/<stem>_skill.md  → plugin_<ppath>_<stem>
    - data/skills/<rel>/SKILL.md        → <rel>
    """
    norm = Path(file_path).as_posix().replace("\\", "/")
    rel = None
    for marker in ("/plugins/", "plugins/"):
        i = norm.find(marker)
        if i != -1:
            rel = norm[i + len(marker):]
            break
    if rel is not None:
        if rel.endswith("_skill.md"):
            rel = rel[: -len("_skill.md")]
        return ("plugin_" + re.sub(r"[^a-z0-9]+", "_", rel.lower())).strip("_") or "plugin"
    rel = None
    for marker in ("/data/skills/", "data/skills/"):
        i = norm.find(marker)
        if i != -1:
            rel = norm[i + len(marker):]
            break
    if rel is not None:
        if rel.endswith("/SKILL.md"):
            rel = rel[: -len("/SKILL.md")]
        elif rel == "SKILL.md":
            rel = "root"
        return re.sub(r"[^a-z0-9]+", "_", rel.lower()).strip("_") or "skill"
    return re.sub(r"[^a-z0-9]+", "_", Path(norm).stem.lower()).strip("_") or "skill"


def _resolve_skill_file(rel: str) -> Optional[Path]:
    """把 skill 相对路径解析成实际文件。

    规则与 cogitor._scan_skills 的命名一一对应：
    - plugin/<ppath>/<stem>  → plugins/<ppath>/<stem>_skill.md
    - <rel>/SKILL.md         → data/skills/<rel>/SKILL.md，兜底 data/skills/aicp/<rel>/SKILL.md
    """
    rel = (rel or "").strip("/")
    if not rel:
        return None

    if rel.startswith("plugin/"):
        sub = rel[len("plugin/"):].strip("/")
        if not sub:
            return None
        cand = _skill_plugins_root() / sub
        if not cand.name.endswith("_skill.md"):
            cand = cand.with_name(cand.name + "_skill.md")
        return cand if cand.exists() else None

    if rel.endswith("SKILL.md"):
        cand = Path("data/skills") / rel
    else:
        cand = Path("data/skills") / rel / "SKILL.md"
    if cand.exists():
        return cand
    alt = Path("data/skills/aicp") / (rel if rel.endswith("SKILL.md") else rel + "/SKILL.md")
    return alt if alt.exists() else None


def _normalize_skill_state(state: dict) -> bool:
    """按 path 去重 + 统一 id。返回 True 表示 state 已被修改。"""
    skills = state.get("skills")
    if not isinstance(skills, dict) or not skills:
        return False

    by_path = {}
    no_path = {}
    for sid, info in list(skills.items()):
        if not isinstance(info, dict):
            continue
        path = str(info.get("path") or "")
        if not path:
            no_path[sid] = info
            continue
        by_path.setdefault(path, []).append((_skill_sid_for_file(path), info))

    new_skills = {}
    for path, entries in by_path.items():
        canon = entries[0][0]
        best = entries[0][1]
        for _c, info in entries[1:]:
            info_pinned = info.get("status") == "pinned"
            best_pinned = best.get("status") == "pinned"
            if info_pinned and not best_pinned:
                best = info
            elif info_pinned == best_pinned and len(str(info.get("full_text") or "")) > len(str(best.get("full_text") or "")):
                best = info
        entry = dict(best)
        entry["id"] = canon
        entry["path"] = path
        if canon in new_skills:
            continue
        new_skills[canon] = entry

    for sid, info in no_path.items():
        new_skills.setdefault(sid, info)

    def _sig(d):
        out = {}
        for k, v in d.items():
            if isinstance(v, dict):
                out[k] = (v.get("status"), v.get("path"), len(str(v.get("full_text") or "")))
            else:
                out[k] = repr(v)
        return out

    if _sig(new_skills) == _sig(skills):
        return False
    state["skills"] = new_skills
    return True


def _skill_title_desc(body: str, file_path: str) -> tuple:
    title = Path(file_path).stem
    desc = ""
    found = False
    for line in body.split("\n"):
        s = line.strip()
        if not s or s.startswith("<!--") or set(s) <= set("= "):
            continue
        if s.startswith("# ") and not found:
            title = s[2:].strip()
            found = True
            continue
        if not desc and not s.startswith("#"):
            desc = s[:120]
            if found:
                break
    return title, desc or title


def _pinned_file(user_id: str, session_id: str) -> Path:
    p = _conversations_dir(user_id, session_id)
    return p / "pinned.json"





def _load_skill_state(user_id: str, session_id: str) -> dict:
    f = _pinned_file(user_id, session_id)
    state = {"pinned": {}, "turn": 0, "last_user_stamp": ""}
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                state = data
        except Exception:
            pass
    if not isinstance(state.get("pinned"), dict):
        state["pinned"] = {}
    if not isinstance(state.get("turn"), int):
        state["turn"] = 0
    if not isinstance(state.get("last_user_stamp"), str):
        state["last_user_stamp"] = ""
    return state


def _save_skill_state(user_id: str, session_id: str, state: dict) -> None:
    """原子写 pinned.json：先写临时文件，再替换，防写一半损坏。"""
    f = _pinned_file(user_id, session_id)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(f".tmp.{uuid.uuid4().hex[:8]}")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, f)
    except Exception as e:
        Logger.warn(f"[skill] 保存 pinned.json 失败: {e}")
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass




def _skill_usage_tokens(info: dict, sid: str) -> list:
    tokens = [sid]
    path = str(info.get("path", "") or "")
    if path:
        tokens.append(_skill_sid_from_path(path))
    extra = str(info.get("usage_token", "") or "")
    if extra:
        tokens.append(extra)
    return [t for t in tokens if t]


def _current_turn_text(context: str) -> str:
    """只取「本回合」片段：最后一次用户发言之后的内容。

    使用判定必须只看本回合——历史里出现过一次就永远算「用过」，
    闲置降级就永远不会触发。
    """
    if not context:
        return ""
    idx = context.rfind("👤")
    if idx == -1:
        return context
    return context[idx:]


def _last_user_stamp(context: str) -> str:
    last = ""
    for line in (context or "").split("\n"):
        if "\U0001F464" in line:
            last = line.strip()[:200]
    return last


def _auto_unpin_idle(user_id: str, session_id: str, context: str) -> list:
    state = _load_skill_state(user_id, session_id)
    pinned = state.get("pinned", {})
    if not pinned:
        return []
    turn_text_all = _current_turn_text(context)
    stamp = _last_user_stamp(context)
    turn = state.get("turn", 0)
    if stamp and stamp != state.get("last_user_stamp", ""):
        turn += 1
        state["last_user_stamp"] = stamp
    state["turn"] = turn
    downgraded = []
    for sid, info in list(pinned.items()):
        if not isinstance(info, dict) or info.get("status") != "pinned":
            continue
        used = any(t in turn_text_all for t in _skill_usage_tokens(info, sid) if t)
        if used:
            info["last_use_turn"] = turn
            continue
        raw = info.get("last_use_turn")
        if raw is None:
            info["last_use_turn"] = turn
            continue
        try:
            last = int(raw)
        except Exception:
            last = turn
        if turn - last >= SKILL_IDLE_ROUNDS:
            info["status"] = "indexed"
            info.pop("pinned_at", None)
            downgraded.append(sid)
    _save_skill_state(user_id, session_id, state)
    return downgraded


def _skill_index_text(user_id: str, session_id: str) -> str:
    """L1 目录：本 session 的 pinned/indexed 技能，每行一个。"""
    state = _load_skill_state(user_id, session_id)
    pinned = state.get("pinned") or {}
    if not pinned:
        return ""
    lines = ["<!-- SKILL_INDEX -->"]
    for sid, info in sorted(pinned.items()):
        if not isinstance(info, dict):
            continue
        marker = "📌" if info.get("status") == "pinned" else "📋"
        src = info.get("source", "skill")
        title = info.get("title", sid)
        desc = str(info.get("desc", "") or "")[:120]
        lines.append(f"- {marker} [{src}] {title} — {desc}")
    lines.append("<!-- /SKILL_INDEX -->")
    return "\n".join(lines)


def _mcp_contracts_dir() -> Path:
    """定位 MCP 契约缓存目录。优先用仓库绝对路径，避免进程 CWD 不一致时读不到。"""
    cands = []
    try:
        cands.append(Path(__file__).resolve().parents[3] / "data" / "os" / "_mcp_client" / "contracts")
    except NameError:
        pass
    cands.append(Path("data/os/_mcp_client/contracts"))
    for c in cands:
        if c.exists():
            return c
    return cands[-1]


def _mcp_index_text() -> str:
    """MCP 外部工具目录（L1）。

    cogitor 能搜到 mcp/xxx/yyy，但模型往往猜错参数名（如 slug vs slug_id）。
    这里直接列出每个外部工具的**参数签名**，让模型不用猜。
    """
    d = _mcp_contracts_dir()
    if not d.exists():
        return ""
    rows = []
    for f in sorted(d.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        server = str(data.get("server", "") or "")
        tool = str(data.get("tool", "") or "")
        if not server or not tool:
            continue
        schema = data.get("inputSchema") or {}
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        sig = ", ".join((k if k in required else f"{k}?") for k in props.keys())
        desc = " ".join(str(data.get("description", "") or "").split())[:110]
        head = f"{server}/{tool}({sig})" if sig else f"{server}/{tool}"
        rows.append(f"- 🔌 {head} — {desc}")
    if not rows:
        return ""
    return "\n".join(["<!-- MCP_INDEX -->"] + rows + ["<!-- /MCP_INDEX -->"])


def _load_active_skill(user_id: str, session_id: str) -> str:
    """拼“当前技能”：L1 目录 + MCP 目录 + 本 session pinned 全文。"""
    parts = []

    state = _load_skill_state(user_id, session_id)
    pinned = state.get("pinned") or {}

    index_text = _skill_index_text(user_id, session_id)
    if index_text:
        parts.append(index_text)

    mcp_text = _mcp_index_text()
    if mcp_text:
        parts.append(mcp_text)

    # pinned 全文：从 pinned.json 的 full_text 取
    pinned_blocks = []
    for sid, info in sorted(pinned.items()):
        if not isinstance(info, dict):
            continue
        if info.get("status") != "pinned":
            continue
        full = info.get("full_text", "")
        if full:
            pinned_blocks.append(full.strip())
    if pinned_blocks:
        parts.append("\n\n".join(pinned_blocks))

    return "\n\n".join(parts)



async def _update_conversation_index(user_id: str, session_id: str, flow: list):
    """每次保存 flow 后更新该 user 的 index.json。"""
    try:
        index_file = _index_file(user_id)
        index = {}
        if index_file.exists():
            index = json.loads(index_file.read_text(encoding="utf-8"))
        convs = index.get("conversations", {})
        meta = convs.get(session_id, {})
        if not meta.get("title"):
            for e in flow:
                if e.get("from") == "user" and e.get("content"):
                    meta["title"] = e["content"][:20]
                    break
        if not meta.get("created_at") and flow:
            meta["created_at"] = flow[0].get("timestamp", datetime.now().isoformat())
        meta["updated_at"] = datetime.now().isoformat()
        meta["message_count"] = len([e for e in flow if e.get("from") == "user"])
        meta["entry_count"] = len(flow)
        for e in reversed(flow):
            if e.get("from") == "ai" and e.get("call") == "reply" and e.get("content"):
                meta["last_preview"] = e["content"][:80]
                break
        meta["archived"] = meta.get("archived", False)
        convs[session_id] = meta
        index["conversations"] = convs
        index_file.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        Logger.warn(f"更新对话索引失败: {e}")


def _load_conversation_index(user_id: str) -> dict:
    index_file = _index_file(user_id)
    if index_file.exists():
        try:
            return json.loads(index_file.read_text(encoding="utf-8")).get("conversations", {})
        except Exception:
            return {}
    return {}

def _index_file(user_id: str) -> Path:
    return _conversations_dir(user_id) / "index.json"
# ============================================================
# InformationFlow
# ============================================================
def _conversations_dir(user_id: str, session_id: str = None) -> Path:
    """session 级数据唯一入口。

    - 有 session_id → users/<uid>/sessions/<sid>/
    - 无 session_id → users/<uid>/sessions/（给 index 用）
    """
    base = MEMORY_DIR / "users" / user_id / "sessions"
    p = base / session_id if session_id else base
    p.mkdir(parents=True, exist_ok=True)
    return p

# ============================================================
# InformationFlow —— 编号化可折叠信息流
# 编号体系：
#   #N        → 第 N 轮（user + reply + 状态 + 计数）
#   ##N.M     → 第 N 轮第 M 条工具链（target.action(锚点) — 💭think + 计数）
#   ###N.M.K  → 第 N 轮第 M 条链第 K 步（完整 JSON 输入/输出）
# 设计原则：
#   1) 编号写进 entry，永久不变；折叠状态在内存 dict，每轮可变
#   2) #N 零参数；##N.M 只有锚点+think+计数，绝不 dump JSON；###N.M.K 才给完整 JSON
#   3) manage_flow 只控制不查询，返回 {ok:true}
#   4) 中断不回退，保留现场 + interrupted 标记
#   5) 异步回调追加新 ##，带 related
#   6) 老数据无编号不补号不渲染，不背技术债
# ============================================================
# ============================================================
# 默认渲染配置
# ============================================================
DEFAULT_HISTORY_ROUNDS = 3        # 默认显示最近 3 轮 #N
LAST_ROUND_CHAINS_ALL = True      # 最后一轮 ## 全展开
# HOT_CHAINS 不再使用（历史轮全折叠，最后一轮全展开）

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

def _cn_to_int(s: str) -> int:
    if not s:
        return 0
    if s in _CN_NUM:
        return _CN_NUM[s]
    # 简单处理"十一"~"十九"
    if s.startswith("十") and len(s) == 2:
        return 10 + _CN_NUM.get(s[1], 0)
    if s.endswith("十") and len(s) == 2:
        return _CN_NUM.get(s[0], 0) * 10
    return 0

# 可配置基线
BASE_ROUNDS = 8          # 默认显示最近 3 轮 #N
BASE_CHAINS = 1          # 默认显示最近 1 轮的 ##（更早的 ## 折叠）
FORCE_FOLD_THRESHOLD = 200_000   # context 字符数，超过就强制提示模型折叠
# 发现类工具：只给条数，不列字段内容


class InformationFlow:
    FROZEN_TAIL = 12          # 保留常量（外部可能引用），新渲染不再使用
    _DISCOVERY_TARGETS = ("cogitor", "os/_registry", "contract_agent")
    _DISCOVERY_ACTIONS = ("list", "list_plugins", "get_map", "search",
                            "list_dir", "list_servers", "list_tools")

    def __init__(self, user_id: str, session_id: str):
        self.session_id = session_id
        self.user_id = user_id

        base = _conversations_dir(user_id, session_id)
        self.flow_file = (base / "flow.jsonl").resolve()

        self._flow: List[dict] = []

        # ★ 编号计数器（不持久化，加载时从末尾恢复）
        self._round_counter = 0
        self._chain_counter = 0
        self._step_counter = 0
        self._current_turn = 0

        # ★ 折叠状态（内存态，每轮可变；不写进 entry）
        self._expanded: Dict[str, int] = {}   # index -> 到期 turn
        self._ignored: Dict[str, int] = {}    # index -> 到期 turn

        # 落盘相关（沿用）
        self._write_lock = asyncio.Lock()
        self._append_fh = None
        self._append_fh_path = None
        self._appended_lines = 0
        self._last_compact_at = 0.0

        self._cleanup_stale_tmp()
        self._load()


    def _index_exists(self, s: str) -> bool:
        """校验编号是否对应真实 entry。"""
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

    def _gc_expanded(self):
        """清理过期 TTL。"""
        cur = self._current_turn
        self._expanded = {k: v for k, v in self._expanded.items() if v > cur}
        self._ignored = {k: v for k, v in self._ignored.items() if v > cur}

    async def manage_flow(self, collapse=None, ignore=None, expand=None, ttl: int = 3) -> dict:
        collapse = collapse or []
        ignore = ignore or []
        expand = expand or []
        ttl = max(1, min(int(ttl or 3), 20))
        self._current_turn += 1

        applied = {"collapse": [], "ignore": [], "expand": [], "invalid": []}

        for idx in collapse:
            s = self._norm_index(idx)
            if not s or not self._index_exists(s):
                applied["invalid"].append(idx); continue
            # collapse: 加到 _ignored（1 轮 TTL）或 _expanded 移除
            self._expanded.pop(s, None)
            self._ignored[s] = self._current_turn + ttl
            applied["collapse"].append(s)

        for idx in ignore:
            s = self._norm_index(idx)
            if not s or not self._index_exists(s):
                applied["invalid"].append(idx); continue
            self._ignored[s] = self._current_turn + 1   # 1 轮
            applied["ignore"].append(s)

        for idx in expand:
            s = self._norm_index(idx)
            if not s or not self._index_exists(s):
                applied["invalid"].append(idx); continue
            self._expanded[s] = self._current_turn + ttl
            self._ignored.pop(s, None)
            applied["expand"].append(s)

        self._gc_expanded()

        parts = []
        if applied["collapse"]: parts.append(f"收起 {len(applied['collapse'])} 条")
        if applied["ignore"]:   parts.append(f"关闭 {len(applied['ignore'])} 条")
        if applied["expand"]:   parts.append(f"展开 {len(applied['expand'])} 条")
        if applied["invalid"]:  parts.append(f"无效 {len(applied['invalid'])} 个")
        think = "，".join(parts) or "无变更"

        return {"ok": True, "think": think, "data": applied}
    # ============================================================
    # 编号计数器
    # ============================================================

    def _next_round(self) -> int:
        self._round_counter += 1
        self._chain_counter = 0
        self._step_counter = 0
        return self._round_counter

    def _next_chain(self) -> int:
        self._chain_counter += 1
        self._step_counter = 0
        return self._chain_counter

    def _next_step(self) -> int:
        self._step_counter += 1
        return self._step_counter

    def _restore_counters(self) -> None:
        """从末尾有编号的 entry 恢复计数器，保证新 entry 编号接续。老 entry 不补号。"""
        for e in reversed(self._flow):
            if e.get("round_no"):
                self._round_counter = max(self._round_counter, e["round_no"])
                self._chain_counter = max(self._chain_counter, e.get("chain_no", 0) or 0)
                self._step_counter = max(self._step_counter, e.get("step_no", 0) or 0)
                return

    # ============================================================
    # 落盘（沿用原实现）
    # ============================================================

    def _cleanup_stale_tmp(self):
        try:
            for p in self.flow_file.parent.glob(f".{self.flow_file.name}_*.tmp"):
                try:
                    p.unlink()
                except Exception:
                    pass
        except Exception:
            pass

    def _load(self):
        if self.flow_file.exists():
            try:
                with open(self.flow_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            self._flow.append(json.loads(line))
                        except json.JSONDecodeError:
                            Logger.warn(f"[flow] 跳过坏行 sid={self.session_id}")
                self._restore_counters()
            except Exception as e:
                Logger.error(f"[flow] 读取 jsonl 失败: {e}")
                self._flow = []



    async def _save_locked(self):
        async with self._write_lock:
            await self._compact_locked()
        await _update_conversation_index(self.user_id, self.session_id, list(self._flow))
        return True

    async def _append_line_locked(self, entry: dict):
        if self._append_fh is None or self._append_fh_path != str(self.flow_file):
            if self._append_fh is not None:
                try:
                    self._append_fh.close()
                except Exception:
                    pass
            self.flow_file.parent.mkdir(parents=True, exist_ok=True)
            self._append_fh = open(self.flow_file, "a", encoding="utf-8", newline="")
            self._append_fh_path = str(self.flow_file)

        line = json.dumps(entry, ensure_ascii=False, default=str)
        self._append_fh.write(line + "\n")
        self._append_fh.flush()
        self._appended_lines += 1

        need = False
        if self._appended_lines >= _COMPACT_MAX_LINES:
            need = True
        elif self.flow_file.exists() and self.flow_file.stat().st_size >= _COMPACT_MAX_BYTES:
            need = True
        if need and (time.monotonic() - self._last_compact_at) >= _COMPACT_MIN_INTERVAL:
            await self._compact_locked()

    async def _compact_locked(self):
        if self._append_fh is not None:
            try:
                self._append_fh.close()
            except Exception:
                pass
            self._append_fh = None
            self._append_fh_path = None

        target_dir = self.flow_file.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(target_dir), prefix=f".{self.flow_file.name}_", suffix=".tmp"
        )
        replaced = False
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                for entry in self._flow:
                    f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(self.flow_file))
            replaced = True
            try:
                dir_fd = os.open(str(target_dir), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except Exception:
                pass
            self._appended_lines = 0
            self._last_compact_at = time.monotonic()
        except Exception as e:
            Logger.error(f"[flow] compact 失败: {e}")
        finally:
            if not replaced:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass



    async def flush(self):
        async with self._write_lock:
            if self._append_fh is not None:
                try:
                    self._append_fh.flush()
                except Exception:
                    pass

    async def close(self):
        async with self._write_lock:
            if self._append_fh is not None:
                try:
                    self._append_fh.close()
                except Exception:
                    pass
                self._append_fh = None
                self._append_fh_path = None

    # ============================================================
    # 写入：分配编号
    # ============================================================

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
            try:
                await self._append_line_locked(entry)
            except Exception as e:
                Logger.error(f"保存对话失败（降级内存态）: {e}")
                self._appended_lines = max(self._appended_lines, _COMPACT_MAX_LINES)
        await _update_conversation_index(self.user_id, self.session_id, list(self._flow))

    async def append_ai(self, think: str, call: str, content: str = "", args: dict = None, retain: int = 0):
        if is_llm_error_string(content):
            return
        if call == "use_tool":
            c = self._next_chain()
            s = 0
        else:
            c = 0
            s = 0
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
            "step_no": s,
        }
        self._flow.append(entry)
        async with self._write_lock:
            try:
                await self._append_line_locked(entry)
            except Exception as e:
                Logger.error(f"保存对话失败（降级内存态）: {e}")
                self._appended_lines = max(self._appended_lines, _COMPACT_MAX_LINES)
        await _update_conversation_index(self.user_id, self.session_id, list(self._flow))

    async def append_system(self, action: str, result_think: str, detail: dict = None,
                            extra: dict = None, retain: int = 1):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "from": "system",
            "action": action,
            "result": result_think,
            "detail": detail or {},
            "retain": retain,
            "round_no": self._round_counter,
            "chain_no": self._chain_counter,
            "step_no": self._next_step(),
        }
        if extra:
            entry["extra"] = extra
        self._flow.append(entry)
        async with self._write_lock:
            try:
                await self._append_line_locked(entry)
            except Exception as e:
                Logger.error(f"保存对话失败（降级内存态）: {e}")
                self._appended_lines = max(self._appended_lines, _COMPACT_MAX_LINES)
        await _update_conversation_index(self.user_id, self.session_id, list(self._flow))

    # ============================================================
    # 异步回调：追加新 ##，带 related，不碰已闭合 ##
    # ============================================================

    def _find_chain_by_trace(self, trace_id: str) -> str:
        """从 trace_id 反查提交分身的 ## 编号。"""
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
            tid = ""
            if isinstance(data, dict):
                tid = data.get("trace_id", "") or ""
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

    async def append_async_callback(self, trace_id: str, payload: dict, trace_meta: dict = None):
        """回调到达：追加新 ##（必要时开新 #N），带 related。"""
        related = self._find_chain_by_trace(trace_id)

        # 当前轮已结束（有 reply）→ 开新轮 + 虚拟 user
        if self._round_counter > 0 and self._current_round_has_reply():
            await self.append_user("（系统：异步分身回调到达）")

        # 新 ##
        c = self._next_chain()
        r = self._round_counter

        # ###1 输入
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
        # ###2 输出
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

    # ============================================================
    # manage_flow —— 只控制不查询
    # ============================================================

    @staticmethod
    def _norm_index(idx: str) -> str:
        return str(idx or "").lstrip("#").strip()

    def _match_entries(self, idx: str) -> List[dict]:
        s = self._norm_index(idx)
        if not s:
            return []
        parts = s.split(".")
        try:
            r = int(parts[0])
            c = int(parts[1]) if len(parts) > 1 else None
            sd = int(parts[2]) if len(parts) > 2 else None
        except (ValueError, IndexError):
            return []
        out = []
        for e in self._flow:
            if (e.get("round_no") or 0) != r:
                continue
            if c is not None and (e.get("chain_no") or 0) != c:
                continue
            if sd is not None and (e.get("step_no") or 0) != sd:
                continue
            out.append(e)
        return out



    def _is_expanded(self, idx: str) -> bool:
        s = self._norm_index(idx)
        exp = self._expanded.get(s)
        return bool(exp) and exp > self._current_turn

    def _is_ignored(self, idx: str) -> bool:
        s = self._norm_index(idx)
        exp = self._ignored.get(s)
        return bool(exp) and exp > self._current_turn

    # ============================================================
    # 渲染辅助
    # ============================================================

    @staticmethod
    def _fmt_time(ts: str, prev_ts: str = "") -> str:
        if not ts:
            return ""
        try:
            dt = datetime.fromisoformat(ts)
        except Exception:
            return ""
        if prev_ts:
            try:
                prev_dt = datetime.fromisoformat(prev_ts)
                if dt.date() == prev_dt.date():
                    return dt.strftime("%H:%M")
            except Exception:
                pass
        return dt.strftime("%m-%d %H:%M")

    def _split_rounds(self) -> List[List[dict]]:
        """按 user 切轮。无编号的 user 也作为轮起点（但轮内无编号 entry 渲染时跳过）。"""
        rounds: List[List[dict]] = []
        cur: List[dict] = []
        for e in self._flow:
            if e.get("from") == "user" and cur:
                rounds.append(cur)
                cur = []
            cur.append(e)
        if cur:
            rounds.append(cur)
        return rounds

    def _group_chains(self, entries: List[dict]) -> Dict[int, List[dict]]:
        """按 chain_no 分组。chain_no=0 的不进分组。"""
        out: Dict[int, List[dict]] = {}
        for e in entries:
            c = e.get("chain_no") or 0
            if not c:
                continue
            out.setdefault(c, []).append(e)
        return out

    def _chain_anchor(self, target: str, action: str, params: dict,
                    chain_entries: List[dict] = None) -> str:
        """从 params 抽对象锚点，绝不 dump JSON。"""
        if not isinstance(params, dict):
            params = {}
        t = target or ""
        a = action or ""

        if "file_utils_api" in t:
            path = params.get("path", "") or ""
            if a in ("edit_file", "patch"):
                old_first = (params.get("old", "") or "").split("\n")[0][:30]
                return f'{path}, "{old_first}"' if old_first else path
            return path

        if "code_runner" in t:
            code = params.get("code", "") or ""
            # ★ 兜底：从 chain_entries 的 call_start.detail 拿
            if not code and chain_entries:
                for e in chain_entries:
                    if e.get("action") == "call_start":
                        d = e.get("detail") or {}
                        code = d.get("code") or (d.get("params") or {}).get("code") or ""
                        break
            if code:
                # 首行如果只是 "async def execute(...)"，跳到下一行有意义的
                first = ""
                for ln in code.split("\n"):
                    ln = ln.strip()
                    if ln and not ln.startswith("async def execute"):
                        first = ln[:50]
                        break
                if not first:
                    first = code.split("\n")[0].strip()[:50]
                return f'"{first}"'
            return ""

        if "contract_agent" in t:
            return params.get("plugin", "") or ""

        if "save_blocks" in t:
            n = len(params.get("blocks", []) or [])
            return f"{n} blocks"

        if "cogitor" in t:
            q = params.get("query", "") or params.get("keywords", "")
            return f'"{str(q)[:40]}"' if q else ""

        if t == "async_callback":
            tr = params.get("trace_id", "") or ""
            return f"trace={tr[:12]}" if tr else ""

        for k, v in params.items():
            if k in ("content", "code", "diff", "html", "text", "old", "new"):
                continue
            if isinstance(v, str) and v and len(v) <= 60:
                return v
        return ""

    def _chain_think(self, chain_entries: List[dict]) -> str:
        """链的 think：取 ai.use_tool 的 think，或 call_start.detail.think。"""
        for e in chain_entries:
            if e.get("from") == "ai" and e.get("call") == "use_tool":
                th = (e.get("think") or "").strip()
                if th:
                    return th[:180]
            if e.get("action") == "call_start":
                th = (e.get("detail") or {}).get("think", "") or ""
                if th:
                    return th[:180]
        return ""

    def _chain_target_action(self, chain_entries: List[dict]) -> Tuple[str, str]:
        for e in chain_entries:
            if e.get("action") == "call_start":
                d = e.get("detail") or {}
                t = d.get("target", "") or ""
                p = d.get("params") or {}
                a = p.get("action", "") if isinstance(p, dict) else ""
                return t, a
            if e.get("from") == "ai" and e.get("call") == "use_tool":
                args = e.get("args") or {}
                return args.get("target", "") or "", args.get("action", "") or ""
        return "", ""

    def _chain_params(self, chain_entries: List[dict]) -> dict:
        for e in chain_entries:
            if e.get("action") == "call_start":
                p = (e.get("detail") or {}).get("params") or {}
                return p if isinstance(p, dict) else {}
            if e.get("from") == "ai" and e.get("call") == "use_tool":
                p = e.get("args") or {}
                return p if isinstance(p, dict) else {}
        return {}

    def _chain_status(self, chain_entries: List[dict]) -> str:
        for e in chain_entries:
            if e.get("action") == "call_end":
                res = (e.get("detail") or {}).get("result") or {}
                if isinstance(res, dict):
                    return "ok" if res.get("ok", True) else "failed"
                return "ok"
            if e.get("action") == "call_error":
                return "failed"
        return "pending"

    def _chain_error_hint(self, chain_entries: List[dict]) -> str:
        for e in chain_entries:
            if e.get("action") == "call_error":
                return str((e.get("detail") or {}).get("error", ""))[:80]
            if e.get("action") == "call_end":
                res = (e.get("detail") or {}).get("result") or {}
                if isinstance(res, dict) and not res.get("ok", True):
                    return str(res.get("error", ""))[:80]
        return ""

    def _round_status(self, entries: List[dict]) -> Tuple[str, str]:
        """返回 (icon, reason)。有 reply → ("", "")；否则给状态。"""
        has_reply = any(
            e.get("from") == "ai" and e.get("call") == "reply" and (e.get("content") or "").strip()
            for e in entries
        )
        if has_reply:
            return "", ""

        # 找 interrupted / call_error
        for e in reversed(entries):
            a = e.get("action", "")
            if a == "interrupted":
                d = e.get("detail") or {}
                reason = d.get("reason", "") or ""
                if reason == "network":
                    return "⚠️", "网络中断"
                if reason == "user_interrupt":
                    return "⚠️", "用户中断"
                if reason == "llm_error":
                    return "⚠️", "LLM 调用失败"
                if reason == "max_depth":
                    return "⚠️", "达到递归深度上限"
                pending = d.get("pending") or {}
                tgt = pending.get("target", "") if isinstance(pending, dict) else ""
                return "⚠️", f"用户中断（中断时在调 {tgt}）" if tgt else "用户中断"
            if a == "call_error":
                err = str((e.get("detail") or {}).get("error", ""))[:80]
                return "❌", f"工具失败: {err}"
        # 有工具链但无 reply
        has_chain = any(e.get("chain_no") for e in entries)
        if has_chain:
            return "⚠️", "未生成最终回复"
        # 只有 user
        return "⏳", "处理中"

    # ============================================================
    # 渲染：主入口
    # ============================================================




    def get_context(self) -> str:
        if not self._flow:
            return "（暂无对话记录）"

        numbered = [e for e in self._flow if (e.get("round_no") or 0) > 0]
        if not numbered:
            return "（暂无对话记录）"

        rounds = self._split_rounds()
        rounds = [[e for e in r if (e.get("round_no") or 0) > 0] for r in rounds]
        rounds = [r for r in rounds if r]
        if not rounds:
            return "（暂无对话记录）"

        total = len(rounds)
        total_chains = sum(len(self._group_chains(r)) for r in rounds)
        last_idx = total - 1

        # ── 1. 确定显示的轮次 ──
        shown: set = set(range(max(0, total - BASE_ROUNDS), total))
        current_user = ""
        for e in reversed(self._flow):
            if e.get("from") == "user":
                current_user = e.get("content", "") or ""
                break
        for n in self._detect_mentioned_rounds(current_user, rounds):
            if 1 <= n <= total:
                shown.add(n - 1)
        for key in list(self._expanded.keys()):
            if self._is_expanded(key) and "." not in key:
                try:
                    n = int(key)
                    if 1 <= n <= total:
                        shown.add(n - 1)
                except ValueError:
                    pass

        # ── 2. 头部 ──
        lines = [f"# 信息流（共 {total} 轮 / {total_chains} 链；显示最近 {len(shown)} 轮）"]
        omitted = [i for i in range(total) if i not in shown]
        if omitted:
            nums = [f"#{i + 1}" for i in omitted[:12]]
            more = "..." if len(omitted) > 12 else ""
            lines.append(
                f"> 📦 已省略 {len(omitted)} 轮（{', '.join(nums)}{more}），"
                f'需要时 expand("#N") 展开，或 search_memory_agent 召回'
            )
        lines.append("")

        # ── 3. 逐轮渲染 ──
        prev_ts = ""
        for r_idx in sorted(shown):
            entries = rounds[r_idx]
            r_no = entries[0].get("round_no") or (r_idx + 1)
            is_last = (r_idx == last_idx)
            is_prev = (r_idx == last_idx - 1)

            # 3.1 #N 头（user + reply + 标识 + 引用块）
            lines.extend(self._render_round_header(entries, prev_ts))
            for e in entries:
                if e.get("from") == "user":
                    prev_ts = e.get("timestamp", "") or prev_ts
                    break
            lines.append("")

            chains = self._group_chains(entries)
            if not chains:
                continue

            # 整轮被折叠（历史轮 / 更早的轮）
            if not is_last and not is_prev and not self._is_expanded(f"#{r_no}"):
                type_counts: Dict[str, int] = {}
                chain_nos = sorted(chains.keys())
                for c_no in chain_nos:
                    t, a = self._chain_target_action(chains[c_no])
                    label = (a or t or "?").split("/")[-1]
                    type_counts[label] = type_counts.get(label, 0) + 1
                type_hint = ", ".join(f"{k}×{v}" for k, v in type_counts.items())
                lines.append(
                    f"> 📦 {len(chains)} 条工具链"
                    f"（##{r_no}.{chain_nos[0]} ~ ##{r_no}.{chain_nos[-1]}）折叠中"
                    + (f"：{type_hint}" if type_hint else "")
                    + f'，expand("#{r_no}") 展开'
                )
                lines.append("")
                continue

            # 当前轮 / 上一轮 / 展开轮：逐 ## 渲染
            chain_nos = sorted(chains.keys())
            if is_prev and not self._is_expanded(f"#{r_no}"):
                show_chains = set(chain_nos[-BASE_CHAINS:]) if BASE_CHAINS > 0 else set()
            else:
                show_chains = set(chain_nos)

            merged = self._merge_consecutive_failures(r_no, chain_nos, chains)

            for item in merged:
                if item["type"] == "group":
                    first, last = item["first"], item["last"]
                    if not any(c in show_chains for c in item["chains"]):
                        lines.append(
                            f"> 📦 ##{r_no}.{first}~##{r_no}.{last} "
                            f"（{item['count']} 次失败）折叠中"
                        )
                        continue
                    lines.append(
                        f"## {r_no}.{first}~## {r_no}.{last} "
                        f"❌×{item['count']} {item['target']}（{item['err']}）折叠中"
                    )
                    lines.append(
                        f'> expand({", ".join(f"##{r_no}.{c}" for c in item["chains"])})'
                    )
                    continue

                c_no = item["chain_no"]
                chain_entries = chains[c_no]
                chain_idx = f"##{r_no}.{c_no}"

                if self._is_ignored(chain_idx):
                    lines.append(f"##{r_no}.{c_no} 🚫 已忽略")
                    continue

                # 不在 show_chains 且不是当前轮 → 一行折叠
                if c_no not in show_chains and not is_last:
                    t, a = self._chain_target_action(chain_entries)
                    status = self._chain_status(chain_entries)
                    icon = {"ok": "✅", "failed": "❌", "pending": "⏳"}.get(status, "")
                    label = f"{t}.{a}" if a else (t or "?")
                    hint = self._chain_result_hint(chain_entries)
                    lines.append(f"> 📦 ##{r_no}.{c_no} {icon} {label}{hint} 折叠中")
                    continue

                # ## 头（问题 4：加结果摘要）
                t, a = self._chain_target_action(chain_entries)
                p = self._chain_params(chain_entries)
                anchor = self._chain_anchor(t, a, p, chain_entries)
                think = self._chain_think(chain_entries)
                status = self._chain_status(chain_entries)
                label = f"{t}.{a}" if a else (t or "?")
                icon = {"ok": "✅", "failed": "❌", "pending": "⏳"}.get(status, "")
                hint = self._chain_result_hint(chain_entries)
                label = f"{t.split('/')[-1]}.{a}" if a else t.split('/')[-1]
                head = f"## {r_no}.{c_no} {icon} 🤖 AI 决策 → {label}"
                if anchor:
                    head += f"({anchor})"
                if hint:
                    head += hint
                if think:
                    head += f" — 💭 {think}"
                lines.append(head)

                # ### 展开
                show_steps = is_last or self._is_expanded(chain_idx)
                if show_steps:
                    step_no = 0
                    for se in chain_entries:
                        # 问题 1：use_tool 不单独渲染（内容与 call_start 重复）
                        if se.get("from") == "ai" and se.get("call") == "use_tool":
                            continue
                        step_no += 1
                        lines.extend(self._render_step(se, r_no, c_no, step_no))
                else:
                    # 步数 = 除 use_tool 之外的 entry 数
                    n_steps = sum(
                        1 for se in chain_entries
                        if not (se.get("from") == "ai" and se.get("call") == "use_tool")
                    )
                    lines.append(
                        f"> 📦 {n_steps} 步"
                        f"（###{r_no}.{c_no}.1 ~ ###{r_no}.{c_no}.{n_steps}）折叠中"
                        f'，expand("{chain_idx}") 展开'
                    )

            lines.append("")

        # ── 4. 操作提示 + 阈值提示 ──
        lines.append("## 操作")
        lines.append('manage_flow(collapse=[...], ignore=[...], expand=[...])')
        lines.append("编号：#N 整轮、##N.M 工具链、###N.M.K 单步")
        lines.append(f"基线：最近 {BASE_ROUNDS} 轮 #N，上一轮 ## 展开 {BASE_CHAINS} 条，当前轮 ## 和 ### 全量。")

        context_len = sum(len(x) for x in lines)
        if context_len > FORCE_FOLD_THRESHOLD:
            lines.append("")
            lines.append(
                f"⚠️ **当前信息流已达 {context_len} 字符（阈值 {FORCE_FOLD_THRESHOLD}）**。\n"
                f"你可以折叠，但**遵守硬规则**：\n"
                f"  - ✅ 只 `collapse(\"##N.M\")` 折叠重复读取的工具链\n"
                f"  - ✅ 只 `ignore(\"###N.M.K\")` 忽略某一步的具体输入输出\n"
                f"  - ❌ **不要 `ignore` / `collapse` 整轮 `#N`** —— `#N` 的 reply 是上下文"
            )

        return "\n".join(lines)
    # ============================================================
    # get_context 的辅助方法
    # ============================================================
    def _render_step(self, se: dict, r_no: int, c_no: int, step_no: int,
                     indent: str = "") -> List[str]:
        """渲染一个 ### 步骤。

        问题 1：use_tool 与 call_start 内容几乎一样，### 里不再重复渲染 use_tool。
        use_tool 的 think 已在 ## 头（💭），args 已在 call_start 的 params。
        """
        # ★ 跳过 ai.use_tool（内容与 call_start 重复）
        if se.get("from") == "ai" and se.get("call") == "use_tool":
            return []

        act = se.get("action", "") or (se.get("call", "") or "")
        out = [f"{indent}### {r_no}.{c_no}.{step_no} {act}"]

        if se.get("from") == "system":
            payload = se.get("detail") or {}
        else:
            payload = {
                "think": se.get("think", ""),
                "call": se.get("call", ""),
                "content": se.get("content", ""),
                "args": se.get("args", {}),
            }

        try:
            j = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        except Exception:
            j = str(payload)
        out.append(f"{indent}```json")
        out.append(j)
        out.append(f"{indent}```")
        return out


    def _is_discovery(self, target: str, action: str) -> bool:
        t = (target or "").lower()
        a = (action or "").lower()
        if any(x in t for x in self._DISCOVERY_TARGETS):
            return True
        if a in self._DISCOVERY_ACTIONS:
            return True
        return False

    @staticmethod
    def _short_size(n) -> str:
        try:
            n = int(n)
        except Exception:
            return str(n)
        if n < 1024:
            return f"{n}B"
        if n < 1024 * 1024:
            return f"{n / 1024:.1f}KB"
        return f"{n / 1024 / 1024:.1f}MB"

    def _chain_result_hint(self, chain_entries: List[dict]) -> str:
        """从 call_end.result 抽"结果摘要"。

        发现类（cogitor / registry / search / list_*）→ 只给"条数"。
        读 / 写 / 执行 / 错误 → 给关键字段。
        """
        target, action = self._chain_target_action(chain_entries)
        is_disc = self._is_discovery(target, action)

        # ── E. 错误优先 ──
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

            data = res.get("data")

            # ══════════════════════════════════════════
            # 发现类：只给"条数"，绝不列字段内容
            # ══════════════════════════════════════════
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
                    if n is None:
                        for k in ("apis", "items", "matches", "plugins",
                                    "results", "lines", "skills", "frontend"):
                            v = data.get(k)
                            if isinstance(v, list):
                                n = len(v)
                                break
                if n is None:
                    # 顶层 total/count
                    for k in ("total", "count"):
                        v = res.get(k)
                        if isinstance(v, (int, float)):
                            n = v
                            break
                return f" → {n}条" if n is not None else " → ✅"

            # ══════════════════════════════════════════
            # 非发现类：按返回结构给"关键字段"
            # ══════════════════════════════════════════

            # F. summary 第一行
            s = str(res.get("summary", "") or "")
            if s:
                first = s.split("\n")[0].strip()
                if first:
                    return " → " + first[:60]

            if not isinstance(data, dict):
                if isinstance(data, list):
                    return f" → {len(data)}条"
                if data is not None:
                    return " → " + str(data)[:60]
                continue

            # A. 读文件
            content = data.get("full_content") or data.get("content")
            if isinstance(content, str) and content:
                size = data.get("size") or len(content)
                return f" → {self._short_size(size)} 文本"
            lines = data.get("lines")
            if isinstance(lines, list):
                rng = ""
                if "start" in data and "end" in data:
                    rng = f" L{data['start']}-{data['end']}"
                return f" → {len(lines)}行{rng}"

            # B. 写文件
            path = data.get("path") or data.get("file_path")
            if path:
                bits = [str(path).replace("\\", "/").split("/")[-1]]
                if "size" in data:
                    bits.append(self._short_size(data["size"]))
                v = data.get("validation")
                if isinstance(v, dict):
                    bits.append("✅" if v.get("ok") else "❌校验")
                elif data.get("written") is True:
                    bits.append("已写")
                return " → " + " / ".join(bits)

            # D. 执行结果
            stderr = data.get("stderr")
            if isinstance(stderr, str) and stderr.strip():
                return " → ❌ " + stderr.strip().split("\n")[0][:60]
            stdout = data.get("stdout")
            if isinstance(stdout, str) and stdout.strip():
                return " → " + stdout.strip().split("\n")[0][:60]

            # G. 兜底：抽 1~2 个标量
            scalars = []
            for k, v in data.items():
                if isinstance(v, (str, int, float, bool)) and v not in ("", 0, None, False):
                    sv = str(v)
                    if len(sv) > 30:
                        sv = sv[:30] + "…"
                    scalars.append(f"{k}={sv}")
                if len(scalars) >= 2:
                    break
            if scalars:
                return " → " + " ".join(scalars)

        return ""
    def _render_round_header(self, entries: List[dict], prev_ts: str) -> List[str]:
        """渲染 #N 头：时间 + 👤 user + 🤖 reply。

        问题 2：user / reply 之间加空行 + 标识。
        问题 3：reply 用引用块（> 每行）包裹，防止 reply 的 md 列表破坏外层 md。
        """
        out: List[str] = []
        r_no = entries[0].get("round_no") or 0

        user_ts = ""
        user_content = ""
        reply_content = ""
        for e in entries:
            if e.get("from") == "user" and not user_content:
                user_content = e.get("content", "") or ""
                user_ts = e.get("timestamp", "") or ""
            if e.get("from") == "ai" and e.get("call") == "reply" and (e.get("content") or ""):
                reply_content = e.get("content", "") or ""

        t_str = self._fmt_time(user_ts, prev_ts)
        header = f"# {r_no}"
        if t_str:
            header += f" [{t_str}]"
        out.append(header)
        out.append("")


        # ── 用户消息 ──
        if user_content:
            uid = self.user_id or ""
            uid_tag = f"({uid})" if uid else ""
            out.append(f"👤 用户{uid_tag}：")
            for ln in user_content.split("\n"):
                out.append(f"> {ln}" if ln else ">")
            out.append("")

        # ── AI 回复 ──
        status_icon, status_reason = self._round_status(entries)
        if reply_content:
            out.append("🤖 AI：")
            for ln in reply_content.split("\n"):
                out.append(f"> {ln}" if ln else ">")
        elif status_icon == "⏳":
            out.append("⏳ 本轮处理中...")
        elif status_icon:
            out.append(f"{status_icon} 本轮未正常结束（{status_reason}）")
        return out

    def _detect_mentioned_rounds(self, user_msg: str, rounds: List[List[dict]]) -> List[int]:
        """从用户消息里检测提到的轮次编号，返回 [N, ...]（1-based）。"""
        if not user_msg:
            return []
        found: List[int] = []
        total = len(rounds)

        # #3 / 第3轮 / 第三轮
        for m in re.finditer(r"#(\d+)|第\s*(\d+)\s*轮|第([一二三四五六七八九十]+)轮", user_msg):
            n = 0
            if m.group(1):
                n = int(m.group(1))
            elif m.group(2):
                n = int(m.group(2))
            elif m.group(3):
                n = _cn_to_int(m.group(3))
            if n and 1 <= n <= total and n not in found:
                found.append(n)

        # 关键词模糊匹配：如果用户提到某轮 user 消息的前几个字
        # （可选，简单实现：跳过，避免误判）
        return found

    def _merge_consecutive_failures(self, r_no: int, chain_nos: List[int], chains: Dict[int, List[dict]]) -> List[dict]:
        """连续 3+ 同 target + failed 的链合并成一行。返回有序 item 列表。"""
        items: List[dict] = []
        i = 0
        n = len(chain_nos)
        while i < n:
            c = chain_nos[i]
            entries = chains[c]
            status = self._chain_status(entries)
            t, _ = self._chain_target_action(entries)
            if status != "failed":
                items.append({"type": "single", "chain_no": c})
                i += 1
                continue
            # 找连续同 target failed
            group = [c]
            j = i + 1
            while j < n:
                c2 = chain_nos[j]
                e2 = chains[c2]
                s2 = self._chain_status(e2)
                t2, _ = self._chain_target_action(e2)
                if s2 == "failed" and t2 == t:
                    group.append(c2)
                    j += 1
                else:
                    break
            if len(group) >= 3:
                items.append({
                    "type": "group",
                    "chains": group,
                    "first": group[0],
                    "last": group[-1],
                    "count": len(group),
                    "target": t,
                    "err": self._chain_error_hint(chains[group[0]]),
                })
            else:
                for c3 in group:
                    items.append({"type": "single", "chain_no": c3})
            i = j
        return items

    # ============================================================
    # 中断标记（供外部调用；不删 flow）
    # ============================================================

    async def mark_interrupted(self, reason: str, error: str = "", pending: dict = None) -> dict:
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
            and any(
                str((e.get("detail") or {}).get("target", "")).startswith(t)
                for t in SIDE_EFFECT_TARGETS
            )
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
# session 锁 / flow 缓存
# ============================================================
_last_ctx_k: Dict[str, float] = {}
_session_processing_locks: Dict[str, asyncio.Lock] = {}
_session_flows: Dict[str, InformationFlow] = {}


def _get_processing_lock(session_id: str) -> asyncio.Lock:
    if session_id not in _session_processing_locks:
        _session_processing_locks[session_id] = asyncio.Lock()
    return _session_processing_locks[session_id]


def _get_or_create_flow(user_id: str, session_id: str) -> InformationFlow:
    key = f"{user_id}:{session_id}"
    if key not in _session_flows:
        _session_flows[key] = InformationFlow(user_id, session_id)
    return _session_flows[key]


# ============================================================
# WS 推送
# ============================================================

async def _push_progress(agent,user_id, session_id, step, msg, extra=None):
    data = {"type": "progress", "session_id": session_id, "step": step, "msg": msg}
    if extra: data.update(extra)
    await agent.system.call(core.Envelop(
        sender="builtins/agents/main_agent",
        receiver="os/_websocket",
        payload={
            "action": "push",
            "channel_id": f"pa_{user_id}",   # ← user_id
            "data": data,
        }
    ))


async def _push_chat(agent, user_id: str, session_id: str, content: str):
    try:
        await agent.system.call(core.Envelop(
            sender="builtins/agents/main_agent",
            receiver="os/_websocket",
            payload={
                "action": "push",
                "channel_id": f"pa_{user_id}",
                "data": {"type": "chat", "session_id": session_id, "content": content},
            }
        ))
    except Exception as e:
        Logger.warn(f"[WS] 推送消息失败: {e}")


async def _push_stream(agent, user_id: str, session_id: str, chunk: str):
    try:
        await agent.system.call(core.Envelop(
            sender="builtins/agents/main_agent",
            receiver="os/_websocket",
            payload={
                "action": "push",
                "channel_id": f"pa_{user_id}",
                "data": {"type": "summary_stream", "session_id": session_id, "chunk": chunk},
            }
        ))
    except Exception as e:
        Logger.warn(f"[WS] 流式推送失败: {e}")

# ============================================================
# 流式推送：合并 + 异步
# ============================================================
_STREAM_BUFFERS: Dict[str, list] = {}       # session_id -> [chunk, ...]
_STREAM_FLUSH_TASKS: Dict[str, asyncio.Task] = {}
_STREAM_FLUSH_INTERVAL = 0.1                 # 100ms 合并一次
_STREAM_BUFFER_MAX = 64                      # buffer 超过 64 个 chunk 强制 flush


async def _flush_stream_buffer(agent, user_id:str,session_id: str):
    """把 buffer 里的 chunk 合并成一次推送。"""
    buf = _STREAM_BUFFERS.get(session_id)
    if not buf:
        return
    text = "".join(buf)
    buf.clear()
    if not text:
        return
    try:
        await _push_stream(agent, user_id,session_id, text)
    except Exception as e:
        Logger.warn(f"[WS] flush 推送失败: {e}")


async def _flush_loop(agent, user_id:str,session_id: str):
    """后台任务：每 100ms flush 一次。"""
    try:
        while True:
            await asyncio.sleep(_STREAM_FLUSH_INTERVAL)
            await _flush_stream_buffer(agent, user_id,session_id)
    except asyncio.CancelledError:
        # 任务被取消时，最后 flush 一次
        await _flush_stream_buffer(agent, user_id,session_id)
        raise


def _buffer_stream_chunk(agent, user_id:str,session_id: str, chunk: str):
    """把一个 chunk 塞进 buffer。超过阈值时立即 flush。"""
    if not chunk:
        return
    buf = _STREAM_BUFFERS.setdefault(session_id, [])
    buf.append(chunk)

    # 超过阈值：立即异步 flush（不阻塞调用方）
    if len(buf) >= _STREAM_BUFFER_MAX:
        asyncio.create_task(_flush_stream_buffer(agent, user_id,session_id))

    # 确保 flush loop 在跑
    task = _STREAM_FLUSH_TASKS.get(session_id)
    if task is None or task.done():
        _STREAM_FLUSH_TASKS[session_id] = asyncio.create_task(_flush_loop(agent, user_id,session_id))


async def _stop_stream_flush(session_id: str):
    """停止 flush loop，并最后 flush 一次。"""
    task = _STREAM_FLUSH_TASKS.pop(session_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass




# ============================================================
# TOOL_SCHEMAS
# ============================================================
MANAGE_FLOW_SCHEMA = {
    "type": "function",
    "function": {
        "name": "manage_flow",
        "description": (
            "控制信息流的显示状态（展开/折叠/忽略），只改变下一轮 context 的渲染，不返回内容。\n"
            "\n"
            "编号：#N 整轮、##N.M 工具链、###N.M.K 单步。\n"
            "\n"
            "## 语义\n"
            "  collapse([...])：折叠成一行。对 ##N.M 折叠成锚点行（结果摘要仍在），对 #N 折叠整轮。\n"
            "  ignore([...])：本轮完全不渲染。**只对 ###N.M.K 用**，不要对 ##N.M 或 #N 用。\n"
            "  expand([...])：展开历史轮 / 折叠的 ##。\n"
            "\n"
            "## 硬规则（违反会丢上下文）\n"
            "  1. **绝对不要 ignore / collapse 整轮 #N**。\n"
            "     #N 的 reply 是结论 / 排查结果 / 方法论，是后续轮的上下文。\n"
            "     #N 默认已经折叠成「user + reply + 一行链计数」，本身就很短，不需要再折叠。\n"
            "  2. **只 collapse 重复读取的 ##N.M**。\n"
            "     同一 target+action+params 被读过多次，保留最新一次，旧的 collapse。\n"
            "  3. **ignore 只用于 ###N.M.K**。\n"
            "     某一步的具体输入输出确认无用（例如超长 stdout 的中间部分），才 ignore。\n"
            "  4. **只有 context_len > 200000 时才需要主动折叠**。\n"
            "     context 末尾会打「⚠️ 当前信息流已达 X 字符」，X > 200000 才动手。\n"
            "     没看到这个警告，不要折叠。\n"
            "\n"
            "## 默认状态（不需要调用 manage_flow 的情况）\n"
            "  - 历史轮 #N 已折叠成「user + reply + 一行链计数」\n"
            "  - 上一轮的 ## 展开最近 1 条\n"
            "  - 当前轮的 ## 和 ### 全量展开\n"
            "  这个默认状态已经足够，绝大多数轮不需要调 manage_flow。\n"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "collapse": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "折叠的编号。推荐只放 ##N.M（重复读取的链）。不要放 #N。"
                },
                "ignore": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "完全不渲染的编号。只放 ###N.M.K。不要放 #N 或 ##N.M。"
                },
                "expand": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "展开的编号。可以放 #N（整轮展开）、##N.M（展开单条链的 ###）。"
                },
                "ttl": {
                    "type": "integer",
                    "default": 3,
                    "minimum": 1,
                    "maximum": 20,
                    "description": "collapse / expand 持续几轮，默认 3。ignore 固定 1 轮。"
                }
            },
            "required": []
        }
    }
}
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "reply",
            "description": (
                "向用户回复最终结果或澄清问题。任务完成、需要用户确认、或无法继续时使用。"
                "⚠️ 不要用 reply 说「我要读文件 / 我要查一下 / 我要继续」——"
                "这些是「下一步动作」，应该用 use_tool / manage_flow直接执行，不是回复用户。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "think": {
                        "type": "string",
                        "description": "本轮推理，100 字以内，写下一步做什么、为什么。"
                    },
                    "content": {
                        "type": "string",
                        "description": "给用户的回复内容。"
                    }
                },
                "required": ["content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "use_tool",
            "description": (
                "调用系统内的一个插件。一次只调一个，拿到结果后再决定下一步。"
                 "⚠️ 文件内容（HTML/JS/CSS/代码）必须写在 plugin_args 里，"
                 "不要写在 reasoning 里,写长内容会被 max_tokens 截断，导致 tool_call 丢失。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "think": {
                        "type": "string",
                        "description": "一句话说明为什么调用这个工具。小于180字，必须输出！"
                    },
                    "target": {
                        "type": "string",
                        "description": "插件路由，如 builtins/tools/file_utils_api、builtins/tools/code_runner、builtins/tools/save_blocks。"
                    },
                    "action": {
                        "type": "string",
                        "description": "插件 action，必填，无 action 的插件传入none。不确认用builtins/toos/contract_agent 查询"
                    },
                    "plugin_args": {
                        "type": "object",
                        "description": "插件的全部参数，直接平铺在这里，没有子层，不要再嵌套 params。例如：{\"path\": \"www/index.html\"} 或 {\"code\": \"...\", \"packages\": []} 或 {\"text\": \"...\"}。",
                        "additionalProperties": True
                    },
                    "retain": {
                        "type": "integer",
                        "description": "信息流保留轮数，1-10。一次性操作 1，写文件 3，读文件 5，任务收尾 3-5。",
                        "minimum": 1,
                        "maximum": 10
                    }
                },
                "required": ["target", "plugin_args"]
            }
        }
    },
    MANAGE_FLOW_SCHEMA
]



# ============================================================
# 提示词
# ============================================================

SYSTEM_PROMPT = """你是 BIO-bk-1，自举式 AI 助手。Session: {{session_id}}

你可以调度、创建、修复、删除插件来实现自我迭代；基于经验背包复用过往能力。
你不是工具，你是系统的主人。

# 工作方式
自递归 Agent：每轮调用一个 tool，系统执行后结果注入信息流，你再次决策，直到任务完成。
复杂任务拆成多步，每轮执行一步。

# 你只有两个 tool
1. reply —— 回复用户：任务完成、需要确认、无法继续时用。
2. use_tool —— 调用插件：执行动作、读写文件、跑代码、造应用、查系统。
3. manage_flow —— 控制信息流：collapse（收起）/ ignore（关闭）/ expand（展开）。

一次只调一个。拿到结果再决定下一步。

# 元能力
- cogitor —— 发现能力：search / list_plugins / get_map
- contract_agent —— 拿插件用法：get(plugin) 返回 {contract, usage, source}
- file_utils_api —— 读写文件 / 读源码
- code_runner —— 执行代码（沙箱禁令：不要 print / time.sleep / input / while True）

# 任务路标
- 造应用 → save_blocks（用法靠 contract_agent.get 拿）
- 实时推送 → os/_websocket
- 定时 → os/_cron
- 持久化 → JSON 文件 / os/_db

# 铁律
- 一次只调一个 tool，拿到结果再下一步。
- reply 时不要在 content 里夹 JSON。
- 连续 2 次同样错误，换思路或告知用户。
- 造应用后检查 loaded 全 true 且 compile_ok: true。
"""



def _get_backpack_file(user_id: str) -> Path:
    return _user_dir(user_id) / "backpack.json"


def _load_backpack(user_id: str) -> dict:
    f = _get_backpack_file(user_id)
    if not f.exists():
        return {"items": []}
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("items"), list):
            return d
    except Exception:
        pass
    return {"items": []}

# ============================================================
# 失败自动记录（插件调用失败 → backpack.json）
# ============================================================

_FAILURE_MAX_ITEMS = 20
_FAILURE_SOFT_FIX = 5          # builtins：踩 5 次标 ⚠️ 需修根
_FAILURE_HARD_FIX = 10         # builtins：踩 10 次标 🛑 必须修根
_FAILURE_APP_SOFT_FIX = 3      # applications：踩 3 次标 ⚠️（应用 bug 更该修）
_FAILURE_APP_HARD_FIX = 5      # applications：踩 5 次标 🛑

_LARGE_FIELD_KEYS = (
    "content", "text", "code", "html", "css", "js",
    "diff", "old", "new", "body", "data",
)


def _save_backpack(user_id: str, data: dict) -> None:
    f = _user_dir(user_id) / "backpack.json"
    tmp = f.with_suffix(f".tmp.{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(f)


def _normalize_error_signature(error_text: str) -> str:
    """归一化错误文本：去时间戳/UUID/数字/路径，取前 60 字。"""
    s = str(error_text or "").strip()
    s = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?", "<TS>", s)
    s = re.sub(r"\b1[6-9]\d{8}(\.\d+)?\b", "<TS>", s)
    s = re.sub(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        "<UUID>", s, flags=re.I,
    )
    s = re.sub(r"(?<![A-Za-z0-9])\d{4,}(?![A-Za-z0-9])", "<NUM>", s)
    s = re.sub(r"[A-Za-z]:\\[^\s'\"]+", "<PATH>", s)
    s = re.sub(r"/(?:[\w.-]+/){2,}[\w.-]+", "<PATH>", s)
    return s[:60]


def _error_signature(error_text: str, error_category: str = "") -> tuple:
    """返回 (error_type, key_phrase[:40])。

    优先级：
    1. error_category（code_runner 等显式分类）
    2. 【错误分类】xxx
    3. 【一句话】xxx + error_category 推断
    4. 异常类（TypeError: xxx）
    5. 业务错误关键词（不存在 / 缺少 / 多个匹配 / 必须是 / 收到的是）
    6. 纯路径
    7. 兜底 BusinessError
    """
    s = str(error_text or "").strip()
    lines = s.split("\n")
    first_line = lines[0] if lines else ""

    # 提取「一句话」（code_runner 专用）
    one_liner = ""
    for ln in lines:
        m = re.search(r"【一句话】\s*(.+)", ln)
        if m:
            one_liner = m.group(1).strip()
            break

    # 提取「原因」首行（code_runner 专用）——用于区分同一分类下的不同根因
    reason = ""
    for ln in lines:
        m = re.search(r"【原因】\s*(.+)", ln)
        if m:
            reason = m.group(1).strip()
            break

    def _compose(liner: str) -> str:
        """一句话 + 原因首行 → 指纹短语。

        同分类不同根因不能合并：只靠 one_liner 会让 TypeError / ZeroDivisionError
        / FileNotFoundError 指纹完全相同，教训回放时拿到的是别的根因的原文。
        """
        p = liner or ""
        if reason:
            r_sig = _normalize_error_signature(reason.split("\n")[0])[:28]
            p = f"{p}: {r_sig}" if p else r_sig
        if not p:
            p = _normalize_error_signature(first_line)[:40]
        return p[:40]

    # 1. error_category 最准
    ec = (error_category or "").strip()
    if ec:
        return ec, _compose(one_liner)

    # 2. 【错误分类】xxx
    m = re.search(r"【错误分类】\s*([\w_]+)", first_line)
    if m:
        return m.group(1), _compose(one_liner)

    # 2.5 框架包装的插件内部异常：Plugin execution error: <inner>
    #     inner 才带真正根因，必须拆开，否则不同异常指纹完全相同
    m = re.match(r"^Plugin execution error:\s*(.+)$", first_line)
    if m:
        inner = m.group(1).strip()
        em = re.match(r"([A-Za-z_][\w.]*(?:Error|Exception))\b", inner)
        et = em.group(1) if em else "PluginException"
        return et, _normalize_error_signature(inner)[:40]

    # 3. 异常类：TypeError: xxx
    m = re.match(r"^([A-Za-z_][\w.]*(?:Error|Exception|Warning)?)\s*[:：]", first_line)
    if m:
        phrase = _normalize_error_signature(first_line)
        phrase = re.sub(r"^\w+\s*[:：]\s*", "", phrase)
        return m.group(1), phrase[:40]

    # 4. 业务错误关键词
    # 4a. 「xxx 不存在」
    if re.search(r"不存在", first_line) or "not found" in first_line.lower():
        # 提取哪个不存在
        m = re.search(r"(?:插件源码|skill 文件|MCP 契约|文件|插件)?\s*不存在[:：]\s*(.+)", first_line)
        phrase = m.group(1).strip() if m else first_line
        # 必须归一化：否则路径里的时间戳/编号不同会被当成不同坑，教训被拆散
        return "NotFound", _normalize_error_signature(phrase)[:40]

    # 4b. 「缺少 xxx 参数」
    m = re.search(r"缺少\s*(\w+)\s*参数", first_line)
    if m:
        return "MissingParam", f"缺少 {m.group(1)} 参数"

    # 4c. 「xxx 必须是 yyy」（后面可能跟「收到的是 / 不能是 / 应为」）
    m = re.search(r"(\w+)\s*必须是\s*([^，,。；;]+)", first_line)
    if m:
        return "ParamFormat", f"{m.group(1)} 必须是 {m.group(2)[:20]}"

    # 4d. 「有多个匹配」
    if "有多个匹配" in first_line or "multiple matches" in first_line.lower():
        m = re.search(r"插件名\s*(\S+)\s*有多个匹配", first_line)
        return "AmbiguousPlugin", f"插件名 {m.group(1)} 有多个匹配" if m else first_line[:40]

    # 5. 纯路径
    if re.match(r"^(?:data|www|plugins|applications|os|builtins)/", first_line) or (
        "/" in first_line and " " not in first_line and len(first_line) < 200
    ):
        return "PathError", first_line[:40]

    # 6. 兜底
    return "BusinessError", _normalize_error_signature(first_line)[:40]


def _failure_fingerprint(target, action, error_text, error_category=""):
    et, phrase = _error_signature(error_text, error_category)
    return f"{target}|{action}|{et}|{phrase}"


def _sanitize_params(params: dict, max_str: int = 80) -> dict:
    """大字段只记元信息（长度/类型），不记内容。"""
    if not isinstance(params, dict):
        return params
    out = {}
    for k, v in params.items():
        if k in _LARGE_FIELD_KEYS:
            if isinstance(v, str):
                out[k] = f"<{len(v)} 字>"
            elif isinstance(v, (list, dict)):
                out[k] = f"<{type(v).__name__}, {len(v)} 项>"
            else:
                out[k] = f"<{type(v).__name__}>"
        elif isinstance(v, str) and len(v) > max_str:
            out[k] = v[:max_str] + f"…（{len(v)} 字）"
        elif isinstance(v, (list, dict)):
            try:
                s = json.dumps(v, ensure_ascii=False, default=str)
            except Exception:
                s = str(v)
            if len(s) > max_str * 2:
                out[k] = f"<{type(v).__name__}, {len(v)} 项>"
            else:
                out[k] = v
        else:
            out[k] = v
    return out


def _extract_fix_from_result(result) -> str:
    """从工具返回里提取「正确做法」（hints / fix / suggestion / usage / next）。"""
    if not isinstance(result, dict):
        return ""
    for key in ("hints", "fix", "suggestion", "usage", "next"):
        v = result.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()[:200]
        if isinstance(v, list) and v:
            return "；".join(str(x) for x in v[:3])[:200]
    return ""


def _infer_fix(params: dict, error_text: str, error_category: str = "") -> str:
    """从 params + error 推断正确做法。"""
    fixes = []
    ec = (error_category or "").upper()

    # ---- code_runner 的 error_category ----
    if ec == "LLM_CODE":
        mr = re.search(r"【原因】\s*(.+)", error_text)
        reason = mr.group(1).strip() if mr else ""
        fixes.append(
            "代码写错了：读 error 的【原因】改代码，重新提交（连续 2 次同错回「无法完成」）"
            + (f"；根因：{reason[:80]}" if reason else "")
        )
    elif ec == "SANDBOX_BLOCK":
        fixes.append("沙箱拦截：换实现（while True → 有限循环；subprocess.Popen → await run_async；同步 requests 打本地 → await fetch_async）")
    elif ec == "RUNTIME_INTERNAL":
        fixes.append("后端问题，代码没问题：告知用户，不要改代码")
    elif ec == "DEPENDENCY":
        fixes.append("依赖安装失败：换库或换方案（packages 里加对包名，或换纯 Python 实现）")

    # ---- 参数格式错 ----
    if "slice indices" in error_text:
        for k, v in (params or {}).items():
            if isinstance(v, dict):
                fixes.append(f"`{k}` 传了 dict，应传 list（如 `{k}=[\"关键词\"]`）")
            elif isinstance(v, str) and v.isdigit():
                fixes.append(f"`{k}` 传了字符串 `\"{v}\"`，应传整数 `{v}`")
    # 分两步取：「xxx 必须是 yyy」与「收到的是/不能是 zzz」可能隔着一个从句
    # （例：packages 必须是数组，如 ["a","b"]，不能是 str），单条正则会漏。
    m = re.search(r"(\w+)\s*必须是\s*([^，,。；;]+)", error_text)
    if m:
        k, expected = m.group(1), m.group(2).strip()
        m2 = re.search(r"(?:收到的是|不能是|应为|而不是)\s*([^。；;]+)", error_text)
        got = m2.group(1).strip() if m2 else ""
        fixes.append(f"`{k}` 必须是 {expected}" + (f"，收到的是 {got}" if got else ""))

    # ---- 缺参数 ----
    m = re.search(r"缺少\s*(\w+)\s*参数", error_text)
    if m:
        fixes.append(f"补上必填参数 `{m.group(1)}`；先用 contract_agent.get 看完整参数列表")

    # ---- 不存在 ----
    if "不存在" in error_text or "not found" in error_text.lower():
        if "插件源码" in error_text or ".py" in error_text:
            fixes.append("插件路径错：先 `cogitor.search` 或 `list_plugins` 查真实路由")
        elif "skill" in error_text.lower():
            fixes.append("skill 路径错：先 `cogitor.search(terms=[...])` 查真实 skill 路由（`skill/plugin/...` 或 `skill/...`）")
        elif "MCP" in error_text:
            fixes.append("MCP 契约不存在：先 `cogitor.list_plugins(category=\"mcp\")` 查真实 server/tool")
        else:
            fixes.append("路径不存在：先 `list_dir` 确认路径，或检查文件名拼写")

    # ---- 多个匹配 ----
    if "有多个匹配" in error_text:
        fixes.append("插件名不唯一：用完整路径（`plugins/xxx/yyy.py` 去掉 `.py`）")

    # ---- 'str' object 异常 ----
    if "'str' object has no attribute 'receiver'" in error_text:
        fixes.append("`agent.system.call` 要传 `core.Envelop(...)`，不能传字符串")

    # ---- 插件内部异常兜底 ----
    # 框架把插件内部未捕获异常包成 {"ok":false,"error":"Plugin execution error: ..."}，
    # 这类不是「调用参数写错」，而是插件自己的代码炸了 —— 修插件，不是改参数。
    _txt = (error_text or "").strip()
    if _txt.startswith("Plugin execution error:"):
        fixes.append(
            "插件内部异常：不是调用参数问题——读【原因】/堆栈定位异常源，修插件源码"
        )
    elif not fixes and re.search(r"[A-Za-z_][\w.]*(?:Error|Exception)\b", _txt):
        fixes.append(
            "运行时异常：读 error 的【原因】/堆栈定位异常源，改代码（不是调用参数问题）"
        )

    return "；".join(fixes)


def _record_failure(agent, user_id, session_id, target, action, params,
                    error_source, error_text, result=None, error_category=""):
    """插件调用失败 → 写 backpack.json（指纹去重 + 20 条上限 + 正确做法）。

    error_source: "exception" | "business"
    result: call_end.detail.result（可选），用于提取工具返回里的 hints/fix。
    """
    try:
        if not target or target.startswith("builtins/agents/main_agent"):
            return

        # 框架把插件内部未捕获异常包成 {"ok":false,"error":"Plugin execution error: ..."}，
        # 走的是 business 分支 —— 这里纠正来源，否则插件自身炸了会被误标成「调用参数写错」。
        if str(error_text or "").strip().startswith("Plugin execution error:"):
            error_source = "exception"

        is_app = target.startswith("applications/")
        et, phrase = _error_signature(error_text, error_category)
        fp = _failure_fingerprint(target, action, error_text, error_category)
        data = _load_backpack(user_id)
        items = data.get("items", [])
        now = datetime.now().isoformat()

        # 正确做法：先取工具返回的 hints/fix，再规则推断，applications 兜底
        fix = _extract_fix_from_result(result) if result else ""
        if not fix:
            fix = _infer_fix(params or {}, error_text, error_category)
        if is_app and not fix:
            app_name = target.split("/")[1] if len(target.split("/")) > 1 else target
            fix = (
                f"应用 {app_name} 的插件出错，检查 {target} 的返回/参数；"
                f"若为应用 bug，修 {app_name} 的代码"
            )

        # 阈值：applications 更低（3/5），builtins 是（5/10）
        soft_th = _FAILURE_APP_SOFT_FIX if is_app else _FAILURE_SOFT_FIX
        hard_th = _FAILURE_APP_HARD_FIX if is_app else _FAILURE_HARD_FIX

        sanitized = _sanitize_params(params or {})
        params_snapshot = json.dumps(sanitized, ensure_ascii=False, default=str)[:300]

        # 指纹已存在 → count + 1
        for it in items:
            if it.get("fingerprint") == fp:
                it["count"] = int(it.get("count", 1)) + 1
                it["last_seen"] = now
                if not it.get("fix") and fix:
                    it["fix"] = fix
                c = it["count"]
                it["needs_fix"] = "hard" if c >= hard_th else ("soft" if c >= soft_th else None)
                _save_backpack(user_id, data)
                return

        # 新指纹 → 追加
        max_n = 0
        for it in items:
            m = re.match(r"exp_(\d+)", str(it.get("id", "")))
            if m:
                try:
                    max_n = max(max_n, int(m.group(1)))
                except Exception:
                    pass

        tags = ["失败", target.split("/")[-1], error_source, et]
        if is_app:
            tags.append("application")

        items.append({
            "id": f"exp_{max_n + 1:03d}",
            "title": f"{target.split('/')[-1]} 失败：{phrase[:30]}",
            "summary": f"调用 {target}（action={action or 'none'}）失败：{et}: {phrase}",
            "tags": tags,
            "body": (
                f"目标：{target}\n"
                f"action：{action or 'none'}\n"
                f"错误类型：{et}\n"
                f"错误原文：{error_text[:500]}\n"
                f"当时参数：{params_snapshot}\n"
                + (f"正确做法：{fix}\n" if fix else "")
                + f"会话：{session_id}\n"
                f"首次：{now}"
            ),
            "params_snapshot": params_snapshot,
            "fix": fix,
            "fingerprint": fp,
            "count": 1,
            "error_type": et,
            "error_source": error_source,
            "first_seen": now,
            "last_seen": now,
        })

        # 20 条上限：count 最低 + last_seen 最旧先淘汰
        if len(items) > _FAILURE_MAX_ITEMS:
            items.sort(key=lambda x: (
                int(x.get("count", 1)),
                str(x.get("last_seen", "") or ""),
            ))
            items = items[-_FAILURE_MAX_ITEMS:]

        # 排序：count 降序 + last_seen 降序
        items.sort(key=lambda x: (
            -int(x.get("count", 1)),
            str(x.get("last_seen", "") or ""),
        ))
        data["items"] = items
        _save_backpack(user_id, data)
    except Exception as e:
        Logger.warn(f"[record_failure] 写失败记录失败: {e}")


def _load_failure_index(user_id: str) -> str:
    """渲染 【经验背包 · 自动记录的失败】段：错误 + 正确做法 + 修根建议。"""
    data = _load_backpack(user_id)
    items = data.get("items", [])
    if not items:
        return ""
    # 只渲染 tags 含 "失败" 的条目
    fails = [it for it in items if "失败" in (it.get("tags") or [])]
    if not fails:
        return ""
    # count 降序 + last_seen 降序
    fails.sort(key=lambda x: (
        -int(x.get("count", 1)),
        str(x.get("last_seen", "") or ""),
    ))
    lines = []
    for it in fails:
        count = int(it.get("count", 1))
        nf = it.get("needs_fix")
        is_app = "application" in (it.get("tags") or [])
        if nf == "hard":
            prefix = "🛑"
        elif nf == "soft":
            prefix = "⚠️"
        else:
            prefix = "❌"
        lines.append(f"- {prefix} [{it.get('id','')}] {it.get('title','')} ×{count}")
        fix = it.get("fix", "")
        if fix:
            lines.append(f"    ✅ 正确做法：{fix}")
        else:
            params = it.get("params_snapshot", "")
            if params:
                lines.append(f"    参数：{params}（截断）")
        if nf == "hard":
            if is_app:
                lines.append(f"    → 应用 bug，踩 {count} 次，该修了")
            else:
                lines.append(f"    → 踩 {count} 次了，先 `contract_agent.get` 看参数类型，别再猜")
    return "\n".join(lines)

def _load_experience_index(user_id: str) -> str:
    """只渲染索引：每条一行 [id] title（tags）。"""
    data = _load_backpack(user_id)
    items = data.get("items", [])
    if not items:
        return ""
    lines = []
    for it in items:
        iid = it.get("id", "")
        title = it.get("title", "")
        tags = it.get("tags", []) or []
        tag_str = f"（tags: {', '.join(str(t) for t in tags)}）" if tags else ""
        lines.append(f"- [{iid}] {title}{tag_str}")
    return "\n".join(lines)

class PromptBuilder:
    @staticmethod
    def build_system(session_id: str) -> str:
        return _load_skill().replace("{{session_id}}", session_id).replace("__SESSION_ID__", session_id)

    @staticmethod
    def build_user(user_id: str, session_id: str, context: str, depth: int = 0, flow=None) -> str:
        parts = []

        failure_index = _load_failure_index(user_id)
        taskboard = _load_taskboard(user_id, session_id)
        _auto_unpin_idle(user_id, session_id, context or "")
        active_skill = _load_active_skill(user_id, session_id)
        if active_skill:
            parts.append(f"【Pin-当前技能-active_skills】\n{active_skill}")
        task_summary = ""
        tasks_file = _conversations_dir(user_id, session_id) / "tasks.json"
        if tasks_file.exists():
            try:
                tasks = json.loads(tasks_file.read_text(encoding="utf-8"))
                if tasks:
                    lines = ["【分身任务】"]
                    for tid, t in sorted(tasks.items(), key=lambda x: x[1].get("created_at", "")):
                        status = t.get("status", "unknown")
                        icon = {"running": "⏳", "done": "✅", "failed": "❌", "timeout": "⏰", "cancelled": "🚫"}.get(status, "?")
                        desc = t.get("description", "")
                        lines.append(f"{icon} {tid}: {desc} [{status}]")
                    task_summary = "\n".join(lines)
            except Exception:
                pass

        if context:
            parts.append(
                f"【信息流】只保留最近 90 条，更早的历史如需回溯，用 "
                f"`builtins/tools/search_memory_agent`（`action=\"get\"`）召回。\n{context}"
            )
            parts.append(
                "【回溯历史指引】用 `builtins/tools/search_memory_agent`，`action=\"get\"`，"
                "`plugin_args` 三种模式**三选一**：\n"
                "\n"
                "1. **按轮次**（想知道「第 N 轮干了什么、结果如何、下一步是什么」）→ 返回因果叙事：\n"
                "   `plugin_args={\"session_id\": \"<sid>\", \"rounds\": [2, 3]}`（`session_id` 唯一即可定位，不需要 `user_id``\n"
                "\n"
                "2. **按步骤**（想知道「第 N 轮第 M 步干了什么」）→ 返回因果叙事：\n"
                "   `plugin_args={\"session_id\": \"<sid>\", \"steps\": [\"3.1\"]}`\n"
                "\n"
                "3. **按关键词**（想知道「某个关键词相关的原文/细节」）→ 返回答案：\n"
                "   `plugin_args={\"session_id\": \"<sid>\", \"query\": \"用户的问题\", \"keywords\": [\"词1\",\"词2\"]}`\n"
                "\n"
                "**注意**：\n"
                "- `session_id` 必填（唯一即可定位文件）；`user_id` 可选，仅用于消歧/加速。\n"
                "- `rounds` / `steps` / `keywords` **三选一**，不要同时传。\n"
                "- `rounds` / `steps` **只返回因果叙事**（一段话，≤300 字），**不返回原文**；需要原文细节用 `keywords` 搜。\n"
                "- `rounds` / `steps` 不需要 `query`；`keywords` 模式需要 `query`，`keywords` 可省略（自动从 query 抽取）。\n"
            )
        if failure_index:
                    parts.append(
                        f"【经验背包 · 坑点，严禁反复踩坑】插件调用失败自动记录，不需要你手动写。\n"
                        f"{failure_index}"
                    )
        if taskboard:
            parts.append(
                f"【任务看板】判断任务复杂度，\n{taskboard}"
            )
        if task_summary:
            parts.append(task_summary)

        # ★ 递归深度提示：按 MAX_RECURSION_DEPTH=40 设计
        #   39 轮必须 reply（硬命令），否则 40 轮 mark_interrupted("max_depth")
        if depth == 0:
            hint = "开始执行，综合分析，整理思路，逐步执行，一次只调用一次工具或者回复。"
        elif depth <= 10:
            hint = (
                "执行中：预计执行深度，随时 `builtins/tools/add_task_board`"
                "（action=\"replace\"，plugin_args={\"content\": \"看板内容\"}）先添加看板、记录过程；"
                "确认完成后用 action=\"clear\" 清除。"
            )
        elif depth <= 20:
            hint = "检查：本次执行是否接近完成？是→reply，否→继续。"
        elif depth <= 30:
            hint = (
                "注意：未完成内容必须建立任务看板！！！避免跨轮丢失内容，"
                "优先考虑是否该 reply 用户当前进展。"
            )
        elif depth <= 37:
            hint = (
                f"⚠️ 警告：执行深度 {depth}/40，即将达到上限。"
                f"**先整理当前进展，优先 reply 用户**；"
                f"确实必须继续时，最多再走 1~2 步就必须 reply。"
            )
        elif depth == 38:
            hint = (
                f"🚨 强警告：执行深度 {depth}/40，**下一步必须 reply**。"
                f"把当前进展 / 结论 / 未完成项整理成 reply 回复用户，不要再调工具。"
            )
        else:  # depth >= 39
            hint = (
                f"🛑 最后机会：执行深度 {depth}/40。"
                f"**本轮唯一允许的动作是 reply**——任何 use_tool 都会被拒绝。"
                f"立刻把当前进展 / 结论 / 未完成项整理成 reply 回复用户。"
            )

        parts.append(f"\n执行深度：{depth}/{MAX_RECURSION_DEPTH} - {hint}\n")
        # ★ 最近一次 env（从 flow 里最后一次 call_end 的 result.env 取）
        last_env = None
        for e in reversed(flow._flow if hasattr(flow, "_flow") else []):
            if e.get("from") == "system" and e.get("action") == "call_end":
                r = (e.get("detail") or {}).get("result") or {}
                if isinstance(r, dict) and isinstance(r.get("env"), dict):
                    last_env = r["env"]
                    break
        if last_env:
            env_bits = []
            if "gpu" in last_env:
                env_bits.append(f"gpu={last_env['gpu']}")
            if "disk_free_gb" in last_env:
                env_bits.append(f"disk_free={last_env['disk_free_gb']}GB")
            if "cpu_count" in last_env:
                env_bits.append(f"cpu={last_env['cpu_count']}")
            if "exec_timeout_s" in last_env:
                env_bits.append(f"exec_timeout={last_env['exec_timeout_s']}s")
            net = last_env.get("net") or {}
            if net:
                env_bits.append("net{" + " ".join(f"{k}={v}" for k, v in net.items()) + "}")
            if env_bits:
                parts.append(
                    f"【执行环境（code_runner 实测）】\n"
                    f"{' | '.join(env_bits)}\n"
                    f"决策时按此评估可行性：下载/安装超过 exec_timeout_s 的、"
                    f"gpu=false 时不要装 CUDA 包、网络慢时不要下大文件。"
                )
        return "\n\n".join(parts)


# ============================================================
# ToolRegistry
# ============================================================

class ToolRegistry:
    @staticmethod
    async def execute(agent, args: dict, user_id: str, session_id: str, flow: InformationFlow,
                      think: str = "") -> dict:
        target = args.get("target", "")
        if target.startswith("skill/"):
            retain = args.get("retain", 5)
            rel = target[len("skill/"):].strip("/")
            # ★ 统一解析：skill/<rel>/SKILL.md 与 skill/plugin/<ppath>/<stem> 都支持
            skill_file = _resolve_skill_file(rel)

            if skill_file is None:
                msg = f"skill 不存在: {target}"
                await flow.append_system(
                    action="call_error", result_think=f"❌ {msg}",
                    detail={"target": target, "error": msg}, retain=3,
                )
                return {"ok": False, "think": msg}

            try:
                text = skill_file.read_text(encoding="utf-8")
            except Exception as e:
                return {"ok": False, "think": f"读取 skill 失败: {e}"}

            body = text
            if text.startswith("---"):
                end = text.find("\n---", 3)
                if end != -1:
                    body = text[end + 4:].strip()

            # ★ 写 skill_state.json（钉住），不进 flow
            # sid 按文件路径生成，跨目录唯一（历史 target.replace 会产生重复 id）
            sid = _skill_sid_for_file(skill_file)
            state = _load_skill_state(user_id, session_id)
            pinned = state.get("pinned", {})
            pinned[sid] = {
                "source": "skill",
                "path": str(skill_file),
                "title": target,
                "desc": body[:120],
                "status": "pinned",
                "full_text": body,
                "retain": retain,
                "last_use_turn": 0,
                "pinned_at": datetime.now().isoformat(),
            }
            state["pinned"] = pinned
            _save_skill_state(user_id, session_id, state)

            # 写一条 system（call_start + call_end），但 result 只放“摘要”，不放全文
            call_id = str(uuid.uuid4())[:8]
            await flow.append_system(
                action="call_start",
                result_think=f"📤 钉住 skill: {target}",
                detail={"call_id": call_id, "target": target, "action": "pin_skill",
                        "params": {}, "status": "pending"},
                retain=retain,
            )
            await flow.append_system(
                action="call_end",
                result_think=f"📨 skill 已钉住: {target}",
                detail={
                    "call_id": call_id, "target": target,
                    "result": {"ok": True, "data": {"pinned": True, "path": str(skill_file), "size": len(body)}},
                },
                retain=retain,
            )
            return {"ok": True, "think": f"skill {target} 已钉住（{len(body)} 字）", "data": {"pinned": True, "path": str(skill_file), "size": len(body), "usage": f"已加入当前技能：{target}（{len(body)} 字）"}}
        tool_action = args.get("action", "")
        if not target:
            # 补一条 call_error，让 flow 和前台都看到
            call_id = str(uuid.uuid4())[:8]
            await flow.append_system(
                action="call_error",
                result_think="❌ 缺少 target",
                detail={"call_id": call_id, "target": "", "error": "缺少 target"},
                retain=3,
            )
            await _push_progress(
                agent, user_id, session_id, "error", "⚠️ 缺少 target",
                extra={"call_id": call_id, "target": "", "status": "error", "error": "缺少 target"},
            )
            return {"ok": False, "think": "缺少 target"}
        # 历史别名兼容：旧工具描述误写 os/file_utils_api，重写为真实路由
        if target == "os/file_utils_api":
            target = "builtins/tools/file_utils_api"

        if isinstance(args.get("plugin_args"), dict):
                tool_params = dict(args["plugin_args"])
        else:
            # 排除元字段，剩下的都当参数
            tool_params = {
                k: v for k, v in args.items()
                if k not in ("target", "action", "retain", "params", "think")
            }


        if target == "builtins/tools/save_blocks":
            default_retain = 20
        elif target in ("builtins/tools/file_utils_api", "os/file_utils_api") and \
             args.get("action") in ("read_file", "read_file_lines"):
            default_retain = 10
        else:
            default_retain = 5
        retain = args.get("retain", default_retain)


        call_id = str(uuid.uuid4())[:8]    # ← 提前
        t0 = time.time()                    # ← 提前
        # ★ 推送：执行工具前
        await _push_progress(
            agent, user_id,session_id, "executing",
                f"⚡ {target}",
                extra={
                    "call_id": call_id,
                    "target": target,
                    "tool_action": tool_action,
                    "status": "start",
                     "think": think,
                },
        )

        # ★ 自调用拦截
        if target == "builtins/agents/main_agent":
            target_session = tool_params.get("session_id", "")
            if not target_session:
                tool_params["session_id"] = f"sub_{uuid.uuid4().hex[:8]}"
            elif target_session == session_id:
                msg = "禁止同 session 自调用（会死循环）。技能已加载时直接用当前角色回复；如需分身，session_id 用 sub_ 开头的新 ID。"
                call_id = str(uuid.uuid4())[:8]
                await flow.append_system(
                    action="call_error",
                    result_think=f"❌ {msg}",
                    detail={"call_id": call_id, "target": target, "error": msg},
                    retain=3,
                )
                await _push_progress(agent, user_id, session_id, "error", f"⚠️ {msg[:120]}",
                                        extra={"call_id": call_id, "target": target, "status": "error"})
                return {"ok": False, "think": msg}
            elif not target_session.startswith("sub_"):
                msg = "分身 session_id 必须以 sub_ 开头，例如 sub_task_001。"
                call_id = str(uuid.uuid4())[:8]
                await flow.append_system(
                    action="call_error",
                    result_think=f"❌ {msg}",
                    detail={"call_id": call_id, "target": target, "error": msg},
                    retain=3,
                )
                await _push_progress(
                    agent, user_id, session_id, "error", f"⚠️ {msg[:120]}",
                    extra={"call_id": call_id, "target": target, "status": "error"},
                )
                return {"ok": False, "think": msg}


        await flow.append_system(
            action="call_start",
            result_think=f"📤 调用工具: {target}",
            detail={
                "call_id": call_id,
                "target": target,
                "action": tool_action,
                "params": tool_params,
                "status": "pending",
            },
            retain=retain,
        )

        meta = {"session_id": session_id, "user_id": user_id, "_call_id": call_id}
        call_params = {**tool_params}
        if "callback_receiver" in call_params:
            meta["callback_receiver"] = call_params.pop("callback_receiver")
        if "callback_session_id" in call_params:
            meta["callback_session_id"] = call_params.pop("callback_session_id")
        if "token" in call_params:
            meta["token"] = call_params.pop("token")

        payload = {"action": tool_action, **call_params} if tool_action else dict(call_params)

        try:
            result = await agent.system.call(core.Envelop(
                sender="builtins/agents/main_agent",
                receiver=target,
                payload=payload,
                meta=meta,
            ))

            data = result.result() if result is not None else {"ok": False, "error": "no response"}

            if data.get("status") == "processing":
                trace_id = (result.meta or {}).get("trace_id", "") if result else ""
                await flow.append_system(
                    action="async_processing",
                    result_think=f"⏳ 异步任务已提交 [trace={trace_id[:8]}]",
                    detail={"trace_id": trace_id, "status": "processing"},
                    retain=3,
                )
                # ★ 推送：异步提交
                await _push_progress(agent,user_id, session_id, "executing", f"⚡ 异步任务已提交（trace={trace_id[:8]}）")
                return {
                    "ok": True,
                    "think": f"任务已提交（trace={trace_id[:8]}）",
                    "data": {"status": "processing", "trace_id": trace_id},
                }

            elapsed_ms = int((time.time() - t0) * 1000)
            ok = data.get("ok", True)
            if ok:
                think = f"工具「{target}」业务成功"
                await _push_progress(agent,user_id, session_id, "done", f"✅ {target}",
                    extra={
                        "call_id": call_id, "target": target,
                        "status": "ok", "duration_ms": elapsed_ms,
                    })
            else:
                think = f"工具「{target}」业务失败: {str(data.get('error', ''))[:150]}"
                await _push_progress(agent,user_id, session_id, "error", f"⚠️ {target}",
                    extra={
                        "call_id": call_id, "target": target,
                        "status": "error", "duration_ms": elapsed_ms,
                        "error": str(data.get("error", ""))[:200],
                    })
                _record_failure(
                    agent, user_id, session_id,
                    target=target, action=tool_action,
                    params=tool_params, error_source="business",
                    error_text=str(data.get("error", "")),
                    result=data,
                    error_category=str(data.get("error_category", "") or ""),
                )

            # ★★★ 补回：把结果写进 flow ★★★
            await flow.append_system(
                action="call_end",
                result_think=f"📨 调用完成: {target}",
                detail={
                    "call_id": call_id,
                    "target": target,
                    "action": tool_action,      # ★ 加这个
                    "result": data,
                    "duration_ms": elapsed_ms,
                },
                retain=retain,
            )

            # ★ 保证 data 字段永远是 dict，避免下游 result["data"].get(...) 报
            #   'str' object has no attribute 'get'
            data_field = data.get("data", data)
            if not isinstance(data_field, dict):
                data_field = {"data": data_field}

            return {
                "ok": ok,
                "think": think,
                "data": data_field,
                "_full_result": json.dumps(data, ensure_ascii=False, default=str),
            }

        except Exception as e:
            Logger.error(f"工具执行异常: target={target}, action={tool_action}\n{e}", e)
            await flow.append_system(
                action="call_error",
                result_think=f"❌ 调用失败: {target}",
                detail={"call_id": call_id, "error": str(e)},
                retain=retain,
            )
            elapsed_ms = int((time.time() - t0) * 1000)
            await _push_progress(agent,user_id, session_id, "error", f"❌ {target}",
                extra={
                    "call_id": call_id,
                    "target": target,
                    "status": "error",
                    "duration_ms": elapsed_ms,
                    "error": str(e)[:200],
                })
            # ★ 失败自动记（异常类）
            _record_failure(
                agent, user_id, session_id,
                target=target, action=tool_action,
                params=tool_params, error_source="exception",
                error_text=str(e), result=None,
            )
            return {"ok": False, "think": f"工具「{target}」执行失败: {str(e)}"}

def _brief_args(args, max_str: int = 80) -> dict:
    """把 args 里的长字符串/长容器截断成摘要，避免 raw_display 过大。"""
    if not isinstance(args, dict):
        return args
    out = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > max_str:
            out[k] = v[:max_str] + f"…（{len(v)} 字）"
        elif isinstance(v, (list, dict)):
            try:
                s = json.dumps(v, ensure_ascii=False, default=str)
            except Exception:
                s = str(v)
            if len(s) > max_str * 2:
                out[k] = s[:max_str] + f"…（{len(s)} 字符）"
            else:
                out[k] = v
        else:
            out[k] = v
    return out

def _fill_base_url(content: str, agent) -> str:
    if not content:
        return content
    base_url = getattr(agent, "base_url", "") or ""
    if not base_url:
        return content
    return content.replace("{base_url}", base_url).replace("{agent.base_url}", base_url)
# ============================================================
# LLMThinker
# ============================================================

class LLMThinker:

    async def think(self, agent, flow: InformationFlow, user_id: str, session_id: str, depth: int = 0) -> dict:
        llm = agent.llm
        if not llm:
            return {"call": "reply", "content": "抱歉，AI 服务暂时不可用", "args": {}}

        # ★ 每轮 LLM 调用算一个 turn，供 manage_flow 的 TTL 判断
        flow._current_turn += 1
        flow._gc_expanded()

        system_prompt = PromptBuilder.build_system(session_id)
        context = flow.get_context()

        user_prompt = PromptBuilder.build_user(user_id, session_id, context, depth, flow=flow)
        try:
            _debug_dump_context(flow, user_prompt, depth)
        except Exception as _e:
            Logger.warn(f"[debug] dump context 失败: {_e}")
        Logger.info("LLM 思考中...")

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]


        if depth >= MAX_RECURSION_DEPTH - 1:
                tools = [t for t in TOOL_SCHEMAS if t["function"]["name"] == "reply"]
                tool_choice = "auto"
        else:
            tools = list(TOOL_SCHEMAS)
            tool_choice = "auto"

        tool_calls_list: list = []
        text_buf = ""
        error_msg = ""
        manage_flow_done = False          # ★ 标记本轮已处理 manage_flow

        try:
            async for event in llm.chat_tool_stream(
                messages, tools=tools, tool_choice=tool_choice,
                session_id=session_id,
            ):
                etype = event.get("type")
                if etype == "thinking":
                    chunk = event.get("content", "")
                    _buffer_stream_chunk(agent, user_id, session_id, chunk)
                elif etype == "text":
                    chunk = event.get("content", "")
                    text_buf += chunk
                    _buffer_stream_chunk(agent, user_id, session_id, chunk)
                elif etype == "tool_call":
                    tc = event.get("tool_call")

                    # ★ manage_flow 特殊处理：直接执行，不进 tool_calls_list
                    if tc.name == "manage_flow":
                        args = tc.arguments or {}
                        try:
                            result = await flow.manage_flow(
                                collapse=args.get("collapse"),
                                ignore=args.get("ignore"),
                                expand=args.get("expand"),
                                ttl=args.get("ttl", 3),
                            )
                            manage_flow_done = True
                            _info_parts = result.get("think", "")
                            Logger.info(f"[manage_flow] depth={depth} {_info_parts}")
                            try:
                                await _push_progress(
                                    agent, user_id, session_id, "executing",
                                    f"🧭 信息流：{_info_parts}",
                                    extra={"status": "manage_flow"},
                                )
                            except Exception:
                                pass
                        except Exception as e:
                            Logger.warn(f"[manage_flow] 执行失败: {e}")
                            manage_flow_done = True
                        # ★ 不 break、不 continue 特殊处理——manage_flow 进 tool_calls_list 之外，
                        #   但流继续消费（模型可能先 manage_flow 再 use_tool）
                        continue

                    # 正常 tool_call（reply / use_tool）
                    tool_calls_list.append(tc)
                    try:
                        tc_args = tc.arguments or {}
                        think_val = tc_args.get("think", "") if isinstance(tc_args, dict) else ""
                        args_display = {k: v for k, v in tc_args.items() if k != "think"} if isinstance(tc_args, dict) else tc_args
                        # ★ 长参数先摘要，避免 raw_display 过大 / 截断 JSON
                        args_brief = _brief_args(args_display)
                        raw_display = json.dumps(
                            {"think": (think_val or "")[:200], "tool": tc.name, "args": args_brief},
                            ensure_ascii=False, default=str
                        )
                        # ★ 不 await，丢后台，避免 WebSocket 慢时阻塞 async for
                        asyncio.create_task(
                            _push_stream(agent, user_id, session_id, f"\n🧩 {raw_display}\n")
                        )
                    except Exception:
                        pass
                elif etype == "error":
                    error_msg = event.get("error", "")
                    await _push_progress(agent, user_id, session_id, "error", f"⚠️ LLM: {error_msg[:120]}")
                elif etype == "done":
                    break
        finally:
            await _stop_stream_flush(session_id)

        # ★ manage_flow 处理完，但本轮还有其他 tool_call / text → 正常走后续分支
        if manage_flow_done and not tool_calls_list and not text_buf.strip():
            await _push_stats(agent, user_id, session_id, depth)
            return {
                "call": "manage_flow",
                "content": "",
                "args": {},
            }
        # 否则继续走下面的 if tool_calls_list / if text_buf.strip() 分支

        if error_msg:
            low = error_msg.lower()
            if (
                "network" in low
                or "connection" in low
                or "timeout" in low
                or "超时" in error_msg          # ★ 加中文「超时」
                or "服务请求超时" in error_msg   # ★ 加完整文案
                or "tool_input_invalid" in low
                or "incomplete" in low
                or "sse stream" in low
                or "api_error" in low
                or "被截断" in low
            ):
                raise ConnectionError(error_msg)
            if is_llm_error_string(error_msg):
                raise ContentSafetyBlockedError(error_msg)
            return {"call": "reply", "content": f"LLM 调用失败: {error_msg[:200]}", "args": {}}
        await _push_stats(agent, user_id, session_id, depth)

        if tool_calls_list:
            results = []
            for tc in tool_calls_list:
                name = tc.name
                args = tc.arguments or {}
                think = args.pop("think", "") if isinstance(args, dict) else ""

                if name == "reply":
                    content = _fill_base_url(args.get("content", ""), agent)
                    if text_buf.strip():
                        content = (text_buf.strip() + "\n" + content).strip() if content else text_buf.strip()
                    await flow.append_ai(think, "reply", content=content, retain=args.get("retain", 3))
                    return {"call": "reply", "content": content, "args": {}}

                if name == "use_tool":
                    retain = args.get("retain", 3)
                    combined_think = think
                    if text_buf.strip():
                        combined_think = (think + "\n" + text_buf.strip()).strip() if think else text_buf.strip()
                        text_buf = ""
                    await flow.append_ai(combined_think, "use_tool", args=args, retain=retain)
                    results.append({"call": "use_tool", "args": args, "think": combined_think, "retain": retain})

            if results:
                first = results[0]
                first["_pending_tool_calls"] = results[1:]
                return first

        if text_buf.strip():
            content = _fill_base_url(text_buf.strip(), agent)
            await flow.append_ai("", "reply", content=content, retain=3)
            return {"call": "reply", "content": content, "args": {}}

        if not tool_calls_list and not text_buf.strip() and not error_msg:
            Logger.warn(f"[LLMThinker][empty_output] depth={depth} session={session_id}")
            raise EmptyOutputError("模型只输出了 thinking，没有 tool_call / text")

        raw_dump = {
            "tool_calls": [repr(tc) for tc in tool_calls_list] if tool_calls_list else None,
            "text_buf": text_buf,
            "text_buf_len": len(text_buf),
            "error_msg": error_msg,
            "depth": depth,
            "session_id": session_id,
        }
        raw_json = json.dumps(raw_dump, ensure_ascii=False, indent=2, default=str)
        Logger.warn(f"[LLMThinker][raw_unhandled] depth={depth} session={session_id}\n{raw_json}")
        content = f"⚠️ 模型原始输出未处理：\n```json\n{raw_json}\n```"
        return {"call": "reply", "content": content, "args": {}}


# ============================================================
# CoreProcessor
# ============================================================

class CoreProcessor:
    def __init__(self):
        self.thinker = LLMThinker()
        self.tools = ToolRegistry()
        self._think_sessions: set = set()
        self._pending_callbacks: Dict[str, int] = {}
        self._interrupt_signals: Dict[str, bool] = {}

    def signal_interrupt(self, session_id: str):
        self._interrupt_signals[session_id] = True

    def clear_interrupt(self, session_id: str):
        self._interrupt_signals.pop(session_id, None)

    async def process(self, agent, user_id: str, session_id: str, flow: InformationFlow, depth: int = 0) -> dict:
        # _think_sessions 由 execute 管，process 不再 add/discard
        result = await self._process_internal(agent, user_id, session_id, flow, depth)
        if result.get("interrupted"):
            self._pending_callbacks.pop(session_id, 0)
            return result
        pending_count = self._pending_callbacks.pop(session_id, 0)
        if pending_count > 0:
            result = await self._process_internal(agent, user_id, session_id, flow, 0)
        return result

    async def _process_internal(self, agent, user_id: str, session_id: str,
                                flow: InformationFlow, depth: int = 0) -> dict:
        # ★ 中断：不删 flow，标记状态
        if self._interrupt_signals.get(session_id, False):
            self.clear_interrupt(session_id)
            try:
                await flow.mark_interrupted("user_interrupt", "")
            except Exception as e:
                Logger.warn(f"[interrupt] mark_interrupted 失败: {e}")
            await _push_progress(
                agent, user_id, session_id, "done", "⏸️ 已中断（现场保留）",
                extra={"status": "interrupted"},
            )
            return {"ok": True, "interrupted": True, "call": "reply", "content": "", "args": {}}

        if depth > MAX_RECURSION_DEPTH:
            Logger.warn(f"达到最大递归深度 ({MAX_RECURSION_DEPTH})")
            try:
                await flow.mark_interrupted("max_depth", f"depth={depth}")
            except Exception:
                pass
            return {"ok": False, "call": "reply",
                    "content": "处理步骤过多，请简化需求后重试", "args": {}}

        # ★ 全局重试计数（session 级）
        retry_key = f"{session_id}:empty_output"
        if not hasattr(self, "_empty_retries"):
            self._empty_retries = {}
        retry_count = self._empty_retries.get(retry_key, 0)

        output = None
        for _retry in range(2):
            try:
                output = await self.thinker.think(agent, flow, user_id, session_id, depth)
                self._empty_retries[retry_key] = 0
                break
            except EmptyOutputError as e:
                if _retry == 0 and retry_count < 2:
                    self._empty_retries[retry_key] = retry_count + 1
                    Logger.warn(
                        f"[empty_output] 模型只输出 thinking，自动重试 1 次 "
                        f"(depth={depth}, 已重试={retry_count + 1}/2)"
                    )
                    try:
                        await flow.append_system(
                            action="task_summary",
                            result_think=(
                                "⚠️ 上一次决策只输出了 thinking，或把「下一步动作」当 reply 说了。"
                                "**「我要读文件 / 我要查一下 / 我要继续 / I'll ...」不是 reply**，"
                                "应该用 use_tool / manage_flow 直接执行。"
                                "请直接输出一个 tool_call（reply 或 use_tool 或 manage_flow），"
                                "不要再只思考或只说要做什么。"
                            ),
                            detail={"reason": "empty_output"},
                            retain=2,
                        )
                    except Exception:
                        pass
                    continue
                self._empty_retries[retry_key] = 0
                content = (
                    "⚠️ **模型连续只输出思考过程，没有产出可执行内容**。\n\n"
                    "**建议**：\n"
                    "- 直接说「继续」或「下一步做什么」，让它重新决策\n"
                    "- 如果反复出现，换一个模型/端点再试"
                )
                await _push_chat(agent, user_id, session_id, content)
                return {"ok": True, "waiting": False, "call": "reply",
                        "content": content, "args": {}}

        if output is None:
            return {"ok": False, "call": "reply", "content": "模型输出异常", "args": {}}

        # ★ manage_flow：管理完直接递归下一轮，不执行工具，不 append_system
        if output.get("call") == "manage_flow":
            await _push_stats(agent, user_id, session_id, depth)
            Logger.info(f"[manage_flow] depth={depth} → 递归下一轮")
            return await self._process_internal(agent, user_id, session_id, flow, depth + 1)

        # reply
        if output.get("call") == "reply":
            content = output.get("content") or "好的，已处理"
            await _push_progress(
                agent, user_id, session_id, "done", "✅ 已回复用户",
                extra={
                    "status": "reply",
                    "is_final": True,
                    "tool_count": depth + 1,
                },
            )
            if content:
                await _push_chat(agent, user_id, session_id, content)
            await _push_stats(agent, user_id, session_id, depth)
            return {"ok": True, "waiting": False, "call": "reply",
                    "content": content, "args": {}}

        # use_tool
        if output.get("call") == "use_tool":
            all_calls = [output] + list(output.get("_pending_tool_calls") or [])

            has_processing = False
            for i, call in enumerate(all_calls):
                args = call.get("args", {}) or {}
                think = call.get("think", "")
                target = args.get("target", "")

                if len(all_calls) > 1:
                    Logger.info(
                        f"[tool] 串行执行 {i + 1}/{len(all_calls)} target={target} "
                        f"session={session_id} depth={depth}"
                    )

                result = await self.tools.execute(
                    agent, args, user_id, session_id, flow, think=think
                )

                if not result.get("ok", True):
                    await flow.append_system(
                        action="task_summary",
                        result_think=f"⚠️ 上一步工具失败: {result.get('think', '')[:150]}",
                        detail={"failed": True},
                        retain=2,
                    )

                if result.get("data", {}).get("status") == "processing":
                    has_processing = True
                    await _push_chat(
                        agent, user_id, session_id,
                        f"⏳ 任务已提交，正在后台处理...（{target}）"
                    )

            if has_processing:
                return await self._process_internal(agent, user_id, session_id, flow, depth + 1)

            await _push_stats(agent, user_id, session_id, depth + 1)
            return await self._process_internal(agent, user_id, session_id, flow, depth + 1)

        return {"ok": False, "call": "reply",
                "content": f"未知调用: {output.get('call')}", "args": {}}
core_processor = CoreProcessor()

async def _push_stats(agent, user_id: str, session_id: str, depth: int = 0):
    try:
        llm_stats = agent.llm.get_session_stats(session_id) if hasattr(agent.llm, "get_session_stats") else {}
        ctx_k = round(llm_stats.get("last_prompt_tokens", 0) / 1000, 1)
        if ctx_k > 0:
            _last_ctx_k[session_id] = ctx_k
        else:
            ctx_k = _last_ctx_k.get(session_id, 0)
        stats = {
            "context_k": ctx_k,
            "round_total": llm_stats.get("round_total_tokens", 0),
            "depth": depth,
            "max_depth": MAX_RECURSION_DEPTH,
        }
        await agent.system.call(core.Envelop(
            sender="builtins/agents/main_agent",
            receiver="os/_websocket",
            payload={"action": "push", "channel_id": f"pa_{user_id}",
                     "data": {"type": "stats", "session_id": session_id, "stats": stats}}
        ))
    except Exception as e:
        Logger.warn(f"[WS] 推送 stats 失败: {e}")
# ============================================================
# 主入口 execute
# ============================================================
from runtime._llm import set_current_session

async def _run_processing(agent, user_id, session_id, flow):
    """主流程异步执行。

    不回退，只标记中断；任何异常都不让 Task exception was never retrieved。
    """
    try:
        async with _get_processing_lock(session_id):
            try:
                await core_processor.process(agent, user_id, session_id, flow, 0)
            finally:
                core_processor._think_sessions.discard(session_id)
                core_processor._pending_callbacks.pop(session_id, 0)
                core_processor.clear_interrupt(session_id)

    except ConnectionError as e:
        Logger.warn(f"[network] 网络中断: {str(e)[:150]}")
        try:
            stats = await flow.mark_interrupted("network", str(e))
            side = (stats or {}).get("side_effects") or []
            hint = f"已完成的写操作：{', '.join(side[:5])}。" if side else ""
            await _push_chat(
                agent, user_id, session_id,
                f"⚠️ 网络中断，现场已保留。{hint}\n"
                f"说「继续」我会接着做，说「重来」我会重新开始。"
            )
        except Exception as inner_e:
            Logger.error(f"[network] 标记失败: {inner_e}", inner_e)
        finally:
            core_processor._think_sessions.discard(session_id)
            core_processor._pending_callbacks.pop(session_id, 0)
            core_processor.clear_interrupt(session_id)

    except ContentSafetyBlockedError as e:
        Logger.warn(f"[safety] LLM 调用失败: {str(e)[:150]}")
        try:
            stats = await flow.mark_interrupted("llm_error", str(e))
            side = (stats or {}).get("side_effects") or []
            hint = f"已完成的写操作：{', '.join(side[:5])}。" if side else ""
            await _push_chat(
                agent, user_id, session_id,
                f"⚠️ LLM 调用失败，现场已保留。{hint}\n"
                f"错误：{str(e)[:200]}\n"
                f"说「继续」我会接着做，说「重来」我会重新开始。"
            )
        except Exception as inner_e:
            Logger.error(f"[safety] 标记失败: {inner_e}", inner_e)
        finally:
            core_processor._think_sessions.discard(session_id)
            core_processor._pending_callbacks.pop(session_id, 0)
            core_processor.clear_interrupt(session_id)

    except asyncio.CancelledError:
        Logger.warn(f"[cancelled] 协程被取消: session={session_id}")
        core_processor._think_sessions.discard(session_id)
        core_processor._pending_callbacks.pop(session_id, 0)
        core_processor.clear_interrupt(session_id)
        raise

    except BaseException as e:
        Logger.error(f"异步处理 BaseException: {type(e).__name__}: {e}")
        try:
            await _push_chat(
                agent, user_id, session_id,
                f"❌ 系统错误: {type(e).__name__}: {str(e)[:200]}"
            )
        except Exception:
            pass
        finally:
            core_processor._think_sessions.discard(session_id)
            core_processor._pending_callbacks.pop(session_id, 0)
            core_processor.clear_interrupt(session_id)


def _json_safe(obj, _depth=0):
    """把 flow 里不可 JSON 序列化的对象（bytes 等）转成可序列化形式。

    背景：历史会话的工具结果带回了 bytes（如 meta.static_content 的字节），
    原样存进 flow 后，网关 json_response 序列化抛 TypeError → 返回 500 纯文本页，
    前端 res.json() 于是报 "Unexpected non-whitespace character after JSON at position 4"。
    """
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

async def _purge_session(user_id: str, session_id: str):
    """清一个会话：主会话 + 所有分身目录 + index 条目，直接删。"""
    sessions_base = _conversations_dir(user_id)

    # 1. 找该会话的所有分身
    sub_ids = []
    tasks_file = sessions_base / session_id / "tasks.json"
    if tasks_file.exists():
        try:
            tasks = json.loads(tasks_file.read_text(encoding="utf-8"))
            if isinstance(tasks, dict):
                sub_ids = list(tasks.keys())
        except Exception:
            pass

    # 2. 清内存缓存（主 + 所有分身）
    for sid in [session_id] + sub_ids:
        old = _session_flows.pop(f"{user_id}:{sid}", None)
        if old is not None:
            try:
                await old.close()
            except Exception:
                pass

    # 3. 删 index 条目（主 + 所有分身）
    index = _load_conversation_index(user_id)
    index_changed = False
    for sid in [session_id] + sub_ids:
        if sid in index:
            del index[sid]
            index_changed = True
    if index_changed:
        try:
            _index_file(user_id).write_text(
                json.dumps({"conversations": index}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            Logger.warn(f"[purge] 写 index 失败: {e}")

    # 4. 删目录（主 + 所有分身）
    for sid in [session_id] + sub_ids:
        shutil.rmtree(sessions_base / sid, ignore_errors=True)

async def execute(envelop, agent):
    try:
        user_id, session_id = resolve_ids(envelop)
        set_current_session(session_id)
        user_input = envelop.payload.get("content", "").strip()
        action = envelop.payload.get("action", "")
        date_str = envelop.payload.get("date", "")
        attachments = envelop.payload.get("attachments", [])
        if action == "whoami":
            envelop.payload = {"ok": True, "user_id": user_id, "session_id": session_id}
            return envelop
        # 1. 回调
        if envelop.meta.get("is_callback"):
            async with _get_processing_lock(session_id):
                return await _handle_callback(envelop, agent)

        # 2. 系统 action
        if action == "get_session":
            flow = _get_or_create_flow(user_id,session_id)
            all_entries = flow._flow

            # 按 user 切轮
            rounds = []
            cur = []
            for e in all_entries:
                if e.get("from") == "user" and cur:
                    rounds.append(cur)
                    cur = []
                cur.append(e)
            if cur:
                rounds.append(cur)

            # before_ts：只要时间戳 < 这个值的轮（用于"加载更多"）
            before_ts = envelop.payload.get("before_ts", "")
            if before_ts:
                rounds = [r for r in rounds if r and r[0].get("timestamp", "") < before_ts]

            # 取最后 N 轮（最近 N 轮）
            N = int(envelop.payload.get("limit", 10))
            recent = rounds[-N:] if len(rounds) > N else rounds
            flat = [e for r in recent for e in r]

            # has_more：还有更早的轮吗
            older_rounds = [r for r in rounds if not recent or (r and r[0].get("timestamp", "") < recent[0][0].get("timestamp", ""))]
            has_more = len(older_rounds) > 0

            envelop.payload = {
                "ok": True,
                "conversation_id": session_id,
                "flow": _json_safe(flat),
                "entry_count": len(all_entries),
                "round_count": len(rounds) if not before_ts else None,
                "returned_rounds": len(recent),
                "has_more": has_more,
                "oldest_ts": recent[0][0].get("timestamp") if recent else "",
                "title": _load_conversation_index(user_id).get(session_id, {}).get("title", ""),
            }
            return envelop

        if action == "get_day_history":
            # 兼容旧前端：不报错，返回空
            envelop.payload = {"ok": False, "error": "get_day_history 已废弃，改用 list_conversations"}
            return envelop

        if action == "get_call_detail":
            call_id = envelop.payload.get("call_id", "")
            if not call_id:
                envelop.payload = {"ok": False, "error": "call_id required"}
                return envelop
            flow = _get_or_create_flow(user_id,session_id)
            entries = flow._flow
            matched = []
            ai_entry = None
            for i, e in enumerate(entries):
                if e.get("from") == "system" and e.get("detail", {}).get("call_id") == call_id:
                    matched.append(e)
                    # 往前找最近的 ai entry（think 在它里面）
                    if ai_entry is None:
                        for j in range(i - 1, -1, -1):
                            if entries[j].get("from") == "ai":
                                ai_entry = entries[j]
                                break
            if not matched:
                envelop.payload = {"ok": False, "error": "call_id not found"}
                return envelop
            envelop.payload = {
                "ok": True,
                "entries": matched,
                "ai_entry": ai_entry,          # ★ 新增：调用发起时的 think + args
                "call_id": call_id,
            }
            return envelop


        if action == "clear_memory":
            flow = _get_or_create_flow(user_id, session_id)
            async with flow._write_lock:
                flow._flow = []
                await flow._compact_locked()
            await _update_conversation_index(user_id, session_id, [])
            envelop.payload = {"ok": True}
            return envelop

        if action == "list_conversations":
            index = _load_conversation_index(user_id)
            include_archived = envelop.payload.get("include_archived", False)
            items = []
            for cid, meta in index.items():
                if meta.get("archived") and not include_archived:
                    continue
                items.append({
                    "id": cid,
                    "title": meta.get("title", ""),
                    "created_at": meta.get("created_at", ""),
                    "updated_at": meta.get("updated_at", ""),
                    "message_count": meta.get("message_count", 0),
                    "last_preview": meta.get("last_preview", ""),
                    "archived": meta.get("archived", False),
                })
            items.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
            envelop.payload = {"ok": True, "conversations": items, "total": len(items)}
            return envelop

        if action == "new_conversation":
            new_sid = envelop.payload.get("session_id") or f"s_{uuid.uuid4().hex[:12]}"
            # ★ 不建目录、不建空 flow：等第一条消息 append_user 时由
            #   _append_line_locked 里的 flow_file.parent.mkdir 自动创建。
            #   否则「新建但没发消息」会留下空目录，且与真正发消息的 sid 不一致时变成孤儿目录。
            index = _load_conversation_index(user_id)
            if new_sid not in index:
                index[new_sid] = {
                    "title": "",
                    "created_at": datetime.now().isoformat(),
                    "updated_at": datetime.now().isoformat(),
                    "message_count": 0,
                    "entry_count": 0,
                    "archived": False,
                }
            else:
                index[new_sid]["updated_at"] = datetime.now().isoformat()
            _index_file(user_id).write_text(
                json.dumps({"conversations": index}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            envelop.payload = {"ok": True, "conversation_id": new_sid}
            return envelop

        if action == "rename_conversation":
            target_sid = envelop.payload.get("conversation_id") or session_id
            new_title = envelop.payload.get("title", "").strip()
            if not new_title:
                envelop.payload = {"ok": False, "error": "title 不能为空"}
                return envelop
            index = _load_conversation_index(user_id)
            if target_sid not in index:
                envelop.payload = {"ok": False, "error": "对话不存在"}
                return envelop
            index[target_sid]["title"] = new_title[:50]
            _index_file(user_id).write_text(json.dumps({"conversations": index}, ensure_ascii=False, indent=2), encoding="utf-8")
            envelop.payload = {"ok": True, "conversation_id": target_sid, "title": new_title}
            return envelop

        if action == "delete_conversation":
            target_sid = envelop.payload.get("conversation_id") or session_id
            await _purge_session(user_id, target_sid)
            envelop.payload = {"ok": True, "deleted": target_sid}
            return envelop

        if action == "get_state":
            state_type = envelop.payload.get("type", "exp")
            if state_type == "exp":
                data = _load_backpack(user_id)
                items = data.get("items", [])
                lines = []
                for it in items:
                    iid = it.get("id", "")
                    title = it.get("title", "")
                    summary = it.get("summary", "")
                    tags = it.get("tags", []) or []
                    tag_str = f"（tags: {', '.join(str(t) for t in tags)}）" if tags else ""
                    lines.append(f"- [{iid}] {title}{tag_str}")
                    if summary:
                        lines.append(f"  {summary}")
                content = "\n".join(lines) if lines else ""
                envelop.payload = {
                    "ok": True,
                    "data": {
                        "type": "exp",
                        "items": items,
                        "content": content,   # 人类可读文本（前端 editor 直接显示）
                        "size": len(content),
                        "count": len(items),
                    },
                }
                return envelop
            else:
                f = _get_taskboard_file(user_id, session_id)
                content = f.read_text(encoding="utf-8") if f.exists() else ""
                envelop.payload = {
                    "ok": True,
                    "data": {"type": state_type, "content": content, "size": len(content)},
                }
                return envelop

        if action == "save_state":
            state_type = envelop.payload.get("type", "exp")
            if state_type == "exp":
                envelop.payload = {
                    "ok": False,
                    "error": "经验背包已结构化，不支持直接编辑。请用 add_experience（append/replace）写入，或用 experience_loader 查看。",
                }
                return envelop
            content = envelop.payload.get("content", "")
            f = _get_taskboard_file(user_id, session_id)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content, encoding="utf-8")
            envelop.payload = {
                "ok": True,
                "data": {"type": state_type, "saved": True, "size": len(content)},
            }
            return envelop

        # 3. 文件处理
        if attachments:
            MAX_FILE_SIZE = 10 * 1024 * 1024
            MAX_FILES = 5
            if len(attachments) > MAX_FILES:
                envelop.payload = {"ok": False, "error": f"文件数量超过限制（最多 {MAX_FILES} 个）"}
                return envelop
            oversized = []
            valid_attachments = []
            for att in attachments:
                size = att.get("size", 0)
                path = att.get("path", "")
                name = att.get("name", "未知文件")
                if size == 0 and path:
                    try:
                        size = Path(path).stat().st_size
                    except:
                        pass
                if size > MAX_FILE_SIZE:
                    oversized.append(f"{name} ({size/1024/1024:.1f}MB)")
                else:
                    valid_attachments.append(att)
            if oversized:
                envelop.payload = {"ok": False, "error": f"以下文件超过大小限制: {', '.join(oversized)}"}
                return envelop
            flow = _get_or_create_flow(user_id,session_id)
            file_lines = []
            for att in valid_attachments:
                name = att.get('name', '未知')
                size = att.get('size', 0)
                path = att.get('path', '')
                file_lines.append(f"- {name} ({size/1024:.1f}KB) 路径: {path}")
            file_think = "\n".join(file_lines)
            await flow.append_system(action="files_uploaded", result_think=f"用户上传了 {len(valid_attachments)} 个文件", detail={"files": valid_attachments})
            if not user_input:
                user_input = f"用户上传了 {len(valid_attachments)} 个文件，请处理：\n{file_think}"
            else:
                user_input = f"{user_input}\n\n[已上传文件]\n{file_think}"

        # 4. 空输入
        if not user_input:
            envelop.payload = {"ok": False, "error": "请输入内容"}
            return envelop

        # 5. 打断
        if envelop.sender == "os/_gateway" and session_id in core_processor._think_sessions:
            core_processor.signal_interrupt(session_id)
            await _push_progress(agent,user_id, session_id, "executing", "⏸️ 中断中...",
                extra={"status": "interrupting"})
            envelop.payload = {"ok": True, "waiting": True, "call": "interrupt", "content": "", "args": {}}
            return envelop

        # ★★★ 分身场景同步跑完，返回最终 reply ★★★
        if envelop.meta.get("_task_id"):
            description = envelop.meta.get("_description", "")
            Logger.info(f"[main_agent] 分身同步执行 session={session_id}")
            await _push_progress(agent, user_id, session_id, "thinking", "🧠 正在思考...",
                    extra={"description": description})
            flow = _get_or_create_flow(user_id, session_id)
            await flow.append_user(user_input)
            if hasattr(agent.llm, "reset_round"):
                agent.llm.reset_round(session_id)
            core_processor._think_sessions.add(session_id)
            try:
                async with _get_processing_lock(session_id):
                    result = await core_processor.process(agent, user_id, session_id, flow, 0)
            except ConnectionError as e:
                # ★ 网络中断：不删 flow，标记状态，返回明确错误
                Logger.warn(f"[sub-agent][network] {str(e)[:150]}")
                try:
                    await flow.mark_interrupted("network", str(e))
                    side = [
                        f"##{e2.get('round_no')}.{e2.get('chain_no')}"
                        for e2 in flow._flow
                        if e2.get("action") == "call_end" and (e2.get("chain_no") or 0) > 0
                        and any(str((e2.get("detail") or {}).get("target", "")).startswith(t)
                                for t in SIDE_EFFECT_TARGETS)
                    ]
                    hint = f"已完成的写操作：{', '.join(side[:5])}。" if side else ""
                except Exception:
                    hint = ""
                result = {
                    "ok": False,
                    "call": "reply",
                    "content": f"⚠️ 网络中断，现场已保留。{hint}说「继续」我会接着做。",
                    "args": {},
                }
            except ContentSafetyBlockedError as e:
                Logger.warn(f"[sub-agent][safety] {str(e)[:150]}")
                try:
                    await flow.mark_interrupted("llm_error", str(e))
                except Exception:
                    pass
                result = {
                    "ok": False,
                    "call": "reply",
                    "content": f"⚠️ LLM 调用失败，现场已保留。错误：{str(e)[:200]}",
                    "args": {},
                }
            except Exception as e:
                # 其它异常：也保留现场，不删
                Logger.error(f"[sub-agent][error] {e}", e)
                try:
                    await flow.mark_interrupted("llm_error", str(e))
                except Exception:
                    pass
                result = {
                    "ok": False,
                    "call": "reply",
                    "content": f"❌ 分身执行异常：{str(e)[:200]}",
                    "args": {},
                }
            finally:
                core_processor._think_sessions.discard(session_id)
                core_processor._pending_callbacks.pop(session_id, 0)
                core_processor.clear_interrupt(session_id)
            envelop.payload = {
                "ok": True,
                "call": result.get("call", "reply"),
                "content": result.get("content", ""),
                "args": {},
            }
            return envelop

        # 6. 正常流程（普通对话，异步）
        Logger.info(f"用户输入 [{session_id}]: {user_input[:60]}")
        await _push_progress(agent, user_id,session_id, "thinking", "🧠 正在思考...")
        flow = _get_or_create_flow(user_id,session_id)
        await flow.append_user(user_input)
        if hasattr(agent.llm, "reset_round"):
            agent.llm.reset_round(session_id)
        # ★ 新 user 消息时，清零 empty_output 重试计数
        retry_key = f"{session_id}:empty_output"
        if hasattr(core_processor, "_empty_retries"):
            core_processor._empty_retries[retry_key] = 0
        core_processor._think_sessions.add(session_id)
        asyncio.create_task(_run_processing(agent, user_id, session_id, flow))
        envelop.payload = {
            "ok": True, "waiting": False, "call": "processing",
            "content": "", "status": "processing", "args": {},
        }
        return envelop

    except Exception as e:
        error_str = str(e)
        try:
            user_id, session_id = resolve_ids(envelop)
        except Exception:
            user_id, session_id = "local", "default"
        flow = _get_or_create_flow(user_id,session_id)

        is_llm_error = (
            isinstance(e, (ContentSafetyBlockedError, ConnectionError))
            or any(error_str.startswith(p) for p in LLM_ERROR_PREFIXES)
        )
        if is_llm_error:
            Logger.warn(f"LLM 调用失败: {error_str[:150]}")
        else:
            Logger.error(f"主入口执行异常: {e}", e)

        if is_llm_error:
            try:
                await flow.mark_interrupted("llm_error", error_str)
            except Exception:
                pass
            msg = (
                f"⚠️ LLM 调用失败，现场已保留。\n"
                f"错误：{error_str[:300]}\n\n"
                f"说「继续」我会接着做，说「重来」我会重新开始。"
            )
            await _push_chat(agent, user_id, session_id, msg)
            envelop.payload = {"ok": False, "waiting": False, "call": "reply",
                               "content": "", "error": msg,
                               "args": {"error": error_str}}
            return envelop

        await _push_chat(agent, user_id,session_id, f"❌ 系统错误: {error_str}")
        envelop.payload = {"ok": False, "waiting": False, "call": "reply", "content": "", "error": error_str, "args": {}}
        return envelop


async def _handle_callback(envelop, agent):
    user_id, session_id = resolve_ids(envelop)
    session_id = envelop.meta.get("callback_session_id") or session_id
    user_id = envelop.meta.get("user_id") or user_id

    flow = _get_or_create_flow(user_id, session_id)
    trace_id = envelop.meta.get("trace_id", envelop.trace_id)
    call_id = envelop.meta.get("_call_id", "")
    task_id = envelop.meta.get("_task_id", "")
    task_summary = envelop.meta.get("_task_summary", "")

    payload = envelop.payload
    if isinstance(payload, dict):
        content = payload.get("content", "") or payload.get("reply", "") or json.dumps(payload, ensure_ascii=False)
    else:
        content = str(payload)

    # ★ 改用 append_async_callback：追加新 ##，带 related，不碰已闭合 ##
    try:
        chain_idx = await flow.append_async_callback(
            trace_id=trace_id,
            payload=payload,
            trace_meta={"call_id": call_id, "task_id": task_id, "task_summary": task_summary},
        )
        Logger.info(f"[callback] 追加新链 {chain_idx} trace={trace_id[:8] if trace_id else ''}")
    except Exception as e:
        Logger.error(f"[callback] append_async_callback 失败: {e}", e)

    await _push_progress(agent, user_id, session_id, "callback",
                         f"📡 任务完成（trace={trace_id[:8] if trace_id else ''}）")

    ok = True
    final_content = ""
    core_processor._think_sessions.add(session_id)
    try:
        result = await core_processor.process(agent, user_id, session_id, flow)
        final_content = result.get("content", "")
        if result.get("interrupted"):
            final_content = ""
        elif final_content:
            await _push_chat(agent, user_id, session_id, final_content)
    except ConnectionError as e:
        ok = False
        Logger.warn(f"[callback][network] {str(e)[:150]}")
        try:
            await flow.mark_interrupted("network", str(e))
        except Exception:
            pass
        await _push_chat(agent, user_id, session_id,
                         "⚠️ 回调处理时网络中断，现场已保留。")
    except Exception as e:
        ok = False
        Logger.error(f"回调处理异常: {e}", e)
        await _push_chat(agent, user_id, session_id, f"❌ 回调处理失败: {e}")
    finally:
        core_processor._think_sessions.discard(session_id)
        core_processor._pending_callbacks.pop(session_id, 0)
        core_processor.clear_interrupt(session_id)

    envelop.payload = {"ok": ok, "content": final_content}
    return envelop


def help():
    return {
        "route": "builtins/agents/main_agent",
        "description": "AICP 主控制台 Agent — tool use 版（reply + use_tool 两个 tool）",
        "tools": ["reply", "use_tool", "manage_flow"],
        "features": [
            "原生 tool use（chat_tool_stream），无 JSON 解析 / 熔断 / @@CONTENT@@",
            "两个 tool：reply（回复）和 use_tool（调插件）",
            "信息流 / 经验背包 / 任务看板 / 分身任务摘要全保留",
            "回调处理 / 系统 action / 文件上传 / 异常回退全保留",
            "自调用拦截 / callback 参数提取全保留",
            "推送补全：thinking / summary_stream / raw / executing / done / error / callback",
        ]
    }
