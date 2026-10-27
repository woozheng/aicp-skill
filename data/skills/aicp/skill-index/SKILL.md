---
name: skill-index
description: 扫描 data/skills 下所有 SKILL.md，生成一份带元数据（skill_id、分类、标题、一句话用途、字节数、行数、相对路径）的 Markdown 索引，输出到 data/workspace/skill_index.md。附带可重跑的脚本，一条命令即可刷新。适用于给技能目录建索引、盘点现有 skills、迁移或清理 skills 前先出核对清单。
tags: ["索引", "skills", "元数据", "盘点", "自动化"]
metadata:
  version: "1"
---

# 技能索引生成器

## 何时用

- 需要盘点 `data/skills` 里有哪些技能、每个是干什么的、多大。
- 清理、迁移、重命名 skills 之前，先出一份可核对的清单。
- 目录会变，需要一份能反复刷新的索引，而不是一次性的手抄结果。

## 一条命令

在项目根目录执行：

    python data/skills/aicp/skill-index/scripts/build_skill_index.py

输出：`data/workspace/skill_index.md`（覆盖写）

退出码：`0` 成功；`2` 表示找不到 skills 根目录，或一条都没扫到（视为失败，不会静默产出空索引）。

## 索引包含什么

每个技能一节：skill_id、分类、标题、一句话用途、字节数与行数、相对路径，以及 frontmatter name、正文 H1、版本、tags（存在时）。文件末尾附汇总表、分类维度速览、生成链实录与踩坑清单。

## 抽取规则（为什么这么写）

1. frontmatter 优先用 PyYAML，失败退回正则；额外覆盖「分隔符与键值写在同一行」的内联写法。
2. 标题回退链：frontmatter title → frontmatter name → 正文 H1 → 目录名。
3. 一句话用途：frontmatter 的 description 或 summary 优先；没有就取正文第一段连续散文，并且必须跳过代码围栏、以竖线开头的表格行、以尖括号开头的引用行和列表项。
4. 表格单元格里的竖线一律替换成斜杠，否则 Markdown 表格会被撑破。

## 已知坑

- **Markdown 校验**：正文里直接写裸围栏符会被校验器判为「代码块未闭合」。描述一律用文字，不出现裸围栏符。
- **路径双源**：`file_utils_api` 返回绝对路径，`code_runner` 依赖进程 cwd 的相对路径。两者不同源就会静默扫到空目录。脚本用「cwd 优先 + 脚本自身 parents 回退」双探测兜底，并对「扫到 0 条」直接判失败。
- **沙箱里 await subprocess.run**：沙箱只把 `run` 与 `run_async` 包成异步，`subprocess.run` 本身仍是同步函数，await 它会直接抛 TypeError。用 `await asyncio.to_thread(subprocess.run, ...)` 包装。
- **别在 data/skills 里放脚本副本**：会污染被扫描目录并造成索引自指。脚本本体就放在本 skill 目录下，但它不是 SKILL.md，不影响扫描。
- **条目数为 0 必须当失败**：否则会生成一份「成功但空」的索引，掩盖路径错误。

## 什么时候不用脚本

首次生成，或需要非标准字段（例如人工校准的一句话用途）时，手工走工具链：`file_utils_api.search` 枚举路径与字节数，`code_runner` 批量解析 frontmatter 与正文首段，`file_utils_api.write_file` 落盘并看校验结果。生成过一次之后就一律跑脚本。

## 验证

跑完后确认：条目数与 `search` 枚举结果一致；每个 skill_id 都在索引里出现；代码围栏成对；汇总表、生成链实录、踩坑清单、最脆弱一环四段齐全。
