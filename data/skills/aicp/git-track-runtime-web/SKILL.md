---
title: 跟踪前端页面并排除运行时目录
description: 把运行时生成目录排除出 git、把手写前端页面纳入版本控制（用户说「www 页面提交不了」「www/apps 不要提交」「commit the www pages」时用），含 check-ignore 验证与暂存泄漏检查
category: aicp
tags: [git, gitignore, frontend, version-control, commit]
triggers: gitignore, www 目录, 运行时目录, 提交前端页面, 提交不了, 没被跟踪, check-ignore, git add, git commit, 泄漏检查, 排除运行时, 手写页面, vendor, autocrlf, 跟踪文件, commit, ignore
not_triggers: 小文件, 单行修改, 简单替换, 分支合并, 冲突解决, 回滚历史
examples: "www 里的页面怎么提交不上去", "把 www/apps 这种运行时目录排除掉别跟着提交", "帮我把工作区改动提交一下但是不要 push", "the www pages are not tracked by git - fix the gitignore"
version: 1.1
updated_at: 2026-11-05
---

# 跟踪前端页面并排除运行时目录

## 何时用

- `.gitignore` 里 `www/*` 之类的整目录忽略，导致手写页面（console / mcp / tools / system）无法回滚（用户说「www 里的页面怎么提交不上去」「the www pages are not tracked by git」时）
- 需要放开顶层，但保留其中某个运行时子目录（如 `www/apps`）为忽略（用户说「把 www/apps 这种运行时目录排除掉别跟着提交」时）
- 工作区改动已写完，只需精确提交而不带上运行时生成物（用户说「帮我把工作区改动提交一下但是不要 push」时）
- 提交前发现 `git diff` 里有真 bug 或重复代码块，需要先修干净再进历史（用户说「提交前先看看 diff 有没有脏代码」时）

**不适用**：小文件（<500行）直接改写；单行修改；简单替换；分支合并与冲突解决；回滚历史。

## 前置条件

- 仓库根可用 `git rev-parse --show-toplevel` 定位（AICP 环境为 `E:\aicp-skills`）
- 在 code_runner 里跑 git 时，helper 必须**定义在 `execute()` 内部**（模块级 async 函数在隔离沙箱里不可见，见 exp_007）
- 沙箱里用 `asyncio.create_subprocess_exec` 而不是 `subprocess.run`（exp_004）
- 运行时生成的 `data/*`、`plugins/applications/*` 本来就忽略；`data/skills/` 是要跟踪的资产

## 核心步骤

### 1. 读现状

```bash
git ls-files www            # 当前跟踪了哪些
git check-ignore -v www/console.html
git status --short
```

`git check-ignore -v` 输出 `规则来源:行号\t路径`，能直接定位是哪一行规则在拦。

### 2. 改 .gitignore

把粗粒度规则换成细粒度：

```gitignore
# 改前
www/*
!www/.gitkeep
!www/index.html

# 改后（放行整个 www，只排除运行时子目录）
# 例外：www/apps 是运行时生成的应用目录，不跟踪、不提交
www/apps/
```

注意：排除规则写成 `www/apps/`（带斜杠 = 只匹配目录），比 `www/apps/*` 更稳。

### 3. 验证规则生效（必做）

对**手写页面**和**运行时目录**各取几个样本双向验证：

```bash
git check-ignore -v www/console.html       # 期望：NOT-IGNORED（无输出）
git check-ignore -v www/apps/todo/index.html  # 期望：命中 www/apps/ 规则
```

判定标准：`git check-ignore` 无输出即「未被忽略」。

### 4. 统计待提交内容

```bash
git ls-files --others --exclude-standard www    # 数量 + 清单
```

按目录汇总体积，若单目录过大（如 vendor/ > 10MB）先问用户是否纳入。

### 5. 暂存 + 查泄漏 + 提交

```bash
git add .gitignore www
git diff --cached --name-status | grep '^www/apps/'   # 必须为空
git diff --cached --stat | tail -1
git commit -m "chore: track www/ frontend pages (exclude runtime-generated www/apps)"
```

### 6. 提交后复核

```bash
git ls-files www/apps       # 期望：空
git ls-files www | wc -l    # 期望：跟踪文件数
git status --short
```

### 提交工作区已有改动（排除运行时产物）

#### A. 先摸清改动全貌

```bash
git status --short --ignored     # 已跟踪改动 + 被忽略的运行时目录
git diff --stat
git log --oneline -5
```

按三类分拣：

- **手写代码 / 文档** → 提交（`plugins/`、`www/` 手写页、`data/skills/`）
- **运行时产物** → 不提交（`data/*`、`plugins/applications/*`、`www/apps/*`、`*.contract.json`、`__pycache__`）
- **旧文件删除** → 先确认已迁移再提交删除（`git log --oneline -3 -- <path>` 看迁移历史）

#### B. 精确 stage，不要 `git add -A` 全仓

```bash
git add -A -- <明确的文件路径列表...>
git diff --cached --stat
# 泄漏检查，两条都必须为空
git diff --cached --name-only | grep -E '^(plugins/applications/|www/apps/)'
git diff --cached --name-only | grep '^data/' | grep -v '^data/skills/'
```

删除的文件也要写全路径才会被 stage。

#### C. 提交前先修掉「工作区里的脏代码」

`git diff` 常常直接暴露真 bug（重复代码块、变量先用后定义）。**提交前先修干净**，别把坏代码写进历史：

```bash
git diff -U0 -- <file>        # 精读 hunk
```

