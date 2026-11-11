"""
cogitor — AICP 系统概览（分组 + 描述版，对齐 SKILL 新规范 v4）
========================================
无状态、实时、按需。不需要启动、不需要心跳、不需要分析。
- get_map：系统概览（分类 + 数量 + 描述）
- list_plugins：一次返回 分类 + 插件名 + 描述
- search：统一搜索（内部插件 + 本地 skill + MCP 外部工具）
- 零 token（描述从源码文件提取，有缓存）
- 毫秒级响应（首次读文件，之后读缓存）

skill 检索（对齐 _STANDARD.md v1）：
  - frontmatter 读 triggers / not_triggers / examples / tags
  - 语料权重：name(10) = triggers(10) > desc(5) > examples(3)
  - not_triggers 完整词匹配，命中 -5（负信号）
  - examples 保留外层引号，状态机切分，不死循环

tokenize（中英分流）：
  - 中文串：找出停用词覆盖区间，只跳过完全落在停用词内部的 2-gram；
    跨边界但不在停用词内部的 2-gram 保留（"改一下" → 保留 "改一"，跳过 "下那"）
  - 英文词：原词 + 轻量词形归一（ing/ed/es/s）
  - 统一停用词过滤

内部插件检索降权：
  - 2 字以内纯中文（文件/代码/系统）是泛词，name +5、desc +2

排序 tie-breaker：
  - score 相同 → triggers 命中数降序 → name 升序（稳定排序）

语料侧 stem：
  - triggers / not_triggers / examples 拼语料时也做 stem，
    保证 query 的 stem 能匹配 triggers 的原词（双向匹配）。
"""

import core
import json
import re
from pathlib import Path


TITLE_PATTERN = re.compile(r'<title>(.*?)</title>', re.DOTALL)

# ★ 基于 cogitor.py 自己的位置，不依赖 CWD
# cogitor.py 在 plugins/builtins/tools/cogitor.py
# 往上 3 级到 plugins/
PLUGINS_ROOT = Path(__file__).parent.parent.parent

PER_GROUP_LIMIT = 15       # 每个分类最多列 15 个插件
PER_PROJECT_LIMIT = 20     # applications 最多列 20 个项目

# 描述缓存：{plugin_name: desc}
_desc_cache = {}
_virtual_desc: dict = {}  # name -> desc（外部虚拟插件，如 MCP 工具）


# ============================================================
# 停用词 / 英文 stem / 语料 stem / tokenize
# ============================================================

_STOPWORDS = {
    # 中文
    "帮我", "一下", "那个", "这个", "怎么", "什么", "可以", "需要",
    "现在", "然后", "就是", "还是", "但是", "因为", "所以",
    "一个", "一些", "我们", "你们", "他们", "自己", "这样", "那样",
    # 英文
    "the", "a", "an", "to", "of", "in", "on", "for", "and", "or",
    "is", "are", "this", "that", "it", "my", "me", "i", "you",
    "please", "help", "can", "could", "would", "should",
    "do", "does", "did", "be", "been", "being", "have", "has", "had",
    "with", "from", "by", "at", "as", "so", "if", "then",
}

# 中文停用词集合（只保留含汉字的，用于 2-gram 覆盖区间计算）
_STOPWORDS_CN = {w for w in _STOPWORDS if re.search(r'[一-龥]', w)}


def _stem_en(w: str) -> str:
    """英文轻量词形归一（只处理最常见后缀，不做完整 Porter）。"""
    if len(w) <= 3:
        return w
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[:-len(suf)]
    return w


def _stem_corpus(words) -> str:
    """语料侧 stem：原词 + stem 都拼进去，保证双向匹配。"""
    out = []
    for w in words:
        w = str(w)
        out.append(w)
        st = _stem_en(w.lower())
        if st != w.lower():
            out.append(st)
    return " ".join(out)

