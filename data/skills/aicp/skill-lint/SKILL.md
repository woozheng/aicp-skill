---
title: 批量校验 SKILL 规范
description: 批量扫描 data/skills 下所有 SKILL.md 并按规范逐条检查（用户说「把 skill 都过一遍规范」「lint 一下 skill」时用），输出 FAIL/WARN 与修复建议
category: aicp
tags: [skill, lint, validate, quality, standards]
triggers: 批量校验skill, 规范检查, skill体检, 过一遍规范, 校验skill, 质量检查, skill不达标, lint skill, validate skill, batch lint, skill audit, check skills, skill compliance, lint all skills
not_triggers: 单个文件修改, 前端页面美化, 插件动作自检, 后端接口调试, 文件修改, 页面美化, 接口调试
examples: "把 data/skills 下所有 skill 都按规范过一遍", "批量校验一下 skill 目录有哪些不合规的", "lint 一下 skill 看看哪些字段缺了"
version: 1.1
updated_at: 2026-11-05
---

# 批量校验 SKILL 规范

## 何时用

- 需要一次性检查 `data/skills/**/SKILL.md` 是否符合 `data/skills/_STANDARD.md`（用户说「把 skill 都过一遍规范」时）
- 批量改写 skill 之前，先摸清哪些文件不合规（用户说「批量校验一下 skill 目录」时）
- 想知道某个 skill 缺哪些字段、triggers 数量是否达标（用户说「lint 一下 skill」时）
- 需要对单个 skill 做体检并给出可执行的修复清单（用户说「这个 skill 缺字段」时）

**不适用**：单个文件修改（直接用 `edit_file` 改）；前端页面美化；插件动作自检（用 `skill/aicp/plugin-action-smoke-test`）；后端接口调试；页面美化；接口调试。

## 前置条件

- 插件：`builtins/tools/code_runner`（跑校验脚本）
- 插件：`builtins/tools/file_utils_api`（读写 SKILL.md）
- 插件：`mytools/skill_lint`（本 skill 的执行入口）
- 规范文件：`data/skills/_STANDARD.md` 存在
- 不需要 LLM；本 skill 不涉及 `agent.llm` 兜底

## 核心步骤

**步骤 1：调用 lint 插件**

用 `use_tool(target=..., action=..., plugin_args={...})` 调用本插件，不要写成其它形式的伪函数。

- 全量扫描（不传 path）：

```python
use_tool(
    target="mytools/skill_lint",
    action="scan",
    plugin_args={},
)
```

- 只扫一个：

```python
use_tool(
    target="mytools/skill_lint",
    action="scan",
    plugin_args={"path": "data/skills/aicp/file-edit/SKILL.md"},
)
```

- 输出到文件（便于长结果留存）：

```python
use_tool(
    target="mytools/skill_lint",
    action="scan",
    plugin_args={"out": "data/workspace/skill-lint-report.json"},
)
```

**步骤 2：读懂返回结构**

```json
{
  "ok": true,
  "total": 12,
  "passed": 4,
  "failed": 8,
  "items": [
    {
      "path": "data/skills/aicp/file-edit/SKILL.md",
      "ok": false,
      "errors": ["triggers 5 个 < 8"],
      "warns": ["description 无英文「」原话"],
      "stats": {"triggers": 5, "not_triggers": 2, "examples": 3, "desc_len": 60}
    }
  ]
}
```

- `errors` = 违反硬约束，必须修
- `warns` = 建议修，不阻塞
- `stats` = 字段计数，供快速判断

**步骤 3：修 FAIL**

按 `errors` 逐条改，**一次只改一个 skill，改完重新 scan 确认再改下一个**。修法见下方「判断规则」表。

**步骤 4：报告**

一行一个：`✅ <path> | triggers=N not=N examples=N` 或 `❌ <path> | 原因`。

## 判断规则

