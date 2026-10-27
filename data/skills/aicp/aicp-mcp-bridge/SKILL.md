---
name: aicp-mcp
description: Connect this machine's AICP agent service (Python; MCP over HTTP at /mcp) to Claude Code, Claude Desktop, Cline, Cursor, VS Code Copilot or Codex, then work through its 3 meta tools (call_plugin / list_capabilities / get_contract). 一条命令自举：`python3 scripts/aicp_mcp.py bootstrap --client auto --yes`. 当用户说"把 AICP 接到 Claude/Cline 里"、"注册 MCP"、"让 agent 能调 AICP 插件"，或会话里看不到 aicp 工具、call_plugin 调用失败时使用。
metadata:
  version: "1"
  requires: python3
---

# AICP MCP

AICP 是本机的 Python 智能体服务：HTTP 网关默认 `:9000`，MCP 端点默认 `http://127.0.0.1:9100/mcp`。

**本 skill 只提供指令。** 它不启动服务、不写客户端配置。真正的动作由 `scripts/aicp_mcp.py` 执行，且必须先给用户看计划并得到批准。

## 端点事实（照抄，不要自己改写协议）

| 项 | 值 |
|---|---|
| 传输 | MCP Streamable HTTP（JSON-RPC over POST） |
| URL | `http://127.0.0.1:9100/mcp`（端口由 AICP 侧决定） |
| 协议版本 | `2025-11-25` |
| 工具数量 | **3 个元工具**：`call_plugin` / `list_capabilities` / `get_contract` |
| 握手 | `initialize` 响应头返回 `Mcp-Session-Id`，后续请求带上它 |
| 其它路由 | `GET /health` `/ready` `/metrics`；`DELETE /mcp` 关会话 |
| 鉴权 | 服务端走 `os/_auth`；本机默认匿名权限 `{"*": true}`，不需要 token |
| 请求头 | `Accept` 必须包含 `application/json` 或 `text/event-stream` |

`call_plugin` 入参：

```json
{"receiver": "builtins/tools/cogitor", "action": "search", "params": {"terms": ["fetch"]}}
```

`receiver` 就是插件路由，等于 `plugins/` 下的目录路径去掉 `.py`：`builtins/tools/save_blocks`、`os/_cron`、`applications/todo/todo`。**它不是插件名，也不是 `/api/` 后面的 HTTP 路径。**

## 第一步：判断该接通还是直接干活

- 会话里**已经有** `call_plugin`（或 `aicp` 前缀的工具）→ 直接读《干活》，**不要重复安装**。
- **没有** → 走《接通》，并且先确认服务本身在跑（见分诊表）。

## 一条命令（agent 自举，推荐）

```bash
python3 scripts/aicp_mcp.py bootstrap --client auto --yes
```

bootstrap = 自探测已装客户端（Cline / Cursor / Claude Code / Desktop / VS Code / Codex）→ 探活（不通就 `ensure` 拉起 MCP 端点）→ 事务写 MCP 配置（备份 + 原子替换 + 读回校验）→ 给 `.clinerules` 落一份 15 行瘦入口。`--root <项目目录>` 指定落点；不带 `--yes` 只打印计划。

之后用户只需两步：重启窗口（Ctrl+Shift+P → Reload Window）→ 问 agent「AICP 能干什么」。

`initialize` 响应的 `instructions` 字段已内含操作手册（路由规则 / 常用 receiver / 安全边界），**因此不需要预先复制 skill 文件**；本 SKILL.md 只在需要细节时查。

下面 6 步是同一件事的手工路径，用于逐步审批或排查场景。

## 接通：诊断 → 计划 → 审批 → 应用 → 重启 → 验证

顺序不能跳。dry-run 和 apply 必须用**同一份 scope**（同一个 `--client`）。

### 1. 诊断（只读，不改任何东西）

```bash
python3 scripts/aicp_mcp.py doctor --json
```

返回 `checks[] = {name, ok, detail, remediation}`。按 `references/connection-and-recovery.md` 的分诊表读结论。**不要在这一步改配置、装软件、sudo。**

### 2. 生成计划（仍然只读）

```bash
python3 scripts/aicp_mcp.py plan --client claude_code --json
```

它只打印：确切的配置文件路径、是否新建、备份路径、要写入的条目、skill 目标目录。把这段**原样**给用户看，不要概括成"我会改一下配置"。

支持的 `--client`：`claude_code` `claude_desktop` `cline` `cursor` `vscode` `codex` `generic`。细节见 `references/client-matrix.md`。

### 3. 审批

- 用户没明确说"装/改"之前，**不要 apply**。
- 计划里出现"已存在同名但不同 URL 的 aicp 条目"时，先问：覆盖、还是换名字（`--name aicp2`）。
- 需要写 skill 文件时，那是**额外一步**，单独列出来一起批（`plan` 会同时列出）。

