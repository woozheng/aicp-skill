---
title: 按规范编写 SKILL 文档
description: 按 data/skills/_STANDARD.md 写或改写 SKILL.md（用户说「写个 skill」「按规范改一下这个 skill」「standardize this skill」时用），保证 9 字段与 7 章节齐全
category: aicp
tags: [skill, writing, standard, documentation, authoring]
triggers: 写skill, 编写skill, 新建skill, 按规范改skill, skill规范, skill标准, 改写skill, 补齐skill字段, skill不合规, write skill, create skill, skill standard, standardize skill, rewrite skill, skill compliance
not_triggers: 批量校验skill, 单个文件修改, 前端页面美化, 插件动作自检
examples: "帮我按规范写一个 skill", "这个 skill 不合规 按标准改一下", "write a new skill following the standard"
version: 1.0
updated_at: 2026-11-05
---

# 按规范编写 SKILL 文档

## 何时用

- 需要新建一个 skill（用户说「帮我写个 skill」时）
- 需要把已有 skill 改写到合规状态（用户说「按规范改一下这个 skill」时）
- 某个 skill 检索不到、cogitor 搜不到它（用户说「这个 skill 搜不出来」时）
- 需要统一多个 skill 的字段写法（用户说「skill 都按同一个标准写」时）

**不适用**：批量校验skill（用 `skill/aicp/skill-lint`）；单个文件修改；前端页面美化；插件动作自检。

## 前置条件

- 规范文件：`data/skills/_STANDARD.md`（唯一权威标准，必须先读）
- 插件：`builtins/tools/file_utils_api`（读写 SKILL.md）
- 插件：`mytools/skill_lint`（校验产出）
- 目录约定：`data/skills/<category>/<skill-name>/SKILL.md`，路由 `skill/<category>/<skill-name>`
- 不需要 LLM；本 skill 不涉及 `agent.llm` 兜底

## 核心步骤

**步骤 1：读规范**

用 `file_utils_api.read_file` 读 `data/skills/_STANDARD.md` 全文。规范第 2/3 节管 frontmatter，第 4 节管正文 7 章节，第 6 节是 13 条校验标准，第 8 节是校验脚本。

**步骤 2：盘点原 skill（改写时）**

- 读目标 SKILL.md 全文
- 列出**必须保留**的实质内容：核心步骤、判断规则表、踩过的坑表、验证段断言——规范第 7.3 节明确这几段不动
- 记录原 `version` 与 `updated_at`，用于第 3.8 节的递增判断

**步骤 3：写 frontmatter**

按规范第 2、3 节写 9 个字段，顺序固定：

```yaml
---
title: <动词开头 ≤20字>
description: <≤120字，含 ≥1 个「」用户原话，中英各 1>
category: <等于路径第一级目录名>
tags: [<3-6个英文小写>]
triggers: <8-20个，中英各 40-60%，单行 CSV>
not_triggers: <2-8个相邻误召回词，单行 CSV>
examples: "<用户原话1>", "<用户原话2>"
version: <递增>
updated_at: <当天 YYYY-MM-DD>
---
```

- 所有列表字段必须**单行 CSV**，不能写 YAML 多行数组——cogitor 是简易 `k: v` 解析器，读不到多行数组
- 列表元素内不得含分隔符（`,` `，` `、` `;` `；`），原话含逗号就改成空格
- `triggers` 覆盖四类：用户口语 / 技术名词 / 数量单位 / 英文别名
- `not_triggers` 收单词或短词，不收泛词（「文件」「应用」「数据」一律不行）
- `examples` 是用户真会说的话，场景互不重合，中英各 ≥1

**步骤 4：写正文**

按规范第 4 节写 7 个二级标题，顺序固定：何时用 → 前置条件 → 核心步骤 → 判断规则 → 踩过的坑 → 验证 → 参考。

- 「何时用」每条后面补「用户说「X」时」
- 末尾 `**不适用**：...` 里的词必须逐个出现在 `not_triggers` 里（not_triggers ⊆ 不适用段）
- 「核心步骤」里的调用写成 `use_tool(target=..., action=..., plugin_args={...})`
- 「参考」段的 skill 路由必须真实存在，写之前先 `list_dir("data/skills/<category>")` 确认
- 「验证」段的代码骨架必须含 `async def execute(envelop, agent):`，所有 import 写在函数内，返回 `{"data": ...}`

**步骤 5：校验**

调用 `mytools/skill_lint` 扫这个文件，必须 `ok: true` 且 `errors` 为空：

```python
use_tool(
    target="mytools/skill_lint",
    action="scan",
    plugin_args={"path": "data/skills/aicp/<skill-name>/SKILL.md"},
)
```

