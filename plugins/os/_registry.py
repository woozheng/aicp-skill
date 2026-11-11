"""
插件发现与注册中心 — 协议 v4.1
提供插件的注册、注销、查询、热重载能力，作为系统的权威插件来源。

v4.1 改进：
- 不再跳过 _init.py，正常注册为 applications/{project}/_init
- 加载 _init.py 后自动调一次 action="init"，用于应用初始化数据
"""

import asyncio
import ast
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set
from datetime import datetime

import core


# ============================================================
# 常量
# ============================================================

PLUGINS_ROOT = Path("plugins")
CACHE_FILE = Path("data/registry_cache.json")
SYNC_LOCK = asyncio.Lock()


# ============================================================
# 插件元数据模型
# ============================================================

class PluginMetadata:
    """插件元数据"""

    def __init__(self, name: str, file_path: Path):
        self.name = name
        self.file_path = file_path
        self.file_hash = self._calc_hash()
        self.last_modified = file_path.stat().st_mtime if file_path.exists() else 0
        self.help_data = None
        self.execute_func = None
        self.is_loaded = False
        self.load_error = None
        self.last_loaded = None

    def _calc_hash(self) -> str:
        if not self.file_path.exists():
            return "missing"
        try:
            with open(self.file_path, "rb") as f:
                return hashlib.md5(f.read(65536)).hexdigest()[:12]
        except Exception:
            return "error"

    def refresh_hash(self) -> str:
        self.file_hash = self._calc_hash()
        if self.file_path.exists():
            self.last_modified = self.file_path.stat().st_mtime
        return self.file_hash

    def is_changed(self) -> bool:
        if not self.file_path.exists():
            return self.file_hash != "missing"
        current_hash = self._calc_hash()
        return current_hash != self.file_hash

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "file_path": str(self.file_path),
            "file_hash": self.file_hash,
            "last_modified": self.last_modified,
            "is_loaded": self.is_loaded,
            "load_error": self.load_error,
            "last_loaded": self.last_loaded.isoformat() if self.last_loaded else None,
            "help": self.help_data
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PluginMetadata":
        meta = cls(data["name"], Path(data["file_path"]))
        meta.file_hash = data.get("file_hash", "unknown")
        meta.last_modified = data.get("last_modified", 0)
        meta.is_loaded = data.get("is_loaded", False)
        meta.load_error = data.get("load_error")
        meta.help_data = data.get("help")
        if data.get("last_loaded"):
            meta.last_loaded = datetime.fromisoformat(data["last_loaded"])
        return meta


# ============================================================
# 工具函数：删除契约文件
# ============================================================

def _delete_contract_file(plugin_name: str, agent=None) -> bool:
    contract_file = PLUGINS_ROOT / f"{plugin_name}.contract.json"
    if contract_file.exists():
        try:
            contract_file.unlink()
            if agent:
                agent.log.info(f"[registry] 删除契约: {contract_file}")
            return True
        except Exception as e:
            if agent:
                agent.log.warning(f"[registry] 删除契约失败: {contract_file}, {e}")
            return False
    return False


# ============================================================
# 工具函数：插件签名检测
# ============================================================

# 路径 -> (mtime, size, 是否有 execute 签名)
_EXEC_SIG_CACHE: Dict[str, tuple] = {}


