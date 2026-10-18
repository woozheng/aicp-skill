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
                    },
                },
            ), self._agent)
        except Exception:
            pass

    async def call(self, envelop):
        # ---------- 可视化推送（out）----------
        if self._viz_enabled:
            try:
                self._viz_queue.put_nowait(("out", envelop))
            except asyncio.QueueFull:
                pass

        # 直接执行插件，不激活记忆、不提取记忆
        response = await self._route(envelop, self._agent)

        # ---------- 可视化推送（in）----------
        if self._viz_enabled:
            try:
                self._viz_queue.put_nowait(("in", envelop, response))
            except asyncio.QueueFull:
                pass

        return response


System = SystemServer
