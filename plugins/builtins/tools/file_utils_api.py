import asyncio
import os
import sys
import re
from pathlib import Path
from datetime import datetime

# ============================================================
# 黑名单配置 —— 只禁止系统目录，其他全放行
# ============================================================

FORBIDDEN_PREFIXES = (
    # Windows 系统目录
    "C:/Windows/",
    "C:/Program Files/",
    "C:/Program Files (x86)/",
    "C:/System32/",
    "C:/syswow64/",
    "C:/boot/",
    "C:/efi/",
    "C:/PerfLogs/",
    "C:/ProgramData/",
    "C:/Recovery/",
    "C:/System Volume Information/",
    "C:/pagefile.sys",
    "C:/hiberfil.sys",
    # Linux 系统目录
    "/etc/",
    "/proc/",
    "/sys/",
    "/boot/",
    "/dev/",
    "/root/",
    "/usr/",
    "/bin/",
    "/sbin/",
    "/lib/",
    "/lib64/",
    "/opt/",
    "/var/",
    "/tmp/",
    "/run/",
    "/mnt/",
    "/media/",
    "/srv/",
    "/lost+found/",
    "/.snapshots/",
    "/.Trash-",
    # 关键文件
    "/etc/passwd",
    "/etc/shadow",
    "/etc/sudoers",
    "/etc/hosts",
    "/etc/fstab",
    "/etc/crontab",
    "/etc/ssh/",
    "/etc/ssl/",
    "/root/.ssh/",
    "/root/.bash_history",
    "/root/.bashrc",
)

MAX_INLINE_SIZE = 100 * 1024  # 100KB


# ============================================================
# 路径解析 —— 不限制项目根目录，只拦黑名单
# ============================================================

def _resolve_path(path_str: str) -> Path:
    """只检查黑名单，允许任何非系统目录"""
    normalized = path_str.replace("\\", "/")

    # 1. 黑名单检查
    for forbidden in FORBIDDEN_PREFIXES:
        if normalized.lower().startswith(forbidden.lower()):
            raise ValueError(f"禁止访问系统目录: {path_str}")

    # 2. 如果是以 / 开头的相对路径写法（/data → data/）
    if normalized.startswith("/"):
        normalized = normalized[1:]

    # 3. 直接解析，不限制项目根目录
    return Path(normalized).resolve()


# ============================================================
# apply_patch — 应用 unified diff 补丁
# ============================================================

def _parse_patch(patch: str):
    """解析 unified diff，返回 hunks 列表"""
    patch_lines = patch.split("\n")
    hunks = []
    current = None

    for line in patch_lines:
        # 跳过文件头
        if line.startswith("---") or line.startswith("+++"):
            continue

        # hunk 头
        hunk_match = re.match(r'^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@', line)
        if hunk_match:
            if current is not None:
                _push_hunk(hunks, current)
            current = []
            continue

        # hunk 内容
        if current is not None:
            if line.startswith(" "):
                current.append(("context", line[1:]))
            elif line.startswith("-"):
                current.append(("remove", line[1:]))
            elif line.startswith("+"):
                current.append(("add", line[1:]))
            elif line == "":
                current.append(("context", ""))

    if current is not None:
        _push_hunk(hunks, current)

    return hunks


def _push_hunk(hunks, hunk):
    """删除 hunk 末尾的空 context 行（patch 末尾 \n 会被 split 成空行）"""
    while len(hunk) > 0 and hunk[-1][0] == "context" and hunk[-1][1] == "":
        hunk.pop()
    hunks.append(hunk)


