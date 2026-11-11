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
import sys
import time
import uuid

import importlib
import sys

# ★ 延迟导入 _service_restart：加载器按文件名字母序遍历 plugins/**/*.py，
#   os/_service_restart.py 排在本文件之后（s > m），模块级 import 会因
#   「还没加载到」抛 ModuleNotFoundError → 引擎起不来。只在要用时 import。
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

# 请求体上限
# 与网关 os/_gateway.py 共用同一个配置项（aicp.yaml max_body_size，缺省 10MB）。
# MCP 的 JSON-RPC 消息和网关的 JSON API 都是"结构化 JSON 请求体"，
# 体积特征一致，用同一个上限，不搞第三套标准。
DEFAULT_MAX_BODY_SIZE = 10 * 1024 * 1024  # 10MB


def _resolve_mcp_host(agent) -> str:
    """MCP 监听地址：复用全局 host（aicp.yaml host），缺省 127.0.0.1。

    MCP 不单独配 host —— 它跟网关是同一台机器上的兄弟服务，
    监听地址跟着全局 host 走即可，单独配置只会出现两处不一致。
    """
    host = "127.0.0.1"
    if agent is not None:
        cfg = getattr(agent, "config", None) or {}
        host = cfg.get("host", host) or host
    return str(host)


def _resolve_mcp_port(agent) -> int:
    """MCP 监听端口：aicp.yaml mcp.port，缺省 9100。"""
    port = 9100
    if agent is not None:
        cfg = getattr(agent, "config", None) or {}
        mcp_cfg = cfg.get("mcp") or {}
        port = mcp_cfg.get("port", port)
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 9100
    return port if 0 < port < 65536 else 9100


def _resolve_max_body_size(agent) -> int:
    """读取 JSON 请求体上限：aicp.yaml max_body_size，缺省 10MB。
    与网关 os/_gateway.py 的 _resolve_max_body_size 读同一个配置键。"""
    val = agent.config.get("max_body_size", DEFAULT_MAX_BODY_SIZE) if agent else DEFAULT_MAX_BODY_SIZE
    try:
        val = int(val)
    except (TypeError, ValueError):
        val = DEFAULT_MAX_BODY_SIZE
    return val if val > 0 else DEFAULT_MAX_BODY_SIZE

# 限流
MAX_CONCURRENT_PER_SESSION = 10
RATE_LIMIT_PER_MINUTE = 120

