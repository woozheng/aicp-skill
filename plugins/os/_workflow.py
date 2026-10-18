"""工作流插件 — 协议 v3.0 系统插件
串行或并行执行多个插件（内部 system.call 版本）
"""
import asyncio
from datetime import datetime
import core


async def execute(envelop, agent):
    action = envelop.payload.get("action", "run")
    
    if action == "run":
        steps = envelop.payload.get("steps", [])
        timeout = envelop.payload.get("timeout", 30)
        
        if not steps:
            envelop.payload = {"ok": False, "error": "No steps defined"}
            return envelop
        
        # 排除控制字段
        current_payload = {}
        for k, v in envelop.payload.items():
            if k not in ("action", "steps", "timeout"):
                current_payload[k] = v
        
        step_history = []
        
        for i, step in enumerate(steps):
            step_start = datetime.now().isoformat()
            receiver = step if isinstance(step, str) else step.get("receiver", "")
            step_payload = step.get("payload", {}) if isinstance(step, dict) else {}
            
            if not receiver:
                step_history.append({
                    "step_index": i,
                    "receiver": None,
                    "success": False,
                    "error": "no receiver",
                })
                envelop.payload = {
                    "ok": False,
                    "error": f"Step {i}: no receiver",
                    "failed_step": i,
                    "step_history": step_history,
                }
                return envelop
            
            try:
                # ★★★ 用 agent.system.call 代替 HTTP ★★★
                result = await asyncio.wait_for(
                    agent.system.call(core.Envelop(
                        sender="builtins/agents/workflow",
                        receiver=receiver,
                        payload={**current_payload, **step_payload},
                        meta={**envelop.meta, "workflow_step": i, "workflow_total": len(steps)},
                    )),
                    timeout=timeout
                )
                
                if result and result.payload:
                    step_history.append({
                        "step_index": i,
                        "receiver": receiver,
                        "success": True,
                        "result": result.payload,
                        "started_at": step_start,
                        "finished_at": datetime.now().isoformat(),
                    })
                    current_payload = result.payload
                else:
                    step_history.append({
                        "step_index": i,
                        "receiver": receiver,
                        "success": False,
                        "error": "no response",
                    })
                    envelop.payload = {
                        "ok": False,
                        "error": f"Step {i} ({receiver}): no response",
                        "failed_step": i,
                        "step_history": step_history,
                    }
                    return envelop
                    
            except asyncio.TimeoutError:
                step_history.append({
                    "step_index": i,
                    "receiver": receiver,
                    "success": False,
                    "error": "timeout",
                })
                envelop.payload = {
                    "ok": False,
                    "error": f"Step {i} ({receiver}) timeout",
                    "failed_step": i,
                    "step_history": step_history,
                }
                return envelop
            except Exception as e:
                step_history.append({
                    "step_index": i,
                    "receiver": receiver,
                    "success": False,
                    "error": str(e),
                })
                envelop.payload = {
                    "ok": False,
                    "error": f"Step {i} ({receiver}): {str(e)}",
                    "failed_step": i,
                    "step_history": step_history,
                }
                return envelop
        
        envelop.payload = {
            "ok": True,
            "data": current_payload,
            "step_history": step_history,
            "total_steps": len(steps),
        }
        return envelop
    
    elif action == "parallel":
        steps = envelop.payload.get("steps", [])
        timeout = envelop.payload.get("timeout", 30)
        
        if not steps:
            envelop.payload = {"ok": False, "error": "No steps defined"}
            return envelop
        
        base_payload = {}
        for k, v in envelop.payload.items():
            if k not in ("action", "steps", "timeout"):
                base_payload[k] = v
        
        async def run_step(step, idx):
            step_start = datetime.now().isoformat()
            receiver = step if isinstance(step, str) else step.get("receiver", "")
            step_payload = step.get("payload", {}) if isinstance(step, dict) else {}
            
            if not receiver:
                return {
                    "step_index": idx,
                    "receiver": None,
                    "success": False,
                    "error": "no receiver",
                }
            
            try:
                # ★★★ 用 agent.system.call 代替 HTTP ★★★
                result = await asyncio.wait_for(
                    agent.system.call(core.Envelop(
                        sender="builtins/agents/workflow",
                        receiver=receiver,
                        payload={**base_payload, **step_payload},
                        meta={**envelop.meta, "workflow_step": idx, "workflow_parallel": True},
                    )),
                    timeout=timeout
                )
                
                if result and result.payload:
                    return {
                        "step_index": idx,
                        "receiver": receiver,
                        "success": True,
                        "result": result.payload,
                        "started_at": step_start,
                        "finished_at": datetime.now().isoformat(),
                    }
                else:
                    return {
                        "step_index": idx,
                        "receiver": receiver,
                        "success": False,
                        "error": "no response",
                    }
                    
            except asyncio.TimeoutError:
                return {
                    "step_index": idx,
                    "receiver": receiver,
                    "success": False,
                    "error": "timeout",
                }
            except Exception as e:
                return {
                    "step_index": idx,
                    "receiver": receiver,
                    "success": False,
                    "error": str(e),
                }
        
        # ★★★ 并行执行 ★★★
        tasks = [run_step(s, i) for i, s in enumerate(steps)]
        results = await asyncio.gather(*tasks)
        
        success_count = sum(1 for r in results if r["success"])
        failed_count = len(results) - success_count
        
        envelop.payload = {
            "ok": failed_count == 0,
            "data": {
                "results": results,
                "success_count": success_count,
                "failed_count": failed_count,
                "total": len(results),
            },
        }
        return envelop
    
    envelop.payload = {"ok": False, "error": f"Unknown action: {action}"}
    return envelop


def help():
    return {
        "route": "builtins/agents/workflow",
        "description": "工作流插件 — 串行或并行执行多个插件（内部 system.call）",
        "input": {
            "action": "run | parallel",
            "steps": "步骤列表",
            "timeout": "超时时间（秒）",
        },
        "output": {
            "ok": "是否成功",
            "data": "结果数据",
            "step_history": "步骤历史",
        }
    }