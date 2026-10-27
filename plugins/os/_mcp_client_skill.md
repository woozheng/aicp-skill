---
title: os/_mcp_client 外部 MCP 接入
description: 把外部 MCP Server 的工具接进 AICP 体内，与内置插件平权调用
category: os
tags: [mcp, client, tools]
---
# `os/_mcp_client` —— 反向接入外部 MCP Server

把外部 MCP Server 的 tool 接进 AICP 体内，和内置插件平权调用。

## 六个 action

| action | 参数 | 说明 |
|---|---|---|
| `connect` | `server`（名字，必填）、`url`、`token`、`ttl`、`timeout` | 建 session + 拉 tools/list + 写缓存与契约。url 只在首次或换地址时传 |
| `list_servers` | — | 配置 + 连通状态 + 工具数 |
| `list_tools` | `server`（可选，不传=全部汇总） | 返回工具的 name/description/inputSchema |
| `call_tool` | `server`、`tool`、`arguments` | 核心。自动 lazy connect + 过期刷新 + 熔断 + 401 重连一次 |
| `refresh` | `server`（可选） | 强制重拉 tools/list 并重写契约 |
| `disconnect` | `server` | DELETE /mcp 清 session（保留 tools 缓存） |

## 用法

```python
# 1. 接一个外部 server（人提供 url，这是唯一需要人的信息）
use_tool("os/_mcp_client", {"action": "connect",
    "server": "weather", "url": "http://host:9100/mcp", "token": "xxx"})
# → {ok, tool_count, tools: ["get_forecast", ...], session_id}

# 2. 看有什么工具
use_tool("os/_mcp_client", {"action": "list_tools", "server": "weather"})

# 3. 调工具（不需要先 connect，内部自己懒连）
use_tool("os/_mcp_client", {"action": "call_tool",
    "server": "weather", "tool": "get_forecast", "arguments": {"city": "北京"}})
# → {ok, data, server, tool}
```

## 设计要点

- **懒连接**：不是每次对话都连，首次 `call_tool` 才建 session。没人调用就永不连。
- **TTL 刷新**：`ttl` 默认 3600s，过期自动 refresh；refresh 失败但工具存在则降级用旧缓存。
- **熔断**：连续 3 次失败 → 60s 内直接返回 `circuit open`，不拖满超时。
- **会话失效重连**：401/403 或 `-32013` → 清 session、重连、重试 1 次。
- **协议**：JSON-RPC 2.0，`Accept` 必须含 `application/json, text/event-stream`，响应可能是 SSE 流（逐行 `data:` 解析），`Mcp-Session-Id` 从响应头读。

## 数据落盘

```
data/os/_mcp_client/
├── servers.json                       配置（人可手改）
│     {"weather": {"url": "...", "token": "...", "ttl": 3600, "enabled": true}}
├── state.json                         运行时（session_id / fail_count / circuit_open_until）
├── tools/<server>.json                tools/list 缓存
└── contracts/<server>.<tool>.json      单工具契约（inputSchema，L1 直取用）
```

`servers.json` 可以直接手改加 server，进程启动不主动重连，第一次调用才生效。

## 契约复用（P2）

`connect` / `refresh` 时会把每个 tool 的 `inputSchema` 落成 `contracts/<server>.<tool>.json`，
格式与 `contract_agent` 的契约一致，可被 L1 路径直取，不需要 LLM 解析 schema。

## 坑

- 工具名跨 server 可能重名 → 必须同时给 `server`，不能只给 `tool`。
- 返回体超过 32000 字符会截断并标 `truncated: true`，避免塞爆信息流。
- 连自身 `os/_mcp_server` 时 address 是本机，会附 `notes: target is local address`——这是提示不是拒绝。
- `disconnect` 不清 tools 缓存，`list_tools` 仍可查；要彻底清就删 `data/os/_mcp_client/`。
