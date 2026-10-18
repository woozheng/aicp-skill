"""builtins/tools/code_runner — 代码执行插件

只执行传入的代码，不调 LLM，不生成代码。
代码由网页 AI 写好，通过 call_plugin 提交。

接收：
    payload.code: Python 代码文本（必须是 async def execute(envelop, agent) 结构）
    payload.packages: 依赖列表（可选，如 ["pandas", "requests"]）

返回：
    {"ok": true, "data": ...} 或 {"ok": false, "error": "...", "error_category": "..."}
"""

import asyncio
import ast
import importlib
import json
import re
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

import core


# ============================================================
# 常量
# ============================================================

DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_SUBPROCESS_TIMEOUT = 30

BLOCKED_BUILTINS = frozenset({
    "eval", "exec", "compile", "open", "globals", "locals", "__import__"
})

import builtins
SAFE_BUILTINS = {
    k: v for k, v in builtins.__dict__.items()
    if k not in BLOCKED_BUILTINS and not k.startswith("_")
}


class SandboxBlockError(RuntimeError):
    pass


DANGEROUS_COMMANDS = [
    "format ", "rm -rf /", "rm -rf ~", "del /f /s /q c:\\",
    "del /f /s /q d:\\", "shutdown", "mkfs", "dd if=/dev/zero",
    "diskpart", "fdisk", "format c:",
]

BLOCKED_FETCH_HOSTS = [
    "169.254.169.254", "metadata.google.internal",
    "localhost", "127.0.0.1", "0.0.0.0",
]


# ============================================================
# 代码清洗
# ============================================================

def _strip_code_wrapper(code: str) -> str:
    """如果代码被 ~~~python ... ~~~ 或 ```python ... ``` 包裹，剥掉"""
    if not code:
        return code
    code = code.strip()
    patterns = [
        (r"^~~~python\s*\n(.*?)\n~~~$", re.DOTALL),
        (r"^```python\s*\n(.*?)\n```$", re.DOTALL),
        (r"^~~~\s*\n(.*?)\n~~~$", re.DOTALL),
        (r"^```\s*\n(.*?)\n```$", re.DOTALL),
    ]
    for pattern, flags in patterns:
        m = re.match(pattern, code, flags)
        if m:
            return m.group(1).strip()
    return code


# ============================================================
# AST 预检
# ============================================================

