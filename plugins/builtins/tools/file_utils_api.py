import asyncio
import os
import sys
import re
from pathlib import Path
from datetime import datetime


# read_file 超过此大小时，不返回尾部预览，改返回 code_structure（结构地图）
# 设为 0 表示禁用（永远返回尾部预览）
READ_FILE_STRUCTURE_THRESHOLD = 50 * 1024
# ============================================================
# 基准目录 —— 相对路径统一基于此解析（可用环境变量覆盖）
# ============================================================

WORKSPACE_ROOT = Path(
    os.environ.get("AICP_WORKSPACE_ROOT", os.getcwd())
).resolve()


def _rel_path(p) -> str:
    """返回相对 WORKSPACE_ROOT 的路径（正斜杠），跨环境/跨机器可用。

    不在 WORKSPACE_ROOT 内时（如绝对路径访问外部目录）回落到绝对路径。
    """
    try:
        rel = Path(p).resolve().relative_to(WORKSPACE_ROOT)
    except (ValueError, OSError):
        return str(p).replace("\\", "/")
    text = rel.as_posix()
    return text if text else "."


def _path_kind(p) -> str:
    """标注路径展示形态：仓库内 relative / 仓库外 absolute。

    与 _rel_path 判定一致——仓库内输出相对路径，仓库外输出绝对路径。
    """
    try:
        Path(p).resolve().relative_to(WORKSPACE_ROOT)
    except (ValueError, OSError):
        return "absolute"
    return "relative"

# ============================================================
# 黑名单配置 —— 只禁止系统目录，其他全放行
# ============================================================

FORBIDDEN_PREFIXES = (
    # Windows 系统目录
    "C:/Windows",
    "C:/Program Files",
    "C:/Program Files (x86)",
    "C:/System32",
    "C:/syswow64",
    "C:/boot",
    "C:/efi",
    "C:/PerfLogs",
    "C:/ProgramData",
    "C:/Recovery",
    "C:/System Volume Information",
    "C:/pagefile.sys",
    "C:/hiberfil.sys",
    # Linux 系统目录
    "/etc",
    "/proc",
    "/sys",
    "/boot",
    "/dev",
    "/root",
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/opt",
    "/var",
    "/tmp",
    "/run",
    "/mnt",
    "/media",
    "/srv",
    "/lost+found",
    "/.snapshots",
    # 关键文件（已在 /etc /root 前缀内，保留显式声明便于审计）
    "/etc/passwd",
    "/etc/shadow",
    "/etc/sudoers",
    "/etc/hosts",
    "/etc/fstab",
    "/etc/crontab",
    "/etc/ssh",
    "/etc/ssl",
    "/root/.ssh",
    "/root/.bash_history",
    "/root/.bashrc",
)

# 预解析黑名单为 Path 列表（一次解析，避免每次操作重复 resolve）
_FORBIDDEN_PATHS = []
for _p in FORBIDDEN_PREFIXES:
    try:
        _FORBIDDEN_PATHS.append(Path(_p).resolve())
    except (OSError, ValueError):
        # 当前平台不存在的路径（如 Linux 下的 C:/）跳过
        pass

MAX_INLINE_SIZE = 100 * 1024  # 100 KiB

# 内容搜索时单文件大小上限（超过跳过，防 OOM）
SEARCH_MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MiB

# 结构地图输出上限：大文件的结构图不应自己膨胀成几万字符
STRUCTURE_MAX_FUNCTIONS = 120
STRUCTURE_MAX_CLASSES = 80
STRUCTURE_MAX_INTERFACES = 40
STRUCTURE_MAX_TYPES = 40
STRUCTURE_MAX_ELEMENTS = 150
STRUCTURE_MAX_HANDLERS = 100


# ============================================================
# 路径解析 —— 不限制项目根目录，只拦黑名单
# ============================================================

def _resolve_path(path_str: str) -> Path:
    """只检查黑名单，允许任何非系统目录。

    - 绝对路径：直接 resolve 后检查
    - 相对路径：基于 WORKSPACE_ROOT 解析
    - 黑名单检查使用 resolve 后的路径 + Path.is_relative_to，
      防止 `..`、符号链接、大小写等绕过。
    """
    if not isinstance(path_str, str) or path_str == "":
        raise ValueError("path 不能为空")

    # 统一分隔符（Path 在 Windows 也能处理 /）
    normalized = path_str.replace("\\", "/")

    # 以 / 开头的"伪绝对"相对写法：/data → data/（相对 WORKSPACE_ROOT）
    # 注意：Windows 下 "C:/..." 是绝对路径，不能走这个分支
    is_windows_abs = bool(re.match(r"^[A-Za-z]:/", normalized))
    if normalized.startswith("/") and not is_windows_abs:
        normalized = normalized[1:]

    raw = Path(normalized)
    if not raw.is_absolute():
        raw = WORKSPACE_ROOT / raw

    try:
        resolved = raw.resolve()
    except OSError as e:
        raise ValueError(f"路径解析失败: {path_str} ({e})")

    # 黑名单检查（resolve 之后）
    for forbidden in _FORBIDDEN_PATHS:
        try:
            if resolved == forbidden or resolved.is_relative_to(forbidden):
                raise ValueError(f"禁止访问系统目录: {path_str}")
        except ValueError:
            # is_relative_to 在不相关路径上会抛 ValueError，属正常
            continue

    return resolved


# ============================================================
# 安全写盘辅助
# ============================================================

def _write_with_backup(p: Path, new_text: str, old_text: str) -> None:
    """直接写盘，不生成 .bak 备份。

    保留 old_text 参数仅为兼容现有调用（_write_file_sync / _edit_file_sync /
    _delete_lines_sync 都传了），函数签名不变，调用方无需改。
    """
    p.write_text(new_text, encoding="utf-8")


# ============================================================
# 写后兜底校验 —— 按扩展名做语法/结构检查（纯静态，不执行）
# ============================================================

VALIDATE_MAX_BYTES = 2 * 1024 * 1024  # 2MiB，超过跳过校验

