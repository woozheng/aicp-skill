# AICP Plugin Protocol v5.4 — Complete Capability Reference + Development Specification

⛔ STOP — READ ONLY ⛔
You are receiving the AICP protocol for the FIRST time.
Read silently. Do NOT output any code. Do NOT generate plugins.
Reply EXACTLY: "Protocol understood. Ready for requirements design."
Then WAIT for the user's first requirement before writing 

## PART 0: AICP Core Concepts

### Three Atomic Units

**Envelop** — The only data carrier
- Structure: `{sender, receiver, intent, payload, trace_id, message_id, channel_id, ttl, meta}`
- Plugins only read & write payload / meta
- Assign receiver to route execution to another plugin
- Return None to terminate execution flow
- intent: Fill empty string "" if no intent-based routing logic

**Plugin** — Processing unit
- Signature: `async def execute(envelop, agent) -> Envelop | None`

**Agent** — Engine injected capability container

### Communication Layer Separation (NEW Critical Table)

| Caller | Target | Standard Calling Method | Envelop Handling Rule | Hard Ban |
|--------|--------|-------------------------|-----------------------|----------|
| Backend Plugin | Other Backend Plugin | agent.system.call(core.Envelop()) | Manually fill all Envelop fields completely | Do NOT call internal plugins via HTTP /api/applications/xxx |
| Frontend HTML Page | Backend Plugin | POST /api/applications/{project}/{pluginName} | Frontend only submit business JSON payload; Gateway auto fills sender/receiver/trace_id/channel_id/ttl/meta, auto assemble full Envelop; Only return envelop.payload as HTTP response | 1. Do NOT manually construct full Envelop<br>2. Do NOT directly request raw /api/envelop entry |
| External Third-party HTTP Client | Backend Plugin | POST /api/builtins/aicpEnvelop | Manually submit complete standard Envelop JSON | Built-in frontend pages are forbidden to use this entry |

### Gateway Auto Assembly Logic for Frontend Requests (Explicit Rule)

When frontend requests POST /api/applications/{project}/{xxx}:
- Auto set sender = "frontend"
- Auto assemble receiver = applications/{project}/{xxx}
- Auto generate unique trace_id / message_id
- Auto assign channel_id = {project}_dashboard
- Default ttl = 10, default meta = {}
- Frontend request body JSON is fully assigned to envelop.payload
- After plugin execution, gateway strips all Envelop fields except payload and returns to frontend

### System Architecture

| Port | Service | Core Plugin | Description |
|------|---------|-------------|-------------|
| base port | HTTP API + Static Resource | os/_gateway | Business JSON API, static HTML/CSS/JS distribution, Envelop routing dispatch |
| port + 1 | WebSocket Real-time | os/_websocket | Bidirectional real-time broadcast |
| port + 2 | Large File Upload | os/_file_receiver | Multipart form-data upload for files over 1MB |

### Agent Built-in Capabilities

| Tool | Return Type | Function Description |
|------|-------------|---------------------|
| agent.llm.chat(messages) | str | Normal LLM text completion |
| agent.llm.chat_json(messages) | dict | LLM forced return parsed JSON object |
| agent.llm.chat_stream(messages) | AsyncIterator[str] | Streaming chunk LLM output |
| agent.system.show_bubble(options) | dict | Pop frontend bubble notification |
| agent.system.show_result_card(options) | dict | Pop structured result card |
| agent.system.call(envelop) | Envelop | Cross-plugin synchronous call, return target plugin processed full Envelop |
| agent.scheduler.create_timer(seconds, callback) | timer_id | Create delayed background timer task |

### Agent Base Read-only Properties

| Property | Type | Description |
|----------|------|-------------|
| agent.config | dict | Global engine system configuration |
| agent.log | Logger | Log output object (error/warn/info) |
| agent.data_dir | Path | Persistent data root directory |
| agent.base_url | str | Engine frontend base URL prefix |

## PART 1: Plugin Standard Execution Patterns (Simplified, Remove Full Business Demo)

Only fixed signature & core logic skeleton reserved; specific business implementation is derivable by AI without full sample code.

### Pattern 1: Single LLM Chat

