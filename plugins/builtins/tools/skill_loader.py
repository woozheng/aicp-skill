"""
skill_loader — 技能加载器（搜索 + 加载 + 管理）

与 Java 版 os/skill_loader.java 对齐。

action：
- scan：扫 data/skills/，建索引
- list：列技能（自动 scan）
- search：搜技能（自动 scan + 粗筛 + LLM 精排）
- detail：查详情
- categories：列分类
- stats：统计
- load：加载技能到 active_skills（支持模糊匹配）
- clear：清空当前技能
- get_active：查看当前技能
- save_active：保存当前技能
- loadfromfile：从文件加载到当前技能

路径：data/memories/main_agent/users/{user_id}/skill.txt
user_id 从 envelop.meta["user_id"] 取，缺省 "local"
"""

import json
import re
import hashlib
import asyncio
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
PLUGINS_ROOT = Path("plugins")

# 身份级：与 main_agent 的 users/{user_id}/skill.txt 对齐
USER_ROOT = Path("data/memories/main_agent/users")


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

    try:
        rel = file_path.relative_to(SKILLS_DIR)
        rel_path = str(rel).replace("\\", "/")
    except ValueError:
        try:
            rel = file_path.relative_to(PLUGINS_ROOT)
            rel_path = ("plugins/" + str(rel)).replace("\\", "/")
        except ValueError:
            rel_path = str(file_path).replace("\\", "/")

    # ★ skill_id 统一交给 _skill_id_from_path（与 main_agent 同规则）：
    #   同一文件只产生一个 id，SKILL_INDEX 不再出现重复行。
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
# 扫描技能
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


def _walk_plugin_skills(dir_path: Path, out: List[Path]) -> None:
    """扫描 plugins/**/*_skill.md —— 插件用法文档，与经验技能同源索引。"""
    if not dir_path.exists():
        return
    try:
        for entry in dir_path.iterdir():
            if entry.is_dir():
                if entry.name == "__pycache__":
                    continue
                _walk_plugin_skills(entry, out)
            elif entry.name.endswith("_skill.md"):
                out.append(entry)
    except Exception:
        pass


def _scan_skills() -> Dict[str, Any]:
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)

    md_files: List[Path] = []
    _walk_skills(SKILLS_DIR, md_files)

    # ★ 双源：经验技能 data/skills/**/SKILL.md  +  插件用法文档 plugins/**/*_skill.md
    plugin_files: List[Path] = []
    _walk_plugin_skills(PLUGINS_ROOT, plugin_files)

    skills: List[Dict[str, Any]] = []
    categories_set = set()
    blocked_titles: List[str] = []
    seen_ids = set()

    for f in md_files + plugin_files:
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
        "last_scan": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "skipped": blocked_titles,
    }
    _save_index(data)
    return data


# ============================================================
# 模糊匹配 + 搜索
# ============================================================

def _fuzzy_match(query: str, text: str, threshold: float = 0.3) -> float:
    q = query.lower()
    t = text.lower()
    if not q or not t:
        return 0.0
    if q in t:
        return 1.0

    max_len = 0
    for i in range(len(q)):
        for j in range(i + 1, len(q) + 1):
            sub = q[i:j]
            if sub in t:
                max_len = max(max_len, len(sub))
    ratio = (2.0 * max_len) / (len(q) + len(t))
    return ratio if ratio >= threshold else 0.0


