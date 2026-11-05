"""工作区只读浏览 — 系统插件 v3.0

对 data/workspace/<session_id>/ 提供只读目录树与文件内容浏览。

数据源:
  - 工作区: data/workspace/<session_id>/

安全边界:
  - session_id 格式白名单
  - 所有路径必须 resolve 后位于 WS_ROOT 之内
  - 跳过符号链接，防目录逃逸
  - 只读，无任何写操作
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

WS_ROOT = Path("data/workspace")
SID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_FILE_BYTES = 512 * 1024
MAX_ENTRIES = 2000
CACHE_TTL = 20.0

_TEXT_EXT = {
    ".md", ".txt", ".json", ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sh", ".bash", ".log", ".csv",
    ".xml", ".sql", ".gitignore", ".env", ".conf",
}
_SKIP_NAMES = {".DS_Store", "Thumbs.db"}

_tree_cache: dict = {}


def _now() -> float:
    return time.time()


def _cache_get(key):
    hit = _tree_cache.get(key)
    if hit and (_now() - hit[0]) < CACHE_TTL:
        return hit[1]
    return None


def _cache_set(key, value):
    _tree_cache[key] = (_now(), value)
    if len(_tree_cache) > 200:
        for k in list(_tree_cache)[:100]:
            _tree_cache.pop(k, None)


def _ws_dir(session_id: str) -> Path | None:
    """定位会话工作区目录。非法 id 或不存在返回 None。"""
    if not session_id or not SID_RE.match(session_id):
        return None
    root = WS_ROOT / session_id
    try:
        if not root.is_dir():
            return None
        resolved = root.resolve()
        resolved.relative_to(WS_ROOT.resolve())
    except (OSError, ValueError):
        return None
    return root


def _mtime(entry) -> float:
    try:
        return entry.stat(follow_symlinks=False).st_mtime
    except OSError:
        return 0.0


def _safe_target(root: Path, rel: str) -> Path | None:
    """把相对路径解析为 root 内的绝对路径，越界返回 None。"""
    rel = (rel or "").strip().strip("/")
    target = root if not rel else (root / rel)
    try:
        resolved = target.resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return resolved


def _list_tree(session_id: str, rel: str) -> dict:
    root = _ws_dir(session_id)
    if root is None:
        return {"ok": False, "error": "workspace not found"}
    target = _safe_target(root, rel)
    if target is None:
        return {"ok": False, "error": "invalid path"}
    if not target.is_dir():
        return {"ok": False, "error": "not a directory"}

    key = ("tree", str(target))
    cached = _cache_get(key)
    if cached is not None:
        return cached

    entries = []
    try:
        for e in os.scandir(target):
            if e.name in _SKIP_NAMES:
                continue
            try:
                if e.is_symlink():
                    continue
                is_dir = e.is_dir(follow_symlinks=False)
                is_file = e.is_file(follow_symlinks=False)
            except OSError:
                continue
            if not (is_dir or is_file):
                continue
            size = 0
            child_count = 0
            if is_file:
                try:
                    size = e.stat(follow_symlinks=False).st_size
                except OSError:
                    size = 0
            else:
                try:
                    child_count = sum(1 for _ in os.scandir(e.path))
                except OSError:
                    child_count = 0
            entries.append({
                "name": e.name,
                "type": "dir" if is_dir else "file",
                "size": size,
                "mtime": _mtime(e),
                "ext": Path(e.name).suffix.lower(),
                "child_count": child_count,
                "hidden": e.name.startswith("."),
            })
    except OSError as exc:
        return {"ok": False, "error": f"scan failed: {exc}"}

    entries.sort(key=lambda x: (0 if x["type"] == "dir" else 1, x["name"].lower()))
    if len(entries) > MAX_ENTRIES:
        entries = entries[:MAX_ENTRIES]

    result = {
        "ok": True,
        "root": str(root),
        "path": rel or "",
        "entries": entries,
    }
    _cache_set(key, result)
    return result


def _read_file(session_id: str, rel: str) -> dict:
    root = _ws_dir(session_id)
    if root is None:
        return {"ok": False, "error": "workspace not found"}
    target = _safe_target(root, rel)
    if target is None:
        return {"ok": False, "error": "invalid path"}
    if not target.is_file():
        return {"ok": False, "error": "not a file"}
    try:
        stat = target.stat()
    except OSError as exc:
        return {"ok": False, "error": f"stat failed: {exc}"}

    size = stat.st_size
    truncated = size > MAX_FILE_BYTES
    try:
        raw = target.read_bytes()[:MAX_FILE_BYTES]
    except OSError as exc:
        return {"ok": False, "error": f"read failed: {exc}"}

    head = raw[:4096]
    is_text = False
    try:
        head.decode("utf-8")
        is_text = b"\x00" not in head
    except UnicodeDecodeError:
        is_text = False

    ext = target.suffix.lower()
    if not is_text and ext not in _TEXT_EXT:
        return {
            "ok": True, "path": rel, "size": size, "truncated": truncated,
            "binary": True, "content": "",
        }

    try:
        content = raw.decode("utf-8", errors="replace")
    except Exception:
        content = repr(raw[:4096])

    return {
        "ok": True,
        "path": rel,
        "size": size,
        "truncated": truncated,
        "binary": False,
        "content": content,
        "mtime": stat.st_mtime,
    }


async def execute(envelop, agent):
    payload = envelop.payload or {}
    action = payload.get("action", "")
    try:
        if action == "list_tree":
            result = _list_tree(
                str(payload.get("session_id") or ""),
                str(payload.get("path") or ""),
            )
        elif action == "read_file":
            result = _read_file(
                str(payload.get("session_id") or ""),
                str(payload.get("path") or ""),
            )
        else:
            result = {"ok": False, "error": f"unknown action: {action}"}
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    envelop.payload = result
    return envelop