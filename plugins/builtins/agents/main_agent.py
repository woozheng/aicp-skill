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
import hashlib
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


def _debug_dump_messages(flow, messages, depth):
    if not _DEBUG_CTX_ENABLED:
        return ""
    base = _conversations_dir(flow.user_id, flow.session_id) / _DEBUG_CTX_DIR_NAME
    base.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%H%M%S")
    fname = f"depth_{depth:02d}_{ts}.json"
    path = base / fname
    header = {
        "session_id": flow.session_id, "user_id": flow.user_id,
        "depth": depth, "timestamp": datetime.now().isoformat(),
        "flow_entries": len(flow._flow), "rounds": flow._round_counter,
        "current_turn": flow._current_turn, "message_count": len(messages),
        "msg_hashes": [
            {"idx": i, "role": m["role"],
             "hash": hashlib.md5(m["content"].encode("utf-8")).hexdigest()[:12],
             "len": len(m["content"])}
            for i, m in enumerate(messages)
        ],
    }
    path.write_text(
        "# Debug Messages\n\n" + json.dumps(header, ensure_ascii=False, indent=2)
        + "\n\n---\n\n" + json.dumps(messages, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    Logger.info(f"[debug] messages dumped → {path}")
    return str(path)


PROJECT = Path(__file__).parent.name
MEMORY_DIR = Path("data/memories/main_agent")
MEMORY_DIR.mkdir(parents=True, exist_ok=True)




MAX_RECURSION_DEPTH = 40

# ============================================================
# 上下文压力阈值 —— 超过则只给 manage_flow，强制去噪
# ============================================================
CONTEXT_PRESSURE_THRESHOLD = 300_000   # 字符数

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



class EmptyOutputError(RuntimeError):
    """LLM 流正常结束，但既没有 tool_call 也没有 text（模型只输出了 thinking）"""


class NarrationReplyError(RuntimeError):
    """模型把「下一步动作」当成了最终回复（旁白型 reply）。

    典型：内容是 "I'll now implement the P2 improvements. Let me start by..."
    这类句子没有交付任何结果，却让自递归提前终止。
    """
    pass
# ============================================================
# 异常
# ============================================================

class ContentSafetyBlockedError(RuntimeError):
    """LLM 调用失败（内容安全 / 网络 / 超时）"""
    pass


# ============================================================
# 旁白型 reply 检测
# ============================================================
# 模型经常在真正的工具调用之前先说一句「我要去读文件了」，并把它当成
# reply 发出来。这种 reply 没有交付任何结果，却让自递归提前终止。
# 判据：内容里只有「我要做什么」的宣告，没有「做完了什么」的结论。

# 英文动作动词：跟在 I'll / Let me / I want to 后面才算「下一步动作」
_EN_ACT = (r"read|check|look|open|search|inspect|analyz|analys|explor|locate|"
           r"review|examin|start|proceed|continue|implement|fix|update|replace|"
           r"wire|add|remove|refactor|mark|write|run|test|verify|collapse|"
           r"finish|apply|create|delete|move|copy|edit|scan|trace|debug|"
           r"plan|dive|dig|digging|go|try|attempt|begin|proceed")

# 中文动作动词
_ZH_ACT = (r"读|读一?下|看|查|查看|打开|检查|分析|探索|搜索|定位|检索|扫|列|列出|"
           r"改|修改|修复|写|写入|创建|新建|生成|实现|执行|运行|跑|测试|验证|确认|"
           r"梳理|整理|处理|解决|尝试|着手|进行|推进|实施|替换|接入|继续|收起|折叠|"
           r"计划|开始|动手|动笔|深入|研究")

_NARRATION_PATTERNS = (
    # 英文：I'll / I will / Let me + （可隔一两个词）+ 动作动词
    r"\b(?:i'?ll|i will|i'?m going to|now i'?ll)\s+(?:\w+\s+){0,2}(?:" + _EN_ACT + r")\w*\b",
    r"\blet\s+me\s+(?:\w+\s+){0,2}(?:" + _EN_ACT + r")\w*\b",
    # 英文：句首 I + 动作动词
    r"^\s*i\s+(?:" + _EN_ACT + r")\w*\b",
    # 中文：我要 / 我先 / 接下来我 / 下面我 / 现在我 / 下一步我 + 动作动词
    r"(?:^|[。！？；\s])(?:我(?:要|将|来)?|接下来|下面|现在|下一步)(?:我)?"
    r"(?:来|去|先|再|接着|继续|开始|准备)?\s*"
    r"(?:" + _ZH_ACT + r")",
    # 中文：我要 / 我继续 / 接下来 开头（后接动作）
    r"^\s*(?:我要|我先|我继续|我接着|我接下来|接下来|下面我|现在我|下一步我|我准备)\s*"
    r"(?:" + _ZH_ACT + r")",
    r"^\s*no\s+\w+\s+(?:requested|needed|required)\b",
)

# 白名单：出现这些词说明这是真交付（结论/产物），不该拦
# 英文用词边界匹配，避免 "P2 changes" 里的 changes 误判
_NARRATION_ALLOW_EN = (
    "done", "completed", "fixed", "implemented", "verified", "summary",
    "conclusion", "findings", "results", "issues", "changes made",
    "what i did", "i've ", "i have ", "here's", "here is",
)
_NARRATION_ALLOW_ZH = (
    "已完成", "完成了", "已改", "已实现", "已修复", "已新增", "已删除",
    "已验证", "已通过", "已测试", "已生成", "已写入", "已保存", "已创建",
    "改动清单", "总结", "结论", "结果如下", "如下：", "问题清单", "缺陷",
    "建议如下", "原因如下", "已修好",
)

# 交付信号：出现这些结构说明内容不只是「我要去做」
_DELIVERY_SIGNALS = (
    "\n#", "\n##", "\n- ", "\n* ", "\n1.", "\n|", "```",
)


def _has_delivery_structure(text: str) -> bool:
    """内容里有列表/标题/代码块等结构 → 是在交付，不该拦。"""
    for sig in _DELIVERY_SIGNALS:
        if sig in text:
            return True
    return False


def is_narration_reply(content: str) -> bool:
    """判断一个 reply 是否只是「我要去做什么」的宣告（旁白），而非交付结果。

    返回 True 表示这是一次旁白型 reply，应当继续递归而不是结束。
    """
    if not content:
        return False
    text = content.strip()
    if not text:
        return False

    # 太长的内容通常包含实质交付，放行（旁白一般很短）
    if len(text) > 400:
        return False

    # 有交付结构 → 放行
    if _has_delivery_structure(text):
        return False

    low = text.lower()

    # 白名单命中 → 是真交付，放行
    for w in _NARRATION_ALLOW_ZH:
        if w in text:
            return False
    for w in _NARRATION_ALLOW_EN:
        if re.search(r"\b" + re.escape(w) + r"\b", low):
            return False

    for pat in _NARRATION_PATTERNS:
        try:
            if re.search(pat, text, re.IGNORECASE | re.MULTILINE):
                return True
        except re.error:
            continue
    return False

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











def _get_taskboard_file(user_id: str, session_id: str) -> Path:
    return _conversations_dir(user_id, session_id) / "taskboard.txt"


def _load_taskboard(user_id: str, session_id: str) -> str:
    f = _get_taskboard_file(user_id, session_id)
    if f.exists():
        return f.read_text(encoding="utf-8").strip()
    return ""











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
        if not meta.get("description"):
            for e in flow:
                if e.get("from") == "user" and e.get("content"):
                    meta["description"] = " ".join(str(e["content"]).split())[:120]
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
        # ★ 分身会话：记录父会话，供前端把分身挂到「发起它的对话」下面
        if session_id.startswith("sub_"):
            _ps = _SUB_PARENTS.get(session_id, "")
            if _ps:
                meta["parent_session"] = _ps
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





# 可配置基线
BASE_ROUNDS = 8          # 降级时固定取最近 8 轮；正常由动态基线决定（见 _detect_baseline_rounds）
FORCE_FOLD_THRESHOLD = 220_000   # context 字符数，超过就强制提示模型折叠
# 发现类工具：只给条数，不列字段内容


class InformationFlow:
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
        self._ignored: Dict[str, int] = {}    # index -> 到期 turn

        # 落盘相关（沿用）
        self._write_lock = asyncio.Lock()
        self._append_fh = None
        self._append_fh_path = None
        self._appended_lines = 0
        self._last_compact_at = 0.0
        self._current_baseline = None   # 动态基线：None=降级固定轮，[]=新任务，[3,10,...]=选中轮
        self._msg_cache: Dict[int, List[dict]] = {}   # round_no -> [user_msg, assistant_msg]
        self._dirty_rounds: set = set()               # manage_flow 对历史轮操作时标记
        self._round_snapshots: Dict[int, dict] = {}
        # {round_no: {"collapsed": [...], "ignored": [...]} —— 本轮结束时的折叠形态

        self._task_summary = ""          # ← 新增
        self._picked_skills = []         # ← 新增

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

    def _gc_ignored(self):
        """清理过期 TTL。"""
        cur = self._current_turn
        self._ignored = {k: v for k, v in self._ignored.items() if v > cur}

    async def manage_flow(self, collapse=None, ignore=None) -> dict:
        """本轮减压阀：只做减法（collapse / ignore）。不展开、不增内容。"""
        collapse = collapse or []
        ignore = ignore or []
        self._current_turn += 1

        applied = {"collapse": [], "ignore": [], "invalid": []}

        for idx in collapse:
            s = self._norm_index(idx)
            if not s or not self._index_exists(s):
                applied["invalid"].append(idx); continue
            self._ignored[s] = self._current_turn + 999
            applied["collapse"].append(s)

        for idx in ignore:
            s = self._norm_index(idx)
            if not s or not self._index_exists(s):
                applied["invalid"].append(idx); continue
            self._ignored[s] = self._current_turn + 999
            applied["ignore"].append(s)

        self._gc_ignored()

        for idx in applied["collapse"] + applied["ignore"]:
            try:
                r = int(str(idx).split(".")[0])
                if r != self._round_counter:
                    self._dirty_rounds.add(r)
            except (ValueError, IndexError):
                pass

        parts = []
        if applied["collapse"]: parts.append(f"收起 {len(applied['collapse'])} 条")
        if applied["ignore"]:   parts.append(f"关闭 {len(applied['ignore'])} 条")
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
                # ★ 恢复历史轮折叠快照
                for e in self._flow:
                    if e.get("action") == "round_snapshot":
                        r = e.get("round_no")
                        if r:
                            d = e.get("detail") or {}
                            self._round_snapshots[r] = {
                                "collapsed": d.get("collapsed", []) or [],
                                "ignored": d.get("ignored", []) or [],
                            }
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
                            extra: dict = None, retain: int = 1, chain_no: int = None):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "from": "system",
            "action": action,
            "result": result_think,
            "detail": detail or {},
            "retain": retain,
            "round_no": self._round_counter,
            "chain_no": chain_no if chain_no is not None else self._chain_counter,
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
        """编号归一化：去掉前缀 # 和空白。"""
        return str(idx or "").lstrip("#").strip()

    def _is_ignored(self, idx: str) -> bool:
        s = self._norm_index(idx)
        exp = self._ignored.get(s)
        return bool(exp) and exp > self._current_turn

    # ============================================================
    # 渲染辅助
    # ============================================================



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


    @staticmethod
    def _chain_status(chain_entries: List[dict]) -> str:
        for e in chain_entries:
            if e.get("action") == "call_error":
                return "failed"
        for e in chain_entries:
            if e.get("action") == "call_end":
                return "ok"
        return "pending"


    def _chain_target_action(self, chain_entries: List[dict]) -> Tuple[str, str]:
        for e in chain_entries:
            if e.get("action") == "call_start":
                d = e.get("detail") or {}
                t = d.get("target", "") or ""
                p = d.get("params") or {}
                # action 真实位置是 detail.action；params.action 只是个别插件的写法
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

    def _render_skill_header(self) -> str:
        """本轮任务可能用到的 skill（从 flow._task_summary / flow._picked_skills 读）。

        区分：
            - 预选（pinned=False）：只给 name + desc，模型按需 use_tool 读全文
            - 钉住（pinned=True）：给 name + desc + full_text 全文（> 引用块包裹）

        钉住的 skill 不受 8 轮 / baseline 影响：只要 pinned.json 里 status == "pinned"，
        就渲染全文。多个钉住时按 pinned.json 的 dict 顺序依次渲染，总预算 50KB。
        """
        task_summary = getattr(self, "_task_summary", "") or ""
        picked_skills = getattr(self, "_picked_skills", []) or []
        if not task_summary and not picked_skills:
            return ""

        pinned = [s for s in picked_skills if s.get("pinned")]
        normal = [s for s in picked_skills if not s.get("pinned")]

        lines = ["【当前任务可能用到的技能】"]
        if task_summary:
            lines.append(f"任务意图：{task_summary}")

        # ---- 预选：只给 name + desc ----
        if normal:
            lines.append("可能相关（按需 use_tool(target=\"skill/<name>\") 读取全文）：")
            for sk in normal:
                name = sk.get("name", "")
                desc = sk.get("desc", "")
                if name:
                    lines.append(f"- {name} — {desc}" if desc else f"- {name}")

        # ---- 钉住：给全文（总预算 50KB）----
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
                        lines.append(f"（已省略后续 {len(pinned) - i} 个钉住技能的全文，超出总预算 {total_budget} 字）")
                        break
                    # 单个最多 20KB，且不超总预算剩余
                    cut = min(20000, remaining)
                    if len(full) > cut:
                        full = full[:cut] + f"\n…（截断，全文 {len(full)} 字，剩余预算 {remaining} 字）"
                    for ln in full.splitlines():
                        lines.append(("> " + ln) if ln else ">")
                    used += len(full)
                else:
                    lines.append("（无全文，请用 use_tool(target=\"skill/...\") 读取）")

        return "\n".join(lines)

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

        # ============================================================
        # 1. 选轮：基线固定（每轮用户消息构造一次，递归中不动）
        # ============================================================
        if baseline is None or not baseline:
            # ★ 降级 / 新任务：带最近 BASE_ROUNDS 轮（<8 轮时就是所有轮）
            selected = rounds[-BASE_ROUNDS:]
        else:
            # ★ LLM 选了相关轮：按 baseline 选
            baseline_set = set(baseline) | {last_r_no}
            selected = [r for r in rounds if (r[0].get("round_no") or 0) in baseline_set]

        # ============================================================
        # 2. 强制补「上一轮」（last_r_no - 1），供全展开示范
        # ============================================================
        if last_r_no > 1:
            prev_r_no = last_r_no - 1
            if not any((r[0].get("round_no") or 0) == prev_r_no for r in selected):
                prev_round = next(
                    (r for r in rounds if (r[0].get("round_no") or 0) == prev_r_no),
                    None
                )
                if prev_round:
                    selected.append(prev_round)

        # ============================================================
        # 3. 显式去重（保险）：按 round_no 去重，保留第一次出现
        # ============================================================
        seen = set()
        deduped = []
        for r in selected:
            r_no = r[0].get("round_no") or 0
            if r_no in seen:
                continue
            seen.add(r_no)
            deduped.append(r)
        selected = deduped
        selected.sort(key=lambda r: r[0].get("round_no") or 0)

        msgs: List[dict] = []

        # ============================================================
        # 4. 技能列表（最前，system prompt 之后）
        # ============================================================
        skill_header = self._render_skill_header()
        if skill_header:
            msgs.append({"role": "user", "content": skill_header})

        # ============================================================
        # 5. 省略提示
        # ============================================================
        total = len(rounds)
        omitted = total - len(selected)
        if omitted > 0 and len(selected) > 1:
            msgs.append({"role": "user", "content":
                f"（{omitted} 轮历史已省略"})

        # ============================================================
        # 6. 渲染选中轮
        #   - 当前轮（is_last）：  #N + ##N.M 全量 + tail
        #   - 上一轮（is_prev）：  #N + ##N.M 全量（不含 tail）
        #   - 其他选中轮：         #N 级（user + reply）
        # ============================================================
        for entries in selected:
            r_no = entries[0].get("round_no") or 0
            is_last = (r_no == last_r_no)
            is_prev = (r_no == last_r_no - 1)

            user_text = self._render_round_user(entries)
            reply_text = self._render_round_reply(entries)

            block: List[dict] = []
            if user_text:
                block.append({"role": "user", "content": user_text})
            if reply_text and not is_last:
                block.append({"role": "assistant", "content": reply_text})

            if is_last or is_prev:
                # ---- 当前轮 / 上一轮：全展开（##N.M 全量）----
                msgs.extend(block)
                chains = self._group_chains(entries)
                for c_no in sorted(chains.keys()):
                    full = self._render_chain_full(chains[c_no], r_no, c_no)
                    if full:
                        msgs.append({"role": "user", "content": full})
                # 只有当前轮带 tail（manage_flow 记录）
                if is_last:
                    tail = self._render_current_tail(entries)
                    if tail:
                        msgs.append({"role": "user", "content": tail})
            else:
                # ---- 其他选中轮：优先用固化快照，没有则默认 ----
                snap = self._round_snapshots.get(r_no)
                if snap is not None:
                    user_text = self._render_round_user(entries)
                    reply_text = self._render_round_reply_with_snapshot(entries, snap)
                    if user_text:
                        msgs.append({"role": "user", "content": user_text})
                    if reply_text:
                        msgs.append({"role": "assistant", "content": reply_text})
                else:
                    msgs.extend(block)

        return msgs

    def _render_chain_full(self, chain_entries: List[dict], r_no: int, c_no: int) -> str:
        """渲染一条工具链的完整结果（当前轮用），供模型看到工具返回内容。

        格式：
            【工具结果 ##N.M】target.action(params)
            目的：发起这次调用的 think（≤100 字，超出兜底截断）
            入参：call_start 的 params
            返回：call_end 的 result（skill 全文 / 普通 JSON / 错误）
        """
        if not chain_entries:
            return ""

        target, action = self._chain_target_action(chain_entries)
        label = f"{target}.{action}" if action and action != "none" else target

        # ---- 发起这次调用的 think（收起 / 未收起都要带）----
        chain_think = ""
        for e in chain_entries:
            if e.get("from") == "ai" and e.get("call") == "use_tool":
                chain_think = (e.get("think") or "").strip()
                break
        chain_think = chain_think.replace("\n", " ").strip()
        if len(chain_think) > 100:
            chain_think = chain_think[:100] + "…"

        # ★ 被 ignore / collapse 的链：保留锚点 + 目的，只省掉入参/返回/步骤 JSON。
        #   文本必须字节稳定（不含 TTL 等动态值），否则 TTL 期间每轮都改写这一条，前缀失效。
        if self._is_ignored(f"##{r_no}.{c_no}"):
            return (
                f"【工具结果 ##{r_no}.{c_no}】{label}\n"
                + (f"目的：{chain_think}\n" if chain_think else "")
                + f"（已收起，内容已释放；如确需原文，按坐标用 read_file_lines 重新读取）"
            )

        lines = [f"【工具结果 ##{r_no}.{c_no}】{label}"]
        if chain_think:
            lines.append(f"目的：{chain_think}")

        # ---- 入参 ----
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

        # ---- 返回：错误 / skill 全文 / 普通 JSON ----
        end_result = None
        end_error = None
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
            skill_body = None
            skill_path = ""
            if isinstance(end_result, dict):
                data = end_result.get("data")
                if isinstance(data, dict) and isinstance(data.get("content"), str):
                    skill_body = data["content"]
                    skill_path = str(data.get("path") or "")
                if skill_body is None:
                    plugin = str(end_result.get("plugin", "") or "")
                    src = str(end_result.get("source", "") or "")
                    if plugin.startswith("skill/") or src == "local_skill":
                        if isinstance(end_result.get("usage"), str):
                            skill_body = end_result["usage"]
                        skill_path = skill_path or str(end_result.get("usage_file") or "")

            if skill_body is not None:
                if len(skill_body) > 20000:
                    skill_body = skill_body[:20000] + f"\n…（截断，全文 {len(skill_body)} 字）"
                head = f"skill 全文（{skill_path}）：" if skill_path else "skill 全文："
                lines.append(head)
                for ln in skill_body.splitlines():
                    lines.append(("> " + ln) if ln else ">")
                rest = {}
                if isinstance(end_result, dict):
                    data = end_result.get("data")
                    if isinstance(data, dict):
                        if data.get("size") is not None:
                            rest["size"] = data["size"]
                        if data.get("body_size") is not None:
                            rest["body_size"] = data["body_size"]
                    for k, v in end_result.items():
                        if k in ("content", "usage", "data", "contract", "tools"):
                            continue
                        rest[k] = v
                if rest:
                    try:
                        lines.append(f"（其它：{json.dumps(rest, ensure_ascii=False, default=str)}）")
                    except Exception:
                        pass
            else:
                try:
                    r_str = json.dumps(end_result, ensure_ascii=False, default=str)
                except Exception:
                    r_str = str(end_result)
                lines.append(f"返回：{r_str}")
        else:
            lines.append("（执行中，无返回）")

        return "\n".join(lines)

    def _render_round_user(self, entries: List[dict]) -> str:
        """渲染一轮的 user 部分。多轮消息下不需要 > 引用块，直接原文。"""
        for e in entries:
            if e.get("from") == "user" and (e.get("content") or ""):
                return f"👤 用户：{e['content']}"
        return ""


    def _render_round_reply(self, entries: List[dict]) -> str:
        """渲染一轮的 assistant 部分 —— reply 原文 +（可选）think。"""
        reply = ""
        think = ""
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
            think_line = f"（think：{t}）"

        if think_line:
            return think_line + "\n\n" + reply
        return reply

    def _render_current_tail(self, entries: List[dict]) -> str:
        # manage_flow 现在走正常 ## 链渲染，不需要额外的轮级 tail。
        return ""


    # ============================================================
    # get_context 的辅助方法
    # ============================================================




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

            # E. manage_flow 专属摘要
            if action == "manage_flow":
                cb = data.get("ctx_before")
                ca = data.get("ctx_after")
                applied = data.get("applied") or {}
                n_collapse = len(applied.get("collapse") or [])
                n_ignore = len(applied.get("ignore") or [])
                bits = []
                if n_collapse:
                    bits.append(f"收起 {n_collapse} 条")
                if n_ignore:
                    bits.append(f"忽略 {n_ignore} 条")
                if cb is not None and ca is not None:
                    bits.append(f"上下文 {cb:,} → {ca:,}")
                if bits:
                    return " → " + " · ".join(bits)

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






    def estimate_context_chars(self) -> int:
        """估算当前 flow 渲染成 messages 后的总字符数。"""
        try:
            msgs = self.get_messages()
        except Exception:
            return 0
        return sum(len(m.get("content") or "") for m in msgs)

    async def snapshot_round(self, round_no: int) -> None:
        """把当前轮的折叠形态固化进 flow（reply 时调用）。"""
        collapsed = [
            k.lstrip("#") for k, v in self._ignored.items()
            if v > self._current_turn and k.count(".") == 1
        ]
        ignored = [
            k.lstrip("#") for k, v in self._ignored.items()
            if v > self._current_turn and k.count(".") == 2
        ]
        self._round_snapshots[round_no] = {"collapsed": collapsed, "ignored": ignored}
        await self.append_system(
            action="round_snapshot",
            result_think=f"📌 固化第 {round_no} 轮折叠形态",
            detail={"collapsed": collapsed, "ignored": ignored},
            retain=1,
            chain_no=0,
        )

    def _render_round_reply_with_snapshot(self, entries: List[dict], snap: dict) -> str:
        """按固化快照渲染历史轮的 assistant 部分。"""
        reply = ""
        for e in entries:
            if e.get("from") == "ai" and e.get("call") == "reply" and (e.get("content") or ""):
                reply = e["content"]
                break
        if not reply:
            return ""

        collapsed = set(snap.get("collapsed", []) or [])
        r_no = entries[0].get("round_no") or 0

        lines = [reply, ""]
        chains = self._group_chains(entries)
        for c_no in sorted(chains.keys()):
            chain_entries = chains[c_no]
            chain_key = f"{r_no}.{c_no}"
            t, a = self._chain_target_action(chain_entries)
            status = self._chain_status(chain_entries)
            icon = {"ok": "✅", "failed": "❌", "pending": "⏳"}.get(status, "")
            hint = self._chain_result_hint(chain_entries)

            if chain_key in collapsed:
                h = (hint or "").strip(" →").strip()
                if len(h) > 70:
                    h = h[:70] + "…"
                lines.append(f"## {r_no}.{c_no} {icon} {t}.{a}" + (f" → {h}" if h else ""))
            else:
                lines.append(f"## {r_no}.{c_no} {icon} {t}.{a}{hint}")

        return "\n".join(lines)

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

# ★ 分身 → 父会话 映射（sub_xxx → 父 session_id）
#   用途：console 左侧会话列表把分身挂到「发起它的对话」下面
_SUB_PARENTS: Dict[str, str] = {}


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

async def _push_progress(agent, user_id, session_id, step, msg, extra=None):
    # ★ 本 session 已确认无前端：直接短路
    if _WS_DEAD.get(session_id, False):
        return 0
    data = {"type": "progress", "session_id": session_id, "step": step, "msg": msg}
    if extra:
        data.update(extra)
    task = asyncio.ensure_future(agent.system.call(core.Envelop(
        sender="builtins/agents/main_agent",
        receiver="os/_websocket",
        meta={"session_id": session_id},
        payload={
            "action": "push",
            "channel_id": f"pa_{user_id}",
            "data": data,
        }
    )))
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=6.0)
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        Logger.warn(f"[WS] push_progress 超时 6s，已放弃")
        return 0
    except Exception as e:
        Logger.warn(f"[WS] push_progress 失败: {type(e).__name__}: {e}")
        return 0
    # ★ 拿到 sent：0 说明频道无人，本回合后续不再推
    try:
        res = task.result() if task.done() else None
        payload = getattr(res, "payload", None) or {}
        sent = int(payload.get("sent") or 0)
    except Exception:
        sent = 0
    if sent == 0:
        _WS_DEAD[session_id] = True
    return sent