PY_EXTS = {".py", ".pyw"}
JSON_EXTS = {".json", ".jsonc", ".json5"}
HTML_EXTS = {".html", ".htm", ".xhtml"}
HTML_LIKE_EXTS = {".vue", ".svelte"}
JS_EXTS = {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"}
CSS_EXTS = {".css", ".scss", ".sass", ".less"}
JAVA_EXTS = {".java"}
KT_EXTS = {".kt", ".kts"}
C_EXTS = {".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".m", ".mm"}
GO_EXTS = {".go"}
RS_EXTS = {".rs"}
YAML_EXTS = {".yaml", ".yml"}
XML_EXTS = {".xml", ".xsd", ".xsl", ".svg"}
SQL_EXTS = {".sql"}
SH_EXTS = {".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd"}
MD_EXTS = {".md", ".markdown"}
TOML_EXTS = {".toml"}
INI_EXTS = {".ini", ".cfg", ".conf", ".properties"}


def _validate_written_file(path: Path) -> dict:
    """写完后对代码/标记文件做静态校验，返回 {checked, ok, error, kind, skipped?}"""
    import json as _json

    ext = path.suffix.lower()
    result = {"checked": False, "ok": True, "error": None, "kind": ext}

    try:
        size = path.stat().st_size
    except OSError as e:
        return {"checked": False, "ok": False, "error": f"stat 失败: {e}", "kind": ext}

    if size == 0:
        return {"checked": True, "ok": False, "error": "文件为空（0 字节）", "kind": ext}

    if size > VALIDATE_MAX_BYTES:
        return {
            "checked": False,
            "ok": True,
            "error": None,
            "kind": ext,
            "skipped": f"文件 {size} 字节超过校验上限，跳过",
        }

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"checked": False, "ok": False, "error": f"读取失败: {e}", "kind": ext}

    if ext in PY_EXTS:
        import ast
        try:
            ast.parse(text)
        except SyntaxError as e:
            return {
                "checked": True,
                "ok": False,
                "error": f"Python 语法错误：第 {e.lineno} 行 {e.msg}",
                "kind": ext,
            }
        return {"checked": True, "ok": True, "error": None, "kind": ext}

    if ext in JSON_EXTS:
        candidate = text
        if ext in (".jsonc", ".json5"):
            candidate = _strip_json_comments(text)
            candidate = re.sub(r",(\s*[}\]])", r"\1", candidate)
        try:
            _json.loads(candidate)
        except Exception as e:
            return {"checked": True, "ok": False, "error": f"JSON 解析失败: {e}", "kind": ext}
        return {"checked": True, "ok": True, "error": None, "kind": ext}

    if ext in HTML_EXTS or ext in HTML_LIKE_EXTS:
        err = _check_html(text, strict=(ext in HTML_EXTS))
        return {"checked": True, "ok": err is None, "error": err, "kind": ext}

    if ext in JS_EXTS:
        err = _check_brackets(text, allow_regex=True)
        return {"checked": True, "ok": err is None, "error": err, "kind": ext}

    if ext in CSS_EXTS:
        err = _check_css(text)
        return {"checked": True, "ok": err is None, "error": err, "kind": ext}

    if ext in JAVA_EXTS or ext in KT_EXTS or ext in C_EXTS or ext in GO_EXTS or ext in RS_EXTS:
        err = _check_c_like(text, ext)
        return {"checked": True, "ok": err is None, "error": err, "kind": ext}

    if ext in YAML_EXTS:
        err = _check_yaml(text)
        return {"checked": True, "ok": err is None, "error": err, "kind": ext}

    if ext in XML_EXTS:
        err = _check_xml(text)
        return {"checked": True, "ok": err is None, "error": err, "kind": ext}

    if ext in SQL_EXTS:
        err = _check_sql(text)
        return {"checked": True, "ok": err is None, "error": err, "kind": ext}

    if ext in SH_EXTS:
        err = _check_shell(text, ext)
        return {"checked": True, "ok": err is None, "error": err, "kind": ext}

    if ext in MD_EXTS:
        err = _check_markdown(text)
        return {"checked": True, "ok": err is None, "error": err, "kind": ext}

    if ext in TOML_EXTS:
        err = _check_toml(text)
        return {"checked": True, "ok": err is None, "error": err, "kind": ext}

    if ext in INI_EXTS:
        err = _check_ini(text)
        return {"checked": True, "ok": err is None, "error": err, "kind": ext}

    return result


# ---------------- 各类校验实现 ----------------

def _strip_json_comments(text: str) -> str:
    out = []
    i, n = 0, len(text)
    in_str = None
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(nxt)
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in ('"', "'"):
            in_str = c
            out.append(c)
            i += 1
            continue
        if c == "/" and nxt == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and nxt == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _check_brackets(text: str, allow_regex: bool = False) -> str | None:
    pairs = {"{": "}", "(": ")", "[": "]"}
    closers = {v: k for k, v in pairs.items()}
    stack = []
    i, n = 0, len(text)
    in_str = None
    in_line_comment = False
    in_block_comment = False
    in_regex = False

    regex_prev_chars = set("=(,[{!&|?:;")
    regex_prev_words = {"return", "typeof", "instanceof", "in", "of",
                        "case", "delete", "void", "throw", "new"}

    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if in_line_comment:
            if c == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            if c == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if in_regex:
            if c == "\\":
                i += 2
                continue
            if c == "/":
                in_regex = False
            i += 1
            continue

        if c == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if c == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue

        if allow_regex and c == "/":
            j = i - 1
            while j >= 0 and text[j] in " \t\n\r":
                j -= 1
            prev_ch = text[j] if j >= 0 else ""

            k = j
            while k >= 0 and (text[k].isalnum() or text[k] == "_"):
                k -= 1
            prev_word = text[k + 1:j + 1] if j >= 0 else ""

            is_regex_start = (
                prev_ch == ""
                or prev_ch in regex_prev_chars
                or prev_word in regex_prev_words
            )

            if is_regex_start:
                m = i + 1
                found_end = False
                while m < n and text[m] != "\n":
                    if text[m] == "\\":
                        m += 2
                        continue
                    if text[m] == "/":
                        found_end = True
                        break
                    m += 1
                if found_end:
                    in_regex = True
                    i += 1
                    continue

        if c in ("'", '"', "`"):
            in_str = c
            i += 1
            continue
        if c in pairs:
            stack.append((c, i))
        elif c in closers:
            if not stack or stack[-1][0] != closers[c]:
                pos = _line_of(text, i)
                top = stack[-1][0] if stack else "空"
                return f"括号不匹配（第 {pos} 行）：遇到 '{c}' 但栈顶是 '{top}'"
            stack.pop()
        i += 1

    if in_str:
        return f"字符串未闭合（引号 {in_str}）"
    if stack:
        ch, pos = stack[-1]
        line = _line_of(text, pos)
        return f"括号未闭合（第 {line} 行开始）：{len(stack)} 个未匹配，栈顶 '{ch}'"
    return None


def _line_of(text: str, idx: int) -> int:
    return text[:idx].count("\n") + 1


def _check_html(text: str, strict: bool = True) -> str | None:
    for m in re.finditer(r"<script[^>]*>(.*?)</script>", text, re.DOTALL | re.IGNORECASE):
        js = m.group(1)
        if js.strip():
            err = _check_brackets(js, allow_regex=True)
            if err:
                return f"<script> 内 JS {err}"

    for m in re.finditer(r"<style[^>]*>(.*?)</style>", text, re.DOTALL | re.IGNORECASE):
        css = m.group(1)
        if css.strip():
            err = _check_css(css)
            if err:
                return f"<style> 内 CSS {err}"

    for tag in ("script", "style"):
        open_n = len(re.findall(rf"<{tag}\b", text, re.IGNORECASE))
        close_n = len(re.findall(rf"</{tag}\s*>", text, re.IGNORECASE))
        if open_n != close_n:
            return f"<{tag}> 标签不配对：{open_n} 个开、{close_n} 个闭"

    if not strict:
        return None

    lower = text.lower()
    if "<html" in lower and "</html>" not in lower:
        return "缺少 </html> 闭合标签"
    if "<body" in lower and "</body>" not in lower:
        return "缺少 </body> 闭合标签"

    div_open = len(re.findall(r"<div\b(?![^>]*/>)", text, re.IGNORECASE))
    div_close = len(re.findall(r"</div\s*>", text, re.IGNORECASE))
    if div_open != div_close:
        return f"<div> 标签不配对：{div_open} 个开、{div_close} 个闭"

    return None


def _check_css(text: str) -> str | None:
    err = _check_brackets(text)
    if err:
        return f"CSS {err}"
    return None


def _check_c_like(text: str, ext: str) -> str | None:
    err = _check_brackets(text)
    if err:
        lang = {
            ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin",
            ".c": "C", ".h": "C", ".cpp": "C++", ".cc": "C++",
            ".cxx": "C++", ".hpp": "C++", ".hh": "C++", ".hxx": "C++",
            ".m": "Objective-C", ".mm": "Objective-C++",
            ".go": "Go", ".rs": "Rust",
        }.get(ext, "代码")
        return f"{lang} {err}"
    return None


def _check_yaml(text: str) -> str | None:
    lines = text.split("\n")
    prev_indent = 0
    prev_was_content = False

    for idx, line in enumerate(lines, 1):
        if line.startswith("\t"):
            return f"YAML 第 {idx} 行使用了 Tab 缩进（YAML 只允许空格）"

        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped in ("---", "..."):
            continue

        indent = len(line) - len(line.lstrip(" "))

        if prev_was_content and indent > prev_indent:
            delta = indent - prev_indent
            if delta % 2 != 0:
                return (
                    f"YAML 第 {idx} 行缩进异常：从 {prev_indent} 增至 {indent}"
                    f"（差 {delta}，疑似奇数缩进）"
                )

        prev_indent = indent
        prev_was_content = True

    return None


def _check_xml(text: str) -> str | None:
    text_clean = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text_clean = re.sub(r"<!\[CDATA\[.*?\]\]>", "", text_clean, flags=re.DOTALL)
    text_clean = re.sub(r"<\?.*?\?>", "", text_clean, flags=re.DOTALL)

    tags = re.findall(r"<(/?)([a-zA-Z_][\w:.-]*)([^>]*?)(/?)>", text_clean)
    stack = []
    for close, name, attrs, self_close in tags:
        if self_close:
            continue
        if close:
            if not stack:
                return f"XML 标签不匹配：多余的 </{name}>"
            if stack[-1] != name:
                return f"XML 标签不匹配：</{name}> 对应的是 <{stack[-1]}>"
            stack.pop()
        else:
            stack.append(name)
    if stack:
        return f"XML 标签未闭合：{stack[:5]}"
    return None


def _check_sql(text: str) -> str | None:
    err = _check_brackets(text)
    if err:
        return f"SQL {err}"

    i, n = 0, len(text)
    in_str = False
    in_line_comment = False
    in_block_comment = False
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_line_comment:
            if c == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            if c == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_str:
            if c == "'" and nxt == "'":
                i += 2
                continue
            if c == "'":
                in_str = False
            i += 1
            continue
        if c == "-" and nxt == "-":
            in_line_comment = True
            i += 2
            continue
        if c == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if c == "'":
            in_str = True
        i += 1

    if in_str:
        return "SQL 单引号未闭合"
    return None


def _check_shell(text: str, ext: str) -> str | None:
    err = _check_brackets(text)
    if err:
        return f"Shell {err}"

    i, n = 0, len(text)
    in_single = False
    in_double = False
    while i < n:
        c = text[i]
        if in_single:
            if c == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_double = False
            i += 1
            continue
        if c == "\\":
            i += 2
            continue
        if c == "'":
            in_single = True
            i += 1
            continue
        if c == '"':
            in_double = True
            i += 1
            continue
        if c == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        i += 1

    if in_single:
        return "Shell 单引号未闭合"
    if in_double:
        return "Shell 双引号未闭合"
    return None


def _check_markdown(text: str) -> str | None:
    fence_count = text.count("```")
    if fence_count % 2 != 0:
        return f"Markdown 代码块 ``` 未闭合（共 {fence_count} 个）"
    return None


def _check_toml(text: str) -> str | None:
    lines = text.split("\n")
    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("["):
            if not stripped.endswith("]"):
                return f"TOML 第 {idx} 行段头未闭合: {stripped[:50]}"
        elif "=" not in stripped:
            return f"TOML 第 {idx} 行缺少 =: {stripped[:50]}"
    return None


def _check_ini(text: str) -> str | None:
    lines = text.split("\n")
    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if stripped.startswith("["):
            if not stripped.endswith("]"):
                return f"INI 第 {idx} 行段头未闭合: {stripped[:50]}"
        elif "=" not in stripped and ":" not in stripped:
            return f"INI 第 {idx} 行缺少 = 或 :: {stripped[:50]}"
    return None


# ============================================================
# 文件操作函数
# ============================================================

def _read_file_sync(path_str):
    p = _resolve_path(path_str)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在: {path_str}")
    if not p.is_file():
        raise IsADirectoryError(f"路径不是文件: {path_str}")

    file_size = p.stat().st_size

    if READ_FILE_STRUCTURE_THRESHOLD > 0 and file_size > READ_FILE_STRUCTURE_THRESHOLD:
        try:
            structure = _parse_code_structure_sync(path_str)
        except Exception as e:
            structure = {
                "error": f"结构解析失败: {e}",
                "hint": "用 read_file_lines(path, start_line, end_line) 读指定范围",
            }
        return {
            "structure": structure,
            "size": file_size,
            "size_kb": round(file_size / 1024, 1),
            "truncated": True,
            "mode": "structure",
            "hint": (
                f"文件超过 {READ_FILE_STRUCTURE_THRESHOLD // 1024}KiB"
                f"（{round(file_size/1024,1)}KiB），已自动返回 code_structure 结构地图，"
                f"不返回全文。用 read_file_lines(path, start_line, end_line) 读关键定义。或者自行用code_runner写代码读取组合片段"
                f"如需强制读尾部预览，把 READ_FILE_STRUCTURE_THRESHOLD 设为 0 或调大阈值。"
            ),
        }

    if file_size <= MAX_INLINE_SIZE:
        content = p.read_text(encoding="utf-8")
        return {
            "full_content": content,
            "size": file_size,
            "size_kb": round(file_size / 1024, 1),
            "truncated": False,
        }

    read_size = min(file_size, MAX_INLINE_SIZE + 1024)
    with open(p, "rb") as f:
        f.seek(file_size - read_size)
        tail_bytes = f.read()

    try:
        tail_text = tail_bytes.decode("utf-8")
    except UnicodeDecodeError:
        first_nl = tail_bytes.find(b"\n")
        if first_nl == -1:
            tail_text = tail_bytes.decode("utf-8", errors="ignore")
        else:
            tail_text = tail_bytes[first_nl + 1:].decode("utf-8", errors="ignore")

    lines = tail_text.splitlines(keepends=True)

    preview_lines = []
    preview_size = 0
    for line in reversed(lines):
        line_size = len(line.encode("utf-8"))
        if preview_size + line_size > MAX_INLINE_SIZE:
            break
        preview_lines.insert(0, line)
        preview_size += line_size

    total_lines = 0
    with open(p, "rb") as f:
        for _ in f:
            total_lines += 1

    start_line = total_lines - len(preview_lines) + 1
    preview_text = "".join(preview_lines)
    preview_bytes = len(preview_text.encode("utf-8"))

    return {
        "preview": preview_text,
        "size": file_size,
        "size_kb": round(file_size / 1024, 1),
        "total_lines": total_lines,
        "preview_start_line": start_line,
        "preview_lines": len(preview_lines),
        "preview_bytes": preview_bytes,
        "preview_kb": round(preview_bytes / 1024, 1),
        "truncated": True,
        "hint": (
            f"文件超过 100KiB（{round(file_size/1024,1)}KiB），只返回最后 "
            f"{preview_bytes/1024:.1f}KiB 内容（{len(preview_lines)} 行）。"
            f"如需读取指定范围，请使用 read_file_lines（path, start_line, end_line）"
        ),
    }


def _read_file_lines_sync(path_str, start_line=None, end_line=None, with_line_num=False):
    p = _resolve_path(path_str)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在: {path_str}")
    if not p.is_file():
        raise IsADirectoryError(f"路径不是文件: {path_str}")

    lines = p.read_text(encoding="utf-8").splitlines()
    total_lines = len(lines)

    start = start_line if start_line is not None else 1
    end = end_line if end_line is not None else total_lines

    if start < 1:
        raise ValueError(f"start_line 必须 >= 1，收到 {start}")
    if end < 1:
        raise ValueError(f"end_line 必须 >= 1，收到 {end}")
    if start > total_lines:
        raise ValueError(
            f"start_line {start} 超过文件总行数 {total_lines}"
        )
    if start > end:
        raise ValueError(
            f"起始行 {start} 大于结束行 {end}（文件总行数: {total_lines}）"
        )

    if end > total_lines:
        end = total_lines

    selected = lines[start - 1:end]

    if with_line_num:
        result = [
            {"line_num": i, "content": line}
            for i, line in enumerate(selected, start=start)
        ]
    else:
        result = selected

    return {
        "lines": result,
        "total_lines": total_lines,
        "start": start,
        "end": end,
        "returned": len(selected),
        "with_line_num": with_line_num,
        "remaining_after": max(0, total_lines - end),
    }


def _write_file_sync(path_str, content):
    p = _resolve_path(path_str)
    p.parent.mkdir(parents=True, exist_ok=True)
    _write_with_backup(p, content, p.read_text(encoding="utf-8") if p.exists() else "")
    return p


def _append_file_sync(path_str, content):
    p = _resolve_path(path_str)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(content)
    return p


def _edit_file_sync(path_str, find, replace):
    p = _resolve_path(path_str)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在: {path_str}")
    if not p.is_file():
        raise IsADirectoryError(f"路径不是文件: {path_str}")

    content = p.read_text(encoding="utf-8")
    idx = content.find(find)
    if idx == -1:
        preview = find[:50]
        raise ValueError(f"find 不匹配（前 50 字: {preview}）")

    second_idx = content.find(find, idx + 1)
    if second_idx != -1:
        raise ValueError("find 不唯一，匹配到多个位置，请扩长 find 字符串")

    new_content = content[:idx] + replace + content[idx + len(find):]
    _write_with_backup(p, new_content, content)

    return {
        "path": str(p),
        "replaced": True,
        "old_size": len(content),
        "new_size": len(new_content),
    }


def _delete_lines_sync(path_str, start_line, end_line):
    p = _resolve_path(path_str)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在: {path_str}")
    if not p.is_file():
        raise IsADirectoryError(f"路径不是文件: {path_str}")

    content = p.read_text(encoding="utf-8")
    lines = content.split("\n")
    total = len(lines)

    if start_line < 1:
        raise ValueError(f"start_line 必须 >= 1，收到 {start_line}")
    if end_line < start_line:
        raise ValueError(
            f"end_line {end_line} 小于 start_line {start_line}（删除模式要求 end_line >= start_line）"
        )
    if start_line > total:
        raise ValueError(
            f"start_line {start_line} 超出文件范围（共 {total} 行）"
        )
    if end_line > total:
        raise ValueError(f"end_line {end_line} 超出文件范围（共 {total} 行）")

    old_block = lines[start_line - 1:end_line]
    del lines[start_line - 1:end_line]
    new_text = "\n".join(lines)
    _write_with_backup(p, new_text, content)

    return {
        "path": str(p),
        "start_line": start_line,
        "end_line": end_line,
        "mode": "delete",
        "old_lines": len(old_block),
        "new_lines": 0,
        "old_total": total,
        "new_total": len(lines),
        "old_size": len(content),
        "new_size": len(new_text),
        "deleted_preview": old_block[:3],
    }


def _file_exists_sync(path_str):
    p = _resolve_path(path_str)
    return p.exists()


def _list_dir_sync(path_str):
    p = _resolve_path(path_str)
    if not p.exists():
        raise FileNotFoundError(f"目录不存在: {path_str}")
    if not p.is_dir():
        raise NotADirectoryError(f"路径不是目录: {path_str}")
    items = []
    for entry in sorted(p.iterdir()):
        items.append({
            "name": entry.name,
            "type": "directory" if entry.is_dir() else "file",
            "size": entry.stat().st_size if entry.is_file() else 0,
        })
    return items


def _mkdir_sync(path_str):
    p = _resolve_path(path_str)
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def _delete_file_sync(path_str):
    p = _resolve_path(path_str)
    if not p.exists():
        raise FileNotFoundError(f"路径不存在: {path_str}")
    if p.is_dir():
        if any(p.iterdir()):
            raise OSError(f"目录不为空，无法删除: {path_str}")
        p.rmdir()
    else:
        p.unlink()
    return True


def _file_stat_sync(path_str):
    p = _resolve_path(path_str)
    if not p.exists():
        raise FileNotFoundError(f"路径不存在: {path_str}")
    st = p.stat()
    return {
        "name": p.name,
        "type": "directory" if p.is_dir() else "file",
        "size": st.st_size,
        "modified_at": datetime.fromtimestamp(st.st_mtime).isoformat(),
        "created_at": datetime.fromtimestamp(st.st_ctime).isoformat(),
    }

# ============================================================
# code_structure — 按扩展名解析代码/文本结构，返回行号地图
# ============================================================

def _parse_code_structure_sync(path_str: str) -> dict:
    p = _resolve_path(path_str)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在: {path_str}")
    if not p.is_file():
        raise IsADirectoryError(f"路径不是文件: {path_str}")

    text = p.read_text(encoding="utf-8", errors="replace")
    ext = p.suffix.lower()
    total_lines = text.count("\n") + 1
    size = p.stat().st_size

    base = {
        "path": _rel_path(p),
        "size": size,
        "size_kb": round(size / 1024, 1),
        "total_lines": total_lines,
        "ext": ext,
        "hint": "用 read_file_lines(path, start_line, end_line) 读关键定义，不要读全文",
    }
    if size <= MAX_INLINE_SIZE:
        return {
            **base,
            "language": ext.lstrip(".") or "unknown",
            "structure": {},
            "full_content": text,
            "truncated": False,
            "hint": (
                "文件小于 100KB，已直接返回全文（full_content）。"
                "直接读 full_content 即可，不需要再调 read_file 或 read_file_lines。"
                "如果需要定位函数/类，本次返回的 structure 为空——但文件不大，直接读全文即可。"
            ),
        }
    if ext in (".html", ".htm", ".vue", ".svelte"):
        return {**base, "language": "html", "structure": _parse_html_structure(text)}

    if ext == ".py":
        return {**base, "language": "python", "structure": _parse_python_structure(text)}

    if ext in (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"):
        return {**base, "language": ext.lstrip("."), "structure": _parse_js_structure(text)}

    if ext in (".java", ".kt", ".kts", ".c", ".h", ".cpp", ".cc", ".cxx",
               ".hpp", ".hh", ".hxx", ".go", ".rs"):
        return {**base, "language": ext.lstrip("."), "structure": _parse_c_structure(text)}

    if ext in (".md", ".markdown"):
        return {**base, "language": "markdown", "structure": _parse_md_structure(text)}

    if ext in (".json", ".jsonc", ".json5", ".yaml", ".yml", ".toml", ".ini"):
        return {**base, "language": ext.lstrip("."), "structure": _parse_config_structure(text, ext)}

    return {
        **base,
        "language": "unknown",
        "structure": {},
        "hint": "不支持的结构解析，用 read_file 读全文或 read_file_lines 读指定范围",
    }


# ---------- Python ----------
def _parse_python_structure(text: str) -> dict:
    import ast
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return {"error": f"Python 语法错误: 第 {e.lineno} 行 {e.msg}"}

    classes, functions, imports, globals_ = [], [], [], []

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            classes.append({
                "name": node.name,
                "start": node.lineno,
                "end": node.end_lineno or node.lineno,
            })
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append({
                "name": node.name,
                "start": node.lineno,
                "end": node.end_lineno or node.lineno,
                "type": "async" if isinstance(node, ast.AsyncFunctionDef) else "sync",
            })
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append({"module": alias.name, "line": node.lineno})
        elif isinstance(node, ast.ImportFrom):
            imports.append({"module": node.module or "", "line": node.lineno})
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    globals_.append({"name": target.id, "line": node.lineno})

    seen_imp, uniq_imp = set(), []
    for imp in imports:
        key = (imp["module"], imp["line"])
        if key not in seen_imp:
            seen_imp.add(key)
            uniq_imp.append(imp)

    _fn = sorted(functions, key=lambda x: x["start"])
    _cls = sorted(classes, key=lambda x: x["start"])
    _truncated = len(_fn) > STRUCTURE_MAX_FUNCTIONS or len(_cls) > STRUCTURE_MAX_CLASSES
    return {
        "classes": _cls[:STRUCTURE_MAX_CLASSES],
        "functions": _fn[:STRUCTURE_MAX_FUNCTIONS],
        "imports": uniq_imp[:50],
        "globals": globals_[:30],
        "truncated": _truncated,
        "note": (
            f"functions {len(_fn)} 个只列前 {STRUCTURE_MAX_FUNCTIONS}，"
            f"classes {len(_cls)} 个只列前 {STRUCTURE_MAX_CLASSES}；"
            f"需要完整列表用 read_file_lines 读全文，或按名字 search"
        ) if _truncated else "",
    }


# ---------- HTML ----------
def _parse_html_structure(text: str) -> dict:
    structure = {}
    for tag in ("style", "script", "body", "head"):
        blocks = []
        for m in re.finditer(rf"<{tag}[^>]*>(.*?)</{tag}>", text, re.DOTALL | re.IGNORECASE):
            start = text[:m.start()].count("\n") + 1
            end = text[:m.end()].count("\n") + 1
            blocks.append({"start": start, "end": end})
        if blocks:
            structure[f"{tag}_blocks"] = blocks

    functions = []
    for m in re.finditer(r"<script[^>]*>(.*?)</script>", text, re.DOTALL | re.IGNORECASE):
        js = m.group(1)
        js_start = text[:m.start(1)].count("\n") + 1
        for fm in re.finditer(r"(?:async\s+)?function\s+(\w+)\s*\([^)]*\)\s*\{", js):
            name = fm.group(1)
            start = js_start + js[:fm.start()].count("\n")
            brace, i = 1, fm.end()
            while i < len(js) and brace > 0:
                if js[i] == "{": brace += 1
                elif js[i] == "}": brace -= 1
                i += 1
            end = js_start + js[:i].count("\n")
            functions.append({"name": name, "start": start, "end": end})
        for fm in re.finditer(r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>\s*\{", js):
            name = fm.group(1)
            start = js_start + js[:fm.start()].count("\n")
            brace, i = 1, fm.end()
            while i < len(js) and brace > 0:
                if js[i] == "{": brace += 1
                elif js[i] == "}": brace -= 1
                i += 1
            end = js_start + js[:i].count("\n")
            functions.append({"name": name, "start": start, "end": end})

    seen_fn, uniq_fn = set(), []
    for f in functions:
        if f["name"] not in seen_fn:
            seen_fn.add(f["name"])
            uniq_fn.append(f)
    structure["functions"] = sorted(uniq_fn, key=lambda x: x["start"])[:STRUCTURE_MAX_FUNCTIONS]

    elements = []
    for m in re.finditer(r"<(\w+)([^>]*?\bid=\"([^\"]+)\"[^>]*)>", text, re.IGNORECASE):
        elements.append({
            "id": m.group(3),
            "tag": m.group(1),
            "line": text[:m.start()].count("\n") + 1,
        })
    structure["elements"] = elements[:STRUCTURE_MAX_ELEMENTS]

    handlers = []
    for m in re.finditer(r"on(click|change|submit|input|keydown|keyup|load)\s*=\s*\"([^\"]+)\"", text, re.IGNORECASE):
        handlers.append({
            "event": "on" + m.group(1),
            "handler": m.group(2).strip(),
            "line": text[:m.start()].count("\n") + 1,
        })
    structure["event_handlers"] = handlers[:STRUCTURE_MAX_HANDLERS]
    return structure


# ---------- JS/TS ----------
def _parse_js_structure(text: str) -> dict:
    functions, classes, interfaces, types, imports = [], [], [], [], []

    def _find_brace_end(start_idx):
        brace, i = 1, start_idx
        while i < len(text) and brace > 0:
            if text[i] == "{": brace += 1
            elif text[i] == "}": brace -= 1
            i += 1
        return i

    for m in re.finditer(r"(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\([^)]*\)\s*\{", text):
        start = text[:m.start()].count("\n") + 1
        end = text[:_find_brace_end(m.end())].count("\n") + 1
        functions.append({"name": m.group(1), "start": start, "end": end})

    for m in re.finditer(r"(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>\s*\{", text):
        start = text[:m.start()].count("\n") + 1
        end = text[:_find_brace_end(m.end())].count("\n") + 1
        functions.append({"name": m.group(1), "start": start, "end": end})

    for m in re.finditer(r"(?:export\s+)?class\s+(\w+)", text):
        brace = text.find("{", m.end())
        if brace == -1: continue
        start = text[:m.start()].count("\n") + 1
        end = text[:_find_brace_end(brace + 1)].count("\n") + 1
        classes.append({"name": m.group(1), "start": start, "end": end})

    for m in re.finditer(r"(?:export\s+)?interface\s+(\w+)", text):
        brace = text.find("{", m.end())
        if brace == -1: continue
        start = text[:m.start()].count("\n") + 1
        end = text[:_find_brace_end(brace + 1)].count("\n") + 1
        interfaces.append({"name": m.group(1), "start": start, "end": end})

    for m in re.finditer(r"(?:export\s+)?type\s+(\w+)\s*=", text):
        start = text[:m.start()].count("\n") + 1
        line_end = text.find("\n", m.end())
        end = text[:line_end if line_end != -1 else len(text)].count("\n") + 1
        types.append({"name": m.group(1), "start": start, "end": end})

    for m in re.finditer(r"import\s+.*?from\s+['\"]([^'\"]+)['\"]", text):
        imports.append({"module": m.group(1), "line": text[:m.start()].count("\n") + 1})

    def _uniq(items):
        seen, out = set(), []
        for x in items:
            if x["name"] not in seen:
                seen.add(x["name"])
                out.append(x)
        return sorted(out, key=lambda x: x["start"])

    _fn = _uniq(functions)
    _cls = _uniq(classes)
    _if = _uniq(interfaces)
    _tp = _uniq(types)
    _truncated = (
        len(_fn) > STRUCTURE_MAX_FUNCTIONS
        or len(_cls) > STRUCTURE_MAX_CLASSES
        or len(_if) > STRUCTURE_MAX_INTERFACES
        or len(_tp) > STRUCTURE_MAX_TYPES
    )
    return {
        "functions": _fn[:STRUCTURE_MAX_FUNCTIONS],
        "classes": _cls[:STRUCTURE_MAX_CLASSES],
        "interfaces": _if[:STRUCTURE_MAX_INTERFACES],
        "types": _tp[:STRUCTURE_MAX_TYPES],
        "imports": imports[:50],
        "truncated": _truncated,
        "note": (
            "结构列表已截断（functions/classes/interfaces/types 上限 "
            f"{STRUCTURE_MAX_FUNCTIONS}/{STRUCTURE_MAX_CLASSES}/"
            f"{STRUCTURE_MAX_INTERFACES}/{STRUCTURE_MAX_TYPES}）；"
            "需要完整列表用 read_file_lines 读全文，或按名字 search"
        ) if _truncated else "",
    }


# ---------- C 系 ----------
def _parse_c_structure(text: str) -> dict:
    classes, functions, imports = [], [], []

    def _find_brace_end(start_idx):
        brace, i = 1, start_idx
        while i < len(text) and brace > 0:
            if text[i] == "{": brace += 1
            elif text[i] == "}": brace -= 1
            i += 1
        return i

    for m in re.finditer(r"(?:public\s+|private\s+|protected\s+|abstract\s+|final\s+|static\s+)?(?:class|struct|interface|enum|trait|impl)\s+(\w+)", text):
        brace = text.find("{", m.end())
        if brace == -1: continue
        start = text[:m.start()].count("\n") + 1
        end = text[:_find_brace_end(brace + 1)].count("\n") + 1
        classes.append({"name": m.group(1), "start": start, "end": end})

    _KEYWORDS = {"if", "for", "while", "switch", "catch", "try", "return", "new",
                 "else", "do", "sizeof", "match", "loop", "go", "defer"}

    for m in re.finditer(r"(?:public|private|protected|static|final|async|synchronized|override|virtual)?\s*(?:[\w<>\[\],\s]+\s+)?(\w+)\s*\([^)]*\)\s*(?:throws\s+[\w,\s]+)?\{", text):
        name = m.group(1)
        if name in _KEYWORDS: continue
        start = text[:m.start()].count("\n") + 1
        end = text[:_find_brace_end(m.end())].count("\n") + 1
        functions.append({"name": name, "start": start, "end": end})

    for m in re.finditer(r"^(?:import|package|use|extern\s+crate|mod)\s+([^\n;{]+)", text, re.MULTILINE):
        imports.append({"module": m.group(1).strip(), "line": text[:m.start()].count("\n") + 1})

    def _uniq(items):
        seen, out = set(), []
        for x in items:
            if x["name"] not in seen:
                seen.add(x["name"])
                out.append(x)
        return sorted(out, key=lambda x: x["start"])

    _fn = _uniq(functions)
    _cls = _uniq(classes)
    _truncated = len(_fn) > STRUCTURE_MAX_FUNCTIONS or len(_cls) > STRUCTURE_MAX_CLASSES
    return {
        "classes": _cls[:STRUCTURE_MAX_CLASSES],
        "functions": _fn[:STRUCTURE_MAX_FUNCTIONS],
        "imports": imports[:50],
        "truncated": _truncated,
        "note": (
            "结构列表已截断；需要完整列表用 read_file_lines 读全文，或按名字 search"
        ) if _truncated else "",
    }


# ---------- Markdown ----------
def _parse_md_structure(text: str) -> dict:
    headings, code_blocks = [], []
    lines = text.split("\n")
    in_code, code_start, code_lang = False, 0, ""

    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("```"):
            if not in_code:
                in_code = True
                code_start = i
                code_lang = stripped[3:].strip() or "text"
            else:
                in_code = False
                code_blocks.append({"lang": code_lang, "start": code_start, "end": i})
            continue
        if in_code:
            continue
        m = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if m:
            headings.append({
                "level": len(m.group(1)),
                "text": m.group(2).strip(),
                "line": i,
            })

    if in_code:
        code_blocks.append({"lang": code_lang, "start": code_start, "end": len(lines), "unclosed": True})

    return {"headings": headings, "code_blocks": code_blocks}


# ---------- 配置（JSON/YAML/TOML/INI）----------
def _parse_config_structure(text: str, ext: str) -> dict:
    keys = []
    lines = text.split("\n")

    if ext in (".json", ".jsonc", ".json5"):
        depth = 0
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("//") or stripped.startswith("/*"):
                continue
            m = re.match(r'^"([^"]+)"\s*:', stripped)
            if m and depth <= 1:
                keys.append({"key": m.group(1), "line": i})
            depth += line.count("{") + line.count("[") - line.count("}") - line.count("]")

    elif ext in (".yaml", ".yml"):
        for i, line in enumerate(lines, 1):
            if line and not line.startswith((" ", "\t", "#", "-")):
                m = re.match(r"^([\w.-]+)\s*:", line)
                if m:
                    keys.append({"key": m.group(1), "line": i})
            elif line.startswith("  ") and not line.startswith("    "):
                m = re.match(r"^\s+([\w.-]+)\s*:", line)
                if m:
                    keys.append({"key": "  " + m.group(1), "line": i})

    elif ext == ".toml":
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            m = re.match(r"^\[([^\]]+)\]", stripped)
            if m:
                keys.append({"section": m.group(1), "line": i})
                continue
            m = re.match(r"^([\w.-]+)\s*=", stripped)
            if m:
                keys.append({"key": m.group(1), "line": i})

    elif ext == ".ini":
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            m = re.match(r"^\[([^\]]+)\]", stripped)
            if m:
                keys.append({"section": m.group(1), "line": i})

    return {"keys": keys[:100]}


def _search_files_sync(path_str, file_pattern="*", content_pattern=None,
                       max_results=10, context=0, content_regex=False,
                       count_only=False):
    """按文件名 glob + 内容匹配搜索文件。

    语义（正交，互不污染）：
      - file_pattern 空/缺省 → 所有文件；非空 → 只扫匹配 glob 的文件
      - content_pattern 空   → 只列文件（mode=files），不读内容
      - content_pattern 非空 → 在筛出的文件里搜内容（mode=content）
      - content_regex=True  → content_pattern 按 Python re 解析；False → 纯子串
    返回带 mode / binary_skipped / scan_truncated / context_applied，调用方无需猜。
    """
    p = _resolve_path(path_str)
    if not p.exists():
        raise FileNotFoundError(f"路径不存在: {path_str}")

    scope = "dir" if p.is_dir() else "file"
    if p.is_file():
        files_iter = [p]
    else:
        files_iter = p.rglob(file_pattern or "*")
    matches = []
    scanned = 0
    binary_skipped = 0
    large_skipped = 0
    scan_truncated = False

    ctx_raw = int(context or 0)
    ctx = max(0, min(ctx_raw, 10))
    context_applied = ctx if ctx != ctx_raw else None

    content_re = None
    if content_pattern:
        if content_regex:
            try:
                content_re = re.compile(content_pattern)
            except re.error as e:
                raise ValueError(f"content_pattern 正则无效: {e}")
        else:
            content_re = None

    skip_dirs = {"__pycache__", ".git", "node_modules", ".venv", "dist", "build"}
    MAX_SCAN_FILES = 5000

    def _hit(line: str) -> bool:
        if content_re is not None:
            return content_re.search(line) is not None
        return content_pattern in line

    for f in files_iter:
        if not f.is_file():
            continue
        if any(part in skip_dirs for part in f.parts):
            continue

        scanned += 1
        if scanned > MAX_SCAN_FILES:
            scan_truncated = True
            break

        try:
            fsize = f.stat().st_size
        except OSError:
            continue

        if not content_pattern:
            matches.append({"path": _rel_path(f), "size": fsize})
            if len(matches) >= max_results:
                break
            continue

        if fsize > SEARCH_MAX_FILE_BYTES:
            large_skipped += 1
            continue
        try:
            with open(f, "rb") as fb:
                head = fb.read(8192)
            if b"\x00" in head:
                binary_skipped += 1
                continue
        except OSError:
            continue

        try:
            lines = []
            with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    lines.append(line.rstrip("\r\n"))
        except Exception:
            continue

        hit_lines = [ln for ln, line in enumerate(lines, 1) if _hit(line)]
        if not hit_lines:
            continue

        if count_only:
            matches.append({"path": _rel_path(f), "size": fsize,
                            "count": len(hit_lines)})
            if len(matches) >= max_results:
                break
            continue

        for ln in hit_lines:
            item = {"path": _rel_path(f), "size": fsize, "line": ln,
                    "text": lines[ln - 1]}
            if ctx > 0:
                lo = max(1, ln - ctx)
                hi = min(len(lines), ln + ctx)
                item["context"] = [
                    {"line": n, "text": lines[n - 1]}
                    for n in range(lo, hi + 1)
                ]
            matches.append(item)
            if len(matches) >= max_results:
                break
        if len(matches) >= max_results:
            break

    mode = "files" if not content_pattern else "content"
    result = {
        "mode": mode,
        "scope": scope,
        "matches": matches,
        "total": len(matches),
        "scanned": scanned,
        "truncated": len(matches) >= max_results or scan_truncated,
        "scan_truncated": scan_truncated,
        "binary_skipped": binary_skipped,
        "large_skipped": large_skipped,
        "context": ctx,
    }
    if context_applied is not None:
        result["context_applied"] = context_applied
        result["context_note"] = f"context 上限 10，已从 {ctx_raw} 钳到 {ctx}"
    if ctx > 0 and len(matches) >= max_results // 2:
        result["context_advice"] = (
            f"context={ctx} 让每条命中带 {ctx*2+1} 行上下文，"
            f"{len(matches)} 条命中共约 {len(matches)*(ctx*2+1)} 行。"
            f"如果只是定位、不需要上下文，下次传 context=0 能大幅减少返回体积。"
        )
    return result

# ============================================================
# 插件入口
# ============================================================

async def execute(envelop, agent):
    action = envelop.payload.get("action", "")
    path_str = envelop.payload.get("path", "")

    if not isinstance(path_str, str) or path_str == "":
        envelop.payload = {"ok": False, "error": "缺少 path 参数"}
        return envelop

    try:
        loop = asyncio.get_running_loop()

        if action == "read_file":
            result = await loop.run_in_executor(None, lambda: _read_file_sync(path_str))
            envelop.payload = {"ok": True, "data": result}
            return envelop

        elif action == "read_file_lines":
            start_line = envelop.payload.get("start_line")
            end_line = envelop.payload.get("end_line")
            with_line_num = envelop.payload.get("with_line_num", False)

            if start_line is not None:
                start_line = int(start_line)
            if end_line is not None:
                end_line = int(end_line)

            result = await loop.run_in_executor(
                None,
                lambda: _read_file_lines_sync(path_str, start_line, end_line, with_line_num),
            )
            envelop.payload = {"ok": True, "data": result}
            return envelop

        elif action == "write_file":
            if "content" not in envelop.payload:
                envelop.payload = {
                    "ok": False,
                    "error": "缺少 content 参数（模型漏传，可能是 tool_call 不完整）"
                }
                return envelop

            content = envelop.payload.get("content", "")
            if not isinstance(content, str):
                envelop.payload = {"ok": False, "error": "content 必须是字符串"}
                return envelop

            ext = Path(path_str).suffix.lower()
            if content == "" and ext in (
                ".html", ".js", ".py", ".css", ".json", ".md",
                ".ts", ".tsx", ".jsx", ".vue", ".xml", ".yaml", ".yml",
            ):
                envelop.payload = {
                    "ok": False,
                    "error": f"content 为空，拒绝写入 {ext} 文件（可能是模型漏传或输出被截断）"
                }
                return envelop

            old_size = 0
            try:
                target = _resolve_path(path_str)
                if target.exists():
                    old_size = target.stat().st_size
            except Exception:
                old_size = 0

            if content == "":
                try:
                    agent.log.warning(
                        f"[file_utils_api] write_file 写入空内容: "
                        f"path={path_str} old_size={old_size}"
                    )
                except Exception:
                    pass

            p = await loop.run_in_executor(None, lambda: _write_file_sync(path_str, content))
            actual_size = p.stat().st_size
            validation = _validate_written_file(p)
            data = {
                "path": _rel_path(p),
                "path_kind": _path_kind(p),
                "size": actual_size,
                "old_size": old_size,
                "new_size": actual_size,
                "validation": validation,
            }
            if validation.get("checked") and not validation.get("ok"):
                data["validation_failed"] = True
                data["warning"] = (
                    f"写后校验失败（文件已保存，但可能有语法问题）：{validation['error']}"
                )
            envelop.payload = {"ok": True, "data": data}
            return envelop

        elif action == "append_file":
            content = envelop.payload.get("content", "")
            if not isinstance(content, str):
                envelop.payload = {"ok": False, "error": "content 必须是字符串"}
                return envelop
            p = await loop.run_in_executor(None, lambda: _append_file_sync(path_str, content))
            validation = _validate_written_file(p)
            data = {
                "path": _rel_path(p),
                "path_kind": _path_kind(p),
                "validation": validation,
            }
            if validation.get("checked") and not validation.get("ok"):
                data["validation_failed"] = True
                data["warning"] = f"追加后校验失败：{validation['error']}"
            envelop.payload = {"ok": True, "data": data}
            return envelop

        elif action == "edit_file":
            find = envelop.payload.get("find")
            replace = envelop.payload.get("replace", "")
            if not find or not isinstance(find, str):
                envelop.payload = {"ok": False, "error": "缺少 find 参数"}
                return envelop
            if not isinstance(replace, str):
                envelop.payload = {"ok": False, "error": "replace 必须是字符串"}
                return envelop
            result = await loop.run_in_executor(
                None, lambda: _edit_file_sync(path_str, find, replace)
            )
            validation = _validate_written_file(Path(result["path"]))
            result["validation"] = validation
            if validation.get("checked") and not validation.get("ok"):
                result["validation_failed"] = True
                result["warning"] = f"编辑后校验失败：{validation['error']}"
            abs_path = result["path"]
            result["path"] = _rel_path(abs_path)
            result["path_kind"] = _path_kind(abs_path)
            envelop.payload = {"ok": True, "data": result}
            return envelop

        elif action == "delete_lines":
            start_line = envelop.payload.get("start_line")
            end_line = envelop.payload.get("end_line")
            if start_line is None or end_line is None:
                envelop.payload = {"ok": False, "error": "缺少 start_line 或 end_line 参数"}
                return envelop
            start_line = int(start_line)
            end_line = int(end_line)
            try:
                result = await loop.run_in_executor(
                    None,
                    lambda: _delete_lines_sync(path_str, start_line, end_line),
                )
            except ValueError as e:
                envelop.payload = {"ok": False, "error": str(e)}
                return envelop
            validation = _validate_written_file(Path(result["path"]))
            result["validation"] = validation
            if validation.get("checked") and not validation.get("ok"):
                result["validation_failed"] = True
                result["warning"] = f"删除后校验失败：{validation['error']}"
            abs_path = result["path"]
            result["path"] = _rel_path(abs_path)
            result["path_kind"] = _path_kind(abs_path)
            envelop.payload = {"ok": True, "data": result}
            return envelop
        elif action == "file_exists":
            exists = await loop.run_in_executor(None, lambda: _file_exists_sync(path_str))
            envelop.payload = {"ok": True, "data": {"exists": exists}}
            return envelop

        elif action == "list_dir":
            items = await loop.run_in_executor(None, lambda: _list_dir_sync(path_str))
            envelop.payload = {"ok": True, "data": {"items": items}}
            return envelop

        elif action == "mkdir":
            created_path = await loop.run_in_executor(None, lambda: _mkdir_sync(path_str))
            envelop.payload = {
                "ok": True,
                "data": {
                    "path": _rel_path(created_path),
                    "path_kind": _path_kind(created_path),
                },
            }
            return envelop

        elif action == "delete_file":
            await loop.run_in_executor(None, lambda: _delete_file_sync(path_str))
            envelop.payload = {"ok": True, "data": {"deleted": True}}
            return envelop

        elif action == "file_stat":
            stat = await loop.run_in_executor(None, lambda: _file_stat_sync(path_str))
            envelop.payload = {"ok": True, "data": {"stat": stat}}
            return envelop

        elif action == "code_structure":
            result = await loop.run_in_executor(
                None, lambda: _parse_code_structure_sync(path_str)
            )
            envelop.payload = {"ok": True, "data": result}
            return envelop

        elif action == "search":
            file_pattern = envelop.payload.get(
                "file_pattern",
                envelop.payload.get("pattern", "*"),
            )
            content_pattern = envelop.payload.get(
                "content_pattern",
                envelop.payload.get("content"),
            )
            max_results = int(envelop.payload.get("max_results", 10))
            context = int(envelop.payload.get("context", 0) or 0)
            content_regex = bool(envelop.payload.get("content_regex", False))
            count_only = bool(envelop.payload.get("count_only", False))

            # ★ 自动修正：content_pattern 含 '|' 但没设 content_regex 时，
            #   模型意图明确（多关键词），直接按正则执行，不返回错误。
            #   拦截会让模型反复撞（实测 23 次），自动修让它一次拿结果。
            _auto_regex = False
            if (isinstance(content_pattern, str) and "|" in content_pattern
                    and not content_regex):
                content_regex = True
                _auto_regex = True

            result = await loop.run_in_executor(
                None,
                lambda: _search_files_sync(
                    path_str, file_pattern, content_pattern,
                    max_results, context, content_regex, count_only,
                ),
            )
            if _auto_regex:
                result["auto_regex"] = (
                    "content_pattern 含 '|'，已自动按 content_regex=true 执行。"
                    "下次多关键词搜索时直接传 content_regex=true，可省一次修正。"
                )
            if result.get("mode") == "content" and result.get("total") == 0:
                result["hint"] = (
                    "0 命中。不要用同样参数重搜，按顺序排查：\n"
                    "1) 关键词是否拼错 / 需要正则：多关键词设 content_regex=true；\n"
                    "2) 文件范围是否太窄：放宽 file_pattern，或 path 指到更上层目录；\n"
                    "3) 换同义词再搜一次（如 handler↔处理、read↔读取）；\n"
                    "4) 仍 0 命中：用 list_dir 看目录里有哪些文件，"
                    "或 read_file_lines 读指定范围——不要 read_file 通读全文。"
                )
            if result.get("truncated") and result.get("mode") == "content":
                result["truncated_hint"] = (
                    f"只返回前 {max_results} 条命中（已达 max_results 上限）。"
                    f"如需要更多，传 max_results=50，或缩小 content_pattern / "
                    f"file_pattern 范围。不要因为看不到全部就重搜同样的参数。"
                )
            envelop.payload = {"ok": True, "data": result}
            return envelop

        else:
            available = (
                "code_structure/read_file/read_file_lines/write_file/append_file/"
                "edit_file/delete_lines/file_exists/list_dir/mkdir/"
                "delete_file/file_stat/search"
            )
            if not action:
                envelop.payload = {
                    "ok": False,
                    "error": f"缺少 action 参数。可用操作: {available}",
                }
            else:
                envelop.payload = {
                    "ok": False,
                    "error": f"未知操作: {action}，可用操作: {available}",
                }
            return envelop

    except (ValueError, FileNotFoundError, IsADirectoryError, NotADirectoryError, OSError) as e:
        envelop.payload = {"ok": False, "error": str(e)}
        return envelop
    except Exception as e:
        envelop.payload = {"ok": False, "error": f"操作失败: {str(e)}"}
        return envelop
# @AICP_ALIGN: actions=read_file,read_file_lines,write_file,append_file,edit_file,delete_lines,file_exists,list_dir,mkdir,delete_file,file_stat,search,code_structure | output_fields=full_content,preview,size,size_kb,total_lines,preview_start_line,preview_lines,preview_bytes,preview_kb,truncated,hint,lines,start,end,returned,with_line_num,remaining_after,path,exists,items,deleted,stat,name,type,modified_at,created_at,replaced,old_size,new_size,validation,checked,ok,error,kind,warning,language,ext,structure,classes,functions,imports,globals,elements,event_handlers,headings,code_blocks,keys,start_line,end_line,mode,old_lines,new_lines,matches,total,scanned,scan_truncated,binary_skipped,large_skipped,context,context_applied,context_note,context_advice,count,text,truncated_hint,note | input_fields=action,path,content,find,replace,start_line,end_line,with_line_num,file_pattern,content_pattern,content_regex,count_only,max_results,context,pattern | type_values=directory,file
