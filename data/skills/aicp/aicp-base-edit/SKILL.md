---
title: 改 AICP 长驻服务前必看的 6 件事
description: 改 os 长驻服务（网关/MCP/WS/上传）必读（用户说「改了不生效」「hot reloaded 但行为没变」「MCP 断线了」时用），覆盖热重载失效、死锁、端口占用、引擎起不来
category: aicp
tags: [os-plugin, hot-reload, aiohttp, daemon, port-conflict, deadlock]
triggers: 改了不生效, 热重载, 不生效, 长驻服务, 端口占用, 端口占死, 死锁, 卡住不返回, MCP 断线, 引擎起不来, 加载顺序, 孤儿 runner, hot reload, reload, daemon, service, port in use, port conflict, deadlock, stuck
not_triggers: 新建应用, 业务应用, 前端页面, 单行修改, 简单替换, 一次性脚本
examples: "改了 _gateway.py 但日志说热重载了行为还是老的", "调 restart 之后卡住不返回 MCP 也断了", "restart 报 address already in use 但 runner 是 None", "hot reloaded but the behavior did not change"
version: 2.0
updated_at: 2026-11-05
---

# 改 AICP 长驻服务前必看的 6 件事

## 何时用

- 要改 `plugins/os/_gateway.py` / `_mcp_server.py` / `_websocket.py` / `_file_receiver.py` 中的任意一个（用户说「改了不生效」「hot reloaded 但行为没变」时）
- 要新增一个长驻服务（aiohttp 常驻 app）（用户说「新增一个常驻服务怎么挂上去」时）
- 遇到长驻服务相关故障：端口占死、restart 卡死、MCP 断线、引擎启动即退出（用户说「MCP 断线了」「restart 卡住不返回」「address already in use」时）
- 需要判断改动层级与代价（用户说「改 core 会被热重载吗」时）

**不适用**：新建应用；业务应用改动；前端页面修改；单行修改；简单替换；一次性脚本。

**先说结论：造应用很容易，改这四件很难。** 它们全部是 aiohttp 常驻 app，有大量隐式约定，违反了**不报错、只静默失效**。

## 前置条件

- 需要编辑 `plugins/os/_*.py` 或 `runtime/_loader.py` 的写权限（`plugins/os` 属于系统插件，改动前应先备份）
- 需要 `builtins/tools/code_runner` 做探测与烟雾测试
- 需要本机可执行 `netstat -ano`（Windows）或等价端口查询命令
- 需要能读取 `data/gateway.log` 排障
- 需要 LLM 时先兜底：`if not agent.llm: return {"data": {"error": "LLM 未配置"}}`
- 改动前先问自己：**改坏了，我还能连上吗？** 外部 agent（Cline / Claude Code / Cursor）的唯一通道是 MCP server（9100），`code_runner` 也是通过 MCP 调的，不是第二条通道。所以改长驻服务时要提前告诉用户「可能要你手动重启」，或者先从网关（9000）留一条 `curl` 通道。

## 核心步骤

### 一、加载顺序依赖 — 引擎起不来

**现象**：改完代码重启引擎，进程直接退出，没有任何 Python 报错栈到你的终端。

**根因**：`runtime/_loader.py` 用 `sorted(root.rglob("*.py"))` 按**文件名字母序**加载。

实测顺序（plugins/os 下）：

    _auth → _cron → _db → _file_receiver → _gateway → _llm_* →
    _mcp_agent → _mcp_client → _mcp_server → _model_picker → _pool →
    _registry → _secrets → _service_restart → _static → _token_manager →
    _websocket → _workflow

所以 `_file_receiver` / `_gateway` / `_mcp_server` / `_websocket` 都**排在 `_service_restart` 之前**。

如果你在它们里面写模块级：

    _service_restart = importlib.import_module("plugin_os__service_restart")

→ 执行到这一行时目标模块还没加载 → `ModuleNotFoundError` → 该插件加载失败。

**规则**：

> **绝不在长驻服务里做模块级 import 其他 os 插件。**

正确写法是延迟到函数里：

    def _sr():
        import importlib
        return importlib.import_module("plugin_os__service_restart")

    # 用的时候：_sr().restart_service(...)

### 二、路由闭包持有旧 globals — 改了不生效

