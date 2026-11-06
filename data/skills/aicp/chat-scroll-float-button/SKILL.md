---
title: 加聊天悬浮回到底部按钮
description: 给流式聊天窗口加「不强制拉回底部」守卫与右侧悬浮回底箭头（用户说「不要自动拉到下面」「add a back-to-bottom button」时用），保住阅读位置
category: aicp
tags: [frontend, scroll, chat, ux, stream]
triggers: 自动拉到底, 拉到下面, 回到底部, 悬浮箭头, 漂浮箭头, 上翻消息, 聊天滚动, 滚回底部, 别动我的位置, autoscroll, scrollbottom, scrolltop, stickybottom, jumpbottom, chat scroll, floating arrow, streaming scroll, back to bottom
not_triggers: 发送消息, 载入历史, 会话切换, 表格, 侧边栏
examples: "我网上拖动的时候会自动拉到下面 希望不要自动拉", "拖上去看的时候右边要有个向下的漂浮箭头", "点了箭头之后它要消失 往上拉又要重新出现", "stop auto-scrolling to bottom and add a floating back-to-bottom arrow"
version: 1.0
updated_at: 2026-11-06
---

# 加聊天悬浮回到底部按钮

## 何时用

- 聊天窗口在流式输出时把用户强行拽回底部，用户正在翻历史（用户说「我网上拖动的时候会自动拉到下面」时）
- 需要一个「回到底部」入口，点了强制置底并自动消失（用户说「点一下就自动回到最下面，然后它消失」时）
- 按钮要放在右侧，且不能挡住正文与输入框（用户说「不要挡住聊天内容最好在右侧」时）
- 输入框高度会变（自动增高、附件预览展开），悬浮元素需要跟随（用户说「别压住输入区」时）

**不适用**：发送消息；载入历史；会话切换；表格；侧边栏滚动。

## 前置条件

- 需要 `builtins/tools/file_utils_api`（`search` 定位滚动调用点、`edit_file` 改代码）
- 需要 `builtins/tools/code_runner`（静态审计 + 行为验证）
- 目标前端文件通常很大（>2000 行），先 `search` 再 `read_file_lines`，不要整文件读
- 需要 LLM 时先兜底：`if not agent.llm: return {"data": {"error": "LLM 未配置"}}`

## 核心步骤

### 一、先摸清「谁在置底」

    use_tool(
        target="builtins/tools/file_utils_api",
        action="search",
        plugin_args={
            "path": "www/console.html",
            "content_pattern": "scrollTop =",
            "context": "2",
        },
    )

把所有写 `scrollTop` / `scrollToBottom` 的点列出来，分三类：

| 类别 | 典型函数 | 是否该强制置底 |
|---|---|---|
| 载入 / 重渲染 | `loadConversationFlow` / `renderFlowAsTurns` | 是（force=true） |
| 用户主动发送 | `addUserMessage` | 是（force=true） |
| 流式增量 | `handleReplyDelta` / `handleToolStart` / `ensureThinkingLine` | 否（force=false） |

### 二、加守卫标志：让流式输出尊重用户位置

    var _stickToBottom = true;   // 用户上翻历史时不再强制拉回底部

**跟随标记不能在每次 scroll 事件里用 `isAtBottom` 重算**——内容变高（thinking 推来）时浏览器不会同步抬高 `scrollTop`，重算会得到「不在底部」而误清标记，导致后续 thinking 再也拉不动底部（用户说「已经在最下端了，有 thinking 推过来还是不动」时）。改成**按滚动方向**判定：

    var _lastScrollTop = msgs.scrollTop;
    msgs.addEventListener('scroll', function(){
        var top = msgs.scrollTop;
        if (isAtBottom(48)) {
            _stickToBottom = true;              // 回到底部 → 恢复跟随
        } else if (top < _lastScrollTop) {
            _stickToBottom = false;             // 用户主动上翻（滚动条/键盘/惯性）
        }
        // 内容变高引起的滚动（scrollTop 未减少）不改变跟随状态
        _lastScrollTop = top;
        updateScrollDownBtn();
    });
    // 滚轮向上：明确表达「我要往上翻」，避免极端情况下没触发 scroll 事件
    // 已在最上端（scrollTop<=0，内容不足以滚动）时不算上翻，否则会误停跟随
    msgs.addEventListener('wheel', function(e){
        if (e.deltaY < 0 && msgs.scrollTop > 0) _stickToBottom = false;
    }, { passive: true });

置底函数改成两态：`force=true` 无视用户位置（发送 / 载入），`force=false` 先查标志：

    function scrollToBottom(force) {
        var msgs = document.getElementById('chatMessages');
        if (!msgs) return;
        if (!force && !_stickToBottom) { updateScrollDownBtn(); return; }
        // rAF 内每帧都要复查标志：用户可能在两帧之间上翻
        requestAnimationFrame(function(){
            if (!force && !_stickToBottom) { updateScrollDownBtn(); return; }
            msgs.scrollTop = msgs.scrollHeight;
            requestAnimationFrame(function(){
                if (!force && !_stickToBottom) return;
                msgs.scrollTop = msgs.scrollHeight;
                updateScrollDownBtn();
            });
        });
    }

