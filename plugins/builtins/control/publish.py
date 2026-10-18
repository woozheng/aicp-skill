"""一键发布应用到商店 — 自动创建仓库 + 推送 + 更新索引"""
import json
import subprocess
import shutil
import tempfile
import os
from pathlib import Path
import aiohttp


async def execute(envelop, agent):
    action = envelop.payload.get("action", "publish")
    
    if action == "publish":
        return await _publish(envelop, agent)
    else:
        envelop.payload = {"error": f"Unknown action: {action}"}
        return envelop


async def _publish(envelop, agent):
    app_id = envelop.payload.get("app_id", "")
    platform = envelop.payload.get("platform", "gitea")
    org = envelop.payload.get("org", "aicp-store")
    token = envelop.payload.get("token", "")
    author = envelop.payload.get("author", "AICP")
    description = envelop.payload.get("description", "")
    category = envelop.payload.get("category", "tools")
    version = envelop.payload.get("version", "1.0.0")

    if not app_id:
        envelop.payload = {"error": "Missing app_id"}
        return envelop
    if not token:
        envelop.payload = {"error": "Missing token — 请在设置中配置 GitHub/Gitea Token"}
        return envelop

    plugin_dir = Path(f"plugins/applications/{app_id}")
    www_dir = Path(f"www/{app_id}")

    if not plugin_dir.exists() and not www_dir.exists():
        envelop.payload = {"error": f"App '{app_id}' not found"}
        return envelop

    # 读取 app.yaml
    app_yaml = plugin_dir / "app.yaml"
    app_name = app_id
    if app_yaml.exists():
        try:
            import yaml
            with open(app_yaml, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            app_name = cfg.get("name", app_id)
            if not description: description = cfg.get("description", "")
            if not version or version == "1.0.0": version = cfg.get("version", "1.0.0")
        except Exception:
            pass

    # 自动创建远程仓库
    repo_url = await _create_remote_repo(platform, org, app_id, token)
    if not repo_url:
        envelop.payload = {"error": "Failed to create remote repository"}
        return envelop

    agent.log.info(f"[Publish] Created repo: {repo_url}")

    # 临时目录打包
    tmp_dir = Path(tempfile.mkdtemp())
    pkg_dir = tmp_dir / app_id

    try:
        if plugin_dir.exists():
            shutil.copytree(plugin_dir, pkg_dir / "plugins" / "applications" / app_id)
        if www_dir.exists():
            shutil.copytree(www_dir, pkg_dir / "www" / app_id)

        readme = f"# {app_name}\n\n{description}\n\n## 安装\n在 AICP 商店中搜索 `{app_id}` 点击安装。\n"
        (pkg_dir / "README.md").write_text(readme, encoding="utf-8")

        # Git 推送
        remote_url = _get_remote_url(platform, org, app_id)
        if not _git_push(pkg_dir, remote_url, token):
            envelop.payload = {"error": "Git push failed"}
            return envelop

        agent.log.info(f"[Publish] Pushed to {remote_url}")

        # 更新本地索引
        _update_local_index(app_id, app_name, description, category, author, version, repo_url)

        envelop.payload = {
            "ok": True,
            "app_id": app_id,
            "name": app_name,
            "repo_url": repo_url,
            "message": "发布成功！"
        }

    except Exception as e:
        envelop.payload = {"error": f"Publish failed: {str(e)}"}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return envelop


async def _create_remote_repo(platform, org, repo_name, token):
    """自动创建远程仓库，返回 HTML URL"""
    if platform == "github":
        api_url = f"https://api.github.com/orgs/{org}/repos"
    elif platform == "gitea":
        api_url = f"https://git.biopoiesis.net/api/v1/orgs/{org}/repos"
    else:
        return None

    headers = {
        "Authorization": f"token {token}",
        "Content-Type": "application/json",
    }
    data = {
        "name": repo_name,
        "private": False,
        "auto_init": False,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(api_url, headers=headers, json=data, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status in (201, 200):
                    result = await resp.json()
                    return result.get("html_url") or result.get("clone_url")

                # 仓库可能已存在，尝试获取
                if platform == "github":
                    get_url = f"https://api.github.com/repos/{org}/{repo_name}"
                else:
                    get_url = f"https://git.biopoiesis.net/api/v1/repos/{org}/{repo_name}"

                async with session.get(get_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as get_resp:
                    if get_resp.status == 200:
                        result = await get_resp.json()
                        return result.get("html_url") or result.get("clone_url")
    except Exception:
        pass

    return None


def _get_remote_url(platform, org, repo_name):
    """获取远程仓库 URL"""
    if platform == "github":
        return f"https://github.com/{org}/{repo_name}.git"
    elif platform == "gitea":
        return f"https://git.biopoiesis.net/{org}/{repo_name}.git"
    return ""


def _git_push(repo_dir, remote_url, token):
    """初始化 Git 仓库并推送，用环境变量传 Token"""
    try:
        subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "add", "."], cwd=repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "Publish via AICP"], cwd=repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "remote", "add", "origin", remote_url], cwd=repo_dir, capture_output=True)

        # 用 x-access-token 认证
        if "github.com" in remote_url:
            push_url = remote_url.replace("https://", f"https://x-access-token:{token}@")
        else:
            push_url = remote_url.replace("https://", f"https://{token}@")

        result = subprocess.run(
            ["git", "push", "-u", "origin", "main", "--force"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        )
        
        if result.returncode == 0:
            return True
        
        # 如果 main 失败，尝试 master
        result = subprocess.run(
            ["git", "branch", "-M", "main"],
            cwd=repo_dir,
            capture_output=True
        )
        result = subprocess.run(
            ["git", "push", "-u", "origin", "main", "--force"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        )
        
        return result.returncode == 0

    except Exception as e:
        print(f"[Publish] Git error: {e}")
        return False


def _update_local_index(app_id, name, description, category, author, version, git_url):
    """更新本地 apps.json 索引"""
    index_path = Path("data/apps.json")
    index = []
    if index_path.exists():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception:
            index = []

    for app in index:
        if app.get("id") == app_id:
            app.update({"name": name, "description": description, "category": category, "author": author, "version": version, "git_url": git_url})
            break
    else:
        index.append({
            "id": app_id, "name": name, "description": description, "category": category,
            "author": author, "version": version, "git_url": git_url, "type": "single",
        })

    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")