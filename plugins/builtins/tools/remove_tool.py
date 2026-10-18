"""remove_tool — 删除已有工具/应用（完全独立）"""

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

        app_dir = Path(f"plugins/applications/{project_name}")
        www_dir = Path(f"www/{project_name}")
        data_www_dir = Path(f"data/www/{project_name}")

        if not app_dir.exists() and not www_dir.exists() and not data_www_dir.exists():
            envelop.payload = {"ok": False, "error": f"项目 {project_name} 不存在"}
            return envelop

        deleted = []

        # 删除整个项目
        if app_dir.exists():
            shutil.rmtree(str(app_dir), ignore_errors=True)
            deleted.append(str(app_dir))
            _log(f"   ✅ 已删除: {app_dir}")

        for www_path in [www_dir, data_www_dir]:
            if www_path.exists():
                try:
                    shutil.rmtree(str(www_path), ignore_errors=True)
                    deleted.append(str(www_path))
                    _log(f"   ✅ 已删除: {www_path}")
                except Exception as e:
                    _log(f"   ⚠️ 删除失败 {www_path}: {e}")

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
        "route": "builtins/tools/remove_tool",
        "description": "删除整个项目（包括前后端）",
        "input": {
            "target": "项目名、插件名、或路径"
        },
        "output": {
            "ok": "是否成功",
            "message": "结果消息"
        }
    }