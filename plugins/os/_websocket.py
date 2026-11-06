"""WebSocket 网关插件 — 复用 HTTP 路由格式，共享 _auth 认证

改造点（v2）：
  - is_local_request 检查 X-Forwarded-For：反向代理后不再误判本机
  - ws_handler 透传 remote / forwarded_for 给 os/_auth，local/hybrid 护栏生效
  - 握手成功后把 user_id 挂到 ws 对象，供后续按用户隔离用
  - token 提取顺序：Sec-WebSocket-Protocol > Header > Cookie > Query

改造点（v3）：
  - push / broadcast / push_to_ws / close_channel 全部加 2s 单次超时
  - 发送超时或异常即剔除死连接，避免慢/死连接阻塞整个 push
  - push 返回 sent 数，死连接已 unregister，下次不再尝试
"""
import asyncio
import json
import time
import uuid
import aiohttp
from aiohttp import web
import core

import importlib
import sys

# ★ 延迟导入 _service_restart：加载器按文件名字母序遍历 plugins/**/*.py，
#   os/_service_restart.py 排在本文件之后（s > w），模块级 import 会因
#   「还没加载到」抛 ModuleNotFoundError → 引擎起不来。只在要用时 import。


def _sr():
    """延迟取得 _service_restart 模块（见上方注释：不能模块级 import）。"""
    import importlib
    return importlib.import_module("plugin_os__service_restart")


# 单次发送超时（秒）。对端不读 -> 超时 -> 剔除，避免 push 卡死。
WS_SEND_TIMEOUT = 0.5


class WebSocketGateway:
    def __init__(self):
        self._channels = {}
        self._ws_to_channels = {}
        self._ws_id = {}
        self._send_fail_count: Dict[Any, int] = {}

    def register(self, ws, channel_id):
        if channel_id not in self._channels:
            self._channels[channel_id] = set()
        self._channels[channel_id].add(ws)
        if ws not in self._ws_to_channels:
            self._ws_to_channels[ws] = set()
        self._ws_to_channels[ws].add(channel_id)
        if ws not in self._ws_id:
            self._ws_id[ws] = f"ws_{uuid.uuid4().hex[:6]}"

    def unregister(self, ws, channel_id=None):
        if channel_id:
            self._channels.get(channel_id, set()).discard(ws)
            if channel_id in self._channels and not self._channels[channel_id]:
                del self._channels[channel_id]
            self._ws_to_channels.get(ws, set()).discard(channel_id)
        else:
            for cid in list(self._ws_to_channels.get(ws, set())):
                self._channels.get(cid, set()).discard(ws)
                if cid in self._channels and not self._channels[cid]:
                    del self._channels[cid]
            self._ws_to_channels.pop(ws, None)
        self._ws_id.pop(ws, None)

    async def push(self, channel_id, data):
        """向 channel 推送。并发发送，单次 send 超时 0.5s，close 超时 0.1s。"""
        targets = [ws for ws in list(self._channels.get(channel_id, set())) if not ws.closed]
        if not targets:
            return 0
        results = await asyncio.gather(
            *[self._push_one(ws, channel_id, data) for ws in targets],
            return_exceptions=True,
        )
        return sum(1 for r in results if r is True)

    async def _push_one(self, ws, channel_id, data):
        """单个连接发送。True=成功，False=失败已剔除。"""
        try:
            await asyncio.wait_for(ws.send_json(data), timeout=WS_SEND_TIMEOUT)
            self._send_fail_count.pop(ws, None)
            return True
        except asyncio.TimeoutError:
            cnt = self._send_fail_count.get(ws, 0) + 1
            self._send_fail_count[ws] = cnt
            print(f"[WS Gateway] push 超时（{WS_SEND_TIMEOUT}s）第 {cnt} 次，"
                  f"强制关闭 channel={channel_id}")
            try:
                # ★ close 超时从 1.0 降到 0.1：只发 FIN，不等对端
                await asyncio.wait_for(ws.close(code=1001), timeout=0.1)
            except Exception:
                pass
            self.unregister(ws)
            self._send_fail_count.pop(ws, None)
            return False
        except Exception as e:
            print(f"[WS Gateway] push 发送异常: {e}")
            self.unregister(ws)
            self._send_fail_count.pop(ws, None)
            return False

    async def broadcast(self, data):
        """向所有连接广播。并发发送，单次 send 超时 0.5s。"""
        targets = [ws for ws in list(self._ws_to_channels.keys()) if not ws.closed]
        if not targets:
            return 0
        results = await asyncio.gather(
            *[self._push_one(ws, "_broadcast", data) for ws in targets],
            return_exceptions=True,
        )
        return sum(1 for r in results if r is True)

    def get_status(self):
        channels_info = {}
        for cid, ws_set in self._channels.items():
            channels_info[cid] = {
                "connections": len(ws_set),
                "ws_ids": [self._ws_id.get(ws, "?") for ws in ws_set if not ws.closed],
            }
        return {
            "total_channels": len(self._channels),
            "total_connections": len(self._ws_to_channels),
            "channels": channels_info,
        }


