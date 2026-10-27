---
title: save_blocks 造应用
description: 一次调用写入前端+后端+app.yaml 创建全栈应用，含自检与交付规范
category: tools
tags: [save_blocks, app, fullstack]
---
# `save_blocks` —— 造应用（含自检与交付）

一次调用把「前端 + 后端 + `app.yaml`」写进磁盘，自动热加载。不合规范的块会被拒写（`rejected`），原因写进 `errors` / `issues`。

> 造应用前先 `get_contract(plugin="builtins/tools/save_blocks")` 拿本文，照本文写，别读现成应用猜结构。

---

## 核心规则：全应用只手写一个名字

**只允许手写一个名字：后端插件文件名（去掉 `.py`）。** 其余（项目名、API 前缀、WS 频道）全部由它派生。

```javascript
const project = window.location.pathname.split("/")[2];  // 派生
const API = `/api/applications/${project}`;               // 派生
const PLUGIN = "notes";         // ← 唯一手写名
const CHANNEL = `${project}_${PLUGIN}`;   // 派生
```

后端**零手写名**，全从文件位置派生：

```python
PROJECT = Path(__file__).parent.name   # 派生：项目名
PLUGIN = Path(__file__).stem           # 派生：插件名（= 文件名去 .py）
CHANNEL = f"{PROJECT}_{PLUGIN}"        # 派生：WS 频道名
```

### 对齐表（一行对一行，错了必 404）

| 对象 | 写法 | 必须等于 |
| --- | --- | --- |
| 后端文件 | `plugins/applications/{project}/{PLUGIN}.py` | `{PLUGIN}` |
| 接口路由 | `fetch(\`${API}/${PLUGIN}\`)` | `{PLUGIN}` |
| WS 频道（前端） | `` `${project}_${PLUGIN}` `` | `{project}_{PLUGIN}` |
| WS 频道（后端） | `f"{PROJECT}_{PLUGIN}"` | 同上 |

- **项目名 ≠ 插件名**。一个项目可以有多个后端插件（`notes.py` / `pusher.py`），前端为每个插件各写一个 `const XXX = "xxx"`。
- **路由由前端调什么决定**：`/api/applications/{project}/notes` 要求磁盘上存在 `plugins/applications/{project}/notes.py`。
- **改名 = 改路由**：后端文件改名后必须删掉旧文件，否则网关会多注册一个 receiver。
- WS 频道的 topic = **发起推送的那个后端插件的文件名**（不是被推送数据的插件名）。

---

## 使用顺序

1. 定名：先想清楚插件文件名（如 `notes`），它就是路由末段，也是频道 topic
2. 写块：一次只传一个块（格式见《代码块格式》）
3. 前端只写 `${API}/${PLUGIN}`，不要自己拼路径
4. 读返回 `ok/rejected/errors/warnings/checks[].runtime`
5. 交付前走《交付前必须真跑》+《自检盲区》，按《交付回复模板》标注实测

---

## 代码块格式

```text
=== PLUGIN: plugins/applications/{project}/{name}.py ===
代码
=== END ===

=== HTML: www/apps/{project}/index.html ===
代码
=== END ===

=== APP: plugins/applications/{project}/app.yaml ===
name: 显示名
id: {project}
version: 1.0
description: 描述
static_root: www/apps/{project}
=== END ===
```

块类型 `PLUGIN/HTML/CSS/JS/APP/YAML`，块尾必须单独一行 `=== END ===`。路径只允许 `plugins/applications/` 和 `www/`。

**一次只传一个块**（`APP` → `PLUGIN` → `HTML`），避免 `tool_call` 参数超长。

`{name}` = 该插件的文件名（去 `.py`），它同时是**路由末段**和**WS 频道 topic**。`{name}` 可以与项目名不同（`demo_notes/notes.py` 完全合法）；一个项目可以保存多个 `PLUGIN` 块 = 多个后端插件。

---

## 返回字段

| 字段 | 含义 | 动作 |
| --- | --- | --- |
| `ok` | 无 `error` / 无拒写 | `false` → 不能交付 |
| `rejected` | 被拒写的文件 | 按 `issues[].hint` 修完重存 |
| `errors` | 必读 | 全部修掉 |
| `warnings` | 不阻塞，多为功能瘸 | 逐条判断 |
| `preview_url` | 可访问地址 | 原样写成 Markdown 链接 |
| `checks[project]` | 按磁盘聚合 | 看 `app_yaml/frontend/compile_ok` |
| `checks[project].runtime` | 保存后真跑过 | `problems` 非空 → 别交付 |
| `compiled` | 语法编译 | `false` = 语法错误 |
| `loaded` | 是否注册进 `core.plugins` | `compiled:true + loaded:false` = 热加载时延，等几秒再调 `action` |

