"""AICP System — Server 版"""
import asyncio
import core
import time


class SystemServer:
    """Server 版 — 无 GUI，无记忆"""
    def __init__(self, loop, route_fn, config=None):
        self.loop = loop
        self._route = route_fn
        self._agent = None

        # ---------- 可视化配置 ----------
        self._viz_enabled = False
        self._viz_channel = "pa_visualizer"
        self._viz_queue = None
        self._viz_task = None
        self._title_cache = {}   # sid -> (ts, title)

        if config:
            viz = config.get("visualization", {})
            if isinstance(viz, dict):
                self._viz_enabled = viz.get("enabled", False)
                self._viz_channel = viz.get("channel", "pa_visualizer")
                queue_size = viz.get("queue_size", 1000)
            elif viz is True:
                self._viz_enabled = True
                queue_size = 1000
            else:
                queue_size = 1000
        else:
            queue_size = 1000

        if self._viz_enabled:
            self._viz_queue = asyncio.Queue(maxsize=queue_size)

    def bind(self, agent):
        self._agent = agent

    # ---------- 可视化 ----------
    async def start_visualizer(self):
        """启动可视化 worker（如果开启）"""
        if not self._viz_enabled or self._viz_task is not None:
            return
        self._viz_task = asyncio.create_task(self._viz_worker())

    async def _viz_worker(self):
        """后台 worker：从队列取事件，推 WebSocket"""
        while True:
            try:
                item = await self._viz_queue.get()
                await self._push_event(*item)
            except Exception:
                pass

    async def _push_event(self, direction, envelop, response=None):
        """推送可视化事件（直接走 route，避免递归）"""
        try:
            from_sid, to_sid = self._sessions_for(envelop)
            await self._route(core.Envelop(
                sender="os/_visualizer",
                receiver="os/_websocket",
                payload={
                    "action": "push",
                    "channel_id": self._viz_channel,
                    "data": {
                        "type": "envelop",
                        "direction": direction,
                        "sender": envelop.sender,
                        "receiver": envelop.receiver,
                        "intent": envelop.intent,
                        "trace_id": envelop.trace_id,
                        "timestamp": time.time(),
                        "has_response": response is not None,
                        # ★ 会话维度：main_agent 是 session 级插件，
                        #   同一路由可能对应多个会话，渲染时按 session 拆节点。
                        "from_session_id": from_sid,
                        "from_session_title": self._session_title(from_sid),
                        "to_session_id": to_sid,
                        "to_session_title": self._session_title(to_sid),
                    },
                },
            ), self._agent)
        except Exception:
            pass

    # ---------- 会话解析（可视化用） ----------

    def _sessions_for(self, envelop):
        """返回 (sender 侧 session_id, receiver 侧 session_id)。

        meta.session_id 是发起方的会话（main_agent 调插件时写入）；
        payload.session_id 是目标方的会话（网关调 main_agent 时由前端指定）。
        """
        payload = getattr(envelop, "payload", None) or {}
        meta = getattr(envelop, "meta", None) or {}
        meta_sid = ""
        if isinstance(meta, dict):
            meta_sid = meta.get("callback_session_id") or meta.get("session_id") or ""
        payload_sid = ""
        if isinstance(payload, dict):
            payload_sid = payload.get("session_id") or ""
            if not payload_sid:
                data = payload.get("data")
                if isinstance(data, dict):
                    payload_sid = data.get("session_id") or ""
        return str(meta_sid or ""), str(payload_sid or meta_sid or "")

    def _session_title(self, sid):
        """取 session 的会话名（对话标题）。15s 缓存，避免每条事件都读盘。"""
        if not sid:
            return ""
        now = time.time()
        cached = self._title_cache.get(sid)
        if cached and now - cached[0] < 15:
            return cached[1]
        title = self._lookup_title(sid)
        self._title_cache[sid] = (now, title)
        return title

    def _lookup_title(self, sid):
        """扫 main_agent 各 user 的会话索引，找 sid 对应的标题。"""
        try:
            import json
            from pathlib import Path
            base = Path("data") / "memories" / "main_agent" / "users"
            if not base.exists():
                return ""
            for user_dir in base.iterdir():
                if not user_dir.is_dir():
                    continue
                idx = user_dir / "sessions" / "index.json"
                if not idx.exists():
                    continue
                try:
                    data = json.loads(idx.read_text(encoding="utf-8"))
                except Exception:
                    continue
                meta = (data.get("conversations") or {}).get(sid) or {}
                title = (meta.get("title") or "").strip()
                if title:
                    return title
        except Exception:
            pass
        return ""

    async def call(self, envelop, timeout=None):
        # 可视化推送（out）
        if self._viz_enabled:
            try:
                self._viz_queue.put_nowait(("out", envelop))
            except asyncio.QueueFull:
                pass

        # ★ 有 timeout 时传给 route；同时 call 自己加一个兜底超时（比 route 的 timeout 略长）
        route_timeout = timeout
        call_timeout = (timeout + 5.0) if timeout is not None else 30.0

        task = asyncio.ensure_future(
            self._route(envelop, self._agent, timeout=route_timeout)
            if route_timeout is not None
            else self._route(envelop, self._agent)
        )
        try:
            response = await asyncio.wait_for(task, timeout=call_timeout)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            # ★ 超时返回一个“失败 Envelop”，不抛异常，让调用方拿到 status
            response = core.Envelop(
                sender=envelop.receiver,
                receiver=envelop.sender,
                payload={"ok": False, "error": f"route timeout after {call_timeout}s"},
                status=core.STATUS_TIMEOUT,
            )

        # 可视化推送（in）
        if self._viz_enabled:
            try:
                self._viz_queue.put_nowait(("in", envelop, response))
            except asyncio.QueueFull:
                pass

        return response


System = SystemServer
