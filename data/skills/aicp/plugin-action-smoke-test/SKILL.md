---
title: 实测插件全部 action
description: 实测某插件全部 action（用户说「测它的所有 action」「test all actions of this plugin」时用），逐个跑通并修掉假成功
category: aicp
tags: [plugin, contract, smoke, test, debug]
triggers: 实测, 测一遍, 全部action, 所有action, 逐个测试, 冒烟测试, 假成功, 插件报错, 回归测试, 全量实测, 调不通, action失效, contract, smoke, test, testing, debug, plugin, run all actions, action test
not_triggers: 小改动, 单行修改, 前端页面, 界面样式, 新建应用
examples: "帮我把 save_note 这个插件的所有 action 都测一遍", "测一下这个插件到底能不能用", "test all actions of the notes plugin, some of them return ok but do nothing", "这个插件的 append 一调就报参数错误，帮我定位"
version: 1.2
updated_at: 2026-11-05
---

# 实测插件全部 action

目标：把一个插件的每个 action 真正跑通，而不是只读契约文档。

## 何时用

- 需要确认某插件全部 action 是否真的可用（用户说「测它的所有 action」「test all actions of this plugin」时）
- 插件报出与参数/签名相关的错误（用户说「这个插件的 append 一调就报参数错误」时）
- 怀疑 action 返回 ok 但没落盘，即「假成功」（用户说「它说成功了但文件还是空的」时）
- 刚改完插件源码，需要全量回归（用户说「改完之后回归一遍全部 action」时）

**不适用**：小改动；单行修改；前端页面；界面样式；新建应用。

## 前置条件

- 插件源码位于 `plugins/**/{name}.py`，且契约可查（`builtins/tools/contract_agent`）
- 写操作类 action 需要该插件的默认落盘目录可写（通常 `data/{project}/`）
- 若需 LLM：先兜底 `if not agent.llm: return {"data": {"error": "LLM 未配置"}}`
- 修改源码前先 `read_file` 备份原片段

## 核心步骤

1. **查契约**

```python
use_tool(
    target="builtins/tools/contract_agent",
    action="get",
    plugin_args={"plugin": "<完整路径>", "auto_load": False},
)
```

记录全部 action 名、必填参数、返回字段。

2. **逐个实测**
   一次只调一个 action，用 `use_tool(target="<插件路径>", action="<action>", plugin_args={...})`。
   优先测最可能出错的 action（通常 append / 写操作）。

3. **报错 → 读源码定位根因**

```python
use_tool(
    target="builtins/tools/file_utils_api",
    action="read_file",
    plugin_args={"path": "plugins/<路径>.py"},
)
```

常见根因：

- 同一函数重复定义，后定义覆盖前定义 → 参数签名不匹配
- 分支只改返回值未落盘 → 「假成功」（例如 clear 只回 ok 但没写文件）
- action 未在 payload 里透传

4. **修复**
   先 `read_file` 再 `apply_patch`（`path` + `content` 为 Unified Diff）。确认返回 `validation.ok: true`。

5. **回归实测全部 action**
   记录每次的 `ok` / `size` / `message`，确认数值变化符合预期（append 后 size 变大、replace 后 size 重置）。

6. **磁盘验证**
   不信返回值，直接读落盘文件确认最终状态（尤其 clear 类操作）。

7. **沉淀**
   过程写回任务看板，收尾 `clear` 看板。

## 判断规则

| 条件 | 动作 |
|---|---|
| 契约里 action 列表与源码不一致 | 先修源码，再按源码重测全部 action |
| 报错是签名/参数不匹配 | 查是否有同名函数重复定义，删掉多余定义 |
| 返回 ok 但磁盘内容没变 | 判定为假成功，在该分支补落盘逻辑 |
| action 未出现在契约中 | 检查 payload action 是否透传到插件入口 |
| 同一错误连续 2 次 | 换思路或告知用户，禁止第 3 次重试 |
| 全部 action 返回 ok 且磁盘状态符合预期 | 结束实测，进入沉淀 |

## 踩过的坑

| 坑 | 现象 | 根因 | 修法 |
|---|---|---|---|
| 同名函数重复定义 | `append` 报 `_save_taskboard() takes 2 positional arguments but 3 were given` | 文件里有两份同名 `_save_taskboard`，3 参版本被 2 参版本覆盖 | 删掉重复定义，保留唯一正确版本 |
| clear 假成功 | clear 返回 ok，但文件内容仍在 | 分支只回 ok 未写文件 | clear 真正落盘写空 |
| 只读契约不实测 | 契约看着齐全，一调就报错 | 契约不反映真实实现 | 以实调结果为准 |
| 迷信返回值 | size 数字不变却以为成功 | 返回值与落盘状态解耦 | 磁盘验证最终状态 |
| 一次调多个 action | 结果互相污染，无法定位 | 并发/批量调用状态混叠 | 一次只调一个 action |

## 验证

改完插件后，用 `code_runner` 或逐个 `use_tool` 复测，并断言：

```python
async def execute(envelop, agent):
    from core import Envelop

    results = {}
    failed = []

    async def call(receiver, action, **kw):
        r = await agent.system.call(
            Envelop(receiver=receiver, payload={"action": action, **kw})
        )
        p = getattr(r, "payload", None) or {}
        st = getattr(r, "status", "") or ""
        if st:
            failed.append({"action": action, "status": st})
        return p

    for act, args in [
        ("append", {"text": "a"}),
        ("list", {}),
        ("clear", {}),
    ]:
        results[act] = await call("applications/demo/notes", act, **args)

    return {"data": {"results": results, "failed": failed}}
```

断言清单：

- 每个 action 返回无错误状态（`status` 为空）
- `append` 后 `size` 增大，`clear` 后 `size` 为 0
- `clear` 后直接读落盘文件，内容确为空文件
- 源码无重复函数定义（同一文件同名 `def` 只出现一次）
- `validation.ok` 为 true

沙箱禁令：禁 `print` / `time.sleep` / `input` / `while True` / `subprocess.Popen` / 同步 requests 打本地 / `eval` / `exec`。

## 参考

- 相关插件：`builtins/tools/contract_agent`、`builtins/tools/file_utils_api`、`builtins/tools/code_runner`
- 相关插件（任务看板）：`builtins/tools/add_task_board`
- 相关 skill：`skill/aicp/file-edit`
- 插件目录：`plugins/applications/`

## 注意

- 一次只调一个工具，拿到结果再决定下一步。
- 同一错误连续 2 次 → 换思路或告知用户，禁止第 3 次重试。
- 写插件后热加载，下一轮即可调用新代码。

## 参考实例

`builtins/tools/add_task_board`：append 报 `_save_taskboard() takes 2 positional arguments but 3 were given`，根因是文件里有两份同名 `_save_taskboard`（3 参版本被 2 参版本覆盖）；同时 clear 分支未写文件导致假清空。修复 = 删掉重复定义 + clear 真正落盘写空。