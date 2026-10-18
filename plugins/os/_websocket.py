"""WebSocket 网关插件 — 复用 HTTP 路由格式，共享 _auth 认证"""
import asyncio
import json
import time
import uuid
import aiohttp
from aiohttp import web
import core
import logging


class WebSocketGateway:
    def __init__(self):
        self._channels = {}
        self._ws_to_channels = {}
        self._ws_id = {}

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
        sent = 0
        for ws in list(self._channels.get(channel_id, set())):
            if not ws.closed:
                try:
                    await ws.send_json(data)
                    sent += 1
                    # print(f"[WS Gateway] 发送成功: channel={channel_id}, sent={sent}")
                except Exception as e:
                    print(f"[WS Gateway] 发送异常: {e}")
                    self.unregister(ws)
            else:
                self.unregister(ws)
        return sent

    async def broadcast(self, data):
        sent = 0
        for ws in list(self._ws_to_channels.keys()):
            if not ws.closed:
                try:
                    await ws.send_json(data)
                    sent += 1
                except Exception:
                    self.unregister(ws)
            else:
                self.unregister(ws)
        return sent

    def get_status(self):
        channels_info = {}
        for cid, ws_set in self._channels.items():
            channels_info[cid] = {"connections": len(ws_set), "ws_ids": [self._ws_id.get(ws, "?") for ws in ws_set if not ws.closed]}
        return {"total_channels": len(self._channels), "total_connections": len(self._ws_to_channels), "channels": channels_info}


def extract_token_from_request(request):
    token = request.query.get("token", "")
    if token:
        return token
    token = request.headers.get("X-AICP-Token", "")
    if token:
        return token
    auth_header = request.headers.get("Authorization", "")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header[7:]
    if hasattr(request, 'cookies'):
        token = request.cookies.get("aicp_token", "")
        if token:
            return token
    return ""


def is_local_request(request):
    client_ip = request.remote
    if client_ip in ['127.0.0.1', 'localhost', '::1']:
        return True
    return False


async def execute(envelop, agent):
    gateway = getattr(agent, 'ws_gateway', None)
    action = envelop.payload.get("action", "")

    if action == "push":
        channel_id = envelop.payload.get("channel_id", "")
        data = envelop.payload.get("data", {})
        if not channel_id:
            envelop.payload = {"error": "channel_id required"}
            return envelop
        data["_meta"] = {"sender": envelop.sender, "intent": envelop.intent, "trace_id": envelop.trace_id, "timestamp": time.time()}
        if gateway:
            sent = await gateway.push(channel_id, data)
            envelop.payload = {"ok": True, "sent": sent, "channel_id": channel_id}
        else:
            envelop.payload = {"error": "ws_gateway not ready"}
        return envelop

    elif action == "broadcast":
        data = envelop.payload.get("data", {})
        data["_meta"] = {"sender": envelop.sender, "trace_id": envelop.trace_id, "timestamp": time.time()}
        sent = await gateway.broadcast(data) if gateway else 0
        envelop.payload = {"ok": True, "sent": sent}
        return envelop

    elif action == "push_to_ws":
        ws_id = envelop.payload.get("ws_id", "")
        data = envelop.payload.get("data", {})
        if not ws_id:
            envelop.payload = {"error": "ws_id required"}
            return envelop
        data["_meta"] = {"sender": envelop.sender, "trace_id": envelop.trace_id, "timestamp": time.time()}
        if gateway:
            sent = 0
            for ws, wid in gateway._ws_id.items():
                if wid == ws_id and not ws.closed:
                    try:
                        await ws.send_json(data)
                        sent += 1
                    except Exception as e:
                        agent.log.info(f"[WS] push_to_ws error: {e}")
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
                    await ws.close(code=1000, message=b"Channel closed by server")
                except Exception:
                    pass
                gateway.unregister(ws, channel_id)
        envelop.payload = {"ok": True, "channel_id": channel_id}
        return envelop

    else:
        envelop.payload = {"error": f"Unknown action: {action}"}
        return envelop


async def start_websocket_server(agent, host="0.0.0.0", port=9001, skip_auth_for_local=False):
    """启动 WebSocket 服务器"""
    gateway = WebSocketGateway()
    agent.ws_gateway = gateway
    app = web.Application()

    async def ws_handler(request):
        token = extract_token_from_request(request)
        channel_id = request.query.get("channel", f"default_{uuid.uuid4().hex[:6]}")

        origin = request.headers.get("Origin", "")
        server_host = request.host
        is_same_origin = (origin == f"http://{server_host}" or origin == f"https://{server_host}")
        is_local = request.remote in ('127.0.0.1', 'localhost', '::1')
        should_skip_auth = is_local or is_same_origin

        if not should_skip_auth:
            auth_env = core.Envelop(
                sender="os/_websocket",
                receiver="os/_auth",
                payload={"action": "verify"},
                meta={"token": token}
            )
            auth_result = await agent.system.call(auth_env)

            if not auth_result or not auth_result.payload.get("ok"):
                agent.log.warning(f"[WS] Auth failed for {request.remote}")
                ws = web.WebSocketResponse()
                await ws.prepare(request)
                await ws.close(code=4001, message=b"Unauthorized")
                return ws

        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=64*1024*1024)
        await ws.prepare(request)

        gateway.register(ws, channel_id)

        ws_id = gateway._ws_id.get(ws, "?")
        agent.log.info(f"[WS] Connected: {ws_id} → channel={channel_id}, from={request.remote}, origin={origin}")

        await ws.send_json({
            "type": "connected",
            "ws_id": ws_id,
            "channel_id": channel_id,
            "timestamp": time.time()
        })

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
                                meta={**body.get("meta", {}), "ws_id": ws_id, "channel_id": channel_id},
                            )

                            # ★ 直接路由，不在这里加记忆逻辑
                            # 记忆激活/提取由 runtime/_system.py 的 SystemServer.call 统一处理
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
                                "timestamp": time.time()
                            })
                            agent.log.info(f"[WS] {ws_id} subscribed to {new_channel}")

                        elif data.get("type") == "unsubscribe":
                            sub_channel = data.get("channel", "")
                            if sub_channel:
                                gateway.unregister(ws, sub_channel)
                            await ws.send_json({
                                "type": "unsubscribed",
                                "channel": sub_channel,
                                "timestamp": time.time()
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

    # agent.log.info(f"[WS] WebSocket server listening on ws://{host}:{port}/ws")

    return runner
