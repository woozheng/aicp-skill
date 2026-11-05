"""skill_lint — 批量校验 data/skills/**/SKILL.md 是否符合 data/skills/_STANDARD.md 第 6 节标准。

修好的版本：
- _split_examples 用状态机重写，保证 i 永远前进，不再死循环
- frontmatter 解析时 examples 字段保留原样（不 strip 引号）
- _check_one 接收 existing_routes，rglob 只跑一次
- 同步 IO 用 asyncio.to_thread 包起来，不阻塞 event loop
- execute 的 except 去掉 agent.log，直接写 payload + traceback
"""
import json
import os
import re
import asyncio
import traceback
from pathlib import Path

import core

PROJECT = Path(__file__).parent.name
PLUGIN = Path(__file__).stem
SENDER = f"applications/{PROJECT}/{PLUGIN}"

REQUIRED = ["title", "description", "category", "tags", "triggers",
            "not_triggers", "examples", "version", "updated_at"]
SECTIONS = ["何时用", "前置条件", "核心步骤", "判断规则", "踩过的坑", "验证", "参考"]
TOOLNAMES = {"edit_file", "replace_lines", "write_file", "read_file",
             "read_file_lines", "delete_lines", "apply_patch",
             "file_utils_api", "validation", "list_dir", "search",
             "code_structure", "mkdir", "file_exists", "file_stat"}
VAGUE = {"应用", "项目", "内容", "写作文", "小结", "归类", "翻译",
         "文件", "处理", "数据", "信息", "文档", "文本"}

# 插件在 plugins/mytools/skill_lint.py，上 3 级到项目根
SKILLS_ROOT = Path(__file__).resolve().parent.parent.parent / "data" / "skills"


def _split_csv(s):
    return [x.strip() for x in re.split(r"[,，、;；]", str(s or "")) if x.strip()]


def _is_cn(s):
    return bool(re.search(r"[一-鿿]", s))


def _split_examples(s):
    """状态机版：in_quote 标记引号内，保证 i 永远前进，不会死循环。"""
    s = str(s or "").strip()
    parts = []
    buf = []
    in_quote = False
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == '"':
            in_quote = not in_quote
            i += 1
            continue
        if not in_quote and c in ',，、;；':
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


def _parse_frontmatter(text):
    """解析 frontmatter，返回 (meta, body)。examples 字段保留原样不 strip 引号。"""
    meta = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm = text[3:end]
            for line in fm.splitlines():
                if ":" in line and not line.strip().startswith("#"):
                    k, v = line.split(":", 1)
                    k = k.strip().lower()
                    raw_v = v.strip()
                    if k in ("examples",):
                        meta[k] = raw_v          # 保留引号，_split_examples 自己处理
                    else:
                        meta[k] = raw_v.strip("\"'")
            body = text[end + 4:]
    return meta, body


def _list_all_routes():
    """一次遍历返回所有 skill 路由集合。os.walk(followlinks=False) 避免 symlink 循环。"""
    existing = set()
    try:
        root = SKILLS_ROOT
        if not root.is_dir():
            return existing
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            if "SKILL.md" in filenames:
                rel = Path(dirpath).relative_to(root).as_posix()
                existing.add("skill/" + rel)
    except Exception:
        pass
    return existing