**关键**：MutationObserver 回调只能调非强制版 `scrollToBottom()`，一旦用 `true` 守卫就失效。

### 三、悬浮按钮：DOM + CSS + 显隐

按钮放在聊天面板内（`.panel` 是 `position:relative`，`right` 相对面板右缘）：

    .scroll-down-btn{
      position:absolute;right:20px;bottom:110px;z-index:18;
      width:30px;height:30px;
      display:none;align-items:center;justify-content:center;
      background:var(--surface-2);color:var(--text-2);
      border:1px solid var(--border);border-radius:50%;
      box-shadow:0 2px 10px rgba(0,0,0,.18);
      cursor:pointer;transition:.15s;
    }
    .scroll-down-btn.show{display:inline-flex}

显隐只看是否贴底：

    function isAtBottom(tol) {
        var msgs = document.getElementById('chatMessages');
        if (!msgs) return true;
        return (msgs.scrollTop + msgs.clientHeight) >= (msgs.scrollHeight - (tol || 48));
    }
    function updateScrollDownBtn() {
        var btn = document.getElementById('scrollDownBtn');
        if (!btn) return;
        if (typeof t === 'function') btn.title = t('chat.scrollBottom');
        if (isAtBottom(48)) btn.classList.remove('show');
        else btn.classList.add('show');
    }

点击强制置底并复位标志，置底后按钮自然消失：

    function scrollToBottomNow() {
        var msgs = document.getElementById('chatMessages');
        if (!msgs) return;
        _stickToBottom = true;
        msgs.scrollTop = msgs.scrollHeight;
        updateScrollDownBtn();
    }

### 四、位置跟随输入区高度（拆成两个函数）

`bottom` 不能写死——输入框会自动增高、附件会撑开输入区。**定位**与**显隐**必须拆开：scroll 事件每帧都触发，布局读（`offsetHeight`）放在这里会掉帧。

    function positionScrollDownBtn() {
        var btn = document.getElementById('scrollDownBtn');
        var wrap = document.querySelector('#panel-chat .input-wrap');
        if (!btn || !wrap) return;
        // 面板底部还预留了 22px 状态栏高度（.panel{padding-bottom:22px}）
        btn.style.bottom = (wrap.offsetHeight + 34) + 'px';
    }

只在输入区尺寸变化时重算位置：

    var wrap = document.querySelector('#panel-chat .input-wrap');
    if (wrap && typeof ResizeObserver === 'function') {
        new ResizeObserver(function(){ positionScrollDownBtn(); }).observe(wrap);
    }

### 五、i18n

    'chat.scrollBottom':'回到底部',        // zh
    'chat.scrollBottom':'Back to bottom',  // en

按钮加 `data-i18n-title="chat.scrollBottom"`，`applyLang` 已支持该属性，会自动刷新 `title`。

## 判断规则

| 条件 | 动作 |
|---|---|
| 滚动态是流式增量（reply delta / tool / thinking） | 用非强制 `scrollToBottom()`，由 `_stickToBottom` 决定 |
| 滚动态是发送消息 / 载入会话 / 重渲染 | 用 `scrollToBottom(true)` |
| 用户点击悬浮箭头 | 强制置底 + `_stickToBottom = true` + 刷新显隐 |
| 用户手动滚到底部 | scroll 监听把 `_stickToBottom` 置回 true，箭头隐藏 |
| 输入区高度变化 | `ResizeObserver` → `positionScrollDownBtn()`（只重算位置，不重算显隐） |
| 面板底部有状态栏预留 | `bottom = 输入区高度 + 状态栏预留 + 间隙`，别写死 |
| 按钮遮挡正文 | 放在面板右缘 `right:20px`；正文列有 `max-width` 时通常不冲突 |

## 踩过的坑

