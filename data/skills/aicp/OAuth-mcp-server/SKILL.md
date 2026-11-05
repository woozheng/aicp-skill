---
title: 通用 MCP OAuth 登录器
description: 用 OAuth 2.1 授权码+PKCE 登录任意 MCP Server（用户说「这个 MCP 要登录」「connect this mcp server with oauth」时用），token 落盘供 _mcp_client 消费
category: aicp
tags: [mcp, oauth, pkce, token, auth]
triggers: MCP登录, MCP OAuth, 授权登录, 怎么登录MCP, 接入MCP, MCP连不上, 令牌过期, 授权码, PKCE, 动态注册, DCR, 刷新token, oauth, mcp login, authorize, pkce, token refresh, dynamic registration, sign in mcp
not_triggers: 静态API密钥, apikey配置, 纯本地工具, 前端页面美化, 前端生成
examples: "这个 MCP 服务要登录才能用 帮我接一下 OAuth", "mcp.agensi.io 要怎么授权登录", "MCP 连上去一直提示未认证 token 是不是过期了", "connect this mcp server with oauth please"
version: 1.1
updated_at: 2026-11-05
---
# 通用 MCP OAuth 登录器

把任意支持 OAuth 2.1 的 MCP Server 接进 AICP 体内。token 落到 `data/os/_mcp_client/servers.json`，由 `os/_mcp_client` 直接消费。

标准实现，不是某个站点的定制脚本：

| 规范 | 用途 |
|---|---|
| RFC 9728 | `/.well-known/oauth-protected-resource` — 从 MCP Server 反查授权服务器 |
| RFC 8414 | `/.well-known/oauth-authorization-server` — 拿 authorization / token / registration 端点 |
| RFC 7591 | 动态客户端注册（DCR） |
| RFC 7636 | PKCE（S256） |
| RFC 8252 | 公共客户端（`token_endpoint_auth_method=none`） |

## 何时用

- 需要给一个 MCP Server 做 OAuth 授权登录（用户说「这个 MCP 要登录」时）
- 只有 MCP 地址、不知道授权服务器是谁（用户说「mcp.agensi.io 要怎么授权登录」时）
- MCP 报未认证 / token 过期需要重新登录或续期（用户说「MCP 连上去一直提示未认证」时）

**不适用**：静态API密钥；apikey配置；纯本地工具；前端页面美化；前端生成。

## 前置条件

- 插件：`os/mcp_oauth`（提供 8 个 action），配套 `os/_mcp_client` 消费 token。
- 需要 MCP Server 暴露 OAuth 2.1 元数据端点（RFC 9728 / RFC 8414）。
- `redirect_uri` 依赖 `agent.base_url`（默认 `http://127.0.0.1:9000`），需保持与本机基址一致。
- 用户需在浏览器中完成授权（插件无法代替用户点击）。

## 核心步骤

### 八个 action

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

### 用法

```python
# 1. 探测（只给人一个 MCP 地址就够）
use_tool(target="os/mcp_oauth", action="discover",
    plugin_args={"server": "agensi", "mcp_url": "https://mcp.agensi.io/mcp"})
# → {ok, discovery: {issuer, authorization_endpoint, token_endpoint,
#                   registration_endpoint, scopes_supported}}

# 2. 发起授权（自动 DCR + PKCE），把 authorize_url 给用户
use_tool(target="os/mcp_oauth", action="start",
    plugin_args={"server": "agensi", "mcp_url": "https://mcp.agensi.io/mcp"})
# → {ok, authorize_url, redirect_uri, client_id, scope, scopes_supported}

# 3. 用户浏览器授权 → 回调自动落盘（无需手动调 callback）

# 4. 查状态
use_tool(target="os/mcp_oauth", action="status", plugin_args={"server": "agensi"})
# → {ok, data: [{server, signed_in, expires_at, expired, has_refresh_token, ...}]}

# 5. 交给 MCP 客户端拉工具
use_tool(target="os/_mcp_client", action="connect", plugin_args={"server": "agensi"})
```

### 前端页面直接调用

浏览器 GET → 返回 HTML 页面（授权按钮 / 结果页）；POST → 返回 JSON。

```
GET  /api/os/mcp_oauth?action=start&server=agensi   → HTML「前往授权」按钮页
GET  /api/os/mcp_oauth?action=list                   → HTML 状态表
POST /api/os/mcp_oauth  {"action":"status"}           → JSON
```

