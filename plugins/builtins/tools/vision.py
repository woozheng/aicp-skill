"""builtins/tools/vision — 图片识别与 OCR 插件

定位：给主控提供「看图」能力，且对环境/模型能力做足容错。

actions:
    describe  多模态模型看图描述（需要模型支持视觉）
    ocr       OCR 文字提取（本地 rapidocr_onnxruntime，不需要模型）
    info      探测当前环境能力（模型是否视觉、OCR 库是否可用）
    install   安装缺失依赖（pip）

容错设计:
    1. 模型不支持视觉 → 返回明确错误 + 建议改用 ocr，不抛异常
    2. OCR 库缺失 → 自动尝试安装；装不上 → 返回错误并说明缺什么
    3. 文件不存在 / 不是图片 → 明确错误
    4. 所有异常都被捕获，返回 {ok: False, error: ...}
"""

import asyncio
import base64
import importlib.util
import json
import mimetypes
import os
import subprocess
import sys
from pathlib import Path

import core

# 支持的图片扩展名
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".ico", ".avif", ".tif", ".tiff"}

# OCR 引擎候选（按优先级）
OCR_ENGINES = ["rapidocr_onnxruntime", "rapidocr", "paddleocr", "easyocr"]

# 模型视觉能力探测结果缓存
_MODEL_INFO_CACHE = {}


def _payload_of(envelop):
    raw = envelop.payload if isinstance(envelop.payload, dict) else {}
    return raw.get("payload") if isinstance(raw.get("payload"), dict) else raw


def _resolve_file(path_str):
    """把用户给的路径解析成真实存在的文件。

    支持：
      - 绝对路径
      - 相对仓库根的路径（data/uploads/xxx/1.jpg）
      - data/uploads/<8位目录>/<文件名> 形式
      - 纯文件名 → 在 data/uploads 下递归找最新一个
    """
    if not path_str:
        return None, "path 为空"
    p = Path(str(path_str).strip().strip('"').strip("'"))
    candidates = []
    if p.is_absolute():
        candidates.append(p)
    else:
        candidates.append(Path.cwd() / p)
        candidates.append(Path(p))
    for c in candidates:
        try:
            if c.is_file():
                return c, None
        except OSError:
            continue

    # 兜底：按文件名在 data/uploads 下找
    name = p.name
    up = Path("data") / "uploads"
    if up.is_dir() and name:
        try:
            hits = [f for f in up.rglob(name) if f.is_file()]
            if hits:
                hits.sort(key=lambda f: f.stat().st_mtime, reverse=True)
                return hits[0], None
        except OSError:
            pass
    return None, "文件不存在: %s" % path_str


def _mime_of(path):
    mime, _ = mimetypes.guess_type(str(path))
    if not mime or not mime.startswith("image/"):
        mime = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp",
            ".svg": "image/svg+xml", ".ico": "image/x-icon", ".avif": "image/avif",
            ".tif": "image/tiff", ".tiff": "image/tiff",
        }.get(Path(path).suffix.lower(), "application/octet-stream")
    return mime


def _read_b64(path):
    raw = Path(path).read_bytes()
    if not raw:
        return None, "文件为空"
    return base64.b64encode(raw).decode("ascii"), None


def _has_lib(name):
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _pick_ocr_engine():
    """返回可用的 OCR 引擎名，没有则返回 None。"""
    for name in OCR_ENGINES:
        if _has_lib(name):
            return name
    return None


