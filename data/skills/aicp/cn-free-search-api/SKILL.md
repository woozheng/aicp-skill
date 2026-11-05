---
title: 联网检索境内免费源并自动补全
description: 境内网络用免费无 key 检索源实现录入即自动补全资料（用户说「录入的时候自动去网上搜一下」「search the web for me」时用），含源可达性矩阵与分步进度反馈
category: aicp
tags: [search, network, http, crawler, china]
triggers: 联网搜索, 自动补全, 录入即搜, 上网查, 抓取网页, 免费接口, 无key, 站点定向, 检索污染, 垃圾站, 转圈没反应, free search api, no api key, web scraping, crawler, bing rss, rss feed, douban api, public endpoint, multi source
not_triggers: 站内查询, 数据库查询, 全文检索, 离线匹配, 全文搜索
examples: "录入图书的时候自动去网上搜一下把简介补全", "点了登记没反应 一直在转圈", "能不能不申请 api key 就联网查资料", "search the web and fill in the description automatically"
version: 2.1
updated_at: 2026-11-05
---

# 联网检索境内免费源并自动补全

## 何时用

- 需要在用户「录入一条记录时立刻联网抓取公开资料自动补全」（用户说「录入的时候自动去网上搜一下把简介补全」时）
- 用户抱怨「点了登记没反应」——长耗时联网步骤塞在同步请求里（用户说「点了登记没反应一直在转圈」时）
- 需要在境内出口找到「不用申请 key 的公开检索接口」（用户说「能不能不申请 api key 就联网查资料」时）
- 需要给普通检索结果做去噪与排序，避免被垃圾结果淹没（用户说「搜出来一堆破解下载站」时）

**不适用**：站内查询（已有本地库直接查）；数据库查询；离线匹配；全文检索（本地语料分词检索，同义叫全文搜索）。

## 前置条件

- 插件：`builtins/tools/code_runner`（跑探测脚本）、`builtins/tools/file_utils_api`（读写后端与前端文件）
- 环境：境内出口网络，能访问目标站公开接口；依赖 `httpx`（已在 code_runner 环境内可 import）
- 需要 LLM 生成概述时先兜底：

```python
if not agent.llm:
    return {"data": {"error": "LLM 未配置"}}
```

- HTTP 超时务必收紧：`httpx.Timeout(9.0, connect=6.0)`，不要用默认 30s

## 核心步骤

**步骤 1：先探测再设计**

写代码前用 `code_runner` 对每个候选源各发一个请求，记录可达/超时/状态码，再决定主源与辅源。

```python
async def execute(envelop, agent):
    import httpx
    probes = [
        ("douban", "https://book.douban.com/j/subject_suggest?q=活着"),
        ("bing_rss", "https://www.bing.com/search?q=活着&format=rss"),
    ]
    out = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(9.0, connect=6.0)) as c:
        for name, url in probes:
            try:
                r = await c.get(url)
                out.append({"name": name, "status": r.status_code, "ok": r.status_code < 400})
            except Exception as e:
                out.append({"name": name, "ok": False, "err": type(e).__name__})
    return {"data": {"probes": out}}
```

失败情形：境外官方开放 API（Google Books / OpenLibrary / Wikipedia / DuckDuckGo / Gutendex）在境内出口会 ConnectTimeout，不应作为主源。

**步骤 2：主源 + 辅源组合**

- 主源 = 目标站自身公开接口（如豆瓣 `https://book.douban.com/j/subject_suggest?q=<关键词>`，返回 JSON：条目、作者、出版年、条目链接）
- 辅源 = Bing RSS `https://www.bing.com/search?q=<query>&format=rss`，返回 XML 无 JS，可直接解析；普通 Bing 网页则解析 `li.b_algo` → `h2>a[href]` + `cite` + `p`
- 百度 / 搜狗 / 360 / 当当可达，但无稳定开放接口，抓 HTML 易反爬

**步骤 3：检索污染规避**

