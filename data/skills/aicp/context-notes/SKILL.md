---
title: 落盘上下文摘要
description: 多文件长任务把关键事实写入磁盘摘要（用户说「上下文被截断了」「记一下刚才读到的字段名」「keep notes across turns」时用），跨轮恢复不再重读原文
category: aicp
tags: [context, notes, summary, memory, workspace]
triggers: 上下文, 摘要, 落盘, 笔记, 记一下, 上下文丢失, 跨轮, 恢复上下文, 重复读, 多文件, context, summary, notes, memory, snapshot, checkpoint, digest, session, summarize
not_triggers: 会议记录, 日报, 周报, 读书笔记, 聊天记录, 待办清单
examples: "上下文被截断了 把刚才读到的字段名记下来", "这个活儿要跑好几轮 先把结论落盘免得下轮重读", "keep notes across turns so I don't reread the file", "这几个文件要一起改 别每次重读一遍"
version: 2.0
updated_at: 2026-11-05
---

# 落盘上下文摘要

### 为什么需要落盘

信息流只保留最近 90 条交互。大段文件内容（code_runner 打印、read_file 全文）会被压缩掉，导致三件事反复发生：

1. 「我上一轮明明读过」在下轮不成立 → 反复重读同一个文件
2. 回读时看到的旧内容已丢 → 误判「文件没落盘」→ 白干重写
3. 关键事实（字段名、CSS 色值、API 参数名）丢失 → 写出的代码对不上

**对策：不靠上下文记忆，靠磁盘摘要。**

## 何时用

- 任务需要分步骤读取 **3 个以上**文件（用户说「这些文件要一起改，别每次重读」时）
- 任务预计跨 5 轮以上工具调用（用户说「这个活儿要跑好几轮」时）
- 要改造/重写一个已存在的大文件，需先摸清多个依赖文件（用户说「改之前先把相关文件都看一下」时）
- 已经出现「重复读同一个文件」的现象（用户说「怎么又读了一遍这个」时）

**不适用**：会议记录；日报；周报；读书笔记；聊天记录；待办清单。这些是给人看的固定产物，不是为了跨轮恢复事实的工具——它们不要求「只记不可再生的推导结果」，也不需要 `session__任务名` 的固定文件名。

## 前置条件

- 插件：`builtins/tools/file_utils_api`（`write_file` / `append_file` / `read_file`）
- 插件：`builtins/tools/code_runner`（做定点截取与落盘校验）
- 目录：`data/workspace/notes/` 存在或可创建
- 本 skill 不需要 LLM，不涉及 `agent.llm` 兜底

## 核心步骤

### 1. 读 → 摘要 → 再读

每读完 **2-3 个**文件就立刻追加摘要，不要等所有文件读完再写。越早落盘越安全。

### 2. 只记「不可再生的推导结果」

**记**：字段名、API action 与参数名、鉴权方式、CSS 变量值、函数签名、行号锚点、文件路径、已确认结论、待办、下一步动作。

**不记**：大段源码原文（只记片段 + 行号范围，需要时按行号精确读回）。

### 3. 摘要文件位置与命名

**文件名必须带 session 与任务名，禁止裸 `notes.md`。**

| 场景 | 路径 |
|---|---|
| 单任务 | `data/workspace/notes/<session_id>__<任务名>.md` |
| 同一 session 多任务并行 | 同目录平铺，用任务名区分：`data/workspace/notes/<session_id>__<任务名>.md` |
| 跨 session 续做同一任务 | **沿用原文件名不变**，不要因为 session 变了就新建 |

命名细则：
- `<session_id>`：当前会话 ID（形如 `s_e9ffa5210f27`），取自**系统提示顶部**的 Session 字段（主控身份描述那一行：「你是 **BIO-1**，AICP 的主控。Session：`s_xxx`」）——自己的上下文里直接就有，不需要额外查询或调用插件
- `<任务名>`：短 slug，连字符分隔，如 `context-notes-skill`、`quadrant-board-fix`
- session 与任务名之间用 `__` 分隔，避免和任务名内部的连字符混淆
- 所有摘要平铺在 `data/workspace/notes/`，靠文件名前缀就能按 session / 任务定位

