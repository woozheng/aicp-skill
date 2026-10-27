#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描 data/skills/**/SKILL.md，生成技能索引 Markdown。

用法（项目根目录执行）:
    python data/skills/aicp/skill-index/scripts/build_skill_index.py

输出:
    data/workspace/skill_index.md
退出码:
    0 成功；2 找不到 skills 根目录或一条都没扫到（视为失败，不允许静默产出空索引）
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

FM_RE = re.compile(r"^---(.{0,4000}?)\s*---(?:\r?\n|$)", re.S)
H1_RE = re.compile(r"(?m)^#\s+(.+?)\s*$")
SCRIPT_PATH = "data/skills/aicp/skill-index/scripts/build_skill_index.py"


# ---------------------------------------------------------------- 路径探测
def find_skills_root():
    """cwd 优先，其次脚本自身位置向上回溯；两套路径源必须同源，否则会静默扫空。"""
    here = Path(__file__).resolve()
    cands = [Path.cwd() / "data" / "skills", Path.cwd() / "skills"]
    for p in here.parents:
        cands.append(p / "skills")
    seen, ordered = set(), []
    for c in cands:
        key = str(c)
        if key not in seen:
            seen.add(key)
            ordered.append(c)
    for c in ordered:
        try:
            if c.is_dir() and any(c.rglob("SKILL.md")):
                return c.resolve()
        except OSError:
            continue
    for c in ordered:
        try:
            if c.is_dir():
                return c.resolve()
        except OSError:
            continue
    return None


# ---------------------------------------------------------------- frontmatter
def regex_frontmatter(raw):
    """PyYAML 不可用/格式怪时的兜底：块状（每行一个键）与单行内联两种形态。"""
    out = {}
    text = raw.strip()
    if "\n" in text:
        for m in re.finditer(r"(?m)^([A-Za-z_][\w-]*)\s*:\s*(.*)$", text):
            k, v = m.group(1), m.group(2).strip()
            if v and k not in out:
                out[k] = v
    else:
        for part in re.split(r"\s(?=[A-Za-z_][\w-]*\s*:)", text):
            if ":" in part:
                k, v = part.split(":", 1)
                k, v = k.strip(), v.strip()
                if k and v and k not in out:
                    out[k] = v
    return out


def load_frontmatter(text):
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return {}, text
    m = FM_RE.match(stripped)
    if not m:
        return {}, text
    raw = m.group(1)
    body = stripped[m.end():]
    data = {}
    try:
        import yaml  # type: ignore

        parsed = yaml.safe_load(raw)
        if isinstance(parsed, dict):
            data = {str(k): ("" if v is None else v) for k, v in parsed.items()}
        else:
            data = regex_frontmatter(raw)
    except Exception:
        data = regex_frontmatter(raw)
    return data, body


def scalar(v):
    if isinstance(v, (list, tuple)):
        return "、".join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


# ---------------------------------------------------------------- 用途抽取
def first_prose(body):
    """正文里第一段连续散文：跳过代码围栏、表格行、引用行、列表项。"""
    buf, in_fence = [], False
    for ln in body.splitlines():
        s = ln.strip()
        if s.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not s:
            if buf:
                break
            continue
        if s.startswith("#") or s.startswith("|") or s.startswith(">"):
            continue
        if re.match(r"^([-*+]|\d+\.)\s+", s):
            continue
        buf.append(s)
        if len(buf) >= 2:
            break
    return re.sub(r"\s+", " ", " ".join(buf)).strip()


def truncate(text, n=160):
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def cell(text):
    """表格单元格值不能含竖线，否则表格被撑破。"""
    return str(text).replace("|", "/").replace("\n", " ")


# ---------------------------------------------------------------- 主流程
def collect(root):
    items = []
    for f in sorted(root.rglob("SKILL.md")):
        raw = f.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        fm, body = load_frontmatter(text)
        m = H1_RE.search(body)
        h1 = m.group(1).strip() if m else ""
        title = scalar(fm.get("title") or "").strip()
        fm_name = scalar(fm.get("name") or "").strip()
        if not title:
            title = fm_name or h1 or f.parent.name
        desc = scalar(fm.get("description") or fm.get("summary") or "").strip()
        if not desc:
            desc = first_prose(body)
        desc = truncate(desc, 160)
        meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
        version = scalar(meta.get("version") or fm.get("version") or "").strip()
        tags = scalar(fm.get("tags") or fm.get("keywords") or "").strip()
        rel = f.relative_to(root.parent.parent).as_posix()
        items.append(
            {
                "skill_id": f.parent.name,
                "category": f.parent.parent.name,
                "title": title,
                "fm_name": fm_name,
                "h1": h1,
                "purpose": desc,
                "size_bytes": len(raw),
                "line_count": len(text.splitlines()),
                "rel_path": rel,
                "version": version,
                "tags": tags,
            }
        )
    return items


