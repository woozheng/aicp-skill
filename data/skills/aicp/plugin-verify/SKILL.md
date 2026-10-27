---
name: plugin-verify
title: 本地系统插件排查与验证
description: AICP 本地系统插件（源码在 plugins/os/*.py，路由 os/_xxx）的 Bug 定位与验证流程。覆盖「注册/销毁不对称、多份状态副本、异常被静默吞掉」三类隐患的根因定位与修复，以及「直接调用插件 vs code_runner」两条验证路径的选型规则。适用于删除后残留、幽灵路由、插件不生效等本地系统插件问题。
tags: ["本地系统插件", "排查流程", "验证", "os/_xxx", "code_runner"]
metadata:
  version: "1"
---

# 本地系统插件排查与验证

## 适用范围（重要：本地系统插件）

本 Skill 只针对 **AICP 本地系统插件**：

| 项 | 说明 |
|---|---|
| 源码位置 | `plugins/os/<name>.py` |
| 路由 | `os/_<name>`（去后缀，`_` 前缀表示系统核心） |
| 调用方式 | `use_tool(target="os/_xxx", action="<action>", plugin_args={...})` |
| 运行位置 | **AICP 主控进程内**（不是独立进程、不是外部服务） |
| 典型代表 | `os/_mcp_client`、`os/_mcp_server`、`os/_registry`、`os/_cron`、`os/_websocket`、`os/_db`、`os/_gateway`、`os/_auth` |

与外部/第三方插件的区别（决定了排查手法）：

- 本地系统插件**持有进程内全局状态**（`core.plugins`、`os/_registry._virtual`、`servers.json` 之类的注册表），因此「写了几处、删了几处」是否对称，是 bug 的主要来源。
- 它由 `execute(action=...)` **分发**，参数与 `agent` 注入在分发分支里完成 —— 单测替身最容易绕过这条路径。
- 它**同时被读路径和写路径消费**（cogitor 搜索 / `list_tools` 读，`connect`/`remove` 写），两边不一致就会出现「界面显示已删、实际还在」。

不适用于：外部 MCP Server、`www/` 下的前端页面逻辑、第三方 SDK 集成（那些没有进程内注册表，症状模型不同）。

---

## 一、排查流程（固定四步，不许跳）

1. **复现** —— 走用户真实路径（前端按钮 → HTTP 网关 → `execute` 分支），拿到确切症状。
2. **定位根因** —— 读源码，列出「这个资源被创建在哪几处、被销毁在哪几处」，逐一对账。
3. **改代码** —— 修根因，不是修症状。改完自问：下次换个入口删，还会不会复现？
4. **验证** —— 真实路径上症状消失才算数（见第三节选型）。

> 反面教材：先手工清理一遍残留数据、看到搜索干净了就收工 —— 那是治症状，根因还在，下次连再删照样复现。

---

## 二、三类高频隐患（核心自检清单）

### 1. 注册 / 销毁不对称

同一个资源，创建路径写了 N 处，删除路径只清了 1 处。

- 对账方法：列出所有写点（配置文件、内存注册表、磁盘缓存、契约文件、虚拟路由），再列所有删点，逐项打勾。
- MCP 案例：注册写 `servers.json` + `core.plugins` + `os/_registry._virtual`，删除只写了第一个 → 幽灵路由。
- **修法**：注册与注销必须是**同一个 owner 的成对操作**，要么一起动，要么封装成一对函数（`_register_virtual` / `_unregister_virtual`）。

### 2. 多份状态副本

同一事实存在 ≥2 处存储，就一定会漂移。

- 选一份作为**唯一真相源**（MCP 场景是 `servers.json`），其余都是派生。
- 加**读时自愈**兜底：读路径（列表/搜索）开头做一次对账，把「真相源里没有、运行时还在」的孤儿自动回收。这样以后新增任何删除入口都不会漏。
- 自愈要挂在**读路径**上（如 `list_tools`），因为 cogitor / 搜索的数据源就是它。

### 3. 异常被静默吞掉

`except Exception: pass` 是排查成本指数级上升的元凶：界面显示成功、实际失败，且**没有任何线索**。

- 改成 `agent.log.warning(...)`，把失败显式化。
- 清理失败不能抛给前端到崩溃，但**必须留日志**。

### 附加自检项

| 检查项 | 对应本次 |
|---|---|
| 创建资源有没有配对的销毁？ | 虚拟路由注册 vs 注销 |
| 有没有第二份状态副本？ | 真相源 vs 派生副本 |
| 清理失败有没有被静默吞掉？ | `except: pass` |
| 读路径与写路径是否一致？ | 删完用 cogitor 搜，读路径自愈生效 |
| 验证是否走了真实插件路径？ | `use_tool` 直调而非沙箱替身 |
| 跑 code_runner 前，diff 的代码段是否都读过了？ | 读全再写调用代码 |

---

## 三、验证路径选型（`use_tool` 直调 vs `code_runner`）

两者**殊途同归**，打的是同一个实例，差别在成本与覆盖，不是「一个对一个错」。

| | 直接 `use_tool` | `code_runner` |
|---|---|---|
| 覆盖 | 真实进程 + 真实 `execute` 分发 + 真实注册表 + agent 注入 + HTTP 网关链路 | 同上，**前提**：import 真实模块，不写 fake 替身 |
| 独有能力 | 编排：拿到结果实时决定下一跳 | 连续多次调用、批量、单元断言、分支遍历 |
| 成本 | 一次一调，最快 | 要先读源码，单次慢 |
| 不能覆盖 | 无法批量断言 | fake core ≠ 真实注册表，可能「测试通过但线上不通过」 |

### 选型三问

1. 只改一处 / 只验一个 action？→ **直接 `use_tool`**，快。
2. 要连着调多个、跑单测、铺分支？→ **`code_runner`**。
3. 要编排多步任务？→ **一次一步 `use_tool`**，拿结果再定下一跳。

### code_runner 的硬前提

**先把相关源码读出来，看清 action 签名与 `execute` 分发逻辑，再写调用代码。** diff 了几段就必须把那几段都读出来 —— 没读全就写 = 盲调；写 fake core 替身 = 自欺（bug 恰恰藏在被绕过的分发分支里）。

正确用法：在 code_runner 里 **import 真实插件模块 + 注入真实 core**，此时覆盖度与直调等同，还能批量断言。

---

## 四、验证阶梯（按成本从低到高）

1. 已知 action 签名 → `use_tool` 直调，真实执行。
2. 未知签名 → `contract_agent.get(plugin="os/_xxx")`（慢，实测 ~70s，慎用，只在必须时调）。
3. 读路径验证 → `cogitor.search([...])` / `cogitor.list_plugins()`，确认索引/路由与写路径一致。
4. 需要连续多步 / 单元测试 / 批量 → `code_runner`（前提：先读源码）。

## 五、本地系统插件常用调用速查

```
use_tool(target="os/_mcp_client", action="list_servers", plugin_args={})
use_tool(target="os/_mcp_client", action="call_tool",   plugin_args={server:"x", tool:"y", arguments:{}})
use_tool(target="builtins/tools/cogitor", action="search", plugin_args={terms:["mcp"], limit:20})
use_tool(target="builtins/tools/contract_agent", action="get", plugin_args={plugin:"os/_mcp_client"})
use_tool(target="builtins/tools/file_utils_api", action="code_structure", plugin_args={path:"plugins/os/_mcp_client.py"})
use_tool(target="builtins/tools/file_utils_api", action="read_file_lines", plugin_args={path, start_line, end_line, with_line_num:true})
```

读大文件先 `code_structure` 拿行号地图，再按行号精确读；不要全文 dump（会挤爆信息流）。

---

## 六、交付口径

修复后必须交代三件事：

1. **做了什么**（改哪个文件、哪几个函数、每处改动解决哪条根因）
2. **局限在哪**（例如：手改配置文件绕过插件删除仍可能产生残留，需靠读时自愈回收）
3. **还能怎么改**（例如：把清理失败的失败态回传给前端，而不是只写日志）

并说明验证方式：**直调真实插件**（列出调了哪个 action、返回什么），而不是「单测通过」。
