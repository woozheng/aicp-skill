---
title: 用 REA 逆向工具分析二进制与 APK
description: 用 REA MCP 的 229 个逆向工具做静态与运行时分析（用户说「帮我逆向这个 exe」「reverse this apk」「这个 apk 怎么脱壳」时用），覆盖选型、工作流与本机限制
category: aicp
tags: [reverse, mcp, binary, apk, javascript]
triggers: 逆向, 反编译, 脱壳, 二进制, 可执行文件, apk分析, 安卓逆向, 静态分析, 动态分析, 证据链, 漏洞分析, reverse, binary, decompile, unpack, apk, analyze, static, dynamic, evidence
not_triggers: 编写代码, 生成应用, 前端页面, 数据分析, 单元测试, 格式化
examples: "帮我逆向一下这个 exe 看看它导出了哪些函数", "这个 apk 怎么脱壳 帮我看看加没加壳", "用 REA 看看这个二进制里的 import 表", "reverse this apk and list its exported functions"
version: 1.1
updated_at: 2026-11-05
---
# 用 REA 逆向工具分析二进制与 APK

REA 以 MCP server 形态提供 229 个逆向工具，已通过 `os/_mcp_client` 接进 AICP，
路由形式 `mcp/rea/{tool}`，也可用 `mytools/rea_server` 的 `call` action。

## 何时用

- 需要分析二进制文件、APK、脚本包时（用户说「帮我逆向一下这个 exe」时）
- 需要脱壳判断、导入导出表分析时（用户说「这个 apk 怎么脱壳」时）
- 需要提取证据链、对比两个版本行为差异时（用户说「用 REA 看看 import 表」时）
- 需要逆向 Electron / Web 包、还原模块时（用户说「reverse this apk」时）

**不适用**：编写代码；生成应用；前端页面；数据分析；单元测试；格式化。

## 前置条件

- REA MCP server 可达：先执行 `ensure` 探活并注册路由（幂等）。
- 一次性工具需要**绝对路径**（`/xxx` 或 `C:\xxx`）。
- 输出目录类工具要求目标目录**不存在**（除非带 `resume`/`overwrite`）。
- 设备类操作需 adb 已连接设备；CDP 类工具需 loopback CDP 端点。
- 深度反编译需 Ghidra / Hopper / IDA，本机未安装则不可用。

## 核心步骤

### 第一步：确保 REA 活着

```python
use_tool(target="mytools/rea_server", action="ensure", plugin_args={})   # 幂等：探活→拉进程→connect→注册路由
use_tool(target="mytools/rea_server", action="status", plugin_args={})   # pid / port / tool_count / connected
use_tool(target="mytools/rea_server", action="tools", plugin_args={"category": "native"})  # 按类查工具名
```

`ensure` 会自动找 npm 全局的 `rea-agents`、挑空闲端口、起 `node scripts/rea.mjs mcp --http`，
再调 `os/_mcp_client.connect`。进程状态落在 `data/mytools/rea_server/`（pid / port / rea.log）。

调用工具时若报连接拒绝，直接 `ensure` 一次即可（插件也内置了自动 ensure）。

### 本机环境限制（重要）

| 能力 | 状态 |
|---|---|
| Ghidra / Hopper / IDA | **未安装** → `procedure_pseudo_code`、`batch_decompile`、`decompile_all`、`analyze_function`、`get_call_graph` 这类**深度反编译不可用** |
| macOS 专属（Mach-O / LLDB / 观察原生 UI / Apple 签名） | 本机 Windows，**不可用** |
| 免引擎静态分析（PE / ELF / APK / JADX / WASM / SQLite / Go / Rust / IL2CPP / PyC / BEAM / EVM / 固件签名） | **可用**，优先走这条 |
| adb | 可用（需设备连接） |
| CDP / 浏览器 / Electron | 可用（需 loopback CDP 端点） |
| Frida / GDB / Rizin debug | 需额外安装对应工具 |
| `linux-only` 工具（unblob、sigrok、pwntools、gdb session、rizin debug） | 本机 Windows **不可用** |

结论：**本机以「免引擎静态分析 + APK + JS/Web」为主战场**。要原生反编译需先装 Ghidra。

