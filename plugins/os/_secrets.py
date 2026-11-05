"""本地密钥库 — Fernet 加密存储 API Key，密文落盘，明文只在内存

存储：
  data/secrets.json      密文库 {aliases: {alias: {ciphertext, updated_at}}}
  data/.master_key       主密钥（0600），首次运行自动生成

用法：
  api_key: ${ENV_VAR}          → 环境变量（优先）
  api_key: !secret <alias>     → 本密钥库（Fernet 加密）

action:
  set         — {alias, plaintext} 加密后写入
  get         — {alias} 解密返回明文（仅内部使用，勿暴露给前端）
  get_masked  — {alias} 返回掩码 sk-****abcd
  list        — 列出所有 alias + 掩码 + 来源
  delete      — {alias} 删除
  ensure_master_key — 确保主密钥存在
"""
import base64
import json
import os
import time
from pathlib import Path

DATA_DIR = Path("data")
SECRETS_PATH = DATA_DIR / "secrets.json"
MASTER_KEY_PATH = DATA_DIR / ".master_key"
ENV_KEY = "AICP_MASTER_KEY"


def _fernet():
    from cryptography.fernet import Fernet

    return Fernet(_master_key())


def _import_secrets():
    """安全导入本模块（避免相对导入在不同加载方式下失败）。"""
    try:
        from plugins.os import _secrets
        return _secrets
    except Exception:
        import importlib
        return importlib.import_module("plugins.os._secrets")


def _master_key() -> bytes:
    """主密钥：环境变量优先，其次 data/.master_key 文件，都没有则生成。"""
    env = os.environ.get(ENV_KEY)
    if env:
        return env.strip().encode("utf-8")
    if MASTER_KEY_PATH.exists():
        raw = MASTER_KEY_PATH.read_text(encoding="utf-8").strip()
        if raw:
            return raw.encode("utf-8")
    from cryptography.fernet import Fernet

    key = Fernet.generate_key()
    try:
        MASTER_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        MASTER_KEY_PATH.write_bytes(key)
        try:
            os.chmod(MASTER_KEY_PATH, 0o600)
        except Exception:
            pass
    except Exception:
        pass
    return key


def _load() -> dict:
    if not SECRETS_PATH.exists():
        return {"aliases": {}}
    try:
        data = json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"aliases": {}}
    if not isinstance(data, dict):
        return {"aliases": {}}
    data.setdefault("aliases", {})
    return data


def _save(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SECRETS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, SECRETS_PATH)
    try:
        os.chmod(SECRETS_PATH, 0o600)
    except Exception:
        pass


def mask(plain: str) -> str:
    """sk-abcdef... → sk-a****wxyz"""
    if not plain:
        return ""
    s = str(plain)
    if len(s) <= 8:
        return s[0] + "****" if len(s) > 1 else "****"
    return s[:4] + "****" + s[-4:]


def decrypt(alias: str) -> str:
    """按 alias 取明文；不存在返回空串。"""
    data = _load()
    entry = (data.get("aliases") or {}).get(alias)
    if not entry or not entry.get("ciphertext"):
        return ""
    try:
        raw = _fernet().decrypt(entry["ciphertext"].encode("utf-8"))
        return raw.decode("utf-8")
    except Exception:
        return ""


def store(alias: str, plaintext: str) -> None:
    data = _load()
    token = _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")
    data.setdefault("aliases", {})[alias] = {
        "ciphertext": token,
        "updated_at": time.time(),
    }
    _save(data)


def source_of(alias: str) -> str:
    """来源：env / vault / none"""
    if os.environ.get(alias):
        return "env"
    data = _load()
    if alias in (data.get("aliases") or {}):
        return "vault"
    return "none"


async def execute(envelop, agent):
    action = envelop.payload.get("action", "list")
    payload = envelop.payload if isinstance(envelop.payload, dict) else {}

    if action == "set":
        alias = (payload.get("alias") or "").strip()
        plaintext = payload.get("plaintext") or ""
        if not alias:
            envelop.payload = {"ok": False, "error": "alias required"}
            return envelop
        try:
            store(alias, plaintext)
        except Exception as exc:
            envelop.payload = {"ok": False, "error": f"写入密钥失败: {exc}"}
            return envelop
        envelop.payload = {"ok": True, "alias": alias, "masked": mask(plaintext)}
        return envelop

    if action == "get":
        alias = (payload.get("alias") or "").strip()
        plain = decrypt(alias)
        envelop.payload = {"ok": bool(plain), "alias": alias, "plaintext": plain}
        return envelop

    if action == "get_masked":
        alias = (payload.get("alias") or "").strip()
        plain = decrypt(alias)
        envelop.payload = {
            "ok": True, "alias": alias,
            "masked": mask(plain), "source": source_of(alias),
            "has_key": bool(plain),
        }
        return envelop

    if action == "list":
        data = _load()
        items = []
        for alias, entry in (data.get("aliases") or {}).items():
            plain = decrypt(alias)
            items.append({
                "alias": alias,
                "masked": mask(plain),
                "source": source_of(alias),
                "has_key": bool(plain),
                "updated_at": entry.get("updated_at"),
            })
        envelop.payload = {"ok": True, "aliases": items}
        return envelop

    if action == "delete":
        alias = (payload.get("alias") or "").strip()
        data = _load()
        removed = data.get("aliases", {}).pop(alias, None) is not None
        if removed:
            _save(data)
        envelop.payload = {"ok": removed, "alias": alias}
        return envelop

    if action == "ensure_master_key":
        key = _master_key()
        envelop.payload = {
            "ok": True,
            "path": str(MASTER_KEY_PATH),
            "from_env": bool(os.environ.get(ENV_KEY)),
            "key_len": len(key),
        }
        return envelop

    envelop.payload = {"ok": False, "error": f"Unknown action: {action}"}
    return envelop