def _has_execute_signature(py_file: Path) -> bool:
    """判断 .py 是否定义了模块级 execute —— 插件的唯一签名。

    非插件的辅助脚本（工具库、GUI 窗口等）不入插件表，
    避免污染加载统计与 cogitor 搜索结果。

    语法错误时返回 True：保留条目，把真实错误留给加载阶段暴露。
    """
    key = str(py_file)
    try:
        st = py_file.stat()
        sig = (st.st_mtime, st.st_size)
    except Exception:
        return True

    cached = _EXEC_SIG_CACHE.get(key)
    if cached and cached[0] == sig:
        return cached[1]

    try:
        src = py_file.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return True

    # ★ AST 优先：只认「模块级」的 execute 定义。
    #   用正则会误判 —— 文档/注释里行首写一行 "def execute(...)" 的示例代码
    #   会被当成真插件。AST 只看顶层节点，判定精确。
    result = False
    try:
        tree = ast.parse(src)
    except Exception:
        # 语法错误：保留条目，把真实错误留给加载阶段暴露
        result = True
    else:
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "execute":
                    result = True
                    break
            elif isinstance(node, ast.Assign):
                if any(isinstance(t, ast.Name) and t.id == "execute"
                       for t in node.targets):
                    result = True
                    break
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) \
                        and node.target.id == "execute":
                    result = True
                    break

    _EXEC_SIG_CACHE[key] = (sig, result)
    return result


# ============================================================
# 插件注册中心
# ============================================================

