"""Agent 池 —— 内置 agent（data/agents/）+ 用户自定义（users/<uid>/agents/）。"""
from pathlib import Path
from typing import Dict, List, Optional

from .paths import system_agents_dir, user_agents_dir


def _parse_frontmatter(text: str):
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm_raw = text[3:end].strip()
    body = text[end + 4:].lstrip("\n")
    meta = {}
    try:
        import yaml
        meta = yaml.safe_load(fm_raw) or {}
    except Exception:
        for line in fm_raw.splitlines():
            if ":" in line and not line.strip().startswith("#"):
                k, v = line.split(":", 1)
                v = v.strip().strip("\"'")
                if v.startswith("[") and v.endswith("]"):
                    v = [x.strip().strip("\"'") for x in v[1:-1].split(",") if x.strip()]
                elif v.lower() in ("true", "false"):
                    v = v.lower() == "true"
                elif v.isdigit():
                    v = int(v)
                meta[k.strip()] = v
    return meta, body


class AgentPool:
    def __init__(self):
        self._system_agents: Dict[str, dict] = {}
        self._user_agents: Dict[str, Dict[str, dict]] = {}
        self.reload_system()

    def _load_md(self, md: Path, source: str = "system") -> Optional[dict]:
        try:
            meta, body = _parse_frontmatter(md.read_text(encoding="utf-8"))
            agent_id = meta.get("id") or md.stem
            cfg = {
                "route": f"builtins/agents/engine#{agent_id}",
                "agent_id": agent_id,
                "system_prompt": {"inline": body},
                "tools": meta.get("tools", ["reply", "use_tool"]),
                "internal_tools": meta.get("internal_tools", []),
                "max_depth": meta.get("max_depth", 40),
                "pressure_threshold": meta.get("pressure_threshold", 300_000),
                "narration_check": meta.get("narration_check", True),
                "tail": meta.get("tail", ["datetime"]),
                "system_actions": meta.get("system_actions",
                                            ["whoami", "get_session", "interrupt"]),
            }
            return {
                "meta": {
                    "id": agent_id,
                    "name": meta.get("name", agent_id),
                    "description": meta.get("description", ""),
                    "tools": cfg["tools"] + cfg.get("internal_tools", []),
                    "max_depth": cfg["max_depth"],
                    "source": source,
                    "default": bool(meta.get("default", False)),
                },
                "config": cfg,
                "execute": None,
            }
        except Exception as e:
            print(f"[pool][WARN] 加载 {md} 失败: {e}")
            return None

    def reload_system(self):
        self._system_agents.clear()
        d = system_agents_dir()
        if not d.exists():
            return
        for md in sorted(d.glob("*.md")):
            entry = self._load_md(md, source="system")
            if entry:
                self._system_agents[entry["meta"]["id"]] = entry

    def load_user(self, uid: str, force: bool = False):
        if uid in self._user_agents and not force:
            return
        d = user_agents_dir(uid)
        if not d.exists():
            self._user_agents[uid] = {}
            return
        agents = {}
        for md in sorted(d.glob("*.md")):
            entry = self._load_md(md, source="user")
            if entry:
                agents[entry["meta"]["id"]] = entry
        self._user_agents[uid] = agents

    def list(self, uid: str = None) -> List[dict]:
        result = {aid: e["meta"] for aid, e in self._system_agents.items()}
        if uid:
            self.load_user(uid)
            for aid, e in self._user_agents.get(uid, {}).items():
                result[aid] = e["meta"]
        return list(result.values())

    def get(self, agent_id: str, uid: str = None) -> Optional[dict]:
        if uid:
            self.load_user(uid)
            if agent_id in self._user_agents.get(uid, {}):
                return self._user_agents[uid][agent_id]
        return self._system_agents.get(agent_id)

    def default_id(self, uid: str = None) -> str:
        for e in self._system_agents.values():
            if e["meta"].get("default"):
                return e["meta"]["id"]
        if self._system_agents:
            return next(iter(self._system_agents))
        return ""

    def reload_all(self, uid: str = None):
        self.reload_system()
        if uid:
            self._user_agents.pop(uid, None)
            self.load_user(uid)

    def set_execute(self, agent_id: str, execute, uid: str = None):
        if uid and agent_id in self._user_agents.get(uid, {}):
            self._user_agents[uid][agent_id]["execute"] = execute
        elif agent_id in self._system_agents:
            self._system_agents[agent_id]["execute"] = execute