def _ast_pre_check(source: str) -> Optional[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return f"语法解析错误：第{e.lineno}行 {e.msg}"

    has_execute = False

    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            return "禁止顶层执行代码，全部逻辑写在 execute 函数内"
        if isinstance(node, ast.If):
            test = node.test
            if isinstance(test, ast.Compare):
                left = test.left
                if isinstance(left, ast.Name) and left.id == "__name__":
                    return "禁止使用 if __name__ == \"__main__\""

    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "execute":
            has_execute = True
            args = [a.arg for a in node.args.args]
            if args != ["envelop", "agent"]:
                return f"execute 参数必须是 (envelop, agent)，实际是 {args}"

    if not has_execute:
        return "代码中必须定义 async def execute(envelop, agent)"

    # 死循环 / 阻塞检测
    for node in ast.walk(tree):
        if isinstance(node, ast.While):
            if isinstance(node.test, ast.Constant) and node.test.value in (True, 1):
                return "禁止 while True，用有限循环"
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "sleep":
                if isinstance(func.value, ast.Name) and func.value.id == "time":
                    return "禁止 time.sleep，用 await asyncio.sleep(N)"
            if isinstance(func, ast.Name) and func.id == "input":
                return "禁止 input()，沙箱没有交互输入"

    return None


# ============================================================
# 依赖安装
# ============================================================

_installed_packages: set = set()
_install_lock: Optional[asyncio.Lock] = None


async def ensure_packages(packages: List[str]) -> None:
    """自动安装缺失的包"""
    global _install_lock

    if not packages:
        return

    missing = []
    for pkg in packages:
        if pkg in _installed_packages:
            continue
        import_name = pkg.replace("-", "_")
        try:
            importlib.import_module(import_name)
            _installed_packages.add(pkg)
        except ImportError:
            missing.append(pkg)

    if not missing:
        return

    if _install_lock is None:
        _install_lock = asyncio.Lock()

    async with _install_lock:
        still_missing = []
        for pkg in missing:
            if pkg in _installed_packages:
                continue
            import_name = pkg.replace("-", "_")
            try:
                importlib.import_module(import_name)
                _installed_packages.add(pkg)
            except ImportError:
                still_missing.append(pkg)

        if not still_missing:
            return

        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", *still_missing,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"pip install 失败（exit={proc.returncode}）：{err[:500]}")

        for pkg in still_missing:
            _installed_packages.add(pkg)

        importlib.invalidate_caches()


# ============================================================
# 沙箱
# ============================================================

def _check_dangerous_command(cmd: str):
    if not isinstance(cmd, str):
        return cmd
    cmd_lower = cmd.lower()
    for d in DANGEROUS_COMMANDS:
        if d in cmd_lower:
            raise SandboxBlockError(f"危险命令被禁止: {d}")
    return cmd


def _check_fetch_url(url: str):
    if not isinstance(url, str):
        raise SandboxBlockError("fetch url 必须是字符串")
    if not url.startswith(("http://", "https://")):
        raise SandboxBlockError(f"fetch 只支持 http/https: {url}")
    url_lower = url.lower()
    for b in BLOCKED_FETCH_HOSTS:
        if b in url_lower:
            raise SandboxBlockError(f"fetch 禁止访问: {b}")
    return url


def _build_safe_globals(agent) -> Dict[str, Any]:
    import requests as _requests
    import bs4 as _bs4
    from pathlib import Path as _Path

    g = dict(SAFE_BUILTINS)

    _original_get = _requests.get
    _original_post = _requests.post

    def _safe_subprocess_run(cmd, **kwargs):
        if isinstance(cmd, str):
            _check_dangerous_command(cmd)
            for c in (">", "<", "`", "$", ";", "&"):
                if c in cmd:
                    raise SandboxBlockError(
                        f"字符串命令中不允许 `{c}`，用 Python open() 替代"
                    )
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
        res = subprocess.run(cmd, **kwargs)
        res.stdout = res.stdout or ""
        res.stderr = res.stderr or ""
        return res

    class SubprocessProxy:
        run = staticmethod(_safe_subprocess_run)
        PIPE = subprocess.PIPE
        STDOUT = subprocess.STDOUT
        DEVNULL = subprocess.DEVNULL
        TimeoutExpired = subprocess.TimeoutExpired
        CalledProcessError = subprocess.CalledProcessError

        @staticmethod
        def Popen(*a, **kw):
            raise SandboxBlockError("禁止使用 subprocess.Popen，用 subprocess.run")

    def _safe_get(url, *a, **kw):
        if url.startswith(("http://127.0.0.1", "http://localhost")):
            raise SandboxBlockError("禁止用同步 requests 调本地 API，用 aiohttp")
        return _original_get(url, *a, **kw)

    def _safe_post(url, *a, **kw):
        if url.startswith(("http://127.0.0.1", "http://localhost")):
            raise SandboxBlockError("禁止用同步 requests 调本地 API，用 aiohttp")
        return _original_post(url, *a, **kw)

    class SafeRequests:
        get = staticmethod(_safe_get)
        post = staticmethod(_safe_post)

    def _noop_print(*a, **kw):
        raise SandboxBlockError("禁止 print()，用 return {'data': ...}")

    def _safe_run(cmd, timeout=DEFAULT_SUBPROCESS_TIMEOUT, cwd=None):
        if timeout > 300:
            raise ValueError(f"Timeout {timeout}s exceeds maximum 300s")
        if isinstance(cmd, str):
            _check_dangerous_command(cmd)
            for c in (">", "<", "`", "$", ";", "&"):
                if c in cmd:
                    raise SandboxBlockError(f"run() 字符串命令中不允许 `{c}`")
        result = subprocess.run(
            cmd, shell=True, capture_output=True, timeout=timeout,
            text=True, encoding="utf-8", errors="replace", cwd=cwd,
        )
        return result.stdout or ""

    async def _safe_run_async(cmd, timeout=DEFAULT_SUBPROCESS_TIMEOUT, cwd=None):
        if timeout > 300:
            raise ValueError(f"Timeout {timeout}s exceeds maximum 300s")
        if isinstance(cmd, str):
            _check_dangerous_command(cmd)
            for c in (">", "<", "`", "$", ";", "&"):
                if c in cmd:
                    raise SandboxBlockError(f"run_async 字符串命令中不允许 `{c}`")
            cmd_list = cmd.split()
        elif isinstance(cmd, list):
            for p in cmd:
                if isinstance(p, str):
                    _check_dangerous_command(p)
            cmd_list = cmd
        else:
            raise SandboxBlockError("run_async 命令必须是字符串或列表")

        if not cmd_list:
            raise SandboxBlockError("run_async 命令不能为空")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd_list,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
        except FileNotFoundError:
            raise SandboxBlockError(f"命令不存在: {cmd_list[0]}")

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return {
                "returncode": proc.returncode,
                "stdout": stdout.decode("utf-8", errors="replace") if stdout else "",
                "stderr": stderr.decode("utf-8", errors="replace") if stderr else "",
            }
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            raise SandboxBlockError(f"run_async 超时（{timeout}s），已终止")

    def _safe_fetch(url, timeout=15):
        _check_fetch_url(url)
        resp = _original_get(url, timeout=timeout, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        resp.raise_for_status()
        if resp.encoding and resp.encoding.lower() != "iso-8859-1":
            resp.encoding = resp.encoding
        else:
            resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text

    class SafeOS:
        def __init__(self):
            self._os = __import__("os")
        def __getattr__(self, name):
            if name == "chdir":
                raise SandboxBlockError("os.chdir 被禁用，用绝对路径或 run(cmd, cwd=...)")
            return getattr(self._os, name)
        def __dir__(self):
            return dir(self._os)

    g.update({
        "agent": agent,
        "Envelop": core.Envelop,
        "asyncio": asyncio,
        "json": json,
        "os": SafeOS(),
        "Path": _Path,
        "open": open,
        "requests": SafeRequests,
        "BeautifulSoup": _bs4.BeautifulSoup,
        "re": re,
        "run": _safe_run,
        "run_async": _safe_run_async,
        "fetch": _safe_fetch,
        "subprocess": SubprocessProxy,
        "print": _noop_print,
    })

    return g


# ============================================================
# 执行器
# ============================================================

async def _execute_code(code: str, agent) -> Dict[str, Any]:
    """执行代码，返回 {ok, data/error, error_category}"""

    # 0. 确保 data/workspace 存在
    try:
        Path("data/workspace").mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    # 1. AST 预检
    ast_err = _ast_pre_check(code)
    if ast_err:
        return {
            "ok": False,
            "error": f"【错误分类】LLM_CODE\n【一句话】AST 预检失败\n【原因】{ast_err}",
            "error_category": "LLM_CODE",
        }

    # 2. 构建沙箱
    try:
        safe_globals = _build_safe_globals(agent)
    except Exception as e:
        return {
            "ok": False,
            "error": f"【错误分类】RUNTIME_INTERNAL\n【一句话】构建沙箱失败\n【原因】{type(e).__name__}: {e}",
            "error_category": "RUNTIME_INTERNAL",
        }

    local_ns: Dict[str, Any] = {}

    # 3. exec 编译
    try:
        exec(code, safe_globals, local_ns)
    except SyntaxError as e:
        return {
            "ok": False,
            "error": f"【错误分类】LLM_CODE\n【一句话】语法错误\n【原因】第 {e.lineno} 行: {e.msg}",
            "error_category": "LLM_CODE",
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"【错误分类】LLM_CODE\n【一句话】代码加载失败\n【原因】{type(e).__name__}: {e}",
            "error_category": "LLM_CODE",
        }

    # 4. 检查 execute
    execute_fn = local_ns.get("execute")
    if execute_fn is None:
        return {
            "ok": False,
            "error": "【错误分类】LLM_CODE\n【一句话】找不到 execute 函数\n【原因】必须定义 async def execute(envelop, agent)",
            "error_category": "LLM_CODE",
        }
    if not asyncio.iscoroutinefunction(execute_fn):
        return {
            "ok": False,
            "error": "【错误分类】LLM_CODE\n【一句话】execute 不是 async 函数\n【原因】必须定义为 async def",
            "error_category": "LLM_CODE",
        }

    # 5. 执行
    envelop = core.Envelop(payload={"state": {}})
    try:
        ret = await asyncio.wait_for(
            execute_fn(envelop, agent),
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "error": f"【错误分类】LLM_CODE\n【一句话】执行超时\n【原因】超过 {DEFAULT_TIMEOUT_SECONDS} 秒",
            "error_category": "LLM_CODE",
        }
    except SandboxBlockError as e:
        return {
            "ok": False,
            "error": f"【错误分类】SANDBOX_BLOCK\n【一句话】沙箱拦截\n【原因】{e}",
            "error_category": "SANDBOX_BLOCK",
        }
    except Exception as e:
        tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        return {
            "ok": False,
            "error": f"【错误分类】LLM_CODE\n【一句话】运行时异常\n【原因】{type(e).__name__}: {e}\n【堆栈】\n{tb}",
            "error_category": "LLM_CODE",
        }

    # 6. 返回值校验
    if not isinstance(ret, dict):
        return {
            "ok": False,
            "error": f"【错误分类】LLM_CODE\n【一句话】返回值不是字典\n【原因】必须 return {{'data': ...}}，实际是 {type(ret).__name__}",
            "error_category": "LLM_CODE",
        }
    if "data" not in ret:
        return {
            "ok": False,
            "error": "【错误分类】LLM_CODE\n【一句话】返回值缺少 data 字段\n【原因】必须 return {'data': ...}",
            "error_category": "LLM_CODE",
        }

    return {"ok": True, "data": ret["data"]}


# ============================================================
# 插件入口
# ============================================================

async def execute(envelop, agent):
    code = envelop.payload.get("code", "")
    packages = envelop.payload.get("packages", [])

    if not code:
        envelop.payload = {"ok": False, "error": "缺少 code 参数"}
        return envelop

    if not isinstance(packages, list):
        packages = []

    # 代码清洗：剥掉 ~~~python~~~ 包裹
    code = _strip_code_wrapper(code)

    if not code:
        envelop.payload = {"ok": False, "error": "代码为空"}
        return envelop

    # 先装依赖
    if packages:
        try:
            await ensure_packages(packages)
        except Exception as e:
            envelop.payload = {
                "ok": False,
                "error": f"【错误分类】DEPENDENCY\n【一句话】依赖安装失败\n【原因】{e}",
                "error_category": "DEPENDENCY",
            }
            return envelop

    result = await _execute_code(code, agent)
    envelop.payload = result
    return envelop


def help():
    return {
        "route": "builtins/tools/code_runner",
        "description": "执行传入的 Python 代码，不调 LLM，不生成代码",
        "input": {
            "code": "Python 代码文本，必须定义 async def execute(envelop, agent)，必须 return {'data': ...}",
            "packages": "依赖列表（可选），如 ['pandas', 'requests']",
        },
        "output": {
            "ok": "是否成功",
            "data": "成功时的结果",
            "error": "失败时的错误描述",
            "error_category": "LLM_CODE / SANDBOX_BLOCK / RUNTIME_INTERNAL / DEPENDENCY",
        },
    }
