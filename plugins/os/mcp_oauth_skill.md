---
title: os/mcp_oauth 通用 OAuth 登录器
description: 标准 OAuth 2.1 授权码+PKCE 登录任意 MCP Server，token 落盘供 _mcp_client 消费
category: os
tags: [mcp, oauth, pkce, dcr]
---
# `os/mcp_oauth` —— 通用 MCP OAuth 登录器

把任意支持 OAuth 2.1 的 MCP Server 接进 AICP 体内。token 落到 `data/os/_mcp_client/servers.json`，由 `os/_mcp_client` 直接消费。

标准实现，不是某个站点的定制脚本：

| 规范 | 用途 |
|---|---|
| RFC 9728 | `/.well-known/oauth-protected-resource` — 从 MCP Server 反查授权服务器 |
| RFC 8414 | `/.well-known/oauth-authorization-server` — 拿 authorization / token / registration 端点 |
| RFC 7591 | 动态客户端注册（DCR） |
| RFC 7636 | PKCE（S256） |
| RFC 8252 | 公共客户端（`token_endpoint_auth_method=none`） |

## 八个 action

| action | 参数 | 说明 |
|---|---|---|
| `discover` | `server`、`issuer`、`mcp_url` | 探测授权元数据。给 `mcp_url` 即可（会先反查 issuer）。不登录、纯只读 |
| `configure` | `server`（必填）、`issuer`、`mcp_url`、`scope`、`client_id`、`client_name`、`redirect_uri` | 手工预登记配置。`discover` 没命中时用它兜底 |
| `start` | `server`（必填）、`issuer`、`mcp_url`、`scope`、`redirect_uri`、`client_id`、`update_url` | 发起授权。必要时先 DCR 注册，返回 `authorize_url`（**用户自己去浏览器打开**） |
| `callback` | `code`、`state`、`server` | 授权回调 → 换 token → 落盘。`server` 可省，按 state 反查 |
| `status` | `server`（可选，不传=全部） | 登录状态、过期时间、是否有 refresh_token |
| `refresh` | `server`（必填） | 用 refresh_token 换新 token |
| `logout` | `server`（必填）、`remove` | 清 token；`remove=true` 彻底删条目 |
| `list` | — | 列出所有已登记 server 及端点 |

## 用法

```python
# 1. 探测（只给人一个 MCP 地址就够）
use_tool("os/mcp_oauth", {"action": "discover",
    "server": "agensi", "mcp_url": "https://mcp.agensi.io/mcp"})
# → {ok, discovery: {issuer, authorization_endpoint, token_endpoint,
#                   registration_endpoint, scopes_supported}}

# 2. 发起授权（自动 DCR + PKCE），把 authorize_url 给用户
use_tool("os/mcp_oauth", {"action": "start",
    "server": "agensi", "mcp_url": "https://mcp.agensi.io/mcp"})
# → {ok, authorize_url, redirect_uri, client_id, scope, scopes_supported}

# 3. 用户浏览器授权 → 回调自动落盘（无需手动调 callback）

# 4. 查状态
use_tool("os/mcp_oauth", {"action": "status", "server": "agensi"})
# → {ok, data: [{server, signed_in, expires_at, expired, has_refresh_token, ...}]}

# 5. 交给 MCP 客户端拉工具
use_tool("os/_mcp_client", {"action": "connect", "server": "agensi"})
```

### 前端页面直接调用

浏览器 GET → 返回 HTML 页面（授权按钮 / 结果页）；POST → 返回 JSON。

```
GET  /api/os/mcp_oauth?action=start&server=agensi   → HTML「前往授权」按钮页
GET  /api/os/mcp_oauth?action=list                   → HTML 状态表
POST /api/os/mcp_oauth  {"action":"status"}           → JSON
```

分流规则在插件 `execute()` 里：`method == "GET"` 或显式传 `format="html"` → HTML；否则（LLM 编排调用、前端 fetch POST）→ JSON。

## 数据落盘

```
data/os/mcp_oauth/
├── servers.json    每个 server 的授权配置（issuer / 端点 / client_id / scope）
└── state.json      进行中的授权流程（state / PKCE verifier），回调成功后删除该条
data/os/_mcp_client/servers.json    最终 token（os/_mcp_client 消费）
```

## 设计要点


- **端点全部来自 discovery**，只在 discovery 失败时才按候选路径探测（`/oauth/authorize`、`/oauth/token`、`/oauth/register` 等），探测规则：只有 404 判定不存在，400/405/302 都算存在。
- **state 校验**：回调时 state 不匹配直接拒绝，防止 CSRF。state 存盘，按 server 索引，也支持按 state 反查（回调时可以不传 server）。
- **PKCE verifier 落盘**：回调时从 `state.json` 取回 verifier 换 token，成功后删除该条。

## 坑

- **`content_type` 不能带 charset**：网关用 `web.Response(body=..., content_type=...)` 渲染，aiohttp 禁止 `text/html; charset=utf-8`（抛 ValueError → 500）。插件里写的是 `"text/html"`。
- **`_html_page` 的 body 可以是 list**：内部会 `join`，传 list 也安全。
- **`redirect_uri` 绑 `agent.base_url`**（默认 `http://127.0.0.1:9000`）：换主机/端口后要重新 `start`，否则 DCR 会因 redirect 不匹配重新注册。
- **DCR 注册后 `client_id` 已存盘**：同一个 server 重复 `start` 不会重复注册，除非 redirect_uri 变了。
- **`os/_mcp_client` 不消费 `refresh_token`**：它只在 `servers.json` 读 `token` 字段，过期后不会自动续期。要续期得显式调 `os/mcp_oauth` 的 `refresh`。
- **授权服务器不提供 `registration_endpoint` 时**：`start` 会报「该授权服务器未提供 DCR 注册端点，请手工传 client_id」，此时用 `configure` 传 `client_id` 再 `start`。
