"""os/_mcp_server — MCP Server 入口插件（只暴露 call_plugin）

定位：当客户端不支持 HTTP 时，提供一个 MCP 入口来执行能力。
- 只暴露一个工具：call_plugin
- 接收 receiver / action / params，转成 Envelop，route 转发
- 认证走 os/_auth（source=mcp，跳过公开路由）
- initialize 总是建 session，返回 Mcp-Session-Id

启动：
    Envelop(sender="__main__", receiver="os/_mcp_server",
            payload={"action": "START", "host": "0.0.0.0", "port": 9100})
"""

import asyncio
import json
import time
import uuid
from collections import defaultdict
from aiohttp import web

import core


# ============================================================
# 常量
# ============================================================

PROTOCOL_VERSION = "2025-11-25"
SERVER_NAME = "aicp-mcp-server"
SERVER_VERSION = "1.0.0"

# JSON-RPC 错误码
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603
ERR_UNAUTHORIZED = -32001
ERR_FORBIDDEN = -32003
ERR_TIMEOUT = -32011
ERR_RATE_LIMITED = -32012
ERR_SESSION_EXPIRED = -32013

# Session
SESSION_IDLE_TIMEOUT = 3600
SESSION_CLEANUP_INTERVAL = 300

# 限流
MAX_CONCURRENT_PER_SESSION = 10
RATE_LIMIT_PER_MINUTE = 120

# 工具超时（秒）
TOOL_TIMEOUT = {
    "builtins/tools/fetch": 60,
    "builtins/tools/file_utils_api": 30,
    "builtins/tools/cogitor": 15,
    "builtins/tools/contract_agent": 30,
    "builtins/tools/aicp_chat": 120,
    "default": 60,
}


# ============================================================
# Session Store
# ============================================================

class SessionStore:
    def __init__(self):
        self.sessions = {}
        self.token_map = {}
        self._lock = asyncio.Lock()

    async def create(self, token, user_id="anonymous", permissions=None, channel_id=None):
        async with self._lock:
            session_id = f"sess_{uuid.uuid4().hex[:16]}"
            session = {
                "session_id": session_id,
                "token": token,
                "user_id": user_id,
                "permissions": permissions or {"*": True},
                "channel_id": channel_id or f"ch_{uuid.uuid4().hex[:12]}",
                "created_at": time.time(),
                "last_active": time.time(),
                "initialized": False,
                "calls": 0,
                "errors": 0,
                "concurrent": 0,
                "rate_window": [],
            }
            self.sessions[session_id] = session
            if token:
                self.token_map[token] = session_id
            return session

    async def get(self, session_id, touch=True):
        async with self._lock:
            s = self.sessions.get(session_id)
            if s and touch:
                s["last_active"] = time.time()
            return s

    async def get_by_token(self, token):
        async with self._lock:
            sid = self.token_map.get(token)
            if sid:
                s = self.sessions.get(sid)
                if s:
                    s["last_active"] = time.time()
                return s
            return None

    async def remove(self, session_id):
        async with self._lock:
            s = self.sessions.pop(session_id, None)
            if s and s.get("token"):
                self.token_map.pop(s["token"], None)
            return s

    async def cleanup(self, max_idle=SESSION_IDLE_TIMEOUT):
        async with self._lock:
            now = time.time()
            expired = [
                sid for sid, s in self.sessions.items()
                if now - s["last_active"] > max_idle
            ]
            for sid in expired:
                s = self.sessions.pop(sid, None)
                if s and s.get("token"):
                    self.token_map.pop(s["token"], None)
            return len(expired)

    async def stats(self):
        async with self._lock:
            return {
                "total": len(self.sessions),
                "active": sum(
                    1 for s in self.sessions.values()
                    if time.time() - s["last_active"] < 300
                ),
            }


# ============================================================
# 指标
# ============================================================

class Metrics:
    def __init__(self):
        self.counters = defaultdict(int)
        self.timings = defaultdict(list)
        self._lock = asyncio.Lock()

    async def inc(self, name, value=1):
        async with self._lock:
            self.counters[name] += value

    async def observe(self, name, ms):
        async with self._lock:
            self.timings[name].append(ms)
            if len(self.timings[name]) > 1000:
                self.timings[name] = self.timings[name][-1000:]

    async def snapshot(self):
        async with self._lock:
            result = {"counters": dict(self.counters)}
            p95 = {}
            for name, vals in self.timings.items():
                if vals:
                    sorted_vals = sorted(vals)
                    idx = int(len(sorted_vals) * 0.95)
                    p95[name] = sorted_vals[min(idx, len(sorted_vals) - 1)]
            result["p95_ms"] = p95
            return result


# ============================================================
# 自描述（initialize 注入，skill 零复制的关键）
# ============================================================

