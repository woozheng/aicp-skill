---
title: 排查并放宽插件 route 超时
description: 排查并放宽插件 route 超时（用户说「contract_agent 超时只有30秒」「plugin times out after 30s」时用），定位三层超时链
category: aicp
tags: [timeout, plugin, route, agent, contract]
triggers: 超时, 30秒, 路由超时, 调用超时, 契约提取, 超时太短, 插件超时, 30s, timeout, route timeout, plugin timeout, contract timeout, deadline, call timeout
not_triggers: code_runner, think剥离, 前端页面, 首token延迟, 插件路由
examples: "contract_agent 超时只有30秒要改成至少120秒", "调用插件总是报 route timeout after 30s", "the plugin call times out after 30 seconds raise the limit", "mcp 调 contract_agent 提取契约老是超时"
version: 1.0
updated_at: 2026-11-06
---

# 排查并放宽插件 route 超时

## 何时用

- 某个插件调用稳定卡在固定秒数上超时（用户说「contract_agent 超时只有30秒」时）
- 报错文案是 `Plugin timeout after Ns` 或 `route timeout after Ns`（用户说「插件调用报 route timeout」时）
- 内层自己配了更长预算，但外层先把请求掐断（用户说「明明配了120秒还是30秒就断」时）
- 要给 MCP 侧调用的插件单独放宽时限（用户说「mcp 调 contract_agent 老超时」时）

**不适用**：code_runner 的执行超时（用 `plugin_args.timeout_s`）；think剥离与流式重试；前端页面样式；首token延迟埋点；插件路由命名与注册。

## 前置条件

- 目标文件：`plugins/builtins/agents/main_agent.py`（核心引擎，改动需授权 + 先备份）、`plugins/os/_mcp_server.py`、`runtime/_system.py`（后者需重启引擎）
- 工具：`builtins/tools/file_utils_api`（search / edit_file）、`builtins/tools/code_runner`
- 需要 LLM 时先兜底：`if not agent.llm: return {"data": {"error": "LLM 未配置"}}`
- 改 `plugins/builtins/agents/main_agent.py` 前先确认用户已授权

## 核心步骤

1. **先认报错文案**，两种文案来自两层，别混：

| 文案 | 抛出位置 | 含义 |
|---|---|---|
| `Plugin timeout after {timeout}s` | `core/__init__.py` 的 `route()` | 路由层超时，timeout 是调用方给的 |
| `route timeout after {timeout}s` | `runtime/_system.py` 的 `SystemServer.call()` | 插件内部 `agent.system.call` 未传 timeout 时的 30s 兜底 |

2. **按三层链逐层查 timeout**（外层必须 ≥ 内层 + 余量）：

| 层 | 文件 | 默认 | 说明 |
|---|---|---|---|
| 路由层 | `core/__init__.py` `route()` | 600.0 | 上限，通常不是瓶颈 |
| 调用层 | `runtime/_system.py` `call()` | 30.0 | `timeout=None` 时 `call_timeout = 30.0` |
| 派发层 | `plugins/builtins/agents/main_agent.py` 的 `use_tool` | 30 | 按 target 分支算 `route_timeout` |
| MCP 层 | `plugins/os/_mcp_server.py` 的 `TOOL_TIMEOUT` | 30 | `contract_agent: 30`、`cogitor: 15`、`default: 60` |
| 插件内层 | 被调插件自己的 `asyncio.wait_for` | 各自 | 例：`contract_agent.py` 内部 LLM 提取 `timeout=120.0` |

3. **改法**：
   - `use_tool` 的 `route_timeout` 加一条 target 分支（`elif target == "builtins/tools/contract_agent": route_timeout = 180`），不要动 `else` 的 30，避免全局变慢；
   - `TOOL_TIMEOUT` 映射把目标插件的值直接调大；
   - 插件内部调 `agent.system.call(env, timeout=180)` 显式传参，别吃 30s 兜底。
   - `main_agent.py` 是核心引擎：先备份 → 工作区改 → `py_compile` 校验 → 写回触发热重载。