def _tokenize(query):
    """按标点切词 → 中文按停用词切分 → 片段内按中英文切 → 中文部分做 2-gram。

    例："帮我改一下那个大文件"
      → 停用词切分：["改", "大文件"]
      → 片段内：改（长度1，原词）、大文件（中文2-gram：大文、文件）
      → 输出：["帮我改一下那个大文件", "改", "大文件", "大文", "文件"]

    例："这个html太长了改不动"
      → 停用词切分：["html太长了改不动"]
      → 片段内：html（英文）、太长了改不动（中文2-gram：太长、长了、了改、改不、不动）
      → 输出：["这个html太长了改不动", "html太长了改不动", "html", "太长", "长了", "了改", "改不", "不动"]

    例："fixing this html"
      → ["fixing", "fix", "html"]
    """
    if not query:
        return []
    tokens = re.split(r'[^一-龥a-zA-Z0-9_-]+', query.strip())
    tokens = [t.lower() for t in tokens if t]
    out = []
    _cn_sorted = sorted(_STOPWORDS_CN, key=len, reverse=True)

    for t in tokens:
        if t in _STOPWORDS:
            continue
        has_cn = bool(re.search(r'[一-龥]', t))
        has_en = bool(re.search(r'[a-zA-Z]', t))

        # 纯英文词：原词 + stem
        if has_en and not has_cn:
            out.append(t)
            st = _stem_en(t)
            if st != t:
                out.append(st)
            continue

        # 中文 / 混合词：原词 + 按停用词切分 + 片段内中英文切 + 中文 2-gram
        out.append(t)
        if len(t) >= 3 and has_cn:
            # 1. 按停用词切分（长停用词优先）
            segments = []
            buf = []
            i = 0
            while i < len(t):
                matched = None
                for sw in _cn_sorted:
                    if t.startswith(sw, i):
                        matched = sw
                        break
                if matched:
                    if buf:
                        segments.append("".join(buf))
                        buf = []
                    i += len(matched)
                else:
                    buf.append(t[i])
                    i += 1
            if buf:
                segments.append("".join(buf))

            # 2. 每个片段：整体进 out；片段内按中英文切；中文部分做 2-gram
            for seg in segments:
                if not seg:
                    continue
                out.append(seg)
                for part in re.split(r'([一-龥]+)', seg):
                    if not part:
                        continue
                    if re.search(r'[a-zA-Z]', part):
                        out.append(part)                # 英文部分，原词
                    elif re.search(r'[一-龥]', part):
                        if len(part) >= 2:
                            for j in range(len(part) - 1):
                                bg = part[j:j+2]
                                if bg not in _STOPWORDS:   # 过滤停用词 2-gram
                                    out.append(bg)

    # 3. 去重保序
    seen, dedup = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            dedup.append(x)
    return dedup

def _split_csv(s) -> list:
    """单行 CSV → list。分隔符支持 , ， 、 ; ；。"""
    return [x.strip() for x in re.split(r"[,，、;；]", str(s or "")) if x.strip()]


def _split_examples(s) -> list:
    """examples 带引号 CSV → list。状态机，保证 i 前进，不会死循环。"""
    s = str(s or "").strip()
    parts, buf = [], []
    in_quote = False
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == '"':
            in_quote = not in_quote
            i += 1
            continue
        if not in_quote and c in ",，、;；":
            v = "".join(buf).strip()
            if v:
                parts.append(v)
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    v = "".join(buf).strip()
    if v:
        parts.append(v)
    return parts


def _safe_int(value, default: int = 10) -> int:
    """安全取整数：字符串数字 / 浮点 / bool 都能转，转不了用默认值，永不抛异常。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


# ============================================================
# 前端扫描
# ============================================================

def _scan_frontend() -> dict:
    """扫描 www/apps/ 目录，提取每个系统的标题"""
    result = {}
    www_dir = Path("www/apps")
    if not www_dir.exists():
        return result

    for app_dir in www_dir.iterdir():
        if not app_dir.is_dir():
            continue
        index_html = app_dir / "index.html"
        if not index_html.exists():
            continue

        app_name = app_dir.name
        title = app_name

        try:
            content = index_html.read_text(encoding="utf-8")
            match = TITLE_PATTERN.search(content)
            if match:
                title = match.group(1).strip()
        except Exception:
            pass

        result[app_name] = title

    return result


# ============================================================
# 描述提取（兼容多种来源）
# ============================================================

def _extract_desc(plugin_name: str) -> str:
    """
    从插件源码文件提取描述。
    优先级：
      1. help() 里的 description 字段（只匹配 help() 之后的）
      2. @AICP_ALIGN 的 actions 列表
      3. 模块 docstring 第一行
      4. 空字符串
    """
    vd = _virtual_desc.get(plugin_name)
    if vd:
        return vd

    if plugin_name in _desc_cache:
        return _desc_cache[plugin_name]

    desc = ""

    for suffix in (".py", ".ts", ".js"):
        f = PLUGINS_ROOT / f"{plugin_name}{suffix}"
        if not f.exists():
            continue
        try:
            content = f.read_text(encoding="utf-8")

            # ---- 1. description 字段（只匹配 help() 之后的） ----
            help_pos = content.find("def help")
            search_from = help_pos if help_pos != -1 else 0
            m = re.search(
                r'["\']?description["\']?\s*[:=]\s*["\']([^"\']{1,100})["\']',
                content[search_from:]
            )
            if m:
                desc = m.group(1).strip()
                desc = desc.rstrip('"\'')
                if desc:
                    break

            # ---- 2. @AICP_ALIGN 的 actions ----
            m2 = re.search(r'@AICP_ALIGN:\s*actions=([^\n|]+)', content)
            if m2:
                actions = [a.strip() for a in m2.group(1).split(",") if a.strip()]
                if actions:
                    desc = ", ".join(actions[:3])
                    if len(actions) > 3:
                        desc += f" 等 {len(actions)} 个"
                    break

            # ---- 3. 模块 docstring 第一行 ----
            m3 = re.match(r'\s*"""?\s*\n?\s*(.{1,100}?)\n', content)
            if m3:
                d = m3.group(1).strip()
                if d and not d.startswith(("import ", "from ", "#")):
                    desc = d.rstrip('"\'')
                    if desc:
                        break
        except Exception:
            pass

    if len(desc) > 50:
        desc = desc[:48] + "…"

    _desc_cache[plugin_name] = desc
    return desc


