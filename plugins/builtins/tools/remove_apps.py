"""remove_apps — 删除已有工具/应用（完全独立）"""

import json
import re
import shutil
from pathlib import Path
from typing import Optional

import core


# ============================================================
# 内部工具函数
# ============================================================

def _log(msg: str):
    print(f"[remove_tool] {msg}", flush=True)


async def _get_project_list(agent) -> list:
    """从 cogitor 获取项目列表"""
    try:
        result = await agent.system.call(core.Envelop(
            sender="builtins/tools/remove_tool",
            receiver="builtins/agents/cogitor",
            payload={"action": "get_map"}
        ))
        if result and result.payload:
            data = result.payload.get("data", {})
            projects = data.get("projects", {})
            project_list = []
            for category, proj_dict in projects.items():
                for proj_name in proj_dict.keys():
                    if proj_name not in project_list:
                        project_list.append(proj_name)
            return project_list
        return []
    except Exception:
        return []


async def _find_project(agent, target: str) -> Optional[str]:
    """从输入中提取项目名"""
    if not target:
        return None

    # 1. 完整路径 applications/xxx/xxx_api → 提取项目名
    if "applications/" in target:
        parts = target.split("applications/")[-1].split("/")
        if parts:
            candidate = parts[0].strip()
            if candidate and Path(f"plugins/applications/{candidate}").exists():
                return candidate

    # 2. ★★★ 兜底：直接检查文件系统 ★★★
    direct_path = Path(f"plugins/applications/{target}")
    if direct_path.exists() and direct_path.is_dir():
        return target

    # 去掉 _api 后缀再检查
    clean_target = re.sub(r'_api$', '', target)
    clean_target = re.sub(r'\.py$', '', clean_target)
    if clean_target and clean_target != target:
        direct_path2 = Path(f"plugins/applications/{clean_target}")
        if direct_path2.exists() and direct_path2.is_dir():
            return clean_target

    # 2.5 只剩数据目录（前后端已删）时也要能找到，否则 data 残留永远删不掉
    if "/" not in target and "\\" not in target and target not in (".", ".."):
        reserved = {"uploads", "www", "memories", "skills", "mcp_agent",
                    "os_cron", "apps", "logs", "plugins", "workspace"}
        data_root = Path(getattr(agent, "data_dir", None) or "data")
        if target.lower() not in reserved and (data_root / target).is_dir():
            return target

    # 3. 从 cogitor 获取项目列表
    projects = await _get_project_list(agent)
    if not projects:
        projects = [d.name for d in Path("plugins/applications").iterdir() if d.is_dir()]

    if target in projects:
        return target

    target_lower = target.lower()
    for p in projects:
        if p.lower() == target_lower:
            return p

    for p in projects:
        if target in p or p in target:
            return p

    if clean_target and clean_target != target:
        for p in projects:
            if clean_target in p or p in clean_target:
                return p

    return None


