"""AICP Studio 入口 — 单窗口架构"""
import core
import wx

STUDIO_STATE = {
    "studio_frame": None,
}


async def execute(envelop, agent):
    payload = envelop.payload.get("payload", envelop.payload)
    action = payload.get("action", "init")

    if action == "init":
        await agent.system.call(core.Envelop(
            sender="builtins/studio/_init",
            receiver="shell/_tray",
            payload={
                "action": "add_menu",
                "app_id": "studio",
                "label": "🛠 AICP Studio",
                "items": [
                    {"label": "打开 Studio", "action": "open", "receiver": "builtins/studio/_init"},
                    {"label": "关闭 Studio", "action": "close", "receiver": "builtins/studio/_init"},
                ],
            },
        ))
        STUDIO_STATE["agent"] = agent
        envelop.payload = {"ok": True, "message": "Studio mounted"}
        return envelop

    elif action == "open":
        from ._studio_frame import StudioFrame
        base_url = agent.base_url or "http://127.0.0.1:9000"
        wx.CallAfter(lambda: StudioFrame(base_url=base_url))
        envelop.payload = {"ok": True}
        return envelop

    elif action == "close":
        from ._studio_frame import StudioFrame
        if StudioFrame._instance:
            wx.CallAfter(StudioFrame._instance.Close)
            StudioFrame._instance = None
        envelop.payload = {"ok": True, "message": "Studio closed"}
        return envelop

    envelop.payload = {"error": f"Unknown: {action}"}
    return envelop