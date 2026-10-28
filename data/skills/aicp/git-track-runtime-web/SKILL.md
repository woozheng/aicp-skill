---
name: git-track-runtime-web
description: 把运行时生成目录从 gitignore 排除、把手写前端页面纳入版本控制的标准流程（gitignore 放行 + check-ignore 验证 + 暂存泄漏检查 + 提交）
---

# 把运行时目录排除、手写页面纳入 git

适用场景：`.gitignore` 里 `www/*` 之类的整目录忽略，导致手写页面（console / mcp / tools / system）无法回滚；需要放开顶层，但保留其中某个运行时子目录（如 `www/apps`）为忽略。

## 核心规则

1. **只忽略最深的运行时目录**，不要用 `dir/*` 粗粒度覆盖整个顶层——顶层一旦被忽略，子目录的 `!` 例外很难可靠生效。
2. **改动前先摸清体积**：monaco / vendor 之类第三方库可能十几 MB，提交前要让用户知道体积。
3. **暂存后必须查泄漏**：`git diff --cached --name-only | grep '被排除目录/'` 必须为空，才允许 commit。
4. **提交前不要 push**：除非用户明确要求，只 commit（可回滚），push 是不可逆操作。

## 标准步骤

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

## 坑

- **`git add` 会因 CRLF 刷一堆 warning**（`LF will be replaced by CRLF`）——这是 `core.autocrlf` 的正常提示，不影响提交，别当成错误。
- **`git check-ignore` 对已跟踪文件不输出**：文件一旦被跟踪，即使命中 ignore 规则也仍在版本库里；要让它退出跟踪必须 `git rm --cached`。
- **`.gitignore` 里 `!data/skills/` 这类例外**：git 规则是「最后一条匹配者生效」，所以例外必须排在忽略规则**之后**。
- **`data/*` + `!data/skills/` 组合**：如果 `data/` 目录本身被整体忽略，需要单独 `!data/skills/` 才能放行子目录内容。

## AICP 环境注意

- 仓库根 = `E:\aicp-skills`（`git rev-parse --show-toplevel`）。
- code_runner 里跑 git 必须**把 helper 定义在 `execute()` 内部**（模块级 async 函数在隔离沙箱里不可见，见 exp_007）。
- 沙箱里用 `asyncio.create_subprocess_exec` 而不是 `subprocess.run`（exp_004）。
- 运行时生成的 `data/*`、`plugins/applications/*` 本来就忽略；`data/skills/` 是要跟踪的资产。

## 关联

- 与 [[app-locator]] 配合：定位应用前端目录时先确认它在不在版本控制内。
