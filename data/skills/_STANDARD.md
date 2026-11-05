============================================================
# SKILL 标准规范 v1
============================================================

本文件是 `data/skills/**/SKILL.md` 的唯一写作标准。所有 skill 必须满足本规范。
不符合的 skill 会被 `cogitor.search` 低命中或搜不到，等于不存在。

---

## 0. 一句话原则

**SKILL.md 不是给人读的说明书，是给检索器和 LLM 读的索引。**
正文负责「怎么做」，frontmatter 负责「什么时候被搜到」。

---

## 1. 文件位置与命名

    data/skills/<category>/<skill-name>/SKILL.md

- `<category>`：领域分类，英文小写，如 `aicp` / `software-development` / `mlops`
- `<skill-name>`：技能名，英文小写，用 `-` 连接，如 `frontend-bigfile-edit`
- 一个 skill 一个目录，目录里必须有 `SKILL.md`
- **路由名 = `skill/<category>/<skill-name>`**，可逆解析

---

## 2. frontmatter（必须）

文件必须以 `---` 开头，frontmatter 以 `---` 结束。9 个字段全部必需，顺序固定：

    ---
    title: <动词开头，≤20字>
    description: <≤120字，含 ≥1 个「」用户原话，中英各 ≥1>
    category: <与路径第一级目录名一致>
    tags: [<领域分类，3-6个，英文小写>]
    triggers: <用户原词，8-20个，中英各 40-60%，逗号分隔>
    not_triggers: <误召回相邻词，2-8个，逗号分隔>
    examples: "<用户原话1>", "<用户原话2>", "<用户原话3>"
    version: <1.0>
    updated_at: <YYYY-MM-DD>
    ---

### 2.1 列表字段格式（硬约束）

**所有列表字段必须用单行 CSV**，不要用 YAML 多行 `- item`。因为 cogitor 的 frontmatter 解析器是简易 `k: v` 解析，不支持多行数组。

    # ✅ 正确
    triggers: 大文件, 分片, 截断, 改不动, 3000行, chunk

    # ❌ 错误（解析器读不到）
    triggers:
      - 大文件
      - 分片

分隔符支持 `,` `，` `、` `;` `；`，任选一种，推荐半角逗号。

所有列表字段的元素内不得含分隔符（`,` `，` `、` `;` `；`）。如果用户原话本身含这些字符，改写时用空格替代或去掉，不要硬塞进 examples。这是解析器的能力边界——cogitor 是简易 `k: v` 解析，不支持元素内嵌分隔符。

### 2.2 examples 的引号写法

`examples: "a", "b", "c"` 这种写法的外层引号是字面字符，解析器会原样保留。这是已知的解析瑕疵，**不影响 LLM few-shot**（LLM 读原文，引号无感），也**不影响中英计数**（判定依据是含中文字符 / 含 ASCII 字母，引号不影响）。**接受现状，不为此改规范或改解析器**——代价远大于收益。

---

## 3. 字段写法规则

### 3.1 title

- 动词开头，≤20 字
- 一眼看出「做什么」，不是「是什么」
- ✅ `分片读写超大前端文件`
- ✅ `MCP 外部工具发现`
- ❌ `超大前端文件`（不是动作）
- ❌ `关于分片读写超大前端文件的技能`（废话）

### 3.2 description

结构：**「做什么」+（用户说「X」时用）+「解决什么」**

- ≤120 字
- **必须含至少 1 个「」包裹的用户原话**
- **中英各至少 1 个**（中文原话 + 英文原话，各用「」包裹）——description 服务 LLM 语义链路，中英混说都要能被语义命中
- 用户原话是用户真会说的话，不是说明书语言

- ✅ `分片读写超大前端文件（用户说「这个html太长了改不动」「this html is too long」时用），避免 max_tokens 截断`
- ❌ `当需要分片读写超大前端文件时使用，避免 max_tokens 截断`（无用户原话，检索命中率低）
- ❌ `用于处理大文件`（太泛）

### 3.3 category