### 运行时自检

```json
{
  "runtime": {
    "ran": true,
    "page_status": 200,
    "page_len": 16535,
    "local_len": 16133,
    "endpoints": {"x": {"status": 200, "ok": true}},
    "problems": []
  }
}
```

| 现象 | 级别 | 怎么办 |
| --- | --- | --- |
| `ran:false` | warning | 页面能否打开未验证 |
| `page_status≠200` | error | 查 `app.yaml` 的 `static_root` |
| `page_shorter_than_local` | error | 静态目录指错 / 同名页面抢路由 |
| `empty_page` | error | 200 但内容为空 |
| `online_stale` | warning | 缓存没刷，让用户 `Ctrl+F5` |
| `api_xxx_not_ok` | warning | 没有 `list` action 属正常，否则真错 |
| `api_xxx_status_500` | error | 接口真报错 |

这层拦「页面打不开 / 首屏接口挂」，拦不住「点按钮没反应」。

---

## 自检规则（`error` = 拒写）

### 后端 `.py`

- 没有 `async def execute(envelop, agent):` → `error`
- 签名不是 `(envelop, agent)` → `error`
- 全文没有 `return envelop` → `error`
- `PROJECT = "写死"` → `error`（必须 `Path(__file__).parent.name`）
- 用 `agent.llm` 没 `if not agent.llm` → `error`
- 有 `__init__.py` → `error`
- 没 `try/except` → `warning`
- `len(...)+1` 当 ID → `warning`
- 写了 `envelop.receiver` → `warning`

### 前端 `.html` / `.js`

- 内联 JS 语法错误 → `error`（`node --check` 或括号/注释配对检查）
- 函数定义从未引用 → `warning`（入口被注释 / 漏写调用）
- 硬编码项目名 → `error`（必须 `window.location.pathname.split('/')[2]`）
- 硬编码 `/api/applications/<字面项目名>` → `error`
- 上传字段写 `files` → `error`（必须 `file` 单数）
- 缺 `window.location.pathname.split` → `warning`
- 硬编码 `127.0.0.1/localhost` → `warning`（用 `/api/ws_config`、`/api/upload_config`）
- `gradient(` / `box-shadow` → `warning`
- `data:image/...;base64` → `warning`
- 没有 `<script>` → `warning`

> 硬编码**插件名**是允许的（`const PLUGIN = "notes"`），这是全应用唯一手写名；它对不对由下面的「对齐检查」判。

### 对齐检查（项目级，前端落盘后跑）

| 现象 | 级别 | 修法 |
| --- | --- | --- |
| 前端调用了插件 `x`，磁盘上没有 `{project}/x.py` | `error` | 改前端插件名，或补后端文件 |
| 前端 `${API}/${变量}` 变量解不出字面量 | `warning` | 改成 `const PLUGIN = "字面量"` + `${API}/${PLUGIN}` |
| 后端有 `{project}/x.py` 但前端没引用 | `warning` | 改名后的残留文件 → 删掉，否则网关多注册一个 receiver |
| 前端 WS 频道名在后端找不到对应定义 | `warning` | 两端同规则：前端 `${project}_${PLUGIN}`，后端 `f"{PROJECT}_{PLUGIN}"` |

### `app.yaml`

- `static_root` 与项目路径不一致 → `error`
- 缺 `id` / `static_root` → `warning`

---

## 配对成品（照抄这两份，只改业务逻辑）

### 后端 `plugins/applications/{project}/notes.py`

```python
import json
from pathlib import Path

PROJECT = Path(__file__).parent.name
PLUGIN = Path(__file__).stem
SENDER = f"applications/{PROJECT}/{PLUGIN}"
CHANNEL = f"{PROJECT}_{PLUGIN}"


def _load():
    f = Path("data") / PROJECT / "notes.json"
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError):
        return []


def _save(items):
    f = Path("data") / PROJECT / "notes.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")


async def execute(envelop, agent):
    try:
        raw = envelop.payload if isinstance(envelop.payload, dict) else {}
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
        action = payload.get("action")
        items = _load()

        if action == "list":
            envelop.payload = {"ok": True, "data": items}
            return envelop

        if action == "create":
            text = str(payload.get("text") or "").strip()
            if not text:
                envelop.payload = {"ok": False, "error": "text 不能为空"}
                return envelop
            note = {"id": max([i["id"] for i in items], default=0) + 1, "text": text}
            items.append(note)
            _save(items)
            envelop.payload = {"ok": True, "data": note}
            return envelop

        envelop.payload = {"ok": False, "error": f"Unknown action: {action}"}
        return envelop

    except Exception as e:
        agent.log.error(str(e))
        envelop.payload = {"ok": False, "error": str(e)}
        return envelop
```

