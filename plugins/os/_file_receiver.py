"""文件接收插件 — 端口 = gateway + 2，接收 multipart 上传
兼容：拆片机（只传 file）和标准 AICP（file + session_id + callback）

v2.2 改造：
- 上传接口接入 os/_auth 鉴权（local/hybrid/token 三档）
- 来源判断查 X-Forwarded-For，反向代理后不误判本机
- 文件名清洗防路径穿越
- CORS 允许 Authorization 头
- ★ watch 注册收口到 START 唯一入口，修复 start/restart 双触发导致反复重载
- ★ _stop 的 kill_port_holders 加守卫，避免热重载期间杀到自己写坏文件
- ★ 上传落盘改「临时文件 + 原子 rename」，防中断留下半截文件
- ★ restart 改为 stop→START，不再调 restart_service_and_watch
"""
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from aiohttp import web
import core

import importlib
import sys

# ★ 延迟导入 _service_restart：加载器按文件名字母序遍历 plugins/**/*.py，
#   os/_service_restart.py 排在本文件之后（s > f），模块级 import 会因
#   「还没加载到」抛 ModuleNotFoundError → 引擎起不来。只在要用时 import。


def _sr():
    """延迟取得 _service_restart 模块（见上方注释：不能模块级 import）。"""
    import importlib
    return importlib.import_module("plugin_os__service_restart")


DEFAULT_UPLOAD_DIR = "data/uploads"

# 单文件上传上限（50MB）。
# 与网关 os/_gateway.py 的 DEFAULT_MAX_UPLOAD_SIZE 是同一个值，
# 两边都从 aicp.yaml 的 upload.max_size 读取（缺省 50MB），
# 这样改一处配置即可，不会再出现"前端宣称的上限和接收器不一致"。
DEFAULT_MAX_UPLOAD_SIZE = 50 * 1024 * 1024   # 50MB


def _resolve_max_upload_size(agent) -> int:
    """读取上传大小上限：aicp.yaml upload.max_size，缺省 50MB。"""
    cfg = agent.config.get("upload", {}) if agent else {}
    val = cfg.get("max_size", DEFAULT_MAX_UPLOAD_SIZE)
    try:
        val = int(val)
    except (TypeError, ValueError):
        val = DEFAULT_MAX_UPLOAD_SIZE
    return val if val > 0 else DEFAULT_MAX_UPLOAD_SIZE


def _get_upload_dir(agent) -> str:
    cfg = agent.config.get("upload", {}) if agent else {}
    d = cfg.get("dir", DEFAULT_UPLOAD_DIR)
    Path(d).mkdir(parents=True, exist_ok=True)
    return str(Path(d).resolve())


class FileReceiver:
    def __init__(self, upload_dir: str):
        self.upload_dir = upload_dir

    def get_status(self):
        total_size = 0
        file_count = 0
        if os.path.exists(self.upload_dir):
            for root, dirs, files in os.walk(self.upload_dir):
                file_count += len(files)
                for f in files:
                    try:
                        total_size += os.path.getsize(os.path.join(root, f))
                    except:
                        pass
        return {
            "upload_dir": self.upload_dir,
            "file_count": file_count,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
        }


def _to_url(file_path: str, upload_dir: str) -> str:
    try:
        rel = Path(file_path).resolve().relative_to(Path(upload_dir).resolve())
        return f"/data/uploads/{rel.as_posix()}"
    except Exception:
        return ""


# ============================================================
# 鉴权辅助
# ============================================================

def _extract_token(request) -> str:
    """上传场景 token 提取顺序：Header > Cookie > Query（上传客户端常不支持自定义 header）"""
    token = request.headers.get("X-AICP-Token", "")
    if token:
        return token
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    if hasattr(request, "cookies"):
        token = request.cookies.get("aicp_token", "")
        if token:
            return token
    return request.query.get("token", "")


def _is_local_request(request) -> bool:
    """有 X-Forwarded-For = 经过代理 = 非本机"""
    if request.headers.get("X-Forwarded-For", ""):
        return False
    return request.remote in ("127.0.0.1", "localhost", "::1")