async def _push_chat(agent, user_id, session_id, content):
    if _WS_DEAD.get(session_id, False):
        return 0
    task = asyncio.ensure_future(agent.system.call(core.Envelop(
        sender="builtins/agents/main_agent",
        receiver="os/_websocket",
        meta={"session_id": session_id},
        payload={
            "action": "push",
            "channel_id": f"pa_{user_id}",
            "data": {"type": "chat", "session_id": session_id, "content": content},
        }
    )))
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=6.0)
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        Logger.warn(f"[WS] 推送 chat 超时 6s，已放弃")
        return 0
    except Exception as e:
        Logger.warn(f"[WS] 推送消息失败: {type(e).__name__}: {e}")
        return 0
    try:
        res = task.result() if task.done() else None
        payload = getattr(res, "payload", None) or {}
        sent = int(payload.get("sent") or 0)
    except Exception:
        sent = 0
    if sent == 0:
        _WS_DEAD[session_id] = True
    return sent

async def _push_stream(agent, user_id: str, session_id: str, chunk: str):
    if _WS_DEAD.get(session_id, False):
        return 0
    task = asyncio.ensure_future(agent.system.call(core.Envelop(
        sender="builtins/agents/main_agent",
        receiver="os/_websocket",
        meta={"session_id": session_id},
        payload={
            "action": "push",
            "channel_id": f"pa_{user_id}",
            "data": {"type": "summary_stream", "session_id": session_id, "chunk": chunk},
        }
    )))
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=6.0)
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        Logger.warn(f"[WS] 流式推送超时 6s，已放弃")
        return 0
    except Exception as e:
        Logger.warn(f"[WS] 流式推送失败: {type(e).__name__}: {e}")
        return 0
    try:
        res = task.result() if task.done() else None
        payload = getattr(res, "payload", None) or {}
        sent = int(payload.get("sent") or 0)
    except Exception:
        sent = 0
    if sent == 0:
        _WS_DEAD[session_id] = True
    return sent
