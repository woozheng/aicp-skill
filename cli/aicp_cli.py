"""AICP CLI 增强版 — 纯 WebSocket + 非阻塞输入 + 多会话

用法：
  python cli/aicp_cli.py                         # 新会话
  python cli/aicp_cli.py --session s_xxx         # 恢复会话
  python cli/aicp_cli.py --url ws://host:9001/ws

命令：
  /new              新建会话（保留当前，切到新会话）
  /list             列出会话
  /resume <sid>     切到指定会话
  /sessions         列出本进程打开的所有会话（并行）
  /switch <sid>     切到已打开的会话
  /close <sid>      关闭一个会话
  /history [n]      显示当前会话最近 n 轮（默认 10，翻页用 --more）
  /more             继续往前翻
  /stop             中断当前会话的回合
  /attach <file>    把文件内容附到下一条消息（可多次）
  /attachments      查看已附加的文件
  /clear-attach     清空附加文件
  /help             帮助
  /quit             退出

输入：
  @file.txt         消息里 @路径 自动把文件内容拼进 content
  Ctrl-C           退出
  Ctrl-D           退出
"""
import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

try:
    import websockets
except ImportError:
    print("pip install websockets", file=sys.stderr)
    sys.exit(1)

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.patch_stdout import patch_stdout
    from prompt_toolkit.history import FileHistory
except ImportError:
    print("pip install prompt_toolkit", file=sys.stderr)
    sys.exit(1)


MAX_ATTACH_BYTES = 200 * 1024   # 单个附件上限 200KB


class SessionState:
    """一个会话的本地状态。"""
    def __init__(self, sid: str):
        self.sid = sid
        self.title = ""
        self.running_turn = False
        self.stream_buf = ""
        self.pending = {}            # trace_id -> Future
        self.oldest_ts = ""
        self.has_more = False
        self.attachments = []        # [(name, content)]