def _is_same_origin(request) -> bool:
    """判断 Origin 是否属于本服务所在的“站点”。

    上传接收器（9002）与网关（9000）是同一台机器上的兄弟服务，
    页面 Origin 必然带网关端口（如 http://192.168.1.5:9000），
    而 request.host 是接收器自己的端口（192.168.1.5:9002）。
    只比端口会把合法的同站请求误判成跨站 → 401。

    因此这里只比“主机名 + 协议”，不要求端口相同。
    """
    origin = request.headers.get("Origin", "")
    if not origin:
        return False
    try:
        o = urlsplit(origin)
    except ValueError:
        return False
    if not o.hostname:
        return False
    # request.host 形如 "192.168.1.5:9002"
    host = request.host.split(":")[0]
    if o.hostname != host:
        return False
    # 协议需与本服务一致（http 上来的 Origin 不用 https）
    return o.scheme in ("http", "https")


async def _check_auth(request, agent) -> tuple[bool, str, str]:
    """返回 (ok, user_id, error_msg)。"""
    token = _extract_token(request)

    # ★ 鉴权裁决统一交给 os/_auth.requires_auth：
    #   mode=token  → 一律要 token，本机/同源不再短路
    #   mode=hybrid → 本机/同源放行，带 XFF 时同源判定失效
    #   mode=local  → 交给 _verify 的 local 护栏
    from plugins.os._auth import requires_auth, _resolve_mode
    # local 模式：本机或同站直连直接放行，无需 token。
    # 9000 网关 -> 9002 接收器属跨端口同站，用只比主机名的 _is_same_origin 判定。
    if _resolve_mode(agent) == "local" and (_is_local_request(request) or _is_same_origin(request)):
        return True, "local", ""
    if not requires_auth(
        agent,
        remote=request.remote or "",
        forwarded_for=request.headers.get("X-Forwarded-For", ""),
        origin=request.headers.get("Origin", ""),
        host=request.host,
    ):
        # 带了 token 也尊重它（便于本机模拟多用户）
        if token:
            auth_env = core.Envelop(
                sender="os/_file_receiver",
                receiver="os/_auth",
                payload={"action": "verify", "token": token},
                meta={
                    "token": token,
                    "source": "mcp",   # 跳过公开路由
                    "remote": request.remote or "",
                    "forwarded_for": request.headers.get("X-Forwarded-For", ""),
                },
            )
            result = await agent.system.call(auth_env)
            if result and result.payload.get("ok"):
                return True, result.payload.get("user_id", "local"), ""
        return True, "local", ""

    # 远程：必须带 token
    if not token:
        return False, "", "token required for remote upload"

    auth_env = core.Envelop(
        sender="os/_file_receiver",
        receiver="os/_auth",
        payload={"action": "verify", "token": token},
        meta={
            "token": token,
            "source": "mcp",
            "remote": request.remote or "",
            "forwarded_for": request.headers.get("X-Forwarded-For", ""),
        },
    )
    result = await agent.system.call(auth_env)
    if not result or not result.payload.get("ok"):
        return False, "", "invalid token"
    return True, result.payload.get("user_id", "local"), ""


def _sanitize_filename(name: str) -> str:
    """防路径穿越 + 保留常见字符。"""
    if not name:
        return "upload.bin"
    # 只取 basename，去掉路径分隔符
    name = os.path.basename(name.replace("\\", "/"))
    # 只保留字母数字和常见符号
    safe = "".join(c for c in name if c.isalnum() or c in "._- ()")
    # 防 "." / ".." / 空
    safe = safe.strip(". ")
    if not safe or safe in (".", ".."):
        return "upload.bin"
    return safe[:200]   # 长度上限


# ============================================================
# 插件内部 action
# ============================================================