def _check_one_inner(path_str, existing_routes=None):
    f = Path(path_str)
    errors, warns = [], []
    if not f.is_file():
        return {"path": path_str, "ok": False, "errors": ["文件不存在: %s" % path_str],
                "warns": [], "stats": {}}

    text = f.read_text(encoding="utf-8", errors="ignore")
    if not text.startswith("---"):
        return {"path": path_str, "ok": False, "errors": ["无 frontmatter"],
                "warns": [], "stats": {}}
    end = text.find("\n---", 3)
    if end == -1:
        return {"path": path_str, "ok": False, "errors": ["frontmatter 未闭合"],
                "warns": [], "stats": {}}

    meta, body = _parse_frontmatter(text)

    for k in REQUIRED:
        if k not in meta or not str(meta[k]).strip():
            errors.append("缺字段 %s" % k)

    posix = f.as_posix()
    if "data/skills/" in posix:
        path_cat = posix.split("data/skills/")[1].split("/")[0]
        if meta.get("category") and meta["category"] != path_cat:
            errors.append("category=%s 与路径 %s 不一致" % (meta["category"], path_cat))

    desc = meta.get("description", "")
    if len(desc) > 120:
        errors.append("description %d 字 > 120" % len(desc))
    if "「" not in desc:
        errors.append("description 无「」用户原话")
    if not re.search(r"「[^」]*[一-鿿][^」]*」", desc):
        warns.append("description 无中文「」原话")
    if not re.search(r"「[^」]*[a-zA-Z][^」]*」", desc):
        warns.append("description 无英文「」原话")

    tr = _split_csv(meta.get("triggers", ""))
    if len(tr) < 8:
        errors.append("triggers %d 个 < 8" % len(tr))
    elif len(tr) > 20:
        errors.append("triggers %d 个 > 20" % len(tr))
    cn = [t for t in tr if _is_cn(t)]
    en = [t for t in tr if not _is_cn(t) and t.strip() not in TOOLNAMES]
    total = len(cn) + len(en)
    if total:
        pct = round(len(cn) * 100 / total)
        if pct < 40 or pct > 60:
            warns.append("triggers 中英比例: cn=%d, en=%d, cn_pct=%d%% (建议各 40-60%%)"
                         % (len(cn), len(en), pct))
    for t in tr:
        if t in VAGUE:
            warns.append("triggers 含泛词「%s」：命中率低且噪声大，建议换具体词" % t)

    nt = _split_csv(meta.get("not_triggers", ""))
    if len(nt) < 2:
        errors.append("not_triggers %d 个 < 2" % len(nt))
    elif len(nt) > 8:
        errors.append("not_triggers %d 个 > 8" % len(nt))
    for w in nt:
        if w in VAGUE:
            warns.append("not_triggers 含泛词「%s」：泛词会大范围误降权，建议换具体词" % w)

    ex = _split_examples(meta.get("examples", ""))
    if len(ex) < 2:
        errors.append("examples %d 个 < 2" % len(ex))
    elif len(ex) > 4:
        warns.append("examples %d 个 > 4" % len(ex))
    ex_cn = [e for e in ex if re.search(r"[一-鿿]", e)]
    ex_en = [e for e in ex if re.search(r"[a-zA-Z]", e)]
    if len(ex_cn) < 1:
        warns.append("examples 无中文条目")
    if len(ex_en) < 1:
        warns.append("examples 无英文条目")

    def _sig(s):
        return set(re.findall(r"[一-鿿]|[a-zA-Z0-9]", s.lower()))
    for i in range(len(ex)):
        for j in range(i + 1, len(ex)):
            a, b = _sig(ex[i]), _sig(ex[j])
            if not a or not b:
                continue
            overlap = len(a & b) / min(len(a), len(b))
            if overlap >= 0.7:
                warns.append("examples 第%d与第%d条场景高度重合(%d%%)，应换成不同场景"
                             % (i + 1, j + 1, round(overlap * 100)))

    positions = []
    for sec in SECTIONS:
        idx = body.find("## %s" % sec)
        if idx == -1:
            errors.append("正文缺章节: ## %s" % sec)
        else:
            positions.append((sec, idx))
    for i in range(1, len(positions)):
        if positions[i][1] < positions[i - 1][1]:
            errors.append("章节顺序错: ## %s 应在 ## %s 之后"
                          % (positions[i][0], positions[i - 1][0]))

    m = re.search(r"\*\*不适用\*\*[：:]\s*(.+)", body)
    if m:
        for w in nt:
            if w and w not in m.group(1):
                warns.append("not_triggers 的「%s」未出现在「不适用」段" % w)

    refs = re.findall(r"skill/[a-z0-9_-]+/[a-z0-9_-]+", body)
    if refs and existing_routes is not None:
        for r in set(refs):
            if r not in existing_routes:
                errors.append("参考段路由不存在: %s" % r)

    for mt in re.finditer(r"(?<![_\w])tool\s*\(", body):
        start = mt.start()
        if start >= 5 and body[start - 5:start] == "use_t":
            continue
        warns.append("正文出现旧式伪函数 tool(...)，建议改用 use_tool(target=..., action=..., plugin_args={...})")
        break

    return {
        "path": path_str,
        "ok": len(errors) == 0,
        "errors": errors,
        "warns": warns,
        "stats": {"triggers": len(tr), "not_triggers": len(nt),
                  "examples": len(ex), "desc_len": len(desc)},
    }


def _check_one(path_str, existing_routes=None):
    """单文件失败不拖累整批。"""
    try:
        return _check_one_inner(path_str, existing_routes)
    except Exception as e:
        return {"path": path_str, "ok": False,
                "errors": ["校验异常: %s: %s" % (type(e).__name__, str(e)[:200])],
                "warns": [], "stats": {}}


def _save_report(op, report):
    op = Path(op)
    op.parent.mkdir(parents=True, exist_ok=True)
    tmp = op.with_suffix(op.suffix + ".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(op)


async def execute(envelop, agent):
    try:
        raw = envelop.payload if isinstance(envelop.payload, dict) else {}
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
        action = str(payload.get("action") or "scan").strip().lower()

        if action != "scan":
            envelop.payload = {"ok": False, "error": "未知 action: %s" % action}
            return envelop

        target = payload.get("path")
        out_path = payload.get("out")

        if target:
            targets = [Path(target)]
        else:
            targets = await asyncio.to_thread(
                lambda: sorted(SKILLS_ROOT.rglob("SKILL.md")) if SKILLS_ROOT.is_dir() else []
            )

        existing_routes = await asyncio.to_thread(_list_all_routes)
        items = await asyncio.to_thread(
            lambda: [_check_one(p.as_posix(), existing_routes) for p in targets]
        )

        passed = sum(1 for it in items if it["ok"])
        failed = len(items) - passed
        report = {
            "ok": failed == 0 and len(items) > 0,
            "total": len(items),
            "passed": passed,
            "failed": failed,
            "items": items,
        }

        if out_path:
            try:
                await asyncio.to_thread(_save_report, out_path, report)
                report["out"] = str(out_path)
            except Exception as e:
                report["out_error"] = str(e)[:200]

        envelop.payload = {"ok": True, "data": report}
        return envelop

    except Exception as e:
        envelop.payload = {
            "ok": False,
            "error": "%s: %s" % (type(e).__name__, str(e)[:300]),
            "trace": traceback.format_exc()[:1500],
        }
        return envelop