APPENDIX = """
---

## 附：生成链实录（人工首跑 → 脚本固化）

### 工具调用序列

| 步骤 | 目标 | 参数 | 结果摘要 | 错误与修复 |
|---|---|---|---|---|
| 1 | builtins/tools/file_utils_api / search | path=data/skills, pattern=**/SKILL.md | 命中 7 份 SKILL.md，返回绝对路径与字节大小（total=7, scanned=7, truncated=false） | 无 |
| 2 | builtins/tools/code_runner | 批量 rglob + 自写 parse_fm() | 拿到 skill_id / 分类 / 字节数 / 行数，但 fm_keys 全为空 | 解析器 bug：value 非空分支把 key 置为 None，键值永不落库 |
| 3 | builtins/tools/code_runner | 取 H1 + 首个非空正文行 | 标题正确，purpose 取到噪声（coder-runner 取到分隔符、save-blocks-app 取到表格行） | 启发式太宽松：未跳过表格 / 引用 / 代码围栏 |
| 4 | builtins/tools/code_runner | 取 frontmatter 原文 + 过滤后首段 | 7 条的 name/title/description/tags/version 全部抽取成功 | 已修复第 2、3 步两处缺陷 |
| 5 | builtins/tools/file_utils_api / write_file | path=data/workspace/skill_index.md | ok=true，6,028 字节，Markdown 校验 ok=true | 无 |
| 6 | builtins/tools/code_runner | 读回索引文件校验落盘 | exists=true，7 个条目标题齐全 | 无 |
| 7 | builtins/tools/file_utils_api / append_file | 追加工具链实录段 | 写入成功但校验失败：Markdown 代码块未闭合 | 表格里写了裸围栏符，被校验器当成开栅栏 |
| 8 | builtins/tools/file_utils_api / read_file_lines | 125-175 行 | 定位到出问题的那一行 | 无 |
| 9 | builtins/tools/file_utils_api / edit_file | 把裸围栏符换成文字描述 | replaced=true，校验 ok=true | 无 |
| 10 | builtins/tools/code_runner | 全文体检 | 7 条目 / 7 个 skill_id / 围栏配平 / 四个必备段齐全 | 无 |
| 11 | builtins/tools/code_runner | await subprocess.run([sys.executable, script]) | 失败：TypeError: object CompletedProcess can't be used in 'await' expression | 沙箱只把 run / run_async 包装成异步，subprocess.run 本身未被改写，仍是同步返回 |
| 12 | builtins/tools/file_utils_api / read_file_lines | plugins/builtins/tools/code_runner.py 第 390-440 行 | 确认 _safe_run 内部走 asyncio.to_thread(subprocess.run)，而非替换 subprocess.run | 无（读源码定位根因，避免第 2 次同错） |
| 13 | builtins/tools/file_utils_api / write_file + builtins/tools/code_runner | 脚本定稿到 data/skills/aicp/skill-index/scripts/build_skill_index.py，用 await asyncio.to_thread(subprocess.run, ...) 重跑，并清理旧副本 | returncode=0，复现同样 7 条目，索引可重复生成 | 已修复第 11 步的 await 误用 |

### 踩坑与修复清单

1. **frontmatter 解析器丢键**：自写 YAML 子集解析时，value 非空的分支把 key 置空，导致 fm_keys 全空、description 全空。修复：优先用 PyYAML，失败再退回正则，并额外覆盖 coder-runner 的单行内联写法。
2. **一句话用途抽取噪声**：正文首个非空行可能是分隔符、Markdown 表格行或引用块。修复：跳过代码围栏、竖线开头、尖括号开头、列表项，取第一段连续散文；description 存在时优先用它。
3. **frontmatter 格式不统一**：aicp-mcp-bridge 用 name，context-notes 同时有 name/title/description，coder-runner 把分隔符与键值写在同一行。任何单一套法都会漏字段，必须多来源回退。
4. **写入校验报错**：在 Markdown 表格里直接写裸围栏符会被校验器判为未闭合代码块。修复：正文描述改用文字，不出现裸围栏符。
5. **路径双源风险**：file_utils_api 返回 E:/aicp-skills/... 绝对路径，code_runner 依赖进程 cwd 的相对路径。两者必须同源，否则会静默扫到空目录。脚本用「cwd 优先 + 脚本自身 parents 回退」双探测解决，并对「扫到 0 条」直接判定失败。
6. **沙箱里 await subprocess.run**：沙箱只提供异步的 run / run_async 助手，subprocess.run 仍是同步函数，await 它会抛 TypeError。修复：改用 await asyncio.to_thread(subprocess.run, ...) 包装，或直接用沙箱的 run_async 助手。

### 最脆弱的一环

**自动摘要层，不是文件遍历层。** 遍历与读写由 file_utils_api / code_runner 稳定完成（7/7 全覆盖、零漏读），真正脆的是从异构 SKILL.md 提炼标题与一句话用途：frontmatter 键名不统一、分隔符写法不同、正文首行可能是表格或引用。任何一条启发式退化，索引仍会「生成成功」但内容静默失真——失败不会显形，只会产出看起来合理的错数据。其次脆弱的是路径双源（绝对路径 vs 进程 cwd）与沙箱执行语义（await 用错 API 会直接抛错，属于会显形的失败）。

### 复用

本文件由脚本生成，可随时重跑：

    python data/skills/aicp/skill-index/scripts/build_skill_index.py

重跑会覆盖本文件（含本附录）。若目录新增或修改了 SKILL.md，重跑即可保持一致。
"""


