"""引擎插件 —— 引擎的统一入口。

职责边界：
  - 只做桥接：把 agent.engine 的能力暴露给插件系统
  - 不实现任何业务逻辑（14 个 action 在 runtime/engine/engine.py）
  - 不做分发决策（dispatch 由 Engine 内部处理）

挂载顺序（runtime/__main__.py）：
  agent.engine = Engine(agent, agents_dir="data/agents")
  load_all_plugins()
  → 本插件 execute 被调用时，agent.engine 已就绪

前端调用：
  POST /api/builtins/agents/engine
    {"action": "list_agents"}
    {"action": "get_session", "session_id": "...", "agent_id": "main_v2"}
    {"content": "你好", "session_id": "...", "agent_id": "main_v2"}
"""
import core


async def execute(envelop, agent):
    engine = getattr(agent, "engine", None)
    if engine is None:
        envelop.payload = {
            "ok": False,
            "error": "engine not initialized — runtime/__main__.py must set agent.engine before load_all_plugins()",
        }
        return envelop
    return await engine.execute(envelop)


def help():
    return {
        "route": "builtins/agents/engine",
        "description": "Agent 引擎入口：action 分发 + agent 分发",
        "actions": [
            "list_agents", "reload_agents",
            "whoami", "list_conversations", "new_conversation",
            "get_session", "delete_conversation", "rename_conversation",
            "get_call_detail", "get_turn_state", "interrupt",
            "get_state", "save_state",
            "get_agent_prompt", "save_agent_prompt",
            "(no action = 发消息，需 payload.agent_id)",
        ],
    }