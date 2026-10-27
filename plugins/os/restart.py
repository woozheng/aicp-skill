# plugins/builtins/os/restart.py
"""系统重启插件 — 跨平台通用版"""
import core
import sys
import os
import asyncio
import subprocess
import shlex
from pathlib import Path


def _get_restart_cmd() -> list:
    """
    智能识别当前启动方式，返回正确的重启命令。
    
    支持：
    - python -m package（相对导入必须用 -m 方式）
    - python xxx.py
    - uvicorn xxx:app
    - gunicorn xxx:app
    - 被 systemd/supervisor/docker 托管
    """
    argv = sys.argv[:]
    
    # 空 argv 保护
    if not argv:
        return None
    
    argv0 = argv[0]
    python_exe = sys.executable
    
    # ============================================================
    # 场景 0：python -m package 启动
    # 主模块有 __package__ 且非空 → 必须用 -m 方式重启，否则相对导入炸
    # ============================================================
    main_module = sys.modules.get('__main__')
    if main_module and getattr(main_module, '__package__', None):
        pkg = main_module.__package__
        return [python_exe, '-m', pkg] + argv[1:]

    # ============================================================
    # 场景 0.5：python path/to/__main__.py 直接跑包入口
    # __package__ 为空，但文件是某个包的 __main__.py，且用了相对导入
    # → 推断包名，用 -m 方式重启
    # ============================================================
    argv0_path = Path(argv0)
    if argv0_path.name == '__main__.py' and argv0_path.parent.name:
        # 读首屏，看有没有 from .xxx 相对导入
        try:
            src = argv0_path.read_text(encoding='utf-8', errors='ignore')
            if 'from .' in src or 'from ..' in src:
                pkg_name = argv0_path.parent.name
                # 确认父目录在 sys.path 里（即 cwd 是项目根）
                parent_dir = str(argv0_path.parent.parent.resolve())
                if parent_dir in [str(Path(p).resolve()) for p in sys.path if p]:
                    return [python_exe, '-m', pkg_name] + argv[1:]
        except Exception:
            pass
    
    # ============================================================
    # 场景 1：python xxx.py
    # ============================================================
    if argv0.endswith('.py'):
        # sys.argv = ['main.py', ...]
        # 需要补 python 解释器
        return [python_exe] + argv
    
    # ============================================================
    # 场景 2：python -m xxx
    # 此时 sys.argv[0] 是 -m 后面的模块路径，这种情况少见
    # ============================================================
    # 一般 python -m uvicorn 时，sys.argv[0] 是 uvicorn 的脚本路径
    
    # ============================================================
    # 场景 3：uvicorn / gunicorn / 其他命令行工具
    # sys.argv[0] 是完整脚本路径（如 .../Scripts/uvicorn.exe）
    # ============================================================
    if os.path.isfile(argv0) or argv0.endswith('.exe'):
        return argv
    
    # ============================================================
    # 场景 4：sys.argv[0] 是相对路径或命令名
    # 尝试用 python_exe 启动
    # ============================================================
    if os.path.exists(argv0):
        return [python_exe] + argv
    
    # ============================================================
    # 场景 5：无法识别，直接用原 argv
    # ============================================================
    return argv


def _spawn_and_exit(cmd: list, agent):
    """启动新进程并退出当前进程"""
    if not cmd:
        agent.log.error("[restart] 无法识别启动方式，放弃重启")
        return
    
    agent.log.info(f"[restart] 重启命令: {cmd}")
    
    try:
        if sys.platform == 'win32':
            # Windows：优先 os.execv —— 新进程继承控制台，能看到启动日志
            # 成功则不返回；失败 fallback 到 Popen（不用 DETACHED_PROCESS）
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
            # Unix：execv 替换当前进程，最干净
            os.execv(cmd[0], cmd)
    except Exception as e:
        agent.log.error(f"[restart] 启动新进程失败: {e}")
    
    # 只有 Popen 分支会走到这里（execv 成功不返回）
    os._exit(0)


async def execute(envelop, agent):
    action = envelop.payload.get("action", "restart")
    
    if action == "restart":
        agent.log.info("[restart] 收到重启请求，3秒后重启...")
        
        envelop.payload = {"ok": True, "message": "系统将在3秒后重启"}
        
        async def _delayed_restart():
            await asyncio.sleep(3)
            agent.log.info("[restart] 正在重启...")
            
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


def _detect_managed() -> str:
    """检测是否被进程管理器托管"""
    
    # systemd
    if os.environ.get('INVOCATION_ID') or os.environ.get('JOURNAL_STREAM'):
        return "systemd"
    
    # supervisor
    if os.environ.get('SUPERVISOR_ENABLED') or os.environ.get('SUPERVISOR_PROCESS_NAME'):
        return "supervisor"
    
    # docker
    if os.path.exists('/.dockerenv'):
        return "docker"
    
    # pm2
    if os.environ.get('PM2_HOME'):
        return "pm2"
    
    # 有 RESTART_CMD 环境变量（自定义）
    if os.environ.get('AICP_RESTART_CMD'):
        return "custom_env"
    
    return ""


def help():
    return {
        "route": "os/restart",
        "description": "系统重启插件 — 跨平台通用版",
        "actions": {
            "restart": "重启系统（3秒后）",
        }
    }