# ★ 后台推送任务强引用集合：asyncio 只持弱引用，不存引用会被 GC 回收
_PUSH_BG_TASKS: set = set()
_PUSH_BG_MAX = 256   # 上限保护：WS 长期异常时不无限堆积

# ★ 无前端短路：session_id -> True 表示本回合已确认无 WS 连接，后续推送直接跳过
#   初始不存在（视作「有前端」）；第一次 push 返回 sent=0 时置 True；回合结束清理。
_WS_DEAD: Dict[str, bool] = {}


def _push_stream_nowait(agent, user_id: str, session_id: str, chunk: str):
    """后台推送流式片段，不阻塞调用方（用于 LLM 流消费循环内）。

    与 _push_stream 的区别：不 await、不抛异常。
    内部仍有 6s 超时兜底（在 _push_stream 里），所以任务不会永久挂住。
    """
    # ★ 本 session 已确认无前端：不创建任务
    if _WS_DEAD.get(session_id, False):
        return
    try:
        if len(_PUSH_BG_TASKS) >= _PUSH_BG_MAX:
            Logger.warn(f"[WS] 后台推送任务堆积达 {_PUSH_BG_MAX}，丢弃本帧")
            return
        task = asyncio.create_task(
            _push_stream(agent, user_id, session_id, chunk)
        )
    except Exception as e:
        Logger.warn(f"[WS] 后台推送创建失败: {type(e).__name__}: {e}")
        return
    _PUSH_BG_TASKS.add(task)
    task.add_done_callback(_PUSH_BG_TASKS.discard)


# ============================================================
# 流式推送：合并 + 异步
# ============================================================
_STREAM_BUFFERS: Dict[str, list] = {}       # session_id -> [chunk, ...]
_STREAM_FLUSH_TASKS: Dict[str, asyncio.Task] = {}
_STREAM_FLUSH_INTERVAL = 0.3                 # 100ms 合并一次
_STREAM_BUFFER_MAX = 128                      # buffer 超过 64 个 chunk 强制 flush


async def _flush_stream_buffer(agent, user_id: str, session_id: str):
    if _WS_DEAD.get(session_id, False):
        # 已确认无前端：清空 buffer，不再推
        _STREAM_BUFFERS.pop(session_id, None)
        return
    buf = _STREAM_BUFFERS.get(session_id)
    if not buf:
        return
    text = "".join(buf)
    buf.clear()
    if not text:
        return
    try:
        await asyncio.wait_for(
            _push_stream(agent, user_id, session_id, text),
            timeout=2.0,
        )
    except (asyncio.TimeoutError, Exception) as e:
        Logger.warn(f"[WS] flush 推送失败: {type(e).__name__}: {e}")


async def _flush_loop(agent, user_id: str, session_id: str):
    try:
        while True:
            await asyncio.sleep(_STREAM_FLUSH_INTERVAL)
            try:
                await asyncio.wait_for(
                    _flush_stream_buffer(agent, user_id, session_id),
                    timeout=3.0,
                )
            except (asyncio.TimeoutError, Exception) as e:
                Logger.warn(f"[WS] flush 超时/失败: {type(e).__name__}: {e}")
    except asyncio.CancelledError:
        try:
            await asyncio.wait_for(
                _flush_stream_buffer(agent, user_id, session_id),
                timeout=2.0,
            )
        except (asyncio.TimeoutError, Exception):
            pass
        raise


def _buffer_stream_chunk(agent, user_id:str,session_id: str, chunk: str):
    """把一个 chunk 塞进 buffer。超过阈值时立即 flush。"""
    if not chunk:
        return
    # ★ 本 session 已确认无前端：不 buffer、不 flush
    if _WS_DEAD.get(session_id, False):
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
            "【本轮递归内的减压阀】只在上下文过长、任务还没完成时由系统提供。\n"
            "它只做减法：把本轮已经用过、暂时不需要的中间结果折叠成一行（保留坐标），"
            "腾出空间继续跑。不展开、不增加内容。\n"
            "\n"
            "编号：#N 整轮、##N.M 工具链、###N.M.K 单步。\n"
            "\n"
            "## 语义\n"
            "  collapse([...])：把 ##N.M 折叠成一行（保留结果摘要 + 坐标）。\n"
            "  ignore([...])：把 ###N.M.K 完全不渲染（超长 stdout 的中间段等）。\n"
            "\n"
            "## 规则\n"
            "  1. 只折叠「本轮」重复读取 / 已用完的 ##N.M，不要折叠当前正在依赖的结果。\n"
            "  2. ignore 只用于 ###N.M.K，不要用于 #N 或 ##N.M。\n"
            "  3. 折叠后你会看到「上下文从 X 缩到 Y」，自己判断够不够；够就继续干活。\n"
            "  4. 本工具只在超阈值时出现；折叠完下一轮恢复正常工具集。\n"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "为什么折叠（可选）。会写进信息流，下一轮你能看到自己当时的理由。"
                },
                "collapse": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "折叠的 ##N.M 编号列表。"
                },
                "ignore": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "完全不渲染的 ###N.M.K 编号列表。"
                }
            },
            "required": []
        }
    }
}
TOOL_SCHEMAS_BASE = [
    {
        "type": "function",
        "function": {
            "name": "reply",
            "description": (
                "向用户回复最终结果或澄清问题。任务完成、需要用户确认、或无法继续时使用。"
                "⚠️ 不要用 reply 说「我要读文件 / 我要查一下 / 我要继续 / "
                "I'll now implement... / Let me check...」——"
                "这些是「下一步动作」，应该用 use_tool / manage_flow直接执行，不是回复用户。"
                "reply 一旦发出，任务就结束了；只宣告下一步动作会让任务半途而废。"
                "这类话请放进 use_tool 的 think 参数里。"
                 "默认按用户使用的语种输出，禁止双语输出相同内容"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "think": {
                        "type": "string",
                        "description": "一句话说明为什么调用这个工具（目的/意图），建议 ≤100 字。不写实现细节、不写代码。必须输出，语种跟随用户。"
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "给用户的回复内容 —— 必须是已经拿到的结果（结论、产物、清单、报错说明），"
                            "不能是「我接下来要做什么」的宣告。"
                        )
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
                    "调用系统内的一个插件：执行动作 / 读写文件 / 跑代码 / 造应用 / 推前端 / 查系统。"
                    "一次只调一个，拿到结果后再决定下一步。"
                    "⚠️ 文件内容（HTML/JS/CSS/代码）必须写在 plugin_args 里，不要写在 reasoning 里"
                    "（写长内容会被 max_tokens 截断，导致 tool_call 丢失）。"
                    "默认按用户使用的语种输出，禁止双语输出相同内容。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "think": {
                            "type": "string",
                            "description": "一句话说明为什么调用这个工具。小于100字，必须输出！语种默认用户语种"
                        },
                        "target": {
                            "type": "string",
                            "description": (
                                "插件路由，如 builtins/tools/file_utils_api、builtins/tools/code_runner、builtins/tools/save_blocks。"
                                        "skill/<rel> 用于读 data/skills 下的 skill 全文。"
                                    )
                        },
                        "action": {
                            "type": "string",
                            "description": (
                                "插件 action，必填。无 action 的插件传空字符串 \"\"，不要传 \"none\"。"
                                "不同 action 的必填 plugin_args 键不同；不确定时先用 "
                                "builtins/tools/contract_agent 查该插件的契约，再发调用。"
                            )
                        },
                        "plugin_args": {
                            "type": "object",
                            "description": (
                                "插件的全部参数，直接平铺在这里，没有子层，不要再嵌套 params。"
                                "不同 action 必填键不同，例如："
                                "file_utils_api.read_file 必填 {\"path\": \"runtime/_config.py\"}；"
                                "file_utils_api.read_file_lines 必填 {\"path\": \"...\", \"start_line\": 1, \"end_line\": 50}；"
                                "file_utils_api.edit_file  必填{\"path\": \"...\", \"find\": \"...\", \"replace\": \"...\"}；"
                                "file_utils_api.write_file  必填{\"path\": \"...\", \"content\": \"...\"}；"
                                "code_runner 必填 {\"code\": \"...\", \"packages\": []}。"
                                "漏必填键会报『缺少 X 参数』——下一发必须补上 X 重发，禁止原样重试。"
                            ),
                            "additionalProperties": True
                        },
                        "retain": {
                            "type": "integer",
                            "description": "信息流保留轮数，1-10。一次性操作 1，写文件 3，读文件 5，任务收尾 3-5。",
                            "minimum": 1,
                            "maximum": 10
                        }
                    },
                    "required": ["target", "action", "plugin_args"]
                }
            }
        },
]

