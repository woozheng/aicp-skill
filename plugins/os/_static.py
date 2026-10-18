"""静态文件服务 — 协议 v3.0 系统插件"""
from pathlib import Path
import mimetypes
import os


def _guess_type(path: Path) -> str:
    """根据文件扩展名猜测 MIME 类型"""
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


def _safe_path(file_path: str) -> bool:
    """路径安全检查，防止目录穿越"""
    if not file_path:
        return True
    # 拒绝绝对路径和父目录引用
    if file_path.startswith("/") or file_path.startswith("\\"):
        return False
    if ".." in file_path:
        return False
    # 拒绝 Windows 盘符
    if len(file_path) >= 2 and file_path[1] == ":":
        return False
    return True


def _normalize_path(base: Path, file_path: str) -> Path | None:
    """规范化路径并确保在 base 目录内"""
    target = (base / file_path).resolve()
    base_resolved = base.resolve()
    
    # 确保解析后的路径在 base 目录内
    if not str(target).startswith(str(base_resolved)):
        return None
    
    return target


def _try_files(base: Path, file_path: str) -> Path | None:
    """按优先级尝试匹配文件：
    1. 精确路径
    2. 补全 .html
    3. 目录下的 index.html
    """
    # 精确匹配
    target = _normalize_path(base, file_path)
    if target and target.exists() and target.is_file():
        return target
    
    # 补全 .html
    target = _normalize_path(base, f"{file_path}.html")
    if target and target.exists() and target.is_file():
        return target
    
    # 目录下的 index.html
    target = _normalize_path(base, f"{file_path}/index.html")
    if target and target.exists() and target.is_file():
        return target
    
    return None


def _serve_file(found: Path, envelop):
    """读取文件并设置响应"""
    content = found.read_bytes()
    envelop.meta["static_content"] = content
    envelop.meta["content_type"] = _guess_type(found)
    envelop.meta["file_size"] = found.stat().st_size
    envelop.payload = {"found": True, "source": str(found)}


async def execute(envelop, agent):
    action = envelop.payload.get("action", "serve")
    
    # ============================================================
    # serve — 提供静态文件
    # ============================================================
    if action == "serve":
        file_path = envelop.payload.get("file_path", "")
        
        # 从 meta 里拿路径（Gateway 可能放在这里）
        if not file_path:
            file_path = envelop.meta.get("path", "").lstrip("/")
        
        if not file_path:
            file_path = "index.html"
        
        # 安全检查
        if not _safe_path(file_path):
            envelop.payload = {"error": "Forbidden"}
            return envelop
        
        # 1. 先查项目 www/ 目录
        parts = file_path.split("/", 1)
        if len(parts) == 2:
            project, rest = parts
            project_www = Path(f"plugins/{project}/www")
            if project_www.exists():
                found = _try_files(project_www, rest)
                if found:
                    _serve_file(found, envelop)
                    envelop.meta["source_dir"] = f"plugins/{project}/www"
                    return envelop
        
        # 2. 再查全局 www/ 目录
        global_www = Path("www")
        found = _try_files(global_www, file_path)
        if found:
            _serve_file(found, envelop)
            envelop.meta["source_dir"] = "www"
            return envelop
        
        # 3. 查 data/ 目录（如果允许）
        data_www = Path("data/www")
        found = _try_files(data_www, file_path)
        if found:
            _serve_file(found, envelop)
            envelop.meta["source_dir"] = "data/www"
            return envelop
        
        envelop.payload = {"error": "Not found"}
        return envelop
    
    # ============================================================
    # serve_data — 提供 data/ 目录下的文件
    # ============================================================
    elif action == "serve_data":
        file_path = envelop.payload.get("file_path", "")
        
        if not _safe_path(file_path):
            envelop.payload = {"error": "Forbidden"}
            return envelop
        
        data_dir = Path("data")
        target = _normalize_path(data_dir, file_path)
        
        if target and target.exists() and target.is_file():
            _serve_file(target, envelop)
            return envelop
        
        envelop.payload = {"error": "Not found"}
        return envelop
    
    # ============================================================
    # list — 列出所有 HTML 页面
    # ============================================================
    elif action == "list":
        pages = []
        seen = set()
        
        # 项目 www/ 目录
        plugins_dir = Path("plugins")
        if plugins_dir.exists():
            for project_dir in plugins_dir.iterdir():
                if not project_dir.is_dir():
                    continue
                if project_dir.name in ("os", "__pycache__", "builtins"):
                    continue
                
                www_dir = project_dir / "www"
                if www_dir.exists():
                    for f in www_dir.rglob("*.html"):
                        rel = str(f.relative_to(www_dir)).replace("\\", "/")
                        url = f"/{project_dir.name}/{rel}"
                        if url not in seen:
                            seen.add(url)
                            pages.append({
                                "path": f"{project_dir.name}/{rel}",
                                "url": url,
                                "size": f.stat().st_size,
                            })
        
        # 全局 www/ 目录
        global_www = Path("www")
        if global_www.exists():
            for f in global_www.rglob("*.html"):
                rel = str(f.relative_to(global_www)).replace("\\", "/")
                url = f"/{rel}"
                if url not in seen:
                    seen.add(url)
                    pages.append({
                        "path": rel,
                        "url": url,
                        "size": f.stat().st_size,
                    })
        
        # 排序：目录在前，文件名升序
        pages.sort(key=lambda p: (p["path"].count("/"), p["path"]))
        
        envelop.payload = {"pages": pages, "total": len(pages)}
        return envelop
    
    envelop.payload = {"error": f"Unknown action: {action}"}
    return envelop