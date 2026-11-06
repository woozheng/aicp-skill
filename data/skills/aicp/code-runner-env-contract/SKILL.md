---
title: 设定 code_runner 超时并读懂 env
description: 给 code_runner 设 timeout_s 并读懂 env 字段（用户说「可以设置超时的时间」「timeout_s 怎么用」「env.exec_timeout_s 是什么意思」时用），避免超时误判与 env 缓存污染
category: aicp
tags: [code-runner, timeout, sandbox, env, contract]
triggers: 超时, timeout_s, 执行超时, 跑太久, 卡住, 限时, 超时时间, 超过60秒, 硬超时, 沙箱拦截, env字段, exec_timeout_s, exec_timeout_default, sandbox block, time limit, run too long, set timeout, timeout
not_triggers: 工作区, 目录树, 调试日志, 前端页面, 会话列表
examples: "我修改了 code runner 可以设置超时的时间 你测试一下", "timeout_s 传 9999 会不会被夹到 300", "env.exec_timeout_s 和 exec_timeout_default 有什么区别", "why does it stop after 60 seconds"
version: 1.0
updated_at: 2026-11-06
---

# 设定 code_runner 超时并读懂 env

## 何时用

- 要给 code_runner 传超时秒数（用户说「可以设置超时的时间」时）
- 要判断某次执行的上限是 60 秒还是别的值（用户说「timeout_s 怎么用」时）
- 要读返回里的 env 字段（用户说「env.exec_timeout_s 是什么意思」时）
- 要排查「跑太久被中断」到底是超时还是别的错（用户说「为什么卡住了」时）

**不适用**：工作区目录树；调试日志；前端页面；会话列表。

## 前置条件

- 插件：`builtins/tools/code_runner`
- 无需 LLM；本 skill 不涉及 `agent.llm` 兜底
- 沙箱契约：`execute` 必须是 `async def execute(envelop, agent)`，所有逻辑（含 import）写在函数内，返回 `{"data": ...}`

## 核心步骤

**步骤 1：传 timeout_s**

`timeout_s` 是本次执行的超时秒数，范围 **1-300**，不传默认 **60**（`DEFAULT_TIMEOUT_SECONDS`）。越界或非整数 → `ok=false`、`error_category="LLM_CODE"`，**校验阶段就返回**，不会执行代码。

```python
use_tool(
    target="builtins/tools/code_runner",
    action="",
    plugin_args={
        "code": "async def execute(envelop, agent):\n    return {'data': {'ok': True}}\n",
        "packages": [],
        "timeout_s": 120,
    },
)
```

**步骤 2：读 env 字段语义**

| 字段 | 含义 | 出现时机 |
|---|---|---|
| `env.exec_timeout_s` | **本次实际**使用的超时 | 成功路径 + 沙箱拦截/超时 + 运行时异常 |
| `env.exec_timeout_default` | 不传 timeout_s 时的默认值（60） | 成功 + 所有失败路径 |
| `env.exec_timeout_used` | **已废弃，不要再读** | 不再出现 |

- 成功路径：`env.exec_timeout_s = 本次 timeout_s`。
- 沙箱拦截 / 运行时异常路径：**写** `exec_timeout_s`（本次超时已生效并被使用）。
- 校验失败路径（timeout_s 越界、AST 预检失败、返回值非法）：**不写** `exec_timeout_s`（本次没有生效的超时），只写 `exec_timeout_default`。

**步骤 3：超时的表现**

`timeout_s=2` 跑 5 秒代码 → 2 秒后返回：`ok=false`、`error_category="SANDBOX_BLOCK"`、error 含「执行超时（2s）」。注意：**线程无法强杀**，超时后代码仍在后台线程跑到自然结束，专用池 `max_workers=2`，第 3 个并发会排队。

**步骤 4：一次调用里验证多个 timeout_s 值**

```python
async def execute(envelop, agent):
    from core import Envelop

    results = {}
    slow = "async def execute(envelop, agent):\n    import asyncio\n    await asyncio.sleep(5)\n    return {'data': {}}\n"
    for ts in [2, 30]:
        r = await agent.system.call(Envelop(
            receiver="builtins/tools/code_runner",
            payload={"action": "", "code": slow, "timeout_s": ts},
        ))
        p = getattr(r, "payload", None) or {}
        results[ts] = {
            "ok": p.get("ok"),
            "error_category": p.get("error_category"),
            "exec_timeout_s": (p.get("env") or {}).get("exec_timeout_s"),
        }
    return {"data": results}
```

- `ts=2` → `error_category="SANDBOX_BLOCK"`，`exec_timeout_s=2`（超时路径也写本次值）。
- `ts=30` → `ok=true`，`exec_timeout_s=30`。
- 两次调用后 `results[2]["exec_timeout_s"]` 仍为 2、`results[30]["exec_timeout_s"]` 为 30，说明浅拷贝生效、无缓存污染。

## 判断规则

