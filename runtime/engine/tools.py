"""工具分发 —— use_tool / skill/ 路由 / manage_flow 执行。

对外暴露：
  - dispatch_tool(ctx, args)     ← use_tool 分发（含 skill/ 路由、自调用拦截）
  - handle_skill(ctx, target)    ← skill/ 路由
  - tool_manage_flow(ctx, args)  ← manage_flow 工具
"""
import json
import time
import uuid
from pathlib import Path

import core
from .backpack import check_failure_lock, record_failure
from .paths import skills_dir


def _brief_params(params, max_str=100):
    out = {}
    for k, v in (params or {}).items():
        if isinstance(v, str):
            out[k] = v if len(v) <= max_str else (v[:max_str] + f"…（{len(v)} 字）")
        elif isinstance(v, (list, dict)):
            try:
                s = json.dumps(v, ensure_ascii=False, default=str)
            except Exception:
                s = str(v)
            out[k] = v if len(s) <= max_str else (s[:max_str] + f"…（{len(s)} 字符）")
        else:
            out[k] = v
    return out


def _fill_base_url(content, agent):
    if not content:
        return content
    base = getattr(agent, "base_url", "") or ""
    if not base:
        return content
    return content.replace("{base_url}", base).replace("{agent.base_url}", base)


async def tool_manage_flow(ctx, args):
    ctx_before = ctx.flow.estimate_context_chars()
    mf_chain_no = ctx.flow._next_chain()
    call_id = str(uuid.uuid4())[:8]
    t0 = time.time()

    ctx.push.progress(ctx.user_id, ctx.session_id, "executing", "⚡ manage_flow", extra={
        "call_id": call_id,
        "target": "builtins/agents/engine",
        "action": "manage_flow",
        "params": {"ignore": args.get("ignore")},
        "status": "start",
        "think": args.get("reason", ""),
    })

    await ctx.flow.append_system(
        action="call_start",
        result_think="📤 调用工具: manage_flow",
        detail={
            "call_id": call_id,
            "target": "builtins/agents/engine",
            "action": "manage_flow",
            "params": {"ignore": args.get("ignore")},
            "status": "pending",
        },
        retain=2, chain_no=mf_chain_no,
    )

    ok = True
    error_msg = ""
    result = {}
    try:
        result = await ctx.flow.manage_flow(ignore=args.get("ignore"))
    except Exception as e:
        ok = False
        error_msg = str(e)

    ctx_after = ctx.flow.estimate_context_chars()
    freed = max(0, ctx_before - ctx_after)
    elapsed_ms = int((time.time() - t0) * 1000)
    _data = result.get("data", {}) if ok else {}
    _still_over = bool(_data.get("still_over", False))
    _need_free = int(_data.get("need_free", 0) or 0)
    _think_txt = (result.get("think", "") if ok else error_msg)

    await ctx.flow.append_system(
        action="call_end" if ok else "call_error",
        result_think=("📨 调用完成: manage_flow" if ok else "❌ manage_flow 失败")
                       + (f"（还需释放 {_need_free:,} 字）" if _still_over else ""),
        detail={
            "call_id": call_id,
            "target": "builtins/agents/engine",
            "action": "manage_flow",
            "result": {
                "ok": ok, "think": _think_txt,
                "data": {
                    "applied": _data,
                    "ctx_before": ctx_before, "ctx_after": ctx_after,
                    "freed": freed,
                    "still_over": _still_over, "need_free": _need_free,
                },
                "error": error_msg,
            },
            "duration_ms": elapsed_ms,
        },
        retain=2, chain_no=mf_chain_no,
    )
    ctx.push.progress(ctx.user_id, ctx.session_id, "done" if ok else "error",
                      "✅ manage_flow" if ok else "❌ manage_flow", extra={
                          "call_id": call_id,
                          "target": "builtins/agents/engine",
                          "status": "ok" if ok else "error",
                          "duration_ms": elapsed_ms,
                          "error": error_msg if not ok else None,
                      })
    return {"control": "continue", "result": result}


