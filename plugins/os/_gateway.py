"""HTTP 入口插件 — 纯网关，只做协议转换和路由转发

v3.1: 加 /data/uploads/ 静态路由
"""
from aiohttp import web
from inspect import isasyncgen
from pathlib import Path
import core
import json
import asyncio
import sys

import importlib
import sys

# ★ 延迟导入 _service_restart：加载器按文件名字母序遍历 plugins/**/*.py，
#   os/_service_restart.py 排在 os/_gateway.py / os/_mcp_server.py /
#   os/_file_receiver.py / os/_websocket.py 之后（s > m > f > g），
#   模块级 import 会因「还没加载到」抛 ModuleNotFoundError → 引擎起不来。
#   所以这里只在真正要用的时候 import（见 _sr()）。

# ★ 热重载友好：网关的 aiohttp 路由闭包在 START 时创建，持有启动那一刻的模块 globals。
#   热重载会 exec 出新模块对象并重绑 core.plugins，但已在跑的 handler 仍指向旧模块，
#   结果 = 改了 PUBLIC_PAGES / 路由逻辑却不生效，必须重启引擎。
#   下面凡「随代码变化的值」都改成运行时从 live 模块取。
_LIVE_MODULE_NAME = "plugin_os__gateway"


def _sr():
    """延迟取得 _service_restart 模块（见上方注释：不能模块级 import）。"""
    import importlib
    return importlib.import_module("plugin_os__service_restart")


def _live(name, default=None):
    """从当前活着的模块取一个值；取不到就回退到本模块的同名值。"""
    mod = sys.modules.get(_LIVE_MODULE_NAME)
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return globals().get(name, default)


# 免登录即可访问的静态页面（其余 .html 需 token 鉴权）
PUBLIC_PAGES = {'index.html', '404.html'}

# 应用私有静态资源前缀（相对 www/）。
# apps/<project>/** 下的 .js/.css/.json/图片等一律与应用页面同口径鉴权，
# 避免"页面要登录、但它引用的脚本可匿名下载"。
# vendor/、system/、tools/ 等共享资源不在此列，保持公开。
APP_ASSET_PREFIXES = ('apps/',)

# ★ 对外隐藏的应用（模板/内置 demo），API 层与静态页都拦截。
#   原先定义在 START 闭包里 → 属于 handler 的局部变量，热重载永远改不动。
#   提到模块级，再由使用点走 _live() 读取，才能热生效。
# ★ 对外隐藏的应用（模板/内置 demo），API 层与静态页都拦截。
#   原先定义在 START 闭包里 → 属于 handler 的局部变量，热重载永远改不动。
#   提到模块级，再由使用点走 _live() 读取，才能热生效。
#   2026-11-09：_demo 取消隐藏（本地实例需要能直接访问调试）。
#   如需恢复隐藏，改回 ("applications/_demo/",) 即可。
HIDDEN_APP_PREFIXES = ()

# ============================================================
# 请求体大小上限
# 原本三处各自为政（网关 50MB / JSON API 10MB / 接收器 50MB），现统一到一处定义。
#   MAX_JSON_BODY_SIZE     : 网关 JSON API（/api/*）的请求体上限，固定 10MB。
#                            JSON 体积比原始文件大（base64 还要再涨 33%），
#                            所以刻意比文件上传更小。
#   DEFAULT_MAX_UPLOAD_SIZE : multipart 直传的单文件默认上限，50MB。
#   client_max_size        : aiohttp 兜底，取上面两者的较大值，
#                            否则会在业务逻辑前就被 413 掉。
# 应用宣称的文件大小上限必须和上传上限对齐，
# 否则大文件能过前端校验却在上传时被服务端拒。
#
# 上传上限从 aicp.yaml 的 upload.max_size 读取（缺省 50MB），
# 上传接收器 os/_file_receiver.py 读的是同一份配置，两边不会再漂移。
#
# JSON 体上限从 aicp.yaml 的 max_body_size 读取（缺省 10MB），
# 网关 JSON API 与 MCP JSON-RPC 消息共用这一个值。
# ============================================================
DEFAULT_MAX_BODY_SIZE = 10 * 1024 * 1024    # 10MB，JSON 请求体（网关 + MCP 共用）
DEFAULT_MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50MB，multipart 直传单文件


