"""模型选择器 — provider 配置 + 模型列表 + 切换默认模型

单一 provider 优先：默认只列出 roles.default 用的 provider。
若检测到多个 provider，provider 变成下拉框可选择。

action:
  list_providers  — 列出 provider（名字 / base_url / 密钥掩码 / 已配模型数）
  endpoint_models — 出网 GET {base_url}/models；失败降级为已配置模型
  select_model    — 选定 provider+model → 写 aicp.yaml → 重建 LLM
  current         — 当前默认模型（拆回 provider / 裸 model id）
  save_provider   — 保存 provider（名称 / 端点 / 密钥）→ 写 aicp.yaml + 密钥库
"""
import copy
import json
import os
import re
import time
from pathlib import Path

import yaml

CONFIG_PATH = Path("aicp.yaml")
ENV_PATTERN = re.compile(r"^\$\{(\w+)\}$")
SECRET_PREFIX = "secret:"

DEFAULT_MAX_TOKENS = 16384
DEFAULT_TEMPERATURE = 0.1

_CACHE_TTL = 300
_endpoint_cache = {}


# --------------------------------------------------------------------------
# aicp.yaml 读写
# --------------------------------------------------------------------------

def _read_config():
    if not CONFIG_PATH.exists():
        return {}
    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")
        cfg = yaml.safe_load(text) if text.strip() else {}
    except Exception:
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _write_config(cfg):
    CONFIG_PATH.write_text(
        yaml.dump(cfg, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def _secret_alias(raw):
    """'secret:aggregator' → 'aggregator'；非密钥引用返回 ''。"""
    if isinstance(raw, str) and raw.strip().startswith(SECRET_PREFIX):
        return raw.strip()[len(SECRET_PREFIX):].strip()
    return ""


def _secrets_mod():
    """安全获取密钥库模块（不同加载方式下相对导入可能失败）。"""
    try:
        from plugins.os import _secrets
        return _secrets
    except Exception:
        import importlib
        return importlib.import_module("plugins.os._secrets")


def _resolve_secret(raw, mem_raw):
    """解析 api_key：${VAR} → 环境变量；secret:alias → 密钥库；否则原值。"""
    if isinstance(raw, str):
        s = raw.strip()
        m = ENV_PATTERN.match(s)
        if m:
            return os.environ.get(m.group(1), "") or (mem_raw or "")
        alias = _secret_alias(s)
        if alias:
            try:
                plain = _secrets_mod().decrypt(alias)
                if plain:
                    return plain
            except Exception:
                pass
            return mem_raw or ""
    return raw or (mem_raw or "")


def _key_info(raw, mem_raw):
    """返回 (has_key, masked, source)。source: env / vault / plain / none"""
    s = (raw or "").strip() if isinstance(raw, str) else ""
    m = ENV_PATTERN.match(s)
    if m:
        var = m.group(1)
        val = os.environ.get(var, "") or (mem_raw or "")
        if val:
            return True, _secrets_mod().mask(val), "env"
        return False, "", "env"
    alias = _secret_alias(s)
    if alias:
        plain = _secrets_mod().decrypt(alias)
        if plain:
            return True, _secrets_mod().mask(plain), "vault"
        return False, "", "vault"
    val = raw or (mem_raw or "")
    if val:
        return True, _secrets_mod().mask(val), "plain"
    return False, "", "none"


def _ref(provider, model_id):
    return f"{provider}/{model_id}"


def _split_ref(ref):
    if isinstance(ref, str) and "/" in ref:
        return ref.split("/", 1)
    return None, ref


def _default_provider_name(raw_cfg):
    """roles.default.model 里的 provider 名；没有则取第一个 provider。"""
    entry = ((raw_cfg.get("models") or {}).get("roles") or {}).get("default", "")
    ref = entry.get("model", "") if isinstance(entry, dict) else str(entry or "")
    pn, _ = _split_ref(ref)
    if pn:
        return pn
    providers = (raw_cfg.get("models") or {}).get("providers") or {}
    for name in providers:
        return name
    return ""


def _current_provider_name(raw_cfg):
    """roles.default.model 里的 provider 名（没有则空）。"""
    ref = _current_ref(raw_cfg)
    pn, _ = _split_ref(ref)
    return pn or ""


def _current_ref(raw_cfg):
    """roles.default.model 的完整引用（如 'DeepSeek/MiniMax-M3'）。"""
    entry = ((raw_cfg.get("models") or {}).get("roles") or {}).get("default", "")
    return entry.get("model", "") if isinstance(entry, dict) else str(entry or "")


def _provider_default_model(pv):
    """provider 自己的默认模型：新结构读 default_model；旧结构回退 models[].id。"""
    dm = pv.get("default_model")
    if isinstance(dm, str) and dm.strip():
        return dm.strip()
    models = pv.get("models") or []
    if models and isinstance(models[0], dict):
        mid = models[0].get("id")
        if mid:
            return str(mid)
    return ""


def _migrate_provider(pv):
    """把旧结构（models[] 带冗余参数）迁移到新结构（default_model）。"""
    models = pv.get("models")
    if isinstance(models, list) and models:
        if not pv.get("default_model"):
            mid = ""
            for m in models:
                if isinstance(m, dict) and m.get("id"):
                    mid = str(m["id"])
                    break
            if mid:
                pv["default_model"] = mid
        # models 列表不再需要，删掉（模型每次实时拉取或手动输入）
        pv.pop("models", None)
    elif "models" in pv and not pv.get("default_model"):
        pv.pop("models", None)
    return pv


# --------------------------------------------------------------------------
# 端点拉取
# --------------------------------------------------------------------------

async def _fetch_endpoint_models(base_url, api_key, timeout=20):
    import aiohttp

    url = base_url.rstrip("/") + "/models"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with aiohttp.ClientSession() as sess:
        async with sess.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}: {body[:200]}")
            try:
                data = json.loads(body)
            except Exception:
                raise RuntimeError(f"响应非 JSON: {body[:200]}")

    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        items = []

    out = []
    for it in items:
        if isinstance(it, str):
            out.append({"id": it, "owned_by": ""})
        elif isinstance(it, dict) and it.get("id"):
            out.append({"id": it["id"], "owned_by": it.get("owned_by", "")})
    return out


# --------------------------------------------------------------------------
# LLM 重建
# --------------------------------------------------------------------------

def _rebuild_llm(agent, raw_cfg):
    """用磁盘配置 + 内存里已展开的密钥重建 LLM。"""
    try:
        from runtime._llm import LLM

        llm_config = agent.config or {}
        llm_models = llm_config.setdefault("models", {})
        llm_providers = llm_models.setdefault("providers", {})
        raw_providers = (raw_cfg.get("models") or {}).get("providers") or {}

        for name, disk_pv in raw_providers.items():
            if name not in llm_providers:
                llm_providers[name] = copy.deepcopy(disk_pv)
                continue
            # 模型清单不再落盘：provider 只存自己的 default_model
            llm_providers[name].pop("models", None)
            llm_providers[name]["default_model"] = disk_pv.get("default_model", "") or ""
            llm_providers[name]["base_url"] = disk_pv.get("base_url", "")
            llm_providers[name]["name"] = disk_pv.get("name", "")
            llm_providers[name]["api_key"] = _resolve_secret(
                disk_pv.get("api_key"),
                llm_providers[name].get("api_key"),
            )

        llm_models["roles"] = copy.deepcopy((raw_cfg.get("models") or {}).get("roles") or {})
        agent.llm = LLM(llm_config)
        return True, ""
    except Exception as exc:
        return False, str(exc)


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------

async def execute(envelop, agent):
    action = envelop.payload.get("action", "list_providers")
    payload = envelop.payload if isinstance(envelop.payload, dict) else {}

    raw_cfg = _read_config()
    raw_providers = (raw_cfg.get("models") or {}).get("providers") or {}
    mem_providers = (((agent.config or {}).get("models") or {}).get("providers")) or {}

    # ── 列出所有 provider ──
    if action == "list_providers":
        items = []
        for pn, pv in raw_providers.items():
            has_key, masked, source = _key_info(
                pv.get("api_key"), (mem_providers.get(pn) or {}).get("api_key")
            )
            raw_key = pv.get("api_key")
            env_var = ""
            if isinstance(raw_key, str):
                m = ENV_PATTERN.match(raw_key.strip())
                if m:
                    env_var = m.group(1)
            items.append({
                "provider": pn,
                "name": pv.get("name", ""),
                "base_url": pv.get("base_url", ""),
                "has_key": has_key,
                "key_masked": masked,
                "key_source": source,
                "key_env_var": env_var,
                "default_model": _provider_default_model(pv),
                "is_current": pn == _current_provider_name(raw_cfg),
            })
        default_pn = _default_provider_name(raw_cfg)
        envelop.payload = {
            "ok": True,
            "providers": items,
            "default_provider": default_pn,
            "current_ref": _current_ref(raw_cfg),
            "multi": len(items) > 1,
        }
        return envelop

    # ── 保存 provider（名称 / 端点 / 密钥）──
    if action == "save_provider":
        pn = (payload.get("provider") or "").strip()
        if not pn:
            envelop.payload = {"ok": False, "error": "provider required"}
            return envelop


        name = (payload.get("name") or "").strip()
        base_url = (payload.get("base_url") or "").strip()
        api_key_in = payload.get("api_key")
        key_mode = payload.get("key_mode") or "vault"   # vault / env
        source_pn = (payload.get("source_provider") or "").strip()

        providers = raw_cfg.setdefault("models", {}).setdefault("providers", {})
        # 改 provider 标识时创建新条目，并继承原条目的端点、密钥和已配模型。
        # 这样改名不会覆盖旧 provider，也不会因为密码框留空而丢失密钥。
        renamed_from = ""
        if pn not in providers and source_pn in providers:
            pv = copy.deepcopy(providers[source_pn])
            providers[pn] = pv
            renamed_from = source_pn
        else:
            pv = providers.setdefault(pn, {})

        # 保存 provider 本身也要同步默认引用，不能等用户再次选择模型才更新。
        roles = raw_cfg.setdefault("models", {}).setdefault("roles", {})
        default_entry = roles.get("default")
        if renamed_from and isinstance(default_entry, dict):
            old_ref = str(default_entry.get("model") or "")
            old_prefix = renamed_from + "/"
            if old_ref.startswith(old_prefix):
                default_entry["model"] = pn + "/" + old_ref[len(old_prefix):]
        elif renamed_from and isinstance(default_entry, str):
            old_prefix = renamed_from + "/"
            if default_entry.startswith(old_prefix):
                roles["default"] = pn + "/" + default_entry[len(old_prefix):]
        if name:
            pv["name"] = name
        if base_url:
            pv["base_url"] = base_url

        key_note = ""
        if api_key_in is not None:
            api_key_in = str(api_key_in).strip()
            if api_key_in:
                if key_mode == "env":
                    # 环境变量模式：写成 ${VAR}，真值由用户自行配置到系统环境
                    m = ENV_PATTERN.match(api_key_in)
                    var = m.group(1) if m else api_key_in
                    if not m:
                        var = f"{pn.upper().replace('-', '_')}_API_KEY"
                    pv["api_key"] = f"${{{var}}}"
                    key_note = f"api_key 引用环境变量 {var}"
                else:
                    _sec = _secrets_mod()
                    try:
                        _sec.store(pn, api_key_in)
                    except Exception as exc:
                        envelop.payload = {"ok": False, "error": f"密钥写入失败: {exc}"}
                        return envelop
                    pv["api_key"] = f"{SECRET_PREFIX}{pn}"
                    key_note = "api_key 已加密存入本地密钥库"
            else:
                # 空值 → 保持原样，不动
                key_note = "api_key 未修改"

        # provider 自己的默认模型（各存各的，互不干扰）
        dm_in = (payload.get("default_model") or "").strip()
        if dm_in:
            pv["default_model"] = dm_in

        # 迁移旧结构：models[] → default_model，并移除冗余的 models 列表
        _migrate_provider(pv)
        try:
            _write_config(raw_cfg)
        except Exception as exc:
            envelop.payload = {"ok": False, "error": f"写 aicp.yaml 失败: {exc}"}
            return envelop

        # 同步内存 + 重建 LLM
        llm_ok = True
        llm_providers = ((agent.config or {}).get("models") or {}).setdefault("providers", {})
        llm_providers.setdefault(pn, {})["api_key"] = _resolve_secret(
            pv.get("api_key"), llm_providers.get(pn, {}).get("api_key")
        )
        llm_reloaded, llm_error = _rebuild_llm(agent, raw_cfg)

        has_key, masked, source = _key_info(
            pv.get("api_key"), llm_providers.get(pn, {}).get("api_key")
        )
        envelop.payload = {
            "ok": True,
            "provider": pn,
            "name": pv.get("name", ""),
            "base_url": pv.get("base_url", ""),
            "has_key": has_key,
            "key_masked": masked,
            "key_source": source,
            "note": key_note,
            "llm_reloaded": llm_reloaded,
            "llm_error": llm_error,
        }
        return envelop

    # ── 拉端点模型列表 ──
    if action == "endpoint_models":
        pn = payload.get("provider", "")
        use_cache = not payload.get("force")

        if use_cache and pn in _endpoint_cache:
            hit = _endpoint_cache[pn]
            if time.time() - hit["at"] < _CACHE_TTL:
                envelop.payload = dict(hit["result"], cached=True)
                return envelop

        pv = raw_providers.get(pn)
        if not pv:
            envelop.payload = {"ok": False, "error": f"provider not found: {pn}"}
            return envelop

        base_url = pv.get("base_url", "")
        api_key = _resolve_secret(
            pv.get("api_key"), (mem_providers.get(pn) or {}).get("api_key")
        )
        # 模型清单不再落盘：已配置模型只有 provider 自己的 default_model
        dm = _provider_default_model(pv)
        configured = [{"id": dm, "owned_by": "config"}] if dm else []

        # 无 base_url 或无 key → 直接降级
        if not base_url or not api_key:
            result = {
                "ok": True, "provider": pn, "source": "config",
                "models": configured,
                "note": "provider 缺 base_url 或 api_key，仅显示已配置模型",
            }
            _endpoint_cache[pn] = {"at": time.time(), "result": result}
            envelop.payload = dict(result, cached=False)
            return envelop

        try:
            remote = await _fetch_endpoint_models(base_url, api_key)
            if not remote:
                raise RuntimeError("端点返回空列表")
            models = []
            seen = set()
            for m in remote:
                ref = _ref(pn, m["id"])
                if ref in seen:
                    continue
                seen.add(ref)
                models.append({
                    "id": m["id"], "ref": ref,
                    "owned_by": m.get("owned_by", ""),
                    "configured": any(c["id"] == m["id"] for c in configured),
                })
            result = {
                "ok": True, "provider": pn, "source": "endpoint",
                "base_url": base_url, "count": len(models), "models": models,
            }
        except Exception as exc:
            result = {
                "ok": True, "provider": pn, "source": "config",
                "models": [
                    {"id": c["id"], "ref": _ref(pn, c["id"]),
                     "owned_by": "config", "configured": True}
                    for c in configured
                ],
                "note": f"端点不可用（{str(exc)[:150]}），降级显示已配置模型",
            }

        _endpoint_cache[pn] = {"at": time.time(), "result": result}
        envelop.payload = dict(result, cached=False)
        return envelop

    # ── 当前默认模型 ──
    if action == "current":
        entry = ((raw_cfg.get("models") or {}).get("roles") or {}).get("default", "")
        ref = entry.get("model", "") if isinstance(entry, dict) else str(entry or "")
        pn, mid = _split_ref(ref)
        mts = entry.get("max_tokens") if isinstance(entry, dict) else None
        tmp = entry.get("temperature") if isinstance(entry, dict) else None
        envelop.payload = {
            "ok": True, "ref": ref, "provider": pn or "",
            "model_id": mid or "", "raw": ref,
            "max_tokens": mts, "temperature": tmp,
        }
        return envelop

    # ── 选定模型 ──
    if action == "select_model":
        pn = payload.get("provider", "")
        mid = payload.get("model_id", "")
        if not pn or not mid:
            envelop.payload = {"ok": False, "error": "provider and model_id required"}
            return envelop

        pv = raw_providers.get(pn)
        if not pv:
            envelop.payload = {"ok": False, "error": f"provider not found: {pn}"}
            return envelop

        # 空值 → 沿用 roles.default 里的旧值；都没有才用默认
        _old = ((raw_cfg.get("models") or {}).get("roles") or {}).get("default", {})
        _old = _old if isinstance(_old, dict) else {}
        _mt_in = payload.get("max_tokens")
        _tp_in = payload.get("temperature")
        if _mt_in in (None, ""):
            max_tokens = int(_old.get("max_tokens") or DEFAULT_MAX_TOKENS)
        else:
            max_tokens = int(_mt_in)
        if _tp_in in (None, ""):
            temperature = float(
                _old.get("temperature")
                if _old.get("temperature") is not None
                else DEFAULT_TEMPERATURE
            )
        else:
            temperature = float(_tp_in)

        # provider 各自保存自己的默认模型；models 列表不再落盘
        pv["default_model"] = mid
        pv.pop("models", None)

        raw_cfg.setdefault("models", {}).setdefault("roles", {})
        raw_cfg["models"]["roles"]["default"] = {
            "model": _ref(pn, mid),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        try:
            _write_config(raw_cfg)
        except Exception as exc:
            envelop.payload = {"ok": False, "error": f"写 aicp.yaml 失败: {exc}"}
            return envelop

        llm_reloaded, llm_error = _rebuild_llm(agent, raw_cfg)

        envelop.payload = {
            "ok": True,
            "provider": pn,
            "model_id": mid,
            "ref": _ref(pn, mid),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "added_to_config": False,
            "llm_reloaded": llm_reloaded,
            "llm_error": llm_error,
        }
        return envelop

    # ── 预检拉模型（provider 尚未保存时，用手填的端点+密钥直接拉）──
    if action == "probe_models":
        base_url = (payload.get("base_url") or "").strip()
        api_key_in = payload.get("api_key")
        key_mode = payload.get("key_mode") or "vault"
        provider = (payload.get("provider") or "").strip() or "provider"

        if not base_url:
            envelop.payload = {"ok": False, "error": "base_url required"}
            return envelop

        # 明文优先：直接用传入的明文；否则回落到已存 provider 的密钥
        api_key = ""
        if api_key_in:
            api_key = str(api_key_in).strip()
        if not api_key and provider in raw_providers:
            pv0 = raw_providers[provider]
            api_key = _resolve_secret(
                pv0.get("api_key"), (mem_providers.get(provider) or {}).get("api_key")
            )
        if not api_key and key_mode == "env":
            var = (payload.get("env_var") or "").strip()
            if var:
                api_key = os.environ.get(var, "")

        try:
            remote = await _fetch_endpoint_models(base_url, api_key)
        except Exception as exc:
            envelop.payload = {
                "ok": False, "error": f"{str(exc)[:200]}",
                "hint": "端点不可达或不支持 /models；请检查地址是否含 /v1、密钥是否正确",
            }
            return envelop

        models = []
        for m in remote:
            models.append({"id": m["id"], "ref": _ref(provider, m["id"]), "owned_by": m.get("owned_by", "")})
        envelop.payload = {
            "ok": True, "provider": provider, "source": "endpoint",
            "base_url": base_url, "count": len(models), "models": models,
        }
        return envelop

    envelop.payload = {"ok": False, "error": f"Unknown action: {action}"}
    return envelop
