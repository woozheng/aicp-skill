---
name: cn-free-search-api
description: 境内网络环境下用「免费无 key 检索源」为应用实现实时联网搜索 + 内容记录更新；含源可达性矩阵、检索污染规避、登记与检索解耦的分步进度反馈套路
category: aicp
---

# 境内免费检索源 + 分步进度式联网检索

## 何时用
用户要求「录入/登记某实体时实时去网络搜索其内容，并自动更新内容记录，列出前几条可达链接及出处；失败也要有提示；还能手工补充和刷新」这类需求。
典型场景：图书、电影、景点、产品、公司、论文的「录入即自动补全资料」应用。

## 一、免费无 key 检索源可达性矩阵（2025-10 实测，境内出口）

| 源 | 接口 | 可达性 |
|---|---|---|
| 豆瓣读书 | `https://book.douban.com/j/subject_suggest?q=<关键词>` | ✅ 返回 JSON（图书条目 + 作者 + 出版年 + 条目链接） |
| Bing RSS | `https://www.bing.com/search?q=<query>&format=rss` | ✅ 返回 XML，**无 JS 可解析** |
| Bing 网页 | `https://www.bing.com/search?q=<query>` | ✅ HTML 里 `li.b_algo` → `h2>a[href]` + `cite` + `p` |
| 百度 / 搜狗 / 360 | 各自站内搜索 | ✅（无稳定开放接口，抓 HTML 易反爬） |
| 当当 | 商品搜索页 | ✅ |
| Google Books / OpenLibrary / Wikipedia / DuckDuckGo / Gutendex | 官方开放 API | ❌ 全部 ConnectTimeout（境外出口不通） |

**结论**：境内环境不要把方案押在「境外正规图书 API」上。主源 = 目标站自身公开接口（如豆瓣 subject_suggest），辅源 = Bing RSS 做 `site:` 定向补齐。

**先探测再设计**：写代码前先用 `code_runner` 对候选源各发一个请求，记下可达/超时/状态码，再决定主辅源。超时要设 `httpx.Timeout(9.0, connect=6.0)`，别等默认 30s。

## 二、检索污染规避（通用检索的通病）

只发一条通用查询（`site:` 之外），会被游戏/破解/下载/影视类垃圾结果淹没。必须叠加：

1. **站点定向查询**：对每个目标站各发一条 `site:<domain> <关键词>`，按域名权重排序（主源 46、百科 44、文库 34…）。
2. **主源加权**：来自结构化接口（如豆瓣接口）的条目直接给 60+ 分，通用检索命中的按关键词命中给 8 分。
3. **离题降权**：命中 `游戏/辅助/外挂/破解/手游/单机/下载器/注册机/刷分/汉化/内购/加速器/电影/电视剧/动漫` 等词直接 -25 分。
4. **正文关键词加权**：命中 `图书/书籍/出版社/作者/简介/目录/读后感/赏析/评价/小说/名著/典藏/阅读/章节/全书/isbn/书评/正文/原文` 加 8 分。
5. **去重**：URL 去 `#fragment`、去尾斜杠、转小写后做 key。

## 三、登记与检索解耦（长耗时联网操作的通用 UX）

用户抱怨「点了登记没反应」= 联网检索和写库塞在同一个请求里。正确架构：

**后端**
- `create`：**只落库，立刻返回**，同时写入 `search` 状态对象（`state=pending`）。检索失败绝不丢记录。
- 检索拆成 3 个**独立 action**，每步结束都把进度 + 日志写回同一条记录：
  1. `search_collect` — 收集候选链接（写 `sources_tried`：每个源用了什么 query、命中几条、是否成功）
  2. `search_probe` — 逐条探测可达性，按顺序取前 N 条并记 `source`（出处）、`published_at`、`checked_at`、HTTP `status`
  3. `search_content` — 抓来源页正文 + LLM 生成概述/关键词/要点
- 另加 `search`（串跑三步）、`progress`（只查状态）、`refresh`（重置状态后重新检索）。
- 进度对象字段：`state`(pending/running/done/error)、`step`、`step_index`、`step_total`、`step_names`、`queries`、`sources_tried`、`candidates`、`candidate_count`、`reachable`、`adopted`、`error`、`logs[{at,text}]`、`started_at`、`finished_at`。
- **进度要能表达失败**：某步 0 候选就 `state=error` + `error` 文案，前端据此提示「可手工添加来源」。

**前端**
- 点登记 → 立刻 `toast('已登记《x》，开始联网检索')` + 清空表单 + 刷新列表（书已经在了）。
- 逐步串行请求，每步前先画**乐观进度**（当前步高亮），拿到返回再用后端真实 state 覆盖。
- 进度面板：3 个 step 卡片（active/done/err 三态）+ 一行 meta（检索词/候选数/可达数）+ 可滚动日志区（每行带时间戳）。
- 列表项上挂状态标签（待检索/检索中/已检索/检索异常）+ `<details>` 展开查看该书检索日志。
- 「重新检索」按钮：先 `refresh` 重置状态，再走同一条三步流程（保持 UI 一致）。

**通用原则**：任何「用户点一下 → 后台要做网络 IO + LLM」的交互，都要 **写入立即返回 + 长任务分步 + 前端可观测**。

## 四、抓取与探测细节

- 可达性探测用 `client.stream("GET", url)` 读**第一块字节就 break**，别把整页下下来；200–399 视为可达。
- 页面正文提取：先删 `<script|style|noscript>`，再删所有标签，`html.unescape`，压空白，截断 ~2600 字喂 LLM。
- LLM 用 `agent.llm.chat_json([{...}])`，返回 `{"summary","keywords","content_points"}`；**必须有兜底**（用来源 snippet 拼 summary），LLM 挂了也不能白跑。
- 手工添加的来源 `origin="manual"`，刷新时保留，不要被自动检索覆盖。

## 五、坑（踩过）

- **三元组解包**：站点表写 `[(domain, label, weight), ...]`，遍历时必须 `for domain, _label, bonus in SITES`；写成二元组解包会报 `too many values to unpack`。
- **全局变量存状态**不可取（多会话互相污染）；状态挂在自己记录里，通过 `envelop.payload` 传参。
- **`code_runner` 单次硬超时 60s**：三步检索串跑很容易超时要。测试时**一步一步单独打**，别一次全跑；或者把探测并发上限压到 10~18 条。
- **Bing RSS 需要 `format=rss`**，普通 HTML 页面要解析 `li.b_algo`，两套解析器都要写（RSS 更省事，优先用）。
- **`file_utils_api.write_file` 自带语法校验**：`validation.ok=false` 必须改完再留。
- **验证断言要具体**：页面 200 且含 `progressPanel`；`create` 返回的 book 里 `search.state` 非空；`search_collect` 的 `candidate_count>0`；`search_probe` 的 `reachable>0` 且前三条 `source` 是真实目标站（不是垃圾站）；`search_content` 的 summary 非空。
- 清理：排查用的临时探针插件放 `plugins/mytools/`，验证通过后删掉，别留在生产路由里。

## 六、文件三件套

| 文件 | 作用 |
|---|---|
| `plugins/applications/{project}/{plugin}.py` | 后端，`async def execute(envelop, agent)` |
| `plugins/applications/{project}/app.yaml` | `id` = project = `static_root` 目录名 |
| `www/apps/{project}/index.html` | 前端单页 |

后端文件名（去 `.py`）= 前端 `const PLUGIN` = 路由末段，三处对齐；数据落 `data/{project}/{plugin}.json`。