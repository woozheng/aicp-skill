---
name: plugin-action-smoke-test
description: 实测某个插件的全部 action（契约 → 逐个调用 → 读源码定位 Bug → 修复 → 磁盘验证 → 沉淀），用于「测试 X 插件」「测它的所有 action」类需求
---

# 插件 action 全量实测流程

目标：把一个插件的每个 action 真正跑通，而不是只读契约文档。

## 步骤

1. **查契约**
   `use_tool(target="builtins/tools/contract_agent", action="get", plugin_args={plugin: "<完整路径>", auto_load: false})`
   记录全部 action 名、必填参数、返回字段。

2. **逐个实测**
   一次只调一个 action，用 `use_tool(target="<插件路径>", action="<action>", plugin_args={...})`。
   优先测最可能出错的 action（通常 append / 写操作）。

3. **报错 → 读源码定位根因**
   `use_tool(target="builtins/tools/file_utils_api", action="read_file", plugin_args={path: "plugins/<路径>.py"})`
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

## 注意

- 一次只调一个工具，拿到结果再决定下一步。
- 同一错误连续 2 次 → 换思路或告知用户，禁止第 3 次重试。
- 写插件后热加载，下一轮即可调用新代码。

## 参考实例

`builtins/tools/add_task_board`：append 报 `_save_taskboard() takes 2 positional arguments but 3 were given`，根因是文件里有两份同名 `_save_taskboard`（3 参版本被 2 参版本覆盖）；同时 clear 分支未写文件导致假清空。修复 = 删掉重复定义 + clear 真正落盘写空。