class PluginRegistry:
    """插件注册中心 - 单例"""

    def __init__(self):
        self._plugins: Dict[str, PluginMetadata] = {}
        self._loaded_modules: Set[str] = set()
        self._initialized = False
        self._agent = None
        self._cache_dirty = False
        self._virtual: Dict[str, dict] = {}

    def initialize(self, agent):
        if self._initialized:
            return
        self._agent = agent
        self._load_cache()
        self._scan_plugins()

        loaded = 0
        failed = 0
        for name in list(self._plugins.keys()):
            if self.load_plugin(name):
                loaded += 1
            else:
                failed += 1

        self._initialized = True
        agent.log.info(f"[registry] 初始化完成，发现 {len(self._plugins)} 个插件，加载 {loaded} 个，失败 {failed} 个")

    # ============================================================
    # 插件发现与扫描
    # ============================================================

    def _scan_plugins(self) -> int:
        discovered = 0
        plugin_files = self._find_plugin_files()

        for name, file_path in plugin_files.items():
            if name in self._plugins:
                if self._plugins[name].is_changed():
                    self._plugins[name].refresh_hash()
                    self._cache_dirty = True
                    _delete_contract_file(name, self._agent)
                    if self._agent:
                        self._agent.log.info(f"[registry] 检测到插件变化: {name}（契约已删除）")
            else:
                self._plugins[name] = PluginMetadata(name, file_path)
                self._cache_dirty = True
                discovered += 1
                if self._agent:
                    self._agent.log.info(f"[registry] 发现新插件: {name}")

        for name in list(self._plugins.keys()):
            if not self._plugins[name].file_path.exists():
                if self._agent:
                    self._agent.log.info(f"[registry] 检测到插件删除: {name}")
                _delete_contract_file(name, self._agent)
                del self._plugins[name]
                self._cache_dirty = True

        if discovered > 0 or self._cache_dirty:
            self._save_cache()

        return discovered

    def _find_plugin_files(self) -> Dict[str, Path]:
        """查找所有插件文件，支持包模式（__init__.py）

        v4.1：不再跳过 _init.py，正常注册。
        """
        plugins = {}

        if not PLUGINS_ROOT.exists():
            return plugins

        init_dirs: Set[Path] = set()
        for py_file in PLUGINS_ROOT.rglob("__init__.py"):
            # ★ 只有自身就是插件的包目录，才按包模式注册
            if _has_execute_signature(py_file):
                init_dirs.add(py_file.parent)

        for py_file in PLUGINS_ROOT.rglob("*.py"):
            # v4.1: 不再跳过 _init.py
            # ★ 只收录定义了模块级 execute 的文件（插件的唯一签名）。
            #   辅助脚本 / 工具库 / GUI 窗口等非插件文件不入插件表，
            #   避免污染加载统计与 cogitor 搜索结果。
            if not _has_execute_signature(py_file):
                continue

            rel_path = py_file.relative_to(PLUGINS_ROOT)

            if py_file.name == "__init__.py":
                name = str(rel_path.parent).replace("\\", "/")
                if name == ".":
                    continue
                plugins[name] = py_file
                continue

            parent_dir = py_file.parent
            if parent_dir in init_dirs:
                continue

            name = str(rel_path.with_suffix("")).replace("\\", "/")

            if "_ui" in name or "ui_generator" in name:
                continue

            plugins[name] = py_file

        return plugins

    # ============================================================
    # 缓存管理
    # ============================================================

    def _load_cache(self):
        if not CACHE_FILE.exists():
            return
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            skipped = 0
            for name, meta_data in data.get("plugins", {}).items():
                meta = PluginMetadata.from_dict(meta_data)
                # ★ 陈旧缓存里的非插件条目（辅助脚本 / GUI 窗口等）不再入表
                if not _has_execute_signature(meta.file_path):
                    skipped += 1
                    continue
                self._plugins[name] = meta
            if self._agent:
                self._agent.log.info(f"[registry] 加载缓存: {len(self._plugins)} 个插件（跳过非插件 {skipped} 个）")
        except Exception as e:
            if self._agent:
                self._agent.log.warning(f"[registry] 缓存加载失败: {e}")

    def _save_cache(self):
        if not self._cache_dirty:
            return
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": "4.1",
                "updated": datetime.now().isoformat(),
                "total": len(self._plugins),
                "plugins": {name: meta.to_dict() for name, meta in self._plugins.items()}
            }
            CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            self._cache_dirty = False
        except Exception as e:
            if self._agent:
                self._agent.log.error(f"[registry] 缓存保存失败: {e}")

    # ============================================================
    # 插件加载
    # ============================================================

    def load_plugin(self, name: str) -> bool:
        """加载插件（同步，不调 init）"""
        meta = self._plugins.get(name)
        if not meta:
            return False

        if meta.is_loaded and not meta.is_changed():
            return True

        try:
            module_name = f"plugins.{name.replace('/', '.')}"

            if module_name in sys.modules:
                del sys.modules[module_name]

            spec = importlib.util.spec_from_file_location(module_name, meta.file_path)
            if spec is None or spec.loader is None:
                raise ImportError(f"Cannot load spec for {name}")

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            if not hasattr(module, "execute"):
                return True

            core.plugins[name] = module.execute
            meta.execute_func = module.execute
            meta.is_loaded = True
            meta.load_error = None
            meta.last_loaded = datetime.now()

            if hasattr(module, "help"):
                try:
                    help_data = module.help()
                    json.dumps(help_data)
                    meta.help_data = help_data
                except Exception as e:
                    meta.help_data = {"error": str(e)}

            self._cache_dirty = True
            self._save_cache()

            if self._agent:
                self._agent.log.debug(f"[registry] 加载成功: {name}")
            return True

        except Exception as e:
            meta.is_loaded = False
            meta.load_error = str(e)
            self._cache_dirty = True
            if self._agent:
                self._agent.log.debug(f"[registry] 加载失败 {name}: {e}")
            return False

    async def _maybe_init(self, name: str, agent):
        """如果加载的是 _init 插件，调一次 action=init"""
        if not name.endswith("/_init"):
            return
        try:
            await agent.system.call(core.Envelop(
                sender="system/registry",
                receiver=name,
                payload={"action": "init"},
            ))
            agent.log.info(f"[registry] 已初始化: {name}")
        except Exception as e:
            agent.log.warning(f"[registry] 初始化失败 {name}: {e}")

    def unload_plugin(self, name: str):
        meta = self._plugins.get(name)
        if meta:
            meta.is_loaded = False
            meta.execute_func = None
            self._cache_dirty = True

        module_name = f"plugins.{name.replace('/', '.')}"
        if module_name in sys.modules:
            del sys.modules[module_name]

        if name in core.plugins:
            del core.plugins[name]

    # ============================================================
    # 查询接口
    # ============================================================

    def list_plugins(self, include_unloaded: bool = False) -> List[dict]:
        result = []
        for name, meta in self._plugins.items():
            if not include_unloaded and not meta.is_loaded:
                continue
            result.append({
                "route": f"/api/{name}",
                "name": name,
                "file_path": str(meta.file_path),
                "file_hash": meta.file_hash,
                "is_loaded": meta.is_loaded,
                "help": meta.help_data,
                "load_error": meta.load_error
            })
        for vname, vinfo in self._virtual.items():
            result.append({
                "route": f"/api/{vname}",
                "name": vname,
                "file_path": None,
                "file_hash": "virtual",
                "is_loaded": True,
                "help": {
                    "description": vinfo.get("desc", ""),
                    "schema": vinfo.get("schema"),
                },
                "load_error": None,
                "is_virtual": True,
                "source": vinfo.get("source", "virtual"),
                "owner": vinfo.get("owner"),
            })
        return result

    def get_plugin_info(self, name: str) -> Optional[dict]:
        meta = self._plugins.get(name)
        if not meta:
            vinfo = self._virtual.get(name)
            if vinfo:
                return {
                    "route": f"/api/{name}",
                    "name": name,
                    "file_path": None,
                    "file_hash": "virtual",
                    "is_loaded": True,
                    "help": {
                        "description": vinfo.get("desc", ""),
                        "schema": vinfo.get("schema"),
                    },
                    "load_error": None,
                    "last_loaded": None,
                    "is_virtual": True,
                    "source": vinfo.get("source", "virtual"),
                    "owner": vinfo.get("owner"),
                }
            return None
        return {
            "route": f"/api/{name}",
            "name": name,
            "file_path": str(meta.file_path),
            "file_hash": meta.file_hash,
            "is_loaded": meta.is_loaded,
            "help": meta.help_data,
            "load_error": meta.load_error,
            "last_loaded": meta.last_loaded.isoformat() if meta.last_loaded else None
        }

    # ---- 虚拟条目（外部 MCP 工具等，不落文件，内存注册）----
    def register_virtual(self, name: str, desc: str = "", schema=None, owner: str = None,
                         source: str = "virtual", handler=None) -> bool:
        if not name:
            return False
        self._virtual[name] = {
            "desc": desc or "",
            "schema": schema,
            "owner": owner,
            "source": source,
            "handler": handler,
        }
        self._cache_dirty = True
        return True

    def unregister_virtual(self, prefix: str) -> int:
        if not prefix:
            return 0
        pfx = prefix.rstrip("/") + "/"
        names = [n for n in self._virtual if n == prefix or n.startswith(pfx)]
        for n in names:
            self._virtual.pop(n, None)
        if names:
            self._cache_dirty = True
        return len(names)

    def get_virtual_handler(self, name: str):
        v = self._virtual.get(name)
        return v.get("handler") if v else None

    def list_virtual(self) -> Dict[str, dict]:
        return self._virtual

    def get_stats(self) -> dict:
        loaded = sum(1 for m in self._plugins.values() if m.is_loaded)
        with_error = sum(1 for m in self._plugins.values() if m.load_error)
        return {
            "total": len(self._plugins),
            "loaded": loaded,
            "with_error": with_error,
            "virtual": len(self._virtual),
            "cache_file": str(CACHE_FILE)
        }


