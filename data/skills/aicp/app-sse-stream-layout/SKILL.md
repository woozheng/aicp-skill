---
name: app-sse-stream-layout
description: 造「LLM 流式 + 可编辑输出 + 历史存档 + 左右布局 + 分页预览 + 统计分区」类应用的通用套路与验证方法
category: aicp
---

# SSE 流式应用 + 分区布局：可复用套路

## 何时用
用户要求「LLM 流式输出 / 结果可编辑可复制可下载 / 自动存历史可调回 / 界面左右布局 / 分页预览 / 底部统计」这类组合需求。

## 文件三件套
| 文件 | 作用 |
|---|---|
| `plugins/applications/{project}/{plugin}.py` | 后端（`async def execute(envelop, agent)`） |
| `plugins/applications/{project}/app.yaml` | `id` = project 名 = `static_root` 的目录名 |
| `www/apps/{project}/index.html` | 前端单页 |

路由 = 目录路径；前端 `const PLUGIN = "{plugin}"`，与后端文件名一致，三处对齐。

## 后端套路
- `envelop.payload = {ok: True, data: {...}}` 返回普通结果。
- **SSE**：把 `envelop.payload` 赋成一个 async generator，每次 `yield "data: " + json.dumps({...}) + "\n\n"`。
  事件类型建议：`delta`（增量文本）/ `done`（含 record + stats）/ `error`。
- 存档策略二选一：**自动**（流式结束前把记录写入 `data/{project}/history.json`，`_load/_save` 拆函数）或**手工**（流式只回 `stats`，前端点「存为历史」再调 `history_add`）。用户嫌自动存档会产生垃圾数据时用后者；此时后端 `stream` 分支不要调用 `_add_record`。
- 历史接口：`history_list`（返回精简 brief，含 stats/lang/style）、`history_get`、`history_add`、`history_update`、`history_delete`、`history_clear`。
- 导出：`save_md` 落盘到 `www/apps/{project}/exports/`，返回 `{filename, path, url}`，前端 `<a download>` 下载。
- `meta` 返回语种/风格等选项字典。

## 前端布局套路
```
header
├── .main  (grid: 1fr 1.3fr)     ← 左输入 / 右结果+预览
│   ├── ① 输入原文 (textarea + 语种/风格 + 开始按钮)
│   └── ② 整理结果 (textarea 可编辑 + 复制/下载/存档/清空 + MD 分页预览)
├── ③ 功能统计 (.stat-grid 四个分区块)
│   ├── A 本次整理压缩对比（字数/字符减少/压缩比/双色条）
│   ├── B 历史总览（总条数/累计字数/平均压缩比）
│   ├── C 按语种分布（横向条形）
│   └── D 按结构风格分布（横向条形）
└── ④ 历史存储记录（列表 + 载入/删除 + 清空）
```
- 统计全部从 `history_list` 的 stats 聚合，历史刷新时一起重渲染。
- **MD 渲染**：自写 mini renderer（标题/列表/引用/表格/代码块/分隔线/行内 code·粗斜体·链接），先 HTML 转义再拼标签。
- **分页**：按行切页 `PAGE_SIZE`（20/30/50/100 可选），`PAGE` 变量 + 上/下一页按钮 + `第 x / y 页 · 共 n 行`，翻页/改页长后重渲染。
- 流式 delta 到达时即时 `renderPreview()`，避免整页刷新。
- 结构风格下拉用**短名**（标准结构/提纲/摘要/长文），长描述放 `title` 属性，否则分布统计标签会很长。

## 验证方法（必须做）
用 `code_runner` + `urllib` 直接打网关：

```python
async def execute(envelop, agent):
    import json, urllib.request          # ← import 必须写在函数内
    out = {}
    req = urllib.request.Request("http://127.0.0.1:9000/apps/{project}/")
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read().decode("utf-8", "ignore")
    out["page_status"] = r.status
    out["input_before_output"] = body.find('id="input"') < body.find('id="output"')
    # POST 接口
    data = json.dumps({"action": "meta"}).encode()
    req = urllib.request.Request("http://127.0.0.1:9000/api/applications/{project}/{plugin}",
                                 data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        out["meta_ok"] = json.loads(r.read().decode()).get("ok")
    return {"data": out}                 # ← 必须 return {"data": ...}，不能 return envelop
```

- JS 语法校验：把 `<script>` 内容写到 `data/workspace/x.js`，`subprocess.run(["node","--check", p])`，`returncode == 0` 才算过。
  **不要用括号计数判断**——正则字面量里有 `(` `)` `[` `]`，会误报。