### 目标类型 → 起手式

#### PE / Windows exe
```
inspect_binary_layout          # 段/节/地址/依赖，底座
inspect_pe_imports             # DLL + 函数
inspect_native_exports         # 导出表
inspect_pe_resources           # 资源树（图标/manifest/版本）
inspect_pe_certificates        # 签名
inspect_pe_load_config         # TLS 回调 / CFG / 安全 cookie
inspect_go_binary / inspect_rust_binary   # 语言识别
inspect_byte_entropy           # 是否加壳
```
#### ELF / Linux
```
inspect_binary_layout
inspect_elf_notes              # build-id / ABI
inspect_native_exports
inspect_kernel_module          # .ko
inspect_go_binary / inspect_rust_binary
```
#### APK / Android
```
inspect_android_package        # 包名/权限/组件（免 jadx）
inspect_apk_signature          # v1/v2/v3 签名
decode_android_resources       # apktool 解 resources + manifest
search_android_classes         # 按类名搜
inspect_android_class          # 类的方法/字段
inspect_android_method         # 反编译单方法（需 overload_index）
trace_android_references       # 找调用点
project_android_application_graph
```
有设备时：`list_adb_devices` → `resolve_adb_packages` → `pull_adb_package` → 再静态分析。

#### JavaScript / Electron / Web
```
analyze_javascript_application   # ASAR/解压目录 → 应用图谱（离线，不用引擎）
recover_javascript_sources       # wakaru 还原模块（linux-only，可能不可用）
inspect_web_page                # CDP 被动观察 DOM/脚本/网络
analyze_web_bundle              # CDP 抓脚本 → 路由/接口候选
export_web_scripts              # 导出脚本 + manifest
trace_web_module_imports / trace_web_source_location
inspect_electron_page / capture_electron_scenario
```
#### 网页运行时
```
list_browser_targets            # 先拿 target_id
inspect_web_page                # 被动快照
observe_web_session             # 观察窗
capture_browser_scenario        # 受控动作（点击/输入）
capture_web_screenshot / compare_web_screenshots
inspect_web_network_capture     # HAR/mitmproxy/qlog
```
#### WASM / 固件 / 其他
```
inspect_wasm_artifact
inspect_evm_interface
inspect_firmware_regions / extract_firmware   # linux-only
inspect_sqlite_database
inspect_python_pyc
inspect_erlang_beam
inspect_devicetree
inspect_model_artifact          # GGUF/safetensors
```

### 会话工作流（会话型工具）

原生深度分析走会话：**open → 探查 → close**。

```
open_binary   {path, provider_id?, format?, architecture?, entry_point?}
  → binary_session / binary_overview
  → list_procedures / list_strings / search_strings
  → procedure_pseudo_code {procedure}
  → analyze_function {procedure}
  → get_call_graph / trace_call_path / trace_feature
  → close_binary {snapshot_path?, overwrite?}
```

- `procedure` 可用符号名或十六进制地址；Ghidra 还接受 `file-offset:0x...`。
- `detail: "summary"` 在大结果时优先用，省 token。
- 结束时 `close_binary` 释放 provider 资源；`snapshot_path` 可存分析快照。

### 证据链（结论必须带证据）

REA 的核心设计是 **Evidence + residual unknown**，别丢结论：

```
get_evidence_bundle       # 取全部留存证据
export_evidence_bundle    # 落盘成 JSON（跨会话复用）
import_evidence_bundle    # 合并进来
list_unknowns             # 残留未知项
record_unknown / update_unknown / verify_unknown_resolution
```
对比类：
```
compare_functions / compare_artifacts / compare_bundles / compare_application_versions
compare_web_captures / compare_process_captures / match_native_functions
find_changed_behavior / correlate_static_and_runtime
```

### 常用调用形式

```python
# 走虚拟路由（已注册 229 条）
use_tool(target="mcp/rea/inspect_android_package", action="", plugin_args={"path": "C:\\app.apk"})

# 走插件 call（带自动 ensure）
use_tool(target="mytools/rea_server", action="call", plugin_args={"tool": "inspect_pe_imports",
                                       "arguments": {"path": "C:\\a.exe"}})
```