`warns` 是建议项，可酌情修。规范第 6 节第 11/12 条（正文场景词覆盖、version 递增）脚本查不出来，**必须人工自检**。

**步骤 6：落盘**

用 `file_utils_api.write_file` 写回目标路径。`validation.ok` 为 false 时必须修完再继续。

**步骤 7：报告**

一行：`✅ <path> | triggers=N not=N examples=N`

## 判断规则

| 条件 | 动作 |
|---|---|
| 新建 skill | 跳过步骤 2，其余照做 |
| 改写已有 skill | 步骤 2 保留实质内容，只动 frontmatter 与「何时用」「不适用」「参考」 |
| frontmatter 动了任何字段 | version 必须 ≥ 原值；结构大改用 major（1.x → 2.0） |
| 正文已重写但 version 没动 | 按变更性质重新定级，不能照抄旧值 |
| 正文写了不存在 skill 的路由 | 删掉或改成真实路由，不能编 |
| 「不适用」段比 not_triggers 长 | 正常，not_triggers 是子集；反向则必须补齐 |
| lint 报 `triggers 中英比例失衡` | 补弱势语言触发词，英文写原形 |
| lint 报 `examples 场景高度重合` | 换成真正不同场景的原话 |
| lint 报 `参考段路由不存在` | 补建该 skill 或删掉该行 |
| lint 报 `description 无英文「」原话` | description 里补一个英文用户原话 |
| lint 报 `not_triggers 含泛词` | 换成具体动作词，如「生成」替「应用」 |

## 踩过的坑

| 坑 | 现象 | 根因 | 修法 |
|---|---|---|---|
| 列表字段写成多行 YAML | cogitor 搜不到这个 skill | 解析器是简易 `k: v`，读不到数组 | 改单行 CSV |
| elements 内含逗号 | 一条被拆成两条，计数错 | 原话本身有逗号 | 改写时用空格替代 |
| 不适用段与 not_triggers 只改一边 | 负信号失效，检索乱召回 | 没做双向对齐 | 两边逐词核对 |
| 照抄旧 version | 版本号与内容不符 | 抄了旧 frontmatter | 动过就递增，updated_at 用当天 |
| 编造参考段 skill 路由 | lint FAIL，链接指向不存在的 skill | 没查目录就写 | 先 `list_dir` 确认 |
| 为了过校验删掉实质内容 | skill 变得没用 | 把「校验通过」当成「内容正确」 | 核心步骤/判断规则/坑表/验证一律不动 |
| 一次改多个 skill | 改到第 3 个忘了前两个 | 批量无隔离 | 一次只改一个，改完 lint 确认 |
| 用行号定位改大文件 | 改错位置、留残留 | `replace_lines` 行号是读取时快照 | 改完一处重新读行号，或从大到小改 |

## 验证

用 `code_runner` 跑，骨架如下（所有逻辑含 import 写在函数内，返回 `{"data": ...}`；禁 print / time.sleep / input / while True / subprocess.Popen / 同步 requests 打本地 / eval / exec）：

```python
async def execute(envelop, agent):
    from core import Envelop

    r = await agent.system.call(
        Envelop(receiver="mytools/skill_lint",
                payload={"action": "scan",
                         "path": "data/skills/aicp/<skill-name>/SKILL.md"})
    )
    p = getattr(r, "payload", None) or {}
    data = p.get("data") or p
    checks = {
        "lint通过": bool(data.get("ok")),
        "无errors": not (data.get("items") or [{}])[0].get("errors"),
        "参考路由存在": "skill/aicp/skill-lint" in (data.get("items") or [{}])[0].get("path", ""),
    }
    return {"data": {"ok": all(checks.values()), "checks": checks}}
```

断言清单：
1. `mytools/skill_lint` 对该文件返回 `ok: true`
2. `errors` 为空数组
3. 写完后 `cogitor.search` 能搜到这个 skill（triggers 没被写坏）
4. 人工确认 version 已递增、updated_at 是当天

沙箱禁令：禁 `print` / `time.sleep` / `input` / `while True` / `subprocess.Popen` / 同步 `requests` 打本地 / `eval` / `exec`。

## 参考

- 规范文件：`data/skills/_STANDARD.md`（唯一权威标准）
- 相关插件：`builtins/tools/file_utils_api`、`builtins/tools/cogitor`、`mytools/skill_lint`
- 相关 skill：`skill/aicp/skill-lint`（批量校验入口）、`skill/aicp/file-edit`（文件修改方法论）、`skill/aicp/aicp-base-edit`（基础写法）
- 相关文件：`plugins/mytools/skill_lint.py`
