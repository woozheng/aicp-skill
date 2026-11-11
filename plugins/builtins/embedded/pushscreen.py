"""pushscreen — 把结构化数据渲染成 SML 并推送给屏幕设备。

职责：
  - 接收结构化数据（模板名 + data）
  - 用内联模板生成 SML 文本
  - 做基础 lint（组件白名单、文本宽度、坐标范围）
  - 调 os/_websocket 的 push_screen action 推送给 role=screen 的设备

不负责：
  - WebSocket 连接管理（交给 os/_websocket）
  - 业务数据获取（调用方提供）
  - 设备端渲染（固件负责）

路由：builtins/embedded/pushscreen
"""
import time


# ============================================================
# 常量
# ============================================================

TEXT_MAX_WIDTH = 32          # 单行文本最大显示宽度（中文按 2 计）
CANVAS_MAX_SIDE = 240        # canvas 单边最大像素，防止越界（对齐固件 sml_render.c CANVAS_MAX_W/H）
ALLOWED_COLORS = True        # 颜色格式校验开关，先不强制

# 组件白名单（小写）
ALLOWED_TAGS = {
    "screens", "screen",
    "list", "item", "text", "status", "canvas", "divider",
    "polyline", "line", "rect", "circle",
    "arc", "path",
}

# action 类型白名单
ALLOWED_ACTIONS = {
    "call", "push", "pop", "popup", "close_popup",
    "capture", "ptt_start", "ptt_stop",
}


# ============================================================
# 工具：文本宽度（中文按 2 计）
# ============================================================

def _text_width(s: str) -> int:
    w = 0
    for ch in s or "":
        w += 2 if ord(ch) > 0x7F else 1
    return w


def _truncate(s: str, max_w: int) -> str:
    """按显示宽度截断，超出部分丢弃。"""
    if _text_width(s) <= max_w:
        return s
    out = []
    w = 0
    for ch in s:
        cw = 2 if ord(ch) > 0x7F else 1
        if w + cw > max_w:
            break
        out.append(ch)
        w += cw
    return "".join(out)


def _esc(s: str) -> str:
    """XML 属性/文本转义。"""
    if s is None:
        return ""
    s = str(s)
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))


# ============================================================
# 模板：data -> SML 片段
# ============================================================

def _tpl_todo_list(data):
    """{"title": str, "items": [{"id","text","state"}]} -> <list>"""
    title = _esc(_truncate(str(data.get("title", "Todo")), TEXT_MAX_WIDTH))
    items = data.get("items") or []
    if not isinstance(items, list):
        items = []
    parts = [f'<list id="{_esc(data.get("id", "todo"))}" title="{title}" source="items" selectable="true">']
    for it in items:
        if not isinstance(it, dict):
            continue
        iid = _esc(str(it.get("id", "")))
        text = _esc(_truncate(str(it.get("text", "")), TEXT_MAX_WIDTH))
        state = it.get("state", "normal")
        if state not in ("normal", "done", "active"):
            state = "normal"
        action = it.get("action")
        act_attr = f' action="{_esc(action)}"' if action else ""
        parts.append(f'<item id="{iid}" text="{text}" state="{state}"{act_attr}/>')
    parts.append("</list>")
    return "".join(parts)


def _tpl_status(data):
    """{"icon": str, "label": str} -> <status>"""
    icon = data.get("icon", "idle")
    if icon not in ("idle", "thinking", "speaking", "listening", "error"):
        icon = "idle"
    label = _esc(_truncate(str(data.get("label", "")), 16))
    return f'<status icon="{icon}" label="{label}"/>'


def _tpl_text(data):
    """{"content": str, "align": str, "size": str} -> <text>"""
    content = _esc(_truncate(str(data.get("content", "")), TEXT_MAX_WIDTH))
    align = data.get("align", "left")
    if align not in ("left", "center", "right"):
        align = "left"
    size = data.get("size", "normal")
    if size not in ("tiny", "small", "normal", "medium", "large", "huge"):
        size = "normal"
    color = _esc(str(data.get("color", "#FFFFFF")))
    return f'<text content="{content}" align="{align}" size="{size}" color="{color}"/>'


