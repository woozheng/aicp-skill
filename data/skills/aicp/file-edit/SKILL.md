---
title: 安全修改文件与代码
description: 用 file_utils_api 改文件/代码的方法论（用户说「改一下这个文件」「this file is too long」时用），含大文件分块定位、edit_file 精确匹配、越界静默 no-op 等坑
category: aicp
tags: [file, edit, code, bigfile, frontend]
triggers: 大文件, 改不动, 太长, 文件太大, 分片, 改文件, 改代码, 修改文件, 行号, 越界, 没生效, 3000行, bigfile, edit file, edit code, modify file, fix, chunk, split, replace
not_triggers: 单行修改, 简单替换, 新建, 生成
examples: "帮我把这个文件里的配置改一下", "把这个 3000 行的 index.html 的导航栏改一下", "edit_file 报 find 不匹配怎么办", "this file is too long, how to edit it"
version: 3.0
updated_at: 2026-11-05
---

# 安全修改文件与代码

## 何时用

- 需要改动已有文件的内容，且改动位置明确（用户说「帮我把这个文件里的配置改一下」时）
- 单个文件数百至数千行（前端 HTML/CSS/JS 或后端 Python），需要定位并修改某一小段（用户说「这个html太长了改不动」「this file is too long」时）
- `edit_file` 报 find 不匹配，需要换一种改法（用户说「edit_file 报 find 不匹配怎么办」时）
- 改完发现文件没变化、怀疑参数用法错了（用户说「改完但文件没变」时）

**不适用**：单行修改；简单替换；新建；生成。

## 前置条件

- 插件：`builtins/tools/file_utils_api`（读/写/定位）、`builtins/tools/code_runner`（批量脚本改造与校验）
- 无环境依赖，任何会话可用
- 不需要 LLM

## 核心步骤

1. **先定位**（定位成本在「读」，不在「改」）。文件 >200 行时先拿结构地图，**绝不 `read_file` 读全文**：

```python
# 方式 A：定义行号地图（Python/JS 函数与类）
use_tool(target="builtins/tools/file_utils_api", action="code_structure",
         plugin_args={"path": "plugins/x.py"})
# 方式 B：关键词带上下文（CSS 选择器、无结构文件的通用做法）
use_tool(target="builtins/tools/file_utils_api", action="search",
         plugin_args={"path": "plugins/x.py", "content": "def foo", "context": 3})
# 方式 C：精读某段
use_tool(target="builtins/tools/file_utils_api", action="read_file_lines",
         plugin_args={"path": "plugins/x.py", "start_line": 40, "end_line": 80})
```

   需要一次拿全的锚点：宿主容器（决定插入层级）、相关 CSS 选择器行、相关 JS 函数定义行、`</style>` / `</script>` 位置、`<head>` / `<body>` 边界。注意 `code_structure` 只给 JS/Python 函数行号，**CSS 块不给行号**，CSS 位置靠 `search(content=".类名")` + `read_file_lines` 找。

2. **改**，按场景选 action：

| 场景 | action | 关键点 |
|---|---|---|
| 散落多处、一次一处 | `edit_file` | find 必须是原文精确子串 |
| 删一段连续行 | `delete_lines` | `start_line` / `end_line` 必填 |
| 插入若干新行 | `edit_file` + `replace` 锚点 | 把锚点原文放进 `replace`，一次完成插入 |
| 大范围重构 | `write_file` | 返回 `validation.ok` |
| 跨文件批量正则 | `code_runner` 脚本 | 不要逐个手改 |

   `edit_file` 是唯一的「改」入口：`find` 取自最近一次读取的原文，`replace` 为新内容（`replace=""` 表示删除该片段）。插入就是「把锚点原文 + 新内容」一起作为 `replace`。

```python
use_tool(target="builtins/tools/file_utils_api", action="edit_file",
         plugin_args={"path": "www/xxx.html",
                      "find": "<div id=\"app\"></div>",
                      "replace": "<div id=\"app\"></div>\n<div class=\"panel\"></div>"})
```

3. **同一位置只改一次**。HTML + CSS + JS 三段若同一次需求涉及，在同一次调用里写完；必须分多次时，每次改完重新读取拿新锚点，禁止用旧锚点改第二次。

4. **回读确认**。改完 `read_file_lines` 复读目标区间，确认落位正确、无重复块；新类名 / 新函数名全文件只应有一处定义。返回成功 ≠ 改在对的位置。

5. **写完立刻看 `validation.ok`**。为 false 就当场修完，不留给下一轮，不带着语法错误往下走。

6. **交付** —— 给可点击 URL（`http://127.0.0.1:9000/apps/xxx/`）+ 说明改了什么与已知局限。

