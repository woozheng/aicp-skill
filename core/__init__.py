"""AICP Core — Envelop + Agent + plugins dict + route

协议核心（Tier 1）：
  - Envelop 类型：sender, receiver, payload, ttl, status, meta
  - route 函数：查找插件、调用、返回；每次路由尝试消耗一个 ttl
  - 插件签名：async def execute(envelop, agent)
  - 插件查找空间：plugins 字典（实现选择；协议要求"按 receiver 可达"）
  - 追加状态约束：本 core 不涉及状态层（属于 Tier 3，由上层处理）

三层分离（改任何东西之前先理解这三层）：
  - Envelop 顶层字段 = 包裹结构（封闭集合）
  - payload            = 内容（开放集合，core 不解释其结构）
  - meta               = 投递指令（半开放，只有"路由行为键"会改变 route 行为）

协议级失败类（status）：
  空字符串      = 成功（无协议级错误）
  "INVALID"    = Envelop 违反约束（route 产生）
  "MISSING"    = receiver 无对应插件（route 产生）
  "TIMEOUT"    = 一次路由尝试未在时限内产生结果（route 产生）
  "EXCEPTION"  = 插件抛异常（插件产生）
  "META_FAILED"= meta 契约无法满足，或拒绝（插件产生）
  终止状态： "DROPPED" / "ISOLATED" / "DORMANT"

# ============================================================
# 路由行为键清单（meta 中会改变 route 行为的键）
#
# 只有下列键被 route / 异步 / 回调真正消费。不在清单上的键视为
# "插件私货"：route 不读，异步回调也不保证透传（见 _execute_async）。
# 新增行为键 = 改协议，必须追加到本清单。
#
#   callback_receiver        有值时：route 立即返回 processing，后台执行，
#                            完成后回调到该地址/路由
#   is_callback              有值时：route 立即 ACK，后台投递到 receiver
#   callback_session_id      回调透传给接收方（跨层语义）
#   session_id               route 兜底填 callback_session_id 用
#   remote_node              回调透传（跨节点标识）
#   remote_plugin            回调透传（远程插件标识）
#   signature                _http_callback 发送时写入，接收方校验
#   token                    调用方提供的回调 token，core 原样带上，不签发
#
# 仅"透传"不等于"行为键"：remote_node / remote_plugin / signature / token
# 不改变 route 的分支走向，只被带到回调消息里。
# ============================================================

# ============================================================
# 协议演进规则（改 core 之前必读）
#
# status：协议内部状态机
#   新增值          → 允许。追加常量 + 更新 is_terminal()，不改已有分支
#   修改已有语义    → 禁止（所有按 status 判断的插件会静默错乱）
#   删除值          → 禁止
#
# Envelop 字段：包裹结构，封闭集合
#   新增字段        → 禁止。Envelop 是"包裹"不是"内容容器"，
#                     任何业务数据一律放 payload，不要长在 Envelop 上。
#                     加字段 = 悄悄改协议，会让 route / 异步 / 回调
#                     每一层都要多回答一次"这个字段要不要透传"。
#   删除字段        → 禁止
#   修改字段语义    → 禁止
#
# payload：内容，开放集合
#   随便加、随便删，core 不解释它的结构。
#
# meta：投递指令，半开放
#   只有"路由行为键"（见上方清单）会改变 route 行为，必须显式登记。
#   不在清单上的键 = 插件私货：route 不读，异步回调也不保证透传
#   （当前只透传 remote_node / remote_plugin / callback_session_id /
#     session_id / user_id / token）。
#
# route 返回契约：永远返回 Envelop，不返回 None
#   禁止改变。
# ============================================================

向后兼容：
  - status 是新增字段，默认 ""。旧代码不读它，行为不变。
  - route 在设置 status 的同时，仍然写 payload["error"]。
  - 所有已有函数签名、字段、payload 语义保留。
  - Envelop.result() 是新增读法；旧代码仍可直接读 .payload / .status。
"""

import asyncio
import uuid
import json
import hmac
import hashlib
import os
from typing import Dict, Optional, Callable
from datetime import datetime