async def _pip_install(pkgs):
    """安装依赖，返回 (ok, detail)。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", *pkgs,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode == 0:
            return True, (out or b"").decode("utf-8", "replace")[-800:]
        return False, (err or b"").decode("utf-8", "replace")[-800:]
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


# ── 模型能力探测 ───────────────────────────────────────────────

async def _probe_model(agent):
    """探测当前 LLM 是否支持视觉输入。

    返回 dict: {available, model, supports_vision, reason}
    """
    llm = getattr(agent, "llm", None)
    if not llm:
        return {
            "available": False,
            "model": None,
            "supports_vision": None,
            "reason": "引擎未配置模型（agent.llm 为 None），请在 aicp.yaml 配置 models.providers",
        }

    # 尝试拿模型名
    model = None
    for attr in ("model", "model_name", "name"):
        v = getattr(llm, attr, None)
        if isinstance(v, str) and v:
            model = v
            break
    if not model:
        cfg = getattr(llm, "config", None)
        if isinstance(cfg, dict):
            m = cfg.get("model")
            if isinstance(m, str):
                model = m
    if not model:
        try:
            cfgs = getattr(llm, "configs", None)
            if isinstance(cfgs, list) and cfgs:
                m = cfgs[0].get("model") if isinstance(cfgs[0], dict) else None
                if isinstance(m, str):
                    model = m
        except Exception:
            pass

    key = model or "__unknown__"
    cached = _MODEL_INFO_CACHE.get(key)
    if cached is not None:
        return cached

    # 探测方法：发一条带 image_url 的最小请求，看是否被拒
    # 用 1x1 PNG，任何支持视觉的模型都会正常返回（哪怕内容无意义）
    tiny = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    supports = None
    reason = ""
    try:
        res = await llm.chat([
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "ok"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + tiny}},
                ],
            }
        ])
        supports = True
        _ = res
    except Exception as e:
        msg = "%s: %s" % (type(e).__name__, str(e)[:300])
        low = msg.lower()
        if any(k in low for k in ("image", "vision", "multimodal", "multi-modal",
                                  "not support", "unsupported", "content",
                                  "invalid_request", "badrequest", "400")):
            supports = False
            reason = msg
        else:
            # 可能是网络/超时等非能力问题，不下结论
            supports = None
            reason = msg

    info = {
        "available": True,
        "model": model,
        "supports_vision": supports,
        "reason": reason,
    }
    _MODEL_INFO_CACHE[key] = info
    return info


# ── OCR ────────────────────────────────────────────────────────

def _ocr_rapidocr(path):
    """rapidocr_onnxruntime 文本识别。"""
    from rapidocr_onnxruntime import RapidOCR
    engine = RapidOCR()
    result, _ = engine(str(path))
    lines = []
    if result:
        for item in result:
            try:
                # item = [box, text, score]
                text = item[1]
                score = float(item[2]) if len(item) > 2 else None
                box = item[0]
                lines.append({
                    "text": str(text),
                    "score": score,
                    "box": box if isinstance(box, list) else None,
                })
            except Exception:
                lines.append({"text": str(item), "score": None, "box": None})
    return lines


def _ocr_easyocr(path):
    import easyocr
    reader = easyocr.Reader(["ch_sim", "en"], gpu=False)
    result = reader.readtext(str(path))
    lines = []
    for item in result or []:
        lines.append({"text": str(item[1]), "score": float(item[2]), "box": item[0]})
    return lines


def _ocr_paddle(path):
    from paddleocr import PaddleOCR
    engine = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
    res = engine.ocr(str(path), cls=True)
    lines = []
    if res and res[0]:
        for item in res[0]:
            lines.append({
                "text": str(item[1][0]),
                "score": float(item[1][1]) if len(item[1]) > 1 else None,
                "box": item[0],
            })
    return lines


OCR_RUNNERS = {
    "rapidocr_onnxruntime": _ocr_rapidocr,
    "rapidocr": _ocr_rapidocr,
    "paddleocr": _ocr_paddle,
    "easyocr": _ocr_easyocr,
}


async def _run_ocr(path, engine_name):
    runner = OCR_RUNNERS.get(engine_name)
    if runner is None:
        return None, "没有匹配的 OCR 引擎实现: %s" % engine_name
    try:
        # OCR 是 CPU 密集同步操作，放线程池避免阻塞事件循环
        return await asyncio.to_thread(runner, path), None
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, str(e)[:300])


# ── actions ───────────────────────────────────────────────────

async def _act_info(payload, agent):
    info = await _probe_model(agent)
    engine = _pick_ocr_engine()
    return {
        "ok": True,
        "data": {
            "llm": info,
            "ocr": {
                "available": bool(engine),
                "engine": engine,
                "candidates": OCR_ENGINES,
                "install_hint": "pip install rapidocr_onnxruntime" if not engine else None,
            },
            "libs": {
                "PIL": _has_lib("PIL"),
                "numpy": _has_lib("numpy"),
                "cv2": _has_lib("cv2"),
            },
        },
    }


async def _act_describe(payload, agent):
    path_str = payload.get("path") or payload.get("file") or payload.get("image") or ""
    prompt = str(payload.get("prompt") or "请详细描述这张图片里有什么。").strip()
    if not prompt:
        prompt = "请详细描述这张图片里有什么。"

    f, err = _resolve_file(path_str)
    if not f:
        return {"ok": False, "error": err}

    if f.suffix.lower() not in IMAGE_EXTS:
        return {"ok": False, "error": "不是图片文件: %s（支持 %s）" % (f.name, ", ".join(sorted(IMAGE_EXTS)))}

    llm = getattr(agent, "llm", None)
    if not llm:
        return {
            "ok": False,
            "error": "引擎未配置模型（agent.llm 为 None），无法做图像识别",
            "hint": "请在 aicp.yaml 配置 models.providers 后重试；或改用 action=ocr（本地 OCR，不需要模型）",
            "error_category": "LLM_NOT_CONFIGURED",
        }

    info = await _probe_model(agent)
    if info.get("supports_vision") is False:
        return {
            "ok": False,
            "error": "当前模型不支持图像识别: %s" % (info.get("model") or "未知模型"),
            "reason": info.get("reason") or "",
            "hint": "换用支持视觉的模型，或改用 action=ocr（本地 OCR，不需要模型）",
            "error_category": "MODEL_NO_VISION",
            "model": info.get("model"),
        }

    b64, err = _read_b64(f)
    if not b64:
        return {"ok": False, "error": err}

    mime = _mime_of(f)
    try:
        res = await llm.chat([
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (mime, b64)}},
                ],
            }
        ])
    except Exception as e:
        return {
            "ok": False,
            "error": "调用视觉模型失败: %s: %s" % (type(e).__name__, str(e)[:300]),
            "error_category": "LLM_CALL_FAILED",
            "hint": "可改用 action=ocr 做纯文字提取",
        }

    return {
        "ok": True,
        "data": {
            "reply": str(res or "").strip(),
            "model": info.get("model"),
            "file": {
                "path": str(f),
                "name": f.name,
                "size": f.stat().st_size,
                "mime": mime,
            },
        },
    }


async def _act_ocr(payload, agent):
    path_str = payload.get("path") or payload.get("file") or payload.get("image") or ""
    auto_install = payload.get("auto_install", True)

    f, err = _resolve_file(path_str)
    if not f:
        return {"ok": False, "error": err}

    engine = _pick_ocr_engine()
    install_log = None

    if not engine:
        if not auto_install:
            return {
                "ok": False,
                "error": "环境缺少 OCR 依赖",
                "missing": OCR_ENGINES,
                "hint": "先调用 action=install 安装，或设 auto_install=true 自动安装",
                "error_category": "DEPENDENCY",
            }
        ok, detail = await _pip_install(["rapidocr_onnxruntime"])
        install_log = detail
        if not ok:
            return {
                "ok": False,
                "error": "OCR 依赖安装失败",
                "detail": detail,
                "missing": OCR_ENGINES,
                "hint": "可手动执行 pip install rapidocr_onnxruntime",
                "error_category": "DEPENDENCY",
            }
        importlib.invalidate_caches()
        engine = _pick_ocr_engine()
        if not engine:
            return {
                "ok": False,
                "error": "安装后仍找不到可用 OCR 引擎",
                "detail": install_log,
                "error_category": "DEPENDENCY",
            }

    lines, err = await _run_ocr(f, engine)
    if err:
        return {
            "ok": False,
            "error": "OCR 识别失败: %s" % err,
            "engine": engine,
            "error_category": "RUNTIME_INTERNAL",
        }

    text = "\n".join(l["text"] for l in (lines or []) if l.get("text"))
    return {
        "ok": True,
        "data": {
            "text": text,
            "lines": lines or [],
            "count": len(lines or []),
            "engine": engine,
            "installed_now": bool(install_log),
            "file": {"path": str(f), "name": f.name, "size": f.stat().st_size},
        },
    }


async def _act_install(payload, agent):
    pkgs = payload.get("packages")
    if not pkgs:
        pkgs = ["rapidocr_onnxruntime"]
    if isinstance(pkgs, str):
        pkgs = [pkgs]
    ok, detail = await _pip_install(list(pkgs))
    importlib.invalidate_caches()
    return {
        "ok": ok,
        "error": None if ok else "安装失败",
        "data": {
            "installed": ok,
            "packages": list(pkgs),
            "detail": detail,
            "ocr_engine": _pick_ocr_engine(),
        },
    }


ACTIONS = {
    "describe": _act_describe,
    "ocr": _act_ocr,
    "info": _act_info,
    "install": _act_install,
}


async def execute(envelop, agent):
    try:
        payload = _payload_of(envelop)
        action = str(payload.get("action") or "describe")
        handler = ACTIONS.get(action)
        if handler is None:
            envelop.payload = {
                "ok": False,
                "error": "Unknown action: %s" % action,
                "available_actions": list(ACTIONS.keys()),
            }
            return envelop

        result = await handler(payload, agent)
        envelop.payload = result
        return envelop
    except Exception as e:
        try:
            agent.log.error("vision: %s" % e)
        except Exception:
            pass
        envelop.payload = {
            "ok": False,
            "error": "%s: %s" % (type(e).__name__, str(e)[:300]),
            "error_category": "RUNTIME_INTERNAL",
        }
        return envelop


def help():
    return {
        "route": "builtins/tools/vision",
        "description": "图片识别（多模态描述）与 OCR（本地文字提取），带模型能力/依赖容错",
        "input": {
            "action": "describe | ocr | info | install",
            "path": "图片路径，支持绝对路径 / data/uploads/xxx/a.jpg / 纯文件名（自动在 data/uploads 找最新）",
            "prompt": "describe 用的提问（可选，默认「请详细描述这张图片里有什么。」）",
            "auto_install": "ocr 时缺依赖是否自动 pip 安装（默认 true）",
            "packages": "install 时要装的包（默认 ['rapidocr_onnxruntime']）",
        },
        "output": {
            "ok": "是否成功",
            "data": "describe → {reply, model, file}；ocr → {text, lines, count, engine}；info → {llm, ocr, libs}",
            "error": "失败原因",
            "error_category": "LLM_NOT_CONFIGURED / MODEL_NO_VISION / LLM_CALL_FAILED / DEPENDENCY / RUNTIME_INTERNAL",
        },
        "notes": {
            "模型无视觉": "describe 返回 MODEL_NO_VISION 并建议改用 ocr",
            "OCR 缺依赖": "ocr 自动 pip 安装 rapidocr_onnxruntime；装不上返回 DEPENDENCY 并给安装命令",
            "二进制读取": "所有二进制读取都走 open(path,'rb')，不用 file_utils_api.read_file（会 utf-8 解码失败）",
        },
    }
