---
name: save-blocks-app
title: 用 save_blocks 造全栈应用（含路由对齐与端到端验证）
description: 用 save_blocks 一次一块写出「APP(app.yaml) + PLUGIN(后端) + HTML(前端)」的全栈应用，并跑通真实核心路径。覆盖三类必踩的坑：插件路由与项目名不一致、自检探测的接口路径对不上、WebSocket 频道名前后端不一致；以及 save_blocks 自检 + use_tool 直调 + code_runner WS 端到端的三层验证阶梯。
tags: ["save_blocks", "造应用", "全栈", "路由对齐", "WebSocket", "自检", "交付标准"]
metadata:
  version: "1"
---

# 用 save_blocks 造全栈应用

## 一、核心心智模型：路由由路径决定，不由 id 决定

| 产物 | 落盘位置 | 决定的东西 |
|---|---|---|
| `APP` 块 | `plugins/applications/<project>/app.yaml` | 应用元信息、`static_root`（指向前端目录） |
| `PLUGIN` 块 | `plugins/applications/<project>/<file>.py` | **路由 = `applications/<project>/<file 去后缀>`** |
| `HTML` 块 | `www/apps/<project>/index.html` | 前端页面（`app.yaml` 的 `static_root` 必须指向它） |

**铁律 1：插件文件名必须等于项目名。**
`save_blocks` 的自检只探测 `applications/<project>/<project>` 这一个接口。你把插件命名成 `notes.py`，自检就会报：

```
Plugin not found: applications/demo_notes/demo_notes
```

→ 改成 `demo_notes.py`，前端调用路径同步改成 `/api/applications/<project>/<project>`。

**铁律 2：一个项目目录只留一个插件文件。**
改名后旧文件要删掉，否则同一目录两个 receiver，热加载与探测都会漂移。

**铁律 3：`app.yaml` 的 `id` 不是路由。** 路由只看目录路径。

---

## 二、块格式与顺序（一次只传一个块）

```
=== APP: plugins/applications/<project>/app.yaml ===
name: <中文名>
id: <project>
version: 1.0
description: <一句话>
static_root: www/apps/<project>
=== END ===
```

```
=== PLUGIN: plugins/applications/<project>/<project>.py ===
... Python 代码 ...
=== END ===
```

```
=== HTML: www/apps/<project>/index.html ===
... HTML ...
=== END ===
```

推荐顺序：**APP → PLUGIN → HTML**。每块保存后看返回里的 `ok` / `saved` / `rejected` / `checks`，最后一块落盘时 `checks` 里才会出现 `frontend: true` 和 `runtime` 自检结果。

单块写入返回的关键字段：

| 字段 | 用途 |
|---|---|
| `ok` | 是否成功（`false` 时看 `error` / `hint`） |
| `saved` / `rejected` | 落盘清单 / 被拒块及原因 |
| `loaded` / `compiled` | 热加载与编译是否通过 |
| `checks.<project>.runtime` | 首屏状态码、接口探测结果、`problems` |
| `warnings` | 例如「前端页面还没落盘，访问会 404」 |
| `next` | 下一步提示（通常是「把 preview_url 原样作为 markdown 链接交付」） |

---

## 三、WebSocket 实时同步：频道名必须前后端一致

后端推：

```python
env = core.Envelop(
    sender=f"applications/{PROJECT}/{PROJECT}",
    receiver="os/_websocket",
    intent="API_CALL",
    payload={"action": "push", "channel_id": CHANNEL, "data": event},
)
await agent.system.call(env)
```

前端订阅（`ws_config` 由系统提供）：

```js
const ws = new WebSocket(cfg.url + "?channel=" + project + "_notes");
```

**最容易翻车的地方：后端 `CHANNEL` 用了 `f"{PROJECT}_{PROJECT}"`，前端却订阅 `f"{project}_notes"`。**
两边字符串必须**手算核对**，不要想当然。推荐把频道名写成常量后端暴露给前端，或严格统一为 `{project}_{topic}` 形式。

前端渲染要点：消息体带 `_meta`（sender / intent / trace_id / timestamp），业务字段在顶层；`type` 区分 `xxx_created` / `xxx_updated` / `xxx_deleted`。

---

## 四、验证阶梯（三层，缺一不可）

| 层 | 手段 | 覆盖 | 判据 |
|---|---|---|---|
| 1. 静态自检 | `save_blocks` 返回的 `checks` | 编译、首屏 200、首屏接口 ok | `problems: []`、`warnings: []` |
| 2. 直调插件 | `use_tool(target="applications/<p>/<p>", action="list")` | 真实 `execute` 分发、真实落盘 | 返回 `ok: true` 且数据落盘 |
| 3. 端到端 | `code_runner` + `websockets` | WS 双客户端收发、断连容错 | A/B 两端都收到 push |

### 第 2 层用法（`use_tool` 直调）

```
use_tool(target="applications/demo_notes/demo_notes", action="list", plugin_args={})
use_tool(target="applications/demo_notes/demo_notes", action="create", plugin_args={text: "第一条"})
use_tool(target="applications/demo_notes/demo_notes", action="delete", plugin_args={id: "xxx"})
```