```python
async def execute(envelop, agent):
    llm = agent.llm
    if not llm:
        envelop.payload = {"error": "LLM not available"}
        return envelop
    # Custom message construction & llm.chat call derived by AI
    return envelop
```

### Pattern 2: Multi-step Pipeline

```python
async def execute(envelop, agent):
    llm = agent.llm
    # Step1 extract, Step2 summarize, sequential llm calls derived by AI
    return envelop
```

### Pattern 3: Parallel Multi-expert Task

```python
import asyncio
async def execute(envelop, agent):
    llm = agent.llm
    # asyncio.gather parallel tasks derived by AI
    return envelop
```

### Pattern 4: Multi-action Dispatch (Chat History CRUD)

```python
async def execute(envelop, agent):
    action = envelop.payload.get("action")
    if action == "send":
        # chat history append logic derived by AI
        return envelop
    elif action == "reset":
        envelop.payload = {"success": True, "history": []}
        return envelop
    envelop.payload = {"error": "Unknown action"}
    return envelop
```

### Pattern 5: Non-LLM Proxy / Webhook

```python
async def execute(envelop, agent):
    # Raw data forward logic derived by AI
    return envelop
```

### Pattern 6: Multi-file Application Routing (Entry _init.py dispatch)

```python
async def execute(envelop, agent):
    action = envelop.payload.get("action")
    if action == "tick":
        envelop.receiver = f"applications/{PROJECT}/tick"
        return envelop
    return envelop
```

### Pattern 7: Streaming SSE Output

```python
async def execute(envelop, agent):
    stream = agent.llm.chat_stream(...)
    # SSE generator wrapper derived by AI
    envelop.payload = sse_generator()
    return envelop
```

### Pattern 8: File Upload Processing

```python
# Frontend upload to port+2 /upload first
# Backend cross-call file analyzer plugin via agent.system.call
result = await agent.system.call(core.Envelop(
    sender=f"applications/{PROJECT}/processor",
    receiver="applications/{PROJECT}/analyzer",
    payload={"file_path": file_path, "action": "analyze"}
))
```

## PART 2: Mandatory Plugin Hard Rules

### 2.1 Function Signature Rule

```python
async def execute(envelop, agent) -> Envelop | None
# envelop.payload is mutable dict for state read/write
# Return None to discard request without response
```

### 2.2 Import Restriction

Forbidden: `from aicp import *` / any internal AICP module import

envelop and agent are fully injected by engine, no manual import required

### 2.3 LLM Invoke Rule

agent.llm.chat() always returns plain string, never dict

Must add guard `if not agent.llm:` before any LLM call

### 2.4 Routing Rule

Do NOT assign envelop.receiver unless explicitly forwarding to another plugin

Self-target receiver causes infinite loop, strictly forbidden

### 2.5 Timeout & Exception Clarification (Revised Ambiguous Text)

Original ambiguous sentence revised:

Engine only manages network/LLM global timeout. DO NOT write manual asyncio.wait_for timeout wrappers.

All business exceptions (IO error, JSON parse failure, parameter missing) must be manually captured with try-except blocks; engine will NOT auto-handle business crash exceptions.

### 2.6 File Transfer Limit

Binary data over 1MB cannot be embedded inside Envelop JSON payload

Use port+2 multipart upload, pass only file absolute path string in payload

5MB size limit only applies to binary base64 embedded JSON

### 2.7 Static Resource Response

```python
with open(filepath, "rb") as f:
    envelop.meta["static_content"] = f.read()
envelop.meta["content_type"] = "image/jpeg"
return envelop
```

## PART 9: Plugin Robustness Mandatory Specification (NEW Full Independent Chapter)

All plugins must comply with below constraints, otherwise output fails checklist validation.

### 9.1 Return Full Branch Guarantee

Every logical branch (normal success, parameter error, IO crash, unknown action) must return an Envelop object with filled payload

Forbidden: uncaught exception interrupt execution without return value (causes frontend undefined empty response)

### 9.2 File & Directory Operation Standard