**现象**：日志明明白白写着 `Hot reloaded: os/_gateway`，但行为一点没变。改常量、改路由逻辑，全都没效果。

**根因**：`START` 里创建的 aiohttp handler 是**闭包**，它的 `__globals__` 指向**启动那一刻的模块对象**。热重载会 `exec` 出新模块对象并重绑 `core.plugins`，但**已在跑的 handler 仍然指向旧 globals**。

**判断方法**（在 code_runner 里）：

    runner = getattr(agent, "<service>_runner", None)
    app = runner.app
    for route in app.router.routes():
        h = route.handler
        g = h.__globals__
        print(g is vars(sys.modules["plugin_os__gateway"]))   # False = 已经陈旧

**两种修法，选一或都用**：

1. **改用 live 读取**（适合常量/配置类）
   ```python
   _LIVE_MODULE_NAME = "plugin_os__gateway"

   def _live(name, default=None):
       mod = sys.modules.get(_LIVE_MODULE_NAME)
       if mod is not None and hasattr(mod, name):
           return getattr(mod, name)
       return globals().get(name, default)

   # 使用点：is_public = file_path in _live("PUBLIC_PAGES", PUBLIC_PAGES)
   ```
   注意：模块名是 `plugin_` + route 把 `/` 换成 `_`，即 `plugin_os__gateway`（**双下划线**）。

2. **热重载后重建 server**（适合路由结构变化）
   加 watcher：每 2 秒比对 `sys.modules[模块名]` 的 `id()`，变了就 STOP + START。
   现成实现：`plugins/os/_service_restart.py` 的 `ensure_hot_reload_watch()`。

**额外坑**：原本定义在 `START` 闭包里的局部变量（如 `HIDDEN_APP_PREFIXES`）**连模块全局都不是**，`_live()` 也救不了。必须**先提到模块级**。

### 三、STOP 无超时 — 自死锁

**现象**：调 `restart` 后永久卡住，端口消失，日志无异常，MCP 直接断线。

**根因**：

    请求进来 → handler → restart action → STOP → runner.cleanup()
                                                        ↑
                                          等所有在途请求结束
                                                        ↑
                                     那个在途请求就是发起 restart 的这个

**规则**：

> **任何 STOP / cleanup 都必须加超时，超时后走端口兜底。**

    try:
        await asyncio.wait_for(
            fn(core.Envelop(..., payload={"action": "STOP"}), agent),
            timeout=5.0,
        )
    except asyncio.TimeoutError:
        pass          # 不等了，下面强杀端口

    # 兜底：关掉占着该端口的 socket
    for srv, sock in find_port_servers(port):
        sock.close()

现成实现：`_service_restart.restart_service(..., stop_timeout=5.0)`。

**另一层死锁**：watcher 自己触发 restart → `restart_service` 里 cancel watcher 并 `await` 它 = **await 自己**。
凡是 cancel task，**只 cancel 不 await**；要 await 就先判断 `task is asyncio.current_task()`。

### 四、双模块名 — 两份代码

**现象**：改了文件，但 `_sr().xxx` 还是旧行为；或 `hasattr` 明明应该有却是 False。

**根因**：同一个文件可能有**两个模块对象**：

| 模块名 | 谁建的 | 是否跟随热重载 |
|---|---|---|
| `plugins.os._service_restart` | `from plugins.os import x` 触发的常规 import | ❌ 不跟 |
| `plugin_os__service_restart` | `runtime/_loader.py` 加载的 | ✅ 跟随 |

**规则**：

> **os 插件之间互相调用，一律用 loader 的命名：`plugin_` + route.replace("/", "_")。**

验证：

    m1 = sys.modules.get("plugins.os._x")
    m2 = sys.modules.get("plugin_os__x")
    print(m1 is m2)     # 期望 False — 说明确实是两个对象

### 五、孤儿 runner 不进 gc — 端口占死

**现象**：重启报 `OSError: [Errno 10048] address already in use`，但 `agent._xxx_runner` 是 None。

**根因**：老代码没把 runner 存到 agent 上。已启动的 `AppRunner` **不在 `gc.get_objects()` 的 AppRunner 列表里**（aiohttp 内部持有），也找不到任何 Python 引用。