# ============================================================
# 全局单例
# ============================================================

_registry = PluginRegistry()


# ============================================================
# 插件入口
# ============================================================

async def execute(envelop, agent):
    action = envelop.payload.get("action", "list")

    _registry.initialize(agent)

    handlers = {
        "list": _handle_list,
        "reload": _handle_reload,
        "sync": _handle_sync,
        "get": _handle_get,
        "stats": _handle_stats,
        "load": _handle_load,
        "unload": _handle_unload,
        "refresh": _handle_refresh,
        "register_virtual": _handle_register_virtual,
        "unregister_virtual": _handle_unregister_virtual
    }

    handler = handlers.get(action)
    if not handler:
        envelop.payload = {"error": f"Unknown action: {action}"}
        return envelop

    return await handler(envelop, agent)


# ============================================================
# 动作处理器
# ============================================================

_RESTORE_DONE = False


async def _maybe_restore_external(agent):
    """进程重启后，从本地 tools 缓存重放外部 MCP 工具（不联网）。"""
    global _RESTORE_DONE
    if _RESTORE_DONE:
        return
    if _registry._virtual:
        _RESTORE_DONE = True
        return
    if "os/_mcp_client" not in core.plugins:
        return  # 尚未加载，下次 list 再试
    _RESTORE_DONE = True
    try:
        env = core.Envelop(sender="os/_registry", receiver="os/_mcp_client",
                           intent="API_CALL", payload={"action": "register_virtual"})
        await agent.system.call(env)
        cnt = len(_registry._virtual)
        if cnt:
            agent.log.info("[registry] 外部工具已恢复 %d 条（本地缓存重放，未联网）" % cnt)
    except Exception as e:
        try:
            agent.log.warning("[registry] 外部工具恢复失败: %s" % e)
        except Exception:
            pass


