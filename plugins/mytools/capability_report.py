"""capability_report — 把"搜工具 → 查契约"封成一次调用

组合 cogitor.search + contract_agent.get：
输入一个自然语言任务，输出可直接执行的路由计划（route / action / 必填参数）。

v1: report 动作（搜工具 + 并行抽契约）
v2: 新增 quick 动作（只搜不抽契约，快路径）；新增 call_template 字段
v3: 新增 plan 动作 —— 用 LLM 把复合任务拆成多个能力查询，分别召回后合并去重，
    按拆解顺序推导调用步骤。修掉 v1/v2 对复合任务召回不足的缺陷。
v4: plan 只用一次 LLM 调用（原 v3 两次，触发 30s 路由超时）；
    LLM/契约提取均加超时兜底，总预算压在路由 30s 之内。
"""
import asyncio
import json

import core

SENDER = "mytools/capability_report"
LLM_TIMEOUT = 12
CONTRACT_TIMEOUT = 120


async def _call(agent, receiver, payload):
    res = await agent.system.call(core.Envelop(
        sender=SENDER,
        receiver=receiver,
        intent="API_CALL",
        payload=payload,
    ))
    return (res.payload or {}) if res else {}


def _actions_of(contract):
    acts = []
    for name, meta in (contract.get("actions") or {}).items():
        meta = meta or {}
        params = meta.get("params") or []
        acts.append({
            "action": name,
            "desc": meta.get("description", ""),
            "required": [p.get("name") for p in params if p.get("required")],
            "optional": [p.get("name") for p in params if not p.get("required")],
        })
    return acts


def _template(route, acts):
    if not acts:
        return f'use_tool(target="{route}", action="", plugin_args={{}})'
    a = acts[0]
    args = ", ".join(f'"{p}": <{p}>' for p in a["required"]) or ""
    return (f'use_tool(target="{route}", action="{a["action"]}", '
            f'plugin_args={{{args}}})')