### 前端 `www/apps/{project}/index.html`

```html
<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Notes</title>
</head>
<body>
<div id="app"></div>
<script>
const project = window.location.pathname.split("/")[2];
const API = `/api/applications/${project}`;
const PLUGIN = "notes";
const CHANNEL = `${project}_${PLUGIN}`;

async function request(action, payload = {}) {
  const resp = await fetch(`${API}/${PLUGIN}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, ...payload }),
  });
  const text = await resp.text();
  return text ? JSON.parse(text) : {};
}

async function render() {
  const r = await request("list");
  const box = document.getElementById("app");
  box.textContent = "";
  for (const note of (r.data || [])) {
    const el = document.createElement("div");
    el.textContent = note.text;
    box.appendChild(el);
  }
}

render();
</script>
</body>
</html>
```

### 铁律

- ✅ 每个分支 `return envelop`
- ✅ 顶层 `try/except` 兜底
- ✅ 数据落 `data/{PROJECT}/*.json`，`mkdir(parents=True, exist_ok=True)`
- ✅ 读 JSON 捕获 `JSONDecodeError` / `FileNotFoundError`
- ✅ ID 用 `max(id)+1`，不用 `len()+1`
- ❌ 不放 `__init__.py`
- ❌ 本插件自己的返回 `envelop` 不设 `receiver`
- ❌ 前端不自己拼路由路径——路由名必须来自 `const XXX = "字面量"`
- ❌ 多个动作用路径区分——用 body 里的 `action`，路由只有一条

---

## 跨插件调用（`agent.system.call`）

### 报错 → 修法速查

| 报错 | 修法 |
| --- | --- |
| `'str' object has no attribute 'receiver'` | `system.call` 收到字符串，改成 `core.Envelop(...)` |
| `'NoneType' object has no attribute 'dataset'` | `upsert` 返回了 `undefined`，改成返回实体（见《双路径渲染》） |
| `websocket push failed: 'str' object...` | push 参数名错，是 `channel_id` 不是 `channel` |
| `'X' object has no attribute 'Y'` | 同一报错连挂两次，回字面找类型断层，别猜 |

### 固定模板（全文档唯一一处 `core.Envelop` 构造）

```python
from pathlib import Path
import core

PROJECT = Path(__file__).parent.name
PLUGIN = Path(__file__).stem
SENDER = f"applications/{PROJECT}/{PLUGIN}"
CHANNEL = f"{PROJECT}_{PLUGIN}"

env = core.Envelop(
    sender=SENDER,
    receiver="os/_websocket",  # 完整路径，不是插件名
    intent="API_CALL",
    payload={
        "action": "push",
        "channel_id": CHANNEL,  # ← 是 channel_id，不是 channel
        "data": {"type": "x"},
    },
)
await agent.system.call(env)
```

`core.Envelop` 签名：

```python
Envelop(sender, receiver, intent, payload, channel_id, ttl, meta, status)
```

**不知道参数先查契约，别猜**：

```python
await agent.system.call(core.Envelop(
    sender=SENDER,
    receiver="builtins/agents/contract_agent",
    intent="API_CALL",
    payload={"action": "get", "plugin": "os/_websocket"},
))
```

### 铁律

- ✅ `receiver` 写完整路径
- ✅ `sender` 动态拼
- ✅ `Envelop` 必须真构造，不能 `dict` 冒充
- ✅ push 包 `try/except`，找不到接收方只 `warning`
- ❌ 不绕过 `system.call` 直接摸 `agent.ws_gateway`

---

## 后端插件里调 LLM（`agent.llm`）

`agent.llm` 是已经配好 API Key 的 LLM 客户端。**直接调，不要 `import openai`，不要读 `OPENAI_API_KEY` / `LLM_API_KEY` 环境变量，不要自己 `requests.post`。**

### 前提：先判空

```python
if not agent.llm:
    envelop.payload = {"ok": False, "error": "LLM 未配置（去 Console → 切换模型 配置 models.providers）"}
    return envelop
```

**`agent.llm` 没配时 `if not agent.llm` 是 `error` 级**（save_blocks 自检会拦）。

### 三种调用方式

**文本（返回 `str`）**：

```python
result = await agent.llm.chat([
    {"role": "system", "content": "你是翻译助手，只输出译文，不要解释。"},
    {"role": "user", "content": f"把下面的文本翻译成{target_lang}：\n{text}"},
])
# result 是 str
```

**JSON（返回 `dict`）**：

```python
data = await agent.llm.chat_json([
    {"role": "user", "content": f"翻译并返回 JSON：{{\"translated\": \"...\"}}\n原文：{text}"},
])
# data 是 dict，直接用 data["translated"]
```

**流式（逐 chunk 返回 `str`）**：

```python
async for chunk in agent.llm.chat_stream([
    {"role": "user", "content": f"翻译：{text}"},
]):
    # chunk 是增量文本，自己拼
    pass
```

### 指定角色 / 多模态 / 异常处理

- `agent.llm.chat(messages, role="code")` —— 用 `models.roles.code` 配的模型；不传 `role` 用 `models.roles.default`
- 多模态：`content` 用数组 `[{"type": "text", ...}, {"type": "image_url", "image_url": {"url": "..."}}]`
- `chat` / `chat_json` / `chat_stream` 都可能抛 `RuntimeError`（未配置）、`asyncio.TimeoutError`（超时）、`Exception`（网络/内容安全），**外层 `try/except` 兜底**

### 铁律

- ✅ 调之前 `if not agent.llm` 判空
- ✅ `await`，`agent.llm.chat` 是协程
- ✅ 外层 `try/except` 兜底
- ✅ 长文本分段（一次送整段超 `max_tokens` 会被截断）
- ❌ 不 `import openai` / 不读 env / 不自己 `requests.post`
- ❌ 不在同步函数里调（`agent.llm.chat` 是 `async`）

---
## SSE 流式返回（比 WS 更简单，推荐用于「一问一答」式流式）

**适用场景**：用户发一个请求，后端流式返回结果（如流式翻译、流式总结）。**不需要前端主动连 WS，前端用 `fetch` + `ReadableStream` 读 SSE 即可。**

### 后端：`envelop.payload` 设为异步生成器

```python
async def _stream(text, target_lang, agent):
    messages = [{"role": "user", "content": f"翻译成{target_lang}：\n{text}"}]
    async for chunk in agent.llm.chat_stream(messages):
        if chunk:
            yield f'data: {json.dumps({"type": "delta", "text": chunk}, ensure_ascii=False)}\n\n'
    yield f'data: {json.dumps({"type": "done"}, ensure_ascii=False)}\n\n'


async def execute(envelop, agent):
    ...
    if action == "translate_stream":
        if not getattr(agent, "llm", None):
            envelop.payload = {"ok": False, "error": "LLM 未配置"}
            return envelop
        envelop.payload = _stream(text, target_lang, agent)   # ← 异步生成器
        return envelop
```

**网关检测到 `envelop.payload` 是异步生成器**，自动返回 `Content-Type: text/event-stream`，逐 chunk `resp.write`。**插件不用管 HTTP 头，不用 `web.StreamResponse`。**

### 前端：`fetch` + `ReadableStream` 读 SSE

```javascript
async function translateStream(text, sourceLang, targetLang) {
  const resp = await fetch(`${API}/${PLUGIN}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "translate_stream", text, source_lang: sourceLang, target_lang: targetLang }),
  });
  if (!resp.ok) throw new Error("HTTP " + resp.status);

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // 按 \n\n 切事件
    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const evt = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const line = evt.split("\n").find(l => l.startsWith("data: "));
      if (!line) continue;
      const msg = JSON.parse(line.slice(6));
      if (msg.type === "delta") appendDelta(msg.text);
      else if (msg.type === "done") onDone();
      else if (msg.type === "error") onError(msg.error);
    }
  }
}
```

**SSE 解析规则**：按 `\n\n` 切事件，每行 `data: ` 后面是 JSON。**`fetch` 的 `reader.read()` 保证是真实字节流（不是一次性返回后切片）。**

### 铁律

- ✅ 后端 `envelop.payload = async_gen()`，**异步生成器直接赋给 payload**，网关自动 SSE
- ✅ 每段 `yield f'data: {json.dumps(...)}\n\n'`，**结尾两个 `\n`**
- ✅ 前端 `fetch` + `resp.body.getReader()`，**`TextDecoder({stream: true})`**
- ✅ 按 `\n\n` 切事件，每行 `data: ` 后面 JSON
- ❌ 不要自己 `web.StreamResponse`（网关处理）
- ❌ 不要用 `EventSource`（它是 GET，POST 用不了；用 `fetch` + `ReadableStream`）
- ❌ 不要 `resp.text()`（会等整个流结束，失去流式）

## WS 推流到前端（流式输出）

后端插件用 `os/_websocket` 的 `push` 推消息，前端用 `WebSocket` 收。**流式输出：后端每产生一段增量，`push` 一次；前端 `onmessage` 逐段渲染。**

### 后端：推一条增量

`core.Envelop` 构造见《跨插件调用 / 固定模板》，只改 `data`：

```python
await agent.system.call(core.Envelop(
    sender=SENDER,
    receiver="os/_websocket",
    intent="API_CALL",
    payload={
        "action": "push",
        "channel_id": CHANNEL,               # = f"{PROJECT}_{PLUGIN}"
        "data": {
            "type": "delta",                  # 前端按 type 分支
            "text": "增量文本片段",
            "seq": 1,                         # 可选：序号
        },
    },
))
```

**`data` 里写什么由你定**，但建议统一带 `type`（前端分支）、`text`（增量文本）、`seq`（可选）。**不要**在 `data` 里塞 `_meta` / `trace_id` / `timestamp`——网关加。

### 网关自动加的 `_meta`（后端不用管）

`os/_websocket.push` 会在 `data` 外面套一层 `_meta` 再发给前端：

```json
{
  "type": "delta", "text": "增量文本片段", "seq": 1,
  "_meta": {"sender": "applications/translator/translator", "intent": "API_CALL", "trace_id": "tr_ab12cd34", "timestamp": 1730000000.123}
}
```

**前端读 `msg.type` / `msg.text` / `msg.seq` 即可，`_meta` 可忽略。**

### 前端：连 WS 并处理增量

```javascript
const CHANNEL = `${project}_${PLUGIN}`;
let ws = null;

async function connectWS(onDelta) {
  const cfg = await fetch("/api/ws_config").then(r => r.json());
  // cfg = { url: "ws://host/ws", port: 9001, secure: false }
  ws = new WebSocket(`${cfg.url}?channel=${encodeURIComponent(CHANNEL)}`);

  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === "connected") return;   // 握手
    if (msg.type === "pong") return;        // 心跳
    if (msg.type === "delta") onDelta(msg.text || "", msg);
  };

  ws.onclose = () => setTimeout(() => connectWS(onDelta), 3000);   // 指数退避重连
}
```

**`/api/ws_config` 返回 `{url, port, secure}`**：`url` 已经含 `ws://host/ws`（或 `wss://`），**直接 `${url}?channel=` 拼接，不要自己拼端口**；`secure` 不用二次判断。

### 流式翻译最小示例

**后端 `translator.py`**（片段）：

```python
async def _translate_stream(text, source_lang, target_lang, agent, push):
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"源语言：{source_lang}\n目标语言：{target_lang}\n\n{text}"},
    ]
    full = ""
    seq = 0
    async for chunk in agent.llm.chat_stream(messages):
        if not chunk:
            continue
        full += chunk
        seq += 1
        await push(chunk, seq)
    return full


async def execute(envelop, agent):
    ...
    if action == "translate_stream":
        text = str(payload.get("text") or "").strip()
        if not text:
            envelop.payload = {"ok": False, "error": "text 不能为空"}
            return envelop
        if not getattr(agent, "llm", None):
            envelop.payload = {"ok": False, "error": "LLM 未配置"}
            return envelop

        await _push(CHANNEL, {"type": "start"}, agent)
        async def push_delta(chunk, seq):
            await _push(CHANNEL, {"type": "delta", "text": chunk, "seq": seq}, agent)

        try:
            full = await _translate_stream(text, source_lang, target_lang, agent, push_delta)
        except Exception as e:
            await _push(CHANNEL, {"type": "error", "error": str(e)[:200]}, agent)
            envelop.payload = {"ok": False, "error": str(e)[:200]}
            return envelop

        record = {..., "translated": full, ...}
        items.append(record)
        _save(items)
        await _push(CHANNEL, {"type": "done", "record_id": record["id"]}, agent)
        envelop.payload = {"ok": True, "data": {"record": record}}
        return envelop


async def _push(channel, data, agent):
    """推一条到 WS 频道，失败只记日志不抛。"""
    try:
        import core
        await agent.system.call(core.Envelop(
            sender=SENDER, receiver="os/_websocket", intent="API_CALL",
            payload={"action": "push", "channel_id": channel, "data": data},
        ))
    except Exception as e:
        logger = getattr(agent, "log", None)
        if logger: logger.warning(f"[{PLUGIN}] push failed: {e}")
```

**前端 `index.html`**（片段）：

```javascript
async function doTranslateStream() {
  const text = $("srcText").value.trim();
  if (!text) { setMsg("请输入要翻译的文本", "err"); return; }
  await request("translate_stream", {   // 普通 POST 触发，增量走 WS 回来
    text,
    source_lang: $("srcLang").value,
    target_lang: $("tgtLang").value,
  });
}

connectWS((text, msg) => {
  if (msg.type === "start") { $("outText").textContent = ""; setMsg("翻译中…"); }
  else if (msg.type === "done") { setMsg("翻译完成，已记入历史", "ok"); loadHistory(); }
  else if (msg.type === "error") { setMsg(msg.error || "翻译失败", "err"); }
  else { $("outText").textContent += text; }
});
```

### 铁律

- ✅ 后端 `push` 的 `data` 带 `type`，前端按 `type` 分支
- ✅ 增量 `type: "delta"`，完成 `type: "done"`，错误 `type: "error"`
- ✅ `push` 包 `try/except`，失败只 `warning`，不影响主流程
- ✅ 前端 `onmessage` 先过滤 `connected` / `pong`，再处理业务
- ✅ 前端 `ws.onclose` 加指数退避重连
- ❌ 后端 `data` 里不加 `_meta` / `trace_id` / `timestamp`（网关加）
- ❌ 前端不自己拼端口（用 `cfg.url`）
- ❌ 不要在流式过程中 `await` 长时间阻塞的操作（比如每次 `push` 后 `sleep`），会拖慢整个流

---

## `agent.llm.chat_stream` 产出语义

`agent.llm.chat_stream(messages, ...)` 返回 `AsyncIterator[str]`，**每个 `chunk` 是增量文本片段（`str`），不是 `delta` 对象、不是 OpenAI 的 `chunk.choices[0].delta`**。

```python
async for chunk in agent.llm.chat_stream([...]):
    # chunk 是 str，直接拼或直接推
    full += chunk
    await push_delta(chunk)
```

### 关键语义

| 项 | 事实 |
| --- | --- |
| 产出类型 | `AsyncIterator[str]`，`chunk` 是 `str` |
| 是否增量 | **是**，每段是增量文本，不是整段 |
| 是否需要 `await` | **是**，`async for` 本身就是 `await` |
| 模型不支持 streaming 时 | **退化**：一次 `yield` 整段（`chunk` 是完整结果） |
| `<think>` 开头的内容 | **被剥掉**，`chunk` 不含 `thinking`（内部处理） |
| `session_id` 参数 | **可选**，不传也行；传了用于 LLM 统计归位 |
| `role` 参数 | 可选，`role="code"` 用 `models.roles.code` 配的模型 |
| 空 chunk | 可能 `yield ""` 或 `None`，**拼之前判空** |
| 异常 | 可能抛 `RuntimeError`（未配置）、`asyncio.TimeoutError`（超时）、`Exception`（网络/内容安全），**外层 `try/except` 兜底** |

### 和 `chat` 的区别

| | `chat` | `chat_stream` |
| --- | --- | --- |
| 返回 | `str`（整段） | `AsyncIterator[str]`（逐段） |
| 用法 | `result = await agent.llm.chat(...)` | `async for chunk in agent.llm.chat_stream(...)` |
| 适用 | 短文本、不需要流式 | 长文本、需要逐字显示 |
| 模型不支持流式 | 不涉及 | 退化成一整段 |

### 铁律

- ✅ `async for chunk in agent.llm.chat_stream(...)`，`chunk` 是 `str`
- ✅ 拼之前判 `if not chunk: continue`
- ✅ 外层 `try/except` 兜底
- ✅ 长文本用 `chat_stream`，短文本用 `chat`
- ❌ 不要 `chunk.choices[0].delta`（那是 OpenAI 原始对象，`chat_stream` 已经剥成 `str`）
- ❌ 不要 `await chunk`（`chunk` 是 `str`，不是协程）
- ❌ 不要 `for chunk in ...`（是 `async for`，不是同步 `for`）

---

## 双路径渲染（WS / 乐观更新）

### 哪些算双路径

- POST 返回 + WS 广播
- 乐观更新
- 轮询 + 手动操作
- 拖拽/编辑本地先动

### Bug 原因

两条路径用同一 ID 渲染，先到建节点，后到 `if (exists) return;` 返回 `undefined`，调用方 `notes[id].dataset.saved` 抛 `TypeError`。表现：接口 `ok`、磁盘有数据、页面不更新。

```javascript
const notes = Object.create(null); // ✅ 避开原型链

function upsertNote(note) {
  // ✅ 建节点 + 标状态 + 挂 DOM 一步做完
  let el = notes[note.id];
  if (el) return el; // ✅ 已有返回现有节点，不是静默 return

  el = document.createElement("div");
  el.textContent = note.text;
  el.dataset.saved = note.text; // ✅ 状态在这里写
  el.dataset.id = note.id;
  notes[note.id] = el;
  board.appendChild(el);
  return el;
}

// 两条路径写法一样
const el = upsertNote(r.data.note);
if (el) el.dataset.saved = r.data.note.text;
```

### 铁律

- ✅ `upsert` 返回实体，绝不 `return` 空
- ✅ 建 DOM + 写 `dataset` + 挂容器不可分割，禁止外部补属性
- ✅ 调用方用返回值，不写 `notes[id].xxx`
- ❌ 不在渲染成功前清空输入框

---

## 前端模板要点

```javascript
const project = window.location.pathname.split("/")[2];
const API = `/api/applications/${project}`;
const PLUGIN = "notes";   // 唯一手写名

async function request(action, payload = {}) {
  const resp = await fetch(`${API}/${PLUGIN}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, ...payload }),
  });
  const text = await resp.text();
  return text ? JSON.parse(text) : {};
}
```

**WS**：见《WS 推流到前端 / 前端：连 WS 并处理增量》。
**上传**：必须用 `XMLHttpRequest`（`fetch` 无进度事件），见《文件上传与下载》。

---

## 文件上传与下载

### 事实基线

| 项 | 事实 |
| --- | --- |
| 上传地址 | `GET /api/upload_config` → `{url, port, secure}` |
| 落盘位置 | `data/uploads/<8位随机目录>/<安全文件名>`（有子目录） |
| 响应字段 | `{success, ok, file_path, file_name, file_size, session_id, url}` |
| `url` | `/data/uploads/<dir>/<name>`，网关静态路由可直接 GET |
| 字段名 | 必须 `file`（单数），写 `files` 收不到 |
| 上传用 XHR | `fetch` 无进度事件 |

```javascript
async function uploadFile(file, onProgress) {
  const cfg = await fetch("/api/upload_config").then((r) => r.json());
  const form = new FormData();
  form.append("file", file); // 单数

  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", cfg.url);
    xhr.upload.onprogress = (e) =>
      e.lengthComputable && onProgress(e.loaded, e.total);
    xhr.onload = () =>
      xhr.status >= 200 && xhr.status < 300
        ? resolve(JSON.parse(xhr.responseText))
        : reject(new Error("HTTP " + xhr.status));
    xhr.onerror = () => reject(new Error("网络错误"));
    xhr.send(form);
  });
}
```

`e.loaded/e.total` 是真实字节，不要用 `setInterval` 伪造。大文件用 `xhr.upload.onloadend` 区分「传完 → 服务端落盘」。

### 上传后自己存清单

`_file_receiver.get_file` 是 `os.path.join(upload_dir, file_name)`，文件在子目录，只传 `basename` 会 `not found`。所以存 `{file_path, file_name, file_size, url, uploaded_at}` 到 `data/{PROJECT}/files.json`。

- 展示用返回的 `url`，不要 `base64`
- 下载原文件用自己存的 `file_path`（绝对路径）
- 用户原始文件名必须自己存：服务端会过滤 `+`、`#`、`😀` 等（`a+b#c.pdf` → `abc.pdf`），返回的 `file_name` 只是安全名
- 同名文件会覆盖，要保留多版本请自己改名或存 `file_path` 全路径

### 下载：校验在插件，文件流交给网关

一旦「分享要验证码」，裸 `url` 就是绕过通道。正确做法：

```python
if action == "download":
    if not _verify(payload.get("token"), payload.get("code")):
        envelop.payload = {"ok": False, "error": "验证码不正确"}
        return envelop
    target = Path(share["file_path"])
    if not target.exists():
        envelop.payload = {"ok": False, "error": "文件已删除"}
        return envelop
    envelop.payload = {"ok": True}
    envelop.meta = {
        "response_type": "file",
        "file_path": str(target),
        "content_type": "application/octet-stream",
        "content_disposition": "attachment",
        "file_name": target.name,
    }
    return envelop
```

前端必须走 GET：

```javascript
const q = new URLSearchParams({ action: "download", token, code });
window.location.href = `${API}/fileshare?${q}`;
```

### 铁律

- ✅ GET 才能直出文件流：POST 分支只返回 JSON，用 POST 下载会拿到 JSON
- ✅ 校验失败不设 `meta.response_type=file`，直接返回 `{ok:false}`
- ✅ 验证码用 `secrets/random`，别用 `hash(时间)`
- ✅ 过期/计次在服务端判断和落盘
- ❌ 带校验的文件不放 `www/apps/` 目录
- ❌ 不在分享页直接写 `<a href="/data/uploads/...">`

---

## 交付

`ok: true` 后必须带 Markdown 链接：`[打开应用](preview_url)`。`ok: false` 不给链接，先说清 `errors` + 怎么修。

### 交付前必须真跑

`lint` 过了 ≠ 功能可用。曾有应用 lint 全绿、API 也通，但入口被未闭合注释吞掉，页面永远「加载中」。

**真跑核心路径**：打开页面 → 首屏数据 → 主操作 → 结果落盘/可见。

- 只造后端测试数据不算测页面
- 上传类必须真传一次文件再下载/分享
- 确认入口执行：`init()` / `loadXxx()` / `render()` 不在注释里
- `warnings` 无「函数定义从未引用」
- `errors` 非空不交付
- HTTP 200 不算通过：页面 200 + API `ok` 仍可能空白（前端抛错、元素没挂载、字段不一致）
- `TypeError` / `AttributeError` 按《报错 → 修法速查》反查，同一报错连挂两次说明假设错了
- 静态自检拦「写错的代码」，真跑拦「跑不通的页面」，两者都必须过

### 交付回复模板

```markdown
[打开应用](preview_url)

核心路径实测：
- [x] 打开 → 首屏数据出来（实测）
- [x] 贴一张 → 页面出现（实测）
- [x] A 贴 → B 1 秒内出现（实测/未实测）
- [x] A 删 → B 消失（实测/未实测）
- [x] 关 B 后 A 继续操作不崩（实测/未实测）

未实测项写原因（沙箱连不上内网 / 没起双窗口）

日志 warning/error 原文
```

### 不允许

- 只写「已创建」不带链接
- 用 `ok:true` 代替功能验证
- 未实测不标注

---

## 自检盲区

`save_blocks` 抓不到以下三类，必须自己测：

| 盲区 | 表现 | 怎么测 |
| --- | --- | --- |
| 双路径竞态 | 接口 `ok`、磁盘有数据、页面不更新 | 两窗口硬刷新 → WS 是 101 → A 贴 B，1 秒内出现 → A 删，B 消失 → 关 B 后 A 继续操作不崩 |
| 跨插件参数名错 | 主流程正常，推送静默失败，日志 `websocket push failed` | `POST os/_websocket {"action":"status"}` 看频道连接数 + 日志无 `push failed` |
| WS 频道名/连接数不对 | 单窗口正常，多窗口不通 | 同上，`channels.<name>.connections ≥ 2` |

交付回复涉及这几项时，逐条标注「已实测/未实测」，不允许拿 `ok: true` 代替。

---

## 踩过的坑

- **路由名 = 后端文件名**：前端 `${API}/notes` 要求磁盘有 `notes.py`；项目名无关，一个项目可以有多份后端插件
- **改名后残留旧文件**：网关按新名找不到、旧名仍注册成第二个 receiver → 前端调新名 404。前端引用的名字磁盘上没有会报 `error`
- **WS 频道前后端规则不一致**：后端 `f"{PROJECT}_{PROJECT}"` vs 前端 `${project}_dashboard` → 单窗口正常、多窗口不通。两端必须同规则：`${project}_{PLUGIN}` / `f"{PROJECT}_{PLUGIN}"`
- `checks` 按磁盘聚合：只存后端时 `frontend:false` 正常，继续存 HTML
- `preview_url` 无块落盘时为 `null`
- 多块同 `text` 时某块 `error` 只拒该块，其它照写
- 保存后内部 `sleep(3)`；`loaded:false` 先看 `compiled`：`false` = 语法错误必改；`true + false` = 热加载时延，等几秒再调 `action`
- `app.yaml` 缺了不 404，但应用列表不显示