- Multi-level directory creation mandatory syntax: `Path.mkdir(parents=True, exist_ok=True)`
- Single exist_ok=True without parents=True is invalid
- JSON read/write double-layer exception capture
  - Inner layer: catch json.JSONDecodeError; auto reset empty list/object if file corrupted
  - Global top-layer try-except wrap full execute function logic, capture all runtime errors
- File delete syntax: `Path.unlink(missing_ok=True)` to avoid file-not-exist crash

### 9.3 Input Parameter Validation

All action branches must validate required payload fields first; reject invalid params and return error payload before executing IO/LLM logic to save resource consumption

### 9.4 Log Standard

- Runtime crash / unexpected error: `agent.log.error(str(e))` with full exception trace
- Business warning (corrupted file, empty data): `agent.log.warning(message)`

## PART 3: Mandatory _init.py Application Entry Template

```python
import core
import webbrowser
from pathlib import Path

PROJECT = Path(__file__).parent.name

async def execute(envelop, agent):
    payload = envelop.payload.get("payload", envelop.payload)
    action = payload.get("action", "init")

    if action == "init":
        await agent.system.call(core.Envelop(
            sender=f"applications/{PROJECT}/_init",
            receiver="shell/_tray",
            payload={
                "action": "add_menu",
                "app_id": PROJECT,
                "label": "Application Display Name",
                "items": [
                    {"label": "Open Panel", "action": "open_web", "url": f"/{PROJECT}/index.html", "receiver": f"applications/{PROJECT}/_init"},
                ],
            },
        ))
        envelop.payload = {"ok": True}
        return envelop

    elif action == "open_web":
        webbrowser.open(f"{agent.base_url}/{PROJECT}/index.html")
        return envelop

    else:
        envelop.payload = {"error": f"Unknown action: {action}"}
        return envelop
```

### Key Constraints

- `PROJECT = Path(__file__).parent.name` — hardcoding project name string is forbidden
- All cross-plugin receiver use formatted string `f"applications/{PROJECT}/..."`
- Tray menu label is visible application name for end users

## PART 3.1 Mandatory app.yaml Application Config Rule (NEW)

Every application must contain root app.yaml config file; gateway skips routing registration without this file, all frontend API return 404.

### Minimum Standard Template

```text
=== APP: plugins/applications/{project}/app.yaml ===
name: Application Display Name
id: {project}
version: 1.0
description: Business brief description
entry: _init.py
static_root: www/apps/{project}
=== END ===
```

## PART 4: Standard Output Format Specification

### 4.1 Path Placeholder Rule

All file paths use {project} placeholder; never hardcode fixed project name string.

### 4.2 Unified Code Block Wrapper

All code blocks wrapped inside ```text markdown fence with fixed separator tags; split multiple blocks for multi-file projects.

```text
=== PLUGIN: plugins/applications/{project}/filename.py ===
full python code
=== END ===

=== HTML: www/apps/{project}/index.html ===
full html code
=== END ===

=== APP: plugins/applications/{project}/app.yaml ===
yaml config
=== END ===
```

### Block Type Mapping

| Prefix Tag | Fixed Root Path |
|------------|-----------------|
| PLUGIN | plugins/applications/{project}/ |
| HTML | www/apps/{project}/ |
| APP | plugins/applications/{project}/ |

### 4.3 Plugin File Naming Rule

- Business plugin: descriptive lowercase name e.g. chat.py, role_mgr.py
- Entry file: single underscore _init.py (MANDATORY)
- Double underscore __init__.py is skipped by engine, strictly forbidden

## PART 5: Frontend Hard Rules (MUST FOLLOW)

### 5.1 Core Immutable JS Variables (Non-derivable, must hardcode exactly)

```javascript
// 1. Dynamic project name, NO hardcode
const project = window.location.pathname.split('/')[1];

// 2. Standard API root prefix
const API = `/api/applications/${project}`;

// 3. WebSocket config fetch — ONLY GET, no headers / body
const { url } = await fetch('/api/ws_config').then(r => r.json());
const ws = new WebSocket(`${url}?channel=${project}_dashboard`);