def _collect_paths(obj, out: set):
    """递归收集 JSON 里可能指向上传文件的路径值（file_path / path / url）"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and k in ("file_path", "path", "url") and v.strip():
                out.add(v.strip())
            else:
                _collect_paths(v, out)
    elif isinstance(obj, list):
        for it in obj:
            _collect_paths(it, out)


def _cleanup_uploads(agent, project_name: str) -> list:
    """删除 data/{PROJECT} 里登记过的上传文件（data/uploads/**），返回已删路径列表

    必须在删除 data/{PROJECT} 目录之前调用——记录就存在那个目录里。
    只删 data/uploads 下的文件，越界路径一律跳过，防止误删。
    """
    removed = []
    data_root = Path(getattr(agent, "data_dir", None) or "data")
    proj_dir = data_root / project_name
    uploads_root = (data_root / "uploads").resolve()

    if not proj_dir.exists():
        return removed

    raw = set()
    for jf in sorted(proj_dir.glob("*.json")):
        try:
            _collect_paths(json.loads(jf.read_text(encoding="utf-8")), raw)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            continue

    cands = set()
    for v in raw:
        if v.startswith("/data/uploads/"):
            cands.add(data_root / v[len("/data/"):])
        elif v.startswith("/uploads/"):
            cands.add(data_root / v[len("/"):])
        else:
            cands.add(Path(v))

    touched_dirs = set()
    for p in cands:
        try:
            rp = p.resolve()
        except OSError:
            continue
        if uploads_root not in rp.parents and rp.parent != uploads_root:
            continue  # 只动 data/uploads 下的文件，越界跳过
        if rp.is_file():
            try:
                rp.unlink()
                removed.append(str(rp))
                touched_dirs.add(rp.parent)
                _log(f"   ✅ 已删除上传文件: {rp}")
            except OSError as e:
                _log(f"   ⚠️ 上传文件删除失败 {rp}: {e}")

    # 清掉因此变空的 uploads 子目录
    for d in touched_dirs:
        try:
            if d.exists() and d != uploads_root and not any(d.iterdir()):
                d.rmdir()
                _log(f"   ✅ 已删除空上传目录: {d}")
        except OSError:
            pass

    return removed


# ============================================================
# 主入口
# ============================================================

async def execute(envelop, agent):
    """删除已有工具（删除整个项目）"""
    # ★★★ 兼容两种传参方式 ★★★
    params = envelop.payload.get("params", {})

    if not params:
        params = {k: v for k, v in envelop.payload.items() if k != "action"}

    target = params.get("target", "")

    if not target:
        envelop.payload = {"ok": False, "error": "缺少 target 参数"}
        return envelop

    _log(f"🗑️ remove_tool: {target}")

    try:
        project_name = await _find_project(agent, target)
        if not project_name:
            envelop.payload = {"ok": False, "error": f"未找到项目: {target}"}
            return envelop

        # 项目名合法性校验：防止 '../x' 之类越权删除
        if (not project_name or "/" in project_name or "\\" in project_name
                or project_name in (".", "..")):
            envelop.payload = {"ok": False, "error": f"项目名非法: {project_name}"}
            return envelop

        data_root = Path(getattr(agent, "data_dir", None) or "data")
        app_dir = Path(f"plugins/applications/{project_name}")
        www_dir = Path(f"www/apps/{project_name}")
        data_www_dir = Path("data/www/{project_name}")   # 旧版前端数据目录
        data_dir = data_root / project_name

        if (not app_dir.exists() and not www_dir.exists()
                and not data_www_dir.exists() and not data_dir.exists()):
            envelop.payload = {"ok": False, "error": f"项目 {project_name} 不存在"}
            return envelop

        deleted = []

        # ① 先清上传文件：要读 data/{PROJECT} 里的记录，必须在删该目录之前
        uploads_removed = _cleanup_uploads(agent, project_name)
        deleted.extend(uploads_removed)

        # ② 删后端项目目录
        if app_dir.exists():
            shutil.rmtree(str(app_dir), ignore_errors=True)
            if app_dir.exists():
                _log(f"   ⚠️ 目录仍然存在（可能被占用）: {app_dir}")
            else:
                deleted.append(str(app_dir))
                _log(f"   ✅ 已删除: {app_dir}")

        # ③ 删前端目录 + 旧版 data/www + 应用数据目录（data/{PROJECT}）
        for extra_path in [www_dir, data_www_dir, data_dir]:
            if extra_path.exists():
                try:
                    shutil.rmtree(str(extra_path), ignore_errors=True)
                    if extra_path.exists():
                        _log(f"   ⚠️ 目录仍然存在（可能被占用）: {extra_path}")
                    else:
                        deleted.append(str(extra_path))
                        _log(f"   ✅ 已删除: {extra_path}")
                except Exception as e:
                    _log(f"   ⚠️ 删除失败 {extra_path}: {e}")

        # 删除契约文件
        for contract_file in Path("plugins/applications").glob(f"{project_name}/*.contract.json"):
            if contract_file.exists():
                contract_file.unlink()
                deleted.append(str(contract_file))
                _log(f"   ✅ 已删除契约: {contract_file}")

        if deleted:
            envelop.payload = {
                "ok": True,
                "message": f"🗑️ 已删除项目 `{project_name}`\n\n共删除 {len(deleted)} 个文件/目录"
            }
        else:
            envelop.payload = {"ok": False, "error": f"未能删除项目 {project_name}"}

        return envelop

    except Exception as e:
        _log(f"remove_tool 异常: {e}")
        envelop.payload = {"ok": False, "error": f"删除失败: {e}"}
        return envelop


def help():
    return {
        "route": "builtins/tools/remove_apps",
        "description": (
            "删除整个项目：后端目录 plugins/applications/{project}、前端 www/apps/{project}、"
            "旧版 data/www/{project}、应用数据目录 data/{project}，并清理数据里登记过的"
            "上传文件（仅限 data/uploads 下，越界路径跳过）"
        ),
        "input": {
            "target": "项目名、插件名、或路径；只剩 data/{project} 残留时也能按项目名删掉"
        },
        "output": {
            "ok": "是否成功",
            "message": "结果消息（含删除数量）"
        }
    }