# 工具超时（秒）
TOOL_TIMEOUT = {
    "builtins/tools/fetch": 60,
    "builtins/tools/file_utils_api": 30,
    "builtins/tools/cogitor": 15,
    "builtins/tools/contract_agent": 120,
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

# ★ 热重载友好：instructions 必须从「当前活着的模块」读，不能用启动时捕获的模块全局。
#   原因：_start() 创建的 aiohttp 路由闭包持有启动那一刻的模块 globals；
#   热重载会 exec 出新模块对象并重绑 core.plugins，但已在跑的 handler 仍指向旧模块。
#   结果 = 改了提示词但要重启引擎才生效。现改成每次请求现取 live 模块。
_LIVE_MODULE_NAME = "plugin_os__mcp_server"


def _sr():
    """延迟取得 _service_restart 模块（见上方注释：不能模块级 import）。"""
    import importlib
    return importlib.import_module("plugin_os__service_restart")


def _live_instructions() -> str:
    """取当前生效的 AICP_INSTRUCTIONS（热重载后立即变）。"""
    mod = sys.modules.get(_LIVE_MODULE_NAME)
    text = getattr(mod, "AICP_INSTRUCTIONS", "") if mod else ""
    return text or AICP_INSTRUCTIONS


AICP_INSTRUCTIONS = """你是接入 AICP 引擎的外部 agent。AICP 不是工具集合，是实时能力底座：插件提供能力，你负责编排。

# 这个mcp server是什么？
这是一个协议驱动，envelop传递、实时python插件生成、热重载引擎，你写出来的aicp引擎插件、应用，生成即可预览，落盘即下一步即可被调用，你不需要熟悉框架，只精通python3.12和前端开发。
生成即可为客户实时展现效果。你用五个工具可以造出新的引擎内置插件和应用。

## ⚠ 先读这一条：造应用前必须先读 _demo
`_demo` 不是可运行示例，是**强制规范**（源文件里写着「造应用照抄本 demo 时必须遵守」）。
开工前先把它读完，不要边写边猜。它有 5 个文件，全部是规范的一部分：

    plugins/applications/_demo/app.yaml            应用注册清单（name/id/version/static_root）——照抄这个格式
    plugins/applications/_demo/notes.py             后端插件范本（execute 契约、状态持久化、action 分发）
    plugins/applications/_demo/notes.contract.json  action 契约（机器可读，params/returns/confidence）
    plugins/applications/_demo/ping.py              第二个插件：跨插件调用范本
    plugins/applications/_demo/collab.py            协作类插件范本
    www/apps/_demo/index.html                       前端范本（调用后端、上传、错误处理）

读它的收益不是"参考"，是**避开一整类返工**：上传协议、action 命名、payload 结构、
鉴权、状态持久化、错误处理约定，全都在 _demo 里写死了。照抄机制，不要自由发挥。
读法：用 file_utils_api 的 read_file 读全文（大文件会自动返回 code_structure 结构地图）。

**新建应用必须先写 app.yaml**，否则应用不会注册、页面也访问不到。字段照抄 _demo 的：
`name`（显示名）、`id`（项目名，与目录同名）、`version`、`description`、
`description_en`、`static_root`（= www/apps/<id>）。

## aicp 引擎内部写入规则
**可写文件位置
plugins/mythools  你创建的纯后端工具位置
plugins/applications 应用后端文件夹，每个项目一个子目录 {project}/
www/apps/ 应用前端为文件夹，每个项目一个子目录和后端对齐 {project}/   对应实时访问地址 {本机基址}/apps/xxx
data/workspace  你的工作区文件夹，
data/cache      临时产物展示区， 用于引擎页面展示，展示地址：{本机基址}/data/cache/xxxx
data/skills/aicp/{skill_id}  总结skill写入目录（每个 skill 一个子目录，内含 SKILL.md），用于总结自己通过aicp 引擎得到的经验
data/uploads/   用于应用上传功能。 `data/uploads/`（`{本机基址}/data/uploads/xxx`）
其他位置均为只读区域，严禁写入

## 文件操作：只用 file_utils_api（别用客户端自带的编辑/搜索工具）
客户端自带的编辑器和文件搜索**不认识 AICP 的目录约定，也没有写后校验**，用它会多绕很多步。
读写文件一律走 MCP 插件：

    call_plugin(receiver="builtins/tools/file_utils_api", action="<action>", params={...})

12 个 action：
  read_file / read_file_lines   读文件（read_file_lines 可按行范围读）
  write_file / append_file      写文件（append 追加）
  edit_file      改文件（edit_file 按 find→replace 精确替换
  file_exists / list_dir / mkdir / delete_file / file_stat
  code_structure                大文件结构地图
  search                        搜文件

四个必须知道的语义（踩过坑）：

1. **write_file / append_file / edit_file 写完自动校验**，返回里带
   `data.validation = {checked, ok, error, kind}`。
   覆盖 .py(ast.parse) / .json / .html / .js / .ts / .vue 等；
   失败时返回 `validation_failed: true` 和 `warning`（文件已落盘但有语法问题）。
   → **看到 validation_failed 就当场改，不要等下一步才发现。**

2. **search 的两个参数别搞反**：
   `file_pattern` = 文件名 glob（"*.py"），`content_pattern` = 要找的内容。
   写反后可能返回 `mode:"files"`（只列文件）或 `matches` 为空，不会报错，容易被误判成"代码里没有"。
   正确：`{file_pattern:"*.py", content_pattern:"MAX_PDF_BYTES"}`。
   要正则匹配时加 `content_regex:true`；只想要命中计数时加 `count_only:true`。

3. **edit_file 要求 find 唯一**，匹配多处会报错要求你加长 find。
   这比"默认替换第一处"安全，是保护不是限制。

4. **路径规则**：`/data/uploads/x` 是**引擎相对路径**（解析到引擎根目录），
   `C:/xxx` 才是真绝对路径。只拦系统目录黑名单，其他位置放行。

**code_runner 用来做什么**：跑脚本做端到端验证、起子进程、调 HTTP 接口探活、看环境版本。
**不要用 code_runner 手搓正则去搜代码**——那是 file_utils_api.search 的活。

**code_runner 的沙箱契约（照抄这段，别自己发挥）**

沙箱有三条硬约束，违反了直接报错：

1. **全部逻辑必须写在 `async def execute(envelop, agent):` 内部。**
   顶层写任何可执行语句会被 AST 预检拦下：
   `【错误分类】LLM_CODE / 禁止顶层执行代码，全部逻辑写在 execute 函数内`
2. **`import` 必须写在 `execute` 内部**，不能放模块级。
   放外面会在函数体里报 `NameError: name 'xxx' is not defined`（沙箱不注入模块命名空间）。
3. **返回值必须是 `{"data": ...}`**，缺 `data` 键报
   `【一句话】返回值缺少 data 字段`。

正确骨架（照抄）：

```python
async def execute(envelop, agent):
    import json
    import os

    async def post(route, payload):
        return await post_json(f"{base_url}/api/{route}", payload)

    result = await post("applications/x/y", {"action": "list"})
    return {"data": {"ok": True, "result": result}}
```

沙箱自带助手（直接用，别 import aiohttp/urllib 打本机）：

- `await post_json(url, dict)` → POST JSON 并解析响应（**打本机网关用这个**）
- `await fetch_async(url)` → 异步 GET（**打本机页面用这个**）
- 同步 `urllib` / `requests` 打 127.0.0.1 会被拦；打外部站点才可用
- `subprocess.run` 是同步函数，`await` 它会抛 `TypeError` → 用 `await asyncio.to_thread(subprocess.run, ...)`
- `exec()` 被 AST 预检拦；`importlib.util.spec_from_file_location` + `exec_module` 可以用

`env` 里会带环境真相，动手前先看：`mem_available_gb`（可用内存）、`exec_timeout_s`（单次硬超时，默认 60）、`libs_missing`、`net`（pypi/hf-mirror/modelscope 连通性）、`gpu`。

**60 秒是硬超时**：长任务分步跑，别一次串完。**沙箱里能拿到的是独立进程视角**，验主控热重载要看主进程 `sys.modules`，code_runner 看不到。

**沙箱禁用的是会毁系统的东西**（`rm -rf /`、`format`、`mkfs` 等黑名单），不是普通能力；`import requests` / `subprocess` / HTTP 都能用。但它和引擎同进程，`os._exit()` 会直接干掉引擎——只跑你自己写的验证脚本。

**核心认知**
在aicp引擎内 自带部署网关，基础地址：{本机基址}
-目录即路由：
插件文件地址 :plugins/xxxx目录/xxx.py  对应call_plugin receiver参数地址 xxxx目录/xxx
-源码即契约
插件必须存在签名函数 async def execute(envelop, agent) 必须return envelop
envelop.payload 内部实现 ’aciton 、自定义参数、业务逻辑‘
-envelop唯一真相源
envelop.payload 内部结构自己根据业务逻辑设计，按demo实现功能路由和参数传递，插件内状态存储写入 data/{项目或插件}/
-最小应用项目示例（含前后端代码）
项目名： _demo
后端文件目录： plugins/applications/_demo
前端文件目录： www/apps/_demo
项目名和目录名须对齐


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
   code 只有一个函数 async def execute(envelop, agent): 必须第一行,import在其内部，且 return {"data": ...}
   参数`agent` 上挂着的能力：`agent.llm`（`chat` / `chat_json` / `chat_stream` / `chat_tool_stream`）、`agent.system.call(envelop)`（调其他插件）、`agent.config`（配置）、`agent.log`（日志）、`agent.base_url`（对外地址）。需要 LLM 或跨插件调用时直接
   代码若调用 agent.llm：必须先 `if not agent.llm:` 兜底；若工具结果带 llm_notice 或提示「未配置模型」，
   说明引擎 aicp.yaml 没配 models.providers，告知用户先配置模型，不要重试 LLM 调用
   aicp如果是本机地址，可调用本机所有资源完成需求
3. 定时 / 实时推送 / 持久化 / 串行编排 → os/_cron、os/_websocket、os/_db、os/_workflow
4. 参数不确定 → get_contract；要源码 → get_contract(plugin="<receiver>", detail="source")，它返回该插件 execute 的源码片段（detail="tools" 给 Function Calling schema）
5. ⚠ 不要在你本地磁盘上找引擎：引擎和你的工作目录不是同一处，`where /r C:\ save_blocks.py`、`dir C:\aicp-engin`、`ls ~/aicp*` 这类搜索全是白费功夫，会把一轮对话耗光。源码只能通过 get_contract(detail="source") / call_plugin 拿；契约里出现的 `source` / `contract_file` 是服务器侧路径，你打不开，也不要去开。想确认某个能力有没有，用 list_capabilities，不要翻文件系统。

## 造应用、造插件、写前端页面（照抄 _demo，不要自由发挥）
参考_demo项目（已实现所有引擎前后端对齐模式，系统提供的基础设施实现方法，正在运行时，直接照抄机制,仅前端页面只需要读app.yaml，静态页面已网关托管直接写）
验收可用code_runner 直接写代码跑整体校验，省token

### 造应用的推荐节奏：读 demo → 一次写完 → 一次验证
别把一次应用开发拆成几十轮"改一处→读回验→再改"。那是低效的主要来源。正确做法：
_demo主要描述前后端通讯完整功能，无后端服务需求、纯浏览器存储的需求页面，仅需读app.yaml,直接写前端页面。
1. **读 _demo 全部 6 个文件**（见上文清单），拿到后端契约、前端骨架、action 命名、
   payload 结构、上传协议、目录布局、app.yaml 字段。
2. **一次 write_file 写完所有文件**，各文件一个调用，不要挤在一次里（超长会超时）：
   - plugins/applications/<project>/app.yaml         应用注册清单（必写，照抄 _demo/app.yaml）
   - plugins/applications/<project>/<name>.py        后端插件
   - plugins/applications/<project>/<name>.contract.json  action 契约（照抄 _demo/notes.contract.json 的格式）
   - www/apps/<project>/index.html                  前端页面
   - plugins/applications/<project>/<name>_skill.md  契约文档（_demo 有就照抄这个）
   write_file 返回的 validation 就是免费的语法检查，**写完当场看一眼**。
3. **一次 code_runner 跑整体验收**：编译后端 + 起浏览器/调接口跑端到端 + 断言关键结果。
   一次跑完，别拆成"编译一次、接口一次、浏览器一次"三轮。

一次应用通常 3～6 个工具调用就够。超过 10 次说明流程跑偏了，回头看是不是在重复验证。

### 踩过的坑（别重走）
- **上传一律 multipart 直传**，不要把文件转 base64 走网关 JSON：
  网关 MAX_BODY_SIZE 有 10MB 硬限，base64 还涨 33%，浏览器内存占用约 2.3 倍，
  还得自己写分片+合并。标准流程是
  `GET /api/upload_config` 拿动态 url → `XMLHttpRequest`+`FormData` 直传 → `xhr.upload.onprogress` 真实进度。
  **上传地址永远别写死 127.0.0.1**（局域网访问会打到客户端本机），交给 /api/upload_config 按请求 hostname 生成。
- **同站判断只按 host 比对，不看端口**：网关(9000)和上传接收器(9002)是同一站点的兄弟服务。
- **网关 API 的 body 是扁平的** `{action, ...参数}`，不是嵌套的 `{action, payload:{...}}`。
- **应用宣称的文件大小上限必须和上传接收器 MAX_UPLOAD_SIZE(50MB) 对齐**，
  不然大文件能过前端校验却在上传时被服务端拒。

## 交付：直接给可点击链接
回复用户时必须原样给一行 markdown 链接：
  [打开应用]({本机基址}/apps/<project>/)
注意是 `/apps/<project>/`，不是 `/<project>/`。
不要只说“已创建”，也不要让用户自己去拼地址。
其他插件返回 url / open_url / link 字段时，同样原样给用户。

## 静态文件（写文件给用户看时用）
- 公开目录：`www/`（`{本机基址}/xxx`）、`data/uploads/`（`{本机基址}/data/uploads/xxx`）、`data/cache/`（`{本机基址}/data/cache/xxx`）
- 要给用户看文件（图片 / SVG / 音频 / PDF），写到  `data/cache/`，然后给 `{本机基址}/data/cache/...` 链接

## 节奏
一次只做**一个阶段**（读规范 → 写文件 → 验证），拿到结果再决定下一步；
但一个阶段内部该并行/批量就批量，不要拆成几十个来回。
掌握了系统架构和认知用code_runner 可省token，尤其搜索、定位、验证等综合性判断
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
            "builtins/tools/file_utils_api / builtins/tools/code_runner / builtins/tools/save_blocks / "
            "os/_cron / applications/<project>/<name>。"
            "示例1 算数据：{receiver:'builtins/tools/code_runner', params:{code:'async def execute(envelop, agent):\\n    return {\"data\": 1+1}'}}；"
            "示例2 读写文件：{receiver:'builtins/tools/file_utils_api', action:'write_file', params:{path:'/plugins/applications/x/x.py', content:'...'}}；"
            "示例3 造应用：{receiver:'builtins/tools/save_blocks', params:{text:'=== PLUGIN: ... ===\\n...\\n=== END ==='}}。"
            "不知道有什么能力先调 list_capabilities；不知道参数先调 get_contract。"
            "⚠ 写文件/改文件/搜文件优先用 builtins/tools/file_utils_api（写完自带语法校验），别用客户端自带的编辑器。"
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

    elif action == "restart":
        # ★ 优雅重启：释放端口 + 用新代码重新起（改了提示词不必重启引擎）
        ok = await _sr().restart_service_and_watch(
            agent, "os/_mcp_server",
            port=_resolve_mcp_port(agent),
            host=_resolve_mcp_host(agent),
            start_payload={"action": "START",
                           "host": _resolve_mcp_host(agent),
                           "port": _resolve_mcp_port(agent)},
        )
        envelop.payload = {"ok": ok, "status": "restarted" if ok else "restart_failed",
                           "port": _resolve_mcp_port(agent)}
        return envelop
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
    host = envelop.payload.get("host") or _resolve_mcp_host(agent)
    port = envelop.payload.get("port") or _resolve_mcp_port(agent)

    if not hasattr(agent, "_mcp_sessions"):
        agent._mcp_sessions = SessionStore()
    if not hasattr(agent, "_mcp_metrics"):
        agent._mcp_metrics = Metrics()
    store = agent._mcp_sessions
    metrics = agent._mcp_metrics

    app = web.Application(client_max_size=_resolve_max_body_size(agent))

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

    # ---------- 认证（优先走 os/_token_manager，统一 token 体系）----------
    async def _verify_token(token, request=None):
        if not token:
            return None
        meta = {
            "token": token,
            "source": "mcp",
            # ★ 来源信息：供 os/_auth 的 local / hybrid 护栏判断
            "remote": (request.remote if request else "") or "",
            "forwarded_for": (request.headers.get("X-Forwarded-For", "") if request else ""),
        }

        # 1) 首选 token_manager：静态 token / HMAC / 动态 token 一套校验，吊销即时生效
        try:
            tm_env = core.Envelop(
                sender="os/_mcp_server",
                receiver="os/_token_manager",
                payload={"action": "check", "token": token},
                meta=meta,
            )
            result = await agent.system.call(tm_env)
            if result and result.payload.get("ok"):
                return {
                    "user_id": result.payload.get("user_id", "local"),
                    "permissions": result.payload.get("permissions", {"*": True}),
                    "source": result.payload.get("source", ""),
                    "expires_at": result.payload.get("expires_at"),
                }
        except Exception as e:
            await _log("warning", "token_manager_check_error", error=str(e))

        # 2) 回落 os/_auth（兼容本地 token 列表等旧路径）
        auth_env = core.Envelop(
            sender="os/_mcp_server",
            receiver="os/_auth",
            payload={"action": "verify", "token": token},
            meta=meta,
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

        max_body_size = _resolve_max_body_size(agent)
        # 先按 Content-Length 拒一次，好给出结构化诊断；
        # aiohttp 的 client_max_size 兜底只会抛裸 413，agent 看不出下一步该做什么。
        # 注意：body 还没解析，拿不到 JSON-RPC 的 id，按规范用 null。
        content_length = request.content_length
        if content_length and content_length > max_body_size:
            return _rpc_error(None, ERR_INVALID_REQUEST,
                              f"request body too large: {content_length} > {max_body_size} bytes "
                              f"(aicp.yaml max_body_size); shrink the payload or upload files via "
                              f"the file_utils_api tool instead of inlining them")

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
            # ★ 用 _live_instructions() 而非模块全局：热重载后立即生效，不用重启引擎
            instructions = _live_instructions()
            try:
                ws = await _call_internal(session, "os/_mcp_agent", action="summary",
                                          params={"client": client_name})
                if isinstance(ws, dict) and ws.get("ok") and ws.get("text"):
                    instructions = AICP_INSTRUCTIONS + "\n\n---\n\n" + ws["text"]
            except Exception as e:
                await _log("warning", "workspace_summary_failed", error=str(e))

            _base = (getattr(agent, "base_url", "") or "http://127.0.0.1:9000").rstrip("/")
            instructions = instructions.replace("{本机基址}", _base)

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
    agent._mcp_host = host
    agent._mcp_port = port

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

    # ★ 自愈监听（统一走 os/_service_restart）：
    #   本模块被热重载后，aiohttp app 里注册的 handler 闭包仍持有旧模块的
    #   globals（AICP_INSTRUCTIONS 不会更新）。watcher 检测到模块被换掉就自动
    #   重启 MCP server —— 否则「改了提示词却要重启引擎才生效」。
    _sr().ensure_hot_reload_watch(
        agent, "os/_mcp_server", port=port, host=host,
        start_payload={"action": "START", "host": host, "port": port},
    )

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


async def _stop(envelop, agent, keep_watcher: bool = False):
    store = getattr(agent, "_mcp_sessions", None)
    if store:
        for sid in list(store.sessions.keys()):
            await store.remove(sid)

    if not keep_watcher:
        rtask = getattr(agent, "_mcp_reload_task", None)
        if rtask:
            # ★ 只 cancel 不 await：若这次 STOP 是 watcher 自己触发的，
            #   await 自己会死锁（task 等不到自己结束）。cancel 后让它自然收尾。
            try:
                rtask.cancel()
            except Exception:
                pass
            agent._mcp_reload_task = None

        swatch = getattr(agent, "_service_watch_os__mcp_server", None)
        if swatch:
            try:
                swatch.cancel()
            except Exception:
                pass
            agent._service_watch_os__mcp_server = None

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
