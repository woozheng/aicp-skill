"""os/_service_restart — 长驻服务的通用「重启」与「热重载自愈」工具

解决的问题
----------
AICP 里所有长驻服务（HTTP 网关 / MCP server / WebSocket / 文件接收器）都用
aiohttp 起在一个常驻 app 上。热重载会 exec 出新模块对象并重绑 core.plugins，
但**已在跑的路由闭包仍持有启动那一刻的模块 globals** —— 结果是：

    改了插件代码 → 热重载成功（日志显示 Hot reloaded）→ 行为却完全没变

只有重启引擎才能生效。对「改网关一个常量」这种小事来说太重。

本模块提供两件事：

1. `restart_service(agent, route, ...)` —— 优雅重启一个服务
   （先 cleanup 旧 runner 释放端口，再用新代码 START）。
2. `ensure_hot_reload_watch(agent, route, ...)` —— 起一个后台 watcher，
   模块一被热重载就自动重启服务（改完代码 ~2 秒生效，不用重启引擎）。

用法（在各服务插件里）：

    from plugins.os import _service_restart as sr   # 或按相对路径 import

    async def execute(envelop, agent):
        action = envelop.payload.get("action", "")
        if action == "restart":
            ok = await sr.restart_service(agent, "os/_gateway")
            envelop.payload = {"ok": ok}
            return envelop
        ...

约束
----
- 必须在**主事件循环**里调用（code_runner 是独立 loop，会报
  "attached to a different loop"）。
- 端口释放靠 runner.cleanup()；若 runner 已被丢弃（老代码没存 runner），
  需要用 `kill_port_holders()` 兜底。
"""

import asyncio
import gc
import sys

import core


# ============================================================
# 端口兜底：找出占着某端口的孤儿 socket 并关掉
# ============================================================

def find_port_servers(port: int):
    """找出所有持有指定端口的 aiohttp Server 对象。

    已启动但没人保存 runner 的孤儿服务不在 gc.get_objects() 的 AppRunner 列表里
    （aiohttp 内部持有），只能从 Server._sockets 反查。
    """
    out = []
    for obj in gc.get_objects():
        try:
            if type(obj).__name__ != "Server":
                continue
            for s in (getattr(obj, "_sockets", None) or []):
                try:
                    if s.getsockname()[1] == port:
                        out.append((obj, s))
                        break
                except Exception:
                    pass
        except Exception:
            pass
    return out


async def kill_port_holders(port: int) -> int:
    """关掉占着该端口的孤儿 socket，返回关闭了几个。

    只在没有可 cleanup 的 runner 时用（老代码遗留的孤儿服务）。
    """
    n = 0
    for srv, sock in find_port_servers(port):
        try:
            sock.close()
            n += 1
        except Exception:
            pass
    return n


# ============================================================
# 通用重启
# ============================================================