TOOL_SCHEMAS_PRESSURE = [MANAGE_FLOW_SCHEMA]

# 兼容旧引用
TOOL_SCHEMAS = TOOL_SCHEMAS_BASE



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
3. manage_flow —— 本轮减压阀（仅上下文超阈值时出现）：collapse（收起）/ ignore（关闭），只减不增。

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

def _param_fingerprint(action: str, params: dict) -> str:
    """对"实质参数"做指纹，用于区分"同一 target/action 但参数已改"的调用。

    只对 code_runner 这类"内容型"参数做 hash；其余参数走 sanitize 后的 JSON。
    目的：改了 code 就应该算"新调用"，不该被 target+action 的旧失败记录拦死。
    """
    p = params or {}
    # 内容型字段优先：code / content / html / script
    for key in ("code", "content", "html", "script", "source"):
        v = p.get(key)
        if isinstance(v, str) and v.strip():
            return hashlib.md5(v.encode("utf-8")).hexdigest()[:16]
    # 其余：sanitize 后 JSON 的 hash（去掉大字段内容，保留结构）
    try:
        s = json.dumps(_sanitize_params(p), ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        s = str(p)
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:16]

def _check_failure_lock(user_id: str, target: str, action: str, params: dict) -> str:
    """同指纹累计 >= 3 次时，返回拒绝原因；否则返回空串。

    指纹计算与 _record_failure 一致：_failure_fingerprint(target, action, error_text)
    但这里拿不到 error_text（还没调），所以用“target + action + 参数类型签名”做轻量指纹，
    覆盖“同一个调用反复错”的最常见场景（缺 path / limit 传字符串 / packages 传字符串）。
    """
    if not target or target.startswith("builtins/agents/main_agent"):
        return ""
    try:
        data = _load_backpack(user_id)
        items = data.get("items", [])
        if not items:
            return ""

        cur_param_fp = _param_fingerprint(action, params or {})   # ★ 新增

        for it in items:
            it_target = it.get("target", "") or ""
            if not it_target:
                s0 = it.get("summary", "") or ""
                m0 = re.search(r"调用\s+(\S+?)（", s0)
                it_target = m0.group(1) if m0 else ""
            if it_target != target:
                continue
            s = it.get("summary", "") or ""
            m = re.search(r"action=([^）]*)", s)
            it_action = (m.group(1) if m else "none")
            if it_action != (action or "none"):
                continue
            c = int(it.get("count", 1))
            if c < 3:
                continue
            # ★ 新增：参数指纹不同 → 说明这次实质参数已改，不算"同一调用"，放行
            it_param_fp = it.get("param_fp", "") or ""
            if it_param_fp and it_param_fp != cur_param_fp:
                continue
            # 命中：给拒绝原因
            eid = it.get("id", "")
            fix = it.get("fix", "") or ""
            return (
                f"同一调用已失败 {c} 次（{eid}），本轮拒绝执行。"
                f"先读经验背包 {eid} 的正确做法：{fix or '先用 contract_agent.get 看参数'}。"
                f"⚠️ 注意：本次 target/action/参数与失败记录完全一致。"
                f"如已修改 code/plugin_args 内容，请确认参数确实变了再重发；"
                f"否则请改用其他工具或换思路。"
            )
    except Exception:
        return ""
    return ""
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
        param_fp = _param_fingerprint(action, params or {})   # ★ 新增
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
                it["param_fp"] = param_fp
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
            "target": target,
            "action": action or "none",
            "tags": tags,
            "body": (
                f"触发条件：调用 {target}（action={action or 'none'}）时，params={params_snapshot} 会失败\n"
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
            "param_fp": param_fp,
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

class PromptBuilder:
    @staticmethod
    def build_system(session_id: str) -> str:
        return _load_skill().replace("{{session_id}}", session_id).replace("__SESSION_ID__", session_id)

    @staticmethod
    def build_tail(user_id: str, session_id: str, depth: int = 0, flow=None) -> str:
        """多轮消息模式下，拼到最后一条 user 尾部的“动态上下文”。

        只放：经验背包 / 任务看板 / 分身任务 / 深度提示 / 执行环境 / 本机基址。
        历史轮已在 messages 里，不在这里重复。
        """
        parts = []

        failure_index = _load_failure_index(user_id)
        if failure_index:
            parts.append(
                f"【经验背包 · 坑点，严禁反复踩坑】插件调用失败自动记录，不需要你手动写。\n"
                f"{failure_index}"
            )

        taskboard = _load_taskboard(user_id, session_id)
        if taskboard:
            parts.append(f"【任务看板】判断任务复杂度，\n{taskboard}")

        # 分身任务
        tasks_file = _conversations_dir(user_id, session_id) / "tasks.json"
        if tasks_file.exists():
            try:
                tasks = json.loads(tasks_file.read_text(encoding="utf-8"))
                if tasks:
                    lines = ["【分身任务】"]
                    for tid, t in sorted(tasks.items(), key=lambda x: x[1].get("created_at", "")):
                        status = t.get("status", "unknown")
                        icon = {"running": "⏳", "done": "✅", "failed": "❌",
                                "timeout": "⏰", "cancelled": "🚫"}.get(status, "?")
                        lines.append(f"{icon} {tid}: {t.get('description', '')} [{status}]")
                    parts.append("\n".join(lines))
            except Exception:
                pass

        # 深度提示（原 build_user 里那段，原样搬过来）
        if depth == 0:
            hint = "开始执行，综合分析，整理思路，逐步执行，一次只调用一次工具或者回复。"
        elif depth <= 10:
            hint = ("执行中：预计执行深度，随时 `builtins/tools/add_task_board`"
                    "（action=\"replace\"，plugin_args={\"content\": \"看板内容\"}）先添加看板、记录过程；"
                    "确认完成后用 action=\"clear\" 清除。")
        elif depth <= 20:
            hint = "检查：本次执行是否接近完成？是→reply，否→继续。"
        elif depth <= 30:
            hint = ("注意：未完成内容必须建立任务看板！！！避免跨轮丢失内容，"
                    "优先考虑是否该 reply 用户当前进展。")
        elif depth <= 37:
            hint = (f"⚠️ 警告：执行深度 {depth}/40，即将达到上限。"
                    f"**先整理当前进展，优先 reply 用户**；"
                    f"确实必须继续时，最多再走 1~2 步就必须 reply。")
        elif depth == 38:
            hint = "🚨 强警告：执行深度 38/40，这一步是最后一次能调工具的机会……"
        else:
            hint = f"🛑 最后机会：执行深度 {depth}/40。本轮唯一允许的动作是 reply。"

        parts.append(f"执行深度：{depth}/{MAX_RECURSION_DEPTH} - {hint}")

        # 执行环境（原 build_user 里那段）
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
    async def _handle_skill(target: str, flow: InformationFlow, chain_no: int = None) -> dict:
        """skill/<rel> → 读 data/skills/<rel>/SKILL.md 全文，写 flow，返回结果。

        路由规则（与 cogitor._scan_skills / contract_agent._handle_skill_contract 一致）：
          skill/<rel>           → data/skills/<rel>/SKILL.md
          兜底                  → data/skills/aicp/<rel>/SKILL.md
          rel 以 SKILL.md 结尾  → data/skills/<rel>（直接当文件）

        不截断：全文写进 call_end.result.data.content，
        由 _render_chain_full 在渲染层统一截断 + > 引用包裹。
        """
        call_id = str(uuid.uuid4())[:8]
        rel = target[len("skill/"):].strip("/")

        # ---- 解析路径：只认 data/skills，不碰 plugins ----
        skill_file = None
        if rel:
            # 1) data/skills/<rel>/SKILL.md
            cand = Path("data/skills") / rel / "SKILL.md"
            if cand.exists():
                skill_file = cand
            else:
                # 2) 兜底 data/skills/aicp/<rel>/SKILL.md
                alt = Path("data/skills/aicp") / rel / "SKILL.md"
                if alt.exists():
                    skill_file = alt
            # 3) rel 直接以 SKILL.md 结尾
            if skill_file is None:
                cand2 = Path("data/skills") / rel
                if cand2.is_file() and cand2.name == "SKILL.md":
                    skill_file = cand2

        # ---- 找不到文件 ----
        if skill_file is None:
            err = f"skill 不存在: {target}"
            await flow.append_system(
                action="call_error",
                result_think=f"❌ {err}",
                detail={"call_id": call_id, "target": target, "error": err},
                chain_no=chain_no,
            )
            return {"ok": False, "think": err}

        # ---- 读全文 + 去 frontmatter ----
        try:
            text = skill_file.read_text(encoding="utf-8")
        except Exception as e:
            err = f"读取 skill 失败: {e}"
            await flow.append_system(
                action="call_error",
                result_think=f"❌ {err}",
                detail={"call_id": call_id, "target": target, "error": err},
                chain_no=chain_no,
            )
            return {"ok": False, "think": err}

        body = text
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end != -1:
                body = text[end + 4:].strip()

        # size：文件字节数；body_size：去 frontmatter 后的正文字数
        try:
            file_size = skill_file.stat().st_size
        except Exception:
            file_size = len(text.encode("utf-8"))
        body_size = len(body)

        # ---- 写 flow：call_start + call_end，全文进 result.data.content ----
        await flow.append_system(
            action="call_start",
            result_think=f"📤 读取 skill: {target}",
            detail={
                "call_id": call_id,
                "target": target,
                "action": "read",
                "params": {},
                "status": "pending",
            },
            chain_no=chain_no,
        )
        await flow.append_system(
            action="call_end",
            result_think=f"📨 skill 已加载: {target}",
            detail={
                "call_id": call_id,
                "target": target,
                "action": "read",
                "result": {
                    "ok": True,
                    "data": {
                        "path": str(skill_file),
                        "size": file_size,        # 文件字节数
                        "body_size": body_size,   # 正文字数
                        "content": body,          # 全文，渲染层截断
                    },
                },
            },
            chain_no=chain_no,
        )
        return {
            "ok": True,
            "think": f"skill 已加载: {target}（{body_size} 字）",
            "data": {"path": str(skill_file), "size": file_size, "body_size": body_size},
        }
    @staticmethod
    async def execute(agent, args: dict, user_id: str, session_id: str, flow: InformationFlow,
                      think: str = "", chain_no: int = None) -> dict:
        target = args.get("target", "")
        # ★ 同指纹 3 次拒绝：先查经验背包，命中就不调插件
        if target and not target.startswith("skill/"):
            _lock_reason = _check_failure_lock(
                user_id, target, args.get("action", ""), args.get("plugin_args") or {}
            )

            if _lock_reason:
                call_id = str(uuid.uuid4())[:8]
                # ★ 1) call_error：让渲染层认出这是"失败"，不再显示"（执行中，无返回）"
                await flow.append_system(
                    action="call_error",
                    result_think=f"⛔ {target} 被经验背包拦截",
                    detail={"call_id": call_id, "target": target, "error": _lock_reason},
                    retain=2,
                    chain_no=chain_no,
                )
                # ★ 2) task_summary：给模型"怎么绕过"的行动指引
                await flow.append_system(
                    action="task_summary",
                    result_think=(
                        f"⛔ {target} 被拦截：target/action/参数与历史失败完全一致。\n"
                        f"**不要原样重发**。要绕过拦截，必须让参数实质变化：\n"
                        f"- code_runner：重写 `code` 字段（如补全 async def execute(envelop, agent)）\n"
                        f"- 其他插件：修改 plugin_args 的具体值，而不是只改 think 描述\n"
                        f"改完后重新提交；参数指纹变了就不会再被拦。"
                    ),
                    detail={"reason": "failure_lock", "target": target},
                    retain=2,
                    chain_no=chain_no,      # ★ 同一个 chain_no，别让 task_summary 跑到别的链
                )
                await _push_progress(
                    agent, user_id, session_id, "error",
                    f"⛔ {target} 被拦截（同错 ≥3 次）",
                    extra={"call_id": call_id, "target": target, "status": "error", "error": _lock_reason},
                )
                return {"ok": False, "think": _lock_reason}
        # ★ skill/ 路由：就地读 SKILL.md，不进 call，结果直接写 flow
        if target.startswith("skill/"):
            return await ToolRegistry._handle_skill(target, flow, chain_no)
        tool_action = args.get("action", "")
        if not target:
            # 补一条 call_error，让 flow 和前台都看到
            call_id = str(uuid.uuid4())[:8]
            await flow.append_system(
                action="call_error",
                result_think="❌ 缺少 target",
                detail={"call_id": call_id, "target": "", "error": "缺少 target"},
                retain=3,
                chain_no=chain_no,
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


        retain = args.get("retain", 3)


        call_id = str(uuid.uuid4())[:8]    # ← 提前
        t0 = time.time()                    # ← 提前
        # ★ 推送：执行工具前
        # ★ 推送：执行工具前（字段名对齐前端 d.action；参数每项截断 100 字）
        _brief_params = {}
        if isinstance(tool_params, dict):
            for _k, _v in tool_params.items():
                if isinstance(_v, str):
                    _brief_params[_k] = _v if len(_v) <= 100 else (_v[:100] + f"…（{len(_v)} 字）")
                elif isinstance(_v, (list, dict)):
                    try:
                        _s = json.dumps(_v, ensure_ascii=False, default=str)
                    except Exception:
                        _s = str(_v)
                    _brief_params[_k] = _v if len(_s) <= 100 else (_s[:100] + f"…（{len(_s)} 字符）")
                else:
                    _brief_params[_k] = _v
        await _push_progress(
            agent, user_id, session_id, "executing",
            f"⚡ {target}",
            extra={
                "call_id": call_id,
                "target": target,
                "action": tool_action,      # ★ 对齐前端 d.action
                "params": _brief_params,    # ★ 对齐前端 d.params，每项截断 100 字
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
                    chain_no=chain_no,
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
                    chain_no=chain_no,
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
            chain_no=chain_no,
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
                    chain_no=chain_no,
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
                chain_no=chain_no,
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
                chain_no=chain_no,
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
        flow._gc_ignored()

        system_prompt = PromptBuilder.build_system(session_id)
        _base = (getattr(agent, "base_url", "") or "http://127.0.0.1:9000").rstrip("/")

        # 多轮消息：system + 历史轮独立消息
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(flow.get_messages())

        # 动态上下文（经验背包 / 任务看板 / 深度提示 / 执行环境 / 本机基址）拼到最后一条 user 尾部
        tail = PromptBuilder.build_tail(user_id, session_id, depth, flow=flow)
        _now = datetime.now()
        _week = "一二三四五六日"[_now.weekday()]
        tail += f"\n\n【当前时间】{_now:%Y-%m-%d} 周{_week} {_now:%H:%M:%S}（{_now:%Z}）"
        tail += f"\n\n【本机基址】{_base}（给用户可点击链接时直接用这个，不要写 {{base_url}} 占位符）"
        tail += f"\n\n【当前会话】{session_id}"
        tail += f"\n\n【工作目录】data/workspace/{session_id}（临时文件放这里，任务完成后清理）"

        # ★ tail 独立成最后一条 user 消息，不拼到已有 user 里
        messages.append({"role": "user", "content": tail})

        # debug dump 改成 dump messages
        try:
            _debug_dump_messages(flow, messages, depth)
        except Exception as _e:
            Logger.warn(f"[debug] dump messages 失败: {_e}")


        # ★ 上下文压力阈值（字符数；折叠是纯减法，触发后必然下降）
        _ctx_chars = sum(len(m.get("content") or "") for m in messages)
        _pressure = _ctx_chars >= CONTEXT_PRESSURE_THRESHOLD

        if depth >= MAX_RECURSION_DEPTH - 1:
            tools = [t for t in TOOL_SCHEMAS_BASE if t["function"]["name"] == "reply"]
            tool_choice = "auto"
        elif _pressure:
            tools = list(TOOL_SCHEMAS_PRESSURE)
            tool_choice = "auto"
            Logger.warn(
                f"[pressure] ctx={_ctx_chars} ≥ {CONTEXT_PRESSURE_THRESHOLD}，"
                f"仅注入 manage_flow，depth={depth}"
            )
        else:
            tools = list(TOOL_SCHEMAS_BASE)
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
                elif etype == "tool_call_delta":
                    # ★ 仅推送进度，不写入 text_buf / flow；让前端在“等完整 tool_call”期间看到参数在涨
                    _name = event.get("name", "") or ""
                    _alen = event.get("args_len", 0) or 0
                    _buf = f"\n⚙️ {_name} ({_alen}B)" if _name else f"\n⚙️ ({_alen}B)"
                    _buffer_stream_chunk(agent, user_id, session_id, _buf)
                elif etype == "retry":
                    await _push_progress(
                        agent, user_id, session_id, "executing",
                        f"⚠️ LLM 超时，正在重试 {event.get('attempt')}/{event.get('max_attempts')}...",
                        extra={"status": "retrying"},
                    )
                elif etype == "tool_call":
                    tc = event.get("tool_call")

                    if tc.name == "manage_flow":
                        # ★ manage_flow 走「正常工具调用」路径：call_start / call_end / 推 WS
                        args = tc.arguments or {}
                        call_id = str(uuid.uuid4())[:8]
                        t0 = time.time()
                        ctx_before = flow.estimate_context_chars()
                        mf_chain_no = flow._next_chain()
                        mf_params = {
                            "collapse": args.get("collapse"),
                            "ignore": args.get("ignore"),
                        }

                        # 推 start（前端 handleToolStart 认 status: "start"）
                        await _push_progress(
                            agent, user_id, session_id, "executing",
                            "⚡ manage_flow",
                            extra={
                                "call_id": call_id,
                                "target": "builtins/agents/main_agent",
                                "action": "manage_flow",
                                "params": mf_params,
                                "status": "start",
                                "think": args.get("reason", ""),
                            },
                        )

                        # 写 call_start（走正常 chain 编号，进 ## 分组）
                        await flow.append_system(
                            action="call_start",
                            result_think="📤 调用工具: manage_flow",
                            detail={
                                "call_id": call_id,
                                "target": "builtins/agents/main_agent",
                                "action": "manage_flow",
                                "params": mf_params,
                                "status": "pending",
                            },
                            retain=2,
                            chain_no=mf_chain_no,
                        )

                        ok = True
                        error_msg = ""
                        result = {}
                        try:
                            result = await flow.manage_flow(
                                collapse=args.get("collapse"),
                                ignore=args.get("ignore"),
                            )
                            manage_flow_done = True
                        except Exception as e:
                            ok = False
                            error_msg = str(e)
                            manage_flow_done = True
                            Logger.warn(f"[manage_flow] 执行失败: {e}")

                        ctx_after = flow.estimate_context_chars()
                        freed = max(0, ctx_before - ctx_after)
                        elapsed_ms = int((time.time() - t0) * 1000)
                        _info_parts = (result.get("think", "") if ok else error_msg)
                        Logger.info(
                            f"[manage_flow] depth={depth} {_info_parts} "
                            f"ctx {ctx_before}→{ctx_after} (freed={freed})"
                        )

                        # 写 call_end / call_error
                        await flow.append_system(
                            action="call_end" if ok else "call_error",
                            result_think="📨 调用完成: manage_flow" if ok else "❌ manage_flow 失败",
                            detail={
                                "call_id": call_id,
                                "target": "builtins/agents/main_agent",
                                "action": "manage_flow",
                                "result": {
                                    "ok": ok,
                                    "data": {
                                        "applied": (result.get("data", {}) if ok else {}),
                                        "ctx_before": ctx_before,
                                        "ctx_after": ctx_after,
                                        "freed": freed,
                                    },
                                    "error": error_msg,
                                },
                                "duration_ms": elapsed_ms,
                            },
                            retain=2,
                            chain_no=mf_chain_no,
                        )

                        # 推 done / error
                        await _push_progress(
                            agent, user_id, session_id, "done" if ok else "error",
                            "✅ manage_flow" if ok else "❌ manage_flow",
                            extra={
                                "call_id": call_id,
                                "target": "builtins/agents/main_agent",
                                "status": "ok" if ok else "error",
                                "duration_ms": elapsed_ms,
                                "error": error_msg if not ok else None,
                            },
                        )
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

                        _push_stream_nowait(agent, user_id, session_id, f"\n🧩 {raw_display}\n")

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
                    # ★ 旁白型 reply：模型把「下一步动作」当成了最终回复
                    #   （"I'll now implement..." / "我要先读文件"）。
                    #   这种 reply 没有交付结果，却会让自递归提前终止。
                    #   在写进 flow 之前拦下，避免渲染成「🤖 AI：我要去读文件」。
                    if is_narration_reply(content):
                        Logger.warn(
                            f"[LLMThinker][narration_reply] depth={depth} "
                            f"session={session_id}: {content[:80]}"
                        )
                        raise NarrationReplyError(content[:200])
                    await flow.append_ai(think, "reply", content=content, retain=args.get("retain", 3))
                    # ★ 固化本轮折叠形态（供下一轮渲染历史轮时用）
                    try:
                        await flow.snapshot_round(flow._round_counter)
                    except Exception as e:
                        Logger.warn(f"[snapshot] 固化失败: {e}")
                    return {"call": "reply", "content": content, "args": {}}

                if name == "use_tool":
                    retain = args.get("retain", 3)
                    combined_think = think
                    if text_buf.strip():
                        combined_think = (think + "\n" + text_buf.strip()).strip() if think else text_buf.strip()
                        text_buf = ""
                    await flow.append_ai(combined_think, "use_tool", args=args, retain=retain)
                    results.append({"call": "use_tool", "args": args, "think": combined_think, "retain": retain,
                                    "chain_no": flow._chain_counter})
            if results:
                first = results[0]
                first["_pending_tool_calls"] = results[1:]
                return first

        if text_buf.strip():
            content = _fill_base_url(text_buf.strip(), agent)
            if is_narration_reply(content):
                Logger.warn(
                    f"[LLMThinker][narration_reply/text] depth={depth} "
                    f"session={session_id}: {content[:80]}"
                )
                raise NarrationReplyError(content[:200])
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

        self._interrupt_signals: Dict[str, bool] = {}
        # ★ 活跃回合状态（方案B）：per-session 记录「正在跑」，供前端刷新/切session后重查按钮态
        self._active_turns: Dict[str, dict] = {}

    def mark_turn_start(self, session_id: str, started_at: float = None):
        import time as _t
        self._active_turns[session_id] = {
            "session_id": session_id,
            "running": True,
            "status": "running",
            "started_at": started_at or _t.time(),
        }

    def mark_turn_end(self, session_id: str, reason: str = "done"):
        # 不 pop 掉 entry：保留 status 让前端能知道「刚才是打断结束的」，
        # 否则打断收尾期间前端查不到状态，按钮态会错乱。
        import time as _t
        info = self._active_turns.get(session_id)
        if info is not None:
            info["status"] = "interrupted" if reason == "interrupted" else "idle"
            info["running"] = False
            info["ended_at"] = _t.time()
        self._interrupt_signals.pop(session_id, None)

    def get_turn_state(self, session_id: str) -> dict:
        info = self._active_turns.get(session_id)
        if not info:
            return {"session_id": session_id, "running": False, "status": "idle"}
        return {
            "session_id": session_id,
            "running": info.get("running", False),
            "status": info.get("status", "running"),
            "started_at": info.get("started_at"),
            "ended_at": info.get("ended_at"),
        }

    def signal_interrupt(self, session_id: str) -> dict:
        """打断当前回合。没有活跃回合 → no_active_turn；已在打断中 → already_interrupting（幂等）。"""
        info = self._active_turns.get(session_id)
        if not info or not info.get("running", False):
            return {"ok": False, "reason": "no_active_turn"}
        if self._interrupt_signals.get(session_id, False):
            # 已在打断中：幂等，不重复推事件
            return {"ok": True, "reason": "already_interrupting"}
        self._interrupt_signals[session_id] = True
        return {"ok": True, "reason": "signaled"}
    def clear_interrupt(self, session_id: str):
        self._interrupt_signals.pop(session_id, None)


    async def process(self, agent, user_id: str, session_id: str, flow: InformationFlow, depth: int = 0) -> dict:
         # _think_sessions 由 execute 管，process 不再 add/discard
         return await self._process_internal(agent, user_id, session_id, flow, depth)

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
        narr_key = f"{session_id}:narration_reply"
        if not hasattr(self, "_empty_retries"):
            self._empty_retries = {}
        if not hasattr(self, "_narration_retries"):
            self._narration_retries = {}
        retry_count = self._empty_retries.get(retry_key, 0)
        narr_count = self._narration_retries.get(narr_key, 0)

        output = None
        for _retry in range(2):
            try:
                output = await self.thinker.think(agent, flow, user_id, session_id, depth)
                self._empty_retries[retry_key] = 0
                self._narration_retries[narr_key] = 0
                break
            except NarrationReplyError as e:
                # ★ 模型把「下一步动作」当成了最终回复。
                #   不推给用户，记一条系统提示让它改用 use_tool，然后重试一次。
                if _retry == 0 and narr_count < 3:
                    self._narration_retries[narr_key] = narr_count + 1
                    Logger.warn(
                        f"[narration_reply] 把下一步动作当成了 reply，自动重试 1 次 "
                        f"(depth={depth}, 已重试={narr_count + 1}/3): {str(e)[:80]}"
                    )
                    try:
                        await flow.append_system(
                            action="task_summary",
                            result_think=(
                                "⚠️ 你上一条回复只是「我要去做什么」的宣告"
                                "（比如「I'll now ...」「我要先读文件」「接下来我来...」），"
                                "**不是交付结果**，任务还没完成。\n"
                                "不要用 reply 说下一步动作 —— 请直接调用 "
                                "use_tool / manage_flow 去真正执行；"
                                "只有在做完、手上有结果时，才用 reply 交付结论。"
                            ),
                            detail={"reason": "narration_reply",
                                    "narration": str(e)[:200]},
                            retain=2,
                        )
                    except Exception:
                        pass
                    continue
                # 重试次数用尽 → 按空输出处理
                Logger.warn(
                    f"[narration_reply] 重试次数用尽 (depth={depth})，转为空输出处理"
                )
                self._narration_retries[narr_key] = 0
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
                    agent, args, user_id, session_id, flow, think=think,
                    chain_no=call.get("chain_no"),
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
            # ★ 工具执行后加 1 秒喘息，避免上游 Key 池被持续调用占满
            if all_calls:
                import random
                await asyncio.sleep(random.uniform(0.5, 2.0))
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

        # ★ 最近一次调用命中率
        last_prompt = int(llm_stats.get("last_prompt_tokens", 0) or 0)
        last_cache_read = int(llm_stats.get("last_cache_read_tokens", 0) or 0)
        cache_hit_rate = round(last_cache_read / last_prompt, 4) if last_prompt > 0 else 0.0
        cache_hit_pct = round(cache_hit_rate * 100, 1)

        # ★ 累计命中率
        total_prompt = int(llm_stats.get("prompt_tokens", 0) or 0)
        total_cache_read = int(llm_stats.get("cache_read_tokens", 0) or 0)
        cache_hit_rate_total = round(total_cache_read / total_prompt, 4) if total_prompt > 0 else 0.0
        cache_hit_pct_total = round(cache_hit_rate_total * 100, 1)

        stats = {
            "context_k": ctx_k,
            "round_total": llm_stats.get("round_total_tokens", 0),
            "depth": depth,
            "max_depth": MAX_RECURSION_DEPTH,
            # 最近一次
            "cache_read": last_cache_read,
            "cache_hit_rate": cache_hit_rate,
            "cache_hit_pct": cache_hit_pct,
            "last_prompt": last_prompt,
            # 累计
            "cache_read_total": total_cache_read,
            "cache_hit_rate_total": cache_hit_rate_total,
            "cache_hit_pct_total": cache_hit_pct_total,
            "total_prompt": total_prompt,
        }
        task = asyncio.ensure_future(agent.system.call(core.Envelop(
            sender="builtins/agents/main_agent",
            receiver="os/_websocket",
             meta={"session_id": session_id},
            payload={"action": "push", "channel_id": f"pa_{user_id}",

                     "data": {"type": "stats", "session_id": session_id, "stats": stats}}
        )))
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=6.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            Logger.warn(f"[WS] 推送 stats 超时 6s，已放弃 session={session_id}")
        except Exception as e:
            Logger.warn(f"[WS] 推送 stats 失败: {type(e).__name__}: {e}")
    except Exception as e:
        Logger.warn(f"[WS] 推送 stats 失败: {e}")

# ============================================================
# 动态基线：#N 级关联判定
# ============================================================

_BASELINE_INPUT_TOKEN_BUDGET = 12000   # 关联判定输入预算（约 30-50 轮摘要）
_BASELINE_MAX_ROUNDS = 8               # 输出上限：最多选 8 轮


def _collect_round_summaries(flow, budget_tokens: int = _BASELINE_INPUT_TOKEN_BUDGET):
    """从最近一轮往前收集 #N 摘要，直到 token 预算用完。

    返回 (summary_lines, valid_rounds)，均按时间升序。
    强制包含最近 1 轮（防止预算太小导致完全没历史）。
    """
    rounds = flow._split_rounds()
    rounds = [[e for e in r if (e.get("round_no") or 0) > 0] for r in rounds]
    rounds = [r for r in rounds if r]
    if not rounds:
        return [], []

    def _round_summary(entries):
        r_no = entries[0].get("round_no") or 0
        user_text = ""
        reply_text = ""
        for e in entries:
            if e.get("from") == "user" and not user_text:
                user_text = (e.get("content") or "")[:120]
            if e.get("from") == "ai" and e.get("call") == "reply" and not reply_text:
                reply_text = (e.get("content") or "")[:80]
        if not user_text:
            return None
        line = f"#{r_no} 用户:{user_text} | 回复:{reply_text}"
        return (r_no, line)

    collected = []
    used_tokens = 0
    for entries in reversed(rounds):
        item = _round_summary(entries)
        if not item:
            continue
        r_no, line = item
        line_tokens = len(line) // 2 + 10   # 粗略估算：中英混合约 2 字符/token
        if used_tokens + line_tokens > budget_tokens:
            break
        collected.append(item)
        used_tokens += line_tokens

    # 强制包含最近 1 轮（预算太小也不能丢当前上文）
    last = rounds[-1]
    last_item = _round_summary(last)
    if last_item and not any(r_no == last_item[0] for r_no, _ in collected):
        collected.append(last_item)

    collected.sort(key=lambda x: x[0])
    return [line for _, line in collected], [r_no for r_no, _ in collected]

def _load_pinned_skills(flow) -> list:
    """读 pinned.json，返回 status == "pinned" 的 skill（可能有多个）。

    返回：[{"name": "skill/...", "desc": "...", "pinned": True, "full_text": "..."}, ...]
    卸载（status = "indexed"）的不返回。
    """
    result = []
    try:
        pinned_file = _conversations_dir(flow.user_id, flow.session_id) / "pinned.json"
        if not pinned_file.exists():
            return result
        pinned_data = json.loads(pinned_file.read_text(encoding="utf-8"))
        pinned_items = pinned_data.get("pinned", {}) or {}
        for skill_id, item in pinned_items.items():
            if not isinstance(item, dict):
                continue
            if item.get("status") != "pinned":
                continue
            # 从 path 构造 name：data/skills/aicp/xxx/SKILL.md → skill/aicp/xxx
            path = str(item.get("path", "") or "").replace("\\", "/")
            name = ""
            if path.startswith("data/skills/"):
                rel = path[len("data/skills/"):]
                if rel.endswith("/SKILL.md"):
                    rel = rel[:-len("/SKILL.md")]
                elif rel.endswith("SKILL.md"):
                    rel = rel[:-len("SKILL.md")].rstrip("/")
                name = "skill/" + rel
            if not name:
                name = f"skill/pinned/{skill_id}"
            result.append({
                "name": name,
                "desc": str(item.get("desc", "") or "")[:80],
                "pinned": True,
                "full_text": item.get("full_text", "") or "",
            })
    except Exception as e:
        Logger.warn(f"[baseline] 读 pinned.json 失败: {e}")
    return result

async def _detect_baseline_rounds(agent, flow, user_input: str):
    """从历史 #N 里选出当前消息所属的任务链轮号，同时总结任务意图、预选相关 skill。

    ★ 额外：读 pinned.json，把用户手工钉住的 skill 合并进 picked_skills。
    ★ 钉住的 skill 不受 8 轮短路影响（短路分支也带）。

    返回：
      (baseline, task_summary, picked_skills)
      baseline: [28, 29, 30, 31] 任务链（升序，≤8 个）
                []               新任务，不需要历史
                None             降级，用固定最近 BASE_ROUNDS 轮
      task_summary: 一句话任务意图（≤50 字，可能为空）
      picked_skills: [{"name": "skill/...", "desc": "...", "pinned": bool, "full_text": "..."}, ...]
    """
    # ★ 先读钉住的 skill（短路分支也要带，不受 8 轮影响）
    pinned_skills = _load_pinned_skills(flow)
    pinned_names = {s["name"] for s in pinned_skills}

    if not agent.llm or not flow._flow:
        return [], "", pinned_skills

    # ★ 短路：总轮数 ≤ BASE_ROUNDS 时，固定 8 轮已覆盖全部历史，不调 LLM
    rounds = flow._split_rounds()
    rounds = [[e for e in r if (e.get("round_no") or 0) > 0] for r in rounds]
    rounds = [r for r in rounds if r]
    if len(rounds) <= BASE_ROUNDS:
        Logger.info(f"[baseline] 总轮数 {len(rounds)} ≤ {BASE_ROUNDS}，跳过 LLM 判定")
        return [], "", pinned_skills

    summary_lines, valid_rounds = _collect_round_summaries(flow)
    if not valid_rounds:
        return [], "", pinned_skills

    # ★ skill 目录（只 name + desc，最多 50 个）
    skill_catalog = _skill_catalog_for_prompt()

    prompt = (
        "下面是历史对话的「用户话 + AI 回复」按时间顺序排列（每轮编号 #N）。\n"
        "最后是用户刚说的一句新话。\n"
        "\n"
        "你的任务有三项：\n"
        "\n"
        "1. 判断这句新话是在【继续哪个任务】。\n"
        "   一个「任务」= 从某一轮开始、连续若干轮、围绕同一个目标推进的对话链。\n"
        "   返回这个任务链包含的所有轮号（按时间升序），如 [28,29,30,31]。\n"
        "   如果这句新话是全新任务、不属于任何历史任务链，返回 []。\n"
        "   如果它延续的是最近一个任务（#N-1 那一轮），就返回那个任务链的全部轮号，不要只返回最后一轮。\n"
        "   不要因为「最近」就选它，只看「目标是否相同」。\n"
        "   如果无法判断它属于哪个任务链，返回 \"UNKNOWN\"。\n"
        "\n"
        "2. 用一句话总结用户这句话的「任务意图」（≤50 字）。\n"
        "\n"
        "3. 从下面的 skill 目录里，选出和任务相关的最可能用到的 1-5 个 skill（返回完整 name）。\n"
        "   没有合适的返回 []。不要因为「可能相关」就全选，只选真正可能用到的。\n"
        "\n"
        "输出格式（JSON 对象，不要解释，不要 ```）：\n"
        '{"rounds": [28,29,30], "task_summary": "改前端大文件的缓存问题", "skills": ["skill/aicp/frontend-bigfile-edit"]}\n'
        '或 {"rounds": [], "task_summary": "...", "skills": []}\n'
        '或 {"rounds": "UNKNOWN", "task_summary": "...", "skills": []}\n'
        "\n"
        "历史对话（user + AI 回复，按时间顺序）：\n"
        + "\n".join(summary_lines) + "\n\n"
        + f"用户刚说的新话：{user_input[:200]}\n\n"
        + "可用 skill 目录（name — desc）：\n"
        + (skill_catalog or "（无可用 skill）") + "\n"
    )

    try:
        raw = await asyncio.wait_for(
            agent.llm.chat(
                [{"role": "user", "content": prompt}],
                session_id=f"baseline_{flow.session_id}",   # 独立槽位，不抢主循环 cache
                max_tokens=300,
                temperature=0.1,
            ),
            timeout=10.0,
        )
    except Exception as e:
        Logger.warn(f"[baseline] 关联判定调用失败，降级固定基线: {e}")
        return None, "", pinned_skills

    raw = (raw or "").strip()

    # ★ 兜底：LLM 说无法判断 → 降级固定基线
    if "UNKNOWN" in raw.upper():
        Logger.info("[baseline] LLM 返回 UNKNOWN，降级固定基线")
        return None, "", pinned_skills

    # ★ 解析 JSON 对象
    obj = None
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
        except Exception:
            obj = None
    if not isinstance(obj, dict):
        # 兼容旧格式：LLM 只返回了数组
        m2 = re.search(r"\[[\d,\s]*\]", raw)
        if not m2:
            return None, "", pinned_skills
        try:
            nums = json.loads(m2.group(0))
        except Exception:
            return None, "", pinned_skills
        baseline = [n for n in nums if isinstance(n, int) and n in valid_rounds]
        baseline = sorted(baseline)[:_BASELINE_MAX_ROUNDS]
        return baseline, "", pinned_skills

    # ---- rounds ----
    rounds_raw = obj.get("rounds")
    if isinstance(rounds_raw, str) and rounds_raw.upper() == "UNKNOWN":
        return None, "", pinned_skills
    baseline = None
    if isinstance(rounds_raw, list):
        baseline = [n for n in rounds_raw if isinstance(n, int) and n in valid_rounds]
        baseline = sorted(baseline)[:_BASELINE_MAX_ROUNDS]
    else:
        baseline = None

    # ---- task_summary ----
    task_summary = str(obj.get("task_summary", "") or "").strip()
    if len(task_summary) > 50:
        task_summary = task_summary[:50]

    # ---- skills ----
    skills_raw = obj.get("skills") or []
    picked_skills = []
    if isinstance(skills_raw, list):
        for sk_name in skills_raw:
            if not isinstance(sk_name, str):
                continue
            sk_name = sk_name.strip()
            if not sk_name.startswith("skill/"):
                continue
            if sk_name in pinned_names:   # 钉住的已包含，不重复
                continue
            info = _skill_info(sk_name)
            picked_skills.append({
                "name": sk_name,
                "desc": str(info.get("desc", "") or "")[:80],
                "pinned": False,
            })
            if len(picked_skills) >= 5:
                break

    # ★ 合并钉住的 skill（放前面，更醒目）
    merged = pinned_skills + picked_skills

    Logger.info(
        f"[baseline] rounds={baseline} summary={task_summary!r} "
        f"skills={[s['name'] for s in merged]} pinned={[s['name'] for s in pinned_skills]}"
    )
    return baseline, task_summary, merged


# ============================================================
# baseline 判定的辅助：skill 目录 / skill info
# ============================================================
def _scan_skills_for_baseline():
    """扫 data/skills/**/SKILL.md，返回 [{name, desc, path}, ...]。"""
    items = []
    root = Path("data/skills")
    if not root.exists():
        return items
    for f in root.rglob("SKILL.md"):
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        meta = {}
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end != -1:
                for line in text[3:end].splitlines():
                    if ":" in line and not line.strip().startswith("#"):
                        k, v = line.split(":", 1)
                        meta[k.strip().lower()] = v.strip().strip("\"'")
        desc = meta.get("description") or meta.get("title") or meta.get("name") or ""
        desc = " ".join(str(desc).split())[:60]
        rel = f.parent.relative_to(root).as_posix()
        items.append({"name": "skill/" + rel, "desc": desc, "path": f.as_posix()})
    items.sort(key=lambda x: x["name"])
    return items

def _skill_catalog_for_prompt(max_skills: int = 50) -> str:
    """给 baseline 判定用的 skill 目录（name + desc，最多 50 个）。"""
    skills = _scan_skills_for_baseline()
    if not skills:
        return ""
    lines = []
    for s in skills[:max_skills]:
        name = s.get("name", "")
        desc = str(s.get("desc", "") or "").strip()
        if not name:
            continue
        if desc:
            lines.append(f"- {name} — {desc}")
        else:
            lines.append(f"- {name}")
    return "\n".join(lines)


def _skill_info(name: str) -> dict:
    """按 name 找 skill 的 desc。"""
    for s in _scan_skills_for_baseline():
        if s.get("name") == name:
            return s
    return {}

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
            # ★ 拿到 session 锁后再写 user，避免在递归进行中把新消息插到当前轮前面
            pending = getattr(flow, "_pending_user_input", None)
            if pending is not None:
                flow._pending_user_input = None
                try:
                    await flow.append_user(pending)
                except Exception as e:
                    Logger.error(f"[_run_processing] append_user 失败: {e}")
            # ★ 新回合开始：重置无前端标记，让第一次推送重新探测
            _WS_DEAD[session_id] = False
            # ★ 基线快照写进 flow（轮级 system entry，不进 ## 分组）
            baseline = getattr(flow, "_current_baseline", None)
            if baseline is not None:
                try:
                    await flow.append_system(
                        action="baseline_rounds",
                        result_think=f"📎 基线：{baseline or '新任务'}",
                        detail={
                            "rounds": baseline,
                            "task_summary": getattr(flow, "_task_summary", "") or "",
                            "skills": getattr(flow, "_picked_skills", []) or [],
                        },
                        retain=2,
                        chain_no=0,
                    )
                except Exception as e:
                    Logger.warn(f"[baseline] 写 flow 失败: {e}")
            else:
                # ★ 降级：写一条标记，dump 里能看到“降级了”
                try:
                    await flow.append_system(
                        action="baseline_rounds",
                        result_think="📎 基线：降级固定最近 8 轮（LLM 无法判断任务链）",
                        detail={"rounds": None, "degraded": True},
                        retain=2,
                        chain_no=0,
                    )
                except Exception as e:
                    Logger.warn(f"[baseline] 写降级标记失败: {e}")
            try:
                core_processor.mark_turn_start(session_id)
                await core_processor.process(agent, user_id, session_id, flow, 0)
            finally:
                core_processor._think_sessions.discard(session_id)
                core_processor.clear_interrupt(session_id)
                core_processor.mark_turn_end(session_id)
                # ★ 回合结束清理无前端标记（下次新回合重新探测）
                _WS_DEAD.pop(session_id, None)

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
            core_processor.clear_interrupt(session_id)
            core_processor.mark_turn_end(session_id, "network_error")

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
            core_processor.clear_interrupt(session_id)
            core_processor.mark_turn_end(session_id, "safety_error")
    except asyncio.CancelledError:
        Logger.warn(f"[cancelled] 协程被取消: session={session_id}")
        core_processor._think_sessions.discard(session_id)
        core_processor.clear_interrupt(session_id)
        core_processor.mark_turn_end(session_id, "cancelled")
        raise

    except BaseException as e:
        Logger.error(f"异步处理 BaseException: {type(e).__name__}: {e}")
        try:
            await _push_chat(
                agent, user_id, session_id,
                f"❌ 系统错误: {type(e).__name__}: {str(e)[:200]}"
            )
        finally:
            core_processor._think_sessions.discard(session_id)
            core_processor.clear_interrupt(session_id)
            core_processor.mark_turn_end(session_id, "base_error")



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
        attachments = envelop.payload.get("attachments", [])
        if action == "whoami":
            envelop.payload = {"ok": True, "user_id": user_id, "session_id": session_id}
            return envelop
        # ★ 回合状态查询（方案B）：前端刷新/切session后据此恢复「停止」按钮态
        if action == "get_turn_state":
            st = core_processor.get_turn_state(session_id)
            envelop.payload = {"ok": True, "session_id": session_id, "turn_state": st}
            return envelop

        # 1. 回调
        if envelop.meta.get("is_callback"):
            async with _get_processing_lock(session_id):
                return await _handle_callback(envelop, agent)

        # 2. 系统 action
        if action == "list_debug_logs":
            dbg_dir = _conversations_dir(user_id, session_id) / _DEBUG_CTX_DIR_NAME
            if not dbg_dir.is_dir():
                envelop.payload = {"ok": True, "session_id": session_id, "files": [], "total": 0, "dir": ""}
                return envelop

            all_files = [p for p in dbg_dir.glob("depth_*.json") if p.is_file()]
            if not all_files:
                envelop.payload = {"ok": True, "session_id": session_id, "files": [], "total": 0,
                                   "dir": str(dbg_dir).replace("\\", "/")}
                return envelop

            # 文件名解析：depth_{depth:02d}_{HHMMSS}.json
            def _parse(p):
                m = re.match(r"depth_(\d+)_(\d{6})", p.name)
                if not m:
                    return None
                return int(m.group(1)), m.group(2)   # depth, hhmmss

            entries = []
            for p in all_files:
                parsed = _parse(p)
                if not parsed:
                    continue
                depth, hhmmss = parsed
                try:
                    mtime = p.stat().st_mtime
                except Exception:
                    mtime = 0
                entries.append({"path": p, "depth": depth, "hhmmss": hhmmss, "mtime": mtime})

            if not entries:
                envelop.payload = {"ok": True, "session_id": session_id, "files": [], "total": 0,
                                   "dir": str(dbg_dir).replace("\\", "/")}
                return envelop

            # 全量按时间倒序（mtime 优先，同 mtime 用文件名里的 HHMMSS）
            entries.sort(key=lambda x: (x["mtime"], x["hhmmss"]), reverse=True)

            # 从最新的开始往下走，遇到 depth_00 就收尾（这就是一组完整的任务轨迹）
            group = []
            for e in entries:
                group.append(e)
                if e["depth"] == 0:
                    break

            # 组内时间倒序：04 03 02 01 00
            group.sort(key=lambda x: (x["hhmmss"], x["depth"]), reverse=True)

            latest_mtime = group[0]["mtime"] if group else 0
            index = _load_conversation_index(user_id)
            meta = index.get(session_id, {}) or {}

            envelop.payload = {
                "ok": True,
                "session_id": session_id,
                "title": meta.get("title", ""),
                "updated_at": datetime.fromtimestamp(latest_mtime).isoformat() if latest_mtime else "",
                "dir": str(dbg_dir).replace("\\", "/"),
                "files": [
                    {
                        "name": x["path"].name,
                        "depth": x["depth"],
                        "ts": datetime.fromtimestamp(x["mtime"]).isoformat() if x["mtime"] else "",
                        "size": x["path"].stat().st_size,
                        "path": str(x["path"]).replace("\\", "/"),
                    }
                    for x in group
                ],
                "total": len(group),
            }
            return envelop

        if action == "read_debug_log":
            rel_path = envelop.payload.get("path", "")
            if not rel_path:
                envelop.payload = {"ok": False, "error": "path required"}
                return envelop
            target = Path(rel_path).resolve()
            allowed_root = _conversations_dir(user_id, session_id).resolve()
            try:
                target.relative_to(allowed_root)
            except ValueError:
                envelop.payload = {"ok": False, "error": "forbidden path"}
                return envelop
            if not target.is_file() or target.suffix != ".json" or target.parent.name != _DEBUG_CTX_DIR_NAME:
                envelop.payload = {"ok": False, "error": "not a debug file"}
                return envelop
            try:
                text = target.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                envelop.payload = {"ok": False, "error": str(e)}
                return envelop
            envelop.payload = {
                "ok": True,
                "name": target.name,
                "content": text,
                "size": target.stat().st_size,
            }
            return envelop
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
                flow._msg_cache = {}
                flow._dirty_rounds = set()
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
                    "description": meta.get("description", ""),
                    "archived": meta.get("archived", False),
                    "parent_session": meta.get("parent_session", ""),
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
        if not user_input and action != "interrupt":
            envelop.payload = {"ok": False, "error": "请输入内容"}
            return envelop

        # 5. 打断：只有显式 interrupt 请求才走这里（普通消息不能进这个分支）
        if envelop.sender == "os/_gateway" and action == "interrupt":
            ir = core_processor.signal_interrupt(session_id)
            if not ir.get("ok"):
                envelop.payload = {"ok": False, "error": "no_active_turn", "reason": ir.get("reason", "no_active_turn")}
                return envelop
            if ir.get("reason") != "already_interrupting":
                await _push_progress(agent, user_id, session_id, "executing", "⏸️ 中断中...",
                    extra={"status": "interrupting"})
            envelop.payload = {"ok": True, "waiting": True, "call": "interrupt", "content": "", "args": {}}
            return envelop

        # ★★★ 分身场景同步跑完，返回最终 reply ★★★
        # ★ 分身场景：session_id 以 sub_ 开头（不认 sender，不认插件名）
        _is_sub_call = (
            isinstance(session_id, str)
            and session_id.startswith("sub_")
        )

        if _is_sub_call:
            # ★ 优先从 payload 读，兼容旧的 meta 写法
            _p = envelop.payload if isinstance(envelop.payload, dict) else {}
            description = _p.get("description", "") or envelop.meta.get("_description", "")
            _parent = (
                _p.get("parent_session", "")
                or envelop.meta.get("_parent_session", "")
                or envelop.meta.get("callback_session_id", "")
                or ""
            )
            _SUB_PARENTS[session_id] = _parent
            Logger.info(f"[main_agent] 分身同步执行 session={session_id}")
            await _push_progress(
                agent, user_id, session_id, "thinking", "🧠 正在思考...",
                extra={"description": description,
                       "parent_session": _SUB_PARENTS.get(session_id, "")},
            )
            flow = _get_or_create_flow(user_id, session_id)
            await flow.append_user(user_input)
            if hasattr(agent.llm, "reset_round"):
                agent.llm.reset_round(session_id)
            core_processor._think_sessions.add(session_id)
            _WS_DEAD[session_id] = False   # ★ 分身回合开始：重置
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
                core_processor.clear_interrupt(session_id)
                _WS_DEAD.pop(session_id, None)   # ★ 分身回合结束清理

            # ★ 显式带回 task_id / parent_session，供 task_manager 认领
            envelop.payload = {
                "ok": True,
                "call": result.get("call", "reply"),
                "content": result.get("content", ""),
                "args": {},
                "task_id": _p.get("task_id", "") or envelop.meta.get("_task_id", ""),
                "parent_session": _parent,
            }
            return envelop

        # 6. 正常流程（普通对话，异步）
        Logger.info(f"用户输入 [{session_id}]: {user_input[:60]}")
        await _push_progress(agent, user_id, session_id, "thinking", "🧠 正在思考...")
        flow = _get_or_create_flow(user_id, session_id)
        # ★ 不立即 append_user：等 _run_processing 拿到 session 锁后再写，
        #   避免心跳/用户消息在递归进行中插到当前轮前面，把信息流打断。
        flow._pending_user_input = user_input
        if hasattr(agent.llm, "reset_round"):
            agent.llm.reset_round(session_id)
        # ★ 新 user 消息时，清零 empty_output 重试计数
        retry_key = f"{session_id}:empty_output"
        if hasattr(core_processor, "_empty_retries"):
            core_processor._empty_retries[retry_key] = 0

        # ★ 动态基线：让 LLM 从历史 #N 里选相关轮 + 总结任务意图 + 预选 skill
        #   推送 fire-and-forget，不阻塞基线判定
        asyncio.create_task(_push_progress(
            agent, user_id, session_id, "analyzing",
            "🔍 正在分析任务关联...",
            extra={"status": "analyzing_baseline"},
        ))
        try:
            baseline, task_summary, picked_skills = await _detect_baseline_rounds(agent, flow, user_input)
            flow._current_baseline = baseline
            flow._task_summary = task_summary
            flow._picked_skills = picked_skills
            Logger.info(
                f"[baseline] session={session_id} 选中轮次={baseline} "
                f"任务={task_summary!r} 预选skill={[s['name'] for s in picked_skills]}"
            )
            if baseline is None:
                asyncio.create_task(_push_progress(
                    agent, user_id, session_id, "analyzed",
                    "🔍 分析完成：降级固定最近 8 轮",
                    extra={"status": "baseline_ready", "degraded": True, "rounds": []},
                ))
            elif not baseline:
                asyncio.create_task(_push_progress(
                    agent, user_id, session_id, "analyzed",
                    "🔍 分析完成：新任务，未引用历史",
                    extra={"status": "baseline_ready", "degraded": False, "rounds": []},
                ))
            else:
                idx = "、".join(f"#{n}" for n in sorted(baseline))
                asyncio.create_task(_push_progress(
                    agent, user_id, session_id, "analyzed",
                    f"🔍 分析完成：关联 {idx}",
                    extra={"status": "baseline_ready", "degraded": False, "rounds": sorted(baseline)},
                ))
        except Exception as e:
            Logger.warn(f"[baseline] 判定异常，降级固定基线: {e}")
            flow._current_baseline = None
            flow._task_summary = ""
            flow._picked_skills = []
            asyncio.create_task(_push_progress(
                agent, user_id, session_id, "analyzed",
                "🔍 分析失败：降级固定最近 8 轮",
                extra={"status": "baseline_ready", "degraded": True, "rounds": []},
            ))

        core_processor._think_sessions.add(session_id)
        _WS_DEAD[session_id] = False   # ★ 新回合开始：重置无前端标记
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

    payload = envelop.payload if isinstance(envelop.payload, dict) else {}
    # ★ 优先从 payload 读
    task_id = payload.get("task_id", "") or envelop.meta.get("_task_id", "")
    task_summary = envelop.meta.get("_task_summary", "") or payload.get("task_summary", "")



    payload = envelop.payload


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
    # ★ 回调回合开始：重置无前端标记
    _WS_DEAD[session_id] = False
    try:
        result = await core_processor.process(agent, user_id, session_id, flow)
        final_content = result.get("content", "")
        if result.get("interrupted"):
            final_content = ""

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
        core_processor.clear_interrupt(session_id)
        # ★ 回调回合结束清理
        _WS_DEAD.pop(session_id, None)

    envelop.payload = {"ok": ok, "content": final_content}
    return envelop


def help():
    return {
        "route": "builtins/agents/main_agent",
        "description": "AICP 主控制台 Agent — tool use 版（三个）",
        "tools": ["reply", "use_tool", "manage_flow"],
        "features": [
            "原生 tool use（chat_tool_stream），无 JSON 解析 / 熔断 / @@CONTENT@@",
            "三个 tool：reply（回复）/ use_tool（调插件）/ manage_flow（控制信息流）",
            "信息流 / 经验背包 / 任务看板 / 分身任务摘要全保留",
            "回调处理 / 系统 action / 文件上传 / 异常回退全保留",
            "自调用拦截 / callback 参数提取全保留",
            "推送补全：thinking / summary_stream / raw / executing / done / error / callback",
        ]
    }