async def dispatch_tool(ctx, args):
    target = args.get("target", "")
    if target.startswith("skill/"):
        return await handle_skill(ctx, target, chain_no=ctx.chain_no)
    action = args.get("action", "")
    if not target:
        call_id = str(uuid.uuid4())[:8]
        await ctx.flow.append_system(
            action="call_error", result_think="❌ 缺少 target",
            detail={"call_id": call_id, "target": "", "error": "缺少 target"},
            retain=3, chain_no=ctx.chain_no,
        )
        ctx.push.progress(ctx.user_id, ctx.session_id, "error", "⚠️ 缺少 target",
                          extra={"call_id": call_id, "target": "", "status": "error",
                                 "error": "缺少 target"})
        return {"ok": False, "think": "缺少 target"}

    _lock = check_failure_lock(ctx.agent_id, ctx.user_id, target, action,
                               args.get("plugin_args") or {})
    if _lock:
        call_id = str(uuid.uuid4())[:8]
        await ctx.flow.append_system(
            action="call_error", result_think=f"⛔ {target} 被经验背包拦截",
            detail={"call_id": call_id, "target": target, "error": _lock},
            retain=2, chain_no=ctx.chain_no,
        )
        ctx.push.progress(ctx.user_id, ctx.session_id, "error",
                          f"⛔ {target} 被拦截（同错 ≥3 次）",
                          extra={"call_id": call_id, "target": target,
                                 "status": "error", "error": _lock})
        return {"ok": False, "think": _lock}

    if target == "os/file_utils_api":
        target = "builtins/tools/file_utils_api"

    if isinstance(args.get("plugin_args"), dict):
        tool_params = dict(args["plugin_args"])
    else:
        tool_params = {k: v for k, v in args.items()
                       if k not in ("target", "action", "retain", "params", "think")}

    retain = args.get("retain", 3)
    call_id = str(uuid.uuid4())[:8]
    t0 = time.time()

    ctx.push.progress(ctx.user_id, ctx.session_id, "executing", f"⚡ {target}", extra={
        "call_id": call_id, "target": target, "action": action,
        "params": _brief_params(tool_params), "status": "start",
        "think": ctx.think or "",
    })

    if target == "builtins/agents/engine":
        target_agent_id = tool_params.get("agent_id", "")
        target_session = tool_params.get("session_id", "")
        if target_agent_id == ctx.agent_id and target_session == ctx.session_id:
            msg = "禁止同 agent 同 session 自调用。如需分身，session_id 用 sub_ 开头，或指定不同 agent_id。"
            await ctx.flow.append_system(
                action="call_error", result_think=f"❌ {msg}",
                detail={"call_id": call_id, "target": target, "error": msg},
                retain=3, chain_no=ctx.chain_no,
            )
            ctx.push.progress(ctx.user_id, ctx.session_id, "error", f"⚠️ {msg[:120]}",
                              extra={"call_id": call_id, "target": target,
                                     "status": "error"})
            return {"ok": False, "think": msg}

    await ctx.flow.append_system(
        action="call_start", result_think=f"📤 调用工具: {target}",
        detail={
            "call_id": call_id, "target": target, "action": action,
            "params": tool_params, "status": "pending",
        },
        retain=retain, chain_no=ctx.chain_no,
    )

    meta = {
        "session_id": ctx.session_id,
        "user_id": ctx.user_id,
        "agent_id": ctx.agent_id,
        "_call_id": call_id,
    }
    # 出向边的「被调方」身份：显式 agent_id 优先，其次按路由推导。
    # 让可视化在「out」方向也能识别接收端 agent，而不只依赖回调时的 callback_agent_id。
    _target_agent = str(tool_params.get("agent_id") or "").strip()
    if not _target_agent:
        if target.startswith("builtins/agents/") or target.startswith("applications/"):
            _target_agent = target.split("/")[-1]
    if _target_agent:
        meta["receiver_agent_id"] = _target_agent
    call_params = {**tool_params}
    for k in ("callback_receiver", "callback_session_id", "token"):
        if k in call_params:
            meta[k] = call_params.pop(k)
    payload = {"action": action, **call_params} if action else dict(call_params)

    if target == "builtins/tools/code_runner":
        _ts = tool_params.get("timeout_s")
        try:
            _ts = int(_ts) if _ts is not None else 60
        except (TypeError, ValueError):
            _ts = 60
        route_timeout = min(max(_ts + 20, 60), 320)
    elif target == "builtins/tools/save_blocks":
        route_timeout = 120
    elif target == "builtins/tools/contract_agent":
        route_timeout = 180
    elif target == "builtins/agents/engine":
        route_timeout = 180
    elif target == "builtins/agents/main_agent":
        route_timeout = 180
    else:
        route_timeout = 30

    try:
        result = await ctx.agent.system.call(core.Envelop(
            sender=ctx.route, receiver=target, payload=payload, meta=meta,
        ), timeout=route_timeout)
        data = result.result() if result is not None else {"ok": False, "error": "no response"}
        if data.get("status") == "processing":
            trace_id = (result.meta or {}).get("trace_id", "") if result else ""
            await ctx.flow.append_system(
                action="async_processing",
                result_think=f"⏳ 异步任务已提交 [trace={trace_id[:8]}]",
                detail={"trace_id": trace_id, "status": "processing"},
                retain=3, chain_no=ctx.chain_no,
            )
            ctx.push.progress(ctx.user_id, ctx.session_id, "executing",
                              f"⚡ 异步任务已提交（trace={trace_id[:8]}）")
            return {"ok": True, "think": f"任务已提交（trace={trace_id[:8]}）",
                    "data": {"status": "processing", "trace_id": trace_id}}

        elapsed_ms = int((time.time() - t0) * 1000)
        ok = data.get("ok", True)
        if ok:
            think = f"工具「{target}」业务成功"
            ctx.push.progress(ctx.user_id, ctx.session_id, "done", f"✅ {target}", extra={
                "call_id": call_id, "target": target, "status": "ok",
                "duration_ms": elapsed_ms,
            })
        else:
            think = f"工具「{target}」业务失败: {str(data.get('error', ''))[:150]}"
            ctx.push.progress(ctx.user_id, ctx.session_id, "error", f"⚠️ {target}", extra={
                "call_id": call_id, "target": target, "status": "error",
                "duration_ms": elapsed_ms, "error": str(data.get("error", ""))[:200],
            })
            record_failure(ctx.agent_id, ctx.user_id, target, action, tool_params,
                           "business", str(data.get("error", "")), data,
                           str(data.get("error_category", "") or ""))

        await ctx.flow.append_system(
            action="call_end", result_think=f"📨 调用完成: {target}",
            detail={
                "call_id": call_id, "target": target, "action": action,
                "result": data, "duration_ms": elapsed_ms,
            },
            retain=retain, chain_no=ctx.chain_no,
        )
        data_field = data.get("data", data)
        if not isinstance(data_field, dict):
            data_field = {"data": data_field}
        return {"ok": ok, "think": think, "data": data_field,
                "_full_result": json.dumps(data, ensure_ascii=False, default=str)}

    except Exception as e:
        print(f"[tools][ERROR] 工具执行异常: target={target} action={action}\n{e}")
        await ctx.flow.append_system(
            action="call_error", result_think=f"❌ 调用失败: {target}",
            detail={"call_id": call_id, "error": str(e)},
            retain=retain, chain_no=ctx.chain_no,
        )
        elapsed_ms = int((time.time() - t0) * 1000)
        ctx.push.progress(ctx.user_id, ctx.session_id, "error", f"❌ {target}", extra={
            "call_id": call_id, "target": target, "status": "error",
            "duration_ms": elapsed_ms, "error": str(e)[:200],
        })
        record_failure(ctx.agent_id, ctx.user_id, target, action, tool_params,
                       "exception", str(e), None)
        return {"ok": False, "think": f"工具「{target}」执行失败: {str(e)}"}