AICP_INSTRUCTIONS = """你是接入 AICP 引擎的外部 agent。AICP 不是工具集合，是能力底座：插件提供能力，你负责编排。

## 五个工具
- call_plugin(receiver, action?, params?)：万能调用，receiver 是插件完整路由
- list_capabilities(query?)：查系统现在有什么能力（先查再写）
- get_contract(plugin, detail?)：拿插件的参数契约 + 完整用法（detail=tools 要 schema，source 要源码）
- my_tasks(action?)：你的任务看板，存在服务器上，换客户端（claude/opencode/cursor）不丢。
  resume=取待确认清单（新对话开工前先调）/ list / upsert / close / park / delete / summary
- my_memory(action?)：你的经验背包，同样存在服务器上。
  append / search / replace / clear。记的是"用 AICP 干活的坑与做法"，用用户原话。
  ⚠ 不要用 builtins/tools/add_experience，那是 AICP 主控自己的背包，你调它等于写进别人的账本。

## 工作台协议（每轮对话都要走）
1. 开工前先 my_tasks resume，把里面的任务念给用户，问"继续吗"。用户确认才往下做；说先不做就 park，不要自己替他开工。
2. 每完成一步就 my_tasks upsert 更新 progress / next_action / anchor（next_action 必须可执行、可验证：给 URL 或 API 或文件路径）。
3. 收尾 my_tasks close，并至少 my_memory append 一条，内容用用户原话，你只补 kind / tags / project。
4. 下次会话即使没有 initialize 注入，也先 resume 一次——不要凭记忆猜上次做到哪。
5. 若上下文里没有"你的工作台"区块，说明本次没注入，第一件事就是 my_tasks resume。

## 调用格式（唯一硬规则）
action 只写一处：写在顶层 action 字段，params 里绝对不要再写一份。
  对：{"receiver":"applications/x/y","action":"add","params":{"name":"A4"}}
  错：{"receiver":"applications/x/y","action":"add","params":{"action":"add","name":"A4"}}
服务端会丢掉 params.action（以顶层为准），但重复写法是噪音，不要写。
没有 action 的插件（如 code_runner、save_blocks）就只传 params。
action 只写一处：写在顶层 action 字段，params 里绝对不要再写一份。
## 路由规则
receiver 必须写完整路径，不是插件名：
  builtins/tools/code_runner、builtins/tools/save_blocks、os/_cron、applications/<project>/<name>
写 code_runner 会 plugin not found。

## 常规路径
1. 先 list_capabilities 看有没有现成的，能复用就不新写
2. 算数据 / 爬网页 / 调 API / 处理文件 → builtins/tools/code_runner
   code 必须含 async def execute(envelop, agent):，且 return {"data": ...}
   代码若调用 agent.llm：必须先 `if not agent.llm:` 兜底；若工具结果带 llm_notice 或提示「未配置模型」，
   说明引擎 aicp.yaml 没配 models.providers，告知用户先配置模型，不要重试 LLM 调用
3. 定时 / 实时推送 / 持久化 / 串行编排 → os/_cron、os/_websocket、os/_db、os/_workflow
4. 参数不确定 → get_contract；要源码 → get_contract(plugin="<receiver>", detail="source")，它返回该插件 execute 的源码片段（detail="tools" 给 Function Calling schema）
5. ⚠ 不要在你本地磁盘上找引擎：引擎和你的工作目录不是同一处，`where /r C:\ save_blocks.py`、`dir C:\aicp-engin`、`ls ~/aicp*` 这类搜索全是白费功夫，会把一轮对话耗光。源码只能通过 get_contract(detail="source") / call_plugin 拿；契约里出现的 `source` / `contract_file` 是服务器侧路径，你打不开，也不要去开。想确认某个能力有没有，用 list_capabilities，不要翻文件系统。

## 造应用（必须按规范，不许猜结构）
第一步永远先 get_contract(plugin="builtins/tools/save_blocks")，它会返回完整造应用规范
（app.yaml / 后端插件 / 前端三件套的模板、块格式、铁律、踩坑）。严格照它写。
不要靠读现成应用代码猜结构——旧应用可能是错的；规范有冲突时以 usage 为准。
一次 save_blocks 只传一个文件块（APP → PLUGIN → HTML），save 后看返回的 checks/errors。
errors 非空必须修完再交付；warnings 要读一遍判断是否影响功能。

## 交付：直接给可点击链接
save_blocks 返回 preview_url（形如 http://127.0.0.1:9000/<project>/）。回复用户时必须原样给一行 markdown 链接：
  [打开应用](http://127.0.0.1:9000/<project>/)
不要只说“已创建”，也不要让用户自己去拼地址。
其他插件返回 url / open_url / link 字段时，同样原样给用户。

## 节奏
一次只做一个动作，拿到结果再决定下一步；不要试图在一次调用里做完所有事。
连续两次同样错误 → 换思路，或把错误原样告诉用户，不要重复重试。

## 结果太长 / 出现缓存文件时
如果工具结果被客户端写进了缓存文件（`cline://cache/...`、`*.result.txt` 这类），说明那一块正文太长被客户端截出去了。
- ⚠ 不要去逐行读那个缓存文件，更不要因为读不全就转去本机磁盘搜插件源码（那是服务器侧路径，你本地没有）
- get_contract 现在已经把用法正文切成多段（每段 <2600 字）放在同一次返回里，按【用法正文 1/N】顺序看完即可，内容完整未省略
- 仍然觉得不够 → 再调一次 get_contract 并用 detail="tools" 或 detail="source" 精确取你要的那一部分

## 安全
AICP 默认放开本机插件调用，包含 code_runner（可以执行任意 Python）。
删除文件、改系统配置、发布应用前先跟用户确认。AICP 只应绑定 127.0.0.1，不要暴露到公网。
部分内部工具（经验/记忆/技能/内部分身任务/MCP 自身/重启引擎/定时器停止等）不对 MCP 开放，调用返回 banned: ... —— 这是策略不是故障，不要重试，换公开插件或用 code_runner 自己实现；名单见 config/mcp_policy.yaml。
"""


