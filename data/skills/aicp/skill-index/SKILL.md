---
title: 生成技能目录索引
description: 扫描 data/skills 生成技能索引（用户说「帮我盘点一下有哪些技能」「list all skills in a markdown index」时用），输出带元数据的清单
category: aicp
tags: [skill, index, inventory, automation, markdown]
triggers: 技能索引, 索引, 盘点, 清单, 有哪些技能, 生成索引, 技能列表, 扫一遍, catalog, index, inventory, list, skill, markdown, scan, skills, catalog, inventory, generate
not_triggers: 单个技能, 写技能, 新建技能, 删技能, 改技能
examples: "帮我看一下 data/skills 里到底有哪些技能", "给技能目录生成一份索引清单", "build a markdown index of all skills please", "列出所有 skill 及其大小"
version: 1.1
updated_at: 2026-11-05
---

# 生成技能目录索引

## 何时用

- 需要盘点 `data/skills` 里有哪些技能、每个是干什么的、多大（用户说「帮我盘点一下有哪些技能」时）
- 清理、迁移、重命名 skills 之前，先出一份可核对的清单（用户说「给技能目录生成一份索引清单」时）
- 目录会变，需要一份能反复刷新的索引，而不是一次性的手抄结果（用户说「list all skills in a markdown index」时）

**不适用**：只关心单个技能时直接读那个 SKILL.md；写技能内容；新建技能；删技能；改技能。

## 前置条件

- 需要插件：`builtins/tools/file_utils_api`（枚举路径、写文件）、`builtins/tools/code_runner`（批量解析与落盘）
- 需要本地 Python 3（脚本本体 `data/skills/aicp/skill-index/scripts/build_skill_index.py`）
- 输出目录 `data/workspace/` 需可写，脚本会自动创建
- 不需要 LLM：全流程是文件遍历与正则/PyYAML 解析，无模型依赖

## 核心步骤

1. **跑脚本（主路径）**。在项目根目录执行：

```bash
python data/skills/aicp/skill-index/scripts/build_skill_index.py
```

   输出 `data/workspace/skill_index.md`（覆盖写）。退出码 `0` 成功；`2` 表示找不到 skills 根目录或一条都没扫到（视为失败，不会静默产出空索引）。

2. **确认产出**。读回 `data/workspace/skill_index.md`，确认条目数、汇总表、分类速览齐全；条目数为 0 时按失败处理并查路径。

3. **需要非标准字段时手工走工具链**。首次生成，或需人工校准的一句话用途时：`use_tool(target="builtins/tools/file_utils_api", action="search", plugin_args={"path": "data/skills", "pattern": "**/SKILL.md"})` 枚举路径与字节数 → `use_tool(target="builtins/tools/code_runner", action="", plugin_args={"code": "async def execute(envelop, agent): ...", "packages": []})` 批量解析 frontmatter 与正文首段 → `use_tool(target="builtins/tools/file_utils_api", action="write_file", plugin_args={"path": "data/workspace/skill_index.md", "content": "..."})` 落盘并看 `validation.ok`。生成过一次之后一律跑脚本。

4. **重跑刷新**。目录新增或修改了 SKILL.md，重新执行第 1 步即可，索引可重复生成。

## 判断规则

| 条件 | 动作 |
|---|---|
| 只要全量清单 | 直接跑 `build_skill_index.py` |
| 需要人工校准的一句话用途 | 手工走工具链（步骤 3） |
| 只关心某一个技能 | 直接读该技能的 SKILL.md，不生成索引 |
| 脚本退出码 2 | 查 skills 根目录探测与 cwd，不要放行空索引 |
| 索引条目数与 `search` 枚举不一致 | 视为路径双源问题，先对齐路径再重跑 |

## 踩过的坑

| 坑 | 现象 | 根因 | 修法 |
|---|---|---|---|
| Markdown 校验失败 | 写入报「代码块未闭合」 | 正文里出现裸围栏符 | 描述一律用文字，不写裸围栏符 |
| 路径双源 | 索引条目数为 0 且无报错 | `file_utils_api` 返回绝对路径、`code_runner` 依赖进程 cwd 的相对路径 | 脚本用「cwd 优先 + 脚本自身 parents 回退」双探测，并对扫到 0 条直接判失败 |
| await subprocess.run | `TypeError: object CompletedProcess can't be used in 'await' expression` | 沙箱只把 `run`/`run_async` 包成异步，`subprocess.run` 本身是同步函数 | 用 `await asyncio.to_thread(subprocess.run, ...)` 包装 |
| 索引自指 | 索引里混进脚本副本 | 把脚本副本放在 `data/skills` 下 | 脚本本体只放本 skill 目录，不放副本 |
| 空索引成功 | 索引「生成成功」但条目为 0 | 未把 0 条当失败 | 条目数为 0 必须判失败退出码 2 |
| frontmatter 丢键 | fm_keys 全空、description 全空 | 自写 YAML 子集解析时 value 非空分支把 key 置空 | 优先用 PyYAML，失败退回正则 |
| 用途抽取噪声 | purpose 取到分隔符或表格行 | 启发式未跳过表格/引用/围栏 | 跳过代码围栏、竖线开头、尖括号开头、列表项，取第一段连续散文 |
| 表格被撑破 | Markdown 表格列错位 | 单元格内含竖线 | 表格单元格里的竖线一律替换成斜杠 |

## 验证

用 `code_runner` 跑，骨架必须包含 `async def execute(envelop, agent):`，所有逻辑（含 import）写在函数内，返回 `{"data": ...}`。禁用 print / time.sleep / input / while True / subprocess.Popen / 同步 requests 打本地 / eval / exec。

断言清单：

1. 索引条目数与 `file_utils_api.search` 枚举出的 SKILL.md 数量一致
2. 每个 skill_id 都在索引里出现
3. 索引 Markdown 中代码围栏成对出现
4. 汇总表、分类维度速览、生成链实录、踩坑清单、最脆弱一环五段齐全
5. 重跑脚本后输出内容稳定（条目数、顺序一致）

## 参考

- 相关 skill：`skill/aicp/skill-index`
- 相关插件：`builtins/tools/file_utils_api`、`builtins/tools/code_runner`
- 相关文件：`data/skills/aicp/skill-index/scripts/build_skill_index.py`