**找它的唯一办法**：从 `Server._sockets` 反查。

    for obj in gc.get_objects():
        if type(obj).__name__ != "Server":
            continue
        for s in (getattr(obj, "_sockets", None) or []):
            if s.getsockname()[1] == port:
                s.close()      # 唯一能释放端口的办法

注意：该版本 aiohttp 的 `Server` **没有** `pre_shutdown()` / `shutdown()` 方法，只能关 socket。

**预防**：`START` 里第一件事就是 `agent._xxx_runner = runner`，并记 `agent._xxx_port` / `_xxx_host`。

### 六、重启后 watcher 要重新挂上自己

**现象**：热重载自愈能用一次，第二次就不灵了。

**根因**：`restart_service` 会 `cancel` watcher 并把 `agent._service_watch_<route>` 置 None；如果 watcher 直接 `return`，就再也没人重启它。

**规则**：watcher 触发完重启后，**必须重新 arm 自己**：

    if not getattr(agent, watch_attr, None):
        ensure_hot_reload_watch(agent, route, port=port, host=host,
                                start_payload=..., watch_attr=watch_attr)

同时 `ensure_hot_reload_watch` 要能顶掉**已结束**的旧 task：

    existing = getattr(agent, watch_attr, None)
    if existing is not None:
        if not existing.done():
            return False          # 真在跑的才算重复
        setattr(agent, watch_attr, None)   # done 的清掉重起

### 测试超时误判的正确姿势（别把测试超时当成功能失效）

这是本次最大的误判来源。

**route 有 30 秒超时。** 如果你在 `code_runner` 里写一个 20~30 秒的轮询循环，它会被掐断，然后你看到的是**服务正处于重启窗口**的瞬时状态（端口刚关、还没重开），很容易误判成「自愈失效」。

**正确做法**：

1. **测试控制在 15 秒内**。改一行 → 等 6 秒 → 探一次 → 复原 → 等 6 秒 → 探一次。
2. **状态异常时先看日志**，不要先改代码：
   ```powershell
   Get-Content data\gateway.log -Tail 30
   ```
   看有没有 `[service] 检测到 xxx 已热重载` / `自动重启 成功` / `Traceback`。
3. 重启需要 2~5 秒，**探测失败要重试**，不要一次判死。

## 判断规则

| 条件 | 动作 |
|---|---|
| 改 `plugins/applications/**` / `plugins/builtins/**` | 直接改，热重载立即生效，风险低 |
| 改 `plugins/os/_*.py`（长驻服务） | 先备份；确认模块级无 `import_module`；改完跑烟雾测试 |
| 改 `core/` `runtime/` | 必须重启引擎，代价最高，先想清楚要不要改 |
| 日志有 `Hot reloaded` 但行为没变 | 查 handler `__globals__` 是否指向旧模块对象 → 用 `_live()` 或重建 server |
| 引擎启动即退出、无 Python 报错 | 查长驻服务里是否有模块级 `import_module` → 改为函数内延迟 import |
| restart 永久卡住、端口消失 | STOP/cleanup 无超时导致自死锁 → 加 `asyncio.wait_for` 超时 + 端口兜底 |
| 报 `address already in use` 且 runner 为 None | 孤儿 AppRunner → 从 `Server._sockets` 反查并 `close()` |
| watcher 自愈只生效一次 | watcher 未重新 arm → 触发后重新 `ensure_hot_reload_watch` |
| 探测失败一次 | 视为重启窗口瞬时状态，等 2~5 秒重试，不要一次判死 |
| 任一烟雾测试项不过 | 考虑回滚，不要继续调 |

## 踩过的坑