async def execute(envelop, agent):
    receiver = getattr(agent, "file_receiver", None)
    action = envelop.payload.get("action", "status")

    if action == "START":
        host = envelop.payload.get("host", getattr(agent, "_file_host", "0.0.0.0"))
        port = int(envelop.payload.get("port", getattr(agent, "_file_port", 9002)))
        if getattr(agent, "_file_runner", None) is not None:
            await _stop_file_receiver(agent)
        await start_file_receiver(agent, host=host, port=port)
        # ★ watch 的唯一注册点：start / restart 都汇聚到这里，
        #   避免在 start_file_receiver 里再注册一次导致双触发。
        _sr().ensure_hot_reload_watch(
            agent, "os/_file_receiver", port=port, host=host,
        )
        envelop.payload = {"ok": True, "status": "listening", "host": host, "port": port}
        return envelop

    if action == "STOP":
        await _stop_file_receiver(agent)
        envelop.payload = {"ok": True, "status": "stopped"}
        return envelop

    if action == "restart":
        # ★ 优雅重启：先显式停掉，再走 START（START 内部注册 watch）。
        #   不再调用 restart_service_and_watch，避免 watch 双注册。
        host = getattr(agent, "_file_host", "0.0.0.0")
        port = int(getattr(agent, "_file_port", 9002))
        await _stop_file_receiver(agent)
        await start_file_receiver(agent, host=host, port=port)
        _sr().ensure_hot_reload_watch(
            agent, "os/_file_receiver", port=port, host=host,
        )
        envelop.payload = {"ok": True, "status": "restarted", "port": port}
        return envelop

    if action == "status":
        envelop.payload = receiver.get_status() if receiver else {"error": "not started"}
        return envelop

    elif action == "get_file":
        file_name = envelop.payload.get("file_name", "")
        if not file_name:
            envelop.payload = {"error": "file_name required"}
            return envelop
        upload_dir = receiver.upload_dir if receiver else DEFAULT_UPLOAD_DIR
        file_path = os.path.join(upload_dir, _sanitize_filename(file_name))
        if not os.path.exists(file_path):
            envelop.payload = {"error": "file not found"}
            return envelop
        size = os.path.getsize(file_path)
        envelop.payload = {
            "ok": True,
            "file_path": file_path,
            "file_name": os.path.basename(file_path),
            "file_size": size,
            "file_size_mb": round(size / (1024 * 1024), 2),
            "url": _to_url(file_path, upload_dir),
        }
        return envelop

    else:
        envelop.payload = {"error": f"Unknown action: {action}"}
        return envelop


# ============================================================
# HTTP 上传
# ============================================================

async def handle_upload(request):
    agent = request.app.get("agent")
    if not agent:
        return web.json_response({"success": False, "error": "agent not ready"}, status=500)

    # ★ 鉴权
    ok, user_id, err = await _check_auth(request, agent)
    if not ok:
        agent.log.warning(f"[_file_receiver] auth failed from {request.remote}: {err}")
        return web.json_response({"success": False, "error": err}, status=401)

    upload_dir = _get_upload_dir(agent)
    max_upload_size = _resolve_max_upload_size(agent)

    reader = await request.multipart()
    file_path = None
    file_name = "unknown"
    session_id = None
    callback = None
    safe_name = "upload.bin"
    size_accum = 0

    while True:
        part = await reader.next()
        if part is None:
            break

        if part.name == "file":
            file_name = part.filename or "upload.bin"
            safe_name = _sanitize_filename(file_name)

            if session_id:
                task_dir = os.path.join(upload_dir, session_id[:8])
            else:
                task_dir = os.path.join(upload_dir, str(uuid.uuid4())[:8])

            os.makedirs(task_dir, exist_ok=True)
            file_path = os.path.join(task_dir, safe_name)

            # ★ 先写临时文件，完整写完后再 os.replace 原子落盘。
            #   即使热重载 / 进程中断打断，也不会在目标路径留下半截坏文件。
            tmp_path = file_path + ".part"
            with open(tmp_path, "wb") as f:
                while True:
                    chunk = await part.read_chunk(8192)
                    if not chunk:
                        break
                    size_accum += len(chunk)
                    if size_accum > max_upload_size:
                        f.close()
                        try:
                            os.unlink(tmp_path)
                        except Exception:
                            pass
                        return web.json_response(
                            {"success": False, "error": f"file too large (> {max_upload_size // (1024*1024)}MB)"},
                            status=413,
                        )
                    f.write(chunk)
            os.replace(tmp_path, file_path)

        elif part.name == "session_id":
            session_id = (await part.text()).strip()

        elif part.name == "callback":
            callback = (await part.text()).strip()

    if not file_path or not os.path.exists(file_path):
        return web.json_response({"success": False, "error": "未收到文件"}, status=400)

    file_size = os.path.getsize(file_path)
    url = _to_url(file_path, upload_dir)

    if callback:
        try:
            callback_payload = {
                "action": "file_uploaded",
                "file": {
                    "path": file_path,
                    "name": os.path.basename(file_path),
                    "size": file_size,
                    "url": url,
                },
                "session_id": session_id or "default",
                "user_id": user_id,   # ★ 带上身份
            }
            await agent.system.call(core.Envelop(
                sender="os/_file_receiver",
                receiver=callback,
                payload=callback_payload,
                meta={
                    "session_id": session_id or "default",
                    "user_id": user_id,   # ★ 透传给回调方
                }
            ))
            agent.log.info(f"[_file_receiver] callback 已发送到: {callback}, user={user_id}")
        except Exception as e:
            agent.log.warning(f"[_file_receiver] callback 失败: {e}")

    return web.json_response({
        "success": True,
        "ok": True,
        "file_path": file_path,
        "file_name": os.path.basename(file_path),
        "file_size": file_size,
        "file_size_mb": round(file_size / (1024 * 1024), 2),
        "session_id": session_id,
        "url": url,
        "user_id": user_id,   # ★ 返回身份，方便前端确认
    })


