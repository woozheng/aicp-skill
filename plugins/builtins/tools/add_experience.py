"""add_experience — 共享经验背包的薄转发层。

存储：data/experiences/backpack.json（全局共享，所有 user/agent/session 可见）
备份：data/experiences/history/{ts}.json

所有读写一律转发到 agent.engine.backpack（runtime/engine/backpack.py），
与引擎侧失败记录、前端读取使用同一份文件、同一把文件锁、同一套路径。
本文件不再自行维护读写/加锁/路径逻辑。

action:
  - list：列出全部经验（前端「弹出经验」用）
  - append（默认）：追加一条；超阈值自动升级为 LLM 归并
  - replace：强制 LLM 归并
  - delete：删除单条
  - clear：清空
"""

import json
import re
from datetime import datetime


# ============================================================
# 引擎背包句柄
# ============================================================

def _backpack(agent):
    """取得引擎的 backpack 模块；不可用时返回 None。"""
    engine = getattr(agent, "engine", None)
    bp = getattr(engine, "backpack", None)
    if bp is not None and hasattr(bp, "save_backpack"):
        return bp
    return None


def _require_backpack(agent):
    bp = _backpack(agent)
    if bp is None:
        raise RuntimeError(
            "引擎背包未就绪（agent.engine.backpack 缺失），请重启服务后再试"
        )
    return bp


# ============================================================
# 参数归一化（兼容纯文本 / 结构化）
# ============================================================

def _normalize_experience(params: dict) -> dict:
    """把 append/replace 的入参归一成 {title, summary, tags, body}。

    兼容两种：
    - 旧：{"experience": "纯文本"}
    - 新：{"title": ..., "summary": ..., "tags": [...], "body": ...}
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
        return {
            "title": title or (body[:30] if body else ""),
            "summary": summary or (body[:100] if body else ""),
            "tags": tags,
            "body": body or explicit_body,
        }

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


def _should_merge(items: list, new_item: dict) -> bool:
    total_chars = sum(len(str(it.get("body", "") or "")) for it in items)
    total_chars += len(str(new_item.get("body", "") or ""))
    return len(items) >= MERGE_MAX_ITEMS or total_chars >= MERGE_MAX_CHARS


# 超过该阈值时 append 自动升级为 LLM 归并（条数或总字符）
MERGE_MAX_ITEMS = 20
MERGE_MAX_CHARS = 5000


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
    """共享经验背包的薄转发层（读写全部委托给 agent.engine.backpack）。

    action:
      - list（新增）：列出全部经验
      - append（默认）：追加；超阈值自动升级为 LLM 归并
      - replace：强制 LLM 归并
      - delete：删除单条
      - clear：清空
    """
    params = envelop.payload.get("params", {})
    if not isinstance(params, dict) or not params:
        params = {
            k: v for k, v in envelop.payload.items()
            if k not in ("action", "target", "think", "retain")
        }

    action = str(envelop.payload.get("action", "") or params.get("action", "") or "append").lower()
    if action not in ("list", "append", "replace", "clear", "delete"):
        action = "append"

    try:
        bp = _require_backpack(agent)
    except Exception as e:
        envelop.payload = {"ok": False, "error": str(e)}
        return envelop

    # ---- list：列出全部经验 ----
    if action == "list":
        items = bp.list_experiences()
        envelop.payload = {
            "ok": True,
            "action": "list",
            "count": len(items),
            "items": items,
        }
        return envelop

    # ---- delete：删除单条 ----
    if action == "delete":
        target_id = str(params.get("id", "") or "").strip()
        if not target_id:
            envelop.payload = {"ok": False, "error": "缺少 id 参数"}
            return envelop
        res = bp.delete_experience(target_id)
        envelop.payload = {
            "ok": bool(res.get("ok")),
            "message": ("✅ " if res.get("ok") else "❌ ") + str(res.get("message", "")),
            "action": "delete",
            "id": target_id,
            "count": res.get("count", 0),
            "revision": res.get("revision", ""),
        }
        if not res.get("ok"):
            envelop.payload["error"] = res.get("message", "删除失败")
        return envelop

    # ---- clear：清空 ----
    if action == "clear":
        res = bp.clear_experiences()
        envelop.payload = {
            "ok": True,
            "message": f"✅ 经验背包已清空（原 {res.get('cleared', 0)} 条）",
            "action": "clear",
            "count": 0,
            "cleared": res.get("cleared", 0),
            "revision": res.get("revision", ""),
        }
        return envelop

    # ---- append / replace ----
    norm = _normalize_experience(params)
    if not norm:
        envelop.payload = {"ok": False, "error": "缺少 experience / title / body 参数"}
        return envelop

    try:
        items = bp.list_experiences()

        # 分配 id（replace 时若传了 id 则用传入的）
        if action == "replace" and params.get("id"):
            norm["id"] = str(params["id"]).strip()
        else:
            norm["id"] = ""
        norm["updated_at"] = datetime.now().isoformat()

        merged = False
        diff_summary = ""

        if action == "replace" and items:
            new_items = await _merge_items_with_llm(items, norm, agent)
            merged = True
            diff_summary = "已 LLM 归并（replace）"
        elif action == "append" and items and _should_merge(items, norm):
            new_items = await _merge_items_with_llm(items, norm, agent)
            merged = True
            diff_summary = (
                f"背包超阈值（{len(items)} 条 / "
                f"{sum(len(str(it.get('body',''))) for it in items)} 字），已 LLM 归并"
            )
        else:
            # 直接追加：交给引擎分配 id（append_experience 内部带 _next_exp_id）
            res = bp.append_experience(norm)
            new_items = items + [norm]
            diff_summary = "直接追加"
            new_id = res.get("id", "")
            envelop.payload = {
                "ok": True,
                "message": f"✅ 经验已追加，当前 {len(new_items)} 条",
                "action": action,
                "count": len(new_items),
                "id": new_id,
                "diff": diff_summary,
            }
            return envelop

        bp.save_backpack({"items": new_items})
        new_id = norm.get("id") or ""

        envelop.payload = {
            "ok": True,
            "message": f"✅ 经验已{'归并' if merged else '追加'}，当前 {len(new_items)} 条",
            "action": action,
            "count": len(new_items),
            "id": new_id,
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
            "共享经验背包的薄转发层（读写委托给 agent.engine.backpack）；"
            f"list 列全部 / append 追加（条数≥{MERGE_MAX_ITEMS} 或字符≥{MERGE_MAX_CHARS} 自动 LLM 归并）/ "
            "replace 强制归并 / delete 删单条 / clear 清空"
        ),
        "input": {
            "action": "list / append（默认）/ replace / clear / delete",
            "experience": "纯文本经验（旧调用兼容）",
            "title": "结构化：标题",
            "summary": "结构化：一句话摘要",
            "tags": "结构化：标签列表",
            "body": "结构化：全文",
            "id": "delete 时指定条目 id；replace 时指定要替换的条目 id",
        },
        "output": {
            "ok": "是否成功",
            "message": "结果消息",
            "action": "使用的操作",
            "count": "当前条目数",
            "items": "list 时返回的全部条目",
            "id": "新写入/更新的条目 id",
            "revision": "备份文件名",
            "diff": "归并说明",
        },
    }