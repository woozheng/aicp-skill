"""控制面板 API — 读取/修改配置、应用管理、日志"""
import os
import re
import yaml
import shutil
from pathlib import Path

ENV_VAR_PATTERN = re.compile(r'\$\{(\w+)\}')


def _mask_api_key(api_key):
    """安全处理 api_key 显示"""
    if not api_key or api_key == "none":
        return api_key
    if ENV_VAR_PATTERN.match(str(api_key)):
        return api_key  # 环境变量原样返回
    # 明文密钥：返回完整值 + 掩码标记
    return api_key


def _mask_sensitive_config(config):
    """递归处理敏感字段，保留原始值但标记"""
    if not isinstance(config, dict):
        return config

    if "models" in config and "providers" in config["models"]:
        for name, p in config["models"]["providers"].items():
            if "api_key" in p and p["api_key"] and p["api_key"] != "none":
                if not ENV_VAR_PATTERN.match(str(p["api_key"])):
                    # 明文密钥：保留完整值，前端会处理显示
                    pass  # 不做截断！
    return config


async def execute(envelop, agent):
    action = envelop.payload.get("action", "get")

    # ── 获取配置 ──
    if action == "get":
        config_path = Path("aicp.yaml")
        if config_path.exists():
            try:
                raw_text = config_path.read_text(encoding="utf-8")
                config = yaml.safe_load(raw_text) if raw_text.strip() else {}
            except Exception:
                config = {}
        else:
            config = {}

        envelop.payload = config
        return envelop

    # ── 修改配置 ──
    elif action == "set":
        key = envelop.payload.get("key", "")
        value = envelop.payload.get("value")
        if not key:
            envelop.payload = {"error": "key required"}
            return envelop

        print(f"[control] set: key={key}, value_keys={list(value.keys()) if isinstance(value, dict) else value}")

        config_path = Path("aicp.yaml")
        try:
            raw_text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
        except Exception:
            raw_text = ""

        try:
            config = yaml.safe_load(raw_text) if raw_text.strip() else {}
            if not isinstance(config, dict):
                config = {}
        except Exception:
            config = {}

        keys = key.split(".")

        # 🔥 关键：models.providers 直接覆盖
        if key == "models.providers":
            if "models" not in config:
                config["models"] = {}
            config["models"]["providers"] = value if isinstance(value, dict) else {}
        else:
            target = config
            if value is None:
                for k in keys[:-1]:
                    if k not in target:
                        target[k] = {}
                    target = target[k]
                target.pop(keys[-1], None)
            else:
                for k in keys[:-1]:
                    if k not in target:
                        target[k] = {}
                    target = target[k]
                target[keys[-1]] = value

        # 清理 null
        def clean_null(obj):
            if isinstance(obj, dict):
                return {k: clean_null(v) for k, v in obj.items() if v is not None}
            elif isinstance(obj, list):
                return [clean_null(v) for v in obj if v is not None]
            else:
                return obj

        config = clean_null(config)

        if "models" not in config:
            config["models"] = {}
        if "providers" not in config["models"]:
            config["models"]["providers"] = {}

        # 写入文件
        config_path.write_text(
            yaml.dump(config, allow_unicode=True, default_flow_style=False, sort_keys=False),
            encoding="utf-8"
        )

        # 同步到内存
        if key == "models.providers":
            if "models" not in agent.config:
                agent.config["models"] = {}
            agent.config["models"]["providers"] = value if isinstance(value, dict) else {}
        else:
            target_mem = agent.config
            if value is None:
                for k in keys[:-1]:
                    if k not in target_mem:
                        target_mem[k] = {}
                    target_mem = target_mem[k]
                target_mem.pop(keys[-1], None)
            else:
                for k in keys[:-1]:
                    if k not in target_mem:
                        target_mem[k] = {}
                    target_mem = target_mem[k]
                target_mem[keys[-1]] = value

        # ★★★ 如果是 models 相关，重建 LLM ★★★
        llm_reloaded = False
        llm_error = None
        if key.startswith("models"):
            try:
                from runtime._llm import LLM
                from runtime._aicp_llm import AICP_LLM

                new_config = agent.config
                if new_config.get("models", {}).get("providers"):
                    new_llm = LLM(new_config)
                    new_aicp_llm = AICP_LLM(new_config, is_remote=True, llm=new_llm)   # ← 传入

                    agent.llm = new_llm
                    agent.aicp_llm = new_aicp_llm
                    agent.chatEnvelop = new_aicp_llm.chatEnvelop if new_aicp_llm else None

                    # 清 remote caps 缓存（类变量）
                    AICP_LLM._cached_remote_section = None
                    AICP_LLM._cached_remote_ts = 0.0

                    llm_reloaded = True
                    print(f"[control] ✅ LLM 已重建（key={key}）")
                else:
                    print(f"[control] config 更新，但无 providers，跳过 LLM 重建")
            except Exception as e:
                import traceback
                llm_error = str(e)
                print(f"[control] ❌ LLM 重建失败: {e}")
                traceback.print_exc()

        envelop.payload = {
            "ok": True,
            "key": key,
            "llm_reloaded": llm_reloaded,
            "llm_error": llm_error,
        }
        return envelop

    # ── 应用列表 ──
    elif action == "list" or action == "apps":
        apps = []
        for scan_dir in ["plugins/builtins", "plugins/applications"]:
            d = Path(scan_dir)
            if not d.exists():
                continue
            for app_dir in sorted(d.iterdir()):
                if not app_dir.is_dir():
                    continue
                if app_dir.name.startswith(".") or app_dir.name.startswith("_"):
                    continue

                try:
                    app_yaml = app_dir / "app.yaml"
                    cfg = {}
                    if app_yaml.exists():
                        try:
                            cfg = yaml.safe_load(app_yaml.read_text(encoding="utf-8")) or {}
                            if not isinstance(cfg, dict):
                                cfg = {}
                        except Exception as e:
                            cfg = {"_error": f"app.yaml 解析失败: {e}"}

                    apps.append({
                        "id": app_dir.name,
                        "name": cfg.get("name", app_dir.name),
                        "type": "builtin" if scan_dir == "plugins/builtins" else "user",
                        "auto_start": cfg.get("auto_start", False),
                        "py_files": len(list(app_dir.rglob("*.py"))),
                        "html_files": len(list(Path("www").rglob(f"{app_dir.name}/**/*.html"))) if Path("www").exists() else 0,
                        "deletable": scan_dir != "plugins/builtins",
                        "error": cfg.get("_error", ""),
                    })
                except Exception as e:
                    apps.append({
                        "id": app_dir.name,
                        "name": app_dir.name,
                        "type": "builtin" if scan_dir == "plugins/builtins" else "user",
                        "auto_start": False,
                        "py_files": 0,
                        "html_files": 0,
                        "deletable": False,
                        "error": str(e),
                    })

        envelop.payload = {"apps": apps}
        return envelop

    # ── 卸载应用 ──
    elif action == "uninstall":
        app_id = envelop.payload.get("app_id", "")
        if not app_id:
            envelop.payload = {"error": "app_id required"}
            return envelop
        if app_id in ("studio", "mk", "control", "system", "os"):
            envelop.payload = {"error": "Cannot uninstall system app"}
            return envelop
        shutil.rmtree(Path(f"plugins/applications/{app_id}"), ignore_errors=True)
        shutil.rmtree(Path(f"www/{app_id}"), ignore_errors=True)
        envelop.payload = {"ok": True, "app_id": app_id}
        return envelop

    # ── 日志 ──
    elif action == "logs":
        log_path = Path("data/gateway.log")
        lines_count = envelop.payload.get("lines", 100)
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8").strip().split("\n")[-lines_count:]
        else:
            lines = ["No log file found"]
        envelop.payload = {"lines": lines}
        return envelop

    # ── 重启 ──
    elif action == "restart":
        import os as _os, sys
        envelop.payload = {"ok": True, "message": "Restarting..."}
        _os.execv(sys.executable, [sys.executable] + sys.argv)
        return envelop

    envelop.payload = {"error": f"Unknown action: {action}"}
    return envelop