- 必须等于 `data/skills/` 下的第一级目录名
- 例：`data/skills/aicp/xxx/SKILL.md` → `category: aicp`
- 不一致会被 cogitor 警告，且路由解析可能失败

### 3.4 tags

- 领域分类，英文小写，3-6 个
- 用于分类聚合，**不用于检索**（检索靠 triggers）
- ✅ `[frontend, file, edit, bigfile]`

### 3.5 triggers（检索主力，最重要）

**这是 cogitor.search 匹配的核心语料**。8-20 个，必须覆盖四类：

| 类型 | 例（bigfile-edit） |
|---|---|
| 用户口语 | 改不动、改不了、太长、卡 |
| 技术名词 | 分片、截断、max_tokens、chunk |
| 数量/单位 | 3000行、2000行、2MB、超过2000行 |
| 英文别名 | bigfile、chunk、timeout |

**四类覆盖是下限，不是上限**。数量/用户口语/技术名词这三类本身要中英各写一遍（见 3.5.1），最终 triggers 里英文词自然占 40-60%。不要只按"英文别名"这一类的字面意思理解。

**规则**：
- 宁多勿少（有 `not_triggers` 负信号兜底，误召回可控）
- 一个词一个条目，不要写短语（"改不动"而不是"文件改不动"）
- 中英文都要有（用户可能混说）
- **不要写泛词**（"文件"、"处理"、"数据"、"信息"——命中率低且噪声大，见第 6 节第 12 条的泛词清单）

#### 3.5.1 中英文混合

- **triggers 中英各写一遍**（cogitor.search 是词面匹配，中英不互通——中文触发词命中不了英文 query，反之亦然）
- **英文写原形**（`edit` 不写 `editing`/`edited`，`file` 不写 `files`）。**前提是 cogitor 检索层已实现 `_en_stem` 词形归一**。如果 cogitor 未实现（agent 改不了时），triggers 要覆盖常见变体：动词写原形 + ing（edit, editing），名词写单数 + 复数（file, files）。**优先推动 cogitor 侧补 `_en_stem`**，否则 triggers 会越写越多。
- **比例中英各 40-60%**（英文原形，不含工具名如 `replace_lines`）
- 中英同义触发词不必一一对应，但要覆盖同一意图的常见说法

### 3.6 not_triggers

- 2-8 个**最容易误召回的相邻场景词**（负信号宁多勿少，误降权可接受，误召回才致命）
- 命中时检索降权（负信号）
- 例：bigfile-edit 的 `小文件, 单行修改, 简单替换, 一行`

**怎么找**：问自己「用户在什么情况下会说类似的话，但**不该**用这个 skill？」

**必须是单词或短词，不要用短语**。例：「大文件分片」应拆成「大文件, 分片」。检索降权是 `term in corpus` 的子串匹配——短语作为整体时，用户单独说"分片"不会触发降权，负信号失效。

**禁止泛词**。不要写「项目」「应用」「内容」「写作文」「小结」「归类」「翻译」「文件」「处理」「数据」「信息」「文档」「文本」这类在大量场景都出现的词。判断方法：拿这个词去想，它在你系统里是不是 >3 个 skill 都会用到？如果是，说明它是泛词，不能进 not_triggers——误降权范围太大，得不偿失。

如果确实需要"组合场景"降权（如"用户在生成应用时"），用具体的动作词（「生成」「新建」），不要用泛名词（「应用」「项目」）。

**not_triggers 不是正文「不适用」段的镜像，必须做筛选**。它只收**真正构成负信号的词**：每个词都要在正文「不适用」段能找到依据（第 6 节第 5 条），但「不适用」段可以更长（它是对人说明边界的）。

### 3.7 examples

- 2-4 个**用户原话**，不是改写、不是说明书语言
- 从以下来源提取（按优先级）：
  1. 该 skill「验证」段里跑过的真实 case
  2. 真实对话日志
  3. 用户最可能的说法（合理推断）
