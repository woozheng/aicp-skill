"""AICP System — Server 版"""
import asyncio
import logging
import time

import core

logger = logging.getLogger(__name__)


# ---- status 兜底:core 里没有就本地定义,避免 AttributeError 导致二次崩溃 ----
_STATUS_TIMEOUT = getattr(core, "STATUS_TIMEOUT", "timeout")
_STATUS_ERROR = getattr(core, "STATUS_ERROR", "error")
_STATUS_CANCELLED = getattr(core, "STATUS_CANCELLED", "cancelled")


def _safe_envelop(sender, receiver, payload, status):
    """构造 Envelop;构造本身失败时返回一个最小 dict 兜底,保证调用方永远拿到东西。"""
    try:
        return core.Envelop(
            sender=sender,
            receiver=receiver,
            payload=payload,
            status=status,
        )
    except Exception:
        logger.exception("failed to construct Envelop, falling back to dict")
        return {
            "sender": sender,
            "receiver": receiver,
            "payload": payload,
            "status": status,
            "_fallback": True,
        }


class SystemServer:
    """Server 版 — 无 GUI,无记忆"""

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
        if not self._viz_enabled or self._viz_task is not None:
            return
        self._viz_task = asyncio.create_task(self._viz_worker())

    async def _viz_worker(self):
        while True:
            try:
                item = await self._viz_queue.get()
                await self._push_event(*item)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 不再静默吞掉,落日志
                logger.exception("viz worker iteration failed")

    async def _push_event(self, direction, envelop, response=None):
        try:
            from_sid, to_sid = self._sessions_for(envelop)
            from_agent, to_agent = self._agents_for(envelop)
            uid = self._user_for(envelop)
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
                        "from_agent_id": from_agent,
                        "to_agent_id": to_agent,
                        "from_session_id": from_sid,
                        "from_session_title": self._session_title(from_sid, uid),
                        "to_session_id": to_sid,
                        "to_session_title": self._session_title(to_sid, uid),
                    },
                },
            ), self._agent)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("push_event failed")

    # ---------- 会话 / agent 解析 ----------
    def _pick_sid(self, meta, payload, include_callback=True):
        """按 meta → payload → payload.data 顺序取 session_id。

        include_callback=False 时不采信 callback_session_id——发送侧不属于回调目标。
        """
        if isinstance(meta, dict):
            sid = ""
            if include_callback:
                sid = meta.get("callback_session_id") or ""
            sid = sid or meta.get("session_id") or ""
            if sid:
                return str(sid)
        if isinstance(payload, dict):
            sid = payload.get("session_id") or ""
            if sid:
                return str(sid)
            data = payload.get("data")
            if isinstance(data, dict):
                sid = data.get("session_id") or ""
                if sid:
                    return str(sid)
        return ""

    def _sessions_for(self, envelop):
        """返回 (from_sid, to_sid)。

        发送侧不采信 callback_session_id（那是回调目标的会话）；
        接收侧优先取 callback_session_id，其次与发送侧同一会话。
        """
        payload = getattr(envelop, "payload", None) or {}
        meta = getattr(envelop, "meta", None) or {}
        from_sid = self._pick_sid(meta, payload, include_callback=False)
        to_sid = self._pick_sid(meta, payload, include_callback=True)
        return from_sid, to_sid

    def _agents_for(self, envelop):
        """从 envelop.meta 解析 agent_id（sender 侧 / receiver 侧）。

        meta.agent_id      —— 当前执行上下文的 agent（新架构）
        meta.callback_agent_id —— 回调目标 agent
        meta.sender_agent_id / meta.receiver_agent_id —— 显式两端
        """
        payload = getattr(envelop, "payload", None) or {}
        meta = getattr(envelop, "meta", None) or {}
        if not isinstance(meta, dict):
            meta = {}
        payload = payload if isinstance(payload, dict) else {}

        def _pay(key):
            sid = payload.get(key) or ""
            if sid:
                return str(sid)
            data = payload.get("data")
            if isinstance(data, dict):
                return str(data.get(key) or "")
            return ""

        sender_agent = str(
            meta.get("sender_agent_id")
            or meta.get("agent_id")
            or _pay("agent_id")
            or ""
        )
        receiver_agent = str(
            meta.get("receiver_agent_id")
            or meta.get("callback_agent_id")
            or _pay("receiver_agent_id")
            or _pay("agent_id")
            or ""
        )
        return sender_agent, receiver_agent

    def _user_for(self, envelop):
        """取 user_id：meta 优先，其次 payload 顶层 / payload.data。"""
        meta = getattr(envelop, "meta", None) or {}
        payload = getattr(envelop, "payload", None) or {}
        if isinstance(meta, dict):
            uid = meta.get("user_id") or ""
            if uid:
                return str(uid)
        if isinstance(payload, dict):
            uid = payload.get("user_id") or ""
            if uid:
                return str(uid)
            data = payload.get("data")
            if isinstance(data, dict):
                uid = data.get("user_id") or ""
                if uid:
                    return str(uid)
        return ""

    def _session_title(self, sid, user_id=""):
        if not sid:
            return ""
        now = time.time()
        cache_key = (user_id or "", sid)
        cached = self._title_cache.get(cache_key)
        if cached and now - cached[0] < 15:
            return cached[1]
        try:
            title = self._lookup_title(sid, user_id)
        except Exception:
            logger.exception("lookup_title failed for sid=%r", sid)
            title = ""
        self._title_cache[cache_key] = (now, title)
        return title

    def _lookup_title(self, sid, user_id=""):
        """从用户级会话索引里查标题。

        新架构：data/memories/users/<uid>/index.json
        旧架构：data/memories/main_agent/users/<uid>/sessions/index.json

        已知 user_id 时直接定位该用户目录；否则退回全目录扫描。
        """
        try:
            import json
            from pathlib import Path
            new_base = Path("data") / "memories" / "users"
            old_base = Path("data") / "memories" / "main_agent" / "users"

            def _read_title(idx, sid):
                if not idx.exists():
                    return ""
                try:
                    data = json.loads(idx.read_text(encoding="utf-8"))
                except Exception:
                    return ""
                conv = (data.get("conversations") or {}).get(sid) or {}
                return (conv.get("title") or "").strip()

            # ---- 精确路径：按 user_id 直接定位 ----
            if user_id:
                safe_uid = str(user_id).replace("/", "_").replace("\\", "_")
                for base, sub in ((new_base, ""), (old_base, "sessions")):
                    idx = base / safe_uid / (sub + "/index.json" if sub else "index.json")
                    title = _read_title(idx, sid)
                    if title:
                        return title
                # 精确路径未命中且用户目录确实存在 -> 不必再全扫
                if (new_base / safe_uid).exists() or (old_base / safe_uid).exists():
                    return ""

            # ---- 回退：全目录扫描 ----
            candidates = [new_base]
            if old_base.exists():
                candidates.append(old_base)

            for base in candidates:
                if not base.exists():
                    continue
                for user_dir in base.iterdir():
                    if not user_dir.is_dir():
                        continue
                    indexes = [user_dir / "index.json"]
                    if base == old_base:
                        indexes.append(user_dir / "sessions" / "index.json")
                    for idx in indexes:
                        title = _read_title(idx, sid)
                        if title:
                            return title
        except Exception:
            logger.exception("lookup_title scan failed")
        return ""

    # ---------- 核心:永不意外中断的 call ----------
    async def call(self, envelop, timeout=None):
        """对调用方承诺:除 CancelledError 外永不抛异常,永远返回 Envelop(或 dict 兜底)。

        返回 status 可能是:
          - 正常:route 返回的 Envelop
          - STATUS_TIMEOUT:超时
          - STATUS_ERROR:route 内部业务异常
          - "_fallback":连 Envelop 都构造不出来时的 dict 兜底(极端情况)
        """
        sender = getattr(envelop, "sender", None)
        receiver = getattr(envelop, "receiver", None)

        # 可视化推送(out)—— 任何失败都不影响主流程
        if self._viz_enabled:
            try:
                self._viz_queue.put_nowait(("out", envelop))
            except asyncio.QueueFull:
                pass
            except Exception:
                logger.exception("viz enqueue (out) failed")

        route_timeout = timeout
        call_timeout = (timeout + 5.0) if timeout is not None else 30.0

        # ---- 构造 task:ensure_future 也可能同步抛 ----
        try:
            if not callable(self._route):
                raise TypeError(f"route_fn is not callable: {type(self._route)!r}")
            if route_timeout is not None:
                coro = self._route(envelop, self._agent, timeout=route_timeout)
            else:
                coro = self._route(envelop, self._agent)
            task = asyncio.ensure_future(coro)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("failed to schedule route task")
            response = _safe_envelop(
                sender=receiver,
                receiver=sender,
                payload={"ok": False, "error": f"{type(e).__name__}: {e}"},
                status=_STATUS_ERROR,
            )
            self._viz_enqueue_in(envelop, response)
            return response

        # ---- 等待 task,分类兜底 ----
        try:
            response = await asyncio.wait_for(task, timeout=call_timeout)
        except asyncio.TimeoutError:
            # 超时:cancel 并短暂等待清理,任何清理异常都不外泄
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception:
                # cancel 期间 task 抛了非 CancelledError 的业务异常 —— 吞掉,落日志
                logger.exception("route task raised during cancel cleanup")
            response = _safe_envelop(
                sender=receiver,
                receiver=sender,
                payload={"ok": False, "error": f"route timeout after {call_timeout}s"},
                status=_STATUS_TIMEOUT,
            )
        except asyncio.CancelledError:
            # 调用方主动取消 call() —— 必须让取消语义向上传播
            raise
        except Exception as e:
            # route 内部业务异常 —— 转成失败 Envelop,不击穿 call 边界
            logger.exception("route raised exception")
            response = _safe_envelop(
                sender=receiver,
                receiver=sender,
                payload={"ok": False, "error": f"{type(e).__name__}: {e}"},
                status=_STATUS_ERROR,
            )

        # 可视化推送(in)—— 任何失败都不影响返回
        self._viz_enqueue_in(envelop, response)

        return response

    def _viz_enqueue_in(self, envelop, response):
        """可视化入队(in),独立方法方便复用与兜底。"""
        if not self._viz_enabled:
            return
        try:
            self._viz_queue.put_nowait(("in", envelop, response))
        except asyncio.QueueFull:
            pass
        except Exception:
            logger.exception("viz enqueue (in) failed")


System = SystemServer