def _route_list_capabilities(args):
    """list_capabilities → cogitor.search / list_plugins"""
    terms = args.get("terms") or []
    if isinstance(terms, str):
        terms = [t for t in terms.replace(",", " ").split() if t]
    if not terms:
        query = (args.get("query") or "").strip()
        terms = [t for t in query.replace(",", " ").split() if t]
    if terms:
        try:
            limit = int(args.get("limit") or 10)
        except (TypeError, ValueError):
            limit = 10
        return "builtins/tools/cogitor", "search", {"terms": terms, "limit": limit}
    inner = {}
    if args.get("category"):
        inner["category"] = args["category"]
    return "builtins/tools/cogitor", "list_plugins", inner


def _route_get_contract(args):
    """get_contract → contract_agent.get / get_tools / get_execute_only"""
    plugin = (args.get("plugin") or "").strip()
    detail = (args.get("detail") or "get").strip()
    if detail in ("tools", "schema", "function_calling"):
        return "builtins/tools/contract_agent", "get_tools", {"plugin": plugin}
    if detail in ("source", "execute", "code"):
        return "builtins/tools/contract_agent", "get_execute_only", {"plugin": plugin}
    return "builtins/tools/contract_agent", "get", {"plugin": plugin}


def _route_my_memory(args):
    """my_memory → os/_mcp_agent（外部 agent 的经验背包，个人版单用户全局共享）"""
    inner = dict(args or {})
    action = str(inner.pop("action", "") or "search").strip()
    mapping = {
        "append": "memory_append",
        "search": "memory_search",
        "replace": "memory_replace",
        "clear": "memory_clear",
    }
    return "os/_mcp_agent", mapping.get(action, action), inner


def _route_my_tasks(args):
    """my_tasks → os/_mcp_agent（外部 agent 的任务看板 + 跨客户端接手）"""
    inner = dict(args or {})
    action = str(inner.pop("action", "") or "resume").strip()
    mapping = {
        "list": "tasks_list",
        "upsert": "tasks_upsert",
        "close": "tasks_close",
        "park": "tasks_park",
        "delete": "tasks_delete",
        "resume": "resume",
        "summary": "summary",
    }
    return "os/_mcp_agent", mapping.get(action, action), inner


# ---------------- MCP 对外黑名单 ----------------
# 内部专属/系统要害工具不对 MCP 开放。最终名单 = 代码默认 + config/mcp_policy.yaml 追加（不支持删默认项）。
DEFAULT_BLACKLIST = {
    "builtins/tools/add_experience",       # 内部经验背包
    "builtins/tools/add_task_board",       # 内部任务看板
    "builtins/tools/search_memory_agent",  # 内部记忆检索
    "builtins/tools/skill_loader",         # 内部技能管理
    "builtins/tools/task_manager",         # 内部分身多任务
    "os/_mcp_server",                      # MCP 自身（防改配置/关端点）
    "os/restart",                          # 重启引擎
    "os/_cron:STOP",                       # 只禁停定时器
    "builtins/control/_settings",          # 改引擎设置
}
_BANNED = set()
_BS = chr(92)


def _load_policy_blacklist():
    rules = set(DEFAULT_BLACKLIST)
    try:
        import yaml
        from pathlib import Path
        f = Path("config/mcp_policy.yaml")
        if f.exists():
            cfg = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            extra = (cfg.get("mcp") or {}).get("blacklist") or []
            if isinstance(extra, list):
                rules |= {str(x).strip().replace(_BS, "/") for x in extra if str(x).strip()}
    except Exception:
        pass
    _BANNED.clear()
    _BANNED.update(rules)


def _banned(receiver, action=None):
    if not _BANNED:
        _load_policy_blacklist()
    name = (receiver or "").strip().replace(_BS, "/")
    if not name:
        return False
    if name in _BANNED:
        return True
    return bool(action) and (name + ":" + str(action)) in _BANNED


def _banned_call(tool_name, receiver, action=None, params=None):
    if _banned(receiver, action):
        return True
    if tool_name == "get_contract":
        return _banned(str((params or {}).get("plugin") or "").strip())
    return False


def _filter_banned_from_result(result):
    # list_capabilities 三种返回形态统一过滤黑名单插件（对外不可见）
    if not isinstance(result, dict):
        return result
    if isinstance(result.get("results"), list):
        result["results"] = [r for r in result["results"] if not _banned((r or {}).get("name", ""))]
        result["total"] = len(result["results"])
    if isinstance(result.get("plugins"), list):
        result["plugins"] = [x for x in result["plugins"] if not _banned((x or {}).get("name", ""))]
        result["total"] = len(result["plugins"])
    groups = result.get("groups")
    if isinstance(groups, dict):
        for info in groups.values():
            if not isinstance(info, dict):
                continue
            pl = info.get("plugins")
            if isinstance(pl, list):
                info["plugins"] = [x for x in pl if not _banned(x if isinstance(x, str) else (x or {}).get("name", ""))]
            projs = info.get("projects")
            if isinstance(projs, dict):
                for proj in list(projs.keys()):
                    lst = projs[proj]
                    if isinstance(lst, list):
                        projs[proj] = [x for x in lst if not _banned(x if isinstance(x, str) else (x or {}).get("name", ""))]
    return result