# ============================================================
# 协议级失败类（Tier 1）
# 这些常量由协议定义。插件实现可以读它们，但不应重新定义。
# ============================================================
STATUS_OK          = ""              # 成功：无协议级错误
STATUS_INVALID     = "INVALID"       # Envelop 违反约束（route 产生）
STATUS_MISSING     = "MISSING"       # receiver 无对应插件（route 产生）
STATUS_TIMEOUT     = "TIMEOUT"       # 路由尝试未在时限内产生结果（route 产生）
STATUS_EXCEPTION   = "EXCEPTION"     # 插件抛异常（插件产生）
STATUS_META_FAILED = "META_FAILED"   # meta 契约无法满足，或拒绝（插件产生）

# 终止状态：消息不再被尝试
STATUS_DROPPED     = "DROPPED"       # 消息被丢弃
STATUS_ISOLATED    = "ISOLATED"      # 消息被隔离
STATUS_DORMANT     = "DORMANT"       # ttl 耗尽

_TERMINAL_STATUSES = frozenset({
    STATUS_DROPPED,
    STATUS_ISOLATED,
    STATUS_DORMANT,
})


def is_terminal(status: str) -> bool:
    """协议辅助：判断 status 是否为终止状态。

    终止状态表示消息不再被尝试；调用者不应再重试。
    空字符串（成功）不是终止状态。
    """
    return status in _TERMINAL_STATUSES


def fail(envelop: "Envelop", status: str, message: str = "") -> "Envelop":
    """协议辅助：把 Envelop 标记为失败。

    设置 status（协议路径），同时写入 payload["error"]（向后兼容）。
    插件作者可以选择使用它；不使用也可以直接设置 envelop.status。
    """
    envelop.status = status
    if message:
        envelop.payload = {**envelop.payload, "error": message}
    return envelop


# ============================================================
# 回调签名密钥（从环境变量读，不硬编码）
# ============================================================
CALLBACK_SECRET = os.environ.get("AICP_CALLBACK_SECRET", "")


def sign_payload(payload: dict) -> str:
    """计算 payload 签名"""
    if not CALLBACK_SECRET:
        return ""
    data = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hmac.new(CALLBACK_SECRET.encode(), data.encode(), hashlib.sha256).hexdigest()


def verify_signature(payload: dict, signature: str) -> bool:
    """验证签名"""
    if not CALLBACK_SECRET or not signature:
        return False
    expected = sign_payload(payload)
    return hmac.compare_digest(expected, signature)


