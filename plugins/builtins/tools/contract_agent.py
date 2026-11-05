"""
契约智能体 — 专用 LLM，只做一件事：读源码，提取契约

- 有 .contract.json 且 source_hash 一致：直接返回（落盘缓存）
- 无契约 或 hash 不一致：读源码 → LLM 输出 LLC 行式紧凑契约 → 解析成标准 JSON → 保存
- LLM 提取失败：返回 fallback（正则粗扫 action 名单）
- 返回：契约（不含源码片段）
- get_tools：返回 Function Calling tools schema
- 外部来源：mcp/server/tool → 读 _mcp_client 契约；skill/xxx → 读 data/skills 文档
"""

import ast
import json
import re
import hashlib
from pathlib import Path

import core


# ============================================================
# LLC 格式说明
# ============================================================

LLC_FORMAT = """
P|插件名
A|action名|一句话描述|参数1:类型:必填性,参数2:类型:必填性|返回1:类型,返回2:类型

类型短码：str / int / num / bool / arr / obj / any
必填性：req / opt
desc 不超过 20 字，不含 | 和 ,
参数或返回为空就留空：A|list|列全部||
每个 action 一行，不要多行
不要输出 ``` 或任何 markdown
"""


CONTRACT_EXTRACTOR_PROMPT = """你是一个契约提取专家。读插件源码，提取契约。

## 输出格式：LLC 行式紧凑契约

只输出下面这种格式，不要 JSON、不要缩进、不要解释：

P|插件名
A|action名|一句话描述|参数1:类型:必填性,参数2:类型:必填性|返回1:类型,返回2:类型

规则：
- 类型用短码：str/int/num/bool/arr/obj/any
- 必填性用 req/opt
- desc 不超过 20 字，不含 | 和 ,
- 参数或返回为空就留空：A|list|列全部||
- 每个 action 一行，不要多行
- 不要输出 ``` 或任何 markdown

## 提取规则

1. 定位 async def execute(envelop, agent):，扫 action 分发（if/elif action == 'xxx'、match action: case 'zzz'、字典路由）
2. 每个 action 的参数：扫 envelop.payload 的引用，payload['x'] 必填、payload.get('x') 非必填、payload.get('x', 默认) 非必填
3. 如果参数在 execute 顶部统一取（path = payload.get("path")），也要归到用到它的 action
4. 参数在 helper 函数里也要提取
5. 返回字段：扫 return envelop 之前的 envelop.payload = {...} 顶层 key，只取顶层，不展开嵌套
6. 不确定的类型标 any，不确定的必填性标 opt

## 示例

P|applications/demo/demo
A|list|列全部||ok:bool,data:obj
A|add|新增一条|text:str:req,x:int:opt,y:int:opt|ok:bool,data:obj

只输出 LLC，不要任何其他文字。
"""


# ============================================================
# LLC → 标准契约解析
# ============================================================

_TYPE_EXPAND = {
    "str": "string",
    "int": "integer",
    "num": "number",
    "bool": "boolean",
    "arr": "array",
    "obj": "object",
    "any": "string",
}


def _parse_fields(s: str) -> list:
    """解析 `name:type:req,name2:type:opt` → [{name,type,required}, ...]"""
    if not s or not s.strip():
        return []
    out = []
    for item in s.split(","):
        item = item.strip()
        if not item:
            continue
        seg = item.split(":")
        name = seg[0].strip()
        if not name:
            continue
        typ = seg[1].strip() if len(seg) > 1 else "any"
        req = (seg[2].strip() == "req") if len(seg) > 2 else False
        out.append({
            "name": name,
            "type": _TYPE_EXPAND.get(typ, "string"),
            "required": req,
            "desc": "",
        })
    return out


def parse_llc(text: str) -> dict:
    """LLC 文本 → 标准契约 dict。解析失败返回空 actions。"""
    contract = {"plugin": "", "actions": {}}
    if not text:
        return contract

    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("```"):
            continue
        parts = line.split("|")
        if parts[0] == "P" and len(parts) >= 2:
            contract["plugin"] = parts[1].strip()
        elif parts[0] == "A" and len(parts) >= 5:
            action = parts[1].strip()
            if not action:
                continue
            desc = parts[2].strip()
            params = _parse_fields(parts[3])
            returns = _parse_fields(parts[4])
            contract["actions"][action] = {
                "description": desc,
                "params": params,
                "returns": returns,
            }

    return contract