插件的 `execute` 里建议同时兼容两种 payload 形态：

```python
raw = envelop.payload if isinstance(envelop.payload, dict) else {}
payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
action = payload.get("action") or "list"
```

因为 `use_tool` 直调与 HTTP `fetch` 网关送进来的 payload 形状可能不同。

### 第 3 层：code_runner 做 WS 端到端

要点（踩过的坑）：

1. **沙箱 AST 预检禁止无 break 的 `while True`** → 改用 `for _ in range(N):` + `asyncio.wait_for` 超时 break。
2. **监听器必须在触发事件前就绪**，且超时给足（≥6s）。用 1.2s 超时会在 create 之前就 break，误判为「没收到消息」。
3. `websockets` 不在预置库里，要 `packages=["websockets"]`。
4. 服务端 WS 地址不在 9000，从 `/api/ws_config` 读：`{"url":"ws://127.0.0.1:9001/ws","port":9001}`。
5. 客户端连接会先收到一条 `{"type":"connected", ...}`，这是正常心跳/握手帧，**不算业务消息**。

验证脚本骨架：

```python
import asyncio, json

async def execute(envelop, agent):
    import websockets
    base = (getattr(agent, "base_url", None) or "http://127.0.0.1:9000").rstrip("/")
    cfg = await fetch_async(base + "/api/ws_config")
    cfg_json = json.loads(cfg["text"]) if isinstance(cfg, dict) and "text" in cfg else cfg
    uri = cfg_json["url"] + "?channel=" + CHANNEL
    msgs_a, msgs_b = [], []

    async def drain(ws, store, tag):
        for _ in range(8):
            try:
                store.append(await asyncio.wait_for(ws.recv(), timeout=6.0))
            except asyncio.TimeoutError:
                break
            except Exception as e:
                out[tag + "_err"] = str(e)
                break

    ws_a = await websockets.connect(uri, open_timeout=10)
    ws_b = await websockets.connect(uri, open_timeout=10)
    ta = asyncio.create_task(drain(ws_a, msgs_a, "a"))
    tb = asyncio.create_task(drain(ws_b, msgs_b, "b"))
    await asyncio.sleep(1.0)

    await post_json(base + API, {"action": "create", "text": "端到端1"})
    await asyncio.sleep(1.5)
    # 关掉 B，再 create，验证 A 仍正常收到
    await ws_b.close()
    await post_json(base + API, {"action": "create", "text": "端到端2"})
    await asyncio.sleep(1.5)

    await ws_a.close()
    await asyncio.gather(ta, tb, return_exceptions=True)
    return {"data": {"a": msgs_a, "b": msgs_b}}
```

注意：`post_json` / `fetch_async` 的返回值是 dict，含 `status` / `text` / `url`，真正的响应体在 `["text"]` 里，要 `json.loads` 一次。

---

## 五、code_runner 沙箱速查

| 禁止 | 替代 |
|---|---|
| `print()` | `return {"data": ...}` |
| `while True`（无 break） | `for _ in range(N)` + `asyncio.wait_for` |
| `time.sleep()` | `await asyncio.sleep()` |
| `subprocess.run` / `os.system` | `await subprocess.run(...)` 或 `run_async(cmd)` |
| 顶层执行代码 | 全部写在 `async def execute(envelop, agent)` 内 |
| 传字典给 packages | `packages` 必须是数组 |

`env` 必看：`gpu`、`exec_timeout_s`（60s）、`disk_free_gb`、`net`（pypi 慢就别下大文件）。

---

## 六、收尾清单

1. 删掉演示 / 测试残留数据（`list` → 逐条 `delete`），别把脏数据留给用户。
2. 删除改名后遗留的重复插件文件。
3. **交付即降级**：应用交付后立刻卸载已钉住的 save_blocks Skill：

```
use_tool(target="builtins/tools/skill_loader", action="unpin", plugin_args={skill_id: "plugin_builtins_tools_save_blocks"})
```

4. 最终回复必须交代：**做了什么 / 局限在哪 / 还能怎么改**，并附 `preview_url` 的 Markdown 链接（原样贴）。

---

## 七、本次实战记录（demo_notes 共享便签墙）

| 坑 | 现象 | 修法 |
|---|---|---|
| 插件名不匹配 | 自检报 `Plugin not found: applications/demo_notes/demo_notes` | `notes.py` → `demo_notes.py` |
| 前端端点错 | 前端仍打 `/notes` | 改为 `/api/applications/<project>/<project>` |
| 重复插件文件 | 改名后目录里两个 receiver | 删掉旧 `notes.py` |
| WS 频道不一致 | 后端 `demo_notes_demo_notes` vs 前端 `demo_notes_notes` | 后端改 `CHANNEL = f"{PROJECT}_notes"` |
| 沙箱 AST 预检 | `while True` 被拦 | 改有限循环 |
| WS 测试假阴性 | 监听器提前超时 break | 超时提到 6s，事件前先 sleep 1s |