# ============================================================
# Envelop —— 唯一消息类型
# ============================================================
class Envelop:
    """AICP 的唯一消息类型。

    协议字段（Tier 1）：
      sender, receiver, ttl, status, meta

    额外字段（本实现选择，Tier 3；旧代码依赖，保留）：
      intent, channel_id, trace_id, message_id, created_at, path_history

    ⚠ 字段集是封闭集合：不允许新增 / 删除 / 改语义。
      业务数据一律放 payload；投递指令放 meta 且必须登记为行为键。
    """

    __slots__ = (
        "sender", "receiver", "intent", "payload",
        "trace_id", "message_id", "channel_id", "ttl", "meta",
        "created_at", "path_history",
        "status",   # 协议级失败状态；空 = 无错误
    )

    def __init__(self, sender="", receiver="", intent="", payload=None,
                 channel_id="", ttl=10, meta=None, status=""):
        self.sender = sender
        self.receiver = receiver
        self.intent = intent
        self.payload = payload if payload is not None else {}
        self.trace_id = f"tr_{uuid.uuid4().hex[:8]}"
        self.message_id = f"msg_{uuid.uuid4().hex[:6]}"
        self.channel_id = channel_id
        self.ttl = ttl
        self.meta = meta if meta is not None else {}
        self.created_at = datetime.now().isoformat()
        self.path_history = []
        self.status = status   # 新增：协议级失败状态

    def to_dict(self) -> dict:
        return {
            "sender": self.sender,
            "receiver": self.receiver,
            "intent": self.intent,
            "payload": self.payload,
            "trace_id": self.trace_id,
            "message_id": self.message_id,
            "channel_id": self.channel_id,
            "ttl": self.ttl,
            "meta": self.meta,
            "created_at": self.created_at,
            "status": self.status,   # 新增
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Envelop":
        env = cls(
            sender=data.get("sender", ""),
            receiver=data.get("receiver", ""),
            intent=data.get("intent", ""),
            payload=data.get("payload", {}),
            channel_id=data.get("channel_id", ""),
            ttl=data.get("ttl", 10),
            meta=data.get("meta", {}),
            status=data.get("status", ""),   # 新增
        )
        env.trace_id = data.get("trace_id", env.trace_id)
        env.message_id = data.get("message_id", env.message_id)
        env.created_at = data.get("created_at", env.created_at)
        return env

    # --------------------------------------------------------
    # 统一读法（新增）
    # --------------------------------------------------------
    def result(self, default_error: str = "no response") -> dict:
        """归一化读取：把 status / payload.error / 空 payload 统一成 {ok, ...}。

        调用者不需要知道失败来自协议层还是业务层，读这一个方法即可。
        旧代码仍可直接读 .payload / .status，行为不变。

        返回：
          {"ok": True,  ...payload}              成功
          {"ok": False, "status": "...", ...}   协议级失败（MISSING / TIMEOUT / ...）
          {"ok": False, ...payload}             业务级失败（payload.error 存在）

        注意：协议级失败时保留 status 字段；调用方若只看 ok 会丢诊断信息，
        需要区分失败类型时读 result["status"]。
        """
        raw = self.payload
        if raw is None:
            payload = {}
        elif isinstance(raw, dict):
            payload = dict(raw)          # 拷贝，避免污染原 Envelop
        else:
            payload = {"data": raw}

        status = self.status or ""
        if status:
            return {**payload, "ok": False, "status": status}

        if payload.get("error"):
            return {**payload, "ok": False}

        return {**payload, "ok": True}


# ============================================================
# Agent —— 能力容器
# ============================================================
class Agent:
    """能力容器。插件通过它接收能力，而不是自己导入能力。

    暴露哪些能力是实现选择（Tier 3）。
    """
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)

    def get(self, key, default=None):
        return getattr(self, key, default)


# ============================================================
# 插件查找空间（Tier 1 要求：按 receiver 可达）
#
# 协议要求：插件按 receiver 在共享命名空间中可达。
# 实现方式（字典、文件系统、数据库）是 Tier 3 选择；此处用字典。
# ============================================================
plugins: Dict[str, Callable] = {}


# ============================================================
# 异步任务内部标记键（不对外暴露）
# ============================================================
_ASYNC_INTERNAL_KEYS = (
    "callback_receiver",
    "_async_started",
)


def _strip_async_meta(meta: dict) -> dict:
    """从 meta 中移除异步内部标记，防止插件透传时重复触发"""
    cleaned = {k: v for k, v in meta.items() if k not in _ASYNC_INTERNAL_KEYS}
    return cleaned


def _attach_traceback(envelop: "Envelop"):
    """把完整 traceback 放进 meta。

    不污染 payload（业务层读不到），但自改 / 排障需要完整堆栈。
    meta["traceback"] 不在路由行为键清单内，不会被 route 消费。
    """
    import traceback
    try:
        envelop.meta["traceback"] = traceback.format_exc()
    except Exception:
        pass
    return envelop