1. 站点定向查询：对每个目标站各发一条 `site:<domain> <关键词>`，按域名权重排序（主源 46、百科 44、文库 34…）
2. 主源加权：结构化接口条目直接给 60+ 分，通用检索命中按关键词命中给 8 分
3. 离题降权：命中 `游戏/辅助/外挂/破解/手游/单机/下载器/注册机/刷分/汉化/内购/加速器/电影/电视剧/动漫` 直接 -25 分
4. 正文关键词加权：命中 `图书/书籍/出版社/作者/简介/目录/读后感/赏析/评价/小说/名著/典藏/阅读/章节/全书/isbn/书评/正文/原文` 加 8 分
5. 去重：URL 去 `#fragment`、去尾斜杠、转小写后作 key

**步骤 4：登记与检索解耦（后端）**

- `create`：**只落库立刻返回**，同时写入 `search` 状态对象（`state=pending`）。检索失败绝不丢记录
- 检索拆成 3 个独立 action，每步结束都把进度 + 日志写回同一条记录：
  1. `search_collect` — 收集候选链接，写 `sources_tried`（每个源用了什么 query、命中几条、是否成功）
  2. `search_probe` — 逐条探测可达性，按顺序取前 N 条，记 `source`、`published_at`、`checked_at`、HTTP `status`
  3. `search_content` — 抓来源页正文 + LLM 生成概述/关键词/要点
- 另加 `search`（串跑三步）、`progress`（只查状态）、`refresh`（重置状态后重新检索）
- 进度对象字段：`state`(pending/running/done/error)、`step`、`step_index`、`step_total`、`step_names`、`queries`、`sources_tried`、`candidates`、`candidate_count`、`reachable`、`adopted`、`error`、`logs[{at,text}]`、`started_at`、`finished_at`
- 某步 0 候选即 `state=error` + `error` 文案，前端据此提示「可手工添加来源」

**步骤 5：前端可观测**

- 点登记 → 立刻 `toast('已登记《x》，开始联网检索')` + 清空表单 + 刷新列表
- 逐步串行请求，每步前先画**乐观进度**（当前步高亮），拿到返回再用后端真实 state 覆盖
- 进度面板：3 个 step 卡片（active/done/err 三态）+ 一行 meta（检索词/候选数/可达数）+ 可滚动日志区（每行带时间戳）
- 列表项挂状态标签（待检索/检索中/已检索/检索异常）+ `<details>` 展开查看该书检索日志
- 「重新检索」按钮：先 `refresh` 重置状态，再走同一条三步流程

**步骤 6：抓取与探测细节**

- 可达性探测用 `client.stream("GET", url)` 读**第一块字节就 break**，别把整页下下来；200–399 视为可达
- 正文提取：先删 `<script|style|noscript>`，再删所有标签，`html.unescape`，压空白，截断 ~2600 字喂 LLM
- LLM 用 `agent.llm.chat_json([...])`，返回 `{"summary","keywords","content_points"}`；**必须有兜底**（用来源 snippet 拼 summary），LLM 挂了也不能白跑
- 手工添加的来源 `origin="manual"`，刷新时保留，不要被自动检索覆盖

**步骤 7：落地文件三件套**

| 文件 | 作用 |
|---|---|
| `plugins/applications/{project}/{plugin}.py` | 后端，`async def execute(envelop, agent)` |
| `plugins/applications/{project}/app.yaml` | `id` = project = `static_root` 目录名 |
| `www/apps/{project}/index.html` | 前端单页 |

后端文件名（去 `.py`）= 前端 `const PLUGIN` = 路由末段，三处对齐；数据落 `data/{project}/{plugin}.json`，用临时文件 + `os.replace` 原子写。

## 判断规则

| 条件 | 动作 |
|---|---|
| 目标站自身有公开接口（豆瓣 subject_suggest 等） | 主源用结构化接口，辅源用 Bing RSS 补 `site:` 定向 |
| 只有网页版搜索、无开放接口（百度/搜狗/360/当当） | 抓 HTML 并加超时与降级，谨慎对待反爬 |
| 官方开放 API 在境内 ConnectTimeout | 放弃该源，改用境内可达源，不要死磕 |
| 用户抱怨「点了登记没反应」 | 拆成 create 立即返回 + 三步检索 action + 前端进度面板 |
| 检索结果被垃圾站淹没 | 站点定向 + 主源加权 + 离题降权 + 关键词加权 |
| LLM 不可用或调用失败 | 用来源 snippet 兜底拼 summary，不丢内容 |
| 需要刷新某条记录的检索结果 | `refresh` 重置状态后重跑三步，保留 `origin="manual"` 的来源 |

