"""控制面板 API — 应用管理"""
from pathlib import Path


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
                    "deletable": scan_dir == "plugins/applications" and d.name not in ("studio", "mk"),
                })

        envelop.payload = {"apps": apps}
        return envelop

    elif action == "uninstall":
        app_id = envelop.payload.get("app_id", "")
        if not app_id:
            envelop.payload = {"error": "app_id required"}
            return envelop
        if app_id in ("studio", "mk", "control"):
            envelop.payload = {"error": "System app cannot be uninstalled"}
            return envelop

        import shutil
        for d in [Path(f"plugins/applications/{app_id}"), Path(f"www/{app_id}")]:
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

    envelop.payload = {"error": f"Unknown action: {action}"}
    return envelop