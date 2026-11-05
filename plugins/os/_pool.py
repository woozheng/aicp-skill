"""Engine Pool — 跨请求状态管理 · 协议 v3.0 系统插件
其他插件通过 HTTP POST /api/os/_pool 或 agent.system.call() 访问。

⚠ 注意：_pool 是模块级单例，当前默认单进程（__main__.py 直接 asyncio.run）。
   若未来启用多 worker（uvicorn --workers N），同一 engine_id 在不同进程内
   会是不同状态。届时需替换为 Redis / SQLite 等跨进程实现。
"""
import asyncio
import time
from typing import Dict, Optional

DEFAULT_TTL = 3600
GC_INTERVAL = 300


class SubEngine:
    def __init__(self, engine_id: str, config: dict):
        self.id = engine_id
        self.config = config
        self.created_at = time.time()
        self.last_access = time.time()
        self.ttl = config.get("ttl", DEFAULT_TTL)
        # 从 config 初始化 state（排除系统字段）
        self.state: dict = {k: v for k, v in config.items() if k not in ("ttl",)}

    def touch(self):
        self.last_access = time.time()

    def expired(self) -> bool:
        return time.time() - self.last_access > self.ttl

    def time_remaining(self) -> float:
        return max(0, self.ttl - (time.time() - self.last_access))

    async def cleanup(self):
        self.state.clear()


class EnginePool:
    def __init__(self):
        self._engines: Dict[str, SubEngine] = {}
        self._lock = asyncio.Lock()
        self._gc_task: Optional[asyncio.Task] = None

    async def create(self, engine_id: str, config: dict) -> str:
        async with self._lock:
            if engine_id in self._engines:
                self._engines[engine_id].touch()
                return engine_id
            self._engines[engine_id] = SubEngine(engine_id, config)
            return engine_id

    async def get(self, engine_id: str) -> Optional[SubEngine]:
        async with self._lock:
            eng = self._engines.get(engine_id)
            if eng:
                eng.touch()
            return eng

    async def exists(self, engine_id: str) -> bool:
        async with self._lock:
            return engine_id in self._engines

    async def destroy(self, engine_id: str) -> bool:
        async with self._lock:
            if engine_id not in self._engines:
                return False
            eng = self._engines.pop(engine_id)
        # 锁外 cleanup：避免 cleanup 内部阻塞整个池
        await eng.cleanup()
        return True

    # ★ 修复：原同步方法在 GC 并发 pop 时可能 RuntimeError: dictionary changed
    #   size during iteration。改为 async + 加锁 + 先快照。
    async def list_engines(self) -> dict:
        async with self._lock:
            snapshot = list(self._engines.items())
        return {
            eid: {
                "ttl": eng.ttl,
                "expired": eng.expired(),
                "time_remaining": eng.time_remaining(),
            }
            for eid, eng in snapshot
        }

    async def start_gc(self):
        if self._gc_task and not self._gc_task.done():
            return

        async def gc_loop():
            while True:
                await asyncio.sleep(GC_INTERVAL)
                async with self._lock:
                    expired = [eid for eid, eng in self._engines.items() if eng.expired()]
                    # 锁内 pop，但 cleanup 放锁外（见下）
                    popped = [self._engines.pop(eid) for eid in expired]
                for eng in popped:
                    await eng.cleanup()

        self._gc_task = asyncio.create_task(gc_loop())

    async def cleanup_all(self):
        # ★ 修复：cancel GC task 后必须 await 吞掉 CancelledError，
        #   否则退出时打印 "Task was destroyed but it is pending"。
        if self._gc_task and not self._gc_task.done():
            self._gc_task.cancel()
            try:
                await self._gc_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._gc_task = None

        async with self._lock:
            engines = list(self._engines.values())
            self._engines.clear()
        for eng in engines:
            await eng.cleanup()


_pool: Optional[EnginePool] = None


def _resolve_engine_id(payload: dict, action: str) -> str:
    """统一取 engine_id：兼容顶层与 payload.payload 嵌套写法。"""
    eid = payload.get("engine_id", "")
    if not eid:
        inner = payload.get("payload")
        if isinstance(inner, dict):
            eid = inner.get("engine_id", "")
    return str(eid or "").strip()