- **中英各至少 1 条**（few-shot 对比，服务 LLM 语义链路）。判定口径：含中文字符即计入中文条目，含 ASCII 字母即计入英文条目，两类可重叠——中英混说的原话（如「edit_file 报 find 不匹配怎么办」）同时计入中英，这是真实用户说法，不该被排除。
- 格式：`examples: "原话1", "原话2", "原话3"`（单行 CSV，每个带引号）

**必须覆盖不同场景，不能是同一句话的同义改写**。4 条 examples 如果说的是同一件事，等于只有 1 条信息量。写完后自检：每两条 examples 描述的场景应该不同（读文件 vs 写文件 vs 调试报错 vs 跨轮恢复 等）。

**中英分布不能靠"混说"凑数**。中文条目必须真的是中文用户会说的话，英文条目必须真的是英文原话——不能把一条中文原话里塞几个英文单词就当英文条目，也不能把同一条中文场景翻译成英文当第二条英文示例。

- ✅ `examples: "把这个 3000 行的 index.html 的导航栏改一下", "读一下这个大文件的前 100 行", "this html is 3000 lines, split it please"`
- ❌ `examples: "分片读写大文件", "处理超大文件"`（说明书语言，不是原话）
- ❌ `examples: "帮我改一下文件", "帮我改一下这个文件", "fix this file", "edit this file"`（两条中文同义 + 两条英文同义 = 实际只有 1 个场景）

### 3.8 version / updated_at

- `version`：语义化版本。**保留原值或递增**——补字段/改描述属 minor（1.0 → 1.1），结构大改属 major（1.x → 2.0）。**不要照抄旧值**；如果原值缺失或明显伪造（如过去的 updated_at），按本次变更性质重新填。
- `updated_at`：**本次改写当天日期** `YYYY-MM-DD`。不要照抄旧值。

**改写必然伴随递增**。只要动了 frontmatter 任何一个字段，`version` 必须 ≥ 原值；若原值与实际内容量明显不符（如正文已重写但 version 仍是 1.0），按本次变更性质重新定级，并在报告里说明理由。`updated_at` 必须等于本次改写当天——出现几个月前的日期，说明这个字段是抄来的而不是更新的。

**自检**：写完后对照「本次改动的性质」和「原 version」两项，确认 version 递增且 updated_at 是当天。

---

## 4. 正文结构（必须，顺序固定）

frontmatter 之后是正文，必须包含以下 7 个二级标题，顺序不变：

    # <title>

    ## 何时用
    ## 前置条件
    ## 核心步骤
    ## 判断规则
    ## 踩过的坑
    ## 验证
    ## 参考

### 4.1 何时用

- 3-5 条触发条件
- **每条后面用「」补一个用户原话示例**
- 最后一段 `**不适用**：...`，列出的词必须与 `not_triggers` 一致（not_triggers 是「不适用」段的子集）

    ## 何时用

    - 目标文件 > 2000 行（用户说「3000行改一下」时）
    - 需要在超大文件中定位并修改某一小段（用户说「这个html太长了改不动」时）
    - 用户说「文件太大了一次改不完」「分块改一下」

    **不适用**：文件很小（<500行）直接用 edit_file；单行修改；简单替换。

### 4.2 前置条件

- 需要的插件（如 `builtins/tools/code_runner`）
- 需要的环境（如本机 Windows / 已装 Node）
- 需要 LLM 时先兜底：`if not agent.llm: return {"data": {"error": "LLM 未配置"}}`

### 4.3 核心步骤

- 步骤 1/2/3...，每步给命令或代码片段
- 说明：这步做什么、输出什么、什么情况下失败
- 代码片段用 ```python / ```bash 包裹
- **调用必须写成 `use_tool(target=..., action=..., plugin_args={...})`，禁止 `await tool(...)` 伪函数**

### 4.4 判断规则

用表格：

    | 条件 | 动作 |
    |---|---|
    | 满足 A | 走路径 1 |
    | 满足 B | 走路径 2 |
    | 都不满足 | 降级到 xxx |

### 4.5 踩过的坑