def _apply_patch(content: str, patch: str) -> dict:
    """应用 unified diff 补丁，返回 {ok, result, error}"""
    try:
        hunks = _parse_patch(patch)
    except Exception as e:
        return {"ok": False, "error": f"patch 解析失败: {e}"}

    if len(hunks) == 0:
        return {"ok": False, "error": "没有找到有效的 hunk"}

    result = content

    for h, hunk in enumerate(hunks):
        # 收集"上下文 + 删除"的行
        old_lines = [text for typ, text in hunk if typ in ("context", "remove")]
        old_text = "\n".join(old_lines)

        # 收集"上下文 + 新增"的行
        new_lines = [text for typ, text in hunk if typ in ("context", "add")]
        new_text = "\n".join(new_lines)

        if not old_text:
            return {"ok": False, "error": f"hunk #{h + 1} 没有上下文或删除行"}

        idx = result.find(old_text)
        if idx == -1:
            preview = old_text[:50]
            return {"ok": False, "error": f"hunk #{h + 1} 不匹配（前 50 字: {preview}）"}

        second_idx = result.find(old_text, idx + 1)
        if second_idx != -1:
            return {"ok": False, "error": f"hunk #{h + 1} 不唯一，匹配到多个位置"}

        result = result[:idx] + new_text + result[idx + len(old_text):]

    return {"ok": True, "result": result}


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

    # 文件 ≤ 100KB：正常读取全部内容
    if file_size <= MAX_INLINE_SIZE:
        content = p.read_text(encoding='utf-8')
        return {
            "full_content": content,
            "size": file_size,
            "size_kb": round(file_size / 1024, 1),
            "truncated": False,
        }

    # 文件 > 100KB：读取最后 100KB 内容
    with open(p, 'r', encoding='utf-8') as f:
        f.seek(0, 2)
        file_size = f.tell()

        read_size = min(file_size, MAX_INLINE_SIZE + 1024)
        f.seek(file_size - read_size)

        tail_bytes = f.read()
        lines = tail_bytes.splitlines(keepends=True)

        preview_lines = []
        preview_size = 0
        for line in reversed(lines):
            line_size = len(line.encode('utf-8'))
            if preview_size + line_size > MAX_INLINE_SIZE:
                break
            preview_lines.insert(0, line)
            preview_size += line_size

        total_lines = sum(1 for _ in open(p, 'r', encoding='utf-8'))
        start_line = total_lines - len(preview_lines) + 1

        preview_text = ''.join(preview_lines)
        preview_bytes = len(preview_text.encode('utf-8'))

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
        "hint": f"文件超过 100KB（{round(file_size/1024,1)}KB），只返回最后 {preview_bytes/1024:.1f}KB 内容（{len(preview_lines)} 行）。如需读取指定范围，请使用 read_file_lines（path, start_line, end_line）"
    }


def _read_file_lines_sync(path_str, start_line=None, end_line=None, with_line_num=False):
    p = _resolve_path(path_str)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在: {path_str}")
    if not p.is_file():
        raise IsADirectoryError(f"路径不是文件: {path_str}")

    lines = p.read_text(encoding='utf-8').splitlines()
    total_lines = len(lines)

    start = start_line if start_line is not None else 1
    end = end_line if end_line is not None else total_lines

    if start < 1:
        start = 1
    if end > total_lines:
        end = total_lines
    if start > end:
        raise ValueError(f"起始行 {start} 大于结束行 {end}，文件总行数: {total_lines}")

    selected = lines[start - 1:end]

    if with_line_num:
        result = []
        for i, line in enumerate(selected, start=start):
            result.append({"line_num": i, "content": line})
    else:
        result = selected

    return {
        "lines": result,
        "total_lines": total_lines,
        "start": start,
        "end": end,
        "returned": len(selected),
        "with_line_num": with_line_num
    }


def _write_file_sync(path_str, content):
    p = _resolve_path(path_str)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding='utf-8')
    return str(p)


def _append_file_sync(path_str, content):
    p = _resolve_path(path_str)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'a', encoding='utf-8') as f:
        f.write(content)
    return str(p)


def _edit_file_sync(path_str, find, replace):
    """局部替换：find 必须唯一匹配"""
    p = _resolve_path(path_str)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在: {path_str}")
    if not p.is_file():
        raise IsADirectoryError(f"路径不是文件: {path_str}")

    content = p.read_text(encoding='utf-8')
    idx = content.find(find)
    if idx == -1:
        preview = find[:50]
        raise ValueError(f"find 不匹配（前 50 字: {preview}）")

    second_idx = content.find(find, idx + 1)
    if second_idx != -1:
        raise ValueError("find 不唯一，匹配到多个位置，请扩长 find 字符串")

    new_content = content[:idx] + replace + content[idx + len(find):]
    p.write_text(new_content, encoding='utf-8')

    return {
        "path": str(p),
        "replaced": True,
        "old_size": len(content),
        "new_size": len(new_content),
    }


