"""ping 应用后端 — 极简示例，被其他插件调用"""
import time
from pathlib import Path

PROJECT = Path(__file__).parent.name
PLUGIN = Path(__file__).stem


async def execute(envelop, agent):
    try:
        raw = envelop.payload if isinstance(envelop.payload, dict) else {}
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
        action = str(payload.get("action") or "ping")

        if action == "ping":
            envelop.payload = {
                "ok": True,
                "data": {"from": PLUGIN, "project": PROJECT, "msg": "pong"},
            }
            return envelop

        if action == "echo":
            msg = str(payload.get("message") or "").strip()
            if not msg:
                envelop.payload = {"ok": False, "error": "message 不能为空"}
                return envelop
            envelop.payload = {
                "ok": True,
                "data": {"from": PLUGIN, "echo": msg, "len": len(msg)},
            }
            return envelop

        if action == "info":
            envelop.payload = {
                "ok": True,
                "data": {
                    "from": PLUGIN,
                    "project": PROJECT,
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "actions": ["ping", "echo", "info"],
                },
            }
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
