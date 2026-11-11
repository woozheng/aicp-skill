"""
LLM — Production-grade multi-provider LLM client.

v4-enterprise-final（本次全部闭环）
Review Fix Checklist：
✅ P0  <think> 标签字面量正确（"<think>" / "</think>"），不是空串
✅ P0  事件 type 统一用 str 字面量，去掉 StreamEventType 枚举（避免 json.dumps 炸）
✅ P1  text_buf / think_buffer 真正隔离：text_buf 只累积非 think 内容
✅ P1  think_buffer 残留统一 flush 到 text_buf，短 token 不丢字
✅ P1  chat_stream think_unclosed + yielded_any 时不重复 yield
✅ P1  chat_tool_stream in_think 未闭合 + yielded_text 时不重复 yield
✅ P1  同 chunk content+tool_calls 时 think_buffer 不丢
✅ P2  _sanitize_for_log 用正则匹配 sk-xxx，不误伤 task-
✅ P2  _model_configs / _model_to_client 用直接赋值，不用 setdefault
✅ P3  多 tool_calls 时打日志（只取第一个）
✅ 保留：脱敏、usage 上报、健康检查、close 资源释放、重试退避、深拷贝 stats

v4.1-debug
✅ _iter_stream_with_timeout 用 shield + cancel，防 SDK 不响应 cancel
✅ stream.close() 超时 10s → 3s
✅ keepalive_expiry 15s → 5s
✅ 新增 _debug_pool_state：连接池快照
✅ chat_tool_stream 每 attempt / 超时 / 异常时打 debug 日志
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Union, TypedDict

import httpx
from openai import AsyncOpenAI

# ---------------------------------------------------------------------------
# Usage JSONL logger — 直接写磁盘，读写分离，不经过插件
# ---------------------------------------------------------------------------

_USAGE_DIR = Path("data/llm_usage")
_USAGE_DIR.mkdir(parents=True, exist_ok=True)


def _write_usage_jsonl(record: dict) -> None:
    """追加一条 JSONL 记录到按日期分片的文件。
    单进程内线程安全（GIL + append mode），不加锁。
    """
    try:
        date_str = time.strftime("%Y-%m-%d")
        filepath = _USAGE_DIR / f"{date_str}.jsonl"
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass  # 日志写入失败不能影响主流程

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("aicp.llm")


def _log(msg: str) -> None:
    logger.debug(msg)


def _info(msg: str) -> None:
    logger.info(msg)


def _warn(msg: str) -> None:
    logger.warning(msg)


# ★ 只脱敏真正的 sk-xxx 密钥，不误伤 task- 之类的词
_KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{20,}")


def _sanitize_for_log(s: str, max_len: int = 120) -> str:
    """日志脱敏：截断，移除疑似密钥片段，防止敏感信息外泄"""
    if not isinstance(s, str):
        try:
            s = str(s)
        except Exception:
            return "<unprintable>"
    s = _KEY_PATTERN.sub("[REDACTED_SK]", s)
    if len(s) > max_len:
        return s[:max_len] + "..."
    return s


# ---------------------------------------------------------------------------
# ★ 当前 session 的 contextvar（跨协程传播）
# 注意：asyncio.create_task 默认不继承 contextvars；
#      如回调内新建 task 且需要 session_id，请显式 set_current_session。
# ---------------------------------------------------------------------------

_current_session_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "llm_session_id", default=""
)


def set_current_session(session_id: str) -> None:
    _current_session_id.set(session_id or "")


def get_current_session() -> str:
    return _current_session_id.get() or ""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# ============================================================
# 超时与重试配置（聚合 key / 多上游场景）
# 设计原则：
# 1. 连接、响应头、首 token、流内间隔、关闭 分阶段独立超时
# 2. 聚合 key 上游质量不一，响应头预算不宜过长，早失败早重试
# 3. 总请求超时必须覆盖「响应头 + 首 token + 生成」的最坏情况
# 4. HTTP 底层读超时必须 >= 上层最大等待，否则底层先断、上层看不到超时日志
# 5. 流内 token 间隔收紧，真卡死时快速重试而不是占并发
# ============================================================

# ---- 连接阶段 ----
DEFAULT_CONNECT_TIMEOUT: float = 15.0      # TCP/TLS 建连。聚合上游通常网络可达，15s 足够；原 30s 偏长

# ---- 响应阶段（分阶段，关键改动）----
DEFAULT_RESPONSE_HEADER_TIMEOUT: float = 45.0   # 新增：等 HTTP 响应头。聚合网关排队/路由异常时 45s 内无 header 早失败
DEFAULT_FIRST_TOKEN_TIMEOUT: float = 120.0      # 等首个 SSE data 帧。Claude 带 tools + 大上下文静默期可达 60-90s，留余量
DEFAULT_INTER_TOKEN_TIMEOUT: float = 45.0       # 流内 token 间隔。原 90s 过宽，45s 无数据基本可判定卡死

# ---- 总预算 ----
DEFAULT_REQUEST_TIMEOUT: float = 180.0     # 单次请求总超时。需覆盖 header(45)+首token(120)+生成，原 90s 对 42k+递归偏紧
DEFAULT_STREAM_TIMEOUT: float = 300.0      # 保留兼容；流式整体上限，建议 >= REQUEST_TIMEOUT
DEFAULT_STREAM_CLOSE_TIMEOUT: float = 3.0  # ★ 流关闭握手，10s → 3s

# ---- HTTP 底层 ----
DEFAULT_HTTP_READ_TIMEOUT: float = 150.0   # 必须 >= FIRST_TOKEN_TIMEOUT(120) 且留余量，否则底层先断。原 120 与 90 首token 勉强，调大后需同步提到 150

# ---- 重试与并发 ----
DEFAULT_MAX_RETRIES: int = 3               # 保持
DEFAULT_RETRY_BACKOFF_MAX: int = 10        # 保持；聚合 key 场景建议退避带 jitter（见下方说明）
DEFAULT_MAX_CONCURRENT: int = 10           # 保持；但递归串行场景下实际占用远小于此
DEFAULT_HEALTH_CHECK_TIMEOUT: float = 5.0  # 健康检查，保持

# ---- 其他 ----
THINK_DECIDER_LEN: int = 10

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"

HTTP_TIMEOUT_CONFIG = {
    "connect": DEFAULT_CONNECT_TIMEOUT,
    "read": DEFAULT_HTTP_READ_TIMEOUT,
    "write": DEFAULT_HTTP_READ_TIMEOUT,
    "pool": DEFAULT_CONNECT_TIMEOUT,
}

# ★ keepalive_expiry：15s → 5s（半死连接 5 秒后回收）
KEEPALIVE_EXPIRY: float = 5.0

ERROR_PREFIXES = (
    "[LLM stream error:",
    "[LLM请求失败:",
    "[系统错误:",
    "[服务请求超时",
    "[模型返回空响应",
    "[LLM 达到最大重试次数]",
    "[LLM 未配置]",
    "[空响应]",
)

# 事件类型统一字符串字面量，不用 Enum（避免 json.dumps / str() 语义坑）
ET_TEXT = "text"
ET_TOOL_CALL = "tool_call"
ET_THINKING = "thinking"
ET_ERROR = "error"
ET_DONE = "done"


def is_llm_error_string(text) -> bool:
    if not isinstance(text, str):
        return False
    if not text:
        return False
    return any(text.startswith(p) for p in ERROR_PREFIXES)


def _classify_stream_error(exc: Exception) -> str:
    if isinstance(
        exc,
        (
            httpx.ReadError,
            httpx.RemoteProtocolError,
            httpx.ConnectError,
            httpx.ReadTimeout,
            httpx.WriteError,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
            httpx.ConnectTimeout,
            httpx.NetworkError,
        ),
    ):
        return "network"
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout"
    if isinstance(exc, json.JSONDecodeError):
        return "parse"

    # ★ OpenAI SDK 的 API 层错误（含 provider 在 SSE 流里发的 error 事件）
    if type(exc).__name__ in ("APIError", "APIConnectionError", "APITimeoutError", "InternalServerError"):
        return "network"

    # ★ 字符串关键词兜底（覆盖 tool_input_invalid / incomplete SSE 等）
    msg = str(exc).lower()
    if (
        "tool_input_invalid" in msg
        or "invalid or incomplete" in msg
        or "incomplete sse" in msg
        or "sse stream" in msg
        or "stream error" in msg
        or "stream interrupted" in msg
        or "api_error" in msg
    ):
        return "network"

    return "unknown"


def _strip_leading_think(text: str) -> str:
    """剥离开头的 <think>...</think>。无 think 原样返回；未闭合返回空串。"""
    if not text:
        return text
    stripped = text.lstrip()
    if not stripped.startswith(THINK_OPEN):
        return text
    close_idx = stripped.find(THINK_CLOSE)
    if close_idx == -1:
        return ""
    # 只去掉 think 标签后的换行，保留正文/代码块的首行缩进。
    return stripped[close_idx + len(THINK_CLOSE):].lstrip("\r\n")


def _bare_model_id(ref: str) -> str:
    """'provider/model' -> 'model'；裸 id 原样返回。"""
    if isinstance(ref, str) and "/" in ref:
        return ref.split("/", 1)[1]
    return ref


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    id: str = ""
    parse_error: str = ""


@dataclass
class LLMResponse:
    text: str = ""
    tool_call: Optional[ToolCall] = None
    raw: Any = None
    finish_reason: str = ""


@dataclass
class ModelConfig:
    client_name: str
    max_tokens: int = 4096
    temperature: float = 0.7
    supports_streaming: bool = True
    high_quality: bool = False


@dataclass
class ProviderConfig:
    name: str
    type: str
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    models: List[Dict[str, Any]] = field(default_factory=list)
    default_model: str = ""


class LLMError(Exception):
    pass


FALLBACK_MESSAGES = {
    "not_configured": "[LLM 未配置]",
    "empty_response": "[模型返回空响应，请稍后重试]",
    "timeout": "[服务请求超时，请稍后重试]",
    "max_retries": "[LLM 达到最大重试次数]",
    "system_error": "[系统错误]",
    "empty": "[空响应]",
}


class UsageDict(TypedDict):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


# ---------------------------------------------------------------------------
# Main LLM
# ---------------------------------------------------------------------------


class LLM:
    def __init__(self, config: Dict[str, Any]) -> None:
        models_cfg = config.get("models", {})
        # 日志级别统一由 runtime/__main__.py 的 setup_logging() 控制。
        # 此处不调 basicConfig，避免劫持宿主进程的 root logger；
        # 也不单独 setLevel("aicp.llm")，让它继承 aicp 父 logger 的级别。
        self._roles: Dict[str, Dict[str, Any]] = {
            k: dict(v) for k, v in models_cfg.get("roles", {}).items()
        }
        self.default_model: str = self._roles.get("default", {}).get(
            "model",
            models_cfg.get("default", "gpt-3.5-turbo"),
        )

        self.providers: Dict[str, Any] = models_cfg.get("providers", {})

        self._clients: Dict[str, AsyncOpenAI] = {}
        self._model_to_client: Dict[str, str] = {}
        self._model_configs: Dict[str, ModelConfig] = {}

        self.max_retries: int = models_cfg.get("max_retries", DEFAULT_MAX_RETRIES)
        self.request_timeout: float = models_cfg.get("request_timeout", DEFAULT_REQUEST_TIMEOUT)
        # stream_timeout：单次 attempt 的总预算（首包+包间+收尾）
        self.stream_timeout: float = models_cfg.get("stream_timeout", DEFAULT_STREAM_TIMEOUT)
        # 首包超时：等第一个 chunk（含响应头等待）
        self.first_token_timeout: float = models_cfg.get(
            "first_token_timeout", DEFAULT_FIRST_TOKEN_TIMEOUT
        )
        # 包间超时：两个 chunk 之间的最大间隔
        self.inter_token_timeout: float = models_cfg.get(
            "inter_token_timeout", DEFAULT_INTER_TOKEN_TIMEOUT
        )
        # 关流超时
        self.stream_close_timeout: float = models_cfg.get(
            "stream_close_timeout", DEFAULT_STREAM_CLOSE_TIMEOUT
        )
        # 重试退避上限（秒）
        self.retry_backoff_max: float = models_cfg.get(
            "retry_backoff_max", DEFAULT_RETRY_BACKOFF_MAX
        )
        self.connect_timeout: float = models_cfg.get("connect_timeout", DEFAULT_CONNECT_TIMEOUT)
        self.skip_health_model_list: bool = models_cfg.get("skip_health_model_list", False)

        max_concurrent: int = models_cfg.get("max_concurrent", DEFAULT_MAX_CONCURRENT)
        self._semaphore = asyncio.Semaphore(max_concurrent)

        self._session_stats: Dict[str, Dict[str, int]] = {}
        self._usage_callback = None
        # ★ 最近一次 usage 的 stats 快照（session_id -> dict）
        #   用于 think.py 的 push_stats 直接读，避免 get_session_stats 深拷贝
        self.last_stats: Dict[str, Dict[str, Any]] = {}

        self.high_quality_model: Optional[str] = None

        _log(f"📋 Config models keys: {list(models_cfg.keys())}")
        _log(
            f"⏱️ 超时预算: request={self.request_timeout}s "
            f"first_token={self.first_token_timeout}s "
            f"inter_token={self.inter_token_timeout}s "
            f"stream_total={self.stream_timeout}s "
            f"close={self.stream_close_timeout}s "
            f"backoff_max={self.retry_backoff_max}s "
            f"retries={self.max_retries}"
        )

        self._init_clients()

    async def close(self) -> None:
        """安全关闭所有 http client，消除资源泄漏。"""
        for cli in self._clients.values():
            try:
                await cli.close()
            except Exception as e:
                _log(f"close openai client failed: {type(e).__name__}: {e}")
        self._clients.clear()
        _info("[LLM] All provider clients closed")

    # ======================================================================
    # debug: 连接池快照
    # ======================================================================

    def _debug_pool_state(self, reason: str = "") -> dict:
        """连接池快照（debug 用）。返回 dict，同时打日志。"""
        result = {"reason": reason, "clients": {}}
        for name, client in self._clients.items():
            try:
                http_client = getattr(client, "_client", None)
                if http_client is None:
                    result["clients"][name] = {"error": "no _client attr"}
                    continue
                pool = getattr(getattr(http_client, "_transport", None), "_pool", None)
                if pool is None:
                    result["clients"][name] = {"error": "no _pool attr"}
                    continue
                conns = list(getattr(pool, "connections", []) or [])
                idle = 0
                active = 0
                for c in conns:
                    try:
                        if c.is_idle():
                            idle += 1
                        else:
                            active += 1
                    except Exception:
                        active += 1
                result["clients"][name] = {
                    "connections": len(conns),
                    "idle": idle,
                    "active": active,
                    "max_connections": 10,
                    "max_keepalive": 5,
                    "keepalive_expiry": KEEPALIVE_EXPIRY,
                }
            except Exception as e:
                result["clients"][name] = {"error": f"{type(e).__name__}: {e}"}
        try:
            tasks = [t for t in asyncio.all_tasks() if not t.done()]
            result["pending_tasks"] = len(tasks)
            result["pending_sample"] = [
                str(t.get_coro())[:120] for t in tasks[:5]
            ]
        except Exception as e:
            result["tasks_error"] = str(e)
        _info(f"[LLM][debug_pool] {json.dumps(result, ensure_ascii=False)[:600]}")
        return result

    # ======================================================================
    # usage
    # ======================================================================

    def set_usage_callback(self, callback) -> None:
        self._usage_callback = callback

    def _extract_usage(self, response: Any) -> Optional[UsageDict]:
        try:
            u = getattr(response, "usage", None)
            if not u:
                return None
            prompt = getattr(u, "prompt_tokens", 0) or 0
            completion = getattr(u, "completion_tokens", 0) or 0
            total = getattr(u, "total_tokens", 0) or (prompt + completion)
            if total <= 0:
                return None
            result = UsageDict(
                prompt_tokens=int(prompt),
                completion_tokens=int(completion),
                total_tokens=int(total),
            )
            # Extract cache tokens (Anthropic / OpenAI style)
            cache_creation = getattr(u, "cache_creation_input_tokens", 0) or 0
            cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
            # OpenAI prompt_tokens_details style
            ptd = getattr(u, "prompt_tokens_details", None)
            if ptd and not cache_read:
                cache_read = getattr(ptd, "cached_tokens", 0) or 0
            result["cache_creation_tokens"] = int(cache_creation)
            result["cache_read_tokens"] = int(cache_read)
            return result
        except Exception:
            return None

    def reset_round(self, session_id: str) -> None:
        sid = session_id or "default"
        if sid in self._session_stats:
            self._session_stats[sid]["round_total_tokens"] = 0
            self._session_stats[sid]["round_calls"] = 0
            self._session_stats[sid]["last_prompt_tokens"] = 0
            self._session_stats[sid]["last_total_tokens"] = 0
            self._session_stats[sid]["last_cache_read_tokens"] = 0

    async def _report_usage(
        self,
        usage: Optional[UsageDict],
        session_id: str,
        method: str,
        model: str,
        extra: Optional[dict] = None,
    ) -> None:
        sid = session_id or get_current_session() or "default"

        if usage:
            if sid not in self._session_stats:
                self._session_stats[sid] = {
                    "round_total_tokens": 0,
                    "round_calls": 0,
                    "last_prompt_tokens": 0,
                    "last_total_tokens": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "calls": 0,
                    "cache_read_tokens": 0,          # ★ 累计命中
                    "cache_creation_tokens": 0,      # ★ 累计创建
                    "last_cache_read_tokens": 0,
                }
            s = self._session_stats[sid]
            p = usage.get("prompt_tokens", 0)
            c = usage.get("completion_tokens", 0)
            t = usage.get("total_tokens", 0)

            s["round_total_tokens"] += t
            s["round_calls"] += 1
            s["last_prompt_tokens"] = p
            s["last_total_tokens"] = t
            s["prompt_tokens"] += p
            s["completion_tokens"] += c
            s["total_tokens"] += t
            s["calls"] += 1
            s["cache_read_tokens"] += usage.get("cache_read_tokens", 0)
            s["cache_creation_tokens"] += usage.get("cache_creation_tokens", 0)
            s["last_cache_read_tokens"] = usage.get("cache_read_tokens", 0)

            # ★ 更新 last_stats 快照（供 think.py 的 push_stats 直接读）
            self.last_stats[sid] = {
                "last_prompt_tokens": s["last_prompt_tokens"],
                "last_total_tokens": s["last_total_tokens"],
                "last_cache_read_tokens": s["last_cache_read_tokens"],
                "round_total_tokens": s["round_total_tokens"],
                "round_calls": s["round_calls"],
                "total_tokens": s["total_tokens"],
                "calls": s["calls"],
                "cache_read_tokens": s["cache_read_tokens"],
            }

            extra = extra or {}
            tool = extra.get("tool", "")
            text_len = extra.get("text_len", 0)
            tool_part = f" tool={tool}" if tool else ""
            text_part = f" text={text_len}ch" if text_len else ""
            _info(
                f"[LLM] {method} sid={sid} model={model}"
                f" P={usage['prompt_tokens']} C={usage['completion_tokens']}"
                f" T={usage['total_tokens']}{tool_part}{text_part}"
                f" | 累计 calls={s['calls']} T={s['total_tokens']}"
            )
        else:
            _info(
                f"[LLM] {method} sid={sid} model={model} usage_missing=True"
            )

        # ── 直接写 JSONL 日志（读写分离，不经过插件） ──
        try:
            now = time.time()
            provider = ""
            model_name = model
            if isinstance(model, str) and "/" in model:
                parts = model.split("/", 1)
                provider = parts[0]
                model_name = parts[1]
            _extra = extra or {}
            prompt_tokens = usage.get("prompt_tokens", 0) if usage else 0
            completion_tokens = usage.get("completion_tokens", 0) if usage else 0
            total_tokens = usage.get("total_tokens", 0) if usage else 0
            error_msg = ""
            if not usage or total_tokens == 0:
                error_msg = "No token usage reported (possible error or empty response)"
            cache_creation = usage.get("cache_creation_tokens", 0) if usage else 0
            cache_read = usage.get("cache_read_tokens", 0) if usage else 0
            _write_usage_jsonl({
                "ts": now,
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
                "date": time.strftime("%Y-%m-%d", time.localtime(now)),
                "session_id": sid,
                "method": method,
                "provider": provider,
                "model": model_name,
                "model_full": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "cache_creation_tokens": cache_creation,
                "cache_read_tokens": cache_read,
                "text_len": _extra.get("text_len", 0),
                "tool": _extra.get("tool", ""),
                "note": error_msg,
            })
        except Exception:
            pass  # 日志写入失败不影响主流程

        if self._usage_callback:
            try:
                info = {
                    "session_id": sid,
                    "method": method,
                    "model": model,
                    "usage": usage or {},
                    "extra": extra or {},
                }
                r = self._usage_callback(usage or {}, info)
                if asyncio.iscoroutine(r):
                    await r
            except Exception as exc:
                _warn(f"usage callback raised: {_sanitize_for_log(str(exc))}")

    def get_session_stats(self, session_id: str) -> dict:
        raw = self._session_stats.get(
            session_id,
            {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "calls": 0,
                "round_total_tokens": 0,
                "round_calls": 0,
                "last_prompt_tokens": 0,
                "last_total_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "last_cache_read_tokens": 0,
            },
        )
        return copy.deepcopy(raw)

    def reset_session_stats(self, session_id: str) -> None:
        self._session_stats.pop(session_id, None)

    # ======================================================================
    # Provider init
    # ======================================================================

    def _init_clients(self) -> None:
        for name, cfg in self.providers.items():
            provider = ProviderConfig(
                name=name,
                type=cfg.get("type", "openai"),
                api_key=cfg.get("api_key", ""),
                base_url=cfg.get("base_url", "https://api.openai.com/v1"),
                models=cfg.get("models", []),
                default_model=cfg.get("default_model", "") or "",
            )
            self._init_openai_provider(provider)

        self._validate_default_model()

        _log(f"🤖 Default: {self.default_model} | Models: {len(self._model_to_client)}")
        if self._roles:
            _log(f"📋 Roles: {self._roles}")

    def _init_openai_provider(self, provider: ProviderConfig) -> None:
        if not provider.api_key or provider.api_key.startswith("${"):
            _log(f"⚠️ Skip {provider.name}: API key not configured")
            return

        try:
            timeout = httpx.Timeout(
                timeout=HTTP_TIMEOUT_CONFIG["read"],
                connect=self.connect_timeout,
                read=HTTP_TIMEOUT_CONFIG["read"],
                write=HTTP_TIMEOUT_CONFIG["write"],
                pool=self.connect_timeout,
            )
            limits = httpx.Limits(
                max_keepalive_connections=5,
                max_connections=10,
                keepalive_expiry=KEEPALIVE_EXPIRY,   # ★ 15s → 5s
            )

            client = AsyncOpenAI(
                api_key=provider.api_key,
                base_url=provider.base_url,
                timeout=timeout,
                http_client=httpx.AsyncClient(timeout=timeout, limits=limits),
                max_retries=0,
            )
            self._clients[provider.name] = client

            # 模型清单不再落盘：provider 只存自己的 default_model。
            # 若旧配置里还残留 models[] 列表，优先用它（兼容），否则用 default_model。
            model_cfgs = provider.models or []
            if not model_cfgs and provider.default_model:
                model_cfgs = [{"id": provider.default_model}]

            for model_cfg in model_cfgs:
                model_id = model_cfg.get("id")
                if not model_id:
                    continue

                mcfg = ModelConfig(
                    client_name=provider.name,
                    max_tokens=model_cfg.get("max_tokens", 4096),
                    temperature=model_cfg.get("temperature", 0.7),
                    supports_streaming=model_cfg.get("supports_streaming", True),
                    high_quality=model_cfg.get("high_quality", False),
                )
                ref = f"{provider.name}/{model_id}"

                # ★ 直接赋值，不用 setdefault，避免多 provider 同模型名时指向旧配置
                self._model_configs[model_id] = mcfg
                self._model_to_client[model_id] = provider.name
                self._model_configs[ref] = mcfg
                self._model_to_client[ref] = provider.name

                if mcfg.high_quality and self.high_quality_model is None:
                    self.high_quality_model = model_id
                _log(f"✅ {model_id} ({provider.name})")

        except Exception as exc:
            _log(f"❌ Failed to init {provider.name}: {_sanitize_for_log(str(exc))}")

    def _validate_default_model(self) -> None:
        if self.default_model not in self._model_to_client:
            if self._model_to_client:
                self.default_model = next(iter(self._model_to_client))
                _log(f"⚠️ roles.default 模型不可用，改用: {self.default_model}")
            else:
                _log("❌ 没有任何可用模型！")

    # ======================================================================
    # Model resolution
    # ======================================================================

    def _resolve_model(self, role: Optional[str] = None) -> str:
        if not role:
            return self.default_model
        model = self._roles.get(role, {}).get("model")
        if model and model in self._model_to_client:
            return model
        if model:
            _log(f"⚠️ roles.{role}={model} 不可用，降级到 default")
        return self.default_model

    def _resolve_params(
        self,
        role: Optional[str],
        model: Optional[str],
        kwargs: Dict[str, Any],
    ) -> tuple[int, float]:
        """参数优先级：kwargs 显式 > roles[role] > provider.models[] > 兜底。

        role 绑定的参数只在 role 指定的模型与实际使用的模型一致时生效，
        避免「角色 A 的参数」被套用到「角色 A 降级后的别的模型」。
        """
        config = self._model_configs.get(model or "", ModelConfig(client_name="unknown"))
        max_tokens = config.max_tokens
        temperature = config.temperature

        effective_role = role or "default"
        rp = self._roles.get(effective_role) or {}
        role_model = rp.get("model")
        if role_model and role_model == (model or ""):
            if rp.get("max_tokens") is not None:
                max_tokens = rp["max_tokens"]
            if rp.get("temperature") is not None:
                temperature = rp["temperature"]

        max_tokens = kwargs.pop("max_tokens", max_tokens)
        temperature = kwargs.pop("temperature", temperature)
        return int(max_tokens), float(temperature)

    def _get_client(self, model: Optional[str] = None) -> tuple[Optional[AsyncOpenAI], str, str]:
        ref = model or self.default_model
        client_name = self._model_to_client.get(ref)
        if client_name is None:
            client_name = self._model_to_client.get(_bare_model_id(ref))
        if client_name and client_name in self._clients:
            return self._clients[client_name], ref, _bare_model_id(ref)
        if self._clients:
            name = next(iter(self._clients))
            return self._clients[name], ref, _bare_model_id(ref)
        return None, ref, _bare_model_id(ref)

    def _calculate_backoff(
        self, attempt: int, max_wait: Optional[float] = None
    ) -> float:
        limit = self.retry_backoff_max if max_wait is None else max_wait
        wait = min(2 ** attempt, limit)
        jitter = random.uniform(0, wait * 0.5)
        return wait + jitter

    # ======================================================================
    # chat（非流式纯文本）
    # ======================================================================

    async def _chat_impl(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        role: Optional[str] = None,
        session_id: str = "",
        **kwargs: Any,
    ) -> str:
        client, actual_model, api_model = self._get_client(model)

        if client is None:
            return FALLBACK_MESSAGES["not_configured"]

        max_tokens, temperature = self._resolve_params(role, actual_model, kwargs)

        _log(f"🔍 请求 URL: {client.base_url}/chat/completions")

        last_error: Optional[str] = None

        for attempt in range(self.max_retries):
            try:
                _log(f"🔄 API call {attempt + 1}/{self.max_retries} → {actual_model}")
                t0 = time.time()

                task = asyncio.create_task(
                    client.chat.completions.create(
                        model=api_model,
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        **kwargs,
                    )
                )

                try:
                    response = await asyncio.wait_for(task, timeout=self.request_timeout)
                except asyncio.TimeoutError:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                    raise

                elapsed = time.time() - t0

                if response and response.choices and response.choices[0].message.content:
                    result = response.choices[0].message.content.strip()
                    if result:
                        result = _strip_leading_think(result)
                        usage = self._extract_usage(response)
                        await self._report_usage(
                            usage,
                            session_id,
                            method="chat",
                            model=actual_model,
                            extra={"text_len": len(result)},
                        )
                        _log(f"✅ 成功 ({elapsed:.1f}s, {len(result)} chars)")
                        return result

                _log(f"⚠️ 空响应 ({elapsed:.1f}s)")
                last_error = "empty_response"

            except asyncio.TimeoutError:
                _log(f"⏱️ 超时 attempt {attempt + 1}")
                last_error = "timeout"
            except Exception as exc:
                error_msg = _sanitize_for_log(str(exc))
                kind = _classify_stream_error(exc)
                _info(
                    f"❌ chat 错误 attempt={attempt+1} kind={kind} "
                    f"type={type(exc).__name__} msg={error_msg[:120]}"
                )
                last_error = str(exc)

            if attempt < self.max_retries - 1:
                wait = self._calculate_backoff(attempt)
                _log(f"⏳ 等待 {wait:.1f}s 后重试...")
                await asyncio.sleep(wait)

        if last_error == "timeout":
            return FALLBACK_MESSAGES["timeout"]
        if last_error == "empty_response":
            return FALLBACK_MESSAGES["empty_response"]
        return f"[LLM请求失败: {_sanitize_for_log(last_error, max_len=200)}]"

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        role: Optional[str] = None,
        session_id: str = "",
        **kwargs: Any,
    ) -> str:
        if model:
            resolved_model = model
        elif role:
            resolved_model = self._resolve_model(role)
        else:
            resolved_model = self.default_model

        _log(f"💬 chat: {resolved_model}" + (f" (role={role})" if role else ""))

        try:
            # ★ 30s 超时防「semaphore 泄漏时无限等」
            try:
                await asyncio.wait_for(self._semaphore.acquire(), timeout=30.0)
            except asyncio.TimeoutError:
                _warn("[LLM] chat semaphore acquire 30s 超时，可能泄漏")
                return FALLBACK_MESSAGES["timeout"]
            try:
                result = await self._chat_impl(
                    messages, resolved_model, role=role, session_id=session_id, **kwargs
                )
                if result is None:
                    return FALLBACK_MESSAGES["system_error"]
                if not isinstance(result, str):
                    result = str(result)
                if not result.strip():
                    return FALLBACK_MESSAGES["empty"]
                return result
            finally:
                self._semaphore.release()
        except Exception as exc:
            _log(f"❌ chat 异常: {type(exc).__name__}: {_sanitize_for_log(str(exc))[:100]}")
            return f"[系统错误: {_sanitize_for_log(str(exc), max_len=100)}]"

    # ======================================================================
    # chat_stream（流式纯文本）
    # ======================================================================

    async def _chat_stream_impl(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        role: Optional[str] = None,
        session_id: str = "",
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        client, actual_model, api_model = self._get_client(model)

        if client is None:
            yield FALLBACK_MESSAGES["not_configured"]
            return

        _log(f"🔍 请求 URL: {client.base_url}/chat/completions")

        config = self._model_configs.get(actual_model, ModelConfig(client_name="unknown"))

        if not config.supports_streaming:
            result = await self._chat_impl(
                messages, model, role=role, session_id=session_id, **kwargs
            )
            if result and not is_llm_error_string(result):
                yield result
            else:
                yield FALLBACK_MESSAGES["empty"]
            return

        max_tokens, temperature = self._resolve_params(role, actual_model, kwargs)

        for attempt in range(self.max_retries):
            stream = None
            think_unclosed = False
            yielded_any = False
            stage = "响应头"
            attempt_start = time.monotonic()
            deadline = attempt_start + self.stream_timeout
            first_chunk_at: Optional[float] = None
            first_token_at: Optional[float] = None
            try:
                _log(f"📡 stream {attempt + 1}/{self.max_retries} → {actual_model}")

                stream = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=api_model,
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        stream=True,
                        stream_options={"include_usage": True},
                        **kwargs,
                    ),
                    timeout=self.request_timeout,
                )
                t_header = time.monotonic() - attempt_start

                buffer = ""
                in_think: Optional[bool] = None
                usage_acc: Optional[UsageDict] = None

                stage = "首包/包间"
                async for chunk in self._iter_stream_with_timeout(
                    stream,
                    first_token_timeout=self.first_token_timeout,
                    inter_token_timeout=self.inter_token_timeout,
                    total_deadline=deadline,
                ):
                    if first_chunk_at is None:
                        first_chunk_at = time.monotonic() - attempt_start
                    u = self._extract_usage(chunk)
                    if u:
                        usage_acc = u

                    if not (chunk.choices and chunk.choices[0].delta.content):
                        continue
                    token = chunk.choices[0].delta.content
                    if not token:
                        continue
                    if first_token_at is None:
                        first_token_at = time.monotonic() - attempt_start
                        _info(
                            f"⏱️ 首token延迟 sid={session_id or get_current_session() or 'default'} "
                            f"model={actual_model} "
                            f"header={t_header * 1000:.0f}ms "
                            f"frame={(first_chunk_at or 0.0) * 1000:.0f}ms "
                            f"ttft={first_token_at * 1000:.0f}ms"
                        )

                    buffer += token

                    if in_think is None and len(buffer) >= THINK_DECIDER_LEN:
                        if buffer.lstrip().startswith(THINK_OPEN):
                            in_think = True
                            _log("流式：检测到 <think> 开头，进入剥离模式")
                        else:
                            in_think = False

                    if in_think is False:
                        yield buffer
                        yielded_any = True
                        buffer = ""
                        continue

                    if in_think is True:
                        close_idx = buffer.find(THINK_CLOSE)
                        if close_idx != -1:
                            # 保留 think 结束后正文/代码块的缩进，只移除换行。
                            rest = buffer[close_idx + len(THINK_CLOSE):].lstrip("\r\n")
                            buffer = rest
                            in_think = False
                            if buffer:
                                yield buffer
                                yielded_any = True
                                buffer = ""

                if in_think is None:
                    if buffer:
                        buffer = _strip_leading_think(buffer)
                        if buffer:
                            yield buffer
                            yielded_any = True
                elif in_think is True:
                    # ★ 未闭合的 <think>：整段 buffer 都是推理内容
                    #   直接丢弃，不外泄给调用方（避免泄露模型内部推理）
                    _info(
                        f"流式：<think> 未闭合，丢弃 {len(buffer)} chars 推理内容"
                    )
                    buffer = ""
                    think_unclosed = True
                elif in_think is False and buffer:
                    yield buffer
                    yielded_any = True

                await self._report_usage(
                    usage_acc, session_id, method="chat_stream", model=actual_model
                )

                if think_unclosed:
                    # ★ 已 yield 过内容，重试会重复 → 不重试
                    if yielded_any:
                        _info("流式：<think> 未闭合但已输出部分内容，不重试")
                        return
                    if attempt == self.max_retries - 1:
                        yield FALLBACK_MESSAGES["empty"]
                        return
                    # 本轮未输出任何内容，安全重试
                else:
                    _log("✅ stream 完成")
                    return

            except asyncio.TimeoutError:
                _info(f"⏱️ stream 超时 stage={stage} attempt={attempt + 1}")
                if yielded_any:
                    return
                if attempt == self.max_retries - 1:
                    yield FALLBACK_MESSAGES["timeout"]
                    return
            except Exception as exc:
                kind = _classify_stream_error(exc)
                msg = _sanitize_for_log(str(exc))
                _info(
                    f"❌ stream 错误 stage={stage} attempt={attempt+1} kind={kind} "
                    f"type={type(exc).__name__} msg={msg[:150]}"
                )
                if yielded_any:
                    return
                if attempt == self.max_retries - 1:
                    yield f"[LLM stream error: {kind}: {msg[:200]}]"
                    return
            finally:
                if stream is not None:
                    try:
                        await asyncio.wait_for(
                            stream.close(), timeout=self.stream_close_timeout
                        )
                    except asyncio.TimeoutError:
                        _log(f"stream.close() 超时 {self.stream_close_timeout}s，放弃")
                    except Exception as exc:
                        _log(f"stream.close 失败: {type(exc).__name__}: {exc}")

            if attempt < self.max_retries - 1:
                await asyncio.sleep(self._calculate_backoff(attempt))

        yield FALLBACK_MESSAGES["max_retries"]

    async def chat_stream(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        role: Optional[str] = None,
        session_id: str = "",
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        if model:
            resolved_model = model
        elif role:
            resolved_model = self._resolve_model(role)
        else:
            resolved_model = self.default_model

        # ★ 30s 超时 + finally 释放，防「async generator 被丢弃时泄漏」
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=30.0)
        except asyncio.TimeoutError:
            _warn("[LLM] chat_stream semaphore acquire 30s 超时，可能泄漏")
            yield FALLBACK_MESSAGES["timeout"]
            return
        try:
            async for token in self._chat_stream_impl(
                messages, resolved_model, role=role, session_id=session_id, **kwargs
            ):
                if token:
                    yield token
        finally:
            self._semaphore.release()

    # ======================================================================
    # chat_json
    # ======================================================================

    async def chat_json(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        role: Optional[str] = None,
        session_id: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        max_json_attempts = 3
        base_messages = list(messages)
        fix_prompt = {
            "role": "system",
            "content": "请只返回有效的 JSON 格式，不要包含任何其他文本。",
        }

        for attempt in range(max_json_attempts):
            call_messages = base_messages + [fix_prompt] if attempt > 0 else base_messages

            raw = await self.chat(
                call_messages,
                model=model,
                role=role,
                session_id=session_id,
                **kwargs,
            )

            if is_llm_error_string(raw):
                if attempt == max_json_attempts - 1:
                    return {"error": raw}
                continue

            cleaned = self._extract_json(raw)

            try:
                return json.loads(cleaned)
            except json.JSONDecodeError as exc:
                _log(f"JSON 解析失败 {attempt + 1}: {exc}")
                if attempt == max_json_attempts - 1:
                    return {"content": cleaned, "parse_error": str(exc)}

        return {"content": raw, "error": "max_json_attempts_exceeded"}

    @staticmethod
    def _extract_json(raw: str) -> str:
        cleaned = raw.strip()
        if "```json" in cleaned:
            parts = cleaned.split("```json", 1)
            if len(parts) > 1:
                cleaned = parts[1].split("```", 1)[0].strip()
        elif "```" in cleaned:
            parts = cleaned.split("```")
            if len(parts) > 1:
                cleaned = parts[1].split("```", 1)[0].strip()
        return cleaned

    # ======================================================================
    # tool use（非流式）
    # ======================================================================

    @staticmethod
    def _parse_tool_call(tc: Any) -> ToolCall:
        name = ""
        args: Dict[str, Any] = {}
        tc_id = ""
        parse_error = ""
        try:
            tc_id = getattr(tc, "id", "") or ""
            fn = getattr(tc, "function", None)
            if fn is not None:
                name = getattr(fn, "name", "") or ""
                raw_args = getattr(fn, "arguments", "") or ""
                if isinstance(raw_args, dict):
                    args = raw_args
                elif isinstance(raw_args, str) and raw_args.strip():
                    try:
                        parsed = json.loads(raw_args)
                        if isinstance(parsed, dict):
                            args = parsed
                        else:
                            parse_error = f"arguments 不是 JSON object: {type(parsed).__name__}"
                    except json.JSONDecodeError as exc:
                        parse_error = f"JSONDecodeError: {exc}"
                        _info(
                            f"⚠️ tool_call arguments 不是合法 JSON: "
                            f"raw={_sanitize_for_log(raw_args, max_len=200)}"
                        )
        except Exception as exc:
            parse_error = f"parse 异常: {exc}"
            _info(f"⚠️ 解析 tool_call 失败: {exc}")
        return ToolCall(
            name=name, arguments=args, id=tc_id, parse_error=parse_error
        )

    async def chat_tool(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        model: Optional[str] = None,
        role: Optional[str] = None,
        tool_choice: Union[str, Dict[str, Any]] = "auto",
        session_id: str = "",
        **kwargs: Any,
    ) -> LLMResponse:
        if model:
            resolved_model = model
        elif role:
            resolved_model = self._resolve_model(role)
        else:
            resolved_model = self.default_model

        client, actual_model, api_model = self._get_client(resolved_model)
        if client is None:
            return LLMResponse(text=FALLBACK_MESSAGES["not_configured"])

        max_tokens, temperature = self._resolve_params(role, actual_model, kwargs)

        _log(f"🛠️ chat_tool: {actual_model} (tools={len(tools)})")

        last_error: Optional[str] = None

        for attempt in range(self.max_retries):
            try:
                t0 = time.time()

                task = asyncio.create_task(
                    client.chat.completions.create(
                        model=api_model,
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        tools=tools,
                        tool_choice=tool_choice,
                        **kwargs,
                    )
                )

                try:
                    response = await asyncio.wait_for(task, timeout=self.request_timeout)
                except asyncio.TimeoutError:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                    raise

                elapsed = time.time() - t0

                if not response or not response.choices:
                    _log(f"⚠️ chat_tool 空响应 ({elapsed:.1f}s)")
                    last_error = "empty_response"
                else:
                    msg = response.choices[0].message
                    finish_reason = getattr(response.choices[0], "finish_reason", "") or ""
                    usage = self._extract_usage(response)

                    tool_calls = getattr(msg, "tool_calls", None)
                    if tool_calls:
                        if len(tool_calls) > 1:
                            _info(
                                f"⚠️ chat_tool 返回 {len(tool_calls)} 个 tool_calls，只取第一个"
                            )
                        tc = self._parse_tool_call(tool_calls[0])
                        await self._report_usage(
                            usage,
                            session_id,
                            method="chat_tool",
                            model=actual_model,
                            extra={"tool": tc.name},
                        )
                        _log(f"✅ chat_tool 返回 tool_call: {tc.name} ({elapsed:.1f}s)")
                        return LLMResponse(
                            text="",
                            tool_call=tc,
                            raw=response,
                            finish_reason=finish_reason,
                        )

                    content = getattr(msg, "content", "") or ""
                    if content:
                        content = _strip_leading_think(content).strip()
                        await self._report_usage(
                            usage,
                            session_id,
                            method="chat_tool",
                            model=actual_model,
                            extra={"text_len": len(content)},
                        )
                        _log(
                            f"✅ chat_tool 返回 text ({elapsed:.1f}s, {len(content)} chars)"
                        )
                        return LLMResponse(
                            text=content, raw=response, finish_reason=finish_reason
                        )

                    _log(f"⚠️ chat_tool 空内容 ({elapsed:.1f}s)")
                    last_error = "empty_response"

            except asyncio.TimeoutError:
                _info(f"⏱️ chat_tool 超时 attempt={attempt + 1}")
                last_error = "timeout"
            except Exception as exc:
                kind = _classify_stream_error(exc)
                error_msg = _sanitize_for_log(str(exc))
                _info(
                    f"❌ chat_tool 错误 attempt={attempt+1} kind={kind} "
                    f"type={type(exc).__name__} msg={error_msg[:120]}"
                )
                last_error = str(exc)

            if attempt < self.max_retries - 1:
                wait = self._calculate_backoff(attempt)
                _log(f"⏳ 等待 {wait:.1f}s 后重试...")
                await asyncio.sleep(wait)

        if last_error == "timeout":
            return LLMResponse(text=FALLBACK_MESSAGES["timeout"])
        if last_error == "empty_response":
            return LLMResponse(text=FALLBACK_MESSAGES["empty_response"])
        return LLMResponse(text=f"[LLM请求失败: {_sanitize_for_log(last_error, max_len=200)}]")

    # ======================================================================
    # tool use（流式）——严格校验 + 宽容下发版
    # 原则：坏数据跳过并记日志，不 return 终止；全部坏才 ET_ERROR
    # ======================================================================

    async def chat_tool_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        model: Optional[str] = None,
        role: Optional[str] = None,
        tool_choice: Union[str, Dict[str, Any]] = "auto",
        session_id: str = "",
        **kwargs: Any,
    ) -> AsyncIterator[Dict[str, Any]]:
        if model:
            resolved_model = model
        elif role:
            resolved_model = self._resolve_model(role)
        else:
            resolved_model = self.default_model

        client, actual_model, api_model = self._get_client(resolved_model)
        if client is None:
            yield {"type": ET_ERROR, "error": FALLBACK_MESSAGES["not_configured"]}
            yield {"type": ET_DONE, "finish_reason": "error"}
            return

        config = self._model_configs.get(actual_model, ModelConfig(client_name="unknown"))

        if not config.supports_streaming:
            resp = await self.chat_tool(
                messages, tools,
                model=actual_model,
                tool_choice=tool_choice,
                session_id=session_id,
                **kwargs,
            )
            if resp.tool_call:
                if resp.tool_call.parse_error:
                    _info(
                        f"⚠️ chat_tool(非流式) tool_call 解析异常: "
                        f"name={resp.tool_call.name!r} "
                        f"reason={resp.tool_call.parse_error[:150]}"
                    )
                yield {"type": ET_TOOL_CALL, "tool_call": resp.tool_call}
            elif resp.text:
                yield {"type": ET_TEXT, "content": resp.text}
            yield {"type": ET_DONE, "finish_reason": resp.finish_reason or "stop"}
            return

        max_tokens, temperature = self._resolve_params(role, actual_model, kwargs)

        sid = session_id or get_current_session() or "default"
        _info(
            f"[LLM] chat_tool_stream 开始 sid={sid} model={actual_model}"
            f" messages={len(messages)} tools={len(tools)}"
        )

        # ★ debug：记录总开始时间
        _ctts_total_start = time.monotonic()

        for attempt in range(self.max_retries):
            stream = None
            text_buf = ""
            think_buf = ""
            usage_acc: Optional[UsageDict] = None
            tool_calls_acc: Dict[int, Dict[str, str]] = {}
            yielded_text = False
            yielded_any = False
            stage = "响应头"
            attempt_start = time.monotonic()
            deadline = attempt_start + self.stream_timeout
            first_chunk_at: Optional[float] = None
            ttft_logged = False

            try:
                _info(
                    f"📡 chat_tool_stream attempt={attempt + 1}/{self.max_retries} "
                    f"model={actual_model} msgs={len(messages)}"
                )

                # ★ FIX 1: create 加 wait_for
                stream = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=api_model,
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        tools=tools,
                        tool_choice=tool_choice,
                        stream=True,
                        stream_options={"include_usage": True},
                        **kwargs,
                    ),
                    timeout=self.request_timeout,
                )
                t_header = time.monotonic() - attempt_start

                finish_reason = ""
                in_think: Optional[bool] = None

                # ★ FIX 2: async for 用 _iter_stream_with_timeout 包
                stage = "首包/包间"
                async for chunk in self._iter_stream_with_timeout(
                    stream,
                    first_token_timeout=self.first_token_timeout,
                    inter_token_timeout=self.inter_token_timeout,
                    total_deadline=deadline,
                ):
                    if first_chunk_at is None:
                        first_chunk_at = time.monotonic() - attempt_start
                    u = self._extract_usage(chunk)
                    if u:
                        usage_acc = u

                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    delta = choice.delta

                    if not ttft_logged:
                        _has_reasoning = bool(
                            getattr(delta, "reasoning_content", None)
                            or getattr(delta, "reasoning", None)
                            or getattr(delta, "thinking", None)
                        )
                        _has_content = bool(
                            delta and getattr(delta, "content", None)
                        )
                        if _has_reasoning or _has_content:
                            ttft_logged = True
                            _info(
                                f"⏱️ 首token延迟 sid={sid} model={actual_model} "
                                f"header={t_header * 1000:.0f}ms "
                                f"frame={(first_chunk_at or 0.0) * 1000:.0f}ms "
                                f"ttft={(time.monotonic() - attempt_start) * 1000:.0f}ms"
                            )

                    if getattr(choice, "finish_reason", None):
                        finish_reason = choice.finish_reason

                    reasoning = (
                        getattr(delta, "reasoning_content", None)
                        or getattr(delta, "reasoning", None)
                        or getattr(delta, "thinking", None)
                    )
                    if reasoning:
                        yield {"type": ET_THINKING, "content": reasoning}
                        yielded_any = True

                    if delta and getattr(delta, "content", None):
                        token = delta.content
                        think_buf += token

                        if in_think is None and len(think_buf) >= THINK_DECIDER_LEN:
                            if think_buf.lstrip().startswith(THINK_OPEN):
                                in_think = True
                                _log("流式：检测到 <think> 开头，进入剥离模式")
                            else:
                                in_think = False
                                text_buf += think_buf
                                yield {"type": ET_TEXT, "content": think_buf}
                                yielded_text = True
                                yielded_any = True
                                think_buf = ""
                        elif in_think is False:
                            text_buf += think_buf
                            yield {"type": ET_TEXT, "content": think_buf}
                            yielded_text = True
                            yielded_any = True
                            think_buf = ""
                        # ★ 用 if 而非 elif：判定块里刚置 in_think=True 时，
                        #   同一 chunk 内可能已含 </think>，需要继续做闭合检测，
                        #   否则最后一个 chunk 里的正文会被当成“未闭合 think”丢弃。
                        if in_think is True:
                            close_idx = think_buf.find(THINK_CLOSE)
                            if close_idx != -1:
                                # 保留 think 结束后正文/代码块的缩进，只移除换行。
                                rest = think_buf[close_idx + len(THINK_CLOSE):].lstrip("\r\n")
                                think_buf = rest
                                in_think = False
                                if think_buf:
                                    text_buf += think_buf
                                    yield {"type": ET_TEXT, "content": think_buf}
                                    yielded_text = True
                                    yielded_any = True
                                    think_buf = ""

                    if delta and getattr(delta, "tool_calls", None):
                        for tc in delta.tool_calls:
                            raw_idx = getattr(tc, "index", None)
                            idx = 0 if raw_idx is None else int(raw_idx)
                            slot = tool_calls_acc.setdefault(
                                idx, {"name": "", "args": "", "id": ""}
                            )
                            if getattr(tc, "id", None):
                                slot["id"] = tc.id

                            fn = getattr(tc, "function", None)
                            fn_name = None
                            fn_args = None
                            if fn is not None:
                                fn_name = getattr(fn, "name", None)
                                fn_args = getattr(fn, "arguments", None)
                            if not fn_name:
                                fn_name = getattr(tc, "name", None)

                            if fn_name and not slot["name"]:
                                slot["name"] = fn_name
                            if fn_args:
                                if isinstance(fn_args, dict):
                                    slot["args"] += json.dumps(fn_args, ensure_ascii=False)
                                else:
                                    slot["args"] += fn_args

                            _log(
                                f"TC chunk idx={idx} id={slot['id']!r} "
                                f"name={slot['name']!r} args_len={len(slot['args'])}"
                            )

                            # ★ 新增：每个 delta 都下发进度事件，供前端在“等待完整 tool_call”期间显示“参数在涨”
                            yield {
                                "type": "tool_call_delta",
                                "index": idx,
                                "name": slot["name"] or "",
                                "args_delta": fn_args if isinstance(fn_args, str) else "",
                                "args_len": len(slot["args"]),
                            }

                if in_think is None and think_buf:
                    cleaned = _strip_leading_think(think_buf)
                    if cleaned:
                        text_buf += cleaned
                        yield {"type": ET_TEXT, "content": cleaned}
                        yielded_text = True
                        yielded_any = True
                    think_buf = ""
                elif in_think is True:
                    # ★ 未闭合的 <think>：整段 think_buf 都是推理内容
                    #   直接丢弃，不外泄给调用方（避免泄露模型内部推理）
                    _info(
                        f"流式：<think> 未闭合，丢弃 {len(think_buf)} chars 推理内容"
                    )
                    think_buf = ""
                elif in_think is False and think_buf:
                    text_buf += think_buf
                    yield {"type": ET_TEXT, "content": think_buf}
                    yielded_text = True
                    yielded_any = True
                    think_buf = ""

                valid_tool_calls: List[ToolCall] = []
                skipped: List[str] = []

                if tool_calls_acc:
                    if len(tool_calls_acc) > 1:
                        _info(
                            f"⚠️ chat_tool_stream 返回 {len(tool_calls_acc)} 个 tool_calls，"
                            f"按 index 顺序全部下发"
                        )

                    if text_buf and not yielded_text:
                        yield {"type": ET_TEXT, "content": text_buf}
                        yielded_text = True
                        yielded_any = True
                        text_buf = ""

                    for idx in sorted(tool_calls_acc.keys()):
                        slot = tool_calls_acc[idx]
                        tool_name = (slot["name"] or "").strip()
                        tool_args_buf = slot["args"] or ""
                        tool_id = slot["id"]

                        if not tool_name:
                            skipped.append(
                                f"idx={idx} missing_name args_len={len(tool_args_buf)}"
                            )
                            _info(
                                f"⚠️ tool_call[{idx}] 缺少 name（args_len={len(tool_args_buf)}, "
                                f"finish={finish_reason!r}），跳过该条，继续处理后续"
                            )
                            continue

                        args: Dict[str, Any] = {}
                        parse_ok = True
                        if isinstance(tool_args_buf, dict):
                            args = tool_args_buf
                        elif tool_args_buf.strip():
                            try:
                                parsed = json.loads(tool_args_buf)
                                if isinstance(parsed, dict):
                                    args = parsed
                                else:
                                    parse_ok = False
                            except (json.JSONDecodeError, TypeError) as exc:
                                _info(
                                    f"⚠️ tool_call[{idx}] arguments 解析失败: {exc}; "
                                    f"finish={finish_reason!r} "
                                    f"raw={_sanitize_for_log(str(tool_args_buf), max_len=200)}"
                                )
                                parse_ok = False

                        if not parse_ok:
                            reason = "被截断" if finish_reason == "length" else "不完整/非法"
                            skipped.append(f"idx={idx} bad_args({reason}) name={tool_name}")
                            continue
                        # 解析成功，构造 ToolCall 并加入有效列表
                        valid_tool_calls.append(
                            ToolCall(
                                name=tool_name,
                                arguments=args,
                                id=tool_id,
                            )
                        )
                        if finish_reason == "length":
                            _info(
                                f"⚠️ tool_call[{idx}] finish_reason=length 但 JSON 解析成功，"
                                f"按完整处理 name={tool_name} args_len={len(tool_args_buf)}"
                            )



                for tc in valid_tool_calls:
                    yield {"type": ET_TOOL_CALL, "tool_call": tc}
                    yielded_any = True

                if not valid_tool_calls and (tool_calls_acc or finish_reason == "tool_calls"):
                    detail = "; ".join(skipped) if skipped else "no tool_calls delta captured"
                    _info(
                        f"❌ chat_tool_stream 无可用 tool_call "
                        f"finish={finish_reason!r} detail={detail[:200]}"
                    )
                    yield {
                        "type": ET_ERROR,
                        "error": (
                            f"[LLM stream error: 无可用 tool_call "
                            f"finish={finish_reason!r} detail={detail[:200]}]"
                        ),
                    }
                    yield {"type": ET_DONE, "finish_reason": "error"}
                    await self._report_usage(
                        usage_acc, session_id,
                        method="chat_tool_stream", model=actual_model,
                        extra={"tool": "", "text_len": len(text_buf)},
                    )
                    return

                await self._report_usage(
                    usage_acc, session_id,
                    method="chat_tool_stream", model=actual_model,
                    extra={
                        "tool": valid_tool_calls[0].name if valid_tool_calls else "",
                        "text_len": len(text_buf),
                    },
                )

                yield {"type": ET_DONE, "finish_reason": finish_reason or "stop"}
                _log("✅ chat_tool_stream 完成")
                return

            except asyncio.TimeoutError:
                elapsed_s = time.monotonic() - attempt_start
                total_s = time.monotonic() - _ctts_total_start
                _info(
                    f"⏱️ chat_tool_stream 超时 stage={stage} attempt={attempt + 1} "
                    f"elapsed={elapsed_s:.1f}s total={total_s:.1f}s "
                    f"yielded_text={yielded_text} yielded_any={yielded_any}"
                )
                # ★ debug：超时时打连接池快照
                try:
                    self._debug_pool_state(
                        reason=f"chat_tool_stream_timeout:stage={stage}:attempt={attempt+1}"
                    )
                except Exception:
                    pass
                # ★ 已输出任何内容（thinking / text / tool_call）就不能整轮重放，
                #   否则前端会收到重复的 thinking。只用 yielded_text 判断会漏掉 thinking。
                if yielded_any:
                    yield {"type": ET_ERROR, "error": FALLBACK_MESSAGES["timeout"]}
                    yield {"type": ET_DONE, "finish_reason": "error"}
                    await self._report_usage(
                        usage_acc, session_id,
                        method="chat_tool_stream", model=actual_model,
                        extra={"tool": "", "text_len": len(text_buf)},
                    )
                    return
                if attempt == self.max_retries - 1:
                    yield {"type": ET_ERROR, "error": FALLBACK_MESSAGES["timeout"]}
                    yield {"type": ET_DONE, "finish_reason": "error"}
                    await self._report_usage(
                        usage_acc, session_id,
                        method="chat_tool_stream", model=actual_model,
                        extra={"tool": "", "text_len": len(text_buf)},
                    )
                    return
                yield {
                    "type": "retry",
                    "attempt": attempt + 1,
                    "max_attempts": self.max_retries,
                    "reason": "timeout",
                    "stage": stage,
                }
            except Exception as exc:
                kind = _classify_stream_error(exc)
                msg = _sanitize_for_log(str(exc))
                elapsed_s = time.monotonic() - attempt_start
                total_s = time.monotonic() - _ctts_total_start
                _info(
                    f"❌ chat_tool_stream 错误 attempt={attempt+1} kind={kind} "
                    f"type={type(exc).__name__} msg={msg[:150]} "
                    f"elapsed={elapsed_s:.1f}s total={total_s:.1f}s"
                )
                # ★ debug：异常时也打连接池快照
                try:
                    self._debug_pool_state(
                        reason=f"chat_tool_stream_error:{kind}:attempt={attempt+1}"
                    )
                except Exception:
                    pass
                if yielded_any:
                    yield {
                        "type": ET_ERROR,
                        "error": f"[LLM stream error: {kind}: {msg[:200]}]",
                    }
                    yield {"type": ET_DONE, "finish_reason": "error"}
                    await self._report_usage(
                        usage_acc, session_id,
                        method="chat_tool_stream", model=actual_model,
                        extra={"tool": "", "text_len": len(text_buf)},
                    )
                    return
                if attempt == self.max_retries - 1:
                    yield {
                        "type": ET_ERROR,
                        "error": f"[LLM stream error: {kind}: {msg[:200]}]",
                    }
                    yield {"type": ET_DONE, "finish_reason": "error"}
                    await self._report_usage(
                        usage_acc, session_id,
                        method="chat_tool_stream", model=actual_model,
                        extra={"tool": "", "text_len": len(text_buf)},
                    )
                    return
                # ★ 新增：网络/解析错误时，也告诉前端“正在重试”
                yield {
                    "type": "retry",
                    "attempt": attempt + 1,
                    "max_attempts": self.max_retries,
                    "reason": kind,
                    "stage": stage,
                }
            finally:
                # ★ FIX 3: stream.close() 加超时 3s
                if stream is not None:
                    try:
                        await asyncio.wait_for(
                            stream.close(), timeout=self.stream_close_timeout
                        )
                    except asyncio.TimeoutError:
                        _log(f"stream.close() 超时 {self.stream_close_timeout}s，放弃")
                    except Exception as exc:
                        _log(f"stream.close 失败: {type(exc).__name__}: {exc}")

            if attempt < self.max_retries - 1:
                await asyncio.sleep(self._calculate_backoff(attempt))

        yield {"type": ET_ERROR, "error": FALLBACK_MESSAGES["max_retries"]}
        yield {"type": ET_DONE, "finish_reason": "error"}

    async def _iter_stream_with_timeout(
        self,
        stream: Any,
        first_token_timeout: float,
        inter_token_timeout: float,
        total_deadline: Optional[float] = None,
    ) -> AsyncIterator[Any]:
        """流式迭代 + 首包超时 + 包间超时 + 单次流总预算。

        ★ v4.1 修复：用 create_task + shield + cancel，防 SDK 不响应 cancel 时 wait_for 卡住。
        """
        first = True
        while True:
            timeout = first_token_timeout if first else inter_token_timeout
            if total_deadline is not None:
                remaining = total_deadline - time.monotonic()
                if remaining <= 0:
                    _info("⏱️ stream 总预算耗尽")
                    raise asyncio.TimeoutError("stream total timeout")
                timeout = min(timeout, remaining)

            # ★ create_task + shield，让 wait_for 超时后能显式 cancel
            task = asyncio.create_task(stream.__anext__())
            try:
                chunk = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError:
                stage = "首包" if first else "包间"
                _info(f"⏱️ stream {stage}超时 budget={timeout:.1f}s，cancel __anext__")
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
                except Exception:
                    pass
                raise
            except Exception:
                # 非超时异常（含 SDK 内部异常），也确保 task 被 cancel
                if not task.done():
                    task.cancel()
                    try:
                        await asyncio.wait_for(task, timeout=2.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass
                    except Exception:
                        pass
                raise
            first = False
            yield chunk

    # ======================================================================
    # Health check
    # ======================================================================

    async def health_check(self) -> bool:
        if self.skip_health_model_list:
            if self._clients:
                _info("[LLM] health_check skip /v1/models by config")
                return True
            return False
        for name, client in self._clients.items():
            try:
                await asyncio.wait_for(
                    client.models.list(), timeout=DEFAULT_HEALTH_CHECK_TIMEOUT
                )
                _log(f"✅ health check: {name} OK")
                return True
            except Exception as exc:
                _log(f"⚠️ health check: {name} failed: {_sanitize_for_log(str(exc))}")
                continue
        return False