| 坑 | 现象 | 根因 | 修法 |
|---|---|---|---|
| 流式路径用了强制置底 | 用户上翻时又被拽回底部 | MutationObserver 回调调 `scrollToBottom(true)`，守卫被绕过 | 流式路径只用非强制版 |
| 只在函数入口查标志 | 用户在两帧之间上翻仍被拉走 | rAF 内没复查 | 每一帧都复查 `_stickToBottom` |
| scroll 回调里读 `offsetHeight` | 滚动时掉帧 | 每次 scroll 都触发强制布局 | 拆出 `positionScrollDownBtn()`，只在 ResizeObserver 里调 |
| 按钮 `bottom` 写死 | 输入框增高后按钮压住发送键 | 输入区高度可变 | 用 `wrap.offsetHeight` 动态算 |
| 忘了面板底部状态栏 | 按钮压到状态栏上 | `.panel{padding-bottom:22px}` | 偏移里加上预留高度 |
| 点了按钮不消失 | 点完箭头还在 | 没有重新算 `isAtBottom` | 点击后调 `updateScrollDownBtn()` |
| 单一全局函数管所有置底 | 想区分「跟随」和「强制」做不到 | 一个 `scrollToBottom()` 混用 | 用 `force` 参数分流 |
| scroll 回调里用 `isAtBottom` 重算跟随标记 | 已在底部时 thinking 推过来，聊天区不再自动上滚 | 内容变高时浏览器不抬高 `scrollTop`，重算得「不在底部」→ 误清 `_stickToBottom` | 改为按方向判定（`top < _lastScrollTop` 才置 false），回到底部才置回 true |

## 验证

静态审计（一次跑完，输出 `{name: bool}` + `all_ok`）：

```python
async def execute(envelop, agent):
    import re
    s = open('www/console.html', encoding='utf-8').read()

    def body(name):
        m = re.search(r'function\s+' + re.escape(name) + r'\s*\([^)]*\)\s*\{', s)
        if not m:
            return None
        j = s.find('{', m.start())
        d = 0
        for k in range(j, len(s)):
            if s[k] == '{':
                d += 1
            elif s[k] == '}':
                d -= 1
                if d == 0:
                    return s[j:k + 1]
        return None

    stb = body('scrollToBottom')
    now = body('scrollToBottomNow')
    upd = body('updateScrollDownBtn')
    pos = body('positionScrollDownBtn')
    init = body('initAutoScroll')
    res = {}

    # 1) 守卫在改 scrollTop 之前，且 rAF 内复查
    gi = (stb or '').find('_stickToBottom')
    si = (stb or '').find('msgs.scrollTop = msgs.scrollHeight')
    res['guard_before_scroll'] = gi != -1 and si != -1 and gi < si
    res['guard_count'] = (stb or '').count('_stickToBottom') >= 3

    # 2) 强制置底只允许出现在 载入/发送/重渲染
    forced = []
    for m in re.finditer(r'scrollToBottom\(true\)', s):
        fns = list(re.finditer(r'(?:async\s+)?function\s+(\w+)\s*\(', s[:m.start()]))
        forced.append(fns[-1].group(1) if fns else None)
    res['forced_ctx'] = forced
    res['forced_ok'] = set(forced) <= {'loadConversationFlow', 'addUserMessage', 'renderFlowAsTurns'}

    # 3) 流式路径不得强制置底
    bad = [f for f in ['handleReplyDelta', 'handleReplyEnd', 'handleToolStart',
                       'handleToolEnd', 'ensureThinkingLine', 'addAssistantMessage']
           if body(f) and 'scrollToBottom(true)' in body(f)]
    res['stream_no_force'] = (bad == [])

    # 4) 按钮显隐 / 点击强制置底
    res['btn_toggle'] = "classList.add('show')" in (upd or '') and "classList.remove('show')" in (upd or '')
    res['now_force'] = '_stickToBottom = true;' in (now or '')
    res['mo_nonforce'] = 'new MutationObserver(function(){ scrollToBottom(); })' in (init or '')

    # 5) 定位与显隐分离，scroll 回调不读布局
    res['pos_exists'] = pos is not None
    res['upd_no_layout_read'] = 'offsetHeight' not in (upd or '')
    res['resize_calls_pos'] = 'ResizeObserver(function(){ positionScrollDownBtn(); })' in (init or '')

    res['all_ok'] = all(v for k, v in res.items() if isinstance(v, bool))
    return {"data": res}
```

断言清单：

1. 守卫判断出现在写 `scrollTop` 之前，且函数体内复查次数 ≥ 3（入口 + 两层 rAF）
2. `scrollToBottom(true)` 只出现在 `loadConversationFlow` / `addUserMessage` / `renderFlowAsTurns`
3. 流式路径（delta / tool / thinking / assistant 消息）里没有 `scrollToBottom(true)`
4. `updateScrollDownBtn` 里没有 `offsetHeight`（布局读只在 `positionScrollDownBtn`）
5. `ResizeObserver` 回调调用的是 `positionScrollDownBtn()`
6. `MutationObserver` 回调调的是非强制 `scrollToBottom()`

**视觉部分脚本查不出来**：按钮是否真挡住正文、圆角/阴影是否协调，必须让用户在浏览器里看一眼。

## 参考

- 相关 skill：`skill/aicp/file-edit`（常规文件编辑规范）
- 相关插件：`builtins/tools/file_utils_api`（定位与修改代码）
- 相关插件：`builtins/tools/code_runner`（静态审计脚本）
- 相关文件：`www/console.html`（聊天面板、`.messages` 滚动容器、`.input-wrap` 输入区）