def _extract_yaml_field(yaml_path: Path, field: str) -> str:
    """从 app.yaml 读取一个顶层字段（如 description）。"""
    try:
        if not yaml_path.exists():
            return ""
        text = yaml_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    m = re.search(r"^" + re.escape(field) + r"\s*[:=]\s*(.+)$", text, re.MULTILINE)
    if not m:
        return ""
    return m.group(1).strip().strip("\"'").strip()


def _desc_for(name: str, skill_desc: dict = None, mcp_desc: dict = None) -> str:
    """给补全列表生成描述：
    skill/{group}/{name}  → SKILL.md frontmatter
    applications/{proj}/x → 同项目 app.yaml 的 description
    mcp/{server}/{tool}   → MCP 工具自带 description
    其余                 → 源码提取（_extract_desc）
    """
    skill_desc = skill_desc or {}
    mcp_desc = mcp_desc or {}
    desc = ""
    if name.startswith("skill/"):
        desc = skill_desc.get(name, "") or ""
    elif name.startswith("mcp/"):
        desc = mcp_desc.get(name, "") or ""
    elif name.startswith("applications/"):
        parts = name.split("/")
        if len(parts) >= 2:
            y = PLUGINS_ROOT / "applications" / parts[1] / "app.yaml"
            desc = _extract_yaml_field(y, "description")
            if not desc:
                desc = _extract_yaml_field(y, "name")
    if not desc:
        try:
            desc = _extract_desc(name)
        except Exception:
            desc = ""
    desc = " ".join(str(desc).split())
    if len(desc) > 60:
        desc = desc[:58] + "…"
    return desc


# ============================================================
# 插件名获取
# ============================================================

async def _get_plugin_names(agent) -> list:
    """从 _registry 获取后端插件名列表"""
    try:
        result = await agent.system.call(core.Envelop(
            sender="builtins/tools/cogitor",
            receiver="os/_registry",
            payload={"action": "list"}
        ))
        if not result or not result.payload or not result.payload.get("ok"):
            return []
        apis = result.payload.get("apis", [])
        names = []
        for a in apis:
            nm = a.get("name", "")
            if not nm:
                continue
            if a.get("is_virtual"):
                helpd = a.get("help") or {}
                d = helpd.get("description") or ""
                if d:
                    _virtual_desc[nm] = d
            names.append(nm)
        return names
    except Exception:
        return []


# ============================================================
# MCP 外部工具（从 os/_mcp_client 拉）
# ============================================================

async def _list_mcp_tools(agent) -> list:
    """从 os/_mcp_client 拉外部工具列表（唯一真相源）。
    返回：[{server, tool, description, inputSchema}, ...]
    """
    try:
        result = await agent.system.call(core.Envelop(
            sender="builtins/tools/cogitor",
            receiver="os/_mcp_client",
            payload={"action": "list_tools"},
        ))
        if not result or not result.payload or not result.payload.get("ok"):
            return []
        return result.payload.get("data", []) or []
    except Exception:
        return []


# ============================================================
# 分组 + 描述
# ============================================================