# ============================================================
# route —— 唯一路由函数
#
# 协议要求：
#   1. 按 receiver 在查找空间中查找插件
#   2. 用 (envelop, agent) 调用它
#   3. 返回结果
#   4. 每次路由尝试消耗一个 ttl
#
# 支持三种模式（Tier 2 约定，由 meta 区分）：
#   1. 同步：无特殊 meta，直接执行并等待结果
#   2. 异步回调：meta 中有 callback_receiver，后台执行完成后回调
#   3. 回调确认：meta 中有 is_callback，立即 ACK 并后台投递
#
# 返回契约（保证）：
#   route 永远返回 Envelop（不返回 None）。
#   调用方可无条件 result.result() 拿归一结果。
# ============================================================
async def route(envelop: Envelop, agent: Agent = None, timeout: float = 600.0) -> Envelop:
    """引擎路由。

    每次路由尝试消耗一个 ttl（协议级）。
    结构性失败设置 envelop.status；同时写 payload["error"]（向后兼容）。
    永远返回 Envelop，不返回 None。
    """

    # ---- INVALID：Envelop 结构违反约束 ----
    if not envelop.receiver:
        envelop.status = STATUS_INVALID
        envelop.payload = {**envelop.payload, "error": "Missing receiver"}
        return envelop

    # ---- DORMANT：ttl 耗尽，终态 ----
    if envelop.ttl <= 0:
        envelop.status = STATUS_DORMANT
        envelop.payload = {**envelop.payload, "error": "TTL expired"}
        return envelop

    # ---- 每次路由尝试消耗一个 ttl（协议级） ----
    envelop.ttl -= 1

    # ---- 插件查找（协议级） ----
    plugin = plugins.get(envelop.receiver)
    if not plugin:
        envelop.status = STATUS_MISSING
        envelop.payload = {**envelop.payload, "error": f"Plugin not found: {envelop.receiver}"}
        return envelop

    if agent is None:
        agent = Agent()

    # ============================================================
    # 模式 3：回调确认 — 收到回调消息，立即 ACK，后台投递
    # （Tier 2 约定，由 meta.is_callback 区分）
    # ============================================================
    if envelop.meta.get("is_callback"):
        asyncio.create_task(_deliver_callback(envelop, agent, timeout))
        return Envelop(
            sender=envelop.receiver,
            receiver=envelop.sender,
            payload={"ok": True, "received": True},
            meta={"is_ack": True, "trace_id": envelop.trace_id},
        )

    # ============================================================
    # 模式 2：异步回调 — 发起方请求异步执行
    # （Tier 2 约定，由 meta.callback_receiver 区分）
    # ============================================================
    callback_receiver = envelop.meta.get("callback_receiver", "")
    if callback_receiver:
        asyncio.create_task(_execute_async(envelop, agent, callback_receiver, timeout))
        return Envelop(
            sender=envelop.receiver,
            receiver=envelop.sender,
            payload={"ok": True, "status": "processing"},
            meta={"async": True, "trace_id": envelop.trace_id},
        )

    # ============================================================
    # 模式 1：同步执行
    # ============================================================
    task = asyncio.ensure_future(plugin(envelop, agent))
    try:
        result = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        if result is None:
            envelop.status = STATUS_EXCEPTION
            envelop.payload = {**envelop.payload, "error": "Plugin returned None"}
            _attach_traceback(envelop)
            return envelop
        return result
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        envelop.status = STATUS_TIMEOUT
        envelop.payload = {**envelop.payload, "error": f"Plugin timeout after {timeout}s"}
        return envelop
    except Exception as e:
        envelop.status = STATUS_EXCEPTION
        envelop.payload = {**envelop.payload, "error": f"Plugin execution error: {str(e)[:200]}"}
        _attach_traceback(envelop)
        return envelop


