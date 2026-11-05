"""local_browser_launcher — 本机浏览器启动器（标准浏览器弹窗插件）

职责：
- 首次启动且 console 未打开时，自动弹本机浏览器
- console 页面打开后写标记，避免重复弹
- 提供 mark / reset / status / open 标准接口

判定逻辑：
- data/.console_connected 存在 → console 已打开 → 不弹
- 不存在 → 首次启动 → 弹浏览器
- 环境变量 AICP_NO_BROWSER=1 → 永不弹（CI / 无头环境）

地址：
- config.host 为 0.0.0.0 / :: 时，浏览器 URL 用 127.0.0.1
  （0.0.0.0 是 bind 地址，浏览器访问会被安全策略拦截）

action：
  open_if_needed     默认。按需打开本地 console（不打开外部网页）
  open               打开任意 URL（用户说「打开某网页」时用这个）
  mark_connected     console 上报「已打开」，只写标记文件，不打开浏览器
  mark_disconnected  console 上报「已关闭」，删标记
  status             查询标记状态（不打开浏览器）
  reset              清标记（强制下次启动重新弹）
"""
import os
import webbrowser
from pathlib import Path

_MARKER = Path("data") / ".console_connected"
_ENV_NO_BROWSER = "AICP_NO_BROWSER"


def _marker_path() -> Path:
    return _MARKER


def _no_browser() -> bool:
    v = os.environ.get(_ENV_NO_BROWSER, "").strip().lower()
    return v not in ("", "0", "false", "no")


def _is_marked() -> bool:
    try:
        return _marker_path().exists()
    except Exception:
        return False


def _write_marker(url: str) -> None:
    p = _marker_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(url or "", encoding="utf-8")


def _clear_marker() -> None:
    p = _marker_path()
    if p.exists():
        p.unlink()


def _base_url(agent) -> str:
    config = getattr(agent, "config", None) or {}
    host = config.get("host", "127.0.0.1")
    port = config.get("port", 9000)
    # 0.0.0.0 / :: 是 bind 地址，不是浏览器可访问地址
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    return f"http://{host}:{port}"


async def execute(envelop, agent):
    payload = envelop.payload or {}
    action = payload.get("action", "open_if_needed")

    # ── 打开任意 URL（不做「已打开就跳过」判定）──
    if action == "open":
        url = (payload.get("url") or "").strip()
        if not url:
            envelop.payload = {"ok": False, "error": "url required"}
            return envelop
        if not (url.startswith("http://") or url.startswith("https://")):
            envelop.payload = {"ok": False, "error": "url must start with http:// or https://"}
            return envelop
        try:
            opened = bool(webbrowser.open(url))
        except Exception as e:
            envelop.payload = {"ok": False, "error": str(e), "url": url}
            return envelop
        envelop.payload = {"ok": True, "opened": opened, "url": url}
        return envelop

    # ── console 上报已打开（只写标记，不打开浏览器）──
    if action == "mark_connected":
        try:
            _write_marker(payload.get("url", ""))
            envelop.payload = {"ok": True, "marked": True}
        except Exception as e:
            envelop.payload = {"ok": False, "error": str(e)}
        return envelop

    # ── console 上报已关闭 ──
    if action == "mark_disconnected":
        try:
            _clear_marker()
            envelop.payload = {"ok": True, "marked": False}
        except Exception as e:
            envelop.payload = {"ok": False, "error": str(e)}
        return envelop

    # ── 查询状态 ──
    if action == "status":
        envelop.payload = {
            "ok": True,
            "marked": _is_marked(),
            "marker_path": str(_marker_path()),
            "no_browser": _no_browser(),
            "url": _base_url(agent),
        }
        return envelop

    # ── 强制清标记 ──
    if action == "reset":
        try:
            _clear_marker()
            envelop.payload = {"ok": True, "marked": False}
        except Exception as e:
            envelop.payload = {"ok": False, "error": str(e)}
        return envelop

    # ── 默认：按需打开本地 console ──
    if action != "open_if_needed":
        envelop.payload = {"ok": True, "skipped": True, "reason": f"unknown action: {action}"}
        return envelop

    if _no_browser():
        envelop.payload = {"ok": True, "opened": False, "reason": f"{_ENV_NO_BROWSER} set"}
        return envelop

    url = _base_url(agent)

    if _is_marked():
        envelop.payload = {"ok": True, "opened": False, "reason": "console already open", "url": url}
        return envelop

    try:
        opened = bool(webbrowser.open(url))
    except Exception:
        opened = False

    envelop.payload = {"ok": True, "opened": opened, "url": url}
    return envelop