**为什么不用 `notes.md`**：裸名无法区分任务，事后检索只能逐个打开猜；带上 session 和任务名后，`ls` 一眼就知道每条摘要属于哪次会话的哪个任务，查找和清理都直接命中。

**只允许一个任务对应一个固定文件名，绝不叠加 `notes2.md` / `notes3.md` / `notes4.md`。**

原因（实测踩过）：叠加式摘要是「事实漂移」的根源。早期几份 notes 记的是被后续推翻的结论，与最新一份并存时，恢复上下文的人（或 agent）会读到过期版本，把已被推翻的结论当成事实继续用。写第 2 份的那一刻起，你就在给未来的自己埋雷。

正确姿势：
- 需要重写摘要时 → `write_file` **覆盖**同一个 `<session_id>__<任务名>.md`
- 只想在末尾追加新事实 → `append_file` 追加到**同一个**文件
- 并行多任务 → 靠任务名区分，同目录平铺，不要在同级叠编号
- 若发现已存在 `notes2.md` 这类历史文件 → 立刻合并进主文件（保留最终结论，标注被推翻项）后删除

写入用 `file_utils_api`：
- 新建 / 覆盖 → `write_file`（path, content）
- 增量 → `append_file`（path, content）

### 4. 冲突判定：覆盖还是追加

**同一小节内事实有冲突 → 必须覆盖，且在原位标注「已推翻」。**

判断规则：
- 新旧事实**指向同一字段 / 同一结论**，二者不可能同时为真 → 覆盖 + 标注
- 新事实是**新增维度**（旧结论仍然成立）→ 追加，不动旧条目
- 判断不了 → 按追加写，但标 `待确认`

标注格式（原小节内就近写，不要堆到文件末尾）：

```markdown
- 静态根目录 = `www/apps/{project}` — 已推翻：旧结论 `www/{project}` → 新结论 `www/apps/{project}`
```

覆盖时用 `edit_file`（find 唯一匹配 + replace），不要用 `append_file` 硬堆新版本 —— 追加会让同一字段出现两条互斥记录，恢复上下文时必然读错一条。

### 5. 摘要文件格式

```markdown
# <任务名> 摘要

session: s_e9ffa5210f27
status: in-progress

## 已确认事实
- 网关白名单 PUBLIC_PAGES = {'index.html','login.html','404.html'} — plugins/os/_gateway.py:14
- studio api 鉴权走 os/_auth，token 从 body.token 取 — plugins/builtins/studio/api.py:10

## 关键标识符
- 前端 token 键名：localStorage.aicp_token（cookie 同名）
- 主题色：--bg #08090A / --surface #16171C / --brand #5E6AD2

## 文件清单（已读）
- www/console.html — 2548 行，仿写样板，CSS 变量在 1-60 行
- www/index.html — token 弹窗已就位

## 待办 / 下一步
- [ ] 重写 web_studio.html 外壳
- [ ] node --check 验证
```

### 6. 跨轮恢复

新轮次开始、或察觉上下文可能被截断时：**先读摘要文件**恢复事实，再决定下一步。**不要直接重读原文件全文。**

### 7. 落盘校验只看差异点

写完文件后不要整篇回读，只查三样：字节数 / 行数 / 关键标记命中数。整篇回读会再次撑爆上下文，制造新一轮截断。

### 8. 大输出显式 retain

- `code_runner` 默认只保留 1 轮，下轮就没了 → 需要跨轮参考时显式传 `retain: 8~10`
- `file_utils_api` 读文件传 `retain: 5`
- **更好的做法**：用 code_runner 定点截取（只打印需要的行段 + 行号），绝不全文 dump

### 9. 生命周期：什么时候删，什么时候留

| 任务状态 | 动作 |
|---|---|
| 任务闭合（已交付） | 删除 `data/workspace/notes/<session_id>__<任务名>.md` |
| 任务未完，本轮内继续 | 保留，下轮直接读回 |
| 任务未完，要跨会话挂起 | 保留，并在文件头标 `status: in-progress` + 「下次从第 N 步继续」 |

摘要文件是临时产物，不是长期资产。但**未闭合的任务不要提前删** —— 删了等于把跨轮恢复的能力一起删掉。

## 判断规则

