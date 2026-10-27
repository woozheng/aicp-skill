"""文件接收插件 — 端口 = gateway + 2，接收 multipart 上传
兼容：拆片机（只传 file）和标准 AICP（file + session_id + callback）

v2.1 改造：
- 上传接口接入 os/_auth 鉴权（local/hybrid/token 三档）
- 来源判断查 X-Forwarded-For，反向代理后不误判本机
- 文件名清洗防路径穿越
- CORS 允许 Authorization 头
"""
import os
import uuid
from pathlib import Path
from aiohttp import web
import core


DEFAULT_UPLOAD_DIR = "data/uploads"
MAX_UPLOAD_SIZE = 50 * 1024 * 1024   # 50MB


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
    origin = request.headers.get("Origin", "")
    if not origin:
        return False
    host = request.host
    return origin in (f"http://{host}", f"https://{host}")


async def _check_auth(request, agent) -> tuple[bool, str, str]:
    """返回 (ok, user_id, error_msg)。"""
    token = _extract_token(request)

    # 本机 / 同源：跳过鉴权，user_id=local
    if _is_local_request(request) or _is_same_origin(request):
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

            with open(file_path, "wb") as f:
                while True:
                    chunk = await part.read_chunk(8192)
                    if not chunk:
                        break
                    size_accum += len(chunk)
                    if size_accum > MAX_UPLOAD_SIZE:
                        f.close()
                        try:
                            os.unlink(file_path)
                        except Exception:
                            pass
                        return web.json_response(
                            {"success": False, "error": f"file too large (> {MAX_UPLOAD_SIZE // (1024*1024)}MB)"},
                            status=413,
                        )
                    f.write(chunk)

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


async def start_file_receiver(agent, host="0.0.0.0", port=9002):
    upload_dir = _get_upload_dir(agent)
    receiver = FileReceiver(upload_dir)
    agent.file_receiver = receiver
    agent.file_receiver_port = port

    app = web.Application(client_max_size=MAX_UPLOAD_SIZE)
    app["agent"] = agent

    @web.middleware
    async def cors(request, handler):
        if request.method == "OPTIONS":
            return web.Response(status=200, headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                # ★ 允许 Authorization，前端带 token 预检才能过
                "Access-Control-Allow-Headers": "Content-Type, Authorization, X-AICP-Token",
            })
        resp = await handler(request)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    app.middlewares.append(cors)
    app.router.add_post("/upload", handle_upload)
    app.router.add_get("/health", lambda r: web.json_response({"status": "ok"}))

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()

    return runner
