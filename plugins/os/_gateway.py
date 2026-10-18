"""HTTP 入口插件 — 纯网关，只做协议转换和路由转发

v3.1: 加 /data/uploads/ 静态路由
"""
from aiohttp import web
from inspect import isasyncgen
from pathlib import Path
import core
import json
import asyncio


async def execute(envelop, agent):
    action = envelop.payload.get("action", "START")

    if action == "START":
        port = envelop.payload.get("port", 9000)
        host = envelop.payload.get("host", "127.0.0.1")

        app = web.Application(client_max_size=50 * 1024 * 1024)

        @web.middleware
        async def cors(request, handler):
            if request.method == "OPTIONS":
                return web.Response(status=200, headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type, X-AICP-Token, Authorization",
                })
            resp = await handler(request)
            resp.headers["Access-Control-Allow-Origin"] = "*"
            return resp

        app.middlewares.append(cors)

        def _extract_token(request):
            token = request.headers.get("X-AICP-Token", "")
            if not token:
                auth_header = request.headers.get("Authorization", "")
                if auth_header.startswith("Bearer "):
                    token = auth_header[7:]
            if not token:
                token = request.cookies.get("aicp_token", "")
            return token

        def _is_local(request):
            forwarded_for = request.headers.get("X-Forwarded-For", "")
            if forwarded_for:
                return False
            return request.remote in ('127.0.0.1', 'localhost', '::1')

        def _is_same_origin(request):
            origin = request.headers.get("Origin", "")
            if not origin:
                return False
            server_host = request.host
            return origin == f"http://{server_host}" or origin == f"https://{server_host}"

        async def _verify_auth(request, agent, route, envelop_meta=None):
            token = _extract_token(request)

            meta = {
                "token": token,
                "authorization": request.headers.get("Authorization", ""),
                "cookie_token": request.cookies.get("aicp_token", ""),
            }
            if envelop_meta:
                meta.update(envelop_meta)

            auth_env = core.Envelop(
                sender="os/_gateway",
                receiver="os/_auth",
                payload={"action": "verify", "route": route},
                meta=meta,
            )
            auth_result = await agent.system.call(auth_env)

            if auth_result and auth_result.payload.get("ok"):
                return True, auth_result.payload
            return False, None

        async def handle_v1_chat(request):
            if request.method != "POST":
                return web.json_response({"error": "Method not allowed"}, status=405)

            try:
                body = await request.json()
            except:
                return web.json_response({"error": "Invalid JSON"}, status=400)

            stream = body.get("stream", False)

            env = core.Envelop(
                sender="os/_gateway",
                receiver="builtins/newbula_plus/engine",
                payload={
                    "action": "chat_completions",
                    "model": body.get("model", "newbula-plus"),
                    "messages": body.get("messages", []),
                    "stream": stream,
                }
            )
            result = await agent.system.call(env)

            if result is None:
                return web.json_response({"error": "no response"}, status=500)

            if stream:
                resp = web.StreamResponse()
                resp.headers['Content-Type'] = 'text/event-stream'
                resp.headers['Cache-Control'] = 'no-cache'
                await resp.prepare(request)

                async for chunk in result.payload:
                    if isinstance(chunk, str):
                        await resp.write(chunk.encode())
                    elif isinstance(chunk, bytes):
                        await resp.write(chunk)
                    else:
                        await resp.write(str(chunk).encode())

                return resp

            return web.json_response(result.payload)

        async def handle_api(request):
            path = request.match_info["path"]
            raw_body = b""

            if path == "ws_config":
                ws_config = agent.config.get("websocket", {})
                ws_external_url = ws_config.get("external_url", "")
                ws_port = agent.config.get("port", 9000) + 1

                is_secure = request.headers.get("X-Forwarded-Proto", request.scheme) == "https"
                hostname = request.host.split(':')[0]

                if ws_external_url:
                    url = ws_external_url
                else:
                    protocol = "wss" if is_secure else "ws"
                    url = f"{protocol}://{hostname}/ws"

                return web.json_response({
                    "url": url,
                    "port": ws_port,
                    "secure": is_secure
                })

            if path == "auth/verify":
                ok, _ = await _verify_auth(request, agent, "auth/verify")
                if ok:
                    return web.json_response({"ok": True})
                else:
                    return web.json_response({"ok": False}, status=401)

            MAX_BODY_SIZE = 10 * 1024 * 1024
            body = {}

            try:
                raw_body = await request.read()

                if len(raw_body) > MAX_BODY_SIZE:
                    return web.json_response({"error": f"Request body too large: {len(raw_body)} > {MAX_BODY_SIZE}"}, status=413)

                if raw_body:
                    try:
                        raw_text = raw_body.decode('utf-8')
                        body = json.loads(raw_text)
                    except UnicodeDecodeError:
                        raw_text = raw_body.decode('utf-8', errors='ignore')
                        try:
                            body = json.loads(raw_text)
                        except json.JSONDecodeError:
                            body = {}
                    except json.JSONDecodeError:
                        body = {}

            except asyncio.TimeoutError:
                body = {}
            except Exception as e:
                import traceback
                traceback.print_exc()
                body = {}

            if not isinstance(body, dict):
                body = {"data": body}

            body_meta = body.get("meta", {}) if isinstance(body.get("meta"), dict) else {}

            need_auth = not (_is_local(request) or _is_same_origin(request))

            auth_payload = None

            if need_auth:
                ok, auth_payload = await _verify_auth(request, agent, path, body_meta)
                if not ok:
                    return web.json_response({"error": "Unauthorized"}, status=401)

            body_payload = body.get("payload", body)
            if not isinstance(body_payload, dict):
                body_payload = {"data": body_payload}

            env_meta = {
                **body_meta,
                "token": _extract_token(request),
                "method": request.method,
                "path": path,
                "headers": {k: v for k, v in request.headers.items()},
                "raw_body": raw_body.decode('utf-8', errors='ignore') if raw_body else "",
            }

            if auth_payload:
                env_meta["auth_result"] = auth_payload

            env = core.Envelop(
                sender="os/_gateway",
                receiver=path,
                intent=body.get("intent", "API_CALL") if isinstance(body.get("intent"), str) else "API_CALL",
                payload=body_payload,
                meta=env_meta,
            )

            result = await agent.system.call(env)

            if result is None:
                return web.json_response({"error": "no response"}, status=500)

            if isasyncgen(result.payload):
                resp = web.StreamResponse()
                resp.headers['Content-Type'] = 'text/event-stream'
                resp.headers['Cache-Control'] = 'no-cache'
                await resp.prepare(request)

                async for chunk in result.payload:
                    if chunk is None:
                        continue
                    if isinstance(chunk, str):
                        await resp.write(chunk.encode())
                    elif isinstance(chunk, bytes):
                        await resp.write(chunk)
                    else:
                        await resp.write(str(chunk).encode())

                return resp

            if result.meta and result.meta.get("static_content"):
                return web.Response(
                    body=result.meta["static_content"],
                    content_type=result.meta.get("content_type", "application/octet-stream"),
                )

            return web.json_response(result.payload if result.payload else {"ok": True})

        async def handle_static(request):
            file_path = request.match_info.get("path", "")
            if not file_path:
                file_path = "index.html"

            if file_path == "api/upload_config":
                upload_config = agent.config.get("upload", {})
                upload_external_url = upload_config.get("external_url", "")
                upload_port = agent.config.get("port", 9000) + 2

                is_secure = request.headers.get("X-Forwarded-Proto", request.scheme) == "https"
                hostname = request.host.split(':')[0]

                if upload_external_url:
                    url = upload_external_url
                else:
                    protocol = "https" if is_secure else "http"
                    url = f"{protocol}://{hostname}:{upload_port}/upload"

                return web.json_response({
                    "url": url,
                    "port": upload_port,
                    "secure": is_secure
                })

            if file_path == "api/ws_config":
                ws_config = agent.config.get("websocket", {})
                ws_external_url = ws_config.get("external_url", "")
                ws_port = agent.config.get("port", 9000) + 1

                is_secure = request.headers.get("X-Forwarded-Proto", request.scheme) == "https"
                hostname = request.host.split(':')[0]

                if ws_external_url:
                    url = ws_external_url
                else:
                    protocol = "wss" if is_secure else "ws"
                    url = f"{protocol}://{hostname}/ws"

                return web.json_response({
                    "url": url,
                    "port": ws_port,
                    "secure": is_secure
                })

            if file_path == "api/backend_info":
                import sys as _sys
                return web.json_response({
                    "language": "python",
                    "runtime": f"{_sys.version_info.major}.{_sys.version_info.minor}.{_sys.version_info.micro}",
                    "version": "5.3",
                    "protocol_version": "5.4",
                    "plugins_dir": "plugins",
                    "www_dir": "www",
                    "features": [
                        "plugin", "frontend", "studio",
                        "websocket", "file_receiver",
                        "restart", "cron"
                    ],
                })

            if file_path == "api/list":
                return await _route_get("os/_registry", request, action="list")

            if file_path == "api/pages":
                return await _route_get("os/_static", request, action="list")

            # ★ /data/uploads/ 静态文件
            if file_path.startswith("data/uploads/"):
                upload_root = Path("data/uploads").resolve()
                full = (Path(".") / file_path).resolve()
                if not str(full).startswith(str(upload_root)):
                    return web.Response(text="Forbidden", status=403)
                if not full.exists() or full.is_dir():
                    return web.Response(text="Not found", status=404)
                return web.FileResponse(path=full)

            if file_path.startswith("api/"):
                route = file_path[4:]
                return await _route_get(route, request)

            if file_path.startswith("data/"):
                return await _route_get("os/_static", request,
                                    action="serve_data", file_path=file_path[5:])

            is_root_file = '/' not in file_path
            is_login_page = file_path in ['login.html', '404.html']

            if file_path.endswith('.html') and not is_login_page and is_root_file:
                ok, _ = await _verify_auth(request, agent, "__static__")
                if not ok:
                    from urllib.parse import quote
                    return web.Response(
                        text=f'<script>location.href="/login.html?redirect={quote(request.path)}"</script>',
                        content_type='text/html',
                    )

            return await _route_get("os/_static", request,
                                action="serve", file_path=file_path)

        async def _route_get(receiver, request, action=None, file_path=None):
            from urllib.parse import parse_qs

            payload = {}

            if action:
                payload["action"] = action
            if file_path:
                payload["file_path"] = file_path

            query = parse_qs(request.query_string)
            for key, values in query.items():
                if values:
                    payload[key] = values[0]

            env = core.Envelop(
                sender="os/_gateway",
                receiver=receiver,
                intent="API_CALL",
                payload=payload,
                meta={
                    "token": _extract_token(request),
                    "method": "GET",
                    "path": file_path or receiver,
                },
            )

            result = await agent.system.call(env)

            if not result:
                return web.json_response({"error": "no response"}, status=500)

            if result.meta and result.meta.get("static_content"):
                return web.Response(
                    body=result.meta["static_content"],
                    content_type=result.meta.get("content_type", "text/html"),
                )

            if result.meta and result.meta.get("response_type") == "file":
                fp = result.meta.get("file_path")
                if not fp or not Path(fp).exists():
                    return web.json_response({"error": "File not found"}, status=404)
                return web.FileResponse(
                    path=fp,
                    headers={
                        "Content-Type": result.meta.get("content_type", "application/octet-stream"),
                        "Content-Disposition": f'{result.meta.get("content_disposition", "inline")}; filename="{result.meta.get("file_name", "file")}"'
                    }
                )

            if result.payload and result.payload.get("error"):
                status = 403 if "Forbidden" in str(result.payload.get("error", "")) else 404
                return web.Response(text=result.payload["error"], status=status)

            return web.json_response(result.payload if result.payload else {"ok": True})

        app.router.add_post("/v1/chat/completions", handle_v1_chat)
        app.router.add_post("/api/{path:.*}", handle_api)
        app.router.add_get("/", handle_static)
        app.router.add_get("/{path:.*}", handle_static)
        app.router.add_get("/health", lambda r: web.json_response({"status": "ok"}))

        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()

        envelop.payload = {"status": "listening", "port": port, "host": host}
        return envelop

    elif action == "STOP":
        envelop.payload = {"status": "stopped"}
        return envelop

    envelop.payload = {"error": f"Unknown action: {action}"}
    return envelop