### 4. 应用（和计划同一范围）

```bash
python3 scripts/aicp_mcp.py apply --client claude_code --yes
```

内部顺序固定：`读配置 → 校验 → 备份(.bak.<时间戳>) → 临时文件 → 原子替换 → 读回校验`。读回失败会**自动回滚**并报出备份路径。保留无关条目和注释以外的所有内容不变。

### 5. 重启 + 验证

客户端重生配置需要重启；Claude Desktop 必须**完全退出**（关窗口不算，运行中的 app 会用内存里的配置覆盖你的写入）。然后：

```bash
python3 scripts/aicp_mcp.py verify --json
```

`verify` 会真跑 `initialize → tools/list → tools/call`（用 `cogitor search` 验证端到端）。**只有它通过，才可以说"接好了"。**

### 6. 回到用户的原任务，用 `call_plugin` 干活。

## 干活：拿到 call_plugin 之后

`call_plugin` 不是万能函数——它要求你知道 `receiver`。MCP 暴露 3 个工具，后两个是前一个的便捷入口（内部翻译成 call_plugin），**先查再调**：

| 想知道什么 | 怎么问 |
|---|---|
| 系统里有什么能力 | `list_capabilities({query: "定时 推送 网页"})`；不带 query 列全图 |
| 插件怎么调、参数是什么 | `get_contract({plugin: "builtins/tools/save_blocks"})`；`detail: "tools"` 要 FC schema，`detail: "source"` 要 execute 源码 |
| 底层等价写法 | `call_plugin({receiver: "builtins/tools/cogitor", action: "search", params: {terms: ["关键词"], limit: 10}})` / `contract_agent.get` |
| 全貌 | `list_capabilities({category: "os"})` 或 `cogitor.list_plugins` |

常用 receiver 速查：

| 意图 | receiver |
|---|---|
| 读写文件、读源码 | `builtins/tools/file_utils_api`（`read_file` / `write_file` / `edit_file` / `apply_patch` / `list_dir`） |
| 跑 Python / 爬网页 / 算数据 | `builtins/tools/code_runner`（`params: {"code": "async def execute(envelop, agent): ..."}`，返回必须 `{"data": ...}`） |
| 造页面 / 应用 | `builtins/tools/save_blocks`（`=== PLUGIN: path ===` 代码块，一次一个文件） |
| 定时任务 | `os/_cron` |
| 实时推送 | `os/_websocket` |

详细配方（含参数样例和踩坑）在 `references/plugin-recipes.md`。

三条纪律：

1. **一次一个工具调用**，拿到结果再决定下一步。
2. **调用前先确认 receiver 拼写是完整路由**（`builtins/tools/xxx` 而不是 `xxx`）。错了不会报路由错，只会 `plugin not found`。
3. **连续两次同样失败就换路径**——读源码（`file_utils_api read_file`）而不是继续猜参数。

## 服务没在跑怎么办

`doctor` 会区分"端点不通"和"配置没写"，别混为一谈。

- **同机、AICP 网关在跑**：`python3 scripts/aicp_mcp.py ensure --api http://127.0.0.1:9000`（等价于 POST `/api/os/_mcp_server` 送 `{"action":"START","port":9100}`）。
- **AICP 侧在自己手上（你是 AICP 里的 agent）**：直接调 `os/_mcp_server` 的 `START`，别让用户去敲命令。
- **不同机器**：端口要通、可能要 SSH 隧道；不要擅自把监听地址改成 `0.0.0.0`（见下面的安全警告）。

## 安全警告（必须主动告知用户）

MCP 端点默认**匿名全权限**（`permissions: {"*": true}`，可以用 `call_plugin` 调 `code_runner` 执行任意代码）。因此：

- 只监听 `127.0.0.1`；要让别的机器访问就上 SSH 隧道或带认证的反代，不要 `0.0.0.0` 裸奔。
- 不要把这个端口的 URL 贴进公开文档、issue、截图。
- 用户要求绑 `0.0.0.0` 时，先说明这个后果，并要求他同时配置 `os/_auth` 的 token。

## 硬性禁令

- ❌ 不要用 `curl` 手工 POST 当作"验证通过"——`verify` 才是唯一判据。
- ❌ 不要为了修配置去装 Node/Homebrew/Java/其他引擎。客户端配置和运行时依赖是两件事。
- ❌ 不要写第二份 aicp 条目而不清理旧的（会出现两个同名 server，客户端行为不确定）。
- ❌ 不要在没读回校验的情况下直接改客户端 JSON（JSONC 注释、缩进、其他 server 都可能被吃掉）。
- ❌ 不要把 `plan` 的输出跳过给用户看就 apply。
- ❌ 不要在客户端已经连上之后再跑一遍 `apply`——`apply` 是幂等的，但重复执行会让用户困惑。