用表格：

    | 坑 | 现象 | 根因 | 修法 |
    |---|---|---|---|
    | 全量 write | tool_call 丢失 | 内容超 max_tokens | 用 edit_file |

### 4.6 验证

- 用 code_runner 跑，遵守沙箱契约
- 骨架必须包含：`async def execute(envelop, agent):`，所有逻辑（含 import）写在函数内，返回 `{"data": ...}`
- 沙箱禁令：禁 print / time.sleep / input / while True / subprocess.Popen / 同步 requests 打本地 / eval / exec
- 明确列出断言

### 4.7 参考

- 相关 skill：`skill/<category>/xxx`（必须真实存在）
- 相关插件：`builtins/tools/xxx`
- 相关文件：`plugins/xxx/yyy.py`

---

## 5. 正反例对照

### 完整示例（标杆）

    ---
    title: 分片读写超大前端文件
    description: 分片读写超大前端文件（用户说「这个html太长了改不动」「this html is too long」时用），避免 max_tokens 截断
    category: aicp
    tags: [frontend, file, edit, bigfile]
    triggers: 大文件, 分片, 截断, 太长, 改不动, 改不了, 3000行, 2000行, max_tokens, bigfile, chunk, 超大, html太大, 文件太大, 分块, 分批
    not_triggers: 小文件, 单行修改, 简单替换, 一行
    examples: "把这个 3000 行的 index.html 的导航栏改一下", "读一下这个大文件的前 100 行", "文件太大了一次改不完", "this html is 3000 lines, split it please"
    version: 1.2
    updated_at: 2026-11-05
    ---

    # 分片读写超大前端文件

    ## 何时用

    - 目标文件 > 2000 行（用户说「3000行改一下」时）
    - 需要在超大文件中定位并修改某一小段（用户说「这个html太长了改不动」时）
    - 用户说「文件太大了一次改不完」「分块改一下」

    **不适用**：文件很小（<500行）直接用 edit_file；单行修改；简单替换。

    （后续段落按 4.2-4.7 写）

### 常见错误

| 错误 | 为什么错 | 正确做法 |
|---|---|---|
| frontmatter 缺 triggers | 检索只能靠 description，命中率低 | 补 8-20 个 triggers |
| triggers 全是技术名词 | 用户说「改不动」时命中不了 | 加用户口语 |
| triggers 中英比例失衡 | 单一语言用户搜索命中率低 | 中英各 40-60% |
| examples 写成说明书语言 | 不是用户原话，few-shot 失效 | 从验证段/日志提取原话 |
| examples 场景重合 | 4 条说同一件事，等于 1 条 | 覆盖不同场景 |
| description 无「」原话 | 检索匹配缺用户语言锚点 | 嵌 ≥1 个「X」示例 |
| 列表字段用多行 YAML | cogitor 解析器读不到 | 改单行 CSV |
| category 与路径不一致 | 路由解析失败 | 以路径为准 |
| 正文「不适用」与 not_triggers 不一致 | 负信号失效 | 两边对齐 |
| triggers 写泛词（"文件"、"处理"） | 噪声大、误召回 | 换具体词 |
| not_triggers 写泛词（"应用"、"项目"） | 大范围误降权 | 换具体动作词 |
| 正文用 `await tool(...)` | 系统无此函数，会 NameError | 用 `use_tool(target=..., action=..., plugin_args={...})` |

---

## 6. 校验标准

改完的 SKILL.md 必须满足：

