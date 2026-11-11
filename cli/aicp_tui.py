"""AICP TUI — Textual client (English UI)

Features:
  - 3-pane layout: session list / output / status bar
  - Streaming render: thinking / tool / reply color-coded
  - Fixed input area (never scrolled away by output)
  - Command palette (Ctrl+K)
  - Multi-session with unread marker on inactive sessions
  - Multi-agent: /agent <id> switches default agent and opens a new session
    (same semantics as console-muti's switchAgent)
  - Boot splash with loading indicator while agents/sessions load
  - Auto-reconnect (exponential backoff)
  - Theme switching (Ctrl+T)
  - Linux / tmux / Windows Terminal compatible

Keybindings:
  Ctrl+K    Command palette
  Ctrl+N    New session
  Ctrl+L    Clear output
  Ctrl+T    Toggle theme
  Ctrl+C    Interrupt current turn / double-press to quit
  Ctrl+D    Quit
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
    from textual import events
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen
    from textual.widgets import (
        Footer, Header, Input, Label, ListItem, ListView, RichLog, Static,
        LoadingIndicator,
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
DEFAULT_AGENT = os.environ.get("AICP_AGENT", "")

ENGINE_ROUTE = "builtins/agents/engine"

SEND_TIMEOUT = 60.0
QUERY_TIMEOUT = 30.0

THEMES = ["textual-dark", "textual-light", "nord", "gruvbox"]


# ============================================================
# Session state
# ============================================================
@dataclass
class SessionState:
    sid: str
    agent_id: str = ""
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
        self.on_event = None
        self.on_state = None
        self._listen_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        # 全局 pending 表：future 不再挂在 SessionState 上。
        # 原因：client.call() 可能用还没注册进 self.sessions 的临时 session
        # （如 action_new_session 的 seed），若 future 存在 session.pending 里，
        # 服务端回来的 envelop_result 就找不到对应的 future → 30s 超时。
        self.pending: Dict[str, asyncio.Future] = {}

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
        try:
            # 加超时：服务端不回 connected 帧时不能无限期挂住启动流程
            raw = await asyncio.wait_for(self.ws.recv(), timeout=3.0)
            msg = json.loads(raw)
            data = msg.get("data") or msg
            if data.get("type") == "connected":
                self.user_id = data.get("user_id") or "local"
        except Exception:
            pass
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

    async def call(self, ss: SessionState, payload: dict,
                   timeout: float = QUERY_TIMEOUT) -> dict:
        """统一走 engine；agent_id 双写 payload + meta。"""
        if not self.ws:
            return {"ok": False, "error": "ws not connected"}
        tid = uuid.uuid4().hex[:12]
        fut = asyncio.get_event_loop().create_future()
        self.pending[tid] = fut
        meta = {"trace_id": tid, "session_id": ss.sid}
        body = dict(payload)
        if ss.agent_id:
            meta["agent_id"] = ss.agent_id
            if "agent_id" not in body:
                body["agent_id"] = ss.agent_id
        body["meta"] = meta
        await self._send({
            "type": "envelop",
            "receiver": ENGINE_ROUTE,
            "payload": body,
        })
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self.pending.pop(tid, None)
            return {"ok": False, "error": f"timeout ({timeout}s)"}


# ============================================================
# Result extraction helpers
# ============================================================
def result_payload(r) -> dict:
    """从 envelop_result 里尽量稳定地取到业务 payload dict。"""
    if not isinstance(r, dict):
        return {}
    d = r.get("data")
    if isinstance(d, dict):
        p = d.get("payload")
        if isinstance(p, dict):
            return p
        return d
    p = r.get("payload")
    if isinstance(p, dict):
        return p
    return r


# ============================================================
# Boot splash
# ============================================================
class BootSplash(Vertical):
    """启动占位：标题 + 加载动画 + 状态行。

    必须是 Vertical：Static 只渲染一段文本，compose() 里的子控件不会被挂载，
    set_message() 找不到 #splash-msg 就静默失败 —— 表现为启动时一片黑。
    """

    def __init__(self, id: Optional[str] = None):
        super().__init__(id=id)
        self._msg = "Starting…"

    def compose(self) -> ComposeResult:
        with Vertical(id="splash-box"):
            yield Static("AICP", id="splash-title")
            yield Static("TUI", id="splash-sub")
            yield LoadingIndicator(id="splash-spin")
            yield Static(self._msg, id="splash-msg")

    def set_message(self, msg: str):
        self._msg = msg
        try:
            self.query_one("#splash-msg", Static).update(Text(msg, style="dim"))
        except Exception:
            pass


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

    BINDINGS = [
        Binding("escape", "close_palette", "Close", show=False, priority=True),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("enter", "select_item", "Select", show=False),
    ]

    COMMANDS = [
        ("New session", "new"),
        ("List conversations", "list"),
        ("List agents", "agents"),
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
                placeholder="Type to filter · Enter runs · Esc closes",
                id="palette-input",
            )
            yield ListView(id="palette-list")

    def on_mount(self):
        lv = self.query_one("#palette-list", ListView)
        for label, cmd in self.COMMANDS:
            lv.append(ListItem(Label(f"{label}  ({cmd})"), id=f"cmd-{cmd}"))
        self.query_one("#palette-input", Input).focus()

    def _visible_commands(self):
        """返回当前可见命令 [(label, cmd)]，与列表显示顺序一致。"""
        out = []
        for item in self.query_one("#palette-list", ListView).children:
            if not item.id or not item.display:
                continue
            cmd = item.id[4:]
            label = next((l for l, c in self.COMMANDS if c == cmd), cmd)
            out.append((label, cmd))
        return out

    async def on_input_changed(self, event: Input.Changed):
        q = (event.value or "").lower().strip()
        lv = self.query_one("#palette-list", ListView)
        for item in lv.children:
            if not item.id:
                continue
            cmd = item.id[4:]
            label = next((l for l, c in self.COMMANDS if c == cmd), cmd)
            item.display = bool(not q or q in label.lower() or q in cmd)
        # 过滤后把高亮移到第一条可见项
        vis = self._visible_commands()
        if vis:
            lv.index = 0

    def on_input_submitted(self, event: Input.Submitted):
        # Enter：优先用高亮项；没有高亮时取第一条可见项
        lv = self.query_one("#palette-list", ListView)
        cmd = ""
        if lv.index is not None:
            try:
                item = lv.children[lv.index]
                if item.display and item.id:
                    cmd = item.id[4:]
            except IndexError:
                cmd = ""
        if not cmd:
            vis = self._visible_commands()
            if vis:
                cmd = vis[0][1]
        self.dismiss(cmd or None)

    def on_list_view_selected(self, event: ListView.Selected):
        if event.item.id:
            self.dismiss(event.item.id[4:])
        else:
            self.dismiss(None)

    def action_close_palette(self):
        self.dismiss(None)

    def action_cursor_down(self):
        lv = self.query_one("#palette-list", ListView)
        self._move_cursor(lv, +1)

    def action_cursor_up(self):
        lv = self.query_one("#palette-list", ListView)
        self._move_cursor(lv, -1)

    def action_select_item(self):
        lv = self.query_one("#palette-list", ListView)
        if lv.index is not None:
            try:
                item = lv.children[lv.index]
                if item.id:
                    self.dismiss(item.id[4:])
                    return
            except IndexError:
                pass
        vis = self._visible_commands()
        if vis:
            self.dismiss(vis[0][1])
        else:
            self.dismiss(None)

    @staticmethod
    def _move_cursor(lv: ListView, delta: int):
        vis = [i for i in lv.children if i.display and i.id]
        if not vis:
            lv.index = None
            return
        cur = lv.index
        if cur is None or cur < 0 or cur >= len(lv.children):
            lv.index = lv.children.index(vis[0])
            return
        try:
            cur_pos = vis.index(lv.children[cur])
        except ValueError:
            cur_pos = 0
        new_pos = max(0, min(len(vis) - 1, cur_pos + delta))
        lv.index = lv.children.index(vis[new_pos])


# ============================================================
# Main App
# ============================================================
class AICPApp(App):
    TITLE = "AICP"
    SUB_TITLE = "TUI"

    ENABLE_COMMAND_PALETTE = False

    # Textual 8.2.8: 鼠标拖选在容器解析为 None 时抛 AttributeError
    # ('NoneType' object has no attribute 'region')，整个 App 直接崩。
    # TUI 不需要拖选复制，复制走终端自身快捷键。
    ALLOW_SELECT = False

    CSS = """
    Screen {
        background: $background;
    }

    /* ---- Boot splash ---- */
    #splash {
        align: center middle;
        width: 1fr;
        height: 1fr;
        background: $background;
    }
    #splash-box {
        width: 46;
        height: auto;
        align: center middle;
        padding: 1 2;
        border: round $accent 40%;
        background: $surface;
    }
    #splash-title {
        text-align: center;
        color: $accent;
        text-style: bold;
        width: 1fr;
    }
    #splash-sub {
        text-align: center;
        color: $text-muted;
        width: 1fr;
        margin-bottom: 1;
    }
    #splash-spin {
        width: 1fr;
        height: 1;
        margin: 1 0;
    }
    #splash-msg {
        text-align: center;
        color: $text-muted;
        width: 1fr;
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
    #hint {
        height: 1;
        color: $text-muted;
        background: $surface;
    }
    #hint.-error {
        color: $warning;
    }
    #hint.-ok {
        color: $success;
    }
    #suggest {
        height: auto;
        max-height: 8;
        background: $surface;
        border-left: thick $accent;
        padding: 0 1;
    }
    #suggest-list {
        height: auto;
        max-height: 8;
        background: $surface;
    }
    #suggest-list ListItem {
        padding: 0 1;
    }
    #suggest-list ListItem.--highlight {
        background: $accent 30%;
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
        Binding("ctrl+k", "palette", "Cmd", show=True, priority=True),
        Binding("ctrl+n", "new_session", "New", show=True, priority=True),
        Binding("ctrl+l", "clear_output", "Clear", show=False, priority=True),
        Binding("ctrl+t", "toggle_theme", "Theme", show=False, priority=True),
        Binding("ctrl+c", "interrupt_or_quit", "Int/Quit", show=True, priority=True),
        Binding("ctrl+d", "quit_app", "Quit", show=True, priority=True),
        Binding("tab", "cycle_suggest_forward", "Next", show=False, priority=True),
        Binding("shift+tab", "cycle_suggest_back", "Prev", show=False, priority=True),
    ]

    def __init__(self, url: str, token: str, agent_id: str = "", initial_sid: str = ""):
        super().__init__()
        self.url = url
        self.token = token
        self.default_agent_id = agent_id
        self.initial_sid = initial_sid or f"s_{uuid.uuid4().hex[:12]}"
        self.client = WsClient(url, token)
        self.client.on_event = self._on_ws_event
        self.client.on_state = self._on_ws_state
        self.sessions: Dict[str, SessionState] = {}
        self.current_sid = ""
        self._connected = False
        self._theme_idx = 0
        self._ctrl_c_count = 0
        self._agents = []
        self._booted = False

    # ------------------------------------------------------------
    # Agent discovery
    # ------------------------------------------------------------
    async def _load_agents(self) -> str:
        probe = SessionState(sid=self.initial_sid)
        try:
            r = await self.client.call(
                probe, {"action": "list_agents"}, timeout=QUERY_TIMEOUT,
            )
            p = result_payload(r)
            agents = p.get("agents") or []
            default = p.get("default") or ""
            self._agents = agents
            ids = [a.get("id") for a in agents if isinstance(a, dict) and a.get("id")]
            if self.default_agent_id and self.default_agent_id in ids:
                return self.default_agent_id
            if default and default in ids:
                return default
            if ids:
                return ids[0]
        except Exception as e:
            self._log_error(f"Failed to list agents: {e}")
        return self.default_agent_id

    # ------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield BootSplash(id="splash")
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
                    with Vertical(id="suggest"):
                        yield ListView(id="suggest-list")
        yield Static("", id="hint")
        yield Static("", id="status")
        yield Footer()

    async def on_mount(self):
        # 立刻显示主布局 —— 不再让 splash 阻塞整个启动流程。
        # 数据加载放到后台任务，界面先可交互。
        try:
            self.query_one("#layout").display = True
            self.query_one("#status").display = True
            self.query_one("#splash").display = False
        except Exception:
            pass

        self._booted = True
        self._update_status()
        self._refresh_chat_header()
        try:
            self.query_one("#msg-input", Input).focus()
        except Exception:
            pass

        # 后台启动加载流程
        self.run_worker(self._boot_sequence(), exclusive=False)

    async def _boot_sequence(self):
        """连接 + 加载数据。全部在后台跑，UI 保持可交互。"""
        splash = None
        try:
            splash = self.query_one("#splash", BootSplash)
        except Exception:
            pass

        ok = await self.client.connect()
        if not ok:
            self._log_error("Cannot connect WebSocket, retrying in background…")
            self._update_status()
            return

        self._update_status()

        # 加载期间的进度提示 —— 免得用户对着空白输出区干等
        self._log_info("loading agents and sessions…")

        # agents 与 sessions 并发拉取，省一个往返。
        # _load_all_sessions 用 probe session，不依赖 current_sid，并发安全。
        resolved, _ = await asyncio.gather(
            self._load_agents(),
            self._load_all_sessions(),
        )
        if resolved:
            self.default_agent_id = resolved

        # 先列出所有会话（一次请求），顺带补齐标题，避免二次 list_conversations
        await self._open_session(self.initial_sid, load_history=True)
        self._update_status()

    # ------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------
    async def _load_all_sessions(self):
        # 用 probe session 而不是 current_sid —— 启动时 current_sid 还是空的，
        # 直接 return 会导致会话列表永远加载不出来。
        ss = self.sessions.get(self.current_sid) or SessionState(
            sid=self.initial_sid, agent_id=self.default_agent_id
        )
        try:
            r = await self.client.call(
                ss,
                {"action": "list_conversations", "include_archived": True},
                timeout=QUERY_TIMEOUT,
            )
            p = result_payload(r)
            for c in (p.get("conversations") or []):
                sid = c.get("id") or ""
                if not sid:
                    continue
                agent_id = c.get("agent_id") or self.default_agent_id
                if sid not in self.sessions:
                    self.sessions[sid] = SessionState(sid=sid, agent_id=agent_id)
                elif agent_id and not self.sessions[sid].agent_id:
                    self.sessions[sid].agent_id = agent_id
                if not self.sessions[sid].title:
                    self.sessions[sid].title = c.get("title") or ""
            await self._refresh_conv_list()
        except Exception as e:
            self._log_error(f"Failed to list conversations: {e}")

    async def _open_session(self, sid: str, load_history: bool = True):
        if sid not in self.sessions:
            self.sessions[sid] = SessionState(sid=sid, agent_id=self.default_agent_id)
        self.current_sid = sid
        ss = self.sessions[sid]
        ss.has_update = False
        await self._refresh_conv_list()
        self._refresh_chat_header()
        self._clear_output()
        if not load_history:
            self._refresh_chat_header()
            try:
                self.query_one("#msg-input", Input).focus()
            except Exception:
                pass
            return
        try:
            r = await self.client.call(
                ss,
                {"action": "get_session", "session_id": sid, "limit": 20},
                timeout=QUERY_TIMEOUT,
            )
            p = result_payload(r)
            if p.get("agent_id"):
                ss.agent_id = p["agent_id"]
            ss.title = p.get("title") or ""
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
        try:
            lv = self.query_one("#conv-list", ListView)
        except Exception:
            return
        try:
            await lv.clear()
        except Exception as e:
            self._log_error(f"conv list clear failed: {type(e).__name__}: {e}")
            return

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
                title = title[:20]
                agent_tag = f" @{ss.agent_id}" if ss.agent_id else ""
                run = " (run)" if ss.running else ""
                dot = " [red]*[/]" if ss.has_update and not is_cur else ""
                label = f"{mark} {title}{agent_tag}{run}{dot}"
                item = ListItem(Label(label), id=f"conv-{sid}")
                if ss.has_update and not is_cur:
                    item.add_class("has-update")
                lv.append(item)
            except Exception as e:
                self._log_error(f"conv item '{sid[:12]}' failed: {type(e).__name__}: {e}")
                continue

    def _refresh_chat_header(self):
        ss = self.sessions.get(self.current_sid)
        title = ss.title if ss and ss.title else "(untitled)"
        agent = (ss.agent_id if ss and ss.agent_id else self.default_agent_id) or "-"
        sid_short = self.current_sid[:12] if self.current_sid else "-"
        try:
            self.query_one("#chat-header", Static).update(
                Text.assemble(
                    ("* ", "cyan"),
                    (title, "bold cyan"),
                    ("  @", "dim"),
                    (agent, "magenta"),
                    ("   ", ""),
                    (f"[{sid_short}]", "dim"),
                )
            )
        except Exception:
            pass

    # ------------------------------------------------------------
    # Output helpers
    # ------------------------------------------------------------
    def _log(self, text, style: str = ""):
        try:
            log = self.query_one("#richlog", RichLog)
        except Exception:
            return
        if isinstance(text, str):
            text = Text(text, style=style)
        log.write(text)

    def _log_error(self, msg: str):
        self._log(Text(f"x {msg}", style="bold red"))

    def _log_info(self, msg: str):
        self._log(Text(f". {msg}", style="dim"))

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
        try:
            self._update_status()
            self._update_hint()
        except Exception:
            pass

    async def _on_ws_event(self, data: dict):
        t = data.get("type")
        sid = data.get("session_id") or ""
        agent_id = data.get("agent_id") or ""

        if t == "envelop_result":
            tid = data.get("trace_id") or ""
            fut = self.client.pending.pop(tid, None)
            if fut and not fut.done():
                fut.set_result(data)
            return

        if not sid:
            return
        ss = self.sessions.get(sid)
        if not ss:
            return
        # 服务端 Push 带 agent_id 是权威来源
        if agent_id:
            ss.agent_id = agent_id
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
                self._log_error(f"render chat failed: {type(e).__name__}: {e}")
            return

        if t == "progress":
            step = data.get("step") or ""
            status = data.get("status") or ""
            try:
                if step == "thinking":
                    ss.running = True
                    if is_cur:
                        self._show_thinking()
                elif status == "start":
                    tgt = data.get("target", "")
                    act = data.get("action", "")
                    ss.last_tool = tgt
                    ss.running = True
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
                elif status == "reply":
                    ss.running = False
                    if is_cur:
                        self._update_status()
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
    # UI updates
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
        agent = (ss.agent_id if ss and ss.agent_id else self.default_agent_id) or "-"
        parts = [
            ("AICP ", "bold cyan"),
            ("| ", "dim"),
            ws_dot,
            (" ", ""),
            ("ws" if self._connected else "offline", "dim"),
            (" | ", "dim"),
            ("agent ", "dim"),
            (agent, "magenta"),
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
        try:
            self.query_one("#status", Static).update(Text.assemble(*parts))
        except Exception:
            pass

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
    async def on_input_changed(self, event: Input.Changed):
        if event.input.id != "msg-input":
            return
        self._update_suggestions(event.value or "")
        self._update_hint()

    # ------------------------------------------------------------
    # Key handling for completion navigation
    # ------------------------------------------------------------
    async def on_key(self, event: events.Key) -> None:
        """↑/↓ 在候选间移动高亮（不填入输入框）。

        Tab / Shift+Tab 走 BINDINGS（priority=True），优先级高于此处。
        """
        if not self._suggest_values():
            return
        key = event.key
        if key == "down":
            if self._move_suggest(1):
                event.prevent_default()
                event.stop()
        elif key == "up":
            if self._move_suggest(-1):
                event.prevent_default()
                event.stop()

    # ------------------------------------------------------------
    # Completion / hints
    # ------------------------------------------------------------
    SLASH_COMMANDS = [
        ("/new", "new session"),
        ("/stop", "interrupt current turn"),
        ("/clear", "clear output"),
        ("/history", "reload history"),
        ("/resume <sid>", "open a session by id"),
        ("/agents", "list agents"),
        ("/agent <id>", "switch default agent (opens new session)"),
        ("/help", "show commands"),
        ("/quit", "quit"),
    ]

    def _update_hint(self):
        """常驻提示行：当前 agent + 连接状态前置，再给命令和快捷键。"""
        try:
            hint = self.query_one("#hint", Static)
        except Exception:
            return
        try:
            inp = self.query_one("#msg-input", Input)
            cur = inp.value or ""
        except Exception:
            cur = ""

        # 前置状态：当前 agent / 连接状态 —— 放在提示行最显眼的位置
        ss = self.sessions.get(self.current_sid)
        agent = (ss.agent_id if ss and ss.agent_id else self.default_agent_id) or "-"
        conn_txt = "connected" if self._connected else "offline"
        conn_style = "green" if self._connected else "red"
        head = Text.assemble(
            ("agent ", "dim"), (agent, "bold magenta"), ("  |  ", "dim"),
            ("ws ", "dim"), (conn_txt, conn_style), ("  |  ", "dim"),
        )

        if cur.startswith("/"):
            head.append("commands: " + "  ".join(c for c, _ in self.SLASH_COMMANDS),
                        style="bold cyan")
        else:
            head.append(
                "Ctrl+K commands · /agents list · /agent <id> switch · "
                "Ctrl+N new · Ctrl+C interrupt/quit · Ctrl+D quit",
                style="dim")
        hint.update(head)
        hint.remove_class("-error")
        hint.remove_class("-ok")

    def _update_suggestions(self, text: str):
        """根据当前输入给出补全候选（命令 + agent id）。

        注意：ListView.remove 是 await 的，而 Input.Changed 处理器必须同步返回，
        所以这里不真正 remove item —— 改为整体清空后重建（mount 前的 remove_children
        是同步的）。见 _clear_suggestions。
        """
        try:
            lv = self.query_one("#suggest-list", ListView)
        except Exception:
            return
        self._clear_suggestions(lv)
        cands = []
        stripped = text.strip()
        if stripped.startswith("/"):
            parts = stripped.split(maxsplit=1)
            cmd = parts[0]
            arg = parts[1] if len(parts) > 1 else ""
            if not arg:
                for c, desc in self.SLASH_COMMANDS:
                    if c.startswith(cmd) or cmd.startswith("/"):
                        cands.append((c, desc, c))
            else:
                # /agent <prefix> → 补 agent id
                if cmd in ("/agent", "/resume"):
                    prefix = arg.lower()
                    pool = self._agent_ids() if cmd == "/agent" else self._session_ids()
                    for i in pool:
                        if not prefix or i.lower().startswith(prefix):
                            cands.append((f"{cmd} {i}", "", i))
        if not cands:
            try:
                self.query_one("#suggest", Vertical).display = False
            except Exception:
                pass
            return
        try:
            self.query_one("#suggest", Vertical).display = True
        except Exception:
            pass
        self._sug_values = []
        for label, desc, value in cands[:10]:
            text_label = f"{label}  {desc}".rstrip() if desc else label
            self._sug_values.append(value)
            try:
                lv.append(ListItem(Label(text_label), id=f"sug-{value}"))
            except Exception:
                pass
        try:
            lv.index = 0
        except Exception:
            pass

    def _clear_suggestions(self, lv: ListView):
        """同步清空候选列表。

        ListView.remove() 是 coroutine，但 Input.Changed 处理器必须是同步的
        （Textual 对非 async handler 返回 None 视为 OK，但对 async 之外的
        场景不保证）。这里改用 remove_children()，它在已 mount 的容器上
        是同步生效的；未 mount 时回退到 display=False。
        """
        try:
            lv.remove_children()
            return
        except Exception:
            pass
        try:
            lv.display = False
        except Exception:
            pass

    def _agent_ids(self):
        out = []
        for a in self._agents or []:
            if isinstance(a, dict) and a.get("id"):
                out.append(a["id"])
            elif isinstance(a, str):
                out.append(a)
        return out

    def _session_ids(self):
        return list(self.sessions.keys())

    def _suggest_values(self):
        """当前候选值列表（供 Tab / ↑↓ 导航）。"""
        return list(getattr(self, "_sug_values", []) or [])

    def _move_suggest(self, delta: int) -> bool:
        """在候选列表中移动高亮。返回是否移动成功。"""
        vals = self._suggest_values()
        if not vals:
            return False
        try:
            lv = self.query_one("#suggest-list", ListView)
        except Exception:
            return False
        try:
            cur = lv.index
        except Exception:
            cur = None
        if cur is None or cur < 0:
            nxt = 0 if delta > 0 else len(vals) - 1
        else:
            nxt = (cur + delta) % len(vals)
        try:
            lv.index = nxt
        except Exception:
            return False
        return True

    def _cycle_suggest(self, delta: int) -> bool:
        """Tab / Shift+Tab 在候选间循环。"""
        vals = self._suggest_values()
        if not vals:
            return False
        try:
            lv = self.query_one("#suggest-list", ListView)
        except Exception:
            return False
        try:
            cur = lv.index
        except Exception:
            cur = None
        if cur is None or cur < 0:
            nxt = 0 if delta > 0 else len(vals) - 1
        else:
            nxt = (cur + delta) % len(vals)
        try:
            lv.index = nxt
            self._accept_suggestion(vals[nxt])
        except Exception:
            return False
        return True

    def action_cycle_suggest_forward(self) -> None:
        """Tab：在候选间循环并填入输入框。"""
        self._cycle_suggest(1)

    def action_cycle_suggest_back(self) -> None:
        """Shift+Tab：反向循环候选。"""
        self._cycle_suggest(-1)

    def _accept_suggestion(self, value: str):
        try:
            inp = self.query_one("#msg-input", Input)
        except Exception:
            return
        inp.value = value
        inp.cursor_position = len(value)
        self._update_suggestions(value)

    async def on_list_view_selected(self, event: ListView.Selected):
        if event.item.id and event.item.id.startswith("sug-"):
            self._accept_suggestion(event.item.id[4:])
            return
        if event.item.id and event.item.id.startswith("conv-"):
            sid = event.item.id[5:]
            if sid != self.current_sid:
                await self._open_session(sid)

    async def on_input_submitted(self, event: Input.Submitted):
        if event.input.id != "msg-input":
            return
        text = (event.value or "").strip()
        if not text:
            return
        event.input.value = ""
        self._update_suggestions("")
        self._update_hint()
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
        r = await self.client.call(
            ss,
            {"content": text, "session_id": ss.sid},
            timeout=SEND_TIMEOUT,
        )
        p = result_payload(r)
        if r.get("error") or (isinstance(p, dict) and p.get("ok") is False):
            err = r.get("error") or (p.get("error") if isinstance(p, dict) else None) or "unknown"
            self._show_error(f"Send failed: {err}")
            ss.running = False
            self._update_status()
            return
        if isinstance(p, dict) and p.get("agent_id"):
            ss.agent_id = p["agent_id"]
            self._refresh_chat_header()
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
        elif cmd == "/agents":
            await self._action_list_agents()
        elif cmd == "/agent":
            if not arg:
                self._show_warn("Usage: /agent <id>  (see /agents)")
            else:
                await self._switch_agent(arg)
        elif cmd == "/help":
            self._show_info("Commands: /new /stop /clear /history /resume <sid> /agents /agent <id> /quit")
        elif cmd == "/resume" and arg:
            await self._open_session(arg)
        else:
            self._show_warn(f"Unknown command: {cmd}")

    # ------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------
    async def action_palette(self):
        await self.push_screen(CommandPalette(), callback=self._on_palette_result)

    def _on_palette_result(self, result):
        if not result:
            return
        self.call_after_refresh(self._dispatch_palette, result)

    async def _dispatch_palette(self, result):
        if result == "new":
            await self.action_new_session()
        elif result == "list":
            await self._action_list_convs()
        elif result == "agents":
            await self._action_list_agents()
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
        agent_id = self.default_agent_id
        seed = SessionState(sid=sid, agent_id=agent_id)
        try:
            r = await self.client.call(
                seed, {"action": "new_conversation"}, timeout=QUERY_TIMEOUT,
            )
            p = result_payload(r)
            if p.get("ok"):
                sid = p.get("conversation_id") or sid
                agent_id = p.get("agent_id") or agent_id
        except Exception:
            pass
        self.sessions[sid] = SessionState(sid=sid, agent_id=agent_id)
        await self._open_session(sid)

    async def _action_list_convs(self):
        ss = self.sessions.get(self.current_sid)
        if not ss:
            return
        r = await self.client.call(
            ss,
            {"action": "list_conversations", "include_archived": True},
            timeout=QUERY_TIMEOUT,
        )
        p = result_payload(r)
        convs = p.get("conversations") or []
        self._log(Text(""))
        self._log(Text("Conversations", style="bold cyan"))
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("ID", style="dim")
        table.add_column("Agent", style="magenta")
        table.add_column("Title")
        table.add_column("Msgs", justify="right")
        for c in convs[:30]:
            table.add_row(
                str(c.get("id", ""))[:12],
                str(c.get("agent_id", "") or "-"),
                str(c.get("title") or "")[:40],
                str(c.get("message_count", 0)),
            )
        self._log(table)

    async def _action_list_agents(self):
        ss = self.sessions.get(self.current_sid) or SessionState(sid=self.initial_sid)
        r = await self.client.call(ss, {"action": "list_agents"}, timeout=QUERY_TIMEOUT)
        p = result_payload(r)
        agents = p.get("agents") or []
        default = p.get("default") or ""
        self._agents = agents
        self._log(Text(""))
        self._log(Text("Agents", style="bold cyan"))
        if not agents:
            self._show_warn("No agents registered (check data/agents/*.md)")
            return
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("ID", style="magenta")
        table.add_column("Name")
        table.add_column("Default", justify="center")
        table.add_column("Description")
        for a in agents:
            if not isinstance(a, dict):
                continue
            aid = a.get("id") or ""
            is_def = "yes" if aid and aid == default else ""
            table.add_row(
                aid,
                str(a.get("name") or ""),
                is_def,
                str(a.get("description") or "")[:50],
            )
        self._log(table)

    async def _switch_agent(self, agent_id: str):
        """切 agent = 切默认 agent + 新建会话（对齐 console-muti 的 switchAgent）。

        原因：Engine 的存储是 users/<uid>/<agent_id>/sessions/<sid>/，
        agent_id 是路径一部分；同一 sid 中途换 agent 会导致 flow 文件不连续。
        切 agent 时开新会话是唯一无歧义的语义。
        """
        agent_id = (agent_id or "").strip()
        if not agent_id:
            self._show_warn("Usage: /agent <id>  (see /agents)")
            return
        if not self._agents:
            await self._action_list_agents()
        ids = [a.get("id") for a in self._agents if isinstance(a, dict) and a.get("id")]
        if ids and agent_id not in ids:
            self._show_warn(f"Unknown agent: {agent_id}  (see /agents)")
            return
        self.default_agent_id = agent_id
        self._show_info(f"Default agent → {agent_id}; opening new session")
        await self.action_new_session()

    async def action_interrupt(self):
        ss = self.sessions.get(self.current_sid)
        if not ss or not ss.running:
            self._show_warn("No running turn")
            return
        r = await self.client.call(
            ss,
            {"action": "interrupt", "session_id": ss.sid, "content": ""},
            timeout=QUERY_TIMEOUT,
        )
        p = result_payload(r)
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
    ap.add_argument("--agent", default=DEFAULT_AGENT)
    ap.add_argument("--theme", default="textual-dark")
    args = ap.parse_args()

    app = AICPApp(args.url, args.token, args.agent, args.session)
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
        print(f"\n[cli] fatal: {type(e).__name__}: {e}")
        print("[cli] if this is a bug, report with the traceback above")
        sys.exit(1)


if __name__ == "__main__":
    main()
