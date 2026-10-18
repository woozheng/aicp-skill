"""AICP 本地引擎 · 纯 Server 启动器"""
import asyncio
import logging
import os
import sys
import threading
from pathlib import Path
import subprocess as _subprocess

_original_run = _subprocess.run

def _utf8_run(*args, **kwargs):
    kwargs.setdefault('encoding', 'utf-8')
    kwargs.setdefault('errors', 'ignore')
    return _original_run(*args, **kwargs)

_subprocess.run = _utf8_run

_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from logging.handlers import RotatingFileHandler
from core import Envelop, Agent, route
from runtime._config import load_config
from runtime._loader import load_all_plugins, start_hot_reload_watcher


def setup_logging():
    log_dir = Path("data")
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("aicp")
    logger.setLevel(logging.DEBUG)

    fh = RotatingFileHandler(
        log_dir / "gateway.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    ))
    logger.addHandler(fh)

    if sys.stdout and hasattr(sys.stdout, 'write'):
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        ch.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(ch)

    return logger


def _load_app_config(app_dir: Path) -> dict | None:
    app_yaml = app_dir / "app.yaml"
    if not app_yaml.exists():
        return {"name": app_dir.name, "auto_start": True, "init_action": "init"}
    try:
        import yaml
        with open(app_yaml, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
            cfg.setdefault("name", app_dir.name)
            cfg.setdefault("auto_start", True)
            cfg.setdefault("init_action", "init")
            return cfg
    except Exception:
        return {"name": app_dir.name, "auto_start": True, "init_action": "init"}


async def main():

    sys.stdout.reconfigure(encoding='utf-8', errors='ignore')
    sys.stderr.reconfigure(encoding='utf-8', errors='ignore')
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    os.environ['PYTHONUTF8'] = '1'
    logger = setup_logging()
    config = load_config()

    port = config.get("port", 9000)
    host = config.get("host", "127.0.0.1")

    mcp_cfg = config.get("mcp", {})
    mcp_enabled = mcp_cfg.get("enabled", True)
    mcp_host = mcp_cfg.get("host", host)
    mcp_port = mcp_cfg.get("port", 9100)

    logger.info("=" * 50)
    logger.info(f"  AICP Engine · http://{host}:{port}")
    if mcp_enabled:
        logger.info(f"  MCP       · http://{mcp_host}:{mcp_port}/mcp")
    logger.info("=" * 50)

    # LLM（保留，插件内部业务调用用；不加载 AICP_LLM）
    llm = None
    if config.get("models", {}).get("providers"):
        try:
            from runtime._llm import LLM
            llm = LLM(config)
            logger.info(f"LLM: {llm.default_model}")
        except Exception as e:
            logger.warning(f"LLM init failed: {e}")

    stop_event = asyncio.Event()

    def shutdown():
        if not stop_event.is_set():
            logger.info("Shutting down...")
            stop_event.set()

    loop = asyncio.get_event_loop()

    from runtime._system import SystemServer
    system = SystemServer(loop, route, config=config)

    # Agent
    agent = Agent()
    agent.config = config
    agent.loop = loop
    agent.log = logger
    agent.data_dir = Path("data")
    agent.base_url = f"http://127.0.0.1:{port}"
    agent.llm = llm
    agent.Envelop = Envelop
    agent.system = system
    system.bind(agent)

    # ---------- 可视化 worker ----------
    if getattr(system, '_viz_enabled', False):
        await system.start_visualizer()
        logger.info(f"Visualizer: enabled on channel {system._viz_channel}")
    else:
        logger.debug("Visualizer: disabled")

    import core
    count = load_all_plugins()
    logger.info(f"Plugins: {count} loaded")

    asyncio.create_task(start_hot_reload_watcher(agent=agent))

    # ---------- 恢复定时任务 ----------
    cron_plugin = core.plugins.get("os/_cron")
    if cron_plugin:
        try:
            cron_env = Envelop(
                sender="__main__",
                receiver="os/_cron",
                payload={"action": "restore"}
            )
            cron_result = await cron_plugin(cron_env, agent)
            if cron_result and cron_result.payload.get("ok"):
                restored_count = cron_result.payload.get("restored", 0)
                if restored_count > 0:
                    logger.info(f"Cron: restored {restored_count} tasks")
        except Exception as e:
            logger.warning(f"Cron restore failed: {e}")

    # ---------- HTTP Gateway ----------
    gateway = core.plugins.get("os/_gateway")
    if not gateway:
        logger.error("os/_gateway not found!")
        return

    env = Envelop(
        sender="__main__",
        receiver="os/_gateway",
        intent="START",
        payload={"port": port, "host": host},
    )

    try:
        result = await gateway(env, agent)
        if result and result.payload.get("status") == "listening":
            logger.info(f"Gateway: http://{host}:{port}")
        else:
            logger.error("Gateway start failed")
            return
    except Exception as e:
        logger.error(f"Gateway start failed: {e}")
        return

    # ---------- MCP Server ----------
    mcp_gateway = None
    if mcp_enabled:
        mcp_gateway = core.plugins.get("os/_mcp_server")
        if mcp_gateway:
            mcp_env = Envelop(
                sender="__main__",
                receiver="os/_mcp_server",
                intent="START",
                payload={"port": mcp_port, "host": mcp_host},
            )
            try:
                mcp_result = await mcp_gateway(mcp_env, agent)
                if mcp_result and mcp_result.payload.get("status") == "listening":
                    logger.info(f"MCP: http://{mcp_host}:{mcp_port}/mcp")
                else:
                    logger.error("MCP start failed")
            except Exception as e:
                logger.error(f"MCP start failed: {e}")
        else:
            logger.warning("os/_mcp_server not found, skipped")

    # ---------- WebSocket ----------
    ws_port = port + 1
    ws_runner = None
    try:
        from plugins.os._websocket import start_websocket_server
        ws_runner = await start_websocket_server(agent, host=host, port=ws_port)
        logger.info(f"WebSocket: ws://{host}:{ws_port}/ws")
    except Exception as e:
        logger.warning(f"WebSocket start failed: {e}")

    # ---------- FileReceiver ----------
    file_port = port + 2
    file_runner = None
    try:
        from plugins.os._file_receiver import start_file_receiver
        file_runner = await start_file_receiver(agent, host=host, port=file_port)
        logger.info(f"FileReceiver: http://{host}:{file_port}/upload")
    except Exception as e:
        logger.warning(f"FileReceiver start failed: {e}")

    # ---------- Applications ----------
    for scan_dir in ["plugins/builtins", "plugins/applications"]:
        apps_dir = Path(scan_dir)
        if not apps_dir.exists():
            continue
        for app_dir in sorted(apps_dir.iterdir()):
            if not app_dir.is_dir():
                continue
            if app_dir.name.startswith(".") or app_dir.name.startswith("_"):
                continue
            if app_dir.name in ("mk", "studio", "shell", "screen_probe", "newbula_plus"):
                continue

            init_file = app_dir / "_init.py"
            if not init_file.exists():
                continue

            app_config = _load_app_config(app_dir)
            if not app_config.get("auto_start"):
                continue

            app_route = f"{scan_dir.replace('plugins/', '')}/{app_dir.name}/_init"
            init_plugin = core.plugins.get(app_route)
            if not init_plugin:
                continue

            init_env = Envelop(
                sender="__main__",
                receiver=app_route,
                payload={"action": app_config.get("init_action", "init")},
            )
            try:
                await init_plugin(init_env, agent)
                logger.info(f"App: {app_config.get('name', app_dir.name)}")
            except Exception as e:
                logger.warning(f"App {app_config.get('name', app_dir.name)} failed: {e}")

    # ---------- 等停止 ----------
    import time

    def listen_stdin():
        if sys.platform == "win32":
            import msvcrt
            def _check():
                if msvcrt.kbhit():
                    return msvcrt.getwch()
                return None
        else:
            import select
            def _check():
                r, _, _ = select.select([sys.stdin], [], [], 0)
                if r:
                    return sys.stdin.read(1)
                return None

        while not stop_event.is_set():
            ch = _check()
            if ch and ch.lower() in ("q", "Q"):
                shutdown()
            time.sleep(0.1)

    threading.Thread(target=listen_stdin, daemon=True).start()

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        shutdown()

    # ================================================================
    # 停止
    # ================================================================
    logger.info("Stopping services...")

    # 1. 停止 MCP
    if mcp_gateway:
        mcp_stop_env = Envelop(
            sender="__main__",
            receiver="os/_mcp_server",
            intent="STOP",
            payload={"action": "STOP"},
        )
        try:
            await mcp_gateway(mcp_stop_env, agent)
        except Exception:
            pass

    # 2. 停止 Gateway
    stop_env = Envelop(
        sender="__main__",
        receiver="os/_gateway",
        intent="STOP",
        payload={"action": "STOP"},
    )
    try:
        await gateway(stop_env, agent)
    except Exception:
        pass

    # 3. 停止 WebSocket
    if ws_runner:
        try:
            if hasattr(agent, 'ws_gateway'):
                gw = agent.ws_gateway
                for channel_id in list(gw._channels.keys()):
                    for ws in list(gw._channels.get(channel_id, set())):
                        try:
                            await ws.close(code=1001, message=b"Server shutting down")
                        except Exception:
                            pass
            await ws_runner.cleanup()
        except Exception:
            pass

    # 4. 停止 FileReceiver
    if file_runner:
        try:
            await file_runner.cleanup()
        except Exception:
            pass

    # 5. 清理 pool
    pool = core.plugins.get("os/_pool")
    if pool:
        try:
            pool_env = Envelop(
                sender="__main__",
                receiver="os/_pool",
                payload={"action": "_cleanup"},
            )
            await pool(pool_env, agent)
        except Exception:
            pass

    logger.info("Goodbye")


if __name__ == "__main__":
    asyncio.run(main())