改完用 `py_compile` / `node --check` 过一遍语法，再进 commit。

#### D. 多行 commit message（约定式）

- 标题一行概括：`feat:` / `fix:` / `chore:` / `docs:` / `refactor:`
- 正文每行一个变更点，`- ` 开头，按「核心逻辑 → 次要修复 → 文档 → 清理」排序
- 清理类改动单独写一行 `chore: 删除已迁移到 xxx 的旧文件`
- **只 commit，不 push**（除非用户明确要求 push）

#### E. 复核

```bash
git log --oneline -3
git status --short              # 期望：空
```

## 判断规则

| 条件 | 动作 |
|---|---|
| 只忽略最深的运行时目录 | 用 `www/apps/` 这种带斜杠的细粒度规则，不要 `www/*` 粗粒度覆盖整个顶层 |
| 改动前发现 monaco / vendor 等第三方库体积大 | 先统计体积告知用户，单目录 > 10MB 先问是否纳入 |
| `git diff --cached --name-only` 命中被排除目录 | 禁止 commit，先修 `.gitignore` 或精确 stage |
| `git check-ignore` 对已跟踪文件无输出 | 文件已在版本库，要退出跟踪必须 `git rm --cached` |
| `.gitignore` 里存在 `!` 例外 | 例外必须排在忽略规则**之后**（最后一条匹配者生效） |
| `data/` 目录本身被整体忽略 | 需要单独 `!data/skills/` 才能放行子目录内容 |
| 用户未明确要求 push | 只 commit（可回滚），不 push |
| `git add` 输出 CRLF warning | 属 `core.autocrlf` 正常提示，不算错误 |
| `git diff` 暴露重复代码块或先用后定义 | 提交前先修干净，再进 commit |

## 踩过的坑

| 坑 | 现象 | 根因 | 修法 |
|---|---|---|---|
| 顶层整目录忽略 | `www/*` 让手写页面全部无法跟踪，子目录 `!` 例外不生效 | 顶层一旦被忽略，子级例外规则失效 | 改为只忽略最深的运行时目录 |
| `git add` 刷 CRLF warning | `LF will be replaced by CRLF` | `core.autocrlf` 的正常提示 | 忽略，不当成错误 |
| `check-ignore` 对已跟踪文件不输出 | 文件已跟踪，命中 ignore 规则仍在版本库里 | git 只对未跟踪文件生效 | `git rm --cached` 退出跟踪 |
| 例外规则不生效 | `!data/skills/` 排在忽略规则之前 | 最后一条匹配者生效 | 把例外移到忽略规则之后 |
| `data/*` + `!data/skills/` 仍为空 | `data/` 目录本身被整体忽略 | 目录级忽略会连带子目录 | 单独 `!data/skills/` 放行子目录内容 |
| `edit_file` 报 find 不匹配 | 换行 / 缩进 / CRLF 任一处不同 | 片段与原文不完全一致 | 先 `read_file` 看真实内容，必要时按精确片段替换 |
| 照 diff 推断代码顺序出错 | 改完逻辑错位 | `git diff` 的 hunk 顺序 ≠ 文件真实顺序 | 改之前按行号读一遍真实源码 |
| 新增块引入重复代码 | 同一段 `p = _resolve_path(...)` 出现两次 | diff 新增块未去重 | 用 `src.count(片段)` 快速定位 |
| `git add -A` 把运行时产物带进暂存 | 暂存区出现 `data/*`、`www/apps/*` | 全仓 stage 无排除 | 改为精确路径列表 + 泄漏检查 |

## 验证

用 code_runner 跑（沙箱内 helper 定义在 `execute()` 内部，用 `asyncio.create_subprocess_exec`）：

```python
async def execute(envelop, agent):
    import asyncio

    async def run(args):
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        return proc.returncode, out.decode("utf-8", "ignore")

    checks = {}
    _, out = await run(["git", "check-ignore", "-v", "www/console.html"])
    checks["console_not_ignored"] = (out.strip() == "")
    _, out = await run(["git", "check-ignore", "-v", "www/apps/todo/index.html"])
    checks["runtime_ignored"] = ("www/apps" in out)
    _, out = await run(["git", "diff", "--cached", "--name-only"])
    leaked = [ln for ln in out.splitlines()
              if ln.startswith("plugins/applications/") or ln.startswith("www/apps/")]
    checks["no_staged_leak"] = (len(leaked) == 0)
    _, out = await run(["git", "ls-files", "www/apps"])
    checks["runtime_untracked"] = (out.strip() == "")

    return {"data": {"checks": checks, "all_ok": all(checks.values())}}
```

断言清单：

1. `console_not_ignored` — 手写页面不被忽略
2. `runtime_ignored` — 运行时目录命中 `www/apps/` 规则
3. `no_staged_leak` — 暂存区无 `plugins/applications/` 与 `www/apps/` 路径
4. `runtime_untracked` — `git ls-files www/apps` 输出为空
5. 提交后 `git status --short` 为空

## 参考

- 相关 skill：`skill/aicp/file-edit`（改 `.gitignore` 前先用它确认编辑方式）、`skill/aicp/skill-index`、`skill/aicp/skill-lint`
- 相关插件：`builtins/tools/file_utils_api`、`builtins/tools/code_runner`、`builtins/tools/cogitor`
- 相关文件：`plugins/builtins/agents/main_agent.py`、`data/skills/_STANDARD.md`
- 关联技能：应用前端目录定位时先确认它是否在版本控制内（`plugins/applications/*` 默认被忽略）