"""文件接收插件 — 端口 = gateway + 2，接收 multipart 上传
兼容：拆片机（只传 file）和标准 AICP（file + session_id + callback）

v2.0 改进：
- 上传目录可配（aicp.yaml: upload.dir，默认 data/uploads）
- 上传后返回 url（可直接 <img src>）
- get_file 返回 url
"""
import os
import uuid
from pathlib import Path
from aiohttp import web
import core


DEFAULT_UPLOAD_DIR = "data/uploads"


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
    """把绝对路径转成可访问的 URL"""
    try:
        rel = Path(file_path).resolve().relative_to(Path(upload_dir).resolve())
        return f"/data/uploads/{rel.as_posix()}"
    except Exception:
        return ""


async def execute(envelop, agent):
    """供其他插件通过 agent.system.call 调用"""
    receiver = getattr(agent, 'file_receiver', None)
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
        file_path = os.path.join(upload_dir, file_name)
        if not os.path.exists(file_path):
            envelop.payload = {"error": "file not found"}
            return envelop
        size = os.path.getsize(file_path)
        envelop.payload = {
            "ok": True,
            "file_path": file_path,
            "file_name": file_name,
            "file_size": size,
            "file_size_mb": round(size / (1024 * 1024), 2),
            "url": _to_url(file_path, upload_dir),
        }
        return envelop

    else:
        envelop.payload = {"error": f"Unknown action: {action}"}
        return envelop


async def handle_upload(request):
    """处理 multipart 文件上传，兼容拆片机（无 session_id）和标准 AICP（带 session_id）"""
    agent = request.app.get("agent")
    upload_dir = _get_upload_dir(agent)

    reader = await request.multipart()
    file_path = None
    file_name = "unknown"
    session_id = None
    callback = None
    safe_name = "upload.bin"

    while True:
        part = await reader.next()
        if part is None:
            break

        if part.name == "file":
            file_name = part.filename or "upload.bin"
            safe_name = "".join(c for c in file_name if c.isalnum() or c in "._- ()") or "upload.bin"

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
                "session_id": session_id or "default"
            }
            if agent:
                await agent.system.call(core.Envelop(
                    sender="os/_file_receiver",
                    receiver=callback,
                    payload=callback_payload,
                    meta={"session_id": session_id or "default"}
                ))
                agent.log.info(f"[_file_receiver] callback 已发送到: {callback}")
        except Exception as e:
            if agent:
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
    })


async def start_file_receiver(agent, host="0.0.0.0", port=9002):
    """启动文件接收服务，挂到 agent 上"""
    upload_dir = _get_upload_dir(agent)
    receiver = FileReceiver(upload_dir)
    agent.file_receiver = receiver
    agent.file_receiver_port = port

    app = web.Application()
    app["agent"] = agent

    @web.middleware
    async def cors(request, handler):
        if request.method == "OPTIONS":
            return web.Response(status=200, headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
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