def _apply_patch_sync(path_str, patch):
    """应用 unified diff 补丁"""
    p = _resolve_path(path_str)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在: {path_str}")
    if not p.is_file():
        raise IsADirectoryError(f"路径不是文件: {path_str}")

    content = p.read_text(encoding='utf-8')
    result = _apply_patch(content, patch)

    if not result["ok"]:
        raise ValueError(result["error"])

    new_content = result["result"]
    p.write_text(new_content, encoding='utf-8')

    return {
        "path": str(p),
        "patched": True,
        "old_size": len(content),
        "new_size": len(new_content),
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
# 插件入口
# ============================================================

async def execute(envelop, agent):
    action = envelop.payload.get("action", "")
    path_str = envelop.payload.get("path", "")

    if not path_str and action not in ("",):
        envelop.payload = {"ok": False, "error": "缺少 path 参数"}
        return envelop

    try:
        loop = asyncio.get_event_loop()

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
                lambda: _read_file_lines_sync(path_str, start_line, end_line, with_line_num)
            )
            envelop.payload = {"ok": True, "data": result}
            return envelop

        elif action == "write_file":
            content = envelop.payload.get("content", "")
            if not isinstance(content, str):
                envelop.payload = {"ok": False, "error": "content 必须是字符串"}
                return envelop
            saved_path = await loop.run_in_executor(None, lambda: _write_file_sync(path_str, content))
            actual_size = Path(saved_path).stat().st_size
            envelop.payload = {"ok": True, "data": {"path": saved_path, "size": actual_size}}
            return envelop

        elif action == "append_file":
            content = envelop.payload.get("content", "")
            if not isinstance(content, str):
                envelop.payload = {"ok": False, "error": "content 必须是字符串"}
                return envelop
            saved_path = await loop.run_in_executor(None, lambda: _append_file_sync(path_str, content))
            envelop.payload = {"ok": True, "data": {"path": saved_path}}
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
                None,
                lambda: _edit_file_sync(path_str, find, replace)
            )
            envelop.payload = {"ok": True, "data": result}
            return envelop

        elif action == "apply_patch":
            patch = envelop.payload.get("content")
            if not patch or not isinstance(patch, str):
                envelop.payload = {"ok": False, "error": "缺少 content 参数（patch 内容）"}
                return envelop
            result = await loop.run_in_executor(
                None,
                lambda: _apply_patch_sync(path_str, patch)
            )
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
            envelop.payload = {"ok": True, "data": {"path": created_path}}
            return envelop

        elif action == "delete_file":
            await loop.run_in_executor(None, lambda: _delete_file_sync(path_str))
            envelop.payload = {"ok": True, "data": {"deleted": True}}
            return envelop

        elif action == "file_stat":
            stat = await loop.run_in_executor(None, lambda: _file_stat_sync(path_str))
            envelop.payload = {"ok": True, "data": {"stat": stat}}
            return envelop

        else:
            envelop.payload = {"ok": False, "error": f"未知操作: {action}"}
            return envelop

    except (ValueError, FileNotFoundError, IsADirectoryError, NotADirectoryError, OSError) as e:
        envelop.payload = {"ok": False, "error": str(e)}
        return envelop
    except Exception as e:
        envelop.payload = {"ok": False, "error": f"操作失败: {str(e)}"}
        return envelop


# @AICP_ALIGN: actions=read_file,read_file_lines,write_file,append_file,edit_file,apply_patch,file_exists,list_dir,mkdir,delete_file,file_stat | output_fields=content,full_content,preview,size,size_kb,total_lines,preview_start_line,preview_lines,preview_bytes,preview_kb,truncated,hint,lines,start,end,returned,with_line_num,path,exists,items,deleted,stat,name,type,modified_at,created_at,replaced,old_size,new_size,patched | input_fields=action,path,content,find,replace,start_line,end_line,with_line_num | type_values=directory,file
