# plugins/builtins/os/restart.py
"""系统重启插件 — 跨平台通用版（稳定版）

稳定措施：
1. 重启前 flush 所有 Flow（防 flow.jsonl 写一半）—— 走 agent.engine.flows + save_locked
2. 重启前等 processing locks 释放（最多 5 秒，超时就强杀）
   —— 走 runtime.engine.engine._processing_locks
3. 重启前清 __pycache__（防新进程 import 到旧 .pyc）
4. 重启前写 data/.restart_marker（让新进程知道"上次是重启，不是崩溃"）
5. os.execv 同进程重启（Windows/Unix 都用 execv，最干净）

注：旧版有「cancel _STREAM_FLUSH_TASKS」一步，新引擎没有跨轮 stream buffer
（Flow 即时追加写、Push 的后台 task 只负责 WS 推送），该步骤已删除。
"""
import core
import sys
import os
import asyncio
import subprocess
import json
import time
import shutil
from pathlib import Path


RESTART_MARKER = Path("data/.restart_marker")
RESTART_WAIT_MAX = 5.0


def _get_restart_cmd() -> list:
    """智能识别当前启动方式，返回正确的重启命令。"""
    argv = sys.argv[:]
    if not argv:
        return None

    argv0 = argv[0]
    python_exe = sys.executable

    main_module = sys.modules.get('__main__')
    if main_module and getattr(main_module, '__package__', None):
        pkg = main_module.__package__
        return [python_exe, '-m', pkg] + argv[1:]

    argv0_path = Path(argv0)
    if argv0_path.name == '__main__.py' and argv0_path.parent.name:
        try:
            src = argv0_path.read_text(encoding='utf-8', errors='ignore')
            if 'from .' in src or 'from ..' in src:
                pkg_name = argv0_path.parent.name
                parent_dir = str(argv0_path.parent.parent.resolve())
                if parent_dir in [str(Path(p).resolve()) for p in sys.path if p]:
                    return [python_exe, '-m', pkg_name] + argv[1:]
        except Exception:
            pass

    if argv0.endswith('.py'):
        return [python_exe] + argv

    if os.path.isfile(argv0) or argv0.endswith('.exe'):
        return argv

    if os.path.exists(argv0):
        return [python_exe] + argv

    return argv


def _detect_managed() -> str:
    if os.environ.get('INVOCATION_ID') or os.environ.get('JOURNAL_STREAM'):
        return "systemd"
    if os.environ.get('SUPERVISOR_ENABLED') or os.environ.get('SUPERVISOR_PROCESS_NAME'):
        return "supervisor"
    if os.path.exists('/.dockerenv'):
        return "docker"
    if os.environ.get('PM2_HOME'):
        return "pm2"
    if os.environ.get('AICP_RESTART_CMD'):
        return "custom_env"
    return ""


async def _flush_all_flows(agent):
    """flush 所有 Flow，防 flow.jsonl 写一半。"""
    engine = getattr(agent, "engine", None)
    if engine is None:
        agent.log.warning("[restart] engine 未就绪，跳过 flow flush")
        return
    flows = getattr(engine, "flows", {}) or {}
    for key, flow in list(flows.items()):
        try:
            save = getattr(flow, "save_locked", None)
            if save is None:
                agent.log.warning(f"[restart] flow {key} 无 save_locked，跳过")
                continue
            await save()
        except Exception as e:
            agent.log.warning(f"[restart] flush flow {key} 失败: {e}")


async def _wait_processing_locks(agent, max_wait: float = RESTART_WAIT_MAX):
    """等 processing locks 释放，最多等 max_wait 秒。"""
    try:
        from runtime.engine.engine import _processing_locks
    except Exception as e:
        agent.log.warning(f"[restart] 读 processing locks 失败，跳过等待: {e}")
        return
    if not _processing_locks:
        return
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if all(not lk.locked() for lk in _processing_locks.values()):
            return
        await asyncio.sleep(0.2)
    agent.log.warning(f"[restart] 等待 processing locks 超时 {max_wait}s，继续重启")


def _clean_pycache():
    try:
        root = Path(".")
        for p in root.rglob("__pycache__"):
            shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass


def _write_restart_marker():
    try:
        RESTART_MARKER.parent.mkdir(parents=True, exist_ok=True)
        RESTART_MARKER.write_text(
            json.dumps({"ts": time.time(), "reason": "restart"}),
            encoding="utf-8",
        )
    except Exception:
        pass


def _spawn_and_exit(cmd: list, agent):
    if not cmd:
        agent.log.error("[restart] 无法识别启动方式，放弃重启")
        return
    agent.log.info(f"[restart] 重启命令: {cmd}")
    try:
        if sys.platform == 'win32':
            try:
                os.execv(cmd[0], cmd)
            except OSError as e:
                agent.log.warning(f"[restart] os.execv 失败({e})，改用 Popen")
                subprocess.Popen(
                    cmd,
                    cwd=os.getcwd(),
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                    close_fds=True,
                    env=os.environ.copy(),
                )
                agent.log.info("[restart] 新进程已启动，退出当前进程")
        else:
            os.execv(cmd[0], cmd)
    except Exception as e:
        agent.log.error(f"[restart] 启动新进程失败: {e}")
    os._exit(0)


async def execute(envelop, agent):
    action = envelop.payload.get("action", "restart")

    if action == "restart":
        agent.log.info("[restart] 收到重启请求，3秒后重启...")
        envelop.payload = {"ok": True, "message": "系统将在3秒后重启"}

        async def _delayed_restart():
            await asyncio.sleep(3)
            agent.log.info("[restart] 正在重启...")

            _write_restart_marker()
            await _flush_all_flows(agent)
            await _wait_processing_locks(agent)
            _clean_pycache()

            is_managed = _detect_managed()
            if is_managed:
                agent.log.info(f"[restart] 托管方式: {is_managed}，退出")
                os._exit(0)
                return

            cmd = _get_restart_cmd()
            if not cmd:
                agent.log.error("[restart] 无法识别启动方式，放弃重启")
                return
            agent.log.info(f"[restart] execv: {cmd}")
            _spawn_and_exit(cmd, agent)

        asyncio.create_task(_delayed_restart())
        return envelop

    envelop.payload = {"error": f"Unknown action: {action}"}
    return envelop


def help():
    return {
        "route": "os/restart",
        "description": "系统重启插件 — 跨平台通用版（稳定版，多 agent 适配）",
        "actions": {
            "restart": "重启系统（3秒后）",
        }
    }