META_TOOL_ROUTES = {
    "list_capabilities": _route_list_capabilities,
    "get_contract": _route_get_contract,
    "my_memory": _route_my_memory,
    "my_tasks": _route_my_tasks,
}


def _skill_text_for(plugin: str) -> str:
    """读插件的用法正文。覆盖三种来源：
    - 内部插件：plugins/<route>.py 同目录 <stem>_skill.md
    - skill/xxx：data/skills/xxx/SKILL.md（或 skill/plugin/... → plugins/...）
    - mcp/server/tool：data/os/_mcp_client/contracts/<server>.<tool>.json 的 description
    """
    import pathlib
    import json

    name = (plugin or "").strip().replace("\\", "/")
    if not name:
        return ""

    def _strip_fm(text: str) -> str:
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                return parts[2].strip()
        return text.strip()

    # ---- skill/xxx ----
    if name.startswith("skill/"):
        rel = name[len("skill/"):].strip("/")
        # skill/plugin/<ppath>/<stem> → plugins/<ppath>/<stem>_skill.md
        if rel.startswith("plugin/"):
            sub = rel[len("plugin/"):].strip("/")
            cand = pathlib.Path("plugins") / sub
            if not cand.name.endswith("_skill.md"):
                cand = cand.with_name(cand.name + "_skill.md")
            if cand.exists():
                return _strip_fm(cand.read_text(encoding="utf-8"))
        # data/skills/<rel>/SKILL.md
        for base in (pathlib.Path("data/skills"), pathlib.Path("data/skills/aicp")):
            cand = base / rel
            if not cand.name.endswith("SKILL.md"):
                cand = cand / "SKILL.md"
            if cand.exists():
                return _strip_fm(cand.read_text(encoding="utf-8"))
        return ""

    # ---- mcp/server/tool ----
    if name.startswith("mcp/"):
        parts = name.split("/")
        if len(parts) < 3:
            return ""
        server, tool = parts[1], parts[2]
        f = pathlib.Path("data/os/_mcp_client/contracts") / f"{server}.{tool}.json"
        if not f.exists():
            return ""
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            return (data.get("description") or "").strip()
        except Exception:
            return ""

    # ---- 内部插件：plugins/<route>.py 同目录 <stem>_skill.md ----
    if name.endswith(".py"):
        name = name[:-3]
    root = pathlib.Path("plugins")
    path = root / (name + ".py")
    if not path.exists():
        hits = sorted(root.rglob(name.split("/")[-1] + ".py"))
        if not hits:
            return ""
        path = hits[0]
    skill_file = path.parent / (path.stem + "_skill.md")
    if not skill_file.exists():
        return ""
    return _strip_fm(skill_file.read_text(encoding="utf-8"))


# ============================================================
# 工具定义（3 个元工具；applications/* 不进 tools/list，避免上下文爆炸）
# ============================================================

