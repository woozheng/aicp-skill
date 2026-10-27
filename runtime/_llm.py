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
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Union, TypedDict

import httpx
from openai import AsyncOpenAI

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
_KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9]{20,}")


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

DEFAULT_REQUEST_TIMEOUT: float = 60.0
DEFAULT_STREAM_TIMEOUT: float = 300.0
DEFAULT_CONNECT_TIMEOUT: float = 30.0
DEFAULT_MAX_RETRIES: int = 3
DEFAULT_MAX_CONCURRENT: int = 10
THINK_DECIDER_LEN: int = 10

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"

HTTP_TIMEOUT_CONFIG = {
    "connect": DEFAULT_CONNECT_TIMEOUT,
    "read":600.0,
    "write": 60.0,
    "pool": DEFAULT_CONNECT_TIMEOUT,
}

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
    return stripped[close_idx + len(THINK_CLOSE):].lstrip()


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
        logging.basicConfig(level=logging.DEBUG)
        logging.getLogger("aicp.llm").setLevel(logging.DEBUG)
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
        self.stream_timeout: float = models_cfg.get("stream_timeout", DEFAULT_STREAM_TIMEOUT)
        self.connect_timeout: float = models_cfg.get("connect_timeout", DEFAULT_CONNECT_TIMEOUT)
        self.skip_health_model_list: bool = models_cfg.get("skip_health_model_list", False)

        max_concurrent: int = models_cfg.get("max_concurrent", DEFAULT_MAX_CONCURRENT)
        self._semaphore = asyncio.Semaphore(max_concurrent)

        self._session_stats: Dict[str, Dict[str, int]] = {}
        self._usage_callback = None

        self.high_quality_model: Optional[str] = None

        _log(f"📋 Config models keys: {list(models_cfg.keys())}")
        _log(f"⏱️ request_timeout: {self.request_timeout}s")

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
            return UsageDict(
                prompt_tokens=int(prompt),
                completion_tokens=int(completion),
                total_tokens=int(total),
            )
        except Exception:
            return None

    def reset_round(self, session_id: str) -> None:
        sid = session_id or "default"
        if sid in self._session_stats:
            self._session_stats[sid]["round_total_tokens"] = 0
            self._session_stats[sid]["round_calls"] = 0
            self._session_stats[sid]["last_prompt_tokens"] = 0
            self._session_stats[sid]["last_total_tokens"] = 0

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
                keepalive_expiry=15.0,
            )

            client = AsyncOpenAI(
                api_key=provider.api_key,
                base_url=provider.base_url,
                timeout=timeout,
                http_client=httpx.AsyncClient(timeout=timeout, limits=limits),
                max_retries=0,
            )
            self._clients[provider.name] = client

            for model_cfg in provider.models:
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

    @staticmethod
    def _calculate_backoff(attempt: int, max_wait: int = 10) -> float:
        wait = min(2 ** attempt, max_wait)
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
            async with self._semaphore:
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
            if result and not result.startswith("["):
                yield result
            else:
                yield FALLBACK_MESSAGES["empty"]
            return

        max_tokens, temperature = self._resolve_params(role, actual_model, kwargs)

        for attempt in range(self.max_retries):
            stream = None
            think_unclosed = False
            yielded_any = False
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

                buffer = ""
                in_think: Optional[bool] = None
                usage_acc: Optional[UsageDict] = None

                async for chunk in self._iter_stream_with_timeout(
                    stream,
                    first_token_timeout=self.request_timeout,
                    inter_token_timeout=60.0,
                ):
                    u = self._extract_usage(chunk)
                    if u:
                        usage_acc = u

                    if not (chunk.choices and chunk.choices[0].delta.content):
                        continue
                    token = chunk.choices[0].delta.content
                    if not token:
                        continue

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
                            rest = buffer[close_idx + len(THINK_CLOSE):].lstrip()
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
                    # ★ FIX: 未闭合不再判失败，宽容吐出最后一个 <think> 之后的尾部
                    last_open = buffer.rfind(THINK_OPEN)
                    tail = (
                        buffer[last_open + len(THINK_OPEN):]
                        if last_open != -1
                        else buffer
                    )
                    if tail:
                        yield tail
                        yielded_any = True
                        _info(f"流式：<think> 未闭合，宽容吐出尾部 {len(tail)} chars")
                    buffer = ""
                    think_unclosed = False  # 不再触发失败重试
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
                _info(f"⏱️ stream 超时 attempt={attempt + 1}")
                if yielded_any:
                    return
                if attempt == self.max_retries - 1:
                    yield FALLBACK_MESSAGES["timeout"]
                    return
            except Exception as exc:
                kind = _classify_stream_error(exc)
                msg = _sanitize_for_log(str(exc))
                _info(
                    f"❌ stream 错误 attempt={attempt+1} kind={kind} "
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
                        await asyncio.wait_for(stream.close(), timeout=10.0)
                    except asyncio.TimeoutError:
                        _log("stream.close() 超时 10s，放弃")
                    except Exception as exc:
                        _log(f"stream.close 失败: {type(exc).__name__}: {exc}")

            if attempt < self.max_retries - 1:
                await asyncio.sleep(self._calculate_backoff(attempt, max_wait=10))

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

        async with self._semaphore:
            async for token in self._chat_stream_impl(
                messages, resolved_model, role=role, session_id=session_id, **kwargs
            ):
                if token:
                    yield token

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

            if raw.startswith("[") and raw.endswith("]"):
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

        for attempt in range(self.max_retries):
            stream = None
            text_buf = ""
            think_buf = ""
            usage_acc: Optional[UsageDict] = None
            tool_calls_acc: Dict[int, Dict[str, str]] = {}
            yielded_text = False
            yielded_any = False

            try:
                _log(f"📡 chat_tool_stream {attempt + 1}/{self.max_retries} → {actual_model}")

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

                finish_reason = ""
                in_think: Optional[bool] = None

                # ★ FIX 2: async for 用 _iter_stream_with_timeout 包
                async for chunk in self._iter_stream_with_timeout(
                    stream,
                    first_token_timeout=self.request_timeout,
                    inter_token_timeout=60.0,
                ):
                    u = self._extract_usage(chunk)
                    if u:
                        usage_acc = u

                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    delta = choice.delta

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
                        elif in_think is True:
                            close_idx = think_buf.find(THINK_CLOSE)
                            if close_idx != -1:
                                rest = think_buf[close_idx + len(THINK_CLOSE):].lstrip()
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

                if in_think is None and think_buf:
                    cleaned = _strip_leading_think(think_buf)
                    if cleaned:
                        text_buf += cleaned
                        yield {"type": ET_TEXT, "content": cleaned}
                        yielded_text = True
                        yielded_any = True
                    think_buf = ""
                elif in_think is True:
                    last_open = think_buf.rfind(THINK_OPEN)
                    tail = (
                        think_buf[last_open + len(THINK_OPEN):]
                        if last_open != -1
                        else think_buf
                    )
                    if tail:
                        text_buf += tail
                        yield {"type": ET_TEXT, "content": tail}
                        yielded_text = True
                        yielded_any = True
                        _info(f"流式：<think> 未闭合，宽容吐出尾部 {len(tail)} chars")
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

                        if finish_reason == "length":
                            _info(
                                f"⚠️ tool_call[{idx}] finish_reason=length 但 JSON 解析成功，"
                                f"按完整处理 name={tool_name} args_len={len(tool_args_buf)}"
                            )

                        valid_tool_calls.append(
                            ToolCall(name=tool_name, arguments=args, id=tool_id)
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
                _info(f"⏱️ chat_tool_stream 超时 attempt={attempt + 1}")
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
            except Exception as exc:
                kind = _classify_stream_error(exc)
                msg = _sanitize_for_log(str(exc))
                _info(
                    f"❌ chat_tool_stream 错误 attempt={attempt+1} kind={kind} "
                    f"type={type(exc).__name__} msg={msg[:150]}"
                )
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
            finally:
                # ★ FIX 3: stream.close() 加超时
                if stream is not None:
                    try:
                        await asyncio.wait_for(stream.close(), timeout=10.0)
                    except asyncio.TimeoutError:
                        _log("stream.close() 超时 10s，放弃")
                    except Exception as exc:
                        _log(f"stream.close 失败: {type(exc).__name__}: {exc}")

            if attempt < self.max_retries - 1:
                await asyncio.sleep(self._calculate_backoff(attempt, max_wait=10))

        yield {"type": ET_ERROR, "error": FALLBACK_MESSAGES["max_retries"]}
        yield {"type": ET_DONE, "finish_reason": "error"}

    async def _iter_stream_with_timeout(
        self,
        stream: Any,
        first_token_timeout: float = 90.0,
        inter_token_timeout: float = 60.0,
    ) -> AsyncIterator[Any]:
        """流式迭代 + 首包超时 + 包间超时。

        - first_token_timeout：等第一个 chunk 的超时（等响应头 + 首 token）
        - inter_token_timeout：两个 chunk 之间的超时
        """
        first = True
        while True:
            timeout = first_token_timeout if first else inter_token_timeout
            try:
                chunk = await asyncio.wait_for(
                    stream.__anext__(), timeout=timeout
                )
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError:
                _info(
                    f"⏱️ stream {'首包' if first else '包间'}超时 {timeout}s"
                )
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
                await asyncio.wait_for(client.models.list(), timeout=5.0)
                _log(f"✅ health check: {name} OK")
                return True
            except Exception as exc:
                _log(f"⚠️ health check: {name} failed: {_sanitize_for_log(str(exc))}")
                continue
        return False