1. 9 个 frontmatter 字段全部存在且非空
2. `category` == 路径第一级目录名
3. `description` 长度 ≤ 120，含 ≥1 个「」，中英各 ≥1 个「」
4. `triggers` 8-20 个，中英词数各占 40-60%（英文原形，不含工具名）。**本条为中英比例的唯一权威阈值**——第 3.5.1 节只描述意图（为什么要中英混写），第 7.2 节只引用本条，脚本按本条判定。判定口径：中文条目 = 含中文字符；英文条目 = 不含中文字符且不在工具名清单（`replace_lines` 等）内。
5. `not_triggers` 2-8 个，且每个词都在「不适用」段能找到依据
6. `examples` 2-4 个，每个带引号，中英各 ≥1 条，且覆盖不同场景
7. 正文 7 个二级标题齐全且顺序正确
8. 「不适用」段的词全部出现在 `not_triggers` 里（not_triggers ⊆ 不适用段）
9. 「参考」段的 skill 路由真实存在（校验脚本扫目录检查）
10. `not_triggers` 不含泛词（应用、项目、内容、写作文、小结、归类、翻译、文件、处理、数据、信息、文档、文本）
11. `triggers` 必须覆盖正文「何时用」里描述的典型场景词——正文写了「3 个以上文件」，triggers 里就得有对应词
12. `version` ≥ 原值（frontmatter 有任何改动就必须递增），`updated_at` == 本次改写当天
13. 正文不得出现旧式伪函数 `await tool(...)`，调用必须写成 `use_tool(target=..., action=..., plugin_args={...})`

**第 5 条与第 8 条的关系**：not_triggers 是「不适用」段的**子集**（每个负信号词都要在正文有依据），不是镜像。「不适用」段可以更长（它是对人说明边界的）。

**第 11-12 条校验脚本查不出来**（脚本无正文语义、无历史版本），必须人工逐条自检，并在报告里说明。

---

## 7. 写作工作流（给 agent 执行）

当被要求「按标准改写现有 skill」或「按标准新建一个 skill」时，对每个 SKILL.md 执行：

### 7.1 读
- 读本规范（`data/skills/_STANDARD.md`）
- 改写：读目标 SKILL.md 全文
- 新建：跳过

### 7.2 写 frontmatter
按第 2、3 节写 9 个字段。重点：
- `description` 嵌 ≥1 个「」用户原话，中英各 1
- `triggers` 覆盖「口语 + 技术 + 数量 + 英文」四类，中英各 40-60%
- `not_triggers` 填相邻误召回词，不写泛词
- `examples` 是用户原话，中英各 ≥1 条，场景不重复

### 7.3 写正文
- 改写已有 skill：按第 4 节检查 7 个章节，**核心步骤 / 判断规则 / 踩过的坑 / 验证段不动**；「何时用」每条补「用户说「X」时」；「不适用」与 `not_triggers` 对齐；「参考」补相关 skill 行（确认路由存在）
- 新建 skill：按第 4 节写全 7 个章节

### 7.4 校验
跑第 8 节的校验脚本，必须无 FAIL 才落盘。

**如果历史 skill 因旧问题卡住，先修旧问题再落盘，不要为了过校验而临时改规范或降低标准。**

**脚本不可用时（code_runner 超时 / 被沙箱拦截 / 环境不允许执行）**：按第 6 节 13 条标准**逐条人工核对**，在报告里标注「非脚本输出，人工核对」。不要因为跑不了脚本就跳过校验。

### 7.5 落盘
用 `file_utils_api.write_file` 写回原路径。

### 7.6 报告
一行：`✅ <path> | triggers=N not=N examples=N`
失败：`❌ <path> | 原因`

**报告必须与实际文件一致**。不要写「挡掉了 X」除非 X 真的在 not_triggers 里且粒度正确（单词/短词，非短语）；不要写「改了 Y」除非 Y 真的改了。校验通过 ≠ 内容正确，报告前逐字段自检 frontmatter。

### 7.7 约束
- **一次只改一个，改完校验通过再下一个**。原因：`replace_lines` / `delete_lines` 的行号是**上次读取时的快照**，不是实时文件状态——前一处插入或删行会让后面所有行号整体偏移，导致改错位置或留下残留。
  - 改完一处后**必须重新 `read_file_lines` 取新行号**，再改下一处；
  - 或按行号**从大到小**依次修改（先改后面的，偏移不影响前面的行号）；
  - 遇到「锚点校验失败」= 行号已漂移，**重新读取**后再改，不要硬改、不要原样重试。
