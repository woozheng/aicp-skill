"""
cogitor — AICP 系统概览（分组 + 描述版）
========================================
无状态、实时、按需。不需要启动、不需要心跳、不需要分析。
- get_map：系统概览（分类 + 数量 + 描述）
- list_plugins：一次返回 分类 + 插件名 + 描述
- 零 token（描述从源码文件提取，有缓存）
- 毫秒级响应（首次读文件，之后读缓存）

描述来源（按优先级）：
  1. help() 里的 description 字段（只匹配 help() 之后的）
  2. @AICP_ALIGN 的 actions 列表
  3. 模块 docstring 第一行
  4. 空字符串
"""

import core
import re
from pathlib import Path


TITLE_PATTERN = re.compile(r'<title>(.*?)</title>', re.DOTALL)

# ★ 基于 cogitor.py 自己的位置，不依赖 CWD
# cogitor.py 在 plugins/builtins/agents/cogitor.py
# 往上 3 级到 plugins/
PLUGINS_ROOT = Path(__file__).parent.parent.parent

PER_GROUP_LIMIT = 15       # 每个分类最多列 15 个插件
PER_PROJECT_LIMIT = 20     # applications 最多列 20 个项目

# 描述缓存：{plugin_name: desc}
_desc_cache = {}


# ============================================================
# 前端扫描
# ============================================================

def _scan_frontend() -> dict:
    """扫描 www/ 目录，提取每个系统的标题"""
    result = {}
    www_dir = Path("www")
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
                # 清理末尾的引号残留
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

    # 截断到 50 字
    if len(desc) > 50:
        desc = desc[:48] + "…"

    _desc_cache[plugin_name] = desc
    return desc


# ============================================================
# 插件名获取
# ============================================================

async def _get_plugin_names(agent) -> list:
    """从 _registry 获取后端插件名列表"""
    try:
        result = await agent.system.call(core.Envelop(
            sender="builtins/agents/cogitor",
            receiver="os/_registry",
            payload={"action": "list"}
        ))
        if not result or not result.payload or not result.payload.get("ok"):
            return []
        apis = result.payload.get("apis", [])
        return [a.get("name", "") for a in apis if a.get("name")]
    except Exception:
        return []


# ============================================================
# 分组 + 描述
# ============================================================

def _group_with_desc(plugin_names: list) -> dict:
    """
    分组 + 描述。

    规则：
    - applications/xxx/yyy → 归到 applications，按项目分组（projects）
    - 其他（builtins/xxx、os/xxx、saver/xxx）→ 归到对应一级分类，列插件名（plugins）

    每个分类最多 PER_GROUP_LIMIT 个插件，超出折叠。
    applications 最多列 PER_PROJECT_LIMIT 个项目。
    """
    groups = {}
    for name in plugin_names:
        if not name:
            continue
        parts = name.split("/")

        # ★ 只有 applications 下的才是"项目"
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
            # builtins/tools/aicp_chat → 归到 builtins
            # os/file_utils_api → 归到 os
            category = parts[0] if parts else "_root"
            if category not in groups:
                groups[category] = {}
            if "plugins" not in groups[category]:
                groups[category]["plugins"] = []
            groups[category]["plugins"].append(name)

    # 精简 + 加描述
    result = {}
    for cat, info in sorted(groups.items()):
        if cat == "_root":
            continue

        if info.get("projects"):
            # applications 这类：列项目名 + 数量 + 描述，限制数量
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
                proj_brief[proj] = {
                    "count": len(plugins),
                    "desc": desc,
                }
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
    """生成"分组 + 数量 + 描述"的概览文本"""
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
def _tokenize(query):
    """拆词：按非中英文数字的符号拆，保留 _ 和 -"""
    if not query:
        return []
    tokens = re.split(r'[^\u4e00-\u9fffa-zA-Z0-9_-]+', query.strip())
    return [t.lower() for t in tokens if t]