async def execute(envelop, agent):
    global _pool

    if _pool is None:
        _pool = EnginePool()
        await _pool.start_gc()

    action = envelop.payload.get("action", "")
    engine_id = _resolve_engine_id(envelop.payload, action)

    # ★ 修复：list 不需要 engine_id；其余 action 必须非空
    if not engine_id and action != "list":
        envelop.payload = {"ok": False, "error": "engine_id required"}
        return envelop

    if action == "create":
        config = envelop.payload.get("config", {})
        if not isinstance(config, dict):
            envelop.payload = {"ok": False, "error": "config must be a dict"}
            return envelop
        await _pool.create(engine_id, config)
        eng = await _pool.get(engine_id)
        envelop.payload = {
            "engine_id": engine_id,
            "ttl": eng.ttl if eng else DEFAULT_TTL,
            "time_remaining": eng.time_remaining() if eng else 0,
            "ok": True,
        }

    elif action == "get":
        key = envelop.payload.get("key")
        eng = await _pool.get(engine_id)
        if not eng:
            envelop.payload = {"ok": False, "error": "Engine not found"}
        elif key is not None:
            envelop.payload = {"ok": True, key: eng.state.get(key)}
        else:
            envelop.payload = {"ok": True, "state": dict(eng.state)}

    elif action == "set":
        eng = await _pool.get(engine_id)
        if not eng:
            envelop.payload = {"ok": False, "error": "Engine not found"}
            return envelop

        updates = envelop.payload.get("updates")
        if updates is not None:
            if not isinstance(updates, dict):
                envelop.payload = {"ok": False, "error": "updates must be a dict"}
                return envelop
            eng.state.update(updates)
            envelop.payload = {"ok": True}
            return envelop

        # ★ 修复：空 key 拒绝（原逻辑 key="" 会被当成合法 key 写入）
        key = envelop.payload.get("key")
        if key is None:
            envelop.payload = {"ok": False, "error": "key+value or updates required"}
            return envelop
        if isinstance(key, str) and not key.strip():
            envelop.payload = {"ok": False, "error": "key must be non-empty"}
            return envelop

        eng.state[key] = envelop.payload.get("value")
        envelop.payload = {"ok": True}

    elif action == "exists":
        envelop.payload = {"ok": True, "exists": await _pool.exists(engine_id)}

    elif action == "destroy":
        ok = await _pool.destroy(engine_id)
        envelop.payload = {"ok": ok, "engine_id": engine_id}

    elif action == "list":
        envelop.payload = {"ok": True, "engines": await _pool.list_engines()}

    # ★ 新增：__main__.py 停止阶段用 _cleanup，原实现没有此 action 会落到 unknown
    elif action in ("_cleanup", "cleanup_all"):
        await _pool.cleanup_all()
        envelop.payload = {"ok": True, "cleaned": True}

    else:
        envelop.payload = {"ok": False, "error": f"Unknown action: {action}"}

    return envelop


def help() -> dict:
    return {
        "route": "os/_pool",
        "description": "跨请求状态池：带 TTL 的纯内存 KV 容器，供插件跨调用共享临时状态",
        "actions": {
            "create": {"input": {"engine_id": "str", "config": "dict"}, "output": {"ok": "bool", "ttl": "int"}},
            "get": {"input": {"engine_id": "str", "key?": "str"}, "output": {"ok": "bool", "state": "dict"}},
            "set": {"input": {"engine_id": "str", "key/value 或 updates": "any"}, "output": {"ok": "bool"}},
            "exists": {"input": {"engine_id": "str"}, "output": {"ok": "bool", "exists": "bool"}},
            "destroy": {"input": {"engine_id": "str"}, "output": {"ok": "bool"}},
            "list": {"output": {"ok": "bool", "engines": "dict"}},
            "_cleanup": {"output": {"ok": "bool", "cleaned": "bool"}},
        },
    }