MCP_TOOLS = [
    {
        "name": "call_plugin",
        "description": (
            "调用任意 AICP 插件。receiver 必须是完整路由，不是插件名："
            "builtins/tools/code_runner / builtins/tools/save_blocks / os/_cron / applications/<project>/<name>。"
            "示例1 算数据：{receiver:'builtins/tools/code_runner', params:{code:'async def execute(envelop, agent):\\n    return {\"data\": 1+1}'}}；"
            "示例2 造应用：{receiver:'builtins/tools/save_blocks', params:{text:'=== PLUGIN: ... ===\\n...\\n=== END ==='}}。"
            "不知道有什么能力先调 list_capabilities；不知道参数先调 get_contract。"
        ),
        "inputSchema": {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {
                "receiver": {
                    "type": "string",
                    "description": "插件完整路由，如 builtins/tools/code_runner、os/_websocket、applications/todo/todo",
                },
                "action": {"type": "string", "description": "插件 action，可选（如 list / add / START）"},
                "params": {"type": "object", "description": "插件参数", "additionalProperties": True},
            },
            "required": ["receiver"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_capabilities",
        "description": (
            "查询 AICP 当前有哪些能力（插件）。先查再写：能复用现成插件就不要新造。"
            "传 query 按关键词模糊搜；不传则列出系统插件全图。"
        ),
        "inputSchema": {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词，可空格分隔多个，如 '定时 推送 网页'"},
                "terms": {"type": "array", "items": {"type": "string"}, "description": "关键词数组，等价 query"},
                "category": {"type": "string", "description": "限定分类：builtins / os / applications"},
                "limit": {"type": "integer", "description": "最多返回条数，默认 10"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_contract",
        "description": (
            "查询某个插件的参数契约与用法（action 列表、params 结构、返回格式、示例）。"
            "plugin 可传完整路由或插件名，支持模糊匹配。detail=source 时直接给 execute 源码。"
        ),
        "inputSchema": {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {
                "plugin": {"type": "string", "description": "插件路由或名称，如 builtins/tools/save_blocks 或 save_blocks"},
                "detail": {
                    "type": "string",
                    "enum": ["get", "tools", "source"],
                    "description": "get=契约+用法（默认）；tools=Function Calling schema；source=execute 源码",
                },
            },
            "required": ["plugin"],
            "additionalProperties": False,
        },
    },
    {
        "name": "my_memory",
        "description": (
            "你（外部 agent）的经验背包，存在 MCP 服务器上，换客户端（claude/opencode/cursor）不丢。"
            "什么时候必须调：踩到坑并验证后、用户说'记一下'、收尾前。"
            "action: append(记一条) / search(按关键词取正文) / replace(改一条) / clear(清空，需 confirm)。"
            "记正文用用户原话，你只补 kind/tags/project。不要调 builtins/tools/add_experience（那是 AICP 主控的背包）。"
        ),
        "inputSchema": {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["append", "search", "replace", "clear"], "description": "默认 search"},
                "title": {"type": "string", "description": "一句话标题（append/replace）"},
                "experience": {"type": "string", "description": "正文：故障现象 → 根因 → 修法/判据（append/replace）"},
                "kind": {"type": "string", "enum": ["pitfall", "howto", "decision"], "description": "默认 pitfall"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "检索标签"},
                "project": {"type": "string", "description": "归属项目（可选）"},
                "query": {"type": "string", "description": "search 关键词，可空格分隔"},
                "terms": {"type": "array", "items": {"type": "string"}, "description": "search 关键词数组，等价 query"},
                "id": {"type": "string", "description": "replace 的目标，如 E3"},
                "limit": {"type": "integer", "description": "search 返回条数，默认 5"},
                "confirm": {"type": "boolean", "description": "clear 必须传 true"},
            },
            "additionalProperties": True,
        },
    },
    {
        "name": "my_tasks",
        "description": (
            "你（外部 agent）的任务看板，存在 MCP 服务器上，换客户端照样接手。"
            "什么时候必须调：新对话开工前先 resume；每完成一步 upsert 更新 progress/next_action/anchor；收尾 close。"
            "action: resume(取待确认清单) / list / upsert(新建或更新) / close(完成) / park(用户说先不做) / delete(物理删除，删自检/误建任务用) / summary。"
            "doing 只允许 1 个焦点（新建 doing 会自动把旧的降为 paused）。用户确认继续后才 upsert 为 doing。"
        ),
        "inputSchema": {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["resume", "list", "upsert", "close", "park", "delete", "summary"], "description": "默认 resume"},
                "id": {"type": "string", "description": "任务 id，如 t-3；新建不要传"},
                "title": {"type": "string", "description": "任务标题"},
                "project": {"type": "string", "description": "归属项目"},
                "status": {"type": "string", "enum": ["todo", "doing", "paused", "blocked", "done"], "description": "doing 需用户确认后才设"},
                "next_action": {"type": "string", "description": "接手方要做的下一步（必须可执行、可验证）"},
                "progress": {"type": "string", "description": "当前进度"},
                "anchor": {"type": "object", "description": "验证锚点 {url, api, files}", "additionalProperties": True},
                "tried": {"type": "array", "items": {"type": "string"}, "description": "已试过且失败的路，接手方别重走"},
                "note": {"type": "string", "description": "本条变更备注"},
                "exp_id": {"type": "string", "description": "关联经验 id，如 E3"},
                "expect_rev": {"type": "integer", "description": "乐观锁：传任务当前 rev，不匹配则拒绝"},
                "limit": {"type": "integer", "description": "list 返回条数，默认 50"},
            },
            "additionalProperties": True,
        },
    },
]


# ============================================================
# 权限
# ============================================================

def _check_permission(session, receiver, action=None):
    perms = session.get("permissions") or {}
    if perms.get("*"):
        return True
    for prefix, allowed in perms.items():
        if prefix == "*":
            continue
        if allowed and receiver.startswith(prefix):
            if isinstance(allowed, dict):
                if action is None:
                    return True
                return allowed.get(action, False) or allowed.get("*", False)
            return True
    return False


# ============================================================
# 限流
# ============================================================

def _check_rate_limit(session):
    now = time.time()
    window = session["rate_window"]
    session["rate_window"] = [t for t in window if now - t < 60]
    if len(session["rate_window"]) >= RATE_LIMIT_PER_MINUTE:
        return False
    session["rate_window"].append(now)
    return True


# ============================================================
# 插件入口
# ============================================================

async def execute(envelop, agent):
    action = envelop.payload.get("action", "START")

    if action == "START":
        return await _start(envelop, agent)
    elif action == "STOP":
        return await _stop(envelop, agent)
    elif action == "sessions":
        store = getattr(agent, "_mcp_sessions", None)
        if store:
            envelop.payload = {"ok": True, "data": await store.stats()}
        else:
            envelop.payload = {"ok": True, "data": {"total": 0, "active": 0}}
        return envelop
    elif action == "metrics":
        metrics = getattr(agent, "_mcp_metrics", None)
        if metrics:
            envelop.payload = {"ok": True, "data": await metrics.snapshot()}
        else:
            envelop.payload = {"ok": True, "data": {}}
        return envelop
    elif action == "tools":
        envelop.payload = {"ok": True, "data": {"tools": MCP_TOOLS, "count": len(MCP_TOOLS)}}
        return envelop
    else:
        envelop.payload = {"ok": False, "error": f"Unknown action: {action}"}
        return envelop