# ============================================================
# 正则校验
# ============================================================

_ACTION_REGEXES = [
    re.compile(r'(?:if|elif)\s+action\s*==\s*["\']([\w\-]+)["\']'),
    re.compile(r'case\s+["\']([\w\-]+)["\']\s*:'),
]


def _scan_source_actions(source_code: str) -> set:
    found = set()
    for rx in _ACTION_REGEXES:
        for m in rx.finditer(source_code):
            found.add(m.group(1))
    return found


def _validate_against_source(contract: dict, source_code: str) -> dict:
    src_actions = _scan_source_actions(source_code)
    llm_actions = set(contract.get("actions", {}).keys())

    for name in src_actions - llm_actions:
        contract["actions"][name] = {
            "description": "(正则扫到但 LLM 未提取)",
            "params": [],
            "returns": [],
            "confidence": "low",
        }

    for name in contract["actions"]:
        if name in src_actions:
            contract["actions"][name]["confidence"] = "high"
        else:
            contract["actions"][name].setdefault("confidence", "low")

    return contract


# ============================================================
# 契约 → Function Calling tools
# ============================================================

_TYPE_MAP = {
    "string": "string",
    "number": "number",
    "integer": "number",
    "boolean": "boolean",
    "array": "array",
    "object": "object",
}


def contract_to_tools(plugin_name: str, contract: dict) -> list:
    tools = []
    actions = contract.get("actions", {})

    for action_name, action_def in actions.items():
        properties = {}
        required = []

        for param in action_def.get("params", []):
            param_name = param.get("name", "")
            if not param_name:
                continue
            param_type = _TYPE_MAP.get(param.get("type", "string"), "string")
            prop = {"type": param_type, "description": param.get("desc", "")}
            if param_type == "array":
                prop["items"] = {"type": "string"}
            properties[param_name] = prop
            if param.get("required", False):
                required.append(param_name)

        tools.append({
            "type": "function",
            "function": {
                "name": f"{plugin_name}::{action_name}",
                "description": action_def.get("description", ""),
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                }
            }
        })

    return tools


# ============================================================
# 源码 hash
# ============================================================

def calc_source_hash(source_code: str) -> str:
    return hashlib.md5(source_code.encode("utf-8")).hexdigest()


# ============================================================
# 源码裁剪
# ============================================================

def _trim_source(source_code: str) -> str:
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return source_code

    func_defs = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_defs[node.name] = node

    if "execute" not in func_defs:
        return source_code

    def _collect_calls(func_node, collected=None):
        if collected is None:
            collected = set()
        for node in ast.walk(func_node):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    name = node.func.id
                    if name in func_defs and name not in collected:
                        collected.add(name)
                        _collect_calls(func_defs[name], collected)
        return collected

    called_helpers = _collect_calls(func_defs["execute"])

    lines = source_code.split("\n")
    exec_node = func_defs["execute"]
    exec_start = exec_node.lineno - 1
    exec_end = exec_node.end_lineno

    pre_lines = []
    for i in range(exec_start):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("def ") or stripped.startswith("async def "):
            continue
        pre_lines.append(line)

    exec_lines = lines[exec_start:exec_end]

    helper_lines = []
    for name in sorted(called_helpers, key=lambda n: func_defs[n].lineno):
        node = func_defs[name]
        helper_lines.extend(lines[node.lineno - 1:node.end_lineno])

    return "\n".join(pre_lines + exec_lines + helper_lines)


# ============================================================
# Fallback 契约
# ============================================================

def build_fallback_contract(plugin_name: str, source_code: str) -> dict:
    actions = {}
    for rx in _ACTION_REGEXES:
        for m in rx.finditer(source_code):
            name = m.group(1)
            if name and name not in actions:
                actions[name] = {
                    "description": "(fallback: LLM extraction failed)",
                    "params": [],
                    "returns": [],
                    "confidence": "low",
                    "notes": "此契约由正则粗扫生成，可能不完整。建议修复后 refresh。",
                }
    return {
        "plugin": plugin_name,
        "actions": actions,
        "_fallback": True,
        "_fallback_note": "LLM 提取失败，此契约由正则粗扫生成，action 名单可能不全，参数和返回值缺失。建议检查插件源码后调用 refresh。",
    }