def _search_skills(
    index: Dict[str, Any],
    keywords: List[str],
    category: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    if not keywords:
        return []

    query = " ".join(keywords).strip()
    if not query:
        return []

    results: List[Dict[str, Any]] = []
    for skill in index.get("skills", []):
        if category and skill.get("category") != category:
            continue

        title_score = _fuzzy_match(query, skill.get("title", ""))
        desc_score = _fuzzy_match(query, skill.get("description", "")) * 0.8
        tag_scores = [
            _fuzzy_match(query, t) * 0.7
            for t in skill.get("tags", [])
        ]
        tag_max = max(tag_scores) if tag_scores else 0.0
        content_score = _fuzzy_match(query, skill.get("content", "")[:2000]) * 0.5

        score = max(title_score, desc_score, tag_max, content_score)
        if score <= 0:
            continue

        results.append({
            "skill_id": skill.get("skill_id"),
            "title": skill.get("title"),
            "description": skill.get("description"),
            "category": skill.get("category"),
            "tags": skill.get("tags"),
            "file_path": skill.get("file_path"),
            "skill_dir": skill.get("skill_dir", ""),
            "score": round(score, 3),
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:limit]


# ============================================================
# LLM 精排
# ============================================================

def _extract_json_object(raw: str) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    start = raw.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    end = -1

    for i in range(start, len(raw)):
        ch = raw[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break

    if end == -1:
        return None

    try:
        return json.loads(raw[start:end + 1])
    except Exception:
        return None


async def _llm_rank(
    agent,
    query: str,
    keywords: List[str],
    candidates: List[Dict[str, Any]],
    top_k: int = 10,
) -> Dict[str, Any]:
    briefs = []
    for s in candidates:
        desc = str(s.get("description", ""))[:200]
        briefs.append({
            "skill_id": s.get("skill_id"),
            "title": s.get("title"),
            "description": desc,
            "tags": list(s.get("tags", []))[:5],
        })

    system_prompt = (
        "你是一个技能推荐专家。用户提出问题并给出关键词，你需要从候选技能中选出最匹配的。\n"
        "候选技能已经经过初步关键词筛选，你只需要做语义精排。\n"
        '返回JSON格式：{"skills": [{"skill_id": "xxx", "reason": "推荐理由"}]}\n'
        "只返回最相关的 top_k 个，按相关度从高到低排序。"
    )

    user_prompt = (
        f"用户问题：{query}\n"
        f"关键词：{', '.join(keywords)}\n"
        f"返回数量：{top_k}\n"
        f"候选技能（共{len(briefs)}个）：\n"
        + json.dumps(briefs, ensure_ascii=False)
    )

    try:
        if not agent.llm:
            raise RuntimeError("LLM 未配置")

        raw = await agent.llm.chat([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])

        parsed = _extract_json_object(raw)
        ranked = parsed.get("skills", []) if isinstance(parsed, dict) else []

        out: List[Dict[str, Any]] = []
        for item in ranked[:top_k]:
            sid = item.get("skill_id")
            for c in candidates:
                if c.get("skill_id") == sid:
                    enriched = dict(c)
                    enriched["reason"] = item.get("reason")
                    out.append(enriched)
                    break

        return {"skills": out, "total": len(out)}
    except Exception as e:
        fallback = candidates[:top_k]
        out = [
            {"skill_id": s.get("skill_id"), "reason": "关键词匹配（LLM降级）"}
            for s in fallback
        ]
        return {"skills": out, "total": len(out), "error": str(e)}


# ============================================================
# 活跃技能读写（身份级，走 users/{user_id}/skill.txt）
# ============================================================

def _user_dir(user_id: str) -> Path:
    p = USER_ROOT / user_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def _pinned_file(user_id: str, session_id: str) -> Path:
    """本会话钉住的技能全文（唯一真相源）。"""
    p = USER_ROOT / user_id / "sessions" / session_id
    p.mkdir(parents=True, exist_ok=True)
    return p / "pinned.json"

def _resolve_session_id(envelop) -> str:
    """从 payload 优先、meta 兜底取 session_id。"""
    payload = envelop.payload or {}
    meta = envelop.meta or {}
    sid = payload.get("session_id") or meta.get("session_id") or ""
    return str(sid).strip()




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

import os, uuid

def _save_state(user_id: str, session_id: str, state: Dict[str, Any]) -> None:
    f = _pinned_file(user_id, session_id)
    tmp = f.with_suffix(f".tmp.{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, f)


def _skill_id_from_path(file_path: str) -> str:
    """从文件路径推导 skill_id。

    ★ 与 main_agent._skill_sid_for_file 规则一致，同一文件不再产生两个 id：
      plugins/<ppath>/<stem>_skill.md → plugin_<ppath>_<stem>
      data/skills/<rel>/SKILL.md      → <rel>
    """
    norm = str(file_path).replace("\\", "/")
    for marker in ("/plugins/", "plugins/"):
        i = norm.find(marker)
        if i != -1:
            rel = norm[i + len(marker):]
            if rel.endswith("_skill.md"):
                rel = rel[: -len("_skill.md")]
            return ("plugin_" + re.sub(r"[^a-z0-9]+", "_", rel.lower())).strip("_") or "plugin"
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

    - 绝对路径            → 原样
    - plugins/...         → 仓库相对路径（插件用法文档）
    - data/...            → 仓库相对路径
    - 其它（SKILLS_DIR 下的相对路径）→ SKILLS_DIR 下
    """
    p = Path(str(file_path))
    if p.is_absolute():
        return p
    norm = str(file_path).replace("\\", "/")
    if norm.startswith("plugins/") or norm.startswith("data/"):
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


def _extract_title_and_desc(content: str, file_path: str) -> Tuple[str, str]:
    """从技能内容提取标题和描述。★ 支持 YAML frontmatter：title/description 优先。"""
    lines = content.split("\n")

    fm = {}
    if lines and lines[0].strip() == "---":
        end = 0
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end = i
                break
            ln = lines[i].strip()
            if ":" in ln and not ln.startswith("#"):
                k, v = ln.split(":", 1)
                fm[k.strip().lower()] = _strip_quotes(v.strip())
        lines = lines[end + 1:]

    title = Path(file_path).stem
    desc = ""
    title_found = False
    if fm.get("title"):
        title = fm["title"]
        title_found = True
    if fm.get("description"):
        return title, fm["description"][:140]

    for line in lines:
        s = line.strip()
        if not s:
            if title_found and desc:
                break
            continue
        if s.startswith("# ") and not title_found:
            title = s[2:].strip()
            title_found = True
            continue
        # 跳过注释行
        if s.startswith("<!--"):
            continue
        # 第一行正文作为描述
        if not desc:
            desc = s[:140]
            if title_found:
                break
    if not desc:
        desc = title
    return title, desc


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


def _compose_active_content(user_id: str, session_id: str, state: Dict[str, Any]) -> str:
    pinned = state.get("pinned", {}) or {}
    parts: List[str] = []
    index_text = _index_to_text(state)
    if index_text:
        parts.append(index_text)
    blocks: List[str] = []
    for _sid, info in sorted(pinned.items()):
        if not isinstance(info, dict):
            continue
        if info.get("status") != "pinned":
            continue
        full = str(info.get("full_text", "") or "")
        if full.strip():
            blocks.append(full.strip())
    if blocks:
        parts.append("\n\n".join(blocks))
    return "\n\n".join(parts)








# ============================================================
# 身份解析
# ============================================================

def _resolve_user_id(envelop) -> str:
    """从 meta 取 user_id，缺省 local（与 main_agent.resolve_ids 一致）。"""
    meta = envelop.meta or {}
    uid = (
        meta.get("user_id")
        or meta.get("auth_result", {}).get("user_id")
        or "local"
    )
    return str(uid).strip() or "local"


# ============================================================
# 主入口
# ============================================================

async def execute(envelop, agent) -> Any:
    payload = envelop.payload or {}

    params = payload.get("params") if isinstance(payload.get("params"), dict) else None
    if not params:
        params = payload

    action = str(payload.get("action", "list"))

    user_id = _resolve_user_id(envelop)
    session_id = _resolve_session_id(envelop)

    # ============ 加载类 action ============
    if action == "loadfromfile":
        file_path = str(params.get("path", "")).strip()
        if not file_path:
            envelop.payload = {"ok": False, "error": "缺少 path 参数"}
            return envelop
        if not session_id:
            envelop.payload = {"ok": False, "error": "缺少 session_id"}
            return envelop
        f = Path(file_path)
        if not f.exists():
            envelop.payload = {"ok": False, "error": f"文件不存在: {file_path}"}
            return envelop
        try:
            content = _strip_frontmatter(f.read_text(encoding="utf-8")).strip()
        except Exception as e:
            envelop.payload = {"ok": False, "error": f"读取失败: {e}"}
            return envelop
        if not content:
            envelop.payload = {"ok": False, "error": f"文件为空: {file_path}"}
            return envelop

        content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()
        state = _load_state(user_id, session_id)
        pinned = state["pinned"]
        sid = _skill_id_from_path(file_path)
        old = pinned.get(sid)
        first_time = old is None
        changed = first_time or (old.get("hash") != content_hash)
        title, desc = _extract_title_and_desc(content, file_path)
        pinned[sid] = {
            "source": "skill",
            "path": str(file_path),
            "title": title,
            "desc": desc,
            "status": "pinned",
            "full_text": content,
            "hash": content_hash,
            "pinned_at": datetime.now().isoformat(),
        }
        _save_state(user_id, session_id, state)

        envelop.payload = {
            "ok": True,
            "message": (f"✅ 已加载: {file_path}，{len(content)} 字（已钉住）" if first_time
                        else (f"✅ 已更新: {file_path}，{len(content)} 字（内容已变更）" if changed
                              else f"✅ 已加载过: {file_path}（内容未变）")),
            "path": file_path,
            "user_id": user_id,
            "session_id": session_id,
            "size": len(content),
            "hash": content_hash,
            "skipped": (not changed),
            "reloaded": (not first_time and changed),
            "skill_id": sid,
            "status": "pinned",
        }
        return envelop

    # ============ 搜索类 action ============
    if action == "scan":
        data = _scan_skills()
        out = {
            "total": len(data.get("skills", [])),
            "categories": data.get("categories"),
            "last_scan": data.get("last_scan"),
            "skipped": data.get("skipped", []),
        }
        envelop.payload = {"ok": True, "data": out}
        return envelop

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
            if not (fp.startswith("data/") or fp.startswith("plugins/")):
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

        # 补漏：pinned 但不在 index 里的（如文件已删）
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

    if action == "search":
        _scan_skills()
        fresh_index = _load_index()

        query = str(params.get("query", "")).strip()
        keywords_raw = params.get("keywords", [])
        keywords: List[str] = []
        if isinstance(keywords_raw, list):
            keywords = [str(k) for k in keywords_raw]
        elif isinstance(keywords_raw, str) and keywords_raw.strip():
            keywords = [keywords_raw.strip()]

        if not query:
            envelop.payload = {"ok": False, "error": "query 不能为空"}
            return envelop

        if not keywords and query:
            keywords = [w for w in query.split() if w] or [query]

        if not keywords:
            envelop.payload = {"ok": False, "error": "keywords 不能为空"}
            return envelop

        category = params.get("category")
        limit = int(params.get("limit", 10) or 10)

        candidates = _search_skills(fresh_index, keywords, category, 30)

        if not candidates:
            envelop.payload = {
                "ok": True,
                "data": {
                    "skills": [],
                    "total": 0,
                    "query": query,
                    "keywords": keywords,
                },
            }
            return envelop

        rank_result = await _llm_rank(agent, query, keywords, candidates, limit)
        final_skills = rank_result.get("skills", [])

        data = {
            "skills": final_skills,
            "total": len(final_skills),
            "query": query,
            "keywords": keywords,
        }
        if "error" in rank_result:
            data["fallback"] = True
            data["error"] = rank_result["error"]

        envelop.payload = {"ok": True, "data": data}
        return envelop

    # ============ 其余 action 读全局 index ============
    index = _load_index()

    if action == "detail":
        skill_id = str(params.get("skill_id", ""))
        for s in index.get("skills", []):
            if s.get("skill_id") == skill_id:
                envelop.payload = {"ok": True, "data": s}
                return envelop
        envelop.payload = {"ok": False, "error": "技能不存在"}
        return envelop

    if action == "categories":
        cats = index.get("categories", [])
        envelop.payload = {
            "ok": True,
            "data": {"categories": cats, "total": len(cats)},
        }
        return envelop

    if action == "stats":
        category_count: Dict[str, int] = {}
        for s in index.get("skills", []):
            cat = s.get("category", "")
            category_count[cat] = category_count.get(cat, 0) + 1
        envelop.payload = {
            "ok": True,
            "data": {
                "total_skills": len(index.get("skills", [])),
                "total_categories": len(index.get("categories", [])),
                "last_scan": index.get("last_scan"),
                "skipped": index.get("skipped", []),
                "category_count": category_count,
            },
        }
        return envelop

    # ============ pin / unpin / index / get_active / clear ============
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
            }
            return envelop

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
        }
        return envelop

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

        envelop.payload = {
            "ok": True,
            "message": f"📋 已降级: {info.get('title', skill_id)}（仅保留目录索引）",
            "skill_id": skill_id,
            "title": info.get("title"),
            "status": "indexed",
        }
        return envelop

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

    if action == "get_active":
        state = _load_state(user_id, session_id)
        content = _compose_active_content(user_id, session_id, state)
        index_text = _index_to_text(state)
        pinned = state.get("pinned", {})
        pinned_count = sum(1 for s in pinned.values() if s.get("status") == "pinned")
        envelop.payload = {
            "ok": True, "user_id": user_id, "session_id": session_id,
            "content": content, "size": len(content), "has_skill": bool(content),
            "index_text": index_text, "total_skills": len(pinned),
            "pinned_count": pinned_count,
        }
        return envelop

    if action == "clear":
        state = _load_state(user_id, session_id)
        pinned = state.get("pinned", {})
        for sid in pinned:
            pinned[sid]["status"] = "indexed"
            pinned[sid].pop("pinned_at", None)
        state["pinned"] = pinned
        _save_state(user_id, session_id, state)
        envelop.payload = {
            "ok": True,
            "user_id": user_id,
            "session_id": session_id,
            "message": "✅ 技能已卸载（所有钉住降级为索引）",
            "mode": "clear",
            "indexed_count": len(pinned),
        }
        return envelop

    # save_active / load：skill.txt 已废弃，改为报错提示（避免前端旧调用 NameError）
    if action == "save_active":
        envelop.payload = {
            "ok": False,
            "error": "save_active 已废弃：Step 3 后技能由 pinned.json 管理，请用 pin / unpin 操作",
        }
        return envelop

    if action == "load":
        envelop.payload = {
            "ok": False,
            "error": "load 已废弃：请用 action=pin + skill_id",
        }
        return envelop

    envelop.payload = {"ok": False, "error": f"未知 action: {action}"}
    return envelop


# ============================================================
# help
# ============================================================

def help():
    return {
        "route": "builtins/tools/skill_loader",
        "description": "技能加载器 — 搜索 / 加载 / 管理技能（users/{user_id}/sessions/{session_id}/pinned.json）",
        "input": {
            "action": "scan | list | search | detail | categories | stats | pin | unpin | clear | get_active | loadfromfile | index",
            "query": "搜索问题（search 时）",
            "keywords": "关键词列表（search 时，可选，为空时用 query 兜底）",
            "category": "分类筛选（list / search 时，可选）",
            "limit": "返回数量（默认 10）",
            "skill_id": "技能 ID（detail / pin / unpin 时）",
            "session_id": "会话 ID（pin / unpin / index / get_active / clear / loadfromfile / list(include_status) 必填）",
            "include_status": "布尔，list 时传 true 则返回 status / deletable",
            "path": "文件路径（loadfromfile 时）",
        },
        "output": {
            "ok": "是否成功",
            "data": "结果数据",
            "message": "结果消息",
            "user_id": "身份标识",
            "session_id": "会话标识",
            "size": "loadfromfile 时本次读到的字符数",
            "hash": "loadfromfile 时本次内容 md5",
            "skipped": "loadfromfile 时内容未变、跳过",
            "reloaded": "loadfromfile 时内容变更、已替换",
        },
    }
