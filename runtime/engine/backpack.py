"""经验背包 —— 失败自动记录 + 坑点回放 + 同指纹拦截。

存储：data/experiences/backpack.json（全局共享，所有 user/agent/session 可见）
"""
import hashlib
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from .paths import experiences_file, experiences_history_dir

_LARGE_FIELD_KEYS = ("content", "text", "code", "html", "css", "js",
                     "diff", "old", "new", "body", "data")


def _backpack_file(agent_id=None, user_id=None):
    """经验背包全局唯一文件；agent_id / user_id 保留兼容旧调用签名。"""
    return experiences_file()


class _FileLock:
    """跨平台可重入文件锁，防止多 user/agent 并发写覆盖。

    同线程同路径重复进入只加一次锁（计数），避免 record_failure → _save
    的嵌套加锁：Windows 下内层退出会误解锁，Linux 下 flock 会死锁。
    """

    _local = threading.local()

    def __init__(self, path, timeout=10.0):
        self.path = Path(str(path) + ".lock")
        self.timeout = timeout
        self._fh = None
        self._depth = 0

    @classmethod
    def _held(cls):
        return getattr(cls._local, "held", {})

    def __enter__(self):
        held = self._held()
        key = str(self.path.resolve())
        if key in held:
            held[key] += 1
            self._depth = 1
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        self._fh = open(self.path, "a+")
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                held[key] = 1
                self._depth = 1
                return self
            except Exception:
                if time.monotonic() >= deadline:
                    try:
                        self._fh.close()
                    except Exception:
                        pass
                    self._fh = None
                    self._depth = 0
                    return self  # 超时降级：不阻塞业务
                time.sleep(0.05)

    def __exit__(self, *exc):
        if self._depth == 0:
            return False
        held = self._held()
        key = str(self.path.resolve())
        if key not in held:
            self._depth = 0
            return False
        held[key] -= 1
        if held[key] > 0:
            self._depth = 0
            return False  # 内层退出：保留外层锁
        held.pop(key, None)
        self._depth = 0
        if self._fh is None:
            return False
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass
        self._fh = None
        return False


def load_backpack(agent_id=None, user_id=None):
    f = _backpack_file()
    if not f.exists():
        return {"items": []}
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("items"), list):
            return d
    except Exception:
        pass
    return {"items": []}


def _save(agent_id, user_id, data):
    f = _backpack_file()
    with _FileLock(f):
        tmp = f.with_suffix(f".tmp.{uuid.uuid4().hex[:8]}")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(f)


def save_backpack(data):
    """公开写接口：原子写 + 文件锁。插件通过 agent.engine.backpack 调用。"""
    if not isinstance(data, dict):
        raise ValueError("backpack 数据必须是 dict")
    items = data.get("items")
    if not isinstance(items, list):
        data = {**data, "items": []}
    _save(None, None, data)
    return data


def list_experiences():
    """读取共享经验条目列表。"""
    return load_backpack().get("items", [])


def _next_exp_id(items):
    """按现有条目分配下一个 exp_XXX 编号。"""
    max_n = 0
    for it in items or []:
        m = re.match(r"exp_(\d+)", str(it.get("id", "")))
        if m:
            try:
                max_n = max(max_n, int(m.group(1)))
            except Exception:
                pass
    return f"exp_{max_n + 1:03d}"