def _group_with_desc(plugin_names: list) -> dict:
    groups = {}
    for name in plugin_names:
        if not name:
            continue
        parts = name.split("/")

        if parts[0] == "applications" and len(parts) >= 3:
            category = "applications"
            project = parts[1]
            if category not in groups:
                groups[category] = {}
            if "projects" not in groups[category]:
                groups[category]["projects"] = {}
            if project not in groups[category]["projects"]:
                groups[category]["projects"][project] = []
            groups[category]["projects"][project].append(name)
        else:
            category = parts[0] if parts else "_root"
            if category not in groups:
                groups[category] = {}
            if "plugins" not in groups[category]:
                groups[category]["plugins"] = []
            groups[category]["plugins"].append(name)

    result = {}
    for cat, info in sorted(groups.items()):
        if cat == "_root":
            continue

        if info.get("projects"):
            proj_brief = {}
            shown = 0
            for proj, plugins in sorted(info["projects"].items()):
                if shown >= PER_PROJECT_LIMIT:
                    break
                desc = ""
                for p in plugins:
                    d = _extract_desc(p)
                    if d:
                        desc = d
                        break
                proj_brief[proj] = {"count": len(plugins), "desc": desc}
                shown += 1

            total_projects = len(info["projects"])
            entry = {
                "projects": proj_brief,
                "count": sum(p["count"] for p in proj_brief.values()),
                "total_projects": total_projects,
            }
            if total_projects > PER_PROJECT_LIMIT:
                entry["more_projects"] = total_projects - PER_PROJECT_LIMIT
            result[cat] = entry
        elif info.get("plugins"):
            plugins = sorted(info["plugins"])
            shown = plugins[:PER_GROUP_LIMIT]
            plugin_brief = [
                {"name": p, "desc": _extract_desc(p)}
                for p in shown
            ]
            entry = {"plugins": plugin_brief, "count": len(plugins)}
            if len(plugins) > PER_GROUP_LIMIT:
                entry["more"] = len(plugins) - PER_GROUP_LIMIT
            result[cat] = entry

    return result


def _format_group_summary(groups: dict, total: int) -> str:
    lines = [f"系统共有 {total} 个后端插件。", ""]
    lines.append("【插件分组】")
    lines.append("")

    for category in sorted(groups.keys()):
        info = groups[category]
        count = info.get("count", 0)

        if info.get("projects"):
            total_projects = info.get("total_projects", len(info["projects"]))
            lines.append(f"  • {category}（{count} 个插件，{total_projects} 个项目）")
            for proj in sorted(info["projects"].keys()):
                p = info["projects"][proj]
                desc = f" — {p['desc']}" if p.get("desc") else ""
                lines.append(f"      - {proj}（{p['count']} 个）{desc}")
            if info.get("more_projects"):
                lines.append(f"      - ... 还有 {info['more_projects']} 个项目")
        elif info.get("plugins"):
            lines.append(f"  • {category}（{count} 个）")
            for p in info.get("plugins", []):
                desc = f" — {p['desc']}" if p.get("desc") else ""
                lines.append(f"      - {p['name']}{desc}")
            if info.get("more"):
                lines.append(f"      - ... 还有 {info['more']} 个")

    return "\n".join(lines)


# ============================================================
# 本地 skill 扫描
# ============================================================

def _parse_frontmatter(text: str) -> tuple:
    """拆 frontmatter → (meta, body)。没有 frontmatter 时 meta 为空、body 为全文。

    examples 字段保留原样（不 strip 外层引号），否则 `"a", "b"` 会被剥成 `a", "b`，
    后续按引号切分会进死循环或错切。
    """
    meta = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                if ":" in line and not line.strip().startswith("#"):
                    k, v = line.split(":", 1)
                    k = k.strip().lower()
                    raw_v = v.strip()
                    if k in ("examples",):
                        meta[k] = raw_v          # 保留引号
                    else:
                        meta[k] = raw_v.strip("\"'")
            body = text[end + 4:].strip()
    return meta, body


def _first_line_desc(text: str, limit: int = 120) -> str:
    """正文第一行有意义的文字（跳过标题/空行/注释/分隔线），用作描述兜底。"""
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("<!--") or set(s) <= set("= -_"):
            continue
        return s[:limit]
    return ""


def _scan_skills() -> list:
    """扫描技能源，返回能力条目。

    唯一来源：data/skills/<rel>/SKILL.md → name = skill/<rel>
    命名可逆：use_tool(target="skill/...") 与 contract_agent.get 都能按同一规则解析回来。

    返回字段：name / desc / triggers / not_triggers / examples / tags / path / source
    """
    items = []

    def _mk_desc(meta, text):
        desc = meta.get("description") or meta.get("title") or meta.get("name") or ""
        desc = " ".join(str(desc).split())
        if not desc:
            desc = _first_line_desc(text)
        if len(desc) > 60:
            desc = desc[:58] + "…"
        return desc

    root = Path("data/skills")
    if root.exists():
        for f in root.rglob("SKILL.md"):
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            meta, _body = _parse_frontmatter(text)
            rel = f.parent.relative_to(root).as_posix()
            items.append({
                "name": "skill/" + rel,
                "desc": _mk_desc(meta, text),
                "triggers": _split_csv(meta.get("triggers", "")),
                "not_triggers": _split_csv(meta.get("not_triggers", "")),
                "examples": _split_examples(meta.get("examples", "")),
                "tags": _split_csv(meta.get("tags", "")),
                "path": f.as_posix(),
                "source": "skills",
            })

    items.sort(key=lambda x: x["name"])
    return items