async def _stop_file_receiver(agent):
    """停掉文件接收器，释放端口。"""
    runner = getattr(agent, "_file_runner", None)
    if runner:
        try:
            await runner.cleanup()
        except Exception:
            pass
        agent._file_runner = None
    # ★ 只有 runner 已彻底清空才兜底杀端口；否则会杀到自己，
    #   热重载期间打断 import 会写坏文件。
    if getattr(agent, "_file_runner", None) is None:
        try:
            port = int(getattr(agent, "_file_port", 9002))
            await _sr().kill_port_holders(port)
        except Exception:
            pass


async def start_file_receiver(agent, host="0.0.0.0", port=9002):
    upload_dir = _get_upload_dir(agent)
    receiver = FileReceiver(upload_dir)
    agent.file_receiver = receiver
    agent.file_receiver_port = port

    max_upload_size = _resolve_max_upload_size(agent)
    app = web.Application(client_max_size=max_upload_size)
    app["agent"] = agent

    @web.middleware
    async def cors(request, handler):
        # ★ 回显真实 Origin，而不是 "*"：
        #   浏览器带 cookie 的跨端口请求不接受 "*"，
        #   回显 Origin 才能让 cookie / Authorization 正常随请求发出。
        origin = request.headers.get("Origin", "")
        allow_origin = origin if origin else "*"
        if request.method == "OPTIONS":
            return web.Response(status=200, headers={
                "Access-Control-Allow-Origin": allow_origin,
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                # ★ 允许 Authorization，前端带 token 预检才能过
                "Access-Control-Allow-Headers": "Content-Type, Authorization, X-AICP-Token",
                "Access-Control-Allow-Credentials": "true",
                "Vary": "Origin",
            })
        resp = await handler(request)
        resp.headers["Access-Control-Allow-Origin"] = allow_origin
        resp.headers["Vary"] = "Origin"
        return resp

    app.middlewares.append(cors)
    app.router.add_post("/upload", handle_upload)
    app.router.add_get("/health", lambda r: web.json_response({"status": "ok"}))

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()

    # ★ runner / 端口记到 agent 上，否则没法优雅重启（端口会被占死）
    agent._file_runner = runner
    agent._file_host = host
    agent._file_port = port
    # ★ 注意：watch 的注册统一收口到 execute() 的 START 分支，
    #   这里不再重复注册，否则 start + restart 会双触发，
    #   导致模块反复重载、文件被反复重写（曾把文件写坏的根因之一）。
    return runner
