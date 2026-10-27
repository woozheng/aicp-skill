# plugins/builtins/studio/api.py
"""Studio API — 项目文件管理（使用系统统一 token 鉴权 os/_auth）"""
import os
import re
from pathlib import Path
import core
from core import Envelop


def _extract_token(envelop) -> str:
    """从请求 body / 网关 meta 中提取 token。"""
    token = envelop.payload.get("token", "") or envelop.meta.get("token", "")
    if not token:
        auth_header = envelop.meta.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    if not token:
        token = envelop.meta.get("cookie_token", "")
    return (token or "").strip()


async def _authorize(envelop, agent) -> dict:
    """统一认证：优先采信网关盖章结果，否则调用 os/_auth 校验 token。"""
    stamped = envelop.meta.get("auth_result") or {}
    if stamped.get("ok"):
        return {
            "ok": True,
            "user_id": stamped.get("user_id", "local"),
            "source": stamped.get("source", "gateway"),
        }

    # 网关已盖章（本地 / 同源放行）：直接通过
    if envelop.meta.get("auth_user_id"):
        return {
            "ok": True,
            "user_id": envelop.meta.get("auth_user_id", "local"),
            "source": envelop.meta.get("auth_source", "gateway"),
        }

    token = _extract_token(envelop)
    if not token:
        return {"ok": False, "error": "未提供 token"}

    try:
        result = await agent.system.call(core.Envelop(
            sender="builtins/studio/api",
            receiver="os/_auth",
            payload={"action": "verify"},
            meta={"token": token},
        ))
    except Exception as e:
        return {"ok": False, "error": f"认证失败: {e}"}

    payload = getattr(result, "payload", {}) or {}
    if payload.get("ok"):
        return {
            "ok": True,
            "user_id": payload.get("user_id", "local"),
            "source": payload.get("source", "hmac"),
        }
    return {"ok": False, "error": payload.get("error", "Unauthorized")}


