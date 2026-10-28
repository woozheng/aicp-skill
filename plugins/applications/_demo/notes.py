"""notes 应用后端 — CRUD + LLM + SSE + WS + 上传清单 + 跨插件调用"""
import json
import time
from pathlib import Path

import core

PROJECT = Path(__file__).parent.name
PLUGIN = Path(__file__).stem
SENDER = f"applications/{PROJECT}/{PLUGIN}"
CHANNEL = f"{PROJECT}_{PLUGIN}"

DATA_DIR = Path("data") / PROJECT
DATA_FILE = DATA_DIR / f"{PLUGIN}.json"
FILES_FILE = DATA_DIR / "files.json"


def _load():
    if not DATA_FILE.exists():
        return []
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError, OSError):
        return []
    return data if isinstance(data, list) else []


def _save(items):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_files():
    if not FILES_FILE.exists():
        return []
    try:
        data = json.loads(FILES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError, OSError):
        return []
    return data if isinstance(data, list) else []


def _save_files(items):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILES_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


async def _push(channel, data, agent):
    """推一条到 WS 频道，失败只忽略不抛。"""
    try:
        await agent.system.call(core.Envelop(
            sender=SENDER,
            receiver="os/_websocket",
            intent="API_CALL",
            payload={"action": "push", "channel_id": channel, "data": data},
        ))
    except Exception:
        pass


async def execute(envelop, agent):
    try:
        raw = envelop.payload if isinstance(envelop.payload, dict) else {}
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
        action = str(payload.get("action") or "list")
        items = _load()

        if action == "list":
            envelop.payload = {"ok": True, "data": items}
            return envelop

        if action == "create":
            text = str(payload.get("text") or "").strip()
            if not text:
                envelop.payload = {"ok": False, "error": "text 不能为空"}
                return envelop
            note = {"id": max([i.get("id", 0) for i in items], default=0) + 1, "text": text}
            items.append(note)
            _save(items)
            await _push(CHANNEL, {"type": "created", "note": note}, agent)
            envelop.payload = {"ok": True, "data": note}
            return envelop

        if action == "delete":
            try:
                rid = int(payload.get("id"))
            except (TypeError, ValueError):
                envelop.payload = {"ok": False, "error": "id 必须是整数"}
                return envelop
            kept = [i for i in items if i.get("id") != rid]
            _save(kept)
            await _push(CHANNEL, {"type": "deleted", "id": rid}, agent)
            envelop.payload = {"ok": True, "data": {"total": len(kept)}}
            return envelop

        if action == "llm_chat":
            text = str(payload.get("text") or "").strip()
            if not text:
                envelop.payload = {"ok": False, "error": "text 不能为空"}
                return envelop
            if not getattr(agent, "llm", None):
                envelop.payload = {"ok": False, "error": "LLM 未配置"}
                return envelop
            result = await agent.llm.chat([
                {"role": "system", "content": "你是简洁的助手，只输出结果，不解释。"},
                {"role": "user", "content": text},
            ])
            envelop.payload = {"ok": True, "data": {"reply": str(result or "").strip()}}
            return envelop

        if action == "llm_json":
            text = str(payload.get("text") or "").strip()
            if not text:
                envelop.payload = {"ok": False, "error": "text 不能为空"}
                return envelop
            if not getattr(agent, "llm", None):
                envelop.payload = {"ok": False, "error": "LLM 未配置"}
                return envelop
            data = await agent.llm.chat_json([
                {"role": "user", "content": f"把下面的文本提取成 JSON：{{\"summary\": \"...\"}}\n原文：{text}"},
            ])
            envelop.payload = {"ok": True, "data": data or {}}
            return envelop

        if action == "stream_demo":
            if not getattr(agent, "llm", None):
                envelop.payload = {"ok": False, "error": "LLM 未配置"}
                return envelop
            text = str(payload.get("text") or "").strip() or "用三句话介绍一下 Python"

            async def _gen():
                try:
                    async for chunk in agent.llm.chat_stream([
                        {"role": "user", "content": text},
                    ]):
                        if not chunk:
                            continue
                        yield f'data: {json.dumps({"type": "delta", "text": chunk}, ensure_ascii=False)}\n\n'
                    yield f'data: {json.dumps({"type": "done"}, ensure_ascii=False)}\n\n'
                except Exception as e:
                    yield f'data: {json.dumps({"type": "error", "error": str(e)[:200]}, ensure_ascii=False)}\n\n'

            envelop.payload = _gen()
            return envelop

        if action == "push_demo":
            text = str(payload.get("text") or "").strip()
            if not text:
                envelop.payload = {"ok": False, "error": "text 不能为空"}
                return envelop
            if not getattr(agent, "llm", None):
                envelop.payload = {"ok": False, "error": "LLM 未配置"}
                return envelop
            seq = 0
            try:
                async for chunk in agent.llm.chat_stream([
                    {"role": "user", "content": text},
                ]):
                    if not chunk:
                        continue
                    seq += 1
                    await _push(CHANNEL, {"type": "delta", "text": chunk, "seq": seq}, agent)
                await _push(CHANNEL, {"type": "done"}, agent)
            except Exception as e:
                await _push(CHANNEL, {"type": "error", "error": str(e)[:200]}, agent)
                envelop.payload = {"ok": False, "error": str(e)[:200]}
                return envelop
            envelop.payload = {"ok": True, "data": {"pushed": True, "seq": seq}}
            return envelop

        if action == "upload_list":
            envelop.payload = {"ok": True, "data": _load_files()}
            return envelop

        if action == "upload_add":
            item = payload.get("file") or {}
            if not isinstance(item, dict) or not item.get("file_path"):
                envelop.payload = {"ok": False, "error": "缺少 file_path"}
                return envelop
            files = _load_files()
            entry = {
                "file_path": str(item.get("file_path")),
                "file_name": str(item.get("file_name") or ""),
                "file_size": int(item.get("file_size") or 0),
                "url": str(item.get("url") or ""),
                "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            files.append(entry)
            _save_files(files)
            envelop.payload = {"ok": True, "data": entry}
            return envelop

        if action == "call_ping":
            # 跨插件调用：调同项目另一个插件 applications/{PROJECT}/ping
            ping_action = str(payload.get("ping_action") or "ping")
            ping_payload = {"action": ping_action}
            if ping_action == "echo":
                ping_payload["message"] = str(payload.get("message") or "hello")
            try:
                res = await agent.system.call(core.Envelop(
                    sender=SENDER,
                    receiver=f"applications/{PROJECT}/ping",
                    intent="API_CALL",
                    payload=ping_payload,
                ))
                ping_data = (res.payload or {}) if res else {}
                envelop.payload = {
                    "ok": True,
                    "data": {
                        "target": f"applications/{PROJECT}/ping",
                        "ping_action": ping_action,
                        "reply": ping_data,
                    },
                }
            except Exception as e:
                envelop.payload = {"ok": False, "error": f"调 ping 失败: {str(e)[:200]}"}
            return envelop

        envelop.payload = {"ok": False, "error": f"Unknown action: {action}"}
        return envelop

    except Exception as e:
        try:
            agent.log.error(str(e))
        except Exception:
            pass
        envelop.payload = {"ok": False, "error": str(e)}
        return envelop
