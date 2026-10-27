# 连接与故障恢复

只在**观察到具体阻塞**时才读这份。先看清是"服务没起"、"配置没写"、"客户端没连上"，还是"客户端策略拒绝"——四者补救方式完全不同，互相不能替代。

## 一、分诊表

| 观察到的现象 | 含义 | 下一步 |
|---|---|---|
| 会话里没有任何 `call_plugin` 工具 | 客户端没加载到 aicp 条目，或加载了但连接失败 | `doctor --json` 看 `client:*` 项是 `missing/absent/configured` 哪种 |
| `doctor` 里 `endpoint` FAIL | 端口没监听 / 服务没起 / 地址错 | `ensure` 或让 AICP 侧启动 `os/_mcp_server` |
| `endpoint` OK，`mcp_handshake` FAIL | 端口被别的服务占了，或 MCP 路由异常 | 看 `GET /health` 返回的 `server` 字段；AICP 侧查日志 |
| `mcp_handshake` OK，`tool_call` FAIL | 端点和协议都对，`call_plugin` 转发失败 | 多半是 `receiver` 拼错，见 `plugin-recipes.md` |
| `client:*` 是 `configured` 但 `aligned: false` | 有条目但指向别的地址（旧端口 / 旧机器） | 重新 `apply`（会备份），或先删旧条目 |
| `client:*` 是 `invalid` | 配置文件本身解析不了 | 别自动重写，报给用户修 |
| 配置正确、`verify` 通过，但客户端里仍无工具 | 客户端没重启 / 没重连 | 重启客户端（Claude Desktop 需完全退出） |
| 客户端报 needing approval / never | **客户端策略**拒绝，不是 AICP 问题 | 让用户在该客户端里批准，或配置 preapprove；不要重跑 apply |
| `stdio_bridge` FAIL | `mcp_stdio_bridge.py` 丢了 | 从 skill 目录恢复该文件 |

**关键：`verify` 通过 ≠ 客户端可用。** `verify` 证明的是"端点 + 协议 + 工具调用"三件事都正常；客户端侧还有"是否重启""是否批准"两道闸。

## 二、服务没在跑

`doctor` 的 `endpoint` 检查已经区分了这点。三种情况：

1. **你在 AICP 里（是这个 agent 本人）**：直接调 `os/_mcp_server` 的 `START`，不要绕去 shell。
2. **同机有网关**：`python3 scripts/aicp_mcp.py ensure --api http://127.0.0.1:9000`。它 POST `/api/os/_mcp_server` 送 `{"action":"START","host":"127.0.0.1","port":9100}`，然后轮询 `/health` 最多 10 秒。
3. **跨机器**：先确认网络可达；推荐 SSH 隧道 `ssh -N -L 9100:127.0.0.1:9100 user@host`，再按 `127.0.0.1:9100` 注册。**不要**为了方便把监听地址改成 `0.0.0.0`。

`os/_mcp_server` 的 `START` **不是幂等的**：重复调用会重复创建 runner 和清理任务。先查 `/health`，通了就别再 START。

## 三、配置写了但客户端不认

按顺序排查：

1. **配置文件路径对不对**——`doctor` 打的就是实际路径。路径对但文件里有 `aicp` 却不生效，多半是路径不是客户端真正读的那份（Claude Desktop 的 MSIX 虚拟化、VS Code 的 workspace vs user 等）。
2. **server key 对不对**——VS Code 是 `servers` 不是 `mcpServers`；Codex 是 TOML 的 `mcp_servers`。见 `client-matrix.md`。
3. **transport 类型对不对**——Claude Desktop 只吃 stdio，给它写 `url` 等于没写。
4. **重启了没**——几乎所有客户端都是启动时读配置。
5. **客户端有没有报错**——看客户端的 MCP 日志/输出面板，别猜。

## 四、`apply` 失败怎么读

| `status` | 含义 | 处理 |
|---|---|---|
| `unchanged` | 已有条目完全一致 | 不需要做任何事 |
| `configured` | 写入并读回校验通过 | 去重启客户端 |
| `blocked` + `reason: jsonc` | 文件带注释/尾逗号，重写会丢注释 | 让用户决定：`--allow-jsonc`（会备份）还是手改 |
| `readback_failed` | 写进去但读回来不一致 | **立刻看 backup 字段**，用备份恢复，别继续 |
| `ok: false` 且无 status | 前置检查失败（路径不可写等） | 查 `error` / `remediation` |

`apply` 在没有 `--yes` 时**永不写文件**——它只回显计划。这是设计上的两道闸：先 `plan` 给用户看，再 `apply --yes`。

## 五、安全相关的失败

MCP 端点默认匿名全权限。如果 `doctor` 显示 endpoint 通但客户端访问被 401/403：

- AICP 侧配了 `os/_auth`，需要在客户端条目里加 `Authorization: Bearer <token>` 头
- 这属于**部署决策**，不是 skill 该自动改的——报给用户，让他提供 token 和策略

## 六、不要做的事

- ❌ 不要为了修 MCP 连接去装 Node / Java / Homebrew / 任何引擎。AICP 是 Python 服务，客户端配置和运行时依赖是两回事。
- ❌ 不要重复跑 `apply`。它是幂等的，但连续跑只会让用户困惑；先 `doctor` 看状态。
- ❌ 不要在不看 `plan` 的情况下 `apply`。
- ❌ 不要在 `verify` 失败时谎称"已连接"。
- ❌ 不要把 `curl` 手工 POST 的结果当作验证通过——`verify` 是唯一判据。
- ❌ 不要同一个 MCP server 写两份条目（`aicp` 和 `aicp2` 同时在），客户端行为不确定。