# ============================================================
# 异步执行 + 回调
# ============================================================
async def _execute_async(envelop: Envelop, agent: Agent, callback_receiver: str, timeout: float):
    """后台执行插件，完成后回调。"""

    # ★★★ 关键：执行插件前，清除异步内部标记 ★★★
    envelop.meta = _strip_async_meta(envelop.meta)
    # ★ 恢复 contextvar：异步 Task 不继承父 Task 的 contextvar，
    #   这里用 envelop 里透传的 session_id 重新设一次，让 LLM 统计归位
    try:
        from runtime._llm import set_current_session
        _sid = envelop.meta.get("callback_session_id") or envelop.meta.get("session_id") or ""
        if _sid:
            set_current_session(_sid)
    except Exception:
        pass

    try:
        plugin = plugins.get(envelop.receiver)
        if plugin:
            _t = asyncio.ensure_future(plugin(envelop, agent))
            try:
                result = await asyncio.wait_for(asyncio.shield(_t), timeout=timeout)   # ← 加 result =
            except asyncio.TimeoutError:
                _t.cancel()
                try:
                    await asyncio.wait_for(_t, timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
                raise
        else:
            result = Envelop(payload={"error": f"Plugin not found: {envelop.receiver}"}, status=STATUS_MISSING)
    except asyncio.TimeoutError:
        result = Envelop(
            payload={"error": f"Plugin timeout after {timeout}s"},
            status=STATUS_TIMEOUT,
        )
    except Exception as e:
        result = Envelop(
            payload={"error": f"Plugin execution error: {str(e)[:200]}"},
            status=STATUS_EXCEPTION,
        )
        _attach_traceback(result)

    if result is None:
        result = Envelop(
            payload={"error": "Plugin returned None"},
            status=STATUS_EXCEPTION,
        )
    # 若插件返回了 status=""（成功），保持它。
    # 若插件显式设了 META_FAILED，也保持。

    # ============================================================
    # 构造回调消息
    # ============================================================
    result.sender = envelop.receiver
    result.receiver = callback_receiver
    result.trace_id = envelop.trace_id
    result.meta["is_callback"] = True
    result.meta["callback_original_receiver"] = envelop.receiver
    result.meta["trace_id"] = envelop.trace_id

    # 透传业务信息（不是异步标记）
    # 注意：只有下列键被透传；其他 meta 键（插件私货）不保证带到回调。
    for key in ("remote_node", "remote_plugin", "callback_session_id", "user_id", "token"):
        if envelop.meta.get(key):
            result.meta[key] = envelop.meta[key]

    # session_id 兜底
    if not result.meta.get("callback_session_id") and envelop.meta.get("session_id"):
        result.meta["callback_session_id"] = envelop.meta["session_id"]

    # ============================================================
    # 发送回调
    # ============================================================
    if callback_receiver.startswith(("http://", "https://")):
        await _http_callback(callback_receiver, result, timeout, agent)
    elif "/" in callback_receiver:
        try:
            await route(result, agent, timeout=timeout)
        except Exception:
            pass
    else:
        await _http_callback(f"https://{callback_receiver}", result, timeout, agent)


async def _http_callback(url: str, envelop: Envelop, timeout: float, agent: Agent = None):
    """HTTP 回调：把结果 POST 到远程地址。

    core 只做投递，不签发 token：
    - token 由调用方放在 envelop.meta["token"]，core 原样带上
    - 没有 token 就不带，接收方决定是否放行
    """
    import aiohttp

    if "://" not in url:
        url = f"https://{url}"
    url = url.rstrip("/")
    if "/api/" not in url:
        url = f"{url}/api/builtins/agents/main_agent"

    # 签名（如果配置了 secret）
    signature = sign_payload(envelop.payload)
    if signature:
        envelop.meta["signature"] = signature

    # token 由调用方提供，core 不签发
    token = envelop.meta.get("token", "")

    body = {
        "token": token,
        "payload": envelop.payload,
        "meta": envelop.meta,
        "status": envelop.status,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=body,
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                print(f"[callback] POST {url} -> {resp.status}")
    except Exception as e:
        print(f"[callback] FAILED: {url} -> {e}")


async def _deliver_callback(envelop: Envelop, agent: Agent, timeout: float):
    """后台投递回调消息到插件。"""
    # ★ 恢复 contextvar（同 _execute_async）
    try:
        from runtime._llm import set_current_session
        _sid = envelop.meta.get("callback_session_id") or envelop.meta.get("session_id") or ""
        if _sid:
            set_current_session(_sid)
    except Exception:
        pass

    try:
        plugin = plugins.get(envelop.receiver)
        if plugin:
            await asyncio.wait_for(plugin(envelop, agent), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    except Exception as e:
        print(f"[callback] deliver failed: {e}")


# 兼容旧代码
engine_route = route