# ============================================================
# L1: Schema 字典直接提取
# ============================================================

_SCHEMA_DICT_NAMES = ("ACTIONS_SCHEMA", "API_SCHEMA", "SCHEMA", "ACTION_SCHEMA")


def _try_extract_schema_dict(source_code: str) -> dict | None:
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return None

    schema_node = None
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in _SCHEMA_DICT_NAMES:
                    schema_node = node.value
                    break
            if schema_node:
                break

    if schema_node is None or not isinstance(schema_node, ast.Dict):
        return None

    actions = {}
    for key_node, val_node in zip(schema_node.keys, schema_node.values):
        if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
            continue
        action_name = key_node.value
        if not isinstance(val_node, ast.Dict):
            continue

        action_def = {"description": "", "params": [], "confidence": "high"}
        required_set = set()
        params_dict = {}

        for akey, aval in zip(val_node.keys, val_node.values):
            if not isinstance(akey, ast.Constant):
                continue
            k = akey.value
            if k == "description" and isinstance(aval, ast.Constant):
                action_def["description"] = aval.value
            elif k == "required" and isinstance(aval, (ast.List, ast.Tuple)):
                for elt in aval.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        required_set.add(elt.value)
            elif k == "params" and isinstance(aval, ast.Dict):
                for pkey, pval in zip(aval.keys, aval.values):
                    if not isinstance(pkey, ast.Constant) or not isinstance(pkey.value, str):
                        continue
                    pname = pkey.value
                    ptype = "string"
                    pdesc = ""
                    if isinstance(pval, ast.Dict):
                        for pk, pv in zip(pval.keys, pval.values):
                            if not isinstance(pk, ast.Constant):
                                continue
                            if pk.value == "type" and isinstance(pv, ast.Constant):
                                ptype = pv.value
                            elif pk.value == "description" and isinstance(pv, ast.Constant):
                                pdesc = pv.value
                    params_dict[pname] = {"type": ptype, "desc": pdesc}

        for pname, pinfo in params_dict.items():
            action_def["params"].append({
                "name": pname,
                "type": pinfo["type"],
                "required": pname in required_set,
                "desc": pinfo["desc"],
            })

        actions[action_name] = action_def

    if not actions:
        return None

    return {"actions": actions, "_source": "schema_dict"}


# ============================================================
# LLM 提取契约
# ============================================================

async def _call_llm_extract(agent, plugin_name: str, source_code: str) -> dict:
    schema_contract = _try_extract_schema_dict(source_code)
    if schema_contract:
        schema_contract.setdefault("plugin", plugin_name)
        return {"ok": True, "contract": schema_contract, "source": "schema_dict"}

    trimmed = _trim_source(source_code)
    prompt = (
        CONTRACT_EXTRACTOR_PROMPT
        + "\n\n## 插件源码\n\n插件路径：" + plugin_name
        + "\n\n" + trimmed
        + "\n\n请按 LLC 格式输出契约，不要任何其他文字。"
    )

    try:
        llm_output = await asyncio.wait_for(
            agent.llm.chat([{"role": "user", "content": prompt}], role="code"),
            timeout=120.0,   # 契约提取给 2 分钟
        )
        if not llm_output:
            return {"ok": False, "error": "LLM 返回为空"}

        raw = llm_output.strip()
        raw = re.sub(r'^```[a-zA-Z]*\n', '', raw)
        raw = re.sub(r'\n```\s*$', '', raw)
        raw = raw.strip()

        contract = parse_llc(raw)

        if not contract.get("actions"):
            return {
                "ok": False,
                "error": "LLC 解析后 actions 为空",
                "raw": llm_output[:500],
            }

        contract.setdefault("plugin", plugin_name)
        if not contract.get("plugin"):
            contract["plugin"] = plugin_name

        contract = _validate_against_source(contract, source_code)

        return {"ok": True, "contract": contract}

    except Exception as e:
        return {"ok": False, "error": f"LLM 调用失败: {e}"}


