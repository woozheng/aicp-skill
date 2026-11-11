"""所有路径的唯一来源。以后加功能只改这个文件。"""
from pathlib import Path

DATA_ROOT = Path("data")
MEMORY_ROOT = DATA_ROOT / "memories"


# ---- 用户级 ----
def user_dir(uid: str) -> Path:
    p = MEMORY_ROOT / "users" / uid
    p.mkdir(parents=True, exist_ok=True)
    return p


def user_profile_file(uid: str) -> Path:
    return user_dir(uid) / "profile.json"


def user_index_file(uid: str) -> Path:
    return user_dir(uid) / "index.json"


def user_agents_dir(uid: str) -> Path:
    p = user_dir(uid) / "agents"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---- agent 级 ----
def agent_dir(uid: str, agent_id: str) -> Path:
    p = user_dir(uid) / agent_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def agent_backpack_file(uid: str, agent_id: str) -> Path:
    return agent_dir(uid, agent_id) / "backpack.json"


# ---- 全局共享经验 ----
def experiences_dir() -> Path:
    """经验背包根目录：所有 user / agent / session 共享。"""
    p = DATA_ROOT / "experiences"
    p.mkdir(parents=True, exist_ok=True)
    return p


def experiences_file() -> Path:
    return experiences_dir() / "backpack.json"


def experiences_history_dir() -> Path:
    p = experiences_dir() / "history"
    p.mkdir(parents=True, exist_ok=True)
    return p


def agent_sessions_dir(uid: str, agent_id: str) -> Path:
    p = agent_dir(uid, agent_id) / "sessions"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---- session 级 ----
def session_dir(uid: str, agent_id: str, sid: str) -> Path:
    p = agent_sessions_dir(uid, agent_id) / sid
    p.mkdir(parents=True, exist_ok=True)
    return p


def session_flow_file(uid: str, agent_id: str, sid: str) -> Path:
    return session_dir(uid, agent_id, sid) / "flow.jsonl"


def session_taskboard_file(uid: str, agent_id: str, sid: str) -> Path:
    return session_dir(uid, agent_id, sid) / "taskboard.txt"


def session_tasks_file(uid: str, agent_id: str, sid: str) -> Path:
    return session_dir(uid, agent_id, sid) / "tasks.json"


def session_debug_dir(uid: str, agent_id: str, sid: str) -> Path:
    p = session_dir(uid, agent_id, sid) / "_debug_context"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---- 系统级 ----
def system_agents_dir() -> Path:
    p = DATA_ROOT / "agents"
    p.mkdir(parents=True, exist_ok=True)
    return p


def system_agent_md_file(agent_id: str) -> Path:
    return system_agents_dir() / f"{agent_id}.md"


def skills_dir() -> Path:
    p = DATA_ROOT / "skills"
    p.mkdir(parents=True, exist_ok=True)
    return p


def uploads_dir(sid: str = None) -> Path:
    p = DATA_ROOT / "uploads" / (sid or "")
    p.mkdir(parents=True, exist_ok=True)
    return p


def workspace_dir(sid: str = None) -> Path:
    p = DATA_ROOT / "workspace" / (sid or "")
    p.mkdir(parents=True, exist_ok=True)
    return p


def llm_usage_dir() -> Path:
    p = DATA_ROOT / "llm_usage"
    p.mkdir(parents=True, exist_ok=True)
    return p
