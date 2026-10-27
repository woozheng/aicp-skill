"""os/_mcp_agent —— 外部 MCP agent 的工作台（经验背包 + 任务看板）

设计定调（个人版 MCP，按 user_id 分目录）：
- 数据按 user_id 分：data/mcp_agent/users/{user_id}/
- 同一 user 换客户端原地不动；不同 user 物理隔离
- user_id 从 envelop.meta 取，缺省 local（兼容无 token 场景）
- clientInfo 只记录不鉴权，用于回答"上次是哪个客户端来的"
- 存储只解决"不丢"；initialize 注入摘要才解决"接着干"
- 写盘：备份(留 5 份) → tmp → os.replace → 读回校验

动作：
  memory_append / memory_search / memory_replace / memory_clear
  tasks_list / tasks_upsert / tasks_close / tasks_park
  resume / summary / profile / init
"""

import asyncio
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

DATA_NAME = "mcp_agent"
EXP_SOFT_LIMIT = 8 * 1024
BACKUP_KEEP = 5
SUMMARY_LIMIT = 1200

_LOCK = asyncio.Lock()

_EXP_RE = re.compile(r"^- E(\d+) \[([a-z]+)\] (.*)$")
_SECRETS = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "sk-***"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-_.=]{8,}"), r"\1***"),
    (re.compile(r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key)\b\s*[=:]\s*\S+"), r"\1=***"),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "***"),
]


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _scrub(text):
    s = str(text or "")
    for pat, rep in _SECRETS:
        s = pat.sub(rep, s)
    return s



def _norm_key(text):
    t = re.sub(r"[\s\-—:：,，。.;；!！?？()（）\[\]|]+", "", str(text or ""))
    return hashlib.md5(t[:120].encode("utf-8")).hexdigest()[:12]


def _dedupe_key(title, body):
    """去重键 = 标题 + 正文首段（剥掉 "标题 → " 前缀和 " | tags..." 尾巴）。"""
    rest = str(body or "").split(" → ", 1)
    core = (rest[1] if len(rest) > 1 else str(body or "")).split(" | ")[0].strip()
    return _norm_key(str(title or "") + "|" + core)


def _dirs(agent, user_id: str = "local"):
    """按 user_id 分目录。缺省 local（兼容旧调用）。"""
    d = Path(agent.data_dir) / DATA_NAME / "users" / (user_id or "local")
    (d / "backups").mkdir(parents=True, exist_ok=True)
    return d, d / "experience.md", d / "tasks.json", d / "profile.json"