def extract_token_from_request(request):
    """从请求提取 token。顺序：Sec-WebSocket-Protocol > Header > Cookie > Query。

    Query 放最后：URL 里的 token 会进 access log / 代理日志 / 浏览器历史。
    """
    # 1. Sec-WebSocket-Protocol（浏览器子协议，不进日志）
    proto = request.headers.get("Sec-WebSocket-Protocol", "")
    for part in proto.split(","):
        part = part.strip()
        if part.startswith("token."):
            return part[6:]

    # 2. 自定义 Header
    token = request.headers.get("X-AICP-Token", "")
    if token:
        return token

    # 3. Bearer
    auth_header = request.headers.get("Authorization", "")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header[7:]

    # 4. Cookie
    if hasattr(request, "cookies"):
        token = request.cookies.get("aicp_token", "")
        if token:
            return token

    # 5. Query（兜底，会进日志）
    return request.query.get("token", "")


def is_local_request(request):
    """有 X-Forwarded-For 说明经过代理，一律视为非本机。"""
    if request.headers.get("X-Forwarded-For", ""):
        return False
    return request.remote in ("127.0.0.1", "localhost", "::1")


def is_same_origin(request):
    origin = request.headers.get("Origin", "")
    if not origin:
        return False
    from urllib.parse import urlparse
    try:
        o = urlparse(origin)
        fwd_host = request.headers.get("X-Forwarded-Host", "")
        fwd_proto = request.headers.get("X-Forwarded-Proto", "https")
        if fwd_host:
            s = urlparse(f"{fwd_proto}://{fwd_host}")
            return o.hostname == s.hostname
        s = urlparse(f"http://{request.host}")
        return o.hostname == s.hostname
    except Exception:
        return False