async def _start(envelop, agent):
    host = envelop.payload.get("host", "127.0.0.1")
    port = envelop.payload.get("port", 9100)

    if not hasattr(agent, "_mcp_sessions"):
        agent._mcp_sessions = SessionStore()
    if not hasattr(agent, "_mcp_metrics"):
        agent._mcp_metrics = Metrics()
    store = agent._mcp_sessions
    metrics = agent._mcp_metrics

    app = web.Application(client_max_size=10 * 1024 * 1024)

    @web.middleware
    async def cors(request, handler):
        if request.method == "OPTIONS":
            return web.Response(status=200, headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization, Mcp-Session-Id, MCP-Protocol-Version",
            })
        resp = await handler(request)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    app.middlewares.append(cors)

    def _rpc_ok(req_id, result):
        return web.json_response({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _rpc_error(req_id, code, message, data=None):
        err = {"code": code, "message": message}
        if data:
            err["data"] = data
        return web.json_response({"jsonrpc": "2.0", "id": req_id, "error": err})

    # 单块正文上限：客户端（Cline / VS Code 系）对超长工具结果会落成 cache 文件，
    # 那个文件是单行 JSON，agent 读不全，于是转去翻磁盘找源码——白烧一轮对话。
    # 所以长正文必须切段，每段都短，让它留在对话框里。
    MCP_BLOCK_CHARS = 2600

    def _to_mcp_content(result, is_error=False):
        if not isinstance(result, dict):
            return {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}],
                "isError": is_error,
            }

        usage = result.get("usage")
        if not isinstance(usage, str) or len(usage) <= MCP_BLOCK_CHARS:
            return {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}],
                "isError": is_error,
            }

        head = {k: v for k, v in result.items() if k != "usage"}
        head["usage_chars"] = len(usage)
        head["usage_blocks"] = (len(usage) + MCP_BLOCK_CHARS - 1) // MCP_BLOCK_CHARS
        head["usage_note"] = (
            "用法正文在后续 %d 段里完整给出（未省略）。直接按这些段照做；"
            "不要去读客户端缓存文件、不要在本机磁盘上搜 plugins/ 或 www/——"
            "那些路径在服务器侧，你本地没有。" % head["usage_blocks"]
        )

        blocks = [{"type": "text", "text": json.dumps(head, ensure_ascii=False, default=str)}]
        for idx in range(0, len(usage), MCP_BLOCK_CHARS):
            part = idx // MCP_BLOCK_CHARS + 1
            blocks.append({
                "type": "text",
                "text": "【用法正文 %d/%d】\n%s" % (part, head["usage_blocks"], usage[idx:idx + MCP_BLOCK_CHARS]),
            })

        return {"content": blocks, "isError": is_error}

    async def _log(level, event, **kwargs):
        try:
            msg = json.dumps({"event": event, **kwargs}, ensure_ascii=False, default=str)
            if level == "info":
                agent.log.info(f"[MCP] {msg}")
            elif level == "warning":
                agent.log.warning(f"[MCP] {msg}")
            else:
                agent.log.error(f"[MCP] {msg}")
        except Exception:
            pass

    # ---------- 认证（走 os/_auth，source=mcp）----------
    async def _verify_token(token, request=None):
        if not token:
            return None
        auth_env = core.Envelop(
            sender="os/_mcp_server",
            receiver="os/_auth",
            payload={"action": "verify", "token": token},
            meta={
                "token": token,
                "source": "mcp",
                # ★ 来源信息：供 os/_auth 的 local / hybrid 护栏判断
                "remote": (request.remote if request else "") or "",
                "forwarded_for": (request.headers.get("X-Forwarded-For", "") if request else ""),
            },
        )
        try:
            result = await agent.system.call(auth_env)
            if result and result.payload.get("ok"):
                return {
                    "user_id": result.payload.get("user_id", "local"),
                    "permissions": result.payload.get("permissions", {"*": True}),
                }
        except Exception as e:
            await _log("error", "auth_error", error=str(e))
        return None

    async def _resolve_auth_mode():
        # 复用 os/_auth 的 mode action，和 os/_gateway 的认证调用同一套判断
        try:
            env = core.Envelop(
                sender="os/_mcp_server",
                receiver="os/_auth",
                payload={"action": "mode"},
            )
            r = await agent.system.call(env)
            mode = ((r.payload or {}) or {}).get("mode")
            return mode if mode in ("local", "token", "hybrid") else "local"
        except Exception:
            return "local"

    # ---------- session 解析：总是返回一个 session ----------
    async def _resolve_session(request):
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        session_id = request.headers.get("Mcp-Session-Id", "")

        # 1. 优先 session_id
        if session_id:
            s = await store.get(session_id)
            if s:
                return s
            # session 过期，重建
            await _log("warning", "session_rebuild", old=session_id)

        # 2. 按 token 查
        if token:
            s = await store.get_by_token(token)
            if s:
                return s

        # 3. 验证 token（如果带了）
        info = await _verify_token(token, request) if token else None
        if not info:
            mode = await _resolve_auth_mode()   # 实时问 os/_auth，模式改动即时生效
            if token and mode != "local":
                # 带了 token 但验证失败，且不是 local 模式 → 明确拒绝
                return None   # 上层 handle_mcp 返回 401
            if not token and mode == "token":
                # token 模式必须带 token
                return None
            # local 模式 / hybrid 本机 → 兜底放行
            info = {"user_id": "local", "permissions": {"*": True}}

        # 4. 建 session
        s = await store.create(
            token or "",
            user_id=info["user_id"],
            permissions=info["permissions"],
        )
        return s

    async def _call_internal(session, receiver, action=None, params=None):
        payload = dict(params or {})
        if action:
            payload["action"] = action
        # clientInfo 只记录不鉴权：把 initialize 时记下的客户端名带下去，
        # 否则 history / profile 里永远是 unknown，排障时分不清是哪个客户端干的。
        # 不覆盖调用方显式传的 client。
        if session.get("client_name") and not payload.get("client"):
            payload["client"] = session["client_name"]

        env = core.Envelop(
            sender="os/_mcp_server",
            receiver=receiver,
            payload=payload,
            meta={
                "session_id": session["session_id"],
                "user_id": session["user_id"],
                "channel_id": session["channel_id"],
                "source": "mcp",
            },
        )

        timeout = TOOL_TIMEOUT.get(receiver, TOOL_TIMEOUT["default"])
        start = time.time()
        try:
            result = await asyncio.wait_for(agent.system.call(env), timeout=timeout)
            elapsed = (time.time() - start) * 1000
            await metrics.observe("call_plugin", elapsed)
            await metrics.inc("call_plugin_ok")
            # route 保证返回 Envelop，统一读法
            return result.result()
        except asyncio.TimeoutError:
            await metrics.inc("call_plugin_timeout")
            return {"ok": False, "error": f"timeout after {timeout}s"}
        except Exception as e:
            await metrics.inc("call_plugin_error")
            await _log("error", "plugin_error", receiver=receiver, error=f"{type(e).__name__}: {e}")
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ---------- MCP 主处理 ----------
    async def handle_mcp(request):
        accept = request.headers.get("Accept", "")
        if "application/json" not in accept and "text/event-stream" not in accept:
            return _rpc_error(None, ERR_INVALID_REQUEST, "Accept must include application/json or text/event-stream")

        try:
            body = await request.json()
        except Exception:
            return _rpc_error(None, ERR_PARSE, "parse error")

        if not isinstance(body, dict):
            return _rpc_error(None, ERR_INVALID_REQUEST, "invalid request")

        method = body.get("method", "")
        req_id = body.get("id")
        params = body.get("params", {})

        # ----- initialize：总是建 session，返回 Mcp-Session-Id -----
        if method == "initialize":
            client_proto = params.get("protocolVersion", PROTOCOL_VERSION)
            session = await _resolve_session(request)

            # clientInfo 只记录不鉴权：个人版 MCP 单用户全局共享，换客户端数据原地不动
            client_info = params.get("clientInfo") or {}
            client_name = str(client_info.get("name") or "").strip()[:60]
            session["client_name"] = client_name

            # 工作台摘要：MCP 无状态，客户端不会主动来查，所以由 initialize 主动回灌
            instructions = AICP_INSTRUCTIONS
            try:
                ws = await _call_internal(session, "os/_mcp_agent", action="summary",
                                          params={"client": client_name})
                if isinstance(ws, dict) and ws.get("ok") and ws.get("text"):
                    instructions = AICP_INSTRUCTIONS + "\n\n---\n\n" + ws["text"]
            except Exception as e:
                await _log("warning", "workspace_summary_failed", error=str(e))

            result = {
                "protocolVersion": client_proto,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                # 自描述：客户端会把这段当系统提示注入，外部 agent 无需先读 skill 文件
                "instructions": instructions,
            }
            resp = _rpc_ok(req_id, result)
            resp.headers["Mcp-Session-Id"] = session["session_id"]
            return resp

        # ----- 其他方法：解析 session（总是能拿到）-----
        session = await _resolve_session(request)
        if session is None:
            return _rpc_error(req_id, ERR_UNAUTHORIZED, "invalid or missing token")

        if method == "notifications/initialized":
            session["initialized"] = True
            return web.json_response({"jsonrpc": "2.0", "id": req_id, "result": {}})

        if method == "ping":
            return _rpc_ok(req_id, {})

        if method == "tools/list":
            return _rpc_ok(req_id, {"tools": MCP_TOOLS})

        if method == "tools/call":
            tool_name = params.get("name", "")
            args = params.get("arguments", {})

            if tool_name != "call_plugin" and tool_name not in META_TOOL_ROUTES:
                return _rpc_error(req_id, ERR_METHOD_NOT_FOUND, f"unknown tool: {tool_name}")

            if not _check_rate_limit(session):
                await metrics.inc("rate_limited")
                return _rpc_error(req_id, ERR_RATE_LIMITED, "rate limited")

            if session["concurrent"] >= MAX_CONCURRENT_PER_SESSION:
                await metrics.inc("concurrent_limited")
                return _rpc_error(req_id, ERR_RATE_LIMITED, "too many concurrent calls")

            # ---- 元工具：先翻译成 (receiver, action, params)，再走同一条执行通路 ----
            if tool_name in META_TOOL_ROUTES:
                try:
                    receiver, action, params_inner = META_TOOL_ROUTES[tool_name](args or {})
                except Exception as e:
                    return _rpc_error(req_id, ERR_INVALID_PARAMS, f"bad arguments: {e}")
                if tool_name == "get_contract" and not params_inner.get("plugin"):
                    return _rpc_error(req_id, ERR_INVALID_PARAMS, "plugin required")
            else:
                receiver = args.get("receiver", "")
                action = args.get("action")
                params_inner = args.get("params", {})

            if not receiver:
                return _rpc_error(req_id, ERR_INVALID_PARAMS, "receiver required")

            # ---- 对外黑名单：内部专属/系统要害工具直接拒绝 ----
            if _banned_call(tool_name, receiver, action, params_inner):
                await metrics.inc("banned_blocked")
                _t = str((params_inner or {}).get("plugin") or receiver) if tool_name == "get_contract" else receiver
                _a = action if (action and (str(_t) + ":" + str(action)) in _BANNED) else None
                _msg = "banned: " + str(_t) + ((":" + str(_a)) if _a else "") + " 不对 MCP 开放（内部专属或策略限制），这是策略不是故障，不要重试；如需调整见 config/mcp_policy.yaml"
                return _rpc_ok(req_id, _to_mcp_content({"ok": False, "error": _msg}, True))

            if not _check_permission(session, receiver, action):
                await metrics.inc("permission_denied")
                return _rpc_error(req_id, ERR_FORBIDDEN, f"permission denied: {receiver}")

            session["concurrent"] += 1
            session["calls"] += 1
            start = time.time()
            try:
                result = await _call_internal(session, receiver, action=action, params=params_inner)

                # get_contract 的 usage 在 AICP 内部只是"已加载"提示；
                # 外部客户端拿不到 active_skills，直接把 skill 正文塞进返回结果。
                if tool_name == "get_contract" and isinstance(result, dict):
                    try:
                        skill_text = _skill_text_for(params_inner.get("plugin") or "")
                        if skill_text:
                            result["usage"] = skill_text
                            result["usage_note"] = "上面是完整用法正文（_skill.md），按它写，不要靠读现成应用猜"
                    except Exception as e:
                        result["usage_read_error"] = str(e)

                # list_capabilities：黑名单插件对外不可见
                if tool_name == "list_capabilities":
                    result = _filter_banned_from_result(result)

                is_error = not result.get("ok", False)
                if is_error:
                    session["errors"] += 1
                return _rpc_ok(req_id, _to_mcp_content(result, is_error))
            finally:
                session["concurrent"] -= 1
                elapsed = (time.time() - start) * 1000
                await _log("info", "tool_call",
                           session=session["session_id"],
                           user=session["user_id"],
                           receiver=receiver,
                           action=action,
                           ms=round(elapsed, 1))

        return _rpc_error(req_id, ERR_METHOD_NOT_FOUND, f"unknown method: {method}")

    async def handle_delete(request):
        session_id = request.headers.get("Mcp-Session-Id", "")
        if not session_id:
            return web.json_response({"error": "Mcp-Session-Id required"}, status=400)
        removed = await store.remove(session_id)
        if removed:
            return web.json_response({"ok": True, "session": session_id})
        return web.json_response({"error": "session not found"}, status=404)

    async def handle_health(request):
        return web.json_response({"status": "ok", **await store.stats()})

    async def handle_ready(request):
        try:
            ready = "os/_mcp_server" in core.plugins
            return web.json_response({"ready": ready, "protocol": PROTOCOL_VERSION})
        except Exception as e:
            return web.json_response({"ready": False, "error": str(e)}, status=503)

    async def handle_metrics(request):
        return web.json_response(await metrics.snapshot())

    app.router.add_post("/mcp", handle_mcp)
    app.router.add_delete("/mcp", handle_delete)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/ready", handle_ready)
    app.router.add_get("/metrics", handle_metrics)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()

    agent._mcp_runner = runner

    async def _cleanup_loop():
        while True:
            try:
                await asyncio.sleep(SESSION_CLEANUP_INTERVAL)
                n = await store.cleanup()
                if n > 0:
                    await _log("info", "session_cleanup", removed=n)
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    agent._mcp_cleanup_task = asyncio.create_task(_cleanup_loop())

    agent.log.info(f"[MCP] Server listening on http://{host}:{port}/mcp")

    envelop.payload = {
        "ok": True,
        "status": "listening",
        "host": host,
        "port": port,
        "url": f"http://{host}:{port}/mcp",
        "protocol": PROTOCOL_VERSION,
    }
    return envelop


async def _stop(envelop, agent):
    store = getattr(agent, "_mcp_sessions", None)
    if store:
        for sid in list(store.sessions.keys()):
            await store.remove(sid)

    task = getattr(agent, "_mcp_cleanup_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        agent._mcp_cleanup_task = None

    runner = getattr(agent, "_mcp_runner", None)
    if runner:
        try:
            await runner.cleanup()
        except Exception:
            pass
        agent._mcp_runner = None

    envelop.payload = {"ok": True, "status": "stopped"}
    return envelop


def help():
    return {
        "route": "os/_mcp_server",
        "description": "MCP Server，只暴露 call_plugin，认证走 os/_auth",
        "actions": {
            "START": {"input": {"host": "默认 127.0.0.1", "port": "默认 9100"}, "output": {"ok": True, "url": "http://host:port/mcp"}},
            "STOP": {"output": {"ok": True, "status": "stopped"}},
            "sessions": {"output": {"ok": True, "data": "session 统计"}},
            "metrics": {"output": {"ok": True, "data": "指标快照"}},
        },
        "mcp_tools": ["call_plugin"],
        "protocol_version": PROTOCOL_VERSION,
    }
