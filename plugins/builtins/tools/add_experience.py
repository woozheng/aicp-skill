"""add_experience — 沉淀经验到背包（结构化 + 内置 LLM 归并）

路径：data/memories/main_agent/users/{user_id}/backpack.json
备份：data/memories/main_agent/experiences/history/{user_id}_{ts}.json

backpack.json 结构：
{
  "items": [
    {"id": "exp_001", "title": "...", "summary": "...", "tags": [...],
     "body": "...", "updated_at": "..."}
  ]
}
"""

import json
import re
import uuid
from datetime import datetime
from pathlib import Path


# ============================================================
# 路径
# ============================================================

MEMORY_DIR = Path("data/memories/main_agent")
USER_ROOT = MEMORY_DIR / "users"
HISTORY_DIR = MEMORY_DIR / "experiences" / "history"

# 超过该阈值时 append 自动升级为 LLM 归并（条数或总字符）
MERGE_MAX_ITEMS = 20
MERGE_MAX_CHARS = 5000


def _resolve_user_id(envelop) -> str:
    meta = envelop.meta or {}
    uid = (
        meta.get("user_id")
        or meta.get("auth_result", {}).get("user_id")
        or "local"
    )
    return str(uid).strip() or "local"


def _user_dir(user_id: str) -> Path:
    p = USER_ROOT / user_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def _backpack_file(user_id: str) -> Path:
    return _user_dir(user_id) / "backpack.json"


def _load_backpack(user_id: str) -> dict:
    f = _backpack_file(user_id)
    if not f.exists():
        return {"items": []}
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("items"), list):
            return d
    except Exception:
        pass
    return {"items": []}


