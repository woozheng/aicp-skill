"""模型选择器 — 从 provider 端点拉取模型列表并切换默认模型

与 builtins/control/_settings 隔离（用户要求），互不影响。

action:
  list_providers  — 列出 provider（名字 / base_url / 已配模型数）
  endpoint_models — 出网 GET {base_url}/models，返回该端点全部模型；失败降级为已配置模型
  select_model    — 选定 provider+model → 写 aicp.yaml → 重建 LLM
  current         — 当前默认模型（拆回 provider / 裸 model id）
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

DEFAULT_MAX_TOKENS = 8192
DEFAULT_TEMPERATURE = 1.0

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


def _resolve_secret(raw, mem_raw):
    """${VAR} → os.environ；取不到则回退内存里（可能已展开）的值。"""
    if isinstance(raw, str):
        m = ENV_PATTERN.match(raw.strip())
        if m:
            return os.environ.get(m.group(1), "") or (mem_raw or "")
    return raw or (mem_raw or "")


def _ref(provider, model_id):
    return f"{provider}/{model_id}"


def _split_ref(ref):
    if isinstance(ref, str) and "/" in ref:
        return ref.split("/", 1)
    return None, ref


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
            items.append({
                "provider": pn,
                "name": pv.get("name", ""),
                "base_url": pv.get("base_url", ""),
                "has_key": bool(_resolve_secret(
                    pv.get("api_key"),
                    (mem_providers.get(pn) or {}).get("api_key"),
                )),
                "configured_count": len(pv.get("models") or []),
            })
        envelop.payload = {"ok": True, "providers": items}
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
        configured = [
            {"id": m.get("id"), "owned_by": "config"}
            for m in (pv.get("models") or []) if m.get("id")
        ]

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

        models = list(pv.get("models") or [])
        idx = next((i for i, m in enumerate(models) if m.get("id") == mid), -1)
        if idx >= 0:
            models[idx] = {**models[idx], "max_tokens": max_tokens,
                           "temperature": temperature}
        else:
            models.append({
                "id": mid, "max_tokens": max_tokens,
                "temperature": temperature, "supports_streaming": True,
            })
        pv["models"] = models

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

        # ── 重建 LLM：磁盘配置里的 api_key 是 ${ENV}，必须用内存里已展开的值 ──
        llm_reloaded = False
        llm_error = None
        try:
            from runtime._llm import LLM

            llm_config = agent.config or {}
            llm_models = llm_config.setdefault("models", {})
            llm_providers = llm_models.setdefault("providers", {})

            for name, disk_pv in raw_providers.items():
                if name not in llm_providers:
                    continue
                llm_providers[name]["models"] = copy.deepcopy(disk_pv.get("models") or [])

            llm_models["roles"] = copy.deepcopy(raw_cfg["models"]["roles"])
            agent.llm = LLM(llm_config)
            llm_reloaded = True
        except Exception as exc:
            llm_error = str(exc)

        envelop.payload = {
            "ok": True,
            "provider": pn,
            "model_id": mid,
            "ref": _ref(pn, mid),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "added_to_config": idx < 0,
            "llm_reloaded": llm_reloaded,
            "llm_error": llm_error,
        }
        return envelop

    envelop.payload = {"ok": False, "error": f"Unknown action: {action}"}
    return envelop