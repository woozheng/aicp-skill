"""
skill_loader — 技能加载器（前端专用：list / index / pin / unpin）

与前端 `www/skills` 页面（或 console 内嵌的 skills 面板）对齐。
仅保留前端使用的 4 个 action：

- list  ：列技能（只扫 data/skills/**/SKILL.md）
- index ：列当前 session 的钉住状态（pinned / indexed）
- pin   ：钉住（写 pinned.json，status = "pinned"，带总长度 ≤ 60KB 检查）
- unpin ：卸载（status = "indexed"）

路径：data/memories/main_agent/users/{user_id}/sessions/{session_id}/pinned.json
user_id 从 envelop.meta["user_id"] 取，缺省 "local"
session_id 从 envelop.payload["session_id"] 优先、meta["session_id"] 兜底

主控 main_agent 直接读 pinned.json（_load_pinned_skills），不调本插件。
"""

import json
import re
import hashlib
import os
import uuid
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

import core


# ============================================================
# 常量
# ============================================================

DATA_DIR = Path("data/skill_loader")
SKILLS_DIR = Path("data/skills")
INDEX_FILE = DATA_DIR / "index.json"

# 身份级：与 main_agent 的 users/{user_id}/skill.txt 对齐
USER_ROOT = Path("data/memories/main_agent/users")

# ★ 总钉住上限：60KB（含所有 status == "pinned" 的 full_text 长度之和）
MAX_PINNED_TOTAL_BYTES = 60 * 1024


# ============================================================
# 黑名单关键词
# ============================================================

BLOCKLIST_KEYWORDS = {
    "godmode", "jailbreak", "uncensoring", "uncensor", "red-teaming",
    "redteam", "safety-bypass", "bypass-safety", "refusal-removal",
    "abliteration", "guardrail-removal", "remove-guardrails", "excision",
    "model-surgery", "g0dm0d3", "obliterator", "obliteratus", "uncensored",
    "bypass", "越狱", "绕过", "去审查", "红队",
}


# ============================================================
# 分类映射
# ============================================================

CATEGORY_MAP: Dict[str, str] = {
    "debugging": "software-development",
    "testing": "software-development",
    "tdd": "software-development",
    "code-review": "software-development",
    "quality": "software-development",
    "development": "software-development",
    "planning": "software-development",
    "implementation": "software-development",
    "workflow": "software-development",
    "documentation": "software-development",
    "subagent": "software-development",
    "delegation": "software-development",
    "parallel": "software-development",
    "mlops": "mlops",
    "fine-tuning": "mlops",
    "training": "mlops",
    "evaluation": "mlops",
    "benchmarking": "mlops",
    "inference": "mlops",
    "huggingface": "mlops",
    "peft": "mlops",
    "lora": "mlops",
    "qlora": "mlops",
    "trl": "mlops",
    "rlhf": "mlops",
    "design": "design",
    "ui": "design",
    "ux": "design",
    "brand": "design",
    "visual": "design",
    "inclusive": "design",
    "github": "devops",
    "git": "devops",
    "devops": "devops",
    "webhook": "devops",
    "sync": "devops",
    "backup": "devops",
    "deployment": "devops",
    "creative": "creative",
    "generative-art": "creative",
    "p5js": "creative",
    "creative-coding": "creative",
    "interactive": "creative",
    "visualization": "creative",
    "canvas": "creative",
    "shaders": "creative",
    "animation": "creative",
    "ascii": "creative",
    "legal": "legal",
    "contract": "legal",
    "policy": "legal",
    "compliance": "legal",
    "gaming": "gaming",
    "pokemon": "gaming",
    "emulator": "gaming",
    "gameplay": "gaming",
    "hermes": "hermes",
    "autonomous": "autonomous-ai",
    "agent": "autonomous-ai",
    "multi-agent": "autonomous-ai",
    "spawning": "autonomous-ai",
    "gateway": "autonomous-ai",
    "unity": "unity",
    "blender": "blender",
    "paid-media": "paid-media",
    "tracking": "paid-media",
    "attribution": "paid-media",
    "engineering": "engineering",
    "media": "media",
    "audio": "media",
    "spectrogram": "media",
    "research": "research",
    "productivity": "productivity",
    "google": "productivity",
    "gmail": "productivity",
    "calendar": "productivity",
    "drive": "productivity",
    "sheets": "productivity",
    "apple": "apple",
    "imessage": "apple",
    "macos": "apple",
}


