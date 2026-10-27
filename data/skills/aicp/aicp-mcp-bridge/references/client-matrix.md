# 客户端注册矩阵

每个客户端读哪个文件、用哪个 server key、走什么 transport。`scripts/aicp_mcp.py` 里 `client_path()` / `CLIENTS` / `entry_for()` 就是这张表的实现。

## 一、路径与格式

| `--client` | 配置文件 | server key | transport | 备注 |
|---|---|---|---|---|
| `claude_code` | `$CLAUDE_CONFIG_DIR` 或 `~` 下的 `.claude.json` | `mcpServers` | http | 支持 `type: "http"` 直连 |
| `claude_desktop` | Win `%APPDATA%\Claude\claude_desktop_config.json`；mac `~/Library/Application Support/Claude/...`；Linux `~/.config/Claude/...` | `mcpServers` | **stdio** | 只吃 stdio → 用 `mcp_stdio_bridge.py`；改配置前必须**完全退出** app |
| `cline` | `<VS Code User>/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` | `mcpServers` | http | 条目要 `type: "streamableHttp"` + `disabled: false` |
| `cursor` | `~/.cursor/mcp.json` | `mcpServers` | http | 全局配置，不动项目级 `.cursor/mcp.json` |
| `vscode` | `<cwd>/.vscode/mcp.json` | **`servers`** | http | key 不叫 mcpServers；`type: "http"` |
| `codex` | `$CODEX_HOME` 或 `~/.codex/config.toml` | `mcp_servers` | http | TOML，`[mcp_servers.aicp]` + `url = "..."` |

VS Code User 目录：Win `%APPDATA%\Code\User`；mac `~/Library/Application Support/Code/User`；Linux `~/.config/Code/User`。

## 二、写入的条目长什么样

```json
// claude_code / cursor（http 直连）
"aicp": { "type": "http", "url": "http://127.0.0.1:9100/mcp" }

// cline
"aicp": { "type": "streamableHttp", "url": "http://127.0.0.1:9100/mcp", "disabled": false }

// vscode（注意 servers，不是 mcpServers）
"aicp": { "type": "http", "url": "http://127.0.0.1:9100/mcp" }

// claude_desktop（stdio 桥）
"aicp": {
  "command": "C:\\Python311\\python.exe",
  "args": ["E:\\aicp-skills\\data\\skills\\aicp\\aicp-mcp-bridge\\scripts\\mcp_stdio_bridge.py",
           "http://127.0.0.1:9100/mcp"],
  "env": {}
}
```

```toml
# codex: ~/.codex/config.toml
[mcp_servers.aicp]
url = "http://127.0.0.1:9100/mcp"
```

`command` 用 `sys.executable`，不是裸 `python` / `python3`——客户端 spawn 时的 PATH 和你 shell 里的 PATH 经常不是一回事。

## 三、JSONC 与注释

Claude Code / Cursor / Cline 的配置文件**可能带注释和尾逗号**。`apply` 的处理：

- 能 `json.loads` 直接过 → 正常写
- 过不了但 `strip_jsonc` 能过 → 标记 `jsonc: true`，**默认拒绝写**（重写会吃掉注释），除非 `--allow-jsonc`
- `--allow-jsonc` 会先备份再重写，返回里带 `comments_lost: true`

这是刻意的：宁可让 agent 报"需要用户确认"，也不要静默毁掉用户的配置文件。

## 四、同名冲突

`plan` 返回 `current`（现有条目）。若 `current` 存在但 `aligned: false`（URL 或 args 不同），说明：

1. 你自己之前注册过别的地址 → 直接 `apply` 覆盖（会备份）
2. **别人**注册的 `aicp` → 换名字：`--name aicp2`，不要覆盖

判断不了就问用户，别猜。

## 五、transport 选择原则

| 客户端能力 | 用什么 |
|---|---|
| 支持 `type: "http"` / `streamableHttp` | 直连 `http://127.0.0.1:9100/mcp` |
| 只支持 stdio | `mcp_stdio_bridge.py <url>` |
| 只支持 SSE（老版本） | 桥也能扛（它解析 `data:` 行），但延迟高，优先升级客户端 |

`verify` 通过 HTTP 验的是**端点本身**；stdio 桥单独 `check_bridge()` 检查文件存在。桥要冒烟可以手工跑：

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 scripts/mcp_stdio_bridge.py http://127.0.0.1:9100/mcp
```

看到一行 JSON、里面 `result.tools[0].name == "call_plugin"` 就通了。

## 六、远程/跨机部署

MCP 端点不在本机时：

- `--url http://<host>:9100/mcp` 注册；`doctor` / `verify` 也带同一个 `--url`
- 端口要通、可能要防火墙或 SSH 隧道：`ssh -N -L 9100:127.0.0.1:9100 user@host` 然后本地注册 `127.0.0.1:9100`——**推荐这条**，比暴露端口安全
- **不要**因为方便就把 AICP 侧改成 `0.0.0.0`（见 SKILL.md 安全警告）
