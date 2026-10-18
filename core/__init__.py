"""AICP Core — Envelop + Agent + plugins dict + route

协议核心（Tier 1）：
  - Envelop 类型：sender, receiver, payload, ttl, status, meta
  - route 函数：查找插件、调用、返回；每次路由尝试消耗一个 ttl
  - 插件签名：async def execute(envelop, agent)
  - 插件查找空间：plugins 字典（实现选择；协议要求"按 receiver 可达"）
  - 追加状态约束：本 core 不涉及状态层（属于 Tier 3，由上层处理）

协议级失败类（status）：
  空字符串      = 成功（无协议级错误）
  "INVALID"    = Envelop 违反约束（route 产生）
  "MISSING"    = receiver 无对应插件（route 产生）
  "TIMEOUT"    = 一次路由尝试未在时限内产生结果（route 产生）
  "EXCEPTION"  = 插件抛异常（插件产生）
  "META_FAILED"= meta 契约无法满足，或拒绝（插件产生）
  终止状态： "DROPPED" / "ISOLATED" / "DORMANT"

向后兼容：
  - status 是新增字段，默认 ""。旧代码不读它，行为不变。
  - route 在设置 status 的同时，仍然写 payload["error"]。
  - 所有已有函数签名、字段、payload 语义保留。
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


def _get_callback_token(agent: Agent = None) -> str:
    """从 agent.config 读 token，不硬编码"""
    if agent and hasattr(agent, 'config'):
        tokens = agent.config.get("tokens", [])
        if tokens:
            return tokens[0]
    return os.environ.get("AICP_TOKEN", "")


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
# ============================================================
async def route(envelop: Envelop, agent: Agent = None, timeout: float = 1200.0) -> Optional[Envelop]:
    """引擎路由。

    每次路由尝试消耗一个 ttl（协议级）。
    结构性失败设置 envelop.status；同时写 payload["error"]（向后兼容）。
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
    try:
        result = await asyncio.wait_for(plugin(envelop, agent), timeout=timeout)
        return result
    except asyncio.TimeoutError:
        envelop.status = STATUS_TIMEOUT
        envelop.payload = {**envelop.payload, "error": f"Plugin timeout after {timeout}s"}
        return envelop
    except Exception as e:
        envelop.status = STATUS_EXCEPTION
        envelop.payload = {**envelop.payload, "error": f"Plugin execution error: {str(e)[:200]}"}
        return envelop


# ============================================================
# 异步执行 + 回调
# ============================================================
async def _execute_async(envelop: Envelop, agent: Agent, callback_receiver: str, timeout: float):
    """后台执行插件，完成后回调。"""

    # ★★★ 关键：执行插件前，清除异步内部标记 ★★★
    envelop.meta = _strip_async_meta(envelop.meta)

    try:
        plugin = plugins.get(envelop.receiver)
        if plugin:
            result = await asyncio.wait_for(plugin(envelop, agent), timeout=timeout)
        else:
            result = Envelop(
                payload={"error": f"Plugin not found: {envelop.receiver}"},
                status=STATUS_MISSING,
            )
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
    for key in ("remote_node", "remote_plugin", "callback_session_id"):
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
    """HTTP 回调：把结果 POST 到远程地址。"""
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

    # ★ token 从 agent.config 读，不硬编码
    token = _get_callback_token(agent)

    body = {
        "token": token,
        "payload": envelop.payload,
        "meta": envelop.meta,
        "status": envelop.status,   # 新增：把协议级状态一起发送
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