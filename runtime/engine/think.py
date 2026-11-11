"""递归决策 —— LLM 流消费 + 工具分派 + 旁白/空输出重试。

对外暴露：
  - think(ctx)                       ← 单轮决策
  - process(ctx, depth)              ← 递归主循环
  - push_stats(ctx, depth)           ← stats 推送
"""
import asyncio
import hashlib
import json
import os
import random
from datetime import datetime
from pathlib import Path

from runtime._llm import is_llm_error_string
from .narration import is_narration_reply
from .paths import session_debug_dir
from .schemas import SCHEMAS
from .tools import dispatch_tool, handle_skill, tool_manage_flow, fill_base_url


_DEBUG_CTX_ENABLED = True      # 关掉改 False
_DEBUG_CTX_DIR_NAME = "_debug_context"


def _debug_dump_messages(ctx, messages, depth):
    """把当前轮 messages dump 成 _debug_context/depth_XX.json。"""
    if not _DEBUG_CTX_ENABLED:
        return
    try:
        base = session_debug_dir(ctx.user_id, ctx.agent_id, ctx.session_id)
        # depth=0 时清空旧文件（只保留本轮递归）
        if depth == 0:
            for p in base.iterdir():
                if p.is_file():
                    try:
                        p.unlink()
                    except Exception:
                        pass
        fname = f"depth_{depth:02d}.json"
        path = base / fname
        header = {
            "session_id": ctx.session_id,
            "user_id": ctx.user_id,
            "agent_id": ctx.agent_id,
            "depth": depth,
            "timestamp": datetime.now().isoformat(),
            "flow_entries": len(ctx.flow._flow),
            "rounds": ctx.flow._round_counter,
            "current_turn": ctx.flow._current_turn,
            "message_count": len(messages),
            "msg_hashes": [
                {
                    "idx": i, "role": m["role"],
                    "hash": hashlib.md5((m.get("content") or "").encode("utf-8")).hexdigest()[:12],
                    "len": len(m.get("content") or ""),
                }
                for i, m in enumerate(messages)
            ],
        }
        tmp = base / f".{fname}.tmp"
        tmp.write_text(
            "# Debug Messages\n\n" + json.dumps(header, ensure_ascii=False, indent=2)
            + "\n\n---\n\n" + json.dumps(messages, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except Exception as e:
        print(f"[think][WARN] debug dump 失败: {e}")


class EmptyOutputError(RuntimeError):
    pass


class NarrationReplyError(RuntimeError):
    pass


class ContentSafetyBlockedError(RuntimeError):
    pass


async def think(ctx):
    llm = ctx.agent.llm
    if not llm:
        return {"call": "reply", "content": "抱歉，AI 服务暂时不可用", "args": {}}

    ctx.flow._current_turn += 1
    ctx.flow._gc_ignored()

    messages = [{"role": "system", "content": ctx.system_prompt}]
    messages.extend(ctx.flow.get_messages())
    tail = ctx.tail_builder(ctx) if ctx.tail_builder else ""
    if tail:
        messages.append({"role": "user", "content": tail})

    # 调试：把本轮 messages dump 到 _debug_context/depth_XX.json
    _debug_dump_messages(ctx, messages, getattr(ctx, "depth", 0))

    _ctx_chars = sum(len(m.get("content") or "") for m in messages)
    _pressure = _ctx_chars >= ctx.pressure_threshold

    if ctx.depth >= ctx.max_depth - 1:
        tools = [SCHEMAS["reply"]()]
        tool_choice = "auto"
    elif _pressure:
        tools = [SCHEMAS["manage_flow"]()]
        tool_choice = "auto"
    else:
        tools = ctx.tool_schemas
        tool_choice = "auto"

    tool_calls_list = []
    text_buf = ""
    error_msg = ""
    manage_flow_done = False

    async for event in llm.chat_tool_stream(
        messages, tools=tools, tool_choice=tool_choice,
        session_id=ctx.session_id,
    ):
        etype = event.get("type")
        if etype == "thinking":
            ctx.push.stream(ctx.user_id, ctx.session_id, event.get("content", ""))
        elif etype == "text":
            chunk = event.get("content", "")
            text_buf += chunk
            ctx.push.stream(ctx.user_id, ctx.session_id, chunk)
        elif etype == "tool_call_delta":
            _name = event.get("name", "") or ""
            _alen = event.get("args_len", 0) or 0
            _buf = f"\n⚙️ {_name} ({_alen}B)" if _name else f"\n⚙️ ({_alen}B)"
            ctx.push.stream(ctx.user_id, ctx.session_id, _buf)
        elif etype == "retry":
            ctx.push.progress(ctx.user_id, ctx.session_id, "executing",
                              f"⚠️ LLM 超时，重试 {event.get('attempt')}/{event.get('max_attempts')}...",
                              extra={"status": "retrying"})
        elif etype == "tool_call":
            tc = event.get("tool_call")
            if tc.name == "manage_flow":
                await tool_manage_flow(ctx, tc.arguments or {})
                manage_flow_done = True
                continue
            tool_calls_list.append(tc)
            try:
                tc_args = tc.arguments or {}
                think_val = tc_args.get("think", "") if isinstance(tc_args, dict) else ""
                args_display = {k: v for k, v in tc_args.items() if k != "think"} if isinstance(tc_args, dict) else tc_args
                raw = json.dumps({"think": (think_val or "")[:200], "tool": tc.name,
                                  "args": _brief_args(args_display)},
                                 ensure_ascii=False, default=str)
                ctx.push.stream(ctx.user_id, ctx.session_id, f"\n🧩 {raw}\n")
            except Exception:
                pass
        elif etype == "error":
            error_msg = event.get("error", "")
            ctx.push.progress(ctx.user_id, ctx.session_id, "error",
                              f"⚠️ LLM: {error_msg[:120]}")
        elif etype == "done":
            break

    if manage_flow_done and not tool_calls_list and not text_buf.strip():
        return {"call": "manage_flow", "content": "", "args": {}}

    if error_msg:
        low = error_msg.lower()
        if ("network" in low or "connection" in low or "timeout" in low
                or "超时" in error_msg or "服务请求超时" in error_msg
                or "tool_input_invalid" in low or "incomplete" in low
                or "sse stream" in low or "api_error" in low or "被截断" in low):
            raise ConnectionError(error_msg)
        if is_llm_error_string(error_msg):
            raise ContentSafetyBlockedError(error_msg)
        return {"call": "reply", "content": f"LLM 调用失败: {error_msg[:200]}", "args": {}}

    if tool_calls_list:
        results = []
        for tc in tool_calls_list:
            name = tc.name
            args = tc.arguments or {}
            think_val = args.pop("think", "") if isinstance(args, dict) else ""
            if name == "reply":
                content = fill_base_url(args.get("content", ""), ctx.agent)
                if text_buf.strip():
                    content = (text_buf.strip() + "\n" + content).strip() if content else text_buf.strip()
                if ctx.narration_check and is_narration_reply(content):
                    raise NarrationReplyError(content[:200])
                await ctx.flow.append_ai(think_val, "reply", content=content,
                                         retain=args.get("retain", 3))
                try:
                    await ctx.flow.snapshot_round(ctx.flow._round_counter)
                except Exception:
                    pass
                return {"call": "reply", "content": content, "args": {}}
            if name == "use_tool":
                retain = args.get("retain", 3)
                combined_think = think_val
                if text_buf.strip():
                    combined_think = (think_val + "\n" + text_buf.strip()).strip() if think_val else text_buf.strip()
                    text_buf = ""
                await ctx.flow.append_ai(combined_think, "use_tool", args=args, retain=retain)
                results.append({"call": "use_tool", "args": args,
                                "think": combined_think, "retain": retain,
                                "chain_no": ctx.flow._chain_counter})
        if results:
            first = results[0]
            first["_pending_tool_calls"] = results[1:]
            return first

    if text_buf.strip():
        content = fill_base_url(text_buf.strip(), ctx.agent)
        if ctx.narration_check and is_narration_reply(content):
            raise NarrationReplyError(content[:200])
        if _pressure:
            raise EmptyOutputError(
                "压力模式下你只能调 manage_flow，不要输出文本。"
                "请调 manage_flow(ignore=[...]) 折叠上下文。"
            )
        await ctx.flow.append_ai("", "reply", content=content, retain=3)
        return {"call": "reply", "content": content, "args": {}}

    if not tool_calls_list and not text_buf.strip() and not error_msg:
        raise EmptyOutputError("模型只输出了 thinking，没有 tool_call / text")
    return {"call": "reply", "content": "⚠️ 模型原始输出未处理", "args": {}}


def _brief_args(args, max_str=80):
    if not isinstance(args, dict):
        return args
    out = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > max_str:
            out[k] = v[:max_str] + f"…（{len(v)} 字）"
        elif isinstance(v, (list, dict)):
            try:
                s = json.dumps(v, ensure_ascii=False, default=str)
            except Exception:
                s = str(v)
            if len(s) > max_str * 2:
                out[k] = s[:max_str] + f"…（{len(s)} 字符）"
            else:
                out[k] = v
        else:
            out[k] = v
    return out


async def process(ctx, depth=0):
    """递归主循环。"""
    k = f"{ctx.agent_id}:{ctx.session_id}"

    if ctx.interrupt_signals.get(k, False):
        ctx.interrupt_signals.pop(k, None)
        try:
            await ctx.flow.mark_interrupted("user_interrupt", "")
        except Exception:
            pass
        ctx.push.progress(ctx.user_id, ctx.session_id, "done", "⏸️ 已中断（现场保留）",
                          extra={"status": "interrupted"})
        return {"ok": True, "interrupted": True, "call": "reply", "content": "", "args": {}}

    if depth > ctx.max_depth:
        try:
            await ctx.flow.mark_interrupted("max_depth", f"depth={depth}")
        except Exception:
            pass
        return {"ok": False, "call": "reply",
                "content": "处理步骤过多，请简化需求后重试", "args": {}}

    ctx.depth = depth

    k_empty = k + ":empty"
    k_narr = k + ":narr"
    retry_count = ctx.empty_retries.get(k_empty, 0)
    narr_count = ctx.narration_retries.get(k_narr, 0)

    output = None
    for _retry in range(2):
        try:
            output = await think(ctx)
            ctx.empty_retries[k_empty] = 0
            ctx.narration_retries[k_narr] = 0
            break
        except NarrationReplyError as e:
            if _retry == 0 and narr_count < 3:
                ctx.narration_retries[k_narr] = narr_count + 1
                try:
                    await ctx.flow.append_system(
                        action="task_summary",
                        result_think=(
                            "⚠️ 你上一条回复只是「我要去做什么」的宣告，不是交付结果。"
                            "不要用 reply 说下一步动作 —— 请直接调用 use_tool / manage_flow 去真正执行。"
                        ),
                        detail={"reason": "narration_reply", "narration": str(e)[:200]},
                        retain=2,
                    )
                except Exception:
                    pass
                continue
            ctx.narration_retries[k_narr] = 0
            break
        except EmptyOutputError as e:
            if _retry == 0 and retry_count < 2:
                ctx.empty_retries[k_empty] = retry_count + 1
                try:
                    await ctx.flow.append_system(
                        action="task_summary",
                        result_think=(
                            "⚠️ 上一次决策只输出了 thinking，或把「下一步动作」当 reply 说了。"
                            "请直接输出一个 tool_call（reply / use_tool / manage_flow）。"
                        ),
                        detail={"reason": "empty_output"},
                        retain=2,
                    )
                except Exception:
                    pass
                continue
            ctx.empty_retries[k_empty] = 0
            content = ("⚠️ **模型连续只输出思考过程，没有产出可执行内容**。\n\n"
                       "**建议**：直接说「继续」让它重新决策。")
            ctx.push.chat(ctx.user_id, ctx.session_id, content)
            return {"ok": True, "waiting": False, "call": "reply",
                    "content": content, "args": {}}

    if output is None:
        return {"ok": False, "call": "reply", "content": "模型输出异常", "args": {}}

    if output.get("call") == "manage_flow":
        push_stats(ctx, depth)
        return await process(ctx, depth + 1)

    if output.get("call") == "reply":
        content = output.get("content") or "好的，已处理"
        ctx.push.progress(ctx.user_id, ctx.session_id, "done", "✅ 已回复用户", extra={
            "status": "reply", "is_final": True, "tool_count": depth + 1,
        })
        if content:
            ctx.push.chat(ctx.user_id, ctx.session_id, content)
        push_stats(ctx, depth)
        return {"ok": True, "waiting": False, "call": "reply",
                "content": content, "args": {}}

    if output.get("call") == "use_tool":
        all_calls = [output] + list(output.get("_pending_tool_calls") or [])
        has_processing = False
        for call in all_calls:
            args = call.get("args", {}) or {}
            ctx.think = call.get("think", "")
            ctx.chain_no = call.get("chain_no")
            target = args.get("target", "")
            if target.startswith("skill/"):
                result = await handle_skill(ctx, target, chain_no=ctx.chain_no)
            else:
                result = await dispatch_tool(ctx, args)
            if not result.get("ok", True):
                await ctx.flow.append_system(
                    action="task_summary",
                    result_think=f"⚠️ 上一步工具失败: {result.get('think', '')[:150]}",
                    detail={"failed": True}, retain=2,
                )
            if result.get("data", {}).get("status") == "processing":
                has_processing = True
                ctx.push.chat(ctx.user_id, ctx.session_id,
                              f"⏳ 任务已提交，正在后台处理...（{target}）")
        await asyncio.sleep(random.uniform(0.5, 2.0))
        if has_processing:
            return await process(ctx, depth + 1)
        push_stats(ctx, depth + 1)
        return await process(ctx, depth + 1)

    return {"ok": False, "call": "reply",
            "content": f"未知调用: {output.get('call')}", "args": {}}


def push_stats(ctx, depth):
    try:
        llm = ctx.agent.llm
        # ★ 直接读 last_stats 快照（不深拷贝、不查 _session_stats）
        s = getattr(llm, "last_stats", {}).get(ctx.session_id) or {}
        if not s:
            # 首次调用时 last_stats 还没写入，用 get_session_stats 兜底一次
            s = llm.get_session_stats(ctx.session_id) if hasattr(llm, "get_session_stats") else {}
            if not s:
                return
        last_prompt = int(s.get("last_prompt_tokens", 0) or 0)
        last_cache_read = int(s.get("last_cache_read_tokens", 0) or 0)
        cache_hit_pct = round(last_cache_read / last_prompt * 100, 1) if last_prompt > 0 else 0.0
        stats = {
            "context_k": round(last_prompt / 1000, 1),
            "round_total": s.get("round_total_tokens", 0),
            "depth": depth,
            "max_depth": ctx.max_depth,
            "cache_hit_pct": cache_hit_pct,
            "last_prompt": last_prompt,
            "cache_read": last_cache_read,
        }
        ctx.push.stats(ctx.user_id, ctx.session_id, stats)
    except Exception as e:
        print(f"[think][WARN] push_stats 失败: {e}")
