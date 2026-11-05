---
title: 修 runtime/_llm.py 流式逻辑
description: 修 runtime/_llm.py 的 think 剥离与流式超时重试（用户说「改了 _llm.py 不生效」「the stream still leaks think tags」时用），覆盖重启引擎、重新加载验证等 5 个坑
category: aicp
tags: [runtime, llm, stream, think, timeout, retry]
triggers: 不生效, 没生效, 不起作用, 重新加载, 重启引擎, 热重载, think剥离, 剥离think, 泄露think, 流式超时, 重试, 旧模块, not effective, restart engine, reload module, strip think, stream timeout, retry, stale module, hot reload
not_triggers: 前端页面, 样式修改, 插件路由, 新建应用, 生成应用
examples: "改了 runtime/_llm.py 但线上没生效", "think 标签还是泄露出来了", "帮我看下 edit_file 改完报语法错 line 120", "the llm stream still uses the old module after my edit"
version: 2.0
updated_at: 2026-11-05
---

# 修 runtime/_llm.py 流式逻辑

## 何时用

- 改 `runtime/_llm.py` 的 `chat_stream` / `chat_tool_stream` / `_iter_stream_with_timeout` / `_strip_leading_think` / 超时与重试逻辑（用户说「改了 _llm.py 不生效」「the stream still leaks think tags」时）
- 验证流式输出时怀疑跑的是旧代码，需要确认模块已重新加载（用户说「验证结果和预期不符」「this still uses the old module」时）
- think 标签泄露、think 未闭合时把尾部当正文吐出（用户说「think 标签还是泄露出来了」「think tags leak into the answer」时）
- 改完引擎底层代码需要向用户交代生效方式（用户说「要不要重启」「when does this take effect」时）

**不适用**：前端页面；样式修改；插件路由；新建应用；生成应用。

## 前置条件

- 目标文件：`runtime/_llm.py`（属底层协议/加载器层）
- 工具：`builtins/tools/file_utils_api`（read_file_lines / edit_file）、`builtins/tools/code_runner`
- 需要 LLM 时先兜底：`if not agent.llm: return {"data": {"error": "LLM 未配置"}}`
- 知道当前引擎是否可重启；本技能只保证代码正确，不代替重启

## 核心步骤

1. 定位待改分支。用 `use_tool(target="builtins/tools/file_utils_api", action="search", plugin_args={"path": "runtime/_llm.py", "content": "关键词", "context": 3})` 取原文片段，不要整文件读取。
2. 改代码。用 `use_tool(target="builtins/tools/file_utils_api", action="edit_file", plugin_args={"path": "runtime/_llm.py", "find": "原文片段", "replace": "新片段"})`；`find` 必须与原文完全一致且唯一，`replace` 自带完整缩进。改完检查返回的 `validation.ok`，为 `false` 立即修，不带着语法错误继续。
3. 验证时重新加载模块。`from runtime import _llm` 拿到的是引擎启动时已导入的旧模块对象，必须用 `importlib` 从磁盘重新载入：

```python
import importlib.util, sys
spec = importlib.util.spec_from_file_location("llm_fresh_probe", "runtime/_llm.py")
L = importlib.util.module_from_spec(spec)
sys.modules["llm_fresh_probe"] = L
spec.loader.exec_module(L)   # L 一定是最新磁盘内容
```

4. 构造假 client 跑断言。`client.base_url` 必须存在：

```python
class FakeClient:
    base_url = "http://fake/v1"
    def __init__(self, chunks):
        self.chat = FakeChat(chunks)
```

假 chunk 还要带：`choices[0].delta.content` / `.tool_calls` / `.reasoning_content` / `.reasoning` / `.thinking`，
以及 `chunk.usage`、`choice.finish_reason`、`chunk.choices` 为空时不能崩（`if not chunk.choices: continue`）。

5. 交付时明确告诉用户「`runtime/` 不走热重载，需要重启引擎才生效」。