# ============================================================
# 三路搜索（内部插件 / 本地 skill / MCP）
# ============================================================

def _term_search(terms, plugin_names, limit=10, mode="any"):
    if not terms:
        return []
    terms = [t.lower().strip() for t in terms if t and t.strip()]
    if not terms:
        return []

    results = []
    for name in plugin_names:
        if name.startswith("mcp/") or name.startswith("skill/"):
            continue
        desc = _extract_desc(name)
        name_lower = name.lower()
        desc_lower = desc.lower()

        score = 0
        matched_terms = []
        match_detail = {}
        desc_matched = []

        for term in terms:
            is_short_cn = len(term) <= 2 and bool(re.fullmatch(r'[一-龥]+', term))
            name_w = 5 if is_short_cn else 10
            desc_w = 2 if is_short_cn else 5

            term_score = 0
            matched_in = []

            if term in name_lower:
                term_score += name_w
                matched_in.append("name")
            if term in desc_lower:
                term_score += desc_w
                matched_in.append("desc")
                idx = desc_lower.find(term)
                if idx >= 0:
                    start = max(0, idx - 20)
                    end = min(len(desc), idx + len(term) + 20)
                    snippet = desc[start:end]
                    if start > 0:
                        snippet = "…" + snippet
                    if end < len(desc):
                        snippet = snippet + "…"
                    desc_matched.append({"term": term, "snippet": snippet})

            if term_score > 0:
                score += term_score
                matched_terms.append(term)
                match_detail[term] = matched_in

        if mode == "all":
            if len(matched_terms) < len(terms):
                continue
        else:
            if not matched_terms:
                continue

        if desc:
            score += 2
        score += len(matched_terms) * 3

        results.append({
            "name": name,
            "desc": desc,
            "score": score,
            "matched_terms": matched_terms,
            "match_detail": match_detail,
            "desc_matched": desc_matched,
            "triggers_hits": 0,
        })

    results.sort(key=lambda x: (-x["score"], x["name"]))
    return results[:limit]


def _search_skills(terms, limit=10, mode="any") -> list:
    """本地 skill 搜索。

    语料 = name + triggers + desc + examples，not_triggers 完整词命中减分。
    权重：name(10) = triggers(10) > desc(5) > examples(3)。
    triggers/not_triggers/examples 语料侧做 stem，保证 query 的 stem 能匹配原词。
    """
    hits = []
    for s in _scan_skills():
        name_l = s["name"].lower()
        desc_l = s["desc"].lower()
        triggers_l = _stem_corpus(s.get("triggers") or []).lower()
        not_set = set(str(w).lower() for w in (s.get("not_triggers") or []))
        examples_l = _stem_corpus(s.get("examples") or []).lower()

        matched = []
        triggers_hits = 0
        score = 0
        for term in terms:
            tl = term.lower()
            if tl in name_l:
                score += 10
                matched.append(term)
            elif tl in triggers_l:
                score += 10
                matched.append(term)
                triggers_hits += 1
            elif tl in examples_l:
                score += 3
                matched.append(term)
            elif tl in desc_l:
                score += 5
                matched.append(term)
            # 负信号：完整词匹配，不用子串（避免 "大文件" 误伤 "文件"）
            if tl in not_set:
                score -= 5

        if not matched:
            continue
        if mode == "all" and len(matched) < len(terms):
            continue
        score += len(matched) * 3
        if s["desc"]:
            score += 2
        if score <= 0:
            continue  # 负信号把分扣光的，不返回

        hits.append({
            "name": s["name"],
            "desc": s["desc"],
            "score": score,
            "matched_terms": matched,
            "match_detail": {},
            "desc_matched": [],
            "is_skill": True,
            "path": s["path"],
            "triggers_hits": triggers_hits,
        })
    hits.sort(key=lambda x: (-x["score"], -x["triggers_hits"], x["name"]))
    return hits[:limit]


