---
name: app-locator
description: 快速定位 AICP 应用（后端插件路径、前端目录、路由）的最短步骤，含 cogitor.search 与 file_utils_api.search 用法与踩坑
tags: [aicp, locate, applications, cogitor]
---

# 应用定位速查

目标：给定应用名（如"翻译器""translator"），给出它的后端插件路径、前端目录和可调用路由。

## 最优路径（1 步）

```text
use_tool(target="builtins/tools/cogitor", action="search",
        plugin_args={terms: ["translator", "翻译"], limit: 5})
```

- `terms` 必须是**数组**，`limit` 必须是**整数**。传字符串或嵌套 dict 会报
  `slice indices must be integers or None or have an __index__ method`。
- 返回 `results[].name` 即 `use_tool` 可直接使用的 target，形如
  `applications/translator/translator`（目录路径去掉 `.py`，不看 `app.yaml` 的 id）。
- 用中文名也能命中，但要**同时**给英文目录名，否则容易 0 结果。

## 次优路径（1 步，纯文件检索）

```text
use_tool(target="builtins/tools/file_utils_api", action="search",
        plugin_args={path: "plugins/applications", pattern: "*translator*", max_results: 8})
```

## 兜底：目录遍历（3~4 步）

1. `list_dir path="plugins"` → 得到 `applications` / `builtins` / `os`
2. `list_dir path="plugins/applications"` → 得到全部项目目录名
3. `list_dir path="plugins/applications/<项目名>"` → 确认 `app.yaml` + `<name>.py`
4. `read_file path="plugins/applications/<项目名>/app.yaml"` → 读 `static_root` 定位前端

> 注意：应用目录在 `plugins/applications/`，**不是** `plugins/application/`（后者不存在）。

## 项目 ↔ 前端对应关系

- 后端：`plugins/applications/<项目名>/*.py`，路由 `applications/<项目名>/<receiver>`
- 前端：`www/apps/<项目名>/*`（HTML/CSS/JS），由 `app.yaml` 的 `static_root` 指定
- 同名即同一项目；仅有前端 = 纯单页应用，仅有后端 = 编排工具

## 补充信息获取

```text
use_tool(target="builtins/tools/contract_agent", action="get",
        plugin_args={plugin: "applications/translator/translator", auto_load: false})
```

返回该插件的 action 清单（translate / history / delete 等）与参数签名。
`auto_load: false` 只查契约不钉全文，省上下文。