async def restart_service(agent, route: str, port=None, host=None,
                          stop_action: str = "STOP",
                          start_payload=None,
                          runner_attr: str = None,
                          extra_stop: bool = True,
                           stop_timeout: float = 5.0) -> bool:
    """优雅重启一个长驻服务。

    Args:
        agent: 引擎 agent
        route: 服务路由（如 "os/_gateway"），用于从 core.plugins 找 execute
        port: 端口（用于端口兜底）
        host: 主机
        stop_action: 停止用的 action 名（默认 "STOP"）
        start_payload: START 时的 payload；None 表示用 {action:START, host, port}
        runner_attr: runner 存在 agent 上的属性名（如 "_gateway_runner"）
        extra_stop: 端口被占死时是否强杀孤儿 socket

    Returns:
        True = 重启成功
    """
    fn = core.plugins.get(route)
    if not fn:
        return False

    # 1) 停旧的
    #    ★ 注意：STOP 可能会 cancel 并 await watcher task。如果这次 restart 是
    #      watcher 自己触发的，await 自己 = 死锁（task 永远等不到自己结束）。
    #      所以先把 watcher task 摘下来（cancel 但不 await），STOP 之后再让它自己结束。
    watch_attr = "_service_watch_" + route.replace("/", "_")
    watcher = getattr(agent, watch_attr, None)
    if watcher is not None and not watcher.done():
        try:
            watcher.cancel()
        except Exception:
            pass
        setattr(agent, watch_attr, None)

    # ★ STOP 加超时：若这次 restart 是「服务自己收到请求后触发的」，
    #   STOP 里的 runner.cleanup() 会等待在途请求结束 —— 而那个在途请求
    #   正是发起 restart 的这个调用 → 死锁（实测 MCP server 会挂死）。
    #   超时后放弃等待，走端口兜底把旧服务挤掉。
    try:
        await asyncio.wait_for(
            fn(core.Envelop(
                sender=route, receiver=route, intent="API_CALL",
                payload={"action": stop_action},
            ), agent),
            timeout=stop_timeout,
        )
    except asyncio.TimeoutError:
        pass
    except Exception:
        pass

    # 2) runner cleanup 不彻底 / 老代码没存 runner / STOP 卡死
    #    → 端口还被占着 → 强杀占着该端口的 socket
    if port and extra_stop:
        await asyncio.sleep(0.3)
        if await kill_port_holders(port):
            await asyncio.sleep(0.5)

    # 3) 用新代码起
    payload = start_payload if start_payload is not None else {"action": "START"}
    if host is not None:
        payload.setdefault("host", host)
    if port is not None:
        payload.setdefault("port", port)

    try:
        res = await fn(core.Envelop(
            sender=route, receiver=route, intent="API_CALL", payload=payload,
        ), agent)
    except Exception:
        return False

    if res is None:
        return False
    p = res.payload or {}
    if p.get("error"):
        return False
    return True


async def restart_service_and_watch(agent, route: str, **kw):
    """重启服务，并重新挂上热重载自愈 watcher。"""
    ok = await restart_service(agent, route, **kw)
    ensure_hot_reload_watch(
        agent, route,
        port=kw.get("port"), host=kw.get("host"),
        start_payload=kw.get("start_payload"),
    )
    return ok


# ============================================================
# 热重载自愈 watcher
# ============================================================

def ensure_hot_reload_watch(agent, route: str, port=None, host=None,
                            start_payload=None,
                            watch_attr: str = None,
                            interval: float = 2.0) -> bool:
    """起一个后台 watcher：模块被热重载后自动重启服务。

    只起一次（同一个 agent 上同一路由不会重复起）。

    Args:
        watch_attr: watcher task 存在 agent 上的属性名；None 时用 "_service_watch_" + route
    """
    watch_attr = watch_attr or ("_service_watch_" + route.replace("/", "_"))
    existing = getattr(agent, watch_attr, None)
    if existing is not None:
        # 已结束（done）的旧 task 要清掉，否则「已存在就不起新的」会误判
        if not existing.done():
            return False
        setattr(agent, watch_attr, None)

    module_name = "plugin_" + route.replace("/", "_")

    async def _watch():
        me = sys.modules.get(module_name)
        my_id = id(me) if me is not None else None
        while True:
            try:
                await asyncio.sleep(interval)
                cur = sys.modules.get(module_name)
                if cur is None or (my_id is not None and id(cur) == my_id):
                    continue

                log = getattr(agent, "log", None)
                if log:
                    log.info(f"[service] 检测到 {route} 已热重载，正在自动重启")

                ok = await restart_service(
                    agent, route, port=port, host=host,
                    start_payload=start_payload,
                )
                if log:
                    log.info(f"[service] {route} 自动重启 {'成功' if ok else '失败'}")

                # ★ 必须重新挂上 watcher：
                #   restart_service 会 cancel 掉自己（防自死锁）并把 agent 上的
                #   watch_attr 置 None。这里如果直接 return，之后就再没有 watcher，
                #   下次热重载不会自动重启（实测：网关改一次后就不再自愈了）。
                if not getattr(agent, watch_attr, None):
                    ensure_hot_reload_watch(
                        agent, route, port=port, host=host,
                        start_payload=start_payload, watch_attr=watch_attr,
                        interval=interval,
                    )
                return
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    setattr(agent, watch_attr, asyncio.create_task(_watch()))
    return True