def _search_mcp_tools(terms, mcp_tools, limit=10, mode="any") -> list:
    """MCP 外部工具搜索。语料 = name + description + inputSchema 参数名（全部 stem）。"""
    hits = []
    for t in mcp_tools:
        server = t.get("server", "")
        tool = t.get("tool", "")
        desc = (t.get("description") or "")[:200]
        name = f"mcp/{server}/{tool}"

        schema = t.get("inputSchema") or {}
        props = schema.get("properties") if isinstance(schema, dict) else None
        param_names = " ".join(props.keys()) if isinstance(props, dict) else ""

        name_l = name.lower()
        desc_l = (desc + " " + _stem_corpus(param_names.split())).lower()

        matched = []
        score = 0
        for term in terms:
            tl = term.lower()
            if tl in name_l:
                score += 10
                matched.append(term)
            elif tl in desc_l:
                score += 5
                matched.append(term)
        if not matched:
            continue
        if mode == "all" and len(matched) < len(terms):
            continue
        score += len(matched) * 3
        if desc:
            score += 2
        hits.append({
            "name": name,
            "desc": desc,
            "score": score,
            "matched_terms": matched,
            "match_detail": {},
            "desc_matched": [],
            "is_mcp": True,
            "server": server,
            "tool": tool,
            "inputSchema": t.get("inputSchema", {}),
            "triggers_hits": 0,
        })

    # ★ 以下整段在 for 循环外（缩进从 8 空格减到 4 空格）
    if not hits:
        return []

    by_server = {}
    for h in hits:
        by_server.setdefault(h.get("server", ""), []).append(h)
    for srv in by_server:
        by_server[srv].sort(key=lambda x: (-x["score"], x["name"]))

    merged = []
    cursors = {srv: 0 for srv in by_server}
    servers = sorted(by_server.keys())
    while len(merged) < limit:
        progressed = False
        for srv in servers:
            if len(merged) >= limit:
                break
            idx = cursors[srv]
            bucket = by_server[srv]
            if idx < len(bucket):
                merged.append(bucket[idx])
                cursors[srv] += 1
                progressed = True
        if not progressed:
            break
    return merged


# ============================================================
# terms 归一化
# ============================================================

def _normalize_terms(raw_terms):
    if raw_terms is None:
        return None, None

    if isinstance(raw_terms, list):
        cleaned = []
        for t in raw_terms:
            if t is None:
                continue
            s = str(t).strip()
            if s:
                cleaned.append(s)
        return cleaned, None

    if isinstance(raw_terms, dict):
        for key in ("item", "items", "terms", "terms_list", "keywords", "query"):
            if key in raw_terms:
                v = raw_terms[key]
                if isinstance(v, list):
                    return _normalize_terms(v)
                if isinstance(v, str) and v.strip():
                    return [v.strip()], None
        return None, (
            f"terms 必须是字符串列表，收到的是对象: {raw_terms}。"
            f'正确格式: ["fetch", "http"]'
        )

    if isinstance(raw_terms, str):
        s = raw_terms.strip()
        if s:
            return [s], None
        return None, None

    return None, (
        f"terms 必须是字符串列表，收到的是 {type(raw_terms).__name__}: {raw_terms}。"
        f'正确格式: ["fetch", "http"]'
    )


# ============================================================
# 主入口
# ============================================================