| 条件 | 动作 |
|---|---|
| 同一小节新旧事实互斥 | `edit_file` 覆盖旧条目 + 就近标注「已推翻」 |
| 新事实是新增维度，旧结论仍成立 | `append_file` 追加，不动旧条目 |
| 无法判断是否冲突 | 按追加写，并标 `待确认` |
| 同一任务已存在 `notes2.md` / `notes3.md` | 合并进主文件（保留最终结论），删除多余文件 |
| 摘要文件是否需要重建 | 同 session 同任务名 → 沿用原文件名，不新建 |
| 任务是否闭合 | 闭合 → 删除摘要；未闭合 → 保留并写 `status: in-progress` |
| 是否需要重新读原文 | 摘要中已有该事实 → 不读；只有片段/行号 → 按行号精确读回 |

## 踩过的坑

| 坑 | 现象 | 根因 | 修法 |
|---|---|---|---|
| 全文 dump 大文件到 code_runner | 输出挤掉前文，跨轮即失忆 | 未做定点截取 | 只打印需要的行段 + 行号 |
| 写完文件整篇回读 | 再次撑爆上下文，误判「没落盘」后重写 | 落盘校验用了全文读取 | 只查字节数 / 行数 / 关键标记命中数 |
| 依赖记忆里的字段名 / 色值 | 跨轮后写错，只能重新翻源码 | 关键事实未落盘 | 记入摘要「关键标识符」段 |
| 一轮里多次大输出 | 同轮互相挤掉，结论失真 | 未设 retain | `code_runner` 显式 `retain: 8~10` |
| 摘要里抄大段源码 | 摘要本身变成新的噪声源 | 未区分「可再生内容」与「推导结果」 | 只记片段 + 行号范围 |
| 叠加 `notes2.md` / `notes3.md` 多份摘要 | 旧结论与新结论并存，恢复上下文时读到已被推翻的版本 | 多份并存造成事实漂移 | 合并进主文件后删除多余文件 |
| 裸名 `notes.md` 不带 session 和任务名 | 事后无法检索，只能逐个打开猜 | 命名未区分任务 | 用 `<session_id>__<任务名>.md` |
| 事实冲突时用 append 堆新版本 | 同一字段两条互斥记录，恢复时必然读错一条 | 混淆「覆盖」与「追加」 | 用 `edit_file` 覆盖并标注「已推翻」 |
| 任务未闭合就删摘要 | 跨轮恢复能力归零，只能重读全部原文 | 误把摘要当长期资产/临时垃圾 | 未闭合保留并标 `status: in-progress` |

## 验证

用 `code_runner` 跑，骨架如下（所有逻辑含 import 写在函数内，返回 `{"data": ...}`；禁 print / time.sleep / input / while True / subprocess.Popen / 同步 requests 打本地 / eval / exec）：

```python
async def execute(envelop, agent):
    from pathlib import Path

    p = Path("data/workspace/notes/s_214fb26796d6__context-notes-skill.md")
    checks = {}
    if p.is_file():
        text = p.read_text(encoding="utf-8", errors="ignore")
        checks["has_session"] = "__" in p.name
        checks["has_status"] = "status:" in text
        checks["has_facts"] = "## 已确认事实" in text
        checks["has_todo"] = "## 待办" in text
        checks["no_sibling_numbering"] = not list(p.parent.glob("*__*.md")) or all(
            "__" in x.name for x in p.parent.glob("*__*.md"))
    else:
        checks["exists"] = False
    checks["all_ok"] = all(checks.values())
    return {"data": {"checks": checks}}
```

断言：
- 摘要文件名含 `__` 分隔的 session 与任务名
- 文件头有 `status:` 行
- 含 `## 已确认事实` 与 `## 待办 / 下一步` 两段
- 同目录不存在 `notes2.md` / `notes3.md` 这类叠加命名

## 参考

- 相关 skill：`skill/aicp/file-edit`（大文件定位与分片读写，本 skill 的上游）
- 相关 skill：`skill/aicp/aicp-base-edit`（本系统 skill 文件的基础写法）
- 相关插件：`builtins/tools/file_utils_api`（write_file / append_file / read_file）
- 相关插件：`builtins/tools/code_runner`（定点截取与落盘校验）
- 相关目录：`data/workspace/notes/`（摘要落盘位置）