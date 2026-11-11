"""WS 推送 —— 统一入口，按 (agent_id, session_id) 加标记。

底层走 agent.system.call(os/_websocket)，和旧 main_agent 一致。
"""
import asyncio
from typing import Dict

import core


class Push:
    _BG_TASKS = set()
    _BG_MAX = 256
    _DEAD: Dict[str, bool] = {}
    _FAIL: Dict[str, int] = {}

    def __init__(self, agent, route, agent_id):
        self.agent = agent
        self.route = route
        self.agent_id = agent_id

    def _chan(self, user_id, session_id):
        return f"pa_{user_id}"

    async def _bg(self, user_id, session_id, data):
        data = {**data, "agent_id": self.agent_id}
        try:
            result = await asyncio.wait_for(
                self.agent.system.call(core.Envelop(
                    sender=self.route,
                    receiver="os/_websocket",
                    meta={
                        "session_id": session_id,
                        "user_id": user_id,
                        "agent_id": self.agent_id,
                    },
                    payload={
                        "action": "push",
                        "channel_id": self._chan(user_id, session_id),
                        "data": data,
                    },
                ), timeout=5.0),
                timeout=1.0,
            )
            payload = getattr(result, "payload", None) or {}
            sent = int(payload.get("sent") or 0)
            if sent == 0:
                cnt = self._FAIL.get(session_id, 0) + 1
                self._FAIL[session_id] = cnt
                if cnt >= 3:
                    self._DEAD[session_id] = True
                    print(f"[push][WARN] 连续 {cnt} 次 sent=0，置 DEAD session={session_id}")
            else:
                self._FAIL.pop(session_id, None)
            return sent
        except asyncio.TimeoutError:
            print(f"[push][WARN] 推送超时 type={data.get('type')}")
            return 0
        except Exception as e:
            print(f"[push][WARN] 推送失败: {type(e).__name__}: {e}")
            return 0

    def nowait(self, user_id, session_id, data, critical=False):
        if not critical and self._DEAD.get(session_id):
            return
        if len(self._BG_TASKS) >= self._BG_MAX:
            return
        try:
            task = asyncio.create_task(self._bg(user_id, session_id, data))
        except Exception:
            return
        self._BG_TASKS.add(task)
        task.add_done_callback(self._BG_TASKS.discard)

    def reset_dead(self, session_id):
        self._DEAD[session_id] = False

    def progress(self, user_id, session_id, step, msg, extra=None, critical=True):
        data = {"type": "progress", "session_id": session_id, "step": step, "msg": msg}
        if extra:
            data.update(extra)
        self.nowait(user_id, session_id, data, critical=critical)

    def stream(self, user_id, session_id, chunk):
        self.nowait(user_id, session_id, {
            "type": "summary_stream", "session_id": session_id, "chunk": chunk,
        })

    def chat(self, user_id, session_id, content):
        self.nowait(user_id, session_id, {
            "type": "chat", "session_id": session_id, "content": content,
        }, critical=True)

    def stats(self, user_id, session_id, stats):
        self.nowait(user_id, session_id, {
            "type": "stats", "session_id": session_id, "stats": stats,
        }, critical=True)