# ============================================================
# 外部来源：MCP 契约 / 本地 skill
# ============================================================

def _schema_to_params(schema: dict) -> list:
    """JSON Schema → 契约 params 格式。"""
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    out = []
    for name, prop in props.items():
        if not isinstance(prop, dict):
            prop = {}
        out.append({
            "name": name,
            "type": prop.get("type", "string"),
            "required": name in required,
            "desc": prop.get("description", ""),
        })
    return out


async def _handle_mcp_contract(plugin_name: str, action: str, envelop) -> object:
    """mcp/server/tool → 读 data/os/_mcp_client/contracts/<server>.<tool>.json"""
    parts = plugin_name.split("/")
    if len(parts) < 3:
        envelop.payload = {"ok": False, "error": f"mcp 契约格式: mcp/server/tool，收到: {plugin_name}"}
        return envelop
    server, tool = parts[1], parts[2]
    f = Path("data/os/_mcp_client/contracts") / f"{server}.{tool}.json"
    if not f.exists():
        envelop.payload = {"ok": False, "error": f"MCP 契约不存在: {f}"}
        return envelop
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        envelop.payload = {"ok": False, "error": f"读取 MCP 契约失败: {e}"}
        return envelop

    desc = data.get("description", "") or ""
    schema = data.get("inputSchema", {}) or {}

    if action == "get":
        envelop.payload = {
            "ok": True,
            "plugin": plugin_name,
            "contract": {
                "plugin": plugin_name,
                "actions": {
                    tool: {
                        "description": desc,
                        "params": _schema_to_params(schema),
                        "returns": [],
                    }
                },
                "source_hash": data.get("fetched_at", ""),
            },
            "usage": desc,
            "usage_file": str(f),
            "usage_file_size": len(desc),
            "source": "mcp_contract",
            "contract_file": str(f),
        }
        return envelop

    if action == "get_tools":
        envelop.payload = {
            "ok": True,
            "plugin": plugin_name,
            "tools": [{
                "type": "function",
                "function": {
                    "name": plugin_name,
                    "description": desc,
                    "parameters": schema,
                }
            }],
            "usage": desc,
            "usage_file": str(f),
            "usage_file_size": len(desc),
            "source": "mcp_contract",
        }
        return envelop

    if action == "get_execute_only":
        envelop.payload = {
            "ok": True,
            "plugin": plugin_name,
            "source_snippet": json.dumps(data, ensure_ascii=False, indent=2),
        }
        return envelop

    envelop.payload = {"ok": False, "error": f"MCP 契约不支持 action: {action}"}
    return envelop


async def _handle_skill_contract(plugin_name: str, action: str, envelop, agent) -> object:
    """skill/xxx → 读 data/skills/xxx/SKILL.md（不再支持 skill/plugin/...）"""
    rel = plugin_name[len("skill/"):].strip("/")

    if rel.endswith("SKILL.md"):
        f = Path("data/skills") / rel
    else:
        f = Path("data/skills") / rel / "SKILL.md"
    if not f.exists():
        alt = Path("data/skills/aicp") / (rel if rel.endswith("SKILL.md") else rel + "/SKILL.md")
        if alt.exists():
            f = alt
    if not f.exists():
        envelop.payload = {"ok": False, "error": f"skill 文件不存在: {f}"}
        return envelop

    try:
        text = f.read_text(encoding="utf-8")
    except Exception as e:
        envelop.payload = {"ok": False, "error": f"读取 skill 失败: {e}"}
        return envelop

    meta = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                if ":" in line and not line.strip().startswith("#"):
                    k, v = line.split(":", 1)
                    meta[k.strip().lower()] = v.strip().strip("\"'")
            body = text[end + 4:].strip()

    desc = meta.get("description") or meta.get("title") or meta.get("name") or ""

    if action == "get":
        envelop.payload = {
            "ok": True,
            "plugin": plugin_name,
            "contract": {
                "plugin": plugin_name,
                "actions": {
                    "use": {"description": desc, "params": [], "returns": []}
                },
                "source_hash": calc_source_hash(text),
            },
            "usage": body,
            "usage_file": str(f),
            "usage_file_size": len(body),
            "source": "local_skill",
            "contract_file": str(f),
        }
        return envelop

    if action == "get_tools":
        envelop.payload = {
            "ok": True,
            "plugin": plugin_name,
            "tools": [{
                "type": "function",
                "function": {
                    "name": plugin_name,
                    "description": desc,
                    "parameters": {"type": "object", "properties": {}, "required": []},
                }
            }],
            "usage": body,
            "usage_file": str(f),
            "usage_file_size": len(body),
            "source": "local_skill",
        }
        return envelop

    if action == "get_execute_only":
        envelop.payload = {
            "ok": True,
            "plugin": plugin_name,
            "source_snippet": body,
        }
        return envelop

    envelop.payload = {"ok": False, "error": f"skill 不支持 action: {action}"}
    return envelop