async def handle_skill(ctx, target, chain_no=None):
    call_id = str(uuid.uuid4())[:8]
    rel = target[len("skill/"):].strip("/")
    skill_file = None
    if rel:
        sd = skills_dir()
        cand = sd / rel / "SKILL.md"
        if cand.exists():
            skill_file = cand
        else:
            alt = sd / "aicp" / rel / "SKILL.md"
            if alt.exists():
                skill_file = alt
        if skill_file is None:
            cand2 = sd / rel
            if cand2.is_file() and cand2.name == "SKILL.md":
                skill_file = cand2
    if skill_file is None:
        err = f"skill 不存在: {target}"
        await ctx.flow.append_system(
            action="call_error", result_think=f"❌ {err}",
            detail={"call_id": call_id, "target": target, "error": err},
            chain_no=chain_no,
        )
        return {"ok": False, "think": err}
    try:
        text = skill_file.read_text(encoding="utf-8")
    except Exception as e:
        err = f"读取 skill 失败: {e}"
        await ctx.flow.append_system(
            action="call_error", result_think=f"❌ {err}",
            detail={"call_id": call_id, "target": target, "error": err},
            chain_no=chain_no,
        )
        return {"ok": False, "think": err}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            body = text[end + 4:].strip()
    file_size = skill_file.stat().st_size if skill_file.exists() else len(text.encode())
    body_size = len(body)
    await ctx.flow.append_system(
        action="call_start", result_think=f"📤 读取 skill: {target}",
        detail={"call_id": call_id, "target": target, "action": "read",
                "params": {}, "status": "pending"},
        chain_no=chain_no,
    )
    await ctx.flow.append_system(
        action="call_end", result_think=f"📨 skill 已加载: {target}",
        detail={"call_id": call_id, "target": target, "action": "read",
                "result": {"ok": True, "data": {
                    "path": str(skill_file), "size": file_size,
                    "body_size": body_size, "content": body,
                }}},
        chain_no=chain_no,
    )
    return {"ok": True, "think": f"skill 已加载: {target}（{body_size} 字）",
            "data": {"path": str(skill_file), "size": file_size, "body_size": body_size}}


def fill_base_url(content, agent):
    return _fill_base_url(content, agent)