def _tpl_canvas(data):
    """{"id","width","height","ops":[...]} -> <canvas>"""
    cid = _esc(data.get("id", "canvas"))
    w = int(data.get("width", 240))
    h = int(data.get("height", 240))
    w = max(1, min(w, CANVAS_MAX_SIDE))
    h = max(1, min(h, CANVAS_MAX_SIDE))
    parts = [f'<canvas id="{cid}" width="{w}" height="{h}">']
    for op in (data.get("ops") or []):
        if not isinstance(op, dict):
            continue
        kind = op.get("op", "")
        if kind == "polyline":
            pts = _esc(str(op.get("points", "")))
            color = _esc(str(op.get("color", "#FFFFFF")))
            width = int(op.get("width", 1))
            parts.append(f'<polyline points="{pts}" color="{color}" width="{width}"/>')
        elif kind == "line":
            parts.append(
                f'<line x0="{int(op.get("x0",0))}" y0="{int(op.get("y0",0))}" '
                f'x1="{int(op.get("x1",0))}" y1="{int(op.get("y1",0))}" '
                f'color="{_esc(str(op.get("color","#FFFFFF")))}" '
                f'width="{int(op.get("width",1))}"/>'
            )
        elif kind == "rect":
            parts.append(
                f'<rect x="{int(op.get("x",0))}" y="{int(op.get("y",0))}" '
                f'w="{int(op.get("w",0))}" h="{int(op.get("h",0))}" '
                f'color="{_esc(str(op.get("color","#FFFFFF")))}" '
                f'fill="{str(bool(op.get("fill",False))).lower()}"/>'
            )
        elif kind == "circle":
            parts.append(
                f'<circle cx="{int(op.get("cx",0))}" cy="{int(op.get("cy",0))}" '
                f'r="{int(op.get("r",0))}" '
                f'color="{_esc(str(op.get("color","#FFFFFF")))}" '
                f'fill="{str(bool(op.get("fill",False))).lower()}"/>'
            )
        elif kind == "text":
            parts.append(
                f'<text x="{int(op.get("x",0))}" y="{int(op.get("y",0))}" '
                f'content="{_esc(_truncate(str(op.get("content","")), TEXT_MAX_WIDTH))}" '
                f'color="{_esc(str(op.get("color","#FFFFFF")))}" '
                f'size="{_esc(str(op.get("size","normal")))}"/>'
            )
        elif kind == "arc":
            parts.append(
                f'<arc cx="{int(op.get("cx",0))}" cy="{int(op.get("cy",0))}" '
                f'r="{int(op.get("r",0))}" '
                f'start="{int(op.get("start",0))}" end="{int(op.get("end",360))}" '
                f'color="{_esc(str(op.get("color","#FFFFFF")))}" '
                f'width="{int(op.get("width",1))}" '
                f'fill="{str(bool(op.get("fill",False))).lower()}"/>'
            )
        elif kind == "path":
            parts.append(
                f'<path d="{_esc(str(op.get("d","")))}" '
                f'color="{_esc(str(op.get("color","#FFFFFF")))}" '
                f'width="{int(op.get("width",1))}" '
                f'fill="{str(bool(op.get("fill",False))).lower()}"/>'
            )
    parts.append("</canvas>")
    return "".join(parts)


TEMPLATES = {
    "todo_list": _tpl_todo_list,
    "status": _tpl_status,
    "text": _tpl_text,
    "canvas": _tpl_canvas,
}


# ============================================================
# lint：基础校验，不通过返回错误字符串列表
# ============================================================

