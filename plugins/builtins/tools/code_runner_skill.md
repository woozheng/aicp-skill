---
title: code_runner 用法
description: 在 AICP 引擎里执行 Python 代码。写 async def execute(envelop, agent)，返回 {"data": ...}。沙箱已把阻塞调用异步化，禁止顶层执行/死循环/eval。
tags: [code_runner, python, sandbox, async]
---

# code_runner —— 执行 Python 代码

你提交一段 Python 代码，引擎在专用线程池里跑它，返回结果。不调 LLM、不生成代码，代码由你写好提交。

## 最小可用示例

async def execute(envelop, agent):
    return {"data": 1 + 1}

调用：

call_plugin(receiver="builtins/tools/code_runner",
            params={"code": "<上面那段代码>"})

返回：{"ok": true, "data": 2, "env": {...}}。

## 硬规则（违反直接 LLM_CODE 报错）

1. 必须定义 async def execute(envelop, agent) —— 参数名、顺序、async 都不能错。
2. 所有逻辑写在 execute 函数内 —— 禁止顶层执行代码、禁止 if __name__ == "__main__"。
3. 必须 return {"data": ...} —— 返回值必须是 dict 且含 data 字段，否则 LLM_CODE。
4. 禁止 eval / exec / __import__ —— 防止绕过沙箱构造。
5. 禁止无 break 的 while True —— 必然死循环会被 AST 预检拦下。

## 沙箱给你什么（能力全放行，阻塞全异步化）

### 1. import 全放行

import requests、import subprocess、import urllib、import http 都能用。但同步调用本地服务会被拦（见下）。

### 2. 阻塞调用全部异步化，必须 await

- 跑子进程：await subprocess.run(cmd, ...)    错误写法：subprocess.run(cmd)（不 await，返回 coroutine）
- 跑 shell 命令：await run("ls -la")    错误写法：run("ls -la")（不 await）
- 跑命令拿结构化结果：await run_async(["git", "status"])    错误写法：subprocess.Popen(...)（直接禁）
- 抓网页（外部）：fetch("https://...")
- 抓本地页面（验证服务）：await fetch_async("http://127.0.0.1:9000/...")    错误写法：fetch("http://127.0.0.1:...")（同步禁内网）
- POST JSON：await post_json(url, {"k": "v"})    错误写法：requests.post("http://127.0.0.1/...")（同步禁本地）

核心原则：打本地回环（127.0.0.1 / localhost）必须用 await fetch_async / await post_json；打外部可以用同步 fetch / requests。

### 3. subprocess.run 的返回

await subprocess.run(cmd, ...) 返回 CompletedProcess，有 .returncode / .stdout / .stderr。默认 capture_output=True, text=True, encoding="utf-8"，不用自己传。

### 4. run_async 的返回（推荐，结构化）

r = await run_async(["git", "status"])
# r = {"returncode": 0, "stdout": "...", "stderr": "..."}

run_async 用 create_subprocess_exec，不走 shell，最安全。超时默认 30s，最大 300s。

### 5. 危险命令会被拦

rm -rf /、format c:、mkfs、dd if=/dev/zero、shutdown、reboot 等会抛 SandboxBlockError。正常开发命令（ls/cat/git/python -m pip 等）一律放行。

### 6. 云元数据地址禁访问

169.254.169.254、metadata.google.internal 会被拦（安全策略）。

## 依赖安装

需要额外包时，传 packages 数组：

call_plugin(receiver="builtins/tools/code_runner",
            params={"code": "...", "packages": ["pandas", "requests"]})

引擎会 pip install 缺失的包。packages 必须是数组，传字符串/dict 会直接 LLM_CODE 报错。

## 超时与并发

- 单次执行硬超时 60s —— 超时返回 SANDBOX_BLOCK，主服务脱身，但线程仍在后台跑到自然结束（Python 线程无法强杀）。
- 专用线程池 2 并发 —— 第 3 个排队。如果看到「线程池繁忙（排队 N 个）」日志，说明前面有卡住的执行。
- 下载/安装类操作超过 60s 会被中断，先估算耗时再决定。大文件分多次下载，或换 run_async 起后台进程。

## 返回值结构

成功：

{"ok": true, "data": <你 return 的 data>, "env": {...}}

失败：

{"ok": false, "error": "【错误分类】LLM_CODE\n【一句话】...\n【原因】...", "error_category": "LLM_CODE"}

error_category 四种：

- LLM_CODE：你写的代码有问题（语法/结构/运行时异常）→ 读【原因】改代码，重新提交
- SANDBOX_BLOCK：沙箱拦截（危险命令/超时/同步调本地）→ 换实现（await run_async / await fetch_async）
- RUNTIME_INTERNAL：引擎内部问题，不是你代码的错 → 告知用户，不要改代码
- DEPENDENCY：pip install 失败 → 换库或换方案（加对包名，或纯 Python 实现）

## env 字段（帮你决策）

每次成功返回都带 env，包含：cpu_count / mem_total_gb / mem_available_gb / disk_free_gb / gpu / gpu_name / libs_available / libs_missing / net / exec_timeout_s。

决策时按 env 来：

- env.gpu === false → 不要装 CUDA 包
- env.net["pypi"] === "fail: Timeout" → 不要下大文件
- env.libs_missing 含你要的库 → packages 里加上
- env.disk_free_gb < 1 → 不要下大模型

## 调 AICP 自己的插件

code_runner 里可以调其他插件：

async def execute(envelop, agent):
    r = await agent.system.call(
        Envelop(
            sender="code_runner",
            receiver="builtins/tools/cogitor",
            payload={"action": "search", "terms": ["文件"]},
        )
    )
    return {"data": r.payload}

用途：批量调插件、串行编排、把多个插件的返回聚合后再分析。

## 五个常见坑（都是真踩过的）

1. import 写在 execute 外 → NameError。所有 import 必须写在函数内，或模块顶层（但模块顶层 import 也会被 AST 预检判「顶层执行代码」——所以统一写在 execute 内）。
2. await subprocess.run 写成 subprocess.run → 返回 coroutine，后续 .stdout 报 AttributeError。必须 await。
3. 同步 requests.get("http://127.0.0.1:...") → 被拦。改 await fetch_async(url)。
4. packages="pandas"（字符串）→ LLM_CODE。改 packages=["pandas"]。
5. 返回值缺 data → return {"result": ...} 不行，必须 return {"data": ...}。

## 一句话总结

写 async def execute(envelop, agent)，所有阻塞调用加 await，打本地用 fetch_async/post_json，返回 {"data": ...}，超时 60s 内搞定。拿不准就看返回的 error_category 和 env，它们会告诉你下一步该改什么。