| 坑 | 现象 | 根因 | 修法 |
|---|---|---|---|
| 模块级 import 其他 os 插件 | 引擎启动即退出，无报错栈 | loader 按字母序加载，目标模块尚未加载 | 延迟到函数里 `importlib.import_module` |
| 闭包持有旧 globals | 日志说热重载了但行为没变 | handler `__globals__` 指向启动时的模块对象 | `_live()` 读 live 模块，或 watcher 重建 server |
| 闭包局部变量 | `_live()` 也读不到新值 | 局部变量连模块全局都不是 | 先提到模块级 |
| STOP 无超时 | restart 永久卡住，MCP 断线 | cleanup 等待在途请求，而发起方就是在途请求 | `asyncio.wait_for` + 关端口兜底 |
| cancel 后 await 自己 | watcher 触发时二次死锁 | cancel watcher 又 await 它 | 只 cancel 不 await，或判 `is asyncio.current_task()` |
| 双模块名 | 改了文件行为却没变 | `plugins.os.x` 与 `plugin_os__x` 是两个对象 | 一律用 `plugin_` + route.replace("/","_") |
| 孤儿 runner | 端口占死但 runner 是 None | AppRunner 被 aiohttp 持有，不在 gc 可达对象里 | 从 `Server._sockets` 反查 `close()` |
| watcher 不重新 arm | 自愈只生效一次 | `restart_service` 置空 watcher 属性后 watcher 直接 return | 触发后重新 `ensure_hot_reload_watch` |
| 测试超时误判 | 误判「自愈失效」 | 轮询循环超过 route 30 秒超时 | 测试控制在 15 秒内，失败先看日志再重试 |
| 端口名混淆 | 探测错端口 | 网关 9000 / MCP 9100 / 其他 9001、9002 | 探测前先核对端口分配 |

## 验证

改完必跑烟雾测试。写在 code_runner 里，15 秒内跑完：

```python
async def execute(envelop, agent):
    import subprocess, sys, urllib.request

    # 1) 插件都加载了
    import core
    for r in ("os/_gateway", "os/_mcp_server", "os/_websocket", "os/_file_receiver"):
        assert r in core.plugins, f"{r} 未加载"

    # 2) 四个端口都在听
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                         encoding="utf-8", errors="ignore").stdout
    for p in (9000, 9001, 9002, 9100):
        assert any(f":{p}" in l and "LISTENING" in l for l in out.splitlines()), f"{p} 未监听"

    # 3) watcher 都活着
    for r in ("os/_gateway", "os/_mcp_server", "os/_websocket", "os/_file_receiver"):
        w = getattr(agent, "_service_watch_" + r.replace("/", "_"), None)
        assert w is not None and not w.done(), f"{r} watcher 死了"

    # 4) 核心 API 能返回
    req = urllib.request.Request("http://127.0.0.1:9000/api/applications/snippet/snippet",
                                 data=b'{"action":"list"}',
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=8) as r:
        assert r.status == 200

    return {"data": "smoke ok"}
```

断言清单：

1. `os/_gateway`、`os/_mcp_server`、`os/_websocket`、`os/_file_receiver` 都在 `core.plugins` 中
2. 端口 9000、9001、9002、9100 全部处于 LISTENING
3. 四个 `_service_watch_*` watcher 存在且未 `done()`
4. 网关核心 API 返回 200

**任何一项不过，就该考虑回滚而不是继续调。**

救火对照表：

| 现象 | 第一步 |
|---|---|
| 引擎起不来 | 检查四个服务里有没有模块级 `import_module`；看 `data/gateway.log` 末尾 |
| 某端口不监听 | `netstat -ano \| findstr :9000` 看有没有 LISTENING；没有说明 START 失败 |
| 端口占死 10048 | 按「孤儿 runner」一节从 `Server._sockets` 关 socket |
| MCP 断线 | 用网关 `curl -X POST http://127.0.0.1:9000/api/os/_mcp_server -d '{"action":"START","port":9100}'` 拉起来；不行就重启引擎 |
| 改了不生效 | 判断 handler 的 `__globals__` 是否陈旧 |
| 卡住不返回 | 就是自死锁，加超时 |

## 参考

- 相关 skill：`skill/aicp/plugin-action-smoke-test`（插件动作冒烟测试）
- 相关 skill：`skill/aicp/file-edit`（常规文件编辑规范）
- 相关 skill：`skill/aicp/skill-lint`（SKILL 规范校验）
- 相关插件：`builtins/tools/code_runner`（探测与烟雾测试执行）
- 相关插件：`builtins/tools/file_utils_api`（读写与备份插件文件）
- 相关文件：`plugins/os/_service_restart.py`（`restart_service` 与 `ensure_hot_reload_watch` 实现）
- 相关文件：`plugins/os/_gateway.py`、`plugins/os/_mcp_server.py`、`plugins/os/_websocket.py`、`plugins/os/_file_receiver.py`
- 相关文件：`runtime/_loader.py`（按字母序加载插件的加载器）