| 条件 | 动作 |
|---|---|
| 目标是下载 / pip install / 大文件 | `timeout_s` 给 180-300 |
| 目标是纯计算 / 断言校验 | `timeout_s` 给 15-30 |
| 目标调外部 API | `timeout_s` 给 30-60 |
| 不传 timeout_s | 用默认 60 |
| 返回 env 里没有 `exec_timeout_s` | 本次在校验阶段就失败了，代码根本没跑 |
| 返回 `error_category="SANDBOX_BLOCK"` 且 error 含「执行超时」 | 是真超时，调大 timeout_s 或拆分任务 |
| 返回 `error_category="SANDBOX_BLOCK"` 但不含「执行超时」 | 是能力被拦（如 Popen / 同步 requests 打本地），改实现 |
| `timeout_s` 传非整数或超出 1-300 | 会被夹成校验失败，不会执行，先自己纠正 |

## 踩过的坑

| 坑 | 现象 | 根因 | 修法 |
|---|---|---|---|
| `_get_env_summary` 直接 `return _ENV_CACHE` | 后续所有调用（含失败分支）都看到脏值 `exec_timeout_s=120` | 模块级可变缓存被调用方写脏 | 返回浅拷贝 `return dict(_ENV_CACHE)` |
| 以为 `env.exec_timeout_s` 是静态上限 | 误以为永远是 60 | 它是「本次实际值」，每次调用由调用方写入 | 读 `exec_timeout_s` 判断本次，读 `exec_timeout_default` 判断默认 |
| 把校验失败也算成「超时了」 | 误判失败原因，盲目调大 timeout_s | 校验失败路径根本不写 `exec_timeout_s` | 先看有没有 `exec_timeout_s` 字段，再看 error_category |
| 超时后以为代码被停了 | 后台线程仍在跑，池被占满，第 3 个任务排队 | Python 线程无法强杀 | 拆分任务；避免连续长任务占用 2 并发池 |
| `timeout_s` 传 9999 | 直接校验失败，什么都没跑 | 范围是 1-300 | 自己先夹到 300 |

## 验证

用 code_runner 跑（骨架含 `async def execute(envelop, agent):`，所有 import 写在函数内，返回 `{"data": ...}`；禁 print / time.sleep / input / while True / subprocess.Popen / 同步 requests 打本地 / eval / exec）：

```python
async def execute(envelop, agent):
    import asyncio

    async def run(code, timeout_s=None):
        from core import Envelop
        payload = {"action": "", "code": code, "packages": []}
        if timeout_s is not None:
            payload["timeout_s"] = timeout_s
        r = await agent.system.call(
            Envelop(receiver="builtins/tools/code_runner", payload=payload)
        )
        return getattr(r, "payload", None) or {}

    slow = "async def execute(envelop, agent):\n    import time\n    await asyncio.sleep(5)\n    return {'data': {}}\n"
    timeout_p = await run(slow, 2)
    bad_p = await run(slow, 9999)
    ok_p = await run("async def execute(envelop, agent):\n    return {'data': {'x': 1}}\n", 30)

    t_env = timeout_p.get("env") or {}
    b_env = bad_p.get("env") or {}
    o_env = ok_p.get("env") or {}

    checks = {
        "超时为沙箱拦截": timeout_p.get("error_category") == "SANDBOX_BLOCK",
        "超时error含超时字样": "执行超时" in (timeout_p.get("error") or ""),
        "超时路径有exec_timeout_s": t_env.get("exec_timeout_s") == 2,
        "超时路径有default": b_env.get("exec_timeout_default") == 60,
        "校验失败无exec_timeout_s": "exec_timeout_s" not in b_env,
        "校验失败分类LLM_CODE": bad_p.get("error_category") == "LLM_CODE",
        "成功路径exec_timeout_s正确": o_env.get("exec_timeout_s") == 30,
        "无废弃字段": "exec_timeout_used" not in o_env,
    }
    return {"data": {"ok": all(checks.values()), "checks": checks}}
```

断言清单：
1. `timeout_s=2` 跑 5 秒代码 → `error_category="SANDBOX_BLOCK"`，error 含「执行超时（2s）」
2. 超时路径的 env **有** `exec_timeout_s=2`
3. `timeout_s=9999` → `error_category="LLM_CODE"`，env **没有** `exec_timeout_s`，但有 `exec_timeout_default=60`
4. 成功路径 env 有 `exec_timeout_s` 等于本次传入值，且无 `exec_timeout_used`
5. 连续两次调用后，第二次 env 里的 `exec_timeout_s` 等于第二次的值（验证浅拷贝生效，无缓存污染）

## 参考

- 相关 skill：`skill/aicp/skill-authoring`（按标准写 skill）、`skill/aicp/plugin-action-smoke-test`（插件动作自检）
- 相关插件：`builtins/tools/code_runner`、`builtins/tools/file_utils_api`
- 相关文件：`plugins/builtins/tools/code_runner.py`