async def execute(envelop, agent):
    action = envelop.payload.get("action", "get_map")

    # ============================================================
    # get_map：系统概览
    # ============================================================
    if action == "get_map":
        plugin_names = await _get_plugin_names(agent)
        if not plugin_names:
            envelop.payload = {"ok": False, "error": "无法获取插件列表"}
            return envelop

        frontend = _scan_frontend()
        groups = _group_with_desc(plugin_names)

        total_plugins = len(plugin_names)
        total_frontend = len(frontend)

        mcp_tools = await _list_mcp_tools(agent)
        mcp_servers = {}
        skills = _scan_skills()
        for t in mcp_tools:
            sv = t.get("server", "")
            if sv:
                mcp_servers.setdefault(sv, 0)
                mcp_servers[sv] += 1

        summary_lines = [
            f"系统共有 {total_plugins} 个后端插件，{total_frontend} 个前端系统。",
            "",
        ]

        if frontend:
            summary_lines.append("【纯前端应用】")
            for app_name, title in sorted(frontend.items()):
                summary_lines.append(f"  • {app_name} — {title}")
            summary_lines.append("")

        summary_lines.append(_format_group_summary(groups, total_plugins))

        if mcp_servers:
            summary_lines.append("")
            summary_lines.append(f"【MCP 外部工具】（{len(mcp_servers)} 个 server，{len(mcp_tools)} 个工具）")
            for sv in sorted(mcp_servers.keys()):
                summary_lines.append(f"  • {sv}（{mcp_servers[sv]} 个工具）")

        if skills:
            summary_lines.append("")
            preview = skills[:3]
            summary_lines.append(f"【本地 Skill】{len(skills)} 个（list_plugins(category=\"skill\") 列全部）")
            for s in preview:
                summary_lines.append(f"  • {s['name']} — {s['desc']}")
            if len(skills) > 3:
                summary_lines.append(f"  • ... 还有 {len(skills) - 3} 个")

        envelop.payload = {
            "ok": True,
            "summary": "\n".join(summary_lines),
            "data": {
                "total_plugins": total_plugins,
                "total_frontend": total_frontend,
                "frontend": frontend,
                "mcp_servers": mcp_servers,
                "mcp_tool_count": len(mcp_tools),
                "skills": skills,
                "total_skills": len(skills),
            },
        }
        return envelop

    # ============================================================
    # list_all：返回全部插件名（供前端自动补全）
    # 可选 exclude_prefix：字符串或列表，按前缀过滤
    # ============================================================
    elif action == "list_all":
        plugin_names = await _get_plugin_names(agent)
        if not plugin_names:
            envelop.payload = {"ok": False, "error": "无法获取插件列表"}
            return envelop

        raw_ex = envelop.payload.get("exclude_prefix") or []
        if isinstance(raw_ex, str):
            raw_ex = [raw_ex]
        excludes = tuple(str(x) for x in raw_ex if x)

        # ---- 虚拟路由 1：本地 skill（skill/{group}/{name}）----
        skill_names = []
        skill_desc_map = {}
        for s in _scan_skills():
            nm = s.get("name") or ""
            if nm:
                skill_names.append(nm)
                skill_desc_map[nm] = s.get("desc") or ""

        # ---- 虚拟路由 2：MCP 外部工具（mcp/{server}/{tool}）----
        mcp_names = []
        mcp_desc_map = {}
        try:
            mcp_tools = await _list_mcp_tools(agent)
        except Exception:
            mcp_tools = []
        for t in mcp_tools or []:
            if not isinstance(t, dict):
                continue
            srv = str(t.get("server") or "").strip()
            tool = str(t.get("tool") or "").strip()
            if srv and tool:
                route = f"mcp/{srv}/{tool}"
                mcp_names.append(route)
                mcp_desc_map[route] = str(t.get("description") or "")

        all_names = list(plugin_names) + skill_names + mcp_names

        names = sorted(
            n for n in all_names
            if n and not any(n.startswith(p) for p in excludes)
        )

        # ---- 描述（供前端补全下拉展示）----
        desc_map = {}
        for n in names:
            desc_map[n] = _desc_for(n, skill_desc_map, mcp_desc_map)

        envelop.payload = {
            "ok": True,
            "plugins": names,
            "total": len(names),
            "excluded": list(excludes),
            "descriptions": desc_map,
            "sources": {
                "plugins": len(plugin_names),
                "skills": len(skill_names),
                "mcp": len(mcp_names),
            },
        }
        return envelop

    # ============================================================
    # search：统一搜索（内部 + skill + MCP）
    # ============================================================
    elif action == "search":
        raw_terms = envelop.payload.get("terms")
        terms, err = _normalize_terms(raw_terms)

        if err:
            envelop.payload = {"ok": False, "error": err}
            return envelop

        if not terms:
            query = envelop.payload.get("query", "").strip()
            terms = _tokenize(query)

        if not terms:
            envelop.payload = {
                "ok": False,
                "error": "缺少 terms 或 query 参数。terms 应该是字符串列表，如 [\"fetch\"]",
            }
            return envelop

        limit = _safe_int(envelop.payload.get("limit", 10), 10)
        mode = envelop.payload.get("mode", "any")  # any | all
        server_filter = (envelop.payload.get("server") or "").strip()  # ★ 新增

        # 三路搜索
        plugin_names = await _get_plugin_names(agent)
        plugin_hits = _term_search(terms, plugin_names, limit, mode)

        skill_hits = _search_skills(terms, limit, mode)

        mcp_tools = await _list_mcp_tools(agent)
        if server_filter:                       # ★ 新增
            mcp_tools = [t for t in mcp_tools if t.get("server") == server_filter]
        mcp_hits = _search_mcp_tools(terms, mcp_tools, limit, mode)

        # 合并 + 排序（tie-breaker：score 降序 → triggers_hits 降序 → name 升序）
        all_hits = plugin_hits + skill_hits + mcp_hits
        all_hits.sort(key=lambda x: (-x["score"], -x.get("triggers_hits", 0), x["name"]))
        results = all_hits[:limit]

        envelop.payload = {
            "ok": True,
            "terms": terms,
            "results": results,
            "total": len(results),
            "sources": {
                "plugins": len(plugin_hits),
                "skills": len(skill_hits),
                "mcp": len(mcp_hits),
            },
        }
        return envelop

    # ============================================================
    # list_plugins：一次返回分组 + 插件名 + 描述
    # ============================================================
    elif action == "list_plugins":
        category = envelop.payload.get("category", "").strip()

        # ★ category="skill"：列全部本地 skill（带 triggers / not_triggers / examples / tags）
        if category == "skill":
            skills = _scan_skills()
            envelop.payload = {
                "ok": True,
                "category": "skill",
                "plugins": [
                    {
                        "name": s["name"],
                        "desc": s["desc"],
                        "triggers": s.get("triggers", []),
                        "not_triggers": s.get("not_triggers", []),
                        "examples": s.get("examples", []),
                        "tags": s.get("tags", []),
                        "path": s["path"],
                    }
                    for s in skills
                ],
                "total": len(skills),
            }
            return envelop

        # ★ category="mcp"：列全部 MCP 外部工具
        if category == "mcp":
            mcp_tools = await _list_mcp_tools(agent)
            envelop.payload = {
                "ok": True,
                "category": "mcp",
                "plugins": [
                    {
                        "name": f"mcp/{t.get('server')}/{t.get('tool')}",
                        "desc": (t.get("description") or "")[:120],
                        "server": t.get("server"),
                        "tool": t.get("tool"),
                    }
                    for t in mcp_tools
                ],
                "total": len(mcp_tools),
            }
            return envelop

        # 内部插件
        plugin_names = await _get_plugin_names(agent)
        if not plugin_names:
            envelop.payload = {"ok": False, "error": "无法获取插件列表"}
            return envelop

        if category:
            matched = [
                n for n in plugin_names
                if n == category or n.startswith(category + "/")
            ]
            if not matched:
                envelop.payload = {
                    "ok": False,
                    "error": f"没有找到分类或插件: {category}",
                    "hint": "先用 list_plugins 不带参数查看所有分类；本地 skill 用 category=\"skill\"，MCP 外部工具用 category=\"mcp\"",
                }
                return envelop

            plugins = [
                {"name": m, "desc": _extract_desc(m)}
                for m in sorted(matched)
            ]
            envelop.payload = {
                "ok": True,
                "category": category,
                "plugins": plugins,
                "total": len(plugins),
            }
            return envelop

        groups = _group_with_desc(plugin_names)

        envelop.payload = {
            "ok": True,
            "groups": groups,
            "total": len(plugin_names),
            "hint": "每个分类最多列 15 个，超出用 more 标记。想看完整列表用 list_plugins category=<分类>；本地 skill 用 category=\"skill\"，MCP 外部工具用 category=\"mcp\"",
        }
        return envelop

    else:
        envelop.payload = {"ok": False, "error": f"未知 action: {action}"}
        return envelop