## 踩过的坑

| 坑 | 现象 | 根因 | 修法 |
|---|---|---|---|
| 三元组解包 | `too many values to unpack` | 站点表写 `[(domain, label, weight), ...]` 却按二元组遍历 | `for domain, _label, bonus in SITES` |
| 全局变量存状态 | 多会话互相污染 | 状态挂在模块级变量 | 状态挂自己记录里，经 `envelop.payload` 传参 |
| code_runner 单次硬超时 60s | 三步检索串跑超时 | 一次调用内做了全部网络 IO | 测试时一步步单独打；探测并发上限压到 10~18 条 |
| Bing 解析器缺失 | 抓不到结果 | RSS 与普通 HTML 两套结构 | RSS 需 `format=rss`，普通页面解析 `li.b_algo`，两套都写，优先 RSS |
| write_file 留下坏文件 | 运行时报语法错 | 忽略 `validation.ok` | `validation.ok=false` 必须改完再留 |
| 断言写太松 | 垃圾站混入「可达链接」也判过 | 只断言数量 | 前三条 `source` 必须是真实目标站而非垃圾站 |
| 临时探针插件留在生产路由 | 路由表被污染 | 排查用插件未清理 | 放 `plugins/mytools/`，验证通过后删掉 |

## 验证

用 `code_runner` 跑，逐条断言：

```python
async def execute(envelop, agent):
    from core import Envelop

    steps, failed = [], []

    async def call(label, receiver, action, **kw):
        r = await agent.system.call(
            Envelop(receiver=receiver, payload={"action": action, **kw})
        )
        p = getattr(r, "payload", None) or {}
        st = getattr(r, "status", "") or ""
        (failed if st else steps).append({"step": label, "status": st or "ok"})
        return p

    base = "applications/booklog/books"
    created = await call("create", base, "create", title="活着")
    collect = await call("search_collect", base, "search_collect", id="1")
    probe = await call("search_probe", base, "search_probe", id="1")
    content = await call("search_content", base, "search_content", id="1")

    book = created.get("book") or {}
    checks = {
        "create_有search状态": bool(book.get("search")),
        "collect_候选大于0": (collect.get("candidate_count") or 0) > 0,
        "probe_可达大于0": (probe.get("reachable") or 0) > 0,
        "probe_来源是目标站": all(
            ("douban" in (c.get("source") or "") or "bing" in (c.get("source") or ""))
            for c in (probe.get("candidates") or [])[:3]
        ),
        "content_摘要非空": bool((content.get("summary") or "").strip()),
    }
    return {"data": {"ok": all(checks.values()), "checks": checks,
                      "steps": steps, "failed": failed}}
```

断言清单：

1. `create` 返回的 book 里 `search.state` 非空
2. `search_collect` 的 `candidate_count > 0`
3. `search_probe` 的 `reachable > 0`，且前三条 `source` 是真实目标站（不是垃圾站）
4. `search_content` 的 `summary` 非空
5. 页面返回 200 且含 `progressPanel` 元素 id

沙箱禁令：禁 `print` / `time.sleep` / `input` / `while True` / `subprocess.Popen` / 同步 `requests` 打本地 / `eval` / `exec`。

## 参考

- 相关插件：`builtins/tools/code_runner`、`builtins/tools/file_utils_api`、`builtins/tools/cogitor`
- 相关文件：`plugins/applications/{project}/app.yaml`、`www/apps/{project}/index.html`
- 相关 skill：`skill/aicp/plugin-action-smoke-test`（插件分步自检）、`skill/aicp/llm-runtime-stream-fix`（LLM 调用与兜底）、`skill/aicp/file-edit`（文件与大文件定位改写）