- 不改 category 值
- 所有列表字段用单行 CSV
- 失败不重试，记录原因继续
- 全部完成后汇总：总数 / 成功 / 失败清单

---

## 8. 校验脚本

agent 用 code_runner 跑，把 `path_str` 替换成目标路径。

**代码块围栏写法要求**：下方脚本块的围栏必须写成顶格的三个反引号 + `python`（无缩进、无空格），闭栏同样顶格。读取本规范提取脚本时用正则 `re.findall(r'```python\n(.*?)```', text, re.S)`——若写成缩进围栏，该正则提取不到任何内容。

```python
async def execute(envelop, agent):
    import re
    from pathlib import Path

    path_str = "data/skills/aicp/frontend-bigfile-edit/SKILL.md"  # ← 替换
    REQUIRED = ["title","description","category","tags","triggers",
                "not_triggers","examples","version","updated_at"]
    SECTIONS = ["何时用","前置条件","核心步骤","判断规则","踩过的坑","验证","参考"]
    TOOLNAMES = {"edit_file","replace_lines","write_file","read_file",
                 "read_file_lines","delete_lines","apply_patch","code_runner",
                 "file_utils_api","validation","list_dir","search",
                 "code_structure","mkdir","file_exists","file_stat"}
    VAGUE = {"应用","项目","内容","写作文","小结","归类","翻译",
             "文件","处理","数据","信息","文档","文本"}

    f = Path(path_str)
    if not f.is_file():
        return {"data": {"ok": False, "errors": [f"文件不存在: {path_str}"]}}

    text = f.read_text(encoding="utf-8", errors="ignore")
    errors, warns = [], []

    if not text.startswith("---"):
        return {"data": {"ok": False, "errors": ["无 frontmatter"]}}
    end = text.find("\n---", 3)
    if end == -1:
        return {"data": {"ok": False, "errors": ["frontmatter 未闭合"]}}
    fm = text[3:end]
    body = text[end + 4:]

    meta = {}
    seen_keys = {}
    for line in fm.splitlines():
        if ":" in line and not line.strip().startswith("#"):
            k, v = line.split(":", 1)
            k = k.strip().lower()
            if k in seen_keys:
                warns.append(f"frontmatter 重复键: {k}")
            seen_keys[k] = True
            meta[k] = v.strip().strip("\"'")

    def split_csv(s):
        return [x.strip() for x in re.split(r"[,，、;；]", str(s or "")) if x.strip()]

    for k in REQUIRED:
        if k not in meta or not str(meta[k]).strip():
            errors.append(f"缺字段 {k}")

    if "data/skills/" in f.as_posix():
        path_cat = f.as_posix().split("data/skills/")[1].split("/")[0]
        if meta.get("category") and meta["category"] != path_cat:
            errors.append(f"category={meta['category']} 与路径 {path_cat} 不一致")

    desc = meta.get("description", "")
    if len(desc) > 120:
        errors.append(f"description {len(desc)} 字 > 120")
    if "「" not in desc:
        errors.append("description 无「」用户原话")
    if not re.search(r"「[^」]*[\u4e00-\u9fff][^」]*」", desc):
        warns.append("description 无中文「」原话")
    if not re.search(r"「[^」]*[a-zA-Z][^」]*」", desc):
        warns.append("description 无英文「」原话")

    tr = split_csv(meta.get("triggers", ""))
    if len(tr) < 8:
        errors.append(f"triggers {len(tr)} 个 < 8")
    elif len(tr) > 20:
        errors.append(f"triggers {len(tr)} 个 > 20")

    def is_cn(s):
        return bool(re.search(r"[\u4e00-\u9fff]", s))
    cn = [t for t in tr if is_cn(t)]
    en = [t for t in tr if not is_cn(t) and t.strip() not in TOOLNAMES]
    total = len(cn) + len(en)
    if total:
        cn_pct = round(len(cn) * 100 / total)
        if cn_pct < 40 or cn_pct > 60:
            errors.append(f"triggers 中英比例失衡: cn={len(cn)}, en={len(en)}, cn_pct={cn_pct}% (要求各 40-60%)")

    for t in tr:
        if t in VAGUE:
            warns.append(f"triggers 含泛词「{t}」：命中率低且噪声大，建议换具体词")

    nt = split_csv(meta.get("not_triggers", ""))
    if len(nt) < 2:
        errors.append(f"not_triggers {len(nt)} 个 < 2")
    elif len(nt) > 8:
        errors.append(f"not_triggers {len(nt)} 个 > 8")
    for w in nt:
        if w in VAGUE:
            errors.append(f"not_triggers 含泛词「{w}」：泛词会大范围误降权，比漏写更糟")

    ex = split_csv(meta.get("examples", ""))
    if len(ex) < 2:
        errors.append(f"examples {len(ex)} 个 < 2")
    elif len(ex) > 4:
        errors.append(f"examples {len(ex)} 个 > 4")
    ex_cn = [e for e in ex if re.search(r"[\u4e00-\u9fff]", e)]
    ex_en = [e for e in ex if re.search(r"[a-zA-Z]", e)]
    if len(ex_cn) < 1:
        errors.append(f"examples 中文条目 {len(ex_cn)} < 1")
    if len(ex_en) < 1:
        errors.append(f"examples 英文条目 {len(ex_en)} < 1")

    def sig(s):
        return set(re.findall(r"[\u4e00-\u9fff]|[a-zA-Z0-9]", s.lower()))
    for i in range(len(ex)):
        for j in range(i + 1, len(ex)):
            a, b = sig(ex[i]), sig(ex[j])
            if not a or not b:
                continue
            overlap = len(a & b) / min(len(a), len(b))
            if overlap >= 0.7:
                warns.append(f"examples 第{i+1}与第{j+1}条场景高度重合({overlap:.0%})，应换成不同场景")

    positions = []
    for sec in SECTIONS:
        idx = body.find(f"## {sec}")
        if idx == -1:
            errors.append(f"正文缺章节: ## {sec}")
        else:
            positions.append((sec, idx))
    for i in range(1, len(positions)):
        if positions[i][1] < positions[i-1][1]:
            errors.append(f"章节顺序错: ## {positions[i][0]} 应在 ## {positions[i-1][0]} 之后")

    m = re.search(r"\*\*不适用\*\*[：:]\s*(.+)", body)
    if m:
        for w in nt:
            if w and w not in m.group(1):
                errors.append(f"not_triggers 的「{w}」未出现在「不适用」段")

    refs = re.findall(r"skill/[a-z0-9_-]+/[a-z0-9_-]+", body)
    if refs:
        skills_root = Path("data/skills")
        existing = set()
        for sf in skills_root.rglob("SKILL.md"):
            rel = sf.parent.relative_to(skills_root).as_posix()
            existing.add("skill/" + rel)
        for r in refs:
            if r not in existing:
                errors.append(f"参考段路由不存在: {r}")

    for mt in re.finditer(r"(?<![_\w])tool\s*\(", body):
        start = mt.start()
        if start >= 5 and body[start-5:start] == "use_t":
            continue
        warns.append("正文出现旧式伪函数 tool(...)，建议改用 use_tool(target=..., action=..., plugin_args={...})")
        break

    return {"data": {
        "ok": len(errors) == 0,
        "errors": errors,
        "warns": warns,
        "stats": {"triggers": len(tr), "not_triggers": len(nt),
                  "examples": len(ex), "desc_len": len(desc)},
    }}

```

---

---

## 9. 改写前的备份

批量改之前先备份：

    async def execute(envelop, agent):
        import shutil
        from pathlib import Path
        from datetime import datetime
        src = Path("data/skills")
        dst = Path(f"data/skills_backup_{datetime.now():%Y%m%d_%H%M%S}")
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        return {"data": {"ok": True, "backup": str(dst)}}

改坏了：`shutil.rmtree(data/skills) + copytree(backup, data/skills)` 回滚。