def build_markdown(root, items):
    total_bytes = sum(i["size_bytes"] for i in items)
    total_lines = sum(i["line_count"] for i in items)
    cats = {}
    for i in items:
        cats[i["category"]] = cats.get(i["category"], 0) + 1
    cat_desc = "、".join("`%s` × %d" % (c, n) for c, n in sorted(cats.items()))

    out = []
    out.append("# data/skills 技能索引")
    out.append("")
    out.append("> 扫描根目录：`%s`" % root.as_posix())
    out.append("> 匹配规则：`**/SKILL.md`")
    out.append("> 条目数：**%d** ｜ 总大小：**%s 字节** ｜ 总行数：**%s 行**" % (len(items), format(total_bytes, ","), format(total_lines, ",")))
    out.append("> 分类分布：%s" % cat_desc)
    out.append("> 生成方式：`python %s`（生成于 %s）" % (SCRIPT_PATH, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    out.append("")
    out.append("---")
    out.append("")

    for n, i in enumerate(items, 1):
        out.append("## %d. %s" % (n, i["skill_id"]))
        out.append("")
        out.append("| 字段 | 值 |")
        out.append("|---|---|")
        out.append("| skill_id | `%s` |" % cell(i["skill_id"]))
        out.append("| 分类 | `%s` |" % cell(i["category"]))
        out.append("| 标题 | %s |" % cell(i["title"]))
        out.append("| 一句话用途 | %s |" % cell(i["purpose"]))
        out.append("| 文件大小 / 行数 | %s 字节 / %s 行 |" % (format(i["size_bytes"], ","), i["line_count"]))
        out.append("| 路径 | `%s` |" % cell(i["rel_path"]))
        extra = []
        if i["fm_name"] and i["fm_name"] != i["skill_id"]:
            extra.append("frontmatter name: `%s`" % cell(i["fm_name"]))
        if i["h1"] and i["h1"] != i["title"]:
            extra.append("正文 H1: %s" % cell(i["h1"]))
        if i["version"]:
            extra.append("版本 `%s`" % cell(i["version"]))
        if i["tags"]:
            extra.append("tags: %s" % cell(i["tags"]))
        if extra:
            out.append("| 其他 | %s |" % cell("；".join(extra)))
        out.append("")

    out.append("---")
    out.append("")
    out.append("## 汇总表")
    out.append("")
    out.append("| # | skill_id | 分类 | 标题 | 字节 | 行数 |")
    out.append("|---|---|---|---|---|---|")
    for n, i in enumerate(items, 1):
        out.append("| %d | %s | %s | %s | %s | %s |" % (n, cell(i["skill_id"]), cell(i["category"]), cell(i["title"]), format(i["size_bytes"], ","), i["line_count"]))
    out.append("| | **合计** | | **%d 条** | **%s** | **%s** |" % (len(items), format(total_bytes, ","), format(total_lines, ",")))
    out.append("")
    out.append(APPENDIX.strip())
    out.append("")
    return "\n".join(out)


def main():
    root = find_skills_root()
    if root is None:
        print(json.dumps({"ok": False, "error": "skills root not found"}, ensure_ascii=False))
        return 2
    items = collect(root)
    if not items:
        print(json.dumps({"ok": False, "error": "0 SKILL.md found under %s" % root.as_posix()}, ensure_ascii=False))
        return 2
    md = build_markdown(root, items)
    out_path = root.parent.parent / "data" / "workspace" / "skill_index.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "root": root.as_posix(),
        "out": out_path.as_posix(),
        "count": len(items),
        "bytes": out_path.stat().st_size,
        "skill_ids": [i["skill_id"] for i in items],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())