# ============================================================
# 主入口
# ============================================================

async def execute(envelop, agent):

    action = envelop.payload.get("action", "get")
    plugin_name = envelop.payload.get("plugin", "").strip()

    if not plugin_name:
        envelop.payload = {"ok": False, "error": "缺少 plugin 参数"}
        return envelop

    # 外部来源分流
    if plugin_name.startswith("mcp/"):
        return await _handle_mcp_contract(plugin_name, action, envelop)
    if plugin_name.startswith("skill/"):
        return await _handle_skill_contract(plugin_name, action, envelop, agent)

    # ---- 内部插件 ----

    if "/" not in plugin_name:
        matches = list(Path("plugins").rglob(f"{plugin_name}.py"))
        if len(matches) == 1:
            rel = matches[0].relative_to("plugins")
            plugin_name = str(rel.with_suffix("")).replace("\\", "/")
        elif len(matches) > 1:
            envelop.payload = {
                "ok": False,
                "error": f"插件名 {plugin_name} 有多个匹配，请用完整路径",
                "matches": [str(m).replace("\\", "/") for m in matches],
            }
            return envelop

    if plugin_name.endswith(".py"):
        plugin_name = plugin_name[:-3]
    elif plugin_name.endswith(".ts"):
        plugin_name = plugin_name[:-3]

    plugin_file = Path("plugins") / f"{plugin_name}.py"
    contract_file = Path("plugins") / f"{plugin_name}.contract.json"

    # ---------------- get ----------------
    if action == "get":
        if not plugin_file.exists():
            envelop.payload = {"ok": False, "error": f"插件源码不存在: {plugin_file}"}
            return envelop

        try:
            source_code = plugin_file.read_text(encoding="utf-8")
        except Exception as e:
            envelop.payload = {"ok": False, "error": f"读取源码失败: {e}"}
            return envelop

        current_hash = calc_source_hash(source_code)

        if contract_file.exists():
            try:
                contract = json.loads(contract_file.read_text(encoding="utf-8"))
                if contract.get("source_hash") == current_hash:
                    envelop.payload = {
                        "ok": True,
                        "plugin": plugin_name,
                        "contract": contract,
                        "source": "existing_contract",
                        "contract_file": str(contract_file),
                    }
                    return envelop
                else:
                    print(f"[contract_agent] source changed, re-extracting: {plugin_name}")
            except Exception:
                pass

        result = await _call_llm_extract(agent, plugin_name, source_code)

        if result.get("ok"):
            result["contract"]["source_hash"] = current_hash
            try:
                contract_file.write_text(
                    json.dumps(result["contract"], ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
                result["contract_saved"] = str(contract_file)
            except Exception as e:
                result["contract_saved"] = f"保存失败: {e}"
            result["source"] = "llm_extraction"
        else:
            result["source"] = "extraction_failed"
            result["fallback"] = build_fallback_contract(plugin_name, source_code)

        result["plugin"] = plugin_name
        envelop.payload = result
        return envelop

    # ---------------- get_tools ----------------
    elif action == "get_tools":
        if not plugin_file.exists():
            envelop.payload = {"ok": False, "error": f"插件源码不存在: {plugin_file}"}
            return envelop

        try:
            source_code = plugin_file.read_text(encoding="utf-8")
        except Exception as e:
            envelop.payload = {"ok": False, "error": f"读取源码失败: {e}"}
            return envelop

        current_hash = calc_source_hash(source_code)
        contract = None
        source = ""

        if contract_file.exists():
            try:
                existing = json.loads(contract_file.read_text(encoding="utf-8"))
                if existing.get("source_hash") == current_hash:
                    contract = existing
                    source = "existing_contract"
            except Exception:
                contract = None

        if contract is None:
            result = await _call_llm_extract(agent, plugin_name, source_code)

            if not result.get("ok"):
                fallback = build_fallback_contract(plugin_name, source_code)
                envelop.payload = {
                    "ok": True,
                    "plugin": plugin_name,
                    "tools": contract_to_tools(plugin_name, fallback),
                    "source": "fallback",
                    "extraction_error": result.get("error"),
                }
                return envelop

            result["contract"]["source_hash"] = current_hash
            contract = result["contract"]
            source = "llm_extraction"

            try:
                contract_file.write_text(
                    json.dumps(contract, ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
            except Exception:
                pass

        tools = contract_to_tools(plugin_name, contract)

        envelop.payload = {
            "ok": True,
            "plugin": plugin_name,
            "tools": tools,
            "source": source,
            "contract_file": str(contract_file),
        }
        return envelop

    # ---------------- get_execute_only ----------------
    elif action == "get_execute_only":
        if not plugin_file.exists():
            envelop.payload = {"ok": False, "error": f"插件源码不存在: {plugin_file}"}
            return envelop

        source_code = plugin_file.read_text(encoding="utf-8")
        lines = source_code.split('\n')
        execute_start = -1
        execute_end = -1

        for i, line in enumerate(lines):
            stripped = line.strip()
            if execute_start == -1 and ('async def execute' in stripped or 'def execute' in stripped):
                execute_start = i
                continue
            if execute_start != -1:
                if stripped.startswith('async def ') or stripped.startswith('def ') or stripped.startswith('class '):
                    if 'def execute' not in stripped:
                        execute_end = i
                        break

        if execute_start == -1:
            execute_code = source_code
        elif execute_end == -1:
            execute_code = '\n'.join(lines[execute_start:])
        else:
            execute_code = '\n'.join(lines[execute_start:execute_end])

        envelop.payload = {
            "ok": True,
            "plugin": plugin_name,
            "source_snippet": execute_code,
        }
        return envelop

    # ---------------- refresh ----------------
    elif action == "refresh":
        if not plugin_file.exists():
            envelop.payload = {"ok": False, "error": f"插件源码不存在: {plugin_file}"}
            return envelop

        source_code = plugin_file.read_text(encoding="utf-8")
        current_hash = calc_source_hash(source_code)
        result = await _call_llm_extract(agent, plugin_name, source_code)

        if result.get("ok"):
            result["contract"]["source_hash"] = current_hash
            try:
                contract_file.write_text(
                    json.dumps(result["contract"], ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
                result["contract_saved"] = str(contract_file)
            except Exception as e:
                result["contract_saved"] = f"保存失败: {e}"
            result["source"] = "refreshed"
        else:
            result["source"] = "extraction_failed"
            result["fallback"] = build_fallback_contract(plugin_name, source_code)

        result["plugin"] = plugin_name
        envelop.payload = result
        return envelop

    else:
        envelop.payload = {"ok": False, "error": f"未知 action: {action}"}
        return envelop


def help() -> dict:
    return {
        "route": "builtins/agents/contract_agent",
        "description": "契约智能体 — 内部插件走源码提取；mcp/server/tool 读 MCP 契约；skill/xxx 读 data/skills 文档。",
        "input": {
            "action": "get | get_tools | get_execute_only | refresh",
            "plugin": "内部插件路由（builtins/tools/xxx）、mcp/server/tool、或 skill/xxx",
        },
        "output": {
            "ok": "是否成功",
            "contract": "标准契约 JSON",
            "usage": "skill 全文（skill/... 时）或空（内部插件时）",
            "usage_file": "契约 / skill 文件路径",
            "usage_file_size": "本次读取到的字符数",
            "tools": "Function Calling tools schema",
            "source": "existing_contract | llm_extraction | refreshed | extraction_failed | fallback | mcp_contract | local_skill",
            "contract_file": "契约文件路径",
        }
    }