async def execute(envelop, agent):
    gateway = getattr(agent, "ws_gateway", None)
    action = envelop.payload.get("action", "")

    if action == "START":
        # 起 WebSocket 服务（幂等：已在跑就先停）
        host = envelop.payload.get("host", getattr(agent, "_ws_host", "0.0.0.0"))
        port = int(envelop.payload.get("port", getattr(agent, "_ws_port", 9001)))
        if getattr(agent, "_ws_runner", None) is not None:
            await _stop_ws(agent)
        runner = await start_websocket_server(agent, host=host, port=port)
        envelop.payload = {"ok": True, "status": "listening", "host": host, "port": port}
        return envelop

    if action == "STOP":
        await _stop_ws(agent)
        envelop.payload = {"ok": True, "status": "stopped"}
        return envelop

    if action == "restart":
        # ★ 优雅重启：释放端口 + 用新代码重新起
        host = getattr(agent, "_ws_host", "0.0.0.0")
        port = int(getattr(agent, "_ws_port", 9001))
        ok = await _sr().restart_service_and_watch(
            agent, "os/_websocket", port=port, host=host,
            start_payload={"action": "START", "host": host, "port": port},
        )
        envelop.payload = {"ok": ok, "status": "restarted" if ok else "restart_failed",
                           "port": port}
        return envelop

    if action == "push":
        channel_id = envelop.payload.get("channel_id", "")
        data = envelop.payload.get("data", {})
        if not channel_id:
            envelop.payload = {"error": "channel_id required"}
            return envelop
        data["_meta"] = {
            "sender": envelop.sender,
            "intent": envelop.intent,
            "trace_id": envelop.trace_id,
            "timestamp": time.time(),
        }
        if gateway:
            sent = await gateway.push(channel_id, data)
            envelop.payload = {"ok": True, "sent": sent, "channel_id": channel_id}
        else:
            envelop.payload = {"error": "ws_gateway not ready"}
        return envelop

    elif action == "broadcast":
        data = envelop.payload.get("data", {})
        data["_meta"] = {
            "sender": envelop.sender,
            "trace_id": envelop.trace_id,
            "timestamp": time.time(),
        }
        sent = await gateway.broadcast(data) if gateway else 0
        envelop.payload = {"ok": True, "sent": sent}
        return envelop

    elif action == "push_to_ws":
        ws_id = envelop.payload.get("ws_id", "")
        data = envelop.payload.get("data", {})
        if not ws_id:
            envelop.payload = {"error": "ws_id required"}
            return envelop
        data["_meta"] = {
            "sender": envelop.sender,
            "trace_id": envelop.trace_id,
            "timestamp": time.time(),
        }
        if gateway:
            sent = 0
            dead = []
            for ws, wid in list(gateway._ws_id.items()):
                if wid == ws_id and not ws.closed:
                    try:
                        await asyncio.wait_for(ws.send_json(data), timeout=WS_SEND_TIMEOUT)
                        sent += 1
                        gateway._send_fail_count.pop(ws, None)
                    except asyncio.TimeoutError:
                        cnt = gateway._send_fail_count.get(ws, 0) + 1
                        gateway._send_fail_count[ws] = cnt
                        agent.log.info(
                            f"[WS] push_to_ws 超时（{WS_SEND_TIMEOUT}s）第 {cnt} 次，强制关闭"
                        )
                        try:
                            await asyncio.wait_for(ws.close(code=1001), timeout=1.0)
                        except Exception:
                            pass
                        dead.append(ws)
                    except Exception as e:
                        agent.log.info(f"[WS] push_to_ws error: {e}")
                        dead.append(ws)
            for ws in dead:
                gateway.unregister(ws)
                gateway._send_fail_count.pop(ws, None)
            envelop.payload = {"ok": True, "sent": sent, "ws_id": ws_id}
        else:
            envelop.payload = {"error": "ws_gateway not ready"}
        return envelop

    elif action == "status":
        envelop.payload = gateway.get_status() if gateway else {"error": "not started"}
        return envelop

    elif action == "close_channel":
        channel_id = envelop.payload.get("channel_id", "")
        if gateway:
            for ws in list(gateway._channels.get(channel_id, set())):
                try:
                    await asyncio.wait_for(
                        ws.close(code=1000, message=b"Channel closed by server"),
                        timeout=WS_SEND_TIMEOUT,
                    )
                except (asyncio.TimeoutError, Exception):
                    pass
                gateway.unregister(ws, channel_id)
        envelop.payload = {"ok": True, "channel_id": channel_id}
        return envelop

    else:
        envelop.payload = {"error": f"Unknown action: {action}"}
        return envelop


async def _stop_ws(agent):
    """停掉 WebSocket 服务，释放端口。"""
    runner = getattr(agent, "_ws_runner", None)
    if runner:
        try:
            await runner.cleanup()
        except Exception:
            pass
        agent._ws_runner = None
    # 兜底：老代码没存 runner 时端口可能被孤儿 socket 占着
    try:
        port = int(getattr(agent, "_ws_port", 9001))
        await _sr().kill_port_holders(port)
    except Exception:
        pass


