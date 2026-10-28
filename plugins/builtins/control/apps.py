"""控制面板 API — 应用管理"""
import re
from pathlib import Path

# 受保护的系统示例应用：不允许被卸载（后端拦截，前端也会提示）
PROTECTED_APPS = {"_demo"}


async def execute(envelop, agent):  
    action = envelop.payload.get("action", "list")

    if action == "list":
        apps = []
        for scan_dir in ["plugins/builtins", "plugins/applications"]:
            apps_dir = Path(scan_dir)
            if not apps_dir.exists():
                continue
            for d in sorted(apps_dir.iterdir()):
                if not d.is_dir() or d.name.startswith(".") or d.name == "__pycache__":
                    continue
                app_yaml = d / "app.yaml"
                has_yaml = app_yaml.exists()
                py_count = len(list(d.rglob("*.py")))
                html_count = len(list(d.rglob("*.html")))

                name = d.name
                description = ""
                auto_start = False
                if has_yaml:
                    try:
                        import yaml
                        cfg = yaml.safe_load(app_yaml.read_text(encoding="utf-8"))
                        name = cfg.get("name", d.name)
                        description = cfg.get("description", "")
                        auto_start = cfg.get("auto_start", False)
                    except Exception:
                        pass

                apps.append({
                    "id": d.name,
                    "name": name,
                    "description": description,
                    "auto_start": auto_start,
                    "has_yaml": has_yaml,
                    "py_files": py_count,
                    "html_files": html_count,
                    "type": "builtin" if scan_dir.startswith("plugins/builtins") else "user",
                    "deletable": scan_dir == "plugins/applications" and d.name not in ("studio", "mk") and d.name not in PROTECTED_APPS,
                })

        envelop.payload = {"apps": apps}
        return envelop

    elif action == "uninstall":
        app_id = envelop.payload.get("app_id", "")
        if not app_id:
            envelop.payload = {"error": "app_id required"}
            return envelop
        if app_id in ("studio", "mk", "control"):
            envelop.payload = {"ok": False, "error": "System app cannot be uninstalled", "code": "SYSTEM_APP"}
            return envelop
        if app_id in PROTECTED_APPS:
            envelop.payload = {"ok": False, "error": "System example app cannot be uninstalled", "code": "PROTECTED_APP"}
            return envelop

        import shutil
        for d in [Path(f"plugins/applications/{app_id}"), Path(f"www/apps/{app_id}")]:
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)

        # 清理路由
        import core
        prefix = f"applications/{app_id}/"
        for route in list(core.plugins.keys()):
            if route.startswith(prefix):
                del core.plugins[route]

        envelop.payload = {"ok": True, "app_id": app_id}
        return envelop

    elif action == "list_frontend":
        # 扫描 www/apps/ 下的前端应用（每个目录含 index.html）
        title_re = re.compile(r"<title>(.*?)</title>", re.S | re.I)
        apps = []
        www_dir = Path("www/apps")
        if www_dir.exists():
            for d in sorted(www_dir.iterdir()):
                if not d.is_dir() or d.name.startswith(".") or d.name == "__pycache__":
                    continue
                index_html = d / "index.html"
                if not index_html.exists():
                    continue

                title = d.name
                try:
                    content = index_html.read_text(encoding="utf-8", errors="ignore")
                    m = title_re.search(content)
                    if m and m.group(1).strip():
                        title = m.group(1).strip()
                except Exception:
                    pass

                desc = ""
                desc_en = ""
                yaml_path = Path("plugins/applications") / d.name / "app.yaml"
                if yaml_path.exists():
                    try:
                        import yaml
                        cfg = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
                        desc = cfg.get("description", "") or ""
                        desc_en = cfg.get("description_en", "") or ""
                        if title == d.name and cfg.get("name"):
                            title = cfg.get("name")
                    except Exception:
                        pass

                apps.append({
                    "id": d.name,
                    "name": title,
                    "description": desc,
                    "description_en": desc_en,
                    "url": f"/apps/{d.name}/",
                    "protected": d.name in PROTECTED_APPS,
                    "deletable": d.name not in PROTECTED_APPS,
                })

        envelop.payload = {"ok": True, "apps": apps, "total": len(apps)}
        return envelop

    envelop.payload = {"error": f"Unknown action: {action}"}
    return envelop