**分层提醒**：`runtime/` 属于最底层（协议/加载器层），**热重载不覆盖，必须重启引擎**。

## 判断规则

| 条件 | 动作 |
|---|---|
| 改的是 `plugins/**` | 热重载，立即生效 |
| 改的是 `runtime/**` 或 `core/**` | 必须重启引擎，交付时明确说明 |
| 验证结果与预期不符 | 先确认是不是「旧模块」，再看代码 |
| `validation.ok: false` | 看报错行号的**上一条 if/elif** 分支缩进 |
| think 未闭合 | 丢弃整段缓冲并打日志，置 `think_unclosed = True` |

## 踩过的坑

| 坑 | 现象 | 根因 | 修法 |
|---|---|---|---|
| 改了不生效 | 线上行为与代码不符 | `runtime/` 不走热重载 | 重启引擎；别用 code_runner 通过推断线上生效 |
| 验证时模块是旧的 | 明明改了还是泄漏 | `from runtime import _llm` 拿到进程启动时的旧模块 | 用 importlib 从磁盘重新加载（见核心步骤 3） |
| 假 client 缺 `base_url` | AttributeError | 日志读 `client.base_url` | 写成类属性 `base_url = "http://fake/v1"` |
| `edit_file` 缩进不自洽 | 语法错 `expected 'except' or 'finally' block` | 替换 `elif`/`else` 分支时 new_content 未自带完整缩进 | 先 read_file_lines 确认行号与缩进，再逐个修正，每次看 `validation.ok` |
| 报错行号指向上一个 if | 报错位置对不上 | 语法错行号指向**上一个** `if` 的起始行 | 沿报错行号往上找最近的 if/elif 分支 |
| think 三态判定错 | think 未闭合时把尾部当正文输出 = 泄露模型内部推理 | `in_think` 三态被当成二态 | `None` 继续攒，`True` 未闭合整段丢弃，`False` 直接 yield |

think 剥离三态判定：

| 状态 | 含义 | 正确行为 |
|---|---|---|
| `None` | 缓冲不足 `THINK_DECIDER_LEN`(10) | 继续攒；流结束时用 `_strip_leading_think` 兜底（未闭合返回空串） |
| `True` | 已确认进入 think 块 | **未闭合时整段丢弃**，不要把尾部当正文吐出 |
| `False` | 确认是正文 | 直接 yield |

`<think>` 未闭合时**把尾部当正文输出 = 泄露模型内部推理**。正确修法是丢弃整段缓冲并打日志，
同时把 `think_unclosed` 置 `True`（置 `False` 会让「未闭合 → 安全重试」的分支变成死代码）。

`edit_file` 还会保留未覆盖的旧行，短内容替换长内容时要核对区间是否全覆盖。

## 验证

用 code_runner 跑，遵守沙箱契约（禁 print / time.sleep / input / while True / subprocess.Popen / 同步 requests 打本地 / eval / exec），骨架如下：

```python
async def execute(envelop, agent):
    # 见核心步骤 3 的 importlib 重新加载
    # 见核心步骤 4 的 FakeClient
    llm = L.LLM({"models": {"providers": {...}, "roles": {...}, "max_retries": 1}})
    llm._clients["openai"] = FakeClient(chunks)
    llm._model_to_client["openai/m1"] = "openai"
    # 依次跑：未闭合 think / 已闭合 think / 纯正文 / tool_call 路径
    # 断言：未闭合不泄漏 "secret"、已闭合保留正文、纯正文原样输出
    return {"data": {"all_ok": all(checks.values())}}
```

断言要**一次跑完**并返回 `{name: bool}` + `all_ok`，不要一条一个 code_runner。

## 参考

- skill/aicp/file-edit —— file_utils_api 各 action 的适用场景
- skill/aicp/aicp-base-edit —— 长驻服务改动的前置约定
- 相关文件：`runtime/_llm.py`