分流规则在插件 `execute()` 里：`method == "GET"` 或显式传 `format="html"` → HTML；否则（LLM 编排调用、前端 fetch POST）→ JSON。

### 数据落盘

```
data/os/mcp_oauth/
├── servers.json    每个 server 的授权配置（issuer / 端点 / client_id / scope）
└── state.json      进行中的授权流程（state / PKCE verifier），回调成功后删除该条
data/os/_mcp_client/servers.json    最终 token（os/_mcp_client 消费）
```

### 设计要点

- **端点全部来自 discovery**，只在 discovery 失败时才按候选路径探测（`/oauth/authorize`、`/oauth/token`、`/oauth/register` 等），探测规则：只有 404 判定不存在，400/405/302 都算存在。
- **state 校验**：回调时 state 不匹配直接拒绝，防止 CSRF。state 存盘，按 server 索引，也支持按 state 反查（回调时可以不传 server）。
- **PKCE verifier 落盘**：回调时从 `state.json` 取回 verifier 换 token，成功后删除该条。

## 判断规则

| 条件 | 动作 |
|---|---|
| 只知道 MCP 地址 | 先 `discover`，拿 issuer 与端点 |
| `discover` 命中且有 `registration_endpoint` | 直接 `start`，自动 DCR + PKCE |
| `discover` 命中但无 `registration_endpoint` | 先 `configure` 传 `client_id`，再 `start` |
| `discover` 未命中 | `configure` 手工指定 issuer / 端点，再 `start` |
| `status` 显示 `signed_in=false` | 重新 `start` 走授权流程 |
| `status` 显示 `expired=true` 且有 `has_refresh_token` | 调 `refresh` 续期，不用重新授权 |
| `status` 显示过期且无 refresh_token | 重新 `start` 让用户再授权一次 |
| 换主机或端口后 | 重新 `start`，让 DCR 用新 `redirect_uri` 注册 |

## 踩过的坑

| 坑 | 现象 | 根因 | 修法 |
|---|---|---|---|
| `content_type` 带 charset | 500 ValueError | aiohttp 禁止 `text/html; charset=utf-8` | 写 `"text/html"` |
| `_html_page` body 传 list | 内部 `join` 出错 | body 期望字符串 | 传 list 也安全（内部会 join） |
| `redirect_uri` 与实际主机不符 | DCR 重新注册 | `redirect_uri` 绑 `agent.base_url` | 换端口后重新 `start` |
| 重复 `start` 重复注册 | client_id 变化 | DCR 注册后 `client_id` 已存盘 | 同一 server 复用已存 client_id，仅 redirect_uri 变了才重注册 |
| token 过期后 MCP 客户端不续期 | 调用报 401 | `os/_mcp_client` 不消费 `refresh_token` | 显式调 `os/mcp_oauth` 的 `refresh` |
| 授权服务器无 DCR 端点 | `start` 报错 | 服务端未提供 `registration_endpoint` | 用 `configure` 传 `client_id` 再 `start` |

## 验证

用 `code_runner` 调用插件，确认链路打通：

```python
async def execute(envelop, agent):
    from core import Envelop

    r = await agent.system.call(
        Envelop(
            receiver="os/mcp_oauth",
            payload={"action": "status", "server": "agensi"},
        )
    )
    payload = getattr(r, "payload", None) or {}
    status = getattr(r, "status", "") or ""

    return {"data": {"ok": not status, "status": status, "payload": payload}}
```

断言清单：

1. `status` action 返回 `ok`，无异常状态（`getattr(r, "status", "")` 为空）。
2. `payload` 中能取到 `data` 列表，且每项含 `server` / `signed_in` / `expires_at` 字段。
3. `discover` action 对已登录 server 返回 `discovery.issuer` 与 `discovery.token_endpoint`。
4. `list` action 返回的条目数与 `servers.json` 条目数一致。
5. 授权成功后 `data/os/_mcp_client/servers.json` 中该 server 的 `token` 非空。

## 参考

- 相关插件：`os/mcp_oauth`、`os/_mcp_client`
- 规范文档：`data/skills/_STANDARD.md`
- 相关文件：`plugins/os/mcp_oauth.py`、`plugins/os/_mcp_client.py`
- 规范文档：`data/skills/_STANDARD.md`