// 4. File upload relative url calculation
const p = window.location.port;
const uploadUrl = p ? `${window.location.protocol}//${window.location.hostname}:${+p + 2}/upload` : '/upload';
const res = await fetch(uploadUrl, { method: 'POST', body: formData });
```

### 5.2 Standard Request Wrapper Template (Mandatory Fault Tolerance)

All frontend API calls must use this wrapper to avoid empty response undefined crash:

```javascript
async function request(pluginName, payload) {
  const resp = await fetch(`${API}/${pluginName}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  const rawText = await resp.text();
  if (!rawText) return {};
  return JSON.parse(rawText);
}
```

### 5.3 Frontend CSS Restriction

Forbidden: gradient, box-shadow decorative styles

Layout structure, flex alignment, message bubble logic are general frontend knowledge, derivable by AI without protocol demo code

### 5.4 Frontend Output Checklist Items

- Dynamic project name from path split
- Standard /api/applications/${project} API prefix
- No hardcoded port (9000/9001) / host (127.0.0.1 / localhost)
- WS only GET /api/ws_config without extra params
- Upload url follow port+2 rule
- No gradient/shadow CSS

## PART 6: Snapshot Single Source Of Truth

### Core Principle

One unified snapshot dict serves both LLM backend calculation and frontend rendering; no separated data copies.

**DO**
- Rebuild full snapshot on every business cycle
- Share identical snapshot for AI logic & frontend WS broadcast
- Persist snapshot once per cycle only
- Push snapshot to frontend via WebSocket channel

**DO NOT**
- Duplicate account/trade/asset data storage in multiple locations
- Generate different dataset for LLM and frontend separately
- Cache cross-cycle stock/state data

## PART 7: Common AI Output Mistakes (Only Architecture-level Hard Errors Reserved, Remove Business Demo Cases)

❌ **Hardcode project name string**
```python
# Wrong
receiver = "applications/chatbot/role_mgr"
# Correct
PROJECT = Path(__file__).parent.name
receiver = f"applications/{PROJECT}/role_mgr"
```

❌ **Hardcode WS port / host**
```javascript
// Wrong
const ws = new WebSocket("ws://127.0.0.1:9001/ws");
// Correct
const { url } = await fetch('/api/ws_config').then(r => r.json());
const ws = new WebSocket(`${url}?channel=${project}_dashboard`);
```

❌ **Hardcode API root path**
```javascript
// Wrong
fetch("/api/applications/chatbot/role_mgr")
// Correct
const project = window.location.pathname.split('/')[1];
fetch(`${API}/role_mgr`)
```

❌ **Embed large binary base64 inside JSON payload**
```python
# Wrong
envelop.payload = {"file_base64": huge_encoded_string}
# Correct
envelop.payload = {"file_path": "/data/xxx.file"}
```

❌ **Split snapshot data for AI & frontend separately**
```python
# Wrong
ai_data = build_ai_snapshot()
fe_data = build_frontend_snapshot()
# Correct
snapshot = build_unified_snapshot()
```

## PART 8: Final Pre-output Full Checklist

Tick all items before delivering any code/file output

### Backend Plugin Check
- Root application _init.py exists with tray menu registration logic
- Use PROJECT = Path(__file__).parent.name, no hardcode project name
- Zero internal aicp module import statements
- LLM guard `if not agent.llm:` added before every llm invoke
- No self-target envelop.receiver infinite loop routing
- No manual asyncio.wait_for timeout wrapper
- Full top-layer try-except global exception capture for execute()
- Directory creation uses mkdir(parents=True, exist_ok=True)
- JSON read/write catch JSONDecodeError, auto reset corrupted file
- Every code branch returns filled payload Envelop object
- Root app directory contains valid app.yaml config file
- All file paths use {project} placeholder in output block tags
- Output wrapped with standard === TYPE: PATH === / === END === text fence

### Frontend HTML Check
- Dynamic project name extracted from window.location.pathname.split('/')[1]
- API variable fixed as /api/applications/${project}
- WS only fetch via GET /api/ws_config, no custom headers/body
- No hardcoded IP, port, hostname strings
- Use standard fault-tolerant request() wrapper for all API POST calls
- CSS contains no gradient / shadow decorative styles

### Data Consistency Check
- Single unified snapshot shared for backend logic and frontend broadcast
- No duplicate independent state datasets stored

[DONE]