def backup_experiences(data):
    """把当前条目备份到 data/experiences/history/，返回备份文件名；无条目返回空串。"""
    items = (data or {}).get("items") or []
    if not items:
        return ""
    d = experiences_history_dir()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    rev = ts
    (d / f"{rev}.json").write_text(
        json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return rev


def append_experience(item):
    """追加一条经验。返回 {"ok","id","count","message"}。"""
    if not isinstance(item, dict) or not item:
        return {"ok": False, "id": "", "count": 0, "message": "经验内容为空"}
    with _FileLock(_backpack_file()):
        data = load_backpack()
        items = data.get("items", [])
        new_item = dict(item)
        if not str(new_item.get("id", "")).strip():
            new_item["id"] = _next_exp_id(items)
        new_item.setdefault("title", "")
        new_item.setdefault("summary", "")
        new_item.setdefault("tags", [])
        new_item.setdefault("body", "")
        new_item.setdefault("updated_at", datetime.now().isoformat())
        items.append(new_item)
        data["items"] = items
        save_backpack(data)
        return {
            "ok": True,
            "id": new_item["id"],
            "count": len(items),
            "message": f"已追加经验 {new_item['id']}，当前 {len(items)} 条",
        }


def delete_experience(exp_id):
    """删除单条经验。返回 {"ok","message","count","revision"}。"""
    exp_id = str(exp_id or "").strip()
    if not exp_id:
        return {"ok": False, "message": "缺少 id 参数", "count": 0, "revision": ""}
    with _FileLock(_backpack_file()):
        data = load_backpack()
        items = data.get("items", [])
        kept = [it for it in items if str(it.get("id", "")) != exp_id]
        if len(kept) == len(items):
            return {
                "ok": False,
                "message": f"未找到经验 {exp_id}",
                "count": len(items),
                "revision": "",
            }
        revision = backup_experiences(data)
        data["items"] = kept
        save_backpack(data)
        return {
            "ok": True,
            "message": f"已删除经验 {exp_id}，剩余 {len(kept)} 条",
            "count": len(kept),
            "revision": revision,
        }


def clear_experiences():
    """清空共享经验。返回 {"ok","message","count","cleared","revision"}。"""
    with _FileLock(_backpack_file()):
        data = load_backpack()
        count = len(data.get("items", []))
        revision = backup_experiences(data)
        save_backpack({"items": []})
        return {
            "ok": True,
            "message": f"已清空经验，原有 {count} 条",
            "count": 0,
            "cleared": count,
            "revision": revision,
        }


def _sanitize_params(params, max_str=80):
    if not isinstance(params, dict):
        return params
    out = {}
    for k, v in params.items():
        if k in _LARGE_FIELD_KEYS:
            if isinstance(v, str):
                out[k] = f"<{len(v)} 字>"
            elif isinstance(v, (list, dict)):
                out[k] = f"<{type(v).__name__}, {len(v)} 项>"
            else:
                out[k] = f"<{type(v).__name__}>"
        elif isinstance(v, str) and len(v) > max_str:
            out[k] = v[:max_str] + f"…（{len(v)} 字）"
        else:
            out[k] = v
    return out


def _normalize_error_signature(error_text):
    s = str(error_text or "").strip()
    s = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?", "<TS>", s)
    s = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
               "<UUID>", s, flags=re.I)
    s = re.sub(r"(?<![A-Za-z0-9])\d{4,}(?![A-Za-z0-9])", "<NUM>", s)
    return s[:60]


def _error_signature(error_text, error_category=""):
    s = str(error_text or "").strip()
    first_line = s.split("\n")[0] if s else ""
    ec = (error_category or "").strip()
    if ec:
        return ec, _normalize_error_signature(first_line)[:40]
    m = re.match(r"^Plugin execution error:\s*(.+)$", first_line)
    if m:
        inner = m.group(1).strip()
        em = re.match(r"([A-Za-z_][\w.]*(?:Error|Exception))\b", inner)
        et = em.group(1) if em else "PluginException"
        return et, _normalize_error_signature(inner)[:40]
    m = re.match(r"^([A-Za-z_][\w.]*(?:Error|Exception|Warning)?)\s*[:：]", first_line)
    if m:
        phrase = _normalize_error_signature(first_line)
        phrase = re.sub(r"^\w+\s*[:：]\s*", "", phrase)
        return m.group(1), phrase[:40]
    if "不存在" in first_line or "not found" in first_line.lower():
        return "NotFound", _normalize_error_signature(first_line)[:40]
    m = re.search(r"缺少\s*(\w+)\s*参数", first_line)
    if m:
        return "MissingParam", f"缺少 {m.group(1)} 参数"
    if "有多个匹配" in first_line:
        return "AmbiguousPlugin", first_line[:40]
    return "BusinessError", _normalize_error_signature(first_line)[:40]


def _failure_fingerprint(target, action, error_text, error_category=""):
    et, phrase = _error_signature(error_text, error_category)
    return f"{target}|{action}|{et}|{phrase}"


def _param_fingerprint(action, params):
    p = params or {}
    for key in ("code", "content", "html", "script", "source"):
        v = p.get(key)
        if isinstance(v, str) and v.strip():
            return hashlib.md5(v.encode("utf-8")).hexdigest()[:16]
    try:
        s = json.dumps(_sanitize_params(p), ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        s = str(p)
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:16]


def _extract_fix_from_result(result):
    if not isinstance(result, dict):
        return ""
    for key in ("hints", "fix", "suggestion", "usage", "next"):
        v = result.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()[:200]
        if isinstance(v, list) and v:
            return "；".join(str(x) for x in v[:3])[:200]
    return ""


def _infer_fix(params, error_text, error_category=""):
    fixes = []
    ec = (error_category or "").upper()
    if ec == "LLM_CODE":
        mr = re.search(r"【原因】\s*(.+)", error_text)
        reason = mr.group(1).strip() if mr else ""
        fixes.append("代码写错了：读 error 的【原因】改代码，重新提交"
                     + (f"；根因：{reason[:80]}" if reason else ""))
    elif ec == "SANDBOX_BLOCK":
        fixes.append("沙箱拦截：换实现（while True → 有限循环）")
    elif ec == "RUNTIME_INTERNAL":
        fixes.append("后端问题，代码没问题：告知用户")
    elif ec == "DEPENDENCY":
        fixes.append("依赖安装失败：换库或换方案")
    if "slice indices" in error_text:
        for k, v in (params or {}).items():
            if isinstance(v, dict):
                fixes.append(f"`{k}` 传了 dict，应传 list")
    if "'str' object has no attribute 'receiver'" in error_text:
        fixes.append("`agent.system.call` 要传 `core.Envelop(...)`，不能传字符串")
    _txt = (error_text or "").strip()
    if _txt.startswith("Plugin execution error:"):
        fixes.append("插件内部异常：读堆栈定位异常源，修插件源码")
    return "；".join(fixes)


