# call_plugin 配方

MCP 暴露 3 个工具：`call_plugin` / `list_capabilities` / `get_contract`。能力全在 `receiver`，后两个是 `cogitor.search` / `contract_agent.get` 的便捷入口。这份文件是"想做 X，用哪个 receiver、参数长什么样"的速查。

## 一、三个元能力（先查再调）

```json
// 有什么能力
{"receiver": "builtins/tools/cogitor", "action": "search",
 "params": {"terms": ["fetch", "http"], "limit": 10}}

// 这个插件怎么调
{"receiver": "builtins/tools/contract_agent", "action": "get",
 "params": {"plugin": "builtins/tools/save_blocks"}}

// 全貌
{"receiver": "builtins/tools/cogitor", "action": "list_plugins", "params": {}}
{"receiver": "builtins/tools/cogitor", "action": "get_map", "params": {}}
```

`cogitor.search` 的 `results[].name` **就是** receiver，可以直接喂给下一次 `call_plugin`。

## 二、receiver 拼写规则（最容易错的地方）

receiver = `plugins/` 下的目录路径 + 文件名，去掉 `plugins/` 前缀和 `.py` 后缀。

| 文件 | receiver |
|---|---|
| `plugins/builtins/tools/save_blocks.py` | `builtins/tools/save_blocks` |
| `plugins/builtins/agents/main_agent.py` | `builtins/agents/main_agent` |
| `plugins/os/_cron.py` | `os/_cron` |
| `plugins/applications/todo/todo.py` | `applications/todo/todo` |

❌ 常见错法：`save_blocks`（缺目录）、`/api/builtins/tools/save_blocks`（那是前端 URL，不是 receiver）、`os/cron`（缺下划线）。

错的时候不会报"路由不合法"，只会 `plugin not found` 或 `ok:false` —— 所以拼错了很难看出来。

## 三、高频配方

### 读写文件

```json
{"receiver": "builtins/tools/file_utils_api", "action": "read_file",
 "params": {"path": "plugins/os/_mcp_server.py"}}

{"receiver": "builtins/tools/file_utils_api", "action": "edit_file",
 "params": {"path": "www/todo/index.html", "find": "旧文本", "replace": "新文本"}}

{"receiver": "builtins/tools/file_utils_api", "action": "list_dir",
 "params": {"path": "plugins/applications"}}
```

参数是**扁平**的：`action` 和业务参数同级，不要写成 `{"action": "read_file", "params": {"params": {...}}}`。

`edit_file` 的 `find` 必须在文件里唯一且一字不差 —— 先 `read_file` 拿到原文再复制，别凭记忆写。

### 跑 Python

```json
{"receiver": "builtins/tools/code_runner",
 "params": {"code": "async def execute(envelop, agent):\n    return {'data': {'hello': 1}}"}}
```

铁律：

- 必须有 `async def execute(envelop, agent):`，所有 import、变量、try/except 都在函数内
- 必须 `return {"data": ...}`
- 沙箱预置：`fetch(url)`、`run(cmd)`、`run_async(cmd)`、`agent.llm.chat/chat_json`、`agent.data_dir`、`agent.base_url`
- 禁 `print()`、`time.sleep()`、`while True`、`input()`；`run()` 命令里禁 `> < $ ; &`

### 造页面 / 应用

```json
{"receiver": "builtins/tools/save_blocks",
 "params": {"text": "=== APP: plugins/applications/demo/app.yaml ===\nname: demo\nid: demo\n=== END ==="}}
```

一次只传一个块 —— 一次传多个会让 `text` 超长、生成被截断。顺序：`APP` → `PLUGIN` → `HTML`。

### 定时任务 / 实时推送

```json
{"receiver": "os/_cron", "action": "add", "params": {"schedule": "*/5 * * * *", "receiver": "applications/demo/demo", "payload": {"action": "tick"}}}
{"receiver": "os/_websocket", "action": "push", "params": {"channel_id": "demo_dashboard", "data": {}}}
```

具体参数先 `contract_agent.get` 确认，各系统版本有差异。

## 四、读结果

`tools/call` 返回：

```json
{"jsonrpc": "2.0", "id": 4, "result": {
  "content": [{"type": "text", "text": "{...插件 payload 的 JSON...}"}],
  "isError": false}}
```

- 业务结果在 `content[0].text` 里，是**字符串化 JSON**，要再解析一次
- `isError: true` 表示插件执行失败，但 `text` 里通常仍有 `{"ok": false, "error": "..."}`，先读它再判断
- 有的系统插件（`os/_websocket status`、`os/_db list`）正常也不带 `ok:true` —— 别把缺 `ok` 当失败

## 五、连续失败怎么办

两次同样的错误就换路径：

1. `contract_agent.get(plugin)` 看契约
2. 契约空 → `file_utils_api read_file` 读源码，看 `execute` 里到底认哪些 action / 参数名
3. 还不行 → 把原始错误告诉用户，不要继续猜

最常见的三个坑：参数层级套了两层、receiver 缺目录、action 名字大小写（`START` vs `start`——`os/_mcp_server` 就是大写）。