def _resolve_max_body_size(agent) -> int:
    """读取 JSON 请求体上限：aicp.yaml max_body_size，缺省 10MB。
    网关 os/_gateway.py 与 MCP 入口 os/_mcp_server.py 共用这一个值。"""
    val = agent.config.get("max_body_size", DEFAULT_MAX_BODY_SIZE) if agent else DEFAULT_MAX_BODY_SIZE
    try:
        val = int(val)
    except (TypeError, ValueError):
        val = DEFAULT_MAX_BODY_SIZE
    return val if val > 0 else DEFAULT_MAX_BODY_SIZE


def _resolve_max_upload_size(agent) -> int:
    """读取上传大小上限：aicp.yaml upload.max_size，缺省 50MB。
    与 os/_file_receiver.py 的同名函数读同一份配置。"""
    cfg = agent.config.get("upload", {}) if agent else {}
    val = cfg.get("max_size", DEFAULT_MAX_UPLOAD_SIZE)
    try:
        val = int(val)
    except (TypeError, ValueError):
        val = DEFAULT_MAX_UPLOAD_SIZE
    return val if val > 0 else DEFAULT_MAX_UPLOAD_SIZE


async def _stop_gateway(agent, keep_watcher: bool = False):
    """真正停掉 HTTP 网关（释放端口）。

    keep_watcher=True 时不取消自重启监听 —— 重启流程自己会调它，
    取消了就没人重建了。
    """
    if not keep_watcher:
        rtask = getattr(agent, "_gateway_reload_task", None)
        if rtask:
            # ★ 只 cancel 不 await：若这次 STOP 是 watcher 自己触发的会死锁
            try:
                rtask.cancel()
            except Exception:
                pass
            agent._gateway_reload_task = None

        swatch = getattr(agent, "_service_watch_os__gateway", None)
        if swatch:
            try:
                swatch.cancel()
            except Exception:
                pass
            agent._service_watch_os__gateway = None

    runner = getattr(agent, "_gateway_runner", None)
    if runner:
        try:
            await runner.cleanup()
        except Exception:
            pass
        agent._gateway_runner = None