## 判断规则

| 条件 | 动作 |
|---|---|
| REA 未连上（调用报连接拒绝） | 先 `ensure` 再重试一次 |
| 目标是 PE / ELF / APK / WASM / 脚本包 | 走一次性 `inspect_*` 静态分析 |
| 需要函数级反编译 / 调用图 | 走 `open_binary` 会话；本机无 Ghidra 则不可用，只能降级 |
| 结果体较大或可能超 32000 字符 | 加 `detail="summary"`，必要时再 `inspect_analysis_view` 取全文 |
| 一次性工具传了相对路径 | 改为绝对路径（`/xxx` 或 `C:\xxx`） |
| 输出目录已存在 | 改用新目录，或带 `resume` / `overwrite` 参数 |
| 操作涉及设备写操作（`push_adb_file` / `install_adb_package` / `start_adb_app`） | 先向用户确认再执行 |
| 目标是 linux-only 工具 | 本机 Windows 不可用，改走免引擎路径 |
| 需要对比两个版本或两个样本 | 用 `compare_*` 系列，必要时 `export_evidence_bundle` 落盘复用 |

## 踩过的坑

| 坑 | 现象 | 根因 | 修法 |
|---|---|---|---|
| `detail="summary"` 丢证据 | 返回体只有摘要，完整 Evidence 留在会话里，带 `parent_evidence_id` | summary 是视图裁剪，不是全量返回 | 后续再调一次取全量，或走 `inspect_analysis_view` |
| 返回超 32000 字符被截断 | 响应带 `truncated: true`，后段丢失 | `_mcp_client` 有单次返回上限 | 大结果一律先 `detail="summary"` |
| 一次性工具报路径无效 | 相对路径被直接拒绝 | `inspect_*` 要求绝对路径 | 传 `/xxx` 或 `C:\xxx` |
| `open_binary` 解析错格式 | 文件被当成错误格式解释 | `format` 是「显式解释」开关，不传才是自动识别 | 优先不传；`raw-binary` 必须同时给 `architecture` 和 `entry_point` |
| 输出目录报错 | 目录已存在导致失败 | 输出类工具要求目录不存在，除非有 `resume`/`overwrite` | 换新目录或传 `overwrite` |
| 设备写操作造成破坏 | 设备状态被改 | `push_adb_file` / `install_adb_package` / `start_adb_app` 是变更类操作 | 写操作前先确认 |
| CDP 工具找不到目标 | 提示缺 `target_id` | `target_id` 必须来自 `list_browser_targets` | 先取 target 再调用 |
| REA 进程残留 | 端口占用、后台进程堆积 | REA 是本插件拉起的独立进程 | `mytools/rea_server` 的 stop 动作可停掉 |

## 验证

自检脚本走 `code_runner`，骨架如下（沙箱内禁止 print / time.sleep / input / while True / subprocess.Popen / 同步 requests / eval / exec）：

```python
async def execute(envelop, agent):
    from core import Envelop

    r = await agent.system.call(
        Envelop(
            receiver="mytools/rea_server",
            payload={"action": "status"},
        )
    )
    p = getattr(r, "payload", None) or {}
    st = getattr(r, "status", "") or ""
    return {
        "data": {
            "ok": bool(st == "" and p),
            "connected": p.get("connected"),
            "tool_count": p.get("tool_count"),
            "port": p.get("port"),
            "error": st or None,
        }
    }
```

断言清单：

1. `status` 调用不返回非空 `status`（即无插件级错误）。
2. `connected` 为真，说明 REA MCP server 已连上、路由已注册。
3. `tool_count` 大于 0（应为 229 左右）。
4. 跑通一次真实 `inspect_*` 时，返回含目标类型的关键字段（如 PE 的 imports 列表非空）。

## 参考

- 相关 skill：`skill/aicp/skill-lint`（本文件改写后的规范校验）、`skill/aicp/plugin-action-smoke-test`（插件 action 冒烟自检）
- 相关插件：`mytools/rea_server`（ensure / status / tools / call / stop）、`os/_mcp_client`（MCP 连接注册）
- 相关文件：`plugins/mytools/rea_server.py`
