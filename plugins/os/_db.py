"""持久化插件 — 协议 v3.0 系统插件
提供 JSON 文件读写。
"""
import json
from pathlib import Path


def _get_data_dir(agent) -> Path:
    data_dir = agent.data_dir if hasattr(agent, 'data_dir') else Path("data")
    if isinstance(data_dir, str):
        data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def _safe_filename(filename: str) -> bool:
    if ".." in filename:
        return False
    if filename.startswith("/") or filename.startswith("\\"):
        return False
    return True


def _read_json(filepath: Path) -> dict:
    if not filepath.exists():
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return {}


def _write_json(filepath: Path, data: dict) -> None:
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


async def execute(envelop, agent):
    action = envelop.payload.get("action", "read")
    data_dir = _get_data_dir(agent)
    
    if action == "read":
        filename = envelop.payload.get("filename", "data.json")
        if not _safe_filename(filename):
            envelop.payload = {"error": "Forbidden"}
            return envelop
        
        data = _read_json(data_dir / filename)
        envelop.payload = {"data": data, "filename": filename}
        return envelop
    
    elif action == "write":
        filename = envelop.payload.get("filename", "data.json")
        data = envelop.payload.get("data", {})
        if not _safe_filename(filename):
            envelop.payload = {"error": "Forbidden"}
            return envelop
        
        _write_json(data_dir / filename, data)
        envelop.payload = {"ok": True, "filename": filename}
        return envelop
    
    elif action == "delete":
        filename = envelop.payload.get("filename", "")
        if not _safe_filename(filename):
            envelop.payload = {"error": "Forbidden"}
            return envelop
        
        filepath = data_dir / filename
        if filepath.exists():
            filepath.unlink()
            envelop.payload = {"ok": True, "filename": filename}
        else:
            envelop.payload = {"error": "File not found"}
        return envelop
    
    elif action == "list":
        files = []
        for f in data_dir.iterdir():
            if f.is_file():
                files.append({"name": f.name, "size": f.stat().st_size})
        envelop.payload = {"files": files}
        return envelop
    
    envelop.payload = {"error": f"Unknown action: {action}"}
    return envelop