async def execute(envelop, agent):
    action = envelop.payload.get("action", "START")

    if action == "START":
        port = envelop.payload.get("port", 9000)
        host = envelop.payload.get("host", "127.0.0.1")

        max_upload_size = _resolve_max_upload_size(agent)
        max_body_size = _resolve_max_body_size(agent)
        # client_max_size 必须 >= JSON 上限和上传上限，否则会在业务逻辑前就被 413 掉
        app = web.Application(client_max_size=max(max_body_size, max_upload_size))

        @web.middleware
        async def cors(request, handler):
            # ★ 回显真实 Origin，而不是 "*"：
            #   1) 鉴权判定（_is_same_origin）依赖 Origin 头，
            #      回显 "*" 会让浏览器拿不到可用的同源信号；
            #   2) 带 cookie 的跨端口请求不接受 "*"，
            #      回显 Origin 才能让 cookie / Authorization 正常随请求发出。
            #   与上传接收器 (_file_receiver.py) 的 CORS 策略保持一致。
            origin = request.headers.get("Origin", "")
            allow_origin = origin if origin else "*"
            if request.method == "OPTIONS":
                return web.Response(status=200, headers={
                    "Access-Control-Allow-Origin": allow_origin,
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type, X-AICP-Token, Authorization",
                    "Access-Control-Allow-Credentials": "true",
                    "Access-Control-Allow-Private-Network": "true",
                    "Access-Control-Allow-Local-Network": "true",
                    "Access-Control-Max-Age": "600",
                    "Vary": "Origin",
                })
            resp = await handler(request)
            resp.headers["Access-Control-Allow-Origin"] = allow_origin
            resp.headers["Access-Control-Allow-Credentials"] = "true"
            resp.headers["Access-Control-Allow-Private-Network"] = "true"
            resp.headers["Access-Control-Allow-Local-Network"] = "true"
            resp.headers["Vary"] = "Origin"
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

        def _is_lan(request):
            """局域网直连（手机/平板访问盒子）。有反向代理头时一律不算。"""
            if request.headers.get("X-Forwarded-For", ""):
                return False
            from plugins.os._auth import is_private_network
            return is_private_network(request.remote or "")

        async def _verify_auth(request, agent, route, envelop_meta=None):
            token = _extract_token(request)

            meta = {
                "token": token,
                "authorization": request.headers.get("Authorization", ""),
                "cookie_token": request.cookies.get("aicp_token", ""),
                "remote": request.remote or "",
                "forwarded_for": request.headers.get("X-Forwarded-For", ""),
                "origin": request.headers.get("Origin", ""),
                "host": request.host,
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

        # ★ 对外隐藏的应用（模板/内置 demo），API 层直接拦截
        #   用 _live() 读模块级常量：热重载后立即生效（旧代码用闭包局部变量，改了没用）

        async def handle_api(request):
            path = request.match_info["path"]
            raw_body = b""

            # ★ 拦截隐藏应用的 API 请求
            if any(path.startswith(pfx) for pfx in _live("HIDDEN_APP_PREFIXES", HIDDEN_APP_PREFIXES)):
                return web.json_response({"error": "Not found"}, status=404)

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
                    # ★ 用请求方 Host 的主机名 + 配置的 ws 端口推导 ws 地址，
                    #   局域网/手机访问才能连上（HTTP 端口与 WS 端口不同，不能直接用 Host 的端口）。
                    #   Host 缺失时保持原样不替换（云端部署走 external_url 覆盖）。
                    if hostname:
                        url = f"{protocol}://{hostname}:{ws_port}/ws"
                    else:
                        url = f"{protocol}://:{ws_port}/ws"

                return web.json_response({
                    "url": url,
                    "port": ws_port,
                    "secure": is_secure
                })

            if path == "auth/verify":
                ok, auth_payload = await _verify_auth(request, agent, "auth/verify")
                if ok:
                    auth_payload = auth_payload or {}
                    return web.json_response({
                        "ok": True,
                        "user_id": auth_payload.get("user_id", "local"),
                        "permissions": auth_payload.get("permissions", {}),
                        "source": auth_payload.get("source", ""),
                    })
                else:
                    return web.json_response({"ok": False, "error": "Unauthorized"}, status=401)

            # JSON API 请求体上限（见文件头 _resolve_max_body_size）。
            # 注意：文件请走 multipart 直传（/api/upload_config 拿地址），
            # 不要 base64 塞进 JSON —— 既超这个上限，体积还额外涨 33%。
            max_body_size = _resolve_max_body_size(agent)
            body = {}

            try:
                raw_body = await request.read()

                if len(raw_body) > max_body_size:
                    return web.json_response({
                        "error": f"Request body too large: {len(raw_body)} > {max_body_size} (JSON API limit; for file upload use /api/upload_config multipart endpoint)",
                        "reason": "body_too_large",
                        "limit_bytes": max_body_size,
                        "actual_bytes": len(raw_body),
                        "configurable_via": "aicp.yaml max_body_size",
                        "hint": "shrink the payload, or upload files via multipart (GET /api/upload_config for the url); base64 in JSON also inflates size by ~33%",
                    }, status=413)

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

            raw_body_meta = body.get("meta", {}) if isinstance(body.get("meta"), dict) else {}

            # ★ 剥掉前端可伪造的身份字段，不让它们进 env_meta
            body_meta = {
                k: v for k, v in raw_body_meta.items()
                if k not in ("user_id", "auth_result", "token", "permissions")
            }

            # ★ 鉴权裁决统一交给 os/_auth.requires_auth：
            #   mode=token  → 一律要 token，本机/同源不再短路（反代同机回源也无法绕过）
            #   mode=hybrid → 本机/同源放行，带 XFF 时同源判定失效
            #   mode=local  → 交给 _verify 的 local 护栏
            from plugins.os._auth import requires_auth
            need_auth = requires_auth(
                agent,
                remote=request.remote or "",
                forwarded_for=request.headers.get("X-Forwarded-For", ""),
                origin=request.headers.get("Origin", ""),
                host=request.host,
            )

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
                "remote": request.remote or "",
                "forwarded_for": request.headers.get("X-Forwarded-For", ""),
                "origin": request.headers.get("Origin", ""),
                "host": request.host,
            }

            # ★ 鉴权成功后，网关盖章 user_id；放在 **body_meta 之后，覆盖前端任何自报值
            if auth_payload:
                env_meta["auth_result"] = auth_payload
                env_meta["auth_user_id"] = auth_payload.get("user_id", "")
                env_meta["auth_source"] = auth_payload.get("source", "")
                env_meta["auth_permissions"] = auth_payload.get("permissions", {})
                if auth_payload.get("user_id"):
                    env_meta["user_id"] = auth_payload["user_id"]
                elif need_auth:
                    # 鉴权了但 _auth 没返回 user_id：拒绝，不落 local
                    return web.json_response({"error": "auth did not return user_id"}, status=500)
            elif not need_auth:
                # 本地 / 同源放行：显式盖章为 local，让下游插件的 admin 判定能工作
                env_meta["auth_user_id"] = "local"
                env_meta["auth_source"] = "local"
                env_meta["auth_permissions"] = {"*": True}
                env_meta["user_id"] = "local"

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
                    content_type=result.meta.get("content_type", "text/html"),
                )

            # ★ 兜底序列化：payload 里混入 bytes/自定义对象时不应整条 500，
            # 否则前端 res.json() 报 "Unexpected non-whitespace character after JSON"
            return web.json_response(
                result.result(),
                dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str),
            )

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
                    # ★ 用请求方 Host 的主机名 + 配置的 ws 端口推导 ws 地址，
                    #   局域网/手机访问才能连上（HTTP 端口与 WS 端口不同，不能直接用 Host 的端口）。
                    #   Host 缺失时保持原样不替换（云端部署走 external_url 覆盖）。
                    if hostname:
                        url = f"{protocol}://{hostname}:{ws_port}/ws"
                    else:
                        url = f"{protocol}://:{ws_port}/ws"

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

            # ★ 拦截隐藏应用的静态页面
            if any(file_path.startswith(f"apps/{pfx.split('/')[1]}/") for pfx in _live("HIDDEN_APP_PREFIXES", HIDDEN_APP_PREFIXES)):
                return web.Response(text="Not found", status=404)

            is_public_page = file_path in _live("PUBLIC_PAGES", PUBLIC_PAGES)

            # ★ 静态资源鉴权口径（原来只有 .html 走鉴权，.js/.css/.json 落 else 分支直接放行，
            #   等于"页面被保护、它引用的脚本却被匿名读走"，且缓存头还不一致）。
            #   现在按"归属"判定，而不是按"扩展名"判定：
            #     - apps/<project>/**  → 应用私有资源，整体需鉴权
            #     - vendor/ system/ tools/ 等共享资源 → 公开
            #   应用私有资源统一加 no-store，和 .html 保持一致。
            app_prefix = _live("APP_ASSET_PREFIXES", APP_ASSET_PREFIXES)
            is_app_asset = any(file_path.startswith(p) for p in app_prefix)

            if (file_path.endswith('.html') and not is_public_page) or is_app_asset:
                # ★ 与 handle_api 的 need_auth 口径一致：本机 / 同源浏览器直接放行，
                #   不走 _verify_auth。否则手机等局域网浏览器打开应用页面时，
                #   页面引用的 .js/.css 会被 os/_auth 在 local 模式下拒绝，返回 403。
                # ★ 与 handle_api 同一裁决口径（os/_auth.requires_auth）：
                #   mode=token  → 一律要 token；静态页不再因本机/同源/局域网而放行
                #   mode=hybrid → 本机/同源/局域网直连放行，带 XFF 时同源失效
                from plugins.os._auth import requires_auth
                if requires_auth(
                    agent,
                    remote=request.remote or "",
                    forwarded_for=request.headers.get("X-Forwarded-For", ""),
                    origin=request.headers.get("Origin", ""),
                    host=request.host,
                ):
                    ok, _ = await _verify_auth(request, agent, "__static__")
                    if not ok:
                        if file_path.endswith('.html'):
                            return web.Response(
                                text='<script>location.href="/"</script>',
                                content_type='text/html',
                            )
                        return web.Response(text="Forbidden", status=403)

                return await _route_get(
                        "os/_static", request,
                        action="serve", file_path=file_path,
                        extra_headers={
                            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                            "Pragma": "no-cache",
                            "Expires": "0",
                        },
                    )
            return await _route_get("os/_static", request,
                                action="serve", file_path=file_path)

        async def _route_get(receiver, request, action=None, file_path=None, extra_headers=None):
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
                headers = dict(extra_headers or {})
                return web.Response(
                    body=result.meta["static_content"],
                    content_type=result.meta.get("content_type", "text/html"),
                    headers=headers,
                )

            if result.meta and result.meta.get("response_type") == "file":
                fp = result.meta.get("file_path")
                if not fp or not Path(fp).exists():
                    return web.json_response({"error": "File not found"}, status=404)
                headers = {
                    "Content-Type": result.meta.get("content_type", "application/octet-stream"),
                    "Content-Disposition": f'{result.meta.get("content_disposition", "inline")}; filename="{result.meta.get("file_name", "file")}"',
                }
                if extra_headers:
                    headers.update(extra_headers)
                return web.FileResponse(path=fp, headers=headers)

            if result.payload and result.payload.get("error"):
                status = 403 if "Forbidden" in str(result.payload.get("error", "")) else 404
                return web.Response(text=result.payload["error"], status=status)

            return web.json_response(result.result())

        app.router.add_post("/v1/chat/completions", handle_v1_chat)
        app.router.add_post("/api/{path:.*}", handle_api)
        app.router.add_get("/", handle_static)
        app.router.add_get("/{path:.*}", handle_static)
        app.router.add_get("/health", lambda r: web.json_response({"status": "ok"}))

        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()

        # ★ runner 必须挂到 agent 上，否则没法优雅重启
        #   （旧代码没存 → STOP 只是个空壳，端口永远占着，重启必然 "address in use"）
        agent._gateway_runner = runner
        agent._gateway_host = host
        agent._gateway_port = port

        # ★ 自愈监听（统一走 os/_service_restart）：
        #   本模块被热重载后，aiohttp app 里注册的 handler 闭包仍持有旧模块的
        #   globals（路由逻辑 / 常量都不会更新）。watcher 检测到模块被换掉就
        #   自动重启服务 —— 否则「改网关逻辑必须重启引擎」。
        _sr().ensure_hot_reload_watch(
            agent, "os/_gateway", port=port, host=host,
            start_payload={"action": "START", "host": host, "port": port},
        )

        envelop.payload = {"status": "listening", "port": port, "host": host}
        return envelop

    elif action == "STOP":
        await _stop_gateway(agent)
        envelop.payload = {"status": "stopped"}
        return envelop

    elif action == "restart":
        # ★ 优雅重启：释放端口 + 用新代码重新起（不需要重启引擎）
        ok = await _sr().restart_service_and_watch(
            agent, "os/_gateway",
            port=getattr(agent, "_gateway_port", 9000),
            host=getattr(agent, "_gateway_host", "0.0.0.0"),
            start_payload={"action": "START",
                           "host": getattr(agent, "_gateway_host", "0.0.0.0"),
                           "port": getattr(agent, "_gateway_port", 9000)},
        )
        envelop.payload = {"ok": ok, "status": "restarted" if ok else "restart_failed",
                           "port": getattr(agent, "_gateway_port", 9000)}
        return envelop

    envelop.payload = {"error": f"Unknown action: {action}"}
    return envelop