def _save_backpack(user_id: str, data: dict) -> None:
    f = _backpack_file(user_id)
    tmp = f.with_suffix(f".tmp.{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(f)


def _backup_backpack(user_id: str, data: dict) -> str:
    if not data.get("items"):
        return ""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    revision = f"{user_id}_{ts}"
    (HISTORY_DIR / f"{revision}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return revision


# ============================================================
# 参数归一化（兼容纯文本 / 结构化）
# ============================================================

def _normalize_experience(params: dict) -> dict:
    """把 append/replace 的入参归一成 {title, summary, tags, body}。

    兼容两种：
    - 旧：{"experience": "纯文本"}
    - 新：{"title": "...", "summary": "...", "tags": [...], "body": "..."}
    """
    body = str(params.get("experience", "") or "").strip()
    title = str(params.get("title", "") or "").strip()
    summary = str(params.get("summary", "") or "").strip()
    tags_raw = params.get("tags", [])
    if isinstance(tags_raw, str):
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    elif isinstance(tags_raw, list):
        tags = [str(t).strip() for t in tags_raw if str(t).strip()]
    else:
        tags = []

    explicit_body = str(params.get("body", "") or "").strip()
    if explicit_body:
        body = explicit_body

    if not body and not title:
        return {}

    if title or explicit_body:
        # 结构化
        return {
            "title": title or (body[:30] if body else ""),
            "summary": summary or (body[:100] if body else ""),
            "tags": tags,
            "body": body or explicit_body,
        }

    # 纯文本：自动截取 title/summary
    first_line = ""
    for ln in body.split("\n"):
        ln = ln.strip()
        if ln:
            first_line = ln
            break
    return {
        "title": (first_line or body)[:30],
        "summary": body[:100],
        "tags": tags,
        "body": body,
    }


def _next_id(items: list) -> str:
    max_n = 0
    for it in items:
        m = re.match(r"exp_(\d+)", str(it.get("id", "")))
        if m:
            try:
                max_n = max(max_n, int(m.group(1)))
            except Exception:
                pass
    return f"exp_{max_n + 1:03d}"


def _should_merge(items: list, new_item: dict) -> bool:
    total_chars = sum(len(str(it.get("body", "") or "")) for it in items)
    total_chars += len(str(new_item.get("body", "") or ""))
    return len(items) >= MERGE_MAX_ITEMS or total_chars >= MERGE_MAX_CHARS


# ============================================================
# LLM 归并（JSON 输入输出）
# ============================================================

MERGE_SYSTEM_PROMPT = """你是经验背包的归并器。输入是当前背包的 items（JSON 数组）和一条新经验（JSON 对象）。

规则：
1. 以【当前背包 items】为基础，把【新经验】里真正的新增、修正合并进去。
2. 如果新经验和某条现有经验语义重复（换措辞但意思一样），合并成一条，保留更清晰、更完整的表述，id 用现有条目的。
3. 如果新经验是现有某条经验的补充，更新那条的 body，updated_at 设成新经验的 updated_at。
4. 如果新经验是全新的，追加一条，id 用新经验给的 id。
5. 保留当前背包里未被新经验触及的条目。
6. 每条必须有 id / title / summary / tags / body / updated_at。
7. 只输出 JSON 数组，不要任何解释、前言、后记、markdown 代码块。

输出格式：
[{"id": "exp_001", "title": "...", "summary": "...", "tags": ["..."], "body": "...", "updated_at": "..."}]"""


async def _merge_items_with_llm(current_items: list, new_item: dict, agent) -> list:
    prompt = (
        MERGE_SYSTEM_PROMPT
        + "\n\n【当前背包 items】\n"
        + json.dumps(current_items, ensure_ascii=False, indent=2)
        + "\n\n【新经验】\n"
        + json.dumps(new_item, ensure_ascii=False, indent=2)
    )
    try:
        raw = await agent.llm.chat([{"role": "user", "content": prompt}])
        raw = (raw or "").strip()
        raw = re.sub(r'^```[a-zA-Z]*\n', '', raw)
        raw = re.sub(r'\n```\s*$', '', raw)
        raw = raw.strip()
        items = json.loads(raw)
        if not isinstance(items, list):
            raise ValueError("归并结果不是数组")
        cleaned = []
        for it in items:
            if not isinstance(it, dict) or not it.get("id"):
                raise ValueError("归并结果条目缺 id")
            cleaned.append({
                "id": str(it.get("id")),
                "title": str(it.get("title", "") or ""),
                "summary": str(it.get("summary", "") or ""),
                "tags": it.get("tags") if isinstance(it.get("tags"), list) else [],
                "body": str(it.get("body", "") or ""),
                "updated_at": str(it.get("updated_at", "") or datetime.now().isoformat()),
            })
        return cleaned
    except Exception as e:
        try:
            print(f"[add_experience] LLM 归并失败，回退追加: {e}")
        except Exception:
            pass
        return current_items + [new_item]


# ============================================================
# 主入口
# ============================================================

async def execute(envelop, agent):
    """沉淀经验到经验背包

    action:
      - append（默认）：追加；超阈值自动升级为 LLM 归并
      - replace：强制 LLM 归并
      - clear：清空
    """
    params = envelop.payload.get("params", {})
    if not isinstance(params, dict) or not params:
        params = {
            k: v for k, v in envelop.payload.items()
            if k not in ("action", "target", "think", "retain")
        }

    user_id = _resolve_user_id(envelop)

    action = str(envelop.payload.get("action", "") or params.get("action", "") or "append").lower()
    if action not in ("append", "replace", "clear"):
        action = "append"

    # clear
    if action == "clear":
        data = _load_backpack(user_id)
        revision = _backup_backpack(user_id, data) if data.get("items") else ""
        _save_backpack(user_id, {"items": []})
        envelop.payload = {
            "ok": True,
            "message": "✅ 经验背包已清空",
            "action": "clear",
            "user_id": user_id,
            "count": 0,
            "revision": revision,
        }
        return envelop

    # append / replace
    norm = _normalize_experience(params)
    if not norm:
        envelop.payload = {"ok": False, "error": "缺少 experience / title / body 参数"}
        return envelop

    try:
        data = _load_backpack(user_id)
        items = data["items"]

        # 分配 id（如果是全新条目）；replace 时若传了 id 则用传的
        if action == "replace" and params.get("id"):
            norm["id"] = str(params["id"]).strip()
        else:
            norm["id"] = _next_id(items)
        norm["updated_at"] = datetime.now().isoformat()

        revision = ""
        merged = False
        diff_summary = ""

        if action == "replace" and items:
            revision = _backup_backpack(user_id, data)
            new_items = await _merge_items_with_llm(items, norm, agent)
            merged = True
            diff_summary = "已 LLM 归并（replace）"
        elif action == "append" and items and _should_merge(items, norm):
            revision = _backup_backpack(user_id, data)
            new_items = await _merge_items_with_llm(items, norm, agent)
            merged = True
            diff_summary = (
                f"背包超阈值（{len(items)} 条 / "
                f"{sum(len(str(it.get('body',''))) for it in items)} 字），已 LLM 归并"
            )
        else:
            new_items = items + [norm]
            diff_summary = "直接追加"

        data["items"] = new_items
        _save_backpack(user_id, data)

        envelop.payload = {
            "ok": True,
            "message": f"✅ 经验已{'归并' if merged else '追加'}，当前 {len(new_items)} 条",
            "action": action,
            "user_id": user_id,
            "count": len(new_items),
            "id": norm["id"],
            "revision": revision,
            "diff": diff_summary,
        }
        return envelop

    except Exception as e:
        envelop.payload = {"ok": False, "error": f"保存经验失败: {e}"}
        return envelop


def help():
    return {
        "route": "builtins/tools/add_experience",
        "description": (
            f"沉淀经验到经验背包（users/{{user_id}}/backpack.json；"
            f"append 追加、条数≥{MERGE_MAX_ITEMS} 或字符≥{MERGE_MAX_CHARS} 自动 LLM 归并 / "
            f"replace 强制归并 / clear 清空）"
        ),
        "input": {
            "action": "append（默认）/ replace / clear",
            "experience": "纯文本经验（旧调用兼容）",
            "title": "结构化：标题",
            "summary": "结构化：一句话摘要",
            "tags": "结构化：标签列表",
            "body": "结构化：全文",
            "id": "replace 时可指定要替换的条目 id",
        },
        "output": {
            "ok": "是否成功",
            "message": "结果消息",
            "action": "使用的操作",
            "user_id": "身份目录",
            "count": "当前条目数",
            "id": "新写入/更新的条目 id",
            "revision": "备份文件名",
            "diff": "归并说明",
        },
    }