## 判断规则

| 条件 | 动作 |
|---|---|
| 文件 < 200 行、改动单处 | 直接 `read_file` + `edit_file` |
| 文件 > 200 行 | `code_structure` / `search(context=3)` 先定位，再 `edit_file` |
| 需删除连续代码块 | `delete_lines`（按行号区间） |
| 需在锚点处插入新行 | `edit_file`，`replace` = 锚点原文 + 新内容 |
| find 连续 2 次不匹配 | 重新 `read_file_lines` 取原文再拼 find，不要猜 |
| 同一处要改第二次 | 先 `read_file_lines` 拿新锚点，禁止用旧锚点 |
| 改动处 > 5 处 | 考虑 `write_file` 整体重写 |
| 跨多个文件 | `code_runner` 脚本批处理 |
| 改动同时含 HTML+CSS+JS | 同一次调用写完三段，禁止分多轮补 |
| 出现重复 CSS / JS 块 | `read_file_lines` 定范围 → `delete_lines` 删旧块 → 再读确认 |
| `validation.ok == false` | 立刻读回修，不带着语法错误往下走 |
| 需求含多状态联动 | 先列状态机（关闭/已打开过/从未打开/展开中）再写码 |
| 全局函数名可能撞名 | 先 `search(content="函数名")` 确认唯一再定义 |

## 踩过的坑

| 坑 | 现象 | 根因 | 修法 |
|---|---|---|---|
| 全量 write | tool_call 丢失 | 内容超 max_tokens | 改 `edit_file` 局部写 |
| 行号越界 | `delete_lines` 报范围错 | 行号来自旧读取，文件已变 | 重新读，拿新行号 |
| find 不匹配 | 报「find 不匹配（前 50 字…）」 | 手打而非抄原文 | 从读取输出里复制原文 |
| find 不唯一 | 报错或改错位置 | 锚点太短 | 扩展 find 加上下文 |
| 缺 path | 报「缺少 path 参数」 | 没带必填参数 | 每个 action 都要 `path` |
| 重复插入 CSS 块 | 规则从 12 行变 37 行，整页样式失效 | 同一处改两次又插一份 | `delete_lines` 删旧块，只留一份 |
| CSS 括号不配对 | 整块样式区失效 | 重复插入多了一个 `{` | 查 `validation.ok`，人工数括号 |
| JS 顶层重名 | 整段 script 失效，页面「完全没反应」 | `const`/`let` 顶层撞名 | 定义前 `search` 确认唯一 |
| 选择器冲突 | 新样式被旧规则覆盖 | 新类名已被占用 | `search(content="新类名")` 数出现次数 |
| 半成品交付 | 用户看到裸元素 / 按钮没反应 | HTML/CSS/JS 分多轮写 | 三段一次写完再交付 |
| 顺手重构 | 修 A 制造 B | 动了用户已验证可用的代码 | 只改用户点名的那几项 |
| 列表字段多行 YAML | cogitor 解析器读不到 | 简易 `k: v` 解析 | 改单行 CSV |

## 验证

```python
async def execute(envelop, agent):
    from pathlib import Path

    path = "data/skills/aicp/file-edit/SKILL.md"
    text = Path(path).read_text(encoding="utf-8")
    head = text.split("\n---", 1)[0]
    body = text[len(head) + 4:]

    checks = {
        "frontmatter_ok": text.startswith("---"),
        "has_triggers": "triggers:" in head,
        "has_not_triggers": "not_triggers:" in head,
        "has_user_quote": "「" in head,
        "no_yaml_list": "\n-" not in head,
        "sections_7": all(f"## {s}" in body for s in
                           ["何时用", "前置条件", "核心步骤", "判断规则",
                            "踩过的坑", "验证", "参考"]),
        "no_replace_lines": "replace_lines" not in text,
        "no_apply_patch": "apply_patch" not in text,
    }
    return {"data": {"all_ok": all(checks.values()), "checks": checks}}
```

写代码文件时断言写法提醒：CSS 断言不要用简单子串包含（`"height:100vh" in css` 会命中 `min-height:100vh`），用带边界正则 `(?<![-\w])height\s*:\s*100vh` 或断言完整片段。

## 参考

- 相关 skill：`skill/aicp/context-notes`（多文件上下文管理）、`skill/aicp/skill-lint`（批量校验 SKILL 规范）
- 相关插件：`builtins/tools/file_utils_api`、`builtins/tools/code_runner`
- 相关文件：`plugins/applications/_demo/`（应用 demo 前端与后端样例）
- 相关规范：`data/skills/_STANDARD.md`