async def start_websocket_server(agent, host="0.0.0.0", port=9001, skip_auth_for_local=False):
    """启动 WebSocket 服务器"""
    gateway = WebSocketGateway()
    agent.ws_gateway = gateway
    app = web.Application()

    async def ws_handler(request):
        token = extract_token_from_request(request)
        channel_id = request.query.get("channel", f"default_{uuid.uuid4().hex[:6]}")

        origin = request.headers.get("Origin", "")
        is_same_orig = is_same_origin(request)
        is_local = is_local_request(request)
        should_skip_auth = is_local or is_same_orig

        # ---- 鉴权 ----
        user_id = "local"
        if not should_skip_auth:
            auth_env = core.Envelop(
                sender="os/_websocket",
                receiver="os/_auth",
                payload={"action": "verify"},
                meta={
                    "token": token,
                    # ★ 透传来源，让 os/_auth 的 local/hybrid 护栏生效
                    "remote": request.remote or "",
                    "forwarded_for": request.headers.get("X-Forwarded-For", ""),
                },
            )
            auth_result = await agent.system.call(auth_env)

            if not auth_result or not auth_result.payload.get("ok"):
                agent.log.warning(f"[WS] Auth failed for {request.remote}")
                ws = web.WebSocketResponse()
                await ws.prepare(request)
                await ws.close(code=4001, message=b"Unauthorized")
                return ws

            user_id = auth_result.payload.get("user_id", "local")

        # ---- 握手 ----
        ws = web.WebSocketResponse(
            heartbeat=30,
            max_msg_size=64 * 1024 * 1024,
            protocols=["aicp"] if "token." in request.headers.get("Sec-WebSocket-Protocol", "") else (),
        )
        await ws.prepare(request)
        ws._user_id = user_id  # 挂到 ws 对象，后续按用户隔离可用

        gateway.register(ws, channel_id)

        ws_id = gateway._ws_id.get(ws, "?")
        agent.log.info(
            f"[WS] Connected: {ws_id} → channel={channel_id}, "
            f"user={user_id}, from={request.remote}, origin={origin}"
        )

        await ws.send_json({
            "type": "connected",
            "ws_id": ws_id,
            "channel_id": channel_id,
            "user_id": user_id,
            "timestamp": time.time(),
        })

        # ---- 消息循环 ----
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)

                        if data.get("type") == "envelop":
                            body = data.get("payload", data)
                            envelop = core.Envelop(
                                sender="os/_websocket",
                                receiver=data.get("receiver", ""),
                                intent=body.get("intent", "API_CALL"),
                                payload=body.get("payload", body),
                                meta={
                                    **body.get("meta", {}),
                                    "ws_id": ws_id,
                                    "channel_id": channel_id,
                                    "user_id": user_id,
                                    "source": "websocket",
                                },
                            )

                            result = await agent.system.call(envelop)

                            await ws.send_json({
                                "type": "envelop_result",
                                "trace_id": envelop.meta.get("trace_id", ""),
                                "ok": True,
                                "payload": result.payload if result else {},
                                "timestamp": time.time(),
                            })

                        elif data.get("type") == "subscribe":
                            new_channel = data.get("channel", channel_id)
                            gateway.register(ws, new_channel)
                            await ws.send_json({
                                "type": "subscribed",
                                "channel": new_channel,
                                "timestamp": time.time(),
                            })
                            agent.log.info(f"[WS] {ws_id} subscribed to {new_channel}")

                        elif data.get("type") == "unsubscribe":
                            sub_channel = data.get("channel", "")
                            if sub_channel:
                                gateway.unregister(ws, sub_channel)
                            await ws.send_json({
                                "type": "unsubscribed",
                                "channel": sub_channel,
                                "timestamp": time.time(),
                            })

                        elif data.get("type") == "ping":
                            await ws.send_json({"type": "pong", "timestamp": time.time()})

                    except json.JSONDecodeError:
                        await ws.send_json({"type": "error", "message": "Invalid JSON"})

                elif msg.type == aiohttp.WSMsgType.ERROR:
                    agent.log.error(f"[WS] Error: {ws.exception()}")
                    break

                elif msg.type == aiohttp.WSMsgType.CLOSE:
                    break

        except asyncio.CancelledError:
            pass
        except Exception as e:
            agent.log.error(f"[WS] Exception: {e}")
        finally:
            gateway.unregister(ws)
            agent.log.info(f"[WS] Disconnected: {ws_id}")

        return ws

    app.router.add_get("/ws", ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()

    agent.log.info(f"[WS] WebSocket server listening on ws://{host}:{port}/ws")
    # ★ runner / 端口记到 agent 上，否则没法优雅重启（端口会被占死）
    agent._ws_runner = runner
    agent._ws_host = host
    agent._ws_port = port
    # 自愈：模块热重载后自动重启（handler 闭包持有旧 globals）
    _sr().ensure_hot_reload_watch(
        agent, "os/_websocket", port=port, host=host,
    )
    return runner