# ============================================================
# 索引读写
# ============================================================

def _load_index() -> Dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not INDEX_FILE.exists():
        return _empty_index()
    try:
        return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except Exception:
        return _empty_index()


def _empty_index() -> Dict[str, Any]:
    return {
        "skills": [],
        "categories": [],
        "last_scan": None,
        "skipped": [],
    }


def _save_index(data: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ============================================================
# 黑名单 / 分类
# ============================================================

def _is_blocked_skill(skill: Dict[str, Any]) -> bool:
    title = str(skill.get("title", "")).lower()
    desc = str(skill.get("description", "")).lower()
    tags = " ".join(skill.get("tags", [])).lower()
    combined = f"{title} {desc} {tags}"
    for kw in BLOCKLIST_KEYWORDS:
        if kw in combined:
            return True
    return False


def _determine_category_from_tags(tags: List[str], file_path: str) -> str:
    for tag in tags:
        t = tag.lower()
        if t in CATEGORY_MAP:
            return CATEGORY_MAP[t]

    parts = file_path.split("/")
    if len(parts) >= 2:
        dir_name = parts[0]
        known_cats = set(CATEGORY_MAP.values())
        if dir_name in known_cats:
            return dir_name
        for key, cat in CATEGORY_MAP.items():
            if key in dir_name.lower():
                return cat
        return dir_name

    return "未分类"


# ============================================================
# 解析 SKILL.md
# ============================================================

def _strip_quotes(s: str) -> str:
    if len(s) >= 2:
        if (s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'"):
            return s[1:-1]
    return s


def _parse_skill_md(file_path: Path) -> Optional[Dict[str, Any]]:
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception:
        return None

    lines = content.split("\n")
    title = ""
    description = ""
    category = "未分类"
    tags: List[str] = []
    fm_end = 0

    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                fm_end = i + 1
                break
            line = lines[i].strip()
            if line.startswith("title:"):
                title = _strip_quotes(line[6:].strip())
            elif line.startswith("description:"):
                description = _strip_quotes(line[12:].strip())
            elif line.startswith("category:"):
                category = _strip_quotes(line[9:].strip())
            elif line.startswith("tags:"):
                tag_str = line[5:].strip()
                if tag_str.startswith("[") and tag_str.endswith("]"):
                    inner = tag_str[1:-1]
                    for t in inner.split(","):
                        cleaned = _strip_quotes(t.strip())
                        if cleaned:
                            tags.append(cleaned)

    if not title:
        for i in range(fm_end, len(lines)):
            if lines[i].startswith("# "):
                title = lines[i][2:].strip()
                break
    if not title:
        title = file_path.stem

    if not description:
        for i in range(fm_end, len(lines)):
            line = lines[i].strip()
            if line and not line.startswith("#"):
                description = line[:200]
                break

    # 只扫 data/skills/，relative_to(SKILLS_DIR) 必然成功
    try:
        rel = file_path.relative_to(SKILLS_DIR)
        rel_path = str(rel).replace("\\", "/")
    except ValueError:
        rel_path = str(file_path).replace("\\", "/")

    skill_id = _skill_id_from_path(str(file_path))
    if not skill_id:
        skill_id = file_path.stem.lower() or "unknown"

    if category == "未分类" or not category:
        category = _determine_category_from_tags(tags, rel_path)

    return {
        "skill_id": skill_id,
        "title": title,
        "description": description,
        "category": category,
        "tags": tags,
        "file_path": rel_path,
        "skill_dir": str(file_path.parent),
        "content": content,
    }


# ============================================================
# 扫描技能（只扫 data/skills/**/SKILL.md）
# ============================================================

def _walk_skills(dir_path: Path, out: List[Path]) -> None:
    if not dir_path.exists():
        return
    try:
        for entry in dir_path.iterdir():
            if entry.is_dir():
                _walk_skills(entry, out)
            elif entry.name == "SKILL.md":
                out.append(entry)
    except Exception:
        pass


def _scan_skills() -> Dict[str, Any]:
    """扫描经验技能：只扫 data/skills/**/SKILL.md。

    与 cogitor._scan_skills / contract_agent._handle_skill_contract /
    main_agent._scan_skills_for_baseline 规则一致——只认 data/skills/，
    不扫 plugins/**/*_skill.md（插件用法文档走 contract_agent 或
    use_tool(target="skill/plugin/...") 取，不进 pinned.json）。
    """
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)

    md_files: List[Path] = []
    _walk_skills(SKILLS_DIR, md_files)

    skills: List[Dict[str, Any]] = []
    categories_set = set()
    blocked_titles: List[str] = []
    seen_ids = set()

    for f in md_files:
        skill = _parse_skill_md(f)
        if skill is None:
            continue
        if _is_blocked_skill(skill):
            blocked_titles.append(skill.get("title", str(f)))
            continue
        sid = str(skill.get("skill_id") or "")
        if sid and sid in seen_ids:
            continue
        if sid:
            seen_ids.add(sid)
        skills.append(skill)
        categories_set.add(skill["category"])

    categories = sorted(categories_set)

    data = {
        "skills": skills,
        "categories": categories,
        "last_scan": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "skipped": blocked_titles,
    }
    _save_index(data)
    return data


# ============================================================
# 钉住状态读写（users/{user_id}/sessions/{session_id}/pinned.json）
# ============================================================

def _pinned_file(user_id: str, session_id: str) -> Path:
    p = USER_ROOT / user_id / "sessions" / session_id
    p.mkdir(parents=True, exist_ok=True)
    return p / "pinned.json"


def _resolve_session_id(envelop) -> str:
    """从 payload 优先、meta 兜底取 session_id。"""
    payload = envelop.payload or {}
    meta = envelop.meta or {}
    sid = payload.get("session_id") or meta.get("session_id") or ""
    return str(sid).strip()


def _resolve_user_id(envelop) -> str:
    """从 meta 取 user_id，缺省 local（与 main_agent.resolve_ids 一致）。"""
    meta = envelop.meta or {}
    uid = (
        meta.get("user_id")
        or meta.get("auth_result", {}).get("user_id")
        or "local"
    )
    return str(uid).strip() or "local"


def _load_state(user_id: str, session_id: str) -> Dict[str, Any]:
    f = _pinned_file(user_id, session_id)
    data: Dict[str, Any] = {"pinned": {}, "turn": 0, "last_user_stamp": ""}
    if f.exists():
        try:
            loaded = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            pass
    if not isinstance(data.get("pinned"), dict):
        data["pinned"] = {}
    if not isinstance(data.get("turn"), int):
        data["turn"] = 0
    if not isinstance(data.get("last_user_stamp"), str):
        data["last_user_stamp"] = ""
    return data


def _save_state(user_id: str, session_id: str, state: Dict[str, Any]) -> None:
    f = _pinned_file(user_id, session_id)
    tmp = f.with_suffix(f".tmp.{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, f)


def _skill_id_from_path(file_path: str) -> str:
    """从文件路径推导 skill_id。

    只认 data/skills/<rel>/SKILL.md → <rel>。
    """
    norm = str(file_path).replace("\\", "/")
    for marker in ("/data/skills/", "data/skills/"):
        i = norm.find(marker)
        if i != -1:
            rel = norm[i + len(marker):]
            if rel.endswith("/SKILL.md"):
                rel = rel[: -len("/SKILL.md")]
            elif rel == "SKILL.md":
                rel = "root"
            return re.sub(r"[^a-z0-9]+", "_", rel.lower()).strip("_") or "skill"
    name = Path(file_path).stem
    name = re.sub(r"_skill$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[^a-z0-9_]+", "_", name.lower())
    return name or "unknown"


def _abs_skill_path(file_path: str) -> Path:
    """把索引 / state 里的 file_path 解析成可读路径。

    - 绝对路径        → 原样
    - data/...        → 仓库相对路径
    - 其它（SKILLS_DIR 下的相对路径）→ SKILLS_DIR 下
    """
    p = Path(str(file_path))
    if p.is_absolute():
        return p
    norm = str(file_path).replace("\\", "/")
    if norm.startswith("data/"):
        return Path(norm)
    return Path(SKILLS_DIR) / norm


def _strip_frontmatter(text: str) -> str:
    """去掉开头 YAML frontmatter（--- / key: value / ---）。"""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    block = text[3:end]
    if not any(":" in ln for ln in block.splitlines() if ln.strip()):
        return text
    return text[end + 4:].lstrip("\n")


def _index_to_text(state: Dict[str, Any]) -> str:
    pinned = state.get("pinned", {})
    if not pinned:
        return ""
    lines = ["<!-- SKILL_INDEX -->"]
    for sid, info in sorted(pinned.items()):
        if not isinstance(info, dict):
            continue
        status = info.get("status", "indexed")
        title = info.get("title", sid)
        desc = info.get("desc", "")
        marker = "📌" if status == "pinned" else "📋"
        lines.append(f"- {marker} {title} — {desc}")
    lines.append("<!-- /SKILL_INDEX -->")
    return "\n".join(lines)


def _pinned_total_len(pinned: Dict[str, Any], exclude_sid: str = "") -> int:
    """已钉住（status == "pinned"）的 full_text 长度之和；可排除某个 sid（重新 pin 时用）。"""
    total = 0
    for sid, info in pinned.items():
        if sid == exclude_sid:
            continue
        if not isinstance(info, dict):
            continue
        if info.get("status") != "pinned":
            continue
        total += len(str(info.get("full_text", "") or ""))
    return total


# ============================================================
# 主入口（只保留 list / index / pin / unpin）
# ============================================================

async def execute(envelop, agent) -> Any:
    payload = envelop.payload or {}
    params = payload.get("params") if isinstance(payload.get("params"), dict) else None
    if not params:
        params = payload

    action = str(payload.get("action", "list"))
    user_id = _resolve_user_id(envelop)
    session_id = _resolve_session_id(envelop)

    # ============================================================
    # list：列技能（前端「技能管理」用，带 include_status）
    # ============================================================
    if action == "list":
        _scan_skills()
        fresh_index = _load_index()
        category = str(params.get("category", ""))
        limit = int(params.get("limit", 100) or 100)
        include_status = bool(params.get("include_status", False))
        state = _load_state(user_id, session_id) if include_status else {"pinned": {}}
        pinned_map = state.get("pinned", {}) if include_status else {}

        skills = []
        for s in fresh_index.get("skills", []):
            if category and s.get("category") != category:
                continue
            sid = s.get("skill_id", "")
            fp = str(s.get("file_path", "") or "").replace("\\", "/")
            if not fp.startswith("data/"):
                fp = "data/skills/" + fp if fp else ""
            item = {
                "skill_id": sid,
                "title": s.get("title"),
                "description": s.get("description"),
                "category": s.get("category"),
                "tags": s.get("tags"),
                "file_path": fp,
                "skill_dir": s.get("skill_dir", ""),
            }
            if include_status:
                p = pinned_map.get(sid) or {}
                item["status"] = "pinned" if p.get("status") == "pinned" else "indexed"
                item["deletable"] = fp.startswith("data/skills/")
            skills.append(item)
            if len(skills) >= limit:
                break

        # 补漏：pinned 但不在 index 里的（如文件已删，或历史遗留）
        if include_status:
            known = {s["skill_id"] for s in skills}
            for sid, p in pinned_map.items():
                if sid in known:
                    continue
                skills.append({
                    "skill_id": sid,
                    "title": p.get("title", sid),
                    "description": p.get("desc", ""),
                    "file_path": p.get("path", ""),
                    "status": "pinned" if p.get("status") == "pinned" else "indexed",
                    "deletable": False,
                })

        envelop.payload = {
            "ok": True,
            "data": {
                "skills": skills, "total": len(skills),
                "categories": fresh_index.get("categories"),
                "last_scan": fresh_index.get("last_scan"),
            },
        }
        return envelop

    # ============================================================
    # index：列当前 session 的钉住状态（前端「已加载」用）
    # ============================================================
    if action == "index":
        state = _load_state(user_id, session_id)
        index_text = _index_to_text(state)
        pinned = state.get("pinned", {})
        pinned_count = sum(1 for s in pinned.values() if s.get("status") == "pinned")
        indexed_count = sum(1 for s in pinned.values() if s.get("status") != "pinned")
        envelop.payload = {
            "ok": True,
            "user_id": user_id,
            "session_id": session_id,
            "index_text": index_text,
            "total": len(pinned),
            "pinned_count": pinned_count,
            "indexed_count": indexed_count,
            "skills": [
                {
                    "skill_id": sid,
                    "title": info.get("title"),
                    "desc": info.get("desc"),
                    "status": info.get("status", "indexed"),
                    "path": info.get("path"),
                }
                for sid, info in sorted(pinned.items())
            ],
        }
        return envelop

    # ============================================================
    # pin：钉住（带 60KB 总长度检查）
    # ============================================================
    if action == "pin":
        skill_id = str(params.get("skill_id", "")).strip()
        if not skill_id:
            envelop.payload = {"ok": False, "error": "缺少 skill_id 参数"}
            return envelop
        if not session_id:
            envelop.payload = {"ok": False, "error": "缺少 session_id"}
            return envelop

        state = _load_state(user_id, session_id)
        pinned = state.get("pinned", {})

        info = pinned.get(skill_id)
        if info is None:
            lower = skill_id.lower()
            candidates = []
            for sid, sinfo in pinned.items():
                if lower in sid.lower() or lower in str(sinfo.get("title", "")).lower():
                    candidates.append(sid)
            if len(candidates) == 1:
                skill_id = candidates[0]
                info = pinned[skill_id]
            elif len(candidates) > 1:
                envelop.payload = {
                    "ok": False,
                    "error": "skill_id 匹配到多个，请用精确 ID",
                    "candidates": candidates,
                }
                return envelop

        # ---- 分支 1：不在 pinned 里，从索引读文件 ----
        if info is None:
            idx = _load_index()
            target = None
            for s in idx.get("skills", []):
                if s.get("skill_id") == skill_id:
                    target = s
                    break
            if target is None:
                lower = skill_id.lower()
                for s in idx.get("skills", []):
                    sid = str(s.get("skill_id", "")).lower()
                    fpath = str(s.get("file_path", "")).lower()
                    if lower in sid or lower in fpath:
                        target = s
                        break
            if target is None:
                envelop.payload = {"ok": False, "error": f"技能不在索引中: {skill_id}"}
                return envelop

            fp = target.get("file_path", "")
            f = _abs_skill_path(fp)
            if not f.exists():
                envelop.payload = {"ok": False, "error": f"文件不存在: {f}"}
                return envelop
            try:
                content = _strip_frontmatter(f.read_text(encoding="utf-8")).strip()
            except Exception as e:
                envelop.payload = {"ok": False, "error": f"读取失败: {e}"}
                return envelop
            if not content:
                envelop.payload = {"ok": False, "error": f"文件为空: {f}"}
                return envelop

            # ★ 总长度检查（写入前）
            pinned_total = _pinned_total_len(pinned)
            if pinned_total + len(content) > MAX_PINNED_TOTAL_BYTES:
                envelop.payload = {
                    "ok": False,
                    "error": (
                        f"钉住总长度超限：已钉 {pinned_total} 字 + 本次 {len(content)} 字 "
                        f"> {MAX_PINNED_TOTAL_BYTES} 字（60KB）。请先 unpin 其他技能再钉。"
                    ),
                    "pinned_total": pinned_total,
                    "new_size": len(content),
                    "limit": MAX_PINNED_TOTAL_BYTES,
                }
                return envelop

            content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()
            sid = target.get("skill_id") or _skill_id_from_path(fp)
            pinned[sid] = {
                "source": "skill",
                "path": str(f),
                "title": target.get("title", sid),
                "desc": target.get("description", "")[:140],
                "status": "pinned",
                "full_text": content,
                "hash": content_hash,
                "pinned_at": datetime.now().isoformat(),
            }
            state["pinned"] = pinned
            _save_state(user_id, session_id, state)

            envelop.payload = {
                "ok": True,
                "message": f"📌 已钉住: {pinned[sid].get('title', sid)}（全文常驻）",
                "skill_id": sid,
                "title": pinned[sid].get("title"),
                "status": "pinned",
                "size": len(content),
                "pinned_total": pinned_total + len(content),
                "limit": MAX_PINNED_TOTAL_BYTES,
            }
            return envelop

        # ---- 分支 2：已在 pinned 里，重新读文件 ----
        file_path = info.get("path", "")
        f = Path(file_path)
        if not f.exists():
            envelop.payload = {"ok": False, "error": f"文件不存在: {file_path}"}
            return envelop
        try:
            content = _strip_frontmatter(f.read_text(encoding="utf-8")).strip()
        except Exception as e:
            envelop.payload = {"ok": False, "error": f"读取失败: {e}"}
            return envelop

        # ★ 总长度检查（写入前，先排除旧的自己）
        pinned_total = _pinned_total_len(pinned, exclude_sid=skill_id)
        if pinned_total + len(content) > MAX_PINNED_TOTAL_BYTES:
            envelop.payload = {
                "ok": False,
                "error": (
                    f"钉住总长度超限：已钉 {pinned_total} 字 + 本次 {len(content)} 字 "
                    f"> {MAX_PINNED_TOTAL_BYTES} 字（60KB）。请先 unpin 其他技能再钉。"
                ),
                "pinned_total": pinned_total,
                "new_size": len(content),
                "limit": MAX_PINNED_TOTAL_BYTES,
            }
            return envelop

        content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()
        info["status"] = "pinned"
        info["hash"] = content_hash
        info["full_text"] = content
        info["pinned_at"] = datetime.now().isoformat()
        pinned[skill_id] = info
        state["pinned"] = pinned
        _save_state(user_id, session_id, state)

        envelop.payload = {
            "ok": True,
            "message": f"📌 已钉住: {info.get('title', skill_id)}（全文常驻）",
            "skill_id": skill_id,
            "title": info.get("title"),
            "status": "pinned",
            "size": len(content),
            "pinned_total": pinned_total + len(content),
            "limit": MAX_PINNED_TOTAL_BYTES,
        }
        return envelop

    # ============================================================
    # unpin：卸载（status = indexed）
    # ============================================================
    if action == "unpin":
        skill_id = str(params.get("skill_id", "")).strip()
        if not skill_id:
            envelop.payload = {"ok": False, "error": "缺少 skill_id 参数"}
            return envelop
        if not session_id:
            envelop.payload = {"ok": False, "error": "缺少 session_id"}
            return envelop

        state = _load_state(user_id, session_id)
        pinned = state.get("pinned", {})
        info = pinned.get(skill_id)

        if info is None:
            lower = skill_id.lower()
            for sid, sinfo in pinned.items():
                if lower in sid.lower() or lower in str(sinfo.get("title", "")).lower():
                    skill_id = sid
                    info = sinfo
                    break

        if info is None:
            envelop.payload = {"ok": False, "error": f"技能不在索引中: {skill_id}"}
            return envelop

        info["status"] = "indexed"
        info.pop("pinned_at", None)
        pinned[skill_id] = info
        state["pinned"] = pinned
        _save_state(user_id, session_id, state)

        remaining_total = _pinned_total_len(pinned)
        envelop.payload = {
            "ok": True,
            "message": f"📋 已降级: {info.get('title', skill_id)}（仅保留目录索引）",
            "skill_id": skill_id,
            "title": info.get("title"),
            "status": "indexed",
            "pinned_total": remaining_total,
            "limit": MAX_PINNED_TOTAL_BYTES,
        }
        return envelop

    envelop.payload = {
        "ok": False,
        "error": f"未知 action: {action}（skill_loader 只支持 list / index / pin / unpin）",
    }
    return envelop


# ============================================================
# help
# ============================================================

def help() -> dict:
    return {
        "route": "builtins/tools/skill_loader",
        "description": "技能加载器（前端专用）— list / index / pin / unpin，钉住总长度 ≤ 60KB",
        "input": {
            "action": "list | index | pin | unpin",
            "session_id": "会话 ID（index / pin / unpin 必填）",
            "skill_id": "技能 ID（pin / unpin 必填）",
            "category": "分类筛选（list 时，可选）",
            "limit": "返回数量（list 时，默认 100）",
            "include_status": "布尔，list 时传 true 则返回 status / deletable",
        },
        "output": {
            "ok": "是否成功",
            "data": "结果数据",
            "message": "结果消息（pin / unpin）",
            "skill_id": "技能 ID",
            "status": "pinned | indexed",
            "pinned_total": "当前已钉住总字符数（pin / unpin）",
            "limit": "钉住总长度上限（60KB = 61440 字符）",
            "error": "错误信息（超限 / 不存在 / 参数缺失）",
        },
        "limits": {
            "max_pinned_total_bytes": MAX_PINNED_TOTAL_BYTES,
        },
    }