async def execute(envelop, agent):
    auth = await _authorize(envelop, agent)
    if not auth.get("ok"):
        return Envelop(receiver=envelop.sender, payload={"ok": False, "error": auth.get("error", "无权限"), "code": "unauthorized"})
    
    action = envelop.payload.get("action", "")
    app_id = envelop.payload.get("app_id", "")
    
    # 列出所有项目
    if action == "list_projects":
        apps_dir = Path("plugins/applications")
        if not apps_dir.exists():
            return Envelop(receiver=envelop.sender, payload={"ok": True, "projects": []})
        apps = [d.name for d in apps_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
        return Envelop(receiver=envelop.sender, payload={"ok": True, "projects": sorted(apps)})
    
    # 列出项目文件树
    if action == "list_files":
        if not app_id:
            return Envelop(receiver=envelop.sender, payload={"ok": False, "error": "缺少 app_id"})
        result = {}
        for base, label in [("plugins/applications", "plugins"), ("www/apps", "www")]:
            root = Path(f"{base}/{app_id}")
            if not root.exists():
                result[label] = []
                continue
            tree = []
            for item in sorted(root.rglob("*")):
                if item.name in ("__pycache__", ".git") or item.suffix == ".pyc" or item.name == "__init__.py":
                    continue
                rel = str(item.relative_to(root)).replace("\\", "/")
                tree.append({"name": rel, "dir": item.is_dir(), "path": f"{base}/{app_id}/{rel}"})
            result[label] = tree
        # 业务数据目录：只列当前项目自己的 data/{app_id}，不存在则为空
        droot = Path("data") / app_id
        dtree = []
        if droot.exists():
            for item in sorted(droot.rglob("*")):
                if item.name in ("__pycache__", ".git") or item.suffix == ".pyc":
                    continue
                rel = str(item.relative_to(droot)).replace("\\", "/")
                dtree.append({"name": rel, "dir": item.is_dir(), "path": f"data/{app_id}/{rel}"})
        result["data"] = dtree

        return Envelop(receiver=envelop.sender, payload={"ok": True, "tree": result})
    
    # 读文件
    if action == "read_file":
        path = envelop.payload.get("path", "")
        if not path:
            return Envelop(receiver=envelop.sender, payload={"ok": False, "error": "缺少 path"})
        full = Path(path).resolve()
        allowed = [Path("plugins/applications").resolve(), Path("www").resolve(), Path("plugins/_cache").resolve(), Path("data").resolve()]
        if not any(str(full).startswith(str(p)) for p in allowed):
            return Envelop(receiver=envelop.sender, payload={"ok": False, "error": "路径越权"})
        if not full.exists() or full.is_dir():
            return Envelop(receiver=envelop.sender, payload={"ok": False, "error": "文件不存在"})
        content = full.read_text(encoding="utf-8", errors="replace")
        return Envelop(receiver=envelop.sender, payload={"ok": True, "content": content})
    
    # 写单个文件（编辑器保存用）
    if action == "write_file":
        path = envelop.payload.get("path", "")
        content = envelop.payload.get("content", "")
        if not path:
            return Envelop(receiver=envelop.sender, payload={"ok": False, "error": "缺少 path"})
        full = Path(path).resolve()
        allowed = [Path("plugins/applications").resolve(), Path("www").resolve(), Path("plugins/_cache").resolve(), Path("data").resolve()]
        if not any(str(full).startswith(str(p)) for p in allowed):
            return Envelop(receiver=envelop.sender, payload={"ok": False, "error": "路径越权"})
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        return Envelop(receiver=envelop.sender, payload={"ok": True, "path": str(full)})
    
    # 保存代码块（书签用，从 AI 回复中提取 === TYPE: path === 块）
    if action == "save_blocks":
        text = envelop.payload.get("text", "")
        app_id = envelop.payload.get("app_id", "")
        if not text:
            return Envelop(receiver=envelop.sender, payload={"ok": False, "error": "缺少 text"})
        
        saved = []
        pattern = r'===\s*(PLUGIN|HTML|CSS|JS|APP|YAML):\s*(\S+)\s*===\s*\n(.*?)=== END ==='
        for match in re.finditer(pattern, text, re.DOTALL):
            btype = match.group(1).upper()
            filepath = match.group(2).strip()
            code = match.group(3).strip()
            # APP/YAML 块正文里的 {project} 占位符也要替换成真实项目名（id / static_root）
            if app_id and btype in ("APP", "YAML") and "{project}" in code:
                code = code.replace("{project}", app_id)
            
            if app_id:
                filepath = filepath.replace("{project}", app_id)
            
            # 如果 AI 已经输出了完整路径，直接使用
            if '/' in filepath and (filepath.startswith('plugins/') or filepath.startswith('www/')):
                # 统一落到 www/apps/ 下（协议里的 www/{project} 也要进 apps）
                if filepath.startswith('www/') and not filepath.startswith('www/apps/'):
                    filepath = 'www/apps/' + filepath[4:]
                full = Path(filepath)
            else:
                # 只有文件名，根据项目拼路径
                if filepath.endswith(('.css', '.js', '.yaml', '.yml', '.json', '.xml', '.svg', '.html', '.htm')):
                    full = Path(f"www/apps/{app_id}") / filepath if app_id else Path(f"www/apps/{filepath}")
                else:
                    full = Path(f"plugins/_cache") / filepath if not app_id else Path(f"plugins/applications/{app_id}") / filepath
            
            full = full.resolve()
            allowed = [Path("plugins/applications").resolve(), Path("www").resolve(), Path("plugins/_cache").resolve(), Path("data").resolve()]
            if not any(str(full).startswith(str(p)) for p in allowed):
                continue
            
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(code, encoding="utf-8")
            saved.append(str(full))
        
        return Envelop(receiver=envelop.sender, payload={"ok": True, "saved": saved, "count": len(saved)})
    # 删除文件
    if action == "delete_file":
        path = envelop.payload.get("path", "")
        if not path:
            return Envelop(receiver=envelop.sender, payload={"ok": False, "error": "缺少 path"})
        full = Path(path).resolve()
        allowed = [Path("plugins/applications").resolve(), Path("www").resolve(), Path("plugins/_cache").resolve(), Path("data").resolve()]
        if not any(str(full).startswith(str(p)) for p in allowed):
            return Envelop(receiver=envelop.sender, payload={"ok": False, "error": "路径越权"})
        if full.exists():
            full.unlink()
        return Envelop(receiver=envelop.sender, payload={"ok": True})
    
    # 获取协议内容
    if action == "get_protocol":
        protocol_path = Path("plugins/builtins/studio/aicp.md")
        if protocol_path.exists():
            protocol = protocol_path.read_text(encoding="utf-8")
            return Envelop(receiver=envelop.sender, payload={"ok": True, "protocol": protocol})
        return Envelop(receiver=envelop.sender, payload={"ok": False, "error": "协议文件不存在"})
    
    return Envelop(receiver=envelop.sender, payload={"ok": False, "error": f"未知操作: {action}"})