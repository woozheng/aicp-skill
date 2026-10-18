"""应用商店 — 安装/卸载/列表（支持 single + bundle 两种类型）"""
import json
import subprocess
import shutil
from pathlib import Path

DEFAULT_INDEX_URL = "https://raw.githubusercontent.com/aicp-store/index/main/apps.json"


async def execute(envelop, agent):
    action = envelop.payload.get("action", "list")

    if action == "list":
        return await _list(envelop, agent)
    elif action == "install":
        return await _install(envelop, agent)
    elif action == "uninstall":
        return await _uninstall(envelop, agent)
    elif action == "installed":
        return await _installed(envelop, agent)
    else:
        envelop.payload = {"error": f"Unknown action: {action}"}
        return envelop


async def _list(envelop, agent):
    import requests
    index_url = envelop.payload.get("index_url", DEFAULT_INDEX_URL)

    try:
        resp = requests.get(index_url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            apps = data.get("apps", [])
        else:
            apps = []
    except Exception as e:
        agent.log.warning(f"[Store] Failed to fetch index: {e}")
        apps = _fallback_index()

    installed_ids = _get_installed_ids()
    for app in apps:
        app["installed"] = app["id"] in installed_ids

    envelop.payload = {"apps": apps, "total": len(apps)}
    return envelop


async def _install(envelop, agent):
    """安装应用：git clone → 复制到对应目录"""
    app_id = envelop.payload.get("app_id", "")
    git_url = envelop.payload.get("git_url", "")
    app_type = envelop.payload.get("type", "single")
    bundled_apps = envelop.payload.get("bundled_apps", [])

    if not app_id or not git_url:
        envelop.payload = {"error": "Missing app_id or git_url"}
        return envelop

    # 检查 git
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        envelop.payload = {"error": "Git not installed"}
        return envelop

    import tempfile
    tmp_dir = Path(tempfile.mkdtemp())

    try:
        result = subprocess.run(
            ["git", "clone", "--depth=1", git_url, str(tmp_dir)],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            envelop.payload = {"error": f"Git clone failed: {result.stderr[:200]}"}
            return envelop

        if app_type == "bundle" and bundled_apps:
            # Bundle 模式：apps/{sub_id}/ 下每个子应用
            for sub_id in bundled_apps:
                _copy_app(tmp_dir / "apps" / sub_id, sub_id)
            installed_ids = bundled_apps
        else:
            # Single 模式：直接复制
            _copy_app(tmp_dir, app_id)
            installed_ids = [app_id]

        agent.log.info(f"[Store] Installed: {installed_ids}")
        envelop.payload = {"ok": True, "app_id": app_id, "installed": installed_ids}

    except subprocess.TimeoutExpired:
        envelop.payload = {"error": "Git clone timeout"}
    except Exception as e:
        envelop.payload = {"error": f"Install failed: {str(e)}"}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return envelop


def _copy_app(src_dir, app_id):
    """复制插件目录和 www 目录"""
    # 插件目录：src/plugins/applications/{app_id} 或 src/{app_id}/plugins/applications/{app_id}
    plugin_src = src_dir / "plugins" / "applications" / app_id
    if not plugin_src.exists():
        # 兼容 bundle 结构：src/apps/{app_id}/plugins/applications/{app_id}
        plugin_src = src_dir / app_id / "plugins" / "applications" / app_id
    plugin_dst = Path(f"plugins/applications/{app_id}")

    # www 目录
    www_src = src_dir / "www" / app_id
    if not www_src.exists():
        www_src = src_dir / app_id / "www" / app_id
    www_dst = Path(f"www/{app_id}")

    if plugin_src.exists():
        if plugin_dst.exists():
            shutil.rmtree(plugin_dst)
        shutil.copytree(plugin_src, plugin_dst)

    if www_src.exists():
        if www_dst.exists():
            shutil.rmtree(www_dst)
        shutil.copytree(www_src, www_dst)


async def _uninstall(envelop, agent):
    """卸载应用"""
    app_id = envelop.payload.get("app_id", "")
    app_type = envelop.payload.get("type", "single")
    bundled_apps = envelop.payload.get("bundled_apps", [])

    if not app_id:
        envelop.payload = {"error": "Missing app_id"}
        return envelop

    if app_type == "bundle" and bundled_apps:
        for sub_id in bundled_apps:
            _remove_app(sub_id)
    else:
        _remove_app(app_id)

    agent.log.info(f"[Store] Uninstalled: {app_id}")
    envelop.payload = {"ok": True, "app_id": app_id}
    return envelop


def _remove_app(app_id):
    """删除应用目录"""
    shutil.rmtree(Path(f"plugins/applications/{app_id}"), ignore_errors=True)
    shutil.rmtree(Path(f"www/{app_id}"), ignore_errors=True)


async def _installed(envelop, agent):
    """列出已安装的应用"""
    apps = []
    apps_dir = Path("plugins/applications")
    if apps_dir.exists():
        for d in sorted(apps_dir.iterdir()):
            if d.is_dir() and not d.name.startswith(".") and not d.name.startswith("_"):
                app_yaml = d / "app.yaml"
                info = {"id": d.name, "name": d.name, "version": "1.0.0", "description": ""}
                if app_yaml.exists():
                    try:
                        import yaml
                        with open(app_yaml, "r", encoding="utf-8") as f:
                            cfg = yaml.safe_load(f) or {}
                        info["name"] = cfg.get("name", d.name)
                        info["version"] = cfg.get("version", "1.0.0")
                        info["description"] = cfg.get("description", "")
                    except Exception:
                        pass
                apps.append(info)

    envelop.payload = {"apps": apps, "total": len(apps)}
    return envelop


def _get_installed_ids():
    """获取已安装应用的 ID 集合"""
    ids = {}
    apps_dir = Path("plugins/applications")
    if apps_dir.exists():
        for d in apps_dir.iterdir():
            if d.is_dir() and not d.name.startswith(".") and not d.name.startswith("_"):
                ids[d.name] = True
    return ids


def _fallback_index():
    """内置默认应用列表"""
    return [
        {
            "id": "office",
            "name": "📦 Office 三件套",
            "type": "single",
            "description": "思维导图 + 文本转MD + 富文本编辑器",
            "category": "office",
            "author": "AICP",
            "version": "1.0.0",
            "git_url": "https://github.com/aicp-store/office.git",
        },
        {
            "id": "magicmouse",
            "name": "🪄 魔法鼠标",
            "type": "single",
            "description": "框选截图 → AI 分析 → 多轮对话",
            "category": "tool",
            "author": "AICP",
            "version": "1.0.0",
            "git_url": "https://github.com/aicp-store/magicmouse.git",
        },
        {
            "id": "hermes",
            "name": "📦 Hermes 运行时",
            "type": "single",
            "description": "Hermes 插件 + CLI Agent 支持",
            "category": "runtime",
            "author": "AICP",
            "version": "1.0.0",
            "git_url": "https://github.com/aicp-store/hermes.git",
        },
    ]