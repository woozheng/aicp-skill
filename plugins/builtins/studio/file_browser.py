"""Studio 文件浏览 — 列出项目文件"""
from pathlib import Path


async def execute(envelop, agent):
    path = envelop.payload.get("path", "plugins/applications")
    root = Path(path)
    if not root.exists():
        envelop.payload = {"error": f"Path not found: {path}"}
        return envelop

    def scan(d: Path, prefix=""):
        items = []
        for f in sorted(d.iterdir()):
            if f.name.startswith(".") or f.name == "__pycache__":
                continue
            rel = f"{prefix}{f.name}"
            if f.is_dir():
                items.append({"name": rel, "type": "dir", "children": scan(f, rel + "/")})
            else:
                items.append({"name": rel, "type": "file"})
        return items

    envelop.payload = {"items": scan(root), "path": path}
    return envelop