- SSE 校验：读流拼接 delta 文本，与 `done` 里的 `output_text` 长度比对，确认没有重复下发。

## 坑（踩过）
- `code_runner`：`import` 写在模块级会被沙箱剥离 → 一律写进 `execute` 内部。
- `code_runner`：必须 `return {"data": ...}`，返回 envelop 报「返回值不是字典」。
- 前后端统计口径要一致：字数 = CJK 字符数 + 拉丁词数；压缩比 = out_chars / in_chars。
- LLM 改写后字数可能变多（结构化 + 标题），此时显示的是膨胀百分比，不是压缩——属正常。## 布局变体：固定视口平铺（无滚动 + 紧凑字号 + 去分页）

用户说「页面不需要拖动」「把结果区缩进功能区平铺」「字体缩小、功能区也缩小、尽量一行」时，用这一套：

```html
<div class="wrap">            <!-- height:100vh; display:flex; flex-direction:column; gap:5px; overflow:hidden -->
  <header/>                   <!-- flex:0 0 auto -->
  <section class="card panel"><!-- flex:1 1 auto; 功能区与结果区同一张卡 -->
    <div class="toolbar"/>     <!-- flex-wrap:nowrap; overflow-x:auto; 单行不换行 -->
    <div class="main">        <!-- grid-template-columns:1fr 1fr; flex:1 1 auto; min-height:0 -->
      <div class="col left">  <!-- textarea#input -->
      <div class="col right"> <!-- textarea#output + #preview -->
    </div>
  </section>
  <section class="card stats"/>   <!-- flex:0 0 auto -->
  <section class="card histbox"/> <!-- flex:0 0 104px; 列表内部滚动 -->
</div>
```

关键 CSS：
- `html,body{height:100%;overflow:hidden;}` —— 整页不滚动，靠内部各区自己滚。
- 固定区 `flex:0 0 auto`，主区 `flex:1 1 auto; min-height:0`，子元素 `min-height:0` 才能真正压缩。
- 字号阶梯：正文 `11px` / 控件 `10.5px` / 次要说明 `9.5px` / 统计标签 `9px`；行高 1.45。
- 工具栏 `flex-wrap:nowrap` + `overflow-x:auto`，保证「一行」；太窄时横向滚动而不是换行。
- 结果 textarea `flex:1 1 auto; min-height:0` 占满右栏剩余高度。
- **分页要去掉**：删 `pgSize/pgPrev/pgNext/splitPages/PAGE_SIZE`，预览直接渲染全文。
- 保留 tab 切换「原文 / 预览」——二者互斥显示，避免同时占空间又不影响手工复制（textarea 里可直接全选复制）。

验证断言（写在 code_runner 里）：
`no_pager = all(x not in html for x in ['pgSize','pgPrev','pgNext','splitPages','PAGE_SIZE'])`、
`toolbar_inside_panel = html.index('class="card panel"') < html.index('class="toolbar"') < html.index('class="main"')`、
`hist_last = html.index('class="card histbox"') > html.index('class="card stats"')`。## 布局模式补充：首屏铺满 + 下方可滚动（2025-10-28）

用户常见诉求：「默认打开全屏正好看到主工作区，往下滚动才看到统计/历史」。

正确做法（不要用 `html,body{height:100%;overflow:hidden}` 锁死整页）：

```css
html,body{height:auto;overflow:visible;}   /* 页面恢复正常滚动 */
.wrap{max-width:1920px;min-height:100vh;margin:0 auto;display:flex;flex-direction:column;gap:5px;}
.panel{flex:0 0 calc(100vh - 36px);display:flex;flex-direction:column;gap:5px;}  /* 主工作区固定为首屏高 */
.main{...flex:1 1 auto;min-height:0;}   /* 左右栏内部各自滚动 */
.stats{flex:0 0 auto;}                   /* 统计区自然落到首屏之下 */
.histbox{flex:0 0 190px;}
```

要点：
- 用 `min-height:100vh` 而不是 `height:100vh`，否则下方统计/历史永远进不了滚动区。
- 主区用 `flex:0 0 calc(100vh - 36px)` 钉住首屏高度，内部 textarea 用 `flex:1 1 auto;min-height:0` 撑满并各自滚动。
- 下方统计/历史用 `flex:0 0 auto` / 固定 px，随页面滚动自然出现。

验证断言陷阱：`"height:100vh" not in css` 会误判 `min-height:100vh`（子串包含）。应断言完整片段 `".wrap{max-width:1920px;min-height:100vh;" in css`，或用正则带词边界。