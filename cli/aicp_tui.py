"""AICP TUI — Textual client (English UI)

Features:
  - 3-pane layout: session list / output / status bar
  - Streaming render: thinking / tool / reply color-coded
  - Fixed input area (never scrolled away by output)
  - Command palette (Ctrl+K)
  - Multi-session with unread marker on inactive sessions
  - Auto-reconnect (exponential backoff)
  - Theme switching (Ctrl+T)
  - Linux / tmux / Windows Terminal compatible

Keybindings:
  Ctrl+K    Command palette
  Ctrl+N    New session
  Ctrl+L    Clear output
  Ctrl+T    Toggle theme
  Ctrl+C    Interrupt current turn / double-press to quit
  Ctrl+Q    Quit
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional

try:
    import websockets
except ImportError:
    print("pip install websockets", file=sys.stderr)
    sys.exit(1)

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen
    from textual.widgets import (
        Footer, Header, Input, Label, ListItem, ListView, RichLog, Static,
    )
    from rich.text import Text
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.table import Table
except ImportError:
    print("pip install textual rich", file=sys.stderr)
    sys.exit(1)


# ============================================================
# Config
# ============================================================
DEFAULT_URL = os.environ.get("AICP_WS_URL", "ws://127.0.0.1:9001/ws")
DEFAULT_TOKEN = os.environ.get("AICP_TOKEN", "")

# 发消息等 envelop_result 的超时：main_agent.execute 里的 baseline LLM 判定
# 最多要 10s，再加上推送/其他开销，10s 会撞车。给足 60s。
SEND_TIMEOUT = 60.0
# 普通查询（get_session / list_conversations）的超时
QUERY_TIMEOUT = 30.0

THEMES = ["textual-dark", "textual-light", "nord", "gruvbox"]


# ============================================================
# Session state
# ============================================================
@dataclass
class SessionState:
    sid: str
    title: str = ""
    running: bool = False
    stream_buf: str = ""
    pending: Dict[str, asyncio.Future] = field(default_factory=dict)
    oldest_ts: str = ""
    has_more: bool = False
    has_update: bool = False
    depth: int = 0
    tokens: int = 0
    last_tool: str = ""


# ============================================================
# WS client (protocol layer, no UI)
# ============================================================
class WsClient:
    def __init__(self, url: str, token: str = ""):
        self.url = url
        self.token = token
        self.ws = None
        self.user_id = "local"
        self._closing = False
        self.on_event = None          # async callback(data)
        self.on_state = None          # sync callback(connected: bool)
        self._listen_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None

    async def connect(self) -> bool:
        qs = ["channel=pa_local"]
        if self.token:
            qs.append(f"token={self.token}")
        sep = "&" if "?" in self.url else "?"
        full = self.url + sep + "&".join(qs)
        try:
            self.ws = await websockets.connect(
                full, ping_interval=20, ping_timeout=20,
                max_size=64 * 1024 * 1024, open_timeout=5,
            )
        except Exception:
            if self.on_state:
                self.on_state(False)
            return False
        raw = await self.ws.recv()
        msg = json.loads(raw)
        data = msg.get("data") or msg
        if data.get("type") == "connected":
            self.user_id = data.get("user_id") or "local"
        await self._send({"type": "subscribe", "channel": f"pa_{self.user_id}"})
        if self.on_state:
            self.on_state(True)
        self._listen_task = asyncio.create_task(self._listen_loop())
        return True

    async def _send(self, obj):
        if not self.ws:
            return
        try:
            await self.ws.send(json.dumps(obj, ensure_ascii=False))
        except Exception:
            pass

    async def _listen_loop(self):
        try:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                data = msg.get("data") or msg
                if self.on_event:
                    try:
                        await self.on_event(data)
                    except Exception:
                        # 单个事件渲染失败不能拖垮监听循环，否则后续 envelop_result 收不到
                        pass
        except websockets.ConnectionClosed:
            pass
        except Exception:
            pass
        finally:
            if not self._closing:
                if self.on_state:
                    self.on_state(False)
                self._reconnect_task = asyncio.create_task(self._reconnect())

    async def _reconnect(self):
        for attempt in range(1, 6):
            if self._closing:
                return
            delay = min(2 ** attempt, 30)
            await asyncio.sleep(delay)
            if self._closing:
                return
            ok = await self.connect()
            if ok:
                return

    async def close(self):
        self._closing = True
        for t in (self._listen_task, self._reconnect_task):
            if t and not t.done():
                t.cancel()
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass

    async def call(self, ss: SessionState, receiver: str, payload: dict,
                   timeout: float = QUERY_TIMEOUT) -> dict:
        if not self.ws:
            return {"ok": False, "error": "ws not connected"}
        tid = uuid.uuid4().hex[:12]
        fut = asyncio.get_event_loop().create_future()
        ss.pending[tid] = fut
        await self._send({
            "type": "envelop",
            "receiver": receiver,
            # os/_websocket 从 data.payload.meta 取 trace_id，必须把 meta 塞进 payload
            "payload": {**payload, "meta": {"trace_id": tid, "session_id": ss.sid}},
        })
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            ss.pending.pop(tid, None)
            return {"ok": False, "error": f"timeout ({timeout}s)"}


# ============================================================
# Command palette (modal)
# ============================================================
class CommandPalette(ModalScreen[Optional[str]]):
    CSS = """
    CommandPalette {
        align: center middle;
        background: $background 60%;
    }
    #palette {
        width: 60;
        height: auto;
        max-height: 24;
        border: thick $accent;
        background: $surface;
        padding: 1;
    }
    #palette-input {
        dock: top;
        margin-bottom: 1;
    }
    #palette-list {
        height: auto;
        max-height: 18;
    }
    """

    COMMANDS = [
        ("New session", "new"),
        ("List conversations", "list"),
        ("Reload history", "history"),
        ("Interrupt current turn", "stop"),
        ("Clear output", "clear"),
        ("Toggle theme", "theme"),
        ("Reconnect", "reconnect"),
        ("Quit", "quit"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="palette"):
            yield Input(
                placeholder="Filter commands · Enter select · Esc close",
                id="palette-input",
            )
            yield ListView(id="palette-list")

    def on_mount(self):
        lv = self.query_one("#palette-list", ListView)
        for label, cmd in self.COMMANDS:
            lv.append(ListItem(Label(f"{label}  ({cmd})"), id=f"cmd-{cmd}"))
        self.query_one("#palette-input", Input).focus()

    async def on_input_changed(self, event: Input.Changed):
        q = (event.value or "").lower().strip()
        lv = self.query_one("#palette-list", ListView)
        for item in lv.children:
            if not item.id:
                continue
            cmd = item.id[4:]
            label = next((l for l, c in self.COMMANDS if c == cmd), cmd)
            # 隐藏不匹配的，显示匹配的
            item.display = bool(not q or q in label.lower() or q in cmd)

    def on_input_submitted(self, event: Input.Submitted):
        lv = self.query_one("#palette-list", ListView)
        if lv.index is not None and 0 <= lv.index < len(lv.children):
            item = lv.children[lv.index]
            if item.id:
                self.dismiss(item.id[4:])
                return
        self.dismiss(None)

    def on_list_view_selected(self, event: ListView.Selected):
        if event.item.id:
            self.dismiss(event.item.id[4:])
        else:
            self.dismiss(None)

    def key_escape(self):
        self.dismiss(None)


# ============================================================
# Main App
# ============================================================
class AICPApp(App):
    TITLE = "AICP BIO-1"
    SUB_TITLE = "TUI"

    # 关闭 Textual 内置命令面板（Ctrl+P）；本应用用自定义 CommandPalette（Ctrl+K）
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen {
        background: $background;
    }

    #layout {
        height: 1fr;
    }

    #sidebar {
        width: 32;
        border-right: solid $panel;
        background: $surface;
    }
    #sidebar-title {
        height: 3;
        padding: 1 1 0 1;
        color: $accent;
        text-style: bold;
    }
    #conv-list {
        height: 1fr;
    }
    #conv-list ListItem {
        padding: 0 1;
    }
    #conv-list ListItem.--highlight {
        background: $accent 30%;
    }
    #conv-list ListItem.has-update {
        color: $warning;
    }

    #main {
        width: 1fr;
    }
    #chat-header {
        height: 3;
        padding: 1 2 0 2;
        color: $text-muted;
        background: $surface;
        border-bottom: solid $panel;
    }
    #output {
        height: 1fr;
        padding: 1 2;
        background: $background;
    }
    #richlog {
        height: 1fr;
    }

    #input-area {
        dock: bottom;
        height: auto;
        max-height: 10;
        padding: 0 2 1 2;
        background: $surface;
    }
    #msg-input {
        border: tall $accent;
    }

    #status {
        height: 1;
        padding: 0 2;
        background: $panel;
        color: $text-muted;
    }

    Footer {
        background: $panel;
    }
    """

    BINDINGS = [
        # priority=True：焦点在 Input 里时也优先触发 App 级快捷键，
        # 否则 Input 自己的 emacs 键位会吞掉 Ctrl+K / Ctrl+T 等
        Binding("ctrl+k", "palette", "Cmd", show=True, priority=True),
        Binding("ctrl+n", "new_session", "New", show=True, priority=True),
        Binding("ctrl+l", "clear_output", "Clear", show=False, priority=True),
        Binding("ctrl+t", "toggle_theme", "Theme", show=False, priority=True),
        Binding("ctrl+c", "interrupt_or_quit", "Int/Quit", show=True, priority=True),
        # Ctrl+Q 在终端里是 XON/XOFF 流控，被终端吞掉 → 换 Ctrl+D
        Binding("ctrl+d", "quit_app", "Quit", show=True, priority=True),
    ]

    def __init__(self, url: str, token: str, initial_sid: str = ""):
        super().__init__()
        self.url = url
        self.token = token
        self.initial_sid = initial_sid or f"s_{uuid.uuid4().hex[:12]}"
        self.client = WsClient(url, token)
        self.client.on_event = self._on_ws_event
        self.client.on_state = self._on_ws_state
        self.sessions: Dict[str, SessionState] = {}
        self.current_sid = ""
        self._connected = False
        self._theme_idx = 0
        self._ctrl_c_count = 0

    async def _load_all_sessions(self):
        """从服务端拉全部会话，补进 self.sessions（不切换当前）。"""
        ss = self.sessions.get(self.current_sid)
        if not ss:
            return
        try:
            r = await self.client.call(
                ss, "builtins/agents/main_agent",
                {"action": "list_conversations", "include_archived": True},
                timeout=QUERY_TIMEOUT,
            )
            p = r.get("payload") or r.get("data") or r
            convs = p.get("conversations") or []
            for c in convs:
                sid = c.get("id") or ""
                if not sid:
                    continue
                if sid not in self.sessions:
                    self.sessions[sid] = SessionState(sid=sid)
                # 标题从列表里补（get_session 的 title 可能为空）
                if not self.sessions[sid].title:
                    self.sessions[sid].title = c.get("title") or ""
            await self._refresh_conv_list()
        except Exception as e:
            self._log_error(f"Failed to list conversations: {e}")
    # ------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="layout"):
            with Vertical(id="sidebar"):
                yield Label("*  Sessions", id="sidebar-title")
                yield ListView(id="conv-list")
            with Vertical(id="main"):
                yield Static("", id="chat-header")
                with VerticalScroll(id="output"):
                    yield RichLog(id="richlog", markup=True, wrap=True, highlight=True)
                with Vertical(id="input-area"):
                    yield Input(
                        placeholder="Type a message, Enter to send, Ctrl+K for commands…",
                        id="msg-input",
                    )
        yield Static("", id="status")
        yield Footer()

    async def on_mount(self):
        self._update_status()
        self._refresh_chat_header()
        ok = await self.client.connect()
        if not ok:
            self._log_error("Cannot connect WebSocket, retrying…")
        await self._open_session(self.initial_sid)
        # 拉全部会话补进左侧列表（不切换当前）
        await self._load_all_sessions()

    # ------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------
    async def _open_session(self, sid: str):
        if sid not in self.sessions:
            self.sessions[sid] = SessionState(sid=sid)
        self.current_sid = sid
        ss = self.sessions[sid]
        ss.has_update = False
        await self._refresh_conv_list()
        self._refresh_chat_header()
        self._clear_output()
        try:
            r = await self.client.call(
                ss, "builtins/agents/main_agent",
                {"action": "get_session", "session_id": sid, "limit": 20},
                timeout=QUERY_TIMEOUT,
            )
            p = r.get("payload") or r.get("data") or r
            ss.title = p.get("title") or ""
            # get_session 可能不给标题（新建会话），从会话列表补
            if not ss.title:
                try:
                    lr = await self.client.call(
                        ss, "builtins/agents/main_agent",
                        {"action": "list_conversations"}, timeout=QUERY_TIMEOUT,
                    )
                    lp = lr.get("payload") or lr.get("data") or lr
                    for c in (lp.get("conversations") or []):
                        if c.get("id") == sid and c.get("title"):
                            ss.title = c["title"]
                            break
                except Exception:
                    pass
            flow = p.get("flow") or []
            ss.oldest_ts = p.get("oldest_ts") or ""
            ss.has_more = bool(p.get("has_more"))
            if flow:
                self._render_flow(flow)
        except Exception as e:
            self._log_error(f"Failed to load history: {e}")
        self._refresh_chat_header()
        try:
            self.query_one("#msg-input", Input).focus()
        except Exception:
            pass

    async def _refresh_conv_list(self):
        """重建左侧会话列表。

        防御点：
        - clear() 是异步，必须 await，否则旧 ListItem 还在 → DuplicateIds
        - 单个 item append 失败不能拖垮整个列表，内部 catch
        - 标题为空时用 sid 前 12 位兜底
        - 按 has_update > running > updated_at 排序，当前会话置顶
        """
        try:
            lv = self.query_one("#conv-list", ListView)
        except Exception:
            return  # 面板还没 mount，下次再刷新

        try:
            await lv.clear()
        except Exception as e:
            self._log_error(f"conv list clear failed: {type(e).__name__}: {e}")
            return

        # 排序：当前会话 > 有更新 > 运行中 > 其他
        def _sort_key(item):
            sid, ss = item
            return (
                0 if sid == self.current_sid else 1,
                0 if ss.has_update else 1,
                0 if ss.running else 1,
                sid,
            )

        ordered = sorted(self.sessions.items(), key=_sort_key)

        for sid, ss in ordered:
            try:
                is_cur = (sid == self.current_sid)
                mark = "*" if is_cur else "-"
                title = (ss.title or sid[:12]).strip() or sid[:12]
                title = title[:24]
                run = " (run)" if ss.running else ""
                dot = " [red]*[/]" if ss.has_update and not is_cur else ""
                item = ListItem(Label(f"{mark} {title}{run}{dot}"), id=f"conv-{sid}")
                if ss.has_update and not is_cur:
                    item.add_class("has-update")
                lv.append(item)
            except Exception as e:
                # 单个 item 失败（重复 id / 渲染异常）不拖垮整个列表
                self._log_error(f"conv item '{sid[:12]}' failed: {type(e).__name__}: {e}")
                continue

    def _refresh_chat_header(self):
        ss = self.sessions.get(self.current_sid)
        title = ss.title if ss and ss.title else "(untitled)"
        sid_short = self.current_sid[:12] if self.current_sid else "-"
        self.query_one("#chat-header", Static).update(
            Text.assemble(
                ("* ", "cyan"),
                (title, "bold cyan"),
                ("   ", ""),
                (f"[{sid_short}]", "dim"),
            )
        )

    # ------------------------------------------------------------
    # Output helpers
    # ------------------------------------------------------------
    def _log(self, text, style: str = ""):
        log = self.query_one("#richlog", RichLog)
        if isinstance(text, str):
            text = Text(text, style=style)
        log.write(text)

    def _log_error(self, msg: str):
        try:
            self._log(Text(f"x {msg}", style="bold red"))
        except Exception:
            # 面板都没 mount，无处可写，放弃
            pass

    def _log_info(self, msg: str):
        try:
            self._log(Text(f". {msg}", style="dim"))
        except Exception:
            pass

    def _clear_output(self):
        try:
            self.query_one("#richlog", RichLog).clear()
        except Exception:
            pass

    def _render_flow(self, flow: list):
        for e in flow:
            frm = e.get("from")
            if frm == "user":
                self._log(Text.assemble(("you> ", "bold green"), (e.get("content") or "", "green")))
            elif frm == "ai" and e.get("call") == "reply":
                self._log(Text(""))
                self._log(Panel(
                    Markdown(e.get("content") or ""),
                    title="assistant",
                    border_style="cyan",
                ))
            elif frm == "system" and e.get("action") == "call_start":
                d = e.get("detail") or {}
                self._log(Text(f"  > {d.get('target','')} [{d.get('action','')}]", style="yellow"))
            elif frm == "system" and e.get("action") == "call_error":
                err = (e.get("detail") or {}).get("error", "")
                self._log(Text(f"  x {err}", style="red"))

    # ------------------------------------------------------------
    # WS events
    # ------------------------------------------------------------
    def _on_ws_state(self, connected: bool):
        self._connected = connected
        # WS 与 Textual 跑在同一线程/事件循环，直接调用即可，不能用 call_from_thread
        try:
            self._update_status()
        except Exception:
            pass

    async def _on_ws_event(self, data: dict):
        t = data.get("type")
        sid = data.get("session_id") or ""

        if t == "envelop_result":
            tid = data.get("trace_id") or ""
            for ss in self.sessions.values():
                fut = ss.pending.pop(tid, None)
                if fut and not fut.done():
                    fut.set_result(data)
                    return
            return

        if not sid:
            return
        ss = self.sessions.get(sid)
        if not ss:
            return
        is_cur = (sid == self.current_sid)

        if t == "summary_stream":
            chunk = data.get("chunk") or ""
            if chunk:
                ss.stream_buf += chunk
            return

        if t == "chat":
            content = data.get("content") or ""
            ss.running = False
            ss.stream_buf = ""
            try:
                if is_cur:
                    self._render_final_reply(content)
                else:
                    ss.has_update = True
                    await self._refresh_conv_list()
            except Exception as e:
                # 内层自己 catch，用户能看到错误，不是静默被 _listen_loop 吞
                self._log_error(f"render chat failed: {type(e).__name__}: {e}")
            return

        if t == "progress":
            step = data.get("step") or ""
            status = data.get("status") or ""
            try:
                if step == "thinking":
                    if is_cur:
                        self._show_thinking()
                elif status == "start":
                    tgt = data.get("target", "")
                    act = data.get("action", "")
                    ss.last_tool = tgt
                    if is_cur:
                        self._show_tool_start(tgt, act)
                elif status == "ok":
                    if is_cur:
                        self._show_tool_ok(data.get("target", ""), data.get("duration_ms"))
                elif status == "error":
                    if is_cur:
                        self._show_tool_err(data.get("target", ""), data.get("error", ""))
                elif status == "interrupting":
                    if is_cur:
                        self._show_info("interrupting…")
                elif status == "interrupted":
                    ss.running = False
                    if is_cur:
                        self._show_warn("interrupted")
            except Exception as e:
                self._log_error(f"render progress failed: {type(e).__name__}: {e}")
            return

        if t == "stats":
            stats = data.get("stats") or {}
            ss.depth = stats.get("depth", ss.depth)
            ss.tokens = stats.get("round_total", ss.tokens)
            if is_cur:
                try:
                    self._update_status()
                except Exception:
                    pass
            return

        if t == "error":
            ss.running = False
            msg = data.get("error") or data.get("message") or ""
            if is_cur:
                self._show_error(msg)
            return

    # ------------------------------------------------------------
    # UI updates (main thread)
    # ------------------------------------------------------------
    def _show_thinking(self):
        self._log(Text("thinking…", style="dim italic"))

    def _render_final_reply(self, content: str):
        self._log(Text(""))
        self._log(Panel(
            Markdown(content or ""),
            title="assistant",
            border_style="cyan",
        ))

    def _show_tool_start(self, tgt: str, act: str):
        self._log(Text(f"  > {tgt} [{act}]", style="yellow"))

    def _show_tool_ok(self, tgt: str, duration_ms):
        # duration_ms 可能是 None / int / str，统一转 int，转换失败就不显示时长
        try:
            d = f" ({int(duration_ms)/1000:.1f}s)" if duration_ms not in (None, "") else ""
        except (TypeError, ValueError):
            d = ""
        self._log(Text(f"  ok {tgt}{d}", style="green"))

    def _show_tool_err(self, tgt: str, err: str):
        self._log(Text(f"  fail {tgt}: {err}", style="red"))

    def _show_info(self, msg: str):
        self._log(Text(msg, style="dim"))

    def _show_warn(self, msg: str):
        self._log(Text(msg, style="yellow"))

    def _show_error(self, msg: str):
        self._log(Text(f"x {msg}", style="bold red"))

    def _update_status(self):
        ss = self.sessions.get(self.current_sid)
        ws_dot = Text("*", style="green") if self._connected else Text("*", style="red")
        # Rich 只认自己的样式名，且 Text.append() 不收 (str, style) 元组。
        # 全部用 Text.assemble(*parts) 一次拼完。
        parts = [
            ("AICP ", "bold cyan"),
            ("| ", "dim"),
            ws_dot,
            (" ", ""),
            ("ws" if self._connected else "offline", "dim"),
            (" | ", "dim"),
        ]
        if ss:
            parts.extend([
                ("depth ", "dim"),
                (f"{ss.depth}/40", "yellow" if ss.depth >= 30 else ""),
                (" | ", "dim"),
                ("tok ", "dim"),
                (self._fmt_tok(ss.tokens), ""),
                (" | ", "dim"),
                ("tool ", "dim"),
                (ss.last_tool.split("/")[-1] if ss.last_tool else "-", "dim"),
            ])
            if ss.running:
                parts.extend([
                    (" | ", "dim"),
                    ("running", "bold yellow"),
                ])
        self.query_one("#status", Static).update(Text.assemble(*parts))

    @staticmethod
    def _fmt_tok(n: int) -> str:
        if not n:
            return "0"
        if n >= 1_000_000:
            return f"{n/1_000_000:.1f}M"
        if n >= 1000:
            return f"{n/1000:.1f}K"
        return str(n)

    # ------------------------------------------------------------
    # Input
    # ------------------------------------------------------------
    async def on_input_submitted(self, event: Input.Submitted):
        if event.input.id != "msg-input":
            return
        text = (event.value or "").strip()
        if not text:
            return
        event.input.value = ""
        if text.startswith("/"):
            await self._handle_slash(text)
            return
        await self._send_message(text)

    async def _send_message(self, text: str):
        ss = self.sessions.get(self.current_sid)
        if not ss:
            return
        if ss.running:
            self._show_warn("Previous turn still running; Ctrl+C to interrupt")
            return
        self._log(Text.assemble(("you> ", "bold green"), (text, "green")))
        self._log(Text(""))
        ss.running = True
        self._update_status()
        # 60s：main_agent.execute 里的 baseline LLM 判定最多 10s，再加其他开销
        r = await self.client.call(
            ss, "builtins/agents/main_agent",
            {"content": text, "session_id": ss.sid}, timeout=SEND_TIMEOUT,
        )
        p = r.get("payload") or r.get("data") or r
        if r.get("error") or (isinstance(p, dict) and p.get("ok") is False):
            err = r.get("error") or (p.get("error") if isinstance(p, dict) else None) or "unknown"
            self._show_error(f"Send failed: {err}")
            ss.running = False
            self._update_status()

    async def _handle_slash(self, text: str):
        parts = text.split(maxsplit=1)
        cmd = parts[0]
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("/quit", "/q"):
            self.exit()
        elif cmd == "/new":
            await self.action_new_session()
        elif cmd == "/stop":
            await self.action_interrupt()
        elif cmd == "/clear":
            self._clear_output()
        elif cmd == "/history":
            await self._open_session(self.current_sid)
        elif cmd == "/help":
            self._show_info("Commands: /new /stop /clear /history /resume <sid> /quit")
        elif cmd == "/resume" and arg:
            await self._open_session(arg)
        else:
            self._show_warn(f"Unknown command: {cmd}")

    # ------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------
    async def action_palette(self):
        # Textual 8: push_screen_wait(wait_for_dismiss=True) 只能在 worker 里调用，
        # 普通 action 用 push_screen + callback（wait_for_dismiss=False）
        await self.push_screen(CommandPalette(), callback=self._on_palette_result)

    def _on_palette_result(self, result):
        # dismiss 回调（同步）；异步分支交给 call_after_refresh 调度
        if not result:
            return
        self.call_after_refresh(self._dispatch_palette, result)

    async def _dispatch_palette(self, result):
        if result == "new":
            await self.action_new_session()
        elif result == "list":
            await self._action_list_convs()
        elif result == "history":
            await self._open_session(self.current_sid)
        elif result == "stop":
            await self.action_interrupt()
        elif result == "clear":
            self._clear_output()
        elif result == "theme":
            self.action_toggle_theme()
        elif result == "reconnect":
            await self.client.connect()
        elif result == "quit":
            self.exit()

    async def action_new_session(self):
        sid = f"s_{uuid.uuid4().hex[:12]}"
        try:
            seed = self.sessions.get(self.current_sid) or SessionState(sid=self.current_sid)
            r = await self.client.call(
                seed, "builtins/agents/main_agent",
                {"action": "new_conversation"}, timeout=QUERY_TIMEOUT,
            )
            p = r.get("payload") or r.get("data") or r
            if p.get("ok"):
                sid = p.get("conversation_id") or sid
        except Exception:
            pass
        await self._open_session(sid)

    async def _action_list_convs(self):
        ss = self.sessions.get(self.current_sid)
        if not ss:
            return
        r = await self.client.call(
            ss, "builtins/agents/main_agent",
            {"action": "list_conversations"}, timeout=QUERY_TIMEOUT,
        )
        p = r.get("payload") or r.get("data") or r
        convs = p.get("conversations") or []
        self._log(Text(""))
        self._log(Text("Conversations", style="bold cyan"))
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("ID", style="dim")
        table.add_column("Title")
        table.add_column("Msgs", justify="right")
        for c in convs[:30]:
            table.add_row(
                str(c.get("id", ""))[:12],
                str(c.get("title") or "")[:40],
                str(c.get("message_count", 0)),
            )
        self._log(table)

    async def action_interrupt(self):
        ss = self.sessions.get(self.current_sid)
        if not ss or not ss.running:
            self._show_warn("No running turn")
            return
        r = await self.client.call(
            ss, "builtins/agents/main_agent",
            {"action": "interrupt", "session_id": ss.sid, "content": ""},
            timeout=QUERY_TIMEOUT,
        )
        p = r.get("payload") or r.get("data") or r
        if p.get("ok"):
            self._show_info("Interrupt requested")
        else:
            self._show_warn(f"Interrupt failed: {p.get('error')}")

    def action_clear_output(self):
        self._clear_output()

    def action_toggle_theme(self):
        self._theme_idx = (self._theme_idx + 1) % len(THEMES)
        try:
            self.theme = THEMES[self._theme_idx]
        except Exception:
            pass
        self._show_info(f"Theme: {THEMES[self._theme_idx]}")

    def action_interrupt_or_quit(self):
        ss = self.sessions.get(self.current_sid)
        if ss and ss.running:
            # Textual 官方异步提交方式，异常会被 Textual exception handler 接住
            self.run_worker(self.action_interrupt(), exclusive=False)
            self._ctrl_c_count = 0
            return
        self._ctrl_c_count += 1
        if self._ctrl_c_count >= 2:
            self.exit()
        else:
            self._show_info("Press Ctrl+C again to quit")

    def action_quit_app(self):
        self.exit()

    async def on_unmount(self):
        await self.client.close()

    # ------------------------------------------------------------
    # Session list click
    # ------------------------------------------------------------
    async def on_list_view_selected(self, event: ListView.Selected):
        if event.item.id and event.item.id.startswith("conv-"):
            sid = event.item.id[5:]
            if sid != self.current_sid:
                await self._open_session(sid)


# ============================================================
# Entry
# ============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--session", default="")
    ap.add_argument("--token", default=DEFAULT_TOKEN)
    ap.add_argument("--theme", default="textual-dark")
    args = ap.parse_args()

    app = AICPApp(args.url, args.token, args.session)
    if args.theme in THEMES:
        app._theme_idx = THEMES.index(args.theme)
        try:
            app.theme = args.theme
        except Exception:
            pass
    try:
        app.run()
    except KeyboardInterrupt:
        print("\n[cli] exit")
    except Exception as e:
        # 崩溃时给一行友好提示，不吐整页 traceback
        print(f"\n[cli] fatal: {type(e).__name__}: {e}")
        print("[cli] if this is a bug, report with the traceback above")
        sys.exit(1)


if __name__ == "__main__":
    main()