def _lint_sml(sml: str):
    errors = []
    if not sml or not sml.strip():
        return ["SML 为空"]
    s = sml.strip()
    if not s.startswith("<screens>"):
        errors.append("顶层必须是 <screens>")
    if not s.endswith("</screens>"):
        errors.append("顶层必须以 </screens> 闭合")
    # 标签白名单：粗扫 <tag，跳过 </
    import re
    for m in re.finditer(r"<\s*([a-zA-Z][a-zA-Z0-9_-]*)", s):
        tag = m.group(1).lower()
        if tag not in ALLOWED_TAGS:
            errors.append(f"未知组件: <{tag}>")
            if len(errors) > 10:
                break
    # 不允许多余模板语法残留
    if "{{" in s or "}}" in s:
        errors.append("SML 中不允许残留 {{ }} 模板语法")
    return errors


def _build_screens(screen_id: str, title: str, theme: str, inner: str) -> str:
    sid = _esc(screen_id)
    t = _esc(_truncate(title, TEXT_MAX_WIDTH))
    th = theme if theme in ("dark", "light") else "dark"
    return (
        f'<screens><screen id="{sid}" title="{t}" theme="{th}">'
        f'{inner}'
        f'</screen></screens>'
    )


# ============================================================
# 入口
# ============================================================

async def execute(envelop, agent):
    payload = envelop.payload or {}
    action = payload.get("action", "")

    if action == "raw":
        # 直接推 SML 文本。用于调试或 AI 直接生成 SML 的场景。
        sml = payload.get("sml", "")
        device_id = payload.get("device_id", "")
        seq = int(payload.get("seq", 0) or 0)
        data_extra = payload.get("data", {}) or {}
        if not isinstance(data_extra, dict):
            data_extra = {}

        errors = _lint_sml(sml)
        if errors:
            envelop.payload = {"ok": False, "errors": errors}
            return envelop

        return await _push(agent, envelop, device_id, sml, seq, data_extra)

    if action == "render":
        # 结构化数据 + 模板名 -> SML
        device_id = payload.get("device_id", "")
        seq = int(payload.get("seq", 0) or 0)
        template = payload.get("template", "")
        data = payload.get("data", {}) or {}
        if not isinstance(data, dict):
            data = {}

        tpl = TEMPLATES.get(template)
        if not tpl:
            envelop.payload = {
                "ok": False,
                "error": f"未知模板: {template}",
                "available": sorted(TEMPLATES.keys()),
            }
            return envelop

        try:
            inner = tpl(data)
        except Exception as exc:
            envelop.payload = {"ok": False, "error": f"模板渲染失败: {exc}"}
            return envelop

        sml = _build_screens(
            screen_id=payload.get("screen_id", "main"),
            title=payload.get("title", "Screen"),
            theme=payload.get("theme", "dark"),
            inner=inner,
        )

        errors = _lint_sml(sml)
        if errors:
            envelop.payload = {"ok": False, "errors": errors, "sml_preview": sml[:400]}
            return envelop

        data_bind = data.get("bind", {}) if isinstance(data.get("bind"), dict) else {}
        return await _push(agent, envelop, device_id, sml, seq, data_bind)

    if action == "list_templates":
        envelop.payload = {"ok": True, "templates": sorted(TEMPLATES.keys())}
        return envelop

    envelop.payload = {"error": f"Unknown action: {action}"}
    return envelop


async def _push(agent, envelop, device_id, sml, seq, data_extra):
    """调 os/_websocket 的 push_screen action。"""
    from core import Envelop

    push_env = Envelop(
        sender="builtins/embedded/pushscreen",
        receiver="os/_websocket",
        intent="API_CALL",
        payload={
            "action": "push_screen",
            "device_id": device_id,
            "sml": sml,
            "seq": seq,
            "data": data_extra,
        },
        meta=dict(getattr(envelop, "meta", {}) or {}),
    )
    result = await agent.system.call(push_env)
    payload = getattr(result, "payload", None) or {}
    envelop.payload = {
        "ok": bool(payload.get("ok", False)),
        "sent": int(payload.get("sent", 0) or 0),
        "device_id": payload.get("device_id", device_id or "all"),
        "reason": payload.get("reason", ""),
        "sml_len": len(sml),
        "ts": time.time(),
    }
    return envelop
