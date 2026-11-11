"""Baseline 策略 —— 决定本轮渲染哪些历史轮。

第一版只实现 recent_n（降级）；llm_task_chain 第二版补。
返回 (baseline, task_summary, picked_skills)。
"""
from .constants import BASE_ROUNDS


def baseline_recent_n(flow, user_input, agent):
    """降级：None 表示走 get_messages 里默认的最近 BASE_ROUNDS 轮。"""
    return None, "", []


async def baseline_llm_task_chain(flow, user_input, agent):
    """第二版实现：调 LLM 从历史摘要里选相关轮。

    第一版先降级到 recent_n，行为可用。
    """
    return await _noop(flow, user_input, agent)


async def _noop(flow, user_input, agent):
    return None, "", []