class AICPCLI:
    def __init__(self, url: str, session_id: str = "", token: str = ""):
        self.url = url
        self._token = token
        self.user_id = "local"
        self.ws = None
        self._ws_lock = asyncio.Lock()
        self._listen_task = None
        self._sessions = {}          # sid -> SessionState
        self._current = ""           # 当前 sid
        self._prompt = None
        self._closing = False

    # ------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------
    async def connect(self, initial_sid: str):
        qs = [f"channel=pa_local"]
        if self._token:
            qs.append(f"token={self._token}")
        sep = "&" if "?" in self.url else "?"
        full = self.url + sep + "&".join(qs)
        print(f"[cli] 连接 {full} ...")

        # 连接重试：3 次，间隔 1s / 2s / 3s；全部失败给友好提示
        last_err = None
        for attempt in range(3):
            try:
                self.ws = await websockets.connect(
                    full, ping_interval=20, ping_timeout=20, max_size=64 * 1024 * 1024,
                    open_timeout=5,
                )
                break
            except ConnectionRefusedError:
                last_err = "服务端拒绝连接"
            except asyncio.TimeoutError:
                last_err = "连接超时（5s）"
            except OSError as e:
                last_err = f"网络错误：{e}"
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
            if attempt < 2:
                wait = attempt + 1
                print(f"[cli] 连接失败（{last_err}），{wait}s 后重试 {attempt + 1}/3 ...")
                await asyncio.sleep(wait)
        if self.ws is None:
            print()
            print(f"[cli] ❌ 无法连接 WebSocket：{last_err}")
            print(f"[cli]    地址：{self.url}")
            print(f"[cli]    请确认：")
            print(f"[cli]      1. AICP 服务已启动")
            print(f"[cli]      2. WebSocket 服务在监听（os/_websocket action=START）")
            print(f"[cli]      3. 端口正确（看 aicp.yaml 的 websocket.external_url 或 /api/ws_config）")
            print(f"[cli]      4. 防火墙未拦截该端口")
            print(f"[cli]    换地址：--url ws://127.0.0.1:9001/ws")
            sys.exit(1)
        raw = await self.ws.recv()
        msg = json.loads(raw)
        data = msg.get("data") or msg
        if data.get("type") == "connected":
            self.user_id = data.get("user_id") or "local"
            print(f"[cli] 已连接 ws_id={data.get('ws_id')} user={self.user_id}")
        await self._send({"type": "subscribe", "channel": f"pa_{self.user_id}"})
        self._listen_task = asyncio.create_task(self._listen_loop())
        # 打开初始会话
        await self._open_session(initial_sid)

    async def _send(self, obj):
        async with self._ws_lock:
            if not self.ws:
                return
            await self.ws.send(json.dumps(obj, ensure_ascii=False))

    # ------------------------------------------------------------
    # 会话管理
    # ------------------------------------------------------------
    def _ss(self) -> SessionState:
        return self._sessions[self._current]

    async def _open_session(self, sid: str):
        if sid in self._sessions:
            self._current = sid
            print(f"[cli] 切到会话: {sid}")
            return
        self._sessions[sid] = SessionState(sid)
        self._current = sid
        # 拉标题
        try:
            r = await self.call(sid, "builtins/agents/main_agent",
                                {"action": "get_session", "session_id": sid, "limit": 1})
            p = r.get("payload") or r.get("data") or r
            self._sessions[sid].title = p.get("title") or ""
        except Exception:
            pass
        print(f"[cli] 会话: {sid}" + (f" ({self._sessions[sid].title})" if self._sessions[sid].title else ""))

    # ------------------------------------------------------------
    # 监听
    # ------------------------------------------------------------
    async def _listen_loop(self):
        try:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                await self._handle(msg.get("data") or msg)
        except websockets.ConnectionClosed:
            pass
        except Exception as e:
            if not self._closing:
                print(f"\n[cli] 监听异常: {type(e).__name__}: {e}")
        finally:
            if not self._closing:
                await self._reconnect()

    async def _reconnect(self):
        """断线自动重连：指数退避，最多 5 次。"""
        for attempt in range(1, 6):
            delay = min(2 ** attempt, 30)
            print(f"[cli] 🔌 连接断开，{delay}s 后重连 {attempt}/5 ...")
            await asyncio.sleep(delay)
            if self._closing:
                return
            try:
                # 清旧连接
                try:
                    await self.ws.close()
                except Exception:
                    pass
                self.ws = None
                # 重新握手
                qs = [f"channel=pa_local"]
                if self._token:
                    qs.append(f"token={self._token}")
                sep = "&" if "?" in self.url else "?"
                full = self.url + sep + "&".join(qs)
                self.ws = await websockets.connect(
                    full, ping_interval=20, ping_timeout=20,
                    max_size=64 * 1024 * 1024, open_timeout=5,
                )
                raw = await self.ws.recv()
                msg = json.loads(raw)
                data = msg.get("data") or msg
                if data.get("type") == "connected":
                    self.user_id = data.get("user_id") or "local"
                await self._send({"type": "subscribe", "channel": f"pa_{self.user_id}"})
                print(f"[cli] ✅ 重连成功")
                self._listen_task = asyncio.create_task(self._listen_loop())
                return
            except Exception as e:
                print(f"[cli] 重连失败: {type(e).__name__}: {e}")
        print("[cli] ❌ 重连 5 次均失败，退出")
        self._closing = True

    async def _handle(self, data):
        t = data.get("type")
        sid = data.get("session_id") or ""

        if t == "envelop_result":
            tid = data.get("trace_id") or ""
            # 找哪个 session 的 pending
            for ss in self._sessions.values():
                fut = ss.pending.pop(tid, None)
                if fut and not fut.done():
                    fut.set_result(data)
                    return
            return

        # 推送：按 sid 找会话
        if not sid or sid not in self._sessions:
            return
        ss = self._sessions[sid]
        is_cur = (sid == self._current)
        pfx = "" if is_cur else f"[{sid[:8]}] "

        if t == "summary_stream":
            chunk = data.get("chunk") or ""
            if chunk:
                ss.stream_buf += chunk
                if is_cur:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                else:
                    # 非当前会话：只在结束时有提示
                    pass
            return

        if t == "chat":
            content = data.get("content") or ""
            ss.stream_buf = ""
            ss.running_turn = False
            if content:
                if is_cur:
                    print(f"\n🤖 {content}\n")
                else:
                    print(f"\n{pfx}🤖 {content[:200]}\n")
            if is_cur:
                self._refresh_prompt()
            return

        if t == "progress":
            step = data.get("step") or ""
            status = data.get("status") or ""
            if step == "thinking":
                if is_cur:
                    sys.stdout.write("💭 ")
                    sys.stdout.flush()
            elif step == "analyzing":
                if is_cur:
                    sys.stdout.write("\n🔍 分析任务关联...\n")
            elif status == "start":
                target = data.get("target") or ""
                action = data.get("action") or ""
                label = target + (f" [{action}]" if action else "")
                if is_cur:
                    sys.stdout.write(f"\n⚡ {label}\n")
                    sys.stdout.flush()
                else:
                    print(f"{pfx}⚡ {label}")
            elif status == "ok":
                if is_cur:
                    sys.stdout.write(f"✅ {data.get('target','')}\n")
                    sys.stdout.flush()
            elif status == "error":
                if is_cur:
                    sys.stdout.write(f"❌ {data.get('target','')}: {data.get('error','')}\n")
                    sys.stdout.flush()
                else:
                    print(f"{pfx}❌ {data.get('target','')}: {data.get('error','')}")
            elif status == "interrupting":
                if is_cur:
                    sys.stdout.write("\n⏸️ 中断中...\n")
            elif status == "interrupted":
                ss.running_turn = False
                if is_cur:
                    print("⏸️ 已中断")
                    self._refresh_prompt()
                else:
                    print(f"{pfx}⏸️ 已中断")
            return

        if t == "error":
            ss.running_turn = False
            msg = data.get("error") or data.get("message") or ""
            if is_cur:
                print(f"\n❌ {msg}\n")
                self._refresh_prompt()
            else:
                print(f"{pfx}❌ {msg}")
            return

    def _refresh_prompt(self):
        """刷新 prompt 提示符（显示当前会话 + 附件数）。"""
        if not self._prompt:
            return
        try:
            self._prompt.message = self._make_prompt()
        except Exception:
            pass

    def _make_prompt(self):
        if not self._current or self._current not in self._sessions:
            return "› "
        ss = self._sessions[self._current]
        tag = self._current[:8]
        att = f" 📎{len(ss.attachments)}" if ss.attachments else ""
        run = " ⏳" if ss.running_turn else ""
        return f"[{tag}]{att}{run} › "

    # ------------------------------------------------------------
    # 请求-响应
    # ------------------------------------------------------------
    async def call(self, sid: str, receiver: str, payload: dict, timeout: float = 30.0) -> dict:
        ss = self._sessions.get(sid)
        if not ss:
            return {"ok": False, "error": f"session not opened: {sid}"}
        if not self.ws:
            print("[cli] ⚠️ WS 未连接，无法发送请求（正在重连中？）")
            return {"ok": False, "error": "ws not connected"}
        tid = uuid.uuid4().hex[:12]
        fut = asyncio.get_event_loop().create_future()
        ss.pending[tid] = fut
        try:
            # ★ os/_websocket 从 data.payload.meta 取 trace_id，必须把 meta 塞进 payload
            await self._send({
                "type": "envelop",
                "receiver": receiver,
                "payload": {**payload, "meta": {"trace_id": tid, "session_id": sid}},
            })
        except Exception as e:
            ss.pending.pop(tid, None)
            print(f"[cli] ⚠️ 发送请求失败: {type(e).__name__}: {e}")
            return {"ok": False, "error": f"send failed: {e}"}
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            ss.pending.pop(tid, None)
            print(f"[cli] ⚠️ 请求超时（{timeout}s）：{receiver} {payload.get('action') or ''}")
            return {"ok": False, "error": "timeout"}

    # ------------------------------------------------------------
    # 发对话
    # ------------------------------------------------------------
    async def send_chat(self, text: str):
        sid = self._current
        ss = self._ss()
        if ss.running_turn:
            print("[cli] 上一回合还在跑；/stop 中断，或等它结束")
            return

        # 处理 @file 引用
        content, attached = self._expand_at_files(text)
        # 拼手工 /attach 的文件
        for name, body in ss.attachments:
            attached.append((name, body))
        if attached:
            extra = "\n\n[附加文件]\n" + "\n\n".join(
                f"=== {n} ===\n{b}" for n, b in attached
            )
            content = content + extra
            ss.attachments.clear()

        ss.running_turn = True
        ss.stream_buf = ""
        print()
        r = await self.call(sid, "builtins/agents/main_agent",
                            {"content": content, "session_id": sid}, timeout=10.0)
        # 请求被拒 / 超时：立刻解锁，并提示
        p = r.get("payload") or r.get("data") or r
        if r.get("error") or (isinstance(p, dict) and p.get("ok") is False):
            err = r.get("error") or p.get("error") or "unknown"
            print(f"[cli] ⚠️ 发送失败：{err}")
            ss.running_turn = False
            self._refresh_prompt()
            return
        self._refresh_prompt()

    def _expand_at_files(self, text: str):
        """把 @path 替换为文件内容。返回 (new_text, [(name, content)])。"""
        import re
        parts = []
        attached = []
        last = 0
        for m in re.finditer(r"(?<!\S)@([^\s]+)", text):
            parts.append(text[last:m.start()])
            path = m.group(1)
            try:
                p = Path(path).expanduser()
                if not p.is_file():
                    parts.append(m.group(0))
                elif p.stat().st_size > MAX_ATTACH_BYTES:
                    parts.append(f"@{path}")
                    print(f"[cli] 跳过 {path}：超过 {MAX_ATTACH_BYTES // 1024}KB")
                else:
                    body = p.read_text(encoding="utf-8", errors="replace")
                    attached.append((p.name, body))
                    parts.append(f"@{path}")
            except Exception as e:
                parts.append(m.group(0))
                print(f"[cli] 读取 {path} 失败: {e}")
            last = m.end()
        parts.append(text[last:])
        return "".join(parts), attached

    # ------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------
    async def cmd_new(self):
        r = await self.call(self._current, "builtins/agents/main_agent", {"action": "new_conversation"})
        p = r.get("payload") or r.get("data") or r
        if p.get("ok"):
            await self._open_session(p.get("conversation_id") or "")
        else:
            print(f"[cli] 新建失败: {p.get('error')}")

    async def cmd_list(self):
        r = await self.call(self._current, "builtins/agents/main_agent", {"action": "list_conversations"})
        p = r.get("payload") or r.get("data") or r
        convs = p.get("conversations") or []
        if not convs:
            print("[cli] 暂无会话")
            return
        print(f"{'ID':<20} {'标题':<30} {'更新'}")
        for c in convs[:30]:
            mark = "*" if c.get("id") == self._current else " "
            opened = "+" if c.get("id") in self._sessions else " "
            title = (c.get("title") or "")[:28]
            print(f"{mark}{opened}{c.get('id',''):<20} {title:<30} {c.get('updated_at','')}")

    async def cmd_resume(self, sid: str):
        if not sid:
            print("[cli] 用法: /resume <sid>")
            return
        await self._open_session(sid)

    async def cmd_sessions(self):
        if not self._sessions:
            print("[cli] 无打开的会话")
            return
        print("打开的会话：")
        for sid, ss in self._sessions.items():
            mark = "*" if sid == self._current else " "
            run = " ⏳" if ss.running_turn else ""
            print(f" {mark} {sid}  {(ss.title or '')[:40]}{run}")

    async def cmd_switch(self, sid: str):
        if sid in self._sessions:
            self._current = sid
            print(f"[cli] 切到 {sid}")
            self._refresh_prompt()
        else:
            print(f"[cli] {sid} 未打开；/resume 打开它")

    async def cmd_close(self, sid: str):
        if sid not in self._sessions:
            print(f"[cli] {sid} 未打开")
            return
        if sid == self._current:
            others = [s for s in self._sessions if s != sid]
            if not others:
                print("[cli] 不能关闭唯一会话")
                return
            self._current = others[0]
        self._sessions.pop(sid, None)
        print(f"[cli] 关闭 {sid}，当前 {self._current}")

    async def cmd_history(self, arg: str):
        sid = self._current
        ss = self._ss()
        n = 10
        if arg.strip().isdigit():
            n = int(arg.strip())
        r = await self.call(sid, "builtins/agents/main_agent",
                            {"action": "get_session", "session_id": sid, "limit": n})
        p = r.get("payload") or r.get("data") or r
        flow = p.get("flow") or []
        ss.oldest_ts = p.get("oldest_ts") or ""
        ss.has_more = bool(p.get("has_more"))
        if not flow:
            print("[cli] 当前会话为空")
            return
        self._print_flow(flow)
        if ss.has_more:
            print(f"[cli] 还有更早的，/more 继续")

    async def cmd_more(self):
        ss = self._ss()
        if not ss.has_more or not ss.oldest_ts:
            print("[cli] 没有更早的了")
            return
        r = await self.call(ss.sid, "builtins/agents/main_agent",
                            {"action": "get_session", "session_id": ss.sid,
                             "limit": 10, "before_ts": ss.oldest_ts})
        p = r.get("payload") or r.get("data") or r
        flow = p.get("flow") or []
        ss.oldest_ts = p.get("oldest_ts") or ""
        ss.has_more = bool(p.get("has_more"))
        if not flow:
            print("[cli] 没有更早的了")
            ss.has_more = False
            return
        self._print_flow(flow)
        if ss.has_more:
            print(f"[cli] 还有更早的，/more 继续")

    def _print_flow(self, flow):
        for e in flow:
            frm = e.get("from")
            if frm == "user":
                print(f"👤 {(e.get('content') or '')[:300]}")
            elif frm == "ai" and e.get("call") == "reply":
                print(f"🤖 {(e.get('content') or '')[:500]}")
            elif frm == "system" and e.get("action") == "call_start":
                d = e.get("detail") or {}
                print(f"  ⚡ {d.get('target','')} [{d.get('action','')}]")
        print()

    async def cmd_stop(self):
        ss = self._ss()
        if not ss.running_turn:
            print("[cli] 当前没有运行中的回合")
            return
        r = await self.call(ss.sid, "builtins/agents/main_agent",
                            {"action": "interrupt", "session_id": ss.sid, "content": ""})
        p = r.get("payload") or r.get("data") or r
        if p.get("ok"):
            print("[cli] 已发送中断请求")
        else:
            print(f"[cli] 中断失败: {p.get('error')}")

    def cmd_attach(self, path: str):
        ss = self._ss()
        if not path:
            print("[cli] 用法: /attach <file>")
            return
        try:
            p = Path(path).expanduser()
            if not p.is_file():
                print(f"[cli] 文件不存在: {path}")
                return
            if p.stat().st_size > MAX_ATTACH_BYTES:
                print(f"[cli] 超过 {MAX_ATTACH_BYTES // 1024}KB 限制")
                return
            body = p.read_text(encoding="utf-8", errors="replace")
            ss.attachments.append((p.name, body))
            print(f"[cli] 附加: {p.name}（{len(body)} 字），共 {len(ss.attachments)} 个")
            self._refresh_prompt()
        except Exception as e:
            print(f"[cli] 附加失败: {e}")

    def cmd_attachments(self):
        ss = self._ss()
        if not ss.attachments:
            print("[cli] 无附件")
            return
        for i, (n, b) in enumerate(ss.attachments, 1):
            print(f"  {i}. {n}（{len(b)} 字）")

    def cmd_clear_attach(self):
        self._ss().attachments.clear()
        print("[cli] 已清空附件")
        self._refresh_prompt()

    # ------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------
    async def run(self, initial_sid: str):
        try:
            await self.connect(initial_sid)
            # prompt_toolkit 历史
            hist_file = Path.home() / ".aicp_cli_history"
            self._prompt = PromptSession(history=FileHistory(str(hist_file)))
            print("输入内容开始对话；/help 看命令；Ctrl-C 退出\n")
            with patch_stdout():
                while True:
                    try:
                        line = await self._prompt.prompt_async(self._make_prompt())
                    except (EOFError, KeyboardInterrupt):
                        # 不打印：break 后 finally -> shutdown() 统一输出
                        break
                    text = (line or "").strip()
                    if not text:
                        continue
                    if text.startswith("/"):
                        if await self._handle_cmd(text):
                            break
                        continue
                    await self.send_chat(text)
        finally:
            await self.shutdown()

    async def shutdown(self):
        """优雅关闭：标记退出 → 取消监听/重连任务 → 取消挂起请求 → 关 WS。"""
        self._closing = True
        # 取消所有未完成的请求 future，避免事件循环关闭时吐 Future pending 警告
        for ss in self._sessions.values():
            for fut in ss.pending.values():
                if not fut.done():
                    fut.cancel()
            ss.pending.clear()
        task = self._listen_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                # 任务被 cancel 抛 CancelledError（继承 BaseException），吞掉即可
                pass
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
        self._listen_task = None
        print("[cli] 已退出")

    async def _handle_cmd(self, text):
        parts = text.split(maxsplit=1)
        cmd = parts[0]
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("/quit", "/q"):
            # 只发信号：run() 收到 True 后 break，finally 里统一 shutdown()
            return True
        elif cmd == "/new":
            await self.cmd_new()
        elif cmd == "/list":
            await self.cmd_list()
        elif cmd == "/resume":
            await self.cmd_resume(arg)
        elif cmd == "/sessions":
            await self.cmd_sessions()
        elif cmd == "/switch":
            await self.cmd_switch(arg)
        elif cmd == "/close":
            await self.cmd_close(arg)
        elif cmd == "/history":
            await self.cmd_history(arg)
        elif cmd == "/more":
            await self.cmd_more()
        elif cmd == "/stop":
            await self.cmd_stop()
        elif cmd == "/attach":
            self.cmd_attach(arg)
        elif cmd == "/attachments":
            self.cmd_attachments()
        elif cmd == "/clear-attach":
            self.cmd_clear_attach()
        elif cmd == "/help":
            print(__doc__)
        else:
            print(f"[cli] 未知命令: {cmd}（/help）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:9001/ws")
    ap.add_argument("--session", default="")
    ap.add_argument("--token", default=os.environ.get("AICP_TOKEN", ""))
    args = ap.parse_args()
    sid = args.session or f"s_{uuid.uuid4().hex[:12]}"
    cli = AICPCLI(args.url, session_id=sid, token=args.token)
    try:
        asyncio.run(cli.run(sid))
    except KeyboardInterrupt:
        pass  # shutdown() 已在 run() 的 finally 里执行并打印
    except ConnectionError as e:
        print(f"\n[cli] ❌ 连接错误：{e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n[cli] ❌ 未预期错误：{type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