def _atomic_write(path, text, agent, backup=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        bdir = path.parent / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        try:
            (bdir / f"{path.stem}-{stamp}{path.suffix}").write_text(
                path.read_text(encoding="utf-8"), encoding="utf-8"
            )
        except Exception:
            pass
        olds = sorted(bdir.glob(f"{path.stem}-*{path.suffix}"))
        for old in olds[:-BACKUP_KEEP]:
            try:
                old.unlink()
            except Exception:
                pass
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    if path.read_text(encoding="utf-8") != text:
        raise RuntimeError(f"写出校验失败: {path.name}")


def _read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _read_exp_lines(path):
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []


def _exp_items(lines):
    items = []
    for ln in lines:
        m = _EXP_RE.match(ln.strip())
        if not m:
            continue
        body = m.group(3)
        title = body.split(" → ")[0].split(" | ")[0][:80]
        # body 是 "标题 → 正文 | tags... | project... | 时间"，剥出纯正文用于去重比较
        rest = body.split(" → ", 1)
        raw = (rest[1] if len(rest) > 1 else body).split(" | ")[0].strip()
        items.append({
            "id": int(m.group(1)),
            "kind": m.group(2),
            "title": title,
            "body": body,
            "raw": raw,
            "line": ln,
        })
    return items


def _next_exp_id(lines):
    ids = []
    for ln in lines:
        m = _EXP_RE.match(ln.strip())
        if m:
            ids.append(int(m.group(1)))
    return (max(ids) + 1) if ids else 1


def _load_tasks(task_path):
    data = _read_json(task_path, {"tasks": []})
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        data = {"tasks": []}
    return data


def _task_id_num(tid):
    m = re.search(r"(\d+)", str(tid or ""))
    return int(m.group(1)) if m else 0


def _next_task_id(tasks):
    nums = [_task_id_num(t.get("id")) for t in tasks] or [0]
    return "t-%d" % (max(nums) + 1)


def _touch_profile(agent, client, prof_path, calls=0):
    prof = _read_json(prof_path, {})
    if not isinstance(prof, dict):
        prof = {}
    prof.setdefault("first_seen", _now())
    prof["last_seen"] = _now()
    prof["calls"] = int(prof.get("calls", 0)) + int(calls)
    if client:
        clients = prof.setdefault("clients", {})
        c = clients.setdefault(client, {"count": 0})
        c["count"] = int(c.get("count", 0)) + 1
        c["last"] = _now()
    _atomic_write(prof_path, json.dumps(prof, ensure_ascii=False, indent=2), agent, backup=False)
    return prof


def _build_text(tasks, exp_items, prof, limit=SUMMARY_LIMIT):
    lines = ["## 你的工作台（服务器侧持久，换客户端不丢）"]
    last = prof.get("last_seen") or "（首次接入）"
    who = ""
    clients = prof.get("clients") or {}
    if clients:
        cname = max(clients.items(), key=lambda kv: str(kv[1].get("last", "")))[0]
        who = f"（上次来自 {cname}）"
    lines.append(f"上次活动：{last}{who}")

    doing = [t for t in tasks if t.get("status") == "doing"]
    blocked = [t for t in tasks if t.get("status") == "blocked"]
    paused = [t for t in tasks if t.get("status") == "paused"]
    todo = [t for t in tasks if t.get("status") == "todo"]

    focus = sorted(doing, key=lambda t: str(t.get("updated_at", "")), reverse=True)[:1]
    if focus:
        lines.append("")
        lines.append("## 有一个任务在跑，先跟用户确认是否继续")
        for t in focus:
            lines.append(f"- {t.get('id')} {t.get('title', '')}（{t.get('project') or '未归项目'}）")
            lines.append(f"  下一步：{t.get('next_action') or '（未填，先问用户）'}")
            anchor = t.get("anchor") or {}
            bits = []
            for k in ("url", "api", "files"):
                v = anchor.get(k)
                if v:
                    bits.append(f"{k}={v if isinstance(v, str) else ', '.join(map(str, v))}")
            if bits:
                lines.append("  锚点：" + " | ".join(bits))
            if t.get("tried"):
                lines.append("  已试过（别重走）：" + "；".join(map(str, t["tried"][-3:])))
            if t.get("progress"):
                lines.append(f"  进度：{t['progress']}")
    if blocked:
        lines.append("")
        lines.append("## 卡住的任务")
        for t in blocked[:2]:
            lines.append(f"- {t.get('id')} {t.get('title', '')} | 卡点：{t.get('next_action') or t.get('progress') or '未知'}")

    tail = []
    if len(doing) > len(focus):
        tail.append("另有 %d 个 doing" % (len(doing) - len(focus)))
    if paused:
        tail.append("%d 个已搁置" % len(paused))
    if todo:
        tail.append("%d 个待办" % len(todo))
    if tail:
        lines.append("\n其余：" + "，".join(tail) + "（完整用 my_tasks list）")

    if exp_items:
        lines.append("")
        lines.append("## 你踩过的坑（只给标题，正文用 my_memory search 取）")
        for it in exp_items[-6:]:
            lines.append(f"- E{it['id']} [{it['kind']}] {it['title']}")

    lines.append("")
    lines.append("## 协议")
    lines.append("1. 开工前先 my_tasks resume，把上面任务念给用户，问“继续吗”。用户确认才往下做；说先不做就 my_tasks park。")
    lines.append("2. 每完成一步就 my_tasks upsert 更新 progress / next_action / anchor，不要等收尾才写。")
    lines.append("3. 收尾 my_tasks close，并至少 my_memory append 一条（内容用用户原话，你只补 tags / project）。")
    lines.append("4. 经验只记踩过且验证过的坑或做法，kind = pitfall | howto | decision。")

    text = "\n".join(lines)
    if len(text) > limit:
        text = text[: limit - 60] + "\n…（已裁剪，完整内容调 my_tasks list / my_memory search）"
    return text


async def execute(envelop, agent):
    try:
        payload = envelop.payload.get("payload", envelop.payload)
        action = str(payload.get("action") or "").strip()
        # ★ 身份级：从 meta 取 user_id（MCP 侧已透传），缺省 local
        user_id = str((envelop.meta or {}).get("user_id") or "local").strip() or "local"
        data_dir, exp_path, task_path, prof_path = _dirs(agent, user_id)
        client = str(payload.get("client") or "").strip()

        if action == "init":
            if not exp_path.exists():
                _atomic_write(exp_path, "", agent, backup=False)
            if not task_path.exists():
                _atomic_write(task_path, json.dumps({"tasks": []}, ensure_ascii=False, indent=2), agent, backup=False)
            envelop.payload = {"ok": True, "data": {"dir": str(data_dir)}}
            return envelop

        # ---------------- 经验背包 ----------------
        if action == "memory_append":
            async with _LOCK:
                lines = _read_exp_lines(exp_path)
                items = _exp_items(lines)
                kind = str(payload.get("kind") or "pitfall").strip().lower()
                if kind not in ("pitfall", "howto", "decision"):
                    kind = "pitfall"
                title = str(payload.get("title") or "").strip()
                body = str(payload.get("experience") or payload.get("body") or "").strip()
                if not title and not body:
                    envelop.payload = {"ok": False, "error": "需要 title 或 experience"}
                    return envelop
                if not title:
                    title = body.splitlines()[0][:80]
                if not body:
                    body = title
                title = _scrub(title)
                body = _scrub(body)
                tags = payload.get("tags") or []
                if isinstance(tags, str):
                    tags = [t for t in re.split(r"[,，\s]+", tags) if t]
                project = str(payload.get("project") or "").strip()
                # 去重键只看 标题 + 正文首段：tags / project / 时间戳不参与。
                # items 里的 body 是 "标题 → 正文 | tags..."，必须先剥出正文再比，
                # 否则同一条经验每次都会被当成新条目写进去。
                key = _dedupe_key(title, body)
                for it in items:
                    if _dedupe_key(it["title"], it.get("raw") or it["body"]) == key:
                        envelop.payload = {
                            "ok": True,
                            "duplicate_of": "E%d" % it["id"],
                            "hint": "已存在相似条目；要改就 memory_replace(id=...)",
                        }
                        return envelop
                eid = _next_exp_id(lines)
                row = "- E%d [%s] %s → %s" % (eid, kind, title, body)
                if tags:
                    row += " | tags: " + ",".join(map(str, tags))
                if project:
                    row += " | project: " + project
                row += " | " + _now()
                lines.append(row)
                content = "\n".join([l for l in lines if l.strip()]) + "\n"
                _atomic_write(exp_path, content, agent)
                size = len(content.encode("utf-8"))
                out = {"ok": True, "id": "E%d" % eid, "size": size, "line": row}
                if size > EXP_SOFT_LIMIT:
                    out["warning"] = "经验背包已 %d 字节（超过 %d），建议 memory_replace 归并同义条目" % (size, EXP_SOFT_LIMIT)
                envelop.payload = out
                return envelop

        if action == "memory_search":
            terms = payload.get("terms") or payload.get("keywords") or []
            if isinstance(terms, str):
                terms = [t for t in re.split(r"[,，\s]+", terms) if t]
            q = str(payload.get("query") or "").strip()
            if q:
                terms = list(terms) + [t for t in re.split(r"[,，\s]+", q) if t]
            terms = [str(t).lower() for t in terms if str(t).strip()]
            try:
                limit = int(payload.get("limit") or 5)
            except (TypeError, ValueError):
                limit = 5
            items = _exp_items(_read_exp_lines(exp_path))
            results = []
            for it in items:
                hay = it["line"].lower()
                score = sum(hay.count(t) for t in terms) if terms else 1
                if terms and score == 0:
                    continue
                results.append({
                    "id": "E%d" % it["id"],
                    "kind": it["kind"],
                    "title": it["title"],
                    "text": it["body"],
                    "score": score,
                })
            results.sort(key=lambda r: r["score"], reverse=True)
            envelop.payload = {"ok": True, "results": results[:limit], "total": len(results)}
            return envelop

        if action == "memory_replace":
            async with _LOCK:
                raw = str(payload.get("id") or "").strip().upper()
                m_id = re.search(r"(\d+)", raw)
                if not m_id:
                    envelop.payload = {"ok": False, "error": "id 必须是 E12 或 12"}
                    return envelop
                tid = int(m_id.group(1))
                lines = _read_exp_lines(exp_path)
                idx = None
                for i, ln in enumerate(lines):
                    m = _EXP_RE.match(ln.strip())
                    if m and int(m.group(1)) == tid:
                        idx = i
                        break
                if idx is None:
                    envelop.payload = {"ok": False, "error": "未找到 E%d" % tid}
                    return envelop
                old = lines[idx]
                m = _EXP_RE.match(old.strip())
                kind = str(payload.get("kind") or m.group(2)).strip().lower()
                old_body = m.group(3)
                title = str(payload.get("title") or old_body.split(" → ")[0][:80]).strip()
                body = str(payload.get("experience") or payload.get("body") or "").strip()
                if not body:
                    body = old_body.split(" → ", 1)[1] if " → " in old_body else old_body
                row = "- E%d [%s] %s → %s | %s" % (tid, kind, _scrub(title), _scrub(body), _now())
                lines[idx] = row
                _atomic_write(exp_path, "\n".join([l for l in lines if l.strip()]) + "\n", agent)
                envelop.payload = {"ok": True, "id": "E%d" % tid, "before": old, "after": row}
                return envelop

        if action == "memory_clear":
            if not payload.get("confirm"):
                envelop.payload = {"ok": False, "error": "清空经验需要 confirm=true"}
                return envelop
            async with _LOCK:
                count = len(_exp_items(_read_exp_lines(exp_path)))
                _atomic_write(exp_path, "", agent)
                envelop.payload = {"ok": True, "cleared": count}
                return envelop

        # ---------------- 任务看板 ----------------
        if action == "tasks_list":
            data = _load_tasks(task_path)
            tasks = data["tasks"]
            st = str(payload.get("status") or "").strip()
            project = str(payload.get("project") or "").strip()
            if st:
                tasks = [t for t in tasks if t.get("status") == st]
            if project:
                tasks = [t for t in tasks if t.get("project") == project]
            try:
                limit = int(payload.get("limit") or 50)
            except (TypeError, ValueError):
                limit = 50
            ordered = sorted(tasks, key=lambda t: str(t.get("updated_at", "")), reverse=True)
            envelop.payload = {"ok": True, "tasks": ordered[:limit], "total": len(tasks)}
            return envelop

        if action == "tasks_upsert":
            async with _LOCK:
                data = _load_tasks(task_path)
                tasks = data["tasks"]
                tid = str(payload.get("id") or "").strip()
                task = None
                if tid:
                    task = next((t for t in tasks if t.get("id") == tid), None)
                    if task is None:
                        envelop.payload = {"ok": False, "error": "未找到 %s（新建任务不要传 id）" % tid}
                        return envelop
                    expect_rev = payload.get("expect_rev")
                    if expect_rev is not None:
                        try:
                            if int(expect_rev) != int(task.get("rev", 1)):
                                envelop.payload = {
                                    "ok": False,
                                    "error": "rev 冲突：当前 rev=%s，请重新 tasks_list 后重试" % task.get("rev"),
                                }
                                return envelop
                        except (TypeError, ValueError):
                            pass
                else:
                    task = {
                        "id": _next_task_id(tasks),
                        "title": "",
                        "project": "",
                        "status": "todo",
                        "priority": 0,
                        "next_action": "",
                        "progress": "",
                        "anchor": {},
                        "tried": [],
                        "history": [],
                        "exp_ids": [],
                        "rev": 1,
                        "created_at": _now(),
                        "updated_at": _now(),
                    }
                    tasks.append(task)

                status = payload.get("status")
                if status:
                    status = str(status).strip().lower()
                    if status not in ("todo", "doing", "paused", "blocked", "done"):
                        envelop.payload = {
                            "ok": False,
                            "error": "status 只能是 todo/doing/paused/blocked/done，收到 %s" % status,
                        }
                        return envelop
                    task["status"] = status

                for field in ("title", "project", "next_action", "progress"):
                    if payload.get(field) is not None:
                        task[field] = _scrub(payload.get(field))
                if payload.get("priority") is not None:
                    try:
                        task["priority"] = int(payload.get("priority"))
                    except (TypeError, ValueError):
                        pass
                if isinstance(payload.get("anchor"), dict):
                    anchor = dict(task.get("anchor") or {})
                    anchor.update({k: v for k, v in payload["anchor"].items() if v})
                    task["anchor"] = anchor
                if payload.get("tried"):
                    tried = payload["tried"]
                    if isinstance(tried, str):
                        tried = [tried]
                    task["tried"] = list(task.get("tried") or []) + [_scrub(x) for x in tried]
                if payload.get("exp_id"):
                    ids = list(task.get("exp_ids") or [])
                    m = re.search(r"(\d+)", str(payload["exp_id"]))
                    if m and m.group(1) not in ids:
                        ids.append(m.group(1))
                    task["exp_ids"] = ids

                note = str(payload.get("note") or "").strip()
                hist = list(task.get("history") or [])
                hist.append({"at": _now(), "client": client or "unknown", "note": _scrub(note) if note else "upsert"})
                task["history"] = hist[-20:]

                demoted = []
                if task.get("status") == "doing":
                    for t in tasks:
                        if t is not task and t.get("status") == "doing":
                            t["status"] = "paused"
                            t["rev"] = int(t.get("rev", 1)) + 1
                            t["updated_at"] = _now()
                            demoted.append(t.get("id"))

                task["rev"] = int(task.get("rev", 1)) + 1
                task["updated_at"] = _now()
                _atomic_write(task_path, json.dumps(data, ensure_ascii=False, indent=2), agent)
                out = {"ok": True, "task": task}
                if demoted:
                    out["warning"] = "doing 只允许 1 个焦点，已把 %s 降为 paused" % ", ".join(demoted)
                envelop.payload = out
                return envelop

        if action in ("tasks_close", "tasks_park"):
            async with _LOCK:
                data = _load_tasks(task_path)
                tid = str(payload.get("id") or "").strip()
                task = next((t for t in data["tasks"] if t.get("id") == tid), None)
                if task is None:
                    envelop.payload = {"ok": False, "error": "未找到 %s" % tid}
                    return envelop
                if action == "tasks_close":
                    task["status"] = "done"
                    task["progress"] = _scrub(payload.get("progress") or task.get("progress") or "完成")
                    task["next_action"] = ""
                else:
                    task["status"] = "paused"
                    task["progress"] = _scrub(payload.get("progress") or task.get("progress") or "用户说先不做")
                task["rev"] = int(task.get("rev", 1)) + 1
                task["updated_at"] = _now()
                hist = list(task.get("history") or [])
                hist.append({"at": _now(), "client": client or "unknown", "note": _scrub(payload.get("note") or action)})
                task["history"] = hist[-20:]
                _atomic_write(task_path, json.dumps(data, ensure_ascii=False, indent=2), agent)
                out = {"ok": True, "task": task}
                if action == "tasks_close" and not task.get("exp_ids"):
                    out["warning"] = "该任务没关联经验（exp_id），建议 my_memory append 一条再收尾"
                envelop.payload = out
                return envelop

        if action == "tasks_delete":
            # 自检 / 误建任务的物理清除通道。close 只标记 done，历史仍占 resume 的
            # total，外部 agent 自检后无法回到"空工作台"，所以补这个动作。
            async with _LOCK:
                data = _load_tasks(task_path)
                tid = str(payload.get("id") or "").strip()
                if tid:
                    keep = [t for t in data["tasks"] if t.get("id") != tid]
                    if len(keep) == len(data["tasks"]):
                        envelop.payload = {"ok": False, "error": "未找到 %s" % tid}
                        return envelop
                    data["tasks"] = keep
                    removed = [tid]
                else:
                    project = str(payload.get("project") or "").strip()
                    status = str(payload.get("status") or "").strip().lower()
                    if not project and not status and not payload.get("confirm"):
                        envelop.payload = {"ok": False, "error": "不传 id 时按 project/status 批量删，需要 confirm=true"}
                        return envelop
                    removed = []
                    keep = []
                    for t in data["tasks"]:
                        hit = True
                        if project and t.get("project") != project:
                            hit = False
                        if status and t.get("status") != status:
                            hit = False
                        if hit:
                            removed.append(t.get("id"))
                        else:
                            keep.append(t)
                    data["tasks"] = keep
                _atomic_write(task_path, json.dumps(data, ensure_ascii=False, indent=2), agent)
                envelop.payload = {"ok": True, "removed": removed, "left": len(data["tasks"])}
                return envelop

        # ---------------- 摘要 / 接手 ----------------
        if action in ("resume", "summary"):
            async with _LOCK:
                prof = _touch_profile(agent, client, prof_path, calls=1)
            data = _load_tasks(task_path)
            exp_items = _exp_items(_read_exp_lines(exp_path))
            text = _build_text(data["tasks"], exp_items, prof)
            if action == "summary":
                envelop.payload = {"ok": True, "text": text, "chars": len(text)}
                return envelop
            counts = {}
            for t in data["tasks"]:
                s = t.get("status") or "todo"
                counts[s] = counts.get(s, 0) + 1
            doing = [t for t in data["tasks"] if t.get("status") == "doing"]
            envelop.payload = {
                "ok": True,
                "text": text,
                "focus": doing[:1],
                "counts": counts,
                "total": len(data["tasks"]),
                "hint": "把上面任务念给用户，问是否继续；确认后才 upsert 加进度，用户说先不做就 tasks_park",
            }
            return envelop

        if action == "profile":
            async with _LOCK:
                prof = _touch_profile(agent, client, prof_path, calls=1)
            envelop.payload = {"ok": True, "data": prof}
            return envelop

        envelop.payload = {"ok": False, "error": "Unknown action: %s" % action}
        return envelop

    except Exception as e:
        try:
            agent.log.error("[mcp_agent] %s" % e)
        except Exception:
            pass
        envelop.payload = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        return envelop