def record_failure(agent_id, user_id, target, action, params,
                   error_source, error_text, result=None, error_category=""):
    try:
        if not target or target.startswith("builtins/agents/"):
            return
        et, phrase = _error_signature(error_text, error_category)
        fp = _failure_fingerprint(target, action, error_text, error_category)
        param_fp = _param_fingerprint(action, params or {})
        now = datetime.now().isoformat()
        fix = _extract_fix_from_result(result) if result else ""
        if not fix:
            fix = _infer_fix(params or {}, error_text, error_category)
        f = _backpack_file()
        with _FileLock(f):
            data = load_backpack(agent_id, user_id)
            items = data.get("items", [])
            for it in items:
                if it.get("fingerprint") == fp:
                    it["count"] = int(it.get("count", 1)) + 1
                    it["last_seen"] = now
                    it["param_fp"] = param_fp
                    if not it.get("fix") and fix:
                        it["fix"] = fix
                    _save(agent_id, user_id, data)
                    return
            max_n = 0
            for it in items:
                m = re.match(r"exp_(\d+)", str(it.get("id", "")))
                if m:
                    try:
                        max_n = max(max_n, int(m.group(1)))
                    except Exception:
                        pass
            sanitized = _sanitize_params(params or {})
            params_snapshot = json.dumps(sanitized, ensure_ascii=False, default=str)[:300]
            items.append({
                "id": f"exp_{max_n + 1:03d}",
                "title": f"{target.split('/')[-1]} 失败：{phrase[:30]}",
                "summary": f"调用 {target}（action={action or 'none'}）失败：{et}: {phrase}",
                "target": target, "action": action or "none",
                "tags": ["失败", target.split("/")[-1], error_source, et],
                "owner": {"user_id": user_id, "agent_id": agent_id},
                "body": (
                    f"触发条件：调用 {target}（action={action or 'none'}）\n"
                    f"错误类型：{et}\n错误原文：{error_text[:500]}\n"
                    f"当时参数：{params_snapshot}\n"
                    + (f"正确做法：{fix}\n" if fix else "")
                ),
                "params_snapshot": params_snapshot,
                "fix": fix, "fingerprint": fp, "param_fp": param_fp,
                "count": 1, "error_type": et, "error_source": error_source,
                "first_seen": now, "last_seen": now,
            })
            if len(items) > 20:
                items.sort(key=lambda x: (int(x.get("count", 1)), str(x.get("last_seen", ""))))
                items = items[-20:]
            items.sort(key=lambda x: (-int(x.get("count", 1)), str(x.get("last_seen", ""))))
            data["items"] = items
            _save(agent_id, user_id, data)
    except Exception as e:
        print(f"[backpack][WARN] 写失败记录失败: {e}")


def load_failure_index(agent_id, user_id):
    data = load_backpack(agent_id, user_id)
    fails = [it for it in data.get("items", []) if "失败" in (it.get("tags") or [])]
    if not fails:
        return ""
    fails.sort(key=lambda x: (-int(x.get("count", 1)), str(x.get("last_seen", ""))))
    lines = []
    for it in fails:
        count = int(it.get("count", 1))
        lines.append(f"- ❌ [{it.get('id','')}] {it.get('title','')} ×{count}")
        fix = it.get("fix", "")
        if fix:
            lines.append(f"    ✅ 正确做法：{fix}")
    return "\n".join(lines)


def check_failure_lock(agent_id, user_id, target, action, params):
    if not target or target.startswith("builtins/agents/"):
        return ""
    try:
        data = load_backpack(agent_id, user_id)
        cur_fp = _param_fingerprint(action, params or {})
        for it in data.get("items", []):
            if it.get("target") != target:
                continue
            m = re.search(r"action=([^）]*)", it.get("summary", "") or "")
            if (m.group(1) if m else "none") != (action or "none"):
                continue
            if int(it.get("count", 1)) < 3:
                continue
            if it.get("param_fp") and it.get("param_fp") != cur_fp:
                continue
            eid = it.get("id", "")
            fix = it.get("fix", "") or ""
            return (f"同一调用已失败 {int(it.get('count', 1))} 次（{eid}），本轮拒绝执行。"
                    f"先读经验背包 {eid} 的正确做法：{fix or '先用 contract_agent.get 看参数'}。")
    except Exception:
        return ""
    return ""