def _term_search(terms, plugin_names, limit=10, mode="any"):
    """
    按词列表搜索。mode=any 任一命中 | mode=all 全部命中

    返回每条的：
      - name: 插件名
      - desc: 完整描述
      - score: 匹配分
      - matched_terms: 命中的词
      - match_detail: 每个词匹配在哪（name / desc）
      - desc_matched: 命中的词在描述里的"片段"（前后各 20 字）
    """
    if not terms:
        return []

    # 统一小写，去空白
    terms = [t.lower().strip() for t in terms if t and t.strip()]
    if not terms:
        return []

    results = []
    for name in plugin_names:
        desc = _extract_desc(name)
        name_lower = name.lower()
        desc_lower = desc.lower()

        score = 0
        matched_terms = []
        match_detail = {}
        desc_matched = []

        for term in terms:
            term_score = 0
            matched_in = []

            if term in name_lower:
                term_score += 10
                matched_in.append("name")
            if term in desc_lower:
                term_score += 5
                matched_in.append("desc")
                # ★ 提取描述里的命中片段
                idx = desc_lower.find(term)
                if idx >= 0:
                    start = max(0, idx - 20)
                    end = min(len(desc), idx + len(term) + 20)
                    snippet = desc[start:end]
                    if start > 0:
                        snippet = "…" + snippet
                    if end < len(desc):
                        snippet = snippet + "…"
                    desc_matched.append({
                        "term": term,
                        "snippet": snippet,
                    })

            if term_score > 0:
                score += term_score
                matched_terms.append(term)
                match_detail[term] = matched_in

        # 模式判断
        if mode == "all":
            if len(matched_terms) < len(terms):
                continue
        else:  # any
            if not matched_terms:
                continue

        # 加分：描述非空
        if desc:
            score += 2
        # 加分：命中的词越多越好
        score += len(matched_terms) * 3

        results.append({
            "name": name,
            "desc": desc,
            "score": score,
            "matched_terms": matched_terms,
            "match_detail": match_detail,
            "desc_matched": desc_matched,    # 描述里的命中片段
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:limit]
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

        envelop.payload = {
            "ok": True,
            "summary": "\n".join(summary_lines),
            "data": {
                "total_plugins": total_plugins,
                "total_frontend": total_frontend,
                "frontend": frontend,
            },
        }
        return envelop
    elif action == "search":
            # ★ 支持两种输入：terms（列表）或 query（字符串）
            terms = envelop.payload.get("terms")
            if not terms:
                # 兼容旧接口：query 字符串，用 _tokenize 拆
                query = envelop.payload.get("query", "").strip()
                terms = _tokenize(query)

            if not terms:
                envelop.payload = {"ok": False, "error": "缺少 terms 或 query 参数"}
                return envelop

            limit = envelop.payload.get("limit", 10)
            mode = envelop.payload.get("mode", "any")  # any | all

            plugin_names = await _get_plugin_names(agent)
            results = _term_search(terms, plugin_names, limit, mode)

            envelop.payload = {
                "ok": True,
                "terms": terms,
                "results": results,
                "total": len(results),
            }
            return envelop

    # ============================================================
    # list_plugins：一次返回分组 + 插件名 + 描述
    # ============================================================
    elif action == "list_plugins":
        plugin_names = await _get_plugin_names(agent)
        if not plugin_names:
            envelop.payload = {"ok": False, "error": "无法获取插件列表"}
            return envelop

        category = envelop.payload.get("category", "").strip()

        # ---- 带 category：返回该分类的完整列表 ----
        if category:
            matched = [
                n for n in plugin_names
                if n == category or n.startswith(category + "/")
            ]
            if not matched:
                envelop.payload = {
                    "ok": False,
                    "error": f"没有找到分类或插件: {category}",
                    "hint": "先用 list_plugins 不带参数查看所有分类",
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

        # ---- 不带 category：一次返回分组 + 插件名 + 描述 ----
        groups = _group_with_desc(plugin_names)
        envelop.payload = {
            "ok": True,
            "groups": groups,
            "total": len(plugin_names),
            "hint": "每个分类最多列 15 个，超出用 more 标记。想看完整列表用 list_plugins category=<分类>",
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
        "route": "builtins/agents/cogitor",
        "description": "系统概览 — 分组+描述+搜索",
        "input": {
            "action": "get_map | list_plugins | search",
            "category": "（list_plugins 可选）分类名",
            "terms": "（search 可选）词列表，如 [\"股票\", \"stock\"]",
            "query": "（search 可选）查询字符串，会按空格/标点拆词",
            "limit": "（search 可选）返回数量，默认 10",
            "mode": "（search 可选）any 任一命中 | all 全部命中，默认 any",
        },
        "output": {
            "ok": "是否成功",
            "summary": "系统概览文本（get_map）",
            "groups": "分组+插件名+描述（list_plugins 无参数）",
            "plugins": "完整插件列表（list_plugins 指定 category）",
            "results": "搜索结果（search）",
        },
        "examples": [
            'search terms=["股票","stock"] → {"results": [{"name": "...", "desc": "...", "match_detail": {...}}]}',
            'search query="股票 stock" → 同上（query 会被拆成 ["股票","stock"]）',
        ],
    }
