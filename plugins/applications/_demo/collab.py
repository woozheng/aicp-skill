"""collab 应用后端 — 演示 agent.system.call 四种跨插件调用"""
import core
from pathlib import Path

PROJECT = Path(__file__).parent.name
PLUGIN = Path(__file__).stem
SENDER = f"applications/{PROJECT}/{PLUGIN}"
CHANNEL = f"{PROJECT}_{PLUGIN}"


def _envelop(receiver, payload, **meta):
    return core.Envelop(
        sender=SENDER,
        receiver=receiver,
        intent="API_CALL",
        payload=payload,
        meta=meta or {},
    )


async def execute(envelop, agent):
    try:
        raw = envelop.payload if isinstance(envelop.payload, dict) else {}
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
        action = str(payload.get("action") or "ping")

        if action == "ping":
            envelop.payload = {"ok": True, "data": {"from": PLUGIN, "msg": "pong"}}
            return envelop

        if action == "call_notes":
            target = f"applications/{PROJECT}/notes"
            res = await agent.system.call(_envelop(target, {"action": "list"}))
            data = (res.payload or {}) if res else {}
            envelop.payload = {"ok": True, "data": {"target": target, "result": data}}
            return envelop

        if action == "chain_create":
            text = str(payload.get("text") or "").strip()
            if not text:
                envelop.payload = {"ok": False, "error": "text 不能为空"}
                return envelop
            res = await agent.system.call(_envelop(
                f"applications/{PROJECT}/notes",
                {"action": "create", "text": text},
            ))
            res_data = (res.payload or {}) if res else {}
            if not res_data.get("ok"):
                envelop.payload = {"ok": False, "error": res_data.get("error", "notes 创建失败")}
                return envelop
            note = res_data.get("data") or {}
            try:
                await agent.system.call(_envelop("os/_websocket", {
                    "action": "push",
                    "channel_id": CHANNEL,
                    "data": {"type": "chain_created", "note": note},
                }))
            except Exception:
                pass
            envelop.payload = {"ok": True, "data": {"note": note, "pushed": True}}
            return envelop

        if action == "get_contract":
            # 真实契约来源：builtins/tools/contract_agent（旧路由 builtins/agents/contract_agent 不存在）
            plugin = str(payload.get("plugin") or "builtins/tools/file_utils_api").strip()
            try:
                res = await agent.system.call(
                    _envelop(
                        "builtins/tools/contract_agent",
                        {"action": "get", "plugin": plugin, "auto_load": False},
                    ),
                    timeout=180,
                )
            except Exception as e:
                envelop.payload = {"ok": False, "error": f"contract_agent 调用失败: {str(e)[:160]}", "plugin": plugin}
                return envelop
            data = (res.payload or {}) if res else {}
            if not data.get("ok"):
                envelop.payload = {"ok": False, "plugin": plugin, "error": data.get("error") or "契约获取失败"}
                return envelop
            contract = data.get("contract") or {}
            envelop.payload = {"ok": True, "data": {
                "plugin": plugin,
                "ok": True,
                "source": data.get("source"),
                "usage_file": data.get("usage_file"),
                "usage_loaded": data.get("usage_loaded"),
                "actions": list((contract.get("actions") or {}).keys()),
                "contract": contract,
            }}
            return envelop

        if action == "broadcast":
            text = str(payload.get("text") or "").strip()
            if not text:
                envelop.payload = {"ok": False, "error": "text 不能为空"}
                return envelop
            try:
                res = await agent.system.call(_envelop("os/_websocket", {
                    "action": "broadcast",
                    "data": {"type": "broadcast", "text": text, "from": PLUGIN},
                }))
                data = (res.payload or {}) if res else {}
                envelop.payload = {"ok": True, "data": {"sent": data.get("sent", 0)}}
            except Exception as e:
                envelop.payload = {"ok": False, "error": str(e)[:200]}
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