async def _contract(agent, route):
    try:
        c = await asyncio.wait_for(
            _call(agent, "builtins/tools/contract_agent", {
                "action": "get", "plugin": route,
            }),
            timeout=CONTRACT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return {"contract_source": "", "actions": [], "error": f"契约提取超时(>{CONTRACT_TIMEOUT}s)"}
    contract = c.get("contract") or {}
    return {
        "contract_source": c.get("source", ""),
        "actions": _actions_of(contract),
        "error": c.get("error", ""),
    }


async def _search(agent, task, limit):
    found = await _call(agent, "builtins/tools/cogitor", {
        "action": "search", "query": task, "limit": limit * 3,
    })
    routes, skills = [], []
    for r in (found.get("results") or []):
        name = str(r.get("name") or "")
        if not name:
            continue
        if r.get("is_skill") or name.startswith("skill/"):
            skills.append({
                "name": name,
                "desc": r.get("desc", ""),
                "path": r.get("path", ""),
            })
        else:
            routes.append({
                "route": name,
                "desc": r.get("desc", ""),
                "score": r.get("score", 0),
            })
    return found, routes[:limit], skills


async def _llm_json(agent, prompt, schema_hint):
    llm = getattr(agent, "llm", None)
    if not llm:
        return None, "LLM 未配置"
    try:
        raw = await asyncio.wait_for(
            llm.chat_json([
                {"role": "system", "content": "你是能力路由专家，只输出 JSON。"},
                {"role": "user", "content": prompt + "\n\n只输出如下结构的 JSON：\n" + schema_hint},
            ]),
            timeout=LLM_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return None, f"LLM 超时（>{LLM_TIMEOUT}s）"
    except Exception as e:
        return None, f"LLM 调用失败: {str(e)[:160]}"
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None, "LLM 返回非 JSON"
    if not isinstance(raw, dict):
        return None, "LLM 返回结构异常"
    return raw, ""


def _pick_action(acts, capability):
    """优先选描述与能力关键词重合的动作，否则取第一个。"""
    if not acts:
        return None
    keys = [k for k in capability if k.strip()]
    best, best_score = acts[0], -1
    for a in acts:
        d = f"{a['action']} {a['desc']}".lower()
        score = sum(1 for k in keys if k.lower() in d)
        if score > best_score:
            best, best_score = a, score
    return best


async def _plan(agent, task, limit):
    decomposed, err = await _llm_json(
        agent,
        f"把下面这个用户任务拆解成 2-5 个需要调用的系统能力，按执行顺序排列。"
        f"每个能力给出用于搜索插件的关键词（空格分隔，2-4 个词）。\n任务：{task}",
        '{"subqueries": [{"capability": "能力名", "keywords": "搜索关键词"}]}',
    )
    if not decomposed:
        return {"ok": False, "error": f"任务拆解失败: {err}"}

    subs = []
    for s in (decomposed.get("subqueries") or [])[:5]:
        kw = str(s.get("keywords") or s.get("capability") or "").strip()
        if not kw:
            continue
        subs.append({"capability": str(s.get("capability") or kw), "keywords": kw})
    if not subs:
        return {"ok": False, "error": "任务拆解结果为空"}

    results = await asyncio.gather(
        *[_search(agent, s["keywords"], 3) for s in subs],
        return_exceptions=True,
    )

    merged, skills, seen, skill_names = [], [], {}, set()
    for s, r in zip(subs, results):
        if isinstance(r, BaseException):
            s["error"] = str(r)[:160]
            continue
        found, routes, sk = r
        s["search_total"] = found.get("total", 0)
        s["routes"] = [x["route"] for x in routes]
        for x in routes:
            if x["route"] not in seen:
                seen[x["route"]] = x
                x["via"] = s["capability"]
                merged.append(x)
            else:
                seen[x["route"]].setdefault("also_via", []).append(s["capability"])
        for k in sk:
            if k["name"] not in skill_names:
                skill_names.add(k["name"])
                skills.append(k)

    merged = merged[:6]
    contracts = await asyncio.gather(
        *[_contract(agent, r["route"]) for r in merged],
        return_exceptions=True,
    )
    for r, c in zip(merged, contracts):
        if isinstance(c, BaseException):
            r["actions"] = []
            r["contract_error"] = str(c)[:160]
        else:
            r.update(c)
        r["call_template"] = _template(r["route"], r.get("actions") or [])

    steps, used = [], set()
    for i, s in enumerate(subs, 1):
        for route_name in s.get("routes", [])[:2]:
            if route_name in used:
                continue
            used.add(route_name)
            r = seen.get(route_name) or {}
            acts = r.get("actions") or []
            a = _pick_action(acts, s["capability"])
            args = {}
            if a:
                args = {p: f"<{p}>" for p in a.get("required", [])}
            steps.append({
                "step": len(steps) + 1,
                "capability": s["capability"],
                "route": route_name,
                "action": (a or {}).get("action", ""),
                "args": args,
                "why": (a or {}).get("desc", ""),
            })
            break

    return {
        "ok": True,
        "data": {
            "task": task,
            "mode": "plan",
            "subqueries": subs,
            "steps": steps,
            "candidates": merged,
            "skills": skills,
            "decompose_error": err,
        },
    }


async def execute(envelop, agent):
    try:
        raw = envelop.payload if isinstance(envelop.payload, dict) else {}
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
        action = str(payload.get("action") or "report")

        if action == "plan":
            task = str(payload.get("task") or "").strip()
            if not task:
                envelop.payload = {"ok": False, "error": "task 不能为空"}
                return envelop
            try:
                limit = int(payload.get("limit") or 5)
            except (TypeError, ValueError):
                limit = 5
            envelop.payload = await _plan(agent, task, limit)
            return envelop

        if action not in ("report", "quick"):
            envelop.payload = {"ok": False, "error": f"Unknown action: {action}"}
            return envelop

        task = str(payload.get("task") or "").strip()
        if not task:
            envelop.payload = {"ok": False, "error": "task 不能为空"}
            return envelop

        try:
            limit = int(payload.get("limit") or 5)
        except (TypeError, ValueError):
            limit = 5

        found, routes, skills = await _search(agent, task, limit)

        if action == "report":
            contracts = await asyncio.gather(
                *[_contract(agent, r["route"]) for r in routes],
                return_exceptions=True,
            )
            for r, c in zip(routes, contracts):
                if isinstance(c, BaseException):
                    r["actions"] = []
                    r["contract_error"] = str(c)[:160]
                else:
                    r.update(c)
                r["call_template"] = _template(r["route"], r.get("actions") or [])

        envelop.payload = {
            "ok": True,
            "data": {
                "task": task,
                "mode": action,
                "candidates": routes,
                "skills": skills,
                "search_total": found.get("total", 0),
                "sources": found.get("sources", {}),
            },
        }
        return envelop

    except Exception as e:
        try:
            agent.log.error(str(e))
        except Exception:
            pass
        envelop.payload = {"ok": False, "error": str(e)}
        return envelop