# ============================================================
# help
# ============================================================

def help() -> dict:
    return {
        "route": "builtins/tools/cogitor",
        "description": "系统概览 — 分组+描述+统一搜索（内部插件 + 本地 skill + MCP 外部工具）",
        "input": {
            "action": "get_map | list_plugins | search | list_all",
            "category": "（list_plugins 可选）分类名；\"skill\" 列全部 skill，\"mcp\" 列全部 MCP 工具",
            "terms": "（search 可选）字符串列表，如 [\"fetch\", \"http\"]。必须是 list，不是对象，不是字符串。",
            "query": "（search 可选）查询字符串，会按空格/标点拆词（中文 2-gram + 英文词形归一，停用词过滤）。和 terms 二选一。",
            "limit": "（search 可选）返回数量，默认 10",
            "mode": "（search 可选）any 任一命中 | all 全部命中，默认 any",
            "exclude_prefix": "（list_all 可选）字符串或列表，按前缀过滤",
        },
        "output": {
            "ok": "是否成功",
            "summary": "系统概览文本（get_map）",
            "groups": "分组+插件名+描述（list_plugins 无参数）",
            "plugins": "完整插件列表（list_plugins 指定 category）；category=\"skill\" 时带 triggers/not_triggers/examples/tags",
            "results": "搜索结果（search），含 name/desc/score/is_skill/is_mcp",
            "sources": "搜索结果的分路计数（plugins / skills / mcp）",
        },
        "examples": [
            'search terms=["股票","stock"] → 内部插件 + 本地 skill + MCP 三路合并',
            'search terms=["fetch"] → 搜 fetch 相关插件 / skill / MCP 工具',
            'search query="帮我改一下那个大文件" → terms 展开为 ["帮我改一下那个大文件","我改","改一","大文","文件"]',
            'search query="fixing this html" → terms 展开为 ["fixing","fix","html"]',
            'list_plugins category="skill" → 列全部 skill，带 triggers / not_triggers / examples / tags',
        ],
    }