# 对外隐藏的应用目录（模板/内置 demo 等），list 不返回、不暴露
# 对外隐藏的应用目录（模板/内置 demo 等），list 不返回、不暴露
# 2026-11-09：_demo 取消隐藏（本地实例需要能直接访问调试）。
HIDDEN_APP_PREFIXES = ()


async def _handle_list(envelop, agent):
    _registry._scan_plugins()  # ★ 先扫描，清理已删文件
    await _maybe_restore_external(agent)
    include_unloaded = envelop.payload.get("include_unloaded", False)
    plugins = _registry.list_plugins(include_unloaded)
    # ★ 过滤隐藏应用
    plugins = [p for p in plugins if not any(p["name"].startswith(pfx) for pfx in HIDDEN_APP_PREFIXES)]
    envelop.payload = {
        "ok": True,
        "apis": plugins,
        "total": len(plugins),
        "stats": _registry.get_stats()
    }
    return envelop


async def _handle_reload(envelop, agent):
    route_name = envelop.payload.get("route", "")
    if not route_name:
        envelop.payload = {"error": "No route specified"}
        return envelop

    _registry._scan_plugins()
    _delete_contract_file(route_name, agent)

    meta = _registry._plugins.get(route_name)

    if not meta:
        if route_name in core.plugins:
            del core.plugins[route_name]
            agent.log.info(f"[registry] 已移除插件: {route_name}")
        _delete_contract_file(route_name, agent)
        envelop.payload = {
            "ok": True,
            "route": route_name,
            "removed": True,
            "message": f"Plugin {route_name} removed"
        }
        return envelop

    success = _registry.load_plugin(route_name)
    if not success:
        envelop.payload = {
            "ok": False,
            "error": meta.load_error if meta else f"Plugin not found: {route_name}"
        }
        return envelop

    # v4.1: 如果是 _init，加载后调一次 init
    await _registry._maybe_init(route_name, agent)

    agent.log.info(f"[registry] 重载成功: {route_name}（契约已删除，下次查询将重新提取）")

    envelop.payload = {
        "ok": True,
        "route": route_name,
        "info": _registry.get_plugin_info(route_name),
        "contract_deleted": True,
        "message": f"Plugin {route_name} reloaded, contract file removed"
    }
    return envelop


async def _handle_sync(envelop, agent):
    async with SYNC_LOCK:
        discovered = _registry._scan_plugins()

        loaded = 0
        newly_loaded = []
        for name, meta in _registry._plugins.items():
            if not meta.is_loaded:
                if _registry.load_plugin(name):
                    loaded += 1
                    newly_loaded.append(name)

        # v4.1: 新加载的 _init 调一次 init
        for name in newly_loaded:
            await _registry._maybe_init(name, agent)

        envelop.payload = {
            "ok": True,
            "discovered": discovered,
            "loaded": loaded,
            "stats": _registry.get_stats()
        }
        return envelop