4. **验证**：见「验证」段。

## 判断规则

| 条件 | 动作 |
|---|---|
| 报错是 `Plugin timeout after 30s` | 查 `main_agent.py` 的 `route_timeout` 分支 |
| 报错是 `route timeout after 30s` | 查调用方有没有显式传 `timeout=` 给 `agent.system.call` |
| 走 MCP 调用的插件 | 查 `_mcp_server.py` 的 `TOOL_TIMEOUT` |
| 目标插件内部有更长 `wait_for` | 外层必须比它大（内层 120 → 外层至少 150） |
| 只想放宽一个插件 | 加 target 专属分支，不要改 `else` 的默认 30 |
| 改的是 `runtime/**` | 不走热重载，必须重启引擎，交付时说明 |

## 踩过的坑

| 坑 | 现象 | 根因 | 修法 |
|---|---|---|---|
| 只改了一处 | 还是 30s 超时 | 三层链各自有默认值 | 三层全查：派发层 / MCP 层 / 插件内层 |
| 改了 `else` 默认值 | 全部插件变慢 | `route_timeout = 30` 是所有普通插件的默认 | 加 target 专属分支 |
| 内层 120 外层 30 | 内层刚超时外层先断，看不到真实错误 | 外层 < 内层 | 外层 = 内层 + 30~60 余量 |
| 插件内部 `agent.system.call` 不传 timeout | 插件间调用卡在 30s | `SystemServer.call` 的 `timeout=None` 兜底 30.0 | 显式传 `timeout=` |
| 直接 edit_file 改 `main_agent.py` | 语法错带崩引擎 | 大文件嵌套缩进易错 | 备份 → 工作区改 → `py_compile` → 写回 |
| 以为改 `plugins/**` 也要重启 | 白等 | 只有 `runtime/**` 和 `core/**` 需重启 | `plugins/**` 2 秒热重载 |

## 验证

用 code_runner 一次跑完所有断言，返回 `{name: bool}` + `all_ok`：

```python
async def execute(envelop, agent):
    import re, time
    from core import Envelop

    res = {}

    # 1) 三层都改到位
    ma = open('plugins/builtins/agents/main_agent.py', encoding='utf-8').read()
    res['main_agent_branch'] = 'builtins/tools/contract_agent' in ma
    res['main_agent_default_30_kept'] = 'route_timeout = 30' in ma

    mcp = open('plugins/os/_mcp_server.py', encoding='utf-8').read()
    res['mcp_timeout_120'] = '"builtins/tools/contract_agent": 120,' in mcp

    # 2) 内层预算 <= 外层
    ca = open('plugins/builtins/tools/contract_agent.py', encoding='utf-8').read()
    inner = float((re.findall(r'timeout=([\d.]+),\s*#\s*契约提取', ca) or [0])[0])
    res['outer_ge_inner'] = 120 >= inner

    # 3) 端到端实测一次（走 LLM 提取路径）
    t0 = time.monotonic()
    r = await agent.system.call(Envelop(
        sender='builtins/tools/code_runner',
        receiver='builtins/tools/contract_agent',
        payload={'action': 'get', 'plugin': 'builtins/tools/add_experience'},
    ), timeout=180)
    res['elapsed_s'] = round(time.monotonic() - t0, 1)
    pl = getattr(r, 'payload', None) or {}
    res['ok'] = bool(pl.get('ok'))
    res['source'] = pl.get('source')

    res['all_ok'] = all(v for k, v in res.items() if isinstance(v, bool))
    return {"data": res}
```

断言要点：`outer_ge_inner` 才是真正的「至少 120 秒」，只改外层不够。

## 参考

- skill/aicp/aicp-base-edit —— 长驻服务改动的前置约定
- skill/aicp/llm-runtime-stream-fix —— 改 `runtime/_llm.py` 后的重启与重载
- 相关文件：`plugins/builtins/agents/main_agent.py`、`plugins/os/_mcp_server.py`、`runtime/_system.py`、`core/__init__.py`