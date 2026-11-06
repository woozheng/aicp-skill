"""builtins/tools/code_runner — 代码执行插件

只执行传入的代码，不调 LLM，不生成代码。
代码由网页 AI 写好，通过 call_plugin 提交。

设计原则：
    1. 能力全放行：agent 想写什么写什么（open / print / import requests / import subprocess / HTTP 都可用）
    2. 阻塞全异步化：所有可能阻塞事件循环的调用，底层透明转成异步/线程池
    3. 兜底靠隔离：execute 整体丢专用线程池跑，同步阻塞也挂不到主事件循环
    4. 只拦真正危险的东西：rm -rf /、format、mkfs 这类会毁系统的命令

稳定性加固（本次）：
    - 专用线程池 _CODE_EXECUTOR（max_workers=2），卡住的执行不占默认池
    - 超时/完成/排队都打日志，方便排查「卡住」是执行慢还是排队
    - 不改 AST 检查、沙箱能力、execute 签名、返回值结构

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
import logging
import re
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

import core


logger = logging.getLogger("aicp.code_runner")


# ============================================================
# 常量
# ============================================================

DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_SUBPROCESS_TIMEOUT = 30
MAX_TIMEOUT_SECONDS = 300

# ★ code_runner 专用线程池：独立于默认池，卡住的执行不占其他插件的线程
#   最多 2 个并发执行，第 3 个排队（避免无限起线程把内存打爆）
_CODE_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="code_runner")

# 只拦截真正会毁系统的命令，不拦"能力"
DANGEROUS_COMMANDS = [
    "rm -rf /", "rm -rf ~", "rm -rf /*",
    "format c:", "format d:", "del /f /s /q c:\\", "del /f /s /q d:\\",
    "mkfs", "dd if=/dev/zero", "diskpart", "fdisk",
    "shutdown", "reboot",
]

# 云元数据地址（安全策略，必须拦）
BLOCKED_FETCH_HOSTS = [
    "169.254.169.254", "metadata.google.internal",
]

# ============================================================
# 环境摘要（模块级缓存，首次算，后续复用）
# ============================================================

_ENV_CACHE: Optional[Dict[str, Any]] = None
_ENV_CACHE_AT: float = 0.0
_ENV_CACHE_TTL: float = 300.0   # 5 分钟刷新一次（磁盘/网络可能变）


def _get_env_summary() -> Dict[str, Any]:
    """探测当前执行环境的关键事实，供模型决策参考。

    缓存 5 分钟：磁盘/网络速度可能变，但 CPU/GPU/库可用性基本不变。
    模型每次跑代码都看到 env，不需要专门「探测环境」这一步。
    """
    global _ENV_CACHE, _ENV_CACHE_AT

    now = time.time()
    if _ENV_CACHE is not None and (now - _ENV_CACHE_AT) < _ENV_CACHE_TTL:
        # ★ 返回浅拷贝：调用方会往 env 里写本次超时等字段，
        #   直接返回缓存对象会造成「可变缓存污染」（污染后所有调用都看到脏值）
        return dict(_ENV_CACHE)

    res: Dict[str, Any] = {}

    # --- CPU / 内存 ---
    try:
        import os as _os
        res["cpu_count"] = _os.cpu_count()
    except Exception:
        res["cpu_count"] = "unknown"
    try:
        import psutil
        vm = psutil.virtual_memory()
        res["mem_total_gb"] = round(vm.total / 1e9, 1)
        res["mem_available_gb"] = round(vm.available / 1e9, 1)
    except Exception:
        res["mem_total_gb"] = "psutil 未装"

    # --- 磁盘 ---
    try:
        import shutil
        du = shutil.disk_usage(".")
        res["disk_total_gb"] = round(du.total / 1e9, 1)
        res["disk_free_gb"] = round(du.free / 1e9, 1)
    except Exception:
        res["disk_free_gb"] = "unknown"

    # --- GPU ---
    # 不 import torch：仅为探测 GPU 存在性就 import torch 会常驻 ~175MB 内存。
    # 用 nvidia-smi 判断有没有 NVIDIA GPU，用 find_spec 判断 torch 是否已安装。
    try:
        import importlib.util as _ilu_gpu
        _has_torch = _ilu_gpu.find_spec("torch") is not None
    except Exception:
        _has_torch = False

    _gpu_found = False
    _gpu_name = None
    try:
        import subprocess as _sp
        _r = _sp.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        if _r.returncode == 0 and _r.stdout and _r.stdout.strip():
            _lines = [x.strip() for x in _r.stdout.strip().splitlines() if x.strip()]
            if _lines:
                _gpu_found = True
                _gpu_name = _lines[0]
    except Exception:
        pass

    if _gpu_found:
        res["gpu"] = True
        res["gpu_name"] = _gpu_name
        res["gpu_note"] = "检测到 NVIDIA GPU"
    elif _has_torch:
        res["gpu"] = False
        res["gpu_note"] = "torch 已装但未检测到 NVIDIA GPU（无 GPU 或驱动缺失）"
    else:
        res["gpu"] = "no_torch"
        res["gpu_note"] = "torch 未装，不要尝试 CUDA 相关包"

    # --- 常用库可用性 ---
    import importlib.util as _ilu
    libs = ["cv2", "skimage", "PIL", "numpy", "requests", "aiohttp",
            "torch", "onnxruntime", "rapidocr_onnxruntime", "easyocr"]
    available = []
    missing = []
    for name in libs:
        try:
            if _ilu.find_spec(name) is not None:
                available.append(name)
            else:
                missing.append(name)
        except Exception:
            missing.append(name)
    res["libs_available"] = available
    res["libs_missing"] = missing

    # --- 网络速度探测（轻量，HEAD 请求，2s 超时）---
    # 只探「大文件下载源」，用于模型判断「能不能下模型」
    net_sources = {
        "pypi": "https://pypi.org",
        "hf-mirror": "https://hf-mirror.com",
        "modelscope": "https://modelscope.cn",
    }
    net: Dict[str, str] = {}
    try:
        import requests as _rq
        for name, url in net_sources.items():
            try:
                t0 = time.time()
                _rq.head(url, timeout=2, allow_redirects=True)
                ms = int((time.time() - t0) * 1000)
                net[name] = f"ok {ms}ms"
            except Exception as e:
                net[name] = f"fail: {type(e).__name__}"
    except ImportError:
        net["_"] = "requests 未装"
    res["net"] = net

    # --- 执行限制（code_runner 自己的事实）---
    # 注意：exec_timeout_s 不在这里写死。它是「本次实际值」，
    # 由调用方在返回前写入 env；默认值见 env.exec_timeout_default。
    res["exec_max_concurrent"] = 2
    res["note"] = (
        "env.exec_timeout_s 是本次 code_runner 执行的实际硬超时；"
        "env.exec_timeout_default 是不传 timeout_s 时的默认值。"
        "下载/安装类操作超过这个时间会被中断，请先估算耗时再决定。"
    )

    _ENV_CACHE = res
    _ENV_CACHE_AT = now
    return res


def _failure_env() -> Dict[str, Any]:
    """失败分支的 env：只有默认值，没有「本次实际值」。

    校验失败（timeout_s 越界/格式错）时本次没有生效的超时，
    因此不写 exec_timeout_s，避免和上一次调用的残留混淆。
    """
    env = _get_env_summary()
    env.pop("exec_timeout_s", None)
    env["exec_timeout_default"] = DEFAULT_TIMEOUT_SECONDS
    return env


class SandboxBlockError(RuntimeError):
    pass


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
# AST 预检（只做"结构合法性"检查，不拦能力）
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

    # 只拦"必然死循环"，不拦正常能力
    for node in ast.walk(tree):
        if isinstance(node, ast.While):
            if isinstance(node.test, ast.Constant) and node.test.value in (True, 1):
                has_break = any(isinstance(n, ast.Break) for n in ast.walk(node))
                if not has_break:
                    return "禁止无 break 的 while True，用有限循环"

    # 禁止 eval/exec/__import__（防止绕过沙箱构造）
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in ("eval", "exec", "__import__"):
                return f"禁止使用 {node.func.id}()"

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
# 沙箱：能力全放行，阻塞全异步化
# ============================================================

def _check_dangerous_command(cmd: str):
    if not isinstance(cmd, str):
        return cmd
    cmd_lower = cmd.lower()
    for d in DANGEROUS_COMMANDS:
        if d in cmd_lower:
            raise SandboxBlockError(f"危险命令被禁止: {d}")
    return cmd


def _build_safe_globals(agent) -> Dict[str, Any]:
    import builtins as _builtins
    import requests as _requests
    import bs4 as _bs4
    from pathlib import Path as _Path

    # 基础 builtins 全放行（open / print / 等都不禁）
    # 仅把 __import__ 换成"受限 import"：拦截危险命令模块的同步用法，但不拦能力
    g = {k: v for k, v in _builtins.__dict__.items() if not k.startswith("_")}

    _original_get = _requests.get
    _original_post = _requests.post

    # ---- subprocess：全异步化，agent 写 await subprocess.run(...) ----
    async def _async_subprocess_run(cmd, **kwargs):
        """subprocess.run 的异步版本：丢线程池，不阻塞事件循环"""
        if isinstance(cmd, str):
            _check_dangerous_command(cmd)
        elif isinstance(cmd, list):
            for p in cmd:
                if isinstance(p, str):
                    _check_dangerous_command(p)
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
        timeout = kwargs.pop("timeout", DEFAULT_SUBPROCESS_TIMEOUT)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(subprocess.run, cmd, **kwargs),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            raise SandboxBlockError(f"subprocess.run 超时（{timeout}s）")

    class SubprocessProxy:
        run = staticmethod(_async_subprocess_run)  # 返回 coroutine，必须 await
        PIPE = subprocess.PIPE
        STDOUT = subprocess.STDOUT
        DEVNULL = subprocess.DEVNULL
        TimeoutExpired = subprocess.TimeoutExpired
        CalledProcessError = subprocess.CalledProcessError

        @staticmethod
        async def Popen(*a, **kw):
            raise SandboxBlockError("禁止使用 subprocess.Popen，用 await subprocess.run(...)")

    # ---- requests：打本地服务改走异步，打外部保持同步 ----
    def _safe_get(url, *a, **kw):
        if url.startswith(("http://127.0.0.1", "http://localhost")):
            raise SandboxBlockError(
                "禁止用同步 requests 调本地 API（会阻塞事件循环），用 await fetch_async(url)"
            )
        return _original_get(url, *a, **kw)

    def _safe_post(url, *a, **kw):
        if url.startswith(("http://127.0.0.1", "http://localhost")):
            raise SandboxBlockError(
                "禁止用同步 requests 调本地 API（会阻塞事件循环），用 await post_json(url, payload)"
            )
        return _original_post(url, *a, **kw)

    class SafeRequests:
        get = staticmethod(_safe_get)
        post = staticmethod(_safe_post)

    # ---- run / run_async：run 也异步化 ----
    async def _safe_run(cmd, timeout=DEFAULT_SUBPROCESS_TIMEOUT, cwd=None):
        if timeout > 300:
            raise ValueError(f"Timeout {timeout}s exceeds maximum 300s")
        if isinstance(cmd, str):
            _check_dangerous_command(cmd)
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    subprocess.run,
                    cmd, shell=True, capture_output=True, timeout=timeout,
                    text=True, encoding="utf-8", errors="replace", cwd=cwd,
                ),
                timeout=timeout + 5,
            )
        except asyncio.TimeoutError:
            raise SandboxBlockError(f"run() 超时（{timeout}s）")
        return result.stdout or ""

    async def _safe_run_async(cmd, timeout=DEFAULT_SUBPROCESS_TIMEOUT, cwd=None):
        if timeout > 300:
            raise ValueError(f"Timeout {timeout}s exceeds maximum 300s")
        if isinstance(cmd, str):
            _check_dangerous_command(cmd)
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

    # ---- fetch：同步版本禁内网，异步版本允许内网 ----
    def _safe_fetch(url, timeout=15):
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise SandboxBlockError(f"fetch 只支持 http/https: {url}")
        url_lower = url.lower()
        for b in BLOCKED_FETCH_HOSTS:
            if b in url_lower:
                raise SandboxBlockError(f"fetch 禁止访问: {b}（云元数据地址）")
        if "127.0.0.1" in url_lower or "localhost" in url_lower:
            raise SandboxBlockError(
                "同步 fetch 禁内网（会阻塞）。验证本地页面用 await fetch_async(url)"
            )
        resp = _original_get(url, timeout=timeout, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        resp.raise_for_status()
        if resp.encoding and resp.encoding.lower() != "iso-8859-1":
            resp.encoding = resp.encoding
        else:
            resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text

    async def _safe_fetch_async(url, timeout=15, headers=None, method="GET", json_body=None):
        """异步 HTTP：可访问本地回环（验证自己起的服务/页面用）"""
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise SandboxBlockError(f"fetch_async 只支持 http/https: {url}")
        if "169.254.169.254" in url or "metadata.google.internal" in url.lower():
            raise SandboxBlockError("fetch_async 禁止访问云元数据地址")
        try:
            import aiohttp
        except ImportError:
            raise SandboxBlockError("aiohttp 未安装，packages 里加 'aiohttp'")
        hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        if headers:
            hdrs.update(headers)
        cli_timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=cli_timeout) as sess:
            async with sess.request(method.upper(), url, headers=hdrs, json=json_body) as r:
                text = await r.text()

                # ★ dict 子类：加 json() 方法，模型写 res.json() 也能用
                class _FetchResult(dict):
                    def json(self):
                        return json.loads(self.get("text") or "{}")
                    @property
                    def ok(self):
                        return 200 <= self.get("status", 0) < 300

                return _FetchResult(
                    status=r.status,
                    text=text,
                    url=str(r.url),
                )

    async def _safe_post_json(url, payload=None, timeout=15, headers=None):
        res = await _safe_fetch_async(
            url, timeout=timeout, headers=headers, method="POST", json_body=payload or {}
        )
        try:
            data = json.loads(res["text"])
        except Exception:
            data = None
        return {"status": res["status"], "json": data, "text": res["text"]}

    # ---- os：只禁 chdir，其他全放行 ----
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
        "requests": SafeRequests,
        "BeautifulSoup": _bs4.BeautifulSoup,
        "re": re,
        "run": _safe_run,
        "run_async": _safe_run_async,
        "fetch": _safe_fetch,
        "fetch_async": _safe_fetch_async,
        "post_json": _safe_post_json,
        "subprocess": SubprocessProxy,
    })

    return g


# ============================================================
# 执行隔离：整个 execute 丢专用线程池，同步阻塞也挂不到主循环
# ============================================================

def _run_execute_sync(execute_fn, envelop, agent):
    """在独立线程里跑 execute（用于隔离同步阻塞）"""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(execute_fn(envelop, agent))
    finally:
        try:
            loop.close()
        except Exception:
            pass


async def _run_execute_isolated(execute_fn, envelop, agent, timeout):
    """把 execute 丢专用线程池，超时主循环脱身。

    注意：线程无法强杀（Python 限制），超时后线程仍在后台跑到自然结束。
    但用专用池（max_workers=2）保证：
      - 不占默认池，不影响其他插件
      - 最多 2 个并发，第 3 个排队（不会无限起线程）
    """
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(
        _CODE_EXECUTOR, _run_execute_sync, execute_fn, envelop, agent
    )
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        # 超时：主循环脱身，但线程仍在跑
        # 这里主动记一条 warn，方便排查「卡住」是不是超时线程占池
        try:
            logger.warning(
                f"[code_runner] 执行超时 {timeout}s，线程仍在后台运行（线程池 2 并发）"
            )
        except Exception:
            pass
        raise SandboxBlockError(
            f"执行超时（{timeout}s）。代码可能仍在后台线程运行，但主服务已脱身"
        )


# ============================================================
# 执行器
# ============================================================

async def _execute_code(code: str, agent, timeout_s: int = DEFAULT_TIMEOUT_SECONDS) -> Dict[str, Any]:
    """执行代码，返回 {ok, data/error, error_category}"""

    try:
        Path("data/workspace").mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    # 1. AST 预检（只查结构合法性）
    ast_err = _ast_pre_check(code)
    if ast_err:
        return {
            "ok": False,
            "error": f"【错误分类】LLM_CODE\n【一句话】AST 预检失败\n【原因】{ast_err}",
            "error_category": "LLM_CODE",
            "env": _failure_env(),
        }

    # 2. 构建沙箱
    try:
        safe_globals = _build_safe_globals(agent)
    except Exception as e:
        return {
            "ok": False,
            "error": f"【错误分类】RUNTIME_INTERNAL\n【一句话】构建沙箱失败\n【原因】{type(e).__name__}: {e}",
            "error_category": "RUNTIME_INTERNAL",
            "env": _failure_env(),
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
            "env": _failure_env(),
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"【错误分类】LLM_CODE\n【一句话】代码加载失败\n【原因】{type(e).__name__}: {e}",
            "error_category": "LLM_CODE",
            "env": _failure_env(),
        }

    # 4. 检查 execute
    execute_fn = local_ns.get("execute")
    if execute_fn is None:
        return {
            "ok": False,
            "error": "【错误分类】LLM_CODE\n【一句话】找不到 execute 函数\n【原因】必须定义 async def execute(envelop, agent)",
            "error_category": "LLM_CODE",
            "env": _failure_env(),
        }
    if not asyncio.iscoroutinefunction(execute_fn):
        return {
            "ok": False,
            "error": "【错误分类】LLM_CODE\n【一句话】execute 不是 async 函数\n【原因】必须定义为 async def",
            "error_category": "LLM_CODE",
            "env": _failure_env(),
        }

    # 5. 执行（隔离到专用线程池，同步阻塞也挂不到主循环）
    envelop = core.Envelop(payload={"state": {}})
    _t0 = time.time()

    # ★ 若专用池被占满（2 个都在跑），本次会排队；提前打日志让前端/日志能看出来
    try:
        _busy = _CODE_EXECUTOR._work_queue.qsize() if hasattr(_CODE_EXECUTOR, "_work_queue") else 0
        if _busy >= 2:
            logger.warning(
                f"[code_runner] 线程池繁忙（排队 {_busy} 个），本次执行需等待"
            )
    except Exception:
        pass

    try:
        ret = await _run_execute_isolated(
            execute_fn, envelop, agent, timeout_s
        )
        logger.info(f"[code_runner] 执行完成 耗时={time.time()-_t0:.1f}s")
    except SandboxBlockError as e:
        logger.warning(
            f"[code_runner] 沙箱拦截/超时 耗时={time.time()-_t0:.1f}s 原因={e}"
        )
        _env = _get_env_summary()
        _env["exec_timeout_s"] = timeout_s
        _env["exec_timeout_default"] = DEFAULT_TIMEOUT_SECONDS
        return {
            "ok": False,
            "error": f"【错误分类】SANDBOX_BLOCK\n【一句话】沙箱拦截\n【原因】{e}",
            "error_category": "SANDBOX_BLOCK",
            "env": _env,
        }
    except Exception as e:
        tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        _env = _get_env_summary()
        _env["exec_timeout_s"] = timeout_s
        _env["exec_timeout_default"] = DEFAULT_TIMEOUT_SECONDS
        return {
            "ok": False,
            "error": f"【错误分类】LLM_CODE\n【一句话】运行时异常\n【原因】{type(e).__name__}: {e}\n【堆栈】\n{tb}",
            "error_category": "LLM_CODE",
            "env": _env,
        }

    # 6. 返回值校验
    if not isinstance(ret, dict):
        return {
            "ok": False,
            "error": f"【错误分类】LLM_CODE\n【一句话】返回值不是字典\n【原因】必须 return {{'data': ...}}，实际是 {type(ret).__name__}",
            "error_category": "LLM_CODE",
            "env": _failure_env(),
        }
    if "data" not in ret:
        return {
            "ok": False,
            "error": "【错误分类】LLM_CODE\n【一句话】返回值缺少 data 字段\n【原因】必须 return {'data': ...}",
            "error_category": "LLM_CODE",
            "env": _failure_env(),
        }

    env = _get_env_summary()
    env["exec_timeout_s"] = timeout_s                     # ★ 本次实际超时（LLM 应读这个）
    env["exec_timeout_default"] = DEFAULT_TIMEOUT_SECONDS  # 不传 timeout_s 时的默认值
    return {"ok": True, "data": ret["data"], "env": env}


# ============================================================
# 插件入口
# ============================================================


async def execute(envelop, agent):
    code = envelop.payload.get("code", "")
    packages = envelop.payload.get("packages", [])
    timeout_s = envelop.payload.get("timeout_s")

    if not code:
        envelop.payload = {"ok": False, "error": "缺少 code 参数"}
        return envelop

    # ★ timeout_s 校验：默认 60，范围 1-MAX_TIMEOUT_SECONDS(300)
    if timeout_s is None:
        timeout_s = DEFAULT_TIMEOUT_SECONDS
    else:
        try:
            timeout_s = int(timeout_s)
        except (TypeError, ValueError):
            envelop.payload = {
                "ok": False,
                "error": (
                    "【错误分类】LLM_CODE\n【一句话】timeout_s 参数格式错误\n"
                    f"【原因】timeout_s 必须是整数，不能是 {type(timeout_s).__name__}"
                ),
                "error_category": "LLM_CODE",
                "env": _failure_env(),
            }
            return envelop
        if timeout_s < 1 or timeout_s > MAX_TIMEOUT_SECONDS:
            envelop.payload = {
                "ok": False,
                "error": (
                    "【错误分类】LLM_CODE\n【一句话】timeout_s 超出范围\n"
                    f"【原因】timeout_s 必须在 1-{MAX_TIMEOUT_SECONDS} 之间，实际是 {timeout_s}"
                ),
                "error_category": "LLM_CODE",
                "env": _failure_env(),
            }
            return envelop

    if not isinstance(packages, list):
        if packages:
            # 传了非 list（dict/str）→ 明确报错，别静默吞
            envelop.payload = {
                "ok": False,
                "error": (
                    "【错误分类】LLM_CODE\n【一句话】packages 参数格式错误\n"
                    "【原因】packages 必须是数组，如 [\"pandas\", \"requests\"]，"
                    f"不能是 {type(packages).__name__}"
                ),
                "error_category": "LLM_CODE",
                "env": _failure_env(),
            }
            return envelop
        packages = []

    code = _strip_code_wrapper(code)

    if not code:
        envelop.payload = {"ok": False, "error": "代码为空"}
        return envelop

    if packages:
        try:
            await ensure_packages(packages)
        except Exception as e:
            envelop.payload = {
                "ok": False,
                "error": f"【错误分类】DEPENDENCY\n【一句话】依赖安装失败\n【原因】{e}",
                "error_category": "DEPENDENCY",
                "env": _failure_env(),
            }
            return envelop

    result = await _execute_code(code, agent, timeout_s)
    if 'agent.llm' in code and not getattr(agent, 'llm', None):
        result['llm_notice'] = ('引擎未配置模型：aicp.yaml 缺 models.providers，agent.llm 为 None，'
                                'LLM 调用不会生效，请提示用户先配置模型')
    envelop.payload = result
    return envelop


def help():
    return {
        "route": "builtins/tools/code_runner",
        "description": "执行传入的 Python 代码，不调 LLM，不生成代码",
        "input": {
            "code": "Python 代码文本，必须定义 async def execute(envelop, agent)，必须 return {'data': ...}",
            "packages": "依赖列表（可选），如 ['pandas', 'requests']",
            "timeout_s": "本次执行超时秒数（可选），默认 60，最大 300。pip install / 大文件下载给 180-300；纯计算给 15-30；调外部 API 给 30-60",
        },
        "output": {
            "ok": "是否成功",
            "data": "成功时的结果",
            "error": "失败时的错误描述",
            "error_category": "LLM_CODE / SANDBOX_BLOCK / RUNTIME_INTERNAL / DEPENDENCY",
        },
        "sandbox_notes": {
            "import": "import requests / subprocess / urllib / http 等都能用，不拦 import",
            "subprocess": "subprocess.run 是 async，必须 await；底层丢线程池，不阻塞事件循环",
            "run": "run() 是 async，必须 await；shell=True 也走线程池",
            "run_async": "run_async() 是 async；用 create_subprocess_exec，最推荐",
            "fetch": "同步 fetch 禁内网；验证本地页面用 await fetch_async(url)",
            "fetch_async": "async，可访问 127.0.0.1/localhost，验证服务用这个",
            "post_json": "async，POST JSON 并解析响应",
            "requests": "同步 requests 打本地会被拦，用 await fetch_async / post_json；打外部可用",
            "execute 隔离": "execute 整体跑在专用线程池（2 并发），同步阻塞也挂不到主事件循环",
            "危险命令": "rm -rf /、format、mkfs 等会被拦",
            "eval/exec/__import__": "禁止（防止绕过沙箱构造）",
        },
    }