async def _handle_get(envelop, agent):
    name = envelop.payload.get("name", "")
    if not name:
        envelop.payload = {"error": "name required"}
        return envelop

    info = _registry.get_plugin_info(name)
    if not info:
        envelop.payload = {"error": f"Plugin not found: {name}"}
        return envelop

    envelop.payload = {"ok": True, "plugin": info}
    return envelop


async def _handle_stats(envelop, agent):
    envelop.payload = {"ok": True, "stats": _registry.get_stats()}
    return envelop


async def _handle_load(envelop, agent):
    name = envelop.payload.get("name", "")
    if not name:
        envelop.payload = {"error": "name required"}
        return envelop

    _registry._scan_plugins()

    success = _registry.load_plugin(name)
    if success:
        # v4.1: 如果是 _init，加载后调一次 init
        await _registry._maybe_init(name, agent)

    envelop.payload = {
        "ok": success,
        "name": name,
        "info": _registry.get_plugin_info(name) if success else None
    }
    return envelop


async def _handle_unload(envelop, agent):
    name = envelop.payload.get("name", "")
    if not name:
        envelop.payload = {"error": "name required"}
        return envelop

    _registry.unload_plugin(name)
    envelop.payload = {"ok": True, "name": name}
    return envelop


async def _handle_refresh(envelop, agent):
    async with SYNC_LOCK:
        CACHE_FILE.unlink(missing_ok=True)

        contract_files = list(PLUGINS_ROOT.glob("*.contract.json"))
        deleted = 0
        for cf in contract_files:
            try:
                cf.unlink()
                deleted += 1
            except Exception:
                pass

        _registry._plugins.clear()
        _registry._cache_dirty = True

        _registry._scan_plugins()

        newly_loaded = []
        for name in list(_registry._plugins.keys()):
            if _registry.load_plugin(name):
                newly_loaded.append(name)

        for name in newly_loaded:
            await _registry._maybe_init(name, agent)

        agent.log.info(f"[registry] 刷新完成，删除 {deleted} 个契约文件")

        envelop.payload = {
            "ok": True,
            "stats": _registry.get_stats(),
            "contracts_deleted": deleted,
            "message": f"Registry fully refreshed, {deleted} contract files deleted"
        }
        return envelop


# ============================================================
# 帮助信息
# ============================================================

async def _handle_register_virtual(envelop, agent):
    payload = envelop.payload
    items = payload.get("items")
    if isinstance(items, list):
        n = 0
        for it in items:
            if isinstance(it, dict) and _registry.register_virtual(
                it.get("name"), it.get("desc", ""), it.get("schema"),
                it.get("owner"), it.get("source", "virtual")
            ):
                n += 1
        envelop.payload = {"ok": True, "registered": n}
        return envelop

    ok = _registry.register_virtual(
        payload.get("name"), payload.get("desc", ""), payload.get("schema"),
        payload.get("owner"), payload.get("source", "virtual")
    )
    envelop.payload = {"ok": ok} if ok else {"ok": False, "error": "name required"}
    return envelop


async def _handle_unregister_virtual(envelop, agent):
    prefix = envelop.payload.get("prefix") or envelop.payload.get("owner") or ""
    n = _registry.unregister_virtual(prefix)
    envelop.payload = {"ok": True, "removed": n, "prefix": prefix}
    return envelop


def help() -> dict:
    return {
        "route": "os/_registry",
        "description": "插件注册中心 - 管理所有插件的发现、加载、重载和查询",
        "input": {
            "action": "required - list|reload|sync|get|stats|load|unload|refresh",
            "route": "optional - 插件路由名，reload 时需要",
            "name": "optional - 插件名，get/load/unload 时需要",
            "include_unloaded": "optional - list 时是否包含未加载的插件"
        },
        "output": {
            "ok": "操作是否成功",
            "error": "错误信息（失败时）",
            "apis": "插件列表",
            "stats": "统计信息",
            "plugin": "单个插件信息",
            "contract_deleted": "是否删除了契约文件（reload 时）",
            "contracts_deleted": "删除的契约文件数量（refresh 时）"
        }
    }