| 条件 | 动作 |
|---|---|
| `缺字段 X` | 补 X，X 的取值范围见规范第 3 节 |
| `triggers N 个 < 8` | 补触发词，覆盖口语/技术/数量/英文四类 |
| `triggers N 个 > 20` | 砍到 20 以内，优先砍泛词 |
| `triggers 中英比例失衡` | 补弱势语言触发词（英文写原形） |
| `not_triggers N 个 < 2` | 补相邻误召回词（单词/短词） |
| `not_triggers N 个 > 8` | 砍到 8 以内 |
| `not_triggers 含泛词「X」` | 换成具体动作词，如「生成」替「应用」 |
| `examples N 个 < 2` / `> 4` | 增删到 2-4 条，保证中英各 ≥1 且场景不重合 |
| `examples 中文条目 0 < 1` | 补一条真实中文用户原话 |
| `正文缺章节: ## X` | 补该章节，标题必须是规范第 4 节的七个之一 |
| `章节顺序错` | 按 何时用→前置条件→核心步骤→判断规则→踩过的坑→验证→参考 重排 |
| `not_triggers 的「X」未出现在「不适用」段` | 在「不适用」段补上 X，或从 not_triggers 删掉 X |
| `参考段路由不存在` | 删掉该行或改成真实存在的路由 |
| `category 与路径不一致` | 以路径为准改 category |
| `description N 字 > 120` | 压缩，保留「」用户原话 |
| `warns: 含泛词` | 换具体词，泛词命中率低 |
| `warns: examples 场景高度重合` | 换一条不同场景的原话 |

## 踩过的坑

| 坑 | 现象 | 根因 | 修法 |
|---|---|---|---|
| 插件返回裸 dict | 调用方报「object has no attribute 'result'」 | 没按 envelop 约定改 `envelop.payload` 并 return envelop | 参照 `plugins/mytools/tts_speak.py` 的返回约定 |
| 一次改多个 skill | 改到第 3 个时忘了前两个的字段 | 批量改写无隔离 | 一次只改一个，改完 scan 确认 |
| 行号定位 | 改错位置、留下残留 | `replace_lines` 行号是读取时快照 | 改完一处重新读取行号，或从大到小改 |
| 列表字段写成多行 YAML | cogitor 解析器读不到，检索不到该 skill | 用了 YAML 数组语法 | 改单行 CSV，逗号分隔 |
| 元素内嵌分隔符 | 解析器把一条拆成多条 | 原话本身含逗号 | 改写时用空格替代逗号 |
| 「不适用」段与 not_triggers 双向不对齐 | 负信号失效 | 只改了其中一边 | 两边逐词对齐，not_triggers ⊆ 不适用段 |
| 把范本示例的 path 当真 | 扫的是错的文件 | 未替换脚本里的 path_str | 用 path 参数显式指定 |
| 校验脚本与规范不同步 | 判过但实际不合规 | 规范改了脚本没改 | 以规范第 6 节 13 条为准，脚本只作辅助 |
| 改写后忘记递增 version | 版本号与内容不符 | 只改了字段没想版本 | frontmatter 动过就必须递增，updated_at 用当天 |
| 参考段写了不存在的 skill 路由 | 校验 FAIL | 凭空编的路由名 | 写之前先 `list_dir("data/skills/aicp")` 确认 |

## 验证

用 `code_runner` 跑，骨架如下（所有逻辑含 import 写在函数内，返回 `{"data": ...}`；禁 print / time.sleep / input / while True / subprocess.Popen / 同步 requests 打本地 / eval / exec）：

```python
async def execute(envelop, agent):
    from core import Envelop

    r = await agent.system.call(
        Envelop(receiver="mytools/skill_lint", payload={"action": "scan"})
    )
    p = getattr(r, "payload", None) or {}
    items = p.get("items") or []
    checks = {
        "有扫描结果": len(items) > 0,
        "每项都带 path": all(it.get("path") for it in items),
        "passed_failed 存在": isinstance(p.get("passed"), int) and isinstance(p.get("failed"), int),
    }
    return {"data": {"ok": all(checks.values()), "checks": checks,
                      "passed": p.get("passed"), "failed": p.get("failed")}}
```

断言清单：
1. 返回的 `items` 非空，每项含 `path`
2. `passed` / `failed` 均为整数且 `passed + failed == len(items)`
3. 目标 skill 修完后单独 scan 该文件，返回 `ok: true` 且 `errors` 为空
4. 修改某个 SKILL.md 后，`cogitor.search` 仍能搜到该 skill（字段没被写坏）

沙箱禁令：禁 `print` / `time.sleep` / `input` / `while True` / `subprocess.Popen` / 同步 `requests` 打本地 / `eval` / `exec`。

## 参考

- 规范文件：`data/skills/_STANDARD.md`（唯一权威标准，含第 6 节 13 条校验标准与第 8 节校验脚本）
- 相关插件：`builtins/tools/code_runner`、`builtins/tools/file_utils_api`、`builtins/tools/cogitor`、`mytools/skill_lint`
- 相关 skill：`skill/aicp/skill-lint`（本 skill 的自检入口）、`skill/aicp/file-edit`（文件修改方法论）
- 相关文件：`plugins/mytools/skill_lint.py`
